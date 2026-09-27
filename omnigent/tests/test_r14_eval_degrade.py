"""r14 E-1: a slice-eval worktree bind failure never kills the driver.

Live defect (release 4680b7e): `_slice_eval_worktree` raised TrioctlError
("cannot bind slice-eval worktree: ...") out of `OmnigentRunner.run`, which
ended `trioctl omnigent loop` fatally. It now logs once, records
`eval_isolation: degraded (<reason>)` in the dispatch's session bookkeeping
and LOG.md, and runs that slice-eval on the root-bound non-isolated path.

Offline: real git scratch repo, fake broker client and dispatch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_worker_worktrees import GIT_ENV, MODULE, SCRIPT, _load, git


@pytest.fixture()
def wt(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return _load("worker_worktrees_r14_degrade", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r14_degrade", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    (repo / "loop").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    (repo / "loop" / "LOG.md").write_text("# Trio loop log\n- iter 1 | lead | x")  # no EOL
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class _Client:
    def __init__(self):
        self.workspaces = []

    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        self.workspaces.append(workspace)
        return {"id": f"s{len(self.workspaces)}"}

    def wait_session(self, session_id, timeout=None, interval=None):
        return {"status": "idle"}

    def get_items(self, session_id):
        return []


def _runner(trioctl, repo, root, monkeypatch):
    runner = trioctl.OmnigentRunner(
        repo=repo, broker_client=_Client(), config={}, interval=0,
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "grade it\n")
    seen = []

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        seen.append({
            "workspace": workspace, "prompt": prompt,
            "fence": trioctl.worker_worktrees.active_fence(repo),
            "inflight": runner.inflight_sessions(),
        })
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner, seen


def test_bind_failure_degrades_to_root_bound_eval(trioctl, wt, repo, tmp_path, monkeypatch, capsys):
    def refuse(*_a, **_k):
        raise wt.WorktreeError("worktree .cursor/mcp.json declares an 'omnigent' MCP server")

    monkeypatch.setattr(wt, "create", refuse)
    runner, seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    # The open-loop Lead is live at the root: a degraded eval must never
    # wait for / release the root, nor prune anything.
    monkeypatch.setattr(runner, "_release_root",
                        lambda *_a, **_k: pytest.fail("degraded eval must not release the root"))
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda *_a, **_k: pytest.fail("nothing may be pruned"))
    runner._inflight.add("lead-session")
    sha = git(repo, "rev-parse", "HEAD")
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": sha}

    assert runner.run("evaluator", 1, repo / "loop", ctx) == 0

    (dispatched,) = seen
    assert dispatched["workspace"] == str(repo)
    assert runner._client().workspaces == [str(repo)]
    assert "ISOLATED EVALUATOR WORKSPACE" not in dispatched["prompt"]
    assert dispatched["fence"] is None
    meta = dispatched["inflight"]["s1"]
    assert meta["kind"] == "slice-eval" and meta["slice"] == "A"
    assert meta["eval_isolation"].startswith("degraded (cannot bind slice-eval worktree: ")
    assert runner.eval_isolation_degraded == [
        {"slice": "A", "sha": sha, "reason": meta["eval_isolation"][len("degraded ("):-1]}
    ]
    log = (repo / "loop" / "LOG.md").read_text()
    assert log.startswith("# Trio loop log\n- iter 1 | lead | x\n")
    last = log.splitlines()[-1]
    assert last.startswith(f"- iter 1 | loop | slice-eval A @{sha[:12]} eval_isolation: degraded (")
    err = capsys.readouterr().err
    assert err.count("eval_isolation: degraded") == 1
    assert "root-bound (non-isolated)" in err
    # Recorded as a root session (ownership evidence for the root restore).
    assert "s1" in wt.root_owned_sessions(repo)


def test_bind_failure_of_unexpected_os_error_also_degrades(trioctl, wt, repo, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError("worktree root not writable")

    monkeypatch.setattr(wt, "create", boom)
    runner, seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "B",
           "sha": git(repo, "rev-parse", "HEAD")}
    assert runner.run("evaluator", 2, repo / "loop", ctx) == 0
    assert seen[0]["workspace"] == str(repo)
    assert "not writable" in seen[0]["inflight"]["s1"]["eval_isolation"]


def test_tracked_session_config_binds_isolated_eval_without_degrading(
    trioctl, wt, repo, tmp_path, monkeypatch, capsys
):
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"omnigent": {
        "command": "/home/alex/py", "args": ["-I", "-m", "omnigent.claude_native_bridge",
                                             "serve-mcp", "--bridge-dir", "/tmp/x"]}}}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked cursor config")
    root = tmp_path / "worktrees"
    runner, seen = _runner(trioctl, repo, root, monkeypatch)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A",
           "sha": git(repo, "rev-parse", "HEAD")}
    assert runner.run("evaluator", 1, repo / "loop", ctx) == 0
    assert Path(seen[0]["workspace"]).parent == root.resolve()
    assert "eval_isolation" not in seen[0]["inflight"]["s1"]
    assert "degraded" not in capsys.readouterr().err
    assert "eval_isolation" not in (repo / "loop" / "LOG.md").read_text()

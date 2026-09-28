"""r11: Omnigent root ``.cursor`` residue in NON-isolated runs.

Live evidence (speed/hard/runs/openrouter/S, --no-isolate-workers): every
session's workspace is the root, cursor-native left ``.cursor/{mcp,hooks}.json``
there, and ``_finalize_ship`` refused the bound SHIP (exit 6, "untracked
product paths: .cursor/hooks.json, .cursor/mcp.json").

Since r16b there is no trioctl-level root-config baseline/restore any more
(the root machinery is gone; every git-checkout loop runs in its own Lead
worktree instead) -- these tests now cover the one remaining layer: the
loop core (`_evaluated_product_problem`/`owned_residue_check`) does not
count EXACT Omnigent-generated `.cursor/{mcp,hooks}.json` residue as
product, wherever it lands. A user file (any other `.cursor` path or
user-edited content) still blocks, and nothing removes it -- there is no
restore any more. Offline, real git, real loop core, a raw `OmnigentRunner`
(no `isolate_workers`/`root_free`: dispatches land straight in `repo`, as a
mailbox outside any Lead-worktree plumbing does).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from test_worker_worktrees_r4 import (  # noqa: E402  (sibling helpers)
    GIT_ENV,
    MODULE,
    REPO_ROOT,
    SCRIPT,
    _LaunchingClient,
    _load,
    _open_loop_repo,
    git,
)

OWNED = (".cursor/mcp.json", ".cursor/hooks.json")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("TRIO_RETIREMENT_WAIT_SECONDS", "0")
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_r11c", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r11c", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def loop_core():
    return _load("trio_loop_r11c", REPO_ROOT / "metrics" / "trio_loop.py")


@pytest.fixture()
def repo(tmp_path):
    """A product repo that does NOT ignore .cursor/."""
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def _lockstep_repo(repo: Path) -> Path:
    mailbox = repo / "loop"
    (mailbox / "GOAL.md").write_text("# Goal\nship the demo\n")
    (mailbox / "STATE.md").write_text(
        "schema: 1\niteration: 0\nmax_iterations: 5\nstatus: ready\nphase: idle\n"
        "mission: ship the demo\n")
    (mailbox / "PLAN.md").write_text(
        "```yaml\nslices:\n  - id: demo\n    writes: [demo.txt]\n    reads: []\n"
        "    accepts: [\"demo exists\"]\n```\n")
    (mailbox / "REPORT.md").write_text("")
    (mailbox / "VERDICT.md").write_text("")
    (mailbox / "LOG.md").write_text("# Trio loop log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "loop: mailbox")
    return mailbox


def _runner(trioctl, repo, monkeypatch, client, *, isolate=None,
            evaluator_drops=()):
    """Non-isolated runner (no `isolate_workers`/`root_free`: r16b has no
    trioctl-level root-config management left to configure).

    Every session launches at the root (the fake client writes Omnigent's
    exact generated config there). The Lead commits the product itself.
    *evaluator_drops*: extra root files the evaluator session leaves.
    """
    runner = trioctl.OmnigentRunner(
        repo=repo, broker_client=client, config={}, interval=0,
        isolate_workers=isolate,
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: role)
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "p\n")
    ended: list[str] = []
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda client, mailbox, session_ids=None, **kw:
                        ended.extend(session_ids) or {"archived": len(session_ids),
                                                      "deleted": len(session_ids)})

    def commit_mailbox(message):
        git(repo, "add", "loop")
        git(repo, "commit", "-q", "--allow-empty", "-m", message)

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        assert Path(workspace) == repo                     # non-isolated: the root
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        ctx = ctx or {}
        if role == "lead":
            (repo / "demo.txt").write_text(f"demo {iteration}\n")
            git(repo, "add", "demo.txt")
            git(repo, "commit", "-q", "--allow-empty", "-m", "slice(demo): add demo")
            sha = git(repo, "rev-parse", "HEAD")
            if (mailbox / "QUEUE.md").is_file():
                (mailbox / "QUEUE.md").write_text(
                    "```yaml\nretired:\n"
                    f"  - slice: demo\n    sha: {sha}\n    at: 2026-01-01T00:00:00Z\n"
                    "```\n\n```yaml\nfaults:\n```\n")
            with open(mailbox / "LOG.md", "a") as fh:
                fh.write(f"- iter {iteration} | lead | demo\n")
            commit_mailbox(f"loop: lead pass {iteration}")
            return 0
        if ctx.get("kind") == "slice-eval":
            with open(mailbox / "VERDICT.md", "a") as fh:
                fh.write(f"## slice demo @{ctx['sha']} — SHIP\n")
            return 0
        for rel in evaluator_drops:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("{}\n")
        it = ctx.get("iteration", iteration)
        body = ["VERDICT: SHIP", "", f"iteration: {it}",
                f"attempt: {ctx['evaluator_attempt']}", f"evaluated: {ctx['pinned_sha']}",
                f"commit: {ctx['pinned_sha']}"]
        old = (mailbox / "VERDICT.md").read_text()
        (mailbox / "VERDICT.md").write_text("\n".join(body) + "\n\n" + old)
        with open(mailbox / "LOG.md", "a") as fh:
            fh.write(f"- iter {it} | evaluator | VERDICT: SHIP\n")
        commit_mailbox(f"loop: iteration {it} — SHIP")
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner, ended


def _drive(loop_core, mailbox, repo, runner, mode):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "code", loop_core.run_loop(mailbox, 5, runner, repo=repo, mode=mode,
                                   poll_seconds=0.01)), daemon=True)
    t.start()
    t.join(90)
    assert "code" in result, "driver did not finish"
    return result["code"]


def _setup(mode, repo):
    return _open_loop_repo(repo) if mode == "open-loop" else _lockstep_repo(repo)


# ------------------------------------------------------------ (a) + (d)


@pytest.mark.parametrize("mode", ["open-loop", "lockstep"])
def test_root_residue_does_not_block_ship_finalizing(
    trioctl, wt, repo, loop_core, monkeypatch, mode
):
    """r16b: no trioctl-level restore any more -- the loop core alone must
    not count the sessions' exact generated `.cursor/{mcp,hooks}.json` as
    product, so SHIP still finalizes and the residue is simply left behind
    (nothing manages or removes it)."""
    mailbox = _setup(mode, repo)
    client = _LaunchingClient()
    runner, ended = _runner(trioctl, repo, monkeypatch, client)
    assert _drive(loop_core, mailbox, repo, runner, mode) == 0
    state = loop_core._read_state(mailbox / "STATE.md")
    assert state["status"] == "shipped"
    assert all(Path(ws) == repo for _s, _t, ws in client.created)
    assert ended == []  # no root_free: nothing ends a session behind the dispatch
    for rel in OWNED:
        assert (repo / rel).is_file(), rel  # left in place, never restored


# ---------------------------------------------------------------- (b)


@pytest.mark.parametrize("mode", ["open-loop", "lockstep"])
def test_user_cursor_file_is_kept_and_still_blocks(
    trioctl, wt, repo, loop_core, monkeypatch, mode
):
    mailbox = _setup(mode, repo)
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "other.json").write_text('{"mine": true}\n')
    runner, _ended = _runner(trioctl, repo, monkeypatch, _LaunchingClient())
    assert _drive(loop_core, mailbox, repo, runner, mode) == 6   # needs_retirement
    assert (repo / ".cursor" / "other.json").read_text() == '{"mine": true}\n'
    # r16b: nothing restores or removes any of it any more.
    for rel in OWNED:
        assert (repo / rel).is_file(), rel


def test_user_cursor_file_dropped_by_evaluator_session_still_blocks(
    trioctl, wt, repo, loop_core, monkeypatch
):
    mailbox = _open_loop_repo(repo)
    runner, _ended = _runner(trioctl, repo, monkeypatch, _LaunchingClient(),
                             evaluator_drops=(".cursor/other.json",))
    assert _drive(loop_core, mailbox, repo, runner, "open-loop") == 6
    assert (repo / ".cursor" / "other.json").is_file()
    problem = loop_core._evaluated_product_problem(
        repo, mailbox, loop_core._read_state(mailbox / "STATE.md")["evaluated_sha"])
    assert problem == "untracked product paths: .cursor/other.json"


def test_preexisting_user_mcp_still_blocks(
    trioctl, wt, repo, loop_core, monkeypatch
):
    """A preexisting user server in `.cursor/mcp.json` (merged, not replaced,
    by cursor-native's own launch) is still product -- r16b has no restore
    to make it disappear or go back to its original bytes."""
    user = '{"mcpServers": {"docs": {"command": "docs-mcp"}}}\n'
    mailbox = _open_loop_repo(repo)
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text(user)
    runner, _ended = _runner(trioctl, repo, monkeypatch, _LaunchingClient())
    assert _drive(loop_core, mailbox, repo, runner, "open-loop") == 6
    merged = json.loads((repo / ".cursor" / "mcp.json").read_text())
    servers = merged["mcpServers"]
    assert "docs" in servers and "omnigent" in servers   # merged, not restored


def test_user_edited_generated_config_is_product(repo, loop_core, wt):
    from test_worker_worktrees_r4 import omnigent_launch
    import json
    omnigent_launch(repo, "s1")
    head = git(repo, "rev-parse", "HEAD")
    assert loop_core._evaluated_product_problem(repo, repo / "loop", head) is None
    data = json.loads((repo / ".cursor" / "hooks.json").read_text())
    data["hooks"]["afterFileEdit"] = [{"command": "fmt"}]
    (repo / ".cursor" / "hooks.json").write_text(json.dumps(data))
    assert loop_core._evaluated_product_problem(repo, repo / "loop", head) == (
        "untracked product paths: .cursor/hooks.json")
    git(repo, "add", ".cursor/mcp.json")                           # tracked: never exempt
    assert "staged" in loop_core._evaluated_product_problem(repo, repo / "loop", head)


def test_loop_core_without_fingerprint_fails_closed(repo, loop_core, monkeypatch):
    from test_worker_worktrees_r4 import omnigent_launch
    omnigent_launch(repo, "s1")
    monkeypatch.setattr(loop_core, "_SIBLING_RESIDUE_CHECK", [None])
    monkeypatch.setattr(loop_core, "owned_residue_check", None)
    head = git(repo, "rev-parse", "HEAD")
    assert "untracked product paths" in loop_core._evaluated_product_problem(
        repo, repo / "loop", head)


def test_trioctl_injects_its_fingerprint_into_the_loop_core(trioctl, wt, repo):
    core = trioctl._load_trio_loop(REPO_ROOT)
    assert core.owned_residue_check is wt.owned_residue

# (c) isolated-vs-default root-config management is gone with r16b (deleted:
# `_manages_root_config`, `restore_root_config_final`, `_restore_root_nonisolated`,
# `_end_root_evaluator`, `_end_root_sessions_nonisolated`, `_release_root`,
# `_prepare_root_config` -- no root to manage or release any more).

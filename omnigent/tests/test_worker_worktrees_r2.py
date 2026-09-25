"""Adversarial regressions for the independent ITERATE on aca01ef (H1, M1-M4, L1-L3).

Offline. Acceptance tests drive the real loop-core retirement contract
(metrics/trio_loop.py) through trioctl's ``_ship_acceptance``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SCRIPT = ROOT / "trioctl"
MODULE = ROOT / "worker_worktrees.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}
SESSION_MCP = {"mcpServers": {"omnigent": {"command": "/usr/bin/python3", "args": [
    "-I", "-m", "omnigent.harnesses.claude_native.bridge", "serve-mcp",
    "--bridge-dir", "/bridges/some-other-session"]}}}
SESSION_HOOKS = {"version": 1, "hooks": {"stop": [{"command": (
    "/usr/bin/python3 -I -m omnigent.harnesses.cursor_native.usage record-usage "
    "--bridge-dir /bridges/some-other-session")}]}}


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_r2", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r2", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def loop_core():
    return _load("trio_loop_r2", REPO_ROOT / "metrics" / "trio_loop.py")


@pytest.fixture()
def repo(tmp_path):
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


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


def integrated_worker(wt, repo, root, slice_id="A"):
    record = wt.create(repo, slice_id=slice_id, mailbox=repo / "loop", root=root)
    (Path(record["path"]) / f"{slice_id}.txt").write_text(f"{slice_id}\n")
    wt.mark_exited(repo, wt.load_record(repo, record["id"]), 0)
    return wt.integrate(repo, record["id"])


def retire_ship(repo: Path, evaluated: str, *, iteration: int = 1, attempt: str = "a-1",
                verdict_evaluated: str | None = None, commit_line: str | None = None,
                state_status: str = "shipped") -> None:
    """A driver-finalized, Evaluator-retired SHIP (real mailbox contract)."""
    mailbox = repo / "loop"
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: SHIP\n\n"
        f"attempt: {attempt}\n"
        f"evaluated: {verdict_evaluated or evaluated}\n"
        f"commit: {commit_line or evaluated}\n"
    )
    git(repo, "add", "loop/VERDICT.md")
    git(repo, "commit", "-q", "-m", f"loop: iteration {iteration} — SHIP")
    (mailbox / "STATE.md").write_text(
        "schema: 1\n"
        f"iteration: {iteration}\n"
        "max_iterations: 3\n"
        f"status: {state_status}\n"
        "mission: test\n"
        f"evaluator_attempt: {attempt}\n"
        f"evaluated_sha: {evaluated}\n"
    )


def acceptance(trioctl, repo, loop_core):
    return lambda box: trioctl._ship_acceptance(box, repo, loop_core)


# ------------------------------------------------------------------- H1


@pytest.mark.parametrize("files", [
    {"mcp.json": SESSION_MCP},
    {"hooks.json": SESSION_HOOKS},
    {"mcp.json": {"mcpServers": {"omnigent": {"command": "x", "args": []}}}},
    {"mcp.json": "not json"},
])
def test_inherited_session_bound_user_config_refuses_isolation(wt, home, repo, root, files):
    (home / ".cursor").mkdir()
    for name, data in files.items():
        (home / ".cursor" / name).write_text(data if isinstance(data, str) else json.dumps(data))
    before = {p.name: p.read_bytes() for p in (home / ".cursor").iterdir()}
    with pytest.raises(wt.WorktreeError, match="refusing isolated dispatch"):
        wt.create(repo, slice_id="A", mailbox=repo / "loop", root=root)
    # Nothing created, nothing recorded, the user's config untouched.
    assert not root.exists() or not any(root.iterdir())
    assert wt.list_records(repo) == []
    assert {p.name: p.read_bytes() for p in (home / ".cursor").iterdir()} == before


def test_user_servers_without_session_bindings_are_allowed(wt, home, repo, root):
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"github": {"command": "gh-mcp", "args": []}}}))
    assert wt.create(repo, slice_id="A", mailbox=repo / "loop", root=root)["state"] == "created"


def test_symlinked_user_cursor_dir_is_uncertain(wt, home, repo, root, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (home / ".cursor").symlink_to(real)
    with pytest.raises(wt.WorktreeError):
        wt.create(repo, slice_id="A", mailbox=repo / "loop", root=root)


def test_system_omnigent_hook_refuses(wt, home, tmp_path):
    system = tmp_path / "etc-hooks.json"
    system.write_text(json.dumps(SESSION_HOOKS))
    assert wt.inherited_cursor_problems(home=home, system_hooks=(system,))


def test_isolated_run_cli_refuses_with_contaminated_home(repo, root, tmp_path):
    home = tmp_path / "chome"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "mcp.json").write_text(json.dumps(SESSION_MCP))
    task = tmp_path / "task.md"
    task.write_text("x\n")
    env = dict(os.environ, **GIT_ENV, HOME=str(home), PATH="/usr/bin:/bin")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "omnigent", "run", "builder",
         "--config", str(ROOT / "trioctl.example.toml"), "--isolate",
         "--mailbox", str(repo / "loop"), "--worker-slice", "A",
         "--worktree-root", str(root), "--workspace", str(repo),
         "--prompt-file", str(task)],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "refusing isolated dispatch" in proc.stderr
    assert "--bridge-dir" not in proc.stderr  # paths of foreign bridges not echoed
    assert not root.exists()


def test_loop_refuses_isolation_with_contaminated_home_and_observe_combo(
    trioctl, repo, home, tmp_path
):
    (repo / "metrics").mkdir()
    for name in ("trio_loop.py", "trio-metrics.py", "trio-shadow.py", "trio-check.py"):
        (repo / "metrics" / name).write_text((REPO_ROOT / "metrics" / name).read_text())
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps(SESSION_MCP))
    import argparse
    args = argparse.Namespace(mailbox=str(repo / "loop"), isolate_workers=True,
                              observe_workers=False, worktree_root=None)
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        with pytest.raises(trioctl.TrioctlError, match="--isolate-workers refused"):
            trioctl.command_loop(args)
        (home / ".cursor" / "mcp.json").unlink()
        args.observe_workers = True
        with pytest.raises(trioctl.TrioctlError, match="cannot be combined"):
            trioctl.command_loop(args)
    finally:
        os.chdir(cwd)


# ------------------------------------------------------------------- M1


def test_verified_retired_ship_of_the_merge_is_accepted(trioctl, wt, repo, root, loop_core):
    rec = integrated_worker(wt, repo, root)
    merged = git(repo, "rev-parse", "HEAD")
    retire_ship(repo, merged)
    got = trioctl._ship_acceptance(repo / "loop", repo, loop_core)
    assert got == {"evaluated": merged, "iteration": 1, "attempt": "a-1"}
    (result,) = wt.cleanup(repo, acceptance_for=acceptance(trioctl, repo, loop_core))
    assert result["state"] == "removed" and result["accepted_by"]["evaluated"] == merged
    assert not Path(rec["path"]).exists()


def test_p01_evaluated_before_merge_with_later_commit_line_is_rejected(
    trioctl, wt, repo, root, loop_core
):
    pre = git(repo, "rev-parse", "HEAD")
    rec = integrated_worker(wt, repo, root)
    merged = git(repo, "rev-parse", "HEAD")
    # The Evaluator graded `pre`; a commit: line names the later merge.
    # (The product change after the pin also fails the loop contract.)
    retire_ship(repo, pre, commit_line=merged)
    (result,) = wt.cleanup(repo, acceptance_for=acceptance(trioctl, repo, loop_core))
    assert result["state"] == "integrated"
    assert Path(rec["path"]).is_dir()


def test_p02_handwritten_uncommitted_short_sha_ship_is_rejected(
    trioctl, wt, repo, root, loop_core
):
    rec = integrated_worker(wt, repo, root)
    short = git(repo, "rev-parse", "--short=7", "HEAD")
    (repo / "loop" / "VERDICT.md").write_text(f"VERDICT: SHIP\n\ncommit: {short}\n")
    assert "pending" in trioctl._ship_acceptance(repo / "loop", repo, loop_core)
    (result,) = wt.cleanup(repo, acceptance_for=acceptance(trioctl, repo, loop_core))
    assert result["state"] == "integrated"
    assert Path(rec["path"]).is_dir()


@pytest.mark.parametrize("mutate", [
    "verdict_dirty", "not_shipped", "short_state_sha", "stale_iteration",
    "late_merge", "no_retirement_commit", "attempt_mismatch",
])
def test_forged_stale_or_incomplete_acceptance_is_rejected(
    trioctl, wt, repo, root, loop_core, mutate
):
    rec = integrated_worker(wt, repo, root)
    merged = git(repo, "rev-parse", "HEAD")
    if mutate == "no_retirement_commit":
        (repo / "loop" / "VERDICT.md").write_text(
            f"VERDICT: SHIP\n\nattempt: a-1\nevaluated: {merged}\ncommit: {merged}\n")
        (repo / "loop" / "STATE.md").write_text(
            "schema: 1\niteration: 1\nmax_iterations: 3\nstatus: shipped\nmission: t\n"
            f"evaluator_attempt: a-1\nevaluated_sha: {merged}\n")
    else:
        retire_ship(
            repo, merged,
            state_status="needs_retirement" if mutate == "not_shipped" else "shipped",
            verdict_evaluated=merged,
            attempt="a-1",
        )
    state = repo / "loop" / "STATE.md"
    if mutate == "verdict_dirty":
        with open(repo / "loop" / "VERDICT.md", "a") as fh:
            fh.write("edited after retirement\n")
    elif mutate == "short_state_sha":
        state.write_text(state.read_text().replace(merged, merged[:12]))
    elif mutate == "stale_iteration":
        state.write_text(state.read_text().replace("iteration: 1", "iteration: 2"))
    elif mutate == "attempt_mismatch":
        state.write_text(state.read_text().replace("a-1", "a-2"))
    elif mutate == "late_merge":
        # A leftover builder merges after the graded pin (M4 late mutation).
        integrated_worker(wt, repo, root, slice_id="LATE")
    assert "pending" in trioctl._ship_acceptance(repo / "loop", repo, loop_core)
    results = {r["id"]: r for r in wt.cleanup(repo, acceptance_for=acceptance(trioctl, repo, loop_core))}
    assert results[rec["id"]]["state"] == "integrated"
    assert Path(rec["path"]).is_dir()


def test_cleanup_without_acceptance_source_never_deletes(wt, repo, root):
    rec = integrated_worker(wt, repo, root)
    assert wt.cleanup(repo)[0]["state"] == "integrated"
    assert Path(rec["path"]).is_dir()


# ------------------------------------------------------------------- M2


@pytest.mark.parametrize("returncode", [1, None])
def test_failed_or_interrupted_output_is_never_integrated(wt, repo, root, returncode):
    rec = wt.create(repo, slice_id="F", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "half.txt").write_text("partial\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), returncode)
    result = wt.integrate(repo, rec["id"])
    assert result["state"] == "retained"
    assert result["retained_reason"] in ("unverified_output", "worker_failed", "interrupted")
    assert not (repo / "half.txt").exists()
    subjects = git(repo, "log", "--format=%s").splitlines()
    assert not any(s.startswith("slice(F)") for s in subjects)
    # There is no override path at all (M2'): the API has no bypass.
    with pytest.raises(TypeError):
        wt.integrate(repo, rec["id"], override_unverified="human said so")


def test_empty_integration_clears_stale_reason(wt, repo, root):
    rec = wt.create(repo, slice_id="E", mailbox=repo / "loop", root=root)
    wt._retain(repo, wt.load_record(repo, rec["id"]), "aggregate_dirty", "earlier")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    done = wt.integrate(repo, rec["id"])
    assert done["state"] == "integrated" and "retained_reason" not in done


# ------------------------------------------------------------------- M3

FAKE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, time
    if sys.argv[1:2] == ["models"]:
        print("glm-5.2-max - GLM 5.2 Max"); sys.exit(0)
    sys.stdin.read()
    open(os.path.join(os.environ["FAKE_LOG"], "pid"), "w").write(str(os.getpid()))
    time.sleep(120)
    """)


def test_dispatcher_sigkill_takes_the_worker_group_down(wt, repo, root, home, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "cursor-agent").write_text(FAKE)
    (bindir / "cursor-agent").chmod(0o755)
    log = tmp_path / "log"
    log.mkdir()
    task = tmp_path / "t.md"
    task.write_text("go\n")
    env = dict(os.environ, **GIT_ENV, HOME=str(home), FAKE_LOG=str(log),
               PATH=f"{bindir}:/usr/bin:/bin")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "omnigent", "run", "builder",
         "--config", str(ROOT / "trioctl.example.toml"), "--isolate",
         "--mailbox", str(repo / "loop"), "--worker-slice", "K",
         "--worktree-root", str(root), "--workspace", str(repo),
         "--prompt-file", str(task)],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 30
    while not (log / "pid").exists() and time.time() < deadline:
        time.sleep(0.05)
    worker = int((log / "pid").read_text())
    proc.kill()  # SIGKILL: no handler, no finally
    proc.wait()
    deadline = time.time() + 15
    while os.path.exists(f"/proc/{worker}") and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(f"/proc/{worker}"), "orphaned worker survived its dispatcher"
    (rec,) = [r for _i, r in wt.list_records(repo)]
    deadline = time.time() + 15  # the watchdog exits right after its sweep
    while wt.identity_alive(rec.get("worker")) and time.time() < deadline:
        time.sleep(0.1)
    assert not wt.group_alive(rec["pgid"])
    assert wt.cleanup(repo)[0]["retained_reason"] == "interrupted"
    assert wt.integrate(repo, rec["id"])["state"] == "retained"


def test_watchdog_bounds_worker_lifetime(wt):
    start = time.time()
    proc = subprocess.run(
        wt.watchdog_command(["sleep", "60"], max_seconds=1.0),
        capture_output=True, timeout=30, start_new_session=True,
    )
    assert proc.returncode == 124
    assert time.time() - start < 15


def test_integrate_refuses_while_escaped_helper_uses_worktree(wt, repo, root, tmp_path):
    rec = wt.create(repo, slice_id="H", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "h.txt").write_text("h\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    helper = subprocess.Popen(["sleep", "60"], cwd=rec["path"], start_new_session=True)
    try:
        result = wt.integrate(repo, rec["id"])
        assert result["retained_reason"] == "active_session"
        assert not (repo / "h.txt").exists()
    finally:
        helper.kill()
        helper.wait()
    assert wt.integrate(repo, rec["id"])["state"] == "integrated"


def test_open_fd_holder_elsewhere_blocks_cleanup(wt, repo, root, tmp_path):
    rec = integrated_worker(wt, repo, root)
    held_file = Path(rec["path"]) / "A.txt"
    holder = subprocess.Popen(
        [sys.executable, "-c", f"f=open({str(held_file)!r}); import time; time.sleep(60)"],
        cwd=tmp_path,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not wt.processes_using(Path(rec["path"])):
            time.sleep(0.05)
        (result,) = wt.cleanup(repo, acceptance_for=lambda box: {"evaluated": git(repo, "rev-parse", "HEAD")})
        assert result["retained_reason"] == "active_session"
    finally:
        holder.kill()
        holder.wait()


# ------------------------------------------------------------------- M4


def test_integration_fence_blocks_merges_during_evaluation(wt, repo, root):
    rec = wt.create(repo, slice_id="G", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "g.txt").write_text("g\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    head = git(repo, "rev-parse", "HEAD")
    token = wt.acquire_fence(repo, reason="evaluator iteration 1", mailbox=repo / "loop")
    result = wt.integrate(repo, rec["id"])
    assert result["retained_reason"] == "integration_fenced"
    assert git(repo, "rev-parse", "HEAD") == head
    wt.release_fence(repo, token)
    assert wt.integrate(repo, rec["id"])["state"] == "integrated"


def test_dead_fence_holder_does_not_block_and_unreadable_fence_does(wt, repo, root):
    token = wt.acquire_fence(repo, reason="x")
    fence = wt.ledger_dir(repo) / "fences" / f"{token}.json"
    data = json.loads(fence.read_text())
    data["holder"] = {"pid": 2**22 + 11, "start": "0"}
    fence.write_text(json.dumps(data))
    assert wt.active_fence(repo) is None
    fence.write_text("{broken")
    assert wt.active_fence(repo) is not None


class _Client:
    def __init__(self):
        self.created = []

    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        self.created.append(workspace)
        return {"id": f"s{len(self.created)}"}

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
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "p\n")
    seen = []

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        seen.append({"role": role, "workspace": workspace,
                     "fence": trioctl.worker_worktrees.active_fence(repo)})
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner, seen


def test_root_reuse_ends_previous_session_and_fences_evaluation(
    trioctl, repo, root, monkeypatch
):
    pruned = []
    # r5 (N6): bookkeeping is dropped only for a session the prune deleted.
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda client, mailbox, **kw: pruned.append(kw["session_ids"])
                        or {"deleted": len(kw["session_ids"])})
    runner, seen = _runner(trioctl, repo, root, monkeypatch)
    assert runner.run("lead", 1, repo / "loop", None) == 0
    assert runner.run("evaluator", 1, repo / "loop", None) == 0
    # The Lead's session ended before root reuse; since r4 the finished root
    # evaluator's own session (s2) is ended right after it, before the loop
    # core grades acceptance, so its root Cursor config can be restored.
    assert pruned == [["s1"], ["s2"]]
    assert seen[0]["fence"] is None and seen[1]["fence"] is not None
    assert runner.run("lead", 2, repo / "loop", None) == 0
    assert pruned == [["s1"], ["s2"]]  # nothing left to end at the next Lead pass
    assert seen[2]["fence"] is None  # released at the next Lead pass


def test_held_previous_root_session_blocks_root_reuse(trioctl, repo, root, monkeypatch):
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda *a, **k: pytest.fail("held session must not be pruned"))
    runner, _seen = _runner(trioctl, repo, root, monkeypatch)
    runner.run("lead", 1, repo / "loop", None)
    runner.held_session_ids.append("s1")
    with pytest.raises(trioctl.TrioctlError, match="held session"):
        runner.run("evaluator", 1, repo / "loop", None)


def test_live_cursor_at_root_blocks_root_reuse(trioctl, repo, root, monkeypatch, tmp_path):
    bindir = tmp_path / "bin2"
    bindir.mkdir()
    fake = bindir / "cursor-agent"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    squatter = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "cursor-agent"], cwd=repo)
    try:
        runner, _seen = _runner(trioctl, repo, root, monkeypatch)
        runner.ROOT_RELEASE_WAIT = 0.2
        with pytest.raises(trioctl.TrioctlError, match="still run at the aggregate root"):
            runner.run("lead", 1, repo / "loop", None)
    finally:
        squatter.kill()
        squatter.wait()


# ------------------------------------------------------------------ L1/L3


def test_slice_eval_record_has_live_owner_during_dispatch(trioctl, wt, repo, root, monkeypatch):
    runner, seen = _runner(trioctl, repo, root, monkeypatch)
    sha = git(repo, "rev-parse", "HEAD")
    states = []

    def dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                 ctx, started, before_text, before_mtime, dispatch, workspace):
        (rec,) = [r for _i, r in wt.list_records(repo)]
        states.append((rec["state"], wt.identity_alive(rec.get("dispatcher"))))
        # A concurrent cleanup must not relabel a live dispatch as interrupted.
        states.append(wt.cleanup(repo)[0]["state"])
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", dispatch)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": sha}
    assert runner.run("evaluator", 1, repo / "loop", ctx) == 0
    assert states == [("running", True), "running"]


def test_builder_note_warns_about_stale_mailbox(trioctl):
    note = trioctl._ISOLATED_BUILDER_NOTE.format(
        path="/w", branch="b", base="c", repo="/r", slice="s", mailbox="/r/loop")
    assert "stale" in note and "/r/loop" in note and "never integrated" in note


# -------------------------------------------- ignored .cursor/ owned config


def test_ignored_owned_cursor_config_is_disposable_but_extra_content_is_not(wt, repo, root):
    (repo / ".gitignore").write_text("__pycache__/\n.cursor/\n")
    git(repo, "commit", "-qam", "ignore cursor")
    sha = git(repo, "rev-parse", "HEAD")
    for extra in (False, True):
        rec = wt.create(repo, slice_id=f"eval-{extra}", mailbox=repo / "loop",
                        root=root, role="evaluator", detach_at=sha)
        path = Path(rec["path"])
        (path / ".cursor").mkdir()
        mcp = dict(SESSION_MCP)
        (path / ".cursor" / "mcp.json").write_text(json.dumps(mcp))
        (path / ".cursor" / "hooks.json").write_text(json.dumps(SESSION_HOOKS))
        if extra:
            (path / ".cursor" / "rules.md").write_text("user rules\n")
        wt.mark_finished(repo, rec["id"])
    results = {r["slice"]: r for r in wt.cleanup(repo)}
    assert results["eval-False"]["state"] == "removed"
    assert results["eval-True"]["retained_reason"] == "ignored_content"

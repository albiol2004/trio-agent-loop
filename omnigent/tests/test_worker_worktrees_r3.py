"""Regressions for the independent ITERATE on c440539 (N1, A1, M2', M4', H1').

Offline. N1 uses real threads with a deterministic barrier; A1 drives the
real open-loop driver (metrics/trio_loop.run_loop) on a real git repo.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import threading
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
    return _load("worker_worktrees_r3", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r3", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def loop_core():
    return _load("trio_loop_r3", REPO_ROOT / "metrics" / "trio_loop.py")


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n.cursor/\n")
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


# ------------------------------------------------------------------- N1


class _BlockingClient:
    """Sessions s1, s2, ... ; the wait for any session in ``hold`` blocks."""

    def __init__(self):
        self.created = []
        self.hold: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        with self.lock:
            sid = f"s{len(self.created) + 1}"
            self.created.append((sid, title, workspace))
        self.started.setdefault(sid, threading.Event()).set()
        return {"id": sid}

    def wait_session(self, session_id, timeout=None, interval=None):
        gate = self.hold.get(session_id)
        if gate is not None:
            assert gate.wait(30), "test gate never released"
        return {"status": "idle"}

    def get_items(self, session_id):
        return []


def _runner(trioctl, repo, root, monkeypatch, client, *, root_free: bool = False):
    kwargs: dict = {}
    if root_free:
        # r16b: per-turn session ending (`_end_finished_session`) and the
        # Lead/repair finished-fence release (`_root_free_prepare`) are
        # both gated on a root-free runner; a real fork is not needed here
        # (neither touches the ledger for a plain "lead"/non-isolated
        # "evaluator" dispatch), just this informational view of it.
        kwargs["root_free"] = {
            "home": str(repo), "lead": str(repo), "branch": "trio/loop",
            "target": "main", "target_base": git(repo, "rev-parse", "HEAD"),
            "mailbox_rel": "loop", "slug": "loop",
            "repo_targets": {}, "repo_aggregates": {},
        }
    runner = trioctl.OmnigentRunner(
        repo=repo, broker_client=client, config={}, interval=0,
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
        **kwargs,
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: role)
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "p\n")

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner


def test_n1_running_slice_eval_is_never_pruned_by_next_lead_pass(
    trioctl, wt, repo, root, monkeypatch
):
    """r16b: the r15.x "root reuse" pruning this test pinned (a later
    dispatch's cleanup retrying a still-unconfirmed earlier root session)
    is gone -- `_end_finished_session` ends a finished dispatch's OWN
    session synchronously at its own turn end (root-free runner only), so
    there is no shared state a later pass could touch. What survives is
    the property this test cares about: a concurrent slice-eval's session
    is never ended by this per-turn mechanism (it is released instead,
    with the eval worktree, once the eval itself finishes)."""
    pruned: list[list[str]] = []
    monkeypatch.setattr(
        trioctl, "_prune_broker_sessions",
        lambda client, mailbox, **kw: pruned.append(list(kw["session_ids"])) or {},
    )
    client = _BlockingClient()
    runner = _runner(trioctl, repo, root, monkeypatch, client, root_free=True)
    mailbox = repo / "loop"
    sha = git(repo, "rev-parse", "HEAD")

    assert runner.run("lead", 1, mailbox, None) == 0            # s1, finished
    client.hold["s2"] = threading.Event()
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": sha}
    results = {}
    evaluator = threading.Thread(
        target=lambda: results.setdefault("eval", runner.run("evaluator", 1, mailbox, ctx)))
    evaluator.start()
    assert client.started.setdefault("s2", threading.Event()).wait(10)  # eval s2 in flight

    # Next Lead pass while s2 is alive: its own session (s3) ends at its own
    # turn end; s2 (running, other role, other workspace) is never touched.
    assert runner.run("lead", 2, mailbox, None) == 0            # s3, ended at once
    assert pruned == [["s1"], ["s3"]]
    assert all("s2" not in call for call in pruned)
    client.hold["s2"].set()
    evaluator.join(10)
    assert results["eval"] == 0
    # The eval worktree records only its own session.
    (rec,) = [r for _i, r in wt.list_records(repo) if r.get("kind") == "eval"]
    assert rec["session_ids"] == ["s2"]
    # s2 (slice-eval) never goes through this per-turn ending, even after
    # the eval itself has finished.
    assert runner.run("lead", 3, mailbox, None) == 0
    assert pruned == [["s1"], ["s3"], ["s4"]]
    assert all("s2" not in call for call in pruned)


def test_n1_failure_path_holds_its_own_session_not_a_concurrent_one(
    trioctl, repo, root, monkeypatch
):
    client = _BlockingClient()
    runner = _runner(trioctl, repo, root, monkeypatch, client)
    held = []
    monkeypatch.setattr(runner, "_hold_dispatch",
                        lambda mailbox, sid, *a, **k: held.append(sid) or (_ for _ in ()).throw(
                            trioctl.TrioctlError("held")))
    other = threading.Event()

    def run_dispatch(client_, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        # Real failure path: create, then (while another role creates a
        # session concurrently) the wait fails.
        dispatch["session_id"] = client_.create_session(agent_id, model, prompt, title)["id"]
        runner.created_session_ids.append(dispatch["session_id"])
        other.wait(10)
        runner.created_session_ids.append("s-concurrent")
        session_id = dispatch.get("session_id")
        return runner._hold_dispatch(mailbox, session_id, role, iteration, title, ctx,
                                     trioctl.TrioctlError("x"), "role_completion_uncertain")

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    other.set()
    with pytest.raises(trioctl.TrioctlError):
        runner.run("lead", 1, repo / "loop", None)
    assert held == ["s1"]
    # The production failure path reads dispatch["session_id"], never the shared list.
    source = SCRIPT.read_text()
    assert "self.created_session_ids[-1]" not in source
    assert "created_session_ids[created_before:]" not in source


# ------------------------------------------------------------------- M4'


def test_m4_overlapping_fence_holders_are_independent(wt, repo, root):
    rec = wt.create(repo, slice_id="G", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "g.txt").write_text("g\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    a = wt.acquire_fence(repo, reason="A evaluating")
    b = wt.acquire_fence(repo, reason="B evaluating")
    assert a != b and len(wt.active_fences(repo)) == 2
    assert wt.release_fence(repo, b) is True
    # A still holds: integration stays fenced (B's release did not clear A).
    assert wt.integrate(repo, rec["id"])["retained_reason"] == "integration_fenced"
    assert wt.release_fence(repo, b) is False           # token-bound, idempotent
    assert wt.release_fence(repo, "../escape") is False
    assert wt.release_fence(repo, a) is True
    assert wt.integrate(repo, rec["id"])["state"] == "integrated"


def test_m4_crashed_holder_is_recovered_unreadable_stays_held(wt, repo):
    token = wt.acquire_fence(repo, reason="x")
    path = wt.ledger_dir(repo) / "fences" / f"{token}.json"
    data = json.loads(path.read_text())
    data["holder"] = {"pid": 2**22 + 13, "start": "0"}
    path.write_text(json.dumps(data))
    assert wt.active_fences(repo) == []
    (path.parent / "junk.json").write_text("{")
    assert wt.active_fence(repo) is not None


def test_m4_lead_releases_only_finished_evaluator_fences(trioctl, wt, repo, root, monkeypatch):
    # r16b: a non-isolated evaluator's fence is acquired by
    # `_root_free_prepare`, gated on a root-free runner (see `_runner`).
    monkeypatch.setattr(trioctl, "_prune_broker_sessions", lambda *a, **k: {})
    client = _BlockingClient()
    runner = _runner(trioctl, repo, root, monkeypatch, client, root_free=True)
    mailbox = repo / "loop"
    client.hold["s1"] = threading.Event()
    t = threading.Thread(target=lambda: runner.run("evaluator", 1, mailbox, None))
    t.start()
    assert client.started.setdefault("s1", threading.Event()).wait(10)
    assert len(wt.active_fences(repo)) == 1
    # A concurrent release request must not drop the RUNNING evaluator's fence.
    runner._release_fences(finished_only=True)
    assert len(wt.active_fences(repo)) == 1
    client.hold["s1"].set()
    t.join(10)
    runner._release_fences(finished_only=True)
    assert wt.active_fences(repo) == []


# ------------------------------------------------------------------- M2'


def test_m2_no_override_on_cli(trioctl):
    parser_help = subprocess.run(
        ["python3", str(SCRIPT), "omnigent", "worktrees", "integrate", "--help"],
        capture_output=True, text=True,
    ).stdout
    assert "--accept-unverified-output" not in parser_help
    assert "override" not in parser_help.lower()


# ------------------------------------------------------------------- H1'


SESSION = {"command": "/usr/bin/python3", "args": [
    "-I", "-m", "omnigent.harnesses.claude_native.bridge", "serve-mcp", "--bridge-dir", "/b/x"]}


@pytest.mark.parametrize("setup", [
    "managed_team_hook", "plugin_mcp_renamed", "plugin_hooks_file",
    "plugin_manifest_unresolved", "env_carried_bridge", "url_bridge",
    "unreadable_plugin_manifest",
])
def test_h1_additional_inherited_sources(wt, home, setup):
    c = home / ".cursor"
    c.mkdir()
    if setup == "managed_team_hook":
        d = c / "managed" / "active-team-hooks"
        d.mkdir(parents=True)
        (d / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [
            {"command": "python -m omnigent.harnesses.cursor_native.usage record-usage --bridge-dir /b"}]}}))
    elif setup == "plugin_mcp_renamed":
        d = c / "plugins" / "cache" / "p" / "v1"
        d.mkdir(parents=True)
        (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"helper": SESSION}}))
    elif setup == "plugin_hooks_file":
        d = c / "plugins" / "cache" / "p" / "v1" / "hooks"
        d.mkdir(parents=True)
        (d / "hooks.json").write_text(json.dumps({"hooks": {"stop": [{"command": "x record-usage --bridge-dir y"}]}}))
    elif setup == "plugin_manifest_unresolved":
        d = c / "plugins" / "cache" / "p" / "v1" / ".cursor-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"name": "p", "hooks": "./missing/hooks.json"}))
    elif setup == "env_carried_bridge":
        (c / "mcp.json").write_text(json.dumps({"mcpServers": {"wrapped": {
            "command": "/opt/wrapper.sh", "env": {"HARNESS_CURSOR_NATIVE_BRIDGE_DIR": "/tmp/omnigent-1/cursor-native/abc"}}}}))
    elif setup == "url_bridge":
        (c / "mcp.json").write_text(json.dumps({"mcpServers": {"remote": {
            "url": "http://127.0.0.1:9/omnigent/mcp"}}}))
    elif setup == "unreadable_plugin_manifest":
        d = c / "plugins" / "cache" / "p" / "v1"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text("{not json")
    assert wt.inherited_cursor_problems(home=home, system_hooks=())


def test_h1_benign_plugins_and_servers_pass(wt, home):
    c = home / ".cursor"
    d = c / "plugins" / "cache" / "p" / "v1"
    d.mkdir(parents=True)
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {
        "command": "npx", "args": ["@playwright/mcp@latest"]}}}))
    (d / "plugin.json").write_text(json.dumps({"name": "p", "description": "x"}))
    (c / "mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    assert wt.inherited_cursor_problems(home=home, system_hooks=()) == []


# ------------------------------------------------------------------- A1


GIT_ISOLATED = dict(GIT_ENV, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def _open_loop_repo(repo: Path) -> Path:
    mailbox = repo / "loop"
    (mailbox / "GOAL.md").write_text("# Goal\nship the demo slice\n")
    (mailbox / "STATE.md").write_text(
        "schema: 1\niteration: 0\nmax_iterations: 5\nstatus: ready\nphase: idle\n"
        "mission: ship the demo slice\n")
    (mailbox / "PLAN.md").write_text(
        "```yaml\nslices:\n  - id: demo\n    writes: [demo.txt]\n    reads: []\n"
        "    accepts: [\"demo exists\"]\n```\n")
    (mailbox / "REPORT.md").write_text("")
    (mailbox / "VERDICT.md").write_text("")
    (mailbox / "LOG.md").write_text("# Trio loop log\n")
    (mailbox / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "loop: mailbox")
    return mailbox


class _OpenLoopRunner:
    """Lead retires `demo` at HEAD; evaluators write real, bound verdicts."""

    def __init__(self, repo, mailbox, *, integration=("SHIP",), bind=True, late=None):
        self.repo, self.mailbox = repo, mailbox
        self.integration = list(integration)
        self.bind = bind
        self.late = late
        self.contexts = []

    def _commit_mailbox(self, message):
        git(self.repo, "add", "loop")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)

    def run(self, role, iteration, mailbox, context=None):
        mailbox = Path(mailbox)
        if role == "lead":
            sha = git(self.repo, "rev-parse", "HEAD")
            (mailbox / "QUEUE.md").write_text(
                "```yaml\nretired:\n"
                f"  - slice: demo\n    sha: {sha}\n    at: 2026-01-01T00:00:00Z\n"
                "```\n\n```yaml\nfaults:\n```\n")
            with open(mailbox / "LOG.md", "a") as fh:
                fh.write(f"- iter {iteration} | lead | retired demo\n")
            self._commit_mailbox(f"loop: lead pass {iteration}")
            return 0
        self.contexts.append(dict(context or {}))
        if context["kind"] == "slice-eval":
            with open(mailbox / "VERDICT.md", "a") as fh:
                fh.write(f"## slice demo @{context['sha']} — SHIP\n")
            return 0
        verdict = self.integration.pop(0)
        if self.late:
            self.late()
            self.late = None
        body = [f"VERDICT: {verdict}", "", f"iteration: {context.get('iteration')}"]
        if self.bind:
            body += [f"attempt: {context['evaluator_attempt']}",
                     f"evaluated: {context['pinned_sha']}"]
        body.append(f"commit: {context.get('pinned_sha') or git(self.repo, 'rev-parse', 'HEAD')}")
        old = (mailbox / "VERDICT.md").read_text()
        (mailbox / "VERDICT.md").write_text("\n".join(body) + "\n\n" + old)
        if verdict == "SHIP":
            with open(mailbox / "LOG.md", "a") as fh:
                fh.write(f"- iter {context.get('iteration')} | evaluator | VERDICT: SHIP\n")
            self._commit_mailbox(f"loop: iteration {context.get('iteration')} — SHIP")
        return 0


def _integrated_demo_worker(wt, repo, root):
    rec = wt.create(repo, slice_id="demo", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "demo.txt").write_text("demo\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    return wt.integrate(repo, rec["id"], summary="add demo")


def _run(loop_core, mailbox, repo, runner):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "code", loop_core.run_loop(mailbox, 5, runner, repo=repo, mode="open-loop",
                                   poll_seconds=0.01)), daemon=True)
    t.start()
    t.join(60)
    assert "code" in result, "open-loop driver did not finish"
    return result["code"]


def test_a1_open_loop_ship_binds_pin_and_attempt_and_drives_cleanup(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    for k, v in GIT_ISOLATED.items():
        monkeypatch.setenv(k, v)
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    runner = _OpenLoopRunner(repo, mailbox)
    assert _run(loop_core, mailbox, repo, runner) == 0
    (integration,) = [c for c in runner.contexts if c["kind"] == "integration-eval"]
    state = loop_core._read_state(mailbox / "STATE.md")
    assert state["status"] == "shipped"
    assert state["evaluated_sha"] == integration["pinned_sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", integration["pinned_sha"])
    assert state["evaluator_attempt"] == integration["evaluator_attempt"]
    got = trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert got["evaluated"] == integration["pinned_sha"]
    (done,) = wt.cleanup(repo, acceptance_for=lambda box: trioctl._ship_acceptance(box, repo, loop_core))
    assert done["state"] == "removed" and done["accepted_by"]["evaluated"] == integration["pinned_sha"]
    assert not Path(rec["path"]).exists()


def test_a1_unbound_open_loop_ship_does_not_finalize_and_reports_pending(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    for k, v in GIT_ISOLATED.items():
        monkeypatch.setenv(k, v)
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    runner = _OpenLoopRunner(repo, mailbox, bind=False)
    assert _run(loop_core, mailbox, repo, runner) == 6       # needs_retirement
    got = trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert "pending" in got and "shipped" in got["pending"]
    (res,) = wt.cleanup(repo, acceptance_for=lambda box: trioctl._ship_acceptance(box, repo, loop_core))
    assert res["state"] == "integrated" and "shipped" in res["acceptance_pending"]
    assert "acceptance pending" in wt.summarize(res)
    assert Path(rec["path"]).is_dir()


def test_a1_late_merge_after_pin_voids_open_loop_ship(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    for k, v in GIT_ISOLATED.items():
        monkeypatch.setenv(k, v)
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)

    def late():
        (repo / "late.txt").write_text("late\n")
        git(repo, "add", "late.txt")
        git(repo, "commit", "-q", "-m", "slice(demo): late product change after the pin")

    runner = _OpenLoopRunner(repo, mailbox, late=late)
    assert _run(loop_core, mailbox, repo, runner) != 0
    assert "pending" in trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert wt.cleanup(repo, acceptance_for=lambda box: trioctl._ship_acceptance(box, repo, loop_core))[0]["state"] == "integrated"
    assert Path(rec["path"]).is_dir()


def test_a1_iterate_clears_pin_and_next_integration_rebinds(loop_core, repo, monkeypatch):
    for k, v in GIT_ISOLATED.items():
        monkeypatch.setenv(k, v)
    mailbox = _open_loop_repo(repo)
    state = mailbox / "STATE.md"
    first = loop_core._open_loop_integration_context(mailbox, repo, 1, state)
    again = loop_core._open_loop_integration_context(mailbox, repo, 1, state)
    assert again["evaluator_attempt"] == first["evaluator_attempt"]   # resume, tree intact
    (repo / "fix.txt").write_text("fix\n")
    git(repo, "add", "fix.txt")
    git(repo, "commit", "-q", "-m", "slice(demo): fix")
    fresh = loop_core._open_loop_integration_context(mailbox, repo, 2, state)
    assert fresh["evaluator_attempt"] != first["evaluator_attempt"]
    assert fresh["pinned_sha"] == git(repo, "rev-parse", "HEAD") != first["pinned_sha"]


def test_a1_integration_prompt_carries_exact_binding(trioctl):
    block = trioctl.OmnigentRunner._open_loop_context_block({
        "mode": "open-loop", "kind": "integration-eval", "sha": "a" * 40,
        "pinned_sha": "a" * 40, "evaluator_attempt": "att-1", "iteration": 3})
    assert f"sha={'a' * 40}" in block
    assert "`attempt: att-1`" in block and f"`evaluated: {'a' * 40}`" in block
    assert "`iteration: 3`" in block and "loop: iteration 3 — SHIP" in block


# --------------------------------------------------- runner stop on prune


def test_prune_archives_before_delete_and_waits_for_runner_stop(trioctl, monkeypatch):
    calls = []

    class C:
        def __init__(self):
            self.online = iter([True, True, False])

        def archive_session(self, sid):
            calls.append(("archive", sid))

        def get_session(self, sid):
            return {"runner_online": next(self.online)}

    monkeypatch.setattr(trioctl.time, "sleep", lambda s: None)
    assert trioctl._stop_session_runner(C(), "s1") is True
    assert calls == [("archive", "s1")]
    # Old/fake clients: no archive method -> unchanged behaviour.
    assert trioctl._stop_session_runner(object(), "s1") is None

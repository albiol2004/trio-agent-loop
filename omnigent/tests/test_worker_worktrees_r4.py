"""Regressions for the live qualification of 3e7dfee (runs A and B).

B: Omnigent's per-session root ``.cursor/{mcp,hooks}.json`` stayed untracked
after the integration evaluator, so a bound SHIP could never be accepted in
a repo that does not ignore ``.cursor/``. A: a stale vendored loop core ran
silently and produced an unbound SHIP.

Offline, real git, real loop core. No ``.cursor`` ignore rule anywhere.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
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
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
PY = "/usr/bin/python3"


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
    return _load("worker_worktrees_r4", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r4", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def loop_core():
    return _load("trio_loop_r4", REPO_ROOT / "metrics" / "trio_loop.py")


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


# ------------------------------------------- faithful Omnigent 2a84483a writer


def _bridge(sid: str) -> str:
    return f"/tmp/omnigent-1000/cursor-native/{hashlib.sha256(sid.encode()).hexdigest()[:32]}"


def omnigent_launch(workspace: Path, sid: str) -> None:
    """What cursor-native writes into a workspace at session launch.

    Mirrors omnigent.harnesses.cursor_native.bridge.write_mcp_config /
    write_hooks_config (merge, replace ``omnigent``, drop stale usage hooks,
    ``json.dumps(indent=2, sort_keys=True)``). No credential is written here:
    the bridge token lives in the bridge dir.
    """
    cursor = workspace / ".cursor"
    cursor.mkdir(exist_ok=True)
    mcp = cursor / "mcp.json"
    try:
        existing = json.loads(mcp.read_text())
    except (OSError, ValueError):
        existing = None
    existing = existing if isinstance(existing, dict) else {}
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    servers["omnigent"] = {
        "command": PY,
        "args": ["-I", "-m", "omnigent.harnesses.claude_native.bridge", "serve-mcp",
                 "--bridge-dir", _bridge(sid)],
        "autoApprove": ["*"],
        "env": {"TMPDIR": "/tmp"},
    }
    mcp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    hooks_path = cursor / "hooks.json"
    try:
        data = json.loads(hooks_path.read_text())
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    data["hooks"] = hooks
    data.setdefault("version", 1)
    command = " ".join(shlex.quote(p) for p in (
        PY, "-I", "-m", "omnigent.harnesses.cursor_native.usage", "record-usage",
        "--bridge-dir", _bridge(sid)))
    stop = hooks.get("stop") if isinstance(hooks.get("stop"), list) else []
    hooks["stop"] = [e for e in stop if "omnigent.harnesses.cursor_native.usage"
                     not in str(e.get("command", ""))] + [{"command": command}]
    hooks_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


# ------------------------------------ end to end: real open-loop driver


GIT_ISOLATED = dict(GIT_ENV)


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


class _LaunchingClient:
    """Broker stand-in: creating a session makes Omnigent write its workspace config."""

    def __init__(self):
        self.created = []
        self.lock = threading.Lock()

    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        with self.lock:
            sid = f"sess{len(self.created) + 1:04d}"
            self.created.append((sid, title, workspace))
        omnigent_launch(Path(workspace), sid)
        return {"id": sid}

    def wait_session(self, session_id, timeout=None, interval=None):
        return {"status": "idle"}

    def get_items(self, session_id):
        return []


def _e2e_runner(trioctl, repo, root, monkeypatch, client, *, end_sessions=True):
    runner = trioctl.OmnigentRunner(
        repo=repo, broker_client=client, config={}, interval=0,
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
        end_sessions=end_sessions,
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: role)
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "p\n")
    ended = []
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda client, mailbox, session_ids=None, **kw:
                        ended.extend(session_ids) or {"archived": len(session_ids),
                                                      "deleted": len(session_ids)})

    def commit_mailbox(message):
        git(repo, "add", "loop")
        git(repo, "commit", "-q", "--allow-empty", "-m", message)

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        if role == "lead":
            sha = git(repo, "rev-parse", "HEAD")
            (mailbox / "QUEUE.md").write_text(
                "```yaml\nretired:\n"
                f"  - slice: demo\n    sha: {sha}\n    at: 2026-01-01T00:00:00Z\n"
                "```\n\n```yaml\nfaults:\n```\n")
            with open(mailbox / "LOG.md", "a") as fh:
                fh.write(f"- iter {iteration} | lead | retired demo\n")
            commit_mailbox(f"loop: lead pass {iteration}")
            return 0
        if ctx["kind"] == "slice-eval":
            with open(mailbox / "VERDICT.md", "a") as fh:
                fh.write(f"## slice demo @{ctx['sha']} — SHIP\n")
            return 0
        body = ["VERDICT: SHIP", "", f"iteration: {ctx.get('iteration')}",
                f"attempt: {ctx['evaluator_attempt']}", f"evaluated: {ctx['pinned_sha']}",
                f"commit: {ctx['pinned_sha']}"]
        old = (mailbox / "VERDICT.md").read_text()
        (mailbox / "VERDICT.md").write_text("\n".join(body) + "\n\n" + old)
        with open(mailbox / "LOG.md", "a") as fh:
            fh.write(f"- iter {ctx.get('iteration')} | evaluator | VERDICT: SHIP\n")
        commit_mailbox(f"loop: iteration {ctx.get('iteration')} — SHIP")
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner, ended


def _integrated_demo_worker(wt, repo, root):
    rec = wt.create(repo, slice_id="demo", mailbox=repo / "loop", root=root)
    (Path(rec["path"]) / "demo.txt").write_text("demo\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    return wt.integrate(repo, rec["id"], summary="add demo")


def _drive(loop_core, mailbox, repo, runner):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "code", loop_core.run_loop(mailbox, 5, runner, repo=repo, mode="open-loop",
                                   poll_seconds=0.01)), daemon=True)
    t.start()
    t.join(90)
    assert "code" in result, "open-loop driver did not finish"
    return result["code"]


def test_e2e_root_config_does_not_block_a_real_bound_ship(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    """r16b: with no `root_free` set, nothing ends a finished session or
    restores anything any more -- the residue is simply left in place. The
    loop core alone (owned_residue_check) keeps it from blocking the SHIP."""
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    client = _LaunchingClient()
    runner, ended = _e2e_runner(trioctl, repo, root, monkeypatch, client)
    assert _drive(loop_core, mailbox, repo, runner) == 0
    state = loop_core._read_state(mailbox / "STATE.md")
    assert state["status"] == "shipped"
    assert ended == []  # no root_free: no auto-ended session
    assert (repo / ".cursor" / "mcp.json").is_file()  # left behind, not product
    # Slice-eval config lived in its own eval worktree, never the root.
    assert any(Path(ws) != repo for _s, _t, ws in client.created)
    got = trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert got["evaluated"] == state["evaluated_sha"]
    removed = [r for r in wt.cleanup(repo, acceptance_for=lambda box:
               trioctl._ship_acceptance(box, repo, loop_core)) if r["id"] == rec["id"]]
    assert removed[0]["state"] == "removed"
    assert not Path(rec["path"]).exists()


def test_e2e_keep_sessions_leaves_config_and_never_forges_acceptance(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    runner, ended = _e2e_runner(trioctl, repo, root, monkeypatch, _LaunchingClient(),
                                end_sessions=False)
    # r11: the (never-ended, r16b) sessions' exact generated config stays in
    # place, but it is Omnigent residue, not product: the bound SHIP is accepted.
    assert _drive(loop_core, mailbox, repo, runner) == 0
    assert ended == []
    assert (repo / ".cursor" / "mcp.json").exists()
    state = loop_core._read_state(mailbox / "STATE.md")
    got = trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert got["evaluated"] == state["evaluated_sha"]
    # A user edit to that config makes it product again: acceptance pends.
    data = json.loads((repo / ".cursor" / "mcp.json").read_text())
    data["mcpServers"]["mine"] = {"command": "my-server"}
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(data))
    assert "pending" in trioctl._ship_acceptance(mailbox, repo, loop_core)
    (res,) = [r for r in wt.cleanup(repo, acceptance_for=lambda box:
              trioctl._ship_acceptance(box, repo, loop_core)) if r["id"] == rec["id"]]
    assert res["state"] == "integrated" and Path(rec["path"]).is_dir()


def test_e2e_real_untracked_product_file_still_blocks(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    (repo / "notes.py").write_text("x = 1\n")                   # real product residue
    runner, _ended = _e2e_runner(trioctl, repo, root, monkeypatch, _LaunchingClient())
    assert _drive(loop_core, mailbox, repo, runner) == 6
    assert (repo / ".cursor" / "mcp.json").is_file()  # owned config: harmless, left in place
    assert "pending" in trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert Path(rec["path"]).is_dir()


# ------------------------------------------------ loop core compatibility


def _vendor(repo: Path, source: Path) -> Path:
    (repo / "metrics").mkdir(exist_ok=True)
    for name in ("trio_loop.py", "trio-metrics.py", "trio-shadow.py", "trio-check.py"):
        shutil.copyfile(source / name, repo / "metrics" / name)
    return repo / "metrics" / "trio_loop.py"


def test_stale_vendored_core_is_refused_with_actionable_message(trioctl, repo):
    core = _vendor(repo, REPO_ROOT / "metrics")
    core.write_text(re.sub(r"^LOOP_CORE_API = \d+\n", "", core.read_text(), flags=re.M))
    before = hashlib.sha256(core.read_bytes()).hexdigest()
    with pytest.raises(trioctl.TrioctlError) as exc:
        trioctl._load_trio_loop(repo)
    message = str(exc.value)
    assert "LOOP_CORE_API 1" in message and "requires 2" in message
    assert "Refresh the repository's metrics/" in message and "Nothing was changed" in message
    assert hashlib.sha256(core.read_bytes()).hexdigest() == before   # never rewritten
    got = trioctl._ship_acceptance(repo / "loop", repo)
    assert got["pending"].startswith("loop core not usable: incompatible loop core")


def test_newer_or_non_literal_api_is_also_refused(trioctl, repo):
    core = _vendor(repo, REPO_ROOT / "metrics")
    text = core.read_text()
    core.write_text(text.replace("LOOP_CORE_API = 2", "LOOP_CORE_API = 3"))
    with pytest.raises(trioctl.TrioctlError, match="LOOP_CORE_API 3"):
        trioctl._load_trio_loop(repo)
    core.write_text(text.replace("LOOP_CORE_API = 2", "LOOP_CORE_API = int('2')"))
    with pytest.raises(trioctl.TrioctlError, match="LOOP_CORE_API 0"):   # r5: ambiguous
        trioctl._load_trio_loop(repo)


def test_current_vendored_core_loads_and_drives(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    module = trioctl._load_trio_loop(repo)
    assert module.LOOP_CORE_API == trioctl.REQUIRED_LOOP_CORE_API
    assert callable(module.run_loop)

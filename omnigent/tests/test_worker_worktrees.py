"""Task-owned worker worktrees: isolation, integration and verified cleanup.

Offline only. The concurrency test runs two real `trioctl omnigent run
builder --isolate` OS processes against a stub `cursor-agent`; it proves
per-process workspace binding and config-root separation, NOT real Cursor
behaviour, output quality or speed-up (that is the live canary's job).
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


@pytest.fixture()
def wt(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return _load("worker_worktrees_under_test", MODULE)


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


LEAD_MCP = {
    "mcpServers": {
        "omnigent": {
            "command": "/usr/bin/python3",
            "args": ["-I", "-m", "omnigent.harnesses.claude_native.bridge",
                     "serve-mcp", "--bridge-dir", "/bridges/lead-session"],
        }
    }
}
LEAD_HOOKS = {
    "version": 1,
    "hooks": {"stop": [{"command": (
        "/usr/bin/python3 -I -m omnigent.harnesses.cursor_native.usage "
        "record-usage --bridge-dir /bridges/lead-session")}]},
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("*.log\n__pycache__/\n")
    (repo / "shared.txt").write_text("one\ntwo\nthree\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "worktrees"


def write_owned_cursor(where: Path, bridge: str = "/bridges/lead-session") -> None:
    (where / ".cursor").mkdir(exist_ok=True)
    mcp = json.loads(json.dumps(LEAD_MCP))
    mcp["mcpServers"]["omnigent"]["args"][-1] = bridge
    hooks = json.loads(json.dumps(LEAD_HOOKS))
    hooks["hooks"]["stop"][0]["command"] = hooks["hooks"]["stop"][0]["command"].replace(
        "/bridges/lead-session", bridge)
    (where / ".cursor" / "mcp.json").write_text(json.dumps(mcp))
    (where / ".cursor" / "hooks.json").write_text(json.dumps(hooks))


def ship(repo: Path, sha: str) -> None:
    (repo / "loop" / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n\nAll accepts pass.\n\ncommit: {sha}\n"
    )


def make_worker(wt, repo, root, slice_id="A", files=None):
    record = wt.create(repo, slice_id=slice_id, mailbox=repo / "loop", root=root)
    path = Path(record["path"])
    for name, text in (files or {f"{slice_id.lower()}.txt": f"{slice_id}\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    wt.mark_exited(repo, wt.load_record(repo, record["id"]), 0)
    return record


# ------------------------------------------------ overlapping real processes

FAKE_CURSOR = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys, time
    args = sys.argv[1:]
    if args[:1] == ["models"]:
        print("glm-5.2-max - GLM 5.2 Max")
        sys.exit(0)
    prompt = sys.stdin.read()
    ws = args[args.index("--workspace") + 1]
    cwd = os.getcwd()
    seen = {}
    for name in ("mcp.json", "hooks.json"):
        p = os.path.join(cwd, ".cursor", name)
        seen[name] = open(p).read() if os.path.exists(p) else None
    start = time.time()
    slice_id = prompt.split("SLICE=")[1].split()[0]
    with open(os.path.join(os.environ["FAKE_LOG"], f"{slice_id}.started"), "w") as fh:
        fh.write(str(os.getpid()))
    time.sleep(float(os.environ.get("FAKE_SLEEP", "1.5")))
    with open(os.path.join(cwd, f"{slice_id}.txt"), "w") as fh:
        fh.write(slice_id + "\\n")
    if "LINGER" in prompt:
        import subprocess
        linger = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # same group
        with open(os.path.join(os.environ["FAKE_LOG"], f"{slice_id}.linger"), "w") as fh:
            fh.write(str(linger.pid))
    if "RESIDUE" in prompt:
        os.makedirs(os.path.join(cwd, ".cursor"), exist_ok=True)
        json.dump(%(mcp)s, open(os.path.join(cwd, ".cursor", "mcp.json"), "w"))
    log = os.path.join(os.environ["FAKE_LOG"], f"{slice_id}.json")
    json.dump({"pid": os.getpid(), "pgid": os.getpgid(0), "cwd": cwd,
               "workspace": ws, "seen": seen, "start": start,
               "end": time.time()}, open(log, "w"))
    print(f"built {slice_id}")
    """
) % {"mcp": repr(LEAD_MCP)}


@pytest.fixture()
def fake_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "cursor-agent"
    fake.write_text(FAKE_CURSOR)
    fake.chmod(0o755)
    log = tmp_path / "fake-log"
    log.mkdir()
    config = tmp_path / "omnigent.toml"
    config.write_text((ROOT / "trioctl.example.toml").read_text())
    env = dict(os.environ, **GIT_ENV)
    env.update({
        "PATH": f"{bindir}:/usr/bin:/bin",
        "FAKE_LOG": str(log),
        "TRIO_TEST_CONFIG": str(config),
    })
    return env


def launch(env, repo, root, slice_id, extra=""):
    prompt = repo.parent / f"task-{slice_id}.md"
    prompt.write_text(f"Implement slice. SLICE={slice_id} {extra}\n")
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), "omnigent", "run", "builder",
         "--config", env["TRIO_TEST_CONFIG"], "--isolate",
         "--mailbox", str(repo / "loop"), "--worker-slice", slice_id,
         "--summary", f"add {slice_id}", "--worktree-root", str(root),
         "--workspace", str(repo), "--prompt-file", str(prompt)],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_concurrent_builders_get_separate_roots_and_keep_lead_config(
    wt, repo, root, fake_env, tmp_path
):
    write_owned_cursor(repo)  # the live Lead's session-bound project config
    lead_before = {n: (repo / ".cursor" / n).read_bytes() for n in ("mcp.json", "hooks.json")}
    procs = [launch(fake_env, repo, root, s) for s in ("A", "B")]
    outs = [p.communicate(timeout=60) for p in procs]
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 0, err
    logs = {s: json.loads((tmp_path / "fake-log" / f"{s}.json").read_text()) for s in "AB"}
    a, b = logs["A"], logs["B"]
    # Real OS-level overlap of the two worker processes.
    assert a["start"] < b["end"] and b["start"] < a["end"]
    # Actual binding: physical cwd == --workspace == own worktree root.
    for log in (a, b):
        assert log["cwd"] == log["workspace"]
        assert Path(log["cwd"]).parent == root.resolve()
        assert wt.cursor_project_root(Path(log["cwd"])) == Path(log["cwd"])
        # The Lead's bridge/stop hook was not visible to the builder.
        assert log["seen"] == {"mcp.json": None, "hooks.json": None}
        assert log["pgid"] == log["pid"]  # own process group
    assert a["cwd"] != b["cwd"]
    # Lead config untouched byte-for-byte.
    for name, data in lead_before.items():
        assert (repo / ".cursor" / name).read_bytes() == data
    # Both merged into the aggregate, as slice commits, gate-visible.
    assert (repo / "A.txt").read_text() == "A\n"
    assert (repo / "B.txt").read_text() == "B\n"
    subjects = git(repo, "log", "--format=%s").splitlines()
    assert "slice(A): add A" in subjects and "slice(B): add B" in subjects
    assert ".cursor" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    records = [r for _i, r in wt.list_records(repo)]
    assert sorted(r["state"] for r in records) == ["integrated", "integrated"]
    for record in records:
        assert wt.is_ancestor(repo, record["worker_commit"], "HEAD")


def test_worker_owned_cursor_residue_is_not_committed_and_is_removed(
    wt, repo, root, fake_env
):
    proc = launch(fake_env, repo, root, "R", extra="RESIDUE")
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert ".cursor" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    (record,) = [r for _i, r in wt.list_records(repo)]
    assert (Path(record["path"]) / ".cursor" / "mcp.json").exists()
    ship(repo, git(repo, "rev-parse", "HEAD"))
    (result,) = wt.cleanup(repo, mailbox=repo / "loop")
    assert result["state"] == "removed", result
    assert not Path(record["path"]).exists()


def test_lingering_worker_descendant_is_drained_before_integration(
    wt, repo, root, fake_env, tmp_path
):
    # Live cursor-agent leaves helpers behind after the wrapper exits.
    proc = launch(dict(fake_env, FAKE_SLEEP="0.1"), repo, root, "L", extra="LINGER")
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    linger = int((tmp_path / "fake-log" / "L.linger").read_text())
    assert not os.path.exists(f"/proc/{linger}") or "Z" in Path(
        f"/proc/{linger}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    (record,) = [r for _i, r in wt.list_records(repo)]
    assert record["state"] == "integrated"


def test_sigterm_to_dispatcher_kills_worker_group_and_retains(
    wt, repo, root, fake_env, tmp_path
):
    proc = launch(dict(fake_env, FAKE_SLEEP="60"), repo, root, "T")
    started = tmp_path / "fake-log" / "T.started"
    deadline = time.time() + 30
    while not started.exists() and time.time() < deadline:
        time.sleep(0.05)
    worker_pid = int(started.read_text())
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    deadline = time.time() + 10
    while os.path.exists(f"/proc/{worker_pid}") and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(f"/proc/{worker_pid}")
    (record,) = [r for _i, r in wt.list_records(repo)]
    assert record["state"] == "retained" and record["retained_reason"] == "interrupted"
    assert Path(record["path"]).is_dir()
    assert wt.cleanup(repo)[0]["state"] == "retained"


def test_failed_worker_keeps_worktree(wt, repo, root, fake_env, tmp_path):
    (tmp_path / "fake-log").chmod(0o500)  # stub cannot write its log -> exits 1
    bad = fake_env
    proc = launch(bad, repo, root, "F")
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 3
    (record,) = [r for _i, r in wt.list_records(repo)]
    assert record["state"] == "retained" and record["retained_reason"] == "worker_failed"
    assert Path(record["path"]).is_dir()


# ------------------------------------------------------------- integration


def test_merge_conflict_is_retained_and_aggregate_is_untouched(wt, repo, root):
    record = make_worker(wt, repo, root, "C", {"shared.txt": "one\nWORKER\nthree\n"})
    (repo / "shared.txt").write_text("one\nLEAD\nthree\n")
    git(repo, "commit", "-qam", "lead edit")
    head = git(repo, "rev-parse", "HEAD")
    result = wt.integrate(repo, record["id"])
    assert result["state"] == "retained"
    assert result["retained_reason"] == "merge_conflict"
    assert git(repo, "rev-parse", "HEAD") == head
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain") == ""
    # Retained: cleanup never deletes it, even with a SHIP on HEAD.
    ship(repo, head)
    (after,) = wt.cleanup(repo, mailbox=repo / "loop")
    assert after["state"] == "retained"
    assert Path(record["path"]).is_dir()
    assert wt.rev(repo, f"refs/heads/{record['branch']}") is not None


def test_dirty_aggregate_and_mailbox_writes_block_integration(wt, repo, root):
    record = make_worker(wt, repo, root, "D")
    (repo / "shared.txt").write_text("uncommitted lead edit\n")
    result = wt.integrate(repo, record["id"])
    assert result["retained_reason"] == "aggregate_dirty"
    git(repo, "checkout", "--", "shared.txt")
    # Retry is allowed and now succeeds.
    assert wt.integrate(repo, record["id"])["state"] == "integrated"

    mailbox_writer = make_worker(wt, repo, root, "M", {"loop/PLAN.md": "hijacked\n"})
    result = wt.integrate(repo, mailbox_writer["id"])
    assert result["retained_reason"] == "mailbox_write"
    assert (repo / "loop" / "PLAN.md").read_text() == "plan\n"


def test_isolated_dispatch_refuses_uncommitted_product_changes(wt, repo, root):
    (repo / "new_product.py").write_text("x = 1\n")
    with pytest.raises(wt.WorktreeError, match="uncommitted product changes"):
        wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)
    # Mailbox edits and the Lead's own generated Cursor config are fine.
    (repo / "new_product.py").unlink()
    (repo / "loop" / "PLAN.md").write_text("edited\n")
    write_owned_cursor(repo)
    wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)


def test_worktree_root_inside_aggregate_is_refused(wt, repo):
    with pytest.raises(wt.WorktreeError, match="inside the aggregate"):
        wt.create(repo, slice_id="X", mailbox=repo / "loop", root=repo / "wts")


# ----------------------------------------------------------------- cleanup


def test_cleanup_waits_for_integration_ship_bound_to_aggregate(wt, repo, root):
    record = make_worker(wt, repo, root, "S")
    assert wt.cleanup(repo)[0]["state"] == "exited"  # not integrated: kept
    wt.integrate(repo, record["id"])
    merged = git(repo, "rev-parse", "HEAD")
    assert wt.cleanup(repo)[0]["state"] == "integrated"  # no verdict yet
    # Slice-level SHIP section is not aggregate acceptance.
    (repo / "loop" / "VERDICT.md").write_text(f"## slice S @{merged} — SHIP\n")
    assert wt.cleanup(repo)[0]["state"] == "integrated"
    # A SHIP naming a commit that predates the merge does not cover it.
    ship(repo, record["base"])
    assert wt.cleanup(repo)[0]["state"] == "integrated"
    # A SHIP on a side commit not on the aggregate branch is refused.
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("s\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-qm", "side")
    side = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    ship(repo, side)
    assert wt.cleanup(repo)[0]["state"] == "integrated"
    ship(repo, merged)
    (done,) = wt.cleanup(repo)
    assert done["state"] == "removed" and done["accepted_by"] == merged
    assert not Path(record["path"]).exists()
    assert wt.rev(repo, f"refs/heads/{record['branch']}") is None


def test_successful_cleanup_is_scoped_and_idempotent(wt, repo, root, tmp_path):
    unrelated = tmp_path / "unrelated-wt"
    git(repo, "worktree", "add", "-q", "-b", "user-branch", str(unrelated))
    git(repo, "branch", "trio-worker/not-ours")  # prefix alone is not ownership
    a = make_worker(wt, repo, root, "A")
    b = make_worker(wt, repo, root, "B")
    wt.integrate(repo, a["id"])
    wt.integrate(repo, b["id"])
    (Path(a["path"]) / "__pycache__").mkdir()
    (Path(a["path"]) / "__pycache__" / "x.pyc").write_bytes(b"\0")
    ship(repo, git(repo, "rev-parse", "HEAD"))
    states = {r["id"]: r["state"] for r in wt.cleanup(repo)}
    assert states == {a["id"]: "removed", b["id"]: "removed"}
    assert unrelated.is_dir()
    branches = git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "user-branch" in branches and "trio-worker/not-ours" in branches
    worktrees = git(repo, "worktree", "list", "--porcelain")
    assert str(unrelated) in worktrees
    # Idempotent second (and third) pass.
    assert {r["state"] for r in wt.cleanup(repo)} == {"removed"}
    assert {r["state"] for r in wt.cleanup(repo)} == {"removed"}


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda p: (p / "notes.md").write_text("new\n"), "untracked"),
        (lambda p: (p / "a.txt").write_text("changed\n"), "dirty"),
        (lambda p: (p / "debug.log").write_text("user data\n"), "ignored_content"),
    ],
)
def test_post_integration_work_is_retained(wt, repo, root, mutate, reason):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    mutate(Path(record["path"]))
    (result,) = wt.cleanup(repo)
    assert result["state"] == "retained" and result["retained_reason"] == reason
    assert Path(record["path"]).is_dir()


def test_new_commit_in_worktree_after_integration_is_retained(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    path = Path(record["path"])
    (path / "late.txt").write_text("late\n")
    git(path, "add", "late.txt")
    git(path, "commit", "-qm", "late work")
    (result,) = wt.cleanup(repo)
    assert result["retained_reason"] == "unintegrated_commits"


def test_non_owned_cursor_config_is_user_content(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    path = Path(record["path"])
    write_owned_cursor(path)
    extra = json.loads((path / ".cursor" / "mcp.json").read_text())
    extra["mcpServers"]["mine"] = {"command": "my-server"}
    (path / ".cursor" / "mcp.json").write_text(json.dumps(extra))
    (result,) = wt.cleanup(repo)
    assert result["retained_reason"] == "untracked"
    assert (path / ".cursor" / "mcp.json").exists()


def test_active_session_blocks_until_it_exits(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    user = subprocess.Popen(["sleep", "60"], cwd=record["path"])
    try:
        (result,) = wt.cleanup(repo)
        assert result["retained_reason"] == "active_session"
        assert Path(record["path"]).is_dir()
    finally:
        user.kill()
        user.wait()
    (result,) = wt.cleanup(repo)
    assert result["state"] == "removed"


def test_orphaned_process_group_member_blocks_cleanup(wt, repo, root, tmp_path):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    # A detached descendant outside the worktree but in the worker's group.
    orphan = subprocess.Popen(["sleep", "60"], cwd=tmp_path, start_new_session=True)
    try:
        rec = wt.load_record(repo, record["id"])
        rec["pgid"] = orphan.pid
        wt.save_record(repo, rec)
        (result,) = wt.cleanup(repo)
        assert result["retained_reason"] == "active_session"
    finally:
        orphan.kill()
        orphan.wait()
    assert wt.cleanup(repo)[0]["state"] == "removed"


def test_held_broker_session_keeps_worktree(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    rec = wt.load_record(repo, record["id"])
    rec["session_ids"] = ["sess-held"]
    wt.save_record(repo, rec)
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    (result,) = wt.cleanup(repo, held_sessions={"sess-held"})
    assert result["retained_reason"] == "held_session"
    assert wt.cleanup(repo, held_sessions=set())[0]["state"] == "removed"


def test_tampered_ownership_is_retained(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    (Path(record["admin_dir"]) / wt.OWNER_MARKER).write_text("someone-else\n")
    (result,) = wt.cleanup(repo)
    assert result["retained_reason"] == "uncertain_ownership"
    assert Path(record["path"]).is_dir()


# ------------------------------------------------------ interruption/restart


def test_interrupted_dispatch_is_retained_then_recoverable(wt, repo, root):
    record = wt.create(repo, slice_id="I", mailbox=repo / "loop", root=root)
    (Path(record["path"]) / "i.txt").write_text("partial\n")
    rec = wt.load_record(repo, record["id"])
    rec["state"] = "running"
    rec["dispatcher"] = {"pid": 2**22 + 7, "start": "0"}  # dead
    rec["worker"] = None
    wt.save_record(repo, rec)
    (result,) = wt.cleanup(repo)
    assert result["retained_reason"] == "interrupted"
    assert Path(record["path"]).is_dir()
    assert wt.integrate(repo, record["id"])["state"] == "integrated"
    assert (repo / "i.txt").read_text() == "partial\n"


def test_restart_after_worktree_removed_finishes_branch_step(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    # Simulate a crash right after `git worktree remove`.
    rec = wt.load_record(repo, record["id"])
    rec["state"] = "removing"
    rec["accepted_by"] = git(repo, "rev-parse", "HEAD")
    wt.save_record(repo, rec)
    git(repo, "worktree", "remove", record["path"])
    assert wt.rev(repo, f"refs/heads/{record['branch']}") is not None
    (result,) = wt.cleanup(repo)
    assert result["state"] == "removed"
    assert wt.rev(repo, f"refs/heads/{record['branch']}") is None
    assert wt.cleanup(repo)[0]["state"] == "removed"


def test_restart_before_worktree_removed_resumes(wt, repo, root):
    record = make_worker(wt, repo, root, "A")
    wt.integrate(repo, record["id"])
    ship(repo, git(repo, "rev-parse", "HEAD"))
    rec = wt.load_record(repo, record["id"])
    rec["state"] = "removing"
    rec["accepted_by"] = git(repo, "rev-parse", "HEAD")
    wt.save_record(repo, rec)
    (result,) = wt.cleanup(repo)
    assert result["state"] == "removed"
    assert not Path(record["path"]).exists()


def test_crash_after_create_record_before_worktree_is_retained(wt, repo, root):
    record = wt.create(repo, slice_id="Z", mailbox=repo / "loop", root=root)
    rec = wt.load_record(repo, record["id"])
    rec["creator"] = {"pid": 2**22 + 9, "start": "0"}
    wt.save_record(repo, rec)
    (result,) = wt.cleanup(repo)
    assert result["state"] == "retained"
    assert Path(record["path"]).is_dir()


# ---------------------------------------------- evaluator session binding


class _BindingClient:
    def __init__(self):
        self.workspaces = []

    def create_session(self, agent_id, model, prompt, title, workspace=None, **_kw):
        self.workspaces.append(workspace)
        write_owned_cursor(Path(workspace), bridge="/bridges/eval")  # what Omnigent writes
        return {"id": f"sess-{len(self.workspaces)}"}

    def wait_session(self, session_id, timeout=None, interval=None):
        return {"status": "idle"}

    def get_items(self, session_id):
        return []


def test_slice_eval_session_is_bound_to_detached_pinned_worktree(wt, repo, root, monkeypatch):
    trioctl = _load("trioctl_worktree_eval", SCRIPT)
    monkeypatch.setattr(trioctl, "worker_worktrees", wt)
    sha = git(repo, "rev-parse", "HEAD")
    runner = trioctl.OmnigentRunner(
        repo=repo, broker_client=_BindingClient(), config={}, interval=0,
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
    )
    context = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": sha}
    seen = {}

    monkeypatch.setattr(runner, "_agent_id", lambda role: "eval-agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "grade it\n")

    def fake_dispatch(client, agent_id, model, prompt, title, role, iteration,
                      mailbox, ctx, started, before_text, before_mtime,
                      created_before, workspace):
        seen["prompt"] = prompt
        runner._create_wait_read(client, agent_id, model, prompt, title, role,
                                 workspace=workspace)
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", fake_dispatch)
    assert runner.run("evaluator", 1, repo / "loop", context) == 0
    (bound,) = runner._client().workspaces
    assert bound != str(repo) and Path(bound).parent == root.resolve()
    assert wt.cursor_project_root(Path(bound)) == Path(bound)
    assert "do NOT" in seen["prompt"] and bound in seen["prompt"]
    # Lead's root config not touched by the evaluator launch.
    assert not (repo / ".cursor").exists()
    (record,) = [r for _i, r in wt.list_records(repo)]
    assert record["kind"] == "eval" and record["finished"]
    assert record["session_ids"] == ["sess-1"]
    # Held: kept. Released: owned residue deleted and worktree removed.
    assert wt.cleanup(repo, held_sessions={"sess-1"})[0]["retained_reason"] == "held_session"
    assert wt.cleanup(repo, held_sessions=set())[0]["state"] == "removed"
    assert not Path(bound).exists()


def test_lead_prompt_carries_isolated_dispatch_block(repo, root):
    trioctl = _load("trioctl_worktree_prompt", SCRIPT)
    runner = trioctl.OmnigentRunner(
        repo=repo, config={}, isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
    )
    block = runner._isolate_block(2, repo / "loop")
    assert "--isolate" in block and "--worker-slice <slice-id>" in block
    assert str(root) in block and "never" in block

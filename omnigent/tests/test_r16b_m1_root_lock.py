"""eval-r16rc-b M1: a root-free driver HOLDS the root mailbox `.lock`.

The loop core's mailbox lock (`<root mailbox>/.lock/{owner,pid}`) is the only
mark a lock-only driver (native `metrics/trio_loop.py`, a pre-r16 release
after a rollback, a lockstep core run by hand) honours. A root-free driver
takes it with the core's own protocol before it creates anything and keeps
it until its very end, so such a driver refuses at any time during the run;
the seed never copies it; the land refuses to write the root mailbox when the
lock was taken away; and a refusal after the acquisition releases it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, World, git, init_repo, snapshot_root


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16b_m1")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    return home


def _core_acquire_in_subprocess(box: Path) -> str:
    """The native core's `_acquire_lock` in another process: 'refused' or 'took'."""
    code = (
        "import sys; sys.dont_write_bytecode = True\n"
        "import importlib.machinery, importlib.util\n"
        f"l = importlib.machinery.SourceFileLoader('c', {str(REPO_ROOT / 'metrics' / 'trio_loop.py')!r})\n"
        "s = importlib.util.spec_from_loader('c', l); m = importlib.util.module_from_spec(s)\n"
        "l.exec_module(m)\n"
        f"lock = m._acquire_lock({str(box)!r})\n"
        "print('refused' if lock is None else 'took'); m._release_lock(lock)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_root_lock_held_for_the_whole_run_and_released(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/m1", [{"id": "m1-a", "write": "src/m1a.py"}])
    box = spec["root_box"]
    before = snapshot_root(home)
    seen: dict[str, object] = {}

    def probe(kind):
        def hook(w, sp, runner, ctx, workspace, mailbox, prompt, iteration):
            lock = box / ".lock"
            seen[kind] = {
                "pid": (lock / "pid").read_text().strip() if lock.is_dir() else None,
                "native": _core_acquire_in_subprocess(box),
            }
            return False  # default behaviour afterwards
        return hook

    world.hooks["lead-pass"] = probe("lead-pass")
    world.hooks["integration-eval"] = probe("integration-eval")
    assert world.run_loop(spec) == 0
    for kind in ("lead-pass", "integration-eval"):
        assert seen[kind] == {"pid": str(os.getpid()), "native": "refused"}, seen
    assert not (box / ".lock").exists()
    assert not list(box.glob(".lock.stale-*"))
    # The lock was never seeded nor landed (the mailbox has no .gitignore).
    tracked = git(home, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert not [p for p in tracked if "/.lock" in p], tracked
    after = snapshot_root(home)
    assert after["status"] == ""  # landed: the mailbox is committed on main
    assert before["head"] != after["head"]
    # A native driver can take the mailbox once the root-free run ended.
    assert _core_acquire_in_subprocess(box) == "took"


def test_native_driver_holding_the_lock_refuses_root_free_and_vice_versa(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/m1b", [{"id": "m1b-a", "write": "src/m1b.py"}])
    box = spec["root_box"]
    # Native holder: a real core `_acquire_lock` in a live child process.
    code = (
        "import sys, time; sys.dont_write_bytecode = True\n"
        "import importlib.machinery, importlib.util\n"
        f"l = importlib.machinery.SourceFileLoader('c', {str(REPO_ROOT / 'metrics' / 'trio_loop.py')!r})\n"
        "s = importlib.util.spec_from_loader('c', l); m = importlib.util.module_from_spec(s)\n"
        "l.exec_module(m)\n"
        f"lock = m._acquire_lock({str(box)!r}); print('held', flush=True); time.sleep(120)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    try:
        assert holder.stdout.readline().strip() == "held"
        capsys.readouterr()
        assert world.run_loop(spec) == 5
        err = capsys.readouterr().err
        assert f"pid {holder.pid}" in err and "nothing was created" in err
        assert world.events == []
        assert git(home, "branch", "--list", "trio/*") == ""
    finally:
        holder.kill()
        holder.wait()
    # The holder died: its lock is stale; root-free clears it like the core.
    assert world.run_loop(spec) == 0
    assert not (box / ".lock").exists()


def test_refusal_after_acquisition_releases_the_lock(world, tmp_path, capsys, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/m1c", [{"id": "m1c-a", "write": "src/m1c.py"}])
    box = spec["root_box"]
    t = world.trioctl
    real_begin = world.rf.begin

    def failing_begin(*a, **kw):
        assert (box / ".lock" / "pid").read_text().strip() == str(os.getpid())
        raise world.rf.RootFreeError("synthetic begin failure")

    monkeypatch.setattr(world.rf, "begin", failing_begin)
    assert world.run_loop(spec) == 3
    assert "synthetic begin failure" in capsys.readouterr().err
    assert not (box / ".lock").exists()
    monkeypatch.setattr(world.rf, "begin", real_begin)
    assert world.run_loop(spec) == 0
    assert t is world.trioctl


def test_land_refuses_when_the_root_lock_was_taken_away(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/m1d", [{"id": "m1d-a", "write": "src/m1d.py"}])
    box = spec["root_box"]
    main_before = git(home, "rev-parse", "main")

    def steal(w, sp, runner, ctx, workspace, mailbox, prompt, iteration):
        # Someone force-replaced the lock (e.g. removed it by hand and a
        # lock-only driver took it): the owner token is no longer ours.
        (box / ".lock" / "owner").write_text("someone-else\n")
        return False

    world.hooks["integration-eval"] = steal
    capsys.readouterr()
    assert world.run_loop(spec) == 8
    err = capsys.readouterr().err
    assert "no longer held by this driver" in err
    assert git(home, "rev-parse", "main") == main_before
    record = world.rf.load_record(world.wt, home, spec["slug"])
    state = (Path(record["live_mailbox"]) / "STATE.md").read_text()
    assert "status: needs_land" in state and "phase: land-blocked" in state
    # Not ours: left in place for its owner.
    assert (box / ".lock" / "owner").read_text().strip() == "someone-else"
    (box / ".lock" / "pid").write_text("999999999\n")  # its owner died
    assert world.run_land(spec) == 0
    assert git(home, "rev-parse", "main") != main_before


def test_discard_pristine_never_removes_a_worktree_whose_root_lock_is_held(world, tmp_path):
    """eval-r16rc-b L1: a racing start never deletes a starting driver's
    fresh Lead worktree (the starter holds the root lock before it creates it)."""
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/m1e", [{"id": "m1e-a", "write": "src/m1e.py"}])
    box = spec["root_box"]
    wt, rf = world.wt, world.rf
    record, created = rf.begin(
        wt, home=home, mailbox_rel="loop/m1e",
        worktree_root=wt.default_worktree_root(home),
    )
    assert created
    other = subprocess.Popen(["sleep", "120"])
    try:
        (box / ".lock").mkdir()
        (box / ".lock" / "pid").write_text(f"{other.pid}\n")
        (box / ".lock" / "owner").write_text("theirs\n")
        assert world.trioctl._discard_pristine_lead(box, "target moved") is False
        assert Path(record["path"]).is_dir()
        assert rf.load_record(wt, home, spec["slug"])["state"] == "active"
    finally:
        other.kill()
        other.wait()
    # Holder gone: the pristine worktree is discardable again.
    assert world.trioctl._discard_pristine_lead(box, "target moved") is True
    assert not Path(record["path"]).exists()
    assert json.loads(json.dumps(rf.load_record(wt, home, spec["slug"])))["state"] == "removed"

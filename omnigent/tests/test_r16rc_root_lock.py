"""eval-r16rc B1: a root-free start honours the ROOT mailbox `.lock`.

Root-bound, lockstep, native `metrics/trio_loop.py` and pre-r16 (9a224c1)
drivers never write the live-loop registry; their only mark is the loop
core's mailbox lock `<root mailbox>/.lock/pid`. A root-free start over a
live holder must exit 5 with the root mailbox byte-identical and nothing
created (no Lead worktree, branch, record); a stale lock (dead pid) is
cleared the way the core's `_acquire_lock` clears it and the loop proceeds.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16rc_lock")


def _home(tmp_path):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": "", "src/app.py": "v0\n"})
    return home


def _tree_bytes(d: Path):
    return {str(p.relative_to(d)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(d.rglob("*"))}


def _old_driver_mailbox(world, home, rel, sid):
    spec = world.add_loop(home, rel, [{"id": sid, "write": f"src/{sid}.py"}])
    box = spec["root_box"]
    (box / ".gitignore").write_text(".lock/\n.sessions/\n.driver.json\n.session.json\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 2\nmax_iterations: 5\nstatus: running\nmission: old\n"
    )
    return spec, box


def _lock(box: Path, pid: int) -> None:
    (box / ".lock").mkdir()
    (box / ".lock" / "pid").write_text(f"{pid}\n")
    (box / ".lock" / "owner").write_text("tok\n")


def test_s9_live_root_lock_refuses_root_free_start_byte_identical(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec, box = _old_driver_mailbox(world, home, "loop/s9", "s9-one")
    holder = subprocess.Popen(["sleep", "300"])
    try:
        _lock(box, holder.pid)
        before = _tree_bytes(box)
        head = git(home, "rev-parse", "main")
        capsys.readouterr()
        code = world.run_loop(spec)
        err = capsys.readouterr().err
        assert code == 5, err
        assert f"pid {holder.pid}" in err and "nothing was created" in err
        assert _tree_bytes(box) == before
        assert world.events == []
        assert world.rf.load_record(world.wt, home, spec["slug"]) is None
        assert git(home, "branch", "--list", "trio/*") == ""
        assert len(git(home, "worktree", "list").splitlines()) == 1
        assert git(home, "rev-parse", "main") == head
        loops = home / ".git" / "trio-worktrees" / "loops"
        assert not loops.is_dir() or list(loops.glob("*.json")) == []
        # `land` over the same live holder: refused the same way.
        capsys.readouterr()
        assert world.run_land(spec) in (2, 5)
        assert _tree_bytes(box) == before
    finally:
        holder.kill()
        holder.wait()


def test_live_root_lock_refuses_reattach_of_an_existing_root_free_loop(world, tmp_path, capsys):
    """Resume of a root-free loop while an old driver took the root mailbox."""
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/re", [{"id": "re-one", "write": "src/re.py"}])
    world.hooks["integration-eval"] = lambda *a: (_ for _ in ()).throw(RuntimeError("stop"))
    assert world.run_loop(spec) == 3  # driver exception: Lead worktree kept
    world.hooks.clear()
    record = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(record)
    box = spec["root_box"]
    holder = subprocess.Popen(["sleep", "300"])
    try:
        _lock(box, holder.pid)
        before = _tree_bytes(box)
        n = len(world.events)
        capsys.readouterr()
        assert world.run_loop(spec) == 5
        assert "owned by a live driver" in capsys.readouterr().err
        assert _tree_bytes(box) == before
        assert len(world.events) == n
    finally:
        holder.kill()
        holder.wait()


def test_pidless_fresh_root_lock_is_owned(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec, box = _old_driver_mailbox(world, home, "loop/np", "np-one")
    (box / ".lock").mkdir()
    before = _tree_bytes(box)
    assert world.run_loop(spec) == 5
    assert _tree_bytes(box) == before
    assert world.rf.load_record(world.wt, home, spec["slug"]) is None


def test_stale_root_lock_is_cleared_and_the_loop_proceeds(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec, box = _old_driver_mailbox(world, home, "loop/st", "st-one")
    dead = subprocess.Popen(["true"])
    dead.wait()
    _lock(box, dead.pid)
    capsys.readouterr()
    code = world.run_loop(spec)
    err = capsys.readouterr().err
    assert code == 0, err
    assert "Removing stale lock" in err
    assert not (box / ".lock").exists()
    assert not list(box.glob(".lock.stale-*"))
    assert git(home, "show", "main:src/st-one.py") == "# st-one"
    assert "status: shipped" in (box / "STATE.md").read_text()


def test_old_pidless_root_lock_is_stale(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec, box = _old_driver_mailbox(world, home, "loop/op", "op-one")
    (box / ".lock").mkdir()
    old = time.time() - 3600
    os.utime(box / ".lock", (old, old))
    assert world.run_loop(spec) == 0
    assert not (box / ".lock").exists()


def test_stale_root_lock_is_left_alone_when_the_start_is_refused(world, tmp_path, capsys):
    """A refusal after the lock check (old metrics set) changes nothing."""
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "r\n"}, metrics=False)
    spec, box = _old_driver_mailbox(world, home, "loop/rf", "rf-one")
    dead = subprocess.Popen(["true"])
    dead.wait()
    _lock(box, dead.pid)
    before = _tree_bytes(box)
    assert world.run_loop(spec) == 3
    assert _tree_bytes(box) == before

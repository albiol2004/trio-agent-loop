"""r14.1 F3: same-cwd lock files of vanished workspaces are pruned at loop start.

eval-r14 F3: `_SameCwdGate` created one lock file per workspace realpath
and never deleted it; each isolated slice-eval worktree path is unique, so
one file per slice-eval dispatch accumulated in `cwd-locks/`. Pruning
unlinks only files this process has flocked whose workspace is gone, and
`acquire` re-checks its locked inode is still at the path, so a waiter that
queued on a pruned inode never overlaps the holder of a fresh file.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import threading
import time

import pytest

from test_r14_same_cwd import BindLagClient, trioctl


def _lock_file(directory, workspace, name):
    path = directory / f"{name}.lock"
    path.write_text(f"123 {workspace}\n")
    return path


def test_prune_removes_only_unheld_locks_of_vanished_workspaces(tmp_path):
    locks = tmp_path / "cwd-locks"
    locks.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    gone = _lock_file(locks, tmp_path / "gone-eval", "a")
    kept_live = _lock_file(locks, live, "b")
    held = _lock_file(locks, tmp_path / "gone-but-held", "c")
    blank = locks / "d.lock"
    blank.write_text("")
    holder = open(held, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        removed = trioctl._prune_same_cwd_locks(locks)
    finally:
        holder.close()
    assert removed == [gone]
    assert not gone.exists()
    assert kept_live.exists() and held.exists() and blank.exists()
    assert trioctl._prune_same_cwd_locks(tmp_path / "missing") == []


def test_gate_waiter_on_a_pruned_inode_never_overlaps_a_fresh_holder(tmp_path, monkeypatch):
    locks = tmp_path / "cwd-locks"
    monkeypatch.setenv(trioctl.SAME_CWD_LOCK_DIR_ENV, str(locks))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    gate = trioctl._SameCwdGate(BindLagClient(0.0), str(workspace))
    path = gate._path()
    locks.mkdir()
    # Holder A (old inode), the gate queues on it.
    a = open(path, "a+")
    fcntl.flock(a.fileno(), fcntl.LOCK_EX)
    done = threading.Event()
    t = threading.Thread(target=lambda: (gate.acquire(), done.set()))
    t.start()
    time.sleep(0.2)
    assert not done.is_set()
    # The file is pruned under A's lock; C takes a fresh file at the path.
    path.unlink()
    c = open(path, "a+")
    fcntl.flock(c.fileno(), fcntl.LOCK_EX)
    a.close()  # the gate can now lock the unlinked inode: must not count
    time.sleep(0.3)
    assert not done.is_set(), "gate treated a pruned inode as exclusive"
    c.close()
    assert done.wait(5)
    t.join()
    try:
        assert os.fstat(gate._fh.fileno()).st_ino == os.stat(path).st_ino
    finally:
        gate.release()


def test_loop_start_prunes_same_cwd_locks(tmp_path, monkeypatch):
    locks = tmp_path / "cwd-locks"
    locks.mkdir()
    monkeypatch.setenv(trioctl.SAME_CWD_LOCK_DIR_ENV, str(locks))
    stale = _lock_file(locks, tmp_path / "eval-A-deadbeef", "stale")
    monkeypatch.chdir(tmp_path)

    def stop(_repo):
        raise trioctl.TrioctlError("stop after housekeeping")

    monkeypatch.setattr(trioctl, "_load_trio_loop", stop)
    with pytest.raises(trioctl.TrioctlError, match="stop after housekeeping"):
        trioctl._command_loop(argparse.Namespace(mailbox=str(tmp_path / "loop")))
    assert not stale.exists()

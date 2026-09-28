"""r14.1 F1: a degraded (root-bound) slice-eval no longer ends the driver.

eval-r14 F1 (repro `test_eval_e1_overlap.py`): while a degraded slice-eval
still ran at the aggregate root, the Lead thread's next pass called
`_release_root`, saw the eval's cursor-agent there, waited
ROOT_RELEASE_WAIT (30 s) and raised TrioctlError -> loop `status: error`,
exit 3. A degraded eval of THIS run is now a known occupant: the Lead pass
waits for it (bounded by that dispatch's role timeout), releases its
finished session and proceeds; only unknown cursor-agents still refuse.
"""
from __future__ import annotations

import threading
import time

import pytest

from test_r14_eval_degrade import _runner, git, repo, trioctl, wt  # noqa: F401


def _degraded(wt, monkeypatch):
    monkeypatch.setattr(
        wt, "create", lambda *a, **k: (_ for _ in ()).throw(wt.WorktreeError("boom"))
    )


def _slow_eval(runner, monkeypatch):
    """The eval dispatch creates its session, then runs until released."""
    orig = runner._run_dispatch
    started = threading.Event()
    release = threading.Event()

    def dispatch(*a, **k):
        if a[5] == "evaluator":
            code = orig(*a, **k)
            started.set()
            release.wait(10)
            return code
        return orig(*a, **k)

    monkeypatch.setattr(runner, "_run_dispatch", dispatch)
    return started, release


def test_next_lead_pass_waits_for_degraded_eval_then_proceeds(
    trioctl, wt, repo, tmp_path, monkeypatch, capsys
):
    _degraded(wt, monkeypatch)
    runner, seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    monkeypatch.setattr(runner, "ROOT_RELEASE_WAIT", 0.2)
    pruned: list[str] = []
    prune_lock = threading.Lock()

    def prune(_client, _mailbox, session_ids=None, **_kw):
        with prune_lock:
            pruned.extend(session_ids or [])
        return {"deleted": len(session_ids or [])}

    monkeypatch.setattr(trioctl, "_prune_broker_sessions", prune)
    # The eval's cursor-agent runs at the root until its session is ended.
    monkeypatch.setattr(wt, "cursor_processes_at",
                        lambda root: [] if "s1" in pruned else [424242])
    started, release = _slow_eval(runner, monkeypatch)
    sha = git(repo, "rev-parse", "HEAD")
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A", "sha": sha}
    codes = {}
    t = threading.Thread(
        target=lambda: codes.setdefault("eval", runner.run("evaluator", 1, repo / "loop", ctx))
    )
    t.start()
    assert started.wait(10)
    timer = threading.Timer(1.0, release.set)
    timer.start()
    try:
        t0 = time.monotonic()
        assert runner.run("lead", 2, repo / "loop", {"mode": "open-loop"}) == 0
        waited = time.monotonic() - t0
    finally:
        release.set()
        timer.cancel()
        t.join()
    assert codes["eval"] == 0
    # Waited for the eval (well past the 0.2 s stranger window), not refused.
    assert waited >= 0.8
    # The eval's finished session, released exactly once; r15.x: the Lead's
    # own session (s2) is ended at its turn end, not left idle at the root.
    assert pruned == ["s1", "s2"]
    assert runner._client().workspaces == [str(repo), str(repo)]
    err = capsys.readouterr().err
    assert err.count("trioctl: root busy with degraded slice-eval A; Lead pass waits") == 1
    assert "still run at the aggregate root" not in err
    assert runner.driver_meta["root_wait_s"] >= 0.8
    assert runner._degraded_root_evals == {}


def test_unknown_root_cursor_agent_still_refuses(trioctl, wt, repo, tmp_path, monkeypatch, capsys):
    runner, _seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    monkeypatch.setattr(runner, "ROOT_RELEASE_WAIT", 0.2)
    monkeypatch.setattr(wt, "cursor_processes_at", lambda root: [999])
    t0 = time.monotonic()
    with pytest.raises(trioctl.TrioctlError, match="still run at the aggregate root"):
        runner.run("lead", 2, repo / "loop", {"mode": "open-loop"})
    assert time.monotonic() - t0 < 5
    assert "Lead pass waits" not in capsys.readouterr().err
    assert "root_wait_s" not in runner.driver_meta


def test_degraded_eval_wait_is_bounded_by_its_role_timeout(
    trioctl, wt, repo, tmp_path, monkeypatch, capsys
):
    _degraded(wt, monkeypatch)
    runner, _seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    monkeypatch.setattr(runner, "ROOT_RELEASE_WAIT", 0.2)
    monkeypatch.setattr(wt, "cursor_processes_at", lambda root: [424242])
    started, release = _slow_eval(runner, monkeypatch)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A",
           "sha": git(repo, "rev-parse", "HEAD")}
    t = threading.Thread(target=lambda: runner.run("evaluator", 1, repo / "loop", ctx))
    t.start()
    try:
        assert started.wait(10)
        runner._timeout = 0.3  # the stuck eval's role timeout has passed
        t0 = time.monotonic()
        with pytest.raises(trioctl.TrioctlError, match="still run at the aggregate root"):
            runner.run("lead", 2, repo / "loop", {"mode": "open-loop"})
        assert time.monotonic() - t0 < 5
    finally:
        release.set()
        t.join()
    err = capsys.readouterr().err
    assert "outlived their role timeout" in err

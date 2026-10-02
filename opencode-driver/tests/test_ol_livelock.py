"""slice(ol-livelock): an open-loop run whose acceptance freeze lands while a
builder is running must not livelock on the commit gate.

In a real run the acceptance author works alongside the Lead, so a builder is
dispatched (its worktree branch cut from the pre-freeze HEAD) before the
freeze commit exists. The driver then merged that branch AFTER the freeze, and
the shared commit gate (``trio-shadow --require-commits``) rejected the slice
commit as "a slice on a line that does not contain the freeze" -- for good.
The poll loop spun on ``commit gate failed for slice X`` forever (STATE
``idle``/``running``, nothing alive, no time limit).

Fakes only (``scenarios/ol_acc_slowfreeze.py``); no live model call.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from trio_opencode import authorbox, driver, openloop

from test_acc_harden import _acc_repo
from test_openloop_e2e import (  # noqa: E402 - sibling test module's helpers
    install_fake_for_process, make_cfg, make_key_file, read_calls,
)

ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _run_slowfreeze(tmp_path: Path, monkeypatch, watchdog_s: float = 120.0):
    root = _acc_repo(tmp_path)
    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_acc_slowfreeze.py")
    monkeypatch.setenv("TRIO_NATIVE_JOBS", "inline")
    monkeypatch.setenv("FAKE_REPO_PATH", str(root))
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (False, "test: no sandbox"))
    # a livelock must FAIL this test, not hang the suite: after the watchdog the
    # driver is told to stop (its idle signal handler), as an operator would
    fired = threading.Event()

    def _bark() -> None:
        fired.set()
        signal.raise_signal(signal.SIGTERM)

    timer = threading.Timer(watchdog_s, _bark)
    timer.daemon = True
    timer.start()
    try:
        result = driver.run(root / "loop", cfg, mode="start", max_iterations=4, root_free=False,
                            acceptance=True)
    finally:
        timer.cancel()
    assert not fired.is_set(), (
        "livelock: the run was still going after the watchdog; LOG:\n"
        + (root / "loop" / "LOG.md").read_text(encoding="utf-8")[-1500:])
    return root, env, result


def test_freeze_landing_mid_builder_does_not_livelock_on_the_commit_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, env, result = _run_slowfreeze(tmp_path, monkeypatch)
    log = (root / "loop" / "LOG.md").read_text(encoding="utf-8")

    assert result["status"] == "shipped", (result, log)
    assert result["code"] == 0, result
    # the builder really was cut before the freeze: the pack froze while it ran
    assert any(c["agent"] == "trio-builder" for c in read_calls(env))
    assert "commit gate failed" not in log, log
    # the history is what the gate demands: every slice commit descends the
    # freeze commit (the builder's branch was moved on top of it before merging)
    freeze = _git(root, "log", "--first-parent", "--format=%H", "--grep=^acceptance: freeze").splitlines()[-1]
    slices = _git(root, "log", "--format=%H", "--grep=^slice(").splitlines()
    assert slices, "no slice commit"
    for sha in slices:
        anc = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", freeze, sha])
        assert anc.returncode == 0, f"slice commit {sha[:8]} does not contain the freeze {freeze[:8]}"
    # and the shared gate itself agrees (the very command the driver runs)
    gate = subprocess.run(["python3", str(SHADOW), "--mailbox", str(root / "loop"),
                           "--require-commits", "--slice", "app"], capture_output=True, text=True)
    assert gate.returncode == 0, gate.stdout


def test_a_gate_failure_nothing_can_repair_stops_loudly_instead_of_spinning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural half: even when the cause is NOT fixed (the rebase is
    disabled here, so the pre-freeze branch lands behind the freeze exactly as
    in the failing run, and the old slice commit stays an offender for ever),
    the run does not spin. The gate failure is logged once with its reason, and
    after the grace period the run ends `status: error` naming the slice."""
    monkeypatch.setattr(openloop.OpenLoopRunner, "_rebase_onto_freeze", lambda *a, **k: None)
    root, env, result = _run_slowfreeze(tmp_path, monkeypatch, watchdog_s=100.0)
    log = (root / "loop" / "LOG.md").read_text(encoding="utf-8")

    assert result["status"] == "error", (result, log)
    assert result["code"] == 3, result
    assert "commit gate keeps failing for slice app" in log, log
    assert "acceptance/freeze ordering" in log, log          # the cause, not a bare failure
    # one line per distinct failure, not one per poll
    assert log.count("commit gate failed for slice app") == 1, log
    # the Lead is a planner here: it is not re-invoked for a gate it cannot fix
    # (one plan + one review call, from the single pass)
    assert len([c for c in read_calls(env) if c["agent"] == "trio-lead"]) == 2
    # nothing was graded or shipped on top of the unverifiable slice
    assert not [c for c in read_calls(env) if c["agent"] == "trio-evaluator"]
    verdict = root / "loop" / "VERDICT.md"
    assert not verdict.exists() or "VERDICT: SHIP" not in verdict.read_text(encoding="utf-8")

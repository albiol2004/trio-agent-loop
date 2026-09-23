"""Bounded post-SHIP retirement wait: race, timeout, resume, strict guard.

A valid SHIP can reach VERDICT.md before the Evaluator's retirement
commit lands. The driver rechecks on a fake clock here (no real sleeps).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import pytest

from metrics import trio_loop
from metrics.tests.test_trio_loop import (
    FakeRunner,
    _commit_relative,
    _git_head,
    init_git,
    make_mailbox,
    state_text,
)


class FakeClock:
    """Monotonic clock + sleep seam; runs hooks on chosen sleep calls."""

    def __init__(self, mailbox: Path | None = None) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.hooks: dict[int, Callable[[], None]] = {}
        self.mailbox = mailbox

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # The loop lock must stay owned by this driver while waiting.
        if self.mailbox is not None:
            lock = self.mailbox / ".lock"
            assert lock.is_dir()
            pid = (lock / "pid").read_text(encoding="utf-8").strip()
            assert pid == str(os.getpid())
        self.sleeps.append(seconds)
        self.now += seconds
        hook = self.hooks.get(len(self.sleeps))
        if hook is not None:
            hook()


@pytest.fixture
def waiting(monkeypatch: pytest.MonkeyPatch):
    """Enable a 10s/2s retirement wait on a fake clock."""
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, "10")
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, "2")

    def install(mailbox: Path | None = None) -> FakeClock:
        fake = FakeClock(mailbox)
        monkeypatch.setattr(trio_loop, "_retirement_clock", fake.clock)
        monkeypatch.setattr(trio_loop, "_retirement_sleep", fake.sleep)
        return fake

    return install


def _repo_with_mailbox(tmp_path: Path) -> Path:
    init_git(tmp_path)
    _commit_relative(tmp_path, "seed.txt", "s\n", "seed")
    return make_mailbox(tmp_path)


def _retire(repo: Path, mailbox: Path) -> str:
    """The Evaluator's mailbox retirement commit for iteration 1."""
    return _commit_relative(
        repo,
        "mailbox/VERDICT.md",
        (mailbox / "VERDICT.md").read_text(encoding="utf-8"),
        "loop: iteration 1 — SHIP",
    )


SHIP_TEXT = "VERDICT: SHIP\n# Verdict — iteration 1\n"


def _log(mailbox: Path) -> str:
    return (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_delayed_retirement_ships_without_redispatch(
    tmp_path: Path, waiting
) -> None:
    """Observed race: SHIP first, retirement commit a few polls later."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    fake.hooks[2] = lambda: _retire(tmp_path, mailbox)
    runner = FakeRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 0
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    assert len(fake.sleeps) == 2
    text = state_text(mailbox)
    assert "status: shipped" in text
    assert "phase: shipped" in text
    log = _log(mailbox)
    assert "SHIP awaiting retirement (up to 10s)" in log
    assert "SHIP retirement verified after 4.0s" in log
    assert not (mailbox / ".lock").exists()


def test_immediate_retirement_ships_without_waiting(
    tmp_path: Path, waiting
) -> None:
    """A retirement already present is accepted at once."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)

    class RetiringRunner(FakeRunner):
        def run(self, role, iteration, mailbox_dir, context=None):
            code = super().run(role, iteration, mailbox_dir, context)
            if role == "evaluator":
                _retire(tmp_path, mailbox_dir)
            return code

    runner = RetiringRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 0
    assert fake.sleeps == []
    assert "status: shipped" in state_text(mailbox)
    assert "awaiting retirement" not in _log(mailbox)


def test_timeout_is_resumable_with_exact_diagnostic(
    tmp_path: Path, waiting
) -> None:
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    runner = FakeRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    assert sum(fake.sleeps) == pytest.approx(10.0)
    assert fake.sleeps == [2.0] * 5
    text = state_text(mailbox)
    assert "status: needs_retirement" in text
    assert "phase: ship-pending-retirement" in text
    assert (
        "- iter 1 | loop | verdict: SHIP not accepted (retirement not "
        "found after waiting 10.0s): no 'loop: iteration 1 — SHIP' "
        "commit touching mailbox/ is an ancestor of HEAD"
    ) in _log(mailbox)
    assert not (mailbox / ".lock").exists()

    # Resume: finalization is rechecked first; no Lead/Evaluator rerun.
    _retire(tmp_path, mailbox)
    resumed = FakeRunner([], expected_roles=[])
    assert trio_loop.run_loop(mailbox, 1, resumed, repo=tmp_path) == 0
    assert resumed.calls == []
    assert "status: shipped" in state_text(mailbox)


def test_interrupted_wait_resumes_to_finalization(
    tmp_path: Path, waiting
) -> None:
    """A driver killed mid-wait leaves resumable needs_retirement state."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)

    def interrupt() -> None:
        raise KeyboardInterrupt

    fake.hooks[1] = interrupt
    runner = FakeRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])
    with pytest.raises(KeyboardInterrupt):
        trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path)
    text = state_text(mailbox)
    assert "status: needs_retirement" in text
    assert "phase: ship-awaiting-retirement" in text
    assert not (mailbox / ".lock").exists()

    # Resume waits again; retirement lands on its first poll.
    fake.hooks.clear()
    fake.sleeps.clear()
    fake.hooks[1] = lambda: _retire(tmp_path, mailbox)
    resumed = FakeRunner([], expected_roles=[])
    assert trio_loop.run_loop(mailbox, 1, resumed, repo=tmp_path) == 0
    assert resumed.calls == []
    assert "status: shipped" in state_text(mailbox)
    assert "resume: SHIP retirement verified" in _log(mailbox)


def test_stale_attempt_is_rejected_without_waiting(
    tmp_path: Path, waiting
) -> None:
    """A SHIP for another attempt never finalizes, even if retired."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    pin = _git_head(tmp_path)
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: needs_retirement\n"
        "phase: ship-pending-retirement\n"
        f"evaluator_attempt: {'a' * 32}\n"
        f"evaluated_sha: {pin}\n",
        encoding="utf-8",
    )
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: SHIP\n# Verdict — iteration 1\n"
        f"attempt: {'b' * 32}\nevaluated: {pin}\n",
        encoding="utf-8",
    )
    _retire(tmp_path, mailbox)
    runner = FakeRunner([], expected_roles=[])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert runner.calls == []
    assert fake.sleeps == []
    assert "status: needs_retirement" in state_text(mailbox)
    assert (
        f"resume: SHIP not accepted (retirement cannot complete): "
        f"VERDICT.md does not record attempt: {'a' * 32}"
    ) in _log(mailbox)


def test_stale_attempt_on_first_pass_does_not_wait(
    tmp_path: Path, waiting
) -> None:
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    runner = FakeRunner(
        ["VERDICT: SHIP\n# Verdict — iteration 1\nattempt: stale\n"],
        inject_lockstep=False,
    )
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert fake.sleeps == []
    assert runner.calls == [("lead", 1), ("evaluator", 1)]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("commit", "product paths committed after pin"),
        ("dirty", "product paths modified in worktree: seed.txt"),
        ("untracked", "untracked product paths: extra.py"),
    ],
)
def test_product_change_during_wait_stops_waiting(
    tmp_path: Path, waiting, change: str, expected: str
) -> None:
    """The strict product guard wins even if retirement lands later."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)

    def mutate() -> None:
        if change == "commit":
            _commit_relative(tmp_path, "app.py", "x\n", "later merge")
            _retire(tmp_path, mailbox)
        elif change == "dirty":
            (tmp_path / "seed.txt").write_text("edited\n", encoding="utf-8")
        else:
            (tmp_path / "extra.py").write_text("x\n", encoding="utf-8")

    fake.hooks[1] = mutate
    runner = FakeRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert len(fake.sleeps) == 1
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    text = state_text(mailbox)
    assert "status: needs_retirement" in text
    assert "status: shipped" not in text
    log = _log(mailbox)
    assert "SHIP not accepted (retirement cannot complete): " in log
    assert expected in log


def test_wait_disabled_rejects_immediately(
    tmp_path: Path, waiting, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, "0")
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    runner = FakeRunner(["VERDICT: SHIP\n# Verdict — iteration 1\n"])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert fake.sleeps == []
    assert "after waiting 0.0s" in _log(mailbox)


def test_wait_settings_default_and_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(trio_loop.RETIREMENT_WAIT_ENV, raising=False)
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, "nope")
    assert trio_loop._retirement_wait_settings() == (
        trio_loop.DEFAULT_RETIREMENT_WAIT_SECONDS,
        trio_loop.DEFAULT_RETIREMENT_POLL_SECONDS,
    )


def _rewrite_verdict_first_line(mailbox: Path, first: str) -> None:
    """Replace the verdict line, keeping attempt/evaluated metadata."""
    lines = (mailbox / "VERDICT.md").read_text(encoding="utf-8").splitlines()
    lines[0] = first
    (mailbox / "VERDICT.md").write_text("\n".join(lines) + "\n", "utf-8")


@pytest.mark.parametrize(
    ("first", "found"),
    [
        ("VERDICT: ITERATE", "(VERDICT: ITERATE)"),
        ("VERDICT: ITERATE scope=local:app.py", "(VERDICT: ITERATE scope=local:app.py)"),
        ("VERDICT: BLOCKED", "(VERDICT: BLOCKED)"),
        ("VERDICT: SHIPPED maybe", "(missing or unparseable verdict)"),
        ("", "(missing or unparseable verdict)"),
    ],
)
def test_retracted_verdict_during_wait_never_ships(
    tmp_path: Path, waiting, first: str, found: str
) -> None:
    """Retraction + SHIP-titled mailbox commit stops at once, no ship."""
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)

    def retract_and_retire() -> None:
        if first:
            _rewrite_verdict_first_line(mailbox, first)
        else:
            (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
        _retire(tmp_path, mailbox)

    fake.hooks[1] = retract_and_retire
    runner = FakeRunner([SHIP_TEXT])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert len(fake.sleeps) == 1
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    text = state_text(mailbox)
    assert "status: needs_retirement" in text
    assert "status: shipped" not in text
    assert (
        "SHIP not accepted (retirement cannot complete): VERDICT.md first "
        f"line is no longer VERDICT: SHIP {found}"
    ) in _log(mailbox)


def test_retraction_without_commit_stops_immediately(
    tmp_path: Path, waiting
) -> None:
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    fake.hooks[1] = lambda: _rewrite_verdict_first_line(
        mailbox, "VERDICT: ITERATE"
    )
    runner = FakeRunner([SHIP_TEXT])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert fake.sleeps == [2.0]


def test_resume_after_retraction_rejects_without_dispatch(
    tmp_path: Path, waiting
) -> None:
    """needs_retirement resume re-parses VERDICT.md before shipping."""
    mailbox = _repo_with_mailbox(tmp_path)
    waiting(mailbox)
    runner = FakeRunner([SHIP_TEXT])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    _rewrite_verdict_first_line(mailbox, "VERDICT: ITERATE")
    _retire(tmp_path, mailbox)  # commit carries ITERATE under SHIP title

    fake = waiting(mailbox)
    resumed = FakeRunner([], expected_roles=[])
    assert trio_loop.run_loop(mailbox, 1, resumed, repo=tmp_path) == 6
    assert resumed.calls == []
    assert fake.sleeps == []
    assert "status: needs_retirement" in state_text(mailbox)
    assert "resume: SHIP not accepted (retirement cannot complete)" in (
        _log(mailbox)
    )


@pytest.mark.parametrize(
    "raw", ["inf", "Infinity", "+inf", "1e309", "nan", "-inf", "-1", "x"]
)
def test_non_finite_or_negative_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, raw)
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, raw)
    assert trio_loop._retirement_wait_settings() == (
        trio_loop.DEFAULT_RETIREMENT_WAIT_SECONDS,
        trio_loop.DEFAULT_RETIREMENT_POLL_SECONDS,
    )


def test_huge_finite_wait_is_capped(
    tmp_path: Path, waiting, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, "1e300")
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, "1e300")
    wait, _poll = trio_loop._retirement_wait_settings()
    assert wait == trio_loop.MAX_RETIREMENT_WAIT_SECONDS
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    # waiting() reset the env; restore the huge values for the run.
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, "1e300")
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, "1e300")
    runner = FakeRunner([SHIP_TEXT])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert sum(fake.sleeps) == trio_loop.MAX_RETIREMENT_WAIT_SECONDS
    assert len(fake.sleeps) == 1


def test_driver_json_phase_matches_state_during_wait(
    tmp_path: Path, waiting
) -> None:
    mailbox = _repo_with_mailbox(tmp_path)
    fake = waiting(mailbox)
    seen: dict[str, str] = {}

    def observe() -> None:
        driver = json.loads(
            (mailbox / ".driver.json").read_text(encoding="utf-8")
        )
        seen["driver"] = driver["phase"]
        seen["state"] = trio_loop._read_state(mailbox / "STATE.md")["phase"]
        _retire(tmp_path, mailbox)

    fake.hooks[1] = observe
    runner = FakeRunner([SHIP_TEXT])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 0
    assert seen == {
        "driver": "ship-awaiting-retirement",
        "state": "ship-awaiting-retirement",
    }
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    final = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert final["phase"] == "shipped"

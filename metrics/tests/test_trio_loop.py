"""Behavioral tests for the stdlib Trio loop state machine."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from metrics import trio_loop


PLAN = """\
```yaml
slices:
  - id: coordination
    writes: [loop/STATE.md, "api:Coordination"]
    reads: []
```
"""


def make_mailbox(parent: Path, plan: str = PLAN) -> Path:
    """Create the smallest mailbox needed by the loop and shadow gate."""
    mailbox = parent / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\n"
        "status: ready\n"
        "phase: idle\n"
        "custom: preserve\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    return mailbox


class FakeRunner:
    """Role runner that records calls and writes evaluator verdicts."""

    def __init__(
        self,
        verdicts: list[str],
        *,
        expected_roles: list[str] | None = None,
        add_log: bool = True,
    ) -> None:
        self.verdicts = list(verdicts)
        self.expected_roles = expected_roles
        self.add_log = add_log
        self.calls: list[tuple[str, int]] = []

    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        """Record a role and emulate its mailbox writes."""
        self.calls.append((role, iteration))
        if self.expected_roles is not None:
            expected = self.expected_roles.pop(0)
            assert role == expected
        if role in {"lead", "repair"} and self.add_log:
            with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write(f"- iter {iteration} | {role} | completed\n")
        if role == "evaluator":
            assert self.verdicts
            (mailbox / "VERDICT.md").write_text(
                self.verdicts.pop(0) + "\n",
                encoding="utf-8",
            )
        return 0


class EvaluatorCrashRunner(FakeRunner):
    """Fake runner that dies after the Lead transition is persisted."""

    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        if role == "evaluator":
            self.calls.append((role, iteration))
            raise RuntimeError("evaluator crashed")
        return super().run(role, iteration, mailbox)


class LeadCrashRunner(FakeRunner):
    """Fake runner that dies while the Lead phase is in flight."""

    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        if role == "lead":
            self.calls.append((role, iteration))
            raise RuntimeError("lead crashed")
        return super().run(role, iteration, mailbox)


def state_text(mailbox: Path) -> str:
    """Read state for assertions without depending on the metrics parser."""
    return (mailbox / "STATE.md").read_text(encoding="utf-8")


def init_git(path: Path) -> None:
    """Create a git repository without creating any slice commits."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_plain_iterate_runs_a_new_lead_pass(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: ITERATE", "VERDICT: SHIP"])

    result = trio_loop.run_loop(mailbox, 2, runner)

    assert result == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("lead", 2),
        ("evaluator", 2),
    ]
    assert "iteration: 2" in state_text(mailbox)
    assert "status: shipped" in state_text(mailbox)


def test_plain_iterate_hits_the_iteration_cap(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: ITERATE"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 4
    assert runner.calls == [("lead", 1), ("evaluator", 1)]


def test_local_repairs_are_capped_then_force_a_lead(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(
        [
            "VERDICT: ITERATE scope=local:a.py",
            "VERDICT: ITERATE scope=local:b.py",
            "VERDICT: ITERATE scope=local:c.py",
            "VERDICT: SHIP",
        ]
    )

    result = trio_loop.run_loop(mailbox, 4, runner)

    assert result == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("repair", 2),
        ("evaluator", 2),
        ("repair", 3),
        ("evaluator", 3),
        ("lead", 4),
        ("evaluator", 4),
    ]
    assert (mailbox / ".repairs").read_text(encoding="utf-8").strip() == "0"
    assert "repair cap" in (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_design_scope_resets_repairs_for_a_full_lead(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(
        [
            "VERDICT: ITERATE scope=local:a.py",
            "VERDICT: ITERATE scope=design",
            "VERDICT: SHIP",
        ]
    )

    assert trio_loop.run_loop(mailbox, 3, runner) == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("repair", 2),
        ("evaluator", 2),
        ("lead", 3),
        ("evaluator", 3),
    ]


@pytest.mark.parametrize(
    ("verdict", "expected_code", "expected_status"),
    [
        ("# VERDICT: SHIP", 0, "shipped"),
        ("verdict: iterate", 4, "running"),
        ("VERDICT: ITERATE scope=design", 4, "running"),
        ("VERDICT: ITERATE scope=local:a.py", 4, "running"),
        ("VERDICT: SHIP scope=local:x", 3, "error"),
    ],
)
def test_verdict_grammar_and_outcomes(
    tmp_path: Path,
    verdict: str,
    expected_code: int,
    expected_status: str,
) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner([verdict])

    assert trio_loop.run_loop(mailbox, 1, runner) == expected_code
    assert f"status: {expected_status}" in state_text(mailbox)


@pytest.mark.parametrize(
    ("verdict", "expected_code", "expected_status"),
    [
        ("VERDICT: BLOCKED", 2, "blocked"),
        ("VERDICT: NEEDS_HUMAN", 5, "needs_human"),
    ],
)
def test_terminal_non_ship_verdicts(
    tmp_path: Path,
    verdict: str,
    expected_code: int,
    expected_status: str,
) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner([verdict])

    assert trio_loop.run_loop(mailbox, 1, runner) == expected_code
    assert f"status: {expected_status}" in state_text(mailbox)


def test_gate_failure_retries_the_same_lead_once(tmp_path: Path) -> None:
    mailbox = make_mailbox(
        tmp_path,
        PLAN.replace("loop/STATE.md, \"api:Coordination\"", "app.py"),
    )
    init_git(mailbox)
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 3
    assert runner.calls == [("lead", 1), ("lead", 1)]
    assert "status: error" in state_text(mailbox)
    assert "gate breach" in (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_log_gate_failure_retries_the_same_lead_once(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"], add_log=False)

    assert trio_loop.run_loop(mailbox, 1, runner) == 3
    assert runner.calls == [("lead", 1), ("lead", 1)]
    assert "status: error" in state_text(mailbox)


def test_crash_between_roles_resumes_at_evaluator(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    crashing = EvaluatorCrashRunner([])

    with pytest.raises(RuntimeError, match="evaluator crashed"):
        trio_loop.run_loop(mailbox, 1, crashing)

    assert crashing.calls == [("lead", 1), ("evaluator", 1)]
    assert "iteration: 1" in state_text(mailbox)
    assert "phase: lead-done" in state_text(mailbox)

    resumed = FakeRunner(["VERDICT: SHIP"], expected_roles=["evaluator"])
    assert trio_loop.run_loop(mailbox, 1, resumed) == 0
    assert resumed.calls == [("evaluator", 1)]
    assert "iteration: 1" in state_text(mailbox)


def test_crash_during_role_resumes_same_role_and_iteration(
    tmp_path: Path,
) -> None:
    mailbox = make_mailbox(tmp_path)
    crashing = LeadCrashRunner([])

    with pytest.raises(RuntimeError, match="lead crashed"):
        trio_loop.run_loop(mailbox, 1, crashing)

    assert "iteration: 1" in state_text(mailbox)
    assert "phase: lead-running" in state_text(mailbox)

    resumed = FakeRunner(
        ["VERDICT: SHIP"],
        expected_roles=["lead", "evaluator"],
    )
    assert trio_loop.run_loop(mailbox, 1, resumed) == 0
    assert resumed.calls == [("lead", 1), ("evaluator", 1)]


def test_live_lock_returns_five(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    lock = mailbox / ".lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 5
    assert runner.calls == []
    assert lock.is_dir()


def test_stale_lock_is_retaken_and_released(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    lock = mailbox / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("999999999\n", encoding="utf-8")
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    assert not lock.exists()


def test_missing_state_is_initialized_and_unknown_lines_survive(
    tmp_path: Path,
) -> None:
    mailbox = make_mailbox(tmp_path)
    (mailbox / "STATE.md").unlink()
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    text = state_text(mailbox)
    assert "iteration: 1" in text
    assert "status: shipped" in text
    assert "phase: shipped" in text

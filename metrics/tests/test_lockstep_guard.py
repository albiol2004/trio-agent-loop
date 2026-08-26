"""Hermetic regression guard: lockstep run_loop must stay byte-identical.

This file must pass in a bare checkout with NO loop*/ directories -- it
builds its own temp mailbox and reads only the fixture below and the
metrics package. It replays the scripted scenario recorded in
metrics/tests/fixtures/lockstep_run_guard.json (captured from HEAD's
run_loop before open-loop mode existed) and compares the STATE.md/LOG.md/
.repairs/.driver.json outcome EXACTLY, so a drift in lockstep behaviour
introduced while adding open-loop mode fails this test.
"""
from __future__ import annotations

import json
from pathlib import Path

from metrics import trio_loop

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "lockstep_run_guard.json"


class FakeRunner:
    """Legacy 3-argument role runner: the exact call shape lockstep uses."""

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[list] = []

    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        self.calls.append([role, iteration])
        if role in {"lead", "repair"}:
            with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write(f"- iter {iteration} | {role} | completed\n")
        if role == "evaluator":
            assert self.verdicts
            (mailbox / "VERDICT.md").write_text(
                self.verdicts.pop(0) + "\n", encoding="utf-8"
            )
        return 0


def make_mailbox(parent: Path, fixture: dict) -> Path:
    mailbox = parent / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(fixture["initial_state"], encoding="utf-8")
    (mailbox / "PLAN.md").write_text(fixture["plan"], encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    return mailbox


def test_lockstep_scenario_matches_recorded_fixture(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    mailbox = make_mailbox(tmp_path, fixture)
    # No QUEUE.md anywhere in this mailbox: mode="auto" must take the
    # lockstep path, exactly like every mailbox at HEAD before QUEUE.md
    # existed.
    assert not (mailbox / "QUEUE.md").exists()

    runner = FakeRunner(fixture["verdicts"])
    code = trio_loop.run_loop(mailbox, fixture["max_iterations"], runner)

    assert code == fixture["expected_exit_code"]
    assert runner.calls == fixture["expected_calls"]

    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    for line in fixture["expected_state_lines"]:
        assert line in state_text, f"missing STATE.md line: {line!r}\n---\n{state_text}"

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    log_lines = [ln for ln in log_text.splitlines() if ln.strip()]
    assert log_lines == fixture["expected_log_lines"]

    repairs_text = (mailbox / ".repairs").read_text(encoding="utf-8").strip()
    assert repairs_text == fixture["expected_repairs"]

    driver_payload = json.loads(
        (mailbox / ".driver.json").read_text(encoding="utf-8")
    )
    driver_payload.pop("pid", None)
    assert driver_payload == fixture["expected_driver_json"]

    assert (mailbox / ".session.json").exists() == fixture[
        "expected_session_json_exists"
    ]

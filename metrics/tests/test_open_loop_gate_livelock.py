"""ol-livelock: a retired slice whose per-slice commit gate keeps failing
must not spin the open loop silently forever.

Seen in a live Terminal-Bench smoke (opencode driver, open-loop +
acceptance): every slice was retired and the Lead was done, but the per-slice
commit gate rejected one slice for good ("acceptance/freeze ordering"). The
poll loop re-ran the gate every poll, appended `commit gate failed for slice
X; skipping until re-retired` to LOG.md each time (hundreds of lines), and
waited for a retirement that could never come: STATE stayed `phase: idle,
status: running`, no process was alive, the integration eval was never
dispatched, and the run (which has no time limit) hung forever.

Everything here is fake runners + a stubbed gate, plus one test that runs the
REAL gate against a real git repo shaped like the failing run.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests.test_open_loop_driver import (
    PLAN_ONE_SLICE,
    QueueModel,
    ScriptedEvalRunner,
    VerdictModel,
    fake_sha,
    make_open_loop_mailbox,
)

#: A stubbed gate polled this often is a livelock: fail loudly instead of
#: letting a broken loop spin the test process forever.
SPIN_LIMIT = 600


class GateLeadRunner:
    """Fake Lead: pass 1 retires the slice; every later pass is a gate
    hand-back whose context must carry the gate text (`gate_errors`)."""

    def __init__(self, first, repairs=()) -> None:
        self.first = first
        self.repairs = list(repairs)
        self.calls: list[dict] = []

    def run(self, role, iteration, mailbox, context=None):
        assert role == "lead"
        self.calls.append({"iteration": iteration, "context": dict(context)})
        if len(self.calls) == 1:
            self.first(mailbox)
            return 0
        assert self.repairs, "lead re-invoked more often than the test scripted"
        self.repairs.pop(0)(mailbox)
        return 0


def _stuck_gate(counter: dict, reason: str = ""):
    def gate(mailbox, repo, slice_id):
        counter["n"] += 1
        if counter["n"] > SPIN_LIMIT:
            raise AssertionError(f"livelock: the commit gate was polled {counter['n']} times")
        return 1

    return gate


def _setup(tmp_path: Path):
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    return mailbox, QueueModel(mailbox, lock), VerdictModel(mailbox, lock)


def _touch_plan(mailbox: Path) -> None:
    with open(mailbox / "PLAN.md", "a", encoding="utf-8") as fh:
        fh.write("\n<!-- lead looked at the gate failure -->\n")


def _log(mailbox: Path) -> str:
    return (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_gate_that_never_passes_hands_back_to_the_lead_then_stops_with_error(
    tmp_path: Path, monkeypatch
) -> None:
    mailbox, queue, _verdict = _setup(tmp_path)
    sha = fake_sha("stuck-gate")
    counter = {"n": 0}
    monkeypatch.setattr(trio_loop, "_per_slice_gate", _stuck_gate(counter))
    lead = GateLeadRunner(
        lambda mb: queue.retire("solo", sha),
        repairs=[_touch_plan, _touch_plan],
    )
    evaluator = ScriptedEvalRunner()

    code = trio_loop.run_open_loop(mailbox, 9, lead, evaluator, poll_seconds=0.001)

    assert code == 3
    assert "status: error" in (mailbox / "STATE.md").read_text(encoding="utf-8")
    # never graded: neither a slice-eval nor the integration eval was dispatched
    assert evaluator.calls == []
    # the Lead was told, in its context, which gate failed for which slice
    handbacks = lead.calls[1:]
    assert handbacks, "the stuck slice was never handed back to the Lead"
    for call in handbacks:
        errors = call["context"].get("gate_errors")
        assert errors and any("solo" in e for e in errors), call["context"]
    log = _log(mailbox)
    # it stopped LOUDLY, naming the slice
    assert "commit gate keeps failing for slice solo" in log, log
    # ... and the LOG is not flooded: one failure line, not one per poll
    assert log.count("commit gate failed for slice solo") == 1, log


def test_lead_that_repairs_the_gate_failure_ships(tmp_path: Path, monkeypatch) -> None:
    mailbox, queue, verdict = _setup(tmp_path)
    sha = fake_sha("repaired-gate")
    fixed = threading.Event()
    counter = {"n": 0}

    def gate(mb, repo, slice_id):
        counter["n"] += 1
        if counter["n"] > SPIN_LIMIT:
            raise AssertionError("livelock")
        return 0 if fixed.is_set() else 1

    monkeypatch.setattr(trio_loop, "_per_slice_gate", gate)
    lead = GateLeadRunner(
        lambda mb: queue.retire("solo", sha),
        repairs=[lambda mb: (fixed.set(), _touch_plan(mb))],
    )
    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha): lambda mb: verdict.append_slice_section("solo", sha, "SHIP")},
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )

    code = trio_loop.run_open_loop(mailbox, 9, lead, evaluator, poll_seconds=0.001)

    assert code == 0
    assert len(lead.calls) == 2 and lead.calls[1]["context"].get("gate_errors")
    assert [c["context"]["kind"] for c in evaluator.calls] == ["slice-eval", "integration-eval"]


def test_no_progress_detector_stops_an_idle_loop_with_nothing_alive(
    tmp_path: Path, monkeypatch
) -> None:
    """The detector is independent of the gate hand-back: with the hand-back
    disabled the same stuck gate leaves the loop idle (Lead done, nothing in
    flight, nothing changing) and the loop must still end, loudly."""
    mailbox, queue, _verdict = _setup(tmp_path)
    sha = fake_sha("idle-forever")
    counter = {"n": 0}
    monkeypatch.setattr(trio_loop, "_per_slice_gate", _stuck_gate(counter))
    lead = GateLeadRunner(lambda mb: queue.retire("solo", sha))

    code = trio_loop.run_open_loop(
        mailbox, 9, lead, ScriptedEvalRunner(), poll_seconds=0.001,
        max_gate_repair_rounds=None, no_progress_polls=15,
    )

    assert code == 3
    assert len(lead.calls) == 1
    assert "status: error" in (mailbox / "STATE.md").read_text(encoding="utf-8")
    log = _log(mailbox)
    assert "no progress" in log and "solo" in log, log
    assert counter["n"] < SPIN_LIMIT


def test_transient_gate_failure_still_waits_without_a_lead_handback(
    tmp_path: Path, monkeypatch
) -> None:
    """A gate that fails a couple of polls and then passes (commits arriving)
    must keep the old behaviour: wait, no Lead hand-back, ship."""
    mailbox, queue, verdict = _setup(tmp_path)
    sha = fake_sha("transient-gate")
    counter = {"n": 0}

    def gate(mb, repo, slice_id):
        counter["n"] += 1
        return 1 if counter["n"] <= 2 else 0

    monkeypatch.setattr(trio_loop, "_per_slice_gate", gate)
    lead = GateLeadRunner(lambda mb: queue.retire("solo", sha))
    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha): lambda mb: verdict.append_slice_section("solo", sha, "SHIP")},
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(mailbox, 9, lead, evaluator, poll_seconds=0.001)
    assert code == 0
    assert len(lead.calls) == 1


class TestNoProgressWatch:
    def test_counts_only_unchanged_idle_observations(self) -> None:
        w = trio_loop._NoProgressWatch()
        assert w.observe("a", idle=True) == 1
        assert w.observe("a", idle=True) == 2
        assert w.observe("b", idle=True) == 1          # state changed
        assert w.observe("b", idle=False) == 0         # live work resets
        assert w.observe("b", idle=True) == 1

    def test_reset(self) -> None:
        w = trio_loop._NoProgressWatch()
        w.observe("a", idle=True)
        w.reset()
        assert w.observe("a", idle=True) == 1


# --- the REAL gate, on the git shape of the failing run -------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_real_gate_failure_names_its_cause(tmp_path: Path) -> None:
    """A slice branch cut BEFORE the acceptance freeze and merged after it:
    the shared gate (trio-shadow) rejects it as freeze-ordering, and
    `_per_slice_gate` now carries that text so LOG.md and the Lead can name
    the cause instead of a bare `commit gate failed`."""
    from metrics.tests.test_r19_shadow_acceptance_gate import setup

    repo, mb = setup(tmp_path)
    base = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")             # drop the clean slice commit
    _git(repo, "checkout", "-q", "-b", "builder", base)       # branch cut pre-freeze
    (repo / "app.py").write_text("v1\n")
    _git(repo, "commit", "-qam", "slice(cli): impl")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "--no-edit", "-m", "merge slice cli", "builder")

    code = trio_loop._per_slice_gate(mb, repo, "cli")

    assert code == 1
    assert "freeze ordering" in getattr(code, "reason", ""), getattr(code, "reason", None)

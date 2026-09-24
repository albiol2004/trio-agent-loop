"""Offline regression T1: a role timeout after a confirmed first prompt.

Eval 44238aa (original G1c, row_lag=35): the first prompt landed (full
user row at +35 s), the fake Lead never wrote LOG.md, and the role
timed out. STATE stayed ``running``/``lead-running``, the loop wrapper's
best-effort DELETE could orphan a still-active pane, and a resume
created a second Lead for the same iteration.

Delivery confirmed is not role completed. Once the prompt was accepted,
any failure before this pass's artifact is known (role timeout, session
wait timeout, broker loss) leaves the pane possibly still working, so
the run holds it with the existing held-dispatch record: no re-post, no
DELETE, STATE ``needs_human``, and every later dispatch refused until a
person reconciles it. Completed roles, a fresh artifact that lands at
the deadline, and an operator cancel (Ctrl-C) are unchanged.

Uses the 0.14 source-faithful broker from test_first_prompt_redelivery.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
TREE = HERE.parents[1]
sys.path.insert(0, str(HERE))
import test_first_prompt_redelivery as h  # noqa: E402
from test_first_prompt_redelivery import clock  # noqa: E402,F401

trioctl = h.trioctl
ATTEMPT, PIN, SID = h.ATTEMPT, h.PIN, h.SID
OTHER_PIN = "f" * 40


class RoleBroker(h.Native014Broker):
    """Native014Broker whose pane writes a role artifact ``artifact_at``
    seconds into the first turn, and can lose the broker (or be
    cancelled) ``fail_at`` seconds after create."""

    def __init__(self, clock, *, mailbox=None, artifact=None,
                 artifact_at=None, fail_at=None, fail_exc=None, **kw):
        super().__init__(clock, **kw)
        self.role_mailbox = mailbox
        self.artifact = artifact
        self.artifact_at = artifact_at
        self.fail_at = fail_at
        self.fail_exc = fail_exc
        self.first_start: float | None = None

    def _start_turn(self, text, t):
        if self.first_start is None:
            self.first_start = t
        super()._start_turn(text, t)

    def _advance(self):
        super()._advance()
        if (
            self.artifact is not None
            and self.first_start is not None
            and self.sim_t >= self.first_start + self.artifact_at
        ):
            name, line = self.artifact
            path = self.role_mailbox / name
            path.write_text(path.read_text("utf-8") + line, "utf-8")
            self.artifact = None

    def get_session(self, session_id):
        if self.fail_at is not None and self.clock.now >= self.fail_at:
            raise self.fail_exc
        return super().get_session(session_id)


def _mailbox(tmp_path: Path, state: str | None = None) -> Path:
    mailbox = tmp_path / "loop"
    mailbox.mkdir(parents=True)
    (mailbox / "STATE.md").write_text(
        state or "iteration: 0\nstatus: ready\nphase: idle\n", "utf-8"
    )
    (mailbox / "LOG.md").write_text("# Trio loop log\n", "utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", "utf-8")
    return mailbox


def _counting(broker):
    creates: list[str] = []
    original = broker.create_session
    broker.create_session = lambda *a, **k: (
        creates.append(a[3]), original(*a, **k)
    )[1]
    return creates


def _runner_factory(tmp_path, broker, monkeypatch, timeout=300.0):
    """`OmnigentRunner` stand-in for command_loop (built before patching)."""
    real = trioctl.OmnigentRunner

    def make(**kwargs):
        monkeypatch.setattr(trioctl, "OmnigentRunner", real)
        runner = h._runner(tmp_path, broker, monkeypatch, timeout=timeout)
        monkeypatch.setattr(trioctl, "OmnigentRunner", make)
        return runner
    return make


def _record(mailbox: Path) -> dict:
    records = trioctl._held_records(mailbox)
    assert len(records) == 1, records
    return records[0][1]


def _assert_resume_refused(tmp_path, mailbox, monkeypatch, clock, core):
    """Two independent driver invocations: no create, no POST."""
    state = (mailbox / "STATE.md").read_text("utf-8")
    for _ in range(2):
        broker = h.Native014Broker(clock, row_lag=5.0)
        creates = _counting(broker)
        runner = h._runner(tmp_path, broker, monkeypatch, timeout=60.0)
        assert core.run_loop(mailbox, 3, runner, repo=None) == 5
        assert creates == [] and broker.posts == []
        assert broker.deletes == []
    assert (mailbox / "STATE.md").read_text("utf-8") == state


# -- T1 repro: Lead delivered, never writes LOG.md ----------------------

@pytest.mark.parametrize(
    "shape",
    [
        # Original G1c: row +35 s; pane idles, no LOG line (artifact wait).
        {"row_lag": 35.0, "turn_len": 100.0},
        # Same, but the pane is still working at the deadline (session wait).
        {"row_lag": 35.0, "turn_len": 400.0},
    ],
    ids=["artifact-wait", "session-wait"],
)
def test_lead_timeout_after_delivery_holds_and_blocks_resume(
    tmp_path, monkeypatch, clock, shape
):
    core = trioctl._load_trio_loop(TREE)
    mailbox = _mailbox(tmp_path)
    broker = h.Native014Broker(clock, **shape)
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)

    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        core.run_loop(mailbox, 3, runner, repo=None)

    assert clock.now < 400.0  # bounded by the role timeout
    assert len(broker.posts) == 1 and broker.deletes == []
    assert broker.turns == [h.PROMPT]  # delivered exactly once
    assert runner.held_session_ids == [SID]
    record = _record(mailbox)
    assert (record["session_id"], record["role"], record["iteration"]) == (
        SID, "lead", 1
    )
    assert record["hold"] == "role_completion_uncertain"
    assert "timed out" in record["reason"]
    state = (mailbox / "STATE.md").read_text("utf-8")
    assert "status: needs_human" in state and "running" not in state
    assert f"held lead session {SID}" in (mailbox / "LOG.md").read_text()

    _assert_resume_refused(tmp_path, mailbox, monkeypatch, clock, core)

    # STATE hand-reset but the record kept: the runner still refuses.
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n", "utf-8"
    )
    broker3 = h.Native014Broker(clock, row_lag=5.0)
    creates3 = _counting(broker3)
    runner3 = h._runner(tmp_path, broker3, monkeypatch, timeout=60.0)
    with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
        core.run_loop(mailbox, 3, runner3, repo=None)
    assert creates3 == [] and broker3.posts == []


def test_command_loop_keeps_held_pane_across_two_invocations(
    tmp_path, monkeypatch, clock, capsys
):
    """The real `loop` wrapper: the held session is not pruned (DELETE),
    and a second invocation exits HELD_DISPATCH_EXIT with no create."""
    core = trioctl._load_trio_loop(TREE)
    mailbox = _mailbox(tmp_path / "repo")
    brokers: list[h.Native014Broker] = []
    pruned: list[list[str]] = []

    real = trioctl.OmnigentRunner

    def make_runner(**kwargs):
        broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
        brokers.append(broker)
        runner = real(
            repo=tmp_path, broker_client=broker, interval=1, timeout=300.0
        )
        runner._agent_id = lambda role: "lead-agent"
        runner._resolve_model = lambda role: "m"
        runner._prompt = lambda *a, **k: h.PROMPT
        return runner

    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: core)
    monkeypatch.setattr(trioctl, "OmnigentRunner", make_runner)
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "3"]
    )

    with pytest.raises(trioctl.TrioctlError, match="held dispatch"):
        args.func(args)
    assert pruned == [[]]  # s1 kept out of the best-effort DELETE
    assert f"kept session {SID}" in capsys.readouterr().err

    for _ in range(2):
        assert args.func(args) == trioctl.HELD_DISPATCH_EXIT
    assert len(brokers) == 1  # no runner, no create, no POST after hold
    assert len(brokers[0].posts) == 1 and brokers[0].deletes == []
    assert pruned == [[]]


# -- Evaluator and scoped repair ----------------------------------------

EVAL_STATE = (
    "iteration: 1\nstatus: running\nphase: lead-done\n"
    f"evaluated_sha: {PIN}\nevaluator_attempt: {ATTEMPT}\n"
)


def test_evaluator_timeout_after_delivery_holds_pin_and_attempt(
    tmp_path, monkeypatch, clock
):
    core = trioctl._load_trio_loop(TREE)
    mailbox = _mailbox(tmp_path, EVAL_STATE)
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)

    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        core.run_loop(mailbox, 3, runner, repo=None)

    assert len(broker.posts) == 1 and broker.deletes == []
    record = _record(mailbox)
    assert (record["role"], record["iteration"]) == ("evaluator", 1)
    assert (record["attempt"], record["pinned_sha"]) == (ATTEMPT, PIN)
    state = (mailbox / "STATE.md").read_text("utf-8")
    assert "status: needs_human" in state
    assert f"evaluated_sha: {PIN}" in state  # pin/attempt kept for reconcile
    assert f"evaluator_attempt: {ATTEMPT}" in state

    _assert_resume_refused(tmp_path, mailbox, monkeypatch, clock, core)

    # The held pane finishes late, grading a different tree. Even with
    # STATE hand-reset to lead-done, that verdict is not this attempt's
    # pinned evidence: nothing ships and no Evaluator is dispatched.
    (mailbox / "VERDICT.md").write_text(
        f"VERDICT: SHIP\niteration: 1\nattempt: {ATTEMPT}\n"
        f"evaluated: {OTHER_PIN}\ncommit: {OTHER_PIN}\n", "utf-8"
    )
    (mailbox / "STATE.md").write_text(EVAL_STATE, "utf-8")
    late = h.Native014Broker(clock, row_lag=5.0)
    creates = _counting(late)
    resume = h._runner(tmp_path, late, monkeypatch, timeout=60.0)
    with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
        core.run_loop(mailbox, 3, resume, repo=None)
    assert creates == []
    assert "shipped" not in (mailbox / "STATE.md").read_text("utf-8")


def test_repair_timeout_after_delivery_holds(tmp_path, monkeypatch, clock):
    core = trioctl._load_trio_loop(TREE)
    mailbox = _mailbox(
        tmp_path, "iteration: 1\nstatus: running\nphase: repair-running\n"
    )
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=400.0)
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)

    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        core.run_loop(mailbox, 3, runner, repo=None)

    assert len(broker.posts) == 1 and broker.deletes == []
    assert (_record(mailbox)["role"], _record(mailbox)["iteration"]) == (
        "repair", 1
    )
    _assert_resume_refused(tmp_path, mailbox, monkeypatch, clock, core)


# -- broker loss after delivery ----------------------------------------

def test_broker_loss_after_delivery_holds_not_deletes(
    tmp_path, monkeypatch, clock
):
    mailbox = _mailbox(tmp_path)
    broker = RoleBroker(
        clock, row_lag=35.0, turn_len=400.0, fail_at=60.0,
        fail_exc=h.BrokerHttpError(
            "GET /v1/sessions/s1 failed: connection refused"
        ),
    )
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)

    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        runner.run("lead", 1, mailbox)

    assert broker.deletes == [] and len(broker.posts) == 1
    record = _record(mailbox)
    assert record["role"] == "lead" and "connection refused" in record["reason"]
    assert "status: needs_human" in (mailbox / "STATE.md").read_text()


# -- unchanged: completed roles, deadline race, cancel, definite miss ---

def test_completed_lead_is_not_held_and_is_pruned(
    tmp_path, monkeypatch, clock
):
    mailbox = _mailbox(tmp_path / "repo")
    pruned: list[list[str]] = []
    broker = RoleBroker(
        clock, mailbox=mailbox, row_lag=35.0, turn_len=100.0,
        artifact=("LOG.md", "- iter 1 | lead | slice done\n"),
        artifact_at=80.0,
    )

    class LeadLoop:
        @staticmethod
        def run_loop(mailbox_path, max_iterations, runner, **kwargs):
            return runner.run("lead", 1, mailbox_path)

    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: LeadLoop)
    monkeypatch.setattr(
        trioctl, "OmnigentRunner",
        _runner_factory(tmp_path, broker, monkeypatch),
    )
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "1"]
    )
    assert args.func(args) == 0
    assert trioctl._held_records(mailbox) == []
    assert [set(ids) for ids in pruned] == [{SID}]  # clean terminal prune


def test_fresh_artifact_at_the_deadline_is_accepted(
    tmp_path, monkeypatch, clock
):
    """Last-chance accept: the artifact that lands as the timer expires
    is this attempt, not an uncertain completion."""
    mailbox = _mailbox(tmp_path)
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)
    original_wait = runner._wait

    def wait(client, session_id, timeout=None):
        # The LOG line lands during the last wait, which itself times out
        # (30 s idle dwell > time left): the race at the deadline.
        try:
            return original_wait(client, session_id, timeout=timeout)
        finally:
            if clock.now >= 299.0 and "- iter 1 | lead |" not in (
                mailbox / "LOG.md"
            ).read_text():
                with (mailbox / "LOG.md").open("a") as handle:
                    handle.write("- iter 1 | lead | landed at deadline\n")

    monkeypatch.setattr(runner, "_wait", wait)
    assert runner.run("lead", 1, mailbox) == 0
    assert trioctl._held_records(mailbox) == []
    assert runner.held_session_ids == []


def test_operator_cancel_is_not_a_hold(tmp_path, monkeypatch, clock):
    """Ctrl-C/SIGTERM stays an explicit cancel: no held record, and the
    loop wrapper prunes the run's sessions (exit 130)."""
    mailbox = _mailbox(tmp_path / "repo")
    pruned: list[list[str]] = []
    broker = RoleBroker(
        clock, row_lag=35.0, turn_len=400.0, fail_at=60.0,
        fail_exc=KeyboardInterrupt(),
    )

    class LeadLoop:
        @staticmethod
        def run_loop(mailbox_path, max_iterations, runner, **kwargs):
            return runner.run("lead", 1, mailbox_path)

    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: LeadLoop)
    monkeypatch.setattr(
        trioctl, "OmnigentRunner",
        _runner_factory(tmp_path, broker, monkeypatch),
    )
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "1"]
    )
    assert args.func(args) == 130
    assert trioctl._held_records(mailbox) == []
    assert [set(ids) for ids in pruned] == [{SID}]
    assert "needs_human" not in (mailbox / "STATE.md").read_text()


def test_definite_first_prompt_rejection_is_not_a_hold(
    tmp_path, monkeypatch, clock
):
    """A 4xx before the pending record: nothing runs, session deleted."""
    mailbox = _mailbox(tmp_path)
    broker = h.Native014Broker(clock, row_lag=5.0)

    def reject(session_id, message):
        raise h.BrokerHttpError("HTTP 409", status_code=409)

    broker.send_message = reject
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)
    with pytest.raises(trioctl.TrioctlError) as excinfo:
        runner.run("lead", 1, mailbox)
    assert "held dispatch" not in str(excinfo.value)
    assert trioctl._held_records(mailbox) == []
    assert broker.deletes == [f"/v1/sessions/{SID}"]


# -- held record cannot be written: STATE fallback ----------------------

def test_record_write_failure_falls_back_to_state(
    tmp_path, monkeypatch, clock
):
    core = trioctl._load_trio_loop(TREE)
    mailbox = _mailbox(tmp_path)
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    runner = h._runner(tmp_path, broker, monkeypatch, timeout=300.0)

    real = trioctl._write_held_record

    def fail(mailbox, record):
        raise OSError("disk full")

    monkeypatch.setattr(trioctl, "_write_held_record", fail)
    with pytest.raises(trioctl.TrioctlError, match="could NOT record"):
        core.run_loop(mailbox, 3, runner, repo=None)
    monkeypatch.setattr(trioctl, "_write_held_record", real)
    assert broker.deletes == []
    state = (mailbox / "STATE.md").read_text("utf-8")
    assert "status: needs_human" in state and "running" not in state
    _assert_resume_refused(tmp_path, mailbox, monkeypatch, clock, core)

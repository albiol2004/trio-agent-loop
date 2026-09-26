"""Concurrent open-loop slice-evals (`slice_eval_concurrency` > 1).

Same fake-runner style as test_open_loop_driver.py: real QUEUE.md /
VERDICT.md writes, in-process runners, no subprocess (the per-slice commit
gate is stubbed), no network.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests.test_open_loop_driver import (
    QueueModel,
    ScriptedEvalRunner,
    ScriptedLeadRunner,
    VerdictModel,
    fake_sha,
    make_open_loop_mailbox,
)


def plan(*ids: str) -> str:
    body = "".join(
        f"  - id: {sid}\n    writes: [loop/STATE.md, \"api:{sid}\"]\n    reads: []\n"
        for sid in ids
    )
    return f"```yaml\nslices:\n{body}```\n"


class TrackingEvalRunner(ScriptedEvalRunner):
    """Records overlap and exposes `inflight_sessions()` like OmnigentRunner."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.active = 0
        self.max_active = 0
        self.events: list[tuple[str, str]] = []  # (start|end, slice or kind)
        self.sessions: dict[str, dict] = {}
        self.threads: set[str] = set()

    def run(self, role, iteration, mailbox, context=None):
        name = context.get("slice") or context["kind"]
        sid = f"sess-{name}-{context.get('sha') or 'x'}"[:40]
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.append(("start", name))
            self.threads.add(threading.current_thread().name)
            self.sessions[sid] = {
                "role": role, "kind": context["kind"],
                "slice": context.get("slice"), "sha": context.get("sha"),
            }
        try:
            return super().run(role, iteration, mailbox, context)
        finally:
            with self._lock:
                self.active -= 1
                self.events.append(("end", name))
                self.sessions.pop(sid, None)

    def inflight_sessions(self):
        with self._lock:
            return {k: dict(v) for k, v in self.sessions.items()}


@pytest.fixture(autouse=True)
def _gate_passes(monkeypatch):
    monkeypatch.setattr(trio_loop, "_per_slice_gate", lambda *a, **k: 0)


def _slice_threads_alive() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("slice-eval")]


def _assert_no_slice_threads(timeout: float = 5.0) -> None:
    for t in _slice_threads_alive():
        t.join(timeout)
        assert not t.is_alive(), f"orphan slice-eval thread {t.name}"


def _setup(tmp_path: Path, ids: tuple[str, ...]):
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, plan(*ids))
    return mailbox, QueueModel(mailbox, lock), VerdictModel(mailbox, lock)


# (a) ----------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{}, {"slice_eval_concurrency": 1}])
def test_n1_keeps_serial_call_order(tmp_path: Path, kwargs) -> None:
    mailbox, queue, verdict = _setup(tmp_path, ("alpha", "beta"))
    sha_a, sha_b = fake_sha("a-n1"), fake_sha("b-n1")
    lead = ScriptedLeadRunner([
        lambda mb: (queue.retire("alpha", sha_a), queue.retire("beta", sha_b)),
    ])
    evaluator = TrackingEvalRunner(
        slice_actions={
            ("alpha", sha_a): lambda mb: verdict.append_slice_section("alpha", sha_a, "SHIP"),
            ("beta", sha_b): lambda mb: verdict.append_slice_section("beta", sha_b, "SHIP"),
        },
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01, **kwargs)
    assert code == 0
    assert evaluator.events == [
        ("start", "alpha"), ("end", "alpha"),
        ("start", "beta"), ("end", "beta"),
        ("start", "integration-eval"), ("end", "integration-eval"),
    ]
    assert evaluator.max_active == 1
    # Serial path runs slice-evals on the driver thread, no pool.
    assert not any(n.startswith("slice-eval") for n in evaluator.threads)
    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert "evaluator_sessions" not in driver


def test_n1_rejects_bad_concurrency(tmp_path: Path) -> None:
    mailbox, _q, _v = _setup(tmp_path, ("alpha",))
    with pytest.raises(ValueError):
        trio_loop.run_open_loop(
            mailbox, 1, ScriptedLeadRunner([]), ScriptedEvalRunner(),
            slice_eval_concurrency=0,
        )


# (b) ----------------------------------------------------------------------


def test_n3_dispatches_all_before_any_completes(tmp_path: Path) -> None:
    ids = ("alpha", "beta", "gamma")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-n3") for sid in ids}
    lead = ScriptedLeadRunner([
        lambda mb: [queue.retire(sid, shas[sid]) for sid in ids],
    ])
    barrier = threading.Barrier(3, timeout=10)

    def grade(sid):
        def action(mb):
            barrier.wait()  # breaks (test fails) unless all 3 are in flight
            verdict.append_slice_section(sid, shas[sid], "SHIP")
        return action

    evaluator = TrackingEvalRunner(
        slice_actions={(sid, shas[sid]): grade(sid) for sid in ids},
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(
        mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=3,
    )
    assert code == 0
    assert evaluator.max_active == 3
    starts = [n for kind, n in evaluator.events if kind == "start"]
    assert sorted(starts[:3]) == sorted(ids)
    first_end = next(i for i, e in enumerate(evaluator.events) if e[0] == "end")
    assert first_end >= 3
    # Integration-eval only after every slice-eval finished.
    assert evaluator.events[-2:] == [("start", "integration-eval"), ("end", "integration-eval")]
    assert not evaluator.slice_actions and not evaluator.integration_actions
    assert all(n.startswith("slice-eval") for n in evaluator.threads - {threading.current_thread().name})
    text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    for sid in ids:
        assert f"## slice {sid} @{shas[sid]} -- SHIP" in text
    assert "restored" not in (mailbox / "LOG.md").read_text(encoding="utf-8")
    _assert_no_slice_threads()


# (c) ----------------------------------------------------------------------


def test_n3_gate_failure_blocks_only_that_slice_and_integration(
    tmp_path: Path, monkeypatch
) -> None:
    ids = ("alpha", "beta", "gamma")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-gate") for sid in ids}
    lead = ScriptedLeadRunner([
        lambda mb: [queue.retire(sid, shas[sid]) for sid in ids],
    ])
    gamma_open = threading.Event()
    observed: dict = {}

    def gate(mailbox_, repo, slice_id):
        if slice_id != "gamma":
            return 0
        if gamma_open.is_set():
            return 0
        text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
        if all(f"## slice {s} @{shas[s]}" in text for s in ("alpha", "beta")):
            # alpha+beta graded, gamma still blocked: integration must not
            # have started; then let gamma through.
            observed["calls_at_block"] = [
                c["context"]["kind"] for c in evaluator.calls
            ]
            gamma_open.set()
        return 1

    monkeypatch.setattr(trio_loop, "_per_slice_gate", gate)
    evaluator = TrackingEvalRunner(
        slice_actions={
            (sid, shas[sid]): (lambda s: lambda mb: verdict.append_slice_section(s, shas[s], "SHIP"))(sid)
            for sid in ids
        },
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(
        mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=3,
    )
    assert code == 0
    assert observed["calls_at_block"] == ["slice-eval", "slice-eval"]
    kinds = [c["context"].get("slice") or c["context"]["kind"] for c in evaluator.calls]
    assert kinds.index("gamma") < kinds.index("integration-eval")
    assert "commit gate failed for slice gamma" in (mailbox / "LOG.md").read_text(encoding="utf-8")
    _assert_no_slice_threads()


def test_n3_runner_failure_raises_after_others_graded(tmp_path: Path) -> None:
    ids = ("alpha", "beta", "gamma")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-fail") for sid in ids}
    lead = ScriptedLeadRunner([
        lambda mb: [queue.retire(sid, shas[sid]) for sid in ids],
    ])

    class FailingGamma(TrackingEvalRunner):
        def run(self, role, iteration, mailbox_, context=None):
            code = super().run(role, iteration, mailbox_, context)
            return 1 if context.get("slice") == "gamma" else code

    evaluator = FailingGamma(
        slice_actions={
            ("alpha", shas["alpha"]): lambda mb: verdict.append_slice_section("alpha", shas["alpha"], "SHIP"),
            ("beta", shas["beta"]): lambda mb: verdict.append_slice_section("beta", shas["beta"], "SHIP"),
            ("gamma", shas["gamma"]): lambda mb: None,
        },
        integration_actions=[],
    )
    with pytest.raises(RuntimeError, match="evaluator runner failed with exit 1"):
        trio_loop.run_open_loop(
            mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=3,
        )
    kinds = [c["context"]["kind"] for c in evaluator.calls]
    assert "integration-eval" not in kinds
    text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"## slice alpha @{shas['alpha']} -- SHIP" in text
    assert f"## slice beta @{shas['beta']} -- SHIP" in text
    assert not (mailbox / ".lock").exists()  # finally drained + released
    _assert_no_slice_threads()


# (d) ----------------------------------------------------------------------


def test_slice_retired_after_first_harvest_is_dispatched_later(tmp_path: Path) -> None:
    mailbox, queue, verdict = _setup(tmp_path, ("alpha", "beta"))
    sha_a, sha_b = fake_sha("a-late"), fake_sha("b-late")
    alpha_harvested = threading.Event()

    def pass1(mb):
        queue.retire("alpha", sha_a)

    def pass2(mb):
        # Wait until the driver harvested alpha (its sidecar lost alpha).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = (mb / "VERDICT.md").read_text(encoding="utf-8")
            try:
                driver = json.loads((mb / ".driver.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                driver = {}
            if f"@{sha_a}" in text and driver.get("evaluator_sessions") == {}:
                alpha_harvested.set()
                break
            time.sleep(0.01)
        queue.retire("beta", sha_b)

    lead = ScriptedLeadRunner([pass1, pass2])
    evaluator = TrackingEvalRunner(
        slice_actions={
            ("alpha", sha_a): lambda mb: verdict.append_slice_section("alpha", sha_a, "SHIP"),
            ("beta", sha_b): lambda mb: verdict.append_slice_section("beta", sha_b, "SHIP"),
        },
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(
        mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=3,
    )
    assert code == 0
    assert alpha_harvested.is_set()
    assert evaluator.events == [
        ("start", "alpha"), ("end", "alpha"),
        ("start", "beta"), ("end", "beta"),
        ("start", "integration-eval"), ("end", "integration-eval"),
    ]
    _assert_no_slice_threads()


# (e) ----------------------------------------------------------------------


def test_driver_json_lists_inflight_evaluator_sessions_then_empties(
    tmp_path: Path,
) -> None:
    ids = ("alpha", "beta", "gamma")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-sidecar") for sid in ids}
    lead = ScriptedLeadRunner([
        lambda mb: [queue.retire(sid, shas[sid]) for sid in ids],
    ])
    release = threading.Event()

    def grade(sid):
        def action(mb):
            assert release.wait(10)
            verdict.append_slice_section(sid, shas[sid], "SHIP")
        return action

    evaluator = TrackingEvalRunner(
        slice_actions={(sid, shas[sid]): grade(sid) for sid in ids},
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    result: dict = {}
    driver_thread = threading.Thread(
        target=lambda: result.setdefault("code", trio_loop.run_open_loop(
            mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=3,
        )),
        name="test-driver",
    )
    driver_thread.start()
    seen = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            driver = {}
        sessions = driver.get("evaluator_sessions") or {}
        if len(sessions) == 3 and all(sessions.values()):
            seen = driver
            break
        time.sleep(0.01)
    release.set()
    driver_thread.join(15)
    assert not driver_thread.is_alive()
    assert result["code"] == 0
    assert seen is not None, "never saw all three in-flight evaluator sessions"
    assert seen["evaluator_sessions"] == {
        f"{sid}@{shas[sid]}": f"sess-{sid}-{shas[sid]}"[:40] for sid in ids
    }
    assert seen["phase"] == "evaluator"
    final = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert final["evaluator_sessions"] == {}
    assert final["phase"] == "done"
    _assert_no_slice_threads()


# (f) ----------------------------------------------------------------------


def _capped_run(tmp_path: Path, *, hold: float, drain: float):
    """4-slice plan, pass 1 retires 3, max_iterations=1 -> Lead caps while
    two slice-evals run (N=2) and the third is still queued."""
    ids = ("alpha", "beta", "gamma", "delta")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-cap") for sid in ids}
    lead = ScriptedLeadRunner([
        lambda mb: [queue.retire(sid, shas[sid]) for sid in ids[:3]],
    ])
    def grade(sid):
        def action(mb):
            time.sleep(hold)
            verdict.append_slice_section(sid, shas[sid], "SHIP")
        return action

    evaluator = TrackingEvalRunner(
        slice_actions={(sid, shas[sid]): grade(sid) for sid in ids[:3]},
    )
    t0 = time.monotonic()
    code = trio_loop.run_open_loop(
        mailbox, 1, lead, evaluator, poll_seconds=0.01,
        slice_eval_concurrency=2, slice_eval_drain_seconds=drain,
    )
    return code, time.monotonic() - t0, evaluator, mailbox


def test_exit_with_running_future_cancels_queued_and_waits_for_running(
    tmp_path: Path,
) -> None:
    code, elapsed, evaluator, mailbox = _capped_run(tmp_path, hold=0.5, drain=10)
    assert code == 4
    started_slices = [n for kind, n in evaluator.events if kind == "start"]
    ended = [n for kind, n in evaluator.events if kind == "end"]
    # Two ran to completion inside the drain; the queued third never ran.
    assert len(started_slices) == 2 and sorted(ended) == sorted(started_slices)
    assert len(evaluator.slice_actions) == 1
    assert elapsed >= 0.4
    assert "abandoned" not in (mailbox / "LOG.md").read_text(encoding="utf-8")
    # Every started slice-eval that finished inside the drain left its section.
    text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert sum(f"## slice {s} @" in text for s in started_slices) == 2
    final = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert final["phase"] == "done" and final["evaluator_sessions"] == {}
    _assert_no_slice_threads()


def test_drain_budget_expiry_is_logged_and_thread_still_ends(tmp_path: Path) -> None:
    code, elapsed, evaluator, mailbox = _capped_run(tmp_path, hold=1.0, drain=0.1)
    assert code == 4
    assert elapsed < 0.9  # did not wait past the budget
    assert "still running after the 0.1s drain budget; abandoned" in (
        mailbox / "LOG.md"
    ).read_text(encoding="utf-8")
    # The abandoned worker finishes on its own; no thread outlives it.
    _assert_no_slice_threads(timeout=5.0)


# clobber guard across concurrent evals -----------------------------------


def test_concurrent_eval_clobbering_a_section_written_mid_flight_is_restored(
    tmp_path: Path,
) -> None:
    """beta starts before alpha's section exists, then rewrites VERDICT.md
    whole: alpha's section (seen on disk while beta ran) must come back."""
    mailbox, queue, verdict = _setup(tmp_path, ("alpha", "beta"))
    sha_a, sha_b = fake_sha("a-clob"), fake_sha("b-clob")
    lead = ScriptedLeadRunner([
        lambda mb: (queue.retire("alpha", sha_a), queue.retire("beta", sha_b)),
    ])
    alpha_done = threading.Event()

    def eval_alpha(mb):
        verdict.append_slice_section("alpha", sha_a, "SHIP")
        alpha_done.set()

    def eval_beta_clobbers(mb):
        assert alpha_done.wait(10)
        time.sleep(0.2)  # the driver harvests alpha and sees its section
        (mb / "VERDICT.md").write_text(f"## slice beta @{sha_b} -- SHIP\n", encoding="utf-8")

    evaluator = TrackingEvalRunner(
        slice_actions={("alpha", sha_a): eval_alpha, ("beta", sha_b): eval_beta_clobbers},
        integration_actions=[lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")],
    )
    code = trio_loop.run_open_loop(
        mailbox, 5, lead, evaluator, poll_seconds=0.01, slice_eval_concurrency=2,
    )
    assert code == 0
    text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"## slice alpha @{sha_a} -- SHIP" in text
    assert f"## slice beta @{sha_b} -- SHIP" in text
    assert (
        "open-loop: restored 1 clobbered per-slice section(s) in VERDICT.md "
        "after slice-eval" in (mailbox / "LOG.md").read_text(encoding="utf-8")
    )
    _assert_no_slice_threads()


def test_drain_budget_defaults_to_min_of_role_timeout_and_120(monkeypatch) -> None:
    monkeypatch.delenv(trio_loop.SLICE_EVAL_DRAIN_ENV, raising=False)

    class R:
        _timeout = 42.0

    class Big:
        _timeout = 3600.0

    assert trio_loop._slice_eval_drain_seconds(R()) == 42.0
    assert trio_loop._slice_eval_drain_seconds(Big()) == 120.0
    # No runner timeout: the 3600 s role default, capped at 120.
    assert trio_loop._slice_eval_drain_seconds(object()) == 120.0


def test_drain_budget_explicit_override_and_env(monkeypatch) -> None:
    class Big:
        _timeout = 3600.0

    monkeypatch.delenv(trio_loop.SLICE_EVAL_DRAIN_ENV, raising=False)
    assert trio_loop._slice_eval_drain_seconds(Big(), 5.0) == 5.0
    assert trio_loop._slice_eval_drain_seconds(Big(), 900) == 900.0
    assert trio_loop._slice_eval_drain_seconds(Big(), 0) == 0.0
    monkeypatch.setenv(trio_loop.SLICE_EVAL_DRAIN_ENV, "7")
    assert trio_loop._slice_eval_drain_seconds(Big()) == 7.0
    assert trio_loop._slice_eval_drain_seconds(Big(), 3.0) == 3.0  # flag wins
    for bad in ("abc", "-1", "nan", "inf"):
        monkeypatch.setenv(trio_loop.SLICE_EVAL_DRAIN_ENV, bad)
        assert trio_loop._slice_eval_drain_seconds(Big()) == 120.0
    # run_loop passes the override through only when given.
    import inspect

    assert "slice_eval_drain_seconds" in inspect.signature(trio_loop.run_loop).parameters


def _held(mailbox: Path) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((mailbox / ".sessions").glob("held-*.json"))
    ]


def test_abandoned_slice_eval_gets_a_held_record(tmp_path: Path) -> None:
    code, _elapsed, evaluator, mailbox = _capped_run(tmp_path, hold=1.0, drain=0.1)
    assert code == 4
    held = _held(mailbox)
    started = sorted(n for kind, n in evaluator.events if kind == "start")
    assert sorted(r["slice"] for r in held) == started and len(held) == 2
    for record in held:
        assert record["hold"] == "abandoned_on_exit"
        assert record["role"] == "evaluator" and record["kind"] == "slice-eval"
        sha = record["sha"]
        assert record["session_id"] == f"sess-{record['slice']}-{sha}"[:40]
        assert record["pinned_sha"] == sha
    log = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "abandoned on exit; see held-" in log
    _assert_no_slice_threads(timeout=5.0)


def test_no_held_record_when_evals_finish_inside_the_drain(tmp_path: Path) -> None:
    code, _elapsed, _evaluator, mailbox = _capped_run(tmp_path, hold=0.3, drain=10)
    assert code == 4
    assert _held(mailbox) == []
    _assert_no_slice_threads()


# F3: a second interrupt during the exit drain -----------------------------


def _interrupt_during_drain(monkeypatch, budget: float) -> None:
    """The drain's own wait (timeout == *budget*) receives a real SIGINT."""
    import concurrent.futures
    import os
    import signal

    real_wait = concurrent.futures.wait

    def wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if timeout == budget:
            os.kill(os.getpid(), signal.SIGINT)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(concurrent.futures, "wait", wait)


def _sigint_when(predicate) -> threading.Thread:
    import os
    import signal

    def body():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=body, name="test-first-sigint", daemon=True)
    thread.start()
    return thread


def test_second_interrupt_during_drain_releases_lock_and_holds(
    tmp_path: Path, monkeypatch
) -> None:
    import signal

    ids = ("alpha", "beta")
    mailbox, queue, verdict = _setup(tmp_path, ids)
    shas = {sid: fake_sha(f"{sid}-sigint") for sid in ids}
    lead = ScriptedLeadRunner([lambda mb: [queue.retire(s, shas[s]) for s in ids]])
    release = threading.Event()

    def grade(sid):
        def action(mb):
            assert release.wait(10)
            verdict.append_slice_section(sid, shas[sid], "SHIP")
        return action

    evaluator = TrackingEvalRunner(
        slice_actions={(s, shas[s]): grade(s) for s in ids},
    )
    _interrupt_during_drain(monkeypatch, 30.0)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        first = _sigint_when(lambda: len(evaluator.inflight_sessions()) == 2)
        with pytest.raises(KeyboardInterrupt):
            trio_loop.run_open_loop(
                mailbox, 5, lead, evaluator, poll_seconds=0.01,
                slice_eval_concurrency=2, slice_eval_drain_seconds=30.0,
            )
        first.join(5)
        assert not (mailbox / ".lock").exists()
        held = _held(mailbox)
        assert sorted(r["slice"] for r in held) == ["alpha", "beta"]
        assert all(r["hold"] == "abandoned_on_exit" for r in held)
        assert "still running when the drain was interrupted; abandoned" in (
            mailbox / "LOG.md"
        ).read_text(encoding="utf-8")
    finally:
        signal.signal(signal.SIGINT, previous)
        release.set()
    _assert_no_slice_threads()

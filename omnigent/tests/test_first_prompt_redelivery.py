"""Offline regression: a consumed first prompt is never re-posted.

Canary eval-canary-retirement/run-1d716d4, session 6f173b9d: the
Evaluator prompt was POSTed once at create (21:28:18Z). Cursor-native
writes the user row only together with the first assistant row
(21:28:53Z, 35 s later), so ``ensure_first_prompt`` declared a miss and
re-posted. The broker queued that copy behind the running turn and
delivered it when the turn went idle -- 30 s after the SHIP retirement
commit -- and the Evaluator re-ran its whole pass in the same session.

The fake below runs on a virtual clock with the live defaults (20 s
window, 0.4 s interval, 3 attempts): POSTed messages queue, a bound
idle runner consumes one per turn, and the user+assistant rows appear
``ingest_lag`` virtual seconds after the turn starts (35 s observed).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


SCRIPT = Path(__file__).parents[1] / "trioctl"
ATTEMPT = "725cd824ca98470592cabb6e83829bae"
PIN = "ab1f0d76ac30ba946cbecf378a48554b45e27335"
PROMPT = (
    f"LOCKSTEP CONTEXT: attempt={ATTEMPT} sha={PIN}\n\n"
    "# Trio Evaluator — one headless iteration\n"
)


def load_trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


trioctl = load_trioctl()
BrokerClient = trioctl.broker_http.BrokerClient
BrokerHttpError = trioctl.broker_http.BrokerHttpError


class CursorQueueBroker(BrokerClient):
    """In-memory broker with Omnigent queue + Cursor-native row timing.

    ``accept`` controls when the TUI consumes queued input:
    ``"poll"`` on the first session poll after a send (pending visible
    once), ``"send"`` immediately (pending never visible), ``"never"``
    (welcome-screen: stays pending), or ``"second"`` (only once a second
    copy arrives -- the documented cold-TUI recovery).
    """

    def __init__(
        self,
        *,
        accept: str = "poll",
        running_polls: int | None = None,
        unbind_after_consume: bool = False,
        rows_on_consume: bool = False,
        expose_pending: bool = True,
        turn_polls: int = 3,
        ingest_lag: float = 35.0,
        mailbox: Path | None = None,
        clock: "VirtualClock | None" = None,
    ) -> None:
        super().__init__("http://fake.invalid")
        self.accept = accept
        self.running_polls = running_polls
        self.unbind_after_consume = unbind_after_consume
        self.rows_on_consume = rows_on_consume
        self.expose_pending = expose_pending
        self.turn_polls = turn_polls
        self.mailbox = mailbox
        self.ingest_lag = ingest_lag
        self.clock = clock
        self.consumed_at = 0.0
        self.events: list[str] = []
        self.queue: list[str] = []
        self.turns: list[str] = []
        self.rows: list[dict[str, Any]] = [
            {"id": "r0", "type": "resource_event", "status": "completed"}
        ]
        self.status = "idle"
        self.runner_id: str | None = "runner-1"
        self.polls_since_consume = 0
        self.wait_polls = 0
        self.deletes: list[str] = []
        self.completed_turns = 0

    # -- HTTP seams -------------------------------------------------
    def _request(self, method, path, payload=None, expected_status=200):
        if method == "POST" and path == "/v1/sessions":
            return {"id": "s1", "status": "idle", "runner_id": "runner-1"}
        if method == "DELETE":
            self.deletes.append(path)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def list_hosts(self):
        return {"hosts": [{"host_id": "h1", "status": "online"}]}

    def send_message(self, session_id, message):
        self.events.append(message)
        self.queue.append(message)
        if (
            self.accept == "send"
            and self.status == "idle"
            and self.runner_id is not None
        ):
            self._consume()
        elif self.accept == "second" and len(self.queue) >= 2:
            self.queue.clear()
            self._consume(message)
        return {"queued": True}

    # -- simulated runner ------------------------------------------
    def _consume(self, message: str | None = None) -> None:
        text = message if message is not None else self.queue.pop(0)
        self.turns.append(text)
        self.status = "running"
        self.polls_since_consume = 0
        self.consumed_at = self.clock.now if self.clock else 0.0
        if self.unbind_after_consume:
            self.runner_id = None
        if self.rows_on_consume:
            self._emit_rows(text)

    def _emit_rows(self, text: str) -> None:
        n = len(self.rows)
        self.rows.append({"id": f"u{n}", "role": "user", "type": "message",
                          "status": "completed",
                          "content": [{"type": "input_text", "text": text}]})
        self.rows.append({"id": f"a{n}", "role": "assistant",
                          "type": "message", "status": "completed",
                          "content": [{"type": "output_text", "text": "ok"}]})

    def _ingest(self) -> None:
        """Cursor writes user+assistant rows ``ingest_lag`` after start."""
        if (
            self.turns
            and self.clock is not None
            and self.clock.now - self.consumed_at >= self.ingest_lag
            and not self._has_rows_for(self.turns[-1])
        ):
            self._emit_rows(self.turns[-1])

    def _finish_turn(self) -> None:
        self.completed_turns += 1
        if self.mailbox is not None and self.completed_turns == 1:
            (self.mailbox / "VERDICT.md").write_text(
                "VERDICT: SHIP\niteration: 1\n"
                f"attempt: {ATTEMPT}\nevaluated: {PIN}\ncommit: {PIN}\n",
                encoding="utf-8",
            )
        self.status = "idle"

    def get_session(self, session_id):
        if (
            self.status == "idle"
            and self.queue
            and self.runner_id is not None
            and self.accept in {"poll", "send"}
        ):
            # Omnigent hands queued input to an idle bound runner.
            self._consume()
        elif self.status == "running" and self.running_polls is not None:
            self.polls_since_consume += 1
            if self.polls_since_consume > self.running_polls:
                self.status = "idle"  # think gap / blip settles idle
        self._ingest()
        snap: dict[str, Any] = {
            "id": session_id,
            "status": self.status,
            "runner_id": self.runner_id,
        }
        if self.expose_pending:
            snap["pending_inputs"] = [
                {"id": f"p{i}"} for i in range(len(self.queue))
            ]
        return snap

    def get_items(self, session_id, limit=100, order="asc", after=None):
        if order == "desc" and limit == 10:
            # Driver wait poll: the running turn makes progress here only.
            self.wait_polls += 1
            if self.status == "running" and self.clock is not None:
                self.clock.now += 1.0  # the model works while we wait
                self._ingest()
                if self._has_rows_for(self.turns[-1]):
                    self.polls_since_consume += 1
                    if self.polls_since_consume >= self.turn_polls:
                        self._finish_turn()
            return {"data": list(reversed(self.rows))[:10]}
        self._ingest()
        return {"data": list(self.rows)}

    def _has_rows_for(self, text: str) -> bool:
        return sum(
            1 for r in self.rows if r.get("role") == "user"
            and r["content"][0]["text"] == text
        ) >= self.turns.count(text)


class VirtualClock:
    """sleep() advances time; every monotonic() read ticks 1 ms."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        self.now += 0.001
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.0)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    """Live prompt defaults on a virtual clock (no env overrides)."""
    for name in (
        "TRIO_OMNIGENT_PROMPT_WAIT",
        "TRIO_OMNIGENT_PROMPT_INTERVAL",
        "TRIO_OMNIGENT_PROMPT_ATTEMPTS",
        "TRIO_OMNIGENT_RUNNER_ID",
        "TRIO_MAILBOX_SESSION_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    virtual = VirtualClock()
    fake_time = SimpleNamespace(
        monotonic=virtual.monotonic,
        sleep=virtual.sleep,
        time=real_time.time,
        time_ns=real_time.time_ns,
        monotonic_ns=real_time.monotonic_ns,
    )
    monkeypatch.setattr(trioctl.broker_http, "time", fake_time)
    monkeypatch.setattr(trioctl, "time", fake_time)
    return virtual


# -- create-time: consumed prompts are not re-posted -------------------

@pytest.mark.parametrize(
    "shape",
    [
        # Incident: pending seen once, drained, turn running, no rows yet.
        {"accept": "poll"},
        # Consumed before the first poll: pending never observed.
        {"accept": "send"},
        # Consumed, then a think gap reads idle on a bound runner.
        {"accept": "send", "running_polls": 1},
        # Broker that does not expose pending_inputs at all.
        {"accept": "send", "expose_pending": False},
    ],
    ids=["drain", "consumed-unseen", "think-gap-idle", "no-pending-field"],
)
def test_consumed_first_prompt_without_rows_is_not_reposted(shape, clock):
    broker = CursorQueueBroker(**shape, clock=clock)

    created = broker.create_session("agent", "model", PROMPT, "title")

    assert created["id"] == "s1"
    assert broker.events == [PROMPT]
    assert broker.queue == []
    assert broker.deletes == []


# -- create-time: genuine misses still recover on the same session -----

def test_prompt_stuck_pending_on_welcome_screen_is_reposted(clock):
    """014 cold TUI: prompt stays pending, no item; re-post nudges it."""
    broker = CursorQueueBroker(
        accept="second", rows_on_consume=True, clock=clock
    )

    broker.create_session("agent", "model", PROMPT, "title")

    assert broker.events == [PROMPT, PROMPT]
    assert broker.turns == [PROMPT]
    assert broker.deletes == []


def test_restart_blip_unbinds_after_consume_is_reposted(clock):
    """Runner consumed then dropped (unbound, no rows): a real miss."""
    broker = CursorQueueBroker(
        accept="send", unbind_after_consume=True, clock=clock
    )
    original_send = broker.send_message

    def rebind_then_send(session_id, message):
        if broker.events:
            broker.runner_id = "runner-2"
            broker.unbind_after_consume = False
            broker.rows_on_consume = True
        return original_send(session_id, message)

    broker.send_message = rebind_then_send  # type: ignore[method-assign]

    broker.create_session("agent", "model", PROMPT, "title")

    assert broker.events == [PROMPT, PROMPT]
    assert broker.deletes == []


def test_no_delivery_evidence_is_reposted_then_fails_closed(clock):
    """Never consumed, nothing pending, idle: bounded re-posts, DELETE."""
    broker = CursorQueueBroker(
        accept="never", expose_pending=False, clock=clock
    )

    with pytest.raises(BrokerHttpError, match="first prompt did not land"):
        broker.create_session("agent", "model", PROMPT, "title")

    assert broker.events == [PROMPT, PROMPT, PROMPT]
    assert broker.deletes == ["/v1/sessions/s1"]


# -- end to end: no duplicate Evaluator turn after SHIP ----------------

def _mailbox(tmp_path: Path) -> Path:
    mailbox = tmp_path / "loop-natural-trial"
    mailbox.mkdir()
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Log\n", encoding="utf-8")
    return mailbox


def _runner(tmp_path: Path, broker, monkeypatch, timeout: float = 600.0):
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, broker_client=broker, interval=0, timeout=timeout
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "evaluator-agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: PROMPT)
    return runner


@pytest.mark.parametrize("accept", ["send", "poll"], ids=["canary", "drain"])
def test_evaluator_ship_is_not_followed_by_a_reposted_duplicate_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock, accept
) -> None:
    """Replay 6f173b9d: rows land 35 s after start, past the 20 s window.

    Unfixed, the create-time re-post queues a second prompt that runs
    as a full Evaluator turn after the SHIP artifact is written.
    """
    mailbox = _mailbox(tmp_path)
    broker = CursorQueueBroker(accept=accept, mailbox=mailbox, clock=clock)
    runner = _runner(tmp_path, broker, monkeypatch)
    context = {"evaluator_attempt": ATTEMPT, "pinned_sha": PIN}

    assert runner.run("evaluator", 1, mailbox, context) == 0

    assert len(broker.turns) == 1, "duplicate Evaluator turn after SHIP"
    assert broker.events == [PROMPT]
    assert broker.queue == []
    verdict = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"attempt: {ATTEMPT}" in verdict


def test_stale_verdict_with_consumed_prompt_still_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """No re-post does not relax the artifact gate: stale SHIP != pass."""
    mailbox = _mailbox(tmp_path)
    stale = (
        "VERDICT: SHIP\niteration: 1\nattempt: 0000stale\n"
        f"evaluated: {PIN}\n"
    )
    (mailbox / "VERDICT.md").write_text(stale, encoding="utf-8")
    broker = CursorQueueBroker(accept="send", clock=clock)  # no VERDICT
    runner = _runner(tmp_path, broker, monkeypatch, timeout=120.0)
    context = {"evaluator_attempt": ATTEMPT, "pinned_sha": PIN}

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        runner.run("evaluator", 1, mailbox, context)

    assert broker.events == [PROMPT]
    assert (mailbox / "VERDICT.md").read_text(encoding="utf-8") == stale


def test_wrong_pin_verdict_with_consumed_prompt_still_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """A fresh-attempt SHIP for another pin is not this iteration."""
    mailbox = _mailbox(tmp_path)
    broker = CursorQueueBroker(accept="send", mailbox=mailbox, clock=clock)
    runner = _runner(tmp_path, broker, monkeypatch, timeout=120.0)
    context = {"evaluator_attempt": ATTEMPT, "pinned_sha": "f" * 40}

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        runner.run("evaluator", 1, mailbox, context)

    assert broker.events == [PROMPT]

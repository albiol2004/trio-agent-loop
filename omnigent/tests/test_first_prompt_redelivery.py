"""Offline regression: re-post a first prompt only on a known miss.

Canary eval-canary-retirement/run-1d716d4, session 6f173b9d: the
Evaluator prompt was POSTed at create (21:28:18Z); cursor-native wrote
its user row only with the first assistant row (+35 s). The 20 s
first-prompt window re-posted it, the TUI queued the copy as a
follow-up, and it ran as a second Evaluator turn after the SHIP
retirement commit.

``Native014Broker`` follows Omnigent 0.14.0 (139c74a1) source, read-only:

* ``fixtures/omnigent_0140_pending_inputs.py`` is the real
  ``omnigent/runtime/pending_inputs.py`` (Apache-2.0, sha256 pinned
  below). POST /events records an entry before forwarding and rolls it
  back only if the forward fails (routes/_sessions/orchestration.py
  ~6012/~6156); it drains only when the transcript user row is
  persisted (resolve_oldest ~2448), or after the 600 s TTL.
* Cursor injection (harnesses/cursor_native/bridge.py): wait up to
  30 s for the input box, else paste blind; a blind paste into a
  booting TUI is lost. Pasting into a busy TUI queues a follow-up turn.
* Status is PTY-activity-derived (runner/resource_registry.py): any
  pane change reads ``running``, 1 s of quiet reads ``idle``. Working
  turns can blip idle; that is modelled with ``quiet_gaps``.

Live shapes (first-prompt-canary/FINDINGS.md, installed e07075c): s1
user row +13.8 s, first assistant row +54.4 s; s2 (9,339-byte prompt)
first delivery persisted a user row missing the first 3,885 chars plus
a leaked ``[201~`` and got an assistant reply; POSTs at 0/21.4/41.5 s,
the third ran as a duplicate turn. Intact rows equal the posted text.

Contract (eval-c19913b-opus/DELIVERY-CONTRACT.md, coordinator rulings):
landed = a user row equal to the prompt; known miss = every accepted
POST has a user row and none matches -> bounded re-post; otherwise
uncertain -> no re-post, no delete, session held from prune.
"""
from __future__ import annotations

import hashlib
import json
import importlib.machinery
import importlib.util
import sys
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


HERE = Path(__file__).parent
SCRIPT = HERE.parent / "trioctl"
PENDING_FIXTURE = HERE / "fixtures" / "omnigent_0140_pending_inputs.py"
PENDING_SHA256 = (
    "c06c4623de38fb839ac5fe0825cb1c5c5354af4ae8a97992fe65e25e089d8178"
)
ATTEMPT = "725cd824ca98470592cabb6e83829bae"
PIN = "ab1f0d76ac30ba946cbecf378a48554b45e27335"
PROMPT = (
    f"LOCKSTEP CONTEXT: attempt={ATTEMPT} sha={PIN}\n\n"
    "# Trio Evaluator — one headless iteration\n"
)
SID = "s1"
SETTLE_S = 30.0  # bridge._TMUX_READY_TIMEOUT_S
PASTE_S = 0.5  # paste render + settle before Enter
TICK = 0.1


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module here
    loader.exec_module(module)
    return module


trioctl = _load(SCRIPT, "trioctl")
BrokerClient = trioctl.broker_http.BrokerClient
BrokerHttpError = trioctl.broker_http.BrokerHttpError


class VirtualClock:
    """sleep() advances time; every monotonic() read ticks 1 ms."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 0.001
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.0)


class Native014Broker(BrokerClient):
    """Omnigent 0.14 native-terminal session driven on a virtual clock."""

    def __init__(
        self,
        clock: VirtualClock,
        *,
        ready_at: float | None = 1.0,
        boot_active: bool = False,
        row_lag: float = 35.0,
        assistant_lag: float | None = None,
        turn_len: float = 400.0,
        corrupt: tuple[str, ...] = (),
        corrupt_row_lag: float = 19.5,
        corrupt_turn_len: float = 38.0,  # live s2 replies at +21, +38 s
        quiet_gaps: tuple[tuple[float, float], ...] = (),
        expose_pending: bool = True,
        unbind_at: float | None = None,
        rebind_at: float | None = None,
        forward_fails: bool = False,
        ambiguous_posts: tuple[str | None, ...] = (),
        writes_rows: bool = True,
        mailbox: Path | None = None,
    ) -> None:
        super().__init__("http://fake.invalid")
        self.clock = clock
        self.pi = _load(PENDING_FIXTURE, f"pending_inputs_{id(self)}")
        self.pi._now = lambda: clock.now
        self.ready_at = ready_at
        self.boot_active = boot_active
        self.row_lag = row_lag
        self.assistant_lag = row_lag if assistant_lag is None else assistant_lag
        self.turn_len = turn_len
        self.corrupt = list(corrupt)
        self.corrupt_row_lag = corrupt_row_lag
        self.corrupt_turn_len = corrupt_turn_len
        self.quiet_gaps = quiet_gaps
        self.expose_pending = expose_pending
        self.unbind_at = unbind_at
        self.rebind_at = rebind_at
        self.forward_fails = forward_fails
        # Per POST: "queued" = accepted, reply lost; "lost" = never
        # reached the broker; None = normal 202.
        self.ambiguous_posts = list(ambiguous_posts)
        self.writes_rows = writes_rows
        self.mailbox = mailbox
        self.runner_id: str | None = "runner-1"
        self.sim_t = 0.0
        self.posts: list[float] = []
        self.forwards: list[dict[str, Any]] = []
        self.busy_until = 0.0
        self.followups: list[str] = []
        self.turn: dict[str, Any] | None = None
        self.turns: list[str] = []
        self.dropped = 0
        self.rows: list[dict[str, Any]] = [
            {"id": "r0", "type": "resource_event", "status": "completed"}
        ]
        self.deletes: list[str] = []

    # -- HTTP seams --------------------------------------------------
    def _request(self, method, path, payload=None, expected_status=200):
        if method == "POST" and path == "/v1/sessions":
            return {"id": SID, "status": "idle", "runner_id": "runner-1"}
        if method == "DELETE":
            self.deletes.append(path)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def list_hosts(self):
        return {"hosts": [{"host_id": "h1", "status": "online"}]}

    def send_message(self, session_id, message):
        self._advance()
        self.posts.append(round(self.clock.now, 1))
        fate = self.ambiguous_posts.pop(0) if self.ambiguous_posts else None
        if fate == "lost":
            raise trioctl.broker_http.BrokerRequestAmbiguous(
                "POST events outcome unknown: connection refused"
            )
        content = [{"type": "input_text", "text": message}]
        pid = self.pi.record(SID, content)
        if self.forward_fails:
            self.pi.resolve(SID, pid)
            raise BrokerHttpError(
                "POST events failed with HTTP 502", status_code=502
            )
        self.forwards.append({"text": message, "posted": self.clock.now})
        if fate == "queued":
            raise trioctl.broker_http.BrokerRequestAmbiguous(
                "POST events outcome unknown: timed out"
            )
        return {"queued": True}

    def get_session(self, session_id):
        self._advance()
        snap: dict[str, Any] = {
            "id": SID,
            "status": self._status(),
            "runner_id": self.runner_id,
        }
        if self.expose_pending:
            snap["pending_inputs"] = self.pi.snapshot_for(SID)
        return snap

    def get_items(self, session_id, limit=100, order="asc", after=None):
        self._advance()
        rows = list(self.rows)
        if order == "desc":
            rows.reverse()
        return {"data": rows[:limit]}

    # -- simulation --------------------------------------------------
    def run_until(self, t: float) -> None:
        self.clock.now = max(self.clock.now, t)
        self._advance()

    def _advance(self) -> None:
        while self.sim_t < self.clock.now:
            self.sim_t = min(self.clock.now, self.sim_t + TICK)
            self._tick(self.sim_t)

    def _tick(self, t: float) -> None:
        if self.unbind_at is not None and t >= self.unbind_at:
            self.unbind_at = None
            self.runner_id = None
            self.turn = None  # the pane died with its runner
            self.followups.clear()
        if self.rebind_at is not None and t >= self.rebind_at:
            self.rebind_at = None
            self.runner_id = "runner-2"
            self.ready_at = t + 1.0
            self.busy_until = t
        if self.runner_id is not None and self.forwards:
            fwd = self.forwards[0]
            if "paste_at" not in fwd:
                start = max(fwd["posted"], self.busy_until)
                ready = self.ready_at
                if ready is not None and ready <= start + SETTLE_S:
                    fwd["paste_at"] = max(start, ready) + PASTE_S
                    fwd["lost"] = False
                else:
                    fwd["paste_at"] = start + SETTLE_S + PASTE_S
                    fwd["lost"] = True
            if t >= fwd["paste_at"]:
                self.forwards.pop(0)
                self.busy_until = fwd["paste_at"]
                if fwd["lost"]:
                    self.dropped += 1
                elif self.turn is not None:
                    self.followups.append(fwd["text"])
                else:
                    self._start_turn(fwd["text"], t)
        turn = self.turn
        if turn is None:
            return
        if not self.writes_rows:
            pass
        elif not turn["user_row"] and t >= turn["start"] + turn["row_lag"]:
            turn["user_row"] = True
            self.pi.resolve_oldest(SID)  # transcript mirrored a user row
            self._row("user", turn["row_text"])
        elif turn["user_row"] and not turn["reply"] and (
            t >= turn["start"] + turn["assistant_lag"]
        ):
            turn["reply"] = True
            self._row("assistant", "working")
        if t >= turn["start"] + turn["len"]:
            if self.mailbox is not None and self.intact_turns() == 1:
                (self.mailbox / "VERDICT.md").write_text(
                    "VERDICT: SHIP\niteration: 1\n"
                    f"attempt: {ATTEMPT}\nevaluated: {PIN}\ncommit: {PIN}\n",
                    encoding="utf-8",
                )
            self.turn = None
            if self.followups:
                self._start_turn(self.followups.pop(0), t)

    def _start_turn(self, text: str, t: float) -> None:
        turn = {
            "text": text, "start": t, "user_row": False, "reply": False,
            "row_text": text, "row_lag": self.row_lag,
            "assistant_lag": max(self.assistant_lag, self.row_lag),
            "len": self.turn_len,
        }
        if self.corrupt:
            # Cold TUI mangled this paste; Cursor records what it got.
            mode = self.corrupt.pop(0)
            turn.update(
                row_text={
                    "head": text[3885:] + "\n[201~",  # live s2
                    "tail": text[: len(text) // 2],
                    "prefix": text[:16],  # "LOCKSTEP CONTEXT"
                    "empty": "",
                }[mode],
                row_lag=self.corrupt_row_lag,
                assistant_lag=self.corrupt_row_lag + 1.0,
                len=self.corrupt_turn_len,
            )
        self.turn = turn
        self.turns.append(turn["row_text"])

    def intact_turns(self) -> int:
        return sum(1 for text in self.turns if text == PROMPT)

    def _row(self, role: str, text: str) -> None:
        kind = "input_text" if role == "user" else "output_text"
        self.rows.append({
            "id": f"r{len(self.rows)}", "type": "message", "role": role,
            "status": "completed",
            "content": [{"type": kind, "text": text}],
        })

    def _status(self) -> str:
        t = self.sim_t
        if self.runner_id is None:
            return "idle"
        if self.turn is not None:
            offset = t - self.turn["start"]
            for lo, hi in self.quiet_gaps:
                if lo <= offset < hi:
                    return "idle"
            return "running"
        if self.boot_active and (self.ready_at is None or t < self.ready_at):
            return "running"
        return "idle"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> VirtualClock:
    """Live defaults (600 s ceiling, 0.4 s poll, 3 copies) on a virtual clock."""
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
        strftime=real_time.strftime,
        gmtime=real_time.gmtime,
    )
    monkeypatch.setattr(trioctl.broker_http, "time", fake_time)
    monkeypatch.setattr(trioctl, "time", fake_time)
    return virtual


def _create(broker: Native014Broker, prompt: str = PROMPT):
    """create_session, then let every queued copy run; the error or None."""
    try:
        broker.create_session("agent", "model", prompt, "title")
        error = None
    except BrokerHttpError as exc:
        error = exc
    broker.run_until(broker.clock.now + 2000.0)
    return error


PADDED = PROMPT + "".join(
    f"reference line {i:05d}: inert padding, ignore it.\n"
    for i in range(200)
)


def test_vendored_pending_inputs_is_pinned():
    digest = hashlib.sha256(PENDING_FIXTURE.read_bytes()).hexdigest()
    assert digest == PENDING_SHA256


# -- landed: an intact user row, however late --------------------------

@pytest.mark.parametrize(
    "shape",
    [
        # Canary 6f173b9d: rows +35 s after the paste.
        {"row_lag": 35.0},
        # Live s1: user row +13.8 s, first assistant row +54.4 s.
        {"row_lag": 13.3, "assistant_lag": 53.9},
        {"row_lag": 5.0},
        {"row_lag": 73.0},
        # Longest archived user-row delay (573 s), inside the 600 s ceiling.
        {"row_lag": 572.0, "turn_len": 900.0},
        # Working turn with PTY-quiet blips.
        {"row_lag": 120.0,
         "quiet_gaps": ((5.0, 25.0), (40.0, 65.0), (80.0, 105.0))},
        # TUI still booting (animated) when the prompt is POSTed.
        {"ready_at": 20.0, "boot_active": True, "row_lag": 35.0},
        # Broker without the pending_inputs field.
        {"row_lag": 35.0, "expose_pending": False},
    ],
    ids=["canary-lag35", "live-s1", "lag5", "lag73", "lag573",
         "quiet-blips", "booting-animated", "no-pending-field"],
)
def test_intact_prompt_runs_exactly_once(clock, shape):
    broker = Native014Broker(clock, **shape)

    assert _create(broker) is None
    assert len(broker.posts) == 1
    assert broker.turns == [PROMPT], "duplicate turn from a re-post"
    assert broker.deletes == []
    assert broker.pi.snapshot_for(SID) == []


# -- known miss: every copy has a row, none intact -> bounded re-post ---

def test_live_s2_head_loss_reposts_once_and_lands_at_43s(clock):
    """Row 1 lost 3,885 head chars and got a reply; copy 2 lands ~+43 s.

    The re-post is needed. A third POST while copy 2 waits behind the
    reply to the garbage would run later as a duplicate turn (live POST
    #3 at 41.5 s).
    """
    broker = Native014Broker(
        clock, corrupt=("head",), row_lag=5.0, turn_len=45.0
    )

    assert _create(broker, PADDED) is None
    assert broker.turns[0].endswith("[201~")
    # Live POST #2 went out at +21.4 s, on the mangled row.
    assert broker.posts[0] == 0.0 and 20.5 < broker.posts[1] < 22.0
    assert len(broker.posts) == 2
    assert broker.turns[1:] == [PADDED], "duplicate intact turn"
    assert broker.rows[-2]["role"] == "user"  # intact row ~ +43 s
    assert broker.deletes == []


@pytest.mark.parametrize("mode", ["head", "tail", "prefix", "empty"])
def test_mangled_row_is_a_known_miss_even_with_a_reply(clock, mode):
    """No row shortcut: an assistant reply to a mangled row is not landed.

    The mangled row lands at +5 s, inside the old 20 s window, so an
    old prefix/substring/empty-row match or row shortcut accepts it.
    """
    broker = Native014Broker(
        clock, corrupt=(mode,), corrupt_row_lag=5.0, row_lag=5.0
    )

    assert _create(broker, PADDED) is None
    assert len(broker.posts) == 2
    assert broker.turns[1:] == [PADDED]


def test_every_copy_mangled_fails_closed_after_attempts(clock):
    broker = Native014Broker(
        clock, corrupt=("head", "tail", "prefix"), row_lag=5.0
    )

    error = _create(broker, PADDED)

    assert isinstance(error, BrokerHttpError)
    assert not isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
    assert "did not land intact" in str(error)
    assert len(broker.posts) == 3
    assert PADDED not in broker.turns
    assert broker.deletes == [f"/v1/sessions/{SID}"]


def _accept_then_502(broker: Native014Broker, *which: int) -> None:
    """POST #n in ``which``: the runner accepted the copy (the turn will
    run), but the server's forward saw ReadTimeout / a tunnel drop ->
    502 and rolled its pending entry back (0.14 orchestration.py
    _forward_native_terminal_message; the rollback never reaches the
    runner)."""
    original = Native014Broker.send_message

    def send(self, session_id, message):
        n = len(self.posts) + 1
        original(self, session_id, message)
        if n in which:
            self.pi.resolve_oldest(SID)
            raise BrokerHttpError(
                "POST events failed with HTTP 502", status_code=502
            )

    broker.send_message = send.__get__(broker)


def test_never_forwarded_502_is_held_not_deleted(clock):
    """A 502 cannot be told apart from accepted-then-502 (G1d): even a
    copy that never reached the runner ends held after the wait."""
    broker = Native014Broker(clock, forward_fails=True)

    error = _create(broker)

    assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
    assert error.session_id == SID
    assert broker.pi.snapshot_for(SID) == []
    assert broker.turns == []
    assert broker.posts == [0.0]
    assert broker.deletes == []


def test_initial_post_502_after_runner_accepted_is_not_deleted(clock):
    """G1a: the copy ran although the POST answered 502."""
    broker = Native014Broker(clock, row_lag=35.0)
    _accept_then_502(broker, 1)

    error = _create(broker)

    assert broker.turns == [PROMPT]
    assert error is None or isinstance(
        error, trioctl.broker_http.PromptDeliveryUncertain
    )
    assert len(broker.posts) == 1
    assert broker.deletes == []


def test_corrective_post_502_after_runner_accepted_is_not_deleted(clock):
    """G1b: same for the corrective re-post after a known miss."""
    broker = Native014Broker(
        clock, corrupt=("head",), row_lag=5.0, turn_len=45.0
    )
    _accept_then_502(broker, 2)

    error = _create(broker, PADDED)

    assert broker.turns.count(PADDED) == 1
    assert len(broker.posts) == 2
    assert not isinstance(error, trioctl.broker_http.PromptDeliveryFailed)
    assert broker.deletes == []


# -- uncertain: an unaccounted copy -> no re-post, no delete ------------

@pytest.mark.parametrize(
    "shape",
    [
        # Welcome-screen drop, static pane: pending stays, status idle.
        {"ready_at": None},
        # Same, animated boot: pending stays, status running.
        {"ready_at": None, "boot_active": True},
        # Accepted and running, but no row ever mirrored.
        {"writes_rows": False, "turn_len": 5000.0},
        # Runner lost mid-turn (it may already have run tools).
        {"unbind_at": 2.0, "rebind_at": 50.0},
    ],
    ids=["stuck-pending-idle", "stuck-pending-running", "running-no-row",
         "unbound"],
)
def test_unaccounted_copy_is_uncertain_not_reposted_or_deleted(clock, shape):
    broker = Native014Broker(clock, **shape)

    error = _create(broker)

    assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
    assert error.session_id == SID
    assert broker.posts == [0.0]
    assert 599.0 < clock.now - 2000.0 < 602.0  # the 600 s ceiling
    assert broker.deletes == []


def test_prompt_timeout_caps_the_uncertain_wait(clock):
    broker = Native014Broker(clock, ready_at=None)

    with pytest.raises(trioctl.broker_http.PromptDeliveryUncertain):
        broker.create_session(
            "agent", "model", PROMPT, "title", prompt_timeout=120.0
        )

    assert 119.0 < clock.now < 122.0
    assert broker.deletes == []


def test_ambiguous_first_post_that_was_queued_lands_once(clock):
    """F1a: timeout after the broker queued it -- a counted copy, never
    deleted or re-posted; its intact row proves delivery."""
    broker = Native014Broker(clock, ambiguous_posts=("queued",))

    assert _create(broker) is None
    assert len(broker.posts) == 1
    assert broker.turns == [PROMPT]
    assert broker.deletes == []


def test_ambiguous_first_post_that_never_arrived_is_held(clock):
    broker = Native014Broker(clock, ambiguous_posts=("lost",))

    error = _create(broker)

    assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
    assert error.session_id == SID
    assert len(broker.posts) == 1
    assert broker.turns == []
    assert broker.deletes == []


@pytest.mark.parametrize("fate", ["queued", "lost"])
def test_ambiguous_corrective_post_never_deletes(clock, fate):
    """F1c: the corrective copy's outcome is unknown: count it, no DELETE."""
    broker = Native014Broker(
        clock, corrupt=("head",), row_lag=5.0, turn_len=45.0,
        ambiguous_posts=(None, fate),
    )

    error = _create(broker, PADDED)

    assert len(broker.posts) == 2
    assert broker.deletes == []
    if fate == "queued":
        assert error is None and broker.turns[1:] == [PADDED]
    else:
        assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)


@pytest.mark.parametrize("status", [409, 502])
def test_corrective_rejection_only_4xx_fails_and_deletes(clock, status):
    """Every earlier copy is accounted for (mangled) and the corrective
    POST is refused with nothing forwarded. Only a 4xx is definite (0.14
    raises none after the forward); a 502 is held, not deleted."""
    broker = Native014Broker(clock, corrupt=("head",), row_lag=5.0)
    original = Native014Broker.send_message

    def refuse_second(self, session_id, message):
        if self.posts:
            self.posts.append(round(self.clock.now, 1))
            raise BrokerHttpError(f"HTTP {status}", status_code=status)
        return original(self, session_id, message)

    broker.send_message = refuse_second.__get__(broker)
    error = _create(broker, PADDED)

    if status < 500:
        assert isinstance(error, trioctl.broker_http.PromptDeliveryFailed)
        assert error.status_code == status
        assert broker.deletes == [f"/v1/sessions/{SID}"]
    else:
        assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
        assert broker.deletes == []
    assert len(broker.posts) == 2


@pytest.mark.parametrize("status", [500, 502, 503, None])
def test_transient_get_errors_keep_polling(clock, status):
    """F1b: a failed items read (any status, or a socket error after the
    GET retries) is not a verdict: keep polling to the deadline."""
    broker = Native014Broker(clock, row_lag=35.0)
    original = Native014Broker.get_items
    calls = {"n": 0}

    def flaky(self, session_id, **kwargs):
        calls["n"] += 1
        if 3 <= calls["n"] <= 30:
            raise BrokerHttpError("GET items failed", status_code=status)
        return original(self, session_id, **kwargs)

    broker.get_items = flaky.__get__(broker)

    assert _create(broker) is None
    assert len(broker.posts) == 1
    assert broker.turns == [PROMPT]
    assert broker.deletes == []


def test_unexpected_error_after_post_is_held_not_deleted(clock):
    """F1d: any other exception once a copy may be queued -> uncertain."""
    broker = Native014Broker(clock, row_lag=35.0)

    def boom(self, session_id, **kwargs):
        raise ValueError("decoder exploded")

    broker.get_items = boom.__get__(broker)
    error = _create(broker)

    assert isinstance(error, trioctl.broker_http.PromptDeliveryUncertain)
    assert "ValueError" in str(error)
    assert broker.deletes == []


@pytest.mark.parametrize(
    "interval", ["nan", "inf", "-1", "0", "bogus"]
)
def test_bad_interval_falls_back_without_delete(clock, monkeypatch, interval):
    """F3a: a real time.sleep(nan|-1) raises; the interval must be > 0."""
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", interval)
    sleeps: list[float] = []
    virtual_sleep = trioctl.broker_http.time.sleep

    def strict_sleep(seconds):
        if not (seconds >= 0) or seconds == float("inf"):
            raise ValueError("sleep length must be non-negative")
        sleeps.append(seconds)
        virtual_sleep(seconds)

    monkeypatch.setattr(trioctl.broker_http.time, "sleep", strict_sleep)
    broker = Native014Broker(clock, row_lag=35.0)

    assert _create(broker) is None
    assert broker.deletes == []
    assert sleeps and all(0 < s <= 0.4 for s in sleeps)


@pytest.mark.parametrize("wait", ["nan", "inf", "-5"])
def test_bad_wait_stays_bounded_by_role_timeout(clock, monkeypatch, wait):
    """F3b: min(nan, cap) is nan -- non-finite waits fall back, negative
    clamps to 0; the role cap always applies."""
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", wait)
    broker = Native014Broker(clock, ready_at=None)

    with pytest.raises(trioctl.broker_http.PromptDeliveryUncertain):
        broker.create_session(
            "agent", "model", PROMPT, "title", prompt_timeout=120.0
        )

    assert clock.now <= 121.0
    assert broker.deletes == []


@pytest.mark.parametrize("cap", [float("nan"), float("inf"), -3.0])
def test_bad_prompt_timeout_is_ignored_or_clamped(clock, cap):
    broker = Native014Broker(clock, ready_at=None)

    with pytest.raises(trioctl.broker_http.PromptDeliveryUncertain):
        broker.create_session(
            "agent", "model", PROMPT, "title", prompt_timeout=cap
        )

    assert clock.now <= (1.0 if cap < 0 else 601.0)
    assert broker.deletes == []


def test_prompt_phase_has_one_total_deadline(clock):
    """F4: corrective POSTs do not restart the prompt budget."""
    broker = Native014Broker(
        clock, corrupt=("head", "head"), corrupt_row_lag=110.0,
        row_lag=110.0, corrupt_turn_len=200.0, turn_len=5000.0,
    )

    with pytest.raises(trioctl.broker_http.PromptDeliveryUncertain):
        broker.create_session(
            "agent", "model", PADDED, "title", prompt_timeout=120.0
        )

    assert clock.now <= 121.0
    assert len(broker.posts) == 2
    assert broker.deletes == []


# -- end to end through OmnigentRunner and the loop wrapper ------------

def _mailbox(tmp_path: Path) -> Path:
    mailbox = tmp_path / "loop-natural-trial"
    mailbox.mkdir()
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Log\n", encoding="utf-8")
    return mailbox


def _runner(tmp_path: Path, broker, monkeypatch, timeout: float = 3000.0):
    # interval=1: live idle dwell (30 s) on the virtual clock.
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, broker_client=broker, interval=1, timeout=timeout
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "evaluator-agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: PROMPT)
    # These fakes compare delivered text to PROMPT exactly; the dispatch
    # nonce is covered by test_reconcile_held.py.
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)
    return runner


CONTEXT = {"evaluator_attempt": ATTEMPT, "pinned_sha": PIN}


def test_evaluator_ship_is_not_followed_by_a_duplicate_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """Replay 6f173b9d end to end: one prompt, one turn, fresh SHIP."""
    mailbox = _mailbox(tmp_path)
    broker = Native014Broker(clock, row_lag=35.0, mailbox=mailbox)
    runner = _runner(tmp_path, broker, monkeypatch)

    assert runner.run("evaluator", 1, mailbox, CONTEXT) == 0
    broker.run_until(clock.now + 2000.0)

    assert broker.turns == [PROMPT], "duplicate Evaluator turn after SHIP"
    assert len(broker.posts) == 1
    verdict = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"attempt: {ATTEMPT}" in verdict
    assert runner.held_session_ids == []


def test_uncertain_delivery_fails_the_role_and_holds_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    mailbox = _mailbox(tmp_path)
    broker = Native014Broker(clock, ready_at=None, mailbox=mailbox)
    runner = _runner(tmp_path, broker, monkeypatch, timeout=300.0)

    with pytest.raises(trioctl.TrioctlError, match="unaccounted"):
        runner.run("evaluator", 1, mailbox, CONTEXT)

    assert runner.held_session_ids == [SID]
    assert broker.posts == [0.0]
    assert broker.deletes == []
    assert (mailbox / "VERDICT.md").read_text(encoding="utf-8") == (
        "VERDICT: none\n"
    )
    record = json.loads(
        (mailbox / ".sessions" / f"held-{SID}.json").read_text("utf-8")
    )
    assert record["session_id"] == SID
    assert (record["role"], record["iteration"]) == ("evaluator", 1)
    assert (record["attempt"], record["pinned_sha"]) == (ATTEMPT, PIN)

    # The runner itself refuses any further role for this mailbox.
    with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
        runner.run("lead", 2, mailbox)
    assert broker.posts == [0.0]


def test_loop_prune_skips_held_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The run's ids file lists the held session; prune must not get it."""
    mailbox = _mailbox(tmp_path)
    pruned: list[list[str]] = []

    class HeldRunner:
        created_session_ids = ["done-1"]
        held_session_ids = [SID]
        session_ids: dict[str, str] = {}

    class RaisingLoop:
        @staticmethod
        def run_loop(mailbox_path, *args, **kwargs):
            ids = mailbox_path / ".sessions"
            ids.mkdir(exist_ok=True)
            for path in ids.glob("run-*.ids"):
                path.unlink()
            (ids / f"run-{trioctl.os.getpid()}.ids").write_text(
                f"done-1\n{SID}\n", encoding="utf-8"
            )
            raise trioctl.TrioctlError("first prompt unaccounted")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: RaisingLoop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: HeldRunner())
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "1"]
    )

    with pytest.raises(trioctl.TrioctlError):
        args.func(args)

    assert pruned == [["done-1", "done-1"]]
    assert f"kept session {SID}" in capsys.readouterr().err


def test_stale_verdict_is_not_accepted_as_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """Idle/exit is not SHIP: a stale attempt's verdict times out."""
    mailbox = _mailbox(tmp_path)
    stale = (
        "VERDICT: SHIP\niteration: 1\nattempt: 0000stale\n"
        f"evaluated: {PIN}\n"
    )
    (mailbox / "VERDICT.md").write_text(stale, encoding="utf-8")
    broker = Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    runner = _runner(tmp_path, broker, monkeypatch, timeout=600.0)

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        runner.run("evaluator", 1, mailbox, CONTEXT)

    assert len(broker.posts) == 1
    assert (mailbox / "VERDICT.md").read_text(encoding="utf-8") == stale


def test_wrong_pin_verdict_is_not_accepted_as_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    mailbox = _mailbox(tmp_path)
    broker = Native014Broker(
        clock, row_lag=35.0, turn_len=100.0, mailbox=mailbox
    )
    runner = _runner(tmp_path, broker, monkeypatch, timeout=600.0)
    context = {"evaluator_attempt": ATTEMPT, "pinned_sha": "f" * 40}

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        runner.run("evaluator", 1, mailbox, context)

    assert len(broker.posts) == 1


def test_held_dispatch_survives_cleanup_and_blocks_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock, capsys
) -> None:
    """Staging finding: after the uncertain exit, nothing durable stayed
    and STATE still read lead-running, so a resume could start a second
    Lead. The hold is now on disk, survives cleanup, and stops the next
    invocation until a person removes it."""
    mailbox = _mailbox(tmp_path)
    state_md = "iteration: 1\nstatus: running\nphase: lead-running\n"
    (mailbox / "STATE.md").write_text(state_md, encoding="utf-8")
    broker = Native014Broker(clock, ready_at=None)
    pruned: list[list[str]] = []
    loop_calls: list[str] = []

    real_runner = trioctl.OmnigentRunner

    def make_runner(**kwargs):
        runner = real_runner(
            repo=kwargs["repo"], broker_client=broker, interval=1,
            timeout=300.0,
        )
        runner._agent_id = lambda role: "lead-agent"
        runner._resolve_model = lambda role: "m"
        runner._prompt = lambda *a, **k: PROMPT
        return runner

    class LeadLoop:
        @staticmethod
        def run_loop(mailbox_path, max_iterations, runner, **kwargs):
            loop_calls.append("run_loop")
            return runner.run("lead", 1, mailbox_path)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: LeadLoop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", make_runner)
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "1"]
    )

    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        args.func(args)

    record_path = mailbox / ".sessions" / f"held-{SID}.json"
    assert json.loads(record_path.read_text("utf-8"))["role"] == "lead"
    assert pruned == [[]]  # held id kept out of cleanup
    assert list((mailbox / ".sessions").glob("run-*.ids")) == []
    assert broker.deletes == []

    held_state = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: needs_human" in held_state
    assert "phase: needs_human" in held_state
    assert "iteration: 1" in held_state
    assert f"held lead session {SID}" in (mailbox / "LOG.md").read_text(
        encoding="utf-8"
    )

    # Resume: refused before the loop core runs; no POST, STATE untouched.
    capsys.readouterr()
    assert args.func(args) == trioctl.HELD_DISPATCH_EXIT
    err = capsys.readouterr().err
    assert f"session {SID} (role lead, iteration 1)" in err
    assert str(record_path) in err
    assert loop_calls == ["run_loop"]
    assert broker.posts == [0.0]
    assert (mailbox / "STATE.md").read_text(encoding="utf-8") == held_state

    # A person reconciled it: record removed, STATE set to the next step.
    record_path.unlink()
    (mailbox / "STATE.md").write_text(state_md, encoding="utf-8")
    with pytest.raises(trioctl.TrioctlError):
        args.func(args)
    assert loop_calls == ["run_loop", "run_loop"]
    assert len(broker.posts) == 2


@pytest.mark.parametrize("phase", ["lead-running", "lead-done"])
def test_run_loop_resume_refuses_lead_and_evaluator_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock, phase
) -> None:
    """F2 via the real loop core (not command_loop): after an uncertain
    dispatch, neither a Lead resume nor an Evaluator resume creates a
    session -- STATE is needs_human and the held record blocks the
    runner even if STATE is reset without clearing it."""
    core = trioctl._load_trio_loop(HERE.parents[1])
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    first = Native014Broker(clock, ready_at=None)
    runner = _runner(tmp_path, first, monkeypatch, timeout=300.0)
    with pytest.raises(trioctl.TrioctlError, match="held dispatch"):
        core.run_loop(mailbox, 3, runner, repo=None)
    assert (mailbox / ".sessions" / f"held-{SID}.json").is_file()
    assert "status: needs_human" in (mailbox / "STATE.md").read_text("utf-8")

    second = Native014Broker(clock, row_lag=5.0)
    creates: list[str] = []
    original = second.create_session
    second.create_session = lambda *a, **k: (creates.append(a[0]), original(*a, **k))[1]
    resume = _runner(tmp_path, second, monkeypatch, timeout=60.0)
    assert core.run_loop(mailbox, 3, resume, repo=None) == 5  # needs_human
    assert creates == []

    # STATE hand-reset but the held record left in place: still refused.
    (mailbox / "STATE.md").write_text(
        f"iteration: 1\nstatus: running\nphase: {phase}\n"
        f"evaluated_sha: {PIN}\nevaluator_attempt: {ATTEMPT}\n",
        encoding="utf-8",
    )
    with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
        core.run_loop(mailbox, 3, resume, repo=None)
    assert creates == []


def test_resume_after_accepted_502_does_not_dispatch_second_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """G1c: a Lead copy accepted then answered 502, with no user row by
    the end of the wait, is held (STATE needs_human, no DELETE), so a
    resume creates no second Lead while it runs. (If the row mirrors in
    time the copy counts as landed, exactly as after a 202.)"""
    core = trioctl._load_trio_loop(HERE.parents[1])
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    first = Native014Broker(clock, row_lag=500.0)
    _accept_then_502(first, 1)
    runner = _runner(tmp_path, first, monkeypatch, timeout=120.0)
    with pytest.raises(trioctl.TrioctlError, match="held dispatch"):
        core.run_loop(mailbox, 3, runner, repo=None)
    first.run_until(clock.now + 1000)
    assert first.turns == [PROMPT]  # the first Lead copy runs
    assert first.deletes == []
    assert (mailbox / ".sessions" / f"held-{SID}.json").is_file()
    assert "status: needs_human" in (mailbox / "STATE.md").read_text("utf-8")

    second = Native014Broker(clock, row_lag=5.0)
    creates: list[str] = []
    original = second.create_session
    second.create_session = lambda *a, **k: (creates.append(a[0]), original(*a, **k))[1]
    resume = _runner(tmp_path, second, monkeypatch, timeout=60.0)
    assert core.run_loop(mailbox, 3, resume, repo=None) == 5  # needs_human
    assert creates == []


def test_held_record_write_failure_still_marks_needs_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock
) -> None:
    """N1: no durable record (OSError) -> STATE still set to needs_human."""
    mailbox = _mailbox(tmp_path)
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n",
        encoding="utf-8",
    )
    broker = Native014Broker(clock, ready_at=None)
    runner = _runner(tmp_path, broker, monkeypatch, timeout=300.0)

    def fail(mailbox, record):
        raise OSError("disk full")

    monkeypatch.setattr(trioctl, "_write_held_record", fail)
    with pytest.raises(trioctl.TrioctlError, match="could NOT record"):
        runner.run("lead", 1, mailbox)

    state = (mailbox / "STATE.md").read_text("utf-8")
    assert "status: needs_human" in state and "running" not in state
    assert broker.deletes == []


def test_unreadable_held_record_skips_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    mailbox = _mailbox(tmp_path)
    pruned: list[list[str]] = []

    class Runner:
        created_session_ids = ["other-1"]
        held_session_ids: list[str] = []
        session_ids: dict[str, str] = {}

    class RaisingLoop:
        @staticmethod
        def run_loop(mailbox_path, *args, **kwargs):
            sessions = mailbox_path / ".sessions"
            sessions.mkdir(exist_ok=True)
            (sessions / "held-x.json").write_text("{truncated", "utf-8")
            raise trioctl.TrioctlError("held")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: RaisingLoop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: Runner())
    monkeypatch.setattr(
        trioctl,
        "_run_post_loop_session_prune",
        lambda mailbox, base_url, ids, **kw: pruned.append(sorted(ids)),
    )
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox),
         "--max-iterations", "1"]
    )

    with pytest.raises(trioctl.TrioctlError):
        args.func(args)

    assert pruned == [[]]
    assert "skipping session prune" in capsys.readouterr().err
    assert args.func(args) == trioctl.HELD_DISPATCH_EXIT

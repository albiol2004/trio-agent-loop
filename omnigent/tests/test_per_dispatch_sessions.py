"""Per-dispatch session bookkeeping: concurrent evaluator dispatches on
one OmnigentRunner must each wait on (and hold) their OWN session, never
the role's last-created one."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from omnigent.tests.test_omnigent_loop import load_trioctl, make_mailbox, profile


class ConcurrentClient:
    """Two creates; the first session's wait blocks until the second exists."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.created: list[str] = []
        self.second_created = threading.Event()
        self.waits: list[tuple[str, str]] = []  # (thread name, session id)

    def create(self, agent_id, model, message, title):
        with self.lock:
            sid = f"s-{len(self.created) + 1}"
            self.created.append(sid)
            if len(self.created) == 2:
                self.second_created.set()
        return {"id": sid}

    def wait_session(self, session_id, timeout=None, interval=None):
        with self.lock:
            self.waits.append((threading.current_thread().name, session_id))
        assert self.second_created.wait(5), "second dispatch never created"
        return {"id": session_id, "status": "idle"}

    def get_items(self, session_id, **_kw):
        return {"items": [{"role": "assistant", "status": "completed"}]}


def _runner(trioctl, tmp_path, client):
    runner = trioctl.OmnigentRunner(
        tmp_path, client=client, config=profile(), timeout=30.0, interval=0,
    )
    runner._agent_id = lambda role: f"{role}-agent"
    runner._prompt = lambda role, iteration, mailbox, context: "prompt"
    return runner


def _context(slice_id: str) -> dict:
    return {"mode": "open-loop", "kind": "slice-eval", "slice": slice_id, "sha": "abc"}


def test_concurrent_evaluators_wait_on_their_own_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    client = ConcurrentClient()
    runner = _runner(trioctl, tmp_path, client)
    ready_calls: dict[str, int] = {}
    snapshots: list[dict] = []

    def fake_ready(mailbox, role, iteration, before_text, before_mtime, context):
        # First check per slice is "not yet": _wait_for_role_artifact must
        # re-enter the wait on this dispatch's own session.
        name = context["slice"]
        ready_calls[name] = ready_calls.get(name, 0) + 1
        if ready_calls[name] == 2:
            snapshots.append(runner.inflight_sessions())
        return ready_calls[name] > 2

    monkeypatch.setattr(trioctl, "_role_artifact_ready", fake_ready)
    codes: dict[str, int] = {}

    def go(slice_id: str) -> None:
        codes[slice_id] = runner.run("evaluator", 1, mailbox, _context(slice_id))

    threads = [threading.Thread(target=go, args=(s,), name=f"eval-{s}") for s in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert codes == {"a": 0, "b": 0}

    # Each thread only ever waited on the session its own create returned.
    by_thread: dict[str, set[str]] = {}
    for name, sid in client.waits:
        by_thread.setdefault(name, set()).add(sid)
    assert all(len(sids) == 1 for sids in by_thread.values()), by_thread
    assert {next(iter(v)) for v in by_thread.values()} == {"s-1", "s-2"}
    # In flight, both sessions were visible with their slice metadata.
    assert any(
        {m.get("slice") for m in snap.values()} == {"a", "b"} for snap in snapshots
    ), snapshots
    # Done: nothing in flight, last-dispatch back-compat value kept.
    assert runner.inflight_sessions() == {}
    assert runner.session_ids["evaluator"] in {"s-1", "s-2"}
    assert sorted(runner.created_session_ids) == ["s-1", "s-2"]
    assert runner._delivery == {}


def test_timeout_holds_this_dispatchs_session_not_the_last_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    runner = _runner(trioctl, tmp_path, ConcurrentClient())
    monkeypatch.setattr(trioctl, "_role_artifact_ready", lambda *a, **k: False)
    # Pretend a concurrent evaluator created a later session meanwhile.
    runner.session_ids["evaluator"] = "someone-else"
    runner._timeout = 0.0
    with pytest.raises(trioctl.TrioctlError) as err:
        runner._wait_for_role_artifact(
            runner._client(), "evaluator", 1, mailbox, 0.0, "", 0.0,
            _context("a"), session_id="mine",
        )
    assert "session mine " in str(err.value)
    assert "someone-else" not in str(err.value)


# `trioctl omnigent loop --slice-eval-concurrency N` plumbing ------------


def test_slice_eval_concurrency_flag_defaults_to_serial_and_passes_through() -> None:
    trioctl = load_trioctl()
    default = trioctl.parser().parse_args(["omnigent", "loop"])
    assert default.slice_eval_concurrency == 1

    class NewCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None,
                     poll_seconds=30, mode="auto", slice_eval_concurrency=1):
            return 0

    class OldCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None):
            return 0

    # Default: the run_loop call is unchanged (no new kwarg), any core.
    assert trioctl._slice_eval_concurrency_kwargs(OldCore, default) == {}
    three = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-concurrency", "3"]
    )
    assert trioctl._slice_eval_concurrency_kwargs(NewCore, three) == {
        "slice_eval_concurrency": 3
    }
    with pytest.raises(trioctl.TrioctlError, match="refused"):
        trioctl._slice_eval_concurrency_kwargs(OldCore, three)
    zero = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-concurrency", "0"]
    )
    with pytest.raises(trioctl.TrioctlError, match=">= 1"):
        trioctl._slice_eval_concurrency_kwargs(NewCore, zero)


def test_real_loop_core_accepts_slice_eval_concurrency() -> None:
    import inspect

    from metrics import trio_loop

    for fn in (trio_loop.run_loop, trio_loop.run_open_loop):
        assert "slice_eval_concurrency" in inspect.signature(fn).parameters

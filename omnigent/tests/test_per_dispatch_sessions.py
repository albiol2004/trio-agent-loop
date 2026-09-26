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


def test_slice_eval_drain_seconds_flag_passes_through() -> None:
    trioctl = load_trioctl()
    default = trioctl.parser().parse_args(["omnigent", "loop"])
    assert default.slice_eval_drain_seconds is None

    class NewCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None,
                     slice_eval_concurrency=1, slice_eval_drain_seconds=None):
            return 0

    class OldCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None,
                     slice_eval_concurrency=1):
            return 0

    assert trioctl._slice_eval_concurrency_kwargs(OldCore, default) == {}
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-drain-seconds", "30"]
    )
    assert trioctl._slice_eval_concurrency_kwargs(NewCore, args) == {
        "slice_eval_drain_seconds": 30.0
    }
    with pytest.raises(trioctl.TrioctlError, match="refused"):
        trioctl._slice_eval_concurrency_kwargs(OldCore, args)
    bad = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-drain-seconds", "-1"]
    )
    with pytest.raises(trioctl.TrioctlError, match=">= 0"):
        trioctl._slice_eval_concurrency_kwargs(NewCore, bad)


def test_real_loop_core_accepts_slice_eval_concurrency() -> None:
    import inspect

    from metrics import trio_loop

    for fn in (trio_loop.run_loop, trio_loop.run_open_loop):
        assert "slice_eval_concurrency" in inspect.signature(fn).parameters


# F1: a slice-eval's readiness is scoped to its OWN section ---------------

FULL_SHA = "abc1234def5678abc1234def5678abc1234def56"


class SiblingAppendClient:
    """A goes idle WITHOUT writing after sibling B appended its section;
    A's re-entered wait parks until released, then A appends its own."""

    def __init__(self, verdict: Path) -> None:
        self.verdict = verdict
        self.lock = threading.Lock()
        self.by_session: dict[str, str] = {}
        self.a_waits = 0
        self.both_created = threading.Event()
        self.b_appended = threading.Event()
        self.a_reentered = threading.Event()
        self.release_a = threading.Event()

    def _append(self, slice_id: str, verdict: str = "SHIP") -> None:
        with self.lock, self.verdict.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## slice {slice_id} @{FULL_SHA} — {verdict}\nevidence\n")

    def create(self, agent_id, model, message, title):
        with self.lock:
            sid = f"s-{len(self.by_session) + 1}"
            self.by_session[sid] = message.split()[-1]
            if len(self.by_session) == 2:
                self.both_created.set()
        return {"id": sid}

    def wait_session(self, session_id, timeout=None, interval=None):
        assert self.both_created.wait(5)
        slice_id = self.by_session[session_id]
        if slice_id == "b":
            self._append("b")
            self.b_appended.set()
            return {"id": session_id, "status": "idle"}
        with self.lock:
            self.a_waits += 1
            first = self.a_waits == 1
        if first:  # idle without writing, after B's append
            assert self.b_appended.wait(5)
            return {"id": session_id, "status": "idle"}
        self.a_reentered.set()
        assert self.release_a.wait(5)
        self._append("a")
        return {"id": session_id, "status": "idle"}

    def get_items(self, session_id, **_kw):
        return {"items": [{"role": "assistant", "status": "completed"}]}


def _slice_runner(trioctl, tmp_path, client):
    runner = _runner(trioctl, tmp_path, client)
    runner._prompt = lambda role, it, mb, ctx: f"prompt {ctx['slice']}"
    return runner


def _slice_ctx(slice_id: str, sha: str = FULL_SHA) -> dict:
    return {"mode": "open-loop", "kind": "slice-eval", "slice": slice_id, "sha": sha}


def test_sibling_append_does_not_satisfy_idle_slice_eval(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    client = SiblingAppendClient(mailbox / "VERDICT.md")
    runner = _slice_runner(trioctl, tmp_path, client)
    codes: dict[str, int] = {}

    def go(slice_id: str) -> None:
        codes[slice_id] = runner.run("evaluator", 1, mailbox, _slice_ctx(slice_id))

    threads = {s: threading.Thread(target=go, args=(s,), name=f"eval-{s}") for s in "ab"}
    for t in threads.values():
        t.start()
    threads["b"].join(10)
    assert codes.get("b") == 0
    # A went idle without writing; B's append must not be accepted for A.
    assert client.a_reentered.wait(5), "A did not re-enter its own wait"
    assert threads["a"].is_alive() and "a" not in codes
    assert "## slice a @" not in (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    client.release_a.set()
    threads["a"].join(10)
    assert not threads["a"].is_alive()
    assert codes == {"a": 0, "b": 0}


def _slice_ready(trioctl, tmp_path, text, *, slice_id, sha=FULL_SHA,
                 before="# Verdicts\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mailbox = make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text(text, encoding="utf-8")
    return trioctl._role_artifact_ready(
        mailbox, "evaluator", 1, before, 0.0, _slice_ctx(slice_id, sha)
    )


def test_slice_eval_ready_requires_own_slice_and_sha(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    base = "# Verdicts\n"
    other_sha = "f" * 40
    # Same slice at a different sha: not this dispatch.
    assert not _slice_ready(
        trioctl, tmp_path / "1", base + f"## slice a @{other_sha} — SHIP\n",
        slice_id="a",
    )
    # Different slice at the same sha: not this dispatch.
    assert not _slice_ready(
        trioctl, tmp_path / "2", base + f"## slice b @{FULL_SHA} — SHIP\n",
        slice_id="a",
    )
    # 7-char heading prefix of the full context sha matches.
    assert _slice_ready(
        trioctl, tmp_path / "3", base + f"## slice a @{FULL_SHA[:7]} — SHIP\n",
        slice_id="a",
    )
    # Short context sha, full heading sha: prefix either way.
    assert _slice_ready(
        trioctl, tmp_path / "4", base + f"## slice a @{FULL_SHA} — SHIP\n",
        slice_id="a", sha=FULL_SHA[:7],
    )
    # ITERATE suffix matches.
    assert _slice_ready(
        trioctl, tmp_path / "5", base + f"## slice a @{FULL_SHA} — ITERATE\n",
        slice_id="a",
    )
    # Own section present but text unchanged (stale): not fresh.
    stale = base + f"## slice a @{FULL_SHA} — SHIP\n"
    assert not _slice_ready(trioctl, tmp_path / "6", stale, before=stale, slice_id="a")


class SingleAppendClient:
    def __init__(self, verdict: Path) -> None:
        self.verdict = verdict
        self.waits = 0

    def create(self, agent_id, model, message, title):
        return {"id": "s-1"}

    def wait_session(self, session_id, timeout=None, interval=None):
        self.waits += 1
        with self.verdict.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## slice solo @{FULL_SHA[:7]} — SHIP\n")
        return {"id": session_id, "status": "idle"}

    def get_items(self, session_id, **_kw):
        return {"items": [{"role": "assistant", "status": "completed"}]}


def test_single_slice_eval_appender_unchanged(tmp_path: Path) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    client = SingleAppendClient(mailbox / "VERDICT.md")
    runner = _slice_runner(trioctl, tmp_path, client)
    assert runner.run("evaluator", 1, mailbox, _slice_ctx("solo")) == 0
    assert client.waits == 1


def test_abandoned_on_exit_hold_blocks_resume_with_reason(tmp_path: Path) -> None:
    from metrics import trio_loop

    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)
    path = trio_loop._write_abandoned_hold(mailbox, "s-9", "alpha", FULL_SHA, 3, 120.0)
    assert path == trioctl._held_record_path(mailbox, "s-9")
    assert trioctl._held_session_ids(mailbox) == {"s-9"}
    message = trioctl._held_dispatch_message(mailbox)
    assert "session s-9 (role evaluator, iteration 3" in message
    assert "slice-eval still running when the loop exited" in message
    assert f"slice-eval alpha @{FULL_SHA}" in message

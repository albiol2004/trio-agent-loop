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
def _isolation_not_a_factor(trioctl, monkeypatch) -> None:
    """r11: isolation is default ON and an explicit N>1 is refused when it is
    off. These tests exercise the slice-eval drain/cleanup plumbing in a
    plain tmp dir (no git checkout), so stub the isolation resolver to
    "no isolate config, not switched off" -- the pre-r11 call shape."""
    monkeypatch.setattr(trioctl, "_resolve_isolation", lambda args, repo, *a, **kw: (None, None))



def test_slice_eval_concurrency_flag_defaults_to_four_and_passes_through(capsys) -> None:
    trioctl = load_trioctl()
    default = trioctl.parser().parse_args(["omnigent", "loop"])
    # r11: "not given" is None and resolves to DEFAULT_SLICE_EVAL_CONCURRENCY.
    assert default.slice_eval_concurrency is None
    assert trioctl.DEFAULT_SLICE_EVAL_CONCURRENCY == 4

    class NewCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None,
                     poll_seconds=30, mode="auto", slice_eval_concurrency=1):
            return 0

    class OldCore:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None):
            return 0

    assert trioctl._slice_eval_concurrency_kwargs(NewCore, default) == {
        "slice_eval_concurrency": 4
    }
    # Default against an old core: serial, call unchanged, one warning.
    assert trioctl._slice_eval_concurrency_kwargs(OldCore, default) == {}
    assert trioctl.OLD_CORE_SERIAL_WARNING in capsys.readouterr().err
    one = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-concurrency", "1"]
    )
    assert trioctl._slice_eval_concurrency_kwargs(NewCore, one) == {}
    assert trioctl._slice_eval_concurrency_kwargs(OldCore, one) == {}
    assert capsys.readouterr().err == ""
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

    # r11: the concurrency default (4) passes through; no drain kwarg.
    assert trioctl._slice_eval_concurrency_kwargs(OldCore, default) == {
        "slice_eval_concurrency": 4
    }
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--slice-eval-drain-seconds", "30"]
    )
    assert trioctl._slice_eval_concurrency_kwargs(NewCore, args) == {
        "slice_eval_concurrency": 4, "slice_eval_drain_seconds": 30.0
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
    # N4: the abandoned hold never sets STATE to needs_human; say so.
    assert "was set to needs_human" not in message
    assert "abandoned_on_exit hold leaves it as the loop last wrote it" in message


# F3: interrupted drain -> cleanup never DELETEs a live worker's session --


def test_second_interrupt_during_drain_keeps_inflight_sessions_out_of_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import concurrent.futures
    import os
    import signal
    import time

    from metrics import trio_loop
    from metrics.tests.test_open_loop_concurrent_evals import TrackingEvalRunner, plan
    from metrics.tests.test_open_loop_driver import (
        QueueModel, ScriptedLeadRunner, VerdictModel, fake_sha, make_open_loop_mailbox,
    )

    trioctl = load_trioctl()
    ids = ("alpha", "beta")
    mailbox = make_open_loop_mailbox(tmp_path, plan(*ids))
    lock = threading.Lock()
    queue, verdict = QueueModel(mailbox, lock), VerdictModel(mailbox, lock)
    shas = {sid: fake_sha(f"{sid}-cleanup") for sid in ids}
    release = threading.Event()

    def grade(sid):
        def action(mb):
            assert release.wait(10)
            verdict.append_slice_section(sid, shas[sid], "SHIP")
        return action

    lead = ScriptedLeadRunner([lambda mb: [queue.retire(s, shas[s]) for s in ids]])
    evaluator = TrackingEvalRunner(slice_actions={(s, shas[s]): grade(s) for s in ids})
    eval_sids = [f"sess-{s}-{shas[s]}"[:40] for s in ids]

    class Runner:
        """Lead + evaluator; 's-ghost' is a live worker's session with no
        held record (e.g. created after the drain's snapshot)."""

        held_session_ids: list[str] = []

        def __init__(self) -> None:
            self.created_session_ids = ["s-lead", "s-ghost", *eval_sids]

        def run(self, role, iteration, mb, context=None):
            target = lead if role == "lead" else evaluator
            return target.run(role, iteration, mb, context)

        def inflight_sessions(self):
            live = evaluator.inflight_sessions()
            if not release.is_set():
                live["s-ghost"] = {"role": "evaluator", "kind": "slice-eval",
                                   "slice": "ghost", "sha": "0" * 40}
            return live

    runner = Runner()
    pruned: list[list[str]] = []
    monkeypatch.setattr(trio_loop, "_per_slice_gate", lambda *a, **k: 0)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: trio_loop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: runner)
    monkeypatch.setattr(
        trioctl, "_run_post_loop_session_prune",
        lambda mb, base_url, sids, **kw: pruned.append(sorted(sids)) or [],
    )
    real_wait = concurrent.futures.wait

    def wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        if timeout == 30.0:  # the exit drain: second SIGINT lands here
            os.kill(os.getpid(), signal.SIGINT)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(concurrent.futures, "wait", wait)
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)

    def first_sigint():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(evaluator.inflight_sessions()) < 2:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.chdir(tmp_path)
    _isolation_not_a_factor(trioctl, monkeypatch)
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox), "--max-iterations", "5",
         "--slice-eval-concurrency", "2", "--slice-eval-drain-seconds", "30"]
    )
    killer = threading.Thread(target=first_sigint, daemon=True)
    try:
        killer.start()
        assert args.func(args) == 130
        killer.join(5)
        assert not (mailbox / ".lock").exists()
        held = sorted(
            p.name for p in (mailbox / ".sessions").glob("held-*.json")
        )
        assert held == sorted(f"held-{sid}.json" for sid in eval_sids)
        # Only the finished Lead session reaches cleanup.
        assert pruned == [["s-lead"]]
        err = capsys.readouterr().err
        assert "skipped_inflight session s-ghost" in err
        for sid in eval_sids:
            assert f"kept session {sid}" in err
        # N1: the CLI does not wait for the abandoned eval threads.
        assert exits == [130]
        assert "exiting without joining 2 abandoned slice-eval thread(s)" in err
    finally:
        release.set()
    for t in [t for t in threading.enumerate() if t.name.startswith("slice-eval")]:
        t.join(5)
        assert not t.is_alive()


# F7: cleanup skips only in-flight slice-eval sessions ------------------


def test_ctrl_c_mid_lead_turn_prunes_lead_session_n1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Real loop core, N=1: SIGINT while the Lead is mid-turn (its thread's
    5 s join times out, so its session is still in flight) -> the Lead's
    session is DELETEd exactly as on 3b5b93b, never `skipped_inflight`."""
    import os
    import signal
    import time

    from metrics import trio_loop
    from metrics.tests.test_open_loop_concurrent_evals import plan
    from metrics.tests.test_open_loop_driver import make_open_loop_mailbox

    trioctl = load_trioctl()
    mailbox = make_open_loop_mailbox(tmp_path, plan("alpha"))
    release = threading.Event()

    class Runner:
        held_session_ids: list[str] = []

        def __init__(self) -> None:
            self.created_session_ids: list[str] = []
            self._inflight: dict[str, dict] = {}
            self.lock = threading.Lock()

        def run(self, role, iteration, mb, context=None):
            assert role == "lead"
            with self.lock:
                self.created_session_ids.append("s-lead-1")
                self._inflight["s-lead-1"] = {
                    "role": "lead", "kind": (context or {}).get("kind"),
                    "slice": None, "sha": None,
                }
            try:
                release.wait(30)
            finally:
                with self.lock:
                    self._inflight.pop("s-lead-1", None)
            return 0

        def inflight_sessions(self):
            with self.lock:
                return {k: dict(v) for k, v in self._inflight.items()}

    runner = Runner()
    pruned: list[list[str]] = []
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: trio_loop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: runner)
    monkeypatch.setattr(
        trioctl, "_run_post_loop_session_prune",
        lambda mb, base_url, sids, **kw: pruned.append(sorted(sids)) or [],
    )

    def sigint_mid_lead():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not runner.inflight_sessions():
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("os._exit called"))
    monkeypatch.chdir(tmp_path)
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox), "--max-iterations", "3"]
    )
    killer = threading.Thread(target=sigint_mid_lead, daemon=True)
    try:
        killer.start()
        assert args.func(args) == 130
        killer.join(5)
        # The Lead is still mid-turn when cleanup runs ...
        assert "s-lead-1" in runner.inflight_sessions()
        # ... and its session is still pruned.
        assert pruned == [["s-lead-1"]]
        assert "skipped_inflight" not in capsys.readouterr().err
    finally:
        release.set()


@pytest.mark.parametrize(
    ("concurrency", "inflight", "expect_pruned", "expect_skipped"),
    [
        pytest.param(
            2,
            {
                "s-lead": {"role": "lead", "kind": "lead-pass"},
                "s-eval": {"role": "evaluator", "kind": "slice-eval",
                           "slice": "alpha", "sha": "a" * 40},
            },
            ["s-done", "s-lead"],
            ["s-eval"],
            id="n2-slice-eval-skipped-lead-deleted",
        ),
        pytest.param(
            2,
            {"s-int": {"role": "evaluator", "kind": "integration-eval"}},
            ["s-done", "s-int"],
            [],
            id="integration-eval-deleted",
        ),
        pytest.param(
            1,
            {"s-lead": {"role": "lead", "kind": "lead-pass"},
             "s-repair": {"role": "lead", "kind": None}},
            ["s-done", "s-lead", "s-repair"],
            [],
            id="n1-lead-and-repair-deleted",
        ),
    ],
)
def test_cleanup_skips_only_inflight_slice_eval_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
    concurrency, inflight, expect_pruned, expect_skipped,
) -> None:
    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    class Core:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None,
                     slice_eval_concurrency=1, slice_eval_drain_seconds=None):
            raise KeyboardInterrupt

    class Runner:
        held_session_ids: list[str] = []
        created_session_ids = ["s-done", *inflight]

        def inflight_sessions(self):
            return {k: dict(v) for k, v in inflight.items()}

    pruned: list[list[str]] = []
    import os

    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("os._exit called"))
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: Core)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: Runner())
    monkeypatch.setattr(
        trioctl, "_run_post_loop_session_prune",
        lambda mb, base_url, sids, **kw: pruned.append(sorted(sids)) or [],
    )
    monkeypatch.chdir(tmp_path)
    _isolation_not_a_factor(trioctl, monkeypatch)
    argv = ["omnigent", "loop", "--mailbox", str(mailbox), "--max-iterations", "3"]
    if concurrency != 1:
        argv += ["--slice-eval-concurrency", str(concurrency)]
    args = trioctl.parser().parse_args(argv)
    assert args.func(args) == 130
    assert pruned == [expect_pruned]
    err = capsys.readouterr().err
    for sid in inflight:
        if sid in expect_skipped:
            assert f"skipped_inflight session {sid}" in err
        else:
            assert f"skipped_inflight session {sid}" not in err


# N1: abandoned slice-evals do not keep the CLI process alive -----------


def test_cli_exits_without_joining_abandoned_slice_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A slice-eval blocked on an event outlives a 0.3 s drain: the CLI
    returns within the drain budget + margin and calls `os._exit` with the
    loop's exit code (mocked here, so the call returns)."""
    import os
    import signal
    import time

    from metrics import trio_loop
    from metrics.tests.test_open_loop_concurrent_evals import TrackingEvalRunner, plan
    from metrics.tests.test_open_loop_driver import (
        QueueModel, ScriptedLeadRunner, fake_sha, make_open_loop_mailbox,
    )

    trioctl = load_trioctl()
    ids = ("alpha", "beta")
    mailbox = make_open_loop_mailbox(tmp_path, plan(*ids))
    queue = QueueModel(mailbox, threading.Lock())
    shas = {sid: fake_sha(f"{sid}-n1") for sid in ids}
    release = threading.Event()

    def block(mb):
        assert release.wait(30)

    lead = ScriptedLeadRunner([lambda mb: [queue.retire(s, shas[s]) for s in ids]])
    evaluator = TrackingEvalRunner(slice_actions={(s, shas[s]): block for s in ids})

    class Runner:
        held_session_ids: list[str] = []
        created_session_ids: list[str] = []

        def run(self, role, iteration, mb, context=None):
            target = lead if role == "lead" else evaluator
            return target.run(role, iteration, mb, context)

        def inflight_sessions(self):
            return evaluator.inflight_sessions()

    runner = Runner()
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    monkeypatch.setattr(trio_loop, "_per_slice_gate", lambda *a, **k: 0)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: trio_loop)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: runner)
    monkeypatch.setattr(
        trioctl, "_run_post_loop_session_prune", lambda *a, **kw: [],
    )
    sigint_at: list[float] = []

    def sigint_when_evals_run():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(evaluator.inflight_sessions()) < 2:
            time.sleep(0.01)
        sigint_at.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.chdir(tmp_path)
    _isolation_not_a_factor(trioctl, monkeypatch)
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox), "--max-iterations", "5",
         "--slice-eval-concurrency", "2", "--slice-eval-drain-seconds", "0.3"]
    )
    killer = threading.Thread(target=sigint_when_evals_run, daemon=True)
    try:
        killer.start()
        assert args.func(args) == 130
        elapsed = time.monotonic() - sigint_at[0]
        killer.join(5)
        assert elapsed < 0.3 + 2.0
        assert exits == [130]
        err = capsys.readouterr().err
        assert "exiting without joining 2 abandoned slice-eval thread(s)" in err
        # The eval threads really were still running (not joined).
        assert [t for t in threading.enumerate() if t.name.startswith("slice-eval")]
    finally:
        release.set()
    for t in [t for t in threading.enumerate() if t.name.startswith("slice-eval")]:
        t.join(5)
        assert not t.is_alive()


def test_cli_normal_exit_does_not_hard_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    trioctl = load_trioctl()
    mailbox = make_mailbox(tmp_path)

    class Core:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo=None, **kw):
            return 4

    class Runner:
        held_session_ids: list[str] = []
        created_session_ids: list[str] = []

    # A stray slice-eval-named thread alive BEFORE this run is not ours.
    stray_release = threading.Event()
    stray = threading.Thread(target=stray_release.wait, args=(10,),
                             name="slice-eval_stray", daemon=True)
    stray.start()
    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("os._exit called"))
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: Core)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: Runner())
    monkeypatch.setattr(trioctl, "_run_post_loop_session_prune", lambda *a, **kw: [])
    monkeypatch.chdir(tmp_path)
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(mailbox), "--max-iterations", "3"]
    )
    try:
        assert args.func(args) == 4
    finally:
        stray_release.set()
        stray.join(5)

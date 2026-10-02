"""Unit tests for trio_opencode.driver's builder-worktree bookkeeping."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from trio_opencode import driver, steplib

TOKEN = "oc-test-token"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-opencode-test", GIT_COMMITTER_EMAIL="t@example.test",
    )
    return env


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, env=_git_env())
    return r.stdout.strip()


def _ctx(repo: Path, mailbox: Path, exec_id: str) -> driver.RunContext:
    return driver.RunContext(
        root_mailbox=mailbox, live_mailbox=mailbox, repo=repo, cfg=object(),
        token=TOKEN, exec_id=exec_id, run_dir=repo / "run", log_dir=repo / "run" / "logs",
        env={}, root_free=False, lead_record=None, out=lambda s: None,
        cancel=threading.Event(),
    )


def test_ledger_records_builder_intent_even_if_git_worktree_add_fails(git_repo: Path) -> None:
    """The ownership ledger entry for a builder worktree must be written
    BEFORE `git worktree add`, so a crash/failure between the two leaves
    only a ledger entry whose branch/worktree never actually exists — which
    native/trio_native_step.py's `_reclaim_builders` (via
    `_previous_builders`/`_branch_sha`) tolerates fine (the candidate's tip
    comes back None, so it is released and skipped, nothing raises). The
    reverse order would leave a REAL worktree/branch the ledger never
    recorded, which `_reclaim_builders` could never find or clean up."""
    mailbox = git_repo / "loop"
    exec_id = "deadbeef" * 4
    ctx = _ctx(git_repo, mailbox, exec_id)
    head = _git(git_repo, "rev-parse", "HEAD")
    branch = f"trio-oc/{exec_id[:8]}/i1-bad"
    # Pre-create the branch so the subsequent `git worktree add -b <branch>`
    # fails (git refuses to reuse an existing branch name with -b).
    _git(git_repo, "branch", branch)

    with pytest.raises(driver.DriverStop):
        driver._create_builder_worktree(ctx, iteration=1, slice_id="bad", index=1,
                                        dispatch_head=head)

    ledger_path = steplib.ledger_path(mailbox, git_repo)
    assert ledger_path is not None and ledger_path.is_file()
    entries = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    assert any(e.get("branch") == branch and e.get("kind") == "builder" and e.get("id") == "bad"
              for e in entries), entries


def test_created_worktree_is_also_ledgered(git_repo: Path) -> None:
    """The happy path still records the same ledger entry once the worktree
    is actually created."""
    mailbox = git_repo / "loop"
    exec_id = "cafef00d" * 4
    ctx = _ctx(git_repo, mailbox, exec_id)
    head = _git(git_repo, "rev-parse", "HEAD")

    path, branch = driver._create_builder_worktree(ctx, iteration=1, slice_id="ok", index=1,
                                                    dispatch_head=head)

    assert path.is_dir()
    ledger_path = steplib.ledger_path(mailbox, git_repo)
    assert ledger_path is not None and ledger_path.is_file()
    entries = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    assert any(e.get("branch") == branch and e.get("path") == str(path) for e in entries), entries


# --------------------------------------------------------------------------
# _retries_for / timeout resolution: a "no wall-clock limit" 0 must never be
# treated as "unset" and silently replaced by the fallback default (README.md
# "Container / no-time-limit mode").
# --------------------------------------------------------------------------


class _FakeRetries:
    def __init__(self, max_attempts=3, backoff_seconds=(10.0, 30.0, 90.0), idle_retry_unlimited=False):
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.idle_retry_unlimited = idle_retry_unlimited


class _FakeCfg:
    def __init__(self, **kw):
        self.retries = _FakeRetries(**kw)


def test_retries_for_returns_idle_retry_unlimited_flag():
    assert driver._retries_for(_FakeCfg(idle_retry_unlimited=True)) == (3, (10.0, 30.0, 90.0), True)
    assert driver._retries_for(_FakeCfg()) == (3, (10.0, 30.0, 90.0), False)
    assert driver._retries_for(object()) == (3, (10.0, 30.0, 90.0), False)


def test_call_role_zero_turn_timeout_is_not_treated_as_unset(monkeypatch, tmp_path: Path):
    """A config-resolved turn_timeout of 0.0 ("no wall-clock limit") must
    reach the runner's TurnSpec as 0.0, never silently replaced by the
    3600.0 fallback -- `or`-style truthiness checks would get this wrong."""
    captured = {}

    class _FakeRunner:
        @staticmethod
        def run_turn(spec, on_spawn=None, cancel=None):
            captured["turn_timeout"] = spec.turn_timeout
            captured["idle_timeout"] = spec.idle_timeout
            return type("R", (), {
                "ok": True, "kind": "ok", "text": "", "session_id": None,
                "denials": [],
            })()

        class TurnSpec:  # minimal stand-in so dataclasses.fields() works
            pass

    import dataclasses

    @dataclasses.dataclass
    class _TurnSpec:
        role: str = ""
        agent: str = ""
        model: str = ""
        prompt: str = ""
        cwd: str = ""
        session_id: str | None = None
        label: str = ""
        env: dict | None = None
        turn_timeout: float | None = 3600.0
        idle_timeout: float = 600.0
        max_attempts: int = 3
        backoff: tuple = (10.0, 30.0, 90.0)
        idle_retry_unlimited: bool = False
        log_dir: str | None = None
        opencode_bin: str = "opencode"
        key_file: str | None = None

    _FakeRunner.TurnSpec = _TurnSpec
    monkeypatch.setattr(driver, "_get_runner", lambda: _FakeRunner)

    cfg = _FakeCfg()
    cfg.timeouts = type("T", (), {"turn_seconds": 0.0, "evaluator_turn_seconds": 5400.0,
                                  "idle_seconds": 600.0})()

    def turn_timeout_for(role):
        return 0.0 if role == "lead" else 5400.0
    cfg.turn_timeout_for = turn_timeout_for

    ctx = driver.RunContext(
        root_mailbox=tmp_path, live_mailbox=tmp_path, repo=tmp_path,
        cfg=cfg, token="t", exec_id="e" * 8, run_dir=tmp_path / "run",
        log_dir=tmp_path / "run" / "logs", env={}, root_free=False, lead_record=None,
        out=lambda s: None, cancel=threading.Event(),
    )

    driver._call_role(ctx, role="lead", agent="trio-lead", model="m",
                      prompt="p", cwd=tmp_path, label="lead test")

    assert captured["turn_timeout"] == 0.0


# --------------------------------------------------------------------------
# Bug 1: `_call_role`'s owned-STATE-key guard (lead/repair/evaluator turns
# only) restores `iteration`/`phase`/`evaluated_sha`/`evaluator_attempt`/
# `evaluated_repos` after the turn, while leaving `status`/`frozen:`/every
# other line exactly as the turn wrote it.
# --------------------------------------------------------------------------


def _fake_runner_module(on_run_turn):
    """A minimal stand-in for ``trio_opencode.runner`` whose ``run_turn``
    calls ``on_run_turn()`` for its side effect, then returns ``ok: True``
    (matching ``test_call_role_zero_turn_timeout_is_not_treated_as_unset``'s
    own fake)."""
    import dataclasses

    @dataclasses.dataclass
    class _TurnSpec:
        role: str = ""
        agent: str = ""
        model: str = ""
        prompt: str = ""
        cwd: str = ""
        session_id: str | None = None
        label: str = ""
        env: dict | None = None
        turn_timeout: float | None = 3600.0
        idle_timeout: float = 600.0
        max_attempts: int = 3
        backoff: tuple = (10.0, 30.0, 90.0)
        idle_retry_unlimited: bool = False
        log_dir: str | None = None
        opencode_bin: str = "opencode"
        key_file: str | None = None

    class _FakeRunner:
        TurnSpec = _TurnSpec

        @staticmethod
        def run_turn(spec, on_spawn=None, cancel=None):
            return on_run_turn()

    return _FakeRunner


def _driver_ctx_for_state_guard(mailbox: Path, repo: Path) -> driver.RunContext:
    return driver.RunContext(
        root_mailbox=mailbox, live_mailbox=mailbox, repo=repo, cfg=object(),
        token=TOKEN, exec_id="f" * 32, run_dir=repo / "run", log_dir=repo / "run" / "logs",
        env={}, root_free=False, lead_record=None, out=lambda s: None,
        cancel=threading.Event(),
    )


def _ok_result(text: str = "") -> object:
    return type("R", (), {"ok": True, "kind": "ok", "text": text, "session_id": None,
                          "denials": []})()


def _permission_result(error: str = "denied: no") -> object:
    return type("R", (), {"ok": False, "kind": "permission", "text": "", "session_id": None,
                          "denials": [], "error": error})()


def test_call_role_restores_owned_state_keys_after_a_lead_turn(tmp_path: Path, monkeypatch) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n"
        "evaluator_attempt: 2\nfrozen: nothing yet\n",
        encoding="utf-8",
    )
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")

    def mutate():
        text = (mailbox / "STATE.md").read_text(encoding="utf-8")
        text = text.replace("phase: lead-running", "phase: lead-planned")
        text = text.replace("evaluator_attempt: 2", "evaluator_attempt: 9")
        (mailbox / "STATE.md").write_text(
            text + "frozen: x @abc\nstatus: needs_human\n", encoding="utf-8")
        return _ok_result()

    monkeypatch.setattr(driver, "_get_runner", lambda: _fake_runner_module(mutate))
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._call_role(ctx, role="lead", agent="trio-lead", model="m", prompt="p",
                      cwd=tmp_path, label="lead plan it1")

    state = steplib.TL._read_state(mailbox / "STATE.md")
    # Driver-owned keys restored to their pre-turn values...
    assert state["phase"] == "lead-running"
    assert state["evaluator_attempt"] == "2"
    assert state["iteration"] == "1"
    # ...but the role's own writes to non-owned lines are kept untouched.
    assert state["status"] == "needs_human"
    assert "frozen: x @abc" in (mailbox / "STATE.md").read_text(encoding="utf-8")

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "restored driver-owned STATE key(s) after lead plan it1" in log_text, log_text
    assert "phase 'lead-planned' -> 'lead-running'" in log_text, log_text


def test_call_role_restores_owned_state_keys_even_when_the_turn_raises_driverstop(
    tmp_path: Path, monkeypatch,
) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")

    def mutate_then_deny():
        text = (mailbox / "STATE.md").read_text(encoding="utf-8")
        (mailbox / "STATE.md").write_text(
            text.replace("phase: lead-running", "phase: lead-planned"), encoding="utf-8")
        return _permission_result()

    monkeypatch.setattr(driver, "_get_runner", lambda: _fake_runner_module(mutate_then_deny))
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    with pytest.raises(driver.DriverStop):
        driver._call_role(ctx, role="lead", agent="trio-lead", model="m", prompt="p",
                          cwd=tmp_path, label="lead plan it1")

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-running"


def test_call_role_never_guards_a_builder_turn(tmp_path: Path, monkeypatch) -> None:
    """Builders run concurrently in their OWN worktrees, never the live
    mailbox: the guard must not touch (or require) a STATE.md next to a
    builder's `cwd`/`live_mailbox` at all."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n", encoding="utf-8")

    def mutate():
        (mailbox / "STATE.md").write_text(
            "iteration: 99\nstatus: running\nphase: lead-planned\n", encoding="utf-8")
        return _ok_result()

    monkeypatch.setattr(driver, "_get_runner", lambda: _fake_runner_module(mutate))
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._call_role(ctx, role="builder", agent="trio-builder", model="m", prompt="p",
                      cwd=tmp_path, label="builder it1w1 a")

    # A builder's own STATE.md writes (however unusual) are never restored.
    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-planned"
    assert state["iteration"] == "99"


# --------------------------------------------------------------------------
# Bug 1, crash-window case: `_normalize_resume_phase` fixes a `status:
# running` STATE.md left in an unrecognised `phase` by a crash mid-turn.
# --------------------------------------------------------------------------


def test_normalize_resume_phase_rewrites_unknown_running_phase_to_lead_running(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-planned\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-running"
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "normalised STATE phase 'lead-planned' -> 'lead-running'" in log_text, log_text


def test_normalize_resume_phase_picks_repair_running_when_repairs_counter_is_set(
    tmp_path: Path,
) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 2\nstatus: running\nphase: some-bogus-phase\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / ".repairs").write_text("1\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "repair-running"


def test_normalize_resume_phase_leaves_non_running_status_alone(tmp_path: Path) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: needs_human\nphase: some reason for a human\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["status"] == "needs_human"
    assert state["phase"] == "some reason for a human"


def test_normalize_resume_phase_leaves_known_running_phases_alone(tmp_path: Path) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-running\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-running"


# --------------------------------------------------------------------------
# Bug 1, Evaluator-crash case (oc-fix-eval VERDICT): `_call_role` persists a
# pre-turn OWNED_STATE_KEYS snapshot into `.driver.json` before a guarded
# turn starts; `_normalize_resume_phase` restores from it on resume (instead
# of guessing a phase), and falls back to the old behaviour -- improved with
# an `evaluator_attempt`+`evaluated_sha` check -- only when no snapshot is on
# disk.
# --------------------------------------------------------------------------


def test_call_role_persists_state_snapshot_to_driver_json_before_turn_starts(
    tmp_path: Path, monkeypatch,
) -> None:
    """The snapshot must be on disk BEFORE the turn's subprocess spawns --
    read it from inside the fake turn itself, as the real crash window
    would see it (a SIGKILL right after this point must leave it there for
    `resume`)."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-done\n"
        "evaluated_sha: abc123\nevaluator_attempt: 1\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    seen = {}

    def during_turn():
        seen["driver_json"] = driver._read_json(mailbox / driver.DRIVER_FILE)
        return _ok_result()

    monkeypatch.setattr(driver, "_get_runner", lambda: _fake_runner_module(during_turn))
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._call_role(ctx, role="evaluator", agent="trio-evaluator", model="m", prompt="p",
                      cwd=tmp_path, label="evaluator it1")

    snap = seen["driver_json"].get("state_snapshot")
    assert snap == {"iteration": "1", "phase": "lead-done", "evaluated_sha": "abc123",
                    "evaluator_attempt": "1", "evaluated_repos": ""}, seen["driver_json"]


def test_call_role_clears_state_snapshot_from_driver_json_after_turn_ends(
    tmp_path: Path, monkeypatch,
) -> None:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-done\n"
        "evaluated_sha: abc123\nevaluator_attempt: 1\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")

    monkeypatch.setattr(driver, "_get_runner", lambda: _fake_runner_module(_ok_result))
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._call_role(ctx, role="evaluator", agent="trio-evaluator", model="m", prompt="p",
                      cwd=tmp_path, label="evaluator it1")

    data = driver._read_json(mailbox / driver.DRIVER_FILE)
    assert data.get("state_snapshot") is None, data
    assert ctx.state_snapshot is None


def test_normalize_resume_phase_restores_from_persisted_snapshot_and_clears_it(
    tmp_path: Path,
) -> None:
    """The oc-fix-eval crash: the Evaluator (running while `phase` is
    `lead-done` with `evaluated_sha`/`evaluator_attempt` already pinned)
    corrupts every OWNED_STATE_KEYS line and is SIGKILLed before
    `_call_role`'s own restore. The persisted pre-turn snapshot, not a
    guessed `lead-running`, must come back exactly."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 7\nstatus: running\nphase: evaluating\n"
        "evaluated_sha: deadbeef\nevaluator_attempt: bogus\nevaluated_repos: bogus\n",
        encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    driver._atomic_write_json(mailbox / driver.DRIVER_FILE, {
        "state_snapshot": {"iteration": "7", "phase": "lead-done", "evaluated_sha": "97b0c1",
                          "evaluator_attempt": "2", "evaluated_repos": ""},
    })
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["iteration"] == "7"
    assert state["phase"] == "lead-done"
    assert state["evaluated_sha"] == "97b0c1"
    assert state["evaluator_attempt"] == "2"
    assert state["evaluated_repos"] == ""
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "restored driver-owned STATE key(s) from pre-turn snapshot" in log_text, log_text
    assert ctx.state_snapshot is None


def test_normalize_resume_phase_ignores_snapshot_from_a_different_run_token(
    tmp_path: Path,
) -> None:
    """oc-fix-eval-3: the snapshot is tied to the `run_token` already
    persisted alongside it in `.driver.json`. A snapshot recorded under a
    DIFFERENT `run_token` than the run now resuming (a different harness
    invocation sharing the mailbox, or a stale file left behind by a switch
    to a driver that never writes `.driver.json` at all) must be ignored --
    as if no snapshot were on disk at all -- never applied to roll STATE
    back to that other run's cursor."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: evaluating\n"
        "evaluated_sha: deadbeef\nevaluator_attempt: bogus\n",
        encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    driver._atomic_write_json(mailbox / driver.DRIVER_FILE, {
        "run_token": "some-other-run-token",
        "state_snapshot": {"iteration": "7", "phase": "lead-done", "evaluated_sha": "97b0c1",
                          "evaluator_attempt": "2", "evaluated_repos": ""},
    })
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)
    assert ctx.token != "some-other-run-token"

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    # The mismatched snapshot's values must never land -- `iteration`/
    # `evaluated_sha`/`evaluator_attempt` stay exactly as the crash left
    # them (this function never touches them outside the snapshot path),
    # and `phase` is normalised by the ordinary fallback guess (both pin
    # keys are already set, so `lead-done`), not the snapshot's own `phase`
    # (which happens to also read `lead-done` here, but arrived via the
    # fallback, not a restore -- the log line below proves which path ran).
    assert state["iteration"] == "1", state
    assert state["evaluated_sha"] == "deadbeef", state
    assert state["evaluator_attempt"] == "bogus", state
    assert state["phase"] == "lead-done", state
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "restored driver-owned STATE key(s) from pre-turn snapshot" not in log_text, log_text
    assert "normalised STATE phase 'evaluating' -> 'lead-done'" in log_text, log_text
    # Ignored, and nothing is left for a later resume to mistakenly adopt.
    assert ctx.state_snapshot is None


def test_normalize_resume_phase_fallback_lead_done_when_evaluator_pin_already_set(
    tmp_path: Path,
) -> None:
    """No persisted snapshot (e.g. an older `.driver.json`): the fallback
    must still recognise an Evaluator-turn crash -- `evaluator_attempt` and
    `evaluated_sha` both already set means the pin happened and `phase`
    normalises back to its own precondition, `lead-done`, never
    `lead-running` (which would wrongly re-run the Lead and leave `pin`
    reusing the stale `evaluated_sha`)."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 7\nstatus: running\nphase: evaluating\n"
        "evaluated_sha: deadbeef\nevaluator_attempt: 2\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-done"


def test_normalize_resume_phase_fallback_without_evaluator_pin_still_lead_running(
    tmp_path: Path,
) -> None:
    """The existing fallback is unchanged when the Evaluator's pin was never
    reached (e.g. a Lead-turn crash): `evaluated_sha`/`evaluator_attempt`
    unset means `lead-running` (or `repair-running`), exactly as before."""
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-planned\n", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    ctx = _driver_ctx_for_state_guard(mailbox, tmp_path)

    driver._normalize_resume_phase(ctx)

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"] == "lead-running"


# --------------------------------------------------------------------------
# Bug 3: `_apply` turns `step_long`'s exhausted-polling result into a clean
# DriverStop -- never a bare `KeyError` on `a["stop"]`.
# --------------------------------------------------------------------------


def test_apply_with_exhausted_step_long_polling_stops_cleanly(tmp_path: Path, monkeypatch) -> None:
    ctx = driver.RunContext(
        root_mailbox=tmp_path, live_mailbox=tmp_path, repo=tmp_path, cfg=object(),
        token=TOKEN, exec_id="a" * 32, run_dir=tmp_path / "run", log_dir=tmp_path / "run" / "logs",
        env={}, root_free=False, lead_record=None, out=lambda s: None,
        cancel=threading.Event(), acceptance=True,
    )

    def fake_step_long(fn, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        assert fn is steplib.apply
        return {"ok": False, "op": "apply", "error": "apply still running after 12 polls"}

    monkeypatch.setattr(steplib, "step_long", fake_step_long)

    with pytest.raises(driver.DriverStop) as exc_info:
        driver._apply(ctx, iteration=1, pin={"evaluator_attempt": 1}, rec={})

    stop = exc_info.value
    assert stop.status == "error"
    assert stop.code == 3
    assert "still running" in stop.reason


def test_variant_for_reaches_turn_spec_and_v2_argv():
    from types import SimpleNamespace
    from trio_opencode import driver, runner
    cfg = SimpleNamespace(
        model_for=lambda r: {"lead": "p/deep", "builder": "p/glm"}[r],
        variant_for=lambda r: {"lead": "max", "builder": None}[r],
    )
    assert driver._variant_for(cfg, "lead", "p/deep") == "max"
    assert driver._variant_for(cfg, "builder", "p/glm") is None
    # an explicit model override that differs from the role's model drops it
    assert driver._variant_for(cfg, "lead", "p/other") is None
    spec = driver._make_turn_spec(runner, role="lead", agent="trio-lead", model="p/deep",
                                  prompt="x", cwd=".", variant=driver._variant_for(cfg, "lead", "p/deep"))
    assert spec.variant == "max"

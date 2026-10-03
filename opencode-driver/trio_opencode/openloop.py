"""The trio-opencode open-loop runner: ``OpenLoopRunner`` (the
``steplib.TL.RoleRunner`` this driver hands to ``steplib.TL.run_open_loop``
as both its Lead and its Evaluator runner), settings resolution (D12),
a STATE.md key guard (D7), a ``.driver.json``/``.session.json`` sidecar
override (D8), the root-free land hook (D11) and ``drive()`` -- the
top-level entry ``driver.run()`` calls once it has detected an open-loop
mailbox (D1) and built its ``RunContext`` the normal way.

``steplib.TL.run_open_loop`` (``metrics/trio_loop.py``) owns ALL of the
open-loop state machine itself: the mailbox lock, the Lead-thread polling
loop, the per-slice commit gate, slice-eval dispatch/concurrency/harvest,
the integration-eval dispatch, verdict parsing/apply and the land-after-SHIP
retry. This module supplies only the two things that core asks a driver
for: a Lead pass (``OpenLoopRunner.run("lead", ...)``, which performs the
WHOLE driver-owned builder pass -- plan, one wave of builders per slice,
merge+retire each the moment its builder reports, then a lead-review turn)
and an Evaluator turn (``OpenLoopRunner.run("evaluator", ...)``, which
dispatches either a slice-eval or an integration-eval depending on
``context["kind"]`` and otherwise just runs the turn -- VERDICT.md parsing,
retrying and applying all happen back in ``run_open_loop`` itself).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from trio_opencode import driver as drv
from trio_opencode import olprompts, olqueue, prompts as prompts_mod, quality, rootfree, steplib, waves

TL = steplib.TL

# --------------------------------------------------------------- settings


class SettingsError(Exception):
    """A refused open-loop setting (CLI exit 2 / ``driver.run`` code 2)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def detect_open_loop(root_mailbox: Path, *, root_free: bool, repo: Path | None = None) -> bool:
    """D1: open-loop iff *root_mailbox* (or, root-free, an existing Lead
    worktree's live mailbox) has a QUEUE.md. No ``--open-loop`` flag."""
    root_mailbox = Path(root_mailbox)
    if (root_mailbox / "QUEUE.md").is_file():
        return True
    if not root_free:
        return False
    try:
        home_repo = repo if repo is not None else Path(drv._git_toplevel(root_mailbox))
        mailbox_rel = rootfree.mailbox_rel(home_repo, root_mailbox.resolve())
        slug = rootfree.loop_slug(mailbox_rel)
        record = rootfree.load_record(home_repo, slug)
    except Exception:  # noqa: BLE001 - best effort detection, never raises
        return False
    if record is None:
        return False
    try:
        return (record.live_mailbox / "QUEUE.md").is_file()
    except OSError:
        return False


def declared_repos_for_prepare(root_mailbox: Path) -> list[dict[str, Any]]:
    """D2: PLAN.md ``repos:`` entries of *root_mailbox*, resolved the way
    the core does, WITHOUT the aggregates map (that only exists once
    :func:`rootfree.prepare` has created it) -- ``{"name", "path"
    (absolute), "base"}`` per entry, ready for ``rootfree.prepare(...,
    declared=...)``."""
    metrics = TL._METRICS
    root_mailbox = Path(root_mailbox).resolve()
    root = metrics.mailbox_repo_root(root_mailbox)
    try:
        text = (root_mailbox / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    repos, _errors = metrics.parse_repos_block(text, root)
    return [{"name": r["name"], "path": str(r["path"]), "base": r.get("base")} for r in repos]


def _is_pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def resolve_settings(cfg: Any, *, is_open_loop: bool,
                     isolate_workers: bool | None = None,
                     slice_eval_concurrency: int | None = None,
                     slice_eval_drain_seconds: float | None = None,
                     kill_check: bool | None = None,
                     poll_seconds: float | None = None,
                     acceptance_wait_seconds: float | None = None) -> dict[str, Any]:
    """D12: CLI > config > default, mirroring trioctl ``omnigent loop``.

    Raises :class:`SettingsError` for a refused combination (CLI exit 2 /
    ``driver.run`` ``status: error, code: 2``); never touches the mailbox.
    """
    notices: list[str] = []

    cfg_isolate = bool(getattr(cfg, "isolate_workers", True))
    isolate = cfg_isolate if isolate_workers is None else bool(isolate_workers)
    isolation_off = None if isolate else "--no-isolate-workers"

    cfg_conc = getattr(cfg, "slice_eval_concurrency", 4)
    explicit_conc = slice_eval_concurrency is not None
    conc = cfg_conc if slice_eval_concurrency is None else slice_eval_concurrency
    if not _is_pos_int(conc):
        raise SettingsError("--slice-eval-concurrency must be an integer >= 1")

    drain = slice_eval_drain_seconds
    if drain is not None:
        if isinstance(drain, bool) or not isinstance(drain, (int, float)) or drain < 0:
            raise SettingsError("--slice-eval-drain-seconds must be a number >= 0")
        drain = float(drain)

    if not is_open_loop:
        # Lockstep: both flags are accepted no-ops (trio-opencode lockstep
        # builders are always driver-isolated; concurrency never applies).
        if isolation_off is not None:
            notices.append(
                "trio-opencode: lockstep mode: worker isolation is always on; "
                "--no-isolate-workers is a no-op"
            )
        if explicit_conc and conc != 1:
            notices.append("trio-opencode: lockstep mode: slice-eval concurrency not applicable")
        isolate = True
        conc = 1
    elif conc > 1 and isolation_off is not None:
        if explicit_conc:
            raise SettingsError(
                f"--slice-eval-concurrency {conc} refused: concurrent slice-evals need "
                f"worker isolation, which is off ({isolation_off}); drop --no-isolate-workers "
                "or pass --slice-eval-concurrency 1"
            )
        conc = 1

    cfg_kill = bool(getattr(cfg, "kill_check", True))
    kill_cli_disabled = (not bool(kill_check)) if kill_check is not None else (not cfg_kill)

    if poll_seconds is not None:
        poll = float(poll_seconds)
    else:
        env = os.environ.get("TRIO_OPENCODE_POLL_SECONDS", "").strip()
        try:
            poll = float(env) if env else 30.0
        except ValueError:
            poll = 30.0

    # No author time limit by default: null/0 (config) means wait for as long
    # as the author turn lives (its idle watchdog is the only bound).
    wait_cfg = acceptance_wait_seconds
    if wait_cfg is None:
        wait_cfg = getattr(cfg, "acceptance_wait_seconds", None)
    if wait_cfg is not None:
        if isinstance(wait_cfg, bool) or not isinstance(wait_cfg, (int, float)) or wait_cfg < 0:
            raise SettingsError("acceptance_wait_seconds must be a number >= 0 (or null)")
        wait_cfg = float(wait_cfg) or None

    return {
        "acceptance_wait_seconds": wait_cfg,
        "isolate_workers": isolate,
        "slice_eval_concurrency": conc,
        "slice_eval_drain_seconds": drain,
        "kill_check_cli_disabled": kill_cli_disabled,
        "poll_seconds": poll,
        "notices": notices,
    }


# ----------------------------------------------------------- state guard


class _StateGuard:
    """D7: wraps ``TL._update_state`` for the duration of one open-loop run
    so every driver-owned STATE.md write records the resulting
    :data:`driver.OWNED_STATE_KEYS` values as the expected ones (under one
    lock, held across the write), and restores a role turn's deviation from
    them after every lead/evaluator turn via ``driver._restore_owned_state``.
    """

    def __init__(self, ctx: "drv.RunContext") -> None:
        self.ctx = ctx
        self.expected: dict[str, str] | None = None
        # Reentrant: `after_turn` holds this lock around
        # `driver._restore_owned_state`, which itself calls back into the
        # wrapped `TL._update_state` to write the restored keys -- a plain
        # Lock would deadlock on that re-entry.
        self._lock = threading.RLock()
        self._orig: Callable[..., None] | None = None

    def install(self) -> None:
        self._orig = TL._update_state
        guard = self

        def wrapped(path, updates):  # noqa: ANN001
            with guard._lock:
                guard._orig(path, updates)
                try:
                    snap = drv._owned_state_snapshot(Path(path))
                except Exception:  # noqa: BLE001 - best effort guard
                    snap = None
                if snap is not None:
                    guard.expected = snap
                    guard.ctx.state_snapshot = snap
                    try:
                        guard.ctx.write_driver_json(
                            phase=guard.ctx._last_phase, iteration=guard.ctx._last_iteration)
                    except Exception:  # noqa: BLE001 - best effort
                        pass

        TL._update_state = wrapped

    def after_turn(self, path: Path, label: str) -> None:
        with self._lock:
            if self.expected is None:
                return
            drv._restore_owned_state(self.ctx, self.expected, Path(path), label)

    def uninstall(self) -> None:
        if self._orig is not None:
            TL._update_state = self._orig
            self._orig = None


# -------------------------------------------------------------- sidecars


def _driver_meta_of(*runners: object) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for runner in runners:
        extra = getattr(runner, "driver_meta", None)
        if isinstance(extra, dict):
            meta.update(extra)
    return meta


def _make_sidecar_writer(ctx: "drv.RunContext") -> Callable[..., None]:
    """D8/H8: replaces ``TL._write_open_loop_sidecars`` for the run with an
    atomic writer of the UNION of TL's own open-loop payload and this
    driver's ``.driver.json`` fields; ``ctx.driver_extra`` is kept in sync
    so ``RunContext.write_driver_json`` (a role turn's ``on_spawn``/
    ``on_turn_end``) never drops these keys when it writes in between.

    Shares ``ctx.driver_json_lock`` with ``RunContext.write_driver_json``
    itself (rather than a lock of its own) -- the Lead thread calls this
    writer while a builder/slice-eval thread can call ``on_spawn``/
    ``on_turn_end`` concurrently, and ONE lock between the two writers is
    what makes the last write to land always carry both sides' keys instead
    of racing each other's ``_atomic_write_json``. ``ctx.turns`` is read
    under ``ctx._turns_lock`` for the same reason a plain ``dict(ctx.turns)``
    here once could: a concurrent ``on_spawn``/``on_turn_end`` mutating it
    mid-copy can raise ``RuntimeError: dictionary changed size during
    iteration``."""

    def wrapped(mailbox, lead_runner, eval_runner, iteration, phase, lead_alive,
               eval_alive, started_at, evaluator_sessions=None) -> None:  # noqa: ANN001
        mailbox = Path(mailbox)
        session_ids: dict[str, Any] = {}
        session_ids.update(getattr(lead_runner, "session_ids", {}) or {})
        session_ids.update(getattr(eval_runner, "session_ids", {}) or {})
        extra: dict[str, Any] = {
            "open_loop": True, "lead_alive": lead_alive, "eval_alive": eval_alive,
        }
        if evaluator_sessions is not None:
            extra["evaluator_sessions"] = dict(evaluator_sessions)
        for key, value in _driver_meta_of(lead_runner, eval_runner).items():
            extra.setdefault(key, value)
        with ctx._turns_lock:
            turns_snapshot = dict(ctx.turns)
        payload = {
            "driver": drv.HARNESS, "pid": os.getpid(), "run_token": ctx.token,
            "exec_id": ctx.exec_id, "iteration": iteration, "phase": phase,
            "live_mailbox": str(ctx.live_mailbox), "root_mailbox": str(ctx.root_mailbox),
            "acceptance_enabled": ctx.acceptance,
            "session_ids": session_ids,
            "turns": turns_snapshot,
            "state_snapshot": ctx.state_snapshot,
            "root_free": {"enabled": ctx.root_free,
                         "branch": ctx.lead_record.branch if ctx.lead_record else None,
                         "path": str(ctx.lead_record.path) if ctx.lead_record else None},
            "updated_at": drv._now_iso(),
        }
        payload.update(extra)
        session_payload = {
            "pid": os.getpid(), "iteration": iteration, "phase": phase, "open_loop": True,
            "lead_alive": lead_alive, "eval_alive": eval_alive, "started_at": started_at,
        }
        with ctx.driver_json_lock:
            drv._atomic_write_json(mailbox / ".driver.json", payload)
            drv._atomic_write_json(mailbox / ".session.json", session_payload)
            # Keep driver.py's own (non-open-loop-aware) write_driver_json
            # from dropping these keys when a role turn's on_spawn/
            # on_turn_end writes `.driver.json` in between two sidecar
            # writes -- it merges `driver_extra` in via `setdefault`.
            ctx.driver_extra = {k: v for k, v in extra.items()}

    return wrapped


# ------------------------------------------------------------ land hook


def _undo_land_commit(lead_repo: Path, verified_tip: str, live_mailbox: Path,
                      saved: dict[str, bytes | None]) -> None:
    TL._git(lead_repo, "reset", "--hard", verified_tip)
    for name, data in saved.items():
        path = live_mailbox / name
        if data is None:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            path.write_bytes(data)


def make_land_hook(ctx: "drv.RunContext") -> Callable[[Path, int], dict]:
    """D11: port of Omnigent's home land record (``omnigent/root_free.py``
    ~1236-1300), simplified to trio-opencode's own :mod:`rootfree` (which
    already walks declared-repos-first/home-last itself, D11's "+ one line
    per declared repo landed"). No Omnigent ``reverify`` re-land rule: a
    diverged target comes back ``needs_land`` (``trio-opencode land``, or
    this hook again on resume, retries) -- documented, not implemented."""

    def land_hook(mailbox: Path, iteration: int) -> dict:  # noqa: ARG001 - mailbox is the live one
        record = ctx.lead_record
        if record is None:
            return {"status": "error", "detail": "land hook called without a root-free Lead record"}
        lead_repo = Path(record.path)
        live_mailbox = ctx.live_mailbox
        try:
            verified_tip = TL._git(lead_repo, "rev-parse", "HEAD").stdout.strip()
            saved: dict[str, bytes | None] = {}
            for name in ("STATE.md", "LOG.md"):
                f = live_mailbox / name
                saved[name] = f.read_bytes() if f.is_file() else None
            for name, info in (record.repos or {}).items():
                tip = TL._git(Path(info["path"]), "rev-parse", "HEAD").stdout.strip()
                TL._append_log(
                    live_mailbox,
                    f"- iter {iteration} | loop | landed {info['branch']} @{tip[:12]} "
                    f"onto {info['target_ref']}",
                )
            TL._append_log(
                live_mailbox,
                f"- iter {iteration} | loop | landed {record.branch} @{verified_tip[:12]} "
                f"onto {record.target}",
            )
            TL._update_state(live_mailbox / "STATE.md", {
                "status": "shipped", "phase": "landed", "landed": verified_tip,
                "target_ref": record.target,
            })
            r_add = subprocess.run(["git", "-C", str(lead_repo), "add", "-A", "--", record.mailbox_rel],
                                   capture_output=True, text=True)
            if r_add.returncode != 0:
                _undo_land_commit(lead_repo, verified_tip, live_mailbox, saved)
                return {"status": "error",
                       "detail": f"could not stage the land record: {r_add.stderr.strip()[:300]}"}
            r_commit = subprocess.run(
                ["git", "-C", str(lead_repo), "commit", "-q", "-m",
                 f"loop: land {record.mailbox_rel} (iteration {iteration})"],
                capture_output=True, text=True,
            )
            if r_commit.returncode != 0:
                _undo_land_commit(lead_repo, verified_tip, live_mailbox, saved)
                return {"status": "error",
                       "detail": "could not commit the land record: "
                                f"{(r_commit.stdout + r_commit.stderr).strip()[:300]}"}
            result = rootfree.land(record)
            if result.get("status") != "landed":
                _undo_land_commit(lead_repo, verified_tip, live_mailbox, saved)
                return {"status": result.get("status", "needs_land"),
                       "phase": result.get("phase"), "detail": result.get("detail")}
            return {"status": "landed", "detail": result.get("detail")}
        except Exception as exc:  # noqa: BLE001 - a land failure is terminal, never `running`
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

    return land_hook


# ------------------------------------------------------------- misc util


class _Fatal:
    """D9: the first :class:`driver.DriverStop` raised by any turn (lead
    pass, slice-eval, integration-eval, author), shared by both the
    ``lead_runner`` and ``eval_runner`` roles (the same
    :class:`OpenLoopRunner` instance plays both). Capturing a stop also
    sets ``ctx.cancel`` so every OTHER live turn (concurrent builders,
    slice-evals, the Lead thread) is killed at once -- ``runner.run_turn``'s
    own pump loop already watches this same ``threading.Event`` and kills
    its process group within its ~0.2s poll, and the next
    ``drv._call_role`` call anywhere refuses to even spawn. A role denial
    (``permission``/``config_error``) is a stop kind, never retried or
    bypassed -- this is what makes it also stop every sibling turn instead
    of letting them run to completion."""

    def __init__(self, ctx: "drv.RunContext") -> None:
        self.ctx = ctx
        self.stop: "drv.DriverStop | None" = None
        self._lock = threading.Lock()

    def capture(self, exc: "drv.DriverStop") -> None:
        with self._lock:
            if self.stop is None:
                self.stop = exc
        self.ctx.cancel.set()


class _ConflictError(Exception):
    def __init__(self, slice_id: str, branch: str) -> None:
        super().__init__(f"merge conflict for slice {slice_id} ({branch})")
        self.slice_id = slice_id
        self.branch = branch


def _repo_field_for(record: "rootfree.LeadWorktree | None", repo_path: Path) -> str | None:
    if record is None:
        return None
    for name, info in (record.repos or {}).items():
        try:
            if Path(info["path"]).resolve() == repo_path.resolve():
                return name
        except OSError:
            continue
    return None


def _acceptance_tool_path() -> str:
    """D13: the trio-acceptance module path, resolved the same way through
    ``steplib.TL`` everywhere it is needed (the author hook, and H10's
    acceptance plan/review fragments) -- never a hardcoded relative path
    (lockstep's own ``_acc_tool`` fallback, ``"metrics/trio-acceptance.py"``,
    assumes a ``ctx.acc["tool"]`` this driver's open-loop run never
    populates: there is no ``steplib.begin``/frozen-acceptance pack here)."""
    mod = TL._load_sibling("trio_acceptance", "trio-acceptance.py")
    return mod.__file__


def _acceptance_plan_notes(ctx: "drv.RunContext") -> list[str]:
    """H10/D13: the frozen-acceptance lead fragment lockstep's plan call
    gives its prompt (``driver._run_lead_pass``'s ``prompts.acc_plan_lines``)
    -- carried into the lead-plan turn's own ``notes`` instead, since this
    driver's ``olprompts`` templates take their acceptance text through that
    common key rather than a dedicated ``acc_lines`` slot. Open-loop has no
    ``steplib.begin``/``ctx.acc`` frozen-acceptance pack to report a
    checks/pin count from (``acc=None`` renders its own ``?``/``?`` -- the
    fragment text, the actual acceptance rules, is what matters here)."""
    tool = _acceptance_tool_path()
    lines = prompts_mod.acc_plan_lines(str(ctx.live_mailbox), tool, ctx.acc)
    return ["\n".join(lines).strip("\n")]


def _acceptance_pass_notes(ctx: "drv.RunContext") -> list[str]:
    """H10/D13: the short reminder every OTHER Lead call in a pass gets
    (``prompts.acc_pass_lines("lead", ...)``) -- lockstep gives this to its
    last integrate/solo-continuation call; this driver's open-loop
    counterpart is the lead-review turn that closes out the pass."""
    tool = _acceptance_tool_path()
    lines = prompts_mod.acc_pass_lines("lead", str(ctx.live_mailbox), tool)
    return ["\n".join(lines).strip("\n")]


# ====================================== author failure degrades, never kills


class DegradableAcceptance(TL.AcceptanceController):
    """The shared core's ``AcceptanceController`` with two driver-side
    differences, both for a run that must not die of its *author*:

    * **No author time limit by default.** The core waits for the author
      ``wait_s`` seconds (default 900; this driver used to pass 180) and then
      stops the loop with ``acceptance-timeout``. Here ``wait_s=None`` (the
      default) waits for as long as the author turn lives -- the turn's own
      idle/hung-connection watchdogs are the only bound. A configured number
      still bounds it.
    * **An author failure degrades to NO PACK instead of ending the run.**
      Whenever the pack was never frozen and the author phase failed
      (contaminated twice, validation never passed, a crashed or timed-out
      turn, ...), every acceptance hook becomes a no-op, a loud
      ``acceptance: DEGRADED`` LOG line says so, and the loop carries on as if
      acceptance were off: no coverage gate, no pre-runs, no SHIP gate. The
      Omnigent, native and lockstep paths END the run with ``status: error``
      here (``_acceptance_stop``); a benchmark run must not. Failures that
      are NOT the author's -- a frozen pack tampered with, a state mismatch
      on resume (NEEDS_HUMAN), a cancellation -- still stop the loop exactly
      as the core decides.
    """

    def __init__(self, mailbox, repo, runner, config) -> None:  # noqa: ANN001
        super().__init__(mailbox, repo, runner, config)
        raw = (config or {}).get("wait_s")
        self.wait_s = float(raw) if raw else None
        self.degraded: str | None = None
        self._degrade_lock = threading.Lock()
        self._on_degrade = (config or {}).get("on_degrade")

    # -- helpers ------------------------------------------------------------

    def _cancelled(self) -> bool:
        ctx = getattr(self.runner, "ctx", None)
        cancel = getattr(ctx, "cancel", None)
        return bool(cancel is not None and cancel.is_set())

    def _degrade(self, iteration: int, exc: "TL.AcceptanceError") -> None:
        with self._degrade_lock:
            if self.degraded:
                return
            self.degraded = f"{exc.reason}: {exc.detail}"
        try:
            self._drop_unfrozen_pack()
        except Exception:  # noqa: BLE001 - best effort, never masks the degrade
            pass
        TL._append_log(
            self.mailbox,
            f"- iter {iteration} | loop | acceptance: DEGRADED to no frozen pack "
            f"({exc.reason}): {exc.detail[:300]}; the loop continues WITHOUT frozen "
            "acceptance (no coverage gate, no pre-runs, no SHIP gate)",
        )
        self._set_meta("acceptance", {"enabled": True, "status": "degraded",
                                      "degraded": self.degraded, "pin": None,
                                      "state_file": str(self.state_path)})
        callback = self._on_degrade
        if callable(callback):
            try:
                callback(self.degraded)
            except Exception:  # noqa: BLE001
                pass

    def _drop_unfrozen_pack(self) -> None:
        """A half-written, never-committed ``acceptance/`` must not stay in
        the mailbox: a later resume would find a pack with no pin
        (``acceptance-state-lost``, NEEDS_HUMAN). A tracked pack is left."""
        if self.frozen() or not self.acc_dir.exists():
            return
        tracked = TL._git(self.repo, "ls-files", "--", self.acc_rel)
        if tracked.returncode == 0 and tracked.stdout.strip():
            return
        shutil.rmtree(self.acc_dir, ignore_errors=True)

    # -- author phase ---------------------------------------------------------

    def start(self, iteration: int) -> None:
        if self.degraded:
            return
        if self._resume_error is not None:
            raise self._resume_error
        try:
            super().start(iteration)
        except TL.AcceptanceNeedsHuman:
            raise
        except TL.AcceptanceError as exc:
            self._degrade(iteration, exc)

    def _call_author(self, export: Path, context: dict) -> dict:
        if self.degraded:
            # A timed-out author thread that is still looping (re-run after a
            # discard / validation retry) must not spawn another turn.
            raise TL.AcceptanceError("acceptance-abandoned", "the run already degraded to no pack")
        return super()._call_author(export, context)

    def _freeze(self, *args, **kwargs):  # noqa: ANN002, ANN003
        if self.degraded:
            raise TL.AcceptanceError("acceptance-abandoned", "the run already degraded to no pack")
        return super()._freeze(*args, **kwargs)

    def wait(self, iteration: int, timeout: float | None = None) -> None:
        if self.degraded:
            return
        if self._resume_error is not None:
            raise self._resume_error
        try:
            self._wait_for_author(iteration, timeout)
        except TL.AcceptanceError as exc:
            if self.degraded:
                return
            if self.frozen() or self._cancelled():
                raise
            self._degrade(iteration, exc)

    def _wait_for_author(self, iteration: int, timeout: float | None) -> None:
        if self._thread is None and not self.frozen():
            self.start(iteration)
            if self.degraded:
                return
        thread = self._thread
        if thread is not None:
            limit = self.wait_s if timeout is None else timeout
            deadline = None if limit is None else time.monotonic() + limit
            while thread.is_alive():
                step = 1.0
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TL.AcceptanceError(
                            "acceptance-timeout",
                            f"the author did not finish within {limit:g}s")
                    step = min(step, left)
                thread.join(timeout=step)
                if thread.is_alive() and self._cancelled():
                    raise TL.AcceptanceError("acceptance-cancelled", "the run was cancelled")
        if self._error is not None:
            raise self._error
        if not self.frozen():
            raise TL.AcceptanceError("acceptance-error", "the pack is not frozen")

    # -- every later hook is a no-op once degraded ------------------------------

    def check_pin(self, iteration: int, role: str) -> bool:
        if self.degraded:
            return True
        return super().check_pin(iteration, role)

    def coverage(self) -> list[str]:
        return [] if self.degraded else super().coverage()

    def lead_gate(self, iteration: int, role: str) -> list[str]:
        self.wait(iteration)
        if self.degraded:
            self.pending_errors = []
            return []
        return super().lead_gate(iteration, role)

    def covered_line(self, slice_id: str, sha: str):  # noqa: ANN201
        return None if self.degraded else super().covered_line(slice_id, sha)

    def integration_context(self, sha: str, iteration: int) -> dict:
        self.wait(iteration)
        if self.degraded:
            return {"text": "", "passed": 0, "total": 0, "failed": [], "unavailable": [],
                    "amendments_left": 0, "degraded": self.degraded}
        return super().integration_context(sha, iteration)

    def review_verdict(self, verdict, scope, iteration, evaluated, state_path):  # noqa: ANN001, ANN201
        if self.degraded:
            return verdict, scope, None
        return super().review_verdict(verdict, scope, iteration, evaluated, state_path)


def _make_degradable_acceptance(mailbox, repo, runner, config):  # noqa: ANN001, ANN201
    """Stand-in for ``TL.make_acceptance`` while :func:`drive` runs the core
    (same shape: ``None`` unless the switch is on)."""
    if not TL.acceptance_enabled(config):
        return None
    controller = DegradableAcceptance(mailbox, repo, runner, config)
    controller.on_resume()
    return controller


# ============================================================ the runner


class OpenLoopRunner:
    """``steplib.TL.RoleRunner`` for trio-opencode's open-loop mailboxes.
    One instance plays BOTH ``lead_runner`` and ``eval_runner`` for
    ``TL.run_open_loop`` (``.run("lead", ...)`` / ``.run("evaluator",
    ...)``), so its own bookkeeping (``session_ids``, in-flight slice-eval
    tracking, the builder-quality record, the shared STATE guard) is
    thread-safe: the Lead thread and the (possibly concurrent) slice-eval
    pool both call into it.
    """

    _PATH_KEYS = ("path", "file_path", "filePath", "filename")

    def __init__(self, ctx: "drv.RunContext", *, settings: dict[str, Any]) -> None:
        self.ctx = ctx
        self.settings = settings
        self.session_ids: dict[str, Any] = {}
        self.driver_meta: dict[str, Any] = {}
        self.timeout = drv._turn_timeout_for(ctx.cfg, "evaluator") or 3600.0
        self.fatal = _Fatal(ctx)
        self.state_guard: _StateGuard | None = None
        self.builder_records: dict[tuple[str, str], dict[str, Any]] = {}
        #: H5 resume: a prior run's crashed builder record, keyed
        #: ``(slice_id, sha[:12])`` (all ``.driver.json`` ever has for a
        #: past slice is the truncated sha its ``builders`` key carries) --
        #: ``_builder_record_for`` falls back to this on a sha-prefix match
        #: when ``builder_records`` (this run's own, full-sha-keyed, live
        #: bookkeeping) has nothing for the pair.
        self._resumed_builders: dict[tuple[str, str], dict[str, Any]] = {}
        self._session_lock = threading.Lock()
        self._inflight: dict[str, dict[str, Any]] = {}
        self._meta_lock = threading.Lock()
        self._merge_locks: dict[str, threading.Lock] = {}
        self._merge_locks_guard = threading.Lock()
        self._retired_logged: set[tuple[str, str]] = set()
        #: H1: ONE guard for the whole run, shared by the Lead thread and
        #: every (possibly concurrent) slice-eval -- see ``olqueue.py``'s
        #: module docstring.
        self.queue_guard = olqueue.QueueGuard(ctx.live_mailbox)

    # -- RoleRunner protocol ----------------------------------------------

    def run(self, role: str, iteration: int, mailbox: Path, context: dict | None = None) -> int:
        context = context or {}
        mailbox = Path(mailbox)
        try:
            if role == "lead":
                return self._run_lead(iteration, mailbox, context)
            if role == "evaluator":
                kind = context.get("kind")
                if kind == "slice-eval":
                    return self._run_slice_eval(iteration, mailbox, context)
                if kind == "integration-eval":
                    return self._run_integration_eval(iteration, mailbox, context)
                raise drv.DriverStop("error", 3, f"openloop: unknown evaluator kind {kind!r}")
            raise drv.DriverStop("error", 3, f"openloop: unknown role {role!r}")
        except drv.DriverStop as exc:
            self.fatal.capture(exc)
            raise

    def inflight_sessions(self) -> dict[str, dict[str, Any]]:
        """``{session_id: meta}`` for every slice-eval this runner knows is
        still running AND whose real opencode session id is already known
        -- the shape ``TL.run_open_loop``'s own ``evaluator_sessions()``
        expects (trioctl's keying: the DICT KEY is the session id, used
        verbatim in a ``held-<sid>.json`` record's own ``session_id``
        field). ``drv._call_role``/``runner.run_turn`` only reveal a brand
        new (never-resumed) turn's session id in its RETURNED
        ``TurnResult`` -- there is no hook to learn it while the turn is
        still running -- so such an entry is left OUT here entirely rather
        than reported under a fabricated key (its own turn label), which
        would otherwise end up as a bogus ``session_id`` in a held record a
        human would be misled into looking up. See :meth:`has_live_turns`
        for this runner's own "anything still running at all" check,
        independent of whether a session id happens to be known."""
        with self._session_lock:
            return {
                str(meta["session_id"]): {k: v for k, v in meta.items() if k != "session_id"}
                for meta in self._inflight.values()
                if meta.get("session_id")
            }

    def has_live_turns(self) -> bool:
        """Whether this runner has any slice-eval/integration-eval turn
        still in flight, regardless of whether its session id is known --
        used by ``drive()``'s exit-drain wait (H2/blocking issue #2), never
        by the core (which wants :meth:`inflight_sessions`'s session-id
        keying instead)."""
        with self._session_lock:
            return bool(self._inflight)

    # -- H5: resume -----------------------------------------------------

    def load_resumed_builders(self, builders: dict[str, Any]) -> None:
        """A crashed run's ``.driver.json`` ``builders`` map (keys
        ``<slice>@<sha12>``): folded into ``_resumed_builders`` (so
        ``_builder_record_for`` can still answer a slice-eval for a slice
        this run never re-built) AND back into ``driver_meta["builders"]``
        (so the next sidecar write keeps them -- they would otherwise
        silently drop out of ``.driver.json`` the moment this run's own
        writer runs without them). Best effort; never raises."""
        if not isinstance(builders, dict) or not builders:
            return
        restored: dict[tuple[str, str], dict[str, Any]] = {}
        for key, entry in builders.items():
            if not isinstance(entry, dict):
                continue
            slice_id, sep, sha12 = str(key).rpartition("@")
            if not sep or not slice_id or not sha12:
                continue
            restored[(slice_id, sha12)] = entry
        if not restored:
            return
        with self._meta_lock:
            self._resumed_builders.update(restored)
            merged = dict(self.driver_meta.get("builders") or {})
            merged.update(builders)
            self.driver_meta = {**self.driver_meta, "builders": merged}

    def _builder_record_for(self, slice_id: str, sha: str) -> dict[str, Any] | None:
        rec = self.builder_records.get((slice_id, sha))
        if rec is not None:
            return rec
        return self._resumed_builders.get((slice_id, sha[:12]))

    # -- H1: QueueGuard ---------------------------------------------------

    def _qg_log(self, mailbox: Path, iteration: int, notes: list[str]) -> None:
        for note in notes:
            TL._append_log(mailbox, f"- iter {iteration} | loop | {note}")

    def author(self, export: Path, context: dict) -> dict:
        """D13: the r19 AcceptanceController's author hook."""
        ctx = self.ctx
        export = Path(export)
        tool = _acceptance_tool_path()
        marker = context.get("marker") or uuid.uuid4().hex[:12]
        attempt = context.get("attempt") or 1
        # The core's author context carries the contaminated-retry `prefix`
        # and the validation retry's `dropped`/`fatal` at the TOP LEVEL
        # (`AcceptanceController._author_phase`), never under `retry`; an
        # explicit `retry` dict (older callers/tests) still wins.
        retry = context.get("retry") or {
            k: context[k] for k in ("prefix", "dropped", "fatal") if context.get(k)}
        notes = context.get("notes")
        if notes is None and context.get("mailbox"):
            notes = (Path(context["mailbox"]) / "ACCEPTANCE-NOTES.md").is_file()
        setup = ctx.author_setup(export, tool, iteration=context.get("iteration") or "?")
        prompt = prompts_mod.author_prompt(
            str(export), tool, marker, attempt, notes=bool(notes), retry=retry,
            shell=setup.shell,
        )
        model = drv._model_for(ctx.cfg, "acceptance")
        label = f"acceptance author {marker}-a{attempt}"
        result = drv._call_role(ctx, role="acceptance", agent=drv.AGENTS["acceptance"],
                                model=model, prompt=prompt, cwd=export, label=label,
                                env_extra=setup.env, argv_prefix=setup.argv_prefix)
        rows = self._tool_call_rows(result, export)
        with self._session_lock:
            self.session_ids["acceptance"] = result.session_id
        return {"exit": 0 if result.ok else 1, "session": result.session_id, "model": model,
               "path": "opencode", "transcript": rows}

    def _tool_call_rows(self, result: Any, export: Path) -> list[dict]:
        if drv.sandboxed(self.ctx):
            return [dict(drv.SANDBOXED_ROW)]
        events_mod = drv._get_events()
        rows: list[dict] = []
        for log_path in getattr(result, "log_paths", []) or []:
            if not str(log_path).endswith(".jsonl"):
                continue
            try:
                text = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                ev = events_mod.parse_line(line)
                if ev is None:
                    continue
                part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                is_tool = ev.get("type") in ("tool_use", "tool") or part.get("type") == "tool"
                if not is_tool:
                    continue
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                if drv.refused_by_permission(state):
                    continue
                tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
                abs_input: dict[str, Any] = {}
                for key, value in drv.audit_tool_input(tool_input or {}, part.get("tool")).items():
                    if (key in self._PATH_KEYS and isinstance(value, str) and value
                            and not Path(value).is_absolute()):
                        abs_input[key] = str((export / value).resolve())
                    else:
                        abs_input[key] = value
                rows.append({"type": "tool_use", "name": part.get("tool"), "input": abs_input})
        return rows

    # -- shared helpers -----------------------------------------------------

    def _target_repo_path(self, repo_name: str | None) -> Path:
        if not repo_name or repo_name in ("home", "."):
            return self.ctx.repo
        record = self.ctx.lead_record
        info = (record.repos or {}).get(repo_name) if record else None
        if info is None:
            raise drv.DriverStop("error", 3, f"openloop: unknown declared repo {repo_name!r}")
        return Path(info["path"])

    def _repos_listing(self) -> list[dict[str, str]]:
        record = self.ctx.lead_record
        if not record or not record.repos:
            return []
        return [{"name": name, "path": info["path"]} for name, info in record.repos.items()]

    def _merge_lock_for(self, repo_path: Path) -> threading.Lock:
        key = str(repo_path.resolve())
        with self._merge_locks_guard:
            lock = self._merge_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._merge_locks[key] = lock
            return lock

    def _set_driver_meta(self, key: str, value: Any) -> None:
        with self._meta_lock:
            self.driver_meta = {**self.driver_meta, key: value}

    def _after_turn_state_guard(self, mailbox: Path, label: str) -> None:
        if self.state_guard is not None:
            self.state_guard.after_turn(mailbox / "STATE.md", label)

    # ================================================================
    # (a)-(e): one Lead pass (D3)
    # ================================================================

    def _validate_plan(self, plan: dict, mailbox: Path, repos_decl: list[dict]) -> str | None:
        problem = waves.check_slices(plan)
        if problem:
            return problem
        slices = plan.get("slices") or []
        seen: set[str] = set()
        valid_repos = {r["name"] for r in repos_decl}
        for s in slices:
            sid = s.get("id")
            if sid in seen:
                return f"duplicate slice id {sid}"
            seen.add(sid)
            repo_name = s.get("repo")
            if repo_name not in (None, "", "home", ".") and repo_name not in valid_repos:
                return f"slice {sid} names an undeclared repo {repo_name!r}"
        # A slice already retired (latest QUEUE.md entry) is returned again
        # only as a fault fix: a forced first Lead pass of a resumed run
        # (run_open_loop always forces one) otherwise re-dispatches builders
        # for work that is already merged, retired and being graded.
        retired_ids = set(olqueue.latest_retired(mailbox))
        for s in slices:
            if s.get("id") in retired_ids and not s.get("fault"):
                return (f"slice {s.get('id')} is already retired; return it only as a fault "
                        "fix (`fault: f<N>`), or return `slices: []` when nothing is left "
                        "to build")
        plan_ids = TL._read_plan_slice_ids(mailbox)
        if plan_ids is not None:
            missing = [s.get("id") for s in slices if s.get("id") not in plan_ids]
            if missing:
                return (f"PLAN.md's slices: block is missing id(s) the plan returned: "
                        f"{missing}")
        return None

    def _run_lead(self, iteration: int, mailbox: Path, context: dict) -> int:
        ctx = self.ctx
        repo = ctx.repo
        model = drv._model_for(ctx.cfg, "lead")
        open_faults = [f for f in olqueue.faults(mailbox) if str(f.get("status")) in ("open", "taken")]
        retired = list(olqueue.latest_retired(mailbox).values())
        repos_decl = self._repos_listing()
        plan_ctx = {
            "mailbox": str(mailbox), "iteration": iteration, "repo": str(repo),
            "driver": "opencode", "output": "fenced-json", "notes": [], "human_answer": None,
            "tmpdir": ctx.tmpdir,
            "queue_errors": context.get("queue_errors") or [],
            "acceptance_errors": context.get("acceptance_errors") or [],
            "open_faults": open_faults, "retired": retired, "refusals": [], "repos": repos_decl,
        }
        if TL._read_plan_slice_ids(mailbox) is None:
            # Blocking issue #4: `metrics/trio_loop.py::_slices_fully_retired`
            # (the core's own "is this open-loop run done" check) reads
            # PLAN.md's OWN `slices:` block, never this driver's structured
            # JSON reply. A Lead that only ever replies with
            # `slices: [...]`/`slices: []` and never writes that block into
            # PLAN.md itself leaves the run looking eternally incomplete even
            # after every slice it ever returned is retired: each further
            # `slices: []` ("nothing left") pass counts as a no-op, and the
            # run stalls `status: error` after 3 of them -- this matters for
            # a mailbox seeded with only GOAL.md (no PLAN.md `slices:` block
            # at all yet). `_validate_plan` already accepts a plan with no
            # PLAN.md block (a fresh mailbox's very first pass is exactly
            # that), so the fix here is telling the Lead, not refusing it.
            plan_ctx["notes"] = plan_ctx["notes"] + [
                "PLAN.md has no `slices:` block yet. Before or right after you "
                "return this plan, also write a ```yaml\nslices:\n...\n``` "
                "fence into PLAN.md listing every slice id you have ever "
                "declared across the WHOLE run so far (not just this pass) -- "
                "the driver's own \"is this run fully retired\" check reads "
                "PLAN.md's block, not your structured reply, and the run "
                "stalls after 3 no-op passes without it."
            ]
        if ctx.acceptance and not ctx.acc_log.get("degraded"):
            # H10/D13: the same frozen-acceptance lead fragment lockstep's
            # own plan call gets -- carried through on the replan attempt
            # below too (`plan_ctx = dict(plan_ctx, refusals=...)` keeps
            # this `notes` list).
            plan_ctx["notes"] = plan_ctx["notes"] + _acceptance_plan_notes(ctx)
        prompt = olprompts.render("lead-plan", plan_ctx)
        label = f"lead-plan it{iteration}"
        self.queue_guard.observe()
        try:
            result = drv._call_role(ctx, role="lead", agent=drv.AGENTS["lead"], model=model,
                                    prompt=prompt, cwd=repo, label=label, guard_state_override=False)
        finally:
            self._qg_log(mailbox, iteration, self.queue_guard.reconcile(label))
        session_id = result.session_id
        plan = drv._structured(ctx, result=result, required=("slices",), cwd=repo, role="lead",
                               agent=drv.AGENTS["lead"], model=model, label=label, advisory=False)
        refusal = self._validate_plan(plan, mailbox, repos_decl)
        if refusal is not None:
            plan_ctx = dict(plan_ctx, refusals=[refusal])
            retry_prompt = olprompts.render("lead-plan", plan_ctx)
            label2 = f"lead-plan it{iteration} (replan)"
            self.queue_guard.observe()
            try:
                result2 = drv._call_role(ctx, role="lead", agent=drv.AGENTS["lead"], model=model,
                                         prompt=retry_prompt, cwd=repo, label=label2,
                                         session_id=session_id, guard_state_override=False)
            finally:
                self._qg_log(mailbox, iteration, self.queue_guard.reconcile(label2))
            plan = drv._structured(ctx, result=result2, required=("slices",), cwd=repo, role="lead",
                                   agent=drv.AGENTS["lead"], model=model, label=label2, advisory=False)
            refusal2 = self._validate_plan(plan, mailbox, repos_decl)
            if refusal2 is not None:
                raise drv.DriverStop("error", 3, f"{label2}: refused: {refusal2}")

        slices = plan.get("slices") or []
        results: list[dict] = []
        pass_slices: list[str] = []
        takeovers: list[dict] = []
        if slices:
            wave_list = waves.plan_waves(slices)
            for wave_index, wave in enumerate(wave_list, start=1):
                outcomes = self._run_wave(iteration, wave_index, wave, mailbox)
                for outcome in outcomes:
                    results.append(outcome["result_entry"])
                    if outcome["status"] == "retired":
                        pass_slices.append(outcome["id"])
                    else:
                        takeovers.append({"id": outcome["id"], "reason": outcome["reason"],
                                          "targeted_check": outcome.get("targeted_check")})

        run_review = bool(slices) or bool(takeovers)
        if run_review:
            review_ctx = dict(plan_ctx, results=results, pass_slices=pass_slices,
                              takeovers=takeovers)
            if ctx.acceptance and not ctx.acc_log.get("degraded"):
                # H10/D13: the review turn is a LATER Lead call in this same
                # pass -- lockstep's own last integrate/solo-continuation
                # call gets `acc_pass_lines`, not another `acc_plan_lines`
                # (that block belongs to the first plan call only).
                review_ctx["notes"] = _acceptance_pass_notes(ctx)
            review_prompt = olprompts.render("lead-review", review_ctx)
            review_label = f"lead-review it{iteration}"
            # D3(d): only commits the Lead makes DURING the review turn
            # (take-overs, gate fixes) are retired by the driver afterwards.
            # The heads are taken here, after every wave merged, so the
            # builders' own `slice(<id>):` commits -- already retired at
            # their merge sha by `_merge_and_retire` -- are never retired a
            # second time at their non-merge sha.
            before_heads: dict[str, str] = {}
            for rp in {str(ctx.repo), *(str(self._target_repo_path(r["name"]))
                                        for r in repos_decl)}:
                before_heads[rp] = TL._git(Path(rp), "rev-parse", "HEAD").stdout.strip()
            self.queue_guard.observe()
            try:
                review_result = drv._call_role(
                    ctx, role="lead", agent=drv.AGENTS["lead"], model=model,
                    prompt=review_prompt, cwd=repo, label=review_label,
                    session_id=session_id, guard_state_override=False)
            finally:
                self._qg_log(mailbox, iteration, self.queue_guard.reconcile(review_label))
            drv._structured(ctx, result=review_result, required=(), cwd=repo, role="lead",
                            agent=drv.AGENTS["lead"], model=model, label=review_label,
                            advisory=True, defaults={})
            slices_by_id = {s.get("id"): s for s in slices if isinstance(s, dict)}
            for rp, before in before_heads.items():
                self._retire_lead_commits(iteration, Path(rp), before, mailbox, slices_by_id)

        lint = quality.lead_pass_lint(mailbox, iteration)
        if lint is not None:
            self._set_driver_meta("lint", lint)
        self._after_turn_state_guard(mailbox, f"lead pass it{iteration}")
        return 0

    # -- wave/builder dispatch ----------------------------------------------

    def _create_builder_worktree(self, repo_path: Path, *, iteration: int, slice_id: str,
                                 attempt: int, dispatch_head: str) -> tuple[Path, str]:
        ctx = self.ctx
        exec8 = ctx.exec_id[:8]
        branch = f"trio-oc/{exec8}/i{iteration}-{slice_id}-a{attempt}"
        uniq = uuid.uuid4().hex[:6]
        path = repo_path / drv.BUILDER_WORKTREES_DIR / f"{exec8}-{slice_id}-a{attempt}-{uniq}"
        path.parent.mkdir(parents=True, exist_ok=True)
        steplib.ledger_append(ctx.live_mailbox, repo_path, {
            "kind": "builder", "exec_id": ctx.exec_id, "run_id": f"oc-{exec8}",
            "path": str(path), "branch": branch, "id": slice_id,
            "iteration": iteration, "verified_by": "driver-created",
        })
        r = _git_worktree_add(repo_path, path, dispatch_head, branch)
        if r.returncode != 0:
            raise drv.DriverStop("error", 3, f"builder worktree for {slice_id}: "
                                 f"git worktree add failed: {r.stderr.strip()}")
        return path, branch

    def _verify_branch(self, repo_path: Path, *, dispatch_head: str, branch: str,
                       slice_id: str) -> tuple[bool, str]:
        tip = TL._git(repo_path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").stdout.strip()
        if not tip:
            return False, f"branch {branch} does not exist"
        if not TL._git_is_ancestor(repo_path, dispatch_head, tip):
            return False, f"branch {branch} does not contain the dispatch HEAD {dispatch_head[:12]}"
        commits = TL._git(repo_path, "rev-list", f"{dispatch_head}..{tip}").stdout.split()
        if not commits:
            return False, f"branch {branch} has no commits since the dispatch HEAD"
        subjects = TL._git(repo_path, "log", "--format=%s", f"{dispatch_head}..{tip}").stdout
        if f"slice({slice_id}):" not in subjects:
            return False, f"branch {branch} has no `slice({slice_id}):` commit"
        mailbox_rel = drv._mailbox_rel(repo_path, self.ctx.live_mailbox)
        for sha in commits:
            for p in TL._commit_paths(repo_path, sha):
                if TL._path_in_mailbox(p, mailbox_rel):
                    return False, f"commit {sha[:12]} commits mailbox files ({p})"
        return True, ""

    def _run_one_builder(self, iteration: int, wave_index: int, s: dict, dispatch_head: str,
                         repo_path: Path, *, attempt: int, notes: list[str]) -> dict:
        ctx = self.ctx
        slice_id = s["id"]
        path, branch = self._create_builder_worktree(
            repo_path, iteration=iteration, slice_id=slice_id, attempt=attempt,
            dispatch_head=dispatch_head)
        model = drv._model_for(ctx.cfg, "builder")
        builder_ctx = {
            "mailbox": str(ctx.live_mailbox), "iteration": iteration, "repo": str(repo_path),
            "driver": "opencode", "output": "fenced-json", "notes": notes, "human_answer": None,
            "tmpdir": ctx.tmpdir, "slice": s, "worktree": None, "base": dispatch_head,
            "branch": branch,
        }
        prompt = olprompts.render("builder", builder_ctx)
        label = f"builder it{iteration}w{wave_index} {slice_id}a{attempt}"
        result = drv._call_role(ctx, role="builder", agent=drv.AGENTS["builder"], model=model,
                                prompt=prompt, cwd=path, label=label)
        reported = drv._structured(ctx, result=result, required=("summary",), cwd=path,
                                   role="builder", agent=drv.AGENTS["builder"], model=model,
                                   label=label, advisory=True,
                                   defaults={"summary": "(no structured report)"})
        reported["id"] = slice_id
        ok, why = self._verify_branch(repo_path, dispatch_head=dispatch_head, branch=branch,
                                      slice_id=slice_id)
        targeted = quality.targeted_check_line(result.text)
        mailbox_rel = drv._mailbox_rel(repo_path, ctx.live_mailbox)
        flags = quality.builder_test_flags(path, dispatch_head, mailbox_rel, s.get("brief"))
        kill_check = None
        if ok and quality.kill_check_enabled(
                ctx.live_mailbox, cli_disabled=self.settings["kill_check_cli_disabled"]):
            kill_check = quality.kill_check_for_builder(
                path, dispatch_head, s.get("brief") or "", targeted, mailbox_rel=mailbox_rel,
                budget=quality.kill_check_budget(ctx.live_mailbox))
        return {"id": slice_id, "branch": branch, "path": path, "ok": ok, "reason": why,
               "iteration": iteration,
               "report": reported, "targeted": targeted, "kill_check": kill_check,
               "flags": flags, "repo_path": repo_path, "attempt": attempt}

    def _cleanup_builder_worktree(self, repo_path: Path, path: Path, branch: str, *,
                                  merged: bool) -> None:
        subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(path)],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo_path), "branch", "-d" if merged else "-D", branch],
                       capture_output=True, text=True)

    def _rebase_onto_freeze(self, repo_path: Path, outcome: dict, mailbox: Path) -> None:
        """Put a builder branch that was cut BEFORE the acceptance freeze on
        top of it, so its slice commits descend the freeze commit.

        The acceptance author runs alongside the Lead, so a builder is routinely
        dispatched -- its worktree branch cut from HEAD -- before the freeze
        commit exists. Merging that branch afterwards puts a ``slice(<id>):``
        commit on a line that does not contain the freeze, which the shared
        commit gate (``trio-shadow --require-commits``) rejects as
        "acceptance/freeze ordering" for good: the slice could never be graded
        and the run livelocked. The builder never saw the pack and the freeze
        commit only adds ``<mailbox>/acceptance/`` (builders may not commit
        mailbox paths), so replaying the branch's own commits on the freeze is
        a clean, content-preserving move. Best effort: when it cannot be done
        the merge proceeds unchanged (the gate then reports the cause).
        Caller holds the repo's merge lock."""
        mailbox_rel = drv._mailbox_rel(repo_path, self.ctx.live_mailbox)
        if mailbox_rel is None:
            return
        base_rel = "" if mailbox_rel in ("", ".") else mailbox_rel
        frozen_rel = "/".join(p for p in (base_rel, "acceptance", "FROZEN") if p)
        added = TL._git(repo_path, "log", "--first-parent", "--diff-filter=A", "--format=%H",
                        "HEAD", "--", frozen_rel).stdout.split()
        if not added:
            return
        freeze = added[-1]          # the FIRST commit that adds FROZEN
        tip = TL._git(repo_path, "rev-parse", "--verify", "-q",
                      f"refs/heads/{outcome['branch']}").stdout.strip()
        if not tip or TL._git_is_ancestor(repo_path, freeze, tip):
            return
        rebase = TL._git(outcome["path"], "rebase", "--autostash", freeze)
        iteration = outcome.get("iteration")
        if rebase.returncode != 0:
            TL._git(outcome["path"], "rebase", "--abort")
            TL._append_log(
                mailbox,
                f"- iter {iteration} | loop | builder branch {outcome['branch']} predates the "
                f"acceptance freeze {freeze[:12]} and could not be moved onto it "
                f"({' '.join((rebase.stderr or rebase.stdout).split())[:200]}); merging as is",
            )
            return
        TL._append_log(
            mailbox,
            f"- iter {iteration} | loop | builder branch {outcome['branch']} was cut before the "
            f"acceptance freeze {freeze[:12]}; rebased onto it so its slice commits follow the freeze",
        )

    def _merge_and_retire(self, repo_path: Path, outcome: dict, mailbox: Path) -> tuple[str, str]:
        lock = self._merge_lock_for(repo_path)
        repo_field = _repo_field_for(self.ctx.lead_record, repo_path)
        with lock:
            self._rebase_onto_freeze(repo_path, outcome, mailbox)
            # Write-ahead merge-intent record (see `_MERGE_INTENT_FILE`): it
            # is durable BEFORE `git merge` runs, and removed only once the
            # `retired:` entry is on disk, so a SIGKILL anywhere in between
            # leaves exactly one record naming the merge for resume to finish.
            # `pre_head` is read under the merge lock: a sibling slice's merge
            # into this repo cannot move HEAD between this read and the merge.
            pre_head = TL._git(repo_path, "rev-parse", "HEAD").stdout.strip()
            intent = _intent_begin(mailbox, slice_id=outcome["id"], branch=outcome["branch"],
                                   repo_path=repo_path, repo_field=repo_field,
                                   pre_head=pre_head, run_token=self.ctx.token)
            r = subprocess.run(
                ["git", "-C", str(repo_path), "merge", "-q", "--no-ff", "--no-edit", "-m",
                 f"merge slice {outcome['id']} ({outcome['branch']})", outcome["branch"]],
                # `-q` + DEVNULL, not a capture pipe: if the driver dies
                # mid-merge, git must not take a SIGPIPE writing its summary
                # to a dead parent's pipe after advancing HEAD but before
                # removing MERGE_HEAD (that left a stale MERGE_HEAD behind,
                # turning the next commit into a merge commit).
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if r.returncode != 0:
                subprocess.run(["git", "-C", str(repo_path), "merge", "--abort"],
                               capture_output=True, text=True)
                _intent_done(mailbox, intent)
                raise _ConflictError(outcome["id"], outcome["branch"])
            sha = TL._git(repo_path, "rev-parse", "HEAD").stdout.strip()
            at = TL._git(repo_path, "log", "-1", "--format=%cI", sha).stdout.strip()
        olqueue.append_retired(mailbox, slice_id=outcome["id"], sha=sha, at=at, repo=repo_field)
        _intent_done(mailbox, intent)
        self.queue_guard.observe()
        return sha, at

    def _record_builder(self, slice_id: str, sha: str, outcome: dict) -> None:
        entry = {"authored_by": "builder", "kill_check": outcome["kill_check"],
                 "flags": outcome["flags"], "targeted_check": outcome["targeted"],
                 "branch": outcome["branch"]}
        key = f"{slice_id}@{sha[:12]}"
        with self._meta_lock:
            self.builder_records[(slice_id, sha)] = entry
            builders = dict(self.driver_meta.get("builders") or {})
            builders[key] = entry
            self.driver_meta = {**self.driver_meta, "builders": builders}

    def _dispatch_and_retire(self, iteration: int, wave_index: int, s: dict, repo_path: Path,
                             dispatch_head: str, mailbox: Path) -> dict:
        slice_id = s["id"]
        notes: list[str] = []
        last_reason = ""
        outcome: dict | None = None
        for attempt in (1, 2):
            if attempt > 1:
                # H6: a sibling slice in this same wave may have merged into
                # `repo_path` since attempt 1's dispatch head was taken --
                # re-dispatching from the stale head just reproduces the
                # same conflict. The retry's worktree, verify, kill-check
                # and merge all key off THIS `dispatch_head`, so recomputing
                # it here (the only place attempt 2 is built) carries
                # through everywhere below.
                dispatch_head = TL._git(repo_path, "rev-parse", "HEAD").stdout.strip()
            outcome = self._run_one_builder(iteration, wave_index, s, dispatch_head, repo_path,
                                            attempt=attempt, notes=notes)
            targeted_failed = quality.targeted_check_failed(outcome["targeted"])
            if outcome["ok"] and not targeted_failed:
                try:
                    sha, _at = self._merge_and_retire(repo_path, outcome, mailbox)
                except _ConflictError as exc:
                    last_reason = str(exc)
                    self._cleanup_builder_worktree(repo_path, outcome["path"], outcome["branch"],
                                                   merged=False)
                    notes = notes + [f"attempt {attempt} failed: {last_reason}"]
                    continue
                self._record_builder(slice_id, sha, outcome)
                summary = outcome["report"].get("summary") or "(no summary)"
                TL._append_log(
                    mailbox,
                    f"- iter {iteration} | builder | {slice_id}: {summary} | "
                    f"{outcome['targeted'] or 'TARGETED_CHECK: (missing)'}",
                )
                self._cleanup_builder_worktree(repo_path, outcome["path"], outcome["branch"],
                                               merged=True)
                return {"id": slice_id, "status": "retired", "sha": sha,
                       "result_entry": {"id": slice_id, "status": "retired", "sha": sha,
                                        "targeted_check": outcome["targeted"],
                                        "summary": summary, "reason": None}}
            last_reason = (outcome["reason"] if not outcome["ok"]
                          else f"targeted check failed: {outcome['targeted']}")
            self._cleanup_builder_worktree(repo_path, outcome["path"], outcome["branch"],
                                           merged=False)
            notes = notes + [f"attempt {attempt} failed: {last_reason}"]
        return {"id": slice_id, "status": "takeover", "reason": last_reason,
               "targeted_check": outcome["targeted"] if outcome else None,
               "result_entry": {"id": slice_id, "status": "failed", "sha": None,
                                "targeted_check": outcome["targeted"] if outcome else None,
                                "summary": None, "reason": last_reason}}

    def _run_wave(self, iteration: int, wave_index: int, wave: list[dict], mailbox: Path) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor

        heads: dict[str, str] = {}
        repo_for: dict[str, Path] = {}
        for s in wave:
            rp = self._target_repo_path(s.get("repo"))
            key = str(rp)
            repo_for[key] = rp
            if key not in heads:
                heads[key] = TL._git(rp, "rev-parse", "HEAD").stdout.strip()
        outcomes: list[dict] = []
        # H7: isolation off means this run wants NO concurrent builder
        # processes (whatever the risk `isolate_workers` is guarding against
        # for slice-evals applies here too) -- one builder at a time.
        max_workers = max(1, len(wave)) if self.settings["isolate_workers"] else 1
        def dispatch(*args):  # noqa: ANN002
            # D9: a fatal stop in ONE builder thread (permission denial,
            # config error, turn failed twice) is captured -- which sets
            # ctx.cancel -- the moment it happens, so every sibling turn of
            # the wave is killed at once instead of after it finishes.
            try:
                return self._dispatch_and_retire(*args)
            except drv.DriverStop as exc:
                self.fatal.capture(exc)
                raise

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(dispatch, iteration, wave_index, s,
                           repo_for[str(self._target_repo_path(s.get("repo")))],
                           heads[str(self._target_repo_path(s.get("repo")))], mailbox)
                for s in wave
            ]
            for fut in futures:
                outcomes.append(fut.result())
        return outcomes

    # -- (d) post-review retirement of Lead-made commits ---------------------

    def _retire_lead_commits(self, iteration: int, repo_path: Path, before_head: str,
                             mailbox: Path, slices_by_id: dict[str, dict]) -> None:
        tip = TL._git(repo_path, "rev-parse", "HEAD").stdout.strip()
        if not tip or tip == before_head:
            return
        log = TL._git(repo_path, "log", "--format=%H%x09%s", f"{before_head}..{tip}").stdout
        for line in log.splitlines():
            sha, _tab, subject = line.partition("\t")
            m = re.match(r"^slice\(([^)]+)\):", subject)
            if not m or not sha:
                continue
            self._retire_one_lead_commit(iteration, m.group(1), sha, repo_path, mailbox,
                                         slices_by_id.get(m.group(1)) or {})

    def _retire_one_lead_commit(self, iteration: int, slice_id: str, sha: str, repo_path: Path,
                                mailbox: Path, slice_info: dict) -> None:
        # D3(d): the plan's own `targeted_check` (brief's command is
        # preferred -- it is the exact text the builder was told to run;
        # the structured plan's own `targeted_check` field is the fallback).
        command = (quality.brief_targeted_command(slice_info.get("brief") or "")
                  or (slice_info.get("targeted_check") or "").strip() or None)
        if command is None:
            if not slice_info and slice_id not in olqueue.latest_retired(mailbox):
                # Not a plan slice at all: the Lead tagged a deliverable
                # commit (e.g. `slice(lead-integration): manifests`) with an
                # id no PLAN.md slice owns. There is nothing to retire and no
                # command to run, so this is expected, not a failure -- say
                # so instead of implying a verification gap.
                TL._append_log(
                    mailbox,
                    f"- iter {iteration} | loop | lead commit {sha[:12]} tagged "
                    f"slice({slice_id}) is not a plan slice; nothing to retire "
                    "(a non-slice deliverable commit)",
                )
                return
            TL._append_log(
                mailbox,
                f"- iter {iteration} | loop | lead commit for {slice_id} not retired: "
                "no known targeted check command",
            )
            return
        budget = quality.kill_check_budget(mailbox)
        try:
            r = subprocess.run(command, shell=True, cwd=str(repo_path), capture_output=True,
                              text=True, timeout=budget)
        except subprocess.TimeoutExpired:
            TL._append_log(
                mailbox,
                f"- iter {iteration} | loop | lead commit for {slice_id} not retired: "
                "targeted check timed out",
            )
            return
        if r.returncode != 0:
            TL._append_log(
                mailbox,
                f"- iter {iteration} | loop | lead commit for {slice_id} not retired: "
                f"targeted check exit {r.returncode}",
            )
            return
        at = TL._git(repo_path, "log", "-1", "--format=%cI", sha).stdout.strip()
        repo_field = _repo_field_for(self.ctx.lead_record, repo_path)
        olqueue.append_retired(mailbox, slice_id=slice_id, sha=sha, at=at, repo=repo_field)
        self.queue_guard.observe()
        targeted = quality.targeted_check_line(r.stdout + r.stderr) or "TARGETED_CHECK: PASS"
        entry = {"authored_by": "lead", "kill_check": None, "flags": [], "targeted_check": targeted,
                "branch": None}
        key = f"{slice_id}@{sha[:12]}"
        with self._meta_lock:
            self.builder_records[(slice_id, sha)] = entry
            builders = dict(self.driver_meta.get("builders") or {})
            builders[key] = entry
            self.driver_meta = {**self.driver_meta, "builders": builders}

    # ================================================================
    # (D4) slice-eval
    # ================================================================

    def _create_eval_worktree(self, repo_path: Path, sha: str, slice_id: str) -> Path:
        ctx = self.ctx
        rand = uuid.uuid4().hex[:6]
        path = repo_path / drv.BUILDER_WORKTREES_DIR / f"eval-{slice_id}-{sha[:8]}-{rand}"
        path.parent.mkdir(parents=True, exist_ok=True)
        steplib.ledger_append(ctx.live_mailbox, repo_path, {
            "kind": "eval", "exec_id": ctx.exec_id, "run_id": f"oc-{ctx.exec_id[:8]}",
            "path": str(path), "branch": None, "id": slice_id, "verified_by": "driver-created",
        })
        r = _git_worktree_add(repo_path, path, sha)
        if r.returncode != 0:
            raise drv.DriverStop("error", 3, f"eval worktree for {slice_id}@{sha[:8]}: "
                                 f"git worktree add failed: {r.stderr.strip()}")
        return path

    def _remove_worktree(self, repo_path: Path, path: Path) -> None:
        subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(path)],
                       capture_output=True, text=True)

    def _run_slice_eval(self, iteration: int, mailbox: Path, context: dict) -> int:
        ctx = self.ctx
        slice_id = context["slice"]
        sha = context["sha"]
        repo_name = context.get("repo")
        repo_path = self._target_repo_path(repo_name)
        isolate = self.settings["isolate_workers"]
        eval_worktree: Path | None = None
        try:
            if isolate:
                eval_worktree = self._create_eval_worktree(repo_path, sha, slice_id)
                cwd = eval_worktree
            else:
                cwd = repo_path
            builder_rec = self._builder_record_for(slice_id, sha)
            authored_by = (builder_rec or {}).get("authored_by") or "lead"
            builder_flags = builder_rec.get("flags") if builder_rec is not None else None
            kill_check = builder_rec.get("kill_check") if builder_rec is not None else None
            flags, accept_lint = quality.slice_lint(mailbox, repo_path, slice_id, sha, builder_flags)
            note = quality.quality_note(
                isolate=isolate, kill_check=kill_check, authored_by=authored_by, flags=flags,
                accept_lint=accept_lint, builder_ran=builder_rec is not None)
            key = (slice_id, sha)
            with self._meta_lock:
                first_seen = key not in self._retired_logged
                if first_seen:
                    self._retired_logged.add(key)
            # ol-harden: the retired LOG line uses the SAME resolved
            # kill_check fact `quality_note` renders (trioctl's own `kc`,
            # ~7532-7538) -- never the raw, possibly-``None`` *kill_check*
            # (that rendered `by builder |  (shadow)`, an empty, misleading
            # suffix) -- and is skipped entirely when isolation is off
            # (`resolved_kc is None`), matching trioctl's own
            # ``if not seen and kc is not None``.
            resolved_kc = quality.resolved_kill_check(isolate, kill_check, authored_by)
            if first_seen and resolved_kc is not None:
                TL._append_log(mailbox, quality.retired_log_line(
                    iteration, slice_id, sha, authored_by, resolved_kc))
            model = drv._model_for(ctx.cfg, "evaluator")
            shadow = str(Path(TL.__file__).parent / "trio-shadow.py")
            eval_ctx = {
                "mailbox": str(mailbox), "iteration": iteration, "repo": str(ctx.repo),
                "driver": "opencode", "output": "fenced-json", "notes": [],
                "human_answer": TL.human_answer_block(mailbox, iteration, "evaluator", "slice-eval"),
                "tmpdir": ctx.tmpdir, "slice": slice_id, "sha": sha, "eval_worktree": str(cwd),
                "repo_name": repo_name or "home", "shadow": shadow, "lead_worktree": str(ctx.repo),
                "acceptance_covered": context.get("acceptance_covered"),
            }
            if note:
                eval_ctx["quality_note"] = note
            prompt = olprompts.render("slice-eval", eval_ctx)
            label = f"slice-eval {slice_id}@{sha[:8]}"
            with self._session_lock:
                self._inflight[label] = {"kind": "slice-eval", "slice": slice_id, "sha": sha,
                                         "session_id": None}
            self.queue_guard.observe()
            try:
                result = drv._call_role(ctx, role="evaluator", agent=drv.AGENTS["evaluator"],
                                        model=model, prompt=prompt, cwd=cwd, label=label,
                                        turn_timeout=drv._turn_timeout_for(ctx.cfg, "evaluator"),
                                        guard_state_override=False)
            finally:
                with self._session_lock:
                    self._inflight.pop(label, None)
                self._qg_log(mailbox, iteration, self.queue_guard.reconcile(label))
            with self._session_lock:
                self.session_ids["evaluator"] = result.session_id
            try:
                verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8", errors="replace")
            except OSError:
                verdict_text = ""
            ev = quality.slice_evidence(verdict_text, slice_id, sha)
            if ev is not None:
                TL._append_log(mailbox, quality.evidence_log_line(iteration, slice_id, sha, ev))
                with self._meta_lock:
                    q = dict(self.driver_meta.get("quality") or {})
                    q[f"{slice_id}@{sha[:12]}"] = ev
                    self.driver_meta = {**self.driver_meta, "quality": q}
            self._after_turn_state_guard(mailbox, label)
            return 0
        finally:
            if eval_worktree is not None:
                self._remove_worktree(repo_path, eval_worktree)

    # ================================================================
    # (D5) integration-eval
    # ================================================================

    def _run_integration_eval(self, iteration: int, mailbox: Path, context: dict) -> int:
        ctx = self.ctx
        pins = context.get("pins") or {"home": context.get("pinned_sha")}
        isolate = self.settings["isolate_workers"]
        attempt = str(context.get("evaluator_attempt") or uuid.uuid4().hex[:8])
        worktrees: dict[str, Path] = {}
        repo_worktrees: dict[str, str] = {}
        try:
            for name, sha in pins.items():
                if not sha:
                    continue
                repo_path = ctx.repo if name == "home" else self._target_repo_path(name)
                if isolate:
                    wt_path = repo_path / drv.BUILDER_WORKTREES_DIR / f"eval-int-{attempt[:8]}-{name}"
                    wt_path.parent.mkdir(parents=True, exist_ok=True)
                    steplib.ledger_append(ctx.live_mailbox, repo_path, {
                        "kind": "eval", "exec_id": ctx.exec_id, "run_id": f"oc-{ctx.exec_id[:8]}",
                        "path": str(wt_path), "branch": None, "id": f"integration-{name}",
                        "verified_by": "driver-created",
                    })
                    r = _git_worktree_add(repo_path, wt_path, sha)
                    if r.returncode != 0:
                        raise drv.DriverStop("error", 3, f"integration eval worktree for {name}: "
                                             f"{r.stderr.strip()}")
                    worktrees[name] = wt_path
                else:
                    worktrees[name] = repo_path
                repo_worktrees[name] = str(worktrees[name])
            lead_worktree_path = worktrees.get("home", ctx.repo)
            model = drv._model_for(ctx.cfg, "evaluator")
            eval_ctx = {
                "mailbox": str(mailbox), "iteration": iteration, "repo": str(ctx.repo),
                "driver": "opencode", "output": "fenced-json", "notes": [],
                "human_answer": TL.human_answer_block(mailbox, iteration, "evaluator",
                                                      "integration-eval"),
                "tmpdir": ctx.tmpdir,
                "sha": context.get("pinned_sha") or pins.get("home") or "",
                "attempt": attempt, "eval_worktree": str(lead_worktree_path),
                "pins": pins, "repo_worktrees": repo_worktrees, "lead_worktree": str(ctx.repo),
                "retire_paths": {name: str(self._target_repo_path(name))
                                 for name in pins if name != "home"},
                "acceptance": context.get("acceptance"),
                "rigor": integration_rigor(),
            }
            prompt = olprompts.render("integration-eval", eval_ctx)
            label = f"integration-eval it{iteration}a{attempt[:8]}"
            with self._session_lock:
                self._inflight[label] = {"kind": "integration-eval", "attempt": attempt,
                                         "session_id": None}
            self.queue_guard.observe()
            try:
                result = drv._call_role(ctx, role="evaluator", agent=drv.AGENTS["evaluator"],
                                        model=model, prompt=prompt, cwd=lead_worktree_path,
                                        label=label,
                                        turn_timeout=drv._turn_timeout_for(ctx.cfg, "evaluator"),
                                        guard_state_override=False)
            finally:
                with self._session_lock:
                    self._inflight.pop(label, None)
                self._qg_log(mailbox, iteration, self.queue_guard.reconcile(label))
            with self._session_lock:
                self.session_ids["evaluator"] = result.session_id
            self._after_turn_state_guard(mailbox, label)
            return 0
        finally:
            for name, path in worktrees.items():
                if isolate and path != ctx.repo:
                    repo_path = ctx.repo if name == "home" else self._target_repo_path(name)
                    self._remove_worktree(repo_path, path)


# ============================================================ stale reclaim


def _reclaim_stale_worktrees(ctx: "drv.RunContext") -> None:
    """D10: ``git worktree remove --force`` + delete the branch for every
    builder/eval ledger entry from an earlier exec id whose worktree still
    exists. A retired slice stays retired either way (QUEUE.md is the
    authority); an un-retired one is simply re-planned by the next Lead
    pass."""
    repo_paths = [ctx.repo]
    if ctx.lead_record:
        repo_paths += [Path(info["path"]) for info in (ctx.lead_record.repos or {}).values()]
    try:
        state = TL._read_state(ctx.live_mailbox / "STATE.md")
        iteration = state.get("iteration", "0")
    except Exception:  # noqa: BLE001
        iteration = "0"
    for repo_path in repo_paths:
        try:
            entries = steplib.NS._ledger(ctx.live_mailbox, repo_path)
        except Exception:  # noqa: BLE001 - best effort reclaim
            entries = []
        for entry in entries:
            if entry.get("exec_id") == ctx.exec_id or entry.get("kind") not in ("builder", "eval"):
                continue
            path = entry.get("path")
            if not path or not Path(path).is_dir():
                continue
            r = subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                continue
            branch = entry.get("branch")
            branch_removed = False
            if branch:
                db = subprocess.run(["git", "-C", str(repo_path), "branch", "-D", branch],
                                   capture_output=True, text=True)
                branch_removed = db.returncode == 0
            try:
                TL._append_log(
                    ctx.live_mailbox,
                    f"- iter {iteration} | loop | reclaimed stale {entry.get('kind')} "
                    f"worktree {path}" + (f" (branch {branch} deleted)" if branch_removed else ""),
                )
            except Exception:  # noqa: BLE001
                pass


#: Driver-owned write-ahead record of a builder-branch merge in flight
#: (ol-harden round 4). ``_merge_and_retire`` runs ``git merge`` and
#: ``olqueue.append_retired`` as two separate steps, so a SIGKILL between
#: them leaves a ``merge slice <id> (<branch>)`` commit with no ``retired:``
#: entry. Resume used to INFER such a merge from git history (a walk bounded
#: by the last commit that touched QUEUE.md); that inference depended on the
#: mailbox being tracked/committed and could not tell this goal's merges from
#: a prior goal's. The record replaces it with exact knowledge:
#:
#: * written atomically (tmp + fsync + rename) into the LIVE mailbox, next to
#:   ``.driver.json``, BEFORE ``git merge`` -- it holds the slice id, the
#:   builder branch, the repo (``repo`` path and ``repo_field``, the QUEUE.md
#:   ``repo:`` name for a declared repo, ``null`` for home), the repo's HEAD
#:   before the merge (``pre_head``) and the run token;
#: * removed after the slice's ``retired:`` entry is on disk (or when the merge
#:   is aborted on conflict);
#: * on resume (``_reconcile_merge_retirement``), consumed record by record --
#:   no record, nothing is ever imported.
#:
#: Format: ``{"version": 1, "intents": [{"slice", "branch", "repo", "repo_field",
#: "pre_head", "run_token", "at"}, ...]}`` -- a list because concurrent builders
#: can each have one in flight. ``git merge`` itself is serialized per repo,
#: but the record is written before it and removed after the slice's
#: ``retired:`` append, both OUTSIDE the merge lock -- so sibling records for
#: the SAME repo coexist (the reconcile handles each independently). The file is deleted
#: when the list empties. It is a runtime sidecar like ``.driver.json``:
#: ``drive()`` adds ``.merge-intent.json*`` to the mailbox ``.gitignore``, and
#: it needs no git history of the mailbox at all.
_MERGE_INTENT_FILE = ".merge-intent.json"
#: Test hook only: ``False`` turns the record off (no write, no delete) so a
#: base-revert sanity test can prove the gap-crash tests depend on it.
_MERGE_INTENT_ENABLED = True
_INTENT_LOCK = threading.Lock()


def _read_intents(mailbox: Path) -> list[dict]:
    try:
        doc = json.loads((Path(mailbox) / _MERGE_INTENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = doc.get("intents") if isinstance(doc, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def _write_intents(mailbox: Path, intents: list[dict]) -> None:
    path = Path(mailbox) / _MERGE_INTENT_FILE
    if not intents:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"{_MERGE_INTENT_FILE}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.write(fd, (json.dumps({"version": 1, "intents": intents}, indent=2, sort_keys=True)
                      + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _intent_key(intent: dict) -> tuple:
    return (intent.get("slice"), intent.get("branch"), intent.get("repo"))


def _intent_begin(mailbox: Path, *, slice_id: str, branch: str, repo_path: Path,
                  repo_field: str | None, pre_head: str, run_token: str) -> dict | None:
    """Durably record a merge about to start. Raises on a write failure: a
    merge with no record could not be reconciled after a crash, so it must
    not start."""
    if not _MERGE_INTENT_ENABLED:
        return None
    intent = {"slice": slice_id, "branch": branch, "repo": str(repo_path),
              "repo_field": repo_field, "pre_head": pre_head, "run_token": run_token,
              "at": drv._now_iso()}
    with _INTENT_LOCK:
        kept = [i for i in _read_intents(mailbox) if _intent_key(i) != _intent_key(intent)]
        _write_intents(mailbox, kept + [intent])
    return intent


def _intent_done(mailbox: Path, intent: dict | None) -> None:
    """Drop one record (its retirement is durable, or its merge never
    happened). Best effort: a record that survives is harmless -- resume
    finds the retirement present and just drops it."""
    if intent is None:
        return
    try:
        with _INTENT_LOCK:
            _write_intents(mailbox, [i for i in _read_intents(mailbox)
                                     if _intent_key(i) != _intent_key(intent)])
    except OSError:
        pass


def _ensure_intent_gitignore(mailbox: Path) -> None:
    """Append ``.merge-intent.json*`` to the mailbox ``.gitignore`` (the
    shared ``MAILBOX_RUNTIME_IGNORES`` list predates the record). Idempotent,
    append-only, best effort."""
    path = Path(mailbox) / ".gitignore"
    pattern = _MERGE_INTENT_FILE + "*"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError:
        return
    if any(line.strip().lstrip("/") in (pattern, _MERGE_INTENT_FILE) for line in text.splitlines()):
        return
    prefix = "" if not text or text.endswith("\n") else "\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{pattern}\n")
    except OSError:
        pass


def _git_worktree_add(repo_path: Path, path: Path, start: str,
                      branch: str | None = None) -> "subprocess.CompletedProcess[str]":
    """``git worktree add`` (``-b branch`` for a builder, ``--detach`` for an
    eval), retried ONCE when it fails because ``.git/worktrees`` vanished
    mid-add: a concurrent ``worktree remove`` of the last sibling worktree
    deletes the then-empty directory between the add's own mkdir steps (git
    reports ``could not create directory of '.git/worktrees/...': No such file
    or directory``). The branch is created before the worktree, so the retry
    checks the already-created branch out instead of creating it again."""
    # every worktree the driver creates lives under ``<repo>/.trio-opencode/``:
    # make sure THIS repo's common git dir excludes it (a declared repo that
    # joined after start included) before an agent can write there
    drv._exclude_driver_scratch(repo_path)

    def _run(first: bool) -> "subprocess.CompletedProcess[str]":
        if branch is None:
            args = ["--detach", str(path), start]
        elif first:
            args = ["-b", branch, str(path), start]
        else:
            args = [str(path), branch]
        return subprocess.run(["git", "-C", str(repo_path), "worktree", "add", *args],
                              capture_output=True, text=True)
    r = _run(True)
    if r.returncode != 0 and "worktrees" in r.stderr and "No such file or directory" in r.stderr:
        if branch is not None:
            exists = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                capture_output=True).returncode == 0
            if not exists:
                return _run(True)
        subprocess.run(["git", "-C", str(repo_path), "worktree", "prune"], capture_output=True)
        r = _run(False)
    return r


def _git_path(repo_path: Path, name: str) -> Path | None:
    r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--git-path", name],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else Path(repo_path) / p


def _clear_completed_merge_state(repo_path: Path) -> bool:
    """If ``repo_path`` has a MERGE_HEAD that is already an ancestor of HEAD
    (the merge committed; only the cleanup was cut short), forget the merge
    state without touching the index or tree: ``git merge --quit``, falling
    back to removing MERGE_HEAD/MERGE_MSG/MERGE_MODE. Returns whether state
    was cleared. A MERGE_HEAD that is NOT an ancestor is a live (conflicted)
    merge and is left alone. Never raises."""
    try:
        r = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return False
        if subprocess.run(["git", "-C", str(repo_path), "merge-base", "--is-ancestor",
                           r.stdout.strip(), "HEAD"], capture_output=True).returncode != 0:
            return False
        q = subprocess.run(["git", "-C", str(repo_path), "merge", "--quit"],
                           capture_output=True, text=True)
        gone = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                              capture_output=True).returncode != 0
        if q.returncode != 0 or not gone:
            for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
                path = _git_path(repo_path, name)
                if path is not None:
                    path.unlink(missing_ok=True)
            gone = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "-q", "--verify",
                                   "MERGE_HEAD"], capture_output=True).returncode != 0
        return gone
    except Exception:  # noqa: BLE001 - best effort
        return False


def _sweep_stale_merge_state(ctx: "drv.RunContext") -> None:
    """Before the Lead runs (start and resume alike), clear a completed-merge
    MERGE_HEAD in every repo the run manages, record or no record: a stale
    MERGE_HEAD would make the next commit (the Evaluator's SHIP retirement
    commit) a merge commit that touches no mailbox paths, ending the run
    ``needs_retirement``. A live conflicted merge is logged, never touched."""
    repos = [ctx.repo]
    if ctx.lead_record:
        repos += [Path(info["path"]) for info in (ctx.lead_record.repos or {}).values()]
    try:
        iteration = TL._read_state(ctx.live_mailbox / "STATE.md").get("iteration", "0")
    except Exception:  # noqa: BLE001
        iteration = "0"
    seen: set[str] = set()
    for repo_path in repos:
        key = str(repo_path)
        if key in seen or not Path(repo_path).is_dir():
            continue
        seen.add(key)
        try:
            if _clear_completed_merge_state(repo_path):
                TL._append_log(ctx.live_mailbox,
                               f"- iter {iteration} | loop | cleared stale MERGE_HEAD in {repo_path} "
                               "(merge already committed)")
            elif subprocess.run(["git", "-C", str(repo_path), "rev-parse", "-q", "--verify",
                                 "MERGE_HEAD"], capture_output=True).returncode == 0:
                TL._append_log(ctx.live_mailbox,
                               f"- iter {iteration} | loop | WARNING: {repo_path} has an unfinished merge "
                               "(MERGE_HEAD not in HEAD); left untouched")
        except Exception:  # noqa: BLE001
            continue


def _reconcile_merge_retirement(ctx: "drv.RunContext") -> None:
    """Crash-consistent retirement, driven by the merge-intent record
    (see :data:`_MERGE_INTENT_FILE`). Called from :func:`drive` BEFORE the
    Lead ever runs (fresh start or resume alike). For each record in this
    mailbox's ``.merge-intent.json``:

    * a record from a different run token is dropped untouched (it describes
      another harness invocation's merge, never this run's);
    * otherwise the record's repo is searched, first-parent and bounded to
      ``pre_head..HEAD``, for the exact commit ``merge slice <slice>
      (<branch>)``. Found: the missing ``retired:`` entry is appended (once --
      ``append_retired`` is idempotent and the pair is checked first), logged
      as a reconciled crash-orphaned merge. Not found: the crash fell before
      the merge committed, so any half-done merge state is aborted and the
      record dropped -- the slice is simply still unbuilt and the Lead
      re-plans it;
    * the record is deleted only after that. A git failure keeps it for the
      next resume.

    A merge commit with NO record is never imported, whatever the history
    holds: no git-history walk exists, so a reused mailbox, a prior goal's
    merges, an ignored/untracked/out-of-repo mailbox and a commit made after
    the crash cannot change the outcome. Best effort; never raises."""
    live_mailbox = ctx.live_mailbox
    try:
        intents = _read_intents(live_mailbox)
    except Exception:  # noqa: BLE001
        return
    if not intents or not (live_mailbox / "QUEUE.md").is_file():
        return
    try:
        iteration = TL._read_state(live_mailbox / "STATE.md").get("iteration", "0")
    except Exception:  # noqa: BLE001 - best effort reconcile, never raises
        iteration = "0"
    try:
        retired_pairs = {
            (e.get("slice"), e.get("sha"))
            for e in TL._read_queue(live_mailbox).get("retired", [])
        }
    except Exception:  # noqa: BLE001
        return
    for intent in intents:
        try:
            _reconcile_one_intent(ctx, intent, retired_pairs, iteration)
        except Exception:  # noqa: BLE001 - best effort, never blocks the run
            continue


def _reconcile_one_intent(ctx: "drv.RunContext", intent: dict, retired_pairs: set,
                          iteration: str) -> None:
    live_mailbox = ctx.live_mailbox
    slice_id, branch = intent.get("slice"), intent.get("branch")
    if intent.get("run_token") != ctx.token and slice_id and branch:
        # Loud, not silent: a record from a different `--run-token` is the
        # evidence of a crashed merge that THIS invocation will not
        # reconcile (the slice is left merged but un-retired and the run
        # wedges). Resume with the original token to reconcile it.
        TL._append_log(
            live_mailbox,
            f"- iter {iteration} | loop | WARNING: dropping merge-intent record for "
            f"{slice_id} ({branch}): run token {intent.get('run_token')!r} != this run's "
            f"{ctx.token!r} -- a crashed merge of this slice (if any) is NOT reconciled; "
            "resume with the original --run-token to reconcile it",
        )
        print(f"trio-opencode: WARNING dropping merge-intent record for {slice_id} "
              f"(run token mismatch); resume with the original --run-token to reconcile it",
              file=sys.stderr)
        _intent_done(live_mailbox, intent)
        return
    if not slice_id or not branch:
        _intent_done(live_mailbox, intent)
        return
    repo_path = Path(str(intent.get("repo") or ""))
    pre_head = str(intent.get("pre_head") or "")
    if not repo_path.is_dir() or not pre_head:
        _intent_done(live_mailbox, intent)
        return
    r = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--first-parent", "--reverse",
         "--format=%H%x09%s", f"{pre_head}..HEAD"], capture_output=True, text=True)
    if r.returncode != 0:
        if subprocess.run(["git", "-C", str(repo_path), "cat-file", "-e", f"{pre_head}^{{commit}}"],
                          capture_output=True).returncode == 0:
            return  # repo busy/unreadable: keep the record for the next resume
        _intent_done(live_mailbox, intent)  # pre_head is gone: nothing to resolve
        return
    subject = f"merge slice {slice_id} ({branch})"
    shas = [line.partition("\t")[0] for line in r.stdout.splitlines()
            if line.partition("\t")[2] == subject]
    if not shas:
        if subprocess.run(["git", "-C", str(repo_path), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode == 0:
            # The merge never committed: whatever is in flight is half-done.
            subprocess.run(["git", "-C", str(repo_path), "merge", "--abort"],
                           capture_output=True, text=True)
        _intent_done(live_mailbox, intent)
        return
    # The merge DID commit. A driver killed while that `git merge` was still
    # running can leave MERGE_HEAD behind even though HEAD already advanced;
    # `merge --abort` would be wrong there, and a later commit would become a
    # two-parent merge commit (the SHIP commit then touches no mailbox paths).
    _clear_completed_merge_state(repo_path)
    sha = shas[-1]
    if (slice_id, sha) not in retired_pairs:
        at = TL._git(repo_path, "log", "-1", "--format=%cI", sha).stdout.strip()
        if olqueue.append_retired(live_mailbox, slice_id=slice_id, sha=sha, at=at,
                                  repo=intent.get("repo_field")):
            TL._append_log(
                live_mailbox,
                f"- iter {iteration} | loop | reconciled crash-orphaned merge for "
                f"{slice_id}@{sha[:12]}: appended the missing retired: entry",
            )
        retired_pairs.add((slice_id, sha))
    _intent_done(live_mailbox, intent)


def _remove_leftover_eval_worktrees(ctx: "drv.RunContext") -> None:
    """Belt-and-suspenders sweep after the run: every slice-eval/integration-
    eval worktree already removes itself in a ``finally``, but a crash or an
    abandoned (exit-drain-timed-out) slice-eval can leave one behind."""
    repo_paths = [ctx.repo]
    if ctx.lead_record:
        repo_paths += [Path(info["path"]) for info in (ctx.lead_record.repos or {}).values()]
    for repo_path in repo_paths:
        d = repo_path / drv.BUILDER_WORKTREES_DIR
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if not child.is_dir() or not child.name.startswith("eval-"):
                continue
            subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(child)],
                           capture_output=True, text=True)


def _restore_resume_state(ctx: "drv.RunContext") -> dict[str, Any] | None:
    """H5: mirrors lockstep's own ``driver._normalize_resume_phase`` crash-
    window restore (same persisted pre-turn ``state_snapshot``, same
    ``run_token`` gate), but for open-loop: read the PREVIOUS run's
    ``.driver.json`` and restore :data:`driver.OWNED_STATE_KEYS` from it
    BEFORE anything in ``drive()`` can overwrite that file (the caller's
    very first statement, before even constructing the ``OpenLoopRunner``
    -- open-loop has no ``steplib.begin``/``next()`` cursor of its own for
    a crashed Evaluator turn to have corrupted, but the SAME STATE.md keys
    (``iteration``, ``phase``, ``evaluated_sha``, ...) are driver-owned here
    too, and ``_StateGuard`` only reasserts them turn-by-turn going
    forward -- it has nothing to restore FROM for a turn that crashed
    before this process even started).

    Also returns the previous run's ``builders`` map (``.driver.json``'s
    own, keyed ``<slice>@<sha12>``) for the caller to fold into the fresh
    ``OpenLoopRunner`` via ``load_resumed_builders`` -- SLICE QUALITY's
    authored-by/kill-check bookkeeping otherwise never survives a crash,
    even though the retired slice itself does (QUEUE.md is the authority).

    A ``.driver.json`` with no ``run_token`` at all predates this check and
    is treated as a match; a recorded token that does not match THIS run's
    (a different harness invocation sharing the mailbox, or a stale file a
    driver that never writes ``.driver.json`` left behind) must never roll
    STATE back to that other run's cursor or adopt its builders -- both are
    skipped, as if the file were absent. Best effort throughout; never
    raises."""
    try:
        prior = drv._read_json(ctx.driver_json_path)
    except Exception:  # noqa: BLE001 - best effort guard
        return None
    if not isinstance(prior, dict):
        return None
    prior_token = prior.get("run_token")
    if prior_token is not None and prior_token != ctx.token:
        return None
    snapshot = prior.get("state_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        drv._restore_owned_state(ctx, snapshot, ctx.live_mailbox / "STATE.md", "resume")
    builders = prior.get("builders")
    return builders if isinstance(builders, dict) else None


def _install_idle_signal_handlers(ctx: "drv.RunContext", stop_now: dict) -> tuple:
    """H4: ``driver.run()``'s own SIGTERM/SIGINT handlers (installed for the
    whole ``run()`` call) only set flags -- ``TL.run_open_loop``'s own poll
    loop (``wake_event.wait``) never checks them while no turn is live, so a
    SIGTERM/SIGINT that arrives between turns would otherwise never stop the
    run. For the DURATION of ``TL.run_open_loop`` only (the caller restores
    the previous handlers via :func:`_restore_signal_handlers` in its own
    ``finally``), install handlers that also set ``ctx.cancel`` (killing
    every live turn, same as H2) and raise ``KeyboardInterrupt`` on the main
    thread -- ``run_open_loop``'s own ``finally`` drains in-flight
    slice-evals and releases the mailbox lock before that exception
    propagates (documented interrupt-safe); H3 maps it to ``status:
    cancelled``."""

    def handler(signum, _frame):  # noqa: ANN001
        stop_now["flag"] = True
        stop_now["code"] = 143 if signum == signal.SIGTERM else 130
        ctx.cancel.set()
        raise KeyboardInterrupt()

    old_term = signal.signal(signal.SIGTERM, handler)
    old_int = signal.signal(signal.SIGINT, handler)
    return old_term, old_int


def _restore_signal_handlers(old_term, old_int) -> None:  # noqa: ANN001
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


#: ``prompts/generate.py`` renders the canonical evaluator's whole-goal
#: rigor block here (the same text trioctl appends from the Omnigent copy,
#: ``OmnigentRunner._integration_rigor``): it is appended to every
#: integration-eval prompt, never to a slice-eval (``_whole_goal_eval``).
INTEGRATION_RIGOR_PATH = steplib.OPENCODE_DRIVER_ROOT / "prompts" / "integration-rigor.md"


def integration_rigor() -> str:
    try:
        return INTEGRATION_RIGOR_PATH.read_text(encoding="utf-8").rstrip("\n") + "\n"
    except OSError:
        return ""


# ==================================================================== drive


def _final_status_for_code(ctx: "drv.RunContext", code: int, state: dict) -> tuple[str, int, str | None]:
    if code == 0:
        return "shipped", 0, None
    if code == 2:
        return "blocked", 2, None
    if code == 3:
        return "error", 3, "open-loop: run ended in error (see LOG.md)"
    if code == 4:
        return "max_iterations", 4, None
    if code == 5:
        # D14: 5 -> needs_human when STATE says so, else a busy-lock refusal.
        if str(state.get("status", "")).strip() == "needs_human":
            return "needs_human", 5, None
        return "refused", 9, ("another trio-opencode driver is running on this mailbox, "
                              "or the mailbox lock is busy")
    if code == 6:
        return "needs_retirement", 6, None
    if code == 8:
        return "needs_land", 8, None
    return "error", code, f"open-loop: run_open_loop returned unexpected code {code}"


def _finalize_result(ctx: "drv.RunContext", runner: OpenLoopRunner, code: int | None,
                     settings: dict, *, exc: BaseException | None = None) -> dict[str, Any]:
    """``exc`` (H3): set when ``TL.run_open_loop`` itself raised rather than
    returning a stop ``code`` -- a runner exception it re-raises instead of
    swallowing (it swallows ``DriverStop`` into ``runner.fatal.stop`` ONLY
    when the stop surfaces through the normal RoleRunner protocol; anything
    else, plus ``KeyboardInterrupt``, comes straight through), or H4's own
    signal handler's ``KeyboardInterrupt``. ``runner.fatal.stop`` still takes
    priority when set (the DriverStop that caused ``exc`` in the first
    place, most of the time); ``KeyboardInterrupt`` with no captured stop
    maps to ``cancelled``/``ctx.cancel_code``; anything else to a bare
    ``error``/code 3 carrying the exception's own text."""
    try:
        state = TL._read_state(ctx.live_mailbox / "STATE.md")
    except Exception:  # noqa: BLE001
        state = {}
    try:
        iteration = int(str(state.get("iteration") or 0))
    except ValueError:
        iteration = 0

    stop = runner.fatal.stop
    if stop is not None:
        status, result_code, reason = stop.status, stop.code, stop.reason
        role_denials = stop.extra.get("role_denials", ctx.role_denials)
    elif exc is not None:
        if isinstance(exc, KeyboardInterrupt):
            status, result_code, reason = "cancelled", ctx.cancel_code.get("code", 130), None
        else:
            status, result_code, reason = "error", 3, f"{type(exc).__name__}: {exc}"
        role_denials = ctx.role_denials
    else:
        status, result_code, reason = _final_status_for_code(ctx, code, state)
        role_denials = ctx.role_denials

    final: dict[str, Any] = {
        "status": status, "code": result_code, "harness": drv.HARNESS,
        "iteration": iteration, "role_denials": role_denials,
        "logs_dir": str(ctx.log_dir),
        "lead_worktree": str(ctx.lead_record.path) if ctx.lead_record else None,
        "branch": ctx.lead_record.branch if ctx.lead_record else None,
        "land": None,
        "open_loop": {"isolate_workers": settings["isolate_workers"],
                     "slice_eval_concurrency": settings["slice_eval_concurrency"],
                     "kill_check": not settings["kill_check_cli_disabled"]},
    }
    if reason:
        final["reason"] = reason
    if ctx.acceptance:
        final["acceptance"] = {"enabled": True}
        if ctx.acc_log.get("author_isolation"):
            final["acceptance"]["author_isolation"] = ctx.acc_log["author_isolation"]
        if ctx.acc_log.get("degraded"):
            final["acceptance"]["degraded"] = ctx.acc_log["degraded"]
    if status == "shipped" and str(state.get("landed", "")).strip():
        final["land"] = {"status": "landed", "landed": state.get("landed"),
                         "target_ref": state.get("target_ref")}
    elif status == "needs_land":
        final["land"] = {"status": "needs_land", "phase": state.get("phase")}
    return final


def drive(ctx: "drv.RunContext", *, mode: str, max_iterations: int, settings: dict[str, Any],
         stop_now: dict) -> dict[str, Any]:
    """The open-loop counterpart of ``driver._drive``: ``ctx`` is already
    built the normal way (``driver.run()`` has done validation, the kernel
    flock, the resume orphan kill, ``rootfree.prepare``, ``ocgen.generate``);
    this drives ``steplib.TL.run_open_loop`` to a stop and returns the same
    result shape lockstep's ``_drive`` does, plus ``open_loop`` (D14)."""
    del mode  # H5's restore is gated on run_token, not on start vs. resume
    # H5: read a crashed previous run's `.driver.json` and restore
    # driver-owned STATE before ANYTHING else here can overwrite that file
    # (`_reclaim_stale_worktrees`/the registry write below never touch it,
    # but this is still the very first statement on purpose).
    resumed_builders = _restore_resume_state(ctx)
    live_mailbox = ctx.live_mailbox
    if not (live_mailbox / "QUEUE.md").is_file():
        # Defensive only -- `detect_open_loop` (D1) already required a
        # QUEUE.md to exist somewhere (the root mailbox, or an existing
        # Lead worktree's live one that `rootfree.prepare` just seeded
        # from); left empty of fences so the first `append_retired`/fault
        # write creates them block-style (an inline `retired: []` is not a
        # fence `_locate_fence` recognises, which would leave a stray
        # unparsed empty block once a real one is appended alongside it).
        live_mailbox.mkdir(parents=True, exist_ok=True)
        olqueue.ensure_queue_file(live_mailbox)

    # Lockstep gets this for free from `steplib.begin` (`op_begin`, never
    # called here -- D2): the mailbox's own `.gitignore` must list the
    # runtime sidecars (`.session.json`, `.driver.json`, ...) BEFORE
    # anything commits under the mailbox (the Lead-review/SHIP retirement
    # commits' own `git add -A -- <mailbox_rel>` would otherwise pick them
    # up as real product changes).
    try:
        steplib.NS._ensure_mailbox_gitignore(live_mailbox)
    except Exception:  # noqa: BLE001 - best effort, mirrors op_begin's own call site
        pass
    _ensure_intent_gitignore(live_mailbox)

    # Blocking issue #1: finish any merge a crash left in flight (named by
    # the merge-intent record, never inferred from git history) BEFORE the
    # Lead ever runs -- see `_reconcile_merge_retirement`'s own docstring.
    # A mailbox with no record costs one failed file read.
    _reconcile_merge_retirement(ctx)
    _sweep_stale_merge_state(ctx)

    runner = OpenLoopRunner(ctx, settings=settings)
    if resumed_builders:
        runner.load_resumed_builders(resumed_builders)
    _reclaim_stale_worktrees(ctx)
    drv._write_registry(
        ctx.root_mailbox, live_mailbox=str(live_mailbox), repo=str(ctx.repo),
        lead_worktree=str(ctx.lead_record.path) if ctx.lead_record else None,
        branch=ctx.lead_record.branch if ctx.lead_record else None,
        target=ctx.lead_record.target if ctx.lead_record else None,
        run_token=ctx.token, exec_id=ctx.exec_id, pid=os.getpid(), state="running",
        begun_at=drv._now_iso(), acceptance_enabled=(True if ctx.acceptance else None),
        open_loop=True,
    )

    # H9: lockstep gets its scratch dir from `steplib.begin`; open-loop
    # (D2: never calls `begin`) makes its own under this run's own
    # `run_dir` -- removed best effort once the run stops.
    tmpdir_path: Path | None = ctx.run_dir / "tmp"
    try:
        tmpdir_path.mkdir(parents=True, exist_ok=True)
        ctx.tmpdir = str(tmpdir_path)
    except OSError:
        tmpdir_path = None

    guard = _StateGuard(ctx)
    guard.install()
    runner.state_guard = guard
    orig_sidecar = TL._write_open_loop_sidecars
    TL._write_open_loop_sidecars = _make_sidecar_writer(ctx)
    land_hook = make_land_hook(ctx) if ctx.root_free and ctx.lead_record else None
    # No author time limit by default (`acceptance_wait_seconds` null/0);
    # an author failure degrades to no pack (`DegradableAcceptance`), and
    # `on_degrade` tells the prompts to stop mentioning a pack that never was.
    def _on_acceptance_degrade(reason: str) -> None:
        ctx.acc_log["degraded"] = reason
        ctx.out(f"acceptance: DEGRADED to no frozen pack ({reason[:200]}); continuing "
                "without frozen acceptance")

    acceptance_cfg = ({"enabled": True, "wait_s": settings.get("acceptance_wait_seconds"),
                       "on_degrade": _on_acceptance_degrade}
                      if ctx.acceptance else None)
    orig_make_acceptance = TL.make_acceptance
    if ctx.acceptance:
        TL.make_acceptance = _make_degradable_acceptance

    # H4: real signal handling for the one window `driver.run()`'s own
    # flag-only handlers miss -- idle between turns, inside
    # `TL.run_open_loop`'s own poll loop.
    old_term, old_int = _install_idle_signal_handlers(ctx, stop_now)
    code: int | None = None
    caught: BaseException | None = None
    try:
        code = TL.run_open_loop(
            live_mailbox, max_iterations, runner, runner, repo=ctx.repo,
            poll_seconds=settings["poll_seconds"],
            slice_eval_concurrency=settings["slice_eval_concurrency"],
            slice_eval_drain_seconds=settings.get("slice_eval_drain_seconds"),
            land=land_hook, acceptance=acceptance_cfg,
            # The Lead here is a planner (`_validate_plan` refuses to re-build a
            # retired slice, and a gate failure is not a code fault it can
            # fix), so a stuck commit gate is not handed back to it: after the
            # grace period the run stops `status: error` with the gate's reason.
            max_gate_repair_rounds=0,
        )
    except BaseException as exc:  # noqa: BLE001 - H3: finalize instead of crashing drive()
        caught = exc
    finally:
        # Blocking issue #2 (exit drain): `run_open_loop` can return (or
        # raise) with a slice-eval/integration-eval turn still alive -- its
        # own drain window (`slice_eval_drain_seconds`) marks an overrun
        # slice-eval "abandoned" in ITS bookkeeping, but never kills the
        # underlying `opencode` process or the Python thread running it.
        # `ctx.cancel` is the SAME event `drv._call_role`/`runner.run_turn`
        # pass to EVERY role turn, builder, slice-eval and integration-eval
        # alike (`_Fatal`'s own docstring), so setting it here kills the
        # abandoned turn's process group within its own ~0.2s poll and the
        # turn returns `cancelled` -- which lets that thread's own
        # `finally` (worktree removal) run to completion in well under a
        # second, instead of the process lingering for the turn's full
        # `evaluator_turn_seconds`. Then wait (briefly, bounded) for every
        # live turn this runner still knows about to actually clear BEFORE
        # the leftover-worktree sweep below runs -- otherwise that sweep
        # can race a still-exiting eval for the same worktree.
        # Snapshot BEFORE cancelling: an abandoned turn's own resulting
        # `DriverStop("cancelled", ...)` (captured into `runner.fatal.stop`
        # the same way any other fatal stop is, `OpenLoopRunner.run`'s own
        # except clause) is bookkeeping ONLY here -- it must never
        # masquerade as the run's own stop reason when the real one
        # (`code`/*exc* above) was already decided before this cleanup
        # cancellation ran. A stop that was ALREADY there (a genuine fatal
        # stop during the run itself, e.g. a permission denial) is left
        # untouched -- that one legitimately explains `code`.
        pre_cancel_stop = runner.fatal.stop
        ctx.cancel.set()
        deadline = time.monotonic() + 10.0
        while runner.has_live_turns() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pre_cancel_stop is None and runner.fatal.stop is not None:
            runner.fatal.stop = None
        _restore_signal_handlers(old_term, old_int)
        guard.uninstall()
        TL._write_open_loop_sidecars = orig_sidecar
        TL.make_acceptance = orig_make_acceptance
        if tmpdir_path is not None:
            shutil.rmtree(tmpdir_path, ignore_errors=True)

    # H3: `TL.run_open_loop` re-raises a runner exception it cannot map to
    # one of its own stop codes (and `KeyboardInterrupt`, H4's own) instead
    # of swallowing it -- `_finalize_result` still produces a normal result
    # (and still gets to run every post-stop step below: `.opencode-result
    # .json`, the registry's `finished` record, leftover-worktree cleanup)
    # exactly as it would for a `code` it got back normally.
    final = _finalize_result(ctx, runner, code, settings, exc=caught)
    _remove_leftover_eval_worktrees(ctx)
    try:
        ctx.driver_json_path.unlink()
    except OSError:
        pass
    drv._atomic_write_json(live_mailbox / drv.RESULT_FILE, final)
    if final.get("land") and final["land"].get("status") == "landed" and ctx.lead_record:
        final["teardown"] = rootfree.teardown(ctx.lead_record)
        drv._atomic_write_json(ctx.root_mailbox / drv.RESULT_FILE, final)
    drv._write_registry(ctx.root_mailbox, state="finished", status=final.get("status"),
                        finished_at=drv._now_iso(),
                        acceptance=final.get("acceptance") if ctx.acceptance else None)
    return final

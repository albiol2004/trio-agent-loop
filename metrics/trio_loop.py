#!/usr/bin/env python3
"""Stdlib Trio loop state machine: gates, verdict, repair, resume.

This is the only implementation of those semantics. portable/driver.sh
(and later trioctl) must call run_loop(); they must not re-code the
state machine. Verdict parsing is delegated to trio-metrics.py so
this driver accepts exactly what trio-check.py accepts.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

def _load_metrics_module():
    path = Path(__file__).resolve().with_name("trio-metrics.py")
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

# Verdict and slice parsing stay in the existing shared metrics module.
_METRICS = _load_metrics_module()
VERDICT_RE = _METRICS.VERDICT_RE
parse_verdict_scope = _METRICS.parse_verdict_scope
KNOWN_VERDICTS = tuple(_METRICS.KNOWN_VERDICTS)
A_LEAD_RE = _METRICS.A_LEAD_RE
SCOPE_RE = re.compile(r"^scope=(design|local:[^\s]+)$", re.IGNORECASE)
# iteration/status are MAILBOX-SCHEMA.md fields. phase is the resume
# cursor this driver owns (trio-metrics parse_state ignores unknown keys).
STATE_RE = re.compile(
    r"^\s*(?:-\s+)?(iteration|status|phase)\s*:\s*(.*)$", re.IGNORECASE
)
ROLE_LOG_RE = re.compile(
    r"^\s*-\s*(?:\w+\s+)?(?:iter|iteration)\s+(\d+)\s*\|\s*"
    r"(lead|repair)\s*\|",
    re.IGNORECASE,
)
OUTCOMES = {
    "SHIP": ("shipped", "shipped", 0),
    "BLOCKED": ("blocked", "blocked", 2),
    "NEEDS_HUMAN": ("needs_human", "needs_human", 5),
}
TERMINAL_CODES = {"shipped": 0, "blocked": 2, "needs_human": 5, "error": 3}

class RoleRunner(Protocol):
    def run(
        self,
        role: str,
        iteration: int,
        mailbox: Path,
        context: dict | None = None,
    ) -> int:
        ...

def _read_state(path: Path) -> dict[str, str]:
    """Create missing state, then read the keys owned by this driver."""
    if not path.is_file():
        path.write_text(
            "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
        )
    state = {"iteration": "0", "status": "ready", "phase": "idle"}
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    for line in lines:
        match = STATE_RE.match(line)
        if match:
            state[match.group(1).lower()] = match.group(2).strip()
    return state

def _update_state(path: Path, updates: dict[str, str]) -> None:
    """Replace owned keys while preserving all unrelated state lines."""
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    found: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = STATE_RE.match(line)
        key = match.group(1).lower() if match else None
        if key in updates:
            line = f"{key}: {updates[key]}"
            found.add(key)
        result.append(line)
    for key in ("iteration", "status", "phase"):
        if key in updates and key not in found:
            result.append(f"{key}: {updates[key]}")
    path.write_text(
        "\n".join(result) + ("\n" if result else ""), encoding="utf-8"
    )


# Open-loop-only: the top-level STATE.md `verdict:` hot-summary line from
# MAILBOX-SCHEMA.md ("STATE.md `verdict:` and `eval:` hot-summary lines").
# Kept separate from STATE_RE/_update_state so lockstep's owned-key
# behaviour (iteration/status/phase) stays byte-identical.
VERDICT_LINE_RE = re.compile(r"^\s*(?:-\s+)?verdict\s*:\s*(.*)$", re.IGNORECASE)


def _write_state_verdict(state_path: Path, word: str) -> None:
    """Set (or idempotently replace) the STATE.md `verdict:` line after an
    integration verdict, preserving every other line untouched."""
    lines = state_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    result: list[str] = []
    replaced = False
    for line in lines:
        if VERDICT_LINE_RE.match(line):
            if not replaced:
                result.append(f"verdict: {word}")
                replaced = True
            continue
        result.append(line)
    if not replaced:
        result.append(f"verdict: {word}")
    state_path.write_text(
        "\n".join(result) + ("\n" if result else ""), encoding="utf-8"
    )


def _write_driver_state(
    mailbox: Path, runner: RoleRunner, iteration: int, phase: str
) -> None:
    """Persist the loop cursor and runner sessions outside the mailbox schema."""
    payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "session_ids": getattr(runner, "session_ids", {}) or {},
    }
    (mailbox / ".driver.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )

def _append_log(mailbox: Path, line: str) -> None:
    path = mailbox / "LOG.md"
    if not path.is_file():
        path.write_text("# Trio loop log\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        path.write_text(text + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as log:
        log.write(f"{line}\n")

def _counter(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if value >= 0 else 0

def _number(value: str) -> int:
    match = re.match(r"\d+", value.strip())
    return int(match.group(0)) if match else 0

def _acquire_lock(mailbox: Path) -> Path | None:
    """mkdir is atomic; replace a lock only after its pid is stale."""
    lock = mailbox / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            pid = int((lock / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
            except OSError:
                pass
            else:
                print(
                    f"Mailbox {mailbox}/ is owned by a live driver "
                    f"(pid {pid}).",
                    file=sys.stderr,
                )
                return None
        print(
            f"Removing stale lock on {mailbox}/ "
            f"(pid {pid or 'unknown'} is gone).",
            file=sys.stderr,
        )
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    return lock

def _commit_gate(mailbox: Path, repo: Path | None) -> tuple[bool, str]:
    """Run trio-shadow; exit 0 passes and exits 1/2 fail."""
    script = Path(__file__).resolve().with_name("trio-shadow.py")
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    command = [
        sys.executable,
        str(script),
        "--mailbox",
        str(target.resolve()),
        "--require-commits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        return False, f"commit gate could not run: {exc}"
    if result.returncode == 0:
        return True, "commit gate passed"
    return False, f"commit gate failed with exit {result.returncode}"

def _log_gate(mailbox: Path, iteration: int, role: str) -> tuple[bool, str]:
    """Require this role's Format-A line for the current iteration."""
    try:
        lines = (mailbox / "LOG.md").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return False, "LOG.md is missing or unreadable"
    for line in lines:
        match = A_LEAD_RE.match(line) if role == "lead" else ROLE_LOG_RE.match(line)
        if match and int(match.group(1)) == iteration:
            if role == "lead" or match.group(2).lower() == role:
                return True, "LOG gate passed"
    return False, f"LOG.md has no iter {iteration} | {role} | line"


run_commit_gate = _commit_gate
run_log_gate = _log_gate

def _invoke_runner(
    runner: RoleRunner,
    role: str,
    iteration: int,
    mailbox: Path,
    context: dict,
) -> int:
    """Call runner.run with `context` only when its signature accepts one.

    api:OpenLoopRunner: a runner whose `run` takes exactly the legacy 3
    positional arguments (every runner at HEAD, including the fakes in
    test_trio_loop.py/test_portable_driver.py and OmnigentRunner) is called
    unchanged. Signature inspection is preferred over a TypeError fallback
    so a real TypeError raised *inside* the runner is never swallowed.
    """
    try:
        params = inspect.signature(runner.run).parameters
        accepts_context = len(params) >= 4 or any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_context = True
    if accepts_context:
        return runner.run(role, iteration, mailbox, context)
    return runner.run(role, iteration, mailbox)

def _run_role(mailbox, iteration, role, runner, repo, state_path) -> bool:
    """Run a Lead/repair role, retrying a failed gate once."""
    for _attempt in range(2):
        result = _invoke_runner(runner, role, iteration, mailbox, {})
        if result != 0:
            raise RuntimeError(f"{role} runner failed with exit {result}")
        checks = (
            _commit_gate(mailbox, repo),
            _log_gate(mailbox, iteration, role),
        )
        failures = [note for ok, note in checks if not ok]
        if not failures:
            return True
    _update_state(state_path, {"status": "error", "phase": "error"})
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | gate breach after {role}: "
        + "; ".join(failures),
    )
    return False

def _first_verdict(path: Path) -> tuple[str | None, str | None]:
    """Parse the first non-empty line with the shared verdict helpers."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    first = next((line.strip() for line in lines if line.strip()), "")
    match = VERDICT_RE.match(first)
    if not match:
        return None, None
    word = match.group(1).upper()
    if word not in KNOWN_VERDICTS:
        return None, None
    parsed_word, scope = parse_verdict_scope(first)
    if parsed_word != word:
        return None, None
    rest = first[match.end():].strip()
    if not rest.startswith("scope="):
        return word, None
    if word != "ITERATE" or not SCOPE_RE.fullmatch(rest):
        return None, None
    return word, scope.lower() if scope else None

def _apply_verdict(
    mailbox, state_path, repair_path, iteration, verdict, scope
) -> int | None:
    """Persist a terminal verdict or queue the next ITERATE role."""
    if verdict in OUTCOMES:
        status, phase, code = OUTCOMES[verdict]
        _update_state(state_path, {"status": status, "phase": phase})
        return code
    repairs = _counter(repair_path)
    if scope in (None, "design"):
        repair_path.write_text("0\n", encoding="utf-8")
    elif repairs < 2:
        repair_path.write_text(f"{repairs + 1}\n", encoding="utf-8")
    else:
        repair_path.write_text("0\n", encoding="utf-8")
        _append_log(
            mailbox,
            f"- iter {iteration} | loop | repair cap hit; "
            "forcing full Lead",
        )
    _update_state(state_path, {"status": "running", "phase": "idle"})
    return None

def _run_lockstep(
    mailbox: Path,
    max_iterations: int,
    runner: RoleRunner,
    *,
    repo: Path | None = None,
) -> int:
    """Run the durable Lead/repair -> Evaluator state machine.

    This is the lockstep body from before open-loop mode existed, kept
    behaviourally byte-identical: same STATE.md keys, same LOG.md lines,
    same exit codes, same .driver.json writes (never .session.json).
    """
    mailbox = Path(mailbox).resolve()
    lock = _acquire_lock(mailbox)
    if lock is None:
        return 5
    try:
        state_path = mailbox / "STATE.md"
        if not (mailbox / "LOG.md").is_file():
            (mailbox / "LOG.md").write_text(
                "# Trio loop log\n", encoding="utf-8"
            )
        state = _read_state(state_path)
        repair_path = mailbox / ".repairs"
        _write_driver_state(
            mailbox,
            runner,
            _number(state["iteration"]),
            state["phase"].strip(),
        )
        while True:
            status = state["status"].strip().lower()
            terminal = TERMINAL_CODES.get(status.split()[0] if status else "")
            iteration = _number(state["iteration"])
            if terminal is not None:
                _write_driver_state(
                    mailbox, runner, iteration, state["phase"].strip()
                )
                return terminal
            # Crash-resume: Lead+gates already landed, so do not bump
            # iteration or re-run Lead (fixes driver.sh:107-108).
            if state["phase"].strip().lower() == "lead-done":
                result = _invoke_runner(runner, "evaluator", iteration, mailbox, {})
                if result != 0:
                    raise RuntimeError(
                        f"evaluator runner failed with exit {result}"
                    )
                verdict, scope = _first_verdict(mailbox / "VERDICT.md")
                if verdict is None:
                    _update_state(
                        state_path, {"status": "error", "phase": "error"}
                    )
                    _append_log(
                        mailbox,
                        f"- iter {iteration} | loop | unparseable verdict",
                    )
                    _write_driver_state(mailbox, runner, iteration, "error")
                    return 3
                code = _apply_verdict(
                    mailbox,
                    state_path,
                    repair_path,
                    iteration,
                    verdict,
                    scope,
                )
                state = _read_state(state_path)
                _write_driver_state(
                    mailbox,
                    runner,
                    _number(state["iteration"]),
                    state["phase"].strip(),
                )
                if code is not None:
                    return code
                continue
            phase = state["phase"].strip().lower()
            if phase in ("lead-running", "repair-running"):
                role = phase.split("-", 1)[0]
            else:
                # Budget applies to new passes, not interrupted role retries.
                if iteration >= max_iterations:
                    return 4
                role = "repair" if _counter(repair_path) else "lead"
                iteration += 1
            _update_state(
                state_path,
                {
                    "iteration": str(iteration),
                    "status": "running",
                    "phase": f"{role}-running",
                },
            )
            _write_driver_state(mailbox, runner, iteration, f"{role}-running")
            if not _run_role(
                mailbox, iteration, role, runner, repo, state_path
            ):
                state = _read_state(state_path)
                _write_driver_state(
                    mailbox,
                    runner,
                    _number(state["iteration"]),
                    state["phase"].strip(),
                )
                return 3
            _update_state(
                state_path, {"status": "running", "phase": "lead-done"}
            )
            _write_driver_state(mailbox, runner, iteration, "lead-done")
            state = _read_state(state_path)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def run_loop(
    mailbox: Path,
    max_iterations: int,
    runner: RoleRunner,
    *,
    repo: Path | None = None,
    poll_seconds: float = 30,
    mode: str = "auto",
) -> int:
    """Dispatch to open-loop or lockstep (api: engine entry points).

    `mode="auto"` (default) selects open-loop iff `(mailbox / "QUEUE.md")`
    is a file; `mode="lockstep"` always takes the lockstep path (byte
    identical to the pre-open-loop behaviour); `mode="open-loop"` forces
    open-loop and, when QUEUE.md is missing, prints a message naming it to
    stderr and returns 3 WITHOUT touching STATE.md. `run_loop` passes the
    same `runner` object as both the open-loop lead_runner and eval_runner.
    """
    if mode not in ("auto", "open-loop", "lockstep"):
        raise ValueError(f"unknown mode: {mode!r} (expected auto/open-loop/lockstep)")
    mailbox_path = Path(mailbox).resolve()
    has_queue = (mailbox_path / "QUEUE.md").is_file()
    if mode == "lockstep" or (mode == "auto" and not has_queue):
        return _run_lockstep(mailbox, max_iterations, runner, repo=repo)
    if not has_queue:
        print(
            f"open-loop mode requires QUEUE.md in {mailbox_path}, "
            "but it was not found.",
            file=sys.stderr,
        )
        return 3
    return run_open_loop(
        mailbox,
        max_iterations,
        runner,
        runner,
        repo=repo,
        poll_seconds=poll_seconds,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_open_loop_sidecars(
    mailbox: Path,
    lead_runner: RoleRunner,
    eval_runner: RoleRunner,
    iteration: int,
    phase: str,
    lead_alive: bool,
    eval_alive: bool,
    started_at: str,
) -> None:
    """Write BOTH sidecars for an open-loop driver (api:OpenLoopSidecar).

    `.driver.json` keeps its pre-open-loop keys (pid/iteration/phase/
    session_ids) plus open_loop/lead_alive/eval_alive. `.session.json` is
    open-loop only (lockstep never writes it); `phase` is exactly one of
    "lead" | "evaluator" | "done" in both files.
    """
    session_ids: dict = {}
    session_ids.update(getattr(lead_runner, "session_ids", {}) or {})
    session_ids.update(getattr(eval_runner, "session_ids", {}) or {})
    driver_payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "session_ids": session_ids,
        "open_loop": True,
        "lead_alive": lead_alive,
        "eval_alive": eval_alive,
    }
    (mailbox / ".driver.json").write_text(
        json.dumps(driver_payload) + "\n", encoding="utf-8"
    )
    session_payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "open_loop": True,
        "lead_alive": lead_alive,
        "eval_alive": eval_alive,
        "started_at": started_at,
    }
    (mailbox / ".session.json").write_text(
        json.dumps(session_payload) + "\n", encoding="utf-8"
    )


def _read_plan_slice_ids(mailbox: Path) -> list[str]:
    """Slice ids declared in PLAN.md's `slices:` block, or [] if absent."""
    plan_path = mailbox / "PLAN.md"
    if not plan_path.is_file():
        return []
    text = plan_path.read_text(encoding="utf-8", errors="replace")
    try:
        slices = _METRICS.parse_slices(_METRICS.find_slices_block(text))
    except _METRICS.SliceParseError:
        return []
    return [sl["id"] for sl in slices]


def _lead_thread_body(
    mailbox: Path,
    state_path: Path,
    lead_runner: RoleRunner,
    max_iterations: int,
    write_sidecar,
    wake_event: threading.Event,
    stop_event: threading.Event,
    result_holder: dict,
    force_first_pass: bool = False,
) -> None:
    """One Lead-thread lifetime: run passes until every PLAN.md slice has a
    retired entry and no fault is open/taken, or the pass budget caps.

    `result_holder` receives exactly one of `outcome` ("done"/"capped"/
    "stopped") or `error` (the exception the runner raised), plus
    `finished` (set in `finally`, after outcome/error, so the poll loop can
    treat `finished` as the authoritative "thread has ended" signal instead
    of racing `Thread.is_alive()`).
    """
    try:
        first = True
        while True:
            if stop_event.is_set():
                result_holder["outcome"] = "stopped"
                return
            if not (first and force_first_pass):
                slice_ids = _read_plan_slice_ids(mailbox)
                queue = _METRICS.read_queue(mailbox)
                retired_ids = {e["slice"] for e in queue["retired"]}
                open_or_taken = [
                    f for f in queue["faults"] if f["status"] in ("open", "taken")
                ]
                if all(sid in retired_ids for sid in slice_ids) and not open_or_taken:
                    result_holder["outcome"] = "done"
                    return
            first = False
            state = _read_state(state_path)
            iteration = _number(state["iteration"]) + 1
            if iteration > max_iterations:
                result_holder["outcome"] = "capped"
                return
            _update_state(
                state_path, {"iteration": str(iteration), "status": "running"}
            )
            write_sidecar("lead", iteration, True, True)
            context = {
                "mode": "open-loop",
                "slice": None,
                "sha": None,
                "kind": "lead-pass",
            }
            result = _invoke_runner(lead_runner, "lead", iteration, mailbox, context)
            if result != 0:
                raise RuntimeError(f"lead runner failed with exit {result}")
    except BaseException as exc:  # noqa: BLE001 - propagated to the poll loop
        result_holder["error"] = exc
    finally:
        result_holder["finished"] = True
        wake_event.set()


def _per_slice_gate(mailbox: Path, repo: Path | None, slice_id: str) -> int:
    """v1 open-loop per-slice commit gate; returns the raw trio-shadow exit
    code (0 pass, 1 missing commits, 2 malformed/unknown-slice/error)."""
    script = Path(__file__).resolve().with_name("trio-shadow.py")
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    command = [
        sys.executable,
        str(script),
        "--mailbox",
        str(target.resolve()),
        "--require-commits",
        "--slice",
        slice_id,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return 2
    return result.returncode


def run_open_loop(
    mailbox: Path,
    max_iterations: int,
    lead_runner: RoleRunner,
    eval_runner: RoleRunner,
    *,
    repo: Path | None = None,
    poll_seconds: float = 30,
) -> int:
    """Run the open-loop Lead+Evaluator state machine against a QUEUE.md
    mailbox: one stdlib Lead thread plus an Evaluator poll loop on the
    calling thread, sharing the ONE mailbox lock this function acquires."""
    mailbox = Path(mailbox).resolve()
    lock = _acquire_lock(mailbox)
    if lock is None:
        return 5
    lead_thread: threading.Thread | None = None
    stop_event = threading.Event()
    try:
        state_path = mailbox / "STATE.md"
        verdict_path = mailbox / "VERDICT.md"
        repair_path = mailbox / ".repairs"
        if not (mailbox / "LOG.md").is_file():
            (mailbox / "LOG.md").write_text(
                "# Trio loop log\n", encoding="utf-8"
            )
        _read_state(state_path)
        started_at = _iso_now()

        def write_sidecar(
            phase: str, iteration: int, lead_alive: bool, eval_alive: bool
        ) -> None:
            _write_open_loop_sidecars(
                mailbox,
                lead_runner,
                eval_runner,
                iteration,
                phase,
                lead_alive,
                eval_alive,
                started_at,
            )

        def current_iteration() -> int:
            return _number(_read_state(state_path)["iteration"])

        def finish(code: int) -> int:
            write_sidecar("done", current_iteration(), False, False)
            return code

        wake_event = threading.Event()

        def spawn_lead(force_first_pass: bool = False) -> tuple[threading.Thread, dict]:
            holder: dict = {}
            thread = threading.Thread(
                target=_lead_thread_body,
                args=(
                    mailbox,
                    state_path,
                    lead_runner,
                    max_iterations,
                    write_sidecar,
                    wake_event,
                    stop_event,
                    holder,
                    force_first_pass,
                ),
                daemon=True,
            )
            thread.start()
            return thread, holder

        write_sidecar("lead", current_iteration(), True, True)
        lead_thread, lead_result = spawn_lead()

        graded: set[tuple[str, str]] = set()

        while True:
            lead_alive = not lead_result.get("finished", False)

            queue = _METRICS.read_queue(mailbox)
            latest_retired: dict[str, dict] = {}
            for entry in queue["retired"]:
                latest_retired[entry["slice"]] = entry
            verdict_text = (
                verdict_path.read_text(encoding="utf-8", errors="replace")
                if verdict_path.is_file()
                else ""
            )
            verdict_sections = _METRICS.parse_slice_verdicts(verdict_text)

            for slice_id, entry in latest_retired.items():
                sha = entry["sha"]
                key = (slice_id, sha)
                if key in graded:
                    continue
                if any(
                    v["slice"] == slice_id and sha.startswith(v["sha"])
                    for v in verdict_sections
                ):
                    graded.add(key)
                    continue
                write_sidecar(
                    "evaluator", current_iteration(), lead_alive, True
                )
                gate_code = _per_slice_gate(mailbox, repo, slice_id)
                if gate_code == 1:
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | commit gate "
                        f"failed for slice {slice_id}; skipping until re-retired",
                    )
                    continue
                if gate_code == 2:
                    stop_event.set()
                    wake_event.set()
                    lead_thread.join(timeout=5)
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | commit gate "
                        f"error for slice {slice_id}",
                    )
                    return finish(3)
                context = {
                    "mode": "open-loop",
                    "slice": slice_id,
                    "sha": sha,
                    "kind": "slice-eval",
                }
                result = _invoke_runner(
                    eval_runner, "evaluator", current_iteration(), mailbox, context
                )
                if result != 0:
                    raise RuntimeError(
                        f"evaluator runner failed with exit {result}"
                    )
                graded.add(key)

            lead_alive = not lead_result.get("finished", False)
            if not lead_alive:
                if lead_result.get("error") is not None:
                    exc = lead_result["error"]
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | lead thread "
                        f"failed: {exc}",
                    )
                    return finish(3)
                if lead_result.get("outcome") == "capped":
                    return finish(4)
                # outcome == "done": re-check with fresh data before
                # trusting it -- a slice-eval just above may have opened a
                # fault after the Lead thread already decided it was done.
                slice_ids = _read_plan_slice_ids(mailbox)
                queue = _METRICS.read_queue(mailbox)
                retired_ids = {e["slice"] for e in queue["retired"]}
                open_or_taken = [
                    f for f in queue["faults"] if f["status"] in ("open", "taken")
                ]
                if all(sid in retired_ids for sid in slice_ids) and not open_or_taken:
                    write_sidecar(
                        "evaluator", current_iteration(), False, True
                    )
                    iteration_now = current_iteration()
                    context = {
                        "mode": "open-loop",
                        "slice": None,
                        "sha": None,
                        "kind": "integration-eval",
                    }
                    result = _invoke_runner(
                        eval_runner, "evaluator", iteration_now, mailbox, context
                    )
                    if result != 0:
                        raise RuntimeError(
                            f"evaluator runner failed with exit {result}"
                        )
                    verdict, scope = _first_verdict(verdict_path)
                    if verdict is None:
                        _update_state(state_path, {"status": "error"})
                        _append_log(
                            mailbox,
                            f"- iter {iteration_now} | loop | unparseable "
                            "integration verdict",
                        )
                        return finish(3)
                    _write_state_verdict(state_path, verdict)
                    code = _apply_verdict(
                        mailbox,
                        state_path,
                        repair_path,
                        iteration_now,
                        verdict,
                        scope,
                    )
                    if code is not None:
                        return finish(code)
                    # ITERATE: wake the Lead for another pass, forcing at
                    # least one even if the queue currently looks "done"
                    # (the integration verdict is the authority here).
                    wake_event.clear()
                    lead_thread, lead_result = spawn_lead(force_first_pass=True)
                    write_sidecar("lead", current_iteration(), True, True)
                    continue
                # Fresh open work appeared after the Lead thread stopped
                # (e.g. a slice-eval fault) -- re-invoke it.
                wake_event.clear()
                lead_thread, lead_result = spawn_lead()
                write_sidecar("lead", current_iteration(), True, True)
                continue

            wake_event.wait(timeout=poll_seconds if poll_seconds > 0 else 0.01)
            wake_event.clear()
    finally:
        stop_event.set()
        if lead_thread is not None:
            lead_thread.join(timeout=5)
        shutil.rmtree(lock, ignore_errors=True)


class _PortableRunner:
    def run(
        self,
        role: str,
        iteration: int,
        mailbox: Path,
        context: dict | None = None,
    ) -> int:
        script = (
            Path(__file__).resolve().parent.parent
            / "portable"
            / "driver.sh"
        )
        environment = os.environ.copy()
        environment["LOOP_DIR"] = str(Path(mailbox).resolve())
        # api:OpenLoopPromptEnv: only set the TRIO_* vars for a non-empty
        # open-loop context, so a lockstep (context={}/None) call renders a
        # byte-identical prompt to HEAD.
        if context and context.get("mode") == "open-loop":
            environment["TRIO_MODE"] = "open-loop"
            environment["TRIO_KIND"] = context.get("kind") or ""
            environment["TRIO_SLICE"] = context.get("slice") or ""
            environment["TRIO_SHA"] = context.get("sha") or ""
        # The shell shim only runs one role. Gates, verdicts, repairs, and
        # resume stay in run_loop so this runner never parses VERDICT.md.
        result = subprocess.run(
            [str(script), "--run-role", role],
            check=False,
            env=environment,
        )
        return result.returncode


def _load_omnigent_runner():
    """Load OmnigentRunner without importing the ``omnigent`` package."""
    path = Path(__file__).resolve().parent.parent / "omnigent" / "trioctl"
    if not path.is_file():
        raise ImportError(f"OmnigentRunner source is missing: {path}")
    loader = importlib.machinery.SourceFileLoader("trioctl", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load OmnigentRunner from {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    runner = getattr(module, "OmnigentRunner", None)
    if not callable(runner):
        raise ImportError(f"{path} does not expose OmnigentRunner")
    return runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the Trio state machine")
    run.add_argument("--mailbox", type=Path, required=True)
    run.add_argument("--max-iterations", type=int, required=True)
    run.add_argument(
        "--runner",
        choices=("portable", "omnigent"),
        default="portable",
    )
    run.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="open-loop Evaluator poll interval in seconds (default: 30)",
    )
    mode_group = run.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--open-loop",
        action="store_true",
        help="force open-loop mode; errors if the mailbox has no QUEUE.md",
    )
    mode_group.add_argument(
        "--lockstep",
        action="store_true",
        help="force lockstep mode even when the mailbox has QUEUE.md",
    )
    args = parser.parse_args(argv)
    repo = Path.cwd()
    runner = (
        _PortableRunner()
        if args.runner == "portable"
        else _load_omnigent_runner()(repo=repo)
    )
    mode = "open-loop" if args.open_loop else "lockstep" if args.lockstep else "auto"
    return run_loop(
        args.mailbox,
        args.max_iterations,
        runner,
        repo=repo,
        poll_seconds=args.poll_seconds,
        mode=mode,
    )

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""trio-native step helper: pull-mode ops over the stdlib Trio loop core.

The saved Workflow ``native/trio-native.js`` sequences a lockstep Trio loop;
it has no filesystem or process API, so every deterministic decision is one
call of this helper, run by a Bash-only ``trio-step`` agent:

    trio_native_step.py <op> --mailbox <abs> --token <run> --nonce <n> [...]

Ops (v0, lockstep): ``begin``, ``next``, ``dispatch``, ``builders``,
``cleanup``, ``gate``, ``pin``, ``apply``, ``end``. Each prints exactly one
JSON object on stdout and exits 0 (usage errors exit 2). Every result echoes
``--nonce`` so the script can detect a step agent that answered for the
wrong command.

Builder waves are driver-owned (workflow subagents have no Agent tool):
``dispatch`` returns the Lead's HEAD before a wave; ``builders`` verifies
each isolated builder's branch (forked from that HEAD, no ``loop/``
commits) and writes the builders' LOG lines; ``cleanup`` removes merged
builder worktrees (``--force`` only for ``loop/`` residue) and branches.
Beyond ``trio_loop``'s gate, a Lead pass must rewrite REPORT.md; a SHIP
folds the driver's final STATE into the retirement commit; ``end`` removes
Evaluator pin worktrees under ``.claude/worktrees/eval-*``.

This file holds no loop semantics. Gates, verdict parsing, the repair
counter, the evaluator pin/attempt and SHIP retirement are the functions of
``metrics/trio_loop.py`` (and through it ``trio-metrics.py``/``trio-shadow.py``)
called in the order ``trio_loop._run_lockstep`` calls them, with the same
STATE.md ``phase`` resume cursor (``<role>-running`` -> ``lead-done`` ->
verdict), so a mailbox driven here can be resumed by ``trio_loop.py run``
and vice versa.

Idempotency: ``gate`` and ``apply`` record their decision in the
driver-internal ``<mailbox>/.native.json``; a re-run of the same
(iteration, role/attempt) returns the recorded answer and never re-applies
(no second LOG line, no second ``.repairs`` bump). ``begin``/``next``/``pin``
are idempotent through STATE.md itself (``next`` resumes a ``*-running``
phase without bumping; ``pin`` reuses the persisted attempt and sha).

Lock: the helper is a new process per op, so the mailbox ``.lock`` (same
on-disk protocol as ``trio_loop._acquire_lock``) is owned by the token
``workflow:<run token>``. Its ``pid`` is the *holder pid* (``_holder_pid``):
the long-lived Claude Code process running the workflow, i.e. the nearest
ancestor whose ``/proc/<pid>/comm`` is ``claude`` (``TRIO_NATIVE_HOLDER_PID``
overrides it; with no ``claude`` ancestor it falls back to the parent pid).
Every op that needs the lock re-stamps ``pid`` and ``heartbeat``:

* same token, recorded pid dead (a journal resume in a new Claude process,
  or a fresh run after a crash) -> the op takes the lock over and records
  its own holder pid, so ``trio_loop``/``trioctl`` (pid-only check) keep
  seeing a live owner;
* same token, recorded pid alive and not ours (a second concurrent launch
  with the default token) -> refused, the lock is left untouched;
* different token or driver -> refused while the pid is alive, except that
  a different *workflow* token also takes over when the heartbeat is older
  than ``TRIO_NATIVE_LOCK_STALE_SECONDS`` (default 4 h). The heartbeat is
  refreshed only by ops, so a single role pass longer than that can be
  taken over; the original run then fails closed at its next op.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NATIVE_STEP_API = 1
HERE = Path(__file__).resolve().parent
METRICS_DIR = Path(
    os.environ.get("TRIO_NATIVE_METRICS_DIR") or HERE.parent / "metrics"
)
RECORDS = ".native.json"
SESSION = ".session.json"
EXCLUDE_LINE = ".claude/worktrees/"
#: Build/test artefacts that are never product (live probe 2 blocker A):
#: added to ``info/exclude`` (the common git dir, so every builder and
#: Evaluator worktree too) so they neither block ``cleanup`` nor trip the
#: SHIP retirement check (``git ls-files -o --exclude-standard``).
#: ``node_modules/`` itself is deliberately not listed.
ARTEFACT_EXCLUDES = (
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".mypy_cache/",
    ".ruff_cache/", "node_modules/.cache/", "node_modules/.vite/",
)
EXCLUDE_HEADER = "# trio-native: loop worktrees and build artefacts"
_ARTEFACT_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache",
                            ".ruff_cache"})
DEFAULT_STALE_SECONDS = 4 * 3600.0
DRIVER = "claude-workflow"
#: Driver/runtime files a run creates inside the mailbox; never product and
#: never committed. trioctl's MAILBOX_RUNTIME_IGNORES (r14 M-1) plus this
#: helper's own ``.native.json`` (eval-native-v0 F8).
MAILBOX_RUNTIME_IGNORES = (
    ".dispatch/", ".driver.json", ".driver.pid", ".session.json",
    ".sessions/", "driver.log", ".lock", ".repairs", RECORDS,
    ".native-launch.json", ".native-runs/",
)
_MAILBOX_RUNTIME_DIRS = frozenset({".dispatch", ".sessions", ".lock",
                                   ".native-runs"})
OPS = ("begin", "next", "dispatch", "builders", "cleanup", "gate", "pin",
       "apply", "end")
WORKTREES_DIR = ".claude/worktrees"
EVAL_WORKTREE_PREFIX = "eval-"
BASE_REF_HINT = (
    "launch Claude Code with --settings '{\"worktree\":{\"baseRef\":\"head\"}}' "
    "so isolated builders fork from the Lead's HEAD"
)
RECORD_KINDS = ("gate", "apply", "report", "builders")


def _load_loop():
    path = METRICS_DIR / "trio_loop.py"
    spec = importlib.util.spec_from_file_location("trio_loop", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio_loop.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TL = _load_loop()


class StepError(Exception):
    """An op refused: reported as ``ok: false`` with ``error``."""


# ---------------------------------------------------------------- helpers
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _owner(token: str) -> str:
    return f"workflow:{token}"


def _repo_for(mailbox: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).resolve()
    result = subprocess.run(
        ["git", "-C", str(mailbox), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _holder_pid() -> int:
    """The long-lived process that owns the lock for this workflow run."""
    env = os.environ.get("TRIO_NATIVE_HOLDER_PID", "").strip()
    if env.isdigit():
        return int(env)
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            break
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        if comm == "claude":
            return pid
        # ppid is field 4; comm (field 2) may contain spaces/parens.
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return os.getppid()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _records(mailbox: Path) -> dict:
    data = _read_json(mailbox / RECORDS)
    for key in RECORD_KINDS:
        data.setdefault(key, {})
    return data


def _record(mailbox: Path, kind: str, key: str, result: dict) -> None:
    data = _records(mailbox)
    stored = {k: v for k, v in result.items() if k != "nonce"}
    data[kind][key] = stored
    _write_json(mailbox / RECORDS, data)


def _recorded(mailbox: Path, kind: str, key: str) -> dict | None:
    value = _records(mailbox)[kind].get(key)
    return dict(value) if isinstance(value, dict) else None


def _report_digest(mailbox: Path) -> str:
    """sha256 of REPORT.md ("" when missing): the gate's rewrite check."""
    try:
        return hashlib.sha256((mailbox / "REPORT.md").read_bytes()).hexdigest()
    except OSError:
        return ""


def _purge_stale_records(mailbox: Path, iteration: int) -> None:
    """A mailbox re-initialized to an earlier iteration drops old records."""
    data = _records(mailbox)
    changed = False
    for kind in RECORD_KINDS:
        for key in list(data[kind]):
            if TL._number(key.split(":", 1)[0]) > iteration:
                del data[kind][key]
                changed = True
    if changed:
        _write_json(mailbox / RECORDS, data)


def _state(mailbox: Path) -> dict[str, str]:
    return TL._read_state(mailbox / "STATE.md")


def _snapshot(state: dict[str, str]) -> dict:
    return {
        "iteration": TL._number(state["iteration"]),
        "status": state["status"].strip(),
        "phase": state["phase"].strip(),
    }


def _write_session(mailbox: Path, token: str, phase: str, done: bool) -> None:
    path = mailbox / SESSION
    old = _read_json(path)
    same = old.get("driver") == DRIVER and old.get("session") == token
    payload = {
        "driver": DRIVER,
        "session": token,
        "pid": _holder_pid(),
        "started_at": old.get("started_at") if same else _now_iso(),
        "phase": "done" if done else phase,
        "done": done,
    }
    _write_json(path, payload)


# ------------------------------------------------------------------- lock
def _stale_seconds() -> float:
    try:
        return float(os.environ.get("TRIO_NATIVE_LOCK_STALE_SECONDS", ""))
    except ValueError:
        return DEFAULT_STALE_SECONDS


def _lock_owner(lock: Path) -> str:
    try:
        return (lock / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _heartbeat_age(lock: Path) -> float | None:
    try:
        stamp = float((lock / "heartbeat").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return max(0.0, time.time() - stamp)


def _stamp_lock(lock: Path, token: str) -> None:
    TL._write_lock_file(lock, "owner", f"{_owner(token)}\n")
    TL._write_lock_file(lock, "pid", f"{_holder_pid()}\n")
    TL._write_lock_file(lock, "heartbeat", f"{time.time():.3f}\n")


def _foreign_live_pid(lock: Path) -> int:
    """The recorded pid when it is alive and is not this run's holder pid."""
    pid = TL._lock_pid(lock)
    if pid > 0 and pid != _holder_pid() and TL._pid_alive(pid):
        return pid
    return 0


def _acquire(mailbox: Path, token: str) -> None:
    """Take (or re-enter) the mailbox lock for this workflow token."""
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        try:
            lock.mkdir()
        except FileExistsError:
            owner = _lock_owner(lock)
            if owner == _owner(token):
                live = _foreign_live_pid(lock)
                if live:
                    raise StepError(
                        f"mailbox is locked by {owner} under another live "
                        f"process (pid {live}): a second launch with the "
                        f"same run_token is refused"
                    )
            else:
                pid = TL._lock_pid(lock)
                alive = pid > 0 and TL._pid_alive(pid)
                age = _heartbeat_age(lock)
                if owner.startswith("workflow:") and age is not None:
                    stale = not alive or age > _stale_seconds()
                elif pid <= 0:
                    try:
                        mtime_age = time.time() - lock.stat().st_mtime
                    except FileNotFoundError:
                        mtime_age = 0.0
                    stale = mtime_age >= TL.LOCK_EMPTY_GRACE_SECONDS
                else:
                    stale = not alive
                if not stale:
                    raise StepError(
                        f"mailbox is locked by {owner or 'another driver'} "
                        f"(pid {pid or 'unknown'})"
                    )
                TL._discard_lock_dir(mailbox, lock)
                try:
                    lock.mkdir()
                except FileExistsError as exc:
                    raise StepError("lost the stale-lock takeover race") from exc
        _stamp_lock(lock, token)


def _require_lock(mailbox: Path, token: str) -> None:
    """Every op after ``begin`` must still hold the lock; re-stamp it.

    The pid is re-stamped as well as the heartbeat: a journal resume in a
    new Claude process replays ``begin`` from its cache, so the first live
    op is where the new holder pid gets recorded (eval-native-v0 F2).
    """
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        owner = _lock_owner(lock)
        if owner != _owner(token):
            raise StepError(
                f"mailbox lock not held by {_owner(token)} "
                f"(owner: {owner or 'none'}); run begin first"
            )
        live = _foreign_live_pid(lock)
        if live:
            raise StepError(
                f"mailbox lock {owner} belongs to another live process "
                f"(pid {live}); this run does not hold it"
            )
        TL._write_lock_file(lock, "pid", f"{_holder_pid()}\n")
        TL._write_lock_file(lock, "heartbeat", f"{time.time():.3f}\n")


def _release(mailbox: Path, token: str) -> str:
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        if not lock.is_dir():
            return "released"
        owner = _lock_owner(lock)
        if owner != _owner(token) or _foreign_live_pid(lock):
            return "foreign"
        TL._discard_lock_dir(mailbox, lock)
    return "released"


# ------------------------------------------------------ mailbox ignore
def _ensure_mailbox_gitignore(mailbox: Path) -> list[str]:
    """Append the missing runtime lines to ``<mailbox>/.gitignore``.

    Same rule as trioctl's ``_ensure_mailbox_gitignore``: idempotent and
    append-only, an entry counts as present when an existing line names it
    (ignoring a leading ``/``, and a trailing ``/`` for directory entries),
    and a user negation (``!name``) also counts. Returns the lines added.
    """
    path = mailbox / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    present: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip("!").lstrip("/")
        name = line.rstrip("/")
        if line.endswith("/") and name not in _MAILBOX_RUNTIME_DIRS:
            continue  # a dir-only pattern does not cover a runtime file
        present.add(name)
    missing = [e for e in MAILBOX_RUNTIME_IGNORES
               if e.rstrip("/") not in present]
    if not missing:
        return []
    prefix = "" if not text or text.endswith("\n") else "\n"
    header = "" if text else "# Trio loop runtime files (added by trio-native)\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(prefix + header + "".join(f"{e}\n" for e in missing))
    return missing


# ---------------------------------------------------------- git exclude
def _ensure_exclude(repo: Path | None) -> str | None:
    """Add `.claude/worktrees/` and the build artefacts to info/exclude.

    Idempotent and append-only: each line is added once. ``info/`` lives in
    the common git dir, so the lines cover every linked worktree as well.
    """
    if repo is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute",
         "--git-path", "info/exclude"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    present = {line.strip() for line in text.splitlines()}
    missing = [e for e in (EXCLUDE_LINE, *ARTEFACT_EXCLUDES)
               if e not in present]
    if missing:
        sep = "" if not text or text.endswith("\n") else "\n"
        header = "" if EXCLUDE_HEADER in present else EXCLUDE_HEADER + "\n"
        path.write_text(text + sep + header
                        + "".join(f"{e}\n" for e in missing),
                        encoding="utf-8")
    return str(path)


def _is_artefact(path: str) -> bool:
    """True for a build/test artefact path (see ``ARTEFACT_EXCLUDES``)."""
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return False
    if any(p in _ARTEFACT_DIRS for p in parts):
        return True
    if re.search(r"\.py[cod]$", parts[-1]):
        return True
    return any(parts[i] == "node_modules" and parts[i + 1] in (".cache", ".vite")
               for i in range(len(parts) - 1))


# ------------------------------------------------------------------- ops
def op_begin(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    if not (mailbox / "GOAL.md").is_file():
        raise StepError(f"{mailbox}/GOAL.md missing: run /trio-init first")
    if (mailbox / "QUEUE.md").is_file():
        raise StepError(
            "QUEUE.md present: open-loop mailboxes are not supported by "
            "trio-native v0 (use trio_loop.py run or remove QUEUE.md)"
        )
    _acquire(mailbox, a.token)
    _ensure_mailbox_gitignore(mailbox)
    if not (mailbox / "LOG.md").is_file():
        (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    state = _state(mailbox)
    snap = _snapshot(state)
    _purge_stale_records(mailbox, snap["iteration"])
    exclude = _ensure_exclude(repo)
    _write_session(mailbox, a.token, snap["phase"], done=False)
    return {
        "mode": "lockstep",
        "repo": str(repo) if repo else None,
        "lock_owner": _owner(a.token),
        "exclude_path": exclude,
        **snap,
    }


def op_next(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """What the driver runs next: lead | repair | evaluate | stop.

    Mirrors the head of ``trio_loop._run_lockstep``'s loop body.
    """
    _require_lock(mailbox, a.token)
    state_path = mailbox / "STATE.md"
    state = _state(mailbox)
    status = state["status"].strip().lower()
    status_word = status.split()[0] if status else ""
    iteration = TL._number(state["iteration"])
    terminal = TL.TERMINAL_CODES.get(status_word)
    verdict, scope = TL._first_verdict(mailbox / "VERDICT.md")
    if terminal is not None:
        code = terminal
        fold = None
        if status_word == "needs_retirement":
            code = TL._finalize_ship(
                mailbox, state_path, iteration, repo, context="resume"
            )
            if code == 0:
                fold = _fold_final_state(mailbox, repo, iteration)
        after = _snapshot(_state(mailbox))
        # Probe 2 finding D: a run that finishes from needs_retirement (or
        # re-reads a finished mailbox) reports the verdict's product
        # commits and the fold, as `apply` does.
        text = _verdict_text(mailbox)
        return {"action": "stop", "code": code, "verdict": verdict,
                "commit_shas": TL._verdict_commit_shas(text),
                "retirement_fold": fold,
                "human_check": _section(text, "Human check"), **after}
    phase = state["phase"].strip().lower()
    if phase == "lead-done":
        return {"action": "evaluate", "iteration": iteration, "attempt": 1,
                "scope": None}
    if phase in ("lead-running", "repair-running"):
        role = phase.split("-", 1)[0]
        fails = _gate_failures(mailbox, iteration, role)
        return {"action": role, "iteration": iteration, "attempt": fails + 1,
                "scope": scope if role == "repair" else None}
    if iteration >= a.max_iterations:
        return {"action": "stop", "code": 4, "verdict": verdict,
                "iteration": iteration, "status": "max_iterations",
                "phase": state["phase"].strip()}
    role = "repair" if TL._counter(mailbox / ".repairs") else "lead"
    iteration += 1
    TL._update_state(state_path, {
        "iteration": str(iteration),
        "status": "running",
        "phase": f"{role}-running",
        "evaluator_attempt": "",
        "evaluated_sha": "",
        "evaluated_repos": "",
    })
    if role == "lead":
        data = _records(mailbox)
        data["report"][str(iteration)] = _report_digest(mailbox)
        _write_json(mailbox / RECORDS, data)
    _write_session(mailbox, a.token, f"{role}-running", done=False)
    return {"action": role, "iteration": iteration, "attempt": 1,
            "scope": scope if role == "repair" else None}


def _verdict_text(mailbox: Path) -> str:
    try:
        return (mailbox / "VERDICT.md").read_text(encoding="utf-8",
                                                  errors="replace")
    except OSError:
        return ""


def _gate_failures(mailbox: Path, iteration: int, role: str) -> int:
    gates = _records(mailbox)["gate"]
    return sum(
        1 for key, rec in gates.items()
        if key.startswith(f"{iteration}:{role}:") and not rec.get("pass")
    )


def _shadow_detail(mailbox: Path, repo: Path | None) -> list[str]:
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    result = subprocess.run(
        [sys.executable, str(METRICS_DIR / "trio-shadow.py"),
         "--mailbox", str(target), "--require-commits"],
        capture_output=True, text=True,
    )
    lines = [ln for ln in result.stdout.splitlines()
             if ln.startswith("commit gate:")]
    lines += [ln for ln in result.stderr.splitlines() if ln.strip()]
    return lines[:20]


def _advisory_check(mailbox: Path) -> dict:
    """trio-check on the mailbox: reported, never gating (as in _run_role)."""
    result = subprocess.run(
        [sys.executable, str(METRICS_DIR / "trio-check.py"), str(mailbox),
         "--json", "--no-prompt-sync"],
        capture_output=True, text=True,
    )
    try:
        summary = json.loads(result.stdout).get("summary", {})
    except ValueError:
        summary = {}
    return {"rc": result.returncode, "ok": summary.get("ok"),
            "v1_violations": summary.get("v1_violations")}


def _report_gate(mailbox: Path, iteration: int) -> tuple[bool, str]:
    """A Lead pass must rewrite REPORT.md (probe blocker 6).

    The digest is recorded by ``next`` when it starts the pass; a pass
    started by another driver has no record and is not checked.
    """
    before = _records(mailbox)["report"].get(str(iteration))
    if before is None:
        return True, "REPORT gate skipped (no digest recorded)"
    if _report_digest(mailbox) in ("", before):
        return False, (
            f"REPORT.md was not rewritten in iteration {iteration} "
            "(write it with a Bash heredoc)"
        )
    return True, "REPORT gate passed"


def op_gate(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """The post-role gate of ``trio_loop._run_role`` for one attempt."""
    _require_lock(mailbox, a.token)
    role, iteration, attempt = a.role, a.iteration, a.attempt
    key = f"{iteration}:{role}:{attempt}"
    recorded = _recorded(mailbox, "gate", key)
    if recorded is not None:
        return recorded
    state = _state(mailbox)
    snap = _snapshot(state)
    if snap["iteration"] != iteration or snap["phase"] != f"{role}-running":
        raise StepError(
            f"gate {key}: STATE is iteration {snap['iteration']} phase "
            f"{snap['phase']}, not {role}-running"
        )
    checks = [
        TL._commit_gate(mailbox, repo),
        TL._log_gate(mailbox, iteration, role),
    ]
    if role == "lead":
        checks.append(_report_gate(mailbox, iteration))
    failures = [note for ok, note in checks if not ok]
    result: dict = {
        "role": role, "iteration": iteration, "attempt": attempt,
        "pass": not failures, "failures": failures,
        "check": _advisory_check(mailbox),
    }
    if not failures:
        TL._update_state(mailbox / "STATE.md",
                         {"status": "running", "phase": "lead-done"})
        result.update(final=False, status="running", phase="lead-done")
    else:
        result["detail"] = _shadow_detail(mailbox, repo)
        if attempt >= 2:
            TL._update_state(mailbox / "STATE.md",
                             {"status": "error", "phase": "error"})
            TL._append_log(
                mailbox,
                f"- iter {iteration} | loop | gate breach after {role}: "
                + "; ".join(failures),
            )
            result.update(final=True, status="error", phase="error")
        else:
            result.update(final=False, status="running",
                          phase=f"{role}-running")
    _record(mailbox, "gate", key, result)
    return result


def _context_block(mailbox: Path, ctx: dict) -> str:
    attempt = str(ctx.get("evaluator_attempt") or "")
    sha = str(ctx.get("pinned_sha") or ctx.get("expected_sha") or "")
    pins = ctx.get("pins") or {}
    extra = (" pins=" + ",".join(f"{n}@{v}" for n, v in pins.items())
             if pins else "")
    return (
        f"LOCKSTEP CONTEXT: attempt={attempt} sha={sha}{extra}\n\n"
        f"MAILBOX OVERRIDE: this run uses `{mailbox}/` as the loop mailbox "
        f"— every `loop/` path in the instructions below resolves to "
        f"`{mailbox}/`.\n"
    )


def op_pin(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """Persist the evaluator pin + attempt (``_lockstep_eval_context``)."""
    _require_lock(mailbox, a.token)
    state = _state(mailbox)
    snap = _snapshot(state)
    if snap["iteration"] != a.iteration or snap["phase"].lower() != "lead-done":
        raise StepError(
            f"pin: STATE is iteration {snap['iteration']} phase "
            f"{snap['phase']}, not lead-done of iteration {a.iteration}"
        )
    ctx = TL._lockstep_eval_context(
        mailbox, repo, a.iteration, state, mailbox / "STATE.md"
    )
    _write_session(mailbox, a.token, "evaluator-running", done=False)
    return {
        "iteration": a.iteration,
        "evaluator_attempt": ctx["evaluator_attempt"],
        "sha": ctx["pinned_sha"],
        "pins": ctx.get("pins") or {},
        # True only when VERDICT.md already is this attempt's artifact
        # (the resume rule of _run_lockstep: do not re-dispatch).
        "skip_evaluator": TL._fresh_evaluator_artifact(
            mailbox, a.iteration, ctx
        ),
        "context_block": _context_block(mailbox, ctx),
    }


def _section(text: str, heading: str, limit: int = 1500) -> str | None:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
        text, re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip()[:limit] if match else None


def op_apply(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """Parse VERDICT.md for this pin and apply it (``_apply_verdict``)."""
    _require_lock(mailbox, a.token)
    key = f"{a.iteration}:{a.attempt}"
    recorded = _recorded(mailbox, "apply", key)
    if recorded is not None:
        return recorded
    state_path = mailbox / "STATE.md"
    state = _state(mailbox)
    snap = _snapshot(state)
    if snap["iteration"] != a.iteration or snap["phase"].lower() != "lead-done":
        raise StepError(
            f"apply: STATE is iteration {snap['iteration']} phase "
            f"{snap['phase']}, not lead-done of iteration {a.iteration}"
        )
    if state["evaluator_attempt"].strip() != a.attempt:
        raise StepError(
            f"apply: STATE evaluator_attempt "
            f"{state['evaluator_attempt'] or '(none)'} is not {a.attempt}"
        )
    verdict_path = mailbox / "VERDICT.md"
    verdict, scope = TL._first_verdict(verdict_path)
    try:
        text = verdict_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    bound = TL._fresh_evaluator_artifact(
        mailbox, a.iteration,
        {"evaluator_attempt": a.attempt,
         "pinned_sha": state["evaluated_sha"].strip()},
    )
    if verdict is not None and not bound:
        # eval-native-v0 F7: a leftover VERDICT.md (an earlier iteration's,
        # or one the Evaluator did not bind to this pin) is never applied.
        # Nothing is written: STATE stays lead-done, so a fresh run's `pin`
        # re-dispatches the Evaluator. trio_loop still applies it (parity
        # gap recorded as a follow-up; changing the core is out of scope).
        raise StepError(
            f"apply: VERDICT.md ({verdict}) is not bound to iteration "
            f"{a.iteration} attempt {a.attempt} sha "
            f"{state['evaluated_sha'].strip() or '(none)'}: stale or "
            f"unbound verdict not applied; STATE left at lead-done"
        )
    if verdict is None:
        TL._update_state(state_path, {"status": "error", "phase": "error"})
        TL._append_log(
            mailbox, f"- iter {a.iteration} | loop | unparseable verdict"
        )
        code = 3
    else:
        code = TL._apply_verdict(
            mailbox, state_path, mailbox / ".repairs", a.iteration,
            verdict, scope, repo=repo,
        )
    fold = (_fold_final_state(mailbox, repo, a.iteration)
            if verdict == "SHIP" and code == 0 else None)
    after = _snapshot(_state(mailbox))
    result = {
        "retirement_fold": fold,
        "verdict": verdict,
        "scope": scope,
        "bound": bound,
        "code": code,
        "stop": code is not None,
        "next_role": (
            None if code is not None
            else "repair" if TL._counter(mailbox / ".repairs") else "lead"
        ),
        "commit_shas": TL._verdict_commit_shas(text),
        "human_check": _section(text, "Human check"),
        **after,
    }
    _record(mailbox, "apply", key, result)
    return result


# ------------------------------------------------------ builder waves
def _lead_running(mailbox: Path, iteration: int, op: str) -> None:
    snap = _snapshot(_state(mailbox))
    if snap["iteration"] != iteration or snap["phase"] != "lead-running":
        raise StepError(
            f"{op}: STATE is iteration {snap['iteration']} phase "
            f"{snap['phase']}, not lead-running of iteration {iteration}"
        )


def _need_repo(repo: Path | None, op: str) -> Path:
    if repo is None:
        raise StepError(f"{op}: no git repository for this mailbox")
    return repo


def op_dispatch(mailbox: Path, repo: Path | None,
                a: argparse.Namespace) -> dict:
    """The Lead's HEAD before a builder wave: every builder must fork here."""
    _require_lock(mailbox, a.token)
    _lead_running(mailbox, a.iteration, "dispatch")
    root = _need_repo(repo, "dispatch")
    head = TL._git_head(root)
    if head is None:
        raise StepError("dispatch: repository has no HEAD commit")
    return {"iteration": a.iteration, "wave": a.wave, "head": head}


def _worktrees(repo: Path) -> list[dict]:
    """``git worktree list --porcelain`` as [{path, head, branch}]."""
    out = TL._git(repo, "worktree", "list", "--porcelain").stdout
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch refs/heads/"):
            cur["branch"] = line[len("branch refs/heads/"):]
    return items


def _branch_sha(repo: Path, branch: str) -> str | None:
    result = TL._git(repo, "rev-parse", "--verify", "-q",
                     f"refs/heads/{branch}^{{commit}}")
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _check_builder(repo: Path, mailbox_rel: str | None, head: str,
                   res: dict) -> str | None:
    """Why this builder result is refused, or None."""
    sid = str(res.get("id") or "?")
    base = str(res.get("base") or "").strip()
    if not base or not TL._sha_matches(base, head):
        return (f"builder {sid} forked from {base or '(unknown)'}, not the "
                f"Lead's HEAD {head[:12]}: {BASE_REF_HINT}")
    commits = res.get("commits") or []
    if not commits:
        return None  # nothing to merge
    branch = str(res.get("branch") or "").strip()
    tip = _branch_sha(repo, branch) if branch else None
    if tip is None:
        return f"builder {sid}: branch {branch or '(none)'} does not exist"
    reported = str(res.get("head") or "").strip()
    if reported and not TL._sha_matches(reported, tip):
        return (f"builder {sid}: reported head {reported[:12]} but branch "
                f"{branch} is at {tip[:12]}")
    if not TL._git_is_ancestor(repo, head, tip):
        return (f"builder {sid}: branch {branch} does not contain the "
                f"Lead's HEAD {head[:12]}: {BASE_REF_HINT}")
    listed = TL._git(repo, "rev-list", f"{head}..{tip}").stdout.split()
    for sha in listed:
        loop_paths = [p for p in TL._commit_paths(repo, sha)
                      if TL._path_in_mailbox(p, mailbox_rel)]
        if loop_paths:
            return (f"builder {sid}: commit {sha[:12]} commits mailbox "
                    f"files ({', '.join(loop_paths[:3])}); builders never "
                    "commit loop/")
    return None


def _one_line(text: object, limit: int = 160) -> str:
    return " ".join(str(text or "").split())[:limit]


def op_builders(mailbox: Path, repo: Path | None,
                a: argparse.Namespace) -> dict:
    """Verify one wave's builder results and write their LOG lines.

    Builders never write LOG.md (their worktree copy is lost): the driver
    appends ``- iter N | builder | <id>: <summary>`` for each accepted one.
    Idempotent per (iteration, wave, branches).
    """
    _require_lock(mailbox, a.token)
    try:
        results = json.loads(a.results or "[]")
    except ValueError as exc:
        raise StepError(f"builders: --results is not JSON: {exc}") from exc
    if not isinstance(results, list) or not all(
            isinstance(r, dict) for r in results):
        raise StepError("builders: --results must be a JSON list of objects")
    branches = ",".join(sorted(str(r.get("branch") or r.get("id") or "")
                               for r in results))
    key = f"{a.iteration}:{a.wave}:{branches}"
    recorded = _recorded(mailbox, "builders", key)
    if recorded is not None:
        return recorded
    _lead_running(mailbox, a.iteration, "builders")
    root = _need_repo(repo, "builders")
    head = str(a.head or "").strip()
    if not head:
        raise StepError("builders: --head (the dispatch HEAD) is required")
    mailbox_rel = TL._mailbox_rel(root, mailbox)
    accepted, refused, merge = [], [], []
    for res in results:
        sid = _one_line(res.get("id"), 64) or "?"
        reason = _check_builder(root, mailbox_rel, head, res)
        if reason:
            refused.append({"id": sid, "reason": reason})
            continue
        accepted.append(sid)
        if res.get("commits"):
            merge.append({"id": sid, "branch": str(res["branch"]).strip()})
    # The record is written after the LOG lines; a crash in between is
    # replayed without a record, so a line already in LOG.md is not
    # appended again (eval-native-v0b N9).
    try:
        logged = set((mailbox / "LOG.md").read_text(
            encoding="utf-8").splitlines())
    except OSError:
        logged = set()
    for res in results:
        sid = _one_line(res.get("id"), 64) or "?"
        if sid in accepted:
            tip = str(res.get("head") or "")[:7]
            where = (f" ({res.get('branch')}@{tip})" if res.get("commits")
                     else " (no commits)")
            line = (f"- iter {a.iteration} | builder | {sid}: "
                    f"{_one_line(res.get('summary'))}{where}")
            if line not in logged:
                TL._append_log(mailbox, line)
    result = {"iteration": a.iteration, "wave": a.wave, "head": head,
              "accepted": accepted, "refused": refused, "merge": merge}
    _record(mailbox, "builders", key, result)
    return result


def _dirty_entries(worktree: str) -> list[tuple[str, str]] | None:
    """``git status --porcelain`` as [(XY code, path)] (ignored files are
    not listed: they never block ``git worktree remove``)."""
    result = subprocess.run(
        ["git", "-C", worktree, "status", "--porcelain",
         "--untracked-files=all"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    entries = []
    for line in result.stdout.splitlines():
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entries.append((line[:2], entry.strip().strip('"')))
    return entries


def _dirty_paths(worktree: str) -> list[str] | None:
    entries = _dirty_entries(worktree)
    return None if entries is None else [p for _c, p in entries]


def _remove_worktree(repo: Path, path: str, mailbox_rel: str | None,
                     force_any: bool = False) -> str | None:
    """Remove a worktree; None on success, else why it was kept.

    ``--force`` only when every uncommitted path is mailbox residue or an
    *untracked* build artefact (``_is_artefact``: never a tracked change,
    never another untracked file), or, for an Evaluator pin worktree,
    always (it never holds product edits). Ignored/excluded files are not
    dirt: ``git worktree remove`` does not refuse on them.
    """
    dirty = _dirty_entries(path)
    if dirty is None:
        return "git status failed in the worktree"
    outside = [p for code, p in dirty
               if not TL._path_in_mailbox(p, mailbox_rel)
               and not (code == "??" and _is_artefact(p))]
    if outside and not force_any:
        return ("uncommitted changes outside the mailbox: "
                + ", ".join(outside[:5]))
    cmd = ["worktree", "remove"] + (["--force"] if dirty else []) + [path]
    result = TL._git(repo, *cmd)
    if result.returncode != 0:
        return f"git worktree remove failed: {result.stderr.strip()[:200]}"
    return None


def _drop_superseded(root: Path, mailbox_rel: str | None, head: str | None,
                     spec: str, trees: dict) -> dict:
    """Drop one unmerged builder branch superseded by a re-dispatch.

    ``spec`` is ``old`` or ``old=new``; with ``=new`` the old branch is
    dropped only when ``new`` is merged into HEAD (the re-dispatch landed).
    Only builder branches are dropped: a worktree under
    ``.claude/worktrees/`` (removed under ``_remove_worktree``'s dirt rule),
    or no worktree and a ``worktree-*`` name. The branch is force-deleted
    (``git branch -D``): its commits are superseded, not merged.
    """
    old, _sep, new = (x.strip() for x in spec.partition("="))
    entry: dict = {"branch": old, "superseded_by": new or None}
    tip = _branch_sha(root, old)
    if tip is None:
        return {**entry, "dropped": True, "note": "branch already gone"}
    if new:
        new_tip = _branch_sha(root, new)
        if (new_tip is None or head is None
                or not TL._git_is_ancestor(root, new_tip, head)):
            return {**entry, "dropped": False,
                    "reason": f"re-dispatched branch {new} is not merged "
                              "into HEAD"}
    tree = trees.get(old)
    path = tree.get("path") if tree else None
    marker = f"{root}/{WORKTREES_DIR}/"
    if path:
        if Path(path).resolve() == root.resolve():
            return {**entry, "dropped": False,
                    "reason": "checked out in the repo"}
        if not path.startswith(marker):
            return {**entry, "dropped": False,
                    "reason": f"worktree {path} is not a builder worktree"}
        why = _remove_worktree(root, path, mailbox_rel)
        if why:
            return {**entry, "dropped": False, "worktree": path,
                    "reason": why}
    elif not old.startswith("worktree-"):
        return {**entry, "dropped": False,
                "reason": "not a builder branch (no worktree-* name)"}
    deleted = TL._git(root, "branch", "-D", old)
    if deleted.returncode != 0:
        return {**entry, "dropped": False, "worktree": path,
                "reason": "git branch -D failed: "
                          + deleted.stderr.strip()[:200]}
    return {**entry, "dropped": True, "worktree": path, "tip": tip}


def op_cleanup(mailbox: Path, repo: Path | None,
               a: argparse.Namespace) -> dict:
    """Remove merged builder worktrees and delete their branches.

    ``--drop-unmerged old[=new],…`` also drops builder branches superseded
    by a re-dispatch (see ``_drop_superseded``), reported in ``dropped``.
    """
    _require_lock(mailbox, a.token)
    root = _need_repo(repo, "cleanup")
    mailbox_rel = TL._mailbox_rel(root, mailbox)
    head = TL._git_head(root)
    wanted = [b.strip() for b in (a.branches or "").split(",") if b.strip()]
    drops = [d.strip() for d in (a.drop_unmerged or "").split(",")
             if d.strip()]
    trees = {t.get("branch"): t for t in _worktrees(root)}
    # Superseded branches first: an `old=new` pair checks that `new` is
    # merged before the loop below deletes the merged `new` branch.
    dropped = [_drop_superseded(root, mailbox_rel, head, d, trees)
               for d in drops]
    removed, kept = [], []
    for branch in wanted:
        tip = _branch_sha(root, branch)
        if tip is None:
            removed.append({"branch": branch, "worktree": None,
                            "note": "branch already gone"})
            continue
        if head is None or not TL._git_is_ancestor(root, tip, head):
            kept.append({"branch": branch, "reason": "not merged into HEAD"})
            continue
        tree = trees.get(branch)
        path = tree.get("path") if tree else None
        if path and Path(path).resolve() == root.resolve():
            kept.append({"branch": branch, "reason": "checked out in the repo"})
            continue
        if path:
            why = _remove_worktree(root, path, mailbox_rel)
            if why:
                kept.append({"branch": branch, "worktree": path,
                             "reason": why})
                continue
        deleted = TL._git(root, "branch", "-d", branch)
        if deleted.returncode != 0:
            kept.append({"branch": branch, "worktree": None,
                         "reason": "git branch -d failed: "
                         + deleted.stderr.strip()[:200]})
            continue
        removed.append({"branch": branch, "worktree": path})
    return {"removed": removed, "kept": kept, "dropped": dropped}


def _eval_worktrees(repo: Path | None) -> list[str]:
    if repo is None:
        return []
    marker = f"{repo}/{WORKTREES_DIR}/{EVAL_WORKTREE_PREFIX}"
    return sorted(t["path"] for t in _worktrees(repo)
                  if t.get("path", "").startswith(marker))


# ------------------------------------------------ retirement fold
_COMMIT_LINE = re.compile(r"^commit:\s*[0-9a-f]{7,40}\s*$")


def _commit_lines_appended(root: Path, rel: str) -> bool:
    """True when the working VERDICT.md is HEAD's copy plus appended
    ``commit: <sha>`` (or blank) lines only: an Evaluator that appended its
    ``commit:`` lines after the retirement commit (eval-native-v0b N6)."""
    shown = TL._git(root, "show", f"HEAD:{rel}")
    if shown.returncode != 0:
        return False
    try:
        now = (root / rel).read_text(encoding="utf-8")
    except OSError:
        return False
    before = shown.stdout
    if not now.startswith(before) or now == before:
        return False
    extra = now[len(before):]
    if before and not before.endswith("\n") and not extra.startswith("\n"):
        return False  # the first appended text continues HEAD's last line
    lines = extra.splitlines()
    return (any(_COMMIT_LINE.match(ln.strip()) for ln in lines)
            and all(not ln.strip() or _COMMIT_LINE.match(ln.strip())
                    for ln in lines))


def _fold_final_state(mailbox: Path, repo: Path | None,
                      iteration: int) -> str:
    """Fold the driver's final STATE (and LOG/.gitignore) into the SHIP
    retirement commit, so the tree is clean after SHIP.

    Same guard rails as ``trio_loop._fold_restored_verdict_into_retirement``:
    HEAD is the single-parent ``loop: iteration N — SHIP`` commit touching
    only the mailbox, nothing is staged, HEAD is on no remote branch, and
    every dirty path is a mailbox sidecar (STATE.md, LOG.md, .gitignore,
    and VERDICT.md when only ``commit:`` lines were appended). Returns "amended", "clean" or "skipped: <reason>".
    """
    root = TL._git_root(repo)
    if root is None:
        return "skipped: no repository"
    mailbox_rel = TL._mailbox_rel(root, mailbox)
    if not mailbox_rel:
        return "skipped: mailbox is not inside the repository"
    sidecars = {f"{mailbox_rel}/{n}" for n in ("STATE.md", "LOG.md",
                                                ".gitignore")}
    dirty = _dirty_paths(str(root))
    if dirty is None:
        return "skipped: git status failed"
    if not dirty:
        return "clean"
    verdict_rel = f"{mailbox_rel}/VERDICT.md"
    if verdict_rel in dirty and _commit_lines_appended(root, verdict_rel):
        sidecars.add(verdict_rel)  # eval-native-v0b N6
    others = [p for p in dirty if p not in sidecars]
    if others:
        return "skipped: other uncommitted paths: " + ", ".join(others[:5])
    head = TL._git_head(root)
    if head is None:
        return "skipped: no HEAD"
    info = TL._git(root, "log", "-1", "--format=%P%n%B", head)
    parents, _sep, message = info.stdout.partition("\n")
    if info.returncode != 0 or len(parents.split()) != 1:
        return "skipped: HEAD is a merge or root commit"
    if f"loop: iteration {iteration} — SHIP" not in message:
        return "skipped: HEAD is not the retirement commit"
    paths = TL._commit_paths(root, head)
    if not paths or any(not TL._path_in_mailbox(p, mailbox_rel)
                        for p in paths):
        return "skipped: HEAD touches paths outside the mailbox"
    if TL._git(root, "branch", "-r", "--contains", head).stdout.strip():
        return "skipped: HEAD is already on a remote branch"
    staged = TL._diff_paths(root, "--cached", "HEAD")
    if staged is None or staged:
        return "skipped: index has staged changes"
    if TL._git(root, "add", "--", *sorted(dirty)).returncode != 0:
        return "skipped: git add failed"
    amend = TL._git(root, "commit", "--amend", "--no-edit", "--no-verify",
                    "-q")
    if amend.returncode != 0:
        TL._git(root, "reset", "-q", "--", *sorted(dirty))
        return "skipped: git commit --amend failed"
    return "amended"


def _dangling_worktrees(repo: Path | None) -> list[str]:
    if repo is None:
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    marker = f"{repo}/.claude/worktrees/"
    return sorted(
        line[len("worktree "):] for line in result.stdout.splitlines()
        if line.startswith("worktree ") and line[9:].startswith(marker)
    )


def op_end(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    eval_removed, eval_kept = [], []
    lock_dir = mailbox / ".lock"
    ours = (lock_dir.is_dir() and _lock_owner(lock_dir) == _owner(a.token)
            and not _foreign_live_pid(lock_dir))
    if repo is not None and ours:
        rel = TL._mailbox_rel(repo, mailbox)
        for path in _eval_worktrees(repo):
            why = _remove_worktree(repo, path, rel, force_any=True)
            (eval_kept if why else eval_removed).append(
                {"worktree": path, "reason": why} if why else path)
    lock = _release(mailbox, a.token)
    snap = _snapshot(_state(mailbox))
    session = _read_json(mailbox / SESSION)
    # Only the lock owner closes the session record: `end` also runs after
    # a refused or garbled `begin` (N2), when another run may own both.
    if ours and session.get("session") == a.token and not session.get("done"):
        _write_session(mailbox, a.token, "done", done=True)
    return {"lock": lock, "dangling_worktrees": _dangling_worktrees(repo),
            "eval_worktrees_removed": eval_removed,
            "eval_worktrees_kept": eval_kept, **snap}


HANDLERS = {"begin": op_begin, "next": op_next, "dispatch": op_dispatch,
            "builders": op_builders, "cleanup": op_cleanup, "gate": op_gate,
            "pin": op_pin, "apply": op_apply, "end": op_end}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("op", choices=OPS)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--token", required=True,
                        help="workflow run token (lock owner workflow:<token>)")
    parser.add_argument("--nonce", required=True,
                        help="echoed verbatim in the JSON result")
    parser.add_argument("--repo", default=None,
                        help="product repo (default: git toplevel of mailbox)")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--iteration", type=int, default=0)
    parser.add_argument("--role", choices=("lead", "repair"), default="lead")
    parser.add_argument("--attempt", default="1",
                        help="gate: role attempt 1|2; apply: evaluator_attempt")
    parser.add_argument("--wave", type=int, default=1,
                        help="dispatch/builders: 1-based builder wave")
    parser.add_argument("--head", default=None,
                        help="builders: the dispatch HEAD sha")
    parser.add_argument("--results", default=None,
                        help="builders: JSON list of builder results")
    parser.add_argument("--branches", default=None,
                        help="cleanup: comma-separated merged builder branches")
    parser.add_argument("--drop-unmerged", default=None,
                        help="cleanup: comma-separated old[=new] builder "
                             "branches superseded by a re-dispatch")
    parser.add_argument("--json", action="store_true",
                        help="accepted for symmetry; output is always JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    mailbox = Path(a.mailbox).resolve()
    out: dict = {"op": a.op, "nonce": a.nonce, "mailbox": str(mailbox),
                 "api": NATIVE_STEP_API}
    try:
        if not mailbox.is_dir():
            raise StepError(f"mailbox {mailbox} is not a directory")
        if a.op == "gate":
            try:
                a.attempt = int(a.attempt)
            except ValueError as exc:
                raise StepError("gate --attempt must be 1 or 2") from exc
        repo = _repo_for(mailbox, a.repo)
        body = HANDLERS[a.op](mailbox, repo, a)
        out.update(body)
        out["ok"] = True
    except StepError as exc:
        out.update(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    out["nonce"] = a.nonce
    out["op"] = a.op
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""trio-native step helper: pull-mode ops over the stdlib Trio loop core.

The saved Workflow ``native/trio-native.js`` sequences a lockstep Trio loop;
it has no filesystem or process API, so every deterministic decision is one
call of this helper, run by a Bash-only ``trio-step`` agent:

    trio_native_step.py <op> --mailbox <abs> --token <run> --nonce <n> [...]

Ops (v0, lockstep): ``begin``, ``next``, ``gate``, ``pin``, ``apply``,
``end``. Each prints exactly one JSON object on stdout and exits 0 (usage
errors exit 2). Every result echoes ``--nonce`` so the script can detect a
step agent that answered for the wrong command.

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
``workflow:<run token>``; its ``pid`` is the long-lived Claude Code process
running the workflow (nearest ``claude`` ancestor), and a ``heartbeat`` file
is refreshed by every op. Another driver sees a live pid and stays out; a
different workflow token takes over only when that pid is dead or the
heartbeat is older than ``TRIO_NATIVE_LOCK_STALE_SECONDS`` (default 4 h).
"""
from __future__ import annotations

import argparse
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
DEFAULT_STALE_SECONDS = 4 * 3600.0
DRIVER = "claude-workflow"
OPS = ("begin", "next", "gate", "pin", "apply", "end")


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
    for key in ("gate", "apply"):
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


def _purge_stale_records(mailbox: Path, iteration: int) -> None:
    """A mailbox re-initialized to an earlier iteration drops old records."""
    data = _records(mailbox)
    changed = False
    for kind in ("gate", "apply"):
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


def _acquire(mailbox: Path, token: str) -> None:
    """Take (or re-enter) the mailbox lock for this workflow token."""
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        try:
            lock.mkdir()
        except FileExistsError:
            owner = _lock_owner(lock)
            if owner != _owner(token):
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
    """Every op after ``begin`` must still hold the lock; refresh it."""
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        owner = _lock_owner(lock)
        if owner != _owner(token):
            raise StepError(
                f"mailbox lock not held by {_owner(token)} "
                f"(owner: {owner or 'none'}); run begin first"
            )
        TL._write_lock_file(lock, "heartbeat", f"{time.time():.3f}\n")


def _release(mailbox: Path, token: str) -> str:
    lock = mailbox / ".lock"
    with TL._MailboxGuard(mailbox):
        if not lock.is_dir():
            return "released"
        owner = _lock_owner(lock)
        if owner != _owner(token):
            return "foreign"
        TL._discard_lock_dir(mailbox, lock)
    return "released"


# ---------------------------------------------------------- git exclude
def _ensure_exclude(repo: Path | None) -> str | None:
    """Add `.claude/worktrees/` to the repo's info/exclude exactly once."""
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
    if EXCLUDE_LINE not in (line.strip() for line in text.splitlines()):
        sep = "" if not text or text.endswith("\n") else "\n"
        path.write_text(f"{text}{sep}{EXCLUDE_LINE}\n", encoding="utf-8")
    return str(path)


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
        if status_word == "needs_retirement":
            code = TL._finalize_ship(
                mailbox, state_path, iteration, repo, context="resume"
            )
        after = _snapshot(_state(mailbox))
        return {"action": "stop", "code": code, "verdict": verdict, **after}
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
    _write_session(mailbox, a.token, f"{role}-running", done=False)
    return {"action": role, "iteration": iteration, "attempt": 1,
            "scope": scope if role == "repair" else None}


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
    checks = (
        TL._commit_gate(mailbox, repo),
        TL._log_gate(mailbox, iteration, role),
    )
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
    after = _snapshot(_state(mailbox))
    result = {
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
    lock = _release(mailbox, a.token)
    snap = _snapshot(_state(mailbox))
    session = _read_json(mailbox / SESSION)
    if session.get("session") == a.token and not session.get("done"):
        _write_session(mailbox, a.token, "done", done=True)
    return {"lock": lock, "dangling_worktrees": _dangling_worktrees(repo),
            **snap}


HANDLERS = {"begin": op_begin, "next": op_next, "gate": op_gate,
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

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
the Evaluator pin worktree ``pin`` assigned to this execution.

native-v01 (after N0): ``builders`` corrects a mis-reported builder sha
from git when the branch holds one well-formed slice commit, and tags every
refusal ``kind: report|work`` (the script re-asks / re-dispatches); ``end``
removes the scratch dirs this run execution created; ``begin`` of a fresh
run reuses (merges) or cleans up the builder worktrees an earlier run of
this mailbox left behind.

native-v01 fix (eval-v01): every merge, removal or deletion the helper does
is authorised only by its ownership ledger (``<git-common-dir>/trio-native/
<mailbox key>/owned.jsonl``, see "ownership ledger" below): builder pairs
git proved from the script's agent index, scratch dirs and Evaluator pin
paths the helper itself created or assigned. Names, role reports and
runtime files never authorise anything.

r19 frozen acceptance (only with ``--acceptance 1``, the script's
``args.acceptance``; every op is unchanged without it): ``begin`` refuses an
author off the Lead/Evaluator tier and reconciles the driver state with
git; ``acceptance-export`` builds the author's workspace; ``acceptance-freeze``
audits, validates at base and makes the driver's freeze commit;
``coverage`` refuses an unmapped plan before any builder; ``dispatch``,
``next`` and ``gate`` check the pin; ``acceptance-run`` pre-runs the pinned
pack for the Evaluator; ``apply`` judges amendments and runs the SHIP gate;
``end`` records the acceptance facts. Long ops run as detached jobs and
answer ``pending`` (see "frozen acceptance" below).

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
import copy
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

NATIVE_STEP_API = 1
HERE = Path(__file__).resolve().parent
METRICS_DIR = Path(
    os.environ.get("TRIO_NATIVE_METRICS_DIR") or HERE.parent / "metrics"
)
RECORDS = ".native.json"
SESSION = ".session.json"
#: Last run's outcome, for the dashboard (native-dash): ``end`` writes a
#: partial record (``source: "end"``); ``launch.sh`` overwrites it with the
#: parsed workflow result, session id, run id and cost (``source:
#: "launcher"``). Schema in native/README.md "Run registry and result".
RESULT = ".native-result.json"
#: Per-user registry of native runs, one JSON file per mailbox, so the
#: dashboard finds runs outside its scan roots (hidden lab dirs included).
#: ``TRIO_NATIVE_RUNS_DIR`` overrides it (tests always do).
DEFAULT_RUNS_DIR = Path.home() / ".local" / "share" / "trio-agent-loop" / "native-runs"
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
    ".native-launch.json", ".native-runs/", RESULT,
)
_MAILBOX_RUNTIME_DIRS = frozenset({".dispatch", ".sessions", ".lock",
                                   ".native-runs"})
OPS = ("begin", "next", "dispatch", "builders", "cleanup", "gate", "pin",
       "apply", "end", "acceptance-export", "acceptance-freeze", "coverage",
       "acceptance-run")
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


def _read_bytes(path: Path) -> bytes | None:
    """A regular file's bytes, opened without following a symlink (None when
    absent, a symlink, not a regular file or unreadable)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict:
    raw = _read_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    """Atomic replace: a ``mkstemp`` file in the same directory (never a
    fixed temp name) and a rename. A target that is a symlink or not a
    regular file is refused (OSError), never written through."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError(f"{path} is a symlink or not a regular file; refusing it")
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _human_answer(mailbox: Path, iteration: int, *, consume: bool = False,
                  key: str = "") -> dict:
    """The driver-verified human answer for a Lead / Evaluator dispatch of
    ``iteration`` (``metrics/human_ledger.py``): ``{human_answer: <block or
    "">, human_notes: [...]}``, or ``{}`` — no keys at all — when the
    mailbox has no HUMAN.md (every result, and so every prompt, is then
    unchanged). The script logs the notes; roles see only the block.
    ``consume`` (``pin``: the Evaluator that rules on the answer) marks it
    consumed in the ledger, so it is never delivered again — except to a
    retry of the same step in the same script execution (same ``key``,
    :func:`_retry_key`: the script re-runs a step whose stdout came back
    garbled)."""
    try:
        os.lstat(mailbox / "HUMAN.md")
    except OSError:
        return {}
    path = METRICS_DIR / "human_ledger.py"
    try:
        spec = importlib.util.spec_from_file_location("trio_native_human_ledger", path)
        if spec is None or spec.loader is None:
            raise ImportError(str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (ImportError, OSError, SyntaxError) as exc:
        return {"human_answer": "",
                "human_notes": [f"HUMAN.md ignored: no answer ledger module ({exc})"]}
    answer, notes = module.verified_answer(mailbox, iteration, consume=consume,
                                           role="evaluator" if consume else "lead",
                                           consume_key=key)
    return {"human_answer": module.driver_block(answer), "human_notes": notes}


def _runs_dir() -> Path:
    value = os.environ.get("TRIO_NATIVE_RUNS_DIR", "").strip()
    return Path(value).expanduser() if value else DEFAULT_RUNS_DIR


def registry_path(mailbox: Path) -> Path:
    """``<runs dir>/<sha256(realpath mailbox)[:16]>.json`` (launch.sh uses
    the same name)."""
    key = hashlib.sha256(str(Path(mailbox).resolve()).encode()).hexdigest()
    return _runs_dir() / f"{key[:16]}.json"


def _register(mailbox: Path, *, replace: bool = False,
              own_token: str | None = None, **fields) -> None:
    """Write this mailbox's run-registry record; never raises (the registry
    is a dashboard aid, never a reason to fail a step).

    ``replace=True`` (``begin``, which owns the mailbox lock by the time it
    calls this) drops any stale record entirely, so an old run's ``status``,
    ``session_id``, ``run_id`` and ``finished_at`` never survive into the
    new one. A merge (``end``) instead applies only when the existing
    record's ``run_token`` is ``own_token`` or unset, so a late ``end``
    (e.g. after a lock takeover) never clobbers a newer ``begin``'s record.
    Guarded by an flock on a sidecar lock file next to the registry file, so
    a concurrent writer of the same record (this helper or ``launch.sh``)
    never interleaves a read-modify-write.
    """
    try:
        path = registry_path(mailbox)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                          | os.O_CLOEXEC, 0o600)
        with os.fdopen(lock_fd, "a+", encoding="utf-8") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                data = {} if replace else _read_json(path)
                if not replace and own_token is not None:
                    existing = data.get("run_token")
                    if existing not in (None, own_token):
                        return
                data.update({"schema": 1, "driver": DRIVER,
                             "mailbox": str(Path(mailbox).resolve()),
                             "updated_at": _now_iso()})
                data.update({k: v for k, v in fields.items() if v is not None})
                _write_json(path, data)
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


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


def _write_session(mailbox: Path, token: str, phase: str, done: bool,
                   exec_id: str | None = None) -> None:
    """``exec_id``: ``begin`` mints a fresh run-execution id per script
    execution (kept by every later write of the same session)."""
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
    if exec_id is None and same and _EXEC_ID_RE.fullmatch(str(old.get("exec_id") or "")):
        exec_id = str(old["exec_id"])
    if exec_id is not None:
        payload["exec_id"] = exec_id
    _write_json(path, payload)


_EXEC_ID_RE = re.compile(r"[0-9a-f]{32}")


def _retry_key(mailbox: Path, token: str, nonce: str, iteration: int) -> str:
    """The consume/retry key of a ``pin`` delivery:
    ``native:{exec_id}:{nonce}@{iteration}``, where ``exec_id`` is the
    run-execution id ``begin`` minted for the current script execution (the
    helper's session state) and the nonce carries it
    (``<token>/<exec_id>/<seq>/<op>``). A retry of that same step in that
    same execution gets the answer again; a fresh run (a new ``begin``,
    whatever its run token) mints a new id, so it can never match a key an
    earlier execution consumed. Anything else: ``""`` (no retry allowance)."""
    session = _read_json(mailbox / SESSION)
    exec_id = str(session.get("exec_id") or "")
    if (session.get("driver") != DRIVER or session.get("session") != token
            or session.get("done") or not _EXEC_ID_RE.fullmatch(exec_id)):
        return ""
    if not nonce.startswith(f"{token}/{exec_id}/"):
        return ""
    return f"native:{exec_id}:{nonce}@{iteration}"


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


# ------------------------------------------ ownership ledger (v01 fix)
#: The governing rule (eval-v01): the driver merges, removes or deletes a
#: worktree, branch or directory only when a record THIS helper wrote proves
#: that one of this mailbox's runs owns it — never because of its name, a
#: role's report or a runtime file a role could write.
#:
#: The ledger is ``<git-common-dir>/trio-native/<mailbox key>/owned.jsonl``
#: (mailbox key = ``registry_path``'s sha256 of the mailbox realpath), an
#: append-only JSON-lines file inside the git dir, where no role writes in
#: normal operation. Entry kinds:
#:
#: * ``dispatch`` — not ownable: the names under ``.claude/worktrees/`` when
#:   a wave was dispatched (so a builder worktree must be new since then);
#: * ``run`` — not ownable: the workflow run id this execution's isolated
#:   builders were proven to carry (pinned once, see ``_own_builder_pair``);
#: * ``builder`` — a builder worktree + branch git proved to be the
#:   workflow's isolation worktree of this dispatch (``_own_builder_pair``);
#: * ``scratch`` — a directory this helper created itself (with the
#:   execution id in its name; ``st_dev``/``st_ino`` recorded);
#: * ``eval-worktree`` — the Evaluator pin worktree path this helper
#:   assigned at ``pin`` (exec id in the name) and its pinned sha; ``end``
#:   removes it only after git lists it there, detached at that sha;
#: * ``released`` — ownership of ``branch``/``path`` ended (removed).
#:
#: Every entry has ``exec_id``, ``run_id``, ``kind``, ``path``, ``branch``,
#: ``created_at`` and ``verified_by``.
LEDGER_DIR = "trio-native"
LEDGER_FILE = "owned.jsonl"
_WF_WORKTREE_RE = re.compile(r"^(wf_[A-Za-z0-9_-]{1,64})-([1-9][0-9]{0,6})$")
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _mailbox_key(mailbox: Path) -> str:
    return hashlib.sha256(str(Path(mailbox).resolve()).encode()).hexdigest()[:16]


def _git_common_dir(repo: Path | None) -> Path | None:
    if repo is None:
        return None
    out = TL._git(repo, "rev-parse", "--path-format=absolute",
                  "--git-common-dir")
    path = out.stdout.strip()
    return Path(path) if out.returncode == 0 and path else None


def _ledger_path(mailbox: Path, repo: Path | None) -> Path | None:
    common = _git_common_dir(repo)
    if common is None:
        return None
    return common / LEDGER_DIR / _mailbox_key(mailbox) / LEDGER_FILE


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _ledger_append(mailbox: Path, repo: Path | None, entry: dict) -> bool:
    """Append one entry (one ``write`` under an flock, fsync'd). The ledger
    dirs and file are never followed through a symlink. False when the
    ledger cannot be written (then nothing is ownable)."""
    path = _ledger_path(mailbox, repo)
    if path is None:
        return False
    try:
        for d in (path.parent.parent, path.parent):
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
            if not _real_dir(d):
                return False
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        record = {"created_at": _now_iso(), "exec_id": None, "run_id": None,
                  "path": None, "branch": None, "verified_by": None, **entry}
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _ledger_read(path: Path) -> list[dict]:
    raw = _read_bytes(path)
    out = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("kind"), str):
            out.append(item)
    return out


def _ledger(mailbox: Path, repo: Path | None) -> list[dict]:
    path = _ledger_path(mailbox, repo)
    if path is None or not _real_dir(path.parent):
        return []
    return _ledger_read(path)


def _foreign_run_ids(mailbox: Path, repo: Path | None) -> set[str]:
    """Run ids another mailbox's ledger (in the same repository) holds."""
    common = _git_common_dir(repo)
    if common is None or not _real_dir(common / LEDGER_DIR):
        return set()
    own = _mailbox_key(mailbox)
    ids: set[str] = set()
    try:
        keys = os.listdir(common / LEDGER_DIR)
    except OSError:
        return ids
    for key in keys:
        if key == own or not _real_dir(common / LEDGER_DIR / key):
            continue
        for e in _ledger_read(common / LEDGER_DIR / key / LEDGER_FILE):
            if e.get("run_id"):
                ids.add(str(e["run_id"]))
    return ids


def _current_exec_id(mailbox: Path) -> str:
    exec_id = str(_read_json(mailbox / SESSION).get("exec_id") or "")
    return exec_id if _EXEC_ID_RE.fullmatch(exec_id) else ""


def _owned(entries: list[dict], kind: str, *, exec_id: str | None = None
           ) -> list[dict]:
    """Live ownership entries of ``kind`` (latest per branch/path, not
    released afterwards), optionally of one execution only."""
    live: dict[tuple, dict] = {}
    for e in entries:
        k = e.get("kind")
        ident = (e.get("branch") or None, e.get("path") or None)
        if k == "released":
            for key in list(live):
                if ((ident[0] and key[0] == ident[0])
                        or (ident[1] and key[1] == ident[1])):
                    del live[key]
        elif k == kind:
            live[ident] = e
    return [e for e in live.values()
            if exec_id is None or e.get("exec_id") == exec_id]


def _owned_branch(mailbox: Path, repo: Path | None, branch: str, *,
                  exec_id: str | None = None) -> dict | None:
    for e in _owned(_ledger(mailbox, repo), "builder", exec_id=exec_id):
        if e.get("branch") == branch:
            return e
    return None


def _release_owned(mailbox: Path, repo: Path | None, entry: dict,
                   why: str) -> None:
    _ledger_append(mailbox, repo, {
        "kind": "released", "exec_id": _current_exec_id(mailbox) or None,
        "run_id": entry.get("run_id"), "path": entry.get("path"),
        "branch": entry.get("branch"), "verified_by": why})


# ------------------------------------------ fd-based directory handling
def _open_dir_at(parent_fd: int, name: str, expect: os.stat_result | None
                 ) -> int:
    """An O_RDONLY directory fd for ``name`` under ``parent_fd``, never
    through a symlink. A directory without read/search permission for us
    (mode 000, e.g. a permission test's leftover) is opened ``O_PATH``,
    verified (same dev/ino as ``expect``) and made 0700 through
    ``/proc/self/fd`` — the chmod reaches exactly that inode, which lies
    inside the tree being removed — then reopened and verified again."""
    try:
        fd = os.open(name, _O_DIR, dir_fd=parent_fd)
    except PermissionError:
        pfd = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
                      | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            st = os.fstat(pfd)
            if expect is not None and (st.st_dev, st.st_ino) != (
                    expect.st_dev, expect.st_ino):
                raise OSError(f"{name} changed while being removed")
            os.chmod(f"/proc/self/fd/{pfd}", stat.S_IMODE(st.st_mode)
                     | stat.S_IRWXU)
        finally:
            os.close(pfd)
        fd = os.open(name, _O_DIR, dir_fd=parent_fd)
    st = os.fstat(fd)
    if expect is not None and (st.st_dev, st.st_ino) != (expect.st_dev,
                                                          expect.st_ino):
        os.close(fd)
        raise OSError(f"{name} changed while being removed")
    return fd


def _rm_contents(dfd: int, dev: int, depth: int = 0) -> list[str]:
    """Remove everything inside the open directory ``dfd``; every entry is
    handled (and its failure recorded) on its own. Only directories inside
    this tree are ever chmod-ed (``fchmod`` on an fd of this walk), symlinks
    are unlinked, never followed, and another filesystem is never entered."""
    errors: list[str] = []
    if depth > 256:
        return ["directory tree deeper than 256 levels"]
    try:
        st = os.fstat(dfd)
        if stat.S_IMODE(st.st_mode) & stat.S_IRWXU != stat.S_IRWXU:
            os.fchmod(dfd, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
        names = os.listdir(dfd)
    except Exception as exc:  # noqa: BLE001 - reported per entry
        return [f"{type(exc).__name__}: {exc}"]
    for name in names:
        try:
            est = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISDIR(est.st_mode):
                if est.st_dev != dev:
                    errors.append(f"{name}: another filesystem, not entered")
                    continue
                cfd = _open_dir_at(dfd, name, est)
                try:
                    sub = _rm_contents(cfd, dev, depth + 1)
                finally:
                    os.close(cfd)
                if sub:
                    errors.extend(f"{name}/{e}" for e in sub)
                    continue
                os.rmdir(name, dir_fd=dfd)
            else:
                os.unlink(name, dir_fd=dfd)
        except Exception as exc:  # noqa: BLE001 - reported per entry
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return errors


def _worktrees_dir_fd(repo: Path, create: bool = False) -> int | None:
    """An fd of ``<repo>/.claude/worktrees`` opened component by component
    without following a symlink (None when missing or a symlink)."""
    try:
        fd = os.open(str(repo), _O_DIR)
    except OSError:
        return None
    try:
        for part in WORKTREES_DIR.split("/"):
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nfd = os.open(part, _O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except OSError:
        os.close(fd)
        return None


def _remove_owned_dir(repo: Path, entry: dict) -> list[str]:
    """Remove one ledger-owned scratch directory; [] on success, else why
    (it is then left in place). It must still be a real directory directly
    under ``.claude/worktrees/`` with the recorded dev/ino, and neither be
    nor contain a registered git worktree."""
    name = os.path.basename(str(entry.get("path") or ""))
    if not name or name in (".", "..") or str(entry.get("path")) != str(
            repo / WORKTREES_DIR / name):
        return ["not a directory directly under .claude/worktrees/"]
    mfd = _worktrees_dir_fd(repo)
    if mfd is None:
        return [".claude/worktrees/ is missing or a symlink"]
    try:
        try:
            st = os.stat(name, dir_fd=mfd, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(st.st_mode):
            return ["not a real directory (a symlink or a file)"]
        if (st.st_dev, st.st_ino) != (entry.get("dev"), entry.get("ino")):
            return ["not the directory this run created (dev/inode differ)"]
        real = os.path.join(os.path.realpath(str(repo / WORKTREES_DIR)), name)
        for t in _worktrees(repo):
            r = os.path.realpath(t.get("path") or "/")
            if r == real or r.startswith(real + os.sep):
                return [f"contains the git worktree {t.get('path')}"]
        dfd = _open_dir_at(mfd, name, st)
        try:
            errors = _rm_contents(dfd, st.st_dev)
        finally:
            os.close(dfd)
        if errors:
            return errors[:5]
        os.rmdir(name, dir_fd=mfd)
        return []
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return [f"{type(exc).__name__}: {exc}"]
    finally:
        os.close(mfd)


def _make_scratch(mailbox: Path, repo: Path | None, exec_id: str,
                  name: str, purpose: str) -> str | None:
    """Create ``.claude/worktrees/<name>`` (the name carries the exec id)
    and record it as this execution's scratch. An existing directory is
    reused only when the ledger already lists it for this execution with
    the same dev/ino. Returns the path, or None."""
    if repo is None or not exec_id or exec_id not in name:
        return None
    path = str(repo / WORKTREES_DIR / name)
    for e in _owned(_ledger(mailbox, repo), "scratch", exec_id=exec_id):
        if e.get("path") == path:
            try:
                st = os.lstat(path)
            except OSError:
                break
            if (stat.S_ISDIR(st.st_mode)
                    and (st.st_dev, st.st_ino) == (e.get("dev"), e.get("ino"))):
                return path
            return None
    mfd = _worktrees_dir_fd(repo, create=True)
    if mfd is None:
        return None
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=mfd)
        except FileExistsError:
            return None  # not created by this helper: never claimed
        st = os.stat(name, dir_fd=mfd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(mfd)
    ok = _ledger_append(mailbox, repo, {
        "kind": "scratch", "exec_id": exec_id, "path": path,
        "dev": st.st_dev, "ino": st.st_ino, "purpose": purpose,
        "verified_by": "created by the helper (mkdir)"})
    return path if ok else None


def _operation_in_progress(root: Path) -> str | None:
    """The in-progress git operation of the checkout (merge, rebase,
    cherry-pick, revert), or None."""
    names = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
             "rebase-apply")
    args = ["rev-parse", "--path-format=absolute"]
    for n in names:
        args += ["--git-path", n]
    out = TL._git(root, *args)
    paths = out.stdout.splitlines()
    if out.returncode != 0 or len(paths) != len(names):
        return "git state unreadable"
    for n, p in zip(names, paths):
        if os.path.lexists(p):
            return n
    return None


# ------------------------------------------------------------------- ops
def op_begin(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    if not (mailbox / "GOAL.md").is_file():
        raise StepError(f"{mailbox}/GOAL.md missing: run /trio-init first")
    if (mailbox / "QUEUE.md").is_file():
        raise StepError(
            "QUEUE.md present: open-loop mailboxes are not supported by "
            "trio-native v0 (use trio_loop.py run or remove QUEUE.md)"
        )
    if _acc_on(a):
        # N1: refused before the lock is taken or anything is written.
        problem = _acc_tier_problem(_acc_json(a, "models"))
        if problem:
            raise StepError(problem)
        if not _acc_tool().is_file():
            raise StepError(f"acceptance: {_acc_tool()} is missing (refresh metrics/ as a set)")
    _acquire(mailbox, a.token)
    _ensure_mailbox_gitignore(mailbox)
    if not (mailbox / "LOG.md").is_file():
        (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    state = _state(mailbox)
    snap = _snapshot(state)
    _purge_stale_records(mailbox, snap["iteration"])
    exclude = _ensure_exclude(repo)
    exec_id = uuid.uuid4().hex
    _write_session(mailbox, a.token, snap["phase"], done=False, exec_id=exec_id)
    # v01 item 3: this run holds the lock, so every builder worktree an
    # earlier run of this mailbox left is dead; reuse or clean up the ones
    # the ownership ledger proves are this mailbox's (nothing else).
    reclaimed = _reclaim_builders(mailbox, repo)
    # v01 item 2: scratch dirs earlier executions of this mailbox created
    # (ledger-owned; e.g. a run that crashed before `end`), then this
    # execution's own TMPDIR, created and recorded by the helper.
    stale = _remove_exec_scratch(mailbox, repo, exclude_exec=exec_id)
    reclaimed["scratch_removed"], reclaimed["scratch_kept"] = stale
    tmpdir = _make_scratch(mailbox, repo, exec_id, f"tmp-{exec_id}", "tmpdir")
    _register(mailbox, replace=True, repo=str(repo) if repo else None,
              helper=str(Path(__file__).resolve()), run_token=a.token,
              holder_pid=_holder_pid(), state="running",
              begun_at=_now_iso(),
              acceptance={"enabled": True} if _acc_on(a) else None)
    extra = ({"acceptance": _acc_begin(mailbox, repo, a, snap["iteration"], exec_id)}
             if _acc_on(a) else {})
    return {
        "mode": "lockstep",
        "repo": str(repo) if repo else None,
        "lock_owner": _owner(a.token),
        "exec_id": exec_id,
        "exclude_path": exclude,
        "reclaimed": reclaimed,
        "tmpdir": tmpdir,
        **snap,
        **extra,
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
        if _acc_on(a):
            # eval-r19n: deleting a failed gate record never buys an attempt.
            sealed = _acc_sealed_native(mailbox, repo, a).get("gate_fail") or {}
            fails = max(fails, int(sealed.get(f"{iteration}:{role}", 0) or 0))
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
        if _acc_on(a):
            digest = data["report"][str(iteration)]
            _acc_sealed_update(mailbox, repo, a, lambda st: _acc_native_of(st).setdefault(
                "report", {}).__setitem__(str(iteration), digest))
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


def _report_gate(mailbox: Path, iteration: int,
                 sealed: str | None = None) -> tuple[bool, str]:
    """A Lead pass must rewrite REPORT.md (probe blocker 6).

    The digest is recorded by ``next`` when it starts the pass; a pass
    started by another driver has no record and is not checked. With frozen
    acceptance the helper's sealed copy (*sealed*) wins over the mailbox
    record, which a role can delete (eval-r19n).
    """
    before = _records(mailbox)["report"].get(str(iteration))
    if sealed is not None:
        before = sealed
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
    if _acc_on(a):
        # eval-r19n: a recorded answer counts only when this execution's
        # helper wrote it (HMAC); its digest is re-derived, never replayed.
        auth = _acc_auth_or_stop(mailbox, repo, a)
        recorded = _acc_recorded(mailbox, auth, "gate", key)
        if recorded is not None:
            recorded["acceptance"] = _acc_fresh_digest(mailbox, repo, a, iteration)
            return recorded
    else:
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
        sealed_report = None
        if _acc_on(a):
            sealed_report = (_acc_sealed_native(mailbox, repo, a).get("report")
                             or {}).get(str(iteration))
        checks.append(_report_gate(mailbox, iteration, sealed_report))
    acc_error = None
    acc_digest: dict = {}
    if _acc_on(a):
        problems, acc_error, acc_digest = _acc_gate(mailbox, repo, a, iteration, role)
        if problems:
            checks.append((False, "acceptance: " + "; ".join(problems)[:400]))
    failures = [note for ok, note in checks if not ok]
    result: dict = {
        "role": role, "iteration": iteration, "attempt": attempt,
        "pass": not failures, "failures": failures,
        "check": _advisory_check(mailbox),
    }
    if _acc_on(a):
        result["acceptance"] = acc_digest
    if acc_error is not None:
        # _run_role: an acceptance phase that cannot continue stops the
        # loop at once (no retry of the role can fix it).
        TL._update_state(mailbox / "STATE.md", {"status": "error", "phase": "error"})
        TL._append_log(
            mailbox,
            f"- iter {iteration} | loop | gate breach after {role}: "
            f"acceptance {acc_error.reason}: {acc_error.detail}",
        )
        _acc_seal_stop(mailbox, f"error: {acc_error.reason}")
        result.update(**{"pass": False}, final=True, status="error", phase="error",
                      failures=failures + [f"acceptance {acc_error.reason}: "
                                           f"{str(acc_error.detail)[:300]}"])
        result["detail"] = _shadow_detail(mailbox, repo)
        _gate_record(mailbox, repo, a, key, result)
        return result
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
    _gate_record(mailbox, repo, a, key, result)
    return result


def _gate_record(mailbox: Path, repo: Path | None, a: argparse.Namespace,
                 key: str, result: dict) -> None:
    if not _acc_on(a):
        _record(mailbox, "gate", key, result)
        return
    if not result.get("pass"):
        slot = f"{result['iteration']}:{result['role']}"

        def bump(st: dict) -> None:
            fails = _acc_native_of(st).setdefault("gate_fail", {})
            fails[slot] = int(fails.get(slot, 0) or 0) + 1
        _acc_sealed_update(mailbox, repo, a, bump)
    _acc_record(mailbox, _acc_auth_or_stop(mailbox, repo, a), "gate", key, result)


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


def _eval_places(mailbox: Path, repo: Path | None, iteration: int,
                 attempt: str, sha: str) -> dict:
    """``pin``: the Evaluator's pin-worktree path and scratch dir, both named
    with this execution's id. The scratch dir is created and recorded here;
    the worktree path is recorded as assigned (with the pinned sha) and is
    removed at ``end`` only when git lists a worktree exactly there,
    detached at that sha."""
    exec_id = _current_exec_id(mailbox)
    tag = re.sub(r"[^0-9A-Za-z]", "", attempt)[:8] or "0"
    out: dict = {"eval_worktree": None, "eval_scratch": None,
                 "tmpdir": None}
    if repo is None or not exec_id:
        return out
    base = f"eval-{exec_id}-{iteration}-{tag}"
    out["eval_scratch"] = _make_scratch(mailbox, repo, exec_id,
                                        base + "-scratch", "eval-scratch")
    path = str(repo / WORKTREES_DIR / base)
    entries = _ledger(mailbox, repo)
    assigned = any(e.get("path") == path and e.get("sha") == sha for e in
                   _owned(entries, "eval-worktree", exec_id=exec_id))
    if assigned or (sha and _ledger_append(mailbox, repo, {
            "kind": "eval-worktree", "exec_id": exec_id, "path": path,
            "sha": sha, "iteration": iteration,
            "verified_by": "assigned by the helper at pin; removed only when "
                           "git lists it here detached at sha"})):
        out["eval_worktree"] = path
    for e in _owned(entries, "scratch", exec_id=exec_id):
        if e.get("purpose") == "tmpdir":
            out["tmpdir"] = e.get("path")
    return out


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
        **_eval_places(mailbox, repo, a.iteration,
                       str(ctx["evaluator_attempt"] or ""),
                       str(ctx["pinned_sha"] or "")),
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
    if _acc_on(a):
        return _acc_apply(mailbox, repo, a, key)
    recorded = _recorded(mailbox, "apply", key)
    if recorded is not None:
        return recorded
    return _apply_body(mailbox, repo, a, key)


def _head_tree(repo: Path | None) -> str:
    if repo is None:
        return ""
    out = TL._git(repo, "rev-parse", "HEAD^{tree}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _acc_apply(mailbox: Path, repo: Path | None, a: argparse.Namespace,
               key: str) -> dict:
    """r19 ``apply`` (eval-r19n finding 1): never a cached SHIP. The verdict
    is judged by ``review_verdict`` (amendments, UNAVAILABLE, the frozen
    pack on the landed tree) in a job, unless THIS execution's helper
    already applied this attempt: an HMAC-valid record whose landed tree is
    HEAD's tree now. That replay is logged. A record or job result a role
    wrote, or one for another tree, is never returned."""
    auth = _acc_auth_or_stop(mailbox, repo, a)
    tree = _head_tree(repo)
    recorded = _acc_recorded(mailbox, auth, "apply", key)
    if recorded is not None:
        if not tree or recorded.get("landed_tree") != tree:
            raise StepError(
                f"apply {key}: this execution already applied the verdict on tree "
                f"{str(recorded.get('landed_tree'))[:12]}, but HEAD's tree is now "
                f"{tree[:12] or '(none)'}; the recorded answer is not replayed (commits "
                "after the verdict); stop and re-evaluate")
        TL._append_log(mailbox, f"- iter {a.iteration} | loop | acceptance: apply {key} "
                                f"replayed (this execution's recorded answer; landed tree "
                                f"{tree[:12]} unchanged)")
        recorded["replayed"] = True
        return recorded

    def landed(body: dict) -> str | None:
        now = _head_tree(repo)
        if body.get("landed_tree") != now:
            return (f"apply {key}: the job's answer was judged on tree "
                    f"{str(body.get('landed_tree'))[:12]}, HEAD's tree is now {now[:12]}")
        return None
    # r19: amendments and the SHIP gate re-run the pack (a job).
    return _run_job(mailbox, repo, a, f"apply-{a.iteration}-{a.attempt}",
                    lambda: _apply_body(mailbox, repo, a, key), check=landed)


def _apply_body(mailbox: Path, repo: Path | None, a: argparse.Namespace,
                key: str) -> dict:
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
    forced = None
    acc_record = None
    _fence()
    if _acc_on(a) and verdict is not None:
        verdict, scope, forced, acc_record = _acc_review(
            mailbox, repo, a, verdict, scope, state["evaluated_sha"].strip())
    if verdict is None:
        TL._update_state(state_path, {"status": "error", "phase": "error"})
        TL._append_log(
            mailbox, f"- iter {a.iteration} | loop | unparseable verdict"
        )
        code = 3
    elif forced is not None:
        code = forced  # STATE already says needs_human / error
    else:
        _fence()
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
    if acc_record is not None:
        result["acceptance"] = acc_record
    if _acc_on(a):
        # The tree this answer was judged and applied on (after the
        # retirement fold): a replay must find it unchanged.
        result["landed_tree"] = _head_tree(repo)
        _fence()
        _acc_record(mailbox, _acc_auth_or_stop(mailbox, repo, a), "apply", key, result)
        return result
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
    if _acc_on(a):
        _acc_dispatch_refusal(mailbox, repo, a, a.iteration)
    head = TL._git_head(root)
    if head is None:
        raise StepError("dispatch: repository has no HEAD commit")
    # What already exists under .claude/worktrees/: a builder worktree of
    # this wave must be new since now (`_own_builder_pair`).
    _ledger_append(mailbox, root, {
        "kind": "dispatch", "exec_id": _current_exec_id(mailbox) or None,
        "iteration": a.iteration, "wave": a.wave, "head": head,
        "names": _worktrees_dir_entries(root) or [],
        "verified_by": "listing of .claude/worktrees/ at dispatch"})
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


def _under_worktrees_marker(root: Path, path: str) -> bool:
    """True when ``path``'s real filesystem location is inside
    ``<root>/.claude/worktrees/`` (eval-native-v0c C2: compared by
    ``os.path.realpath``, so a symlinked path cannot claim to be a builder
    worktree it is not)."""
    marker = os.path.realpath(str(root / WORKTREES_DIR))
    real = os.path.realpath(path)
    return real == marker or real.startswith(marker + os.sep)


def _single_slice_commit(repo: Path, head: str, tip: str, sid: str) -> bool:
    """True when ``head..tip`` is exactly one non-merge commit whose parent
    is ``head`` and whose subject is ``slice(<sid>): …`` — the one
    well-formed builder commit a sha correction may accept (v01 item 1)."""
    listed = TL._git(repo, "rev-list", f"{head}..{tip}").stdout.split()
    if listed != [tip]:
        return False
    info = TL._git(repo, "log", "-1", "--format=%P%n%s", tip)
    parents, _sep, subject = info.stdout.partition("\n")
    return (info.returncode == 0 and parents.split() == [head]
            and subject.strip().startswith(f"slice({sid}):"))


def _refusal(reason: str, kind: str, own_branch: str | None = None) -> dict:
    """A builder refusal: ``kind`` is ``report`` when only the builder's
    report is in doubt (asking it to report again may resolve it) and
    ``work`` when git shows the work itself is unusable. ``own_branch`` is
    set only when the refused branch is this dispatch's own, ledger-owned
    builder branch (``_own_builder_pair``) — the only branch a re-dispatch
    may supersede."""
    return {"reason": reason, "kind": kind, "own_branch": own_branch}


def _branch_created_at(repo: Path, branch: str) -> str | None:
    """The sha the branch was created at (its oldest reflog entry), or None
    when git keeps no reflog for it."""
    out = TL._git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}",
                  "--")
    shas = out.stdout.split() if out.returncode == 0 else []
    return shas[-1] if shas else None


def _own_builder_pair(mailbox: Path, root: Path, exec_id: str,
                      iteration: int, wave: int, head: str, index: object,
                      trees: list[dict], reported: tuple[str, str] = ("", "")
                      ) -> tuple[dict | None, str]:
    """The workflow's isolation worktree of builder agent ``index`` of this
    dispatch, proven from git alone (never from the builder's report):
    ``(entry, "")`` or ``(None, why)``.

    The harness names an isolated agent's worktree ``<repo>/.claude/
    worktrees/<runId>-<n>`` on branch ``worktree-<runId>-<n>``, ``n`` being
    the 1-based number of the ``agent()`` call in the run (probe P4; N0
    vps-pool r1: builders 5..9 after begin, next, lead plan, dispatch). The
    script counts its own ``agent()`` calls and passes ``n`` as the result's
    ``agent_index``. A worktree qualifies only when ``git worktree list``
    has it at exactly that path (real path, directly under the marker, not a
    symlink) on exactly that branch, it did not exist at this wave's
    ``dispatch`` (ledger snapshot), and the branch was created at the
    dispatch HEAD (its reflog; without a reflog it must contain that HEAD).
    ``runId`` is not visible to the script: the first unambiguous match of
    an execution pins it in the ledger (``run``); a run id another
    mailbox's ledger or an earlier execution holds never qualifies, and more
    than one candidate is ambiguous (nothing owned). Before the run id is
    pinned, the builder's report must also name that same worktree and
    branch (a necessary, never a sufficient, condition)."""
    try:
        n = int(str(index))
    except (TypeError, ValueError):
        return None, "no agent index from the script"
    if n < 1 or not exec_id:
        return None, "no agent index from the script"
    entries = _ledger(mailbox, root)
    snaps = [e for e in entries if e.get("kind") == "dispatch"
             and e.get("exec_id") == exec_id and e.get("iteration") == iteration
             and e.get("wave") == wave and e.get("head") == head]
    if not snaps:
        return None, "no dispatch record of this wave in the ownership ledger"
    before = set(snaps[-1].get("names") or [])
    pinned = [e.get("run_id") for e in entries
              if e.get("kind") == "run" and e.get("exec_id") == exec_id]
    taken = _foreign_run_ids(mailbox, root) | {
        str(e["run_id"]) for e in entries
        if e.get("run_id") and e.get("exec_id") != exec_id}
    marker = os.path.realpath(str(root / WORKTREES_DIR))
    found = []
    for t in trees:
        path, branch = t.get("path") or "", t.get("branch") or ""
        name = os.path.basename(path)
        m = _WF_WORKTREE_RE.fullmatch(name)
        if not m or int(m.group(2)) != n or branch != f"worktree-{name}":
            continue
        rid = m.group(1)
        if (pinned and rid != pinned[-1]) or rid in taken or name in before:
            continue
        if (os.path.realpath(os.path.dirname(path)) != marker
                or os.path.realpath(path) != os.path.join(marker, name)
                or not _real_dir(Path(path))):
            continue
        created = _branch_created_at(root, branch)
        if created is not None and created != head:
            continue
        tip = _branch_sha(root, branch)
        if tip is None or not TL._git_is_ancestor(root, head, tip):
            continue
        found.append({"run_id": rid, "path": path, "branch": branch,
                      "tip": tip, "agent_index": n,
                      "reflog": created is not None})
    if len(found) != 1:
        return None, (f"no isolation worktree <runId>-{n} of this dispatch"
                      if not found else
                      f"{len(found)} candidate worktrees for agent {n}: ambiguous")
    own = found[0]
    if not pinned:
        rep_path, rep_branch = reported
        if (not rep_path or rep_branch != own["branch"]
                or os.path.realpath(rep_path) != os.path.realpath(own["path"])):
            return None, ("the run id is not pinned yet and the report does "
                          f"not name {own['path']} on {own['branch']}")
        _ledger_append(mailbox, root, {
            "kind": "run", "exec_id": exec_id, "run_id": own["run_id"],
            "verified_by": f"the one new isolation worktree for agent {n} "
                           f"at dispatch HEAD {head[:12]}"})
    return own, ""


def _check_builder(repo: Path, mailbox_rel: str | None, head: str,
                   res: dict, worktree_branches: dict[str, str | None],
                   own: dict | None = None
                   ) -> tuple[dict | None, dict | None]:
    """``(refusal, correction)`` for one builder result; both None = ok.

    ``worktree_branches`` maps each worktree's real path to the branch it is
    actually checked out on (from ``git worktree list --porcelain``), so a
    builder cannot report a branch it does not itself own (eval-native-v0c
    C2): the ``worktree`` it names must really be on the ``branch`` it names.

    A reported ``head`` that does not match the branch tip (N0 vps-pool r1:
    ``cc51ac69a9ba`` committed, ``cc51ac6a9bad…`` reported) is re-read from
    git: when the branch (own worktree, descends from the dispatch HEAD, no
    ``loop/`` commits) holds exactly one well-formed ``slice(<id>):``
    commit, the tip is accepted and returned as a ``correction``
    ``{reported, actual}``; otherwise the refusal is of kind ``report``.
    """
    sid = str(res.get("id") or "?")
    base = str(res.get("base") or "").strip()
    if not base or not TL._sha_matches(base, head):
        return _refusal(f"builder {sid} forked from {base or '(unknown)'}, "
                        f"not the Lead's HEAD {head[:12]}: {BASE_REF_HINT}",
                        "work"), None
    commits = res.get("commits") or []
    if not commits:
        return None, None  # nothing to merge
    branch = str(res.get("branch") or "").strip()
    worktree = str(res.get("worktree") or "").strip()
    if own is not None and (branch != own["branch"] or not worktree or
                            os.path.realpath(worktree)
                            != os.path.realpath(own["path"])):
        # git knows this dispatch's own worktree: a report naming another
        # pair is refused (the builder is asked again), never trusted.
        return _refusal(f"builder {sid}: reported worktree {worktree or '(none)'} "
                        f"on {branch or '(none)'} is not this dispatch's "
                        f"isolation worktree {own['path']} on {own['branch']}",
                        "report"), None
    mine = own["branch"] if own is not None else None
    tip = _branch_sha(repo, branch) if branch else None
    if tip is None:
        return _refusal(f"builder {sid}: branch {branch or '(none)'} does "
                        "not exist", "report"), None
    if not worktree:
        return _refusal(f"builder {sid}: no worktree reported for branch "
                        f"{branch}", "report"), None
    on_branch = worktree_branches.get(os.path.realpath(worktree))
    if on_branch != branch:
        return _refusal(
            f"builder {sid}: reported worktree {worktree} is not the "
            f"builder's own worktree for branch {branch} (it is "
            + (f"checked out on {on_branch}" if on_branch
               else "not a known worktree") + ")", "report"), None
    if not TL._git_is_ancestor(repo, head, tip):
        return _refusal(f"builder {sid}: branch {branch} does not contain "
                        f"the Lead's HEAD {head[:12]}: {BASE_REF_HINT}",
                        "work", mine), None
    listed = TL._git(repo, "rev-list", f"{head}..{tip}").stdout.split()
    for sha in listed:
        loop_paths = [p for p in TL._commit_paths(repo, sha)
                      if TL._path_in_mailbox(p, mailbox_rel)]
        if loop_paths:
            return _refusal(f"builder {sid}: commit {sha[:12]} commits "
                            f"mailbox files ({', '.join(loop_paths[:3])}); "
                            "builders never commit loop/", "work",
                            mine), None
    reported = str(res.get("head") or "").strip()
    if reported and not TL._sha_matches(reported, tip):
        if _single_slice_commit(repo, head, tip, sid):
            return None, {"id": sid, "branch": branch, "reported": reported,
                          "actual": tip}
        return _refusal(f"builder {sid}: reported head {reported[:12]} but "
                        f"branch {branch} is at {tip[:12]} ({len(listed)} "
                        "commit(s) since the dispatch HEAD, not one "
                        f"well-formed slice({sid}): commit)", "report",
                        mine), None
    return None, None


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
    report_round = str(a.attempt or "1").strip()
    if report_round not in ("", "1"):
        # A builder asked to report again (v01 item 1) is re-verified under
        # its own key, never answered from the first report's record.
        key += f":report{report_round}"
    recorded = _recorded(mailbox, "builders", key)
    if recorded is not None:
        return recorded
    _lead_running(mailbox, a.iteration, "builders")
    root = _need_repo(repo, "builders")
    head = str(a.head or "").strip()
    if not head:
        raise StepError("builders: --head (the dispatch HEAD) is required")
    mailbox_rel = TL._mailbox_rel(root, mailbox)
    trees = _worktrees(root)
    worktree_branches = {os.path.realpath(t["path"]): t.get("branch")
                         for t in trees if t.get("path")}
    exec_id = _current_exec_id(mailbox)
    accepted, refused, merge, corrected = [], [], [], []
    owned, unowned = [], []
    for res in results:
        sid = _one_line(res.get("id"), 64) or "?"
        own, why = _own_builder_pair(
            mailbox, root, exec_id, a.iteration, a.wave, head,
            res.get("agent_index"), trees,
            (str(res.get("worktree") or "").strip(),
             str(res.get("branch") or "").strip()))
        if own is not None:
            # Ownership is git's (and the script's agent index), not the
            # report's: recorded whether or not the report is accepted.
            if _owned_branch(mailbox, root, own["branch"],
                             exec_id=exec_id) is None:
                _ledger_append(mailbox, root, {
                    "kind": "builder", "exec_id": exec_id,
                    "run_id": own["run_id"], "path": own["path"],
                    "branch": own["branch"], "id": sid,
                    "iteration": a.iteration, "wave": a.wave, "head": head,
                    "tip": own["tip"], "agent_index": own["agent_index"],
                    "verified_by": "git worktree list: <runId>-<agent index> "
                                   "isolation worktree, new since dispatch, "
                                   + ("branch reflog created at the dispatch "
                                      "HEAD" if own["reflog"] else
                                      "branch contains the dispatch HEAD "
                                      "(no reflog)")})
            owned.append({"id": sid, "branch": own["branch"],
                          "worktree": own["path"]})
        elif res.get("commits"):
            unowned.append({"id": sid, "reason": why})
        refusal, correction = _check_builder(root, mailbox_rel, head, res,
                                             worktree_branches, own)
        if refusal:
            refused.append({"id": sid, **refusal})
            continue
        if correction:
            # git is the authority: the branch tip replaces the report.
            res["head"] = correction["actual"]
            res["commits"] = [correction["actual"]]
            corrected.append(correction)
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
    # `builders`: the ledger-owned pairs (what a fresh run may reclaim);
    # `unowned`: builders with commits whose ownership git did not prove —
    # never merged, removed or deleted by the driver (reported only).
    result = {"iteration": a.iteration, "wave": a.wave, "head": head,
              "accepted": accepted, "refused": refused, "merge": merge,
              "corrected": corrected, "builders": owned,
              "unowned": unowned}
    _record(mailbox, "builders", key, result)
    return result


def _dirty_entries(worktree: str) -> list[tuple[str, str]] | None:
    """``git status --porcelain -z`` as [(XY code, path)] (ignored files are
    not listed: they never block ``git worktree remove``).

    ``-z`` is required, not optional: porcelain v1's non-``-z`` text form
    quotes a path containing the literal " -> " sequence to disambiguate it
    from a rename record's own separator, and the old code split on that
    substring for *every* status code, unconditionally, after stripping only
    a leading/trailing quote. An untracked file legitimately named e.g.
    ``notes -> old.pyc`` was then misread as ``old.pyc`` (eval-native-v0c
    C1). With ``-z`` there is no quoting and no ``" -> "`` text at all: a
    plain entry is one NUL-terminated ``XY path`` record, and only a rename
    or copy (``R``/``C`` in either status column) carries a second
    NUL-terminated field, the *original* path, which this helper discards
    (only the current path matters to worktree removal and the fold check).
    """
    result = subprocess.run(
        ["git", "-C", worktree, "status", "--porcelain", "-z",
         "--untracked-files=all"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    tokens = result.stdout.split("\0")
    entries = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if not record:
            continue
        code, path = record[:2], record[3:]
        if code[0] in ("R", "C") or code[1] in ("R", "C"):
            i += 1  # the original path field; not needed here
        entries.append((code, path))
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


def _is_builder_branch(root: Path, trees: dict, branch: str) -> bool:
    """True when ``branch`` is a builder branch: a ``worktree-*`` name, or
    checked out in a worktree under ``<root>/.claude/worktrees/`` (checked
    by real path via ``_under_worktrees_marker``, so a symlinked worktree
    cannot spoof the marker)."""
    if branch.startswith("worktree-"):
        return True
    tree = trees.get(branch)
    path = tree.get("path") if tree else None
    return bool(path) and _under_worktrees_marker(root, path)


def _drop_superseded(mailbox: Path, root: Path, mailbox_rel: str | None,
                     head: str | None, spec: str, trees: dict) -> dict:
    """Drop one unmerged builder branch superseded by a re-dispatch.

    ``spec`` must be ``old=new`` (eval-native-v0c C3: the bare ``old`` form
    is refused — nothing then confirms ``old``'s work was actually
    superseded by anything, so a live ``worktree-*`` branch with committed,
    unmerged work named as the bare ``old`` used to be force-deleted
    unconditionally). ``new`` must itself be a builder branch (a
    ``worktree-*`` name, or a worktree under ``.claude/worktrees/`` by real
    path) that is merged into the Lead's HEAD — never ``master`` or the
    loop's target branch, which would otherwise "count as merged" (it is
    trivially an ancestor of everything built on it) and launder any branch
    named as ``old``, including one with commits not reachable from HEAD.
    ``old`` itself must also be a builder branch: a worktree under
    ``.claude/worktrees/`` (removed under ``_remove_worktree``'s dirt rule),
    or no worktree and a ``worktree-*`` name. The branch is then
    force-deleted (``git branch -D``): its commits are superseded by
    ``new``, not merged themselves.

    v01 fix (eval-v01 finding 4): both ``old`` and ``new`` must be builder
    branches the ownership ledger lists for THIS execution, and ``old``'s
    worktree (if any) must be the ledger's path. A branch a builder merely
    reported (another run's, or a user's) is never dropped.
    """
    old, sep, new = (x.strip() for x in spec.partition("="))
    entry: dict = {"branch": old, "superseded_by": new or None}
    if not sep or not new:
        return {**entry, "dropped": False,
                "reason": "drop_unmerged requires 'old=new': the bare "
                          "'old' form is refused"}
    exec_id = _current_exec_id(mailbox)
    old_own = _owned_branch(mailbox, root, old, exec_id=exec_id or "-")
    if old_own is None:
        return {**entry, "dropped": False,
                "reason": f"{old} is not a builder branch this run owns "
                          "(no ownership-ledger entry): never dropped"}
    tip = _branch_sha(root, old)
    if tip is None:
        _release_owned(mailbox, root, old_own, "branch already gone")
        return {**entry, "dropped": True, "note": "branch already gone"}
    if _owned_branch(mailbox, root, new, exec_id=exec_id or "-") is None:
        return {**entry, "dropped": False,
                "reason": f"{new} is not a builder branch this run owns "
                          "(no ownership-ledger entry)"}
    if not _is_builder_branch(root, trees, new):
        return {**entry, "dropped": False,
                "reason": f"{new} is not a builder branch (no worktree-* "
                          "name and no worktree under .claude/worktrees/)"}
    new_tip = _branch_sha(root, new)
    if (new_tip is None or head is None
            or not TL._git_is_ancestor(root, new_tip, head)):
        return {**entry, "dropped": False,
                "reason": f"re-dispatched branch {new} is not merged "
                          "into HEAD"}
    if not _is_builder_branch(root, trees, old):
        return {**entry, "dropped": False,
                "reason": "not a builder branch (no worktree-* name)"}
    tree = trees.get(old)
    path = tree.get("path") if tree else None
    if path:
        if Path(path).resolve() == root.resolve():
            return {**entry, "dropped": False,
                    "reason": "checked out in the repo"}
        if os.path.realpath(path) != os.path.realpath(str(old_own["path"])):
            return {**entry, "dropped": False, "worktree": path,
                    "reason": "checked out outside its ledger worktree"}
        why = _remove_worktree(root, path, mailbox_rel)
        if why:
            return {**entry, "dropped": False, "worktree": path,
                    "reason": why}
    deleted = TL._git(root, "branch", "-D", old)
    if deleted.returncode != 0:
        return {**entry, "dropped": False, "worktree": path,
                "reason": "git branch -D failed: "
                          + deleted.stderr.strip()[:200]}
    _release_owned(mailbox, root, old_own, f"superseded by {new}; dropped")
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
    dropped = [_drop_superseded(mailbox, root, mailbox_rel, head, d, trees)
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
        if path and not _under_worktrees_marker(root, path):
            # eval-native-v0c C2: cleanup never removes a worktree outside
            # the marker directory, merged or not — only `_drop_superseded`
            # had this check before.
            kept.append({"branch": branch, "worktree": path,
                        "reason": f"worktree {path} is not under "
                                  f"{root}/{WORKTREES_DIR}/"})
            continue
        own = _owned_branch(mailbox, root, branch)
        if own is None:
            # v01 fix: merged or not, only a ledger-owned builder branch of
            # this mailbox is ever removed by the driver.
            kept.append({"branch": branch, "worktree": path,
                         "reason": "not owned by this mailbox's runs (no "
                                   "ownership-ledger entry)"})
            continue
        if path and os.path.realpath(path) != os.path.realpath(
                str(own.get("path") or "")):
            kept.append({"branch": branch, "worktree": path,
                         "reason": "checked out outside its ledger worktree "
                                   f"{own.get('path')}"})
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
        _release_owned(mailbox, root, own, "merged; removed by cleanup")
        removed.append({"branch": branch, "worktree": path})
    return {"removed": removed, "kept": kept, "dropped": dropped}


def _remove_eval_worktrees(mailbox: Path, repo: Path, exec_id: str
                           ) -> tuple[list[str], list[dict], list[str]]:
    """``end``: remove the Evaluator pin worktrees this execution was
    assigned at ``pin`` (ledger ``eval-worktree``), each only when git lists
    a worktree at exactly that path (real path, not a symlink), detached at
    the pinned sha. Returns ``(removed, kept, left)``; ``left`` = other
    ``eval-*`` worktrees under ``.claude/worktrees/`` (another execution's,
    another mailbox's or a user's): reported, never touched (eval-v01
    finding 8)."""
    rel = TL._mailbox_rel(repo, mailbox)
    removed, kept = [], []
    trees = _worktrees(repo)
    handled: set[str] = set()
    for e in _owned(_ledger(mailbox, repo), "eval-worktree", exec_id=exec_id):
        path = str(e.get("path") or "")
        match = [t for t in trees if t.get("path") and os.path.realpath(
            t["path"]) == os.path.realpath(path)]
        if not match:
            continue  # never created (graded in place) or already gone
        handled.add(path)
        t = match[0]
        real = os.path.realpath(path)
        if (real != os.path.join(os.path.realpath(str(repo / WORKTREES_DIR)),
                                 os.path.basename(path))
                or not _real_dir(Path(path))):
            kept.append({"worktree": path, "reason": "not a real directory "
                         "at its assigned path"})
            continue
        if t.get("branch") or t.get("head") != e.get("sha"):
            kept.append({"worktree": path, "reason": "not detached at the "
                         f"pinned sha {str(e.get('sha'))[:12]}"})
            continue
        why = _remove_worktree(repo, path, rel, force_any=True)
        if why:
            kept.append({"worktree": path, "reason": why})
        else:
            _release_owned(mailbox, repo, e, "eval pin worktree removed at end")
            removed.append(path)
    marker = f"{repo}/{WORKTREES_DIR}/{EVAL_WORKTREE_PREFIX}"
    left = sorted(t["path"] for t in trees
                  if t.get("path", "").startswith(marker)
                  and t["path"] not in handled)
    return removed, kept, left


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


# ------------------------------------------ run scratch (v01 item 2)
#: Scratch names roles leave under ``.claude/worktrees/`` that are not git
#: worktrees (N0 subusage r1, vps-pool r2). Used only to REPORT such dirs
#: (``scratch_left``); what ``end`` removes comes from the ledger alone.
SCRATCH_RE = re.compile(r"^(?:eval-|tmp)[^/]*$")


def _worktrees_dir_entries(repo: Path | None) -> list[str] | None:
    """Names directly under ``<repo>/.claude/worktrees/`` (None when the
    directory is missing, a symlink or unreadable)."""
    if repo is None:
        return None
    marker = repo / WORKTREES_DIR
    try:
        if not stat.S_ISDIR(os.lstat(marker).st_mode):
            return None
        return sorted(os.listdir(marker))
    except OSError:
        return None


def _remove_exec_scratch(mailbox: Path, repo: Path | None, *,
                         exec_id: str | None = None,
                         exclude_exec: str | None = None
                         ) -> tuple[list[str], list[dict]]:
    """Remove the ledger-owned scratch dirs of ``exec_id`` (``end``) or of
    every execution but ``exclude_exec`` (``begin``: earlier executions of
    this mailbox, dead since this run holds the lock). Nothing matched by a
    name pattern alone is ever removed. Returns ``(removed, kept)``."""
    removed, kept = [], []
    if repo is None:
        return removed, kept
    for e in _owned(_ledger(mailbox, repo), "scratch"):
        if exec_id is not None and e.get("exec_id") != exec_id:
            continue
        if exclude_exec is not None and e.get("exec_id") == exclude_exec:
            continue
        path = str(e.get("path") or "")
        errors = _remove_owned_dir(repo, e)
        if errors:
            kept.append({"path": path, "reason": "; ".join(errors)[:400]})
            continue
        _release_owned(mailbox, repo, e, "scratch removed")
        removed.append(path)
    return removed, kept


def _scratch_left(repo: Path | None) -> list[str]:
    """``eval-*``/``tmp*`` directories still under ``.claude/worktrees/``
    that are not themselves git worktrees: reported, never removed by
    name."""
    if repo is None:
        return []
    marker = os.path.realpath(str(repo / WORKTREES_DIR))
    registered = [os.path.realpath(t["path"]) for t in _worktrees(repo)
                  if t.get("path")]
    out = []
    for name in _worktrees_dir_entries(repo) or []:
        path = repo / WORKTREES_DIR / name
        real = os.path.join(marker, name)
        if SCRATCH_RE.match(name) and _real_dir(path) and real not in registered:
            out.append(str(path))
    return out


# ----------------------------- previous run's builders (v01 item 3)
def _previous_builders(mailbox: Path, repo: Path, exec_id: str
                       ) -> list[dict]:
    """The builder branches earlier executions of this mailbox own: live
    ``builder`` entries of the ownership ledger (never a name pattern, a
    builder report, ``.native.json`` or ``.native-result.json`` — eval-v01
    findings 1 and 9)."""
    out: dict[str, dict] = {}
    for e in _owned(_ledger(mailbox, repo), "builder"):
        if e.get("exec_id") == exec_id:
            continue
        branch = str(e.get("branch") or "")
        if branch:
            out[branch] = e
    return list(out.values())


def _drop_builder(root: Path, mailbox_rel: str | None, branch: str,
                  path: str | None, *, merged: bool) -> str | None:
    """Remove a builder worktree (``_remove_worktree``'s dirt rule) and its
    branch (``-d`` when merged, else ``-D``). None, or why it was kept."""
    if path:
        why = _remove_worktree(root, path, mailbox_rel)
        if why:
            return why
    deleted = TL._git(root, "branch", "-d" if merged else "-D", branch)
    if deleted.returncode != 0:
        return "git branch failed: " + deleted.stderr.strip()[:200]
    return None


def _reclaim_builders(mailbox: Path, repo: Path | None) -> dict:
    """``begin`` of a fresh run: reuse or clean up the committed builder
    worktrees earlier runs of this mailbox left (v01 item 3).

    Candidates are ledger-owned builder branches only (``_previous_builders``);
    each must still be checked out, if at all, in its ledger worktree:

    * already merged into HEAD -> worktree removed, branch deleted;
    * STATE is ``lead-running`` of the branch's iteration (the pass will be
      re-planned), its commits descend from HEAD (as it was when ``begin``
      started), none commits ``loop/``, and the checkout has no staged or
      tracked changes outside the mailbox -> ``git merge --no-ff --no-edit``
      into HEAD, then removed; a conflicting merge (of ours) is aborted and
      the branch discarded;
    * otherwise -> discarded (worktree removed, branch force-deleted; the
      tip sha is logged).

    Nothing at all happens while the checkout has an in-progress merge,
    rebase, cherry-pick or revert (eval-v01 finding 5): every candidate is
    kept, and that operation is never aborted. A worktree with product dirt
    is never removed (kept, and still in ``dangling_worktrees``). Every
    action gets a ``| loop |`` LOG line.
    """
    result: dict = {"merged": [], "removed": [], "discarded": [], "kept": []}
    if repo is None:
        return result
    root = repo
    candidates = _previous_builders(mailbox, root, _current_exec_id(mailbox))
    if not candidates:
        return result
    trees = {t.get("branch"): t for t in _worktrees(root) if t.get("branch")}
    mailbox_rel = TL._mailbox_rel(root, mailbox)
    snap = _snapshot(_state(mailbox))
    head0 = TL._git_head(root)

    def note(kind: str, entry: dict, text: str) -> None:
        result[kind].append(entry)
        TL._append_log(mailbox, f"- iter {snap['iteration']} | loop | "
                                f"previous-run builder {text}")

    busy = _operation_in_progress(root)
    if busy:
        for c in candidates:
            result["kept"].append({
                "id": c.get("id") or "?", "branch": c.get("branch"),
                "tip": _branch_sha(root, str(c.get("branch"))),
                "worktree": c.get("path"),
                "reason": f"the checkout has an in-progress operation "
                          f"({busy}); nothing reclaimed"})
        TL._append_log(mailbox, f"- iter {snap['iteration']} | loop | "
                                f"previous-run builders kept: the checkout has "
                                f"an in-progress operation ({busy})")
        return result
    dirty = _dirty_entries(str(root))
    staged = TL._diff_paths(root, "--cached", "HEAD")
    mergeable_tree = (
        dirty is not None and staged is not None and not staged
        and not any(code != "??" and not TL._path_in_mailbox(p, mailbox_rel)
                    for code, p in dirty))

    for c in candidates:
        branch = str(c["branch"])
        cid = str(c.get("id") or "?")
        tip = _branch_sha(root, branch)
        if tip is None:
            _release_owned(mailbox, root, c, "branch gone before reclaim")
            continue
        if head0 is None:
            continue
        tree = trees.get(branch)
        path = tree.get("path") if tree else None
        entry = {"id": cid, "branch": branch, "tip": tip, "worktree": path}
        if path and (Path(path).resolve() == root.resolve()
                     or os.path.realpath(path)
                     != os.path.realpath(str(c.get("path") or ""))
                     or not _under_worktrees_marker(root, path)):
            result["kept"].append({**entry, "reason": "checked out outside "
                                   f"its ledger worktree {c.get('path')}"})
            continue
        if TL._git_is_ancestor(root, tip, head0):
            why = _drop_builder(root, mailbox_rel, branch, path, merged=True)
            if why:
                result["kept"].append({**entry, "reason": why})
            else:
                _release_owned(mailbox, root, c, "reclaimed: already merged")
                note("removed", entry, f"{branch}@{tip[:12]}: already merged; "
                                       "worktree removed")
            continue
        listed = TL._git(root, "rev-list", f"{head0}..{tip}").stdout.split()
        reusable = bool(
            snap["phase"] == "lead-running"
            and c.get("iteration") == snap["iteration"]
            and TL._git_is_ancestor(root, head0, tip) and listed
            and not any(TL._path_in_mailbox(p, mailbox_rel)
                        for sha in listed for p in TL._commit_paths(root, sha))
        )
        if reusable and not mergeable_tree:
            # Valid work, but the checkout has staged or tracked changes
            # outside the mailbox: never merged into (or discarded for) it.
            result["kept"].append({**entry, "reason": "valid, but the checkout "
                                   "has uncommitted tracked changes"})
            continue
        if reusable:
            merged = TL._git(root, "merge", "--no-ff", "--no-edit", "-q",
                             branch)
            if merged.returncode == 0:
                why = _drop_builder(root, mailbox_rel, branch, path,
                                    merged=True)
                if why:
                    entry["reason"] = why
                else:
                    _release_owned(mailbox, root, c, "reclaimed: merged")
                note("merged", entry, f"{branch}@{tip[:12]} ({cid}): "
                                      "merged into HEAD for re-planning")
                continue
            # No operation was in progress before this merge (checked
            # above), so a MERGE_HEAD now is this merge's own.
            in_merge = TL._git(root, "rev-parse", "-q", "--verify",
                               "MERGE_HEAD").returncode == 0
            if not in_merge:
                result["kept"].append({**entry, "reason": "merge refused: "
                                       + merged.stderr.strip()[:200]})
                continue
            TL._git(root, "merge", "--abort")
            entry["reason"] = "merge conflicted"
        why = _drop_builder(root, mailbox_rel, branch, path, merged=False)
        if why:
            result["kept"].append({**entry, "reason": why})
        else:
            _release_owned(mailbox, root, c, "reclaimed: discarded")
            note("discarded", entry,
                 f"{branch}@{tip[:12]} ({cid}): discarded "
                 f"({entry.get('reason') or 'not reusable'}; tip {tip})")
    return result


def op_end(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """Remove this execution's ledger-owned Evaluator pin worktrees and
    scratch dirs, then release the lock and write the session, result and
    registry records. Cleanup can never prevent the release or the records
    (eval-v01 finding 3): any failure is reported (``scratch_kept`` /
    ``eval_worktrees_kept``) and the dir stays in ``scratch_left``."""
    eval_removed, eval_kept, eval_left = [], [], []
    scratch_removed, scratch_kept, scratch_left = [], [], []
    lock_dir = mailbox / ".lock"
    ours = (lock_dir.is_dir() and _lock_owner(lock_dir) == _owner(a.token)
            and not _foreign_live_pid(lock_dir))
    try:
        exec_id = _current_exec_id(mailbox)
        if repo is not None and ours and exec_id:
            try:
                eval_removed, eval_kept, eval_left = _remove_eval_worktrees(
                    mailbox, repo, exec_id)
            except Exception as exc:  # noqa: BLE001 - reported
                eval_kept.append({"worktree": None,
                                  "reason": f"{type(exc).__name__}: {exc}"})
            try:
                scratch_removed, scratch_kept = _remove_exec_scratch(
                    mailbox, repo, exec_id=exec_id)
            except Exception as exc:  # noqa: BLE001 - reported
                scratch_kept.append({"path": None,
                                     "reason": f"{type(exc).__name__}: {exc}"})
        try:
            scratch_left = _scratch_left(repo)
        except Exception:  # noqa: BLE001 - a report only
            scratch_left = []
    finally:
        lock = _release(mailbox, a.token)
    snap = _snapshot(_state(mailbox))
    session = _read_json(mailbox / SESSION)
    # Only the lock owner closes the session record: `end` also runs after
    # a refused or garbled `begin` (N2), when another run may own both.
    if ours and session.get("session") == a.token and not session.get("done"):
        _write_session(mailbox, a.token, "done", done=True)
    dangling = _dangling_worktrees(repo)
    acc_summary = None
    if _acc_on(a):
        # r19: the dashboard's acceptance facts (additive keys; read-only).
        try:
            ctl = TL.AcceptanceController(mailbox, repo, None, {"enabled": True})
            # eval-r19n: the helper's sealed record when it verifies for
            # this execution; else the state file, marked unauthenticated.
            authenticated = False
            try:
                auth = _acc_auth(mailbox, repo, a)
                if ours:
                    # eval-r19n2 finding 1a: the sealed state over the
                    # state file, and the stop recorded, at every end.
                    _acc_seal_stop(mailbox, "end")
                ctl.state = _acc_unseal(auth)["state"]
                authenticated = True
            except (StepError, TL.AcceptanceError):
                pass
            acc_summary = _acc_summary(ctl)
            acc_summary["authenticated"] = authenticated
        except Exception as exc:  # noqa: BLE001 - a report only
            acc_summary = {"enabled": True, "error": f"{type(exc).__name__}: {exc}"[:300]}
    if ours:
        # A partial outcome for runs without launch.sh (an interactive
        # Workflow call); launch.sh replaces it with the full result.
        record = {
            "schema": 1, "source": "end", "driver": DRIVER,
            "run_token": a.token, "lock": lock,
            "dangling_worktrees": dangling,
            "state_status": snap["status"], "phase": snap["phase"],
            "iteration": snap["iteration"],
            "session_started_at": session.get("started_at"),
            "finished_at": _now_iso(),
        }
        if acc_summary is not None:
            record["acceptance"] = acc_summary
        try:
            _write_json(mailbox / RESULT, record)
        except OSError:
            pass
        _register(mailbox, own_token=a.token, state="ended",
                  ended_at=_now_iso(), lock=lock,
                  dangling_worktrees=dangling, acceptance=acc_summary)
    extra = {"acceptance": acc_summary} if acc_summary is not None else {}
    return {**extra, "lock": lock, "dangling_worktrees": dangling,
            "eval_worktrees_removed": eval_removed,
            "eval_worktrees_kept": eval_kept,
            "eval_worktrees_left": eval_left,
            "scratch_removed": scratch_removed,
            "scratch_kept": scratch_kept,
            "scratch_left": scratch_left, **snap}


# ------------------------------------------------------ frozen acceptance
# r19 N2 (DESIGN §5.2, §6.3; docs/FROZEN-ACCEPTANCE.md). Every hook below is
# reached only when the script passes ``--acceptance 1`` (``args.acceptance``);
# without it every op answers exactly as before. The loop semantics are the
# loop core's ``AcceptanceController`` (``metrics/trio_loop.py``) and
# ``metrics/trio-acceptance.py`` / ``trio-check.py``: this section only
# adapts them to one helper process per step.
#
# Trust: the helper is the driver. It exports, validates, audits, freezes,
# pins, restores and gates; no role ever runs these functions. The running
# Cursor-path driver keeps its pin and the checks' PATH in memory; here each
# op is a new process with no memory, so (eval-r19n) NOTHING read from a
# role-writable location -- the mailbox (``.native.json`` included), the
# driver state file, ``native-jobs/``, PLAN/QUEUE, worktrees -- relaxes a
# check or short-circuits a gate. What the helper carries between ops is
# either re-derived from trusted sources or authenticated to this run
# execution:
#
# * a per-execution key: HMAC(secret, exec_id), where ``begin`` writes a
#   fresh random secret only into ``<git-common-dir>/trio-native/<mailbox
#   key>/acc-secret`` (beside the ownership ledger, outside every worktree
#   and mailbox) and the exec id comes from the script's step nonce;
# * the sealed record ``acc-sealed.json`` there: the controller state plus
#   the interpreters ``begin`` resolved, HMAC'd with that key. Every op loads
#   the controller state from it (never from the state file, which is only
#   written, for trioctl/trio-shadow, and compared), and re-derives the
#   checks' PATH from those interpreters;
# * ``.native.json`` gate/apply records and detached-job results carry an
#   HMAC; one that does not verify stops the loop (``status: error``);
# * the script's digest (``--acc``: status, pin, pin commit, freeze commit,
#   base, counters; never a PATH) is its memory since ``begin``: the sealed
#   record, the state file and the pin chain git derives
#   (``derive_pin_chain``) must all agree with it at every op, from ``begin``
#   on (``authoring`` until this execution's own freeze), else NEEDS_HUMAN
#   ``acceptance-state-mismatch``; counters take the largest value.
#
# eval-r19n2: the sealed record is also the only source of truth ACROSS
# executions: every acceptance stop and ``end`` write it over the state file
# and record the stop (``_acc_seal_stop``); ``begin`` authenticates the
# previous record with the secret it was sealed under before rotating
# (``_acc_prev_sealed``) and resumes from it with the pin chain derived from
# its own commits (``_acc_resume_sealed``: a pin it does not hold is
# restored, NEEDS_HUMAN ``acceptance-tamper``); trioctl's ``amend --human``
# writes a human adoption into it (``cli_human_amend``); a sequence number
# the script holds refuses an older record; git runs with
# ``GIT_NO_REPLACE_OBJECTS=1``.
#
# Same-uid limit (documented): roles run as the same user and are told not
# to touch the git common dir; a process that WRITES the git common dir can
# forge records for this execution (it can read or replace ``acc-secret``,
# or remove the sealed record so the next ``begin`` is a first run), as it
# could forge the Cursor driver's state file. The secret rotates at every
# ``begin``.
ACC_OPS = ("acceptance-export", "acceptance-freeze", "coverage", "acceptance-run")
#: Keys of the digest the script hands back (``--acc``). No PATH: the
#: checks' PATH is re-derived from the sealed record at every op.
ACC_DIGEST_KEYS = ("status", "pin", "pin_commit", "freeze_commit", "base",
                   "tamper_events", "amendments", "seq")
ACC_SECRET = "acc-secret"
#: eval-r19n2: the previous secret, kept only while ``begin`` re-seals the
#: previous execution's record under the new one (a crash in between).
ACC_SECRET_PREV = "acc-secret.prev"
ACC_SEALED = "acc-sealed.json"
_ACC_MAC_DOMAIN = b"trio-native-acceptance-exec-v1\0"
#: Long ops (a pack run at base, amendment re-runs, the SHIP gate) run in a
#: detached job so no step exceeds the step agent's 600 s Bash ceiling: the
#: op waits up to this long, then answers ``pending`` and the script polls.
JOB_WAIT_ENV = "TRIO_NATIVE_JOB_WAIT_S"
JOB_MODE_ENV = "TRIO_NATIVE_JOBS"  # "inline" runs the job in the op itself
DEFAULT_JOB_WAIT_S = 480.0
AUTHOR_MARK = "ACCEPTANCE-AUTHOR-RUN:"
_ACC_MARKER_RE = re.compile(r"[0-9A-Za-z._-]{1,120}")
_BINDING_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ACC_ID_RE = re.compile(r"^ACC-[0-9]{1,4}$")
#: Tools whose omitted ``path`` means "the session cwd" (the loop repo).
_CWD_TOOLS = frozenset({"Grep", "Glob", "LS"})


def _acc_on(a: argparse.Namespace) -> bool:
    return bool(getattr(a, "acceptance", 0))


def _acc_tool() -> Path:
    return METRICS_DIR / "trio-acceptance.py"


def _acc_ta():
    module = TL._load_sibling("trio_native_acceptance", "trio-acceptance.py")
    if module is None:
        raise TL.AcceptanceError("acceptance-unavailable",
                                 f"{_acc_tool()} is missing (refresh metrics/ as a set)")
    return module


def _acc_json(a: argparse.Namespace, name: str, kind=dict):
    raw = getattr(a, name, None)
    if raw in (None, ""):
        return kind()
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise StepError(f"--{name} is not JSON ({exc})") from exc
    if not isinstance(value, kind):
        raise StepError(f"--{name} must be a JSON {kind.__name__}")
    return value


def _acc_tier_problem(models: dict) -> str | None:
    """N1: the author runs on the Lead/Evaluator tier, never cheaper
    (DESIGN §5.2; the Cursor doctor's rule: lead, evaluator and acceptance
    resolve to one model)."""
    lead, evaluator = models.get("lead"), models.get("evaluator")
    author = models.get("acceptance") or evaluator
    if not all(isinstance(m, str) and m for m in (lead, evaluator, author)):
        return "acceptance: models.lead and models.evaluator are required"
    if len({lead, evaluator, author}) != 1:
        return ("acceptance author must run on the Lead/Evaluator tier (lead "
                f"{lead}, evaluator {evaluator}, acceptance {author}); refused at begin")
    return None


def _acc_same_pin_descendant(ctl, old: str, new: str, pin: str) -> bool:
    """The state's pin commit moved past the script's by driver commits
    that keep the pin (a restore): harmless, the pinned pack is the same."""
    if not old or not new:
        return False
    if TL._git(ctl.repo, "merge-base", "--is-ancestor", old, new).returncode != 0:
        return False
    if new not in (ctl.state.get("driver_commits") or []):
        return False
    return ctl.ta.pack_hash_at(ctl.repo, new, ctl.acc_rel) == pin


# ------------------------------------------- execution authentication
class _Fenced(BaseException):
    """A detached job whose execution no longer holds the mailbox."""


#: (mailbox, token, exec_id) inside a detached job (``_run_job``), else None.
_JOB_FENCE: tuple | None = None


def _fence() -> None:
    """In a detached job: stop before any write once this execution no
    longer holds the lock (finding 5). A no-op in the op process itself."""
    if _JOB_FENCE is None:
        return
    mailbox, token, exec_id = _JOB_FENCE
    lock = mailbox / ".lock"
    if (not lock.is_dir() or _lock_owner(lock) != _owner(token)
            or _current_exec_id(mailbox) != exec_id):
        raise _Fenced(f"execution {exec_id[:12]} no longer holds {mailbox}")


def _canon(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()


def _plain(value):
    """The JSON value a signature covers (a round trip: tuples -> lists)."""
    return json.loads(json.dumps(value, sort_keys=True))


class _Auth:
    """This run execution's MAC key (never written anywhere)."""

    def __init__(self, home: Path, exec_id: str, key: bytes) -> None:
        self.home, self.exec_id, self._key = home, exec_id, key
        #: the sealed record's sequence number (eval-r19n2 finding 3):
        #: every seal writes the next one; an older record is refused.
        self.seq = 0

    def mac(self, kind: str, name: str, payload) -> str:
        return hmac.new(self._key, _canon({"kind": kind, "name": name, "exec": self.exec_id,
                                           "payload": payload}), hashlib.sha256).hexdigest()

    def sign(self, kind: str, name: str, payload) -> dict:
        return {"exec": self.exec_id, "mac": self.mac(kind, name, payload)}

    def check(self, kind: str, name: str, payload, auth) -> bool:
        return (isinstance(auth, dict) and auth.get("exec") == self.exec_id
                and isinstance(auth.get("mac"), str)
                and hmac.compare_digest(auth["mac"], self.mac(kind, name, payload)))


_AUTHS: dict = {}
#: mailbox -> (this execution's _Auth, repo): what a stop seals (finding 1a).
_ACC_CUR: dict = {}


def _nonce_exec(a: argparse.Namespace) -> str:
    """The run-execution id the script put in its step nonce
    (``<token>/<exec_id>/<seq>/<op>``): the script's memory, not the
    mailbox's ``.session.json``."""
    parts = str(getattr(a, "nonce", "") or "").split("/")
    if len(parts) >= 4 and parts[0] == a.token and _EXEC_ID_RE.fullmatch(parts[1]):
        return parts[1]
    return ""


def _acc_owned_dir(path: Path) -> bool:
    """A real directory (lstat: never a symlink) owned by this user."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _acc_home(mailbox: Path, repo: Path | None, create: bool = False) -> Path:
    """``<git-common-dir>/trio-native/<mailbox key>/``. Both levels must be
    real directories owned by this user (eval-r19n2 finding 3: lstat, so a
    symlinked or foreign ``trio-native/`` is refused, not followed)."""
    path = _ledger_path(mailbox, repo)
    if path is None:
        raise TL.AcceptanceError("acceptance-unavailable",
                                 "no git common dir for the helper's sealed acceptance records")
    home = path.parent
    for d in (home.parent, home):
        if create:
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
        if not _acc_owned_dir(d):
            raise TL.AcceptanceNeedsHuman(
                "acceptance-state-mismatch",
                f"{d} is missing, a symlink, not a directory or not owned by this user")
    return home


def _acc_private_bytes(path: Path) -> bytes | None:
    """A regular file of this user's, opened without following a symlink
    (None otherwise)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)


def _acc_private_json(path: Path) -> dict:
    raw = _acc_private_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _acc_write_private(path: Path, data: bytes) -> None:
    """Atomic 0600 replace in the helper's home; a symlink or non-file at
    *path* is refused (never written through)."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError(f"{path} is a symlink or not a regular file; refusing it")
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _acc_secret_at(path: Path) -> str:
    raw = (_acc_private_bytes(path) or b"").decode("ascii", "replace").strip()
    return raw if re.fullmatch(r"[0-9a-f]{64}", raw) else ""


def _acc_key(secret: str, exec_id: str) -> bytes:
    return hmac.new(bytes.fromhex(secret), _ACC_MAC_DOMAIN + exec_id.encode(),
                    hashlib.sha256).digest()


def _acc_sealed_auth(home: Path, names=(ACC_SECRET,)) -> tuple[_Auth, str] | None:
    """The key the sealed record was sealed under: its own exec id (inside
    the MAC) with the first of *names* whose secret verifies it."""
    raw = _acc_private_json(home / ACC_SEALED)
    blob = raw.get("_auth") if isinstance(raw.get("_auth"), dict) else {}
    exec_id = str(blob.get("exec") or "")
    if not _EXEC_ID_RE.fullmatch(exec_id):
        return None
    for name in names:
        secret = _acc_secret_at(home / name)
        if not secret:
            continue
        auth = _Auth(home, exec_id, _acc_key(secret, exec_id))
        try:
            _acc_unseal(auth)
        except TL.AcceptanceError:
            continue
        return auth, secret
    return None


def _acc_prev_sealed(mailbox: Path, repo: Path | None) -> dict | None:
    """``begin`` (eval-r19n2 finding 1b): the previous execution's sealed
    record, authenticated with the secret it was sealed under BEFORE the
    secret rotates. None when there is none (a first run: git alone, as
    before). One that exists but does not verify needs a human."""
    home = _acc_home(mailbox, repo, create=True)
    path = home / ACC_SEALED
    if not os.path.lexists(path):
        return None
    found = _acc_sealed_auth(home, (ACC_SECRET, ACC_SECRET_PREV))
    if found is None:
        raise TL.AcceptanceNeedsHuman(
            "acceptance-state-mismatch",
            f"the helper's sealed acceptance record {path} from an earlier run does not verify "
            f"with the secret it was sealed under (edited, replaced, or the secret was); a human "
            "reviews the pack's history, then moves the record away to re-derive the pin from "
            "git (as a first run)")
    auth, secret = found
    payload = _acc_unseal(auth)
    payload["secret"] = secret
    return payload


def _acc_auth_begin(mailbox: Path, repo: Path | None, exec_id: str,
                    prev: dict | None = None) -> _Auth:
    """``begin``: a fresh random secret for this execution (the previous
    execution's records and jobs can no longer be authenticated). The
    previous execution's sealed record (*prev*, already authenticated) is
    re-sealed under the new key at once, so the helper's record survives
    the rotation (the old secret is kept beside it until then)."""
    home = _acc_home(mailbox, repo, create=True)
    if prev is not None:
        _acc_write_private(home / ACC_SECRET_PREV, (prev["secret"] + "\n").encode())
    secret = secrets.token_hex(32)
    _acc_write_private(home / ACC_SECRET, (secret + "\n").encode())
    auth = _Auth(home, exec_id, _acc_key(secret, exec_id))
    _AUTHS[(str(mailbox), exec_id)] = auth
    _ACC_CUR[str(mailbox)] = (auth, repo)
    if prev is not None:
        auth.seq = int(prev.get("seq") or 0)
        _acc_seal(auth, prev["state"], prev["interpreters"])
    try:
        os.unlink(home / ACC_SECRET_PREV)
    except FileNotFoundError:
        pass
    return auth


def _acc_auth(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> _Auth:
    exec_id = _nonce_exec(a)
    if not exec_id:
        raise StepError("acceptance: the step nonce carries no run-execution id "
                        "(<token>/<exec_id>/<seq>/<op>, minted by begin)")
    cached = _AUTHS.get((str(mailbox), exec_id))
    if cached is not None:
        _ACC_CUR[str(mailbox)] = (cached, repo)
        return cached
    if _current_exec_id(mailbox) != exec_id:
        raise StepError(f"acceptance: run execution {exec_id[:12]} of this step is not the "
                        "mailbox's current execution (another run owns it)")
    home = _acc_home(mailbox, repo)
    raw = _acc_secret_at(home / ACC_SECRET)
    if not raw:
        raise TL.AcceptanceNeedsHuman(
            "acceptance-state-mismatch",
            f"the helper's execution secret {home / ACC_SECRET} is missing, malformed, a "
            "symlink or not this user's")
    auth = _Auth(home, exec_id, _acc_key(raw, exec_id))
    _AUTHS[(str(mailbox), exec_id)] = auth
    _ACC_CUR[str(mailbox)] = (auth, repo)
    return auth


def _acc_auth_or_stop(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> _Auth:
    try:
        return _acc_auth(mailbox, repo, a)
    except TL.AcceptanceError as exc:
        stop = _acc_stop(mailbox, _acc_iteration(mailbox, a), exc)
        raise StepError(f"acceptance stopped the loop ({stop['reason']}): "
                        f"{stop['detail'][:300]}") from exc


def _acc_forged(mailbox: Path, what: str) -> StepError:
    """A cached answer this execution's helper did not write: the loop
    stops (``status: error``); it is never returned."""
    _fence()
    try:
        iteration = TL._number(_state(mailbox)["iteration"])
    except Exception:  # noqa: BLE001 - logging only
        iteration = 0
    TL._update_state(mailbox / "STATE.md", {"status": "error", "phase": "error"})
    TL._append_log(mailbox, f"- iter {iteration} | loop | acceptance stopped the loop "
                            f"(acceptance-record-forged): {what} is not authenticated to "
                            "this run execution")
    _acc_seal_stop(mailbox, "error: acceptance-record-forged")
    return StepError(f"acceptance-record-forged: {what} is not authenticated to this run "
                     "execution (written or edited outside the helper); the loop stops")


def _acc_record(mailbox: Path, auth: _Auth, kind: str, key: str, result: dict) -> None:
    stored = _plain({k: v for k, v in result.items() if k != "nonce"})
    stored["_auth"] = auth.sign("record", f"{kind}:{key}", stored)
    data = _records(mailbox)
    data[kind][key] = stored
    _write_json(mailbox / RECORDS, data)


def _acc_recorded(mailbox: Path, auth: _Auth, kind: str, key: str) -> dict | None:
    value = _records(mailbox)[kind].get(key)
    if value is None:
        return None
    body = {k: v for k, v in value.items() if k != "_auth"} if isinstance(value, dict) else None
    if body is None or not auth.check("record", f"{kind}:{key}", body, value.get("_auth")):
        raise _acc_forged(mailbox, f"{RECORDS} {kind} record {key}")
    return dict(body)


def _acc_interpreters(ta) -> dict:
    """``begin``: the interpreters the checks may use, resolved once on the
    helper's own PATH (eval-r19d finding 4), sealed for every later op."""
    env_path = os.environ.get("PATH", "")
    out = {}
    for name in ta.PATH_INTERPRETERS:
        found = shutil.which(name, path=env_path)
        if found:
            out[name] = os.path.abspath(found)
    return out


def _acc_check_path(ta, interpreters: dict) -> str:
    """The checks' PATH from the sealed interpreters (``check_path``'s rule:
    each interpreter's directory, then the system directories)."""
    dirs: list[str] = []
    for name in ta.PATH_INTERPRETERS:
        found = interpreters.get(name)
        if (isinstance(found, str) and found.startswith("/") and found.isprintable()
                and os.pathsep not in found):
            d = os.path.dirname(found)
            if d not in dirs:
                dirs.append(d)
    for d in ta.SYSTEM_PATH_DIRS:
        if d not in dirs and os.path.isdir(d):
            dirs.append(d)
    return os.pathsep.join(dirs)


def _acc_seal(auth: _Auth, state: dict, interpreters: dict) -> None:
    """Seal *state* as the next record (``seq`` + 1, finding 3)."""
    auth.seq += 1
    payload = _plain({"state": state, "interpreters": interpreters, "seq": auth.seq})
    body = {**payload, "_auth": auth.sign("sealed", "state", payload)}
    _acc_write_private(auth.home / ACC_SEALED,
                       (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _acc_unseal(auth: _Auth) -> dict:
    """The sealed record ``{state, interpreters, seq}`` when it verifies for
    *auth*'s execution (a record without ``seq`` is b9fb9e5's: seq 0)."""
    path = auth.home / ACC_SEALED
    raw = _acc_private_json(path)
    payload = {"state": raw.get("state"), "interpreters": raw.get("interpreters")}
    if "seq" in raw:
        payload["seq"] = raw.get("seq")
    seq = payload.get("seq", 0)
    if (not isinstance(payload["state"], dict) or not isinstance(payload["interpreters"], dict)
            or not isinstance(seq, int) or isinstance(seq, bool)
            or not auth.check("sealed", "state", payload, raw.get("_auth"))):
        raise TL.AcceptanceNeedsHuman(
            "acceptance-state-mismatch",
            f"the helper's sealed acceptance record {path} is missing or not authentic "
            "for this run execution")
    auth.seq = max(auth.seq, seq)
    return {**payload, "seq": seq}


def _acc_chain_record(state: dict) -> dict:
    """The sealed record's own pack commits, for ``derive_pin_chain``
    (eval-r19n2 finding 1b): a freeze/pin/restore it did not make, or a
    human adoption it does not hold, is never a legitimate pin."""
    return {"driver_commits": [str(c) for c in state.get("driver_commits") or []],
            "human_amends": [str(c) for c in state.get("human_amends") or []]}


def _acc_seal_stop(mailbox: Path, why: str) -> bool:
    """eval-r19n2 finding 1a (Cursor parity: the driver saves its in-memory
    state at every stop): at every acceptance stop and at ``end`` the
    helper's sealed state is written over the role-writable driver state
    file, so nothing a role wrote there survives the stop, and the stop is
    recorded in the sealed record (``native.stopped``: trioctl's native
    ``amend --human`` path runs only then). False when this process holds
    no authentic record (nothing is written)."""
    cur = _ACC_CUR.get(str(mailbox))
    if cur is None:
        return False
    auth, repo = cur
    _fence()
    try:
        payload = _acc_unseal(auth)
    except TL.AcceptanceError:
        return False
    st = payload["state"]
    _acc_native_of(st)["stopped"] = {"reason": str(why)[:300], "exec": auth.exec_id,
                                     "utc": _now_iso()}
    _acc_seal(auth, st, payload["interpreters"])
    try:
        ctl = TL.AcceptanceController(mailbox, repo, None, {"enabled": True})
        ctl.ta.save_state(ctl.state_path, st)
    except (TL.AcceptanceError, OSError):
        return False
    return True


def _acc_native_of(state: dict) -> dict:
    native = state.get("native")
    if not isinstance(native, dict):
        native = state["native"] = {}
    return native


def _acc_sealed_native(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """The sealed ``native`` facts (report digests, gate failures); {} when
    the record cannot be read (the op's own controller then stops)."""
    try:
        return dict(_acc_unseal(_acc_auth(mailbox, repo, a))["state"].get("native") or {})
    except (StepError, TL.AcceptanceError):
        return {}


def _acc_sealed_update(mailbox: Path, repo: Path | None, a: argparse.Namespace, fn) -> bool:
    try:
        auth = _acc_auth(mailbox, repo, a)
        payload = _acc_unseal(auth)
    except (StepError, TL.AcceptanceError):
        return False
    fn(payload["state"])
    _acc_seal(auth, payload["state"], payload["interpreters"])
    return True


def _acc_bind(ctl, auth: _Auth, interpreters: dict) -> None:
    """Make *ctl* this execution's controller: the checks' PATH from the
    sealed interpreters, every save sealed (and fenced in a job)."""
    ctl.check_path = _acc_check_path(ctl.ta, interpreters)
    ctl.state["check_path"] = ctl.check_path  # the record only; never read back
    ctl.native_auth = auth
    ctl.native_interpreters = interpreters
    _ACC_CUR[str(ctl.mailbox)] = (auth, ctl.repo)
    file_save, file_log = ctl._save, ctl._log

    def save() -> None:
        _fence()
        ctl.state["check_path"] = ctl.check_path
        file_save()
        _acc_seal(auth, ctl.state, interpreters)

    def log(iteration: int, text: str) -> None:
        _fence()
        file_log(iteration, text)
    ctl._save = save
    ctl._log = log


def _acc_mismatch(detail: str):
    return TL.AcceptanceNeedsHuman("acceptance-state-mismatch", detail)


def _acc_verify(ctl, held: dict, on_file: dict, *, replay_freeze: bool = False) -> None:
    """The sealed record against the script's digest, the state file and
    git (finding 2 and 3). Raises NEEDS_HUMAN ``acceptance-state-mismatch``."""
    st = ctl.state
    frozen = ctl.frozen()
    exec_id = ctl.native_auth.exec_id
    where = f"(state file {ctl.state_path})"
    h_pin = held.get("pin") or None
    # 1. the script's memory
    if held.get("status") == "frozen" or h_pin:
        if not frozen or st.get("pin") != h_pin:
            raise _acc_mismatch(f"this run's pin {str(h_pin)[:12]} is not the helper's sealed "
                                f"pin {str(st.get('pin'))[:12]} (status {st.get('status')})")
        if held.get("freeze_commit") and held.get("freeze_commit") != st.get("freeze_commit"):
            raise _acc_mismatch(f"this run's freeze {str(held.get('freeze_commit'))[:12]} is not "
                                f"the sealed freeze {str(st.get('freeze_commit'))[:12]}")
        want, have = str(held.get("pin_commit") or ""), str(st.get("pin_commit") or "")
        if have != want and not _acc_same_pin_descendant(ctl, want, have, h_pin):
            raise _acc_mismatch(f"the sealed pin commit {have[:12]} is not this run's "
                                f"{want[:12]}")
    elif frozen and not (replay_freeze and _acc_native(ctl).get("freeze_exec") == exec_id):
        raise _acc_mismatch("the sealed record is frozen but this run is still authoring "
                            "(no freeze by this execution)")
    if held.get("base") and held.get("base") != st.get("base"):
        raise _acc_mismatch(f"this run's author base {str(held.get('base'))[:12]} is not the "
                            f"sealed base {str(st.get('base'))[:12]}")
    # 2. the driver state file (same-uid writable): it may only add strictness
    for key in ("status", "pin", "pin_commit", "freeze_commit"):
        if (on_file.get(key) or None) != (st.get(key) or None):
            raise _acc_mismatch(f"the driver state file's {key} {str(on_file.get(key))[:16]} is "
                                f"not the helper's {str(st.get(key))[:16]} {where}: it changed "
                                "while the workflow ran")
    for key in ("tamper_events", "amendments"):
        values = [st.get(key), held.get(key), on_file.get(key)]
        try:
            st[key] = max(int(v or 0) for v in values)
        except (TypeError, ValueError):
            st[key] = int(st.get(key, 0) or 0)
    # 3. git
    if frozen:
        start = str(st.get("run_head") or "") or None
        if start and TL._git(ctl.repo, "merge-base", "--is-ancestor", start,
                             "HEAD").returncode != 0:
            raise _acc_mismatch(f"the sealed run head {start[:12]} is not an ancestor of HEAD")
        # eval-r19n2 finding 1b: with the sealed record's own commits, so a
        # pin it never made is tamper (``check_pin`` restores it), never
        # the chain's pin.
        chain = ctl.ta.derive_pin_chain(ctl.repo, ctl.acc_rel, start, verify=False,
                                        **_acc_chain_record(st))
        if chain["freeze_commit"] != st.get("freeze_commit") or chain["pin"] != st.get("pin"):
            raise _acc_mismatch(
                f"the pin chain git derives (freeze {str(chain['freeze_commit'])[:12]}, pin "
                f"{str(chain['pin'])[:12]}) is not the helper's (freeze "
                f"{str(st.get('freeze_commit'))[:12]}, pin {str(st.get('pin'))[:12]})"
                + (f": {'; '.join(chain['problems'][:2])[:300]}" if chain["problems"] else ""))
    elif ctl._has_pack():
        raise _acc_mismatch(f"{ctl.acc_rel}/ holds a pack although this execution never froze "
                            "one (written or committed outside the helper's freeze): tamper")


def _acc_held(a: argparse.Namespace) -> dict:
    if getattr(a, "acc", None) in (None, ""):
        raise StepError("acceptance: --acc (the digest the script holds since begin) is required")
    return _acc_json(a, "acc")


def _acc_controller(mailbox: Path, repo: Path | None, a: argparse.Namespace, *,
                    replay_freeze: bool = False):
    """This op's ``AcceptanceController``: its state is the helper's sealed
    record (never the state file), verified against the script's digest
    (``--acc``), the state file and git (``_acc_verify``)."""
    auth = _acc_auth(mailbox, repo, a)
    held = _acc_held(a)
    sealed = _acc_unseal(auth)
    h_seq = held.get("seq")
    if isinstance(h_seq, int) and not isinstance(h_seq, bool) and sealed["seq"] < h_seq:
        # eval-r19n2 finding 3: an older authentic record of this execution
        # put back (it would reset gate failures, anti-thrash, digests).
        raise _acc_mismatch(f"the helper's sealed record (seq {sealed['seq']}) is older than "
                            f"the one this run holds (seq {h_seq}): an earlier record was put back")
    ctl = TL.AcceptanceController(mailbox, repo, None, {"enabled": True})
    on_file = ctl.state
    ctl.state = copy.deepcopy(sealed["state"])
    ctl.chain_record = _acc_chain_record(ctl.state)
    _acc_bind(ctl, auth, sealed["interpreters"])
    _acc_verify(ctl, held, on_file, replay_freeze=replay_freeze)
    return ctl


def _acc_digest(ctl) -> dict:
    st = ctl.state
    return {
        "status": st.get("status"), "pin": st.get("pin"),
        "pin_commit": st.get("pin_commit"), "freeze_commit": st.get("freeze_commit"),
        "base": st.get("base"),
        "tamper_events": int(st.get("tamper_events", 0) or 0),
        "amendments": int(st.get("amendments", 0) or 0),
        "checks": st.get("checks"),
        "seq": int(getattr(getattr(ctl, "native_auth", None), "seq", 0) or 0),
        "state_file": str(ctl.state_path), "tool": str(_acc_tool()),
    }


def _acc_fresh_digest(mailbox: Path, repo: Path | None, a: argparse.Namespace,
                      iteration: int) -> dict:
    try:
        return _acc_digest(_acc_controller(mailbox, repo, a))
    except TL.AcceptanceError as exc:
        return {"stop": _acc_stop(mailbox, iteration, exc)}


def _acc_audit_summary(st: dict) -> dict | None:
    audits = [x for x in st.get("audits") or [] if isinstance(x, dict)]
    if not audits:
        return None
    last = audits[-1]
    return {"limited": bool(last.get("limited")), "contaminated": bool(last.get("contaminated")),
            "attempts": len(audits), "transcripts": len(last.get("transcripts") or []),
            "path": last.get("path") or "native"}


def _acc_summary(ctl) -> dict:
    """Dashboard record (``.native-result.json`` / registry): additive."""
    st = ctl.state
    native = st.get("native") if isinstance(st.get("native"), dict) else {}
    return {
        "enabled": True, "status": st.get("status"), "pin": st.get("pin"),
        "freeze_commit": st.get("freeze_commit"), "pin_commit": st.get("pin_commit"),
        "checks": st.get("checks"), "dropped": len(st.get("dropped") or []),
        "amendments": int(st.get("amendments", 0) or 0),
        "tamper_events": int(st.get("tamper_events", 0) or 0),
        "last_prerun": native.get("last_prerun"), "ship_gate": native.get("ship_gate"),
        "refusals": native.get("refusals") or {},
        # finding 6: whether the author audit read the author's transcript
        # (``limited: false``) or fell back to the limited audit.
        "audit": _acc_audit_summary(st),
    }


def _acc_native(ctl) -> dict:
    native = ctl.state.get("native")
    if not isinstance(native, dict):
        native = ctl.state["native"] = {}
    return native


def _acc_count(ctl, what: str) -> None:
    refusals = _acc_native(ctl).setdefault("refusals", {})
    refusals[what] = int(refusals.get(what, 0) or 0) + 1


def _acc_stop(mailbox: Path, iteration: int, exc) -> dict:
    """``_acceptance_stop`` for one helper op (lockstep form: NEEDS_HUMAN
    records its reason as the phase, an error sets ``phase: error``)."""
    _fence()
    state_path = mailbox / "STATE.md"
    if isinstance(exc, TL.AcceptanceNeedsHuman):
        TL._update_state(state_path, {"status": "needs_human", "phase": exc.reason})
        status, code = "needs_human", 5
    else:
        TL._update_state(state_path, {"status": "error", "phase": "error"})
        status, code = "error", 3
    TL._append_log(mailbox, f"- iter {iteration} | loop | acceptance stopped the loop "
                            f"({exc.reason}): {exc.detail}")
    _acc_seal_stop(mailbox, f"{status}: {exc.reason}")
    return {"status": status, "code": code, "reason": exc.reason,
            "detail": str(exc.detail)[:600]}


# ------------------------------------------------------------ long jobs
def _job_wait() -> float:
    try:
        return max(0.0, float(os.environ.get(JOB_WAIT_ENV, "")))
    except ValueError:
        return DEFAULT_JOB_WAIT_S


def _job_body(fn) -> dict:
    try:
        return {"ok": True, "body": fn()}
    except StepError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - reported like main() does
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _job_signed(auth: _Auth, key: str, data: dict) -> dict:
    data = _plain(data)
    payload = {k: data.get(k) for k in ("ok", "body", "error")}
    return {**data, "_auth": auth.sign("job", key, payload)}


def _job_take(mailbox: Path, auth: _Auth, key: str, res: Path, pidf: Path,
              data: dict, check=None) -> dict:
    """A finished job: its body, or its error (never cached: a failed job
    runs again on the next call). Only a result this execution's job signed
    is taken (eval-r19n findings 1 and 4); *check* may refuse a body that no
    longer fits the repository (the apply's landed tree)."""
    payload = {k: data.get(k) for k in ("ok", "body", "error")}
    if not auth.check("job", key, payload, data.get("_auth")):
        raise _acc_forged(mailbox, f"the detached job result {res}")
    if data.get("ok"):
        why = check(data["body"]) if check is not None else None
        if why:
            raise StepError(f"{why}; the job's answer is not returned")
        return data["body"]
    for path in (res, pidf):
        try:
            path.unlink()
        except OSError:
            pass
    raise StepError(str(data.get("error") or "job failed"))


def _job_pid(pidf: Path) -> int:
    raw = _read_bytes(pidf)
    try:
        return int((raw or b"").decode().strip() or 0)
    except ValueError:
        return 0


def _jobs_dir(mailbox: Path, repo: Path | None, exec_id: str) -> Path:
    return _acc_ta().state_file(repo or mailbox, mailbox).parent / "native-jobs" / exec_id


def _run_job(mailbox: Path, repo: Path | None, a: argparse.Namespace, key: str, fn,
             check=None) -> dict:
    """Run *fn* (an op body) as a detached job keyed by *key*.

    A finished job's body is returned (and kept: a retried step gets the
    same answer, so the job never runs twice). A running one is waited for
    up to ``TRIO_NATIVE_JOB_WAIT_S`` (480 s); after that the op answers
    ``{"pending": true}`` and the script re-runs the same op to poll.

    eval-r19n: job files live per run execution (``native-jobs/<exec_id>/``
    beside the driver state); the job signs its result with this
    execution's key, which it holds in memory (inherited over ``fork``, never
    through a file), and the result is verified on take. The job is fenced
    to its execution: once another execution (or run) holds the mailbox it
    stops before its next write and writes no result."""
    auth = _acc_auth_or_stop(mailbox, repo, a)
    jobs = _jobs_dir(mailbox, repo, auth.exec_id)
    jobs.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:120]
    res, pidf = jobs / f"{safe}.json", jobs / f"{safe}.pid"
    if res.exists():
        return _job_take(mailbox, auth, key, res, pidf, _read_json(res), check)
    if os.environ.get(JOB_MODE_ENV, "") == "inline" or not hasattr(os, "fork"):
        data = _job_signed(auth, key, _job_body(fn))
        if data.get("ok"):
            _write_json(res, data)
        return _job_take(mailbox, auth, key, res, pidf, data, check)
    pid = _job_pid(pidf)
    child = 0
    if not pid or not TL._pid_alive(pid):
        try:
            pidf.unlink()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(str(pidf), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise StepError(f"job {key} is being started by another step") from exc
        sys.stdout.flush()
        sys.stderr.flush()
        child = os.fork()
        if child == 0:  # the job: detached, no stdio, result file only
            try:
                os.close(fd)
                os.setsid()
                null = os.open(os.devnull, os.O_RDWR)
                for n in (0, 1, 2):
                    os.dup2(null, n)
                global _JOB_FENCE
                _JOB_FENCE = (mailbox, a.token, auth.exec_id)
                try:
                    _fence()
                    data = _job_signed(auth, key, _job_body(fn))
                    _fence()
                    _write_json(res, data)
                except _Fenced:
                    pass
            finally:
                os._exit(0)
        os.write(fd, f"{child}\n".encode())
        os.close(fd)
        pid = child
    deadline = time.monotonic() + _job_wait()
    while True:
        if res.exists():
            if child:
                try:
                    os.waitpid(child, os.WNOHANG)
                except ChildProcessError:
                    pass
            try:
                pidf.unlink()
            except FileNotFoundError:
                pass
            return _job_take(mailbox, auth, key, res, pidf, _read_json(res), check)
        if child:
            try:
                done, _status = os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                done = child
            alive = not done
        else:
            alive = TL._pid_alive(pid)
        if not alive and not res.exists():
            try:
                pidf.unlink()
            except FileNotFoundError:
                pass
            raise StepError(f"job {key} (pid {pid}) ended without a result; re-run the step")
        if time.monotonic() >= deadline:
            return {"pending": True, "job": key}
        time.sleep(0.2)


def _acc_exec_job_alive(jobs: Path, exec_id: str) -> bool:
    d = jobs / exec_id
    if not d.is_dir() or d.is_symlink():
        return False
    return any(TL._pid_alive(_job_pid(p)) for p in d.glob("*.pid"))


def _acc_clean_other_execs(ctl, exec_id: str) -> None:
    """Job dirs and author workspaces of earlier executions (their jobs are
    fenced; a dir with a live job -- its job dir or its export, validation
    or snapshot dir -- is left until the job ends, eval-r19n2 finding 5)."""
    base = ctl.state_path.parent
    jobs = base / "native-jobs"
    try:
        entries = list(jobs.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.name == exec_id:
            continue
        if entry.is_dir() and not entry.is_symlink():
            if _acc_exec_job_alive(jobs, entry.name):
                continue
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    try:
        dirs = list(base.iterdir())
    except OSError:
        dirs = []
    for entry in dirs:
        name = entry.name
        m = re.fullmatch(r"(?:export|validate|snap)-([0-9a-f]{32})", name)
        if name in ("export", "validate") or (
                m and m.group(1) != exec_id and not _acc_exec_job_alive(jobs, m.group(1))):
            shutil.rmtree(entry, ignore_errors=True)


def _acc_tree_digest(root: Path) -> str | None:
    """A digest of every entry under *root* (never following a symlink:
    a link is its target text), None when *root* is not a directory."""
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return None
    except OSError:
        return None
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        rel = os.path.relpath(dirpath, root)
        for name in sorted(dirs + files):
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                h.update(b"?" + os.path.join(rel, name).encode("utf-8", "surrogateescape"))
                continue
            key = os.path.join(rel, name).encode("utf-8", "surrogateescape")
            if stat.S_ISLNK(st.st_mode):
                h.update(b"L\0" + key + b"\0" + os.readlink(path).encode("utf-8", "surrogateescape"))
            elif stat.S_ISDIR(st.st_mode):
                h.update(b"D\0" + key)
            elif stat.S_ISREG(st.st_mode):
                with open(path, "rb") as fh:
                    body = hashlib.sha256(fh.read()).hexdigest()
                h.update(b"F\0" + key + b"\0" + str(st.st_mode & 0o111).encode()
                         + b"\0" + body.encode())
            else:
                h.update(b"S\0" + key)
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
    return h.hexdigest()


def _acc_work(ctl, what: str) -> Path:
    """This execution's author workspace (``export``) or validation copy."""
    return ctl.state_path.parent / f"{what}-{ctl.native_auth.exec_id}"


# ------------------------------------------------------------ the audit
def _author_transcripts(marker: str, since: float) -> list[Path]:
    """The author agent's own transcript(s), if the Workflow runtime keeps
    them where trio-dash finds workflow runs (``${CLAUDE_CONFIG_DIR:-~/.claude}/
    projects/*/<session>/subagents/...``): files written since the export
    whose text carries this attempt's ``ACCEPTANCE-AUTHOR-RUN:`` line."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    needle = f"{AUTHOR_MARK} {marker}".encode()
    found: list[Path] = []
    try:
        projects = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return found
    for project in projects:
        try:
            sessions = [s / "subagents" for s in project.iterdir() if (s / "subagents").is_dir()]
        except OSError:
            continue
        for sub in sessions:
            for root, _dirs, files in os.walk(sub):
                for name in files:
                    if not name.endswith(".jsonl") or name == "journal.jsonl":
                        continue
                    path = Path(root) / name
                    try:
                        st = path.stat()
                        if st.st_mtime + 5 < since or st.st_size > 64 << 20:
                            continue
                        with path.open("rb") as fh:
                            head = fh.read(1 << 20)
                    except OSError:
                        continue
                    if needle in head:
                        found.append(path)
    return sorted(found)


def _session_cwd_hits(entries: list) -> list[str]:
    """Claude-native: Grep/Glob/LS without a ``path`` search the session's
    cwd (the loop repository), which the argument audit cannot see."""
    hits: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get("type") == "tool_use" and value.get("name") in _CWD_TOOLS:
                args = value.get("input") if isinstance(value.get("input"), dict) else {}
                if not str(args.get("path") or "").strip():
                    hits.append(f"{value.get('name')} without a path searches the session cwd "
                                "(the loop repository)")
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for entry in entries:
        walk(entry)
    return hits


def _native_author_audit(ctl, export: Path, marker: str, since: float) -> dict:
    """§2.2 layer 2 on the native path: the transcript audit when the
    author's transcript is found (tool-call arguments, relative paths from
    the session cwd = the loop repository), else the limited audit (the
    authored pack spelling the repository path), recorded ``limited``."""
    paths = _author_transcripts(marker, since)
    if not paths:
        audit = ctl._audit(export, {"transcript": None})
        audit["transcripts"] = []
        audit["note"] = ("limited: no author transcript found under "
                         "${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/*/subagents/")
        return audit
    entries: list = []
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            continue
    audit = ctl.ta.audit_transcript(entries, export, forbidden=ctl._audit_forbidden(),
                                    cwd=ctl.repo)
    extra = _session_cwd_hits(entries)
    if extra:
        audit["hits"] = (audit["hits"] + extra)[:40]
        audit["contaminated"] = True
    audit["limited"] = False
    audit["transcripts"] = [str(p) for p in paths]
    return audit


# -------------------------------------------------------------- the ops
def _acc_iteration(mailbox: Path, a: argparse.Namespace) -> int:
    return a.iteration or TL._number(_state(mailbox)["iteration"])


def _acc_int_attempt(a: argparse.Namespace) -> int:
    try:
        return max(1, int(a.attempt))
    except (TypeError, ValueError) as exc:
        raise StepError("--attempt must be an integer") from exc


def _acc_marker(a: argparse.Namespace) -> str:
    marker = str(getattr(a, "marker", "") or "")
    if not _ACC_MARKER_RE.fullmatch(marker):
        raise StepError("--marker must match [0-9A-Za-z._-]{1,120}")
    return marker


def _acc_trailing(ctl) -> list[str]:
    """Pack commits after the sealed pin that the sealed record does not
    hold (a pin or restore it did not make, an unadopted amendment, any
    other pack edit), when HEAD's pack is not the pinned one."""
    start = str(ctl.state.get("run_head") or "") or None
    chain = ctl.ta.derive_pin_chain(ctl.repo, ctl.acc_rel, start, verify=False,
                                    **_acc_chain_record(ctl.state))
    trailing = [a["label"] for a in chain["pending_amends"]] + list(chain["pending_tamper"])
    if not trailing or chain["head_pack"] == ctl.state.get("pin"):
        return []
    return trailing


def _acc_resume_sealed(ctl, prev: dict, iteration: int) -> None:
    """``begin`` over the previous execution's authentic sealed record
    (eval-r19n2 finding 1b): the record is the only source of truth across
    executions.

    - The state file is overwritten from it (logged when it disagreed).
    - Sealed ``authoring`` with a pack in the mailbox: the helper never
      froze it, so it is tamper (NEEDS_HUMAN), never re-derived from git.
    - Sealed ``frozen``: ``on_resume`` reconciles it with the pin chain git
      derives WITH the record's own commits (``chain_record``), so a pin or
      human adoption it does not hold is never adopted; such commits (HEAD's
      pack off the pin) are restored by a driver commit and the loop stops
      NEEDS_HUMAN ``acceptance-tamper``.
    """
    st = copy.deepcopy(prev["state"])
    on_file = ctl.state
    keys = ("status", "pin", "pin_commit", "freeze_commit", "driver_commits",
            "human_amends", "human_adoptions", "tamper_events", "amendments")
    differ = [k for k in keys if (on_file.get(k) or None) != (st.get(k) or None)]
    if differ:
        ctl._log(iteration, f"the driver state file {ctl.state_path} disagreed with the helper's "
                            f"sealed record ({', '.join(differ)}); overwritten from the sealed "
                            "record")
    ctl.state = st
    ctl.state["check_path"] = ctl.check_path
    ctl.chain_record = _acc_chain_record(st)
    if not ctl.frozen():
        if ctl._has_pack():
            raise TL.AcceptanceNeedsHuman(
                "acceptance-tamper",
                f"{ctl.acc_rel}/ holds a pack the helper's sealed record never froze (the last "
                "run stopped while authoring): it was written or committed outside the helper's "
                "freeze. Review it, remove it (git rm -r, commit), then start again")
        return
    ctl.on_resume()
    if ctl._resume_error is not None:
        raise ctl._resume_error
    trailing = _acc_trailing(ctl)
    if trailing:
        ctl.restore(iteration, "begin", "tamper")
        ctl.state["tamper_events"] = int(ctl.state.get("tamper_events", 0) or 0) + 1
        ctl._save()
        raise TL.AcceptanceNeedsHuman(
            "acceptance-tamper",
            f"pack commits after the helper's sealed pin {str(ctl.state.get('pin'))[:12]} that "
            "its record does not hold were not adopted; the pinned pack was restored by a "
            f"driver commit: {'; '.join(t[:120] for t in trailing[:3])}. A human amendment is "
            "made with `trioctl omnigent acceptance amend --human [--adopt <sha>]` while the "
            "loop is stopped; review, then start again")


def _acc_begin(mailbox: Path, repo: Path | None, a: argparse.Namespace,
               iteration: int, exec_id: str) -> dict:
    """``begin``: the previous execution's sealed record, authenticated
    with the secret it was sealed under (eval-r19n2 finding 1b), then a
    fresh execution key (the record re-sealed under it), then the controller
    state: from that record (``_acc_resume_sealed``), or on a first run (no
    record) from the driver state reconciled with git (``on_resume``)
    exactly as the Cursor driver does at start. The new sealed record holds
    the reconciled state (``authoring`` without a pin), the interpreters
    resolved now, and the failed-gate counts and REPORT digests of the
    previous record (finding 4: never from ``.native.json``). Records of
    earlier executions cannot be authenticated and are dropped."""
    try:
        prev = _acc_prev_sealed(mailbox, repo)
        auth = _acc_auth_begin(mailbox, repo, exec_id, prev)
        ctl = TL.AcceptanceController(mailbox, repo, None, {"enabled": True})
        _acc_bind(ctl, auth, _acc_interpreters(ctl.ta))
        prev_native: dict = {}
        if prev is None:
            ctl.on_resume()
            if ctl._resume_error is not None:
                raise ctl._resume_error
        else:
            prev_native = dict(prev["state"].get("native") or {})
            _acc_resume_sealed(ctl, prev, iteration)
        if not ctl.frozen():
            ctl.state = {"status": "authoring"}
        else:
            ctl.state.pop("native", None)
        native = _acc_native(ctl)
        fails = prev_native.get("gate_fail") if isinstance(prev_native.get("gate_fail"), dict) \
            else {}
        report = prev_native.get("report") if isinstance(prev_native.get("report"), dict) else {}
        native.update(exec=exec_id,
                      gate_fail={str(k): int(v or 0) for k, v in fails.items()
                                 if isinstance(v, int) and not isinstance(v, bool)},
                      report={str(k): v for k, v in report.items() if isinstance(v, str)},
                      live={"exec": exec_id, "holder_pid": _holder_pid(), "utc": _now_iso()})
        data = _records(mailbox)
        if data["gate"] or data["apply"]:
            data["gate"], data["apply"] = {}, {}
            _write_json(mailbox / RECORDS, data)
        ctl._save()
        _acc_clean_other_execs(ctl, exec_id)
        return _acc_digest(ctl)
    except TL.AcceptanceError as exc:
        return {"stop": _acc_stop(mailbox, iteration, exc)}


def op_acceptance_export(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """The author's workspace (§2.2 layer 1): ``git archive`` of the base
    into this execution's dir beside the driver state, without git,
    mailboxes or vendored Trio files; ``.acceptance-input/`` holds GOAL.md
    (+ notes) only. Attempt 1 takes the base (HEAD now: no Lead pass ran
    yet); a pack this execution holds frozen (sealed) answers
    ``frozen: true`` and nothing is exported. A pack in the mailbox that the
    helper did not freeze stops the loop (``_acc_verify``)."""
    _require_lock(mailbox, a.token)
    iteration = _acc_iteration(mailbox, a)
    attempt = _acc_int_attempt(a)
    try:
        ctl = _acc_controller(mailbox, repo, a)
        if ctl.frozen():
            return {"frozen": True, "acceptance": _acc_digest(ctl)}
        base = str(ctl.state.get("base") or "") if attempt > 1 else ""
        base = base or TL._git_head(ctl.repo) or ""
        if not base:
            raise TL.AcceptanceError("acceptance-error", "the loop repository has no HEAD")
        exec_id = ctl.native_auth.exec_id
        _acc_clean_other_execs(ctl, exec_id)
        export = _acc_work(ctl, "export")
        info = ctl.ta.build_export(ctl.repo, base, export, ctl.mailbox)
        ctl.state.update({"status": "authoring", "started_utc": ctl.ta.utc_now(),
                          "base": info["base"], "run_head": info["base"]})
        _acc_native(ctl)["export_ts"] = time.time()
        ctl._save()
        return {"frozen": False, "export": str(export), "base": info["base"],
                "marker": f"{exec_id}-{iteration}", "notes": bool(info["notes"]),
                "removed": len(info["removed"]), "tool": str(_acc_tool()),
                "acceptance": _acc_digest(ctl)}
    except TL.AcceptanceError as exc:
        return {"frozen": False, "acceptance": {"stop": _acc_stop(mailbox, iteration, exc)}}


def op_acceptance_freeze(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """After the author agent: audit, validation at base on a fresh export
    (``freeze_filter``), at most one retry and one contaminated re-run
    (the script's ``--prior`` says which were used), then the driver's
    freeze commit (``AcceptanceController._freeze``). Runs as a job."""
    _require_lock(mailbox, a.token)
    iteration = _acc_iteration(mailbox, a)
    attempt = _acc_int_attempt(a)
    marker = _acc_marker(a)
    prior = _acc_json(a, "prior")
    author = _acc_json(a, "author")
    model = str(getattr(a, "model", "") or "") or None

    def body() -> dict:
        try:
            # eval-r19n finding 3: frozen only by this execution's own freeze
            # (sealed ``freeze_exec`` + the structurally verified driver
            # freeze commit git derives); a state file or mailbox frozen by
            # anyone else stops the loop in ``_acc_verify``.
            ctl = _acc_controller(mailbox, repo, a, replay_freeze=True)
            ta = ctl.ta
            if ctl.frozen():
                ctl._log(iteration, f"freeze replayed (this execution's freeze "
                                    f"{str(ctl.state.get('freeze_commit'))[:12]})")
                return {"action": "frozen", "checks": ctl.state.get("checks"),
                        "dropped": [list(d) for d in ctl.state.get("dropped") or []][:40],
                        "replayed": True, "acceptance": _acc_digest(ctl)}
            export = _acc_work(ctl, "export")
            base = str(ctl.state.get("base") or "")
            if ctl.state.get("status") != "authoring" or not base or not export.is_dir():
                raise StepError("acceptance-freeze: no author export (run acceptance-export first)")
            since = float(_acc_native(ctl).get("export_ts") or 0.0)
            audit = _native_author_audit(ctl, export, f"{marker}-a{attempt}", since)
            summary = {"attempt": attempt, "contaminated": audit["contaminated"],
                       "limited": audit.get("limited"), "hits": audit["hits"][:10],
                       "transcripts": audit.get("transcripts") or [],
                       "path": audit.get("path") or "native"}
            ctl.state.setdefault("audits", []).append(summary)
            ctl._save()
            if audit["contaminated"]:
                ctl._log(iteration, "author session contaminated "
                                    f"({'; '.join(audit['hits'][:3])}); discarded")
                if prior.get("contaminated"):
                    raise TL.AcceptanceError("acceptance-contaminated",
                                             "the re-run author session was contaminated too")
                ta.build_export(ctl.repo, base, export, ctl.mailbox)
                _acc_native(ctl)["export_ts"] = time.time()
                ctl._save()
                return {"action": "reauthor", "prefix": TL.CONTAMINATION_PREFIX,
                        "hits": audit["hits"][:5], "audit": summary,
                        "acceptance": _acc_digest(ctl)}
            if author.get("exit") not in (0, None):
                ctl._log(iteration, f"author session exited {author.get('exit')}")
            goal = (ctl.mailbox / "GOAL.md").read_text(encoding="utf-8", errors="replace")
            notes_path = ctl.mailbox / "ACCEPTANCE-NOTES.md"
            notes = (notes_path.read_text(encoding="utf-8", errors="replace")
                     if notes_path.is_file() else None)
            # eval-r19n2 finding 5: validate a private snapshot of the
            # author's pack and freeze exactly those bytes; the export must
            # still hold them at the freeze.
            src = export / ta.PACK_DIR
            snap = _acc_work(ctl, "snap")
            shutil.rmtree(snap, ignore_errors=True)
            snap.mkdir(parents=True)
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, snap / ta.PACK_DIR, symlinks=True)
            acc = snap / ta.PACK_DIR
            validated = _acc_tree_digest(acc)
            fresh = _acc_work(ctl, "validate")
            info = ta.build_export(ctl.repo, base, fresh, ctl.mailbox)
            try:
                try:
                    manifest = ta.load_manifest(acc)
                except ta.ManifestError as exc:
                    manifest = None
                    ctl._log(iteration, f"author wrote no usable manifest ({exc})")
                if manifest is not None:
                    base_run = ta.run_pack(acc, fresh, exclude={ta.PACK_DIR, ta.INPUT_DIR},
                                           manifest=manifest, path=ctl.check_path)
                    filtered = ta.freeze_filter(manifest, base_run, goal, notes, acc)
                else:
                    filtered = {"manifest": None, "dropped": [], "unavailable_at_base": [],
                                "retry": True, "fatal": ["no manifest"]}
            finally:
                shutil.rmtree(fresh, ignore_errors=True)
            if filtered["retry"] and not prior.get("retried"):
                shutil.rmtree(snap, ignore_errors=True)
                ctl._log(iteration, "author retry: "
                         + (f"{len(filtered['dropped'])} dropped" if filtered["dropped"]
                            else "; ".join(filtered["fatal"][:2]) or "too few checks"))
                return {"action": "retry",
                        "dropped": [list(d) for d in filtered["dropped"]][:40],
                        "fatal": list(filtered["fatal"])[:20], "audit": summary,
                        "acceptance": _acc_digest(ctl)}
            _fence()
            if ctl._has_pack():
                raise _acc_mismatch(f"{ctl.acc_rel}/ holds a pack the helper did not freeze "
                                    "(written or committed while the author's pack was "
                                    "validated): tamper")
            if _acc_tree_digest(acc) != validated or _acc_tree_digest(src) != validated:
                raise TL.AcceptanceNeedsHuman(
                    "acceptance-tamper",
                    f"the author's pack changed while it was validated ({src}; a process "
                    "still writing the export?): not frozen")
            _acc_native(ctl)["freeze_exec"] = ctl.native_auth.exec_id
            try:
                ctl._freeze(iteration, snap, filtered,
                            {"model": model, "effort": "high",
                             "session": f"{marker}-a{attempt}",
                             "path": summary.get("path") or "native"},
                            info, goal, notes)
            finally:
                shutil.rmtree(snap, ignore_errors=True)
            return {"action": "frozen", "checks": ctl.state.get("checks"),
                    "dropped": [list(d) for d in ctl.state.get("dropped") or []][:40],
                    "unavailable_at_base": list(filtered.get("unavailable_at_base") or []),
                    "audit": summary, "acceptance": _acc_digest(ctl)}
        except TL.AcceptanceError as exc:
            return {"action": "stop", "acceptance": {"stop": _acc_stop(mailbox, iteration, exc)}}

    return _run_job(mailbox, repo, a, f"freeze-{iteration}-{attempt}-{marker}", body)


def _plan_text_for(plan: dict) -> tuple[str, list[str]]:
    """The structured plan's acceptance mapping as PLAN.md text, so
    ``trio-check.coverage_refusals`` judges it exactly as it judges
    PLAN.md (no second implementation of the rule)."""
    problems: list[str] = []
    slices = plan.get("slices") if isinstance(plan.get("slices"), list) else []
    lines = ["# Plan (structured output of the Lead's plan call)", "", "```yaml", "slices:"]
    for s in slices:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sid):
            problems.append(f"bad slice id {sid[:40]!r}")
            continue
        covers = s.get("covers") or []
        good = []
        for item in covers if isinstance(covers, list) else [covers]:
            if isinstance(item, str) and _ACC_ID_RE.match(item.strip()):
                good.append(item.strip())
            else:
                problems.append(f"slice {sid} `covers` item {str(item)[:40]!r} is not an "
                                "acceptance id (ACC-NN)")
        lines += [f"  - id: {sid}", "    writes: [placeholder]"]
        if good:
            lines.append(f"    covers: [{', '.join(good)}]")
    lines += ["```", "", "## Verification standard"]
    lead = plan.get("lead_integration") or []
    items = []
    for item in lead if isinstance(lead, list) else [lead]:
        if isinstance(item, str) and _ACC_ID_RE.match(item.strip()):
            items.append(item.strip())
        else:
            problems.append(f"`lead_integration` item {str(item)[:40]!r} is not an "
                            "acceptance id (ACC-NN)")
    if items:
        lines.append(f"lead_integration: [{', '.join(items)}]")
    bindings = plan.get("acceptance_bindings") or {}
    if not isinstance(bindings, dict):
        problems.append("`acceptance_bindings` is not an object")
        bindings = {}
    rows = []
    for name, value in bindings.items():
        text = str(value) if isinstance(value, (str, int, float)) else None
        if not _BINDING_NAME_RE.match(str(name)) or text is None or not text \
                or not text.isprintable() or any(c in text for c in '"\\#'):
            problems.append(f"`acceptance_bindings` entry {str(name)[:40]!r} is not "
                            "NAME: <printable value without \", \\ or #>")
            continue
        rows.append(f'  {name}: "{text}"')
    if rows:
        lines += ["acceptance_bindings:"] + rows
    return "\n".join(lines) + "\n", problems


def _structured_refusals(ctl, plan: dict) -> list[str]:
    text, problems = _plan_text_for(plan)
    work = Path(tempfile.mkdtemp(prefix="acc-plan-"))
    try:
        for name in ("GOAL.md", "ACCEPTANCE-NOTES.md"):
            if (ctl.mailbox / name).is_file():
                shutil.copy2(ctl.mailbox / name, work / name)
        (work / ctl.ta.PACK_DIR).mkdir()
        shutil.copy2(ctl.acc_dir / ctl.ta.MANIFEST, work / ctl.ta.PACK_DIR / ctl.ta.MANIFEST)
        (work / "PLAN.md").write_text(text, encoding="utf-8")
        if ctl.tc is None:
            return problems + ["trio-check.py is missing; acceptance coverage cannot be checked"]
        try:
            return problems + list(ctl.tc.coverage_refusals(work))
        except Exception as exc:  # noqa: BLE001 - fail closed
            return problems + [f"coverage check failed: {type(exc).__name__}: {exc}"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _acc_briefs(ctl, plan: dict) -> dict:
    """``## Acceptance (frozen; do not edit)`` per slice (trioctl's
    ``_acceptance_brief``), from the verified pack and the plan's covers."""
    try:
        manifest = ctl.ta.load_manifest(ctl.acc_dir)
    except Exception:  # noqa: BLE001 - the section is best effort
        return {}
    by_id = {c.get("id"): c for c in manifest.get("checks") or [] if isinstance(c, dict)}
    tool = shlex.quote(str(_acc_tool()))
    box = shlex.quote(str(ctl.mailbox))
    out: dict = {}
    for s in plan.get("slices") or []:
        if not isinstance(s, dict) or not isinstance(s.get("covers"), list):
            continue
        mine = [by_id[c] for c in s["covers"] if isinstance(c, str) and c in by_id]
        if not mine:
            continue
        lines = ["## Acceptance (frozen; do not edit)",
                 "Independent black-box checks this slice covers (written from GOAL.md "
                 "before the plan; never edit anything under the mailbox's acceptance/). "
                 "Run them against your worktree:",
                 f"`python3 {tool} run --mailbox {box} --tree \"$(git rev-parse "
                 f"--show-toplevel)\" --ids {','.join(c['id'] for c in mine)}`"]
        for check in mine:
            lines.append(f"- {check['id']} ({check.get('kind')}): \"{check.get('goal_quote')}\"")
        lines.append("A covered check that FAILs only because a sibling slice has not landed "
                     "yet: report `ACCEPTANCE: <id> FAIL (needs <slice>)`. A FAIL on your own "
                     "surface means the slice is not done.")
        out[str(s.get("id"))] = "\n".join(lines) + "\n"
    return out


def op_coverage(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """§4.3 native: before any builder, every frozen behaviour/doc id must
    be mapped by the structured plan AND by PLAN.md as written; the pin is
    checked (restored, counted) first. Attempt 2 refusing stops the loop
    (``status: error``), as a second Lead-pass refusal does in trio_loop."""
    _require_lock(mailbox, a.token)
    iteration = _acc_iteration(mailbox, a)
    attempt = _acc_int_attempt(a)
    _lead_running(mailbox, iteration, "coverage")
    plan = _acc_json(a, "plan")
    empty = {"covered_ok": False, "refusals": [], "briefs": {}}
    try:
        ctl = _acc_controller(mailbox, repo, a)
        if not ctl.frozen():
            raise TL.AcceptanceError("acceptance-error",
                                     "the pack is not frozen (the author phase did not run)")
        refusals: list[str] = []
        if not ctl.check_pin(iteration, "lead plan"):
            refusals.append("acceptance/ was edited outside the freeze/amend protocol "
                            "(restored from the pin)")
        refusals += [f"structured plan: {r}" for r in _structured_refusals(ctl, plan)]
        refusals += [f"PLAN.md: {r}" for r in ctl.coverage()]
        briefs = _acc_briefs(ctl, plan)
        if refusals:
            ctl._log(iteration, "lead plan refused: coverage ("
                     + "; ".join(r[:160] for r in refusals[:3]) + ")")
            TL._append_log(mailbox, f"- iter {iteration} | loop | lead pass refused: "
                                    "acceptance coverage")
            _acc_count(ctl, "coverage")
            ctl._save()
            if attempt >= 2:
                raise TL.AcceptanceError(
                    "acceptance-coverage",
                    "the re-planned Lead pass was refused again: " + "; ".join(refusals)[:400])
        return {"covered_ok": not refusals, "refusals": refusals[:40], "briefs": briefs,
                "acceptance": _acc_digest(ctl)}
    except TL.AcceptanceError as exc:
        return {**empty, "acceptance": {"stop": _acc_stop(mailbox, iteration, exc)}}


def op_acceptance_run(mailbox: Path, repo: Path | None, a: argparse.Namespace) -> dict:
    """§8.1 driver pre-run: the pinned pack (from git, hash-verified) at the
    evaluated sha, each check in its own copy (``integration_context``).
    The FROZEN ACCEPTANCE block goes into the Evaluator's prompt. Job."""
    _require_lock(mailbox, a.token)
    iteration = _acc_iteration(mailbox, a)
    sha = str(getattr(a, "sha", "") or "")
    state = _state(mailbox)
    snap = _snapshot(state)
    if snap["iteration"] != iteration or snap["phase"].lower() != "lead-done":
        raise StepError(f"acceptance-run: STATE is iteration {snap['iteration']} phase "
                        f"{snap['phase']}, not lead-done of iteration {iteration}")
    if not sha or sha != state["evaluated_sha"].strip():
        raise StepError(f"acceptance-run: --sha {sha[:12] or '(none)'} is not the pinned sha "
                        f"{state['evaluated_sha'].strip()[:12] or '(none)'}")
    attempt = state["evaluator_attempt"].strip()

    def body() -> dict:
        empty = {"text": "", "passed": 0, "failed": 0, "unavailable": 0, "total": 0}
        try:
            ctl = _acc_controller(mailbox, repo, a)
            ctx = ctl.integration_context(sha, iteration)
            _acc_native(ctl)["last_prerun"] = {
                "iteration": iteration, "attempt": attempt, "sha": sha,
                "passed": ctx["passed"], "failed": ctx["failed"],
                "unavailable": ctx["unavailable"], "total": ctx["total"]}
            ctl._save()
            return {"text": ctx["text"], "passed": ctx["passed"], "failed": ctx["failed"],
                    "unavailable": ctx["unavailable"], "total": ctx["total"],
                    "amendments_left": ctx["amendments_left"],
                    "acceptance": _acc_digest(ctl)}
        except TL.AcceptanceError as exc:
            return {**empty, "acceptance": {"stop": _acc_stop(mailbox, iteration, exc)}}

    return _run_job(mailbox, repo, a, f"prerun-{iteration}-{attempt}-{sha}", body)


def _acc_next(mailbox: Path, repo: Path | None, a: argparse.Namespace, body: dict) -> dict:
    """``next`` of a Lead/repair pass: the pin is checked before the pass
    (restored and counted, as ``_run_role`` does), and a SHIP the gate
    refused hands its failures to the next Lead (``acceptance_errors``)."""
    iteration = int(body.get("iteration") or 0)
    try:
        ctl = _acc_controller(mailbox, repo, a)
        out: dict = {}
        if ctl.frozen():
            ctl.check_pin(iteration, f"before {body['action']}")
        pending = _acc_native(ctl).get("pending_errors")
        if (body["action"] == "lead" and isinstance(pending, dict)
                and pending.get("iteration") == iteration):
            out["errors"] = [str(e)[:800] for e in pending.get("errors") or []][:10]
        out.update(_acc_digest(ctl))
        return out
    except TL.AcceptanceError as exc:
        return {"stop": _acc_stop(mailbox, iteration, exc)}


def _acc_gate(mailbox: Path, repo: Path | None, a: argparse.Namespace,
              iteration: int, role: str) -> tuple[list[str], object, dict]:
    """The third gate of ``_run_role``: freeze reached, pin intact (else
    restored and counted), every check mapped (Lead). (problems, error,
    digest)."""
    try:
        ctl = _acc_controller(mailbox, repo, a)
        problems = ctl.lead_gate(iteration, role)
        return problems, None, _acc_digest(ctl)
    except TL.AcceptanceError as exc:
        return [], exc, {}


def _acc_dispatch_refusal(mailbox: Path, repo: Path | None, a: argparse.Namespace,
                          iteration: int) -> None:
    """trioctl's builder refusal on the native driver: no wave starts while
    the pack is not frozen, off its pin (restored and counted) or unmapped
    in PLAN.md."""
    try:
        ctl = _acc_controller(mailbox, repo, a)
        problems = []
        if not ctl.frozen():
            problems.append("the pack is not frozen")
        else:
            # A pack edit since the coverage step (an integrate call) is
            # restored and counted, as before any role pass; the wave then
            # forks from the restored HEAD.
            ctl.check_pin(iteration, "builder dispatch")
            problems += ctl.coverage()
    except TL.AcceptanceError as exc:
        stop = _acc_stop(mailbox, iteration, exc)
        raise StepError(f"builder dispatch refused: acceptance stopped the loop "
                        f"({stop['reason']}): {stop['detail'][:300]}") from exc
    if problems:
        raise StepError("builder dispatch refused (frozen acceptance): "
                        + "; ".join(problems)[:600])


def _acc_review(mailbox: Path, repo: Path | None, a: argparse.Namespace,
                verdict: str, scope, evaluated: str) -> tuple[str, object, int | None, dict]:
    """``apply``: amendments, anti-thrash, UNAVAILABLE and the SHIP gate
    (``review_verdict``), then the refused SHIP's failures for the next
    Lead. Returns (verdict, scope, forced code | None, record)."""
    state_path = mailbox / "STATE.md"
    try:
        ctl = _acc_controller(mailbox, repo, a)
        native = _acc_native(ctl)
        prerun = native.get("last_prerun")
        if isinstance(prerun, dict) and prerun.get("sha") == evaluated:
            ctl.last_run = dict(prerun)
        before = verdict
        verdict, scope, forced = ctl.review_verdict(verdict, scope, a.iteration, evaluated,
                                                    state_path)
        if before == "SHIP" and ctl.last_run and ctl.last_run is not prerun \
                and "results" in ctl.last_run:
            native["ship_gate"] = {
                "iteration": a.iteration, "sha": evaluated,
                "passed": ctl.last_run.get("passed"), "failed": ctl.last_run.get("failed"),
                "unavailable": ctl.last_run.get("unavailable"),
                "total": ctl.last_run.get("total"), "verdict": verdict}
        if ctl.pending_errors:
            native["pending_errors"] = {"iteration": a.iteration + 1,
                                        "errors": list(ctl.pending_errors)}
        if before == "SHIP" and verdict != "SHIP":
            _acc_count(ctl, "ship")
        ctl._save()
        record = {**_acc_digest(ctl), "verdict_in": before, "verdict_out": verdict,
                  "ship_refused": before == "SHIP" and verdict != "SHIP",
                  "ship_gate": native.get("ship_gate") if before == "SHIP" else None,
                  "pending_errors": list(ctl.pending_errors)[:5]}
        if forced is not None:
            try:
                phase = _state(mailbox)["phase"].strip()
            except Exception:  # noqa: BLE001 - a report only
                phase = ""
            record["stop"] = {"status": "needs_human" if forced == 5 else "error",
                              "code": forced, "reason": phase or "acceptance",
                              "detail": "forced by the frozen-acceptance review"}
            _acc_seal_stop(mailbox, f"{record['stop']['status']}: {record['stop']['reason']}")
        return verdict, scope, forced, record
    except TL.AcceptanceError as exc:
        stop = _acc_stop(mailbox, a.iteration, exc)
        return verdict, scope, stop["code"], {"stop": stop}


# ------------------------------------------- trioctl's native amend path
def acc_sealed_record(mailbox: Path, repo: Path | None) -> Path | None:
    """The helper's sealed acceptance record for *mailbox* when one exists
    (a symlink or any other entry there counts: it is refused later)."""
    path = _ledger_path(Path(mailbox).resolve(), repo)
    if path is None:
        return None
    sealed = path.parent / ACC_SEALED
    return sealed if os.path.lexists(sealed) else None


def cli_human_amend(mailbox: Path, controller, ids: list[str], reason: str,
                    adopt: list[str] | None = None) -> int:
    """``trioctl omnigent acceptance amend --human [--adopt]`` on a mailbox
    the native helper keeps a sealed record for (eval-r19n2 finding 1c).

    The sealed record, not the role-writable driver state file, is the
    source: it must verify with the current secret, and it must record a
    stop (``native.stopped``, written at every acceptance stop and at
    ``end``) unless the execution that sealed it is gone (its holder pid is
    dead: a run that died without ``end``). The amendment then runs from
    the sealed state with the chain derived from the record's own commits
    (``AcceptanceController.human_amend``: commits since the sealed pin
    must be named with ``--adopt`` after review), and the adoption (new
    pin, pin commit, ``human_amends``) is written into the sealed record,
    authenticated with the current secret. Only this path adds to the
    sealed ``human_amends``; the next ``begin`` adopts nothing else.
    0 amended, 3 refused, 5 the loop is running."""
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
    mailbox = Path(mailbox).resolve()
    try:
        home = _acc_home(mailbox, controller.repo)
        found = _acc_sealed_auth(home)
        if found is None:
            raise TL.AcceptanceNeedsHuman(
                "acceptance-state-mismatch",
                f"the helper's sealed record {home / ACC_SEALED} does not verify with the "
                "current secret")
        auth = found[0]
        sealed = _acc_unseal(auth)
    except TL.AcceptanceError as exc:
        print(f"acceptance amend: refused ({exc.reason}): {exc.detail}", file=sys.stderr)
        return 3
    st = copy.deepcopy(sealed["state"])
    native = _acc_native_of(st)
    if not native.get("stopped"):
        live = native.get("live") if isinstance(native.get("live"), dict) else {}
        try:
            pid = int(live.get("holder_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and TL._pid_alive(pid):
            print(f"acceptance amend: refused: native loop execution {auth.exec_id[:12]} is "
                  f"running (its sealed record holds no stop; holder pid {pid} is alive); "
                  "stop it first", file=sys.stderr)
            return 5
    if not (st.get("status") == "frozen" and st.get("pin")):
        print("acceptance amend: refused: the helper's sealed record holds no frozen pack",
              file=sys.stderr)
        return 3
    controller.state = st
    controller.state["check_path"] = controller.check_path
    controller.chain_record = _acc_chain_record(st)
    rc = controller.human_amend(ids, reason, adopt=adopt)
    if rc == 0:
        _acc_seal(auth, controller.state, sealed["interpreters"])
        print(f"acceptance: the adoption is recorded in the native helper's sealed record "
              f"{home / ACC_SEALED}")
    return rc


HANDLERS = {"begin": op_begin, "next": op_next, "dispatch": op_dispatch,
            "builders": op_builders, "cleanup": op_cleanup, "gate": op_gate,
            "pin": op_pin, "apply": op_apply, "end": op_end,
            "acceptance-export": op_acceptance_export,
            "acceptance-freeze": op_acceptance_freeze,
            "coverage": op_coverage, "acceptance-run": op_acceptance_run}


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
                        help="gate: role attempt 1|2; apply: evaluator_attempt; "
                             "builders: report round (2 = a re-report)")
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
    # r19 N2 (frozen acceptance): used only when the script runs with
    # args.acceptance; every op ignores them otherwise.
    parser.add_argument("--acceptance", type=int, choices=(0, 1), default=0,
                        help="frozen acceptance on (args.acceptance)")
    parser.add_argument("--models", default=None,
                        help="begin: JSON {lead, evaluator, acceptance} (tier check)")
    parser.add_argument("--acc", default=None,
                        help="the acceptance digest the script holds (JSON)")
    parser.add_argument("--marker", default=None,
                        help="acceptance-freeze: the author run marker")
    parser.add_argument("--prior", default=None,
                        help="acceptance-freeze: JSON {contaminated, retried}")
    parser.add_argument("--author", default=None,
                        help="acceptance-freeze: JSON summary of the author agent")
    parser.add_argument("--model", default=None,
                        help="acceptance-freeze: the author's model")
    parser.add_argument("--plan", default=None,
                        help="coverage: JSON {slices:[{id,covers}], lead_integration, "
                             "acceptance_bindings}")
    parser.add_argument("--sha", default=None, help="acceptance-run: the pinned sha")
    parser.add_argument("--poll", type=int, default=0,
                        help="a re-run of a pending long op (ignored)")
    parser.add_argument("--json", action="store_true",
                        help="accepted for symmetry; output is always JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    # eval-r19n2 finding 2: every git process of this helper and its jobs
    # (and the loop core's, which inherit this environment) reads the real
    # objects; a `refs/replace/` ref never stands in for a commit.
    os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"
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
        if a.op == "next" and body.get("action") == "lead":
            body.update(_human_answer(mailbox, int(body.get("iteration") or 0)))
        if a.op == "next" and _acc_on(a) and body.get("action") in ("lead", "repair"):
            body["acceptance"] = _acc_next(mailbox, repo, a, body)
        elif a.op == "pin":
            body.update(_human_answer(mailbox, a.iteration, consume=True,
                                      key=_retry_key(mailbox, a.token, str(a.nonce or ""),
                                                     a.iteration)))
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

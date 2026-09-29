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
import fcntl
import hashlib
import importlib.util
import json
import os
import re
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
              begun_at=_now_iso())
    return {
        "mode": "lockstep",
        "repo": str(repo) if repo else None,
        "lock_owner": _owner(a.token),
        "exec_id": exec_id,
        "exclude_path": exclude,
        "reclaimed": reclaimed,
        "tmpdir": tmpdir,
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
    if ours:
        # A partial outcome for runs without launch.sh (an interactive
        # Workflow call); launch.sh replaces it with the full result.
        try:
            _write_json(mailbox / RESULT, {
                "schema": 1, "source": "end", "driver": DRIVER,
                "run_token": a.token, "lock": lock,
                "dangling_worktrees": dangling,
                "state_status": snap["status"], "phase": snap["phase"],
                "iteration": snap["iteration"],
                "session_started_at": session.get("started_at"),
                "finished_at": _now_iso(),
            })
        except OSError:
            pass
        _register(mailbox, own_token=a.token, state="ended",
                  ended_at=_now_iso(), lock=lock,
                  dangling_worktrees=dangling)
    return {"lock": lock, "dangling_worktrees": dangling,
            "eval_worktrees_removed": eval_removed,
            "eval_worktrees_kept": eval_kept,
            "eval_worktrees_left": eval_left,
            "scratch_removed": scratch_removed,
            "scratch_kept": scratch_kept,
            "scratch_left": scratch_left, **snap}


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
        if a.op == "next" and body.get("action") == "lead":
            body.update(_human_answer(mailbox, int(body.get("iteration") or 0)))
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

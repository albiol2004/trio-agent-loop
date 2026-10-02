"""Owned running evidence for the dashboard (api:LiveRegistry).

"Running" must come only from evidence a driver or session owns. This module
holds the two pieces that are not already a sidecar read in ``serve.py``:

* the session heartbeat, ``<mailbox>/.heartbeat.json``: a lightweight file an
  in-session Trio orchestrator (no driver, no lock) writes so its loop does
  not look interrupted; format and liveness rule in ``MAILBOX-SCHEMA.md``
  (``## Session heartbeat``);
* ``native_run_evidence``: whether a native-runs registry entry (as returned
  by ``loop_actions.native_registry``) proves a live claude-workflow run.

Native registry values, as the writers use them (``native/trio_native_step.py``
``_register`` and ``native/launch.sh`` ``_update_registry``):

* ``begin`` writes ``state: "running"`` and ``holder_pid`` (the long-lived
  process that owns the mailbox ``.lock``) and no ``lock`` key at all: while
  the run is live the record carries no ``lock`` field, which means "the
  mailbox lock is still held";
* ``end`` writes ``state: "ended"`` and ``lock: "released"`` (or ``"foreign"``
  when another run owns the lock);
* ``launch.sh`` writes ``state: "finished"`` when the launcher exits.

So a live run is ``lock`` in {``"held"``, absent} (a ``"held"`` value is
accepted for forward compatibility), ``state`` not terminal and
``holder_pid`` alive. Terminal states: ended, finished, released, error.

Stdlib only; loaded by file path from ``serve.py``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import stat
from pathlib import Path

HEARTBEAT_FILE = ".heartbeat.json"
HEARTBEAT_DEFAULT_TTL_S = 900
HEARTBEAT_MIN_TTL_S = 60
HEARTBEAT_MAX_TTL_S = 14400
HEARTBEAT_FUTURE_SKEW_S = 120
HEARTBEAT_READ_LIMIT = 64 * 1024

NATIVE_TERMINAL_STATES = frozenset({"ended", "finished", "released", "error"})
NATIVE_LIVE_LOCKS = frozenset({"held", ""})


def _default_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_time(value) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp


def _read_bounded(path: Path) -> bytes | None:
    """Regular-file read of at most HEARTBEAT_READ_LIMIT bytes, never
    through a symlink; None when unreadable."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, HEARTBEAT_READ_LIMIT + 1)[:HEARTBEAT_READ_LIMIT]
    except OSError:
        return None
    finally:
        os.close(fd)


def _view(live: bool, reason: str, age_s, ttl_s: int, data: dict) -> dict:
    def text(key):
        value = data.get(key)
        return value if isinstance(value, str) else ""

    iteration = _as_int(data.get("iteration"))
    return {"live": live, "reason": reason, "age_s": age_s, "ttl_s": ttl_s,
            "phase": text("phase"), "iteration": iteration,
            "session_id": text("session_id"), "writer": text("writer"),
            "updated_at": text("updated_at")}


def read_heartbeat(mailbox, *, now=None, pid_alive=None) -> dict | None:
    """The mailbox's session heartbeat, judged; None when there is none.

    ``now`` is a ``datetime`` or epoch seconds (default: the clock);
    ``pid_alive(pid) -> bool`` replaces the OS probe (tests).
    """
    path = Path(mailbox) / HEARTBEAT_FILE
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return _view(False, "symlink", None, HEARTBEAT_DEFAULT_TTL_S, {})
    raw = _read_bounded(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    ttl = _as_int(data.get("ttl_s"))
    ttl = HEARTBEAT_DEFAULT_TTL_S if not ttl or ttl <= 0 else ttl
    ttl = max(HEARTBEAT_MIN_TTL_S, min(ttl, HEARTBEAT_MAX_TTL_S))

    if data.get("schema") != 1 or isinstance(data.get("schema"), bool):
        return _view(False, "schema", None, ttl, data)
    if data.get("done") is True:
        return _view(False, "done", None, ttl, data)
    stamp = _parse_time(data.get("updated_at"))
    if stamp is None:
        return _view(False, "no-timestamp", None, ttl, data)
    if now is None:
        current = _dt.datetime.now(_dt.timezone.utc)
    elif isinstance(now, _dt.datetime):
        current = now if now.tzinfo else now.replace(tzinfo=_dt.timezone.utc)
    else:
        current = _dt.datetime.fromtimestamp(float(now), _dt.timezone.utc)
    age = (current - stamp).total_seconds()
    if age < -HEARTBEAT_FUTURE_SKEW_S:
        return _view(False, "future", age, ttl, data)
    if age > ttl:
        return _view(False, "stale", age, ttl, data)
    raw_pid = data.get("pid")
    if raw_pid is not None:
        pid = _as_int(raw_pid)
        probe = pid_alive or _default_pid_alive
        if pid is None or not probe(pid):
            return _view(False, "pid-dead", age, ttl, data)
    return _view(True, "fresh", age, ttl, data)


def native_run_evidence(entries, mailbox, *, pid_alive=None) -> dict | None:
    """The native-runs registry entry proving a live run of ``mailbox``."""
    try:
        target = str(Path(mailbox).resolve())
    except (OSError, RuntimeError):
        return None
    probe = pid_alive or _default_pid_alive
    for entry in entries or ():
        if not isinstance(entry, dict) or entry.get("mailbox") != target:
            continue
        lock = entry.get("lock")
        lock = "" if lock is None else str(lock).strip().lower()
        if lock not in NATIVE_LIVE_LOCKS:
            continue
        state = str(entry.get("state") or "").strip().lower()
        if state in NATIVE_TERMINAL_STATES:
            continue
        pid = _as_int(entry.get("holder_pid"))
        if pid is None or not probe(pid):
            continue
        return {"run_token": entry.get("run_token"), "holder_pid": pid,
                "state": entry.get("state"),
                "registry_file": entry.get("registry_file")}
    return None

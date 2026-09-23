"""Opt-in cursor-worker subprocess lifecycle events.

Default off: callers pass no events path and this module writes nothing.
Telemetry IO failures never raise to the worker. Records must not include
prompt text, stderr, env dumps, or credentials.

Correlation (run-id / iteration / slice) is explicit CLI or env only.
Do not inherit broker session ids. Do not derive a path from keep-sessions.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "trio.cursor_worker.v1"
# Fixed warning: no path interpolation, no exception text, no prompts.
TELEMETRY_WARN = "trioctl: worker-events write failed; continuing"
BUILDER_ROLES = frozenset({"builder"})
TERMINAL_KINDS = frozenset(
    {
        "exited",
        "nonzero_exit",
        "timeout",
        "interrupted",
        "spawn_failed",
        "unknown",
    }
)
# Timing overlap is not proof of useful concurrent work.
CAUTION = (
    "Observed subprocess lifetime is not proof of useful "
    "concurrent computation. Overlap does not imply readiness, "
    "independence, or a serial scheduling cause."
)


def new_invocation_id() -> str:
    """Always a fresh UUID4. Callers cannot reuse a supplied id."""
    return str(uuid.uuid4())


def boot_id() -> str:
    """Linux boot identity so monotonic values are not mixed across boots."""
    try:
        text = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii", errors="replace"
        )
        ident = text.strip()
        if ident:
            return ident
    except OSError:
        pass
    return "unknown"


def clock_domain() -> str:
    """Host, boot, UTC wall, and monotonic elapsed clocks."""
    host = socket.gethostname()
    return (
        f"host={host};boot={boot_id()};wall=utc;"
        "elapsed=monotonic_ns"
    )


def wall_utc() -> str:
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def resolve_events_path(
    raw: str | None,
    mailbox: str | Path | None = None,
) -> Path | None:
    """Return a path or None when disabled / malformed.

    When mailbox is set, the file must resolve inside it so product
    trees are not used (product guard must not see these writes).
    Relative paths require a mailbox. Empty raw means disabled.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if mailbox:
        try:
            root = Path(mailbox).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved
    if not path.is_absolute():
        # No mailbox: refuse relative paths (unsafe default).
        return None
    try:
        return path.expanduser()
    except (OSError, RuntimeError):
        return None


def shard_dir(path: Path) -> Path:
    """Per-invocation files live next to the named path, not inside FIFOs."""
    return Path(str(path) + ".d")


def _safe_shard_name(invocation_id: str) -> str | None:
    try:
        return str(uuid.UUID(invocation_id)) + ".jsonl"
    except (ValueError, AttributeError, TypeError):
        return None


def _append_regular(path: Path, payload: bytes) -> None:
    """Nonblocking append to a regular file, or raise OSError."""
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def emit(path: Path | None, record: dict[str, Any]) -> None:
    """Append one complete JSON line without blocking the worker.

    Writes ``{path}.d/{invocation}.jsonl`` so concurrent workers do not
    share a lock. Nonblocking open + LOCK_NB; drop with TELEMETRY_WARN
    on FIFO, lock contention, or IO errors. Never raises.
    """
    if path is None:
        return
    try:
        inv = record.get("invocation_id")
        name = _safe_shard_name(inv) if isinstance(inv, str) else None
        if name is None:
            print(TELEMETRY_WARN, file=sys.stderr)
            return
        # allow_nan=False: NaN/Inf must not become JSON numbers.
        line = json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if "\n" in line:
            return
        payload = (line + "\n").encode("utf-8")
        dest_dir = shard_dir(path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        _append_regular(dest_dir / name, payload)
    except (OSError, TypeError, ValueError) as exc:
        # Child-process TimeoutExpired / TimeoutError are not telemetry.
        if type(exc) is TimeoutError:
            raise
        print(TELEMETRY_WARN, file=sys.stderr)


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        chunks: list[bytes] = []
        total = 0
        while True:
            piece = os.read(fd, 65536)
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
            if total > 32 * 1024 * 1024:
                break
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_jsonl(raw: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        piece = line.strip()
        if not piece:
            continue
        try:
            item = json.loads(piece)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("schema") != SCHEMA:
            continue
        if item.get("source") != "cursor_worker":
            continue
        inv = item.get("invocation_id")
        kind = item.get("kind")
        if not isinstance(inv, str) or not inv:
            continue
        if not isinstance(kind, str):
            continue
        rows.append(item)
    return rows


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse JSONL shards; skip malformed bytes/types. Never raise."""
    rows: list[dict[str, Any]] = []
    files: list[Path] = []
    try:
        files.append(path)
        extra = shard_dir(path)
        if extra.is_dir():
            files.extend(sorted(extra.glob("*.jsonl")))
    except OSError:
        return rows
    for file in files:
        try:
            rows.extend(_parse_jsonl(_read_regular_bytes(file)))
        except OSError:
            continue
    return rows


def base_record(
    *,
    invocation_id: str,
    kind: str,
    role: str,
    pid: int | None = None,
    returncode: int | None = None,
    duration_ns: int | None = None,
    run_id: str | None = None,
    iteration: str | None = None,
    slice_id: str | None = None,
    monotonic_ns: int | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "schema": SCHEMA,
        "source": "cursor_worker",
        "invocation_id": invocation_id,
        "kind": kind,
        "role": role,
        "wall_utc": wall_utc(),
        "monotonic_ns": (
            time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        ),
        "clock_domain": clock_domain(),
        "host": socket.gethostname(),
    }
    if pid is not None:
        rec["pid"] = pid
    if returncode is not None:
        rec["returncode"] = returncode
    if duration_ns is not None:
        rec["duration_ns"] = duration_ns
    if run_id:
        rec["run_id"] = run_id
    if iteration:
        rec["iteration"] = str(iteration)
    if slice_id:
        rec["slice"] = slice_id
    return rec


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _duration(start: dict[str, Any], end: dict[str, Any]) -> int | None:
    direct = _as_int(end.get("duration_ns"))
    if direct is not None:
        return direct
    end_m = _as_int(end.get("monotonic_ns"))
    start_m = _as_int(start.get("monotonic_ns"))
    if end_m is None or start_m is None:
        return None
    return end_m - start_m


def pair_builder_runs(
    records: list[dict[str, Any]],
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pair spawned+terminal by invocation_id for builder workers only.

    Scout/docs and session records are ignored. Nonzero exits are
    known-duration failed, not unknown timing. Incomplete is unknown.
    """
    wanted = [r for r in records if r.get("role") in BUILDER_ROLES]
    if run_id:
        wanted = [r for r in wanted if r.get("run_id") == run_id]
    by_id: dict[str, list[dict[str, Any]]] = {}
    for rec in wanted:
        by_id.setdefault(rec["invocation_id"], []).append(rec)

    paired: list[dict[str, Any]] = []
    for inv, events in by_id.items():
        spawned = next((e for e in events if e.get("kind") == "spawned"), None)
        terminal = next(
            (e for e in events if e.get("kind") in TERMINAL_KINDS), None
        )
        row: dict[str, Any] = {"invocation_id": inv}
        # Spawn never started: known outcome, not unknown timing.
        if terminal and terminal.get("kind") == "spawn_failed":
            row["outcome"] = "spawn_failed"
            row["duration_ns"] = None
            paired.append(row)
            continue
        if spawned is None or terminal is None:
            row["outcome"] = "unknown"
            row["duration_ns"] = None
            start_m = _as_int((spawned or {}).get("monotonic_ns"))
            if start_m is not None:
                row["start_monotonic_ns"] = start_m
            paired.append(row)
            continue
        kind = terminal["kind"]
        if kind == "nonzero_exit":
            row["outcome"] = "failed"
        elif kind == "exited":
            code = terminal.get("returncode")
            row["outcome"] = "failed" if code not in (0, None) else "ok"
        else:
            row["outcome"] = kind
        row["duration_ns"] = _duration(spawned, terminal)
        start_m = _as_int(spawned.get("monotonic_ns"))
        if start_m is not None:
            row["start_monotonic_ns"] = start_m
        domain = spawned.get("clock_domain")
        if isinstance(domain, str) and domain:
            row["clock_domain"] = domain
        end_mono = _as_int(terminal.get("monotonic_ns"))
        if end_mono is not None:
            row["end_monotonic_ns"] = end_mono
        elif row["duration_ns"] is not None and start_m is not None:
            row["end_monotonic_ns"] = start_m + row["duration_ns"]
        paired.append(row)
    return paired


def overlap_ns(a: dict[str, Any], b: dict[str, Any]) -> int | None:
    """Positive overlap of known same-boot intervals, else None."""
    domain_a = a.get("clock_domain")
    domain_b = b.get("clock_domain")
    if not isinstance(domain_a, str) or not domain_a:
        return None
    if domain_a != domain_b:
        return None
    a0 = _as_int(a.get("start_monotonic_ns"))
    a1 = _as_int(a.get("end_monotonic_ns"))
    b0 = _as_int(b.get("start_monotonic_ns"))
    b1 = _as_int(b.get("end_monotonic_ns"))
    if None in (a0, a1, b0, b1):
        return None
    if a.get("duration_ns") is None or b.get("duration_ns") is None:
        return None
    lo = max(a0, b0)
    hi = min(a1, b1)
    # Disjoint or touching intervals are not overlap. Never emit 0.
    if hi > lo:
        return hi - lo
    return None


def report(path: Path, run_id: str | None = None) -> dict[str, Any]:
    empty = {
        "builders": [],
        "overlaps": [],
        "caution": CAUTION,
    }
    try:
        records = read_records(path)
        builders = pair_builder_runs(records, run_id=run_id)
        overlaps: list[dict[str, Any]] = []
        for i, left in enumerate(builders):
            for right in builders[i + 1 :]:
                ns = overlap_ns(left, right)
                if ns is None or ns <= 0:
                    continue
                overlaps.append(
                    {
                        "a": left["invocation_id"],
                        "b": right["invocation_id"],
                        "overlap_ns": ns,
                    }
                )
        return {
            "builders": builders,
            "overlaps": overlaps,
            "caution": CAUTION,
        }
    except (OSError, TypeError, ValueError, UnicodeError):
        return empty

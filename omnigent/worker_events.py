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


def new_invocation_id(raw: str | None) -> str:
    """Return a UUID4 string. PID is not unique across hosts or wrap."""
    text = (raw or "").strip()
    if text:
        try:
            return str(uuid.UUID(text))
        except ValueError:
            pass
    return str(uuid.uuid4())


def clock_domain() -> str:
    """Identify host plus UTC wall and monotonic clocks."""
    host = socket.gethostname()
    return f"host={host};wall=utc;elapsed=monotonic_ns"


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


def emit(path: Path | None, record: dict[str, Any]) -> None:
    """Append one complete JSON line with flock. Never raise."""
    if path is None:
        return
    try:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        if "\n" in line:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (line + "\n").encode("utf-8")
        with path.open("ab") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        print(TELEMETRY_WARN, file=sys.stderr)


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


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse JSONL; skip malformed lines. Incomplete last line is dropped."""
    rows: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
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


def _duration(start: dict[str, Any], end: dict[str, Any]) -> int | None:
    if "duration_ns" in end and isinstance(end["duration_ns"], int):
        return end["duration_ns"]
    try:
        return int(end["monotonic_ns"]) - int(start["monotonic_ns"])
    except (KeyError, TypeError, ValueError):
        return None


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
            if spawned and "monotonic_ns" in spawned:
                row["start_monotonic_ns"] = spawned["monotonic_ns"]
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
        row["start_monotonic_ns"] = spawned["monotonic_ns"]
        end_mono = terminal.get("monotonic_ns")
        if isinstance(end_mono, int):
            row["end_monotonic_ns"] = end_mono
        elif isinstance(row["duration_ns"], int):
            row["end_monotonic_ns"] = (
                int(spawned["monotonic_ns"]) + row["duration_ns"]
            )
        paired.append(row)
    return paired


def overlap_ns(a: dict[str, Any], b: dict[str, Any]) -> int | None:
    """Positive overlap of known intervals, else None. No causal claims."""
    try:
        a0 = int(a["start_monotonic_ns"])
        a1 = int(a["end_monotonic_ns"])
        b0 = int(b["start_monotonic_ns"])
        b1 = int(b["end_monotonic_ns"])
    except (KeyError, TypeError, ValueError):
        return None
    if a.get("duration_ns") is None or b.get("duration_ns") is None:
        return None
    lo = max(a0, b0)
    hi = min(a1, b1)
    if hi > lo:
        return hi - lo
    return 0


def report(path: Path, run_id: str | None = None) -> dict[str, Any]:
    records = read_records(path)
    builders = pair_builder_runs(records, run_id=run_id)
    overlaps: list[dict[str, Any]] = []
    for i, left in enumerate(builders):
        for right in builders[i + 1 :]:
            ns = overlap_ns(left, right)
            if ns is None:
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

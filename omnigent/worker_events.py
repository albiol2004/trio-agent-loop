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
import re
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


def _parse_jsonl(
    raw: bytes,
    schema: str = SCHEMA,
    source: str = "cursor_worker",
) -> list[dict[str, Any]]:
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
        if item.get("schema") != schema:
            continue
        if item.get("source") != source:
            continue
        inv = item.get("invocation_id")
        kind = item.get("kind")
        if not isinstance(inv, str) or not inv:
            continue
        if not isinstance(kind, str):
            continue
        rows.append(item)
    return rows


def read_records(
    path: Path,
    schema: str = SCHEMA,
    source: str = "cursor_worker",
) -> list[dict[str, Any]]:
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
            rows.extend(
                _parse_jsonl(_read_regular_bytes(file), schema, source)
            )
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


# --- Loop-level observation (`trioctl omnigent loop --observe-workers`) ---
# Lead/Evaluator phase timing is measured by the loop process. Wave
# decisions are copied from the Lead's REPORT.md and stay agent-reported.
PHASE_SCHEMA = "trio.loop_phase.v1"
SUMMARY_SCHEMA = "trio.observe_summary.v1"
WAVE_HEADING = "## Wave decisions"
WAVE_REPORT_MAX = 4000
SUMMARY_CAUTION = (
    CAUTION + " Phase durations are loop-observed session lifetimes. "
    "Wave decisions are agent-reported, not measured causality."
)
_SECRET_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}"
    r"|bearer\s+[A-Za-z0-9._-]{12,}"
    r"|(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+)"
)


def observe_paths(observe_dir: Path) -> dict[str, Path]:
    return {
        "workers": observe_dir / "workers.jsonl",
        "phases": observe_dir / "phases.jsonl",
        "summary": observe_dir / "summary.json",
    }


def redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def wave_section(report_text: str) -> str | None:
    """Return the Lead's `## Wave decisions` body, redacted and capped."""
    lines = report_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != WAVE_HEADING.lower():
            continue
        body: list[str] = []
        for later in lines[index + 1 :]:
            if later.startswith("## ") or later.startswith("# "):
                break
            body.append(later)
        text = "\n".join(body).strip()
        if not text:
            return None
        return redact(text)[:WAVE_REPORT_MAX]
    return None


def phase_record(
    *,
    invocation_id: str,
    kind: str,
    role: str,
    run_id: str,
    iteration: int,
    **fields: Any,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "schema": PHASE_SCHEMA,
        "source": "loop_phase",
        "invocation_id": invocation_id,
        "kind": kind,
        "role": role,
        "run_id": run_id,
        "iteration": str(iteration),
        "wall_utc": wall_utc(),
        "monotonic_ns": time.monotonic_ns(),
        "clock_domain": clock_domain(),
    }
    rec.update({k: v for k, v in fields.items() if v is not None})
    return rec


def _union_ns(rows: list[dict[str, Any]]) -> int | None:
    """Wall time covered by at least one builder; None if not all known."""
    if not rows:
        return None
    domains = {r.get("clock_domain") for r in rows}
    spans: list[tuple[int, int]] = []
    for row in rows:
        start = _as_int(row.get("start_monotonic_ns"))
        end = _as_int(row.get("end_monotonic_ns"))
        if start is None or end is None or row.get("duration_ns") is None:
            return None
        spans.append((start, end))
    if len(domains) != 1 or None in domains:
        return None
    spans.sort()
    total = 0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    return total + cur_end - cur_start


def _pair_phases(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_id.setdefault(rec["invocation_id"], []).append(rec)
    phases: list[dict[str, Any]] = []
    for inv, events in by_id.items():
        start = next((e for e in events if e.get("kind") == "start"), None)
        end = next((e for e in events if e.get("kind") == "end"), None)
        first = start or end or {}
        row: dict[str, Any] = {
            "invocation_id": inv,
            "role": first.get("role"),
            "iteration": first.get("iteration"),
            "started_utc": (start or {}).get("wall_utc"),
        }
        if first.get("open_loop_kind"):
            row["open_loop_kind"] = first["open_loop_kind"]
        if start is None or end is None:
            # Crash, SIGKILL, or a still-running pass: never guess.
            row["outcome"] = "unknown"
            row["duration_ns"] = None
        else:
            row["outcome"] = end.get("outcome") or "unknown"
            row["duration_ns"] = _duration(start, end)
            if "wave_report" in end:
                row["wave_report"] = end.get("wave_report")
                row["wave_report_source"] = "agent_reported"
        phases.append(row)
    phases.sort(key=lambda r: (r.get("started_utc") or "", r["invocation_id"]))
    return phases


def summarize(observe_dir: Path, run_id: str) -> dict[str, Any]:
    """Per-iteration builder overlap + Lead/Evaluator phase timing.

    Offline and read-only over one observe run directory. Anything not
    proven by paired records is reported as unknown, never as zero.
    """
    paths = observe_paths(observe_dir)
    all_workers = read_records(paths["workers"])
    workers = [r for r in all_workers if r.get("run_id") == run_id]
    stray = [r for r in all_workers if r.get("run_id") != run_id]
    phases = _pair_phases(
        [
            r
            for r in read_records(paths["phases"], PHASE_SCHEMA, "loop_phase")
            if r.get("run_id") == run_id
        ]
    )
    labels: dict[str, tuple[str | None, str | None]] = {}
    for rec in workers:
        labels.setdefault(
            rec["invocation_id"], (rec.get("iteration"), rec.get("slice"))
        )
    other_roles: dict[str, int] = {}
    seen_other: set[str] = set()
    for rec in workers:
        role = rec.get("role")
        if role in BUILDER_ROLES or rec["invocation_id"] in seen_other:
            continue
        seen_other.add(rec["invocation_id"])
        key = f"{rec.get('iteration') or '?'}:{role}"
        other_roles[key] = other_roles.get(key, 0) + 1

    iterations: dict[str, dict[str, Any]] = {}

    def bucket(key: str) -> dict[str, Any]:
        return iterations.setdefault(
            key,
            {
                "phases": [],
                "builders": [],
                "overlaps": [],
                "unknowns": [],
            },
        )

    for phase in phases:
        bucket(str(phase.get("iteration") or "unlabelled"))["phases"].append(
            phase
        )
    for row in pair_builder_runs(workers, run_id=run_id):
        iteration, slice_id = labels.get(row["invocation_id"], (None, None))
        row["slice"] = slice_id
        bucket(str(iteration or "unlabelled"))["builders"].append(row)

    unknowns: list[str] = []
    if stray:
        unknowns.append(
            f"{len({r['invocation_id'] for r in stray})} worker invocation(s) "
            "in this file carry a different or missing run id; excluded"
        )
    for key, item in sorted(iterations.items()):
        builders = item["builders"]
        for i, left in enumerate(builders):
            for right in builders[i + 1 :]:
                ns = overlap_ns(left, right)
                if ns:
                    item["overlaps"].append(
                        {
                            "a": left["invocation_id"],
                            "a_slice": left.get("slice"),
                            "b": right["invocation_id"],
                            "b_slice": right.get("slice"),
                            "overlap_ns": ns,
                        }
                    )
        known = [b for b in builders if b.get("duration_ns") is not None]
        item["builder_count"] = len(builders)
        item["builder_sum_ns"] = (
            sum(b["duration_ns"] for b in known)
            if known and len(known) == len(builders)
            else None
        )
        item["builder_union_ns"] = _union_ns(builders)
        notes = item["unknowns"]
        if key == "unlabelled":
            notes.append("records without an iteration label")
        for b in builders:
            if b["outcome"] == "unknown":
                notes.append(
                    f"builder {b['invocation_id']} has no paired "
                    "spawn/terminal events (duration unknown)"
                )
            if not b.get("slice"):
                notes.append(
                    f"builder {b['invocation_id']} has no slice label"
                )
        lead_like = [
            p for p in item["phases"] if p.get("role") in ("lead", "repair")
        ]
        if lead_like and not builders:
            notes.append(
                "no builder events recorded: the Lead may have worked "
                "alone or dispatched without the observed command"
            )
        for p in item["phases"]:
            if p["outcome"] == "unknown":
                notes.append(
                    f"{p.get('role')} phase {p['invocation_id']} has no "
                    "end record (duration unknown)"
                )
            if p.get("role") in ("lead", "repair") and not p.get(
                "wave_report"
            ):
                notes.append(
                    f"{p.get('role')} phase {p['invocation_id']}: no "
                    f"'{WAVE_HEADING}' section captured from REPORT.md"
                )
        other = {
            k.split(":", 1)[1]: v
            for k, v in other_roles.items()
            if k.split(":", 1)[0] == key
        }
        if other:
            item["other_workers"] = other
    return {
        "schema": SUMMARY_SCHEMA,
        "run_id": run_id,
        "iterations": iterations,
        "unknowns": unknowns,
        "caution": SUMMARY_CAUTION,
    }


def summary_lines(summary: dict[str, Any]) -> list[str]:
    """Short human lines for stderr at loop exit."""
    def secs(ns: Any) -> str:
        return "unknown" if ns is None else f"{ns / 1e9:.1f}s"

    lines = [f"observe run {summary.get('run_id')}:"]
    for key, item in sorted(summary.get("iterations", {}).items()):
        phase_bits = ", ".join(
            f"{p.get('role')} {secs(p.get('duration_ns'))} ({p.get('outcome')})"
            for p in item.get("phases", [])
        ) or "no phases"
        overlap = sum(o["overlap_ns"] for o in item.get("overlaps", []))
        lines.append(
            f"  iter {key}: {phase_bits}; builders {item.get('builder_count', 0)}"
            f", overlap pairs {len(item.get('overlaps', []))}"
            + (f" ({secs(overlap)})" if overlap else "")
            + f", unknowns {len(item.get('unknowns', []))}"
        )
    lines.append("  " + SUMMARY_CAUTION)
    return lines

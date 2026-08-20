#!/usr/bin/env python3
"""serve.py — read-only web dashboard backend for trio loops.

A stdlib-only HTTP server (Python 3.7+) that renders loop mailboxes as a
live status board and tails omp agent session transcripts over SSE. It is
strictly read-only: no endpoint mutates loops, sessions, or the repo.

Usage:
    python3 dashboard/serve.py [--host 127.0.0.1] [--port <port>] [--root <dir>]

With no --port, the first free port in the range 9470-9479 is used
(override the range with the TRIO_DASH_PORTS env var, e.g. "9500-9509").

`--root` defaults to the current working directory and is scanned top-level
for `loop*/` mailbox directories. Parsing logic for mailboxes is reused from
`metrics/trio-metrics.py` (loaded by path relative to this file, never
duplicated).

API contract
------------
Static files:
    GET /            -> dashboard/index.html
    GET /app.css     -> dashboard/app.css
    GET /app.js      -> dashboard/app.js

Board:
    GET /api/board
    Response: {"loops": [<loop>], "updated_at": "<ISO-8601 UTC>"}
    Each loop object:
        name, path, mission, iteration, max_iterations, status,
        final_verdict, last_activity, last_entry_summary, segments

Sessions:
    GET /api/sessions?loop=<loop-name>
    Response: [{"id", "label", "timestamp", "path", "size",
                "kind", "parent_id", "parent_path"}, ...]
    Top-level <ISO-TS>_<id>.jsonl files are "parent" sessions and are
    listed newest first; nested .jsonl files (subagent transcripts with
    arbitrary names) follow them with kind "subagent", plus the parent
    session's file stem ("parent_id") and absolute path ("parent_path").
    The loop's absolute path is mapped to an omp session slug ("-" +
    path-relative-to-$HOME with "/" -> "-"); if that slug directory does
    not exist, the project root's slug is used as a fallback (sessions
    are keyed by the cwd of the omp run).

Transcript tail (SSE):
    GET /api/transcript?path=<absolute-path>&offset=<bytes>
    Content-Type: text/event-stream; charset=utf-8
    Events:
        event: init   data: {"offset": <int>, "size": <int>}
        event: line   data: {"offset": <byte-offset-after-line>, "record": <obj>}
        event: error  data: {"error": "<message>"}   (then the stream closes)
    Path is validated to resolve under ~/.omp/agent/sessions/. Incomplete
    final lines are buffered until more bytes arrive. The stream polls the
    file every ~500 ms and emits a ":heartbeat" comment every ~15 s.
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
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

DASHBOARD_DIR = Path(__file__).resolve().parent
"""Directory this file lives in; static frontend files are served from here."""

METRICS_PATH = DASHBOARD_DIR.parent / "metrics" / "trio-metrics.py"
"""Parsing module, resolved relative to this file (NOT cwd)."""

SHADOW_PATH = DASHBOARD_DIR.parent / "metrics" / "trio-shadow.py"
"""Slice shadow module (slice-commit/git attribution), resolved like METRICS_PATH."""

SESSIONS_ROOT = Path.home() / ".omp" / "agent" / "sessions"
"""Root under which omp session JSONL files live."""

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

SESSION_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\dTZ:\-]+_[0-9a-fA-F\-]+\.jsonl$")
"""Filename shape of top-level omp session transcripts: <ISO-TS>_<id>.jsonl."""

NESTED_SESSION_FILE_RE = re.compile(r"^[^.\s][^/]*\.jsonl$")
"""Relaxed shape for nested (subagent) transcripts: any ``.jsonl`` name.

Applied only to files nested under the slug directory, so arbitrary names
like ``EvalIter1.SessionScout.jsonl`` are accepted while top-level files
still must match ``SESSION_FILE_RE``.
"""

POLL_SECONDS = 0.5
"""Transcript tail poll interval."""

HEARTBEAT_SECONDS = 15.0
"""Transcript heartbeat comment interval."""

# --------------------------------------------------------------------------
# metrics/trio-metrics.py loading (no regex duplication)
# --------------------------------------------------------------------------


_METRICS_MODULE = None
"""Cached metrics module; loaded once and shared by the server and helpers."""


def load_metrics_module():
    """Load metrics/trio-metrics.py via importlib and return the module.

    The hyphenated filename cannot be imported normally, so it is loaded
    by file location relative to this file. Only public functions are used
    (discover_loops, analyze_loop, parse_log, parse_timeline,
    parse_slices_block); no parsing regex is copied. The module is loaded
    once and cached.
    """
    global _METRICS_MODULE
    if _METRICS_MODULE is not None:
        return _METRICS_MODULE
    spec = importlib.util.spec_from_file_location("trio_metrics", METRICS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load metrics module: {METRICS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in ("discover_loops", "analyze_loop", "parse_log", "parse_timeline",
               "parse_slices_block"):
        if not hasattr(module, fn):
            raise RuntimeError(f"metrics module missing required function: {fn}")
    _METRICS_MODULE = module
    return module


_SHADOW_MODULE = None
"""Cached shadow module; loaded once, None when unavailable (slice_activity
degrades to null in that case)."""


def load_shadow_module():
    """Load metrics/trio-shadow.py via importlib and return the module.

    Same by-path loading as ``load_metrics_module``. The dashboard reuses
    its slice-commit/git-attribution functions (find_slices_block,
    parse_slices, analyze_slice) instead of duplicating them. Any load
    failure raises; callers degrade gracefully.
    """
    global _SHADOW_MODULE
    if _SHADOW_MODULE is not None:
        return _SHADOW_MODULE
    spec = importlib.util.spec_from_file_location("trio_shadow", SHADOW_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load shadow module: {SHADOW_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in ("find_slices_block", "parse_slices", "analyze_slice"):
        if not hasattr(module, fn):
            raise RuntimeError(f"shadow module missing required function: {fn}")
    _SHADOW_MODULE = module
    return module


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    """Format a datetime as 'YYYY-MM-DDTHH:MM:SSZ' (UTC, second precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_int(value) -> int | None:
    """Coerce a state_* field to int; return None when it is not numeric."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return None


def _mission_from_goal(goal_path: Path, limit: int = 120) -> str:
    """Extract the mission from GOAL.md.

    Rule: the first non-empty line that is not a markdown heading marker,
    skipping YAML-ish metadata lines such as ``profile: software``; an
    explicit ``mission:`` line wins over a prose paragraph. Falls back to
    the first heading's text when nothing else exists. Capped at ``limit``
    characters.
    """
    if not goal_path.is_file():
        return ""
    heading = None
    try:
        with goal_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if heading is None:
                        heading = line.lstrip("#").strip()
                    continue
                meta = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
                if meta:
                    key, value = meta.group(1).lower(), meta.group(2)
                    if key == "mission" and value:
                        text = value
                        break
                    # Treat any other key:value line as frontmatter and skip it.
                    continue
                text = line
                break
            else:
                text = heading or ""
    except OSError:
        return ""
    text = text.strip()
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _last_activity(loop_dir: Path, entries: list[dict]) -> str | None:
    """Most recent of LOG/STATE/VERDICT mtimes or the newest parsed entry date."""
    candidates: list[datetime] = []
    for name in ("LOG.md", "STATE.md", "VERDICT.md"):
        try:
            p = loop_dir / name
            if p.is_file():
                candidates.append(datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))
        except OSError:
            continue
    for entry in entries:
        date = entry.get("date")
        if date:
            try:
                candidates.append(
                    datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                )
            except ValueError:
                continue
    return _utc_iso(max(candidates)) if candidates else None


def _last_entry_summary(entries: list[dict]) -> str:
    """Short human summary of the last parsed LOG.md entry."""
    if not entries:
        return "no activity"
    entry = entries[-1]
    parts = []
    if entry.get("iter") is not None:
        parts.append(f"iter {entry['iter']}")
    parts.append(entry.get("role", "?"))
    if entry.get("verdict"):
        parts.append(entry["verdict"])
    return " | ".join(parts)


def _loop_timeline(log_path: Path) -> list[dict]:
    """Parse LOG.md into the full ordered list of parsed entries.

    Every parsed entry is returned in file order (no per-(iter, role)
    merging — the drawer groups and dedups frontend-side). Delegates to
    metrics.parse_timeline so all LOG parsing stays in trio-metrics.py.
    """
    return load_metrics_module().parse_timeline(log_path)


def _loop_slices(loop_dir: Path) -> list[dict] | None:
    """The parsed PLAN.md ```yaml slices: block, or None when absent/unparseable.

    Uses metrics.parse_slices_block (the lenient wrapper): a missing PLAN.md
    or a missing/malformed slices block yields None, never an error.
    """
    plan_path = loop_dir / "PLAN.md"
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return load_metrics_module().parse_slices_block(text)


def _loop_slice_activity(loop_dir: Path, root: Path) -> dict | None:
    """Shadow drift for a loop's slices, or None when not applicable.

    Reuses metrics/trio-shadow.py's slice-commit/git attribution (loaded by
    path via importlib, same as the metrics module) and shapes the result
    per the API contract: {"slices": [{"id", "declared_writes",
    "actual_writes", "undeclared", "commits"}]}. Slice repos resolve
    relative to the mailbox root — the project root the dashboard scans.
    Any failure (no PLAN.md, no slices block, a missing/non-git repo, or a
    shadow-script load error) yields None, never a 500.
    """
    try:
        shadow = load_shadow_module()
        text = (loop_dir / "PLAN.md").read_text(encoding="utf-8", errors="replace")
        slices = shadow.parse_slices(shadow.find_slices_block(text))
    except Exception:
        return None
    entries = []
    for sl in slices:
        entry = shadow.analyze_slice(sl, root)
        if entry["repo_status"] != "ok":
            return None  # cannot attribute commits -> no meaningful drift data
        entries.append({
            "id": entry["id"],
            "declared_writes": entry["declared_writes"],
            "actual_writes": entry["actual_touched"],
            "undeclared": entry["undeclared_touches"],
            "commits": entry["commits"],
        })
    return {"slices": entries}


def _loop_iterations(loop_dir: Path, root: Path) -> tuple[list[dict], list[dict]]:
    """Per-iteration lifecycle + overlaps for a loop.

    All parsing lives in metrics/trio-metrics.py (derive_iterations /
    iteration_path_sets / iteration_overlaps); this helper only loads the
    mailbox inputs. Any failure yields ([], []), never a 500.
    """
    try:
        metrics = load_metrics_module()
        state = metrics.parse_state(loop_dir / "STATE.md")
        timeline = metrics.parse_timeline(loop_dir / "LOG.md")
        slices = _loop_slices(loop_dir) or []
        try:
            verdict_text = (loop_dir / "VERDICT.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            verdict_text = ""
        try:
            report_text = (loop_dir / "REPORT.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            report_text = ""
        activity = _loop_slice_activity(loop_dir, root)
        iterations = metrics.derive_iterations(
            state, timeline, slices,
            verdict_text=verdict_text, report_text=report_text,
            slice_activity=activity)
        overlaps = metrics.iteration_overlaps(
            iterations, metrics.iteration_path_sets(slices))
        return iterations, overlaps
    except Exception:
        traceback.print_exc()
        return [], []


_SLICE_COMMIT_RE = re.compile(r"^slice\(([^)]+)\):\s*(.*)$")
"""Conventional slice commit subject: ``slice(<id>): <subject>``."""

STALE_MINUTES = 45
"""An active-looking loop with no activity for this long is flagged stale."""

_ACTIVE_STATUS_WORDS = ("running", "pending", "in_progress", "planning", "building")


def _loop_commits(loop_dir: Path, root: Path) -> list[dict]:
    """Git metadata for a loop: slice-attributed commits newest first.

    Reads ``git log`` of the coordination repo (the mailbox's parent root)
    and keeps subjects matching the conventional ``slice(<id>): `` prefix
    (MAILBOX-SCHEMA.md). Not a git repo / git missing / any failure -> [].
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "--format=%H%x09%h%x09%s", "-n", "200"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    commits = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        sha, short, subject = parts
        m = _SLICE_COMMIT_RE.match(subject)
        if not m:
            continue
        commits.append({
            "sha": sha,
            "short": short,
            "slice": m.group(1),
            "subject": m.group(2),
        })
    return commits


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _inbox_items(loop_dir: Path, card: dict, root: Path) -> list[dict]:
    """Attention signals for one loop, highest severity first.

    Kinds: needs_human / blocked (high), stale / drift (medium),
    repair (low). Anything that cannot be determined is simply absent —
    the inbox never guesses.
    """
    items = []

    def add(severity, kind, headline, detail):
        items.append({
            "loop": card["name"],
            "kind": kind,
            "severity": severity,
            "headline": headline,
            "detail": detail,
        })

    verdict = (card.get("final_verdict") or "").upper()
    if verdict == "NEEDS_HUMAN":
        add("high", "needs_human", "Human verification pending",
            "Agent-verifiable criteria pass; verify: human criteria remain.")
    elif verdict == "BLOCKED":
        add("high", "blocked", "Loop blocked",
            card.get("last_entry_summary") or "")

    status = (card.get("status") or "").lower()
    if any(w in status for w in _ACTIVE_STATUS_WORDS):
        last = _parse_iso(card.get("last_activity"))
        if last is not None:
            idle = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if idle >= STALE_MINUTES:
                hours = idle / 60
                span = f"{int(hours)}h" if hours >= 1 else f"{int(idle)}m"
                add("medium", "stale", f"No activity for {span}",
                    f"Status is '{card['status']}' but the mailbox has not moved.")

    plan = loop_dir / "PLAN.md"
    try:
        has_slices = "slices:" in plan.read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        has_slices = False
    if has_slices:
        activity = _loop_slice_activity(loop_dir, root)
        if activity:
            drift_files = set()
            drift_slices = 0
            for sl in activity["slices"]:
                if sl["undeclared"]:
                    drift_slices += 1
                    drift_files.update(sl["undeclared"])
            if drift_files:
                add("medium", "drift",
                    f"{len(drift_files)} undeclared write"
                    f"{'s' if len(drift_files) != 1 else ''}",
                    f"Across {drift_slices} slice"
                    f"{'s' if drift_slices != 1 else ''}: "
                    + ", ".join(sorted(drift_files)[:4])
                    + ("…" if len(drift_files) > 4 else ""))

    try:
        _, overlaps = _loop_iterations(loop_dir, root)
        for ov in overlaps:
            paths = ov["paths"]
            shown = ", ".join(paths[:6]) + ("\u2026" if len(paths) > 6 else "")
            verb = ("share write paths" if ov["relation"] == "write-write"
                    else "overlap read/write paths" if ov["relation"] == "write-read"
                    else "share write paths and read/write paths")
            add("medium", "overlap",
                f"Iterations {ov['a']} and {ov['b']} {verb}", shown)
    except Exception:
        traceback.print_exc()

    try:
        repairs = int((loop_dir / ".repairs").read_text().strip())
    except (OSError, ValueError):
        repairs = 0
    if repairs >= 1:
        add("low", "repair", f"{repairs} consecutive scoped repair"
             f"{'s' if repairs != 1 else ''}",
            "Repair-only loop risk: a full Lead pass is forced at 2.")

    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order[i["severity"]])
    return items


# --------------------------------------------------------------------------
# Session discovery
# --------------------------------------------------------------------------


def _session_slug(path: Path) -> str | None:
    """omp session slug for a path: '-' + relative-to-$HOME with '/' -> '-'."""
    try:
        rel = Path(path).resolve().relative_to(Path.home())
    except (ValueError, OSError):
        return None
    return "-" + str(rel).replace("/", "-")


def _session_files_for_loop(loop_dir: Path, root: Path) -> list[dict]:
    """Session descriptors for a loop, first matching slug directory wins.

    Each descriptor is {"path", "kind", "parent_id", "parent_path"}:
      - "parent": a top-level <ISO-TS>_<id>.jsonl file directly under the
        slug directory (SESSION_FILE_RE).
      - "subagent": any other .jsonl file nested under the slug directory
        (relaxed NESTED_SESSION_FILE_RE), discovered recursively so
        subagent transcripts are never hidden from the UI.

    The slug is derived from the loop's absolute path per the API contract.
    Sessions are keyed by the omp run's cwd, so when the loop-specific slug
    directory does not exist we fall back to the project root's slug (the
    cwd under which this loop lives).
    """
    candidates: list[Path] = []
    seen = set()
    for base in (loop_dir, root):
        slug = _session_slug(base)
        if slug:
            d = SESSIONS_ROOT / slug
            if d not in seen:
                seen.add(d)
                candidates.append(d)
    for directory in candidates:
        try:
            top_level = [
                p for p in directory.iterdir()
                if p.is_file() and SESSION_FILE_RE.match(p.name)
            ]
            nested = [
                p for p in directory.rglob("*.jsonl")
                if p.is_file() and p.parent != directory
                and not p.name.startswith(".")
                and NESTED_SESSION_FILE_RE.match(p.name)
            ]
        except OSError:
            continue
        if top_level or nested:
            return _session_descriptors(top_level, nested)
    return []


def _session_descriptors(top_level: list[Path], nested: list[Path]) -> list[dict]:
    """One descriptor per session file: parents first, subagents after.

    A subagent's parent session is the ``<parent-dir-name>.jsonl`` file
    sitting next to its directory; ``parent_id`` is that file's stem (the
    directory name) and ``parent_path`` its absolute path, or None when
    the file does not exist (e.g. a bare fixture dropped under the slug
    directory).
    """
    descriptors = [
        {"path": p, "kind": "parent", "parent_id": None, "parent_path": None}
        for p in top_level
    ]
    for p in nested:
        parent_file = p.parent.parent / (p.parent.name + ".jsonl")
        descriptors.append({
            "path": p,
            "kind": "subagent",
            "parent_id": p.parent.name,
            "parent_path": str(parent_file.resolve()) if parent_file.is_file() else None,
        })
    return descriptors


_FILENAME_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T)(\d{2})-(\d{2})-(\d{2})-(\d{3}Z)")


def _timestamp_from_stem(stem: str) -> str:
    """Normalize the filename timestamp (dashes) to ISO-8601 (colons)."""
    m = _FILENAME_TS_RE.match(stem)
    if m:
        return f"{m.group(1)}{m.group(2)}:{m.group(3)}:{m.group(4)}.{m.group(5)}"
    return stem


def _parse_session_file(path: Path) -> dict:
    """Parse one <ISO-TS>_<id>.jsonl session file.

    The `session` record sits on line 2 (line 1 is a `title` record). If it
    cannot be parsed, fall back to filename-derived id/timestamp so real
    session files are never hidden from the UI.
    """
    stem = path.name[: -len(".jsonl")]
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.readline()  # line 1: title record
            record = json.loads(fh.readline() or "null")
        if not (isinstance(record, dict) and record.get("type") == "session"):
            raise ValueError("line 2 is not a session record")
        session_id = record.get("id") or stem.rsplit("_", 1)[-1]
        timestamp = record.get("timestamp") or _timestamp_from_stem(stem)
    except (OSError, ValueError, json.JSONDecodeError):
        session_id = stem.rsplit("_", 1)[-1]
        timestamp = _timestamp_from_stem(stem)
    return {
        "id": session_id,
        "label": stem,
        "timestamp": timestamp,
        "path": str(path.resolve()),
        "size": path.stat().st_size,
    }


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the dashboard API and static files."""

    server_version = "TrioLoopDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _api(self, handler) -> None:
        """Run an API handler; convert unexpected failures into 500 JSON."""
        try:
            handler()
        except Exception:
            traceback.print_exc()
            try:
                self._send_json(500, {"error": "internal server error"})
            except OSError:
                pass

    # -- static files ------------------------------------------------------

    def _serve_static(self, name: str, ctype: str) -> None:
        path = DASHBOARD_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            return self._send_text(404, f"{name} not found")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- /api/board --------------------------------------------------------

    def _loop_card(self, loop_dir: Path, metrics) -> dict:
        analysis = metrics.analyze_loop(loop_dir)
        entries = metrics.parse_log(loop_dir / "LOG.md")
        return {
            "name": analysis["name"],
            "path": analysis["name"],
            "mission": _mission_from_goal(loop_dir / "GOAL.md"),
            "iteration": _to_int(analysis["state_iteration"]),
            "max_iterations": _to_int(analysis["state_max_iterations"]),
            "status": analysis["state_status"] or "unknown",
            "final_verdict": analysis["final_verdict"],
            "last_activity": _last_activity(loop_dir, entries),
            "last_entry_summary": _last_entry_summary(entries),
            "segments": analysis["segments"],
        }

    def _handle_board(self) -> None:
        metrics = self.server.metrics
        loops = []
        for loop_dir in metrics.discover_loops(self.server.root):
            try:
                loops.append(self._loop_card(loop_dir, metrics))
            except Exception:
                traceback.print_exc()
                # Keep the board alive even if one loop's mailbox is broken.
                loops.append({
                    "name": loop_dir.name,
                    "path": loop_dir.name,
                    "mission": "",
                    "iteration": None,
                    "max_iterations": None,
                    "status": "unknown",
                    "final_verdict": None,
                    "last_activity": None,
                    "last_entry_summary": "unreadable mailbox",
                    "segments": [],
                })
        inbox = []
        for loop_dir, card in zip(
            list(metrics.discover_loops(self.server.root)), loops
        ):
            try:
                inbox.extend(_inbox_items(loop_dir, card, self.server.root))
            except Exception:
                traceback.print_exc()
        order = {"high": 0, "medium": 1, "low": 2}
        inbox.sort(key=lambda i: (order[i["severity"]], i["loop"]))
        self._send_json(200, {
            "loops": loops,
            "inbox": inbox,
            "updated_at": _utc_iso(datetime.now(timezone.utc)),
        })

    # -- /api/sessions -----------------------------------------------------

    def _session_list(self, loop_dir: Path) -> list[dict]:
        """Parents first (newest first), then subagents, for one loop."""
        sessions = []
        for desc in _session_files_for_loop(loop_dir, self.server.root):
            try:
                session = _parse_session_file(desc["path"])
            except OSError:
                continue
            session["kind"] = desc["kind"]
            session["parent_id"] = desc["parent_id"]
            session["parent_path"] = desc["parent_path"]
            sessions.append(session)
        parents = [s for s in sessions if s["kind"] == "parent"]
        subagents = [s for s in sessions if s["kind"] != "parent"]
        parents.sort(key=lambda s: (s["timestamp"], s["label"]), reverse=True)
        subagents.sort(key=lambda s: (s["timestamp"], s["label"]), reverse=True)
        return parents + subagents

    def _find_loop_dir(self, name: str) -> Path | None:
        metrics = self.server.metrics
        return next(
            (p for p in metrics.discover_loops(self.server.root) if p.name == name),
            None,
        )

    def _handle_sessions(self, query: dict) -> None:
        name = (query.get("loop") or [None])[0]
        if not name:
            return self._send_json(400, {"error": "missing 'loop' parameter"})
        loop_dir = self._find_loop_dir(name)
        if loop_dir is None:
            return self._send_json(400, {"error": f"unknown loop: {name}"})
        self._send_json(200, self._session_list(loop_dir))

    # -- /api/loop (detail) --------------------------------------------------

    def _handle_loop_detail(self, query: dict) -> None:
        name = (query.get("name") or [None])[0]
        if not name:
            return self._send_json(400, {"error": "missing 'name' parameter"})
        loop_dir = self._find_loop_dir(name)
        if loop_dir is None:
            return self._send_json(400, {"error": f"unknown loop: {name}"})
        card = self._loop_card(loop_dir, self.server.metrics)
        card["mission"] = _mission_from_goal(loop_dir / "GOAL.md", limit=4000)
        card["timeline"] = _loop_timeline(loop_dir / "LOG.md")
        card["commits"] = _loop_commits(loop_dir, self.server.root)
        card["slices"] = _loop_slices(loop_dir)
        card["slice_activity"] = _loop_slice_activity(loop_dir, self.server.root)
        card["iterations"], card["overlaps"] = _loop_iterations(
            loop_dir, self.server.root)
        card["sessions"] = self._session_list(loop_dir)
        self._send_json(200, card)

    # -- /api/transcript (SSE) ---------------------------------------------

    def _sse_start(self) -> bool:
        """Send SSE response headers; False if the client is already gone."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            return True
        except OSError:
            return False

    def _sse_event(self, wfile, event: str, data) -> None:
        wfile.write(f"event: {event}\n".encode("ascii"))
        wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))

    def _sse_error(self, error: dict) -> None:
        """Send an error event (best effort) and close the stream."""
        try:
            if self._sse_start():
                self._sse_event(self.wfile, "error", error)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.close_connection = True

    def _validate_transcript_params(self, query: dict) -> tuple[int, Path]:
        """Validate `path`/`offset` for the transcript endpoint.

        Raises ValueError with a client-safe message when invalid.
        """
        offset = 0
        offset_str = (query.get("offset") or ["0"])[0]
        try:
            offset = int(offset_str)
        except (TypeError, ValueError):
            raise ValueError("invalid offset")
        if offset < 0:
            raise ValueError("invalid offset")

        path_str = (query.get("path") or [None])[0]
        if not path_str:
            raise ValueError("invalid session path")
        try:
            target = Path(path_str).expanduser().resolve()
        except (OSError, RuntimeError):
            raise ValueError("invalid session path")
        try:
            target.relative_to(SESSIONS_ROOT.resolve())
        except ValueError:
            raise ValueError("invalid session path")
        if not target.is_file():
            raise ValueError("session file not found")
        return offset, target

    def _stream_transcript(self, target: Path, offset: int) -> None:
        """Tail `target` from `offset` as SSE line events until disconnect."""
        wfile = self.wfile
        size = target.stat().st_size
        offset = min(offset, size)
        self._sse_event(wfile, "init", {"offset": offset, "size": size})
        wfile.flush()

        pending = b""  # incomplete line (no trailing newline yet)
        cursor = offset  # absolute byte offset of the next read

        # If resuming from an arbitrary offset that is not at a line boundary,
        # skip the first partial line so we never emit a truncated JSON object.
        if 0 < offset < size:
            try:
                with target.open("rb") as check:
                    check.seek(offset - 1)
                    if check.read(1) != b"\n":
                        # Mid-line: read and discard up to the next newline.
                        check.seek(offset)
                        skip = check.read(min(65536, size - offset))
                        nl = skip.find(b"\n")
                        if nl != -1:
                            cursor = offset + nl + 1
                            pending = b""
                        else:
                            # No newline yet; wait for more data normally.
                            cursor = offset
            except OSError:
                cursor = offset
        next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

        with target.open("rb") as fh:
            while True:
                if cursor < size:
                    fh.seek(cursor)
                    chunk = fh.read(65536)
                    cursor += len(chunk)
                    if chunk:
                        data = pending + chunk
                        parts = data.split(b"\n")
                        pending = parts.pop()
                        pos = cursor - len(data)  # absolute offset of data[0]
                        for part in parts:
                            pos += len(part) + 1  # byte offset after this line
                            if part:
                                try:
                                    record = json.loads(part.decode("utf-8"))
                                except (UnicodeDecodeError, json.JSONDecodeError):
                                    record = part.decode("utf-8", errors="replace")
                                self._sse_event(wfile, "line", {"offset": pos, "record": record})
                        wfile.flush()
                try:
                    size = target.stat().st_size
                except OSError:
                    break  # file vanished mid-stream
                if cursor < size:
                    continue  # more to read: poll again immediately
                now = time.monotonic()
                if now >= next_heartbeat:
                    wfile.write(b":heartbeat\n\n")
                    wfile.flush()
                    next_heartbeat = now + HEARTBEAT_SECONDS
                time.sleep(POLL_SECONDS)

    def _handle_transcript(self, query: dict) -> None:
        try:
            offset, target = self._validate_transcript_params(query)
        except ValueError as exc:
            return self._sse_error({"error": str(exc)})
        except Exception:
            traceback.print_exc()
            return self._sse_error({"error": "internal server error"})
        if not self._sse_start():
            return
        try:
            self._stream_transcript(target, offset)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # client disconnected — close cleanly
        except OSError:
            pass  # socket gone or file vanished
        except Exception:
            traceback.print_exc()
        finally:
            self.close_connection = True

    # -- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in STATIC_ROUTES:
            name, ctype = STATIC_ROUTES[path]
            return self._serve_static(name, ctype)
        if path == "/api/board":
            return self._api(self._handle_board)
        if path == "/api/loop":
            return self._api(lambda: self._handle_loop_detail(query))
        if path == "/api/sessions":
            return self._api(lambda: self._handle_sessions(query))
        if path == "/api/transcript":
            return self._handle_transcript(query)
        if path.startswith("/api/"):
            return self._send_json(404, {"error": "not found"})
        return self._send_text(404, "not found")


class DashboardServer(ThreadingHTTPServer):
    """Threaded server carrying the project root and the loaded metrics module."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path):
        self.root = Path(root).resolve()
        self.metrics = load_metrics_module()
        super().__init__(address, DashboardHandler)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Trio Loop Dashboard — read-only status board and "
                    "transcript viewer for trio loop mailboxes."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="bind port (default: first free port in the TRIO_DASH_PORTS range, 9470-9479)")
    parser.add_argument("--root", default=".", help="project root scanned for loop*/ dirs (default: cwd)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: --root {args.root} is not a directory", file=sys.stderr)
        return 2

    port_range = os.environ.get("TRIO_DASH_PORTS", "9470-9479")
    try:
        range_start, range_end = (int(p) for p in port_range.split("-", 1))
    except ValueError:
        print(f"error: invalid TRIO_DASH_PORTS range {port_range!r} (expected START-END)", file=sys.stderr)
        return 2
    candidate_ports = [args.port] if args.port is not None else list(range(range_start, range_end + 1))

    server = None
    for port in candidate_ports:
        try:
            server = DashboardServer((args.host, port), root)
            break
        except OSError as exc:
            if args.port is not None:
                print(f"error: cannot bind {args.host}:{port} — {exc}", file=sys.stderr)
                return 1
            continue  # range scan: port busy, try the next one
        except Exception as exc:
            print(f"error: failed to load metrics module: {exc}", file=sys.stderr)
            return 1
    if server is None:
        print(f"error: no free port in range {port_range} on {args.host}", file=sys.stderr)
        return 1

    print(f"Trio Loop Dashboard listening on http://{args.host}:{port} (root: {root})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

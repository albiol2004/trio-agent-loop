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


def load_metrics_module():
    """Load metrics/trio-metrics.py via importlib and return the module.

    The hyphenated filename cannot be imported normally, so it is loaded
    by file location relative to this file. Only public functions are used
    (discover_loops, analyze_loop, parse_log); no parsing regex is copied.
    """
    spec = importlib.util.spec_from_file_location("trio_metrics", METRICS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load metrics module: {METRICS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in ("discover_loops", "analyze_loop", "parse_log"):
        if not hasattr(module, fn):
            raise RuntimeError(f"metrics module missing required function: {fn}")
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
        self._send_json(200, {
            "loops": loops,
            "updated_at": _utc_iso(datetime.now(timezone.utc)),
        })

    # -- /api/sessions -----------------------------------------------------

    def _handle_sessions(self, query: dict) -> None:
        name = (query.get("loop") or [None])[0]
        if not name:
            return self._send_json(400, {"error": "missing 'loop' parameter"})
        metrics = self.server.metrics
        loop_dir = next(
            (p for p in metrics.discover_loops(self.server.root) if p.name == name),
            None,
        )
        if loop_dir is None:
            return self._send_json(400, {"error": f"unknown loop: {name}"})
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
        # Parents first (newest first — unchanged), then subagents, so the
        # frontend can render parent-first groups.
        parents = [s for s in sessions if s["kind"] == "parent"]
        subagents = [s for s in sessions if s["kind"] != "parent"]
        parents.sort(key=lambda s: (s["timestamp"], s["label"]), reverse=True)
        subagents.sort(key=lambda s: (s["timestamp"], s["label"]), reverse=True)
        self._send_json(200, parents + subagents)

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

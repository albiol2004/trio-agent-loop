#!/usr/bin/env python3
"""serve.py — web dashboard backend for trio loops and skill registry.

A stdlib-only HTTP server (Python 3.11+, for `tomllib`) that renders loop
mailboxes as a live status board, tails omp agent session transcripts over
SSE, and serves the skill registry editor. Registry mutations are restricted
to its explicitly allowlisted harness directories.

Usage:
    python3 dashboard/serve.py [--host 127.0.0.1] [--port <port>]
        [--workspace <dir>] ...

With no --workspace, the current working directory and existing directories
under ~/pruebas are discovered as workspace roots. Parsing logic for mailboxes
is reused from `metrics/trio-metrics.py` (loaded by path relative to this file,
never duplicated).

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
        final_verdict, last_activity, last_entry_summary, segments,
        driver_phase, driver, running

Loop control:
    POST /api/loop/start  Body: {"root", "driver", "max_iterations"?}
    POST /api/loop/stop   Body: {"root"}
    GET  /api/loop/status?root=<absolute-path>

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

Registry (format-aware):
    GET /api/registry/file?path=<absolute-path>
    Response: {"path", "format", "frontmatter", "body", "managed",
               "quoted_keys"}
        format is one of "toml" | "yaml" | "text". "toml" bodies hold the
        file's `developer_instructions` string; "text" means no frontmatter
        fence was found (frontmatter is {}, body is the whole file).

    GET /api/registry/models?root=<absolute-path>
    Response: {"root", "rows"} describing model resolution for each agent.
        The root query is required; optional runtime overrides are read only
        from the dashboard process home.

    GET /api/registry/health?root=<absolute-path>
    Response: lineage, manifest, installation, generator-check, and dangling
        artifact health for the explicit repository and optional dashboard home.

    GET /api/registry/schema
    Response: {"destinations": {<harness>: [<surface>, ...]},
               "formats": {"<harness>:<surface>": "yaml"|"toml"},
               "keys": {"<harness>:<surface>": [<fieldspec>, ...]}}
        fieldspec: {"key", "type", "widget", "required", "enum", "help",
                    "values_from"};
        widget is one of text | textarea | checkbox | select | list | raw |
        permission-grid | spawns-select | json-schema.
        Derived from registry/scan.py's SURFACE_FORMAT/KEY_SCHEMA tables and
        this module's _GLOBAL_REGISTRY_DIRS — no workspace root required.

    POST /api/registry/serialize
    Body: {"format", "frontmatter", "body", "harness"?, "surface"?, "path"?}
    Response: 200 {"content", "warnings": [<str>]} or 400 {"error"}
        Pure serialization + validation; never writes to disk. A frontmatter
        value shaped {"$yaml": "<text>"} is parsed as YAML server-side (the
        raw sub-editor escape hatch) using the *strict* parser (rejects
        malformed/mis-indented input instead of scan.py's lenient on-disk
        behaviour); a parse failure is a 400 naming the key. Warnings are
        non-blocking: unknown key, `name` mismatching the
        target filename/dirname (suppressed for omnigent), or a format that
        disagrees with the harness/surface's canonical format.

Canonical agents (registry/agents.py's api:AgentsAPI; repo files, no ?root=):
    GET /api/registry/agents
    Response: {"agents": [{"name","description","model_tier","tool_policy","path"}],
               "harnesses": [<render harness>, ...],
               "support": {<harness>: {"supported": bool, "reason": str}},
               "model_tiers": [...], "tool_policies": [...]}

    GET /api/registry/agents/file?name=<name>
    Response: 200 {"name","description","model_tier","tool_policy",
                    "instructions","path"}; 404 {"error": "agent not found"}.

    POST /api/registry/agents   (create)
    PUT  /api/registry/agents/file   (update; 404 if the agent is absent)
    Body: {"name","description","model_tier","tool_policy","instructions"}
    Response: 201/200 {"path"}; 400 on validation failure (CanonicalAgent's
        ValueError text verbatim); POST is 409 when the agent already exists.

    DELETE /api/registry/agents/file?name=<name>
    Response: 200 {"path"}; 404 {"error": "agent not found"}.

    POST /api/registry/install
    Body: {"agent": "<canonical agent name>", "harness": "<harness>"}
    Response: 201 (new file) or 200 (overwrote an existing install)
        {"path","harness","format","filename","created"}.
        404 {"error": "agent not found"} for an unknown agent. 400
        {"error","reason","harness","supported": false} for an unsupported
        or unknown harness (e.g. omnigent) — never a 500. Writes are
        confined to _WRITABLE_ROOTS (403) and refuse a
        prompts/generate.py-managed destination (403).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import threading
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

REGISTRY_PATH = DASHBOARD_DIR.parent / "registry" / "scan.py"
"""Skill registry scanner, resolved relative to this file."""

AGENTS_PATH = DASHBOARD_DIR.parent / "registry" / "agents.py"
"""Canonical-agent model module, resolved relative to this file."""

TOPOLOGY_PATH = DASHBOARD_DIR.parent / "registry" / "topology.py"
"""Topology collector module, resolved relative to this file."""

MODELS_PATH = DASHBOARD_DIR.parent / "registry" / "models.py"
"""Model collector module, resolved relative to this file."""

HEALTH_PATH = DASHBOARD_DIR.parent / "registry" / "health.py"
"""Health collector module, resolved relative to this file."""

REPO_ROOT = DASHBOARD_DIR.parent.resolve()
"""Repository root containing canonical harness sources."""

HOME = Path.home()
"""Current user's home directory, used for global harness locations."""

REGISTRY_CACHE_SECONDS = 5.0
"""Maximum age for the in-memory registry index."""

WORKSPACE_SCAN_SECONDS = 60.0
"""Maximum age of the automatically discovered workspace list."""

_REGISTRY_MODULE = None
_AGENTS_MODULE = None
_TOPOLOGY_MODULE = None
_MODELS_MODULE = None
_HEALTH_MODULE = None
_REGISTRY_CACHE: dict[Path, tuple[dict, float]] = {}
_REGISTRY_CACHE_LOCK = threading.Lock()

SESSIONS_ROOT = HOME / ".omp" / "agent" / "sessions"

# Keep handles for children started by this server so stop can reap a child
# after sending SIGTERM without ever signalling its process group.
_LOOP_PROCESSES = {}
_LOOP_PROCESSES_LOCK = threading.Lock()


_WRITABLE_ROOTS = (
    HOME / ".claude" / "skills",
    HOME / ".claude" / "commands",
    HOME / ".claude" / "agents",
    HOME / ".agents" / "skills",
    HOME / ".codex" / "agents",
    HOME / ".omp" / "agent" / "commands",
    HOME / ".omp" / "agent" / "agents",
    HOME / ".config" / "opencode" / "commands",
    HOME / ".config" / "opencode" / "agents",
    HOME / ".kimi-code" / "skills",
    HOME / ".zcode" / "skills",
    REPO_ROOT / ".claude",
    REPO_ROOT / "codex",
    REPO_ROOT / "kimi",
    REPO_ROOT / "zcode",
    REPO_ROOT / "opencode",
    REPO_ROOT / "omp",
    REPO_ROOT / "omnigent" / "entrypoints",
)

_GLOBAL_REGISTRY_DIRS = {
    ("claude", "skill"): HOME / ".claude" / "skills",
    ("claude", "command"): HOME / ".claude" / "commands",
    ("claude", "agent"): HOME / ".claude" / "agents",
    ("codex", "skill"): HOME / ".agents" / "skills",
    ("codex", "agent"): HOME / ".codex" / "agents",
    ("omp", "command"): HOME / ".omp" / "agent" / "commands",
    ("omp", "agent"): HOME / ".omp" / "agent" / "agents",
    ("opencode", "command"): HOME / ".config" / "opencode" / "commands",
    ("opencode", "agent"): HOME / ".config" / "opencode" / "agents",
    ("kimi", "skill"): HOME / ".kimi-code" / "skills",
    ("zcode", "skill"): HOME / ".zcode" / "skills",
}


SHADOW_PATH = DASHBOARD_DIR.parent / "metrics" / "trio-shadow.py"
"""Slice shadow module (slice-commit/git attribution), resolved like METRICS_PATH."""

STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/skills.html": ("skills.html", "text/html; charset=utf-8"),
    "/skills.js": ("skills.js", "text/javascript; charset=utf-8"),
    "/nav.js": ("nav.js", "text/javascript; charset=utf-8"),
    "/agents.html": ("agents.html", "text/html; charset=utf-8"),
    "/agents.js": ("agents.js", "text/javascript; charset=utf-8"),
    "/topology.html": ("topology.html", "text/html; charset=utf-8"),
    "/topology.js": ("topology.js", "text/javascript; charset=utf-8"),
    "/models.html": ("models.html", "text/html; charset=utf-8"),
    "/models.js": ("models.js", "text/javascript; charset=utf-8"),
    "/health.html": ("health.html", "text/html; charset=utf-8"),
    "/health.js": ("health.js", "text/javascript; charset=utf-8"),
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

def load_registry_module():
    """Load registry/scan.py by path and cache the module."""
    global _REGISTRY_MODULE
    if _REGISTRY_MODULE is not None:
        return _REGISTRY_MODULE
    spec = importlib.util.spec_from_file_location("trio_registry_scan", REGISTRY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load registry module: {REGISTRY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in (
        "collect", "collect_canonical", "build_index", "parse_frontmatter",
        "FRONTMATTER_RE", "file_format", "split_file", "join_file",
        "parse_yaml", "default_template", "SURFACE_FORMAT", "KEY_SCHEMA",
        "generated_paths",
    ):
        if not hasattr(module, fn):
            raise RuntimeError(f"registry module missing required attribute: {fn}")
    _REGISTRY_MODULE = module
    return module


def load_topology_module():
    """Load registry/topology.py by path and cache the module."""
    global _TOPOLOGY_MODULE
    if _TOPOLOGY_MODULE is not None:
        return _TOPOLOGY_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_registry_topology", TOPOLOGY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load topology module: {TOPOLOGY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "collect_topology"):
        raise RuntimeError(
            "topology module missing required attribute: collect_topology")
    _TOPOLOGY_MODULE = module
    return module


def load_models_module():
    """Load registry/models.py by path and cache the module."""
    global _MODELS_MODULE
    if _MODELS_MODULE is not None:
        return _MODELS_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_registry_models", MODELS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load models module: {MODELS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "collect_models"):
        raise RuntimeError(
            "models module missing required attribute: collect_models")
    _MODELS_MODULE = module
    return module


def load_health_module():
    """Load registry/health.py by path and cache the module."""
    global _HEALTH_MODULE
    if _HEALTH_MODULE is not None:
        return _HEALTH_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_registry_health", HEALTH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load health module: {HEALTH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "collect_health"):
        raise RuntimeError(
            "health module missing required attribute: collect_health")
    _HEALTH_MODULE = module
    return module


def load_agents_module():
    """Load registry/agents.py by path and cache the module.

    Mirrors ``load_registry_module``: same by-path importlib load, cached
    once, with its own required-attribute assertion covering the
    ``api:AgentsAPI`` names this module actually calls.
    """
    global _AGENTS_MODULE
    if _AGENTS_MODULE is not None:
        return _AGENTS_MODULE
    spec = importlib.util.spec_from_file_location("trio_registry_agents", AGENTS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load agents module: {AGENTS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in (
        "MODEL_TIERS", "TOOL_POLICIES", "HARNESS_SUPPORT", "RENDER_HARNESSES",
        "CanonicalAgent", "RenderedAgent", "UnsupportedHarness",
        "agents_dir", "parse_agent", "dump_agent", "list_agents", "load_agent",
        "save_agent", "delete_agent", "render_agent", "install_support",
        "agent_index_records",
    ):
        if not hasattr(module, fn):
            raise RuntimeError(f"agents module missing required attribute: {fn}")
    _AGENTS_MODULE = module
    return module


def _registry_index(root: Path) -> dict:
    """Return a per-project registry scan, refreshing it at most every five seconds."""
    root_path = Path(root).resolve()
    now = time.monotonic()
    with _REGISTRY_CACHE_LOCK:
        cached = _REGISTRY_CACHE.get(root_path)
        if cached is not None and now - cached[1] <= REGISTRY_CACHE_SECONDS:
            return cached[0]
        registry = load_registry_module()
        entries = registry.collect(root_path)
        entries += registry.collect_canonical()
        try:
            canonical_agents = load_agents_module().agent_index_records()
        except Exception:
            # A broken canonical agent file must not take down the whole
            # registry index — degrade to an empty agent_matrix instead.
            traceback.print_exc()
            canonical_agents = []
        index = registry.build_index(entries, canonical_agents=canonical_agents)
        _REGISTRY_CACHE[root_path] = (index, time.monotonic())
        return index


def _invalidate_registry_cache() -> None:
    """Ensure subsequent reads observe a completed registry mutation."""
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.clear()


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


def _pid_is_live(pid: int) -> bool:
    """Return whether the kernel accepts a no-op signal for ``pid``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _process_cmdline(pid: int) -> str:
    """Read a Linux process command line, or an empty string on failure."""
    if pid <= 0:
        return ""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace")


def _read_driver_state(loop_dir: Path) -> dict | None:
    """Read the loop driver's private state file when it is valid JSON."""
    try:
        payload = json.loads(
            (loop_dir / ".driver.json").read_text(
                encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _driver_from_cmdline(pid: int) -> str | None:
    """Infer the dashboard driver from a live driver's command line."""
    cmdline = _process_cmdline(pid)
    if "trioctl" in cmdline and "loop" in cmdline:
        return "omnigent"
    if "portable/driver.sh" in cmdline:
        return "portable"
    if "trio_loop.py" in cmdline:
        return "omnigent" if "--runner omnigent" in cmdline else "portable"
    return None


def _driver_snapshot(loop_dir: Path) -> dict | None:
    """Normalize driver state for the status endpoint and board cards."""
    state = _read_driver_state(loop_dir)
    if state is None:
        return None
    raw_pid = state.get("pid")
    pid = _to_int(raw_pid) if not isinstance(raw_pid, bool) else None
    pid = pid or 0
    sessions = state.get("session_ids")
    if not isinstance(sessions, dict):
        sessions = {}
    driver = state.get("driver")
    if driver not in ("portable", "omnigent"):
        driver = _driver_from_cmdline(pid)
    phase = state.get("phase")
    if phase is not None:
        phase = str(phase)
    return {
        "pid": pid,
        "iteration": _to_int(state.get("iteration")),
        "phase": phase,
        "session_ids": sessions,
        "driver": driver,
        "live": _pid_is_live(pid),
    }


def _live_lock_pid(mailbox: Path) -> int | None:
    """Return a live lock owner PID, ignoring malformed or stale locks."""
    lock = mailbox / ".lock"
    try:
        if not lock.exists():
            return None
        pid = int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_is_live(pid) else None


def _owns_loop_process(pid: int) -> bool:
    """Allow stop only for the known loop command shapes."""
    cmdline = _process_cmdline(pid)
    return (
        "trio_loop.py" in cmdline
        or "portable/driver.sh" in cmdline
        or ("trioctl" in cmdline and "loop" in cmdline)
    )


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

def _resolve_registry_path(value) -> Path:
    """Resolve an API path, raising ``ValueError`` for malformed values."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing 'path'")
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid path")


def _writable_registry_path(
    value, workspace_roots: tuple[Path, ...] = (),
    project_root: Path | None = None,
) -> Path:
    """Resolve a path and enforce global or selected-project write roots."""
    target = _resolve_registry_path(value)
    for root in _WRITABLE_ROOTS:
        try:
            target.relative_to(root.resolve())
            return target
        except (ValueError, OSError, RuntimeError):
            continue
    for root in workspace_roots:
        try:
            target.relative_to(root.resolve())
            if project_root is not None:
                target.relative_to(project_root.resolve())
            return target
        except (ValueError, OSError, RuntimeError):
            continue
    raise PermissionError("path is outside writable registry roots")


def _registry_entry(index: dict, target: Path) -> dict | None:
    """Find a scanned file by resolved path."""
    for entry in index.get("entries", []):
        try:
            if Path(entry["path"]).expanduser().resolve() == target:
                return entry
        except (KeyError, OSError, RuntimeError, ValueError):
            continue
    return None


def _check_project_registry_entry(
    entry: dict | None, target: Path, project_root: Path,
    workspace_roots: tuple[Path, ...],
) -> None:
    """Keep project-scoped registry paths inside a known selected workspace."""
    if not entry or entry.get("scope") != "project":
        return
    try:
        target.relative_to(project_root.resolve())
        if not any(
            _path_is_under(target, workspace.resolve())
            for workspace in workspace_roots
        ):
            raise PermissionError("project registry path is not in a workspace")
    except (ValueError, OSError, RuntimeError):
        raise PermissionError("project registry path is outside selected workspace")


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _safe_registry_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing 'name'")
    name = value.strip()
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("invalid name")
    if Path(name).name != name:
        raise ValueError("invalid name")
    return name


def _registry_target(harness, surface, name) -> Path:
    """Map a global harness/surface/name tuple to its file layout."""
    if not isinstance(harness, str) or not isinstance(surface, str):
        raise ValueError("invalid harness or surface")
    harness = harness.strip().lower()
    surface = surface.strip().lower()
    root = _GLOBAL_REGISTRY_DIRS.get((harness, surface))
    if root is None:
        raise ValueError("unsupported harness or surface")
    name = _safe_registry_name(name)
    if surface == "skill":
        return (root / name / "SKILL.md").resolve()
    registry = load_registry_module()
    fmt = registry.SURFACE_FORMAT.get((harness, surface), "yaml")
    ext = "toml" if fmt == "toml" else "md"
    return (root / f"{name}.{ext}").resolve()


def _update_toml_name(text: str, name: str) -> str:
    """Rewrite (or insert) a top-level ``name = "..."`` line in TOML text.

    Every other byte of the source is left untouched: only the matched
    line's content changes, its original line ending is preserved.
    """
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"^\s*name\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            ending = ""
            if line.endswith("\r\n"):
                ending = "\r\n"
            elif line.endswith("\n"):
                ending = "\n"
            lines[i] = f'name = "{escaped}"{ending}'
            return "".join(lines)
    return f'name = "{escaped}"\n' + text


def _update_registry_name(text: str, name: str, fmt: str = "yaml") -> str:
    """Preserve source text while changing (or adding) the entity's ``name``.

    ``fmt`` selects the dialect: "toml" rewrites (or prepends) a top-level
    ``name = "..."`` line and never falls through to the YAML branch; any
    other value uses today's frontmatter ``name:`` line splice.
    """
    if fmt == "toml":
        return _update_toml_name(text, name)
    registry = load_registry_module()
    match = registry.FRONTMATTER_RE.match(text)
    if match:
        lines = match.group(1).splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\s*name\s*:", line):
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = f"{indent}name: {name}"
                prefix = text[:match.start(1)]
                suffix = text[match.end(1):]
                return prefix + "\n".join(lines) + suffix
        block = "\n".join([f"name: {name}"] + lines)
        return text[:match.start(1)] + block + text[match.end(1):]
    return f"---\nname: {name}\ndescription: \n---\n\n{text}"


def _reject_if_managed(target: Path) -> None:
    """Raise PermissionError when prompts/generate.py owns this path."""
    registry = load_registry_module()
    try:
        resolved = str(target.resolve())
    except (OSError, RuntimeError):
        resolved = str(target)
    if resolved in registry.generated_paths():
        raise PermissionError(
            "file is managed by prompts/generate.py and would be "
            "overwritten by the next run")


def _write_registry_file(target: Path, content: str, *, create: bool = False) -> None:
    """Write UTF-8 registry content after the target has passed policy checks."""
    if target.exists() and target.is_dir():
        raise IsADirectoryError(str(target))
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as fh:
            fh.write(content)
    else:
        if not target.parent.is_dir():
            raise FileNotFoundError(str(target.parent))
        target.write_text(content, encoding="utf-8")


def _read_json_body(handler) -> dict:
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(raw_length or "0")
    except ValueError:
        raise ValueError("invalid Content-Length")
    if length < 0 or length > 32 * 1024 * 1024:
        raise ValueError("invalid request body")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid JSON body")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


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

    def _resolve_root(self, query: dict) -> Path:
        """Resolve and authorize a request's workspace root."""
        value = (query.get("root") or [None])[0]
        if value is None:
            return self.server.default_root
        if not isinstance(value, str) or not value.strip():
            raise PermissionError("invalid workspace root")
        try:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                raise ValueError("workspace root must be absolute")
            candidate = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            raise PermissionError("invalid workspace root")
        seeds = self.server.get_workspace_seeds()
        if not any(_path_is_under(candidate, seed) for seed in seeds):
            pruebas = HOME / "pruebas"
            if (
                self.server.workspace_discovery_enabled
                and candidate.is_dir()
                and _path_is_under(candidate, pruebas)
            ):
                seeds = self.server.get_workspace_seeds(force=True)
        if not any(_path_is_under(candidate, seed) for seed in seeds):
            raise PermissionError("workspace root is not allowed")
        return candidate

    def _request_root(self, query: dict) -> Path | None:
        try:
            return self._resolve_root(query)
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return None

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

    # -- /api/workspaces ----------------------------------------------------

    def _handle_workspaces(self) -> None:
        workspaces = []
        for seed in self.server.get_workspace_seeds():
            workspaces.append({
                "id": str(seed),
                "path": str(seed),
                "has_loop": (seed / "loop").is_dir(),
                "has_trio_config": any(
                    (seed / marker).exists()
                    for marker in (
                        ".trio", "AGENTS.md", "CLAUDE.md",
                        ".cursor", ".opencode", ".claude",
                    )
                ),
            })
        self._send_json(200, workspaces)

    # -- /api/registry -----------------------------------------------------

    def _handle_registry(self, root: Path) -> None:
        self._send_json(200, _registry_index(root))

    def _handle_registry_file(self, query: dict, root: Path) -> None:
        value = (query.get("path") or [None])[0]
        try:
            target = _resolve_registry_path(value)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        index = _registry_index(root)
        entry = _registry_entry(index, target)
        if entry is None:
            return self._send_json(404, {"error": "file not found"})
        try:
            _check_project_registry_entry(
                entry, target, root, self.server.get_workspace_seeds())
            text = target.read_text(encoding="utf-8", errors="replace")
            registry = load_registry_module()
            fmt = registry.file_format(target)
            if fmt == "toml":
                frontmatter, body = registry.split_file(text, "toml")
            elif registry.FRONTMATTER_RE.match(text):
                fmt = "yaml"
                frontmatter, body = registry.parse_frontmatter(text)
            else:
                fmt = "text"
                frontmatter, body = {}, text
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except OSError:
            return self._send_json(404, {"error": "file not found"})
        self._send_json(200, {
            "path": str(target),
            "format": fmt,
            "frontmatter": frontmatter,
            "body": body,
            "managed": bool(entry.get("managed")),
            "quoted_keys": registry.quoted_key_paths(frontmatter),
        })

    def _handle_registry_topology(self, root: Path) -> None:
        """Return the canonical harness topology for an explicit root."""
        topology = load_topology_module()
        self._send_json(200, topology.collect_topology(root))

    def _handle_registry_models(self, root: Path) -> None:
        """Return model resolution rows for an explicit root."""
        models = load_models_module()
        self._send_json(200, models.collect_models(root, home=HOME))

    def _handle_registry_health(self, root: Path) -> None:
        """Return lineage and installation health for an explicit root."""
        health = load_health_module()
        self._send_json(200, health.collect_health(root, home=HOME))

    def _handle_registry_schema(self) -> None:
        """Static harness/surface schema: destinations, formats, key specs."""
        registry = load_registry_module()
        order = ("skill", "command", "agent")
        destinations: dict[str, list[str]] = {}
        for harness, surface in _GLOBAL_REGISTRY_DIRS:
            surfaces = destinations.setdefault(harness, [])
            if surface not in surfaces:
                surfaces.append(surface)
        for surfaces in destinations.values():
            surfaces.sort(key=lambda s: order.index(s) if s in order else len(order))
        formats = {
            f"{harness}:{surface}": fmt
            for (harness, surface), fmt in registry.SURFACE_FORMAT.items()
        }
        keys = dict(registry.KEY_SCHEMA)
        self._send_json(200, {
            "destinations": destinations,
            "formats": formats,
            "keys": keys,
        })

    def _handle_registry_serialize(self) -> None:
        """Pure serialization + validation of a frontmatter/body pair; no I/O."""
        try:
            payload = _read_json_body(self)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        fmt = payload.get("format")
        if fmt not in ("yaml", "toml", "text"):
            return self._send_json(400, {"error": "invalid 'format'"})
        body = payload.get("body")
        if not isinstance(body, str):
            return self._send_json(400, {"error": "missing or invalid 'body'"})
        if fmt == "text":
            return self._send_json(200, {"content": body, "warnings": []})
        frontmatter = payload.get("frontmatter")
        if not isinstance(frontmatter, dict):
            return self._send_json(400, {"error": "missing or invalid 'frontmatter'"})

        harness = payload.get("harness")
        surface = payload.get("surface")
        path_value = payload.get("path")
        harness_norm = (
            harness.strip().lower()
            if isinstance(harness, str) and harness.strip() else None
        )
        surface_norm = (
            surface.strip().lower()
            if isinstance(surface, str) and surface.strip() else None
        )

        registry = load_registry_module()

        resolved: dict = {}
        for key, value in frontmatter.items():
            if (isinstance(value, dict) and set(value.keys()) == {"$yaml"}
                    and isinstance(value.get("$yaml"), str)):
                raw = value["$yaml"]
                try:
                    # Strict: the scanner's parser is deliberately lenient
                    # for on-disk files (a bad file must not kill a scan),
                    # but user-submitted raw-YAML must be rejected rather
                    # than silently coerced/data-dropped on save.
                    parsed = registry.parse_yaml(raw, strict=True)
                except Exception as exc:
                    return self._send_json(
                        400, {"error": f"invalid YAML for key '{key}': {exc}"})
                if raw.strip() and not parsed:
                    # The scanner's parser is deliberately lenient (a bad file
                    # must not kill a scan), so unparseable text comes back as
                    # an empty map rather than an exception. In the editor that
                    # is a save that would silently drop the user's block.
                    return self._send_json(400, {
                        "error": f"invalid YAML for key '{key}': "
                                 "text is not a mapping or list"})
                if isinstance(parsed, dict) and list(parsed.keys()) == [key]:
                    parsed = parsed[key]
                resolved[key] = parsed
            else:
                resolved[key] = value

        # JSON turns scan.py's quoted string-key markers into plain strings.
        # Restore them before the existing YAML writer renders the response.
        if fmt == "yaml":
            registry.apply_quoted_key_paths(
                resolved, payload.get("quoted_keys"))

        warnings: list[str] = []
        schema_key = (
            f"{harness_norm}:{surface_norm}" if harness_norm and surface_norm else None
        )
        schema = registry.KEY_SCHEMA.get(schema_key) if schema_key else None
        if schema:
            schema_by_key = {spec["key"]: spec for spec in schema}
            # The browser keeps JSON-schema text as a YAML literal string.
            # Validate it here too so non-browser callers cannot save bad JSON.
            for spec in schema:
                if spec.get("widget") != "json-schema":
                    continue
                key = spec["key"]
                value = resolved.get(key)
                if isinstance(value, str):
                    try:
                        json.loads(value)
                    except (TypeError, ValueError) as exc:
                        return self._send_json(
                            400,
                            {"error": f"invalid JSON for key '{key}': {exc}"},
                        )
            for spec in schema:
                if spec.get("required"):
                    value = resolved.get(spec["key"])
                    if value is None or value == "":
                        return self._send_json(
                            400, {"error": f"missing required key '{spec['key']}'"})
            for key in resolved:
                if key not in schema_by_key:
                    warnings.append(f"unknown key '{key}' for {schema_key}")
            if ("name" in schema_by_key and harness_norm != "omnigent"
                    and path_value):
                try:
                    p = Path(path_value)
                    expected = p.parent.name if p.name == "SKILL.md" else p.stem
                except (TypeError, ValueError):
                    expected = None
                actual = resolved.get("name")
                if expected and actual and actual != expected:
                    warnings.append(
                        f"name '{actual}' does not match expected '{expected}'")
        if harness_norm and surface_norm:
            expected_fmt = registry.SURFACE_FORMAT.get((harness_norm, surface_norm))
            if expected_fmt and expected_fmt != fmt:
                warnings.append(
                    f"format '{fmt}' does not match expected '{expected_fmt}' "
                    f"for {harness_norm}:{surface_norm}")

        try:
            content = registry.join_file(resolved, body, fmt)
        except Exception as exc:
            return self._send_json(400, {"error": str(exc)})

        self._send_json(200, {"content": content, "warnings": warnings})

    def _handle_registry_put(self, root: Path) -> None:
        try:
            payload = _read_json_body(self)
            target = _writable_registry_path(
                payload.get("path"), self.server.get_workspace_seeds(), root)
            _reject_if_managed(target)
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValueError("missing or invalid 'content'")
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        try:
            _write_registry_file(target, content)
        except IsADirectoryError:
            return self._send_json(400, {"error": "path is a directory"})
        except FileNotFoundError:
            return self._send_json(404, {"error": "parent directory not found"})
        except OSError:
            return self._send_json(500, {"error": "could not write file"})
        _invalidate_registry_cache()
        self._send_json(200, {"path": str(target)})

    def _handle_registry_create(self) -> None:
        try:
            payload = _read_json_body(self)
            harness = payload.get("harness")
            surface = payload.get("surface")
            name = _safe_registry_name(payload.get("name"))
            target = _registry_target(harness, surface, name)
            _writable_registry_path(str(target))
            _reject_if_managed(target)
            content = payload.get("content")
            if not isinstance(content, str):
                raise ValueError("missing or invalid 'content'")
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if os.path.lexists(target):
            return self._send_json(409, {"error": "file already exists"})
        if content == "":
            registry = load_registry_module()
            h = harness.strip().lower() if isinstance(harness, str) else harness
            s = surface.strip().lower() if isinstance(surface, str) else surface
            content = registry.default_template(h, s, name)
        try:
            _write_registry_file(target, content, create=True)
        except FileExistsError:
            return self._send_json(409, {"error": "file already exists"})
        except OSError:
            return self._send_json(500, {"error": "could not create file"})
        _invalidate_registry_cache()
        self._send_json(201, {"path": str(target)})

    def _handle_registry_import(self, root: Path) -> None:
        try:
            payload = _read_json_body(self)
            if payload.get("mode") != "copy":
                raise ValueError("unsupported import mode")
            source = _resolve_registry_path(payload.get("from_path"))
            index = _registry_index(root)
            entry = _registry_entry(index, source)
            if entry is None:
                return self._send_json(404, {"error": "source file not found"})
            _check_project_registry_entry(
                entry, source, root, self.server.get_workspace_seeds())
            name = _safe_registry_name(payload.get("name"))
            target = _registry_target(
                payload.get("to_harness"), payload.get("to_surface"), name)
            _writable_registry_path(str(target))
            _reject_if_managed(target)
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if os.path.lexists(target):
            return self._send_json(409, {"error": "file already exists"})
        try:
            content = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self._send_json(404, {"error": "source file not found"})
        registry = load_registry_module()
        target_fmt = registry.file_format(target)
        if registry.file_format(source) != target_fmt:
            # Renaming across dialects would splice a TOML `name =` line into
            # Markdown (or vice versa) and write a corrupt file. Converting
            # between dialects is a separate concern from copying.
            return self._send_json(400, {
                "error": "cannot import across formats "
                         f"({registry.file_format(source)} -> {target_fmt})"})
        content = _update_registry_name(content, name, target_fmt)
        try:
            _write_registry_file(target, content, create=True)
        except FileExistsError:
            return self._send_json(409, {"error": "file already exists"})
        except OSError:
            return self._send_json(500, {"error": "could not import file"})
        _invalidate_registry_cache()
        self._send_json(201, {"path": str(target)})

    def _handle_registry_delete(self, query: dict, root: Path) -> None:
        try:
            target = _writable_registry_path(
                (query.get("path") or [None])[0],
                self.server.get_workspace_seeds(), root)
            _reject_if_managed(target)
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if not target.exists():
            return self._send_json(404, {"error": "file not found"})
        if target.is_dir():
            return self._send_json(400, {"error": "path is a directory"})
        entry = _registry_entry(_registry_index(root), target)
        try:
            _check_project_registry_entry(
                entry, target, root, self.server.get_workspace_seeds())
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        try:
            target.unlink()
        except OSError:
            return self._send_json(500, {"error": "could not delete file"})
        _invalidate_registry_cache()
        self._send_json(200, {"path": str(target)})

    # -- /api/registry/agents (canonical-agent CRUD) ------------------------

    def _agent_summary(self, agent, agents) -> dict:
        return {
            "name": agent.name,
            "description": agent.description,
            "model_tier": agent.model_tier,
            "tool_policy": agent.tool_policy,
            "path": str(agents.agents_dir() / f"{agent.name}.md"),
        }

    def _agent_detail(self, agent, agents) -> dict:
        detail = self._agent_summary(agent, agents)
        detail["instructions"] = agent.instructions
        return detail

    def _agent_from_payload(self, agents, payload: dict):
        instructions = payload.get("instructions")
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        return agents.CanonicalAgent(
            name=payload.get("name"),
            description=payload.get("description"),
            instructions=instructions,
            model_tier=payload.get("model_tier"),
            tool_policy=payload.get("tool_policy"),
        )

    def _handle_agents_list(self) -> None:
        agents = load_agents_module()
        support = {
            harness: {"supported": supported, "reason": reason}
            for harness, (supported, reason) in agents.HARNESS_SUPPORT.items()
        }
        self._send_json(200, {
            "agents": [
                self._agent_summary(a, agents) for a in agents.list_agents()
            ],
            "harnesses": list(agents.RENDER_HARNESSES),
            "support": support,
            "model_tiers": sorted(agents.MODEL_TIERS),
            "tool_policies": sorted(agents.TOOL_POLICIES),
        })

    def _handle_agent_file_get(self, query: dict) -> None:
        agents = load_agents_module()
        try:
            name = _safe_registry_name((query.get("name") or [None])[0])
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        try:
            agent = agents.load_agent(name)
        except FileNotFoundError:
            return self._send_json(404, {"error": "agent not found"})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        self._send_json(200, self._agent_detail(agent, agents))

    def _handle_agent_create(self) -> None:
        agents = load_agents_module()
        try:
            payload = _read_json_body(self)
            agent = self._agent_from_payload(agents, payload)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if (agents.agents_dir() / f"{agent.name}.md").exists():
            return self._send_json(409, {"error": "agent already exists"})
        path = agents.save_agent(agent)
        _invalidate_registry_cache()
        self._send_json(201, {"path": str(path)})

    def _handle_agent_update(self) -> None:
        agents = load_agents_module()
        try:
            payload = _read_json_body(self)
            agent = self._agent_from_payload(agents, payload)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if not (agents.agents_dir() / f"{agent.name}.md").exists():
            return self._send_json(404, {"error": "agent not found"})
        path = agents.save_agent(agent)
        _invalidate_registry_cache()
        self._send_json(200, {"path": str(path)})

    def _handle_agent_delete(self, query: dict) -> None:
        agents = load_agents_module()
        try:
            name = _safe_registry_name((query.get("name") or [None])[0])
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        path = agents.agents_dir() / f"{name}.md"
        if not agents.delete_agent(name):
            return self._send_json(404, {"error": "agent not found"})
        _invalidate_registry_cache()
        self._send_json(200, {"path": str(path)})

    # -- /api/registry/install -----------------------------------------------

    def _handle_agent_install(self) -> None:
        agents = load_agents_module()
        try:
            payload = _read_json_body(self)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        harness = payload.get("harness")
        harness_norm = (
            harness.strip().lower() if isinstance(harness, str) and harness.strip()
            else None
        )
        try:
            name = _safe_registry_name(payload.get("agent"))
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        try:
            agent = agents.load_agent(name)
        except FileNotFoundError:
            return self._send_json(404, {"error": "agent not found"})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        supported, reason = agents.install_support(harness_norm or "")
        if not supported:
            return self._send_json(400, {
                "error": f"harness {harness!r} does not support "
                         "canonical-agent install",
                "reason": reason,
                "harness": harness_norm if harness_norm is not None else harness,
                "supported": False,
            })
        try:
            # install_support already vetted the harness; this guards a
            # HARNESS_SUPPORT entry that ever outlives its renderer.
            rendered = agents.render_agent(agent, harness_norm)
            target = _writable_registry_path(
                str(_registry_target(harness_norm, "agent", agent.name)))
            _reject_if_managed(target)
        except agents.UnsupportedHarness as exc:
            return self._send_json(400, {
                "error": f"harness {harness!r} does not support "
                         "canonical-agent install",
                "reason": exc.reason,
                "harness": harness_norm,
                "supported": False,
            })
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        created = not target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_registry_file(target, rendered.text)
        except IsADirectoryError:
            return self._send_json(400, {"error": "path is a directory"})
        except OSError:
            return self._send_json(500, {"error": "could not write file"})
        _invalidate_registry_cache()
        self._send_json(201 if created else 200, {
            "path": str(target),
            "harness": harness_norm,
            "format": rendered.format,
            "filename": rendered.filename,
            "created": created,
        })

    # -- /api/board --------------------------------------------------------

    def _loop_card(self, loop_dir: Path, metrics) -> dict:
        analysis = metrics.analyze_loop(loop_dir)
        entries = metrics.parse_log(loop_dir / "LOG.md")
        driver_state = _driver_snapshot(loop_dir)
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
            "driver_phase": (
                driver_state["phase"] if driver_state else None
            ),
            "driver": driver_state["driver"] if driver_state else None,
            "running": bool(driver_state and driver_state["live"]),
        }

    def _handle_board(self, root: Path) -> None:
        metrics = self.server.metrics
        loop_dirs = list(metrics.discover_loops(root))
        loops = []
        for loop_dir in loop_dirs:
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
                    "driver_phase": None,
                    "driver": None,
                    "running": False,
                })
        inbox = []
        for loop_dir, card in zip(loop_dirs, loops):
            try:
                inbox.extend(_inbox_items(loop_dir, card, root))
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

    def _session_list(self, loop_dir: Path, root: Path) -> list[dict]:
        """Parents first (newest first), then subagents, for one loop."""
        sessions = []
        for desc in _session_files_for_loop(loop_dir, root):
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

    def _find_loop_dir(self, name: str, root: Path) -> Path | None:
        metrics = self.server.metrics
        return next(
            (p for p in metrics.discover_loops(root) if p.name == name),
            None,
        )

    def _handle_sessions(self, query: dict, root: Path) -> None:
        name = (query.get("loop") or [None])[0]
        if not name:
            return self._send_json(400, {"error": "missing 'loop' parameter"})
        loop_dir = self._find_loop_dir(name, root)
        if loop_dir is None:
            return self._send_json(400, {"error": f"unknown loop: {name}"})
        self._send_json(200, self._session_list(loop_dir, root))

    # -- /api/loop (detail) --------------------------------------------------

    def _handle_loop_detail(self, query: dict, root: Path) -> None:
        name = (query.get("name") or [None])[0]
        if not name:
            return self._send_json(400, {"error": "missing 'name' parameter"})
        loop_dir = self._find_loop_dir(name, root)
        if loop_dir is None:
            return self._send_json(400, {"error": f"unknown loop: {name}"})
        card = self._loop_card(loop_dir, self.server.metrics)
        card["mission"] = _mission_from_goal(loop_dir / "GOAL.md", limit=4000)
        card["timeline"] = _loop_timeline(loop_dir / "LOG.md")
        card["commits"] = _loop_commits(loop_dir, root)
        card["slices"] = _loop_slices(loop_dir)
        card["slice_activity"] = _loop_slice_activity(loop_dir, root)
        card["iterations"], card["overlaps"] = _loop_iterations(loop_dir, root)
        card["sessions"] = self._session_list(loop_dir, root)
        self._send_json(200, card)

    # -- /api/loop controls -------------------------------------------------

    def _read_loop_body(self) -> dict | None:
        """Read a loop-control body and turn malformed JSON into a 400."""
        try:
            return _read_json_body(self)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return None

    def _resolve_loop_root(self, payload: dict) -> Path | None:
        """Resolve a loop-control root through the existing seed allowlist."""
        try:
            return self._resolve_root({"root": [payload.get("root")]})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
            return None

    def _loop_mailbox(
        self, root: Path, *, require_goal: bool = True
    ) -> Path | None:
        """Return the in-workspace loop mailbox when it is available."""
        try:
            mailbox = (root / "loop").resolve()
        except (OSError, RuntimeError):
            return None
        if not _path_is_under(mailbox, root):
            return None
        if not mailbox.is_dir():
            return None
        if require_goal and not (mailbox / "GOAL.md").is_file():
            return None
        return mailbox

    def _handle_loop_start(self) -> None:
        payload = self._read_loop_body()
        if payload is None:
            return
        root = self._resolve_loop_root(payload)
        if root is None:
            return
        driver = payload.get("driver")
        if driver not in ("portable", "omnigent"):
            return self._send_json(
                400, {"error": "driver must be portable or omnigent"})
        max_iterations = payload.get("max_iterations", 10)
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
        ):
            return self._send_json(
                400, {"error": "max_iterations must be a positive integer"})
        mailbox = self._loop_mailbox(root)
        if mailbox is None:
            return self._send_json(
                400, {"error": "loop mailbox or GOAL.md is missing"})
        lock_pid = _live_lock_pid(mailbox)
        if lock_pid is not None:
            return self._send_json(
                409, {"error": f"loop is already running (pid {lock_pid})"})
        if driver == "portable":
            command = [
                "python3", "metrics/trio_loop.py", "run",
                "--mailbox", str(mailbox),
                "--max-iterations", str(max_iterations),
                "--runner", "portable",
            ]
        else:
            command = [
                "python3", "omnigent/trioctl", "omnigent", "loop",
                "--mailbox", str(mailbox),
                "--max-iterations", str(max_iterations),
            ]
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return self._send_json(500, {"error": "could not start loop"})
        with _LOOP_PROCESSES_LOCK:
            _LOOP_PROCESSES[process.pid] = process
        # Seed driver state before the child overwrites it so status
        # is not 404 in the window between spawn and first loop tick.
        (mailbox / ".driver.json").write_text(
            json.dumps({
                "pid": process.pid,
                "iteration": 0,
                "phase": "starting",
                "session_ids": {},
                "driver": driver,
            })
            + "\n",
            encoding="utf-8",
        )
        self._send_json(202, {
            "pid": process.pid,
            "driver": driver,
            "mailbox": str(mailbox),
        })

    def _handle_loop_stop(self) -> None:
        payload = self._read_loop_body()
        if payload is None:
            return
        root = self._resolve_loop_root(payload)
        if root is None:
            return
        mailbox = self._loop_mailbox(root, require_goal=False)
        if mailbox is None:
            return self._send_json(404, {"error": "loop mailbox not found"})
        driver_state = _driver_snapshot(mailbox)
        if driver_state is None:
            return self._send_json(404, {"error": "driver state not found"})
        pid = driver_state["pid"]
        if not driver_state["live"]:
            return self._send_json(404, {"error": "loop process is stale"})
        if not _owns_loop_process(pid):
            return self._send_json(403, {"error": "loop process is not owned"})
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return self._send_json(404, {"error": "loop process is stale"})
        except PermissionError:
            return self._send_json(403, {"error": "cannot stop loop process"})
        with _LOOP_PROCESSES_LOCK:
            process = _LOOP_PROCESSES.pop(pid, None)
        if process is not None:
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._send_json(200, {"stopped": True, "pid": pid})

    def _handle_loop_status(self, root: Path) -> None:
        mailbox = self._loop_mailbox(root, require_goal=False)
        if mailbox is None:
            return self._send_json(404, {"error": "loop mailbox not found"})
        driver_state = _driver_snapshot(mailbox)
        if driver_state is None:
            return self._send_json(404, {"error": "driver state not found"})
        payload = {
            "live": driver_state["live"],
            "pid": driver_state["pid"],
            "iteration": driver_state["iteration"],
            "phase": driver_state["phase"],
            "session_ids": driver_state["session_ids"],
        }
        if driver_state["driver"] is not None:
            payload["driver"] = driver_state["driver"]
        self._send_json(200, payload)

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
        if path == "/api/workspaces":
            return self._api(self._handle_workspaces)
        if path == "/api/registry":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry(root))
        if path == "/api/registry/file":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_file(query, root))
        if path == "/api/registry/schema":
            return self._api(self._handle_registry_schema)
        if path == "/api/registry/topology":
            if not query.get("root"):
                return self._send_json(400, {"error": "root is required"})
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_topology(root))
        if path == "/api/registry/models":
            if not query.get("root"):
                return self._send_json(400, {"error": "root is required"})
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_models(root))
        if path == "/api/registry/health":
            if not query.get("root"):
                return self._send_json(400, {"error": "root is required"})
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_health(root))
        if path == "/api/registry/agents":
            return self._api(self._handle_agents_list)
        if path == "/api/registry/agents/file":
            return self._api(lambda: self._handle_agent_file_get(query))
        if path == "/api/board":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_board(root))
        if path == "/api/loop/status":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_loop_status(root))
        if path == "/api/loop":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_loop_detail(query, root))
        if path == "/api/sessions":
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_sessions(query, root))
        if path == "/api/transcript":
            if self._request_root(query) is None:
                return
            return self._handle_transcript(query)
        if path.startswith("/api/"):
            return self._send_json(404, {"error": "not found"})
        return self._send_text(404, "not found")

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/registry/file":
            query = parse_qs(parsed.query)
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_put(root))
        if parsed.path == "/api/registry/agents/file":
            return self._api(self._handle_agent_update)
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/registry/serialize":
            return self._api(self._handle_registry_serialize)
        if path in ("/api/registry/create", "/api/registry/import"):
            root = self._request_root(query)
            if root is None:
                return
            if path == "/api/registry/create":
                return self._api(self._handle_registry_create)
            return self._api(lambda: self._handle_registry_import(root))
        if path == "/api/registry/agents":
            return self._api(self._handle_agent_create)
        if path == "/api/registry/install":
            return self._api(self._handle_agent_install)
        if path == "/api/loop/start":
            return self._api(self._handle_loop_start)
        if path == "/api/loop/stop":
            return self._api(self._handle_loop_stop)
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/registry/file":
            query = parse_qs(parsed.query)
            root = self._request_root(query)
            if root is None:
                return
            return self._api(lambda: self._handle_registry_delete(query, root))
        if parsed.path == "/api/registry/agents/file":
            query = parse_qs(parsed.query)
            return self._api(lambda: self._handle_agent_delete(query))
        self._send_json(404, {"error": "not found"})


class DashboardServer(ThreadingHTTPServer):
    """Threaded server carrying workspace roots and the loaded metrics module."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], root: Path | None = None,
        workspaces: list[Path] | tuple[Path, ...] | None = None,
        auto_discover: bool = False,
    ):
        if workspaces is None:
            seeds = [Path(root or Path.cwd()).resolve()]
        else:
            seeds = [Path(path).resolve() for path in workspaces]
            if not seeds:
                seeds = [Path.cwd().resolve()]
        unique_seeds = []
        for seed in seeds:
            if seed not in unique_seeds:
                unique_seeds.append(seed)
        self._fixed_workspace_seeds = tuple(unique_seeds)
        self.workspace_discovery_enabled = bool(auto_discover)
        self._workspace_lock = threading.Lock()
        self.workspace_scan_at = 0.0
        self.workspace_seeds = self._fixed_workspace_seeds
        self.default_root = (
            Path(root).resolve() if root is not None
            else self._fixed_workspace_seeds[0]
        )
        self.root = self.default_root
        self.metrics = load_metrics_module()
        self.get_workspace_seeds(force=True)
        super().__init__(address, DashboardHandler)

    def get_workspace_seeds(self, force: bool = False) -> tuple[Path, ...]:
        """Return fixed seeds plus a TTL-refreshed scan of project directories."""
        if not self.workspace_discovery_enabled:
            return self._fixed_workspace_seeds
        now = time.monotonic()
        with self._workspace_lock:
            if (
                not force
                and now - self.workspace_scan_at <= WORKSPACE_SCAN_SECONDS
            ):
                return self.workspace_seeds
            seeds = list(self._fixed_workspace_seeds)
            scan_roots = [
                HOME / "pruebas",
                HOME / "Projects",
                HOME / "projects",
                HOME / "dev",
                HOME / "src",
                HOME / "code",
                HOME / "repos",
                HOME / "work",
            ]
            for scan_root in scan_roots:
                try:
                    discovered = sorted(
                        path.resolve() for path in scan_root.iterdir()
                        if path.is_dir() and not path.name.startswith(".")
                    )
                except OSError:
                    continue
                for seed in discovered:
                    if seed not in seeds:
                        seeds.append(seed)
            self.workspace_seeds = tuple(seeds)
            self.workspace_scan_at = now
            return self.workspace_seeds

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Trio Loop Dashboard — read-only status board and "
                    "transcript viewer for trio loop mailboxes."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="bind port (default: first free port in the TRIO_DASH_PORTS range, 9470-9479)")
    parser.add_argument(
        "--workspace", action="append", dest="workspace_paths", metavar="PATH",
        help="workspace root (repeatable; default: cwd and ~/pruebas/* dirs)",
    )
    # Keep the old spelling for scripts that have not migrated yet.
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.workspace_paths is not None:
        raw_workspaces = args.workspace_paths
        auto_discover = False
    elif args.root is not None:
        raw_workspaces = [args.root]
        auto_discover = False
    else:
        raw_workspaces = [str(Path.cwd())]
        auto_discover = True
    workspaces = []
    for value in raw_workspaces:
        workspace = Path(value).expanduser().resolve()
        if not workspace.is_dir():
            print(
                f"error: --workspace {value} is not a directory",
                file=sys.stderr,
            )
            return 2
        if workspace not in workspaces:
            workspaces.append(workspace)
    root = workspaces[0]

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
            server = DashboardServer(
                (args.host, port),
                workspaces=workspaces,
                auto_discover=auto_discover,
            )
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

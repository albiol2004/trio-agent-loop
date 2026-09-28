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
    Response: {"loops": [<loop>], "inbox": [<item>],
               "updated_at": "<ISO-8601 UTC>"}
    Each loop object:
        name, path, mission, iteration, max_iterations, status,
        final_verdict, last_activity, verdict_mtime, last_entry_summary,
        segments, driver_phase, driver, running, running_sources,
        running_substate
    Each inbox item includes: loop, kind, severity, headline, detail, id,
    read, first_seen.

Inbox:
    POST /api/inbox/read    Body: {"ids": ["<id>"], "root": "<absolute-path>"}
    POST /api/inbox/unread  Body: {"ids": ["<id>"], "root": "<absolute-path>"}
    Response: {"ok": true, "ids": ["<id>", ...]}

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
               "source", "quoted_keys"}
        format is one of "toml" | "yaml" | "yaml-document" | "text".
        "toml" bodies hold the file's `developer_instructions` string;
        "yaml-document" bodies hold the file's `prompt` string; "text" means
        no frontmatter fence was found (frontmatter is {}, body is the whole
        file). `source` is the generate.py prompt/overlay descriptor or null.

    GET /api/registry/models?root=<absolute-path>
    Response: {"root", "rows", "available", "by_executor", "sources"}.
        The catalog fields describe available model ids and source status.
        The root query is required; optional runtime overrides are read only
        from the dashboard process home.

    GET /api/registry/health?root=<absolute-path>
    Response: lineage, manifest, installation, generator-check, and dangling
        artifact health for the explicit repository and optional dashboard home.

    GET /api/registry/topology?root=<absolute-path>&workflow=<name>&home=<bool>
    Response: {"root", "workflow", "graphs", "include_home"}.
        ``home`` accepts 0, 1, true, or false (case-insensitive). When omitted,
        installed graphs are included only by the installed trio-dash copy,
        which has no sibling ``omnigent`` directory.

    GET /api/registry/schema
    Response: {"destinations": {<harness>: {
                   <surface>: {"project": <relative-path>|null,
                               "global": <relative-path>|null}}},
               "formats": {"<harness>:<surface>": "yaml"|"yaml-document"|"toml"},
               "keys": {"<harness>:<surface>": [<fieldspec>, ...]}}
        fieldspec: {"key", "type", "widget", "required", "enum", "help",
                    "values_from"};
        widget is one of text | textarea | checkbox | select | list | raw |
        permission-grid | spawns-select | json-schema.
        Destination paths are relative to the selected project or dashboard
        home and come from the project/global registry directory tables; no
        workspace root is required.

    POST /api/registry/create
    Body: {"harness", "surface", "name", "content"?, "scope"?, "project"?}
    Response: 201 {"path", "scope_used"}; project scope falls back to the
        global destination when that harness/surface has no project layout.

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

    POST /api/registry/regenerate
    Body: {"path": "<generate.py-managed destination>", "root"?}
    Response: 200 {"source", "wrote", "diff", "install"}; a dirty workspace
        returns 409 {"error": "working tree dirty", "files": [...]}; an
        unmanaged destination returns 400.

Canonical agents (registry/agents.py's api:AgentsAPI; repo files, no ?root=):
    GET /api/registry/agents
    Response: {"agents": [{"name","description","model_tier","tool_policy","path"}],
               "harnesses": [<render harness>, ...],
               "support": {<harness>: {"supported": bool, "reason": str}},
               "model_tiers": [...], "tool_policies": [...]}

    GET /api/registry/agents/file?name=<name>
    Response: 200 {"name","description","model_tier","tool_policy","spawns",
                    "harness_overrides","instructions","path"}; 404
                    {"error": "agent not found"}.

    GET /api/registry/agents/defaults?model_tier=<tier>&tool_policy=<policy>
    Response: 200 {"model_tier","tool_policy","harness_defaults"}; 400 for
                    an unknown model tier or tool policy. The defaults are
                    renderer output for a hypothetical agent and never write
                    to disk.

    GET /api/registry/agents/destinations?name=<name>&scope=<scope>
    Response: 200 {"name","scope","destinations"} with resolved native paths.
                    Each destination includes its effective per-harness
                    "scope_used" ("project" or "global").

    POST /api/registry/agents   (create)
    PUT  /api/registry/agents/file   (update; 404 if the agent is absent)
    Body: {"name","description","model_tier","tool_policy","spawns"?,
           "harness_overrides"?, "instructions", "harnesses"?, "scope"?,
           "project"?}
    Response: 201/200 {"path","scope","installations"}; 400 on validation
        failure (CanonicalAgent's ValueError text verbatim); selected
        harnesses are installed in the requested project or global scope,
        with each installation reporting its effective "scope_used"; POST is
        409 when the agent already exists.

    DELETE /api/registry/agents/file?name=<name>
    Response: 200 {"path"}; 404 {"error": "agent not found"}.

    POST /api/registry/install
    Body: {"agent": "<canonical agent name>", "harness": "<harness>",
           "harnesses"?, "scope"?, "project"?}
    Response: 201 (new file) or 200 (overwrote an existing install)
        {"path","harness","format","filename","created","scope_used"}.
        Omnigent results also include the broker's durable `agent_id` and
        `session_id`, plus a `register` command naming that agent id.
        404 {"error": "agent not found"} for an unknown agent. 400
        {"error","reason","harness","supported": false} for an unsupported
        or unknown harness — never a 500. Omnigent is supported at
        `~/.omnigent/agents/<name>/config.yaml`; installation uploads that
        bundle and writes a `broker.json` sidecar. Writes are confined to
        _WRITABLE_ROOTS (403) and refuse a
        prompts/generate.py-managed destination (403).
"""
from __future__ import annotations

import argparse
import bisect
import copy
import ipaddress
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
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
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

INBOX_STATE_PATH = DASHBOARD_DIR / "inbox_state.py"
"""Inbox identity and read-state module, resolved relative to this file."""

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

BROKER_HTTP_PATH = DASHBOARD_DIR.parent / "omnigent" / "broker_http.py"
"""Omnigent broker client, resolved relative to this file."""

REPO_ROOT = DASHBOARD_DIR.parent.resolve()
"""Repository root containing canonical harness sources."""

HOME = Path.home()
"""Current user's home directory, used for global harness locations."""
_INITIAL_HOME = HOME

PROC_ROOT = Path("/proc")
"""Process root used for liveness and mailbox command-line scans."""

BROKER_BASE_URL = os.environ.get("TRIO_BOARD_BROKER_URL", "").strip()
"""Optional broker base URL; an empty value disables broker probing."""

REGISTRY_CACHE_SECONDS = 5.0
"""Maximum age for the in-memory registry index."""

WORKSPACE_SCAN_SECONDS = 60.0
"""Maximum age of the automatically discovered workspace list."""

OVERVIEW_CACHE_SECONDS = 15.0
"""Age after which a poll triggers one background rebuild of the overview.

Polls in between are answered from the last build; git-backed parts are
reused across builds while their inputs are unchanged (``_heavy``)."""

OVERVIEW_WORKERS = 4
"""Workspaces scanned in parallel when building the overview."""

_REGISTRY_MODULE = None
_AGENTS_MODULE = None
_TOPOLOGY_MODULE = None
_MODELS_MODULE = None
_HEALTH_MODULE = None
_BROKER_HTTP_MODULE = None
_INBOX_STATE_MODULE = None
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
    HOME / ".cursor" / "skills",
    HOME / ".cursor" / "agents",
    HOME / ".agents" / "skills",
    HOME / ".codex" / "agents",
    HOME / ".omp" / "agent" / "commands",
    HOME / ".omp" / "agent" / "agents",
    HOME / ".config" / "opencode" / "commands",
    HOME / ".config" / "opencode" / "agents",
    HOME / ".omnigent" / "agents",
    HOME / ".kimi-code" / "skills",
    HOME / ".zcode" / "skills",
    REPO_ROOT / ".claude",
    REPO_ROOT / ".cursor",
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
    ("cursor", "skill"): HOME / ".cursor" / "skills",
    ("cursor", "agent"): HOME / ".cursor" / "agents",
    ("codex", "skill"): HOME / ".agents" / "skills",
    ("codex", "agent"): HOME / ".codex" / "agents",
    ("omp", "command"): HOME / ".omp" / "agent" / "commands",
    ("omp", "agent"): HOME / ".omp" / "agent" / "agents",
    ("opencode", "command"): HOME / ".config" / "opencode" / "commands",
    ("opencode", "agent"): HOME / ".config" / "opencode" / "agents",
    ("omnigent", "agent"): HOME / ".omnigent" / "agents",
    ("kimi", "skill"): HOME / ".kimi-code" / "skills",
    ("zcode", "skill"): HOME / ".zcode" / "skills",
}

_GLOBAL_REGISTRY_RELATIVE_DIRS = {
    ("claude", "skill"): Path(".claude/skills"),
    ("claude", "command"): Path(".claude/commands"),
    ("claude", "agent"): Path(".claude/agents"),
    ("cursor", "skill"): Path(".cursor/skills"),
    ("cursor", "agent"): Path(".cursor/agents"),
    ("codex", "skill"): Path(".agents/skills"),
    ("codex", "agent"): Path(".codex/agents"),
    ("omp", "command"): Path(".omp/agent/commands"),
    ("omp", "agent"): Path(".omp/agent/agents"),
    ("opencode", "command"): Path(".config/opencode/commands"),
    ("opencode", "agent"): Path(".config/opencode/agents"),
    ("omnigent", "agent"): Path(".omnigent/agents"),
    ("kimi", "skill"): Path(".kimi-code/skills"),
    ("zcode", "skill"): Path(".zcode/skills"),
}

# Mirrors the project-scoped directories collected by registry/scan.py.
_PROJECT_REGISTRY_DIRS = {
    ("claude", "skill"): Path(".claude/skills"),
    ("claude", "command"): Path(".claude/commands"),
    ("claude", "agent"): Path(".claude/agents"),
    ("opencode", "agent"): Path(".opencode/agents"),
    ("cursor", "skill"): Path(".cursor/skills"),
    ("cursor", "agent"): Path(".cursor/agents"),
}


def _registry_destination_catalog() -> dict[str, dict[str, dict[str, str | None]]]:
    """Build the browser-facing project/global registry path catalog."""
    pairs = list(_GLOBAL_REGISTRY_RELATIVE_DIRS)
    pairs.extend(
        pair for pair in _PROJECT_REGISTRY_DIRS if pair not in pairs
    )
    destinations: dict[str, dict[str, dict[str, str | None]]] = {}
    for harness, surface in pairs:
        project = _PROJECT_REGISTRY_DIRS.get((harness, surface))
        global_path = _GLOBAL_REGISTRY_RELATIVE_DIRS.get((harness, surface))
        destinations.setdefault(harness, {})[surface] = {
            "project": project.as_posix() if project is not None else None,
            "global": (
                global_path.as_posix() if global_path is not None else None
            ),
        }
    return destinations


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
               "parse_slices_block", "loop_name", "read_queue", "derive_slices",
               "parse_slice_verdicts"):
        if not hasattr(module, fn):
            raise RuntimeError(f"metrics module missing required function: {fn}")
    _METRICS_MODULE = module
    return module


def load_inbox_state_module():
    """Load dashboard/inbox_state.py by path and cache the module."""
    global _INBOX_STATE_MODULE
    if _INBOX_STATE_MODULE is not None:
        return _INBOX_STATE_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_inbox_state", INBOX_STATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load inbox state module: {INBOX_STATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in ("decorate_items", "set_read"):
        if not hasattr(module, fn):
            raise RuntimeError(f"inbox state module missing required function: {fn}")
    _INBOX_STATE_MODULE = module
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
        "unflatten_dotted_keys", "generated_paths",
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


def load_broker_http_module():
    """Load the stdlib Omnigent broker client by path and cache it."""
    global _BROKER_HTTP_MODULE
    if _BROKER_HTTP_MODULE is not None:
        return _BROKER_HTTP_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_omnigent_broker_http", BROKER_HTTP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load broker module: {BROKER_HTTP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for fn in (
        "bundle_agent_dir", "register_agent_bundle", "update_agent_bundle",
        "BrokerHttpError",
    ):
        if not hasattr(module, fn):
            raise RuntimeError(f"broker module missing required attribute: {fn}")
    _BROKER_HTTP_MODULE = module
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


def _using_real_proc() -> bool:
    """Return whether liveness should use the host kernel's process table."""
    try:
        return Path(PROC_ROOT).resolve() == Path("/proc").resolve()
    except OSError:
        return False


def _pid_is_live(pid: int) -> bool:
    """Return whether ``pid`` is live in the configured process environment."""
    if pid <= 0:
        return False
    if not _using_real_proc():
        try:
            return (Path(PROC_ROOT) / str(pid) / "cmdline").is_file()
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        return False
    # A zombie still answers kill(0) until its parent reaps it; it is dead.
    return _proc_state(pid) not in ("Z", "X")


def _proc_stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the ``(comm)`` field, or None."""
    try:
        raw = (Path(PROC_ROOT) / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    _, sep, rest = raw.rpartition(")")
    return rest.split() if sep else None


def _proc_state(pid: int) -> str | None:
    fields = _proc_stat_fields(pid)
    return fields[0] if fields else None


_BOOT_TIME = None


def _pid_start_epoch(pid: int) -> float | None:
    """Wall-clock start time of a real process, or None when unknown."""
    global _BOOT_TIME
    fields = _proc_stat_fields(pid)
    if not fields or len(fields) < 20:
        return None
    try:
        if _BOOT_TIME is None:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    _BOOT_TIME = float(line.split()[1])
        ticks = os.sysconf("SC_CLK_TCK")
        return _BOOT_TIME + int(fields[19]) / ticks
    except (OSError, ValueError, TypeError):
        return None


def _pid_owns_record(pid: int, record: Path) -> bool:
    """Whether a live ``pid`` can be the process that wrote ``record``.

    Sidecars and lock files are written by their process, so a process that
    started after the file's last write is a recycled PID, not the owner.
    Unknown start times (fake /proc, permissions) are not treated as reuse.
    """
    if not _using_real_proc():
        return True
    started = _pid_start_epoch(pid)
    if started is None:
        return True
    try:
        written = record.stat().st_mtime
    except OSError:
        return True
    return started <= written + 2.0


def _record_pid_live(pid: int, record: Path) -> bool:
    return _pid_is_live(pid) and _pid_owns_record(pid, record)


def _process_cmdline(pid: int) -> str:
    """Read a Linux process command line, or an empty string on failure."""
    if pid <= 0:
        return ""
    try:
        raw = (Path(PROC_ROOT) / str(pid) / "cmdline").read_bytes()
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
        "live": _record_pid_live(pid, loop_dir / ".driver.json"),
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
    return pid if _record_pid_live(pid, lock / "pid") else None


def _read_session_sidecar(loop_dir: Path) -> dict | None:
    """Read the optional wrapper-owned session sidecar."""
    try:
        payload = json.loads(
            (loop_dir / ".session.json").read_text(
                encoding="utf-8", errors="replace"
            )
        )
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_PROC_SNAPSHOT = threading.local()
"""Per-thread state shared while one board or overview is being built."""


class _proc_snapshot:
    """Scope in which every mailbox check reuses one read of the process
    table and one broker session listing.

    A board checks every mailbox against every live process; without the
    scope that is one full ``/proc`` walk per mailbox (twice, once for the
    card and once for the inbox).
    """

    def __init__(self, processes: list[tuple] | None = None,
                 broker: dict | None = None):
        self._given = processes
        self._broker = broker

    def __enter__(self):
        self._outer = getattr(_PROC_SNAPSHOT, "processes", None)
        if self._outer is None:
            _PROC_SNAPSHOT.processes = (
                self._given if self._given is not None
                else _live_processes())
            _PROC_SNAPSHOT.paths = _candidate_paths_for(
                _PROC_SNAPSHOT.processes)
            _PROC_SNAPSHOT.broker = (
                self._broker if self._broker is not None
                else _broker_listing())
            _PROC_SNAPSHOT.ambiguous = {}
            _PROC_SNAPSHOT.memo = {}
        return self

    def __exit__(self, *exc):
        if self._outer is None:
            _PROC_SNAPSHOT.processes = None
            _PROC_SNAPSHOT.paths = None
            _PROC_SNAPSHOT.broker = None
            _PROC_SNAPSHOT.ambiguous = None
            _PROC_SNAPSHOT.memo = None
        return False


def _process_args(pid: int) -> list[str]:
    try:
        raw = (Path(PROC_ROOT) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [
        part.decode("utf-8", errors="replace")
        for part in raw.split(b"\0") if part
    ]


def _live_processes() -> list[tuple[int, list[str], str | None]] | None:
    """Live processes as ``(pid, argv, cwd)`` (excluding this server)."""
    try:
        entries = list(Path(PROC_ROOT).iterdir())
    except (OSError, RuntimeError):
        return None
    current_pid = os.getpid()
    processes = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current_pid or not _pid_is_live(pid):
            continue
        args = _process_args(pid)
        if not args:
            continue
        try:
            cwd = os.readlink(Path(PROC_ROOT) / str(pid) / "cwd")
        except OSError:
            cwd = None
        processes.append((pid, args, cwd))
    return processes


_NON_MAILBOX_OPTIONS = {"--config", "-c"}


def _arg_paths(args: list[str], cwd: str | None):
    """Normalized filesystem paths named by argv elements.

    Each element counts as a whole (``--opt=value`` contributes ``value``);
    nothing is matched inside a larger string such as a shell ``-c`` script.
    A relative element resolves against the process cwd only when it looks
    like a path (contains a separator) or is an option's value
    (``--mailbox loop``); a bare word such as the ``loop`` subcommand of
    ``trioctl omnigent loop`` is never a path.
    """
    previous = ""
    for arg in args[1:]:
        value = arg
        option = previous if previous.startswith("-") and "=" not in previous else ""
        if arg.startswith("-") and "=" in arg:
            option, value = arg.split("=", 1)
        is_option_value = bool(option)
        previous = arg
        # A shared role config (`--config <mailbox>/omnigent.toml`) says
        # which settings a run uses, not which loop it works on.
        if option in _NON_MAILBOX_OPTIONS:
            continue
        if not value or "\n" in value or value.startswith("-"):
            continue
        if os.path.isabs(value):
            yield os.path.normpath(value)
        elif cwd and (os.sep in value or is_option_value):
            yield os.path.normpath(os.path.join(cwd, value))


def _path_names_mailbox(path: str, mailbox: str) -> bool:
    """``path`` is the mailbox or a file inside it, not inside a child
    mailbox nested below it (those belong to the child)."""
    if path == mailbox:
        return True
    if not path.startswith(mailbox + os.sep):
        return False
    first = path[len(mailbox) + 1:].split(os.sep, 1)[0]
    try:
        return not load_metrics_module().is_mailbox(Path(mailbox) / first)
    except Exception:
        return True


def _mailbox_candidate_paths(processes) -> list[str]:
    """Sorted argv paths that could name a mailbox (a ``loop*`` component).

    Every discovered mailbox lives under a top-level ``loop*`` directory, so
    other paths can never match; filtering once per snapshot keeps each
    mailbox check to a binary search.
    """
    paths = set()
    for _, args, cwd in processes or ():
        for path in _arg_paths(args, cwd):
            if any(part.startswith("loop") for part in path.split(os.sep)):
                paths.add(path)
    return sorted(paths)


_CANDIDATE_PATHS_MEMO: list = [None, None]
_CANDIDATE_PATHS_LOCK = threading.Lock()


def _candidate_paths_for(processes) -> list[str]:
    """``_mailbox_candidate_paths`` once per process snapshot, however many
    workspace and worktree scopes of one overview build share it."""
    with _CANDIDATE_PATHS_LOCK:
        if processes is not None and _CANDIDATE_PATHS_MEMO[0] is processes:
            return _CANDIDATE_PATHS_MEMO[1]
    paths = _mailbox_candidate_paths(processes)
    with _CANDIDATE_PATHS_LOCK:
        _CANDIDATE_PATHS_MEMO[0] = processes
        _CANDIDATE_PATHS_MEMO[1] = paths
    return paths


def _proc_matches_mailbox(loop_dir: Path) -> bool:
    """Return whether a live process names this mailbox in its argv."""
    try:
        mailbox_text = str(loop_dir.resolve())
    except (OSError, RuntimeError):
        return False
    paths = getattr(_PROC_SNAPSHOT, "paths", None)
    if paths is None:
        paths = _mailbox_candidate_paths(_live_processes())
    start = bisect.bisect_left(paths, mailbox_text)
    for path in paths[start:]:
        if not path.startswith(mailbox_text):
            break
        if _path_names_mailbox(path, mailbox_text):
            return True
    return False


BROKER_LIST_PAGES = 20
"""Upper bound on ``GET /v1/sessions`` pages read per listing (100 each)."""

BROKER_LIST_SECONDS = 5.0
"""How long one broker listing is reused across boards and workspaces."""

_BROKER_LISTING = {"at": 0.0, "value": None}
_BROKER_LISTING_LOCK = threading.Lock()


def _broker_listing() -> dict:
    """Running broker sessions, with an explicit ``status``.

    ``status`` is ``disabled`` (no broker URL), ``unreachable`` (any page
    failed: liveness from the broker is unknown, not "not running") or
    ``ok``. Only sessions whose status is ``running`` are kept.
    """
    base_url = str(BROKER_BASE_URL).strip()
    if not base_url:
        return {"status": "disabled", "running": []}
    with _BROKER_LISTING_LOCK:
        cached = _BROKER_LISTING["value"]
        if (
            cached is not None
            and cached.get("url") == base_url
            and time.monotonic() - _BROKER_LISTING["at"] <= BROKER_LIST_SECONDS
        ):
            return cached
        running = []
        status = "ok"
        after = None
        complete = False
        for _ in range(BROKER_LIST_PAGES):
            query = {"limit": "100"}
            if after:
                query["after"] = after
            url = (base_url.rstrip("/") + "/v1/sessions?"
                   + urllib.parse.urlencode(query))
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    page = json.loads(
                        response.read().decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                status = "unreachable"
                break
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list):
                status = "unreachable"
                break
            for item in data:
                if (
                    isinstance(item, dict)
                    and str(item.get("status", "")).lower() == "running"
                ):
                    running.append({
                        "id": str(item.get("id") or ""),
                        "title": str(item.get("title") or ""),
                        "workspace": str(item.get("workspace") or ""),
                    })
            after = page.get("last_id")
            if not page.get("has_more") or not after:
                complete = True
                break
        if status == "ok" and not complete:
            status = "truncated"
        value = {"status": status, "running": running, "url": base_url}
        _BROKER_LISTING["value"] = value
        _BROKER_LISTING["at"] = time.monotonic()
        return value


def _broker_sessions_for_mailbox(loop_dir: Path, root: Path | None,
                                 listing: dict) -> list[dict]:
    """Running broker sessions that belong to exactly this mailbox.

    trioctl titles sessions ``trioctl <mailbox-dir-name> <role>:...`` and
    records the workspace. Both must match, and the mailbox dir name must be
    unique within the workspace; a title alone never attributes a session.
    """
    if root is None or not listing.get("running"):
        return []
    ambiguous = (getattr(_PROC_SNAPSHOT, "ambiguous", None) or {}).get(
        str(root), set())
    if loop_dir.name in ambiguous:
        return []
    prefix = f"trioctl {loop_dir.name} "
    try:
        root_text = str(root.resolve())
    except OSError:
        return []
    matches = []
    for session in listing["running"]:
        workspace = session.get("workspace")
        if not workspace or not session["title"].startswith(prefix):
            continue
        try:
            same = str(Path(workspace).resolve()) == root_text
        except OSError:
            same = False
        if same:
            matches.append(session)
    return matches


def _broker_session_ids(
    driver_state: dict | None, session_state: dict | None
) -> list[str]:
    """Collect unique broker session IDs in stable input order."""
    values = []
    if driver_state:
        session_ids = driver_state.get("session_ids")
        if isinstance(session_ids, dict):
            values.extend(session_ids.values())
    if session_state:
        values.append(session_state.get("session"))

    session_ids = []
    for value in values:
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        text = str(value).strip()
        if text and text not in session_ids:
            session_ids.append(text)
    return session_ids


def _broker_has_running_session(session_ids: list[str],
                                listing: dict | None = None) -> bool:
    """Probe configured broker sessions, treating failures as unknown.

    A complete listing (``status: ok``) already names every running
    session, so it answers without one request per id.
    """
    base_url = str(BROKER_BASE_URL).strip()
    if not base_url or not session_ids:
        return False
    if listing is not None and listing.get("status") == "ok":
        running = {s.get("id") for s in listing.get("running", [])}
        return any(sid in running for sid in session_ids)

    for session_id in session_ids:
        url = (
            base_url.rstrip("/")
            + "/v1/sessions/"
            + urllib.parse.quote(session_id, safe="")
        )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                payload = json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in ("status", "state"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip().lower() == "running":
                return True
    return False


def _open_loop_substate(loop_dir: Path) -> str | None:
    """Derive the open-loop lead/evaluator/both sub-state, or None.

    Reads the raw ``.driver.json`` sidecar first; falls back to
    ``.session.json`` when the driver sidecar is absent or is not an
    open-loop sidecar (``open_loop: true``). Returns None for lockstep
    mailboxes and for any mailbox with no open-loop sidecar.
    """
    sidecar = _read_driver_state(loop_dir)
    if not (isinstance(sidecar, dict) and sidecar.get("open_loop") is True):
        sidecar = _read_session_sidecar(loop_dir)
    if not (isinstance(sidecar, dict) and sidecar.get("open_loop") is True):
        return None
    lead_alive = sidecar.get("lead_alive") is True
    eval_alive = sidecar.get("eval_alive") is True
    if lead_alive and eval_alive:
        return "both"
    if lead_alive:
        return "lead"
    if eval_alive:
        return "evaluator"
    return None


def _snapshot_memo(key: tuple, compute):
    """Reuse ``compute()`` within one snapshot (one board/overview build)."""
    memo = getattr(_PROC_SNAPSHOT, "memo", None)
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def _running_detection(loop_dir: Path, root: Path | None = None) -> dict:
    """Running evidence for a mailbox; computed once per build (the card
    and the inbox both ask)."""
    return copy.deepcopy(_snapshot_memo(
        ("detect", str(loop_dir), str(root)),
        lambda: _detect_running(loop_dir, root)))


def _detect_running(loop_dir: Path, root: Path | None = None) -> dict:
    """Return concrete running evidence and any stale session sidecar.

    ``broker`` reports whether broker liveness was known for this check:
    ``ok`` (listing read), ``disabled`` (no broker URL) or ``unreachable`` /
    ``truncated``; only ``ok`` lets callers treat "no broker session" as a
    fact.
    """
    driver_state = _driver_snapshot(loop_dir)
    session_state = _read_session_sidecar(loop_dir)
    sources = []

    lock_pid = _live_lock_pid(loop_dir)
    driver_live = bool(driver_state and driver_state["live"])
    if driver_live or lock_pid is not None:
        sources.append("driver")

    if _proc_matches_mailbox(loop_dir):
        sources.append("proc")

    orphaned_session = None
    session_running = False
    if session_state is not None:
        phase = str(session_state.get("phase") or "").strip().lower()
        done = session_state.get("done") is True
        done = done or phase in {"done", "finished", "cleared"}
        raw_pid = session_state.get("pid")
        pid = (
            _to_int(raw_pid)
            if not isinstance(raw_pid, bool)
            else None
        )
        if (
            not done and pid is not None
            and _record_pid_live(pid, loop_dir / ".session.json")
        ):
            session_running = True
        elif not done:
            orphaned_session = {
                "pid": pid,
                "started_at": session_state.get("started_at"),
            }
    if session_running:
        sources.append("session")

    listing = getattr(_PROC_SNAPSHOT, "broker", None)
    if listing is None:
        listing = _broker_listing()
    session_ids = _broker_session_ids(driver_state, session_state)
    matched = _broker_sessions_for_mailbox(loop_dir, root, listing)
    if matched or _broker_has_running_session(session_ids, listing):
        sources.append("broker")

    control_pid = None
    if driver_live:
        control_pid = driver_state["pid"]
    elif lock_pid is not None:
        control_pid = lock_pid

    return {
        "sources": sources,
        "orphaned_session": orphaned_session,
        "substate": _open_loop_substate(loop_dir),
        "broker": listing.get("status", "disabled"),
        "broker_sessions": [m["title"] for m in matched],
        "control_pid": control_pid,
    }


def _owns_loop_process(pid: int) -> bool:
    """Allow stop only for the known loop command shapes."""
    cmdline = _process_cmdline(pid)
    return (
        "trio_loop.py" in cmdline
        or "portable/driver.sh" in cmdline
        or ("trioctl" in cmdline and "loop" in cmdline)
    )


DRIVER_ENTRYPOINTS = {
    "portable": REPO_ROOT / "metrics" / "trio_loop.py",
    "omnigent": REPO_ROOT / "omnigent" / "trioctl",
}
"""Loop drivers run from this dashboard's checkout, against any workspace."""

LAUNCH_GRACE_SECONDS = 1.5
"""A started driver must still be alive this long before Start reports 202."""

LAUNCH_LOG_KEEP = 20

_LOOP_ACTIONS: dict[str, dict] = {}
"""Last Start/Stop per mailbox path, kept for the life of the server."""
_LOOP_ACTIONS_LOCK = threading.Lock()


def _record_action(mailbox: Path, **fields) -> dict:
    with _LOOP_ACTIONS_LOCK:
        record = dict(_LOOP_ACTIONS.get(str(mailbox), {}))
        if "action" in fields:
            record = {}
        record.update(fields)
        record["updated_at"] = _utc_iso(datetime.now(timezone.utc))
        _LOOP_ACTIONS[str(mailbox)] = record
        return dict(record)


def _last_action(mailbox: Path) -> dict | None:
    with _LOOP_ACTIONS_LOCK:
        record = _LOOP_ACTIONS.get(str(mailbox))
        return dict(record) if record else None


def _launch_log_path(mailbox: Path) -> Path:
    """Per-launch driver output, outside every workspace."""
    directory = HOME / ".local" / "state" / "trio-dash" / "launch"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        logs = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-LAUNCH_LOG_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", mailbox.parent.name)[:60]
    return directory / f"{stamp}-{slug}.log"


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_bytes()[-limit:].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _seed_driver_state(mailbox: Path, pid: int, driver: str) -> None:
    """Point .driver.json at a started driver, keeping its resume cursor.

    A driver that already wrote its own sidecar is left alone; otherwise the
    existing iteration and session ids survive and only pid/driver/phase
    change.
    """
    path = mailbox / ".driver.json"
    current = _read_driver_state(mailbox) or {}
    if _to_int(current.get("pid")) == pid:
        return
    state = dict(current)
    state.update({"pid": pid, "driver": driver, "phase": "starting"})
    state.setdefault("iteration", 0)
    state.setdefault("session_ids", {})
    tmp = path.with_name(".driver.json.dashboard-tmp")
    try:
        tmp.write_text(json.dumps(state) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        traceback.print_exc()


def _reap_loop_process(mailbox: Path, process) -> None:
    """Wait for a started driver so it never lingers as a zombie."""
    try:
        code = process.wait()
    except Exception:
        return
    with _LOOP_PROCESSES_LOCK:
        _LOOP_PROCESSES.pop(process.pid, None)
    record = _last_action(mailbox) or {}
    if record.get("pid") == process.pid:
        outcome = "stopped" if record.get("action") == "stop" or code in (
            -signal.SIGTERM, 143) else ("finished" if code == 0 else "exited")
        _record_action(mailbox, outcome=outcome, exit_code=code,
                       message=f"Driver PID {process.pid} {outcome} "
                               f"(code {code})")


def _loop_controls(loop_dir: Path, root: Path | None, detection: dict,
                   driver: str | None) -> dict:
    """What Start/Stop can do for this mailbox, with the reason if not.

    The control API acts on ``<workspace>/loop`` only. Start needs GOAL.md,
    the driver entrypoint in this checkout, and no live evidence of a run;
    Stop needs a live driver or lock PID with a known loop command line.
    """
    is_root_loop = False
    if root is not None:
        try:
            is_root_loop = loop_dir.resolve() == (root / "loop").resolve()
        except OSError:
            pass
    chosen = driver if driver in DRIVER_ENTRYPOINTS else "portable"
    sources = detection["sources"]
    if not is_root_loop:
        start = (False, "Start and stop act on a workspace's loop/ mailbox "
                        "only; run this one from its session.")
    elif not (loop_dir / "GOAL.md").is_file():
        start = (False, "GOAL.md is missing.")
    elif not DRIVER_ENTRYPOINTS[chosen].is_file():
        start = (False, f"The {chosen} driver is not in this dashboard "
                        f"checkout ({DRIVER_ENTRYPOINTS[chosen]}).")
    elif sources:
        start = (False, "Already running (" + ", ".join(sources) + ").")
    elif detection.get("broker") in ("unreachable", "truncated"):
        start = (False, "Broker liveness is unknown (the broker "
                        + ("did not answer" if detection.get("broker")
                           == "unreachable" else "list was cut short")
                        + "); a broker-only run cannot be ruled out.")
    else:
        start = (True, f"Starts the {chosen} driver from "
                       f"{REPO_ROOT} for this mailbox.")
    pid = detection.get("control_pid")
    if not is_root_loop:
        stop = (False, start[1] if not start[0] else "")
    elif pid is None:
        stop = (False, "No driver or lock process is live"
                + (f"; live via {', '.join(sources)} only." if sources
                   else "."))
    elif not _owns_loop_process(pid):
        stop = (False, f"PID {pid} is not a loop driver command.")
    else:
        stop = (True, f"Sends SIGTERM to driver PID {pid}.")
    return {
        "driver": chosen,
        "start": {"enabled": start[0], "reason": start[1]},
        "stop": {"enabled": stop[0], "reason": stop[1]},
    }


def _live_card_fields(loop_dir: Path, root: Path | None) -> dict:
    """Card fields that must be fresh on every poll (never cached)."""
    driver_state = _driver_snapshot(loop_dir)
    detection = _running_detection(loop_dir, root)
    sources = detection["sources"]
    driver = driver_state["driver"] if driver_state else None
    return {
        "driver_phase": driver_state["phase"] if driver_state else None,
        "driver": driver,
        "running": bool(sources),
        "running_sources": sources,
        "running_substate": detection["substate"],
        "broker": detection["broker"],
        "broker_sessions": detection["broker_sessions"],
        "controls": _loop_controls(loop_dir, root, detection, driver),
        "last_action": _last_action(loop_dir),
    }


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


_GENERIC_GOAL_HEADINGS = {"goal", "mission", "objective", "task", "brief"}


def _goal_title(goal_path: Path, limit: int = 120) -> str:
    """Return GOAL.md's first heading as a short human title, or ``""``.

    ``# Mission: fix X`` becomes ``fix X`` (first letter capitalised); a bare
    generic heading such as ``# Goal`` yields ``""`` so callers fall back to
    the mailbox name instead of showing a meaningless word.
    """
    try:
        with goal_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line.startswith("#"):
                    continue
                text = line.lstrip("#").strip()
                text = re.sub(
                    r"^(mission|goal|objective)\s*(?::|—|–|-)\s*", "", text,
                    flags=re.IGNORECASE)
                if text.casefold() in _GENERIC_GOAL_HEADINGS:
                    return ""
                if text:
                    text = text[0].upper() + text[1:]
                if len(text) > limit:
                    text = text[: limit - 1] + "\u2026"
                return text
    except OSError:
        return ""
    return ""


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


def _verdict_mtime(loop_dir: Path) -> str | None:
    """Return VERDICT.md's modification time as an ISO UTC timestamp."""
    try:
        verdict_path = loop_dir / "VERDICT.md"
        if not verdict_path.is_file():
            return None
        modified = datetime.fromtimestamp(
            verdict_path.stat().st_mtime, tz=timezone.utc
        )
    except OSError:
        return None
    return _utc_iso(modified)


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


_DERIVED_SLICE_KEYS = (
    "lifecycle", "retired_sha", "retired_at", "verdict",
    "open_faults", "superseded", "stale_candidates",
)
"""derive_slices' output keys beyond id/iteration, which raw PLAN slices
already carry and keep unchanged by the merge below."""


def _loop_slices_derived(loop_dir: Path, mode: str, commits: list[dict]) -> list[dict]:
    """Raw PLAN.md slices merged with metrics.derive_slices' lifecycle keys.

    Per PLAN.md's frozen ``api:LoopDetailJSON`` DECISION note: every raw
    PLAN.md field (writes, reads, status, id, iteration, repo, gate,
    accepts -- existing app.js consumers read writes/reads/status) is
    preserved, and derive_slices' seven lifecycle keys (id/iteration are
    already present) are added on top, matched by id. Always a list --
    ``[]`` when PLAN.md has no ``slices:`` block -- never None. All parsing
    stays in trio-metrics.py; any failure in the derive step leaves the raw
    slices unmerged rather than 500ing.
    """
    raw = _loop_slices(loop_dir) or []
    if not raw:
        return []
    try:
        metrics = load_metrics_module()
        queue = metrics.read_queue(loop_dir)
        try:
            verdict_text = (loop_dir / "VERDICT.md").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            verdict_text = ""
        commit_subjects = [f"slice({c['slice']}): {c['subject']}" for c in commits]
        derived_by_id = {
            d["id"]: d for d in metrics.derive_slices(
                raw, queue, verdict_text, commit_subjects,
                open_loop=(mode == "open-loop"))
        }
        merged = []
        for sl in raw:
            entry = dict(sl)
            d = derived_by_id.get(sl.get("id"))
            if d:
                for key in _DERIVED_SLICE_KEYS:
                    entry[key] = d[key]
            merged.append(entry)
        return merged
    except Exception:
        traceback.print_exc()
        return raw


def _open_loop_iteration_lifecycle(iterations: list[dict], slices: list[dict]) -> list[dict]:
    """Open-loop iteration lifecycle, derived from slice lifecycle.

    GOAL.md (frozen): shipped iff all of that iteration's slices are
    shipped; else in_flight if any is building/retired/repairing; else
    pending_eval if any is retired and none faulted; else planned. The
    integration VERDICT: line still wins when present -- i.e. when
    derive_iterations already attributed a verdict to that iteration
    (``it["verdict"]`` is not None), its lifecycle is left untouched. An
    iteration with no slices also keeps its derive_iterations lifecycle.
    Lockstep derivation is untouched: callers gate this on mode ==
    "open-loop".
    """
    slices_by_n: dict[int, list] = {}
    for sl in slices or []:
        n = sl.get("iteration")
        if n is None:
            continue
        slices_by_n.setdefault(n, []).append(sl)
    out = []
    for it in iterations:
        entry = dict(it)
        if it.get("verdict") is None:
            slice_list = slices_by_n.get(it.get("n"))
            if slice_list:
                lifecycles = [s.get("lifecycle") for s in slice_list]
                if all(lc == "shipped" for lc in lifecycles):
                    entry["lifecycle"] = "shipped"
                elif any(lc in ("building", "retired", "repairing") for lc in lifecycles):
                    entry["lifecycle"] = "in_flight"
                elif any(lc == "retired" for lc in lifecycles) and not any(
                        lc == "faulted" for lc in lifecycles):
                    entry["lifecycle"] = "pending_eval"
                else:
                    entry["lifecycle"] = "planned"
        out.append(entry)
    return out


HEAVY_CACHE_SECONDS = 600.0
"""Upper bound on reusing a git-backed derivation whose inputs look unchanged."""

HEAVY_CACHE_MAX = 4096
_HEAVY_CACHE: dict[tuple, tuple] = {}
_HEAVY_CACHE_LOCK = threading.Lock()


def _dir_fingerprint(path: Path) -> tuple:
    """(name, mtime_ns, size) of a directory's direct files, sorted."""
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(
                (entry.name, entry.stat().st_mtime_ns, entry.stat().st_size)
                for entry in entries if entry.is_file()
            ))
    except OSError:
        return ()


def _git_fingerprint(root: Path) -> tuple:
    """Stat of the files a commit, checkout or ref update always touches."""
    try:
        probe = root.resolve()
    except OSError:
        return ()
    for candidate in (probe, *probe.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            gitdir = dot_git
            break
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return ()
            if not text.startswith("gitdir:"):
                return ()
            gitdir = (candidate / text[len("gitdir:"):].strip()).resolve()
            break
    else:
        return ()
    out = []
    for rel in ("HEAD", "logs/HEAD", "packed-refs", "commondir"):
        try:
            st = (gitdir / rel).stat()
            out.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((rel, None, None))
    return tuple(out)


def _jittered(seconds: float, key: str) -> float:
    """``seconds`` stretched by up to 50% by a stable hash of ``key``, so
    caches filled in one build do not all expire in the same later build."""
    spread = (zlib.crc32(key.encode("utf-8")) % 1000) / 1000.0
    return seconds * (1.0 + 0.5 * spread)


def _heavy(kind: str, loop_dir: Path, root: Path | None, compute):
    """Reuse ``compute()`` while the mailbox files and git state are unchanged.

    Keyed by mailbox + workspace; invalidated by any mailbox file write or a
    commit/checkout in the workspace repo, and in any case after
    ``HEAVY_CACHE_SECONDS``. Returns a deep copy so callers may mutate.
    """
    key = (kind, str(loop_dir), str(root))
    fingerprint = (
        _snapshot_memo(("dirfp", str(loop_dir)),
                       lambda: _dir_fingerprint(loop_dir)),
        _snapshot_memo(("gitfp", str(root)), lambda: _git_fingerprint(root))
        if root is not None else ())
    now = time.monotonic()
    with _HEAVY_CACHE_LOCK:
        hit = _HEAVY_CACHE.get(key)
        if hit and hit[0] == fingerprint and now - hit[1] <= _jittered(
                HEAVY_CACHE_SECONDS, str(loop_dir)):
            return copy.deepcopy(hit[2])
    value = compute()
    with _HEAVY_CACHE_LOCK:
        if len(_HEAVY_CACHE) >= HEAVY_CACHE_MAX:
            oldest = min(_HEAVY_CACHE, key=lambda k: _HEAVY_CACHE[k][1])
            _HEAVY_CACHE.pop(oldest, None)
        _HEAVY_CACHE[key] = (fingerprint, now, value)
    return copy.deepcopy(value)


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
    return _heavy("slice_activity", loop_dir, root,
                  lambda: _compute_slice_activity(loop_dir, root))


def _compute_slice_activity(loop_dir: Path, root: Path) -> dict | None:
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
    iterations, overlaps = _heavy(
        "iterations", loop_dir, root,
        lambda: _compute_loop_iterations(loop_dir, root))
    return iterations, overlaps


def _compute_loop_iterations(loop_dir: Path, root: Path) -> tuple[list[dict], list[dict]]:
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


def _loop_commits(loop_dir: Path, root: Path) -> list[dict]:
    return _heavy("commits", loop_dir, root,
                  lambda: _compute_loop_commits(loop_dir, root))


def _compute_loop_commits(loop_dir: Path, root: Path) -> list[dict]:
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


_RUNNING_STATUS_WORDS = {"running", "in_progress", "in-progress", "active",
                         "iterating"}

_HUMAN_STATUS_WORDS = {"needs_human", "needs-human", "awaiting_human",
                       "awaiting-human", "awaiting_user", "awaiting-user"}
"""STATE.md status words that hand the loop to a person."""
"""STATE.md status words that claim a loop is still working."""


def _inbox_items(loop_dir: Path, card: dict, root: Path) -> list[dict]:
    """Attention signals for one loop, highest severity first.

    Kinds: needs_human / blocked (high), orphaned / interrupted / drift /
    overlap / queue_fault / slice_overlap (medium), and repair (low). For open-loop
    mailboxes (QUEUE.md present) the iteration-overlap item is suppressed
    and replaced by queue_fault (one per open fault) and slice_overlap
    (write-set intersection between simultaneously-building slices);
    lockstep mailboxes keep the overlap item unchanged. Anything that
    cannot be determined is simply absent — the inbox never guesses.
    """
    items = []

    def add(severity, kind, headline, detail, anchor=None):
        item = {
            "loop": card["name"],
            "kind": kind,
            "severity": severity,
            "headline": headline,
            "detail": detail,
        }
        if anchor is not None:
            item["_inbox_anchor"] = anchor
        items.append(item)

    detection = _running_detection(loop_dir, root)
    orphaned = detection["orphaned_session"]
    if orphaned is not None and not detection["sources"]:
        pid = orphaned.get("pid")
        started_at = orphaned.get("started_at")
        pid_text = "" if pid is None else str(pid)
        started_text = "" if started_at is None else str(started_at)
        add(
            "medium",
            "orphaned",
            "Orphaned session sidecar",
            f"Session PID {pid_text or 'missing'} is not live.",
            f"session:{pid_text}:{started_text}",
        )

    verdict = (card.get("final_verdict") or "").upper()
    status = str(card.get("status") or "").strip().lower()
    if (
        status in _RUNNING_STATUS_WORDS
        and not detection["sources"]
        and orphaned is None
        and verdict not in ("SHIP", "NEEDS_HUMAN", "BLOCKED")
    ):
        # Two recorded facts disagree; no idle-time threshold is involved.
        # Without a readable broker, a broker-only loop cannot be ruled out,
        # so the item is a low-severity note instead of a call to act.
        last = card.get("last_activity") or ""
        when = (f" Last mailbox write {last[:16].replace('T', ' ')} UTC."
                if last else "")
        broker = detection.get("broker")
        working = _workspace_worker_count(root)
        if working:
            add("low", "interrupted",
                f"STATE.md says {status}; no worker names this mailbox",
                f"{working} loop worker{'s' if working != 1 else ''} "
                "(Trio driver, role runner or headless harness) "
                f"{'are' if working != 1 else 'is'} running in this "
                "workspace without naming a mailbox, so this loop may still "
                "be progressing." + when,
                f"interrupted:{last}")
        elif broker == "ok":
            add("medium", "interrupted",
                f"STATE.md says {status}; nothing is live",
                "No driver, lock, process, session sidecar or broker "
                "session is live." + when,
                f"interrupted:{last}")
        else:
            reason = {
                "disabled": "broker liveness is not configured",
                "unreachable": "the broker did not answer",
                "truncated": "the broker session list was too long to read",
            }.get(broker, "broker liveness is unknown")
            add("low", "interrupted",
                f"STATE.md says {status}; no local process",
                "No driver, lock, process or session sidecar is live; "
                f"{reason}, so a broker-only run cannot be ruled out." + when,
                f"interrupted:{last}")

    if verdict == "NEEDS_HUMAN":
        add("high", "needs_human", "Human verification pending",
            "Agent-verifiable criteria pass; verify: human criteria remain.")
    elif verdict == "BLOCKED":
        add("high", "blocked", "Loop blocked",
            card.get("last_entry_summary") or "")
    elif status in _HUMAN_STATUS_WORDS:
        # A Lead that stops for a decision records it in STATE.md; the
        # verdict file may still say "none" or the previous ITERATE.
        add("high", "needs_human", "STATE.md asks for a human",
            f"STATE.md status is {status}"
            + (f"; verdict file says {verdict}." if verdict else ".")
            + (f" Last log entry: {card.get('last_entry_summary')}."
               if card.get("last_entry_summary") else ""),
            f"state:{status}")
    elif status == "blocked":
        add("high", "blocked", "STATE.md says blocked",
            card.get("last_entry_summary") or "", "state:blocked")

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
                sorted_drift_files = sorted(drift_files)
                add("medium", "drift",
                    f"{len(drift_files)} undeclared write"
                    f"{'s' if len(drift_files) != 1 else ''}",
                    f"Across {drift_slices} slice"
                    f"{'s' if drift_slices != 1 else ''}: "
                    + ", ".join(sorted_drift_files[:4])
                    + ("…" if len(sorted_drift_files) > 4 else ""),
                    ",".join(sorted_drift_files))

    is_open_loop = (loop_dir / "QUEUE.md").is_file()

    if not is_open_loop:
        try:
            _, overlaps = _loop_iterations(loop_dir, root)
            for ov in overlaps:
                paths = ov["paths"]
                shown = ", ".join(paths[:6]) + ("\u2026" if len(paths) > 6 else "")
                verb = ("share write paths" if ov["relation"] == "write-write"
                        else "overlap read/write paths" if ov["relation"] == "write-read"
                        else "share write paths and read/write paths")
                add("medium", "overlap",
                    f"Iterations {ov['a']} and {ov['b']} {verb}", shown,
                    f"{ov['a']}:{ov['b']}:{ov['relation']}")
        except Exception:
            traceback.print_exc()
    else:
        # Open-loop mailboxes: one item per open fault, plus a write-set
        # overlap warning between simultaneously-building slices, replacing
        # the iteration-overlap item above (GOAL.md, PLAN.md's frozen
        # inbox-item contract).
        try:
            metrics = load_metrics_module()
            queue = metrics.read_queue(loop_dir)
            for fault in queue.get("faults") or []:
                if fault.get("status") != "open":
                    continue
                fid = fault.get("id") or "?"
                sid = fault.get("slice") or "?"
                scope = ", ".join(fault.get("scope") or [])
                reason = fault.get("reason") or ""
                add("medium", "queue_fault",
                    f"Open fault {fid} on slice {sid}",
                    f"scope: {scope}; reason: {reason}",
                    f"fault:{fid}")
        except Exception:
            traceback.print_exc()

        try:
            metrics = load_metrics_module()
            commits = _loop_commits(loop_dir, root)
            derived = _loop_slices_derived(loop_dir, "open-loop", commits)
            building = [sl for sl in derived if sl.get("lifecycle") == "building"]
            seen_pairs = set()
            for i, a in enumerate(building):
                for b in building[i + 1:]:
                    aw = {
                        p for p in (
                            metrics._norm_declared_path(x) for x in (a.get("writes") or [])
                        ) if p
                    }
                    bw = {
                        p for p in (
                            metrics._norm_declared_path(x) for x in (b.get("writes") or [])
                        ) if p
                    }
                    shared = metrics._intersect(aw, bw)
                    if not shared:
                        continue
                    pair = tuple(sorted((a.get("id"), b.get("id"))))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    add("medium", "slice_overlap",
                        f"Slices {pair[0]} and {pair[1]} share write paths",
                        ", ".join(sorted(shared)),
                        f"{pair[0]}:{pair[1]}")
        except Exception:
            traceback.print_exc()

    try:
        repairs = int((loop_dir / ".repairs").read_text().strip())
    except (OSError, ValueError):
        repairs = 0
    if repairs >= 1:
        add("low", "repair", f"{repairs} consecutive scoped repair"
             f"{'s' if repairs != 1 else ''}",
            "Repair-only loop risk: a full Lead pass is forced at 2.",
            str(repairs))

    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order[i["severity"]])
    return load_inbox_state_module().decorate_items(
        items, root, card["name"], loop_dir, HOME
    )


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
    writable_roots = list(_WRITABLE_ROOTS)
    writable_roots.extend(_GLOBAL_REGISTRY_DIRS.values())
    if HOME != _INITIAL_HOME:
        writable_roots.extend(
            Path(HOME) / relative
            for relative in _GLOBAL_REGISTRY_RELATIVE_DIRS.values()
        )
    for root in writable_roots:
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


def _normalize_agent_harnesses(value) -> list[str]:
    """Normalize one or more install-harness values without duplicates."""
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ValueError("harnesses must be a list")
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("harnesses must contain strings")
        for raw in item.split(","):
            harness = raw.strip().lower()
            if harness and harness not in normalized:
                normalized.append(harness)
    return normalized


def _agent_payload_harnesses(payload: dict):
    """Read the plural target field while retaining singular compatibility."""
    for key in ("harnesses", "target_harnesses", "targets"):
        if key in payload:
            return payload[key]
    return payload.get("harness")


def _normalize_agent_scope(value) -> str:
    """Return the supported canonical-agent destination scope."""
    scope = "global" if value is None else value
    if not isinstance(scope, str) or scope.strip().lower() not in (
        "global", "project",
    ):
        raise ValueError("scope must be 'project' or 'global'")
    return scope.strip().lower()


def _registry_target(
    harness, surface, name, scope="global", project=None, home=None
) -> Path:
    """Map a harness/surface/name tuple to its scoped file layout."""
    if not isinstance(harness, str) or not isinstance(surface, str):
        raise ValueError("invalid harness or surface")
    harness = harness.strip().lower()
    surface = surface.strip().lower()
    scope = _normalize_agent_scope(scope)
    if scope == "project":
        relative = _PROJECT_REGISTRY_DIRS.get((harness, surface))
        if relative is None:
            raise ValueError("unsupported project registry destination")
        if project is None:
            raise ValueError("project is required for project scope")
        root = Path(project).expanduser().resolve() / relative
    else:
        if home is None and HOME != _INITIAL_HOME:
            home = HOME
        root = _GLOBAL_REGISTRY_DIRS.get((harness, surface))
        relative = _GLOBAL_REGISTRY_RELATIVE_DIRS.get((harness, surface))
        if home is not None and relative is not None:
            root = Path(home).expanduser().resolve() / relative
        if root is None:
            raise ValueError("unsupported harness or surface")
    name = _safe_registry_name(name)
    if surface == "skill":
        return (root / name / "SKILL.md").resolve()
    if (harness, surface) == ("omnigent", "agent"):
        return (root / name / "config.yaml").resolve()
    registry = load_registry_module()
    fmt = registry.SURFACE_FORMAT.get((harness, surface), "yaml")
    ext = "toml" if fmt == "toml" else "md"
    return (root / f"{name}.{ext}").resolve()


def _registry_target_with_scope(
    harness, surface, name, scope, project=None, home=None
) -> tuple[Path, str]:
    """Resolve a registry target and report its effective destination scope."""
    requested_scope = _normalize_agent_scope(scope)
    try:
        target = _registry_target(
            harness,
            surface,
            name,
            scope=requested_scope,
            project=project,
            home=home,
        )
    except ValueError as exc:
        # Only an absent project layout is allowed to fall back. Other
        # validation errors must retain _registry_target's behavior.
        if str(exc) != "unsupported project registry destination":
            raise
        target = _registry_target(
            harness,
            surface,
            name,
            scope="global",
            home=home,
        )
        return target, "global"
    return target, requested_scope


def _agent_registry_target(
    harness, name, scope, project=None, home=None
) -> tuple[Path, str]:
    """Resolve an agent target and report its effective destination scope."""
    requested_scope = _normalize_agent_scope(scope)
    try:
        target = _registry_target(
            harness,
            "agent",
            name,
            scope=requested_scope,
            project=project,
            home=home,
        )
    except ValueError as exc:
        # Only an absent project layout is allowed to fall back. Other
        # validation errors must retain _registry_target's behavior.
        if str(exc) != "unsupported project registry destination":
            raise
        target = _registry_target(
            harness,
            "agent",
            name,
            scope="global",
            home=home,
        )
        return target, "global"
    return target, requested_scope


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


class _OmnigentRegistrationError(RuntimeError):
    """A broker failure that still leaves the rendered YAML on disk."""

    def __init__(self, path: Path, broker_error: Exception):
        self.path = path
        self.broker_error = str(broker_error)
        super().__init__(self.broker_error)


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


def _env_list(name: str) -> set[str]:
    return {
        value.strip().lower().rstrip("/")
        for value in os.environ.get(name, "").split(",") if value.strip()
    }


def _allowed_origins() -> set[str]:
    return _env_list("TRIO_DASH_ALLOWED_ORIGINS")


def _split_host(host: str) -> str:
    """Hostname part of a Host header (``[::1]:80`` -> ``::1``)."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _host_allowed(host: str) -> bool:
    name = _split_host(host).rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True  # an IP literal cannot be a rebinding attacker's name
    except ValueError:
        pass
    return name == "localhost" or name in _env_list("TRIO_DASH_ALLOWED_HOSTS")


def _origin_allowed(origin: str, host: str) -> bool:
    value = origin.strip().lower().rstrip("/")
    if value in _allowed_origins():
        return True
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if not host or parsed.netloc != host.strip().lower():
        return False
    return _host_allowed(parsed.netloc)


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the dashboard API and static files."""

    server_version = "TrioLoopDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- request guards ----------------------------------------------------

    def _request_allowed(self, mutating: bool) -> bool:
        """Reject DNS-rebinding hosts and cross-origin writes.

        Every request needs a Host that is an IP literal, ``localhost`` or a
        name in ``TRIO_DASH_ALLOWED_HOSTS``. A mutating request with an
        ``Origin`` must come from the same origin as its Host or from
        ``TRIO_DASH_ALLOWED_ORIGINS``; browsers' cross-site fetches are
        refused; and any request body must be ``application/json`` so a
        cross-origin "simple" form/text POST cannot reach a handler.
        """
        host = (self.headers.get("Host") or "").strip()
        if host and not _host_allowed(host):
            self._reject(421, "host not allowed")
            return False
        if not mutating:
            return True
        origin = (self.headers.get("Origin") or "").strip()
        if origin and not _origin_allowed(origin, host):
            self._reject(403, "cross-origin request refused")
            return False
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site == "cross-site" and not (
                origin and origin.lower() in _allowed_origins()):
            self._reject(403, "cross-site request refused")
            return False
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length != 0:
            media = (self.headers.get("Content-Type") or "").split(";", 1)[0]
            if media.strip().lower() != "application/json":
                self._reject(415, "request body must be application/json")
                return False
        return True

    def _reject(self, code: int, message: str) -> None:
        # Refused requests may leave an unread body; close the connection
        # and say so, or a proxy that pools connections (tailscale serve)
        # sends the next request into a dead socket.
        self.close_connection = True
        body = json.dumps({"error": message}).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, format: str, *args) -> None:
        """Log errors and writes; skip successful reads (polls, probes)."""
        try:
            status = int(str(args[1])) if len(args) > 1 else 0
        except ValueError:
            status = 0
        if self.command == "GET" and 200 <= status < 400:
            return
        super().log_message(format, *args)

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
        seeds = (self.server.get_workspace_seeds()
                 + self.server.get_worktree_seeds())
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
            # Prompt inputs are not registry entries, but exposing them
            # read-only lets the managed-file editor open its producing source.
            if not _path_is_under(target, root / "prompts") or not target.is_file():
                return self._send_json(404, {"error": "file not found"})
            entry = {"managed": False, "source": None}
        try:
            _check_project_registry_entry(
                entry, target, root, self.server.get_workspace_seeds())
            text = target.read_text(encoding="utf-8", errors="replace")
            registry = load_registry_module()
            fmt = registry.file_format(target)
            surface_fmt = registry.SURFACE_FORMAT.get(
                (entry.get("harness"), entry.get("surface")))
            if surface_fmt == "yaml-document":
                fmt = surface_fmt
                frontmatter, body = registry.split_file(text, fmt)
            elif fmt == "toml":
                frontmatter, body = registry.split_file(text, fmt)
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
            "source": entry.get("source"),
            "quoted_keys": registry.quoted_key_paths(frontmatter),
        })

    def _handle_registry_regenerate(self, query: dict) -> None:
        """Regenerate a managed destination after a clean-tree check."""
        try:
            payload = _read_json_body(self)
            target = _resolve_registry_path(payload.get("path"))
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})

        root_query = query
        if "root" in payload:
            root_query = {"root": [payload.get("root")]}
        try:
            root = self._resolve_root(root_query)
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})

        registry = load_registry_module()
        source = registry.generated_sources().get(str(target))
        if not source:
            return self._send_json(
                400, {"error": "path is not managed by prompts/generate.py"})
        entry = _registry_entry(_registry_index(root), target)
        if not entry or not entry.get("source"):
            return self._send_json(
                400, {"error": "path is not managed by prompts/generate.py"})
        source = entry["source"]

        install_flags = {
            "claude": "--global",
            "codex": "--codex",
            "omnigent": "--omnigent",
            "kimi": "--kimi",
            "zcode": "--zcode",
            "opencode": "--opencode",
            "omp": "--omp",
            "pi": "--pi",
        }
        harness = str(entry.get("harness") or "").strip().lower()
        install_flag = install_flags.get(harness)
        if install_flag is None:
            return self._send_json(400, {
                "error": f"unsupported generator harness: {harness or 'unknown'}",
            })

        try:
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._send_json(400, {"error": "workspace is not a git repository"})
        if status.returncode != 0:
            return self._send_json(400, {"error": "workspace is not a git repository"})
        dirty_files = [
            line[3:].strip() if len(line) > 3 else line.strip()
            for line in status.stdout.splitlines()
            if line.strip()
        ]
        if dirty_files:
            return self._send_json(409, {
                "error": "working tree dirty",
                "files": dirty_files,
            })

        try:
            generated = subprocess.run(
                ["python3", "prompts/generate.py"],
                cwd=root, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._send_json(500, {"error": "generate.py failed"})
        if generated.returncode != 0:
            detail = generated.stderr or generated.stdout
            return self._send_json(500, {
                "error": "generate.py failed",
                "detail": detail[:65536],
            })

        env = os.environ.copy()
        # Keep global harness installation under the dashboard's configured
        # home. Tests replace serve.HOME with a temporary directory here.
        env["HOME"] = str(HOME)
        try:
            installed = subprocess.run(
                ["./install.sh", install_flag],
                cwd=root, env=env, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return self._send_json(500, {"error": "install.sh failed"})
        if installed.returncode != 0:
            detail = installed.stderr or installed.stdout
            return self._send_json(500, {
                "error": "install.sh failed",
                "detail": detail[:65536],
            })

        wrote = []
        for line in generated.stdout.splitlines():
            line = line.strip()
            if line.startswith("wrote "):
                wrote.append(line[len("wrote "):])
        install_output = installed.stdout
        if len(install_output) > 65536:
            install_output = install_output[:65536] + "\n[output truncated]"
        _invalidate_registry_cache()
        self._send_json(200, {
            "source": source,
            "wrote": wrote,
            "diff": generated.stdout,
            "install": install_output,
        })

    def _handle_registry_topology(
        self, root: Path, workflow: str = "roles",
        include_home: bool = False,
    ) -> None:
        """Return topology for an explicit root and optional dashboard home."""
        topology = load_topology_module()
        if include_home:
            payload = topology.collect_topology(
                root, home=HOME, workflow=workflow)
        else:
            payload = topology.collect_topology(root, workflow=workflow)
        payload["include_home"] = include_home
        self._send_json(200, payload)

    def _handle_registry_models(self, root: Path) -> None:
        """Return model resolution rows for an explicit root."""
        models = load_models_module()
        self._send_json(
            200,
            models.collect_models(
                root,
                home=HOME,
                env=os.environ,
                allow_cli=True,
            ),
        )

    def _handle_registry_health(self, root: Path) -> None:
        """Return lineage and installation health for an explicit root."""
        health = load_health_module()
        self._send_json(200, health.collect_health(root, home=HOME))

    def _handle_registry_schema(self) -> None:
        """Static harness/surface schema: destinations, formats, key specs."""
        registry = load_registry_module()
        formats = {
            f"{harness}:{surface}": fmt
            for (harness, surface), fmt in registry.SURFACE_FORMAT.items()
        }
        keys = dict(registry.KEY_SCHEMA)
        self._send_json(200, {
            "destinations": _registry_destination_catalog(),
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
        if fmt not in ("yaml", "yaml-document", "toml", "text"):
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

        schema_key = (
            f"{harness_norm}:{surface_norm}" if harness_norm and surface_norm else None
        )
        schema = registry.KEY_SCHEMA.get(schema_key) if schema_key else None
        dotted_schema_keys = {
            spec["key"] for spec in (schema or []) if "." in spec["key"]
        }
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

        resolved = registry.unflatten_dotted_keys(resolved, dotted_schema_keys)

        # JSON turns scan.py's quoted string-key markers into plain strings.
        # Restore them before the existing YAML writer renders the response.
        if fmt in ("yaml", "yaml-document"):
            registry.apply_quoted_key_paths(
                resolved, payload.get("quoted_keys"))

        warnings: list[str] = []
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
            known_keys = {key.split(".", 1)[0] for key in schema_by_key}
            for key in resolved:
                if key not in known_keys:
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
            if _path_is_under(target, root / "prompts"):
                raise PermissionError("prompt source files are read-only")
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
            scope = _normalize_agent_scope(payload.get("scope"))
            project_root = None
            if scope == "project":
                project_value = payload.get("project")
                if not isinstance(project_value, str) or not project_value.strip():
                    raise ValueError("project is required for project scope")
                project_root = self._agent_project_root(project_value)
            target, scope_used = _registry_target_with_scope(
                harness,
                surface,
                name,
                scope=scope,
                project=project_root,
            )
            _writable_registry_path(
                str(target),
                self.server.get_workspace_seeds(),
                project_root if scope_used == "project" else None,
            )
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
        self._send_json(201, {
            "path": str(target),
            "scope_used": scope_used,
        })

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
            if _path_is_under(target, root / "prompts"):
                raise PermissionError("prompt source files are read-only")
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
            "spawns": list(agent.spawns),
            "harness_overrides": agent.harness_overrides,
            "path": str(agents.agents_dir() / f"{agent.name}.md"),
        }

    def _agent_detail(self, agent, agents) -> dict:
        detail = self._agent_summary(agent, agents)
        detail["instructions"] = agent.instructions
        detail["harness_defaults"] = self._harness_defaults(agents, agent)
        return detail

    def _handle_agent_defaults(self, query: dict) -> None:
        """Return renderer defaults for a hypothetical canonical agent."""
        agents = load_agents_module()
        model_tier = (query.get("model_tier") or [None])[0]
        tool_policy = (query.get("tool_policy") or [None])[0]
        try:
            hypothetical = agents.CanonicalAgent(
                name="new-agent",
                description="TODO: describe this agent.",
                instructions="",
                model_tier=model_tier,
                tool_policy=tool_policy,
                harness_overrides={},
            )
            defaults = self._harness_defaults(agents, hypothetical)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        self._send_json(200, {
            "model_tier": model_tier,
            "tool_policy": tool_policy,
            "harness_defaults": defaults,
        })

    @staticmethod
    def _harness_defaults(agents, agent) -> dict:
        """Renderer fields with overrides stripped, used as the catalog baseline."""
        bare = agents.CanonicalAgent(
            name=agent.name,
            description=agent.description,
            instructions=agent.instructions,
            model_tier=agent.model_tier,
            tool_policy=agent.tool_policy,
            spawns=list(agent.spawns),
            harness_overrides={},
        )
        defaults = {}
        for harness in agents.RENDER_HARNESSES:
            rendered = agents.render_agent(bare, harness)
            fields, _body = agents.scan.split_file(
                rendered.text, rendered.format)
            defaults[harness] = fields
        return defaults

    def _agent_from_payload(self, agents, payload: dict, *, existing=None):
        instructions = payload.get("instructions")
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        # Omitted spawns/overrides on PUT must not wipe the stored maps.
        if "spawns" in payload:
            spawns = payload.get("spawns")
        elif existing is not None:
            spawns = list(existing.spawns)
        else:
            spawns = []
        if "harness_overrides" in payload:
            overrides = payload.get("harness_overrides")
        elif existing is not None:
            overrides = copy.deepcopy(existing.harness_overrides)
        else:
            overrides = {}
        return agents.CanonicalAgent(
            name=payload.get("name"),
            description=payload.get("description"),
            instructions=instructions,
            model_tier=payload.get("model_tier"),
            tool_policy=payload.get("tool_policy"),
            spawns=spawns,
            harness_overrides=overrides,
        )

    def _agent_project_root(self, value) -> Path:
        """Resolve a project target and keep it inside a selected workspace."""
        root = (
            self.server.default_root
            if value is None or (isinstance(value, str) and not value.strip())
            else _resolve_registry_path(value)
        )
        if not any(
            _path_is_under(root, workspace)
            for workspace in self.server.get_workspace_seeds()
        ):
            raise PermissionError("project registry path is not in a workspace")
        return root

    def _agent_install_plan(
        self, agent, agents, harnesses, scope, project_root=None
    ) -> list[dict]:
        """Render and authorize every requested install before writing any."""
        plan = []
        for harness in harnesses:
            supported, reason = agents.install_support(harness)
            if not supported:
                raise agents.UnsupportedHarness(harness, reason)
            rendered = agents.render_agent(agent, harness)
            target, scope_used = _agent_registry_target(
                harness,
                agent.name,
                scope,
                project=project_root,
            )
            target = _writable_registry_path(
                str(target),
                self.server.get_workspace_seeds(),
                project_root if scope_used == "project" else None,
            )
            _reject_if_managed(target)
            plan.append({
                "harness": harness,
                "rendered": rendered,
                "target": target,
                "scope_used": scope_used,
            })
        return plan

    @staticmethod
    def _register_omnigent_bundle(target: Path) -> tuple[str, str]:
        """Upload an Omnigent config and persist its broker identity."""
        broker = load_broker_http_module()
        sidecar_path = target.parent / "broker.json"
        try:
            sidecar = json.loads(
                sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            sidecar = {}
        if not isinstance(sidecar, dict):
            sidecar = {}

        session_id = sidecar.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = None
        bundle = broker.bundle_agent_dir(target)
        title = f"dashboard-install-{target.parent.name}"
        try:
            if session_id:
                try:
                    agent_id = broker.update_agent_bundle(session_id, bundle)
                    agent_name = sidecar.get("agent_name")
                except broker.BrokerHttpError as exc:
                    if exc.status_code != 404:
                        raise
                    registration = broker.register_agent_bundle(bundle, title)
                    agent_id = registration["agent_id"]
                    session_id = registration["session_id"]
                    agent_name = registration["agent_name"]
            else:
                registration = broker.register_agent_bundle(bundle, title)
                agent_id = registration["agent_id"]
                session_id = registration["session_id"]
                agent_name = registration["agent_name"]
        except broker.BrokerHttpError as exc:
            raise _OmnigentRegistrationError(target, exc) from exc

        if not isinstance(agent_name, str) or not agent_name:
            agent_name = target.parent.name
        sidecar = {
            "agent_id": agent_id,
            "session_id": session_id,
            "agent_name": agent_name,
        }
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2) + "\n",
            encoding="utf-8",
        )
        return agent_id, session_id

    @staticmethod
    def _write_agent_plan(plan) -> list[dict]:
        """Write an already-authorized install plan and report each target."""
        results = []
        for item in plan:
            target = item["target"]
            created = not target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_registry_file(target, item["rendered"].text)
            rendered = item["rendered"]
            results.append({
                "path": str(target),
                "harness": item["harness"],
                "format": rendered.format,
                "filename": rendered.filename,
                "created": created,
                "scope_used": item["scope_used"],
            })
            if item["harness"] == "omnigent":
                agent_id, session_id = DashboardHandler._register_omnigent_bundle(
                    target)
                results[-1]["agent_id"] = agent_id
                results[-1]["session_id"] = session_id
                results[-1]["register"] = (
                    f"sys_session_create(agent_id={agent_id})")
        return results

    def _agent_destination_list(
        self, agents, harnesses, scope, project_root=None, name="__preview__"
    ) -> list[dict]:
        """Return resolved paths for the pre-confirmation destination preview."""
        destinations = []
        registry = load_registry_module()
        for harness in harnesses:
            supported, reason = agents.install_support(harness)
            if not supported:
                raise agents.UnsupportedHarness(harness, reason)
            target, scope_used = _agent_registry_target(
                harness,
                name,
                scope,
                project=project_root,
            )
            destinations.append({
                "harness": harness,
                "path": str(target),
                "format": registry.SURFACE_FORMAT[(harness, "agent")],
                "scope_used": scope_used,
            })
            if harness == "omnigent":
                destinations[-1]["register"] = (
                    f"sys_session_create(config_path={target})")
        return destinations

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
            raw_harnesses = _agent_payload_harnesses(payload)
            harnesses = _normalize_agent_harnesses(raw_harnesses)
            scope = _normalize_agent_scope(payload.get("scope"))
            project_root = (
                self._agent_project_root(payload.get("project"))
                if scope == "project"
                else None
            )
            plan = self._agent_install_plan(
                agent, agents, harnesses, scope, project_root)
        except agents.UnsupportedHarness as exc:
            return self._send_json(400, {
                "error": f"harness {exc.harness!r} does not support "
                         "canonical-agent install",
                "reason": exc.reason,
                "harness": exc.harness,
                "supported": False,
            })
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if (agents.agents_dir() / f"{agent.name}.md").exists():
            return self._send_json(409, {"error": "agent already exists"})
        path = agents.save_agent(agent)
        try:
            installations = self._write_agent_plan(plan)
        except IsADirectoryError:
            return self._send_json(400, {"error": "path is a directory"})
        except _OmnigentRegistrationError as exc:
            _invalidate_registry_cache()
            return self._send_json(502, {
                "error": f"omnigent broker registration failed: "
                         f"{exc.broker_error}",
                "broker_error": exc.broker_error,
                "path": str(exc.path),
            })
        except OSError:
            return self._send_json(500, {"error": "could not write file"})
        _invalidate_registry_cache()
        self._send_json(201, {
            "path": str(path),
            "scope": scope,
            "installations": installations,
        })

    def _handle_agent_update(self) -> None:
        agents = load_agents_module()
        try:
            payload = _read_json_body(self)
            existing = None
            try:
                existing = agents.load_agent(payload.get("name"))
            except (FileNotFoundError, TypeError, ValueError, OSError):
                existing = None
            agent = self._agent_from_payload(
                agents, payload, existing=existing)
            raw_harnesses = _agent_payload_harnesses(payload)
            harnesses = _normalize_agent_harnesses(raw_harnesses)
            scope = _normalize_agent_scope(payload.get("scope"))
            project_root = (
                self._agent_project_root(payload.get("project"))
                if scope == "project"
                else None
            )
            plan = self._agent_install_plan(
                agent, agents, harnesses, scope, project_root)
        except agents.UnsupportedHarness as exc:
            return self._send_json(400, {
                "error": f"harness {exc.harness!r} does not support "
                         "canonical-agent install",
                "reason": exc.reason,
                "harness": exc.harness,
                "supported": False,
            })
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if not (agents.agents_dir() / f"{agent.name}.md").exists():
            return self._send_json(404, {"error": "agent not found"})
        path = agents.save_agent(agent)
        try:
            installations = self._write_agent_plan(plan)
        except IsADirectoryError:
            return self._send_json(400, {"error": "path is a directory"})
        except _OmnigentRegistrationError as exc:
            _invalidate_registry_cache()
            return self._send_json(502, {
                "error": f"omnigent broker registration failed: "
                         f"{exc.broker_error}",
                "broker_error": exc.broker_error,
                "path": str(exc.path),
            })
        except OSError:
            return self._send_json(500, {"error": "could not write file"})
        _invalidate_registry_cache()
        self._send_json(200, {
            "path": str(path),
            "scope": scope,
            "installations": installations,
        })

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

    def _handle_agent_destinations(self, query: dict) -> None:
        """Resolve agent install paths for the editor's pre-confirmation view."""
        agents = load_agents_module()
        try:
            name = _safe_registry_name((query.get("name") or [None])[0])
            raw_harnesses = query.get("harnesses") or query.get("harness")
            harnesses = _normalize_agent_harnesses(raw_harnesses)
            if not harnesses:
                harnesses = list(agents.RENDER_HARNESSES)
            scope = _normalize_agent_scope(
                (query.get("scope") or [None])[0])
            project_root = (
                self._agent_project_root((query.get("project") or [None])[0])
                if scope == "project"
                else None
            )
            destinations = self._agent_destination_list(
                agents, harnesses, scope, project_root, name)
        except agents.UnsupportedHarness as exc:
            return self._send_json(400, {
                "error": f"harness {exc.harness!r} does not support "
                         "canonical-agent install",
                "reason": exc.reason,
                "harness": exc.harness,
                "supported": False,
            })
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        self._send_json(200, {
            "name": name,
            "scope": scope,
            "destinations": destinations,
        })

    def _handle_agent_install(self) -> None:
        agents = load_agents_module()
        try:
            payload = _read_json_body(self)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        harness = payload.get("harness")
        raw_harnesses = _agent_payload_harnesses(payload)
        try:
            harnesses = _normalize_agent_harnesses(raw_harnesses)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        if not harnesses:
            return self._send_json(400, {
                "error": f"harness {harness!r} does not support "
                         "canonical-agent install",
                "reason": "missing harness",
                "harness": harness,
                "supported": False,
            })
        harness_norm = harnesses[0] if len(harnesses) == 1 else None
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
        try:
            scope = _normalize_agent_scope(payload.get("scope"))
            project_root = (
                self._agent_project_root(payload.get("project"))
                if scope == "project"
                else None
            )
            plan = self._agent_install_plan(
                agent, agents, harnesses, scope, project_root)
        except agents.UnsupportedHarness as exc:
            return self._send_json(400, {
                "error": f"harness {harness!r} does not support "
                         "canonical-agent install",
                "reason": exc.reason,
                "harness": exc.harness,
                "supported": False,
            })
        except PermissionError as exc:
            return self._send_json(403, {"error": str(exc)})
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        try:
            results = self._write_agent_plan(plan)
        except IsADirectoryError:
            return self._send_json(400, {"error": "path is a directory"})
        except _OmnigentRegistrationError as exc:
            _invalidate_registry_cache()
            return self._send_json(502, {
                "error": f"omnigent broker registration failed: "
                         f"{exc.broker_error}",
                "broker_error": exc.broker_error,
                "path": str(exc.path),
            })
        except OSError:
            return self._send_json(500, {"error": "could not write file"})
        _invalidate_registry_cache()
        if len(results) == 1:
            result = results[0]
            self._send_json(201 if result["created"] else 200, result)
        else:
            self._send_json(201, {
                "agent": agent.name,
                "scope": scope,
                "installations": results,
            })

    # -- /api/board --------------------------------------------------------

    def _loop_card(self, loop_dir: Path, metrics, root: Path | None = None) -> dict:
        def static() -> dict:
            analysis = metrics.analyze_loop(loop_dir, root)
            entries = metrics.parse_log(loop_dir / "LOG.md")
            return {
                "name": analysis["name"],
                "path": analysis["name"],
                "mission": _mission_from_goal(loop_dir / "GOAL.md"),
                "title": _goal_title(loop_dir / "GOAL.md"),
                "iteration": _to_int(analysis["state_iteration"]),
                "max_iterations": _to_int(analysis["state_max_iterations"]),
                "status": analysis["state_status"] or "unknown",
                "final_verdict": analysis["final_verdict"],
                "last_activity": _last_activity(loop_dir, entries),
                "verdict_mtime": _verdict_mtime(loop_dir),
                "last_entry_summary": _last_entry_summary(entries),
                "segments": analysis["segments"],
            }

        card = _heavy("card", loop_dir, root, static)
        card.update(_live_card_fields(loop_dir, root))
        return card

    def _handle_board(self, root: Path) -> None:
        self._send_json(200, self._board_payload(root))

    def _board_payload(
        self, root: Path, processes: list | None = None,
        broker: dict | None = None, only: dict | None = None,
    ) -> dict:
        with _proc_snapshot(processes, broker):
            if only is None:
                return self._build_board(root)
            return self._build_board(root, only)

    def _build_board(self, root: Path, only: dict | None = None) -> dict:
        """Cards and inbox for a workspace; ``only`` limits a linked
        worktree to its selected mailboxes ({path: reasons})."""
        metrics = self.server.metrics
        loop_dirs = _discover_loops_cached(root)
        # Broker titles carry only the mailbox dir name; a name used twice in
        # one workspace cannot be attributed to either mailbox.
        seen: dict[str, int] = {}
        for loop_dir in loop_dirs:
            seen[loop_dir.name] = seen.get(loop_dir.name, 0) + 1
        ambiguous = getattr(_PROC_SNAPSHOT, "ambiguous", None)
        if ambiguous is not None:
            ambiguous[str(root)] = {n for n, c in seen.items() if c > 1}
        if only is not None:
            loop_dirs = [d for d in loop_dirs if str(d) in only]
        loops = []
        for loop_dir in loop_dirs:
            try:
                loops.append(self._loop_card(loop_dir, metrics, root))
            except Exception:
                traceback.print_exc()
                # Keep the board alive even if one loop's mailbox is broken.
                card = {
                    "name": metrics.loop_name(root, loop_dir),
                    "path": metrics.loop_name(root, loop_dir),
                    "mission": "",
                    "title": "",
                    "iteration": None,
                    "max_iterations": None,
                    "status": "unknown",
                    "final_verdict": None,
                    "last_activity": None,
                    "verdict_mtime": _verdict_mtime(loop_dir),
                    "last_entry_summary": "unreadable mailbox",
                    "segments": [],
                }
                card.update(_live_card_fields(loop_dir, root))
                loops.append(card)
        if only is not None:
            for loop_dir, card in zip(loop_dirs, loops):
                card["worktree_reasons"] = only.get(str(loop_dir), [])
        inbox = []
        for loop_dir, card in zip(loop_dirs, loops):
            try:
                inbox.extend(_inbox_items(loop_dir, card, root))
            except Exception:
                traceback.print_exc()
        order = {"high": 0, "medium": 1, "low": 2}
        inbox.sort(key=lambda i: (order[i["severity"]], i["loop"]))
        return {
            "loops": loops,
            "inbox": inbox,
            "broker": (getattr(_PROC_SNAPSHOT, "broker", None)
                       or {}).get("status", "disabled"),
            "updated_at": _utc_iso(datetime.now(timezone.utc)),
        }

    # -- /api/overview -----------------------------------------------------

    def _handle_overview(self) -> None:
        self._send_json(200, self.server.overview(self._board_payload))

    # -- /healthz ----------------------------------------------------------

    def _handle_healthz(self) -> None:
        server = self.server
        self._send_json(200, {
            "ok": True,
            "uptime_seconds": round(time.monotonic() - server.started_at, 1),
            "version": server.version,
            "workspaces": len(server.get_workspace_seeds()),
            "overview_age_seconds": server.overview_age(),
            "broker": (server._overview or {}).get("broker"),
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
        name = str(name).strip()
        if (
            not name
            or name.startswith(("/", "\\"))
            or ".." in name.replace("\\", "/").split("/")
        ):
            return None
        return next(
            (p for p in metrics.discover_loops(root)
             if metrics.loop_name(root, p) == name),
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
        card = self._loop_card(loop_dir, self.server.metrics, root)
        card["mission"] = _mission_from_goal(loop_dir / "GOAL.md", limit=4000)
        card["timeline"] = _loop_timeline(loop_dir / "LOG.md")
        card["commits"] = _loop_commits(loop_dir, root)
        mode = "open-loop" if (loop_dir / "QUEUE.md").is_file() else "lockstep"
        card["mode"] = mode
        card["slices"] = _loop_slices_derived(loop_dir, mode, card["commits"])
        if mode == "open-loop":
            try:
                card["queue"] = self.server.metrics.read_queue(loop_dir)
            except Exception:
                traceback.print_exc()
                card["queue"] = {"retired": [], "faults": []}
        card["slice_activity"] = _loop_slice_activity(loop_dir, root)
        card["iterations"], card["overlaps"] = _loop_iterations(loop_dir, root)
        if mode == "open-loop":
            card["iterations"] = _open_loop_iteration_lifecycle(
                card["iterations"], card["slices"])
        card["sessions"] = self._session_list(loop_dir, root)
        self._send_json(200, card)

    # -- /api/inbox/read and /api/inbox/unread ------------------------------

    def _handle_inbox_read(self, read: bool) -> None:
        """Update inbox state without reading or writing any mailbox files."""
        payload = self._read_loop_body()
        if payload is None:
            return
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return self._send_json(400, {"error": "ids must be a list"})
        if any(not isinstance(identity, str) for identity in ids):
            return self._send_json(400, {"error": "ids must contain strings"})
        root = self._resolve_loop_root(payload)
        if root is None:
            return
        load_inbox_state_module().set_read(root, ids, read, HOME)
        self._send_json(200, {"ok": True, "ids": ids})

    # -- /api/loop controls -------------------------------------------------

    def _read_loop_body(self) -> dict | None:
        """Read a loop-control body and turn malformed JSON into a 400."""
        try:
            return _read_json_body(self)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return None

    def _resolve_loop_root(self, payload: dict) -> Path | None:
        """Resolve a loop-control root through the existing seed allowlist.

        Controls never fall back to the default workspace: ``root`` is
        required.
        """
        if not isinstance(payload.get("root"), str) or not payload["root"].strip():
            self._send_json(400, {"error": "root is required"})
            return None
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
        detection = _running_detection(mailbox, root)
        controls = _loop_controls(mailbox, root, detection, driver)
        if not controls["start"]["enabled"]:
            return self._send_json(
                409, {"error": controls["start"]["reason"]})
        entrypoint = DRIVER_ENTRYPOINTS[driver]
        if driver == "portable":
            command = [
                "python3", str(entrypoint), "run",
                "--mailbox", str(mailbox),
                "--max-iterations", str(max_iterations),
                "--runner", "portable",
            ]
        else:
            command = [
                "python3", str(entrypoint), "omnigent", "loop",
                "--mailbox", str(mailbox),
                "--max-iterations", str(max_iterations),
            ]
        log_path = _launch_log_path(mailbox)
        try:
            log = open(log_path, "ab")
        except OSError:
            log = None
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log if log is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log is not None
                else subprocess.DEVNULL,
            )
        except OSError as exc:
            _record_action(mailbox, action="start", outcome="failed",
                           message=f"could not start {driver} driver: {exc}")
            return self._send_json(500, {"error": "could not start loop"})
        finally:
            if log is not None:
                log.close()
        # Answer only once the driver has survived its startup: a driver
        # that exits at once must not leave a sidecar that looks running.
        try:
            code = process.wait(timeout=LAUNCH_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            code = None
        if code is not None:
            tail = _log_tail(log_path)
            message = f"{driver} driver exited during startup (code {code})"
            _record_action(mailbox, action="start", outcome="failed",
                           pid=process.pid, exit_code=code, message=message,
                           log=str(log_path))
            return self._send_json(502, {
                "error": message, "log": str(log_path), "log_tail": tail})
        with _LOOP_PROCESSES_LOCK:
            _LOOP_PROCESSES[process.pid] = process
        _seed_driver_state(mailbox, process.pid, driver)
        _record_action(mailbox, action="start", outcome="running",
                       pid=process.pid, driver=driver, log=str(log_path),
                       message=f"{driver} driver started (PID {process.pid})")
        threading.Thread(
            target=_reap_loop_process, args=(mailbox, process),
            daemon=True).start()
        self._send_json(202, {
            "pid": process.pid,
            "driver": driver,
            "mailbox": str(mailbox),
            "log": str(log_path),
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
        _record_action(mailbox, action="stop", outcome="stopping", pid=pid,
                       message=f"SIGTERM sent to driver PID {pid}")
        with _LOOP_PROCESSES_LOCK:
            process = _LOOP_PROCESSES.pop(pid, None)
        if process is not None:
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not _pid_is_live(pid):
            _record_action(mailbox, outcome="stopped",
                           message=f"Driver PID {pid} stopped")
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
        if not self._request_allowed(mutating=False):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in STATIC_ROUTES:
            name, ctype = STATIC_ROUTES[path]
            return self._serve_static(name, ctype)
        if path == "/healthz":
            return self._api(self._handle_healthz)
        if path == "/api/workspaces":
            return self._api(self._handle_workspaces)
        if path == "/api/overview":
            return self._api(self._handle_overview)
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
            workflow = (query.get("workflow") or ["roles"])[0]
            topology = load_topology_module()
            if workflow not in topology.WORKFLOWS:
                return self._send_json(
                    400, {"error": f"unknown topology workflow: {workflow}"})
            root = self._request_root(query)
            if root is None:
                return
            home_values = query.get("home")
            if home_values:
                home_value = home_values[0].strip().casefold()
                if home_value in ("1", "true"):
                    include_home = True
                elif home_value in ("0", "false"):
                    include_home = False
                else:
                    return self._send_json(
                        400,
                        {"error": "home must be 0, 1, true, or false"},
                    )
            else:
                # Repo checkouts have the workspace's Omnigent sources nearby;
                # installed trio-dash copies rely on the explicit home scan.
                include_home = not (
                    DASHBOARD_DIR.parent / "omnigent"
                ).is_dir()
            return self._api(
                lambda: self._handle_registry_topology(
                    root, workflow, include_home))
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
        if path == "/api/registry/agents/defaults":
            return self._api(lambda: self._handle_agent_defaults(query))
        if path == "/api/registry/agents/destinations":
            return self._api(lambda: self._handle_agent_destinations(query))
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
        if not self._request_allowed(mutating=True):
            return
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
        if not self._request_allowed(mutating=True):
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/registry/serialize":
            return self._api(self._handle_registry_serialize)
        if path == "/api/registry/regenerate":
            return self._api(lambda: self._handle_registry_regenerate(query))
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
        if path == "/api/inbox/read":
            return self._api(lambda: self._handle_inbox_read(True))
        if path == "/api/inbox/unread":
            return self._api(lambda: self._handle_inbox_read(False))
        if path == "/api/loop/start":
            return self._api(self._handle_loop_start)
        if path == "/api/loop/stop":
            return self._api(self._handle_loop_stop)
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if not self._request_allowed(mutating=True):
            return
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


def _workspace_scan_roots() -> list[Path]:
    """Directories below which workspaces are auto-discovered.

    ``TRIO_DASH_SCAN_ROOTS`` (``os.pathsep``-separated) replaces the default
    list, so a long-running service can cover e.g. ``~/personal``; see
    ``_discover_workspaces`` for the bounded walk.
    """
    configured = os.environ.get("TRIO_DASH_SCAN_ROOTS", "").strip()
    if configured:
        return [
            Path(value).expanduser()
            for value in configured.split(os.pathsep) if value.strip()
        ]
    return [
        HOME / "pruebas",
        HOME / "Projects",
        HOME / "projects",
        HOME / "dev",
        HOME / "src",
        HOME / "code",
        HOME / "repos",
        HOME / "work",
    ]


SCAN_SKIP_NAMES = {
    "node_modules", "venv", "__pycache__", "site-packages", "dist", "build",
    "target", "vendor", "coverage", "tmp", "cache",
}
"""Directory names never descended into while discovering workspaces."""

SCAN_DIR_BUDGET = 4000
"""Most directories listed per scan root per discovery pass."""


def _scan_depth() -> int:
    try:
        return max(1, min(6, int(os.environ.get("TRIO_DASH_SCAN_DEPTH", "3"))))
    except ValueError:
        return 3


def _has_loop_mailbox(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(
                entry.name.startswith("loop") and entry.is_dir()
                for entry in entries
            )
    except OSError:
        return False


def _discover_workspaces(scan_root: Path) -> list[Path]:
    """Workspaces (not linked worktrees) below one scan root."""
    return _scan_root_walk(scan_root)[0]


def _discover_worktrees(scan_root: Path) -> list[Path]:
    """Linked git worktrees below one scan root (same bounded walk)."""
    return _scan_root_walk(scan_root)[1]


_SCAN_WALK_CACHE: dict[str, tuple[float, tuple]] = {}


def _scan_root_walk(scan_root: Path) -> tuple[list[Path], list[Path]]:
    """Workspaces and linked worktrees below one scan root, bounded.

    Direct children are always workspaces (the registry pages use them).
    Deeper directories, down to ``TRIO_DASH_SCAN_DEPTH`` levels, count only
    when they hold a ``loop*`` directory. A linked git worktree (``.git`` is
    a file) is returned separately and never entered: its mailboxes are
    mostly committed copies of the main checkout's, and
    ``_worktree_mailboxes`` picks the ones that matter. Dot-dirs,
    dependency/build dirs and loop mailboxes themselves are never entered,
    the walk lists at most ``SCAN_DIR_BUDGET`` directories, and HOME or
    ``/`` are refused as scan roots so secrets and caches are never walked.
    """
    try:
        root = scan_root.expanduser().resolve()
    except OSError:
        return [], []
    if root in (HOME.resolve(), Path("/")) or not root.is_dir():
        if root in (HOME.resolve(), Path("/")):
            print(f"note: refusing to scan {root} for workspaces; list its "
                  "project directories in TRIO_DASH_SCAN_ROOTS instead",
                  file=sys.stderr)
        return [], []
    found: list[Path] = []
    worktrees: list[Path] = []
    depth_limit = _scan_depth()
    budget = SCAN_DIR_BUDGET
    frontier = [(root, 0)]
    while frontier and budget > 0:
        directory, depth = frontier.pop(0)
        budget -= 1
        try:
            children = sorted(
                entry.path for entry in os.scandir(directory)
                if entry.is_dir(follow_symlinks=False)
                and not entry.name.startswith(".")
                and not entry.name.startswith("loop")
                and entry.name not in SCAN_SKIP_NAMES
            )
        except OSError:
            continue
        for child_text in children:
            child = Path(child_text)
            if (child / ".git").is_file():
                if _has_loop_mailbox(child):
                    worktrees.append(child)
                continue
            if depth == 0 or _has_loop_mailbox(child):
                found.append(child)
            if depth + 1 < depth_limit:
                frontier.append((child, depth + 1))
    return found, worktrees


# -- linked worktrees -------------------------------------------------------

WORKTREE_GIT_SECONDS = 90.0
"""Longest reuse (jittered up to +50%) of a worktree's ``git status``
labels while its index and HEAD reflog are unchanged; an ordinary edit of a
tracked mailbox file shows up within this bound."""

WORKTREE_REFS_SECONDS = 300.0
"""Longest reuse of a worktree's ancestry evidence with unchanged refs."""

_WORKTREE_GIT_CACHE: dict[str, tuple] = {}
_WORKTREE_GIT_LOCK = threading.Lock()


def _worktree_gitdir(worktree: Path) -> Path | None:
    try:
        text = (worktree / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    value = text[len("gitdir:"):].strip()
    gitdir = Path(value) if os.path.isabs(value) else worktree / value
    try:
        return gitdir.resolve()
    except OSError:
        return None


def _worktree_main(worktree: Path) -> Path | None:
    """The main checkout a linked worktree belongs to, if it has one."""
    gitdir = _worktree_gitdir(worktree)
    if gitdir is None:
        return None
    try:
        common = (gitdir / (gitdir / "commondir").read_text(
            encoding="utf-8").strip()).resolve()
    except OSError:
        return None
    if common.name != ".git":
        return None  # bare repository: no main checkout to compare with
    return common.parent


_RUNTIME_FILES = {".driver.json", ".session.json", ".lock", ".repairs",
                  ".driver.json.dashboard-tmp"}
"""Runtime files a running loop writes that are not mailbox content."""


def _worktree_git_labels(worktree: Path, mailboxes: list[Path]) -> dict:
    """``git status`` of a worktree's mailboxes: ``untracked``/``modified``,
    plus ``runtime`` for untracked or ignored sidecars/locks.

    One ``git status --porcelain -uall --ignored=matching -- <mailboxes>``
    per worktree (``--no-optional-locks``: never rewrites the index), reused
    while the worktree's index and HEAD reflog are unchanged and for at most
    a jittered ``WORKTREE_GIT_SECONDS`` (edits that touch neither show up
    within that bound; liveness is checked every build).
    """
    gitdir = _worktree_gitdir(worktree)
    stamps = []
    for rel in ("index", "logs/HEAD"):
        try:
            stamps.append((gitdir / rel).stat().st_mtime_ns if gitdir else None)
        except OSError:
            stamps.append(None)
    fingerprint = (tuple(stamps), tuple(str(m) for m in mailboxes))
    key = str(worktree)
    now = time.monotonic()
    with _WORKTREE_GIT_LOCK:
        hit = _WORKTREE_GIT_CACHE.get(key)
        if hit and hit[0] == fingerprint and now - hit[1] <= _jittered(
                WORKTREE_GIT_SECONDS, key):
            return dict(hit[2])
    rels = [str(m.relative_to(worktree)) for m in mailboxes]
    labels: dict[str, set] = {str(m): set() for m in mailboxes}
    lines: list[str] = []
    if rels:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(worktree), "status",
                 "--porcelain", "-uall", "--ignored=matching", "--", *rels],
                capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                lines = result.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            lines = []
    # Deepest mailbox first, so a child's file never labels its container.
    ordered = sorted(mailboxes, key=lambda m: len(m.parts), reverse=True)
    for line in lines:
        code, path = line[:2], line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        target = worktree / path.rstrip("/")
        runtime = bool(
            {part for part in target.parts[-2:]} & _RUNTIME_FILES)
        if code == "!!" and not runtime:
            continue  # other ignored files are not loop activity
        for mailbox in ordered:
            if target == mailbox or mailbox in target.parents:
                if runtime and code in ("??", "!!"):
                    # Git never creates untracked/ignored runtime files:
                    # a loop ran in this worktree.
                    labels[str(mailbox)].add("runtime")
                else:
                    labels[str(mailbox)].add(
                        "untracked" if code == "??" else "modified")
                break
    value = {k: sorted(v) for k, v in labels.items()}
    with _WORKTREE_GIT_LOCK:
        _WORKTREE_GIT_CACHE[key] = (fingerprint, now, value)
    return dict(value)


DISCOVER_CACHE_SECONDS = 60.0
_DISCOVER_CACHE: dict[str, tuple] = {}
_DISCOVER_LOCK = threading.Lock()


def _loop_dirs_fingerprint(root: Path) -> tuple:
    """mtimes of the root and its ``loop*`` dirs: they change when a mailbox
    directory is created or removed at either level."""
    stamps = []
    try:
        stamps.append(root.stat().st_mtime_ns)
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith("loop") and entry.is_dir():
                    stamps.append((entry.name, entry.stat().st_mtime_ns))
    except OSError:
        pass
    return tuple(sorted(stamps, key=str))


def _discover_loops_cached(root: Path) -> list[Path]:
    """``discover_loops(root)``, reused while the loop dirs are unchanged
    (and for at most a jittered ``DISCOVER_CACHE_SECONDS``, which covers a
    directory that becomes a mailbox by gaining its first GOAL.md)."""
    key = str(root)
    fingerprint = _loop_dirs_fingerprint(root)
    now = time.monotonic()
    with _DISCOVER_LOCK:
        hit = _DISCOVER_CACHE.get(key)
        if hit and hit[0] == fingerprint and now - hit[1] <= _jittered(
                DISCOVER_CACHE_SECONDS, key):
            return list(hit[2])
    loops = list(load_metrics_module().discover_loops(root))
    with _DISCOVER_LOCK:
        _DISCOVER_CACHE[key] = (fingerprint, now, loops)
    return list(loops)


def _git_out(cwd: Path, *args: str) -> str | None:
    """stdout of a read-only git command, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _common_gitdir(worktree: Path) -> Path | None:
    gitdir = _worktree_gitdir(worktree)
    if gitdir is None:
        return None
    try:
        return (gitdir / (gitdir / "commondir").read_text(
            encoding="utf-8").strip()).resolve()
    except OSError:
        return None


_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def _resolve_ref(common: Path, gitdir: Path, ref: str, depth: int = 0) -> str | None:
    """Resolve ``HEAD`` or ``refs/...`` by reading ref files and
    packed-refs; None when that needs git itself (then callers ask git)."""
    if depth > 4:
        return None
    if ref == "HEAD":
        try:
            text = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if _SHA_RE.match(text):
            return text
        if not text.startswith("ref:"):
            return None
        return _resolve_ref(common, gitdir, text[4:].strip(), depth + 1)
    for base in (gitdir, common):
        try:
            text = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _SHA_RE.match(text):
            return text
        if text.startswith("ref:"):
            return _resolve_ref(common, gitdir, text[4:].strip(), depth + 1)
    try:
        with open(common / "packed-refs", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _SHA_RE.match(parts[0]):
                    return parts[0]
    except OSError:
        pass
    return None


def _worktree_heads(worktree: Path) -> tuple[str | None, str | None, str]:
    """(worktree HEAD, base tip, base description) as commit ids.

    The base tip is the main checkout's HEAD; for a worktree of a bare
    repository (no main checkout) it is ``main``/``master``/``origin/HEAD``.
    Ref files are read directly; git is asked only when that fails.
    """
    gitdir = _worktree_gitdir(worktree)
    common = _common_gitdir(worktree)
    head = tip = None
    description = "no main checkout or main/master branch"
    if gitdir is not None and common is not None:
        head = _resolve_ref(common, gitdir, "HEAD")
        main = _worktree_main(worktree)
        if main is not None:
            tip = _resolve_ref(common, common, "HEAD")
            try:
                branch = (common / "HEAD").read_text(encoding="utf-8").strip()
            except OSError:
                branch = ""
            branch = branch[len("ref: refs/heads/"):] if branch.startswith(
                "ref: refs/heads/") else "detached"
            description = f"{main.name} HEAD ({branch})"
            if tip is None:
                tip = (_git_out(main, "rev-parse", "--verify", "-q", "HEAD")
                       or "").strip() or None
        else:
            for ref in ("refs/heads/main", "refs/heads/master",
                        "refs/remotes/origin/HEAD"):
                tip = _resolve_ref(common, common, ref) or (
                    _git_out(worktree, "rev-parse", "--verify", "-q", ref)
                    or "").strip() or None
                if tip:
                    description = ref
                    break
    if head is None:
        head = (_git_out(worktree, "rev-parse", "--verify", "-q", "HEAD")
                or "").strip() or None
    return head, tip, description


def _deepest_mailbox(target: Path, ordered: list[Path]) -> Path | None:
    for mailbox in ordered:
        if target == mailbox or mailbox in target.parents:
            return mailbox
    return None


_WORKTREE_REFS_CACHE: dict[str, tuple] = {}
_WORKTREE_REFS_LOCK = threading.Lock()


def _worktree_ancestry(worktree: Path, mailboxes: list[Path]) -> dict:
    """Ancestry evidence for a worktree's mailboxes, from git objects only
    (never file mtimes, so merges, rebases, resets, stashes and touched
    files cannot move it).

    With ``mb = merge-base(HEAD, base tip)``: a mailbox's own files are
    those added, modified or deleted between ``mb`` and HEAD; they still
    belong to the branch when the base tip does not already have the same
    content at that path. ``changed`` maps such mailboxes to
    ``changed``/``deleted``; ``new`` lists those among them absent from
    the ``mb`` tree. Cached on (HEAD, tip) commit ids, so a commit
    elsewhere in the repository does not recompute every worktree.
    """
    head, tip, description = _worktree_heads(worktree)
    fingerprint = (head, tip, tuple(str(m) for m in mailboxes))
    key = str(worktree)
    now = time.monotonic()
    with _WORKTREE_REFS_LOCK:
        hit = _WORKTREE_REFS_CACHE.get(key)
        if hit and hit[0] == fingerprint and now - hit[1] <= _jittered(
                WORKTREE_REFS_SECONDS, key):
            return copy.deepcopy(hit[2])
    rels = [str(m.relative_to(worktree)) for m in mailboxes]
    ordered = sorted(mailboxes, key=lambda m: len(m.parts), reverse=True)
    merge_base = None
    if tip and head:
        merge_base = (_git_out(worktree, "merge-base", head, tip)
                      or "").strip() or None
        if merge_base is None:
            description += "; no common history"
    result = {"base": description, "merge_base": merge_base,
              "changed": {}, "new": []}
    if merge_base and rels and merge_base != head:
        own = _git_out(worktree, "diff", "--name-status", "--no-renames",
                       merge_base, head, "--", *rels)
        own_files: dict[str, str] = {}
        for line in (own or "").splitlines():
            status, _, path = line.partition("\t")
            if path:
                own_files[path] = "deleted" if status.startswith("D") else "changed"
        if own_files:
            touched = sorted({
                str(m.relative_to(worktree)) for m in
                (_deepest_mailbox(worktree / f, ordered) for f in own_files)
                if m is not None})
            differs = set((_git_out(worktree, "diff", "--name-only", tip,
                                    head, "--", *touched) or "").splitlines())
            for path, kind in own_files.items():
                if path not in differs:
                    continue  # the base tip already has this exact content
                mailbox = _deepest_mailbox(worktree / path, ordered)
                if mailbox is not None:
                    kinds = result["changed"].setdefault(str(mailbox), [])
                    if kind not in kinds:
                        kinds.append(kind)
            if result["changed"]:
                present = _git_out(
                    worktree, "ls-tree", "-d", "--name-only", merge_base,
                    "--", *[str(Path(k).relative_to(worktree))
                            for k in result["changed"]])
                at_base = {str(worktree / line)
                           for line in (present or "").splitlines()}
                result["new"] = [k for k in result["changed"]
                                 if k not in at_base]
    with _WORKTREE_REFS_LOCK:
        _WORKTREE_REFS_CACHE[key] = (fingerprint, now, copy.deepcopy(result))
    return result


_ACTIONABLE_STATUS = (_RUNNING_STATUS_WORDS | _HUMAN_STATUS_WORDS
                      | {"blocked"})


def _mailbox_actionable(mailbox: Path) -> bool:
    """STATE.md or VERDICT.md says the loop is running or needs a person."""
    try:
        state = (mailbox / "STATE.md").read_text(
            encoding="utf-8", errors="replace")
        m = re.search(r"(?im)^status:\s*([\w-]+)", state)
        if m and m.group(1).strip().lower() in _ACTIONABLE_STATUS:
            return True
    except OSError:
        pass
    try:
        head = (mailbox / "VERDICT.md").read_text(
            encoding="utf-8", errors="replace")[:200].upper()
        return "NEEDS_HUMAN" in head or "BLOCKED" in head
    except OSError:
        return False


def _worktree_static_selection(worktree: Path) -> tuple[list[Path], dict]:
    """(all mailboxes, {path: reasons}) from git evidence.

    Reasons: ``new on branch`` (absent from the merge-base tree, with
    content the base tip does not have), ``committed on branch`` /
    ``deleted on branch`` (own changes since the merge-base the base tip
    does not already have), ``modified`` / ``untracked`` (git status), ``runtime
    files present`` (untracked or ignored sidecars/locks, counted when the
    mailbox has other evidence or STATE/VERDICT says running or needs a
    person; a finished loop's leftovers are not pending work). Without a merge-base (no main checkout or
    branch, unrelated histories) nothing can be called inherited, so
    mailboxes whose STATE/VERDICT is running or needs a person are shown
    with ``no common base (…)`` instead of being hidden.
    """
    try:
        mailboxes = _discover_loops_cached(worktree)
    except OSError:
        mailboxes = []
    ancestry = _worktree_ancestry(worktree, mailboxes)
    labels = _worktree_git_labels(worktree, mailboxes)
    new = set(ancestry["new"])
    reasons: dict[str, list[str]] = {}
    for mailbox in mailboxes:
        key = str(mailbox)
        found = []
        if key in new:
            found.append("new on branch")
        else:
            for kind in sorted(ancestry["changed"].get(key, [])):
                found.append("deleted on branch" if kind == "deleted"
                             else "committed on branch")
        tags = labels.get(key) or []
        found.extend(t for t in tags if t != "runtime")
        # Leftover sidecars prove a loop ran here, not that it is pending:
        # they count for loops that still say running / need a person.
        if "runtime" in tags and (found or _mailbox_actionable(mailbox)):
            found.append("runtime files present")
        if ancestry["merge_base"] is None and _mailbox_actionable(mailbox):
            found.append(f"no common base ({ancestry['base']})")
        if found:
            reasons[key] = found
    return mailboxes, reasons


def _worktree_live_candidates(worktree: Path, mailboxes: list[Path],
                              listing: dict) -> list[Path]:
    """Mailboxes some live argv path or broker session points at.

    Uses the snapshot's sorted argv path index (one binary search per
    worktree) and the broker listing; no per-mailbox file access.
    """
    base = str(worktree)
    paths = getattr(_PROC_SNAPSHOT, "paths", None)
    named = []
    if paths:
        start = bisect.bisect_left(paths, base + os.sep)
        for path in paths[start:]:
            if not path.startswith(base + os.sep):
                break
            named.append(path)
    sessions = [
        s for s in listing.get("running", [])
        if s.get("workspace") and s.get("title", "").startswith("trioctl ")
    ]
    out = []
    for mailbox in mailboxes:
        text = str(mailbox)
        if any(p == text or p.startswith(text + os.sep) for p in named):
            out.append(mailbox)
            continue
        prefix = f"trioctl {mailbox.name} "
        if any(s["title"].startswith(prefix) for s in sessions):
            out.append(mailbox)
    return out


def _worktree_mailboxes(worktree: Path, listing: dict) -> dict[str, list[str]]:
    """Mailboxes of a linked worktree worth showing, with the reasons.

    A committed copy the worktree only inherited is left out. A mailbox is
    shown when any of these holds:

    * ``new on branch`` / ``committed on branch`` / ``deleted on branch``
      — git ancestry: absent from ``merge-base(HEAD, base tip)``, or files
      changed since it that the base tip does not already have;
    * ``untracked`` / ``modified`` — ``git status`` reports uncommitted
      files in it;
    * ``runtime files present`` — untracked or ignored sidecars/locks
      exist, which git never creates, so a loop ran here; shown alone only
      while STATE/VERDICT says running or needs a person; tracked sidecars
      (some repos commit them) do not count;
    * ``no common base (…)`` — no merge-base could be found, and STATE or
      VERDICT says running / needs a person;
    * ``live`` — a live argv path, a broker session whose workspace is this
      worktree, or a live sidecar; checked on every build.
    """
    mailboxes, reasons = _worktree_static_selection(worktree)
    for mailbox in _worktree_live_candidates(worktree, mailboxes, listing):
        if _running_detection(mailbox, worktree)["sources"]:
            reasons.setdefault(str(mailbox), []).append("live")
    # A mailbox already shown for its files also gets its sidecar checked.
    for key in list(reasons):
        if "live" in reasons[key]:
            continue
        mailbox = Path(key)
        if any((mailbox / n).exists()
               for n in (".driver.json", ".session.json", ".lock")):
            if _running_detection(mailbox, worktree)["sources"]:
                reasons[key].append("live")
    return reasons


_INTERPRETERS = {"python", "python3", "node", "bun", "deno", "uv", "uvx",
                 "bash", "sh", "env"}


def _command_words(args: list[str]) -> list[str]:
    """argv with interpreter prefixes (``python3 -I x``, ``env``, ``node``)
    stripped, program names reduced to their basename."""
    words = list(args)
    while words:
        name = os.path.basename(words[0])
        base = re.sub(r"[\d.]+$", "", name)  # python3.12 -> python
        if base in _INTERPRETERS or name in _INTERPRETERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            continue
        break
    if words:
        words[0] = os.path.basename(words[0])
    return words


def _is_loop_worker(args: list[str]) -> bool:
    """A process positively shaped like loop work.

    Trio drivers and role runners (``trioctl … run|loop``, ``trio_loop.py``,
    ``portable/driver.sh``) and headless harness runs (``claude -p/--print``,
    ``codex exec``, ``cursor-agent -p/--print``, ``opencode run``,
    ``omp -p``). Interactive sessions, MCP servers and anything unknown are
    not workers, so they never mask a stopped loop.
    """
    words = _command_words(args)
    if not words:
        return False
    if any(w.endswith("portable/driver.sh") for w in args[:3]):
        return True
    program, rest = words[0], words[1:]
    if "mcp" in program.lower() or any(
            w in ("mcp", "mcp-server", "serve-mcp") for w in rest[:2]):
        return False
    if program in ("trio_loop.py",):
        return True
    if program == "trioctl":
        return any(w in ("run", "loop") for w in rest[:3])
    if program == "claude":
        return "-p" in rest or "--print" in rest
    if program == "codex":
        return "exec" in rest  # options such as -c k=v may precede it
    if program in ("cursor-agent", "agent"):
        return "-p" in rest or "--print" in rest
    if program == "opencode":
        return "run" in rest
    if program == "omp":
        return "-p" in rest or "--print" in rest
    return False


def _workspace_worker_count(root: Path, processes=None) -> int:
    """Live loop-worker processes (``_is_loop_worker``) whose cwd is inside
    ``root``."""
    if processes is None:
        processes = getattr(_PROC_SNAPSHOT, "processes", None)
        if processes is None:
            processes = _live_processes() or []

    def compute() -> int:
        try:
            base = str(root.resolve())
        except OSError:
            return 0
        return sum(
            1 for _, args, cwd in processes or ()
            if cwd and (cwd == base or cwd.startswith(base + os.sep))
            and _is_loop_worker(args)
        )

    return _snapshot_memo(("workers", str(root)), compute)


def _unattributed_processes(root: Path, loops: list[dict],
                            processes) -> int:
    """Loop-worker processes whose cwd is inside ``root`` while no loop of
    that workspace is running (so none of them was attributed)."""
    if any(loop.get("running") for loop in loops):
        return 0
    return _workspace_worker_count(root, processes)


def _worktree_label(worktree: Path) -> str:
    main = _worktree_main(worktree)
    base = main.name if main is not None else worktree.parent.name
    return f"{base} (worktree {worktree.name})"


def _dashboard_version() -> str:
    """Short git revision of the served dashboard, or ``"unknown"``."""
    try:
        result = subprocess.run(
            ["git", "-C", str(DASHBOARD_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


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
        self.worktree_seeds: tuple[Path, ...] = ()
        self.default_root = (
            Path(root).resolve() if root is not None
            else self._fixed_workspace_seeds[0]
        )
        self.root = self.default_root
        self.metrics = load_metrics_module()
        self._overview_lock = threading.Lock()
        self._overview_state_lock = threading.Lock()
        self._overview_building = False
        self._overview = None
        self._overview_at = 0.0
        self.started_at = time.monotonic()
        self._version = None
        self.get_workspace_seeds(force=True)
        super().__init__(address, DashboardHandler)

    def overview(self, board_payload) -> dict:
        """Return every workspace's board without making viewers wait.

        The first request builds synchronously. After that a poll gets the
        last build immediately and, when it is older than
        ``OVERVIEW_CACHE_SECONDS``, starts one background rebuild; concurrent
        polls never scan the same mailboxes twice. A workspace that fails
        keeps its slot with an ``error`` so the rest still show.
        """
        with self._overview_state_lock:
            cached = self._overview
            fresh = (
                cached is not None
                and time.monotonic() - self._overview_at
                <= OVERVIEW_CACHE_SECONDS
            )
            if cached is not None and not fresh and not self._overview_building:
                self._overview_building = True
                threading.Thread(
                    target=self._rebuild_overview, args=(board_payload,),
                    daemon=True).start()
        if cached is not None:
            return cached
        with self._overview_lock:
            if self._overview is not None:
                return self._overview
            return self._build_overview(board_payload)

    def _rebuild_overview(self, board_payload) -> None:
        try:
            with self._overview_lock:
                self._build_overview(board_payload)
        except Exception:
            traceback.print_exc()
        finally:
            with self._overview_state_lock:
                self._overview_building = False

    def _build_overview(self, board_payload) -> dict:
        """Scan every workspace now; callers hold ``_overview_lock``."""
        now = time.monotonic()
        seeds = self.get_workspace_seeds()
        processes = _live_processes()
        broker = _broker_listing()

        def build(seed: Path) -> dict:
            started = time.monotonic()
            entry = {"root": str(seed), "name": seed.name}
            try:
                board = board_payload(seed, processes, broker)
                entry["loops"] = board["loops"]
                entry["inbox"] = board["inbox"]
            except Exception as exc:
                traceback.print_exc()
                entry["loops"] = []
                entry["inbox"] = []
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            return entry

        def build_worktree(worktree: Path) -> dict | None:
            started = time.monotonic()
            with _proc_snapshot(processes, broker):
                only = _worktree_mailboxes(worktree, broker)
            if not only:
                return None
            entry = {"root": str(worktree), "name": _worktree_label(worktree),
                     "worktree": True}
            try:
                board = board_payload(worktree, processes, broker, only)
                entry["loops"] = board["loops"]
                entry["inbox"] = board["inbox"]
            except Exception as exc:
                traceback.print_exc()
                entry["loops"] = []
                entry["inbox"] = []
                entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            return entry

        worktrees = self.get_worktree_seeds()
        with ThreadPoolExecutor(max_workers=OVERVIEW_WORKERS) as pool:
            entries = list(pool.map(build, seeds))
            worktree_entries = [
                e for e in pool.map(build_worktree, worktrees) if e]
        names = [entry["name"] for entry in entries]
        for entry, seed in zip(entries, seeds):
            if names.count(entry["name"]) > 1:
                entry["name"] = f"{seed.parent.name}/{seed.name}"
        entries.extend(worktree_entries)
        # Processes working inside a workspace without naming any mailbox
        # (e.g. builders started with only --config/--workspace): shown as
        # evidence, never attributed to a loop.
        for entry in worktree_entries:
            entry["unattributed_processes"] = _unattributed_processes(
                Path(entry["root"]), entry["loops"], processes)
        self._overview = {
            "workspaces": [
                entry for entry in entries
                if entry["loops"] or entry.get("error")
            ],
            "scanned": len(seeds),
            "worktrees_scanned": len(worktrees),
            "scan_roots": [str(r) for r in _workspace_scan_roots()]
            if self.workspace_discovery_enabled else [],
            "broker": broker.get("status", "disabled"),
            "updated_at": _utc_iso(datetime.now(timezone.utc)),
            "elapsed_ms": int((time.monotonic() - now) * 1000),
        }
        self._overview_at = time.monotonic()
        return self._overview

    @property
    def version(self) -> str:
        """Served git revision, resolved on first use (not at startup)."""
        if self._version is None:
            self._version = _dashboard_version()
        return self._version

    def overview_age(self) -> float | None:
        """Seconds since the last overview build, or None before the first."""
        if self._overview is None:
            return None
        return round(time.monotonic() - self._overview_at, 1)

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
            worktrees: list[Path] = []
            for scan_root in _workspace_scan_roots():
                found, linked = _scan_root_walk(scan_root)
                for seed in found:
                    if seed not in seeds:
                        seeds.append(seed)
                for worktree in linked:
                    if worktree not in seeds and worktree not in worktrees:
                        worktrees.append(worktree)
            self.workspace_seeds = tuple(seeds)
            self.worktree_seeds = tuple(worktrees)
            self.workspace_scan_at = now
            return self.workspace_seeds

    def get_worktree_seeds(self) -> tuple[Path, ...]:
        """Linked worktrees found by the last discovery pass (not listed as
        workspaces; the overview shows only their relevant mailboxes)."""
        self.get_workspace_seeds()
        return self.worktree_seeds

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
    parser.add_argument(
        "--discover", action="store_true",
        help="also auto-discover workspaces below TRIO_DASH_SCAN_ROOTS "
             "when --workspace is given",
    )
    # Keep the old spelling for scripts that have not migrated yet.
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    registered = [
        value for value in
        os.environ.get("TRIO_DASH_WORKSPACES", "").split(os.pathsep)
        if value.strip()
    ]
    if args.workspace_paths is not None:
        raw_workspaces = args.workspace_paths + registered
        auto_discover = args.discover
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
    bind_host = args.host
    for port in candidate_ports:
        try:
            server = DashboardServer(
                (args.host, port),
                workspaces=workspaces,
                auto_discover=auto_discover,
            )
            break
        except OSError as exc:
            # A wildcard bind collides with `tailscale serve` holding the
            # same port on the tailnet address; the proxy targets loopback,
            # so binding 127.0.0.1 on that port is what remote access needs.
            if args.host in ("0.0.0.0", "::", ""):
                try:
                    server = DashboardServer(
                        ("127.0.0.1", port),
                        workspaces=workspaces,
                        auto_discover=auto_discover,
                    )
                    bind_host = "127.0.0.1"
                    print(f"note: {args.host}:{port} is taken (tailscale serve?); bound 127.0.0.1:{port} instead", file=sys.stderr)
                    break
                except OSError:
                    pass
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

    print(f"Trio Loop Dashboard listening on http://{bind_host}:{port} (root: {root})", flush=True)

    def prewarm() -> None:
        # Build the overview once so the first viewer after a restart does
        # not wait for a cold scan of every workspace.
        time.sleep(0.5)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/overview", timeout=120).read()
        except (OSError, ValueError):
            pass

    if bind_host in ("127.0.0.1", "0.0.0.0", "", "localhost"):
        threading.Thread(target=prewarm, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Agent identity by session START directory (stdlib only).

Loaded by file path (no package-relative imports).  A session's identity is
where it was started, never where it later wandered: the broker ``workspace``,
the first ``cwd`` recorded in a Claude transcript, the Codex ``session_meta``
cwd or the Cursor ``meta.json`` cwd.  The start directory is normalised like
loop ids (git common dir + path relative to the checkout's toplevel, see
``loop_ids.repo_identity``), so the same directory seen through the main
checkout or any linked worktree has one identity:

  <repo>/agents/<name>[/...]  ->  ``<name>``
  <repo> (the checkout root)  ->  ``coordinator``
  anything else, in or out of git  ->  ``unassigned``
  no start directory known    ->  None (``identity_source: "unavailable"``)

Subagents and workflow builders run in temporary worktrees; they inherit the
PARENT session's identity (``inherit``), not their own worktree path.

``identity_source`` vocabulary: broker-workspace, claude-transcript-cwd,
codex-session-meta, cursor-cwd, omnigent-export-workspace,
``inherited:<source>`` and unavailable.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath

COORDINATOR = "coordinator"
UNASSIGNED = "unassigned"
UNAVAILABLE = "unavailable"
INHERITED_PREFIX = "inherited:"
AGENTS_DIR = "agents"

_LINE_CAP = 4 * 1024 * 1024
_LOOP_IDS = None


def _loop_ids():
    global _LOOP_IDS
    if _LOOP_IDS is None:
        path = Path(__file__).resolve().with_name("loop_ids.py")
        spec = importlib.util.spec_from_file_location(
            "trio_agent_identity_loop_ids", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOOP_IDS = module
    return _LOOP_IDS


def _empty(identity=None, start_dir=None) -> dict:
    return {"identity": identity, "start_dir": start_dir,
            "common_dir": None, "rel": None}


def identity_for_start_dir(start_dir) -> dict:
    """``{"identity", "start_dir", "common_dir", "rel"}`` for a start dir.

    Never raises: a directory that no longer exists is resolved as far as
    possible (its real path, and the nearest existing ancestor for the git
    lookup)."""
    if not isinstance(start_dir, str) or not start_dir.strip():
        return _empty()
    try:
        real = os.path.realpath(start_dir)
    except (OSError, ValueError):
        return _empty(UNASSIGNED, start_dir)
    try:
        repo = _loop_ids().repo_identity(real)
    except Exception:  # noqa: BLE001 - identity must never break a listing
        repo = None
    if not repo:
        return _empty(UNASSIGNED, real)
    try:
        rel = os.path.relpath(real, repo["toplevel"])
    except ValueError:
        return {"identity": UNASSIGNED, "start_dir": real,
                "common_dir": repo["common_dir"], "rel": None}
    rel = "" if rel == "." else PurePosixPath(*rel.split(os.sep)).as_posix()
    parts = rel.split("/") if rel else []
    if not parts:
        identity = COORDINATOR
    elif parts[0] == AGENTS_DIR and len(parts) >= 2 and parts[1] != "..":
        identity = parts[1]
    else:
        identity = UNASSIGNED
    return {"identity": identity, "start_dir": real,
            "common_dir": repo["common_dir"], "rel": rel}


# --------------------------------------------------------- start-dir readers


def _records(path, limit):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(limit):
                line = fh.readline(_LINE_CAP)
                if not line:
                    return
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def claude_start_dir(path, *, max_lines: int = 200) -> str | None:
    """The first JSONL record carrying a string ``cwd`` (later ones are
    ignored: ``cd`` never changes a session's identity)."""
    for rec in _records(path, max_lines):
        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def codex_start_dir(path) -> str | None:
    """The ``session_meta`` record's ``payload.cwd``."""
    for rec in _records(path, 8):
        if rec.get("type") != "session_meta":
            continue
        payload = rec.get("payload")
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return cwd if isinstance(cwd, str) and cwd else None
    return None


def export_start_dir(path) -> str | None:
    """The Omnigent export header's (first line, no ``type``) ``workspace``."""
    for rec in _records(path, 1):
        if "type" in rec:
            return None
        workspace = rec.get("workspace")
        return workspace if isinstance(workspace, str) and workspace else None
    return None


# ------------------------------------------------------------------ stamping


def stamp(row: dict, start_dir, source: str) -> dict:
    """Set ``identity``, ``identity_source`` and ``start_dir`` on ``row``."""
    found = identity_for_start_dir(start_dir)
    if found["identity"] is None:
        row["identity"] = None
        row["identity_source"] = UNAVAILABLE
        row["start_dir"] = None
    else:
        row["identity"] = found["identity"]
        row["identity_source"] = source
        row["start_dir"] = found["start_dir"]
    return row


def inherit(child: dict, parent: dict) -> dict:
    """Copy the parent's identity and start dir onto ``child``."""
    source = parent.get("identity_source") or UNAVAILABLE
    child["identity"] = parent.get("identity")
    child["start_dir"] = parent.get("start_dir")
    child["identity_source"] = (
        source if source.startswith(INHERITED_PREFIX)
        else INHERITED_PREFIX + source)
    return child

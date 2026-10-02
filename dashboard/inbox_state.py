"""Stable attention-inbox identities and read state (schema 2).

The dashboard loads this module by file path, so it intentionally has no
package-relative imports.  Persistence is kept separate from mailbox files:
the only write target is the configured dashboard home.

Notification ids are semantic and global: ``sha256("v2", loop_id, kind,
key)`` where ``loop_id`` is the canonical loop id (the same from the main
checkout and any linked worktree) and ``key`` names what the item is about
(a verdict word + iteration, a run id, ...), never a file digest or a
volatile timestamp.  The state document is::

    {"schema": 2,
     "items": {id: {"first_seen", "read", "loop_id", "common_dir"}},
     "legacy": {root: {"read": [old ids]}},
     "migrated_at": "..."}

A v1 document (keyed by workspace root, ids hashed with the root) is migrated
on first load: roots that no longer exist are dropped, the others are kept
under ``legacy`` so a first-seen v2 id inherits ``read`` from any old id that
maps to it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


_STATE_LOCK = RLock()
_ITERATION_HEADING_RE = re.compile(
    r"^#{1,6}\s+.*?\biter(?:ation)?\s*[:#-]?\s*(\d+)\b",
    re.IGNORECASE,
)
_STATE_ITERATION_RE = re.compile(
    r"^\s*(?:-\s+)?iteration\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)


def _loop_ids():
    """The sibling ``loop_ids`` module, loaded by file path (shared with
    serve.py through ``sys.modules`` when it already loaded it)."""
    name = "trio_dashboard_loop_ids"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("loop_ids.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load dashboard/loop_ids.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def state_path(home: Path | str) -> Path:
    """Return the configured state file path without consulting real HOME.

    ``TRIO_DASH_INBOX_STATE`` overrides it, so a second (dev) dashboard can
    run next to the service without sharing its read state."""
    override = os.environ.get("TRIO_DASH_INBOX_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(home) / ".local" / "share" / "trio-agent-loop" / "inbox-state.json"


def notification_id(loop_id: str, kind: str, key: str) -> str:
    """The semantic (v2) identity of one inbox item."""
    value = "\0".join(("v2", str(loop_id), str(kind), str(key)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def legacy_item_id(root: Path | str, loop_name: str, kind: str, anchor: str) -> str:
    """The v1 identity (root-partitioned), kept to map old read marks."""
    root_text = str(Path(root).resolve())
    value = "\0".join((root_text, str(loop_name), str(kind), str(anchor)))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_document(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_document(path: Path, document: dict) -> None:
    """Replace the state file atomically using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _exists(path) -> bool:
    try:
        return os.path.exists(path)
    except (OSError, ValueError):
        return False


def _normal_item(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    first_seen = value.get("first_seen")
    loop_id = value.get("loop_id")
    common = value.get("common_dir")
    return {
        "first_seen": first_seen if isinstance(first_seen, str) else _timestamp(),
        "read": value.get("read") is True,
        "loop_id": loop_id if isinstance(loop_id, str) else None,
        "common_dir": common if isinstance(common, str) else None,
    }


def _normal_legacy(value) -> dict:
    read = value.get("read") if isinstance(value, dict) else None
    return {"read": sorted({r for r in read if isinstance(r, str)})
            if isinstance(read, list) else []}


def _migrate_v1(raw: dict) -> dict:
    """v1 (root-keyed ``{read, first_seen}``) -> v2: roots gone from disk are
    garbage-collected, the rest keep their read lists under ``legacy``."""
    legacy = {}
    for root, record in raw.items():
        if not isinstance(root, str) or not isinstance(record, dict):
            continue
        if not _exists(root):
            continue
        entry = _normal_legacy(record)
        if entry["read"]:
            legacy[root] = entry
    return {"schema": 2, "items": {}, "legacy": legacy,
            "migrated_at": _timestamp()}


def _load(path: Path) -> tuple[dict, bool]:
    """(normalised v2 document, changed-vs-disk).  Migrates v1 and garbage
    collects items/legacy roots whose repository or root no longer exists."""
    raw = _read_document(path)
    if raw.get("schema") == 2:
        migrated = raw.get("migrated_at")
        document = {
            "schema": 2,
            "items": {},
            "legacy": {},
            "migrated_at": migrated if isinstance(migrated, str) else _timestamp(),
        }
        raw_items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
        alive: dict[str, bool] = {}
        for identity, value in raw_items.items():
            record = _normal_item(value)
            if not isinstance(identity, str) or record is None:
                continue
            common = record["common_dir"]
            if common is not None:
                if common not in alive:
                    alive[common] = _exists(common)
                if not alive[common]:
                    continue
            document["items"][identity] = record
        raw_legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}
        for root, value in raw_legacy.items():
            if isinstance(root, str) and _exists(root):
                document["legacy"][root] = _normal_legacy(value)
        return document, document != raw
    return _migrate_v1(raw), True


def _verdict_iteration(loop_dir: Path) -> str:
    """The iteration a verdict belongs to: the VERDICT.md iteration heading,
    else STATE.md's ``iteration:``, else ``none``."""
    try:
        verdict_bytes = (loop_dir / "VERDICT.md").read_bytes()
    except OSError:
        verdict_bytes = b""
    verdict_text = verdict_bytes.decode("utf-8", errors="replace")
    for line in verdict_text.splitlines():
        match = _ITERATION_HEADING_RE.match(line)
        if match:
            return match.group(1)
    iteration = ""
    try:
        state_text = (loop_dir / "STATE.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        state_text = ""
    for line in state_text.splitlines():
        match = _STATE_ITERATION_RE.match(line)
        if not match:
            continue
        value = match.group(1)
        number = re.search(r"\d+", value)
        iteration = number.group(0) if number else value.strip()
        break
    return iteration or "none"


def _verdict_anchor(loop_dir: Path) -> str:
    """The v1 anchor (iteration plus a digest of the whole VERDICT.md)."""
    try:
        verdict_bytes = (loop_dir / "VERDICT.md").read_bytes()
    except OSError:
        verdict_bytes = b""
    digest = hashlib.sha256(verdict_bytes).hexdigest()
    return f"{_verdict_iteration(loop_dir)}:{digest}"


def verdict_key(loop_dir: Path | str, kind: str) -> str:
    """Semantic key of a VERDICT-derived needs_human/blocked item: the
    verdict word and iteration, never the file's bytes."""
    word = "BLOCKED" if kind == "blocked" else "NEEDS_HUMAN"
    return f"verdict:{word}:{_verdict_iteration(Path(loop_dir))}"


def _key(item: dict, loop_dir: Path) -> str:
    explicit = item.get("_inbox_anchor")
    if explicit is not None:
        return str(explicit)
    kind = str(item.get("kind", ""))
    if kind in ("needs_human", "blocked"):
        return verdict_key(loop_dir, kind)
    return str(item.get("anchor", ""))


def _legacy_anchor(item: dict, loop_dir: Path, key: str) -> str:
    kind = str(item.get("kind", ""))
    if kind in ("needs_human", "blocked"):
        return _verdict_anchor(loop_dir)
    explicit = item.get("_inbox_legacy_anchor")
    return key if explicit is None else str(explicit)


def _legacy_read(legacy: dict, root: Path | str, loop_name: str, kind: str,
                 legacy_anchor: str, canonical: dict) -> bool:
    """Whether any v1 id mapping to this item was read (read wins)."""
    if not legacy:
        return False
    ids = _loop_ids()
    common = canonical.get("common_dir")
    names = {str(loop_name)}
    if canonical.get("rel"):
        names.add(canonical["rel"])
    try:
        root_real = str(Path(root).resolve())
    except OSError:
        root_real = str(root)
    for legacy_root, record in legacy.items():
        read = set(record.get("read") or ())
        if not read:
            continue
        if legacy_root != root_real:
            if common is None:
                continue
            identity = ids.repo_identity(legacy_root)
            if identity is None or identity["common_dir"] != common:
                continue
        for name in names:
            if legacy_item_id(legacy_root, name, kind, legacy_anchor) in read:
                return True
    return False


def decorate_items(
    items: list[dict],
    root: Path | str,
    loop_name: str,
    loop_dir: Path | str,
    home: Path | str,
    *,
    loop_id: str | None = None,
) -> list[dict]:
    """Attach id/loop_id/read/first_seen and persist newly seen identities.

    ``root`` no longer partitions storage: the id depends on the canonical
    loop id, so a mailbox read through any checkout is one item."""
    mailbox = Path(loop_dir)
    canonical = _loop_ids().canonical_loop(mailbox)
    loop_id = loop_id or canonical["loop_id"]
    path = state_path(home)
    with _STATE_LOCK:
        document, changed = _load(path)
        stored = document["items"]
        for item in items:
            kind = str(item.get("kind", ""))
            key = _key(item, mailbox)
            identity = notification_id(loop_id, kind, key)
            record = stored.get(identity)
            if record is None:
                record = {
                    "first_seen": _timestamp(),
                    "read": _legacy_read(
                        document["legacy"], root, loop_name, kind,
                        _legacy_anchor(item, mailbox, key), canonical),
                    "loop_id": loop_id,
                    "common_dir": canonical["common_dir"],
                }
                stored[identity] = record
                changed = True
            item.pop("_inbox_anchor", None)
            item.pop("_inbox_legacy_anchor", None)
            item["id"] = identity
            item["loop_id"] = loop_id
            item["read"] = record["read"]
            item["first_seen"] = record["first_seen"]
        if changed:
            _write_document(path, document)
    return items


def set_read(
    root: Path | str,
    ids: list[str],
    read: bool,
    home: Path | str,
) -> None:
    """Set read state for ids, including ids not currently on the board."""
    path = state_path(home)
    identity = _loop_ids().repo_identity(root)
    common = identity["common_dir"] if identity else None
    with _STATE_LOCK:
        document, changed = _load(path)
        stored = document["items"]
        for item_id in ids:
            record = stored.get(item_id)
            if record is None:
                if not read:
                    continue
                stored[item_id] = {"first_seen": _timestamp(), "read": True,
                                   "loop_id": None, "common_dir": common}
                changed = True
            elif record["read"] != bool(read):
                record["read"] = bool(read)
                changed = True
        if changed:
            _write_document(path, document)

"""Stable attention-inbox identities and per-workspace read state.

The dashboard loads this module by file path, so it intentionally has no
package-relative imports.  Persistence is kept separate from mailbox files:
the only write target is the configured dashboard home.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def state_path(home: Path | str) -> Path:
    """Return the configured state file path without consulting real HOME.

    ``TRIO_DASH_INBOX_STATE`` overrides it, so a second (dev) dashboard can
    run next to the service without sharing its read state."""
    override = os.environ.get("TRIO_DASH_INBOX_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(home) / ".local" / "share" / "trio-agent-loop" / "inbox-state.json"


def item_id(root: Path | str, loop_name: str, kind: str, anchor: str) -> str:
    """Build the stable identity specified by the inbox API contract."""
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


def _normal_record(value) -> dict:
    if not isinstance(value, dict):
        value = {}
    raw_read = value.get("read", [])
    raw_first_seen = value.get("first_seen", {})
    read_values = raw_read if isinstance(raw_read, list) else []
    first_seen_values = raw_first_seen if isinstance(raw_first_seen, dict) else {}
    read = {
        item for item in read_values if isinstance(item, str)
    }
    first_seen = {
        str(key): str(timestamp)
        for key, timestamp in first_seen_values.items()
        if isinstance(key, str) and isinstance(timestamp, str)
    }
    return {"read": read, "first_seen": first_seen}


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


def _verdict_anchor(loop_dir: Path) -> str:
    verdict_path = loop_dir / "VERDICT.md"
    try:
        verdict_bytes = verdict_path.read_bytes()
    except OSError:
        verdict_bytes = b""
    verdict_text = verdict_bytes.decode("utf-8", errors="replace")
    for line in verdict_text.splitlines():
        match = _ITERATION_HEADING_RE.match(line)
        if match:
            iteration = match.group(1)
            break
    else:
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
    iteration = iteration or "none"
    digest = hashlib.sha256(verdict_bytes).hexdigest()
    return f"{iteration}:{digest}"


def _anchor(item: dict, loop_dir: Path) -> str:
    kind = str(item.get("kind", ""))
    if kind in ("needs_human", "blocked"):
        return _verdict_anchor(loop_dir)
    explicit = item.get("_inbox_anchor")
    if explicit is not None:
        return str(explicit)
    return str(item.get("anchor", ""))


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decorate_items(
    items: list[dict],
    root: Path | str,
    loop_name: str,
    loop_dir: Path | str,
    home: Path | str,
) -> list[dict]:
    """Attach id/read/first_seen and persist newly observed item identities."""
    root_text = str(Path(root).resolve())
    mailbox = Path(loop_dir)
    path = state_path(home)
    with _STATE_LOCK:
        document = _read_document(path)
        raw_record = document.get(root_text)
        record = _normal_record(raw_record)
        for item in items:
            anchor = _anchor(item, mailbox)
            identity = item_id(root_text, loop_name, item.get("kind", ""), anchor)
            if identity not in record["first_seen"]:
                record["first_seen"][identity] = _timestamp()
            item.pop("_inbox_anchor", None)
            item["id"] = identity
            item["read"] = identity in record["read"]
            item["first_seen"] = record["first_seen"][identity]
        stored_record = {
            "read": sorted(record["read"]),
            "first_seen": dict(sorted(record["first_seen"].items())),
        }
        if raw_record != stored_record:
            document[root_text] = stored_record
            _write_document(path, document)
    return items


def set_read(
    root: Path | str,
    ids: list[str],
    read: bool,
    home: Path | str,
) -> None:
    """Set read state for ids, including ids not currently on the board."""
    root_text = str(Path(root).resolve())
    path = state_path(home)
    with _STATE_LOCK:
        document = _read_document(path)
        raw_record = document.get(root_text)
        record = _normal_record(raw_record)
        if read:
            record["read"].update(ids)
        else:
            record["read"].difference_update(ids)
        stored_record = {
            "read": sorted(record["read"]),
            "first_seen": dict(sorted(record["first_seen"].items())),
        }
        if raw_record != stored_record:
            document[root_text] = stored_record
            _write_document(path, document)

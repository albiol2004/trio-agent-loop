#!/usr/bin/env python3
"""Defensive token-usage parser for OpenCode's per-loop session storage.

Uploaded standalone into a task container at ``/opt/trio/usage_parser.py``
(stdlib only -- ``json``/``sqlite3``/``pathlib``, no package, no import of
anything under ``tbench/``) and run there with the bundled Python against
each ``run-*/xdg/data`` directory the driver's ``ocgen.generate`` created
for this mailbox. The exact OpenCode v2.0.20 on-disk schema was not
live-verified for this build (see README.md "9. Known gaps"), so this never
assumes a specific shape: it walks every JSON file under the root (and,
defensively, any ``.db``/``.sqlite``/``.sqlite3`` file) looking for any
object that looks like an assistant message with a ``tokens`` object, and
sums whatever it finds. An unreadable or unrecognised storage layout yields
all-zero totals and ``usage_source: "none"`` rather than raising.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

_TOKEN_KEYS = ("input", "output", "reasoning")
_CACHE_KEYS = ("read", "write")


#: per ``agent|provider/model#variant`` totals from the last parse (v2 sqlite only)
BY_AGENT: dict[str, dict[str, int]] = {}
#: sum of OpenCode's per-message ``cost`` field: a list-price API-equivalent
#: ESTIMATE only (runs use a subscription key) -- never report it as spend.
API_EQUIV: list[float] = [0.0]


def _empty_totals() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _add_tokens(totals: dict[str, int], tokens: dict[str, Any]) -> None:
    for key in _TOKEN_KEYS:
        value = tokens.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            totals[f"{key}_tokens"] += int(value)
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        for key in _CACHE_KEYS:
            value = cache.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[f"cache_{key}_tokens"] += int(value)


def _message_id(obj: dict[str, Any]) -> str | None:
    for key in ("id", "messageID", "messageId", "message_id"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _model_of(obj: dict[str, Any]) -> str:
    model = obj.get("model")
    if isinstance(model, dict):
        # OpenCode v2 session_message.data: {"model": {"id", "providerID", "variant"}}
        name = "/".join(str(model[k]) for k in ("providerID", "id") if model.get(k))
        if model.get("variant"):
            name += f"#{model['variant']}"
        if name:
            return name
    for key in ("modelID", "model_id", "model", "providerID"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return "unknown"


def _walk_json(
    node: Any,
    seen_ids: set[str],
    totals: dict[str, int],
    by_model: dict[str, dict[str, int]],
) -> None:
    if isinstance(node, dict):
        role = node.get("role")
        tokens = node.get("tokens")
        if role == "assistant" and isinstance(tokens, dict):
            msg_id = _message_id(node)
            if msg_id is None or msg_id not in seen_ids:
                if msg_id is not None:
                    seen_ids.add(msg_id)
                _add_tokens(totals, tokens)
                model_totals = by_model.setdefault(_model_of(node), _empty_totals())
                _add_tokens(model_totals, tokens)
        for value in node.values():
            _walk_json(value, seen_ids, totals, by_model)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, seen_ids, totals, by_model)


def _parse_json_files(
    root: Path,
    totals: dict[str, int],
    by_model: dict[str, dict[str, int]],
    seen_ids: set[str],
) -> int:
    count = 0
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            continue
        _walk_json(data, seen_ids, totals, by_model)
        count += 1
    return count


def _parse_sqlite_files(
    root: Path,
    totals: dict[str, int],
    by_model: dict[str, dict[str, int]],
    seen_ids: set[str],
) -> int:
    count = 0
    paths = list(root.rglob("*.db")) + list(root.rglob("*.sqlite")) + list(root.rglob("*.sqlite3"))
    for path in paths:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cur.fetchall()]
            except sqlite3.Error:
                tables = []
            for table in tables:
                try:
                    cur.execute(f'SELECT * FROM "{table}"')
                    col_names = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    obj = dict(zip(col_names, row))
                    if (table == "session_message" and obj.get("type") == "assistant"
                            and isinstance(obj.get("data"), str)):
                        # OpenCode v2: role lives in the row's ``type`` column,
                        # not inside ``data``; the row id is the message id.
                        try:
                            data = json.loads(obj["data"])
                        except (json.JSONDecodeError, RecursionError):
                            data = None
                        if isinstance(data, dict):
                            data.setdefault("role", "assistant")
                            data.setdefault("id", obj.get("id"))
                            fresh = data["id"] not in seen_ids
                            _walk_json(data, seen_ids, totals, by_model)
                            agent = data.get("agent") or "unknown"
                            if fresh and isinstance(data.get("cost"), (int, float)):
                                API_EQUIV[0] += float(data["cost"])
                            if fresh and isinstance(data.get("tokens"), dict):
                                _add_tokens(BY_AGENT.setdefault(f"{agent}|{_model_of(data)}", _empty_totals()), data["tokens"])
                        continue
                    _walk_json(obj, seen_ids, totals, by_model)
                    for value in obj.values():
                        if isinstance(value, str) and value.strip()[:1] in "{[":
                            try:
                                _walk_json(json.loads(value), seen_ids, totals, by_model)
                            except (json.JSONDecodeError, RecursionError):
                                continue
            count += 1
        finally:
            conn.close()
    return count


def parse_opencode_usage(roots: Path | str | Iterable[Path | str]) -> dict[str, Any]:
    """Walk one or more per-loop ``XDG_DATA_HOME`` roots defensively and sum
    token usage across all of them (a resumed run mints a fresh ``exec_id``
    and thus a new root, so usage must be aggregated, not taken from just
    the last one).

    Returns ``{"usage_source": "json"|"sqlite"|"json+sqlite"|"none",
    "totals": {...}, "by_model": {model: {...}}, "message_count": int}``.
    Never raises: an unreadable/unknown storage layout yields all-zero
    totals and ``usage_source: "none"``.
    """
    if isinstance(roots, (str, Path)):
        root_list = [Path(roots)]
    else:
        root_list = [Path(r) for r in roots]

    BY_AGENT.clear()
    API_EQUIV[0] = 0.0
    totals = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    seen_ids: set[str] = set()
    sources: list[str] = []

    for root in root_list:
        if not root.is_dir():
            continue
        if _parse_json_files(root, totals, by_model, seen_ids) and "json" not in sources:
            sources.append("json")
        if _parse_sqlite_files(root, totals, by_model, seen_ids) and "sqlite" not in sources:
            sources.append("sqlite")

    return {
        "usage_source": "+".join(sources) if sources else "none",
        "totals": totals,
        "by_model": by_model,
        "by_agent": dict(BY_AGENT),
        "api_equiv_usd_estimate": round(API_EQUIV[0], 4),
        "message_count": len(seen_ids),
    }


if __name__ == "__main__":
    print(json.dumps(parse_opencode_usage(sys.argv[1:])))

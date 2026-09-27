#!/usr/bin/env python3
"""trio-metrics.py — token/iteration metrics for trio-agent-loop mailboxes.

Usage: trio-metrics.py <project-or-loop-dir> [--json]

If <path> contains a LOG.md, it is treated as a single loop mailbox.
Otherwise the directory is treated as a project and all top-level
loop*/ directories are scanned (non-recursive).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# Contract version metrics/trio_loop.py (and trioctl, before loading it)
# relies on: 2 = read_queue / parse_slice_verdicts / parse_verdict_scope;
# 3 = plain fault `scope:` values, lenient read_queue `errors` and
# `malformed_slices`. A copy without this constant predates it.
METRICS_API = 3

A_LEAD_RE = re.compile(
    r"^\s*-\s*(?:\w+\s+)?(?:iter|iteration)\s+(\d+)\s*\|\s*lead\s*\|",
    re.IGNORECASE,
)
A_EVAL_RE = re.compile(
    r"^\s*-\s*(?:\w+\s+)?(?:iter|iteration)\s+(\d+)\s*\|\s*evaluator\s*\|\s*.*?"
    r"(?:VERDICT|verdict)[:.\s]+(\w+)"
    r"(?:\s*[—\-].*)?(?:\s*\|.*)?$",
    re.IGNORECASE,
)
B_LEAD_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*\|\s*Lead\s*\|\s*iteration\s+(\d+)",
    re.IGNORECASE,
)
B_EVAL_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*\|\s*Evaluator\s*\|\s*iteration\s+(\d+)",
    re.IGNORECASE,
)
B_VERDICT_RE = re.compile(r"^(?:VERDICT|verdict)[:.\s]*(\w+)", re.IGNORECASE)
B_WORD_RE = re.compile(r"^(\w+)")
C_EVAL_RE = re.compile(r"VERDICT:\s*(\w+)", re.IGNORECASE)
C_ITER_RE = re.compile(r"\biteration\s+(\d+)", re.IGNORECASE)
C_LEAD_RE = re.compile(r"\bLead\b", re.IGNORECASE)
C_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
STATE_RE = re.compile(
    r"^\s*(?:-\s+)?(iteration|max_iterations|status|mission|mission_fingerprint)\s*:\s*(.*)$",
    re.IGNORECASE,
)
VERDICT_RE = re.compile(r"^(?:#\s*)?VERDICT:\s*(\w+)", re.IGNORECASE)

# --- Timeline parsing -------------------------------------------------------
# The dashboard timeline reuses these (via parse_timeline) instead of
# duplicating LOG-line parsing in serve.py.

LOG_LINE_RE = re.compile(r"^-\s*iter(?:ation)?\s+(\d+)\s*\|\s*([^|]+?)\s*\|\s*(.*)$")
"""Format-A LOG.md line splitter: `- iter N | role | body`."""

_SLICE_PAREN_RE = re.compile(r"^-\s*slice\(([^)]+)\):\s*(.*\S)\s*$", re.IGNORECASE)
"""Builder slice-commit line: `- slice(<id>): <body>` (attributed to the builder)."""

_SLICE_PREFIX_RE = re.compile(
    r"^(?:-\s*)?([A-Za-z0-9_.\-]*iter(\d+)[A-Za-z0-9_.\-]*):\s+(.*\S)\s*$"
)
"""Builder slice-result line: `<slice-id containing iterN>: <body>`."""

_ITER_IN_TEXT_RE = re.compile(r"\biter(?:ation)?\s*(\d+)", re.IGNORECASE)
"""Fallback iteration extractor for slice lines whose id carries no iterN."""

_FIELD_SEGMENT_RE = re.compile(r"\s*\|\s*([A-Za-z_]+):\s*([^|]*?)\s*$")
"""One trailing ``| key: value`` segment (value runs up to the next ``|`` or EOL)."""

_VERDICT_WORD_RE = re.compile(r"\bVERDICT:\s*(\w+)", re.IGNORECASE)
_SCOPE_SUFFIX_RE = re.compile(r"\bscope=(design|local:[^\s|]+)", re.IGNORECASE)

KNOWN_VERDICTS = ("SHIP", "ITERATE", "BLOCKED", "NEEDS_HUMAN")
"""The verdict words the timeline contract exposes (parse_verdict_scope is
lenient and returns any ``VERDICT: <WORD>``; parse_timeline filters to these)."""

# --- Slice block parsing (PLAN.md ```yaml slices:) --------------------------
# Moved here from trio-shadow.py so the dashboard and the shadow checker share
# one parser for the documented format (MAILBOX-SCHEMA.md). Stdlib only.

SLICE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLICES_KEY_RE = re.compile(r"^\s*slices\s*:\s*(.*)$")
ENTRY_RE = re.compile(r"^\s*- id:\s*(.+?)\s*$")
KEY_RE = re.compile(r"^([a-z]+):\s*(.*)$")
FLOW_LIST_RE = re.compile(r"^\[(.*)\]$")
ITEM_RE = re.compile(r"^\s*- (.+)$")
SLICE_KEYS = (
    "id", "repo", "writes", "reads", "gate", "status", "iteration", "accepts",
)
STATUS_VALUES = ("planned", "in_progress", "complete")


class SliceParseError(Exception):
    """The PLAN.md slices block is missing or does not match the restricted shape."""


def _parse_b_verdict(text: str) -> str | None:
    for field in text.split("|"):
        field = field.strip()
        if not field:
            continue
        m = B_VERDICT_RE.match(field)
        if m:
            candidate = m.group(1).upper()
        else:
            m = B_WORD_RE.match(field)
            if not m:
                continue
            candidate = m.group(1).upper()
        if candidate in {"SHIP", "ITERATE", "BLOCKED", "NEEDS_HUMAN"}:
            return candidate
    return None


def parse_entry(line: str) -> dict | None:
    """Parse one LOG.md line into an entry dict."""
    # Format A
    m = A_EVAL_RE.match(line)
    if m:
        return {
            "iter": int(m.group(1)),
            "role": "evaluator",
            "verdict": m.group(2).upper(),
            "format": "A",
            "date": None,
        }
    m = A_LEAD_RE.match(line)
    if m:
        return {
            "iter": int(m.group(1)),
            "role": "lead",
            "verdict": None,
            "format": "A",
            "date": None,
        }

    # Format B
    m = B_EVAL_PREFIX_RE.match(line)
    if m:
        verdict = _parse_b_verdict(line[m.end():])
        if verdict:
            return {
                "iter": int(m.group(2)),
                "role": "evaluator",
                "verdict": verdict,
                "format": "B",
                "date": m.group(1),
            }
    m = B_LEAD_RE.match(line)
    if m:
        return {
            "iter": int(m.group(2)),
            "role": "lead",
            "verdict": None,
            "format": "B",
            "date": m.group(1),
        }

    # Format C / legacy free-form
    m = C_EVAL_RE.search(line)
    if m:
        it_m = C_ITER_RE.search(line)
        date_m = C_DATE_RE.match(line)
        return {
            "iter": int(it_m.group(1)) if it_m else None,
            "role": "evaluator",
            "verdict": m.group(1).upper(),
            "format": "C",
            "date": date_m.group(1) if date_m else None,
        }
    it_m = C_ITER_RE.search(line)
    if it_m and C_LEAD_RE.search(line):
        date_m = C_DATE_RE.match(line)
        return {
            "iter": int(it_m.group(1)),
            "role": "lead",
            "verdict": None,
            "format": "C",
            "date": date_m.group(1) if date_m else None,
        }

    return None


def parse_log(log_path: Path) -> list[dict]:
    """Return ordered list of parsed LOG.md entries."""
    entries: list[dict] = []
    if not log_path.is_file():
        return entries
    with log_path.open("r", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            e = parse_entry(raw)
            if e:
                e["line"] = lineno
                entries.append(e)
    return entries


def extract_trailing_fields(text: str) -> tuple[str, dict]:
    """Split trailing ``| key: value`` segments from a LOG line body.

    Returns ``(summary, fields)``: the body with any trailing
    ``| key: value`` segments removed, and a dict mapping each segment's
    key to its raw (stripped) string value. A segment's key is
    ``[A-Za-z_]+`` and its value runs up to the next ``|`` or EOL, so the
    known timing keys (``started_at``, ``ended_at``, ``duration_sec``) and
    any future keys parse identically. Segments are peeled one at a time
    from the end; prose containing ``|`` (e.g. ``|slip|~0.29``) is left
    untouched because it lacks the ``key:`` shape.
    """
    fields: dict[str, str] = {}
    tail = text.rstrip()
    while True:
        m = _FIELD_SEGMENT_RE.search(tail)
        if not m:
            break
        fields[m.group(1)] = m.group(2).strip()
        tail = tail[: m.start()].rstrip()
    return tail.strip(" |"), fields


def parse_verdict_scope(text: str) -> tuple[str | None, str | None]:
    """Find ``VERDICT: <WORD>`` and an optional ``scope=`` suffix.

    Returns ``(verdict, scope)``: the word after ``VERDICT:`` uppercased
    (None when absent), and ``design`` or ``local:<paths>`` when a
    ``scope=`` suffix is present (None otherwise). The verdict word is not
    restricted to the known set — callers such as ``parse_timeline`` may
    filter to ``KNOWN_VERDICTS``.
    """
    m = _VERDICT_WORD_RE.search(text)
    verdict = m.group(1).upper() if m else None
    sm = _SCOPE_SUFFIX_RE.search(text)
    scope = sm.group(1) if sm else None
    return verdict, scope


def _to_int(value) -> int | None:
    """Coerce a string to int; None when it is not a plain integer."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return None


def _entry_body(raw: str, entry: dict) -> tuple[str, dict]:
    """Extract the summary body and trailing fields for a parsed entry.

    The body is the part of the LOG line carrying the entry's prose: for
    format A the text after the role field, for format B the text after
    the ``iteration N`` marker, and for format C the whole line. Trailing
    ``| key: value`` segments are split off via ``extract_trailing_fields``.
    """
    line = raw.strip()
    if entry["format"] == "A":
        m = LOG_LINE_RE.match(line)
        if m:
            return extract_trailing_fields(m.group(3))
    elif entry["format"] == "B":
        m = B_EVAL_PREFIX_RE.match(line) or B_LEAD_RE.match(line)
        if m:
            return extract_trailing_fields(line[m.end():].lstrip("|").lstrip())
    return extract_trailing_fields(line)


def parse_timeline(log_path: Path) -> list[dict]:
    """Return every parsed LOG.md entry in file order, with timeline fields.

    Unlike ``parse_log`` (raw entry dicts), each result is shaped for the
    dashboard timeline: ``seq`` (0-based index in file order), ``iteration``,
    ``role``, ``summary`` (the line body with trailing ``| key: value``
    segments removed), ``verdict`` (one of ``KNOWN_VERDICTS``, else None),
    ``scope`` (``design`` / ``local:<paths>`` / None), and the trailing
    timing fields ``started_at``/``ended_at``/``duration_sec`` (int or
    None). Entries are never merged per (iteration, role) — the drawer
    groups and dedups frontend-side.
    """
    entries: list[dict] = []
    if not log_path.is_file():
        return entries
    known = set(KNOWN_VERDICTS)
    seq = 0
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                e = parse_entry(raw)
                if not e:
                    line = raw.strip()
                    m = _SLICE_PAREN_RE.match(line)
                    if m:
                        slice_id, body = m.group(1), m.group(2)
                        im = _ITER_IN_TEXT_RE.search(slice_id) or _ITER_IN_TEXT_RE.search(body)
                        iteration = int(im.group(1)) if im else None
                    else:
                        m = _SLICE_PREFIX_RE.match(line)
                        if not m:
                            continue
                        slice_id, body = m.group(1), m.group(3)
                        iteration = int(m.group(2))
                    summary, fields = extract_trailing_fields(body)
                    verdict, scope = parse_verdict_scope(body)
                    entries.append({
                        "seq": seq,
                        "iteration": iteration,
                        "role": "builder",
                        "summary": summary,
                        "verdict": verdict if verdict in known else None,
                        "scope": scope,
                        "slice": slice_id,
                        "started_at": fields.get("started_at"),
                        "ended_at": fields.get("ended_at"),
                        "duration_sec": _to_int(fields.get("duration_sec")),
                    })
                    seq += 1
                    continue
                summary, fields = _entry_body(raw, e)
                verdict, scope = parse_verdict_scope(raw)
                entries.append({
                    "seq": seq,
                    "iteration": e["iter"],
                    "role": e["role"],
                    "summary": summary,
                    "verdict": verdict if verdict in known else None,
                    "scope": scope,
                    "slice": None,
                    "started_at": fields.get("started_at"),
                    "ended_at": fields.get("ended_at"),
                    "duration_sec": _to_int(fields.get("duration_sec")),
                })
                seq += 1
    except OSError:
        return []
    return entries


def find_slices_block(plan_text: str) -> list[str]:
    """Return the lines of the first ```yaml fence with a top-level `slices:` key.

    Only yaml-marked fences (```yaml / ```yml) are candidates, matching the
    documented PLAN.md format. Raises SliceParseError when no such block
    exists.
    """
    in_fence = False
    yaml_fence = False
    buf: list[str] = []
    for raw in plan_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            if in_fence:
                if yaml_fence and _has_slices_key(buf):
                    return buf
                in_fence = False
                yaml_fence = False
                buf = []
            else:
                in_fence = True
                yaml_fence = stripped[3:].strip().lower() in ("yaml", "yml")
                buf = []
            continue
        if in_fence and yaml_fence:
            buf.append(raw)
    if in_fence and yaml_fence and _has_slices_key(buf):
        return buf
    raise SliceParseError(
        "no ```yaml slices block found in PLAN.md "
        "(expected a fenced yaml block whose top-level key is `slices:`)"
    )


def _has_slices_key(lines: list[str]) -> bool:
    return any(SLICES_KEY_RE.match(ln) for ln in lines)


def _unquote(item: str) -> str:
    item = item.strip()
    if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
        return item[1:-1]
    return item


def _split_flow_items(
    body: str, line: int, what: str = "bracketed list"
) -> list[str]:
    """Split a flow-list body on top-level commas, quote-aware.

    A comma inside a single- or double-quoted item is part of the item, not
    a separator -- accepts:/scope: hold prose and paths that routinely
    contain commas, unlike the plain writes:/reads: paths this originally
    served. A small stdlib character scan (tracking the active quote char)
    is enough; no need for csv/ast/shlex. An unterminated quote is a
    SliceParseError naming the line, not a silent truncation.
    """
    items: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in body:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch == ",":
            items.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if quote is not None:
        raise SliceParseError(f"line {line}: unterminated {quote!r} quote in {what}")
    items.append("".join(cur))
    return [part for part in items if part.strip()]


def _parse_flow_list(value: str, line: int) -> list[str]:
    m = FLOW_LIST_RE.match(value)
    if not m:
        raise SliceParseError(
            f"line {line}: expected a bracketed list like "
            f'[path.py, "api:Name"], got {value!r}'
        )
    return [_unquote(part) for part in _split_flow_items(m.group(1), line)]


def parse_slices(lines: list[str]) -> list[dict]:
    """Parse the restricted slices shape into a list of slice dicts.

    Accepts exactly: a top-level `slices:` key, then one `- id: <kebab-case>`
    entry per slice with keys id/repo/writes/reads/gate/status/iteration.
    `writes:`/`reads:` are flow-style `[a, "b"]` lists or block-style
    `- item` lists. `repo` defaults to `.`, `gate` to `false`, `status` to
    `in_progress`, `iteration` to None; anything else raises
    SliceParseError with the offending line number.
    """
    slices: list[dict] = []
    cur: dict | None = None
    list_key: str | None = None
    saw_slices = False

    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        m = SLICES_KEY_RE.match(stripped)
        if m:
            if cur is not None:
                raise SliceParseError(
                    f"line {i}: duplicate `slices:` key inside a slice entry"
                )
            if m.group(1).strip():
                raise SliceParseError(
                    f"line {i}: expected `slices:` with an empty value followed "
                    "by `- id:` entries"
                )
            saw_slices = True
            continue

        m = ENTRY_RE.match(stripped)
        if m:
            if cur is not None:
                slices.append(cur)
            slice_id = m.group(1).strip()
            if not SLICE_ID_RE.match(slice_id):
                raise SliceParseError(
                    f"line {i}: slice id {slice_id!r} is not kebab-case "
                    "(lowercase letters, digits, hyphens)"
                )
            cur = {
                "id": slice_id,
                "repo": ".",
                "writes": [],
                "reads": [],
                "gate": False,
                "status": "in_progress",
                "iteration": None,
                "accepts": [],
            }
            list_key = None
            continue

        if cur is None:
            raise SliceParseError(
                f"line {i}: unexpected content before any slice entry: {stripped!r}"
            )

        m = KEY_RE.match(stripped)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if key not in SLICE_KEYS:
                raise SliceParseError(
                    f"line {i}: unknown slice key {key!r} "
                    f"(expected one of {', '.join(SLICE_KEYS)})"
                )
            if key == "id":
                raise SliceParseError(
                    f"line {i}: `id` is set by the `- id:` entry; remove this line"
                )
            if key == "repo":
                if not value:
                    raise SliceParseError(f"line {i}: `repo:` needs a path value")
                cur["repo"] = value
                list_key = None
            elif key in ("writes", "reads", "accepts"):
                if value:
                    cur[key] = _parse_flow_list(value, i)
                    list_key = None
                else:
                    cur[key] = []
                    list_key = key
            elif key == "gate":
                if value not in ("true", "false"):
                    raise SliceParseError(
                        f"line {i}: `gate:` must be true or false, got {value!r}"
                    )
                cur["gate"] = value == "true"
                list_key = None
            elif key == "status":
                if value not in STATUS_VALUES:
                    raise SliceParseError(
                        f"line {i}: `status:` must be one of "
                        f"{', '.join(STATUS_VALUES)}, got {value!r}"
                    )
                cur["status"] = value
                list_key = None
            elif key == "iteration":
                try:
                    cur["iteration"] = int(value)
                except ValueError:
                    raise SliceParseError(
                        f"line {i}: `iteration:` must be an integer, got {value!r}"
                    ) from None
                list_key = None
            continue

        # Block-style list item under the current writes:/reads: key.
        if list_key is not None:
            m = ITEM_RE.match(stripped)
            if not m:
                raise SliceParseError(
                    f"line {i}: expected a `- item` list entry under "
                    f"`{list_key}:`, got {stripped!r}"
                )
            cur[list_key].append(_unquote(m.group(1).strip()))
            continue

        raise SliceParseError(
            f"line {i}: unexpected content in slice {cur['id']!r}: {stripped!r}"
        )

    if cur is not None:
        slices.append(cur)
    if not saw_slices:
        raise SliceParseError(
            "the yaml block has no top-level `slices:` key "
            "(expected `slices:` followed by `- id:` entries)"
        )
    return slices


def parse_slices_block(plan_text: str) -> list[dict] | None:
    """Parse the PLAN.md ``slices:`` block, or return None when absent/malformed.

    Lenient wrapper around ``find_slices_block`` + ``parse_slices`` for
    consumers that must degrade gracefully (the dashboard): a missing or
    unparseable block yields None instead of raising ``SliceParseError``.
    """
    try:
        return parse_slices(find_slices_block(plan_text))
    except SliceParseError:
        return None


# --- QUEUE.md parsing (v1 open-loop extension) -------------------------------
# MAILBOX-SCHEMA.md "v1 open-loop extension (optional)": QUEUE.md carries two
# independent fenced ```yaml blocks, `retired:` (Lead-appended) and `faults:`
# (Evaluator-appended). Mirrors the slices-block trio in style (line-based
# state machine, 1-based line numbers, _parse_flow_list/_unquote reuse) but
# stays permissive: enum/shape validation (status values, `f<N>` ids,
# retired.slice existing in PLAN.md) lives in trio-check.py, not here.

RETIRED_KEY_RE = re.compile(r"^\s*retired\s*:\s*(.*)$")
FAULTS_KEY_RE = re.compile(r"^\s*faults\s*:\s*(.*)$")
RETIRED_ENTRY_RE = re.compile(r"^\s*- slice:\s*(.*)$")
FAULT_ENTRY_RE = re.compile(r"^\s*- id:\s*(.*)$")
QUEUE_KEY_RE = re.compile(r"^([a-z_]+):\s*(.*)$")
RETIRED_KEYS = ("slice", "sha", "at")
FAULT_KEYS = ("id", "slice", "observed_at", "scope", "reason", "status")
# The only free-text queue field: a deeper-indented continuation line after
# it is YAML plain-scalar folding. After any other key it is an error (a
# stray note must never silently change `status`/`sha`/`slice`/...).
QUEUE_FOLD_KEYS = ("reason",)
# Fault statuses that close a fault; every other value gates as live.
QUEUE_CLOSED_STATUSES = ("done", "stale")


class QueueParseError(ValueError):
    """QUEUE.md is present but does not match the restricted shape."""


def find_queue_block(
    queue_text: str, key: str, errors: list[str] | None = None
) -> list[str] | None:
    """Return the body lines of the fenced yaml block whose only top-level
    key is `key` ("retired" or "faults"), or None when absent.

    Unlike find_slices_block, absence is not an error: a missing block means
    an empty queue for that key (MAILBOX-SCHEMA.md: "Either block may be
    absent ... absent always means an empty queue, never an error").

    Only the FIRST fence opened with exactly ```` ```yaml ```` (or
    ```` ```yml ````) that carries a `key:` line is returned. Any OTHER
    fence carrying a `key:` line is a violation (r11g P1), since its
    entries would otherwise be invisible to the gate: a second ```yaml
    block with the key, or the key inside an untagged fence, a ``~~~``
    fence, or a fence with a different/extra info string (```` ```yaml
    title ````). `errors` None (strict): the first such violation raises
    QueueParseError. `errors` a list (lenient): each is appended to it and
    the first ```yaml block (if any) is still returned.
    """
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*)$")
    found: list[str] | None = None
    fence: str | None = None  # "```" or "~~~" while inside a fence
    fence_line = 0
    info = ""
    buf: list[str] = []

    def _fail(msg: str) -> None:
        if errors is None:
            raise QueueParseError(msg)
        errors.append(msg)

    def _close() -> None:
        nonlocal found
        if not any(key_re.match(ln) for ln in buf):
            return
        yaml_fence = fence == "```" and info.lower() in ("yaml", "yml")
        opener = f"{fence}{info}"
        if not yaml_fence:
            _fail(
                f"line {fence_line}: a `{key}:` block in a "
                f"{_junk_prefix(opener)!r} fence is ignored; the queue "
                f"only reads a fence opened with exactly ```yaml -- move "
                f"its entries into the ```yaml `{key}:` block"
            )
        elif found is not None:
            _fail(
                f"line {fence_line}: a second fenced ```yaml `{key}:` block "
                f"is ignored; merge its entries into the first `{key}:` "
                "block"
            )
        else:
            found = list(buf)

    for lineno, raw in enumerate(queue_text.splitlines(), 1):
        stripped = raw.strip()
        opener = (
            "```" if stripped.startswith("```")
            else "~~~" if stripped.startswith("~~~")
            else None
        )
        if fence is None:
            if opener is not None:
                fence, fence_line, buf = opener, lineno, []
                info = stripped.lstrip(opener[0]).strip()
            continue
        if opener == fence:
            _close()
            fence, info, buf = None, "", []
            continue
        buf.append(raw)
    if fence is not None:
        _close()
    return found


def _normalize_scope_items(items: list[str]) -> list[str]:
    """Normalize fault scope items to the internal shape: a list of path
    strings, or exactly ``["design"]`` for a design-scoped fault.

    A leading ``local:`` (the VERDICT.md ``scope=local:<paths>`` spelling
    that evaluators copy into QUEUE.md) is stripped from any item, so
    ``[local:a.py, b.py]`` and ``local:a.py,b.py`` both become
    ``["a.py", "b.py"]``. Blank items are dropped.
    """
    out: list[str] = []
    for item in items:
        item = _unquote(item)
        if item.lower().startswith("local:"):
            item = item[len("local:"):].strip()
        if item:
            out.append(item)
    return out


def _parse_scope_value(value: str, line: int) -> list[str]:
    """Parse a non-empty inline fault ``scope:`` value.

    Accepted shapes (MAILBOX-SCHEMA.md ``faults:``):
    - a flow list: ``[a.py, "b c.py"]`` (items may carry ``local:``);
    - a plain ``local:<comma-separated paths>`` value;
    - a plain ``design``;
    - plain bare comma-separated paths.
    All normalize to a list of path strings (``["design"]`` for design).
    A plain value that normalizes to nothing (``local:``) is an error.
    """
    if FLOW_LIST_RE.match(value):
        try:
            return _normalize_scope_items(_parse_flow_list(value, line))
        except SliceParseError as exc:
            raise QueueParseError(str(exc)) from exc
    try:
        parts = _split_flow_items(value, line, what="plain `scope:` value")
    except SliceParseError as exc:
        raise QueueParseError(str(exc)) from exc
    items = _normalize_scope_items(parts)
    if not items:
        raise QueueParseError(
            f"line {line}: `scope:` value {value!r} names no paths "
            "(expected `local:<paths>`, `design`, or a bracket list)"
        )
    return items


def _junk_prefix(text: str, limit: int = 60) -> str:
    """`text` cut to `limit` chars (with an ellipsis) for an error message."""
    return text if len(text) <= limit else text[:limit] + "…"


def _parse_queue_entries(
    lines: list[str],
    *,
    top_key_re: re.Pattern,
    top_key_name: str,
    entry_re: re.Pattern,
    entry_field: str,
    required: tuple[str, ...],
    list_fields: tuple[str, ...],
    scalar_list_fields: tuple[str, ...] = (),
    errors: list[str] | None = None,
    malformed: set[str] | None = None,
) -> list[dict]:
    """Shared state machine for `retired:`/`faults:` entry lists.

    Each entry is a `- <entry_field>: <value>` line followed by indented
    `key: value` lines; `list_fields` (e.g. `scope`) accept a flow list or a
    block `- item` list, exactly like `writes:`/`reads:` in parse_slices.
    `scalar_list_fields` additionally accept a plain inline value (see
    `_parse_scope_value`). A non-key line indented deeper than a
    `reason:` line above it (QUEUE_FOLD_KEYS, the only free-text field) is
    YAML plain-scalar folding and is appended to it (joined by one space),
    not an error. The same continuation after ANY other scalar key
    (`status`, `sha`, `slice`, `observed_at`, `at`, ...) is an error naming
    the line, a text prefix and the key it followed; the line is skipped
    and the entry is kept with that key UNCHANGED (parsing of the entry
    continues). The `reason:` fold is checked BEFORE any structural match,
    so a deeper line shaped like a key or a garbled header still folds --
    but a well-formed `- <entry_field>:` header or the top-level key never
    does, at any indent (it starts the next entry / is reported). A
    key repeated inside one entry is an error too: the FIRST value is
    kept, the repeat (and its continuation/item lines) skipped -- except a
    duplicated `status:`, where a live value beats done/stale (fail closed).
    A garbled entry header (`-slice:`, `* slice:`, `- Slice:`, `slice=`
    ...; case-insensitive key, `-`/`*` bullet) is recognized and its id
    poisoned; a header too mangled to name the key (`- slcie:`) is only
    reported as unexpected content.

    `errors` None (strict): the first violation raises QueueParseError.
    `errors` a list (lenient): each violation is appended to it and parsing
    resumes at the next `- <entry_field>:` line. An unexpected line inside
    an entry that already has every required key is reported (line number
    and text prefix) and skipped -- the complete entry is KEPT; an entry
    still missing a required key is dropped. One malformed entry never
    discards the valid entries around it.

    `malformed` (lenient only): receives the `<entry_field>` value of every
    dropped entry, plus the id named by a stray `<entry_field>:` line or a
    garbled `- <entry_field>` header, so callers can refuse to trust that
    id (see parse_queue_block `malformed_slices`).
    """
    entries: list[dict] = []
    cur: dict | None = None
    cur_line = 0
    list_key: str | None = None
    # (key, indent, mode) of the last scalar `key:` line of `cur`, for the
    # deeper-indented non-key lines right after it. mode "fold": append to
    # `reason`; "error": report + skip (non-free-text key); "dup": skip
    # silently (continuation of an already-reported duplicate key).
    fold: tuple[str, int, str] | None = None
    # block-list items after a duplicate list key are skipped, not appended
    list_dup = False
    saw_key = False
    skipping = False  # lenient: after a dropped/closed entry, until a header
    garbled_header_re = re.compile(
        rf"^(?:[-*]\s*{re.escape(entry_field)}\b\s*[:=]?|"
        rf"{re.escape(entry_field)}\s*[:=])\s*[\"']?([^\s\"']+)",
        re.IGNORECASE,
    )

    def _fail(msg: str) -> None:
        if errors is None:
            raise QueueParseError(msg)
        errors.append(msg)

    def _poison(ident: str | None) -> None:
        ident = (ident or "").strip().strip("\"'")
        if malformed is not None and ident:
            malformed.add(ident)

    def _missing(entry: dict) -> list[str]:
        return [k for k in required if not str(entry.get(k, "")).strip()]

    def _finish(entry: dict, line: int) -> None:
        missing = _missing(entry)
        if missing:
            _fail(
                f"line {line}: {top_key_name} entry missing required "
                f"key(s): {', '.join(missing)}"
            )
            _poison(entry.get(entry_field))
            return
        entries.append(entry)

    def _bad(msg: str, also_poison: str | None = None) -> None:
        """Report `msg`, then close the current entry: keep it when it is
        already complete, drop it (and poison its id) otherwise. Lines up
        to the next `- <entry_field>:` header are skipped either way."""
        nonlocal cur, skipping, list_key, fold
        if cur is not None and not _missing(cur):
            msg += (
                f" (skipped; the complete `- {entry_field}: "
                f"{cur.get(entry_field)}` entry at line {cur_line} is kept)"
            )
            _fail(msg)
            entries.append(cur)
        else:
            _fail(msg)
            if cur is not None:
                _poison(cur.get(entry_field))
        _poison(also_poison)
        cur, skipping, list_key, fold = None, True, None, None

    def _garbled_id(stripped: str) -> str | None:
        m = garbled_header_re.match(stripped)
        return m.group(1) if m else None

    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        expanded = raw.expandtabs()
        indent = len(expanded) - len(expanded.lstrip())

        # `reason:` folding runs FIRST (r11f R1/R2): while the previous
        # scalar is `reason:` (QUEUE_FOLD_KEYS), ANY line indented deeper
        # than its key column is a wrapped continuation -- even one shaped
        # like a key (`status: done`) or a garbled header (`ID=5`,
        # `- id ...`). Only a line at (or left of) the key column is parsed
        # structurally, so a garbled header at header indent is still
        # caught below. A well-formed entry header (`entry_re`) or the
        # top-level key (`top_key_re`) is NEVER folded, whatever its indent
        # (r11g NEW-S1): a `reason:`-last entry followed by a deeper or
        # tab-indented `- id:` must still start the next entry.
        if (
            cur is not None
            and not skipping
            and fold is not None
            and fold[0] in QUEUE_FOLD_KEYS
            and fold[2] in ("fold", "dup")
            and indent > fold[1]
            and not entry_re.match(stripped)
            and not top_key_re.match(stripped)
        ):
            if fold[2] == "fold":
                cur[fold[0]] = f"{cur[fold[0]]} {stripped}".strip()
            continue  # "dup": continuation of a reported duplicate, skipped

        m = top_key_re.match(stripped)
        if m:
            if cur is not None:
                _bad(f"line {i}: duplicate `{top_key_name}:` key inside an entry")
                continue
            if m.group(1).strip():
                _fail(
                    f"line {i}: expected `{top_key_name}:` with an empty value "
                    f"followed by `- {entry_field}:` entries"
                )
            saw_key = True
            skipping = False
            continue

        m = entry_re.match(stripped)
        if m:
            if cur is not None:
                _finish(cur, cur_line)
            cur = {entry_field: m.group(1).strip()}
            cur_line = i
            list_key = None
            list_dup = False
            # The header's own value is a non-free-text scalar too: a line
            # indented deeper than its key column (after `- `) is a
            # continuation of it -> reported, never folded.
            after_dash = stripped[1:]
            key_col = indent + 1 + len(after_dash) - len(after_dash.lstrip())
            fold = (entry_field, key_col, "error")
            skipping = False
            continue

        if skipping:
            continue

        if cur is None:
            _bad(
                f"line {i}: unexpected content before any `- {entry_field}:` "
                f"entry: {_junk_prefix(stripped)!r}",
                also_poison=_garbled_id(stripped),
            )
            continue

        m = QUEUE_KEY_RE.match(stripped)
        if m:
            key, value = m.group(1), m.group(2).strip()
            fold = None
            list_dup = False
            if key != entry_field and key in cur:
                # Fail closed on a duplicated `status:`: a live value
                # (anything but done/stale, non-empty) beats a closed one,
                # whatever the order. Still reported as an error.
                if (
                    key == "status"
                    and str(cur.get(key, "")) in QUEUE_CLOSED_STATUSES
                    and value
                    and value not in QUEUE_CLOSED_STATUSES
                ):
                    _fail(
                        f"line {i}: duplicate `{key}:` key in the "
                        f"`- {entry_field}: {cur.get(entry_field)}` entry at "
                        f"line {cur_line}; the live value "
                        f"{_junk_prefix(value)!r} is kept, "
                        f"{_junk_prefix(str(cur[key]))!r} is dropped"
                    )
                    cur[key] = value
                    list_key = None
                    fold = (key, indent, "dup")
                    continue
                _fail(
                    f"line {i}: duplicate `{key}:` key in the "
                    f"`- {entry_field}: {cur.get(entry_field)}` entry at line "
                    f"{cur_line}; the first value is kept, "
                    f"{_junk_prefix(value)!r} is skipped"
                )
                if key in list_fields and not value:
                    list_key, list_dup = key, True
                else:
                    list_key = None
                    fold = (key, indent, "dup")
                continue
            if key == entry_field:
                _bad(
                    f"line {i}: stray `{entry_field}: {_junk_prefix(value)}` "
                    f"line: `{entry_field}` is set by the `- {entry_field}:` "
                    "entry header",
                    also_poison=value,
                )
                continue
            if key in list_fields:
                if value:
                    try:
                        if key in scalar_list_fields:
                            cur[key] = _parse_scope_value(value, i)
                        else:
                            cur[key] = _parse_flow_list(value, i)
                    except SliceParseError as exc:
                        # _parse_flow_list is shared with parse_slices and
                        # always raises SliceParseError; translate to this
                        # module's own error type so callers only ever see
                        # QueueParseError out of parse_retired/parse_faults.
                        _bad(str(exc))
                        continue
                    except QueueParseError as exc:
                        _bad(str(exc))
                        continue
                    list_key = None
                else:
                    cur[key] = []
                    list_key = key
            else:
                cur[key] = value
                list_key = None
                fold = (key, indent, "fold" if key in QUEUE_FOLD_KEYS else "error")
            continue

        if list_key is not None:
            m = ITEM_RE.match(stripped)
            if not m:
                _bad(
                    f"line {i}: expected a `- item` list entry under "
                    f"`{list_key}:`, got {_junk_prefix(stripped)!r}"
                )
                continue
            if list_dup:
                continue
            item = _unquote(m.group(1).strip())
            if list_key in scalar_list_fields:
                cur[list_key].extend(_normalize_scope_items([item]))
            else:
                cur[list_key].append(item)
            continue

        garbled = _garbled_id(stripped)
        if fold is not None and indent > fold[1] and garbled is None:
            key, _, mode = fold
            if mode == "fold":
                # YAML plain-scalar folding: a wrapped `reason:` continuation.
                cur[key] = f"{cur[key]} {stripped}".strip()
            elif mode == "error":
                # Never fold into a non-free-text key: `status: open` plus an
                # indented note must stay `open` (r11 N1). Report, skip the
                # line, keep the entry and the key's value unchanged.
                _fail(
                    f"line {i}: unexpected continuation line after `{key}:` "
                    f"(only `reason:` may continue on the next line): "
                    f"{_junk_prefix(stripped)!r} (skipped; `{key}:` kept as "
                    f"{_junk_prefix(str(cur.get(key, '')))!r})"
                )
            continue

        _bad(
            f"line {i}: unexpected content: {_junk_prefix(stripped)!r}",
            also_poison=garbled,
        )

    if cur is not None:
        _finish(cur, cur_line)
    if not saw_key:
        _fail(
            f"the yaml block has no top-level `{top_key_name}:` key "
            f"(expected `{top_key_name}:` followed by `- {entry_field}:` entries)"
        )
    return entries


def parse_retired(
    lines: list[str],
    errors: list[str] | None = None,
    malformed: set[str] | None = None,
) -> list[dict]:
    """Parse a `retired:` block into `{"slice", "sha", "at"}` dicts.

    Strict (raises QueueParseError) unless an `errors` list is passed, in
    which case malformed entries are dropped and reported there, and the
    slice id of every malformed entry is added to `malformed` (when given)
    -- such a slice must not be gated as retired (MAILBOX-SCHEMA.md)."""
    return _parse_queue_entries(
        lines,
        top_key_re=RETIRED_KEY_RE,
        top_key_name="retired",
        entry_re=RETIRED_ENTRY_RE,
        entry_field="slice",
        required=RETIRED_KEYS,
        list_fields=(),
        errors=errors,
        malformed=malformed,
    )


def parse_faults(lines: list[str], errors: list[str] | None = None) -> list[dict]:
    """Parse a `faults:` block into `{"id", "slice", "observed_at", "scope",
    "reason", "status"}` dicts.

    `scope:` accepts a flow list, a block list, or a plain value
    (`local:<comma-separated paths>`, `design`, or bare comma-separated
    paths); every shape normalizes to a list of path strings, with
    `["design"]` for a design-scoped fault and any `local:` prefix
    stripped. Strict (raises QueueParseError) unless an `errors` list is
    passed, in which case malformed entries are dropped and reported there
    while valid entries survive."""
    return _parse_queue_entries(
        lines,
        top_key_re=FAULTS_KEY_RE,
        top_key_name="faults",
        entry_re=FAULT_ENTRY_RE,
        entry_field="id",
        required=FAULT_KEYS,
        list_fields=("scope",),
        scalar_list_fields=("scope",),
        errors=errors,
    )


def _empty_queue() -> dict:
    return {"retired": [], "faults": [], "errors": [], "malformed_slices": []}


def parse_queue_block(queue_text: str) -> dict:
    """Lenient: always returns {"retired": [...], "faults": [...],
    "errors": [...], "malformed_slices": [...]}.

    `malformed_slices` (sorted slice ids) names every slice with a
    malformed `retired:` entry anywhere in the block; the open-loop driver
    does not gate such a slice as retired, even when an older valid entry
    for it survives, until the entry is repaired.

    Per-block wrapper around find_queue_block + parse_retired/parse_faults
    in their lenient mode: a malformed entry is dropped and described in
    `errors` (prefixed with its block name) while every valid entry of the
    same block survives -- a parse problem is never silently turned into an
    empty queue. Never returns None: "no queue" and "empty queue" are
    indistinguishable to callers (both have empty `errors`).
    """
    result = _empty_queue()
    for key, parser in (("retired", parse_retired), ("faults", parse_faults)):
        block_errors: list[str] = []
        lines = find_queue_block(queue_text, key, errors=block_errors)
        if lines is None:
            result["errors"].extend(
                f"`{key}:` block: {e}" for e in block_errors
            )
            continue
        if key == "retired":
            malformed: set[str] = set()
            result[key] = parse_retired(
                lines, errors=block_errors, malformed=malformed
            )
            result["malformed_slices"] = sorted(malformed)
        else:
            result[key] = parser(lines, errors=block_errors)
        result["errors"].extend(f"`{key}:` block: {e}" for e in block_errors)
    return result


def read_queue(loop_dir: Path) -> dict:
    """Read and parse loop_dir/QUEUE.md; never raises.

    Missing QUEUE.md, an unreadable file, or an empty file all yield
    {"retired": [], "faults": [], "errors": [], "malformed_slices": []} —
    indistinguishable from a
    QUEUE.md with two empty blocks (MAILBOX-SCHEMA.md: "absent always means
    an empty queue, never an error"). Parse problems in a present QUEUE.md
    land in `errors` (see parse_queue_block); they never raise.
    """
    queue_path = loop_dir / "QUEUE.md"
    if not queue_path.is_file():
        return _empty_queue()
    try:
        text = queue_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _empty_queue()
    if not text.strip():
        return _empty_queue()
    return parse_queue_block(text)


def _state_value(key: str, raw: str) -> str:
    val = raw.strip()
    if key in ("iteration", "max_iterations"):
        m = re.search(r"\d+", val)
        return m.group(0) if m else val
    if key == "status":
        return val.split()[0] if val.split() else val
    return val


def parse_state(state_path: Path) -> dict:
    """Return top-level key/value map from STATE.md."""
    state: dict[str, str] = {}
    if not state_path.is_file():
        return state
    with state_path.open("r", errors="replace") as fh:
        for raw in fh:
            m = STATE_RE.match(raw)
            if m:
                key = m.group(1).lower()
                state[key] = _state_value(key, m.group(2))
    return state


OVERALL_VERDICT_RE = re.compile(r"[Oo]verall:\s*(\w+)")
"""Legacy prose verdicts, e.g. ``**Overall: SHIP**`` in pre-v1 mailboxes."""


def parse_verdict(verdict_path: Path) -> str | None:
    """Return the verdict on the first non-empty line of VERDICT.md.

    Strict v1 contract first (``VERDICT: <word>`` on the first non-empty
    line). When that fails, lenient fallbacks for legacy mailboxes: any
    ``VERDICT: <word>`` line, then an ``Overall: <word>`` marker. Lenient
    hits are restricted to known verdict words. trio-check.py stays strict
    -- this fallback is for dashboard/metrics display only.
    """
    if not verdict_path.is_file():
        return None
    try:
        text = verdict_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = VERDICT_RE.match(line)
        if m:
            return m.group(1).upper()
        break
    known = set(KNOWN_VERDICTS)
    for pattern in (VERDICT_RE, OVERALL_VERDICT_RE):
        for raw in text.splitlines():
            m = pattern.search(raw)
            if m and m.group(1).upper() in known:
                return m.group(1).upper()
    return None


SLICE_VERDICT_RE = re.compile(
    r"^##\s+slice\s+(\S+)\s+@([0-9a-fA-F]{4,40})\s+(?:—|--|-)\s+(SHIP|ITERATE)\s*$"
)
"""MAILBOX-SCHEMA.md "Per-slice verdicts in VERDICT.md": a per-slice
evaluation section heading, e.g. ``## slice foo @0f3c...40hex — SHIP``. The
schema mandates the em dash ``—``; this also accepts ``-``/``--`` and a
short sha prefix, matching the leniency GOAL.md asks for in derive_slices."""


def parse_slice_verdicts(verdict_text: str) -> list[dict]:
    """Parse every ``## slice <id> @<sha> — SHIP|ITERATE`` heading in
    VERDICT.md into ``{"slice", "sha", "verdict"}`` dicts, in file order.

    Public and reused by dashboard/serve.py (see PLAN.md's api-slices-inbox
    slice) so the regex is defined exactly once. Does not attempt to
    resolve short shas against a slice's latest retired sha -- that
    matching (a short heading sha that the latest full sha startswith) is
    derive_slices' job.
    """
    out: list[dict] = []
    if not verdict_text:
        return out
    for raw in verdict_text.splitlines():
        m = SLICE_VERDICT_RE.match(raw.strip())
        if m:
            out.append({
                "slice": m.group(1),
                "sha": m.group(2),
                "verdict": m.group(3),
            })
    return out


def verdict_letter(verdict: str) -> str:
    return {
        "SHIP": "S",
        "ITERATE": "I",
        "BLOCKED": "B",
        "NEEDS_HUMAN": "H",
    }.get(verdict, "?")


def dominant_format(entries: list[dict]) -> str:
    if not entries:
        return "unknown"
    counts = {}
    for e in entries:
        counts[e["format"]] = counts.get(e["format"], 0) + 1
    top = max(counts.values())
    leaders = [f for f, c in counts.items() if c == top]
    return "mixed" if len(leaders) > 1 else leaders[0]


def segment_entries(entries: list[dict]) -> list[list[dict]]:
    """Split entries at SHIP evaluator boundaries."""
    segments: list[list[dict]] = []
    current: list[dict] = []
    for e in entries:
        current.append(e)
        if e["role"] == "evaluator" and e["verdict"] == "SHIP":
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    if not segments:
        segments.append([])
    return segments


def summarize_segment(seg: list[dict]) -> dict:
    if not seg:
        return {
            "iteration_count": 0,
            "verdict_sequence": "",
            "lead_count": 0,
            "evaluator_count": 0,
            "format": "unknown",
            "date_span": None,
            "final_log_verdict": None,
            "unparsed": True,
        }

    lead_count = sum(1 for e in seg if e["role"] == "lead")
    evals = [e for e in seg if e["role"] == "evaluator"]
    eval_count = len(evals)

    sequence = "".join(verdict_letter(e["verdict"]) for e in evals)
    final_log = evals[-1]["verdict"] if evals else None

    iters = [e["iter"] for e in seg if e["iter"] is not None]
    if iters:
        iteration_count = max(iters)
    elif evals:
        # Fall back to evaluator-entry order when no explicit numbers are present.
        iteration_count = len(evals)
    else:
        iteration_count = lead_count if lead_count else 0

    dates = [e["date"] for e in seg if e["date"]]
    date_span = f"{min(dates)} to {max(dates)}" if dates else None

    return {
        "iteration_count": iteration_count,
        "verdict_sequence": sequence,
        "lead_count": lead_count,
        "evaluator_count": eval_count,
        "format": dominant_format(seg),
        "date_span": date_span,
        "final_log_verdict": final_log,
        "unparsed": False,
    }


def analyze_loop(loop_dir: Path, root: Path | None = None) -> dict:
    name = loop_name(root, loop_dir) if root is not None else loop_dir.name
    log_path = loop_dir / "LOG.md"
    state_path = loop_dir / "STATE.md"
    verdict_path = loop_dir / "VERDICT.md"

    entries = parse_log(log_path)
    segments = segment_entries(entries)
    segment_summaries = [summarize_segment(s) for s in segments]

    state = parse_state(state_path)
    final_verdict = parse_verdict(verdict_path)

    parsed = any(not s["unparsed"] for s in segment_summaries)

    return {
        "name": name,
        "final_verdict": final_verdict,
        "state_status": state.get("status"),
        "state_iteration": state.get("iteration"),
        "state_max_iterations": state.get("max_iterations"),
        "segments": segment_summaries,
        "parsed": parsed,
        "path": str(loop_dir),
    }


MAILBOX_MARKERS = ("LOG.md", "GOAL.md", "STATE.md", "VERDICT.md", "PLAN.md")
SKIP_DIR_NAMES = {".git", "briefs", "__pycache__"}


def is_mailbox(path: Path) -> bool:
    """A directory is a loop mailbox if it holds any of the mailbox files."""
    return path.is_dir() and any((path / m).is_file() for m in MAILBOX_MARKERS)


def _skip_dir(path: Path) -> bool:
    n = path.name
    return (
        n.startswith(".") or n in SKIP_DIR_NAMES or n.startswith("evidence")
    )


def loop_name(root: Path, loop_dir: Path) -> str:
    """Board name for a mailbox: relative POSIX path under root (or dir name)."""
    try:
        rel = loop_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return loop_dir.name
    return rel.as_posix() if rel.parts else loop_dir.name


def discover_loops(root: Path) -> list[Path]:
    """Return loop mailbox dirs for a project root, or a single mailbox dir.

    Every top-level ``loop*`` dir is inspected: it is listed when it is itself
    a mailbox, and its direct subdirectories that are mailboxes are listed
    too (depth 2). ``.git``, ``briefs``, ``evidence*``, ``__pycache__`` and
    dot-dirs are skipped.
    """
    if not root.is_dir():
        return []
    if (root / "LOG.md").is_file() and not any(
        p.is_dir() and p.name.startswith("loop") for p in root.iterdir()
    ):
        return [root]
    found: list[Path] = []
    for top in sorted(root.iterdir()):
        if not top.is_dir() or not top.name.startswith("loop") or _skip_dir(top):
            continue
        if is_mailbox(top):
            found.append(top)
        for sub in sorted(top.iterdir()):
            if sub.is_dir() and not _skip_dir(sub) and is_mailbox(sub):
                found.append(sub)
    return found


def aggregate(loops: list[dict]) -> dict:
    total_loops = len(loops)
    total_iterations = 0
    distribution: dict[str, int] = {}
    ship_iterations: list[int] = []
    unparsed: list[str] = []

    for loop in loops:
        parsed = loop["parsed"]
        if not parsed:
            unparsed.append(loop["name"])

        for seg in loop["segments"]:
            total_iterations += seg["iteration_count"]

        # Use the last segment's final log verdict if available, otherwise fall back
        # to the VERDICT.md file.
        outcome = None
        if loop["segments"]:
            outcome = loop["segments"][-1]["final_log_verdict"]
        if not outcome and loop["final_verdict"]:
            outcome = loop["final_verdict"]
        if not outcome:
            outcome = "unparsed" if not parsed else "unknown"
        distribution[outcome] = distribution.get(outcome, 0) + 1

        # iterations-to-SHIP: every segment (or single-segment loop) ending SHIP.
        for seg in loop["segments"]:
            fv = seg["final_log_verdict"] or loop["final_verdict"]
            if fv == "SHIP":
                ship_iterations.append(seg["iteration_count"])
        # If there are no segments but VERDICT.md says SHIP, still count it.
        if not loop["segments"] and loop["final_verdict"] == "SHIP":
            # No iteration data; count as 1 to avoid zero-to-ship distortions.
            ship_iterations.append(1)

    mean_it = statistics.mean(ship_iterations) if ship_iterations else None
    median_it = statistics.median(ship_iterations) if ship_iterations else None

    return {
        "total_loops": total_loops,
        "total_iterations": total_iterations,
        "verdict_distribution": distribution,
        "iterations_to_ship": {
            "count": len(ship_iterations),
            "mean": mean_it,
            "median": median_it,
        },
        "unparsed_loops": unparsed,
    }


def fmt_value(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render(loops: list[dict], agg: dict) -> str:
    lines: list[str] = []
    for loop in loops:
        lines.append(f"loop: {loop['name']}")
        lines.append(f"  final_verdict: {fmt_value(loop['final_verdict'])}")
        lines.append(
            f"  state: {fmt_value(loop['state_status'])} "
            f"(iteration {fmt_value(loop['state_iteration'])}/{fmt_value(loop['state_max_iterations'])})")
        lines.append(f"  segments: {len(loop['segments'])}")
        for i, seg in enumerate(loop["segments"], 1):
            prefix = f"    segment {i}: " if len(loop["segments"]) > 1 else "    "
            final = seg["final_log_verdict"] or loop["final_verdict"] or "-"
            ds = seg["date_span"] or "-"
            lines.append(
                f"{prefix}format={seg['format']}, iterations={seg['iteration_count']}, "
                f"sequence={seg['verdict_sequence'] or '-'}, "
                f"lead={seg['lead_count']}, eval={seg['evaluator_count']}, "
                f"final={final}, dates={ds}")
        if not loop["parsed"]:
            lines.append("    [unparsed: no parseable LOG entries]")
        lines.append("")

    lines.append("---")
    lines.append(f"Total loops: {agg['total_loops']}")
    lines.append(f"Total iterations: {agg['total_iterations']}")
    dist = agg["verdict_distribution"]
    lines.append(
        "Verdict distribution: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(dist.items()))
    )
    its = agg["iterations_to_ship"]
    if its["count"]:
        lines.append(
            f"Iterations-to-SHIP ({its['count']} loops): "
            f"mean={fmt_value(its['mean'])}, median={fmt_value(its['median'])}"
        )
    else:
        lines.append("Iterations-to-SHIP: no SHIP loops")
    if agg["unparsed_loops"]:
        lines.append("Unparsed loops: " + ", ".join(agg["unparsed_loops"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute token/iteration metrics for trio-agent-loop mailboxes.",
    )
    parser.add_argument("path", help="Project directory or single loop directory")
    parser.add_argument("--json", action="store_true", help="Emit full JSON report")
    args = parser.parse_args(argv)

    root = Path(args.path).expanduser().resolve()
    loops = [analyze_loop(p, root) for p in discover_loops(root)]
    agg = aggregate(loops)
    report = {"loops": loops, "aggregate": agg}

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render(loops, agg))

    return 0


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------
# Iteration lifecycle derivation + overlap analysis (dashboard)
# --------------------------------------------------------------------------

LIFECYCLE_STATES = ("planned", "in_flight", "pending_eval", "shipped", "abandoned")
"""Per-iteration lifecycle states. Derivation rules: loop-iteration-model/PLAN.md."""

_MAILBOX_BASENAMES = {"GOAL.md", "PLAN.md", "STATE.md", "LOG.md", "REPORT.md", "VERDICT.md"}

_CRITERION_ID_RE = r"([A-Z]{1,3}(?:\d+(?:-[a-z])?|-[a-z]))"
_CRIT_HEADING_RE = re.compile(
    r"^#{1,6}\s+" + _CRITERION_ID_RE + r"\s*[—–-]\s*(.*?)\s*[—–-]\s*(?:\*\*)?(PASS|FAIL)(?:\*\*)?\s*(?:\([^)]*\))?\s*$",
    re.IGNORECASE,
)
_CRIT_LIST_RE = re.compile(
    r"^\s*[-*]\s*" + _CRITERION_ID_RE + r"[.)]\s+(.*?)\s*(?:\*\*)?(PASS|FAIL)(?:\*\*)?\s*$",
    re.IGNORECASE,
)
_CRIT_OUTCOME_CELL_RE = re.compile(r"\b(PASS|FAIL)\b|allPass\s*=\s*(true|false)", re.IGNORECASE)
_ITER_HEADING_RE = re.compile(r"^#{1,6}\s.*\biter(?:ation)?\s+(\d+)", re.IGNORECASE)


def parse_criteria_outcomes(text: str) -> list[dict]:
    """Parse acceptance-criteria outcome lines from VERDICT.md/REPORT.md text.

    Recognized shapes (parse, never invent):
      - Heading: ``## K1 — title — **PASS**``
      - List:    ``- K1. title **PASS**``
      - Table:   ``| **K1** | ... PASS ... |`` (or allPass=true/false)
    Returns [{"id", "title", "outcome"}] with outcome PASS/FAIL. Unparseable
    lines are skipped. Duplicate ids keep the first occurrence.
    """
    outcomes: list[dict] = []
    seen: set[str] = set()

    def add(cid: str, title: str, outcome: str) -> None:
        cid = cid.upper()
        if cid in seen:
            return
        seen.add(cid)
        outcomes.append({"id": cid, "title": title.strip(), "outcome": outcome.upper()})

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _CRIT_HEADING_RE.match(line)
        if m:
            add(m.group(1), m.group(2), m.group(3))
            continue
        m = _CRIT_LIST_RE.match(line)
        if m:
            add(m.group(1), m.group(2), m.group(3))
            continue
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            idm = re.match(r"^\**(" + _CRITERION_ID_RE[1:-1] + r")(?:\s+[^*]*)?\**$", cells[0])
            if not idm:
                continue
            outcome = None
            for cell in cells[1:]:
                om = _CRIT_OUTCOME_CELL_RE.search(cell)
                if om:
                    if om.group(1):
                        outcome = om.group(1).upper()
                    else:
                        outcome = "PASS" if om.group(2).lower() == "true" else "FAIL"
                    break
            if outcome:
                title = re.sub(r"\*\*", "", cells[1]).strip() if len(cells) > 1 else ""
                add(idm.group(1), title, outcome)
    return outcomes


def _section_for_iteration(text: str, n: int) -> str:
    """Return the body of the section whose heading mentions iteration N.

    Section runs from that heading to the next heading of same-or-higher
    level. Empty string when no such heading exists.
    """
    lines = text.splitlines()
    start = None
    level = 0
    for i, raw in enumerate(lines):
        m = _ITER_HEADING_RE.match(raw)
        if m and int(m.group(1)) == n:
            start = i
            level = len(raw) - len(raw.lstrip("#"))
            break
    if start is None:
        return ""
    out = [lines[start]]
    for raw in lines[start + 1:]:
        heading = raw.lstrip().lstrip("#")
        if raw.lstrip().startswith("#"):
            hlevel = len(raw.lstrip()) - len(heading)
            if hlevel <= level:
                break
        out.append(raw)
    return "\n".join(out)


def _parse_iso_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _attribute_verdict(verdict_text: str, state_iteration: int | None) -> tuple[str | None, int | None]:
    """Lenient VERDICT.md word + the iteration it is attributed to.

    The word comes from any ``VERDICT: <word>`` or ``Overall: <word>`` line
    (known words only). Attribution: the iteration named in a heading, else
    STATE.iteration.
    """
    if not verdict_text.strip():
        return None, None
    known = set(KNOWN_VERDICTS)
    word = None
    for pattern in (VERDICT_RE, OVERALL_VERDICT_RE):
        for raw in verdict_text.splitlines():
            m = pattern.search(raw)
            if m and m.group(1).upper() in known:
                word = m.group(1).upper()
                break
        if word:
            break
    if word is None:
        return None, None
    for raw in verdict_text.splitlines():
        m = _ITER_HEADING_RE.match(raw)
        if m:
            return word, int(m.group(1))
    return word, state_iteration


def _norm_declared_path(p: str) -> str | None:
    """Normalize a declared write/read path; None when it is not a product path.

    Drops ``api:`` pseudo-targets and mailbox bookkeeping paths.
    """
    p = p.strip()
    if not p or p.startswith("api:"):
        return None
    p = p.lstrip("./").rstrip("/")
    if not p:
        return None
    parts = p.split("/")
    if parts[0].startswith("loop"):
        return None
    if len(parts) == 1 and parts[0] in _MAILBOX_BASENAMES:
        return None
    return p


def _covers(a: str, b: str) -> bool:
    """True when declared path ``a`` covers ``b`` (equal or directory prefix)."""
    return a == b or b.startswith(a + "/")


def iteration_path_sets(slices: list[dict] | None) -> dict:
    """Map iteration number -> {"writes": set, "reads": set} of product paths."""
    sets: dict[int, dict] = {}
    for sl in slices or []:
        n = sl.get("iteration")
        if n is None:
            continue
        entry = sets.setdefault(n, {"writes": set(), "reads": set()})
        for p in sl.get("writes") or []:
            np = _norm_declared_path(p)
            if np:
                entry["writes"].add(np)
        for p in sl.get("reads") or []:
            np = _norm_declared_path(p)
            if np:
                entry["reads"].add(np)
    return sets


def _intersect(a: set, b: set) -> set:
    """Shared paths between two declared sets, honoring directory prefixes."""
    shared = set()
    for p in a:
        for q in b:
            if _covers(p, q):
                shared.add(q)
            elif _covers(q, p):
                shared.add(p)
    return shared


_NON_COMPLETE = ("planned", "in_flight", "pending_eval")


def iteration_overlaps(iterations: list[dict], path_sets: dict) -> list[dict]:
    """Overlapping write/read sets between non-complete iterations.

    Returns [{"a", "b", "paths", "relation"}] where relation is
    "write-write" | "write-read" | "both". Pairs are unordered, a < b.
    """
    active = [it["n"] for it in iterations if it.get("lifecycle") in _NON_COMPLETE]
    overlaps = []
    ordered = sorted(active)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            sa = path_sets.get(a, {"writes": set(), "reads": set()})
            sb = path_sets.get(b, {"writes": set(), "reads": set()})
            ww = _intersect(sa["writes"], sb["writes"])
            wr = _intersect(sa["writes"], sb["reads"]) | _intersect(sb["writes"], sa["reads"])
            if not ww and not wr:
                continue
            relation = "both" if ww and wr else ("write-write" if ww else "write-read")
            overlaps.append({
                "a": a,
                "b": b,
                "paths": sorted(ww | wr),
                "relation": relation,
            })
    return overlaps


def _lifecycle_for(n: int, entries: list[dict], slice_list: list[dict],
                   last_verdict: str | None, is_current: bool,
                   state_status: str, state_iteration: int | None) -> str:
    """First-match-wins lifecycle derivation (PLAN.md rules)."""
    if last_verdict:
        return {"SHIP": "shipped", "ITERATE": "abandoned",
                "BLOCKED": "abandoned", "NEEDS_HUMAN": "pending_eval"}[last_verdict]
    if is_current:
        if state_status in ("pending_eval", "evaluating", "awaiting_eval"):
            return "pending_eval"
        if state_status in ("error", "blocked", "aborted", "failed"):
            return "abandoned"
        if state_status in ("planning", "ready") and not entries and not any(
                s.get("status") == "in_progress" for s in slice_list):
            return "planned"
    if any(s.get("status") == "in_progress" for s in slice_list):
        return "in_flight"
    if any(s.get("status") == "planned" for s in slice_list) and not entries:
        return "planned"
    if entries:
        if state_iteration is not None and n < state_iteration and (
                (slice_list and all(s.get("status") == "complete" for s in slice_list))
                or not slice_list):
            return "shipped"
        return "in_flight"
    if state_iteration is not None and n < state_iteration and slice_list and all(
            s.get("status") == "complete" for s in slice_list):
        return "shipped"
    return "in_flight" if is_current else "planned"


def derive_iterations(state: dict, timeline: list, slices: list | None,
                      *, verdict_text: str = "", report_text: str = "",
                      slice_activity: dict | None = None) -> list[dict]:
    """Derive per-iteration lifecycle + compare-mode facts for one loop.

    Works on existing mailboxes with no protocol change. Inputs are the
    already-parsed structures (parse_state, parse_timeline,
    parse_slices_block) plus raw VERDICT.md/REPORT.md text and optional
    shadow slice_activity. Returns a list sorted by iteration number:
    {"n", "lifecycle", "verdict", "started", "ended", "duration_sec",
     "files", "criteria"}.
    """
    state = state or {}
    slices = slices or []
    state_iteration = _to_int(state.get("iteration"))
    state_status = (state.get("status") or "").strip().lower().split()
    state_status = state_status[0] if state_status else ""

    entries_by_n: dict[int, list] = {}
    ns: set[int] = set()
    for e in timeline or []:
        n = e.get("iteration")
        if n is None:
            continue
        ns.add(n)
        entries_by_n.setdefault(n, []).append(e)

    slices_by_n: dict[int, list] = {}
    for sl in slices:
        n = sl.get("iteration")
        if n is None:
            continue
        ns.add(n)
        slices_by_n.setdefault(n, []).append(sl)
    if state_iteration is not None:
        ns.add(state_iteration)

    vword, v_n = _attribute_verdict(verdict_text, state_iteration)

    activity_by_id: dict[str, list] = {}
    for sl in (slice_activity or {}).get("slices", []):
        activity_by_id[sl.get("id")] = sl.get("actual_writes") or []

    verdict_sections = _sections_by_iteration(verdict_text)
    report_sections = _sections_by_iteration(report_text)

    iterations = []
    for n in sorted(ns):
        entries = entries_by_n.get(n, [])
        slice_list = slices_by_n.get(n, [])

        last_verdict = None
        for e in entries:
            if e.get("verdict") in KNOWN_VERDICTS:
                last_verdict = e["verdict"]
        if last_verdict is None and vword and v_n == n:
            last_verdict = vword

        is_current = state_iteration is not None and n == state_iteration
        lifecycle = _lifecycle_for(n, entries, slice_list, last_verdict,
                                   is_current, state_status, state_iteration)

        starts = [_parse_iso_ts(e.get("started_at")) for e in entries]
        ends = [_parse_iso_ts(e.get("ended_at")) for e in entries]
        starts = [s for s in starts if s]
        ends = [s for s in ends if s]
        started = min(starts).isoformat().replace("+00:00", "Z") if starts else None
        ended = max(ends).isoformat().replace("+00:00", "Z") if ends else None
        duration = None
        if starts and ends:
            duration = int((max(ends) - min(starts)).total_seconds())
        else:
            durs = [e.get("duration_sec") for e in entries if e.get("duration_sec") is not None]
            if durs:
                duration = max(durs)

        files: set[str] = set()
        for sl in slice_list:
            for p in sl.get("writes") or []:
                np = _norm_declared_path(p)
                if np:
                    files.add(np)
            for p in activity_by_id.get(sl.get("id"), []):
                np = _norm_declared_path(p)
                if np:
                    files.add(np)

        crit_text = verdict_sections.get(n) or report_sections.get(n) or ""
        criteria = parse_criteria_outcomes(crit_text) if crit_text else []

        iterations.append({
            "n": n,
            "lifecycle": lifecycle,
            "verdict": last_verdict,
            "started": started,
            "ended": ended,
            "duration_sec": duration,
            "files": sorted(files),
            "criteria": criteria,
        })
    return iterations


SLICE_COMMIT_PREFIX_RE_TMPL = "slice({}): "
"""``slice(<id>): `` commit-subject prefix, per MAILBOX-SCHEMA.md's
per-slice commit gate (trio-shadow.py --require-commits)."""


def derive_slices(
    slices: list[dict] | None,
    queue: dict | None,
    verdict_text: str = "",
    commits: "list[str] | None" = None,
    *,
    open_loop: bool | None = None,
) -> list[dict]:
    """Derive per-slice lifecycle for one mailbox (GOAL.md "Slice lifecycle
    (derived, stdlib, in metrics/trio-metrics.py)", PLAN.md's frozen
    ``api:DeriveSlices`` contract).

    Inputs are already-parsed structures: ``slices`` from
    ``parse_slices_block`` (PLAN.md order; None/[] -> [] out), ``queue``
    from ``read_queue`` (None treated as ``{"retired": [], "faults": []}``),
    raw ``verdict_text`` (parsed here via ``parse_slice_verdicts``), and an
    optional iterable of commit subject lines (``git log --format=%s``;
    None means "no commit information available" -- NOT "no commits").

    ``open_loop`` selects the derivation mode: True/False from the caller,
    or inferred as ``bool(queue["retired"] or queue["faults"])`` when None
    (a lockstep mailbox with no QUEUE.md has neither). Lockstep mailboxes
    (``open_loop`` False) only ever produce planned/building/shipped, from
    ``status``/commits alone, with retired_sha/retired_at/verdict left None
    and open_faults/superseded/stale_candidates left []; this keeps
    derive_iterations' lockstep behaviour completely untouched -- nothing
    here reads or affects derive_iterations/_lifecycle_for.

    Returns one dict per slice, in PLAN.md order, with exactly the keys:
    id, iteration, lifecycle, retired_sha, retired_at, verdict, open_faults,
    superseded, stale_candidates.

    `retired:` is append-only and means "retired at sha" (GOAL.md's
    "Resolving the retired: sha tension"): the LATEST entry for a slice
    (last occurrence in QUEUE.md file order) gives retired_sha/retired_at;
    any earlier shas for that slice are superseded (deduped, order
    preserved, latest excluded). A live fault (status open or taken)
    observed at a superseded sha is a stale_candidate.

    Open-loop lifecycle precedence (first match wins; PLAN.md's DECISION
    note resolves the GOAL.md faulted/repairing ordering literally, since
    "faulted := latest verdict ITERATE" read alone would make `repairing`
    unreachable -- a fault is created together with its ITERATE section):
      1. planned    -- no retired entry, no `slice(<id>):` commit, and
                       status is "planned" or absent/blank.
      2. building   -- no retired entry, and status is "in_progress" or the
                       slice has a `slice(<id>): ` commit. (Also the
                       fallback for any other no-retired-entry status, e.g.
                       "complete" with no commit/retired trace yet, so
                       every input still resolves.)
      3. retired    -- has a retired entry whose latest sha has no
                       matching `## slice <id> @<sha>` verdict section.
      4. faulted    -- any fault for this slice is `open`; OR the latest
                       matching verdict is ITERATE and no fault is `taken`.
      5. repairing  -- any fault for this slice is `taken` (and none open).
      6. shipped    -- latest matching verdict is SHIP and no open/taken
                       fault remains.
    "Latest matching verdict" is the verdict of the LAST `## slice <id>
    @<sha>` section (file order) whose sha the slice's latest retired sha
    startswith (so a short sha in the heading still matches).
    """
    slices = slices or []
    queue = queue or {"retired": [], "faults": []}
    retired_all = queue.get("retired") or []
    faults_all = queue.get("faults") or []
    if open_loop is None:
        open_loop = bool(retired_all or faults_all)

    verdict_sections = parse_slice_verdicts(verdict_text or "")
    commit_subjects = list(commits) if commits is not None else None

    out: list[dict] = []
    for sl in slices:
        sid = sl.get("id")
        status = (sl.get("status") or "").strip()
        has_commit = commit_subjects is not None and any(
            c.startswith(SLICE_COMMIT_PREFIX_RE_TMPL.format(sid)) for c in commit_subjects
        )

        entries_for_slice = [e for e in retired_all if e.get("slice") == sid]
        faults_for_slice = [f for f in faults_all if f.get("slice") == sid]

        if not open_loop:
            if status == "complete":
                lifecycle = "shipped"
            elif status == "in_progress" or has_commit:
                lifecycle = "building"
            else:
                lifecycle = "planned"
            out.append({
                "id": sid,
                "iteration": sl.get("iteration"),
                "lifecycle": lifecycle,
                "retired_sha": None,
                "retired_at": None,
                "verdict": None,
                "open_faults": [],
                "superseded": [],
                "stale_candidates": [],
            })
            continue

        has_retired = bool(entries_for_slice)
        retired_sha = None
        retired_at = None
        superseded: list[str] = []
        if has_retired:
            latest_entry = entries_for_slice[-1]
            retired_sha = latest_entry.get("sha")
            retired_at = latest_entry.get("at")
            seen: list[str] = []
            for e in entries_for_slice[:-1]:
                sha = e.get("sha")
                if sha and sha != retired_sha and sha not in seen:
                    seen.append(sha)
            superseded = seen

        verdict = None
        if retired_sha:
            for v in verdict_sections:
                if v["slice"] != sid:
                    continue
                if retired_sha.startswith(v["sha"]):
                    verdict = v["verdict"]

        live_faults = [f for f in faults_for_slice if f.get("status") in ("open", "taken")]
        open_faults = [f.get("id") for f in live_faults]
        open_fault_exists = any(f.get("status") == "open" for f in faults_for_slice)
        taken_fault_exists = any(f.get("status") == "taken" for f in faults_for_slice)
        stale_candidates = [
            f.get("id") for f in live_faults if f.get("observed_at") in superseded
        ]

        if not has_retired and not has_commit and status in ("planned", ""):
            lifecycle = "planned"
        elif not has_retired and (status == "in_progress" or has_commit):
            lifecycle = "building"
        elif not has_retired:
            lifecycle = "building"
        elif verdict is None:
            lifecycle = "retired"
        elif open_fault_exists or (verdict == "ITERATE" and not taken_fault_exists):
            lifecycle = "faulted"
        elif taken_fault_exists:
            lifecycle = "repairing"
        else:
            lifecycle = "shipped"

        out.append({
            "id": sid,
            "iteration": sl.get("iteration"),
            "lifecycle": lifecycle,
            "retired_sha": retired_sha,
            "retired_at": retired_at,
            "verdict": verdict,
            "open_faults": open_faults,
            "superseded": superseded,
            "stale_candidates": stale_candidates,
        })
    return out


def _sections_by_iteration(text: str) -> dict[int, str]:
    """Map iteration number -> section body, for every heading naming one."""
    sections: dict[int, str] = {}
    if not text.strip():
        return sections
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = _ITER_HEADING_RE.match(raw)
        if m:
            n = int(m.group(1))
            sections[n] = _section_for_iteration(text, n)
    return sections

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
SLICE_KEYS = ("id", "repo", "writes", "reads", "gate", "status", "iteration")
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


def _parse_flow_list(value: str, line: int) -> list[str]:
    m = FLOW_LIST_RE.match(value)
    if not m:
        raise SliceParseError(
            f"line {line}: `writes:`/`reads:` must be a bracketed list like "
            f'[path.py, "api:Name"], got {value!r}'
        )
    return [_unquote(part) for part in m.group(1).split(",") if part.strip()]


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
            elif key in ("writes", "reads"):
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


def analyze_loop(loop_dir: Path) -> dict:
    name = loop_dir.name
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


def discover_loops(root: Path) -> list[Path]:
    """Return loop dirs for a project root, or a single loop dir."""
    if not root.is_dir():
        return []
    if (root / "LOG.md").is_file():
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("loop"))


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
    loops = [analyze_loop(p) for p in discover_loops(root)]
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

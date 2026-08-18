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


def parse_verdict(verdict_path: Path) -> str | None:
    """Return the verdict on the first non-empty line of VERDICT.md."""
    if not verdict_path.is_file():
        return None
    with verdict_path.open("r", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            m = VERDICT_RE.match(line)
            if m:
                return m.group(1).upper()
            break
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

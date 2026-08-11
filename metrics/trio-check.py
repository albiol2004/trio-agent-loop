#!/usr/bin/env python3
"""trio-check.py — mailbox schema conformance checker for trio-agent-loop mailboxes.

Usage: trio-check.py [path] [--json]

Detects loop mailboxes exactly like metrics/trio-metrics.py (discover_loops)
and reuses its STATE.md / VERDICT.md parsing, then classifies each mailbox:

  v1      STATE.md contains `schema: 1`
  legacy  STATE.md exists but has no `schema: 1` marker
  unknown STATE.md is missing or unreadable

v1 mailboxes are validated against MAILBOX-SCHEMA.md: required files, required
STATE.md fields, the VERDICT.md first-line contract, and a non-empty LOG.md.
Legacy and unknown mailboxes are reported for information and never fail the
run.

Exit code: 0 when no v1 mailbox has violations, 1 when at least one does.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REQUIRED_FILES = (
    "GOAL.md",
    "STATE.md",
    "PLAN.md",
    "REPORT.md",
    "VERDICT.md",
    "LOG.md",
)
REQUIRED_STATE_KEYS = ("iteration", "max_iterations", "status", "mission")
VALID_VERDICTS = ("SHIP", "ITERATE", "BLOCKED")

# Mirrors the STATE.md key-line format of trio-metrics.STATE_RE
# (^\s*(?:-\s+)?<key>\s*:\s*(.*)$, case-insensitive), extended with the
# `schema` key so the version marker is detected with the same syntax rules.
SCHEMA_RE = re.compile(r"^\s*(?:-\s+)?schema\s*:\s*(.*)$", re.IGNORECASE)


def load_trio_metrics():
    """Load metrics/trio-metrics.py as a module.

    The filename contains a hyphen, so it cannot be imported by name. Loading
    it from source keeps this checker's format detection consistent with
    trio-metrics.py: we reuse discover_loops(), parse_state(), parse_verdict()
    and VERDICT_RE instead of duplicating them.
    """
    path = Path(__file__).resolve().parent / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def missing_required_files(loop_dir: Path) -> list[str]:
    return [name for name in REQUIRED_FILES if not (loop_dir / name).is_file()]


def classify_version(state_path: Path) -> tuple[str, list[str]]:
    """Return (version, info_lines) for a loop dir's STATE.md.

    v1      — STATE.md contains a `schema: 1` top-level key.
    legacy  — STATE.md exists but has no `schema: 1` marker.
    unknown — STATE.md is missing or unreadable.
    """
    if not state_path.is_file():
        return "unknown", ["STATE.md missing — cannot determine schema version"]
    try:
        with state_path.open("r", errors="replace") as fh:
            for raw in fh:
                m = SCHEMA_RE.match(raw)
                if m:
                    value = m.group(1).strip()
                    if value == "1":
                        return "v1", []
                    return "legacy", [
                        f"STATE.md schema marker has value {value!r}, expected `1`"
                    ]
        return "legacy", ["STATE.md has no `schema:` marker (pre-v1 mailbox)"]
    except OSError as exc:
        return "unknown", [f"STATE.md unreadable: {exc}"]


def check_verdict(loop_dir: Path, tm) -> list[str]:
    """Validate VERDICT.md's first-line contract (empty file is allowed)."""
    verdict_path = loop_dir / "VERDICT.md"
    if not verdict_path.is_file():
        return ["VERDICT.md missing"]
    first = None
    try:
        with verdict_path.open("r", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line:
                    first = line
                    break
    except OSError as exc:
        return [f"VERDICT.md unreadable: {exc}"]
    if first is None:
        # No verdict recorded yet — allowed (e.g. a freshly initialized loop).
        return []
    m = tm.VERDICT_RE.match(first)
    if not m or m.group(1).upper() not in VALID_VERDICTS:
        return [
            "VERDICT.md first non-empty line must be `VERDICT: SHIP|ITERATE|BLOCKED` "
            f"(case-insensitive, optional `# ` prefix); got: {first!r}"
        ]
    return []


def check_log(loop_dir: Path) -> list[str]:
    """LOG.md must exist and be non-empty (a `# Trio loop log` header suffices)."""
    log_path = loop_dir / "LOG.md"
    if not log_path.is_file():
        return ["LOG.md missing"]
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        return [f"LOG.md unreadable: {exc}"]
    if not any(line.strip() for line in text.splitlines()):
        return ["LOG.md is empty — expected at least a `# Trio loop log` header"]
    return []


def check_v1(loop_dir: Path, tm) -> list[str]:
    """Validate a v1 mailbox; return violations (empty list = conformant).

    The `schema: 1` requirement is enforced by classify_version(), which gates
    entry into v1 validation.
    """
    errors: list[str] = []

    missing = missing_required_files(loop_dir)
    if missing:
        errors.append("missing required file(s): " + ", ".join(missing))

    state = tm.parse_state(loop_dir / "STATE.md")
    for key in REQUIRED_STATE_KEYS:
        if key not in state or not str(state[key]).strip():
            errors.append(f"STATE.md missing required field `{key}`")

    errors.extend(check_verdict(loop_dir, tm))
    errors.extend(check_log(loop_dir))
    return errors


def inspect_loop(loop_dir: Path, tm) -> dict:
    version, info = classify_version(loop_dir / "STATE.md")
    if version == "v1":
        errors = check_v1(loop_dir, tm)
    else:
        errors = []
        missing = missing_required_files(loop_dir)
        if missing:
            info.append("missing v1 required file(s): " + ", ".join(missing))
    return {
        "name": loop_dir.name,
        "path": str(loop_dir),
        "version": version,
        "errors": errors,
        "info": info,
        "ok": version == "v1" and not errors,
    }


def summarize(loops: list[dict]) -> dict:
    counts = {"v1": 0, "legacy": 0, "unknown": 0}
    violations = 0
    for loop in loops:
        counts[loop["version"]] = counts.get(loop["version"], 0) + 1
        if loop["version"] == "v1" and loop["errors"]:
            violations += 1
    return {
        "total": len(loops),
        "v1": counts["v1"],
        "legacy": counts["legacy"],
        "unknown": counts["unknown"],
        "v1_violations": violations,
        "ok": violations == 0,
    }


def render(loops: list[dict], root: Path, summary: dict) -> str:
    lines = [f"Checked: {root}"]
    if not loops:
        lines.append("No loop mailboxes found.")
    for loop in loops:
        if loop["version"] == "v1":
            lines.append(f"{loop['name']}/  v1  {'PASS' if loop['ok'] else 'FAIL'}")
            for err in loop["errors"]:
                lines.append(f"    - {err}")
        else:
            lines.append(
                f"{loop['name']}/  {loop['version']}  (informational — not validated)"
            )
            for note in loop["info"]:
                lines.append(f"    - {note}")
    lines.append(
        f"Summary: {summary['total']} loop(s): {summary['v1']} v1, "
        f"{summary['legacy']} legacy, {summary['unknown']} unknown; "
        f"{summary['v1_violations']} v1 mailbox(es) with violations"
    )
    lines.append("Result: PASS" if summary["ok"] else "Result: FAIL")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check trio-agent-loop mailboxes against MAILBOX-SCHEMA.md.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project directory or single loop mailbox (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    args = parser.parse_args(argv)

    tm = load_trio_metrics()
    root = Path(args.path).expanduser().resolve()
    loops = [inspect_loop(p, tm) for p in tm.discover_loops(root)]
    summary = summarize(loops)

    if args.json:
        json.dump({"path": str(root), "loops": loops, "summary": summary}, sys.stdout, indent=2)
        print()
    else:
        print(render(loops, root, summary))

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""trio-shadow.py — shadow-mode slice contract checker for the trio pipeline.

Usage:
  python3 metrics/trio-shadow.py --mailbox <loop-or-project-dir> [--json]

Reads the machine-readable ``slices:`` block from a mailbox's PLAN.md
(schema: MAILBOX-SCHEMA.md), resolves each slice's commits in its target
git repo by the ``slice(<id>): `` commit-message prefix, and reports
declared ``writes:`` vs the files actually touched per slice:

  declared writes         every entry in the slice's ``writes:`` list
  actual touched          union of files in the slice's commits
  touched but undeclared  actual files no declared write covers
  declared but untouched  declared path entries no commit touched

``api:<Name>`` entries are interface names, not paths: they appear in
``declared writes`` for transparency but are excluded from git matching.

Shadow mode: the report is observability only — undeclared writes are
measured, never enforced. The exit code is 0 whenever the analysis itself
succeeds, including missing/non-git repos and slices with no prefixed
commits; 2 means the PLAN.md slices block is missing or malformed; 1 is a
usage error. This script never gates anything.

Stdlib only — the restricted YAML shape is parsed line-based; there is no
PyYAML dependency.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

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
    `in_progress`; `iteration` is optional (validated as int when present).
    Anything else raises SliceParseError with the offending line number.
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


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True
    )


def slice_commits(slice_id: str, repo_dir: Path) -> list[str] | None:
    """Commit shas whose message starts with `slice(<id>): `; None if not a git repo.

    Slice ids are restricted to kebab-case, so the id is safe to embed in
    the BRE --grep pattern (`(`/`)` are literal in basic regex).
    """
    probe = _git(repo_dir, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        return None
    proc = _git(repo_dir, "log", "--format=%H", f"--grep=^slice({slice_id}):")
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def commit_files(sha: str, repo_dir: Path) -> list[str]:
    """File names touched by one commit (handles root commits; merges resolve
    to their combined diff)."""
    proc = _git(repo_dir, "show", "--format=", "--name-only", sha)
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _normalize(path: str) -> str:
    path = path.strip().rstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path


def covers(declared: str, actual: str) -> bool:
    """Whether a declared write covers an actual file: exact path match, or
    the declared path is a directory prefix of it."""
    d, a = _normalize(declared), _normalize(actual)
    if not d or not a:
        return False
    return a == d or a.startswith(d + "/")


def analyze_slice(sl: dict, base: Path) -> dict:
    repo_path = (base / sl["repo"]).resolve()
    entry: dict = {
        "id": sl["id"],
        "repo": sl["repo"],
        "repo_path": str(repo_path),
        "repo_status": "ok",
        "commits": [],
        "declared_writes": sl["writes"],
        "actual_touched": [],
        "undeclared_touches": [],
        "declared_untouched": [],
    }
    if not repo_path.exists():
        entry["repo_status"] = "missing"
        return entry
    commits = slice_commits(sl["id"], repo_path)
    if commits is None:
        entry["repo_status"] = "not-a-git-repo"
        return entry

    entry["commits"] = commits
    touched: set[str] = set()
    for sha in commits:
        touched.update(commit_files(sha, repo_path))
    entry["actual_touched"] = sorted(touched)

    # api: entries are interface names, not paths — they never match git files.
    declared = list(
        dict.fromkeys(w for w in sl["writes"] if not w.startswith("api:"))
    )
    entry["undeclared_touches"] = sorted(
        f for f in touched if not any(covers(d, f) for d in declared)
    )
    entry["declared_untouched"] = sorted(
        d for d in declared if not any(covers(d, f) for f in touched)
    )
    return entry


def analyze(mailbox: Path) -> dict:
    mailbox = mailbox.resolve()
    plan_path = mailbox / "loop" / "PLAN.md"
    if not plan_path.is_file():
        plan_path = mailbox / "PLAN.md"
    if not plan_path.is_file():
        raise SliceParseError(
            f"PLAN.md not found under {mailbox} "
            f"(looked for {mailbox / 'loop' / 'PLAN.md'} and {mailbox / 'PLAN.md'})"
        )
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SliceParseError(f"PLAN.md unreadable: {exc}") from exc

    slices = parse_slices(find_slices_block(text))
    entries = [analyze_slice(sl, mailbox) for sl in slices]
    summary = {
        "total_slices": len(entries),
        "slices_with_undeclared_touches": sum(
            1 for e in entries if e["undeclared_touches"]
        ),
        "undeclared_file_count": len(
            {f for e in entries for f in e["undeclared_touches"]}
        ),
        "repos_missing_or_not_git": sum(
            1 for e in entries if e["repo_status"] != "ok"
        ),
        "slices_without_commits": sum(1 for e in entries if not e["commits"]),
    }
    return {
        "mailbox": str(mailbox),
        "plan": str(plan_path),
        "slices": entries,
        "summary": summary,
    }


def _join(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


def render(report: dict) -> str:
    lines = [
        f"Checked: {report['mailbox']}",
        f"Slice contracts: {report['plan']}",
    ]
    for sl in report["slices"]:
        if sl["repo_status"] == "ok":
            head = (
                f"  {sl['id']}  (repo: {sl['repo']}, {len(sl['commits'])} commit(s), "
                f"{len(sl['actual_touched'])} file(s) touched)"
            )
        else:
            why = "repo missing" if sl["repo_status"] == "missing" else "not a git repo"
            head = f"  {sl['id']}  (repo: {sl['repo']}, {why}: {sl['repo_path']})"
        lines.append(head)
        lines.append(f"    declared writes: {_join(sl['declared_writes'])}")
        lines.append(f"    actual touched:  {_join(sl['actual_touched'])}")
        if sl["undeclared_touches"]:
            lines.append(f"    touched but undeclared: {_join(sl['undeclared_touches'])}")
        if sl["declared_untouched"]:
            lines.append(f"    declared but untouched: {_join(sl['declared_untouched'])}")
        if sl["repo_status"] == "ok" and not sl["commits"]:
            lines.append("    no slice-prefixed commits")
    s = report["summary"]
    lines.append(
        f"Summary: {s['total_slices']} slice(s), "
        f"{s['slices_with_undeclared_touches']} with undeclared touches, "
        f"{s['undeclared_file_count']} undeclared file(s)"
    )
    if s["repos_missing_or_not_git"]:
        lines.append(
            f"  ({s['repos_missing_or_not_git']} slice(s) with a missing or "
            "non-git repo)"
        )
    lines.append("Result: shadow mode — informational only, never gates (exit 0)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure declared-vs-actual writes for PLAN.md slices "
        "(shadow mode: observability only, never gates).",
    )
    parser.add_argument(
        "--mailbox",
        default=".",
        help="loop dir or project dir containing loop/ (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    args = parser.parse_args(argv)

    try:
        report = analyze(Path(args.mailbox))
    except SliceParseError as exc:
        print(f"trio-shadow.py: error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

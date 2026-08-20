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
usage error.

Active mode (the commit gate):

  python3 metrics/trio-shadow.py --mailbox <dir> --require-commits

With --require-commits, a slice that is code-changing — at least one
declared ``writes:`` entry that is neither an ``api:`` pseudo-entry nor a
path inside ``loop/`` — must have at least one matching
``slice(<id>): `` commit, or the script exits 1 listing the offenders.
Exit 0 means every code-changing slice has commits (slices that write only
``loop/`` files or ``api:`` names are exempt). A missing or malformed
slices block still exits 2, and a parseable block with no code-changing
slices never fails the gate. This is the first active interlock: drivers
run it post-Lead, pre-Evaluator, and retry the Lead once on exit 1.

Stdlib only — the restricted YAML shape is parsed line-based; there is no
PyYAML dependency. The block parser itself (``find_slices_block`` /
``parse_slices`` / ``SliceParseError``) is shared from trio-metrics.py, the
single source of truth for the format, and loaded here by file location.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

def _load_metrics_module():
    """Load metrics/trio-metrics.py via importlib by path.

    The hyphenated filename cannot be imported normally, so the slice
    block parser is shared from trio-metrics.py and loaded here by file
    location (same pattern as trio-check.py and dashboard/serve.py). This
    keeps one source of truth for the PLAN.md ``slices:`` format.
    """
    path = Path(__file__).resolve().parent / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_METRICS = _load_metrics_module()
SliceParseError = _METRICS.SliceParseError
find_slices_block = _METRICS.find_slices_block
parse_slices = _METRICS.parse_slices


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


def _is_loop_path(path: str) -> bool:
    norm = _normalize(path)
    return norm == "loop" or norm.startswith("loop/")


def code_changing_writes(declared: list[str]) -> list[str]:
    """Declared writes that make a slice code-changing: not an ``api:``
    pseudo-entry and not a path inside ``loop/``."""
    return [
        w for w in declared
        if not w.startswith("api:") and not _is_loop_path(w)
    ]


def commit_gate_offenders(report: dict) -> list[dict]:
    """Slices that are code-changing but have zero ``slice(<id>): `` commits.

    The commit gate fails on exactly these; loop/-only and api:-only slices
    are exempt even without commits.
    """
    return [
        e for e in report["slices"]
        if code_changing_writes(e["declared_writes"]) and not e["commits"]
    ]


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


def render(report: dict, require_commits: bool = False) -> str:
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
    if require_commits:
        lines.append(
            "Result: commit gate active — slice commits enforced (exit 0 if the gate passes, 1 otherwise)"
        )
    else:
        lines.append("Result: shadow mode — informational only, never gates (exit 0)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure declared-vs-actual writes for PLAN.md slices "
        "(shadow mode: observability only; --require-commits turns on the "
        "active commit gate).",
    )
    parser.add_argument(
        "--mailbox",
        default=".",
        help="loop dir or project dir containing loop/ (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    parser.add_argument(
        "--require-commits",
        action="store_true",
        help="ACTIVE interlock: exit 1 when any code-changing slice (a writes "
        "entry that is neither api: nor under loop/) has no slice(<id>): "
        "commit; exit 0 when every code-changing slice has commits. "
        "Missing/malformed slices block still exits 2.",
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
        print(render(report, require_commits=args.require_commits))

    if args.require_commits:
        offenders = commit_gate_offenders(report)
        if offenders:
            for e in offenders:
                why = ""
                if e["repo_status"] != "ok":
                    why = f" (repo: {e['repo_status']})"
                cc = ", ".join(code_changing_writes(e["declared_writes"]))
                print(
                    f"commit gate: slice {e['id']!r} is code-changing "
                    f"(writes: {cc}) but has no slice({e['id']}): commit{why}"
                )
            print(
                f"commit gate: FAIL — {len(offenders)} code-changing slice(s) "
                "missing slice-prefixed commits; fix before Evaluator dispatch"
            )
            return 1
        print("commit gate: PASS — every code-changing slice has a slice(<id>): commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())

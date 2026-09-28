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
run. When the scanned root contains prompts/generate.py, the checker also
runs its `--check` mode so drift in the single-sourced role prompts fails
conformance (disable with `--no-prompt-sync`).

Exit code: 0 when no v1 mailbox has violations, 1 when at least one does,
2 when a v1 mailbox's PLAN.md has a slice that writes outside its repo
(r15: the mailbox repo, or the declared `repos:` entry its `repo:` names;
absolute path elsewhere, `..` escape, undeclared nested git clone, a
second repo, or a builder brief's targeted-check `cd` there), an invalid
`repos:`/per-repo `full_check:` block, or an unparseable slices block --
one stderr line per offending slice -- and when the sibling
trio-metrics.py has a different METRICS_API.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TRIO_CHECK_VERSION = "1.0.0"

REQUIRED_FILES = (
    "GOAL.md",
    "STATE.md",
    "PLAN.md",
    "REPORT.md",
    "VERDICT.md",
    "LOG.md",
)
REQUIRED_STATE_KEYS = ("iteration", "max_iterations", "status", "mission")
VALID_VERDICTS = ("SHIP", "ITERATE", "BLOCKED", "NEEDS_HUMAN")

# Optional scope= suffix, allowed only on ITERATE: scope=design for an
# explicit full Lead iteration, or scope=local:<comma-separated-paths> for a
# builder-direct repair pass.
SCOPE_RE = re.compile(r"^scope=(design|local:[^\s]+)$", re.IGNORECASE)

# Mirrors the STATE.md key-line format of trio-metrics.STATE_RE
# (^\s*(?:-\s+)?<key>\s*:\s*(.*)$, case-insensitive), extended with the
# `schema` key so the version marker is detected with the same syntax rules.
SCHEMA_RE = re.compile(r"^\s*(?:-\s+)?schema\s*:\s*(.*)$", re.IGNORECASE)

# v1 open-loop extension (MAILBOX-SCHEMA.md "Per-slice verdicts in VERDICT.md"):
# a QUEUE.md mailbox's VERDICT.md may legitimately consist only of appended
# per-slice sections and therefore have no `VERDICT:` first line.
OPEN_LOOP_SLICE_VERDICT_RE = re.compile(
    r"^## slice \S+ @[0-9a-f]{7,40} — (SHIP|ITERATE)$"
)
VALID_FAULT_STATUSES = ("open", "taken", "done", "stale")
FAULT_ID_RE = re.compile(r"^f\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


#: The sibling trio-metrics.py contract this checker calls into
#: (``METRICS_API``; 4 = ``find_queue_block(..., errors=)``, r11h; 5 =
#: declared repos, r15; 6 = root-free open-loop, r16). Must equal
#: omnigent/trioctl ``REQUIRED_METRICS_API``.
REQUIRED_METRICS_API = 6


class MetricsApiMismatch(ImportError):
    """The sibling trio-metrics.py is from a different metrics/ release."""


def load_trio_metrics():
    """Load metrics/trio-metrics.py as a module.

    The filename contains a hyphen, so it cannot be imported by name. Loading
    it from source keeps this checker's format detection consistent with
    trio-metrics.py: we reuse discover_loops(), parse_state(), parse_verdict()
    and VERDICT_RE instead of duplicating them.

    A sibling whose ``METRICS_API`` differs from REQUIRED_METRICS_API (a
    partially vendored metrics/ set) raises MetricsApiMismatch before any
    call into it, instead of a TypeError deep in the queue check.
    """
    path = Path(__file__).resolve().parent / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = getattr(module, "METRICS_API", 1)
    if found != REQUIRED_METRICS_API:
        raise MetricsApiMismatch(
            f"sibling {path.name} has METRICS_API {found}, this trio-check "
            f"requires {REQUIRED_METRICS_API} (mixed metrics/ versions); "
            f"refresh metrics/ as a set (trio_loop.py, trio-metrics.py, "
            f"trio-shadow.py, trio-check.py) from one release"
        )
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
        # v1 open-loop extension: a mailbox with QUEUE.md may have a
        # VERDICT.md consisting only of appended per-slice sections, with no
        # `VERDICT:` first line yet (reserved for the final integration
        # verdict). Without QUEUE.md this relaxation does not apply.
        if (loop_dir / "QUEUE.md").is_file() and OPEN_LOOP_SLICE_VERDICT_RE.match(first):
            return []
        return [
            "VERDICT.md first non-empty line must be `VERDICT: SHIP|ITERATE|BLOCKED|NEEDS_HUMAN` "
            "(case-insensitive, optional `# ` prefix; ITERATE may carry a "
            "`scope=design` or `scope=local:<paths>` suffix); "
            f"got: {first!r}"
        ]
    # Optional scope= suffix: validate its syntax and that it only rides on
    # ITERATE. Trailing prose after the verdict word stays tolerated.
    rest = first[m.end():].strip()
    if rest.startswith("scope="):
        if m.group(1).upper() != "ITERATE":
            return [
                "scope= suffix is only valid on `VERDICT: ITERATE` "
                f"(scope=design or scope=local:<paths>); got: {first!r}"
            ]
        if not SCOPE_RE.match(rest):
            return [
                "invalid scope= suffix on VERDICT.md first line; expected "
                "`scope=design` or `scope=local:<comma-separated-paths>`; "
                f"got: {rest!r}"
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


def _plan_slices(loop_dir: Path, tm) -> list[dict] | None:
    """PLAN.md's parsed `slices:` block, or None when PLAN.md is missing,
    unreadable, or its `slices:` block does not parse (tm.parse_slices_block
    is already the lenient wrapper for the last case)."""
    plan_path = loop_dir / "PLAN.md"
    if not plan_path.is_file():
        return None
    try:
        plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return tm.parse_slices_block(plan_text)


def _fault_scope_errors(fid: str, scope) -> list[str]:
    """Validate one parsed fault `scope` (MAILBOX-SCHEMA.md `faults:`).

    The parser normalizes every accepted spelling (bracket/block list,
    plain `local:<paths>`, plain `design`, bare paths) to a list of path
    strings, `["design"]` for a design-scoped fault. Violations: empty,
    not a list, `design` mixed with paths, or an item that still looks like
    the VERDICT.md suffix (`scope=...`) or contains whitespace-only text.
    """
    if not isinstance(scope, list) or not scope:
        return [f"QUEUE.md faults: {fid!r} has an empty `scope:`"]
    errs: list[str] = []
    lowered = [str(item).strip().lower() for item in scope]
    if "design" in lowered and len(scope) > 1:
        errs.append(
            f"QUEUE.md faults: {fid!r} `scope:` mixes `design` with paths; "
            "use either `design` or `local:<paths>`"
        )
    for item in scope:
        text = str(item).strip()
        if not text:
            errs.append(f"QUEUE.md faults: {fid!r} `scope:` has a blank item")
        elif text.lower().startswith("scope="):
            errs.append(
                f"QUEUE.md faults: {fid!r} `scope:` item {text!r} uses the "
                "VERDICT.md `scope=` suffix; write `scope: local:<paths>` "
                "or `scope: design`"
            )
    return errs


def check_queue(loop_dir: Path, tm, slices: list[dict] | None) -> list[str]:
    """Validate QUEUE.md (v1 open-loop extension); [] when QUEUE.md is absent.

    `slices` is the loop's PLAN.md `slices:` block already parsed by the
    caller (see `_plan_slices`) — None means PLAN.md is missing or its
    slices block does not parse. Every parse error the parser collects
    (one per malformed entry — see parse_retired/parse_faults `errors=`)
    is a violation, and the entries that did parse are still validated,
    so one bad entry neither hides the others nor passes silently. See
    MAILBOX-SCHEMA.md "v1 open-loop extension (optional)".
    """
    queue_path = loop_dir / "QUEUE.md"
    if not queue_path.is_file():
        return []
    try:
        queue_text = queue_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"QUEUE.md unreadable: {exc}"]

    errors: list[str] = []

    retired: list[dict] = []
    parse_errors: list[str] = []
    retired_lines = tm.find_queue_block(queue_text, "retired", errors=parse_errors)
    if retired_lines is not None:
        retired = tm.parse_retired(retired_lines, errors=parse_errors)
    errors.extend(f"QUEUE.md `retired:` block: {e}" for e in parse_errors)

    faults: list[dict] = []
    parse_errors = []
    faults_lines = tm.find_queue_block(queue_text, "faults", errors=parse_errors)
    if faults_lines is not None:
        faults = tm.parse_faults(faults_lines, errors=parse_errors)
    errors.extend(f"QUEUE.md `faults:` block: {e}" for e in parse_errors)

    known_ids = {sl["id"] for sl in slices} if slices is not None else None
    # r15: `repo:` of a retired entry (omitted = home) must name the repo
    # its slice belongs to (a declared PLAN.md `repos:` name, or home).
    declared = tm.read_repos(loop_dir)
    repo_names = {r["name"] for r in declared["repos"]}
    slice_repos = (
        {sl["id"]: tm.slice_repo_name(sl, repo_names) for sl in slices}
        if slices is not None and repo_names
        else {}
    )
    if known_ids is None:
        errors.append(
            "QUEUE.md is present but PLAN.md is missing or its `slices:` "
            "block does not parse; cannot validate `retired:` slice references"
        )

    seen_retired_pairs: set[tuple[str, str]] = set()
    for entry in retired:
        slice_id = entry.get("slice", "")
        sha = entry.get("sha", "")
        if not SHA_RE.match(sha):
            errors.append(
                f"QUEUE.md retired: entry for slice {slice_id!r} has an "
                f"invalid sha (must be 40 lowercase hex chars): {sha!r}"
            )
        if not str(entry.get("at", "")).strip():
            errors.append(
                f"QUEUE.md retired: entry for slice {slice_id!r} is missing `at:`"
            )
        if known_ids is not None and slice_id not in known_ids:
            errors.append(
                f"QUEUE.md retired: entry references slice {slice_id!r}, not "
                "found in PLAN.md `slices:` block"
            )
        repo = str(entry.get("repo") or HOME_REPO).strip()
        if repo in (".", "./", ""):
            repo = HOME_REPO  # the home spellings, as the driver reads them
        if repo != HOME_REPO and repo not in repo_names:
            errors.append(
                f"QUEUE.md retired: entry for slice {slice_id!r} has repo "
                f"{repo!r}, not declared in PLAN.md `repos:`"
            )
        elif slice_id in slice_repos and slice_repos[slice_id] not in (None, repo):
            errors.append(
                f"QUEUE.md retired: entry for slice {slice_id!r} has repo "
                f"{repo!r} but PLAN.md puts that slice in repo "
                f"{slice_repos[slice_id]!r}"
            )
        # A repeated slice id is legal (a post-retirement fix appends a new
        # entry — MAILBOX-SCHEMA.md "v1 open-loop extension"); only a
        # repeated (slice, sha) pair is a violation.
        pair = (slice_id, sha)
        if pair in seen_retired_pairs:
            errors.append(
                f"QUEUE.md retired: duplicate entry for slice {slice_id!r} "
                f"at sha {sha!r} — each (slice, sha) pair must be unique"
            )
        else:
            seen_retired_pairs.add(pair)

    seen_fault_ids: set[str] = set()
    for entry in faults:
        fid = entry.get("id", "")
        if not FAULT_ID_RE.match(fid):
            errors.append(f"QUEUE.md faults: id {fid!r} must match `f<N>`")
        elif fid in seen_fault_ids:
            errors.append(f"QUEUE.md faults: duplicate fault id {fid!r}")
        seen_fault_ids.add(fid)
        status = entry.get("status", "")
        if status not in VALID_FAULT_STATUSES:
            errors.append(
                f"QUEUE.md faults: {fid!r} has status {status!r}, expected "
                f"one of {', '.join(VALID_FAULT_STATUSES)}"
            )
        errors.extend(_fault_scope_errors(fid, entry.get("scope")))
        if not str(entry.get("reason", "")).strip():
            errors.append(f"QUEUE.md faults: {fid!r} is missing `reason:`")
        # A fault's `slice` not existing in PLAN.md is advisory only — the
        # frozen schema requires the retired: -> PLAN.md reference, not this
        # one — so it never lands in `errors` (see queue_info_lines).

    return errors


def queue_info_lines(loop_dir: Path, tm, slices: list[dict] | None) -> list[str]:
    """Informational QUEUE.md summary for inspect_loop's `info` list.

    Only emitted when QUEUE.md exists. One short counts line, plus one
    advisory line per fault whose `slice` is not a known PLAN.md slice id
    (informational only — see check_queue).
    """
    if not (loop_dir / "QUEUE.md").is_file():
        return []
    queue = tm.read_queue(loop_dir)
    retired, faults = queue["retired"], queue["faults"]
    open_faults = sum(1 for f in faults if f.get("status") == "open")
    lines = [f"queue: {len(retired)} retired, {open_faults} open fault(s)"]
    known_ids = {sl["id"] for sl in slices} if slices is not None else None
    if known_ids is not None:
        for f in faults:
            if f.get("slice") not in known_ids:
                lines.append(
                    f"QUEUE.md faults: {f.get('id')!r} references slice "
                    f"{f.get('slice')!r}, not found in PLAN.md `slices:` "
                    "block (advisory)"
                )
    return lines


# --- r15: slices stay inside their repo ------------------------------------
# MAILBOX-SCHEMA.md "Declared repos (r15)" and "Repo scope (r15 guard)".
# Worktrees, retire shas, the whole-tree gate and the eval pin resolve in the
# slice's repo: the mailbox repo (`home`, the git repo containing
# loop/<mailbox>/) unless PLAN.md declares `repos:` and the slice names one
# with `repo: <name>`. A slice whose `writes:` (or whose builder brief's
# targeted-check `cd`) leaves its repo -- an absolute path elsewhere, a `..`
# escape, a nested clone with its own `.git` that no `repos:` entry
# declares, or a second declared repo -- is refused instead of limping
# (silent Lead take-over, empty aggregate merges, unresolvable retire shas).
# The `repos:`/`full_check:` parsers live in trio-metrics.py (METRICS_API
# 5); omnigent/trioctl loads this file from its own release to enforce it.

HOME_REPO = "home"
# `## Targeted check`, `### Targeted checks`, `## Targeted check (backend)`,
# or a bold `**Targeted check:**` label line (eval-r15a F3). Every such
# section is scanned, up to the next markdown heading.
TARGETED_CHECK_HEADING_RE = re.compile(
    r"^(?:#{2,6}\s+targeted\s+checks?\b.*|\*\*targeted\s+checks?\b[^*]*\*\*.*)$",
    re.IGNORECASE,
)
# eval-r15 N8: label (not heading) spellings -- `__Targeted check__` and a
# plain `Targeted check:` line (optionally a list item). A label line's own
# text after the label is part of its section (`Targeted check: `cd x``).
TARGETED_CHECK_LABEL_RE = re.compile(
    r"^(?:[-*+]\s+)?(?:\*\*|__)?targeted\s+checks?(?![a-z0-9])[^:*_\n]*"
    r"(?::(?:\*\*|__)?|(?:\*\*|__):?)(?P<rest>.*)$",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^#{1,6}\s")
_ARG = r"""("[^"]*"|'[^']*'|[^\s;&|)`'"]+)"""
_SEP = r"""(?:^|&&|\|\||;|\(|`|\s)"""
# cwd-changing commands: `cd <dir>` / `pushd <dir>`, with `cd`'s own
# options and an end-of-options `--` skipped (eval-r15 N8: `cd -- <dir>`).
CD_RE = re.compile(
    _SEP + r"(?:cd|pushd)\s+(?:-[LPe@]+\s+)*(?:--\s+)?" + _ARG
)
# Commands that run in a named directory without changing the shell's cwd:
# `git -C <dir>`, `make -C <dir>`, `env -C <dir>`, `npm -C <dir>`,
# `pnpm -C|--dir <dir>`, `poetry -C <dir>`,
# `--rootdir/--prefix/--cwd/--directory/--chdir[= ]<dir>` (`uv run
# --directory`, `env --chdir`; eval-r15 N8).
TARGET_DIR_RE = re.compile(
    _SEP + r"(?:(?:git|make|env|npm|pnpm|poetry)\s+-C(?:=|\s+)" + _ARG
    + r"|pnpm\s+--dir(?:=|\s+)" + _ARG
    + r"|--(?:rootdir|prefix|cwd|directory|chdir)(?:=|\s+)" + _ARG + ")"
)
# A shell comment: ` # ...` to end of line (never inside the command).
COMMENT_RE = re.compile(r"(?:^|\s)#(?:\s|$).*$")
GLOB_CHARS = frozenset("*?[")


def repo_scope_message(slice_id: str, path) -> str:
    """The exact r15 refusal line for one offending slice."""
    return (
        f"slice {slice_id} writes outside the mailbox repo ({path}); declare "
        "it in PLAN.md repos: (r15) or move the mailbox into that repo"
    )


def mailbox_dir_message(slice_id: str, path, loop_dir) -> str:
    """A home slice writes under the mailbox directory (eval-r16rc G1)."""
    return (
        f"slice {slice_id} writes under the mailbox directory ({path}); files "
        f"under {loop_dir} (evidence, receipts, results, scripts) are Lead work "
        "the Lead writes and commits itself, never a builder slice: drop the "
        "slice from PLAN.md slices: (the mailbox repo is always `home`)"
    )


def _home_write_base(sl: dict, loop_dir: Path, root: Path) -> Path | None:
    """Single-repo-mode base a slice's `writes:` resolve against, when that
    slice is (or resolves into) the home repo, else None.

    `.`, `./`, empty and `home` are conventional home aliases (matched the
    same way multi-repo mode's `slice_repo_name` treats them) and resolve
    to *root* directly, never as filesystem paths. Any other value is a
    pre-r15 path-like `repo:`, resolved the same way `_slice_offending_path`
    already does it: relative to the mailbox dir first, falling back to the
    repo root when that is not a directory (trio-shadow's own resolution).
    home applies when that path lands at the repo root itself or at/under
    the mailbox dir (eval-r16rc-b L4): an undeclared `repo:` can't spell
    home, the repo root, or a mailbox subdirectory as a relative path to
    smuggle a home slice's writes: out of the G1 guard. A resolved path
    that does not exist as a directory, or that is itself a nested git
    clone (its own `.git`), is left to the existing r15 escape/cross-repo
    checks (`_slice_offending_path`), which already refuse it under their
    own message -- an undeclared clone nested in the mailbox dir is a
    scope violation, not Lead-owned mailbox content.
    """
    repo = str(sl.get("repo") or ".").strip()
    if repo in ("", ".", "./", HOME_REPO):
        return root
    base = _resolve_under(loop_dir, repo)
    alt = _resolve_under(root, repo)
    if not base.is_dir() and alt.is_dir():
        base = alt
    if base == root:
        return root
    if base.is_dir() and (base == loop_dir or loop_dir in base.parents) \
            and not (base / ".git").exists():
        return base
    return None


def _under_mailbox(path: Path, loop_dir: Path) -> bool:
    """*path* is the mailbox dir, under it, or an ancestor of it.

    The G1 guard refuses a home write reaching the mailbox from below
    (`loop/x/a.py`) exactly as it refuses one covering it from above
    (`loop`, `.`, an absolute repo-root path, or a glob matching an
    ancestor directory of the mailbox -- eval-r16rc-b L5): either way the
    write is Lead-owned mailbox content, not a builder slice's.
    """
    return (
        path == loop_dir
        or loop_dir in path.parents
        or path in loop_dir.parents
    )


def _mailbox_dir_write(
    sl: dict, loop_dir: Path, root: Path, named: dict[str, Path], tm
) -> Path | None:
    """First `writes:` path of a home slice at, above, or under *loop_dir*,
    or None."""
    if loop_dir == root:
        return None
    if named:
        if tm.slice_repo_name(sl, named) != HOME_REPO:
            return None
        base = root
    else:
        base = _home_write_base(sl, loop_dir, root)
        if base is None:
            return None
    if base == loop_dir or loop_dir in base.parents:
        # The slice's repo itself resolves into the mailbox dir (a
        # path-like `repo:` such as `scripts`, L4): every write of this
        # slice lands under the mailbox no matter what it names.
        return base
    for write in sl.get("writes") or []:
        write = str(write).strip()
        if not write or write.startswith("api:"):
            continue
        for target in _expand_write(base, write):
            if _under_mailbox(target, loop_dir):
                return target
    return None


def repo_escape_message(slice_id: str, repo: str, path) -> str:
    """A slice of declared repo *repo* writes (or cds) outside that repo."""
    return (
        f"slice {slice_id} writes outside its repo {repo} ({path}); its "
        "writes: and targeted-check cds are relative to that repo's root (r15)"
    )


def cross_repo_message(slice_id: str, repo: str, other: str, path) -> str:
    """A slice touches a second repo (one repo per slice)."""
    return (
        f"slice {slice_id} of repo {repo} touches repo {other} ({path}); one "
        "repo per slice: split it (depends_on: across repos is fine) (r15)"
    )


def main_checkout_message(slice_id: str, repo: str, path) -> str:
    """A declared-repo slice's targeted check names the repo's main checkout
    (an absolute path) instead of running in the builder's worktree."""
    return (
        f"slice {slice_id} targeted check cds into the main checkout of repo "
        f"{repo} ({path}); the builder runs it from its worktree root: cd to "
        "a path relative to the repo root, never an absolute one (r15)"
    )


def full_check_scope_message(repo: str, path) -> str:
    """A per-repo `full_check:` command leaves its repo."""
    return (
        f"PLAN.md full_check: command of repo {repo} runs outside that repo "
        f"({path}); it runs from the repo's root (r15)"
    )


def plan_scope_message(path) -> str:
    """The r15 refusal line for a PLAN.md-level targeted/full check."""
    return (
        f"PLAN.md targeted/full check runs outside the mailbox repo ({path}); "
        "declare it in PLAN.md repos: (r15) or move the mailbox into that repo"
    )


def mailbox_repo_root(loop_dir: Path) -> Path | None:
    """The git repo containing *loop_dir*: its nearest ancestor (or itself)
    holding a `.git` entry (directory, or file for a worktree/submodule)."""
    here = Path(loop_dir).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def parse_repos_block(plan_text: str, root: Path | None) -> tuple[list[dict], list[str]]:
    """PLAN.md `repos:` block (see trio-metrics.parse_repos_block)."""
    return load_trio_metrics().parse_repos_block(plan_text, root)


def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _outside_repo(path: Path, root: Path, declared: list[Path]) -> bool:
    """True when *path* leaves the mailbox repo -- not under *root*, or under
    a nested git repo inside it -- and no declared repo covers it."""
    if any(_within(path, d) for d in declared):
        return False
    if not _within(path, root):
        return True
    for anc in (path, *path.parents):
        if anc == root:
            return False
        if (anc / ".git").exists():
            return True
    return False


def targeted_check_lines(text: str) -> list[str]:
    """Lines of EVERY targeted-check section of *text* (heading variants per
    TARGETED_CHECK_HEADING_RE), each up to the next markdown heading."""
    section: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if TARGETED_CHECK_HEADING_RE.match(stripped):
            inside = True
            label = TARGETED_CHECK_LABEL_RE.match(stripped)
            if label and label.group("rest").strip():
                section.append(label.group("rest"))
            continue
        label = TARGETED_CHECK_LABEL_RE.match(stripped)
        if label:
            inside = True
            if label.group("rest").strip():
                section.append(label.group("rest"))
            continue
        if inside and HEADING_RE.match(stripped):
            inside = False
            continue
        if inside:
            section.append(line)
    return section


def _unquote_arg(arg: str) -> str:
    if arg[:1] in ("'", '"') and arg[-1:] == arg[:1]:
        return arg[1:-1]
    return arg


def command_dirs(lines: list[str]) -> list[tuple[str, str]]:
    """(kind, dir) of every directory a check command runs in, in order.

    kind `cd` changes the working directory of the rest of the command
    (`cd`, `pushd`); kind `target` names a directory without changing it
    (`git`/`make`/`env`/`npm`/`pnpm`/`poetry` `-C`, `pnpm --dir`, `--rootdir`,
    `--prefix`, `--cwd`, `--directory`, `--chdir`). Shell comments
    are ignored; inline-code backticks are separators.
    """
    found: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        line = COMMENT_RE.sub("", line)
        for m in CD_RE.finditer(line):
            found.append((index * 10_000 + m.start(), "cd", _unquote_arg(m.group(1))))
        for m in TARGET_DIR_RE.finditer(line):
            arg = next(g for g in m.groups() if g is not None)
            found.append((index * 10_000 + m.start(), "target", _unquote_arg(arg)))
    return [(kind, arg) for _pos, kind, arg in sorted(found)]


def targeted_check_cds(text: str) -> list[str]:
    """Every `cd`/`pushd` argument in *text*'s targeted-check sections."""
    return [arg for kind, arg in command_dirs(targeted_check_lines(text)) if kind == "cd"]


def _safe_resolve(path: Path) -> Path:
    """``path.resolve()``, or the absolute unresolved path on a symlink loop
    (eval-r15a F7: never a traceback; the caller treats it as a path)."""
    try:
        return path.resolve()
    except (RuntimeError, OSError):
        return Path(os.path.abspath(path))


def _resolve_under(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return _safe_resolve(p if p.is_absolute() else base / p)


def _expand_write(base: Path, value: str) -> list[Path]:
    """*value* resolved under *base*; a glob (`*`, `?`, `[`) also yields
    every existing match of each of its path prefixes (eval-r15a F5), so
    `app-*/x.py` or `*/app/x.py` reaches a nested clone it could name."""
    target = _resolve_under(base, value)
    if not GLOB_CHARS & set(value):
        return [target]
    out = [target]
    parts = Path(value).expanduser().parts
    anchor = Path(value).expanduser() if Path(value).expanduser().is_absolute() else None
    for depth in range(1, len(parts) + 1):
        pattern = Path(*parts[:depth])
        full = pattern if anchor is not None else base / pattern
        for match in glob.glob(str(full)):
            out.append(_safe_resolve(Path(match)))
    return out


def _command_offending_path(
    lines: list[str], cwd: Path, root: Path, declared: list[Path]
) -> Path | None:
    """First directory a check command runs in outside the mailbox repo."""
    for kind, arg in command_dirs(lines):
        if arg == "-" or "$" in arg or "`" in arg:
            continue
        target = _resolve_under(cwd, arg)
        if _outside_repo(target, root, declared):
            return target
        if kind == "cd":
            cwd = target
    return None


def _slice_offending_path(
    sl: dict, loop_dir: Path, root: Path, repos: dict[str, Path], brief: str | None
) -> Path | None:
    declared = list(repos.values())
    repo = str(sl.get("repo") or ".").strip()
    if repo in repos:
        base = repos[repo]
    elif repo in (".", HOME_REPO):
        base = root
    else:
        # Pre-r15 `repo:` is a path relative to the mailbox dir (as
        # trio-shadow resolves it); fall back to the repo root. A value
        # that names no directory is an undeclared repo, never the home.
        base = _resolve_under(loop_dir, repo)
        alt = _resolve_under(root, repo)
        if not base.is_dir() and alt.is_dir():
            base = alt
        if not base.is_dir() or _outside_repo(base, root, declared):
            return base
    if _outside_repo(base, root, declared):
        return base
    for write in sl.get("writes") or []:
        write = str(write).strip()
        if not write or write.startswith("api:"):
            continue
        for target in _expand_write(base, write):
            if _outside_repo(target, root, declared):
                return target
    if brief:
        return _command_offending_path(
            targeted_check_lines(brief), root, root, declared
        )
    return None


def repo_owner(path: Path, root: Path | None, by_path: dict[Path, str]) -> str | None:
    """The repo *path* belongs to: the nearest ancestor that is a declared
    repo root (its name) or the mailbox repo root (`home`). None when an
    undeclared nested git repo, or nothing known, contains it."""
    for anc in (path, *path.parents):
        if anc in by_path:
            return by_path[anc]
        if root is not None and anc == root:
            return HOME_REPO
        try:
            if (anc / ".git").exists():
                return None
        except OSError:
            return None
    return None


def _declared_slice_problem(
    sl: dict, root: Path, named: dict[str, Path], brief: str | None, tm
) -> str | None:
    """Multi-repo mode: the refusal line for one slice, or None.

    The slice's repo is `repo:` (a declared name, or `home`); every
    `writes:` path resolves against that repo's root and must stay in it;
    its targeted check starts at that root (the builder's worktree root)
    and every directory it names must stay in the same repo -- relative,
    for a declared repo, since an absolute path is the repo's main
    checkout, not the builder's worktree.
    """
    slice_id = sl["id"]
    name = tm.slice_repo_name(sl, named)
    if name is None:
        return (
            f"slice {slice_id} repo: {str(sl.get('repo')).strip()!r} is not "
            "declared in PLAN.md repos: (declared: "
            + ", ".join([HOME_REPO, *named]) + ") (r15)"
        )
    base = root if name == HOME_REPO else named[name]
    by_path = {path: repo for repo, path in named.items()}

    def placed(target: Path) -> str | None:
        owner = repo_owner(target, root, by_path)
        if owner == name:
            return None
        if owner is not None:
            return cross_repo_message(slice_id, name, owner, target)
        if name == HOME_REPO:
            return repo_scope_message(slice_id, target)
        return repo_escape_message(slice_id, name, target)

    for write in sl.get("writes") or []:
        write = str(write).strip()
        if not write or write.startswith("api:"):
            continue
        for target in _expand_write(base, write):
            problem = placed(target)
            if problem:
                return problem
    if brief:
        cwd = base
        for kind, arg in command_dirs(targeted_check_lines(brief)):
            if arg == "-" or "$" in arg or "`" in arg:
                continue
            target = _resolve_under(cwd, arg)
            problem = placed(target)
            if problem:
                return problem
            if name != HOME_REPO and Path(arg).expanduser().is_absolute():
                return main_checkout_message(slice_id, name, target)
            if kind == "cd":
                cwd = target
    return None


def _declared_plan_problems(
    plan_text: str, root: Path, named: dict[str, Path], tm
) -> list[str]:
    """Multi-repo mode: PLAN-level checks. The `full_check:` mapping keys
    must be declared repos, and each repo's command must stay in that repo
    (run from its root); PLAN.md's own targeted-check sections may name
    home or any declared repo, never an undeclared place."""
    problems: list[str] = []
    by_path = {path: repo for repo, path in named.items()}
    checks, errors = tm.parse_full_check(plan_text)
    problems.extend(errors)
    for key, command in checks.items():
        if key != HOME_REPO and key not in named:
            problems.append(
                f"PLAN.md full_check: repo {key!r} is not declared in "
                "PLAN.md repos: (declared: "
                + ", ".join([HOME_REPO, *named]) + ")"
            )
            continue
        cwd = root if key == HOME_REPO else named[key]
        for kind, arg in command_dirs([command]):
            if arg == "-" or "$" in arg or "`" in arg:
                continue
            target = _resolve_under(cwd, arg)
            if repo_owner(target, root, by_path) != key:
                problems.append(full_check_scope_message(key, target))
                break
            if kind == "cd":
                cwd = target
    cwd = root
    for kind, arg in command_dirs(targeted_check_lines(plan_text)):
        if arg == "-" or "$" in arg or "`" in arg:
            continue
        target = _resolve_under(cwd, arg)
        if repo_owner(target, root, by_path) is None:
            problems.append(plan_scope_message(target))
            break
        if kind == "cd":
            cwd = target
    return problems


def _full_check_lines(plan_text: str) -> list[str]:
    """The PLAN.md `full_check:` value lines (key line + indented follow-ups).

    A block mapping continues across blank lines while the next non-blank
    line is another indented `<repo>: <cmd>` item (eval-r15 N4), matching
    trio-metrics' ``_full_check_section``.
    """
    item = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\s*:\s+\S")
    lines = plan_text.splitlines()
    for index, raw in enumerate(lines):
        m = re.match(r"^full_check\s*:\s*(.*)$", raw)
        if not m:
            continue
        out = [m.group(1)]
        follow_lines = lines[index + 1:]
        for pos, follow in enumerate(follow_lines):
            if not follow.strip():
                nxt = next((ln for ln in follow_lines[pos + 1:] if ln.strip()), "")
                if (
                    not out[0].strip() and len(out) > 1 and nxt[:1].isspace()
                    and item.match(nxt.strip())
                    and all(item.match(ln.strip()) for ln in out[1:])
                ):
                    continue
                break
            if not follow[:1].isspace():
                break
            out.append(follow)
        return out
    return []


def _declared_base_problems(repos: list[dict]) -> list[str]:
    """eval-r15 N9: a declared `base:` must be an existing branch of its repo
    (a dispatch refuses it anyway; refuse it before any dispatch)."""
    problems: list[str] = []
    for repo in repos:
        base = repo.get("base")
        if not base:
            continue
        try:
            found = subprocess.run(
                ["git", "-C", str(repo["path"]), "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{base}^{{commit}}"],
                capture_output=True, text=True, check=False,
            ).returncode == 0
        except OSError:
            found = False
        if not found:
            problems.append(
                f"PLAN.md repos: {repo['name']!r}: base: {base!r} is not a branch "
                f"of {repo['path']} (r15)"
            )
    return problems


def repo_scope_refusals(
    loop_dir: Path,
    tm,
    *,
    slice_id: str | None = None,
    brief_text: str | None = None,
) -> list[str]:
    """r15: one refusal line per offending slice, plus `repos:` errors.

    Checks every slice of PLAN.md's `slices:` block (only *slice_id* when
    given) and the directories its builder brief's targeted check names
    (*brief_text*, else `<mailbox>/briefs/<id>.md`). Without a `repos:`
    block (single-repo mode) this is the r15 guard: `repo:`/`writes:`
    resolve against the mailbox repo root and the brief's cds from it, and
    PLAN.md's own targeted/full checks are scanned too. With one, the block
    and the per-repo `full_check:` are validated and each slice must stay
    inside its own repo (`_declared_slice_problem`). An unparseable slices
    block is refused (fail closed). [] means nothing to refuse.
    """
    loop_dir = Path(loop_dir).resolve()
    plan_path = loop_dir / "PLAN.md"
    try:
        plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    root = mailbox_repo_root(loop_dir)
    repos, problems = tm.parse_repos_block(plan_text, root)
    if root is None:
        return problems
    named = {r["name"]: r["path"] for r in repos}
    declared = list(named.values())
    multi = bool(repos)
    try:
        block = tm.find_slices_block(plan_text)
    except tm.SliceParseError:
        block = None  # no slices block yet: nothing to check
    slices: list[dict] = []
    if block is not None:
        try:
            slices = tm.parse_slices(block)
        except tm.SliceParseError as exc:
            # eval-r15a F4: fail closed -- an unparseable block could hide
            # any `repo:`/`writes:`.
            problems.append(
                f"PLAN.md slices block does not parse ({exc}); the repo-scope "
                "guard cannot check it (r15)"
            )
    if slice_id is None and multi:
        problems.extend(_declared_plan_problems(plan_text, root, named, tm))
        problems.extend(_declared_base_problems(repos))
    elif slice_id is None:
        # eval-r15a F6: PLAN-level checks run from the mailbox repo root.
        path = _command_offending_path(
            targeted_check_lines(plan_text) + _full_check_lines(plan_text),
            root, root, declared,
        )
        if path is not None:
            problems.append(plan_scope_message(path))
    elif brief_text is not None and slice_id not in {sl["id"] for sl in slices}:
        # eval-r15a F3: a slice id PLAN.md does not know still has its
        # brief checked (`run builder --isolate --worker-slice <ghost>`).
        path = _command_offending_path(
            targeted_check_lines(brief_text), root, root, declared
        )
        if path is not None:
            problems.append(repo_scope_message(slice_id, path))
    for sl in slices:
        if slice_id is not None and sl["id"] != slice_id:
            continue
        inside = _mailbox_dir_write(sl, loop_dir, root, named, tm)
        if inside is not None:
            problems.append(mailbox_dir_message(sl["id"], inside, loop_dir))
            continue
        brief = brief_text if slice_id is not None else None
        if brief is None:
            brief_path = loop_dir / "briefs" / f"{sl['id']}.md"
            try:
                brief = brief_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                brief = None
        if multi:
            problem = _declared_slice_problem(sl, root, named, brief, tm)
            if problem:
                problems.append(problem)
            continue
        path = _slice_offending_path(sl, loop_dir, root, named, brief)
        if path is not None:
            problems.append(repo_scope_message(sl["id"], path))
    return problems


# --- r18a quality lints (advisory; `--strict-quality` makes REJECT a violation)
# `accepts:` grammar: `<input/action> -> <observable> | oracle: <kind>`
# (MAILBOX-SCHEMA "`accepts:` grammar"). A REJECT is free text with no
# relation and no oracle (or a banned phrase alone); a WARN is a half-formed
# accept. Also: open-loop mailboxes without `goal_probe:`/`goal_acceptance:`
# and a `full_check:` made only of artifact readers (L5).

ORACLE_KINDS = ("value", "property", "diff", "refusal", "static", "rerun")
_ORACLE_TAG_RE = re.compile(r"(?:^|\||\s)oracle\s*:\s*([A-Za-z_-]*)", re.IGNORECASE)
# `input -> observable`: an arrow, or an (in)equality / comparison between
# two sides (`==`, `=`, `!=`, `<=`, `>=`, `<`, `>`).
_RELATION_RE = re.compile(
    r"->|→|=>|==|!=|<=|>=|(?<![<>!=:])=(?!=)|(?<![-=<])>(?![=>])|\s<\s"
)
# A relation word (C6, eval-r18a lint table): "returns None", "is not_found",
# "equals raw gold", "match build_bridge(...)", "exactly 0", "byte-identical
# to cd2cc8e", "232/.../4 unchanged", "close 7 vs 8". It counts as a relation
# only together with an observable (below), so "the page is fine" stays free
# text; `->` is then optional (a WARN for the missing oracle tag, not REJECT).
_RELATION_WORD_RE = re.compile(
    r"\b(?:is|are|was|equals?|returns?|match(?:es)?|exactly|identical|unchanged"
    r"|echo(?:es)?|prints?|exits?|yields?|gives?|responds?|vs\.?|raises?|renders?|shows?"
    r"|emits?|sends?|posts?|maps?|records?|stores?|refuses?|rejects?)\b",
    re.IGNORECASE,
)
# An observable: a number, a quoted / backticked / bracketed literal, an
# identifier with `_`, `()` or inner capitals, an ALLCAPS token (3+), or
# None/null/true/false.
_OBSERVABLE_RE = re.compile(
    r"\d|['\"`{\[]|\b[A-Za-z]\w*_\w+|\b\w+\(|\b[a-z]+[A-Z]\w*|\b[A-Z]{3,}\b"
    r"|\b(?:None|null|nil|true|false|True|False|NaN)\b"
)
# A bare HTTP status code is itself an observable relation ("401 {error:..}
# when missing", "upstream 429 is rate_limited").
_HTTP_STATUS_RE = re.compile(
    r"(?<![\w.-])(?:200|201|202|204|301|302|303|304|307|308|400|401|403|404|405|406|409"
    r"|410|412|413|415|422|423|429|500|501|502|503|504)(?![\w.-])"
)
# Static config artifacts (compose, nginx, Dockerfile, systemd, tsconfig,
# yaml/toml/ini): an accept or a test ABOUT their content is `static-config`
# -- not a tautology, but it must be paired with one runtime check (parse
# it: yaml load / `nginx -t` / `docker compose config`, or a behavioural
# accept/test on the same slice / file).
STATIC_CONFIG_RE = re.compile(
    r"(?:^|[/\s(`'\"])(?:(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml|Dockerfile[\w.-]*|[\w.-]+\.service"
    r"|[\w.-]+\.timer|nginx[\w.-]*\.conf|[\w.-]*\.conf|tsconfig[\w.-]*\.json|[\w.-]+\.ya?ml"
    r"|[\w.-]+\.toml|[\w.-]+\.ini|[\w.-]+\.tf|requirements[\w.-]*\.txt|package\.json"
    r"|runtime-deps|Caddyfile|\.env(?:\.[\w-]+)?)(?=$|[\s)`'\",;:])"
    r"|\b(?:docker[- ]compose|compose file|nginx|Dockerfile|systemd(?: units?)?|tsconfig)\b",
    re.IGNORECASE,
)


def accept_relation(head: str) -> bool:
    """True when an accept (sans oracle tag) states a checkable relation."""
    if _RELATION_RE.search(head) or _HTTP_STATUS_RE.search(head):
        return True
    return bool(_RELATION_WORD_RE.search(head) and _OBSERVABLE_RE.search(head))


_BANNED_ACCEPT_RE = re.compile(
    r"^\s*(?:(?:all|the|existing|other)\s+)*(?:tests?|suite|checks?)?\s*"
    r"(?:pass(?:es)?|works?|exists?|is\s+documented|documented|stays?\s+green|"
    r"(?:are|is|stay|stays|remain|remains)\s+green|green)\s*\.?\s*$",
    re.IGNORECASE,
)


def accept_findings(text: str) -> list[tuple[str, str]]:
    """(level, reason) findings for one `accepts:`/`goal_acceptance:` item.

    Levels: REJECT (free text / banned phrase), WARN (half-formed), and
    STATIC (a static-config accept: never a tautology finding on its own;
    `quality_findings` WARNs when a slice has nothing but static accepts).
    """
    item = str(text).strip()
    tag = _ORACLE_TAG_RE.search(item)
    head = item[: tag.start()] if tag else item
    relation = accept_relation(head)
    if _BANNED_ACCEPT_RE.match(head.strip(" |")):
        return [("REJECT", "only says tests pass / works / exists / green; name an input -> observable")]
    static = bool(STATIC_CONFIG_RE.search(head))
    if static and not relation:
        return [("STATIC", "static-config accept (checked by parsing the artifact); pair it with one runtime accept")]
    if not tag and not relation:
        return [("REJECT", "free text without an oracle; write `<input/action> -> <observable> | oracle: <kind>`")]
    out: list[tuple[str, str]] = []
    if static:
        out.append(("STATIC", "static-config accept"))
    if tag:
        kind = tag.group(1).lower()
        if kind not in ORACLE_KINDS:
            out.append(("WARN", f"unknown oracle kind {kind or '(empty)'!r} (one of {', '.join(ORACLE_KINDS)})"))
        if not relation and not static:
            out.append(("WARN", "oracle tag without an `input -> observable`"))
    else:
        out.append(("WARN", "no `| oracle: <kind>` tag"))
    return out


def _plan_text(loop_dir: Path) -> str | None:
    try:
        return (loop_dir / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _goal_acceptance_items(plan_text: str) -> list[str] | None:
    """Items of the plain `goal_acceptance:` line (flow list or indented
    `- ` items), or None when PLAN.md declares none."""
    lines = plan_text.splitlines()
    for index, raw in enumerate(lines):
        m = re.match(r"^\s*(?:-\s+)?goal_acceptance\s*:\s*(.*)$", raw)
        if not m:
            continue
        value = m.group(1).strip()
        if value.startswith("["):
            body = value.strip("[]")
            return [x.strip().strip("\"'") for x in re.split(r"\"\s*,\s*\"", body) if x.strip()]
        items: list[str] = [value] if value else []
        for follow in lines[index + 1:]:
            if not follow.strip():
                if items:
                    break
                continue
            item = re.match(r"^\s+-\s+(.*)$", follow)
            if not item:
                break
            items.append(item.group(1).strip().strip("\"'"))
        return items
    return None


def _has_goal_probe(plan_text: str) -> bool:
    return any(
        re.match(r"^\s*(?:-\s+)?goal_probe\s*:\s*\S", ln)
        for ln in plan_text.splitlines()
    )


_READER_HEAD_RE = re.compile(
    r"^(?:jq|cat|head|tail|ls|test|stat|file|\[|grep|wc|true|echo)\b"
)


def _full_check_commands(plan_text: str) -> list[str]:
    """Each `full_check:` command (per repo for a mapping)."""
    lines = _full_check_lines(plan_text)
    if not lines:
        return []
    first, rest = lines[0].strip(), [ln.strip() for ln in lines[1:] if ln.strip()]
    if first.startswith("{"):
        return [v.strip().strip("\"'") for v in re.findall(r":\s*(\"[^\"]*\"|'[^']*')", first)]
    mapped = [re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*\s*:\s+(.*)$", ln) for ln in rest]
    if not first and rest and all(mapped):
        return [m.group(1).strip().strip("\"'") for m in mapped]
    return [" ".join([first, *rest]).strip()] if (first or rest) else []


def _reader_segment(segment: str, loop_dir: Path, root: Path | None) -> bool | None:
    """True when a command segment only reads an artifact; None = neutral."""
    seg = segment.strip().strip("()").strip()
    seg = re.sub(r"^(?:timeout\s+\S+\s+|env\s+|[A-Z_][A-Z0-9_]*=\S+\s+)+", "", seg)
    if not seg:
        return None
    if re.match(r"^(?:cd|pushd|popd|set)\b", seg):
        return None
    if "--verify-only" in seg or _READER_HEAD_RE.match(seg):
        return True
    for token in re.findall(r"[^\s'\"]+", seg):
        if "/" not in token or token.startswith("-"):
            continue
        cand = Path(token)
        if not cand.is_absolute() and root is not None:
            cand = root / cand
        try:
            cand = Path(os.path.normpath(str(cand)))
        except (TypeError, ValueError):
            continue
        if cand == loop_dir or loop_dir in cand.parents:
            return True
    return False


def full_check_reader_only(plan_text: str, loop_dir: Path) -> list[str]:
    """`full_check:` commands made only of artifact readers (L5 lint)."""
    root = mailbox_repo_root(loop_dir)
    loop_dir = Path(os.path.normpath(str(loop_dir)))
    flagged: list[str] = []
    for cmd in _full_check_commands(plan_text):
        verdicts = [
            _reader_segment(seg, loop_dir, root)
            for seg in re.split(r"&&|\|\||;", cmd)
        ]
        real = [v for v in verdicts if v is not None]
        if real and all(real):
            flagged.append(cmd)
    return flagged


def quality_findings(loop_dir: Path, tm) -> list[tuple[str, str]]:
    """r18a advisory quality findings for one v1 mailbox: (level, message)."""
    plan_text = _plan_text(loop_dir)
    if plan_text is None:
        return []
    out: list[tuple[str, str]] = []
    slices = tm.parse_slices_block(plan_text) or []
    for sl in slices:
        items = list(sl.get("accepts") or [])
        static_only = bool(items)
        for n, item in enumerate(items, 1):
            found = accept_findings(item)
            if not any(level == "STATIC" for level, _ in found):
                static_only = False
            for level, why in found:
                if level != "STATIC":
                    out.append((level, f"slice {sl['id']} accept {n} {_clip(item)!r}: {why}"))
        if static_only:
            out.append(("WARN", f"slice {sl['id']}: static-config accepts only; pair them with one "
                                "runtime accept (parse / start / request the configured service)"))
    goal_items = _goal_acceptance_items(plan_text)
    for n, item in enumerate(goal_items or [], 1):
        for level, why in accept_findings(item):
            if level != "STATIC":
                out.append((level, f"goal_acceptance {n} {_clip(item)!r}: {why}"))
    if (loop_dir / "QUEUE.md").is_file():
        if not _has_goal_probe(plan_text):
            out.append(("WARN", "no `goal_probe:` under `## Verification standard` (open-loop mailbox)"))
        if not goal_items:
            out.append(("WARN", "no `goal_acceptance:` under `## Verification standard` (open-loop mailbox)"))
        for sl in slices:
            code = [w for w in sl.get("writes") or [] if not str(w).startswith("api:")]
            if code and not sl.get("accepts"):
                out.append(("WARN", f"slice {sl['id']} changes code but has no `accepts:`"))
    for flag in mailbox_test_flags(loop_dir, tm):
        out.append(("WARN", f"test looks tautological: {flag}"))
    for cmd in full_check_reader_only(plan_text, loop_dir):
        out.append((
            "WARN",
            f"full_check: {_clip(cmd, 120)!r} only reads artifacts (--verify-only, "
            "JSON/receipt readers or scripts/tests under the mailbox); it is not a "
            "whole-tree check",
        ))
    return out


def _clip(text: str, width: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


# --- r18a L7: deterministic tautology lint over test files (advisory) ------
# Free (no model call). Flags the canonical evaluator's tautology list where
# a static reading can see it; the evaluator adjudicates (false positives:
# legitimate static-property and snapshot tests).

TEST_FILE_RE = re.compile(
    r"(?:^|/)test_[^/]*\.py$|(?:^|/)[^/]*_test\.py$|\.(?:test|spec)\.[cm]?[jt]sx?$"
)
_RECEIPT_DIR_RE = re.compile(r"(?:^|/)(?:results|evidence)(?:/|$)")


def _py_open_handles(tree) -> set[str]:
    """Names bound to `open(...)` (`with open(p) as fh`, `fh = open(p)`)."""
    import ast
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.withitem) and _is_open_call(node.context_expr) \
                and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
        elif isinstance(node, ast.Assign) and _is_open_call(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_open_call(node) -> bool:
    import ast
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "open")


def _py_file_reader_funcs(tree) -> set[str]:
    """Module functions whose return value is a file's text (`_text(p)`)."""
    import ast
    names: set[str] = set()
    handles = _py_open_handles(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and _is_file_read(sub.value, set(), handles):
                    names.add(node.name)
    return names


def _is_file_read(node, readers: set[str], handles: set[str] | frozenset = frozenset()) -> bool:
    """A call returning a file's text: `.read_text()`/`.read_bytes()`, a reader
    helper, or `.read()` on an `open(...)` handle -- never an HTTP response's
    `.read()` / `.decode()` (eval-r18a: response bodies are not file text)."""
    import ast
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "read_bytes"):
        return True
    if isinstance(func, ast.Attribute) and func.attr == "read":
        target = func.value
        return _is_open_call(target) or (isinstance(target, ast.Name) and target.id in handles)
    if isinstance(func, ast.Name) and func.id in readers:
        return True
    return False


def _py_name_constants(tree) -> dict[str, set[str]]:
    """name -> every string constant its assignments build on, followed
    through other names (`RES = ROOT / "results"`; `p = RES / "r.txt"`)."""
    import ast
    direct: dict[str, set[str]] = {}
    refs: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        consts = {x.value for x in ast.walk(node.value)
                  if isinstance(x, ast.Constant) and isinstance(x.value, str)}
        names = {x.id for x in ast.walk(node.value) if isinstance(x, ast.Name)}
        for t in node.targets:
            if isinstance(t, ast.Name):
                direct.setdefault(t.id, set()).update(consts)
                refs.setdefault(t.id, set()).update(names)
    out = {k: set(v) for k, v in direct.items()}
    for _ in range(6):
        changed = False
        for name, via in refs.items():
            for other in via:
                extra = out.get(other, set()) - out[name]
                if extra and other != name:
                    out[name] |= extra
                    changed = True
        if not changed:
            break
    return out


def _expr_constants(node, name_consts: dict[str, set[str]]) -> set[str]:
    import ast
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.add(sub.value)
        elif isinstance(sub, ast.Name):
            found |= name_consts.get(sub.id, set())
    return found


def _static_config_read(node, name_consts: dict[str, set[str]] | None = None) -> bool:
    """The read's path names a static config artifact (compose, nginx, ...)."""
    consts = _expr_constants(node, name_consts or {})
    return any(STATIC_CONFIG_RE.search(" " + c.strip()) for c in consts)


_PY_INERT_CALLS = frozenset({
    "Path", "PurePath", "open", "read", "read_text", "read_bytes", "joinpath", "resolve",
    "absolute", "exists", "is_file", "is_dir", "with_suffix", "with_name", "relative_to",
    "encode", "decode", "strip", "rstrip", "lstrip", "lower", "upper", "splitlines", "split",
    "replace", "format", "join", "len", "str", "int", "float", "bool", "sorted", "list",
    "set", "dict", "tuple", "print", "getenv", "get", "dirname", "abspath", "startswith",
    "endswith", "count", "index", "find", "isinstance", "any", "all", "enumerate", "range",
})


def _first_action_line(nodes) -> int | None:
    """Line of the first call in a scope that DOES something (runs product
    code, a runner, a subprocess): a file read after it is runtime output."""
    import ast
    best = None
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name is None or name in _PY_INERT_CALLS or name.startswith("assert"):
            continue
        line = getattr(node, "lineno", None)
        if line is not None and (best is None or line < best):
            best = line
    return best


def _py_scopes(tree):
    """(scope node, its own statements' nodes) for the module and each
    function; a function's nodes exclude nested function bodies."""
    import ast

    def own(node):
        out = []
        stack = list(ast.iter_child_nodes(node))
        while stack:
            cur = stack.pop()
            out.append(cur)
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.extend(ast.iter_child_nodes(cur))
        return out

    scopes = [(tree, own(tree))]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, own(node)))
    return scopes


_PY_RUNTIME_RE = re.compile(
    r"\bsubprocess\b|\bos\.system\b|\brunpy\b|spec_from_file_location|import_module"
    r"|\burlopen\b|\brequests\.|\bhttpx\.|TestClient\b"
)


def product_modules(paths) -> set[str]:
    """Python product module names a test may import, from changed paths:
    each non-test `.py` stem (hyphens as underscores) and its package dirs."""
    out: set[str] = set()
    for p in paths:
        p = str(p)
        if not p.endswith(".py") or _RECEIPT_DIR_RE.search(p) or TEST_FILE_RE.search(p) \
                or re.search(r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)conftest\.py$", p):
            continue
        parts = Path(p).with_suffix("").parts
        for part in parts:
            name = part.replace("-", "_")
            if name.isidentifier() and name not in ("__init__", "src", "lib"):
                out.add(name)
    return out


def python_test_findings(
    rel: str, text: str, product_modules: set[str] | None = None,
) -> list[tuple[str, str]]:
    """(category, `<rel>:<line> why`) for one Python test file. Categories:
    `tautology` (advisory flag) and `static-config` (string presence on a
    static config artifact: not a tautology; flagged only when the file has
    no runtime check at all)."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    readers = _py_file_reader_funcs(tree)
    handles = _py_open_handles(tree)
    out: list[tuple[str, str]] = []

    def flag(node, why: str, cat: str = "tautology") -> None:
        out.append((cat, f"{rel}:{getattr(node, 'lineno', 0)} {why}"))

    def in_checks(expr) -> list:
        return [expr] if (
            isinstance(expr, ast.Compare) and len(expr.ops) == 1
            and isinstance(expr.ops[0], (ast.In, ast.NotIn))
        ) else []

    name_consts = _py_name_constants(tree)

    def bound(nodes, helpers: set[str] = frozenset()) -> tuple[set[str], set[str], set[str]]:
        """(file-text names, static-config names, other names) assigned here.
        A file read AFTER the scope ran something (a runner, product code)
        is that action's runtime output, not static file text."""
        files: set[str] = set()
        static: set[str] = set()
        other: set[str] = set()
        acted = _first_action_line([n for n in nodes if not (
            isinstance(n, ast.Call) and _is_file_read(n, readers, handles))])
        for node in nodes:
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if _is_file_read(node.value, readers, handles):
                if acted is not None and node.lineno > acted:
                    other.update(names)
                elif _static_config_read(node.value, name_consts):
                    static.update(names)
                else:
                    files.update(names)
            else:
                other.update(names)
        return files, static, other - files - static

    scopes = _py_scopes(tree)
    mod_files, mod_static, _mod_other = bound(scopes[0][1])
    receipts = False
    verify_only = False
    runtime_asserts = 0
    static_hits: list[tuple[object, str]] = []
    def receipt_const(sub) -> bool:
        return isinstance(sub, ast.Constant) and isinstance(sub.value, str) and (
            sub.value.strip("/") in ("results", "evidence")
            or bool(re.search(r"(?:^|/)(?:results|evidence)/", sub.value.strip() + "/")))

    # Receipt detection is path-based (eval-r18a): a file read whose path
    # expression names results/ or evidence/ (directly or via a variable
    # bound to such a path) -- never a "results" JSON key.
    receipt_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "--verify-only" in node.value:
            verify_only = True
        if isinstance(node, ast.Assign) and any(receipt_const(x) for x in ast.walk(node.value)):
            receipt_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    for scope, nodes in scopes:
        files, static, other = bound(nodes)
        if scope is not scopes[0][0]:
            files = files | (mod_files - other - static)
            static = static | (mod_static - other - files)
        for node in nodes:
            if isinstance(node, ast.Call) and _is_file_read(node, readers, handles):
                for sub in ast.walk(node):
                    if receipt_const(sub) or (isinstance(sub, ast.Name) and (
                            sub.id in receipt_names or any(
                                receipt_const(ast.Constant(value=c)) for c in name_consts.get(sub.id, ())))):
                        receipts = True
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
                disj = [c for v in test.values for c in in_checks(v)]
                subjects = [c.comparators[0] for c in disj]
                negative = any(isinstance(c.ops[0], ast.NotIn) for c in disj)
                on_file = any(isinstance(x, ast.Name) and x.id in files for x in subjects)
                if len(disj) >= 2 and (negative or on_file):
                    flag(node, "or-chain of `in` checks (one disjunct may be satisfied by a header or constant)")
                elif len(disj) >= 2 and all(isinstance(x, ast.Name) and x.id in static for x in subjects):
                    static_hits.append((node, "or-chain"))
                else:
                    runtime_asserts += 1
                continue
            checks = in_checks(test)
            if not checks:
                runtime_asserts += 1
            for cmp_ in checks:
                left, right = cmp_.left, cmp_.comparators[0]
                on_file = isinstance(right, ast.Name) and right.id in files
                on_static = isinstance(right, ast.Name) and right.id in static
                if isinstance(left, ast.Constant) and isinstance(left.value, str):
                    if len(left.value) <= 1 or (len(left.value) <= 2 and on_file):
                        flag(node, f"`{left.value!r} in ...` checks a {len(left.value)}-character literal")
                    elif on_file:
                        flag(node, f"string presence {left.value[:40]!r} on file text (`{right.id}`), not behaviour")
                    elif on_static:
                        static_hits.append((node, left.value))
                    else:
                        runtime_asserts += 1
                else:
                    runtime_asserts += 1
            if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) \
                    and test.func.attr in ("is_file", "exists", "is_dir"):
                flag(node, f"presence-only check (`.{test.func.attr}()`)")
    runtime = runtime_asserts > 0 or bool(_PY_RUNTIME_RE.search(text)) or bool(
        re.search(r"\bself\.assert(?!In\b|NotIn\b)\w+\(", text))
    for node, what in static_hits:
        cat = "static-config"
        why = f"static-config: string presence {str(what)[:40]!r} on a config artifact"
        if not runtime:
            cat = "tautology"
            why += " with no runtime check in the file (parse it or run the service)"
        flag(node, why, cat)
    if receipts:
        out.append(("tautology", f"{rel}:1 reads receipts under results/ or evidence/ (a receipt is a claim, not an oracle)"))
    if verify_only:
        out.append(("tautology", f"{rel}:1 runs a `--verify-only` pass-flag reader"))
    if product_modules:
        mods = {m.replace("-", "_") for m in product_modules}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        parts = {seg for name in imported for seg in name.split(".")}
        # A module named in a string (subprocess `-m pkg.mod`, a script path,
        # importlib) or a test that loads / runs / reads the product counts.
        named = {w.replace("-", "_") for w in re.findall(r"[A-Za-z_][\w-]*", " ".join(
            n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)))}
        exercises = bool(_PY_RUNTIME_RE.search(text)) or any(
            isinstance(n, ast.Call) and _is_file_read(n, readers, handles) for n in ast.walk(tree))
        if not (parts | named) & mods and not exercises:
            out.append((
                "tautology",
                f"{rel}:1 imports none of the slice's product modules "
                f"({', '.join(sorted(product_modules))})",
            ))
    return out


def python_test_flags(rel: str, text: str, product_modules: set[str] | None = None) -> list[str]:
    """Advisory tautology flags for one Python test file (`<rel>:<line> why`)."""
    return [msg for cat, msg in python_test_findings(rel, text, product_modules) if cat == "tautology"]


_TS_READFILE_RE = re.compile(r"readFileSync\s*\(")
_TS_TOCONTAIN_RE = re.compile(r"expect\(\s*([\w.$]+)[^;]*?\)\s*(?:\.not)?\.toContain\(\s*(['\"`])(.*?)\2\s*\)")
_TS_FILE_VAR_RE = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*(?::\s*[\w<>\[\]]+\s*)?=\s*(?:await\s+)?"
                             r"(?:[\w.]*\.)?readFileSync\s*\(([^)]*)\)")
_TS_FILE_READ_CALL_RE = re.compile(r"readFileSync\s*\(([^)]*)\)|readFile\s*\(([^)]*)\)")


def ts_test_findings(rel: str, text: str) -> list[tuple[str, str]]:
    """Regex equivalent for TypeScript/JavaScript tests: (category, flag)."""
    out: list[tuple[str, str]] = []
    file_vars: dict[str, bool] = {}          # name -> is a static config file
    for m in _TS_FILE_VAR_RE.finditer(text):
        file_vars[m.group(1)] = bool(STATIC_CONFIG_RE.search(" " + m.group(2)))
    runtime = bool(re.search(r"\b(?:render|screen|fetch|request|supertest|spawn|exec|execFile)\b", text))
    for n, line in enumerate(text.splitlines(), 1):
        for m in _TS_TOCONTAIN_RE.finditer(line):
            subject, literal = m.group(1), m.group(3)
            static = file_vars.get(subject)
            on_file = subject in file_vars or "readFileSync" in subject
            if len(literal) <= 1 or (len(literal) <= 2 and on_file and not static):
                out.append(("tautology", f"{rel}:{n} toContain of a {len(literal)}-character literal"))
            elif on_file and static:
                cat = "static-config" if runtime else "tautology"
                out.append((cat, f"{rel}:{n} static-config: toContain over config text (`{subject}`)"
                                 + ("" if runtime else " with no runtime check in the file")))
            elif on_file:
                out.append(("tautology", f"{rel}:{n} toContain over file text read with readFileSync, not behaviour"))
        for m in _TS_FILE_READ_CALL_RE.finditer(line):
            arg = m.group(1) or m.group(2) or ""
            if re.search(r"(?:^|['\"`/])(?:results|evidence)/", arg):
                out.append(("tautology", f"{rel}:{n} reads a receipt under results/ or evidence/"))
    return out


def ts_test_flags(rel: str, text: str) -> list[str]:
    return [msg for cat, msg in ts_test_findings(rel, text) if cat == "tautology"]


def test_file_findings(rel: str, text: str, product_modules: set[str] | None = None) -> list[tuple[str, str]]:
    if rel.endswith(".py"):
        return python_test_findings(rel, text, product_modules)
    if re.search(r"\.[cm]?[jt]sx?$", rel):
        return ts_test_findings(rel, text)
    return []


def test_file_flags(rel: str, text: str, product_modules: set[str] | None = None) -> list[str]:
    """Tautology flags only (static-config findings are not tautologies)."""
    return [msg for cat, msg in test_file_findings(rel, text, product_modules) if cat == "tautology"]


def empty_tsconfig_flag(root: Path, command: str | None) -> list[str]:
    """`tsc ... -p <dir>` over a tsconfig with `"files": []` and no include."""
    out: list[str] = []
    for m in re.finditer(r"\btsc\b[^&|;]*?-p\s+(\S+)", command or ""):
        cfg = root / m.group(1)
        cfg = cfg / "tsconfig.json" if cfg.is_dir() or not cfg.suffix else cfg
        try:
            data = cfg.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r'"files"\s*:\s*\[\s*\]', data) and '"include"' not in data:
            out.append(f"{cfg.relative_to(root) if root in cfg.parents else cfg}:1 "
                       "typecheck over `files: []` (a no-op, not a build)")
    return out


def mailbox_test_flags(loop_dir: Path, tm) -> list[str]:
    """L7 flags over the mailbox's own test files and the test files the
    slices declare in `writes:` (resolved in the mailbox repo)."""
    root = mailbox_repo_root(loop_dir) or loop_dir
    files: list[Path] = []
    for top, dirs, names in os.walk(loop_dir):
        # Nested repos / worktrees (declared repos under the mailbox) are not
        # mailbox files: their suites would crowd out the mailbox's own tests.
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d not in ("node_modules", "__pycache__")
            and not os.path.lexists(os.path.join(top, d, ".git"))
        )
        for name in sorted(names):
            p = Path(top) / name
            if TEST_FILE_RE.search(p.as_posix()):
                files.append(p)
    plan_text = _plan_text(loop_dir) or ""
    for sl in tm.parse_slices_block(plan_text) or []:
        for write in sl.get("writes") or []:
            w = str(write)
            if w.startswith("api:") or not TEST_FILE_RE.search(w):
                continue
            cand = (root / w)
            if cand.is_file() and cand not in files:
                files.append(cand)
    flags: list[str] = []
    for path in files[:200]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix() if root in path.parents else str(path)
        flags.extend(test_file_flags(rel, text))
    return flags


def check_prompt_sync(root: Path) -> tuple[bool, list[str]]:
    """Run prompts/generate.py --check when the scanned root has a generator.

    The Trio role prompts are single-sourced (prompts/canonical + overlays);
    generated flavor files that drift from the tree fail conformance. Roots
    without prompts/generate.py (e.g. plain loop mailboxes) skip the check.
    Returns (ok, error_lines).
    """
    gen = root / "prompts" / "generate.py"
    if not gen.is_file():
        return True, []
    try:
        proc = subprocess.run(
            [sys.executable, str(gen), "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [f"prompt-sync check could not run: {exc}"]
    if proc.returncode == 0:
        return True, []
    detail = (proc.stdout + proc.stderr).strip().splitlines()
    return False, [
        "generated Trio prompt files are out of sync with "
        "prompts/canonical+overlays (run `python3 prompts/generate.py`):",
        *[f"    {line}" for line in detail[:12]],
    ]


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
    errors.extend(check_queue(loop_dir, tm, _plan_slices(loop_dir, tm)))
    return errors


def inspect_loop(loop_dir: Path, tm) -> dict:
    version, info = classify_version(loop_dir / "STATE.md")
    refusals: list[str] = []
    if version == "v1":
        errors = check_v1(loop_dir, tm)
        info = info + queue_info_lines(loop_dir, tm, _plan_slices(loop_dir, tm))
        refusals = repo_scope_refusals(loop_dir, tm)
    else:
        errors = []
        missing = missing_required_files(loop_dir)
        if missing:
            info.append("missing v1 required file(s): " + ", ".join(missing))
    quality = (
        [{"level": lv, "message": msg} for lv, msg in quality_findings(loop_dir, tm)]
        if version == "v1" else []
    )
    return {
        "name": loop_dir.name,
        "path": str(loop_dir),
        "version": version,
        "errors": errors,
        "refusals": refusals,
        "info": info,
        "quality": quality,
        "finished": finished_mailbox(loop_dir),
        "ok": version == "v1" and not errors and not refusals,
    }


_FINISHED_RE = re.compile(
    r"^\s*(?:status|phase)\s*:\s*(?:SHIP|shipped|landed|done|complete|abandoned|stopped)\b",
    re.IGNORECASE | re.MULTILINE,
)


def finished_mailbox(loop_dir: Path) -> bool:
    """A finished (shipped / landed / abandoned) mailbox: its PLAN is history,
    so `--strict-quality` keeps its quality REJECTs advisory."""
    try:
        return bool(_FINISHED_RE.search((loop_dir / "STATE.md").read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def summarize(loops: list[dict]) -> dict:
    counts = {"v1": 0, "legacy": 0, "unknown": 0}
    violations = 0
    refused = 0
    for loop in loops:
        counts[loop["version"]] = counts.get(loop["version"], 0) + 1
        if loop["version"] == "v1" and loop["errors"]:
            violations += 1
        if loop.get("refusals"):
            refused += 1
    return {
        "total": len(loops),
        "v1": counts["v1"],
        "legacy": counts["legacy"],
        "unknown": counts["unknown"],
        "v1_violations": violations,
        "refused": refused,
        "ok": violations == 0 and refused == 0,
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
            for msg in loop.get("refusals", []):
                lines.append(f"    - REFUSED: {msg}")
            for finding in loop.get("quality", []):
                lines.append(f"    - quality: {finding['level']} {finding['message']}")
        else:
            lines.append(
                f"{loop['name']}/  {loop['version']}  (informational — not validated)"
            )
            for note in loop["info"]:
                lines.append(f"    - {note}")
    if summary["prompt_sync_lines"]:
        lines.append("Prompt sync:")
        for line in summary["prompt_sync_lines"]:
            lines.append(f"  {line}")
    lines.append(
        f"Summary: {summary['total']} loop(s): {summary['v1']} v1, "
        f"{summary['legacy']} legacy, {summary['unknown']} unknown; "
        f"{summary['v1_violations']} v1 mailbox(es) with violations"
    )
    lines.append("Result: PASS" if summary["ok"] else "Result: FAIL")
    return "\n".join(lines)


def _live_loop_dirs(tm, loop_dirs: list[Path]) -> list[Path]:
    """r16: a root mailbox whose root-free loop runs in a Lead worktree is
    checked in that live copy (the root copy is stale until the loop lands)."""
    live_fn = getattr(tm, "live_mailbox", None)
    out: list[Path] = []
    for loop_dir in loop_dirs:
        live = live_fn(loop_dir) if live_fn is not None else None
        if live is not None:
            print(
                f"trio-check: {loop_dir} runs root-free; checking its live mailbox {live}",
                file=sys.stderr,
            )
        out.append(live or loop_dir)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check trio-agent-loop mailboxes against MAILBOX-SCHEMA.md.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"trio-check {TRIO_CHECK_VERSION}",
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
    parser.add_argument(
        "--strict-quality",
        action="store_true",
        help="r18b preview: a quality REJECT (accepts: free text without an "
        "oracle) is a violation (exit 1); advisory by default in r18a",
    )
    parser.add_argument(
        "--no-prompt-sync",
        action="store_true",
        help="Skip the prompts/generate.py --check drift check (runs automatically "
        "when the scanned root contains prompts/generate.py)",
    )
    args = parser.parse_args(argv)

    try:
        tm = load_trio_metrics()
    except MetricsApiMismatch as exc:
        print(f"trio-check: {exc}", file=sys.stderr)
        return 2
    root = Path(args.path).expanduser().resolve()
    loops = [inspect_loop(p, tm) for p in _live_loop_dirs(tm, tm.discover_loops(root))]
    if args.strict_quality:
        for loop in loops:
            rejects = [q for q in loop.get("quality", []) if q["level"] == "REJECT"]
            if rejects and loop.get("finished"):
                loop["info"] = loop["info"] + [
                    f"--strict-quality: {len(rejects)} quality REJECT(s) kept advisory "
                    "(finished mailbox: its PLAN is history)"
                ]
            elif rejects:
                loop["errors"] = loop["errors"] + [
                    f"quality REJECT: {q['message']}" for q in rejects
                ]
                loop["ok"] = False
    summary = summarize(loops)
    summary["quality_findings"] = sum(len(loop.get("quality", [])) for loop in loops)

    prompt_ok, prompt_lines = (True, []) if args.no_prompt_sync else check_prompt_sync(root)
    summary["prompt_sync_lines"] = prompt_lines
    if not prompt_ok:
        summary["ok"] = False

    if args.json:
        json.dump({"path": str(root), "loops": loops, "summary": summary}, sys.stdout, indent=2)
        print()
    else:
        print(render(loops, root, summary))

    if summary["refused"]:
        # r15 guard: refuse rather than limp -- one line per offending
        # slice on stderr, exit 2 (over a plain violation's 1).
        for loop in loops:
            for msg in loop.get("refusals", []):
                print(f"trio-check: {loop['name']}: {msg}", file=sys.stderr)
        return 2
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

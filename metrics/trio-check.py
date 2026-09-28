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
#: declared repos, r15). Must equal omnigent/trioctl ``REQUIRED_METRICS_API``.
REQUIRED_METRICS_API = 5


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
HEADING_RE = re.compile(r"^#{1,6}\s")
_ARG = r"""("[^"]*"|'[^']*'|[^\s;&|)`'"]+)"""
_SEP = r"""(?:^|&&|\|\||;|\(|`|\s)"""
# cwd-changing commands: `cd <dir>` / `pushd <dir>`.
CD_RE = re.compile(_SEP + r"(?:cd|pushd)\s+" + _ARG)
# Commands that run in a named directory without changing the shell's cwd:
# `git -C <dir>`, `make -C <dir>`, `--rootdir/--prefix/--cwd[= ]<dir>`.
TARGET_DIR_RE = re.compile(
    _SEP + r"(?:(?:git|make)\s+-C\s+" + _ARG
    + r"|--(?:rootdir|prefix|cwd)(?:=|\s+)" + _ARG + ")"
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
    (`git -C`, `make -C`, `--rootdir`, `--prefix`, `--cwd`). Shell comments
    are ignored; inline-code backticks are separators.
    """
    found: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        line = COMMENT_RE.sub("", line)
        for m in CD_RE.finditer(line):
            found.append((index * 10_000 + m.start(), "cd", _unquote_arg(m.group(1))))
        for m in TARGET_DIR_RE.finditer(line):
            arg = m.group(1) or m.group(2)
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
    """The PLAN.md `full_check:` value lines (key line + indented follow-ups)."""
    lines = plan_text.splitlines()
    for index, raw in enumerate(lines):
        m = re.match(r"^full_check\s*:\s*(.*)$", raw)
        if not m:
            continue
        out = [m.group(1)]
        for follow in lines[index + 1:]:
            if not follow.strip() or not follow[:1].isspace():
                break
            out.append(follow)
        return out
    return []


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
    return {
        "name": loop_dir.name,
        "path": str(loop_dir),
        "version": version,
        "errors": errors,
        "refusals": refusals,
        "info": info,
        "ok": version == "v1" and not errors and not refusals,
    }


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
    loops = [inspect_loop(p, tm) for p in tm.discover_loops(root)]
    summary = summarize(loops)

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

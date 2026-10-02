"""Shared helpers for trio-opencode OPEN-LOOP fake-``opencode`` scenario
modules (``tests/test_openloop_e2e.py``) -- the open-loop counterpart of
``tests/scenarios/common.py`` (which this module itself imports and reuses
for git/marker/structured-reply plumbing; it is NOT a replacement for it).

Every scenario module runs inside the fake ``opencode`` executable's own
subprocess, never inside pytest -- see ``common.py``'s own module docstring
for why every scenario module inserts this file's directory onto
``sys.path`` before importing either module.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

_CONTEXT_RE = re.compile(r"^OPEN-LOOP CONTEXT: kind=(\S+?)(?: slice=(\S+))?(?: sha=(\S+))?$")
_MAILBOX_RE = re.compile(r"Mailbox \(absolute\): (\S+)\.")
_ITERATION_RE = re.compile(r"for iteration (\d+) of an open-loop")
_LEAD_WT_RE = re.compile(r"Lead worktree `([^`]+)`")
_ATTEMPT_RE = re.compile(r"`attempt: ([^`]+)`")
_EVALUATED_RE = re.compile(r"`evaluated: ([^`]+)`")
#: olprompts._render_builder's hard-coded fix-commit instruction: present
#: only when the Lead returned this slice with a `fault:` id.
_FIX_COMMIT_RE = re.compile(r"as `slice\(([^)]+)\): fix (f\d+) <summary>`")
#: olprompts._render_lead_plan's "OPEN FAULTS (orientation):" line shape.
_OPEN_FAULT_RE = re.compile(
    r"^- (f\d+) slice=(\S+) scope=\S+ status=(\S+) observed_at=(\S+): ", re.M)
#: olprompts._multi_repo_pins_listing's one-line-per-repo pin listing.
_PIN_LINE_RE = re.compile(r"^  - `([^`]+)`: `([^`]+)` @`([0-9a-f?]+)`$", re.M)


def context(ctx) -> tuple[str, str | None, str | None]:
    """``(kind, slice, sha)`` from the first ``OPEN-LOOP CONTEXT:`` line."""
    m = _CONTEXT_RE.match(ctx.prompt.splitlines()[0])
    if not m:
        raise AssertionError(f"ol scenario: no OPEN-LOOP CONTEXT line: {ctx.prompt[:120]!r}")
    return m.group(1), m.group(2), m.group(3)


def mailbox_of(ctx) -> Path:
    m = _MAILBOX_RE.search(ctx.prompt)
    if not m:
        raise AssertionError("ol scenario: no mailbox in prompt")
    return Path(m.group(1))


def iteration_of(ctx) -> int:
    m = _ITERATION_RE.search(ctx.prompt)
    return int(m.group(1)) if m else 1


def lead_worktree_of(ctx) -> Path:
    m = _LEAD_WT_RE.search(ctx.prompt)
    if not m:
        raise AssertionError("ol scenario: no Lead worktree path in prompt")
    return Path(m.group(1))


def attempt_of(ctx) -> str:
    m = _ATTEMPT_RE.search(ctx.prompt)
    if not m:
        raise AssertionError("ol scenario: no `attempt: ...` field in prompt")
    return m.group(1)


def evaluated_of(ctx) -> str:
    m = _EVALUATED_RE.search(ctx.prompt)
    if not m:
        raise AssertionError("ol scenario: no `evaluated: ...` field in prompt")
    return m.group(1)


def pin_lines(ctx) -> dict[str, tuple[str, str]]:
    """``{repo name: (path, sha)}`` from the MULTI-REPO integration-eval
    note's pin listing (``olprompts._multi_repo_pins_listing``)."""
    return {name: (path, sha) for name, path, sha in _PIN_LINE_RE.findall(ctx.prompt)}


def first_open_fault(ctx) -> tuple[str, str] | None:
    """``(fault_id, slice_id)`` of the first ``status=open`` fault listed in
    a lead-plan prompt's ``OPEN FAULTS (orientation):`` section, else None."""
    m = _OPEN_FAULT_RE.search(ctx.prompt)
    if m and m.group(3) == "open":
        return m.group(1), m.group(2)
    return None


def fix_commit_target(ctx) -> tuple[str, str] | None:
    """``(slice_id, fault_id)`` a builder prompt's commit instruction names
    (present only when the Lead returned this slice with a `fault:`)."""
    m = _FIX_COMMIT_RE.search(ctx.prompt)
    return (m.group(1), m.group(2)) if m else None


def touch_marker_once(ctx, name: str, content: str = "") -> None:
    """Like ``common.touch_marker``, but never overwrites an existing
    marker -- for a value (e.g. a start timestamp) that must keep reflecting
    the FIRST of possibly several real dispatches of the same (slice, sha
    nominally-same-content) grading or build turn."""
    if not common.marker_path(ctx, name).exists():
        common.touch_marker(ctx, name, content)


def set_fault_status(mbox: Path, fault_id: str, new_status: str) -> None:
    """Flip one QUEUE.md ``faults:`` entry's ``status:`` line in place (the
    Lead's own job per MAILBOX-SCHEMA.md -- never touches any other field)."""
    path = mbox / "QUEUE.md"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"(- id: {re.escape(fault_id)}\n(?:.*\n)*?    status: )\S+")
    new_text, n = pattern.subn(lambda m: m.group(1) + new_status, text, count=1)
    if n != 1:
        raise AssertionError(
            f"ol_common.set_fault_status: fault {fault_id} status line not found ({n} matches)")
    path.write_text(new_text, encoding="utf-8")


#: Matches a well-formed ```yaml fence whose body has a `faults:` line at
#: column 0 (MAILBOX-SCHEMA.md); used to insert a new entry into an
#: EXISTING fence rather than opening a second one (a second `faults:`
#: fence is a parse error that holds the integration gate -- a correctly
#: behaving Evaluator always edits the existing fence in place).
_FAULTS_FENCE_RE = re.compile(r"(^```ya?ml\n(?:(?!^```).*\n)*?faults:\n(?:(?!^```).*\n)*)(^```$)",
                              re.M)


_RETIRED_SLICE_RE = re.compile(r"^\s*-\s*slice:\s*(\S+)\s*$")
_RETIRED_SHA_RE = re.compile(r"^\s*sha:\s*(\S+)\s*$")


def latest_retired_sha(mbox: Path, slice_id: str) -> str | None:
    """The LAST (file-order) ``retired:`` entry's sha for *slice_id* --
    MAILBOX-SCHEMA.md's definition of "latest" (what the Evaluator grades);
    an EARLIER entry for the same slice is "superseded", the basis for a
    fault's ``observed_at`` going `stale` rather than needing a fresh fix."""
    text = (mbox / "QUEUE.md").read_text(encoding="utf-8") if (mbox / "QUEUE.md").is_file() else ""
    latest: str | None = None
    current_slice: str | None = None
    for raw in text.splitlines():
        m = _RETIRED_SLICE_RE.match(raw)
        if m:
            current_slice = m.group(1)
            continue
        m2 = _RETIRED_SHA_RE.match(raw)
        if m2 and current_slice == slice_id:
            latest = m2.group(1)
            current_slice = None
    return latest


_FAULT_ID_RE = re.compile(r"^\s*-\s*id:\s*f(\d+)\s*$", re.M)


def next_fault_id(mbox: Path) -> str:
    """The next free ``f<N>`` id (scans every ``- id: f<N>`` line in
    QUEUE.md, in or out of a fence -- a conservative over-count is fine, it
    only ever skips an id, never reuses one)."""
    text = (mbox / "QUEUE.md").read_text(encoding="utf-8") if (mbox / "QUEUE.md").is_file() else ""
    nums = [int(m) for m in _FAULT_ID_RE.findall(text)]
    return f"f{(max(nums) + 1) if nums else 1}"


def append_fault(mbox: Path, *, fault_id: str, slice_id: str, observed_at: str,
                 scope: str, reason: str) -> None:
    """Append one ``faults:`` entry to QUEUE.md: into the existing fence's
    ```yaml block when one is already there, else a freshly created one
    (the Evaluator's own job, MAILBOX-SCHEMA.md)."""
    entry = (
        f"  - id: {fault_id}\n"
        f"    slice: {slice_id}\n"
        f"    observed_at: {observed_at}\n"
        f"    scope: {scope}\n"
        f"    reason: {reason}\n"
        "    status: open\n"
    )
    path = mbox / "QUEUE.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    new_text, n = _FAULTS_FENCE_RE.subn(lambda m: m.group(1) + entry + m.group(2), text, count=1)
    if n == 1:
        path.write_text(new_text, encoding="utf-8")
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n```yaml\nfaults:\n" + entry + "```\n")


def handle_integration_eval_ship(ctx) -> None:
    """Generic single-repo integration-eval SHIP handler: overwrite
    VERDICT.md with the integration verdict header (preserving every
    per-slice section already appended) and make the SHIP retirement commit
    in the Lead worktree -- same as ol_happy.py's own (private) handler."""
    mbox = mailbox_of(ctx)
    iteration = iteration_of(ctx)
    lead_wt = lead_worktree_of(ctx)
    attempt = attempt_of(ctx)
    sha = evaluated_of(ctx)

    verdict_path = mbox / "VERDICT.md"
    existing = verdict_path.read_text(encoding="utf-8") if verdict_path.is_file() else ""
    header = (f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
             f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n\n")
    verdict_path.write_text(header + existing, encoding="utf-8")
    mailbox_rel = str(mbox.relative_to(lead_wt))
    common.commit(lead_wt, f"loop: iteration {iteration} — SHIP", [mailbox_rel])
    ctx.text(f"SHIP. iteration {iteration} verified end to end. "
            f"attempt: {attempt} evaluated: {sha}")

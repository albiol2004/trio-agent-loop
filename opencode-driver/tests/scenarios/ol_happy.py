"""Open-loop happy path: 3 disjoint slices (a, b, c), one wave, clean SHIP.
Used by ``test_openloop_e2e.py``.

Slice-evals for ``a`` and ``b`` prove genuine concurrency the same way
``happy.py`` proves builder overlap: each writes its own start marker, then
waits (briefly) to see the OTHER's marker before finishing -- this can only
succeed if ``trio_opencode.openloop``/``steplib.TL.run_open_loop`` actually
ran them on separate threads at the same time (``slice_eval_concurrency``
default 4, isolation on).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICES = ["a", "b", "c"]

_CONTEXT_RE = re.compile(r"^OPEN-LOOP CONTEXT: kind=(\S+?)(?: slice=(\S+))?(?: sha=(\S+))?$")
_MAILBOX_RE = re.compile(r"Mailbox \(absolute\): (\S+)\.")
_ITERATION_RE = re.compile(r"for iteration (\d+) of an open-loop")
_LEAD_WT_RE = re.compile(r"Lead worktree `([^`]+)`")
_ATTEMPT_RE = re.compile(r"`attempt: ([^`]+)`")
_EVALUATED_RE = re.compile(r"`evaluated: ([^`]+)`")


def _context(ctx):
    m = _CONTEXT_RE.match(ctx.prompt.splitlines()[0])
    if not m:
        raise AssertionError(f"ol_happy.py: no OPEN-LOOP CONTEXT line: {ctx.prompt[:120]!r}")
    return m.group(1), m.group(2), m.group(3)


def mailbox_of(ctx):
    m = _MAILBOX_RE.search(ctx.prompt)
    if not m:
        raise AssertionError("ol_happy.py: no mailbox in prompt")
    return Path(m.group(1))


def iteration_of(ctx):
    m = _ITERATION_RE.search(ctx.prompt)
    return int(m.group(1)) if m else 1


# --------------------------------------------------------------- lead


def _handle_lead(ctx):
    kind, _slice, _sha = _context(ctx)
    if kind == "lead-plan":
        common.reply(ctx, {
            "slices": [
                {"id": s, "brief": f"## Targeted check\ntest -f {s}.py\n", "writes": [f"{s}.py"],
                 "reads": [], "depends": [], "repo": "home", "targeted_check": f"test -f {s}.py",
                 "fault": None}
                for s in SLICES
            ],
            "notes": "three disjoint slices, one wave",
        })
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": SLICES, "takeovers": [],
                           "summary": "all three slices retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_happy.py: unexpected lead kind {kind!r}")


# ------------------------------------------------------------- builder


def _handle_builder(ctx):
    kind, slice_id, _sha = _context(ctx)
    assert kind == "builder", kind
    common.touch_marker(ctx, f"start-{slice_id}")
    other = {"a": "b", "b": "a"}.get(slice_id)
    if other:
        common.wait_for_marker(ctx, f"start-{other}", deadline=8.0)
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py"}, note=None)


# ----------------------------------------------------------- evaluator


def _handle_slice_eval(ctx, slice_id, sha):
    common.touch_marker(ctx, f"eval-start-{slice_id}")
    other = {"a": "b", "b": "a"}.get(slice_id)
    saw_other = False
    if other:
        saw_other = common.wait_for_marker(ctx, f"eval-start-{other}", deadline=8.0)
        common.touch_marker(ctx, f"eval-saw-other-{slice_id}", "1" if saw_other else "0")
    mbox = mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (a.py/b.py/c.py present).")


def _handle_integration_eval(ctx):
    mbox = mailbox_of(ctx)
    iteration = iteration_of(ctx)
    lead_wt_m = _LEAD_WT_RE.search(ctx.prompt)
    if not lead_wt_m:
        raise AssertionError("ol_happy.py: no Lead worktree path in integration-eval prompt")
    lead_wt = Path(lead_wt_m.group(1))
    attempt = _ATTEMPT_RE.search(ctx.prompt).group(1)
    sha = _EVALUATED_RE.search(ctx.prompt).group(1)

    verdict_path = mbox / "VERDICT.md"
    existing = verdict_path.read_text(encoding="utf-8") if verdict_path.is_file() else ""
    header = (f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
             f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n\n")
    verdict_path.write_text(header + existing, encoding="utf-8")
    mailbox_rel = str(mbox.relative_to(lead_wt))
    common.commit(lead_wt, f"loop: iteration {iteration} — SHIP", [mailbox_rel])
    ctx.text(f"SHIP. iteration {iteration} verified end to end. "
            f"attempt: {attempt} evaluated: {sha}")


def _handle_evaluator(ctx):
    kind, slice_id, sha = _context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        _handle_integration_eval(ctx)
        return
    ctx.error("UnknownError", f"ol_happy.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_happy.py: unexpected agent {ctx.agent!r}")

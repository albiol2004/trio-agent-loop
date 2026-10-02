"""Open-loop concurrency: 3 disjoint slices (a, b, c) in one wave.

Slice `c`'s builder deliberately waits (bounded) for slice `a`'s slice-eval
to have STARTED before it finishes its own commit -- proof that a slice is
graded as soon as it lands, not only after the whole wave's builders report
(the driver merges+retires each builder the moment it reports, independent
of its wave-mates: ``OpenLoopRunner._dispatch_and_retire``). Slice-evals `a`
and `b` additionally prove genuine concurrency the same way
``ol_happy.py``/``happy.py`` do: each writes its own start marker, then
waits (briefly) to see the OTHER's marker before finishing.

Used by ``test_openloop_e2e.py::test_open_loop_three_slices_concurrent_slice_evals``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

SLICES = ["a", "b", "c"]


def _slice_dict(s: str) -> dict:
    return {"id": s, "brief": f"## Targeted check\ntest -f {s}.py\n", "writes": [f"{s}.py"],
           "reads": [], "depends": [], "repo": "home", "targeted_check": f"test -f {s}.py",
           "fault": None}


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        common.reply(ctx, {"slices": [_slice_dict(s) for s in SLICES],
                           "notes": "three disjoint slices, one wave"})
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": SLICES, "takeovers": [],
                           "summary": "all three slices retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_3slices.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    # `touch_marker_once`: a slice's builder commit (and, below, its
    # slice-eval's start marker) may run more than once for distinct
    # retired shas of the SAME slice (the driver may append more than one
    # `retired:` entry for one builder pass); the FIRST occurrence is the
    # one this test's timing proof cares about.
    ol_common.touch_marker_once(ctx, f"builder-start-{slice_id}")
    if slice_id == "c":
        # Deliberately the last to finish: wait (bounded) for proof that
        # slice `a`'s slice-eval already started while this builder (part
        # of the SAME wave) is still running.
        common.wait_for_marker(ctx, "eval-start-a", deadline=8.0)
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ol_common.touch_marker_once(ctx, f"builder-done-{slice_id}")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py"}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    ol_common.touch_marker_once(ctx, f"eval-start-{slice_id}")
    other = {"a": "b", "b": "a"}.get(slice_id)
    if other:
        saw_other = common.wait_for_marker(ctx, f"eval-start-{other}", deadline=8.0)
        ol_common.touch_marker_once(ctx, f"eval-saw-other-{slice_id}", "1" if saw_other else "0")
    mbox = ol_common.mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified ({slice_id}.py present).")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        ol_common.handle_integration_eval_ship(ctx)
        return
    ctx.error("UnknownError", f"ol_3slices.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_3slices.py: unexpected agent {ctx.agent!r}")

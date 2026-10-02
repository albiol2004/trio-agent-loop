"""Open-loop exit drain: slices `a`/`b` build and dispatch concurrently;
slice `b`'s slice-eval sleeps well past the CLI's own
``--slice-eval-drain-seconds`` budget. Slice `z` (declared in PLAN.md, never
built) keeps the run from ever looking "fully retired"
(``metrics/trio_loop.py::_slices_fully_retired`` reads PLAN.md's own
``slices:`` block, never what the Lead returns): once `a`/`b` retire, the
Lead keeps returning ``slices: []`` ("nothing left to build") until the
core's own 3-no-op-pass stall guard ends the run with `b`'s slice-eval
STILL running. Used to prove the exit drain (``openloop.drive``) actually
kills an abandoned slice-eval instead of letting the process linger for its
full turn timeout (ol-harden blocking issue #2).

Used by
``test_openloop_e2e.py::test_open_loop_exit_drain_kills_abandoned_slice_eval``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402


def _slice(sid: str) -> dict:
    return {"id": sid, "brief": f"## Targeted check\ntest -f {sid}.py\n", "writes": [f"{sid}.py"],
           "reads": [], "depends": [], "repo": "home", "targeted_check": f"test -f {sid}.py",
           "fault": None}


def _handle_lead(ctx) -> None:
    kind, _slice_id, _sha = ol_common.context(ctx)
    mbox = ol_common.mailbox_of(ctx)
    if kind == "lead-plan":
        retired_a = ol_common.latest_retired_sha(mbox, "a")
        retired_b = ol_common.latest_retired_sha(mbox, "b")
        if retired_a and retired_b:
            # `z` is never returned on purpose: PLAN.md still declares it,
            # so the core's own "fully retired" check never agrees this run
            # is done -- the 3-no-op stall guard is what ends it, with `b`'s
            # slow slice-eval still in flight.
            common.reply(ctx, {"slices": [], "notes": "a/b retired; z never built (by design)"})
            return
        common.reply(ctx, {"slices": [_slice("a"), _slice("b")], "notes": "a, b"})
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["a", "b"], "takeovers": [],
                           "summary": "a/b retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_drain.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py", "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    mbox = ol_common.mailbox_of(ctx)
    if slice_id == "b":
        common.touch_marker(ctx, "slow-eval", str(os.getpid()))
        time.sleep(float(os.environ.get("EV_SLOW", "25")))
        common.touch_marker(ctx, "slow-eval-done", str(time.time()))
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified ({slice_id}.py present).")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    ctx.error("UnknownError", f"ol_drain.py: unexpected evaluator kind {kind!r} "
             "(the run must stall -- `z` is never built -- before any integration-eval)")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_drain.py: unexpected agent {ctx.agent!r}")

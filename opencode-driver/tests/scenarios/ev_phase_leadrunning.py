"""Scenario (oc-fix-eval-3 regression): the Evaluator's FIRST turn of
iteration 1 (running while `phase` is `lead-done` with `evaluated_sha`/
`evaluator_attempt` already pinned) overwrites `phase` with `lead-running`
-- another of `next()`'s own OK-looking phases, just the wrong one -- and
then sleeps forever, so the test's SIGKILL of the driver process lands
squarely inside this guarded turn, never reaching `_call_role`'s own
`finally` restore.

This is the oc-fix-eval-2 blocker: with the snapshot restore gated behind
the `_RESUME_OK_PHASES` early return, a crash that leaves `phase` at
`lead-running` was treated as "nothing to fix" and the Lead's plan call was
re-run on top of an already-pinned iteration, ending the run in `error`. The
fix restores the snapshot before that early return, so `resume` must put
`phase` back to `lead-done` and re-dispatch THIS iteration's Evaluator
directly, never re-running the Lead.

One disjoint slice, no concurrency to prove -- modelled on `eval_crash.py`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICES = [{"id": "a", "writes": ["a.py"], "reads": []}]


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        common.write(mbox / "PLAN.md", common.render_plan(SLICES))
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]} printing '{s['id']}'.",
                 "writes": s["writes"], "reads": s["reads"], "depends": []}
                for s in SLICES
            ],
            "notes": "",
        })
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        common.write(mbox / "REPORT.md", f"# Report — iteration {iteration}\n\na.py added.\n")
        common.append_log(mbox, f"- iter {iteration} | lead | added a.py")
        common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged a"})
        return
    ctx.error("UnknownError", f"ev_phase_leadrunning.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    common.write(Path(ctx.dir) / f"{slice_id}.py", f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    common.reply(ctx, {"summary": f"added {slice_id}.py", "head": sha})


def _handle_evaluator(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if not common.count_markers(ctx, "eval-phaselr-crashed"):
        common.touch_marker(ctx, "eval-phaselr-crashed", str(os.getpid()))
        # `evaluated_sha`/`evaluator_attempt` are left untouched -- only
        # `phase` is corrupted, to ANOTHER OK-looking value this time.
        state_path = mbox / "STATE.md"
        text = state_path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        state_path.write_text(text + "phase: lead-running\n", encoding="utf-8")
        ctx.sleep(1000)  # killed by the test's SIGKILL of the driver
        return
    iteration = common.iteration_of(ctx, mbox)
    attempt, sha = common.pin_attempt_and_sha(ctx)
    common.write(mbox / "VERDICT.md",
                f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
                f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n")
    common.commit(ctx.dir, f"loop: iteration {iteration} — SHIP", ["loop/VERDICT.md"])
    ctx.text(f"SHIP. a.py exists and is committed as slice(a). "
            f"attempt: {attempt} evaluated: {sha}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ev_phase_leadrunning.py: unexpected agent {ctx.agent!r}")

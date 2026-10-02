"""Scenario (bug 1, crash-window regression guard): the Lead's FIRST plan
call of iteration 1 corrupts every `OWNED_STATE_KEYS` line (same corruption
as `eval_crash.py`'s Evaluator) and then sleeps forever, so the driver is
SIGKILLed mid-turn. Unlike the Evaluator case, the pre-turn snapshot here
IS exactly `lead-running` (the Lead's own precondition), so `resume` must
still land on `lead-running` and re-run the Lead's plan call -- proving the
Bug 1 crash-window fix (restore from the persisted snapshot) does not
regress the ordinary Lead-turn crash the fallback already handled.

Modelled on `crash_resume.py`/`lead_writes_phase.py`.
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
        if not common.count_markers(ctx, "lead-plan-crashed"):
            common.touch_marker(ctx, "lead-plan-crashed", str(os.getpid()))
            state_path = mbox / "STATE.md"
            text = state_path.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                text += "\n"
            state_path.write_text(
                text + "phase: lead-planned\niteration: 99\nevaluated_sha: deadbeef\n"
                      "evaluator_attempt: bogus\nevaluated_repos: bogus\n",
                encoding="utf-8",
            )
            ctx.sleep(1000)  # killed by the test's SIGKILL of the driver
            return
        common.write(mbox / "PLAN.md", common.render_plan(SLICES))
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]}.", "writes": s["writes"],
                 "reads": s["reads"], "depends": []}
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
    ctx.error("UnknownError", f"lead_crash_corrupt.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    common.write(Path(ctx.dir) / f"{slice_id}.py", f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    common.reply(ctx, {"summary": f"added {slice_id}.py", "head": sha})


def _handle_evaluator(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    iteration = common.iteration_of(ctx)
    attempt, sha = common.pin_attempt_and_sha(ctx)
    common.write(mbox / "VERDICT.md",
                f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
                f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n")
    common.commit(ctx.dir, f"loop: iteration {iteration} — SHIP", ["loop/VERDICT.md"])
    ctx.text(f"SHIP. attempt: {attempt} evaluated: {sha}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"lead_crash_corrupt.py: unexpected agent {ctx.agent!r}")

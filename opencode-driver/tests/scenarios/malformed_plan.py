"""Scenario 4: the Lead's plan turn returns no fenced ```json block ->
exactly one re-prompt in the SAME opencode session. With env
``FAKE_OC_MALFORMED_TWICE`` unset, the re-prompt is answered properly and the
run ships; with it set, the re-prompt is ALSO malformed and the run stops
with status error (STATE left resumable at ``lead-running``)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICE = {"id": "solo", "writes": ["solo.py"], "reads": []}
MALFORMED_TWICE = bool(os.environ.get("FAKE_OC_MALFORMED_TWICE"))


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_reprompt(ctx):
        if MALFORMED_TWICE:
            ctx.text("Sorry, still just prose, no structured block this time either.")
            return
        common.write(mbox / "PLAN.md", common.render_plan([SLICE]))
        common.reply(ctx, {
            "slices": [{"id": "solo", "brief": "Create solo.py.", "writes": ["solo.py"],
                       "reads": [], "depends": []}],
            "notes": "",
        }, note="Apologies, here is the corrected plan.")
        return
    if common.is_plan_call(ctx):
        # No fenced ```json block at all: prose only.
        ctx.text("I looked at GOAL.md and STATE.md; I will add solo.py next.")
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        if "(last wave)" in ctx.prompt:
            common.write(mbox / "REPORT.md", f"# Report — iteration {iteration}\n\nsolo.py added.\n")
            common.append_log(mbox, f"- iter {iteration} | lead | added solo.py")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged solo"})
        return
    ctx.error("UnknownError", f"malformed_plan.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    common.write(Path(ctx.dir) / "solo.py", "print('solo')\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add solo.py")
    common.reply(ctx, {"summary": "added solo.py", "head": sha})


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
        ctx.error("UnknownError", f"malformed_plan.py: unexpected agent {ctx.agent!r}")

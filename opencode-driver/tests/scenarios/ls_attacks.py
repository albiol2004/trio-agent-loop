"""Lockstep attacks-gate scenario (rule 1): one slice `x`; with
``$FAKE_OC_LS_MODE=first_no_attacks`` the Evaluator's FIRST SHIP lists no
`attacks:`, every later SHIP lists one (the default mode always lists one).

After the driver refuses the first SHIP (verdict becomes ITERATE, scope
None) the iteration-2 Lead plan carries ``SHIP GATE ERRORS FROM THE DRIVER``;
this Lead then has nothing to build (`slices: []`) and finishes the pass
alone (the solo call), and the Evaluator re-grades with an attack.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

MODE = os.environ.get("FAKE_OC_LS_MODE", "always_attacks")
SLICE = {"id": "x", "writes": ["x.py"], "reads": []}


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    iteration = common.iteration_of(ctx)
    if common.is_plan_call(ctx):
        if MODE == "first_no_attacks" and "SHIP GATE ERRORS FROM THE DRIVER" in ctx.prompt:
            common.reply(ctx, {"slices": [], "notes": "nothing to build; re-grade"})
            return
        common.write(mbox / "PLAN.md", common.render_plan([SLICE]))
        common.reply(ctx, {
            "slices": [{"id": "x", "brief": "Create x.py with a print statement.",
                        "writes": ["x.py"], "reads": [], "depends": []}],
            "notes": "one slice",
        })
        return
    if common.is_integrate_call(ctx):
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        if "(last wave)" in ctx.prompt:
            common.write(mbox / "REPORT.md", f"# Report — iteration {iteration}\n\nx.py added.\n")
            common.append_log(mbox, f"- iter {iteration} | lead | added x.py")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged x"})
        return
    if common.is_solo_call(ctx):
        common.write(mbox / "REPORT.md",
                     f"# Report — iteration {iteration}\n\nNo product change; re-grade.\n")
        common.append_log(mbox, f"- iter {iteration} | lead | no product change; re-grade")
        common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"summary": "no product change"}, note="Done.")
        return
    ctx.error("UnknownError", f"ls_attacks.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    common.write(Path(ctx.dir) / "x.py", "print('x')\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add x.py")
    common.reply(ctx, {"summary": "added x.py", "head": sha})


def _handle_evaluator(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    iteration = common.iteration_of(ctx, mbox)
    attempt, sha = common.pin_attempt_and_sha(ctx)
    n = common.count_markers(ctx, "evaluator-") + 1
    common.touch_marker(ctx, f"evaluator-{n}")
    attacks = "" if (MODE == "first_no_attacks" and n == 1) else common.ATTACKS
    common.write(mbox / "VERDICT.md",
                 f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
                 f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n" + attacks)
    common.commit(ctx.dir, f"loop: iteration {iteration} — SHIP", ["loop/VERDICT.md"])
    ctx.text(f"SHIP. x.py prints. attempt: {attempt} evaluated: {sha}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ls_attacks.py: unexpected agent {ctx.agent!r}")

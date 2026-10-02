"""Concurrent crash + resume: 2 disjoint slices dispatched in ONE wave (like
happy.py), but BOTH builders sleep forever on their first invocation (a
marker file per slice id, not ``ctx.n``, since ``FAKE_OC_STATE`` persists
across both driver invocations — see crash_resume.py's own note). The test
SIGKILLs the driver once BOTH builders are confirmed spawned (so
``.driver.json``'s ``turns`` dict has two live entries at once), leaving two
orphan opencode processes behind; ``resume`` must kill BOTH before `begin`
reclaims their builder worktrees. The second invocation of each builder
(after resume re-dispatches) behaves like the happy path's builders and the
run ships.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICES = [
    {"id": "a", "writes": ["a.py"], "reads": []},
    {"id": "b", "writes": ["b.py"], "reads": []},
]


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        common.write(mbox / "PLAN.md", common.render_plan(SLICES))
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]}.", "writes": s["writes"],
                 "reads": s["reads"], "depends": []}
                for s in SLICES
            ],
            "notes": "two disjoint slices, one wave",
        })
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        if "(last wave)" in ctx.prompt:
            common.write(mbox / "REPORT.md", f"# Report — iteration {iteration}\n\na.py/b.py added.\n")
            common.append_log(mbox, f"- iter {iteration} | lead | added a.py and b.py")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged a/b"})
        return
    ctx.error("UnknownError", f"concurrent_crash_resume.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    marker_name = f"builder-first-{slice_id}"
    if not common.count_markers(ctx, marker_name):
        common.touch_marker(ctx, marker_name, str(os.getpid()))
        ctx.sleep(1000)  # killed by the test's SIGKILL of the driver
        return
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
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
        ctx.error("UnknownError", f"concurrent_crash_resume.py: unexpected agent {ctx.agent!r}")

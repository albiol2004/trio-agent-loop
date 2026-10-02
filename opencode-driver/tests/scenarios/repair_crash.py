"""Scenario (oc-fix-eval-3 regression): like `iterate_repair.py` -- iteration
1 ships an ``ITERATE scope=local:x.py``, `next()` dispatches a repair turn
for iteration 2 -- except the repair turn's FIRST attempt corrupts `phase`
(to `repairing`, not one of `_RESUME_OK_PHASES`) and `iteration` (to `9`),
then sleeps forever, so the test's SIGKILL of the driver process lands
squarely inside this guarded turn, never reaching `_call_role`'s own
`finally` restore.

The persisted pre-turn snapshot (`phase: repair-running`, `iteration: 2`)
must be restored on `resume`, and the repair turn re-dispatched -- not the
Lead -- so the run goes on to fix x.py and ship.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICE = {"id": "x", "writes": ["x.py"], "reads": []}


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        common.write(mbox / "PLAN.md", common.render_plan([SLICE]))
        common.reply(ctx, {
            "slices": [{"id": "x", "brief": "Create x.py with a print statement.",
                       "writes": ["x.py"], "reads": [], "depends": []}],
            "notes": "one slice",
        })
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        if "(last wave)" in ctx.prompt:
            common.write(mbox / "REPORT.md",
                        f"# Report — iteration {iteration}\n\nx.py added (buggy on purpose).\n")
            common.append_log(mbox, f"- iter {iteration} | lead | added x.py")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged x"})
        return
    ctx.error("UnknownError", f"repair_crash.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    path = Path(ctx.dir) / "x.py"
    common.write(path, "print('x has a bug'\n")  # deliberately missing ')'
    sha = common.commit(ctx.dir, f"slice({slice_id}): add x.py (buggy)")
    common.reply(ctx, {"summary": "added x.py (has a syntax bug)", "head": sha})


def _handle_repair(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if not common.count_markers(ctx, "repair-crashed"):
        common.touch_marker(ctx, "repair-crashed", str(os.getpid()))
        state_path = mbox / "STATE.md"
        text = state_path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        state_path.write_text(text + "phase: repairing\niteration: 9\n", encoding="utf-8")
        ctx.sleep(1000)  # killed by the test's SIGKILL of the driver
        return
    iteration = common.iteration_of(ctx)
    path = Path(ctx.dir) / "x.py"
    common.write(path, "print('x fixed')\n")
    common.commit(ctx.dir, "slice(x): fix syntax error", ["x.py"])
    common.append_log(mbox, f"- iter {iteration} | repair | fixed x.py syntax error")
    common.commit(ctx.dir, f"loop: iteration {iteration} repair report", ["loop/LOG.md"])
    common.reply(ctx, {"summary": "fixed the syntax error in x.py"}, note="Fixed.")


def _handle_evaluator(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    iteration = common.iteration_of(ctx)
    attempt, sha = common.pin_attempt_and_sha(ctx)
    if iteration == 1:
        common.write(mbox / "VERDICT.md",
                    "VERDICT: ITERATE scope=local:x.py\n# Verdict — iteration 1\n"
                    f"attempt: {attempt}\nevaluated: {sha}\n\n"
                    "x.py has a syntax error (unbalanced parenthesis) — repair scope: local:x.py.\n")
        common.commit(ctx.dir, "loop: iteration 1 — ITERATE", ["loop/VERDICT.md"])
        ctx.text(f"ITERATE scope=local:x.py — x.py fails to parse. attempt: {attempt} evaluated: {sha}")
        return
    common.write(mbox / "VERDICT.md",
                f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
                f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n")
    common.commit(ctx.dir, f"loop: iteration {iteration} — SHIP", ["loop/VERDICT.md"])
    ctx.text(f"SHIP. x.py now parses and prints. attempt: {attempt} evaluated: {sha}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-repair":
        _handle_repair(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"repair_crash.py: unexpected agent {ctx.agent!r}")

"""Scenario 2: ITERATE scope=local:<path> -> a repair turn fixes and commits
-> SHIP. Used with ``root_free=False`` (in-place) for variety.

Iteration 1: one slice ``x`` (writes ``x.py``), evaluator returns
``ITERATE scope=local:x.py`` (x.py has a deliberate bug the repair pass must
fix). Iteration 2: ``next()`` dispatches a repair turn scoped to that path;
the repair fixes x.py and commits ``slice(x): fix ...`` and the mandatory
``| repair |`` LOG line; evaluator then ships.
"""
from __future__ import annotations

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
            common.write(mbox / "REPORT.md", f"# Report — iteration {iteration}\n\nx.py added (buggy on purpose).\n")
            common.append_log(mbox, f"- iter {iteration} | lead | added x.py")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged x"})
        return
    ctx.error("UnknownError", f"iterate_repair.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    path = Path(ctx.dir) / "x.py"
    common.write(path, "print('x has a bug'\n")  # deliberately missing ')'
    sha = common.commit(ctx.dir, f"slice({slice_id}): add x.py (buggy)")
    common.reply(ctx, {"summary": "added x.py (has a syntax bug)", "head": sha})


def _handle_repair(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
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
        ctx.error("UnknownError", f"iterate_repair.py: unexpected agent {ctx.agent!r}")

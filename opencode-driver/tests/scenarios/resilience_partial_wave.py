"""NEW RESILIENCE scenario (top acceptance item, SPEC.md task 8): a wave of 2
disjoint slices, ``a`` and ``b``. Builder ``a`` always succeeds immediately
(writes + commits ``a.py`` on its own branch). Builder ``b`` fails transient
on EVERY invocation — exhausting both the runner's own internal retries
(``max_attempts``) and the driver's own two-attempt ``_call_role`` loop —
until the test flips it healthy (a marker file) and calls ``resume``.

Since ``a`` finishes and ``b`` never does, ``_run_wave``'s
``ThreadPoolExecutor`` never reaches the integrate/cleanup step for THIS
wave at all (iterating ``futures`` hits ``b``'s raised ``DriverStop`` before
any merge happens) — ``a``'s branch is left, complete and committed, but
UNMERGED into the Lead's HEAD, and the whole run stops ``status: error``
with STATE.md left resumable (same iteration, phase ``lead-running``).

On ``resume`` the driver re-attaches the Lead worktree WITHOUT re-seeding
it (the live mailbox keeps STATE ``lead-running`` of iteration 1), so native
``begin()`` reclaim finds ``a``'s ledger-owned, verified branch reusable and
merges it into the Lead HEAD (``reclaimed["merged"]``); the resumed plan
prompt carries the "PREVIOUS RUN: the driver merged" note and this
scenario's Lead then plans only the missing slice ``b``. ``a``'s original
commit is kept and builder ``a`` never runs again.
"""
from __future__ import annotations

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
        merged_before = "PREVIOUS RUN: the driver merged" in ctx.prompt and "a (`" in ctx.prompt
        todo = [s for s in SLICES if not (merged_before and s["id"] == "a")]
        common.write(mbox / "PLAN.md", common.render_plan(todo))
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]}.", "writes": s["writes"],
                 "reads": s["reads"], "depends": []}
                for s in todo
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
        common.reply(ctx, {"merged": list(branches.keys()), "conflicts": [], "summary": "merged"})
        return
    ctx.error("UnknownError", f"resilience_partial_wave.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    if slice_id == "b" and not common.count_markers(ctx, "healthy-b"):
        # No `ctx.text()` here on purpose: emitting text first would give the
        # accumulator a step, and the runner's own transient-retry logic
        # then "continues" the same session with a generic prompt that
        # carries no slice id at all — this scenario needs every retry
        # (both the runner's internal ones and the driver's own two tries)
        # to see the ORIGINAL builder prompt so `common.builder_slice_id`
        # keeps working.
        ctx.stderr("upstream service timeout")
        ctx.exit(1)
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
        ctx.error("UnknownError", f"resilience_partial_wave.py: unexpected agent {ctx.agent!r}")

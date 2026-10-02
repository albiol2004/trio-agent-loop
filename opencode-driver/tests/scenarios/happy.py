"""Scenario 1: happy path, 2 disjoint slices in one concurrent wave, clean
SHIP. Used by ``test_e2e.py::test_happy_path_root_free_concurrent_wave``.

Slice ``a`` writes ``a.py``, slice ``b`` writes ``b.py`` — disjoint, so
``waves.plan_waves`` puts them in one wave; both builder turns overlap in
time (proved via marker files, see ``common.wait_for_marker``/
``count_markers``: each builder writes its own start marker, then waits
briefly for the OTHER slice's marker before proceeding, so neither can
finish without having observed the other mid-flight).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICES = [
    {"id": "a", "writes": ["a.py"], "reads": []},
    {"id": "b", "writes": ["b.py"], "reads": []},
]

#: test_e2e.py's diverged-target land variant: commit directly onto the
#: ROOT checkout's target branch (simulating a human pushing to it) once,
#: from the very first Lead call, before this loop's own SHIP tries to land.
_DIVERGE_ROOT = os.environ.get("FAKE_OC_DIVERGE_TARGET_REPO")
_DIVERGE_DONE_FLAG = "diverge-done"


def _maybe_diverge_target(ctx) -> None:
    if not _DIVERGE_ROOT or common.count_markers(ctx, _DIVERGE_DONE_FLAG):
        return
    common.write(Path(_DIVERGE_ROOT) / "human-change.txt", "a human committed directly to main\n")
    common.commit(_DIVERGE_ROOT, "human: direct commit to main while the loop ran",
                  ["human-change.txt"])
    common.touch_marker(ctx, _DIVERGE_DONE_FLAG, "1")


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        _maybe_diverge_target(ctx)
        common.write(mbox / "PLAN.md", common.render_plan(SLICES))
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]} printing '{s['id']}'.",
                 "writes": s["writes"], "reads": s["reads"], "depends": []}
                for s in SLICES
            ],
            "notes": "two disjoint slices, one wave",
        })
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(__import__("re").findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        last = "(last wave)" in ctx.prompt
        merged = list(branches.keys())
        if last:
            common.write(mbox / "REPORT.md",
                        f"# Report — iteration {iteration}\n\nSlices {', '.join(merged)} "
                        "implemented by builders a/b.\n")
            common.append_log(mbox, f"- iter {iteration} | lead | shipped slices {', '.join(merged)}")
            # Keep the Lead worktree clean for teardown: commit every
            # pending mailbox write from this whole pass (PLAN.md from the
            # plan call, REPORT.md/LOG.md from here).
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": merged, "conflicts": [], "summary": "merged a and b cleanly"})
        return
    # Unexpected lead call in this scenario (solo/repair-retry): fail loud.
    ctx.error("UnknownError", f"happy.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
    common.touch_marker(ctx, f"start-{slice_id}")
    # Prove overlap: wait (briefly) to see the *other* slice's start marker
    # before finishing — if the driver ran builders sequentially, whichever
    # runs second would find its sibling long finished, but the FIRST to run
    # would never see the second's marker in time and this wait would time
    # out (surfaced as a plain assertion failure in the builder's own
    # process, which fake_opencode.py reports as a crash — the test asserts
    # `run_turn` never saw that, i.e. that both did see each other).
    other = "b" if slice_id == "a" else "a"
    seen = common.wait_for_marker(ctx, f"start-{other}", deadline=8.0)
    common.touch_marker(ctx, f"saw-other-{slice_id}", "1" if seen else "0")
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
    ctx.text(f"SHIP. Both slices verified: a.py and b.py exist and are committed as "
            f"slice(a)/slice(b). attempt: {attempt} evaluated: {sha}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"happy.py: unexpected agent {ctx.agent!r}")

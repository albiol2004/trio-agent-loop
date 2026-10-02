"""Scenario (bug 1 regression): the Lead's plan turn writes its own
``phase``/``iteration`` lines into the live STATE.md, exactly as if those
were its to own (a real run hit this and ``next()``'s ``_lead_running`` gate
then refused the following ``dispatch`` with "STATE is iteration 1 phase
lead-planned, not lead-running of iteration 1"). ``driver.py``'s
``_call_role`` owned-STATE-key guard must restore the driver's cursor right
after this turn, so the rest of the pass (and the run) proceeds normally.

One disjoint slice, no concurrency to prove — otherwise modelled on
``happy.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICES = [{"id": "a", "writes": ["a.py"], "reads": []}]


def _corrupt_state(ctx) -> None:
    """Append (never replace) a stray ``phase``/``iteration`` pair, as a
    role turn that mistakenly treats the driver's own cursor keys as
    role-writable would — ``_read_state`` takes the LAST matching line, so
    this masks the driver's real ``lead-running``/``1`` until restored."""
    mbox = common.mailbox_dir(ctx)
    state_path = mbox / "STATE.md"
    text = state_path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    state_path.write_text(text + "phase: lead-planned\niteration: 99\n", encoding="utf-8")


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        common.write(mbox / "PLAN.md", common.render_plan(SLICES))
        _corrupt_state(ctx)
        common.reply(ctx, {
            "slices": [
                {"id": s["id"], "brief": f"Create {s['writes'][0]} printing '{s['id']}'.",
                 "writes": s["writes"], "reads": s["reads"], "depends": []}
                for s in SLICES
            ],
            "notes": "one slice, proving the driver restores STATE after this turn",
        })
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(__import__("re").findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        merged = list(branches.keys())
        common.write(mbox / "REPORT.md",
                    f"# Report — iteration {iteration}\n\nSlice {', '.join(merged)} "
                    "implemented by builder a.\n")
        common.append_log(mbox, f"- iter {iteration} | lead | shipped slice {', '.join(merged)}")
        common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": merged, "conflicts": [], "summary": "merged a cleanly"})
        return
    ctx.error("UnknownError", f"lead_writes_phase.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    slice_id = common.builder_slice_id(ctx)
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
        ctx.error("UnknownError", f"lead_writes_phase.py: unexpected agent {ctx.agent!r}")

"""Open-loop attacks-gate scenario (rule 1): one slice `a`; the Evaluator's
first SHIP lacks an `attacks:` list, the re-graded one carries it.

``$FAKE_OC_ATTACKS_MODE``:

* ``slice``: the FIRST slice-eval of `a` writes a SHIP section with NO
  attacks; every later one lists an attack. The integration SHIP always
  lists one.
* ``integration``: slice-evals always list an attack; the FIRST
  integration-eval SHIPs with no `attacks:` list, later ones list one.
* ``slice_never``: slice-evals never list an attack (the driver's re-dispatch
  cap must stop the run, not ship it).

Every prompt is recorded by the fake (``calls.jsonl``); markers count the
dispatches per kind.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

MODE = os.environ.get("FAKE_OC_ATTACKS_MODE", "slice")


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        if "SHIP GATE ERRORS FROM THE DRIVER" in ctx.prompt:
            # The refused SHIP's errors reached this Lead: nothing to build,
            # the Evaluator re-grades the same pin.
            mbox = ol_common.mailbox_of(ctx)
            # A pass that wrote nothing is "empty" to the core and keeps the
            # errors pending; note the refusal in PLAN.md (real work).
            with open(mbox / "PLAN.md", "a", encoding="utf-8") as fh:
                fh.write("\nThe last SHIP lacked an attack: re-grade the same pin.\n")
            common.reply(ctx, {"slices": [], "notes": "re-grade with an attack"})
            return
        common.reply(ctx, {
            "slices": [{"id": "a", "brief": "## Targeted check\ntest -f a.py\n",
                        "writes": ["a.py"], "reads": [], "depends": [], "repo": "home",
                        "targeted_check": "test -f a.py", "fault": None}],
            "notes": "one slice",
        })
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["a"], "takeovers": [],
                           "summary": "slice a retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_attacks.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    common.write(Path(ctx.dir) / f"{slice_id}.py", f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py", "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    n = common.count_markers(ctx, "slice-eval-") + 1
    common.touch_marker(ctx, f"slice-eval-{n}")
    mbox = ol_common.mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
        if MODE == "slice_never" or (MODE == "slice" and n == 1):
            fh.write("- a.py: PASS (re-run) `test -f a.py`\n")
        else:
            fh.write(common.ATTACKS)
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (a.py present).")


def _handle_integration(ctx) -> None:
    n = common.count_markers(ctx, "integration-eval-") + 1
    common.touch_marker(ctx, f"integration-eval-{n}")
    mbox = ol_common.mailbox_of(ctx)
    iteration = ol_common.iteration_of(ctx)
    lead_wt = ol_common.lead_worktree_of(ctx)
    attempt = ol_common.attempt_of(ctx)
    sha = ol_common.evaluated_of(ctx)
    attacks = "" if (MODE == "integration" and n == 1) else common.ATTACKS
    path = mbox / "VERDICT.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(
        f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
        f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n" + attacks + "\n" + existing,
        encoding="utf-8")
    common.commit(lead_wt, f"loop: iteration {iteration} — SHIP", [str(mbox.relative_to(lead_wt))])
    ctx.text(f"SHIP. iteration {iteration} verified end to end. attempt: {attempt} evaluated: {sha}")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
    elif kind == "integration-eval":
        _handle_integration(ctx)
    else:
        ctx.error("UnknownError", f"ol_attacks.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-acceptance":
        # `$FAKE_OC_ATTACKS_ACC=crash`: the author dies, acceptance degrades to
        # no pack (the attacks gate must still apply).
        ctx.error("UnknownError", "author turn died")
    elif ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_attacks.py: unexpected agent {ctx.agent!r}")

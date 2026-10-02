"""Open-loop crash + resume: a slice-eval turn sleeps forever right after
writing a marker (simulating a real, non-interactive ``opencode run`` that
would otherwise sit there); the test SIGKILLs the DRIVER process and then
resumes. Modelled on ``tests/scenarios/eval_crash.py``/``crash_resume.py``
(lockstep), open-loop style (see ``ol_happy.py``).

One disjoint slice, `a` -- no concurrency to prove here.

Used by ``test_openloop_e2e.py::test_open_loop_crash_mid_slice_eval_then_resume``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

SLICE_A = {"id": "a", "brief": "## Targeted check\ntest -f a.py\n", "writes": ["a.py"],
          "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f a.py",
          "fault": None}


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        # A real Lead reads QUEUE.md: once `a` is retired (the forced first
        # pass of the resumed run) there is nothing left to build.
        if ol_common.latest_retired_sha(ol_common.mailbox_of(ctx), "a"):
            common.reply(ctx, {"slices": [], "notes": "slice a already retired; nothing to do"})
            return
        common.reply(ctx, {"slices": [SLICE_A], "notes": "single slice a"})
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["a"], "takeovers": [],
                           "summary": "slice a retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_crash.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py", "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    if not common.count_markers(ctx, "slice-eval-crashed"):
        common.touch_marker(ctx, "slice-eval-crashed", str(os.getpid()))
        ctx.sleep(1000)  # killed by the test's SIGKILL of the driver
        return
    mbox = ol_common.mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (a.py present).")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        ol_common.handle_integration_eval_ship(ctx)
        return
    ctx.error("UnknownError", f"ol_crash.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_crash.py: unexpected agent {ctx.agent!r}")

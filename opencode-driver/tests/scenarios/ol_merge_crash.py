"""Open-loop crash between merge and retire: the test's own wrapper script
(``kill_after_merge.py``, written at test time) patches
``olqueue.append_retired`` to SIGKILL the whole process the instant slice
`a`'s builder branch merges, BEFORE the ``retired:`` entry is ever written
(``openloop._merge_and_retire``'s own crash window, ol-harden blocking
issue #1). Everything the Lead/builder/evaluator do here is otherwise a
plain, single-slice happy path -- no sleep, no orphan turn; the crash is
injected entirely by the test's own process-level SIGKILL, not by anything
this scenario does. Modelled on ``ol_crash.py`` (same lead/builder shape,
minus its slice-eval sleep).

One disjoint slice, `a`.

Used by
``test_openloop_e2e.py::test_open_loop_sigkill_between_merge_and_retire_then_resume``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

# A gitignored mailbox: the generic integration-SHIP commit helper `git add`s
# the mailbox path, which git refuses for an ignored path unless forced (a
# test-fixture concern only, not a driver one) -- force-add explicit paths,
# but ONLY ones git actually ignores (a tracked mailbox keeps its normal add,
# so its runtime sidecars stay out of the SHIP commit).
_orig_commit = common.commit


def _commit(cwd, message, paths=None):
    if any(subprocess.run(["git", "-C", str(cwd), "check-ignore", "-q", p]).returncode == 0
           for p in paths or []):
        common.git(cwd, "add", "-f", *paths)
        common.git(cwd, "commit", "-q", "-m", message)
        return common.git(cwd, "rev-parse", "HEAD")
    return _orig_commit(cwd, message, paths)


common.commit = _commit

SLICE_A = {"id": "a", "brief": "## Targeted check\ntest -f a.py\n", "writes": ["a.py"],
          "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f a.py",
          "fault": None}


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        # A real Lead reads QUEUE.md: once `a` is retired (whether by a
        # normal builder pass or, on resume, by the driver's own crash
        # reconciliation) there is nothing left to build.
        if ol_common.latest_retired_sha(ol_common.mailbox_of(ctx), "a"):
            common.reply(ctx, {"slices": [], "notes": "slice a already retired; nothing to do"})
            return
        common.reply(ctx, {"slices": [SLICE_A], "notes": "single slice a"})
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["a"], "takeovers": [],
                           "summary": "slice a retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_merge_crash.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py", "head": sha}, note=None)


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        mbox = ol_common.mailbox_of(ctx)
        with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
            fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
            fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
        ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (a.py present).")
        return
    if kind == "integration-eval":
        ol_common.handle_integration_eval_ship(ctx)
        return
    ctx.error("UnknownError", f"ol_merge_crash.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_merge_crash.py: unexpected agent {ctx.agent!r}")

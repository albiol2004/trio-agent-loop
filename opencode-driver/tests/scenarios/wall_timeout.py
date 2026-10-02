"""Scenario 7: a builder turn just hangs (no stdout, no error) past
``turn_seconds`` — the runner kills the whole process group and reports
``kind="timeout"`` (never retried by the runner itself, since "timeout" is
not a retryable kind); the DRIVER's own outer retry (``_call_role``'s
"runAgentTwice") runs the turn again, which also times out, and the run
stops with status error."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SLICE = {"id": "solo", "writes": ["solo.py"], "reads": []}


def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        common.write(mbox / "PLAN.md", common.render_plan([SLICE]))
        common.reply(ctx, {
            "slices": [{"id": "solo", "brief": "Create solo.py.", "writes": ["solo.py"],
                       "reads": [], "depends": []}],
            "notes": "",
        })
        return
    ctx.error("UnknownError", f"wall_timeout.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    common.touch_marker(ctx, f"builder-pid-{ctx.n}", str(os.getpid()))
    ctx.sleep(1000)  # never responds; the runner's wall-clock timeout kills it


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    else:
        ctx.error("UnknownError", f"wall_timeout.py: unexpected agent {ctx.agent!r}")

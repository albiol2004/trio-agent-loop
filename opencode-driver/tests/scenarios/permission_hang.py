"""Scenario 6: a builder turn hits a denied permission and then hangs (a
real non-interactive ``opencode run`` cannot answer the prompt and would
otherwise sit forever) — the runner must recognise the "permission
requested: ... auto-rejecting" stderr line and kill the turn immediately,
never waiting for the wall-clock/idle timeout."""
from __future__ import annotations

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
    ctx.error("UnknownError", f"permission_hang.py: unexpected lead call: {ctx.prompt[:120]!r}")


def _handle_builder(ctx) -> None:
    common.touch_marker(ctx, "builder-pid", str(__import__("os").getpid()))
    ctx.permission_ask("external_directory", ["/etc/*"])
    ctx.sleep(1000)  # a real, non-interactive `opencode run` would hang here


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    else:
        ctx.error("UnknownError", f"permission_hang.py: unexpected agent {ctx.agent!r}")

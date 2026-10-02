"""Scenario 11: the Lead's plan turn just sleeps — used with the
``trio-opencode`` CLI as a real subprocess so the test can send it a real
SIGTERM mid-turn (Python signal handlers only run on the main thread, so an
in-process ``driver.run()`` call cannot be interrupted this way from the
same test process)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def handle(ctx) -> None:
    if ctx.agent == "trio-lead" and common.is_plan_call(ctx):
        common.touch_marker(ctx, "lead-pid", str(os.getpid()))
        ctx.sleep(1000)
        return
    ctx.error("UnknownError", f"sigterm_hang.py: unexpected call: {ctx.agent} {ctx.prompt[:120]!r}")

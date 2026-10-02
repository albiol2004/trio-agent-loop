"""Open-loop permission denial: builder `b` asks a denied permission and
then hangs (a real non-interactive ``opencode run`` cannot answer the
prompt and would otherwise sit forever); builder `a` (the other slice of
the SAME wave) is deliberately left running too, so a resume/report can
show whether the fatal stop also cancels sibling turns. Modelled on
``tests/scenarios/permission_hang.py`` (lockstep).

Used by ``test_openloop_e2e.py::test_open_loop_permission_denial_stops_and_surfaces``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

SLICES = [
    {"id": "a", "brief": "## Targeted check\ntest -f a.py\n", "writes": ["a.py"],
     "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f a.py", "fault": None},
    {"id": "b", "brief": "## Targeted check\ntest -f b.py\n", "writes": ["b.py"],
     "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f b.py", "fault": None},
]


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        common.reply(ctx, {"slices": SLICES, "notes": "two disjoint slices, one wave"})
        return
    ctx.error("UnknownError", f"ol_permission.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    common.touch_marker(ctx, f"builder-pid-{slice_id}", str(os.getpid()))
    if slice_id == "b":
        ctx.permission_ask("external_directory", ["/etc/*"])
    ctx.sleep(1000)  # a real, non-interactive `opencode run` would hang here


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    else:
        ctx.error("UnknownError", f"ol_permission.py: unexpected agent {ctx.agent!r}")

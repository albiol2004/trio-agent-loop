"""Open-loop frozen acceptance where the FREEZE LANDS WHILE A BUILDER IS
RUNNING -- the order a real run has (the author runs alongside the Lead, so
a builder is dispatched, and its branch cut, before the pack is frozen).

Reuses ``ol_acceptance``'s Lead/evaluator handlers and pack; only the author
and the builder are re-timed (all cross-process, via markers in
``FAKE_OC_STATE``):

* the AUTHOR waits until the builder has started (its worktree is cut from
  the pre-freeze HEAD), then writes the pack;
* the BUILDER commits its slice, then waits until the driver's
  ``acceptance: freeze`` commit is on the product repo's HEAD
  (``FAKE_REPO_PATH``) before it reports -- so the branch is merged AFTER the
  freeze and, unfixed, is a slice commit on a line that does not contain it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_acceptance  # noqa: E402
import ol_common  # noqa: E402


def _freeze_on_head(repo: str) -> bool:
    out = subprocess.run(["git", "-C", repo, "log", "--format=%s", "--grep=^acceptance: freeze"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def _handle_author(ctx) -> None:
    if not common.wait_for_marker(ctx, "builder-started", deadline=60.0):
        ctx.error("UnknownError", "slowfreeze: the builder never started")
        return
    ol_acceptance._handle_acceptance(ctx)


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    common.touch_marker(ctx, "builder-started")
    ok = ", ".join(str(k) for k in range(1, ol_acceptance.N_CHECKS + 1))
    common.write(Path(ctx.dir) / "app.py", "import sys\n"
                 f"if int(sys.argv[1]) in ({ok},):\n    print('hello', sys.argv[1])\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): implement hello")
    repo = os.environ["FAKE_REPO_PATH"]
    end = time.monotonic() + 60.0
    while time.monotonic() < end and not _freeze_on_head(repo):
        time.sleep(0.05)
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": "implemented app.py", "head": sha}, note=None)


def handle(ctx) -> None:
    if ctx.agent == "trio-acceptance":
        _handle_author(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    else:
        ol_acceptance.handle(ctx)

"""Open-loop multi-repo (PLAN.md ``repos:``): slice `a` in the home repo,
slice `b` in the declared repo `be`. Builder `b` runs in a worktree of
`be`'s Lead aggregate (``OpenLoopRunner._target_repo_path``); its retired
entry carries ``repo: be`` (MAILBOX-SCHEMA.md r15). The integration-eval
grades both pins and performs the per-repo SHIP retirement
``olprompts._MULTI_REPO_INTEGRATION_NOTE`` spells out: one empty
``loop: iteration N — SHIP (<mailbox>)`` commit in `be`'s checked-out
worktree, recorded as ``commit: be@<pin>`` in VERDICT.md, then the usual
home mailbox SHIP commit.

The pin listing names each declared repo's Lead aggregate (its
retirement target) in both isolation modes; under isolation the grading
copy is a separate detached worktree named in the workspace preface.

Used by ``test_openloop_e2e.py::test_open_loop_multi_repo_ships_and_lands_both``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

SLICE_A = {"id": "a", "brief": "## Targeted check\ntest -f a.py\n", "writes": ["a.py"],
          "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f a.py",
          "fault": None}
SLICE_B = {"id": "b", "brief": "## Targeted check\ntest -f b.py\n", "writes": ["b.py"],
          "reads": [], "depends": [], "repo": "be", "targeted_check": "test -f b.py",
          "fault": None}


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    if kind == "lead-plan":
        common.reply(ctx, {"slices": [SLICE_A, SLICE_B],
                           "notes": "slice a (home) and slice b (repo be), one wave"})
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["a", "b"], "takeovers": [],
                           "summary": "slice a and slice b retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_multirepo.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    path = Path(ctx.dir) / f"{slice_id}.py"
    common.write(path, f"print({slice_id!r})\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"added {slice_id}.py", "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    mbox = ol_common.mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified.")


def _handle_integration_eval(ctx) -> None:
    mbox = ol_common.mailbox_of(ctx)
    iteration = ol_common.iteration_of(ctx)
    attempt = ol_common.attempt_of(ctx)
    pins = ol_common.pin_lines(ctx)  # {name: (path, sha)}
    home_path, home_sha = pins["home"]
    be_path, be_sha = pins["be"]
    mailbox_name = str(mbox).rstrip("/").rsplit("/", 1)[-1]

    # Per-repo SHIP retirement FIRST (olprompts._MULTI_REPO_INTEGRATION_NOTE):
    # one empty commit in `be`'s own checked-out worktree.
    common.git(be_path, "commit", "--allow-empty", "-m",
              f"loop: iteration {iteration} — SHIP ({mailbox_name})")

    verdict_path = mbox / "VERDICT.md"
    existing = verdict_path.read_text(encoding="utf-8") if verdict_path.is_file() else ""
    header = (
        "VERDICT: SHIP\n"
        f"# Verdict — iteration {iteration}\n"
        f"attempt: {attempt}\n"
        f"evaluated: home@{home_sha}, be@{be_sha}\n"
        f"commit: home@{home_sha}\n"
        f"commit: be@{be_sha}\n\n"
    )
    verdict_path.write_text(header + existing, encoding="utf-8")
    mailbox_rel = str(mbox.relative_to(home_path))
    common.commit(home_path, f"loop: iteration {iteration} — SHIP", [mailbox_rel])
    ctx.text(f"SHIP. iteration {iteration} verified end to end across home and be. "
            f"attempt: {attempt} evaluated: home@{home_sha}, be@{be_sha}")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        _handle_integration_eval(ctx)
        return
    ctx.error("UnknownError", f"ol_multirepo.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_multirepo.py: unexpected agent {ctx.agent!r}")

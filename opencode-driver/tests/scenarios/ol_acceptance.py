"""Open-loop frozen acceptance: the fake author turn (agent
``trio-acceptance``, cwd = export) writes a minimal, schema-valid pack --
same pack-writing mechanism as ``tests/scenarios/acceptance.py``'s
``write_pack`` (``test_e2e.py``'s lockstep acceptance e2e) -- the single
slice's PLAN.md ``covers:`` every check id by construction (both are
hardcoded here, so no runtime race with the author thread that
``steplib.TL.run_open_loop`` starts alongside the first Lead pass), the
builder implements every check, the run freezes and ships through the
frozen-acceptance gate.

Used by ``test_openloop_e2e.py::test_open_loop_acceptance_on_ships``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

#: 5, matching tests/scenarios/acceptance.py's (lockstep) default -- a
#: smaller pack (tried first: 2) gets one free "too few checks" author
#: retry from metrics/trio-acceptance.py's own freeze_filter, which would
#: make the `len(acc_calls) == 1` assertion below flaky.
N_CHECKS = 5

CHECK = """\
import subprocess, sys
r = subprocess.run([sys.executable, "app.py", "{k}"], capture_output=True, text=True)
if r.returncode == 0 and "hello {k}" in r.stdout:
    sys.exit(0)
print("app.py does not print hello {k}")
sys.exit(1)
"""

_ALL_IDS = [f"ACC-{k:02d}" for k in range(1, N_CHECKS + 1)]


def _write_pack(export: Path) -> None:
    """Mirrors ``tests/scenarios/acceptance.py``'s ``write_pack``: a
    minimal but schema-valid pack whose checks genuinely FAIL at the base
    commit (``app.py`` does not exist yet there) and PASS once the slice
    implements ``app.py`` up to that check's ``k``."""
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for k in range(1, N_CHECKS + 1):
        name = f"acc_{k:02d}.py"
        (acc / "checks" / name).write_text(CHECK.format(k=k), encoding="utf-8")
        checks.append({"id": f"ACC-{k:02d}", "goal_ref": "GOAL.md:2", "goal_quote": "prints hello N",
                       "kind": "behaviour", "surface": "cli",
                       "run": ["python3", f"acceptance/checks/{name}"],
                       "expect": {"exit": 0}, "timeout_s": 30})
    (acc / "MANIFEST.json").write_text(json.dumps({
        "acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
        "checks": checks}, indent=2), encoding="utf-8")
    (acc / "AUTHOR.md").write_text("# inventory\n- testable-black-box: hello N\n",
                                   encoding="utf-8")


def _handle_acceptance(ctx) -> None:
    export = Path(ctx.dir)
    _write_pack(export)
    common.reply(ctx, {"checks": N_CHECKS, "summary": "wrote the pack"})


def _render_plan() -> str:
    lines = ["# Plan", "", "```yaml", "slices:", "  - id: app",
             "    writes: [app.py]", "    reads: []",
             f"    covers: [{', '.join(_ALL_IDS)}]", "```", "",
             "## Verification standard", "implement-then-smoke", "", "full_check: true"]
    return "\n".join(lines) + "\n"


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    mbox = ol_common.mailbox_of(ctx)
    if kind == "lead-plan":
        common.write(mbox / "PLAN.md", _render_plan())
        common.reply(ctx, {
            "slices": [{
                "id": "app", "brief": f"## Targeted check\npython3 app.py {N_CHECKS}\n",
                "writes": ["app.py"], "reads": [], "depends": [], "repo": "home",
                "targeted_check": f"python3 app.py {N_CHECKS}", "fault": None,
            }],
            "notes": "one slice, one wave",
        })
        return
    if kind == "lead-review":
        common.reply(ctx, {"results": [], "pass_slices": ["app"], "takeovers": [],
                           "summary": "slice app retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_acceptance.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    ok = ", ".join(str(k) for k in range(1, N_CHECKS + 1))
    path = Path(ctx.dir) / "app.py"
    common.write(path, "import sys\n"
                 f"if int(sys.argv[1]) in ({ok},):\n    print('hello', sys.argv[1])\n")
    sha = common.commit(ctx.dir, f"slice({slice_id}): implement hello")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"implemented app.py up to {N_CHECKS}",
                       "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    mbox = ol_common.mailbox_of(ctx)
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (app.py present).")


def _handle_integration_eval(ctx) -> None:
    ol_common.handle_integration_eval_ship(ctx)


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        _handle_integration_eval(ctx)
        return
    ctx.error("UnknownError", f"ol_acceptance.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-acceptance":
        _handle_acceptance(ctx)
    elif ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_acceptance.py: unexpected agent {ctx.agent!r}")

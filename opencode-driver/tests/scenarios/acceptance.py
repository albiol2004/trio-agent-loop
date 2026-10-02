"""r19 frozen-acceptance end-to-end scenario: a real, schema-valid pack
(mirrors ``native/tests/test_r19_acceptance.py``'s ``write_pack``/``CHECK``/
``GOAL`` fixture, ported here rather than imported across test packages),
authored by a fake ``trio-acceptance`` turn and run for real through
``metrics/trio-acceptance.py``'s own sandboxed check execution (the helper
ops this driver calls run in-process, unfaked).

Selected by ``$FAKE_OC_ACC_MODE`` (default ``"ship"``):

- ``ship``: happy path — 5 checks, one slice covers all of them, the
  builder implements every one, SHIP.
- ``ship_refused``: the builder under-implements iteration 1 (3 of 5); the
  frozen-acceptance SHIP gate turns the evaluator's SHIP into ITERATE;
  iteration 2's builder finishes the rest and it ships for real.
- ``coverage_ok``: the first plan leaves the last check unmapped; the
  driver's one re-plan (same prompt, now carrying the refusal) covers it.
- ``coverage_stop``: the first AND the re-plan both leave a check unmapped;
  the second refusal stops the loop before any builder runs.
- ``contaminated``: the author's first attempt's tool call reads the
  product repo (``$FAKE_OC_ACC_REPO``); the driver discards it and
  re-authors once, honestly, from a prompt carrying the contamination
  notice.
- ``retry``: the author's first pack has one check that already passes at
  the base commit; the helper asks for one validation retry, which the
  second attempt's clean (smaller) pack satisfies.

``$FAKE_OC_ACC_CHECKS`` (default 5) sets the ``ship``/``ship_refused``/
``coverage_*``/``contaminated`` pack size; ``retry`` always authors 6 then 4.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

MODE = os.environ.get("FAKE_OC_ACC_MODE", "ship")

QUOTE = "prints hello N"
CHECK = """\
import subprocess, sys
r = subprocess.run([sys.executable, "app.py", "{k}"], capture_output=True, text=True)
if r.returncode == 0 and "hello {k}" in r.stdout:
    sys.exit(0)
print("app.py does not print hello {k}")
sys.exit(1)
"""

_ATTEMPT_RE = re.compile(r"ACCEPTANCE-AUTHOR-RUN: \S+-a(\d+)")


def _n_checks() -> int:
    return int(os.environ.get("FAKE_OC_ACC_CHECKS", "5"))


def write_pack(export: Path, n: int, *, passing: tuple = ()) -> None:
    """Mirrors ``native/tests/test_r19_acceptance.py``'s ``write_pack``: a
    minimal but schema-valid pack whose non-``passing`` checks genuinely
    FAIL at the base commit (``app.py`` does not exist yet there) and PASS
    once a slice implements ``app.py`` up to that check's ``k``."""
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for k in range(1, n + 1):
        name = f"acc_{k:02d}.py"
        if k in passing:
            body = "import os, sys\nsys.exit(0 if os.path.isfile('README') else 1)\n"
        else:
            body = CHECK.format(k=k)
        (acc / "checks" / name).write_text(body, encoding="utf-8")
        checks.append({"id": f"ACC-{k:02d}", "goal_ref": "GOAL.md:2", "goal_quote": QUOTE,
                       "kind": "behaviour", "surface": "cli",
                       "run": ["python3", f"acceptance/checks/{name}"],
                       "expect": {"exit": 0}, "timeout_s": 30})
    (acc / "MANIFEST.json").write_text(json.dumps({
        "acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
        "checks": checks}, indent=2), encoding="utf-8")
    (acc / "AUTHOR.md").write_text("# inventory\n- testable-black-box: hello N\n",
                                   encoding="utf-8")


def _all_ids(n: int) -> list[str]:
    return [f"ACC-{k:02d}" for k in range(1, n + 1)]


def _render_plan(covers: list[str]) -> str:
    lines = ["# Plan", "", "```yaml", "slices:", "  - id: app",
             "    writes: [app.py]", "    reads: []",
             f"    covers: [{', '.join(covers)}]", "```", "",
             "## Verification standard", ""]
    return "\n".join(lines)


def _structured_plan(covers: list[str]) -> dict:
    return {"slices": [{"id": "app", "brief": "Implement app.py to pass the covered checks.",
                        "writes": ["app.py"], "reads": [], "depends": [], "covers": covers}],
            "lead_integration": [], "acceptance_bindings": {}, "notes": "one slice, one wave"}


def _author_attempt(ctx) -> int:
    m = _ATTEMPT_RE.search(ctx.prompt)
    return int(m.group(1)) if m else 1


# --------------------------------------------------------------- acceptance
def _handle_acceptance(ctx) -> None:
    export = Path(ctx.dir)
    attempt = _author_attempt(ctx)
    if MODE == "contaminated" and attempt == 1:
        repo = os.environ.get("FAKE_OC_ACC_REPO", "")
        # A persisted tool_use event reading the product repository's own
        # mailbox — exactly the hit native's own contamination test looks
        # for (metrics/trio-acceptance.py's audit_transcript, "a forbidden
        # root" = ctl.repo/ctl.mailbox).
        ctx.tool("bash", input={"command": f"cat {repo}/loop/PLAN.md"})
        write_pack(export, _n_checks())
        common.reply(ctx, {"checks": _n_checks(), "summary": "wrote the pack"})
        return
    if MODE == "retry" and attempt == 1:
        write_pack(export, 6, passing=(5, 6))
        common.reply(ctx, {"checks": 6, "summary": "wrote the pack"})
        return
    if MODE == "retry":  # attempt 2+: the author leaves the pack as-is —
        # the helper's own freeze_filter drops the passing-at-base checks
        # (ACC-05/06) on retry regardless, same as a native author who
        # makes no change (native/tests/test_r19_acceptance.py's own
        # retry test never rewrites the pack between attempts either).
        common.reply(ctx, {"checks": 6, "summary": "left the pack as-is"})
        return
    write_pack(export, _n_checks())
    common.reply(ctx, {"checks": _n_checks(), "summary": "wrote the pack"})


# -------------------------------------------------------------------- lead
def _handle_lead(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    if common.is_plan_call(ctx):
        n = 4 if MODE == "retry" else _n_checks()
        all_ids = _all_ids(n)
        refused = "COVERAGE REFUSED" in ctx.prompt
        if MODE == "coverage_stop" or (MODE == "coverage_ok" and not refused):
            covers = all_ids[:-1]  # the last id is deliberately left unmapped
        else:
            covers = all_ids
        common.write(mbox / "PLAN.md", _render_plan(covers))
        common.reply(ctx, _structured_plan(covers))
        return
    if common.is_integrate_call(ctx):
        iteration = common.iteration_of(ctx)
        branches = dict(re.findall(r"- (\S+): branch `([^`]+)`", ctx.prompt))
        for branch in branches.values():
            common.git(ctx.dir, "merge", "--no-ff", "--no-edit", branch)
        last = "(last wave)" in ctx.prompt
        merged = list(branches.keys())
        if last:
            common.write(mbox / "REPORT.md",
                        f"# Report — iteration {iteration}\n\nSlice app implemented by a builder.\n")
            common.append_log(mbox, f"- iter {iteration} | lead | shipped slice app")
            common.commit(ctx.dir, f"loop: iteration {iteration} report", ["loop"])
        common.reply(ctx, {"merged": merged, "conflicts": [], "summary": "merged app cleanly"})
        return
    ctx.error("UnknownError", f"acceptance.py: unexpected lead call: {ctx.prompt[:160]!r}")


# ----------------------------------------------------------------- builder
def _handle_builder(ctx) -> None:
    iteration = common.iteration_of(ctx)
    n = 4 if MODE == "retry" else _n_checks()
    upto = n
    if MODE == "ship_refused":
        upto = 3 if iteration == 1 else n
    ok = ", ".join(str(k) for k in range(1, upto + 1))
    path = Path(ctx.dir) / "app.py"
    common.write(path, "import sys\n"
                 f"if int(sys.argv[1]) in ({ok},):\n    print('hello', sys.argv[1])\n")
    sha = common.commit(ctx.dir, "slice(app): implement hello")
    common.reply(ctx, {"summary": f"implemented app.py up to {upto}", "head": sha})


# --------------------------------------------------------------- evaluator
def _handle_evaluator(ctx) -> None:
    mbox = common.mailbox_dir(ctx)
    iteration = common.iteration_of(ctx, mbox)
    attempt, sha = common.pin_attempt_and_sha(ctx)
    common.write(mbox / "VERDICT.md",
                f"VERDICT: SHIP\n# Verdict — iteration {iteration}\n"
                f"attempt: {attempt}\nevaluated: {sha}\ncommit: {sha}\n")
    common.commit(ctx.dir, f"loop: iteration {iteration} — SHIP", ["loop/VERDICT.md"])
    ctx.text(f"SHIP. The frozen acceptance pack and PLAN.md both check out. "
            f"attempt: {attempt} evaluated: {sha}")


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
        ctx.error("UnknownError", f"acceptance.py: unexpected agent {ctx.agent!r}")

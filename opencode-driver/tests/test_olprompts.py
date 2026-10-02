"""olprompts.py — shared open-loop role prompt texts for the OpenCode driver.

Ported from the stopped `parity` branch's `native/tests/test_ol_prompts.py`
(commit b74a38b). Loads `trio_opencode.olprompts` the normal package way
(conftest.py puts `opencode-driver/` on sys.path) and `omnigent/trioctl`'s
source by `ast` parsing (never imported or executed) for the drift checks.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from trio_opencode import olprompts as ol

TESTS_DIR = Path(__file__).resolve().parent
OPENCODE_DRIVER_ROOT = TESTS_DIR.parent
REPO = OPENCODE_DRIVER_ROOT.parent
TRIOCTL = REPO / "omnigent" / "trioctl"


def _trioctl_constant(name: str) -> str:
    """Load one module-level string constant from omnigent/trioctl by
    parsing it with `ast` -- the source is never imported or executed."""
    src = TRIOCTL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            value = node.value
            assert isinstance(value, ast.Constant) and isinstance(value.value, str), name
            return value.value
    raise AssertionError(f"{name} not found in {TRIOCTL}")


# ------------------------------------------------------------- ctx fixtures

def _common_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = {
        "mailbox": "/mb/loop",
        "iteration": 3,
        "repo": "/repo",
        "driver": "opencode",
        "output": "structured",
        "notes": [],
        "human_answer": None,
        "tmpdir": None,
    }
    ctx.update(overrides)
    return ctx


def _lead_plan_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = _common_ctx(**overrides.pop("common", {}))
    ctx.update({
        "queue_errors": [],
        "acceptance_errors": [],
        "open_faults": [],
        "retired": [],
        "refusals": [],
        "repos": [],
    })
    ctx.update(overrides)
    return ctx


def _builder_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = _common_ctx(**overrides.pop("common", {}))
    ctx.update({
        "slice": {"id": "status-parse", "brief": "Do the thing.", "writes": ["a.py"], "repo": "home", "fault": None},
        "worktree": "/repo/.worktrees/status-parse",
        "base": "a" * 40,
        "branch": "worktree-status-parse",
    })
    ctx.update(overrides)
    return ctx


def _lead_review_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = _common_ctx(**overrides.pop("common", {}))
    ctx.update({
        "results": [
            {"id": "status-parse", "status": "retired", "sha": "b" * 40,
             "targeted_check": "TARGETED_CHECK: 4 passed in 0.10s", "summary": "did it", "reason": None},
        ],
        "pass_slices": ["status-parse"],
        "takeovers": [],
    })
    ctx.update(overrides)
    return ctx


def _slice_eval_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = _common_ctx(**overrides.pop("common", {}))
    ctx.update({
        "slice": "status-parse",
        "sha": "c" * 40,
        "eval_worktree": "/repo/.worktrees/eval-status-parse",
        "repo_name": "home",
        "shadow": "/release/metrics/trio-shadow.py",
        "lead_worktree": "/repo",
        "acceptance_covered": None,
    })
    ctx.update(overrides)
    return ctx


def _integration_eval_ctx(**overrides: Any) -> dict[str, Any]:
    ctx = _common_ctx(**overrides.pop("common", {}))
    ctx.update({
        "sha": "d" * 40,
        "attempt": "e" * 8,
        "eval_worktree": "/repo/.worktrees/eval-integration",
        "pins": {"home": "d" * 40, "app-backend": "f" * 40},
        "repo_worktrees": {"app-backend": "/repo/.worktrees/eval-app-backend"},
        "lead_worktree": "/repo",
        "acceptance": None,
    })
    ctx.update(overrides)
    return ctx


_FACTORY = {
    "lead-plan": _lead_plan_ctx,
    "builder": _builder_ctx,
    "lead-review": _lead_review_ctx,
    "slice-eval": _slice_eval_ctx,
    "integration-eval": _integration_eval_ctx,
}


# ------------------------------------------------------------------ basics

def test_kinds_tuple():
    assert ol.KINDS == ("lead-plan", "lead-review", "builder", "slice-eval", "integration-eval")


@pytest.mark.parametrize("kind", ol.KINDS)
def test_renders_with_full_ctx(kind):
    text = ol.render(kind, _FACTORY[kind]())
    assert isinstance(text, str) and text
    assert text.startswith("OPEN-LOOP CONTEXT: kind=" + kind)


@pytest.mark.parametrize("fn", [ol.render, ol.required_keys, ol.context_line])
def test_unknown_kind_raises(fn):
    with pytest.raises(ValueError):
        if fn is ol.render:
            fn("not-a-kind", {})
        else:
            fn("not-a-kind")


@pytest.mark.parametrize("kind", ol.KINDS)
def test_missing_required_key_raises(kind):
    # ACCEPT: render(kind, ctx missing a key) -> ValueError naming the key | oracle: refusal
    ctx = _FACTORY[kind]()
    for key in ol.required_keys(kind):
        bad = dict(ctx)
        del bad[key]
        with pytest.raises(ValueError, match=key):
            ol.render(kind, bad)


@pytest.mark.parametrize("kind", ol.KINDS)
def test_required_keys_superset_of_common(kind):
    keys = ol.required_keys(kind)
    for k in ("mailbox", "iteration", "repo", "driver", "output", "notes", "human_answer", "tmpdir"):
        assert k in keys


def test_lead_review_requires_takeovers_key():
    assert "takeovers" in ol.required_keys("lead-review")


def test_slice_eval_and_integration_eval_do_not_require_optional_keys():
    # quality_note / rigor are optional: rendering without them must not raise.
    assert "quality_note" not in ol.required_keys("slice-eval")
    assert "rigor" not in ol.required_keys("integration-eval")
    ol.render("slice-eval", _slice_eval_ctx())
    ol.render("integration-eval", _integration_eval_ctx())


# ------------------------------------------------------------ driver default

def test_not_router_defaults_to_opencode_when_driver_falsy():
    ctx = _lead_plan_ctx(driver=None)
    text = ol.render("lead-plan", ctx)
    assert "trio-opencode driver" in text
    assert "trio-native driver" not in text


# ------------------------------------------------------------ context_line

def test_context_line_matches_trioctl_format():
    # Replicates trioctl's own line-building logic (omnigent/trioctl ~6144-6150)
    # byte-for-byte: "OPEN-LOOP CONTEXT: kind=<kind>[ slice=<id>][ sha=<sha>]".
    def trioctl_line(kind, slice_id=None, sha=None):
        line = f"OPEN-LOOP CONTEXT: kind={kind}"
        if slice_id:
            line += f" slice={slice_id}"
        if sha:
            line += f" sha={sha}"
        return line

    cases = [
        ("lead-plan", None, None),
        ("lead-review", None, None),
        ("builder", "status-parse", None),
        ("slice-eval", "status-parse", "a" * 40),
        ("integration-eval", None, "b" * 40),
    ]
    for kind, slice_id, sha in cases:
        assert ol.context_line(kind, slice_id, sha) == trioctl_line(kind, slice_id, sha)


def test_context_line_no_slice_no_sha():
    assert ol.context_line("lead-plan") == "OPEN-LOOP CONTEXT: kind=lead-plan"


# ------------------------------------------------------------- slice-eval

def test_slice_eval_heading_shadow_path_no_worktree_add():
    ctx = _slice_eval_ctx(slice="my-slice", sha="1" * 40, shadow="/abs/metrics/trio-shadow.py")
    text = ol.render("slice-eval", ctx)
    assert "## slice my-slice @" + "1" * 40 + " — SHIP" in text
    assert "## slice my-slice @" + "1" * 40 + " — ITERATE" in text
    assert "/abs/metrics/trio-shadow.py" in text
    # step 2 must tell the role NOT to run it (never a positive instruction
    # to create/remove a worktree of its own, unlike lockstep evals).
    assert "git worktree add <tmp>" not in text
    assert "Do NOT run" in text
    assert "`git worktree add`/`remove`" in text
    assert ctx["eval_worktree"] in text
    assert ctx["mailbox"] in text


def test_slice_eval_drift_matches_trioctl_after_substitutions():
    trioctl_src = _trioctl_constant("_OPEN_LOOP_SLICE_EVAL_PROCEDURE")
    slice_id, sha = "status-parse", "2" * 40
    formatted = trioctl_src.format(slice=slice_id, sha=sha)
    tail = formatted[formatted.index("3. Append"):]  # steps 3-5, untouched by the port
    text = ol.render("slice-eval", _slice_eval_ctx(slice=slice_id, sha=sha))
    assert tail in text
    # The intro line (minus the literal "<mailbox>" placeholder step 1 drops) survives too.
    intro = formatted[:formatted.index("1. Run")]
    assert intro.strip() in text


def test_slice_eval_quality_note_appended_verbatim_after_procedure():
    # ACCEPT: render('slice-eval', ctx with quality_note) -> text contains
    # OPEN-LOOP CONTEXT line, the verbatim trioctl steps 3-5 and the quality
    # note | oracle: value
    trioctl_src = _trioctl_constant("_OPEN_LOOP_SLICE_EVAL_PROCEDURE")
    slice_id, sha = "status-parse", "9" * 40
    formatted = trioctl_src.format(slice=slice_id, sha=sha)
    tail = formatted[formatted.index("3. Append"):]
    note = "## SLICE QUALITY\nbase-revert: none\nauthor: builder-3\n"
    ctx = _slice_eval_ctx(slice=slice_id, sha=sha, quality_note=note)
    text = ol.render("slice-eval", ctx)
    assert text.startswith("OPEN-LOOP CONTEXT: kind=slice-eval slice=" + slice_id + " sha=" + sha)
    assert tail in text
    assert note.rstrip("\n") in text
    # the note comes after the procedure body, not before it
    assert text.index(tail) < text.index(note.rstrip("\n"))


def test_slice_eval_no_quality_note_when_absent():
    text = ol.render("slice-eval", _slice_eval_ctx())
    assert "SLICE QUALITY" not in text


def test_slice_eval_multi_repo_note_when_repo_name_declared():
    ctx = _slice_eval_ctx(
        slice="be-slice", sha="7" * 40, repo_name="app-backend",
        eval_worktree="/repo/.worktrees/eval-be-slice",
    )
    text = ol.render("slice-eval", ctx)
    trioctl_src = _trioctl_constant("_OPEN_LOOP_MULTI_REPO_SLICE_EVAL_NOTE")
    first = trioctl_src.split("Step\n")[0].format(
        slice="be-slice", sha="7" * 40, name="app-backend", path="/repo/.worktrees/eval-be-slice",
    )
    # trioctl's first sentence is verbatim; its `git worktree add` step-2
    # sentence is replaced by the already-checked-out worktree.
    assert first in text
    assert "Step\n2's worktree `/repo/.worktrees/eval-be-slice` is a checkout of that repo at `" + "7" * 40 + "`" in text
    assert "git -C /repo/.worktrees/eval-be-slice worktree add" not in text


@pytest.mark.parametrize("repo_name", [None, "home", "."])
def test_slice_eval_no_multi_repo_note_for_home(repo_name):
    ctx = _slice_eval_ctx(repo_name=repo_name)
    text = ol.render("slice-eval", ctx)
    assert "MULTI-REPO: slice" not in text


# -------------------------------------------------------- integration-eval

def test_integration_eval_fields_and_repo_worktrees():
    ctx = _integration_eval_ctx(sha="3" * 40, iteration=7, attempt="zz999999")
    text = ol.render("integration-eval", ctx)
    assert "iteration: 7" in text
    assert "attempt: zz999999" in text
    assert "evaluated: " + "3" * 40 in text
    for path in ctx["repo_worktrees"].values():
        assert path in text
    for name in ctx["repo_worktrees"]:
        assert name in text
    assert ctx["lead_worktree"] in text
    assert ctx["mailbox"] in text


def test_integration_eval_drift_matches_trioctl_verbatim():
    trioctl_src = _trioctl_constant("_OPEN_LOOP_INTEGRATION_EVAL_PROCEDURE")
    ctx = _integration_eval_ctx(sha="4" * 40, iteration=9, attempt="deadbeef")
    formatted = trioctl_src.format(sha=ctx["sha"], iteration=ctx["iteration"], attempt=ctx["attempt"])
    text = ol.render("integration-eval", ctx)
    assert formatted in text


def test_integration_eval_rigor_appended_after_procedure():
    rigor = "## Whole-goal rigor addendum\nprobe the billing export end to end.\n"
    ctx = _integration_eval_ctx(pins={"home": "5" * 40}, repo_worktrees={}, rigor=rigor)
    text = ol.render("integration-eval", ctx)
    assert rigor.rstrip("\n") in text
    trioctl_src = _trioctl_constant("_OPEN_LOOP_INTEGRATION_EVAL_PROCEDURE")
    formatted = trioctl_src.format(sha=ctx["sha"], iteration=ctx["iteration"], attempt=ctx["attempt"])
    assert text.index(formatted.rstrip("\n")) < text.index(rigor.rstrip("\n"))


def test_integration_eval_no_rigor_when_absent():
    text = ol.render("integration-eval", _integration_eval_ctx(pins={"home": "5" * 40}, repo_worktrees={}))
    assert "Whole-goal rigor addendum" not in text


def test_integration_eval_multi_repo_note_when_multiple_pins():
    ctx = _integration_eval_ctx(
        pins={"home": "1" * 40, "app-backend": "2" * 40},
        repo_worktrees={"app-backend": "/repo/.worktrees/eval-app-backend"},
    )
    text = ol.render("integration-eval", ctx)
    assert "MULTI-REPO (PLAN.md declares `repos:`)" in text
    assert "app-backend" in text
    assert "evaluated: home@" + "1" * 40 + ", app-backend@" + "2" * 40 in text
    assert ctx["lead_worktree"] in text


def test_integration_eval_no_multi_repo_note_for_single_pin():
    ctx = _integration_eval_ctx(pins={"home": "1" * 40}, repo_worktrees={})
    text = ol.render("integration-eval", ctx)
    assert "MULTI-REPO (PLAN.md declares `repos:`)" not in text


# ----------------------------------------------------------------- lead-plan

def test_lead_plan_blocks_present_when_nonempty():
    ctx = _lead_plan_ctx(
        queue_errors=["slice x: bad shape"],
        acceptance_errors=["ACC-03 unmapped"],
        refusals=["id unmapped: ACC-09"],
        open_faults=[{"id": "f1", "slice": "status-parse", "scope": "local:a.py",
                      "reason": "flaky", "status": "open", "observed_at": "a" * 40}],
        retired=[{"slice": "status-parse", "sha": "b" * 40}],
        repos=[{"name": "app-backend", "path": "/repo/app-backend"}],
    )
    text = ol.render("lead-plan", ctx)
    assert "QUEUE.md PARSE ERRORS" in text and "slice x: bad shape" in text
    assert "ACCEPTANCE ERRORS FROM THE DRIVER" in text and "ACC-03 unmapped" in text
    assert "PLAN REFUSED" in text and "ACC-09" in text
    assert "OPEN FAULTS (orientation):" in text and "f1" in text and "flaky" in text
    assert "RETIRED SLICES (latest per slice" in text and "status-parse" in text
    assert "Multi-repo:" in text and "app-backend" in text
    assert "MULTI-REPO (PLAN.md declares `repos:`)" in text
    # driver-owned rewrite: never tell the Lead to dispatch/merge/retire itself
    assert "do NOT dispatch builders" in text
    assert "do NOT append `retired:` entries" in text


def test_lead_plan_blocks_omitted_when_empty():
    text = ol.render("lead-plan", _lead_plan_ctx())
    assert "QUEUE.md PARSE ERRORS" not in text
    assert "ACCEPTANCE ERRORS FROM THE DRIVER" not in text
    assert "PLAN REFUSED" not in text
    assert "OPEN FAULTS: none." in text
    assert "RETIRED SLICES: none yet." in text
    assert "Multi-repo:" not in text
    assert "MULTI-REPO (PLAN.md declares `repos:`)" not in text


def test_lead_plan_field_list_includes_targeted_check():
    text = ol.render("lead-plan", _lead_plan_ctx())
    assert "`targeted_check` (the exact command from" in text
    assert "`id`, `brief`, `writes`, `reads`, `depends`, `repo`, `targeted_check`, `fault`" in text


# ------------------------------------------------------------------ builder

def test_builder_with_worktree():
    ctx = _builder_ctx(worktree="/repo/.worktrees/w1", branch="worktree-w1", base="a" * 40)
    text = ol.render("builder", ctx)
    assert "Work ONLY in `/repo/.worktrees/w1`" in text
    assert "worktree-w1" in text
    assert "isolated worktree created for you" not in text


def test_builder_without_worktree():
    ctx = _builder_ctx(worktree=None, branch=None)
    text = ol.render("builder", ctx)
    assert "Work ONLY in" not in text
    assert "isolated git worktree created for you" in text
    assert "git reset --hard" in text


def test_builder_fault_commit_form():
    ctx = _builder_ctx()
    ctx["slice"] = dict(ctx["slice"], fault="f7")
    text = ol.render("builder", ctx)
    assert "slice(status-parse): fix f7 <summary>" in text


def test_builder_no_fault_commit_form():
    text = ol.render("builder", _builder_ctx())
    assert "slice(status-parse): <summary>" in text


# -------------------------------------------------------------- lead-review

def test_lead_review_mentions_results_and_never_retires():
    ctx = _lead_review_ctx(results=[
        {"id": "a", "status": "retired", "sha": "b" * 40, "targeted_check": "TARGETED_CHECK: 1 passed in 0.01s", "summary": "ok", "reason": None},
        {"id": "b", "status": "conflict", "sha": None, "targeted_check": None, "summary": "merge conflict", "reason": "overlap"},
    ], pass_slices=["a", "b"])
    text = ol.render("lead-review", ctx)
    assert "never edit or append `retired:` entries" in text
    assert "never revert a driver merge" in text
    assert "a: retired" in text
    assert "b: conflict" in text
    assert "Refused/conflicted slices" in text and "b" in text.split("Refused/conflicted slices")[1]


def test_lead_review_no_gate_fix_retired_entry_contradiction():
    # S4b fix: step 3 must no longer tell the Lead to append a NEW
    # `retired:` entry itself for a gate fix -- the driver does that.
    text = ol.render("lead-review", _lead_review_ctx())
    assert "plus a NEW `retired:` entry at the fix sha" not in text
    assert "the driver re-runs that slice's targeted check and appends the new " \
        "`retired:` entry itself after this call" in text


def test_lead_review_takeover_block_present_when_nonempty():
    ctx = _lead_review_ctx(takeovers=[
        {"id": "status-parse", "reason": "builder failed twice", "targeted_check": "pytest -q tests/test_x.py"},
    ])
    text = ol.render("lead-review", ctx)
    assert "TAKE-OVER" in text
    assert "the driver's builder failed twice for these slices" in text
    assert "status-parse" in text
    assert "builder failed twice" in text
    assert "pytest -q tests/test_x.py" in text


def test_lead_review_takeover_block_absent_when_empty():
    text = ol.render("lead-review", _lead_review_ctx(takeovers=[]))
    assert "TAKE-OVER" not in text


# -------------------------------------------------------------- output modes

@pytest.mark.parametrize("kind", ol.KINDS)
def test_output_modes(kind):
    structured = ol.render(kind, _FACTORY[kind](output="structured"))
    fenced = ol.render(kind, _FACTORY[kind](output="fenced-json"))
    assert "Return through the structured output:" in structured
    assert "End your final message with exactly one fenced ```json block:" in fenced


# --------------------------------------------------------- notes / human answer

@pytest.mark.parametrize("kind", ol.KINDS)
def test_notes_and_human_answer_appended_verbatim(kind):
    notes = ["ROOT-FREE: work in the loop's Lead worktree.", "MULTI-REPO: see repos above."]
    human = "## Verified human answer (driver)\nid: h1\nanswer: use option B\n"
    ctx = _FACTORY[kind](notes=notes, human_answer=human)
    text = ol.render(kind, ctx)
    stripped = text.rstrip("\n")
    assert stripped.endswith(human.rstrip("\n"))
    for note in notes:
        assert note in text
    # order: both notes must precede the human answer block
    assert text.index(notes[0]) < text.index(notes[1]) < text.index("Verified human answer")


# ------------------------------------------------------------------- guard

@pytest.mark.parametrize("kind", ol.KINDS)
def test_no_omnigent_or_dispatch_script_tokens(kind):
    guard = re.compile(r"trioctl|omnigent|cursor|\.dispatch/.*\.sh", re.IGNORECASE)
    for worktree in (None, "/repo/.worktrees/w1"):
        for output in ("structured", "fenced-json"):
            ctx = _FACTORY[kind](output=output)
            if kind == "builder":
                ctx["worktree"] = worktree
                ctx["branch"] = None if worktree is None else "worktree-w1"
            if kind == "lead-plan":
                ctx["repos"] = [{"name": "app-backend", "path": "/repo/app-backend"}]
            if kind == "lead-review":
                ctx["takeovers"] = [
                    {"id": "x", "reason": "twice", "targeted_check": "pytest -q t.py"},
                ]
            if kind == "slice-eval":
                ctx["repo_name"] = "app-backend"
            if kind == "integration-eval":
                ctx["pins"] = {"home": "1" * 40, "app-backend": "2" * 40}
                ctx["repo_worktrees"] = {"app-backend": "/repo/.worktrees/eval-app-backend"}
            text = ol.render(kind, ctx)
            assert not guard.search(text), f"{kind}: forbidden token found in:\n{text}"


def test_integration_eval_multi_repo_retirement_goes_to_the_aggregate():
    """A declared repo's SHIP retirement commit is made on its checked-out
    base branch (its Lead aggregate), never in the detached grading copy."""
    ctx = _integration_eval_ctx(retire_paths={"app-backend": "/agg/app-backend"})
    text = ol.render("integration-eval", ctx)
    assert "  - `app-backend`: `/agg/app-backend` @`" + "f" * 40 + "`" in text
    assert "its SHIP retirement commit goes in its Lead aggregate `/agg/app-backend`" in text
    assert "  - `app-backend`: `/repo/.worktrees/eval-app-backend`" not in text


def test_integration_eval_isolation_off_preface_allows_retirement_in_lead_worktree():
    ctx = _integration_eval_ctx(eval_worktree="/repo", pins={"home": "5" * 40}, repo_worktrees={})
    text = ol.render("integration-eval", ctx)
    assert "worker isolation off" in text
    assert "never commit in it" not in text
    assert "SHIP retirement commit(s) are made in this Lead worktree" in text
    isolated = ol.render("integration-eval", _integration_eval_ctx(pins={"home": "5" * 40},
                                                                    repo_worktrees={}))
    assert "ISOLATED EVALUATOR WORKSPACE" in isolated and "worker isolation off" not in isolated

"""r19 C1: slice-evals back to fast; rigor split core/integration.

Rendered through the real ``OmnigentRunner._prompt`` (root-bound and
root-free): the slice-eval render carries no independent probe, no
per-accept table and no "at least two attacks" duty, but a SHIP section
needs an `attacks:` list with one counted probe beyond the accepts (the
acceptance-strength floor rule); the integration-eval and lockstep renders
carry all three whole-goal duties; receipt-never-PASS is in every
evaluator render.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


T = _load("trioctl_r19_c1", ROOT / "omnigent" / "trioctl")
RF = {"home": "/work/repo", "lead": "/work/lead", "branch": "trio/m", "target": "main",
      "target_base": "d" * 40, "mailbox_rel": "loop", "slug": "loop", "repo_targets": {}}
SLICE = {"mode": "open-loop", "kind": "slice-eval", "slice": "s1", "sha": "a" * 40}
INTEGRATION = {"mode": "open-loop", "kind": "integration-eval", "slice": None, "sha": None,
               "pinned_sha": "b" * 40, "evaluator_attempt": "a1", "iteration": 3}
LOCKSTEP = {"evaluator_attempt": "a1", "pinned_sha": "c" * 40}

WHOLE_GOAL_MARKERS = ("attacks:", "Independent probe", "| # | criterion |")
#: What a slice-eval render carries instead of the whole-goal attack duty.
SLICE_ATTACK_MARKERS = (
    "Frozen checks are a floor, not the finish line",
    "an `attacks:` list",
    "needs at least one counted attack beyond the slice's accepts",
    "The driver refuses a SHIP section with no counted attack",
    "the slice-eval is re-dispatched",
)
RECEIPT = "A receipt alone is never PASS"


def _render(tmp_path: Path, ctx: dict, root_free: bool) -> str:
    mailbox = tmp_path / "mb"
    mailbox.mkdir(exist_ok=True)
    kw = {"root_free": RF} if root_free else {}
    runner = T.OmnigentRunner(repo=tmp_path, **kw)
    return runner._prompt("evaluator", 3, mailbox, dict(ctx))


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("root_free", [False, True], ids=["root-bound", "root-free"])
def test_slice_eval_render_is_fast(tmp_path, root_free):
    text = _flat(_render(tmp_path, SLICE, root_free))
    for marker in ("Independent probe", "| # | criterion |", "| # | accept |", "at least two"):
        assert marker not in text, marker
    for marker in ("attacks:",) + SLICE_ATTACK_MARKERS:
        assert marker in text, marker
    assert RECEIPT in text
    assert "`evidence: re-run=<n> implementer-test=<n> receipt=<n> unverified=<n>`" in text


@pytest.mark.parametrize("ctx", [INTEGRATION, LOCKSTEP], ids=["integration", "lockstep"])
@pytest.mark.parametrize("root_free", [False, True], ids=["root-bound", "root-free"])
def test_whole_goal_renders_carry_the_integration_rigor(tmp_path, ctx, root_free):
    raw = _render(tmp_path, ctx, root_free)
    text = _flat(raw)
    for marker in WHOLE_GOAL_MARKERS:
        assert marker in text, marker
    assert RECEIPT in text
    assert len(re.findall(r"^## Whole-goal verification rigor$", raw, re.M)) == 1


def test_integration_rigor_prompt_is_found_like_every_role_prompt(tmp_path):
    runner = T.OmnigentRunner(repo=tmp_path)
    path = runner._prompt_path(T.INTEGRATION_RIGOR_PROMPT)
    assert path.name == "integration-rigor.md"
    assert path.parent == ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts"


def test_slice_evidence_without_attacks_is_na_and_with_attacks_counts():
    body = ("## slice s1 @" + "a" * 40 + " — SHIP\n"
            "evidence: re-run=1 implementer-test=2 receipt=0 unverified=0\n")
    got = T.slice_evidence(body, "s1", "a" * 40)
    assert got["attacks"] == "n/a" and got["evidence"]["implementer-test"] == 2
    got = T.slice_evidence(body + "attacks:\n- x -> y\n", "s1", "a" * 40)
    assert got["attacks"] == 1


def test_generator_splits_core_and_integration():
    gen = _load("gen_r19_c1", ROOT / "prompts" / "generate.py")
    core = " ".join(gen.rigor_content().split())
    integ = " ".join(gen.integration_rigor_content().split())
    assert RECEIPT in core and RECEIPT not in integ
    # the floor rule and the slice SHIP `attacks:` requirement ride the core
    # (every evaluator dispatch); the "at least two" duty stays whole-goal.
    assert "Frozen checks are a floor, not the finish line" in core
    assert "carries an `attacks:` list" in core and "at least two" not in core
    for marker in ("### Independent probe", "at least two concrete attacks",
                   "### Data-work profile", "per receipt family"):
        assert marker in integ and marker not in core, marker
    assert gen.RIGOR_PIECES == gen.RIGOR_CORE + gen.RIGOR_INTEGRATION

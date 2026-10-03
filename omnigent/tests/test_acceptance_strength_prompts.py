"""Acceptance strength, prompt side: frozen checks are a floor, and the
author maps every requirement to its checks.

Rule 1: passing the frozen pack (or a slice's accepts) is necessary, never
sufficient -- before ANY SHIP (per-slice open-loop sections included) the
Evaluator records at least one attack beyond it in an `attacks:` list, and
the driver refuses a SHIP with no counted attack.
Rule 2: the acceptance author lists every GOAL requirement in MANIFEST.json
under `requirements`, each with a normal and a HARD check.

Every generated harness copy, the two hand-maintained slice-eval procedure
strings (trioctl, the OpenCode driver) and the evaluator/lead fragments are
pinned on short, stable phrases (never whole paragraphs). Generated files
are read as written to the tree, so a stale generation also fails here.
"""
from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRIOCTL = ROOT / "omnigent" / "trioctl"
OLPROMPTS = ROOT / "opencode-driver" / "trio_opencode" / "olprompts.py"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


GEN = _load("gen_acceptance_strength", ROOT / "prompts" / "generate.py")


def _flat(text: str) -> str:
    """Whitespace-flattened; JS/JSON string escapes (\\n, \\") undone so the
    embedded native Workflow constants read like the markdown."""
    return " ".join(text.replace("\\n", " ").replace('\\"', '"').split())


def _read(rel: str) -> str:
    return _flat((ROOT / rel).read_text(encoding="utf-8"))


def _constant(path: Path, name: str) -> str:
    """One module-level string constant, by ast (never imported or executed)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            return _flat(node.value.value)
    raise AssertionError(f"{name} not found in {path}")


# ---------------------------------------------------------------- Rule 1
FLOOR = "Frozen checks are a floor, not the finish line (every SHIP)"
FLOOR_BEYOND = "run at least one adversarial probe beyond that floor"
FLOOR_REFUSED = "a SHIP with no counted attack"
SLICE_ATTACKS = "Every open-loop slice section carries an `attacks:` list"
SLICE_REFUSED = "a slice SHIP section is not accepted"
WHOLE_GOAL_TWO = "at least two concrete attacks"

#: Every generated role file that carries the full canonical evaluator.
EVALUATOR_BODIES = (
    ".claude/agents/trio-evaluator.md",
    ".codex/agents/trio-evaluator.toml",
    "codex/agents/trio-evaluator.toml",
    "codex/skills/trio/references/prompts/evaluator.md",
    "kimi/skills/trio/references/prompts/evaluator.md",
    "omp/agents/trio-evaluator.md",
    "opencode/agents/trio-evaluator.md",
    "opencode-driver/agents/trio-evaluator.md",
    "portable/prompts/evaluator.md",
)
#: The Omnigent evaluator carries the canonical core rigor in a marked block.
OMNIGENT_CORE = (
    "omnigent/trio-omnigent-roles/evaluator/config.yaml",
    "omnigent/entrypoints/trio-omnigent/prompts/evaluator.md",
)
#: Whole-goal rigor, appended to integration-eval and lockstep prompts only.
WHOLE_GOAL_RIGOR = (
    "omnigent/entrypoints/trio-omnigent/prompts/integration-rigor.md",
    "opencode-driver/prompts/integration-rigor.md",
)


@pytest.mark.parametrize("rel", EVALUATOR_BODIES)
def test_every_evaluator_body_states_the_floor_and_the_slice_attacks(rel):
    text = _read(rel)
    for phrase in (FLOOR, FLOOR_BEYOND, FLOOR_REFUSED, SLICE_ATTACKS, SLICE_REFUSED,
                   WHOLE_GOAL_TWO, "- <command or input> -> <what happened>",
                   "Re-running a frozen check or an accept's own test is not an attack"):
        assert phrase in text, (rel, phrase)
    # the old design said slices carry no attacks
    assert "attacks or probe: slice-evals stay fast" not in text, rel
    assert "attacks and probe are whole-goal duties only" not in text, rel


def test_generated_evaluator_bodies_cover_every_flavor():
    """A new harness's evaluator body must be added to EVALUATOR_BODIES above."""
    found = sorted(
        str(path.relative_to(ROOT)) for path, text in GEN.all_outputs().items()
        if "# Role: Evaluator (adversarial verify)" in text)
    assert sorted(EVALUATOR_BODIES) == found


@pytest.mark.parametrize("rel", OMNIGENT_CORE)
def test_omnigent_evaluator_rigor_block_states_the_floor_and_slice_attacks(rel):
    text = _read(rel)
    for phrase in (FLOOR, FLOOR_BEYOND, FLOOR_REFUSED, SLICE_ATTACKS, SLICE_REFUSED):
        assert phrase in text, (rel, phrase)
    assert "slice-evals stay fast" in text  # no per-accept table, no probe
    assert "attacks or probe: slice-evals stay fast" not in text


@pytest.mark.parametrize("rel", WHOLE_GOAL_RIGOR)
def test_whole_goal_rigor_aims_attacks_beyond_the_pack(rel):
    text = _read(rel)
    for phrase in (WHOLE_GOAL_TWO, "the driver refuses a SHIP with no counted attack",
                   "Your attacks go beyond the frozen pack", "`requirement_gaps:`",
                   "AUTHOR.md dropped or marked live-only", "malformed input",
                   "concurrency or handoff"):
        assert phrase in text, (rel, phrase)


def test_trioctl_slice_eval_procedure_requires_attacks_for_ship():
    text = _constant(TRIOCTL, "_OPEN_LOOP_SLICE_EVAL_PROCEDURE")
    for phrase in ("then an `attacks:` list", "a SHIP section needs at least one counted attack",
                   "beyond the slice's accepts and the frozen pack",
                   "The driver refuses a SHIP section with no counted attack",
                   "the slice-eval is re-dispatched",
                   "no per-accept table and no independent probe"):
        assert phrase in text, phrase
    assert "no attacks and no independent probe" not in text


def test_opencode_slice_eval_steps_match_trioctl_and_require_attacks():
    text = _constant(OLPROMPTS, "_SLICE_EVAL_STEPS_3_5")
    for phrase in ("then an `attacks:` list", "a SHIP section needs at least one counted attack",
                   "The driver refuses a SHIP section with no counted attack",
                   "the slice-eval is re-dispatched"):
        assert phrase in text, phrase
    assert "no attacks and no independent probe" not in text
    # hand copy of trioctl's steps 3-5 (slice/sha placeholders left unformatted)
    trioctl = _constant(TRIOCTL, "_OPEN_LOOP_SLICE_EVAL_PROCEDURE")
    assert text.split("3. Append", 1)[1].split("4. SHIP", 1)[0] == \
        trioctl.split("3. Append", 1)[1].split("4. SHIP", 1)[0]


# ---------------------------------------------------------------- Rule 2
AUTHOR_PROMPTS = (
    "prompts/canonical/acceptance.md",
    ".claude/agents/trio-acceptance.md",
    "opencode-driver/agents/trio-acceptance.md",
    "omnigent/trio-omnigent-roles/acceptance/config.yaml",
    "omnigent/entrypoints/trio-omnigent/prompts/acceptance.md",
)


@pytest.mark.parametrize("rel", AUTHOR_PROMPTS)
def test_author_prompt_maps_every_requirement_to_normal_and_hard_checks(rel):
    text = _read(rel)
    for phrase in ('"requirements": [',
                   '"normal": ["ACC-01"]',
                   '"hard": [{"check": "ACC-02", "type": "boundary"}',
                   '{"check": "ACC-05", "type": "interaction", "with": "R3"}',
                   "at least one normal check", "HARD check",
                   "`boundary`", "`interaction`", "`failure-mode`",
                   "`malformed-input`", "`concurrency`",
                   "never both", "`REQUIREMENT GAP: ...`", "ONE retry",
                   "requirements: R2 has no hard check", "requirement_gaps:"):
        assert phrase in text, (rel, phrase)
    # the unchanged contract the generators and the validate step rely on
    assert "# Role: Acceptance Author — frozen acceptance, once per loop" in text
    assert "8–25 checks" in text


def test_author_prompt_keeps_the_validate_sentence_and_export_placeholder():
    canonical = (ROOT / "prompts" / "canonical" / "acceptance.md").read_text(encoding="utf-8")
    assert GEN._OMNIGENT_VALIDATE in canonical and "{export}" in canonical


# ----------------------------------------------- evaluator / lead fragments
EVALUATOR_FRAGMENTS = (
    "prompts/canonical/acceptance-evaluator.md",
    "prompts/canonical/acceptance-native-evaluator.md",
    "omnigent/entrypoints/trio-omnigent/prompts/acceptance-evaluator.md",
    "native/trio-native.js",  # ACC_EVALUATOR_FRAGMENT (JSON string constant)
)
LEAD_FRAGMENTS = (
    "prompts/canonical/acceptance-lead.md",
    "prompts/canonical/acceptance-native-lead.md",
    "omnigent/entrypoints/trio-omnigent/prompts/acceptance-lead.md",
    "native/trio-native.js",  # ACC_LEAD_FRAGMENT
)


@pytest.mark.parametrize("rel", EVALUATOR_FRAGMENTS)
def test_evaluator_fragments_aim_attacks_at_requirement_gaps(rel):
    text = _read(rel)
    for phrase in ("floor, not the finish line", "`attacks:` go beyond it",
                   "`requirement_gaps:` lines in `acceptance/FROZEN`",
                   "`requirements` in MANIFEST.json", "AUTHOR.md's dropped and live-only sentences",
                   "A SHIP with no counted attack is refused by the driver"):
        assert phrase in text, (rel, phrase)
    assert text.index("`requirement_gaps:`") < text.index("AUTHOR.md's dropped")


@pytest.mark.parametrize("rel", LEAD_FRAGMENTS)
def test_lead_fragments_tell_the_lead_to_plan_for_edge_cases(rel):
    text = _read(rel)
    for phrase in ("`requirements` map", "which requirements have hard checks",
                   "attacks beyond the pack", "plan for the edge cases, not just the frozen checks"):
        assert phrase in text, (rel, phrase)


def test_lead_criteria_say_the_frozen_pack_is_not_the_finish_line():
    for rel in (".claude/agents/trio-lead.md", "opencode-driver/agents/trio-lead.md",
                "omnigent/trio-omnigent-roles/lead/config.yaml",
                "omnigent/entrypoints/trio-omnigent/prompts/lead.md"):
        text = _read(rel)
        assert "Passing the frozen checks or your accepts is not the finish line" in text, rel
        assert "counted attacks beyond them" in text, rel

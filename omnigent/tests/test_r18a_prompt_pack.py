"""r18a prompt pack (L1 L3 L4 L5 L8 L9): acceptance oracles, evidence kinds,
independent probe, goal acceptance, mode enforcement, receipts re-executed,
attacks listed. Rendered through the real paths: ``OmnigentRunner._prompt``
(root-bound and root-free), the isolated builder note, the canonical role
bodies, every file ``prompts/generate.py`` renders, and the Omnigent role
configs."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "prompts" / "canonical"
ROLES = ROOT / "omnigent" / "trio-omnigent-roles"
ENTRY = ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace('\\"', '"').replace("\\n", " "))


TRIOCTL = _load("trioctl_r18a_pack", ROOT / "omnigent" / "trioctl")
RF = {"home": "/work/repo", "lead": "/work/lead", "branch": "trio/m", "target": "main",
      "target_base": "d" * 40, "mailbox_rel": "loop", "slug": "loop", "repo_targets": {}}

LEAD_PASS = {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None}
SLICE = {"mode": "open-loop", "kind": "slice-eval", "slice": "s1", "sha": "a" * 40}
INTEGRATION = {"mode": "open-loop", "kind": "integration-eval", "slice": None, "sha": None,
               "pinned_sha": "b" * 40, "evaluator_attempt": "a1", "iteration": 3}

GRAMMAR = "`<input/action> -> <observable> | oracle: <kind>`"
LEAD_NEEDLES = (GRAMMAR, "`## Accepts`", "`ACCEPT_TEST:` lines", "AUTHORED-BY: lead")
SLICE_NEEDLES = (
    "evidence kind `re-run`|`probe`|`implementer-test`|`receipt`",
    "`attacks:` (at least two you tried",
    "`evidence: re-run=<n> probe=<n> implementer-test=<n> receipt=<n> unverified=<n>`",
    "a receipt alone is never PASS",
    "the declared `mode:` is enforced",
)
INTEGRATION_NEEDLES = (
    "`## Independent probe` section (`probe: PASS|FAIL|UNAVAILABLE`",
    "PLAN.md's `goal_probe:`",
    "UNAVAILABLE is NEEDS_HUMAN, never SHIP",
)
RIGOR_NEEDLES = (
    "## Evidence kinds", "## Independent probe",
    "A receipt alone is never PASS",
    "`in` checks of a one- or two-character literal (`assert \"4\" in t`)",
    "a typecheck over an empty project (`tsc` whose tsconfig has `files: []`)",
    "asserting the exact literal the implementation writes",
    "`--verify-only` or pass-flag readers",
    "string-presence checks on files the slice or the Lead wrote",
    "`test-first` needs red-before-green evidence",
    "`implement-then-smoke` needs the smoke re-executed by you at the pin",
    "`AUTHORED-BY: lead`",
    "at least two concrete attacks",
)


def _prompt(tmp_path: Path, role: str, ctx: dict, *, root_free: bool) -> str:
    mailbox = tmp_path / "mb"
    mailbox.mkdir(exist_ok=True)
    kw = {"root_free": RF} if root_free else {}
    runner = TRIOCTL.OmnigentRunner(
        repo=tmp_path, isolate_workers={"trioctl": ROOT / "omnigent" / "trioctl",
                                        "worktree_root": str(tmp_path / "wt")}, **kw)
    return _flat(runner._prompt(role, 3, mailbox, dict(ctx)))


@pytest.mark.parametrize("root_free", [False, True], ids=["root-bound", "root-free"])
def test_open_loop_renders_carry_the_pack(tmp_path, root_free):
    lead = _prompt(tmp_path, "lead", LEAD_PASS, root_free=root_free)
    for needle in LEAD_NEEDLES:
        assert needle in lead, needle
    sl = _prompt(tmp_path, "evaluator", SLICE, root_free=root_free)
    for needle in SLICE_NEEDLES + RIGOR_NEEDLES:
        assert needle in sl, needle
    integ = _prompt(tmp_path, "evaluator", INTEGRATION, root_free=root_free)
    for needle in INTEGRATION_NEEDLES + RIGOR_NEEDLES:
        assert needle in integ, needle


def test_lockstep_evaluator_render_carries_rigor(tmp_path):
    ev = _prompt(tmp_path, "evaluator", {"evaluator_attempt": "a", "pinned_sha": "c" * 40},
                 root_free=False)
    for needle in RIGOR_NEEDLES + ("the `## Independent probe` section",):
        assert needle in ev, needle


def test_isolated_builder_note_asks_for_accept_mapping():
    note = _flat(TRIOCTL._ISOLATED_BUILDER_NOTE.format(
        path="/w", branch="b", base="c", repo="/r", slice="s", mailbox="/m"))
    assert "ACCEPT_TEST: <accept, abbreviated> -> <test file>::<test name>" in note
    assert "never assert on the text of files you wrote" in note


def test_canonical_bodies_carry_the_pack():
    lead = _flat((CANONICAL / "lead.md").read_text())
    for needle in (GRAMMAR, "`goal_acceptance:`", "`goal_probe:`", "`offline: yes|no`",
                   "You declare the probe; you never implement it",
                   "`test-first` means every code slice's new tests must fail on the base",
                   "Switching `mode:` mid-loop needs a `DECISION:` line",
                   "`## Accepts`", "AUTHORED-BY: lead"):
        assert needle in lead, needle
    ev = _flat((CANONICAL / "evaluator.md").read_text())
    for needle in RIGOR_NEEDLES:
        assert needle in ev, needle
    builder = _flat((CANONICAL / "builder.md").read_text())
    assert "`ACCEPT_TEST: <accept, abbreviated> -> <test file>::<test name>`" in builder


def test_every_generated_flavor_carries_the_pack():
    gen = _load("trio_generate_r18a_pack", ROOT / "prompts" / "generate.py")
    seen = {"lead": 0, "evaluator": 0, "builder": 0}
    for path, text in gen.all_outputs().items():
        flat = _flat(text)
        if "Role: Lead" in text:
            seen["lead"] += 1
            assert "`goal_probe:`" in flat and GRAMMAR in flat, path
        elif "Role: Evaluator" in text:
            seen["evaluator"] += 1
            for needle in RIGOR_NEEDLES:
                assert needle in flat, (path, needle)
        elif "Role: Builder" in text:
            seen["builder"] += 1
            assert "ACCEPT_TEST:" in flat, path
    assert all(seen.values()), seen


def test_omnigent_role_configs_carry_the_pack():
    lead = _flat((ROLES / "lead" / "config.yaml").read_text())
    for needle in ("`goal_acceptance:`", "`goal_probe:`", "AUTHORED-BY: lead", "`## Accepts`"):
        assert needle in lead, needle
    assert "ACCEPT_TEST:" in _flat((ROLES / "builder" / "config.yaml").read_text())
    ev = _flat((ROLES / "evaluator" / "config.yaml").read_text())
    for needle in RIGOR_NEEDLES:
        assert needle in ev, needle
    entry_lead = _flat((ENTRY / "lead.md").read_text())
    assert "`goal_probe:`" in entry_lead and "oracle: <kind>" in entry_lead

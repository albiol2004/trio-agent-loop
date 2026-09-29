"""The generated repair prompts satisfy the drivers' repair LOG gate (F5)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPAIR_TARGETS = [
    "prompts/canonical/repair.md",
    ".claude/agents/trio-repair.md",
    "portable/prompts/repair.md",
    ".codex/agents/trio-repair.toml",
    "codex/agents/trio-repair.toml",
    "codex/skills/trio/references/prompts/repair.md",
    "kimi/skills/trio/references/prompts/repair.md",
    "omp/agents/trio-repair.md",
    "opencode/agents/trio-repair.md",
]
LOG_FORM = re.compile(r"`(- iter N \| [a-z]+ \| [^`]*)`")


def _trio_loop():
    spec = importlib.util.spec_from_file_location(
        "trio_loop_contract", ROOT / "metrics" / "trio_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _flat(rel: str) -> str:
    return " ".join((ROOT / rel).read_text(encoding="utf-8").split())


@pytest.mark.parametrize("rel", REPAIR_TARGETS)
def test_repair_log_lines_pass_repair_gate(rel: str, tmp_path: Path) -> None:
    tl = _trio_loop()
    forms = LOG_FORM.findall(_flat(rel))
    assert len(forms) >= 2, forms  # the normal line and the scope mismatch
    for form in forms:
        box = tmp_path / f"box{forms.index(form)}"
        box.mkdir()
        line = form.replace("iter N", "iter 3").replace("<", "").replace(">", "")
        (box / "LOG.md").write_text(f"# Trio loop log\n{line}\n")
        ok, note = tl._log_gate(box, 3, "repair")
        assert ok, (rel, form, note)
    assert any("scope mismatch" in form for form in forms)


@pytest.mark.parametrize("rel", REPAIR_TARGETS)
def test_repair_commits_its_fix_and_never_loop(rel: str) -> None:
    flat = _flat(rel)
    assert "never commit." not in flat
    assert "| lead | repair" not in flat
    assert "as `slice(<id>): fix <summary>`" in flat
    assert "Never commit `loop/` files" in flat


def test_evaluator_and_orchestrator_accept_repair_entry() -> None:
    for rel in ("prompts/canonical/evaluator.md",
                ".claude/agents/trio-evaluator.md",
                "prompts/canonical/orchestrator.md"):
        assert "`- iter N | repair | ...` entry" in _flat(rel), rel
    assert "`ITERATE scope=design` (never `scope=local:`)" in _flat(
        ".claude/agents/trio-evaluator.md")


def test_no_lead_repair_form_left_in_skills() -> None:
    for rel in ("zcode/skills/trio/SKILL.md",
                "omnigent/entrypoints/trio-omnigent/SKILL.md"):
        assert "| lead | repair" not in _flat(rel), rel

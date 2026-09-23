"""Prove shared planning/eval obligations reach effective prompts."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "omnigent" / "trioctl"

# Phrases that must appear in generated native roles and Omnigent dispatch.
OBLIGATIONS = (
    "Original-goal planning",
    "Independent evaluation",
    "Commit ownership (driver)",
    "Task-specific checklist",
    "must-preserve",
    "unverified",
    "knowledge.yaml",
    "evaluated:",
    "unavailable environment",
)


def load_trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_generate_check_is_clean() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "prompts" / "generate.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_generated_native_roles_carry_obligations() -> None:
    lead = (ROOT / ".claude" / "agents" / "trio-lead.md").read_text(
        encoding="utf-8"
    )
    evaluator = (
        ROOT / ".claude" / "agents" / "trio-evaluator.md"
    ).read_text(encoding="utf-8")
    for phrase in (
        "must-preserve",
        "knowledge.yaml",
        "unverified",
        "require-commits",
    ):
        assert phrase in lead or phrase in evaluator, phrase
    assert "must-preserve" in lead
    assert "unverified" in evaluator
    assert "task-specific checklist" in lead
    assert "unavailable environment" in evaluator
    assert "whole-goal SHIP" in evaluator


def test_omnigent_dispatch_prompt_carries_shared_essentials(
    tmp_path: Path,
) -> None:
    trioctl = load_trioctl()
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    for name in (
        "GOAL.md",
        "STATE.md",
        "PLAN.md",
        "REPORT.md",
        "VERDICT.md",
        "LOG.md",
    ):
        (mailbox / name).write_text("", encoding="utf-8")
    runner = trioctl.OmnigentRunner(repo=ROOT)
    lead = runner._prompt("lead", 1, mailbox)
    evaluator = runner._prompt("evaluator", 1, mailbox)
    for phrase in OBLIGATIONS:
        assert phrase in lead, phrase
        assert phrase in evaluator, phrase


def test_examples_keep_goal_rows_and_incomplete_omits() -> None:
    """Structural: complete PLAN lists GOAL refs; incomplete omits one."""
    base = ROOT / "examples" / "task-verification"
    ui_goal = (base / "ui" / "GOAL.md").read_text(encoding="utf-8")
    ui_plan = (base / "ui" / "PLAN.md").read_text(encoding="utf-8")
    ui_inc = (base / "ui" / "incomplete-PLAN.md").read_text(encoding="utf-8")
    assert "GOAL empty-error" in ui_plan
    assert "GOAL empty-error" not in ui_inc
    assert "Title is required" in ui_goal
    assert "VERDICT: ITERATE" in (
        base / "ui" / "incomplete-VERDICT.md"
    ).read_text(encoding="utf-8")
    data_plan = (base / "data" / "PLAN.md").read_text(encoding="utf-8")
    data_inc = (base / "data" / "incomplete-PLAN.md").read_text(
        encoding="utf-8"
    )
    assert "GOAL reconcile" in data_plan
    assert "GOAL rerun" in data_plan
    assert "GOAL reconcile" not in data_inc
    assert "independent" in data_plan.lower()
    agents = (ROOT / "portable" / "AGENTS.template.md").read_text(
        encoding="utf-8"
    )
    assert "## Verification defaults" in agents

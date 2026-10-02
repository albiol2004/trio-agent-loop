"""The two new canonical evaluator rule sections ("Goal-derived pass/fail",
"Closing unverified claims") and the new canonical lead section
("Goal-derived criteria") reach every generated role-prompt surface:
native (.claude), the standalone OpenCode driver, the in-OpenCode plugin,
and both Omnigent sites (the registered role config and the per-dispatch
prompt, inside their respective marked blocks).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EVALUATOR_HEADINGS = ("Goal-derived pass/fail", "Closing unverified claims")
EVALUATOR_SECTIONS = tuple(f"## {h}" for h in EVALUATOR_HEADINGS)
LEAD_HEADING = "Goal-derived criteria"
LEAD_SECTION = f"## {LEAD_HEADING}"

# The standalone OpenCode driver's rendered bodies exist only on branches
# that carry its overlay (prompts/overlays/opencode-driver.md).
_DRIVER = (ROOT / "prompts" / "overlays" / "opencode-driver.md").is_file()
EVALUATOR_SITES = (
    ROOT / ".claude" / "agents" / "trio-evaluator.md",
    ROOT / "opencode" / "agents" / "trio-evaluator.md",
) + ((ROOT / "opencode-driver" / "agents" / "trio-evaluator.md",) if _DRIVER else ())
LEAD_SITES = (
    ROOT / ".claude" / "agents" / "trio-lead.md",
    ROOT / "opencode" / "agents" / "trio-lead.md",
) + ((ROOT / "opencode-driver" / "agents" / "trio-lead.md",) if _DRIVER else ())

OMNIGENT_EVALUATOR_SITES = (
    ROOT / "omnigent" / "trio-omnigent-roles" / "evaluator" / "config.yaml",
    ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts" / "evaluator.md",
)
OMNIGENT_LEAD_SITES = (
    ROOT / "omnigent" / "trio-omnigent-roles" / "lead" / "config.yaml",
    ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts" / "lead.md",
)


def _block(text: str, marker: str) -> str:
    start = text.index(f"<!-- {marker}:start -->")
    end = text.index(f"<!-- {marker}:end -->")
    return text[start:end]


def test_evaluator_sections_present_in_native_and_driver_agents():
    for path in EVALUATOR_SITES:
        text = path.read_text(encoding="utf-8")
        for heading in EVALUATOR_SECTIONS:
            assert heading in text, (path, heading)


def test_lead_section_present_in_native_and_driver_agents():
    for path in LEAD_SITES:
        text = path.read_text(encoding="utf-8")
        assert LEAD_SECTION in text, path


def test_evaluator_sections_present_inside_omnigent_rigor_block():
    # The Omnigent rigor extraction demotes `## <heading>` to `### <heading>`
    # (generate.py's _rigor_text); check the heading text, not the level.
    for path in OMNIGENT_EVALUATOR_SITES:
        text = path.read_text(encoding="utf-8")
        block = _block(text, "trio-evaluator-rigor")
        for heading in EVALUATOR_HEADINGS:
            assert heading in block, (path, heading)


def test_lead_section_present_inside_omnigent_lead_criteria_block():
    for path in OMNIGENT_LEAD_SITES:
        text = path.read_text(encoding="utf-8")
        block = _block(text, "trio-lead-criteria")
        assert LEAD_HEADING in block, path

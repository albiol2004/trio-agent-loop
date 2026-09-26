"""r12: open-loop Lead retires each slice as soon as it is integrated.

Asserts on the canonical Lead body, on every Lead file rendered from it by
the real generator (prompts/generate.py), and on the Omnigent open-loop
Lead prompt rendered through ``OmnigentRunner._prompt``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL_LEAD = ROOT / "prompts" / "canonical" / "lead.md"

EARLY_RETIRE = "Do not wait for the wave to land or for the hazard check to retire a slice"
KEEP_DISPATCHING = "Keep dispatching the remaining slices and faults while earlier retired slices are being graded"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _flat(text: str) -> str:
    """Collapse wrapping (and TOML/YAML quoting noise) to single spaces."""
    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _rendered_leads() -> dict[Path, str]:
    generate = _load("trio_generate_r12", ROOT / "prompts" / "generate.py")
    outputs = generate.all_outputs()
    leads = {
        path: text
        for path, text in outputs.items()
        if "lead" in path.name and "## Open-loop mode" in text
    }
    assert leads, "generator rendered no Lead file carrying the open-loop section"
    return leads


def _omnigent_open_loop_lead(tmp_path: Path) -> str:
    trioctl = _load("trioctl_r12", ROOT / "omnigent" / "trioctl")
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    for name in ("GOAL.md", "STATE.md", "PLAN.md", "REPORT.md", "VERDICT.md", "LOG.md"):
        (mailbox / name).write_text("", encoding="utf-8")
    runner = trioctl.OmnigentRunner(repo=ROOT)
    return runner._prompt(
        "lead", 2, mailbox,
        {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None},
    )


def _open_loop_block(prompt: str) -> str:
    """The OPEN-LOOP CONTEXT block: everything before the base Lead prompt."""
    assert prompt.startswith("OPEN-LOOP CONTEXT: kind=lead-pass\n")
    return prompt.split("\n\n", 1)[0]


# ---------------------------------------------------------------- commit A


def test_canonical_open_loop_section_retires_per_slice() -> None:
    section = _flat(_section(CANONICAL_LEAD.read_text(encoding="utf-8"), "## Open-loop mode"))
    assert "prints `integrated`" in section
    assert "IMMEDIATELY set that slice's `status: complete`" in section
    assert "`merge_commit` sha from the builder's JSON output line" in section
    assert "before waiting for any other builder in the wave" in section
    assert EARLY_RETIRE in section
    # Hazard check survives, but runs after every retirement.
    assert "once every member of the wave is retired" in section
    assert "trio-shadow.py --mailbox <dir>" in section
    assert "append a NEW `retired:` entry for that slice at the fix sha" in section
    assert KEEP_DISPATCHING in section
    # Old per-finish phrasing is gone.
    assert "On finishing a slice: commit, set" not in section


def test_canonical_phase2_defers_hazard_check_in_open_loop() -> None:
    phase2 = _flat(_section(CANONICAL_LEAD.read_text(encoding="utf-8"), "## Phase 2"))
    assert "this is the one hazard check" in phase2
    assert "never hold a retirement for it" in phase2


def test_generated_lead_files_carry_early_retire() -> None:
    for path, text in _rendered_leads().items():
        flat = _flat(text).replace('\\"', '"').replace("\\n", " ")
        flat = _flat(flat)
        assert EARLY_RETIRE in flat, path
        assert KEEP_DISPATCHING in flat, path


def test_omnigent_open_loop_lead_prompt_carries_early_retire(tmp_path: Path) -> None:
    block = _flat(_open_loop_block(_omnigent_open_loop_lead(tmp_path)))
    assert "prints `integrated`" in block
    assert "IMMEDIATELY set that slice's `status: complete`" in block
    assert "full `merge_commit` sha from the builder's JSON output line" in block
    assert EARLY_RETIRE in block
    assert "once every wave member is retired" in block
    assert "NEW `retired:` entry for that slice at the fix sha" in block
    assert KEEP_DISPATCHING in block
    assert "commits without appending a `retired:` entry is incomplete" in block

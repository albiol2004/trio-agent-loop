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


# ---------------------------------------------------------------- commit B

SUITE_MANDATE = "Run the project's build/tests/linters before reporting"
NO_SELF_CHECK = "do NOT run the project suite"
LEDGER_EVAL = (
    "REPORT.md is a dispatch ledger, not a verification claim; your own "
    "full-suite run is the sole authoritative verification"
)


def _canonical() -> str:
    return CANONICAL_LEAD.read_text(encoding="utf-8")


def _output_templates(text: str) -> tuple[str, str]:
    # The templates contain their own `## ` lines inside code fences, so
    # take everything from the Output heading up to the Rules heading.
    output = text[text.index("## Output"): text.index("## Rules")]
    lockstep, open_loop = output.split("Open-loop (`loop/QUEUE.md` exists)", 1)
    return lockstep, open_loop


def test_canonical_open_loop_section_drops_lead_self_check() -> None:
    section = _flat(_section(_canonical(), "## Open-loop mode"))
    assert SUITE_MANDATE.lower() not in section.lower()
    assert NO_SELF_CHECK in section
    assert "do NOT re-verify the integrated tree" in section
    assert "sole authoritative verification" in section
    assert 'git log --grep="^slice(<id>):"' in section
    assert "Never weaken verification" in section
    assert "`merge_commit` equal to `base`" in section


def test_canonical_lockstep_keeps_suite_mandate_and_verified_section() -> None:
    text = _canonical()
    quality = _flat(_section(text, "## Quality bar"))
    assert "Lockstep: " + SUITE_MANDATE in quality
    assert "Never weaken verification to pass it" in quality
    assert "Commit presence is a completion criterion" in text
    lockstep, open_loop = _output_templates(text)
    assert "## How I verified it" in lockstep
    assert "## How I verified it" not in open_loop
    flat_open = _flat(open_loop)
    for field in ("builder id", "merge sha", "files",
                  "builder-reported targeted test result line",
                  "one-line status", "## Deviations from plan",
                  "## Known weaknesses"):
        assert field in flat_open, field


def test_generated_leads_and_evaluators_carry_commit_b() -> None:
    generate = _load("trio_generate_r12b", ROOT / "prompts" / "generate.py")
    outputs = generate.all_outputs()
    leads = evaluators = 0
    for path, text in outputs.items():
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        if "lead" in path.name and "## Open-loop mode" in text:
            leads += 1
            assert NO_SELF_CHECK in flat, path
            assert "Lockstep: " + SUITE_MANDATE in flat, path
            assert "open-loop dispatch ledger" in flat, path
        if "evaluator" in path.name and "## Open-loop mode" in text:
            evaluators += 1
            assert LEDGER_EVAL in flat, path
    assert leads and evaluators


def test_omnigent_open_loop_lead_prompt_drops_self_check(tmp_path: Path) -> None:
    prompt = _omnigent_open_loop_lead(tmp_path)
    block = _flat(_open_loop_block(prompt))
    assert NO_SELF_CHECK in block
    assert "replaces the base prompt's step 4 in open-loop" in block
    assert "sole authoritative verification" in block
    assert "dispatch/merge ledger" in block
    assert "not reported" in block
    assert "REPORT.md" in block
    assert SUITE_MANDATE.lower() not in block.lower()
    # The base (lockstep) body is still appended unchanged after the block.
    assert "Run the checks promised by the plan" in prompt


def test_omnigent_lockstep_lead_prompt_unchanged(tmp_path: Path) -> None:
    trioctl = _load("trioctl_r12_lock", ROOT / "omnigent" / "trioctl")
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    runner = trioctl.OmnigentRunner(repo=ROOT)
    prompt = runner._prompt("lead", 1, mailbox)
    assert not prompt.startswith("OPEN-LOOP CONTEXT")
    assert "Run the checks promised by the plan" in prompt
    assert "exact commands and outputs" in prompt
    assert NO_SELF_CHECK not in prompt


def test_omnigent_integration_eval_prompt_names_ledger(tmp_path: Path) -> None:
    trioctl = _load("trioctl_r12_eval", ROOT / "omnigent" / "trioctl")
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    runner = trioctl.OmnigentRunner(repo=ROOT)
    prompt = runner._prompt(
        "evaluator", 3, mailbox,
        {"mode": "open-loop", "kind": "integration-eval", "slice": None,
         "sha": None, "pinned_sha": "abc1234", "evaluator_attempt": "a1",
         "iteration": 3},
    )
    block = _flat(prompt.split("\n\n", 1)[0])
    assert block.startswith("OPEN-LOOP CONTEXT: kind=integration-eval")
    assert LEDGER_EVAL in block

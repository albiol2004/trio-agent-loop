"""r18a L0: the Omnigent evaluator receives the canonical evaluator rigor.

The rigor block is generated from prompts/canonical/evaluator.md (never a
hand copy) into the Omnigent evaluator's registered role config and its
per-dispatch prompt; the open-loop integration procedure no longer points
at "Method and Output sections above" that the Omnigent prompt never had.
Rendered through the real paths (``prompts/generate.py`` and
``OmnigentRunner._prompt``).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "omnigent" / "trio-omnigent-roles" / "evaluator" / "config.yaml"
ENTRY = ROOT / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts" / "evaluator.md"

RIGOR = (
    "## Verification rigor",
    "unit tests are NOT sufficient ground truth",
    "re-run the pipeline yourself from scratch",
    "lists what you actively tried to break",
    "Prefer executing code over reading it",
    "If you did not run a criterion's check yourself, it is not PASS",
    "Then go beyond them: edge cases, error paths",
)
DANGLING = (
    "Method and Output sections above",
    "sections above apply",
    "Retirement commit convention",
)


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _eval_prompt(tmp_path: Path, context: dict) -> str:
    trioctl = _load(f"trioctl_r18a_l0_{context['kind'].replace('-', '_')}",
                    ROOT / "omnigent" / "trioctl")
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    runner = trioctl.OmnigentRunner(repo=ROOT)
    return runner._prompt("evaluator", 3, mailbox, context)


SLICE = {"mode": "open-loop", "kind": "slice-eval", "slice": "s1", "sha": "a" * 40}
INTEGRATION = {"mode": "open-loop", "kind": "integration-eval", "slice": None,
               "sha": None, "pinned_sha": "b" * 40, "evaluator_attempt": "a1",
               "iteration": 3}


@pytest.mark.parametrize("path", [CONFIG, ENTRY], ids=["config", "entrypoint"])
def test_omnigent_evaluator_sources_carry_rigor(path: Path) -> None:
    flat = _flat(path.read_text(encoding="utf-8"))
    for needle in RIGOR:
        assert needle in flat, (path.name, needle)
    assert "<!-- trio-evaluator-rigor:start -->" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("context", [SLICE, INTEGRATION], ids=["slice", "integration"])
def test_rendered_omnigent_evaluator_prompt_has_rigor_and_no_dangling_refs(
    tmp_path: Path, context: dict
) -> None:
    prompt = _eval_prompt(tmp_path, dict(context))
    flat = _flat(prompt)
    for needle in RIGOR:
        assert needle in flat, needle
    for needle in DANGLING:
        assert needle not in flat, needle
    # Every "your role prompt's `## X`" reference resolves inside the prompt.
    for heading in re.findall(r"role prompt's `## ([^`]+)`", flat):
        assert re.search(rf"^\s*## {re.escape(heading)}\s*$", prompt, re.M), heading


def test_integration_procedure_spells_out_its_method(tmp_path: Path) -> None:
    block = _flat(_eval_prompt(tmp_path, dict(INTEGRATION)).split("\n\n", 1)[0])
    for needle in (
        "check GOAL.md completeness against PLAN.md",
        "run the full check and every acceptance check yourself at the pin",
        "REPORT.md and receipts are claims, never evidence",
        "PASS, FAIL or unverified",
        "list what you actively tried to break",
        "classify unavailable environment vs product failure",
        "follow your role prompt's SHIP retirement steps",
    ):
        assert needle in block, needle


def test_rigor_block_is_generated_from_canonical_not_copied() -> None:
    gen = _load("trio_generate_r18a_l0", ROOT / "prompts" / "generate.py")
    content = gen.rigor_content()
    canonical = (ROOT / "prompts" / "canonical" / "evaluator.md").read_text(encoding="utf-8")
    # Every non-heading line of the block (after its intro) is canonical text.
    body = content.split("\n\n", 1)[1]
    for line in body.splitlines():
        if not line.strip() or line.startswith("### "):
            continue
        assert line in canonical, line
    outputs = gen.all_outputs()
    for path in (CONFIG, ENTRY):
        assert outputs[path] == path.read_text(encoding="utf-8"), path


def test_missing_canonical_anchor_fails_generation(tmp_path: Path, monkeypatch) -> None:
    gen = _load("trio_generate_r18a_l0_anchor", ROOT / "prompts" / "generate.py")
    canon = tmp_path / "canonical"
    shutil.copytree(ROOT / "prompts" / "canonical", canon)
    ev = canon / "evaluator.md"
    ev.write_text(ev.read_text(encoding="utf-8").replace(
        "- Prefer executing code over reading it.", "- Prefer reading code."), encoding="utf-8")
    monkeypatch.setattr(gen, "CANONICAL_DIR", canon)
    with pytest.raises(ValueError, match="Prefer executing code"):
        gen.rigor_content()


def test_protocol_essentials_carry_rigor_bullet_everywhere() -> None:
    gen = _load("trio_generate_r18a_l0_ess", ROOT / "prompts" / "generate.py")
    for relpath, _style in gen.EMBEDDED:
        path = ROOT / relpath
        if path.is_file():
            flat = _flat(path.read_text(encoding="utf-8"))
            assert "Verification rigor — run every check yourself" in flat, relpath

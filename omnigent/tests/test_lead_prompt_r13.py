"""r13: open-loop Lead gets one bounded whole-tree gate (drops r12 B).

Rendered through the real paths: the Omnigent open-loop Lead prompt via
``OmnigentRunner._prompt``, the isolated builder note trioctl injects, the
canonical role bodies and every file ``prompts/generate.py`` renders from
them, and the hand-maintained Omnigent role configs.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "prompts" / "canonical"
ROLES = ROOT / "omnigent" / "trio-omnigent-roles"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _flat(text: str) -> str:
    text = text.replace('\\"', '"').replace("\\n", " ")
    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _trioctl(tag: str):
    return _load(f"trioctl_r13_{tag}", ROOT / "omnigent" / "trioctl")


def _lead_prompt(tmp_path: Path, *, isolate: bool = True) -> str:
    trioctl = _trioctl("lead")
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    for name in ("GOAL.md", "STATE.md", "PLAN.md", "REPORT.md", "VERDICT.md", "LOG.md"):
        (mailbox / name).write_text("", encoding="utf-8")
    kwargs = {}
    if isolate:
        kwargs["isolate_workers"] = {
            "trioctl": ROOT / "omnigent" / "trioctl",
            "worktree_root": str(tmp_path / "wt"),
        }
    runner = trioctl.OmnigentRunner(repo=ROOT, **kwargs)
    return runner._prompt(
        "lead", 2, mailbox,
        {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None},
    )


def _block(prompt: str) -> str:
    assert prompt.startswith("OPEN-LOOP CONTEXT: kind=lead-pass\n")
    return prompt.split("\n\n", 1)[0]


def _integration_eval_prompt(tmp_path: Path) -> str:
    trioctl = _trioctl("eval")
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    runner = trioctl.OmnigentRunner(repo=ROOT)
    return runner._prompt(
        "evaluator", 3, mailbox,
        {"mode": "open-loop", "kind": "integration-eval", "slice": None,
         "sha": None, "pinned_sha": "abc1234", "evaluator_attempt": "a1",
         "iteration": 3},
    )


def _generated() -> dict[Path, str]:
    return _load("trio_generate_r13", ROOT / "prompts" / "generate.py").all_outputs()


def _generated_role(role: str) -> list[tuple[Path, str]]:
    out = [(p, _flat(t)) for p, t in _generated().items()
           if role in p.name and "## Open-loop mode" in t]
    assert out, role
    return out


# ------------------------------------------------------------ G1 gate

B_NO_SUITE = "do NOT run the project suite"
B_NO_REVERIFY = "do NOT re-verify the integrated tree"
B_SOLE = "the Evaluator's own full-suite run is the sole authoritative verification"
B_EVAL = ("REPORT.md is a dispatch ledger, not a verification claim; your own "
          "full-suite run is the sole authoritative verification")
GATE_HEAD = "Whole-tree gate — your ONE verification"
GATE_WHEN = ("After the LAST builder of the wave has integrated and every slice "
             "is retired, run the repository's whole-tree verification ONCE on HEAD")
GATE_CMD = ("exactly the `full_check:` command(s) PLAN.md's `## Verification "
            "standard` names as the full check")
GATE_BUDGET = ("with a wall-clock budget of 120 s by default — PLAN.md may "
               "override it with a `full_check_budget_s:` line there")
GATE_FIX = ("On failure, fix ONLY within the failing paths: a `slice(<id>): fix")
GATE_RETIRE = "NEW `retired:` entry at the fix sha (step 3's post-retirement rule), then re-run the gate once"
GATE_GIVEUP = ("If it still fails, or it exceeds the budget, write the failure "
               "(command, failing tests/errors) into REPORT.md `## Known weaknesses` "
               "and end the pass — the Evaluator decides")
GATE_PARALLEL = "Slice-evals may still be grading the last retirements meanwhile; do not wait for them"
EVAL_LEDGER = ("REPORT.md is the Lead's dispatch/merge ledger plus one "
               "`## Whole-tree gate` result — a claim to check, not evidence; your "
               "own full-suite run is the authoritative verification")


def _assert_gate(text: str) -> None:
    for needle in (GATE_HEAD, GATE_WHEN, GATE_CMD, GATE_BUDGET, GATE_FIX,
                   GATE_RETIRE, GATE_GIVEUP, GATE_PARALLEL):
        assert needle in text, needle
    for needle in (B_NO_SUITE, B_NO_REVERIFY, B_SOLE):
        assert needle not in text, needle


def test_omnigent_open_loop_lead_has_gate_not_b(tmp_path: Path) -> None:
    for isolate in (True, False):
        sub = tmp_path / str(isolate)
        sub.mkdir()
        block = _flat(_block(_lead_prompt(sub, isolate=isolate)))
        _assert_gate(block)
        assert "`timeout <budget> sh -c '<full_check>'`" in block
        assert "If PLAN.md names no `full_check:`, add that line first" in block
        assert "No other Lead verification: no per-slice re-review, no open-ended self-review" in block


def test_omnigent_open_loop_report_template_ledger_plus_gate(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    assert "REPORT.md = the dispatch/merge ledger plus the gate result" in block
    assert ("one row: slice id | builder id | merge sha | files | the "
            "builder-reported targeted test result line") in block
    assert "Then `## Whole-tree gate`: the exact command(s)" in block
    assert "`PASS` or `FAIL` — the only verification claim you make" in block
    assert "not a verification claim" not in block
    assert block.index("Then `## Whole-tree gate`") < block.rindex("## Known weaknesses")


def test_canonical_and_generated_leads_carry_gate() -> None:
    canonical = (CANONICAL / "lead.md").read_text(encoding="utf-8")
    section = _flat(_section(canonical, "## Open-loop mode"))
    _assert_gate(section)
    output = canonical[canonical.index("## Output"): canonical.index("## Rules")]
    lockstep, open_loop = output.split("Open-loop (`loop/QUEUE.md` exists)", 1)
    assert "## Whole-tree gate" in open_loop and "## Whole-tree gate" not in lockstep
    assert "## Slices" in open_loop  # the ledger stays
    assert "## How I verified it" in lockstep
    assert "only the one whole-tree gate after the last retirement" in _flat(
        _section(canonical, "## Tiered test execution"))
    for path, flat in _generated_role("lead"):
        _assert_gate(flat)
        assert "## Whole-tree gate" in flat, path


def test_evaluator_ledger_sentence_names_gate(tmp_path: Path) -> None:
    block = _flat(_integration_eval_prompt(tmp_path).split("\n\n", 1)[0])
    assert EVAL_LEDGER in block
    assert B_EVAL not in block
    canonical = _flat((CANONICAL / "evaluator.md").read_text(encoding="utf-8"))
    assert EVAL_LEDGER in canonical and B_EVAL not in canonical
    for path, flat in _generated_role("evaluator"):
        assert EVAL_LEDGER in flat, path
        assert B_EVAL not in flat, path


def test_lead_config_and_base_prompt_name_gate(tmp_path: Path) -> None:
    config = _flat((ROLES / "lead" / "config.yaml").read_text(encoding="utf-8"))
    assert "run no suite" not in config
    assert ("after the last retirement run the PLAN's `full_check:` once on HEAD "
            "as the OPEN-LOOP CONTEXT's whole-tree gate (120 s budget unless PLAN "
            "sets `full_check_budget_s:`)") in config
    prompt = _flat(_lead_prompt(tmp_path))
    assert "no Lead suite run" not in prompt
    assert ("replaces this step with a dispatch/merge ledger and ONE whole-tree "
            "gate after the last retirement") in prompt


def test_plan_template_requires_full_check() -> None:
    canonical = _flat((CANONICAL / "lead.md").read_text(encoding="utf-8"))
    assert "The section MUST include a `full_check:` line" in canonical
    assert "`full_check_budget_s: <n>`" in canonical
    assert "never keys in the `slices:` block" in canonical
    config = _flat((ROLES / "lead" / "config.yaml").read_text(encoding="utf-8"))
    assert "a `full_check:` line naming the exact whole-tree command(s)" in config
    schema = _flat((ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8"))
    assert "**`full_check:`** (required)" in schema
    for path, flat in _generated_role("lead"):
        assert "The section MUST include a `full_check:` line" in flat, path

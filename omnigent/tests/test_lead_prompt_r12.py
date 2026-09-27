"""r12: open-loop Lead retires each slice as soon as it is integrated.

Asserts on the canonical Lead body, on every Lead file rendered from it by
the real generator (prompts/generate.py), and on the Omnigent open-loop
Lead prompt rendered through ``OmnigentRunner._prompt``.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re

import pytest
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
# r13 G1 dropped B's "no Lead self-check" in favour of one bounded
# whole-tree gate; these tests now pin B's absence and the gate's presence
# (the full r13 contract lives in test_lead_prompt_r13.py).

SUITE_MANDATE = "Run the project's build/tests/linters before reporting"
NO_SELF_CHECK = "do NOT run the project suite"
B_LEDGER_EVAL = (
    "REPORT.md is a dispatch ledger, not a verification claim; your own "
    "full-suite run is the sole authoritative verification"
)
LEDGER_EVAL = (
    "REPORT.md is the Lead's dispatch/merge ledger plus one "
    "`## Whole-tree gate` result — a claim to check, not evidence; your own "
    "full-suite run is the authoritative verification"
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
    # r13 G1: B's "no self-check" is gone; one whole-tree gate replaces it.
    section = _flat(_section(_canonical(), "## Open-loop mode"))
    assert SUITE_MANDATE.lower() not in section.lower()
    assert NO_SELF_CHECK not in section
    assert "do NOT re-verify the integrated tree" not in section
    assert "Whole-tree gate — your ONE verification" in section
    assert "run the repository's whole-tree verification ONCE on HEAD" in section
    assert "Never weaken verification" in section
    # B's step-6 restatement of the retire conditions is gone; step 3 keeps them.
    assert "`merge_commit` differs from its `base`" in section


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
            assert NO_SELF_CHECK not in flat, path
            assert "Whole-tree gate — your ONE verification" in flat, path
            assert "Lockstep: " + SUITE_MANDATE in flat, path
            assert "open-loop dispatch ledger" in flat, path
        if "evaluator" in path.name and "## Open-loop mode" in text:
            evaluators += 1
            assert LEDGER_EVAL in flat, path
            assert B_LEDGER_EVAL not in flat, path
    assert leads and evaluators


def test_omnigent_open_loop_lead_prompt_drops_self_check(tmp_path: Path) -> None:
    prompt = _omnigent_open_loop_lead(tmp_path)
    block = _flat(_open_loop_block(prompt))
    assert NO_SELF_CHECK not in block
    assert "sole authoritative verification" not in block
    assert "replaces the base prompt's step 4 in open-loop" in block
    assert "Whole-tree gate — your ONE verification" in block
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
    assert B_LEDGER_EVAL not in block


# ---------------------------------------------------------------- commit C

TASK_MANDATE = "MUST contain a `## Targeted check` section"
INVALID_TASK = "A task file without it is invalid — do not dispatch it"
# r13 G2: "LAST summary line" became "the line that states the pass/fail COUNTS".
BUILDER_CONTRACT = (
    "run exactly that command after implementing — never skip it — and put "
    "the output line that states the pass/fail COUNTS verbatim in your final "
    "message on a line prefixed `TARGETED_CHECK: `"
)
BUILDER_FAILED = "print `TARGETED_CHECK: FAILED <summary>` instead"


def _assert_retire_conditions(flat: str) -> None:
    assert "Retire only when both hold" in flat
    assert "`merge_commit` differs from its `base`" in flat
    assert "`targeted_check` field" in flat
    assert "does not start with `TARGETED_CHECK: FAILED`" in flat
    assert "do NOT retire: re-dispatch that slice once" in flat
    assert "do not append a fault" in flat
    assert "take the slice over yourself" in flat


def test_canonical_open_loop_requires_targeted_check_section() -> None:
    section = _flat(_section(_canonical(), "## Open-loop mode"))
    assert TASK_MANDATE in section
    assert INVALID_TASK in section
    assert "scoped to the slice's `writes:` and derived from its `accepts:`" in section
    _assert_retire_conditions(section)
    lockstep, open_loop = _output_templates(_canonical())
    assert "`TARGETED_CHECK:` line (JSON `targeted_check`)" in _flat(open_loop)
    assert "TARGETED_CHECK" not in lockstep


def test_omnigent_open_loop_lead_prompt_requires_targeted_check(tmp_path: Path) -> None:
    block = _flat(_open_loop_block(_omnigent_open_loop_lead(tmp_path)))
    assert TASK_MANDATE in block
    assert INVALID_TASK in block
    _assert_retire_conditions(block)
    assert "JSON `targeted_check` (`TARGETED_CHECK: ...`) verbatim" in block


def test_generated_leads_carry_targeted_check_mandate() -> None:
    for path, text in _rendered_leads().items():
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        assert TASK_MANDATE in flat, path
        assert "Retire only when both hold" in flat, path


def test_builder_prompt_sources_carry_targeted_check_contract() -> None:
    canonical = _flat((ROOT / "prompts" / "canonical" / "builder.md").read_text(encoding="utf-8"))
    config = _flat((ROOT / "omnigent" / "trio-omnigent-roles" / "builder" / "config.yaml")
                   .read_text(encoding="utf-8"))
    for source in (canonical, config):
        assert "`## Targeted check` section" in source
        assert BUILDER_CONTRACT in source
        assert BUILDER_FAILED in source
    generate = _load("trio_generate_r12c", ROOT / "prompts" / "generate.py")
    builders = [t for p, t in generate.all_outputs().items() if "builder" in p.name]
    assert builders
    for text in builders:
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        assert "TARGETED_CHECK: " in flat


def test_targeted_check_line_helper() -> None:
    trioctl = _load("trioctl_r12_tc", ROOT / "omnigent" / "trioctl")
    assert trioctl._targeted_check_line(None) is None
    assert trioctl._targeted_check_line("built A\nall good") is None
    out = "x\n  TARGETED_CHECK: 1 failed\nTARGETED_CHECK: 4 passed in 0.12s  \ndone"
    assert trioctl._targeted_check_line(out) == "TARGETED_CHECK: 4 passed in 0.12s"


# ------------------------------------------------ r12 repair R1 (contract)

# r13 G2: the literal task sentence names the counts line, per runner.
TASK_SENTENCE = (
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>` after "
    "running the check (pytest: `N passed[, M failed] in ...`; vitest: ` "
    "Tests N passed | M failed`, not `Duration`; go test: `ok`/`FAIL`; "
    "otherwise `TARGETED_CHECK: PASS <n>` or `TARGETED_CHECK: FAILED <summary>`)."
)


def test_omnigent_lead_task_template_carries_builder_sentence(tmp_path: Path) -> None:
    block = _flat(_open_loop_block(_omnigent_open_loop_lead(tmp_path)))
    assert "End that section with this literal sentence, which the builder sees verbatim" in block
    assert f'"{TASK_SENTENCE}"' in block


def test_canonical_and_generated_leads_carry_builder_sentence() -> None:
    assert TASK_SENTENCE in _flat(_section(_canonical(), "## Open-loop mode"))
    for path, text in _rendered_leads().items():
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        assert TASK_SENTENCE in flat, path


def test_isolated_builder_note_carries_contract() -> None:
    trioctl = _load("trioctl_r12_note", ROOT / "omnigent" / "trioctl")
    note = _flat(trioctl._ISOLATED_BUILDER_NOTE.format(
        path="/w", branch="b", base="c", repo="/r", slice="s", mailbox="/m"))
    assert "run the task file's `## Targeted check` command (never skip it)" in note
    assert "TARGETED_CHECK: <counts line>" in note  # r13 G2
    assert "plain text, no backticks, no bold, no bullet" in note
    assert "TARGETED_CHECK: FAILED <summary>" in note


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("TARGETED_CHECK: 4 passed in 0.12s", "TARGETED_CHECK: 4 passed in 0.12s"),
        ("`TARGETED_CHECK: 4 passed`", "TARGETED_CHECK: 4 passed"),
        ("**TARGETED_CHECK:** 4 passed", "TARGETED_CHECK: 4 passed"),
        ("**TARGETED_CHECK: 4 passed**", "TARGETED_CHECK: 4 passed"),
        ("- TARGETED_CHECK: 4 passed", "TARGETED_CHECK: 4 passed"),
        ("* `TARGETED_CHECK: 4 passed`", "TARGETED_CHECK: 4 passed"),
        ("1. TARGETED_CHECK: 4 passed", "TARGETED_CHECK: 4 passed"),
        ("TARGETED_CHECK:3 passed", "TARGETED_CHECK: 3 passed"),
        ("TARGETED_CHECK:\t3 passed", "TARGETED_CHECK: 3 passed"),
        ("TARGETED_CHECK:    3 passed", "TARGETED_CHECK: 3 passed"),
        ("targeted_check: 3 passed", "TARGETED_CHECK: 3 passed"),
        ("TARGETED_CHECK: `3 passed`", "TARGETED_CHECK: 3 passed"),
        ("TARGETED_CHECK: FAILED 2 failed", "TARGETED_CHECK: FAILED 2 failed"),
        ("TARGETED_CHECK: failed 2", "TARGETED_CHECK: FAILED 2"),
        ("TARGETED_CHECK: Failed: import error", "TARGETED_CHECK: FAILED: import error"),
        ("  TARGETED_CHECK: 5 passed  \r", "TARGETED_CHECK: 5 passed"),
        # r12 repair2 F1: failure text without the FAILED prefix is FAILED.
        ("TARGETED_CHECK: 2 failed, 3 passed", "TARGETED_CHECK: FAILED 2 failed, 3 passed"),
        ("TARGETED_CHECK: 2 failed, 3 passed in 0.2s", "TARGETED_CHECK: FAILED 2 failed, 3 passed in 0.2s"),
        ("TARGETED_CHECK: 1 error", "TARGETED_CHECK: FAILED 1 error"),
        ("TARGETED_CHECK: 1 error in 0.1s", "TARGETED_CHECK: FAILED 1 error in 0.1s"),
        ("TARGETED_CHECK: 3 passed, 2 errors in 0.3s", "TARGETED_CHECK: FAILED 3 passed, 2 errors in 0.3s"),
        ("TARGETED_CHECK: no tests ran", "TARGETED_CHECK: FAILED no tests ran"),
        ("TARGETED_CHECK: no tests ran in 0.01s", "TARGETED_CHECK: FAILED no tests ran in 0.01s"),
        ("TARGETED_CHECK: FAIL", "TARGETED_CHECK: FAILED FAIL"),
        ("TARGETED_CHECK: FAIL src/a.test.ts", "TARGETED_CHECK: FAILED FAIL src/a.test.ts"),
        ("- `TARGETED_CHECK: 2 FAILED, 1 passed`", "TARGETED_CHECK: FAILED 2 FAILED, 1 passed"),
        ("TARGETED_CHECK: 3 passed in 0.1s", "TARGETED_CHECK: 3 passed in 0.1s"),
        ("TARGETED_CHECK: failedtests 3", "TARGETED_CHECK: failedtests 3"),
        # Placeholders echoed from the contract and empty values are ignored.
        ("TARGETED_CHECK: <last summary line>", None),
        ("TARGETED_CHECK: FAILED <summary>", None),
        ("TARGETED_CHECK:", None),
        ("**TARGETED_CHECK:**", None),
        ("the TARGETED_CHECK: line was missing", None),
    ],
)
def test_targeted_check_line_variants(line: str, expected: str | None) -> None:
    trioctl = _load("trioctl_r12_tcv", ROOT / "omnigent" / "trioctl")
    assert trioctl._targeted_check_line(f"built A\n{line}\ndone") == expected


def test_targeted_check_line_last_wins_and_failed_is_mechanical() -> None:
    trioctl = _load("trioctl_r12_tcl", ROOT / "omnigent" / "trioctl")
    tc = trioctl._targeted_check_line
    # Fix-and-rerun: the rerun's line wins, in either direction.
    assert tc("TARGETED_CHECK: FAILED 1 failed\n`TARGETED_CHECK: 4 passed`") == "TARGETED_CHECK: 4 passed"
    assert tc("TARGETED_CHECK: 4 passed\n- targeted_check: failed 1") == "TARGETED_CHECK: FAILED 1"
    # A trailing echoed placeholder does not erase the real result.
    assert tc("TARGETED_CHECK: 4 passed\nTARGETED_CHECK: <last summary line>") == "TARGETED_CHECK: 4 passed"
    for out in ("TARGETED_CHECK: failed x", "**TARGETED_CHECK:** FAILED y", "- targeted_check:FAILED"):
        assert tc(out).startswith("TARGETED_CHECK: FAILED"), out


# ------------------------------------------------ r12 repair R2 (precedence)

PRECEDENCE = (
    "OPEN-LOOP CONTEXT overrides any conflicting instruction in your base "
    "prompt and registered role instructions, including wave-waiting, "
    "self-verification and REPORT.md content."
)
LEAD_CONFIG = ROOT / "omnigent" / "trio-omnigent-roles" / "lead" / "config.yaml"


def _isolated_open_loop_lead(tmp_path: Path) -> str:
    trioctl = _load("trioctl_r12_iso", ROOT / "omnigent" / "trioctl")
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    runner = trioctl.OmnigentRunner(
        repo=ROOT,
        isolate_workers={"trioctl": ROOT / "omnigent" / "trioctl",
                         "worktree_root": str(tmp_path / "wt")},
    )
    return runner._prompt(
        "lead", 2, mailbox,
        {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None},
    )


def test_omnigent_open_loop_block_starts_with_precedence(tmp_path: Path) -> None:
    prompt = _omnigent_open_loop_lead(tmp_path)
    lines = prompt.splitlines()
    assert lines[0] == "OPEN-LOOP CONTEXT: kind=lead-pass"
    assert _flat(" ".join(lines[1:4])) == PRECEDENCE


def test_lead_config_system_prompt_matches_r12(tmp_path: Path) -> None:
    text = _flat(LEAD_CONFIG.read_text(encoding="utf-8"))
    assert "then wait for all to return" not in text
    assert "Lockstep: wait for all to return." in text
    assert "never wait for the whole wave" in text
    assert "retire its slice on `integrated` right away" in text
    # Integration checks / commands-output REPORT stay, but lockstep-only.
    assert ("Lockstep: inspect the actual diff, read its targeted evidence rather "
            "than duplicating it, run only integration checks affected by the diff") in text
    assert "actual commands/output, and delegation provenance" in text
    # r13 G1: "run no suite" replaced by the one whole-tree gate.
    assert "run no suite" not in text
    assert ("Open-loop with isolated builders: do not re-verify builder slices "
            "one by one") in text
    assert "run the PLAN's `full_check:` once on HEAD as the OPEN-LOOP CONTEXT's whole-tree gate" in text
    assert "REPORT.md is the dispatch/merge ledger plus its `## Whole-tree gate` section" in text
    # Smoke-test anchors survive.
    assert "Before any deep reconnaissance" in text
    assert "machine-readable YAML `slices:`" in text


def test_base_step3_review_has_open_loop_exception(tmp_path: Path) -> None:
    prompt = _flat(_omnigent_open_loop_lead(tmp_path))
    assert ("correct integration or correctness issues yourself. (Open-loop: not for "
            "isolated-builder slices -- the OPEN-LOOP CONTEXT procedure's retire "
            "conditions replace this review.)") in prompt


def test_isolate_block_redispatch_is_capped(tmp_path: Path) -> None:
    prompt = _flat(_isolated_open_loop_lead(tmp_path))
    assert "## Isolated builders (on for this loop run)" in prompt
    assert "dispatch a fresh builder for the slice once, then take over (see step 3" in prompt
    assert "dispatch a fresh builder for the slice (or stop for a human)" not in prompt
    assert prompt.startswith("OPEN-LOOP CONTEXT: kind=lead-pass " + PRECEDENCE)


# ------------------------------------- r12 repair R3 (per-builder finish)


def _idiom(block: str, start: str, end: str) -> str:
    """The indented shell lines between two prose markers of the block."""
    body = block[block.index(start) + len(start): block.index(end)]
    return "\n".join(ln[5:] for ln in body.splitlines() if ln.startswith("     "))


def test_open_loop_lead_prompt_gives_per_builder_finish_mechanics(tmp_path: Path) -> None:
    block = _open_loop_block(_isolated_open_loop_lead(tmp_path))
    flat = _flat(block)
    assert "`<mailbox>/.dispatch/<slice>.sh`" in flat
    assert "start the whole wave detached in ONE call" in flat
    assert "It returns as soon as ANY builder finishes" in flat
    assert "Handle each returned slice at once (step 3) before the next wait-next call" in flat
    assert "Do not end the pass while any `.run` marker remains" in flat
    assert "`.dispatch/` is untracked runtime state, like `.sessions/`; never commit it" in flat
    assert "the wait-next call returns it" in flat
    # Mechanics sit inside step 2, before step 3's retire rule.
    assert block.index("wait-next call") < block.index("3. Retire each slice")


def test_dispatch_idiom_reports_each_builder_as_it_finishes(tmp_path: Path) -> None:
    """Run the prompt's exact shell idiom: the fast builder is reported
    while the slow one is still running, then the slow one, then none."""
    import subprocess
    import time

    block = _open_loop_block(_isolated_open_loop_lead(tmp_path))
    dispatch = _idiom(block, "detached in ONE call:", "Then repeat this wait-next call.")
    wait_next = _idiom(block, "its JSON line:", "Handle each returned slice")
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    d = mailbox / ".dispatch"
    d.mkdir()
    for slice_id, delay, rc in (("fast", 0.2, 0), ("slow", 5, 3)):
        (d / f"{slice_id}.sh").write_text(
            f"sleep {delay}\necho built {slice_id}\n"
            f"echo '{{\"targeted_check\": null, \"worker_worktree\": {{\"slice\": \"{slice_id}\"}}}}'\n"
            f"exit {rc}\n"
        )
    sub = lambda text: text.replace("<mailbox>", str(mailbox))
    dispatch = sub(dispatch).replace("<slice-a> <slice-b>", "fast slow")
    wait_next = sub(wait_next)

    def run(script: str) -> str:
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, check=True).stdout

    run(dispatch)
    deadline = time.time() + 10
    while not (d / "fast.rc").exists() and time.time() < deadline:
        time.sleep(0.05)
    first = run(wait_next)
    assert first.splitlines()[0] == "slice=fast exit=0"
    assert '"slice": "fast"' in first
    assert (d / "slow.run").exists()  # the slow builder was still running
    second = run(wait_next)
    assert second.splitlines()[0] == "slice=slow exit=3"
    assert '"slice": "slow"' in second
    assert run(wait_next).strip() == "no builder running"
    deadline = time.time() + 5
    while (d / "slow.run").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert sorted(p.name for p in d.glob("*.done")) == ["fast.done", "slow.done"]


# ------------------------------------ r12 repair R4 (revert failed merges)

REVERT_CMD = (
    '`git revert -m 1 --no-commit <merge_commit> && git commit -m '
    '"revert(<slice>): failed targeted check, re-dispatching"`'
)


def test_omnigent_lead_reverts_failed_merge_before_redispatch(tmp_path: Path) -> None:
    block = _flat(_open_loop_block(_isolated_open_loop_lead(tmp_path)))
    assert "trioctl merges a run before you see its `targeted_check`" in block
    assert "is already on HEAD" in block
    assert "Revert that merge on the aggregate before you re-dispatch" in block
    assert REVERT_CMD in block
    assert "The re-dispatched builder then starts from the reverted HEAD" in block
    assert "retained as `aggregate_dirty` (or `merge_failed`" in block
    assert "for either, run `omnigent worktrees integrate <id>` once the revert commit exists" in block
    assert "The worker ledger keeps the reverted run `integrated`" in block
    assert "post-SHIP cleanup removes it normally" in block
    assert "Do not revert a second failed run; take over on top of it" in block
    # Ordering: revert sits between "re-dispatch once" and the take-over.
    assert (block.index("re-dispatch that slice once")
            < block.index(REVERT_CMD)
            < block.index("If the second run still fails either condition"))


def test_omnigent_lead_integrate_recovery_runs_targeted_check(tmp_path: Path) -> None:
    block = _flat(_open_loop_block(_isolated_open_loop_lead(tmp_path)))
    assert ("A slice recovered with `omnigent worktrees integrate <id>` (after "
            "`merge_conflict`, `aggregate_dirty`, `merge_failed` and the like) prints no "
            "`targeted_check`: run that slice's `## Targeted check` command "
            "yourself once on HEAD") in block
    assert "as the `targeted_check` for the retire decision and the ledger" in block
    assert "plus your one targeted-check run for a `worktrees integrate` recovery (step 3)" in block


def test_canonical_and_generated_leads_carry_revert() -> None:
    section = _flat(_section(_canonical(), "## Open-loop mode"))
    assert REVERT_CMD in section
    assert "run its `## Targeted check` command yourself once on HEAD" in section
    for path, text in _rendered_leads().items():
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        assert "revert(<slice>): failed targeted check, re-dispatching" in flat, path


# ------------------------------------------- r12 repair2 (F1-F5, R2 wording)


def test_targeted_check_helper_flags_unprefixed_failures() -> None:
    trioctl = _load("trioctl_r12_f1", ROOT / "omnigent" / "trioctl")
    tc = trioctl._targeted_check_line
    for value in ("2 failed, 3 passed", "1 error", "no tests ran", "FAIL"):
        assert tc(f"TARGETED_CHECK: {value}") == f"TARGETED_CHECK: FAILED {value}", value
    assert tc("TARGETED_CHECK: 3 passed in 0.1s") == "TARGETED_CHECK: 3 passed in 0.1s"
    # Already-prefixed values are not double-prefixed.
    assert tc("TARGETED_CHECK: FAILED 2 failed") == "TARGETED_CHECK: FAILED 2 failed"
    assert tc("TARGETED_CHECK: failed 1 error") == "TARGETED_CHECK: FAILED 1 error"


def test_lead_step3_treats_unprefixed_failure_text_as_failed(tmp_path: Path) -> None:
    wording = ("A value that reports failures without the prefix (`N failed`, "
               "`N error(s)`, `no tests ran` or `FAIL`, any case) counts as FAILED; "
               "trioctl already normalizes it to `TARGETED_CHECK: FAILED <original>`.")
    block = _flat(_open_loop_block(_isolated_open_loop_lead(tmp_path)))
    assert wording in block
    assert block.index("does not start with `TARGETED_CHECK: FAILED`") < block.index(wording)
    assert wording in _flat(_section(_canonical(), "## Open-loop mode"))
    for path, text in _rendered_leads().items():
        flat = _flat(_flat(text).replace('\\"', '"').replace("\\n", " "))
        assert "trioctl already normalizes it to `TARGETED_CHECK: FAILED <original>`" in flat, path


def test_wait_next_timeout_clause(tmp_path: Path) -> None:
    block = _open_loop_block(_isolated_open_loop_lead(tmp_path))
    flat = _flat(block)
    assert "Run every wait-next call with a shell timeout of at least 910 s" in flat
    assert ("Cursor's Shell tool takes it as its per-call `timeout` parameter, in "
            "milliseconds (`timeout: 910000`; its default is only 30 s)") in flat
    assert ("pass the maximum timeout it allows and never rely on the default") in flat
    assert "If the call still times out, just call it again." in flat
    # The clause sits with the wait-next idiom, before step 3.
    assert (block.index("its JSON line:") < block.index("at least 910 s")
            < block.index("3. Retire each slice"))


def test_revert_window_names_merge_failed(tmp_path: Path) -> None:
    raw = _isolated_open_loop_lead(tmp_path)
    block = _flat(_open_loop_block(raw))
    assert ("retained as `aggregate_dirty` (or `merge_failed`, if its merge started "
            "just as your revert was staged): for either, run `omnigent worktrees "
            "integrate <id>` once the revert commit exists") in block
    assert "`merge_conflict`, `aggregate_dirty`, `merge_failed` and the like) prints no" in block
    prompt = _flat(raw)
    assert ("For `merge_conflict`, `aggregate_dirty`, `merge_failed`, `aggregate_moved`, "
            "`integration_fenced` or `active_session`, remove the cause, then run "
            "`omnigent worktrees integrate <id>` once (`merge_failed` right after an "
            "open-loop revert: once the revert commit exists).") in prompt
    section = _flat(_section(_canonical(), "## Open-loop mode"))
    assert ("may be retained as `aggregate_dirty` or `merge_failed`: run `omnigent "
            "worktrees integrate <id>` once the revert commit exists") in section


def _wait_next_env(tmp_path: Path, cap: int | None = None):
    import subprocess

    block = _open_loop_block(_isolated_open_loop_lead(tmp_path))
    dispatch = _idiom(block, "detached in ONE call:", "Then repeat this wait-next call.")
    wait_next = _idiom(block, "its JSON line:", "Run every wait-next call")
    mailbox = tmp_path / "loop"
    d = mailbox / ".dispatch"
    d.mkdir(parents=True)
    sub = lambda text: text.replace("<mailbox>", str(mailbox))
    wait_next = sub(wait_next)
    if cap is not None:
        assert wait_next.count('-lt 900 ]') == 1
        wait_next = wait_next.replace('-lt 900 ]', f'-lt {cap} ]')

    def run(script: str, prelude: str = "") -> str:
        return subprocess.run(["bash", "-c", prelude + script], capture_output=True,
                              text=True, timeout=60, check=True).stdout

    return sub(dispatch), wait_next, d, run


def _dead_pid() -> int:
    import subprocess

    proc = subprocess.Popen(["sh", "-c", "exit 0"])
    proc.wait()
    return proc.pid


def test_wait_next_removes_stale_marker(tmp_path: Path) -> None:
    _, wait_next, d, run = _wait_next_env(tmp_path)
    pid = _dead_pid()
    (d / "dead.run").write_text(f"{pid}\n")
    (d / "dead.out").write_text('partial\n{"worker_worktree": {"slice": "dead"}}\n')
    out = run(wait_next).splitlines()
    assert out[0] == f"stale marker dead.run removed (wrapper pid {pid} gone; logged to {d}/stale.log)"
    assert out[1] == "slice=dead exit=stale"
    assert '"slice": "dead"' in out[2]
    assert not (d / "dead.run").exists()
    assert (d / "dead.done").read_text().strip() == "stale"
    log = (d / "stale.log").read_text()
    assert f"stale marker dead.run: wrapper pid {pid} gone, removed" in log
    assert run(wait_next).strip() == "no builder running"


def test_wait_next_keeps_live_and_empty_markers(tmp_path: Path) -> None:
    import subprocess

    _, wait_next, d, run = _wait_next_env(tmp_path, cap=2)
    live = subprocess.Popen(["sleep", "30"])
    try:
        (d / "live.run").write_text(f"{live.pid}\n")
        (d / "starting.run").write_text("")  # wrapper has not written its pid yet
        out = run(wait_next).strip()
        assert out == "still running: live.run\nstarting.run"
        assert (d / "live.run").exists() and (d / "starting.run").exists()
        assert not (d / "stale.log").exists()
    finally:
        live.kill()
        live.wait()


def test_wait_next_reports_killed_dispatched_builder_as_stale(tmp_path: Path) -> None:
    """Dispatch with the prompt's exact idiom, SIGKILL the builder's whole
    process group (wrapper + builder), and wait-next reports it stale."""
    import os
    import signal
    import time

    dispatch, wait_next, d, run = _wait_next_env(tmp_path)
    (d / "hang.sh").write_text("sleep 30\n")
    run(dispatch.replace("<slice-a> <slice-b>", "hang"))
    marker = d / "hang.run"
    deadline = time.time() + 10
    while not marker.read_text().strip() and time.time() < deadline:
        time.sleep(0.05)
    pid = int(marker.read_text().strip())
    assert os.getpgid(pid) == pid  # setsid: the wrapper leads its own group
    os.killpg(pid, signal.SIGKILL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    out = run(wait_next).splitlines()
    assert out[1] == "slice=hang exit=stale"
    assert not marker.exists() and not (d / "hang.rc").exists()
    assert "stale marker hang.run" in (d / "stale.log").read_text()


def test_wait_next_rescans_rc_before_no_builder_running(tmp_path: Path) -> None:
    """A builder that writes `.rc` and removes `.run` right after the first
    `.rc` scan is still reported, never `no builder running`."""
    _, wait_next, d, run = _wait_next_env(tmp_path)
    (d / "quick.run").write_text("")
    (d / "quick.out").write_text('{"worker_worktree": {"slice": "quick"}}\n')
    # Deterministic race: the first `[ -e <unmatched *.rc glob> ]` test (the
    # first scan finding nothing) is the moment the builder finishes.
    prelude = (
        f'D0="{d}"\n'
        '[() { if builtin [ ! -e "$D0/fired" ] && builtin [ "$1" = "-e" ] && builtin [ "$2" = "$D0/*.rc" ]; then\n'
        '  : >"$D0/fired"; echo 0 >"$D0/quick.rc"; rm -f "$D0/quick.run"; fi\n'
        '  builtin [ "$@"; }\n'
    )
    out = run(wait_next, prelude).splitlines()
    assert (d / "fired").exists()  # the race was injected
    assert out[0] == "slice=quick exit=0"
    assert '"slice": "quick"' in out[1]
    assert (d / "quick.done").exists()

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
             "is retired (in a fault-only pass: after the last fault fix), run the "
             "repository's whole-tree verification ONCE on HEAD")
GATE_CMD = ("exactly the `full_check:` command(s) PLAN.md's `## Verification "
            "standard` names as the full check")
GATE_BUDGET = ("with a wall-clock budget of 120 s by default — PLAN.md may "
               "override it with a `full_check_budget_s:` line there")
GATE_OLD_GIVEUP = "If it still fails, or it exceeds the budget"
GATE_FIX = ("On failure, fix ONLY within the failing paths: a `slice(<id>): fix")
GATE_RETIRE = "NEW `retired:` entry at the fix sha (step 3's post-retirement rule), then re-run the gate once"
GATE_GIVEUP = ("a second failure or timeout → Known weaknesses: write it (command, "
               "failing tests/errors or the hanging test) into REPORT.md "
               "`## Known weaknesses` and end the pass — the Evaluator decides")
GATE_PARALLEL = "Slice-evals may still be grading the last retirements meanwhile; do not wait for them"
EVAL_LEDGER = ("REPORT.md is the Lead's dispatch/merge ledger plus one "
               "`## Whole-tree gate` result — a claim to check, not evidence; your "
               "own full-suite run is the authoritative verification")


def _assert_gate(text: str) -> None:
    for needle in (GATE_HEAD, GATE_WHEN, GATE_CMD, GATE_BUDGET, GATE_FIX,
                   GATE_RETIRE, GATE_GIVEUP, GATE_PARALLEL):
        assert needle in text, needle
    for needle in (B_NO_SUITE, B_NO_REVERIFY, B_SOLE, GATE_OLD_GIVEUP):
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
    assert "Then `## Whole-tree gate`: a ledger of EVERY gate run of this pass" in block
    assert ("`PASS`, `FAIL` or `TIMEOUT` (a skip row reads `skipped (no product "
            "change since <sha>)`) — the only verification claim you make") in block
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
    assert ("after the last retirement run the OPEN-LOOP CONTEXT's proportional "
            "whole-tree gate on HEAD (120 s budget unless PLAN sets "
            "`full_check_budget_s:`)") in config
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


# ------------------------------------------------------ G2 counts line

COUNTS_TASK = (
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>` after "
    "running the check (pytest: `N passed[, M failed] in ...`; vitest: ` "
    "Tests N passed | M failed`, not `Duration`; go test: `ok`/`FAIL`; "
    "otherwise `TARGETED_CHECK: PASS <n>` or `TARGETED_CHECK: FAILED <summary>`)."
)
TSC_RULE = ("When the slice's `writes:` include `.ts` or `.tsx` files, the "
            "command MUST also typecheck the builder's project: prefix it with "
            "`npx tsc --noEmit -p <project> && `")


def test_isolated_builder_note_names_counts_line() -> None:
    note = _flat(_trioctl("note")._ISOLATED_BUILDER_NOTE.format(
        path="/w", branch="b", base="c", repo="/r", slice="s", mailbox="/m"))
    assert "LAST summary line" not in note
    assert "<last summary line>" not in note
    assert "print the line of its output that states the pass/fail COUNTS" in note
    assert "TARGETED_CHECK: <counts line>" in note
    assert "pytest -> the `N passed[, M failed] in ...` line" in note
    assert "vitest -> the ` Tests N passed | M failed` line (NOT the `Duration` line)" in note
    assert "go test -> the `ok` / `FAIL` line" in note
    assert "print TARGETED_CHECK: PASS <n>" in note
    assert "TARGETED_CHECK: FAILED <summary>" in note
    assert "use the test runner's counts line; any non-zero exit is a failure" in note


def test_lead_task_template_counts_line_and_tsc(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    assert f'"{COUNTS_TASK}"' in block
    assert "<last summary line>" not in block
    assert "last summary line" not in block.split("7. REPORT.md")[0]
    assert TSC_RULE in block
    assert "`npx tsc --noEmit -p api && npx vitest run api/test/x.test.ts`" in block
    assert "with the command's counts line as its ledger evidence" in block
    canonical = _flat(_section((CANONICAL / "lead.md").read_text(encoding="utf-8"),
                               "## Open-loop mode"))
    assert COUNTS_TASK in canonical and TSC_RULE in canonical
    assert "<last summary line>" not in canonical
    for path, flat in _generated_role("lead"):
        assert COUNTS_TASK in flat, path
        assert TSC_RULE in flat, path


def test_builder_sources_name_counts_line() -> None:
    sources = [
        _flat((CANONICAL / "builder.md").read_text(encoding="utf-8")),
        _flat((ROLES / "builder" / "config.yaml").read_text(encoding="utf-8")),
    ]
    sources += [_flat(t) for p, t in _generated().items() if "builder" in p.name]
    assert len(sources) > 2
    for flat in sources:
        if "`## Targeted check` section" not in flat:
            continue
        assert "LAST summary line" not in flat
        assert "the output line that states the pass/fail COUNTS verbatim" in flat
        assert "vitest → the ` Tests N passed | M failed` line (NOT the `Duration` line)" in flat
        assert "pytest → the `N passed[, M failed] in …` line" in flat
        assert "go test → the `ok` / `FAIL` line" in flat
        assert "`TARGETED_CHECK: PASS <n>` on success" in flat


def test_counts_lines_parse_through_real_helper() -> None:
    tc = _trioctl("tc")._targeted_check_line
    assert tc("TARGETED_CHECK: Tests  9 passed (9)") == "TARGETED_CHECK: Tests  9 passed (9)"
    assert tc("TARGETED_CHECK: Tests  1 failed | 384 passed (385)").startswith("TARGETED_CHECK: FAILED")
    assert tc("TARGETED_CHECK: FAIL\texample.com/pkg\t0.01s").startswith("TARGETED_CHECK: FAILED")
    assert tc("TARGETED_CHECK: ok  \texample.com/pkg\t0.01s") is not None
    assert tc("TARGETED_CHECK: PASS 3") == "TARGETED_CHECK: PASS 3"
    assert tc("TARGETED_CHECK: <counts line>") is None
    assert tc("TARGETED_CHECK: <the line stating the pass/fail counts>") is None


def test_zero_failure_counts_are_not_failed() -> None:
    """r13 L-2: `| 0 failed`, `0 errors` are passes, not FAILED."""
    tc = _trioctl("zero")._targeted_check_line
    for value in (
        "Tests  10 passed | 0 failed",
        "Found 0 errors",
        "0 errors",
        "0 failed, 12 passed",
        "PASS 12 (0 errors)",
        "12 passed, 0 failed, 0 errors in 0.3s",
    ):
        assert tc(f"TARGETED_CHECK: {value}") == f"TARGETED_CHECK: {value}", value
    for value in (
        "1 failed",
        "2 failed, 3 passed",
        "Tests  1 failed | 384 passed (385)",
        "10 failed",
        "1 error",
        "3 errors",
        "12 passed, 0 failed, 1 error in 0.3s",
        "no tests ran",
        "FAIL\texample.com/pkg\t0.01s",
    ):
        assert tc(f"TARGETED_CHECK: {value}") == f"TARGETED_CHECK: FAILED {value}", value
    assert tc("TARGETED_CHECK: FAILED 0 passed") == "TARGETED_CHECK: FAILED 0 passed"
    assert tc("TARGETED_CHECK: failed 1") == "TARGETED_CHECK: FAILED 1"



# ------------------------------------------ r13 doubt 2: template literals


def test_template_literals_copied_verbatim_are_not_reported() -> None:
    tc = _trioctl("tpl")._targeted_check_line
    literals = (
        "PASS <n>", "PASS <count>", "<last summary line>", "<counts line>",
        "<n> passed", "<n> passed in <t>s", "Tests <n> passed", "PASS n",
        "...", "\u2026", "counts line", "last summary line", "FAILED <summary>",
        "`PASS <n>`",
    )
    for value in literals:
        assert tc(f"TARGETED_CHECK: {value}") is None, value
        assert tc(f"- **TARGETED_CHECK:** {value}") is None, value
        # An echoed literal after a real result does not erase it ...
        assert tc(f"TARGETED_CHECK: 4 passed\nTARGETED_CHECK: {value}") == "TARGETED_CHECK: 4 passed", value
        # ... and a literal alone never reads as a pass.
        assert tc(f"built\nTARGETED_CHECK: {value}\ndone") is None, value


def test_real_values_survive_template_filter() -> None:
    tc = _trioctl("tplok")._targeted_check_line
    assert tc("TARGETED_CHECK: PASS 3") == "TARGETED_CHECK: PASS 3"
    assert tc("TARGETED_CHECK: 4 passed in 0.12s") == "TARGETED_CHECK: 4 passed in 0.12s"
    assert tc("TARGETED_CHECK: Tests  9 passed (9)") == "TARGETED_CHECK: Tests  9 passed (9)"
    assert tc("TARGETED_CHECK: PASS") == "TARGETED_CHECK: PASS"
    # A real failure quoting `<...>` text is kept (and stays FAILED).
    assert (tc("TARGETED_CHECK: FAILED test_x - assert <Foo> == 1")
            == "TARGETED_CHECK: FAILED test_x - assert <Foo> == 1")
    assert (tc("TARGETED_CHECK: 1 failed: <Foo> != 2")
            == "TARGETED_CHECK: FAILED 1 failed: <Foo> != 2")


# ------------------------------------------------ G3 append-only retire

APPEND_ONLY = ("appends ONE new entry at the end of the block, with `at:` = that "
               "sha's committer time from `git log -1 --format=%cI <merge_commit>` "
               "— never an invented or estimated time")
NO_REWRITE = "Never edit, replace, reorder or delete existing entries"
AWK = ("awk '/^```/{f=0} f&&/^  - slice:/{n++} /^retired:/{f=1} "
       "END{print n+0}' QUEUE.md")
GREP_CHECK = _flat("count the entries inside the `retired:` fence only, before and after: "
                   f"`{AWK}` — an append must grow it by exactly one")
FENCE_RULE = ("The new entry goes INSIDE the ```yaml `retired:` fence, before its "
              "closing ```, indented as a list item of `retired:`")
FENCE_EXAMPLE = (
    "```yaml\nretired:\n"
    "  - slice: status-parse\n"
    "    sha: af7d8220c4d606f549c4a374c9e22b6a6a03ec04\n"
    "    at: 2026-09-27T03:03:20Z\n"
    "  - slice: cli-whoami\n"
    "    sha: e688fdf5493dac69e4db40345a69aa1287cd6aa1\n"
    "    at: 2026-09-27T03:04:53Z\n"
    "```\n"
)
SHA_KEYS = ("exactly three keys, in this order: `slice:`, `sha:`")
SHA_NOT_MERGE = "the key is `sha:`, never `merge_commit:`"
REPAIR = ("The ONLY edit allowed to an existing entry is repairing one the loop "
          "has logged as malformed (`QUEUE.md: slice <id> has a malformed retired "
          "entry`): fix that entry's keys in place, change nothing else.")


def test_omnigent_step3_retire_is_append_only(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    step3 = block[block.index("3. Retire each slice"): block.index("4. Never wait for a verdict")]
    assert "QUEUE.md `retired:` is APPEND-ONLY" in step3
    for needle in (APPEND_ONLY, NO_REWRITE, GREP_CHECK):
        assert needle in step3, needle
    assert "insert after the block's last line" not in step3
    (tmp_path / "raw").mkdir()
    assert f"`{AWK}`" in _block(_lead_prompt(tmp_path / "raw"))
    assert "grep -c 'slice:'" not in step3
    for needle in (FENCE_RULE, SHA_KEYS, SHA_NOT_MERGE, REPAIR,
                   "`slice:`, `sha:` (the full `merge_commit` sha"):
        assert needle in step3, needle
    # The rule sits before the retire conditions it governs.
    assert step3.index(APPEND_ONLY) < step3.index("Retire only when both hold")


def test_canonical_and_generated_leads_retire_append_only() -> None:
    section = _flat(_section((CANONICAL / "lead.md").read_text(encoding="utf-8"),
                             "## Open-loop mode"))
    assert "**`retired:` is append-only:**" in section
    for needle in (APPEND_ONLY, NO_REWRITE, GREP_CHECK):
        assert needle in section, needle
    for needle in (FENCE_RULE, SHA_KEYS, SHA_NOT_MERGE, REPAIR):
        assert needle in section, needle
    assert "insert after the block's last line" not in section
    for path, flat in _generated_role("lead"):
        assert APPEND_ONLY in flat and GREP_CHECK in flat, path
        assert FENCE_RULE in flat and REPAIR in flat and SHA_NOT_MERGE in flat, path
    schema = _flat((ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8"))
    assert "the committer time of `sha` (`git log -1 --format=%cI <sha>`)" in schema
    assert ("Lead appends each entry INSIDE the ```yaml `retired:` fence, before its "
            "closing ```, indented as a list item of `retired:`") in schema
    assert ("exactly these three keys, in this order: `slice:`, `sha:`, `at:`. "
            "The key is `sha:` — never `merge_commit:`") in schema
    assert REPAIR in schema
    assert _flat(f"`{AWK}`") in schema
    # Unflattened: the exact command (two-space list indent) is copyable.
    assert f"`{AWK}`" in (ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8")
    assert f"`{AWK}`" in (CANONICAL / "lead.md").read_text(encoding="utf-8")
    assert "after the block's last line" not in schema
    assert "grep -c 'slice:'" not in schema


def _dedent_example(text: str) -> str:
    """The literal ```yaml example in ``text``, dedented."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "```yaml"
                 and lines[i + 1].strip() == "retired:")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "```")
    indent = len(lines[start]) - len(lines[start].lstrip())
    return "\n".join(l[indent:] for l in lines[start:end + 1]) + "\n"


def test_fence_example_is_literal_and_parses(tmp_path: Path) -> None:
    trioctl_block = _block(_lead_prompt(tmp_path))
    sources = {
        "trioctl": trioctl_block,
        "canonical": (CANONICAL / "lead.md").read_text(encoding="utf-8"),
        "schema": (ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8").split(
            "`retired:` is **append-only, Lead only**")[1],
    }
    for path, _flat_text in _generated_role("lead"):
        sources[str(path)] = _generated()[path].replace("\\n", "\n")
    tm = _load("trio_metrics_r13_fence", ROOT / "metrics" / "trio-metrics.py")
    for name, text in sources.items():
        example = _dedent_example(text)
        assert example == FENCE_EXAMPLE, name
        # The example is a well-formed QUEUE.md: two retired entries, no errors.
        queue = tm.parse_queue_block(example)
        assert queue["errors"] == [] and queue["malformed_slices"] == [], name
        assert [e["slice"] for e in queue["retired"]] == ["status-parse", "cli-whoami"], name


# The real hard-fixture shape (speed/hard/runs/subusage/D/repo/loop-hard/QUEUE.md).
HARD_QUEUE = (
    "```yaml\nretired:\n"
    "  - slice: status-parse\n    sha: af7d8220c4d606f549c4a374c9e22b6a6a03ec04\n    at: 2026-09-27T03:03:20Z\n"
    "  - slice: docs-cursor-whoami\n    sha: 245dae8fc3a9522fa8a722a050cb9085d6e50b9d\n    at: 2026-09-27T03:03:45Z\n"
    "  - slice: cli-whoami\n    sha: e688fdf5493dac69e4db40345a69aa1287cd6aa1\n    at: 2026-09-27T03:04:53Z\n"
    "  - slice: collect-timeline\n    sha: f22ebc77de143b83e70405460a8fd2020d9c9d5c\n    at: 2026-09-27T03:05:54Z\n"
    "```\n\n```yaml\nfaults:\n```\n"
)
NEW_ENTRY = "  - slice: x\n    sha: " + "a" * 40 + "\n    at: 2026-09-27T04:00:00Z\n"
FAULT = ("  - id: f1\n    slice: cli-whoami\n    observed_at: " + "b" * 40 +
         "\n    scope: [a.py]\n    reason: r\n    status: open\n")


def _awk_count(tmp_path: Path, text: str) -> int:
    import shutil
    import subprocess
    if shutil.which("awk") is None:
        import pytest
        pytest.skip("awk not installed")
    q = tmp_path / "QUEUE.md"
    q.write_text(text, encoding="utf-8")
    script = AWK[len("awk '"):-len("' QUEUE.md")]
    out = subprocess.run(["awk", script, str(q)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def test_awk_self_check_counts_only_retired_fence(tmp_path: Path) -> None:
    assert _awk_count(tmp_path, HARD_QUEUE) == 4
    assert _awk_count(tmp_path, "") == 0
    inside = HARD_QUEUE.replace("```\n\n```yaml\nfaults:", NEW_ENTRY + "```\n\n```yaml\nfaults:", 1)
    assert _awk_count(tmp_path, inside) == 5
    # An entry after the closing fence (M-1) is NOT counted: the self-check fails.
    outside = HARD_QUEUE.replace("```\n\n```yaml\nfaults:", "```\n" + NEW_ENTRY + "\n```yaml\nfaults:", 1)
    assert _awk_count(tmp_path, outside) == 4
    # A concurrently appended fault (L-4) does not move the count.
    faulted = HARD_QUEUE.replace("faults:\n```", "faults:\n" + FAULT + "```")
    assert _awk_count(tmp_path, faulted) == 4
    assert _awk_count(tmp_path, faulted.replace("```\n\n```yaml\nfaults:", NEW_ENTRY + "```\n\n```yaml\nfaults:", 1)) == 5
    # Faults block first, retired second.
    swapped = "```yaml\nfaults:\n" + FAULT + "```\n\n" + HARD_QUEUE.split("\n\n")[0] + "\n"
    assert _awk_count(tmp_path, swapped) == 4


# ------------------------------------------------ L-3 tsc <project> rule

TSC_PROJECT = ("`<project>` is the directory of the nearest tsconfig.json at or above "
               "the slice's first `writes:` path; if none, omit the tsc prefix.")


def _tsc_project(root: Path, writes: list[str]) -> str | None:
    """The documented rule, applied mechanically to a fake tree."""
    first = root / writes[0]
    for d in [first.parent, *first.parent.parents]:
        if (d / "tsconfig.json").is_file():
            return d.relative_to(root).as_posix() or "."
        if d == root:
            break
    return None


def test_tsc_project_rule_named_and_applies(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    assert TSC_PROJECT in block
    canonical = _flat(_section((CANONICAL / "lead.md").read_text(encoding="utf-8"),
                               "## Open-loop mode"))
    assert TSC_PROJECT in canonical
    for path, flat in _generated_role("lead"):
        assert TSC_PROJECT in flat, path
    repo = tmp_path / "repo"
    (repo / "api" / "src" / "routes").mkdir(parents=True)
    (repo / "api" / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "web").mkdir()
    # The example `-p api` falls out of the rule for a fake api slice.
    assert _tsc_project(repo, ["api/src/routes/x.ts", "api/test/x.test.ts"]) == "api"
    # Only the FIRST writes: path decides.
    assert _tsc_project(repo, ["web/y.ts", "api/src/z.ts"]) is None
    (repo / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert _tsc_project(repo, ["web/y.ts"]) == "."
    assert _tsc_project(repo, ["api/src/routes/x.ts"]) == "api"


# ------------------------------------------- G4 whole-goal deliverables

LEAD_INTEGRATION_PLAN = (
    "Every whole-goal deliverable that is not inside a slice (smoke evidence, "
    "an `evidence/` dir, a generated report) MUST be assigned either to a "
    "slice's `writes:` or to a `lead_integration:` line in the same section"
)
EVIDENCE_GAP = ("evidence that does not meet the declared standard is an ITERATE "
                "whose failure scope is the evidence gap itself")


def test_plan_template_requires_lead_integration() -> None:
    canonical = _flat((CANONICAL / "lead.md").read_text(encoding="utf-8"))
    assert LEAD_INTEGRATION_PLAN in canonical
    assert "the deliverables the Lead produces itself after the gate" in canonical
    assert "nothing GOAL requires may be left unowned" in canonical
    for path, flat in _generated_role("lead"):
        assert LEAD_INTEGRATION_PLAN in flat, path
    config = _flat((ROLES / "lead" / "config.yaml").read_text(encoding="utf-8"))
    assert ("Assign every whole-goal deliverable not inside a slice (smoke evidence, "
            "an `evidence/` dir) to a slice's `writes:` or to a `lead_integration:` "
            "line there, which you produce after the whole-tree gate") in config
    schema = _flat((ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8"))
    assert "**`lead_integration:`** (required when any exist)" in schema


def test_open_loop_lead_does_lead_integration_after_gate(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    step6 = block[block.index("6. Whole-tree gate"): block.index("7. REPORT.md")]
    assert "Then produce each PLAN.md `lead_integration:` deliverable" in step6
    assert step6.index(GATE_GIVEUP) < step6.index("`lead_integration:` deliverable")
    assert "`## Lead integration` (each `lead_integration:` deliverable" in block
    canonical = (CANONICAL / "lead.md").read_text(encoding="utf-8")
    assert "Then produce each PLAN.md `lead_integration:`" in _flat(
        _section(canonical, "## Open-loop mode"))
    output = canonical[canonical.index("## Output"): canonical.index("## Rules")]
    assert "## Lead integration" in output.split("Open-loop (`loop/QUEUE.md` exists)", 1)[1]


def test_evaluator_keeps_unverified_evidence_iterate(tmp_path: Path) -> None:
    prompt = _flat(_integration_eval_prompt(tmp_path))
    assert EVIDENCE_GAP in prompt
    for path, flat in _generated_role("evaluator"):
        assert "Verification standard" in flat, path


# ------------------------------- G-1..G-3 proportional whole-tree gate

GATE_GREP = "grep -oE 'gate: PASS @[0-9a-f]{40}' <mailbox>/LOG.md | tail -n 1"
GATE_DIFF = "git diff --quiet <gate_sha> HEAD -- . ':!<mailbox>'"
GATE_SKIP = ("the pass committed no product code since that gate: run nothing "
             "and record `gate: skipped (no product change since <gate_sha>)`")
GATE_INTEGRATION = (
    "Otherwise run the typecheck/lint named in `full_check:` (if any) plus the "
    "union of the `## Targeted check` commands of every slice this pass retired "
    "(builder merges, take-overs, fixes), re-run on merged HEAD under one budget"
)
GATE_NO_TARGETED = ("If this pass retired no slice with a targeted check (e.g. it "
                    "fixed only an `integration` fault), run the full check.")
GATE_FULL_WHEN = ("run only when that section has `cross_cutting: true` or declares "
                  "`full_check_budget_s:`")
GATE_AUTHORITATIVE = "The integration eval's own full suite stays the authoritative check."
GATE_TIMEOUT = ("A timeout is a failure: identify the hanging test, fix within its "
                "paths, re-run once; a second failure or timeout → Known weaknesses")
GATE_LOG = ("End your LOG.md line with the pass's last gate outcome: "
            "`gate: PASS @<full HEAD sha>`, `gate: FAIL @<full HEAD sha>` or "
            "`gate: skipped (no product change since <gate_sha>)`")
GATE_FAULT_PASS = "(in a fault-only pass: after the last fault fix)"


def _assert_proportional(text: str) -> None:
    for needle in (f"`{GATE_GREP}`", f"`{GATE_DIFF}`", GATE_SKIP, GATE_INTEGRATION,
                   GATE_NO_TARGETED, GATE_FULL_WHEN, GATE_AUTHORITATIVE,
                   GATE_TIMEOUT, GATE_FAULT_PASS, GATE_CMD, GATE_BUDGET):
        assert needle in text, needle
    assert "gate: PASS @<full HEAD sha>" in text
    # The skip check precedes the scopes; the scopes precede the failure rule.
    assert text.index(GATE_SKIP) < text.index(GATE_INTEGRATION) < text.index(GATE_FULL_WHEN)
    assert text.index(GATE_FULL_WHEN) < text.index(GATE_TIMEOUT)


def test_omnigent_open_loop_gate_is_proportional(tmp_path: Path) -> None:
    for isolate in (True, False):
        sub = tmp_path / str(isolate)
        sub.mkdir()
        block = _flat(_block(_lead_prompt(sub, isolate=isolate)))
        step6 = block[block.index("6. Whole-tree gate"): block.index("7. REPORT.md")]
        _assert_proportional(step6)
        assert GATE_LOG in step6
        assert "`full_check_budget_s:` <= 60" in step6
        # The integration check is the default; the full check is not.
        assert step6.index("Default scope: the integration check") < step6.index(
            "Full check only when warranted")
    # Unflattened: the commands are copyable verbatim from the raw block.
    (tmp_path / "raw").mkdir()
    raw = _block(_lead_prompt(tmp_path / "raw"))
    assert f"`{GATE_GREP}`" in raw and f"`{GATE_DIFF}`" in raw


def test_canonical_and_generated_leads_gate_is_proportional() -> None:
    canonical = (CANONICAL / "lead.md").read_text(encoding="utf-8")
    section = _flat(_section(canonical, "## Open-loop mode"))
    _assert_proportional(section)
    assert "`full_check_budget_s:` ≤ 60" in section
    assert _flat(GATE_LOG.replace("End your", "end your")) in section
    assert f"`{GATE_GREP}`" in canonical and f"`{GATE_DIFF}`" in canonical
    # The one LOG.md read is named as the only exception to "never read it".
    economics = _flat(_section(canonical, "## Context economics"))
    assert ("Open-loop step 6's one `grep` for the last `gate: PASS @<sha>` is the "
            "only exception") in economics
    for path, flat in _generated_role("lead"):
        _assert_proportional(flat)
        assert f"`{GATE_GREP}`" in flat, path


def test_report_gate_section_is_a_per_pass_ledger(tmp_path: Path) -> None:
    block = _flat(_block(_lead_prompt(tmp_path)))
    step7 = block[block.index("7. REPORT.md"):]
    for needle in (
        "a ledger of EVERY gate run of this pass, in order, including timeouts and skips",
        "never overwrite an earlier row within the pass",
        "scope (`integration`, `full` or `skipped`) | the exact command(s) | duration in s",
        "`PASS`, `FAIL` or `TIMEOUT`",
    ):
        assert needle in step7, needle
    canonical = (CANONICAL / "lead.md").read_text(encoding="utf-8")
    output = canonical[canonical.index("## Output"): canonical.index("## Rules")]
    open_loop = _flat(output.split("Open-loop (`loop/QUEUE.md` exists)", 1)[1])
    for needle in (
        "## Whole-tree gate (a ledger of EVERY gate run of this pass, in order, "
        "including timeouts and skips; never overwrite an earlier row within the pass",
        "scope `integration` | `full` | `skipped` | the exact command | duration in s",
        "PASS, FAIL or TIMEOUT; a skip row reads `skipped (no product change since <sha>)`",
    ):
        assert needle in open_loop, needle
    for path, flat in _generated_role("lead"):
        assert "a ledger of EVERY gate run of this pass" in flat, path


def test_plan_template_names_cross_cutting() -> None:
    canonical = _flat((CANONICAL / "lead.md").read_text(encoding="utf-8"))
    assert "`cross_cutting: true` when the iteration changes shared code" in canonical
    assert "All three are plain lines under the heading, never keys in the `slices:` block" in canonical
    config = _flat((ROLES / "lead" / "config.yaml").read_text(encoding="utf-8"))
    assert "optional `cross_cutting: true` when the change touches shared code" in config
    assert ("skipped when no product code changed since the last `gate: PASS @<sha>` "
            "LOG.md line") in config
    assert ("the full `full_check:` only when PLAN sets `cross_cutting: true` or "
            "`full_check_budget_s:` <= 60") in config
    assert "every gate run of the pass, timeouts and skips included" in config
    schema = _flat((ROOT / "MAILBOX-SCHEMA.md").read_text(encoding="utf-8"))
    assert "**`cross_cutting:`** (optional, default false)" in schema
    assert ("Like `full_check:`, a plain line under this heading — never a key in "
            "the `slices:` block") in schema
    docs = _flat((ROOT / "docs" / "CONCURRENT-SLICE-EVAL.md").read_text(encoding="utf-8"))
    assert "gate: skipped (no product change since <sha>)" in docs
    assert "`cross_cutting: true`" in docs
    for path, flat in _generated_role("lead"):
        assert "`cross_cutting: true` when the iteration changes shared code" in flat, path


def _git(repo: Path, *args: str, check: bool = True):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=check)


def test_gate_skip_commands_apply_mechanically(tmp_path: Path) -> None:
    """The documented LOG.md lookup and product-diff check, run for real."""
    import shutil
    import subprocess
    if shutil.which("git") is None or shutil.which("grep") is None:
        import pytest
        pytest.skip("git/grep not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "loop-hard").mkdir()
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "loop-hard" / "PLAN.md").write_text("plan\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "slice(a): one")
    gate_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    old_sha = "c" * 40
    log = repo / "loop-hard" / "LOG.md"
    log.write_text(
        "# Trio loop log\n"
        f"- iter 1 | lead | wave 1 retired; gate: PASS @{old_sha}\n"
        f"- iter 1 | evaluator | ITERATE f1\n"
        f"- iter 2 | lead | fix f1; gate: PASS @{gate_sha}\n"
        f"- iter 3 | lead | f2 stale; gate: skipped (no product change since {gate_sha})\n"
        f"- iter 4 | lead | fix f3; gate: FAIL @{'d' * 40}\n",
        encoding="utf-8",
    )

    def lookup() -> str:
        cmd = GATE_GREP.replace("<mailbox>", "loop-hard")
        out = subprocess.run(["sh", "-c", cmd], cwd=repo, capture_output=True, text=True)
        return out.stdout.strip()

    # The LAST PASS wins; skip and FAIL lines never become the baseline.
    assert lookup() == f"gate: PASS @{gate_sha}"
    found = lookup().split("@", 1)[1]

    def unchanged() -> bool:
        cmd = GATE_DIFF.replace("<gate_sha>", found).replace("<mailbox>", "loop-hard")
        return subprocess.run(["sh", "-c", cmd], cwd=repo).returncode == 0

    assert unchanged()
    # Mailbox-only commits (PLAN/QUEUE/LOG) are not product changes.
    (repo / "loop-hard" / "QUEUE.md").write_text("q\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "loop: mailbox")
    assert unchanged()
    # A product commit makes the gate run.
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "slice(a): fix f3")
    assert not unchanged()
    # No PASS recorded yet -> nothing to skip against.
    log.write_text("# Trio loop log\n- iter 1 | lead | gate: FAIL @" + "d" * 40 + "\n",
                   encoding="utf-8")
    assert lookup() == ""


PLAN_VS_BASE = """\
# Plan

```yaml
slices:
  - id: alpha
    writes: [a.py]
    status: complete
  - id: beta
    writes: [b.py]
    status: planned
```

## Verification standard
- **Mode**: `implement-then-smoke`.
"""
PLAN_VS_KEYS = PLAN_VS_BASE + (
    "full_check: python3 -m pytest -q\n"
    "full_check_budget_s: 150\n"
    "cross_cutting: true\n"
    "lead_integration: evidence/full-check.txt\n"
)


def test_trio_check_accepts_plans_with_and_without_gate_keys(tmp_path: Path) -> None:
    import subprocess
    import sys
    checker = ROOT / "metrics" / "trio-check.py"
    tm = _load("trio_metrics_r13_keys", ROOT / "metrics" / "trio-metrics.py")
    sha = "0123456789abcdef0123456789abcdef01234567"
    for name, plan in (("without", PLAN_VS_BASE), ("with", PLAN_VS_KEYS),
                       ("false", PLAN_VS_KEYS.replace("cross_cutting: true",
                                                      "cross_cutting: false"))):
        mailbox = tmp_path / name
        mailbox.mkdir()
        (mailbox / "GOAL.md").write_text("# Goal\nprofile: software\nFixture.\n", encoding="utf-8")
        (mailbox / "STATE.md").write_text(
            "schema: 1\niteration: 1\nmax_iterations: 5\nstatus: iterating\n"
            "mission: Fixture.\n", encoding="utf-8")
        (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
        (mailbox / "REPORT.md").write_text("Fixture.\n", encoding="utf-8")
        (mailbox / "VERDICT.md").write_text(f"## slice alpha @{sha} — SHIP\nok\n",
                                            encoding="utf-8")
        (mailbox / "LOG.md").write_text(
            f"# Trio loop log\n- iter 1 | lead | wave 1; gate: PASS @{sha}\n",
            encoding="utf-8")
        (mailbox / "QUEUE.md").write_text(
            "```yaml\nretired:\n  - slice: alpha\n"
            f"    sha: {sha}\n    at: 2026-08-26T10:00:00Z\n```\n\n"
            "```yaml\nfaults:\n```\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(checker), str(mailbox)],
                                capture_output=True, text=True)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        # The plain lines never leak into the slices block.
        slices = tm.parse_slices_block(plan)
        assert [s["id"] for s in slices] == ["alpha", "beta"], name
        for s in slices:
            assert "cross_cutting" not in s and "full_check" not in s, name

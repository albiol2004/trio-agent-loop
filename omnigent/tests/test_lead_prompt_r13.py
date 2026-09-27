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

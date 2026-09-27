"""Tests for the v1 open-loop QUEUE.md extension (MAILBOX-SCHEMA.md
"v1 open-loop extension (optional)"):

- trio-metrics.py: find_queue_block / parse_retired / parse_faults /
  parse_queue_block / read_queue, plus the `accepts:` slice key.
- trio-check.py: check_queue validation, and the check_verdict relaxation
  for a QUEUE.md mailbox whose VERDICT.md carries only per-slice headings.

trio-metrics.py and trio-check.py have hyphenated filenames, so both are
loaded by path via importlib, exactly like the sibling test modules.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

METRICS_PATH = Path(__file__).parents[1] / "trio-metrics.py"
CHECKER = Path(__file__).parents[1] / "trio-check.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TM = _load(METRICS_PATH, "trio_metrics")


# --- read_queue / find_queue_block / parse_queue_block -----------------------


def test_read_queue_no_file_returns_empty_queues(tmp_path: Path) -> None:
    """A2: no QUEUE.md at all -> empty queues, no exception."""
    assert TM.read_queue(tmp_path) == {
        "retired": [], "faults": [], "errors": [], "malformed_slices": []
    }


def test_read_queue_empty_file_returns_empty_queues(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text("", encoding="utf-8")
    assert TM.read_queue(tmp_path) == {
        "retired": [], "faults": [], "errors": [], "malformed_slices": []
    }


def test_read_queue_only_retired_block(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text(
        "```yaml\n"
        "retired:\n"
        "  - slice: alpha\n"
        "    sha: " + "a" * 40 + "\n"
        "    at: 2026-08-26T10:00:00Z\n"
        "```\n",
        encoding="utf-8",
    )
    queue = TM.read_queue(tmp_path)
    assert queue["faults"] == []
    assert len(queue["retired"]) == 1
    assert queue["retired"][0]["slice"] == "alpha"


def test_read_queue_only_faults_block(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text(
        "```yaml\n"
        "faults:\n"
        "  - id: f1\n"
        "    slice: alpha\n"
        "    observed_at: " + "a" * 40 + "\n"
        "    scope: [a.py]\n"
        "    reason: broke it\n"
        "    status: open\n"
        "```\n",
        encoding="utf-8",
    )
    queue = TM.read_queue(tmp_path)
    assert queue["retired"] == []
    assert len(queue["faults"]) == 1
    assert queue["faults"][0]["id"] == "f1"


QUEUE_ROUNDTRIP = """\
```yaml
retired:
  - slice: alpha
    sha: 0123456789abcdef0123456789abcdef01234567
    at: 2026-08-26T10:00:00Z
  - slice: beta
    sha: fedcba9876543210fedcba9876543210fedcba98
    at: 2026-08-26T11:30:00Z
```

```yaml
faults:
  - id: f1
    slice: alpha
    observed_at: 0123456789abcdef0123456789abcdef01234567
    scope: [a.py, "lib/util.py"]
    reason: broke the thing
    status: open
  - id: f2
    slice: beta
    observed_at: fedcba9876543210fedcba9876543210fedcba98
    scope:
      - c.py
      - d.py
    reason: another one
    status: done
```
"""


def test_roundtrip_two_retired_two_faults_all_fields(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text(QUEUE_ROUNDTRIP, encoding="utf-8")
    queue = TM.read_queue(tmp_path)

    assert queue["retired"] == [
        {
            "slice": "alpha",
            "sha": "0123456789abcdef0123456789abcdef01234567",
            "at": "2026-08-26T10:00:00Z",
        },
        {
            "slice": "beta",
            "sha": "fedcba9876543210fedcba9876543210fedcba98",
            "at": "2026-08-26T11:30:00Z",
        },
    ]
    assert queue["faults"] == [
        {
            "id": "f1",
            "slice": "alpha",
            "observed_at": "0123456789abcdef0123456789abcdef01234567",
            "scope": ["a.py", "lib/util.py"],
            "reason": "broke the thing",
            "status": "open",
        },
        {
            "id": "f2",
            "slice": "beta",
            "observed_at": "fedcba9876543210fedcba9876543210fedcba98",
            "scope": ["c.py", "d.py"],
            "reason": "another one",
            "status": "done",
        },
    ]
    # Flow-list and block-list `scope:` styles both parsed correctly.
    assert queue["faults"][0]["scope"] == ["a.py", "lib/util.py"]
    assert queue["faults"][1]["scope"] == ["c.py", "d.py"]


def test_find_queue_block_absent_returns_none() -> None:
    assert TM.find_queue_block("# no fences here\n", "retired") is None
    assert TM.find_queue_block("```yaml\nfaults:\n  - id: f1\n```\n", "retired") is None


def test_malformed_block_raises_strict_and_empty_lenient() -> None:
    """A missing required key: QueueParseError from the strict parser, []
    from the lenient parse_queue_block wrapper."""
    text = (
        "```yaml\n"
        "retired:\n"
        "  - slice: alpha\n"
        "    sha: " + "a" * 40 + "\n"
        # `at:` is missing.
        "```\n"
    )
    lines = TM.find_queue_block(text, "retired")
    assert lines is not None
    with pytest.raises(TM.QueueParseError, match=r"missing required key"):
        TM.parse_retired(lines)

    queue = TM.parse_queue_block(text)
    assert queue["retired"] == [] and queue["faults"] == []
    assert len(queue["errors"]) == 1
    assert "`retired:` block" in queue["errors"][0]
    assert "missing required key" in queue["errors"][0]


def test_malformed_faults_block_raises_strict_and_reports_lenient() -> None:
    text = (
        "```yaml\n"
        "faults:\n"
        "  - id: f1\n"
        "    slice: alpha\n"
        "    observed_at: " + "a" * 40 + "\n"
        "    scope: [a.py]\n"
        # `reason:` and `status:` are missing.
        "```\n"
    )
    lines = TM.find_queue_block(text, "faults")
    assert lines is not None
    with pytest.raises(TM.QueueParseError, match=r"missing required key"):
        TM.parse_faults(lines)
    queue = TM.parse_queue_block(text)
    assert queue["faults"] == []
    assert len(queue["errors"]) == 1
    assert "`faults:` block" in queue["errors"][0]


# --- parse_slices `accepts:` --------------------------------------------------


def test_parse_slices_accepts_defaults_to_empty_list() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["accepts"] == []


def test_parse_slices_accepts_flow_style() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    accepts: ["trio-check exits 0", "no QUEUE.md -> empty queues"]
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["accepts"] == [
        "trio-check exits 0",
        "no QUEUE.md -> empty queues",
    ]


def test_parse_slices_accepts_block_style() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    accepts:
      - trio-check exits 0
      - no QUEUE.md -> empty queues
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["accepts"] == [
        "trio-check exits 0",
        "no QUEUE.md -> empty queues",
    ]


def test_parse_slices_without_accepts_key_other_slices_unaffected() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
  - id: beta
    writes: [b.py]
    accepts: [thing]
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["accepts"] == []
    assert slices[1]["accepts"] == ["thing"]


# --- _parse_flow_list: quote-aware comma splitting ---------------------------
# accepts:/scope: hold prose/paths that routinely contain commas inside a
# quoted item; a naive `.split(",")` shreds them. Regression coverage for
# the fix, shared by writes:/reads:/accepts:/scope: (all four route through
# _parse_flow_list).


def test_accepts_quoted_comma_stays_one_item() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    accepts: ["trio-check exits 0, no QUEUE.md -> empty queues", "second one"]
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["accepts"] == [
        "trio-check exits 0, no QUEUE.md -> empty queues",
        "second one",
    ]


def test_scope_quoted_comma_stays_one_path_item() -> None:
    text = (
        "```yaml\n"
        "faults:\n"
        "  - id: f1\n"
        "    slice: alpha\n"
        "    observed_at: " + "a" * 40 + "\n"
        '    scope: [a.py, "dir with, comma/file.py"]\n'
        "    reason: r\n"
        "    status: open\n"
        "```\n"
    )
    lines = TM.find_queue_block(text, "faults")
    faults = TM.parse_faults(lines)
    assert faults[0]["scope"] == ["a.py", "dir with, comma/file.py"]


def test_unquoted_flow_lists_unchanged() -> None:
    """Regression: plain comma-separated flow lists parse exactly as before."""
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py, "api:Name"]
    reads: [b.py]
    accepts: [one, two, three]
```
"""
    slices = TM.parse_slices(TM.find_slices_block(plan))
    assert slices[0]["writes"] == ["a.py", "api:Name"]
    assert slices[0]["reads"] == ["b.py"]
    assert slices[0]["accepts"] == ["one", "two", "three"]


def test_unterminated_quote_in_accepts_raises_slice_parse_error() -> None:
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    accepts: ["unterminated]
```
"""
    with pytest.raises(TM.SliceParseError, match=r"unterminated"):
        TM.parse_slices(TM.find_slices_block(plan))


def test_unterminated_quote_in_scope_raises_queue_parse_error() -> None:
    text = (
        "```yaml\n"
        "faults:\n"
        "  - id: f1\n"
        "    slice: alpha\n"
        "    observed_at: " + "a" * 40 + "\n"
        '    scope: ["unterminated]\n'
        "    reason: r\n"
        "    status: open\n"
        "```\n"
    )
    lines = TM.find_queue_block(text, "faults")
    with pytest.raises(TM.QueueParseError, match=r"unterminated"):
        TM.parse_faults(lines)


# --- trio-check.py: QUEUE.md validation ---------------------------------------

SHA_A = "0123456789abcdef0123456789abcdef01234567"

PLAN_TWO_SLICES = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    status: complete
  - id: beta
    writes: [b.py]
    status: planned
```
"""


def _make_mailbox(
    tmp_path: Path,
    *,
    plan: str = PLAN_TWO_SLICES,
    verdict: str = "## slice alpha @" + SHA_A + " — SHIP\nLooks good.\n",
    queue: str | None = None,
    name: str = "mailbox",
) -> Path:
    """The smallest v1 mailbox that satisfies check_v1, optionally with QUEUE.md."""
    mailbox = tmp_path / name
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\nprofile: software\nFixture.\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 5\nstatus: iterating\n"
        "mission: Fixture.\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("Fixture.\n", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text(verdict, encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n- fixture\n", encoding="utf-8")
    if queue is not None:
        (mailbox / "QUEUE.md").write_text(queue, encoding="utf-8")
    return mailbox


def _run_check(mailbox: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(mailbox)],
        capture_output=True,
        text=True,
    )


VALID_QUEUE = (
    "```yaml\n"
    "retired:\n"
    "  - slice: alpha\n"
    f"    sha: {SHA_A}\n"
    "    at: 2026-08-26T10:00:00Z\n"
    "```\n"
    "\n"
    "```yaml\n"
    "faults:\n"
    "  - id: f1\n"
    "    slice: beta\n"
    f"    observed_at: {SHA_A}\n"
    "    scope: [b.py]\n"
    "    reason: needs more work\n"
    "    status: open\n"
    "```\n"
)


def test_valid_queue_mailbox_exits_0(tmp_path: Path) -> None:
    mailbox = _make_mailbox(tmp_path, queue=VALID_QUEUE)
    result = _run_check(mailbox)
    assert result.returncode == 0, result.stdout


def test_bad_fault_status_exits_1(tmp_path: Path) -> None:
    bad_queue = VALID_QUEUE.replace("status: open", "status: bogus")
    mailbox = _make_mailbox(tmp_path, queue=bad_queue, name="bad-status")
    result = _run_check(mailbox)
    assert result.returncode == 1, result.stdout
    assert "status" in result.stdout and "bogus" in result.stdout


def test_bad_retired_sha_exits_1(tmp_path: Path) -> None:
    bad_queue = VALID_QUEUE.replace(f"sha: {SHA_A}\n", "sha: not-a-sha\n")
    mailbox = _make_mailbox(tmp_path, queue=bad_queue, name="bad-sha")
    result = _run_check(mailbox)
    assert result.returncode == 1, result.stdout
    assert "invalid sha" in result.stdout


def test_retired_slice_not_in_plan_exits_1(tmp_path: Path) -> None:
    bad_queue = VALID_QUEUE.replace("slice: alpha", "slice: ghost")
    mailbox = _make_mailbox(tmp_path, queue=bad_queue, name="bad-slice-ref")
    result = _run_check(mailbox)
    assert result.returncode == 1, result.stdout
    assert "ghost" in result.stdout
    assert "not found in PLAN.md" in result.stdout


def test_fault_slice_not_in_plan_is_advisory_not_a_violation(tmp_path: Path) -> None:
    """A fault referencing an unknown slice is advisory (info), not an error
    — only the retired: -> PLAN.md reference is required."""
    advisory_queue = VALID_QUEUE.replace(
        "    slice: beta\n    observed_at:", "    slice: ghost\n    observed_at:"
    )
    mailbox = _make_mailbox(tmp_path, queue=advisory_queue, name="advisory-fault")
    result = _run_check(mailbox)
    assert result.returncode == 0, result.stdout
    result_json = subprocess.run(
        [sys.executable, str(CHECKER), str(mailbox), "--json"],
        capture_output=True,
        text=True,
    )
    assert result_json.returncode == 0
    import json

    doc = json.loads(result_json.stdout)
    info = doc["loops"][0]["info"]
    assert any("ghost" in line for line in info)


def test_open_loop_verdict_heading_valid_with_queue_exits_0(tmp_path: Path) -> None:
    """A VERDICT.md whose first non-empty line is a per-slice heading is
    valid when QUEUE.md is present."""
    mailbox = _make_mailbox(
        tmp_path,
        queue=VALID_QUEUE,
        verdict=f"## slice alpha @{SHA_A} — SHIP\nLooks good.\n",
        name="open-loop-verdict",
    )
    result = _run_check(mailbox)
    assert result.returncode == 0, result.stdout


def test_same_verdict_without_queue_still_fails(tmp_path: Path) -> None:
    """The identical VERDICT.md, but with no QUEUE.md in the mailbox, keeps
    failing the ordinary first-line contract (byte-for-byte unchanged
    behaviour without the open-loop extension)."""
    mailbox = _make_mailbox(
        tmp_path,
        queue=None,
        verdict=f"## slice alpha @{SHA_A} — SHIP\nLooks good.\n",
        name="lockstep-verdict",
    )
    result = _run_check(mailbox)
    assert result.returncode == 1, result.stdout
    assert "VERDICT.md first non-empty line must be" in result.stdout


def test_no_queue_md_is_a_noop(tmp_path: Path) -> None:
    """Absence of QUEUE.md never triggers a queue-related violation, and no
    `queue:` info line appears."""
    mailbox = _make_mailbox(
        tmp_path, queue=None, verdict="VERDICT: SHIP\nFixture.\n", name="no-queue"
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(mailbox), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    import json

    doc = json.loads(result.stdout)
    info = doc["loops"][0]["info"]
    assert not any(line.startswith("queue:") for line in info)


# --- r11g NEW-S1: a well-formed header after a `reason:`-last entry -----

# (reason line, next header) -- the r11g evaluator's variants: `reason:` at
# col 0/1 before a canonical `  - id:`, and a canonical col-4 `reason:`
# before a tab-indented or col-6 `- id:` (plus the cross products).
NEW_S1_VARIANTS = [
    ("reason: flush-left reason", "  - id: f1"),
    (" reason: col-1 reason", "  - id: f1"),
    ("    reason: canonical reason", "\t- id: f1"),
    ("    reason: canonical reason", "      - id: f1"),
    ("reason: flush-left reason", "\t- id: f1"),
    (" reason: col-1 reason", "      - id: f1"),
]


def new_s1_faults_block(reason_line: str, header_line: str) -> str:
    return (
        "```yaml\n"
        "faults:\n"
        "  - id: f0\n"
        "    slice: solo\n"
        "    observed_at: " + "a" * 40 + "\n"
        "    scope: design\n"
        "    status: done\n"
        f"{reason_line}\n"
        f"{header_line}\n"
        "    slice: solo\n"
        "    observed_at: " + "b" * 40 + "\n"
        "    scope: local:src/solo.py\n"
        "    reason: real bug\n"
        "    status: open\n"
        "```\n"
    )


@pytest.mark.parametrize("reason_line,header_line", NEW_S1_VARIANTS)
def test_new_s1_header_after_reason_last_entry_starts_next_entry(
    reason_line: str, header_line: str
) -> None:
    text = new_s1_faults_block(reason_line, header_line)
    queue = TM.parse_queue_block(text)
    assert queue["errors"] == []
    assert [(f["id"], f["status"]) for f in queue["faults"]] == [
        ("f0", "done"), ("f1", "open"),
    ]
    assert queue["faults"][0]["reason"] == reason_line.split(": ", 1)[1]
    # strict mode: no violation, same entries
    strict = TM.parse_faults(TM.find_queue_block(text, "faults"))
    assert [(f["id"], f["status"]) for f in strict] == [
        ("f0", "done"), ("f1", "open"),
    ]


def test_new_s1_top_level_key_is_never_folded_into_reason() -> None:
    """A deeper `faults:` line after `reason:` is the duplicate top-level
    key (reported), not reason text."""
    text = (
        "```yaml\nfaults:\n  - id: f1\n    slice: s\n    observed_at: abc\n"
        "    scope: design\n    status: open\n    reason: r\n"
        "      faults:\n```\n"
    )
    queue = TM.parse_queue_block(text)
    assert queue["faults"][0]["reason"] == "r"
    assert any("duplicate `faults:` key" in e for e in queue["errors"])


def test_key_and_garbled_id_shaped_reason_continuations_still_fold() -> None:
    """R1/R2 unchanged: only a WELL-FORMED header/top key escapes the fold."""
    for cont in ("status: done", "ID=5 and more", "Id: 7 x", "- id mismatch",
                 "* id thing"):
        text = (
            "```yaml\nfaults:\n  - id: f1\n    slice: s\n    observed_at: abc\n"
            f"    scope: design\n    reason: wrapped\n      {cont}\n"
            "    status: open\n```\n"
        )
        queue = TM.parse_queue_block(text)
        assert queue["errors"] == [], cont
        assert queue["faults"][0]["status"] == "open"
        assert queue["faults"][0]["reason"] == f"wrapped {cont}"


# --- r11g P1: fence-level violations are reported -----------------------

P1_ENTRY = (
    "faults:\n  - id: f1\n    slice: s\n    observed_at: abc\n"
    "    scope: design\n    reason: r\n    status: open\n"
)


def test_second_faults_fence_is_an_error() -> None:
    first = "faults:\n  - id: f0\n    slice: s\n    observed_at: abc\n" \
            "    scope: design\n    reason: r\n    status: done\n"
    text = f"```yaml\n{first}```\n\n```yaml\n{P1_ENTRY}```\n"
    queue = TM.parse_queue_block(text)
    assert [f["id"] for f in queue["faults"]] == ["f0"]
    assert len(queue["errors"]) == 1
    assert queue["errors"][0].startswith("`faults:` block: line 11:")
    assert "second fenced ```yaml `faults:` block" in queue["errors"][0]
    with pytest.raises(TM.QueueParseError, match="second fenced"):
        TM.find_queue_block(text, "faults")


def test_second_retired_fence_is_an_error() -> None:
    entry = "retired:\n  - slice: s1\n    sha: {}\n    at: t\n"
    text = (
        f"```yaml\n{entry.format('a' * 40)}```\n\n"
        f"```yaml\n{entry.format('b' * 40)}```\n"
    )
    queue = TM.parse_queue_block(text)
    assert [e["sha"] for e in queue["retired"]] == ["a" * 40]
    assert len(queue["errors"]) == 1
    assert queue["errors"][0].startswith("`retired:` block:")


@pytest.mark.parametrize("opener,closer", [
    ("```", "```"),
    ("~~~yaml", "~~~"),
    ("~~~", "~~~"),
    ("```yaml title", "```"),
    ("```YAML extra", "```"),
])
def test_faults_block_in_non_yaml_fence_is_an_error(opener, closer) -> None:
    text = f"{opener}\n{P1_ENTRY}{closer}\n"
    queue = TM.parse_queue_block(text)
    assert queue["faults"] == []
    assert len(queue["errors"]) == 1
    assert queue["errors"][0].startswith("`faults:` block: line 1:")
    assert "fence is ignored" in queue["errors"][0]
    with pytest.raises(TM.QueueParseError, match="fence is ignored"):
        TM.find_queue_block(text, "faults")


def test_single_yaml_fence_and_unrelated_fences_are_not_errors() -> None:
    text = (
        "Notes:\n\n```\nsome shell output\n```\n\n~~~\nmore\n~~~\n\n"
        f"```yml\n{P1_ENTRY}```\n"
    )
    queue = TM.parse_queue_block(text)
    assert queue["errors"] == []
    assert [f["id"] for f in queue["faults"]] == ["f1"]


def test_trio_check_reports_fence_level_violation(tmp_path) -> None:
    (tmp_path / "PLAN.md").write_text(
        "```yaml\nslices:\n  - id: s\n    writes: []\n    reads: []\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "QUEUE.md").write_text(f"```\n{P1_ENTRY}```\n", encoding="utf-8")
    checker = _load(CHECKER, "trio_check_p1")
    slices = TM.parse_slices(TM.find_slices_block(
        (tmp_path / "PLAN.md").read_text(encoding="utf-8")
    ))
    errors = checker.check_queue(tmp_path, TM, slices)
    assert any(
        e.startswith("QUEUE.md `faults:` block: line 1:") and "fence" in e
        for e in errors
    )


# --- r11h F-FENCE: CommonMark fence close + orphan entry headers -----------

FENCE_F0_DONE = (
    "  - id: f0\n    slice: s1\n    observed_at: abc\n    scope: design\n"
    "    status: done\n"
)
FENCE_F1_OPEN = (
    "  - id: f1\n    slice: s1\n    observed_at: abc\n"
    "    scope: local:a.py\n    reason: real bug\n    status: open\n"
)
EMPTY_RETIRED = "```yaml\nretired:\n```\n\n"


def _x1(inner_open: str, inner_close: str, snippet: str = "x = 1") -> str:
    """The r11h verbatim X1 shape: a code block quoted inside f0's
    `reason:` (col 6), then the open f1."""
    body = "".join(f"      {ln}\n" for ln in snippet.splitlines())
    return (
        EMPTY_RETIRED + "```yaml\nfaults:\n" + FENCE_F0_DONE
        + f"    reason: see this snippet\n      {inner_open}\n{body}"
        + (f"      {inner_close}\n" if inner_close is not None else "")
        + FENCE_F1_OPEN + "```\n"
    )


X1_VERBATIM = """\
```yaml
retired:
```

```yaml
faults:
  - id: f0
    slice: s1
    observed_at: abc
    scope: design
    status: done
    reason: see this snippet
      ```python
      x = 1
      ```
  - id: f1
    slice: s1
    observed_at: abc
    scope: local:a.py
    reason: real bug
    status: open
```
"""


def _live_ids(queue: dict) -> list[str]:
    return [f["id"] for f in queue["faults"]
            if f["status"] not in TM.QUEUE_CLOSED_STATUSES]


def _fault_errors(queue: dict) -> list[str]:
    return [e for e in queue["errors"] if e.startswith("`faults:` block:")]


def test_x1_verbatim_inner_code_block_does_not_close_the_fence() -> None:
    """r11h X1 verbatim: the col-6 ```python used to close the yaml fence
    and hide the open f1 (live=[], errors=[])."""
    assert X1_VERBATIM == _x1("```python", "```")
    queue = TM.parse_queue_block(X1_VERBATIM)
    assert queue["errors"] == []
    assert [(f["id"], f["status"]) for f in queue["faults"]] == [
        ("f0", "done"), ("f1", "open")]
    assert "```python" in queue["faults"][0]["reason"]
    strict = TM.parse_faults(TM.find_queue_block(X1_VERBATIM, "faults"))
    assert [f["id"] for f in strict] == ["f0", "f1"]


@pytest.mark.parametrize("inner_open,inner_close,snippet", [
    ("```python", "```", "x = 1"),
    ("```", "```", "x = 1"),
    ("~~~", "~~~", "x = 1"),
    ("~~~yaml", "~~~", "x = 1"),
    ("```text", None, "x = 1"),               # unterminated inner block
    ("```yaml", "```", "faults:\n  - id: f99"),  # quoted faults snippet
    ("```yaml", "```", "retired:\n  - slice: s1\n    sha: " + "b" * 40),
])
def test_x1_variants_keep_the_open_fault_or_report(
    inner_open, inner_close, snippet
) -> None:
    text = _x1(inner_open, inner_close, snippet)
    queue = TM.parse_queue_block(text)
    assert "f1" in _live_ids(queue) or _fault_errors(queue), text
    if "- id:" not in snippet:
        assert _live_ids(queue) == ["f1"]
        assert _fault_errors(queue) == []
        assert queue["retired"] == []  # the quoted retired entry never counts


@pytest.mark.parametrize("col", [0, 1, 2, 3])
def test_x2_stray_fence_line_at_cols_0_to_3_is_an_error(col: int) -> None:
    """A stray ``` at col 0-3 closes the fence; the entries after it are
    outside every fence -> reported (the gate holds)."""
    text = (EMPTY_RETIRED + "```yaml\nfaults:\n" + " " * col + "```\n"
            + FENCE_F1_OPEN + "```\n")
    queue = TM.parse_queue_block(text)
    assert queue["faults"] == []
    errs = _fault_errors(queue)
    assert errs and "`- id:` entry '- id: f1' is outside the `faults:` block" \
        in errs[0]
    assert "outside every fenced block" in errs[0]
    assert errs[0].startswith("`faults:` block: line 8:")
    with pytest.raises(TM.QueueParseError, match="outside the `faults:` block"):
        TM.find_queue_block(text, "faults")


@pytest.mark.parametrize("col", [4, 6, 8])
def test_x2_stray_fence_line_at_col_4_plus_is_content(col: int) -> None:
    """r11h X2 (seed 1): a col-8 ``` is fence content, not a close."""
    text = (EMPTY_RETIRED + "```yaml\nfaults:\n" + " " * col + "```\n"
            + FENCE_F1_OPEN + "```\n")
    queue = TM.parse_queue_block(text)
    assert _live_ids(queue) == ["f1"]


def test_x2_verbatim_seed1() -> None:
    text = (
        "```yaml\nretired:\n```\n\n```yaml\nfaults:\n        ```\n"
        "  - id: f1\n    slice: s1\n    observed_at: deadbeef\n"
        "    scope: [a.py]\n    reason: x: y z\n    status: taken\n"
        "    status: stale\n```\n"
    )
    queue = TM.parse_queue_block(text)
    assert _live_ids(queue) == ["f1"]


def test_x3_second_fence_without_key_is_an_error() -> None:
    text = (
        EMPTY_RETIRED + "```yaml\nfaults:\n" + FENCE_F0_DONE
        + "    reason: r\n```\n\n```yaml\n" + FENCE_F1_OPEN + "```\n"
    )
    queue = TM.parse_queue_block(text)
    assert [f["id"] for f in queue["faults"]] == ["f0"]
    errs = _fault_errors(queue)
    assert len(errs) == 1
    assert "'- id: f1' is outside the `faults:` block (in the '```yaml' " \
        "fence opened at line 15, which has no column-0 `faults:` key)" in errs[0]
    with pytest.raises(TM.QueueParseError, match="no column-0 `faults:` key"):
        TM.find_queue_block(text, "faults")


@pytest.mark.parametrize("top", [
    "fault:", "Faults:", "faults", "  faults:", "", "falts:",
])
def test_x4_typod_or_missing_top_key_is_an_error(top: str) -> None:
    body = (top + "\n" if top else "") + FENCE_F1_OPEN
    for opener, closer in (("```yaml", "```"), ("~~~", "~~~"), ("```", "```")):
        text = EMPTY_RETIRED + f"{opener}\n{body}{closer}\n"
        queue = TM.parse_queue_block(text)
        assert queue["faults"] == []
        errs = _fault_errors(queue)
        assert errs and "is outside the `faults:` block" in errs[0], text
        with pytest.raises(TM.QueueParseError):
            TM.find_queue_block(text, "faults")


def test_x4_entries_with_no_fence_at_all_are_an_error() -> None:
    queue = TM.parse_queue_block("faults:\n" + FENCE_F1_OPEN)
    assert _fault_errors(queue)


def test_fence_close_needs_a_long_enough_run_and_no_info_string() -> None:
    """A ````yaml opener is closed only by a run of >= 4 backticks; a
    ```sh line inside is content."""
    text = (
        EMPTY_RETIRED + "````yaml\nfaults:\n" + FENCE_F0_DONE
        + "    reason: r\n```\n```sh\n" + FENCE_F1_OPEN + "````\n"
    )
    queue = TM.parse_queue_block(text)
    assert "f1" in _live_ids(queue) or _fault_errors(queue)
    # a ~~~ never closes a ``` fence and vice versa
    text2 = (EMPTY_RETIRED + "```yaml\nfaults:\n~~~\n" + FENCE_F1_OPEN
             + "```\n")
    queue2 = TM.parse_queue_block(text2)
    assert "f1" in _live_ids(queue2) or _fault_errors(queue2)


def test_four_space_opener_is_not_a_fence() -> None:
    text = "    ```yaml\nfaults:\n" + FENCE_F1_OPEN + "    ```\n"
    queue = TM.parse_queue_block(text)
    assert _fault_errors(queue)


def test_unrelated_prose_fences_and_leading_space_opener_still_parse() -> None:
    text = (
        "Notes:\n\n```sh\necho hi\n```\n\n"
        "   ```yaml\nretired:\n```\n\n  ```yaml\nfaults:\n"
        + FENCE_F1_OPEN + "   ```\n\n~~~\nprose ``` inside\n~~~\n"
    )
    queue = TM.parse_queue_block(text)
    assert queue["errors"] == []
    assert _live_ids(queue) == ["f1"]


# --- r11h P4b: the top key matches only at column 0 -------------------------

def test_p4b_indented_retired_in_reason_does_not_select_the_faults_fence() -> None:
    """A faults fence BEFORE the retired fence whose reason quotes a whole
    retired entry used to be selected as the retired block (the quoted
    sha was trusted, the real retired fence ignored)."""
    text = (
        "```yaml\nfaults:\n" + FENCE_F0_DONE + "    reason: quoting\n"
        "      retired:\n      - slice: solo\n        sha: " + "b" * 40 + "\n"
        "        at: 2026-01-01T00:00:00Z\n```\n\n"
        "```yaml\nretired:\n  - slice: solo\n    sha: " + "a" * 40 + "\n"
        "    at: 2026-01-01T00:00:00Z\n```\n"
    )
    queue = TM.parse_queue_block(text)
    assert queue["retired"] == [
        {"slice": "solo", "sha": "a" * 40, "at": "2026-01-01T00:00:00Z"}]
    assert [f["id"] for f in queue["faults"]] == ["f0"]
    assert _fault_errors(queue) == []
    # the quoted `- slice:` inside the faults fence is reported (retired)
    assert any(e.startswith("`retired:` block:") and "- slice: solo" in e
               for e in queue["errors"])
    strict = TM.parse_retired(TM.find_queue_block(text.replace(
        "      - slice: solo", "      slice solo"), "retired"))
    assert [e["sha"] for e in strict] == ["a" * 40]


def test_p4a_reason_line_retired_does_not_steal_the_retired_block() -> None:
    text = (
        "```yaml\nfaults:\n" + FENCE_F0_DONE
        + "    reason: wrapped\n      retired: no entry yet\n```\n\n"
        "```yaml\nretired:\n  - slice: solo\n    sha: " + "a" * 40 + "\n"
        "    at: t\n```\n"
    )
    queue = TM.parse_queue_block(text)
    assert queue["errors"] == []
    assert [e["slice"] for e in queue["retired"]] == ["solo"]


# --- r11h API-4: trio-check refuses a mixed metrics/ set --------------------

@pytest.mark.parametrize("api_line", ["METRICS_API = 3\n", "METRICS_API = 2\n", ""])
def test_trio_check_refuses_mismatched_sibling_metrics(tmp_path, api_line) -> None:
    mdir = tmp_path / "metrics"
    mdir.mkdir()
    (mdir / "trio-check.py").write_text(CHECKER.read_text(encoding="utf-8"))
    (mdir / "trio-metrics.py").write_text(
        METRICS_PATH.read_text(encoding="utf-8").replace(
            "METRICS_API = 4\n", api_line)
    )
    mailbox = tmp_path / "proj" / "loop"
    mailbox.mkdir(parents=True)
    (mailbox / "STATE.md").write_text("schema: 1\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(mdir / "trio-check.py"), str(tmp_path / "proj")],
        capture_output=True, text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    found = api_line.split("=")[1].strip() if api_line else "1"
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr + proc.stdout
    assert (f"sibling trio-metrics.py has METRICS_API {found}, this "
            "trio-check requires 4 (mixed metrics/ versions)") in proc.stderr


def test_trio_check_requires_the_current_metrics_api() -> None:
    checker = _load(CHECKER, "trio_check_api4")
    assert checker.REQUIRED_METRICS_API == TM.METRICS_API == 4

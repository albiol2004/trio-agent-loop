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
    assert TM.read_queue(tmp_path) == {"retired": [], "faults": [], "errors": []}


def test_read_queue_empty_file_returns_empty_queues(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text("", encoding="utf-8")
    assert TM.read_queue(tmp_path) == {"retired": [], "faults": [], "errors": []}


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

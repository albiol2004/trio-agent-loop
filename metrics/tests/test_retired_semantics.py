"""Tests for the new `retired:` semantics (MAILBOX-SCHEMA.md "v1 open-loop
extension" — "retired at `sha`", repeat slice ids with distinct shas legal,
duplicate (slice, sha) pairs a violation, latest-entry-per-slice grading).

trio-check.py has a hyphenated filename, so it is loaded by path via
importlib, exactly like its sibling test modules (see
metrics/tests/test_open_loop_queue.py, which this file mirrors the style
of).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).parents[1] / "trio-check.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TC = _load(CHECKER, "trio_check")
TM = _load(Path(__file__).parents[1] / "trio-metrics.py", "trio_metrics_retired")

SHA_A = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"
SHA_C = "1111111111111111111111111111111111111111"

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


def _retired_queue(entries: list[tuple[str, str, str]]) -> str:
    """Build a QUEUE.md with only a `retired:` block from (slice, sha, at) rows."""
    lines = ["```yaml", "retired:"]
    for slice_id, sha, at in entries:
        lines.append(f"  - slice: {slice_id}")
        lines.append(f"    sha: {sha}")
        lines.append(f"    at: {at}")
    lines.append("```")
    return "\n".join(lines) + "\n"


# --- check_queue: repeated slice ids with distinct shas are legal -----------


def test_two_retired_entries_same_slice_distinct_shas_via_mailbox(tmp_path: Path) -> None:
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_B, "2026-08-26T12:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(tmp_path, queue=queue, name="repeat-distinct")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    assert errors == []


def test_two_retired_entries_same_pair_is_one_duplicate_error(tmp_path: Path) -> None:
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_A, "2026-08-26T12:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(tmp_path, queue=queue, name="repeat-same-pair")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    dup_errors = [e for e in errors if "duplicate" in e]
    assert len(dup_errors) == 1, errors
    assert "alpha" in dup_errors[0]
    assert SHA_A in dup_errors[0]


def test_three_entries_one_repeated_pair_is_one_error(tmp_path: Path) -> None:
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_B, "2026-08-26T11:00:00Z"),
            ("alpha", SHA_A, "2026-08-26T12:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(tmp_path, queue=queue, name="three-entries-one-repeat")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    dup_errors = [e for e in errors if "duplicate" in e]
    assert len(dup_errors) == 1, errors


def test_end_to_end_repeated_ids_distinct_shas_exits_0(tmp_path: Path) -> None:
    """A whole QUEUE.md with repeated slice ids (distinct shas) passes
    end-to-end through trio-check.py's CLI, exit code 0."""
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_B, "2026-08-26T12:00:00Z"),
            ("alpha", SHA_C, "2026-08-26T14:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(
        tmp_path,
        queue=queue,
        verdict=f"## slice alpha @{SHA_C} — SHIP\nLooks good.\n",
        name="end-to-end-repeat",
    )
    result = _run_check(mailbox)
    assert result.returncode == 0, result.stdout


def test_end_to_end_duplicate_pair_exits_1_and_mentions_duplicate(tmp_path: Path) -> None:
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_A, "2026-08-26T12:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(tmp_path, queue=queue, name="end-to-end-duplicate")
    result = _run_check(mailbox)
    assert result.returncode == 1, result.stdout
    assert "duplicate" in result.stdout
    assert SHA_A in result.stdout


# --- pre-existing validations still fire -------------------------------------


def test_bad_sha_shape_still_errors(tmp_path: Path) -> None:
    queue = _retired_queue([("alpha", "not-a-sha", "2026-08-26T10:00:00Z")])
    mailbox = _make_mailbox(tmp_path, queue=queue, name="bad-sha-shape")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    assert any("invalid sha" in e for e in errors), errors


def test_missing_at_still_errors(tmp_path: Path) -> None:
    queue_text = (
        "```yaml\n"
        "retired:\n"
        "  - slice: alpha\n"
        f"    sha: {SHA_A}\n"
        "```\n"
    )
    mailbox = _make_mailbox(tmp_path, queue=queue_text, name="missing-at")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    # parse_retired (strict, frozen) rejects a missing `at:` before
    # check_queue's own per-field validation ever sees the entry.
    assert any("missing required key" in e and "at" in e for e in errors), errors


def test_unknown_slice_id_still_errors(tmp_path: Path) -> None:
    queue = _retired_queue([("ghost", SHA_A, "2026-08-26T10:00:00Z")])
    mailbox = _make_mailbox(tmp_path, queue=queue, name="unknown-slice")
    errors = TC.check_queue(mailbox, TM, [{"id": "alpha"}, {"id": "beta"}])
    assert any(
        "ghost" in e and "not found in PLAN.md" in e for e in errors
    ), errors


# --- end-to-end via trio-check.py's public entry point (inspect_loop) -------


def test_inspect_loop_repeated_ids_end_to_end(tmp_path: Path) -> None:
    """A QUEUE.md with repeated slice ids (distinct shas) passes end-to-end
    through inspect_loop, trio-check.py's public per-mailbox entry point."""
    queue = _retired_queue(
        [
            ("alpha", SHA_A, "2026-08-26T10:00:00Z"),
            ("alpha", SHA_B, "2026-08-26T12:00:00Z"),
        ]
    )
    mailbox = _make_mailbox(
        tmp_path,
        queue=queue,
        verdict=f"## slice alpha @{SHA_B} — SHIP\nLooks good.\n",
        name="inspect-loop-repeat",
    )
    result = TC.inspect_loop(mailbox, TM)
    assert result["ok"] is True, result["errors"]
    assert result["errors"] == []

"""Tests for the dashboard-facing parsers in metrics/trio-metrics.py.

Covers the LOG.md timeline parsing helpers (extract_trailing_fields,
parse_verdict_scope, parse_timeline) and the lenient PLAN.md slices block
parser (parse_slices_block) added for dashboard/serve.py. trio-metrics.py
has a hyphenated filename, so it is loaded by path via importlib, exactly
like trio-check.py, trio-shadow.py, and dashboard/serve.py do.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

METRICS_PATH = Path(__file__).parents[1] / "trio-metrics.py"

spec = importlib.util.spec_from_file_location("trio_metrics", METRICS_PATH)
assert spec is not None and spec.loader is not None
TM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TM)


def test_extract_trailing_fields_with_timing_fields() -> None:
    summary, fields = TM.extract_trailing_fields(
        "Built dashboard server | started_at: 2026-08-11T17:29:28Z "
        "| ended_at: 2026-08-11T17:54:49Z | duration_sec: 1521"
    )
    assert summary == "Built dashboard server"
    assert fields == {
        "started_at": "2026-08-11T17:29:28Z",
        "ended_at": "2026-08-11T17:54:49Z",
        "duration_sec": "1521",
    }


def test_extract_trailing_fields_without_fields() -> None:
    summary, fields = TM.extract_trailing_fields(
        "Verified end-to-end via API and browser click-through"
    )
    assert summary == "Verified end-to-end via API and browser click-through"
    assert fields == {}


def test_extract_trailing_fields_leaves_pipe_prose_intact() -> None:
    """Prose containing | without a trailing `key: value` shape is untouched."""
    summary, fields = TM.extract_trailing_fields("smoke PASS (|slip|~0.29, speed 36)")
    assert summary == "smoke PASS (|slip|~0.29, speed 36)"
    assert fields == {}


def test_extract_trailing_fields_future_keys() -> None:
    """Any [A-Za-z_]+ key parses, not just the known timing keys."""
    summary, fields = TM.extract_trailing_fields("note | tokens_in: 1234 | tokens_out: 56")
    assert summary == "note"
    assert fields == {"tokens_in": "1234", "tokens_out": "56"}


def test_parse_verdict_scope_design() -> None:
    assert TM.parse_verdict_scope("VERDICT: ITERATE scope=design") == (
        "ITERATE",
        "design",
    )


def test_parse_verdict_scope_local_paths() -> None:
    assert TM.parse_verdict_scope("VERDICT: ITERATE scope=local:a.sh,b.py") == (
        "ITERATE",
        "local:a.sh,b.py",
    )


def test_parse_verdict_scope_plain() -> None:
    assert TM.parse_verdict_scope("VERDICT: SHIP — all criteria pass") == ("SHIP", None)


def test_parse_verdict_scope_absent() -> None:
    assert TM.parse_verdict_scope("no verdict mentioned here") == (None, None)


def test_parse_verdict_scope_case_insensitive() -> None:
    assert TM.parse_verdict_scope("verdict: blocked") == ("BLOCKED", None)


def test_parse_slices_block_defaults_applied() -> None:
    plan = """\
# Plan

```yaml
slices:
  - id: alpha
    writes: [a.py, "api:AlphaConfig"]
    reads: []
```

## Verification standard
implement-then-smoke
"""
    slices = TM.parse_slices_block(plan)
    assert slices is not None
    assert len(slices) == 1
    (sl,) = slices
    assert sl["id"] == "alpha"
    assert sl["writes"] == ["a.py", "api:AlphaConfig"]
    assert sl["reads"] == []
    assert sl["repo"] == "."  # omitted -> default
    assert sl["gate"] is False  # omitted -> default
    assert sl["status"] == "in_progress"  # omitted -> default
    assert sl["iteration"] is None  # omitted -> default


def test_parse_slices_block_full_shape() -> None:
    plan = """\
```yaml
slices:
  - id: beta
    repo: ../other
    writes: [b.py]
    reads: [a.py]
    gate: true
    status: complete
    iteration: 3
```
"""
    slices = TM.parse_slices_block(plan)
    assert slices == [{
        "id": "beta",
        "repo": "../other",
        "writes": ["b.py"],
        "reads": ["a.py"],
        "gate": True,
        "status": "complete",
        "iteration": 3,
    }]


def test_parse_slices_block_absent_returns_none() -> None:
    assert TM.parse_slices_block("# plan prose only, no yaml fence") is None
    assert TM.parse_slices_block("") is None
    # A yaml fence without a top-level `slices:` key is not a slices block.
    assert TM.parse_slices_block("```yaml\nwrites: [a.py]\n```\n") is None


def test_parse_slices_block_malformed_returns_none() -> None:
    """Unparseable blocks degrade to None (the dashboard contract), never raise."""
    assert TM.parse_slices_block("```yaml\nslices:\n  - id: alpha\n    bogus: 1\n```\n") is None


def test_parse_timeline_entries_have_seq_and_scope(tmp_path: Path) -> None:
    log = tmp_path / "LOG.md"
    log.write_text(
        "- iter 1 | lead | Built dashboard server\n"
        "- iter 1 | evaluator | VERDICT: ITERATE scope=local:src/a.js,src/b.py\n"
        "- iter 2 | lead | Implemented nested discovery | started_at: 2026-08-11T18:02:33Z | ended_at: 2026-08-11T18:23:02Z | duration_sec: 1229\n",
        encoding="utf-8",
    )
    entries = TM.parse_timeline(log)
    assert [e["seq"] for e in entries] == [0, 1, 2]
    assert entries[0] == {
        "seq": 0,
        "iteration": 1,
        "role": "lead",
        "summary": "Built dashboard server",
        "verdict": None,
        "scope": None,
        "slice": None,
        "started_at": None,
        "ended_at": None,
        "duration_sec": None,
    }
    assert entries[1]["verdict"] == "ITERATE"
    assert entries[1]["scope"] == "local:src/a.js,src/b.py"
    assert entries[2]["summary"] == "Implemented nested discovery"
    assert entries[2]["started_at"] == "2026-08-11T18:02:33Z"
    assert entries[2]["ended_at"] == "2026-08-11T18:23:02Z"
    assert entries[2]["duration_sec"] == 1229


def test_parse_timeline_missing_file_is_empty(tmp_path: Path) -> None:
    assert TM.parse_timeline(tmp_path / "nope.md") == []


def test_parse_timeline_slice_lines_become_builder_entries(tmp_path: Path) -> None:
    log = tmp_path / "LOG.md"
    log.write_text(
        "# Trio loop log\n"
        "- iter 9 | lead | Delegating two isolated builders (MODEL / PHYSICS)\n"
        "- slice(DriftLead3.DriftRepair3): iter3 P1/P2 landed in index.html\n"
        "iter9-car-physics: stepCar weight transfer + per-axle grip\n",
        encoding="utf-8",
    )
    entries = TM.parse_timeline(log)
    assert [e["role"] for e in entries] == ["lead", "builder", "builder"]
    assert entries[1]["slice"] == "DriftLead3.DriftRepair3"
    assert entries[1]["iteration"] == 3  # from "iter3" in the body
    assert entries[2]["slice"] == "iter9-car-physics"
    assert entries[2]["iteration"] == 9  # from "iter9" in the slice id
    assert entries[2]["summary"] == "stepCar weight transfer + per-axle grip"

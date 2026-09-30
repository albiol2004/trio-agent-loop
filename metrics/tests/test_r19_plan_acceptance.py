"""r19 C2: PLAN.md `covers:`, `lead_integration:` ACC ids,
`acceptance_bindings:`; METRICS_API 7."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

METRICS = Path(__file__).resolve().parents[1] / "trio-metrics.py"


def _load():
    spec = importlib.util.spec_from_file_location("trio_metrics_r19", METRICS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TM = _load()

PLAN = """# PLAN

```yaml
slices:
  - id: or-api
    writes: [api/src/stats.ts]
    covers: [ACC-01, ACC-03]
    accepts: ["GET stats -> 200 | oracle: value"]
  - id: or-view
    writes: [web/page.tsx]
    covers:
      - ACC-02
      - ACC-03
  - id: docs
    writes: [README.md]
```

## Verification standard
mode: test-first
full_check: npm test
lead_integration: [evidence/smoke.md, ACC-04]   # smoke + one check
acceptance_bindings: {STATS_ROUTE: /api/openrouter/stats, "X": y}

```text
lead_integration: [ACC-99]
```
"""


def test_covers_parses_flow_and_block_lists_and_is_absent_when_undeclared():
    slices = TM.parse_slices(TM.find_slices_block(PLAN))
    by = {s["id"]: s for s in slices}
    assert by["or-api"]["covers"] == ["ACC-01", "ACC-03"]
    assert by["or-view"]["covers"] == ["ACC-02", "ACC-03"]
    assert "covers" not in by["docs"]  # pre-r19 dict shape, byte for byte
    assert TM.plan_covers(slices) == {
        "ACC-01": ["or-api"], "ACC-03": ["or-api", "or-view"], "ACC-02": ["or-view"]}


@pytest.mark.parametrize("value", ["[acc-1]", "[ACC-]", "[ACC-01, foo]", "[ACC-12345]"])
def test_covers_rejects_non_ids(value):
    block = f"```yaml\nslices:\n  - id: a\n    writes: [x]\n    covers: {value}\n```\n"
    with pytest.raises(TM.SliceParseError, match="covers"):
        TM.parse_slices(TM.find_slices_block(block))
    assert TM.parse_slices_block(block) is None


def test_block_covers_rejects_non_ids():
    block = "```yaml\nslices:\n  - id: a\n    covers:\n      - nope\n```\n"
    with pytest.raises(TM.SliceParseError, match="covers"):
        TM.parse_slices(TM.find_slices_block(block))


def test_parse_plan_acceptance_lead_integration_and_bindings():
    got = TM.parse_plan_acceptance(PLAN)
    # Deliverables stay deliverables; a fenced example is ignored.
    assert got["lead_integration"] == ["ACC-04"]
    assert got["bindings"] == {"STATS_ROUTE": "/api/openrouter/stats"}
    assert any("'\"X\": y'" in e or "X" in e for e in got["errors"])


def test_parse_plan_acceptance_block_forms_and_errors():
    plan = ("lead_integration:\n  - ACC-07\n  - evidence/x.md\n  - acc-8\n"
            "acceptance_bindings:\n  ROUTE: /a\n  lower: /b\n"
            "acceptance_bindings: ROUTE=/c\n")
    got = TM.parse_plan_acceptance(plan)
    assert got["lead_integration"] == ["ACC-07"]
    assert got["bindings"] == {"ROUTE": "/a"}
    errs = " ".join(got["errors"])
    assert "acc-8" in errs and "'lower: /b'" in errs and "ROUTE=/c" not in got["bindings"]
    assert len(got["errors"]) == 3


def test_parse_plan_acceptance_empty_and_never_raises():
    assert TM.parse_plan_acceptance("") == {"lead_integration": [], "bindings": {}, "errors": []}
    got = TM.parse_plan_acceptance('lead_integration: [ACC-1, "unterminated\n'
                                   "acceptance_bindings: {A: b\n")
    assert len(got["errors"]) == 2


def test_metrics_api_is_7():
    assert TM.METRICS_API == 7
    assert "covers" in TM.SLICE_KEYS

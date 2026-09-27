"""Fault `scope:` shapes and lenient QUEUE.md parse-error reporting.

Evaluator prompts tell evaluators to write a fault's scope as a plain value
(`scope: <the failing paths>`, `scope=design`), but parse_faults used to
accept only a list, and parse_queue_block swallowed the resulting error as
`faults: []` -- every live fault was invisible to the driver's gates
(openrouter/L run). See MAILBOX-SCHEMA.md "Fault `scope` shapes".
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


TM = _load(METRICS_PATH, "trio_metrics_fault_scope")
TC = _load(CHECKER, "trio_check_fault_scope")

SHA = "f4527ebaf960aa01844d5be718978572c9421171"


def _fault(fid: str, scope_line: str, status: str = "open") -> str:
    return (
        f"  - id: {fid}\n"
        "    slice: alpha\n"
        f"    observed_at: {SHA}\n"
        f"{scope_line}"
        "    reason: broke it\n"
        f"    status: {status}\n"
    )


def _faults_block(*entries: str) -> str:
    return "```yaml\nfaults:\n" + "".join(entries) + "```\n"


def _parse(*entries: str) -> dict:
    return TM.parse_queue_block(_faults_block(*entries))


# --- accepted shapes ----------------------------------------------------------


def test_plain_local_scope_single_path() -> None:
    q = _parse(_fault("f1", "    scope: local:api/test/x.test.ts\n"))
    assert q["errors"] == []
    assert q["faults"][0]["scope"] == ["api/test/x.test.ts"]


def test_plain_local_scope_comma_separated_paths() -> None:
    q = _parse(_fault("f1", "    scope: local:src/a.py, src/b.py,lib/c.py\n"))
    assert q["errors"] == []
    assert q["faults"][0]["scope"] == ["src/a.py", "src/b.py", "lib/c.py"]


def test_plain_design_scope() -> None:
    q = _parse(_fault("f1", "    scope: design\n"))
    assert q["errors"] == []
    assert q["faults"][0]["scope"] == ["design"]


def test_plain_bare_paths_scope() -> None:
    q = _parse(_fault("f1", "    scope: src/a.py, src/b.py\n"))
    assert q["faults"][0]["scope"] == ["src/a.py", "src/b.py"]


def test_flow_list_scope_unchanged() -> None:
    q = _parse(_fault("f1", '    scope: [a.py, "lib/util.py"]\n'))
    assert q["errors"] == []
    assert q["faults"][0]["scope"] == ["a.py", "lib/util.py"]


def test_block_list_scope_unchanged() -> None:
    q = _parse(_fault("f1", "    scope:\n      - a.py\n      - local:b.py\n"))
    assert q["errors"] == []
    assert q["faults"][0]["scope"] == ["a.py", "b.py"]


def test_list_items_with_local_prefix_are_normalized() -> None:
    q = _parse(_fault("f1", "    scope: [local:a.py, b.py]\n"))
    assert q["faults"][0]["scope"] == ["a.py", "b.py"]


def test_mixed_shapes_in_one_block() -> None:
    q = _parse(
        _fault("f1", "    scope: local:api/x.ts\n"),
        _fault("f2", "    scope: design\n", status="taken"),
        _fault("f3", "    scope: [a.py, b.py]\n", status="done"),
        _fault("f4", "    scope:\n      - c.py\n", status="stale"),
    )
    assert q["errors"] == []
    assert [(f["id"], f["scope"], f["status"]) for f in q["faults"]] == [
        ("f1", ["api/x.ts"], "open"),
        ("f2", ["design"], "taken"),
        ("f3", ["a.py", "b.py"], "done"),
        ("f4", ["c.py"], "stale"),
    ]


def test_strict_parse_faults_accepts_plain_scope() -> None:
    lines = TM.find_queue_block(
        _faults_block(_fault("f1", "    scope: design\n")), "faults"
    )
    assert TM.parse_faults(lines)[0]["scope"] == ["design"]


# --- malformed entries: valid ones survive, error surfaced -------------------


def test_malformed_entry_among_valid_ones() -> None:
    q = _parse(
        _fault("f1", "    scope: local:a.py\n"),
        # f2: unterminated quote in its scope list.
        _fault("f2", '    scope: ["unterminated]\n'),
        # f3: missing reason/status.
        "  - id: f3\n    slice: alpha\n    observed_at: " + SHA + "\n"
        "    scope: design\n",
        _fault("f4", "    scope: design\n"),
        # f5: junk line inside the entry.
        _fault("f5", "    scope: b.py\n    this is not yaml\n"),
        _fault("f6", "    scope: [c.py]\n", status="done"),
    )
    assert [f["id"] for f in q["faults"]] == ["f1", "f4", "f6"]
    assert len(q["errors"]) == 3
    assert all(e.startswith("`faults:` block: line ") for e in q["errors"])
    assert any("unterminated" in e for e in q["errors"])
    assert any("missing required key(s): reason, status" in e for e in q["errors"])
    assert any("unexpected content" in e for e in q["errors"])


def test_empty_plain_local_scope_is_an_error() -> None:
    q = _parse(_fault("f1", "    scope: local:\n"), _fault("f2", "    scope: x.py\n"))
    assert [f["id"] for f in q["faults"]] == ["f2"]
    assert len(q["errors"]) == 1 and "names no paths" in q["errors"][0]


def test_strict_parse_still_raises_on_malformed_entry() -> None:
    lines = TM.find_queue_block(
        _faults_block(_fault("f1", '    scope: ["unterminated]\n')), "faults"
    )
    with pytest.raises(TM.QueueParseError, match="unterminated"):
        TM.parse_faults(lines)


def test_malformed_retired_entry_keeps_valid_ones_and_reports() -> None:
    text = (
        "```yaml\nretired:\n"
        f"  - slice: alpha\n    sha: {SHA}\n    at: t\n"
        f"  - slice: beta\n    sha: {SHA}\n"
        f"  - slice: gamma\n    sha: {SHA}\n    at: t\n"
        "```\n"
    )
    q = TM.parse_queue_block(text)
    assert [e["slice"] for e in q["retired"]] == ["alpha", "gamma"]
    assert len(q["errors"]) == 1 and q["errors"][0].startswith("`retired:` block:")


def test_read_queue_reports_errors(tmp_path: Path) -> None:
    (tmp_path / "QUEUE.md").write_text(
        _faults_block(_fault("f1", '    scope: ["x]\n'), _fault("f2", "    scope: design\n")),
        encoding="utf-8",
    )
    q = TM.read_queue(tmp_path)
    assert [f["id"] for f in q["faults"]] == ["f2"]
    assert len(q["errors"]) == 1


# --- the real openrouter/L QUEUE.md -------------------------------------------

L_QUEUE = """\
```yaml
retired:
  - slice: or-docs
    sha: 134af14ce05e6a9db31557643dca9eb5fd0fad9b
    at: 2026-09-27T02:11:00Z
  - slice: or-view
    sha: b160c564a90b82df0d55d69d2258e7c1db800b20
    at: 2026-09-27T02:12:10Z
  - slice: or-map
    sha: 323e7a4106bc236c370bf374b1c64f543653017a
    at: 2026-09-27T02:14:20Z
  - slice: or-api
    sha: f4527ebaf960aa01844d5be718978572c9421171
    at: 2026-09-27T02:15:40Z
```

```yaml
faults:
  - id: f1
    slice: or-api
    observed_at: f4527ebaf960aa01844d5be718978572c9421171
    scope: local:api/test/openrouter-route.test.ts
    reason: Slice vitest is not green at the retired merge sha. test/openrouter-route.test.ts expects JSON 503 on GET /openrouter when injectors are omitted, but f4527eb already includes api/src/openrouter-view.ts so startBare() returns HTML 200 with an openrouter-unavailable banner (C3 HTML path). Fix the assertion to the merged-tree contract; do not change JSON 503 for /api/openrouter/* without a management key.
    status: open
  - id: f2
    slice: integration
    observed_at: f4527ebaf960aa01844d5be718978572c9421171
    scope: design
    reason: Integration eval of pin f4527eb. `cd api && npm test` 384 passed / 1 failed (same openrouter-route.test.ts 503-vs-200 as f1). `cd api && npm run typecheck` exit 2 — openrouter-analytics.ts:286-287 ActivityRow[] not assignable to Record<string, unknown>[]; openrouter-view.test.ts:183 stats undefined vs exactOptionalPropertyTypes. PLAN also requires fixture HTTP smoke saved under loop-hard/evidence/ (directory absent). No live OpenRouter probe (fixture).
    status: open
```
"""


def test_real_openrouter_l_queue_parses_both_faults() -> None:
    q = TM.parse_queue_block(L_QUEUE)
    assert q["errors"] == []
    assert len(q["retired"]) == 4
    assert [(f["id"], f["slice"], f["scope"], f["status"]) for f in q["faults"]] == [
        ("f1", "or-api", ["api/test/openrouter-route.test.ts"], "open"),
        ("f2", "integration", ["design"], "open"),
    ]


# --- trio-check.py validation ---------------------------------------------------

PLAN = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
```
"""


def _mailbox(tmp_path: Path, faults_text: str) -> Path:
    (tmp_path / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (tmp_path / "QUEUE.md").write_text(faults_text, encoding="utf-8")
    return tmp_path


def test_check_queue_accepts_plain_and_list_scopes(tmp_path: Path) -> None:
    mb = _mailbox(tmp_path, _faults_block(
        _fault("f1", "    scope: local:a.py,b.py\n"),
        _fault("f2", "    scope: design\n"),
        _fault("f3", "    scope: [c.py]\n"),
    ))
    assert TC.check_queue(mb, TM, [{"id": "alpha"}]) == []


def test_check_queue_reports_every_malformed_entry_and_validates_rest(
    tmp_path: Path,
) -> None:
    mb = _mailbox(tmp_path, _faults_block(
        _fault("f1", '    scope: ["x]\n'),
        _fault("f2", "    scope: local:\n"),
        _fault("f3", "    scope: design\n", status="bogus"),
    ))
    errors = TC.check_queue(mb, TM, [{"id": "alpha"}])
    assert sum("`faults:` block" in e for e in errors) == 2
    # f3 parsed and was still validated.
    assert any("'f3' has status 'bogus'" in e for e in errors)


def test_check_queue_rejects_bad_scope_shapes(tmp_path: Path) -> None:
    mb = _mailbox(tmp_path, _faults_block(
        _fault("f1", "    scope: [design, a.py]\n"),
        _fault("f2", "    scope: scope=local:a.py\n"),
        _fault("f3", "    scope: []\n"),
    ))
    errors = TC.check_queue(mb, TM, [{"id": "alpha"}])
    assert any("'f1' `scope:` mixes `design` with paths" in e for e in errors)
    assert any("'f2' `scope:` item" in e and "scope=" in e for e in errors)
    assert any("'f3' has an empty `scope:`" in e for e in errors)


def _v1_mailbox(tmp_path: Path, queue: str) -> Path:
    """Smallest v1 mailbox trio-check's CLI recognizes (see
    test_open_loop_queue._make_mailbox), with the given QUEUE.md."""
    mb = tmp_path / "mailbox"
    mb.mkdir()
    (mb / "GOAL.md").write_text("# Goal\nprofile: software\nFixture.\n", encoding="utf-8")
    (mb / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 5\nstatus: iterating\n"
        "mission: Fixture.\n",
        encoding="utf-8",
    )
    (mb / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (mb / "REPORT.md").write_text("Fixture.\n", encoding="utf-8")
    (mb / "VERDICT.md").write_text("## slice alpha @" + SHA + " — SHIP\nok\n", encoding="utf-8")
    (mb / "LOG.md").write_text("# Trio loop log\n- fixture\n", encoding="utf-8")
    (mb / "QUEUE.md").write_text(queue, encoding="utf-8")
    return mb


def test_trio_check_cli_passes_plain_scopes(tmp_path: Path) -> None:
    mb = _v1_mailbox(tmp_path, _faults_block(
        _fault("f1", "    scope: local:a.py\n"), _fault("f2", "    scope: design\n"),
    ))
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(mb)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_trio_check_cli_fails_on_malformed_fault(tmp_path: Path) -> None:
    mb = _v1_mailbox(tmp_path, _faults_block(
        _fault("f1", '    scope: ["x]\n'), _fault("f2", "    scope: design\n"),
    ))
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(mb)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "`faults:` block" in (result.stdout + result.stderr)

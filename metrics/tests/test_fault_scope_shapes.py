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


# --- r11 queue-harden F2: a complete entry survives a stray line --------------


def test_wrapped_reason_continuation_is_folded_not_an_error() -> None:
    entry = (
        "  - id: f1\n"
        "    slice: alpha\n"
        f"    observed_at: {SHA}\n"
        "    scope: local:a.py\n"
        "    reason: the route test is red on the merged tree because\n"
        "      the banner path returns HTML 200\n"
        "    status: open\n"
    )
    q = _parse(entry, _fault("f2", "    scope: design\n"))
    assert q["errors"] == []
    assert [f["id"] for f in q["faults"]] == ["f1", "f2"]
    assert q["faults"][0]["reason"] == (
        "the route test is red on the merged tree because "
        "the banner path returns HTML 200"
    )
    assert q["faults"][0]["status"] == "open"
    # Strict mode agrees: folding is valid YAML, not a violation.
    lines = TM.find_queue_block(_faults_block(entry), "faults")
    assert TM.parse_faults(lines)[0]["reason"].endswith("returns HTML 200")


def test_wrapped_continuation_after_last_key_is_folded() -> None:
    entry = (
        "  - id: f1\n    slice: alpha\n"
        f"    observed_at: {SHA}\n    scope: a.py\n    status: open\n"
        "    reason: long\n      tail\n"
    )
    q = _parse(entry)
    assert q["errors"] == []
    assert q["faults"][0]["reason"] == "long tail"


def test_prose_line_after_complete_entry_keeps_entry_one_error() -> None:
    q = _parse(
        _fault("f1", "    scope: local:a.py\n")
        + "  this fault is about the route test and nothing else\n",
        _fault("f2", "    scope: design\n"),
    )
    assert [f["id"] for f in q["faults"]] == ["f1", "f2"]
    assert len(q["errors"]) == 1
    err = q["errors"][0]
    assert err.startswith("`faults:` block: line 8: unexpected content: ")
    assert "'this fault is about the route test and nothing else'" in err
    assert "`- id: f1` entry at line 2 is kept" in err


def test_long_junk_line_is_reported_by_prefix() -> None:
    junk = "x" * 200
    q = _parse(_fault("f1", "    scope: a.py\n") + f"    {junk}\n")
    assert [f["id"] for f in q["faults"]] == ["f1"]
    assert len(q["errors"]) == 1
    assert ("x" * 60 + "…") in q["errors"][0] and junk not in q["errors"][0]


def test_stray_id_line_after_complete_entry_keeps_entry() -> None:
    q = _parse(
        _fault("f1", "    scope: a.py\n") + "    id: f7\n",
        _fault("f2", "    scope: b.py\n"),
    )
    assert [f["id"] for f in q["faults"]] == ["f1", "f2"]
    assert len(q["errors"]) == 1 and "line 8: stray `id: f7` line" in q["errors"][0]


def test_garbled_next_header_keeps_previous_entry() -> None:
    # `-id: f2` is not a valid header; its key lines must not overwrite f1.
    garbled = _fault("f2", "    scope: b.py\n").replace("  - id: f2", "  -id: f2")
    q = _parse(_fault("f1", "    scope: a.py\n"), garbled, _fault("f3", "    scope: c.py\n"))
    assert [(f["id"], f["scope"]) for f in q["faults"]] == [
        ("f1", ["a.py"]), ("f3", ["c.py"]),
    ]
    assert len(q["errors"]) == 1 and "'-id: f2'" in q["errors"][0]


def test_junk_inside_incomplete_entry_still_drops_it() -> None:
    q = _parse(
        "  - id: f1\n    slice: alpha\n    not yaml at all\n"
        f"    observed_at: {SHA}\n    scope: a.py\n    reason: r\n    status: open\n",
        _fault("f2", "    scope: b.py\n"),
    )
    assert [f["id"] for f in q["faults"]] == ["f2"]
    assert len(q["errors"]) == 1


def test_strict_mode_still_raises_on_stray_line_after_complete_entry() -> None:
    lines = TM.find_queue_block(
        _faults_block(_fault("f1", "    scope: a.py\n") + "  prose\n"), "faults"
    )
    with pytest.raises(TM.QueueParseError, match="line 8: unexpected content"):
        TM.parse_faults(lines)


# --- r11 queue-harden F1: malformed retired entries poison their slice --------

SHA2 = "0" * 39 + "2"


def _retired(*entries: str) -> str:
    return "```yaml\nretired:\n" + "".join(entries) + "```\n"


def test_newest_malformed_retired_entry_poisons_its_slice() -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n    sha: {SHA}\n    at: t1\n",
        f"  - slice: beta\n    sha: {SHA}\n    at: t1\n",
        f"  - slice: alpha\n    sha: {SHA2}\n",  # re-retire, missing `at:`
    ))
    assert [(e["slice"], e["sha"]) for e in q["retired"]] == [
        ("alpha", SHA), ("beta", SHA),
    ]
    assert q["malformed_slices"] == ["alpha"]
    assert len(q["errors"]) == 1


def test_stray_slice_line_and_garbled_header_poison_named_slice() -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n    sha: {SHA}\n    at: t1\n",
        "    slice: beta\n",
        f"  - slice: gamma\n    sha: {SHA}\n    at: t1\n",
        f"  -slice: delta\n    sha: {SHA2}\n    at: t2\n",
    ))
    # alpha and gamma are complete and kept; delta's lines never
    # overwrite gamma's sha.
    assert [(e["slice"], e["sha"]) for e in q["retired"]] == [
        ("alpha", SHA), ("gamma", SHA),
    ]
    assert q["malformed_slices"] == ["beta", "delta"]
    assert len(q["errors"]) == 2


def test_valid_retired_block_has_no_malformed_slices() -> None:
    q = TM.parse_queue_block(L_QUEUE)
    assert q["malformed_slices"] == []


def test_plain_scope_unterminated_quote_names_plain_value() -> None:
    q = _parse(_fault("f1", '    scope: "a.py, b.py\n'))
    assert q["faults"] == []
    assert "quote in plain `scope:` value" in q["errors"][0]
    assert "bracketed list" not in q["errors"][0]
    q = _parse(_fault("f1", '    scope: ["a.py, b.py]\n'))
    assert "quote in bracketed list" in q["errors"][0]


# --- r11 queue-harden F3: METRICS_API 3 ---------------------------------------

TRIOCTL = Path(__file__).parents[2] / "omnigent" / "trioctl"


def _load_trioctl():
    import importlib.machinery

    loader = importlib.machinery.SourceFileLoader("trioctl_queue_harden", str(TRIOCTL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_metrics_api_is_3_and_matches_trioctl() -> None:
    assert TM.METRICS_API == 3
    trioctl = _load_trioctl()
    assert trioctl.REQUIRED_METRICS_API == TM.METRICS_API
    assert trioctl._api_marker(METRICS_PATH, "METRICS_API") == 3


def test_too_old_vendored_metrics_is_refused_with_clear_message(
    tmp_path: Path,
) -> None:
    trioctl = _load_trioctl()
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    core_src = Path(__file__).parents[1] / "trio_loop.py"
    (metrics_dir / "trio_loop.py").write_text(core_src.read_text())
    (metrics_dir / "trio-metrics.py").write_text(
        METRICS_PATH.read_text().replace("METRICS_API = 3\n", "METRICS_API = 2\n")
    )
    with pytest.raises(
        trioctl.TrioctlError,
        match=r"METRICS_API 2, this trioctl requires 3 \(mixed metrics/ versions\)",
    ):
        trioctl._check_loop_core_api(metrics_dir / "trio_loop.py")


# --- r11 fold-fix N1: only `reason:` folds; other continuations are errors ----


def _strict_faults(text: str):
    return TM.parse_faults(TM.find_queue_block(text, "faults"))


def test_status_with_trailing_note_stays_open_one_error() -> None:
    entry = _fault("f1", "    scope: a.py\n").replace(
        "    status: open\n", "    status: open\n      (see f0)\n"
    )
    q = _parse(entry, _fault("f2", "    scope: b.py\n"))
    assert [(f["id"], f["status"]) for f in q["faults"]] == [
        ("f1", "open"), ("f2", "open"),
    ]
    assert len(q["errors"]) == 1
    err = q["errors"][0]
    assert "line 8:" in err and "after `status:`" in err and "'(see f0)'" in err
    with pytest.raises(TM.QueueParseError, match="line 8: unexpected continuation"):
        _strict_faults(_faults_block(entry))


def test_status_note_mid_entry_keeps_entry_and_parses_rest() -> None:
    # The note sits before later keys: they still parse, the entry is kept.
    entry = (
        "  - id: f1\n    status: open\n      see f0 for context\n"
        f"    slice: alpha\n    observed_at: {SHA}\n    scope: a.py\n"
        "    reason: r\n"
    )
    q = _parse(entry)
    assert [(f["id"], f["status"], f["reason"]) for f in q["faults"]] == [
        ("f1", "open", "r"),
    ]
    assert len(q["errors"]) == 1 and "line 4:" in q["errors"][0]


@pytest.mark.parametrize("key", ["slice", "observed_at"])
def test_fault_key_with_junk_continuation_is_unchanged(key: str) -> None:
    entry = _fault("f1", "    scope: a.py\n")
    line = next(ln for ln in entry.splitlines(keepends=True)
                if ln.startswith(f"    {key}:"))
    q = _parse(entry.replace(line, line + "        junk here\n"))
    expected = {"slice": "alpha", "observed_at": SHA}[key]
    assert q["faults"][0][key] == expected
    assert len(q["errors"]) == 1 and f"after `{key}:`" in q["errors"][0]


def test_retired_sha_with_junk_continuation_is_unchanged() -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n    sha: {SHA}\n      junk\n    at: t1\n",
    ))
    assert [(e["slice"], e["sha"], e["at"]) for e in q["retired"]] == [
        ("alpha", SHA, "t1"),
    ]
    assert len(q["errors"]) == 1
    assert "line 4:" in q["errors"][0] and "after `sha:`" in q["errors"][0]
    assert q["malformed_slices"] == []
    with pytest.raises(TM.QueueParseError, match="after `sha:`"):
        TM.parse_retired(TM.find_queue_block(_retired(
            f"  - slice: alpha\n    sha: {SHA}\n      junk\n    at: t1\n",
        ), "retired"))


def test_retired_slice_and_at_with_junk_continuation_are_unchanged() -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n      junk\n    sha: {SHA}\n    at: t1\n        x\n",
    ))
    assert [(e["slice"], e["sha"], e["at"]) for e in q["retired"]] == [
        ("alpha", SHA, "t1"),
    ]
    assert len(q["errors"]) == 2
    assert "line 3:" in q["errors"][0] and "after `slice:`" in q["errors"][0]
    assert "line 6:" in q["errors"][1] and "after `at:`" in q["errors"][1]
    assert q["malformed_slices"] == []
    # At the key column (not deeper) it is plain unexpected content: the
    # incomplete entry is dropped and its slice poisoned, as before.
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n    junk\n    sha: {SHA}\n    at: t1\n",
    ))
    assert q["retired"] == [] and q["malformed_slices"] == ["alpha"]


def test_reason_fold_still_works_lenient_and_strict() -> None:
    entry = _fault("f1", "    scope: a.py\n").replace(
        "    reason: broke it\n", "    reason: broke it\n      badly\n        twice\n"
    )
    q = _parse(entry)
    assert q["errors"] == []
    assert q["faults"][0]["reason"] == "broke it badly twice"
    assert _strict_faults(_faults_block(entry))[0]["reason"] == "broke it badly twice"


# --- r11 fold-fix N4: a duplicate key inside one entry ------------------------


def test_duplicate_status_keeps_first_value_one_error() -> None:
    entry = _fault("f1", "    scope: a.py\n") + "    status: done\n"
    q = _parse(entry, _fault("f2", "    scope: b.py\n"))
    assert [(f["id"], f["status"]) for f in q["faults"]] == [
        ("f1", "open"), ("f2", "open"),
    ]
    assert len(q["errors"]) == 1
    err = q["errors"][0]
    assert "line 8: duplicate `status:` key" in err and "first value is kept" in err
    with pytest.raises(TM.QueueParseError, match="duplicate `status:` key"):
        _strict_faults(_faults_block(entry))


def test_duplicate_slice_and_block_scope_keep_first() -> None:
    entry = (
        "  - id: f1\n    slice: alpha\n    slice: other\n"
        f"    observed_at: {SHA}\n    scope:\n      - a.py\n"
        "    scope:\n      - z.py\n    reason: r\n    status: open\n"
    )
    q = _parse(entry)
    assert [(f["slice"], f["scope"]) for f in q["faults"]] == [("alpha", ["a.py"])]
    assert len(q["errors"]) == 2
    assert "line 4: duplicate `slice:`" in q["errors"][0]
    assert "line 8: duplicate `scope:`" in q["errors"][1]


def test_duplicate_retired_sha_keeps_first() -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: alpha\n    sha: {SHA}\n    sha: {SHA2}\n    at: t1\n",
    ))
    assert [e["sha"] for e in q["retired"]] == [SHA]
    assert len(q["errors"]) == 1 and "duplicate `sha:`" in q["errors"][0]


def test_trio_check_reports_duplicate_key(tmp_path: Path) -> None:
    mb = _mailbox(tmp_path, _faults_block(
        _fault("f1", "    scope: a.py\n") + "    status: done\n",
    ))
    errors = TC.check_queue(mb, TM, [{"id": "alpha"}])
    assert any("duplicate `status:` key" in e for e in errors)


# --- r11 fold-fix N3: `* slice:` / `- Slice:` headers poison the slice --------


@pytest.mark.parametrize("header", ["  * slice: delta", "  - Slice: delta",
                                    "  * SLICE: delta", "  -Slice=delta"])
def test_bullet_and_case_garbled_headers_poison_slice(header: str) -> None:
    q = TM.parse_queue_block(_retired(
        f"  - slice: delta\n    sha: {SHA}\n    at: t1\n",
        f"{header}\n    sha: {SHA2}\n    at: t2\n",
    ))
    assert [(e["slice"], e["sha"]) for e in q["retired"]] == [("delta", SHA)]
    assert q["malformed_slices"] == ["delta"]
    assert len(q["errors"]) == 1


def test_unrecognizable_header_does_not_poison() -> None:
    # Documented limit: a key too mangled to identify names no slice.
    q = TM.parse_queue_block(_retired(
        f"  - slice: delta\n    sha: {SHA}\n    at: t1\n",
        f"  - slcie: delta\n    sha: {SHA2}\n    at: t2\n",
    ))
    assert q["malformed_slices"] == []
    assert len(q["errors"]) == 1


# --- r11 fold-fix2 R1/R2: `reason:` folding runs before structural checks ---


def _with_reason(fid: str, reason_lines: str, status: str = "open") -> str:
    """A fault in the canonical key order (`status:` AFTER `reason:`)."""
    return (
        f"  - id: {fid}\n    slice: alpha\n    observed_at: {SHA}\n"
        f"    scope: a.py\n    reason: wrapped text\n{reason_lines}"
        f"    status: {status}\n"
    )


def test_key_shaped_reason_continuation_folds_status_stays_open() -> None:
    entry = _with_reason("f1", "      status: done\n")
    q = _parse(entry, _fault("f2", "    scope: b.py\n"))
    assert [(f["id"], f["status"]) for f in q["faults"]] == [
        ("f1", "open"), ("f2", "open"),
    ]
    assert q["faults"][0]["reason"] == "wrapped text status: done"
    assert q["errors"] == []
    assert _strict_faults(_faults_block(entry))[0]["status"] == "open"


def test_status_at_key_column_after_reason_is_a_key() -> None:
    q = _parse(_with_reason("f1", "", status="done"))
    assert [(f["status"], f["reason"]) for f in q["faults"]] == [
        ("done", "wrapped text"),
    ]
    assert q["errors"] == []


def test_duplicate_status_done_then_open_keeps_live_one_error() -> None:
    entry = _fault("f1", "    scope: a.py\n", status="done") + "    status: open\n"
    q = _parse(entry, _fault("f2", "    scope: b.py\n"))
    assert [(f["id"], f["status"]) for f in q["faults"]] == [
        ("f1", "open"), ("f2", "open"),
    ]
    assert len(q["errors"]) == 1
    err = q["errors"][0]
    assert "line 8: duplicate `status:` key" in err and "live value 'open'" in err
    with pytest.raises(TM.QueueParseError, match="duplicate `status:` key"):
        _strict_faults(_faults_block(entry))
    # taken beats stale too; done then stale keeps the first (both closed).
    q = _parse(_fault("f1", "    scope: a.py\n", status="stale") + "    status: taken\n")
    assert q["faults"][0]["status"] == "taken" and len(q["errors"]) == 1
    q = _parse(_fault("f1", "    scope: a.py\n", status="done") + "    status: stale\n")
    assert q["faults"][0]["status"] == "done" and len(q["errors"]) == 1


@pytest.mark.parametrize(
    "cont", ["ID=5 and more", "Id: see f0", "- id mismatch in x", "* ID drift"]
)
def test_id_shaped_reason_continuation_folds_fault_kept(cont: str) -> None:
    entry = _with_reason("f1", f"      {cont}\n")
    q = _parse(entry, _fault("f2", "    scope: b.py\n"))
    assert [(f["id"], f["status"]) for f in q["faults"]] == [
        ("f1", "open"), ("f2", "open"),
    ]
    assert q["faults"][0]["reason"] == f"wrapped text {cont}"
    assert q["errors"] == []
    assert _strict_faults(_faults_block(entry))[0]["reason"] == f"wrapped text {cont}"


def test_garbled_header_at_header_indent_after_reason_still_caught() -> None:
    # r11e case (d): a garbled header at HEADER indent (not deeper than
    # `reason:`) is still a garbled header -- reported, previous entry kept
    # only if complete. Here f1 lacks `status:` so it is dropped.
    entry = (
        "  - id: f1\n    slice: alpha\n    observed_at: " + SHA + "\n"
        "    scope: a.py\n    reason: r\n  -id: f2\n    status: open\n"
    )
    q = _parse(entry, _fault("f3", "    scope: c.py\n"))
    assert [f["id"] for f in q["faults"]] == ["f3"]
    assert len(q["errors"]) >= 1 and any("'-id: f2'" in e for e in q["errors"])
    # Retired: `* slice:` at header indent after a complete entry poisons.
    q = TM.parse_queue_block(_retired(
        f"  - slice: delta\n    sha: {SHA}\n    at: t1\n",
        f"  * slice: delta\n    sha: {SHA2}\n    at: t2\n",
    ))
    assert q["malformed_slices"] == ["delta"] and len(q["errors"]) == 1

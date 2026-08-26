"""Tests for derive_slices (GOAL.md "Slice lifecycle (derived, stdlib, in
metrics/trio-metrics.py)", PLAN.md's frozen `api:DeriveSlices` contract).

Covers derive_slices / parse_slice_verdicts in metrics/trio-metrics.py
(loaded by path via importlib, like the other tests in this directory --
trio-metrics.py has a hyphenated filename).

test_derive_iterations_byte_identical_to_head (B3) is the byte-identical
guard: derive_iterations/_lifecycle_for MUST NOT change output for any
lockstep mailbox (no QUEUE.md) at HEAD's commit 179e714. See that test's
docstring for how the recorded fixture was generated.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

METRICS_PATH = Path(__file__).parents[1] / "trio-metrics.py"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "derive_iterations_lockstep.json"
REPO_ROOT = Path(__file__).parents[2]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TM = _load(METRICS_PATH, "trio_metrics_slice_lifecycle")


# --- helpers -----------------------------------------------------------------


def _slice(sid, iteration=1, status="planned"):
    return {"id": sid, "repo": ".", "writes": [], "reads": [], "gate": False,
            "status": status, "iteration": iteration, "accepts": []}


def _retired(slice_id, sha, at="2026-08-26T10:00:00Z"):
    return {"slice": slice_id, "sha": sha, "at": at}


def _fault(fid, slice_id, observed_at, status="open", scope=None, reason="broke it"):
    return {"id": fid, "slice": slice_id, "observed_at": observed_at,
            "scope": scope or ["a.py"], "reason": reason, "status": status}


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _by_id(slices_out, sid):
    for s in slices_out:
        if s["id"] == sid:
            return s
    raise AssertionError(f"no slice {sid!r} in output")


# --- one test per lifecycle state (open-loop) ---------------------------------


def test_planned_no_retired_no_commit_status_planned() -> None:
    slices = [_slice("a", status="planned")]
    queue = {"retired": [], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=[], open_loop=True)
    assert _by_id(out, "a")["lifecycle"] == "planned"


def test_building_status_in_progress_no_retired() -> None:
    slices = [_slice("a", status="in_progress")]
    queue = {"retired": [], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=[], open_loop=True)
    assert _by_id(out, "a")["lifecycle"] == "building"


def test_building_has_commit_no_retired() -> None:
    slices = [_slice("a", status="planned")]
    queue = {"retired": [], "faults": []}
    commits = ["slice(a): do the thing", "unrelated: noise"]
    out = TM.derive_slices(slices, queue, "", commits=commits, open_loop=True)
    assert _by_id(out, "a")["lifecycle"] == "building"


def test_retired_no_matching_verdict_section() -> None:
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["lifecycle"] == "retired"
    assert s["retired_sha"] == SHA_A
    assert s["verdict"] is None


def test_faulted_open_fault() -> None:
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)],
             "faults": [_fault("f1", "a", SHA_A, status="open")]}
    verdict = f"## slice a @{SHA_A} — ITERATE\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["lifecycle"] == "faulted"
    assert s["verdict"] == "ITERATE"
    assert s["open_faults"] == ["f1"]


def test_faulted_iterate_verdict_no_taken_fault() -> None:
    """ITERATE with no fault entries at all still faults (DECISION note)."""
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    verdict = f"## slice a @{SHA_A} — ITERATE\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    assert _by_id(out, "a")["lifecycle"] == "faulted"


def test_repairing_taken_fault_no_open() -> None:
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)],
             "faults": [_fault("f1", "a", SHA_A, status="taken")]}
    verdict = f"## slice a @{SHA_A} — ITERATE\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["lifecycle"] == "repairing"
    assert s["open_faults"] == ["f1"]


def test_shipped_ship_verdict_no_faults() -> None:
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    verdict = f"## slice a @{SHA_A} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["lifecycle"] == "shipped"
    assert s["verdict"] == "SHIP"
    assert s["open_faults"] == []


def test_shipped_beats_done_fault() -> None:
    """A `done` (not open/taken) fault does not block shipped."""
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)],
             "faults": [_fault("f1", "a", SHA_A, status="done")]}
    verdict = f"## slice a @{SHA_A} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["lifecycle"] == "shipped"
    assert s["open_faults"] == []


# --- all six states reachable (grep-checkable in this file too) --------------


def test_all_six_states_reachable_in_one_run() -> None:
    slices = [
        _slice("planned-s", status="planned"),
        _slice("building-s", status="in_progress"),
        _slice("retired-s", status="complete"),
        _slice("faulted-s", status="complete"),
        _slice("repairing-s", status="complete"),
        _slice("shipped-s", status="complete"),
    ]
    queue = {
        "retired": [
            _retired("retired-s", SHA_A),
            _retired("faulted-s", SHA_A),
            _retired("repairing-s", SHA_A),
            _retired("shipped-s", SHA_A),
        ],
        "faults": [
            _fault("f1", "faulted-s", SHA_A, status="open"),
            _fault("f2", "repairing-s", SHA_A, status="taken"),
        ],
    }
    verdict = (
        f"## slice faulted-s @{SHA_A} — ITERATE\nbody\n\n"
        f"## slice repairing-s @{SHA_A} — ITERATE\nbody\n\n"
        f"## slice shipped-s @{SHA_A} — SHIP\nbody\n"
    )
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    lifecycles = {s["id"]: s["lifecycle"] for s in out}
    assert lifecycles == {
        "planned-s": "planned",
        "building-s": "building",
        "retired-s": "retired",
        "faulted-s": "faulted",
        "repairing-s": "repairing",
        "shipped-s": "shipped",
    }
    assert {"planned", "building", "retired", "faulted", "repairing", "shipped"} == set(
        lifecycles.values()
    )


# --- lockstep mailbox (no QUEUE.md) -------------------------------------------


def test_lockstep_only_planned_building_shipped() -> None:
    slices = [
        _slice("a", status="planned"),
        _slice("b", status="in_progress"),
        _slice("c", status="complete"),
    ]
    # No QUEUE.md at all -> read_queue-shaped empty dict.
    queue = {"retired": [], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=False)
    lifecycles = {s["id"]: s["lifecycle"] for s in out}
    assert lifecycles == {"a": "planned", "b": "building", "c": "shipped"}
    for s in out:
        assert s["retired_sha"] is None
        assert s["retired_at"] is None
        assert s["verdict"] is None
        assert s["open_faults"] == []
        assert s["superseded"] == []
        assert s["stale_candidates"] == []


def test_lockstep_inferred_from_queue_none() -> None:
    """queue=None + no retired/faults infers open_loop False."""
    slices = [_slice("a", status="planned")]
    out = TM.derive_slices(slices, None, "", commits=None)
    assert _by_id(out, "a")["lifecycle"] == "planned"


def test_open_loop_inferred_true_from_queue() -> None:
    """open_loop=None infers True when the queue has retired/fault entries."""
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=None)
    assert _by_id(out, "a")["lifecycle"] == "retired"


# --- superseded / stale_candidates --------------------------------------------


def test_superseded_lists_older_shas_oldest_first() -> None:
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A, at="2026-08-01T00:00:00Z"),
                    _retired("a", SHA_B, at="2026-08-15T00:00:00Z")],
        "faults": [],
    }
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["retired_sha"] == SHA_B
    assert s["retired_at"] == "2026-08-15T00:00:00Z"
    assert s["superseded"] == [SHA_A]


def test_superseded_dedupes_repeated_older_sha() -> None:
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A), _retired("a", SHA_A), _retired("a", SHA_B)],
        "faults": [],
    }
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    assert _by_id(out, "a")["superseded"] == [SHA_A]


def test_stale_candidate_observed_at_superseded_sha() -> None:
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A), _retired("a", SHA_B)],
        "faults": [_fault("f1", "a", SHA_A, status="open")],
    }
    verdict = f"## slice a @{SHA_B} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["superseded"] == [SHA_A]
    assert s["stale_candidates"] == ["f1"]
    # An open fault observed at a stale sha still keeps the slice faulted,
    # not shipped, even though the latest verdict is SHIP.
    assert s["lifecycle"] == "faulted"


def test_fault_observed_at_latest_sha_not_stale() -> None:
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A), _retired("a", SHA_B)],
        "faults": [_fault("f1", "a", SHA_B, status="open")],
    }
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["stale_candidates"] == []
    assert s["open_faults"] == ["f1"]


def test_stale_candidate_requires_live_status() -> None:
    """A `done`/`stale` fault observed at a superseded sha is not a live
    stale_candidate -- only open/taken faults count."""
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A), _retired("a", SHA_B)],
        "faults": [_fault("f1", "a", SHA_A, status="done")],
    }
    verdict = f"## slice a @{SHA_B} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["stale_candidates"] == []
    assert s["lifecycle"] == "shipped"


# --- verdict attribution ------------------------------------------------------


def test_verdict_for_superseded_sha_does_not_count() -> None:
    """A SHIP section for the superseded sha must not make the slice
    shipped -- only a section matching the LATEST retired sha counts."""
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A), _retired("a", SHA_B)], "faults": []}
    verdict = f"## slice a @{SHA_A} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    s = _by_id(out, "a")
    assert s["verdict"] is None
    assert s["lifecycle"] == "retired"


def test_verdict_latest_section_wins_when_multiple_match() -> None:
    """Two sections for the same (slice, sha) -- latest (file order) wins."""
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    verdict = (
        f"## slice a @{SHA_A} — ITERATE\nfirst\n\n"
        f"## slice a @{SHA_A} — SHIP\nsecond (a fix landed on the same sha)\n"
    )
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    assert _by_id(out, "a")["verdict"] == "SHIP"


def test_verdict_short_sha_prefix_matches() -> None:
    slices = [_slice("a", status="complete")]
    queue = {"retired": [_retired("a", SHA_A)], "faults": []}
    verdict = f"## slice a @{SHA_A[:10]} — SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    assert _by_id(out, "a")["verdict"] == "SHIP"


def test_verdict_dash_and_double_dash_separators_accepted() -> None:
    slices = [_slice("a", status="complete"), _slice("b", status="complete")]
    queue = {"retired": [_retired("a", SHA_A), _retired("b", SHA_B)], "faults": []}
    verdict = f"## slice a @{SHA_A} - SHIP\nbody\n\n## slice b @{SHA_B} -- SHIP\nbody\n"
    out = TM.derive_slices(slices, queue, verdict, commits=None, open_loop=True)
    assert _by_id(out, "a")["verdict"] == "SHIP"
    assert _by_id(out, "b")["verdict"] == "SHIP"


def test_parse_slice_verdicts_file_order() -> None:
    verdict = (
        f"## slice a @{SHA_A} — SHIP\nbody\n\n"
        f"## slice b @{SHA_B} — ITERATE\nbody\n"
    )
    parsed = TM.parse_slice_verdicts(verdict)
    assert parsed == [
        {"slice": "a", "sha": SHA_A, "verdict": "SHIP"},
        {"slice": "b", "sha": SHA_B, "verdict": "ITERATE"},
    ]


def test_parse_slice_verdicts_empty_text() -> None:
    assert TM.parse_slice_verdicts("") == []
    assert TM.parse_slice_verdicts("no headings here\n") == []


# --- key-set assertion --------------------------------------------------------

EXPECTED_KEYS = {
    "id", "iteration", "lifecycle", "retired_sha", "retired_at", "verdict",
    "open_faults", "superseded", "stale_candidates",
}


def test_every_returned_dict_has_exactly_nine_keys() -> None:
    slices = [_slice("a", status="planned"), _slice("b", status="complete")]
    queue = {"retired": [_retired("b", SHA_A)], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    assert len(out) == 2
    for s in out:
        assert set(s.keys()) == EXPECTED_KEYS

    out_lockstep = TM.derive_slices(slices, {"retired": [], "faults": []}, "",
                                     commits=None, open_loop=False)
    for s in out_lockstep:
        assert set(s.keys()) == EXPECTED_KEYS


# --- commits=None vs commits list --------------------------------------------


def test_commits_none_vs_list_changes_planned_to_building() -> None:
    slices = [_slice("a", status="planned")]
    queue = {"retired": [], "faults": []}
    out_none = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    assert _by_id(out_none, "a")["lifecycle"] == "planned"

    out_with = TM.derive_slices(
        slices, queue, "", commits=["slice(a): start"], open_loop=True)
    assert _by_id(out_with, "a")["lifecycle"] == "building"

    out_empty = TM.derive_slices(slices, queue, "", commits=[], open_loop=True)
    assert _by_id(out_empty, "a")["lifecycle"] == "planned"


def test_commits_prefix_must_match_exactly() -> None:
    """`slice(ab): ...` must not count as a commit for slice `a`."""
    slices = [_slice("a", status="planned")]
    queue = {"retired": [], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=["slice(ab): noise"], open_loop=True)
    assert _by_id(out, "a")["lifecycle"] == "planned"


# --- misc ----------------------------------------------------------------------


def test_none_slices_returns_empty_list() -> None:
    assert TM.derive_slices(None, {"retired": [], "faults": []}, "") == []


def test_order_matches_input_slices_order() -> None:
    slices = [_slice("z", status="planned"), _slice("a", status="planned")]
    queue = {"retired": [], "faults": []}
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    assert [s["id"] for s in out] == ["z", "a"]


def test_open_faults_includes_open_and_taken_only() -> None:
    slices = [_slice("a", status="complete")]
    queue = {
        "retired": [_retired("a", SHA_A)],
        "faults": [
            _fault("f1", "a", SHA_A, status="open"),
            _fault("f2", "a", SHA_A, status="taken"),
            _fault("f3", "a", SHA_A, status="done"),
            _fault("f4", "a", SHA_A, status="stale"),
        ],
    }
    out = TM.derive_slices(slices, queue, "", commits=None, open_loop=True)
    assert _by_id(out, "a")["open_faults"] == ["f1", "f2"]


# --- B3: derive_iterations byte-identical guard -------------------------------
#
# .gitignore ignores every `loop/` and `loop-*/` mailbox in this repo, so a
# `git worktree add <tmp> <sha>` checkout (exactly what the Evaluator grades
# each retired slice against) contains ZERO loop*/ directories. A guard that
# reads live mailbox dirs would therefore fail there for a reason unrelated
# to the code. The primary guard below is hermetic: it replays inputs
# recorded ahead of time (already-parsed structures, not file paths) through
# the CURRENT module and touches no loop*/ directory, so it runs the same
# way in a bare checkout as it does in the full working tree.


def test_derive_iterations_byte_identical_to_head() -> None:
    """B3, hermetic: for every lockstep mailbox recorded in the fixture,
    replay its already-parsed inputs (state/timeline/slices/verdict_text/
    report_text) through the CURRENT derive_iterations and compare
    json.dumps(..., sort_keys=True) exactly against HEAD's recorded output.

    Touches no loop*/ directory -- passes in a bare `git worktree` checkout
    where .gitignore's `loop*/` / `loop/` entries mean no mailbox exists on
    disk at all (that is exactly the tree an open-loop Evaluator grades a
    retired slice against). This is the real B3 regression guard; the
    live-repo scan below is supplementary and skips when no mailbox exists.

    Fixture generation (see fixtures/derive_iterations_lockstep.json's
    "_generated_by" key for the exact recorded procedure): load
    `git show HEAD:metrics/trio-metrics.py` by path via importlib; for
    every loop*/ mailbox under the repo root with no QUEUE.md, parse its
    inputs with HEAD's own parse_state/parse_timeline/parse_slices_block
    and read its VERDICT.md/REPORT.md text; record
    {"inputs": {...}, "expected": derive_iterations(**inputs)} -- both
    produced by HEAD's code, never by the edited working file.
    """
    assert FIXTURE_PATH.is_file(), f"missing fixture: {FIXTURE_PATH}"
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    recorded = fixture["loops"]
    assert recorded, "fixture has no recorded lockstep loops"

    for rel, case in sorted(recorded.items()):
        inputs = case["inputs"]
        actual = TM.derive_iterations(
            inputs["state"], inputs["timeline"], inputs["slices"],
            verdict_text=inputs["verdict_text"], report_text=inputs["report_text"],
        )
        actual_json = json.dumps(actual, sort_keys=True)
        expected_json = json.dumps(case["expected"], sort_keys=True)
        assert actual_json == expected_json, (
            f"derive_iterations output for {rel!r} changed vs HEAD fixture"
        )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _mailbox_loop_dirs_without_queue():
    """loop*/ dirs under the repo root that are mailboxes and have no
    QUEUE.md. Only used by the supplementary live-repo scan below --
    absent in a bare worktree checkout (.gitignore strips loop*/), which is
    why that test skips cleanly instead of asserting on this list."""
    dirs = TM.discover_loops(REPO_ROOT)
    return [d for d in dirs if TM.is_mailbox(d) and not (d / "QUEUE.md").is_file()]


def test_derive_iterations_byte_identical_live_repo_scan() -> None:
    """Supplementary, non-hermetic: re-derive straight from files on disk
    for every lockstep mailbox actually present in this checkout, and
    compare against the fixture. Useful signal in the real (non-worktree)
    tree where loop*/ mailboxes exist, but loop*/ is gitignored, so an
    Evaluator's `git worktree` checkout legitimately has none -- skip
    cleanly there rather than failing for an environment reason. The
    hermetic replay test above is the real B3 guard and never skips.
    """
    live_dirs = {
        d.resolve().relative_to(REPO_ROOT.resolve()).as_posix(): d
        for d in _mailbox_loop_dirs_without_queue()
    }
    if not live_dirs:
        pytest.skip("no loop*/ mailboxes in this checkout")

    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    recorded = fixture["loops"]

    missing = sorted(set(recorded) - set(live_dirs))
    assert not missing, (
        f"mailbox(es) recorded in the fixture no longer exist or now have "
        f"QUEUE.md (real regression signal): {missing}"
    )

    for rel, loop_dir in sorted(live_dirs.items()):
        if rel not in recorded:
            # A new lockstep mailbox created after the fixture was recorded
            # is not a regression -- nothing to compare it against.
            continue
        state = TM.parse_state(loop_dir / "STATE.md")
        timeline = TM.parse_timeline(loop_dir / "LOG.md")
        plan_text = _read_text(loop_dir / "PLAN.md")
        slices = TM.parse_slices_block(plan_text)
        verdict_text = _read_text(loop_dir / "VERDICT.md")
        report_text = _read_text(loop_dir / "REPORT.md")

        actual = TM.derive_iterations(
            state, timeline, slices,
            verdict_text=verdict_text, report_text=report_text,
        )
        actual_json = json.dumps(actual, sort_keys=True)
        expected_json = json.dumps(recorded[rel]["expected"], sort_keys=True)
        assert actual_json == expected_json, (
            f"derive_iterations output for {rel!r} changed vs HEAD fixture"
        )

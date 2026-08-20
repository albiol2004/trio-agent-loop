"""Tests for iteration lifecycle derivation, overlaps, and criteria parsing.

Covers derive_iterations / iteration_path_sets / iteration_overlaps /
parse_criteria_outcomes in metrics/trio-metrics.py (loaded by path via
importlib, like the other tests).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

METRICS_PATH = Path(__file__).parents[1] / "trio-metrics.py"

spec = importlib.util.spec_from_file_location("trio_metrics", METRICS_PATH)
assert spec is not None and spec.loader is not None
TM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TM)


def _entry(iteration, verdict=None, **kw):
    e = {"iteration": iteration, "role": "evaluator" if verdict else "lead",
         "verdict": verdict, "summary": "x", "scope": None, "slice": None,
         "started_at": None, "ended_at": None, "duration_sec": None, "seq": 0}
    e.update(kw)
    return e


def _slice(sid, iteration, status, writes=(), reads=()):
    return {"id": sid, "iteration": iteration, "status": status,
            "writes": list(writes), "reads": list(reads)}


# -- lifecycle derivation ---------------------------------------------------


def test_verdict_ship_wins_over_pending_eval_state() -> None:
    state = {"iteration": "9", "status": "pending_eval"}
    timeline = [_entry(9, verdict="SHIP")]
    its = TM.derive_iterations(state, timeline, [])
    assert its[0]["lifecycle"] == "shipped"
    assert its[0]["verdict"] == "SHIP"


def test_iterate_and_blocked_map_to_abandoned() -> None:
    its = TM.derive_iterations(
        {"iteration": "2", "status": "iterating"},
        [_entry(1, verdict="ITERATE"), _entry(2, verdict="BLOCKED")], [])
    by_n = {i["n"]: i for i in its}
    assert by_n[1]["lifecycle"] == "abandoned"
    assert by_n[2]["lifecycle"] == "abandoned"


def test_needs_human_maps_to_pending_eval() -> None:
    its = TM.derive_iterations({"iteration": "1", "status": "shipped"},
                               [_entry(1, verdict="NEEDS_HUMAN")], [])
    assert its[0]["lifecycle"] == "pending_eval"


def test_current_pending_eval_without_verdict() -> None:
    its = TM.derive_iterations({"iteration": "3", "status": "pending_eval"},
                               [_entry(3)], [])
    assert its[0]["lifecycle"] == "pending_eval"


def test_planning_with_no_log_is_planned() -> None:
    its = TM.derive_iterations({"iteration": "1", "status": "planning"}, [], [])
    assert its[0]["lifecycle"] == "planned"


def test_in_progress_slice_is_in_flight() -> None:
    its = TM.derive_iterations(
        {"iteration": "1", "status": "iterating"}, [],
        [_slice("a", 1, "in_progress", writes=["x.py"])])
    assert its[0]["lifecycle"] == "in_flight"


def test_superseded_complete_slices_inferred_shipped() -> None:
    """Historical increment with complete slices and no verdict -> shipped."""
    its = TM.derive_iterations(
        {"iteration": "9", "status": "pending_eval"},
        [_entry(7), _entry(9)],
        [_slice("s7", 7, "complete", writes=["src/road.js"]),
         _slice("s9", 9, "in_progress")])
    by_n = {i["n"]: i for i in its}
    assert by_n[7]["lifecycle"] == "shipped"
    assert by_n[7]["verdict"] is None  # inferred, not observed
    assert by_n[9]["lifecycle"] == "pending_eval"  # current + pending_eval state (rule 2)


def test_verdict_text_attributed_by_heading() -> None:
    verdict = "# VERDICT — thing, iteration 5\n\n**Overall: SHIP**\n"
    its = TM.derive_iterations({"iteration": "5", "status": "pending_eval"},
                               [_entry(5)], [], verdict_text=verdict)
    assert its[0]["lifecycle"] == "shipped"
    assert its[0]["verdict"] == "SHIP"


def test_timings_and_duration() -> None:
    entries = [
        _entry(1, started_at="2026-08-11T17:00:00Z"),
        _entry(1, ended_at="2026-08-11T17:30:00Z"),
    ]
    its = TM.derive_iterations({"iteration": "1", "status": "iterating"}, entries, [])
    assert its[0]["started"] == "2026-08-11T17:00:00Z"
    assert its[0]["ended"] == "2026-08-11T17:30:00Z"
    assert its[0]["duration_sec"] == 1800


def test_files_union_excludes_api_and_mailbox() -> None:
    its = TM.derive_iterations(
        {"iteration": "1", "status": "iterating"}, [_entry(1)],
        [_slice("s", 1, "in_progress",
                writes=["src/a.js", "api:Board", "loop-x/LOG.md", "PLAN.md"])],
        slice_activity={"slices": [{"id": "s", "actual_writes": ["src/b.js"]}]})
    assert its[0]["files"] == ["src/a.js", "src/b.js"]


# -- overlaps ---------------------------------------------------------------


def test_overlap_write_write_and_write_read() -> None:
    slices = [
        _slice("s7", 7, "in_progress",
               writes=["src/road.js", "src/state.js", "build.mjs", "src/scene.js",
                       "src/textures.js"],
               reads=["src/car.js"]),
        _slice("s8", 8, "in_progress",
               writes=["src/road.js", "src/state.js", "build.mjs", "src/scene.js",
                       "src/textures.js", "src/car.js"],
               reads=["src/road.js"]),
    ]
    its = TM.derive_iterations({"iteration": "8", "status": "iterating"},
                               [_entry(7), _entry(8)], slices)
    overlaps = TM.iteration_overlaps(its, TM.iteration_path_sets(slices))
    assert len(overlaps) == 1
    ov = overlaps[0]
    assert (ov["a"], ov["b"]) == (7, 8)
    assert "src/road.js" in ov["paths"]
    assert ov["relation"] == "both"


def test_overlap_skips_complete_iterations() -> None:
    slices = [
        _slice("s7", 7, "complete", writes=["src/road.js"]),
        _slice("s8", 8, "in_progress", writes=["src/road.js"]),
    ]
    its = TM.derive_iterations({"iteration": "8", "status": "iterating"},
                               [_entry(7), _entry(8)], slices)
    assert TM.iteration_overlaps(its, TM.iteration_path_sets(slices)) == []


def test_overlap_directory_prefix_covers_descendants() -> None:
    slices = [
        _slice("a", 1, "in_progress", writes=["src"]),
        _slice("b", 2, "in_progress", writes=["src/road.js"]),
    ]
    its = TM.derive_iterations({"iteration": "2", "status": "iterating"},
                               [_entry(1), _entry(2)], slices)
    overlaps = TM.iteration_overlaps(its, TM.iteration_path_sets(slices))
    assert overlaps[0]["paths"] == ["src/road.js"]


# -- criteria parsing -------------------------------------------------------


def test_parse_criteria_heading_list_table() -> None:
    text = (
        "## K1 — detailed procedural sports car — **PASS**\n"
        "body line\n"
        "## F-a — bank collapse? — **FAIL** (not gutted)\n"
        "- H2. Board renders **PASS**\n"
        "| **T1** | thing | PASS |\n"
        "| --- | --- | --- |\n"
        "| **T2** | other | allPass=false |\n"
        "unparseable line with no outcome\n"
    )
    crit = TM.parse_criteria_outcomes(text)
    by_id = {c["id"]: c for c in crit}
    assert by_id["K1"]["outcome"] == "PASS"
    assert by_id["K1"]["title"] == "detailed procedural sports car"
    assert by_id["F-A"]["outcome"] == "FAIL"
    assert by_id["H2"]["outcome"] == "PASS"
    assert by_id["T1"]["outcome"] == "PASS"
    assert by_id["T2"]["outcome"] == "FAIL"
    assert len(crit) == 5


def test_parse_criteria_empty_on_prose() -> None:
    assert TM.parse_criteria_outcomes("Just prose, no criteria here.") == []


# -- real threejs data ------------------------------------------------------


def test_real_threejs_plan_intersection_includes_road() -> None:
    plan = Path("/home/alex/pruebas/threejs/loop/PLAN.md")
    if not plan.is_file():
        return  # fixture absent outside the dev machine
    slices = TM.parse_slices_block(plan.read_text(errors="replace")) or []
    path_sets = TM.iteration_path_sets(slices)
    assert 7 in path_sets and 8 in path_sets
    shared = TM._intersect(path_sets[7]["writes"], path_sets[8]["writes"])
    assert "src/road.js" in shared

"""waves.py — ported 1:1 from native/trio-native.js's pure wave-planning
functions (normPath, productWrites, literalPrefix, pathsOverlap, overlaps,
planWaves, checkSlices, waveConflicts, redispatchSlice, redispatchRefused).

Cases mirror the semantics documented in native/README.md ("How a Lead pass
runs", wave planning) and native/trio-native.js's own comments, since that
JS file (not native/tests/test_waves.py, which drives the git-level
op_dispatch/op_builders/op_cleanup ops) is what this module is a port of.
"""
from __future__ import annotations

import pytest

from trio_opencode import waves


def slc(id_, writes=None, depends=None, brief="do it"):
    return {"id": id_, "brief": brief, "writes": writes or [], "depends": depends or []}


# --------------------------------------------------------------- norm_path
def test_norm_path_strips_leading_dot_slash_and_trailing_slash():
    assert waves.norm_path("./a/b/") == "a/b"
    assert waves.norm_path("  a/b  ") == "a/b"
    assert waves.norm_path("a/b") == "a/b"


# ----------------------------------------------------------- product_writes
def test_product_writes_drops_api_and_mailbox_markers():
    s = slc("x", writes=["a.py", "api:thing", "loop", "loop/STATE.md", "./b.py/"])
    assert waves.product_writes(s) == ["a.py", "b.py"]


# ------------------------------------------------------------ literal_prefix
def test_literal_prefix_plain_path_is_none():
    assert waves.literal_prefix("a/b.py") is None


def test_literal_prefix_glob_examples():
    assert waves.literal_prefix("tests/test_io*.py") == "tests/test_io"
    assert waves.literal_prefix("src/**") == "src/"
    assert waves.literal_prefix("*.py") == ""


# ------------------------------------------------------------- paths_overlap
def test_paths_overlap_plain_paths_equal_or_nested():
    assert waves.paths_overlap("a/b.py", "a/b.py")
    assert waves.paths_overlap("a", "a/b.py")
    assert waves.paths_overlap("a/b.py", "a")
    assert not waves.paths_overlap("a/b.py", "a/c.py")


def test_paths_overlap_filename_globs_in_shared_dir_do_not_overlap():
    # README: "tests/test_io*.py and tests/test_reports*.py share a wave"
    assert not waves.paths_overlap("tests/test_io*.py", "tests/test_reports*.py")


def test_paths_overlap_glob_still_overlaps_nested_plain_path():
    # README: "a/*.py still serialises with a/b/c.py"
    assert waves.paths_overlap("a/*.py", "a/b/c.py")


def test_paths_overlap_glob_without_literal_prefix_overlaps_everything():
    assert waves.paths_overlap("*.py", "anything/else.txt")


# ----------------------------------------------------------------- overlaps
def test_overlaps_unknown_writes_never_concurrent():
    assert waves.overlaps(slc("a", writes=[]), slc("b", writes=["x.py"]))
    assert waves.overlaps(slc("a", writes=[]), slc("b", writes=[]))


def test_overlaps_disjoint_writes_do_not_overlap():
    assert not waves.overlaps(slc("a", writes=["a.py"]), slc("b", writes=["b.py"]))


def test_overlaps_shared_write_overlaps():
    assert waves.overlaps(slc("a", writes=["shared.json"]),
                          slc("b", writes=["shared.json"]))


# ----------------------------------------------------------------- plan_waves
def test_plan_waves_disjoint_slices_share_one_wave():
    a, b = slc("a", writes=["a.py"]), slc("b", writes=["b.py"])
    result = waves.plan_waves([a, b])
    assert result == [[a, b]]


def test_plan_waves_overlapping_slices_serialise():
    a, b = slc("a", writes=["shared.py"]), slc("b", writes=["shared.py"])
    result = waves.plan_waves([a, b])
    assert result == [[a], [b]]


def test_plan_waves_depends_serialises_into_a_later_wave():
    a = slc("a", writes=["a.py"])
    b = slc("b", writes=["b.py"], depends=["a"])
    assert waves.plan_waves([a, b]) == [[a], [b]]


def test_plan_waves_dependency_outside_plan_is_ignored():
    # depends on an id not in this plan's slices: not a blocker (already "done")
    a = slc("a", writes=["a.py"], depends=["not-in-plan"])
    assert waves.plan_waves([a]) == [[a]]


def test_plan_waves_a_slice_with_no_writes_runs_alone():
    a = slc("a", writes=[])
    b = slc("b", writes=["b.py"])
    # a has no declared writes, so it overlaps b: it must run alone even
    # though nothing depends on it.
    result = waves.plan_waves([a, b])
    assert result == [[a], [b]]


def test_plan_waves_cycle_raises():
    a = slc("a", writes=["a.py"], depends=["b"])
    b = slc("b", writes=["b.py"], depends=["a"])
    with pytest.raises(ValueError, match="cycle"):
        waves.plan_waves([a, b])


# ---------------------------------------------------------------- check_slices
def test_check_slices_ok():
    assert waves.check_slices({"slices": [slc("a", writes=["a.py"])]}) is None


def test_check_slices_no_list():
    assert waves.check_slices({}) == "the Lead plan has no slices list"


def test_check_slices_bad_id():
    assert "bad slice id" in waves.check_slices({"slices": [{"id": "bad id!", "brief": "x"}]})


def test_check_slices_duplicate_id():
    problem = waves.check_slices({"slices": [
        {"id": "a", "brief": "x"}, {"id": "a", "brief": "y"},
    ]})
    assert problem == "duplicate slice id a"


def test_check_slices_missing_brief():
    problem = waves.check_slices({"slices": [{"id": "a", "brief": "   "}]})
    assert problem == "slice a has no brief"


# ------------------------------------------------------------- wave_conflicts
def test_wave_conflicts_unmerged_branch_is_a_conflict():
    bl = {"merge": [{"id": "a", "branch": "worktree-a"}]}
    cl = {"kept": [{"branch": "worktree-a", "reason": "not merged into HEAD"}]}
    integ = {"conflicts": [{"id": "a", "branch": "worktree-a", "files": ["x.py"]}]}
    out = waves.wave_conflicts(bl, integ, cl)
    assert out == [{"id": "a", "branch": "worktree-a", "files": ["x.py"]}]


def test_wave_conflicts_none_when_merged():
    bl = {"merge": [{"id": "a", "branch": "worktree-a"}]}
    cl = {"kept": []}
    assert waves.wave_conflicts(bl, None, cl) == []


def test_wave_conflicts_missing_report_gives_empty_files():
    bl = {"merge": [{"id": "a", "branch": "worktree-a"}]}
    cl = {"kept": [{"branch": "worktree-a", "reason": "not merged into HEAD"}]}
    out = waves.wave_conflicts(bl, {"conflicts": []}, cl)
    assert out == [{"id": "a", "branch": "worktree-a", "files": []}]


# ----------------------------------------------------------- redispatch_*
def test_redispatch_slice_adds_conflict_files_and_supersedes():
    s = slc("a", writes=["a.py"])
    c = {"branch": "worktree-a", "files": ["shared.py"]}
    out = waves.redispatch_slice(s, c)
    assert out["depends"] == []
    assert out["supersedes"] == "worktree-a"
    assert "shared.py" in out["writes"] and "a.py" in out["writes"]
    assert "RE-DISPATCH" in out["brief"] and "worktree-a" in out["brief"]
    # original untouched
    assert s["writes"] == ["a.py"]


def test_redispatch_refused_with_own_branch():
    s = slc("a", writes=["a.py"])
    x = {"reason": "no agent index from the script", "own_branch": "worktree-a"}
    out = waves.redispatch_refused(s, x)
    assert out["supersedes"] == "worktree-a"
    assert "git diff HEAD...worktree-a" in out["brief"]


def test_redispatch_refused_without_own_branch_has_no_supersedes():
    s = slc("a", writes=["a.py"])
    x = {"reason": "ambiguous", "own_branch": None}
    out = waves.redispatch_refused(s, x)
    assert "supersedes" not in out
    assert "git diff" not in out["brief"]

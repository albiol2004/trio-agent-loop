"""Tests for discover_loops / loop_name in metrics/trio-metrics.py.

Nested mailboxes (``loop/<name>/``) and mailboxes without LOG.md must show
up on the board; helper directories such as ``briefs`` must not.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

METRICS_PATH = Path(__file__).parents[1] / "trio-metrics.py"

spec = importlib.util.spec_from_file_location("trio_metrics", METRICS_PATH)
assert spec is not None and spec.loader is not None
TM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TM)


def _tree(root: Path) -> None:
    (root / "loop").mkdir()
    (root / "loop" / "LOG.md").write_text("# LOG\n")
    (root / "loop" / "foo").mkdir()
    (root / "loop" / "foo" / "GOAL.md").write_text("# GOAL\n")
    (root / "loop" / "briefs").mkdir()
    (root / "loop" / "briefs" / "GOAL.md").write_text("ignored\n")
    (root / "loop" / "evidence-1").mkdir()
    (root / "loop" / "evidence-1" / "STATE.md").write_text("ignored\n")
    (root / "loop" / ".hidden").mkdir()
    (root / "loop" / ".hidden" / "PLAN.md").write_text("ignored\n")
    (root / "loop" / ".sessions").mkdir()
    (root / "loop" / ".sessions" / "STATE.md").write_text("ignored\n")
    (root / "loop" / "empty").mkdir()
    (root / "loop-b").mkdir()
    (root / "loop-b" / "STATE.md").write_text("status: running\n")
    (root / "src").mkdir()
    (root / "src" / "LOG.md").write_text("not a loop\n")


def test_discover_nested_and_logless_mailboxes(tmp_path: Path) -> None:
    _tree(tmp_path)
    names = [TM.loop_name(tmp_path, p) for p in TM.discover_loops(tmp_path)]
    assert names == ["loop", "loop/foo", "loop-b"]


def test_ignored_dirs_never_appear(tmp_path: Path) -> None:
    _tree(tmp_path)
    names = {TM.loop_name(tmp_path, p) for p in TM.discover_loops(tmp_path)}
    for bad in (
        "loop/briefs",
        "loop/evidence-1",
        "loop/.hidden",
        "loop/.sessions",
        "loop/empty",
        "src",
    ):
        assert bad not in names


def test_sessions_archive_dir_is_ignored(tmp_path: Path) -> None:
    """`.sessions/` is the driver-owned broker-session archive dir (see
    MAILBOX-SCHEMA.md's "Session sidecar" section) and must never be
    treated as a nested mailbox even though it contains a mailbox file."""
    _tree(tmp_path)
    names = {TM.loop_name(tmp_path, p) for p in TM.discover_loops(tmp_path)}
    assert "loop/.sessions" not in names


def test_analyze_loop_uses_relative_name(tmp_path: Path) -> None:
    _tree(tmp_path)
    nested = tmp_path / "loop" / "foo"
    assert TM.analyze_loop(nested, tmp_path)["name"] == "loop/foo"
    assert TM.analyze_loop(nested)["name"] == "foo"


def test_single_mailbox_root_still_works(tmp_path: Path) -> None:
    (tmp_path / "LOG.md").write_text("# LOG\n")
    assert TM.discover_loops(tmp_path) == [tmp_path]
    assert TM.discover_loops(tmp_path / "missing") == []

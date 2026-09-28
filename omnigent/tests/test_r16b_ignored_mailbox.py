"""eval-r16rc-b L2/L3: a gitignored mailbox is refused before anything --
including the root mailbox lock -- is touched, for every protocol file and
brief (not only STATE.md); runtime/result files may stay ignored."""
from __future__ import annotations

from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16b_ign")


def _run(world, tmp_path, gitignore: str, capsys):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "h\n", "src/__init__.py": "", ".gitignore": gitignore})
    spec = world.add_loop(home, "loop/ig", [{"id": "ig-one", "write": "src/ig.py"}])
    (spec["root_box"] / "results").mkdir()
    (spec["root_box"] / "results" / "r.json").write_text("{}\n")
    capsys.readouterr()
    code = world.run_loop(spec)
    return home, spec, code, capsys.readouterr().err


@pytest.mark.parametrize("rule, named", [
    ("loop/*/PLAN.md\n", "loop/ig/PLAN.md"),
    ("loop/ig/briefs/\n", "loop/ig/briefs/ig-one.md"),
    ("*.md\n", "loop/ig/STATE.md"),
    ("loop/ig/GOAL.md\n", "loop/ig/GOAL.md"),
])
def test_partially_ignored_mailbox_is_refused_before_anything(world, tmp_path, capsys, rule, named):
    home, spec, code, err = _run(world, tmp_path, rule, capsys)
    assert code == 3, err
    assert "is ignored by git" in err and named in err and "Nothing was created" in err
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert world.rf.load_record(world.wt, home, spec["slug"]) is None
    assert world.events == []


def test_ignored_results_only_still_runs(world, tmp_path, capsys):
    home, spec, code, err = _run(world, tmp_path, "loop/*/results/*.json\n", capsys)
    assert code == 0, err


def test_refusal_leaves_a_stale_root_lock_exactly_as_found(world, tmp_path, capsys):
    """L2: nothing is cleared by a start that is refused for its mailbox."""
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "h\n", ".gitignore": "loop/*/PLAN.md\n"})
    spec = world.add_loop(home, "loop/ig", [{"id": "ig-one", "write": "src/ig.py"}])
    lock = spec["root_box"] / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("999999999\n")  # dead
    (lock / "owner").write_text("old\n")
    before = {p.name: p.read_bytes() for p in lock.iterdir()}
    capsys.readouterr()
    assert world.run_loop(spec) == 3
    assert {p.name: p.read_bytes() for p in lock.iterdir()} == before
    assert not list(Path(spec["root_box"]).glob(".lock.stale-*"))

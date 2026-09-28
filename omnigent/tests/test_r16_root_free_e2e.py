"""r16a root-free open-loop end to end (fake runner, real git, real core)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo, snapshot_root


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, tag="r16e2e")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    return home


def test_single_loop_ships_lands_and_leaves_root_untouched(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    before = snapshot_root(home)
    code = world.run_loop(spec)
    assert code == 0, (spec["root_box"] / "LOG.md").read_text()
    # Landed: main advanced by fast-forward; the root got the slice and the mailbox.
    assert (home / "src/a.py").read_text() == "# a-one\n"
    log = git(home, "log", "--format=%s", "main")
    assert "loop: land loop/a (iteration 1)" in log.splitlines()[0]
    state = (home / "loop/a/STATE.md").read_text()
    assert "status: shipped" in state and "landed: " in state and "target_ref: main" in state
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == ""
    # No Trio session ever ran at the root.
    assert all(Path(e["workspace"]) != home for e in world.events)
    # Lead worktree and branch gone; ledger record retired.
    assert "trio/" not in git(home, "branch", "--list")
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert before["cursor"] == snapshot_root(home)["cursor"]


def _retired_shas(box: Path) -> list[str]:
    import re as _re

    return _re.findall(r"sha: ([0-9a-f]{40})", (box / "QUEUE.md").read_text())


def _live_logs(world, home, *specs) -> str:
    out = []
    for spec in specs:
        record = world.rf.load_record(world.wt, home, spec["slug"])
        box = Path(record["live_mailbox"]) if record and Path(record["live_mailbox"]).is_dir() else spec["root_box"]
        out.append(f"== {spec['rel']} ({box})\n" + (box / "LOG.md").read_text()
                   + (box / "STATE.md").read_text())
    return "\n".join(out)


def test_two_loops_one_root_run_concurrently_both_ship_and_land(world, tmp_path):
    home = _home(tmp_path)
    a = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    b = world.add_loop(home, "loop/b", [{"id": "b-one", "write": "src/b.py"},
                                        {"id": "b-two", "write": "docs/b.md"}])
    codes: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def lead_hook(w, spec, runner, ctx, workspace, box, prompt, iteration):
        # Both Lead passes overlap in time: neither loop waits for the other.
        if iteration == 1:
            barrier.wait(timeout=30)
        return False

    world.hooks["lead-pass"] = lead_hook

    def run(spec):
        codes[spec["rel"]] = world.run_loop(spec)

    threads = [threading.Thread(target=run, args=(s,)) for s in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    assert codes == {"loop/a": 0, "loop/b": 0}, (codes, _live_logs(world, home, a, b))
    # main has both loops' slice commits and both land records; every
    # retired sha is an ancestor of main.
    subjects = git(home, "log", "--format=%s", "main").splitlines()
    assert any(s.startswith("slice(a-one)") for s in subjects)
    assert any(s.startswith("slice(b-one)") for s in subjects)
    assert "loop: land loop/a (iteration 1)" in subjects
    assert "loop: land loop/b (iteration 1)" in subjects
    for spec in (a, b):
        for sha in _retired_shas(spec["root_box"]):
            git(home, "merge-base", "--is-ancestor", sha, "main")
    # The second lander merged the moved target and re-checked it.
    logs = {s["rel"]: (s["root_box"] / "LOG.md").read_text() for s in (a, b)}
    merged = [rel for rel, text in logs.items() if "merged home:main@" in text]
    assert len(merged) == 1, logs
    assert "full_check PASS" in logs[merged[0]]
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert "trio/" not in git(home, "branch", "--list")
    # No Trio session at the root; each loop's sessions stayed in its own trees.
    for event in world.events:
        assert Path(event["workspace"]) != home
        other = "loop/b" if event["loop"] == "loop/a" else "loop/a"
        assert f"lead-{other.replace('/', '--')}" not in event["workspace"]


def test_user_edit_at_root_blocks_land_then_land_resumes(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    (home / "notes.txt").write_text("mine, untracked\n")
    (home / "README.md").write_text("home, edited by the user\n")

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        # While the loop runs, the user creates the very file the loop adds.
        (home / "src" / "a.py").write_text("user's own a.py\n")
        return False

    world.hooks["integration-eval"] = integration
    before_main = git(home, "rev-parse", "main")
    code = world.run_loop(spec)
    assert code == 8
    record = world.rf.load_record(world.wt, home, "loop--a")
    live = Path(record["live_mailbox"])
    state = (live / "STATE.md").read_text()
    assert "status: needs_land" in state and "phase: land-blocked" in state
    assert "src/a.py" in (live / "LOG.md").read_text()
    # Nothing at the root changed: main, the user's files, the pre-seed copies.
    assert git(home, "rev-parse", "main") == before_main
    assert (home / "src/a.py").read_text() == "user's own a.py\n"
    assert (home / "notes.txt").is_file() and (home / "loop/a/GOAL.md").is_file()
    assert Path(record["path"]).is_dir()
    # The user moves the file away; `trioctl omnigent land` finishes the job.
    (home / "src/a.py").unlink()
    world.hooks.clear()
    assert world.run_land(spec) == 0
    assert (home / "src/a.py").read_text() == "# a-one\n"
    assert (home / "notes.txt").read_text() == "mine, untracked\n"
    assert (home / "README.md").read_text() == "home, edited by the user\n"
    assert "status: shipped" in (home / "loop/a/STATE.md").read_text()
    assert not Path(record["path"]).exists()
    # No Lead pass ran for the land (only the first run's single Lead pass).
    assert sum(1 for e in world.events if e["kind"] == "lead-pass") == 1


def test_r15_guard_refuses_before_any_lead_worktree_exists(world, tmp_path, capsys):
    home = _home(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": f"{outside}/x.py"}])
    before = snapshot_root(home)
    assert world.run_loop(spec) == 3
    assert "writes outside the mailbox repo" in capsys.readouterr().err
    assert "trio/" not in git(home, "branch", "--list")
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert snapshot_root(home) == before

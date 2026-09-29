"""Live-probe round 2 blockers on real git: build artefacts (A), the
conflict re-dispatch cleanup (B), the needs_retirement finish (D)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from test_step_ops import (git, git_env, mbox, repo, retire,  # noqa: F401
                           step, to_lead_done)
from test_waves import builder_branch, lead_running

ARTEFACTS = ("__pycache__/", "*.py[cod]", ".pytest_cache/")


def pytest_droppings(root: Path, name: str) -> None:
    """What `python3 -m pytest` leaves behind in a checkout (probe 2)."""
    for rel in (f"__pycache__/{name}.cpython-312.pyc",
                "tests/__pycache__/__init__.cpython-312.pyc",
                f"tests/__pycache__/test_{name}.cpython-312-pytest-9.1.1.pyc",
                ".pytest_cache/v/cache/nodeids", "stray.pyc"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk\n")


def exclude_lines(repo: Path) -> list[str]:
    path = Path(git(repo, "rev-parse", "--path-format=absolute",
                    "--git-path", "info/exclude"))
    return path.read_text().splitlines()


# ---------------------------------------------------------- A: begin
def test_begin_excludes_build_artefacts_once(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    assert step(repo, "begin")["ok"]
    lines = exclude_lines(repo)
    for entry in (".claude/worktrees/", *ARTEFACTS):
        assert lines.count(entry) == 1, (entry, lines)
    assert "node_modules/" not in lines
    pytest_droppings(repo, "app")
    status = git(repo, "status", "--porcelain", "--untracked-files=all")
    assert status == "?? loop/.gitignore", status  # begin's runtime ignores


def test_exclude_covers_linked_worktrees(repo: Path) -> None:
    step(repo, "begin")
    wt = repo / ".claude" / "worktrees" / "b1"
    git(repo, "worktree", "add", "-q", "-b", "worktree-b1", str(wt), "HEAD")
    pytest_droppings(wt, "b1")
    assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""


# -------------------------------------------------- A: cleanup (9/9 kept)
def test_cleanup_removes_merged_worktrees_with_pytest_droppings(
        repo: Path) -> None:
    """Probe 2 P1: every merged builder worktree was kept for .pyc dirt."""
    lead_running(repo)
    b1 = builder_branch(repo, "b1")
    b2 = builder_branch(repo, "b2")
    for b in (b1, b2):
        pytest_droppings(Path(b["worktree"]), b["id"])
        git(repo, "merge", "--no-ff", "--no-edit", "-q", b["branch"])
    out = step(repo, "cleanup", branches="worktree-b1,worktree-b2")
    assert out["ok"] and out["kept"] == [], out
    assert {r["branch"] for r in out["removed"]} == {"worktree-b1",
                                                     "worktree-b2"}
    assert not Path(b1["worktree"]).exists()
    assert not Path(b2["worktree"]).exists()
    assert "worktree-b" not in git(repo, "branch")
    assert step(repo, "end")["dangling_worktrees"] == []


def test_cleanup_forces_unexcluded_artefacts_but_never_product(
        repo: Path) -> None:
    """Without the exclude lines (a repo whose info/exclude was reset), an
    untracked artefact is still force-removable; a tracked change or any
    other untracked file keeps the worktree."""
    lead_running(repo)
    path = Path(git(repo, "rev-parse", "--path-format=absolute",
                    "--git-path", "info/exclude"))
    path.write_text(".claude/worktrees/\n")
    b1 = builder_branch(repo, "b1")
    b2 = builder_branch(repo, "b2")
    b3 = builder_branch(repo, "b3")
    pytest_droppings(Path(b1["worktree"]), "b1")
    pytest_droppings(Path(b2["worktree"]), "b2")
    (Path(b2["worktree"]) / "notes.txt").write_text("wip\n")  # unignored
    pytest_droppings(Path(b3["worktree"]), "b3")
    (Path(b3["worktree"]) / "b3.py").write_text("x = 2\n")    # tracked edit
    for b in (b1, b2, b3):
        git(repo, "merge", "--no-ff", "--no-edit", "-q", b["branch"])
    out = step(repo, "cleanup",
               branches="worktree-b1,worktree-b2,worktree-b3")
    kept = {k["branch"]: k["reason"] for k in out["kept"]}
    assert [r["branch"] for r in out["removed"]] == ["worktree-b1"]
    assert "notes.txt" in kept["worktree-b2"]
    assert "__pycache__" not in kept["worktree-b2"]
    assert "b3.py" in kept["worktree-b3"]
    assert Path(b2["worktree"], "notes.txt").exists()
    assert Path(b3["worktree"], "b3.py").read_text() == "x = 2\n"


# ------------------------------------------ A: SHIP retirement (p2, p4)
def test_ship_with_root_pytest_droppings_is_shipped_and_clean(
        repo: Path) -> None:
    """Probe 2 P2/P4: a role ran pytest in the root checkout, and the SHIP
    stopped at needs_retirement ("untracked product paths")."""
    step(repo, "begin")
    p = to_lead_done(repo)
    pytest_droppings(repo, "app")
    git(repo, "add", "loop")
    retire(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["status"] == "shipped" and a["code"] == 0, a
    assert a["retirement_fold"] == "amended"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "__pycache__" / "app.cpython-312.pyc").exists()


def test_ship_still_refuses_untracked_product_file(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    (repo / "helper.py").write_text("x = 1\n")  # real, untracked product
    git(repo, "add", "loop")
    retire(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["status"] == "needs_retirement" and a["code"] == 6
    assert "helper.py" in (mbox(repo) / "LOG.md").read_text()


def test_launcher_disables_bytecode() -> None:
    text = (Path(__file__).resolve().parents[1] / "launch.sh").read_text()
    assert "export PYTHONDONTWRITEBYTECODE=1" in text


# ------------------------------------- B: superseded branch after re-dispatch
def conflicting_wave(repo: Path) -> tuple[dict, dict]:
    """alpha and beta both append to registry.py (probe 2 P3)."""
    (repo / "registry.py").write_text("ENTRIES = ['core']\n")
    git(repo, "add", "registry.py")
    git(repo, "commit", "-q", "-m", "registry")
    lead_running(repo)
    alpha = builder_branch(repo, "alpha", files={
        "alpha.py": "A = 1\n", "registry.py": "ENTRIES = ['core', 'alpha']\n"})
    beta = builder_branch(repo, "beta", files={
        "beta.py": "B = 1\n", "registry.py": "ENTRIES = ['core', 'beta']\n"})
    git(repo, "merge", "--no-ff", "--no-edit", "-q", alpha["branch"])
    merge = subprocess_git(repo, "merge", "--no-ff", "--no-edit", beta["branch"])
    assert merge.returncode != 0  # conflict
    git(repo, "merge", "--abort")
    return alpha, beta


def subprocess_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=git_env())


def test_cleanup_drops_conflicted_branch_once_redispatch_merged(
        repo: Path) -> None:
    alpha, beta = conflicting_wave(repo)
    first = step(repo, "cleanup", branches="worktree-alpha,worktree-beta")
    assert [k["branch"] for k in first["kept"]] == ["worktree-beta"]
    assert first["kept"][0]["reason"] == "not merged into HEAD"
    # the re-dispatched builder forks from the post-merge HEAD
    beta2 = builder_branch(repo, "beta2", files={
        "beta.py": "B = 1\n",
        "registry.py": "ENTRIES = ['core', 'alpha', 'beta']\n"})
    # not merged yet: the old branch is kept
    early = step(repo, "cleanup",
                 drop_unmerged="worktree-beta=worktree-beta2")
    assert early["dropped"][0]["dropped"] is False
    assert "not merged" in early["dropped"][0]["reason"]
    assert Path(beta["worktree"]).exists()
    git(repo, "merge", "--no-ff", "--no-edit", "-q", beta2["branch"])
    out = step(repo, "cleanup", branches="worktree-beta2",
               drop_unmerged="worktree-beta=worktree-beta2")
    assert out["ok"] and out["kept"] == []
    assert out["dropped"] == [{
        "branch": "worktree-beta", "superseded_by": "worktree-beta2",
        "dropped": True, "worktree": beta["worktree"],
        "tip": beta["head"]}]
    assert not Path(beta["worktree"]).exists()
    assert not Path(beta2["worktree"]).exists()
    assert "worktree-beta" not in git(repo, "branch")
    assert step(repo, "end")["dangling_worktrees"] == []
    assert alpha  # merged and removed by the first cleanup
    assert not Path(alpha["worktree"]).exists()


def test_drop_unmerged_never_touches_non_builder_branches(repo: Path) -> None:
    lead_running(repo)
    git(repo, "branch", "feature")
    out = step(repo, "cleanup", drop_unmerged="feature")
    assert out["dropped"][0]["dropped"] is False
    assert "not a builder branch" in out["dropped"][0]["reason"]
    assert "feature" in git(repo, "branch")
    own = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    out = step(repo, "cleanup", drop_unmerged=own)
    assert out["dropped"][0]["dropped"] is False
    assert own in git(repo, "branch")


def test_drop_unmerged_keeps_worktree_with_product_dirt(repo: Path) -> None:
    lead_running(repo)
    old = builder_branch(repo, "old", extra_dirt=True)
    out = step(repo, "cleanup", drop_unmerged="worktree-old")
    assert out["dropped"][0]["dropped"] is False
    assert "scratch.txt" in out["dropped"][0]["reason"]
    assert Path(old["worktree"], "scratch.txt").exists()


# --------------------------------------- D: finish from needs_retirement
def test_next_finishing_needs_retirement_reports_shas_and_fold(
        repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    git(repo, "add", "loop")
    (repo / "helper.py").write_text("x = 1\n")  # blocks retirement
    retire(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["status"] == "needs_retirement"
    (repo / "helper.py").unlink()  # the operator removes it, then starts
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "stop" and n["status"] == "shipped", n
    assert n["code"] == 0 and n["commit_shas"] == [p["sha"]]
    assert n["retirement_fold"] == "amended" and n["human_check"] is None
    assert git(repo, "status", "--porcelain") == ""
    again = step(repo, "next", max_iterations=4)
    assert again["commit_shas"] == [p["sha"]]
    assert again["retirement_fold"] is None  # nothing finalized this time

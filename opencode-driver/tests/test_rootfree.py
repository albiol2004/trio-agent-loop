"""rootfree.py — the trio-opencode Lead worktree: prepare/land/teardown/abandon
and loop_slug parity with metrics/trio-metrics.py."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from trio_opencode import rootfree, steplib

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-opencode-test", GIT_COMMITTER_EMAIL="t@example.test",
    )
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=_git_env(),
    ).stdout.strip()


def mbox(repo: Path) -> Path:
    return repo / "loop"


def other_repo(tmp_path: Path, name: str = "repo-b") -> Path:
    """A second, independent git repo (a PLAN.md ``repos:`` declared repo),
    outside the home repo entirely -- the non-nested aggregate case."""
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("b\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "init b")
    return root


def declare(name: str, path: Path, base: str | None = None) -> dict:
    return {"name": name, "path": str(path), "base": base}


# ----------------------------------------------------------------- loop_slug
def test_loop_slug_matches_trio_metrics():
    path = REPO_ROOT / "metrics" / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("trio_metrics_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = ["loop", "loop/scenario-basis-and-months", "loop/a.b c", "",
             "a" * 200, "loop//weird///path", "loop/ünïcode"]
    for case in cases:
        assert rootfree.loop_slug(case) == module.loop_slug(case), case


# ------------------------------------------------------------------- prepare
def test_prepare_creates_worktree_and_seed_commit(git_repo: Path) -> None:
    (mbox(git_repo) / "GOAL.md").write_text("# Goal\nShip app.py\nUpdated.\n",
                                            encoding="utf-8")
    record = rootfree.prepare(mbox(git_repo))
    assert record.branch == "trio/loop"
    assert record.target == "main"
    assert Path(record.path).is_dir()
    assert record.seed, "expected a seed commit sha"
    live_goal = record.live_mailbox / "GOAL.md"
    assert live_goal.read_text() == "# Goal\nShip app.py\nUpdated.\n"

    # A git worktree really was created on that branch.
    trees = git(git_repo, "worktree", "list", "--porcelain")
    assert record.path in trees and "trio/loop" in trees

    # The record file lives under the repo's common git dir.
    rp = rootfree.record_path(git_repo, "loop")
    assert rp.is_file() and rp.parent.name == "trio-opencode"


def test_prepare_reattaches_to_the_same_worktree(git_repo: Path) -> None:
    first = rootfree.prepare(mbox(git_repo))
    (mbox(git_repo) / "GOAL.md").write_text("# Goal\nShip app.py\nSecond edit.\n",
                                            encoding="utf-8")
    second = rootfree.prepare(mbox(git_repo))
    assert second.path == first.path
    assert second.branch == first.branch
    assert (second.live_mailbox / "GOAL.md").read_text().endswith("Second edit.\n")
    trees = git(git_repo, "worktree", "list", "--porcelain")
    assert trees.count("worktree " + first.path) == 1


def test_reattach_keeps_live_loop_state_and_syncs_only_human_inputs(git_repo: Path) -> None:
    """Resume must never reset the live STATE/LOG from the root snapshot."""
    first = rootfree.prepare(mbox(git_repo))
    live = first.live_mailbox
    (live / "STATE.md").write_text("iteration: 1\nstatus: running\nphase: lead-running\n",
                                   encoding="utf-8")
    (live / "LOG.md").write_text("# log\n- iter 1 | builder | a: done\n", encoding="utf-8")
    (live / "HUMAN.md").write_text("answer one\n", encoding="utf-8")
    (mbox(git_repo) / "HUMAN.md").write_text("stale root copy\n", encoding="utf-8")
    second = rootfree.prepare(mbox(git_repo))
    assert "phase: lead-running" in (second.live_mailbox / "STATE.md").read_text()
    assert "builder | a" in (second.live_mailbox / "LOG.md").read_text()
    # A root HUMAN.md that is not an append-only extension never clobbers.
    assert (second.live_mailbox / "HUMAN.md").read_text() == "answer one\n"
    (mbox(git_repo) / "HUMAN.md").write_text("answer one\nanswer two\n", encoding="utf-8")
    third = rootfree.prepare(mbox(git_repo))
    assert (third.live_mailbox / "HUMAN.md").read_text().endswith("answer two\n")


def test_prepare_refuses_when_branch_exists_without_a_record(git_repo: Path) -> None:
    git(git_repo, "branch", "trio/loop")
    with pytest.raises(rootfree.RootFreeError, match="already exists"):
        rootfree.prepare(mbox(git_repo))


def test_prepare_refuses_detached_head(git_repo: Path) -> None:
    sha = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", sha)
    with pytest.raises(rootfree.RootFreeError, match="detached"):
        rootfree.prepare(mbox(git_repo))


# ---------------------------------------------------------------------- land
def test_land_ff_when_target_checked_out_and_clean(git_repo: Path) -> None:
    record = rootfree.prepare(mbox(git_repo))
    (Path(record.path) / "product.txt").write_text("built\n", encoding="utf-8")
    git(Path(record.path), "add", "product.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(x): build product")
    tip = git(Path(record.path), "rev-parse", "HEAD")

    result = rootfree.land(record)
    assert result == {"status": "landed", "phase": "ff-merged", "detail": None,
                      "tip": tip, "target": "main"}
    assert git(git_repo, "rev-parse", "HEAD") == tip
    assert (git_repo / "product.txt").read_text() == "built\n"

    # idempotent: landing again reports already-landed.
    again = rootfree.land(record)
    assert again["status"] == "landed" and again["phase"] == "already-landed"


def test_land_via_update_ref_when_target_checked_out_nowhere(git_repo: Path) -> None:
    record = rootfree.prepare(mbox(git_repo))
    git(git_repo, "checkout", "-q", "-b", "scratch")  # main now checked out nowhere
    (Path(record.path) / "product.txt").write_text("built\n", encoding="utf-8")
    git(Path(record.path), "add", "product.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(x): build product")
    tip = git(Path(record.path), "rev-parse", "HEAD")

    result = rootfree.land(record)
    assert result["status"] == "landed" and result["phase"] == "update-ref"
    assert git(git_repo, "rev-parse", "refs/heads/main") == tip


def test_needs_land_diverged(git_repo: Path) -> None:
    record = rootfree.prepare(mbox(git_repo))
    (Path(record.path) / "trio-side.txt").write_text("a\n", encoding="utf-8")
    git(Path(record.path), "add", "trio-side.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(x): trio side")

    (git_repo / "root-side.txt").write_text("b\n", encoding="utf-8")
    git(git_repo, "add", "root-side.txt")
    git(git_repo, "commit", "-q", "-m", "unrelated root work")

    result = rootfree.land(record)
    assert result["status"] == "needs_land" and result["phase"] == "diverged"
    assert "trio-opencode land" in result["detail"] or "rebase" in result["detail"]


def test_needs_land_blocked_on_overlapping_local_changes(git_repo: Path) -> None:
    (git_repo / "conflict.txt").write_text("root\n", encoding="utf-8")
    git(git_repo, "add", "conflict.txt")
    git(git_repo, "commit", "-q", "-m", "add conflict.txt")

    record = rootfree.prepare(mbox(git_repo))
    (Path(record.path) / "conflict.txt").write_text("trio-side\n", encoding="utf-8")
    git(Path(record.path), "add", "conflict.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(x): edit conflict.txt")

    # Uncommitted, overlapping local edit in the target checkout.
    (git_repo / "conflict.txt").write_text("dirty local edit\n", encoding="utf-8")

    result = rootfree.land(record)
    assert result["status"] == "needs_land" and result["phase"] == "land-blocked"
    assert result["detail"]
    # Never force, never reset: the local edit survives untouched.
    assert (git_repo / "conflict.txt").read_text() == "dirty local edit\n"


# ------------------------------------------------------------------ teardown
def test_teardown_after_landed_removes_worktree_and_branch(git_repo: Path) -> None:
    # A real run's mailbox .gitignore (from steplib's MAILBOX_RUNTIME_IGNORES)
    # already ignores runtime sidecars; commit it before preparing, so the
    # Lead worktree forks with nothing left to seed and its tip starts out
    # identical (already landed) to main's.
    (mbox(git_repo) / ".gitignore").write_text(".session.json\n", encoding="utf-8")
    git(git_repo, "add", "loop/.gitignore")
    git(git_repo, "commit", "-q", "-m", "loop: ignore runtime sidecars")

    record = rootfree.prepare(mbox(git_repo))
    assert not record.seed, "nothing left to seed: no divergent commit to land"
    (record.live_mailbox / ".session.json").write_text('{"ok": true}\n', encoding="utf-8")

    landed = rootfree.land(record)
    assert landed["status"] == "landed" and landed["phase"] == "already-landed"

    out = rootfree.teardown(record)
    assert out["worktree_removed"] is True
    assert out["branch_deleted"] is True
    assert ".session.json" in out["runtime_copied"]
    assert (mbox(git_repo) / ".session.json").read_text() == '{"ok": true}\n'
    assert not Path(record.path).exists()
    assert "trio/loop" not in git(git_repo, "branch")

    reloaded = rootfree.load_record(git_repo, "loop")
    assert reloaded.landed is True and reloaded.landed_at


# ------------------------------------------------------------------- abandon
def test_abandon_keeps_branch_removes_clean_worktree(git_repo: Path) -> None:
    record = rootfree.prepare(mbox(git_repo))
    tip = git(Path(record.path), "rev-parse", "HEAD")
    out = rootfree.abandon(record)
    assert out["worktree_removed"] is True
    assert out["branch_kept"] == "trio/loop"
    assert out["tip"] == tip
    assert not Path(record.path).exists()
    assert "trio/loop" in git(git_repo, "branch")


def test_abandon_refuses_dirty_worktree_without_force(git_repo: Path) -> None:
    record = rootfree.prepare(mbox(git_repo))
    (Path(record.path) / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(rootfree.RootFreeError):
        rootfree.abandon(record)
    assert Path(record.path).exists()
    out = rootfree.abandon(record, force=True)
    assert out["worktree_removed"] is True


# ------------------------------------------------------- multi-repo aggregates
# api:RootFreeAggregates: a PLAN.md `repos:` declared repo gets its own
# aggregate worktree on the SAME `trio/<slug>` branch as home.

def test_prepare_creates_aggregate_and_map(git_repo: Path, tmp_path: Path) -> None:
    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])

    assert set(record.repos) == {"b"}
    info = record.repos["b"]
    assert info["main"] == str(repo_b)
    assert info["branch"] == "trio/loop"
    assert info["target_ref"] == "main"
    assert info["nested"] is False
    # Non-nested: the aggregate lives under repo_b's OWN git-common-dir
    # (".git/trio-opencode/agg-<slug>"), never under the Lead worktree.
    assert not info["path"].startswith(str(record.path))
    assert "trio-opencode" in info["path"] and info["path"].endswith("agg-loop")
    agg_path = Path(info["path"])
    assert agg_path.is_dir()

    trees = git(repo_b, "worktree", "list", "--porcelain")
    assert str(agg_path) in trees and "trio/loop" in trees

    map_path = rootfree.aggregates_map_path(record.live_mailbox)
    assert map_path == record.live_mailbox / ".sessions" / "aggregates.json"
    assert map_path.is_file()
    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert data == {
        "schema": 1,
        "repos": {"b": {"path": str(agg_path), "branch": "trio/loop", "main": str(repo_b)}},
    }


def test_prepare_nested_declared_repo_aggregates_under_lead_worktree(git_repo: Path) -> None:
    # A declared repo that is a sub-repo of the HOME repo's own worktree
    # tree (its path resolves inside the home repo): its aggregate lives at
    # <lead worktree>/<rel>, not at its own git-common-dir.
    sub = git_repo / "vendor" / "nested"
    sub.mkdir(parents=True)
    git(sub, "init", "-q", "-b", "main")
    (sub / "README").write_text("n\n", encoding="utf-8")
    git(sub, "add", "README")
    git(sub, "commit", "-q", "-m", "init nested")

    record = rootfree.prepare(mbox(git_repo), declared=[declare("nested", sub)])
    info = record.repos["nested"]
    assert info["nested"] is True
    assert Path(info["path"]) == Path(record.path) / "vendor" / "nested"
    assert Path(info["path"]).is_dir()


def test_prepare_resume_reattaches_aggregate_without_recreating_branch(
    git_repo: Path, tmp_path: Path,
) -> None:
    repo_b = other_repo(tmp_path)
    first = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    agg_path = Path(first.repos["b"]["path"])
    (agg_path / "work.txt").write_text("builder work\n", encoding="utf-8")
    git(agg_path, "add", "work.txt")
    git(agg_path, "commit", "-q", "-m", "slice(x): builder work")
    tip = git(agg_path, "rev-parse", "HEAD")

    second = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    assert second.repos["b"]["path"] == first.repos["b"]["path"]
    assert second.repos["b"]["branch"] == "trio/loop"
    # Re-attach never recreated the branch or discarded the builder commit.
    assert git(agg_path, "rev-parse", "HEAD") == tip
    trees = git(repo_b, "worktree", "list", "--porcelain")
    assert trees.count("worktree " + str(agg_path)) == 1

    # Repair: the aggregate worktree directory goes missing (its branch
    # survives) -- resume re-creates it on the SAME branch, keeping the tip.
    git(repo_b, "worktree", "remove", "--force", str(agg_path))
    assert not agg_path.exists()
    third = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    assert agg_path.is_dir()
    assert git(agg_path, "rev-parse", "HEAD") == tip
    assert third.repos["b"]["path"] == first.repos["b"]["path"]


def test_prepare_detached_declared_repo_without_base_leaves_nothing_half_made(
    git_repo: Path, tmp_path: Path,
) -> None:
    repo_b = other_repo(tmp_path)
    sha = git(repo_b, "rev-parse", "HEAD")
    git(repo_b, "checkout", "-q", sha)  # detached HEAD, no `base:` declared

    with pytest.raises(rootfree.RootFreeError, match="detached"):
        rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])

    # Nothing half-made: no Lead worktree, no record, no branch anywhere.
    assert "trio/loop" not in git(git_repo, "worktree", "list", "--porcelain")
    assert "trio/loop" not in git(git_repo, "branch")
    assert rootfree.load_record(git_repo, "loop") is None
    assert "trio/loop" not in git(repo_b, "branch")


def test_land_declared_repo_ffs_before_home(git_repo: Path, tmp_path: Path) -> None:
    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])

    agg_b = Path(record.repos["b"]["path"])
    (agg_b / "built.txt").write_text("b work\n", encoding="utf-8")
    git(agg_b, "add", "built.txt")
    git(agg_b, "commit", "-q", "-m", "slice(b): build")
    tip_b = git(agg_b, "rev-parse", "HEAD")

    (Path(record.path) / "built.txt").write_text("home work\n", encoding="utf-8")
    git(Path(record.path), "add", "built.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(home): build")
    tip_home = git(Path(record.path), "rev-parse", "HEAD")

    result = rootfree.land(record)
    assert result["status"] == "landed"
    assert result["repos"]["b"] == {"status": "landed", "phase": "ff-merged", "sha": tip_b}
    assert result["repos"]["home"]["status"] == "landed"
    assert git(repo_b, "rev-parse", "HEAD") == tip_b
    assert (repo_b / "built.txt").read_text() == "b work\n"
    assert git(git_repo, "rev-parse", "HEAD") == tip_home

    # Idempotent retry: both already landed.
    again = rootfree.land(record)
    assert again["status"] == "landed"
    assert again["repos"]["b"]["phase"] == "already-landed"


def test_land_diverged_declared_repo_blocks_home(git_repo: Path, tmp_path: Path) -> None:
    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])

    agg_b = Path(record.repos["b"]["path"])
    (agg_b / "b-side.txt").write_text("a\n", encoding="utf-8")
    git(agg_b, "add", "b-side.txt")
    git(agg_b, "commit", "-q", "-m", "slice(b): b side")

    # repo_b diverges from under the aggregate (unrelated work on main).
    (repo_b / "root-side.txt").write_text("c\n", encoding="utf-8")
    git(repo_b, "add", "root-side.txt")
    git(repo_b, "commit", "-q", "-m", "unrelated work on repo b")

    # Home has its own landable work, but it must NOT land while b is stuck.
    (Path(record.path) / "home.txt").write_text("d\n", encoding="utf-8")
    git(Path(record.path), "add", "home.txt")
    git(Path(record.path), "commit", "-q", "-m", "slice(home): home work")
    home_tip_before = git(git_repo, "rev-parse", "HEAD")

    result = rootfree.land(record)
    assert result["status"] == "needs_land"
    assert result["repos"]["b"]["status"] == "needs_land"
    assert "home" not in result["repos"]
    assert "b" in result["detail"]
    assert git(git_repo, "rev-parse", "HEAD") == home_tip_before


def test_teardown_removes_aggregate_worktree_and_map(git_repo: Path, tmp_path: Path) -> None:
    # Ignore the aggregates-map runtime dir so it never shows the Lead
    # worktree as dirty (same convention as
    # test_teardown_after_landed_removes_worktree_and_branch above).
    (mbox(git_repo) / ".gitignore").write_text(".sessions/\n", encoding="utf-8")
    git(git_repo, "add", "loop/.gitignore")
    git(git_repo, "commit", "-q", "-m", "loop: ignore runtime sidecars")

    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    agg_b = Path(record.repos["b"]["path"])

    landed = rootfree.land(record)
    assert landed["status"] == "landed"

    out = rootfree.teardown(record)
    assert out["worktree_removed"] is True
    assert out["branch_deleted"] is True
    assert out["repos"]["b"]["worktree_removed"] is True
    assert out["repos"]["b"]["branch_deleted"] is True
    assert not agg_b.exists()
    assert "trio/loop" not in git(repo_b, "branch")
    assert not rootfree.aggregates_map_path(record.live_mailbox).exists()


def test_abandon_keeps_aggregate_branch(git_repo: Path, tmp_path: Path) -> None:
    (mbox(git_repo) / ".gitignore").write_text(".sessions/\n", encoding="utf-8")
    git(git_repo, "add", "loop/.gitignore")
    git(git_repo, "commit", "-q", "-m", "loop: ignore runtime sidecars")

    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    agg_b = Path(record.repos["b"]["path"])
    tip_b = git(agg_b, "rev-parse", "HEAD")

    out = rootfree.abandon(record)
    assert out["worktree_removed"] is True
    assert out["repos"]["b"] == {"worktree_removed": True, "branch_kept": "trio/loop", "tip": tip_b}
    assert not agg_b.exists()
    assert "trio/loop" in git(repo_b, "branch")
    assert not rootfree.aggregates_map_path(record.live_mailbox).exists()


def test_read_repos_resolves_declared_repo_to_the_aggregate(
    git_repo: Path, tmp_path: Path,
) -> None:
    """The shared reader (``metrics/trio-metrics.py:read_repos``, loaded via
    ``steplib.TL._METRICS``) must resolve a PLAN.md-declared repo to this
    run's aggregate, not the declared checkout."""
    repo_b = other_repo(tmp_path)
    record = rootfree.prepare(mbox(git_repo), declared=[declare("b", repo_b)])
    agg_b = Path(record.repos["b"]["path"])

    plan = record.live_mailbox / "PLAN.md"
    plan.write_text(
        "# Plan\n```yaml\nrepos:\n  - name: b\n    path: " + str(repo_b) + "\n```\n",
        encoding="utf-8",
    )

    read_repos = steplib.TL._METRICS.read_repos
    info = read_repos(record.live_mailbox)
    assert info["declared"] is True
    assert not info["errors"]
    [repo] = [r for r in info["repos"] if r["name"] == "b"]
    assert Path(repo["path"]) == agg_b
    assert Path(repo["main_path"]).resolve() == repo_b.resolve()
    assert repo["base"] == "trio/loop"

"""eval-native-v0c low findings C1 (porcelain -z parsing) and C2 (cleanup /
builders marker + branch-ownership checks)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from test_step_ops import git, git_env, mbox, repo, step  # noqa: F401
from test_waves import builder_branch, lead_running, owned_wave

NATIVE = Path(__file__).resolve().parents[1]
HELPER = NATIVE / "trio_native_step.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("tns_v0c", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TNS = _load_helper()


# --------------------------------------------------------------------- C1
def test_dirty_entries_keeps_full_untracked_name_with_arrow_literal(
        repo: Path) -> None:
    """C1: an untracked file literally named 'notes -> old.pyc' must be
    parsed as that one, whole path — not truncated to 'old.pyc' by a naive
    split on ' -> ' (porcelain v1's non-`-z` text form quotes exactly this
    filename, to disambiguate it from its own rename-record syntax)."""
    name = "notes -> old.pyc"
    (repo / name).write_text("real product content\n", encoding="utf-8")
    entries = TNS._dirty_entries(str(repo))
    assert entries is not None
    paths = [p for _c, p in entries]
    assert name in paths
    assert "old.pyc" not in paths


def test_dirty_entries_splits_rename_only_for_r_code(repo: Path) -> None:
    """C1: the two-field record (current path + original path) exists only
    for R/C status codes; an ordinary status line is never split."""
    git(repo, "mv", "README", "notes -> renamed.md")
    entries = TNS._dirty_entries(str(repo))
    assert entries == [("R ", "notes -> renamed.md")]


def test_dirty_entries_untracked_plain_arrow_name_not_split_as_rename(
        repo: Path) -> None:
    """A second, independent untracked file also containing ' -> ' sits
    alongside real dirt; both must be reported as their own whole names."""
    (repo / "a -> b.txt").write_text("x\n", encoding="utf-8")
    (repo / "plain.txt").write_text("y\n", encoding="utf-8")
    entries = TNS._dirty_entries(str(repo))
    codes_paths = sorted(entries)
    assert ("??", "a -> b.txt") in codes_paths
    assert ("??", "plain.txt") in codes_paths
    assert len(entries) == 2


# --------------------------------------------------------------------- C2
def test_cleanup_never_removes_worktree_outside_marker(repo: Path) -> None:
    """C2: op_cleanup had no `.claude/worktrees/` marker check of its own
    (only `_drop_superseded` did); a merged branch checked out in a user
    worktree elsewhere, with only __pycache__ dirt, must survive."""
    lead_running(repo)
    other = repo.parent / "user-feature"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(other), "HEAD")
    (other / "__pycache__").mkdir()
    (other / "__pycache__" / "x.pyc").write_text("junk\n", encoding="utf-8")
    out = step(repo, "cleanup", branches="feature")
    assert out["ok"]
    kept = {k["branch"]: k["reason"] for k in out["kept"]}
    assert "feature" in kept and "not under" in kept["feature"]
    assert other.exists()
    assert "feature" in git(repo, "branch")


def test_cleanup_marker_check_resolves_symlinked_marker_dir(
        repo: Path) -> None:
    """C2: the marker check uses os.path.realpath, so a worktree really
    created under `.claude/worktrees/` is still removed when that directory
    itself is reached through a symlink (realpath resolves both sides to
    the same location — this is a robustness check, not a bypass)."""
    real_storage = repo.parent / "real-worktree-storage"
    real_storage.mkdir()
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    os.symlink(real_storage, repo / ".claude" / "worktrees")
    lead_running(repo)
    b1 = owned_wave(repo, {"b1": {"loop_residue": False}})["b1"]
    wt = Path(b1["worktree"])
    git(repo, "merge", "--no-ff", "--no-edit", "-q", b1["branch"])
    out = step(repo, "cleanup", branches=b1["branch"])
    assert out["ok"] and [r["branch"] for r in out["removed"]] == [
        b1["branch"]]
    assert not wt.exists()


def test_builders_refuses_branch_not_own_worktree(repo: Path) -> None:
    """C2: `builders` ties a reported branch to the builder's own worktree
    (the worktree `git worktree list --porcelain` shows for that path must
    really be on that branch) — a builder reporting another branch (one
    that exists and contains the Lead's HEAD) is refused."""
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    real = builder_branch(repo, "real")
    other = builder_branch(repo, "other")
    liar = dict(other, branch="worktree-real")  # claims real's branch
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([liar]))
    assert out["accepted"] == []
    reason = out["refused"][0]["reason"]
    assert "not the builder's own worktree" in reason
    assert real and other  # both built successfully; only the lie is refused


def test_builders_refuses_missing_worktree_field(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b = builder_branch(repo, "b1")
    no_worktree = dict(b)
    no_worktree.pop("worktree")
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([no_worktree]))
    assert out["accepted"] == []
    assert "no worktree reported" in out["refused"][0]["reason"]

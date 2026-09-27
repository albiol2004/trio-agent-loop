"""r11 F2: the dirty-checkout gate of an isolated dispatch on a real repo.

`aggregate_blockers` / `create` classify aggregate status entries outside
this mailbox:

(i)   modified/untracked files under a declared product path (any PLAN.md
      slice's `writes:`) -> blocker;
(ii)  other untracked files, and any other Trio mailbox directory (GOAL.md +
      STATE.md) -> not a blocker, listed in one stderr note;
(iii) modified TRACKED files outside the product paths -> blocker whose
      message tells the user to commit/stash THEIR change and the Lead not
      to commit files it did not edit.

Offline, real git scratch repos.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from test_worker_worktrees import GIT_ENV, MODULE, SCRIPT, _load, git

PLAN = """# Plan

```yaml
slices:
  - id: api
    writes: [src/app.py, "api:Thing"]
    reads: []
  - id: docs
    writes:
      - docs/
    reads: []
```
"""


@pytest.fixture()
def wt(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return _load("worker_worktrees_r11_under_test", MODULE)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "loop").mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / "docs" / "guide.md").write_text("guide\n")
    (repo / "README.md").write_text("readme\n")
    (repo / "loop" / "PLAN.md").write_text(PLAN)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "worktrees"


def second_mailbox(repo: Path) -> Path:
    other = repo / "loop-auth"
    other.mkdir()
    (other / "GOAL.md").write_text("# other mission\n")
    (other / "STATE.md").write_text("status: running\n")
    (other / "LOG.md").write_text("- iter 1 | lead | x\n")
    return other


def test_declared_product_paths_parse_flow_and_block_lists(wt, repo):
    assert wt.declared_product_paths(repo / "loop") == ["src/app.py", "docs"]
    assert wt.declared_product_paths(None) == []


def test_i_product_path_changes_block(wt, repo, root):
    (repo / "src" / "app.py").write_text("x = 2\n")  # modified, declared
    (repo / "docs" / "new.md").write_text("new\n")  # untracked, under docs/
    found = wt.classify_aggregate(repo, repo / "loop")
    assert found["product"] == [" M src/app.py", "?? docs/new.md"]
    assert found["foreign"] == [] and found["ignored"] == []
    with pytest.raises(wt.WorktreeError) as info:
        wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)
    msg = str(info.value)
    assert msg.startswith("aggregate has uncommitted product changes")
    assert "src/app.py" in msg and "docs/new.md" in msg
    assert not root.exists()


def test_ii_untracked_and_second_mailbox_are_not_blockers(wt, repo, root, capsys):
    (repo / "scratch.txt").write_text("user notes\n")
    (repo / "tmp" / "deep").mkdir(parents=True)
    (repo / "tmp" / "deep" / "x.bin").write_text("x\n")
    second_mailbox(repo)
    (repo / "loop" / "STATE.md").write_text("status: running\n")  # own mailbox
    found = wt.classify_aggregate(repo, repo / "loop")
    assert found["product"] == [] and found["foreign"] == []
    assert found["ignored"] == ["loop-auth/", "scratch.txt", "tmp/deep/x.bin"]
    assert wt.aggregate_blockers(repo, repo / "loop") == []
    record = wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [
        "trioctl: ignored (not product paths; the worker worktree will not "
        "see them): loop-auth/, scratch.txt, tmp/deep/x.bin"
    ]
    worktree = Path(record["path"])
    assert not (worktree / "scratch.txt").exists()
    assert not (worktree / "loop-auth").exists()
    # The user's untracked files are left exactly where they were.
    assert (repo / "scratch.txt").read_text() == "user notes\n"


def test_iii_modified_tracked_outside_product_paths_blocks(wt, repo, root):
    (repo / "README.md").write_text("user edit\n")
    found = wt.classify_aggregate(repo, repo / "loop")
    assert found["foreign"] == [" M README.md"] and found["product"] == []
    with pytest.raises(wt.WorktreeError) as info:
        wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)
    assert str(info.value) == (
        "aggregate has modified tracked files outside every declared product "
        "path: commit or stash YOUR change to README.md; the Lead must not "
        "commit files it did not edit"
    )


def test_tracked_files_inside_another_mailbox_do_not_block(wt, repo, root):
    other = second_mailbox(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "track other mailbox")
    (other / "STATE.md").write_text("status: shipped\n")  # the other run moved on
    found = wt.classify_aggregate(repo, repo / "loop")
    assert found == {"product": [], "foreign": [], "ignored": ["loop-auth/"]}
    wt.create(repo, slice_id="X", mailbox=repo / "loop", root=root)


def test_no_declared_writes_keeps_untracked_conservative(wt, repo, root):
    (repo / "loop" / "PLAN.md").write_text("plan without slices\n")
    git(repo, "commit", "-q", "-am", "plan")
    (repo / "new_product.py").write_text("x\n")
    second_mailbox(repo)
    found = wt.classify_aggregate(repo, repo / "loop")
    assert found["product"] == ["?? new_product.py"]
    assert found["ignored"] == ["loop-auth/"]


def test_integration_ignores_untracked_non_product_files(wt, repo, root):
    record = wt.create(repo, slice_id="W", mailbox=repo / "loop", root=root)
    Path(record["path"], "src", "app.py").write_text("x = 3\n")
    wt.mark_exited(repo, wt.load_record(repo, record["id"]), 0)
    (repo / "scratch.txt").write_text("appeared while the worker ran\n")
    second_mailbox(repo)
    result = wt.integrate(repo, record["id"], summary="worker")
    assert result["state"] == "integrated", result
    assert (repo / "src" / "app.py").read_text() == "x = 3\n"
    assert (repo / "scratch.txt").exists()


def _cli(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, **GIT_ENV, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        ["python3", str(SCRIPT), "omnigent", "worktrees", "create",
         "--repo", str(repo), "--mailbox", str(repo / "loop"), *argv],
        cwd=repo, capture_output=True, text=True, env=env, timeout=120,
    )


def test_cli_foreign_refusal_and_ignored_note(wt, repo, root):
    (repo / "README.md").write_text("user edit\n")
    refused = _cli(repo, "--slice", "X", "--worktree-root", str(root))
    assert refused.returncode == 1
    assert "commit or stash YOUR change to README.md" in refused.stderr
    git(repo, "checkout", "--", "README.md")
    second_mailbox(repo)
    ok = _cli(repo, "--slice", "X", "--worktree-root", str(root))
    assert ok.returncode == 0, ok.stderr
    assert "trioctl: ignored (not product paths; the worker worktree will not see them): loop-auth/" in ok.stderr

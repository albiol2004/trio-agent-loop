"""r14 W-2: a repository that TRACKS project `.cursor/{mcp,hooks}.json`.

Live defect (release 4680b7e): the product repo committed a stale
`omnigent` MCP server and an Omnigent usage stop hook in `.cursor/`, so
every task-owned worktree checkout carried them and
`cursor_config_conflicts` retained every builder/eval worktree as
`unsafe_cursor_config`. Tracked config is now neutralised in the worktree
(session-bound entries stripped, `skip-worktree` set in the worktree's own
index) so the worker never honours it and the product diff/merge never
carries the overwrite back. An UNTRACKED foreign config is still refused.

Offline, real git scratch repos.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from test_worker_worktrees import GIT_ENV, MODULE, _load, clean, git

PLAN = """# Plan

```yaml
slices:
  - id: api
    writes: [src/app.py]
    reads: []
```
"""

#: What the live repo committed: an old-layout Omnigent bridge on another
#: user's home, plus (added here) a user server that must survive.
TRACKED_MCP = {
    "mcpServers": {
        "omnigent": {
            "command": "/home/alex/.local/share/uv/tools/omnigent/bin/python3",
            "args": ["-I", "-m", "omnigent.claude_native_bridge", "serve-mcp",
                     "--bridge-dir", "/tmp/omnigent-1000/cursor-native/b228615a"],
            "env": {"TMPDIR": "/tmp"},
        },
        "docs": {"command": "npx", "args": ["-y", "docs-mcp"]},
    }
}
#: An old-module usage hook (``omnigent.cursor_native_usage``, which
#: Omnigent's own merge does NOT drop) plus a user hook that must survive.
TRACKED_HOOKS = {
    "version": 1,
    "hooks": {
        "stop": [
            {"command": "/home/alex/py -I -m omnigent.cursor_native_usage record-usage "
                        "--bridge-dir /tmp/omnigent-1000/cursor-native/7a2eb86c"},
            {"command": "echo done"},
        ],
        "afterFileEdit": [{"command": "./fmt.sh"}],
    },
}


@pytest.fixture()
def wt(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return _load("worker_worktrees_r14_under_test", MODULE)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "product"
    (repo / "src").mkdir(parents=True)
    (repo / "loop").mkdir()
    (repo / ".cursor").mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / "loop" / "PLAN.md").write_text(PLAN)
    (repo / ".cursor" / "mcp.json").write_text(json.dumps(TRACKED_MCP, indent=2) + "\n")
    (repo / ".cursor" / "hooks.json").write_text(json.dumps(TRACKED_HOOKS, indent=2) + "\n")
    (repo / ".cursor" / "rules.md").write_text("be nice\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "worktrees"


def omnigent_merge(where: Path, bridge: str) -> None:
    """What Omnigent's cursor-native launch merges into existing config."""
    mcp_path = where / ".cursor" / "mcp.json"
    mcp = json.loads(mcp_path.read_text())
    mcp.setdefault("mcpServers", {})["omnigent"] = {
        "command": "/usr/bin/python3",
        "args": ["-I", "-m", "omnigent.harnesses.claude_native.bridge", "serve-mcp",
                 "--bridge-dir", bridge],
    }
    mcp_path.write_text(json.dumps(mcp, indent=2))
    hooks_path = where / ".cursor" / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    hooks.setdefault("hooks", {}).setdefault("stop", []).append({
        "command": "/usr/bin/python3 -I -m omnigent.harnesses.cursor_native.usage "
                   f"record-usage --bridge-dir {bridge}"})
    hooks_path.write_text(json.dumps(hooks, indent=2))


def test_tracked_session_config_is_neutralised_not_fatal(wt, repo, root):
    record = wt.create(repo, slice_id="api", mailbox=repo / "loop", root=root)
    path = Path(record["path"])
    assert record.get("state") == "created" and "retained_reason" not in record
    assert sorted(record["neutralised_cursor"]) == [".cursor/hooks.json", ".cursor/mcp.json"]
    assert wt.cursor_config_conflicts(path) == []
    mcp = json.loads((path / ".cursor" / "mcp.json").read_text())
    assert mcp == {"mcpServers": {"docs": TRACKED_MCP["mcpServers"]["docs"]}}
    hooks = json.loads((path / ".cursor" / "hooks.json").read_text())
    assert hooks["hooks"]["stop"] == [{"command": "echo done"}]
    assert hooks["hooks"]["afterFileEdit"] == [{"command": "./fmt.sh"}]
    # The worktree reads clean: the neutralisation is invisible to status.
    assert git(path, "status", "--porcelain") == ""
    # The aggregate checkout is untouched.
    assert json.loads((repo / ".cursor" / "mcp.json").read_text()) == TRACKED_MCP


def test_builder_merge_never_carries_the_neutralised_config(wt, repo, root):
    original = {rel: (repo / rel).read_bytes() for rel in (".cursor/mcp.json", ".cursor/hooks.json")}
    record = wt.create(repo, slice_id="api", mailbox=repo / "loop", root=root)
    path = Path(record["path"])
    omnigent_merge(path, "/tmp/omnigent-1000/cursor-native/worker")  # the worker's launch
    (path / "src" / "app.py").write_text("x = 2\n")
    wt.mark_exited(repo, wt.load_record(repo, record["id"]), 0)
    result = wt.integrate(repo, record["id"], summary="api")
    assert result["state"] == "integrated", result.get("retained_detail")
    changed = git(repo, "diff", "--name-only", f"{record['base']}..{result['merge_commit']}")
    assert changed.splitlines() == ["src/app.py"]
    for rel, data in original.items():
        assert (repo / rel).read_bytes() == data
        assert subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"],
                              capture_output=True).stdout == data
    assert git(repo, "status", "--porcelain") == ""


def test_worker_that_commits_the_neutralised_config_is_retained(wt, repo, root):
    record = wt.create(repo, slice_id="api", mailbox=repo / "loop", root=root)
    path = Path(record["path"])
    git(path, "update-index", "--no-skip-worktree", ".cursor/mcp.json")
    git(path, "add", ".cursor/mcp.json")
    git(path, "commit", "-q", "-m", "sneak")
    wt.mark_exited(repo, wt.load_record(repo, record["id"]), 0)
    result = wt.integrate(repo, record["id"], summary="api")
    assert result["state"] == "retained"
    assert result["retained_reason"] == "cursor_config_write"
    assert json.loads((repo / ".cursor" / "mcp.json").read_text()) == TRACKED_MCP


def test_eval_worktree_is_neutralised_and_retires_cleanly(wt, repo, root):
    sha = git(repo, "rev-parse", "HEAD")
    record = wt.create(repo, slice_id="eval-api", mailbox=repo / "loop", root=root,
                       role="evaluator", detach_at=sha)
    path = Path(record["path"])
    assert wt.cursor_config_conflicts(path) == []
    omnigent_merge(path, "/tmp/omnigent-1000/cursor-native/eval")
    wt.mark_finished(repo, record["id"])
    (result,) = clean(wt, repo, held_sessions=set())
    assert result["state"] == "removed", result.get("retained_detail")
    assert not path.exists()


def test_untracked_foreign_cursor_config_is_still_refused(wt, repo, root):
    git(repo, "rm", "-q", "--cached", ".cursor/mcp.json", ".cursor/hooks.json")
    (repo / ".gitignore").write_text(".cursor/mcp.json\n.cursor/hooks.json\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "untrack")
    record = wt.create(repo, slice_id="api", mailbox=repo / "loop", root=root)
    path = Path(record["path"])
    assert "neutralised_cursor" not in record
    # A foreign, UNTRACKED session config appearing in a worktree (another
    # bridge's launch) is never touched and still fails the guard.
    (path / ".cursor").mkdir(exist_ok=True)
    (path / ".cursor" / "mcp.json").write_text(json.dumps(TRACKED_MCP))
    (path / ".cursor" / "hooks.json").write_text(json.dumps(TRACKED_HOOKS))
    before = (path / ".cursor" / "mcp.json").read_bytes()
    assert wt.neutralise_tracked_cursor(path) == []
    assert (path / ".cursor" / "mcp.json").read_bytes() == before
    problems = wt.cursor_config_conflicts(path)
    assert any("'omnigent' MCP server" in p for p in problems)
    assert any("usage stop hook" in p for p in problems)


def test_tracked_symlinked_cursor_dir_is_still_refused(wt, repo, root, tmp_path):
    git(repo, "rm", "-q", "-r", "--cached", ".cursor")
    target = tmp_path / "shared-cursor"
    (repo / ".cursor").rename(target)
    (repo / ".cursor").symlink_to(target)
    git(repo, "add", ".cursor")
    git(repo, "commit", "-q", "-m", "symlink")
    with pytest.raises(wt.WorktreeError, match="symlink"):
        wt.create(repo, slice_id="api", mailbox=repo / "loop", root=root)
    (entry,) = [r for _i, r in wt.list_records(repo)]
    assert entry["retained_reason"] == "unsafe_cursor_config"


# r16b deleted (r16 DESIGN §1.11) the root-specific recognizer this test
# pinned -- `classify_aggregate`'s own branch that treated an omnigent
# merge INTO the repository root's tracked `.cursor/{mcp,hooks}.json` as
# ignorable (`omnigent_only_change` + `_normal_cursor`, and the root
# stranger detection with it). It existed only because the Lead used to
# launch a Cursor session directly at the aggregate root; since r16b every
# role runs in its own Lead worktree (root-free, both open-loop and
# lockstep) and the root never runs a Cursor session at all, so this
# scenario -- "the Lead's own root launch merges its entries into the
# tracked files" -- can no longer arise, and `classify_aggregate` now
# correctly reports such a change as `foreign` like any other, matching
# the grep confirming both symbols are gone from worker_worktrees.py.
# `omnigent_merge` itself stays: the worker/eval-worktree tests above
# still exercise real, current neutralisation of a session's own merge
# into an ISOLATED worktree's tracked config.

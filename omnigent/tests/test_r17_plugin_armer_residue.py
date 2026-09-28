"""eval-r17 T1-T4: Omnigent's plugin stop armer is owned residue.

With `OMNIGENT_CURSOR_PLUGIN_DIR` on, Omnigent writes a project
`.cursor/hooks.json` stop entry `{"command": "true # omnigent-plugin-stop-armer"}`
(bound to no session) so cursor-agent fires the plugin's own stop hook.
Armer-only content is Omnigent's: rebuildable/strippable, never a retention
reason, never "differs beyond owned entries", never an inherited-config
problem. The armer next to a user hook keeps the user hook (user content).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARMER = {"command": "true # omnigent-plugin-stop-armer"}
USER_HOOK = {"command": "./scripts/notify.sh"}


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wt():
    return _load("r17_worker_worktrees", ROOT / "worker_worktrees.py")


@pytest.fixture(scope="module")
def rf():
    return _load("r17_root_free", ROOT / "root_free.py")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin"},
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in {"README.md": "r\n", **(files or {})}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def _hooks(*entries: dict) -> dict:
    return {"version": 1, "hooks": {"stop": list(entries)}}


def _usage(wt, bridge: str = "b" * 32) -> dict:
    return {"command": f"/usr/bin/python3 -I -m omnigent.harnesses.cursor_native.usage "
                       f"record-usage --bridge-dir /tmp/omnigent-1000/cursor-native/{bridge}"}


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# ------------------------------------- N9: is_plugin_stop_armer shape parity
#
# Omnigent's own recognizer of the same name (omnigent/harnesses/cursor_native
# /bridge.py, commit da6045a2e) additionally tolerates an explicit
# `"type": "command"` key next to `"command"`. Trio's predicate must accept
# every shape Omnigent's writer can produce, and stay fail-closed on anything
# else (a different command string, a user hook, or an unknown key).


@pytest.mark.parametrize("entry", [
    {"command": "true # omnigent-plugin-stop-armer"},
    {"command": "true # omnigent-plugin-stop-armer", "type": "command"},
])
def test_n9_armer_shapes_omnigent_can_write_are_recognized(wt, entry):
    assert wt.is_plugin_stop_armer(entry)


@pytest.mark.parametrize("entry", [
    {"command": "true # omnigent-plugin-stop-armer", "timeout": 5},
    {"command": "true # omnigent-plugin-stop-armer", "type": "prompt"},
    {"command": "true  # omnigent-plugin-stop-armer"},
    {"command": "true # omnigent-plugin-stop-armer", "type": "command", "timeout": 5},
    USER_HOOK,
    {"type": "command"},
    "true # omnigent-plugin-stop-armer",
    None,
    {},
])
def test_n9_non_armer_shapes_stay_refused(wt, entry):
    assert not wt.is_plugin_stop_armer(entry)


def test_n9_armer_with_type_key_is_owned_residue(wt, tmp_path):
    """The `type: command` shape is treated exactly like the plain shape
    everywhere `is_plugin_stop_armer` gates behavior, not just in isolation."""
    repo = _repo(tmp_path)
    typed = {"command": "true # omnigent-plugin-stop-armer", "type": "command"}
    _write(repo / ".cursor" / "hooks.json", _hooks(typed))
    assert wt.owned_residue(repo, ".cursor/hooks.json")
    user, residue = wt._split_status(repo, wt.status_entries(repo))
    assert user == [] and residue == [".cursor/hooks.json"]
    # next to a user hook: user content, not residue (same as the plain shape).
    _write(repo / ".cursor" / "hooks.json", _hooks(typed, USER_HOOK))
    assert not wt.owned_residue(repo, ".cursor/hooks.json")


# ------------------------------------------------ T1 _owned_hooks / owned_residue


def test_t1_armer_only_hooks_json_is_owned_residue(wt, tmp_path):
    repo = _repo(tmp_path)
    hooks = repo / ".cursor" / "hooks.json"
    _write(hooks, _hooks(ARMER))
    assert wt.owned_residue(repo, ".cursor/hooks.json")
    _write(hooks, _hooks(_usage(wt), ARMER))  # usage hook + armer: owned too
    assert wt.owned_residue(repo, ".cursor/hooks.json")
    user, residue = wt._split_status(repo, wt.status_entries(repo))
    assert user == [] and residue == [".cursor/hooks.json"]


@pytest.mark.parametrize("entries", [
    [ARMER, USER_HOOK],                                          # user hook kept
    [{"command": "true # omnigent-plugin-stop-armer", "timeout": 5}],  # not exact
    [{"command": "true  # omnigent-plugin-stop-armer"}],              # not exact
])
def test_t1_armer_with_user_content_is_not_residue(wt, tmp_path, entries):
    repo = _repo(tmp_path)
    _write(repo / ".cursor" / "hooks.json", _hooks(*entries))
    assert not wt.owned_residue(repo, ".cursor/hooks.json")
    user, residue = wt._split_status(repo, wt.status_entries(repo))
    assert residue == [] and user and user[0].endswith(".cursor/hooks.json")


def test_t1_core_residue_gate_accepts_the_armer(wt, tmp_path):
    """metrics/trio_loop.py delegates to owned_residue (non-isolated SHIP)."""
    core = _load("r17_trio_loop", ROOT.parent / "metrics" / "trio_loop.py")
    core.owned_residue_check = wt.owned_residue
    repo = _repo(tmp_path)
    _write(repo / ".cursor" / "hooks.json", _hooks(ARMER))
    assert core._product_untracked_paths(repo, None) == []
    _write(repo / ".cursor" / "hooks.json", _hooks(ARMER, USER_HOOK))
    assert core._product_untracked_paths(repo, None) == [".cursor/hooks.json"]


def test_t1_root_free_lead_worktree_removal_is_not_retained_for_the_armer(wt, rf, tmp_path):
    """root_free.remove_worktree (the r16 residue unlink) drops an armer-only file."""
    repo = _repo(tmp_path)
    lead = tmp_path / "lead"
    git(repo, "worktree", "add", "-q", "-b", "trio/x", str(lead))
    owner = "owner-token"
    rf._mark_owner(wt, lead, owner)
    _write(lead / ".cursor" / "hooks.json", _hooks(ARMER))
    assert rf.remove_worktree(wt, repo, lead, owner=owner, branch="trio/x") is None
    assert not lead.exists()
    # The armer next to a user hook: user content, the worktree is retained.
    lead2 = tmp_path / "lead2"
    git(repo, "worktree", "add", "-q", "-b", "trio/y", str(lead2))
    rf._mark_owner(wt, lead2, owner)
    _write(lead2 / ".cursor" / "hooks.json", _hooks(ARMER, USER_HOOK))
    why = rf.remove_worktree(wt, repo, lead2, owner=owner, branch="trio/y")
    assert why and why.startswith("dirty:") and ".cursor/hooks.json" in why
    assert json.loads((lead2 / ".cursor" / "hooks.json").read_text())["hooks"]["stop"] == [
        ARMER, USER_HOOK,
    ]


# ------------------------------------------------ T2 _owned_ignored_cursor


def test_t2_gitignored_cursor_dir_with_the_armer_is_disposable(wt, tmp_path):
    repo = _repo(tmp_path, {".gitignore": ".cursor/\n"})
    _write(repo / ".cursor" / "hooks.json", _hooks(ARMER))
    assert wt._owned_ignored_cursor(repo, ".cursor/") == [".cursor/hooks.json"]
    _write(repo / ".cursor" / "mcp.json", {"mcpServers": {"mine": {"command": "x"}}})  # user
    assert wt._owned_ignored_cursor(repo, ".cursor/") is None
    (repo / ".cursor" / "mcp.json").unlink()
    _write(repo / ".cursor" / "hooks.json", _hooks(ARMER, USER_HOOK))
    assert wt._owned_ignored_cursor(repo, ".cursor/") is None


# ------------------------------------------------ T4 inherited_cursor_problems


def test_t4_user_level_armer_is_not_an_inherited_session_binding(wt, tmp_path):
    home = tmp_path / "home"
    _write(home / ".cursor" / "hooks.json", _hooks(ARMER))
    assert wt.inherited_cursor_problems(home=home, system_hooks=()) == []
    _write(home / ".cursor" / "hooks.json", _hooks(ARMER, USER_HOOK))
    assert wt.inherited_cursor_problems(home=home, system_hooks=()) == []
    # A real session binding next to the armer is still found.
    _write(home / ".cursor" / "hooks.json", _hooks(ARMER, _usage(wt)))
    problems = wt.inherited_cursor_problems(home=home, system_hooks=())
    assert problems and "carries an Omnigent session hook" in problems[0]


# ------------------------------------------------ r17 B2: empty mcpServers left behind


def test_untracked_empty_mcp_servers_is_owned_residue(wt, rf, tmp_path):
    repo = _repo(tmp_path)
    mcp = repo / ".cursor" / "mcp.json"
    _write(mcp, {"mcpServers": {}})
    assert wt.owned_residue(repo, ".cursor/mcp.json")
    assert wt._split_status(repo, wt.status_entries(repo)) == ([], [".cursor/mcp.json"])
    _write(repo / ".cursor" / "hooks.json", _hooks(ARMER))
    ignored = _repo(tmp_path / "ign", {".gitignore": ".cursor/\n"})
    _write(ignored / ".cursor" / "mcp.json", {"mcpServers": {}})
    assert wt._owned_ignored_cursor(ignored, ".cursor/") == [".cursor/mcp.json"]
    # Not residue: a user server, or extra top-level keys.
    _write(mcp, {"mcpServers": {"mine": {"command": "x"}}})
    assert not wt.owned_residue(repo, ".cursor/mcp.json")
    _write(mcp, {"mcpServers": {}, "note": 1})
    assert not wt.owned_residue(repo, ".cursor/mcp.json")
    # root_free Lead-worktree removal is not retained by it (r16b: every
    # loop's Lead runs in its own worktree; there is no root to restore).
    lead = tmp_path / "lead-mcp"
    git(repo, "worktree", "add", "-q", "-b", "trio/m", str(lead))
    rf._mark_owner(wt, lead, "own")
    _write(lead / ".cursor" / "mcp.json", {"mcpServers": {}})
    assert rf.remove_worktree(wt, repo, lead, owner="own", branch="trio/m") is None

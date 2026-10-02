#!/usr/bin/env python3
"""opencode-dash: the dashboard's detection of trio-opencode loops.

Covers the per-user run registry (``dashboard/loop_actions.opencode_registry``)
and the serve.py plumbing that makes a registered mailbox show up on the
board: ``_opencode_registry``, ``_live_loop_dirs`` (reading the live Lead
worktree copy), the ``.session.json`` broker-session exclusion, and the
needs_land inbox text. Modeled on native/tests/test_dash_records.py (the
registry semantics) and registry/tests/test_serve_dash_actions.py (loading
serve.py/loop_actions.py by path, without starting the HTTP server).

Everything runs offline against a temporary HOME and temporary git
fixtures; nothing here touches the real
``~/.local/state/trio-agent-loop`` or ``~/.local/share/trio-agent-loop``.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"
LOOP_ACTIONS_PATH = REPO_ROOT / "dashboard" / "loop_actions.py"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


la = _load("trio_dashboard_loop_actions_oc_test", LOOP_ACTIONS_PATH)
serve = _load("trio_dashboard_serve_oc_test", SERVE_PATH)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real per-user registries and Claude dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("TRIO_OPENCODE_RUNS_DIR", str(tmp_path / "opencode-runs"))
    monkeypatch.setenv("TRIO_NATIVE_RUNS_DIR", str(tmp_path / "native-runs"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    serve.HOME = home
    with serve._OPENCODE_REGISTRY_LOCK:
        serve._OPENCODE_REGISTRY.update(at=0.0, home=None, value=[])
    with serve._NATIVE_REGISTRY_LOCK:
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def _mailbox(root: Path, rel: str, state: str = "iteration: 0\nstatus: ready\nphase: idle\n") -> Path:
    box = root / rel
    box.mkdir(parents=True, exist_ok=True)
    (box / "GOAL.md").write_text("# Mission: opencode dash test\n", encoding="utf-8")
    (box / "STATE.md").write_text(state, encoding="utf-8")
    return box


def _repo_with_lead_worktree(tmp_path: Path, name: str = "repo"):
    """A repo with a committed root ``loop/`` mailbox, and a second worktree
    on branch ``trio/loop`` holding its own ``loop/`` copy (the Lead
    worktree a root-free trio-opencode run would keep its live mailbox in).
    Returns (repo, root_mailbox, lead_worktree, live_mailbox)."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    root_mailbox = _mailbox(repo, "loop")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    lead_wt = tmp_path / f"{name}-lead-wt"
    _git(repo, "worktree", "add", "-q", "-b", "trio/loop", str(lead_wt))
    live_mailbox = _mailbox(lead_wt, "loop",
                            "iteration: 3\nmax_iterations: 8\nstatus: running\nphase: lead-running\n")
    return repo, root_mailbox, lead_wt, live_mailbox


def _registry_record(mailbox: Path, repo: Path, *, driver: str = "opencode",
                     live_mailbox: Path | str | None = None, **extra) -> dict:
    record = {
        "schema": 1, "driver": driver, "harness": "opencode",
        "mailbox": str(mailbox), "live_mailbox": str(live_mailbox) if live_mailbox else None,
        "repo": str(repo), "lead_worktree": None, "branch": "trio/loop",
        "target": "main", "run_token": "tok-1", "exec_id": "exec-1", "pid": 999999999,
        "state": "running", "status": None, "result_path": str(mailbox / ".opencode-result.json"),
        "begun_at": "2026-09-30T00:00:00Z", "updated_at": "2026-09-30T00:00:00Z",
        "finished_at": None,
    }
    record.update(extra)
    return record


def _write_record(runs_dir: Path, mailbox: Path, record: dict) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(mailbox.resolve()).encode()).hexdigest()[:16]
    path = runs_dir / f"{key}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# --------------------------------------------------------- registry (loop_actions)


def test_valid_opencode_record_is_detected_with_harness_and_live_mailbox(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=live_mailbox))
    home = Path(os.environ["HOME"])
    entries = la.opencode_registry(home)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["mailbox"] == str(root_mailbox.resolve())
    assert entry["harness"] == "opencode"
    assert entry["live_mailbox"] == str(live_mailbox.resolve())
    assert entry["repo"] == str(repo.resolve())


def test_claude_workflow_record_in_opencode_dir_is_ignored(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, driver="claude-workflow",
                                  live_mailbox=live_mailbox))
    home = Path(os.environ["HOME"])
    assert la.opencode_registry(home) == []


def test_live_mailbox_in_unrelated_repo_is_dropped_to_none(tmp_path):
    repo, root_mailbox, _lead_wt, _live_mailbox = _repo_with_lead_worktree(tmp_path)
    other_repo = tmp_path / "unrelated"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    unrelated_mailbox = _mailbox(other_repo, "loop")
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=unrelated_mailbox))
    home = Path(os.environ["HOME"])
    entries = la.opencode_registry(home)
    assert len(entries) == 1
    assert entries[0]["live_mailbox"] is None


def test_live_mailbox_symlink_is_dropped_to_none(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    link = tmp_path / "live-link"
    link.symlink_to(live_mailbox, target_is_directory=True)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=link))
    home = Path(os.environ["HOME"])
    entries = la.opencode_registry(home)
    assert len(entries) == 1
    assert entries[0]["live_mailbox"] is None


def test_garbage_json_is_ignored(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    runs.mkdir(parents=True)
    (runs / "garbage.json").write_text("{not json", encoding="utf-8")
    (runs / "not-a-dict.json").write_text("[1, 2, 3]", encoding="utf-8")
    home = Path(os.environ["HOME"])
    assert la.opencode_registry(home) == []
    # A valid record alongside the garbage is still picked up.
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=live_mailbox))
    entries = la.opencode_registry(home)
    assert len(entries) == 1
    assert entries[0]["mailbox"] == str(root_mailbox.resolve())


def test_missing_runs_dir_returns_empty_list(tmp_path):
    home = Path(os.environ["HOME"])
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    assert not runs.exists()
    assert la.opencode_registry(home) == []


# --------------------------------------------------------- serve.py plumbing


def test_live_loop_dirs_reads_the_live_lead_worktree_copy(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=live_mailbox))
    metrics = serve.load_metrics_module()
    pairs = serve._live_loop_dirs(metrics, repo)
    matches = [(root, live) for root, live in pairs
              if Path(root).resolve() == root_mailbox.resolve()]
    assert len(matches) == 1
    root, live = matches[0]
    assert Path(live).resolve() == live_mailbox.resolve()


def test_opencode_registry_entry_helper(tmp_path):
    repo, root_mailbox, _lead_wt, live_mailbox = _repo_with_lead_worktree(tmp_path)
    runs = Path(os.environ["TRIO_OPENCODE_RUNS_DIR"])
    _write_record(runs, root_mailbox,
                 _registry_record(root_mailbox, repo, live_mailbox=live_mailbox))
    entry = serve._opencode_registry_entry(root_mailbox)
    assert entry is not None
    assert entry["harness"] == "opencode"


def test_session_sidecar_with_opencode_driver_is_not_a_broker_session():
    session_state = {"driver": "opencode", "session": "run-token-not-a-broker-session"}
    assert serve._broker_session_ids(None, session_state) == []
    # A session sidecar naming an unrelated driver still counts as one.
    other = {"driver": "some-broker", "session": "real-broker-session"}
    assert serve._broker_session_ids(None, other) == ["real-broker-session"]


def test_is_opencode_loop_reads_session_sidecar(tmp_path):
    box = _mailbox(tmp_path, "loop")
    assert serve._is_opencode_loop(box) is False
    (box / ".session.json").write_text(json.dumps({"driver": "opencode", "session": "tok"}),
                                       encoding="utf-8")
    assert serve._is_opencode_loop(box) is True
    (box / ".session.json").write_text(json.dumps({"driver": "claude-workflow", "session": "tok"}),
                                       encoding="utf-8")
    assert serve._is_opencode_loop(box) is False


def test_driver_snapshot_accepts_opencode_from_driver_json(tmp_path):
    box = _mailbox(tmp_path, "loop")
    (box / ".driver.json").write_text(json.dumps({"pid": 0, "driver": "opencode",
                                                  "phase": "lead-running"}),
                                      encoding="utf-8")
    snapshot = serve._driver_snapshot(box)
    assert snapshot is not None
    assert snapshot["driver"] == "opencode"


def test_loop_controls_refuses_start_for_opencode_driver(tmp_path):
    box = _mailbox(tmp_path, "loop")
    detection = {"sources": [], "control_pid": None, "broker": "disabled"}
    controls = serve._loop_controls(box, box.parent, detection, "opencode")
    assert controls["start"]["enabled"] is False
    assert "opencode-driver/trio-opencode" in controls["start"]["reason"]


def test_state_inbox_items_error_suffix_and_needs_land_text():
    items = []

    def add(severity, kind, headline, detail, anchor=None):
        items.append({"severity": severity, "kind": kind, "headline": headline,
                      "detail": detail})

    serve._state_inbox_items({"state": "error", "detail": {"reason": "boom"}},
                             add, opencode=True, mailbox="/repo/loop")
    assert any("(opencode)" in i["headline"] for i in items)

    items.clear()
    serve._state_inbox_items({"state": "needs_land", "phase": "lead-running"},
                             add, opencode=True, mailbox="/repo/loop")
    needs_land = next(i for i in items if i["kind"] == "needs_land")
    assert "opencode-driver/trio-opencode land --mailbox /repo/loop" in needs_land["detail"]

    items.clear()
    serve._state_inbox_items({"state": "needs_land", "phase": "lead-running"}, add)
    needs_land = next(i for i in items if i["kind"] == "needs_land")
    assert "trioctl omnigent land" in needs_land["detail"]
    assert "opencode-driver" not in needs_land["detail"]

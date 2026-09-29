"""native-dash: .native-result.json and the per-user run registry.

launch.sh (fake `claude`) and the helper's begin/end write the records the
trio-dash dashboard reads; the registry dir comes from TRIO_NATIVE_RUNS_DIR
(set per test by conftest.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_launch import launch  # noqa: F401  (fixture helper)
from test_launch import box  # noqa: F401  (pytest fixture)
from test_step_ops import git_env, mbox, repo, step  # noqa: F401

NATIVE = Path(__file__).resolve().parents[1]

RUN_ID_FAKE = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
base = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "-repo", session)
os.makedirs(os.path.join(base, "subagents", "workflows", "wf_abc123-9f0"), exist_ok=True)
print(json.dumps({"type": "result", "total_cost_usd": 2.5,
                  "result": os.environ["FAKE_RESULT"]}))
'''


def registry_file(runs: Path, mailbox: Path) -> Path:
    key = hashlib.sha256(str(mailbox.resolve()).encode()).hexdigest()[:16]
    return runs / f"{key}.json"


def run_launch(box: Path, tmp_path: Path, result: str, *args: str,
               fake_src: str = RUN_ID_FAKE) -> subprocess.CompletedProcess:
    fake = tmp_path / "claude-runid"
    fake.write_text(fake_src)
    fake.chmod(0o755)
    env = dict(os.environ, TRIO_NATIVE_CLAUDE=str(fake), FAKE_RESULT=result)
    return subprocess.run(
        ["bash", str(NATIVE / "launch.sh"), *(args or ("start",)),
         "--mailbox", str(box)], capture_output=True, text=True, env=env)


def test_launch_persists_result_and_registry(box: Path, tmp_path: Path,
                                              _isolated_native_registry: Path) -> None:
    body = ('```json\n{"status": "held", "held_step": "gate", "reason": "gate held: denied",'
            ' "conflicts": [{"id": "a", "branch": "b", "files": ["x.py"]}],'
            ' "dangling_worktrees": ["/r/.claude/worktrees/wf_1-2"],'
            ' "role_denials": [{"label": "lead", "text": "DENIED: rm"}], "iteration": 2}\n```')
    proc = run_launch(box, tmp_path, body)
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)
    rec = json.loads((box / ".native-result.json").read_text())
    assert rec["source"] == "launcher" and rec["driver"] == "claude-workflow"
    assert rec["status"] == "held" and rec["held_step"] == "gate"
    assert rec["conflicts"][0]["files"] == ["x.py"]
    assert rec["dangling_worktrees"] == ["/r/.claude/worktrees/wf_1-2"]
    assert rec["role_denials"][0]["label"] == "lead"
    assert rec["session_id"] == printed["launcher"]["session_id"]
    assert rec["run_id"] == "wf_abc123-9f0"
    assert rec["api_equiv_usd"] == 2.5 and rec["exit_code"] == 0
    assert rec["launcher"] == str(NATIVE / "launch.sh")
    reg = json.loads(registry_file(_isolated_native_registry, box).read_text())
    assert reg["mailbox"] == str(box.resolve())
    assert reg["state"] == "finished" and reg["status"] == "held"
    assert reg["run_id"] == "wf_abc123-9f0"
    assert reg["session_id"] == rec["session_id"]
    assert reg["launcher"] == str(NATIVE / "launch.sh")
    assert reg["result_path"] == str(box.resolve() / ".native-result.json")


def test_launch_error_path_persists_error(box: Path, tmp_path: Path,
                                          _isolated_native_registry: Path) -> None:
    proc = run_launch(box, tmp_path, "no fenced json here")
    assert proc.returncode == 3
    rec = json.loads((box / ".native-result.json").read_text())
    assert rec["status"] == "error" and "no fenced result JSON" in rec["reason"]
    assert rec["api_equiv_usd"] == 2.5
    reg = json.loads(registry_file(_isolated_native_registry, box).read_text())
    assert reg["status"] == "error"


def test_lock_refused_start_keeps_previous_result(box: Path, tmp_path: Path) -> None:
    run_launch(box, tmp_path, '```json\n{"status": "shipped"}\n```')
    first = (box / ".native-result.json").read_text()
    refused = ('```json\n{"status": "error", "lock": "foreign", "reason": '
               '"begin: mailbox is locked by trio_loop (pid 9)"}\n```')
    proc = run_launch(box, tmp_path, refused)
    assert proc.returncode == 0
    assert (box / ".native-result.json").read_text() == first


def test_launch_does_not_repoint_a_live_launchers_record(
        box: Path, tmp_path: Path, _isolated_native_registry: Path) -> None:
    reg = registry_file(_isolated_native_registry, box)
    reg.parent.mkdir(parents=True)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        reg.write_text(json.dumps({"mailbox": str(box.resolve()), "state": "running",
                                   "launcher_pid": live.pid, "session_id": "live-s"}))
        refused = ('```json\n{"status": "error", "lock": "foreign", "reason": '
                   '"begin: mailbox is locked by workflow:x (pid 9)"}\n```')
        run_launch(box, tmp_path, refused)
        assert json.loads(reg.read_text())["session_id"] == "live-s"
    finally:
        live.kill()
        live.wait()


def test_resume_records_given_run_id(box: Path, tmp_path: Path) -> None:
    run_launch(box, tmp_path, '```json\n{"status": "error"}\n```')
    proc = run_launch(box, tmp_path, '```json\n{"status": "shipped"}\n```',
                      "resume", "--run-id", "wf_given-1")
    assert proc.returncode == 0, proc.stderr
    rec = json.loads((box / ".native-result.json").read_text())
    assert rec["run_id"] == "wf_given-1" and rec["mode"] == "resume"


def test_helper_begin_registers_and_end_writes_partial_result(
        repo: Path, _isolated_native_registry: Path) -> None:
    out = step(repo, "begin")
    assert out["ok"], out
    reg_path = registry_file(_isolated_native_registry, mbox(repo))
    reg = json.loads(reg_path.read_text())
    assert reg["state"] == "running" and reg["run_token"] == "t-run1"
    assert reg["repo"] == str(repo) and reg["helper"].endswith("trio_native_step.py")
    end = step(repo, "end")
    assert end["ok"] and end["lock"] == "released"
    rec = json.loads((mbox(repo) / ".native-result.json").read_text())
    assert rec["source"] == "end" and rec["lock"] == "released"
    assert rec["state_status"] == "ready" and rec["dangling_worktrees"] == []
    assert json.loads(reg_path.read_text())["state"] == "ended"
    assert ".native-result.json" in (mbox(repo) / ".gitignore").read_text()


def test_end_of_a_foreign_run_writes_nothing(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    out = step(repo, "end", token="someone-else")
    assert out["lock"] == "foreign"
    assert not (mbox(repo) / ".native-result.json").exists()

"""native/launch.sh against a fake `claude` binary (no real session)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

LAUNCH = Path(__file__).resolve().parents[1] / "launch.sh"
SUFFIX = ("Launch only; do not edit files, settings or permissions; output "
          "the result JSON verbatim in one fenced block and stop.")

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
body = os.environ.get("FAKE_RESULT", "")
print(json.dumps({"type": "result", "result": body}))
'''


@pytest.fixture
def box(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "loop").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo / "loop"


def launch(box: Path, tmp_path: Path, *args: str, result: str) -> tuple:
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    argv = tmp_path / "argv.jsonl"
    env = dict(os.environ, TRIO_NATIVE_CLAUDE=str(fake), FAKE_ARGV=str(argv),
               FAKE_RESULT=result)
    proc = subprocess.run(["bash", str(LAUNCH), *args, "--mailbox", str(box)],
                          capture_output=True, text=True, env=env)
    calls = [json.loads(line) for line in argv.read_text().splitlines()] \
        if argv.exists() else []
    return proc, calls


def test_start_flags_prompt_and_parse(box: Path, tmp_path: Path) -> None:
    body = 'Done.\n```json\n{"status": "shipped", "code": 0}\n```\n'
    proc, calls = launch(box, tmp_path, "start", "--max-iterations", "3",
                         result=body)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "shipped" and out["launcher"]["exit_code"] == 0
    argv = calls[0]["argv"]
    assert calls[0]["cwd"] == str(box.parent)
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith("Run the saved workflow trio-native with args ")
    assert prompt.endswith(SUFFIX)
    assert '"max_iterations":3' in prompt and f'"mailbox":"{box}"' in prompt
    for flag, value in (("--permission-mode", "auto"),
                        ("--settings", '{"worktree":{"baseRef":"head"}}'),
                        ("--model", "claude-opus-5-5"),
                        ("--output-format", "json")):
        assert argv[argv.index(flag) + 1] == value
    session = argv[argv.index("--session-id") + 1]
    record = json.loads((box / ".native-launch.json").read_text())
    assert record["session_id"] == session == out["launcher"]["session_id"]
    joined = " ".join(argv)
    assert "dangerously" not in joined and "bypass" not in joined.lower()


def test_resume_reuses_session_and_args(box: Path, tmp_path: Path) -> None:
    body = '```json\n{"status": "held"}\n```'
    launch(box, tmp_path, "start", result=body)
    record = json.loads((box / ".native-launch.json").read_text())
    proc, calls = launch(box, tmp_path, "resume", "--run-id", "wf_abc",
                         result='```\n{"status": "shipped"}\n```')
    assert proc.returncode == 0
    argv = calls[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == record["session_id"]
    assert "--session-id" not in argv
    prompt = argv[argv.index("-p") + 1]
    assert 'resumeFromRunId "wf_abc"' in prompt and record["args"] in prompt
    assert prompt.endswith(SUFFIX)


def test_unparsable_result_exits_3(box: Path, tmp_path: Path) -> None:
    proc, _ = launch(box, tmp_path, "start", result="The workflow finished.")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["status"] == "error"


def test_resume_without_run_id_is_usage_error(box: Path, tmp_path: Path) -> None:
    proc, calls = launch(box, tmp_path, "resume", result="")
    assert proc.returncode == 2 and not calls

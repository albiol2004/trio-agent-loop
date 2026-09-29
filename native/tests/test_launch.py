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
if "FAKE_RAW" in os.environ:
    print(os.environ["FAKE_RAW"])
else:
    print(json.dumps({"type": "result", "result": body}))
'''


@pytest.fixture
def box(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "loop").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo / "loop"


def launch(box: Path, tmp_path: Path, *args: str, result: str,
           raw: str | None = None) -> tuple:
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    argv = tmp_path / "argv.jsonl"
    env = dict(os.environ, TRIO_NATIVE_CLAUDE=str(fake), FAKE_ARGV=str(argv),
               FAKE_RESULT=result)
    if raw is not None:
        env["FAKE_RAW"] = raw
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


# ------------------------------------------------ eval-native-v0b N7
@pytest.mark.parametrize("raw", ['["a", "b"]', '"just a string"', "42"])
def test_non_object_session_json_exits_3(box: Path, tmp_path: Path,
                                          raw: str) -> None:
    proc, _ = launch(box, tmp_path, "start", result="", raw=raw)
    assert proc.returncode == 3, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"] == "error" and "not an object" in out["reason"]


@pytest.mark.parametrize("tag", ["JSON", "jsonc", "Json", ""])
def test_fence_tags_accepted(box: Path, tmp_path: Path, tag: str) -> None:
    body = f"Result:\r\n```{tag}\r\n{{\"status\": \"shipped\"}}\r\n```\r\n"
    proc, _ = launch(box, tmp_path, "start", result=body)
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)["status"] == "shipped"


def test_result_block_wins_over_trailing_block(box: Path,
                                               tmp_path: Path) -> None:
    body = ('```json\n{"status": "shipped", "code": 0}\n```\n'
            'Note:\n```json\n{"hint": "x"}\n```\n')
    proc, _ = launch(box, tmp_path, "start", result=body)
    assert json.loads(proc.stdout)["status"] == "shipped"


def test_lock_refused_start_restores_previous_record(box: Path,
                                                    tmp_path: Path) -> None:
    launch(box, tmp_path, "start",
           result='```json\n{"status": "error", "reason": "killed"}\n```')
    first = (box / ".native-launch.json").read_text()
    refused = ('```json\n{"status": "error", "lock": "foreign", "reason": '
               '"begin: mailbox is locked by workflow:t under another live '
               'process (pid 7): a second launch with the same run_token is '
               'refused"}\n```')
    proc, calls = launch(box, tmp_path, "start", result=refused)
    out = json.loads(proc.stdout)
    assert out["launcher"]["record"] == "restored"
    assert (box / ".native-launch.json").read_text() == first
    assert out["launcher"]["session_id"] != json.loads(first)["session_id"]
    assert not list((box / ".native-runs").glob("launch-record.*"))


def test_first_start_refused_removes_record(box: Path, tmp_path: Path) -> None:
    refused = ('```json\n{"status": "error", "lock": "foreign", "reason": '
               '"begin: mailbox is locked by trio_loop (pid 9)"}\n```')
    proc, _ = launch(box, tmp_path, "start", result=refused)
    assert json.loads(proc.stdout)["launcher"]["record"] == "removed"
    assert not (box / ".native-launch.json").exists()


def test_start_that_ran_keeps_new_record(box: Path, tmp_path: Path) -> None:
    launch(box, tmp_path, "start", result='```json\n{"status": "held"}\n```')
    first = json.loads((box / ".native-launch.json").read_text())
    proc, _ = launch(box, tmp_path, "start",
                     result='```json\n{"status": "shipped"}\n```')
    new = json.loads((box / ".native-launch.json").read_text())
    assert new["session_id"] != first["session_id"]
    assert new["session_id"] == json.loads(proc.stdout)["launcher"]["session_id"]
    assert not list((box / ".native-runs").glob("launch-record.*"))


def test_default_timeout_is_at_least_six_hours() -> None:
    text = LAUNCH.read_text()
    import re
    value = int(re.search(r'timeout_s="(\d+)"', text).group(1))
    assert value >= 6 * 3600

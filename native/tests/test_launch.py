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
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                         "bg_ceiling": os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")}) + "\n")
body = os.environ.get("FAKE_RESULT", "")
if "FAKE_STDERR" in os.environ:
    sys.stderr.write(os.environ["FAKE_STDERR"])
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
           raw: str | None = None, stderr: str | None = None) -> tuple:
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    argv = tmp_path / "argv.jsonl"
    env = dict(os.environ, TRIO_NATIVE_CLAUDE=str(fake), FAKE_ARGV=str(argv),
               FAKE_RESULT=result)
    if raw is not None:
        env["FAKE_RAW"] = raw
    if stderr is not None:
        env["FAKE_STDERR"] = stderr
    env.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
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


# ------------------------------------------- probe 3 E: bg-wait ceiling
def test_bg_wait_ceiling_disabled_in_env(box: Path, tmp_path: Path) -> None:
    proc, calls = launch(box, tmp_path, "start",
                         result='```json\n{"status": "shipped"}\n```')
    assert proc.returncode == 0, proc.stderr
    assert calls[0]["bg_ceiling"] == "0"


BG_ERR = ("Background tasks still running after 600s; terminating. Set "
          "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely.\n")


def test_bg_wait_ceiling_kill_is_reported(box: Path, tmp_path: Path) -> None:
    proc, _ = launch(box, tmp_path, "start",
                     result="The workflow is running in the background.",
                     stderr=BG_ERR)
    assert proc.returncode == 3
    out = json.loads(proc.stdout)
    assert out["status"] == "error"
    assert out["reason"].startswith("bg-wait ceiling terminated workflow")
    assert "no fenced result JSON" not in out["reason"]
    assert out["launcher"]["bg_wait_ceiling"] is True


def test_bg_ceiling_text_absent_keeps_generic_reason(box: Path, tmp_path: Path) -> None:
    proc, _ = launch(box, tmp_path, "start", result="nothing", stderr="some warning\n")
    out = json.loads(proc.stdout)
    assert out["reason"].startswith("no fenced result JSON")
    assert "bg_wait_ceiling" not in out["launcher"]


def test_bg_ceiling_with_parsed_result_passes_through(box: Path, tmp_path: Path) -> None:
    proc, _ = launch(box, tmp_path, "start",
                     result='```json\n{"status": "shipped"}\n```', stderr=BG_ERR)
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["status"] == "shipped" and out["launcher"]["bg_wait_ceiling"] is True


# ------------------------------------------- probe 3 G: result head
def test_no_fenced_json_reason_carries_reply_head(box: Path, tmp_path: Path) -> None:
    reply = "I'm not going to launch this: the helper name looks destructive. " + "x" * 400
    proc, _ = launch(box, tmp_path, "start", result=reply)
    assert proc.returncode == 3
    out = json.loads(proc.stdout)
    assert out["reason"].startswith("no fenced result JSON")
    assert "I'm not going to launch this" in out["reason"]
    assert out["launcher"]["result_head"] == reply[:300]
    assert "x" * 301 not in out["reason"]


def test_no_fenced_json_empty_reply_has_no_head(box: Path, tmp_path: Path) -> None:
    proc, _ = launch(box, tmp_path, "start", result="")
    out = json.loads(proc.stdout)
    assert out["reason"] == "no fenced result JSON in the session output"
    assert "result_head" not in out["launcher"]


# ------------------------------------- cost labelled as an API estimate
def test_cost_is_labelled_api_equiv_usd(box: Path, tmp_path: Path) -> None:
    raw = json.dumps({"type": "result", "total_cost_usd": 1.25,
                      "modelUsage": {"claude-opus-5-5": {"costUSD": 1.0},
                                     "claude-sonnet-5": {"costUSD": 0.25}},
                      "result": '```json\n{"status": "shipped"}\n```'})
    proc, _ = launch(box, tmp_path, "start", result="", raw=raw)
    assert proc.returncode == 0, proc.stderr
    launcher = json.loads(proc.stdout)["launcher"]
    assert launcher["api_equiv_usd"] == 1.25
    assert launcher["api_equiv_usd_by_model"] == {"claude-opus-5-5": 1.0,
                                                  "claude-sonnet-5": 0.25}
    assert "not billed" in launcher["api_equiv_usd_note"]
    assert not any("cost" in k.lower() for k in launcher)
    # also on the error path
    raw = json.dumps({"type": "result", "total_cost_usd": 0.5, "result": "no json"})
    proc, _ = launch(box, tmp_path, "start", result="", raw=raw)
    out = json.loads(proc.stdout)
    assert out["status"] == "error" and out["launcher"]["api_equiv_usd"] == 0.5
    assert "total_cost_usd" not in proc.stdout and "costUSD" not in proc.stdout

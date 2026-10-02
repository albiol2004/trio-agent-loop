"""Tests for trio_opencode.runner + trio_opencode.events, against the fake
``opencode`` executable (fake_opencode.py / fakeoc.install_fake)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest

from trio_opencode import events, runner
from trio_opencode.runner import TurnResult, TurnSpec, run_turn

from fakeoc import install_fake


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_scenario(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"scenario_{name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def make_key_file(tmp_path: Path, value: str = "fake-secret-key-xyz") -> Path:
    path = tmp_path / "fake-key.txt"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def read_calls(env: dict) -> list[dict]:
    calls_path = Path(env["FAKE_OC_STATE"]) / "calls.jsonl"
    if not calls_path.exists():
        return []
    return [json.loads(l) for l in calls_path.read_text(encoding="utf-8").splitlines() if l.strip()]


def base_spec(tmp_path: Path, env: dict, key_path: Path, **overrides) -> TurnSpec:
    kwargs = dict(
        role="builder", agent="trio-builder", model="opencode-go/glm-5.3-flash",
        prompt="do the thing", cwd=str(tmp_path), env=env,
        turn_timeout=8.0, idle_timeout=4.0, max_attempts=1, backoff=(0.1, 0.1, 0.1),
        log_dir=str(tmp_path / "logs"), key_file=str(key_path),
    )
    kwargs.update(overrides)
    return TurnSpec(**kwargs)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path(tmp_path):
    key_path = make_key_file(tmp_path)
    env = install_fake(tmp_path)  # default scenario: text "OK", v2 style
    spec = base_spec(tmp_path, env, key_path, label="lead plan it1")

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "OK"
    assert result.session_id and result.session_id.startswith("ses_")
    assert result.exit_code == 0
    assert result.attempts == 1
    # v2's default event style emits ONLY the `text` event (no
    # step_start/step_finish at all — SPEC.md "Completion: process exit 0 +
    # collected text = ok even with NO step events"), so no tokens are
    # reported and there is exactly one event.
    assert result.tokens["input"] == 0
    assert result.tokens["output"] == 0
    assert result.events == 1

    calls = read_calls(env)
    assert len(calls) == 1
    assert calls[0]["stdin_ok"] is True
    assert calls[0]["has_key"] is True

    # the fake key never leaks into the on-disk logs
    for p in result.log_paths:
        text = Path(p).read_text(encoding="utf-8")
        assert "fake-secret-key-xyz" not in text


def test_happy_path_v1_style_emits_steps_and_tokens(tmp_path, monkeypatch):
    """The v1 compatibility path (FAKE_OC_STYLE=v1): step_start/step_finish
    wrap every reply and the auto stop-step at the end reports tokens, same
    as the pre-v2 fake's behaviour."""
    monkeypatch.setenv("FAKE_OC_STYLE", "v1")
    key_path = make_key_file(tmp_path)
    env = install_fake(tmp_path)
    env["FAKE_OC_STYLE"] = "v1"
    spec = base_spec(tmp_path, env, key_path, label="lead plan it1")

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "OK"
    assert result.tokens["input"] == 6
    assert result.tokens["output"] == 4
    assert result.events >= 2  # step_start/text/step_finish

    calls = read_calls(env)
    assert calls[0]["style"] == "v1"
    assert "--dir" in calls[0]["argv"]
    assert "--standalone" not in calls[0]["argv"]


# ---------------------------------------------------------------------------
# $PWD (OpenCode v2 derives its agent's working directory from the
# inherited $PWD env var, not the subprocess's actual cwd= -- live-verified
# by the coordinator; fake_opencode.py honors $PWD for v2 the same way).
# ---------------------------------------------------------------------------


def test_run_turn_sets_pwd_to_resolved_cwd_not_inherited(tmp_path):
    """Regression test for "opencode v2 uses the inherited $PWD, not the
    process cwd": even when the PARENT process's own PWD points somewhere
    else entirely, run_turn must still spawn the turn with PWD set to
    spec.cwd's resolved path -- never left as whatever PWD happened to be
    inherited. Without runner.py's `pwd_env` fix this fails because
    fake_opencode.py (v2 style) reports having run in `wrong_dir` instead
    of the intended `target_dir`."""
    key_path = make_key_file(tmp_path)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    wrong_dir = tmp_path / "wrong"
    wrong_dir.mkdir()
    env = install_fake(tmp_path)
    env["PWD"] = str(wrong_dir)  # simulates a driver launched from elsewhere
    spec = base_spec(tmp_path, env, key_path, cwd=str(target_dir), label="lead plan it1")

    result = run_turn(spec)

    assert result.ok is True
    calls = read_calls(env)
    assert len(calls) == 1
    assert calls[0]["cwd"] == str(target_dir.resolve())
    assert calls[0]["cwd"] != str(wrong_dir.resolve())


def test_detect_cli_probe_sets_pwd_to_given_cwd(tmp_path):
    """The `run --help`/`--version` feature-detection probes are spawns of
    `opencode` too -- they must set PWD to match the cwd they are given,
    never leave it inherited."""
    env = install_fake(tmp_path)
    target_dir = tmp_path / "probe-dir"
    target_dir.mkdir()
    wrong_dir = tmp_path / "probe-wrong"
    wrong_dir.mkdir()
    env["PWD"] = str(wrong_dir)

    captured: dict = {}
    real_run = subprocess.run

    def _spy_run(argv, **kwargs):
        if argv and argv[0] == shutil.which("opencode", path=env["PATH"]) and "run" in argv and "--help" in argv:
            captured["env_pwd"] = kwargs.get("env", {}).get("PWD")
            captured["cwd"] = kwargs.get("cwd")
        return real_run(argv, **kwargs)

    import trio_opencode.runner as runner_mod
    orig = runner_mod.subprocess.run
    runner_mod.subprocess.run = _spy_run
    try:
        runner_mod.detect_cli(shutil.which("opencode", path=env["PATH"]), env, cwd=str(target_dir))
    finally:
        runner_mod.subprocess.run = orig

    assert captured.get("cwd") == str(target_dir)
    assert captured.get("env_pwd") == str(target_dir.resolve())


# ---------------------------------------------------------------------------
# stdin / argv construction
# ---------------------------------------------------------------------------


def test_popen_called_with_stdin_devnull_and_no_auto(tmp_path, monkeypatch):
    key_path = make_key_file(tmp_path)
    env = install_fake(tmp_path)
    captured = {}
    real_popen = subprocess.Popen

    class _FakeProc:
        def __init__(self):
            r, w = os.pipe()
            os.close(w)
            self.stdout = os.fdopen(r, "rb")
            self.pid = 999_999_999  # never a real pid; getpgid() must fail safely

        def wait(self, timeout=None):
            return 0

    def _fake_popen(argv, **kwargs):
        # Only fake the actual turn spawn (run_turn's own direct
        # `subprocess.Popen(..., start_new_session=True)` call); detect_cli's
        # `run --help`/`--version` probes (via `subprocess.run`, which never
        # passes `start_new_session`) go to the real — but still safely
        # fake — `opencode` binary so caps come back fully supported.
        if kwargs.get("start_new_session"):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc()
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen)

    spec = base_spec(tmp_path, env, key_path, max_attempts=1)
    result = run_turn(spec)

    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert "--auto" not in captured["argv"]
    assert "--yolo" not in captured["argv"]
    # empty stdout -> no text, no error -> classified transient ("empty output")
    assert result.kind == "transient"
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def test_missing_key_file_is_config_error_and_never_spawns(tmp_path):
    env = install_fake(tmp_path)
    spec = base_spec(tmp_path, env, tmp_path / "does-not-exist.txt")

    result = run_turn(spec)

    assert result.ok is False
    assert result.kind == "config_error"
    assert result.attempts == 0
    assert "does-not-exist.txt" in result.error
    assert read_calls(env) == []


def test_empty_key_file_is_config_error(tmp_path):
    env = install_fake(tmp_path)
    empty_key = tmp_path / "empty-key.txt"
    empty_key.write_text("   \n", encoding="utf-8")
    spec = base_spec(tmp_path, env, empty_key)

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.attempts == 0


def test_key_passed_via_env_only(tmp_path):
    key_path = make_key_file(tmp_path, value="s3cr3t-value")
    env = install_fake(tmp_path)
    spec = base_spec(tmp_path, env, key_path)

    result = run_turn(spec)

    calls = read_calls(env)
    assert calls[0]["has_key"] is True
    import hashlib
    assert calls[0]["key_sha8"] == hashlib.sha256(b"s3cr3t-value").hexdigest()[:8]
    # never in argv/logs/result
    for p in result.log_paths:
        assert "s3cr3t-value" not in Path(p).read_text(encoding="utf-8")
    assert "s3cr3t-value" not in (result.text or "")
    assert "s3cr3t-value" not in (result.error or "")


# ---------------------------------------------------------------------------
# Timeouts / process-group kill
# ---------------------------------------------------------------------------


def test_wall_timeout_kills_whole_process_group(tmp_path):
    key_path = make_key_file(tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    scenario = write_scenario(tmp_path, "wall_timeout", f"""
        def handle(ctx):
            p = ctx.sh("sleep 1000")
            with open({str(pid_file)!r}, "w") as fh:
                fh.write(str(p.pid))
            ctx.sleep(30)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=2.0, idle_timeout=30.0, max_attempts=1)

    started = time.monotonic()
    result = run_turn(spec)
    elapsed = time.monotonic() - started

    assert result.kind == "timeout"
    assert result.ok is False
    assert elapsed < 10
    grandchild_pid = int(pid_file.read_text().strip())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_idle_timeout_retried_then_idle_timeout(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "idle", """
        def handle(ctx):
            ctx.sleep(30)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=20.0, idle_timeout=1.0,
                      max_attempts=2, backoff=(0.1,))

    result = run_turn(spec)

    assert result.kind == "idle_timeout"
    assert result.attempts == 2


def test_turn_seconds_zero_disables_wall_clock_limit(tmp_path):
    """README.md "Container / no-time-limit mode": turn_timeout=0 is the
    "no wall-clock limit" sentinel -- a turn that runs well past what would
    normally be a tiny turn_timeout must still complete successfully."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "no_wall_limit", """
        def handle(ctx):
            ctx.sleep(1.5)
            ctx.text("done despite no wall-clock limit")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=0, idle_timeout=5.0, max_attempts=1)

    started = time.monotonic()
    result = run_turn(spec)
    elapsed = time.monotonic() - started

    assert result.ok is True
    assert result.kind == "ok"
    assert elapsed >= 1.4  # actually ran the full sleep, never killed on a wall clock


def test_turn_seconds_none_also_disables_wall_clock_limit(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "no_wall_limit_none", """
        def handle(ctx):
            ctx.sleep(1.5)
            ctx.text("done")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=None, idle_timeout=5.0, max_attempts=1)

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"


# ---------------------------------------------------------------------------
# Hung-connection watchdog: retries.idle_retry_unlimited
# ---------------------------------------------------------------------------


def test_idle_retry_unlimited_retries_past_max_attempts_then_succeeds(tmp_path, capsys):
    """README.md "Container / no-time-limit mode": with
    idle_retry_unlimited=True, an idle_timeout keeps being retried even past
    max_attempts=1 -- it never turns into a final failure just because the
    bounded retry budget ran out."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "idle_unlimited", """
        def handle(ctx):
            if ctx.n <= 3:
                ctx.sleep(30)
            else:
                ctx.text("finally answered")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(
        tmp_path, env, key_path, turn_timeout=20.0, idle_timeout=0.3,
        max_attempts=1, backoff=(0.05,), idle_retry_unlimited=True,
    )

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "finally answered"
    # retried strictly more than max_attempts=1 -- the unbounded watchdog
    # path, not the ordinary bounded one.
    assert result.attempts > spec.max_attempts
    assert result.attempts == 4

    err = capsys.readouterr().err
    assert err.count("idle watchdog retry") == 3
    assert "attempt 1" in err and "attempt 2" in err and "attempt 3" in err


def test_idle_retry_unlimited_false_still_bounded_by_max_attempts(tmp_path):
    """Without the flag, idle_timeout keeps its ordinary bounded-retry
    behaviour (unchanged default behaviour)."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "idle_bounded", """
        def handle(ctx):
            ctx.sleep(30)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=20.0, idle_timeout=0.3,
                     max_attempts=2, backoff=(0.05,), idle_retry_unlimited=False)

    result = run_turn(spec)

    assert result.kind == "idle_timeout"
    assert result.ok is False
    assert result.attempts == 2


# ---------------------------------------------------------------------------
# Retries / classification
# ---------------------------------------------------------------------------


def test_transient_retried_then_succeeds(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "transient_then_ok", """
        def handle(ctx):
            if ctx.n == 1:
                ctx.text("partial")
                ctx.stderr("upstream service timeout")
                ctx.exit(1)
            ctx.text("done after retry")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "done after retry"
    assert result.attempts == 2


def test_retry_continues_session_when_spec_had_none(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "continue_session", """
        def handle(ctx):
            if ctx.n == 1:
                ctx.text("partial")
                ctx.stderr("server_error while streaming")
                ctx.exit(1)
            ctx.text("resumed")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, session_id=None, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    calls = read_calls(env)
    assert len(calls) == 2
    assert calls[0]["session"] is None
    first_session = calls[0]["resolved_session"]
    assert calls[1]["session"] == first_session
    assert calls[1]["prompt"] == runner._CONTINUE_PROMPT
    assert result.session_id == first_session


def test_retry_resends_fresh_prompt_when_spec_had_session(tmp_path):
    key_path = make_key_file(tmp_path)
    # Pre-create a session by running once, then reuse it as spec.session_id.
    env = install_fake(tmp_path)
    seed = base_spec(tmp_path, env, key_path, max_attempts=1)
    seeded = run_turn(seed)
    existing_session = seeded.session_id
    assert existing_session

    scenario = write_scenario(tmp_path, "fresh_resend", """
        def handle(ctx):
            if ctx.n == 2:
                ctx.stderr("upstream service timeout")
                ctx.exit(1)
            ctx.text("resent original")
    """)
    env["FAKE_OC_SCENARIO"] = str(scenario)
    spec = base_spec(tmp_path, env, key_path, session_id=existing_session,
                      prompt="original prompt text", max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    calls = read_calls(env)
    last_two = calls[-2:]
    assert last_two[0]["session"] == existing_session
    assert last_two[1]["session"] == existing_session
    assert last_two[1]["prompt"] == "original prompt text"


def test_config_error_not_retried(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "config_error", """
        def handle(ctx):
            ctx.error("ProviderAuthError", "invalid api key")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3, backoff=(0.05, 0.05))

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.ok is False
    assert result.attempts == 1


def test_model_error_not_retried(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "model_error", """
        def handle(ctx):
            ctx.error("MessageAbortedError", "aborted by user")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3, backoff=(0.05, 0.05))

    result = run_turn(spec)

    assert result.kind == "model_error"
    assert result.attempts == 1


def test_empty_output_retried(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "empty_output", """
        def handle(ctx):
            pass
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.kind == "transient"
    assert result.error == "empty output"
    assert result.attempts == 2
    assert result.ok is False


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


def test_model_echoing_the_key_never_reaches_text_error_or_denials(tmp_path):
    """A model turn that echoes the raw provider key back — in its own text,
    on stderr, in a DENIED line and inside an error message — must never let
    that key value reach ``TurnResult.text``/``.error``/``.denials``, nor any
    on-disk log file (the runner's own scrub must run BEFORE parse/feed/the
    permission check, not only on the log-file copy)."""
    secret = "sk-fake-super-secret-do-not-leak"
    key_path = make_key_file(tmp_path, value=secret)
    scenario = write_scenario(tmp_path, "echo_key", f"""
        def handle(ctx):
            key = {secret!r}
            ctx.stderr(f"debug: using key {{key}}")
            ctx.text(f"leaking the key right here: {{key}}")
            ctx.text("DENIED: unrelated safe line, no secret here")
            ctx.error("ProviderAuthError", f"invalid credentials: {{key}}")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=1)

    result = run_turn(spec)

    assert secret not in (result.text or "")
    assert secret not in (result.error or "")
    for d in result.denials:
        assert secret not in d
    for p in result.log_paths:
        assert secret not in Path(p).read_text(encoding="utf-8")
    # The safe, key-free text line the model also emitted still made it
    # through untouched — only the key-bearing lines were dropped.
    assert "DENIED: unrelated safe line, no secret here" in (result.text or "")


def test_permission_warning_kills_quickly(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "permission", """
        def handle(ctx):
            ctx.permission_ask("bash", "rm -rf *")
            ctx.sleep(30)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=20.0, idle_timeout=8.0, max_attempts=1)

    started = time.monotonic()
    result = run_turn(spec)
    elapsed = time.monotonic() - started

    assert result.kind == "permission"
    assert result.ok is False
    assert "bash" in result.error
    assert elapsed < 6  # well before idle_timeout=8s

    calls = read_calls(env)
    fake_pid = calls[0]["pid"]
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(fake_pid, 0)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_event(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "cancel", """
        def handle(ctx):
            ctx.sleep(30)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, turn_timeout=20.0, idle_timeout=20.0, max_attempts=1)
    cancel = threading.Event()

    result_holder = {}

    def run():
        result_holder["result"] = run_turn(spec, cancel=cancel)

    t = threading.Thread(target=run)
    started = time.monotonic()
    t.start()
    time.sleep(0.5)
    cancel.set()
    t.join(timeout=15)
    elapsed = time.monotonic() - started

    assert not t.is_alive()
    result = result_holder["result"]
    assert result.kind == "cancelled"
    assert elapsed < 10


# ---------------------------------------------------------------------------
# Log scrubbing (unit level on the runner's own helpers)
# ---------------------------------------------------------------------------


def test_scrub_line_and_file(tmp_path):
    secret = "top-secret-abc"
    assert runner._scrub_line(f"OPENCODE_API_KEY={secret}", secret) == "[redacted]"
    assert runner._scrub_line("Authorization: Bearer xyz", None) == "[redacted]"
    assert runner._scrub_line('{"apikey": "xyz"}', None) == "[redacted]"
    assert runner._scrub_line("normal log line", secret) == "normal log line"

    p = tmp_path / "stderr.log"
    p.write_text(f"line one\nsecret is {secret}\nAPI_KEY leaking\nfine\n", encoding="utf-8")
    scrubbed = runner._scrub_file_in_place(p, secret)
    assert secret not in scrubbed
    assert secret not in p.read_text(encoding="utf-8")
    assert "fine" in scrubbed


# ---------------------------------------------------------------------------
# events.py unit tests
# ---------------------------------------------------------------------------


def test_parse_line_tolerant():
    assert events.parse_line("") is None
    assert events.parse_line("not json") is None
    assert events.parse_line("[1,2,3]") is None  # valid JSON, not an object
    assert events.parse_line('{"type":"text"}') == {"type": "text"}


def test_find_fenced_json_last_wins_and_required_keys():
    text = """
    first
    ```json
    {"a": 1}
    ```
    some text
    ```
    {"a": 2, "b": 3}
    ```
    """
    assert events.find_fenced_json(text) == {"a": 2, "b": 3}
    assert events.find_fenced_json(text, required_keys=("b",)) == {"a": 2, "b": 3}
    assert events.find_fenced_json(text, required_keys=("c",)) is None
    assert events.find_fenced_json("no fences here") is None
    assert events.find_fenced_json("```json\nnot json\n```") is None


def test_denied_lines():
    text = "ok\nDENIED: tried to write x\nfine\nDENIED: outside slice\n"
    assert events.denied_lines(text) == ["DENIED: tried to write x", "DENIED: outside slice"]
    assert events.denied_lines("") == []


def test_classify_ok_config_transient_model_permission():
    acc = events.TurnAccumulator()
    acc.feed({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}})
    acc.feed({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "hi"}})
    acc.feed({"type": "step_finish", "sessionID": "ses_1", "part": {"type": "step-finish", "reason": "stop"}})
    assert events.classify(acc, 0, "") == ("ok", None)

    acc2 = events.TurnAccumulator()
    acc2.feed({"type": "error", "sessionID": "ses_2",
               "error": {"name": "ProviderAuthError", "data": {"message": "bad key"}}})
    kind, err = events.classify(acc2, 1, "")
    assert kind == "config_error" and "ProviderAuthError" in err

    acc3 = events.TurnAccumulator()
    kind, err = events.classify(acc3, 1, "level=ERROR upstream service timeout")
    assert kind == "transient"

    acc4 = events.TurnAccumulator()
    acc4.feed({"type": "error", "sessionID": "ses_4",
               "error": {"name": "StructuredOutputError", "data": {"message": "bad json"}}})
    kind, err = events.classify(acc4, 1, "")
    assert kind == "model_error"

    acc5 = events.TurnAccumulator()
    kind, err = events.classify(acc5, 1, "permission requested: bash (rm -rf); auto-rejecting")
    assert kind == "permission"

    acc6 = events.TurnAccumulator()
    assert events.classify(acc6, 0, "")[0] == "transient"  # empty output


# ---------------------------------------------------------------------------
# v2 feature detection (runner.detect_cli / CliCaps)
# ---------------------------------------------------------------------------


def test_detect_cli_v2_default_style(tmp_path):
    env = install_fake(tmp_path)
    caps = runner.detect_cli(shutil.which("opencode", path=env["PATH"]), env)
    assert caps.style == "v2"
    assert caps.standalone is True
    assert caps.dir_flag is False
    assert not caps.missing
    assert caps.version == "2.0.20"


def test_detect_cli_v1_style(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OC_STYLE", "v1")
    env = install_fake(tmp_path)
    env["FAKE_OC_STYLE"] = "v1"
    caps = runner.detect_cli(shutil.which("opencode", path=env["PATH"]), env)
    assert caps.style == "v1"
    assert caps.dir_flag is True
    assert caps.standalone is False
    assert not caps.missing
    assert caps.version == "1.18.33"


def test_detect_cli_missing_session_flag_is_config_error_without_running(tmp_path):
    env = install_fake(tmp_path)
    env["FAKE_OC_HELP"] = (
        "Usage: opencode run [flags]\n\nFlags:\n"
        "  --standalone\n  --format <default|json>\n  --agent <name>\n"
        "  -m, --model <provider/model>\n"
    )
    key_path = make_key_file(tmp_path)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3)

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.attempts == 0
    assert "unsupported opencode version" in result.error
    assert "session" in result.error
    assert read_calls(env) == []  # never spawned the turn


def test_detect_cli_caches_by_path_and_mtime(tmp_path):
    env = install_fake(tmp_path)
    bin_path = shutil.which("opencode", path=env["PATH"])
    caps1 = runner.detect_cli(bin_path, env)
    # A help text override with no file change must NOT be re-probed (cache
    # key is (realpath, mtime) only, per SPEC.md).
    env2 = dict(env)
    env2["FAKE_OC_HELP"] = "totally different text with none of the flags"
    caps2 = runner.detect_cli(bin_path, env2)
    assert caps2 is caps1


# ---------------------------------------------------------------------------
# v2 argv shape
# ---------------------------------------------------------------------------


def test_argv_v2_has_standalone_model_variant_hash_no_dir_no_variant_flag(tmp_path):
    key_path = make_key_file(tmp_path)
    env = install_fake(tmp_path)
    spec = base_spec(tmp_path, env, key_path, variant="thinking",
                     model="opencode-go/deepseek-v4.1-flash", session_id="ses_abc123")

    result = run_turn(spec)

    assert result.ok is True
    calls = read_calls(env)
    assert calls[0]["argv"].count("--standalone") == 1
    assert "--dir" not in calls[0]["argv"]
    assert "--variant" not in calls[0]["argv"]
    assert calls[0]["model"] == "opencode-go/deepseek-v4.1-flash"
    assert calls[0]["variant"] == "thinking"
    assert calls[0]["session"] == "ses_abc123"
    assert calls[0]["cwd"] == str(tmp_path)


# ---------------------------------------------------------------------------
# v2 event stream shapes
# ---------------------------------------------------------------------------


def test_text_only_stream_with_no_step_events_is_ok(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "text_only", """
        def handle(ctx):
            ctx.text("the whole reply, no steps at all")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=1)

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "the whole reply, no steps at all"
    assert result.tokens["input"] == 0  # no step_finish was ever emitted


def test_repeated_part_id_keeps_latest_text_in_first_seen_order(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "growing_text", """
        def handle(ctx):
            ctx.text("Setting up", part_id="prt_x_text-0")
            ctx.text("note before the fence")
            ctx.text("Setting up the full answer", part_id="prt_x_text-0")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=1)

    result = run_turn(spec)

    assert result.ok is True
    # first-seen order: the growing part (last value wins) then the
    # independent chunk that was never re-emitted.
    assert result.text == "Setting up the full answer\nnote before the fence"


def test_unrecognised_event_stream_is_config_error_not_retried(tmp_path):
    """>= 3 all-unknown-type events is a genuine "this parser doesn't
    understand this CLI" signal (a 1-2 event all-unknown stream is too weak
    a signal on its own — see test_short_unknown_event_stream_is_transient)."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "weird_stream", """
        def handle(ctx):
            ctx._emit({"type": "totally-new-event-type", "sessionID": ctx._sid, "part": {}})
            ctx._emit({"type": "another-unknown", "sessionID": ctx._sid, "part": {}})
            ctx._emit({"type": "yet-another-unknown", "sessionID": ctx._sid, "part": {}})
            ctx._finished = True  # skip the (v1-only) auto stop-step
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3, backoff=(0.05, 0.05))

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.attempts == 1  # never retried
    assert "unrecognised JSON event stream" in result.error
    assert "totally-new-event-type" in result.error


def test_short_unknown_event_stream_is_transient(tmp_path):
    """1-2 unknown-type events, exit 0, is too weak a signal for the genuine
    "unsupported opencode version" config_error -- it is retried as
    transient instead (A3)."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "short_weird_stream", """
        def handle(ctx):
            if ctx.n == 1:
                ctx._emit({"type": "totally-new-event-type", "sessionID": ctx._sid, "part": {}})
                ctx._finished = True
            else:
                ctx.text("done")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    assert result.attempts == 2


# ---------------------------------------------------------------------------
# v2 error classification (error.type, dotted)
# ---------------------------------------------------------------------------


def test_v2_provider_no_route_is_config_error_even_with_unavailable_in_message(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "no_route", """
        def handle(ctx):
            ctx.error("provider.no-route", "Model unavailable: acme/ghost-model")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3, backoff=(0.05, 0.05))

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.attempts == 1


def test_v2_rate_limited_type_is_transient(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "v2_rate_limited", """
        def handle(ctx):
            if ctx.n == 1:
                ctx.error("provider.rate-limited", "slow down")
            else:
                ctx.text("done")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    assert result.attempts == 2


def test_v2_auth_error_type_is_config_error(tmp_path):
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "v2_auth", """
        def handle(ctx):
            ctx.error("provider.auth-failed", "invalid api key")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=3, backoff=(0.05, 0.05))

    result = run_turn(spec)

    assert result.kind == "config_error"
    assert result.attempts == 1


def test_classify_v2_dotted_error_types():
    acc = events.TurnAccumulator()
    acc.feed({"type": "error", "sessionID": "ses_1",
              "error": {"type": "provider.no-route", "message": "Model unavailable: x/y"}})
    assert events.classify(acc, 1, "")[0] == "config_error"

    acc2 = events.TurnAccumulator()
    acc2.feed({"type": "error", "sessionID": "ses_2",
              "error": {"type": "provider.overloaded", "message": "try again"}})
    assert events.classify(acc2, 1, "")[0] == "transient"

    acc3 = events.TurnAccumulator()
    acc3.feed({"type": "error", "sessionID": "ses_3",
              "error": {"type": "some.totally-unknown-type", "message": "???"}})
    assert events.classify(acc3, 1, "")[0] == "model_error"


def test_classify_unrecognised_stream_vs_truly_empty_output():
    # >= 3 all-unknown-type events, exit 0: genuine config_error (A3).
    acc = events.TurnAccumulator()
    acc.feed({"type": "weird", "sessionID": "ses_1", "part": {}})
    acc.feed({"type": "weird2", "sessionID": "ses_1", "part": {}})
    acc.feed({"type": "weird3", "sessionID": "ses_1", "part": {}})
    kind, err = events.classify(acc, 0, "")
    assert kind == "config_error"
    assert "unrecognised JSON event stream" in err

    # 1-2 unknown-type events is too weak a signal on its own -> transient,
    # not config_error (A3).
    acc1 = events.TurnAccumulator()
    acc1.feed({"type": "weird", "sessionID": "ses_1", "part": {}})
    assert events.classify(acc1, 0, "")[0] == "transient"

    acc2 = events.TurnAccumulator()  # no events fed at all
    assert events.classify(acc2, 0, "")[0] == "transient"


def test_session_id_field_name_variants_are_all_recognized():
    for key in ("sessionID", "sessionId", "session_id"):
        acc = events.TurnAccumulator()
        acc.feed({"type": "text", key: "ses_var", "part": {"type": "text", "text": "hi"}})
        assert acc.session_id == "ses_var", key


def test_event_mentioning_api_key_words_is_not_dropped(tmp_path, monkeypatch):
    """Product talk about an Authorization header / api_key must still reach
    TurnResult.text (only the secret value itself is redacted)."""
    from trio_opencode import runner
    import io
    acc = runner.events.TurnAccumulator()
    line = json.dumps({"type": "text", "sessionID": "ses_x",
                       "part": {"id": "prt_1_text-0", "type": "text",
                                "text": "Added the Authorization header and api_key param; key=SECRET123"}})
    runner._handle_stdout_line(line.encode(), io.StringIO(), acc, "SECRET123")
    assert "Authorization header and api_key param" in acc.text
    assert "SECRET123" not in acc.text and "[redacted]" in acc.text


# ---------------------------------------------------------------------------
# oc-classify slice: truncated streams / stderr noise / permission-on-model-
# content / APIError fields / killed-by-signal (brief-classify.md)
# ---------------------------------------------------------------------------


def test_truncated_stream_retried_then_succeeds(tmp_path):
    """A1: a lone ``step_start`` (then exit 0, no step_finish) is the real
    incident this slice fixes -- retried as transient, and the turn
    succeeds once a later attempt actually completes."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "truncated_then_ok", """
        def handle(ctx):
            if ctx.n == 1:
                ctx._emit({"type": "step_start", "sessionID": ctx._sid, "part": {"type": "step-start"}})
                ctx._finished = True  # skip the (v1-only) auto stop-step
            else:
                ctx.text("done after retry")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    assert result.kind == "ok"
    assert result.text == "done after retry"
    assert result.attempts == 2


def test_truncated_stream_retried_until_max_attempts_stays_transient(tmp_path):
    """A1: when it keeps happening every attempt, the final result is still
    ``transient`` (never silently escalated) with ``attempts ==
    max_attempts``."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "truncated_always", """
        def handle(ctx):
            ctx._emit({"type": "step_start", "sessionID": ctx._sid, "part": {"type": "step-start"}})
            ctx._finished = True
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is False
    assert result.kind == "transient"
    assert "truncated event stream" in result.error
    assert result.attempts == spec.max_attempts


def test_classify_mid_step_truncation_is_transient_even_with_earlier_finished_text():
    """A2: [step_start, text("partial"), step_finish(stop), step_start] exit
    0 -> transient (truncated), even though an earlier, already-finished
    step produced text (today that was wrongly "ok")."""
    acc = events.TurnAccumulator()
    acc.feed({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}})
    acc.feed({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "partial"}})
    acc.feed({"type": "step_finish", "sessionID": "ses_1",
              "part": {"type": "step-finish", "reason": "stop"}})
    acc.feed({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}})

    kind, err = events.classify(acc, 0, "")

    assert kind == "transient"
    assert "truncated event stream" in err


def test_completed_turn_not_reclassified_by_info_level_spawn_process_noise(tmp_path, monkeypatch):
    """A4: a completed turn (step_start, text, step_finish stop, exit 0)
    whose stderr contains an INFO-level "spawning process" line echoing the
    model's own shell command (containing "timeout 540"/"network"/"rate
    limit"/"503" -- words the transient free-text scan would otherwise
    match) must still classify as "ok", attempts == 1."""
    monkeypatch.setenv("FAKE_OC_STYLE", "v1")  # auto-closes the step with "stop"
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "spawn_noise_ok", """
        def handle(ctx):
            ctx.stderr(
                'level=INFO msg="running" message="spawning process" '
                'args=["bash","-c","timeout 540 curl network rate limit 503"]'
            )
            ctx.text("all good")
    """)
    env = install_fake(tmp_path, scenario)
    env["FAKE_OC_STYLE"] = "v1"
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.kind == "ok"
    assert result.ok is True
    assert result.attempts == 1
    assert result.text == "all good"


def test_failed_turn_with_warn_timeout_error_line_is_transient(tmp_path):
    """A5: a failed turn (exit 1, no text) whose stderr has the real-world
    WARN line (``failed to load OpenCode provider config ... TimeoutError:
    The operation timed out.``) classifies as transient."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "timeout_fail", """
        def handle(ctx):
            ctx.stderr(
                'level=WARN msg="failed to load OpenCode provider config" '
                'cause="HttpClientError: Transport error '
                '(GET https://opencode.ai/console/api/v2/config) '
                '(cause: TimeoutError: The operation timed out.)"'
            )
            ctx.exit(1)
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=1)

    result = run_turn(spec)

    assert result.kind == "transient"
    assert result.ok is False


def test_permission_text_in_stdout_tool_output_does_not_kill_the_turn(tmp_path):
    """A6: a stdout ``tool_use`` event whose tool output contains the exact
    permission-warning phrase (e.g. this very driver's own source grepped by
    a builder) must NOT be treated as a permission prompt -- the turn
    completes normally. (The companion half of A6 -- a REAL stderr warning
    line still kills the turn as "permission" -- is covered by
    test_permission_warning_kills_quickly above.)"""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "tool_output_permission_text", """
        def handle(ctx):
            ctx.tool("grep", status="completed", input={"pattern": "auto-rejecting"},
                      output="match: permission requested: bash; auto-rejecting")
            ctx.text("done")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=1)

    result = run_turn(spec)

    assert result.kind == "ok"
    assert result.ok is True
    assert result.denials == []


def test_classify_api_error_status_codes():
    """A7: v1 ``APIError`` with ``error.data.statusCode``/``isRetryable``
    (not just a free-text message scan) decides config_error vs transient."""
    acc = events.TurnAccumulator()
    acc.feed({"type": "error", "sessionID": "ses_1",
              "error": {"name": "APIError", "data": {"message": "service unavailable",
                                                       "statusCode": 503, "isRetryable": True}}})
    assert events.classify(acc, 1, "")[0] == "transient"

    acc2 = events.TurnAccumulator()
    acc2.feed({"type": "error", "sessionID": "ses_2",
               "error": {"name": "APIError", "data": {"message": "bad credentials",
                                                        "statusCode": 401, "isRetryable": False}}})
    assert events.classify(acc2, 1, "")[0] == "config_error"


def test_api_error_through_run_turn_retries_503_then_succeeds(tmp_path):
    """A7, end to end: a 503/isRetryable APIError on attempt 1 is retried as
    transient and the turn succeeds on attempt 2."""
    key_path = make_key_file(tmp_path)
    scenario = write_scenario(tmp_path, "api_error_503", """
        def handle(ctx):
            if ctx.n == 1:
                ctx.error("APIError", "service unavailable", statusCode=503, isRetryable=True)
            else:
                ctx.text("done")
    """)
    env = install_fake(tmp_path, scenario)
    spec = base_spec(tmp_path, env, key_path, max_attempts=2, backoff=(0.05,))

    result = run_turn(spec)

    assert result.ok is True
    assert result.attempts == 2


def test_classify_negative_exit_code_killed_by_signal_is_transient():
    """A8: exit code -9 (SIGKILL, e.g. OOM), no events at all -> transient,
    not an unclassified model_error."""
    acc = events.TurnAccumulator()
    kind, err = events.classify(acc, -9, "")
    assert kind == "transient"
    assert "killed by signal 9" in err

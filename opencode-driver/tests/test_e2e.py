"""End-to-end tests: the whole ``trio_opencode.driver``/CLI stack against
``tests/fake_opencode.py`` (never the real ``opencode`` binary — see
``tests/scenarios/`` for the fake's per-test behaviour and
``tests/scenarios/common.py`` for shared helpers).

Every test builds its own tiny ``Config`` directly (bypassing
``config.load_config``'s file/validate path — ``driver.run()`` never calls
``config.validate()`` itself, so there is nothing to relax for fast
timeouts) and points ``PATH``/``FAKE_OC_STATE``/``FAKE_OC_SCENARIO`` at the
fake via ``monkeypatch`` before calling ``trio_opencode.driver.run()``
directly, except where a scenario explicitly needs the ``trio-opencode`` CLI
as a real subprocess (crash+resume, the lock refusal, ``status``/``doctor``)
— those write a small JSON config file and pass it via ``--config``.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode import driver, rootfree, steplib

from fakeoc import install_fake

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DRIVER_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = DRIVER_ROOT / "trio_opencode" / "cli.py"
FAKE_KEY_VALUE = "sk-fake-e2e-key-do-not-leak"


# ---------------------------------------------------------------------------
# Config / fake-binary plumbing
# ---------------------------------------------------------------------------


def make_key_file(tmp_path: Path, value: str = FAKE_KEY_VALUE) -> Path:
    path = tmp_path / "fake-key.txt"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def make_cfg(key_file: Path, *, turn_seconds: float = 20.0, idle_seconds: float = 15.0,
            evaluator_turn_seconds: float | None = None, max_attempts: int = 2,
            backoff=(0.15, 0.15), max_iterations: int = 4) -> config_mod.Config:
    """A tiny, fast Config for tests. ``driver.run()`` never calls
    ``config.validate()`` (only ``doctor.py`` does), so the production
    ``idle_seconds >= 180`` rule simply never applies to this path — no
    test-only override is needed."""
    d = config_mod._default_dict()
    return config_mod.Config(
        opencode_bin="opencode",
        models=dict(d["models"]),
        variants=dict(d["variants"]),
        provider=config_mod.ProviderConfig(id="opencode-go", key_file=str(key_file),
                                          key_env="OPENCODE_API_KEY"),
        timeouts=config_mod.TimeoutsConfig(
            turn_seconds=turn_seconds, idle_seconds=idle_seconds,
            evaluator_turn_seconds=evaluator_turn_seconds or turn_seconds,
        ),
        retries=config_mod.RetriesConfig(max_attempts=max_attempts, backoff_seconds=tuple(backoff)),
        max_iterations=max_iterations,
        root_free=True,
    )


def write_cfg_file(path: Path, key_file: Path, **kwargs) -> Path:
    cfg = make_cfg(key_file, **kwargs)
    doc = {
        "opencode_bin": cfg.opencode_bin,
        "models": cfg.models,
        "variants": cfg.variants,
        "provider": {"id": cfg.provider.id, "key_file": cfg.provider.key_file,
                    "key_env": cfg.provider.key_env},
        "timeouts": {"turn_seconds": cfg.timeouts.turn_seconds,
                    "idle_seconds": cfg.timeouts.idle_seconds,
                    "evaluator_turn_seconds": cfg.timeouts.evaluator_turn_seconds},
        "retries": {"max_attempts": cfg.retries.max_attempts,
                   "backoff_seconds": list(cfg.retries.backoff_seconds)},
        "max_iterations": cfg.max_iterations,
        "root_free": cfg.root_free,
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def install_fake_for_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             scenario: str | Path | None, *, state_dir: Path | None = None) -> dict[str, str]:
    """Points THIS process's env at the fake ``opencode`` (for
    ``driver.run()``-based tests, which spawn turn subprocesses inline) —
    ``fakeoc.install_fake`` returns a full env dict; only ``PATH``,
    ``FAKE_OC_STATE`` and ``FAKE_OC_SCENARIO`` actually need to reach this
    process (everything else it copies from ``os.environ`` is already
    here)."""
    scenario_path = str(SCENARIOS_DIR / scenario) if isinstance(scenario, str) else scenario
    env = install_fake(tmp_path, scenario_path)
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        env["FAKE_OC_STATE"] = str(state_dir)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_OC_STATE", env["FAKE_OC_STATE"])
    if "FAKE_OC_SCENARIO" in env:
        monkeypatch.setenv("FAKE_OC_SCENARIO", env["FAKE_OC_SCENARIO"])
    else:
        monkeypatch.delenv("FAKE_OC_SCENARIO", raising=False)
    return env


def read_calls(env: dict[str, str]) -> list[dict]:
    calls_path = Path(env["FAKE_OC_STATE"]) / "calls.jsonl"
    if not calls_path.exists():
        return []
    return [json.loads(l) for l in calls_path.read_text(encoding="utf-8").splitlines() if l.strip()]


def key_sha8(value: str = FAKE_KEY_VALUE) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def cli_subprocess_env(tmp_path: Path, scenario: str, cfg_path: Path,
                       *, state_dir: Path | None = None) -> dict[str, str]:
    """The environment for a real ``trio-opencode`` (CLI) subprocess: the
    fake ``opencode`` on PATH plus ``TRIO_OPENCODE_CONFIG`` — everything
    else (HOME/XDG isolation) is inherited from THIS process's already
    isolated ``os.environ`` (conftest.py's autouse fixture), since
    ``fakeoc.install_fake`` starts from a copy of it."""
    env = install_fake(tmp_path, str(SCENARIOS_DIR / scenario))
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        env["FAKE_OC_STATE"] = str(state_dir)
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    return env


def spawn_cli(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(CLI_PATH), *args], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)


def run_cli(args: list[str], env: dict[str, str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI_PATH), *args], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def wait_for_file(path: Path, deadline: float = 20.0, poll: float = 0.05) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(poll)
    return path.exists()


# ---------------------------------------------------------------------------
# Shared post-run assertions
# ---------------------------------------------------------------------------


def assert_no_auto_and_devnull_and_key(env: dict[str, str], *, key_value: str = FAKE_KEY_VALUE) -> list[dict]:
    calls = read_calls(env)
    assert calls, "the fake opencode was never invoked"
    for c in calls:
        assert "--auto" not in c["argv"], c
        assert c["stdin_ok"] is True, c
        assert c["has_key"] is True, c
        assert c["key_sha8"] == key_sha8(key_value), c
    return calls


# ---------------------------------------------------------------------------
# Scenario 1: happy path, root-free, 2 disjoint slices, one concurrent wave
# ---------------------------------------------------------------------------


def test_happy_path_root_free_concurrent_wave(git_repo: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    env = install_fake_for_process(tmp_path, monkeypatch, "happy.py")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["harness"] == "opencode"
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]
    assert result["lock"] == "released", result

    # Both builders overlapped: each saw the other's start marker before
    # finishing (common.wait_for_marker in happy.py's builder handler).
    markers = Path(env["FAKE_OC_STATE"]) / "markers"
    assert (markers / "saw-other-a").read_text().strip() == "1", "builder a never saw b running"
    assert (markers / "saw-other-b").read_text().strip() == "1", "builder b never saw a running"

    calls = assert_no_auto_and_devnull_and_key(env)
    builder_pids = {c["pid"] for c in calls if c["agent"] == "trio-builder"}
    assert len(builder_pids) == 2, "expected two distinct builder processes"

    # No omnigent module ever loaded, in-process.
    steplib.assert_no_omnigent_loaded(git_repo)

    # Landed cleanly by fast-forward onto the target branch.
    assert subprocess.run(["git", "-C", str(git_repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip() == ""
    branch = subprocess.run(["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "main"
    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert "slice(a):" in log and "slice(b):" in log, log

    # Lead worktree removed, trio/<slug> branch deleted.
    slug = rootfree.loop_slug("loop")
    assert rootfree.load_record(git_repo, slug).landed is True
    lead_path = Path(rootfree.load_record(git_repo, slug).path)
    assert not lead_path.exists(), "Lead worktree should have been removed at teardown"
    branches = subprocess.run(["git", "-C", str(git_repo), "branch", "--list", f"trio/{slug}"],
                              capture_output=True, text=True).stdout
    assert branches.strip() == "", "trio/<slug> branch should have been deleted"

    # Root mailbox has the result file; registry says shipped/opencode.
    assert (mailbox / ".opencode-result.json").is_file()
    reg = json.loads(driver.registry_path(mailbox).read_text(encoding="utf-8"))
    assert reg["state"] == "finished"
    assert reg["status"] == "shipped"
    assert reg["harness"] == "opencode"


# ---------------------------------------------------------------------------
# Scenario 1b: the SAME happy path, but the fake `opencode` is v1.18.33-style
# end to end (FAKE_OC_STYLE=v1) — proves the v1 compatibility path still
# works, not just v2 (the default everywhere else in this file).
# ---------------------------------------------------------------------------


def test_happy_path_v1_style_end_to_end(git_repo: Path, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    monkeypatch.setenv("FAKE_OC_STYLE", "v1")
    env = install_fake_for_process(tmp_path, monkeypatch, "happy.py")
    env["FAKE_OC_STYLE"] = "v1"

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    calls = assert_no_auto_and_devnull_and_key(env)
    for c in calls:
        assert c["style"] == "v1", c
        assert "--dir" in c["argv"], c
        assert "--standalone" not in c["argv"], c

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert "slice(a):" in log and "slice(b):" in log, log


# ---------------------------------------------------------------------------
# Scenario 1c (bug 1 regression): the Lead's plan turn writes its own
# `phase`/`iteration` into the live STATE.md -- the driver restores its
# cursor right after that turn and the run still ships normally.
# ---------------------------------------------------------------------------


def test_lead_plan_turn_state_corruption_is_restored_and_run_still_ships(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    install_fake_for_process(tmp_path, monkeypatch, "lead_writes_phase.py")

    mailbox = git_repo / "loop"
    # in-place mode (root_free=False): the live mailbox IS `mailbox`, so
    # LOG.md/STATE.md can be read straight back without a land step.
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False)

    assert result["status"] == "shipped", result

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "restored driver-owned STATE key(s) after lead plan it1" in log_text, log_text
    assert "phase 'lead-planned' -> 'lead-running'" in log_text, log_text

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert "slice(a):" in log, log


# ---------------------------------------------------------------------------
# Scenario 2: ITERATE scope=local:<path> -> repair -> SHIP (in-place mode)
# ---------------------------------------------------------------------------


def test_iterate_scope_local_repairs_then_ships_in_place(git_repo: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    env = install_fake_for_process(tmp_path, monkeypatch, "iterate_repair.py")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False)

    assert result["status"] == "shipped", result
    assert result["land"] is None, "in-place mode never lands"

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "| repair |" in log_text, log_text

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — ITERATE" in log, log
    assert "loop: iteration 2 — SHIP" in log, log
    assert "slice(x): fix syntax error" in log, log

    x_py = (git_repo / "x.py").read_text(encoding="utf-8")
    assert "fixed" in x_py

    iterations = result.get("iterations") or []
    roles = [it.get("role") for it in iterations]
    assert "lead" in roles and "repair" in roles, iterations

    assert_no_auto_and_devnull_and_key(env)


# ---------------------------------------------------------------------------
# Scenario 3: transient provider error on the Lead plan turn -> retried
# ---------------------------------------------------------------------------


def test_transient_error_on_lead_plan_turn_is_retried_and_ships(git_repo: Path, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, max_attempts=3, backoff=(0.1, 0.1))
    env = install_fake_for_process(tmp_path, monkeypatch, "transient_plan.py")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", root_free=True)

    assert result["status"] == "shipped", result
    calls = assert_no_auto_and_devnull_and_key(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls


# ---------------------------------------------------------------------------
# Scenario 4: malformed plan output -> one re-prompt in the same session
# ---------------------------------------------------------------------------


def test_malformed_plan_reprompts_once_in_same_session_then_ships(git_repo: Path, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    env = install_fake_for_process(tmp_path, monkeypatch, "malformed_plan.py")

    result = driver.run(git_repo / "loop", cfg, mode="start", root_free=True)

    assert result["status"] == "shipped", result
    calls = read_calls(env)
    lead_calls = [c for c in calls if c["agent"] == "trio-lead"]
    plan_first = next(c for c in lead_calls if c["prompt"].count("PLAN CALL"))
    reprompts = [c for c in lead_calls if "did not include a usable structured result" in c["prompt"]]
    assert len(reprompts) == 1, reprompts
    assert reprompts[0]["session"] == plan_first["resolved_session"], (reprompts[0], plan_first)


def test_malformed_plan_reprompt_also_malformed_leaves_state_resumable(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    monkeypatch.setenv("FAKE_OC_MALFORMED_TWICE", "1")
    install_fake_for_process(tmp_path, monkeypatch, "malformed_plan.py")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", root_free=False)

    assert result["status"] == "error", result
    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["phase"].strip() == "lead-running", state
    assert state["status"].strip() == "running", state


# ---------------------------------------------------------------------------
# Scenario 5: unbound VERDICT.md -> one evaluator re-prompt -> ships
# ---------------------------------------------------------------------------


def test_unbound_verdict_reprompts_evaluator_once_then_ships(git_repo: Path, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    env = install_fake_for_process(tmp_path, monkeypatch, "unbound_verdict.py")

    result = driver.run(git_repo / "loop", cfg, mode="start", root_free=True)

    assert result["status"] == "shipped", result
    calls = read_calls(env)
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 2, eval_calls
    assert eval_calls[1]["session"] == eval_calls[0]["resolved_session"], eval_calls


# ---------------------------------------------------------------------------
# Scenario 12: evaluator always ITERATE (no scope), max_iterations=1
# ---------------------------------------------------------------------------


def test_max_iterations_stops_with_code_4(git_repo: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, max_iterations=1)
    install_fake_for_process(tmp_path, monkeypatch, "always_iterate.py")

    result = driver.run(git_repo / "loop", cfg, mode="start", max_iterations=1, root_free=False)

    assert result["status"] == "max_iterations", result
    assert result["code"] == 4, result


# ---------------------------------------------------------------------------
# Scenario 6: permission hang -> killed quickly, no orphan, lock released
# ---------------------------------------------------------------------------


def test_permission_denial_kills_the_turn_quickly_no_orphan(git_repo: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, turn_seconds=30.0, idle_seconds=20.0, max_attempts=2)
    env = install_fake_for_process(tmp_path, monkeypatch, "permission_hang.py")

    started = time.monotonic()
    result = driver.run(git_repo / "loop", cfg, mode="start", root_free=False)
    elapsed = time.monotonic() - started

    assert result["status"] == "error", result
    assert "permission" in result["reason"].lower(), result
    assert elapsed < 15.0, f"took {elapsed:.1f}s — the permission-hang turn should die almost instantly"
    assert result["lock"] == "released", result

    marker = Path(env["FAKE_OC_STATE"]) / "markers" / "builder-pid"
    pid = int(marker.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ---------------------------------------------------------------------------
# Scenario 7: wall-clock timeout, retried once, still times out -> error
# ---------------------------------------------------------------------------


def test_wall_clock_timeout_retried_once_then_error_process_group_killed(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, turn_seconds=1.5, idle_seconds=10.0, max_attempts=2)
    env = install_fake_for_process(tmp_path, monkeypatch, "wall_timeout.py")

    started = time.monotonic()
    result = driver.run(git_repo / "loop", cfg, mode="start", root_free=False)
    elapsed = time.monotonic() - started

    assert result["status"] == "error", result
    assert "timeout" in result["reason"].lower(), result
    assert "twice" in result["reason"].lower(), result
    assert elapsed < 15.0, f"took {elapsed:.1f}s for two ~1.5s wall timeouts"

    markers_dir = Path(env["FAKE_OC_STATE"]) / "markers"
    pids = [int(p.read_text().strip()) for p in markers_dir.glob("builder-pid-*")]
    assert len(pids) == 2, pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# ---------------------------------------------------------------------------
# Scenario 11: SIGTERM during a turn -> cancelled, end ran, group dead
# ---------------------------------------------------------------------------


def test_sigterm_during_a_turn_cancels_cleanly(git_repo: Path, tmp_path: Path) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=60.0, idle_seconds=30.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "sigterm_hang.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    proc = spawn_cli(["start", "--mailbox", str(mailbox), "--in-place"], env)
    try:
        marker = state_dir / "markers" / "lead-pid"
        assert wait_for_file(marker, deadline=20.0), "Lead plan turn never started"
        lead_pid = int(marker.read_text().strip())

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10.0)

    result = json.loads(out)
    assert result["status"] == "cancelled", result
    assert result["lock"] == "released", result
    # SIGTERM -> code 143 (128 + SIGTERM), the CLI exit code follows it.
    assert result["code"] == 143, result
    assert proc.returncode == 143, (proc.returncode, out, err)

    with pytest.raises(ProcessLookupError):
        os.kill(lead_pid, 0)


def test_sigint_during_a_turn_cancels_with_code_130(git_repo: Path, tmp_path: Path) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=60.0, idle_seconds=30.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "sigterm_hang.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    proc = spawn_cli(["start", "--mailbox", str(mailbox), "--in-place"], env)
    try:
        marker = state_dir / "markers" / "lead-pid"
        assert wait_for_file(marker, deadline=20.0), "Lead plan turn never started"
        lead_pid = int(marker.read_text().strip())

        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=20.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10.0)

    result = json.loads(out)
    assert result["status"] == "cancelled", result
    assert result["lock"] == "released", result
    # SIGINT -> code 130 (128 + SIGINT), unchanged from before.
    assert result["code"] == 130, result
    assert proc.returncode == 130, (proc.returncode, out, err)

    with pytest.raises(ProcessLookupError):
        os.kill(lead_pid, 0)


# ---------------------------------------------------------------------------
# Scenario 9: lock refusal — a concurrent start AND a concurrent resume both
# exit 9 and modify nothing
# ---------------------------------------------------------------------------


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if ".git" in p.parts:
            continue
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def test_lock_refusal_second_start_and_resume_both_exit_9_unchanged(
    git_repo: Path, tmp_path: Path,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=60.0, idle_seconds=30.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "sigterm_hang.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox), "--in-place"], env)
    try:
        marker = state_dir / "markers" / "lead-pid"
        assert wait_for_file(marker, deadline=20.0), "first driver never started its Lead turn"

        before = _snapshot_tree(git_repo)

        second_start = run_cli(["start", "--mailbox", str(mailbox), "--in-place",
                                "--config", str(cfg_path)], env, timeout=20.0)
        second_resume = run_cli(["resume", "--mailbox", str(mailbox), "--in-place",
                                 "--config", str(cfg_path)], env, timeout=20.0)

        after = _snapshot_tree(git_repo)
        assert before == after, "a refused start/resume must modify nothing"
    finally:
        first.send_signal(signal.SIGTERM)
        try:
            first.communicate(timeout=15.0)
        except subprocess.TimeoutExpired:
            first.kill()
            first.communicate(timeout=10.0)

    assert second_start.returncode == 9, (second_start.stdout, second_start.stderr)
    assert second_resume.returncode == 9, (second_resume.stdout, second_resume.stderr)
    for cp in (second_start, second_resume):
        result = json.loads(cp.stdout)
        assert result["code"] == 9, result


# ---------------------------------------------------------------------------
# Scenario 13: `status`/`doctor` CLI
# ---------------------------------------------------------------------------


def test_status_and_doctor_cli(git_repo: Path, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    # doctor.py's own config check enforces idle_seconds >= 180 (production
    # validation, SPEC.md) unlike driver.run() itself — this config file is
    # for `doctor`'s validation, not for actually running a turn.
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, idle_seconds=180.0)
    # One fake install serves both the in-process driver.run() call below
    # (needs THIS process's own PATH/FAKE_OC_STATE/FAKE_OC_SCENARIO, hence
    # install_fake_for_process) and the CLI subprocess calls (which just
    # need the returned env dict).
    cli_env = install_fake_for_process(tmp_path, monkeypatch, "happy.py")

    status = run_cli(["status", "--mailbox", str(git_repo / "loop")], cli_env)
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["mailbox"] == str(git_repo / "loop")
    # No run has happened yet in this mailbox: no driver_json/result/registry.
    assert "driver_json" not in payload

    doctor_result = run_cli(["doctor", "--config", str(cfg_path)], cli_env)
    assert doctor_result.returncode == 0, (doctor_result.stdout, doctor_result.stderr)
    assert FAKE_KEY_VALUE not in doctor_result.stdout
    assert FAKE_KEY_VALUE not in doctor_result.stderr
    report = json.loads(doctor_result.stdout)
    assert report["ok"] is True, report

    # Now run the loop, and check `status` reflects it afterwards.
    result = driver.run(git_repo / "loop", make_cfg(key_file), mode="start", root_free=False)
    assert result["status"] == "shipped", result

    status2 = run_cli(["status", "--mailbox", str(git_repo / "loop")], cli_env)
    assert status2.returncode == 0
    payload2 = json.loads(status2.stdout)
    assert payload2["registry"]["harness"] == "opencode"
    assert payload2["registry"]["status"] == "shipped"


# ---------------------------------------------------------------------------
# Scenario 10: needs_land (land-blocked, then diverged) + `land` CLI
# ---------------------------------------------------------------------------


def test_needs_land_when_root_has_an_overlapping_uncommitted_change(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    install_fake_for_process(tmp_path, monkeypatch, "happy.py")

    # A human has an uncommitted, untracked a.py in the root checkout — the
    # loop's builder also creates a.py, so `git merge --ff-only` at land
    # time refuses ("untracked working tree files would be overwritten").
    (git_repo / "a.py").write_text("a human's local, uncommitted a.py\n", encoding="utf-8")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", root_free=True)

    assert result["status"] == "needs_land", result
    assert result["code"] == 8, result
    assert result["land"]["status"] == "needs_land", result["land"]
    assert result["land"]["phase"] == "land-blocked", result["land"]

    slug = rootfree.loop_slug("loop")
    record = rootfree.load_record(git_repo, slug)
    assert record is not None and not record.landed
    lead_mailbox = Path(record.path) / "loop"
    state = steplib.TL._read_state(lead_mailbox / "STATE.md")
    assert state["status"].strip() == "needs_land", state
    assert "needs_land" in (lead_mailbox / "LOG.md").read_text(encoding="utf-8")

    # The human resolves the conflict; `trio-opencode land` then succeeds.
    # (The driver's own `needs_land` LOG.md line, appended after the run, is
    # uncommitted loop bookkeeping in the Lead worktree — commit it too, the
    # same way a human resolving this would, so the worktree is clean for
    # teardown once it lands.)
    (git_repo / "a.py").unlink()
    subprocess.run(["git", "-C", str(record.path), "add", "-A", "--", "loop"], check=True)
    subprocess.run(["git", "-C", str(record.path), "commit", "-q", "-m",
                    "loop: record needs_land"], check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "trio-opencode-test",
                        "GIT_AUTHOR_EMAIL": "t@example.test",
                        "GIT_COMMITTER_NAME": "trio-opencode-test",
                        "GIT_COMMITTER_EMAIL": "t@example.test"})
    land_out = run_cli(["land", "--mailbox", str(mailbox)], dict(os.environ))
    assert land_out.returncode == 0, (land_out.stdout, land_out.stderr)
    land_result = json.loads(land_out.stdout)
    assert land_result["status"] == "landed", land_result
    assert land_result["teardown"]["worktree_removed"] is True, land_result

    assert not Path(record.path).exists()
    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert (git_repo / "a.py").read_text(encoding="utf-8") == "print('a')\n"


def test_needs_land_diverged_target(git_repo: Path, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    monkeypatch.setenv("FAKE_OC_DIVERGE_TARGET_REPO", str(git_repo))
    install_fake_for_process(tmp_path, monkeypatch, "happy.py")

    mailbox = git_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", root_free=True)

    assert result["status"] == "needs_land", result
    assert result["code"] == 8, result
    assert result["land"]["phase"] == "diverged", result["land"]


# ---------------------------------------------------------------------------
# Scenario 8: crash + resume
# ---------------------------------------------------------------------------


def test_crash_and_resume_kills_orphan_and_ships(git_repo: Path, tmp_path: Path) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "crash_resume.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "first-plan-started"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping plan turn"
        orphan_pid = int(marker.read_text().strip())

        # Kill the DRIVER process itself (not its process group): the
        # sleeping fake-opencode turn (its own session/process group,
        # start_new_session=True) is left running, orphaned.
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log

    slug = rootfree.loop_slug("loop")
    record = rootfree.load_record(git_repo, slug)
    assert record is not None and record.landed
    assert not Path(record.path).exists()


# ---------------------------------------------------------------------------
# Scenario 8c: Bug 1 crash-window regression (oc-fix-eval VERDICT) -- an
# Evaluator turn crashes (SIGKILL) right after corrupting every
# OWNED_STATE_KEYS line, including `phase` itself (to an unknown value,
# `evaluating`); `resume` must restore the persisted pre-turn snapshot and
# re-dispatch THIS iteration's Evaluator directly, never re-running the
# Lead. This scenario's corrupted `phase` is on its own enough to route
# through the snapshot-restore path even with the oc-fix-eval-2 ordering
# bug still present -- the sha-only/phase-only/iteration-only variants below
# (oc-fix-eval-3) are what actually prove the corrupted `evaluated_sha` is
# never reused when the crash leaves `phase` at one of `next()`'s own
# OK-looking values.
# ---------------------------------------------------------------------------


def test_evaluator_crash_mid_turn_resumes_without_rerunning_lead(
    git_repo: Path, tmp_path: Path,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "eval_crash.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "eval-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping evaluator turn"
        orphan_pid = int(marker.read_text().strip())

        # Kill the DRIVER process itself (not its process group): the
        # sleeping fake-opencode evaluator turn is left running, orphaned.
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    # Before the fix: `status: "error"`, reason "pin it1: pin: STATE is
    # iteration 1 phase lead-running, not lead-done of iteration 1".
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls  # the Lead is NOT re-run
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 2, eval_calls  # the killed attempt + the resumed re-dispatch

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log

    slug = rootfree.loop_slug("loop")
    record = rootfree.load_record(git_repo, slug)
    assert record is not None and record.landed
    assert not Path(record.path).exists()


def test_lead_crash_with_owned_key_corruption_still_resumes_to_lead_running(
    git_repo: Path, tmp_path: Path,
) -> None:
    """Regression guard: the same persisted-snapshot restore path used for
    the Evaluator-crash fix above must still land a Lead-turn crash on
    `lead-running` (its own pre-turn value) and re-run the Lead's plan call,
    exactly as the pre-existing fallback did."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "lead_crash_corrupt.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "lead-plan-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping plan turn"
        orphan_pid = int(marker.read_text().strip())

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls  # killed attempt + the resumed re-run

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log


# ---------------------------------------------------------------------------
# Scenario 8d: oc-fix-eval-3 regression -- the oc-fix-eval-2 blocker was that
# `_normalize_resume_phase`'s `_RESUME_OK_PHASES` early return sat ABOVE the
# persisted-snapshot restore, so a crash that corrupted the pin keys or
# `iteration` while leaving `phase` at one of `next()`'s own OK-looking
# values was never restored. Each of the three variants below corrupts only
# ONE such thing and must still ship with exactly one Lead plan call.
# ---------------------------------------------------------------------------


def test_evaluator_crash_corrupting_only_evaluated_sha_resumes_without_reusing_it(
    git_repo: Path, tmp_path: Path,
) -> None:
    """`phase` stays `lead-done` (an OK phase) while `evaluated_sha`/
    `evaluator_attempt` are corrupted to `deadbeef`/`bogus` -- before the
    fix, `_RESUME_OK_PHASES` returned before the snapshot was even read, so
    `resume` reused `deadbeef` as the pin and the run ended in
    `needs_retirement` rather than `shipped`."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "ev_sha_only.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "eval-sha-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping evaluator turn"
        orphan_pid = int(marker.read_text().strip())

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls  # the Lead is NOT re-run
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 2, eval_calls  # the killed attempt + the resumed re-dispatch
    # Both evaluator prompts must carry the SAME (uncorrupted) pin -- the
    # resumed one must never see the corrupted `deadbeef`.
    shas = {m.group(1) for c in eval_calls
           for m in [re.search(r"`evaluated: (\S+)`", c["prompt"])] if m}
    assert shas and "deadbeef" not in shas, (shas, [c["prompt"][:300] for c in eval_calls])
    assert len(shas) == 1, (shas, [c["prompt"][:300] for c in eval_calls])

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log


def test_evaluator_crash_corrupting_phase_to_lead_running_resumes_without_rerunning_lead(
    git_repo: Path, tmp_path: Path,
) -> None:
    """`evaluated_sha`/`evaluator_attempt` stay pinned while `phase` is
    overwritten with `lead-running` -- another OK-looking phase, just the
    wrong one. Before the fix, `_RESUME_OK_PHASES` treated this as nothing
    to restore, so `resume` re-ran the Lead's plan call on top of an
    already-pinned iteration and the run ended in `error`."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "ev_phase_leadrunning.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "eval-phaselr-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping evaluator turn"
        orphan_pid = int(marker.read_text().strip())

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls  # the Lead is NOT re-run
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 2, eval_calls  # the killed attempt + the resumed re-dispatch

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log


def test_evaluator_crash_corrupting_only_iteration_resumes_without_rerunning_lead(
    git_repo: Path, tmp_path: Path,
) -> None:
    """`phase`/`evaluated_sha`/`evaluator_attempt` all stay as they were
    (`lead-done` and the real pin); only `iteration` is corrupted to `7`.
    Before the fix the corrupted `iteration` was never restored (it plays
    no part in the fallback's own guess), so it silently persisted past
    `resume`."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "ev_iter_only.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "eval-iter-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping evaluator turn"
        orphan_pid = int(marker.read_text().strip())

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls  # the Lead is NOT re-run
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 2, eval_calls  # the killed attempt + the resumed re-dispatch
    # The resumed Evaluator's prompt must state iteration 1, never the
    # corrupted `7`.
    assert "iteration 1" in eval_calls[-1]["prompt"], eval_calls[-1]["prompt"][:300]

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log


def test_repair_turn_crash_with_owned_key_corruption_still_resumes_and_ships(
    git_repo: Path, tmp_path: Path,
) -> None:
    """The repair turn's first attempt corrupts `phase` (to `repairing`,
    not an OK phase) and `iteration` (to `9`) before sleeping forever. The
    persisted pre-turn snapshot (`phase: repair-running`, `iteration: 2`)
    must be restored on `resume`, re-dispatching the repair turn (not the
    Lead) so the run goes on to fix x.py and ship."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "repair_crash.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "repair-crashed"
        assert wait_for_file(marker, deadline=20.0), "first driver never reached the sleeping repair turn"
        orphan_pid = int(marker.read_text().strip())

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls  # the Lead is NOT re-run
    repair_calls = [c for c in calls if c["agent"] == "trio-repair"]
    assert len(repair_calls) == 2, repair_calls  # the killed attempt + the resumed re-dispatch

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "| repair |" in log_text, log_text

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — ITERATE" in log, log
    assert log.count("loop: iteration 2 — SHIP") == 1, log


# ---------------------------------------------------------------------------
# Scenario 8b: TWO concurrent builders both recorded live in `.driver.json`
# `turns`, and BOTH killed on resume (extends scenario 8 to concurrency)
# ---------------------------------------------------------------------------


def test_concurrent_builders_both_recorded_in_driver_json_turns_and_both_killed_on_resume(
    git_repo: Path, tmp_path: Path,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0, idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    env = cli_subprocess_env(tmp_path, "concurrent_crash_resume.py", cfg_path, state_dir=state_dir)

    mailbox = git_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker_a = state_dir / "markers" / "builder-first-a"
        marker_b = state_dir / "markers" / "builder-first-b"
        assert wait_for_file(marker_a, deadline=20.0), "builder a never started"
        assert wait_for_file(marker_b, deadline=20.0), "builder b never started"
        orphan_a = int(marker_a.read_text().strip())
        orphan_b = int(marker_b.read_text().strip())
        assert orphan_a != orphan_b

        # Both live turns must be visible in `.driver.json`'s `turns` dict
        # (the Lead worktree's live mailbox — root-free is the default)
        # before the driver is killed.
        slug = rootfree.loop_slug("loop")
        record = None
        deadline = time.monotonic() + 20.0
        driver_json = {}
        while time.monotonic() < deadline:
            record = rootfree.load_record(git_repo, slug)
            if record is not None:
                driver_json = driver._read_json(record.live_mailbox / driver.DRIVER_FILE)
                if len(driver_json.get("turns") or {}) >= 2:
                    break
            time.sleep(0.1)
        assert record is not None
        turns = driver_json.get("turns") or {}
        assert len(turns) == 2, driver_json
        pids = {t["pid"] for t in turns.values()}
        assert pids == {orphan_a, orphan_b}, (pids, orphan_a, orphan_b)

        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_a, 0)  # still alive: real orphans
        os.kill(orphan_b, 0)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_a, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(orphan_b, 0)

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log
    assert "slice(a):" in log and "slice(b):" in log, log


# ---------------------------------------------------------------------------
# NEW RESILIENCE scenario (SPEC.md task 8, top acceptance item): builder A
# finishes and commits; builder B fails transient on every attempt,
# exhausting the runner's own retries AND the driver's second try -> the
# whole run stops status error with STATE resumable. Then (scenario healthy)
# `resume` ships. A's slice(a) commit must appear exactly once in the final
# history — see resilience_partial_wave.py's own docstring for exactly what
# native's begin-reclaim does with A's unmerged branch.
# ---------------------------------------------------------------------------


def test_resilience_partial_wave_one_builder_stuck_transient_then_resume_ships(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, max_attempts=2, backoff=(0.05, 0.05))
    env = install_fake_for_process(tmp_path, monkeypatch, "resilience_partial_wave.py")

    mailbox = git_repo / "loop"

    # --- First run: A commits, B fails transient forever -> status error,
    # STATE.md left resumable.
    first = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert first["status"] == "error", first
    assert "twice" in (first.get("reason") or "").lower(), first
    assert "transient" in (first.get("reason") or "").lower(), first

    slug = rootfree.loop_slug("loop")
    record = rootfree.load_record(git_repo, slug)
    assert record is not None and not record.landed, "first run must not have landed anything"
    lead_mailbox = Path(record.path) / "loop"
    state = steplib.TL._read_state(lead_mailbox / "STATE.md")
    assert state["status"].strip() == "running", state
    assert state["phase"].strip() == "lead-running", state

    # A's branch exists, complete, but was never merged into the Lead's HEAD
    # (the wave's integrate/cleanup step never ran — B's DriverStop surfaced
    # before it).
    a_branches = subprocess.run(
        ["git", "-C", str(record.path), "branch", "--list", "trio-oc/*"],
        capture_output=True, text=True,
    ).stdout
    assert "-a" in a_branches, a_branches
    lead_log_before = subprocess.run(["git", "-C", str(record.path), "log", "--format=%s"],
                                     capture_output=True, text=True).stdout
    assert "slice(a):" not in lead_log_before, "A's commit must not be on the Lead HEAD yet"

    # --- The scenario turns healthy for B; resume.
    (Path(env["FAKE_OC_STATE"]) / "markers").mkdir(parents=True, exist_ok=True)
    (Path(env["FAKE_OC_STATE"]) / "markers" / "healthy-b").write_text("1", encoding="utf-8")

    second = driver.run(mailbox, cfg, mode="resume", max_iterations=4, root_free=True)

    assert second["status"] == "shipped", second

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    # A's completed work is reused, not redone: begin's reclaim merged its
    # branch (the Lead worktree is re-attached without re-seeding, so STATE
    # stays lead-running of iteration 1), the resumed plan prompt says so,
    # and builder a ran exactly once across both runs.
    assert log.count("slice(a):") == 1, log
    assert log.count("slice(b):") == 1, log
    assert "loop: iteration 1 — SHIP" in log, log
    a_sha_before = re.search(r"trio-oc/\S+-a", a_branches)
    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls
    assert "PREVIOUS RUN: the driver merged" not in plan_calls[0]["prompt"]
    assert "PREVIOUS RUN: the driver merged" in plan_calls[1]["prompt"], plan_calls[1]["prompt"]
    a_builder_calls = [c for c in calls if c["agent"] == "trio-builder" and "slice `a`" in c["prompt"]]
    assert len(a_builder_calls) == 1, a_builder_calls
    second_reclaimed = second.get("reclaimed_builders") or {}
    assert [m["id"] for m in second_reclaimed.get("merged") or []] == ["a"], second_reclaimed
    assert a_sha_before is not None


# ---------------------------------------------------------------------------
# Scenario 14 (optional): a verified human answer reaches the Lead plan
# ---------------------------------------------------------------------------

LEDGER_PATH = steplib.REPO_ROOT / "metrics" / "human_ledger.py"


def _ledger_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("e2e_human_ledger", LEDGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verified_human_answer_reaches_lead_plan_prompt(git_repo: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional per the task brief: ``metrics/human_ledger.py`` set up
    offline with a tmp ledger dir (``TRIO_DASH_STATE_DIR``), following the
    exact recipe native/tests/test_eval4.py's ``_answer``/``_stopped``
    helpers use — stop the mailbox at NEEDS_HUMAN (committed), record a
    signed ledger answer bound to that stop, HUMAN.md carries the matching
    entry, then re-arm STATE.md at the same iteration so `next()` treats
    this as a fresh Lead dispatch that should carry the verified answer."""
    lg = _ledger_module()
    mailbox = git_repo / "loop"

    (mailbox / "STATE.md").write_text("iteration: 1\nstatus: needs_human\nphase: idle\n",
                                      encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: NEEDS_HUMAN\n# Verdict — iteration 1\n",
                                        encoding="utf-8")
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m",
                    "loop: iteration 1 — NEEDS_HUMAN"], check=True, env=os.environ)

    state_dir = tmp_path / "ledger-state"
    key = lg.load_key(state_dir, create=True)
    at = "2026-09-30T12:00:00Z"
    answer_id = "abc123def456"
    body = "Human check: PASSED — go ahead with the plain approach."
    lg.append_record(state_dir, lg.make_record(
        key, answer_id=answer_id, loop="e2e", mailbox=mailbox, root_mailbox=mailbox,
        iteration=1, at=at, body=body))
    sig = lg.entry_sig(key, at, answer_id, 1, body)
    with open(mailbox / "HUMAN.md", "a", encoding="utf-8") as fh:
        fh.write(f"\n## {at} — answer {answer_id} — iteration 1 — trio-dash {sig}\n\n"
                 f"{lg.quote_body(body)}\n")

    # Re-arm at the same iteration the stop recorded, as a fresh dispatch.
    (mailbox / "STATE.md").write_text("iteration: 1\nstatus: running\nphase: idle\n",
                                      encoding="utf-8")

    monkeypatch.setenv("TRIO_DASH_STATE_DIR", str(state_dir))
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file)
    env = install_fake_for_process(tmp_path, monkeypatch, "iterate_repair.py")

    result = driver.run(mailbox, cfg, mode="start", root_free=False)

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert plan_calls, result
    assert "## Verified human answer (driver)" in plan_calls[0]["prompt"], plan_calls[0]["prompt"]
    assert "Human check: PASSED" in plan_calls[0]["prompt"]


# ---------------------------------------------------------------------------
# Scenario 14: a model echoing the raw provider key never leaks it anywhere
# ---------------------------------------------------------------------------


def test_model_echoing_key_never_leaks_via_cli(git_repo: Path, tmp_path: Path) -> None:
    """``leak_key.py``'s Lead plan turn echoes the raw fake key via
    ``ctx.text()`` and ``ctx.stderr()``, in a ``DENIED:`` line, and inside an
    error message. The key must be absent from the CLI's own stdout/stderr,
    the parsed result, ``.opencode-result.json``, the registry record,
    ``.driver.json`` (if it still exists) and every file under the run's
    logs dir — run through the real ``trio-opencode start`` subprocess, not
    ``driver.run()`` in-process, so the CLI's own stdout/stderr are covered
    too."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_cfg_file(tmp_path / "cfg.json", key_file, max_attempts=1,
                              backoff=(0.05,), turn_seconds=20.0, idle_seconds=15.0)
    env = cli_subprocess_env(tmp_path, "leak_key.py", cfg_path)

    mailbox = git_repo / "loop"
    cp = run_cli(["start", "--mailbox", str(mailbox), "--in-place"], env, timeout=30.0)

    assert FAKE_KEY_VALUE not in cp.stdout, cp.stdout
    assert FAKE_KEY_VALUE not in cp.stderr, cp.stderr

    result = json.loads(cp.stdout)
    assert result["status"] == "error", result
    assert FAKE_KEY_VALUE not in json.dumps(result)

    # The safe, key-free DENIED line the scenario also emitted still made it
    # through (only the key-bearing lines were dropped), and shows up as a
    # role denial — itself key-free.
    denials = result.get("role_denials") or []
    assert any("safe line" in d.get("text", "") for d in denials), denials
    for d in denials:
        assert FAKE_KEY_VALUE not in d.get("text", "")

    result_path = mailbox / driver.RESULT_FILE
    assert result_path.is_file()
    assert FAKE_KEY_VALUE not in result_path.read_text(encoding="utf-8")

    reg_path = driver.registry_path(mailbox)
    if reg_path.is_file():
        assert FAKE_KEY_VALUE not in reg_path.read_text(encoding="utf-8")

    driver_json_path = mailbox / driver.DRIVER_FILE
    if driver_json_path.is_file():
        assert FAKE_KEY_VALUE not in driver_json_path.read_text(encoding="utf-8")

    logs_dir = Path(result["logs_dir"])
    assert logs_dir.is_dir(), result
    checked = 0
    for p in logs_dir.rglob("*"):
        if p.is_file():
            checked += 1
            assert FAKE_KEY_VALUE not in p.read_text(encoding="utf-8", errors="replace"), p
    assert checked > 0, "expected at least one per-turn log file under logs_dir"


# ---------------------------------------------------------------------------
# r19 frozen acceptance: end-to-end through a real, schema-valid pack run for
# real by metrics/trio-acceptance.py's own sandboxed check execution (only
# the opencode turns are faked — see tests/scenarios/acceptance.py). Mirrors
# native/tests/test_r19_acceptance.py's write_pack/CHECK/GOAL fixture.
# ---------------------------------------------------------------------------

ACC_GOAL = ("# Goal\n"
           "Ship app.py: `python3 app.py N` prints hello N for every N.\n"
           "Keep the README.\n")


def make_acc_repo(tmp_path: Path) -> Path:
    root = tmp_path / "accproduct"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text(ACC_GOAL, encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    subprocess.run(["git", "add", "loop"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "loop: init"], cwd=root, check=True)
    return root


def run_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path, *, mode: str,
                   checks: int = 5, extra_env: dict[str, str] | None = None,
                   root_free: bool = False, max_iterations: int = 4,
                   jobs_inline: bool = True, job_wait: int | None = None) -> tuple[dict, dict]:
    """Runs the ``acceptance`` scenario (``$FAKE_OC_ACC_MODE=mode``) through
    the real driver with ``acceptance=True``. ``jobs_inline`` runs the
    helper's detached acceptance jobs (freeze/acceptance-run/apply)
    synchronously in this process (fast, deterministic); a caller that wants
    to exercise the real pending-job path sets ``jobs_inline=False`` and
    ``job_wait=0`` instead (see ``test_acceptance_long_op_pending_...``)."""
    key_file = make_key_file(tmp_path)
    cfg = make_cfg(key_file, max_iterations=max_iterations)
    env = install_fake_for_process(tmp_path, monkeypatch, "acceptance.py")
    monkeypatch.setenv("FAKE_OC_ACC_MODE", mode)
    monkeypatch.setenv("FAKE_OC_ACC_CHECKS", str(checks))
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)
    if jobs_inline:
        monkeypatch.setenv("TRIO_NATIVE_JOBS", "inline")
    else:
        monkeypatch.delenv("TRIO_NATIVE_JOBS", raising=False)
    if job_wait is not None:
        monkeypatch.setenv("TRIO_NATIVE_JOB_WAIT_S", str(job_wait))
    mailbox = repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=max_iterations,
                        root_free=root_free, acceptance=True)
    return result, env


def test_acceptance_ship_through_a_frozen_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) The full loop: a fake author turn writes a valid 5-check pack
    (all failing at base), the plan covers every check, coverage/dispatch/
    gate all pass, the builder implements app.py, the evaluator SHIPs with
    retirement commits, and apply keeps SHIP."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="ship")

    assert result["status"] == "shipped", result
    acc = result["acceptance"]
    assert acc["enabled"] is True
    assert acc["status"] == "frozen", acc
    assert acc["checks"] == 5, acc
    assert acc["audit"]["limited"] is True  # no author transcript was persisted
    assert acc["ship_refused"] == []

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 1, plan_calls
    acc_calls = [c for c in calls if c["agent"] == "trio-acceptance"]
    assert len(acc_calls) == 1, acc_calls
    builder_calls = [c for c in calls if c["agent"] == "trio-builder"]
    assert len(builder_calls) == 1, builder_calls
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert len(eval_calls) == 1, eval_calls
    assert "FROZEN ACCEPTANCE @" in eval_calls[0]["prompt"]
    assert "5/5 PASS" in eval_calls[0]["prompt"], eval_calls[0]["prompt"]
    assert "metrics/trio-acceptance.py run" in eval_calls[0]["prompt"]

    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert any(s.startswith("acceptance: freeze 5 checks") for s in log.splitlines()), log

    result_file = json.loads((repo / "loop" / driver.RESULT_FILE).read_text(encoding="utf-8"))
    assert result_file["acceptance"]["status"] == "frozen"
    assert result_file["acceptance"]["checks"] == 5
    # the OpenCode author audit is recorded as such -- not silently as
    # "native" -- end to end through op_acceptance_freeze: the result
    # file's digest and the frozen MANIFEST.json's author record.
    assert result_file["acceptance"]["audit"]["path"] == "opencode", result_file["acceptance"]
    manifest = json.loads((repo / "loop" / "acceptance" / "MANIFEST.json").read_text())
    assert manifest["author"]["path"] == "opencode", manifest["author"]


def test_acceptance_ship_refused_by_the_frozen_gate_then_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) The builder under-implements iteration 1 (3 of 5 checks); the
    evaluator still writes SHIP, but the frozen-acceptance gate inside
    `apply` turns it into ITERATE and surfaces the failures to the next
    Lead plan prompt; iteration 2's builder finishes the rest and it ships
    for real."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="ship_refused")

    assert result["status"] == "shipped", result
    assert result["acceptance"]["ship_refused"] == [{"iteration": 1, "became": "ITERATE"}], result

    log_text = (repo / "loop" / "LOG.md").read_text(encoding="utf-8")
    assert "SHIP refused by the acceptance gate (verdict becomes ITERATE)" in log_text, log_text

    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "acceptance gate refused SHIP (ITERATE)" in log, log
    assert "loop: iteration 2 — SHIP" in log, log

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls
    assert "ACCEPTANCE ERRORS FROM THE DRIVER" in plan_calls[1]["prompt"], plan_calls[1]["prompt"]
    assert "FAIL" in plan_calls[1]["prompt"]
    builder_calls = [c for c in calls if c["agent"] == "trio-builder"]
    assert len(builder_calls) == 2, builder_calls  # iteration 1 (partial) + iteration 2 (fix)


def test_acceptance_coverage_refusal_recovers_after_one_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c, part 1) The first plan leaves the last check unmapped; the
    driver's one re-plan (same prompt call, now carrying the refusal)
    covers it and the loop proceeds normally to SHIP."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="coverage_ok")

    assert result["status"] == "shipped", result
    assert result["acceptance"]["coverage_refusals"], result
    assert result["acceptance"]["replanned"] is True

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls
    assert "COVERAGE REFUSED" not in plan_calls[0]["prompt"]
    assert "COVERAGE REFUSED" in plan_calls[1]["prompt"], plan_calls[1]["prompt"]
    assert "ACC-05" in plan_calls[1]["prompt"]
    builder_calls = [c for c in calls if c["agent"] == "trio-builder"]
    assert len(builder_calls) == 1, builder_calls


def test_acceptance_coverage_refusal_stops_after_second_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c, part 2) Both the first plan AND the re-plan leave a check
    unmapped: the second refusal stops the loop with status error, before
    any builder or evaluator ever ran."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="coverage_stop")

    assert result["status"] == "error", result
    # The helper's own `coverage` op already stops the loop on a second
    # refusal (`op_coverage`'s `attempt >= 2` branch raises
    # `AcceptanceError("acceptance-coverage", ...)` before returning), so
    # the reason comes back through `steplib.acc_stop` as "acceptance
    # acceptance-coverage: ..." — `_coverage_gate`'s own `attempt == 2`
    # DriverStop (a second, redundant guard) is never reached.
    assert "acceptance-coverage" in (result.get("reason") or ""), result
    assert "refused again" in (result.get("reason") or ""), result

    calls = read_calls(env)
    plan_calls = [c for c in calls if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]
    assert len(plan_calls) == 2, plan_calls
    assert "COVERAGE REFUSED" in plan_calls[1]["prompt"]
    assert not [c for c in calls if c["agent"] == "trio-builder"], "no builder should have run"
    assert not [c for c in calls if c["agent"] == "trio-evaluator"], "no evaluator should have run"


def test_acceptance_contaminated_author_reauthors_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) The author's first attempt makes a tool call reading the product
    repository's own mailbox (a persisted tool_use event, exactly what the
    audit's `audit_transcript` treats as a forbidden-root read); the driver
    discards that attempt and re-authors once from a prompt carrying the
    contamination notice."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="contaminated",
                                 extra_env={"FAKE_OC_ACC_REPO": str(repo)})

    assert result["status"] == "shipped", result
    assert result["acceptance"]["status"] == "frozen", result["acceptance"]
    assert result["acceptance"]["author_attempts"] == 2, result["acceptance"]

    calls = read_calls(env)
    acc_calls = [c for c in calls if c["agent"] == "trio-acceptance"]
    assert len(acc_calls) == 2, acc_calls
    assert "PREVIOUS ATTEMPT WAS DISCARDED" in acc_calls[1]["prompt"], acc_calls[1]["prompt"]


def test_acceptance_validation_retry_drops_checks_passing_at_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) The author's first pack has two checks that already pass at the
    base commit; the helper asks for one validation retry (the prompt
    carries the dropped list), and freezes the remaining 4."""
    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="retry")

    assert result["status"] == "shipped", result
    assert result["acceptance"]["checks"] == 4, result["acceptance"]

    calls = read_calls(env)
    acc_calls = [c for c in calls if c["agent"] == "trio-acceptance"]
    assert len(acc_calls) == 2, acc_calls
    assert "RETRY" in acc_calls[1]["prompt"], acc_calls[1]["prompt"]
    assert "ACC-05" in acc_calls[1]["prompt"] and "ACC-06" in acc_calls[1]["prompt"]


def test_acceptance_long_op_pending_then_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(f) With the detached-job wait forced to 0 (``TRIO_NATIVE_JOB_WAIT_S``)
    and real forking left on (``TRIO_NATIVE_JOBS`` NOT ``inline``), every
    acceptance job (freeze/acceptance-run/apply) answers `pending` on its
    first poll and the real result on the next — `steplib.step_long`'s own
    pending path, not merely an in-process synchronous call."""
    pending_seen: list[bool] = []
    real_step_long = steplib.step_long

    def spying_step_long(fn, *a, **kw):  # noqa: ANN001
        def wrapped(*a2, **kw2):
            r = fn(*a2, **kw2)
            if r.get("pending"):
                pending_seen.append(True)
            return r
        return real_step_long(wrapped, *a, **kw)

    monkeypatch.setattr(steplib, "step_long", spying_step_long)

    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="ship",
                                 jobs_inline=False, job_wait=0)

    assert result["status"] == "shipped", result
    assert pending_seen, ("expected at least one detached acceptance job to answer "
                          "pending at least once before resolving")


def test_acceptance_apply_long_op_exhausted_stops_cleanly_not_a_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 3 regression, driven through the real acceptance e2e flow (coverage,
    builder, evaluator all run and SHIP normally): when the `apply` job never
    resolves, `steplib.step_long`'s own exhausted-polling result (`ok: False`)
    makes `driver._apply` raise a clean DriverStop instead of `_drive`
    crashing on `a["stop"]` with a `KeyError` -- the run ends with status
    "error", code 3, naming the stuck op, not a traceback."""
    real_step_long = steplib.step_long

    def stuck_apply_step_long(fn, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        if fn is steplib.apply:
            return {"ok": False, "op": "apply", "error": "apply still running after 12 polls"}
        return real_step_long(fn, *a, **kw)

    monkeypatch.setattr(steplib, "step_long", stuck_apply_step_long)

    repo = make_acc_repo(tmp_path)
    result, env = run_acceptance(tmp_path, monkeypatch, repo, mode="ship")

    assert result["status"] == "error", result
    assert result.get("code") == 3, result
    assert "still running" in (result.get("reason") or ""), result

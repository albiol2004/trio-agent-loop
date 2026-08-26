"""Tests for the open-loop pass-through in portable/driver.sh.

Covers api:OpenLoopRunner + api:OpenLoopPromptEnv from the frozen contracts
in loop-open-loop-drivers/PLAN.md: TRIO_MODE/POLL_SECONDS argv assembly at
the bottom of driver.sh, the OPEN-LOOP CONTEXT block build_prompt prepends,
and the lockstep/open-loop CLI paths in metrics/trio_loop.py itself.

Mirrors the runner-faking style of test_portable_driver.py: a fake `python3`
placed earlier on PATH records the argv driver.sh would have exec'd, so
these tests never drive a real loop through the shell shim.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
DRIVER = ROOT / "portable" / "driver.sh"
LEAD_PROMPT = ROOT / "portable" / "prompts" / "lead.md"
EVALUATOR_PROMPT = ROOT / "portable" / "prompts" / "evaluator.md"
TRIO_LOOP = ROOT / "metrics" / "trio_loop.py"


def write_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_mailbox_with_goal(tmp_path: Path) -> Path:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    return mailbox


def make_fake_python3(tmp_path: Path, argv_file: Path) -> Path:
    """A stand-in `python3` recording argv, placed ahead of the real one."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "python3"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def run_driver_with_fake_python(
    mailbox: Path, tmp_path: Path, argv_file: Path, *args: str, **env_extra: str
) -> subprocess.CompletedProcess[str]:
    fake_bin = make_fake_python3(tmp_path, argv_file)
    environment = os.environ.copy()
    environment.update(
        {
            "LOOP_DIR": str(mailbox),
            "HARNESS": "generic",
            "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
            **env_extra,
        }
    )
    return subprocess.run(
        [str(DRIVER), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("trio_mode", "poll_seconds", "expect_present", "expect_absent"),
    [
        ("open-loop", None, ["--open-loop"], ["--lockstep"]),
        ("lockstep", None, ["--lockstep"], ["--open-loop"]),
        (None, None, [], ["--open-loop", "--lockstep"]),
        (None, "7", ["--poll-seconds", "7"], []),
    ],
)
def test_mode_and_poll_argv_pass_through(
    tmp_path: Path, trio_mode, poll_seconds, expect_present, expect_absent
) -> None:
    """TRIO_MODE/POLL_SECONDS map onto the exec'd python argv, or don't."""
    mailbox = make_mailbox_with_goal(tmp_path)
    argv_file = tmp_path / "argv.txt"
    env_extra = {}
    if trio_mode is not None:
        env_extra["TRIO_MODE"] = trio_mode
    if poll_seconds is not None:
        env_extra["POLL_SECONDS"] = poll_seconds

    result = run_driver_with_fake_python(mailbox, tmp_path, argv_file, "5", **env_extra)

    assert result.returncode == 0, result.stderr
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    for token in expect_present:
        assert token in argv, argv
    for token in expect_absent:
        assert token not in argv, argv


def test_unrecognised_trio_mode_fails_loudly(tmp_path: Path) -> None:
    """An unknown TRIO_MODE value errors instead of being silently dropped."""
    mailbox = make_mailbox_with_goal(tmp_path)
    argv_file = tmp_path / "argv.txt"

    result = run_driver_with_fake_python(
        mailbox, tmp_path, argv_file, "5", TRIO_MODE="bogus"
    )

    assert result.returncode != 0
    assert "TRIO_MODE" in result.stderr
    assert not argv_file.exists()


def test_build_prompt_byte_identical_when_trio_mode_unset(tmp_path: Path) -> None:
    """Lockstep (no TRIO_MODE) prompt rendering is unchanged from HEAD."""
    mailbox = make_mailbox_with_goal(tmp_path)

    result = subprocess.run(
        [str(DRIVER), "--run-role", "lead"],
        cwd=ROOT,
        env={
            **os.environ,
            "LOOP_DIR": str(mailbox),
            "HARNESS": "generic",
            "RUN_LEAD": "cat",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    override = (
        f"MAILBOX OVERRIDE: this run uses `{mailbox}/` as the loop mailbox — "
        f"every `loop/` path in the instructions below resolves to `{mailbox}/`.\n\n"
    )
    expected = override + LEAD_PROMPT.read_text(encoding="utf-8")
    assert result.stdout == expected


def test_open_loop_context_block_precedes_prompt(tmp_path: Path) -> None:
    """TRIO_MODE=open-loop prepends an OPEN-LOOP CONTEXT block naming kind/slice/sha."""
    mailbox = make_mailbox_with_goal(tmp_path)
    sha = "0123456789abcdef0123456789abcdef01234567"

    result = subprocess.run(
        [str(DRIVER), "--run-role", "evaluator"],
        cwd=ROOT,
        env={
            **os.environ,
            "LOOP_DIR": str(mailbox),
            "HARNESS": "generic",
            "RUN_EVAL": "cat",
            "TRIO_MODE": "open-loop",
            "TRIO_KIND": "slice-eval",
            "TRIO_SLICE": "demo",
            "TRIO_SHA": sha,
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OPEN-LOOP CONTEXT:")
    first_line = result.stdout.splitlines()[0]
    assert "slice-eval" in first_line
    assert "demo" in first_line
    assert sha in first_line
    # The rest of the prompt (past the context + override blocks) is untouched.
    assert result.stdout.rstrip("\n").endswith(
        EVALUATOR_PROMPT.read_text(encoding="utf-8").rstrip("\n")
    )


def make_lockstep_mailbox(tmp_path: Path) -> Path:
    """A throwaway mailbox with an empty (but parseable) slices block."""
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    # An empty slices: block is parseable with zero entries, so the
    # per-slice commit gate has nothing code-changing to require -- verified
    # by hand: `trio-shadow.py --require-commits` exits 0 against it.
    (mailbox / "PLAN.md").write_text("```yaml\nslices:\n```\n", encoding="utf-8")
    return mailbox


def test_lockstep_cli_end_to_end_still_ships(tmp_path: Path) -> None:
    """trio_loop.py run --lockstep still runs the CLI path end to end."""
    mailbox = make_lockstep_mailbox(tmp_path)
    lead = write_script(
        tmp_path / "lead.sh",
        (
            'iteration="$(awk -F": " \'/^iteration:/{print $2}\' '
            '"$LOOP_DIR/STATE.md")"\n'
            'printf "%s\\n" "- iter ${iteration} | lead | completed" '
            '>> "$LOOP_DIR/LOG.md"\n'
        ),
    )
    evaluator = write_script(
        tmp_path / "evaluator.sh",
        (
            'iteration="$(awk -F": " \'/^iteration:/{print $2}\' '
            '"$LOOP_DIR/STATE.md")"\n'
            'printf "%s\\n" "- iter ${iteration} | evaluator | checked" '
            '>> "$LOOP_DIR/LOG.md"\n'
            'printf "%s\\n" "VERDICT: SHIP" > "$LOOP_DIR/VERDICT.md"\n'
        ),
    )

    result = subprocess.run(
        [
            sys.executable,
            str(TRIO_LOOP),
            "run",
            "--mailbox",
            str(mailbox),
            "--max-iterations",
            "1",
            "--lockstep",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "LOOP_DIR": str(mailbox),
            "HARNESS": "generic",
            "RUN_LEAD": str(lead),
            "RUN_EVAL": str(evaluator),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    state = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: shipped" in state
    assert not (mailbox / ".session.json").exists()


def test_open_loop_without_queue_exits_3(tmp_path: Path) -> None:
    """--open-loop on a mailbox without QUEUE.md exits 3 naming QUEUE.md."""
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(TRIO_LOOP),
            "run",
            "--mailbox",
            str(mailbox),
            "--max-iterations",
            "1",
            "--open-loop",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "QUEUE.md" in result.stderr

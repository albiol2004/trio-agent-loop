"""Integration tests for the portable shell shim and Python loop boundary.

The default driver must exec Python so one process owns the mailbox lock.
Role mode only dispatches a harness command, so Bash must not create a lock
or recurse into Python.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
DRIVER = ROOT / "portable" / "driver.sh"

PLAN_LOOP_ONLY = """\
```yaml
slices:
  - id: mailbox-chores
    writes: [loop/STATE.md, loop/LOG.md, loop/VERDICT.md]
    reads: []
```
"""

PLAN_CODE = """\
```yaml
slices:
  - id: application
    writes: [app.py]
    reads: []
```
"""


def make_mailbox(tmp_path: Path, plan: str = PLAN_LOOP_ONLY) -> Path:
    """Create the mailbox files consumed by the real loop."""
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    return mailbox


def write_script(path: Path, body: str) -> Path:
    """Write an executable fake harness command."""
    path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def run_driver(
    mailbox: Path,
    *args: str,
    **variables: str,
) -> subprocess.CompletedProcess[str]:
    """Run the driver from the repository root with an isolated mailbox."""
    environment = os.environ.copy()
    environment.update(
        {
            "LOOP_DIR": str(mailbox),
            "HARNESS": "generic",
            **variables,
        }
    )
    return subprocess.run(
        [str(DRIVER), *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_driver_has_valid_bash_syntax() -> None:
    """The portable entrypoint remains valid Bash."""
    result = subprocess.run(
        ["bash", "-n", str(DRIVER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def successful_harnesses(tmp_path: Path) -> tuple[Path, Path]:
    """Create Lead and Evaluator commands for a shipped loop."""
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
    return lead, evaluator


def test_driver_executes_python_loop_and_ships(
    tmp_path: Path,
    successful_harnesses: tuple[Path, Path],
) -> None:
    """The shim delegates iteration and verdict handling to Python."""
    mailbox = make_mailbox(tmp_path)
    lead, evaluator = successful_harnesses

    # exec keeps Python as the single owner of the mailbox lock.
    result = run_driver(
        mailbox,
        "2",
        RUN_LEAD=str(lead),
        RUN_EVAL=str(evaluator),
    )

    assert result.returncode == 0, result.stderr
    assert "status: shipped" in (mailbox / "STATE.md").read_text()


def test_driver_retries_a_missing_slice_commit(
    tmp_path: Path,
) -> None:
    """A code-changing slice without a commit retries Lead, then errors."""
    mailbox = make_mailbox(tmp_path, PLAN_CODE)
    subprocess.run(["git", "init", "-q", str(mailbox)], check=True)
    counter = tmp_path / "lead-count"
    lead = write_script(
        tmp_path / "lead.sh",
        (
            'if [[ -f "$COUNT_FILE" ]]; then count="$(<"$COUNT_FILE")"; '
            'else count=0; fi\n'
            'printf "%s\\n" "$((count + 1))" > "$COUNT_FILE"\n'
            'iteration="$(awk -F": " \'/^iteration:/{print $2}\' '
            '"$LOOP_DIR/STATE.md")"\n'
            'printf "%s\\n" "- iter ${iteration} | lead | no commit" '
            '>> "$LOOP_DIR/LOG.md"\n'
        ),
    )
    evaluator = write_script(tmp_path / "evaluator.sh", "exit 1\n")

    result = run_driver(
        mailbox,
        "1",
        RUN_LEAD=str(lead),
        RUN_EVAL=str(evaluator),
        COUNT_FILE=str(counter),
    )

    assert result.returncode == 3, result.stderr
    assert "status: error" in (mailbox / "STATE.md").read_text()
    assert counter.read_text(encoding="utf-8").strip() == "2"


def test_run_role_does_not_recurse_into_python(tmp_path: Path) -> None:
    """Role mode dispatches the fake harness without requiring a verdict."""
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    lead = write_script(tmp_path / "lead.sh", 'printf "%s\\n" "OK"\n')

    # Role mode bypasses Python and therefore must not take its lock.
    result = run_driver(mailbox, "--run-role", "lead", RUN_LEAD=str(lead))

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    assert not (mailbox / ".lock").exists()

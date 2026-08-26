"""Integration-regression tests for the open-loop driver (slice
`integration-regression-drivers`).

Unlike metrics/tests/test_open_loop_driver.py (which fakes/monkeypatches the
per-slice commit gate away), the end-to-end test here drives run_loop against
a REAL temp git repo and shells out to the REAL metrics/trio-shadow.py and
metrics/trio-check.py subprocesses, exactly as a live pipeline would. Every
fixture is built fresh in tmp_path -- loop*/ is gitignored, so an Evaluator
worktree has no mailbox to depend on.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from metrics import trio_loop

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)

TRIO_CHECK = Path(trio_loop.__file__).resolve().with_name("trio-check.py")

# Isolated git identity/config, same hermetic pattern as
# metrics/tests/test_trio_shadow.py: no dependency on the host's global git
# config (avoids gpg-signing prompts / missing identity in CI sandboxes).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Open Loop Integration Test",
    "GIT_AUTHOR_EMAIL": "open-loop-integration@example.com",
    "GIT_COMMITTER_NAME": "Open Loop Integration Test",
    "GIT_COMMITTER_EMAIL": "open-loop-integration@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _run_with_deadline(fn, args=(), kwargs=None, deadline: float = 30.0):
    """Run fn(*args, **kwargs) on a daemon thread; fail (not hang) past deadline."""
    kwargs = kwargs or {}
    result: dict = {}

    def target() -> None:
        result["code"] = fn(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=deadline)
    assert not thread.is_alive(), (
        f"driver call did not finish within {deadline}s (possible hang)"
    )
    assert "code" in result, "worker thread finished without recording a result"
    return result["code"]


def _write_common_mailbox_files(
    mailbox: Path, plan_text: str, *, with_queue: bool
) -> None:
    (mailbox / "GOAL.md").write_text(
        "# Goal\nShip the demo slice end to end.\n", encoding="utf-8"
    )
    (mailbox / "STATE.md").write_text(
        "schema: 1\n"
        "iteration: 0\n"
        "max_iterations: 5\n"
        "status: ready\n"
        "phase: idle\n"
        "mission: ship the demo slice\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(plan_text, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    if with_queue:
        (mailbox / "QUEUE.md").write_text(
            "```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n", encoding="utf-8"
        )


def _write_retired_queue(mailbox: Path, slice_id: str, sha: str, at: str) -> None:
    (mailbox / "QUEUE.md").write_text(
        "```yaml\n"
        "retired:\n"
        f"  - slice: {slice_id}\n"
        f"    sha: {sha}\n"
        f"    at: {at}\n"
        "```\n"
        "\n"
        "```yaml\n"
        "faults:\n"
        "```\n",
        encoding="utf-8",
    )


def _append_slice_verdict(mailbox: Path, slice_id: str, sha: str, verdict: str) -> None:
    path = mailbox / "VERDICT.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"## slice {slice_id} @{sha} — {verdict}\n"
    path.write_text(text, encoding="utf-8")


def _prepend_integration_verdict(mailbox: Path, line: str) -> None:
    path = mailbox / "VERDICT.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(line + "\n" + existing, encoding="utf-8")


PLAN_DEMO = """\
# Iteration 1 -- current increment

```yaml
slices:
  - id: demo
    writes: [src/demo.py]
    reads: []
    accepts: ["module importable"]
```

## Verification standard
implement-then-smoke
"""

# Loop/api-only writes exempt this slice from the per-slice commit gate
# (metrics/trio-shadow.py commit_gate_offenders), so this fixture needs no
# real git repo at all -- only the (open-loop vs lockstep) path selection
# is under test here.
PLAN_LOOP_ONLY = """\
```yaml
slices:
  - id: solo
    writes: [loop/STATE.md, "api:Solo"]
    reads: []
```
"""

PLAN_EMPTY_SLICES = "```yaml\nslices:\n```\n"


class DemoRunner:
    """Single runner used as BOTH lead_runner and eval_runner (run_loop's
    contract): dispatches on role/context['kind'] exactly like a real Lead
    and Evaluator would, writing real QUEUE.md/VERDICT.md text."""

    def __init__(self, slice_id: str, sha: str) -> None:
        self.slice_id = slice_id
        self.sha = sha
        self.lead_calls = 0
        self.slice_eval_calls = 0
        self.integration_eval_calls = 0

    def run(self, role, iteration, mailbox, context=None):
        if role == "lead":
            self.lead_calls += 1
            assert self.lead_calls == 1, "only one Lead pass is scripted"
            _write_retired_queue(
                mailbox, self.slice_id, self.sha, "2026-01-01T00:00:00Z"
            )
            return 0
        if role == "evaluator":
            assert context is not None
            if context["kind"] == "slice-eval":
                self.slice_eval_calls += 1
                assert context["slice"] == self.slice_id
                assert context["sha"] == self.sha
                _append_slice_verdict(mailbox, self.slice_id, self.sha, "SHIP")
            elif context["kind"] == "integration-eval":
                self.integration_eval_calls += 1
                _prepend_integration_verdict(mailbox, "VERDICT: SHIP")
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unexpected kind {context['kind']!r}")
            return 0
        raise AssertionError(f"unexpected role {role!r}")  # pragma: no cover


# --- 1. End-to-end: real git repo, real trio-shadow gate, real trio-check --


def test_end_to_end_open_loop_ships_and_trio_check_passes(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    subprocess.run(["git", "init", "-q", str(mailbox)], check=True)

    _write_common_mailbox_files(mailbox, PLAN_DEMO, with_queue=True)

    src = mailbox / "src"
    src.mkdir()
    (src / "demo.py").write_text("def demo():\n    return 42\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(mailbox), "add", "PLAN.md", "src/demo.py"],
        check=True,
        env=GIT_ENV,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(mailbox),
            "commit",
            "-q",
            "-m",
            "slice(demo): implement the demo helper",
        ],
        check=True,
        env=GIT_ENV,
    )
    sha = subprocess.run(
        ["git", "-C", str(mailbox), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert re.match(r"^[0-9a-f]{40}$", sha)

    runner = DemoRunner("demo", sha)

    code = _run_with_deadline(
        trio_loop.run_loop,
        args=(mailbox, 5, runner),
        kwargs={"mode": "auto", "poll_seconds": 0.01},
    )

    assert code == 0
    assert runner.lead_calls == 1
    assert runner.slice_eval_calls == 1
    assert runner.integration_eval_calls == 1

    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: shipped" in state_text
    assert re.search(r"^verdict: SHIP$", state_text, re.MULTILINE)

    assert (mailbox / ".session.json").is_file()

    check = subprocess.run(
        [sys.executable, str(TRIO_CHECK), str(mailbox)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, (
        f"trio-check.py failed on the resulting mailbox:\n"
        f"stdout:\n{check.stdout}\nstderr:\n{check.stderr}"
    )
    assert "Result: PASS" in check.stdout


# --- 2. Auto-selection, both ways -----------------------------------------


def test_auto_selection_with_queue_takes_open_loop_path(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    _write_common_mailbox_files(mailbox, PLAN_LOOP_ONLY, with_queue=True)
    assert (mailbox / "QUEUE.md").is_file()

    sha = "a" * 40
    runner = DemoRunner("solo", sha)

    code = _run_with_deadline(
        trio_loop.run_loop,
        args=(mailbox, 5, runner),
        kwargs={"mode": "auto", "poll_seconds": 0.01},
    )

    assert code == 0
    # api:OpenLoopSidecar: .session.json is written ONLY by the open-loop
    # path -- this is the observable side effect that proves which path ran.
    assert (mailbox / ".session.json").is_file()
    session = json.loads((mailbox / ".session.json").read_text(encoding="utf-8"))
    assert session["open_loop"] is True


def test_auto_selection_without_queue_takes_lockstep_path(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    _write_common_mailbox_files(mailbox, PLAN_EMPTY_SLICES, with_queue=False)
    assert not (mailbox / "QUEUE.md").exists()

    class LockstepRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def run(self, role, iteration, mailbox, context=None):
            self.calls.append((role, iteration))
            if role == "lead":
                with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                    log.write(f"- iter {iteration} | lead | completed\n")
            elif role == "evaluator":
                (mailbox / "VERDICT.md").write_text(
                    "VERDICT: SHIP\n", encoding="utf-8"
                )
            return 0

    runner = LockstepRunner()

    code = _run_with_deadline(
        trio_loop.run_loop,
        args=(mailbox, 5, runner),
        kwargs={"mode": "auto", "poll_seconds": 0.01},
    )

    assert code == 0
    assert runner.calls == [("lead", 1), ("evaluator", 1)]
    # Lockstep never writes the open-loop-only sidecar.
    assert not (mailbox / ".session.json").exists()
    assert (mailbox / ".driver.json").is_file()


# --- 3. Regression witness: no pre-existing test file was deleted ---------


def test_no_pre_existing_open_loop_test_file_was_deleted() -> None:
    tests_dir = Path(__file__).parent
    expected = [
        "test_trio_loop.py",
        "test_portable_driver.py",
        "test_lockstep_guard.py",
        "test_open_loop_driver.py",
        "test_cli_driver_openloop.py",
        "test_open_loop_running.py",
        "test_slice_lifecycle.py",
    ]
    missing = [name for name in expected if not (tests_dir / name).is_file()]
    assert not missing, f"pre-existing test file(s) deleted: {missing}"

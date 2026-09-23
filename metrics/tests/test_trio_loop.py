"""Behavioral tests for the stdlib Trio loop state machine."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from metrics import trio_loop


PLAN = """\
```yaml
slices:
  - id: coordination
    writes: [loop/STATE.md, "api:Coordination"]
    reads: []
```
"""


def make_mailbox(parent: Path, plan: str = PLAN) -> Path:
    """Create the smallest mailbox needed by the loop and shadow gate."""
    mailbox = parent / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\n"
        "status: ready\n"
        "phase: idle\n"
        "custom: preserve\n",
        encoding="utf-8",
    )
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    return mailbox


class FakeRunner:
    """Role runner that records calls and writes evaluator verdicts."""

    def __init__(
        self,
        verdicts: list[str],
        *,
        expected_roles: list[str] | None = None,
        add_log: bool = True,
    ) -> None:
        self.verdicts = list(verdicts)
        self.expected_roles = expected_roles
        self.add_log = add_log
        self.calls: list[tuple[str, int]] = []
        self.contexts: list[object] = []

    def run(
        self,
        role: str,
        iteration: int,
        mailbox: Path,
        context: dict | None = None,
    ) -> int:
        """Record a role and emulate its mailbox writes."""
        self.calls.append((role, iteration))
        self.contexts.append(context)
        if self.expected_roles is not None:
            expected = self.expected_roles.pop(0)
            assert role == expected
        if role in {"lead", "repair"} and self.add_log:
            with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write(f"- iter {iteration} | {role} | completed\n")
        if role == "evaluator":
            assert self.verdicts
            text = self.verdicts.pop(0)
            extras: list[str] = []
            if context:
                attempt = str(context.get("evaluator_attempt") or "")
                if attempt and "attempt:" not in text.lower():
                    extras.append(f"attempt: {attempt}")
                pinned = str(context.get("pinned_sha") or "")
                if pinned and "commit:" not in text.lower():
                    extras.append(f"commit: {pinned}")
            if extras:
                text = text.rstrip("\n") + "\n" + "\n".join(extras) + "\n"
            (mailbox / "VERDICT.md").write_text(
                text if text.endswith("\n") else text + "\n",
                encoding="utf-8",
            )
        return 0


class EvaluatorCrashRunner(FakeRunner):
    """Fake runner that dies after the Lead transition is persisted."""

    def run(
        self, role: str, iteration: int, mailbox: Path, context=None
    ) -> int:
        if role == "evaluator":
            self.calls.append((role, iteration))
            raise RuntimeError("evaluator crashed")
        return super().run(role, iteration, mailbox, context)


class LeadCrashRunner(FakeRunner):
    """Fake runner that dies while the Lead phase is in flight."""

    def run(
        self, role: str, iteration: int, mailbox: Path, context=None
    ) -> int:
        if role == "lead":
            self.calls.append((role, iteration))
            raise RuntimeError("lead crashed")
        return super().run(role, iteration, mailbox, context)


def state_text(mailbox: Path) -> str:
    """Read state for assertions without depending on the metrics parser."""
    return (mailbox / "STATE.md").read_text(encoding="utf-8")


def test_gate_aliases_are_public() -> None:
    assert trio_loop.run_commit_gate is trio_loop._commit_gate
    assert trio_loop.run_log_gate is trio_loop._log_gate


def test_ship_persists_driver_state(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"])
    runner.session_ids = {
        "lead": "lead-session",
        "evaluator": "evaluator-session",
    }

    assert trio_loop.run_loop(mailbox, 1, runner) == 0

    driver_state = json.loads(
        (mailbox / ".driver.json").read_text(encoding="utf-8")
    )
    assert driver_state == {
        "pid": os.getpid(),
        "iteration": 1,
        "phase": "shipped",
        "session_ids": runner.session_ids,
    }
    assert "phase: shipped" in state_text(mailbox)


def test_main_accepts_omnigent_runner_without_live_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class FakeOmnigentRunner:
        def __init__(self, **kwargs: object) -> None:
            seen["runner_kwargs"] = kwargs

    def fake_run_loop(*args: object, **kwargs: object) -> int:
        seen["run_args"] = args
        seen["run_kwargs"] = kwargs
        return 23

    monkeypatch.setattr(
        trio_loop,
        "_load_omnigent_runner",
        lambda: FakeOmnigentRunner,
    )
    monkeypatch.setattr(trio_loop, "run_loop", fake_run_loop)

    result = trio_loop.main(
        [
            "run",
            "--mailbox",
            str(tmp_path),
            "--max-iterations",
            "1",
            "--runner",
            "omnigent",
        ]
    )

    assert result == 23
    assert seen["runner_kwargs"] == {"repo": Path.cwd()}


def init_git(path: Path) -> None:
    """Create a git repository without creating any slice commits."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_plain_iterate_runs_a_new_lead_pass(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: ITERATE", "VERDICT: SHIP"])

    result = trio_loop.run_loop(mailbox, 2, runner)

    assert result == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("lead", 2),
        ("evaluator", 2),
    ]
    assert "iteration: 2" in state_text(mailbox)
    assert "status: shipped" in state_text(mailbox)


def test_plain_iterate_hits_the_iteration_cap(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: ITERATE"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 4
    assert runner.calls == [("lead", 1), ("evaluator", 1)]


def test_local_repairs_are_capped_then_force_a_lead(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(
        [
            "VERDICT: ITERATE scope=local:a.py",
            "VERDICT: ITERATE scope=local:b.py",
            "VERDICT: ITERATE scope=local:c.py",
            "VERDICT: SHIP",
        ]
    )

    result = trio_loop.run_loop(mailbox, 4, runner)

    assert result == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("repair", 2),
        ("evaluator", 2),
        ("repair", 3),
        ("evaluator", 3),
        ("lead", 4),
        ("evaluator", 4),
    ]
    assert (mailbox / ".repairs").read_text(encoding="utf-8").strip() == "0"
    assert "repair cap" in (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_design_scope_resets_repairs_for_a_full_lead(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(
        [
            "VERDICT: ITERATE scope=local:a.py",
            "VERDICT: ITERATE scope=design",
            "VERDICT: SHIP",
        ]
    )

    assert trio_loop.run_loop(mailbox, 3, runner) == 0
    assert runner.calls == [
        ("lead", 1),
        ("evaluator", 1),
        ("repair", 2),
        ("evaluator", 2),
        ("lead", 3),
        ("evaluator", 3),
    ]


@pytest.mark.parametrize(
    ("verdict", "expected_code", "expected_status"),
    [
        ("# VERDICT: SHIP", 0, "shipped"),
        ("verdict: iterate", 4, "running"),
        ("VERDICT: ITERATE scope=design", 4, "running"),
        ("VERDICT: ITERATE scope=local:a.py", 4, "running"),
        ("VERDICT: SHIP scope=local:x", 3, "error"),
    ],
)
def test_verdict_grammar_and_outcomes(
    tmp_path: Path,
    verdict: str,
    expected_code: int,
    expected_status: str,
) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner([verdict])

    assert trio_loop.run_loop(mailbox, 1, runner) == expected_code
    assert f"status: {expected_status}" in state_text(mailbox)


@pytest.mark.parametrize(
    ("verdict", "expected_code", "expected_status"),
    [
        ("VERDICT: BLOCKED", 2, "blocked"),
        ("VERDICT: NEEDS_HUMAN", 5, "needs_human"),
    ],
)
def test_terminal_non_ship_verdicts(
    tmp_path: Path,
    verdict: str,
    expected_code: int,
    expected_status: str,
) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner([verdict])

    assert trio_loop.run_loop(mailbox, 1, runner) == expected_code
    assert f"status: {expected_status}" in state_text(mailbox)


def test_gate_failure_retries_the_same_lead_once(tmp_path: Path) -> None:
    mailbox = make_mailbox(
        tmp_path,
        PLAN.replace("loop/STATE.md, \"api:Coordination\"", "app.py"),
    )
    init_git(mailbox)
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 3
    assert runner.calls == [("lead", 1), ("lead", 1)]
    assert "status: error" in state_text(mailbox)
    assert "gate breach" in (mailbox / "LOG.md").read_text(encoding="utf-8")


def test_log_gate_failure_retries_the_same_lead_once(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"], add_log=False)

    assert trio_loop.run_loop(mailbox, 1, runner) == 3
    assert runner.calls == [("lead", 1), ("lead", 1)]
    assert "status: error" in state_text(mailbox)


def test_crash_between_roles_resumes_at_evaluator(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    crashing = EvaluatorCrashRunner([])

    with pytest.raises(RuntimeError, match="evaluator crashed"):
        trio_loop.run_loop(mailbox, 1, crashing)

    assert crashing.calls == [("lead", 1), ("evaluator", 1)]
    assert "iteration: 1" in state_text(mailbox)
    assert "phase: lead-done" in state_text(mailbox)

    resumed = FakeRunner(["VERDICT: SHIP"], expected_roles=["evaluator"])
    assert trio_loop.run_loop(mailbox, 1, resumed) == 0
    assert resumed.calls == [("evaluator", 1)]
    assert "iteration: 1" in state_text(mailbox)


def test_crash_during_role_resumes_same_role_and_iteration(
    tmp_path: Path,
) -> None:
    mailbox = make_mailbox(tmp_path)
    crashing = LeadCrashRunner([])

    with pytest.raises(RuntimeError, match="lead crashed"):
        trio_loop.run_loop(mailbox, 1, crashing)

    assert "iteration: 1" in state_text(mailbox)
    assert "phase: lead-running" in state_text(mailbox)

    resumed = FakeRunner(
        ["VERDICT: SHIP"],
        expected_roles=["lead", "evaluator"],
    )
    assert trio_loop.run_loop(mailbox, 1, resumed) == 0
    assert resumed.calls == [("lead", 1), ("evaluator", 1)]


def test_live_lock_returns_five(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    lock = mailbox / ".lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 5
    assert runner.calls == []
    assert lock.is_dir()


def test_stale_lock_is_retaken_and_released(tmp_path: Path) -> None:
    mailbox = make_mailbox(tmp_path)
    lock = mailbox / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("999999999\n", encoding="utf-8")
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    assert not lock.exists()


def test_missing_state_is_initialized_and_unknown_lines_survive(
    tmp_path: Path,
) -> None:
    mailbox = make_mailbox(tmp_path)
    (mailbox / "STATE.md").unlink()
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    text = state_text(mailbox)
    assert "iteration: 1" in text
    assert "status: shipped" in text
    assert "phase: shipped" in text


def git_identity_env() -> dict[str, str]:
    """Local git identity for test commits only (not git config)."""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "trio-test"
    env["GIT_AUTHOR_EMAIL"] = "trio-test@example.test"
    env["GIT_COMMITTER_NAME"] = "trio-test"
    env["GIT_COMMITTER_EMAIL"] = "trio-test@example.test"
    return env


def test_git_repo_ship_without_retirement_is_not_finished(
    tmp_path: Path,
) -> None:
    """Observed SHIP-without-mailbox-retirement must not look shipped."""
    init_git(tmp_path)
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    text = state_text(mailbox)
    assert "status: needs_retirement" in text
    assert "phase: ship-pending-retirement" in text
    assert "status: shipped" not in text


def test_lockstep_passes_attempt_context_to_evaluator(
    tmp_path: Path,
) -> None:
    """Default lockstep mints and persists a unique attempt before dispatch."""
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    eval_contexts = [
        ctx for (role, _i), ctx in zip(runner.calls, runner.contexts)
        if role == "evaluator"
    ]
    assert eval_contexts and eval_contexts[0]
    attempt = str(eval_contexts[0].get("evaluator_attempt") or "")
    assert attempt
    text = state_text(mailbox)
    assert f"evaluator_attempt: {attempt}" in text
    verdict = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"attempt: {attempt}" in verdict


def test_git_repo_ship_with_fake_commit_line_is_not_shipped(
    tmp_path: Path,
) -> None:
    """Invented hex is not a git object, so SHIP cannot finish."""
    init_git(tmp_path)
    mailbox = make_mailbox(tmp_path)
    sha = "a" * 40
    runner = FakeRunner(
        [f"VERDICT: SHIP\n# Verdict — iteration 1\ncommit: {sha}\n"]
    )

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert "status: needs_retirement" in state_text(mailbox)
    assert "status: shipped" not in state_text(mailbox)


def test_git_repo_ship_with_empty_retirement_commit_is_not_shipped(
    tmp_path: Path,
) -> None:
    """A message-only empty SHIP commit is not mailbox identity."""
    init_git(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "loop: iteration 1 — SHIP",
        ],
        check=True,
        env=git_identity_env(),
    )
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"])

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert "status: needs_retirement" in state_text(mailbox)


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_relative(
    repo: Path, relative: str, content: str, message: str
) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", relative],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        env=git_identity_env(),
    )
    return _git_head(repo)


def test_git_repo_real_retirement_ships_and_keeps_foreign_dirty(
    tmp_path: Path,
) -> None:
    """Verified objects + mailbox paths ship; dirty extras stay unstaged."""
    init_git(tmp_path)
    foreign = tmp_path / "foreign-dirty.txt"
    foreign.write_text("leave me", encoding="utf-8")
    mailbox = make_mailbox(tmp_path)
    product = _commit_relative(
        tmp_path, "app.py", "print(1)\n", "slice(demo): product"
    )
    _commit_relative(
        tmp_path,
        "mailbox/VERDICT.md",
        "placeholder\n",
        "loop: iteration 1 — SHIP",
    )
    runner = FakeRunner(
        [
            "VERDICT: SHIP\n# Verdict — iteration 1\n"
            f"commit: {product}\n"
        ]
    )

    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 0
    assert "status: shipped" in state_text(mailbox)
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "foreign-dirty.txt" in status
    assert not status.split("foreign-dirty.txt")[0].endswith("M ")


def test_stale_same_iteration_verdict_redispatches_evaluator(
    tmp_path: Path,
) -> None:
    """Leftover iteration-N SHIP is not this attempt."""
    mailbox = make_mailbox(tmp_path)
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-done\n",
        encoding="utf-8",
    )
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: SHIP\n# Verdict — iteration 1\n",
        encoding="utf-8",
    )
    with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
        log.write("- iter 1 | lead | completed\n")
    runner = FakeRunner(["VERDICT: SHIP"], expected_roles=["evaluator"])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    assert runner.calls == [("evaluator", 1)]
    assert "status: shipped" in state_text(mailbox)


def test_matching_attempt_skips_evaluator_redispatch(
    tmp_path: Path,
) -> None:
    """Crash-resume accepts only evidence for the persisted attempt."""
    mailbox = make_mailbox(tmp_path)
    attempt = "cafef00d" + "ab" * 12
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: running\nphase: lead-done\n"
        f"evaluator_attempt: {attempt}\n",
        encoding="utf-8",
    )
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: SHIP\n# Verdict — iteration 1\n"
        f"attempt: {attempt}\n",
        encoding="utf-8",
    )
    with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
        log.write("- iter 1 | lead | completed\n")
    runner = FakeRunner([], expected_roles=[])

    assert trio_loop.run_loop(mailbox, 1, runner) == 0
    assert runner.calls == []
    assert "status: shipped" in state_text(mailbox)


def test_needs_retirement_resumes_after_real_finalization(
    tmp_path: Path,
) -> None:
    """Later verified retirement finishes without a second Evaluator."""
    init_git(tmp_path)
    mailbox = make_mailbox(tmp_path)
    runner = FakeRunner(["VERDICT: SHIP"])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    evaluated = None
    for line in state_text(mailbox).splitlines():
        if line.startswith("evaluated_sha:"):
            evaluated = line.split(":", 1)[1].strip()
    assert "status: needs_retirement" in state_text(mailbox)

    if not evaluated:
        _commit_relative(tmp_path, "app.py", "x\n", "slice(demo): product")
        evaluated = _git_head(tmp_path)
        trio_loop._update_state(
            mailbox / "STATE.md", {"evaluated_sha": evaluated}
        )
    else:
        _commit_relative(tmp_path, "app.py", "x\n", "slice(demo): product")
    _commit_relative(
        tmp_path,
        "mailbox/VERDICT.md",
        (mailbox / "VERDICT.md").read_text(encoding="utf-8"),
        "loop: iteration 1 — SHIP",
    )
    resumed = FakeRunner([], expected_roles=[])
    assert trio_loop.run_loop(mailbox, 1, resumed, repo=tmp_path) == 0
    assert resumed.calls == []
    assert "status: shipped" in state_text(mailbox)


def test_changed_product_is_not_accepted_on_retirement_resume(
    tmp_path: Path,
) -> None:
    """HEAD that dropped the graded revision must not ship."""
    init_git(tmp_path)
    first = _commit_relative(tmp_path, "app.py", "one\n", "first")
    mailbox = make_mailbox(tmp_path)
    (mailbox / "STATE.md").write_text(
        "iteration: 1\nstatus: needs_retirement\n"
        "phase: ship-pending-retirement\n"
        f"evaluated_sha: {first}\n",
        encoding="utf-8",
    )
    (mailbox / "VERDICT.md").write_text(
        "VERDICT: SHIP\n# Verdict — iteration 1\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "--orphan", "other", "-q"],
        check=True,
    )
    _commit_relative(tmp_path, "other.py", "two\n", "unrelated history")
    _commit_relative(
        tmp_path,
        "mailbox/VERDICT.md",
        "VERDICT: SHIP\n",
        "loop: iteration 1 — SHIP",
    )
    runner = FakeRunner([], expected_roles=[])
    assert trio_loop.run_loop(mailbox, 1, runner, repo=tmp_path) == 6
    assert runner.calls == []
    assert "status: needs_retirement" in state_text(mailbox)

"""steplib.py — begin/next/end round trip over a real-git mailbox fixture,
using the private trio_native_step.py module copy with our overrides."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from trio_opencode import steplib

TOKEN = "oc-test-token"


def mbox(repo: Path) -> Path:
    return repo / "loop"


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-opencode-test", GIT_COMMITTER_EMAIL="t@example.test",
        TRIO_RETIREMENT_WAIT_SECONDS="0",
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=_git_env())


def test_begin_uses_opencode_driver_label_and_ledger_dir(git_repo: Path) -> None:
    out = steplib.begin(mbox(git_repo), None, TOKEN)
    assert out["ok"], out
    assert out["lock_owner"] == f"opencode:{TOKEN}"

    session = (mbox(git_repo) / ".session.json")
    assert session.is_file()
    import json
    data = json.loads(session.read_text())
    assert data["driver"] == "opencode"
    assert data["session"] == TOKEN

    # Lock owner file says opencode:<token>, never workflow:<token>.
    owner = (mbox(git_repo) / ".lock" / "owner").read_text().strip()
    assert owner == f"opencode:{TOKEN}"

    # Our own records/result filenames, never the native driver's.
    assert not (mbox(git_repo) / ".native.json").exists()

    steplib.end(mbox(git_repo), None, TOKEN)


def test_ledger_lives_under_trio_opencode_git_dir(git_repo: Path) -> None:
    steplib.begin(mbox(git_repo), None, TOKEN)
    path = steplib.ledger_path(mbox(git_repo), git_repo)
    assert path is not None
    assert path.parent.parent.name == "trio-opencode"
    steplib.end(mbox(git_repo), None, TOKEN)


def test_no_omnigent_module_loaded_after_a_full_round_trip(git_repo: Path) -> None:
    steplib.begin(mbox(git_repo), None, TOKEN)
    n = steplib.next_(mbox(git_repo), None, TOKEN, max_iterations=4)
    assert n["ok"] and n["action"] == "lead"
    steplib.end(mbox(git_repo), None, TOKEN)
    steplib.assert_no_omnigent_loaded(git_repo)


def test_second_begin_with_a_live_foreign_pid_is_refused(git_repo: Path) -> None:
    first = steplib.begin(mbox(git_repo), None, TOKEN)
    assert first["ok"]
    # Simulate another live process holding the same token: stamp a pid
    # that is definitely alive (pid 1 / our own long-lived test pid) under
    # a *different* run token, which the acquire logic treats as foreign.
    other_token = "oc-other-token"
    lock = mbox(git_repo) / ".lock"
    (lock / "owner").write_text(f"opencode:{other_token}\n")
    (lock / "pid").write_text(f"{__import__('os').getpid()}\n")
    import time
    (lock / "heartbeat").write_text(f"{time.time():.3f}\n")

    second = steplib.begin(mbox(git_repo), None, other_token)
    # our own process's pid *is* alive, so the second begin under the
    # *original* token must be refused while `other_token`'s (still-live)
    # holder pid occupies the lock.
    assert not second["ok"] or second["lock_owner"] == f"opencode:{other_token}"

    refused = steplib.begin(mbox(git_repo), None, TOKEN)
    assert not refused["ok"]
    assert "locked" in refused["error"]

    steplib.end(mbox(git_repo), None, other_token)


def test_full_round_trip_gate_pin_apply_ship(git_repo: Path) -> None:
    steplib.begin(mbox(git_repo), None, TOKEN)
    n = steplib.next_(mbox(git_repo), None, TOKEN, max_iterations=4)
    assert n["ok"] and n["action"] == "lead" and n["iteration"] == 1

    (git_repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    _git(git_repo, "add", "app.py")
    _git(git_repo, "commit", "-q", "-m", "slice(app): pass 1")
    with (mbox(git_repo) / "LOG.md").open("a", encoding="utf-8") as fh:
        fh.write("- iter 1 | lead | done\n")
    (mbox(git_repo) / "REPORT.md").write_text("# Report — iteration 1\n", encoding="utf-8")

    g = steplib.gate(mbox(git_repo), None, TOKEN, role="lead", iteration=1, attempt=1)
    assert g["ok"] and g["pass"], g

    p = steplib.pin(mbox(git_repo), None, TOKEN, iteration=1)
    assert p["ok"], p

    (mbox(git_repo) / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n# Verdict — iteration 1\n"
        f"attempt: {p['evaluator_attempt']}\nevaluated: {p['sha']}\n"
        f"commit: {p['sha']}\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "loop/VERDICT.md")
    _git(git_repo, "commit", "-q", "-m", "loop: iteration 1 — SHIP")

    a = steplib.apply(mbox(git_repo), None, TOKEN, iteration=1,
                       attempt=p["evaluator_attempt"])
    assert a["ok"] and a["verdict"] == "SHIP" and a["status"] == "shipped", a

    e = steplib.end(mbox(git_repo), None, TOKEN)
    assert e["ok"] and e["lock"] == "released"


def test_dangling_worktrees_reports_under_trio_opencode_worktrees_dir(git_repo: Path) -> None:
    """``native/trio_native_step.py``'s own ``_dangling_worktrees`` hard-codes
    the literal ``.claude/worktrees/`` instead of its own ``WORKTREES_DIR``
    global; steplib.py must override it so ``end``'s ``dangling_worktrees``
    actually finds an opencode builder worktree under
    ``.trio-opencode/worktrees/``, never under the native driver's own
    ``.claude/worktrees/``."""
    worktrees_dir = git_repo / ".trio-opencode" / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    wt_path = worktrees_dir / "dangling-b1"
    _git(git_repo, "worktree", "add", "-b", "trio-oc/dangling-b1", str(wt_path))

    dangling = steplib.NS._dangling_worktrees(git_repo)
    assert str(wt_path) in dangling, dangling

    # Never reported under the native driver's own directory.
    claude_worktrees = git_repo / ".claude" / "worktrees"
    claude_worktrees.mkdir(parents=True, exist_ok=True)
    other_wt = claude_worktrees / "not-ours"
    _git(git_repo, "worktree", "add", "-b", "workflow/not-ours", str(other_wt))
    dangling2 = steplib.NS._dangling_worktrees(git_repo)
    assert str(other_wt) not in dangling2, dangling2
    assert str(wt_path) in dangling2, dangling2


def test_required_keys_validated(git_repo: Path) -> None:
    out = steplib.begin(mbox(git_repo), None, TOKEN)
    for key in steplib.REQUIRED["begin"]:
        assert key in out
    steplib.end(mbox(git_repo), None, TOKEN)


# ---------------------------------------------------------------------------
# Bug 3: `step_long`'s exhausted-polling result, never the raw last `pending`
# dict a caller would `KeyError` on indexing `["stop"]`/`["status"]`.
# ---------------------------------------------------------------------------


def test_step_long_exhausted_polling_returns_ok_false_never_the_raw_pending_dict() -> None:
    calls: list[int] = []

    def always_pending(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        calls.append(kwargs.get("poll", 0))
        return {"ok": True, "pending": True}

    sleeps: list[float] = []
    result = steplib.step_long(always_pending, sleep=sleeps.append, max_polls=3)

    assert result == {"ok": False, "op": "always-pending",
                      "error": "always-pending still running after 3 polls"}
    # One initial call (no `poll` kwarg) plus one per poll up to `max_polls`:
    # N+1 total calls, never waiting for real.
    assert calls == [0, 1, 2, 3]
    assert sleeps == [1.0, 1.0, 1.0]


def test_step_long_resolves_normally_when_pending_clears_before_max_polls() -> None:
    answers = iter([{"ok": True, "pending": True}, {"ok": True, "pending": True},
                    {"ok": True, "done": True}])

    def flaky(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return next(answers)

    result = steplib.step_long(flaky, sleep=lambda s: None, max_polls=5)
    assert result == {"ok": True, "done": True}

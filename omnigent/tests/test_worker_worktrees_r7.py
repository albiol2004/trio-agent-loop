"""Regressions for D1 (r6 live run B): an evaluator worktree whose session
was just archived/deleted by the post-loop prune was retained forever as
``active_session`` because cleanup ran ~0.3 s later, while the session's
runner was still exiting.

Offline, real git, real /proc. Holders are detached test-owned processes
with a bounded lifetime that stand in for a runner exiting after DELETE;
the product code never kills them (the fixture reaps leftovers).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_worker_worktrees_r4 import (  # noqa: E402  (sibling helpers)
    GIT_ENV,
    MODULE,
    SCRIPT,
    _integrated_demo_worker,
    _load,
    git,
)

_HOLDER = r"""
import os, sys, time
if os.fork():
    os._exit(0)
os.setsid()
os.chdir(sys.argv[1])
tmp = sys.argv[3] + ".tmp"
with open(tmp, "w") as fh:
    fh.write(str(os.getpid()))
os.rename(tmp, sys.argv[3])
time.sleep(float(sys.argv[2]))
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_r7", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r7", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


@pytest.fixture()
def holders(wt, tmp_path):
    """Start detached (reparented) holders; reap any left at teardown."""
    started: list[dict] = []

    def start(cwd: Path, seconds: float) -> dict:
        pidfile = tmp_path / f"holder-{len(started)}.pid"
        subprocess.run(
            [sys.executable, "-c", _HOLDER, str(cwd), str(seconds), str(pidfile)],
            check=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while not pidfile.exists():
            assert time.monotonic() < deadline, "holder did not start"
            time.sleep(0.01)
        ident = wt.process_identity(int(pidfile.read_text()))
        started.append(ident)
        return ident

    yield start
    for ident in started:
        if wt.owner_exit_state(ident) == "alive":
            os.kill(ident["pid"], 9)  # test-owned process only


def _eval_worktree(wt, repo, root, session_id="s-eval"):
    """A finished slice-eval worktree, recorded the way trioctl does it."""
    sha = git(repo, "rev-parse", "HEAD")
    rec = wt.create(repo, slice_id="eval-demo", mailbox=repo / "loop",
                    root=root, role="evaluator", detach_at=sha)
    wt.mark_running(repo, rec, None, session_id=session_id)
    wt.mark_finished(repo, rec["id"])
    return wt.load_record(repo, rec["id"])


def _registered(repo, path) -> bool:
    return str(path) in git(repo, "worktree", "list", "--porcelain")


def _guard_removal(wt, monkeypatch, holder):
    """Fail if `git worktree remove` runs while the holder is still alive.

    *holder* is an identity, or a callable returning one once started.
    """
    real_git = wt.git
    removals = []

    def guarded(cwd, *args, **kw):
        if args[:2] == ("worktree", "remove"):
            ident = holder() if callable(holder) else holder
            assert wt.owner_exit_state(ident) == "exited", "removed before owner exit"
            removals.append(args[2])
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(wt, "git", guarded)
    return removals


# ----------------------------------------- D1 at the real loop `finally`


class _PruneClient:
    def __init__(self, rows):
        self.rows = rows
        self.deleted: list[str] = []

    def list_sessions(self, limit=20, after=None):
        return {"data": self.rows}

    def get_items(self, session_id, limit=100, order="asc", after=None):
        return {"items": []}

    def delete_session(self, session_id):
        self.deleted.append(session_id)
        return {"deleted": True}


def test_d1_loop_end_removes_eval_worktree_once_torn_down_runner_exits(
    trioctl, wt, repo, root, holders, monkeypatch, capsys
):
    state: dict = {}

    class Runner:
        def __init__(self, **kwargs):
            self.created_session_ids = ["s-eval"]
            self.held_session_ids: list[str] = []

        def release_all_fences(self):
            pass

        def restore_root_config_final(self, mailbox):
            return []

    class Loop:
        @staticmethod
        def run_loop(mailbox, max_iterations, runner, *, repo):
            rec = _eval_worktree(wt, repo, root)
            state["rec"] = rec
            # The session's runner outlives the prune's DELETE briefly.
            state["holder"] = holders(Path(rec["path"]), 1.5)
            return 0

    client = _PruneClient([{"id": "s-eval", "status": "idle",
                            "title": "trioctl loop evaluator:iteration 1"}])
    monkeypatch.chdir(repo)
    monkeypatch.setattr(trioctl, "OmnigentRunner", Runner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: Loop)
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url=None: client)
    monkeypatch.setattr(trioctl, "WORKTREE_SETTLE_SECONDS", 20.0)
    real_cleanup = wt.cleanup
    first_pass = []

    def cleanup(*a, **k):
        results = real_cleanup(*a, **k)
        first_pass.extend(r for r in results if r.get("id") == state.get("rec", {}).get("id"))
        return results

    monkeypatch.setattr(wt, "cleanup", cleanup)
    removals = _guard_removal(wt, monkeypatch, lambda: state["holder"])
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", "loop", "--max-iterations", "1",
         "--isolate-workers", "--worktree-root", str(root)]
    )
    started = time.monotonic()
    assert args.func(args) == 0
    elapsed = time.monotonic() - started

    assert client.deleted == ["s-eval"]
    # Without the recheck this is where D1 stopped: retained, active_session.
    assert [r["retained_reason"] for r in first_pass] == ["active_session"]
    record = wt.load_record(repo, state["rec"]["id"])
    assert record["state"] == "removed" and record["worktree_removed"] is True
    assert record["owner_exit"]["outcome"] == "exited"
    assert removals == [state["rec"]["path"]]
    assert not _registered(repo, state["rec"]["path"])
    assert elapsed < 15  # waited for the exit, not the whole bound
    assert f"worktree {state['rec']['id']}: removed" in capsys.readouterr().err


def test_prune_reports_exactly_the_deleted_ids(trioctl, tmp_path, monkeypatch):
    class Partial(_PruneClient):
        def delete_session(self, session_id):
            if session_id == "s-bad":
                raise trioctl.broker_http.BrokerHttpError("boom")
            return super().delete_session(session_id)

    client = Partial([{"id": "s-ok", "status": "idle", "title": "t"},
                      {"id": "s-bad", "status": "idle", "title": "t"},
                      {"id": "s-other", "status": "idle", "title": "t"}])
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url=None: client)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    assert trioctl._run_post_loop_session_prune(mailbox, None, []) == []
    # s-ok deleted before s-bad's failure aborts the pass; s-other untouched.
    assert trioctl._run_post_loop_session_prune(mailbox, None, ["s-ok", "s-bad"]) == ["s-ok"]


# ---------------------------------- bounded, fail-safe retained outcomes


def test_d1_stuck_owner_is_retained_after_the_bound_and_never_killed(
    trioctl, wt, repo, root, holders, monkeypatch
):
    rec = _eval_worktree(wt, repo, root)
    holder = holders(Path(rec["path"]), 120)
    monkeypatch.setattr(trioctl, "WORKTREE_SETTLE_SECONDS", 0.6)
    started = time.monotonic()
    results = trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-eval"])
    elapsed = time.monotonic() - started

    (result,) = [r for r in results if r["id"] == rec["id"]]
    assert result["state"] == "retained" and result["retained_reason"] == "active_session"
    assert "still using worktree after 0.6s" in result["retained_detail"]
    assert result["owner_exit"]["outcome"] == "timeout"
    assert holder["pid"] in result["owner_exit"]["pids"]
    assert 0.5 <= elapsed < 5
    assert wt.owner_exit_state(holder) == "alive"  # not killed
    assert _registered(repo, rec["path"])

    # Retention is recoverable: once the owner is gone, plain cleanup removes it.
    os.kill(holder["pid"], 9)  # the test's own process
    deadline = time.monotonic() + 10
    while wt.owner_exit_state(holder) != "exited":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    (after,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop") if r["id"] == rec["id"]]
    assert after["state"] == "removed"


def test_d1_unconfirmed_exit_is_retained(trioctl, wt, repo, root, holders, monkeypatch):
    rec = _eval_worktree(wt, repo, root)
    holders(Path(rec["path"]), 0.5)
    # An owner whose /proc entry cannot be inspected never counts as gone.
    monkeypatch.setattr(wt, "owner_exit_state", lambda ident: "unknown")
    monkeypatch.setattr(trioctl, "WORKTREE_SETTLE_SECONDS", 1.5)
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-eval"])
                 if r["id"] == rec["id"]]
    assert result["retained_reason"] == "active_session"
    assert "unconfirmed" in result["retained_detail"]
    assert result["owner_exit"]["outcome"] == "unconfirmed"
    assert _registered(repo, rec["path"])


def test_owner_exit_state_on_real_proc(wt):
    me = wt.process_identity(os.getpid())
    assert wt.owner_exit_state(me) == "alive"
    assert wt.owner_exit_state({**me, "start": "0"}) == "exited"      # pid reused
    assert wt.owner_exit_state({**me, "start": None}) == "unknown"    # never identified
    child = subprocess.Popen(["true"])
    child.wait()
    assert wt.owner_exit_state({"pid": child.pid, "start": "1"}) == "exited"


# ---------------------------------------- scope: only torn-down sessions


@pytest.mark.parametrize("torn_down", [[], ["s-other"], ["s-eval"]])
def test_d1_never_waits_for_sessions_this_run_did_not_tear_down(
    trioctl, wt, repo, root, holders, monkeypatch, torn_down
):
    rec = _eval_worktree(wt, repo, root)
    if torn_down == ["s-eval"]:
        # Two sessions used it; only one was torn down.
        record = wt.load_record(repo, rec["id"])
        record["session_ids"].append("s-second")
        wt.save_record(repo, record)
    holders(Path(rec["path"]), 60)
    monkeypatch.setattr(wt, "await_owner_exit",
                        lambda *a, **k: pytest.fail("must not wait"))
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=torn_down)
                 if r["id"] == rec["id"]]
    assert result["retained_reason"] == "active_session"
    assert "owner_exit" not in result


def test_d1_live_recorded_owner_is_never_waited_out(trioctl, wt, repo, root, monkeypatch):
    rec = _eval_worktree(wt, repo, root)
    record = wt.load_record(repo, rec["id"])
    record["dispatcher"] = wt.process_identity(os.getpid())  # a live dispatcher
    wt.save_record(repo, record)
    monkeypatch.setattr(wt, "await_owner_exit",
                        lambda *a, **k: pytest.fail("must not wait"))
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-eval"])
                 if r["id"] == rec["id"]]
    assert result["retained_detail"] == "recorded worker/dispatcher process is alive"


def test_d1_no_wait_when_nothing_is_retained(trioctl, wt, repo, root, monkeypatch):
    rec = _eval_worktree(wt, repo, root)
    monkeypatch.setattr(wt, "await_owner_exit",
                        lambda *a, **k: pytest.fail("must not wait"))
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-eval"])
                 if r["id"] == rec["id"]]
    assert result["state"] == "removed"


def test_await_owner_exit_returns_without_sleeping_once_clear(wt, repo, root):
    rec = _eval_worktree(wt, repo, root)
    outcome = wt.await_owner_exit(
        repo, [rec["id"]], deadline_s=30,
        sleep=lambda s: pytest.fail("no unconditional sleep"),
    )
    assert outcome[rec["id"]]["outcome"] == "exited"


# ------------------------ guards re-run after exit: no work or binding lost


@pytest.mark.parametrize("change", ["untracked", "commit"])
def test_d1_recheck_keeps_dirty_or_moved_eval_worktree(
    trioctl, wt, repo, root, holders, monkeypatch, change
):
    rec = _eval_worktree(wt, repo, root)
    path = Path(rec["path"])
    (path / "notes.txt").write_text("evaluator scratch\n")
    if change == "commit":
        git(path, "add", "notes.txt")
        git(path, "commit", "-q", "-m", "evaluator commit")
    holder = holders(path, 1.0)
    removals = _guard_removal(wt, monkeypatch, holder)
    monkeypatch.setattr(trioctl, "WORKTREE_SETTLE_SECONDS", 20.0)
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-eval"])
                 if r["id"] == rec["id"]]
    if change == "untracked":
        assert result["owner_exit"]["outcome"] == "exited"
        assert result["retained_reason"] == "untracked"
    else:
        # A moved HEAD fails the ownership check before the process check,
        # so it is retained at once and never waited for.
        assert result["retained_reason"] == "uncertain_ownership"
        assert result["retained_detail"] == "eval_head_mismatch"
        assert "owner_exit" not in result
        assert git(path, "log", "-1", "--format=%s") == "evaluator commit"
    assert (path / "notes.txt").read_text() == "evaluator scratch\n"
    assert removals == []


def test_d1_recheck_keeps_a_worktree_whose_session_became_held(
    wt, repo, root, holders
):
    rec = _eval_worktree(wt, repo, root)
    holder = holders(Path(rec["path"]), 0.5)
    first = wt.cleanup(repo)
    assert wt.settle_candidates(first, ["s-eval"]) == [rec["id"]]
    outcomes = wt.await_owner_exit(repo, [rec["id"]], deadline_s=20)
    assert outcomes[rec["id"]]["outcome"] == "exited"
    assert wt.owner_exit_state(holder) == "exited"
    (result,) = wt.recheck_settled(repo, outcomes, deadline_s=20, held_sessions={"s-eval"})
    assert result["retained_reason"] == "held_session"
    assert _registered(repo, rec["path"])


def test_d1_integrated_builder_keeps_waiting_for_acceptance(
    trioctl, wt, repo, root, holders, monkeypatch
):
    rec = _integrated_demo_worker(wt, repo, root)
    record = wt.load_record(repo, rec["id"])
    record.setdefault("session_ids", []).append("s-builder")
    wt.save_record(repo, record)
    holders(Path(rec["path"]), 0.5)
    monkeypatch.setattr(trioctl, "WORKTREE_SETTLE_SECONDS", 20.0)
    (result,) = [r for r in trioctl._run_worktree_cleanup(repo, repo / "loop", torn_down=["s-builder"])
                 if r["id"] == rec["id"]]
    # No verified acceptance: pending, never removed, no binding invented.
    assert result["state"] == "integrated"
    assert "accepted_by" not in result
    assert _registered(repo, rec["path"])

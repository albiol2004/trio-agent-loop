"""r14.1 F4: the slice-eval bind degrades on ANY error, with no orphan worktree.

eval-r14 F4: `_bind_slice_eval` only degraded on TrioctlError (a
ValueError/KeyError, e.g. from corrupt ledger JSON, still escaped and ended
the driver), and when `create()` succeeded but `mark_running` raised, the
created eval worktree stayed in state `created` while the eval degraded.
"""
from __future__ import annotations

from pathlib import Path

from test_r14_eval_degrade import _runner, git, repo, trioctl, wt  # noqa: F401


def _ctx(repo, slice_id="A"):
    return {"mode": "open-loop", "kind": "slice-eval", "slice": slice_id,
            "sha": git(repo, "rev-parse", "HEAD")}


def _registered(repo) -> list[str]:
    out = git(repo, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]


def test_non_trioctl_error_from_bind_degrades(trioctl, wt, repo, tmp_path, monkeypatch, capsys):
    def corrupt(*_a, **_k):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(wt, "create", corrupt)
    runner, seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    assert runner.run("evaluator", 1, repo / "loop", _ctx(repo)) == 0
    assert seen[0]["workspace"] == str(repo)
    meta = seen[0]["inflight"]["s1"]
    assert meta["eval_isolation"] == "degraded (ValueError: Expecting value: line 1 column 1 (char 0))"
    err = capsys.readouterr().err
    assert err.count("eval_isolation: degraded") == 1
    assert "[ValueError]" in err


def test_mark_running_failure_removes_created_worktree(
    trioctl, wt, repo, tmp_path, monkeypatch, capsys
):
    created = []
    real_create = wt.create

    def create(*a, **k):
        record = real_create(*a, **k)
        created.append(record)
        return record

    def broken_mark(*_a, **_k):
        raise KeyError("dispatcher")

    monkeypatch.setattr(wt, "create", create)
    monkeypatch.setattr(wt, "mark_running", broken_mark)
    runner, seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    assert runner.run("evaluator", 1, repo / "loop", _ctx(repo)) == 0
    (record,) = created
    assert seen[0]["workspace"] == str(repo)  # degraded to the root
    assert "KeyError" in seen[0]["inflight"]["s1"]["eval_isolation"]
    # No orphan: the worktree is gone and its ledger record says so.
    assert record["path"] not in _registered(repo)
    assert not Path(record["path"]).exists()
    assert wt.load_record(repo, record["id"])["state"] == "removed"
    err = capsys.readouterr().err
    assert f"unbound slice-eval worktree {record['id']}: removed" in err
    assert "[TrioctlError]" in err


def test_blocked_discard_is_recorded_as_retained(trioctl, wt, repo, tmp_path, monkeypatch, capsys):
    created = []
    real_create = wt.create

    def create(*a, **k):
        record = real_create(*a, **k)
        created.append(record)
        return record

    monkeypatch.setattr(wt, "create", create)
    monkeypatch.setattr(wt, "mark_running",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("disk full")))
    # Removal blocked (e.g. a process still uses the path): recorded, kept.
    monkeypatch.setattr(wt, "cleanup_one", lambda repo, worker_id, **_k: wt.load_record(repo, worker_id))
    runner, _seen = _runner(trioctl, repo, tmp_path / "worktrees", monkeypatch)
    assert runner.run("evaluator", 1, repo / "loop", _ctx(repo)) == 0
    (record,) = created
    saved = wt.load_record(repo, record["id"])
    assert saved["state"] == "retained"
    assert saved["retained_reason"] == "bind_failed"
    assert "RuntimeError: disk full" in saved["retained_detail"]
    assert saved["finished"] is True and saved["dispatcher"] is None
    assert "bind_failed" in capsys.readouterr().err

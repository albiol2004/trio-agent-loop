"""eval-r16rc F1 (mixed metrics set: pre-check before create; an unused
seeded Lead worktree is re-seedable, `--root-bound` works) and F2 (a failed
land is `needs_land`/`land-error`, exit 8, resumable by `land` and by `loop`
without another Lead pass or integration-eval)."""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16rc_f12")


def _mixed_home(tmp_path) -> Path:
    """trio-metrics.py API 6 next to an r15 core (marker 5) on main."""
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "r\n"})
    core = home / "metrics" / "trio_loop.py"
    core.write_text(re.sub(r"^METRICS_API = \d+", "METRICS_API = 5", core.read_text(),
                           count=1, flags=re.M))
    git(home, "commit", "-q", "-am", "r15 core next to API-6 trio-metrics")
    return home


def _refresh(world, *argv):
    t = world.trioctl
    args = t.parser().parse_args(["omnigent", "metrics", "refresh", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def _nothing_created(world, home, spec):
    rec = world.rf.load_record(world.wt, home, spec["slug"])
    assert rec is None or rec.get("state") == "removed", rec
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize("marker", ["5", None])
def test_f1_mixed_set_refused_before_create_then_refresh_runs(world, tmp_path, capsys, marker):
    home = _mixed_home(tmp_path)
    if marker is None:  # an unmarked (pre-r15) core: counts as 4
        core = home / "metrics" / "trio_loop.py"
        core.write_text(re.sub(r"^METRICS_API = \d+\n", "", core.read_text(), count=1,
                               flags=re.M))
        git(home, "commit", "-q", "-am", "unmarked core")
    spec = world.add_loop(home, "loop/mix", [{"id": "m1", "write": "src/m.py"}])
    capsys.readouterr()
    assert world.run_loop(spec) == 3
    err = capsys.readouterr().err
    assert f"METRICS_API {marker or 4}" in err and "Nothing was changed" in err
    _nothing_created(world, home, spec)
    assert world.events == []
    code, _out, rerr = _refresh(world, "--repo", str(home), "--commit")
    assert code == 0, rerr
    assert world.run_loop(spec) == 0
    assert git(home, "show", "main:src/m.py") == "# m1"
    _nothing_created(world, home, spec)


def test_f1_post_create_refusal_removes_the_unused_worktree(world, tmp_path, monkeypatch, capsys):
    home = _mixed_home(tmp_path)
    spec = world.add_loop(home, "loop/mix", [{"id": "m1", "write": "src/m.py"}])
    monkeypatch.setattr(world.trioctl, "_target_metrics_api", lambda home, target: 6)
    capsys.readouterr()
    assert world.run_loop(spec) == 3
    err = capsys.readouterr().err
    assert "predates root-free" in err and "no loop progress" in err
    _nothing_created(world, home, spec)


def test_f1_stale_seeded_branch_is_reseeded_after_refresh(world, tmp_path, monkeypatch, capsys):
    home = _mixed_home(tmp_path)
    spec = world.add_loop(home, "loop/mix", [{"id": "m1", "write": "src/m.py"}])
    with monkeypatch.context() as m:
        m.setattr(world.trioctl, "_target_metrics_api", lambda home, target: 6)
        m.setattr(world.rf, "discard_pristine", lambda *a, **k: False)
        assert world.run_loop(spec) == 3
    old = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(old)
    # The refresh moves main past the seed: the unused worktree is re-seeded.
    code, _o, rerr = _refresh(world, "--repo", str(home), "--commit")
    assert code == 0, rerr
    capsys.readouterr()
    assert world.run_loop(spec) == 0
    err = capsys.readouterr().err
    assert "had no loop progress; removed" in err and "moved" in err
    assert git(home, "show", "main:src/m.py") == "# m1"
    _nothing_created(world, home, spec)


def test_f1_root_bound_refused_even_on_an_unused_seeded_worktree(
    world, tmp_path, monkeypatch, capsys
):
    """r16b: `--root-bound` is refused outright (exit 2, pointing at
    `land`/`abandon`) instead of discarding a pristine, unused Lead
    worktree and falling back to running root-bound -- that mode, and the
    fallback, are both gone (r16 DESIGN)."""
    home = _mixed_home(tmp_path)
    spec = world.add_loop(home, "loop/mix", [{"id": "m1", "write": "src/m.py"}])
    with monkeypatch.context() as m:
        m.setattr(world.trioctl, "_target_metrics_api", lambda home, target: 6)
        m.setattr(world.rf, "discard_pristine", lambda *a, **k: False)
        assert world.run_loop(spec) == 3
    before = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(before)  # the refused run's pristine worktree
    t = world.trioctl
    args = world.loop_args(spec, "--root-bound")
    capsys.readouterr()
    assert t._root_free_begin(args, home, spec["root_box"]) == 2
    err = capsys.readouterr().err
    assert "root-bound mode was removed in r16b" in err
    assert "land --mailbox" in err and "abandon --mailbox" in err
    # Nothing changed: not even the pristine worktree from the refused run
    # (never examined, let alone discarded).
    assert world.rf.load_record(world.wt, home, spec["slug"]) == before


def test_f1_root_bound_still_refused_with_loop_progress(world, tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "r\n"})
    spec = world.add_loop(home, "loop/prog", [{"id": "p1", "write": "src/p.py"}])
    world.hooks["integration-eval"] = lambda *a: (_ for _ in ()).throw(RuntimeError("stop"))
    assert world.run_loop(spec) == 3  # a slice merged on trio/<slug>: progress
    world.hooks.clear()
    t = world.trioctl
    capsys.readouterr()
    assert t._root_free_begin(world.loop_args(spec, "--root-bound"), home, spec["root_box"]) == 2
    assert "abandon" in capsys.readouterr().err
    assert world.rf.active(world.rf.load_record(world.wt, home, spec["slug"]))


def _flaky_land(world, monkeypatch):
    real = world.rf.land
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated git failure during land")
        return real(*a, **kw)

    monkeypatch.setattr(world.rf, "land", flaky)
    return calls


def _state(box: Path) -> dict:
    out = {}
    for line in (box / "STATE.md").read_text().splitlines():
        k, s, v = line.partition(":")
        if s:
            out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("resume", ["land", "loop"])
def test_f2_land_error_is_needs_land_and_resumes_at_land(world, tmp_path, monkeypatch, resume):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "r\n"})
    spec = world.add_loop(home, "loop/le", [{"id": "le1", "write": "src/le.py"}])
    calls = _flaky_land(world, monkeypatch)
    assert world.run_loop(spec) == 8
    live = Path(world.rf.load_record(world.wt, home, spec["slug"])["live_mailbox"])
    st = _state(live)
    assert (st["status"], st["phase"]) == ("needs_land", "land-error"), st
    assert "needs_land (land-error)" in (live / "LOG.md").read_text()
    before = len(world.events)
    code = world.run_land(spec) if resume == "land" else world.run_loop(spec)
    assert code == 0
    assert calls["n"] == 2
    assert [e["kind"] for e in world.events[before:]] == []  # no Lead, no integration-eval
    assert git(home, "show", "main:src/le.py") == "# le1"
    subjects = git(home, "log", "--format=%s", "main")
    assert subjects.count("loop: iteration 1 — SHIP") == 1, subjects
    assert "status: shipped" in (spec["root_box"] / "STATE.md").read_text()


def test_f2_pre_fix_error_land_error_mailbox_is_resumable(world, tmp_path, monkeypatch):
    """A mailbox an rc driver left at `error`/`land-error` lands via `land`."""
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "r\n"})
    spec = world.add_loop(home, "loop/old", [{"id": "o1", "write": "src/o.py"}])
    _flaky_land(world, monkeypatch)
    assert world.run_loop(spec) == 8
    live = Path(world.rf.load_record(world.wt, home, spec["slug"])["live_mailbox"])
    text = (live / "STATE.md").read_text().replace("status: needs_land", "status: error")
    (live / "STATE.md").write_text(text)
    before = len(world.events)
    assert world.run_land(spec) == 0
    assert world.events[before:] == []
    assert git(home, "show", "main:src/o.py") == "# o1"

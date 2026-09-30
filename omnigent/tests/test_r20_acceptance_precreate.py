"""r20: with frozen acceptance ON, `trioctl omnigent loop` refuses a target
whose COMMITTED metrics/ set is below ACCEPTANCE_METRICS_API before any Lead
worktree, trio/ branch or registry record exists (exit 3, a message naming
`trioctl omnigent metrics refresh --mailbox <mb> --commit`), in open-loop and
lockstep -- whether the driver loads the repository's core (raw release CLI)
or the release's own API-7 core (installed-adapter binding). Switch OFF: an
API-6 target still runs."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from r16_harness import GIT_ENV, SCRIPT, World, git, init_repo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    return World(tmp_path, monkeypatch, tag="r20pre")


def _api6_home(tmp_path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    for name in ("trio-metrics.py", "trio_loop.py"):
        path = home / "metrics" / name
        path.write_text(re.sub(r"^METRICS_API = 7$", "METRICS_API = 6", path.read_text(),
                               count=1, flags=re.M))
    git(home, "commit", "-qam", "vendor an r17 (METRICS_API 6) core")
    return home


def _nothing_created(world, home, spec):
    rec = world.rf.load_record(world.wt, home, spec["slug"])
    assert rec is None or rec.get("state") == "removed", rec
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    common = Path(git(home, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    loops = common / "trio-worktrees" / "loops"
    assert not loops.exists() or not any(loops.iterdir())


def _refused(err, spec):
    assert "frozen acceptance needs METRICS_API 7 committed on main (METRICS_API 6)" in err
    assert f"trioctl omnigent metrics refresh --mailbox {spec['root_box']} --commit" in err
    assert "--no-acceptance" in err and "Nothing was created" in err


@pytest.mark.parametrize("lockstep", [False, True])
def test_raw_cli_refuses_before_create(world, tmp_path, capsys, lockstep):
    home = _api6_home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}], lockstep=lockstep)
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3
    _refused(capsys.readouterr().err, spec)
    _nothing_created(world, home, spec)
    assert world.events == []
    # Switch off: the same API-6 target runs (and ships) unchanged.
    assert world.run_loop(spec) == 0


@pytest.mark.parametrize("lockstep", [False, True])
def test_adapter_binding_release_core_still_refuses_an_api6_target(
        world, tmp_path, monkeypatch, capsys, lockstep):
    """The installed adapter binds `_load_trio_loop` to the RELEASE core (API
    7), so the loaded-core guard alone would pass; the committed set decides."""
    t = world.trioctl
    real = t._load_trio_loop
    monkeypatch.setattr(t, "_load_trio_loop", lambda repo: real(ROOT))
    assert t._core_metrics_api(real(ROOT)) >= t.ACCEPTANCE_METRICS_API
    home = _api6_home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}], lockstep=lockstep)
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3
    _refused(capsys.readouterr().err, spec)
    _nothing_created(world, home, spec)
    assert world.events == []


def test_reattach_to_an_api6_loop_branch_is_refused_with_the_switch_on(world, tmp_path,
                                                                       monkeypatch, capsys):
    """A re-attached Lead worktree runs its loop branch's committed set: the
    same floor applies, and the existing worktree is left as it is."""
    t = world.trioctl
    home = _api6_home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    monkeypatch.setattr(t, "_command_loop_run", lambda *a, **kw: 9)
    assert world.run_loop(spec) == 9  # switch off: Lead worktree created, loop "stopped"
    rec = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(rec)
    before = git(home, "worktree", "list")
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3
    err = capsys.readouterr().err
    assert (f"committed on this loop's branch {rec['branch']} (METRICS_API 6)" in err
            and "Nothing was created" in err)
    assert git(home, "worktree", "list") == before


def test_api7_target_passes_the_precheck(world, tmp_path):
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    args = world.loop_args({"root_box": home / "loop"}, "--acceptance")
    assert t._acceptance_target_refusal(args, home, "main", home / "loop",
                                        t._target_metrics_api(home, "main")) is None
    off = world.loop_args({"root_box": home / "loop"})
    assert t._acceptance_target_refusal(off, home, "main", home / "loop", 6) is None


def test_real_cli_subprocess_refuses_before_create(tmp_path):
    """The release CLI as a process (no in-process bindings)."""
    env = dict(os.environ, **GIT_ENV, HOME=str(tmp_path / "userhome"),
               XDG_STATE_HOME=str(tmp_path / "state"), PYTHONDONTWRITEBYTECODE="1")
    env.pop("TRIO_ACCEPTANCE", None)
    (tmp_path / "userhome").mkdir()
    home = _api6_home(tmp_path)
    from r16_harness import write_root_mailbox  # noqa: PLC0415
    box = write_root_mailbox(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    proc = subprocess.run([sys.executable, str(SCRIPT), "omnigent", "loop", "--acceptance",
                           "--mailbox", str(box), "--max-iterations", "1"],
                          cwd=home, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3, proc.stderr
    assert f"metrics refresh --mailbox {box} --commit" in proc.stderr
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1


# ---------------------------------------------------------------- r20 review F2
def _progressed_reattach(world, tmp_path, monkeypatch):
    """An API-6 loop started with the switch OFF that made progress on its
    `trio/<slug>` branch (so a moved target never re-seeds it), then stopped."""
    t = world.trioctl
    home = _api6_home(tmp_path)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    monkeypatch.setattr(t, "_command_loop_run", lambda *a, **kw: 9)
    assert world.run_loop(spec) == 9  # switch off: Lead worktree created, loop "stopped"
    rec = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(rec)
    lead = Path(rec["path"])
    (lead / "src" / "a.py").write_text("progress = 1\n")
    git(lead, "add", "src/a.py")
    git(lead, "commit", "-qm", "a-one: progress on the loop branch")
    return t, home, spec, rec, lead


def _refresh(t, *argv):
    return t.command_metrics_refresh(t.parser().parse_args(["omnigent", "metrics", "refresh", *argv]))


def _branch_api(t, home, branch):
    return t._target_metrics_api(home, branch)


def test_reattach_advice_names_the_working_exits(world, tmp_path, monkeypatch, capsys):
    t, home, spec, rec, lead = _progressed_reattach(world, tmp_path, monkeypatch)
    branch = rec["branch"]
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3
    err = capsys.readouterr().err
    assert (f"frozen acceptance needs METRICS_API 7 committed on this loop's branch {branch} "
            f"(METRICS_API 6), which its Lead worktree {lead} runs; refreshing main does not "
            f"reach {branch}.") in err
    assert f"`trioctl omnigent metrics refresh --repo {lead} --commit`" in err
    assert "resume with --no-acceptance" in err
    assert (f"`cd {home} && trioctl omnigent abandon --mailbox {spec['root_box']}` "
            f"(it keeps {branch}: take what you need from it, then `git branch -D {branch}`), "
            f"refresh the target with main checked out (`trioctl omnigent metrics refresh "
            f"--mailbox {spec['root_box']} --commit`) and start the loop again.") in err
    assert "Nothing was created" in err
    # The r20-rc advice ("refresh --mailbox ... with trio/<slug> checked out") is gone ...
    assert f"with {branch} checked out" not in err
    # ... and following it never helped: the target moves, the loop branch does not.
    monkeypatch.chdir(home)
    assert _refresh(t, "--mailbox", str(spec["root_box"]), "--commit") == 0
    assert _branch_api(t, home, "main") == 7 and _branch_api(t, home, branch) == 6
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3
    assert f"committed on this loop's branch {branch} (METRICS_API 6)" in capsys.readouterr().err


def test_reattach_exit_1_refresh_inside_the_lead_worktree(world, tmp_path, monkeypatch, capsys):
    t, home, spec, rec, lead = _progressed_reattach(world, tmp_path, monkeypatch)
    before = git(home, "worktree", "list", "--porcelain")
    before = [l for l in before.splitlines() if not l.startswith("HEAD ")]
    assert _refresh(t, "--repo", str(lead), "--commit") == 0
    assert "chore: vendor trio loop core" in git(lead, "log", "-1", "--format=%s")
    assert _branch_api(t, home, rec["branch"]) == 7
    assert _branch_api(t, home, "main") == 6  # the target is untouched
    assert world.run_loop(spec, "--acceptance") == 9  # past the pre-check: re-attached
    after = git(home, "worktree", "list", "--porcelain")
    assert [l for l in after.splitlines() if not l.startswith("HEAD ")] == before


def test_reattach_exit_2_no_acceptance_overrides_env_and_profile(world, tmp_path, monkeypatch):
    """A dashboard resume passes no flag: the env (or profile) decides; the
    advised --no-acceptance resumes the loop switch-off."""
    t, home, spec, rec, lead = _progressed_reattach(world, tmp_path, monkeypatch)
    monkeypatch.setenv("TRIO_ACCEPTANCE", "1")
    assert world.run_loop(spec) == 3
    assert world.run_loop(spec, "--no-acceptance") == 9


def test_reattach_exit_3_abandon_refresh_target_restart(world, tmp_path, monkeypatch, capsys):
    t, home, spec, rec, lead = _progressed_reattach(world, tmp_path, monkeypatch)
    branch = rec["branch"]
    progress = git(home, "rev-parse", branch).strip()
    monkeypatch.chdir(home)
    assert t.command_abandon(t.parser().parse_args(
        ["omnigent", "abandon", "--mailbox", str(spec["root_box"])])) == 0
    assert not lead.exists()
    # the branch is kept, and the loop's progress on it with it
    assert subprocess.run(["git", "-C", str(home), "merge-base", "--is-ancestor", progress, branch],
                          env={**os.environ, **GIT_ENV}).returncode == 0
    assert _refresh(t, "--mailbox", str(spec["root_box"]), "--commit") == 0
    assert _branch_api(t, home, "main") == 7
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 3  # the kept branch blocks a restart ...
    assert f"git branch -D {branch}" in capsys.readouterr().err
    git(home, "branch", "-D", branch)                  # ... as the advice says
    capsys.readouterr()
    assert world.run_loop(spec, "--acceptance") == 9, capsys.readouterr().err
    new = world.rf.load_record(world.wt, home, spec["slug"])
    assert world.rf.active(new) and _branch_api(t, home, new["branch"]) == 7

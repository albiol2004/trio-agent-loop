"""r15 guard fixes in trioctl (eval-r15a F1, F2, F8).

F1: an installed trioctl (install.sh layout: siblings in one bin dir, no
metrics/) finds its own trio-check.py/trio-metrics.py. F2: the loop-start
guard runs under the mailbox lock; a mailbox a live driver owns is left
byte-identical (exit 5). F8: refusal LOG labels never run ahead of STATE.md.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from metrics import trio_loop

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_r15_repo_scope_guard as guard  # noqa: E402

ROOT = HERE.parent
REPO_ROOT = ROOT.parent
INSTALLED = (
    "trioctl", "broker_http.py", "reconcile.py", "worker_worktrees.py",
    "worker_events.py", "trioctl.example.toml",
)


def _install_layout(tmp_path: Path, with_checker: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in INSTALLED:
        shutil.copy2(ROOT / name, bin_dir / name)
    if with_checker:
        for name in ("trio-check.py", "trio-metrics.py"):
            shutil.copy2(REPO_ROOT / "metrics" / name, bin_dir / name)
    return bin_dir


def _run_isolated(bin_dir: Path, home: Path, box: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(bin_dir / "trioctl"), "omnigent", "run", "builder",
         "--config", str(bin_dir / "trioctl.example.toml"), "--isolate",
         "--mailbox", str(box), "--worker-slice", "bridge",
         "--workspace", str(home), "--prompt-file", str(box / "briefs" / "bridge.md")],
        capture_output=True, text=True, cwd=home,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )


def test_install_sh_ships_the_checker_next_to_trioctl():
    text = (REPO_ROOT / "install.sh").read_text()
    assert 'cp "$ROOT/metrics/trio-check.py" "$TRIOCTL_BIN_DIR/trio-check.py"' in text
    assert 'cp "$ROOT/metrics/trio-metrics.py" "$TRIOCTL_BIN_DIR/trio-metrics.py"' in text


def test_installed_layout_runs_the_guard(tmp_path):
    home, box = guard.make_home(tmp_path, guard.plan(guard.OK + guard.BAD))
    bin_dir = _install_layout(tmp_path)
    proc = _run_isolated(bin_dir, home, box)
    assert proc.returncode == 2, proc.stderr
    assert "guard unavailable" not in proc.stderr
    assert guard.message("bridge", home / "app-backend" / "app" / "x.py") in proc.stderr
    assert len(guard.worktrees(home)) == 1


def test_installed_layout_without_checker_names_every_candidate(tmp_path):
    home, box = guard.make_home(tmp_path, guard.plan(guard.OK + guard.BAD))
    bin_dir = _install_layout(tmp_path, with_checker=False)
    proc = _run_isolated(bin_dir, home, box)
    assert proc.returncode != 0
    assert "guard unavailable: trio-check.py is missing" in proc.stderr
    assert str(bin_dir / "trio-check.py") in proc.stderr


def test_loop_start_refusal_leaves_a_live_drivers_mailbox_untouched(
    tmp_path, monkeypatch, capsys
):
    # r16b: root-free's own `.lock`-owner check (`_root_lock_refused`) now
    # runs before it even looks at PLAN.md, so a mailbox a live driver
    # owns is refused (exit 5) without ever reaching the repo-scope guard
    # -- still left byte-identical, still under the same `.lock`. (A
    # non-git mailbox cannot exercise this instead: its repo-scope guard
    # never fires at all -- see test_r15_repo_scope_guard.py's `in_repo`
    # cases -- so `OmnigentRunner` would actually be constructed here.)
    home, box = guard.make_home(tmp_path, guard.plan(guard.OK + guard.BAD))
    (box / ".lock").mkdir()
    (box / ".lock" / "pid").write_text(f"{os.getpid()}\n")  # a live owner
    before = {name: (box / name).read_bytes() for name in ("STATE.md", "LOG.md")}
    monkeypatch.chdir(home)
    monkeypatch.setattr(guard.trioctl, "OmnigentRunner", guard.NoRunner)
    monkeypatch.setattr(guard.trioctl, "_load_trio_loop", lambda repo: trio_loop)
    args = guard._loop_args(box)
    assert args.func(args) == 5
    assert {name: (box / name).read_bytes() for name in before} == before
    assert (box / ".lock" / "pid").read_text() == f"{os.getpid()}\n"
    err = capsys.readouterr().err
    assert "owned by a live driver" in err


def test_loop_start_refusal_takes_and_releases_the_lock(tmp_path, monkeypatch):
    # r16b: this offending PLAN is refused by root-free's own pre-check
    # (before any Lead worktree, or its `.lock`, ever exists) -- stderr
    # only, the root's own mailbox never written (see
    # test_r15_repo_scope_guard.py::test_loop_start_refuses_offending_plan).
    home, box = guard.make_home(tmp_path, guard.plan(guard.OK + guard.BAD))
    monkeypatch.chdir(home)
    monkeypatch.setattr(guard.trioctl, "OmnigentRunner", guard.NoRunner)
    monkeypatch.setattr(guard.trioctl, "_load_trio_loop", lambda repo: trio_loop)
    args = guard._loop_args(box)
    assert args.func(args) == 3
    assert not (box / ".lock").exists()
    assert "status: error" not in (box / "STATE.md").read_text().splitlines()


def test_open_loop_refusal_labels_never_run_ahead(tmp_path, monkeypatch):
    home, box = guard.make_home(tmp_path, guard.plan(guard.OK))
    seen: list[str] = []
    runner = guard._runner(home, monkeypatch, seen, guard._lead_writes_bad_plan)
    assert trio_loop.run_loop(box, 3, runner, repo=home, poll_seconds=0.01) == 3
    loop_lines = [ln for ln in (box / "LOG.md").read_text().splitlines() if "| loop |" in ln]
    labels = [int(re.match(r"- iter (\d+) \|", ln).group(1)) for ln in loop_lines]
    assert labels == sorted(labels), loop_lines
    state_iter = re.search(r"^iteration: (\d+)", (box / "STATE.md").read_text(), re.M)
    assert max(labels) <= int(state_iter.group(1))

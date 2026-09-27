"""r14 M-1: `trioctl omnigent loop` makes the mailbox ignore its runtime files.

Live defect (release 4680b7e): nothing ignored `.dispatch/`, `driver.log`
or `.driver.pid`, and a coordinator committed them. The loop now appends
the missing runtime entries to `<mailbox>/.gitignore` (append-only,
idempotent). The file may stay untracked; the dirty-checkout gate never
treats it (or the ignored runtime files) as a blocking product change.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from test_worker_worktrees import GIT_ENV, MODULE, SCRIPT, _load, git

RUNTIME = [".dispatch/", ".driver.json", ".driver.pid", ".session.json",
           ".sessions/", "driver.log", ".lock", ".repairs"]


@pytest.fixture()
def trioctl():
    return _load("trioctl_r14_gitignore", SCRIPT)


@pytest.fixture()
def wt(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return _load("worker_worktrees_r14_gitignore", MODULE)


def test_fresh_mailbox_gets_every_runtime_entry_once(trioctl, tmp_path):
    box = tmp_path / "loop"
    box.mkdir()
    assert trioctl._ensure_mailbox_gitignore(box) == RUNTIME
    text = (box / ".gitignore").read_text()
    assert [ln for ln in text.splitlines() if not ln.startswith("#")] == RUNTIME
    assert trioctl._ensure_mailbox_gitignore(box) == []
    assert (box / ".gitignore").read_text() == text


def test_existing_lines_are_kept_verbatim_and_only_missing_appended(trioctl, tmp_path):
    box = tmp_path / "loop"
    box.mkdir()
    original = "app-backend/\nresults/*.json\n/.dispatch\n.lock/\n!driver.log\n.repairs/"
    (box / ".gitignore").write_text(original)  # no trailing newline
    added = trioctl._ensure_mailbox_gitignore(box)
    # `.repairs/` is dir-only and does not cover the `.repairs` counter file.
    assert added == [".driver.json", ".driver.pid", ".session.json", ".sessions/", ".repairs"]
    text = (box / ".gitignore").read_text()
    assert text.startswith(original + "\n")
    assert text[len(original) + 1:].splitlines() == added
    assert trioctl._ensure_mailbox_gitignore(box) == []


def test_loop_start_ensures_the_mailbox_gitignore(trioctl, tmp_path, monkeypatch):
    box = tmp_path / "loop"
    monkeypatch.chdir(tmp_path)
    def stop(_repo):
        raise trioctl.TrioctlError("stop after housekeeping")

    # Stop right after the start-of-loop housekeeping.
    monkeypatch.setattr(trioctl, "_load_trio_loop", stop)
    args = argparse.Namespace(mailbox=str(box))
    with pytest.raises(trioctl.TrioctlError, match="stop after housekeeping"):
        trioctl._command_loop(args)
    lines = (box / ".gitignore").read_text().splitlines()
    assert all(entry in lines for entry in RUNTIME)


def test_held_refusal_leaves_the_mailbox_untouched(trioctl, tmp_path, monkeypatch):
    box = tmp_path / "loop"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trioctl, "_held_dispatch_message", lambda mailbox: "held")
    assert trioctl._command_loop(argparse.Namespace(mailbox=str(box))) == trioctl.HELD_DISPATCH_EXIT
    assert not (box / ".gitignore").exists()


@pytest.mark.parametrize("plan", ["plan without writes\n",
                                  "```yaml\nslices:\n  - id: a\n    writes: [app.py]\n```\n"])
def test_untracked_mailbox_gitignore_and_runtime_files_never_block(trioctl, wt, tmp_path, plan):
    repo = tmp_path / "product"
    box = repo / "loop"
    box.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    (box / "PLAN.md").write_text(plan)
    (box / "STATE.md").write_text("status: running\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    trioctl._ensure_mailbox_gitignore(box)
    for name in (".driver.json", ".driver.pid", ".session.json", "driver.log", ".repairs"):
        (box / name).write_text("x\n")
    for name in (".dispatch", ".sessions", ".lock"):
        (box / name).mkdir()
        (box / name / "f").write_text("x\n")
    status = git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert status == ["?? loop/.gitignore"]
    found = wt.classify_aggregate(repo, box)
    assert found == {"product": [], "foreign": [], "ignored": []}
    assert wt.aggregate_blockers(repo, box) == []

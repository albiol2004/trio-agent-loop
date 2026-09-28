"""eval-r16rc low findings: N5 (a start refused at registration removes the
Lead worktree it just made), N6 (gitignored mailbox: clear refusal, nothing
created), N8 (needs_land names the repos already landed; merge notes
survive a `land` resume), N9 (root-free multi-repo integration wording)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16rc_lows")


def _home(tmp_path, extra=None):
    home = tmp_path / "home"
    files = {"README.md": "home\n", "src/__init__.py": ""}
    files.update(extra or {})
    init_repo(home, "main", files)
    return home


def test_n5_start_refused_at_registration_removes_its_new_lead_worktree(
    world, tmp_path, monkeypatch, capsys
):
    home = _home(tmp_path)
    a = world.add_loop(home, "loop/oa", [{"id": "oa-one", "write": "src/shared.py"}])
    b = world.add_loop(home, "loop/ob", [{"id": "ob-one", "write": "src/shared.py"}])
    go, inlead = threading.Event(), threading.Event()
    codes = {}

    def lead_hook(w, s, runner, ctx, ws, box, prompt, it):
        if s is a:
            inlead.set()
            go.wait(60)
        return False

    world.hooks["lead-pass"] = lead_hook
    t = threading.Thread(target=lambda: codes.__setitem__("a", world.run_loop(a)))
    t.start()
    try:
        assert inlead.wait(60)
        real = world.trioctl._writes_overlap_problems
        calls = {"n": 0}

        def racy(root, mailbox):
            calls["n"] += 1  # the pre-check (outside the flock) lost the race
            return [] if calls["n"] == 1 else real(root, mailbox)

        monkeypatch.setattr(world.trioctl, "_writes_overlap_problems", racy)
        capsys.readouterr()
        codes["b"] = world.run_loop(b)
        err = capsys.readouterr().err
        monkeypatch.setattr(world.trioctl, "_writes_overlap_problems", real)
    finally:
        go.set()
        t.join(120)
    assert codes["b"] == 2, err
    assert "refused at registration" in err
    rec = world.rf.load_record(world.wt, home, b["slug"])
    assert rec is not None and rec["state"] == "removed"
    assert "trio/loop--ob" not in git(home, "branch", "--list")
    assert not Path(rec["path"]).exists()
    assert codes["a"] == 0


def test_n6_gitignored_mailbox_is_refused_with_a_remedy(world, tmp_path, capsys):
    home = _home(tmp_path, {".gitignore": "loop/\n"})
    spec = world.add_loop(home, "loop/ig", [{"id": "ig-one", "write": "src/ig.py"}])
    capsys.readouterr()
    assert world.run_loop(spec) == 3
    err = capsys.readouterr().err
    assert "is ignored by git" in err and "Nothing was created" in err
    assert "git add" not in err
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert world.events == []


def _layout_b(world, tmp_path):
    home = tmp_path / "home"
    init_repo(home, "main", {
        ".gitignore": "loop/x/app-backend/\nloop/x/app-frontend/\n",
        "README.md": "home\n", "docs/index.md": "docs\n",
    })
    box = home / "loop" / "x"
    init_repo(box / "app-backend", "dev", {"app/core.py": "x = 1\n"}, metrics=False)
    init_repo(box / "app-frontend", "feat/ui", {"src/app.js": "//\n"}, metrics=False)
    repos_block = (
        "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n"
        "  - name: app-frontend\n    path: loop/x/app-frontend\n    base: feat/ui\n"
    )
    slices = [
        {"id": "be-a", "repo": "app-backend", "write": "app/a.py"},
        {"id": "fe-b", "repo": "app-frontend", "write": "src/b.js"},
        {"id": "home-c", "repo": "home", "write": "docs/c.md"},
    ]
    spec = world.add_loop(
        home, "loop/x", slices, repos_block=repos_block,
        full_check="full_check:\n  app-backend: test -f app/a.py\n  app-frontend: true\n  home: true",
    )
    return home, spec, box


def test_n8_needs_land_names_landed_repos_and_keeps_merge_notes(world, tmp_path):
    home, spec, box = _layout_b(world, tmp_path)
    be = box / "app-backend"

    def integ(w, s, runner, ctx, ws, mbox, prompt, it):
        (be / "other.txt").write_text("user\n")  # the backend base moves (disjoint)
        git(be, "add", "other.txt")
        git(be, "commit", "-q", "-m", "user: other")
        (home / "docs/c.md").write_text("USER\n")  # blocks the home land
        return False

    world.hooks["integration-eval"] = integ
    assert world.run_loop(spec) == 8
    live = Path(world.rf.load_record(world.wt, home, spec["slug"])["live_mailbox"])
    line = [ln for ln in (live / "LOG.md").read_text().splitlines()
            if "needs_land (land-blocked)" in ln][-1]
    assert "already landed: app-backend@" in line and "app-frontend@" in line, line
    (home / "docs/c.md").unlink()
    world.hooks.clear()
    assert world.run_land(spec) == 0
    final = [ln for ln in (home / "loop/x/LOG.md").read_text().splitlines()
             if "onto main" in ln][-1]
    assert "merged app-backend:dev@" in final, final


def test_n9_root_free_multi_repo_integration_prompt_names_the_loop_branch(world, tmp_path):
    home, spec, box = _layout_b(world, tmp_path)
    seen = {}

    def integ(w, s, runner, ctx, ws, mbox, prompt, it):
        seen["prompt"] = prompt
        return False

    world.hooks["integration-eval"] = integ
    assert world.run_loop(spec) == 0
    assert "in its aggregate (on the loop branch), make ONE" in seen["prompt"]
    assert "checked-out base branch, make ONE" not in seen["prompt"]

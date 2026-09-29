#!/usr/bin/env python3
"""eval6 (dash-actions round 6): LAB/dash/eval6/repros, inverted.

1. layouts6.sh / probe-layouts6.py cases 1, 1b, 1c — a workspace repository
   with ``loop/`` holding sub-mailboxes, one of which is its own repository's
   top level (a plain ``git init`` in the mailbox; shipped and NEEDS_HUMAN):
   board card, detail, actions and answer are 200 and the answer is written;
   the plain sibling is unaffected. A foreign ``.git`` in an intermediate
   directory (``loop-grp/.git`` above a mailbox that is its own repository)
   is still 403. The mailbox repository's own config never runs a program.
3. probe-keys.sh (LOW) — the diagnosis snapshot's ``git diff HEAD --binary``
   runs no configured ``diff.external`` or textconv program.
4. honest-gate.sh (LOW) — the retire_ship preview says the retirement commit
   is made unsigned and without repository hooks.
"""
from __future__ import annotations

import shlex
import stat
import subprocess
import unittest
import urllib.parse
from pathlib import Path

from . import test_serve_dash_actions as D
from .test_serve_eval3 import NH_STATE, _AnswerBase

la = D.la
_git = D._git
_mailbox = D._mailbox

SHIPPED = "status: shipped\nphase: idle\niteration: 1\nmax_iterations: 5\n"


class _Hooked(_AnswerBase):
    def setUp(self):
        super().setUp()
        self.marker = self.home / "own-repo-pwned.log"
        self.hook = self.home / "hook.sh"
        self.hook.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {shlex.quote(str(self.marker))}\n"
                             "cat\n")
        self.hook.chmod(self.hook.stat().st_mode | stat.S_IXUSR)

    def ran(self) -> str:
        return self.marker.read_text() if self.marker.exists() else ""

    def arm(self, repo: Path) -> None:
        """Programs in *repo*'s own config that SAFE_GIT_CONFIG must keep
        from running (the dashboard's read-only calls)."""
        for key in ("core.fsmonitor", "core.pager", "gpg.program", "diff.external",
                    "diff.x.textconv", "diff.x.command"):
            _git(repo, "config", key, str(self.hook))
        _git(repo, "config", "log.showSignature", "true")

    def get(self, path: str, **query) -> tuple[int, dict]:
        return D._request("GET", f"{self.base}{path}?{urllib.parse.urlencode(query)}")


class OwnRepoSubMailboxTests(_Hooked):
    """eval6 finding 1 (repros/layouts6.sh case 1/1b/1c, inverted)."""

    def build(self) -> dict:
        _git(self.root, "init", "-q", "-b", "main")
        (self.root / "app.txt").write_text("x\n")
        sib = _mailbox(self.root, "loop/add-lever", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
        (sib / "LOG.md").write_text("# log\n")
        (self.root / "loop" / "PLAN.md").write_text("# plan\n")
        (self.root / "loop" / "LOG.md").write_text("# log\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "init")
        shipped = _mailbox(self.root, "loop/frontend-sp", SHIPPED, "VERDICT: SHIP\n")
        (shipped / "LOG.md").write_text("# log\n")
        (shipped / ".gitignore").write_text("/frontend/\n")
        _git(shipped, "init", "-q", "-b", "main")
        _git(shipped, "add", "-A")
        _git(shipped, "commit", "-q", "-m", "loop: iteration 1 — SHIP")
        nh = _mailbox(self.root, "loop/nested-nh", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
        (nh / "LOG.md").write_text("# log\n")
        _git(nh, "init", "-q", "-b", "main")
        _git(nh, "add", "-A")
        _git(nh, "commit", "-q", "-m", "c")
        self.arm(shipped)
        self.arm(nh)
        self.marker.unlink(missing_ok=True)
        return {"loop/add-lever": sib, "loop/frontend-sp": shipped, "loop/nested-nh": nh}

    def test_the_helper_accepts_only_a_real_own_top_level(self):
        boxes = self.build()
        for name in ("loop/frontend-sp", "loop/nested-nh"):
            with self.subTest(name=name):
                self.assertTrue(la.mailbox_own_repo(boxes[name]))
                self.assertFalse(la.mailbox_nested_git(boxes[name], self.root, self.home))
                self.assertFalse(la.mailbox_nested_git(boxes[name]))
        self.assertFalse(la.mailbox_own_repo(boxes["loop/add-lever"]))
        self.assertFalse(la.mailbox_own_repo(self.root / "nope"))
        # a gitfile or a link named .git is no own repository
        for kind in ("file", "link"):
            with self.subTest(kind=kind):
                box = _mailbox(self.root, f"loop/g-{kind}", NH_STATE)
                if kind == "file":
                    (box / ".git").write_text(f"gitdir: {boxes['loop/nested-nh']}/.git\n")
                else:
                    (box / ".git").symlink_to(boxes["loop/nested-nh"] / ".git")
                self.assertFalse(la.mailbox_own_repo(box))
                self.assertTrue(la.mailbox_nested_git(box, self.root, self.home))
                self.assertTrue(la.mailbox_nested_git(box))
        # a .git directory that is no repository (show-toplevel fails)
        bogus = _mailbox(self.root, "loop/bogus", NH_STATE)
        (bogus / ".git").mkdir()
        self.assertFalse(la.mailbox_own_repo(bogus))
        self.assertTrue(la.mailbox_nested_git(bogus, self.root, self.home))
        self.assertEqual(self.ran(), "")

    def test_board_detail_actions_and_answer_are_200(self):
        boxes = self.build()
        self.start_server()
        for name in ("loop/frontend-sp", "loop/nested-nh", "loop/add-lever"):
            with self.subTest(name=name):
                self.assertNotIn("refused", self.card(name))
                status, data = self.get("/api/loop", root=str(self.root), name=name)
                self.assertEqual(status, 200, data)
                self.actions(name)
        for name in ("loop/nested-nh", "loop/add-lever"):
            with self.subTest(answer=name):
                self.answer(name)
                self.assertTrue((boxes[name] / "HUMAN.md").is_file())
        self.assertFalse((boxes["loop/frontend-sp"] / "HUMAN.md").exists())
        self.assertEqual(self.ran(), "")

    def test_the_context_uses_the_mailbox_repository(self):
        boxes = self.build()
        box = boxes["loop/nested-nh"]
        ctx = la.LoopContext(home=self.home, root=self.root, name="loop/nested-nh",
                             root_mailbox=box, live_mailbox=box, detection={}, driver=None)
        self.assertEqual(Path(ctx.repo_root).resolve(), box.resolve())
        la._snapshot_files(ctx)
        self.assertEqual(self.ran(), "")

    def test_a_foreign_git_between_an_own_repo_mailbox_and_the_root_is_403(self):
        self.build()
        box = _mailbox(self.root, "loop-grp/m1", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
        (box / "LOG.md").write_text("# log\n")
        _git(box, "init", "-q", "-b", "main")
        grp = self.root / "loop-grp"
        _git(grp, "init", "-q", ".")
        self.arm(grp)
        self.marker.unlink(missing_ok=True)
        self.assertTrue(la.mailbox_own_repo(box))
        self.assertTrue(la.mailbox_nested_git(box, self.root, self.home))
        self.start_server()
        status, data = self.get("/api/loop/actions", root=str(self.root), loop="loop-grp/m1")
        self.assertEqual(status, 403, data)
        status, data = self.get("/api/loop", root=str(self.root), name="loop-grp/m1")
        self.assertEqual(status, 403, data)
        self.assertEqual(self.card("loop-grp/m1")["refused"], "mailbox contains a .git entry")
        status, data = self.post("/api/loop/answer", {"root": str(self.root),
                                                      "loop": "loop-grp/m1",
                                                      "answer": "x", "reset": True})
        self.assertEqual(status, 403, data)
        self.assertFalse((box / "HUMAN.md").exists())
        with self.assertRaises(la.PathEscape):
            la.LoopContext(home=self.home, root=self.root, name="loop-grp/m1", root_mailbox=box,
                           live_mailbox=box, detection={}, driver=None)
        # the sibling own-repo mailbox stays readable
        status, data = self.get("/api/loop", root=str(self.root), name="loop/nested-nh")
        self.assertEqual(status, 200, data)
        self.assertEqual(self.ran(), "")


class SnapshotDiffTests(_Hooked):
    """eval6 finding 3 (LOW): the snapshot diff runs no external diff or
    textconv program of the workspace's own config."""

    def test_the_snapshot_diff_runs_no_configured_diff_program(self):
        box = self.repo_with_stop("loop-diff")
        (self.root / ".gitattributes").write_text("* diff=x\n")
        (self.root / "f.txt").write_text("a\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "f")
        self.arm(self.root)
        (self.root / "f.txt").write_text("b\n")
        self.marker.unlink(missing_ok=True)
        ctx = la.LoopContext(home=self.home, root=self.root, name="loop-diff", root_mailbox=box,
                             live_mailbox=box, detection={}, driver=None)
        first = la._snapshot_files(ctx)
        self.assertEqual(self.ran(), "")
        self.assertIn("<git diff>", first)
        (self.root / "f.txt").write_text("c\n")
        self.assertNotEqual(la._snapshot_files(ctx)["<git diff>"], first["<git diff>"])
        self.assertEqual(self.ran(), "")
        # control: the same diff without the flags runs the program
        subprocess.run(["git", *la.SAFE_GIT_CONFIG, "-C", str(self.root), "diff", "HEAD",
                        "--binary"], capture_output=True, stdin=subprocess.DEVNULL)
        self.assertNotEqual(self.ran(), "")


class RetireShipPreviewTests(_AnswerBase):
    """eval6 finding 4 (LOW): the preview says unsigned and hook-less."""

    def test_the_preview_says_the_commit_is_unsigned_and_skips_hooks(self):
        _git(self.root, "init", "-q", "-b", "main")
        box = _mailbox(self.root, "loop-ret", "iteration: 2\nstatus: needs_retirement\n",
                       "VERDICT: SHIP\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "seed")
        ctx = la.LoopContext(home=self.home, root=self.root, name="loop-ret", root_mailbox=box,
                             live_mailbox=box, detection={}, driver=None)
        notes = " ".join(la.plan_fix(ctx, "retire_ship")["notes"])
        self.assertIn("unsigned", notes)
        self.assertIn("without repository hooks", notes)
        self.assertIn("SAFE_GIT_CONFIG", notes)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""eval4 (dash-actions round 4): LAB/dash/eval4/repros, inverted.

2. nested-git-fsmonitor.sh — every dashboard git call on a mailbox's
   repository runs with ``-c core.fsmonitor=false -c core.hooksPath=/dev/null
   -c protocol.file.allow=never``; a mailbox holding a ``.git`` entry is
   refused as a whole (403 / a "refused" card), so its config is never read.
4. trioctl ``_prompt`` fills placeholders in one pass: a path containing
   ``{repo}`` is not re-substituted; normal prompts are byte-identical.
5. an answer with a lone UTF-16 surrogate is a clean 400 (nothing written).
6. ``plan_basis`` carries the GOAL.md digest: a GOAL.md swapped between the
   answer preview and its confirm changes the confirm token.
(1, the native pin retry key, is tested on the native side and in
metrics.human_ledger.retry_eligible below.)
"""
from __future__ import annotations

import json
import shlex
import stat
import subprocess
import urllib.parse
from pathlib import Path

from . import test_serve_dash_actions as D
from .test_serve_eval3 import NH_STATE, _AnswerBase, _load

REPO_ROOT = D.REPO_ROOT
la = D.la
serve = D.serve
_git = D._git
_mailbox = D._mailbox


class NestedGitTests(_AnswerBase):
    """eval4 finding 2 (repros/nested-git-fsmonitor.sh, inverted)."""

    def hook(self) -> tuple[Path, Path]:
        marker = self.home / "fsm-pwned.log"
        hook = self.home / "fsm-hook.sh"
        hook.write_text(f"#!/bin/sh\necho ran >> {shlex.quote(str(marker))}\nexit 1\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        return hook, marker

    def get(self, path: str, **query) -> tuple[int, dict]:
        return D._request("GET", f"{self.base}{path}?{urllib.parse.urlencode(query)}")

    def test_a_mailbox_that_is_its_own_repo_is_read_but_its_fsmonitor_never_runs(self):
        # eval6 finding 1: a mailbox that is its own repository's top level
        # is accepted (eval4 refused it); SAFE_GIT_CONFIG still keeps its
        # config's programs from running.
        box = self.repo_with_stop("loop-fsm")
        hook, marker = self.hook()
        _git(box, "init", "-q", ".")
        _git(box, "config", "core.fsmonitor", str(hook))
        _git(box, "commit", "-q", "--allow-empty", "-m", "x")
        marker.unlink(missing_ok=True)
        self.start_server()
        self.actions("loop-fsm")
        status, data = self.get("/api/loop", root=str(self.root), name="loop-fsm")
        self.assertEqual(status, 200, data)
        self.assertNotIn("refused", self.card("loop-fsm"))
        self.answer("loop-fsm")
        self.assertTrue((box / "HUMAN.md").is_file())
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_a_git_file_or_link_named_dot_git_is_refused_too(self):
        for kind in ("file", "link"):
            with self.subTest(kind=kind):
                box = _mailbox(self.root, f"loop-{kind}", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
                if kind == "file":
                    (box / ".git").write_text("gitdir: /nowhere\n")
                else:
                    (box / ".git").symlink_to(self.home)
                self.assertTrue(la.mailbox_nested_git(box))
                with self.assertRaises(la.PathEscape):
                    la.LoopContext(home=self.home, root=self.root, name=f"loop-{kind}",
                                   root_mailbox=box, live_mailbox=box, detection={}, driver=None)
        self.assertFalse(la.mailbox_nested_git(self.root / "nope"))

    def test_the_workspace_repos_own_fsmonitor_and_hooks_never_run(self):
        box = self.repo_with_stop("loop-ws")
        hook, marker = self.hook()
        _git(self.root, "config", "core.fsmonitor", str(hook))
        hooks = self.root / ".git" / "hooks"
        for name in ("pre-commit", "post-commit", "commit-msg"):
            (hooks / name).write_text(hook.read_text())
            (hooks / name).chmod(0o755)
        marker.unlink(missing_ok=True)
        self.start_server()
        self.actions("loop-ws")
        self.board()
        self.get("/api/loop", root=str(self.root), name="loop-ws")
        self.answer("loop-ws")
        self.assertTrue((box / "HUMAN.md").exists())
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        # the retirement commit the dashboard plans runs without hooks
        (box / "VERDICT.md").write_text("VERDICT: SHIP\niteration: 2\n")
        (box / "STATE.md").write_text("iteration: 2\nstatus: needs_retirement\nphase: idle\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--no-verify", "-m", "ship")
        (box / "LOG.md").write_text("# log\n- ship\n")
        marker.unlink(missing_ok=True)   # the test's own git calls above ran it
        ctx = la.LoopContext(home=self.home, root=self.root, name="loop-ws", root_mailbox=box,
                             live_mailbox=box, detection={}, driver=None)
        plan = la.plan_fix(ctx, "retire_ship")
        gits = [s["argv"] for s in plan["steps"] if s.get("argv") and s["argv"][0] == "git"]
        self.assertEqual(len(gits), 2, plan["steps"])
        for argv in gits:
            self.assertEqual(argv[1:1 + len(la.SAFE_GIT_CONFIG)], list(la.SAFE_GIT_CONFIG))
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        # control: the same repo's git status without the overrides runs it
        subprocess.run(["git", "-C", str(self.root), "status"], capture_output=True)
        self.assertTrue(marker.exists())

    def test_every_git_argv_carries_the_safe_config(self):
        # the source scan over all git-calling files is eval5's
        # (test_serve_eval5.SafeGitConfigTests); the tuple is human_ledger's.
        self.assertIs(serve.SAFE_GIT_CONFIG, la.SAFE_GIT_CONFIG)
        self.assertEqual(la.ledger().SAFE_GIT_CONFIG, la.SAFE_GIT_CONFIG)
        for flag in ("core.fsmonitor=false", "core.hooksPath=/dev/null", "protocol.file.allow=never"):
            self.assertIn(flag, la.SAFE_GIT_CONFIG)
        n = len(la.SAFE_GIT_CONFIG)
        self.assertEqual(la.git_argv(Path("/r"), "status")[:n + 1], ["git", *la.SAFE_GIT_CONFIG])

class PromptSinglePassTests(D._Base):
    """eval4 finding 4: trioctl ``_prompt`` fills its placeholders once."""

    def runner(self, repo: Path):
        trioctl = _load("eval4_trioctl_prompt", REPO_ROOT / "omnigent" / "trioctl")
        return trioctl, trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={},
                                               interval=0, workspace=str(repo))

    def test_a_path_with_a_placeholder_is_not_resubstituted(self):
        repo = self.root / "x {repo} {iteration}"
        box = _mailbox(repo, "loop {mailbox}", "iteration: 1\nstatus: running\nphase: idle\n")
        _trioctl, runner = self.runner(repo)
        for role in ("lead", "evaluator"):
            with self.subTest(role=role):
                prompt = runner._prompt(role, 7, box, {})
                self.assertIn(shlex.quote(str(box.resolve())), prompt)
                self.assertIn(shlex.quote(str(repo)), prompt)

    def test_plain_prompts_are_byte_identical_to_sequential_replace(self):
        box = _mailbox(self.root, "loop", "iteration: 1\nstatus: running\nphase: idle\n")
        trioctl, runner = self.runner(self.root)
        for role in ("lead", "evaluator"):
            with self.subTest(role=role):
                template = runner._prompt_path(role).read_text(encoding="utf-8")
                for placeholder, value in (("{mailbox}", trioctl._pp(box.resolve())),
                                           ("{iteration}", "3"), ("{repo}", trioctl._pp(self.root))):
                    template = template.replace(placeholder, value)
                expected = template.rstrip() + "\n"
                if role == "evaluator":
                    # r19 C1: a lockstep Evaluator prompt also carries the
                    # whole-goal rigor block, appended after the fill.
                    expected = expected.rstrip("\n") + "\n\n" + runner._integration_rigor()
                self.assertEqual(runner._prompt(role, 3, box, {}), expected)


class AnswerInputTests(_AnswerBase):
    """eval4 findings 5 and 6."""

    def test_a_lone_surrogate_is_a_clean_400(self):
        box = self.repo_with_stop()
        self.start_server()
        for text in ("\ud800", "ok \udfff tail", "a\ud83d"):
            with self.subTest(text=repr(text)):
                status, data = self.post("/api/loop/answer", {"root": str(self.root),
                                                              "loop": "loop-nh", "answer": text,
                                                              "reset": True})
                self.assertEqual(status, 400, data)
                self.assertIn("surrogate", data["error"])
        self.assertFalse((box / "HUMAN.md").exists())
        self.assertFalse((self.state() / "answers.jsonl").exists())
        ctx = la.LoopContext(home=self.home, root=self.root, name="loop-nh", root_mailbox=box,
                             live_mailbox=box, detection={}, driver=None)
        with self.assertRaises(la.FixRefused):
            la.plan_answer(ctx, "x\ud800", True, {})
        # a real astral character (a surrogate pair in JSON) is fine
        status, data = self.confirmed("/api/loop/answer", {"root": str(self.root), "loop": "loop-nh",
                                                           "answer": "PASSED \U0001f600",
                                                           "reset": True})
        self.assertEqual(status, 200, data)

    def test_goal_swapped_between_preview_and_confirm_changes_the_token(self):
        box = self.repo_with_stop()
        self.start_server()
        payload = {"root": str(self.root), "loop": "loop-nh", "answer": "PASSED", "reset": True}
        status, data = self.post("/api/loop/answer", payload)
        self.assertEqual(status, 409, data)
        self.assertIn("goal", data["plan"]["basis"])
        token = data["plan"]["confirm_token"]
        (box / "GOAL.md").write_text("# Goal B: something else\n")
        status, data = self.post("/api/loop/answer", {**payload, "confirm": True,
                                                      "confirm_token": token})
        self.assertEqual(status, 409, data)
        self.assertTrue(data.get("plan_changed"), data)
        self.assertFalse((box / "HUMAN.md").exists())


class RetryKeyTests(D._Base):
    """eval4 finding 1 (shared module): only an execution-unique native key
    re-receives a consumed answer."""

    def test_only_execution_unique_keys_are_retry_eligible(self):
        lg = la.ledger()
        e = "0123456789abcdef0123456789abcdef"
        self.assertTrue(lg.retry_eligible(f"native:{e}:tok/{e}/5/pin@3"))
        for key in ("", "native:tok/5/pin@3", f"native:{e[:31]}:x@3", f"native:{e}:x", None):
            with self.subTest(key=key):
                self.assertFalse(lg.retry_eligible(key))


if __name__ == "__main__":
    import unittest
    unittest.main()

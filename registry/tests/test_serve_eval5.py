#!/usr/bin/env python3
"""eval5 (dash-actions round 5): LAB/dash/eval5/repros, inverted.

1. embedded-bare-slice.sh — a PLAN.md slice ``repo:`` pointing at a TRACKED
   bare-repository layout whose ``config`` sets ``log.showSignature`` and
   ``gpg.program``: no git call of the dashboard or of trio-shadow (the slice
   attribution and trioctl's commit gate) runs it. One SAFE_GIT_CONFIG
   (metrics/human_ledger.py; trio-metrics.py keeps a compared copy) is on
   every git argv of serve.py, loop_actions.py, human_ledger.py,
   trio-metrics.py and trio-shadow.py (AST scan), and every option in it is
   valid on this git and keeps honest calls working (the unsigned retirement
   commit included).
2. nested-git-parent.sh — a ``.git`` entry anywhere between a mailbox and
   the workspace root (``loop-grp/.git`` for ``loop-grp/m1``) is refused.
3. git-layouts5.sh case C — a workspace root that is both the mailbox and its
   repository's top level is accepted again (its ``.git`` is its own).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from . import test_serve_dash_actions as D
from .test_serve_eval3 import NH_STATE, _AnswerBase

REPO_ROOT = D.REPO_ROOT
la = D.la
serve = D.serve
_git = D._git
_mailbox = D._mailbox

SCANNED = ("dashboard/serve.py", "dashboard/loop_actions.py", "metrics/human_ledger.py",
           "metrics/trio-metrics.py", "metrics/trio-shadow.py")
REQUIRED = ("core.fsmonitor=false", "core.hooksPath=/dev/null", "protocol.file.allow=never",
            "safe.bareRepository=explicit", "log.showSignature=false", "core.pager=cat",
            "gpg.program=/bin/false", "gpg.ssh.program=/bin/false",
            "gpg.x509.program=/bin/false", "core.askPass=", "credential.helper=",
            "commit.gpgSign=false", "core.sshCommand=false")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shadow = _load("trio_eval5_shadow", "metrics/trio-shadow.py")
metrics = shadow._METRICS


def _hook(directory: Path, marker: Path) -> Path:
    hook = directory / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho \"ran $*\" >> {shlex.quote(str(marker))}\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hook


def _embedded_bare(root: Path, rel: str, marker: Path, scratch: Path) -> Path:
    """repros/embedded-bare-slice.sh: a bare layout as ordinary files under
    *root*/*rel* whose one commit is signed and titled ``slice(x): …``, with a
    tracked ``config`` naming ``./hook.sh`` as gpg.program."""
    src = scratch / "bare-src"
    subprocess.run(["git", "init", "-q", "--bare", str(src)], check=True)
    tree = subprocess.run(["git", "-C", str(src), "mktree"], input="", capture_output=True,
                          text=True, check=True).stdout.strip()
    body = (f"tree {tree}\nauthor a <a@b> 1 +0000\ncommitter a <a@b> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n AAAA\n -----END PGP SIGNATURE-----\n"
            "\nslice(x): attributed\n")
    commit = subprocess.run(["git", "-C", str(src), "hash-object", "-t", "commit", "-w",
                             "--stdin"], input=body, capture_output=True, text=True,
                            check=True).stdout.strip()
    subprocess.run(["git", "-C", str(src), "update-ref", "refs/heads/main", commit], check=True)
    subprocess.run(["git", "-C", str(src), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    layout = root / rel
    layout.mkdir(parents=True)
    for name in ("HEAD", "objects", "refs"):
        subprocess.run(["cp", "-r", str(src / name), str(layout)], check=True)
    (layout / "config").write_text(
        "[core]\n\tbare = true\n[log]\n\tshowSignature = true\n[gpg]\n\tprogram = ./hook.sh\n")
    _hook(layout, marker)
    return layout


class SafeGitConfigTests(unittest.TestCase):
    """eval5 finding 1: one SAFE_GIT_CONFIG, valid, on every git argv."""

    def test_one_source_of_truth_with_every_required_option(self):
        ledger = la.ledger()
        self.assertIs(la.SAFE_GIT_CONFIG, serve.SAFE_GIT_CONFIG)
        self.assertEqual(la.SAFE_GIT_CONFIG, ledger.SAFE_GIT_CONFIG)
        # trio-metrics.py is vendored stand-alone: an identical copy
        self.assertEqual(metrics.SAFE_GIT_CONFIG, ledger.SAFE_GIT_CONFIG)
        self.assertIs(shadow.SAFE_GIT_CONFIG, metrics.SAFE_GIT_CONFIG)
        pairs = ledger.SAFE_GIT_CONFIG
        self.assertEqual(pairs[0::2], ("-c",) * (len(pairs) // 2))
        self.assertEqual(set(pairs[1::2]), set(REQUIRED))
        # diff.external= makes git 2.43 run an empty command on `git diff`
        self.assertFalse(any(v.startswith("diff.external") for v in pairs[1::2]))

    def test_every_git_argv_in_the_scanned_files_carries_it(self):
        for rel in SCANNED:
            with self.subTest(file=rel):
                tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
                seen = 0
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                        continue
                    head = node.elts[0]
                    if not (isinstance(head, ast.Constant) and head.value == "git"):
                        continue
                    seen += 1
                    starred = [e.value.id for e in node.elts[1:3] if isinstance(e, ast.Starred)
                               and isinstance(e.value, ast.Name)]
                    self.assertIn("SAFE_GIT_CONFIG", starred,
                                  f"{rel}:{node.lineno} git argv without SAFE_GIT_CONFIG")
                self.assertGreater(seen, 0, rel)

    def test_every_option_is_valid_and_honest_calls_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            env = {**os.environ, **D.GIT_ENV}
            cfg = list(la.SAFE_GIT_CONFIG)

            def git(*args, cwd=repo):
                return subprocess.run(["git", *cfg, *args], cwd=cwd, capture_output=True,
                                      text=True, env=env, stdin=subprocess.DEVNULL)

            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            _git(repo, "config", "commit.gpgSign", "true")   # would need a signing program
            (repo / "a.txt").write_text("a\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "init")
            (repo / "a.txt").write_text("a\nb\n")
            for args in (("log", "--oneline"), ("log", "-p", "-1"), ("show", "--stat", "HEAD"),
                         ("status", "--porcelain"), ("diff",), ("diff", "--name-status", "HEAD"),
                         ("rev-parse", "--show-toplevel"), ("worktree", "list", "--porcelain"),
                         ("rev-parse", "--path-format=absolute", "--git-common-dir")):
                with self.subTest(args=args):
                    proc = git(*args)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
            # a linked worktree (the root-free Lead layout) works too
            wt = Path(tmp) / "wt"
            self.assertEqual(git("worktree", "add", "-q", "-b", "t", str(wt)).returncode, 0)
            self.assertEqual(git("status", "--porcelain", cwd=wt).returncode, 0)
            self.assertEqual(git("worktree", "remove", str(wt)).returncode, 0)
            # the retirement commit (retire_ship add + commit -- <rel>) is unsigned
            self.assertEqual(git("add", "--", "a.txt").returncode, 0)
            proc = git("commit", "-q", "-m", "loop: iteration 2 — SHIP", "--", "a.txt")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("gpgsig", _git(repo, "cat-file", "commit", "HEAD"))
            # control: without the overrides the signing config breaks it
            (repo / "a.txt").write_text("c\n")
            _git(repo, "add", "-A")
            self.assertNotEqual(subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo,
                                               capture_output=True, env=env).returncode, 0)


class EmbeddedBareSliceTests(_AnswerBase):
    """eval5 finding 1 (repros/embedded-bare-slice.sh, inverted)."""

    def setUp(self):
        super().setUp()
        self.marker = self.home / "slice-pwned.log"

    def ran(self) -> str:
        return self.marker.read_text() if self.marker.exists() else ""

    def build(self) -> tuple[Path, Path]:
        box = self.repo_with_stop("loop-slice")
        (box / "STATE.md").write_text("status: running\nphase: build\niteration: 1\n"
                                      "max_iterations: 5\n")
        (box / "PLAN.md").write_text("# plan\n\n```yaml\nslices:\n  - id: x\n"
                                     "    repo: loop-slice/attr\n    writes: [a.txt]\n```\n")
        layout = _embedded_bare(self.root, "loop-slice/attr", self.marker, self.home)
        _git(self.root, "add", "-f", "-A")
        _git(self.root, "commit", "-q", "-m", "loop-slice: plan + tracked bare layout")
        self.marker.unlink(missing_ok=True)
        return box, layout

    def test_the_payload_is_live_without_the_safe_config(self):
        _box, layout = self.build()
        subprocess.run(["git", "log", "-1"], cwd=layout, capture_output=True,
                       stdin=subprocess.DEVNULL)
        self.assertIn("ran", self.ran())

    def test_the_dashboard_never_runs_it(self):
        self.build()
        self.start_server()
        for path, query in (("/api/loop", {"name": "loop-slice"}),
                            ("/api/board", {}),
                            ("/api/loop/actions", {"loop": "loop-slice"})):
            with self.subTest(path=path):
                status, data = D._request(
                    "GET", f"{self.base}{path}?"
                    + urllib.parse.urlencode({"root": str(self.root), **query}))
                self.assertEqual(status, 200, data)
                self.assertEqual(self.ran(), "")
        # the slice layout is no repository for the attribution: no commits
        self.assertIsNone(shadow.slice_commits("x", self.root / "loop-slice" / "attr"))

    def test_trio_shadow_and_the_commit_gate_never_run_it(self):
        box, _layout = self.build()
        # the CLI resolves a pre-r15 `repo:` against the mailbox directory
        (box / "PLAN.md").write_text("# plan\n\n```yaml\nslices:\n  - id: x\n"
                                     "    repo: attr\n    writes: [a.txt]\n```\n")
        for extra in ((), ("--json",), ("--require-commits",)):
            with self.subTest(args=extra):
                subprocess.run([sys.executable, str(REPO_ROOT / "metrics" / "trio-shadow.py"),
                                "--mailbox", str(box), *extra], cwd=self.root,
                               capture_output=True, stdin=subprocess.DEVNULL)
                self.assertEqual(self.ran(), "")

    def test_an_honest_slice_commit_still_passes_the_gate(self):
        box = self.repo_with_stop("loop-honest")
        (box / "PLAN.md").write_text("# plan\n\n```yaml\nslices:\n  - id: s1\n"
                                     "    writes: [app.txt]\n```\n")
        (self.root / "app.txt").write_text("changed\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "slice(s1): change app")
        proc = subprocess.run([sys.executable, str(REPO_ROOT / "metrics" / "trio-shadow.py"),
                               "--mailbox", str(box), "--require-commits"], cwd=self.root,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(shadow.slice_commits("s1", self.root)), 1)


class NestedGitLayoutTests(_AnswerBase):
    """eval5 findings 2 (repros/nested-git-parent.sh, inverted) and 3
    (repros/git-layouts5.sh case C)."""

    def get(self, path: str, **query) -> tuple[int, dict]:
        return D._request("GET", f"{self.base}{path}?{urllib.parse.urlencode(query)}")

    def test_a_git_entry_in_a_parent_below_the_root_is_refused(self):
        self.repo_with_stop("loop-ok")
        box = _mailbox(self.root, "loop-grp/m1", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
        (box / "LOG.md").write_text("# log\n")
        marker = self.home / "parent-pwned.log"
        hook = _hook(self.home, marker)
        grp = self.root / "loop-grp"
        _git(grp, "init", "-q", ".")
        _git(grp, "config", "core.fsmonitor", str(hook))
        _git(grp, "config", "log.showSignature", "true")
        _git(grp, "config", "gpg.program", str(hook))
        self.assertTrue(la.mailbox_nested_git(box, self.root, self.home))
        self.assertFalse(la.mailbox_nested_git(self.root / "loop-ok", self.root, self.home))
        marker.unlink(missing_ok=True)
        self.start_server()
        status, data = self.get("/api/loop/actions", root=str(self.root), loop="loop-grp/m1")
        self.assertEqual(status, 403, data)
        self.assertTrue(data.get("refused"))
        status, data = self.get("/api/loop", root=str(self.root), name="loop-grp/m1")
        self.assertEqual(status, 403, data)
        self.assertEqual(self.card("loop-grp/m1")["refused"], "mailbox contains a .git entry")
        self.assertNotIn("refused", self.card("loop-ok"))
        status, data = self.post("/api/loop/answer", {"root": str(self.root),
                                                      "loop": "loop-grp/m1",
                                                      "answer": "x", "reset": True})
        self.assertEqual(status, 403, data)
        self.assertFalse((box / "HUMAN.md").exists())
        self.assertEqual(marker.read_text() if marker.exists() else "", "")
        with self.assertRaises(la.PathEscape):
            la.LoopContext(home=self.home, root=self.root, name="loop-grp/m1", root_mailbox=box,
                           live_mailbox=box, detection={}, driver=None)

    def test_a_mailbox_that_is_its_own_top_level_below_the_root_stays_refused(self):
        box = _mailbox(self.root, "loop-own", NH_STATE, "VERDICT: NEEDS_HUMAN\n")
        _git(box, "init", "-q", ".")
        self.assertTrue(la.mailbox_nested_git(box, self.root, self.home))

    def test_a_root_that_is_the_mailbox_and_its_repo_top_level_is_accepted(self):
        _git(self.root, "init", "-q", "-b", "main")
        _mailbox(self.root, ".", NH_STATE, "VERDICT: NEEDS_HUMAN\niteration: 2\n")
        (self.root / "LOG.md").write_text("# log\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "loop: iteration 2 — NEEDS_HUMAN")
        self.assertFalse(la.mailbox_nested_git(self.root, self.root, self.home))
        self.start_server()
        cards = self.board()["loops"]
        self.assertEqual(len(cards), 1, cards)
        name = cards[0]["name"]
        self.assertNotIn("refused", cards[0])
        self.actions(name)
        status, data = self.get("/api/loop", root=str(self.root), name=name)
        self.assertEqual(status, 200, data)
        self.answer(name)
        self.assertTrue((self.root / "HUMAN.md").is_file())
        # a foreign .git in a directory below it would still be refused
        (self.root / "sub").mkdir()
        (self.root / "sub" / ".git").write_text("gitdir: /nowhere\n")
        self.assertTrue(la.mailbox_nested_git(self.root / "sub", self.root, self.home))

    def test_a_linked_lead_worktree_anchors_its_live_copy(self):
        box = self.repo_with_stop("loop-rf")
        common = Path(_git(self.root, "rev-parse", "--path-format=absolute",
                           "--git-common-dir")).resolve()
        key = __import__("hashlib").sha256(str(common).encode()).hexdigest()[:12]
        wt = la.trio_worktree_base(self.home) / f"{self.root.name}-{key}" / "lead-loop-rf"
        _git(self.root, "worktree", "add", "-q", "-b", "trio/loop-rf", str(wt))
        live = wt / "loop-rf"
        self.assertTrue(live.is_dir())
        self.assertFalse(la.mailbox_nested_git(live, self.root, self.home))
        self.assertFalse(la.mailbox_nested_git(box, self.root, self.home))
        # a foreign repository inside the worktree, above a mailbox, is not
        (wt / "loop-grp" / "m1").mkdir(parents=True)
        (wt / "loop-grp" / ".git").mkdir()
        self.assertTrue(la.mailbox_nested_git(wt / "loop-grp" / "m1", self.root, self.home))
        # outside the workspace and its worktrees: refused
        self.assertTrue(la.mailbox_nested_git(self.home, self.root, self.home))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Canonical loop identities (dashboard/loop_ids.py) against real git
repositories: one loop id from the main checkout and any linked worktree,
and the same id on every card the dashboard serves."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"
LOOP_IDS_PATH = REPO_ROOT / "dashboard" / "loop_ids.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


loop_ids = _load("trio_loop_ids_under_test", LOOP_IDS_PATH)
serve = _load("trio_dashboard_serve_loop_ids", SERVE_PATH)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env,
                          check=True, capture_output=True, text=True).stdout


def write_mailbox(path: Path, status: str = "shipped",
                  verdict: str = "VERDICT: SHIP\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "GOAL.md").write_text(f"# Mission: {path.name}\n", "utf-8")
    (path / "STATE.md").write_text(f"status: {status}\niteration: 2\n", "utf-8")
    (path / "VERDICT.md").write_text(verdict, "utf-8")
    (path / "LOG.md").write_text("## iter 1\n", "utf-8")


def _get(url: str):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        finally:
            exc.close()


class RepoFixture(unittest.TestCase):
    """A main checkout with mailboxes ``loop-x`` and ``loop/nested``, plus a
    linked worktree of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.main = self.base / "product"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        write_mailbox(self.main / "loop-x", "needs_human",
                      "VERDICT: NEEDS_HUMAN\n\n## Iteration 3\n")
        write_mailbox(self.main / "loop" / "nested")
        (self.main / "README.md").write_text("product\n", "utf-8")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "baseline")
        self.wt = self.base / "product-wt"
        git(self.main, "worktree", "add", "-q", "-b", "wt-branch", str(self.wt))
        loop_ids.clear_cache()


class RepoIdentityTests(RepoFixture):
    def test_main_checkout_and_linked_worktree_share_one_common_dir(self):
        main = loop_ids.repo_identity(self.main / "loop-x")
        linked = loop_ids.repo_identity(self.wt / "loop-x")
        common = str((self.main / ".git").resolve())
        self.assertEqual(main, {"common_dir": common,
                                "toplevel": str(self.main),
                                "main_worktree": str(self.main)})
        self.assertEqual(linked, {"common_dir": common,
                                  "toplevel": str(self.wt),
                                  "main_worktree": str(self.main)})

    def test_outside_git_is_none_and_files_resolve_through_their_parent(self):
        outside = self.base / "plain" / "loop"
        outside.mkdir(parents=True)
        self.assertIsNone(loop_ids.repo_identity(outside))
        self.assertEqual(
            loop_ids.repo_identity(self.main / "loop-x" / "STATE.md")["toplevel"],
            str(self.main))

    def test_bare_repository_worktree_has_no_main_worktree(self):
        bare = self.base / "bare.git"
        git(self.base, "clone", "-q", "--bare", str(self.main), str(bare))
        wt = self.base / "bare-wt"
        git(bare, "worktree", "add", "-q", "-b", "bw", str(wt), "main")
        loop_ids.clear_cache()
        identity = loop_ids.repo_identity(wt / "loop-x")
        self.assertEqual(identity["common_dir"], str(bare.resolve()))
        self.assertIsNone(identity["main_worktree"])
        self.assertEqual(identity["toplevel"], str(wt))

    def test_symlinked_path_resolves_to_the_same_identity(self):
        link = self.base / "link"
        link.symlink_to(self.main)
        self.assertEqual(loop_ids.repo_identity(link / "loop-x"),
                         loop_ids.repo_identity(self.main / "loop-x"))


class CanonicalLoopTests(RepoFixture):
    def test_same_mailbox_from_main_and_worktree_has_one_id(self):
        a = loop_ids.canonical_loop(self.main / "loop-x")
        b = loop_ids.canonical_loop(self.wt / "loop-x")
        self.assertEqual(a, {**b, "toplevel": a["toplevel"]})
        self.assertEqual(a["loop_id"], b["loop_id"])
        self.assertEqual(a["loop_key"], b["loop_key"])
        common = str((self.main / ".git").resolve())
        self.assertEqual(a["loop_key"], f"{common}::loop-x")
        self.assertEqual(a["rel"], "loop-x")
        self.assertEqual(a["common_dir"], common)
        self.assertEqual(
            a["loop_id"],
            hashlib.sha256(a["loop_key"].encode()).hexdigest()[:16])

    def test_nested_mailbox_uses_posix_relative_path_and_differs_per_mailbox(self):
        nested = loop_ids.canonical_loop(self.main / "loop" / "nested")
        self.assertEqual(nested["rel"], "loop/nested")
        self.assertEqual(
            nested["loop_id"],
            loop_ids.canonical_loop(self.wt / "loop" / "nested")["loop_id"])
        self.assertNotEqual(
            nested["loop_id"],
            loop_ids.canonical_loop(self.main / "loop-x")["loop_id"])

    def test_outside_git_falls_back_to_the_real_path(self):
        outside = self.base / "plain" / "loop"
        outside.mkdir(parents=True)
        got = loop_ids.canonical_loop(outside)
        self.assertEqual(got["loop_key"], "path::" + str(outside))
        self.assertIsNone(got["common_dir"])
        self.assertEqual(
            got["loop_id"],
            hashlib.sha256(got["loop_key"].encode()).hexdigest()[:16])

    def test_other_repository_same_relative_path_is_a_different_loop(self):
        other = self.base / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        write_mailbox(other / "loop-x")
        self.assertNotEqual(
            loop_ids.canonical_loop(other / "loop-x")["loop_id"],
            loop_ids.canonical_loop(self.main / "loop-x")["loop_id"])


class ServedCardsCarryLoopIdTests(RepoFixture):
    def setUp(self):
        super().setUp()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        original = serve.HOME
        serve.HOME = Path(self.home.name)
        self.addCleanup(setattr, serve, "HOME", original)
        for cache in (serve._HEAVY_CACHE, serve._DISCOVER_CACHE):
            cache.clear()
        patcher = patch.object(serve, "BROKER_BASE_URL", "")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.main, self.wt],
            auto_discover=False)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def board(self, root: Path) -> dict:
        status, data = _get(self.url + "/api/board?"
                            + urllib.parse.urlencode({"root": str(root)}))
        self.assertEqual(status, 200, data)
        return data

    def test_board_cards_and_items_have_the_canonical_id_from_either_root(self):
        want = loop_ids.canonical_loop(self.main / "loop-x")
        for root in (self.main, self.wt):
            data = self.board(root)
            card = next(c for c in data["loops"] if c["name"] == "loop-x")
            self.assertEqual(card["loop_id"], want["loop_id"], root)
            self.assertEqual(card["loop_key"], want["loop_key"], root)
            item = next(i for i in data["inbox"] if i["kind"] == "needs_human")
            self.assertEqual(item["loop_id"], want["loop_id"], root)

    def test_loop_detail_card_has_loop_id(self):
        want = loop_ids.canonical_loop(self.main / "loop" / "nested")
        status, card = _get(self.url + "/api/loop?" + urllib.parse.urlencode(
            {"root": str(self.wt), "name": "loop/nested"}))
        self.assertEqual(status, 200, card)
        self.assertEqual(card["loop_id"], want["loop_id"])
        self.assertEqual(card["loop_key"], want["loop_key"])

    def test_overview_cards_have_loop_id(self):
        status, data = _get(self.url + "/api/overview")
        self.assertEqual(status, 200, data)
        cards = [c for w in data["workspaces"] for c in w["loops"]]
        self.assertTrue(cards)
        for card in cards:
            self.assertTrue(card.get("loop_id"), card["name"])
            self.assertTrue(card.get("loop_key"), card["name"])


if __name__ == "__main__":
    unittest.main()

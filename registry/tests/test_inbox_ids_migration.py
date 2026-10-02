#!/usr/bin/env python3
"""Stable notification ids (inbox-state v2) and the one-time v1 migration,
through the dashboard's HTTP handlers against real git repositories.

Reproduces the live failures: the same mailbox seen through a new worktree
re-surfaced read items (a), volatile anchors (whole-VERDICT digest,
``interrupted:{last_activity}``, the full drift file list) minted new ids on
every harmless write (b), and the root-keyed state file grew without bound.
"""
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


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_ids_migration", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

NEEDS_HUMAN = "VERDICT: NEEDS_HUMAN\n\n## Iteration 3\n"


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env,
                          check=True, capture_output=True, text=True).stdout


def write_mailbox(path: Path, status: str, verdict: str, iteration: int = 2):
    path.mkdir(parents=True, exist_ok=True)
    (path / "GOAL.md").write_text(f"# Mission: {path.name}\n", "utf-8")
    (path / "STATE.md").write_text(
        f"status: {status}\niteration: {iteration}\n", "utf-8")
    (path / "VERDICT.md").write_text(verdict, "utf-8")
    (path / "LOG.md").write_text("## iter 1\n", "utf-8")


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write_mailbox(path / "loop-x", "needs_human", NEEDS_HUMAN)
    write_mailbox(path / "loop-run", "running", "VERDICT: none\n")
    (path / "README.md").write_text("product\n", "utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "baseline")
    return path


def _request(method: str, url: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        finally:
            exc.close()


class IdsFixture(unittest.TestCase):
    """A repository with a linked worktree, served with both as workspaces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.main = make_repo(self.base / "product")
        self.wt = self.base / "product-wt"
        git(self.main, "worktree", "add", "-q", "-b", "wt-branch", str(self.wt))
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
        self.state_file = Path(os.environ["TRIO_DASH_INBOX_STATE"])

    def start(self, *workspaces: Path):
        server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=list(workspaces),
            auto_discover=False)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        return server

    def board(self, root: Path) -> dict:
        status, data = _request("GET", self.url + "/api/board?"
                                + urllib.parse.urlencode({"root": str(root)}))
        self.assertEqual(status, 200, data)
        return data

    def item(self, root: Path, kind: str, loop: str = "loop-x") -> dict:
        return next(i for i in self.board(root)["inbox"]
                    if i["kind"] == kind and i["loop"] == loop)

    def mark(self, root: Path, ids: list[str], read: bool = True):
        path = "/api/inbox/read" if read else "/api/inbox/unread"
        status, data = _request("POST", self.url + path,
                                {"ids": ids, "root": str(root)})
        self.assertEqual(status, 200, data)

    def document(self) -> dict:
        return json.loads(self.state_file.read_text("utf-8"))


class SharedAcrossCheckoutsTests(IdsFixture):
    def test_main_and_worktree_views_share_loop_id_item_id_and_read_state(self):
        self.start(self.main, self.wt)
        via_main = self.item(self.main, "needs_human")
        via_wt = self.item(self.wt, "needs_human")
        self.assertEqual(via_main["loop_id"], via_wt["loop_id"])
        self.assertEqual(via_main["id"], via_wt["id"])
        self.assertFalse(via_wt["read"])
        self.mark(self.main, [via_main["id"]])
        self.assertTrue(self.item(self.wt, "needs_human")["read"])
        self.mark(self.wt, [via_main["id"]], read=False)
        self.assertFalse(self.item(self.main, "needs_human")["read"])

    def test_new_worktree_does_not_resurface_a_read_item(self):
        self.start(self.main)
        first = self.item(self.main, "needs_human")
        self.mark(self.main, [first["id"]])
        late = self.base / "late-wt"
        git(self.main, "worktree", "add", "-q", "-b", "late", str(late))
        self.addCleanup(git, self.main, "worktree", "remove", "--force", str(late))
        self.start(self.main, late)
        again = self.item(late, "needs_human")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["read"])

    def test_state_file_is_schema_2_keyed_by_item_not_root(self):
        self.start(self.main, self.wt)
        item = self.item(self.wt, "needs_human")
        self.mark(self.main, [item["id"]])
        doc = self.document()
        self.assertEqual(doc["schema"], 2)
        self.assertNotIn(str(self.main), doc)
        self.assertNotIn(str(self.wt), doc)
        record = doc["items"][item["id"]]
        self.assertTrue(record["read"])
        self.assertEqual(record["loop_id"], item["loop_id"])
        self.assertEqual(record["common_dir"], str((self.main / ".git").resolve()))
        self.assertIn("first_seen", record)


class SemanticKeyTests(IdsFixture):
    def setUp(self):
        super().setUp()
        self.start(self.main)

    def test_verdict_text_rewrite_keeps_id_and_read_but_iteration_bump_mints_new(self):
        old = self.item(self.main, "needs_human")
        self.mark(self.main, [old["id"]])
        (self.main / "loop-x" / "VERDICT.md").write_text(
            NEEDS_HUMAN + "\nrewritten prose, same verdict\n", "utf-8")
        same = self.item(self.main, "needs_human")
        self.assertEqual(same["id"], old["id"])
        self.assertTrue(same["read"])
        (self.main / "loop-x" / "VERDICT.md").write_text(
            "VERDICT: NEEDS_HUMAN\n\n## Iteration 4\n", "utf-8")
        bumped = self.item(self.main, "needs_human")
        self.assertNotEqual(bumped["id"], old["id"])
        self.assertFalse(bumped["read"])

    def test_interrupted_id_survives_mailbox_writes_and_follows_the_run(self):
        box = self.main / "loop-run"
        first = self.item(self.main, "interrupted", "loop-run")
        self.mark(self.main, [first["id"]])
        with (box / "LOG.md").open("a", encoding="utf-8") as log:
            log.write("\n## iter 2\n- more work, then the process died\n")
        os.utime(box / "LOG.md", (2_000_000_000, 2_000_000_000))
        again = self.item(self.main, "interrupted", "loop-run")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["read"])
        # A recorded run id is the identity; a different run is a new item.
        (box / ".session.json").write_text(
            json.dumps({"exec_id": "run-1", "done": True}), "utf-8")
        run_one = self.item(self.main, "interrupted", "loop-run")
        self.assertNotEqual(run_one["id"], first["id"])
        with (box / "LOG.md").open("a", encoding="utf-8") as log:
            log.write("- still dead\n")
        self.assertEqual(self.item(self.main, "interrupted", "loop-run")["id"],
                         run_one["id"])
        (box / ".session.json").write_text(
            json.dumps({"exec_id": "run-2", "done": True}), "utf-8")
        self.assertNotEqual(self.item(self.main, "interrupted", "loop-run")["id"],
                            run_one["id"])

    def test_drift_id_ignores_growth_of_the_undeclared_file_list(self):
        box = self.main / "loop-run"
        (box / "PLAN.md").write_text("slices:\n", "utf-8")
        small = {"slices": [{"undeclared": ["a.py"]}]}
        grown = {"slices": [{"undeclared": ["a.py", "b.py", "c.py"]}]}
        with patch.object(serve, "_loop_slice_activity", return_value=small):
            first = self.item(self.main, "drift", "loop-run")
        self.mark(self.main, [first["id"]])
        with patch.object(serve, "_loop_slice_activity", return_value=grown):
            second = self.item(self.main, "drift", "loop-run")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["read"])
        self.assertIn("3 undeclared writes", second["headline"])
        (box / "STATE.md").write_text(
            "status: running\niteration: 3\nphase: next\n", "utf-8")
        with patch.object(serve, "_loop_slice_activity", return_value=grown):
            later = self.item(self.main, "drift", "loop-run")
        self.assertNotEqual(later["id"], first["id"])


class V1MigrationTests(IdsFixture):
    def v1_id(self, root: Path | str, loop: str, kind: str, anchor: str) -> str:
        value = "\0".join((str(root), loop, kind, anchor))
        return hashlib.sha256(value.encode()).hexdigest()

    def verdict_anchor(self, mailbox: Path, iteration: str = "3") -> str:
        digest = hashlib.sha256((mailbox / "VERDICT.md").read_bytes()).hexdigest()
        return f"{iteration}:{digest}"

    def write_v1(self, document: dict):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(document), "utf-8")

    def test_v1_document_migrates_gc_and_carries_read_marks(self):
        vanished = self.base / "gone-checkout"
        anchor = self.verdict_anchor(self.main / "loop-x")
        read_in_wt = self.v1_id(self.wt, "loop-x", "needs_human", anchor)
        self.write_v1({
            str(self.main): {"read": [], "first_seen": {"x": "2026-01-01T00:00:00Z"}},
            str(self.wt): {"read": [read_in_wt],
                           "first_seen": {read_in_wt: "2026-01-01T00:00:00Z"}},
            str(vanished): {
                "read": [self.v1_id(vanished, "loop-x", "needs_human", anchor)],
                "first_seen": {}},
        })
        self.start(self.main)
        item = self.item(self.main, "needs_human")
        self.assertTrue(item["read"], "a previously read item must stay read")
        doc = self.document()
        self.assertEqual(doc["schema"], 2)
        self.assertIn("migrated_at", doc)
        self.assertNotIn(str(vanished), doc["legacy"])
        self.assertEqual(doc["legacy"][str(self.wt)], {"read": [read_in_wt]})
        self.assertNotIn(str(self.main), doc)
        self.assertNotIn(str(self.wt), doc)
        self.assertTrue(doc["items"][item["id"]]["read"])
        self.assertNotIn(
            self.v1_id(vanished, "loop-x", "needs_human", anchor),
            json.dumps(doc))
        # The same item through the worktree is the same read item.
        self.start(self.main, self.wt)
        self.assertTrue(self.item(self.wt, "needs_human")["read"])

    def test_read_marks_of_an_unrelated_repository_are_not_inherited(self):
        other = make_repo(self.base / "other")
        anchor = self.verdict_anchor(other / "loop-x")
        self.write_v1({str(other): {
            "read": [self.v1_id(other, "loop-x", "needs_human", anchor)],
            "first_seen": {}}})
        self.start(self.main)
        self.assertFalse(self.item(self.main, "needs_human")["read"])

    def test_old_read_mark_does_not_cover_a_different_verdict_iteration(self):
        anchor = self.verdict_anchor(self.main / "loop-x")
        self.write_v1({str(self.main): {
            "read": [self.v1_id(self.main, "loop-x", "needs_human", anchor)],
            "first_seen": {}}})
        (self.main / "loop-x" / "VERDICT.md").write_text(
            "VERDICT: NEEDS_HUMAN\n\n## Iteration 4\n", "utf-8")
        self.start(self.main)
        self.assertFalse(self.item(self.main, "needs_human")["read"])

    def test_old_read_mark_of_a_rewritten_verdict_stays_unmatched_until_seen(self):
        # The v1 id hashed the whole VERDICT.md: if the file changed since,
        # the old mark cannot be mapped unambiguously and is not carried.
        old_bytes = (self.main / "loop-x" / "VERDICT.md").read_bytes()
        old_anchor = "3:" + hashlib.sha256(old_bytes).hexdigest()
        self.write_v1({str(self.main): {
            "read": [self.v1_id(self.main, "loop-x", "needs_human", old_anchor)],
            "first_seen": {}}})
        (self.main / "loop-x" / "VERDICT.md").write_text(
            NEEDS_HUMAN + "edited\n", "utf-8")
        self.start(self.main)
        self.assertFalse(self.item(self.main, "needs_human")["read"])

    def test_interrupted_read_mark_maps_through_the_legacy_anchor(self):
        last = "2026-03-04T05:06:07+00:00"
        legacy = self.v1_id(self.main, "loop-run", "interrupted",
                            f"interrupted:{last}")
        self.write_v1({str(self.main): {"read": [legacy], "first_seen": {}}})
        self.start(self.main)
        with patch.object(serve, "_last_activity", return_value=last):
            self.assertTrue(
                self.item(self.main, "interrupted", "loop-run")["read"])

    def test_garbage_and_missing_state_files_start_a_fresh_v2_document(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("not json", "utf-8")
        self.start(self.main)
        self.assertFalse(self.item(self.main, "needs_human")["read"])
        self.assertEqual(self.document()["schema"], 2)


class GarbageCollectionTests(IdsFixture):
    def test_items_of_a_vanished_repository_are_dropped_at_load(self):
        inbox = serve.load_inbox_state_module()
        live_common = str((self.main / ".git").resolve())
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "schema": 2,
            "items": {
                "alive": {"first_seen": "t", "read": True, "loop_id": "l1",
                          "common_dir": live_common},
                "dead": {"first_seen": "t", "read": True, "loop_id": "l2",
                         "common_dir": str(self.base / "vanished" / ".git")},
                "unknown-origin": {"first_seen": "t", "read": True,
                                   "loop_id": None, "common_dir": None},
            },
            "legacy": {str(self.base / "vanished"): {"read": ["z"]}},
            "migrated_at": "2026-01-01T00:00:00Z",
        }), "utf-8")
        inbox.decorate_items([], self.main, "loop-x", self.main / "loop-x",
                              serve.HOME)
        doc = json.loads(self.state_file.read_text("utf-8"))
        self.assertEqual(sorted(doc["items"]), ["alive", "unknown-origin"])
        self.assertEqual(doc["legacy"], {})
        self.assertEqual(doc["migrated_at"], "2026-01-01T00:00:00Z")

    def test_marking_an_unseen_id_read_is_remembered_and_unread_forgets_nothing_else(self):
        inbox = serve.load_inbox_state_module()
        inbox.set_read(self.main, ["future-id"], True, serve.HOME)
        doc = json.loads(self.state_file.read_text("utf-8"))
        self.assertTrue(doc["items"]["future-id"]["read"])
        self.assertEqual(doc["items"]["future-id"]["common_dir"],
                         str((self.main / ".git").resolve()))
        inbox.set_read(self.main, ["future-id", "never-seen"], False, serve.HOME)
        doc = json.loads(self.state_file.read_text("utf-8"))
        self.assertFalse(doc["items"]["future-id"]["read"])
        self.assertNotIn("never-seen", doc["items"])


if __name__ == "__main__":
    unittest.main()

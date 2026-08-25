"""Nested-mailbox discovery over HTTP and the in-place board renderer.

Loads dashboard/serve.py by path (like test_serve_pages.py) and runs it
against a temp workspace with a ``loop/`` mailbox plus a nested
``loop/foo`` mailbox that only has GOAL.md.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_board_loops", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class NestedMailboxBoardTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        root = Path(self.workspace.name)
        (root / "loop").mkdir()
        (root / "loop" / "LOG.md").write_text("# LOG\n", encoding="utf-8")
        (root / "loop" / "foo").mkdir()
        (root / "loop" / "foo" / "GOAL.md").write_text(
            "# Foo\n\nmission: nested\n", encoding="utf-8")
        (root / "loop" / "briefs").mkdir()
        (root / "loop" / "briefs" / "GOAL.md").write_text("x\n", encoding="utf-8")
        self.root = root
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[root], auto_discover=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        serve.HOME = self.original_home
        self.workspace.cleanup()
        self.home.cleanup()

    def _url(self, path: str, **query) -> str:
        query["root"] = str(self.root)
        return self.base + path + "?" + urllib.parse.urlencode(query)

    def test_board_lists_nested_mailbox(self):
        # The regression target must remain the real home, not the fake one.
        real_state = (
            Path.home() / ".local" / "share" / "trio-agent-loop"
            / "inbox-state.json"
        )
        real_exists = real_state.exists()
        real_before = real_state.stat() if real_exists else None

        status, data = _get(self._url("/api/board"))
        self.assertEqual(status, 200)
        names = [loop["name"] for loop in data["loops"]]
        self.assertEqual(names, ["loop", "loop/foo"])

        if real_before is None:
            self.assertFalse(real_state.exists())
        else:
            self.assertTrue(real_state.exists())
            real_after = real_state.stat()
            self.assertEqual(real_after.st_mtime_ns, real_before.st_mtime_ns)
            self.assertEqual(real_after.st_size, real_before.st_size)

        fake_state = (
            Path(self.home.name) / ".local" / "share" / "trio-agent-loop"
            / "inbox-state.json"
        )
        self.assertTrue(fake_state.exists())

    def test_loop_detail_accepts_slash_name(self):
        status, data = _get(self._url("/api/loop", name="loop/foo"))
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "loop/foo")

    def test_loop_detail_rejects_traversal_and_absolute(self):
        for bad in ("loop/../loop", "../loop", str(self.root / "loop"), "loop/briefs"):
            with self.subTest(name=bad):
                status, _ = _get(self._url("/api/loop", name=bad))
                self.assertEqual(status, 400)


class BoardJsInPlacePatchTests(unittest.TestCase):
    """renderBoard must patch cards in place instead of wiping the grid."""

    def test_board_uses_fact_only_tabs_and_has_no_loop_state(self):
        source = (REPO_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("function loopState", source)
        self.assertNotIn("loopState(", source)
        match = re.search(r"const TABS = \[(.*?)\];", source, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(
            re.findall(r'"([^"]+)"', match.group(1)),
            ["running", "attention", "all", "archived"],
        )

    def test_render_board_has_no_grid_wipe(self):
        source = (REPO_ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
        match = re.search(r"function renderBoard\(\) \{(.*?)\n\}\n", source, re.S)
        self.assertIsNotNone(match)
        body = match.group(1)
        self.assertNotIn('grid.textContent = "";\n  if (!state.loops.length)', body)
        for marker in ("boardSignature", "patchCard", "existing.get(loop.name)",
                       "insertBefore"):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)
        self.assertIn("state.boardSignature", source)


class FactOnlyBoardHttpTests(unittest.TestCase):
    """Board facts must not infer activity from STATE.md status text."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        root = Path(self.workspace.name)
        (root / "loop").mkdir()
        (root / "loop" / "STATE.md").write_text(
            "status: running\n", encoding="utf-8"
        )
        archive = root / "loop-archive-demo"
        archive.mkdir()
        (archive / "GOAL.md").write_text(
            "# Archived loop\n\nmission: retained\n", encoding="utf-8"
        )
        self.root = root
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[root], auto_discover=False
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, daemon=True
        ).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        serve.HOME = self.original_home
        self.workspace.cleanup()
        self.home.cleanup()

    def _board(self) -> dict:
        query = urllib.parse.urlencode({"root": str(self.root)})
        status, data = _get(f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200)
        return data

    def test_board_exposes_status_archive_and_verdict_mtime_facts(self):
        data = self._board()
        loops = {loop["name"]: loop for loop in data["loops"]}

        self.assertIn("loop", loops)
        self.assertFalse(loops["loop"]["running"])
        self.assertIn("running", loops["loop"]["status"])
        self.assertIsNone(loops["loop"]["verdict_mtime"])
        self.assertIn("loop-archive-demo", loops)

        (self.root / "loop" / "VERDICT.md").write_text(
            "VERDICT: SHIP\n", encoding="utf-8"
        )
        loops = {
            loop["name"]: loop for loop in self._board()["loops"]
        }
        self.assertIsInstance(loops["loop"]["verdict_mtime"], str)
        self.assertTrue(loops["loop"]["verdict_mtime"])

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class _RunningBrokerHandler(BaseHTTPRequestHandler):
    """Return one running session for the broker probe test."""

    def do_GET(self):
        if self.path != "/v1/sessions/sess-1":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status":"running"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class RealRunningDetectionTests(unittest.TestCase):
    """Board activity comes from live process and session evidence."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.proc = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.mailbox = self.root / "loop"
        self.mailbox.mkdir()
        (self.mailbox / "GOAL.md").write_text(
            "# Detection loop\n\nmission: running detection\n",
            encoding="utf-8",
        )
        (self.mailbox / "STATE.md").write_text(
            "iteration: 1\nstatus: running\n",
            encoding="utf-8",
        )
        (self.mailbox / "LOG.md").write_text("# LOG\n", encoding="utf-8")

        self.original_home = serve.HOME
        self.original_proc_root = serve.PROC_ROOT
        self.original_broker_url = serve.BROKER_BASE_URL
        serve.HOME = Path(self.home.name)
        serve.PROC_ROOT = Path(self.proc.name)
        serve.BROKER_BASE_URL = ""
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME = self.original_home
        serve.PROC_ROOT = self.original_proc_root
        serve.BROKER_BASE_URL = self.original_broker_url
        self.proc.cleanup()
        self.workspace.cleanup()
        self.home.cleanup()

    def _board(self) -> dict:
        query = urllib.parse.urlencode({"root": str(self.root)})
        status, data = _get(f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200)
        return data

    def _loop(self) -> dict:
        return next(loop for loop in self._board()["loops"] if loop["name"] == "loop")

    def _write_proc_cmdline(self, pid: int, command: bytes):
        process = Path(self.proc.name) / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(command)

    def test_state_running_without_evidence_is_not_running(self):
        loop = self._loop()
        self.assertFalse(loop["running"])
        self.assertEqual(loop["running_sources"], [])
        self.assertIn("running", loop["status"])

    def test_proc_mailbox_command_line_marks_loop_running(self):
        pid = 42001
        command = (
            b"python3\0--mailbox\0"
            + str(self.mailbox.resolve()).encode("utf-8")
            + b"\0"
        )
        self._write_proc_cmdline(pid, command)
        loop = self._loop()
        self.assertTrue(loop["running"])
        self.assertIn("proc", loop["running_sources"])

    def test_session_sidecar_live_then_orphaned(self):
        live_pid = 42002
        self._write_proc_cmdline(live_pid, b"python3\0worker\0")
        sidecar = {
            "driver": "portable",
            "session": "sess-sidecar",
            "pid": live_pid,
            "started_at": "2026-08-25T13:00:00Z",
            "phase": "running",
        }
        path = self.mailbox / ".session.json"
        path.write_text(json.dumps(sidecar), encoding="utf-8")
        loop = self._loop()
        self.assertTrue(loop["running"])
        self.assertIn("session", loop["running_sources"])

        dead_pid = 42003
        sidecar["pid"] = dead_pid
        path.write_text(json.dumps(sidecar), encoding="utf-8")
        board = self._board()
        loop = next(item for item in board["loops"] if item["name"] == "loop")
        self.assertFalse(loop["running"])
        self.assertEqual(loop["running_sources"], [])
        orphaned = next(item for item in board["inbox"]
                        if item["kind"] == "orphaned")
        self.assertIn(str(dead_pid), orphaned["detail"])

    def test_broker_running_session_and_failed_probe(self):
        broker = ThreadingHTTPServer(
            ("127.0.0.1", 0), _RunningBrokerHandler
        )
        broker_thread = threading.Thread(
            target=broker.serve_forever, daemon=True
        )
        broker_thread.start()
        try:
            serve.BROKER_BASE_URL = (
                f"http://127.0.0.1:{broker.server_address[1]}"
            )
            (self.mailbox / ".session.json").write_text(
                json.dumps({"session": "sess-1", "pid": 0}),
                encoding="utf-8",
            )
            loop = self._loop()
            self.assertTrue(loop["running"])
            self.assertIn("broker", loop["running_sources"])
        finally:
            broker.shutdown()
            broker.server_close()
            broker_thread.join(timeout=5)

        serve.BROKER_BASE_URL = "http://127.0.0.1:0"
        loop = self._loop()
        self.assertFalse(loop["running"])
        self.assertNotIn("broker", loop["running_sources"])

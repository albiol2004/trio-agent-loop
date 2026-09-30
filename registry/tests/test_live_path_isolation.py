#!/usr/bin/env python3
"""r20 F1: no registry/dashboard test writes the LIVE per-user dashboard
paths (the running trio-dash's inbox read-state, its state dir, the native
run registry). conftest.py points them into per-test temp paths and guards
the live inbox-state.json for the whole session; these tests prove both."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load("trio_dashboard_serve_live_paths", REPO_ROOT / "dashboard" / "serve.py")
guard = _load("trio_registry_tests_conftest_copy", HERE / "conftest.py")
REAL_HOME = Path(os.path.expanduser("~"))


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


class LivePathEnvTests(unittest.TestCase):
    """The autouse fixture applies to unittest classes as well."""

    def test_every_live_path_env_is_a_temp_path(self):
        self.assertEqual(guard.LIVE_PATH_ENV,
                         ("TRIO_DASH_INBOX_STATE", "TRIO_DASH_STATE_DIR", "TRIO_NATIVE_RUNS_DIR"))
        for name in guard.LIVE_PATH_ENV + ("XDG_STATE_HOME",):
            with self.subTest(env=name):
                value = os.environ.get(name, "")
                self.assertTrue(value, f"{name} is not set by conftest")
                self.assertFalse(Path(value).resolve().is_relative_to(REAL_HOME / ".local"),
                                 f"{name}={value} points into the real HOME")
        self.assertNotIn("TRIO_WORKTREE_ROOT", os.environ)

    def test_dashboard_defaults_resolve_to_the_temp_paths(self):
        la = serve.load_loop_actions_module()
        inbox = serve.load_inbox_state_module()
        self.assertEqual(inbox.state_path(serve.HOME), Path(os.environ["TRIO_DASH_INBOX_STATE"]))
        self.assertNotEqual(inbox.state_path(serve.HOME), guard.LIVE_INBOX_STATE)
        self.assertEqual(la.state_dir(serve.HOME), Path(os.environ["TRIO_DASH_STATE_DIR"]))
        self.assertEqual(la.native_runs_dir(serve.HOME), Path(os.environ["TRIO_NATIVE_RUNS_DIR"]))


class UnpatchedHomeServerTests(unittest.TestCase):
    """The F1 shape: a DashboardServer with the REAL serve.HOME (no patch), a
    board read (writes first_seen) and an inbox read-mark (writes read)."""

    def setUp(self):
        self.assertEqual(serve.HOME, REAL_HOME)  # the leak's precondition (unpatched)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        box = self.root / "loop"
        box.mkdir()
        (box / "GOAL.md").write_text("# Mission: loop\n")
        (box / "STATE.md").write_text("status: blocked\niteration: 2\n")
        (box / "VERDICT.md").write_text("VERDICT: none\n")
        (box / "LOG.md").write_text("## iter 1\n")
        self.server = serve.DashboardServer(("127.0.0.1", 0), workspaces=[self.root],
                                            auto_discover=False)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_board_and_read_mark_write_only_the_temp_inbox_state(self):
        live_before = _sha(guard.LIVE_INBOX_STATE)
        with urllib.request.urlopen(self.base + "/api/board?"
                                    + urllib.parse.urlencode({"root": str(self.root)})) as resp:
            board = json.loads(resp.read().decode())
        ids = [item["id"] for item in board["inbox"]]
        self.assertTrue(ids)
        req = urllib.request.Request(self.base + "/api/inbox/read", method="POST",
                                     data=json.dumps({"ids": ids[:1], "root": str(self.root)}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
        temp_state = json.loads(Path(os.environ["TRIO_DASH_INBOX_STATE"]).read_text())
        self.assertEqual(temp_state[str(self.root)]["read"], ids[:1])
        self.assertEqual(_sha(guard.LIVE_INBOX_STATE), live_before)  # None stays None


class GuardAttributionTests(unittest.TestCase):
    def test_only_test_temp_roots_count_as_a_leak(self):
        before = {"/home/u/proj": {"read": [], "first_seen": {}}}
        after = {"/home/u/proj": {"read": ["a"], "first_seen": {}},   # live service write
                 "/home/u/new": {"read": [], "first_seen": {}},       # live service write
                 "/lab/tmp/tmpabc": {"read": [], "first_seen": {"x": "t"}},
                 "/lab/tmpx": {"read": [], "first_seen": {}}}          # prefix, not under root
        self.assertEqual(guard.inbox_keys_owned_by_tests(before, after, ["/lab/tmp"]),
                         ["/lab/tmp/tmpabc"])
        self.assertEqual(guard.inbox_keys_owned_by_tests(after, after, ["/lab/tmp"]), [])
        self.assertEqual(guard.inbox_keys_owned_by_tests(before, after, ["/lab/tmp/"]),
                         ["/lab/tmp/tmpabc"])

    def test_temp_roots_are_this_sessions_only_never_bare_tmp(self):
        """r20 review round 2 (finding 4): a live-service write for any /tmp
        workspace (a claude scratchpad, dom-smoke) must not fail the session."""
        class Factory:
            @staticmethod
            def getbasetemp():
                return Path("/lab/bt/pytest-1")
        roots = guard._test_temp_roots(Factory())
        self.assertNotIn("/tmp", roots)
        self.assertIn("/lab/bt/pytest-1", roots)
        self.assertTrue(any(r.startswith("/tmp/pytest-of-") for r in roots), roots)
        self.assertIn(tempfile.gettempdir(), roots + ["/tmp"])
        self.assertEqual(guard.inbox_keys_owned_by_tests(
            {}, {"/tmp/claude-1000/x/scratchpad/ws": {"read": []}}, roots), [])

    def test_live_path_is_the_real_home_file(self):
        self.assertEqual(guard.LIVE_INBOX_STATE,
                         REAL_HOME / ".local" / "share" / "trio-agent-loop" / "inbox-state.json")


if __name__ == "__main__":
    unittest.main()

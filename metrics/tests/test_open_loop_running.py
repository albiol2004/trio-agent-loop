"""Tests for the open-loop `running_substate` field on /api/loop (PLAN.md's
frozen ``api:OpenLoopRunningAPI`` contract, slice ``dashboard-open-loop-running``).

Boots dashboard/serve.py (loaded by path, like metrics/tests/test_api_slices.py)
against a temp workspace so no real ``loop*/`` mailbox is required -- those
directories are gitignored and absent from a fresh checkout / worktree.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_open_loop_running", SERVE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _json_get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _dead_pid() -> int:
    """Return a PID that is guaranteed not to be live right now."""
    for candidate in range(2**15, 2**15 + 20000):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise RuntimeError("could not find a dead pid for the test")


class _MailboxServerTestCase(unittest.TestCase):
    """Boots dashboard/serve.py against an isolated workspace and HOME."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
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
        self.workspace.cleanup()
        self.home.cleanup()

    def make_mailbox(self, name: str) -> Path:
        mailbox = self.root / name
        mailbox.mkdir()
        (mailbox / "GOAL.md").write_text(
            "# Test loop\n\nmission: test\n", encoding="utf-8"
        )
        (mailbox / "STATE.md").write_text(
            "iteration: 1\nmax_iterations: 3\nstatus: running\n",
            encoding="utf-8",
        )
        (mailbox / "LOG.md").write_text("- iter 1 | lead | working\n",
                                         encoding="utf-8")
        (mailbox / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
        (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
        return mailbox

    def write_json(self, mailbox: Path, filename: str, payload: dict) -> None:
        (mailbox / filename).write_text(json.dumps(payload), encoding="utf-8")

    def detail(self, name: str) -> dict:
        query = urllib.parse.urlencode({"name": name, "root": str(self.root)})
        status, payload = _json_get(f"{self.base}/api/loop?{query}")
        self.assertEqual(status, 200, payload)
        return payload


class OpenLoopRunningSubstateTests(_MailboxServerTestCase):
    def test_both_alive_live_pid_yields_both(self):
        mailbox = self.make_mailbox("loop-both")
        self.write_json(mailbox, ".driver.json", {
            "pid": os.getpid(),
            "iteration": 1,
            "phase": "lead",
            "session_ids": {},
            "open_loop": True,
            "lead_alive": True,
            "eval_alive": True,
        })
        card = self.detail("loop-both")
        self.assertEqual(card["running_substate"], "both")
        self.assertTrue(card["running_sources"])

    def test_lead_only_yields_lead(self):
        mailbox = self.make_mailbox("loop-lead")
        self.write_json(mailbox, ".driver.json", {
            "pid": os.getpid(),
            "iteration": 1,
            "phase": "lead",
            "session_ids": {},
            "open_loop": True,
            "lead_alive": True,
            "eval_alive": False,
        })
        card = self.detail("loop-lead")
        self.assertEqual(card["running_substate"], "lead")

    def test_eval_only_yields_evaluator(self):
        mailbox = self.make_mailbox("loop-eval")
        self.write_json(mailbox, ".driver.json", {
            "pid": os.getpid(),
            "iteration": 1,
            "phase": "evaluator",
            "session_ids": {},
            "open_loop": True,
            "lead_alive": False,
            "eval_alive": True,
        })
        card = self.detail("loop-eval")
        self.assertEqual(card["running_substate"], "evaluator")

    def test_neither_alive_yields_null(self):
        mailbox = self.make_mailbox("loop-neither")
        self.write_json(mailbox, ".driver.json", {
            "pid": _dead_pid(),
            "iteration": 1,
            "phase": "done",
            "session_ids": {},
            "open_loop": True,
            "lead_alive": False,
            "eval_alive": False,
        })
        card = self.detail("loop-neither")
        self.assertIn("running_substate", card)
        self.assertIsNone(card["running_substate"])

    def test_lockstep_driver_json_yields_null(self):
        mailbox = self.make_mailbox("loop-lockstep")
        self.write_json(mailbox, ".driver.json", {
            "pid": os.getpid(),
            "iteration": 1,
            "phase": "lead",
            "session_ids": {},
        })
        card = self.detail("loop-lockstep")
        self.assertIn("running_substate", card)
        self.assertIsNone(card["running_substate"])

    def test_no_sidecar_at_all_yields_null(self):
        self.make_mailbox("loop-bare")
        card = self.detail("loop-bare")
        self.assertIn("running_substate", card)
        self.assertIsNone(card["running_substate"])

    def test_session_json_fallback_yields_evaluator(self):
        mailbox = self.make_mailbox("loop-fallback")
        self.write_json(mailbox, ".session.json", {
            "pid": os.getpid(),
            "iteration": 1,
            "phase": "evaluator",
            "open_loop": True,
            "lead_alive": False,
            "eval_alive": True,
            "started_at": "2026-08-26T22:00:00Z",
        })
        card = self.detail("loop-fallback")
        self.assertEqual(card["running_substate"], "evaluator")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""HTTP tests for the cross-workspace overview, health probe and goal titles."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_overview", SERVE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
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


def _mailbox(root: Path, name: str, goal: str, state: str, verdict: str = ""):
    mailbox = root / name
    mailbox.mkdir(parents=True)
    (mailbox / "GOAL.md").write_text(goal, encoding="utf-8")
    (mailbox / "STATE.md").write_text(state, encoding="utf-8")
    if verdict:
        (mailbox / "VERDICT.md").write_text(verdict, encoding="utf-8")
    return mailbox


class OverviewTests(unittest.TestCase):
    """One request returns every workspace's loops, keyed by workspace."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.a = tempfile.TemporaryDirectory()
        self.b = tempfile.TemporaryDirectory()
        self.empty = tempfile.TemporaryDirectory()
        _mailbox(Path(self.a.name), "loop",
                 "# Mission: ship the widget\n\nmission: widget\n",
                 "iteration: 2\nstatus: running\n",
                 "VERDICT: ITERATE\n")
        _mailbox(Path(self.b.name), "loop-review",
                 "# Goal\n\nprofile: software\nReview the parser.\n",
                 "iteration: 1\nstatus: needs_human\n",
                 "VERDICT: NEEDS_HUMAN\n")
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0),
            workspaces=[Path(self.a.name), Path(self.b.name),
                        Path(self.empty.name)],
            auto_discover=False,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME = self.original_home
        for tmp in (self.home, self.a, self.b, self.empty):
            tmp.cleanup()

    def overview(self) -> dict:
        status, payload = _get(self.base + "/api/overview")
        self.assertEqual(status, 200, payload)
        return payload

    def test_overview_lists_workspaces_with_loops_only(self):
        data = self.overview()
        self.assertEqual(data["scanned"], 3)
        roots = [ws["root"] for ws in data["workspaces"]]
        self.assertEqual(
            roots,
            [str(Path(self.a.name).resolve()), str(Path(self.b.name).resolve())],
        )
        for ws in data["workspaces"]:
            self.assertIn("elapsed_ms", ws)
            self.assertNotIn("error", ws)
        self.assertIn("updated_at", data)

    def test_titles_come_from_goal_heading_and_skip_generic_words(self):
        loops = {
            loop["name"]: loop
            for ws in self.overview()["workspaces"] for loop in ws["loops"]
        }
        self.assertEqual(loops["loop"]["title"], "Ship the widget")
        self.assertEqual(loops["loop-review"]["title"], "")

    def test_state_running_with_nothing_live_is_a_note_without_broker(self):
        # No broker URL: a broker-only run cannot be ruled out, so the
        # contradiction is a low-severity note, not a call to act.
        workspaces = {ws["root"]: ws for ws in self.overview()["workspaces"]}
        inbox = workspaces[str(Path(self.a.name).resolve())]["inbox"]
        kinds = {item["kind"]: item for item in inbox}
        self.assertIn("interrupted", kinds)
        item = kinds["interrupted"]
        self.assertEqual(item["severity"], "low")
        self.assertIn("running", item["headline"])
        self.assertIn("not configured", item["detail"])
        other = workspaces[str(Path(self.b.name).resolve())]["inbox"]
        self.assertNotIn("interrupted", {item["kind"] for item in other})
        self.assertIn("needs_human", {item["kind"] for item in other})

    def test_titles_strip_dash_prefixes(self):
        goal = Path(self.a.name) / "loop" / "GOAL.md"
        goal.write_text("# GOAL — cp-cy1 readiness\n", encoding="utf-8")
        self.assertEqual(serve._goal_title(goal), "Cp-cy1 readiness")

    def test_live_driver_suppresses_interrupted(self):
        mailbox = Path(self.a.name) / "loop"
        (mailbox / ".driver.json").write_text(
            json.dumps({"pid": os.getpid(), "phase": "lead"}), encoding="utf-8")
        with patch.object(serve, "OVERVIEW_CACHE_SECONDS", 0):
            self.server._overview = None
            data = self.overview()
        ws = next(w for w in data["workspaces"]
                  if w["root"] == str(Path(self.a.name).resolve()))
        self.assertTrue(ws["loops"][0]["running"])
        self.assertNotIn("interrupted", {item["kind"] for item in ws["inbox"]})

    def test_workspace_failure_is_isolated(self):
        original = serve.DashboardHandler._build_board
        bad = str(Path(self.b.name).resolve())

        def flaky(handler, root):
            if str(root) == bad:
                raise RuntimeError("mailbox exploded")
            return original(handler, root)

        with patch.object(serve.DashboardHandler, "_build_board", flaky):
            self.server._overview = None
            data = self.overview()
        by_root = {ws["root"]: ws for ws in data["workspaces"]}
        self.assertIn("mailbox exploded", by_root[bad]["error"])
        self.assertEqual(by_root[bad]["loops"], [])
        good = by_root[str(Path(self.a.name).resolve())]
        self.assertEqual(len(good["loops"]), 1)

    def test_stale_overview_is_served_while_rebuilding(self):
        first = self.overview()
        self.server._overview_at -= serve.OVERVIEW_CACHE_SECONDS + 1
        second = self.overview()
        self.assertEqual(second["updated_at"], first["updated_at"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.server._overview_building:
            time.sleep(0.05)
        self.assertFalse(self.server._overview_building)
        self.assertIsNot(self.server._overview, first)

    def test_healthz_reports_ok_and_workspace_count(self):
        status, data = _get(self.base + "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["workspaces"], 3)
        self.assertIsNone(data["overview_age_seconds"])
        self.overview()
        status, data = _get(self.base + "/healthz")
        self.assertIsInstance(data["overview_age_seconds"], float)


class ScanRootsTests(unittest.TestCase):
    def test_env_replaces_default_scan_roots(self):
        with patch.dict(os.environ,
                        {"TRIO_DASH_SCAN_ROOTS": os.pathsep.join(["/a", "~/b"])}):
            roots = serve._workspace_scan_roots()
        self.assertEqual(roots, [Path("/a"), Path("~/b").expanduser()])

    def test_default_scan_roots_are_kept_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRIO_DASH_SCAN_ROOTS", None)
            roots = serve._workspace_scan_roots()
        self.assertIn(serve.HOME / "pruebas", roots)


class ProcSnapshotTests(unittest.TestCase):
    def test_snapshot_scans_the_process_table_once(self):
        calls = []
        real = serve._live_processes

        def counting():
            calls.append(1)
            return real()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(serve, "_live_processes", counting), \
                    patch.object(serve, "BROKER_BASE_URL", ""):
                with serve._proc_snapshot():
                    for _ in range(5):
                        serve._proc_matches_mailbox(Path(tmp))
                self.assertEqual(len(calls), 1)
                serve._proc_matches_mailbox(Path(tmp))
                self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""HTTP tests for stable dashboard inbox identities and read state."""
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
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_inbox", SERVE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _json_request(method: str, url: str, payload=None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class InboxStateTests(unittest.TestCase):
    """Run the inbox API against an isolated workspace and dashboard HOME."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.mailbox = self.root / "loop"
        self.mailbox.mkdir()
        (self.mailbox / "GOAL.md").write_text(
            "# Test loop\n\nmission: inbox state\n", encoding="utf-8"
        )
        (self.mailbox / "STATE.md").write_text(
            "iteration: 3\nstatus: blocked\nphase: eval-done\n",
            encoding="utf-8",
        )
        self.write_verdict("VERDICT: NEEDS_HUMAN\n\n## Iteration 3\n")
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.start_server()

    def start_server(self):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def tearDown(self):
        self.stop_server()
        serve.HOME = self.original_home
        self.workspace.cleanup()
        self.home.cleanup()

    def write_verdict(self, text: str):
        (self.mailbox / "VERDICT.md").write_text(text, encoding="utf-8")

    def board(self) -> dict:
        query = urllib.parse.urlencode({"root": str(self.root)})
        status, payload = _json_request("GET", f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200, payload)
        return payload

    def post(self, path: str, payload) -> tuple[int, dict]:
        return _json_request("POST", self.base + path, payload)

    def inbox_item(self, kind="needs_human") -> dict:
        return next(item for item in self.board()["inbox"] if item["kind"] == kind)

    def test_needs_human_id_and_first_seen_are_stable(self):
        first = self.inbox_item()
        second = self.inbox_item()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["first_seen"], second["first_seen"])
        self.assertFalse(first["read"])

    def test_verdict_rewrite_creates_new_unread_identity(self):
        old = self.inbox_item()
        self.write_verdict("VERDICT: NEEDS_HUMAN\n\n## Iteration 3\nchanged\n")
        new = self.inbox_item()
        self.assertNotEqual(old["id"], new["id"])
        self.assertFalse(new["read"])

    def test_read_state_survives_new_server_and_unread_flips_it_back(self):
        item = self.inbox_item()
        status, payload = self.post(
            "/api/inbox/read", {"ids": [item["id"]], "root": str(self.root)}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, {"ok": True, "ids": [item["id"]]})
        self.assertTrue(self.inbox_item()["read"])

        self.stop_server()
        self.start_server()
        self.assertTrue(self.inbox_item()["read"])

        status, payload = self.post(
            "/api/inbox/unread", {"ids": [item["id"]], "root": str(self.root)}
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(self.inbox_item()["read"])

    def test_running_old_activity_has_no_stale_item(self):
        self.write_verdict("")
        state = self.mailbox / "STATE.md"
        state.write_text("iteration: 3\nstatus: running\n", encoding="utf-8")
        old = time.time() - 4 * 60 * 60
        for path in (self.mailbox / "GOAL.md", state, self.mailbox / "VERDICT.md"):
            os.utime(path, (old, old))
        self.assertNotIn("stale", {item["kind"] for item in self.board()["inbox"]})

    def test_drift_id_is_stable_for_same_undeclared_files(self):
        (self.mailbox / "PLAN.md").write_text("slices:\n", encoding="utf-8")
        activity = {
            "slices": [{"undeclared": ["z.py", "a.py"]}],
        }
        with patch.object(serve, "_loop_slice_activity", return_value=activity):
            first = self.inbox_item("drift")
            second = self.inbox_item("drift")
        self.assertEqual(first["id"], second["id"])

    def test_post_requires_ids_list(self):
        status, _payload = self.post(
            "/api/inbox/read", {"root": str(self.root)}
        )
        self.assertEqual(status, 400)

    def test_post_does_not_create_workspace_files(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        status, _payload = self.post(
            "/api/inbox/read",
            {"ids": ["unknown-id"], "root": str(self.root)},
        )
        self.assertEqual(status, 200)
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

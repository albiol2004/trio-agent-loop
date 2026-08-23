#!/usr/bin/env python3
"""HTTP tests for ``GET /api/registry/health``."""
from __future__ import annotations

import importlib.util
import json
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
        "trio_dashboard_serve_health", SERVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _http_json(method: str, url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(
                response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class DashboardHealthTestCase(unittest.TestCase):
    """Run health requests with an isolated dashboard home."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0),
            workspaces=[REPO_ROOT],
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
        self.home.cleanup()

    def url(self, root: Path | None = REPO_ROOT) -> str:
        query = {}
        if root is not None:
            query["root"] = str(root)
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        return f"{self.base}/api/registry/health{suffix}"


class HealthEndpointTests(DashboardHealthTestCase):
    def test_health_endpoint_returns_all_sections(self):
        status, payload = _http_json("GET", self.url())
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["lineage"])
        self.assertIn("generate_check", payload)
        self.assertIn("dangling", payload)
        self.assertIn("trio-lead", {
            item["name"] for item in payload["lineage"]
        })

    def test_health_endpoint_requires_root_query(self):
        status, payload = _http_json("GET", self.url(root=None))
        self.assertEqual(status, 400, payload)
        self.assertIn("root", payload["error"])

    def test_health_endpoint_rejects_foreign_root(self):
        status, payload = _http_json("GET", self.url(root=Path("/tmp")))
        self.assertEqual(status, 403, payload)
        self.assertIn("root", payload["error"])

    def test_delete_still_refuses_generator_managed_file(self):
        target = REPO_ROOT / ".claude" / "agents" / "trio-lead.md"
        before = target.read_bytes()
        query = urllib.parse.urlencode({
            "root": str(REPO_ROOT),
            "path": str(target),
        })
        status, payload = _http_json(
            "DELETE", f"{self.base}/api/registry/file?{query}")
        self.assertEqual(status, 403, payload)
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

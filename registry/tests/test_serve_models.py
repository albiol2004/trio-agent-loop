#!/usr/bin/env python3
"""HTTP tests for ``GET /api/registry/models``."""
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


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_models", SERVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(
                response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class DashboardModelsTestCase(unittest.TestCase):
    """Run the endpoint with an explicit temporary home directory."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.original_home = serve.HOME
        self.original_path = os.environ.get("PATH")
        self.empty_bin = Path(self.home.name) / "empty-bin"
        self.empty_bin.mkdir()
        os.environ["PATH"] = str(self.empty_bin)
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0),
            workspaces=[REPO_ROOT],
            auto_discover=False,
        )
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME = self.original_home
        if self.original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.original_path
        self.home.cleanup()

    def url(self, root: Path | None = REPO_ROOT) -> str:
        query = {}
        if root is not None:
            query["root"] = str(root)
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        return f"{self.base}/api/registry/models{suffix}"


class ModelsEndpointTests(DashboardModelsTestCase):
    def test_models_endpoint_returns_resolved_rows(self):
        status, payload = _get_json(self.url())
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            set(payload["available"]),
            {"claude", "codex", "omp", "opencode", "cursor", "omnigent"},
        )
        self.assertEqual(
            set(payload["by_executor"]),
            {
                "claude", "claude-native", "codex", "codex-native",
                "cursor", "cursor-native", "omp", "omp-native",
                "opencode", "opencode-native",
            },
        )
        self.assertEqual(
            set(payload["sources"]),
            set(payload["available"]),
        )
        self.assertEqual(
            payload["by_executor"]["cursor"],
            payload["by_executor"]["cursor-native"],
        )
        rows = {
            (row["harness"], row["agent"]): row
            for row in payload["rows"]
        }
        claude_lead = rows["claude", "trio-lead"]
        self.assertEqual(claude_lead["layer"], "frontmatter")
        self.assertEqual(claude_lead["model"], "opus")
        for agent in ("trio-omnigent-builder", "trio-omnigent-lead"):
            with self.subTest(agent=agent):
                self.assertTrue(
                    rows["omnigent", agent]["layer"],
                    rows["omnigent", agent],
                )

    def test_models_endpoint_requires_root_query(self):
        status, payload = _get_json(self.url(root=None))
        self.assertEqual(status, 400, payload)
        self.assertIn("root", payload["error"])


if __name__ == "__main__":
    unittest.main()

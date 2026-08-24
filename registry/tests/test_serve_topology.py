#!/usr/bin/env python3
"""HTTP tests for ``GET /api/registry/topology``.

The server uses an ephemeral port and the query selects the repository root,
so this suite never scans or writes the real user's home directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    """Load dashboard/serve.py by path, as the existing endpoint tests do."""
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_topology", SERVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get_json(url: str) -> tuple[int, dict]:
    """Return status and decoded JSON for both success and HTTP errors."""
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(
                response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class DashboardTopologyTestCase(unittest.TestCase):
    """Reuse the standard isolated DashboardServer fixture."""

    def setUp(self):
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

    def _topology_url(self, workflow: str | None = None) -> str:
        params = {"root": str(REPO_ROOT)}
        if workflow is not None:
            params["workflow"] = workflow
        query = urllib.parse.urlencode(params)
        return f"{self.base}/api/registry/topology?{query}"


class TopologyEndpointTests(DashboardTopologyTestCase):
    def test_topology_endpoint_returns_required_graphs_and_edges(self):
        status, payload = _get_json(self._topology_url())
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["workflow"], "roles")
        graphs = payload["graphs"]
        required = {"claude", "codex", "omp", "opencode", "omnigent", "pi"}
        self.assertTrue(required.issubset(graphs), graphs.keys())

        opencode = {
            (edge["type"], edge["src"], edge["dst"])
            for edge in graphs["opencode"]["edges"]
        }
        self.assertIn(("invokes", "trio", "trio-orchestrator"), opencode)
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-scout"),
            opencode)
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-orchestrator"),
            opencode)

        omp = {
            (edge["type"], edge["src"], edge["dst"])
            for edge in graphs["omp"]["edges"]
        }
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-scout"), omp)
        self.assertIn(("spawns", "trio-lead", "trio-builder"), omp)
        self.assertIn(("spawns", "trio-lead", "trio-scout"), omp)

    def test_topology_endpoint_returns_productionize_graphs(self):
        status, payload = _get_json(
            self._topology_url(workflow="productionize"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["workflow"], "productionize")
        self.assertEqual(
            set(payload["graphs"]),
            {"claude", "codex", "omp", "opencode", "kimi", "zcode"},
        )
        claude_edges = {
            (edge["type"], edge["src"], edge["dst"])
            for edge in payload["graphs"]["claude"]["edges"]
        }
        for target in ("trio-scout", "trio-lead", "trio-evaluator"):
            self.assertIn(
                ("subagent", "trio-productionize", target),
                claude_edges,
            )

    def test_topology_endpoint_rejects_unknown_workflow(self):
        status, payload = _get_json(self._topology_url(workflow="nope"))
        self.assertEqual(status, 400, payload)
        self.assertIn("workflow", payload["error"])

    def test_topology_endpoint_requires_root_query(self):
        status, payload = _get_json(
            f"{self.base}/api/registry/topology")
        self.assertEqual(status, 400, payload)


if __name__ == "__main__":
    unittest.main()

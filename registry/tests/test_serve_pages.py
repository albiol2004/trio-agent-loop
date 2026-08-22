#!/usr/bin/env python3
"""Tests for dashboard/serve.py's static page surface: the agent install
matrix page (agents.html/agents.js) plus a regression guard over the whole
STATIC_ROUTES table.

Loads dashboard/serve.py by path with importlib, mirroring
registry/tests/test_serve_registry.py.
Run: python3 -m unittest discover -s registry/tests -t . (from repo root)
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location("trio_dashboard_serve_pages", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get_raw(url: str) -> tuple[int, str, str]:
    """Returns (status, content-type header, decoded body) without assuming JSON."""
    try:
        with urllib.request.urlopen(url) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, resp.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, exc.headers.get("Content-Type", ""), body


class DashboardPagesTestCase(unittest.TestCase):
    """Spins up a real DashboardServer on an ephemeral port, static-routes only."""

    def setUp(self):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[REPO_ROOT], auto_discover=False)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, str, str]:
        return _get_raw(f"{self.base}{path}")


class AgentsPageTests(DashboardPagesTestCase):

    def test_agents_html_serves_200_with_expected_markers(self):
        status, content_type, body = self._get("/agents.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("topbar-meta", body)
        self.assertIn("/nav.js", body)
        self.assertIn("/agents.js", body)

    def test_agents_js_serves_200_as_javascript(self):
        status, content_type, _body = self._get("/agents.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)


class StaticRoutesTableTests(unittest.TestCase):

    def test_static_routes_contains_agents_entries(self):
        self.assertEqual(serve.STATIC_ROUTES.get("/agents.html"), ("agents.html", "text/html; charset=utf-8"))
        self.assertEqual(serve.STATIC_ROUTES.get("/agents.js"), ("agents.js", "text/javascript; charset=utf-8"))


class AppCssTests(unittest.TestCase):

    def test_app_css_defines_status_missing(self):
        css = (REPO_ROOT / "dashboard" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".status-missing", css)


class ExistingStaticRoutesRegressionTests(DashboardPagesTestCase):
    """Every static route present before this slice must keep working."""

    def test_all_pre_existing_static_routes_still_200(self):
        for path in ("/", "/app.css", "/app.js", "/skills.html", "/skills.js", "/nav.js"):
            with self.subTest(path=path):
                status, _content_type, _body = self._get(path)
                self.assertEqual(status, 200, path)


class ReciprocalLinkTests(unittest.TestCase):

    def test_index_links_to_agents_page(self):
        html = (REPO_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/agents.html"', html)

    def test_skills_links_to_agents_page(self):
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(encoding="utf-8")
        self.assertIn('href="/agents.html"', html)


class AgentsJsConventionTests(unittest.TestCase):
    """Sanity check on the no-innerHTML / no-inline-script convention."""

    def test_agents_js_has_no_innerhtml_or_script_tag(self):
        source = (REPO_ROOT / "dashboard" / "agents.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)


if __name__ == "__main__":
    unittest.main()

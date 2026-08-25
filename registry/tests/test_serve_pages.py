#!/usr/bin/env python3
"""Tests for dashboard/serve.py's static registry page surface plus a
regression guard over the whole STATIC_ROUTES table.

Loads dashboard/serve.py by path with importlib, mirroring
registry/tests/test_serve_registry.py.
Run: python3 -m unittest discover -s registry/tests -t . (from repo root)
"""
from __future__ import annotations

import importlib.util
import subprocess
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


class TopologyPageTests(DashboardPagesTestCase):

    def test_topology_html_serves_200_with_expected_markers(self):
        status, content_type, body = self._get("/topology.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        for marker in (
            "topbar-meta", "/nav.js", "/topology.js", "page-state",
            "topology-graph", "topology-svg", "compare-toggle",
            "compare-list",
            "harness-select",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_topology_js_serves_200_as_javascript(self):
        status, content_type, _body = self._get("/topology.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)


class ModelsPageTests(DashboardPagesTestCase):
    def test_models_html_serves_200_with_expected_markers(self):
        status, content_type, body = self._get("/models.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        for marker in (
            "topbar-meta", "/nav.js", "/models.js", "page-state",
            "models-table",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_models_js_serves_200_as_javascript(self):
        status, content_type, _body = self._get("/models.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)


class HealthPageTests(DashboardPagesTestCase):
    def test_health_html_serves_200_with_expected_markers(self):
        status, content_type, body = self._get("/health.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        for marker in (
            "topbar-meta", "/nav.js", "/health.js", "page-state",
            "health-lineage", "health-manifests", "health-installed",
            "health-generate", "health-dangling",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_health_js_serves_200_as_javascript(self):
        status, content_type, _body = self._get("/health.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)


class StaticRoutesTableTests(unittest.TestCase):

    def test_static_routes_contains_agents_entries(self):
        self.assertEqual(serve.STATIC_ROUTES.get("/agents.html"), ("agents.html", "text/html; charset=utf-8"))
        self.assertEqual(serve.STATIC_ROUTES.get("/agents.js"), ("agents.js", "text/javascript; charset=utf-8"))

    def test_static_routes_contains_topology_entries(self):
        self.assertEqual(
            serve.STATIC_ROUTES.get("/topology.html"),
            ("topology.html", "text/html; charset=utf-8"))
        self.assertEqual(
            serve.STATIC_ROUTES.get("/topology.js"),
            ("topology.js", "text/javascript; charset=utf-8"))

    def test_static_routes_contains_models_entries(self):
        self.assertEqual(
            serve.STATIC_ROUTES.get("/models.html"),
            ("models.html", "text/html; charset=utf-8"))
        self.assertEqual(
            serve.STATIC_ROUTES.get("/models.js"),
            ("models.js", "text/javascript; charset=utf-8"))

    def test_static_routes_contains_health_entries(self):
        self.assertEqual(
            serve.STATIC_ROUTES.get("/health.html"),
            ("health.html", "text/html; charset=utf-8"))
        self.assertEqual(
            serve.STATIC_ROUTES.get("/health.js"),
            ("health.js", "text/javascript; charset=utf-8"))


class AppCssTests(unittest.TestCase):

    def test_app_css_defines_status_missing(self):
        css = (REPO_ROOT / "dashboard" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".status-missing", css)


class ExistingStaticRoutesRegressionTests(DashboardPagesTestCase):
    """Every static route present before this slice must keep working."""

    def test_all_pre_existing_static_routes_still_200(self):
        for path in (
            "/", "/app.css", "/app.js", "/skills.html", "/skills.js",
            "/nav.js", "/topology.html", "/topology.js",
        ):
            with self.subTest(path=path):
                status, _content_type, _body = self._get(path)
                self.assertEqual(status, 200, path)


class ReciprocalLinkTests(unittest.TestCase):

    def test_registry_pages_link_to_health(self):
        for page in ("index", "skills", "agents", "topology", "models", "health"):
            html = (REPO_ROOT / "dashboard" / f"{page}.html").read_text(
                encoding="utf-8")
            with self.subTest(page=page):
                self.assertIn('href="/health.html"', html)

    def test_index_links_to_agents_page(self):
        html = (REPO_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/agents.html"', html)

    def test_index_links_to_topology_page(self):
        html = (REPO_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/topology.html"', html)

    def test_index_links_to_models_page(self):
        html = (REPO_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/models.html"', html)

    def test_skills_links_to_agents_page(self):
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(encoding="utf-8")
        self.assertIn('href="/agents.html"', html)

    def test_skills_links_to_topology_page(self):
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(encoding="utf-8")
        self.assertIn('href="/topology.html"', html)

    def test_skills_links_to_models_page(self):
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(encoding="utf-8")
        self.assertIn('href="/models.html"', html)

    def test_agents_links_to_topology_page(self):
        html = (REPO_ROOT / "dashboard" / "agents.html").read_text(encoding="utf-8")
        self.assertIn('href="/topology.html"', html)

    def test_agents_links_to_models_page(self):
        html = (REPO_ROOT / "dashboard" / "agents.html").read_text(encoding="utf-8")
        self.assertIn('href="/models.html"', html)

    def test_topology_links_to_registry_pages(self):
        html = (REPO_ROOT / "dashboard" / "topology.html").read_text(encoding="utf-8")
        self.assertIn('href="/skills.html"', html)
        self.assertIn('href="/agents.html"', html)

    def test_topology_links_to_models_page(self):
        html = (REPO_ROOT / "dashboard" / "topology.html").read_text(encoding="utf-8")
        self.assertIn('href="/models.html"', html)

    def test_models_links_to_registry_pages(self):
        html = (REPO_ROOT / "dashboard" / "models.html").read_text(encoding="utf-8")
        for page in ("skills", "agents", "topology"):
            with self.subTest(page=page):
                self.assertIn(f'href="/{page}.html"', html)


class AgentsJsConventionTests(unittest.TestCase):
    """Sanity check on the no-innerHTML / no-inline-script convention."""

    def test_agents_js_has_no_innerhtml_or_script_tag(self):
        source = (REPO_ROOT / "dashboard" / "agents.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)


class TopologyJsConventionTests(unittest.TestCase):
    """Keep topology rendering on safe DOM and SVG construction APIs."""

    def test_topology_js_has_no_innerhtml_or_script_tag(self):
        source = (REPO_ROOT / "dashboard" / "topology.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)

    def test_skills_js_honors_path_query_from_topology(self):
        source = (REPO_ROOT / "dashboard" / "skills.js").read_text(encoding="utf-8")
        self.assertIn("URLSearchParams(location.search)", source)


class SkillsJsWidgetTests(unittest.TestCase):
    """Keep structured catalog widgets in the safe plain-DOM frontend."""

    def test_skills_js_contains_structured_widget_paths(self):
        source = (REPO_ROOT / "dashboard" / "skills.js").read_text(
            encoding="utf-8")
        for marker in (
            "permission-grid",
            "spawns-select",
            "json-schema",
            "custom…",
            "/api/registry/models",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(
            encoding="utf-8")
        self.assertIn(".field-offlist", html)
        self.assertIn(".field-error", html)

    def test_skills_managed_source_controls_exist(self):
        html = (REPO_ROOT / "dashboard" / "skills.html").read_text(
            encoding="utf-8")
        source = (REPO_ROOT / "dashboard" / "skills.js").read_text(
            encoding="utf-8")
        self.assertIn('id="managed-source"', html)
        self.assertIn('id="regen-file"', html)
        self.assertIn("/api/registry/regenerate", source)
        self.assertNotIn("innerHTML", source)

    def test_skills_js_helpers_exercise_save_guard_catalog_and_root(self):
        script = r"""
const assert = require("node:assert/strict");
const {
  catalogChoiceState,
  destinationSurfaces,
  registryDestinationPath,
  registryDestinationPreview,
  validateJsonSchemaField,
  withRoot,
} = require(process.argv[1]);

assert.equal(
  withRoot("/api/registry/models", "/tmp/project"),
  "/api/registry/models?root=%2Ftmp%2Fproject"
);
assert.equal(
  withRoot("/api/registry/models?scope=all", "/tmp/project"),
  "/api/registry/models?scope=all&root=%2Ftmp%2Fproject"
);

const invalidOutput = validateJsonSchemaField('{"type": invalid}', true);
assert.equal(invalidOutput.valid, false);
assert.equal(invalidOutput.fieldError, true);
assert.equal(invalidOutput.blocksSave, true);
assert.match(invalidOutput.error, /Invalid JSON/);

const validOutput = validateJsonSchemaField('{"type": "object"}', true);
assert.equal(validOutput.valid, true);
assert.equal(validOutput.fieldError, false);
assert.equal(validOutput.blocksSave, false);

const offList = catalogChoiceState("custom-model", ["known-model"]);
assert.equal(offList.value, "custom-model");
assert.equal(offList.selected, "__trio_custom__");
assert.equal(offList.offList, true);

const known = catalogChoiceState("known-model", ["known-model"]);
assert.equal(known.selected, "known-model");
assert.equal(known.offList, false);

assert.equal(
  registryDestinationPath(
    "project", "claude", "skill", "x", "/tmp/ws", {}),
  "/tmp/ws/.claude/skills/x/SKILL.md"
);
assert.equal(
  registryDestinationPath(
    "global", "codex", "agent", "x", "", {"codex:agent": "toml"}),
  "~/.codex/agents/x.toml"
);
assert.equal(
  registryDestinationPath(
    "global", "omnigent", "agent", "x", "",
    {"omnigent:agent": "yaml-document"}),
  "~/.omnigent/agents/x/config.yaml"
);
assert.equal(
  registryDestinationPath("project", "omp", "skill", "x", "/tmp/ws"),
  null
);

const catalog = {
  codex: { skill: { project: null, global: ".agents/skills" } },
  cursor: { skill: { project: ".cursor/skills", global: null } },
};
assert.deepEqual(destinationSurfaces(catalog, "codex"), ["skill"]);
assert.equal(
  registryDestinationPreview(
    "project", "codex", "skill", "x", "/tmp/ws", {}, catalog),
  "codex:skill · global only → ~/.agents/skills/x/SKILL.md"
);
assert.equal(
  registryDestinationPreview(
    "project", "cursor", "skill", "x", "/tmp/ws", {}, catalog),
  "cursor:skill · /tmp/ws/.cursor/skills/x/SKILL.md"
);
"""
        result = subprocess.run(
            ["node", "-e", script, str(REPO_ROOT / "dashboard" / "skills.js")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ModelsJsConventionTests(unittest.TestCase):
    def test_models_js_has_no_innerhtml_or_script_tag(self):
        source = (REPO_ROOT / "dashboard" / "models.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)

    def test_models_js_links_overrides_to_skills(self):
        source = (REPO_ROOT / "dashboard" / "models.js").read_text(
            encoding="utf-8")
        self.assertIn("skills.html?path=", source)


class HealthJsConventionTests(unittest.TestCase):
    def test_health_js_has_no_innerhtml_or_script_tag(self):
        source = (REPO_ROOT / "dashboard" / "health.js").read_text(
            encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("<script", source)


if __name__ == "__main__":
    unittest.main()

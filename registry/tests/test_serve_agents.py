#!/usr/bin/env python3
"""Tests for dashboard/serve.py's canonical-agent CRUD + install surface.

Loads dashboard/serve.py by path with importlib, mirroring
test_serve_registry.py. Run: python3 -m unittest discover -s registry/tests -t .
(from repo root).

Isolation rule: no test here may write into the real $HOME or into the real
registry/canonical-agents/ directory. CRUD tests use throwaway agent names
(prefixed __trio_agent_test__) cleaned up in tearDown; install tests
monkeypatch serve._GLOBAL_REGISTRY_DIRS / serve._WRITABLE_ROOTS to point at a
tempdir for the duration of the test.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"
CANONICAL_AGENTS_DIR = REPO_ROOT / "registry" / "canonical-agents"
SEED_NAMES = {"registry-scout", "registry-editor"}
# CanonicalAgent's frozen NAME_RE (registry/agents.py) requires the name to
# *start* with [a-z0-9], so a literal "__trio_agent_test__" prefix would
# itself fail validation. "z_trio_agent_test_" keeps the same recognizable
# throwaway marker while satisfying that constraint.
THROWAWAY_PREFIX = "z_trio_agent_test_"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location("trio_dashboard_serve_agents", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()
registry = serve.load_registry_module()
agents = serve.load_agents_module()


def _http_json(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class DashboardServerTestCase(unittest.TestCase):
    """Base case: spins up a real DashboardServer on an ephemeral port."""

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

    def _get(self, path: str) -> tuple[int, dict]:
        return _http_json("GET", f"{self.base}{path}")

    def _assert_canonical_agents_dir_only_has_seeds(self):
        names = {
            p.stem for p in CANONICAL_AGENTS_DIR.glob("*.md") if p.is_file()
        }
        self.assertEqual(names, SEED_NAMES, names)


class AgentsListEndpointTests(DashboardServerTestCase):

    def test_lists_both_seed_agents(self):
        status, payload = self._get("/api/registry/agents")
        self.assertEqual(status, 200, payload)
        names = {a["name"] for a in payload["agents"]}
        self.assertTrue(SEED_NAMES.issubset(names), names)

    def test_response_shape(self):
        status, payload = self._get("/api/registry/agents")
        self.assertEqual(status, 200, payload)
        for key in ("agents", "harnesses", "support", "model_tiers", "tool_policies"):
            self.assertIn(key, payload)
        self.assertEqual(set(payload["harnesses"]), {"claude", "codex", "omp", "opencode"})
        for agent in payload["agents"]:
            for key in ("name", "description", "model_tier", "tool_policy", "path"):
                self.assertIn(key, agent)

    def test_omnigent_is_unsupported_with_a_reason(self):
        status, payload = self._get("/api/registry/agents")
        self.assertEqual(status, 200, payload)
        omnigent = payload["support"]["omnigent"]
        self.assertFalse(omnigent["supported"])
        self.assertTrue(omnigent["reason"])

    def test_support_covers_every_harness_support_key(self):
        status, payload = self._get("/api/registry/agents")
        self.assertEqual(status, 200, payload)
        self.assertEqual(set(payload["support"].keys()), set(agents.HARNESS_SUPPORT.keys()))


class AgentMatrixInRegistryIndexTests(DashboardServerTestCase):

    def test_registry_index_has_agent_matrix_row_per_canonical_agent(self):
        status, payload = self._get("/api/registry")
        self.assertEqual(status, 200, payload)
        self.assertIn("agent_matrix", payload)
        self.assertIsInstance(payload["agent_matrix"], list)
        self.assertEqual(len(payload["agent_matrix"]), len(agents.list_agents()))


class AgentCrudTests(DashboardServerTestCase):

    def setUp(self):
        super().setUp()
        self._created_names: list[str] = []

    def tearDown(self):
        for name in self._created_names:
            agents.delete_agent(name)
        self._assert_canonical_agents_dir_only_has_seeds()
        super().tearDown()

    def _post_agent(self, **overrides) -> tuple[int, dict]:
        body = {
            "name": THROWAWAY_PREFIX + "crud",
            "description": "a throwaway test agent",
            "model_tier": "cheap",
            "tool_policy": "read-only",
            "instructions": "Do the throwaway thing.\n",
        }
        body.update(overrides)
        status, payload = _http_json("POST", f"{self.base}/api/registry/agents", body)
        if status == 201:
            self._created_names.append(body["name"])
        return status, payload

    def test_full_crud_round_trip(self):
        name = THROWAWAY_PREFIX + "roundtrip"

        # POST create
        status, payload = self._post_agent(name=name)
        self.assertEqual(status, 201, payload)
        self.assertTrue(Path(payload["path"]).is_file())

        # GET list contains it
        status, listing = self._get("/api/registry/agents")
        self.assertEqual(status, 200, listing)
        self.assertIn(name, {a["name"] for a in listing["agents"]})

        # GET file returns the same fields
        status, detail = self._get(f"/api/registry/agents/file?name={name}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["name"], name)
        self.assertEqual(detail["description"], "a throwaway test agent")
        self.assertEqual(detail["model_tier"], "cheap")
        self.assertEqual(detail["tool_policy"], "read-only")
        self.assertEqual(detail["instructions"], "Do the throwaway thing.\n")

        # PUT updates the description
        status, put_payload = _http_json(
            "PUT", f"{self.base}/api/registry/agents/file",
            {
                "name": name,
                "description": "an updated description",
                "model_tier": "cheap",
                "tool_policy": "read-only",
                "instructions": "Do the throwaway thing.\n",
            })
        self.assertEqual(status, 200, put_payload)

        # GET file shows the update
        status, detail2 = self._get(f"/api/registry/agents/file?name={name}")
        self.assertEqual(status, 200, detail2)
        self.assertEqual(detail2["description"], "an updated description")

        # DELETE
        status, del_payload = _http_json(
            "DELETE", f"{self.base}/api/registry/agents/file?name={name}")
        self.assertEqual(status, 200, del_payload)
        self.assertFalse(Path(del_payload["path"]).exists())

        # GET file 404
        status, gone = self._get(f"/api/registry/agents/file?name={name}")
        self.assertEqual(status, 404, gone)

    def test_post_unknown_model_tier_is_400_naming_model_tier(self):
        status, payload = self._post_agent(
            name=THROWAWAY_PREFIX + "badtier", model_tier="ultra-mega")
        self.assertEqual(status, 400, payload)
        self.assertIn("model_tier", payload["error"])

    def test_post_duplicate_is_409(self):
        name = THROWAWAY_PREFIX + "dup"
        status, payload = self._post_agent(name=name)
        self.assertEqual(status, 201, payload)
        status, payload = self._post_agent(name=name)
        self.assertEqual(status, 409, payload)
        self.assertIn("error", payload)

    def test_put_nonexistent_agent_is_404(self):
        status, payload = _http_json(
            "PUT", f"{self.base}/api/registry/agents/file",
            {
                "name": THROWAWAY_PREFIX + "does-not-exist",
                "description": "d",
                "model_tier": "cheap",
                "tool_policy": "read-only",
                "instructions": "x\n",
            })
        self.assertEqual(status, 404, payload)


class InstallEndpointTests(DashboardServerTestCase):
    """Install into an isolated tempdir standing in for harness home dirs."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name)
        self._orig_global_dirs = serve._GLOBAL_REGISTRY_DIRS
        self._orig_writable_roots = serve._WRITABLE_ROOTS
        fake_dirs = {
            ("claude", "agent"): tmp_root / "claude-agents",
            ("codex", "agent"): tmp_root / "codex-agents",
            ("omp", "agent"): tmp_root / "omp-agents",
            ("opencode", "agent"): tmp_root / "opencode-agents",
        }
        serve._GLOBAL_REGISTRY_DIRS = fake_dirs
        serve._WRITABLE_ROOTS = (tmp_root,)
        self._tmp_root = tmp_root

    def tearDown(self):
        serve._GLOBAL_REGISTRY_DIRS = self._orig_global_dirs
        serve._WRITABLE_ROOTS = self._orig_writable_roots
        self._tmp.cleanup()
        super().tearDown()

    def _install(self, agent="registry-scout", harness="claude") -> tuple[int, dict]:
        return _http_json(
            "POST", f"{self.base}/api/registry/install",
            {"agent": agent, "harness": harness})

    def test_install_claude_written_to_temp_dir_not_home(self):
        status, payload = self._install(harness="claude")
        self.assertEqual(status, 201, payload)
        path = Path(payload["path"])
        self.assertTrue(str(path).startswith(str(self._tmp_root)), path)
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).endswith(".md"))
        fields, body = registry.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["name"], "registry-scout")
        self.assertNotIn(str(serve.HOME), str(path))

    def test_install_codex_toml_developer_instructions_exact(self):
        agent = agents.load_agent("registry-scout")
        status, payload = self._install(harness="codex")
        self.assertEqual(status, 201, payload)
        path = Path(payload["path"])
        self.assertTrue(str(path).endswith(".toml"), path)
        fields = registry.parse_toml(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["name"], "registry-scout")
        self.assertEqual(fields["developer_instructions"], agent.instructions)

    def test_install_omp_md_has_name(self):
        status, payload = self._install(harness="omp")
        self.assertEqual(status, 201, payload)
        path = Path(payload["path"])
        self.assertTrue(str(path).endswith(".md"), path)
        fields, _ = registry.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["name"], "registry-scout")

    def test_install_opencode_md_has_no_name_and_permission_dict(self):
        status, payload = self._install(harness="opencode")
        self.assertEqual(status, 201, payload)
        path = Path(payload["path"])
        self.assertTrue(str(path).endswith(".md"), path)
        fields, _ = registry.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertNotIn("name", fields)
        self.assertIsInstance(fields["permission"], dict)

    def test_install_twice_second_call_is_200_created_false_same_content(self):
        status1, payload1 = self._install(harness="claude")
        self.assertEqual(status1, 201, payload1)
        path = Path(payload1["path"])
        content1 = path.read_text(encoding="utf-8")

        status2, payload2 = self._install(harness="claude")
        self.assertEqual(status2, 200, payload2)
        self.assertFalse(payload2["created"])
        content2 = path.read_text(encoding="utf-8")
        self.assertEqual(content1, content2)

    def test_install_omnigent_is_400_with_reason_and_writes_nothing(self):
        before = set()
        for d in serve._GLOBAL_REGISTRY_DIRS.values():
            if d.is_dir():
                before |= set(d.rglob("*"))
        status, payload = self._install(harness="omnigent")
        self.assertEqual(status, 400, payload)
        self.assertFalse(payload["supported"])
        self.assertTrue(payload["reason"])
        after = set()
        for d in serve._GLOBAL_REGISTRY_DIRS.values():
            if d.is_dir():
                after |= set(d.rglob("*"))
        self.assertEqual(before, after)

    def test_install_unknown_harness_is_400_never_500(self):
        status, payload = self._install(harness="totally-unknown-harness")
        self.assertEqual(status, 400, payload)
        self.assertIn("error", payload)

    def test_install_nonexistent_agent_is_404_never_500(self):
        status, payload = self._install(agent="__no-such-canonical-agent__")
        self.assertEqual(status, 404, payload)
        self.assertIn("error", payload)


class ManagedFileRefusalTests(DashboardServerTestCase):
    """PUT/DELETE against a real generate.py-owned file must be refused."""

    TARGET = REPO_ROOT / ".claude" / "agents" / "trio-lead.md"

    def test_put_managed_file_is_403_and_file_untouched(self):
        original = self.TARGET.read_bytes()
        status, payload = _http_json(
            "PUT", f"{self.base}/api/registry/file",
            {"path": str(self.TARGET), "content": "corrupted by test\n"})
        self.assertEqual(status, 403, payload)
        self.assertIn("generate.py", payload["error"])
        self.assertEqual(self.TARGET.read_bytes(), original)

    def test_delete_managed_file_is_403_and_file_untouched(self):
        original = self.TARGET.read_bytes()
        status, payload = _http_json(
            "DELETE", f"{self.base}/api/registry/file?path={self.TARGET}")
        self.assertEqual(status, 403, payload)
        self.assertIn("generate.py", payload["error"])
        self.assertEqual(self.TARGET.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for dashboard/serve.py's format-aware registry surface (api:ServeAPI).

Loads dashboard/serve.py by path with importlib, mirroring how serve.py
itself loads registry/scan.py and metrics/trio-metrics.py at runtime.
Run: python3 -m unittest discover -s registry/tests -t . (from repo root)
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import shutil
import sys
import tomllib
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location("trio_dashboard_serve", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()
registry = serve.load_registry_module()


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


class RegistryTargetTests(unittest.TestCase):
    """Unit tests for pure functions — no server, no I/O."""

    def test_registry_target_codex_agent_is_toml(self):
        target = serve._registry_target("codex", "agent", "x")
        self.assertTrue(str(target).endswith(".toml"), target)

    def test_registry_target_claude_agent_is_md(self):
        target = serve._registry_target("claude", "agent", "x")
        self.assertTrue(str(target).endswith(".md"), target)

    def test_registry_target_claude_skill_is_skill_md(self):
        target = serve._registry_target("claude", "skill", "x")
        self.assertTrue(str(target).endswith("x/SKILL.md".replace("/", "/")), target)
        self.assertEqual(target.name, "SKILL.md")
        self.assertEqual(target.parent.name, "x")

    def test_update_registry_name_codex_toml_changes_only_name_line(self):
        source = (REPO_ROOT / "codex" / "agents" / "trio-evaluator.toml").read_text(
            encoding="utf-8")
        result = serve._update_registry_name(source, "trio-evaluator-renamed", "toml")
        self.assertNotEqual(result, source)
        src_lines = source.splitlines(keepends=True)
        res_lines = result.splitlines(keepends=True)
        self.assertEqual(len(src_lines), len(res_lines))
        diff_indices = [
            i for i, (a, b) in enumerate(zip(src_lines, res_lines)) if a != b
        ]
        self.assertEqual(len(diff_indices), 1, diff_indices)
        self.assertTrue(src_lines[diff_indices[0]].lstrip().startswith("name"))
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["name"], "trio-evaluator-renamed")

    def test_update_registry_name_yaml_skill_changes_only_name_line(self):
        source = (
            REPO_ROOT / ".claude" / "skills" / "trio-init" / "SKILL.md"
        ).read_text(encoding="utf-8")
        result = serve._update_registry_name(source, "trio-init-renamed", "yaml")
        self.assertNotEqual(result, source)
        src_lines = source.splitlines(keepends=True)
        res_lines = result.splitlines(keepends=True)
        self.assertEqual(len(src_lines), len(res_lines))
        diff_indices = [
            i for i, (a, b) in enumerate(zip(src_lines, res_lines)) if a != b
        ]
        self.assertEqual(len(diff_indices), 1, diff_indices)
        self.assertIn("name:", src_lines[diff_indices[0]])
        new_fields, _ = registry.parse_frontmatter(result)
        self.assertEqual(new_fields["name"], "trio-init-renamed")

    def test_update_registry_name_inserts_missing_name(self):
        source = "---\ndescription: no name here\n---\n\nbody text\n"
        result = serve._update_registry_name(source, "inserted-name", "yaml")
        fields, body = registry.parse_frontmatter(result)
        self.assertEqual(fields.get("name"), "inserted-name")
        self.assertEqual(fields.get("description"), "no name here")
        self.assertIn("body text", body)


class DashboardServerTestCase(unittest.TestCase):
    """Base case: spins up a real DashboardServer on an ephemeral port."""

    def setUp(self):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[REPO_ROOT], auto_discover=False)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._cleanup_paths: list[Path] = []

    def tearDown(self):
        for path in self._cleanup_paths:
            try:
                path.unlink()
            except OSError:
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, dict]:
        return _http_json("GET", f"{self.base}{path}")


class SchemaEndpointTests(DashboardServerTestCase):

    def test_schema_has_all_sections(self):
        status, payload = self._get("/api/registry/schema")
        self.assertEqual(status, 200)
        for key in ("destinations", "formats", "keys"):
            self.assertIn(key, payload)

    def test_schema_formats_codex_agent_is_toml(self):
        _, payload = self._get("/api/registry/schema")
        self.assertEqual(payload["formats"].get("codex:agent"), "toml")

    def test_schema_destinations_match_global_registry_dirs(self):
        _, payload = self._get("/api/registry/schema")
        expected_harnesses = {h for (h, _s) in serve._GLOBAL_REGISTRY_DIRS}
        expected_pairs = set(serve._GLOBAL_REGISTRY_DIRS.keys())
        self.assertEqual(set(payload["destinations"].keys()), expected_harnesses)
        actual_pairs = {
            (harness, surface)
            for harness, surfaces in payload["destinations"].items()
            for surface in surfaces
        }
        self.assertEqual(actual_pairs, expected_pairs)

    def test_schema_opencode_agent_keys_have_no_name_field(self):
        _, payload = self._get("/api/registry/schema")
        keys = payload["keys"].get("opencode:agent", [])
        self.assertNotIn("name", [spec.get("key") for spec in keys])


class RegistryFileEndpointTests(DashboardServerTestCase):

    def test_opencode_agent_is_yaml_with_nested_permission(self):
        path = REPO_ROOT / "opencode" / "agents" / "trio-evaluator.md"
        status, payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["format"], "yaml")
        permission = payload["frontmatter"]["permission"]
        self.assertIsInstance(permission, dict)
        self.assertIn("*", permission)
        bash = permission["bash"]
        self.assertIsInstance(bash, dict)
        self.assertTrue(
            any(" " in key for key in bash.keys()),
            f"expected a bash key containing a space, got {list(bash.keys())}")

    def test_codex_agent_is_toml_with_developer_instructions_body(self):
        path = REPO_ROOT / "codex" / "agents" / "trio-evaluator.toml"
        status, payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["format"], "toml")
        self.assertTrue(payload["body"].strip())
        self.assertIn("\n", payload["body"].strip())
        self.assertNotIn("developer_instructions", payload["frontmatter"])

    def test_text_format_for_frontmatter_less_file(self):
        # ~/.claude/CLAUDE.md is scanned as scope="global" harness="claude"
        # surface="instructions" (registry.scan_instructions) with no ---
        # fence, so it exercises the real format == "text" branch of
        # GET /api/registry/file.
        claude_md = serve.HOME / ".claude" / "CLAUDE.md"
        if not claude_md.is_file():
            self.skipTest("~/.claude/CLAUDE.md not present on this machine")
        status, payload = self._get(f"/api/registry/file?path={claude_md}")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["format"], "text")
        self.assertEqual(payload["frontmatter"], {})
        self.assertEqual(payload["body"], claude_md.read_text(encoding="utf-8"))


class RoundTripTests(DashboardServerTestCase):

    def test_opencode_agent_http_round_trip(self):
        path = REPO_ROOT / "opencode" / "agents" / "trio-evaluator.md"
        status, get_payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        status, ser_payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": get_payload["format"],
                "frontmatter": get_payload["frontmatter"],
                "body": get_payload["body"],
            })
        self.assertEqual(status, 200, ser_payload)
        content = ser_payload["content"]
        self.assertNotIn("[object Object]", content)
        for line in content.splitlines():
            if line.strip() == "permission:":
                continue
        # No "permission:" line followed directly by a non-indented line
        # (which would indicate a flattened/corrupted nested map).
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.rstrip() == "permission:":
                self.assertTrue(i + 1 < len(lines), "permission: was the last line")
                nxt = lines[i + 1]
                self.assertTrue(
                    nxt.startswith(" ") or nxt.startswith("\t"),
                    f"permission: not followed by an indented line: {nxt!r}")
        refields, rebody = registry.parse_frontmatter(content)
        self.assertEqual(refields, get_payload["frontmatter"])
        self.assertEqual(rebody, get_payload["body"])

    def test_codex_agent_http_round_trip(self):
        path = REPO_ROOT / "codex" / "agents" / "trio-evaluator.toml"
        status, get_payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        status, ser_payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": get_payload["format"],
                "frontmatter": get_payload["frontmatter"],
                "body": get_payload["body"],
            })
        self.assertEqual(status, 200, ser_payload)
        content = ser_payload["content"]
        parsed = tomllib.loads(content)
        developer_instructions = parsed.pop("developer_instructions")
        expected = dict(get_payload["frontmatter"])
        self.assertEqual(parsed, expected)
        self.assertEqual(developer_instructions, get_payload["body"])


class SerializeValidationTests(DashboardServerTestCase):

    def test_text_format_serializes_body_verbatim(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {"format": "text", "frontmatter": {}, "body": "whole file body\n"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "whole file body\n")
        self.assertEqual(payload["warnings"], [])

    def test_yaml_wrapper_value_is_parsed(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {
                    "permission": {"$yaml": 'permission:\n  "*": deny\n  read: allow\n'},
                },
                "body": "body\n",
            })
        self.assertEqual(status, 200, payload)
        refields, _ = registry.parse_frontmatter(payload["content"])
        self.assertEqual(refields["permission"], {"*": "deny", "read": "allow"})

    def test_malformed_yaml_wrapper_returns_400_naming_key(self):
        # The hand-rolled YAML-subset parser is deliberately lenient (never
        # raises on ordinary bad syntax so a single misformatted harness
        # file can't kill a scan); the one input shape that reliably raises
        # is unbounded nested flow-collection recursion (RecursionError).
        pathological = "x: " + "[" * 4000
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"permission": {"$yaml": pathological}},
                "body": "body\n",
            })
        self.assertEqual(status, 400, payload)
        self.assertIn("permission", payload["error"])

    def test_missing_required_key_is_400(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"description": "no name field"},
                "body": "body\n",
                "harness": "claude",
                "surface": "agent",
            })
        self.assertEqual(status, 400, payload)

    def test_unknown_key_is_warning_not_error(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {
                    "name": "x", "description": "d", "totally_unknown_key": "y",
                },
                "body": "body\n",
                "harness": "claude",
                "surface": "agent",
            })
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            any("totally_unknown_key" in w for w in payload["warnings"]),
            payload["warnings"])

    def test_name_mismatch_is_warning(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"name": "wrong-name", "description": "d"},
                "body": "body\n",
                "harness": "claude",
                "surface": "agent",
                "path": str(REPO_ROOT / ".claude" / "agents" / "actual-name.md"),
            })
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            any("wrong-name" in w for w in payload["warnings"]), payload["warnings"])

    def test_omnigent_harness_suppresses_name_warning(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"name": "trio-omnigent-lead", "description": "d"},
                "body": "body\n",
                "harness": "omnigent",
                "surface": "agent",
                "path": str(REPO_ROOT / "omnigent" / "entrypoints" / "lead" / "config.yaml"),
            })
        self.assertEqual(status, 200, payload)
        self.assertFalse(
            any("does not match" in w for w in payload["warnings"]), payload["warnings"])


class CreateAndDeleteTests(DashboardServerTestCase):
    """Acceptance A5: codex agent create writes valid TOML; cleaned up after."""

    THROWAWAY = "__trio_serve_test__"

    def setUp(self):
        super().setUp()
        self._codex_path = (
            serve.HOME / ".codex" / "agents" / f"{self.THROWAWAY}.toml")

    def tearDown(self):
        try:
            self._codex_path.unlink()
        except OSError:
            pass
        super().tearDown()

    def test_create_codex_agent_writes_valid_toml(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/create",
            {"harness": "codex", "surface": "agent", "name": self.THROWAWAY,
             "content": ""})
        self.assertEqual(status, 201, payload)
        created = Path(payload["path"])
        self.assertTrue(str(created).endswith(".toml"), created)
        data = tomllib.loads(created.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], self.THROWAWAY)

        status, payload = _http_json(
            "DELETE", f"{self.base}/api/registry/file?path={created}")
        self.assertEqual(status, 200, payload)
        self.assertFalse(created.exists())


class RegressionTests(DashboardServerTestCase):
    """Existing flows (GET, PUT, import) still work for flat-frontmatter files."""

    def test_registry_index_returns_entries(self):
        status, payload = self._get("/api/registry")
        self.assertEqual(status, 200)
        self.assertIn("entries", payload)
        self.assertGreater(len(payload["entries"]), 0)

    def test_put_round_trip_inside_claude_skills(self):
        tmp_dir = REPO_ROOT / ".claude" / "skills" / "__trio_serve_test_put__"
        tmp_dir.mkdir(exist_ok=True)
        tmp_path = tmp_dir / "SKILL.md"
        original = (
            "---\nname: __trio_serve_test_put__\ndescription: tmp\n---\n\nbody\n")
        try:
            status, payload = _http_json(
                "PUT", f"{self.base}/api/registry/file",
                {"path": str(tmp_path), "content": original})
            self.assertEqual(status, 200, payload)
            self.assertEqual(tmp_path.read_text(encoding="utf-8"), original)

            status, get_payload = self._get(f"/api/registry/file?path={tmp_path}")
            self.assertEqual(status, 200)
            self.assertEqual(get_payload["format"], "yaml")
            self.assertEqual(get_payload["frontmatter"]["name"],
                              "__trio_serve_test_put__")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_import_claude_skill_to_throwaway_name(self):
        source = REPO_ROOT / ".claude" / "skills" / "trio-init" / "SKILL.md"
        throwaway = "__trio_serve_test_import__"
        target_dir = serve.HOME / ".claude" / "skills" / throwaway
        try:
            status, payload = _http_json(
                "POST", f"{self.base}/api/registry/import",
                {
                    "mode": "copy",
                    "from_path": str(source),
                    "to_harness": "claude",
                    "to_surface": "skill",
                    "name": throwaway,
                })
            self.assertEqual(status, 201, payload)
            copied = Path(payload["path"])
            fields, _ = registry.parse_frontmatter(
                copied.read_text(encoding="utf-8"))
            self.assertEqual(fields["name"], throwaway)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

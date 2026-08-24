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
import subprocess
import sys
import tempfile
import tomllib
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

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

    def test_registry_target_project_claude_skill_is_under_project_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = serve._registry_target(
                "claude", "skill", "x", scope="project", project=Path(tmp))
            expected = Path(tmp) / ".claude" / "skills" / "x" / "SKILL.md"
            self.assertEqual(target, expected.resolve())

    def test_registry_target_project_cursor_skill_is_under_project_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = serve._registry_target(
                "cursor", "skill", "x", scope="project", project=Path(tmp))
            expected = Path(tmp) / ".cursor" / "skills" / "x" / "SKILL.md"
            self.assertEqual(target, expected.resolve())

    def test_registry_target_project_claude_command_is_under_project_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = serve._registry_target(
                "claude", "command", "x", scope="project", project=Path(tmp))
            expected = Path(tmp) / ".claude" / "commands" / "x.md"
            self.assertEqual(target, expected.resolve())

    def test_registry_target_project_claude_agent_is_under_project_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = serve._registry_target(
                "claude", "agent", "x", scope="project", project=Path(tmp))
            expected = Path(tmp) / ".claude" / "agents" / "x.md"
            self.assertEqual(target, expected.resolve())

    def test_registry_target_project_opencode_agent_is_under_project_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = serve._registry_target(
                "opencode", "agent", "x", scope="project", project=Path(tmp))
            expected = Path(tmp) / ".opencode" / "agents" / "x.md"
            self.assertEqual(target, expected.resolve())

    def test_registry_target_rejects_unsupported_project_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                serve._registry_target(
                    "omp", "skill", "x", scope="project", project=Path(tmp))

    def test_agent_registry_target_falls_back_when_project_agent_dir_missing(self):
        original_home = serve.HOME
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            serve.HOME = tmp_path
            try:
                project = tmp_path / "project"
                project.mkdir()
                target, scope_used = serve._agent_registry_target(
                    "codex", "x", scope="project", project=project)
                self.assertEqual(scope_used, "global")
                expected = tmp_path / ".codex" / "agents" / "x.toml"
                self.assertEqual(target, expected.resolve())

                target, scope_used = serve._agent_registry_target(
                    "claude", "x", scope="project", project=project)
                self.assertEqual(scope_used, "project")
                expected = project / ".claude" / "agents" / "x.md"
                self.assertEqual(target, expected.resolve())
            finally:
                serve.HOME = original_home

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


class ScopedRegistryCreateTests(unittest.TestCase):
    """Create scoped files without touching the real home directory."""

    NAME = "throwaway"

    def test_create_skill_uses_project_and_fake_global_destinations(self):
        real_target = (
            Path.home() / ".claude" / "skills" / self.NAME / "SKILL.md")
        real_before = (
            real_target.read_bytes() if real_target.exists() else None)
        original_home = serve.HOME
        server = None
        thread = None
        with tempfile.TemporaryDirectory() as project_tmp, \
                tempfile.TemporaryDirectory() as home_tmp:
            project = Path(project_tmp).resolve()
            fake_home = Path(home_tmp).resolve()
            try:
                serve.HOME = fake_home
                server = serve.DashboardServer(
                    ("127.0.0.1", 0),
                    workspaces=[project],
                    auto_discover=False)
                base = f"http://127.0.0.1:{server.server_address[1]}"
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                thread.start()

                status, payload = _http_json(
                    "POST",
                    f"{base}/api/registry/create",
                    {
                        "harness": "claude",
                        "surface": "skill",
                        "name": self.NAME,
                        "content": "",
                        "scope": "project",
                        "project": str(project),
                    })
                self.assertEqual(status, 201, payload)
                project_target = (
                    project / ".claude" / "skills" / self.NAME / "SKILL.md")
                self.assertEqual(Path(payload["path"]), project_target)
                self.assertTrue(project_target.is_file())

                status, payload = _http_json(
                    "POST",
                    f"{base}/api/registry/create",
                    {
                        "harness": "claude",
                        "surface": "skill",
                        "name": self.NAME,
                        "content": "",
                        "scope": "global",
                    })
                self.assertEqual(status, 201, payload)
                global_target = (
                    fake_home / ".claude" / "skills" / self.NAME / "SKILL.md")
                self.assertEqual(Path(payload["path"]), global_target)
                self.assertTrue(global_target.is_file())
            finally:
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                serve.HOME = original_home

        self.assertEqual(
            real_target.read_bytes() if real_target.exists() else None,
            real_before)


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

    def test_schema_exposes_sources_and_specialized_widgets(self):
        _, payload = self._get("/api/registry/schema")
        keys = payload["keys"]

        def spec_for(surface, field):
            return next(
                spec for spec in keys[surface] if spec["key"] == field)

        self.assertEqual(
            spec_for("claude:agent", "model")["values_from"],
            "models:claude")
        self.assertEqual(
            spec_for("codex:agent", "model")["values_from"],
            "models:codex")
        self.assertEqual(
            spec_for("omp:agent", "model")["values_from"],
            "models:omp")
        self.assertEqual(
            spec_for("opencode:agent", "permission")["widget"],
            "permission-grid")
        self.assertEqual(
            spec_for("omp:agent", "spawns")["widget"],
            "spawns-select")
        self.assertEqual(
            spec_for("omp:agent", "output")["widget"],
            "json-schema")

    def test_schema_exposes_omnigent_agent_document_fields(self):
        status, payload = self._get("/api/registry/schema")
        self.assertEqual(status, 200, payload)
        expected = [
            "name",
            "description",
            "spawn",
            "executor.model",
            "executor.config.harness",
            "executor.config.yolo",
        ]
        actual = [
            spec["key"] for spec in payload["keys"]["omnigent:agent"]
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(
            payload["formats"]["omnigent:agent"], "yaml-document")


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

    def test_generated_claude_agent_includes_source_metadata(self):
        path = REPO_ROOT / ".claude" / "agents" / "trio-lead.md"
        status, payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            payload["source"]["prompt"].endswith(
                "prompts/canonical/lead.md"))
        self.assertIn("overlays/.claude", payload["source"]["overlay"])

    def test_prompt_source_file_is_readable_without_registry_entry(self):
        path = REPO_ROOT / "prompts" / "canonical" / "lead.md"
        status, payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["format"], "text")
        self.assertIn("{{lead.", payload["body"])
        self.assertIsNone(payload["source"])

    def test_prompt_source_file_is_read_only_for_put(self):
        path = REPO_ROOT / "prompts" / "canonical" / "lead.md"
        before = path.read_bytes()
        status, payload = _http_json(
            "PUT",
            f"{self.base}/api/registry/file",
            {"path": str(path), "content": "must not write\n"},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(path.read_bytes(), before)

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


class RegenerateEndpointTests(unittest.TestCase):
    """Regeneration must inspect an isolated git workspace before writing."""

    def _post(self, project: Path, payload: dict) -> tuple[int, dict]:
        server = serve.DashboardServer(
            ("127.0.0.1", 0),
            root=project,
            workspaces=[project],
            auto_discover=False,
        )
        base = f"http://127.0.0.1:{server.server_address[1]}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return _http_json(
                "POST", f"{base}/api/registry/regenerate", payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def _init_git_repo(self, project: Path) -> None:
        subprocess.run(
            ["git", "init", "-q"], cwd=project, check=True,
            capture_output=True, text=True)

    def test_dirty_workspace_returns_conflict_without_running_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            self._init_git_repo(project)
            dirty = project / "dirty.txt"
            dirty.write_text("do not regenerate\n", encoding="utf-8")
            status, payload = self._post(project, {
                "path": str(REPO_ROOT / ".claude/agents/trio-lead.md"),
                "root": str(project),
            })
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"], "working tree dirty")
        self.assertIn("dirty.txt", payload["files"])

    def test_unmanaged_path_returns_bad_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            self._init_git_repo(project)
            status, payload = self._post(project, {
                "path": str(REPO_ROOT / ".claude/skills/trio-init/SKILL.md"),
                "root": str(project),
            })
        self.assertEqual(status, 400, payload)

    def test_clean_workspace_runs_generator_then_isolated_install(self):
        responses = [
            subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 0,
                stdout="  wrote .claude/agents/trio-lead.md\n",
                stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout="installed\n", stderr=""),
        ]
        with tempfile.TemporaryDirectory() as project_tmp, \
                tempfile.TemporaryDirectory() as home_tmp:
            project = Path(project_tmp).resolve()
            fake_home = Path(home_tmp).resolve()
            self._init_git_repo(project)
            original_home = serve.HOME
            try:
                serve.HOME = fake_home
                with mock.patch.object(
                    serve.subprocess, "run",
                    side_effect=responses,
                ) as run:
                    status, payload = self._post(project, {
                        "path": str(REPO_ROOT / ".claude/agents/trio-lead.md"),
                        "root": str(project),
                    })
            finally:
                serve.HOME = original_home

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["wrote"], [".claude/agents/trio-lead.md"])
        self.assertEqual(payload["install"], "installed\n")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["python3", "prompts/generate.py"],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["./install.sh", "--global"],
        )
        self.assertEqual(
            run.call_args_list[2].kwargs["env"]["HOME"],
            str(fake_home),
        )


class RoundTripTests(DashboardServerTestCase):
    ROLE_CONFIGS = tuple(sorted(
        (REPO_ROOT / "omnigent" / "trio-omnigent-roles").glob(
            "*/config.yaml")))

    def test_opencode_agent_http_round_trip(self):
        path = REPO_ROOT / "opencode" / "agents" / "trio-evaluator.md"
        status, get_payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        self.assertIn("permission.bash.sort", get_payload["quoted_keys"])
        self.assertNotIn("permission.read", get_payload["quoted_keys"])
        status, ser_payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": get_payload["format"],
                "frontmatter": get_payload["frontmatter"],
                "body": get_payload["body"],
                "quoted_keys": get_payload["quoted_keys"],
            })
        self.assertEqual(status, 200, ser_payload)
        content = ser_payload["content"]
        self.assertEqual(content.encode("utf-8"), path.read_bytes())
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

    def test_opencode_edit_preserves_untouched_key_quoting(self):
        path = REPO_ROOT / "opencode" / "agents" / "trio-evaluator.md"
        status, get_payload = self._get(f"/api/registry/file?path={path}")
        self.assertEqual(status, 200)
        frontmatter = dict(get_payload["frontmatter"])
        frontmatter["description"] = "Updated evaluator description."
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": get_payload["format"],
                "frontmatter": frontmatter,
                "body": get_payload["body"],
                "quoted_keys": get_payload["quoted_keys"],
            })
        self.assertEqual(status, 200, payload)
        content = payload["content"]
        self.assertIn('    "sort": allow\n', content)
        self.assertIn("  read: allow\n", content)
        self.assertNotIn("    sort: allow\n", content)
        self.assertNotIn('  "read": allow\n', content)

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

    def test_omnigent_role_http_round_trip_keeps_nested_document(self):
        self.assertEqual(len(self.ROLE_CONFIGS), 4)
        for path in self.ROLE_CONFIGS:
            with self.subTest(path=path):
                status, get_payload = self._get(
                    f"/api/registry/file?path={path}")
                self.assertEqual(status, 200, get_payload)
                self.assertEqual(get_payload["format"], "yaml-document")

                frontmatter = get_payload["frontmatter"]
                body = get_payload["body"]
                self.assertNotIn("prompt", frontmatter)
                self.assertTrue(body.strip())
                config = frontmatter["executor"]["config"]

                status, ser_payload = _http_json(
                    "POST",
                    f"{self.base}/api/registry/serialize",
                    {
                        "format": get_payload["format"],
                        "frontmatter": frontmatter,
                        "body": body,
                        "harness": "omnigent",
                        "surface": "agent",
                    },
                )
                self.assertEqual(status, 200, ser_payload)
                content = ser_payload["content"]
                self.assertNotIn("---", content)
                self.assertIn("prompt: |", content)

                parsed = registry.parse_yaml(content)
                self.assertEqual(parsed["prompt"], body)
                self.assertEqual(
                    parsed["executor"]["config"], config)
                expected_config_line = (
                    "  config: {harness: "
                    f"{config['harness']}, yolo: "
                    f"{str(config['yolo']).lower()}}}\n"
                )
                self.assertIn(expected_config_line, content)
                for key in ("os_env", "guardrails"):
                    if key in frontmatter:
                        self.assertIn(key, parsed)
                        self.assertEqual(parsed[key], frontmatter[key])

    def test_serialize_unflattens_omnigent_dotted_executor_keys(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml-document",
                "harness": "omnigent",
                "surface": "agent",
                "body": "hello\n",
                "frontmatter": {
                    "name": "throwaway",
                    "description": "x",
                    "executor": {"type": "omnigent"},
                    "executor.model": "gpt-5.6-luna-max",
                    "executor.config.harness": "cursor-native",
                    "executor.config.yolo": True,
                },
            },
        )
        self.assertEqual(status, 200, payload)
        parsed = registry.parse_yaml(payload["content"])
        self.assertNotIn("executor.model", parsed)
        self.assertEqual(parsed["executor"]["type"], "omnigent")
        self.assertEqual(parsed["executor"]["model"], "gpt-5.6-luna-max")
        self.assertIn(
            "config: {harness: cursor-native, yolo: true}",
            payload["content"],
        )


class SerializeValidationTests(DashboardServerTestCase):

    def test_text_format_serializes_body_verbatim(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {"format": "text", "frontmatter": {}, "body": "whole file body\n"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "whole file body\n")
        self.assertEqual(payload["warnings"], [])

    def test_json_schema_invalid_json_is_400(self):
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {
                    "name": "trio-evaluator",
                    "description": "test output schema",
                    "output": '{"type": invalid}',
                },
                "body": "body\n",
                "harness": "omp",
                "surface": "agent",
            })
        self.assertEqual(status, 400, payload)
        self.assertIn("output", payload["error"])

    def test_json_schema_valid_json_is_200(self):
        value = '{\n  "type": "object"\n}\n'
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {
                    "name": "trio-evaluator",
                    "description": "test output schema",
                    "output": value,
                },
                "body": "body\n",
                "harness": "omp",
                "surface": "agent",
            })
        self.assertEqual(status, 200, payload)
        self.assertIn("output: |", payload["content"])
        fields, _body = registry.parse_frontmatter(payload["content"])
        self.assertEqual(json.loads(fields["output"]), {"type": "object"})

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

    def test_malformed_yaml_wrapper_mis_indented_nested_line_returns_400(self):
        # A realistic slip: mis-indenting one line of a nested permission
        # block by two spaces. The lenient scan.py parser silently drops the
        # "rm -rf *" rule and returns {} for that branch (see VERDICT.md
        # iteration 1); the serialize endpoint must reject it instead of
        # writing a file with a rule silently missing.
        malformed = '"*": deny\nbash:\n    "git status *": allow\n  "rm -rf *": deny\n'
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"permission": {"$yaml": malformed}},
                "body": "body\n",
            })
        self.assertEqual(status, 400, payload)
        self.assertIn("permission", payload["error"])

    def test_malformed_yaml_wrapper_tab_indentation_returns_400(self):
        # A tab-indented nested line: scan.py's indent tracking only counts
        # spaces, so a tab is silently treated as indent 0 and the value is
        # re-nested at the wrong level instead of raising.
        malformed = "a:\n\tb: 1\n"
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"permission": {"$yaml": malformed}},
                "body": "body\n",
            })
        self.assertEqual(status, 400, payload)
        self.assertIn("permission", payload["error"])

    def test_malformed_yaml_wrapper_duplicate_key_returns_400(self):
        # A duplicate key at the same nesting level: the lenient parser just
        # lets the later value silently overwrite the earlier one.
        malformed = '"*": deny\nread: allow\nread: deny\n'
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"permission": {"$yaml": malformed}},
                "body": "body\n",
            })
        self.assertEqual(status, 400, payload)
        self.assertIn("permission", payload["error"])

    def test_malformed_yaml_wrapper_unclosed_flow_returns_400(self):
        # An unclosed flow list/map: the lenient parser coerces this into a
        # partial list rather than raising.
        malformed = "bad: [unclosed\n"
        status, payload = _http_json(
            "POST", f"{self.base}/api/registry/serialize",
            {
                "format": "yaml",
                "frontmatter": {"permission": {"$yaml": malformed}},
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
                "format": "yaml-document",
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

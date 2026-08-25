#!/usr/bin/env python3
"""Unit tests for the isolated, precedence-aware model collector."""
from __future__ import annotations

import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_PATH = REPO_ROOT / "registry" / "models.py"


def _load_models():
    spec = importlib.util.spec_from_file_location(
        "trio_registry_models_test", MODELS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load models module: {MODELS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


models = _load_models()


class ModelCollectorTests(unittest.TestCase):
    """Each test uses a tiny repository and a throwaway explicit home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.home = base / "home"
        self.bin = base / "bin"
        self.root.mkdir()
        self.home.mkdir()
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative: str, text: str, *, home: bool = False):
        path = (self.home if home else self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def executable(self, name: str, body: str):
        """Create a shell fixture that can be selected through PATH."""
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def cli_env(self):
        return {"PATH": str(self.bin)}

    def write_catalog_clis(self):
        """Install small, side-effect-free catalog command fixtures."""
        self.executable(
            "cursor-agent",
            "printf '\\033[32mAvailable models\\033[0m\\n'\n"
            "printf 'cursor/fast - Fast model\\n'\n"
            "printf 'bare-model\\n'",
        )
        self.executable(
            "opencode",
            "printf '\\033[36mopen/live\\033[0m\\n'\n"
            "printf '\\nopen/second\\n'",
        )
        self.executable(
            "omp",
            "printf '%s' "
            "'{\"models\":[{\"selector\":\"omp/selected\"},"
            "{\"provider\":\"vertex\",\"id\":\"gemini\"}]}'",
        )

    def row(self, harness: str, agent: str):
        result = models.collect_models(self.root, home=self.home)
        return next(
            row for row in result["rows"]
            if row["harness"] == harness and row["agent"] == agent
        )

    def test_frontmatter_wins_over_omp_config(self):
        self.write(
            ".claude/agents/trio-builder.md",
            "---\nname: trio-builder\nmodel: front-model\n---\nbody\n",
        )
        self.write(
            ".omp/agent/config.yml",
            "task:\n  agentModelOverrides:\n    trio-builder: override-model\n",
            home=True,
        )
        row = self.row("claude", "trio-builder")
        self.assertEqual(row["model"], "front-model")
        self.assertEqual(row["layer"], "frontmatter")
        self.assertTrue(row["editable"])
        self.assertIsNone(row["override_file"])

    def test_omp_config_wins_when_frontmatter_model_is_absent(self):
        self.write(
            ".claude/agents/trio-builder.md",
            "---\nname: trio-builder\ndescription: no pin\n---\nbody\n",
        )
        self.write(
            ".omp/agent/config.yml",
            "task:\n  agentModelOverrides:\n    trio-builder: override-model\n",
            home=True,
        )
        row = self.row("claude", "trio-builder")
        self.assertEqual(row["model"], "override-model")
        self.assertEqual(row["layer"], "omp-config")
        self.assertFalse(row["editable"])
        self.assertTrue(row["override_file"].endswith("config.yml"))

    def test_jsonc_comments_are_stripped_and_agent_model_is_read(self):
        self.write(
            "opencode/agents/trio-builder.md",
            "---\ndescription: no model\n---\nbody\n",
        )
        self.write(
            "opencode/opencode.trio.example.jsonc",
            '{\n'
            '  "agent": {\n'
            '    "trio-builder": {"model": "open/live"}\n'
            '  }\n'
            '  // trailing comment\n'
            '}\n',
        )
        row = self.row("opencode", "trio-builder")
        self.assertEqual(row["model"], "open/live")
        self.assertEqual(row["layer"], "opencode-jsonc")

    def test_trioctl_builder_fallback_is_used(self):
        self.write(
            "omp/agents/trio-builder.md",
            "---\nname: trio-builder\ndescription: no model\n---\nbody\n",
        )
        self.write(
            "omnigent/trioctl.example.toml",
            '[roles.builder]\nfallback_model = "fallback/builder"\n',
        )
        row = self.row("omp", "trio-builder")
        self.assertEqual(row["model"], "fallback/builder")
        self.assertEqual(row["layer"], "trioctl-roles")

    def test_omnigent_executor_is_the_last_layer(self):
        self.write(
            "omnigent/trio-omnigent-roles/builder/config.yaml",
            "name: trio-omnigent-builder\nexecutor:\n  model: executor/builder\n",
        )
        row = self.row("omnigent", "trio-omnigent-builder")
        self.assertEqual(row["model"], "executor/builder")
        self.assertEqual(row["layer"], "omnigent-executor")
        self.assertIsNone(row["path"])

    def test_unknown_custom_model_is_reported_without_raising(self):
        self.write(
            ".claude/agents/trio-builder.md",
            "---\nname: trio-builder\nmodel: custom/unknown\n---\nbody\n",
        )
        row = self.row("claude", "trio-builder")
        self.assertEqual(row["availability"], "unknown")
        self.assertEqual(row["warning"], "unknown model id")

    def test_collect_models_does_not_create_home_files(self):
        self.write(
            ".claude/agents/trio-builder.md",
            "---\nname: trio-builder\nmodel: sonnet\n---\nbody\n",
        )
        before = sorted(self.home.rglob("*"))
        models.collect_models(self.root, home=self.home)
        self.assertEqual(sorted(self.home.rglob("*")), before)

    def test_harvests_config_files_with_explicit_home(self):
        self.write(
            ".claude/settings.json",
            '{"model": "claude/live", "models": {"claude/key": {}}}',
            home=True,
        )
        self.write(
            ".codex/config.toml",
            'model = "codex/live"\n[models]\n"codex/key" = {}\n',
            home=True,
        )
        self.write(
            ".config/opencode/opencode.json",
            '{"model": "open/json"}',
            home=True,
        )
        self.write(
            ".config/opencode/opencode.jsonc",
            '{\n  "providers": {"open": {"models": {"jsonc/key": {}}}}\n'
            '  // fixture comment\n}\n',
            home=True,
        )
        self.write(
            ".omp/agent/config.yml",
            "model: omp/live\n",
            home=True,
        )

        catalog = models.harvest_catalog(
            self.home,
            env=self.cli_env(),
            allow_cli=False,
            use_cache=False,
        )
        self.assertIn("claude/live", catalog["available"]["claude"])
        self.assertIn("claude/key", catalog["available"]["claude"])
        self.assertIn("codex/live", catalog["available"]["codex"])
        self.assertIn("codex/key", catalog["available"]["codex"])
        self.assertIn("open/json", catalog["available"]["opencode"])
        self.assertIn("jsonc/key", catalog["available"]["opencode"])
        self.assertIn("omp/live", catalog["available"]["omp"])

    def test_harvests_cli_catalogs_and_reports_missing_cli(self):
        self.write_catalog_clis()
        catalog = models.harvest_catalog(
            self.home,
            env=self.cli_env(),
            allow_cli=True,
            use_cache=False,
        )
        self.assertIn("cursor/fast", catalog["available"]["cursor"])
        self.assertIn("bare-model", catalog["available"]["cursor"])
        self.assertEqual(
            catalog["by_executor"]["cursor"],
            catalog["by_executor"]["cursor-native"],
        )
        self.assertIn("omp/selected", catalog["available"]["omp"])
        self.assertIn("vertex/gemini", catalog["available"]["omp"])
        self.assertIn("claude-opus-5", catalog["available"]["claude"])
        self.assertTrue(
            any(not source["ok"] for source in catalog["sources"]["claude"])
        )
        self.assertEqual(
            catalog["available"]["omnigent"],
            sorted({
                model
                for harness in models.EXECUTOR_HARNESSES
                for model in catalog["available"][harness]
            }),
        )
        for harness in models.CATALOG_HARNESSES:
            for source in catalog["sources"][harness]:
                self.assertEqual(
                    set(source), {"source", "ok", "count", "error"}
                )
                if source["ok"]:
                    self.assertIsNone(source["error"])

    def test_harvest_does_not_create_files_under_fake_home(self):
        self.write_catalog_clis()
        before = sorted(self.home.rglob("*"))
        models.harvest_catalog(
            self.home,
            env=self.cli_env(),
            allow_cli=True,
            use_cache=False,
        )
        self.assertEqual(sorted(self.home.rglob("*")), before)

    def test_allow_cli_false_never_executes_binaries(self):
        self.executable(
            "cursor-agent",
            'printf executed > "$HOME/should-not-exist"',
        )
        models.harvest_catalog(
            self.home,
            env=self.cli_env(),
            allow_cli=False,
            use_cache=False,
        )
        self.assertFalse((self.home / "should-not-exist").exists())

    def test_public_parsers_and_resolution_helper(self):
        text = '{"url": "https://example.test//path"} // comment\n'
        self.assertNotIn("comment", models.strip_jsonc(text))
        self.assertEqual(
            models.parse_jsonc(text)["url"], "https://example.test//path")
        toml_path = self.write("sample.toml", 'model = "gpt-5.6-luna"\n')
        self.assertEqual(
            models.load_toml(toml_path)["model"], "gpt-5.6-luna")
        self.assertEqual(
            models.resolve_model(
                frontmatter="first", **{"omp-config": "second"}),
            ("first", "frontmatter"),
        )


if __name__ == "__main__":
    unittest.main()

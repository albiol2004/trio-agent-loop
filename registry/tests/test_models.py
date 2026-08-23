#!/usr/bin/env python3
"""Unit tests for the isolated, precedence-aware model collector."""
from __future__ import annotations

import importlib.util
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
        self.root.mkdir()
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative: str, text: str, *, home: bool = False):
        path = (self.home if home else self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

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

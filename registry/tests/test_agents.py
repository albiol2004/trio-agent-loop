#!/usr/bin/env python3
"""Tests for the canonical agent model (registry/agents.py).

Run: python3 -m unittest discover -s registry/tests -t .
"""

from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load("trio_registry_scan_agents_test", "registry/scan.py")
agents = _load("trio_registry_agents_test", "registry/agents.py")


SEED_NAMES = ("registry-scout", "registry-editor")


def _make_agent(name="sample-agent", model_tier="standard", tool_policy="edit",
                 instructions="Hello.\n", description="A sample agent."):
    return agents.CanonicalAgent(
        name=name, description=description, instructions=instructions,
        model_tier=model_tier, tool_policy=tool_policy)


# --------------------------------------------------------------------------
# Round-trip
# --------------------------------------------------------------------------


class RoundTrip(unittest.TestCase):
    def _assert_round_trips(self, agent: agents.CanonicalAgent):
        dumped = agents.dump_agent(agent)
        again = agents.parse_agent(dumped, name=agent.name)
        self.assertEqual(again, agent)

    def test_seed_registry_scout_round_trips(self):
        self._assert_round_trips(agents.load_agent("registry-scout"))

    def test_seed_registry_editor_round_trips(self):
        self._assert_round_trips(agents.load_agent("registry-editor"))

    def test_tricky_instructions_round_trip(self):
        instructions = (
            "Some text with a --- horizontal rule look-alike.\n\n"
            "Backticks: `inline code` and a fenced block:\n\n"
            "```python\n"
            "def f(x):\n"
            "    return x + 1\n"
            "```\n\n"
            "Trailing newline follows.\n"
        )
        agent = _make_agent(name="tricky-agent", instructions=instructions)
        self._assert_round_trips(agent)
        # instructions body must not have been mangled
        dumped = agents.dump_agent(agent)
        again = agents.parse_agent(dumped, name="tricky-agent")
        self.assertEqual(again.instructions, instructions)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class Validation(unittest.TestCase):
    def test_bad_name_raises(self):
        with self.assertRaises(ValueError):
            _make_agent(name="Not-A-Valid-Name")
        with self.assertRaises(ValueError):
            _make_agent(name="-leading-dash")
        with self.assertRaises(ValueError):
            _make_agent(name="")

    def test_empty_description_raises(self):
        with self.assertRaises(ValueError):
            _make_agent(description="")

    def test_unknown_model_tier_raises(self):
        with self.assertRaises(ValueError):
            _make_agent(model_tier="ultra")

    def test_unknown_tool_policy_raises(self):
        with self.assertRaises(ValueError):
            _make_agent(tool_policy="god-mode")

    def test_name_stem_mismatch_raises(self):
        text = agents.dump_agent(_make_agent(name="foo"))
        with self.assertRaises(ValueError):
            agents.parse_agent(text, name="bar")

    def test_missing_name_both_sides_raises(self):
        # No frontmatter name key, and no name= argument given.
        with self.assertRaises(ValueError):
            agents.parse_agent("no frontmatter here at all")


# --------------------------------------------------------------------------
# Renderer output parses with the real parsers + key vocabulary
# --------------------------------------------------------------------------


class RendererOutputParses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = agents.load_agent("registry-scout")

    def test_claude_parses_and_has_name(self):
        r = agents.render_agent(self.agent, "claude")
        self.assertEqual(r.format, "yaml")
        self.assertEqual(r.filename, "registry-scout.md")
        fields, body = scan.parse_frontmatter(r.text)
        self.assertEqual(fields["name"], self.agent.name)
        self.assertEqual(body, self.agent.instructions)
        for value in fields.values():
            self.assertNotEqual(value, "")

    def test_codex_parses_and_has_name(self):
        r = agents.render_agent(self.agent, "codex")
        self.assertEqual(r.format, "toml")
        self.assertEqual(r.filename, "registry-scout.toml")
        fields = scan.parse_toml(r.text)
        self.assertEqual(fields["name"], self.agent.name)
        for key, value in fields.items():
            if key == "developer_instructions":
                continue
            self.assertNotEqual(value, "")

    def test_omp_parses_and_has_name(self):
        r = agents.render_agent(self.agent, "omp")
        self.assertEqual(r.format, "yaml")
        fields, body = scan.parse_frontmatter(r.text)
        self.assertEqual(fields["name"], self.agent.name)
        self.assertEqual(body, self.agent.instructions)
        for value in fields.values():
            self.assertNotEqual(value, "")

    def test_opencode_parses_and_has_no_name(self):
        r = agents.render_agent(self.agent, "opencode")
        self.assertEqual(r.format, "yaml")
        fields, body = scan.parse_frontmatter(r.text)
        self.assertNotIn("name", fields)
        self.assertEqual(body, self.agent.instructions)
        self.assertEqual(fields["mode"], "subagent")
        self.assertIs(fields["hidden"], True)
        for value in fields.values():
            self.assertNotEqual(value, "")


# --------------------------------------------------------------------------
# Codex developer_instructions exactness
# --------------------------------------------------------------------------


class CodexDeveloperInstructions(unittest.TestCase):
    def test_multiline_instructions_round_trip_exactly(self):
        instructions = (
            "# Heading\n\n"
            "Some ```code``` and a fence:\n\n"
            "```bash\necho hi\n```\n\n"
            "Trailing.\n"
        )
        agent = _make_agent(name="codex-di-agent", instructions=instructions)
        r = agents.render_agent(agent, "codex")
        fields = scan.parse_toml(r.text)
        self.assertEqual(fields["developer_instructions"], instructions)

    def test_empty_instructions_round_trip_exactly(self):
        agent = _make_agent(name="codex-empty-agent", instructions="")
        r = agents.render_agent(agent, "codex")
        fields = scan.parse_toml(r.text)
        self.assertEqual(fields["developer_instructions"], "")

    def test_seed_agents_developer_instructions_exact(self):
        for name in SEED_NAMES:
            agent = agents.load_agent(name)
            r = agents.render_agent(agent, "codex")
            fields = scan.parse_toml(r.text)
            with self.subTest(name=name):
                self.assertEqual(fields["developer_instructions"], agent.instructions)


# --------------------------------------------------------------------------
# opencode permission nesting
# --------------------------------------------------------------------------


class OpencodePermission(unittest.TestCase):
    def test_read_only_permission_is_nested_dict_with_star(self):
        agent = _make_agent(tool_policy="read-only")
        r = agents.render_agent(agent, "opencode")
        fields, _ = scan.parse_frontmatter(r.text)
        perm = fields["permission"]
        self.assertIsInstance(perm, dict)
        self.assertIn("*", perm)
        self.assertEqual(perm["*"], "deny")
        self.assertEqual(perm["read"], "allow")

    def test_edit_permission_dict(self):
        agent = _make_agent(tool_policy="edit")
        r = agents.render_agent(agent, "opencode")
        fields, _ = scan.parse_frontmatter(r.text)
        self.assertEqual(fields["permission"], {"task": "deny"})

    def test_spawn_permission_dict(self):
        agent = _make_agent(tool_policy="spawn")
        r = agents.render_agent(agent, "opencode")
        fields, _ = scan.parse_frontmatter(r.text)
        self.assertEqual(fields["permission"], {"task": "allow"})


# --------------------------------------------------------------------------
# Every (tier, policy) combination x every render harness
# --------------------------------------------------------------------------


class AllCombinations(unittest.TestCase):
    def test_every_combination_renders_without_raising(self):
        for tier in agents.MODEL_TIERS:
            for policy in agents.TOOL_POLICIES:
                agent = _make_agent(
                    name=f"combo-{tier}-{policy}", model_tier=tier, tool_policy=policy)
                for harness in agents.RENDER_HARNESSES:
                    with self.subTest(tier=tier, policy=policy, harness=harness):
                        r = agents.render_agent(agent, harness)
                        self.assertTrue(r.text)

    def test_model_value_non_empty_for_claude_codex_omp(self):
        for tier in agents.MODEL_TIERS:
            for policy in agents.TOOL_POLICIES:
                agent = _make_agent(
                    name=f"model-{tier}-{policy}", model_tier=tier, tool_policy=policy)
                for harness in ("claude", "codex", "omp"):
                    with self.subTest(tier=tier, policy=policy, harness=harness):
                        r = agents.render_agent(agent, harness)
                        if harness == "codex":
                            fields = scan.parse_toml(r.text)
                        else:
                            fields, _ = scan.parse_frontmatter(r.text)
                        self.assertIn("model", fields)
                        self.assertTrue(fields["model"])

    def test_renderers_never_mutate_the_tables(self):
        tiers_before = copy.deepcopy(agents.MODEL_TIERS)
        policies_before = copy.deepcopy(agents.TOOL_POLICIES)
        for tier in agents.MODEL_TIERS:
            for policy in agents.TOOL_POLICIES:
                agent = _make_agent(model_tier=tier, tool_policy=policy)
                for harness in agents.RENDER_HARNESSES:
                    agents.render_agent(agent, harness)
        self.assertEqual(agents.MODEL_TIERS, tiers_before)
        self.assertEqual(agents.TOOL_POLICIES, policies_before)


# --------------------------------------------------------------------------
# Unsupported harness
# --------------------------------------------------------------------------


class UnsupportedHarnessTest(unittest.TestCase):
    def test_omnigent_raises_unsupported_harness(self):
        agent = _make_agent()
        with self.assertRaises(agents.UnsupportedHarness) as ctx:
            agents.render_agent(agent, "omnigent")
        self.assertEqual(ctx.exception.harness, "omnigent")
        self.assertTrue(ctx.exception.reason)

    def test_install_support_matches_render_agent_reason(self):
        agent = _make_agent()
        with self.assertRaises(agents.UnsupportedHarness) as ctx:
            agents.render_agent(agent, "omnigent")
        self.assertEqual(agents.install_support("omnigent"), (False, ctx.exception.reason))

    def test_install_support_unknown_harness(self):
        supported, reason = agents.install_support("totally-unknown-harness")
        self.assertFalse(supported)
        self.assertTrue(reason)

    def test_render_agent_unknown_harness_raises(self):
        agent = _make_agent()
        with self.assertRaises(agents.UnsupportedHarness):
            agents.render_agent(agent, "totally-unknown-harness")


# --------------------------------------------------------------------------
# CRUD against a tempdir root
# --------------------------------------------------------------------------


class CrudAgainstTempdir(unittest.TestCase):
    def test_full_crud_lifecycle_never_touches_real_dir(self):
        real_dir = agents.agents_dir()
        real_before = set(real_dir.glob("*.md")) if real_dir.is_dir() else set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = _make_agent(name="temp-crud-agent", description="v1")
            path = agents.save_agent(agent, root=root)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, root)

            listed = agents.list_agents(root=root)
            self.assertEqual([a.name for a in listed], ["temp-crud-agent"])

            loaded = agents.load_agent("temp-crud-agent", root=root)
            self.assertEqual(loaded, agent)

            updated = _make_agent(name="temp-crud-agent", description="v2")
            agents.save_agent(updated, root=root)
            reloaded = agents.load_agent("temp-crud-agent", root=root)
            self.assertEqual(reloaded.description, "v2")

            self.assertTrue(agents.delete_agent("temp-crud-agent", root=root))
            self.assertFalse(agents.delete_agent("temp-crud-agent", root=root))
            with self.assertRaises(FileNotFoundError):
                agents.load_agent("temp-crud-agent", root=root)

        real_after = set(real_dir.glob("*.md")) if real_dir.is_dir() else set()
        self.assertEqual(real_before, real_after)

    def test_list_agents_empty_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(agents.list_agents(root=Path(tmp)), [])

    def test_list_agents_nonexistent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            self.assertEqual(agents.list_agents(root=missing), [])


# --------------------------------------------------------------------------
# agent_index_records shape + body_hash invariant vs scan.entry_record
# --------------------------------------------------------------------------


class AgentIndexRecords(unittest.TestCase):
    def test_shape_against_seed_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in SEED_NAMES:
                agents.save_agent(agents.load_agent(name), root=root)
            records = agents.agent_index_records(root=root)
        self.assertEqual(len(records), len(SEED_NAMES))
        by_name = {r["name"]: r for r in records}
        for name in SEED_NAMES:
            r = by_name[name]
            self.assertEqual(r["surface"], "canonical-agent")
            self.assertIn("path", r)
            self.assertIn("model_tier", r)
            self.assertIn("tool_policy", r)
            self.assertIn("description", r)
            self.assertEqual(set(r["renders"]), set(agents.RENDER_HARNESSES))
            for harness, render in r["renders"].items():
                self.assertIn("filename", render)
                self.assertIn("format", render)
                self.assertIn("body_hash", render)
                self.assertIn("frontmatter", render)
            self.assertIn("omnigent", r["unsupported"])
            self.assertTrue(r["unsupported"]["omnigent"])

    def test_body_hash_matches_entry_record_for_every_harness(self):
        agent = agents.load_agent("registry-scout")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents.save_agent(agent, root=root)
            records = agents.agent_index_records(root=root)
            record = records[0]

            for harness in agents.RENDER_HARNESSES:
                rendered = agents.render_agent(agent, harness)
                out_path = root / rendered.filename
                out_path.write_text(rendered.text, encoding="utf-8")
                entry = scan.entry_record(out_path, harness, "agent", "canonical")
                with self.subTest(harness=harness):
                    self.assertEqual(
                        entry["body_hash"], record["renders"][harness]["body_hash"])


if __name__ == "__main__":
    unittest.main()

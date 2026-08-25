#!/usr/bin/env python3
"""Tests for the canonical harness topology collector.

The fixture is the repository itself: topology is read-only and must describe
the six native harness layouts without consulting the test runner's home.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent.parent
TOPOLOGY_PATH = REPO / "registry" / "topology.py"


def _load_topology():
    """Load the module by path, matching the registry module convention."""
    spec = importlib.util.spec_from_file_location(
        "trio_registry_topology_test", TOPOLOGY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load topology module: {TOPOLOGY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


topology = _load_topology()


def _edge_tuples(graph: dict) -> set[tuple[str, str, str]]:
    """Reduce public edge records to the form used by behavior assertions."""
    return {
        (edge["type"], edge["src"], edge["dst"])
        for edge in graph["edges"]
    }


class TopologyCollectorTests(unittest.TestCase):
    """The repository corpus is the ground truth for every required edge."""

    @classmethod
    def setUpClass(cls):
        cls.result = topology.collect_topology(REPO)
        cls.graphs = cls.result["graphs"]
        cls.productionize = topology.collect_topology(
            REPO, workflow="productionize")
        cls.entrypoints = topology.collect_topology(
            REPO, workflow="entrypoints")

    def test_required_harness_graphs_are_present(self):
        required = {"claude", "codex", "omp", "opencode", "omnigent", "pi"}
        self.assertTrue(required.issubset(self.graphs), self.graphs.keys())

    def test_default_workflow_remains_roles_with_spawn_edges(self):
        self.assertEqual(self.result["workflow"], "roles")
        self.assertIn(
            ("spawns", "trio-lead", "trio-builder"),
            _edge_tuples(self.graphs["omp"]),
        )

    def test_required_graphs_have_nodes(self):
        for harness in ("claude", "codex", "omp",
                        "opencode", "omnigent", "pi"):
            with self.subTest(harness=harness):
                self.assertGreaterEqual(len(self.graphs[harness]["nodes"]), 1)

    def test_cursor_fixture_collects_project_skill_and_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".cursor" / "skills" / "demo" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "# Demo skill\n\nUse this skill.\n",
                encoding="utf-8",
            )
            agent = root / ".cursor" / "agents" / "demo.md"
            agent.parent.mkdir(parents=True)
            agent.write_text(
                "---\n"
                "name: demo\n"
                "description: Demo agent.\n"
                "model: cursor-grok-4.6-medium\n"
                "---\n\n"
                "Follow these instructions.\n",
                encoding="utf-8",
            )

            result = topology.collect_topology(root)

        graph = result["graphs"]["cursor"]
        node_keys = {
            (node["kind"], node["name"], node["harness"])
            for node in graph["nodes"]
        }
        self.assertIn(("entrypoint", "demo", "cursor"), node_keys)
        self.assertIn(("agent", "demo", "cursor"), node_keys)

    def test_opencode_invokes_orchestrator_and_spawns_allowed_agents(self):
        edges = _edge_tuples(self.graphs["opencode"])
        self.assertIn(("invokes", "trio", "trio-orchestrator"), edges)
        expected = {
            ("spawns", "trio-orchestrator", target)
            for target in (
                "trio-scout", "trio-lead", "trio-repair", "trio-evaluator")
        }
        self.assertTrue(expected.issubset(edges), expected - edges)

    def test_omp_lead_spawns_builder_and_scout(self):
        edges = _edge_tuples(self.graphs["omp"])
        self.assertIn(("spawns", "trio-lead", "trio-builder"), edges)
        self.assertIn(("spawns", "trio-lead", "trio-scout"), edges)

    def test_omp_evaluator_reports_output_schema(self):
        evaluator = next(
            node for node in self.graphs["omp"]["nodes"]
            if node["kind"] == "agent" and node["name"] == "trio-evaluator"
        )
        self.assertTrue(evaluator["output"])

    def test_omnigent_lead_has_executor_model_node(self):
        graph = self.graphs["omnigent"]
        self.assertIn(
            ("agent", "trio-omnigent-lead"),
            {(node["kind"], node["name"]) for node in graph["nodes"]})
        config = REPO / "omnigent/trio-omnigent-roles/lead/config.yaml"
        fields = topology.scan.parse_yaml(config.read_text(encoding="utf-8"))
        model = fields["executor"]["model"]
        self.assertIn(
            ("model", model),
            {(node["kind"], node["name"]) for node in graph["nodes"]})

    def test_claude_productionize_dispatches_to_scout(self):
        edges = _edge_tuples(self.graphs["claude"])
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-scout"), edges)

    def test_productionize_has_a_graph_for_every_wrapper(self):
        self.assertEqual(self.productionize["workflow"], "productionize")
        self.assertEqual(
            set(self.productionize["graphs"]),
            {"claude", "codex", "omp", "opencode", "kimi", "zcode"},
        )

    def test_entrypoints_include_only_trio_entrypoint_wiring(self):
        result = self.entrypoints
        self.assertEqual(result["workflow"], "entrypoints")
        claude_names = {
            node["name"] for node in result["graphs"]["claude"]["nodes"]
            if node["kind"] == "entrypoint"
        }
        self.assertTrue(
            {"trio", "trio-productionize"}.issubset(claude_names))

        for graph in result["graphs"].values():
            entries = {
                node["name"] for node in graph["nodes"]
                if node["kind"] == "entrypoint"
            }
            self.assertTrue(
                all(edge["src"] in entries for edge in graph["edges"]))
            self.assertNotIn("spawns", {
                edge["type"] for edge in graph["edges"]})
            self.assertNotIn("subagent", {
                edge["type"] for edge in graph["edges"]})

        opencode_edges = _edge_tuples(result["graphs"]["opencode"])
        self.assertIn(
            ("invokes", "trio", "trio-orchestrator"), opencode_edges)
        for harness in ("claude", "opencode", "omp"):
            self.assertIn(
                ("dispatches_to", "trio-productionize", "trio-scout"),
                _edge_tuples(result["graphs"][harness]),
            )

    def test_entrypoints_include_kimi_zcode_omnigent_and_pi(self):
        graphs = self.entrypoints["graphs"]
        for harness in ("kimi", "zcode"):
            self.assertIn(harness, graphs)
            names = {
                node["name"] for node in graphs[harness]["nodes"]
                if node["kind"] == "entrypoint"
            }
            self.assertIn("trio-productionize", names)

        omnigent_names = {
            node["name"] for node in graphs["omnigent"]["nodes"]
            if node["kind"] == "entrypoint"
        }
        self.assertTrue({
            "trio-omnigent", "trio-productionize-omnigent"
        }.issubset(omnigent_names))
        omnigent_edges = _edge_tuples(graphs["omnigent"])
        self.assertIn(
            ("dispatches_to", "trio-omnigent", "trio-omnigent-lead"),
            omnigent_edges,
        )
        pi_names = {
            node["name"] for node in graphs["pi"]["nodes"]
            if node["kind"] == "entrypoint"
        }
        self.assertIn("trio", pi_names)

    def test_entrypoints_fixture_dispatches_without_home_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".claude" / "skills" / "trio" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "# Fixture\n\n"
                "## Dispatch table\n\n"
                "- agent: `trio-lead`\n",
                encoding="utf-8",
            )

            with patch.object(
                topology.Path,
                "home",
                side_effect=AssertionError("Path.home used"),
            ):
                result = topology.collect_topology(
                    root,
                    home=Path("/never-read"),
                    workflow="entrypoints",
                )

        graph = result["graphs"]["claude"]
        self.assertIn(
            ("dispatches_to", "trio", "trio-lead"),
            _edge_tuples(graph),
        )
        self.assertIn(
            ("agent", "trio-lead"),
            {(node["kind"], node["name"]) for node in graph["nodes"]},
        )

    def test_productionize_claude_dispatches_to_all_trio_roles(self):
        edges = _edge_tuples(self.productionize["graphs"]["claude"])
        expected = {
            ("subagent", "trio-productionize", target)
            for target in ("trio-scout", "trio-lead", "trio-evaluator")
        }
        self.assertTrue(expected.issubset(edges), expected - edges)

    def test_productionize_preserves_harness_dispatch_mechanisms(self):
        graphs = self.productionize["graphs"]
        self.assertIn(
            ("skill", "trio-productionize", "trio-scout"),
            _edge_tuples(graphs["kimi"]),
        )
        self.assertIn(
            ("subagent", "trio-productionize", "trio-scout"),
            _edge_tuples(graphs["zcode"]),
        )
        self.assertIn(
            ("subagent", "trio-productionize", "default task agent"),
            _edge_tuples(graphs["omp"]),
        )

    def test_productionize_fixture_warns_and_parses_without_home_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = (
                root / ".claude" / "skills" / "trio-productionize" /
                "SKILL.md")
            malformed.parent.mkdir(parents=True)
            malformed.write_text(
                "# Fixture\n\n## Dispatch table\n\nNo executor rows.\n",
                encoding="utf-8",
            )
            parsed = (
                root / "zcode" / "skills" / "trio-productionize" / "SKILL.md")
            parsed.parent.mkdir(parents=True)
            parsed.write_text(
                "## Dispatch table\n\n"
                "- executor: scout: invoke Agent with custom subagent "
                "`trio-scout`.\n",
                encoding="utf-8",
            )
            command = (
                root / "opencode" / "commands" / "trio-productionize.md")
            command.parent.mkdir(parents=True)
            command.write_text(
                "## Dispatch table\n\n"
                "- executor: scout: use the slash command `/trio-scout`.\n",
                encoding="utf-8",
            )

            with patch.object(
                topology.Path,
                "home",
                side_effect=AssertionError("Path.home used"),
            ):
                result = topology.collect_topology(
                    root, home=Path("/never-read"), workflow="productionize")

        warning = result["graphs"]["claude"]["nodes"]
        self.assertEqual(len(warning), 1)
        self.assertEqual(warning[0]["kind"], "warning")
        self.assertEqual(warning[0]["name"], "unparseable-dispatch-table")
        self.assertEqual(warning[0]["path"], str(malformed))

        parsed_graph = result["graphs"]["zcode"]
        self.assertIn(
            ("agent", "trio-scout"),
            {(node["kind"], node["name"]) for node in parsed_graph["nodes"]},
        )
        self.assertIn(
            ("subagent", "trio-productionize", "trio-scout"),
            _edge_tuples(parsed_graph),
        )
        self.assertIn(
            ("command", "trio-productionize", "trio-scout"),
            _edge_tuples(result["graphs"]["opencode"]),
        )

    def test_opencode_productionize_dispatches_to_scout_and_orchestrator(self):
        edges = _edge_tuples(self.graphs["opencode"])
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-scout"), edges)
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-orchestrator"),
            edges)

    def test_omp_productionize_dispatches_to_scout(self):
        edges = _edge_tuples(self.graphs["omp"])
        self.assertIn(
            ("dispatches_to", "trio-productionize", "trio-scout"), edges)

    def test_pi_has_trio_entrypoint(self):
        names = {
            node["name"] for node in self.graphs["pi"]["nodes"]
            if node["kind"] == "entrypoint"
        }
        self.assertIn("trio", names)

    def test_nodes_and_edges_are_stably_sorted(self):
        for graph in self.graphs.values():
            nodes = [(node["kind"], node["name"]) for node in graph["nodes"]]
            edges = [
                (edge["type"], edge["src"], edge["dst"])
                for edge in graph["edges"]
            ]
            self.assertEqual(nodes, sorted(nodes))
            self.assertEqual(edges, sorted(edges))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the canonical harness topology collector.

The fixture is the repository itself: topology is read-only and must describe
the six native harness layouts without consulting the test runner's home.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


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

    def test_required_harness_graphs_are_present(self):
        required = {"claude", "codex", "omp", "opencode", "omnigent", "pi"}
        self.assertTrue(required.issubset(self.graphs), self.graphs.keys())

    def test_required_graphs_have_nodes(self):
        for harness in ("claude", "codex", "omp",
                        "opencode", "omnigent", "pi"):
            with self.subTest(harness=harness):
                self.assertGreaterEqual(len(self.graphs[harness]["nodes"]), 1)

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

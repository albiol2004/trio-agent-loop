#!/usr/bin/env python3
"""Tests for the explicit-root registry health collector."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HEALTH_PATH = REPO_ROOT / "registry" / "health.py"


def _load_health():
    spec = importlib.util.spec_from_file_location(
        "trio_registry_health_test", HEALTH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load health module: {HEALTH_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


health = _load_health()


class HealthCollectorTests(unittest.TestCase):
    """Manifest and dangling checks must remain isolated from the real home."""

    def test_report_shape_manifest_drift_and_dangling_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agents = home / ".claude" / "agents"
            agents.mkdir(parents=True)
            matching = agents / "matching.md"
            matching.write_text("matching\n", encoding="utf-8")
            mismatch = agents / "mismatch.md"
            mismatch.write_text("changed\n", encoding="utf-8")
            orphan = agents / "orphan.md"
            orphan.write_text("orphan\n", encoding="utf-8")
            manifest = (
                f"{hashlib.sha256(matching.read_bytes()).hexdigest()}  "
                "matching.md\n"
                f"{'0' * 64}  mismatch.md\n"
            )
            (agents / ".trio-hashes").write_text(manifest, encoding="utf-8")
            empty = home / ".claude" / "skills" / "__registry_import_smoke__"
            empty.mkdir(parents=True)

            # Collection must use the supplied home rather than asking
            # pathlib for another one.
            with patch.object(health.Path, "home",
                              side_effect=AssertionError("Path.home used")):
                report = health.collect_health(REPO_ROOT, home=home)

        self.assertEqual(
            set(report),
            {"lineage", "manifests", "installed_harnesses",
             "generate_check", "dangling"},
        )
        self.assertTrue(report["lineage"])
        target_paths = {
            hop["path"]
            for row in report["lineage"]
            for hop in row["hops"]
        }
        self.assertIn(str((REPO_ROOT / "pi/extensions/trio.ts").resolve()),
                      target_paths)
        self.assertIn("exit_code", report["generate_check"])
        manifest = next(
            item for item in report["manifests"]
            if item["directory"] == str(agents.resolve())
        )
        self.assertTrue(manifest["present"])
        self.assertEqual(manifest["status"], "drift")
        entries = {item["file"]: item["status"] for item in manifest["entries"]}
        self.assertEqual(entries["matching.md"], "match")
        self.assertEqual(entries["mismatch.md"], "mismatch")
        dangling = {item["path"]: item for item in report["dangling"]}
        self.assertEqual(dangling[str(empty)], {
            "path": str(empty),
            "kind": "empty-dir",
            "deletable": False,
        })
        self.assertEqual(dangling[str(orphan)], {
            "path": str(orphan),
            "kind": "orphan-file",
            "deletable": True,
        })

    def test_missing_generator_check_is_reported_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = health.collect_health(Path(tmp), home=Path(tmp))
        check = report["generate_check"]
        self.assertIsInstance(check["exit_code"], int)
        self.assertNotEqual(check["exit_code"], 0)
        self.assertFalse(check["timed_out"])
        self.assertTrue(check["stderr"])


if __name__ == "__main__":
    unittest.main()

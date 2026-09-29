"""launcher.workflow_script reports the saved `trio-native` script Claude
Code will actually run: a project-scope `<repo>/.claude/workflows/
trio-native.js` shadows the user-scope `$CLAUDE_CONFIG_DIR/workflows/
trio-native.js` (docs: "If a project workflow and a personal workflow share
a name, the project one runs"). The same fields reach the printed result's
launcher, `.native-result.json` and the run registry (probe4 side finding).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from test_dash_records import NATIVE, registry_file, run_launch
from test_launch import box  # noqa: F401  (pytest fixture)

RELEASE_JS = NATIVE / "trio-native.js"
FIELDS = ("workflow_script", "workflow_script_scope", "workflow_script_is_release",
          "workflow_script_sha256", "workflow_script_candidates")
SHIPPED = '```json\n{"status": "shipped", "verdict": "SHIP", "iteration": 1}\n```'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(box: Path, data: bytes) -> Path:
    path = box.parent / ".claude" / "workflows" / "trio-native.js"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


def _user(data: bytes) -> Path:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "workflows" / "trio-native.js"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


def _records(box: Path, tmp_path: Path, runs: Path) -> tuple[dict, dict, dict]:
    proc = run_launch(box, tmp_path, SHIPPED)
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout)["launcher"]
    result = json.loads((box / ".native-result.json").read_text())
    reg = json.loads(registry_file(runs, box).read_text())
    for field in FIELDS:  # one truth in all three places
        assert result[field] == printed[field] == reg[field], field
    # records written by native-dash readers keep their keys
    assert result["status"] == "shipped" and reg["state"] == "finished"
    return printed, result, reg


def _scopes(facts: dict) -> list[tuple[str, bool]]:
    return [(c["scope"], c["exists"]) for c in facts["workflow_script_candidates"]]


def test_project_only(box: Path, tmp_path: Path, _isolated_native_registry: Path) -> None:
    data = RELEASE_JS.read_bytes()
    proj = _project(box, data)
    facts, _, _ = _records(box, tmp_path, _isolated_native_registry)
    assert facts["workflow_script"] == os.path.realpath(proj)
    assert facts["workflow_script_scope"] == "project"
    assert facts["workflow_script_is_release"] is True
    assert facts["workflow_script_sha256"] == _sha(data)
    assert _scopes(facts) == [("project", True), ("user", False)]


def test_user_only(box: Path, tmp_path: Path, _isolated_native_registry: Path) -> None:
    user = _user(b"export const meta = {name: 'trio-native'}\n// some other release\n")
    facts, _, _ = _records(box, tmp_path, _isolated_native_registry)
    assert facts["workflow_script"] == os.path.realpath(user)
    assert facts["workflow_script_scope"] == "user"
    assert facts["workflow_script_is_release"] is False
    assert facts["workflow_script_sha256"] == _sha(user.read_bytes())
    assert _scopes(facts) == [("project", False), ("user", True)]


def test_user_symlink_to_release_is_release(box: Path, tmp_path: Path,
                                            _isolated_native_registry: Path) -> None:
    link = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "workflows" / "trio-native.js"
    link.parent.mkdir(parents=True)
    link.symlink_to(RELEASE_JS)  # README "Install (user scope)"
    facts, _, _ = _records(box, tmp_path, _isolated_native_registry)
    assert facts["workflow_script"] == str(RELEASE_JS.resolve())
    assert facts["workflow_script_scope"] == "user"
    assert facts["workflow_script_is_release"] is True


@pytest.mark.parametrize("project_is_release", [True, False])
def test_both_project_wins(box: Path, tmp_path: Path, _isolated_native_registry: Path,
                           project_is_release: bool) -> None:
    release = RELEASE_JS.read_bytes()
    stale = b"export const meta = {name: 'trio-native'}\n// stale copy\n"
    proj = _project(box, release if project_is_release else stale)
    user = _user(stale if project_is_release else release)
    facts, _, _ = _records(box, tmp_path, _isolated_native_registry)
    assert facts["workflow_script"] == os.path.realpath(proj)
    assert facts["workflow_script_scope"] == "project"
    assert facts["workflow_script_is_release"] is project_is_release
    assert _scopes(facts) == [("project", True), ("user", True)]
    shas = [c["sha256"] for c in facts["workflow_script_candidates"]]
    assert shas == [_sha(proj.read_bytes()), _sha(user.read_bytes())]


def test_neither(box: Path, tmp_path: Path, _isolated_native_registry: Path) -> None:
    facts, _, _ = _records(box, tmp_path, _isolated_native_registry)
    assert facts["workflow_script"] is None
    assert facts["workflow_script_scope"] == "none"
    assert facts["workflow_script_is_release"] is False
    assert facts["workflow_script_sha256"] is None
    assert _scopes(facts) == [("project", False), ("user", False)]


def test_not_started_outcome_carries_fields(box: Path, tmp_path: Path) -> None:
    _project(box, RELEASE_JS.read_bytes())
    proc = run_launch(box, tmp_path, "no json", no_begin=True)
    assert proc.returncode == 3
    out = json.loads(proc.stdout)["launcher"]
    assert out["workflow_script_scope"] == "project"
    assert out["workflow_script_is_release"] is True
    assert not (box / ".native-result.json").exists()

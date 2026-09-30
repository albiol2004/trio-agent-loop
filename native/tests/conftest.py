"""Keep every native test off the real per-user run registry and Claude dir."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_native_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runs = tmp_path / "native-runs-registry"
    monkeypatch.setenv("TRIO_NATIVE_RUNS_DIR", str(runs))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    return runs

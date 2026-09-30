"""Shared test defaults for the registry / dashboard tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Keep worktree roots, root-free Lead records and acceptance state off
    the real ``${XDG_STATE_HOME:-~/.local/state}/trio-agent-loop`` (r20: two
    driver tests leaked ACTIVE Lead records there). A test that needs its
    own location still sets XDG_STATE_HOME / TRIO_WORKTREE_ROOT itself."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)

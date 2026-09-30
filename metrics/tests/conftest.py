"""Shared test defaults for the Trio loop driver."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_retirement_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SHIP-without-retirement checks immediate by default.

    The driver's bounded post-SHIP retirement wait is covered by tests
    that opt in explicitly; everything else keeps the historical
    immediate verdict so no suite sleeps on a real clock.
    """
    monkeypatch.setenv("TRIO_RETIREMENT_WAIT_SECONDS", "0")



@pytest.fixture(autouse=True)
def _isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Keep worktree roots, root-free Lead records and acceptance state off
    the real ``${XDG_STATE_HOME:-~/.local/state}/trio-agent-loop`` (r20: two
    driver tests leaked ACTIVE Lead records there). A test that needs its
    own location still sets XDG_STATE_HOME / TRIO_WORKTREE_ROOT itself."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)

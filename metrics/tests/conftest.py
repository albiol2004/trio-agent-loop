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
def _root_turn_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """r15.x root-turn locks (trioctl-driven tests) go to a scratch dir."""
    monkeypatch.setenv(
        "TRIO_ROOT_TURN_LOCK_DIR", str(tmp_path_factory.mktemp("root-turn"))
    )
    # A test that leaks a turn fails in a minute instead of hanging 3900 s.
    monkeypatch.setenv("TRIO_ROOT_TURN_WAIT_S", "60")

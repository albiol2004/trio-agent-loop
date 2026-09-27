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
def _same_cwd_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """r14 S-1 per-workspace create locks go to a scratch dir, never ~/.local."""
    monkeypatch.setenv(
        "TRIO_SAME_CWD_LOCK_DIR", str(tmp_path_factory.mktemp("cwd-locks"))
    )

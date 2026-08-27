"""Version output test for metrics/trio-check.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).parents[1] / "trio-check.py"


def test_version_output() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "trio-check 1.0.0"

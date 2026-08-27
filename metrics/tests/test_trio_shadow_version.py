"""Version output test for metrics/trio-shadow.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SHADOW = Path(__file__).parents[1] / "trio-shadow.py"


def test_version_output() -> None:
    result = subprocess.run(
        [sys.executable, str(SHADOW), "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "trio-shadow 1.0.0"

"""Installs ``fake_opencode.py`` on ``PATH`` as ``opencode`` for a test."""
from __future__ import annotations

import os
import stat
from pathlib import Path

_FAKE_SRC = Path(__file__).resolve().parent / "fake_opencode.py"


def install_fake(tmp_path: Path, scenario_path: str | Path | None = None) -> dict[str, str]:
    """Creates ``<tmp_path>/fakebin/opencode`` (a copy of ``fake_opencode.py``,
    executable) and returns an environment with ``PATH`` prefixed with it,
    plus ``FAKE_OC_STATE`` (a fresh per-test state dir) and, if given,
    ``FAKE_OC_SCENARIO`` pointing at ``scenario_path``."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "opencode"
    target.write_text(_FAKE_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    state_dir = tmp_path / "fakestate"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_OC_STATE"] = str(state_dir)
    if scenario_path is not None:
        env["FAKE_OC_SCENARIO"] = str(scenario_path)
    else:
        env.pop("FAKE_OC_SCENARIO", None)
    return env

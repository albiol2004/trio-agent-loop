"""r20 review F6: native v0.1 does not implement frozen acceptance. launch.sh
warns loudly and records `acceptance: unsupported-in-native-v01` in
.native-result.json when the mailbox has a frozen pack or the switch
resolves ON (TRIO_ACCEPTANCE, else the profile); otherwise nothing changes."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from test_launch import box, launch  # noqa: F401  (fixture + fake-claude runner)

BODY = '```json\n{"status": "shipped", "code": 0}\n```'
TAG = "unsupported-in-native-v01"


@pytest.fixture(autouse=True)
def _clean_switch(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _profile(tmp_path: Path, text: str) -> None:
    path = tmp_path / "xdg-config" / "trio-agent-loop" / "omnigent.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _result(box: Path) -> dict:
    return json.loads((box / ".native-result.json").read_text())


def test_no_acceptance_no_warning_no_field(box: Path, tmp_path: Path) -> None:
    _profile(tmp_path, "[acceptance]\nenabled = false\n")
    proc, _ = launch(box, tmp_path, "start", result=BODY)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" not in proc.stderr
    assert "acceptance" not in _result(box)
    assert "acceptance" not in json.loads(proc.stdout)["launcher"]


@pytest.mark.parametrize("how", ["frozen-pack", "env", "profile"])
def test_acceptance_detected_warns_and_records(box: Path, tmp_path: Path, monkeypatch,
                                               how: str) -> None:
    if how == "frozen-pack":
        (box / "acceptance").mkdir()
        (box / "acceptance" / "FROZEN").write_text("pin: x\n")
        want = "frozen pack: acceptance/FROZEN"
    elif how == "env":
        monkeypatch.setenv("TRIO_ACCEPTANCE", "1")
        want = "TRIO_ACCEPTANCE=1"
    else:
        _profile(tmp_path, "[acceptance]\nenabled = true\nwait_s = 900\n")
        want = "profile: [acceptance] enabled = true"
    proc, calls = launch(box, tmp_path, "start", result=BODY)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1  # the run itself proceeds unchanged
    assert "frozen acceptance is NOT supported by the native v0.1 driver" in proc.stderr
    assert want in proc.stderr and "trioctl omnigent loop --acceptance" in proc.stderr
    rec = _result(box)
    assert rec["acceptance"] == TAG and rec["status"] == "shipped"
    assert any(want in d for d in rec["acceptance_detected"])
    out = json.loads(proc.stdout)
    assert out["launcher"]["acceptance"] == TAG


def test_env_off_overrides_the_profile(box: Path, tmp_path: Path, monkeypatch) -> None:
    _profile(tmp_path, "[acceptance]\nenabled = true\n")
    monkeypatch.setenv("TRIO_ACCEPTANCE", "0")
    proc, _ = launch(box, tmp_path, "start", result=BODY)
    assert proc.returncode == 0 and "WARNING" not in proc.stderr
    assert "acceptance" not in _result(box)


def test_frozen_pack_warns_on_resume_too(box: Path, tmp_path: Path) -> None:
    launch(box, tmp_path, "start", result='```json\n{"status": "held"}\n```')
    (box / "acceptance").mkdir()
    (box / "acceptance" / "FROZEN").write_text("pin: x\n")
    proc, _ = launch(box, tmp_path, "resume", "--run-id", "wf_abc", result=BODY)
    assert proc.returncode == 0, proc.stderr
    assert "NOT supported by the native v0.1 driver" in proc.stderr
    assert _result(box)["acceptance"] == TAG


def test_launcher_never_edits_helper_or_workflow_script() -> None:
    """F6 stays inside launch.sh (r19-native owns the helper and the script)."""
    native = Path(__file__).resolve().parents[1]
    assert TAG in (native / "launch.sh").read_text()
    for name in ("trio_native_step.py", "trio-native.js"):
        assert TAG not in (native / name).read_text()

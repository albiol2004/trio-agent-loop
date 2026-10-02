"""r19: launch.sh --acceptance and the additive acceptance keys of the
dashboard records (.native-result.json, run registry, trio-dash state)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from test_launch import box, launch  # noqa: F401 (fixture)

ACC = {"enabled": True, "status": "frozen", "pin": "p" * 64, "freeze_commit": "c" * 40,
       "checks": 6, "ship_gate": {"passed": 6, "failed": 0, "unavailable": 0, "total": 6},
       "coverage_refusals": [], "replanned": False}


def test_start_acceptance_reaches_args_and_result(box: Path, tmp_path: Path) -> None:
    body = "```json\n" + json.dumps({"status": "shipped", "code": 0, "acceptance": ACC}) + "\n```"
    proc, calls = launch(box, tmp_path, "start", "--acceptance", result=body)
    assert proc.returncode == 0, proc.stderr
    prompt = calls[0]["argv"][calls[0]["argv"].index("-p") + 1]
    assert '"acceptance":true' in prompt
    record = json.loads((box / ".native-launch.json").read_text())
    assert json.loads(record["args"])["acceptance"] is True
    result = json.loads((box / ".native-result.json").read_text())
    assert result["acceptance"] == ACC
    reg = list((Path(tmp_path) / "native-runs-registry").glob("*.json"))
    assert reg and json.loads(reg[0].read_text())["acceptance"]["pin"] == ACC["pin"]


def test_start_without_acceptance_is_unchanged(box: Path, tmp_path: Path) -> None:
    body = '```json\n{"status": "shipped", "code": 0}\n```'
    proc, calls = launch(box, tmp_path, "start", result=body)
    assert proc.returncode == 0
    prompt = calls[0]["argv"][calls[0]["argv"].index("-p") + 1]
    assert '"acceptance"' not in prompt
    assert "acceptance" not in json.loads((box / ".native-result.json").read_text())


def test_resume_refuses_the_acceptance_flag(box: Path, tmp_path: Path) -> None:
    proc, calls = launch(box, tmp_path, "resume", "--run-id", "wf_x", "--acceptance", result="")
    assert proc.returncode == 2 and "start flag" in proc.stderr and not calls


def _loop_actions():
    path = Path(__file__).resolve().parents[2] / "dashboard" / "loop_actions.py"
    spec = importlib.util.spec_from_file_location("r19_loop_actions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dash_state_carries_acceptance_detail(tmp_path: Path) -> None:
    la = _loop_actions()
    fake = {"session": {"driver": "claude-workflow", "done": True},
            "result": {"source": "launcher", "status": "shipped", "acceptance": ACC,
                       "session_id": "s"}}
    detail = la.native_acceptance_detail(fake)
    assert detail == {"pin": ACC["pin"][:12], "checks": 6, "ship_gate": "6/6 PASS",
                      "coverage_refusals": 0, "ship_refused": 0, "tamper_events": 0,
                      "amendments": 0, "status": "frozen", "audit_limited": None}
    # eval-r19n finding 6: the limited-audit fact reaches the drawer
    limited = dict(fake, result=dict(fake["result"], acceptance=dict(
        ACC, audit={"limited": True, "contaminated": False, "attempts": 1})))
    assert la.native_acceptance_detail(limited)["audit_limited"] is True
    assert la.native_acceptance_detail({"result": {"status": "shipped"}}) is None

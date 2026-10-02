"""r19 N3/N4: trio-native.js with args.acceptance (node harness), and the
switch-off identity of the script against d31f749."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from test_workflow_script import HARNESS, MAILBOX, NATIVE, NODE, SCRIPT, needs_node, run, seq

ACC = {"mailbox": MAILBOX, "acceptance": True}


def ops(out: dict) -> list[str]:
    return seq(out)


def step_prompts(out: dict, op: str) -> list[str]:
    return [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-step"
            and re.search(rf"op={re.escape(op)},", c["prompt"])]


@needs_node
def test_author_then_lead_then_coverage_then_builders() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC)})
    names = ops(out)
    assert names[:12] == ["begin", "next", "acceptance-export", "acceptance", "acceptance-freeze",
                          "lead", "coverage", "dispatch", "builder", "builders", "lead", "cleanup"], names
    assert names.index("acceptance-freeze") < names.index("lead") < names.index("coverage") \
        < names.index("dispatch") < names.index("builder")
    assert "acceptance-run" in names and names.index("pin") < names.index("acceptance-run") \
        < names.index("evaluator") < names.index("apply")
    r = out["result"]
    assert r["status"] == "shipped" and r["acceptance"]["enabled"] is True
    assert r["acceptance"]["pin"] == "PIN0123456789"


@needs_node
def test_author_call_is_isolated_from_the_repo_and_on_the_evaluator_tier() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC)})
    author = next(c for c in out["calls"] if c["agentType"] == "trio-acceptance")
    assert author["model"] == "opus" and author["effort"] == "high"
    assert author["isolation"] is None and author["schemaKeys"] == ["checks", "summary", "denials"]
    p = author["prompt"]
    assert p.startswith("ACCEPTANCE-AUTHOR-RUN: EXEC-1-a1")
    assert "/state/loop-x/export" in p and "validate --export ." in p
    assert MAILBOX not in p and "/repo" not in p  # never the mailbox or the repo path
    assert "PLAN" not in p
    begin = step_prompts(out, "begin")[0]
    assert "--acceptance '1'" in begin
    assert '"acceptance":"opus"' in begin


@needs_node
def test_evaluator_override_moves_the_author_with_it() -> None:
    args = dict(ACC, models={"evaluator": "claude-sonnet-5"})
    out = run({"verdicts": ["SHIP"], "args": args})
    begin = step_prompts(out, "begin")[0]
    # the tier check is the helper's; the script asks with the moved author
    assert '"evaluator":"claude-sonnet-5","acceptance":"claude-sonnet-5"' in begin


@needs_node
def test_tier_refusal_at_begin_stops_before_any_role() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "acc": {"begin_stop": True}})
    assert ops(out) == ["begin", "end"]
    r = out["result"]
    assert r["status"] == "needs_human" and "acceptance-goal-changed" in r["reason"]


@needs_node
def test_plan_schema_and_prompt_carry_the_mapping() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC)})
    plan = next(c for c in out["calls"] if c["agentType"] == "trio-lead")
    schema = json.loads(plan["schemaJson"])
    assert "covers" in schema["properties"]["slices"]["items"]["properties"]
    assert "lead_integration" in schema["properties"]
    assert "acceptance_bindings" in schema["properties"]
    assert "FROZEN ACCEPTANCE (r19; this loop runs with args.acceptance on)" in plan["prompt"]
    assert f"{MAILBOX}/acceptance/MANIFEST.json" in plan["prompt"]
    assert "Frozen pack:" in plan["prompt"] and "pin PIN012345678" in plan["prompt"]
    cov = step_prompts(out, "coverage")[0]
    assert '"covers":["ACC-01"]' in cov and '"lead_integration":["ACC-02"]' in cov
    builder = next(c for c in out["calls"] if c["agentType"] == "trio-builder")
    assert "## Acceptance (frozen; do not edit)" in builder["prompt"]
    ev = next(c for c in out["calls"] if c["agentType"] == "trio-evaluator")
    assert "FROZEN ACCEPTANCE @sha1: 5/6 PASS" in ev["prompt"]
    assert "FROZEN ACCEPTANCE (r19; whole-goal verdict)" in ev["prompt"]
    assert "/rel/metrics/trio-acceptance.py run --mailbox" in ev["prompt"]
    integ = [c for c in out["calls"] if c["agentType"] == "trio-lead"][1]
    assert "## Frozen acceptance (Lead run)" in integ["prompt"]


@needs_node
def test_coverage_refusal_replans_once_before_any_builder() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "acc": {"coverage": [False, True]}})
    names = ops(out)
    i = names.index("coverage")
    assert names[i:i + 4] == ["coverage", "lead", "coverage", "dispatch"], names
    assert names.index("builder") > names.index("dispatch")
    replan = [c for c in out["calls"] if c["agentType"] == "trio-lead"][1]
    assert "COVERAGE REFUSED (re-plan, attempt 2 of 2)" in replan["prompt"]
    assert "ACC-02" in replan["prompt"]
    assert "--attempt '2'" in step_prompts(out, "coverage")[1]
    r = out["result"]
    assert r["status"] == "shipped" and r["acceptance"]["replanned"] is True


@needs_node
def test_second_coverage_refusal_stops_with_no_builder() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "acc": {"coverage": [False, False]}})
    names = ops(out)
    assert "dispatch" not in names and "builder" not in names
    assert names[-1] == "end"
    r = out["result"]
    assert r["status"] == "error" and "acceptance-coverage" in r["reason"]
    assert len(r["acceptance"]["coverage_refusals"]) == 2


@needs_node
def test_solo_pass_also_waits_for_coverage() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "plan": [], "acc": {"coverage": [False, False]}})
    names = ops(out)
    assert names.count("lead") == 2 and "gate" not in names  # plan + re-plan, no solo pass


@needs_node
def test_author_retry_and_reauthor_paths() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "acc": {"freeze": ["reauthor", "retry", "frozen"]}})
    names = ops(out)
    assert names.count("acceptance") == 3 and names.count("acceptance-freeze") == 3
    authors = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-acceptance"]
    assert authors[1].startswith("YOUR PREVIOUS ATTEMPT WAS DISCARDED")
    assert "RETRY: the driver's validation at base" in authors[2] and "ACC-02: passes-at-base" in authors[2]
    freezes = step_prompts(out, "acceptance-freeze")
    assert '"contaminated":false,"retried":false' in freezes[0]
    assert '"contaminated":true,"retried":false' in freezes[1]
    assert '"contaminated":true,"retried":true' in freezes[2]
    assert out["result"]["acceptance"]["author_attempts"] == 3


@needs_node
def test_contaminated_twice_stops() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC), "acc": {"freeze": ["reauthor", "stop"]}})
    assert "lead" not in ops(out)
    r = out["result"]
    assert r["status"] == "error" and "acceptance-contaminated" in r["reason"]


@needs_node
def test_pending_long_ops_are_polled() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC),
               "acc": {"pending": {"acceptance-freeze": 2, "acceptance-run": 1, "apply": 1}}})
    names = ops(out)
    assert names.count("acceptance-freeze") == 3 and names.count("acceptance-run") == 2
    assert names.count("apply") == 2
    assert "--poll '1'" in step_prompts(out, "acceptance-freeze")[1]
    assert out["result"]["status"] == "shipped"


@needs_node
def test_ship_refused_by_the_gate_iterates_with_the_failures() -> None:
    out = run({"verdicts": ["SHIP", "SHIP"], "args": dict(ACC),
               "acc": {"refuse_ship": [0], "errors": ["The driver refused the SHIP: ACC-04 FAIL"]}})
    r = out["result"]
    assert r["status"] == "shipped" and len(r["iterations"]) == 2
    assert r["iterations"][0]["acceptance"]["ship_refused"] is True
    assert r["acceptance"]["ship_refused"] == [{"iteration": 1, "became": "ITERATE"}]
    plans = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-lead" and c["schemaKeys"]
             and "slices" in c["schemaKeys"]]
    assert "ACCEPTANCE ERRORS FROM THE DRIVER" in plans[1] and "ACC-04 FAIL" in plans[1]
    assert ops(out).count("acceptance") == 1  # authored once per loop


@needs_node
def test_every_acceptance_op_carries_the_digest_once_frozen() -> None:
    out = run({"verdicts": ["SHIP"], "args": dict(ACC)})
    for op in ("coverage", "dispatch", "gate", "acceptance-run", "apply"):
        p = step_prompts(out, op)[0]
        assert "--acceptance '1'" in p and '"pin":"PIN0123456789"' in p, (op, p)
    assert "--acceptance" not in step_prompts(out, "pin")[0]


# ------------------------------------------------------ switch-off identity
SCENARIOS = [
    {"verdicts": ["SHIP"]},
    {"verdicts": ["ITERATE", "SHIP"]},
    {"verdicts": ["ITERATE scope=local:app.py", "NEEDS_HUMAN"]},
    {"verdicts": ["SHIP"], "gates": [False, True]},
    {"verdicts": ["SHIP"], "conflicts": {"b": ["shared.py"]},
     "plan": [{"id": "a", "brief": "a", "writes": ["a.py"]}, {"id": "b", "brief": "b", "writes": ["b.py"]}]},
    {"verdicts": ["SHIP"], "plan": []},
    {"verdicts": ["SHIP"], "refuse_ids": ["app"]},
    {"verdicts": ["BLOCKED"], "human": {"answer": "## Verified human answer (driver)\nyes", "notes": []}},
    {"verdicts": ["SHIP"], "args": {"max_agents": 6}},
    {"verdicts": ["SHIP"], "args": {"acceptance": False}},
]


def _run_script(script: Path, scenario: dict) -> dict:
    scenario = json.loads(json.dumps(scenario))
    scenario.setdefault("args", {})
    scenario["args"].setdefault("mailbox", MAILBOX)
    proc = subprocess.run([NODE, str(HARNESS), str(script), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[str(i) for i in range(len(SCENARIOS))])
def test_switch_off_journal_identical_to_d31f749(tmp_path: Path, scenario: dict) -> None:
    top = NATIVE.parent
    old = subprocess.run(["git", "-C", str(top), "show", "d31f749:native/trio-native.js"],
                         capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("d31f749 is not in this checkout's history")
    old_script = tmp_path / "trio-native-d31f749.js"
    old_script.write_text(old.stdout, encoding="utf-8")
    a = _run_script(old_script, scenario)
    b = _run_script(SCRIPT, scenario)
    # the journal: every agent() call with its prompt and options, in order
    assert b["calls"] == a["calls"]
    assert b["result"] == a["result"]
    assert b["logs"] == a["logs"]
    assert "acceptance" not in b["result"]

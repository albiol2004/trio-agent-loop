"""Static checks and a stubbed-runtime unit harness for trio-native.js."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parents[1]
SCRIPT = NATIVE / "trio-native.js"
HARNESS = Path(__file__).resolve().with_name("wf_harness.mjs")
SRC = SCRIPT.read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
MAILBOX = "/work/product/loop"


def _meta_text() -> str:
    match = re.search(r"^export const meta = (\{.*?\n\})", SRC, re.S | re.M)
    assert match, "script must begin with export const meta = {...}"
    return match.group(1)


# ------------------------------------------------------------------ static
def test_script_starts_with_meta() -> None:
    first = next(ln for ln in SRC.splitlines() if ln.strip())
    assert first.startswith("export const meta = {")


def test_meta_is_a_pure_literal() -> None:
    meta = _meta_text()
    assert "${" not in meta and "`" not in meta and "..." not in meta
    # no identifiers used as values (variables / calls): every value is a
    # quoted string, a nested literal, or an array of literals.
    assert not re.search(r":\s*[A-Za-z_$][\w$]*\s*[,(\n}]", meta), meta
    assert not re.search(r"\w\s*\(", re.sub(r"'[^']*'", "''", meta))
    for key in ("name:", "description:", "phases:"):
        assert key in meta
    titles = re.findall(r"title:\s*'([^']+)'", meta)
    called = re.findall(r"\bphase\('([^']+)'\)", SRC)
    assert titles == ["Begin", "Iterate", "Finish"]
    assert set(called) == set(titles)


def test_no_nondeterministic_builtins() -> None:
    code = re.sub(r"//.*", "", SRC)
    assert "Date.now" not in code and "Math.random" not in code
    assert not re.search(r"new Date\(\s*\)", code)
    assert "require(" not in code and "import " not in code


def test_every_agent_call_carries_model_and_type() -> None:
    calls = [m.start() for m in re.finditer(r"\bagent\(", SRC)]
    assert len(calls) == 2  # step() and runAgentTwice()
    step_call = SRC[calls[0]:calls[0] + 400]
    assert "agentType: 'trio-step'" in step_call
    assert "model: MODELS.step" in step_call
    assert "schema: STEP_SCHEMA" in step_call
    assert "effort: 'low'" in step_call
    # role agents: runAgentTwice gets agentType + model at every call site
    sites = re.findall(r"runAgentTwice\([^)]*\{\s*(.*?)\}\)", SRC, re.S)
    assert len(sites) == 2
    for site in sites:
        assert "agentType:" in site and "model:" in site
    assert "lead: 'claude-opus-5-5'" in SRC
    assert "evaluator: 'claude-opus-5-5'" in SRC
    assert "repair: 'claude-sonnet-5'" in SRC
    assert "step: 'claude-sonnet-5'" in SRC


def test_max_agents_enforced_in_spend() -> None:
    assert re.search(r"function spend\(", SRC)
    assert "agentsUsed >= limit" in SRC
    # every agent() path goes through spend()
    for fn in ("async function step(", "async function runAgentTwice("):
        start = SRC.index(fn)
        chunk = SRC[start:SRC.index("agent(prompt", start)]
        assert "spend(" in chunk


def test_step_agent_definition() -> None:
    text = (NATIVE / "agents" / "trio-step.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert re.search(r"^name: trio-step$", front, re.M)
    assert re.search(r"^tools: Bash$", front, re.M)
    assert "exactly one" in text.lower() or "exactly one" in text


@needs_node
def test_node_check_parses_wrapped_script(tmp_path: Path) -> None:
    body = SRC.replace("export const meta =", "const meta =", 1)
    wrapped = tmp_path / "wrapped.js"
    wrapped.write_text(
        "async function __workflow(agent, parallel, pipeline, phase, log, "
        "args, budget, workflow) {\n" + body + "\n}\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, "--check", str(wrapped)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ----------------------------------------------------------------- harness
def run(scenario: dict) -> dict:
    scenario.setdefault("args", {})
    scenario["args"].setdefault("mailbox", MAILBOX)
    proc = subprocess.run(
        [NODE, str(HARNESS), str(SCRIPT), json.dumps(scenario)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def seq(out: dict) -> list[str]:
    names = []
    for c in out["calls"]:
        if c["agentType"] == "trio-step":
            names.append(re.search(r"op=(\w+)", c["prompt"]).group(1))
        else:
            names.append(c["agentType"].replace("trio-", ""))
    return names


@needs_node
def test_harness_ship() -> None:
    out = run({"verdicts": ["SHIP"]})
    r = out["result"]
    assert r["status"] == "shipped" and r["verdict"] == "SHIP"
    assert r["commit_shas"] == ["c0ffee"] and r["lock"] == "released"
    assert seq(out) == ["begin", "next", "lead", "gate", "pin", "evaluator",
                        "apply", "end"]
    assert r["agents_used"] == 8
    roles = {c["agentType"]: c for c in out["calls"]}
    assert roles["trio-lead"]["model"] == "claude-opus-5-5"
    assert roles["trio-evaluator"]["model"] == "claude-opus-5-5"
    assert roles["trio-step"]["model"] == "claude-sonnet-5"
    assert all(c["schema"] for c in out["calls"] if c["agentType"] == "trio-step")
    lead = roles["trio-lead"]["prompt"]
    assert 'isolation: "worktree"' in lead and "git merge" in lead
    assert "does NOT apply inside this role" in lead
    ev = roles["trio-evaluator"]["prompt"]
    assert ev.startswith("LOCKSTEP CONTEXT: attempt=att1 sha=sha1")
    assert "attempt: att1" in ev and "evaluated: sha1" in ev
    step_cmds = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-step"]
    assert all(f"--mailbox '{MAILBOX}'" in p for p in step_cmds)
    assert all("trio_native_step.py" in p for p in step_cmds)


@needs_node
def test_harness_lead_prompt_builder_worktree_contract() -> None:
    """F6: absolute mailbox LOG path, no loop/ commits, exact merge steps."""
    out = run({"verdicts": ["SHIP"]})
    lead = next(c for c in out["calls"] if c["agentType"] == "trio-lead")
    p = lead["prompt"]
    assert f"absolute mailbox path `{MAILBOX}/LOG.md`" in p
    assert "never to a `loop/LOG.md` inside your worktree" in p
    assert "Never edit, `git add` or commit anything under `loop/`" in p
    assert "`git merge --no-ff --no-edit <builder branch>`" in p
    assert "from your own checkout, on your branch" in p
    assert "If the merge conflicts, run `git merge --abort`, stop dispatching, and report" in p
    assert "After a branch is merged, remove its worktree (`git worktree remove <path>`)" in p
    assert "`git branch -d <builder branch>`" in p
    assert 'isolation: "worktree"' in p


@needs_node
def test_harness_iterate_then_ship() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"]})
    r = out["result"]
    assert r["status"] == "shipped" and r["iteration"] == 2
    assert seq(out) == ["begin", "next", "lead", "gate", "pin", "evaluator",
                        "apply", "next", "lead", "gate", "pin", "evaluator",
                        "apply", "end"]
    assert [i["verdict"] for i in r["iterations"]] == ["ITERATE", "SHIP"]


@needs_node
def test_harness_scoped_iterate_runs_repair_on_sonnet() -> None:
    out = run({"verdicts": ["ITERATE scope=local:app.py", "SHIP"],
               "args": {"max_agents": 20}})
    assert seq(out)[7:10] == ["next", "repair", "gate"]
    repair = next(c for c in out["calls"] if c["agentType"] == "trio-repair")
    assert repair["model"] == "claude-sonnet-5"
    assert "| repair |" in repair["prompt"]
    assert "scope=local:app.py" in repair["prompt"]


@needs_node
def test_harness_gate_fail_retries_lead_once() -> None:
    out = run({"verdicts": ["SHIP"], "gates": [False, True]})
    assert out["result"]["status"] == "shipped"
    assert seq(out)[:6] == ["begin", "next", "lead", "gate", "lead", "gate"]
    retry = [c for c in out["calls"] if c["agentType"] == "trio-lead"][1]
    assert "RETRY (attempt 2 of 2)" in retry["prompt"]
    gates = [c["prompt"] for c in out["calls"] if "op=gate" in c["prompt"]]
    assert "--attempt '1'" in gates[0] and "--attempt '2'" in gates[1]


@needs_node
def test_harness_gate_fail_twice_stops_with_error() -> None:
    out = run({"verdicts": ["SHIP"], "gates": [False, False]})
    r = out["result"]
    assert r["status"] == "error" and "gate breach" in r["reason"]
    assert seq(out) == ["begin", "next", "lead", "gate", "lead", "gate", "end"]


@needs_node
def test_harness_max_agents_exhaustion_keeps_end() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"], "args": {"max_agents": 9}})
    r = out["result"]
    assert r["status"] == "budget" and "max_agents=9" in r["reason"]
    assert r["agents_used"] == 9 and r["lock"] == "released"
    assert seq(out) == ["begin", "next", "lead", "gate", "pin", "evaluator",
                        "apply", "next", "end"]


@needs_node
def test_harness_nonce_mismatch_reruns_step() -> None:
    out = run({"verdicts": ["SHIP"], "wrong_nonce_once": True})
    assert out["result"]["status"] == "shipped"
    assert seq(out)[:3] == ["begin", "begin", "next"]
    assert any("nonce mismatch" in line for line in out["logs"])


@needs_node
@pytest.mark.parametrize("word,status", [("NEEDS_HUMAN", "needs_human"),
                                         ("BLOCKED", "blocked")])
def test_harness_terminal_verdicts_stop(word: str, status: str) -> None:
    out = run({"verdicts": [word]})
    assert out["result"]["status"] == status
    assert seq(out)[-2:] == ["apply", "end"]


@needs_node
def test_harness_role_agent_death_stops_resumable() -> None:
    out = run({"verdicts": ["SHIP"], "die": ["evaluator"]})
    r = out["result"]
    assert r["status"] == "error" and "evaluator agent failed" in r["reason"]
    assert seq(out)[-3:] == ["evaluator", "evaluator", "end"]


@needs_node
def test_harness_meta_evaluates_and_rejects_bad_args() -> None:
    out = run({"verdicts": ["SHIP"]})
    assert out["meta"]["name"] == "trio-native"
    proc = subprocess.run(
        [NODE, str(HARNESS), str(SCRIPT),
         json.dumps({"verdicts": [], "args": {"mailbox": "relative/loop"}})],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0 and "absolute mailbox" in proc.stderr


# ------------------------------------------- user decisions (2026-09-29)
def test_no_bypass_permissions_anywhere_in_native() -> None:
    for path in NATIVE.rglob("*"):
        if path.is_file() and path.suffix in (".js", ".mjs", ".md", ".py"):
            if path.name == "test_workflow_script.py":
                continue
            text = path.read_text(encoding="utf-8")
            assert "bypassPermissions" not in text, path
            assert "dangerously-skip-permissions" not in text, path


def test_caps_are_opt_in() -> None:
    assert "A.max_agents : null" in SRC
    assert "A.token_budget : null" in SRC
    assert "if (MAX_AGENTS !== null)" in SRC


@needs_node
def test_harness_no_agent_cap_by_default() -> None:
    out = run({"verdicts": ["ITERATE"] * 5 + ["SHIP"],
               "args": {"max_iterations": 10}})
    r = out["result"]
    assert r["status"] == "shipped" and r["iteration"] == 6
    assert r["agents_used"] == 2 + 6 * 6 and r["max_agents"] is None


@needs_node
def test_harness_token_budget_opt_in() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"],
               "args": {"token_budget": 5000}})
    r = out["result"]
    assert r["status"] == "budget" and "token_budget=5000" in r["reason"]
    assert seq(out) == ["begin", "next", "lead", "gate", "pin", "end"]


@needs_node
@pytest.mark.parametrize("op", ["gate", "apply", "next"])
def test_harness_held_step_stops_without_retry(op: str) -> None:
    out = run({"verdicts": ["SHIP"], "held_op": op})
    r = out["result"]
    assert r["status"] == "held" and r["held_step"] == op
    assert "permission denied" in r["reason"]
    names = seq(out)
    assert names.count(op) == 1 and names[-1] == "end"
    assert names[-2] == op


@needs_node
def test_harness_held_end_is_surfaced() -> None:
    """F10: a denied `end` keeps the loop outcome but reports the hold."""
    out = run({"verdicts": ["SHIP"], "held_op": "end"})
    r = out["result"]
    assert r["status"] == "shipped" and r["lock"] == "not_released"
    assert r["held_step"] == "end"
    assert "permission denied" in r["end_error"]
    assert seq(out).count("end") == 1


@needs_node
def test_harness_released_end_has_no_end_error() -> None:
    r = run({"verdicts": ["SHIP"]})["result"]
    assert r["end_error"] is None and r["held_step"] is None

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
    assert len(calls) == 3  # step(), runAgentTwice() and the builder wave
    step_call = SRC[calls[0]:calls[0] + 400]
    assert "agentType: 'trio-step'" in step_call
    assert "model: MODELS.step" in step_call
    assert "schema: STEP_SCHEMA" in step_call
    assert "effort: 'low'" in step_call
    # role agents: runAgentTwice gets agentType + model at every call site
    sites = [m.start() for m in re.finditer(r"await runAgentTwice\(", SRC)]
    assert len(sites) == 5  # plan, solo lead, integrate, lead/repair retry, evaluator
    for start in sites:
        site = SRC[start:SRC.index("})", start)]
        assert "agentType:" in site and "model:" in site, site
    assert "lead: 'claude-opus-5-5'" in SRC
    assert "evaluator: 'claude-opus-5-5'" in SRC
    assert "repair: 'claude-sonnet-5'" in SRC
    assert "step: 'claude-sonnet-5'" in SRC
    assert "builder: 'claude-sonnet-5'" in SRC
    builder_call = SRC[calls[2]:calls[2] + 400]
    for key in ("agentType: 'trio-builder'", "model: MODELS.builder",
                "isolation: 'worktree'", "schema: BUILDER_SCHEMA"):
        assert key in builder_call


def test_max_agents_enforced_in_spend() -> None:
    assert re.search(r"function spend\(", SRC)
    assert "agentsUsed >= limit" in SRC
    # every agent() path goes through spend()
    for fn in ("async function step(", "async function runAgentTwice("):
        start = SRC.index(fn)
        chunk = SRC[start:SRC.index("agent(prompt", start)]
        assert "spend(" in chunk
    start = SRC.index("const results = await parallel(")
    assert "spend(`builder ${s.id}`)" in SRC[start - 200:start]


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


ONE_PASS = ["begin", "next", "lead", "dispatch", "builder", "builders", "lead",
            "cleanup", "gate", "pin", "evaluator", "apply"]
ITER = ONE_PASS[1:]


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
    assert seq(out) == ONE_PASS + ["end"]
    assert r["agents_used"] == 13
    roles = {c["agentType"]: c for c in out["calls"]}
    assert roles["trio-lead"]["model"] == "claude-opus-5-5"
    assert roles["trio-evaluator"]["model"] == "claude-opus-5-5"
    assert roles["trio-step"]["model"] == "claude-sonnet-5"
    assert all(c["schema"] for c in out["calls"] if c["agentType"] == "trio-step")
    lead = roles["trio-lead"]["prompt"]
    assert "does NOT apply inside this role" in lead
    assert roles["trio-builder"]["isolation"] == "worktree"
    assert roles["trio-builder"]["model"] == "claude-sonnet-5"
    ev = roles["trio-evaluator"]["prompt"]
    assert ev.startswith("LOCKSTEP CONTEXT: attempt=att1 sha=sha1")
    assert "attempt: att1" in ev and "evaluated: sha1" in ev
    step_cmds = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-step"]
    assert all(f"--mailbox '{MAILBOX}'" in p for p in step_cmds)
    assert all("trio_native_step.py" in p for p in step_cmds)


@needs_node
def test_harness_driver_owned_builder_contract() -> None:
    """Probe blocker 1 + F6: the Lead plans, the driver spawns isolated
    builders, builders never write LOG or commit loop/, the Lead merges."""
    out = run({"verdicts": ["SHIP"]})
    leads = [c for c in out["calls"] if c["agentType"] == "trio-lead"]
    plan, integ = leads[0], leads[1]
    assert plan["schemaKeys"] == ["slices", "notes"]
    assert "You have no Agent tool" in plan["prompt"]
    assert "Agent tool option" not in plan["prompt"]
    assert "Do NOT implement product code" in plan["prompt"]
    builder = next(c for c in out["calls"] if c["agentType"] == "trio-builder")
    b = builder["prompt"]
    assert builder["isolation"] == "worktree"
    assert "as `base`" in b and "The driver requires `base` = H1w1" in b
    assert "Do NOT write LOG.md" in b and "Never commit `loop/` files" in b
    assert "the driver writes your LOG line from this result" in b
    assert "ASSIGNMENT FROM THE LEAD:\nbuild app.py" in b
    i = integ["prompt"]
    assert "`git merge --no-ff --no-edit <builder branch>`" in i
    assert "from your own checkout, on your branch" in i
    assert "run `git merge --abort` and go on with the next branch" in i
    assert "the driver re-dispatches it to a new builder" in i
    assert integ["schemaKeys"] == ["merged", "conflicts", "summary"]
    assert "branch `worktree-app`" in i
    assert "Do not remove worktrees or delete branches: the driver does that" in i
    assert "cat > /work/product/loop/REPORT.md <<'EOF'" in i
    assert "| lead | <summary>` to /work/product/loop/LOG.md" in i
    cleanup = next(c["prompt"] for c in out["calls"] if "op=cleanup" in c["prompt"])
    assert "--branches 'worktree-app'" in cleanup
    ev = next(c["prompt"] for c in out["calls"] if c["agentType"] == "trio-evaluator")
    assert "/repo/.claude/worktrees/eval-1-att1 sha1" in ev


@needs_node
def test_harness_disjoint_slices_share_a_wave_overlaps_serialize() -> None:
    plan = [
        {"id": "a", "brief": "A", "writes": ["src/a.py"]},
        {"id": "b", "brief": "B", "writes": ["src/b.py"]},
        {"id": "c", "brief": "C", "writes": ["src"]},              # overlaps a, b
        {"id": "d", "brief": "D", "writes": ["docs/d.md"], "depends": ["a"]},
        {"id": "e", "brief": "E", "writes": []},                   # unknown: alone
    ]
    out = run({"verdicts": ["SHIP"], "plan": plan})
    r = out["result"]
    assert r["status"] == "shipped"
    assert r["iterations"][0]["waves"] == [["a", "b"], ["c", "d"], ["e"]]
    names = seq(out)
    assert names.count("dispatch") == 3 and names.count("cleanup") == 3
    assert names.count("lead") == 1 + 3
    integ = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-lead"][1:]
    assert "More waves follow" in integ[0] and "(last wave)" in integ[2]
    bases = [re.search(r"`base` = (\S+) ", c["prompt"]).group(1)
             for c in out["calls"] if c["agentType"] == "trio-builder"]
    assert bases == ["H1w1", "H1w1", "H1w2", "H1w2", "H1w3"]


@needs_node
def test_harness_builder_wrong_base_stops_before_integrate() -> None:
    """Probe blocker 2: a worktree forked from origin/HEAD is refused."""
    out = run({"verdicts": ["SHIP"], "bad_base_ids": ["app"]})
    r = out["result"]
    assert r["status"] == "error" and "baseRef" in r["reason"]
    assert "origin-head" in r["reason"]
    assert seq(out) == ["begin", "next", "lead", "dispatch", "builder", "end"]


@needs_node
def test_harness_helper_refusal_stops() -> None:
    out = run({"verdicts": ["SHIP"], "refuse_ids": ["app"]})
    r = out["result"]
    assert r["status"] == "error" and "builders refused" in r["reason"]
    assert seq(out)[-2:] == ["builders", "end"]


@needs_node
def test_harness_empty_plan_runs_one_solo_lead_call() -> None:
    out = run({"verdicts": ["SHIP"], "plan": []})
    assert out["result"]["status"] == "shipped"
    assert seq(out)[:5] == ["begin", "next", "lead", "lead", "gate"]
    solo = [c for c in out["calls"] if c["agentType"] == "trio-lead"][1]
    assert "no code-changing slices" in solo["prompt"]


@needs_node
@pytest.mark.parametrize("op", ["gate", "pin", "apply", "begin", "end", "builders"])
def test_harness_lossy_step_result_is_rerun(op: str) -> None:
    """Probe blocker 3: a result missing the op's keys is not accepted."""
    out = run({"verdicts": ["SHIP"], "lossy": {op: 1}})
    r = out["result"]
    assert r["status"] == "shipped" and r["commit_shas"] == ["c0ffee"]
    assert seq(out).count(op) == 2
    assert any(f"step {op}: result lacks" in line for line in out["logs"])
    if op == "end":
        assert r["lock"] == "released"


@needs_node
def test_harness_self_refusal_is_error_not_held() -> None:
    """Probe blocker 8: `held` without a harness denial is an error."""
    out = run({"verdicts": ["SHIP"], "self_refuse_op": "pin"})
    r = out["result"]
    assert r["status"] == "error" and r["held_step"] is None
    assert "declined without a harness permission denial" in r["reason"]
    assert seq(out).count("pin") == 1


@needs_node
def test_harness_iterate_then_ship() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"]})
    r = out["result"]
    assert r["status"] == "shipped" and r["iteration"] == 2
    assert seq(out) == ONE_PASS + ITER + ["end"]
    assert [i["verdict"] for i in r["iterations"]] == ["ITERATE", "SHIP"]


@needs_node
def test_harness_scoped_iterate_runs_repair_on_sonnet() -> None:
    out = run({"verdicts": ["ITERATE scope=local:app.py", "SHIP"],
               "args": {"max_agents": 20}})
    assert seq(out)[12:15] == ["next", "repair", "gate"]
    repair = next(c for c in out["calls"] if c["agentType"] == "trio-repair")
    assert repair["model"] == "claude-sonnet-5"
    assert "| repair |" in repair["prompt"]
    assert "scope=local:app.py" in repair["prompt"]


@needs_node
def test_harness_gate_fail_retries_lead_once() -> None:
    out = run({"verdicts": ["SHIP"], "gates": [False, True]})
    assert out["result"]["status"] == "shipped"
    assert seq(out)[:11] == ONE_PASS[:9] + ["lead", "gate"]
    retry = [c for c in out["calls"] if c["agentType"] == "trio-lead"][2]
    assert "RETRY (attempt 2 of 2)" in retry["prompt"]
    gates = [c["prompt"] for c in out["calls"] if "op=gate" in c["prompt"]]
    assert "--attempt '1'" in gates[0] and "--attempt '2'" in gates[1]


@needs_node
def test_harness_gate_fail_twice_stops_with_error() -> None:
    out = run({"verdicts": ["SHIP"], "gates": [False, False]})
    r = out["result"]
    assert r["status"] == "error" and "gate breach" in r["reason"]
    assert seq(out) == ONE_PASS[:9] + ["lead", "gate", "end"]


@needs_node
def test_harness_max_agents_exhaustion_keeps_end() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"], "args": {"max_agents": 9}})
    r = out["result"]
    assert r["status"] == "budget" and "max_agents=9" in r["reason"]
    assert r["agents_used"] == 9 and r["lock"] == "released"
    assert seq(out) == ONE_PASS[:8] + ["end"]


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
        if path.is_file() and path.suffix in (".js", ".mjs", ".md", ".py", ".sh"):
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
    assert r["agents_used"] == 2 + 6 * 11 and r["max_agents"] is None


@needs_node
def test_harness_token_budget_opt_in() -> None:
    out = run({"verdicts": ["ITERATE", "SHIP"],
               "args": {"token_budget": 5000}})
    r = out["result"]
    assert r["status"] == "budget" and "token_budget=5000" in r["reason"]
    assert seq(out) == ["begin", "next", "lead", "dispatch", "builder", "end"]


@needs_node
@pytest.mark.parametrize("op", ["gate", "apply", "next", "dispatch", "cleanup"])
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


# ------------------------------------------- probe 2 blocker B: conflicts
TWO = [{"id": "alpha", "brief": "A", "writes": ["alpha.py"]},
       {"id": "beta", "brief": "B", "writes": ["beta.py"]}]


@needs_node
def test_harness_conflict_redispatches_from_new_head() -> None:
    out = run({"verdicts": ["SHIP"], "plan": TWO,
               "conflicts": {"beta": ["registry.py"]}})
    r = out["result"]
    assert r["status"] == "shipped" and r["conflicts"] == []
    it = r["iterations"][0]
    assert it["waves"] == [["alpha", "beta"], ["beta"]]
    assert it["conflicts"] == [{"id": "beta", "branch": "worktree-beta",
                                "files": ["registry.py"]}]
    builders = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-builder"]
    assert len(builders) == 3
    again = builders[2]
    assert "The driver requires `base` = H1w2" in again  # the post-merge HEAD
    assert "RE-DISPATCH" in again and "`worktree-beta`" in again
    assert "registry.py" in again and "beta.py, registry.py" in again
    integ = [c["prompt"] for c in out["calls"]
             if c["agentType"] == "trio-lead"][1:]
    assert len(integ) == 2
    assert "If any merge in this call conflicted, stop after your review" in integ[0]
    assert "(last wave)" in integ[1] and "- beta: branch `worktree-beta-r2`" in integ[1]
    cleanups = [c["prompt"] for c in out["calls"] if "op=cleanup" in c["prompt"]]
    assert "--drop-unmerged" not in cleanups[0]
    assert "--drop-unmerged 'worktree-beta=worktree-beta-r2'" in cleanups[1]
    assert any("merge conflict beta (worktree-beta) on registry.py" in line
               for line in out["logs"])
    assert seq(out).count("gate") == 1


@needs_node
def test_harness_second_conflict_stops_with_conflict_status() -> None:
    out = run({"verdicts": ["SHIP"], "plan": TWO,
               "conflicts": {"beta": ["registry.py"]},
               "conflict_again": ["beta"]})
    r = out["result"]
    assert r["status"] == "conflict" and r["lock"] == "released"
    assert "beta (worktree-beta-r2) on registry.py" in r["reason"]
    assert r["conflicts"] == [{"id": "beta", "branch": "worktree-beta-r2",
                               "files": ["registry.py"]}]
    names = seq(out)
    assert names[-2:] == ["cleanup", "end"] and "gate" not in names
    assert names.count("builder") == 3  # at most one re-dispatch per slice


@needs_node
def test_harness_unreported_unmerged_branch_is_a_conflict() -> None:
    """git decides: a branch cleanup finds unmerged is re-dispatched even
    when the Lead reported no conflict (files then unknown)."""
    out = run({"verdicts": ["SHIP"], "plan": TWO,
               "conflicts": {"beta": ["registry.py"]},
               "integrate_hides_conflicts": True})
    assert out["result"]["status"] == "shipped"
    assert out["result"]["iterations"][0]["waves"] == [["alpha", "beta"], ["beta"]]


@needs_node
def test_harness_plan_prompt_puts_shared_files_in_writes() -> None:
    out = run({"verdicts": ["SHIP"]})
    plan = next(c for c in out["calls"] if c["agentType"] == "trio-lead")
    assert "registries, `__init__.py`, config" in plan["prompt"]
    assert "must both list it" in plan["prompt"]


@needs_node
def test_harness_gate_retry_names_kept_branch_and_reruns_cleanup() -> None:
    """eval-native-v0b N5."""
    out = run({"verdicts": ["SHIP"], "gates": [False, True],
               "kept_branches": ["worktree-app"]})
    assert out["result"]["status"] == "shipped"
    solo = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-lead"][2]
    assert "RETRY (attempt 2 of 2)" in solo
    assert "- `worktree-app`: uncommitted changes outside the mailbox" in solo
    names = seq(out)
    i = names.index("lead", names.index("gate"))
    assert names[i:i + 3] == ["lead", "cleanup", "gate"]
    assert out["result"]["iterations"][0]["kept"] == [
        {"branch": "worktree-app",
         "reason": "uncommitted changes outside the mailbox: notes.txt"}]


@needs_node
def test_harness_builder_discards_untrusted_leftovers() -> None:
    """Probe 2 finding C: a journal resume re-creates the killed builder's
    worktree with its uncommitted files; the builder discards them, and
    only inside its own .claude/worktrees/ path."""
    out = run({"verdicts": ["SHIP"]})
    b = next(c["prompt"] for c in out["calls"] if c["agentType"] == "trio-builder")
    assert "Leftovers are untrusted" in b
    assert "`git merge-base --is-ancestor H1w1 HEAD`" in b
    assert "`git reset --hard H1w1 && git clean -fd`" in b
    assert "is inside `/repo/.claude/worktrees/`" in b
    assert "Never run these outside that directory" in b
    assert "git clean -fdx" not in b

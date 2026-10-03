"""slice(drv-opencode): the two acceptance-strength rules through the
trio-opencode driver (both lockstep and open-loop), against the fake
``opencode`` only (never the real binary).

Rule 1 (the SHIP attacks gate): every SHIP needs at least one counted
adversarial probe beyond the frozen checks (an ``attacks:`` list of
``- <probe> -> <what happened>``), acceptance on or off. Rule 2 (the
requirement map): the acceptance author must map every GOAL requirement to a
normal and a hard check; gaps reach the author's retry as
``- pack: requirements: ...``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from trio_opencode import driver, olprompts, openloop, prompts, quality, steplib

import test_e2e as ls_e2e  # noqa: E402 - lockstep helpers (aliased: same names as open-loop's)
from test_acc_harden import _acc_repo  # noqa: E402
from test_openloop_e2e import (  # noqa: E402 - sibling test module's helpers
    _git, install_fake_for_process, make_cfg, make_key_file, ol_repo_one_slice, read_calls,
)

TL = steplib.TL
EVAL_HEAD = "SHIP REFUSED BY THE DRIVER (your previous SHIP was refused):"
ATTACKS = "attacks:\n- empty input -> rejected with exit 2\n"


def _prompts_of(env, agent: str, kind: str) -> list[str]:
    """Every prompt the fake received for *agent* whose OPEN-LOOP CONTEXT
    kind is *kind*, in call order."""
    out = []
    for c in read_calls(env):
        if c["agent"] == agent and f"OPEN-LOOP CONTEXT: kind={kind}" in c["prompt"].splitlines()[0]:
            out.append(c["prompt"])
    return out


def _log(root: Path) -> str:
    return (root / "loop" / "LOG.md").read_text(encoding="utf-8")


def _run_ol(tmp_path, monkeypatch, repo: Path, mode: str, **kw):
    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_attacks.py")
    monkeypatch.setenv("FAKE_OC_ATTACKS_MODE", mode)
    monkeypatch.setenv("TRIO_NATIVE_JOBS", "inline")
    result = driver.run(repo / "loop", cfg, mode="start", max_iterations=6, root_free=False, **kw)
    return env, result


# ------------------------------------------------------------ open-loop rule 1
def test_ol_slice_ship_without_attacks_is_redispatched_then_accepted(
        ol_repo_one_slice, tmp_path, monkeypatch):
    env, result = _run_ol(tmp_path, monkeypatch, ol_repo_one_slice, "slice")
    log = _log(ol_repo_one_slice)
    assert result["status"] == "shipped", (result, log)
    evals = _prompts_of(env, "trio-evaluator", "slice-eval")
    assert len(evals) == 2, [e[:200] for e in evals]
    assert "SLICE-EVAL ERRORS FROM THE DRIVER:" not in evals[0]
    assert "SLICE-EVAL ERRORS FROM THE DRIVER:" in evals[1]
    block = evals[1].split("SLICE-EVAL ERRORS FROM THE DRIVER:", 1)[1].lstrip("\n")
    assert block.startswith("- "), block[:200]
    assert re.search(r"slice a @[0-9a-f]{12}.*attack", block, re.I | re.S), block[:600]
    assert "SHIP not accepted" in log and "attacks=n/a" in log and "attacks=1" in log and "re-dispatching slice-eval (attempt 1)" in log, log
    assert "slice: a" in (ol_repo_one_slice / "loop" / "QUEUE.md").read_text(encoding="utf-8")


def test_ol_slice_ship_that_never_lists_attacks_stops_instead_of_shipping(
        ol_repo_one_slice, tmp_path, monkeypatch):
    env, result = _run_ol(tmp_path, monkeypatch, ol_repo_one_slice, "slice_never")
    assert result["status"] != "shipped", result
    assert len(_prompts_of(env, "trio-evaluator", "slice-eval")) <= 4
    assert not _prompts_of(env, "trio-evaluator", "integration-eval")


def test_ol_integration_ship_without_attacks_is_refused_then_ships(
        ol_repo_one_slice, tmp_path, monkeypatch):
    env, result = _run_ol(tmp_path, monkeypatch, ol_repo_one_slice, "integration")
    log = _log(ol_repo_one_slice)
    assert result["status"] == "shipped", (result, log)
    assert "SHIP refused by the attacks gate (verdict becomes ITERATE)" in log, log
    leads = _prompts_of(env, "trio-lead", "lead-plan")
    assert len(leads) >= 2, [l[:120] for l in leads]
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):" not in leads[0]
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):" in leads[1]
    assert len(_prompts_of(env, "trio-evaluator", "integration-eval")) == 2
    # spent once a Lead got it: no stale refusal is left to re-render
    assert TL.ship_gate_errors(ol_repo_one_slice / "loop") == []


def test_ol_integration_refusal_reaches_the_next_integration_evaluator(
        ol_repo_one_slice, tmp_path, monkeypatch):
    env, result = _run_ol(tmp_path, monkeypatch, ol_repo_one_slice, "integration")
    assert result["status"] == "shipped", (result, _log(ol_repo_one_slice))
    evaluators = _prompts_of(env, "trio-evaluator", "integration-eval")
    assert len(evaluators) == 2
    assert EVAL_HEAD not in evaluators[0]
    assert EVAL_HEAD + "\n- The driver refused your previous SHIP" in evaluators[1]
    for slice_eval in _prompts_of(env, "trio-evaluator", "slice-eval"):
        assert EVAL_HEAD not in slice_eval
    # the Lead's block is its own text, and the Evaluator's copy is spent
    leads = _prompts_of(env, "trio-lead", "lead-plan")
    assert all(EVAL_HEAD not in l for l in leads)
    assert TL.eval_gate_errors(ol_repo_one_slice / "loop") == []


def test_ol_degraded_acceptance_still_applies_the_attacks_gate(
        ol_repo_one_slice, tmp_path, monkeypatch):
    """The author dies, acceptance degrades to no pack ("as if acceptance
    were off"): the attacks gate is not part of acceptance and still refuses
    an attack-less SHIP."""
    env, result = _run_ol(tmp_path, monkeypatch, ol_repo_one_slice, "integration",
                          acceptance=True)
    log = _log(ol_repo_one_slice)
    assert "acceptance: DEGRADED to no frozen pack" in log, log
    assert "the attacks gate still applies" in log, log
    assert "SHIP refused by the attacks gate (verdict becomes ITERATE)" in log, log
    assert result["status"] == "shipped", (result, log)
    assert len(_prompts_of(env, "trio-evaluator", "integration-eval")) == 2
    leads = _prompts_of(env, "trio-lead", "lead-plan")
    assert any("SHIP GATE ERRORS FROM THE DRIVER" in l for l in leads[1:]), [l[:80] for l in leads]


# ------------------------------------------------------------- lockstep rule 1
def _run_ls(tmp_path, monkeypatch, repo: Path, mode: str, *, scenario="ls_attacks.py", **kw):
    cfg = ls_e2e.make_cfg(ls_e2e.make_key_file(tmp_path), max_iterations=4)
    env = ls_e2e.install_fake_for_process(tmp_path, monkeypatch, scenario)
    monkeypatch.setenv("FAKE_OC_LS_MODE", mode)
    result = driver.run(repo / "loop", cfg, mode="start", max_iterations=4, root_free=False, **kw)
    return env, result


def _lead_plans(env) -> list[str]:
    return [c["prompt"] for c in ls_e2e.read_calls(env)
            if c["agent"] == "trio-lead" and "PLAN CALL" in c["prompt"]]


def test_ls_ship_without_attacks_is_refused_then_ships(git_repo, tmp_path, monkeypatch):
    """End to end through the native helper's `apply`: the attacks gate turns
    the attack-less SHIP into ITERATE, the next Lead plan shows why, the
    re-grade ships. (The refusal itself is the native op's job -- this test
    only passes once native `op_apply` runs ``review_ship_attacks``.)"""
    env, result = _run_ls(tmp_path, monkeypatch, git_repo, "first_no_attacks")
    log = (git_repo / "loop" / "LOG.md").read_text(encoding="utf-8")
    assert "SHIP refused by the attacks gate (verdict becomes ITERATE)" in log, log
    assert result["status"] == "shipped" and result["iteration"] == 2, result
    assert result["ship_gate_refused"] == [{"iteration": 1, "became": "ITERATE"}], result
    plans = _lead_plans(env)
    assert len(plans) == 2, [p[:80] for p in plans]
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):" not in plans[0]
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):" in plans[1]


def test_ls_ship_refusal_reaches_the_second_evaluator_prompt(git_repo, tmp_path, monkeypatch):
    env, result = _run_ls(tmp_path, monkeypatch, git_repo, "first_no_attacks")
    assert result["status"] == "shipped" and result["iteration"] == 2, result
    evaluators = [c["prompt"] for c in ls_e2e.read_calls(env) if c["agent"] == "trio-evaluator"]
    assert len(evaluators) == 2, [p[:80] for p in evaluators]
    assert EVAL_HEAD not in evaluators[0]
    assert EVAL_HEAD + "\n- The driver refused your previous SHIP" in evaluators[1]
    assert "SHIP GATE ERRORS" not in evaluators[1]
    assert TL.eval_gate_errors(git_repo / "loop") == []


def test_ls_default_run_reports_no_ship_gate_refusal(git_repo, tmp_path, monkeypatch):
    env, result = _run_ls(tmp_path, monkeypatch, git_repo, "always_attacks")
    assert result["status"] == "shipped", result
    assert result["ship_gate_refused"] == []
    assert not any("SHIP GATE ERRORS" in p for p in _lead_plans(env))


def test_ls_lead_plan_renders_ship_gate_errors_read_from_the_core(git_repo, tmp_path, monkeypatch):
    """The refusal is persisted by the core in `.driver.json`; the lockstep
    driver reads it straight from ``TL.ship_gate_errors`` (not from the native
    `next` result) and renders it into the very Lead plan call of that
    iteration -- acceptance off -- then spends it."""
    box = git_repo / "loop"
    TL._persist_ship_gate_errors(box, 1, ["The driver refused the SHIP at abc: no attack."])
    env, result = _run_ls(tmp_path, monkeypatch, git_repo, "always_attacks")
    assert result["status"] == "shipped", result
    plans = _lead_plans(env)
    assert plans and "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n" \
        "- The driver refused the SHIP at abc: no attack." in plans[0], plans[0][-900:]
    assert TL.ship_gate_errors(box) == []


def test_ls_stale_ship_gate_errors_of_another_iteration_are_not_rendered(
        git_repo, tmp_path, monkeypatch):
    TL._persist_ship_gate_errors(git_repo / "loop", 7, ["stale"])
    env, result = _run_ls(tmp_path, monkeypatch, git_repo, "always_attacks")
    assert result["status"] == "shipped", result
    assert not any("SHIP GATE ERRORS" in p for p in _lead_plans(env))


def test_ls_ship_gate_errors_merge_the_core_read_with_the_native_next_result(tmp_path):
    TL._persist_ship_gate_errors(tmp_path, 4, ["core says no attack", "shared"])
    ctx = SimpleNamespace(live_mailbox=tmp_path, next_ship_gate_errors=["native says it", "shared"])
    assert driver._ship_gate_errors(ctx, 4) == ["core says no attack", "shared", "native says it"]
    # another iteration's stored gate is stale; the native list alone still renders
    assert driver._ship_gate_errors(ctx, 5) == ["native says it", "shared"]
    ctx.next_ship_gate_errors = []
    assert driver._ship_gate_errors(ctx, 5) == []


class _Ctx:
    """Just enough of a RunContext for `_apply`'s attacks-gate notice."""

    def __init__(self, mailbox: Path) -> None:
        self.live_mailbox = mailbox
        self.acceptance = False
        self.ship_gate_refused: list = []
        self.out_lines: list[str] = []

    def out(self, line: str) -> None:
        self.out_lines.append(line)


def test_ls_apply_records_the_refusal_from_the_op_result_key(tmp_path):
    ctx = _Ctx(tmp_path)
    driver._note_attacks_gate(ctx, 3, {"verdict": "ITERATE",
                                       "attacks_gate": {"refused": True, "errors": ["x"]}})
    assert ctx.ship_gate_refused == [{"iteration": 3, "became": "ITERATE"}]
    assert any("attacks gate refused the SHIP" in l for l in ctx.out_lines), ctx.out_lines
    # replayed apply: no duplicate record
    driver._note_attacks_gate(ctx, 3, {"verdict": "ITERATE", "attacks_gate": {"refused": True}})
    assert len(ctx.ship_gate_refused) == 1


def test_ls_apply_falls_back_to_the_core_log_refusal_line(tmp_path):
    (tmp_path / "LOG.md").write_text(
        "# Trio loop log\n- iter 3 | loop | SHIP refused by the attacks gate "
        "(verdict becomes ITERATE): no attack beyond the frozen checks (attacks=n/a)\n",
        encoding="utf-8")
    ctx = _Ctx(tmp_path)
    driver._note_attacks_gate(ctx, 3, {"verdict": "ITERATE"})
    assert ctx.ship_gate_refused == [{"iteration": 3, "became": "ITERATE"}]
    # another iteration's line, a shipped verdict, or an explicit "not refused" key: nothing
    other = _Ctx(tmp_path)
    driver._note_attacks_gate(other, 2, {"verdict": "ITERATE"})
    driver._note_attacks_gate(other, 3, {"verdict": "SHIP"})
    driver._note_attacks_gate(other, 3, {"verdict": "ITERATE", "attacks_gate": {"refused": False}})
    assert other.ship_gate_refused == []


# -------------------------------------------------------- prompt rendering units
def test_lockstep_lead_plan_prompt_renders_ship_gate_errors_with_or_without_acceptance():
    err = "The driver refused the SHIP at abc123: no attack."
    plain = prompts.lead_plan_prompt(2, "/m", "/r", "/t", ship_gate_errors=[err])
    block = ("SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n- " + err)
    assert block in plain
    with_acc = prompts.lead_plan_prompt(
        2, "/m", "/r", "/t", ship_gate_errors=[err],
        acc_lines=prompts.acc_plan_lines("/m", "/tool", {"status": "frozen", "pin": "p" * 40},
                                         errors=["ACC refused"]))
    assert block in with_acc and "ACCEPTANCE ERRORS FROM THE DRIVER" in with_acc
    assert "SHIP GATE ERRORS" not in prompts.lead_plan_prompt(2, "/m", "/r", "/t")
    assert "SHIP GATE ERRORS" not in prompts.lead_plan_prompt(2, "/m", "/r", "/t", ship_gate_errors=[])


def _ol_ctx(kind: str, **extra) -> dict:
    base = {"mailbox": "/m", "iteration": 2, "repo": "/r", "driver": "opencode",
            "output": "fenced-json", "notes": [], "human_answer": None, "tmpdir": "/t"}
    if kind == "lead-plan":
        base.update(queue_errors=[], acceptance_errors=[], open_faults=[], retired=[],
                    refusals=[], repos=[])
    else:
        base.update(slice="a", sha="1" * 40, eval_worktree="/w", repo_name="home",
                    shadow="/s", lead_worktree="/r", acceptance_covered=None)
    base.update(extra)
    return base


def test_olprompts_render_ship_gate_and_slice_eval_error_blocks():
    lead = olprompts.render("lead-plan", _ol_ctx("lead-plan", ship_gate_errors=["E1", "E2"]))
    assert ("SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n- E1\n- E2") in lead
    assert "SHIP GATE ERRORS" not in olprompts.render("lead-plan", _ol_ctx("lead-plan"))
    ev = olprompts.render("slice-eval", _ol_ctx("slice-eval", slice_eval_errors=["S1", "S2"]))
    assert "SLICE-EVAL ERRORS FROM THE DRIVER:\n- S1\n- S2" in ev
    assert "SLICE-EVAL ERRORS" not in olprompts.render("slice-eval", _ol_ctx("slice-eval"))


def test_olprompts_render_the_integration_evaluators_refusal_block():
    ctx = _ol_ctx("slice-eval")
    ctx = {k: v for k, v in ctx.items() if k not in ("slice", "repo_name", "shadow", "acceptance_covered")}
    ctx.update(attempt="att1", pins={"home": "1" * 40}, repo_worktrees={"home": "/w"},
               acceptance=None, retire_paths={})
    plain = olprompts.render("integration-eval", dict(ctx))
    assert "SHIP REFUSED BY THE DRIVER" not in plain
    shown = olprompts.render("integration-eval", dict(ctx, eval_gate_errors=["E1", "E2"]))
    assert (EVAL_HEAD + "\n- E1\n- E2") in shown
    assert "SHIP REFUSED BY THE DRIVER" not in olprompts.render(
        "slice-eval", _ol_ctx("slice-eval", slice_eval_errors=["S1"]))


# ------------------------------------------------------------------- rule 2
def test_author_retry_prompt_carries_requirement_gaps_from_fatal():
    out = prompts.author_prompt("/export", "/tool", "marker", 2,
                                retry={"fatal": ["requirements: R1 has no hard check"]})
    assert "- pack: requirements: R1 has no hard check" in out
    assert "adding a hard check" in out
    assert "adding a hard check" not in prompts.author_prompt(
        "/export", "/tool", "marker", 2, retry={"fatal": ["schema: bad"]})


def test_lockstep_author_gets_the_requirement_gap_on_its_one_retry(tmp_path, monkeypatch):
    """A fake author whose first map gives R1 no hard check: the core's freeze
    gate reports the gap, the driver forwards it, attempt 2 fixes it."""
    repo = ls_e2e.make_acc_repo(tmp_path)
    result, env = ls_e2e.run_acceptance(tmp_path, monkeypatch, repo, mode="nohard_retry")
    assert result["status"] == "shipped", result
    authors = [c for c in ls_e2e.read_calls(env) if c["agent"] == "trio-acceptance"]
    assert len(authors) == 2, [a["prompt"][:100] for a in authors]
    assert "requirements:" not in authors[0]["prompt"].split("RETRY", 1)[-1] \
        or "RETRY" not in authors[0]["prompt"]
    assert "- pack: requirements: R1 has no hard check" in authors[1]["prompt"], authors[1]["prompt"]
    manifest = json.loads((repo / "loop" / "acceptance" / "MANIFEST.json").read_text())
    assert manifest["requirements"][0]["hard"] == [{"check": "ACC-02", "type": "boundary"}]
    frozen = (repo / "loop" / "acceptance" / "FROZEN").read_text()
    assert "requirement_gaps" not in frozen


def test_legacy_frozen_pack_resume_logs_the_warning_once(tmp_path, monkeypatch):
    """A pack frozen before the requirement-map rule keeps working; every
    controller start on resume (open-loop's DegradableAcceptance inherits the
    core's) logs the WARNING exactly once."""
    from scenarios import ol_acceptance
    root = _acc_repo(tmp_path)
    monkeypatch.setenv("TRIO_ACCEPTANCE_STATE", str(tmp_path / "accstate"))

    class _LegacyAuthor:
        driver_meta: dict = {}

        def author(self, export, context):  # noqa: ANN001
            ol_acceptance._write_pack(Path(export))
            mf = Path(export) / "acceptance" / "MANIFEST.json"
            data = json.loads(mf.read_text(encoding="utf-8"))
            data.pop("requirements")
            mf.write_text(json.dumps(data), encoding="utf-8")
            return {"exit": 0, "transcript": []}

    box = root / "loop"
    ctl = openloop.DegradableAcceptance(box, root, _LegacyAuthor(), {"enabled": True})
    with monkeypatch.context() as m:
        # freeze exactly as the pre-rule core did: no requirement gaps reported
        m.setattr(ctl.ta, "requirements_problems", lambda *a, **k: [])
        ctl.start(1)
        ctl.wait(1)
    assert ctl.frozen() and ctl.degraded is None
    assert "WARNING" not in (box / "LOG.md").read_text(encoding="utf-8")
    for _ in range(2):  # two resumes / helper ops
        again = openloop.DegradableAcceptance(box, root, _LegacyAuthor(), {"enabled": True})
        again.on_resume()
        again.start(1)
        assert again.degraded is None
    text = (box / "LOG.md").read_text(encoding="utf-8")
    assert text.count("acceptance: WARNING legacy frozen pack: no requirements map") == 1, text
    assert text.count("WARNING") == 1, text


# ------------------------------------------------------ .driver.json ship_gate
def _ctx_for(root: Path) -> driver.RunContext:
    mailbox = root / "loop"
    return driver.RunContext(
        root_mailbox=mailbox, live_mailbox=mailbox, repo=root, cfg=object(), token="tok",
        exec_id="e" * 32, run_dir=root / "run", log_dir=root / "run" / "logs", env={},
        root_free=False, lead_record=None, out=lambda s: None, cancel=threading.Event())


def _sidecar(ctx) -> None:
    openloop._make_sidecar_writer(ctx)(
        ctx.live_mailbox, SimpleNamespace(session_ids={}, driver_meta={}),
        SimpleNamespace(session_ids={}, driver_meta={}), 2, "lead", True, True, "2024-01-01T00:00:00Z")


def _gate(ctx):
    return json.loads(ctx.driver_json_path.read_text(encoding="utf-8")).get("ship_gate")


@pytest.mark.parametrize("writer", ["lockstep", "open-loop"])
def test_driver_json_rewrites_preserve_a_pending_ship_gate_and_never_resurrect_a_cleared_one(
        git_repo, writer):
    ctx = _ctx_for(git_repo)
    write = (lambda: ctx.write_driver_json(phase="lead-running", iteration=2)) \
        if writer == "lockstep" else (lambda: _sidecar(ctx))
    write()
    assert _gate(ctx) is None
    TL._persist_ship_gate_errors(ctx.live_mailbox, 2, ["refused"])    # the core, mid-run
    for _ in range(2):                                                # on_spawn / on_turn_end ...
        write()
        assert _gate(ctx) == {"iteration": 2, "errors": ["refused"]}
    TL.clear_ship_gate_errors(ctx.live_mailbox)                       # the core: Lead got it
    write()
    assert _gate(ctx) is None, "a cleared ship_gate came back"
    assert TL.ship_gate_errors(ctx.live_mailbox) == []


@pytest.mark.parametrize("writer", ["lockstep", "open-loop"])
def test_driver_json_rewrites_preserve_a_pending_eval_gate_and_never_resurrect_a_cleared_one(
        git_repo, writer):
    ctx = _ctx_for(git_repo)
    write = (lambda: ctx.write_driver_json(phase="lead-running", iteration=2)) \
        if writer == "lockstep" else (lambda: _sidecar(ctx))
    write()
    TL._persist_eval_gate_errors(ctx.live_mailbox, 2, ["refused"])
    for _ in range(2):
        write()
        assert json.loads(ctx.driver_json_path.read_text())["eval_gate"] == {
            "iteration": 2, "errors": ["refused"]}
    TL.clear_ship_gate_errors(ctx.live_mailbox)           # the Lead's clear leaves it alone
    write()
    assert TL.eval_gate_errors(ctx.live_mailbox) == ["refused"]
    TL.clear_eval_gate_errors(ctx.live_mailbox)           # an Evaluator got it
    write()
    assert "eval_gate" not in json.loads(ctx.driver_json_path.read_text()), "a cleared gate came back"


def test_retiring_driver_json_keeps_an_unread_evaluator_refusal(git_repo):
    ctx = _ctx_for(git_repo)
    ctx.write_driver_json(phase="evaluate-running", iteration=1)
    TL._persist_ship_gate_errors(ctx.live_mailbox, 2, ["lead copy"])
    TL._persist_eval_gate_errors(ctx.live_mailbox, 2, ["eval copy"])
    driver._retire_driver_json(ctx.driver_json_path, shipped=True)
    assert not ctx.driver_json_path.exists()
    ctx.write_driver_json(phase="evaluate-running", iteration=1)
    TL._persist_eval_gate_errors(ctx.live_mailbox, 2, ["eval copy"])
    driver._retire_driver_json(ctx.driver_json_path)
    assert json.loads(ctx.driver_json_path.read_text()) == {
        "eval_gate": {"iteration": 2, "errors": ["eval copy"]}}
    assert TL.eval_gate_errors(ctx.live_mailbox) == ["eval copy"]


def test_crash_rollback_snapshot_neither_drops_a_pending_nor_resurrects_a_cleared_gate(git_repo):
    box = git_repo / "loop"
    (box / "STATE.md").write_text("iteration: 2\nstatus: running\nphase: lead-running\n",
                                  encoding="utf-8")
    first = _ctx_for(git_repo)
    first.state_snapshot = {"iteration": "2", "phase": "lead-running", "evaluated_sha": "",
                            "evaluator_attempt": ""}
    first.write_driver_json(phase="lead-running", iteration=2)       # a guarded turn is in flight
    TL._persist_ship_gate_errors(box, 2, ["refused"])                # core persists during it
    first.write_driver_json(phase="lead-running", iteration=2)       # on_turn_end of another turn
    # crash; a fresh process resumes: restore from the snapshot, then write
    (box / "STATE.md").write_text("iteration: 9\nstatus: running\nphase: error\n", encoding="utf-8")
    second = _ctx_for(git_repo)
    driver._normalize_resume_phase(second)
    assert second.state_snapshot is None
    second.write_driver_json(phase="begin", iteration=2)
    assert _gate(second) == {"iteration": 2, "errors": ["refused"]}   # pending: kept
    assert json.loads(second.driver_json_path.read_text())["state_snapshot"] is None
    TL.clear_ship_gate_errors(box)
    third = _ctx_for(git_repo)
    driver._normalize_resume_phase(third)
    third.write_driver_json(phase="begin", iteration=2)
    assert _gate(third) is None                                       # cleared: stays cleared


def test_retiring_driver_json_keeps_only_an_unread_refusal(git_repo):
    ctx = _ctx_for(git_repo)
    ctx.write_driver_json(phase="evaluate-running", iteration=1)
    TL._persist_ship_gate_errors(ctx.live_mailbox, 2, ["refused"])
    driver._retire_driver_json(ctx.driver_json_path, shipped=True)    # shipped: nothing to resume
    assert not ctx.driver_json_path.exists()
    ctx.write_driver_json(phase="evaluate-running", iteration=1)
    TL._persist_ship_gate_errors(ctx.live_mailbox, 2, ["refused"])
    driver._retire_driver_json(ctx.driver_json_path)                  # stopped before a Lead read it
    assert json.loads(ctx.driver_json_path.read_text()) == {
        "ship_gate": {"iteration": 2, "errors": ["refused"]}}
    assert TL.ship_gate_errors(ctx.live_mailbox, 2) == ["refused"]
    driver._retire_driver_json(ctx.driver_json_path, shipped=True)
    ctx.write_driver_json(phase="x", iteration=1)
    driver._retire_driver_json(ctx.driver_json_path)                  # no gate: plain removal
    assert not ctx.driver_json_path.exists()


# ---------------------------------------------------- slice_evidence vs the core
_SECTIONS = {
    "no header": "evidence: re-run=1\n- a.py: PASS\n",
    "header, no bullets": "attacks:\n",
    "one counted": "attacks:\n- empty input -> rejected with exit 2\n",
    "unicode arrow": "attacks:\n- empty input \u2192 rejected\n",
    "no result": "attacks:\n- empty input\n",
    "empty result": "attacks:\n- empty input ->\n",
    "empty probe": "attacks:\n- -> rejected\n",
    "placeholder none": "attacks:\n- none -> n/a\n",
    "placeholder n/a": "attacks:\n- n/a -> nothing\n",
    "placeholder tbd": "attacks:\n- TBD -> later\n",
    "mixed": "attacks:\n- none -> x\n- bad input -> exit 2\n- no result\n- 0 -> ok\n",
    "continuation line": "attacks:\n- probe one\n    -> result on the next line\n",
    "bold header": "**Attacks:**\n- a -> b\n",
    "inline text after colon": "attacks: tried two\n- a -> b\n- c -> d\n",
    "two blocks sum": "attacks:\n- a -> b\n\nnotes\n\nattacks:\n- c -> d\n",
    "list ends at prose": "attacks:\n- a -> b\nprose line\n- c -> d\n",
    "star bullets": "attacks:\n* a -> b\n* c -> d\n",
    "tried header": "Attacks tried:\n- a -> b\n",
    "attempted heading": "## Attacks attempted\n1. a -> b\n",
    "prose with attacks": "Attacks were tried\n- a -> b\n",
    "placeholder words": "attacks:\n- a -> not tested\n- b -> nothing\n- c -> not run twice: x\n",
    "slots and punctuation": "attacks:\n- a -> <x>, <y>\n- b -> ...\n- <p>, <q> -> r\n- ok -> fine\n",
}


@pytest.mark.parametrize("name", sorted(_SECTIONS))
def test_slice_evidence_attacks_match_the_core_count_attacks(name):
    sha = "ab" * 20
    body = _SECTIONS[name]
    text = f"## slice a @{sha} \u2014 SHIP\nevidence: re-run=1\n{body}"
    core = TL.count_attacks(f"evidence: re-run=1\n{body}")
    want = "n/a" if core is None else core
    assert quality.slice_evidence(text, "a", sha)["attacks"] == want
    # the fallback (a core without count_attacks) is the same rule
    assert quality._fallback_count_attacks(body) == TL.count_attacks(body)


def test_slice_evidence_keeps_an_attacks_heading_inside_the_slice_section():
    sha = "ef" * 20
    text = (f"## slice a @{sha} \u2014 SHIP\nevidence: re-run=1\n## Attacks\n- a -> b\n"
            f"## slice b @{'01' * 20} \u2014 SHIP\n## Attacks tried\n- c -> d\n- e -> f\n## Notes\nx\n")
    assert quality.slice_evidence(text, "a", sha)["attacks"] == 1
    assert quality.slice_evidence(text, "b", "01" * 20)["attacks"] == 2
    assert TL.slice_attacks(text, "a", sha) == 1


def test_slice_evidence_counts_only_attacks_that_record_a_result():
    sha = "cd" * 20
    text = (f"## slice a @{sha} \u2014 SHIP\nevidence: re-run=1\n"
            "attacks:\n- a\n- b -> c\n")
    # was 2 (every bullet); the gate counts one
    assert quality.slice_evidence(text, "a", sha)["attacks"] == 1

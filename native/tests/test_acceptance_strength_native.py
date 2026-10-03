"""Acceptance strength on the native driver.

Rule 1 (SHIP attacks gate): frozen checks are a floor, so a SHIP whose
VERDICT.md lists no counted adversarial probe beyond them is refused by
``apply`` (acceptance off or on), becomes ITERATE, and the refusal reaches
the next Lead through ``next`` (``ship_gate_errors``).

Rule 2 (requirement map): ``acceptance-freeze`` retries a pack whose map has
a gap (``fatal`` carries ``requirements: ...``), freezes the survivors after
the one retry with ``requirement_gaps``, and a loop resumed on a legacy pack
(no map) is warned about exactly once.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import test_r19_acceptance as R19
from test_r19_acceptance import (  # noqa: F401 (repo fixture)
    MODELS, PLAN_ALL, Run, acc_env, begin, freeze, git, lead_pass, log_text, mbox, repo,
    requirements_map, ship_verdict, state_text, to_prerun, write_pack)
from test_step_ops import HELPER, NATIVE, step, to_lead_done, write_verdict
from test_step_ops import lead_pass as plain_lead_pass
from test_workflow_acceptance import ACC, step_prompts
from test_workflow_script import needs_node, run as wf_run

REFUSED = "SHIP refused by the attacks gate (verdict becomes ITERATE)"
REFUSAL_COMMIT = "attacks gate refused SHIP (ITERATE)"


def fixing_pass(repo: Path, iteration: int) -> None:
    """A Lead pass that changes the product (a comment) and passes the gates."""
    with (repo / "app.py").open("a", encoding="utf-8") as fh:
        fh.write(f"# pass {iteration}\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", f"slice(app): pass {iteration}")
    with (mbox(repo) / "LOG.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- iter {iteration} | lead | done\n")
    (mbox(repo) / "REPORT.md").write_text(f"# Report {iteration}\n", encoding="utf-8")


def commit_subjects(repo: Path) -> list[str]:
    return git(repo, "log", "--format=%s").splitlines()


def driver_json(repo: Path) -> dict:
    path = mbox(repo) / ".driver.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def verdict_without_attacks(repo: Path, iteration: int, pin: dict, attacks: str = "") -> None:
    """A SHIP the Evaluator wrote with no counted attack (or only `attacks`)."""
    write_verdict(repo, "VERDICT: SHIP", iteration, pin,
                  attacks + f"commit: {pin['sha']}\n", attacks=False)


# ----------------------------------------------- Rule 1, acceptance off
def test_off_ship_without_attacks_is_refused_and_iterates(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    verdict_without_attacks(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["ok"], a
    assert a["verdict"] == "ITERATE" and a["stop"] is False and a["code"] is None, a
    assert a["attacks_gate"]["refused"] is True
    assert len(a["attacks_gate"]["errors"]) == 1
    assert "no attack beyond the frozen checks" in a["attacks_gate"]["errors"][0]
    assert a["ship_refused"] is True
    assert a["next_role"] == "lead" and a["status"] == "running"
    log = log_text(repo)
    assert log.count(REFUSED) == 1 and "attacks=n/a" in log, log
    assert [s for s in commit_subjects(repo) if REFUSAL_COMMIT in s] == [
        "loop: iteration 1 — " + REFUSAL_COMMIT]
    assert "shipped" not in state_text(repo)


@pytest.mark.parametrize("attacks, ships", [
    ("attacks: none\n", False),
    ("attacks:\n- n/a -> nothing\n- tbd -> later\n", False),
    ("attacks:\n- empty input\n", False),  # no `->`: not counted
    ("attacks:\n- reran ACC-01 -> still passes\n- n/a -> nothing\n", True),
])
def test_off_only_counted_attacks_let_a_ship_through(repo: Path, attacks: str, ships: bool) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    verdict_without_attacks(repo, 1, p, attacks)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    if ships:
        assert a["verdict"] == "SHIP" and "attacks_gate" not in a, a
    else:
        assert a["verdict"] == "ITERATE" and a["attacks_gate"]["refused"], a


def test_off_replayed_apply_is_idempotent(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    verdict_without_attacks(repo, 1, p)
    first = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert first["verdict"] == "ITERATE"
    # the recorded answer is replayed, also after the loop moved on
    for _ in range(2):
        again = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
        assert again["verdict"] == "ITERATE" and again["attacks_gate"] == first["attacks_gate"]
        assert again["stop"] is False
    step(repo, "next", max_iterations=4)
    again = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert again["verdict"] == "ITERATE" and again["attacks_gate"]["refused"] is True
    assert log_text(repo).count(REFUSED) == 1
    assert len([s for s in commit_subjects(repo) if REFUSAL_COMMIT in s]) == 1


def test_off_next_delivers_ship_gate_errors_until_the_lead_pass_is_done(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    verdict_without_attacks(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert "ship_gate" in driver_json(repo)
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "lead" and n["iteration"] == 2, n
    assert n["ship_gate_errors"] == a["attacks_gate"]["errors"]
    # `next` is idempotent: the pass is still running, the errors still come
    again = step(repo, "next", max_iterations=4)
    assert again["action"] == "lead" and again["ship_gate_errors"] == n["ship_gate_errors"]
    # the pass finishes (gate passes): the refusal is forgotten
    plain_lead_pass(repo, 2)
    g = step(repo, "gate", role="lead", iteration=2, attempt=1)
    assert g["ok"] and g["pass"], g
    assert "ship_gate" not in driver_json(repo)
    # the key is absent (not an empty list) when there is nothing to deliver
    assert "ship_gate_errors" not in step(repo, "pin", iteration=2)


def test_off_first_lead_pass_has_no_ship_gate_errors(repo: Path) -> None:
    step(repo, "begin")
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "lead" and "ship_gate_errors" not in n


def test_off_ship_with_attacks_ships(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: SHIP", 1, p, f"commit: {p['sha']}\n")
    git(repo, "add", "loop/VERDICT.md")
    git(repo, "commit", "-q", "-m", "loop: iteration 1 — SHIP")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["verdict"] == "SHIP" and a["code"] == 0 and a["status"] == "shipped", a
    assert "attacks_gate" not in a and "ship_refused" not in a
    assert REFUSED not in log_text(repo)


def test_off_refused_ship_then_a_fixed_pass_ships(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    verdict_without_attacks(repo, 1, p)
    assert step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])["verdict"] == "ITERATE"
    assert step(repo, "next", max_iterations=4)["iteration"] == 2
    plain_lead_pass(repo, 2)
    assert step(repo, "gate", role="lead", iteration=2, attempt=1)["pass"]
    p2 = step(repo, "pin", iteration=2)
    write_verdict(repo, "VERDICT: SHIP", 2, p2, f"commit: {p2['sha']}\n")
    git(repo, "add", "loop/VERDICT.md")
    git(repo, "commit", "-q", "-m", "loop: iteration 2 — SHIP")
    a = step(repo, "apply", iteration=2, attempt=p2["evaluator_attempt"])
    assert a["verdict"] == "SHIP" and a["status"] == "shipped", a


# ------------------------------------------------ Rule 1, acceptance on
def test_on_ship_without_attacks_is_refused_and_the_pack_is_not_run(repo: Path) -> None:
    run = Run(repo)
    # 3 of 5 checks pass: the frozen gate WOULD refuse this SHIP too.
    p, ar = to_prerun(run, repo, upto=3)
    assert ar["failed"] == 2
    ship_verdict(repo, 1, p)
    (mbox(repo) / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n# Verdict — iteration 1\nattempt: {p['evaluator_attempt']}\n"
        f"evaluated: {p['sha']}\ncommit: {p['sha']}\n", encoding="utf-8")
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["ok"], ap
    assert ap["verdict"] == "ITERATE" and not ap["stop"], ap
    assert ap["attacks_gate"]["refused"] is True and ap["ship_refused"] is True
    # the frozen gate did not run: no acceptance verdict record, no pack LOG line
    assert "acceptance" not in ap
    log = log_text(repo)
    assert log.count(REFUSED) == 1
    assert "acceptance gate" not in log and "SHIP gate:" not in log
    assert [s for s in commit_subjects(repo) if "acceptance gate refused" in s] == []
    assert len([s for s in commit_subjects(repo) if REFUSAL_COMMIT in s]) == 1
    # next: the attacks refusal reaches the Lead; the pack's own errors do not
    n = run("next", max_iterations=4)
    assert n["action"] == "lead" and n["iteration"] == 2, n
    assert n["ship_gate_errors"] == ap["attacks_gate"]["errors"]
    assert not n["acceptance"].get("errors"), n["acceptance"]
    # replayed apply (sealed answer): not re-judged, not re-logged
    again = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert again["verdict"] == "ITERATE" and again["replayed"] is True, again
    assert again["attacks_gate"] == ap["attacks_gate"]
    assert run("next", max_iterations=4)["ship_gate_errors"] == n["ship_gate_errors"]
    log = log_text(repo)
    assert log.count(REFUSED) == 1
    assert len([s for s in commit_subjects(repo) if REFUSAL_COMMIT in s]) == 1


def test_on_refused_ship_then_a_ship_with_attacks_runs_the_pack_and_ships(repo: Path) -> None:
    run = Run(repo)
    p, _ = to_prerun(run, repo, upto=5)
    (mbox(repo) / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n# Verdict — iteration 1\nattempt: {p['evaluator_attempt']}\n"
        f"evaluated: {p['sha']}\ncommit: {p['sha']}\n", encoding="utf-8")
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "ITERATE" and ap["attacks_gate"]["refused"], ap
    n = run("next", max_iterations=4)
    assert n["ship_gate_errors"], n
    fixing_pass(repo, 2)
    assert run("gate", role="lead", iteration=2, attempt=1)["pass"]
    assert "ship_gate" not in driver_json(repo)  # the pass is done: forgotten
    p2 = run("pin", acceptance=False, iteration=2)
    ar = run("acceptance-run", iteration=2, sha=p2["sha"])
    assert ar["passed"] == 5
    ship_verdict(repo, 2, p2)  # carries `attacks:` with a counted probe
    ap2 = run("apply", iteration=2, attempt=p2["evaluator_attempt"])
    assert ap2["verdict"] == "SHIP" and ap2["status"] == "shipped", ap2
    assert "attacks_gate" not in ap2
    assert ap2["acceptance"]["ship_gate"]["passed"] == 5  # the frozen gate ran
    assert "SHIP gate: 5/5 PASS" in log_text(repo)


# ------------------------------------------- Rule 2, requirement map gate
def first_freeze(run: Run, **pack):
    begin(run)
    n = run("next", max_iterations=4)
    assert n["ok"] and n["action"] == "lead", n
    ex = run("acceptance-export", iteration=1, attempt=1)
    assert ex["ok"] and ex["frozen"] is False, ex
    write_pack(Path(ex["export"]), **pack)
    return ex


def freeze_op(run: Run, ex: dict, attempt: int, **prior) -> dict:
    fr = run("acceptance-freeze", iteration=1, attempt=attempt, marker=ex["marker"],
             prior=prior, author={"exit": 0}, model="claude-opus-5-5")
    assert fr["ok"], fr
    return fr


def test_freeze_with_a_complete_map_has_no_requirement_gaps(repo: Path) -> None:
    run = Run(repo)
    ex = first_freeze(run)
    fr = freeze_op(run, ex, 1)
    assert fr["action"] == "frozen" and fr["requirement_gaps"] == [], fr
    assert "requirement_gaps" not in (mbox(repo) / "acceptance" / "FROZEN").read_text()


def test_freeze_retries_a_pack_missing_a_hard_check_with_the_gap_in_fatal(repo: Path) -> None:
    run = Run(repo)
    ex = first_freeze(run, requirements=requirements_map(5, gap=True))
    fr = freeze_op(run, ex, 1)
    assert fr["action"] == "retry", fr
    assert "requirements: R1 has no hard check" in fr["fatal"], fr["fatal"]
    assert fr["requirement_gaps"] == ["requirements: R1 has no hard check"]
    assert git(repo, "log", "-1", "--format=%s") == "loop: init"  # nothing frozen yet
    assert "author retry: requirements: R1 has no hard check" in log_text(repo)


def test_a_second_gapped_pack_freezes_with_requirement_gaps(repo: Path) -> None:
    run = Run(repo)
    ex = first_freeze(run, requirements=requirements_map(5, gap=True))
    assert freeze_op(run, ex, 1)["action"] == "retry"
    fr = freeze_op(run, ex, 2, retried=True)
    assert fr["action"] == "frozen" and fr["checks"] == 5, fr
    assert fr["requirement_gaps"] == ["requirements: R1 has no hard check"], fr
    assert "requirement_gaps: requirements: R1 has no hard check" in (
        mbox(repo) / "acceptance" / "FROZEN").read_text()
    assert "WARNING frozen with requirement-map gaps after the retry: 1" in log_text(repo)
    # a re-run freeze op on the frozen loop reports the same gaps
    again = freeze_op(run, ex, 3, retried=True)
    assert again["action"] == "frozen" and again["replayed"] is True
    assert again["requirement_gaps"] == fr["requirement_gaps"]


def test_a_pack_without_the_map_retries_then_freezes_with_the_missing_gap(repo: Path) -> None:
    run = Run(repo)
    ex = first_freeze(run, requirements=False)
    fr = freeze_op(run, ex, 1)
    assert fr["action"] == "retry" and any(f.startswith("requirements: missing") for f in fr["fatal"])
    fr = freeze_op(run, ex, 2, retried=True)
    assert fr["action"] == "frozen"
    assert [g.startswith("requirements: missing") for g in fr["requirement_gaps"]] == [True]


# ---------------------------------------------------- legacy pack resume
LEGACY_WARNING = ("acceptance: WARNING legacy frozen pack: no requirements map (frozen before "
                  "the requirement-map rule); hard-check coverage is not enforced for this pack")

PRE_RULE_HELPER = '''\
"""The native helper as it was before the requirement-map rule: the freeze
finds no requirement gaps, so a pack without the map freezes clean (a
legacy pack)."""
import sys
sys.path.insert(0, {native!r})
import trio_native_step as NS

_orig = NS.TL._load_sibling


def _load(name, filename):
    mod = _orig(name, filename)
    if name == "trio_acceptance" and mod is not None:
        mod.requirements_problems = lambda *a, **k: []
    return mod


NS.TL._load_sibling = _load
sys.exit(NS.main())
'''


def test_legacy_pack_resume_warns_exactly_once_across_many_ops(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "pre_rule_helper.py"
    legacy.write_text(PRE_RULE_HELPER.format(native=str(NATIVE)), encoding="utf-8")
    # 1. an old run froze a pack without a requirement map
    monkeypatch.setattr(R19, "HELPER", legacy)
    old = Run(repo)
    ex = first_freeze(old, requirements=False)
    fr = freeze_op(old, ex, 1)
    assert fr["action"] == "frozen" and fr["requirement_gaps"] == [], fr
    assert "requirement_gaps" not in (mbox(repo) / "acceptance" / "FROZEN").read_text()
    assert "requirements" not in json.loads(
        (mbox(repo) / "acceptance" / "MANIFEST.json").read_text())
    assert LEGACY_WARNING not in log_text(repo)
    assert old("end")["ok"]
    monkeypatch.setattr(R19, "HELPER", HELPER)
    # 2. the loop is resumed on the current helper: many ops, one warning
    run = Run(repo)
    b = begin(run)
    assert b["acceptance"]["status"] == "frozen", b
    assert log_text(repo).count(LEGACY_WARNING) == 1, log_text(repo)
    n = run("next", max_iterations=4)
    assert n["ok"] and n["action"] == "lead" and n["iteration"] == 1, n
    lead_pass(repo, 1)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert ar["ok"] and ar["total"] == 5, ar
    ship_verdict(repo, 1, p, "ITERATE")
    assert run("apply", iteration=1, attempt=p["evaluator_attempt"])["verdict"] == "ITERATE"
    assert run("next", max_iterations=4)["iteration"] == 2
    assert log_text(repo).count(LEGACY_WARNING) == 1
    assert run("end")["ok"]
    # 3. and resumed again: still one warning for the loop
    again = Run(repo)
    assert begin(again)["acceptance"]["status"] == "frozen"
    assert again("next", max_iterations=4)["ok"]
    assert again("end")["ok"]
    log = log_text(repo)
    assert log.count(LEGACY_WARNING) == 1 and log.count("WARNING legacy") == 1


# ------------------------------------------------- the workflow script
def plan_prompts(out: dict) -> list[str]:
    return [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-lead" and c["schemaKeys"]
            and "slices" in c["schemaKeys"]]


@needs_node
@pytest.mark.parametrize("acceptance", [False, True])
def test_workflow_ship_refused_by_the_attacks_gate_reaches_the_second_plan(acceptance: bool) -> None:
    errs = ["The driver refused the SHIP at sha1: the Evaluator listed no attack beyond the frozen checks"]
    args = dict(ACC) if acceptance else {"mailbox": ACC["mailbox"]}
    out = wf_run({"verdicts": ["SHIP", "SHIP"], "args": args, "attacks": {"refuse": [0], "errors": errs}})
    r = out["result"]
    assert r["status"] == "shipped" and len(r["iterations"]) == 2, r
    assert r["iterations"][0]["verdict"] == "ITERATE"
    assert r["iterations"][0]["attacks_gate"]["refused"] is True
    assert any("the attacks gate refused the SHIP" in line for line in out["logs"])
    plans = plan_prompts(out)
    assert len(plans) == 2
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):" not in plans[0]
    assert "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n- " + errs[0] in plans[1]
    if acceptance:
        # the attacks refusal is not the frozen gate's: acceptance keeps its meaning
        assert r["acceptance"]["ship_refused"] == []
        assert "ACCEPTANCE ERRORS FROM THE DRIVER" not in plans[1]


@needs_node
def test_workflow_logs_the_requirement_gaps_a_pack_froze_with() -> None:
    gaps = ["requirements: R1 has no hard check"]
    out = wf_run({"verdicts": ["SHIP"], "args": dict(ACC),
                  "acc": {"freeze": ["retry", "frozen"], "retry_fatal": gaps, "freeze_gaps": gaps}})
    frozen = [line for line in out["logs"] if line.startswith("acceptance: frozen ")]
    assert len(frozen) == 1 and "; requirement gaps 1" in frozen[0], out["logs"]
    authors = [c["prompt"] for c in out["calls"] if c["agentType"] == "trio-acceptance"]
    assert len(authors) == 2 and "- pack: requirements: R1 has no hard check" in authors[1]
    # a clean freeze logs no gap count
    clean = wf_run({"verdicts": ["SHIP"], "args": dict(ACC)})
    assert not any("requirement gaps" in line for line in clean["logs"])

"""eval4 (round 4) on the native side — LAB/dash/eval4/repros, inverted.

Finding 1: the pin retry allowance is unique to one script execution.
``begin`` mints a random run-execution id (``exec_id``, kept in the helper's
session state), every later step nonce carries it, and the consume/retry key
is ``native:{exec_id}:{nonce}@{iteration}``. A fresh run that reuses the run
token (or the default token, a slug of the mailbox path) and so reproduces
the same ``<token>/<seq>/pin`` sequence can never receive a consumed answer
(repros/native-nonce-replay.sh). An in-run retry and the honest answer ->
rerun delivery still work.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

from test_step_ops import HELPER, git, git_env, mbox, repo  # noqa: F401
from test_eval3 import _answer, _stopped
from test_workflow_script import needs_node, run as run_script

LEDGER = Path(__file__).resolve().parents[2] / "metrics" / "human_ledger.py"
TOKEN = "trio-native-product-loop"   # the JS default token shape (mailbox slug)


def _step(repo: Path, op: str, nonce: str, env: dict, **kw) -> dict:
    cmd = [sys.executable, str(HELPER), op, "--mailbox", str(mbox(repo)), "--token", TOKEN,
           "--nonce", nonce, "--json"]
    for key, value in kw.items():
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["nonce"] == nonce, out
    return out


def _lead_done(box: Path, iteration: int = 3) -> None:
    text = (box / "STATE.md").read_text()
    text = re.sub(r"(?m)^(status|phase|iteration|evaluator_attempt|evaluated_sha):.*\n", "", text)
    (box / "STATE.md").write_text(text + f"status: running\nphase: lead-done\niteration: {iteration}\n")


def _delivered(out: dict) -> bool:
    return "## Verified human answer (driver)" in (out.get("human_answer") or "")


def test_a_fresh_run_with_the_same_token_never_gets_a_consumed_answer(repo: Path,
                                                                      tmp_path: Path) -> None:
    """repros/native-nonce-replay.sh, inverted."""
    state = tmp_path / "state"
    env = {**git_env(), "TRIO_DASH_STATE_DIR": str(state)}
    box = _stopped(repo)
    _answer(state, box, 2, "Human check: PASSED (UI at iteration 2)")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "answer recorded")

    # run 1: begin, a Lead pass, pin at seq 5 (delivered + consumed)
    b1 = _step(repo, "begin", f"{TOKEN}/1/begin", env)
    e1 = b1["exec_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", e1), b1
    session = json.loads((box / ".session.json").read_text())
    assert session["exec_id"] == e1 and session["session"] == TOKEN
    (repo / "app.txt").write_text("slice a\n")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-q", "-m", "slice(a): lead work")
    _lead_done(box)
    pin1 = _step(repo, "pin", f"{TOKEN}/{e1}/5/pin", env, iteration=3)
    assert _delivered(pin1), pin1
    retry = _step(repo, "pin", f"{TOKEN}/{e1}/5/pin", env, iteration=3)
    assert retry["human_answer"] == pin1["human_answer"]          # in-run retry still works
    assert not _delivered(_step(repo, "pin", f"{TOKEN}/{e1}/6/pin", env, iteration=3))
    # Evaluator 1 rules ITERATE (left uncommitted, as native does), apply, end
    (box / "VERDICT.md").write_text(
        f"VERDICT: ITERATE\niteration: 3\nattempt: {pin1['evaluator_attempt']}\n"
        f"evaluated: {pin1['sha']}\ncriterion 2 failed\n")
    applied = _step(repo, "apply", f"{TOKEN}/{e1}/7/apply", env, iteration=3,
                    attempt=pin1["evaluator_attempt"])
    assert applied["ok"], applied
    assert _step(repo, "end", f"{TOKEN}/{e1}/8/end", env)["ok"]

    # attacker (mailbox + repo content only)
    with open(repo / "app.txt", "a") as fh:
        fh.write("evil\n")
    git(repo, "commit", "-q", "-m", "slice(b): unreviewed change", "--", "app.txt")
    old = subprocess.run(["git", "-C", str(repo), "show", "HEAD~2:loop/VERDICT.md"],
                         capture_output=True, check=True).stdout
    (box / "VERDICT.md").write_bytes(old)
    _lead_done(box)

    # fresh run 2, same (default) token: the same seq sequence
    b2 = _step(repo, "begin", f"{TOKEN}/1/begin", env)
    e2 = b2["exec_id"]
    assert b2["ok"] and e2 != e1
    for nonce in (f"{TOKEN}/{e2}/3/pin", f"{TOKEN}/{e2}/5/pin",
                  f"{TOKEN}/{e1}/5/pin",        # run 1's exact nonce, replayed
                  f"{TOKEN}/5/pin"):            # the round-3 shape
        out = _step(repo, "pin", nonce, env, iteration=3)
        assert out["ok"] and out["human_answer"] == "", (nonce, out)
        assert any("consumed" in n for n in out["human_notes"]), out["human_notes"]
    # even a restored run-1 session record does not reopen the allowance for run 2's nonces
    session = json.loads((box / ".session.json").read_text())
    (box / ".session.json").write_text(json.dumps({**session, "exec_id": e1}))
    out = _step(repo, "pin", f"{TOKEN}/{e2}/5/pin", env, iteration=3)
    assert out["human_answer"] == "", out
    _step(repo, "end", f"{TOKEN}/{e2}/9/end", env)
    lines = [json.loads(x) for x in (state / "consumed.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["key"] == f"native:{e1}:{TOKEN}/{e1}/5/pin@3"


def test_each_begin_mints_a_new_exec_id_and_steps_keep_it(repo: Path, tmp_path: Path) -> None:
    env = {**git_env(), "TRIO_DASH_STATE_DIR": str(tmp_path / "state")}
    ids = set()
    for i in range(3):
        b = _step(repo, "begin", f"{TOKEN}/1/begin", env)
        ids.add(b["exec_id"])
        n = _step(repo, "next", f"{TOKEN}/{b['exec_id']}/2/next", env, max_iterations=3)
        assert n["ok"], n
        assert json.loads((mbox(repo) / ".session.json").read_text())["exec_id"] == b["exec_id"]
        _step(repo, "end", f"{TOKEN}/{b['exec_id']}/3/end", env)
    assert len(ids) == 3


def test_the_honest_answer_rerun_still_delivers(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    env = {**git_env(), "TRIO_DASH_STATE_DIR": str(state)}
    box = _stopped(repo)
    _answer(state, box, 2)
    (box / "STATE.md").write_text("iteration: 2\nstatus: running\nphase: idle\n")
    e = _step(repo, "begin", f"{TOKEN}/1/begin", env)["exec_id"]
    n = _step(repo, "next", f"{TOKEN}/{e}/2/next", env, max_iterations=4)
    assert n["iteration"] == 3 and "> Human check: PASSED" in n["human_answer"]
    (repo / "app.py").write_text("print('v2')\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "slice(a): apply the human answer")
    _lead_done(box)
    p = _step(repo, "pin", f"{TOKEN}/{e}/4/pin", env, iteration=3)
    assert p["human_answer"] == n["human_answer"]
    assert _step(repo, "pin", f"{TOKEN}/{e}/4/pin", env, iteration=3)["human_answer"] == p["human_answer"]


def test_only_execution_unique_keys_are_retry_eligible() -> None:
    spec = importlib.util.spec_from_file_location("t4_ledger", LEDGER)
    lg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lg)
    e = "0123456789abcdef0123456789abcdef"
    assert lg.retry_eligible(f"native:{e}:tok/{e}/5/pin@3")
    for key in ("", "native:tok/5/pin@3", f"native:{e[:31]}:x@3", f"native:{e}:@3",
                f"native:{e}:x", f"native:{e}:a\nb@3", None, 5):
        assert not lg.retry_eligible(key), key


def _harness(scenario: dict) -> dict:
    return run_script(scenario)


@needs_node
def test_script_nonces_carry_the_exec_id_after_begin() -> None:
    e = "fedcba9876543210fedcba9876543210"
    out = _harness({"verdicts": ["SHIP"], "exec_id": e})
    assert out["result"]["status"] == "shipped"
    steps = [re.search(r"nonce=([^)]+)\)", c["prompt"]).group(1)
             for c in out["calls"] if c["agentType"] == "trio-step"]
    assert steps[0].endswith("/1/begin") and e not in steps[0]
    assert all(f"/{e}/" in s for s in steps[1:]) and len(steps) > 3
    assert any(s.endswith("/pin") for s in steps)


@needs_node
def test_script_refuses_a_begin_without_an_exec_id() -> None:
    for scenario in ({"verdicts": ["SHIP"], "no_exec_id": True},
                     {"verdicts": ["SHIP"], "exec_id": "not-hex"}):
        out = _harness(scenario)
        assert out["result"]["status"] == "error", out["result"]
        ops = [re.search(r"op=(\w+)", c["prompt"]).group(1)
               for c in out["calls"] if c["agentType"] == "trio-step"]
        assert "pin" not in ops and ops[-1] == "end", ops

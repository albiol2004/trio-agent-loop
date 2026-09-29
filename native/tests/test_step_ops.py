"""trio_native_step.py ops on real-git fixture mailboxes (lockstep v0)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parents[1]
HELPER = NATIVE / "trio_native_step.py"
TOKEN = "t-run1"

PLAN = """\
# Plan
```yaml
slices:
  - id: app
    writes: [app.py]
    reads: []
```
"""


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-test", GIT_COMMITTER_EMAIL="t@example.test",
        TRIO_RETIREMENT_WAIT_SECONDS="0",
        TRIO_NATIVE_HOLDER_PID=str(os.getpid()),
    )
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=git_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README").write_text("x\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip app.py\n", encoding="utf-8")
    (box / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\ncustom: keep\n",
        encoding="utf-8",
    )
    (box / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    git(root, "add", "loop")
    git(root, "commit", "-q", "-m", "loop: init")
    return root


def mbox(repo: Path) -> Path:
    return repo / "loop"


_SEQ = [0]


def step(repo: Path, op: str, *, token: str = TOKEN, env=None, **kw) -> dict:
    _SEQ[0] += 1
    nonce = f"{token}/{_SEQ[0]}/{op}"
    cmd = [sys.executable, str(HELPER), op, "--mailbox", str(mbox(repo)),
           "--token", token, "--nonce", nonce, "--json"]
    for key, value in kw.items():
        cmd += [f"--{key.replace('_', '-')}", str(value)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=env or git_env())
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["nonce"] == nonce and out["op"] == op
    return out


def same(a: dict, b: dict) -> bool:
    strip = lambda d: {k: v for k, v in d.items() if k != "nonce"}
    return strip(a) == strip(b)


def lead_pass(repo: Path, iteration: int, *, role: str = "lead",
              commit: bool = True, log: bool = True, body: str = "") -> None:
    if commit:
        (repo / "app.py").write_text(f"print({iteration}){body}\n",
                                     encoding="utf-8")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", f"slice(app): pass {iteration}")
    if log:
        with (mbox(repo) / "LOG.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- iter {iteration} | {role} | done\n")


def write_verdict(repo: Path, first: str, iteration: int, pin: dict,
                  extra: str = "") -> None:
    (mbox(repo) / "VERDICT.md").write_text(
        f"{first}\n# Verdict — iteration {iteration}\n"
        f"attempt: {pin['evaluator_attempt']}\nevaluated: {pin['sha']}\n"
        + extra,
        encoding="utf-8",
    )


def retire(repo: Path, iteration: int, pin: dict) -> None:
    write_verdict(repo, "VERDICT: SHIP", iteration, pin,
                  f"commit: {pin['sha']}\n")
    git(repo, "add", "loop/VERDICT.md")
    git(repo, "commit", "-q", "-m", f"loop: iteration {iteration} — SHIP")


def to_lead_done(repo: Path, iteration: int = 1) -> dict:
    n = step(repo, "next", max_iterations=4)
    assert n["ok"] and n["action"] == "lead" and n["iteration"] == iteration
    lead_pass(repo, iteration)
    g = step(repo, "gate", role="lead", iteration=iteration, attempt=1)
    assert g["ok"] and g["pass"], g
    p = step(repo, "pin", iteration=iteration)
    assert p["ok"], p
    return p


def state(repo: Path) -> str:
    return (mbox(repo) / "STATE.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ begin
def test_begin_adds_exclude_once_and_is_idempotent(repo: Path) -> None:
    first = step(repo, "begin")
    again = step(repo, "begin")
    assert first["ok"] and first["mode"] == "lockstep"
    assert same(first, again)
    exclude = Path(first["exclude_path"])
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".claude/worktrees/") == 1
    lock = mbox(repo) / ".lock"
    assert (lock / "owner").read_text().strip() == f"workflow:{TOKEN}"
    assert (lock / "pid").read_text().strip() == str(os.getpid())
    session = json.loads((mbox(repo) / ".session.json").read_text())
    assert session["driver"] == "claude-workflow" and not session["done"]
    # A worktree dir under .claude/worktrees/ is not untracked product.
    (repo / ".claude" / "worktrees" / "b1").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "b1" / "f.py").write_text("x")
    assert ".claude" not in git(repo, "status", "--porcelain")


def test_begin_refuses_open_loop_and_missing_goal(repo: Path) -> None:
    (mbox(repo) / "QUEUE.md").write_text("q\n")
    out = step(repo, "begin")
    assert not out["ok"] and "QUEUE.md" in out["error"]
    (mbox(repo) / "QUEUE.md").unlink()
    (mbox(repo) / "GOAL.md").unlink()
    out = step(repo, "begin")
    assert not out["ok"] and "GOAL.md" in out["error"]


def test_lock_refuses_other_token_until_holder_dead(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    other = step(repo, "begin", token="t-other")
    assert not other["ok"] and "locked" in other["error"]
    # ops without the lock are refused (nonce still echoed)
    refused = step(repo, "next", token="t-other", max_iterations=4)
    assert not refused["ok"] and "lock not held" in refused["error"]
    # holder gone -> takeover
    (mbox(repo) / ".lock" / "pid").write_text(f"{dead_pid()}\n")
    taken = step(repo, "begin", token="t-other")
    assert taken["ok"] and taken["lock_owner"] == "workflow:t-other"


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def live_pid():
    proc = subprocess.Popen(["sleep", "60"])
    yield proc.pid
    proc.kill()
    proc.wait()


def env_pid(pid: int) -> dict[str, str]:
    env = git_env()
    env["TRIO_NATIVE_HOLDER_PID"] = str(pid)
    return env


def lock_file(repo: Path, name: str) -> str:
    return (mbox(repo) / ".lock" / name).read_text().strip()


def load_trio_loop():
    spec = importlib.util.spec_from_file_location(
        "trio_loop_for_test", NATIVE.parent / "metrics" / "trio_loop.py")
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    return tl


def test_same_default_token_two_live_runs_refused(repo: Path,
                                                   live_pid: int) -> None:
    """F1: a second live launch with the same token is not an owner."""
    first = step(repo, "begin")
    assert first["ok"] and lock_file(repo, "pid") == str(os.getpid())
    second = step(repo, "begin", env=env_pid(live_pid))
    assert not second["ok"] and "same run_token" in second["error"]
    assert lock_file(repo, "pid") == str(os.getpid())  # untouched
    refused = step(repo, "next", env=env_pid(live_pid), max_iterations=4)
    assert not refused["ok"] and "another live process" in refused["error"]
    assert "iteration: 0" in state(repo)  # no bump by the second launch
    # the second launch's end never releases the first run's lock
    assert step(repo, "end", env=env_pid(live_pid))["lock"] == "foreign"
    assert lock_file(repo, "owner") == f"workflow:{TOKEN}"
    # the first run is unaffected
    assert step(repo, "next", max_iterations=4)["action"] == "lead"


def test_journal_resume_restamps_pid(repo: Path) -> None:
    """F2: begin replayed from the journal; the first live op re-stamps."""
    gone = dead_pid()
    assert step(repo, "begin", env=env_pid(gone))["ok"]
    assert lock_file(repo, "pid") == str(gone)
    # new Claude process (this pytest pid) resumes: begin is a cache hit,
    # `next` is the first op that really runs.
    n = step(repo, "next", max_iterations=4)
    assert n["ok"] and n["action"] == "lead"
    assert lock_file(repo, "pid") == str(os.getpid())
    tl = load_trio_loop()
    assert tl._acquire_lock(mbox(repo)) is None  # trio_loop stays out
    assert lock_file(repo, "owner") == f"workflow:{TOKEN}"
    # a fresh run with the same token in a new process after a crash
    (mbox(repo) / ".lock" / "pid").write_text(f"{dead_pid()}\n")
    again = step(repo, "begin")
    assert again["ok"] and lock_file(repo, "pid") == str(os.getpid())


def test_every_op_refreshes_heartbeat(repo: Path) -> None:
    """F11: pid + heartbeat are re-stamped by each op, not only begin."""
    step(repo, "begin")
    hb = mbox(repo) / ".lock" / "heartbeat"
    for op, kw in (("next", {"max_iterations": 4}),
                   ("gate", {"role": "lead", "iteration": 1, "attempt": 1}),
                   ("gate", {"role": "lead", "iteration": 1, "attempt": 1})):
        hb.write_text("1000.0\n")
        (mbox(repo) / ".lock" / "pid").write_text(f"{dead_pid()}\n")
        assert step(repo, op, **kw)["ok"]
        assert float(hb.read_text()) > 1000.0
        assert lock_file(repo, "pid") == str(os.getpid())


def test_stale_heartbeat_takeover_fails_closed(repo: Path,
                                               live_pid: int) -> None:
    step(repo, "begin", env=env_pid(live_pid))
    step(repo, "next", env=env_pid(live_pid), max_iterations=4)
    (mbox(repo) / ".lock" / "heartbeat").write_text(
        f"{__import__('time').time() - 5 * 3600:.3f}\n")
    taken = step(repo, "begin", token="t-other")
    assert taken["ok"] and lock_file(repo, "owner") == "workflow:t-other"
    old = step(repo, "gate", env=env_pid(live_pid), role="lead",
               iteration=1, attempt=1)
    assert not old["ok"] and "lock not held" in old["error"]
    assert step(repo, "end", env=env_pid(live_pid))["lock"] == "foreign"
    assert lock_file(repo, "owner") == "workflow:t-other"


def test_lock_respects_live_trio_loop_driver(repo: Path) -> None:
    lock = mbox(repo) / ".lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "owner").write_text("abc\n")
    out = step(repo, "begin")
    assert not out["ok"] and "locked" in out["error"]


# --------------------------------------------------------------- lockstep
def test_lockstep_ship(repo: Path) -> None:
    b = step(repo, "begin")
    assert b["iteration"] == 0 and b["repo"] == str(repo)
    p = to_lead_done(repo)
    assert p["sha"] == git(repo, "rev-parse", "HEAD")
    assert p["context_block"].startswith(
        f"LOCKSTEP CONTEXT: attempt={p['evaluator_attempt']} sha={p['sha']}"
    )
    assert "MAILBOX OVERRIDE" in p["context_block"]
    assert p["skip_evaluator"] is False
    retire(repo, 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["ok"] and a["verdict"] == "SHIP" and a["stop"]
    assert a["status"] == "shipped" and a["code"] == 0 and a["bound"]
    assert a["commit_shas"] == [p["sha"]]
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "stop" and n["status"] == "shipped" and n["code"] == 0
    e = step(repo, "end")
    assert e["ok"] and e["lock"] == "released" and e["dangling_worktrees"] == []
    assert not (mbox(repo) / ".lock").exists()
    assert json.loads((mbox(repo) / ".session.json").read_text())["done"]
    assert "custom: keep" in state(repo)


def test_ship_without_retirement_is_needs_retirement(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: SHIP", 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["stop"] and a["status"] == "needs_retirement" and a["code"] == 6
    # resume: the retirement commit lands later; next finalizes, no role.
    git(repo, "add", "loop/VERDICT.md")
    git(repo, "commit", "-q", "-m", "loop: iteration 1 — SHIP")
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "stop" and n["status"] == "shipped" and n["code"] == 0


def test_iterate_runs_next_lead_iteration(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: ITERATE", 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["ok"] and not a["stop"] and a["next_role"] == "lead"
    assert a["status"] == "running" and a["phase"] == "idle"
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "lead" and n["iteration"] == 2 and n["attempt"] == 1
    assert "evaluator_attempt: \n" in state(repo)  # attempt cleared per pass


def test_scoped_iterate_routes_repair(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: ITERATE scope=local:app.py", 1, p)
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["next_role"] == "repair" and a["scope"] == "local:app.py"
    assert (mbox(repo) / ".repairs").read_text().strip() == "1"
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "repair" and n["iteration"] == 2
    assert n["scope"] == "local:app.py"
    # a `| lead |` line does not satisfy the repair LOG gate
    lead_pass(repo, 2, role="lead")
    g = step(repo, "gate", role="repair", iteration=2, attempt=1)
    assert not g["pass"] and not g["final"]
    assert any("repair" in f for f in g["failures"])
    lead_pass(repo, 2, role="repair", commit=False)
    g2 = step(repo, "gate", role="repair", iteration=2, attempt=2)
    assert g2["pass"] and g2["phase"] == "lead-done"


def test_repair_cap_forces_full_lead(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    for it in (1, 2, 3):
        write_verdict(repo, "VERDICT: ITERATE scope=local:app.py", it, p)
        a = step(repo, "apply", iteration=it, attempt=p["evaluator_attempt"])
        n = step(repo, "next", max_iterations=9)
        expected = "repair" if it < 3 else "lead"
        assert n["action"] == expected, (it, n)
        lead_pass(repo, it + 1, role=expected, body=f"#{it}")
        assert step(repo, "gate", role=expected, iteration=it + 1,
                    attempt=1)["pass"]
        p = step(repo, "pin", iteration=it + 1)
    assert "repair cap hit" in (mbox(repo) / "LOG.md").read_text()


def test_gate_failure_retry_then_error(repo: Path) -> None:
    step(repo, "begin")
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "lead"
    lead_pass(repo, 1, commit=False)  # LOG line only: commit gate fails
    g1 = step(repo, "gate", role="lead", iteration=1, attempt=1)
    assert g1["ok"] and not g1["pass"] and not g1["final"]
    assert any("commit gate" in f for f in g1["failures"])
    assert any("slice 'app'" in d for d in g1["detail"])
    # resume (fresh run): next returns the same role at attempt 2
    n2 = step(repo, "next", max_iterations=4)
    assert (n2["action"], n2["iteration"], n2["attempt"]) == ("lead", 1, 2)
    g2 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    assert not g2["pass"] and g2["final"] and g2["status"] == "error"
    log = (mbox(repo) / "LOG.md").read_text()
    assert log.count("gate breach after lead") == 1
    stop = step(repo, "next", max_iterations=4)
    assert stop["action"] == "stop" and stop["status"] == "error"
    assert stop["code"] == 3


def test_gate_retry_passes_on_second_attempt(repo: Path) -> None:
    step(repo, "begin")
    step(repo, "next", max_iterations=4)
    lead_pass(repo, 1, commit=False)
    assert not step(repo, "gate", role="lead", iteration=1, attempt=1)["pass"]
    lead_pass(repo, 1, log=False)
    g2 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    assert g2["pass"] and g2["phase"] == "lead-done"
    assert step(repo, "next", max_iterations=4)["action"] == "evaluate"


@pytest.mark.parametrize(
    "word,status,code", [("NEEDS_HUMAN", "needs_human", 5),
                         ("BLOCKED", "blocked", 2)],
)
def test_needs_human_and_blocked_stop(repo: Path, word, status, code) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, f"VERDICT: {word}", 1, p,
                  "\n## Human check\n1. open the app\n2. confirm\n")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["stop"] and a["status"] == status and a["code"] == code
    assert a["human_check"].startswith("1. open the app")
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "stop" and n["status"] == status and n["code"] == code


def test_unparseable_verdict_is_error(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    (mbox(repo) / "VERDICT.md").write_text("looks fine to me\n")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["stop"] and a["status"] == "error" and a["code"] == 3
    assert "unparseable verdict" in (mbox(repo) / "LOG.md").read_text()


def test_max_iterations_stop(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: ITERATE", 1, p)
    step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    n = step(repo, "next", max_iterations=1)
    assert n["action"] == "stop" and n["status"] == "max_iterations"
    assert n["code"] == 4


def test_apply_and_pin_refuse_wrong_binding(repo: Path) -> None:
    step(repo, "begin")
    step(repo, "next", max_iterations=4)
    early = step(repo, "pin", iteration=1)
    assert not early["ok"] and "lead-done" in early["error"]
    lead_pass(repo, 1)
    step(repo, "gate", role="lead", iteration=1, attempt=1)
    p = step(repo, "pin", iteration=1)
    bad = step(repo, "apply", iteration=1, attempt="deadbeef")
    assert not bad["ok"] and "evaluator_attempt" in bad["error"]
    assert p["ok"]


def test_apply_refuses_stale_unbound_verdict(repo: Path) -> None:
    """F7: iteration 2's Evaluator writes nothing; iteration 1's ITERATE
    is not re-applied."""
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: ITERATE scope=local:app.py", 1, p)
    assert step(repo, "apply", iteration=1,
                attempt=p["evaluator_attempt"])["next_role"] == "repair"
    n = step(repo, "next", max_iterations=4)
    lead_pass(repo, 2, role="repair", body="#2")
    assert step(repo, "gate", role="repair", iteration=2, attempt=1)["pass"]
    p2 = step(repo, "pin", iteration=2)
    assert n["action"] == "repair" and p2["skip_evaluator"] is False
    log_before = (mbox(repo) / "LOG.md").read_text()
    stale = step(repo, "apply", iteration=2, attempt=p2["evaluator_attempt"])
    assert not stale["ok"] and "not bound" in stale["error"]
    assert "iteration: 2" in state(repo) and "phase: lead-done" in state(repo)
    assert (mbox(repo) / ".repairs").read_text().strip() == "1"
    assert (mbox(repo) / "LOG.md").read_text() == log_before
    # a fresh run re-pins (same attempt) and the Evaluator is re-dispatched
    again = step(repo, "pin", iteration=2)
    assert again["evaluator_attempt"] == p2["evaluator_attempt"]
    assert again["skip_evaluator"] is False
    write_verdict(repo, "VERDICT: ITERATE", 2, again)
    ok = step(repo, "apply", iteration=2, attempt=again["evaluator_attempt"])
    assert ok["ok"] and ok["bound"] and ok["next_role"] == "lead"


def test_apply_refuses_unbound_ship(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    (mbox(repo) / "VERDICT.md").write_text(
        "VERDICT: SHIP\n# Verdict — iteration 1\nattempt: someone-else\n")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not a["ok"] and "not bound" in a["error"]
    assert "status: running" in state(repo)


# ------------------------------------------------------------ idempotency
def test_replay_every_op_gives_same_answer(repo: Path) -> None:
    b1, b2 = step(repo, "begin"), step(repo, "begin")
    assert same(b1, b2)
    n1, n2 = step(repo, "next", max_iterations=4), step(repo, "next",
                                                       max_iterations=4)
    assert same(n1, n2) and n1["iteration"] == 1  # no double bump
    lead_pass(repo, 1, commit=False)
    f1 = step(repo, "gate", role="lead", iteration=1, attempt=1)
    lead_pass(repo, 1, log=False)  # fixing afterwards does not rewrite #1
    f2 = step(repo, "gate", role="lead", iteration=1, attempt=1)
    assert same(f1, f2) and not f2["pass"]
    g1 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    g2 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    assert same(g1, g2) and g1["pass"]
    p1, p2 = step(repo, "pin", iteration=1), step(repo, "pin", iteration=1)
    assert same(p1, p2)
    write_verdict(repo, "VERDICT: ITERATE scope=local:app.py", 1, p1)
    a1 = step(repo, "apply", iteration=1, attempt=p1["evaluator_attempt"])
    log_before = (mbox(repo) / "LOG.md").read_text()
    a2 = step(repo, "apply", iteration=1, attempt=p1["evaluator_attempt"])
    assert same(a1, a2)
    assert (mbox(repo) / ".repairs").read_text().strip() == "1"  # once
    assert (mbox(repo) / "LOG.md").read_text() == log_before
    # the gate record of a finished pass is replayed after state moved on
    assert same(g1, step(repo, "gate", role="lead", iteration=1, attempt=2))
    e1, e2 = step(repo, "end"), step(repo, "end")
    assert same(e1, e2) and e1["lock"] == "released"


def test_replay_of_final_gate_failure_logs_once(repo: Path) -> None:
    step(repo, "begin")
    step(repo, "next", max_iterations=4)
    lead_pass(repo, 1, commit=False)
    step(repo, "gate", role="lead", iteration=1, attempt=1)
    e1 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    e2 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    assert same(e1, e2) and e1["final"]
    assert (mbox(repo) / "LOG.md").read_text().count("gate breach") == 1


def test_pin_after_evaluator_reports_skip(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    write_verdict(repo, "VERDICT: ITERATE", 1, p)
    again = step(repo, "pin", iteration=1)
    assert again["evaluator_attempt"] == p["evaluator_attempt"]
    assert again["sha"] == p["sha"] and again["skip_evaluator"] is True


def test_reinitialized_mailbox_drops_stale_records(repo: Path) -> None:
    step(repo, "begin")
    step(repo, "next", max_iterations=4)
    lead_pass(repo, 1, commit=False)
    step(repo, "gate", role="lead", iteration=1, attempt=1)
    step(repo, "end")
    (mbox(repo) / "STATE.md").write_text("iteration: 0\nstatus: ready\n"
                                         "phase: idle\n")
    step(repo, "begin")
    n = step(repo, "next", max_iterations=4)
    assert n["attempt"] == 1  # the old iteration-1 failure is gone


def test_nonce_echo_on_every_op_and_error(repo: Path, tmp_path: Path) -> None:
    for op, kw in (("begin", {}), ("next", {"max_iterations": 2}),
                   ("gate", {"role": "lead", "iteration": 9, "attempt": 1}),
                   ("pin", {"iteration": 9}),
                   ("apply", {"iteration": 9, "attempt": "x"}),
                   ("end", {})):
        out = step(repo, op, **kw)  # step() asserts nonce + op echo
        assert "ok" in out
    missing = subprocess.run(
        [sys.executable, str(HELPER), "next", "--mailbox",
         str(tmp_path / "nope"), "--token", TOKEN, "--nonce", "N-1"],
        capture_output=True, text=True, env=git_env(),
    )
    out = json.loads(missing.stdout)
    assert out["nonce"] == "N-1" and not out["ok"]


def test_mailbox_resumable_by_trio_loop(repo: Path) -> None:
    """The helper's STATE cursor is trio_loop's: run_loop finishes it."""
    step(repo, "begin")
    to_lead_done(repo)
    step(repo, "end")
    spec = importlib.util.spec_from_file_location(
        "trio_loop_for_test", NATIVE.parent / "metrics" / "trio_loop.py")
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    roles = []

    class Runner:
        def run(self, role, iteration, mailbox, context=None):
            roles.append(role)
            text = (f"VERDICT: BLOCKED\n# Verdict — iteration {iteration}\n"
                    f"attempt: {context['evaluator_attempt']}\n")
            (Path(mailbox) / "VERDICT.md").write_text(text)
            return 0

    assert tl.run_loop(mbox(repo), 4, Runner(), repo=repo) == 2
    assert roles == ["evaluator"]

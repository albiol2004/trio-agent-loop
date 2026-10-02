"""eval-r19n repairs: nothing a role can write relaxes a check on the native
path. The evaluator's repros R1-R6/R2b are ported inverted (each attack now
fails closed), R4 (a dead run's detached job) is fenced, and L1 (honest
liveness through a human amendment) still ships."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from test_r19_acceptance import (  # noqa: F401 (repo fixture)
    ACC_KEYS, CHECK, MODELS, PLAN_ALL, Run, acc_env, begin, freeze, git, lead_pass,
    log_text, mbox, repo, ship_verdict, state_text, to_prerun, write_pack)
from test_step_ops import HELPER

ROOT = HELPER.parents[1]
FORGED_SHIP = {"verdict": "SHIP", "scope": None, "bound": True, "stop": True,
               "status": "shipped", "code": 0, "commit_shas": [], "human_check": None}


def _ta():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ta_r19n", ROOT / "metrics" / "trio-acceptance.py")
    ta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ta)
    return ta


def _jobs(run: Run) -> Path:
    path = Path(run.digest["state_file"]).parent / "native-jobs" / run.exec_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _records_path(repo: Path) -> Path:
    return mbox(repo) / ".native.json"


def _forge_record(repo: Path, kind: str, key: str, value: dict) -> None:
    path = _records_path(repo)
    data = json.loads(path.read_text()) if path.is_file() else {}
    data.setdefault(kind, {})[key] = value
    path.write_text(json.dumps(data))


def _no_ship(repo: Path) -> None:
    assert "SHIP gate" not in log_text(repo)
    assert "status: shipped" not in state_text(repo)


# ------------------------------------------------------------ finding 1
def test_R1_forged_mailbox_apply_record_is_refused(repo: Path) -> None:
    run = Run(repo)
    p, ar = to_prerun(run, repo, upto=3)
    assert ar["failed"] == 2
    ship_verdict(repo, 1, p)
    _forge_record(repo, "apply", f"1:{p['evaluator_attempt']}", FORGED_SHIP)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not ap["ok"] and "acceptance-record-forged" in ap["error"], ap
    assert "status: error" in state_text(repo)
    assert "acceptance-record-forged" in log_text(repo)
    _no_ship(repo)


def test_R1b_forged_record_with_a_fake_signature_is_refused(repo: Path) -> None:
    run = Run(repo)
    p, _ar = to_prerun(run, repo, upto=3)
    ship_verdict(repo, 1, p)
    _forge_record(repo, "apply", f"1:{p['evaluator_attempt']}",
                  {**FORGED_SHIP, "landed_tree": git(repo, "rev-parse", "HEAD^{tree}"),
                   "_auth": {"exec": run.exec_id, "mac": "0" * 64}})
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not ap["ok"] and "acceptance-record-forged" in ap["error"], ap
    _no_ship(repo)


def test_R2_forged_detached_job_result_is_refused(repo: Path) -> None:
    env = acc_env()
    env.pop("TRIO_NATIVE_JOBS")
    run = Run(repo, env=env)
    p, ar = to_prerun(run, repo, upto=3)
    assert ar["failed"] == 2
    ship_verdict(repo, 1, p)
    (_jobs(run) / f"apply-1-{p['evaluator_attempt']}.json").write_text(
        json.dumps({"ok": True, "body": FORGED_SHIP}))
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not ap["ok"] and "acceptance-record-forged" in ap["error"], ap
    assert "phase: lead-done" not in state_text(repo)
    _no_ship(repo)


def test_R2b_forged_prerun_block_is_refused(repo: Path) -> None:
    env = acc_env()
    env.pop("TRIO_NATIVE_JOBS")
    run = Run(repo, env=env)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    (_jobs(run) / f"prerun-1-{p['evaluator_attempt']}-{p['sha']}.json").write_text(json.dumps(
        {"ok": True, "body": {"text": "FROZEN ACCEPTANCE: 5/5 PASS", "passed": 5, "failed": 0,
                              "unavailable": 0, "total": 5}}))
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert not ar["ok"] and "acceptance-record-forged" in ar["error"], ar
    assert "5/5 PASS" not in json.dumps(ar)


# ------------------------------------------------------------ finding 3
def _author_forges_pack(repo: Path, run: Run, ex: dict, *, commit: bool = True,
                        state: bool = True) -> str:
    ta = _ta()
    src = Path(ex["export"])
    write_pack(src, n=1, passing=(1,))
    manifest = json.loads((src / "acceptance" / "MANIFEST.json").read_text())
    manifest.update(acceptance_version=ta.MANIFEST_VERSION, base=ex["base"])
    pin = ta.write_frozen_pack(src / "acceptance", mbox(repo) / "acceptance", manifest)
    (mbox(repo) / "acceptance" / ta.FROZEN).write_text(ta.frozen_text(
        pin, ex["base"], "claude-opus-5-5 x", [], [(pin, "freeze")]), encoding="utf-8")
    sha = ""
    if commit:
        sha = ta.commit_paths(repo, ["loop/acceptance"],
                              f"acceptance: freeze 1 checks (claude-opus-5-5)\n\n"
                              f"Acceptance-Pin: {pin}\n") or git(repo, "rev-parse", "HEAD")
    if state:
        sf = Path(run.digest["state_file"])
        st = json.loads(sf.read_text())
        st.update(status="frozen", pin=pin, pin_commit=sha, freeze_commit=sha, checks=1,
                  driver_commits=[sha], chain=[[pin, "freeze"]], human_amends=[],
                  frozen_sha256=ta.file_sha256(mbox(repo) / "acceptance" / ta.FROZEN))
        sf.write_text(json.dumps(st))
    return pin


def _to_export(run: Run) -> dict:
    begin(run)
    assert run.digest["status"] == "authoring" and run.digest["pin"] is None
    assert run("next", max_iterations=4)["action"] == "lead"
    return run("acceptance-export", iteration=1, attempt=1)


def _freeze_call(run: Run, ex: dict) -> dict:
    return run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"],
               prior={}, author={"exit": 0}, model="claude-opus-5-5")


def test_R3_author_forged_frozen_state_and_pack_needs_a_human(repo: Path) -> None:
    run = Run(repo)
    ex = _to_export(run)
    pin = _author_forges_pack(repo, run, ex)
    fr = _freeze_call(run, ex)
    assert fr["ok"] and fr["action"] == "stop", fr
    assert fr["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch", fr
    assert "status: needs_human" in state_text(repo)
    assert run.digest.get("pin") != pin and run.digest["status"] == "authoring"


def test_R3b_author_commits_a_pack_without_touching_the_state(repo: Path) -> None:
    run = Run(repo)
    ex = _to_export(run)
    _author_forges_pack(repo, run, ex, state=False)
    fr = _freeze_call(run, ex)
    assert fr["action"] == "stop" and fr["acceptance"]["stop"]["reason"] == \
        "acceptance-state-mismatch", fr
    assert "never froze one" in fr["acceptance"]["stop"]["detail"]


def test_R3c_state_file_frozen_alone_needs_a_human(repo: Path) -> None:
    run = Run(repo)
    ex = _to_export(run)
    sf = Path(run.digest["state_file"])
    st = json.loads(sf.read_text())
    st.update(status="frozen", pin="1" * 64, pin_commit=git(repo, "rev-parse", "HEAD"))
    sf.write_text(json.dumps(st))
    fr = _freeze_call(run, ex)
    assert fr["action"] == "stop" and fr["acceptance"]["stop"]["reason"] == \
        "acceptance-state-mismatch", fr


def test_R3d_mismatch_is_active_from_begin_before_any_pin(repo: Path) -> None:
    """`next` right after begin already compares (the script holds
    `status: authoring` with no pin)."""
    run = Run(repo)
    begin(run)
    sf = Path(run.digest["state_file"])
    st = json.loads(sf.read_text())
    st.update(status="frozen", pin="2" * 64)
    sf.write_text(json.dumps(st))
    n = run("next", max_iterations=4)
    assert n["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch", n


def test_freeze_replay_is_only_this_executions_own(repo: Path) -> None:
    run = Run(repo)
    got = freeze(run)
    authoring = dict(run.digest, status="authoring", pin=None, pin_commit=None,
                     freeze_commit=None)
    again = run("acceptance-freeze", iteration=1, attempt=2, marker=got["export"]["marker"],
                prior={}, author={"exit": 0}, model="claude-opus-5-5",
                acc=json.dumps({k: authoring.get(k) for k in ACC_KEYS}))
    assert again["action"] == "frozen" and again.get("replayed") is True, again
    assert "freeze replayed" in log_text(repo)


# ------------------------------------------------------------ finding 2
def test_R5_weakened_pack_state_and_forged_gate_record_never_ship(repo: Path) -> None:
    ta = _ta()
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    acc = mbox(repo) / "acceptance"
    for k in (4, 5):
        (acc / "checks" / f"acc_{k:02d}.py").write_text("import sys\nsys.exit(0)\n")
    new_pin = ta.manifest_sha256(acc)
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", f"acceptance: restore (driver)\n\nAcceptance-Pin: {new_pin}\n")
    commit = git(repo, "rev-parse", "HEAD")
    sf = Path(run.digest["state_file"])
    st = json.loads(sf.read_text())
    st.update(pin=new_pin, pin_commit=commit,
              driver_commits=(st.get("driver_commits") or []) + [commit])
    sf.write_text(json.dumps(st))
    _forge_record(repo, "gate", "1:lead:1", {
        "role": "lead", "iteration": 1, "attempt": "1", "pass": True, "failures": [],
        "check": {"ok": True}, "acceptance": dict(run.digest, pin=new_pin, pin_commit=commit),
        "status": "running", "phase": "lead-done"})
    stp = mbox(repo) / "STATE.md"
    stp.write_text(stp.read_text().replace("phase: lead-running", "phase: lead-done"))
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert not g["ok"] and "acceptance-record-forged" in g["error"], g
    assert run.digest["pin"] != new_pin
    _no_ship(repo)


def test_R5b_weakened_pack_and_state_without_a_record_need_a_human(repo: Path) -> None:
    ta = _ta()
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    acc = mbox(repo) / "acceptance"
    (acc / "checks" / "acc_04.py").write_text("import sys\nsys.exit(0)\n")
    new_pin = ta.manifest_sha256(acc)
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", f"acceptance: restore (driver)\n\nAcceptance-Pin: {new_pin}\n")
    sf = Path(run.digest["state_file"])
    st = json.loads(sf.read_text())
    st.update(pin=new_pin, pin_commit=git(repo, "rev-parse", "HEAD"))
    sf.write_text(json.dumps(st))
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert any("acceptance-state-mismatch" in f for f in g["failures"]) or \
        (g.get("acceptance") or {}).get("stop", {}).get("reason") == "acceptance-state-mismatch", g
    assert not g["pass"]
    _no_ship(repo)


def test_R6_forged_gate_record_with_a_path_is_refused(repo: Path, tmp_path: Path) -> None:
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    evil = tmp_path / "evil-bin"
    evil.mkdir()
    (evil / "python3").write_text("#!/bin/sh\nexit 0\n")
    (evil / "python3").chmod(0o755)
    _forge_record(repo, "gate", "1:lead:1", {
        "role": "lead", "iteration": 1, "attempt": "1", "pass": True, "failures": [],
        "check": {"ok": True}, "acceptance": dict(run.digest, path=f"{evil}:/usr/bin"),
        "status": "running", "phase": "lead-done"})
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert not g["ok"] and "acceptance-record-forged" in g["error"], g
    _no_ship(repo)


def test_R6b_a_path_in_the_digest_is_ignored(repo: Path, tmp_path: Path) -> None:
    """Even a digest the script hands back with a PATH (a buggy or hostile
    relay) never reaches the checks: the helper re-derives it."""
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    evil = tmp_path / "evil-bin"
    evil.mkdir()
    (evil / "python3").write_text("#!/bin/sh\nexit 0\n")
    (evil / "python3").chmod(0o755)
    p = run("pin", acceptance=False, iteration=1)
    held = {k: run.digest.get(k) for k in ACC_KEYS}
    held["path"] = f"{evil}:/usr/bin:/bin"
    ar = run("acceptance-run", iteration=1, sha=p["sha"], acc=json.dumps(held))
    assert ar["failed"] == 2 and ar["passed"] == 3, ar


def test_checks_path_comes_from_the_interpreters_sealed_at_begin(repo: Path, tmp_path: Path) -> None:
    """A python3 shim put first on the helper's PATH after begin is never
    used by the checks (eval-r19d 4 on the native path)."""
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1, upto=3)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    evil = tmp_path / "shim"
    evil.mkdir()
    (evil / "python3").write_text("#!/bin/sh\nexit 0\n")
    (evil / "python3").chmod(0o755)
    run.env = dict(run.env, PATH=f"{evil}:{run.env.get('PATH', os.environ['PATH'])}")
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert ar["failed"] == 2, ar
    ta = _ta()
    st = json.loads(Path(run.digest["state_file"]).read_text())
    assert str(evil) not in st["check_path"]
    assert st["check_path"] == ta.check_path()


def test_sealed_record_edit_needs_a_human(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    top = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    sealed = next((top / "trio-native").glob("*/acc-sealed.json"))
    data = json.loads(sealed.read_text())
    data["state"]["tamper_events"] = 0
    data["interpreters"]["python3"] = "/tmp/evil/python3"
    sealed.write_text(json.dumps(data))
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    c = run("coverage", iteration=1, attempt=1,
            plan={"slices": [{"id": "app", "covers": ["ACC-01"]}]})
    assert c["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch", c
    assert "not authentic" in c["acceptance"]["stop"]["detail"]


def test_a_nonce_without_this_execution_is_refused(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    run.exec_id = "f" * 32  # another execution's id in the nonce
    d = run("dispatch", iteration=1, wave=1)
    assert not d["ok"] and "not the mailbox's current execution" in d["error"], d


def test_deleting_a_failed_gate_record_buys_no_attempt(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1)
    (mbox(repo) / "acceptance" / "checks" / "acc_01.py").write_text("import sys\nsys.exit(0)\n")
    assert not run("gate", role="lead", iteration=1, attempt=1)["pass"]
    data = json.loads(_records_path(repo).read_text())
    data["gate"] = {}
    _records_path(repo).write_text(json.dumps(data))
    n = run("next", max_iterations=4)
    assert n["action"] == "lead" and n["attempt"] == 2, n


def test_apply_replay_is_logged_and_needs_the_same_tree(repo: Path) -> None:
    run = Run(repo)
    p, _ar = to_prerun(run, repo)
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "SHIP" and ap["code"] == 0, ap
    again = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert again["verdict"] == "SHIP" and again["replayed"] is True
    assert "replayed (this execution's recorded answer" in log_text(repo)
    (repo / "later.txt").write_text("x\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "a commit after the verdict")
    third = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not third["ok"] and "not replayed" in third["error"], third


# ------------------------------------------------------------ finding 5
def _slow(export: Path, secs: float = 2.0) -> None:
    for f in (export / "acceptance" / "checks").glob("*.py"):
        f.write_text(f"import time; time.sleep({secs})\n" + f.read_text())


def test_R4_a_dead_runs_freeze_job_is_fenced(repo: Path) -> None:
    holder1 = subprocess.Popen(["sleep", "600"])
    holder2 = subprocess.Popen(["sleep", "600"])
    try:
        env1 = acc_env(TRIO_NATIVE_HOLDER_PID=str(holder1.pid), TRIO_NATIVE_JOB_WAIT_S="0")
        env1.pop("TRIO_NATIVE_JOBS")
        r1 = Run(repo, env=env1, token="t-run1")
        ex1 = _to_export(r1)
        write_pack(Path(ex1["export"]))
        _slow(Path(ex1["export"]))
        fr = _freeze_call(r1, ex1)
        assert fr.get("pending") is True, fr
        jobs1 = _jobs(r1)
        pid = int((jobs1 / next(p.name for p in jobs1.glob("*.pid"))).read_text())
        holder1.kill()
        holder1.wait()
        env2 = acc_env(TRIO_NATIVE_HOLDER_PID=str(holder2.pid), TRIO_NATIVE_JOB_WAIT_S="60")
        env2.pop("TRIO_NATIVE_JOBS")
        r2 = Run(repo, env=env2, token="t-run2")
        b2 = r2("begin", models=MODELS)
        assert b2["ok"] and b2["acceptance"]["status"] == "authoring", b2
        state_before = state_text(repo)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.5)
        subjects = git(repo, "log", "--format=%s")
        assert "acceptance: freeze" not in subjects, subjects
        assert not list(jobs1.glob("*.json"))          # no result written
        assert state_text(repo) == state_before         # nothing written by the dead job
        assert not (mbox(repo) / "acceptance").exists()
        n2 = r2("next", max_iterations=4)
        assert n2["ok"] and "stop" not in (n2.get("acceptance") or {}), n2
    finally:
        for h in (holder1, holder2):
            h.kill()


# ------------------------------------------------------------ finding 6
def test_limited_audit_reaches_the_result_record(repo: Path) -> None:
    run = Run(repo)
    p, _ar = to_prerun(run, repo)
    ship_verdict(repo, 1, p)
    run("apply", iteration=1, attempt=p["evaluator_attempt"])
    end = run("end")
    assert end["acceptance"]["audit"] == {"limited": True, "contaminated": False,
                                         "attempts": 1, "transcripts": 0, "path": "native"}
    assert end["acceptance"]["authenticated"] is True
    result = json.loads((mbox(repo) / ".native-result.json").read_text())
    assert result["acceptance"]["audit"]["limited"] is True


# ------------------------------------------------------------ liveness (L1)
PLAN6 = PLAN_ALL.replace("ACC-05]", "ACC-05, ACC-06]")


def test_L1_needs_human_then_human_amend_then_fresh_run_ships(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env, token="t-run1")
    freeze(run, n=6, unavailable=(6,))
    lead_pass(repo, 1, plan=PLAN6)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert ar["unavailable"] == 1
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "NEEDS_HUMAN" and ap["stop"], ap
    end = run("end")
    assert end["ok"] and not (mbox(repo) / ".lock").exists(), end
    old_pin = run.digest["pin"]
    chk = mbox(repo) / "acceptance" / "checks" / "acc_06.py"
    chk.write_text(CHECK.format(k=6))
    cli = subprocess.run([sys.executable, str(ROOT / "omnigent" / "trioctl"), "omnigent",
                          "acceptance", "amend", "--mailbox", str(mbox(repo)), "--human",
                          "--ids", "ACC-06", "--reason", "needs no live service"],
                         capture_output=True, text=True, env=env, timeout=300)
    assert cli.returncode == 0, cli.stderr[-800:]
    st = (mbox(repo) / "STATE.md").read_text()
    st = re.sub(r"(?m)^status:.*$", "status: running", st)
    st = re.sub(r"(?m)^phase:.*$", "phase: idle", st)
    (mbox(repo) / "STATE.md").write_text(st)
    git(repo, "add", "loop/STATE.md")
    git(repo, "commit", "-q", "-m", "human: resume")
    run2 = Run(repo, env=env, token="t-run2")
    b = run2("begin", models=MODELS)
    assert b["ok"] and b["acceptance"].get("pin") and b["acceptance"]["pin"] != old_pin, b
    n = run2("next", max_iterations=4)
    assert n["action"] == "lead", n
    ex = run2("acceptance-export", iteration=n["iteration"], attempt=1)
    assert ex["frozen"] is True, ex
    cov = run2("coverage", iteration=n["iteration"], attempt=1,
               plan={"slices": [{"id": "app", "covers": [f"ACC-0{k}" for k in range(1, 7)]}]})
    assert cov["covered_ok"], cov
    lead_pass(repo, n["iteration"], upto=6, plan=PLAN6)
    g = run2("gate", role="lead", iteration=n["iteration"], attempt=1)
    assert g["pass"], g
    p = run2("pin", acceptance=False, iteration=n["iteration"])
    ar = run2("acceptance-run", iteration=n["iteration"], sha=p["sha"])
    assert ar["passed"] == 6, ar
    ship_verdict(repo, n["iteration"], p)
    ap = run2("apply", iteration=n["iteration"], attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "SHIP" and ap["status"] == "shipped", ap
    assert run2("end")["ok"]

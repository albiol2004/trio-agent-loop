"""eval-r19n2 repairs: the helper's sealed record is the only source of
truth across executions. The evaluator's repros F1a/F1b/F1c (a role's
mid-run "human" amendment adopted by the next run) and F2 (`git replace`)
are ported inverted; the LOW findings (lstat, rollback, seeding, freeze
snapshot, live-job dirs) have their own tests; L1 still ships, also with
`--adopt`."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from test_r19_acceptance import (  # noqa: F401 (repo fixture)
    CHECK, MODELS, PLAN_ALL, Run, acc_env, begin, freeze, git, lead_pass, log_text, mbox,
    repo, ship_verdict, state_text, write_pack)
from test_step_ops import HELPER

ROOT = HELPER.parents[1]
PLAN6 = PLAN_ALL.replace("ACC-05]", "ACC-05, ACC-06]")


def _tl():
    spec = importlib.util.spec_from_file_location("tl_r19n2_fix", ROOT / "metrics" / "trio_loop.py")
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)
    return tl


def _home(repo: Path) -> Path:
    top = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return next((top / "trio-native").glob("*/acc-sealed.json")).parent


def _sealed(repo: Path) -> dict:
    return json.loads((_home(repo) / "acc-sealed.json").read_text())


def _soften(repo: Path) -> None:
    acc = mbox(repo) / "acceptance" / "checks"
    for k in (4, 5):
        (acc / f"acc_0{k}.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")


def _role_forges_human_amend(repo: Path) -> None:
    """F1a: the role calls the release's own controller (no --adopt review,
    no sealed record): amend + pin commits with the human trailer and a
    matching state file."""
    _soften(repo)
    ctl = _tl().AcceptanceController(mbox(repo), repo, None, {"enabled": True})
    assert ctl.human_amend(["ACC-04", "ACC-05"], "flaky", 1) == 0
    assert "(human)" in git(repo, "log", "-3", "--format=%s")


def _human_resumes(repo: Path) -> None:
    st = state_text(repo)
    st = re.sub(r"(?m)^status:.*$", "status: running", st)
    st = re.sub(r"(?m)^phase:.*$", "phase: idle", st)
    (mbox(repo) / "STATE.md").write_text(st)
    git(repo, "add", "loop/STATE.md")
    git(repo, "commit", "-q", "--allow-empty", "-m", "human: resume")


def _trioctl_amend(repo: Path, env, ids: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "omnigent" / "trioctl"), "omnigent",
                           "acceptance", "amend", "--mailbox", str(mbox(repo)), "--human",
                           "--ids", ids, "--reason", "flaky", *extra],
                          capture_output=True, text=True, env=env, timeout=300)


def _checks_are_the_frozen_ones(repo: Path) -> None:
    acc = mbox(repo) / "acceptance" / "checks"
    for k in (4, 5):
        assert (acc / f"acc_0{k}.py").read_text() == CHECK.format(k=k)
        assert git(repo, "show", f"HEAD:loop/acceptance/checks/acc_0{k}.py") + "\n" \
            == CHECK.format(k=k)


def _stop_then_fresh_runs(repo: Path, env, run: Run, pin0: str) -> None:
    """The live run stopped; the documented recovery: `end`, a human
    resumes, a fresh run. Its `begin` restores the pinned pack and stops
    NEEDS_HUMAN acceptance-tamper; after review a third run keeps pin0 and
    never SHIPs the 3/5 app."""
    state_file = Path(run.digest["state_file"])
    # finding 1a: the stop wrote the sealed state over the state file
    assert json.loads(state_file.read_text())["pin"] == pin0
    assert _sealed(repo)["state"]["native"]["stopped"]
    assert run("end")["ok"]
    assert json.loads(state_file.read_text())["pin"] == pin0
    _human_resumes(repo)
    run2 = Run(repo, env=env, token="t-run2")
    b = run2("begin", models=MODELS)
    assert b["ok"] and b["acceptance"]["stop"]["reason"] == "acceptance-tamper", b
    assert "status: needs_human" in state_text(repo)
    assert "phase: acceptance-tamper" in state_text(repo)
    _checks_are_the_frozen_ones(repo)
    assert git(repo, "log", "-1", "--format=%s").startswith("acceptance: restore (tamper")
    st = json.loads(state_file.read_text())
    assert st["pin"] == pin0 and not st.get("human_amends"), st
    assert _sealed(repo)["state"]["pin"] == pin0
    assert "not adopted" in log_text(repo)
    assert run2("end")["ok"]
    # --- a human reviewed; a third run keeps the pin and refuses SHIP
    _human_resumes(repo)
    run3 = Run(repo, env=env, token="t-run3")
    b3 = run3("begin", models=MODELS)
    assert b3["ok"] and b3["acceptance"]["pin"] == pin0, b3
    assert "stop" not in b3["acceptance"], b3
    n = run3("next", max_iterations=6)
    it = n["iteration"]
    assert run3("acceptance-export", iteration=it, attempt=1)["frozen"] is True
    run3("coverage", iteration=it, attempt=1,
         plan={"slices": [{"id": "app", "covers": [f"ACC-0{k}" for k in range(1, 6)]}]})
    (repo / "NOTES").write_text(f"pass {it}\n")
    git(repo, "add", "NOTES")
    git(repo, "commit", "-q", "-m", f"slice(app): notes {it}")  # app.py still 3/5
    with (mbox(repo) / "LOG.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- iter {it} | lead | done\n")
    (mbox(repo) / "REPORT.md").write_text(f"# Report {it}\n", encoding="utf-8")
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    assert run3("gate", role="lead", iteration=it, attempt=1)["pass"]
    p = run3("pin", acceptance=False, iteration=it)
    ar = run3("acceptance-run", iteration=it, sha=p["sha"])
    assert ar["failed"] == 2 and ar["passed"] == 3, ar
    ship_verdict(repo, it, p)
    ap = run3("apply", iteration=it, attempt=p["evaluator_attempt"])
    assert not (ap.get("verdict") == "SHIP" and ap.get("status") == "shipped"), ap
    assert ap["verdict"] == "ITERATE", ap


# ------------------------------------------------------------ finding 1
def test_F1a_forged_human_amend_mid_run_is_restored_not_adopted(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env, token="t-run1")
    freeze(run)
    pin0 = run.digest["pin"]
    lead_pass(repo, 1, upto=3)
    _role_forges_human_amend(repo)            # inside the Lead's turn
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert "acceptance-state-mismatch" in json.dumps(g), g
    _stop_then_fresh_runs(repo, env, run, pin0)


def test_F1b_forged_human_amend_plus_deleted_state_is_not_rederived(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env, token="t-run1")
    freeze(run)
    pin0 = run.digest["pin"]
    state_file = Path(run.digest["state_file"])
    lead_pass(repo, 1, upto=3)
    _role_forges_human_amend(repo)
    state_file.unlink()                        # "lost" driver state
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert "acceptance-state-mismatch" in json.dumps(g), g
    _stop_then_fresh_runs(repo, env, run, pin0)
    assert "pin re-derived from git history" not in log_text(repo)


def test_F1c_role_running_trioctl_amend_mid_run_is_refused(repo: Path) -> None:
    """Native twin of the Cursor test_role_deleting_the_lock_gets_no_
    adoption_the_running_driver_honours: the role runs the real `trioctl
    omnigent acceptance amend --human`, deleting the lock when refused. The
    native path refuses on the sealed record (no stop, holder alive); the
    role's fallback (the release controller itself) is restored at the next
    begin and never adopted."""
    env = acc_env()
    run = Run(repo, env=env, token="t-run1")
    freeze(run)
    pin0 = run.digest["pin"]
    lead_pass(repo, 1, upto=3)
    _soften(repo)
    head = git(repo, "rev-parse", "HEAD")
    cli = _trioctl_amend(repo, env, "ACC-04,ACC-05")
    assert cli.returncode == 5, cli.stderr
    lock = mbox(repo) / ".lock"
    saved = mbox(repo) / ".lock-saved"
    shutil.move(str(lock), str(saved))
    cli = _trioctl_amend(repo, env, "ACC-04,ACC-05")
    if (mbox(repo) / ".lock").exists():
        shutil.rmtree(mbox(repo) / ".lock")
    shutil.move(str(saved), str(lock))        # the role puts the lock back
    assert cli.returncode == 5 and "is running" in cli.stderr, cli.stderr
    assert git(repo, "rev-parse", "HEAD") == head      # nothing committed
    assert _sealed(repo)["state"]["human_amends"] == []
    _role_forges_human_amend(repo)            # the fallback: the controller itself
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert "acceptance-state-mismatch" in json.dumps(g), g
    _stop_then_fresh_runs(repo, env, run, pin0)


def test_state_file_forged_between_runs_is_overwritten_from_the_sealed_record(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    pin0 = run.digest["pin"]
    state_file = Path(run.digest["state_file"])
    assert run("end")["ok"]
    st = json.loads(state_file.read_text())
    st["pin"] = "0" * 64
    st["human_amends"] = ["f" * 40]
    state_file.write_text(json.dumps(st))
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["pin"] == pin0 and "stop" not in b["acceptance"], b
    assert "disagreed with the helper's sealed record (pin, human_amends)" in log_text(repo)
    st = json.loads(state_file.read_text())
    assert st["pin"] == pin0 and st["human_amends"] == []


def test_a_pack_committed_while_the_sealed_record_is_authoring_needs_a_human(repo: Path) -> None:
    """The R3 class across executions: a pack that appears after a run
    stopped while authoring is never re-derived from git."""
    run = Run(repo)
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    assert run("end")["ok"]
    write_pack(Path(ex["export"]))
    shutil.copytree(Path(ex["export"]) / "acceptance", mbox(repo) / "acceptance")
    (mbox(repo) / "acceptance" / "FROZEN").write_text("pin[0]: " + "0" * 64 + " freeze\n")
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", "acceptance: freeze 5 checks (forged)")
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["stop"]["reason"] == "acceptance-tamper", b


def test_L1b_human_adopt_while_stopped_lands_in_the_sealed_record(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env, token="t-run1")
    freeze(run, n=6, unavailable=(6,))
    lead_pass(repo, 1, plan=PLAN6)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    run("acceptance-run", iteration=1, sha=p["sha"])
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "NEEDS_HUMAN", ap
    assert run("end")["ok"]
    assert _sealed(repo)["state"]["native"]["stopped"]["reason"] == "end"
    old_pin = run.digest["pin"]
    # --- the human commits an amendment, then adopts it after review
    (mbox(repo) / "acceptance" / "checks" / "acc_06.py").write_text(CHECK.format(k=6))
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", "acceptance: amend ACC-06 (human): needs no live service")
    amend_sha = git(repo, "rev-parse", "HEAD")
    cli = _trioctl_amend(repo, env, "ACC-06", "--adopt", amend_sha)
    assert cli.returncode == 0, cli.stderr[-800:]
    assert "sealed record" in cli.stdout
    sealed = _sealed(repo)["state"]
    assert amend_sha in sealed["human_amends"] and sealed["pin"] != old_pin
    new_pin = sealed["pin"]
    _human_resumes(repo)
    run2 = Run(repo, env=env, token="t-run2")
    b = run2("begin", models=MODELS)
    assert b["ok"] and b["acceptance"]["pin"] == new_pin and "stop" not in b["acceptance"], b


def test_trioctl_amend_refuses_a_sealed_record_that_does_not_verify(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env)
    freeze(run)
    assert run("end")["ok"]
    path = _home(repo) / "acc-sealed.json"
    data = json.loads(path.read_text())
    data["state"]["human_amends"] = ["f" * 40]
    path.write_text(json.dumps(data))
    (mbox(repo) / "acceptance" / "checks" / "acc_05.py").write_text("import sys\nsys.exit(0)\n")
    cli = _trioctl_amend(repo, env, "ACC-05")
    assert cli.returncode == 3 and "does not verify" in cli.stderr, cli.stderr
    fresh = Run(repo, env=env)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch", b
    assert "does not verify" in b["acceptance"]["stop"]["detail"]


# ------------------------------------------------------------ finding 2
def test_F2_git_replace_never_stands_in_for_the_evaluated_commit(repo: Path) -> None:
    env = acc_env()
    run = Run(repo, env=env)
    freeze(run)
    lead_pass(repo, 1, upto=3)                   # real: ACC-04/05 FAIL
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    real = p["sha"]
    full = ("import sys\nif int(sys.argv[1]) in (1, 2, 3, 4, 5,):\n"
            "    print('hello', sys.argv[1])\n")
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=full, capture_output=True, text=True, check=True).stdout.strip()
    idx = dict(env, GIT_INDEX_FILE=str(repo / ".git" / "phantom-index"))

    def phantom_of(sha: str, parent: str, msg: str) -> str:
        subprocess.run(["git", "-C", str(repo), "read-tree", sha], env=idx, check=True)
        subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                        f"100644,{blob},app.py"], env=idx, check=True)
        tree = subprocess.run(["git", "-C", str(repo), "write-tree"], env=idx,
                              capture_output=True, text=True, check=True).stdout.strip()
        return subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-p", parent,
                               "-m", msg], env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
    git(repo, "replace", real, phantom_of(real, git(repo, "rev-parse", f"{real}^"),
                                          "slice(app): hello 1"))
    ar = run("acceptance-run", iteration=1, sha=real)
    assert ar["failed"] == 2 and ar["passed"] == 3, ar   # the real commit
    ship_verdict(repo, 1, p)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "replace", head, phantom_of(head, real, git(repo, "log", "-1", "--format=%B", head)))
    git(repo, "read-tree", "HEAD")
    git(repo, "checkout", "--", "app.py")
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert not (ap.get("verdict") == "SHIP" and ap.get("status") == "shipped"), ap
    assert "status: shipped" not in state_text(repo)


# ------------------------------------------------------------ finding 3
def test_an_older_sealed_record_of_this_execution_is_refused(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    path = _home(repo) / "acc-sealed.json"
    older = path.read_bytes()
    lead_pass(repo, 1)
    (mbox(repo) / "acceptance" / "checks" / "acc_01.py").write_text("import sys\nsys.exit(0)\n")
    g = run("gate", role="lead", iteration=1, attempt=1)       # a failed gate: sealed
    assert not g["pass"], g
    assert _sealed(repo)["seq"] > json.loads(older)["seq"]
    run.digest["seq"] = _sealed(repo)["seq"]                    # what the script holds
    path.write_bytes(older)                                     # authentic, but older
    n = run("next", max_iterations=4)
    stop = (n.get("acceptance") or {}).get("stop") or {}
    assert stop.get("reason") == "acceptance-state-mismatch", n
    assert "older" in stop["detail"]


def test_a_symlinked_trio_native_dir_is_refused(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    assert run("end")["ok"]
    top = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    (top / "trio-native").rename(top / "trio-native-real")
    (top / "trio-native").symlink_to(top / "trio-native-real")
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch", b
    assert "symlink" in b["acceptance"]["stop"]["detail"]


# ------------------------------------------------------------ finding 4
def test_begin_seeds_gate_failures_from_the_sealed_record_only(repo: Path) -> None:
    run = Run(repo, token="t-run1")
    freeze(run)
    lead_pass(repo, 1)
    (mbox(repo) / "acceptance" / "checks" / "acc_01.py").write_text("import sys\nsys.exit(0)\n")
    assert not run("gate", role="lead", iteration=1, attempt=1)["pass"]
    assert run("end")["ok"]
    (mbox(repo) / ".native.json").unlink()                       # between executions
    run2 = Run(repo, token="t-run2")
    b = run2("begin", models=MODELS)
    assert b["ok"], b
    assert _sealed(repo)["state"]["native"]["gate_fail"] == {"1:lead": 1}
    n = run2("next", max_iterations=4)
    assert n["action"] == "lead" and n["attempt"] == 2, n


# ------------------------------------------------------------ finding 5
def test_a_pack_changed_during_validation_is_not_frozen(repo: Path) -> None:
    run = Run(repo)
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    export = Path(ex["export"])
    write_pack(export)
    target = export / "acceptance" / "checks" / "acc_02.py"
    # a check that, when validated, rewrites the author's export (a leftover
    # author process racing the freeze)
    first = export / "acceptance" / "checks" / "acc_01.py"
    first.write_text(f"open({str(target)!r}, 'a').write('# swapped\\n')\n" + first.read_text())
    fr = run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"], prior={},
             author={"exit": 0}, model="claude-opus-5-5")
    stop = (fr.get("acceptance") or {}).get("stop") or {}
    assert stop.get("reason") == "acceptance-tamper", fr
    assert "changed while it was validated" in stop["detail"]
    assert "acceptance: freeze" not in git(repo, "log", "--format=%s")


def test_other_executions_dirs_with_a_live_job_are_kept(repo: Path) -> None:
    run = Run(repo, token="t-run1")
    begin(run)
    base = Path(run.digest["state_file"]).parent
    other = "a" * 32
    job = subprocess.Popen(["sleep", "600"])
    try:
        (base / "native-jobs" / other).mkdir(parents=True)
        (base / "native-jobs" / other / "j.pid").write_text(f"{job.pid}\n")
        for what in ("export", "validate", "snap"):
            (base / f"{what}-{other}").mkdir()
        assert run("end")["ok"]
        run2 = Run(repo, token="t-run2")
        assert run2("begin", models=MODELS)["ok"]
        for what in ("export", "validate", "snap"):
            assert (base / f"{what}-{other}").is_dir(), what
    finally:
        job.kill()
        job.wait()
    assert run2("end")["ok"]
    run3 = Run(repo, token="t-run3")
    assert run3("begin", models=MODELS)["ok"]
    for what in ("export", "validate", "snap"):
        assert not (base / f"{what}-{other}").exists(), what

"""r19 round-2 review repairs (eval-r19b VERDICT findings 1-6), switch ON.

The evaluator's repros E1-E12 (E8 and E11 are controls) ported here
inverted: each asserts the repaired behaviour over a REAL git repo with the
real driver, runner, trio-shadow and trio-check. Extra tests close the
class, not the instance (shared helpers, whole-pack re-run, git-derived
chain, explicit human adoption, restore failure, process sweep).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"


def _ta():
    spec = importlib.util.spec_from_file_location("ta_review_fixes_r2",
                                                  ROOT / "metrics" / "trio-acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _ta()
FEATS_5_8 = [f"f{k}" for k in range(5, 9)]  # ACC-01..04 must FAIL forever


def shadow(mb, env=None, *extra):
    return subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits",
                           *extra], capture_output=True, text=True, env=env)


def blind(tmp_path):
    return dict(os.environ, TRIO_ACCEPTANCE_STATE=str(tmp_path / "elsewhere"))


def state_of(repo, mb) -> dict:
    return TA.load_state(TA.state_file(repo, mb))


def _resume(mb):
    st = (mb / "STATE.md").read_text().replace("status: needs_human", "status: running") \
        .replace("phase: needs_human", "phase: idle")
    (mb / "STATE.md").write_text(st)


def _soften(mb, ks, who="human", iteration=1):
    acc = mb / "acceptance"
    for k in ks:
        (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
        with (acc / "AMENDMENTS.md").open("a") as fh:
            fh.write(f"## ACC-{k:02d} · iter {iteration} · {who} · t\nchange: x\n")
    return [f"ACC-{k:02d}" for k in ks]


def _shipped(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    return repo, mb


def _stopped(tmp_path, feats=FEATS_5_8):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": feats}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    return repo, mb


def _never_shipped_softened(repo, mb, code):
    """The loop did not SHIP a product missing f1..f4 on softened checks."""
    shipped = code == 0 and "status: shipped" in T.state(mb)
    assert not shipped or "'f1'" in (repo / "app.py").read_text(), T.log(mb)[-2000:]
    for k in (1, 2, 3, 4):
        assert "SystemExit(0)" not in (mb / "acceptance" / "checks" / f"acc_{k:02d}.py").read_text()


# ------------------------------------------------------------------ finding 1


class Smuggle(T.Fake):
    """E12: an in-budget amendment naming only ACC-08 that also rewrites
    checks/acc_01..04.py to exit 0."""

    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
            self.done = True
            acc = self.mb / "acceptance"
            for k in (1, 2, 3, 4):
                (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
            p = acc / "checks" / "acc_08.py"
            p.write_text(p.read_text() + "# clarified\n")
            with (acc / "AMENDMENTS.md").open("a") as fh:
                fh.write(f"## ACC-08 · iter {iteration} · evaluator · t\n"
                         "goal_quote: feature f8 works\ndefect in check: x\nchange: comment\n")
            T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(self.repo, "commit", "-qm",
                        f"acceptance: amend ACC-08 (evaluator, iter {iteration}): clarify")
        return super()._eval(iteration, context)


def test_E12_amendment_may_change_only_the_amended_ids_files(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = Smuggle(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                   eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert "amendment of ACC-08 accepted" not in lg
    assert "amendment of ACC-08 rejected" in lg
    assert "checks/acc_01.py is used by ACC-01" in lg
    assert "SHIP gate: 8/8 PASS" not in lg
    _never_shipped_softened(repo, mb, code)


def test_E12b_whole_pack_rerun_catches_what_a_scope_hole_would_let_through(tmp_path,
                                                                          monkeypatch):
    """Defence in depth: even with the scope rule disabled, the whole pack
    re-runs at base and ACC-01..04 (FAILed at base before) must still FAIL."""
    repo, mb = T.make_repo(tmp_path)
    ctl_cls = trio_loop.AcceptanceController
    real_init = ctl_cls.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self.ta.amendment_problems = lambda *args, **kws: []
    monkeypatch.setattr(ctl_cls, "__init__", init)
    fake = Smuggle(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                   eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert "amendment of ACC-08 rejected" in lg
    assert "ACC-01 no longer FAILs at base (PASS)" in lg
    _never_shipped_softened(repo, mb, code)


def test_E6_run_retarget_to_another_checks_script_is_rejected(tmp_path):
    repo, mb = T.make_repo(tmp_path)

    class Retarget(T.Fake):
        def _eval(self, iteration, context):
            if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
                self.done = True
                acc = self.mb / "acceptance"
                man = json.loads((acc / "MANIFEST.json").read_text())
                for c in man["checks"]:
                    if c["id"] == "ACC-01":
                        c["run"] = ["python3", "acceptance/checks/acc_08.py"]
                (acc / "MANIFEST.json").write_text(json.dumps(man, indent=2))
                with (acc / "AMENDMENTS.md").open("a") as fh:
                    fh.write(f"## ACC-01 · iter {iteration} · evaluator · t\n"
                             "goal_quote: feature f1 works\ndefect in check: x\nchange: run\n")
                T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
                T.git_retry(self.repo, "commit", "-qm",
                            f"acceptance: amend ACC-01 (evaluator, iter {iteration}): x")
            return super()._eval(iteration, context)

    feats = [f"f{k}" for k in range(2, 9)]  # f1 never implemented
    fake = Retarget(repo, mb, lead_script=[{"features": feats}] * 6,
                    eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert "amendment of ACC-01 accepted" not in lg
    assert "ACC-01: `run` now points at checks/acc_08.py, which belongs to ACC-08" in lg
    assert not (code == 0 and "status: shipped" in T.state(mb))


def _chk(cid, script):
    return {"id": cid, "goal_quote": "q", "kind": "behaviour",
            "run": ["python3", f"acceptance/checks/{script}"]}


def test_amendment_scope_attributes_every_changed_file():
    old = {"checks": [_chk("ACC-01", "a.py"), _chk("ACC-02", "b.py"),
                      {**_chk("ACC-03", "c.py"),
                       "run": ["sh", "-c", "python3 acceptance/checks/c.py --fake $ACC_DIR/fakes/srv"]}]}
    files = ["MANIFEST.json", "AMENDMENTS.md", "checks/a.py", "checks/b.py", "checks/c.py",
             "fakes/srv/app.py", "checks/helper.py"]
    ok = TA.amendment_problems(old, old, ["checks/a.py", "AMENDMENTS.md"], ["ACC-01"], files)
    assert ok == []
    # Another id's script.
    probs = TA.amendment_problems(old, old, ["checks/b.py"], ["ACC-01"], files)
    assert any("checks/b.py is used by ACC-02" in p for p in probs)
    # A fake (fakes/ is a shared location since eval-r19c: every check).
    probs = TA.amendment_problems(old, old, ["fakes/srv/app.py"], ["ACC-01"], files)
    assert any("fakes/srv/app.py is shared by every check" in p and "ACC-02, ACC-03" in p
               for p in probs)
    # A helper no `run` names belongs to every check.
    probs = TA.amendment_problems(old, old, ["checks/helper.py"], ["ACC-01"], files)
    assert any("shared helper" in p and "ACC-02, ACC-03" in p for p in probs)
    assert TA.amendment_problems(old, old, ["checks/helper.py"],
                                 ["ACC-01", "ACC-02", "ACC-03"], files) == []
    # `run` pointed at another check's file, or at a shared helper.
    new = {"checks": [_chk("ACC-01", "b.py"), old["checks"][1], old["checks"][2]]}
    assert any("belongs to ACC-02" in p
               for p in TA.amendment_problems(old, new, ["MANIFEST.json"], ["ACC-01"], files))
    new = {"checks": [_chk("ACC-01", "helper.py"), old["checks"][1], old["checks"][2]]}
    assert any("shared pack file" in p
               for p in TA.amendment_problems(old, new, ["MANIFEST.json"], ["ACC-01"], files))
    # A brand-new script for the amended id is its own -- only under its
    # per-check directory (eval-r19c); a new top-level checks/ file is not.
    new = {"checks": [_chk("ACC-01", "ACC-01/a2.py"), old["checks"][1], old["checks"][2]]}
    assert TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-01/a2.py"],
                                 ["ACC-01"], files) == []
    new = {"checks": [_chk("ACC-01", "a2.py"), old["checks"][1], old["checks"][2]]}
    assert any("new file outside checks/<ID>/" in p for p in TA.amendment_problems(
        old, new, ["MANIFEST.json", "checks/a2.py"], ["ACC-01"], files))
    # No removals.
    new = {"checks": old["checks"][:2]}
    assert any("removed" in p for p in TA.amendment_problems(old, new, [], ["ACC-03"], files))


# ------------------------------------------------------------------ finding 2


class DelayedForger(T.Fake):
    """E1: the integration Evaluator leaves a background job that commits a
    `(human)` amend once the driver released the mailbox, then induces a
    clean NEEDS_HUMAN stop."""

    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval":
            repo, mb = self.repo, self.mb

            def job():
                lock = mb / ".lock"
                deadline = time.monotonic() + 60
                time.sleep(0.2)
                while lock.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                time.sleep(0.3)
                ids = _soften(mb, (1, 2, 3, 4))
                T.git_retry(repo, "add", "-A", "--", "loop/acceptance")
                T.git_retry(repo, "commit", "-qm",
                            f"acceptance: amend {', '.join(ids)} (human): checks over-specified")
                self.job_done = True
            self.job = threading.Thread(target=job, daemon=True)
            self.job.start()
        return super()._eval(iteration, context)


def test_E1_resume_never_adopts_a_role_jobs_human_amend(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = DelayedForger(repo, mb, lead_script=[{"features": FEATS_5_8}],
                         eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    fake.job.join(30)
    assert getattr(fake, "job_done", False)
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                   eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake2)
    lg = T.log(mb)
    assert "adopted on resume" not in lg
    assert "not adopted: resume never adopts" in lg
    assert "acceptance tamper restored (before lead)" in lg
    assert "SHIP gate: 8/8" not in lg
    _never_shipped_softened(repo, mb, code)
    assert shadow(mb).returncode == 0, shadow(mb).stdout


def test_E2_state_file_pin_rewritten_after_stop_forces_needs_human(tmp_path):
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    for k in (1, 2, 3, 4):
        (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
    path = TA.state_file(repo, mb)
    st = TA.load_state(path)
    st["pin"] = TA.manifest_sha256(acc)
    TA.save_state(path, st)
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{"features": FEATS_5_8}],
                   eval_script=[{"verdict": "SHIP"}])
    code = T.run(mb, fake2)
    lg = T.log(mb)
    assert code == 5, lg[-1500:]
    assert "acceptance-state-mismatch" in lg and "status: needs_human" in T.state(mb)
    assert not [c for c in fake2.calls if c[0] == "lead"]


def test_E9_state_deleted_pin_rederived_from_git_not_the_working_tree(tmp_path):
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    committed_pin = state_of(repo, mb)["pin"]
    for k in (1, 2, 3, 4):
        (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
    pin = TA.manifest_sha256(acc)
    fz = acc / "FROZEN"
    n = fz.read_text().count("pin[")
    fz.write_text(fz.read_text().rstrip("\n") + f"\npin[{n}]: {pin} amend ACC-01\n")
    TA.state_file(repo, mb).unlink()
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                   eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake2)
    lg = T.log(mb)
    assert f"pin re-derived from git history {committed_pin[:12]}" in lg
    assert "acceptance tamper restored (before lead)" in lg
    assert state_of(repo, mb)["pin"] == committed_pin
    _never_shipped_softened(repo, mb, code)


def test_E8_control_resume_without_driver_state_rederives_pin_and_ships(tmp_path, monkeypatch):
    repo, mb = _stopped(tmp_path, feats=[f"f{k}" for k in range(1, 8)])
    monkeypatch.setenv("TRIO_ACCEPTANCE_STATE", str(tmp_path / "state-elsewhere"))
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{}, {}, {}], eval_script=[{"verdict": "SHIP"}])
    code = T.run(mb, fake2)
    lg = T.log(mb)
    assert "pin re-derived from git history" in lg and code == 0, lg[-2000:]
    assert "acceptance tamper restored" not in lg


def test_lost_state_with_an_ambiguous_freeze_needs_a_human(tmp_path):
    """Lost state + FROZEN added twice on the first-parent history (a `git
    rm` and re-add): the run's freeze is ambiguous -> NEEDS_HUMAN."""
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    frozen = (acc / "FROZEN").read_text()
    T.git(repo, "rm", "-q", "--", "loop/acceptance/FROZEN")
    T.git(repo, "commit", "-qm", "slice(cli): tidy")
    (acc / "FROZEN").write_text(frozen)
    pin = TA.manifest_sha256(acc)
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: freeze 8 checks (lead)\n\nAcceptance-Pin: {pin}\n")
    TA.state_file(repo, mb).unlink()
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake2) == 5
    lg = T.log(mb)
    assert "acceptance-state-lost" in lg and "ambiguous" in lg
    assert not [c for c in fake2.calls if c[0] == "lead"]


def test_human_amend_refuses_unreviewed_pack_commits_and_records_the_adoption(tmp_path):
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    _soften(mb, (1,))
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "acceptance: amend ACC-01 (human): x")
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    before = state_of(repo, mb)["pin"]
    assert ctl.human_amend(["ACC-02"], "fix ACC-02", iteration=1) == 3  # ACC-01 commit unnamed
    assert state_of(repo, mb)["pin"] == before
    # A slice commit that touched the pack can never be adopted.
    T.git(repo, "reset", "-q", "--hard", "HEAD~1")
    (acc / "checks" / "acc_02.py").write_text("raise SystemExit(1)\n")
    T.git(repo, "commit", "-qam", "slice(cli): touch")
    sha = T.git(repo, "rev-parse", "HEAD")
    assert ctl.human_amend(["ACC-02"], "x", iteration=1, adopt=[sha]) == 3


def test_human_amendments_do_not_count_toward_the_evaluator_budget(tmp_path):
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_08.py").write_text("import sys\nsys.exit(1)\n# human\n")
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    assert ctl.human_amend(["ACC-08", "ACC-07", "ACC-06"], "rewrite", iteration=1) == 0
    st = state_of(repo, mb)
    assert st["amendments"] == 0 and ctl.amendments_left() == 2
    chain = TA.derive_pin_chain(repo, "loop/acceptance")
    assert chain["pins"][-1]["kind"] == "human" and chain["amend_used"] == 0
    assert chain["pin"] == st["pin"]


def test_close_sweeps_role_process_groups_when_the_runner_tracks_them(tmp_path):
    repo, mb = _shipped(tmp_path)

    class Runner:
        driver_meta: dict = {}
        swept = 0

        def sweep_role_processes(self):
            Runner.swept += 1
            return 2
    ctl = trio_loop.AcceptanceController(mb, repo, Runner(), {"enabled": True})
    ctl.close()
    assert Runner.swept == 1
    assert "swept 2 role process group(s) at stop" in T.log(mb)
    # A runner without the hook (OmnigentRunner) is left alone.
    trio_loop.AcceptanceController(mb, repo, object(), {"enabled": True}).close()


def test_loop_stop_calls_the_runner_sweep(tmp_path):
    repo, mb = T.make_repo(tmp_path)

    class Sweeping(T.Fake):
        swept = 0

        def sweep_role_processes(self):
            Sweeping.swept += 1
            return 0
    fake = Sweeping(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    assert Sweeping.swept == 1


# ------------------------------------------------------------------ finding 3


def test_E3_evaluator_labelled_amend_with_no_pin_fails_trio_shadow(tmp_path):
    repo, mb = _shipped(tmp_path)
    ids = _soften(mb, (1, 2), who="evaluator")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: amend {', '.join(ids)} (evaluator, iter 2): x")
    with_state = shadow(mb)
    without = shadow(mb, blind(tmp_path))
    for proc in (with_state, without):
        assert proc.returncode == 1, proc.stdout
        assert "is not the last legitimate pin" in proc.stdout


def test_E3b_without_state_forged_amend_plus_pin_fails(tmp_path):
    repo, mb = _shipped(tmp_path)
    ids = _soften(mb, (1, 2, 3, 4), who="evaluator")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: amend {', '.join(ids)} (evaluator, iter 2): x")
    acc = mb / "acceptance"
    pin = TA.manifest_sha256(acc)
    fz = acc / "FROZEN"
    n = fz.read_text().count("pin[")
    fz.write_text(fz.read_text().rstrip("\n") + f"\npin[{n}]: {pin} amend {', '.join(ids)}\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: pin {pin[:12]} (amend)\n\nAcceptance-Pin: {pin}\n")
    without = shadow(mb, blind(tmp_path))
    assert without.returncode == 1 and "amendment budget exceeded" in without.stdout, without.stdout
    assert shadow(mb).returncode == 1


def test_E3c_without_state_in_budget_forged_softening_pin_fails_discrimination(tmp_path):
    """A forged amend + pin that passes scope, records and budget (2 of 8)
    still fails: the chain re-checks discrimination at the frozen base."""
    repo, mb = _shipped(tmp_path)
    ids = _soften(mb, (1, 2), who="evaluator")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: amend {', '.join(ids)} (evaluator, iter 2): x")
    acc = mb / "acceptance"
    pin = TA.manifest_sha256(acc)
    fz = acc / "FROZEN"
    n = fz.read_text().count("pin[")
    fz.write_text(fz.read_text().rstrip("\n") + f"\npin[{n}]: {pin} amend {', '.join(ids)}\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: pin {pin[:12]} (amend)\n\nAcceptance-Pin: {pin}\n")
    proc = shadow(mb, blind(tmp_path))
    assert proc.returncode == 1, proc.stdout
    assert "ACC-01, ACC-02 no longer FAIL at base" in proc.stdout


def test_E4_without_state_refreeze_does_not_move_the_base(tmp_path):
    repo, mb = _shipped(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "slice(cli): loosen")
    other = blind(tmp_path)
    assert shadow(mb, env=other).returncode == 1  # control
    frozen = (acc / "FROZEN").read_text()
    T.git(repo, "rm", "-q", "--", "loop/acceptance/FROZEN")
    T.git(repo, "commit", "-qm", "slice(cli): tidy")
    pin = TA.manifest_sha256(acc)
    (acc / "FROZEN").write_text(frozen.rstrip("\n") + f"\npin[9]: {pin} x\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: freeze 8 checks (lead)\n\nAcceptance-Pin: {pin}\n")
    proc = shadow(mb, env=other)
    assert proc.returncode == 1, proc.stdout
    assert "slice(cli): loosen" in proc.stdout and "second freeze commit" in proc.stdout


def test_trio_shadow_with_state_flags_a_state_pin_git_does_not_hold(tmp_path):
    repo, mb = _shipped(tmp_path)
    path = TA.state_file(repo, mb)
    st = TA.load_state(path)
    st["pin"] = "0" * 64
    TA.save_state(path, st)
    proc = shadow(mb)
    assert proc.returncode == 1 and "driver state's pin" in proc.stdout, proc.stdout


def test_derived_chain_matches_the_driver_state_after_a_real_amendment(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    good = ("import subprocess, sys\nout = subprocess.run([sys.executable, 'app.py', 'f1'],"
            " capture_output=True, text=True).stdout\nsys.exit(0 if 'ok-f1' in out else 1)\n")
    fake = T.Fake(repo, mb, lead_script=[{}, {}],
                  eval_script=[{"verdict": "ITERATE", "amend": [{"k": 1, "body": good}]},
                               {"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    st = state_of(repo, mb)
    chain = TA.derive_pin_chain(repo, "loop/acceptance", verify=True)
    assert chain["pin"] == st["pin"] and chain["amend_used"] == 1
    assert [p["kind"] for p in chain["pins"]] == ["freeze", "amend"]
    assert not chain["problems"] and not chain["pending_tamper"]
    assert shadow(mb, blind(tmp_path)).returncode == 0


# ------------------------------------------------------------------ finding 4


def test_E7_concurrent_check_pin_counts_one_tamper_once(tmp_path):
    repo, mb = _shipped(tmp_path)
    for attempt in range(8):
        acc = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
        acc.state["tamper_events"] = 0
        (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
        barrier = threading.Barrier(2)
        errors = []
        results = []

        def go(role):
            barrier.wait()
            try:
                results.append(acc.check_pin(1, role))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        ths = [threading.Thread(target=go, args=(r,)) for r in ("before lead", "slice-eval")]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        assert errors == [], (attempt, errors)
        assert acc.state["tamper_events"] == 1, attempt
        assert sorted(results) == [False, True]
        assert acc.pin_ok()


def test_restore_failure_is_retried_then_fails_closed(tmp_path, monkeypatch):
    repo, mb = _shipped(tmp_path)
    acc = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise OSError("disk went away")
    monkeypatch.setattr(acc.ta, "restore_pack_files", broken)
    with pytest.raises(trio_loop.AcceptanceError) as exc:
        acc.check_pin(1, "lead")
    assert exc.value.reason == "acceptance-restore-failed" and len(calls) == 3


# ------------------------------------------------------------------ finding 5


def test_E5_audit_flags_reads_that_do_not_spell_the_repo(tmp_path, monkeypatch):
    export = tmp_path / "export"
    export.mkdir()
    lab = tmp_path / "lab"
    repo = lab / "wt" / "repo"
    (repo / "loop").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x")
    monkeypatch.setenv("HOME", str(lab))  # `$HOME/...` reaching the repo
    rows = {
        "grep tool on an ancestor dir": json.dumps(
            {"type": "tool_use", "name": "Grep",
             "input": {"pattern": "def main", "path": str(lab)}}),
        "cd ancestor then relative": f"cd {lab} && cat wt/repo/src/app.py",
        "cd ancestor then glob mailbox": f"cd {lab} && cat wt/repo/loop/PLA?.md",
        "$HOME-relative": "cat $HOME/wt/repo/src/app.py",
        "~-relative": "cat ~/wt/repo/src/app.py",
        "/proc cwd of the driver": "cat /proc/$PPID/cwd/src/app.py",
        "$OLDPWD": "cat $OLDPWD/src/app.py",
        "find exec cat": f"find {lab} -name 'PLA*' -exec cat {{}} +",
        "cd into the repo": f"cd {repo} && ls",
        "grep -r on /": "grep -rn PLAN /",
    }
    missed = [label for label, row in rows.items()
              if not TA.audit_transcript([row], export,
                                         forbidden=[repo, repo / "loop"])["contaminated"]]
    assert missed == []


def test_E10_product_files_named_like_mailbox_files_do_not_contaminate(tmp_path):
    export = tmp_path / "export"
    export.mkdir()
    repo = tmp_path / "repo"
    rows = [
        "python3 -m tool report --out /tmp/acc-scratch && cat /tmp/acc-scratch/REPORT.md",
        json.dumps({"type": "tool_use", "name": "Read",
                    "input": {"file_path": str(tmp_path / "scratch" / "LOG.md")}}),
        "cd $(mktemp -d) && python3 app.py init && cat STATE.md",
        f"cd {tmp_path / 'scratch'} && cat loop/PLAN.md && cd - && ls acceptance",
        "ls /",
        "cat PLAN.md",
    ]
    flagged = [r for r in rows if TA.audit_transcript(
        [r], export, forbidden=[repo, repo / "loop"])["contaminated"]]
    assert flagged == []
    # ... but the same names inside the loop repository still are.
    assert TA.audit_transcript([f"cat {repo}/loop/REPORT.md"], export,
                               forbidden=[repo, repo / "loop"])["contaminated"]


def test_E11_control_open_loop_goal_changed_on_resume(tmp_path):
    import re as _re
    repo, mb = T.make_repo(tmp_path, queue=True)
    fake = T.Fake(repo, mb, lead_script=[{"features": [f"f{k}" for k in range(1, 8)]}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}], mode="open-loop")
    T.run(mb, fake, mode="open-loop")
    (mb / "GOAL.md").write_text(T.GOAL + "- a brand new requirement\n")
    T.git(repo, "commit", "-qam", "new goal")
    st = (mb / "STATE.md").read_text()
    st = _re.sub(r"^status: .*$", "status: running", st, flags=_re.M)
    st = _re.sub(r"^phase: .*$", "phase: idle", st, flags=_re.M)
    (mb / "STATE.md").write_text(st)
    fake2 = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}], mode="open-loop")
    code2 = T.run(mb, fake2, mode="open-loop")
    assert code2 == 5 and "acceptance-goal-changed" in T.log(mb)
    assert not [c for c in fake2.calls if c[0] == "lead"]

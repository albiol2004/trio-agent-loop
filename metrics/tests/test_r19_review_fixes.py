"""r19 review repairs (eval-r19 VERDICT findings 1-13), with the switch ON.

The evaluator's repros R1-R11 (each of which confirmed a defect) ported
here inverted: every test asserts the repaired behaviour, over a REAL git
repo with the real driver, runner, trio-shadow and trio-check.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"


def _ta():
    spec = importlib.util.spec_from_file_location("ta_review_fixes",
                                                  ROOT / "metrics" / "trio-acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _ta()


def shadow(mb, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True, env=env)


def shipped(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    return repo, mb


def state_of(repo, mb) -> dict:
    return TA.load_state(TA.state_file(repo, mb))


# ------------------------------------------------ finding 1: (human) label


class ForgeHuman(T.Fake):
    """Integration Evaluator that labels its amend commit `(human)`."""

    def _eval(self, iteration, context):
        if context.get("kind") == "slice-eval":
            return super()._eval(iteration, context)
        acc = self.mb / "acceptance"
        ids = []
        for k in (1, 2, 3, 4):  # 4 of 8 = 50%, well above the 25% / 2 cap
            (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
            ids.append(f"ACC-{k:02d}")
            with (acc / "AMENDMENTS.md").open("a") as fh:
                fh.write(f"## ACC-{k:02d} · iter {iteration} · human · t\nchange: x\n")
        T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
        T.git_retry(self.repo, "commit", "-qm",
                    f"acceptance: amend {', '.join(ids)} (human): softened")
        self.eval_script = [{"verdict": "SHIP"}]
        return super()._eval(iteration, context)


def test_R1_forged_human_amend_is_judged_as_a_role_amendment(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    feats = [f"f{k}" for k in range(5, 9)]  # ACC-01..04 must FAIL
    fake = ForgeHuman(repo, mb, lead_script=[{"features": feats}], eval_script=[])
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 5, lg
    assert "status: needs_human" in T.state(mb)
    assert "unauthenticated `(human)` label" in lg
    assert "amendment budget exceeded" in lg
    assert "accepted" not in lg.split("unauthenticated")[1]
    assert "SHIP gate:" not in lg
    # The softened checks were restored.
    assert "SystemExit(0)" not in (mb / "acceptance" / "checks" / "acc_01.py").read_text()
    assert TA.manifest_sha256(mb / "acceptance") == state_of(repo, mb)["pin"]
    # Finding 13: the refused SHIP is on record in history.
    assert "loop: iteration 1 — acceptance gate refused SHIP (NEEDS_HUMAN)" in T.subjects(repo)


def test_R1b_trio_shadow_flags_an_unauthenticated_human_amend(tmp_path):
    repo, mb = shipped(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write("## ACC-01 · iter 2 · human · t\nchange: soften\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "acceptance: amend ACC-01 (human): soften")
    proc = shadow(mb)
    assert proc.returncode == 1, proc.stdout
    assert "never authenticated" in proc.stdout


def test_R1c_trioctl_human_amend_is_authenticated_in_driver_state(tmp_path):
    repo, mb = shipped(tmp_path)
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    acc = mb / "acceptance"
    (acc / "checks" / "acc_08.py").write_text("import sys\nsys.exit(1)\n# human fix\n")
    assert ctl.human_amend(["ACC-08"], "fix the check", iteration=2) == 0
    st = state_of(repo, mb)
    amend = T.git(repo, "log", "-1", "--format=%H", "--grep=^acceptance: amend ACC-08 (human)")
    assert amend in st["human_amends"]
    assert st["pin"] == TA.manifest_sha256(acc)
    proc = shadow(mb)
    assert proc.returncode == 0, proc.stdout


# ------------------------------------------------ finding 3: forged pins


def test_R2_trio_shadow_rejects_a_forged_pin_commit(tmp_path):
    repo, mb = shipped(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    forged = TA.manifest_sha256(acc)
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: pin {forged[:12]} (lead)\n\nAcceptance-Pin: {forged}\n")
    proc = shadow(mb)
    assert proc.returncode == 1, proc.stdout
    assert "not an acceptance commit the driver recorded" in proc.stdout
    assert "may only extend" in proc.stdout


def test_R2b_without_driver_state_forged_pin_and_restore_still_fail(tmp_path):
    import os
    repo, mb = shipped(tmp_path)
    blind = dict(os.environ, TRIO_ACCEPTANCE_STATE=str(tmp_path / "elsewhere"))
    assert shadow(mb, blind).returncode == 0
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    forged = TA.manifest_sha256(acc)
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", f"acceptance: pin {forged[:12]} (lead)\n\nAcceptance-Pin: {forged}\n")
    proc = shadow(mb, blind)
    assert proc.returncode == 1 and "may only extend" in proc.stdout, proc.stdout
    # A forged "restore" to a never-pinned pack does not excuse the tamper.
    T.git(repo, "reset", "-q", "--hard", "HEAD~1")
    (acc / "checks" / "acc_02.py").write_text("raise SystemExit(0)\n")
    T.git(repo, "commit", "-qam", "slice(cli): loosen")
    (acc / "checks" / "acc_03.py").write_text("raise SystemExit(0)\n")
    loose = TA.manifest_sha256(acc)
    T.git(repo, "commit", "-qam", f"acceptance: restore (tamper after x)\n\nAcceptance-Pin: {loose}\n")
    proc = shadow(mb, blind)
    assert proc.returncode == 1, proc.stdout
    assert "restores a pack that was never pinned" in proc.stdout
    assert "slice(cli): loosen" in proc.stdout


def test_R2c_the_gate_reads_git_not_the_working_tree_pack(tmp_path):
    import shutil
    repo, mb = shipped(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    T.git(repo, "commit", "-qam", "slice(cli): tamper")
    # The working-tree pack (and its FROZEN `base:`) is Lead-writable: a
    # deleted or rewritten copy must not move or empty the checked range.
    shutil.rmtree(acc)
    proc = shadow(mb)
    assert proc.returncode == 1 and "slice(cli): tamper" in proc.stdout, proc.stdout


def test_driver_state_records_its_own_pack_commits(tmp_path):
    repo, mb = shipped(tmp_path)
    st = state_of(repo, mb)
    freeze = T.git(repo, "log", "-1", "--format=%H", "--grep=^acceptance: freeze")
    assert st["freeze_commit"] == freeze and freeze in st["driver_commits"]
    assert st["human_amends"] == [] and st["tamper_events"] == 0


# ------------------------------------------------ finding 8: node_modules


def test_R3_node_modules_in_the_pack_never_reaches_a_run_or_a_driver_commit(tmp_path):
    repo, mb = shipped(tmp_path)
    acc = mb / "acceptance"
    before = TA.manifest_sha256(acc)
    nm = acc / "checks" / "node_modules" / "planted"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 'lead-controlled';\n")
    assert TA.manifest_sha256(acc) == before  # outside the pin ...
    manifest = json.loads((acc / "MANIFEST.json").read_text())
    probe = dict(manifest["checks"][0], id="ACC-01",
                 run=["python3", "-c",
                      "import os,sys; p=os.path.join(os.environ['ACC_DIR'],'checks','node_modules','planted','index.js');"
                      "sys.exit(0 if os.path.exists(p) else 1)"])
    res = TA.run_pack(acc, str(repo), manifest={**manifest, "checks": [probe]}, exclude={"loop"})
    assert res["results"][0]["outcome"] == "FAIL"  # ... and outside the run
    (acc / "AUTHOR.md").write_text("# inventory v2\n")
    assert TA.commit_paths(repo, ["loop/acceptance"], "acceptance: test commit\n")
    tree = T.git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "loop/acceptance")
    assert "node_modules" not in tree and "loop/acceptance/AUTHOR.md" in tree


# ------------------------------------------------ finding 4 + 12: resume


def _stop_needs_human(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    missing = [f"f{k}" for k in range(1, 8)]  # f8 never implemented
    fake = T.Fake(repo, mb, lead_script=[{"features": missing}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    return repo, mb, missing


def _resume(mb):
    st = (mb / "STATE.md").read_text().replace("status: needs_human", "status: running") \
        .replace("phase: needs_human", "phase: idle")
    (mb / "STATE.md").write_text(st)


def test_R4_manual_human_amend_is_adopted_only_by_the_explicit_command(tmp_path):
    """eval-r19b finding 2: a human's own `(human)` commit made while the
    loop is stopped is adopted by `trioctl ... amend --human --adopt <sha>`
    (recorded in the driver state and a pin trailer), never by resume."""
    repo, mb, missing = _stop_needs_human(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_08.py").write_text("import sys\nsys.exit(1)\n# human fix\n")
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write("## ACC-08 · iter 1 · human · t\nchange: fix\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "acceptance: amend ACC-08 (human): fix the check")
    own = T.git(repo, "rev-parse", "HEAD")
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    # Without naming the commit the amend is refused (explicit adoption only).
    assert ctl.human_amend(["ACC-08"], "fix the check", iteration=1) == 3
    assert ctl.human_amend(["ACC-08"], "fix the check", iteration=1, adopt=[own[:12]]) == 0
    st = state_of(repo, mb)
    assert own in st["human_amends"]
    adoption = st["human_adoptions"][-1]
    assert own in adoption["amends"] and adoption["pin_commit"] == T.git(repo, "rev-parse", "HEAD")
    body = T.git(repo, "log", "-1", "--format=%B")
    assert f"Acceptance-Human-Amend: {own}" in body
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{"features": missing}, {"features": missing}],
                   eval_script=[{"verdict": "NEEDS_HUMAN"}])
    T.run(mb, fake2)
    lg = T.log(mb)
    assert "# human fix" in (acc / "checks" / "acc_08.py").read_text(), lg[-1500:]
    assert "acceptance tamper restored" not in lg
    st = state_of(repo, mb)
    assert st["pin"] == TA.manifest_sha256(acc) and st["tamper_events"] == 0
    assert shadow(mb).returncode == 0, shadow(mb).stdout
    # trio-shadow recognises the adoption from the trailer without the state.
    import os
    blind = dict(os.environ, TRIO_ACCEPTANCE_STATE=str(tmp_path / "elsewhere"))
    proc = shadow(mb, blind)
    assert proc.returncode == 0, proc.stdout


def test_R4b_resume_never_adopts_a_human_labelled_commit(tmp_path):
    repo, mb, missing = _stop_needs_human(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_08.py").write_text("raise SystemExit(0)\n")
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write("## ACC-08 · iter 1 · human · t\nchange: x\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "acceptance: amend ACC-08 (human): x")
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{"features": missing}],
                   eval_script=[{"verdict": "NEEDS_HUMAN"}])
    T.run(mb, fake2)
    lg = T.log(mb)
    assert "not adopted: resume never adopts" in lg
    assert "adopted on resume" not in lg
    assert "acceptance tamper restored" in lg
    assert "SystemExit(0)" not in (acc / "checks" / "acc_08.py").read_text()


def test_goal_changed_on_resume_forces_needs_human(tmp_path):
    repo, mb, missing = _stop_needs_human(tmp_path)
    (mb / "GOAL.md").write_text(T.GOAL + "- a brand new requirement\n")
    T.git(repo, "commit", "-qam", "new goal")
    _resume(mb)
    fake2 = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake2) == 5
    lg = T.log(mb)
    assert "acceptance-goal-changed" in lg
    assert "status: needs_human" in T.state(mb)
    assert not [c for c in fake2.calls if c[0] == "lead"]


# ------------------------------------------------ finding 10 (documented)


def test_R7_amend_cap_on_a_small_pack_stays_fail_safe_zero():
    class C:
        state = {"amendments": 0, "checks": 3}
    assert trio_loop.AcceptanceController.amendments_left(C()) == 0


# ------------------------------------------------ finding 5: early take-over


class EarlyTakeover(T.Fake):
    """Lead pass 1 commits a take-over slice before the (slow) author freezes."""

    def author(self, export, context):
        time.sleep(2.0)
        return super().author(export, context)

    def _lead(self, iteration, context):
        if iteration == 1:
            (self.repo / "app.py").write_text(T.APP.format(features=["f1"]))
            (self.mb / "PLAN.md").write_text(T.plan(T.ALL))
            T.git_retry(self.repo, "add", "-A", "--", "app.py", "loop/PLAN.md")
            T.git_retry(self.repo, "commit", "-qm", "slice(cli): early take-over")
            with (self.mb / "LOG.md").open("a") as fh:
                fh.write(f"- iter {iteration} | lead | pass\n")
            return 0
        return super()._lead(iteration, context)


def test_R8_takeover_before_freeze_no_longer_poisons_the_commit_gate(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = EarlyTakeover(repo, mb, lead_script=[{}, {}, {}],
                         eval_script=[{"verdict": "ITERATE"}, {"verdict": "SHIP"}])
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 0, lg
    assert "gate breach" not in lg
    proc = shadow(mb)
    assert proc.returncode == 0 and "acceptance note:" in proc.stdout


def test_commit_gate_failure_names_the_acceptance_cause(tmp_path):
    repo, mb = shipped(tmp_path)
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    T.git(repo, "commit", "-qam", "slice(cli): make the check pass")
    ok, note = trio_loop._commit_gate(mb, repo)
    assert not ok and "acceptance gate:" in note and "only the driver" in note


# ------------------------------------------------ finding 2: the audit


class RouteAuthor(T.Fake):
    """An honest author whose HTTP check names the GOAL's route literal."""

    def author(self, export, context):
        res = super().author(export, context)
        chk = Path(export) / "acceptance" / "checks" / "acc_01.py"
        chk.write_text(chk.read_text() + '\nROUTE = "/api/openrouter/stats"\n'
                       "# see PLAN.md in the product docs; scratch in /tmp/acc-x\n")
        res["transcript"] = None  # broker history without tool rows -> limited audit
        return res


def test_R10_route_literal_in_an_honest_pack_does_not_contaminate(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = RouteAuthor(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}],
                       author=[{}, {}])
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 0, lg
    assert "contaminated" not in lg
    assert [a for a in state_of(repo, mb)["audits"] if a["limited"] and not a["contaminated"]]


def test_limited_audit_still_flags_the_loop_repository_path(tmp_path):
    repo = tmp_path / "repo"
    hit = TA.audit_output([f'CODE = open("{repo}/src/app.py").read()'], [repo])
    assert hit["contaminated"]
    assert not TA.audit_output(['ROUTE = "/api/openrouter/stats"  # PLAN.md'], [repo])["contaminated"]


def _row(kind, **data):
    return json.dumps({"id": "x", "type": kind, "data": data})


def test_audit_honest_http_author_transcript_is_clean(tmp_path):
    export = tmp_path / "export"
    (export / "docs").mkdir(parents=True)
    (export / "docs" / "PLAN.md").write_text("product doc\n")
    repo = tmp_path / "repo"
    rows = [
        _row("tool_call", command=f"ls {export}"),
        _row("tool_call", command="cat .acceptance-input/GOAL.md"),
        _row("tool_call", command="cat docs/PLAN.md"),  # the export's own product doc
        _row("tool_call", command="which node"),
        json.dumps({"role": "tool", "type": "tool_result",
                    "content": "/home/u/.nvm/versions/node/v22.1.0/bin/node"}),
        json.dumps({"type": "tool_call_output",
                    "output": "README: the plan lives in loop/PLAN.md; GET /api/openrouter/stats"}),
        _row("tool_call", command="/home/u/.nvm/versions/node/v22.1.0/bin/node -e 'x'"),
        _row("tool_call", command="curl -s http://127.0.0.1:8787/api/openrouter/stats"),
        _row("tool_call", command="mkdir -p /tmp/acc-scratch && cp fakes/x /tmp/acc-scratch/"),
        json.dumps({"type": "function_call", "name": "write_file",
                    "arguments": json.dumps({"path": "acceptance/checks/acc_01.py",
                                             "content": 'ROUTE = "/api/openrouter/stats"\n'})}),
        json.dumps({"role": "assistant", "type": "message",
                    "content": "I will not read PLAN.md or VERDICT.md."}),
        "python3 acceptance/checks/acc_01.py",
    ]
    audit = TA.audit_transcript(rows, export, forbidden=[repo, repo / "loop"])
    assert audit == {"contaminated": False, "hits": []}


@pytest.mark.parametrize("row", [
    "cat {repo}/loop/PLAN.md",
    "ls {repo}",
    "grep -rn stats {repo}/src",
    '{{"type": "tool_use", "name": "Read", "input": {{"file_path": "{repo}/src/app.py"}}}}',
    '{{"role": "assistant", "type": "message", "content": [{{"type": "tool_use", '
    '"name": "Read", "input": {{"file_path": "{repo}/loop/VERDICT.md"}}}}]}}',
    "cat ../repo/loop/PLAN.md",
    "find / -name PLAN.md",
    "cat /lab/speed/hard/hidden/pack/check.py",
])
def test_audit_true_contamination_is_flagged(tmp_path, row):
    export = tmp_path / "export"
    export.mkdir()
    repo = tmp_path / "repo"
    audit = TA.audit_transcript([row.format(repo=repo)], export, forbidden=[repo, repo / "loop"])
    assert audit["contaminated"], audit


# ------------------------------------------------ finding 9: escalation


def test_R11_repeated_tamper_escalates_to_error(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"tamper": True}, {}, {"tamper": True}, {},
                                         {"tamper": True}, {}],
                  eval_script=[{"verdict": "ITERATE"}, {"verdict": "ITERATE"}, {"verdict": "SHIP"}])
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 3, lg
    assert lg.count("acceptance tamper restored (lead)") == 2
    assert "acceptance-tamper-repeated" in lg
    assert "status: error" in T.state(mb)
    assert state_of(repo, mb)["tamper_events"] == 2


def test_R11_open_loop_repeated_tamper_escalates_to_error(tmp_path):
    repo, mb = T.make_repo(tmp_path, queue=True)
    fake = T.Fake(repo, mb, mode="open-loop",
                  lead_script=[{"tamper": True}, {}, {"tamper": True}, {}, {}],
                  eval_script=[{"verdict": "ITERATE"}, {"verdict": "SHIP"}])
    code = T.run(mb, fake, mode="open-loop")
    lg = T.log(mb)
    assert code == 3, lg
    assert "acceptance-tamper-repeated" in lg


# ------------------------------------------------ finding 13: SHIP refusal


def test_ship_refusal_is_recorded_in_log_and_history(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": [f"f{k}" for k in range(1, 8)]}, {}],
                  eval_script=[{"verdict": "SHIP"}, {"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    lg = T.log(mb)
    assert "SHIP refused by the acceptance gate (verdict becomes ITERATE)" in lg
    subjects = T.subjects(repo)
    assert "loop: iteration 1 — acceptance gate refused SHIP (ITERATE)" in subjects
    # The refusal record is never mistaken for a SHIP retirement.
    assert not trio_loop._git(repo, "log", "--grep", "loop: iteration 1 — SHIP",
                              "--format=%s").stdout.count("refused")

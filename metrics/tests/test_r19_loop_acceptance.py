"""r19 C6: the loop core's frozen-acceptance phase, with fake role runners
over a REAL git repo and the REAL runner/gates (trio-acceptance.py,
trio-shadow.py, trio-check.py). Lockstep and open-loop.

Scenarios (design §9.3): author isolation audit, freeze ordering + pin
chain, tamper -> restore -> retry -> error, coverage refusal and re-run,
drop at freeze + one retry, UNAVAILABLE -> NEEDS_HUMAN, SHIP refused
unless the pack passes, amendment validation / budget / anti-thrash, and
the switch-off identity with the 9342a57 core.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from metrics import trio_loop

ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"
N_CHECKS = 8
GOAL = "# GOAL\n\n" + "".join(f"- feature f{k} works: `app.py f{k}` prints ok-f{k}\n"
                              for k in range(1, N_CHECKS + 1))
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TRIO_ACCEPTANCE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("TRIO_ACCEPTANCE_SANDBOX", "none")
    monkeypatch.setenv("TRIO_RETIREMENT_WAIT_SECONDS", "0")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True).stdout.strip()


def git_retry(repo, *args):
    for _ in range(100):
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
        if "index.lock" not in proc.stderr:
            raise AssertionError(proc.stderr)
        time.sleep(0.05)
    raise AssertionError("index.lock never released")


APP = """import sys
FEATURES = {features!r}
arg = sys.argv[1] if len(sys.argv) > 1 else ''
print('ok-' + arg if arg in FEATURES else 'v0')
"""


def make_repo(tmp_path, *, queue=False) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    mb = repo / "loop"
    mb.mkdir(parents=True)
    (repo / "app.py").write_text(APP.format(features=[]))
    (mb / "GOAL.md").write_text(GOAL)
    (mb / "PLAN.md").write_text("# PLAN\n")
    (mb / "STATE.md").write_text("schema: 1\niteration: 0\nmax_iterations: 6\nstatus: ready\n"
                                 "phase: idle\nmission: m\n")
    (mb / "LOG.md").write_text("# Trio loop log\n")
    (mb / "REPORT.md").write_text("")
    (mb / "VERDICT.md").write_text("")
    (mb / ".gitignore").write_text(".lock/\n.driver.json\n.session.json\n.repairs\n")
    if queue:
        (mb / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n")
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo, mb


def author_pack(export: Path, *, n=N_CHECKS, passing_at_base=(), unavailable=(),
                bad_quote=()) -> None:
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for k in range(1, n + 1):
        cid = f"ACC-{k:02d}"
        if k in unavailable:
            body = "raise SystemExit(77)\n"
        elif k in passing_at_base:
            body = "raise SystemExit(0)\n"
        else:
            body = ("import subprocess, sys\n"
                    f"out = subprocess.run([sys.executable, 'app.py', 'f{k}'], capture_output=True,"
                    " text=True).stdout.strip()\n"
                    f"print(out)\nsys.exit(0 if out == 'ok-f{k}' else 1)\n")
        (acc / "checks" / f"acc_{k:02d}.py").write_text(body)
        quote = "not in the goal" if k in bad_quote else f"feature f{k} works"
        checks.append({"id": cid, "goal_ref": f"GOAL.md:{k + 2}", "goal_quote": quote,
                       "kind": "behaviour", "surface": "cli",
                       "run": ["python3", f"acceptance/checks/acc_{k:02d}.py"],
                       "expect": {"exit": 0}, "timeout_s": 30, "needs": [], "binds": [],
                       "network": "loopback"})
    (acc / "MANIFEST.json").write_text(json.dumps(
        {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
         "checks": checks}, indent=2))
    (acc / "AUTHOR.md").write_text("# inventory\n")


def plan(covers: list[str], lead_integration: list[str] = ()) -> str:
    li = f"lead_integration: [{', '.join(lead_integration)}]\n" if lead_integration else ""
    return ("# PLAN\n\n```yaml\nslices:\n  - id: cli\n    writes: [app.py]\n"
            f"    covers: [{', '.join(covers)}]\n    accepts: [\"app.py f1 -> ok-f1 | oracle: value\"]\n"
            "```\n\n## Verification standard\nmode: test-first\n" + li)


ALL = [f"ACC-{k:02d}" for k in range(1, N_CHECKS + 1)]


def append_retired(mb: Path, sha: str, n: int) -> None:
    q = mb / "QUEUE.md"
    text = q.read_text()
    i = text.index("retired:\n") + len("retired:\n")
    j = text.index("```", i)
    entry = f"  - slice: cli\n    sha: {sha}\n    at: 2026-01-01T00:00:{n % 60:02d}Z\n"
    q.write_text(text[:j] + entry + text[j:])


def wait_freeze(repo: Path, timeout=30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if git(repo, "log", "--format=%s", "--grep=^acceptance: freeze", check=False):
            return
        time.sleep(0.05)
    raise AssertionError("no freeze commit")


class Fake:
    """Scripted Lead/Evaluator/author over the real repo (lockstep or
    open-loop). `lead_script[n]` / `eval_script[n]` are per-call dicts."""

    def __init__(self, repo, mb, *, lead_script, eval_script, author=None, mode="lockstep"):
        self.repo, self.mb, self.mode = repo, mb, mode
        self.lead_script = list(lead_script)
        self.eval_script = list(eval_script)
        self.author_script = list(author or [{}])
        self.calls: list[tuple] = []
        self.contexts: list[dict] = []
        self.driver_meta: dict = {}
        self.session_ids: dict = {}

    # -- author -----------------------------------------------------------
    def author(self, export, context):
        spec = self.author_script.pop(0) if self.author_script else {}
        self.calls.append(("author", context.get("attempt")))
        self.contexts.append({"author": dict(context)})
        author_pack(Path(export), **spec.get("pack", {}))
        return {"exit": 0, "session": f"s{context.get('attempt')}", "model": "fake-model",
                "effort": "medium", "path": "fake",
                "transcript": spec.get("transcript", [f"ls {export}"])}

    # -- roles --------------------------------------------------------------
    def run(self, role, iteration, mailbox, context=None):
        context = dict(context or {})
        self.calls.append((role, iteration, context.get("kind")))
        self.contexts.append(context)
        if role in ("lead", "repair"):
            return self._lead(iteration, context)
        return self._eval(iteration, context)

    def _lead(self, iteration, context):
        step = self.lead_script.pop(0) if self.lead_script else {}
        wait_freeze(self.repo)
        (self.mb / "PLAN.md").write_text(plan(step.get("covers", ALL), step.get("lead_int", ())))
        feats = step.get("features", [f"f{k}" for k in range(1, N_CHECKS + 1)])
        (self.repo / "app.py").write_text(APP.format(features=feats))
        if step.get("tamper"):
            (self.mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
        git_retry(self.repo, "add", "-A", "--", "app.py", "loop/PLAN.md",
                  *(["loop/acceptance"] if step.get("tamper") else []))
        git_retry(self.repo, "commit", "--allow-empty", "-qm", f"slice(cli): pass {iteration}")
        sha = git(self.repo, "rev-parse", "HEAD")
        if self.mode == "open-loop":
            append_retired(self.mb, sha, iteration)
        with (self.mb / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | pass\n")
        return 0

    def _eval(self, iteration, context):
        if context.get("kind") == "slice-eval":
            with (self.mb / "VERDICT.md").open("a") as fh:
                fh.write(f"\n## slice {context['slice']} @{context['sha']} — SHIP\n"
                         "evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
            return 0
        step = self.eval_script.pop(0) if self.eval_script else {"verdict": "SHIP"}
        sha = context.get("pinned_sha") or ""
        for amend in step.get("amend", []):
            acc = self.mb / "acceptance"
            path = acc / "checks" / f"acc_{amend['k']:02d}.py"
            path.write_text(amend["body"])
            if amend.get("record", True):
                with (acc / "AMENDMENTS.md").open("a") as fh:
                    fh.write(f"## ACC-{amend['k']:02d} · iter {iteration} · evaluator · t\n"
                             f"goal_quote: feature f{amend['k']} works\n"
                             "defect in check: over-specified\nchange: x\n")
            git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            git_retry(self.repo, "commit", "-qm",
                      f"acceptance: amend ACC-{amend['k']:02d} (evaluator, iter {iteration}): x")
        verdict = step["verdict"]
        body = (f"VERDICT: {verdict}\niteration: {iteration}\n"
                f"attempt: {context.get('evaluator_attempt')}\nevaluated: {sha}\n"
                "## Frozen acceptance\nacceptance: see driver run\ndisputes: none\n"
                "amendments: none\n")
        old = (self.mb / "VERDICT.md").read_text()
        sections = old[old.find("\n## slice"):] if "\n## slice" in old else ""
        (self.mb / "VERDICT.md").write_text(body + sections)
        if verdict == "SHIP":
            with (self.mb / "LOG.md").open("a") as fh:
                fh.write(f"- iter {iteration} | evaluator | VERDICT: SHIP — ok\n")
            git_retry(self.repo, "add", "-A", "--", "loop")
            git_retry(self.repo, "commit", "-qm", f"loop: iteration {iteration} — SHIP")
        return 0


def run(mb, fake, *, mode="lockstep", deadline=120.0, acceptance=None):
    acceptance = {"enabled": True, "wait_s": 60} if acceptance is None else acceptance
    result = {}

    def target():
        try:
            result["code"] = trio_loop.run_loop(mb, 6, fake, repo=mb.parent, mode=mode,
                                                poll_seconds=0.01, acceptance=acceptance)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            import traceback
            result["exc"] = traceback.format_exc()

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(deadline)
    assert not th.is_alive(), "loop hung"
    assert "exc" not in result, result.get("exc")
    return result["code"]


def log(mb):
    return (mb / "LOG.md").read_text()


def state(mb):
    return (mb / "STATE.md").read_text()


def subjects(repo):
    return git(repo, "log", "--reverse", "--format=%s").splitlines()


# ------------------------------------------------------------ lockstep


def test_lockstep_happy_path_freeze_first_pin_and_ship(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert run(mb, fake) == 0
    subs = subjects(repo)
    freeze = next(i for i, s in enumerate(subs) if s.startswith("acceptance: freeze"))
    first_slice = next(i for i, s in enumerate(subs) if s.startswith("slice("))
    assert freeze < first_slice
    assert subs[freeze] == f"acceptance: freeze {N_CHECKS} checks (fake-model)"
    body = git(repo, "log", "-1", "--format=%B", "--grep=^acceptance: freeze")
    pin = re.search(r"Acceptance-Pin: ([0-9a-f]{64})", body).group(1)
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert f"pin[0]: {pin} freeze" in frozen
    assert re.search(rf"^acceptance_pin: {pin[:16]} @[0-9a-f]{{12}}$", state(mb), re.M)
    assert "status: shipped" in state(mb)
    lg = log(mb)
    assert "acceptance: frozen 8 check(s)" in lg
    assert "integration pre-run" in lg and "0/8 PASS" not in lg.split("integration pre-run")[1][:40]
    assert "SHIP gate: 8/8 PASS" in lg
    ev = next(c for c in fake.contexts if c.get("evaluator_attempt"))
    assert ev["acceptance"]["text"].startswith("FROZEN ACCEPTANCE @")
    assert ev["acceptance"]["passed"] == 8
    proc = subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    # The author ran once, on the filtered export (the mailbox is not in it).
    export = Path(next(c for c in fake.contexts if "author" in c)["author"]["export"])
    assert not (export / "loop").exists() and not (export / ".git").exists()
    driver = json.loads((mb / ".driver.json").read_text())
    assert driver["acceptance"]["status"] == "frozen" and driver["acceptance"]["pin"] == pin


def test_lockstep_tamper_restored_then_retry_then_error(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{"tamper": True}, {}], eval_script=[{"verdict": "SHIP"}])
    assert run(mb, fake) == 0
    lg = log(mb)
    assert "acceptance tamper restored (lead)" in lg
    subs = subjects(repo)
    assert any(s.startswith("acceptance: restore (tamper after") for s in subs)
    assert "raise SystemExit(0)" not in (mb / "acceptance" / "checks" / "acc_01.py").read_text()
    lead_calls = [c for c in fake.calls if c[0] == "lead"]
    assert len(lead_calls) == 2  # retried once
    # Twice -> gate breach -> status: error.
    repo2, mb2 = make_repo(tmp_path / "b")
    fake2 = Fake(repo2, mb2, lead_script=[{"tamper": True}, {"tamper": True}], eval_script=[])
    assert run(mb2, fake2) == 3
    assert "status: error" in state(mb2)
    assert "gate breach after lead" in log(mb2)


def test_lockstep_coverage_refusal_reruns_lead_with_errors(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{"covers": ALL[:-1]}, {}],
                eval_script=[{"verdict": "SHIP"}])
    assert run(mb, fake) == 0
    assert "lead pass refused: acceptance coverage" in log(mb)
    lead_ctx = [c for c in fake.contexts if "acceptance_errors" in c]
    assert lead_ctx and "ACC-08" in " ".join(lead_ctx[0]["acceptance_errors"])


def test_ship_refused_unless_pack_passes(tmp_path):
    repo, mb = make_repo(tmp_path)
    missing = [f"f{k}" for k in range(1, N_CHECKS)]  # f8 not implemented
    fake = Fake(repo, mb, lead_script=[{"features": missing}, {}],
                eval_script=[{"verdict": "SHIP"}, {"verdict": "SHIP"}])
    assert run(mb, fake) == 0
    lg = log(mb)
    assert "ship_unaccepted (acceptance): 7/8 PASS" in lg and "ACC-08 FAIL" in lg
    second = [c for c in fake.contexts if c.get("acceptance_errors")]
    assert second and "refused the SHIP" in second[0]["acceptance_errors"][0]
    assert "status: shipped" in state(mb)
    assert len([c for c in fake.calls if c[0] == "lead"]) == 2


def test_unavailable_forces_needs_human_and_refuses_ship(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}],
                author=[{"pack": {"unavailable": (3,)}}])
    assert run(mb, fake) == 5
    assert "status: needs_human" in state(mb) and "phase: acceptance-unavailable" in state(mb)
    assert "forced NEEDS_HUMAN (acceptance-unavailable)" in log(mb) and "ACC-03" in log(mb)
    assert "unavailable_at_base: ACC-03" in (mb / "acceptance" / "FROZEN").read_text()


def test_unavailable_only_iterate_is_logged(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{}, {}],
                eval_script=[{"verdict": "ITERATE"}, {"verdict": "NEEDS_HUMAN"}],
                author=[{"pack": {"unavailable": (3,)}}])
    assert run(mb, fake) == 5
    assert "acceptance-unavailable-iterate" in log(mb)


def test_drop_at_freeze_and_one_retry_with_drop_list(tmp_path):
    repo, mb = make_repo(tmp_path)
    fake = Fake(repo, mb, lead_script=[{"covers": ALL[:5]}], eval_script=[{"verdict": "SHIP"}],
                author=[{"pack": {"passing_at_base": (6, 7, 8)}},
                        {"pack": {"passing_at_base": (6, 7, 8)}}])
    assert run(mb, fake) == 0
    authors = [c["author"] for c in fake.contexts if "author" in c]
    assert len(authors) == 2 and authors[0]["dropped"] == []
    assert {d[0] for d in authors[1]["dropped"]} == {"ACC-06", "ACC-07", "ACC-08"}
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert "ACC-06 passes-at-base" in frozen
    manifest = json.loads((mb / "acceptance" / "MANIFEST.json").read_text())
    assert [c["id"] for c in manifest["checks"]] == ALL[:5]
    assert "author retry: 3 dropped" in log(mb)


def test_author_contamination_rerun_then_error(tmp_path):
    repo, mb = make_repo(tmp_path)
    bad = {"transcript": [f"cat {repo}/loop/PLAN.md"]}
    fake = Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}],
                author=[bad, {}])
    assert run(mb, fake) == 0
    authors = [c["author"] for c in fake.contexts if "author" in c]
    assert authors[1]["contaminated_retry"] and "DISCARDED" in authors[1]["prefix"]
    assert "author session contaminated" in log(mb)
    repo2, mb2 = make_repo(tmp_path / "b")
    bad = {"transcript": [f"cat {repo2}/loop/PLAN.md"]}
    fake2 = Fake(repo2, mb2, lead_script=[{}], eval_script=[], author=[bad, bad])

    def lead_no_wait(iteration, context):  # the Lead never sees a freeze
        with (mb2 / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | pass\n")
        return 0
    fake2._lead = lead_no_wait
    assert run(mb2, fake2) == 3
    assert "acceptance-contaminated" in log(mb2)
    assert not (mb2 / "acceptance").exists()


def test_amendment_accepted_rejected_and_budget(tmp_path):
    repo, mb = make_repo(tmp_path)
    good = ("import subprocess, sys\nout = subprocess.run([sys.executable, 'app.py', 'f1'],"
            " capture_output=True, text=True).stdout\nsys.exit(0 if 'ok-f1' in out else 1)\n")
    fake = Fake(repo, mb, lead_script=[{}, {}],
                eval_script=[{"verdict": "ITERATE", "amend": [{"k": 1, "body": good}]},
                             {"verdict": "SHIP"}])
    assert run(mb, fake) == 0
    frozen = (mb / "acceptance" / "FROZEN").read_text()
    assert re.search(r"^pin\[1\]: [0-9a-f]{64} amend ACC-01$", frozen, re.M)
    assert "amendment of ACC-01 accepted" in log(mb)
    assert any(s.startswith("acceptance: pin ") for s in subjects(repo))
    proc = subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    # An amendment that makes the check pass at base is rejected and reverted.
    repo2, mb2 = make_repo(tmp_path / "b")
    fake2 = Fake(repo2, mb2, lead_script=[{}, {}],
                 eval_script=[{"verdict": "ITERATE", "amend": [{"k": 2, "body": "raise SystemExit(0)\n"}]},
                              {"verdict": "SHIP"}])
    assert run(mb2, fake2) == 0
    assert "amendment of ACC-02 rejected" in log(mb2) and "no longer FAILs at base" in log(mb2)
    assert "raise SystemExit(0)" not in (mb2 / "acceptance" / "checks" / "acc_02.py").read_text()
    assert "pin[1]" not in (mb2 / "acceptance" / "FROZEN").read_text()
    # A third amendment exceeds the budget (2 per loop) -> NEEDS_HUMAN.
    repo3, mb3 = make_repo(tmp_path / "c")
    amends = [{"k": k, "body": good.replace("f1", f"f{k}")} for k in (1, 2, 3)]
    fake3 = Fake(repo3, mb3, lead_script=[{}, {}],
                 eval_script=[{"verdict": "ITERATE", "amend": amends[:2]},
                              {"verdict": "ITERATE", "amend": amends[2:]}])
    assert run(mb3, fake3) == 5
    assert "forced NEEDS_HUMAN (acceptance-amendments)" in log(mb3)


def test_anti_thrash_same_check_failing_twice_forces_needs_human(tmp_path):
    repo, mb = make_repo(tmp_path)
    missing = [f"f{k}" for k in range(1, N_CHECKS)]
    fake = Fake(repo, mb, lead_script=[{"features": missing}, {"features": missing}, {}],
                eval_script=[{"verdict": "ITERATE"}, {"verdict": "ITERATE"}])
    assert run(mb, fake) == 5
    assert "forced NEEDS_HUMAN (acceptance-thrash): ACC-08 failed 2 consecutive" in log(mb)


# ------------------------------------------------------------ open-loop


def test_open_loop_covered_line_integration_block_and_ship(tmp_path):
    repo, mb = make_repo(tmp_path, queue=True)
    fake = Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}], mode="open-loop")
    assert run(mb, fake, mode="open-loop") == 0
    slice_ctx = next(c for c in fake.contexts if c.get("kind") == "slice-eval")
    assert slice_ctx["acceptance_covered"].startswith("ACCEPTANCE (covered): ACC-01 PASS")
    assert "[sole-cover]" in slice_ctx["acceptance_covered"]
    integ = next(c for c in fake.contexts if c.get("kind") == "integration-eval")
    assert integ["acceptance"]["passed"] == N_CHECKS
    subs = subjects(repo)
    assert subs.index(next(s for s in subs if s.startswith("acceptance: freeze"))) < \
        subs.index(next(s for s in subs if s.startswith("slice(")))
    assert "status: shipped" in state(mb)


def test_open_loop_coverage_refusal_then_error_on_second(tmp_path):
    repo, mb = make_repo(tmp_path, queue=True)
    fake = Fake(repo, mb, lead_script=[{"covers": ALL[:2]}, {"covers": ALL[:2]}],
                eval_script=[], mode="open-loop")
    assert run(mb, fake, mode="open-loop") == 3
    lg = log(mb)
    assert lg.count("lead pass refused: acceptance coverage") == 2
    assert "gate breach after lead: acceptance" in lg and "status: error" in state(mb)
    ctxs = [c for c in fake.contexts if c.get("kind") == "lead-pass"]
    assert "acceptance_errors" in ctxs[1] and "unmapped acceptance check(s)" in ctxs[1]["acceptance_errors"][0]


def test_open_loop_ship_refused_then_fixed(tmp_path):
    repo, mb = make_repo(tmp_path, queue=True)
    missing = [f"f{k}" for k in range(1, N_CHECKS)]
    fake = Fake(repo, mb, lead_script=[{"features": missing}, {}],
                eval_script=[{"verdict": "SHIP"}, {"verdict": "SHIP"}], mode="open-loop")
    assert run(mb, fake, mode="open-loop") == 0
    assert "ship_unaccepted (acceptance)" in log(mb)
    assert "status: shipped" in state(mb)


# ------------------------------------------------------- switch off


def _old_core(tmp_path) -> object | None:
    have = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", "9342a57^{commit}"],
                          capture_output=True)
    if have.returncode != 0:
        return None
    d = tmp_path / "old-metrics"
    d.mkdir()
    for name in ("trio_loop.py", "trio-metrics.py", "trio-shadow.py", "trio-check.py"):
        (d / name).write_text(subprocess.run(
            ["git", "-C", str(ROOT), "show", f"9342a57:metrics/{name}"],
            capture_output=True, text=True, check=True).stdout)
    spec = importlib.util.spec_from_file_location("trio_loop_9342a57", d / "trio_loop.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Plain:
    """A switch-off run: no author() hook, no acceptance/ anywhere."""

    def __init__(self, repo, mb, mode):
        self.repo, self.mb, self.mode = repo, mb, mode
        self.driver_meta = {}
        self.session_ids = {}
        self.n = 0

    def run(self, role, iteration, mailbox, context=None):
        context = context or {}
        if role == "lead":
            (self.mb / "PLAN.md").write_text(plan([]).replace("    covers: []\n", ""))
            (self.repo / "app.py").write_text(APP.format(features=["f1"]))
            git(self.repo, "add", "-A", "--", "app.py", "loop/PLAN.md")
            git(self.repo, "commit", "-qm", f"slice(cli): pass {iteration}")
            sha = git(self.repo, "rev-parse", "HEAD")
            if self.mode == "open-loop":
                append_retired(self.mb, sha, iteration)
            with (self.mb / "LOG.md").open("a") as fh:
                fh.write(f"- iter {iteration} | lead | pass\n")
            return 0
        if context.get("kind") == "slice-eval":
            with (self.mb / "VERDICT.md").open("a") as fh:
                fh.write(f"\n## slice cli @{context['sha']} — SHIP\n")
            return 0
        sha = context.get("pinned_sha") or ""
        (self.mb / "VERDICT.md").write_text(
            f"VERDICT: SHIP\niteration: {iteration}\nattempt: {context.get('evaluator_attempt')}\n"
            f"evaluated: {sha}\n")
        with (self.mb / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | evaluator | VERDICT: SHIP — ok\n")
        git(self.repo, "add", "-A", "--", "loop")
        git(self.repo, "commit", "-qm", f"loop: iteration {iteration} — SHIP")
        return 0


def _normal(text: str) -> str:
    text = re.sub(r"[0-9a-f]{40}", "<sha>", text)
    text = re.sub(r"\b[0-9a-f]{12}\b", "<sha12>", text)
    text = re.sub(r"[0-9a-f]{32}", "<attempt>", text)
    return re.sub(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", "<ts>", text)


@pytest.mark.parametrize("mode", ["lockstep", "open-loop"])
@pytest.mark.parametrize("switch", [None, {"enabled": False}])
def test_switch_off_is_byte_identical_to_9342a57_core(tmp_path, mode, switch):
    old = _old_core(tmp_path)
    if old is None:
        pytest.skip("9342a57 is not in this clone's history")
    outs = []
    for label, core in (("old", old), ("new", trio_loop)):
        repo, mb = make_repo(tmp_path / label, queue=mode == "open-loop")
        kw = {} if (label == "old" or switch is None) else {"acceptance": switch}
        code = core.run_loop(mb, 4, Plain(repo, mb, mode), repo=repo, mode=mode,
                             poll_seconds=0.01, **kw)
        driver = json.loads((mb / ".driver.json").read_text())
        driver.pop("pid", None)
        outs.append((code, _normal(log(mb)), _normal(state(mb)), driver,
                     [s for s in subjects(repo)]))
    assert outs[0] == outs[1]
    assert not (tmp_path / "state").exists()  # no acceptance state was ever written

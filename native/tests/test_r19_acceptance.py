"""r19 N2/N4: frozen acceptance on the native helper (real-git fixtures).

Every op here runs with ``--acceptance 1``, the pack under
``TRIO_ACCEPTANCE_SANDBOX=none`` (fast; bwrap is covered by the metrics
suite) and long ops inline (``TRIO_NATIVE_JOBS=inline``) unless a test
exercises the detached job itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_step_ops import HELPER, NATIVE, TOKEN, git, git_env, mbox  # noqa: F401

GOAL = ("# Goal\n"
        "Ship app.py: `python3 app.py N` prints hello N for every N.\n"
        "Keep the README.\n")
QUOTE = "prints hello N"
MODELS = {"lead": "claude-opus-5-5", "evaluator": "claude-opus-5-5",
          "acceptance": "claude-opus-5-5"}
PLAN_ALL = """\
# Plan
```yaml
slices:
  - id: app
    writes: [app.py]
    reads: []
    covers: [ACC-01, ACC-02, ACC-03, ACC-04, ACC-05]
```

## Verification standard
"""
CHECK = """\
import subprocess, sys
r = subprocess.run([sys.executable, "app.py", "{k}"], capture_output=True, text=True)
if r.returncode == 0 and "hello {k}" in r.stdout:
    sys.exit(0)
print("app.py does not print hello {k}")
sys.exit(1)
"""


#: The digest the script hands back (trio-native.js ACC_KEYS).
ACC_KEYS = ("status", "pin", "pin_commit", "freeze_commit", "base", "tamper_events",
            "amendments", "seq")


def acc_env(**extra: str) -> dict[str, str]:
    env = git_env()
    env.update(TRIO_ACCEPTANCE_SANDBOX="none", TRIO_NATIVE_JOBS="inline")
    env.update(extra)
    return env


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
    (box / "GOAL.md").write_text(GOAL, encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n",
                                  encoding="utf-8")
    (box / "PLAN.md").write_text(PLAN_ALL.replace(
        "    covers: [ACC-01, ACC-02, ACC-03, ACC-04, ACC-05]\n", ""), encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    # An older mailbox in the tree: never part of the author's export.
    old = root / "loop-old"
    old.mkdir()
    (old / "GOAL.md").write_text("old goal\n", encoding="utf-8")
    (old / "PLAN.md").write_text("old plan SECRET-PLAN\n", encoding="utf-8")
    (old / "STATE.md").write_text("iteration: 3\nstatus: shipped\nphase: idle\n",
                                  encoding="utf-8")
    git(root, "add", "loop", "loop-old")
    git(root, "commit", "-q", "-m", "loop: init")
    return root


class Run:
    """One workflow execution's view: the digest the script would hold."""

    def __init__(self, repo: Path, env: dict | None = None, token: str = TOKEN):
        self.repo, self.token = repo, token
        self.env = env or acc_env()
        self.digest: dict = {}
        self.seq = 0
        self.exec_id = ""

    def __call__(self, op: str, *, acceptance: bool = True, **kw) -> dict:
        self.seq += 1
        # the script's nonce: <token>/<exec_id>/<seq>/<op> once begin minted it
        nonce = (f"{self.token}/{self.exec_id}/{self.seq}/{op}" if self.exec_id
                 else f"{self.token}/{self.seq}/{op}")
        cmd = [sys.executable, str(HELPER), op, "--mailbox", str(mbox(self.repo)),
               "--token", self.token, "--nonce", nonce, "--json"]
        if acceptance:
            cmd += ["--acceptance", "1"]
            if self.digest and "acc" not in kw:
                kw["acc"] = json.dumps({k: self.digest.get(k) for k in ACC_KEYS})
        for key, value in kw.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            cmd += [f"--{key.replace('_', '-')}", str(value)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=self.env)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["nonce"] == nonce and out["op"] == op
        if op == "begin" and out.get("ok") and out.get("exec_id"):
            self.exec_id = out["exec_id"]
        acc = out.get("acceptance")
        if isinstance(acc, dict) and isinstance(acc.get("status"), str):
            self.digest = acc
        return out


def write_pack(export: Path, n: int = 5, *, passing: tuple = (), unavailable: tuple = (),
               quote: str = QUOTE) -> None:
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for k in range(1, n + 1):
        name = f"acc_{k:02d}.py"
        if k in passing:
            body = "import os, sys\nsys.exit(0 if os.path.isfile('README') else 1)\n"
        elif k in unavailable:
            body = "import sys\nprint('needs a live service')\nsys.exit(77)\n"
        else:
            body = CHECK.format(k=k)
        (acc / "checks" / name).write_text(body, encoding="utf-8")
        checks.append({"id": f"ACC-{k:02d}", "goal_ref": "GOAL.md:2", "goal_quote": quote,
                       "kind": "behaviour", "surface": "cli",
                       "run": ["python3", f"acceptance/checks/{name}"],
                       "expect": {"exit": 0}, "timeout_s": 30})
    (acc / "MANIFEST.json").write_text(json.dumps({
        "acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
        "checks": checks}, indent=2), encoding="utf-8")
    (acc / "AUTHOR.md").write_text("# inventory\n- testable-black-box: hello N\n",
                                   encoding="utf-8")


def begin(run: Run) -> dict:
    b = run("begin", models=MODELS)
    assert b["ok"], b
    return b


def freeze(run: Run, **pack) -> dict:
    """begin -> next (lead 1) -> export -> an honest author -> freeze."""
    begin(run)
    n = run("next", max_iterations=4)
    assert n["ok"] and n["action"] == "lead", n
    ex = run("acceptance-export", iteration=1, attempt=1)
    assert ex["ok"] and ex["frozen"] is False, ex
    write_pack(Path(ex["export"]), **pack)
    fr = run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"],
             prior={}, author={"exit": 0}, model="claude-opus-5-5")
    assert fr["ok"], fr
    return {"export": ex, "freeze": fr}


def implement(repo: Path, upto: int = 5, *, iteration: int = 1) -> None:
    ok = ", ".join(str(k) for k in range(1, upto + 1))
    (repo / "app.py").write_text(
        "import sys\n"
        f"if int(sys.argv[1]) in ({ok},):\n    print('hello', sys.argv[1])\n",
        encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", f"slice(app): hello {iteration}")


def lead_pass(repo: Path, iteration: int = 1, *, upto: int = 5, plan: str = PLAN_ALL) -> None:
    (mbox(repo) / "PLAN.md").write_text(plan, encoding="utf-8")
    implement(repo, upto, iteration=iteration)
    with (mbox(repo) / "LOG.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- iter {iteration} | lead | done\n")
    (mbox(repo) / "REPORT.md").write_text(f"# Report {iteration}\n", encoding="utf-8")


def ship_verdict(repo: Path, iteration: int, pin: dict, word: str = "SHIP") -> None:
    box = mbox(repo)
    (box / "VERDICT.md").write_text(
        f"VERDICT: {word}\n# Verdict — iteration {iteration}\n"
        f"attempt: {pin['evaluator_attempt']}\nevaluated: {pin['sha']}\n"
        + (f"commit: {pin['sha']}\n" if word == "SHIP" else ""), encoding="utf-8")
    if word == "SHIP":
        git(repo, "add", "loop/VERDICT.md")
        git(repo, "commit", "-q", "-m", f"loop: iteration {iteration} — SHIP")


def state_text(repo: Path) -> str:
    return (mbox(repo) / "STATE.md").read_text(encoding="utf-8")


def log_text(repo: Path) -> str:
    return (mbox(repo) / "LOG.md").read_text(encoding="utf-8")


def to_prerun(run: Run, repo: Path, *, upto: int = 5) -> tuple[dict, dict]:
    freeze(run)
    lead_pass(repo, 1, upto=upto)
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert g["ok"] and g["pass"], g
    p = run("pin", acceptance=False, iteration=1)
    assert p["ok"], p
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert ar["ok"], ar
    return p, ar


# ------------------------------------------------------------------ N1
def test_begin_refuses_a_cheaper_author_before_taking_the_lock(repo: Path) -> None:
    run = Run(repo)
    b = run("begin", models={**MODELS, "acceptance": "claude-sonnet-5"})
    assert not b["ok"] and "Lead/Evaluator tier" in b["error"]
    assert not (mbox(repo) / ".lock").exists()
    b = run("begin", models={**MODELS, "lead": "claude-sonnet-5", "acceptance": None})
    assert not b["ok"] and "tier" in b["error"]
    assert begin(run)["acceptance"].get("pin") is None


def test_generated_author_agent_is_on_the_evaluator_tier() -> None:
    root = NATIVE.parent
    agent = (root / ".claude" / "agents" / "trio-acceptance.md").read_text(encoding="utf-8")
    evaluator = (root / ".claude" / "agents" / "trio-evaluator.md").read_text(encoding="utf-8")
    model = next(ln for ln in evaluator.splitlines() if ln.startswith("model:"))
    assert model in agent.splitlines()[:8]
    assert "name: trio-acceptance" in agent and "disallowedTools: Agent, WebFetch, WebSearch" in agent
    assert "trioctl" not in agent and "{export}" not in agent
    proc = subprocess.run([sys.executable, str(root / "prompts" / "generate.py"), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------------------ export
def test_export_is_the_base_without_git_or_mailboxes(repo: Path) -> None:
    run = Run(repo)
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    assert ex["ok"] and ex["frozen"] is False and ex["base"] == git(repo, "rev-parse", "HEAD")
    export = Path(ex["export"])
    assert not str(export).startswith(str(repo))  # outside the repository
    names = sorted(p.relative_to(export).as_posix() for p in export.rglob("*"))
    assert names == [".acceptance-input", ".acceptance-input/GOAL.md", "README"], names
    assert (export / ".acceptance-input" / "GOAL.md").read_text() == GOAL
    assert ex["tool"].endswith("metrics/trio-acceptance.py")
    assert ex["marker"].endswith("-1")
    assert run.digest["status"] == "authoring"


# ------------------------------------------------------------ freeze
def test_freeze_is_a_driver_commit_with_the_pin(repo: Path) -> None:
    run = Run(repo)
    got = freeze(run)
    fr = got["freeze"]
    assert fr["action"] == "frozen" and fr["checks"] == 5, fr
    assert fr["audit"]["limited"] is True  # no author transcript in the fixture
    head = git(repo, "log", "-1", "--format=%s%n%b")
    assert head.startswith("acceptance: freeze 5 checks (claude-opus-5-5)")
    assert f"Acceptance-Pin: {run.digest['pin']}" in head
    assert run.digest["status"] == "frozen"
    assert run.digest["pin_commit"] == git(repo, "rev-parse", "HEAD")
    files = git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert all(f.startswith("loop/acceptance/") for f in files), files
    assert "acceptance_pin:" in state_text(repo)
    manifest = json.loads((mbox(repo) / "acceptance" / "MANIFEST.json").read_text())
    assert manifest["author"]["path"] == "native"
    assert manifest["author"]["model"] == "claude-opus-5-5"
    # idempotent: a retried freeze step answers frozen without a new commit
    again = run("acceptance-freeze", iteration=1, attempt=1, marker=got["export"]["marker"],
                prior={}, author={"exit": 0}, model="claude-opus-5-5")
    assert again["action"] == "frozen" and git(repo, "rev-parse", "HEAD") == run.digest["pin_commit"]
    # a later export of the frozen loop authors nothing
    assert run("acceptance-export", iteration=1, attempt=1)["frozen"] is True


def test_freeze_drops_checks_passing_at_base_with_one_retry(repo: Path) -> None:
    run = Run(repo)
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    write_pack(Path(ex["export"]), 6, passing=(5, 6))
    fr = run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"], prior={},
             author={"exit": 0}, model="claude-opus-5-5")
    assert fr["action"] == "retry", fr
    assert sorted(d[0] for d in fr["dropped"]) == ["ACC-05", "ACC-06"]
    assert git(repo, "log", "-1", "--format=%s") == "loop: init"  # nothing frozen yet
    fr = run("acceptance-freeze", iteration=1, attempt=2, marker=ex["marker"],
             prior={"retried": True}, author={"exit": 0}, model="claude-opus-5-5")
    assert fr["action"] == "frozen" and fr["checks"] == 4, fr
    frozen = (mbox(repo) / "acceptance" / "FROZEN").read_text()
    assert "ACC-05 passes-at-base" in frozen


def _transcript(tmp: Path, marker: str, calls: list[dict]) -> Path:
    base = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-repo" / "sess" / \
        "subagents" / "workflows" / "wf_abc"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"agent-{len(list(base.iterdir()))}.jsonl"
    rows = [{"type": "user", "message": {"role": "user",
                                         "content": f"ACCEPTANCE-AUTHOR-RUN: {marker}\nwork"}}]
    for call in calls:
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": call["name"], "input": call["input"]}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_audit_reads_the_author_transcript_and_reauthors_once(repo: Path, tmp_path: Path) -> None:
    run = Run(repo, acc_env(CLAUDE_CONFIG_DIR=os.environ["CLAUDE_CONFIG_DIR"]))
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    write_pack(Path(ex["export"]))
    # a relative read with no cd resolves in the session cwd: the repository
    _transcript(tmp_path, f"{ex['marker']}-a1",
                [{"name": "Bash", "input": {"command": "cat loop/PLAN.md"}}])
    fr = run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"], prior={},
             author={"exit": 0}, model="claude-opus-5-5")
    assert fr["action"] == "reauthor", fr
    assert fr["audit"]["limited"] is False and fr["audit"]["transcripts"]
    assert "PREVIOUS ATTEMPT WAS DISCARDED" in fr["prefix"]
    assert not (Path(ex["export"]) / "acceptance").exists()  # the export was rebuilt
    write_pack(Path(ex["export"]))
    _transcript(tmp_path, f"{ex['marker']}-a2", [{"name": "Grep", "input": {"pattern": "SECRET"}}])
    fr = run("acceptance-freeze", iteration=1, attempt=2, marker=ex["marker"],
             prior={"contaminated": True}, author={"exit": 0}, model="claude-opus-5-5")
    assert fr["action"] == "stop", fr
    stop = fr["acceptance"]["stop"]
    assert stop["reason"] == "acceptance-contaminated" and stop["status"] == "error"
    assert "status: error" in state_text(repo)
    assert "acceptance stopped the loop (acceptance-contaminated)" in log_text(repo)


def test_audit_passes_an_honest_author(repo: Path, tmp_path: Path) -> None:
    run = Run(repo)
    begin(run)
    run("next", max_iterations=4)
    ex = run("acceptance-export", iteration=1, attempt=1)
    exp = ex["export"]
    write_pack(Path(exp))
    _transcript(tmp_path, f"{ex['marker']}-a1", [
        {"name": "Bash", "input": {"command": f"cd {exp} && cat .acceptance-input/GOAL.md"}},
        {"name": "Read", "input": {"file_path": f"{exp}/README"}},
        {"name": "Grep", "input": {"pattern": "hello", "path": exp}},
    ])
    fr = run("acceptance-freeze", iteration=1, attempt=1, marker=ex["marker"], prior={},
             author={"exit": 0}, model="claude-opus-5-5")
    assert fr["action"] == "frozen", fr
    assert fr["audit"]["limited"] is False and not fr["audit"]["contaminated"]


# ---------------------------------------------------------- coverage
def test_coverage_refuses_unmapped_checks_then_stops(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    partial = {"slices": [{"id": "app", "covers": ["ACC-01", "ACC-02"]}],
               "lead_integration": ["ACC-03"], "acceptance_bindings": {}}
    c = run("coverage", iteration=1, attempt=1, plan=partial)
    assert c["ok"] and c["covered_ok"] is False, c
    assert any("structured plan: unmapped acceptance check(s): ACC-04, ACC-05" in r
               for r in c["refusals"]), c["refusals"]
    assert "lead pass refused: acceptance coverage" in log_text(repo)
    full = {"slices": [{"id": "app", "covers": ["ACC-01", "ACC-02", "ACC-03"]}],
            "lead_integration": ["ACC-04", "ACC-05"], "acceptance_bindings": {}}
    c = run("coverage", iteration=1, attempt=2, plan=full)
    assert c["covered_ok"] is True, c
    assert "ACC-03 (behaviour)" in c["briefs"]["app"] and "--ids ACC-01,ACC-02,ACC-03" in c["briefs"]["app"]
    # PLAN.md as written must map them too
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL.replace(", ACC-05", ""), encoding="utf-8")
    c = run("coverage", iteration=1, attempt=2, plan=full)
    assert c["covered_ok"] is False
    assert c["acceptance"]["stop"]["reason"] == "acceptance-coverage"
    assert "status: error" in state_text(repo)


def test_coverage_rejects_bad_structured_items(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    plan = {"slices": [{"id": "app", "covers": ["ACC-01", "ACC-02", "ACC-03", "ACC-04",
                                                   "ACC-05", "acc 9"]}],
            "lead_integration": [], "acceptance_bindings": {"NOPE": "/x", "bad name": "y"}}
    c = run("coverage", iteration=1, attempt=1, plan=plan)
    text = " ".join(c["refusals"])
    assert "'acc 9' is not an acceptance id" in text
    assert "not declared in the manifest: NOPE" in text
    assert "'bad name'" in text


def test_dispatch_is_refused_while_plan_md_leaves_a_check_unmapped(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    d = run("dispatch", iteration=1, wave=1)
    assert not d["ok"] and "builder dispatch refused (frozen acceptance)" in d["error"]
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    d = run("dispatch", iteration=1, wave=1)
    assert d["ok"], d


# ------------------------------------------------------- pin / tamper
def test_gate_restores_a_pack_edit_and_escalates_the_second(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1)
    check = mbox(repo) / "acceptance" / "checks" / "acc_01.py"
    check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    g = run("gate", role="lead", iteration=1, attempt=1)
    assert g["ok"] and not g["pass"], g
    assert any("acceptance: acceptance/ was edited" in f for f in g["failures"])
    assert check.read_text() == CHECK.format(k=1)  # restored from the pin
    assert not git(repo, "status", "--porcelain", "--", "loop/acceptance")
    assert run.digest["tamper_events"] == 1
    # the second breach, committed this time: restored by a driver commit
    check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", "slice(app): tweak the check")
    g = run("gate", role="lead", iteration=1, attempt=2)
    assert g["final"] and g["status"] == "error"
    assert any("acceptance-tamper-repeated" in f for f in g["failures"]), g
    assert "status: error" in state_text(repo)
    assert git(repo, "log", "-1", "--format=%s").startswith("acceptance: restore (tamper")
    assert check.read_text() == CHECK.format(k=1)


def test_state_file_edit_under_a_running_workflow_needs_a_human(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    path = Path(run.digest["state_file"])
    data = json.loads(path.read_text())
    data["pin"] = "0" * 64
    path.write_text(json.dumps(data))
    (mbox(repo) / "PLAN.md").write_text(PLAN_ALL, encoding="utf-8")
    c = run("coverage", iteration=1, attempt=1,
            plan={"slices": [{"id": "app", "covers": ["ACC-01"]}]})
    assert c["acceptance"]["stop"]["reason"] == "acceptance-state-mismatch"
    assert "status: needs_human" in state_text(repo)


def test_counters_in_the_state_file_only_add_strictness(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1)
    check = mbox(repo) / "acceptance" / "checks" / "acc_01.py"
    check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    assert not run("gate", role="lead", iteration=1, attempt=1)["pass"]
    path = Path(run.digest["state_file"])
    data = json.loads(path.read_text())
    data["tamper_events"] = 0  # a same-uid process "forgets" the first breach
    path.write_text(json.dumps(data))
    check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    g = run("gate", role="lead", iteration=1, attempt=2)
    assert any("acceptance-tamper-repeated" in f for f in g["failures"]), g


# --------------------------------------------------------- SHIP gate
def test_prerun_block_and_ship_refused_while_a_check_fails(repo: Path) -> None:
    run = Run(repo)
    p, ar = to_prerun(run, repo, upto=3)
    assert ar["passed"] == 3 and ar["failed"] == 2 and ar["total"] == 5
    assert ar["text"].startswith(f"FROZEN ACCEPTANCE @{p['sha'][:12]}: 3/5 PASS")
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["ok"], ap
    assert ap["verdict"] == "ITERATE" and not ap["stop"], ap
    acc = ap["acceptance"]
    assert acc["verdict_in"] == "SHIP" and acc["ship_refused"] is True
    assert acc["ship_gate"]["failed"] == 2
    assert "SHIP refused by the acceptance gate (verdict becomes ITERATE)" in log_text(repo)
    assert "acceptance gate refused SHIP (ITERATE)" in git(repo, "log", "-1", "--format=%s")
    n = run("next", max_iterations=4)
    assert n["action"] == "lead" and n["iteration"] == 2
    assert any("ACC-04 FAIL" in e for e in n["acceptance"]["errors"]), n
    # idempotent: the recorded apply answers the same
    again = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert again["verdict"] == "ITERATE"


def test_ship_gate_passes_and_ships(repo: Path) -> None:
    run = Run(repo)
    p, ar = to_prerun(run, repo)
    assert ar["passed"] == 5
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "SHIP" and ap["code"] == 0 and ap["status"] == "shipped", ap
    assert ap["acceptance"]["ship_gate"]["passed"] == 5
    assert "SHIP gate: 5/5 PASS" in log_text(repo)
    end = run("end")
    result = json.loads((mbox(repo) / ".native-result.json").read_text())
    assert result["acceptance"]["pin"] == run.digest["pin"]
    assert result["acceptance"]["ship_gate"]["passed"] == 5
    assert end["acceptance"]["checks"] == 5


def test_unavailable_check_forces_needs_human(repo: Path) -> None:
    run = Run(repo)
    freeze(run, n=6, unavailable=(6,))
    assert run.digest["checks"] == 6
    lead_pass(repo, 1, plan=PLAN_ALL.replace("ACC-05]", "ACC-05, ACC-06]"))
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    ar = run("acceptance-run", iteration=1, sha=p["sha"])
    assert ar["unavailable"] == 1
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "NEEDS_HUMAN" and ap["code"] == 5 and ap["stop"], ap
    assert "phase: acceptance-unavailable" in state_text(repo)


def _amend(repo: Path, body: str, cid: str = "ACC-05") -> None:
    acc = mbox(repo) / "acceptance"
    (acc / "checks" / f"acc_{cid[-2:]}.py").write_text(body, encoding="utf-8")
    with (acc / "AMENDMENTS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"## {cid} · iter 1 · evaluator · 2026-09-30T00:00:00Z\n"
                 f"goal_quote: {QUOTE}\ndefect in check: flaky\nchange: x\n")
    git(repo, "add", "loop/acceptance")
    git(repo, "commit", "-q", "-m", f"acceptance: amend {cid} (evaluator, iter 1): x")


def test_amendment_that_passes_at_base_is_reverted(repo: Path) -> None:
    run = Run(repo)
    p, _ar = to_prerun(run, repo, upto=4)
    pin = run.digest["pin"]
    _amend(repo, "import sys\nsys.exit(0)\n")
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["ok"], ap
    assert "amendment of ACC-05 rejected" in log_text(repo)
    assert run.digest["pin"] == pin
    assert (mbox(repo) / "acceptance" / "checks" / "acc_05.py").read_text() == CHECK.format(k=5)
    assert ap["verdict"] == "ITERATE" and ap["acceptance"]["ship_refused"]


def test_valid_amendment_extends_the_pin_chain(repo: Path) -> None:
    run = Run(repo)
    p, _ar = to_prerun(run, repo)
    pin = run.digest["pin"]
    _amend(repo, CHECK.format(k=5) + "# clearer failure text\n")
    ship_verdict(repo, 1, p)
    ap = run("apply", iteration=1, attempt=p["evaluator_attempt"])
    assert ap["verdict"] == "SHIP" and ap["code"] == 0, ap
    assert "amendment of ACC-05 accepted" in log_text(repo)
    assert ap["acceptance"]["pin"] != pin and ap["acceptance"]["amendments"] == 1
    assert "pin[1]:" in (mbox(repo) / "acceptance" / "FROZEN").read_text()


# ------------------------------------------------------------ resume
def test_resume_with_a_changed_goal_needs_a_human(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    assert run("end")["ok"]
    (mbox(repo) / "GOAL.md").write_text(GOAL + "More.\n", encoding="utf-8")
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["ok"] and b["acceptance"]["stop"]["reason"] == "acceptance-goal-changed", b
    assert "status: needs_human" in state_text(repo)


def test_resume_reconciles_and_keeps_the_pin(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    pin = run.digest["pin"]
    path = Path(run.digest["state_file"])
    assert run("end")["ok"]
    path.unlink()  # a lost driver state: the helper's sealed record restores it
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["pin"] == pin and b["acceptance"]["status"] == "frozen", b
    assert "overwritten from the sealed record" in log_text(repo)
    assert json.loads(path.read_text())["pin"] == pin


def test_resume_without_a_sealed_record_rederives_the_pin_from_git(repo: Path) -> None:
    """eval-r19n2 (d): no sealed record (a first run, or it was moved away)
    -> git alone, exactly as before."""
    run = Run(repo)
    freeze(run)
    pin = run.digest["pin"]
    path = Path(run.digest["state_file"])
    assert run("end")["ok"]
    path.unlink()
    top = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    next((top / "trio-native").glob("*/acc-sealed.json")).unlink()
    fresh = Run(repo)
    b = fresh("begin", models=MODELS)
    assert b["acceptance"]["pin"] == pin and b["acceptance"]["status"] == "frozen", b
    assert "pin re-derived from git history" in log_text(repo)


# -------------------------------------------------------------- jobs
def test_long_op_answers_pending_then_the_result(repo: Path) -> None:
    run = Run(repo)
    freeze(run)
    lead_pass(repo, 1)
    assert run("gate", role="lead", iteration=1, attempt=1)["pass"]
    p = run("pin", acceptance=False, iteration=1)
    env = dict(run.env)
    env.pop("TRIO_NATIVE_JOBS")
    env["TRIO_NATIVE_JOB_WAIT_S"] = "0"
    run.env = env
    first = run("acceptance-run", iteration=1, sha=p["sha"])
    assert first["ok"] and first.get("pending") is True, first
    env["TRIO_NATIVE_JOB_WAIT_S"] = "120"
    got = run("acceptance-run", iteration=1, sha=p["sha"], poll=1)
    assert got["ok"] and got.get("pending") is None and got["passed"] == 5, got
    # a retried step gets the finished job's answer, the pack is not re-run
    before = log_text(repo).count("integration pre-run")
    again = run("acceptance-run", iteration=1, sha=p["sha"])
    assert again["passed"] == 5 and log_text(repo).count("integration pre-run") == before


# ---------------------------------------------------- switch-off identity
def _old_tree(dest: Path) -> Path | None:
    top = NATIVE.parent
    proc = subprocess.run(["git", "-C", str(top), "cat-file", "-e", "6842907^{commit}"],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    dest.mkdir()
    archive = subprocess.run(["git", "-C", str(top), "archive", "6842907", "native", "metrics"],
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return dest / "native" / "trio_native_step.py"


def _script(repo: Path, helper: Path, env: dict) -> tuple[list, str, str, str]:
    outs = []
    seq = [0]

    def op(name: str, **kw) -> dict:
        seq[0] += 1
        cmd = [sys.executable, str(helper), name, "--mailbox", str(mbox(repo)), "--token", TOKEN,
               "--nonce", f"{TOKEN}/{seq[0]}/{name}", "--json"]
        for k, v in kw.items():
            cmd += [f"--{k.replace('_', '-')}", str(v)]
        out = json.loads(subprocess.run(cmd, capture_output=True, text=True, env=env,
                                        check=True).stdout)
        outs.append({k: v for k, v in out.items()
                     if k not in ("exec_id", "tmpdir", "eval_worktree", "eval_scratch",
                                  "nonce", "exclude_path", "repo", "mailbox", "reclaimed")})
        return out
    op("begin")
    op("next", max_iterations=4)
    lead_pass(repo, 1, upto=4)
    op("gate", role="lead", iteration=1, attempt=1)
    p = op("pin", iteration=1)
    ship_verdict(repo, 1, p, "ITERATE")
    op("apply", iteration=1, attempt=p["evaluator_attempt"])
    op("next", max_iterations=4)
    lead_pass(repo, 2)
    op("gate", role="lead", iteration=2, attempt=1)
    p = op("pin", iteration=2)
    ship_verdict(repo, 2, p)
    op("apply", iteration=2, attempt=p["evaluator_attempt"])
    op("end")
    subjects = git(repo, "log", "--format=%s")
    return outs, log_text(repo), state_text(repo), subjects


def test_switch_off_is_identical_to_6842907(tmp_path: Path) -> None:
    old = _old_tree(tmp_path / "old-tree")
    if old is None:
        pytest.skip("6842907 is not in this checkout's history")
    results = []
    for tag, helper in (("old", old), ("new", HELPER)):
        base = tmp_path / tag
        base.mkdir()
        root = base / "product"
        root.mkdir()
        git(root, "init", "-q")
        (root / "README").write_text("x\n")
        git(root, "add", "README")
        git(root, "commit", "-q", "-m", "init")
        (root / "loop").mkdir()
        (mbox(root) / "GOAL.md").write_text(GOAL)
        (mbox(root) / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n")
        (mbox(root) / "PLAN.md").write_text(PLAN_ALL.replace(
            "    covers: [ACC-01, ACC-02, ACC-03, ACC-04, ACC-05]\n", ""))
        (mbox(root) / "LOG.md").write_text("# Trio loop log\n")
        git(root, "add", "loop")
        git(root, "commit", "-q", "-m", "loop: init")
        env = git_env()
        env.update(GIT_AUTHOR_DATE="2026-09-30T00:00:00Z", GIT_COMMITTER_DATE="2026-09-30T00:00:00Z",
                   TRIO_NATIVE_RUNS_DIR=str(base / "runs"), XDG_STATE_HOME=str(base / "xdg"))
        results.append(_script(root, helper, env))
    import re as _re

    def norm(tag: str, value) -> str:
        text = json.dumps(value, sort_keys=True).replace(str(tmp_path / tag), "<BASE>")
        text = _re.sub(r"[0-9a-f]{32,40}", "<HEX>", text)
        return _re.sub(r"(eval-<HEX>-\d+-)[0-9a-f]{8}", r"\1<ATT>", text)
    (old_out, old_log, old_state, old_git), (new_out, new_log, new_state, new_git) = results
    assert norm("new", new_out) == norm("old", old_out)
    assert norm("new", [new_log, new_state, new_git]) == norm("old", [old_log, old_state, old_git])
    assert "acceptance" not in json.dumps(new_out)
    assert [o.get("op") for o in new_out] == ["begin", "next", "gate", "pin", "apply", "next",
                                             "gate", "pin", "apply", "end"]
    assert new_out[-2]["verdict"] == "SHIP" and new_out[-2]["code"] == 0
    assert not (tmp_path / "new" / "xdg" / "trio-agent-loop" / "acceptance").exists()

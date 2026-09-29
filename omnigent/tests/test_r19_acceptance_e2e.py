"""r19 end to end (fake role runner, real git, real loop core, real
`trioctl omnigent loop`, real isolated builders): frozen acceptance ON in a
root-free open-loop and in a root-free lockstep loop; coverage refusal
before any worktree; tamper restore; switch off leaves no trace."""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import time
from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, World, git, init_repo

TOKENS = {"a-one": ["alpha", "bravo", "charlie"], "a-two": ["delta", "echo", "foxtrot"]}
FILES = {"a-one": "src/a.py", "a-two": "src/b.py"}


def _goal() -> str:
    return "# Goal\n\n" + "".join(
        f"- `{FILES[s]}` mentions {tok}\n" for s, toks in TOKENS.items() for tok in toks)


def _checks() -> list[tuple[str, str, str]]:
    out = []
    n = 0
    for sid, toks in TOKENS.items():
        for tok in toks:
            n += 1
            out.append((f"ACC-{n:02d}", sid, tok))
    return out


def fake_author(runner, export, context):
    export = Path(export)
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for cid, sid, tok in _checks():
        name = f"{cid.lower().replace('-', '_')}.py"
        (acc / "checks" / name).write_text(
            "import pathlib, sys\n"
            f"p = pathlib.Path('{FILES[sid]}')\n"
            f"ok = p.is_file() and '{tok}' in p.read_text()\n"
            f"print('ok' if ok else 'missing {tok}')\nsys.exit(0 if ok else 1)\n")
        checks.append({"id": cid, "goal_ref": "GOAL.md", "goal_quote": f"`{FILES[sid]}` mentions {tok}",
                       "kind": "behaviour", "surface": "cli",
                       "run": ["python3", f"acceptance/checks/{name}"], "expect": {"exit": 0},
                       "timeout_s": 30, "needs": [], "binds": [], "network": "loopback"})
    (acc / "MANIFEST.json").write_text(json.dumps(
        {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {}, "checks": checks}))
    (acc / "AUTHOR.md").write_text("# inventory\n")
    return {"exit": 0, "session": "author-1", "model": "m", "effort": "medium",
            "path": "fake", "transcript": [f"ls {export}"]}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, tag="r19e2e")
    monkeypatch.setenv("TRIO_ACCEPTANCE_SANDBOX", "none")
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    monkeypatch.setattr(w.trioctl.OmnigentRunner, "author", fake_author)
    w.builder_prompts = []
    real_worker = w._worker

    def worker(role, config, **kw):
        w.builder_prompts.append(kw.get("prompt", ""))
        return real_worker(role, config, **kw)

    monkeypatch.setattr(w.trioctl, "run_cursor_worker", worker)
    return w


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    shutil.copy2(REPO_ROOT / "metrics" / "trio-acceptance.py", home / "metrics" / "trio-acceptance.py")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "vendor trio-acceptance.py")
    return home


def _slices(lockstep=False):
    return [{"id": sid, "write": FILES[sid], "content": " ".join(TOKENS[sid]) + "\n"}
            for sid in TOKENS]


def _wait_frozen(box: Path, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (box / "acceptance" / "FROZEN").is_file() and "acceptance_pin:" in (box / "STATE.md").read_text():
            return
        time.sleep(0.05)
    raise AssertionError("acceptance never froze")


def _map_and_commit(workspace: Path, box: Path, covers: dict[str, list[str]]):
    plan = (box / "PLAN.md").read_text()
    for sid, ids in covers.items():
        plan = plan.replace(f"  - id: {sid}\n", f"  - id: {sid}\n    covers: [{', '.join(ids)}]\n", 1)
    (box / "PLAN.md").write_text(plan)
    rel = box.relative_to(workspace).as_posix()
    git(workspace, "add", "--", f"{rel}/PLAN.md")
    git(workspace, "commit", "-q", "-m", "loop: map frozen acceptance", "--", f"{rel}/PLAN.md")


FULL = {"a-one": ["ACC-01", "ACC-02", "ACC-03"], "a-two": ["ACC-04", "ACC-05", "ACC-06"]}


def _mapping_lead(world, covers=FULL, lockstep=False):
    def hook(w, spec, runner, ctx, workspace, box, prompt, iteration):
        _wait_frozen(box)
        if "covers:" not in (box / "PLAN.md").read_text():
            _map_and_commit(Path(runner.repo), box, covers)
        if lockstep:
            w.lockstep_lead(spec, runner, workspace, box, iteration)
        else:
            w.lead(spec, runner, box, iteration)
        return True
    return hook


def _live_box(world, home, spec) -> Path:
    record = world.rf.load_record(world.wt, home, spec["slug"])
    if record and Path(record["live_mailbox"]).is_dir():
        return Path(record["live_mailbox"])
    return spec["root_box"]


def test_root_free_open_loop_acceptance_on_ships_and_lands(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/a", _slices())
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    world.hooks["lead-pass"] = _mapping_lead(world)
    code = world.run_loop(spec, "--acceptance")
    box = home / "loop/a"
    assert code == 0, (box / "LOG.md").read_text()
    subjects = git(home, "log", "--reverse", "--format=%s", "main").splitlines()
    freeze = next(i for i, s in enumerate(subjects) if s.startswith("acceptance: freeze 6 checks"))
    first_slice = next(i for i, s in enumerate(subjects) if s.startswith("slice("))
    assert freeze < first_slice, subjects
    log = (box / "LOG.md").read_text()
    assert "acceptance: frozen 6 check(s)" in log and "SHIP gate: 6/6 PASS" in log
    assert "integration pre-run" in log
    frozen = (box / "acceptance" / "FROZEN").read_text()
    assert re.search(r"^pin\[0\]: [0-9a-f]{64} freeze$", frozen, re.M)
    # Every builder brief carried its frozen checks; slice-evals got the
    # covered line; the integration-eval got the pre-run block + procedure;
    # the Lead got the acceptance procedure.
    assert all("## Acceptance (frozen; do not edit)" in p for p in world.builder_prompts)
    slice_prompts = [e["prompt"] for e in world.events if e["kind"] == "slice-eval"]
    assert slice_prompts and all("ACCEPTANCE (covered): ACC-0" in p for p in slice_prompts)
    integ = [e["prompt"] for e in world.events if e["kind"] == "integration-eval"]
    assert integ and "FROZEN ACCEPTANCE @" in integ[0] and "## Frozen acceptance" in integ[0]
    lead = [e["prompt"] for e in world.events if e["kind"] == "lead-pass"]
    assert "trioctl omnigent acceptance wait --mailbox" in lead[0]
    # The author's export never contained the mailbox or git.
    driver = json.loads((_live_box(world, home, spec) / ".driver.json").read_text()) \
        if (_live_box(world, home, spec) / ".driver.json").is_file() else {}
    assert driver == {} or driver.get("acceptance", {}).get("status") == "frozen"
    shadow = world.trioctl.subprocess.run(
        ["python3", str(REPO_ROOT / "metrics" / "trio-shadow.py"), "--mailbox", str(box),
         "--require-commits"], capture_output=True, text=True)
    assert "acceptance gate" not in shadow.stdout, shadow.stdout


def test_root_free_lockstep_acceptance_on_ships_and_lands(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/l", _slices(), lockstep=True)
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    world.hooks["lead"] = _mapping_lead(world, lockstep=True)
    code = world.run_loop(spec, "--acceptance")
    box = home / "loop/l"
    assert code == 0, (box / "LOG.md").read_text()
    log = (box / "LOG.md").read_text()
    assert "acceptance: frozen 6 check(s)" in log and "SHIP gate: 6/6 PASS" in log
    evals = [e for e in world.events if e["kind"] == "evaluator"]
    assert evals and "FROZEN ACCEPTANCE @" in evals[0]["prompt"]
    assert "acceptance_pin:" in (box / "STATE.md").read_text()
    subjects = git(home, "log", "--reverse", "--format=%s", "main").splitlines()
    assert subjects.index(next(s for s in subjects if s.startswith("acceptance: freeze"))) < \
        subjects.index(next(s for s in subjects if s.startswith("slice(")))


def test_coverage_refusal_before_any_worktree_then_rerun_maps(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/c", _slices())
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    attempts = {"n": 0, "refused": None, "worktrees": None}

    def lead(w, spec_, runner, ctx, workspace, box, prompt, iteration):
        attempts["n"] += 1
        _wait_frozen(box)
        if attempts["n"] == 1:
            # Unmapped: the real builder dispatch is refused before a worktree.
            before = git(Path(runner.repo), "worktree", "list")
            args = w.trioctl.parser().parse_args([
                "omnigent", "run", "builder", "--isolate", "--mailbox", str(box),
                "--worker-slice", "a-one", "--worktree-root", str(runner._isolate["worktree_root"]),
                "--workspace", str(runner.repo), "--prompt-file", str(box / "briefs" / "a-one.md")])
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                attempts["refused"] = w.trioctl.command_run(args)
            attempts["worktrees"] = (before, git(Path(runner.repo), "worktree", "list"))
            attempts["err"] = err.getvalue()
            with (box / "LOG.md").open("a") as fh:
                fh.write(f"- iter {iteration} | lead | tried a builder before mapping\n")
            plan = (box / "PLAN.md").read_text()
            (box / "PLAN.md").write_text(plan + "\n")  # a change, so the pass is not empty
            return True
        attempts["prompt2"] = prompt
        if "covers:" not in (box / "PLAN.md").read_text():
            _map_and_commit(Path(runner.repo), box, FULL)
        w.lead(spec_, runner, box, iteration)
        return True

    world.hooks["lead-pass"] = lead
    code = world.run_loop(spec, "--acceptance")
    box = home / "loop/c"
    assert code == 0, (box / "LOG.md").read_text()
    assert attempts["refused"] == world.trioctl.ACCEPTANCE_REFUSED_EXIT
    assert attempts["worktrees"][0] == attempts["worktrees"][1]
    assert "unmapped acceptance check(s)" in attempts["err"]
    assert "lead pass refused: acceptance coverage" in (box / "LOG.md").read_text()
    assert "ACCEPTANCE REFUSAL" in attempts["prompt2"] and "unmapped" in attempts["prompt2"]


def test_lead_tamper_is_restored_and_the_loop_ships(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/t", _slices())
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    mapping = _mapping_lead(world)
    seen = {"n": 0}

    def lead(w, spec_, runner, ctx, workspace, box, prompt, iteration):
        seen["n"] += 1
        if seen["n"] == 1:
            _wait_frozen(box)
            check = box / "acceptance" / "checks" / "acc_01.py"
            check.write_text("raise SystemExit(0)\n")
            rel = box.relative_to(Path(runner.repo)).as_posix()
            git(Path(runner.repo), "add", "--", f"{rel}/acceptance")
            git(Path(runner.repo), "commit", "-q", "-m", "loop: loosen a check")
            with (box / "LOG.md").open("a") as fh:
                fh.write(f"- iter {iteration} | lead | loosened a check\n")
            return True
        seen["prompt2"] = prompt
        return mapping(w, spec_, runner, ctx, workspace, box, prompt, iteration)

    world.hooks["lead-pass"] = lead
    code = world.run_loop(spec, "--acceptance")
    box = home / "loop/t"
    live = _live_box(world, home, spec)
    assert code == 0, (live / "LOG.md").read_text() + (live / "STATE.md").read_text()
    log = (box / "LOG.md").read_text()
    assert "acceptance tamper restored" in log
    assert "raise SystemExit(0)" not in (box / "acceptance" / "checks" / "acc_01.py").read_text()
    subjects = git(home, "log", "--format=%s", "main")
    assert "acceptance: restore (tamper after" in subjects
    assert "ACCEPTANCE REFUSAL" in seen["prompt2"] and "restored from the pin" in seen["prompt2"]


def test_switch_off_leaves_no_acceptance_trace(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/o", _slices())
    code = world.run_loop(spec)
    box = home / "loop/o"
    assert code == 0, (box / "LOG.md").read_text()
    assert not (box / "acceptance").exists()
    assert "acceptance" not in (box / "LOG.md").read_text()
    assert "acceptance_pin" not in (box / "STATE.md").read_text()
    for e in world.events:
        assert "FROZEN ACCEPTANCE" not in e["prompt"] and "ACCEPTANCE (covered)" not in e["prompt"]
    assert not any("## Acceptance (frozen" in p for p in world.builder_prompts)
    assert not (tmp_path / "state" / "trio-agent-loop" / "acceptance").exists()

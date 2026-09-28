"""r15 guard in trioctl: refuse slices that write outside the mailbox repo.

Spec r15 item 7. `trioctl omnigent loop` refuses an offending PLAN before
dispatching anything; a Lead pass that writes one ends the open-loop driver
with `status: error` (exit 3) before any evaluator is dispatched; `run
builder --isolate` refuses before creating a worktree. The check itself is
metrics/trio-check.py `repo_scope_refusals` (loop core untouched).
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from metrics import trio_loop

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "trioctl"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}
UNSUPPORTED = (
    "repos: declared but multi-repo slices are not supported by this "
    "release (r15 pending)"
)


def _load(name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


trioctl = _load("trioctl_r15_guard")


def message(slice_id: str, path: Path) -> str:
    return (
        f"slice {slice_id} writes outside the mailbox repo ({path}); declare "
        "it in PLAN.md repos: (r15) or move the mailbox into that repo"
    )


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout


def plan(slices: str, header: str = "") -> str:
    return f"# PLAN\n\n{header}```yaml\nslices:\n{slices}```\n"


OK = "  - id: home-ok\n    writes: [src/ok.py]\n    reads: []\n    status: planned\n"
BAD = "  - id: bridge\n    writes: [app-backend/app/x.py]\n    reads: []\n    status: planned\n"
BRIEF_BAD = "# Brief\n\n## Targeted check\n\ncd app-backend && python3 -m pytest -q\n"
BRIEF_OK = "# Brief\n\n## Targeted check\n\npython3 -m pytest -q tests\n"
REPOS = (
    "```yaml\nrepos:\n  - name: app-backend\n    path: app-backend\n"
    "    base: dev\n```\n\n"
)


def make_home(tmp_path: Path, plan_text: str, *, queue: bool = True) -> tuple[Path, Path]:
    home = tmp_path / "home"
    box = home / "loop"
    (box / "briefs").mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("app-backend/\n")
    (home / "src").mkdir()
    (home / "src" / "ok.py").write_text("x = 1\n")
    nested = home / "app-backend"
    (nested / "app").mkdir(parents=True)
    git(nested, "init", "-q", "-b", "dev")
    (box / "GOAL.md").write_text("# Goal\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 3\nstatus: running\nmission: m\n"
    )
    (box / "PLAN.md").write_text(plan_text)
    (box / "REPORT.md").write_text("# Report\n")
    (box / "VERDICT.md").write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    (box / "briefs" / "bridge.md").write_text(BRIEF_BAD)
    (box / "briefs" / "home-ok.md").write_text(BRIEF_OK)
    if queue:
        (box / "QUEUE.md").write_text("# Queue\n")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "init")
    return home, box


def worktrees(home: Path) -> list[str]:
    return [ln for ln in git(home, "worktree", "list").splitlines() if ln.strip()]


# --- loop start: refuse before anything is dispatched ------------------------


class NoRunner:
    def __init__(self, **_kw):
        raise AssertionError("OmnigentRunner constructed despite the r15 refusal")


class NoLoop:
    @staticmethod
    def run_loop(*_a, **_kw):
        raise AssertionError("run_loop called despite the r15 refusal")


def _loop_args(box: Path) -> argparse.Namespace:
    return trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(box), "--max-iterations", "3"]
    )


def test_loop_start_refuses_offending_plan(tmp_path, monkeypatch, capsys):
    home, box = make_home(tmp_path, plan(OK + BAD))
    monkeypatch.chdir(home)
    monkeypatch.setattr(trioctl, "OmnigentRunner", NoRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: NoLoop)
    args = _loop_args(box)
    assert args.func(args) == 3
    line = message("bridge", home / "app-backend" / "app" / "x.py")
    assert f"- iter 1 | loop | {line}" in (box / "LOG.md").read_text().splitlines()
    assert "status: error" in (box / "STATE.md").read_text().splitlines()
    assert f"trioctl: {line}" in capsys.readouterr().err
    assert len(worktrees(home)) == 1


def test_loop_start_refuses_declared_repos(tmp_path, monkeypatch, capsys):
    home, box = make_home(tmp_path, plan(OK + BAD, REPOS))
    monkeypatch.chdir(home)
    monkeypatch.setattr(trioctl, "OmnigentRunner", NoRunner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: NoLoop)
    args = _loop_args(box)
    assert args.func(args) == 3
    log = (box / "LOG.md").read_text().splitlines()
    assert f"- iter 1 | loop | {UNSUPPORTED}" in log
    assert not any("slice bridge" in ln for ln in log)  # covered, not refused
    assert UNSUPPORTED in capsys.readouterr().err


def test_loop_start_single_repo_unchanged(tmp_path, monkeypatch):
    home, box = make_home(tmp_path, plan(OK))
    monkeypatch.chdir(home)
    calls = []

    class Runner:
        def __init__(self, **kw):
            calls.append("runner")

    class Loop:
        @staticmethod
        def run_loop(*_a, **_kw):
            calls.append("loop")
            return 0

    monkeypatch.setattr(trioctl, "OmnigentRunner", Runner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: Loop)
    monkeypatch.setattr(trioctl, "_resolve_isolation", lambda *a: (None, "test"))
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(box), "--max-iterations", "3",
         "--keep-sessions"]
    )
    before = (box / "LOG.md").read_text()
    assert args.func(args) == 0
    assert calls == ["runner", "loop"]
    assert (box / "LOG.md").read_text() == before


# --- Lead pass writes an offending PLAN (fake runner, real loop core) --------


def _runner(home: Path, monkeypatch, seen: list, on_lead):
    runner = trioctl.OmnigentRunner(
        repo=home, broker_client=object(), config={}, interval=0,
        workspace=str(home),
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda role, *a, **k: f"{role} prompt\n")
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration,
                     mailbox, ctx, started, before_text, before_mtime,
                     dispatch, workspace):
        seen.append(role)
        if role == "lead":
            on_lead(mailbox, iteration)
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner


def _lead_writes_bad_plan(mailbox: Path, iteration: int) -> None:
    (mailbox / "PLAN.md").write_text(plan(OK + BAD))
    with (mailbox / "LOG.md").open("a") as fh:
        fh.write(f"- iter {iteration} | lead | planned bridge\n")


def test_open_loop_lead_pass_writing_offending_plan_ends_status_error(
    tmp_path, monkeypatch, capsys
):
    home, box = make_home(tmp_path, plan(OK))
    seen: list[str] = []
    runner = _runner(home, monkeypatch, seen, _lead_writes_bad_plan)
    code = trio_loop.run_loop(box, 3, runner, repo=home, poll_seconds=0.01)
    assert code == 3
    assert seen == ["lead"]  # no evaluator, no second pass
    line = message("bridge", home / "app-backend" / "app" / "x.py")
    log = (box / "LOG.md").read_text().splitlines()
    assert any(ln.startswith("- iter ") and ln.endswith(f"| loop | {line}") for ln in log)
    assert "status: error" in (box / "STATE.md").read_text().splitlines()
    assert f"trioctl: {line}" in capsys.readouterr().err
    assert len(worktrees(home)) == 1


def test_lead_pass_not_dispatched_over_offending_plan(tmp_path, monkeypatch):
    home, box = make_home(tmp_path, plan(OK + BAD))
    seen: list[str] = []
    runner = _runner(home, monkeypatch, seen, lambda *a: None)
    with pytest.raises(trioctl.RepoScopeRefusal):
        runner.run("lead", 2, box, {"mode": "open-loop", "kind": "lead-pass"})
    assert seen == []
    # eval-r15a F8: labelled with STATE.md's iteration (1), not the pass's 2.
    assert "- iter 1 | loop | " + message(
        "bridge", home / "app-backend" / "app" / "x.py"
    ) in (box / "LOG.md").read_text().splitlines()


def test_lockstep_lead_refusal_exits_3_via_command_loop(tmp_path, monkeypatch):
    home, box = make_home(tmp_path, plan(OK), queue=False)
    seen: list[str] = []
    runner = _runner(home, monkeypatch, seen, _lead_writes_bad_plan)
    monkeypatch.chdir(home)
    monkeypatch.setattr(trioctl, "OmnigentRunner", lambda **kw: runner)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: trio_loop)
    monkeypatch.setattr(trioctl, "_resolve_isolation", lambda *a: (None, "test"))
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", str(box), "--max-iterations", "3",
         "--keep-sessions"]
    )
    assert args.func(args) == 3
    assert seen == ["lead"]
    assert "status: error" in (box / "STATE.md").read_text().splitlines()


def test_single_repo_lead_pass_unaffected(tmp_path, monkeypatch):
    home, box = make_home(tmp_path, plan(OK))
    seen: list[str] = []
    runner = _runner(home, monkeypatch, seen, lambda *a: None)
    assert runner.run("lead", 2, box, {"mode": "open-loop", "kind": "lead-pass"}) == 0
    assert seen == ["lead"]


# --- run builder --isolate: refuse before creating the worktree -------------


def _run_builder(box: Path, home: Path, slice_id: str, brief: Path):
    return trioctl.parser().parse_args(
        ["omnigent", "run", "builder", "--isolate", "--mailbox", str(box),
         "--worker-slice", slice_id, "--workspace", str(home),
         "--prompt-file", str(brief)]
    )


@pytest.fixture()
def no_worktree(monkeypatch):
    monkeypatch.setattr(trioctl, "load_config", lambda path: {})
    created = []

    def create(*a, **kw):
        created.append(kw.get("slice_id"))
        raise AssertionError("worktree created despite the r15 refusal")

    monkeypatch.setattr(trioctl.worker_worktrees, "create", create)
    return created


def test_run_builder_isolate_refused_before_worktree(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path, plan(OK + BAD))
    args = _run_builder(box, home, "bridge", box / "briefs" / "bridge.md")
    assert args.func(args) == trioctl.REPO_SCOPE_REFUSED_EXIT == 2
    assert no_worktree == []
    line = message("bridge", home / "app-backend" / "app" / "x.py")
    assert f"trioctl: {line}" in capsys.readouterr().err
    assert len(worktrees(home)) == 1


def test_run_builder_isolate_refuses_brief_targeted_check(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path, plan(OK))
    brief = tmp_path / "task.md"
    brief.write_text("## Targeted check\n\ncd ../.. && pytest -q\n")
    args = _run_builder(box, home, "home-ok", brief)
    assert args.func(args) == 2
    assert message("home-ok", home.parent.parent) in capsys.readouterr().err
    assert no_worktree == []


def test_run_builder_isolate_refuses_declared_repos(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path, plan(OK + BAD, REPOS))
    args = _run_builder(box, home, "home-ok", box / "briefs" / "home-ok.md")
    assert args.func(args) == 2
    assert UNSUPPORTED in capsys.readouterr().err


def test_run_builder_isolate_other_slice_passes_guard(tmp_path, no_worktree):
    """Only the dispatched slice is checked; a clean one reaches create()."""
    home, box = make_home(tmp_path, plan(OK + BAD))
    args = _run_builder(box, home, "home-ok", box / "briefs" / "home-ok.md")
    with pytest.raises(AssertionError, match="worktree created"):
        args.func(args)
    assert no_worktree == ["home-ok"]


# --- Lead prompt wording -------------------------------------------------------


REPO_ROOT = ROOT.parent


@pytest.mark.parametrize(
    "relative",
    [
        "prompts/canonical/lead.md",
        ".claude/agents/trio-lead.md",
        "portable/prompts/lead.md",
        "omnigent/entrypoints/trio-omnigent/prompts/lead.md",
    ],
)
def test_lead_prompt_states_the_repo_scope_rule(relative):
    text = " ".join((REPO_ROOT / relative).read_text().split())
    assert "must stay inside the mailbox repo" in text
    assert "Every slice's `repo:` (omit it or use `.`)" in text
    assert "unless PLAN.md declares `repos:`, which this release refuses" in text
    assert "a nested clone with its own `.git` is not a workaround" in text

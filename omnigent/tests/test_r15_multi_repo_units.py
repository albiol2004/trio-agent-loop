"""r15 multi-repo units: worker_worktrees per repo, trioctl prompt rendering
(single-repo byte-identical, multi-repo addenda), builder repo note."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "trioctl"
MODULE = ROOT / "worker_worktrees.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=dict(os.environ, **GIT_ENV)).stdout.strip()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    wt = _load("ww_r15u", MODULE)
    trioctl = _load("trioctl_r15u", SCRIPT)
    monkeypatch.setattr(trioctl, "worker_worktrees", wt)
    return wt, trioctl


@pytest.fixture()
def layout(tmp_path):
    home = tmp_path / "home"
    box = home / "loop"
    box.mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("svc/\n")
    (home / "a.txt").write_text("a\n")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "init")
    svc = home / "svc"
    svc.mkdir()
    git(svc, "init", "-q", "-b", "dev")
    (svc / "s.py").write_text("s\n")
    git(svc, "add", "-A")
    git(svc, "commit", "-qm", "init")
    (box / "PLAN.md").write_text(
        "```yaml\nrepos:\n  - name: svc\n    path: svc\n    base: dev\n```\n\n"
        "```yaml\nslices:\n  - id: s1\n    repo: svc\n    writes: [s.py]\n"
        "  - id: h1\n    writes: [a.txt]\n```\n"
    )
    return home, box, svc


def test_default_worktree_root_name_and_base(env, layout, tmp_path):
    wt, _ = env
    home, _box, svc = layout
    home_root = wt.default_worktree_root(home)
    got = wt.default_worktree_root(svc, name="svc", base=home_root.parent)
    assert got.parent == home_root.parent and got.name.startswith("svc-")
    assert got != wt.default_worktree_root(home, name="svc", base=home_root.parent)


def test_create_refuses_a_repo_off_its_base_branch(env, layout, tmp_path):
    wt, _ = env
    _home, box, svc = layout
    git(svc, "checkout", "-qb", "other")
    with pytest.raises(wt.WorktreeError, match="not its PLAN.md repos: base 'dev'"):
        wt.create(svc, slice_id="s1", mailbox=box, root=tmp_path / "r", repo_name="svc",
                  base_branch="dev", declared=["s.py"])


def test_record_carries_repo_name_and_declared_writes(env, layout, tmp_path):
    wt, _ = env
    home, box, svc = layout
    rec = wt.create(svc, slice_id="s1", mailbox=box, root=tmp_path / "r", repo_name="svc",
                    base_branch="dev", declared=["s.py"])
    assert rec["repo_name"] == "svc" and rec["declared_writes"] == ["s.py"]
    assert Path(rec["repo"]) == svc
    assert (svc / ".git" / "trio-worktrees" / f"{rec['id']}.json").is_file()
    home_rec = wt.create(home, slice_id="h1", mailbox=box, root=tmp_path / "h")
    assert "repo_name" not in home_rec and "declared_writes" not in home_rec


def test_declared_writes_scope_the_aggregate_check(env, layout, tmp_path):
    """A dirty file outside the repo's own slices' writes is foreign there."""
    wt, _ = env
    _home, box, svc = layout
    (svc / "other.py").write_text("x\n")
    git(svc, "add", "other.py")
    git(svc, "commit", "-qm", "o")
    (svc / "other.py").write_text("dirty\n")
    with pytest.raises(wt.WorktreeError, match="outside every declared product path"):
        wt.create(svc, slice_id="s1", mailbox=box, root=tmp_path / "r", repo_name="svc",
                  declared=["s.py"])


def test_acceptance_uses_the_records_repo_pin(env, layout, tmp_path):
    wt, _ = env
    _home, box, svc = layout
    rec = wt.create(svc, slice_id="s1", mailbox=box, root=tmp_path / "r", repo_name="svc",
                    declared=["s.py"])
    (Path(rec["path"]) / "s.py").write_text("new\n")
    wt.mark_exited(svc, wt.load_record(svc, rec["id"]), 0)
    rec = wt.integrate(svc, rec["id"], summary="x")
    head = git(svc, "rev-parse", "HEAD")
    pending = dict(rec)
    assert wt._accepting_revision(svc, pending, lambda _b: {"evaluated": "f" * 40}) is None
    assert "no evaluated pin for repo svc" in pending["acceptance_pending"]
    ok = dict(rec)
    got = wt._accepting_revision(
        svc, ok, lambda _b: {"evaluated": "f" * 40, "evaluated_repos": {"svc": head}})
    assert got is not None


def _runner(trioctl, home, isolate=None):
    return trioctl.OmnigentRunner(repo=home, broker_client=object(), config={}, interval=0,
                                  workspace=str(home), isolate_workers=isolate)


def test_single_repo_open_loop_prompt_has_no_multi_repo_text(env, tmp_path):
    _wt, trioctl = env
    home = tmp_path / "solo"
    (home / "loop").mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / "loop" / "PLAN.md").write_text("```yaml\nslices:\n  - id: a\n```\n")
    runner = _runner(trioctl, home)
    for ctx in ({"mode": "open-loop", "kind": "lead-pass"},
                {"mode": "open-loop", "kind": "slice-eval", "slice": "a", "sha": "a" * 40},
                {"mode": "open-loop", "kind": "integration-eval", "pinned_sha": "b" * 40,
                 "evaluator_attempt": "x", "iteration": 1}):
        role = "lead" if ctx["kind"] == "lead-pass" else "evaluator"
        text = runner._prompt(role, 1, home / "loop", ctx)
        assert "MULTI-REPO (" not in text and "MULTI-REPO:" not in text
        assert text.startswith(trioctl.OmnigentRunner._open_loop_context_block(ctx))


def test_multi_repo_prompts_render_the_addenda(env, layout):
    _wt, trioctl = env
    home, box, svc = layout
    runner = _runner(trioctl, home)
    lead = runner._prompt("lead", 2, box, {"mode": "open-loop", "kind": "lead-pass"})
    assert f"  - `svc`: `{svc}` (base `dev`)" in lead and f"  - `home`: `{home}`" in lead
    assert "gate: PASS @X:[0-9a-f]{40}" in lead
    se = runner._prompt("evaluator", 2, box, {"mode": "open-loop", "kind": "slice-eval",
                                              "slice": "s1", "sha": "c" * 40, "repo": "svc"})
    assert f"`git -C {svc} worktree add <tmp> {'c' * 40}`" in se
    ie = runner._prompt("evaluator", 2, box, {
        "mode": "open-loop", "kind": "integration-eval", "pinned_sha": "a" * 40,
        "evaluator_attempt": "att", "iteration": 2,
        "pins": {"home": "a" * 40, "svc": "d" * 40}})
    assert f"`evaluated: home@{'a' * 40}, svc@{'d' * 40}`" in ie
    assert "loop: iteration 2 — SHIP (loop)" in ie
    lock = runner._prompt("evaluator", 2, box, {"evaluator_attempt": "att", "pinned_sha": "a" * 40,
                                                "pins": {"home": "a" * 40, "svc": "d" * 40}})
    assert lock.startswith(f"LOCKSTEP CONTEXT: attempt=att sha={'a' * 40} pins=home@{'a' * 40},svc@{'d' * 40}\n")


def test_eval_repo_falls_back_to_plan_when_context_has_no_repo(env, layout):
    _wt, trioctl = env
    _home, box, svc = layout
    name, declared = trioctl.OmnigentRunner._eval_repo(box, {"slice": "s1", "sha": "x"})
    assert name == "svc" and declared["path"] == svc
    assert trioctl.OmnigentRunner._eval_repo(box, {"slice": "h1"}) == ("home", None)


def test_builder_repo_note(env):
    _wt, trioctl = env
    note = trioctl._ISOLATED_BUILDER_REPO_NOTE.format(name="svc", repo="/r/svc", branch="dev")
    assert "declared repo `svc`" in note and "Never `cd` into or edit" in note

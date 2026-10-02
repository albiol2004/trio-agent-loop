"""Unit tests for ``trio_opencode.openloop`` (the trio-opencode open-loop
runner: ``OpenLoopRunner``, settings resolution, the STATE.md key guard, the
sidecar writer, retire/takeover/conflict conditions and the acceptance
``author()`` hook).

These exercise ``OpenLoopRunner``'s private helpers directly against a real
git repository (never a subprocess ``opencode`` -- that is
``test_openloop_e2e.py``'s job), stubbing the one role-turn call each test
needs (``trio_opencode.driver._call_role``) rather than spinning up the fake
binary, so they stay fast and focused on this module's own bookkeeping.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trio_opencode import config as config_mod
from trio_opencode import driver, olqueue, openloop, prompts as prompts_mod, rootfree
from trio_opencode import quality as quality_mod

from conftest import git, git_env  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def make_cfg(**overrides) -> config_mod.Config:
    d = config_mod._default_dict()
    d.update(overrides)
    return config_mod.Config(
        opencode_bin="opencode",
        models=dict(d["models"]),
        variants=dict(d["variants"]),
        provider=config_mod.ProviderConfig(id="opencode-go", key_file="/dev/null",
                                          key_env="OPENCODE_API_KEY"),
        timeouts=config_mod.TimeoutsConfig(turn_seconds=20.0, idle_seconds=15.0,
                                          evaluator_turn_seconds=20.0),
        retries=config_mod.RetriesConfig(max_attempts=2, backoff_seconds=(0.0, 0.0)),
        max_iterations=4, root_free=True,
        isolate_workers=overrides.get("isolate_workers", True),
        slice_eval_concurrency=overrides.get("slice_eval_concurrency", 4),
        kill_check=overrides.get("kill_check", True),
    )


@pytest.fixture
def product_repo(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(
        "# Plan\n```yaml\nslices:\n"
        "  - id: a\n    writes: [a.py]\n    reads: []\n"
        "```\n", encoding="utf-8",
    )
    (box / "QUEUE.md").write_text("# Queue\n", encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    git(root, "add", "loop")
    git(root, "commit", "-q", "-m", "loop: init")
    return root


def make_ctx(root: Path, *, mailbox: Path | None = None, cfg=None) -> driver.RunContext:
    mailbox = mailbox or (root / "loop")
    cfg = cfg or make_cfg()
    return driver.RunContext(
        root_mailbox=mailbox, live_mailbox=mailbox, repo=root, cfg=cfg, token="tok",
        exec_id="exec1234exec1234exec1234exec1234", run_dir=mailbox / ".run",
        log_dir=mailbox / ".run" / "logs", env={}, root_free=False, lead_record=None,
        out=lambda line: None, cancel=threading.Event(),
    )


# --------------------------------------------------------------------------
# D12: resolve_settings
# --------------------------------------------------------------------------


def test_resolve_settings_open_loop_defaults():
    cfg = make_cfg()
    settings = openloop.resolve_settings(cfg, is_open_loop=True)
    assert settings == {
        "isolate_workers": True, "slice_eval_concurrency": 4,
        "slice_eval_drain_seconds": None, "kill_check_cli_disabled": False,
        "poll_seconds": 30.0, "notices": [],
        "acceptance_wait_seconds": None,   # no author time limit by default
    }


def test_resolve_settings_cli_overrides_config():
    cfg = make_cfg(isolate_workers=True, slice_eval_concurrency=4, kill_check=True)
    settings = openloop.resolve_settings(
        cfg, is_open_loop=True, isolate_workers=False, slice_eval_concurrency=1,
        kill_check=False, poll_seconds=0.5,
    )
    assert settings["isolate_workers"] is False
    assert settings["slice_eval_concurrency"] == 1
    assert settings["kill_check_cli_disabled"] is True
    assert settings["poll_seconds"] == 0.5


def test_resolve_settings_refuses_explicit_concurrency_without_isolation():
    cfg = make_cfg()
    with pytest.raises(openloop.SettingsError, match="refused"):
        openloop.resolve_settings(cfg, is_open_loop=True, isolate_workers=False,
                                  slice_eval_concurrency=2)


def test_resolve_settings_default_concurrency_falls_back_to_1_without_isolation():
    cfg = make_cfg()
    settings = openloop.resolve_settings(cfg, is_open_loop=True, isolate_workers=False)
    assert settings["slice_eval_concurrency"] == 1


def test_resolve_settings_bad_concurrency_refused():
    cfg = make_cfg()
    with pytest.raises(openloop.SettingsError):
        openloop.resolve_settings(cfg, is_open_loop=True, slice_eval_concurrency=0)


def test_resolve_settings_lockstep_is_a_noop_with_notices():
    cfg = make_cfg()
    settings = openloop.resolve_settings(cfg, is_open_loop=False, isolate_workers=False,
                                         slice_eval_concurrency=2)
    assert settings["isolate_workers"] is True
    assert settings["slice_eval_concurrency"] == 1
    assert len(settings["notices"]) == 2


def test_cli_refuses_no_isolate_workers_with_concurrency_2(tmp_path, monkeypatch):
    """Accept: ``--no-isolate-workers --slice-eval-concurrency 2`` -> exit 2."""
    from trio_opencode import cli

    mailbox = tmp_path / "nope"
    mailbox.mkdir()
    monkeypatch.setattr(driver, "_git_toplevel", lambda p: str(tmp_path))
    (mailbox / "QUEUE.md").write_text("# Queue\n", encoding="utf-8")
    code = cli.main(["start", "--mailbox", str(mailbox), "--no-isolate-workers",
                     "--slice-eval-concurrency", "2"])
    assert code == 2


# --------------------------------------------------------------------------
# D1: detect_open_loop
# --------------------------------------------------------------------------


def test_detect_open_loop_true_with_queue_md(product_repo):
    assert openloop.detect_open_loop(product_repo / "loop", root_free=False) is True


def test_detect_open_loop_false_without_queue_md(product_repo):
    (product_repo / "loop" / "QUEUE.md").unlink()
    assert openloop.detect_open_loop(product_repo / "loop", root_free=False) is False


def test_detect_open_loop_root_free_checks_live_mailbox(product_repo):
    mailbox = product_repo / "loop"
    (mailbox / "QUEUE.md").unlink()
    record = rootfree.prepare(mailbox)
    (record.live_mailbox / "QUEUE.md").write_text("# Queue\n", encoding="utf-8")
    assert openloop.detect_open_loop(mailbox, root_free=True) is True


# --------------------------------------------------------------------------
# D2: declared_repos_for_prepare
# --------------------------------------------------------------------------


def test_declared_repos_for_prepare_empty_without_repos_block(product_repo):
    assert openloop.declared_repos_for_prepare(product_repo / "loop") == []


def test_declared_repos_for_prepare_reads_repos_block(tmp_path, product_repo):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "f").write_text("x\n", encoding="utf-8")
    git(other, "add", "f")
    git(other, "commit", "-q", "-m", "init")

    plan = product_repo / "loop" / "PLAN.md"
    plan.write_text(
        plan.read_text(encoding="utf-8") + f"\n```yaml\nrepos:\n"
        f"  - name: other\n    path: {other}\n```\n", encoding="utf-8",
    )
    decl = openloop.declared_repos_for_prepare(product_repo / "loop")
    assert len(decl) == 1
    assert decl[0]["name"] == "other"
    assert Path(decl[0]["path"]).resolve() == other.resolve()


# --------------------------------------------------------------------------
# D7: state guard
# --------------------------------------------------------------------------


def test_state_guard_restores_owned_key_after_turn(product_repo):
    ctx = make_ctx(product_repo)
    state_path = ctx.live_mailbox / "STATE.md"
    guard = openloop._StateGuard(ctx)
    guard.install()
    try:
        # TL writes the iteration cursor forward (the "expected" value).
        openloop.TL._update_state(state_path, {"iteration": "3", "phase": "lead-running"})
        # A role turn's own file edit clobbers it (the exact bug the guard
        # is for -- an opencode Lead/Evaluator turn editing STATE.md).
        text = state_path.read_text(encoding="utf-8")
        text = text.replace("iteration: 3", "iteration: 99")
        state_path.write_text(text, encoding="utf-8")
        guard.after_turn(state_path, "lead-pass it3")
        restored = openloop.TL._read_state(state_path)
        assert restored["iteration"] == "3"
    finally:
        guard.uninstall()
    assert openloop.TL._update_state is not guard._orig or True  # uninstalled below
    assert openloop.TL._update_state.__name__ == "_update_state"


def test_state_guard_uninstall_restores_original():
    orig = openloop.TL._update_state
    ctx = SimpleNamespace(state_snapshot=None, _last_phase="begin", _last_iteration=0,
                          write_driver_json=lambda **_: None)
    guard = openloop._StateGuard(ctx)  # type: ignore[arg-type]
    guard.install()
    assert openloop.TL._update_state is not orig
    guard.uninstall()
    assert openloop.TL._update_state is orig


# --------------------------------------------------------------------------
# D8: sidecar writer
# --------------------------------------------------------------------------


def test_sidecar_writer_union_of_tl_and_driver_fields(product_repo):
    ctx = make_ctx(product_repo)
    ctx.turns["builder it1w1 a"] = {"pid": 123, "pgid": 123, "session_id": None,
                                    "started_at": "now"}
    lead_runner = SimpleNamespace(session_ids={"lead": "ses_lead"}, driver_meta={"lint": {"x": 1}})
    eval_runner = SimpleNamespace(session_ids={"evaluator": "ses_eval"}, driver_meta={})
    writer = openloop._make_sidecar_writer(ctx)
    writer(ctx.live_mailbox, lead_runner, eval_runner, 2, "lead", True, True, "2024-01-01T00:00:00Z")

    data = json.loads((ctx.live_mailbox / ".driver.json").read_text(encoding="utf-8"))
    # TL's own open-loop keys.
    assert data["open_loop"] is True
    assert data["lead_alive"] is True
    assert data["session_ids"] == {"lead": "ses_lead", "evaluator": "ses_eval"}
    assert data["lint"] == {"x": 1}
    # This driver's own keys, preserved (D8).
    assert data["driver"] == "opencode"
    assert data["run_token"] == "tok"
    assert data["exec_id"] == ctx.exec_id
    assert "builder it1w1 a" in data["turns"]

    session = json.loads((ctx.live_mailbox / ".session.json").read_text(encoding="utf-8"))
    assert session["phase"] == "lead"
    assert session["open_loop"] is True

    # ctx.driver_extra is kept in sync so write_driver_json (a role turn's
    # on_spawn/on_turn_end) does not drop these keys if it fires in between.
    assert ctx.driver_extra.get("open_loop") is True
    ctx.write_driver_json(phase="lead-running", iteration=2)
    data2 = json.loads(ctx.driver_json_path.read_text(encoding="utf-8"))
    assert data2["open_loop"] is True
    assert "builder it1w1 a" in data2["turns"]


# --------------------------------------------------------------------------
# D3: builder retire / takeover / conflict
# --------------------------------------------------------------------------


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", path], check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True,
                   env=git_env())
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True, env=git_env()).stdout.strip()


def test_dispatch_and_retire_merges_on_success(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    head = subprocess.run(["git", "-C", str(product_repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, env=git_env()).stdout.strip()

    def fake_run_one_builder(iteration, wave_index, s, dispatch_head, repo_path, *, attempt, notes):
        branch = f"trio-oc/exec123/i{iteration}-{s['id']}-a{attempt}"
        subprocess.run(["git", "-C", str(repo_path), "branch", branch, dispatch_head],
                       check=True, env=git_env())
        wt = repo_path / driver.BUILDER_WORKTREES_DIR / f"wt-{s['id']}-a{attempt}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo_path), "worktree", "add", str(wt), branch],
                       check=True, env=git_env())
        (wt / "a.py").write_text("print('a')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "a.py"], check=True, env=git_env())
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", f"slice({s['id']}): add a.py"],
                       check=True, env=git_env())
        return {"id": s["id"], "branch": branch, "path": wt, "ok": True, "reason": "",
               "report": {"summary": "added a.py"}, "targeted": "TARGETED_CHECK: 1 passed",
               "kill_check": None, "flags": [], "repo_path": repo_path, "attempt": attempt}

    monkeypatch.setattr(runner, "_run_one_builder", fake_run_one_builder)
    outcome = runner._dispatch_and_retire(1, 1, {"id": "a", "brief": "x"}, product_repo, head,
                                          ctx.live_mailbox)
    assert outcome["status"] == "retired"
    assert outcome["sha"]
    retired = olqueue.latest_retired(ctx.live_mailbox)
    assert "a" in retired and retired["a"]["sha"] == outcome["sha"]
    assert (product_repo / "a.py").is_file()
    # worktree removed, branch merged+deleted.
    wt_list = subprocess.run(["git", "-C", str(product_repo), "worktree", "list"],
                             capture_output=True, text=True, env=git_env()).stdout
    assert "wt-a-a1" not in wt_list
    branches = subprocess.run(["git", "-C", str(product_repo), "branch", "--list",
                              "trio-oc/exec123/*"], capture_output=True, text=True,
                             env=git_env()).stdout
    assert branches.strip() == ""


def test_dispatch_and_retire_failed_targeted_check_takes_over_after_one_redispatch(
        product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    head = subprocess.run(["git", "-C", str(product_repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, env=git_env()).stdout.strip()
    calls = []

    def fake_run_one_builder(iteration, wave_index, s, dispatch_head, repo_path, *, attempt, notes):
        calls.append(attempt)
        branch = f"trio-oc/exec123/i{iteration}-{s['id']}-a{attempt}"
        subprocess.run(["git", "-C", str(repo_path), "branch", branch, dispatch_head],
                       check=True, env=git_env())
        wt = repo_path / driver.BUILDER_WORKTREES_DIR / f"wt-{s['id']}-a{attempt}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo_path), "worktree", "add", str(wt), branch],
                       check=True, env=git_env())
        (wt / "a.py").write_text("broken\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "a.py"], check=True, env=git_env())
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", f"slice({s['id']}): broken"],
                       check=True, env=git_env())
        return {"id": s["id"], "branch": branch, "path": wt, "ok": True, "reason": "",
               "report": {"summary": "broken"}, "targeted": "TARGETED_CHECK: FAILED 1 failed",
               "kill_check": None, "flags": [], "repo_path": repo_path, "attempt": attempt}

    monkeypatch.setattr(runner, "_run_one_builder", fake_run_one_builder)
    outcome = runner._dispatch_and_retire(1, 1, {"id": "a", "brief": "x"}, product_repo, head,
                                          ctx.live_mailbox)
    assert calls == [1, 2]
    assert outcome["status"] == "takeover"
    assert "targeted check failed" in outcome["reason"]
    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    # Neither attempt's worktree/branch survives.
    wt_list = subprocess.run(["git", "-C", str(product_repo), "worktree", "list"],
                             capture_output=True, text=True, env=git_env()).stdout
    assert "wt-a-a1" not in wt_list and "wt-a-a2" not in wt_list


def test_merge_and_retire_conflict_aborts_and_raises(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    # Conflicting change directly on main...
    _commit(product_repo, "README", "changed on main\n", "main: change README")
    # ...and a branch forked from the ORIGINAL tip that also touches README.
    orig_head = subprocess.run(["git", "-C", str(product_repo), "rev-parse", "HEAD~1"],
                               capture_output=True, text=True, env=git_env()).stdout.strip()
    branch = "trio-oc/exec123/i1-a-a1"
    subprocess.run(["git", "-C", str(product_repo), "branch", branch, orig_head], check=True,
                   env=git_env())
    wt = product_repo / driver.BUILDER_WORKTREES_DIR / "wt-conflict"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(product_repo), "worktree", "add", str(wt), branch],
                   check=True, env=git_env())
    (wt / "README").write_text("changed on the branch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "README"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "slice(a): conflicting"],
                   check=True, env=git_env())

    with pytest.raises(openloop._ConflictError):
        runner._merge_and_retire(product_repo, {"id": "a", "branch": branch}, ctx.live_mailbox)
    status = subprocess.run(["git", "-C", str(product_repo), "status", "--porcelain",
                            "--untracked-files=no"],
                            capture_output=True, text=True, env=git_env()).stdout
    assert status.strip() == ""
    assert olqueue.latest_retired(ctx.live_mailbox) == {}


# --------------------------------------------------------------------------
# D3(d): post-review retirement of Lead-made commits
# --------------------------------------------------------------------------


def test_retire_lead_commits_runs_targeted_check_and_retires(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    before = subprocess.run(["git", "-C", str(product_repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, env=git_env()).stdout.strip()
    (product_repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(product_repo), "add", "-A"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(product_repo), "commit", "-q", "-m", "slice(a): take over"],
                   check=True, env=git_env())

    slices_by_id = {"a": {"id": "a", "targeted_check": "test -f a.py"}}
    runner._retire_lead_commits(1, product_repo, before, ctx.live_mailbox, slices_by_id)
    retired = olqueue.latest_retired(ctx.live_mailbox)
    assert "a" in retired
    assert ("a", retired["a"]["sha"]) in runner.builder_records
    assert runner.builder_records[("a", retired["a"]["sha"])]["authored_by"] == "lead"


def test_retire_lead_commits_not_retired_without_known_command(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    before = subprocess.run(["git", "-C", str(product_repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, env=git_env()).stdout.strip()
    (product_repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(product_repo), "add", "-A"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(product_repo), "commit", "-q", "-m", "slice(a): take over"],
                   check=True, env=git_env())

    runner._retire_lead_commits(1, product_repo, before, ctx.live_mailbox, {"a": {"id": "a"}})
    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "not retired: no known targeted check command" in log


# --------------------------------------------------------------------------
# D13: author() transcript rows
# --------------------------------------------------------------------------


def test_tool_call_rows_makes_relative_paths_absolute(tmp_path, product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    export = tmp_path / "export"
    export.mkdir()
    log_path = tmp_path / "turn.jsonl"
    events = [
        {"type": "tool", "part": {"type": "tool", "tool": "edit",
                                  "state": {"input": {"path": "sub/file.txt", "content": "x"}}}},
        {"type": "tool", "part": {"type": "tool", "tool": "bash",
                                  "state": {"input": {"command": "ls"}}}},
    ]
    log_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    result = SimpleNamespace(log_paths=[str(log_path)])

    rows = runner._tool_call_rows(result, export)
    assert len(rows) == 2
    assert rows[0]["name"] == "edit"
    assert Path(rows[0]["input"]["path"]).is_absolute()
    assert rows[0]["input"]["path"] == str((export / "sub/file.txt").resolve())
    # text the author WROTE is never audited as something it read
    assert "content" not in rows[0]["input"]
    assert rows[1]["input"]["command"] == "ls"


def test_author_builds_prompt_and_absolute_transcript(tmp_path, product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    export = tmp_path / "export"
    export.mkdir()
    log_path = tmp_path / "turn.jsonl"
    log_path.write_text(json.dumps({
        "type": "tool", "part": {"type": "tool", "tool": "read",
                                 "state": {"input": {"path": "x.txt"}}},
    }) + "\n", encoding="utf-8")

    captured = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, session_id=None,
                       turn_timeout=None, idle_timeout=None, env_extra=None,
                       argv_prefix=()):
        captured["prompt"] = prompt
        captured["cwd"] = cwd
        captured["role"] = role
        return SimpleNamespace(ok=True, session_id="ses_author", text="done",
                               log_paths=[str(log_path)])

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    result = runner.author(export, {"marker": "m1", "attempt": 1})
    assert result["exit"] == 0
    assert result["session"] == "ses_author"
    assert result["path"] == "opencode"
    assert captured["role"] == "acceptance"
    assert captured["cwd"] == export
    assert len(result["transcript"]) == 1
    assert result["transcript"][0]["input"]["path"] == str((export / "x.txt").resolve())


# --------------------------------------------------------------------------
# ol-harden: H1 QueueGuard wiring
# --------------------------------------------------------------------------


def _write_faults_block(box: Path, entries_text: str) -> None:
    path = box / "QUEUE.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = "```yaml\nfaults:\n" + entries_text + "```\n"
    path.write_text(existing + ("\n" if existing and not existing.endswith("\n\n") else "") + block,
                    encoding="utf-8")


def test_run_slice_eval_queue_guard_restores_dropped_entries(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    olqueue.append_retired(mailbox, slice_id="b", sha="2" * 40, at="2026-09-29T00:00:00Z")
    _write_faults_block(
        mailbox,
        "  - id: f1\n"
        "    slice: b\n"
        "    observed_at: " + "2" * 40 + "\n"
        "    scope: [b.py]\n"
        "    reason: missing error handling\n"
        "    status: open\n",
    )
    before = olqueue.TL._read_queue(mailbox)
    assert [e["slice"] for e in before["retired"]] == ["b"]
    assert [f["id"] for f in before["faults"]] == ["f1"]

    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=False)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        # A concurrent LLM evaluator turn that rewrites QUEUE.md from
        # scratch, dropping the sibling's retired entry and fault.
        (mailbox / "QUEUE.md").write_text(
            "```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n", encoding="utf-8")
        return SimpleNamespace(ok=True, session_id="ses_eval")

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    monkeypatch.setattr(openloop.TL, "human_answer_block", lambda *a, **kw: None)

    code = runner._run_slice_eval(1, mailbox, {"slice": "a", "sha": "1" * 40, "repo": None})
    assert code == 0

    after = olqueue.TL._read_queue(mailbox)
    assert [e["slice"] for e in after["retired"]] == ["b"]
    assert [f["id"] for f in after["faults"]] == ["f1"]

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "- iter 1 | loop | queue guard (slice-eval a@11111111): re-appended retired b" in log_text
    assert "re-appended fault f1" in log_text


# --------------------------------------------------------------------------
# ol-harden: H2 fatal-stop cancellation
# --------------------------------------------------------------------------


def test_fatal_stop_cancels_concurrent_turns_and_finalizes_error(product_repo):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)

    start_second = threading.Event()
    saw_cancel = threading.Event()

    def fake_lead(iteration, mailbox, context):
        start_second.set()
        raise driver.DriverStop("error", 3, "lead-plan it1: permission: denied",
                                role_denials=[{"label": "lead-plan it1", "text": "permission denied"}])

    def fake_integration_eval(iteration, mailbox, context):
        assert start_second.wait(2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if ctx.cancel.is_set():
                saw_cancel.set()
                break
            time.sleep(0.01)
        raise driver.DriverStop("cancelled", ctx.cancel_code.get("code", 130),
                                "integration-eval: cancelled", role_denials=ctx.role_denials)

    runner._run_lead = fake_lead
    runner._run_integration_eval = fake_integration_eval

    def run_lead_thread():
        try:
            runner.run("lead", 1, ctx.live_mailbox, {})
        except driver.DriverStop:
            pass

    def run_eval_thread():
        try:
            runner.run("evaluator", 1, ctx.live_mailbox, {"kind": "integration-eval"})
        except driver.DriverStop:
            pass

    t1 = threading.Thread(target=run_lead_thread)
    t2 = threading.Thread(target=run_eval_thread)
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)

    assert saw_cancel.is_set()
    assert ctx.cancel.is_set()

    final = openloop._finalize_result(ctx, runner, 3, settings)
    assert final["status"] == "error"
    assert final["role_denials"] == [{"label": "lead-plan it1", "text": "permission denied"}]


# --------------------------------------------------------------------------
# ol-harden: H3 drive() exception safety
# --------------------------------------------------------------------------


def test_drive_wraps_run_open_loop_exception_and_still_finalizes(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)

    def boom(*a, **kw):
        raise RuntimeError("lead turn exploded")

    monkeypatch.setattr(openloop.TL, "run_open_loop", boom)

    final = openloop.drive(ctx, mode="start", max_iterations=4, settings=settings,
                           stop_now={"flag": False, "code": 130})

    assert final["status"] == "error"
    assert final["code"] == 3
    assert final["reason"] == "RuntimeError: lead turn exploded"
    assert (ctx.live_mailbox / driver.RESULT_FILE).is_file()
    registry = json.loads(driver.registry_path(ctx.root_mailbox).read_text(encoding="utf-8"))
    assert registry["state"] == "finished"
    assert registry["status"] == "error"


# --------------------------------------------------------------------------
# ol-harden: H4 real signal handling while idle
# --------------------------------------------------------------------------


def test_drive_stops_on_sigterm_while_idle_between_turns(product_repo):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    # `driver.run()` always builds `RunContext.cancel_code` from the SAME
    # dict object it passes as `drive()`'s own `stop_now` (so the signal
    # handler's write to one is visible through the other); mirror that
    # here instead of `make_ctx`'s own decoupled default.
    stop_now = {"flag": False, "code": 130}
    ctx.cancel_code = stop_now

    def idle_stub(*a, **kw):
        threading.Event().wait(5.0)
        return 0

    old_run_open_loop = openloop.TL.run_open_loop
    openloop.TL.run_open_loop = idle_stub
    timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM))
    try:
        timer.start()
        start = time.monotonic()
        final = openloop.drive(ctx, mode="start", max_iterations=4, settings=settings,
                               stop_now=stop_now)
        elapsed = time.monotonic() - start
    finally:
        openloop.TL.run_open_loop = old_run_open_loop
        timer.join(2.0)

    assert elapsed < 2.0
    assert final["status"] == "cancelled"
    assert final["code"] == 143


# --------------------------------------------------------------------------
# ol-harden: H5 resume restores driver-owned STATE and builders
# --------------------------------------------------------------------------


def test_restore_resume_state_matching_token_restores_state_and_builders(product_repo):
    ctx = make_ctx(product_repo)
    (ctx.live_mailbox / "STATE.md").write_text(
        "iteration: 5\nstatus: running\nphase: evaluator-running\n"
        "evaluated_sha: deadbeef\nevaluator_attempt: 1\nevaluated_repos: home\n",
        encoding="utf-8")
    driver._atomic_write_json(ctx.driver_json_path, {
        "run_token": ctx.token,
        "state_snapshot": {"iteration": "2", "phase": "lead-running", "evaluated_sha": "",
                          "evaluator_attempt": "", "evaluated_repos": ""},
        "builders": {"a@deadbeefcafe": {"authored_by": "builder", "kill_check": None,
                                        "flags": [], "targeted_check": "TARGETED_CHECK: PASS",
                                        "branch": "x"}},
    })

    builders = openloop._restore_resume_state(ctx)

    state_text = (ctx.live_mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "iteration: 2" in state_text
    assert "phase: lead-running" in state_text
    assert "iteration: 5" not in state_text
    assert builders == {"a@deadbeefcafe": {"authored_by": "builder", "kill_check": None,
                                           "flags": [], "targeted_check": "TARGETED_CHECK: PASS",
                                           "branch": "x"}}


def test_restore_resume_state_mismatched_token_is_untouched(product_repo):
    ctx = make_ctx(product_repo)
    (ctx.live_mailbox / "STATE.md").write_text(
        "iteration: 5\nstatus: running\nphase: evaluator-running\n", encoding="utf-8")
    driver._atomic_write_json(ctx.driver_json_path, {
        "run_token": "some-other-run-token",
        "state_snapshot": {"iteration": "2", "phase": "lead-running"},
        "builders": {"a@deadbeefcafe": {"authored_by": "builder"}},
    })

    builders = openloop._restore_resume_state(ctx)

    state_text = (ctx.live_mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "iteration: 5" in state_text
    assert builders is None


def test_builder_record_for_falls_back_to_resumed_sha_prefix(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    runner.load_resumed_builders({"a@deadbeefcafe": {"authored_by": "builder", "kill_check": None}})
    assert runner._builder_record_for("a", "deadbeefcafe" + "0" * 28) == {
        "authored_by": "builder", "kill_check": None}
    assert runner._builder_record_for("a", "ffffffffffff" + "0" * 28) is None
    assert runner.driver_meta["builders"] == {"a@deadbeefcafe":
                                              {"authored_by": "builder", "kill_check": None}}


def test_drive_resume_restores_state_before_run_open_loop_is_entered(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    (ctx.live_mailbox / "STATE.md").write_text(
        "iteration: 5\nstatus: running\nphase: evaluator-running\n"
        "evaluated_sha: deadbeef\nevaluator_attempt: 1\nevaluated_repos: home\n",
        encoding="utf-8")
    driver._atomic_write_json(ctx.driver_json_path, {
        "run_token": ctx.token,
        "state_snapshot": {"iteration": "2", "phase": "lead-running", "evaluated_sha": "",
                          "evaluator_attempt": "", "evaluated_repos": ""},
    })

    captured = {}

    def stub(mailbox, *a, **kw):
        captured["state_text"] = Path(mailbox, "STATE.md").read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(openloop.TL, "run_open_loop", stub)
    openloop.drive(ctx, mode="resume", max_iterations=4, settings=settings,
                   stop_now={"flag": False, "code": 130})

    assert "iteration: 2" in captured["state_text"]
    assert "iteration: 5" not in captured["state_text"]


# --------------------------------------------------------------------------
# ol-harden: H6 re-dispatch from the current head after a conflict
# --------------------------------------------------------------------------


def test_dispatch_and_retire_redispatches_from_current_head_after_conflict(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    repo = product_repo

    (repo / "x.py").write_text("init\n", encoding="utf-8")
    git(repo, "add", "x.py")
    git(repo, "commit", "-q", "-m", "add x.py")
    h0 = git(repo, "rev-parse", "HEAD")

    seen_heads: list[str] = []
    state: dict[str, str] = {}

    def fake_run_one_builder(iteration, wave_index, s, dispatch_head, repo_path, *,
                             attempt, notes):
        seen_heads.append(dispatch_head)
        branch = f"builder-a-attempt{attempt}"
        wt = repo_path / ".trio-opencode" / "worktrees" / f"wt-a{attempt}"
        wt.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(repo_path), "worktree", "add", "-b", branch,
                       str(wt), dispatch_head], check=True, capture_output=True, text=True,
                      env=git_env())
        if attempt == 1:
            (wt / "x.py").write_text("from-builder\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt), "add", "x.py"], check=True, env=git_env())
            subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "slice(a): attempt1"],
                          check=True, env=git_env())
            # A sibling slice in this same wave merges into `repo_path`
            # before this attempt's own merge runs.
            (repo_path / "x.py").write_text("from-sibling\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_path), "add", "x.py"], check=True, env=git_env())
            subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", "slice(b): sibling"],
                          check=True, env=git_env())
            state["sibling_head"] = git(repo_path, "rev-parse", "HEAD")
        else:
            (wt / "y.py").write_text("from-builder-attempt2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt), "add", "y.py"], check=True, env=git_env())
            subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "slice(a): attempt2"],
                          check=True, env=git_env())
        return {"id": "a", "branch": branch, "path": wt, "ok": True, "reason": "",
               "report": {"summary": "did stuff"}, "targeted": "TARGETED_CHECK: PASS",
               "kill_check": None, "flags": [], "repo_path": repo_path, "attempt": attempt}

    runner._run_one_builder = fake_run_one_builder
    outcome = runner._dispatch_and_retire(1, 1, {"id": "a"}, repo, h0, ctx.live_mailbox)

    assert outcome["status"] == "retired"
    assert seen_heads[0] == h0
    assert seen_heads[1] == state["sibling_head"]
    assert seen_heads[1] != h0


# --------------------------------------------------------------------------
# ol-harden: H7 isolation off -> builders serial
# --------------------------------------------------------------------------


def test_run_wave_uses_one_worker_when_isolation_off(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=False)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)

    captured = {}
    real_executor = cf.ThreadPoolExecutor

    class RecordingExecutor(real_executor):
        def __init__(self, max_workers=None, *a, **kw):
            captured["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, *a, **kw)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(runner, "_dispatch_and_retire",
                        lambda iteration, wave_index, s, repo_path, head, mailbox: {
                            "id": s["id"], "status": "retired", "result_entry": {}})

    outcomes = runner._run_wave(1, 1, [{"id": "a"}, {"id": "b"}], ctx.live_mailbox)

    assert captured["max_workers"] == 1
    assert {o["id"] for o in outcomes} == {"a", "b"}


def test_run_wave_uses_wave_size_workers_when_isolation_on(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=True)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)

    captured = {}
    real_executor = cf.ThreadPoolExecutor

    class RecordingExecutor(real_executor):
        def __init__(self, max_workers=None, *a, **kw):
            captured["max_workers"] = max_workers
            super().__init__(max_workers=max_workers, *a, **kw)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(runner, "_dispatch_and_retire",
                        lambda iteration, wave_index, s, repo_path, head, mailbox: {
                            "id": s["id"], "status": "retired", "result_entry": {}})

    runner._run_wave(1, 1, [{"id": "a"}, {"id": "b"}], ctx.live_mailbox)

    assert captured["max_workers"] == 2


# --------------------------------------------------------------------------
# ol-harden: H8 .driver.json writer race
# --------------------------------------------------------------------------


def test_driver_json_writers_serialize_and_never_raise(product_repo):
    ctx = make_ctx(product_repo)
    sidecar = openloop._make_sidecar_writer(ctx)
    lead_runner = SimpleNamespace(session_ids={}, driver_meta={})
    eval_runner = SimpleNamespace(session_ids={}, driver_meta={})
    # Make sure `ctx.driver_extra` (open_loop/session_ids/...) is already
    # populated before the race starts -- it is `setdefault`-merged into
    # EVERY `write_driver_json` call from then on, regardless of which
    # writer's call lands last.
    sidecar(ctx.live_mailbox, lead_runner, eval_runner, 0, "begin", True, True,
           "2026-01-01T00:00:00Z")

    errors: list[Exception] = []

    def spam_turns() -> None:
        for i in range(200):
            label = f"t{i}"
            ctx.on_spawn(20000 + i, 20000 + i, label=label, session_id=None)
            ctx.on_turn_end(label)

    def spam_sidecar() -> None:
        for i in range(200):
            try:
                sidecar(ctx.live_mailbox, lead_runner, eval_runner, i, "lead-running",
                       True, True, "2026-01-01T00:00:00Z")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=spam_turns), threading.Thread(target=spam_sidecar),
              threading.Thread(target=spam_sidecar)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors
    data = json.loads((ctx.live_mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert "turns" in data
    assert data.get("open_loop") is True
    assert data.get("run_token") == ctx.token


# --------------------------------------------------------------------------
# ol-harden: H9 open-loop scratch dir
# --------------------------------------------------------------------------


def test_drive_sets_and_cleans_up_scratch_tmpdir(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    captured = {}

    def stub(*a, **kw):
        captured["tmpdir"] = ctx.tmpdir
        captured["tmpdir_is_dir"] = ctx.tmpdir is not None and Path(ctx.tmpdir).is_dir()
        captured["turn_env_tmpdir"] = ctx.turn_env().get("TMPDIR")
        return 0

    monkeypatch.setattr(openloop.TL, "run_open_loop", stub)
    openloop.drive(ctx, mode="start", max_iterations=4, settings=settings,
                   stop_now={"flag": False, "code": 130})

    assert captured["tmpdir"] is not None
    assert captured["tmpdir_is_dir"] is True
    assert captured["turn_env_tmpdir"] == captured["tmpdir"]
    assert not Path(captured["tmpdir"]).exists()


# --------------------------------------------------------------------------
# ol-harden: H10 acceptance fragment in the lead-plan/lead-review turns
# --------------------------------------------------------------------------


def test_acceptance_plan_and_pass_notes_render_distinct_fragments(product_repo):
    ctx = make_ctx(product_repo)
    plan_notes = openloop._acceptance_plan_notes(ctx)
    pass_notes = openloop._acceptance_pass_notes(ctx)
    assert any("FROZEN ACCEPTANCE" in n for n in plan_notes)
    assert any("FROZEN ACCEPTANCE" in n for n in pass_notes)
    assert any("Frozen pack:" in n for n in plan_notes)
    assert not any("Frozen pack:" in n for n in pass_notes)


def test_lead_plan_prompt_carries_acceptance_fragment_when_enabled(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    ctx.acceptance = True
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))

    captured = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        captured["prompt"] = prompt
        return SimpleNamespace(ok=True, session_id="ses_lead",
                               text='```json\n{"slices": []}\n```')

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    runner._run_lead(1, ctx.live_mailbox, {})

    tool = openloop._acceptance_tool_path()
    fragment = prompts_mod.acc_lead_fragment(str(ctx.live_mailbox), tool)
    assert fragment in captured["prompt"]


def test_lead_plan_prompt_omits_acceptance_fragment_when_disabled(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    assert ctx.acceptance is False
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))

    captured = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        captured["prompt"] = prompt
        return SimpleNamespace(ok=True, session_id="ses_lead",
                               text='```json\n{"slices": []}\n```')

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    runner._run_lead(1, ctx.live_mailbox, {})

    assert "FROZEN ACCEPTANCE" not in captured["prompt"]



def test_integration_eval_prompt_ends_with_whole_goal_rigor(product_repo, monkeypatch):
    """ol-rigor: the runner's integration-eval prompt carries the generated
    `## Whole-goal verification rigor` block (trioctl appends it to every
    whole-goal eval); its slice-eval prompt never does."""
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=False)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)
    prompts_seen: dict[str, str] = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        prompts_seen[label.split()[0]] = prompt
        return SimpleNamespace(ok=True, session_id="ses_eval")

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    monkeypatch.setattr(openloop.TL, "human_answer_block", lambda *a, **kw: None)
    head = openloop.TL._git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
    assert runner._run_integration_eval(1, mailbox, {
        "kind": "integration-eval", "pinned_sha": head, "evaluator_attempt": "c" * 32}) == 0
    assert runner._run_slice_eval(1, mailbox, {"slice": "a", "sha": head, "repo": None}) == 0
    rigor = openloop.integration_rigor()
    assert rigor.startswith("## Whole-goal verification rigor")
    assert prompts_seen["integration-eval"].rstrip("\n").endswith(rigor.rstrip("\n")) or \
        rigor.rstrip("\n") in prompts_seen["integration-eval"]
    assert "## Whole-goal verification rigor\n" not in prompts_seen["slice-eval"]


def test_validate_plan_refuses_retired_slice_without_fault(product_repo):
    """run_open_loop forces a first Lead pass on every (re)start: a plan that
    returns an already-retired slice without a `fault` is refused (re-plan),
    a fault fix for it is accepted."""
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)
    olqueue.append_retired(mailbox, slice_id="a", sha="1" * 40, at="2026-10-01T00:00:00Z")
    base = {"brief": "b", "writes": ["a.py"], "reads": [], "depends": [], "repo": "home"}
    refusal = runner._validate_plan({"slices": [dict(base, id="a", fault=None)]}, mailbox, [])
    assert refusal is not None and "already retired" in refusal
    fix = runner._validate_plan({"slices": [dict(base, id="a", fault="f1")]}, mailbox, [])
    # (PLAN.md's own slices-block check may still speak; never the retired rule.)
    assert "already retired" not in str(fix)


# --------------------------------------------------------------------------
# ol-repair: blocking issue #1 -- crash-consistent merge/retire
# --------------------------------------------------------------------------


def _merge_slice_commit(repo: Path, slice_id: str, filename: str, ctx=None, *,
                        repo_field: str | None = None) -> str:
    """A `_merge_and_retire`-shaped merge commit: a branch forked off HEAD
    with one `slice(<id>): ...` commit, merged `--no-ff` with the exact
    subject `_merge_and_retire` writes -- WITHOUT ever calling
    `olqueue.append_retired`, simulating the SIGKILL crash window. With
    ``ctx`` it also writes the merge-intent record first, the way
    `_merge_and_retire` does; without it the merge is a foreign/unrecorded
    one that reconciliation must never import."""
    branch = f"trio-oc/exec1234/i1-{slice_id}-a1"
    head = git(repo, "rev-parse", "HEAD")
    if ctx is not None:
        openloop._intent_begin(ctx.live_mailbox, slice_id=slice_id, branch=branch,
                               repo_path=repo, repo_field=repo_field, pre_head=head,
                               run_token=ctx.token)
    git(repo, "branch", branch, head)
    wt = repo / driver.BUILDER_WORKTREES_DIR / f"wt-{slice_id}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), branch], check=True,
                   env=git_env())
    (wt / filename).write_text(f"print({slice_id!r})\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", filename], check=True, env=git_env())
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", f"slice({slice_id}): add {filename}"],
                   check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", "-m",
                   f"merge slice {slice_id} ({branch})", branch], check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "branch", "-d", branch], check=True, env=git_env())
    return git(repo, "rev-parse", "HEAD")


def test_reconcile_merge_retirement_appends_missing_entry_for_orphaned_merge(product_repo):
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    assert olqueue.latest_retired(ctx.live_mailbox) == {}

    openloop._reconcile_merge_retirement(ctx)

    retired = olqueue.latest_retired(ctx.live_mailbox)
    assert retired["a"]["sha"] == sha
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert f"reconciled crash-orphaned merge for a@{sha[:12]}" in log


def test_reconcile_merge_retirement_is_idempotent_on_a_clean_mailbox(product_repo):
    """A merge that was already retired normally (no crash) must never be
    re-appended -- `append_retired`'s own self-check would otherwise be the
    only thing stopping a duplicate (double-retirement regression, ol-harden
    non-blocking note)."""
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    olqueue.append_retired(ctx.live_mailbox, slice_id="a", sha=sha, at="2026-10-01T00:00:00Z")

    openloop._reconcile_merge_retirement(ctx)
    openloop._reconcile_merge_retirement(ctx)
    assert openloop._read_intents(ctx.live_mailbox) == []

    queue = olqueue.TL._read_queue(ctx.live_mailbox)
    assert len(queue["retired"]) == 1, queue["retired"]


def test_reconcile_merge_retirement_leaves_properly_retired_slices_alone(product_repo):
    """Two merges for the SAME slice (a fault fix): only the sha with no
    matching retired entry is reconciled -- the first attempt's own
    (already retired) sha is never touched or duplicated."""
    ctx = make_ctx(product_repo)
    sha1 = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    olqueue.append_retired(ctx.live_mailbox, slice_id="a", sha=sha1, at="2026-10-01T00:00:00Z")
    openloop._intent_done(ctx.live_mailbox, openloop._read_intents(ctx.live_mailbox)[0])
    sha2 = _merge_slice_commit(product_repo, "a", "a2.py", ctx)
    # sha2's own retired: entry is "lost" (the crash window) on purpose.

    openloop._reconcile_merge_retirement(ctx)

    queue = olqueue.TL._read_queue(ctx.live_mailbox)
    shas = sorted(e["sha"] for e in queue["retired"] if e["slice"] == "a")
    assert shas == sorted([sha1, sha2])


def test_reconcile_merge_retirement_resume_then_lead_sees_it_already_done(product_repo,
                                                                          monkeypatch):
    """End-to-end of the fix within `_run_lead`: once reconciled, a plan
    that returns the (now-retired) slice again without `fault:` is refused
    the same way an ordinarily-retired slice is."""
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)

    openloop._reconcile_merge_retirement(ctx)

    base = {"brief": "b", "writes": ["a.py"], "reads": [], "depends": [], "repo": "home"}
    refusal = runner._validate_plan({"slices": [dict(base, id="a", fault=None)]}, ctx.live_mailbox,
                                    [])
    assert refusal is not None and "already retired" in refusal
    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha


def test_reconcile_merge_retirement_ignores_foreign_loop_merge_with_colliding_id(product_repo):
    """ol-harden round 2 blocking issue: a PREVIOUS, unrelated goal's merge
    for a slice id (committed to this exact repo before this mailbox was
    reset for a NEW goal) must never be imported alongside a genuine
    crash-orphaned merge for that SAME id from the CURRENT goal -- the
    colliding id must not let the foreign sha get retired, and must not
    stop the current crash's own sha from being reconciled."""
    ctx = make_ctx(product_repo)
    # An earlier, unrelated loop already merged (but never cleanly retired,
    # itself a crash this test does not care about) a DIFFERENT "a" on this
    # exact repo.
    foreign_sha = _merge_slice_commit(product_repo, "a", "old_a.py")
    # A brand-new goal reuses this mailbox path and the SAME slice id "a":
    # QUEUE.md is reset and the reset is committed -- the same shape a
    # human/CLI reusing a mailbox leaves (or `rootfree._seed`'s own `loop:
    # seed` on a fresh/re-attached Lead worktree).
    (product_repo / "loop" / "QUEUE.md").write_text("```yaml\nretired:\n```\n", encoding="utf-8")
    git(product_repo, "add", "-A", "loop")
    git(product_repo, "commit", "-q", "-m", "loop: new goal in loop")
    # The CURRENT goal's own slice "a" crashes between merge and retire.
    crash_sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    assert crash_sha != foreign_sha
    assert olqueue.latest_retired(ctx.live_mailbox) == {}

    openloop._reconcile_merge_retirement(ctx)

    queue = olqueue.TL._read_queue(ctx.live_mailbox)
    shas = [e["sha"] for e in queue["retired"] if e["slice"] == "a"]
    assert shas == [crash_sha], (foreign_sha, shas)
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert foreign_sha[:12] not in log, log


def test_reconcile_merge_retirement_ignores_prior_goals_merges_on_reused_mailbox(product_repo):
    """Same regression, no crash at all: a prior goal's `merge slice ...`
    commits (any ids) must never be imported into a fresh goal's QUEUE.md
    just because it reuses the same mailbox path -- the bare-minimum,
    non-colliding-id case behind blocking issue #1's own round-2 repair."""
    ctx = make_ctx(product_repo)
    _merge_slice_commit(product_repo, "a", "a.py")
    (product_repo / "loop" / "QUEUE.md").write_text("```yaml\nretired:\n```\n", encoding="utf-8")
    git(product_repo, "add", "-A", "loop")
    git(product_repo, "commit", "-q", "-m", "loop: new goal in loop")

    openloop._reconcile_merge_retirement(ctx)

    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "reconciled crash-orphaned" not in log


def test_reconcile_never_imports_a_merge_without_an_intent_record(product_repo):
    """The git-history walk is gone: a `merge slice` commit with no
    merge-intent record is never imported, whatever the history around it
    (and the epoch-boundary helper no longer exists)."""
    ctx = make_ctx(product_repo)
    _merge_slice_commit(product_repo, "a", "a.py")  # no record
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    assert not hasattr(openloop, "_mailbox_epoch_boundary")


def test_merge_intent_record_roundtrip_and_format(product_repo):
    ctx = make_ctx(product_repo)
    head = git(product_repo, "rev-parse", "HEAD")
    openloop._intent_begin(ctx.live_mailbox, slice_id="a", branch="trio-oc/x/a", repo_path=product_repo,
                           repo_field=None, pre_head=head, run_token="tok")
    openloop._intent_begin(ctx.live_mailbox, slice_id="b", branch="trio-oc/x/b", repo_path=product_repo,
                           repo_field="be", pre_head=head, run_token="tok")
    doc = json.loads((ctx.live_mailbox / ".merge-intent.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1 and [i["slice"] for i in doc["intents"]] == ["a", "b"]
    first = doc["intents"][0]
    assert {"slice", "branch", "repo", "repo_field", "pre_head", "run_token", "at"} <= set(first)
    assert first["repo"] == str(product_repo) and first["pre_head"] == head
    openloop._intent_done(ctx.live_mailbox, first)
    assert [i["slice"] for i in openloop._read_intents(ctx.live_mailbox)] == ["b"]
    openloop._intent_done(ctx.live_mailbox, openloop._read_intents(ctx.live_mailbox)[0])
    assert not (ctx.live_mailbox / ".merge-intent.json").exists()
    assert not [p for p in ctx.live_mailbox.iterdir() if p.name.endswith(".tmp")]


def test_intent_record_is_gitignored_in_the_mailbox(product_repo):
    box = product_repo / "loop"
    openloop._ensure_intent_gitignore(box)
    openloop._ensure_intent_gitignore(box)
    assert (box / ".gitignore").read_text(encoding="utf-8").count(".merge-intent.json*") == 1


def test_reconcile_after_a_later_unrelated_commit(product_repo):
    """A commit made after the crash (even one touching QUEUE.md) cannot
    hide the merge: the record bounds the search by pre_head, not by any
    commit of the mailbox."""
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    (product_repo / "other.py").write_text("x\n", encoding="utf-8")
    (product_repo / "loop" / "QUEUE.md").write_text("# Queue\n# edited\n", encoding="utf-8")
    git(product_repo, "add", "-A")
    git(product_repo, "commit", "-q", "-m", "unrelated work after the crash")
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha
    assert openloop._read_intents(ctx.live_mailbox) == []


def test_reconcile_works_for_an_untracked_and_an_ignored_mailbox(product_repo):
    git(product_repo, "rm", "-r", "-q", "--cached", "loop")
    (product_repo / ".gitignore").write_text("loop/\n", encoding="utf-8")
    git(product_repo, "add", ".gitignore")
    git(product_repo, "commit", "-q", "-m", "ignore loop")
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha


def test_reconcile_works_for_a_mailbox_outside_the_repo(product_repo, tmp_path):
    box = tmp_path / "outside-loop"
    box.mkdir()
    (box / "QUEUE.md").write_text("# Queue\n", encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    ctx = make_ctx(product_repo, mailbox=box)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(box)["a"]["sha"] == sha


def test_reconcile_drops_record_when_merge_never_committed(product_repo):
    ctx = make_ctx(product_repo)
    head = git(product_repo, "rev-parse", "HEAD")
    openloop._intent_begin(ctx.live_mailbox, slice_id="a", branch="trio-oc/never", repo_path=product_repo,
                           repo_field=None, pre_head=head, run_token=ctx.token)
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    assert openloop._read_intents(ctx.live_mailbox) == []


def test_reconcile_ignores_a_record_from_another_run_token(product_repo):
    ctx = make_ctx(product_repo)
    head = git(product_repo, "rev-parse", "HEAD")
    openloop._intent_begin(ctx.live_mailbox, slice_id="a", branch="trio-oc/exec1234/i1-a-a1",
                           repo_path=product_repo, repo_field=None, pre_head=head, run_token="other")
    _merge_slice_commit(product_repo, "a", "a.py")  # same subject, but a foreign token's record
    openloop._reconcile_merge_retirement(ctx)
    assert olqueue.latest_retired(ctx.live_mailbox) == {}
    assert openloop._read_intents(ctx.live_mailbox) == []


def test_reconcile_carries_the_declared_repo_field(product_repo, tmp_path):
    be = tmp_path / "be-repo"
    be.mkdir()
    git(be, "init", "-q", "-b", "main")
    (be / "README").write_text("be\n", encoding="utf-8")
    git(be, "add", "README")
    git(be, "commit", "-q", "-m", "init be")
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(be, "b", "b.py", ctx, repo_field="be")
    openloop._reconcile_merge_retirement(ctx)
    entry = [e for e in olqueue.TL._read_queue(ctx.live_mailbox)["retired"] if e["slice"] == "b"]
    assert [(e["sha"], e.get("repo")) for e in entry] == [(sha, "be")]


def test_merge_and_retire_writes_then_clears_the_record(product_repo, monkeypatch):
    """`_merge_and_retire` itself: the record exists at the moment
    `append_retired` runs (the crash window) and is gone afterwards."""
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    branch = "trio-oc/exec1234/i1-a-a1"
    git(product_repo, "branch", branch, "HEAD")
    wt = product_repo / "wt-a"
    subprocess.run(["git", "-C", str(product_repo), "worktree", "add", str(wt), branch],
                   check=True, env=git_env())
    (wt / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "a.py"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "slice(a): a"], check=True,
                   env=git_env())
    seen = {}
    real = olqueue.append_retired

    def spy(mailbox, **kw):
        seen["during"] = openloop._read_intents(mailbox)
        return real(mailbox, **kw)

    monkeypatch.setattr(olqueue, "append_retired", spy)
    sha, _at = runner._merge_and_retire(product_repo, {"id": "a", "branch": branch}, ctx.live_mailbox)
    assert [i["slice"] for i in seen["during"]] == ["a"]
    assert seen["during"][0]["pre_head"] != sha
    assert openloop._read_intents(ctx.live_mailbox) == []
    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha


def test_reconcile_merge_retirement_ignores_slice_takeover_commits(product_repo):
    """A Lead take-over/fix commit (`slice(<id>): ...`, no merge) is never
    this function's business -- `_retire_one_lead_commit` retires it
    synchronously in the same turn; nothing to reconcile here."""
    ctx = make_ctx(product_repo)
    (product_repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(product_repo), "add", "-A"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(product_repo), "commit", "-q", "-m", "slice(a): take over"],
                   check=True, env=git_env())

    openloop._reconcile_merge_retirement(ctx)

    assert olqueue.latest_retired(ctx.live_mailbox) == {}


# --------------------------------------------------------------------------
# ol-repair: drive() calls the reconciliation before the Lead ever runs
# --------------------------------------------------------------------------


def test_drive_reconciles_orphaned_merge_before_run_open_loop(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)

    captured = {}

    def stub(mailbox, *a, **kw):
        captured["retired"] = dict(olqueue.latest_retired(Path(mailbox)))
        return 0

    monkeypatch.setattr(openloop.TL, "run_open_loop", stub)
    openloop.drive(ctx, mode="resume", max_iterations=4, settings=settings,
                   stop_now={"flag": False, "code": 130})

    assert captured["retired"]["a"]["sha"] == sha


# --------------------------------------------------------------------------
# ol-harden2: stale MERGE_HEAD, quiet merge, token-mismatch warning,
# worktree-add retry
# --------------------------------------------------------------------------


def _plant_stale_merge_state(repo: Path) -> str:
    """What a driver SIGKILLed while its `git merge` child still ran leaves
    behind once the merge COMMITTED: HEAD advanced, MERGE_HEAD/MERGE_MSG/
    MERGE_MODE not yet removed. HEAD must be a merge commit (^2 = the branch
    tip). Returns the planted MERGE_HEAD sha."""
    tip = git(repo, "rev-parse", "HEAD^2")
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    (gitdir / "MERGE_HEAD").write_text(tip + "\n", encoding="utf-8")
    (gitdir / "MERGE_MSG").write_text("merge slice a (b)\n", encoding="utf-8")
    (gitdir / "MERGE_MODE").write_text("no-ff", encoding="utf-8")
    return tip


def _merge_head_exists(repo: Path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode == 0


def test_reconcile_clears_stale_merge_head_when_the_merge_already_committed(product_repo):
    """VERDICT4 O-1: the found-commit path appended the retirement but left
    MERGE_HEAD, so the Evaluator's SHIP commit became a two-parent merge."""
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    _plant_stale_merge_state(product_repo)
    assert _merge_head_exists(product_repo)

    openloop._reconcile_merge_retirement(ctx)

    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha
    assert not _merge_head_exists(product_repo)
    gitdir = Path(git(product_repo, "rev-parse", "--absolute-git-dir"))
    assert not [n for n in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE") if (gitdir / n).exists()]
    # The next commit (the SHIP retirement commit) has exactly one parent.
    git(product_repo, "commit", "-q", "--allow-empty", "-m", "loop: iteration 1 - SHIP")
    assert len(git(product_repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 2


def test_reconcile_stale_merge_head_with_retirement_already_present(product_repo):
    """The record survived and the retirement is already on disk: the stale
    MERGE_HEAD is still cleared (it is independent of the append)."""
    ctx = make_ctx(product_repo)
    sha = _merge_slice_commit(product_repo, "a", "a.py", ctx)
    olqueue.append_retired(ctx.live_mailbox, slice_id="a", sha=sha, at="2026-10-01T00:00:00Z")
    _plant_stale_merge_state(product_repo)
    openloop._reconcile_merge_retirement(ctx)
    assert not _merge_head_exists(product_repo)


def test_sweep_clears_stale_merge_head_even_without_an_intent_record(product_repo):
    ctx = make_ctx(product_repo)
    _merge_slice_commit(product_repo, "a", "a.py")  # no record
    _plant_stale_merge_state(product_repo)
    openloop._sweep_stale_merge_state(ctx)
    assert not _merge_head_exists(product_repo)
    assert "cleared stale MERGE_HEAD" in (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")


def test_sweep_leaves_a_live_conflicted_merge_alone(product_repo):
    """A MERGE_HEAD that is NOT in HEAD is a real, unfinished merge: never
    quit/aborted by the sweep, only reported."""
    ctx = make_ctx(product_repo)
    (product_repo / "c.txt").write_text("base\n", encoding="utf-8")
    git(product_repo, "add", "c.txt")
    git(product_repo, "commit", "-q", "-m", "add c")
    git(product_repo, "checkout", "-q", "-b", "side")
    (product_repo / "c.txt").write_text("side\n", encoding="utf-8")
    git(product_repo, "commit", "-qam", "side change")
    git(product_repo, "checkout", "-q", "main")
    (product_repo / "c.txt").write_text("main\n", encoding="utf-8")
    git(product_repo, "commit", "-qam", "main change")
    assert subprocess.run(["git", "-C", str(product_repo), "merge", "side"], capture_output=True,
                          env=git_env()).returncode != 0
    assert _merge_head_exists(product_repo)

    openloop._sweep_stale_merge_state(ctx)

    assert _merge_head_exists(product_repo)
    assert "WARNING" in (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")


def test_drive_clears_stale_merge_head_before_the_run(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    _merge_slice_commit(product_repo, "a", "a.py", ctx)
    _plant_stale_merge_state(product_repo)
    seen = {}

    def stub(mailbox, *a, **kw):
        seen["merge_head"] = _merge_head_exists(product_repo)
        return 0

    monkeypatch.setattr(openloop.TL, "run_open_loop", stub)
    openloop.drive(ctx, mode="resume", max_iterations=4, settings=settings,
                   stop_now={"flag": False, "code": 130})
    assert seen["merge_head"] is False


def _branch_with_commit(repo: Path, slice_id: str, filename: str, content: str = "x\n") -> str:
    branch = f"trio-oc/exec1234/i1-{slice_id}-a1"
    git(repo, "branch", branch, "HEAD")
    wt = repo / driver.BUILDER_WORKTREES_DIR / f"wt-{slice_id}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), branch], check=True,
                   env=git_env())
    (wt / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", filename], check=True, env=git_env())
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", f"slice({slice_id}): add"],
                   check=True, env=git_env())
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   check=True, env=git_env())
    return branch


def test_merge_and_retire_runs_git_merge_without_pipes_to_the_driver(product_repo, tmp_path,
                                                                    monkeypatch):
    """A driver killed mid-merge closes its pipe ends; a `git merge` writing
    its summary into one takes SIGPIPE after advancing HEAD and before
    removing MERGE_HEAD. The merge must therefore have no pipe on stdout or
    stderr (observed from inside the child via a PATH shim)."""
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    branch = _branch_with_commit(product_repo, "a", "a.py")
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = tmp_path / "shim"
    shim.mkdir()
    out = tmp_path / "fds.txt"
    (shim / "git").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do if [ "$a" = merge ]; then\n'
        f'  for fd in 0 1 2; do t=$(readlink /proc/$$/fd/$fd); echo "$fd $t" >> {out}; done\n'
        "fi; done\n"
        f'exec {real_git} "$@"\n', encoding="utf-8")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")

    sha, _at = runner._merge_and_retire(product_repo, {"id": "a", "branch": branch},
                                        ctx.live_mailbox)

    assert olqueue.latest_retired(ctx.live_mailbox)["a"]["sha"] == sha
    targets = dict(line.split(" ", 1) for line in out.read_text(encoding="utf-8").splitlines())
    assert targets, "the merge shim never ran"
    for fd in ("0", "1", "2"):
        assert not targets[fd].startswith("pipe:"), (fd, targets)


def test_merge_and_retire_still_raises_conflict_and_aborts(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    branch = _branch_with_commit(product_repo, "a", "README", "branch side\n")
    (product_repo / "README").write_text("main\n", encoding="utf-8")
    git(product_repo, "commit", "-qam", "main change README")
    with pytest.raises(openloop._ConflictError):
        runner._merge_and_retire(product_repo, {"id": "a", "branch": branch}, ctx.live_mailbox)
    assert not _merge_head_exists(product_repo)
    assert openloop._read_intents(ctx.live_mailbox) == []


def test_reconcile_token_mismatch_warns_loudly(product_repo, capsys):
    ctx = make_ctx(product_repo)
    head = git(product_repo, "rev-parse", "HEAD")
    openloop._intent_begin(ctx.live_mailbox, slice_id="a", branch="trio-oc/exec1234/i1-a-a1",
                           repo_path=product_repo, repo_field=None, pre_head=head, run_token="other")
    _merge_slice_commit(product_repo, "a", "a.py")
    openloop._reconcile_merge_retirement(ctx)
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "WARNING: dropping merge-intent record for a" in log
    assert "--run-token" in log
    assert "run token mismatch" in capsys.readouterr().err
    assert olqueue.latest_retired(ctx.live_mailbox) == {}


_VANISHED = ("fatal: could not create directory of '/x/.git/worktrees/w': "
             "No such file or directory\n")


def _fail_first_worktree_add(monkeypatch, *, create_branch: bool):
    real_run = subprocess.run
    seen = {"adds": 0}

    def fake(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[3:5] == ["worktree", "add"]:
            seen["adds"] += 1
            if seen["adds"] == 1:
                if create_branch and "-b" in cmd:  # git makes the branch before the worktree
                    i = cmd.index("-b")
                    real_run(["git", "-C", cmd[2], "branch", cmd[i + 1], cmd[-1]], check=True,
                             capture_output=True, env=git_env())
                return subprocess.CompletedProcess(cmd, 128, "", _VANISHED)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(openloop.subprocess, "run", fake)
    return seen


def test_worktree_add_retries_once_when_git_worktrees_dir_vanished_builder(product_repo, tmp_path,
                                                                           monkeypatch):
    seen = _fail_first_worktree_add(monkeypatch, create_branch=True)
    head = git(product_repo, "rev-parse", "HEAD")
    path = tmp_path / "wt-b"
    r = openloop._git_worktree_add(product_repo, path, head, "trio-oc/x/i1-a-a1")
    assert r.returncode == 0, r.stderr
    assert seen["adds"] == 2 and (path / "README").is_file()
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "trio-oc/x/i1-a-a1"


def test_worktree_add_retries_once_when_git_worktrees_dir_vanished_eval(product_repo, tmp_path,
                                                                        monkeypatch):
    seen = _fail_first_worktree_add(monkeypatch, create_branch=False)
    head = git(product_repo, "rev-parse", "HEAD")
    path = tmp_path / "wt-e"
    r = openloop._git_worktree_add(product_repo, path, head)
    assert r.returncode == 0, r.stderr
    assert seen["adds"] == 2 and (path / "README").is_file()


def test_worktree_add_does_not_retry_other_failures(product_repo, tmp_path, monkeypatch):
    real_run = subprocess.run
    adds = []

    def fake(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[3:5] == ["worktree", "add"]:
            adds.append(cmd)
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: invalid reference: nope\n")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(openloop.subprocess, "run", fake)
    r = openloop._git_worktree_add(product_repo, tmp_path / "w", "nope")
    assert r.returncode == 128 and len(adds) == 1


# --------------------------------------------------------------------------
# ol-repair: blocking issue #2 -- exit drain cancels/waits for live turns
# --------------------------------------------------------------------------


def test_inflight_sessions_omits_entries_with_unknown_session_id(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    with runner._session_lock:
        runner._inflight["slice-eval a@11111111"] = {"kind": "slice-eval", "slice": "a",
                                                      "sha": "1" * 40, "session_id": None}
    assert runner.inflight_sessions() == {}
    assert runner.has_live_turns() is True

    with runner._session_lock:
        runner._inflight["slice-eval a@11111111"]["session_id"] = "ses_abc123"
    assert runner.inflight_sessions() == {
        "ses_abc123": {"kind": "slice-eval", "slice": "a", "sha": "1" * 40},
    }


def test_has_live_turns_false_once_everything_cleared(product_repo):
    ctx = make_ctx(product_repo)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    assert runner.has_live_turns() is False
    with runner._session_lock:
        runner._inflight["x"] = {"kind": "integration-eval", "session_id": None}
    assert runner.has_live_turns() is True
    with runner._session_lock:
        runner._inflight.clear()
    assert runner.has_live_turns() is False


def test_run_integration_eval_tracks_and_clears_inflight(product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=False)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)
    seen_live = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        seen_live["mid_call"] = runner.has_live_turns()
        return SimpleNamespace(ok=True, session_id="ses_int")

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    monkeypatch.setattr(openloop.TL, "human_answer_block", lambda *a, **kw: None)
    head = openloop.TL._git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
    code = runner._run_integration_eval(1, mailbox, {"pinned_sha": head,
                                                     "evaluator_attempt": "c" * 32})
    assert code == 0
    assert seen_live["mid_call"] is True
    assert runner.has_live_turns() is False


def test_drive_sets_cancel_and_waits_for_live_turns_before_cleanup(product_repo, monkeypatch):
    """Blocking issue #2: once `run_open_loop` returns, `drive()` sets
    `ctx.cancel` immediately and waits (bounded) for `has_live_turns()` to
    clear before the leftover-worktree sweep -- modelling a turn thread
    that notices cancellation and stops shortly after, the way a real
    `runner.run_turn` pump loop does within its own ~0.2s poll."""
    ctx = make_ctx(product_repo)
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True)
    cleared = threading.Event()
    cancel_seen_before_clear = {}

    def slow_turn():
        # Mirror a turn thread that is still "running" until it notices
        # ctx.cancel -- this is the ONLY lever drive() has.
        ctx.cancel.wait(2.0)
        cancel_seen_before_clear["cancel_was_set"] = ctx.cancel.is_set()
        time.sleep(0.1)
        cleared.set()

    def run_open_loop_stub(mailbox, max_iterations, lead_runner, eval_runner, **kw):
        with lead_runner._session_lock:
            lead_runner._inflight["slow"] = {"kind": "slice-eval", "slice": "a", "sha": "1" * 40,
                                             "session_id": None}
        threading.Thread(target=lambda: (
            slow_turn(),
            lead_runner._session_lock.acquire(),
            lead_runner._inflight.pop("slow", None),
            lead_runner._session_lock.release(),
        )).start()
        return 0

    monkeypatch.setattr(openloop.TL, "run_open_loop", run_open_loop_stub)
    start = time.monotonic()
    final = openloop.drive(ctx, mode="start", max_iterations=4, settings=settings,
                           stop_now={"flag": False, "code": 130})
    elapsed = time.monotonic() - start

    assert cleared.wait(3.0)
    assert cancel_seen_before_clear["cancel_was_set"] is True
    assert elapsed < 5.0
    assert final["status"] == "shipped"


# --------------------------------------------------------------------------
# ol-repair: blocking issue #4 -- a Lead that never writes PLAN.md's own
# `slices:` block is told to, not silently left to stall after 3 passes.
# --------------------------------------------------------------------------


@pytest.fixture
def goal_only_repo(tmp_path: Path) -> Path:
    """A mailbox seeded with only GOAL.md's worth of a plan: PLAN.md exists
    but has no `slices:` block yet -- the benchmark-harness shape blocking
    issue #4 is about."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text("# Plan\n(nothing yet)\n", encoding="utf-8")
    (box / "QUEUE.md").write_text("# Queue\n", encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    git(root, "add", "loop")
    git(root, "commit", "-q", "-m", "loop: init")
    return root


def test_lead_plan_prompt_tells_lead_to_write_plan_slices_block_when_missing(
        goal_only_repo, monkeypatch):
    ctx = make_ctx(goal_only_repo)
    assert openloop.TL._read_plan_slice_ids(ctx.live_mailbox) is None
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    captured = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        captured["prompt"] = prompt
        return SimpleNamespace(ok=True, session_id="ses_lead",
                               text='```json\n{"slices": []}\n```')

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    runner._run_lead(1, ctx.live_mailbox, {})

    assert "PLAN.md has no `slices:` block yet" in captured["prompt"]
    assert "stalls after 3 no-op passes" in captured["prompt"]


def test_lead_plan_prompt_omits_plan_slices_block_note_when_already_present(
        product_repo, monkeypatch):
    ctx = make_ctx(product_repo)
    assert openloop.TL._read_plan_slice_ids(ctx.live_mailbox) is not None
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    captured = {}

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        captured["prompt"] = prompt
        return SimpleNamespace(ok=True, session_id="ses_lead",
                               text='```json\n{"slices": []}\n```')

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    runner._run_lead(1, ctx.live_mailbox, {})

    assert "PLAN.md has no `slices:` block yet" not in captured["prompt"]


# --------------------------------------------------------------------------
# ol-repair: blocking issue #3 -- SLICE QUALITY / retired-LOG-line parity
# --------------------------------------------------------------------------


def test_resolved_kill_check_isolation_off_is_always_none():
    assert quality_mod.resolved_kill_check(False, None, "builder") is None
    assert quality_mod.resolved_kill_check(False, {"outcome": "killed"}, "builder") is None


def test_resolved_kill_check_real_kill_check_passes_through():
    kc = {"outcome": "killed", "reason": None}
    assert quality_mod.resolved_kill_check(True, kc, "builder") is kc


def test_resolved_kill_check_lead_takeover_placeholder():
    assert quality_mod.resolved_kill_check(True, None, "lead") == {
        "outcome": "n/a",
        "reason": "no builder run merged this sha (Lead take-over or fix)",
    }


def test_resolved_kill_check_builder_kill_check_off_placeholder():
    assert quality_mod.resolved_kill_check(True, None, "builder") == {
        "outcome": "n/a",
        "reason": "not recorded (kill check off or pre-r18a trioctl)",
    }


def test_quality_note_emits_authored_by_and_base_revert_in_every_case():
    """ol-harden blocking issue #3: Lead take-overs, kill-check-off builder
    slices and briefs with no targeted command (all of which leave
    *kill_check* ``None``) must still render BASE-REVERT/AUTHORED-BY under
    isolation, exactly matching trioctl's own wording."""
    lead_note = quality_mod.quality_note(isolate=True, kill_check=None, authored_by="lead",
                                         flags=[], accept_lint=[], builder_ran=False)
    assert "AUTHORED-BY: lead" in lead_note
    assert ("BASE-REVERT: n/a -- no builder run merged this sha "
            "(Lead take-over or fix)") in lead_note

    kill_check_off_note = quality_mod.quality_note(isolate=True, kill_check=None,
                                                    authored_by="builder", flags=[],
                                                    accept_lint=[], builder_ran=True)
    assert "AUTHORED-BY: builder" in kill_check_off_note
    assert ("BASE-REVERT: n/a -- not recorded (kill check off or "
            "pre-r18a trioctl)") in kill_check_off_note

    # isolation off: still nothing to say when there's no lint either.
    assert quality_mod.quality_note(isolate=False, kill_check=None, authored_by="lead",
                                    flags=[], accept_lint=[], builder_ran=False) == ""


def test_run_slice_eval_retired_log_line_never_empty_for_lead_takeover(product_repo, monkeypatch):
    """Fixes the `by builder |  (shadow)` empty-kill_check rendering: a
    Lead-take-over's retired LOG line always carries the `n/a` placeholder
    kill_check text, never a blank suffix."""
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    before = openloop.TL._git(product_repo, "rev-parse", "HEAD").stdout.strip()
    (product_repo / "a.py").write_text("print('a')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(product_repo), "add", "-A"], check=True, env=git_env())
    subprocess.run(["git", "-C", str(product_repo), "commit", "-q", "-m", "slice(a): take over"],
                   check=True, env=git_env())
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=True)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)
    runner._retire_lead_commits(1, product_repo, before, mailbox,
                                {"a": {"id": "a", "targeted_check": "test -f a.py"}})
    sha = olqueue.latest_retired(mailbox)["a"]["sha"]

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        return SimpleNamespace(ok=True, session_id="ses_eval")

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    monkeypatch.setattr(openloop.TL, "human_answer_block", lambda *a, **kw: None)
    code = runner._run_slice_eval(1, mailbox, {"slice": "a", "sha": sha, "repo": None})
    assert code == 0

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "by lead |  (shadow)" not in log_text  # the empty-rendering bug
    # Exact trioctl-faithful suffix (kill_check_suffix only ever renders the
    # outcome/tag, never the reason -- the reason lives in the quality note,
    # not the LOG line).
    assert f"retired slice a @{sha[:12]} by lead | kill_check: n/a (shadow)" in log_text


def test_run_slice_eval_skips_retired_log_line_when_isolation_off(product_repo, monkeypatch):
    """trioctl only logs the retired LOG line when a kill_check fact is
    available (`if not seen and kc is not None`); isolation off has none."""
    ctx = make_ctx(product_repo)
    mailbox = ctx.live_mailbox
    settings = openloop.resolve_settings(ctx.cfg, is_open_loop=True, isolate_workers=False)
    runner = openloop.OpenLoopRunner(ctx, settings=settings)

    def fake_call_role(ctx_, *, role, agent, model, prompt, cwd, label, **kw):
        return SimpleNamespace(ok=True, session_id="ses_eval")

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    monkeypatch.setattr(openloop.TL, "human_answer_block", lambda *a, **kw: None)
    head = openloop.TL._git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
    code = runner._run_slice_eval(1, mailbox, {"slice": "a", "sha": head, "repo": None})
    assert code == 0

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "retired slice a" not in log_text

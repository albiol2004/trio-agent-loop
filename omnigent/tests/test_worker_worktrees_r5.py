"""Regressions for the independent ITERATE on d39bfd9 (B1, B2, N1-N6).

Offline, real git. Every secret below is a fake literal; tests assert it
never crosses roots and never appears in any message.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest

from test_worker_worktrees_r4 import (  # noqa: E402  (sibling helpers)
    GIT_ENV,
    REPO_ROOT,
    SCRIPT,
    MODULE,
    _LaunchingClient,
    _drive,
    _e2e_runner,
    _integrated_demo_worker,
    _load,
    _open_loop_repo,
    _vendor,
    git,
    omnigent_launch,
)

FAKE_MAIN = "FAKE-MAIN-ONLY-SECRET-0001"
FAKE_SIDE = "FAKE-SIDE-ONLY-SECRET-0002"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_r5", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r5", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def loop_core():
    return _load("trio_loop_r5", REPO_ROOT / "metrics" / "trio_loop.py")


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def side(repo, tmp_path):
    """A linked worktree: a second aggregate root sharing repo's common dir."""
    path = tmp_path / "side-root"
    git(repo, "worktree", "add", "-q", "-b", "side", str(path))
    return path


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


def _user_mcp(path: Path, secret: str, *, name: str = "db") -> bytes:
    raw = json.dumps({"mcpServers": {name: {"command": "x", "env": {"API_TOKEN": secret}}}}).encode()
    (path / ".cursor").mkdir(exist_ok=True)
    (path / ".cursor" / "mcp.json").write_bytes(raw)
    return raw


def _ledger_texts(wt, repo) -> str:
    return "\n".join(p.name for p in (wt.ledger_dir(repo) / "root-cursor").glob("*"))


# ------------------------------------------------------------------ B1


def test_b1_two_roots_one_common_dir_restore_their_own_bytes(wt, repo, side, loop_core):
    main_raw = _user_mcp(repo, FAKE_MAIN)
    side_raw = _user_mcp(side, FAKE_SIDE, name="cache")
    assert wt.common_dir(repo) == wt.common_dir(side)
    assert wt.snapshot_root_cursor(repo) is True
    assert wt.snapshot_root_cursor(side) is True          # its own baseline, not "taken"
    omnigent_launch(repo, "main-lead")
    omnigent_launch(side, "side-lead")                    # both roots live at once
    wt.record_root_session(repo, "main-lead", repo / "loop")
    wt.record_root_session(side, "side-lead", side / "loop")
    # Main finishes first; its final restore must not touch the side root's records.
    assert wt.restore_root_cursor(repo, {"main-lead"}, final=True) == []
    assert (repo / ".cursor" / "mcp.json").read_bytes() == main_raw
    assert wt.root_owned_sessions(side) == {"side-lead": str(side / "loop")}
    assert wt.restore_root_cursor(side, {"side-lead"}, final=True) == []
    assert (side / ".cursor" / "mcp.json").read_bytes() == side_raw
    for path, secret in ((repo, FAKE_SIDE), (side, FAKE_MAIN)):
        for f in (path / ".cursor").iterdir():
            assert secret not in f.read_text()
    assert not (wt.ledger_dir(repo) / "root-cursor").exists()


def test_b1_reviewer_c10_main_secret_never_lands_in_linked_root(wt, repo, side):
    (repo / ".cursor").mkdir()
    secret = json.dumps({"mcpServers": {"omnigent": {"command": "mine",
                                                     "env": {"API_TOKEN": FAKE_MAIN}}}}).encode()
    (repo / ".cursor" / "mcp.json").write_bytes(secret)
    assert wt.snapshot_root_cursor(repo) is True
    assert wt.snapshot_root_cursor(side) is True
    omnigent_launch(side, "w1")
    problems = wt.restore_root_cursor(side, {"w1"})
    assert problems == []
    assert not (side / ".cursor" / "mcp.json").exists()
    assert git(side, "status", "--porcelain") == ""
    assert (repo / ".cursor" / "mcp.json").read_bytes() == secret   # untouched


def test_b1_one_root_cannot_consume_or_delete_another_roots_state(wt, repo, side):
    wt.snapshot_root_cursor(side)
    wt.record_root_session(side, "side-1", side / "loop")
    before = _ledger_texts(wt, repo)
    # main has no baseline: nothing of side's is used or removed.
    omnigent_launch(repo, "main-1")
    assert wt.restore_root_cursor(repo, {"main-1"}, final=True) == []
    assert _ledger_texts(wt, repo) == before
    assert wt.root_owned_sessions(repo) == {}


def test_b1_record_bound_to_another_root_is_refused(wt, repo, side):
    wt.snapshot_root_cursor(repo)
    key_main, _ = wt._root_identity(repo)
    key_side, _ = wt._root_identity(side)
    store = wt.ledger_dir(repo) / "root-cursor"
    # Misuse: copy main's record under the side key (e.g. a hand migration).
    (store / f"{key_side}.baseline.json").write_bytes(
        (store / f"{key_main}.baseline.json").read_bytes())
    with pytest.raises(wt.WorktreeError, match="belongs to"):
        wt.snapshot_root_cursor(side)
    with pytest.raises(wt.WorktreeError, match="belongs to"):
        wt.restore_root_cursor(side, set())


def test_b1_unkeyed_d39bfd9_records_are_reported_never_used_or_deleted(wt, repo, loop_core):
    store = wt.ledger_dir(repo) / "root-cursor"
    store.mkdir(parents=True)
    legacy = {"baseline.json": b'{"schema": 1, "files": {}}', "mcp.json.orig": b"{}"}
    for name, data in legacy.items():
        (store / name).write_bytes(data)
    wt.snapshot_root_cursor(repo)
    omnigent_launch(repo, "s1")
    problems = wt.restore_root_cursor(repo, {"s1"}, final=True)
    assert any("unattributed root Cursor record" in p for p in problems)
    assert not (repo / ".cursor" / "mcp.json").exists()           # own restore still done
    for name, data in legacy.items():
        assert (store / name).read_bytes() == data                 # never consumed or deleted


# ------------------------------------------------------------------ B2


def test_b2_reviewer_c8_recorded_held_session_is_never_stripped(wt, repo):
    wt.snapshot_root_cursor(repo)
    omnigent_launch(repo, "held-1")
    wt.record_root_session(repo, "held-1", repo / "loop")
    problems = wt.restore_root_cursor(repo, set(), final=True)
    assert (repo / ".cursor" / "mcp.json").exists()
    assert problems and all("left in place" in p for p in problems)
    assert wt.root_owned_sessions(repo) == {"held-1": str(repo / "loop")}   # evidence kept


def _hold(mailbox: Path, sid: str) -> None:
    (mailbox / ".sessions").mkdir(parents=True, exist_ok=True)
    (mailbox / ".sessions" / f"held-{sid}.json").write_text(json.dumps({
        "session_id": sid, "role": "lead", "hold": "role_completion_uncertain"}))


def test_b2_runner_excludes_held_in_any_recording_mailbox_and_inflight(trioctl, wt, repo, root):
    here, there = repo / "loop", repo / "other-box"
    there.mkdir()
    runner = trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={}, interval=0,
                                    isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)})
    for sid, box in (("done-1", here), ("held-here", here), ("held-there", there),
                     ("flying", here)):
        wt.record_root_session(repo, sid, box)
    _hold(here, "held-here")
    _hold(there, "held-there")
    runner._inflight.add("flying")
    assert trioctl._held_session_ids(there) == {"held-there"}
    assert runner._root_owned_ids(here) == {"done-1"}


def test_b3_runner_never_owns_another_mailboxs_running_or_unattributed_record(
    trioctl, wt, repo, root
):
    here, there = repo / "loop", repo / "other-box"
    there.mkdir()
    runner = trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={}, interval=0,
                                    isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)})
    wt.record_root_session(repo, "done-1", here)
    wt.record_root_session(repo, "other-live", there)       # not held, not in our _inflight
    wt.record_root_session(repo, "no-box")                  # ownership uncertain
    wt.record_root_session(repo, "contested", there)
    runner.created_session_ids.append("contested")          # conflicting evidence: fail closed
    assert runner._root_owned_ids(here) == {"done-1"}
    assert runner._root_owned_ids(there) == {"other-live", "contested"}   # its own records


def test_b3_reviewer_r18b_other_mailboxs_new_session_survives_restore(
    trioctl, wt, repo, root, monkeypatch
):
    """R18b: loop B's session is recorded before its cursor-agent starts."""
    here, there = repo / "loop", repo / "box2"
    there.mkdir()
    raw = b'{ "mcpServers" : {"db": {"command": "mine", "env": {"API_TOKEN": "%s"}}}}' % (
        FAKE_MAIN.encode())
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_bytes(raw)
    monkeypatch.setattr(wt, "cursor_processes_at", lambda _repo: [])  # the launch gap
    runner = trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={}, interval=0,
                                    isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)})
    runner._prepare_root_config(here)                        # loop A's baseline
    wt.record_root_session(repo, "other-live", there)        # loop B (another process)
    omnigent_launch(repo, "other-live")
    mcp_before = (repo / ".cursor" / "mcp.json").read_bytes()
    hooks_before = (repo / ".cursor" / "hooks.json").read_bytes()
    problems = runner._restore_root_config(here, final=True)
    assert (repo / ".cursor" / "mcp.json").read_bytes() == mcp_before
    assert (repo / ".cursor" / "hooks.json").read_bytes() == hooks_before
    data = json.loads(mcp_before)
    assert wt._bridge_arg(data["mcpServers"]["omnigent"]["args"]) == wt.bridge_key("other-live")
    assert problems, "unowned residue must fail closed, not report a clean restore"
    assert not any(FAKE_MAIN in p for p in problems)
    assert wt.root_owned_sessions(repo) == {"other-live": str(there)}   # evidence kept

    # Same-mailbox cleanup still works once loop B's residue is gone: A's own
    # recorded (not created-in-process) session is undone back to the baseline.
    (repo / ".cursor" / "mcp.json").write_bytes(raw)
    (repo / ".cursor" / "hooks.json").unlink()
    wt.record_root_session(repo, "own-done", here)
    omnigent_launch(repo, "own-done")
    fresh = trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={}, interval=0,
                                   isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)})
    assert fresh._root_owned_ids(here) == {"own-done"}
    assert fresh._restore_root_config(here) == []
    assert (repo / ".cursor" / "mcp.json").read_bytes() == raw
    assert not (repo / ".cursor" / "hooks.json").exists()


def test_b2_e2e_held_root_session_config_survives_the_run(
    trioctl, wt, repo, root, loop_core, monkeypatch
):
    mailbox = _open_loop_repo(repo)
    _integrated_demo_worker(wt, repo, root)
    runner, _ended = _e2e_runner(trioctl, repo, root, monkeypatch, _LaunchingClient())
    omnigent_launch(repo, "held-old")                       # a held session's live config
    wt.snapshot_root_cursor(repo)                            # its pre-state for this run
    _hold(mailbox, "held-old")
    wt.record_root_session(repo, "held-old", mailbox)
    runner.created_session_ids.append("held-old")            # even if this run made it
    runner._prepare_root_config(mailbox)
    data = json.loads((repo / ".cursor" / "mcp.json").read_text())
    assert wt._bridge_arg(data["mcpServers"]["omnigent"]["args"]) == wt.bridge_key("held-old")
    hooks = json.loads((repo / ".cursor" / "hooks.json").read_text())["hooks"]["stop"]
    assert [wt._usage_hook_bridge(e) for e in hooks] == [wt.bridge_key("held-old")]


# ------------------------------------------------------------------ N1


def test_n1_reviewer_c9_write_after_recheck_is_kept_on_replace(wt, repo, monkeypatch):
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n')
    wt.snapshot_root_cursor(repo)
    omnigent_launch(repo, "s1")
    target = repo / ".cursor" / "mcp.json"
    foreign = b'{"mcpServers": {"late": {"command": "z"}}}\n'
    real_open, fired = wt.os.open, []

    def racing_open(path, *a, **k):
        if ".trio-restore-" in str(path) and not fired:
            fired.append(1)
            target.write_bytes(foreign)
        return real_open(path, *a, **k)

    monkeypatch.setattr(wt.os, "open", racing_open)
    problems = wt.restore_root_cursor(repo, {"s1"})
    monkeypatch.setattr(wt.os, "open", real_open)
    assert fired
    assert target.read_bytes() == foreign
    assert any("changed during restore" in p for p in problems)
    assert not list((repo / ".cursor").glob(".*trio-restore-*"))


def test_n1_write_after_recheck_is_kept_on_removal(wt, repo, monkeypatch):
    wt.snapshot_root_cursor(repo)                            # baseline: absent
    omnigent_launch(repo, "s1")
    target = repo / ".cursor" / "mcp.json"
    foreign = b'{"mcpServers": {"late": {"command": "z"}}}\n'
    real, fired = wt._renameat2, []

    def racing(src, dst, flags):
        if Path(src) == target and not fired:
            fired.append(1)
            target.write_bytes(foreign)
        return real(src, dst, flags)

    monkeypatch.setattr(wt, "_renameat2", racing)
    problems = wt.restore_root_cursor(repo, {"s1"})
    assert fired and target.read_bytes() == foreign
    assert any("mcp.json changed during restore" in p for p in problems)


def test_n1_write_racing_the_swap_back_is_retained_not_lost(wt, repo, monkeypatch):
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n')
    wt.snapshot_root_cursor(repo)
    omnigent_launch(repo, "s1")
    target = repo / ".cursor" / "mcp.json"
    first, second = b'{"a": 1}\n', b'{"b": 2}\n'
    real, calls = wt._renameat2, []

    def racing(src, dst, flags):
        calls.append(flags)
        if flags == wt._RENAME_EXCHANGE and len(calls) == 1:
            target.write_bytes(first)            # lands before our exchange
        elif flags == wt._RENAME_EXCHANGE and len(calls) == 2:
            target.write_bytes(second)           # lands before our swap-back
        return real(src, dst, flags)

    monkeypatch.setattr(wt, "_renameat2", racing)
    problems = wt.restore_root_cursor(repo, {"s1"})
    kept = list((repo / ".cursor").glob(".mcp.json.trio-restore-*"))
    assert target.read_bytes() == first
    assert len(kept) == 1 and kept[0].read_bytes() == second
    assert any("kept at" in p for p in problems)


# ------------------------------------------------------------ N3 / N4


def test_n3_legacy_restore_keeps_the_index_mode(wt, repo):
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n')
    os.chmod(repo / ".cursor" / "mcp.json", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "exec mcp")
    omnigent_launch(repo, "s1")                               # no baseline (legacy)
    assert wt.restore_root_cursor(repo, {"s1"}) == []
    assert stat.S_IMODE((repo / ".cursor" / "mcp.json").stat().st_mode) == 0o755
    assert git(repo, "status", "--porcelain") == ""


def test_n3_legacy_tracked_symlink_is_refused(wt, repo):
    (repo / "real.json").write_text("{}\n")
    (repo / ".cursor").mkdir()
    os.symlink("../real.json", repo / ".cursor" / "hooks.json")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "symlinked hooks")
    os.unlink(repo / ".cursor" / "hooks.json")
    omnigent_launch(repo, "s1")
    problems = wt.restore_root_cursor(repo, {"s1"})
    assert any("mode 120000" in p for p in problems)
    assert (repo / ".cursor" / "hooks.json").is_file()


def test_n4_stale_baseline_mismatch_names_the_record_and_how_to_retire_it(wt, repo):
    wt.snapshot_root_cursor(repo)                             # crashed run: absent
    _user_mcp(repo, "FAKE-TEAM-0003", name="team")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "team mcp")
    omnigent_launch(repo, "s1")
    (problem,) = [p for p in wt.restore_root_cursor(repo, {"s1"}) if "mcp.json" in p]
    assert ".baseline.json" in problem and "remove that baseline" in problem
    assert "FAKE-TEAM-0003" not in problem


# ------------------------------------------------------------ N5 / N6


def test_n5_root_session_is_recorded_before_the_dispatch_completes(
    trioctl, wt, repo, root, monkeypatch
):
    client = _LaunchingClient()
    runner, _ = _e2e_runner(trioctl, repo, root, monkeypatch, client)
    mailbox = _open_loop_repo(repo)
    seen = {}

    def crash(client_, agent_id, model, prompt, title, role, iteration, mb, ctx,
              started, before_text, before_mtime, dispatch, workspace):
        runner._create_wait_read(client_, agent_id, model, prompt, title, role,
                                 workspace=workspace, dispatch=dispatch)
        seen.update(wt.root_owned_sessions(repo))            # before the dispatch ends
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_run_dispatch", crash)
    with pytest.raises(KeyboardInterrupt):
        runner.run("lead", 1, mailbox, None)
    (sid,) = seen
    assert seen[sid] == str(mailbox)


@pytest.mark.parametrize("prune", ["raises", "not-deleted"])
def test_n6_prune_failure_keeps_bookkeeping_and_config(
    trioctl, wt, repo, root, loop_core, monkeypatch, prune
):
    mailbox = _open_loop_repo(repo)
    rec = _integrated_demo_worker(wt, repo, root)
    client = _LaunchingClient()
    runner, _ = _e2e_runner(trioctl, repo, root, monkeypatch, client)

    def failing(client_, mb, session_ids=None, **kw):
        titles = {sid: title for sid, title, _ws in client.created}
        if not any("integration-eval" in titles.get(s, "") for s in session_ids):
            return {"archived": len(session_ids), "deleted": len(session_ids)}
        if prune == "raises":                     # only the evaluator's own prune fails
            raise RuntimeError("archive failed")
        return {"archived": 0, "deleted": 0, "failed": 1}

    monkeypatch.setattr(trioctl, "_prune_broker_sessions", failing)
    code = _drive(loop_core, mailbox, repo, runner)
    # r11: the config is left in place, but exact generated residue is not
    # product, so the bound SHIP is accepted (a user edit would still block).
    assert code == 0
    assert (repo / ".cursor" / "mcp.json").exists()             # config left in place
    evaluators = [e for e in runner._root_finished if e["role"] == "evaluator"]
    assert evaluators, "finished evaluator bookkeeping must be kept"
    (repo / ".cursor" / "other.json").write_text("{}\n")       # real product residue
    assert "pending" in trioctl._ship_acceptance(mailbox, repo, loop_core)
    assert Path(rec["path"]).is_dir()


# ------------------------------------------------------------------ N2


def test_n2_mixed_metrics_without_marker_is_refused(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    metrics = repo / "metrics" / "trio-metrics.py"
    metrics.write_text(re.sub(r"^METRICS_API = \d+\n", "", metrics.read_text(), flags=re.M))
    with pytest.raises(trioctl.TrioctlError, match="METRICS_API 1.*mixed metrics"):
        trioctl._load_trio_loop(repo)
    assert "mixed metrics" in trioctl._ship_acceptance(repo / "loop", repo)["pending"]


def test_n2_marked_but_broken_metrics_is_a_clear_refusal(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    metrics = repo / "metrics" / "trio-metrics.py"
    metrics.write_text(metrics.read_text().replace("def parse_verdict_scope", "def _gone"))
    with pytest.raises(trioctl.TrioctlError, match="loading it failed \\(AttributeError"):
        trioctl._load_trio_loop(repo)
    assert trioctl._ship_acceptance(repo / "loop", repo)["pending"].startswith(
        "loop core not usable: incompatible loop core")


def test_n2_rebound_marker_is_ambiguous_and_refused(trioctl, repo):
    core = _vendor(repo, REPO_ROOT / "metrics")
    core.write_text(core.read_text().replace(
        "LOOP_CORE_API = 2\n", "LOOP_CORE_API = 2\nLOOP_CORE_API = 3\n"))
    with pytest.raises(trioctl.TrioctlError, match="LOOP_CORE_API 0"):
        trioctl._load_trio_loop(repo)


def test_n2_current_set_loads(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    module = trioctl._load_trio_loop(repo)
    assert module.LOOP_CORE_API == 2 and module._METRICS.METRICS_API == 4

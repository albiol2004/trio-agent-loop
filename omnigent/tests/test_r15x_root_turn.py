"""r15.x root-turn lock (r16 DESIGN §2, acceptance I1-I8).

Fake role runner + real git + real processes: separate `trioctl omnigent
loop` driver processes (`r15x_loop_harness.py`), a fake `cursor-agent`
(a shell script of that name that sleeps with a chosen cwd, detected by the
same /proc scan the driver uses), one-shots through the real
`trioctl omnigent run`.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from test_worker_worktrees import GIT_ENV, MODULE, SCRIPT, _load, git

REPO_ROOT = SCRIPT.parent.parent
HARNESS = Path(__file__).with_name("r15x_loop_harness.py")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home_dir = tmp_path / "userhome"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TRIO_ROOT_TURN_POLL_S", "0.05")
    wt = _load("worker_worktrees_r15x", MODULE)
    trioctl = _load("trioctl_r15x", SCRIPT)
    monkeypatch.setattr(trioctl, "worker_worktrees", wt)
    return tmp_path, wt, trioctl


def _plan(slices: list[tuple[str, str]]) -> str:
    rows = "".join(
        f"  - id: {sid}\n    writes: [{write}]\n    reads: []\n    status: planned\n"
        f'    accepts: ["{sid} works"]\n'
        for sid, write in slices
    )
    return (
        "# PLAN\n\n## Verification standard\n\nmode: test-first\n\n"
        "full_check: true\n\n```yaml\nslices:\n" + rows + "```\n"
    )


BRIEF = (
    "# Task {sid}\n\nImplement {sid}.\n\n## Targeted check\n\n"
    "python3 -m pytest -q tests\n\n"
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n"
)


def _fixture(tmp_path: Path, loops: dict[str, list[tuple[str, str]]]) -> Path:
    """A product repo (vendored loop core) with one mailbox per loop.

    Mailboxes are gitignored (`loop/`): until r16, two loops share one
    checkout, and a tracked mailbox of a concurrently running loop is a
    `product` path to the other loop's SHIP acceptance (C10, not this fix).
    """
    home = tmp_path / "product"
    (home / "metrics").mkdir(parents=True)
    for src in (REPO_ROOT / "metrics").glob("*.py"):
        shutil.copy(src, home / "metrics" / src.name)
    (home / "README.md").write_text("product\n")
    (home / "src").mkdir()
    (home / "src" / "base.py").write_text("x = 1\n")
    (home / ".gitignore").write_text("loop/\n__pycache__/\n")
    git(home, "init", "-q", "-b", "main")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "init")
    for name, slices in loops.items():
        box = home / "loop" / name
        (box / "briefs").mkdir(parents=True)
        for sid, _write in slices:
            (box / "briefs" / f"{sid}.md").write_text(BRIEF.format(sid=sid))
        (box / "GOAL.md").write_text(f"# Goal {name}\n")
        (box / "STATE.md").write_text("schema: 1\niteration: 0\nstatus: ready\nphase: idle\n")
        (box / "PLAN.md").write_text(_plan(slices))
        (box / "QUEUE.md").write_text("# Queue\n\n```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n")
        (box / "REPORT.md").write_text("# Report\n")
        (box / "VERDICT.md").write_text("")
        (box / "LOG.md").write_text("# Trio loop log\n")
    return home


def _spawn_loop(tmp_path: Path, home: Path, name: str, slices, timeline: Path, *,
                env: dict | None = None, **cfg):
    config = {
        "trioctl": str(SCRIPT), "worktrees": str(MODULE), "home": str(home),
        "mailbox": str(home / "loop" / name), "timeline": str(timeline),
        "slices": [list(s) for s in slices], **cfg,
    }
    path = tmp_path / f"harness-{name}.json"
    path.write_text(json.dumps(config))
    log = open(tmp_path / f"harness-{name}.log", "w")
    return subprocess.Popen(
        [sys.executable, str(HARNESS), str(path)], stdout=log, stderr=subprocess.STDOUT,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **(env or {})), cwd=home,
    )


def _events(timeline: Path) -> list[dict]:
    if not timeline.is_file():
        return []
    return [json.loads(line) for line in timeline.read_text().splitlines() if line.strip()]


def _wait_event(timeline: Path, loop: str, event: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in _events(timeline):
            if ev["loop"] == loop and ev["event"] == event:
                return ev
        time.sleep(0.05)
    raise AssertionError(f"{loop} never reached {event}: {_events(timeline)}")


def _intervals(events: list[dict], loop: str) -> list[tuple[str, float, float]]:
    """Root turns of *loop*: Lead passes, and integration-eval through acceptance."""
    out, open_at = [], {}
    for ev in events:
        if ev["loop"] != loop:
            continue
        if ev["event"] in ("lead-start", "integration-start"):
            open_at[ev["event"]] = ev["t"]
        elif ev["event"] == "lead-end":
            out.append(("lead", open_at.pop("lead-start"), ev["t"]))
        elif ev["event"] == "run-loop-returned" and "integration-start" in open_at:
            # The evaluator's turn is held with its fence through `_finalize_ship`.
            out.append(("integration+acceptance", open_at.pop("integration-start"), ev["t"]))
    return out


def _registry_entries(home: Path) -> list[Path]:
    return sorted((home / ".git" / "trio-worktrees" / "loops").glob("*.json"))


def _harness_log(tmp_path: Path, name: str) -> str:
    try:
        return (tmp_path / f"harness-{name}.log").read_text()
    except OSError:
        return ""


def _fake_cursor_agent(tmp_path: Path, cwd: Path, seconds: float = 600) -> subprocess.Popen:
    """A process whose cmdline names `cursor-agent`, sleeping with *cwd*."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "cursor-agent"
    script.write_text(f"#!/bin/sh\nsleep {seconds}\n")
    script.chmod(0o755)
    return subprocess.Popen(["/bin/sh", str(script)], cwd=cwd, start_new_session=True)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait(timeout=10)


# ------------------------------------------------------------ I1 / I3


def test_i1_i3_two_loops_one_root_turns_never_overlap_and_both_ship(env):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": [("a1", "src/a1.py")], "b": [("b1", "src/b1.py")]})
    timeline = tmp_path / "timeline.jsonl"
    retire_env = {"TRIO_RETIREMENT_WAIT_SECONDS": "20", "TRIO_RETIREMENT_POLL_SECONDS": "0.2"}
    a = _spawn_loop(tmp_path, home, "a", [("a1", "src/a1.py")], timeline,
                    lead_sleep=2.0, retire_delay=1.5, env=retire_env)
    try:
        _wait_event(timeline, "a", "lead-start")
        b = _spawn_loop(tmp_path, home, "b", [("b1", "src/b1.py")], timeline,
                        lead_sleep=1.0, env=retire_env)
        try:
            assert a.wait(timeout=120) == 0, _harness_log(tmp_path, "a")
            assert b.wait(timeout=120) == 0, _harness_log(tmp_path, "b")
        finally:
            if b.poll() is None:
                b.kill()
    finally:
        if a.poll() is None:
            a.kill()
    events = _events(timeline)
    turns_a, turns_b = _intervals(events, "a"), _intervals(events, "b")
    assert [t[0] for t in turns_a] == ["lead", "integration+acceptance"]
    assert [t[0] for t in turns_b] == ["lead", "integration+acceptance"]
    for _ka, sa, ea in turns_a:
        for _kb, sb, eb in turns_b:
            assert ea <= sb or eb <= sa, (turns_a, turns_b)
    # B's Lead waited for A's Lead turn, then proceeded (recorded wait).
    b_lead = next(t for t in turns_b if t[0] == "lead")
    a_lead = next(t for t in turns_a if t[0] == "lead")
    assert b_lead[1] >= a_lead[2]
    driver_b = json.loads((home / "loop" / "b" / ".driver.json").read_text())
    assert driver_b["root_turn_wait_s"] > 0.3
    assert "still run at the aggregate root" not in _harness_log(tmp_path, "b")
    # I3: A's retirement lagged its SHIP; no commit of B's landed between A's
    # pin and A's acceptance (the eval turn is held through `_finalize_ship`).
    for name in ("a", "b"):
        state = (home / "loop" / name / "STATE.md").read_text()
        assert "status: shipped" in state, (name, state, _harness_log(tmp_path, name))
        assert "product tree changed" not in (home / "loop" / name / "LOG.md").read_text()
    b_commits = [e["t"] for e in events if e["loop"] == "b" and e["event"] == "lead-commit"]
    a_accepted = next(e["t"] for e in events if e["loop"] == "a" and e["event"] == "run-loop-returned")
    a_pinned = next(e["t"] for e in events if e["loop"] == "a" and e["event"] == "integration-start")
    assert all(not (a_pinned <= t <= a_accepted) for t in b_commits)
    # Nothing left behind: lock free, registry empty.
    lock = trioctl._RootTurnLock(home, who={})
    assert not lock.held_by_other()
    assert _registry_entries(home) == []


# ------------------------------------------------------------ I2 one-shots


def _registered_live_loop(home: Path, trioctl, mailbox: Path) -> subprocess.Popen:
    """A registry entry of a live 'loop' (a sleeping process's identity)."""
    holder = subprocess.Popen(["sleep", "300"])
    time.sleep(0.05)
    reg = trioctl._LoopRegistration(home, mailbox)
    reg.register()
    entry = json.loads(reg.path.read_text())
    ident = trioctl.worker_worktrees.process_identity(holder.pid)
    entry.update(pid=ident["pid"], pid_start=ident["start"])
    reg.path.write_text(json.dumps(entry))
    return holder


def _fake_cursor_bin(tmp_path: Path) -> Path:
    """`cursor-agent` on PATH for one-shots: lists a model; records its cwd."""
    bindir = tmp_path / "oneshot-bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "cursor-agent"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = models ]; then echo "glm-5.2-max - GLM 5.2 Max"; exit 0; fi
        pwd -P >> {tmp_path / 'oneshot-cwds.txt'}
        cat > /dev/null
        sleep 0.3
        echo scouted
        """))
    script.chmod(0o755)
    return bindir


def _run_oneshot(tmp_path: Path, workspace: Path, *, cwd: Path, extra_env=None, args=()):
    env = dict(os.environ, PATH=f"{_fake_cursor_bin(tmp_path)}:{os.environ['PATH']}",
               PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
    prompt = tmp_path / "task.md"
    prompt.write_text("look around\n")
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), "omnigent", "run", "scout", "--workspace", str(workspace),
         "--prompt-file", str(prompt), *args],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.fixture()
def resolvable(monkeypatch):
    """`run scout` resolves a model from the fake `cursor-agent models`."""
    monkeypatch.setenv("TRIOCTL_CONFIG", str(SCRIPT.with_name("trioctl.example.toml")))


def _oneshot_works(tmp_path, home) -> None:
    proc = _run_oneshot(tmp_path, home, cwd=tmp_path)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert "scouted" in out


# ------------------------------------------------------------ I4 stranger


def _stranger_env(**extra) -> dict:
    return {"TRIO_ROOT_STRANGER_WAIT_S": "1.5", **extra}


@pytest.mark.parametrize("where", ["root", "subdir"])
def test_i4_foreign_cursor_agent_at_root_exits_9_needs_human(env, where):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": [("a1", "src/a1.py")]})
    (home / ".cursor").mkdir()
    user_mcp = json.dumps({"mcpServers": {"user": {"command": "x"}}}) + "\n"
    (home / ".cursor" / "mcp.json").write_text(user_mcp)
    git(home, "add", ".cursor/mcp.json")
    git(home, "commit", "-q", "-m", "user cursor config")
    cwd = home if where == "root" else home / "src"
    stranger = _fake_cursor_agent(tmp_path, cwd)
    timeline = tmp_path / "timeline.jsonl"
    try:
        time.sleep(0.2)
        proc = _spawn_loop(tmp_path, home, "a", [("a1", "src/a1.py")], timeline,
                           env=_stranger_env())
        code = proc.wait(timeout=120)
    finally:
        _kill(stranger)
    log_out = _harness_log(tmp_path, "a")
    assert code == 9, log_out
    box = home / "loop" / "a"
    state = (box / "STATE.md").read_text()
    assert "status: needs_human" in state and "phase: root-occupied" in state
    assert "reason: root-occupied" in state
    assert "status: running" not in state
    log = (box / "LOG.md").read_text().splitlines()[-1]
    for needle in (f"pid {stranger.pid}", f"cwd {cwd.resolve()}", "cursor-agent", "parent ",
                   "started ", "needs_human (root-occupied)"):
        assert needle in log, (needle, log)
    for needle in (f"pid {stranger.pid}", f"cwd {cwd.resolve()}", "resume with: trioctl omnigent loop"):
        assert needle in log_out
    driver = json.loads((box / ".driver.json").read_text())
    assert driver["lead_alive"] is False and driver["eval_alive"] is False
    assert driver["stop"]["reason"] == "root-occupied"
    # Lock released, root .cursor restored byte-identical, registry empty.
    assert not trioctl._RootTurnLock(home, who={}).held_by_other()
    assert (home / ".cursor" / "mcp.json").read_text() == user_mcp
    assert not (home / ".cursor" / "hooks.json").exists()
    assert _registry_entries(home) == []
    # Resume after the stranger is gone clears the stop reason and ships.
    proc = _spawn_loop(tmp_path, home, "a", [("a1", "src/a1.py")], timeline)
    assert proc.wait(timeout=120) == 0, _harness_log(tmp_path, "a")
    state = (box / "STATE.md").read_text()
    assert "status: shipped" in state and "reason:" not in state


def test_stranger_detection_matches_subdirs_not_nested_checkouts(env):
    tmp_path, wt, _trioctl = env
    home = _fixture(tmp_path, {})
    nested = home / "src" / "clone"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    procs = [_fake_cursor_agent(tmp_path, home / "src"), _fake_cursor_agent(tmp_path, nested)]
    other = subprocess.Popen(["sleep", "30"], cwd=home, start_new_session=True)
    try:
        time.sleep(0.3)
        found = wt.cursor_processes_detail(home)
        pids = wt.cursor_processes_at(home)
    finally:
        for p in (*procs, other):
            _kill(p)
    assert [d["pid"] for d in found] == [procs[0].pid]
    detail = found[0]
    assert detail["cwd"] == str((home / "src").resolve())
    assert "cursor-agent" in detail["cmd"] and detail["started"]
    assert detail["parents"] and detail["parents"][0]["pid"] > 1
    assert pids == [procs[0].pid]


def test_stranger_detection_uses_the_injected_process_lister(env, monkeypatch):
    tmp_path, wt, _trioctl = env
    home = _fixture(tmp_path, {})
    table = [
        (4242, str(home / "src"), b"node\0/x/cursor-agent/index.js\0-p"),
        (4243, str(home), b"sleep\0100"),
        (4244, str(tmp_path), b"cursor-agent\0"),
    ]
    monkeypatch.setattr(wt, "_list_processes", lambda: iter(table))
    assert wt.cursor_processes_at(home) == [4242]


def test_release_root_waits_bounded_for_a_stranger_then_raises_root_occupied(
    env, monkeypatch, capsys
):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": []})
    runner = trioctl.OmnigentRunner(repo=home, config={}, interval=0,
                                    isolate_workers={"trioctl": SCRIPT, "worktree_root": "x"})
    monkeypatch.setattr(runner, "ROOT_STRANGER_WAIT", 0.4)
    seen = iter([[77], [77], []])
    monkeypatch.setattr(wt, "cursor_processes_at", lambda root: next(seen, []))
    runner._release_root(home / "loop" / "a")  # left within the bound: proceeds
    monkeypatch.setattr(wt, "cursor_processes_at", lambda root: [os.getpid()])
    t0 = time.monotonic()
    with pytest.raises(trioctl.RootOccupiedError) as info:
        runner._release_root(home / "loop" / "a")
    assert 0.35 <= time.monotonic() - t0 < 5
    assert info.value.pids == [os.getpid()]
    assert f"cursor-agent pid {os.getpid()}" in str(info.value)
    assert info.value.stop["exit"] == 9


# ------------------------------------------------------------ I5 dispatch error


def test_i5_integration_eval_dispatch_raising_sets_state_error_not_running(env):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": [("a1", "src/a1.py")]})
    timeline = tmp_path / "timeline.jsonl"
    proc = _spawn_loop(tmp_path, home, "a", [("a1", "src/a1.py")], timeline,
                       integration="raise")
    code = proc.wait(timeout=120)
    out = _harness_log(tmp_path, "a")
    assert code != 0, out
    assert "injected" in out
    box = home / "loop" / "a"
    state = (box / "STATE.md").read_text()
    assert "status: error" in state and "phase: driver-exception" in state
    assert "status: running" not in state
    assert "integration-eval dispatch blew up" in (box / "LOG.md").read_text().splitlines()[-1]
    driver = json.loads((box / ".driver.json").read_text())
    assert driver["eval_alive"] is False and driver["lead_alive"] is False
    assert json.loads((box / ".session.json").read_text())["eval_alive"] is False
    # Sessions/fences/locks released.
    assert not trioctl._RootTurnLock(home, who={}).held_by_other()
    assert wt.active_fences(home) == []
    assert not (box / ".lock").exists()


# ------------------------------------------------------------ I6 crash


def test_i6_holder_killed_mid_turn_releases_the_lock_within_one_poll(env, tmp_path):
    _tmp, _wt, trioctl = env
    root = tmp_path / "root"
    root.mkdir()
    ready = tmp_path / "ready"
    code = textwrap.dedent(f"""
        import importlib.machinery, importlib.util, pathlib, time
        loader = importlib.machinery.SourceFileLoader("t", {str(SCRIPT)!r})
        spec = importlib.util.spec_from_loader("t", loader)
        m = importlib.util.module_from_spec(spec); loader.exec_module(m)
        lock = m._RootTurnLock({str(root)!r}, who={{"mailbox": "crash", "role": "lead"}})
        lock.acquire(5)
        pathlib.Path({str(ready)!r}).write_text("x")
        time.sleep(300)
    """)
    holder = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.05)
        waiter = trioctl._RootTurnLock(root, who={"mailbox": "next", "role": "lead"})
        assert waiter.held_by_other()
        assert waiter.holder()["mailbox"] == "crash"
        killed = {}

        def kill():
            killed["t"] = time.monotonic()
            holder.kill()

        threading.Timer(0.5, kill).start()
        waiter.acquire(30)
        latency = time.monotonic() - killed["t"]
        waiter.release()
    finally:
        holder.kill()
        holder.wait()
    assert latency < 0.5  # one poll (0.05 s) plus process teardown


def test_root_turn_lock_is_reentrant_per_runner_and_evaluator_turn_is_sticky(env):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": []})
    runner = trioctl.OmnigentRunner(repo=home, config={}, interval=0)
    box = home / "loop" / "a"
    probe = trioctl._RootTurnLock(home, who={})
    runner._enter_root_turn("lead", box, 1, "lead-pass")
    runner._enter_root_turn("evaluator", box, 1, "slice-eval")  # degraded eval joins
    runner._exit_root_turn()
    assert probe.held_by_other()
    runner._exit_root_turn()
    assert not probe.held_by_other()
    # Evaluator: held after its dispatch until the next Lead pass ends.
    runner._enter_root_turn("evaluator", box, 1, "integration-eval")
    runner._keep_root_turn_sticky()
    assert probe.held_by_other()
    runner._enter_root_turn("lead", box, 2, "lead-pass")
    runner._release_sticky_root_turns()
    assert probe.held_by_other()
    runner._exit_root_turn()
    assert not probe.held_by_other()
    runner._enter_root_turn("evaluator", box, 2, "integration-eval")
    runner._keep_root_turn_sticky()
    runner.release_root_turns()
    assert not probe.held_by_other()


def test_root_turn_wait_is_bounded_and_names_the_holder(env, monkeypatch, capsys):
    tmp_path, _wt, trioctl = env
    monkeypatch.setenv("TRIO_ROOT_TURN_WAIT_S", "0.3")
    monkeypatch.setenv("TRIO_ROOT_TURN_PROGRESS_S", "0.1")
    home = _fixture(tmp_path, {"a": [], "b": []})
    other = trioctl._RootTurnLock(home, who={"mailbox": str(home / "loop" / "b"),
                                              "role": "lead", "kind": "lead-pass"})
    other.acquire(1)
    runner = trioctl.OmnigentRunner(repo=home, config={}, interval=0)
    try:
        with pytest.raises(trioctl.RootBusyError, match=r"loop/b lead \(pid \d+"):
            runner._enter_root_turn("lead", home / "loop" / "a", 1, "lead-pass")
    finally:
        other.release()
    err = capsys.readouterr().err
    assert f"root turn at {home.resolve()} held by {home / 'loop' / 'b'} lead" in err
    assert err.count("held by") >= 2  # progress lines


def test_root_turn_wait_over_the_progress_period_logs_once(env, monkeypatch):
    tmp_path, _wt, trioctl = env
    monkeypatch.setenv("TRIO_ROOT_TURN_PROGRESS_S", "0.2")
    home = _fixture(tmp_path, {"a": [], "b": []})
    other = trioctl._RootTurnLock(home, who={"mailbox": "b", "role": "lead"})
    other.acquire(1)
    threading.Timer(0.6, other.release).start()
    runner = trioctl.OmnigentRunner(repo=home, config={}, interval=0)
    box = home / "loop" / "a"
    assert runner._enter_root_turn("lead", box, 3, "lead-pass") is True
    runner._exit_root_turn()
    lines = [ln for ln in (box / "LOG.md").read_text().splitlines() if "root turn:" in ln]
    assert len(lines) == 1 and lines[0].startswith("- iter 3 | loop | root turn: lead-pass waited")
    assert runner.driver_meta["root_turn_wait_s"] >= 0.5


def test_isolated_slice_eval_never_takes_the_root_turn(env, monkeypatch):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": []})
    other = trioctl._RootTurnLock(home, who={"mailbox": "b", "role": "lead"})
    other.acquire(1)
    runner = trioctl.OmnigentRunner(
        repo=home, config={}, interval=0,
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(tmp_path / "wts")},
    )
    runner._client = lambda: object()
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: "p\n")
    seen = []

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        seen.append(workspace)
        dispatch["session_id"] = "s-eval"
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    monkeypatch.setenv("TRIO_ROOT_TURN_WAIT_S", "0.2")
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "A",
           "sha": git(home, "rev-parse", "HEAD")}
    try:
        assert runner.run("evaluator", 1, home / "loop" / "a", ctx) == 0
    finally:
        other.release()
    assert seen and Path(seen[0]) != home


# ------------------------------------------------------------ I7 overlap


# ------------------------------------------------------------ I8 regression


def test_i8_single_loop_ships_and_records_no_root_turn_wait(env):
    tmp_path, wt, trioctl = env
    home = _fixture(tmp_path, {"a": [("a1", "src/a1.py"), ("a2", "src/a2.py")]})
    timeline = tmp_path / "timeline.jsonl"
    proc = _spawn_loop(tmp_path, home, "a", [("a1", "src/a1.py"), ("a2", "src/a2.py")],
                       timeline, lead_sleep=0.1)
    assert proc.wait(timeout=120) == 0, _harness_log(tmp_path, "a")
    box = home / "loop" / "a"
    assert "status: shipped" in (box / "STATE.md").read_text()
    driver = json.loads((box / ".driver.json").read_text())
    assert "root_turn_wait_s" not in driver
    out = _harness_log(tmp_path, "a")
    # The Lead's session ends with its turn (not idling at the root).
    assert "ended finished root lead session sess-a-lead-1-lead-pass" in out

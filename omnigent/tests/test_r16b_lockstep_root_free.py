"""r16b: lockstep mode runs root-free (r16 DESIGN §1.8).

Fake role runner + real git + real loop core + real `trioctl omnigent loop`:
the lockstep Lead and Evaluator both run in the loop's Lead worktree on
`trio/<slug>`, the Evaluator's SHIP retirement commit lands there, and the
driver lands the branch onto the target (ff-only / merge + re-check) exactly
as for open-loop. The root is untouched until the land (except the driver's
own root mailbox `.lock`, eval-r16rc-b M1).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo, snapshot_root


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16b_ls")


def _home(tmp_path: Path, extra: dict | None = None) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": "", **(extra or {})})
    return home


def _state(box: Path) -> dict[str, str]:
    out = {}
    for line in (box / "STATE.md").read_text().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _live(world, spec) -> Path:
    record = world.rf.load_record(world.wt, spec["home"], spec["slug"])
    return Path(record["live_mailbox"])


def _kinds(world, role):
    return [e for e in world.events if e["kind"] == role]


def test_lockstep_runs_in_the_lead_worktree_and_lands_ff(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/ls", [{"id": "ls-a", "write": "src/ls_a.py"},
                                            {"id": "ls-b", "write": "docs/ls_b.md"}],
                          lockstep=True)
    before = snapshot_root(home)
    reflog_before = git(home, "reflog", "show", "--format=%H %gs", "main").splitlines()
    samples: list[dict] = []
    registry: list[dict] = []

    def sample(w, sp, runner, ctx, workspace, box, prompt, iteration):
        samples.append(snapshot_root(home))
        loops = home / ".git" / "trio-worktrees" / "loops"
        registry.extend(json.loads(p.read_text()) for p in loops.glob("*.json"))
        return False

    world.hooks["lead"] = sample
    world.hooks["evaluator"] = sample
    assert world.run_loop(spec) == 0
    leads, evals = _kinds(world, "lead"), _kinds(world, "evaluator")
    assert len(leads) == 1 and len(evals) == 1
    lead_wt = Path(leads[0]["workspace"])
    assert lead_wt.name == "lead-loop--ls" and not lead_wt.is_relative_to(home)
    assert Path(evals[0]["workspace"]) == lead_wt  # Evaluator in the Lead worktree
    assert Path(leads[0]["mailbox"]) == lead_wt / "loop/ls"
    for event in leads + evals:
        assert "ROOT-FREE (this lockstep loop runs in its own Lead worktree)" in event["prompt"]
        assert f"`{lead_wt}`" in event["prompt"] and "trio/loop--ls" in event["prompt"]
    assert "LOCKSTEP CONTEXT:" in evals[0]["prompt"]
    assert all(s == before for s in samples), samples
    assert registry and {r["mode"] for r in registry} == {"lockstep"}
    assert registry[0]["lead_worktree"] == str(lead_wt)
    # One fast-forward of main carrying the SHIP retirement and land commits.
    reflog = git(home, "reflog", "show", "--format=%H %gs", "main").splitlines()
    assert len(reflog) == len(reflog_before) + 1 and "fast-forward" in reflog[0].lower()
    log = git(home, "log", "--format=%s", "main").splitlines()
    assert log[:2] == ["loop: land loop/ls (iteration 1)", "loop: iteration 1 — SHIP"]
    assert "slice(ls-a): ls-a work" in log and "slice(ls-b): ls-b work" in log
    state = _state(home / "loop/ls")
    assert state["status"] == "shipped" and state["phase"] == "landed"
    assert state["landed"] == git(home, "rev-parse", "main~1")
    assert (home / "src/ls_a.py").is_file() and (home / "docs/ls_b.md").is_file()
    assert git(home, "status", "--porcelain") == ""
    assert git(home, "branch", "--list", "trio/*") == ""
    assert not lead_wt.exists()
    assert not (home / "loop/ls/.lock").exists()


def test_lockstep_user_dirty_overlap_is_needs_land_then_land_retries(world, tmp_path, capsys):
    home = _home(tmp_path, {"docs/guide.md": "v0\n"})
    spec = world.add_loop(home, "loop/ud", [{"id": "ud-a", "write": "docs/guide.md",
                                              "content": "loop edit\n"}], lockstep=True)
    (home / "docs/guide.md").write_text("user edit\n")  # uncommitted, overlapping
    main_before = git(home, "rev-parse", "main")
    capsys.readouterr()
    assert world.run_loop(spec) == 8
    err = capsys.readouterr().err
    assert "docs/guide.md" in err
    live = _live(world, spec)
    state = _state(live)
    assert state["status"] == "needs_land" and state["phase"] == "land-blocked"
    assert (home / "docs/guide.md").read_text() == "user edit\n"
    assert git(home, "rev-parse", "main") == main_before
    dispatched = len(world.events)
    # The user moves their edit away; `land` completes without any dispatch.
    (home / "docs/guide.md").write_text("v0\n")
    assert world.run_land(spec) == 0
    assert len(world.events) == dispatched
    assert (home / "docs/guide.md").read_text() == "loop edit\n"
    assert _state(home / "loop/ud")["phase"] == "landed"


def test_lockstep_target_moved_under_its_writes_reverifies_then_lands(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/rv", [{"id": "rv-a", "write": "src/rv.py",
                                              "reads": ["src/shared.py"]}], lockstep=True)
    moved = {}

    def move_target(w, sp, runner, ctx, workspace, box, prompt, iteration):
        if not moved:
            # Someone commits to main under this loop's reads: meanwhile.
            (home / "src/shared.py").write_text("shared\n")
            git(home, "add", "src/shared.py")
            git(home, "commit", "-q", "-m", "user: shared")
            moved["sha"] = git(home, "rev-parse", "HEAD")
        return False

    world.hooks["evaluator"] = move_target
    assert world.run_loop(spec) == 0
    evals = _kinds(world, "evaluator")
    assert len(evals) == 2, [e["ctx"] for e in evals]  # the SHIP, then the re-check
    assert evals[1]["ctx"]["pinned_sha"] != evals[0]["ctx"]["pinned_sha"]
    assert len(_kinds(world, "lead")) == 1  # never a new Lead pass
    main = git(home, "log", "--format=%s", "main").splitlines()
    assert main[0] == "loop: land loop/rv (iteration 1)"
    assert any(s.startswith("land: merge main@") for s in main)
    assert git(home, "merge-base", "--is-ancestor", moved["sha"], "main", check=False) == ""
    log = (home / "loop/rv/LOG.md").read_text()
    assert "re-verifying with a new evaluator (round 1/2)" in log
    assert (home / "src/shared.py").is_file() and (home / "src/rv.py").is_file()


def test_lockstep_crash_after_ship_lands_on_resume_without_dispatch(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/cr", [{"id": "cr-a", "write": "src/cr.py"}],
                          lockstep=True)
    t = world.trioctl
    real = t._run_lockstep_root_free

    def crash_before_land(loop_core, mailbox, args, runner, repo, kwargs, land):
        code = loop_core.run_loop(mailbox, args.max_iterations, runner, repo=repo)
        assert code == 0
        raise KeyboardInterrupt  # the driver died between the SHIP and the land

    monkeypatch.setattr(t, "_run_lockstep_root_free", crash_before_land)
    assert world.run_loop(spec) == 130
    live = _live(world, spec)
    assert _state(live)["status"] == "shipped" and "landed" not in _state(live)
    dispatched = len(world.events)
    monkeypatch.setattr(t, "_run_lockstep_root_free", real)
    assert world.run_loop(spec) == 0
    assert len(world.events) == dispatched
    assert _state(home / "loop/cr")["phase"] == "landed"


def test_lockstep_resume_reattaches_without_reseeding(world, tmp_path, monkeypatch):
    """R7 (lockstep): interrupted after the Lead pass, resume re-attaches."""
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/r7", [{"id": "r7-a", "write": "src/r7.py"}],
                          lockstep=True)
    stop = {"once": True}

    def interrupt(w, sp, runner, ctx, workspace, box, prompt, iteration):
        if stop.pop("once", None):
            raise KeyboardInterrupt
        return False

    world.hooks["evaluator"] = interrupt
    assert world.run_loop(spec) == 130
    record = world.rf.load_record(world.wt, home, spec["slug"])
    seed = record["seed"]
    lead_before = git(Path(record["path"]), "rev-parse", "HEAD")
    assert world.run_loop(spec) == 0
    assert len(_kinds(world, "lead")) == 1  # Lead-done: straight to the evaluator
    main = git(home, "log", "--format=%H %s", "main").splitlines()
    seeds = [line for line in main if " loop: seed loop/r7" in line]
    assert len(seeds) == 1 and seeds[0].startswith(seed)
    assert git(home, "merge-base", "--is-ancestor", lead_before, "main", check=False) == ""


def test_two_lockstep_loops_and_one_open_loop_on_one_root_all_land(world, tmp_path):
    home = _home(tmp_path)
    a = world.add_loop(home, "loop/la", [{"id": "la-1", "write": "src/la.py"}], lockstep=True)
    b = world.add_loop(home, "loop/lb", [{"id": "lb-1", "write": "docs/lb.md"}], lockstep=True)
    c = world.add_loop(home, "loop/oc", [{"id": "oc-1", "write": "src/oc.py"}])
    codes: dict[str, int] = {}
    errors: list[BaseException] = []

    def run(spec):
        try:
            codes[spec["rel"]] = world.run_loop(spec)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,)) for s in (a, b, c)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(240)
    assert not errors, errors
    assert codes == {"loop/la": 0, "loop/lb": 0, "loop/oc": 0}, codes
    for rel in ("loop/la", "loop/lb", "loop/oc"):
        assert _state(home / rel)["phase"] == "landed", rel
    for path in ("src/la.py", "docs/lb.md", "src/oc.py"):
        assert (home / path).is_file()
    assert git(home, "status", "--porcelain") == ""
    assert git(home, "branch", "--list", "trio/*") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    workspaces = {e["loop"]: {Path(x["workspace"]).name for x in world.events
                              if x["loop"] == e["loop"] and x["kind"] in ("lead", "evaluator")}
                  for e in world.events}
    assert workspaces["loop/la"] == {"lead-loop--la"}
    assert workspaces["loop/lb"] == {"lead-loop--lb"}


def test_lockstep_with_explicit_isolated_builders(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/li", [{"id": "li-a", "write": "src/li.py"}],
                          lockstep=True)
    assert world.run_loop(spec, "--isolate-workers") == 0
    main = git(home, "log", "--format=%s", "main").splitlines()
    assert any(s.startswith("merge(worker): li-a-") for s in main), main
    assert (home / "src/li.py").is_file()


def test_shipped_root_mailbox_is_a_no_op(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/sh", [{"id": "sh-a", "write": "src/sh.py"}],
                          lockstep=True)
    (spec["root_box"] / "STATE.md").write_text(
        "schema: 1\niteration: 3\nmax_iterations: 5\nstatus: shipped\nphase: done\n"
    )
    capsys.readouterr()
    assert world.run_loop(spec) == 0
    assert "already shipped" in capsys.readouterr().err
    assert world.events == []
    assert world.rf.load_record(world.wt, home, spec["slug"]) is None
    assert git(home, "branch", "--list", "trio/*") == ""


@pytest.mark.parametrize("lockstep", [True, False])
def test_root_bound_is_removed_with_a_clear_refusal(world, tmp_path, capsys, lockstep):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/rb", [{"id": "rb-a", "write": "src/rb.py"}],
                          lockstep=lockstep)
    before = snapshot_root(home)
    capsys.readouterr()
    assert world.run_loop(spec, "--root-bound") == 2
    err = capsys.readouterr().err
    assert "root-bound mode was removed in r16b" in err
    assert "trioctl omnigent land" in err and "abandon" in err
    assert snapshot_root(home) == before and world.events == []


def test_open_loop_without_isolation_is_refused(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/ni", [{"id": "ni-a", "write": "src/ni.py"}])
    capsys.readouterr()
    assert world.run_loop(spec, "--no-isolate-workers") == 2
    err = capsys.readouterr().err
    assert "needs isolated builders" in err and "--no-isolate-workers" in err
    assert world.events == []
    assert git(home, "branch", "--list", "trio/*") == ""
    # Lockstep keeps running without isolation (its default).
    ls = world.add_loop(home, "loop/nl", [{"id": "nl-a", "write": "src/nl.py"}],
                        lockstep=True)
    assert world.run_loop(ls, "--no-isolate-workers") == 0


def test_lockstep_finished_sessions_end_with_their_turn(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/se", [{"id": "se-a", "write": "src/se.py"}],
                          lockstep=True)
    ended: list[list[str]] = []
    monkeypatch.setattr(
        world.trioctl, "_prune_broker_sessions",
        lambda client, mailbox, **kw: ended.append(list(kw.get("session_ids") or []))
        or {"deleted": len(kw.get("session_ids") or []), "archived": 0,
            "skipped_running": 0, "failed": 0},
    )
    assert world.run_loop(spec) == 0
    flat = [sid for batch in ended for sid in batch]
    assert any("-lead-" in sid for sid in flat) and any("-evaluator-" in sid for sid in flat)


@pytest.mark.parametrize("isolated", [False, True])
def test_lockstep_fixture_b_multi_repo_aggregates_and_land_home_last(world, tmp_path, isolated):
    """Fixture B (clones nested in the mailbox) under root-free lockstep."""
    home = tmp_path / "home"
    init_repo(home, "main", {
        ".gitignore": "loop/x/app-backend/\nloop/x/app-frontend/\n",
        "README.md": "home\n", "docs/index.md": "docs\n",
    })
    box = home / "loop" / "x"
    init_repo(box / "app-backend", "dev", {"app/core.py": "x = 1\n"}, metrics=False)
    init_repo(box / "app-frontend", "feat/ui", {"src/app.js": "//\n"}, metrics=False)
    repos_block = (
        "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n"
        "  - name: app-frontend\n    path: loop/x/app-frontend\n    base: feat/ui\n"
    )
    spec = world.add_loop(
        home, "loop/x", [
            {"id": "be-a", "repo": "app-backend", "write": "app/a.py"},
            {"id": "fe-b", "repo": "app-frontend", "write": "src/b.js"},
            {"id": "home-c", "repo": "home", "write": "docs/c.md"},
        ],
        repos_block=repos_block, lockstep=True,
        full_check="full_check:\n  app-backend: true\n  app-frontend: true\n  home: true",
    )
    seen: dict = {}

    def evaluator(w, sp, runner, ctx, workspace, mailbox, prompt, iteration):
        record = w.rf.load_record(w.wt, home, "loop--x")
        seen["workspace"] = workspace
        seen["prompt"] = prompt
        for name, info in record["repos"].items():
            agg = Path(info["path"])
            assert agg == Path(record["path"]) / "loop" / "x" / name
            assert git(agg, "symbolic-ref", "--short", "HEAD") == "trio/loop--x"
            assert ctx["pins"][name] == git(agg, "rev-parse", "HEAD")
        return False

    world.hooks["evaluator"] = evaluator
    extra = ("--isolate-workers",) if isolated else ()
    assert world.run_loop(spec, *extra) == 0
    record = world.rf.load_record(world.wt, home, "loop--x")
    assert Path(seen["workspace"]) == Path(record["path"])
    assert "ROOT-FREE (this lockstep loop" in seen["prompt"] and "MULTI-REPO" in seen["prompt"]
    assert "in its aggregate (on the loop branch), make ONE" in seen["prompt"]
    for name, branch, path in (("app-backend", "dev", "app/a.py"),
                               ("app-frontend", "feat/ui", "src/b.js")):
        clone = box / name
        subjects = git(clone, "log", "--format=%s", branch).splitlines()
        assert subjects[0] == "loop: iteration 1 — SHIP (x)", subjects
        assert (clone / path).is_file()
        assert git(clone, "branch", "--list", "trio/*") == ""
    assert (home / "docs/c.md").is_file()
    log = (home / "loop/x/LOG.md").read_text().splitlines()
    landed = [line for line in log if "| loop | landed " in line]
    assert [("app-backend" in l, "app-frontend" in l) for l in landed[:2]] in (
        [(True, False), (False, True)], [(False, True), (True, False)])
    assert landed[-1].endswith("onto main") or "onto main (" in landed[-1]
    assert _state(home / "loop/x")["phase"] == "landed"


def test_lockstep_mailbox_interrupted_at_the_root_continues_root_free(world, tmp_path):
    """Migration: a pre-r16b lockstep run stopped at the root after its Lead
    pass (product committed on main, STATE lead-done) restarts root-free."""
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/mg", [{"id": "mg-a", "write": "src/mg.py",
                                              "status": "complete"}], lockstep=True)
    spec["slices"][0]["done"] = True
    (home / "src/mg.py").write_text("# mg\n")
    git(home, "add", "src/mg.py")
    git(home, "commit", "-q", "-m", "slice(mg-a): mg-a work")
    box = spec["root_box"]
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 2\nmax_iterations: 5\nstatus: running\nmission: r16\n"
        "phase: lead-done\n"
    )
    with (box / "LOG.md").open("a") as fh:
        fh.write("- iter 2 | lead | old root-bound pass; gate: PASS\n")
    assert world.run_loop(spec) == 0
    assert _kinds(world, "lead") == []  # straight to the Evaluator
    evals = _kinds(world, "evaluator")
    assert len(evals) == 1 and Path(evals[0]["workspace"]).name == "lead-loop--mg"
    state = _state(home / "loop/mg")
    assert state["iteration"] == "2" and state["phase"] == "landed"
    assert git(home, "log", "-1", "--format=%s", "main") == "loop: land loop/mg (iteration 2)"


@pytest.mark.parametrize("first", ["native", "lockstep"])
def test_lockstep_root_lock_excludes_a_native_driver_both_ways(world, tmp_path, first):
    import os
    import subprocess
    import sys
    from r16_harness import REPO_ROOT

    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/nl", [{"id": "nl-a", "write": "src/nl.py"}],
                          lockstep=True)
    box = spec["root_box"]
    core = str(REPO_ROOT / "metrics" / "trio_loop.py")
    code = (
        "import sys, time; sys.dont_write_bytecode = True\n"
        "import importlib.machinery, importlib.util\n"
        f"l = importlib.machinery.SourceFileLoader('c', {core!r})\n"
        "s = importlib.util.spec_from_loader('c', l); m = importlib.util.module_from_spec(s)\n"
        "l.exec_module(m)\n"
        f"lock = m._acquire_lock({str(box)!r}); print('held' if lock else 'refused', flush=True)\n"
        "time.sleep(60 if lock else 0)\n"
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if first == "native":
        holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  text=True, env=env)
        try:
            assert holder.stdout.readline().strip() == "held"
            assert world.run_loop(spec) == 5
            assert world.events == []
        finally:
            holder.kill()
            holder.wait()
        return
    seen = {}

    def probe(w, sp, runner, ctx, workspace, mailbox, prompt, iteration):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env)
        seen["native"] = out.stdout.strip()
        return False

    world.hooks["evaluator"] = probe
    assert world.run_loop(spec) == 0
    assert seen["native"] == "refused"

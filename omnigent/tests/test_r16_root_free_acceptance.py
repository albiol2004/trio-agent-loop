"""r16a acceptance (DESIGN.md 4.3 R1, R3-R10 + refusals): fake runner, real
git, real loop core, real `trioctl omnigent loop|land|abandon|status`."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import threading
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo, snapshot_root

MCP = '{\n  "mcpServers": {\n    "docs": {"command": "echo", "args": ["docs"]}\n  }\n}\n'


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, tag="r16acc")


def _home(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    home = tmp_path / "home"
    files = {"README.md": "home\n", "src/__init__.py": ""}
    files.update(extra or {})
    init_repo(home, "main", files)
    return home


def _record(world: World, home: Path, spec: dict) -> dict | None:
    return world.rf.load_record(world.wt, home, spec["slug"])


def _live(world: World, home: Path, spec: dict) -> Path:
    record = _record(world, home, spec)
    if record and Path(record["live_mailbox"]).is_dir():
        return Path(record["live_mailbox"])
    return spec["root_box"]


def _dump(world: World, home: Path, *specs: dict) -> str:
    out = []
    for spec in specs:
        box = _live(world, home, spec)
        for name in ("STATE.md", "LOG.md"):
            f = box / name
            out.append(f"== {spec['rel']} {name} ({box})\n"
                       + (f.read_text() if f.is_file() else "<missing>"))
    return "\n".join(out)


def _state(box: Path) -> dict[str, str]:
    out = {}
    for line in (box / "STATE.md").read_text().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip().lstrip("- ").strip().lower()] = value.strip()
    return out


def _branches(repo: Path) -> list[str]:
    return [b.strip("* +").strip() for b in git(repo, "branch", "--list").splitlines()]


def _worktrees(repo: Path) -> list[str]:
    return [ln.split(" ", 1)[1] for ln in git(repo, "worktree", "list", "--porcelain").splitlines()
            if ln.startswith("worktree ")]


def _kinds(world: World, kind: str, loop: str | None = None) -> list[dict]:
    return [e for e in world.events if e["kind"] == kind and (loop is None or e["loop"] == loop)]


# ---------------------------------------------------------------- R1


def test_r1_root_untouched_until_one_fast_forward_land(world, tmp_path):
    home = _home(tmp_path, {".cursor/mcp.json": MCP})
    spec = world.add_loop(home, "loop/r1", [{"id": "r1-one", "write": "src/r1.py"},
                                            {"id": "r1-two", "write": "docs/r1.md"}])
    before = snapshot_root(home)
    reflog_before = git(home, "reflog", "show", "--format=%H %gs", "main").splitlines()
    samples: list[dict] = []

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        samples.append(snapshot_root(home))
        return False

    world.hooks["integration-eval"] = integration
    code = world.run_loop(spec)
    assert code == 0, _dump(world, home, spec)
    assert samples, "the integration-eval never ran"
    for sample in samples:
        assert sample == before  # status, index mtime, HEAD, .cursor bytes
    assert world.events and all(Path(e["workspace"]) != home for e in world.events)
    assert all(not Path(e["workspace"]).is_relative_to(home) for e in world.events)
    reflog = git(home, "reflog", "show", "--format=%H %gs", "main").splitlines()
    assert len(reflog) == len(reflog_before) + 1, reflog
    new_sha, _, message = reflog[0].partition(" ")
    assert "fast-forward" in message.lower(), reflog[0]
    assert new_sha == git(home, "rev-parse", "main")
    assert (home / ".cursor/mcp.json").read_text() == MCP
    assert snapshot_root(home)["cursor"] == before["cursor"]
    assert (home / "src/r1.py").is_file() and (home / "docs/r1.md").is_file()


# ---------------------------------------------------------------- R3


def test_r3_conflicting_loops_first_lands_second_needs_land_then_resolved(world, tmp_path):
    home = _home(tmp_path)
    a = world.add_loop(home, "loop/ca", [{"id": "ca-one", "write": "src/shared.py",
                                          "content": "VALUE = 'a'\n"}])
    b = world.add_loop(home, "loop/cb", [{"id": "cb-one", "write": "src/shared.py",
                                          "content": "VALUE = 'b'\n"}])
    codes: dict[str, int] = {}

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        # Loop B was forked from main before A existed; A runs and lands
        # completely while B's first integration-eval is being dispatched.
        if s is b and "ca" not in codes:
            codes["ca"] = w.run_loop(a)
        return False

    world.hooks["integration-eval"] = integration
    codes["cb"] = world.run_loop(b)
    assert codes["ca"] == 0, _dump(world, home, a)
    assert codes["cb"] == 8, _dump(world, home, b)
    assert (home / "src/shared.py").read_text() == "VALUE = 'a'\n"
    record = _record(world, home, b)
    live, lead = Path(record["live_mailbox"]), Path(record["path"])
    state = _state(live)
    assert state["status"] == "needs_land" and state["phase"] == "land-conflict", state
    log = (live / "LOG.md").read_text()
    assert "src/shared.py" in log and "needs_land (land-conflict)" in log
    assert not (Path(git(lead, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").exists()
    assert git(lead, "status", "--porcelain", "--", "src") == ""  # the merge was aborted
    # A human resolves in the Lead worktree.
    evals_before = len(_kinds(world, "integration-eval", "loop/cb"))
    pins_before = {e["ctx"].get("pinned_sha") for e in _kinds(world, "integration-eval", "loop/cb")}
    leads_before = len(_kinds(world, "lead-pass"))
    merge = git(lead, "merge", "--no-edit", "main", check=False)
    assert "CONFLICT" in merge or git(lead, "diff", "--name-only", "--diff-filter=U")
    (lead / "src/shared.py").write_text("VALUE = 'a+b'\n")
    git(lead, "add", "src/shared.py")
    git(lead, "commit", "-q", "--no-edit")
    resolved = git(lead, "rev-parse", "HEAD")
    code = world.run_land(b)
    assert code == 0, _dump(world, home, b)
    evals = _kinds(world, "integration-eval", "loop/cb")
    assert len(evals) == evals_before + 1, "land did not re-verify with an integration-eval"
    new_pin = evals[-1]["ctx"].get("pinned_sha")
    assert new_pin not in pins_before and git(home, "merge-base", "--is-ancestor", resolved,
                                             new_pin) == ""
    assert len(_kinds(world, "lead-pass")) == leads_before  # land never dispatches a Lead
    assert (home / "src/shared.py").read_text() == "VALUE = 'a+b'\n"
    git(home, "merge-base", "--is-ancestor", resolved, "main")
    root_log = (home / "loop/cb/LOG.md").read_text()
    assert "re-verifying with a new integration-eval" in root_log
    assert "status: shipped" in (home / "loop/cb/STATE.md").read_text()
    assert "trio/" not in git(home, "branch", "--list")
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == ""


# ---------------------------------------------------------------- R4


def _r4_loops(world: World, home: Path):
    a = world.add_loop(home, "loop/ra", [{"id": "ra-one", "write": "src/a.py"}])
    b = world.add_loop(home, "loop/rb", [{"id": "rb-one", "write": "src/b.py",
                                          "reads": ["src/a.py"]}])
    return a, b


def test_r4_incoming_commit_on_reads_reverifies_and_iterate_stops_land(world, tmp_path):
    home = _home(tmp_path)
    a, b = _r4_loops(world, home)
    codes: dict[str, int] = {}
    seen: list[str] = []

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        if s is not b:
            return False
        seen.append(ctx["pinned_sha"])
        if len(seen) == 1:
            codes["ra"] = w.run_loop(a)  # A lands src/a.py (B's reads) meanwhile
            return False
        # The re-verification finds a problem: ITERATE (then, since this
        # scripted Lead has nothing left to change, BLOCKED ends the run).
        w.integration(s, runner, ctx, workspace, box,
                      verdict="ITERATE" if len(seen) == 2 else "BLOCKED")
        return True

    world.hooks["integration-eval"] = integration
    codes["rb"] = world.run_loop(b)
    assert codes["ra"] == 0
    assert codes["rb"] not in (0, 8), _dump(world, home, b)
    assert len(seen) >= 2, _dump(world, home, b)
    live = _live(world, home, b)
    log = (live / "LOG.md").read_text()
    lines = log.splitlines()
    reverify = [i for i, ln in enumerate(lines) if "re-verifying with a new integration-eval" in ln]
    assert reverify and "src/a.py" in lines[reverify[0]], log  # names the touched read
    assert "full_check PASS" not in log  # not just the deterministic check
    iterate = [i for i, ln in enumerate(lines) if "VERDICT: ITERATE" in ln]
    assert iterate and iterate[0] > reverify[0], log
    assert "landed trio/" not in log
    # The re-verification pinned the merge of the moved target, not the old tip.
    assert seen[1] != seen[0]
    git(home, "merge-base", "--is-ancestor", git(home, "rev-parse", "main"), seen[1])
    assert not (home / "src/b.py").exists()
    assert git(home, "cat-file", "-e", "main:src/b.py", check=False) == ""
    assert not any(s.startswith("slice(rb-one)")
                   for s in git(home, "log", "--format=%s", "main").splitlines())
    assert _state(live)["status"] != "shipped"


def test_r4_iterate_with_idle_lead_terminates(world, tmp_path):
    home = _home(tmp_path)
    a, b = _r4_loops(world, home)
    seen: list[str] = []
    bound = 8

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        if s is not b:
            return False
        seen.append(ctx["pinned_sha"])
        if len(seen) == 1:
            w.run_loop(a)
            return False
        # ITERATE every time; escape hatch after `bound` evals so the test ends.
        w.integration(s, runner, ctx, workspace, box,
                      verdict="ITERATE" if len(seen) <= bound else "BLOCKED")
        return True

    world.hooks["integration-eval"] = integration
    code = world.run_loop(b)
    assert len(seen) <= bound, f"{len(seen)} integration-evals for one idle Lead"
    assert code == 3
    assert _state(_live(world, home, b))["status"] == "error"


# ---------------------------------------------------------------- R5


def test_r5_unrelated_user_files_preserved_by_successful_land(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/r5", [{"id": "r5-one", "write": "src/r5.py"}])
    (home / "README.md").write_text("home, edited by the user\n")
    (home / "scratch").mkdir()
    (home / "scratch/notes.txt").write_text("mine\n")
    code = world.run_loop(spec)
    assert code == 0, _dump(world, home, spec)
    assert (home / "src/r5.py").read_text() == "# r5-one\n"
    assert (home / "README.md").read_text() == "home, edited by the user\n"
    assert (home / "scratch/notes.txt").read_text() == "mine\n"
    status = git(home, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    assert sorted(line.strip() for line in status) == ["?? scratch/notes.txt", "M README.md"], status


# ---------------------------------------------------------------- R6


def _side_commit(home: Path) -> str:
    """A commit on `side` (child of main) that touches no loop path."""
    git(home, "switch", "-q", "-c", "side")
    (home / "other.txt").write_text("from elsewhere\n")
    git(home, "add", "other.txt")
    git(home, "commit", "-q", "-m", "side: other.txt")
    sha = git(home, "rev-parse", "HEAD")
    git(home, "switch", "-q", "-c", "other", "main")
    return sha


def test_r6_target_checked_out_nowhere_lands_by_cas_update_ref(world, tmp_path):
    home = _home(tmp_path)
    git(home, "switch", "-q", "-c", "other")
    spec = world.add_loop(home, "loop/r6", [{"id": "r6-one", "write": "src/r6.py"}])
    before = snapshot_root(home)
    main_before = git(home, "rev-parse", "main")
    code = world.run_loop(spec, "--target", "main")
    assert code == 0, _dump(world, home, spec)
    assert git(home, "symbolic-ref", "--short", "HEAD") == "other"
    assert git(home, "rev-parse", "HEAD") == before["head"]
    assert git(home, "status", "--porcelain=v1", "--untracked-files=no") == ""
    assert not (home / "src/r6.py").exists()
    git(home, "merge-base", "--is-ancestor", main_before, "main")
    assert git(home, "show", "main:src/r6.py") == "# r6-one"
    assert git(home, "log", "-1", "--format=%s", "main") == "loop: land loop/r6 (iteration 1)"
    assert "trio/" not in git(home, "branch", "--list")


def test_r6_concurrent_ref_move_fails_cas_then_merges_and_lands(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    side = _side_commit(home)  # root now on `other`; main unchanged
    spec = world.add_loop(home, "loop/r6b", [{"id": "r6b-one", "write": "src/r6b.py"}])
    real = world.rf.checkout_of
    moved: list[str] = []

    def checkout_of(wt, repo, branch):
        # Between _land_one's read of main and its CAS update-ref, someone
        # else advances main (a commit made elsewhere).
        if branch == "main" and not moved:
            old = git(home, "rev-parse", "main")
            git(home, "update-ref", "refs/heads/main", side, old)
            moved.append(old)
        return real(wt, repo, branch)

    monkeypatch.setattr(world.rf, "checkout_of", checkout_of)
    code = world.run_loop(spec, "--target", "main")
    assert moved, "the land never reached phase 2"
    assert code == 0, _dump(world, home, spec)
    git(home, "merge-base", "--is-ancestor", side, "main")
    assert git(home, "show", "main:src/r6b.py") == "# r6b-one"
    assert git(home, "show", "main:other.txt") == "from elsewhere"
    log = git(home, "show", "main:loop/r6b/LOG.md")
    assert "merged home:main@" in log and "full_check PASS" in log, log
    assert git(home, "symbolic-ref", "--short", "HEAD") == "other"


# ---------------------------------------------------------------- R7


def _lead_appending(w: World, spec: dict, runner, box: Path, iteration: int) -> None:
    """The harness Lead pass, on a QUEUE.md that already has retired entries
    (the scripted Lead inserts into an empty `retired:` block only)."""
    queue = box / "QUEUE.md"
    text = queue.read_text()
    match = re.search(r"retired:\n(.*?)```", text, re.S)
    prior = match.group(1) if match else ""
    if prior:
        queue.write_text(text.replace(match.group(0), "retired:\n```", 1))
    w.lead(spec, runner, box, iteration)
    if prior:
        text = queue.read_text()
        queue.write_text(text.replace("retired:\n", "retired:\n" + prior, 1))


def _restart_setup(world: World, home: Path):
    slices = [{"id": f"r7-{n}", "write": f"src/r7_{n}.py"} for n in ("one", "two", "three")]
    spec = world.add_loop(home, "loop/r7", slices)
    release = threading.Event()
    passes = [0]

    def lead_hook(w, s, runner, ctx, workspace, box, prompt, iteration):
        if release.is_set():
            _lead_appending(w, s, runner, box, iteration)
            return True
        passes[0] += 1
        if passes[0] == 1:
            slices[2]["done"] = True  # retire two of three this pass
            try:
                w.lead(s, runner, box, iteration)
            finally:
                slices[2]["done"] = False
            lead = Path(runner.repo)
            git(lead, "add", "-A", "--", "loop/r7")
            git(lead, "commit", "-q", "-m", f"loop: iteration {iteration}")
            return True
        # Second pass: the driver is stopped (SIGTERM) after two retirements.
        os.kill(os.getpid(), signal.SIGTERM)
        release.wait(120)
        raise RuntimeError("interrupted run's Lead thread ends")

    world.hooks["lead-pass"] = lead_hook
    return spec, slices, release


@pytest.mark.parametrize("vanish", [False, True], ids=["reattach", "recreate"])
def test_r7_restart_reattaches_without_reseed_and_lands(world, tmp_path, capsys, vanish):
    home = _home(tmp_path)
    spec, slices, release = _restart_setup(world, home)
    try:
        code = world.run_loop(spec)
        assert code == 130, _dump(world, home, spec)
        record = _record(world, home, spec)
        assert record and record["state"] == "active"
        lead, branch = Path(record["path"]), record["branch"]
        assert lead.is_dir() and branch in _branches(home)
        assert not world.rf.registry_file(world.wt, home, spec["slug"]).exists()
        retired = re.findall(r"slice: (r7-\w+)", (Path(record["live_mailbox"]) / "QUEUE.md").read_text())
        assert sorted(retired) == ["r7-one", "r7-two"]
        seeds = git(home, "log", "--format=%s", branch).splitlines().count("loop: seed loop/r7")
        assert seeds == 1
        if vanish:
            shutil.rmtree(lead)
            assert str(lead) in _worktrees(home)  # git still has it registered
        capsys.readouterr()
        release.set()
        code = world.run_loop(spec)
        err = capsys.readouterr().err
        assert code == 0, _dump(world, home, spec) + err
        assert "re-attached" in err and str(lead) in err
        subjects = git(home, "log", "--format=%s", "main").splitlines()
        assert subjects.count("loop: seed loop/r7") == 1, subjects
        for sl in slices:
            assert (home / sl["write"]).is_file()
        assert "loop: land loop/r7 (iteration" in subjects[0]
        assert branch not in _branches(home) and not lead.exists()
        after = _record(world, home, spec)
        assert after["path"] == str(lead) and after["branch"] == branch
    finally:
        release.set()


# ---------------------------------------------------------------- R8


def test_r8_integration_eval_detached_at_pin_and_root_scratch_does_not_block(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/r8", [{"id": "r8-one", "write": "src/r8.py"}])
    (home / "scratch.txt").write_text("user scratch\n")
    seen: dict = {}

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        lead = Path(runner._root_free["lead"])
        seen["lead"], seen["workspace"] = lead, workspace
        assert workspace != lead and workspace != home
        assert git(workspace, "rev-parse", "HEAD") == ctx["pinned_sha"]
        assert git(workspace, "symbolic-ref", "-q", "HEAD", check=False) == ""
        assert "ISOLATED EVALUATOR WORKSPACE" in prompt and "ROOT-FREE" in prompt
        assert f"git -C {lead}" in prompt
        w.integration(s, runner, ctx, workspace, box)
        seen["ship_tip"] = git(lead, "log", "-1", "--format=%H %s", f"trio/{s['slug']}")
        return True

    world.hooks["integration-eval"] = integration
    code = world.run_loop(spec)
    assert code == 0, _dump(world, home, spec)
    sha, _, subject = seen["ship_tip"].partition(" ")
    assert subject == "loop: iteration 1 — SHIP"
    git(home, "merge-base", "--is-ancestor", sha, "main")
    assert (home / "scratch.txt").read_text() == "user scratch\n"
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == "?? scratch.txt"


# ---------------------------------------------------------------- R9


def test_r9_integration_fences_are_per_aggregate_branch(tmp_path, monkeypatch):
    from r16_harness import GIT_ENV, ROOT, load

    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    wt = load("worker_worktrees_r16acc_r9", ROOT / "worker_worktrees.py")
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "x\n"}, metrics=False)
    lead_a, lead_b = tmp_path / "wts" / "lead-a", tmp_path / "wts" / "lead-b"
    git(home, "worktree", "add", "-q", "-b", "trio/a", str(lead_a))
    git(home, "worktree", "add", "-q", "-b", "trio/b", str(lead_b))
    root = tmp_path / "workers"

    def builder(lead: Path, name: str) -> dict:
        rec = wt.create(lead, slice_id=name, root=root)
        (Path(rec["path"]) / f"{name}.txt").write_text(name + "\n")
        wt.mark_exited(lead, rec, 0)
        return rec

    token = wt.acquire_fence(lead_a, reason="integration-eval of trio/a")
    from_b = builder(lead_b, "wb")
    from_a = builder(lead_a, "wa")
    got_b = wt.integrate(lead_b, from_b["id"])
    got_a = wt.integrate(lead_a, from_a["id"])
    assert got_b["state"] == "integrated", (got_b.get("retained_reason"), got_b.get("retained_detail"))
    assert (lead_b / "wb.txt").is_file()
    assert got_a["state"] == "retained" and got_a["retained_reason"] == "integration_fenced", got_a
    assert not (lead_a / "wa.txt").exists()
    assert wt.release_fence(lead_a, token)
    # A flat fence at the root (on main) retains neither loop's builders.
    root_token = wt.acquire_fence(home, reason="root-bound integration-eval")
    assert wt.active_fence(home) is not None
    got_a = wt.integrate(lead_a, from_a["id"])
    assert got_a["state"] == "integrated", got_a
    from_b2 = builder(lead_b, "wb2")
    assert wt.integrate(lead_b, from_b2["id"])["state"] == "integrated"
    assert (lead_a / "wa.txt").is_file() and (lead_b / "wb2.txt").is_file()
    assert wt.release_fence(home, root_token)


# ---------------------------------------------------------------- R10


def _setup_home(tmp_path: Path, command: str) -> Path:
    return _home(tmp_path, {
        ".gitignore": "node_modules/\n",
        ".cursor/worktrees.json": json.dumps({"setup-worktree": [command]}) + "\n",
    })


def test_r10_setup_installs_deps_and_lead_worktree_removed_after_land(world, tmp_path):
    home = _setup_home(
        tmp_path,
        'mkdir -p node_modules/x && echo hi > node_modules/x/i.js && test -n "$ROOT_WORKTREE_PATH"',
    )
    spec = world.add_loop(home, "loop/r10", [{"id": "r10-one", "write": "src/r10.py"}])
    seen: dict = {}

    def lead_hook(w, s, runner, ctx, workspace, box, prompt, iteration):
        seen["deps"] = (Path(runner.repo) / "node_modules/x/i.js").read_text()
        return False

    world.hooks["lead-pass"] = lead_hook
    code = world.run_loop(spec)
    assert code == 0, _dump(world, home, spec)
    assert seen["deps"] == "hi\n"
    record = _record(world, home, spec)
    assert record["state"] == "removed", record.get("retained_reason")
    assert not Path(record["path"]).exists()
    assert len(_worktrees(home)) == 1
    assert not (home / "node_modules").exists()


def test_r10_setup_failure_is_error_with_output_and_root_untouched(world, tmp_path):
    home = _setup_home(tmp_path, "echo boom; exit 7")
    spec = world.add_loop(home, "loop/r10f", [{"id": "r10f-one", "write": "src/r10f.py"}])
    before = snapshot_root(home)
    code = world.run_loop(spec)
    assert code == 3
    assert not world.events  # nothing was dispatched
    live = _live(world, home, spec)
    assert live != spec["root_box"]
    state = _state(live)
    assert state["status"] == "error" and state["phase"] == "worktree-setup", state
    assert "boom" in (live / "LOG.md").read_text()
    assert snapshot_root(home) == before
    assert git(home, "rev-parse", "main") == before["head"]


# ---------------------------------------------------------------- extras


def test_root_bound_refused_while_root_free_record_active(world, tmp_path):
    home = _setup_home(tmp_path, "echo boom; exit 7")
    spec = world.add_loop(home, "loop/rb2", [{"id": "rb2-one", "write": "src/rb2.py"}])
    assert world.run_loop(spec) == 3  # leaves an active record (setup failed)
    assert world.rf.active(_record(world, home, spec))
    before = snapshot_root(home)
    assert world.run_loop(spec, "--root-bound") == 2
    assert snapshot_root(home) == before
    assert not world.events


def _status(world: World, box: Path, capsys) -> dict:
    capsys.readouterr()
    args = world.trioctl.parser().parse_args(["omnigent", "status", "--mailbox", str(box), "--json"])
    assert world.trioctl.command_status(args) == 0
    return json.loads(capsys.readouterr().out)


def _needs_land(world: World, home: Path, rel: str) -> dict:
    spec = world.add_loop(home, rel, [{"id": rel.split("/")[-1] + "-one",
                                       "write": f"src/{rel.split('/')[-1]}.py"}])
    target = home / "src" / f"{rel.split('/')[-1]}.py"

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        target.write_text("user's own\n")  # blocks the land
        return False

    world.hooks["integration-eval"] = integration
    assert world.run_loop(spec) == 8, _dump(world, home, spec)
    world.hooks.clear()
    return spec


def test_status_reports_root_free_loop_from_root_mailbox(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = _needs_land(world, home, "loop/st")
    record = _record(world, home, spec)
    payload = _status(world, spec["root_box"], capsys)
    assert payload["root_free"] is True
    assert payload["live_mailbox"] == record["live_mailbox"]
    assert payload["branch"] == "trio/loop--st" and payload["target_ref"] == "main"
    assert payload["lead_worktree"] == record["path"]
    assert payload["record_state"] == "active" and payload["driver"] is None
    assert payload["state"]["status"] == "needs_land"


def test_abandon_removes_lead_worktree_and_keeps_branch(world, tmp_path, capsys):
    home = _home(tmp_path)
    spec = _needs_land(world, home, "loop/ab")
    record = _record(world, home, spec)
    lead, branch = Path(record["path"]), record["branch"]
    tip = git(home, "rev-parse", branch)
    main_before = git(home, "rev-parse", "main")
    args = world.trioctl.parser().parse_args(
        ["omnigent", "abandon", "--mailbox", str(spec["root_box"])])
    capsys.readouterr()
    code = world.trioctl.command_abandon(args)
    out = capsys.readouterr()
    assert code == 0, out.out + out.err
    assert not lead.exists() and str(lead) not in _worktrees(home)
    # The branch is kept: it still contains the loop's work, plus one
    # mailbox-only `loop: abandon` commit with the driver's own state.
    git(home, "merge-base", "--is-ancestor", tip, branch)
    assert git(home, "log", "-1", "--format=%s", branch) == "loop: abandon loop/ab"
    assert git(home, "diff", "--name-only", tip, branch).splitlines()
    assert all(p.startswith("loop/ab/") for p in
               git(home, "diff", "--name-only", tip, branch).splitlines())
    assert git(home, "rev-parse", "main") == main_before
    assert _record(world, home, spec)["state"] == "removed"


def test_begin_refused_when_target_metrics_api_too_old(world, tmp_path):
    home = _home(tmp_path)
    tm = home / "metrics" / "trio-metrics.py"
    text = tm.read_text()
    assert re.search(r"^METRICS_API = \d+", text, re.M)
    tm.write_text(re.sub(r"^METRICS_API = \d+", "METRICS_API = 5", text, count=1, flags=re.M))
    git(home, "commit", "-q", "-am", "old metrics")
    spec = world.add_loop(home, "loop/old", [{"id": "old-one", "write": "src/old.py"}])
    before = snapshot_root(home)
    assert world.run_loop(spec) == 3
    assert snapshot_root(home) == before
    assert not [b for b in _branches(home) if b.startswith("trio/")]
    assert len(_worktrees(home)) == 1
    assert _record(world, home, spec) is None
    assert not world.events


def test_integration_eval_bind_failure_is_terminal_error(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/ib", [{"id": "ib-one", "write": "src/ib.py"}])

    def boom(self, mailbox, context):
        raise world.trioctl.TrioctlError("no worktree for you")

    monkeypatch.setattr(world.trioctl.OmnigentRunner, "_integration_eval_worktree", boom)
    code = world.run_loop(spec)
    assert code == 3
    live = _live(world, home, spec)
    state = _state(live)
    assert state["status"] == "error" and state["phase"] == "driver-exception", state
    assert "no worktree for you" in (live / "LOG.md").read_text()
    assert not _kinds(world, "integration-eval")
    assert all(Path(e["workspace"]) != home for e in world.events)
    assert not (home / "src/ib.py").exists()


def test_slice_eval_bind_failure_never_grades_at_root(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/sb", [{"id": "sb-one", "write": "src/sb.py"}])
    calls = [0]

    def boom(self, role, mailbox, context):
        calls[0] += 1
        raise world.trioctl.TrioctlError("slice worktree refused")

    monkeypatch.setattr(world.trioctl.OmnigentRunner, "_slice_eval_worktree", boom)
    code = world.run_loop(spec)
    assert code == 3, _dump(world, home, spec)
    live = _live(world, home, spec)
    log = (live / "LOG.md").read_text()
    assert log.count("eval_isolation_failed") >= 3, log
    assert _state(live)["status"] == "error"
    assert not _kinds(world, "slice-eval")
    assert not _kinds(world, "integration-eval")
    assert all(Path(e["workspace"]) != home for e in world.events)
    assert calls[0] >= 6  # each bind tried twice, three attempts

"""native-v01 fix (eval-v01): the ownership ledger. The driver merges,
removes or deletes only what a record the helper itself wrote proves this
mailbox's runs own. The eval-v01 repros (repros/test_repro_v01.py, each of
which passed while its defect existed) are ported here inverted."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from test_step_ops import git, mbox, repo, step  # noqa: F401 (fixture)
from test_waves import builder_branch, lead_running, owned_wave

BOX_B = "loop-b"


def ledger(repo: Path, box: Path | None = None) -> list[dict]:
    import hashlib
    key = hashlib.sha256(str((box or mbox(repo)).resolve()).encode()).hexdigest()[:16]
    path = repo / ".git" / "trio-native" / key / "owned.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def untouched(repo: Path, b: dict) -> bool:
    return (Path(b["worktree"]).exists()
            and git(repo, "branch", "--list", b["branch"]) != "")


# ------------------------------------------------ R1 / R1b (finding 1)
def test_r1_refused_foreign_branch_is_never_reclaimed(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    mine = builder_branch(repo, "wf_A-5")
    other = builder_branch(repo, "wf_OTHER-5", loop_residue=False)
    # the builder reports ITS OWN worktree but a wrong (foreign) branch
    claim = dict(mine, branch=other["branch"], head=other["head"],
                 commits=other["commits"])
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([claim]))
    [ref] = out["refused"]
    assert ref["kind"] == "report" and ref["own_branch"] is None
    # nothing ownable was recorded for a report git could not confirm
    assert out["builders"] == []
    assert [e for e in ledger(repo) if e["kind"] == "builder"] == []
    step(repo, "end")
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert all(r[k] == [] for k in ("merged", "discarded", "removed", "kept"))
    assert untouched(repo, other) and untouched(repo, mine)


def test_r1b_foreign_branch_not_deleted_outside_lead_running(
        repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    mine = builder_branch(repo, "wf_A-5")
    other = builder_branch(repo, "wf_OTHER-5", loop_residue=False)
    claim = dict(mine, branch=other["branch"], head=other["head"],
                 commits=other["commits"])
    step(repo, "builders", iteration=1, wave=1, head=head,
         results=json.dumps([claim]))
    step(repo, "end")
    st = mbox(repo) / "STATE.md"
    st.write_text(st.read_text().replace("phase: lead-running", "phase: idle"))
    headnow = git(repo, "rev-parse", "HEAD")
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert r["discarded"] == [] and git(repo, "rev-parse", "HEAD") == headnow
    assert untouched(repo, other)


def test_pinned_run_id_refuses_a_sibling_pair_and_names_the_own_one(
        repo: Path) -> None:
    """Once the run id is pinned, git alone decides: a builder reporting a
    sibling's consistent pair is refused (report kind), and its own
    isolation worktree is recorded."""
    lead_running(repo)
    w = owned_wave(repo, {"s1": {}}, rid="wf_P", first=5)   # pins wf_P
    assert w
    head = step(repo, "dispatch", iteration=1, wave=2)["head"]
    a = builder_branch(repo, "wf_P-7", sid="s2")
    b = builder_branch(repo, "wf_P-8", sid="s3")
    liar = dict(b, id="s2", agent_index=7)          # s2 reports s3's pair
    out = step(repo, "builders", iteration=1, wave=2, head=head,
               results=json.dumps([liar]))
    [ref] = out["refused"]
    assert ref["kind"] == "report" and a["worktree"] in ref["reason"]
    assert out["builders"] == [{"id": "s2", "branch": a["branch"],
                                "worktree": a["worktree"]}]


def test_ambiguous_or_foreign_run_ids_own_nothing(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    a = builder_branch(repo, "wf_A-5")
    b = builder_branch(repo, "wf_B-5")              # same index, same HEAD
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([a]))
    assert out["accepted"] == ["wf_A-5"] and out["builders"] == []
    assert "ambiguous" in out["unowned"][0]["reason"]
    # merged or not, cleanup never removes an unowned branch
    git(repo, "merge", "--no-ff", "--no-edit", "-q", a["branch"])
    cl = step(repo, "cleanup", branches=a["branch"])
    assert cl["removed"] == [] and untouched(repo, a) and untouched(repo, b)


def test_worktree_that_predates_the_dispatch_is_not_owned(repo: Path) -> None:
    lead_running(repo)
    early = builder_branch(repo, "wf_E-5")
    head = step(repo, "dispatch", iteration=1)["head"]
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([early]))
    assert out["builders"] == [] and out["accepted"] == ["wf_E-5"]


def test_a_run_id_held_by_another_mailbox_is_never_owned(repo: Path) -> None:
    """Mailbox B's ledger holds wf_S; mailbox A's builder that reports B's
    isolation worktree (same agent index, same HEAD) owns nothing."""
    box_b = repo / BOX_B
    box_b.mkdir()
    (box_b / "GOAL.md").write_text("# Goal B\n")
    (box_b / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n")
    (box_b / "LOG.md").write_text("# Trio loop log\n")
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    shared = builder_branch(repo, "wf_S-5")
    from test_step_ops import HELPER, git_env
    import sys

    def step_b(op: str, **kw) -> dict:
        cmd = [sys.executable, str(HELPER), op, "--mailbox", str(box_b),
               "--token", "t-b", "--nonce", f"t-b/{op}", "--json"]
        for k, v in kw.items():
            cmd += [f"--{k.replace('_', '-')}", str(v)]
        return json.loads(subprocess.run(cmd, capture_output=True, text=True,
                                         env=git_env()).stdout)
    assert step_b("begin")["ok"] and step_b("next", max_iterations=4)["ok"]
    head_b = step_b("dispatch", iteration=1)["head"]
    assert head_b == head
    # B dispatched after wf_S-5 existed, so B owns only its later wf_S-6,
    # which pins wf_S in B's ledger
    b_own = builder_branch(repo, "wf_S-6")
    got = step_b("builders", iteration=1, wave=1, head=head,
                 results=json.dumps([b_own]))
    assert [x["branch"] for x in got["builders"]] == [b_own["branch"]]
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([shared]))
    # without B's ledger this would be A's (new since A's dispatch, report
    # agrees); B holds wf_S, so A owns nothing
    assert out["builders"] == [] and out["accepted"] == ["wf_S-5"]


# ------------------------------------------------------ R2 (finding 2)
def test_r2_end_never_removes_other_runs_scratch(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    wt = repo / ".claude" / "worktrees"
    other_eval = wt / "eval-1-0badc0de-scratch"     # mailbox B's Evaluator
    other_tmp = wt / "tmp.pytestB"                   # mailbox B's TMPDIR
    for d in (other_eval, other_tmp):
        d.mkdir()
        (d / "in-use.txt").write_text("live\n")
    e = step(repo, "end")
    assert other_eval.exists() and other_tmp.exists()
    assert sorted(e["scratch_left"]) == sorted([str(other_eval), str(other_tmp)])


def test_scratch_swapped_after_creation_is_not_removed(repo: Path) -> None:
    tmpdir = Path(step(repo, "begin")["tmpdir"])
    moved = tmpdir.with_name(tmpdir.name + ".orig")
    tmpdir.rename(moved)
    tmpdir.mkdir()                    # same name, not the helper's inode
    (tmpdir / "someone.txt").write_text("x")
    e = step(repo, "end")
    assert moved.exists()
    assert tmpdir.exists() and e["scratch_removed"] == []
    assert "dev/inode differ" in e["scratch_kept"][0]["reason"]
    assert str(tmpdir) in e["scratch_left"] and e["lock"] == "released"


# ------------------------------------------------------ R3 (finding 4)
def test_r3_work_refusal_never_supersedes_a_foreign_branch(repo: Path) -> None:
    lead_running(repo)
    head0 = git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("l\n")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "later product commit")
    other = builder_branch(repo, "wf_OTHER-2", base=head0, loop_residue=False)
    head = step(repo, "dispatch", iteration=1)["head"]
    claim = dict(other, base=head)          # the builder reports the pair
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([claim]))
    [ref] = out["refused"]
    assert ref["kind"] == "work" and ref["own_branch"] is None
    # even if a script asked: cleanup never drops an unowned branch
    new = owned_wave(repo, {"s": {}}, wave=2)["s"]
    git(repo, "merge", "--no-ff", "--no-edit", "-q", new["branch"])
    cl = step(repo, "cleanup", branches=new["branch"],
              drop_unmerged=f"{other['branch']}={new['branch']}")
    assert cl["dropped"][0]["dropped"] is False
    assert untouched(repo, other)


# ------------------------------------------------------ R4 (finding 5)
def test_r4_preexisting_merge_state_is_left_alone(repo: Path,
                                                  tmp_path: Path) -> None:
    lead_running(repo)
    b = owned_wave(repo, {"s": {}})["s"]
    step(repo, "end")
    side = tmp_path / "side-wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side), "HEAD")
    (side / "side.txt").write_text("s\n")
    git(side, "add", "side.txt")
    git(side, "commit", "-q", "-m", "side")
    git(repo, "worktree", "remove", str(side))
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    git(repo, "reset", "-q", "--", "side.txt")      # index == HEAD again
    merge_head = (repo / ".git" / "MERGE_HEAD").read_text()
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert (repo / ".git" / "MERGE_HEAD").read_text() == merge_head
    assert r["merged"] == [] and r["discarded"] == []
    assert "in-progress operation (MERGE_HEAD)" in r["kept"][0]["reason"]
    assert untouched(repo, b)
    assert "in-progress operation" in (mbox(repo) / "LOG.md").read_text()


# ------------------------------------------------ R5 / 6 (findings 3, 6)
def test_r5_unopenable_scratch_dir_is_removed_and_end_completes(
        repo: Path, _isolated_native_registry) -> None:
    tmpdir = Path(step(repo, "begin")["tmpdir"])
    locked = tmpdir / "pytest-of-u" / "pytest-3" / "test_perm0" / "cfg"
    locked.mkdir(parents=True)
    (locked / "secret").write_text("s\n")
    os.chmod(locked, 0)
    os.chmod(locked.parent, 0o500)
    e = step(repo, "end")
    assert e["ok"] and e["lock"] == "released"
    assert e["scratch_removed"] == [str(tmpdir)] and not tmpdir.exists()
    assert not (mbox(repo) / ".lock").exists()
    assert (mbox(repo) / ".native-result.json").exists()
    regs = [json.loads(p.read_text()).get("state")
            for p in Path(_isolated_native_registry).glob("*.json")]
    assert regs == ["ended"]


def test_top_level_mode_000_scratch_is_removed_without_touching_the_shared_dir(
        repo: Path) -> None:
    tmpdir = Path(step(repo, "begin")["tmpdir"])
    (tmpdir / "x").write_text("x")
    os.chmod(tmpdir, 0)
    wts = repo / ".claude" / "worktrees"
    os.chmod(wts, 0o755)
    e = step(repo, "end")
    assert e["scratch_removed"] == [str(tmpdir)] and not tmpdir.exists()
    assert stat.S_IMODE(os.stat(wts).st_mode) == 0o755


def test_cleanup_failure_still_releases_the_lock_and_writes_records(
        repo: Path, _isolated_native_registry) -> None:
    """A scratch dir that now holds a git worktree is refused; `end` still
    releases the lock and writes the result and registry records, and the
    dir is reported in scratch_left."""
    tmpdir = Path(step(repo, "begin")["tmpdir"])
    git(repo, "worktree", "add", "-q", "--detach", str(tmpdir / "wt"), "HEAD")
    e = step(repo, "end")
    assert e["ok"] and e["lock"] == "released"
    assert e["scratch_removed"] == [] and str(tmpdir) in e["scratch_left"]
    assert "git worktree" in e["scratch_kept"][0]["reason"]
    assert (mbox(repo) / ".native-result.json").exists()


def test_rmtree_handles_every_callback_kind_in_process(tmp_path: Path) -> None:
    """_rm_contents directly: unreadable, unwritable and unsearchable dirs,
    files, symlinks (to a mode-000 dir outside: never followed or
    chmod-ed)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tns", Path(__file__).resolve().parents[1] / "trio_native_step.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    top = tmp_path / "top"
    for d, mode in (("a", 0o000), ("b", 0o500), ("c", 0o300), ("d/e", 0o100)):
        (top / d).mkdir(parents=True)
        (top / d / "f").write_text("x") if mode else None
    for d, mode in (("a", 0o000), ("b", 0o500), ("c", 0o300), ("d/e", 0o100)):
        os.chmod(top / d, mode)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0)
    (top / "ln").symlink_to(outside)
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        errors = mod._rm_contents(fd, os.fstat(fd).st_dev)
    finally:
        os.close(fd)
    assert errors == [] and os.listdir(top) == []
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0
    os.chmod(outside, 0o700)


# ------------------------------------------------------ finding 8 (INFO)
def test_end_never_removes_another_executions_eval_pin(repo: Path) -> None:
    step(repo, "begin")
    from test_step_ops import to_lead_done
    p = to_lead_done(repo)
    mine = Path(p["eval_worktree"])
    git(repo, "worktree", "add", "-q", "--detach", str(mine), p["sha"])
    other = repo / ".claude" / "worktrees" / ("eval-" + "f" * 32 + "-1-00000000")
    git(repo, "worktree", "add", "-q", "--detach", str(other), p["sha"])
    moved = repo / ".claude" / "worktrees" / "eval-1-x"
    e = step(repo, "end")
    assert e["eval_worktrees_removed"] == [str(mine)] and not mine.exists()
    assert other.exists() and str(other) in e["eval_worktrees_left"]
    assert not moved.exists()


def test_eval_pin_moved_off_the_pinned_sha_is_kept(repo: Path) -> None:
    step(repo, "begin")
    from test_step_ops import to_lead_done
    p = to_lead_done(repo)
    mine = Path(p["eval_worktree"])
    git(repo, "worktree", "add", "-q", "--detach", str(mine), p["sha"])
    (mine / "n.txt").write_text("n")
    git(mine, "add", "n.txt")
    git(mine, "commit", "-q", "-m", "evaluator scratch commit")
    e = step(repo, "end")
    assert mine.exists() and e["eval_worktrees_removed"] == []
    assert "pinned sha" in e["eval_worktrees_kept"][0]["reason"]
    assert Path(p["eval_scratch"]).parent == mine.parent
    assert str(p["eval_scratch"]) in e["scratch_removed"]

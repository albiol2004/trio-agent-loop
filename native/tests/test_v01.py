"""native-v01 (after N0): builder sha correction, run-scratch removal at
`end`, and a fresh run reclaiming an earlier run's builder worktrees."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from test_step_ops import git, mbox, repo, step  # noqa: F401 (fixture)
from test_waves import builder_branch, lead_running


def fabricate(sha: str) -> str:
    """The N0 vps-pool r1 shape: git printed the short sha (7 chars), the
    builder reported a reconstructed full sha that shares only that prefix
    (`cc51ac69a9ba` committed, `cc51ac6a9bad75…` reported)."""
    swap = {c: d for c, d in zip("0123456789abcdef", "123456789abcdef0")}
    tail = "".join(swap[c] for c in sha[7:])
    return sha[:7] + tail


def extra_commit(wt: str, name: str, subject: str) -> str:
    Path(wt, name).write_text("more\n")
    git(Path(wt), "add", name)
    git(Path(wt), "commit", "-q", "-m", subject)
    return git(Path(wt), "rev-parse", "HEAD")


def log_text(repo: Path) -> str:
    return (mbox(repo) / "LOG.md").read_text()


# ------------------------------------------------ item 1: sha correction
def test_fabricate_matches_the_n0_shape() -> None:
    reported = fabricate("cc51ac69a9ba" + "0" * 28)
    assert reported.startswith("cc51ac6") and reported[:12] != "cc51ac69a9ba"


def test_fabricated_head_of_one_slice_commit_is_corrected_from_git(
        repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b = builder_branch(repo, "pool-core")
    actual = b["head"]
    lie = fabricate(actual)
    assert lie != actual and lie[:7] == actual[:7]
    b.update(head=lie, commits=[lie])
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([b]))
    assert out["ok"] and out["accepted"] == ["pool-core"] and not out["refused"]
    assert out["merge"] == [{"id": "pool-core", "branch": "worktree-pool-core"}]
    assert out["corrected"] == [{"id": "pool-core",
                                 "branch": "worktree-pool-core",
                                 "reported": lie, "actual": actual}]
    # the builder LOG line names git's tip, not the reported sha
    assert f"(worktree-pool-core@{actual[:7]})" in log_text(repo)


def test_mismatch_with_two_commits_asks_for_a_report_then_accepts(
        repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b = builder_branch(repo, "b2")
    tip = extra_commit(b["worktree"], "b2_more.py", "slice(b2): more")
    b.update(head=fabricate(tip))
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([b]))
    assert out["accepted"] == [] and not out["corrected"]
    [ref] = out["refused"]
    assert ref["kind"] == "report" and ref["own_branch"] == "worktree-b2"
    assert "2 commit(s)" in ref["reason"]
    assert "| builder | b2" not in log_text(repo)
    # the builder reports again with git's real tip: a new key, re-verified
    b.update(head=tip, commits=[b["commits"][0], tip])
    again = step(repo, "builders", iteration=1, wave=1, head=head,
                 results=json.dumps([b]), attempt=2)
    assert again["accepted"] == ["b2"] and not again["refused"]
    assert log_text(repo).count("| builder | b2") == 1


def test_mismatch_without_a_slice_subject_is_not_corrected(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    wt = repo / ".claude" / "worktrees" / "b3"
    git(repo, "worktree", "add", "-q", "-b", "worktree-b3", str(wt), head)
    tip = extra_commit(str(wt), "b3.py", "wip: no slice subject")
    res = {"id": "b3", "worktree": str(wt), "branch": "worktree-b3",
           "base": head, "head": fabricate(tip), "commits": [tip],
           "summary": "b3"}
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([res]))
    assert out["refused"][0]["kind"] == "report"


def test_loop_commit_is_a_work_refusal(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    bad = builder_branch(repo, "b4", files={"b4.py": "z\n",
                                            "loop/LOG.md": "oops\n"})
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([bad]))
    [ref] = out["refused"]
    assert ref["kind"] == "work" and ref["own_branch"] == "worktree-b4"


def test_foreign_branch_report_has_no_own_branch(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b5 = builder_branch(repo, "b5")
    b6 = builder_branch(repo, "b6")
    liar = dict(b5, branch=b6["branch"], head=fabricate(b6["head"]))
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([liar]))
    [ref] = out["refused"]
    assert ref["kind"] == "report" and ref["own_branch"] is None


# ------------------------------------------------ item 2: run scratch
def _mk(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "f.txt").write_text("x")
    return path


def test_end_removes_only_scratch_this_run_created(repo: Path,
                                                   tmp_path: Path) -> None:
    wts = repo / ".claude" / "worktrees"
    old = _mk(wts / "tmpOLD")               # predates the run: reported only
    assert step(repo, "begin")["ok"]
    scratch = _mk(wts / "eval-1-abcd1234-scratch")
    ro = _mk(scratch / "node_modules" / "pkg")
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)       # read-only cache dir
    os.chmod(ro.parent, stat.S_IRUSR | stat.S_IXUSR)
    tmp = _mk(wts / "tmpa1b2c3")
    keep = _mk(wts / "notes")                 # not a scratch name
    outside = _mk(tmp_path / "outside")
    (wts / "tmpLINK").symlink_to(outside)     # a symlink: never followed
    (wts / "tmpfile").write_text("not a dir")
    pinwt = wts / "eval-1-pin"                # a git worktree: eval rule
    git(repo, "worktree", "add", "-q", "--detach", str(pinwt), "HEAD")
    tmpwt = wts / "tmp-wt"                    # a builder worktree: reported
    git(repo, "worktree", "add", "-q", "-b", "worktree-tmp", str(tmpwt), "HEAD")
    e = step(repo, "end")
    assert e["ok"]
    assert sorted(e["scratch_removed"]) == sorted([str(scratch), str(tmp)])
    assert not scratch.exists() and not tmp.exists()
    assert e["scratch_left"] == [str(old)] and old.exists()
    assert keep.exists() and (outside / "f.txt").exists()
    assert (wts / "tmpLINK").is_symlink() and (wts / "tmpfile").exists()
    assert e["eval_worktrees_removed"] == [str(pinwt)]
    assert e["dangling_worktrees"] == [str(tmpwt)] and tmpwt.exists()


def test_end_without_this_runs_baseline_removes_nothing(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    scratch = _mk(repo / ".claude" / "worktrees" / "tmpzzz")
    records = json.loads((mbox(repo) / ".native.json").read_text())
    records["scratch_baseline"]["exec_id"] = "0" * 32   # another execution
    (mbox(repo) / ".native.json").write_text(json.dumps(records))
    e = step(repo, "end")
    assert e["scratch_removed"] == [] and scratch.exists()


def test_end_of_a_foreign_run_removes_no_scratch(repo: Path) -> None:
    assert step(repo, "begin")["ok"]
    scratch = _mk(repo / ".claude" / "worktrees" / "tmpq")
    e = step(repo, "end", token="someone-else")
    assert e["lock"] == "foreign" and e["scratch_removed"] == []
    assert scratch.exists()


# ------------------------------------- item 3: previous run's builders
def _previous_run(repo: Path) -> dict:
    """Run 1 of iteration 1: a wave of three builders, then an error stop
    before integrate (the N0 vps-pool r1 attempt-1 shape)."""
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b1 = builder_branch(repo, "b1", files={"b1.py": "a = 1\n"})
    b2 = builder_branch(repo, "b2", files={"b2.py": "b = 2\n"})
    b2tip = extra_commit(b2["worktree"], "b2x.py", "slice(b2): more")
    b2.update(head=fabricate(b2tip))           # refused: report kind
    b3 = builder_branch(repo, "b3", files={"b3.py": "c\n",
                                           "loop/NOTE.md": "x\n"})
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([b1, b2, b3]))
    assert out["accepted"] == ["b1"] and len(out["refused"]) == 2
    assert step(repo, "end")["lock"] == "released"
    return {"b1": b1, "b2": b2, "b3": b3, "head": head, "b2tip": b2tip}


def test_fresh_begin_merges_valid_builders_and_discards_the_rest(
        repo: Path) -> None:
    prev = _previous_run(repo)
    b = step(repo, "begin", token="t-run2")
    assert b["ok"], b
    r = b["reclaimed"]
    assert sorted(x["id"] for x in r["merged"]) == ["b1", "b2"]
    assert [x["id"] for x in r["discarded"]] == ["b3"] and not r["kept"]
    head = git(repo, "rev-parse", "HEAD")
    for tip in (prev["b1"]["head"], prev["b2tip"]):
        assert git(repo, "merge-base", "--is-ancestor", tip, head) == ""
    assert (repo / "b1.py").exists() and (repo / "b2x.py").exists()
    assert not (repo / "b3.py").exists()
    for name in ("b1", "b2", "b3"):
        assert not Path(prev[name]["worktree"]).exists()
    assert git(repo, "branch", "--list", "worktree-*") == ""
    log = log_text(repo)
    assert "| loop | previous-run builder worktree-b1@" in log
    b3tip = prev["b3"]["head"]
    assert f"discarded (not reusable; tip {b3tip})" in log
    assert git(repo, "status", "--porcelain", "--", "b1.py", "b2.py") == ""
    e = step(repo, "end", token="t-run2")
    assert e["dangling_worktrees"] == []


def test_fresh_begin_outside_lead_running_only_cleans_up(repo: Path) -> None:
    prev = _previous_run(repo)
    state = mbox(repo) / "STATE.md"
    state.write_text(state.read_text().replace("phase: lead-running",
                                               "phase: idle"))
    head = git(repo, "rev-parse", "HEAD")
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert r["merged"] == [] and sorted(x["id"] for x in r["discarded"]) == [
        "b1", "b2", "b3"]
    assert git(repo, "rev-parse", "HEAD") == head
    assert not Path(prev["b1"]["worktree"]).exists()


def test_fresh_begin_conflicting_builder_is_aborted_and_discarded(
        repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    x = builder_branch(repo, "x", files={"same.py": "x = 1\n"})
    y = builder_branch(repo, "y", files={"same.py": "y = 2\n"})
    step(repo, "builders", iteration=1, wave=1, head=head,
         results=json.dumps([x, y]))
    step(repo, "end")
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert [m["id"] for m in r["merged"]] == ["x"]
    assert [(d["id"], d["reason"]) for d in r["discarded"]] == [
        ("y", "merge conflicted")]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "same.py").read_text() == "x = 1\n"


def test_fresh_begin_never_touches_unrecorded_or_dirty_worktrees(
        repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    mine = builder_branch(repo, "dirty", extra_dirt=True)
    step(repo, "builders", iteration=1, wave=1, head=head,
         results=json.dumps([mine]))
    other = builder_branch(repo, "other")     # another mailbox's builder
    step(repo, "end")
    # the Lead left a tracked product change in the checkout
    (repo / "README").write_text("lead wip\n")
    b = step(repo, "begin", token="t-run2")
    r = b["reclaimed"]
    assert r["merged"] == [] and r["discarded"] == []
    assert [k["id"] for k in r["kept"]] == ["dirty"]
    assert "uncommitted tracked changes" in r["kept"][0]["reason"]
    assert Path(other["worktree"]).exists() and Path(mine["worktree"]).exists()
    e = step(repo, "end", token="t-run2")
    assert sorted(e["dangling_worktrees"]) == sorted(
        [mine["worktree"], other["worktree"]])


def test_fresh_begin_discards_unreported_worktrees_of_the_last_run_id(
        repo: Path) -> None:
    lead_running(repo)
    step(repo, "dispatch", iteration=1)
    wt = repo / ".claude" / "worktrees" / "wf_abc123-3"
    git(repo, "worktree", "add", "-q", "-b", "worktree-wf_abc123-3", str(wt),
        "HEAD")
    extra_commit(str(wt), "k.py", "slice(k): killed mid-wave")
    unrelated = builder_branch(repo, "wf_zzz-1")
    step(repo, "end")
    (mbox(repo) / ".native-result.json").write_text(
        json.dumps({"run_id": "wf_abc123", "status": "error"}))
    r = step(repo, "begin", token="t-run2")["reclaimed"]
    assert [d["branch"] for d in r["discarded"]] == ["worktree-wf_abc123-3"]
    assert r["merged"] == [] and not wt.exists()
    assert Path(unrelated["worktree"]).exists()

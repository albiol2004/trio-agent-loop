"""Driver-owned builder waves, REPORT gate, retirement fold, eval worktrees
(live-probe blockers 1/2/5/6/7)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from test_step_ops import (TOKEN, git, lead_pass, mbox, repo, retire, state,
                           step, to_lead_done)  # noqa: F401 (fixture)


def lead_running(repo: Path) -> dict:
    assert step(repo, "begin")["ok"]
    n = step(repo, "next", max_iterations=4)
    assert n["action"] == "lead"
    return n


def builder_branch(repo: Path, name: str, *, base: str = "HEAD",
                   files: dict | None = None, loop_residue: bool = True,
                   extra_dirt: bool = False, sid: str | None = None) -> dict:
    wt = repo / ".claude" / "worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", f"worktree-{name}", str(wt), base)
    base_sha = git(wt, "rev-parse", "HEAD")
    for rel, text in (files or {f"{name}.py": "x = 1\n"}).items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
        git(wt, "add", rel)
    git(wt, "commit", "-q", "-m", f"slice({sid or name}): build")
    if loop_residue:  # the builder role's habit: an uncommitted LOG line
        with (wt / "loop" / "LOG.md").open("a") as fh:
            fh.write(f"- iter 1 | builder | {name}\n")
    if extra_dirt:
        (wt / "scratch.txt").write_text("wip\n")
    out = {"id": sid or name, "worktree": str(wt), "branch": f"worktree-{name}",
           "base": base_sha, "head": git(wt, "rev-parse", "HEAD"),
           "commits": [git(wt, "rev-parse", "HEAD")],
           "summary": f"built {name}"}
    m = _WF_NAME.fullmatch(name)
    if m:  # the harness's isolation name: the script passes its agent index
        out["agent_index"] = int(m.group(1))
    return out


_WF_NAME = re.compile(r"wf_[A-Za-z0-9_-]+?-([0-9]+)")


def owned_wave(repo: Path, specs: dict, *, rid: str = "wf_T", iteration: int = 1,
               wave: int = 1, first: int = 5) -> dict:
    """Dispatch one wave and create one workflow isolation worktree per
    label (``<rid>-<n>``, agent index ``n`` from ``first``), verified by the
    ``builders`` op, so each is ledger-owned. Returns {label: result}."""
    head = step(repo, "dispatch", iteration=iteration, wave=wave)["head"]
    out = {}
    for i, (label, kw) in enumerate(specs.items()):
        b = builder_branch(repo, f"{rid}-{first + i}", sid=label, **(kw or {}))
        out[label] = b
    res = step(repo, "builders", iteration=iteration, wave=wave, head=head,
               results=json.dumps(list(out.values())))
    assert res["ok"], res
    owned = {x["id"] for x in res["builders"]}
    assert owned == set(out), res
    for b in out.values():
        b["dispatch_head"] = head
    return out


def test_dispatch_returns_lead_head_and_needs_lead_running(repo: Path) -> None:
    step(repo, "begin")
    early = step(repo, "dispatch", iteration=1)
    assert not early["ok"] and "lead-running" in early["error"]
    step(repo, "next", max_iterations=4)
    d = step(repo, "dispatch", iteration=1, wave=1)
    assert d["ok"] and d["head"] == git(repo, "rev-parse", "HEAD")


def test_builders_accepts_and_logs_once(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    b1 = builder_branch(repo, "b1")
    results = json.dumps([b1])
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=results)
    assert out["ok"] and out["accepted"] == ["b1"] and not out["refused"]
    assert out["merge"] == [{"id": "b1", "branch": "worktree-b1"}]
    again = step(repo, "builders", iteration=1, wave=1, head=head,
                 results=results)
    assert again["accepted"] == ["b1"]
    log = (mbox(repo) / "LOG.md").read_text()
    assert log.count("- iter 1 | builder | b1: built b1") == 1
    # the root LOG has the line; the builder's worktree copy is irrelevant


def test_builders_refuses_wrong_base(repo: Path) -> None:
    """Blocker 2: a worktree forked from origin/HEAD-like base is refused."""
    lead_running(repo)
    old = git(repo, "rev-parse", "HEAD")
    (repo / "lead.py").write_text("y = 2\n")
    git(repo, "add", "lead.py")
    git(repo, "commit", "-q", "-m", "slice(app): lead work")
    head = step(repo, "dispatch", iteration=1)["head"]
    stale = builder_branch(repo, "b2", base=old)
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([stale]))
    assert out["ok"] and out["accepted"] == []
    reason = out["refused"][0]["reason"]
    assert "not the Lead's HEAD" in reason and "baseRef" in reason
    # lying about the base does not help: the branch lacks the Lead's HEAD
    liar = dict(stale, base=head)
    out = step(repo, "builders", iteration=1, wave=2, head=head,
               results=json.dumps([liar]))
    assert "does not contain the Lead's HEAD" in out["refused"][0]["reason"]
    assert "| builder |" not in (mbox(repo) / "LOG.md").read_text()


def test_builders_refuses_committed_loop_files(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    bad = builder_branch(repo, "b3", files={"b3.py": "z\n",
                                            "loop/LOG.md": "oops\n"})
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([bad]))
    assert "never commit loop/" in out["refused"][0]["reason"]


def test_builder_without_commits_is_accepted_without_merge(repo: Path) -> None:
    lead_running(repo)
    head = step(repo, "dispatch", iteration=1)["head"]
    res = {"id": "noop", "branch": "worktree-gone", "base": head,
           "head": head, "commits": [], "summary": "nothing to do"}
    out = step(repo, "builders", iteration=1, wave=1, head=head,
               results=json.dumps([res]))
    assert out["accepted"] == ["noop"] and out["merge"] == []
    assert "| builder | noop: nothing to do (no commits)" in (
        mbox(repo) / "LOG.md").read_text()


def test_cleanup_removes_merged_worktrees_force_only_for_loop_residue(
        repo: Path) -> None:
    lead_running(repo)
    w = owned_wave(repo, {"b1": {},                        # loop/ residue only
                          "b2": {"extra_dirt": True},      # product dirt
                          "b3": {}})                       # never merged
    b1, b2, b3 = w["b1"], w["b2"], w["b3"]
    for b in (b1, b2):
        git(repo, "merge", "--no-ff", "--no-edit", "-q", b["branch"])
    out = step(repo, "cleanup", branches=",".join(
        [b1["branch"], b2["branch"], b3["branch"], "worktree-nope"]))
    assert out["ok"]
    removed = {r["branch"] for r in out["removed"]}
    kept = {k["branch"]: k["reason"] for k in out["kept"]}
    assert removed == {b1["branch"], "worktree-nope"}
    assert "outside the mailbox" in kept[b2["branch"]]
    assert kept[b3["branch"]] == "not merged into HEAD"
    assert not Path(b1["worktree"]).exists()
    assert b1["branch"] not in git(repo, "branch")
    assert Path(b2["worktree"]).exists() and Path(b3["worktree"]).exists()


def test_cleanup_never_removes_a_merged_branch_it_does_not_own(
        repo: Path) -> None:
    """v01 fix: a merged builder-looking branch that no `builders` call
    proved to be this mailbox's (another mailbox's, a user's `claude -w`)
    is kept, merged or not."""
    lead_running(repo)
    other = builder_branch(repo, "wf_OTHER-5")
    git(repo, "merge", "--no-ff", "--no-edit", "-q", other["branch"])
    out = step(repo, "cleanup", branches=other["branch"])
    assert out["removed"] == []
    assert "no ownership-ledger entry" in out["kept"][0]["reason"]
    assert Path(other["worktree"]).exists()
    assert other["branch"] in git(repo, "branch")


def test_gate_requires_report_rewrite(repo: Path) -> None:
    """Blocker 6: a Lead pass that never wrote REPORT.md fails the gate."""
    (mbox(repo) / "REPORT.md").write_text("# old report\n")
    lead_running(repo)
    lead_pass(repo, 1, report=False)
    g1 = step(repo, "gate", role="lead", iteration=1, attempt=1)
    assert not g1["pass"] and any("REPORT.md was not rewritten" in f
                                  for f in g1["failures"])
    (mbox(repo) / "REPORT.md").write_text("# Report — iteration 1\n")
    g2 = step(repo, "gate", role="lead", iteration=1, attempt=2)
    assert g2["pass"]


def test_ship_folds_final_state_into_retirement(repo: Path) -> None:
    """Blocker 7: the tree is clean after SHIP; STATE is committed."""
    step(repo, "begin")
    p = to_lead_done(repo)
    git(repo, "add", "loop")  # the Lead's mailbox files, as a retirement would
    retire(repo, 1, p)
    subject = git(repo, "log", "-1", "--format=%s")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["status"] == "shipped" and a["retirement_fold"] == "amended"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "log", "-1", "--format=%s") == subject
    assert "status: shipped" in git(repo, "show", "HEAD:loop/STATE.md")
    step(repo, "end")
    assert git(repo, "status", "--porcelain") == ""


def test_fold_skips_when_other_paths_dirty(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    git(repo, "add", "loop")
    retire(repo, 1, p)
    (mbox(repo) / "notes.md").write_text("a stray mailbox note\n")
    a = step(repo, "apply", iteration=1, attempt=p["evaluator_attempt"])
    assert a["status"] == "shipped"
    assert a["retirement_fold"].startswith("skipped: other uncommitted")
    assert "status: shipped" not in git(repo, "show", "HEAD:loop/STATE.md")


def test_end_removes_eval_pin_worktrees(repo: Path) -> None:
    step(repo, "begin")
    p = to_lead_done(repo)
    wt = Path(p["eval_worktree"])
    assert wt.parent == repo / ".claude" / "worktrees"
    git(repo, "worktree", "add", "-q", "--detach", str(wt), p["sha"])
    (wt / "build.pyc").write_text("junk")
    other = repo / ".claude" / "worktrees" / "b9"
    git(repo, "worktree", "add", "-q", "-b", "worktree-b9", str(other), "HEAD")
    # another mailbox's (or an older prompt's) eval pin worktree: reported
    foreign = repo / ".claude" / "worktrees" / "eval-1-abcd1234"
    git(repo, "worktree", "add", "-q", "--detach", str(foreign), "HEAD")
    e = step(repo, "end")
    assert e["eval_worktrees_removed"] == [str(wt)] and not wt.exists()
    assert e["eval_worktrees_left"] == [str(foreign)] and foreign.exists()
    assert e["dangling_worktrees"] == sorted([str(other), str(foreign)])

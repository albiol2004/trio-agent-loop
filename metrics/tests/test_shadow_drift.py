"""Tests for `trio-shadow.py --report-drift` (cross-mailbox drift report).

Builds a single temp git repo with two fake `loop*/` mailboxes as
subdirectories (the same layout the real repo uses for archived loops:
`repo:` defaults to the mailbox directory itself, which is inside the
repo's work tree rather than a repo of its own). Covers a clean slice, an
undeclared touch, a declared-but-untouched path, and one hidden pairwise
hazard (two same-iteration slices whose declared writes look disjoint but
whose commits actually collide on a file neither declared), then asserts
the aggregate numbers.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).parents[1] / "trio-shadow.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)

# loop-alpha: mixer-1/mixer-2 share iteration 1, both declare disjoint
# single-file writes but each commit also touches shared.py — an undeclared
# touch for both slices, and (since their declared writes never overlap
# while shared.py does) a hidden pairwise hazard.
PLAN_ALPHA = """\
```yaml
slices:
  - id: mixer-1
    repo: .
    writes: [core.py]
    reads: []
    iteration: 1
  - id: mixer-2
    repo: .
    writes: [util.py]
    reads: []
    iteration: 1
```
"""

# loop-beta: clean-1 is fully clean; clean-2 declares unused.py but never
# touches it (declared-but-untouched). Neither slice's actual files
# intersect the other's, so no hazard here.
PLAN_BETA = """\
```yaml
slices:
  - id: clean-1
    repo: .
    writes: [foo.py]
    reads: []
    iteration: 1
  - id: clean-2
    repo: .
    writes: [bar.py, unused.py]
    reads: []
    iteration: 1
```
"""

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Shadow Drift Test",
    "GIT_AUTHOR_EMAIL": "shadow-drift@example.com",
    "GIT_COMMITTER_NAME": "Shadow Drift Test",
    "GIT_COMMITTER_EMAIL": "shadow-drift@example.com",
}
_commit_counter = 0


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh git repo at tmp_path/repo with identity configured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    """Stage and commit files (paths relative to repo root) with a fixed
    timestamp; return the new HEAD sha."""
    global _commit_counter
    _commit_counter += 1
    env = dict(GIT_ENV)
    env["GIT_AUTHOR_DATE"] = f"2026-01-01T00:00:{_commit_counter % 60:02d}Z"
    env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "--", name], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        check=True,
        env=env,
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def run_checker(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--report-drift", "--root", str(root), *extra],
        capture_output=True,
        text=True,
    )


def write_plan(mailbox: Path, plan: str) -> None:
    mailbox.mkdir(parents=True, exist_ok=True)
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")


@pytest.fixture
def two_mailbox_repo(git_repo: Path) -> Path:
    write_plan(git_repo / "loop-alpha", PLAN_ALPHA)
    write_plan(git_repo / "loop-beta", PLAN_BETA)
    commit(
        git_repo,
        {"loop-alpha/PLAN.md": PLAN_ALPHA, "loop-beta/PLAN.md": PLAN_BETA},
        "plan: declare loop-alpha and loop-beta slices",
    )
    # loop-alpha: undeclared touch (shared.py) on both slices, plus the
    # hazard (declared disjoint, actual overlap on shared.py).
    commit(git_repo, {"core.py": "C\n", "shared.py": "S1\n"}, "slice(mixer-1): add core")
    commit(git_repo, {"util.py": "U\n", "shared.py": "S2\n"}, "slice(mixer-2): add util")
    # loop-beta: clean-1 fully clean; clean-2 never touches unused.py.
    commit(git_repo, {"foo.py": "F\n"}, "slice(clean-1): add foo")
    commit(git_repo, {"bar.py": "B\n"}, "slice(clean-2): add bar")
    return git_repo


def test_report_drift_skips_mailboxes_without_a_parsable_slices_block(
    git_repo: Path,
) -> None:
    """A loop*/ dir with no PLAN.md, and one with a PLAN.md but no yaml
    slices fence, are both skipped silently rather than erroring."""
    (git_repo / "loop-empty").mkdir()
    write_plan(git_repo / "loop-prose", "# Just prose\nno fences here\n")
    commit(git_repo, {"loop-prose/PLAN.md": "# Just prose\nno fences here\n"}, "plan: prose only")

    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    agg = json.loads(result.stdout)
    assert agg["mailboxes_scanned"] == 0
    assert agg["total_slices"] == 0
    assert agg["mailboxes"] == []


def test_aggregate_drift_and_hazard_across_two_mailboxes(
    two_mailbox_repo: Path,
) -> None:
    result = run_checker(two_mailbox_repo, "--json")
    assert result.returncode == 0, result.stderr
    agg = json.loads(result.stdout)

    assert agg["mailboxes_scanned"] == 2
    assert agg["total_slices"] == 4
    assert agg["slices_with_commits"] == 4
    assert agg["slices_with_commits_pct"] == 100.0

    # mixer-1 and mixer-2 both touch shared.py, which neither declared.
    assert agg["slices_with_undeclared_touches"] == 2
    assert agg["slices_with_undeclared_touches_pct"] == 50.0
    assert agg["total_undeclared_touches"] == 2
    assert agg["top_undeclared_paths"] == [{"path": "shared.py", "count": 2}]

    # clean-2 declared unused.py but a commit never touched it.
    assert agg["slices_with_declared_untouched"] == 1
    assert agg["slices_with_declared_untouched_pct"] == 25.0

    # The hidden pairwise hazard: mixer-1/mixer-2 declared disjoint writes
    # ([core.py] vs [util.py]) but both actually touched shared.py.
    assert agg["pairwise_hazards_total"] == 1
    by_name = {m["mailbox"]: m for m in agg["mailboxes"]}
    alpha_hazards = by_name["loop-alpha"]["pairwise_hazards"]
    assert len(alpha_hazards) == 1
    hazard = alpha_hazards[0]
    assert hazard["iteration"] == 1
    assert {hazard["slice_a"], hazard["slice_b"]} == {"mixer-1", "mixer-2"}
    assert hazard["overlap"] == ["shared.py"]
    assert by_name["loop-beta"]["pairwise_hazards"] == []

    # loop-beta's own numbers: clean-1 clean, clean-2 declared-but-untouched.
    beta = by_name["loop-beta"]
    assert beta["total_slices"] == 2
    assert beta["slices_with_undeclared_touches"] == 0
    assert beta["slices_with_declared_untouched"] == 1

    human = run_checker(two_mailbox_repo)
    assert human.returncode == 0
    assert "loop-alpha" in human.stdout and "loop-beta" in human.stdout
    assert "shared.py" in human.stdout
    assert "mixer-1" in human.stdout and "mixer-2" in human.stdout
    assert "Result: shadow mode" in human.stdout


def test_no_mailboxes_reports_zeros_and_exits_0(tmp_path: Path) -> None:
    """An empty root (no loop*/ dirs at all) still exits 0 with zeroed
    aggregates rather than erroring."""
    result = run_checker(tmp_path, "--json")
    assert result.returncode == 0, result.stderr
    agg = json.loads(result.stdout)
    assert agg["mailboxes_scanned"] == 0
    assert agg["total_slices"] == 0
    assert agg["slices_with_commits_pct"] == 0.0
    assert agg["pairwise_hazards_total"] == 0

    human = run_checker(tmp_path)
    assert human.returncode == 0
    assert "Mailboxes scanned (parsable slices block): 0" in human.stdout

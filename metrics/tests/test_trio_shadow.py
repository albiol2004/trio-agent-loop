"""Tests for metrics/trio-shadow.py (shadow-mode slice contract checker).

Builds deterministic tempfile git repos with the git binary (fixed author
dates, isolated config) and asserts on the --json report: undeclared touches
are attributed per slice and reported precisely, repos without slice-prefixed
commits report plainly with exit 0, and missing/malformed slices blocks exit
2 with a clear message.
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

PLAN_A = """\
# Iteration 1 — current increment

```yaml
slices:
  - id: alpha
    repo: .
    writes: [a.py, "api:AlphaConfig"]
    reads: []
    gate: false
  - id: beta
    repo: .
    writes: [b.py, lib/]
    reads: ["api:AlphaConfig"]
    gate: false
```

## Verification standard
implement-then-smoke
"""

PLAN_B = """\
# Iteration 1 — current increment

```yaml
slices:
  - id: solo
    writes: [a.py]
    reads: []
```

## Verification standard
implement-then-smoke
"""

PLAN_D = """\
```yaml
slices:
  - id: gamma
    writes: [gamma.py]
    reads: []
```
"""

PLAN_CUMULATIVE = """\
# Iteration 2 — current increment

```yaml
slices:
  - id: alpha
    writes: [a.py]
    reads: []
    status: complete
    iteration: 1
  - id: fresh
    writes: [fresh.py]
    reads: []
    status: in_progress
    iteration: 2
```

## Verification standard
implement-then-smoke
"""

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Shadow Test",
    "GIT_AUTHOR_EMAIL": "shadow@example.com",
    "GIT_COMMITTER_NAME": "Shadow Test",
    "GIT_COMMITTER_EMAIL": "shadow@example.com",
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
    """Stage and commit files with a fixed timestamp; return the new HEAD sha."""
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


def run_checker(mailbox: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--mailbox", str(mailbox), *extra],
        capture_output=True,
        text=True,
    )


def write_plan(mailbox: Path, plan: str) -> None:
    (mailbox / "loop").mkdir(parents=True, exist_ok=True)
    (mailbox / "loop" / "PLAN.md").write_text(plan, encoding="utf-8")


def test_undeclared_touches_reported_per_slice(git_repo: Path) -> None:
    """Two slices; one commit touches a file outside its declared writes.

    The api: pseudo-entry must appear in declared writes but never in the
    git-derived sets, and a declared directory must cover files beneath it.
    """
    write_plan(git_repo, PLAN_A)
    commit(
        git_repo,
        {"loop/PLAN.md": PLAN_A, "loop/STATE.md": "schema: 1\n"},
        "plan: declare slices alpha and beta",
    )
    commit(git_repo, {"a.py": "A\n"}, "slice(alpha): add module a")
    commit(
        git_repo,
        {"b.py": "B\n", "lib/util.py": "U\n", "stray.py": "S\n"},
        "slice(beta): add module b and a stray file",
    )

    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mailbox"] == str(git_repo.resolve())
    assert report["plan"] == str(git_repo / "loop" / "PLAN.md")
    slices = {s["id"]: s for s in report["slices"]}

    alpha = slices["alpha"]
    assert alpha["repo_status"] == "ok"
    assert alpha["declared_writes"] == ["a.py", "api:AlphaConfig"]
    assert alpha["actual_touched"] == ["a.py"]
    assert alpha["undeclared_touches"] == []
    # api: entries are names, not paths — excluded from the git-derived sets.
    assert "api:AlphaConfig" not in alpha["declared_untouched"]
    assert alpha["declared_untouched"] == []

    beta = slices["beta"]
    assert beta["actual_touched"] == ["b.py", "lib/util.py", "stray.py"]
    # lib/ is a declared directory and covers lib/util.py; stray.py is not.
    assert beta["undeclared_touches"] == ["stray.py"]
    assert beta["declared_untouched"] == []

    summary = report["summary"]
    assert summary["total_slices"] == 2
    assert summary["slices_with_undeclared_touches"] == 1
    assert summary["undeclared_file_count"] == 1


def test_no_slice_prefixed_commits_reports_plainly(git_repo: Path) -> None:
    """A repo with ordinary commits and no slice(<id>): prefixes: exit 0,
    empty touched sets, and a plain human-readable note."""
    write_plan(git_repo, PLAN_B)
    commit(git_repo, {"loop/PLAN.md": PLAN_B}, "plan: declare slices")
    commit(git_repo, {"a.py": "A\n"}, "feat: add a")
    commit(git_repo, {"a.py": "A2\n"}, "fix: tweak a")

    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    solo = report["slices"][0]
    assert solo["id"] == "solo"
    assert solo["repo"] == "."  # repo: omitted -> defaults to the mailbox root
    assert solo["repo_status"] == "ok"
    assert solo["commits"] == []
    assert solo["actual_touched"] == []
    assert solo["undeclared_touches"] == []
    assert solo["declared_untouched"] == ["a.py"]
    assert report["summary"]["slices_with_undeclared_touches"] == 0
    assert report["summary"]["undeclared_file_count"] == 0

    human = run_checker(git_repo)
    assert human.returncode == 0
    assert "no slice-prefixed commits" in human.stdout
    assert "Result: shadow mode" in human.stdout


def test_missing_git_repo_reports_plainly(git_repo: Path, tmp_path: Path) -> None:
    """A declared repo path that does not exist: exit 0, status reported."""
    plan = PLAN_B.replace("writes: [a.py]", "repo: nope\n    writes: [a.py]")
    write_plan(git_repo, plan)
    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    solo = json.loads(result.stdout)["slices"][0]
    assert solo["repo_status"] == "missing"
    assert solo["commits"] == []
    assert json.loads(result.stdout)["summary"]["repos_missing_or_not_git"] == 1

    human = run_checker(git_repo)
    assert "repo missing" in human.stdout


def test_mailbox_may_be_the_loop_dir_itself(git_repo: Path) -> None:
    """Pointing --mailbox at loop/ (PLAN.md directly under it) works too."""
    write_plan(git_repo, PLAN_D)
    commit(git_repo, {"loop/PLAN.md": PLAN_D}, "plan: declare slices")
    commit(git_repo, {"gamma.py": "G\n", "oops.py": "O\n"}, "slice(gamma): add gamma and oops")

    result = run_checker(git_repo / "loop", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["plan"] == str(git_repo / "loop" / "PLAN.md")
    gamma = report["slices"][0]
    assert gamma["repo_status"] == "ok"
    assert gamma["undeclared_touches"] == ["oops.py"]


@pytest.mark.parametrize(
    ("name", "plan_text", "expected"),
    [
        ("no-yaml-fence", "# Plan only, no fences\nslices: dangling\n", "no ```yaml slices block"),
        ("missing-slices-key", "```yaml\nwrites: [a.py]\n```\n", "no ```yaml slices block"),
        ("unknown-key", "```yaml\nslices:\n  - id: alpha\n    writes: [a.py]\n    bogus: 1\n```\n", "unknown slice key 'bogus'"),
        ("bad-writes", "```yaml\nslices:\n  - id: alpha\n    writes: nope\n```\n", "bracketed list"),
        ("bad-id", "```yaml\nslices:\n  - id: Bad ID\n    writes: []\n```\n", "not kebab-case"),
        ("bad-gate", "```yaml\nslices:\n  - id: alpha\n    writes: []\n    gate: maybe\n```\n", "must be true or false"),
        ("bad-status", "```yaml\nslices:\n  - id: alpha\n    writes: []\n    status: done\n```\n", "must be one of planned, in_progress, complete"),
        ("bad-iteration", "```yaml\nslices:\n  - id: alpha\n    writes: []\n    iteration: 2.5\n```\n", "must be an integer"),
        ("unknown-key-with-valid-status", "```yaml\nslices:\n  - id: alpha\n    writes: []\n    status: complete\n    bogus: 1\n```\n", "unknown slice key 'bogus'"),
        ("content-outside-slice", "```yaml\nslices:\n  - id: alpha\n    writes: []\n  - id: beta\n    writes: []\nrandom: line\n```\n", "unknown slice key 'random'"),
    ],
)
def test_malformed_slices_block_exits_2(
    tmp_path: Path, name: str, plan_text: str, expected: str
) -> None:
    mailbox = tmp_path / name
    write_plan(mailbox, plan_text)
    result = run_checker(mailbox)
    assert result.returncode == 2, result.stdout
    assert expected in result.stderr


def test_missing_plan_exits_2(tmp_path: Path) -> None:
    mailbox = tmp_path / "empty"
    mailbox.mkdir()
    result = run_checker(mailbox)
    assert result.returncode == 2
    assert "PLAN.md not found" in result.stderr


def test_status_and_iteration_keys_parse_and_analyze(git_repo: Path) -> None:
    """Slices carrying the optional status/iteration keys parse and analyze
    exactly like plain slices; commit matching is unaffected."""
    plan = """\
```yaml
slices:
  - id: alpha
    writes: [a.py]
    reads: []
    status: complete
    iteration: 1
```
"""
    write_plan(git_repo, plan)
    commit(git_repo, {"loop/PLAN.md": plan}, "plan: declare slice")
    sha = commit(git_repo, {"a.py": "A\n"}, "slice(alpha): add module a")

    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    alpha = report["slices"][0]
    assert alpha["id"] == "alpha"
    assert alpha["repo_status"] == "ok"
    assert alpha["commits"] == [sha]
    assert alpha["actual_touched"] == ["a.py"]
    assert alpha["undeclared_touches"] == []
    assert report["summary"]["total_slices"] == 1


def test_cumulative_history_covers_completed_and_new_slices(git_repo: Path) -> None:
    """A cumulative slices block keeps old completed entries next to the new
    iteration's: both are resolved and reported; a completed slice with
    commits still matches, and only the commit-less new slice counts in
    slices_without_commits."""
    write_plan(git_repo, PLAN_CUMULATIVE)
    commit(git_repo, {"loop/PLAN.md": PLAN_CUMULATIVE}, "plan: declare slices")
    commit(git_repo, {"a.py": "A\n"}, "slice(alpha): add module a")

    result = run_checker(git_repo, "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    by_id = {s["id"]: s for s in report["slices"]}
    assert set(by_id) == {"alpha", "fresh"}
    assert by_id["alpha"]["commits"]  # completed slice still resolved
    assert by_id["fresh"]["commits"] == []
    summary = report["summary"]
    assert summary["total_slices"] == 2
    assert summary["slices_without_commits"] == 1

    human = run_checker(git_repo)
    assert human.returncode == 0
    assert "alpha" in human.stdout and "fresh" in human.stdout
    assert "no slice-prefixed commits" in human.stdout

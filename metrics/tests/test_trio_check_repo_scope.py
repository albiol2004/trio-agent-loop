"""r15 guard: trio-check refuses slices that write outside the mailbox repo.

Spec r15 item 7 ("refuse rather than limp"): a slice whose `writes:` or
builder-brief `## Targeted check` `cd` resolves outside the mailbox repo
(absolute elsewhere, `..` escape, or a gitignored nested clone with its own
`.git`) and is not covered by a declared `repos:` entry makes trio-check
exit 2 with one exact line per slice. Since r15 multi-repo, a declared
`repos:` block is parsed and validated, and a slice that names its repo
passes (tests/test_r15_multi_repo_check.py covers the declared mode).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).parents[1] / "trio-check.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}
def message(slice_id: str, path: Path | str) -> str:
    return (
        f"slice {slice_id} writes outside the mailbox repo ({path}); declare "
        "it in PLAN.md repos: (r15) or move the mailbox into that repo"
    )


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout


def plan(slices: str, header: str = "") -> str:
    return f"# PLAN\n\n{header}## Slices\n\n```yaml\nslices:\n{slices}```\n"


OK_SLICE = "  - id: home-ok\n    writes: [src/ok.py]\n    reads: []\n"
NESTED_SLICE = "  - id: bridge\n    writes: [app-backend/app/x.py]\n    reads: []\n"
NESTED_BRIEF = (
    "# Brief\n\n## Targeted check\n\ncd app-backend && python3 -m pytest -q\n"
)


def make_home(tmp_path: Path, plan_text: str, briefs: dict[str, str] | None = None) -> Path:
    """Home repo with a gitignored nested clone `app-backend/` (own .git)."""
    home = tmp_path / "home"
    box = home / "loop"
    box.mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("app-backend/\n")
    (home / "src").mkdir()
    (home / "src" / "ok.py").write_text("x = 1\n")
    nested = home / "app-backend"
    (nested / "app").mkdir(parents=True)
    git(nested, "init", "-q", "-b", "dev")
    # The declared `base: feat/x` below must be a branch (eval-r15 N9).
    git(nested, "commit", "-q", "--allow-empty", "-m", "init")
    git(nested, "branch", "feat/x")
    (box / "GOAL.md").write_text("# Goal\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 3\nstatus: running\nmission: m\n"
    )
    (box / "PLAN.md").write_text(plan_text)
    (box / "REPORT.md").write_text("# Report\n")
    (box / "VERDICT.md").write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    if briefs:
        (box / "briefs").mkdir()
        for slice_id, text in briefs.items():
            (box / "briefs" / f"{slice_id}.md").write_text(text)
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "init")
    return home


def check(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(target), "--no-prompt-sync"],
        capture_output=True, text=True,
    )


def refusal_lines(proc: subprocess.CompletedProcess) -> list[str]:
    prefix = "trio-check: loop: "
    return [ln[len(prefix):] for ln in proc.stderr.splitlines() if ln.startswith(prefix)]


def test_nested_clone_writes_and_check_refused_exit_2(tmp_path):
    home = make_home(tmp_path, plan(OK_SLICE + NESTED_SLICE), {"bridge": NESTED_BRIEF})
    proc = check(home)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert refusal_lines(proc) == [message("bridge", home / "app-backend" / "app" / "x.py")]
    assert "REFUSED: " + message("bridge", home / "app-backend" / "app" / "x.py") in proc.stdout


def test_mailbox_dir_path_also_refused(tmp_path):
    home = make_home(tmp_path, plan(NESTED_SLICE))
    assert check(home / "loop").returncode == 2


@pytest.mark.parametrize(
    "writes, expected",
    [
        ("[../elsewhere/x.py]", lambda home: home.parent / "elsewhere" / "x.py"),
        ("[/opt/other/x.py]", lambda home: Path("/opt/other/x.py")),
        ("[src/../../escape.py]", lambda home: home.parent / "escape.py"),
        ("[app-backend]", lambda home: home / "app-backend"),
    ],
)
def test_outside_write_shapes(tmp_path, writes, expected):
    slices = f"  - id: bad\n    writes: {writes}\n    reads: []\n"
    home = make_home(tmp_path, plan(OK_SLICE + slices))
    proc = check(home)
    assert proc.returncode == 2
    assert refusal_lines(proc) == [message("bad", expected(home))]


@pytest.mark.parametrize(
    "command, expected",
    [
        ("cd app-backend && pytest -q", lambda home: home / "app-backend"),
        ("cd ../.. && pytest -q", lambda home: home.parent.parent),
        ("cd {home}/app-backend/app && pytest -q", lambda home: home / "app-backend" / "app"),
        ("cd src; cd ../app-backend && pytest", lambda home: home / "app-backend"),
    ],
)
def test_brief_targeted_check_cd_refused(tmp_path, command, expected):
    home_guess = (tmp_path / "home").resolve()
    brief = "## Targeted check\n\n" + command.format(home=home_guess) + "\n\n## Goal\n\ncd ../..\n"
    home = make_home(tmp_path, plan(OK_SLICE), {"home-ok": brief})
    proc = check(home)
    assert proc.returncode == 2
    assert refusal_lines(proc) == [message("home-ok", expected(home.resolve()))]


def test_brief_cd_inside_repo_and_outside_section_is_fine(tmp_path):
    brief = "## Targeted check\n\ncd src && python3 -m pytest -q\n\n## Notes\n\ncd ../..\n"
    home = make_home(tmp_path, plan(OK_SLICE), {"home-ok": brief})
    proc = check(home)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_legacy_repo_path_into_nested_clone_refused(tmp_path):
    """Live layout (syngenta 2026-09-28): `repo: app-backend` names a
    gitignored clone inside the mailbox dir; writes are relative to it."""
    slices = (
        "  - id: backend-bridge-basis\n    repo: app-backend\n"
        "    writes: [app/scenario_kernel/levers.py]\n    reads: []\n"
    )
    home = make_home(tmp_path, plan(slices))
    clone = home / "loop" / "app-backend"
    clone.mkdir()
    git(clone, "init", "-q", "-b", "dev")
    proc = check(home)
    assert proc.returncode == 2
    assert refusal_lines(proc) == [message("backend-bridge-basis", clone)]


def test_repo_value_naming_no_directory_refused(tmp_path):
    slices = "  - id: split\n    repo: app-backend+app-frontend\n    writes: [a.py]\n    reads: []\n"
    home = make_home(tmp_path, plan(slices))
    proc = check(home)
    assert proc.returncode == 2
    assert refusal_lines(proc) == [message("split", home / "loop" / "app-backend+app-frontend")]


def test_single_repo_mailbox_unchanged(tmp_path):
    # (a `writes:` under the mailbox dir, e.g. loop/PLAN.md, is refused
    # since eval-r16rc G1: mailbox files are Lead work)
    slices = OK_SLICE + "  - id: api\n    repo: .\n    writes: [src/, \"api:Thing\", docs/PLAN.md]\n    reads: []\n"
    home = make_home(tmp_path, plan(slices), {"home-ok": "## Targeted check\n\npython3 -m pytest -q\n"})
    proc = check(home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == ""


def test_no_git_repo_skips_path_checks(tmp_path):
    home = make_home(tmp_path, plan(NESTED_SLICE))
    import shutil
    shutil.rmtree(home / ".git")
    shutil.rmtree(home / "app-backend")
    assert check(home).returncode == 0


REPOS = """```yaml
repos:
  - name: app-backend            # kebab id, unique
    path: app-backend            # relative to the mailbox repo root
    base: feat/x
```

"""


REPO_SLICE = (
    "  - id: bridge\n    repo: app-backend\n    writes: [app/x.py]\n    reads: []\n"
)
REPO_BRIEF = "# Brief\n\n## Targeted check\n\ncd app && python3 -m pytest -q\n"


def test_declared_repos_supported_since_r15(tmp_path):
    """The r15 guard's "not supported" refusal is lifted: a slice that
    names its declared repo (writes relative to it) passes."""
    home = make_home(tmp_path, plan(OK_SLICE + REPO_SLICE, REPOS), {"bridge": REPO_BRIEF})
    proc = check(home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert refusal_lines(proc) == []


def test_declared_repos_home_slice_into_clone_is_cross_repo(tmp_path):
    """A home slice still may not write into the (now declared) clone."""
    home = make_home(tmp_path, plan(OK_SLICE + NESTED_SLICE, REPOS), {"bridge": NESTED_BRIEF})
    proc = check(home)
    assert proc.returncode == 2
    target = home / "app-backend" / "app" / "x.py"
    assert refusal_lines(proc) == [
        f"slice bridge of repo home touches repo app-backend ({target}); one "
        "repo per slice: split it (depends_on: across repos is fine) (r15)"
    ]


def test_declared_repos_empty_list_is_single_repo(tmp_path):
    home = make_home(tmp_path, plan(OK_SLICE, "```yaml\nrepos: []\n```\n\n"))
    assert check(home).returncode == 0


@pytest.mark.parametrize(
    "block, error",
    [
        ("  - name: Bad_Name\n    path: app-backend\n", "'Bad_Name': name must be kebab-case"),
        ("  - name: a\n    path: app-backend\n  - name: a\n    path: app-backend\n", "'a': duplicate name"),
        ("  - name: a\n", "'a': missing `path:`"),
        ("  - name: a\n    path: nowhere\n", "does not exist"),
        ("  - name: a\n    path: src\n", "is not a git repository"),
        ("  - name: home\n    path: app-backend\n", "`home` is reserved"),
        ("  - name: a\n    path: app-backend\n    branch: x\n", "unexpected key 'branch'"),
    ],
)
def test_repos_block_validated(tmp_path, block, error):
    header = f"```yaml\nrepos:\n{block}```\n\n"
    home = make_home(tmp_path, plan(OK_SLICE, header))
    proc = check(home)
    assert proc.returncode == 2
    lines = refusal_lines(proc)
    assert any(error in ln for ln in lines), lines


def test_json_report_carries_refusals(tmp_path):
    import json
    home = make_home(tmp_path, plan(NESTED_SLICE))
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(home), "--json", "--no-prompt-sync"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    report = json.loads(proc.stdout)
    assert report["summary"]["refused"] == 1
    assert report["loops"][0]["refusals"] == [
        message("bridge", home / "app-backend" / "app" / "x.py")
    ]

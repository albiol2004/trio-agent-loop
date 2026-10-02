"""Test isolation for trio-opencode: every test runs with its own HOME/XDG
dirs and never touches the real ``~/.local/{state,share}/trio-agent-loop``,
``~/.config/opencode`` or ``~/.local/share/opencode`` (SPEC.md "Hard
constraints" / test isolation).

TEST HYGIENE (SPEC.md "TEST HYGIENE BUG"): a real ``opencode serve
--service`` was once started by a test because a real install on the host
PATH (``~/.local/bin/opencode`` / ``~/.opencode/bin/opencode``) shadowed the
fake. ``_isolated_env`` below strips every PATH entry whose ``opencode``
executable is not under this test's own ``tmp_path`` BEFORE the test body
runs, and asserts after it that ``shutil.which("opencode")`` still resolves
nowhere outside ``tmp_path`` (or nowhere at all) — a violation aborts the
whole test session (``pytest.exit``), not just the one test, since it means
a real binary was reachable and may already have been invoked."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
OPENCODE_DRIVER_ROOT = TESTS_DIR.parent
REPO_ROOT = TESTS_DIR.parents[1]

if str(OPENCODE_DRIVER_ROOT) not in sys.path:
    sys.path.insert(0, str(OPENCODE_DRIVER_ROOT))

#: Real-install directories that must never be reachable as `opencode` from
#: inside a test, named literally (SPEC.md) rather than derived from the
#: sanitized $HOME this fixture is about to set.
_REAL_HOME = Path(os.environ.get("HOME") or Path.home())
_BANNED_OPENCODE_DIRS = (str(_REAL_HOME / ".local" / "bin"), str(_REAL_HOME / ".opencode" / "bin"))


def _sanitized_path(tmp_path: Path) -> str:
    """The current PATH with every entry removed whose own ``opencode``
    executable does not resolve under ``tmp_path`` — not just the two named
    real-install directories, but any other real install a differently
    configured host might have on PATH too."""
    tmp_resolved = str(tmp_path.resolve())
    kept: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if entry in _BANNED_OPENCODE_DIRS:
            continue
        candidate = Path(entry) / "opencode"
        if candidate.exists():
            try:
                resolved = str(candidate.resolve())
            except OSError:
                resolved = str(candidate)
            if not resolved.startswith(tmp_resolved):
                continue
        kept.append(entry)
    return os.pathsep.join(kept)

PLAN = """\
# Plan
```yaml
slices:
  - id: app
    writes: [app.py]
    reads: []
```
"""


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # PATH hygiene FIRST, before anything else touches os.environ — a bare
    # `shutil.which("opencode")` (no explicit `path=`) anywhere in this
    # process (ours, the fake's own detect_cli probe, a helper script) must
    # never resolve to a real install (SPEC.md "TEST HYGIENE BUG").
    monkeypatch.setenv("PATH", _sanitized_path(tmp_path))

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("TRIO_OPENCODE_RUNS_DIR", str(tmp_path / "opencode-runs"))
    monkeypatch.setenv("TRIO_NATIVE_RUNS_DIR", str(tmp_path / "native-runs"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)
    # HOME is a fresh tmp dir with no ~/.gitconfig: every git subprocess in
    # this process (ours and rootfree.py's/steplib's own) needs an identity
    # to commit, including the loop-mailbox and Lead-worktree "seed"/"SHIP"
    # commits the tests make.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "trio-opencode-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "trio-opencode-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.test")
    monkeypatch.setenv("TRIO_RETIREMENT_WAIT_SECONDS", "0")
    # config.validate()'s idle_seconds >= 180 floor is a production rule;
    # every test that builds a tiny-timeout Config needs it lifted (SPEC.md
    # task 5 / driver.run()'s new validate-before-anything-else gate) —
    # global, so no individual test call site needs editing for it.
    monkeypatch.setenv("TRIO_OPENCODE_TEST_TIMEOUTS", "1")
    # The acceptance author runs under bwrap where the host can start a user
    # namespace; the fake `opencode` and its scenario files live outside that
    # sandbox, so by default tests take the `no-shell` level on every host.
    # The sandbox tests opt back in (TRIO_OPENCODE_AUTHOR_ISOLATION=auto).
    monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "no-shell")

    yield

    # Guard: if anything during the test widened PATH back to a real
    # install (or the test's own env-building helpers forgot to sanitize),
    # fail loudly and STOP the whole run — this is exactly the class of bug
    # that let a real `opencode serve --service` start once already.
    found = shutil.which("opencode")
    if found:
        try:
            resolved = str(Path(found).resolve())
        except OSError:
            resolved = found
        if not resolved.startswith(str(tmp_path.resolve())):
            pytest.exit(
                "trio-opencode test hygiene violation: shutil.which('opencode') "
                f"resolved to a REAL binary outside tmp: {found} "
                f"(during {os.environ.get('PYTEST_CURRENT_TEST', '?')})",
                returncode=1,
            )


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-opencode-test", GIT_COMMITTER_EMAIL="t@example.test",
        TRIO_RETIREMENT_WAIT_SECONDS="0",
    )
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=git_env(),
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A git repo with an initialized ``loop/`` mailbox: GOAL.md, STATE.md in
    the format ``metrics/trio_loop.py``'s ``_read_state`` expects (the
    minimal fixture shape of ``native/tests/test_step_ops.py``)."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip app.py\n", encoding="utf-8")
    (box / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\ncustom: keep\n",
        encoding="utf-8",
    )
    (box / "PLAN.md").write_text(PLAN, encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    git(root, "add", "loop")
    git(root, "commit", "-q", "-m", "loop: init")
    return root

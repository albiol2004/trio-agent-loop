"""r15 guard fixes (eval-r15a F3-F7, repos: parser decisions).

A parametrised allowed/refused harness over `repo_scope_refusals` (the
reviewer's case list plus every bypass it found), the fail-closed rule for
an unparseable slices block, PLAN-level targeted/full checks, symlink
loops, and the `repos:` block decisions (same path twice, a non-home name
for the mailbox repo, env vars never expand, flow maps, any key order).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

CHECKER = Path(__file__).parents[1] / "trio-check.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}


def _load():
    loader = importlib.machinery.SourceFileLoader("trio_check_fixes", str(CHECKER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module, module.load_trio_metrics()


TC, TM = _load()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, **GIT_ENV))


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "loop" / "briefs").mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("app-backend/\n")
    (home / "src").mkdir()
    (home / "node_modules" / "dep").mkdir(parents=True)
    (home / "node_modules" / "dep" / ".git").mkdir()
    (home / "app-backend" / "app").mkdir(parents=True)
    git(home / "app-backend", "init", "-q", "-b", "dev")
    (tmp_path / "outside").mkdir()
    return home


def run(home: Path, *, writes: str = "[src/x.py]", repo: str | None = None,
        brief: str | None = None, extra_plan: str = "", slice_id: str | None = None,
        brief_text: str | None = None) -> list[str]:
    box = home / "loop"
    repo_line = f"    repo: {repo}\n" if repo is not None else ""
    (box / "PLAN.md").write_text(
        f"# PLAN\n\n{extra_plan}\n```yaml\nslices:\n  - id: s1\n{repo_line}"
        f"    writes: {writes}\n    reads: []\n```\n"
    )
    brief_path = box / "briefs" / "s1.md"
    if brief is not None:
        brief_path.write_text(brief)
    elif brief_path.exists():
        brief_path.unlink()
    return TC.repo_scope_refusals(box, TM, slice_id=slice_id, brief_text=brief_text)


def check(body: str) -> str:
    return f"# Brief\n\n## Targeted check\n\n{body}\n"


REFUSED = [
    # writes
    ("nested clone write", dict(writes="[app-backend/app/x.py]")),
    ("dir glob", dict(writes="[app-backend/**]")),
    ("dot-slash", dict(writes="[./app-backend/app/x.py]")),
    ("dotdot inside", dict(writes="[src/../app-backend/app/x.py]")),
    ("escape", dict(writes="[../outside/x.py]")),
    ("absolute", dict(writes="[/tmp/x.py]")),
    ("tilde", dict(writes="[~/x.py]")),
    ("F5 prefix glob", dict(writes="[app-*/x.py]")),
    ("F5 star dir", dict(writes="[*/app/x.py]")),
    ("F5 question", dict(writes="[app-backen?/x.py]")),
    ("nested .git in dep", dict(writes="[node_modules/dep/patch.js]")),
    # repo:
    ("repo names clone", dict(repo="app-backend", writes="[app/x.py]")),
    ("repo names nothing", dict(repo="ghost", writes="[app/x.py]")),
    # brief cds
    ("plain cd", dict(brief=check("cd app-backend && pytest -q"))),
    ("fenced", dict(brief=check("```bash\ncd app-backend\npytest -q\n```"))),
    ("subshell", dict(brief=check("(cd app-backend && pytest -q)"))),
    ("chained", dict(brief=check("cd src && cd ../app-backend && pytest"))),
    ("abs cd", dict(brief=check("cd /tmp && pytest"))),
    ("F3 inline code", dict(brief=check("Run `cd app-backend && pytest -q`."))),
    ("F3 list code", dict(brief=check("- `cd app-backend && pytest -q`"))),
    ("F3 pushd", dict(brief=check("pushd app-backend && pytest"))),
    ("F3 h3 heading", dict(brief="## Task\n\n### Targeted check\n\ncd app-backend && pytest\n")),
    ("F3 plural heading", dict(brief="## Targeted checks\n\ncd app-backend && pytest\n")),
    ("F3 suffixed heading", dict(brief="## Targeted check (backend)\n\ncd app-backend\n")),
    ("F3 bold label", dict(brief="**Targeted check:**\n\ncd app-backend && pytest\n")),
    ("F3 second section", dict(brief=check("pytest -q") + "\n## Notes\n\nx\n\n" + check("cd app-backend"))),
    ("F3 git -C", dict(brief=check("git -C app-backend status && pytest"))),
    ("F3 make -C", dict(brief=check("make -C app-backend test"))),
    ("F3 rootdir", dict(brief=check("pytest --rootdir=app-backend -q"))),
    ("F3 prefix", dict(brief=check("npm --prefix app-backend test"))),
    ("F3 cwd flag", dict(brief=check("tool --cwd app-backend run"))),
    # PLAN level (F6)
    ("F6 plan targeted check", dict(writes="[app/x.py]",
                                    extra_plan="## Targeted check\n\ncd app-backend && pytest\n")),
    ("F6 plan full_check", dict(extra_plan="full_check: cd app-backend && pytest -q\n")),
    ("F6 plan full_check block", dict(extra_plan="full_check:\n  cd /tmp && pytest -q\n")),
]

ALLOWED = [
    ("home write", dict()),
    ("repo home", dict(repo="home")),
    ("repo dotdot to root", dict(repo="..")),
    ("cd var", dict(brief=check('cd "$VAR" && pytest'))),
    ("cd dash", dict(brief=check("cd - && pytest"))),
    ("no brief", dict()),
    ("new dir", dict(writes="[newdir/sub/x.py]")),
    ("dot", dict(writes="[.]")),
    ("dot slash", dict(writes="[./]")),
    ("api", dict(writes="[api:foo]")),
    ("node_modules dir", dict(writes="[node_modules/, src/x.py]")),
    ("home glob", dict(writes="[src/*.py]")),
    ("cd inside", dict(brief=check("cd src && pytest -q"))),
    ("F3 comment is not a cd", dict(brief=check("pytest -q  # never cd app-backend here"))),
    ("plan full_check home", dict(extra_plan="full_check: python3 -m pytest -q\n")),
]


@pytest.mark.parametrize("name, case", REFUSED, ids=[c[0] for c in REFUSED])
def test_refused(home, name, case):
    assert run(home, **case), f"{name} should be refused"


@pytest.mark.parametrize("name, case", ALLOWED, ids=[c[0] for c in ALLOWED])
def test_allowed(home, name, case):
    assert run(home, **case) == [], f"{name} should be allowed"


def test_unknown_slice_brief_is_checked(home):
    """F3: `run builder --isolate --worker-slice <id not in PLAN>`."""
    lines = run(home, slice_id="ghost", brief_text=check("cd app-backend && pytest"))
    assert lines == [TC.repo_scope_message("ghost", home / "app-backend")]


def test_malformed_slices_block_fails_closed(home):
    """F4: a slices block that does not parse is refused, never allowed."""
    box = home / "loop"
    (box / "PLAN.md").write_text(
        "```yaml\nslices:\n  - id: s1\n    notes: hi\n    writes: [app-backend/app/x.py]\n```\n"
    )
    lines = TC.repo_scope_refusals(box, TM)
    assert len(lines) == 1 and "slices block does not parse" in lines[0]


def test_no_slices_block_yet_is_not_refused(home):
    (home / "loop" / "PLAN.md").write_text("# PLAN\n\nskeleton only\n")
    assert TC.repo_scope_refusals(home / "loop", TM) == []


def test_symlink_loop_is_not_a_traceback(home):
    """F7: a symlink loop resolves to an unresolved path, no RuntimeError."""
    (home / "src" / "a").symlink_to(home / "src" / "b")
    (home / "src" / "b").symlink_to(home / "src" / "a")
    lines = run(home, writes="[src/a/x.py]")
    assert isinstance(lines, list)


# --- repos: parser decisions (reviewer Q2) ------------------------------------


def repos(home: Path, block: str):
    text = f"```yaml\nrepos:\n{block}```\n"
    return TC.parse_repos_block(text, home)


def test_same_path_under_two_names_is_rejected(home):
    got, errors = repos(home, "  - name: a\n    path: app-backend\n  - name: b\n    path: app-backend/\n")
    assert [r["name"] for r in got] == ["a"]
    assert any("already declared as 'a'" in e for e in errors)


def test_non_home_name_for_the_mailbox_repo_is_rejected(home):
    got, errors = repos(home, "  - name: main\n    path: .\n")
    assert got == [] and any("implicit `home` repo" in e for e in errors)


def test_home_with_dot_is_dropped(home):
    assert repos(home, "  - name: home\n    path: .\n") == ([], [])


def test_env_vars_never_expand(home):
    got, errors = repos(home, "  - name: a\n    path: $HOME/x\n")
    assert got == [] and any("only `~` expands" in e for e in errors)


def test_flow_map_and_any_key_order(home):
    got, errors = repos(
        home,
        "  - {name: a, path: app-backend, base: dev}\n"
        "  - path: ../outside\n    name: b\n",
    )
    assert errors == [] or all("not a git repository" in e for e in errors)
    assert got[0]["name"] == "a" and got[0]["base"] == "dev"


def test_entry_without_name_is_rejected(home):
    _got, errors = repos(home, "  - path: app-backend\n")
    assert any("has no `name:`" in e for e in errors)

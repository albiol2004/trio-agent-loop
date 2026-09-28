"""r15 multi-repo: parsers (trio-metrics), trio-check declared mode,
trio-shadow per-repo resolution, and the loop core's per-repo pins and
retirement contract (unit level; the end-to-end run is in
omnigent/tests/test_r15_multi_repo_e2e.py)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from metrics import trio_loop

TM = trio_loop._METRICS
CHECKER = Path(__file__).parents[1] / "trio-check.py"
SHADOW = Path(__file__).parents[1] / "trio-shadow.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}


def _load_checker():
    loader = importlib.machinery.SourceFileLoader("trio_check_r15m", str(CHECKER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TC = _load_checker()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env=dict(os.environ, **GIT_ENV),
    ).stdout.strip()


REPOS = (
    "```yaml\nrepos:\n  - name: app-backend\n    path: app-backend\n    base: dev\n"
    "  - name: ext\n    path: {ext}\n```\n\n"
)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    box = home / "loop"
    (box / "briefs").mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("app-backend/\n")
    (home / "src").mkdir()
    (home / "src" / "ok.py").write_text("x = 1\n")
    (box / "GOAL.md").write_text("# Goal\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 3\nstatus: running\nmission: m\n"
    )
    for name in ("REPORT.md", "VERDICT.md"):
        (box / name).write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "init")
    back = home / "app-backend"
    (back / "app").mkdir(parents=True)
    (back / "app" / "core.py").write_text("y = 1\n")
    git(back, "init", "-q", "-b", "dev")
    git(back, "add", "-A")
    git(back, "commit", "-q", "-m", "init")
    ext = tmp_path / "elsewhere" / "ext"
    (ext / "lib").mkdir(parents=True)
    (ext / "lib" / "m.py").write_text("z = 1\n")
    git(ext, "init", "-q", "-b", "main")
    git(ext, "add", "-A")
    git(ext, "commit", "-q", "-m", "init")
    return home


def plan(home: Path, slices: str, extra: str = "") -> Path:
    box = home / "loop"
    ext = home.parent / "elsewhere" / "ext"
    (box / "PLAN.md").write_text(
        "# PLAN\n\n" + REPOS.format(ext=ext) + extra
        + "\n```yaml\nslices:\n" + slices + "```\n"
    )
    return box


def slice_(sid: str, writes: str, repo: str | None = None) -> str:
    repo_line = f"    repo: {repo}\n" if repo else ""
    return f"  - id: {sid}\n{repo_line}    writes: [{writes}]\n    reads: []\n"


def refusals(box: Path, **kw) -> list[str]:
    return TC.repo_scope_refusals(box, TM, **kw)


# --- parsers ------------------------------------------------------------------


def test_parse_repos_block_resolves_relative_and_absolute(home):
    box = plan(home, slice_("s", "src/x.py"))
    info = TM.read_repos(box)
    assert info["errors"] == [] and info["declared"]
    got = {r["name"]: r for r in info["repos"]}
    assert got["app-backend"]["path"] == home / "app-backend"
    assert got["app-backend"]["base"] == "dev"
    assert got["ext"]["path"] == home.parent / "elsewhere" / "ext"
    assert got["ext"]["base"] is None


def test_read_repos_single_repo(tmp_path):
    (tmp_path / "PLAN.md").write_text("# PLAN\n```yaml\nslices:\n  - id: a\n```\n")
    assert TM.read_repos(tmp_path)["declared"] is False
    assert TM.read_repos(tmp_path / "missing")["repos"] == []


@pytest.mark.parametrize("value, name", [
    (".", "home"), ("home", "home"), ("", "home"), ("app-backend", "app-backend"),
    ("ghost", None), ("../x", None),
])
def test_slice_repo_name(value, name):
    assert TM.slice_repo_name({"repo": value}, ["app-backend"]) == name


@pytest.mark.parametrize("values, pins", [
    (["abc1234"], [("home", "abc1234")]),
    (["home@abc1234, app-backend@DEF5678"], [("home", "abc1234"), ("app-backend", "def5678")]),
    (["`app-backend@abc1234`", "ext@" + "a" * 40], [("app-backend", "abc1234"), ("ext", "a" * 40)]),
    (["not-a-sha", "x@zz", "HEAD"], []),
])
def test_parse_repo_pins(values, pins):
    assert TM.parse_repo_pins(values) == pins


@pytest.mark.parametrize("text, checks, errors", [
    ("full_check: pytest -q\n", {"home": "pytest -q"}, 0),
    ("full_check:\n  cd app && npx tsc --noEmit\nfull_check_budget_s: 60\n",
     {"home": "cd app && npx tsc --noEmit"}, 0),
    ('full_check: { app-backend: "pytest -q", home: "make test" }\n',
     {"app-backend": "pytest -q", "home": "make test"}, 0),
    ("full_check:\n  app-backend: pytest -q\n  ext: npm test\n",
     {"app-backend": "pytest -q", "ext": "npm test"}, 0),
    ("full_check:\n  app-backend: pytest -q\n  plain command\n", {}, 1),
    ("full_check: { a: x\n", {}, 1),
    ("no check here\n", {}, 0),
    ("```\nfull_check: inside fence\n```\n", {}, 0),
])
def test_parse_full_check(text, checks, errors):
    got, errs = TM.parse_full_check(text)
    assert got == checks and len(errs) == errors, errs


# --- trio-check declared mode ---------------------------------------------------


def test_declared_slices_pass(home):
    box = plan(home, slice_("home-a", "src/x.py") + slice_("be", "app/x.py", "app-backend")
               + slice_("ex", "lib/n.py", "ext"),
               "full_check:\n  app-backend: python3 -m pytest -q\n  home: true\n")
    (box / "briefs" / "be.md").write_text("## Targeted check\n\ncd app && python3 -m pytest -q\n")
    assert refusals(box) == []


@pytest.mark.parametrize("slices, brief, fragment", [
    (slice_("be", "../src/x.py", "app-backend"), None, "of repo app-backend touches repo home"),
    (slice_("be", "../../x.py", "app-backend"), None, "writes outside its repo app-backend"),
    (slice_("be", "app/x.py", "app-backend"), "cd .. && pytest", "touches repo home"),
    (slice_("home-a", "app-backend/app/x.py"), None, "of repo home touches repo app-backend"),
    (slice_("be", "app/x.py", "ghost"), None, "repo: 'ghost' is not declared"),
    (slice_("be", "app/x.py", "app-backend"), "cd {abs}/app && pytest", "main checkout of repo app-backend"),
    (slice_("ex", "lib/x.py", "ext"), "cd ../../home/src", "touches repo home"),
    (slice_("ex", "../outside.py", "ext"), None, "writes outside its repo ext"),
])
def test_declared_slices_refused(home, slices, brief, fragment):
    box = plan(home, slices)
    sid = slices.split("id: ")[1].split("\n")[0]
    if brief:
        (box / "briefs" / f"{sid}.md").write_text(
            "## Targeted check\n\n" + brief.format(abs=home / "app-backend") + "\n")
    lines = refusals(box)
    assert len(lines) == 1 and fragment in lines[0], lines


def test_undeclared_outside_path_refused_with_the_exact_message(home):
    box = plan(home, slice_("home-a", "../outside/x.py"))
    assert refusals(box) == [TC.repo_scope_message("home-a", home.parent / "outside" / "x.py")]
    proc = subprocess.run([sys.executable, str(CHECKER), str(box), "--no-prompt-sync"],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert TC.repo_scope_message("home-a", home.parent / "outside" / "x.py") in proc.stderr


@pytest.mark.parametrize("extra, fragment", [
    ("full_check:\n  nope: pytest\n", "repo 'nope' is not declared"),
    ("full_check:\n  app-backend: cd .. && pytest\n", "full_check: command of repo app-backend runs outside"),
    ("## Targeted check\n\ncd ../outside && pytest\n", "PLAN.md targeted/full check runs outside"),
])
def test_declared_plan_level_checks(home, extra, fragment):
    box = plan(home, slice_("home-a", "src/x.py"), extra)
    lines = refusals(box)
    assert any(fragment in ln for ln in lines), lines


def test_retired_repo_is_validated(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("home-a", "src/x.py"))
    sha = "a" * 40
    (box / "QUEUE.md").write_text(
        "```yaml\nretired:\n"
        f"  - slice: be\n    repo: app-backend\n    sha: {sha}\n    at: 2026-09-28T00:00:00Z\n"
        f"  - slice: home-a\n    sha: {sha}\n    at: 2026-09-28T00:00:00Z\n"
        "```\n\n```yaml\nfaults:\n```\n")
    assert TC.check_queue(box, TM, TC._plan_slices(box, TM)) == []
    (box / "QUEUE.md").write_text(
        "```yaml\nretired:\n"
        f"  - slice: be\n    sha: {sha}\n    at: 2026-09-28T00:00:00Z\n"
        f"  - slice: home-a\n    repo: ghost\n    sha: {sha}\n    at: 2026-09-28T00:00:00Z\n"
        "```\n\n```yaml\nfaults:\n```\n")
    errors = TC.check_queue(box, TM, TC._plan_slices(box, TM))
    assert any("has repo 'home' but PLAN.md puts that slice in repo 'app-backend'" in e for e in errors)
    assert any("has repo 'ghost', not declared" in e for e in errors)


def test_single_repo_retired_repo_key_is_rejected(tmp_path):
    box = tmp_path / "loop"
    box.mkdir()
    (box / "PLAN.md").write_text("```yaml\nslices:\n  - id: a\n    writes: [x]\n```\n")
    (box / "QUEUE.md").write_text(
        "```yaml\nretired:\n  - slice: a\n    repo: other\n    sha: " + "b" * 40
        + "\n    at: t\n```\n")
    errors = TC.check_queue(box, TM, TC._plan_slices(box, TM))
    assert any("not declared in PLAN.md `repos:`" in e for e in errors)


# --- trio-shadow ---------------------------------------------------------------------


def test_shadow_resolves_declared_repo_commits_and_per_repo_hazards(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("home-a", "app/x.py"))
    back = home / "app-backend"
    (back / "app" / "x.py").write_text("1\n")
    git(back, "add", "-A")
    git(back, "commit", "-q", "-m", "slice(be): x")
    proc = subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(box),
                           "--require-commits", "--slice", "be"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    shadow = _load_shadow()
    report = shadow.analyze(box)
    be = next(e for e in report["slices"] if e["id"] == "be")
    assert be["repo_path"] == str(back) and be["actual_touched"] == ["app/x.py"]
    # home-a has no commit in home: the gate fails for it alone.
    proc = subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(box),
                           "--require-commits", "--slice", "home-a"], capture_output=True, text=True)
    assert proc.returncode == 1
    # Same relative path in two repos is never a hazard.
    fake = [dict(be, iteration=1), dict(be, id="home-a", repo_path=str(home), iteration=1)]
    assert shadow.mailbox_pairwise_hazards(fake) == []


def _load_shadow():
    loader = importlib.machinery.SourceFileLoader("trio_shadow_r15m", str(SHADOW))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_iteration_path_sets_are_repo_qualified():
    sets = TM.iteration_path_sets([
        {"iteration": 1, "repo": "app-backend", "writes": ["app/x.py"], "reads": []},
        {"iteration": 1, "repo": ".", "writes": ["app/x.py"], "reads": []},
    ])
    assert sets[1]["writes"] == {"app-backend:app/x.py", "app/x.py"}


# --- loop core: pins and retirement ---------------------------------------------------


def _core_state(box: Path, **extra) -> None:
    lines = ["schema: 1", "iteration: 1", "max_iterations: 3", "status: running", "mission: m"]
    lines += [f"{k}: {v}" for k, v in extra.items()]
    (box / "STATE.md").write_text("\n".join(lines) + "\n")


def test_single_repo_state_never_gets_evaluated_repos(tmp_path):
    state = tmp_path / "STATE.md"
    state.write_text("iteration: 1\nstatus: running\n")
    trio_loop._update_state(state, {"evaluator_attempt": "", "evaluated_sha": "", "evaluated_repos": ""})
    assert "evaluated_repos" not in state.read_text()
    trio_loop._update_state(state, {"evaluated_repos": "a@" + "1" * 40})
    trio_loop._update_state(state, {"evaluated_repos": ""})
    assert "evaluated_repos: \n" in state.read_text()  # an existing key is cleared


def test_integration_context_pins_every_repo_and_reuses_intact_pins(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    _core_state(box)
    ctx = trio_loop._open_loop_integration_context(box, home, 3, box / "STATE.md")
    heads = {"home": git(home, "rev-parse", "HEAD"),
             "app-backend": git(home / "app-backend", "rev-parse", "HEAD"),
             "ext": git(home.parent / "elsewhere" / "ext", "rev-parse", "HEAD")}
    assert ctx["pins"] == heads
    state = (box / "STATE.md").read_text()
    assert f"evaluated_repos: app-backend@{heads['app-backend']} ext@{heads['ext']}" in state
    again = trio_loop._open_loop_integration_context(box, home, 3, box / "STATE.md")
    assert again["evaluator_attempt"] == ctx["evaluator_attempt"]
    ext = home.parent / "elsewhere" / "ext"
    (ext / "lib" / "m.py").write_text("changed\n")
    git(ext, "commit", "-qam", "later")
    fresh = trio_loop._open_loop_integration_context(box, home, 3, box / "STATE.md")
    assert fresh["evaluator_attempt"] != ctx["evaluator_attempt"]
    assert fresh["pins"]["ext"] == git(ext, "rev-parse", "HEAD")


def test_single_repo_integration_context_is_unchanged(tmp_path):
    repo = tmp_path / "r"
    (repo / "loop").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "i")
    _core_state(repo / "loop")
    ctx = trio_loop._open_loop_integration_context(repo / "loop", repo, 1, repo / "loop" / "STATE.md")
    assert "pins" not in ctx
    assert "evaluated_repos" not in (repo / "loop" / "STATE.md").read_text()


def _ship(box: Path, home: Path, *, iteration=1, evaluated=None, retire=("app-backend", "ext"),
          commit_lines=()):
    heads = {"app-backend": git(home / "app-backend", "rev-parse", "HEAD"),
             "ext": git(home.parent / "elsewhere" / "ext", "rev-parse", "HEAD")}
    home_sha = git(home, "rev-parse", "HEAD")
    _core_state(box, evaluated_sha=home_sha, evaluator_attempt="att",
                evaluated_repos=f"app-backend@{heads['app-backend']} ext@{heads['ext']}")
    paths = {"app-backend": home / "app-backend", "ext": home.parent / "elsewhere" / "ext"}
    for name in retire:
        git(paths[name], "commit", "-q", "--allow-empty", "-m",
            f"loop: iteration {iteration} — SHIP (loop)")
    ev = evaluated or f"home@{home_sha}, app-backend@{heads['app-backend']}, ext@{heads['ext']}"
    (box / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n\niteration: {iteration}\nattempt: att\nevaluated: {ev}\n"
        + "".join(f"commit: {c}\n" for c in commit_lines))
    git(home, "add", "-A", "loop")
    git(home, "commit", "-q", "-m", f"loop: iteration {iteration} — SHIP")
    return heads, paths


def test_multi_repo_ship_retirement_complete(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    _ship(box, home)
    assert trio_loop._ship_retirement_problem(box, 1, home) is None


def test_missing_repo_retirement_commit_is_pending(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    _ship(box, home, retire=("app-backend",))
    kind, detail = trio_loop._ship_retirement_problem(box, 1, home)
    assert kind == trio_loop.RETIREMENT_PENDING and "in repo ext" in detail


def test_repo_without_slices_needs_no_retirement_commit(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend"))
    _ship(box, home, retire=("app-backend",))
    assert trio_loop._ship_retirement_problem(box, 1, home) is None


def test_unrecorded_repo_pin_is_final(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    home_sha = git(home, "rev-parse", "HEAD")
    _ship(box, home, evaluated=home_sha)
    kind, detail = trio_loop._ship_retirement_problem(box, 1, home)
    assert kind == trio_loop.RETIREMENT_FINAL and "does not record evaluated: app-backend@" in detail


def test_product_change_in_a_repo_after_its_pin_is_final(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    _heads, paths = _ship(box, home)
    (paths["ext"] / "lib" / "m.py").write_text("dirty\n")
    kind, detail = trio_loop._ship_retirement_problem(box, 1, home)
    assert kind == trio_loop.RETIREMENT_FINAL and "product tree changed in repo ext" in detail


def test_fabricated_repo_commit_line_is_pending(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend") + slice_("ex", "lib/n.py", "ext"))
    _ship(box, home, commit_lines=("ext@" + "f" * 40,))
    kind, detail = trio_loop._ship_retirement_problem(box, 1, home)
    assert kind == trio_loop.RETIREMENT_PENDING and "commit: ext@" in detail


def test_verdict_commit_shas_home_meaning_unchanged():
    text = "commit: abcdef1\ncommit: app-backend@1234567\ncommit: home@7654321\n"
    assert trio_loop._verdict_commit_shas(text) == ["abcdef1"]
    assert trio_loop._verdict_commit_shas(text, "app-backend") == ["1234567"]
    assert trio_loop._verdict_commit_shas(text, "home") == ["abcdef1", "7654321"]


def test_lead_pass_snapshot_sees_a_commit_in_a_declared_repo(home):
    box = plan(home, slice_("be", "app/x.py", "app-backend"))
    before = trio_loop._lead_pass_snapshot(box, home)
    git(home / "app-backend", "commit", "-q", "--allow-empty", "-m", "slice(be): x")
    assert trio_loop._lead_pass_snapshot(box, home) != before

"""eval-r16rc G1: the mailbox repo is always `home`; files under the mailbox
directory are Lead work, never a builder slice (repo-scope guard + prompts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, git, init_repo, load, plan_text, write_root_mailbox

CHECK = load("trio_check_r16rc_g1", REPO_ROOT / "metrics" / "trio-check.py")
TM = CHECK._load_metrics() if hasattr(CHECK, "_load_metrics") else None


def _tm():
    if TM is not None:
        return TM
    return load("trio_metrics_r16rc_g1", REPO_ROOT / "metrics" / "trio-metrics.py")


def _refusals(box: Path, **kw) -> list[str]:
    return CHECK.repo_scope_refusals(box, _tm(), **kw)


@pytest.fixture()
def home(tmp_path):
    repo = tmp_path / "home"
    init_repo(repo, "main", {"README.md": "r\n", "src/a.py": ""}, metrics=False)
    return repo


@pytest.mark.parametrize("write", [
    "loop/x/results/live-floor.json", "loop/x/scripts/probe.sh", "loop/x", "loop/x/*",
])
def test_home_slice_writing_under_the_mailbox_dir_is_refused(home, write):
    box = write_root_mailbox(home, "loop/x", [{"id": "live-floor", "write": write}])
    problems = _refusals(box)
    assert len(problems) == 1, problems
    assert problems[0].startswith("slice live-floor writes under the mailbox directory (")
    assert "Lead work" in problems[0] and "never a builder slice" in problems[0]
    assert "always `home`" in problems[0]
    # also per dispatch (`run builder --isolate --worker-slice live-floor`)
    assert _refusals(box, slice_id="live-floor") == problems


def test_product_and_other_mailbox_writes_are_not_mailbox_work(home):
    box = write_root_mailbox(home, "loop/x", [
        {"id": "p1", "write": "src/a.py"},
        {"id": "p2", "write": "loop/other/notes.md"},
        {"id": "p3", "write": "loop/xy/a.md"},
    ])
    assert _refusals(box) == []


def test_multi_repo_home_slice_under_mailbox_refused_clone_nested_there_is_not(home, tmp_path):
    app = home / "loop" / "x" / "app"
    (home / ".gitignore").write_text("loop/x/app/\n")
    git(home, "add", ".gitignore")
    git(home, "commit", "-q", "-m", "ignore clone")
    init_repo(app, "dev", {"app/core.py": ""}, metrics=False)
    box = write_root_mailbox(
        home, "loop/x",
        [{"id": "be", "repo": "app", "write": "app/core.py"},
         {"id": "floor", "repo": "home", "write": "loop/x/results/floor.json"},
         {"id": "home-ok", "write": "src/a.py"}],
        repos_block="  - name: app\n    path: loop/x/app\n    base: dev\n",
    )
    problems = _refusals(box)
    assert len(problems) == 1, problems
    assert problems[0].startswith("slice floor writes under the mailbox directory")


def test_coordinator_is_not_an_alias_of_home(home, tmp_path):
    app = tmp_path / "app"
    init_repo(app, "main", {"x.py": ""}, metrics=False)
    box = write_root_mailbox(
        home, "loop/x",
        [{"id": "shots", "repo": "coordinator", "write": "src/a.py"}],
        repos_block=f"  - name: app\n    path: {app}\n",
    )
    problems = _refusals(box)
    assert problems == [
        "slice shots repo: 'coordinator' is not declared in PLAN.md repos: "
        "(declared: home, app) (r15)"
    ], problems


def test_mailbox_at_the_repo_root_refuses_nothing(home):
    box = home
    for name, text in (("PLAN.md", plan_text([{"id": "r1", "write": "src/a.py"}])),
                       ("GOAL.md", "# Goal\n"), ("STATE.md", "status: ready\n")):
        (box / name).write_text(text)
    assert _refusals(box) == []


G1 = "The mailbox repo is always named `home`"


@pytest.mark.parametrize("rel", [
    "omnigent/entrypoints/trio-omnigent/prompts/lead.md",
    "prompts/canonical/lead.md",
    "omnigent/trio-omnigent-roles/lead/config.yaml",
])
def test_lead_prompt_config_state_home_and_mailbox_dir_rule(rel):
    text = " ".join((REPO_ROOT / rel).read_text().split())
    assert G1 in text and "never `coordinator`" in text
    assert "never a builder slice" in text and "under the mailbox directory" in text


def test_coordinator_skill_states_home_and_mailbox_dir_rule():
    text = " ".join((REPO_ROOT / "omnigent/entrypoints/trio-omnigent/SKILL.md").read_text().split())
    assert "always named `home`" in text and "do not invent `coordinator`" in text
    assert "never a builder slice" in text

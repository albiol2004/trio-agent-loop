"""R11: r15 declared repos under r16a root-free open-loop (fixtures B and C)."""
from __future__ import annotations

from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, tag="r16mr")


def _layout_b(world: World, tmp_path: Path):
    home = tmp_path / "home"
    init_repo(home, "main", {
        ".gitignore": "loop/x/app-backend/\nloop/x/app-frontend/\n",
        "README.md": "home\n", "docs/index.md": "docs\n",
    })
    box = home / "loop" / "x"
    init_repo(box / "app-backend", "dev", {"app/core.py": "x = 1\n"}, metrics=False)
    init_repo(box / "app-frontend", "feat/ui", {"src/app.js": "//\n"}, metrics=False)
    repos_block = (
        "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n"
        "  - name: app-frontend\n    path: loop/x/app-frontend\n    base: feat/ui\n"
    )
    slices = [
        {"id": "be-a", "repo": "app-backend", "write": "app/a.py"},
        {"id": "fe-b", "repo": "app-frontend", "write": "src/b.js"},
        {"id": "home-c", "repo": "home", "write": "docs/c.md"},
    ]
    spec = world.add_loop(
        home, "loop/x", slices, repos_block=repos_block,
        full_check="full_check:\n  app-backend: true\n  app-frontend: true\n  home: true",
    )
    return home, spec, {"app-backend": (box / "app-backend", "dev"),
                        "app-frontend": (box / "app-frontend", "feat/ui")}


def _layout_c(world: World, tmp_path: Path):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "docs/index.md": "docs\n"})
    svc = tmp_path / "elsewhere" / "svc"
    init_repo(svc, "feat/x", {"svc/main.py": "y = 2\n"}, metrics=False)
    repos_block = f"  - name: svc\n    path: {svc}\n    base: feat/x\n"
    slices = [
        {"id": "svc-a", "repo": "svc", "write": "svc/a.py"},
        {"id": "home-b", "repo": "home", "write": "docs/b.md"},
    ]
    spec = world.add_loop(
        home, "loop/x", slices, repos_block=repos_block,
        full_check='full_check: { svc: "true", home: "true" }',
    )
    return home, spec, {"svc": (svc, "feat/x")}


@pytest.mark.parametrize("kind", ["B", "C"])
def test_declared_repos_get_per_repo_aggregates_and_land_home_last(world, tmp_path, kind):
    home, spec, repos = (_layout_b if kind == "B" else _layout_c)(world, tmp_path)
    tips = {name: git(path, "rev-parse", branch) for name, (path, branch) in repos.items()}
    seen: dict = {}

    def integration(w, s, runner, ctx, workspace, box, prompt, iteration):
        record = w.rf.load_record(w.wt, home, "loop--x")
        seen["record"] = record
        seen["workspace"] = workspace
        for name, info in record["repos"].items():
            agg = Path(info["path"])
            assert git(agg, "symbolic-ref", "--short", "HEAD") == "trio/loop--x"
            if kind == "B":
                # Nested at the same relative path inside the Lead worktree,
                # and checked out at its pin inside the detached eval worktree.
                assert agg == Path(record["path"]) / "loop" / "x" / name
                nested = workspace / "loop" / "x" / name
                assert git(nested, "rev-parse", "HEAD") == ctx["pins"][name]
            else:
                assert agg.name == "lead-loop--x" and not str(agg).startswith(record["path"])
        assert "ROOT-FREE" in prompt and "MULTI-REPO" in prompt
        return False

    world.hooks["integration-eval"] = integration
    code = world.run_loop(spec)
    assert code == 0, (spec["root_box"] / "LOG.md").read_text()
    assert seen["record"]["repos"], "the integration-eval hook never ran"
    log = (home / "loop/x/LOG.md").read_text()
    # Declared repos landed first (their lines precede home's), home last.
    lines = [ln for ln in log.splitlines() if "| loop | landed trio/loop--x" in ln]
    assert len(lines) == len(repos) + 1, log
    assert lines[-1].endswith("onto main") or " onto main" in lines[-1]
    for name, (path, branch) in repos.items():
        subjects = git(path, "log", "--format=%s", branch).splitlines()
        assert subjects[0] == "loop: iteration 1 — SHIP (x)"
        assert any(s.startswith("slice(") for s in subjects)
        assert git(path, "merge-base", "--is-ancestor", tips[name], branch) == ""
        # The declared checkout was fast-forwarded (its branch is checked out there).
        assert git(path, "rev-parse", "HEAD") == git(path, "rev-parse", branch)
        assert git(path, "status", "--porcelain") == ""
        assert len(git(path, "worktree", "list").splitlines()) == 1
        assert "trio/" not in git(path, "branch", "--list")
    assert (home / "docs/c.md").is_file() or (home / "docs/b.md").is_file()
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert len(git(home, "worktree", "list").splitlines()) == 1
    assert all(Path(e["workspace"]) != home for e in world.events)

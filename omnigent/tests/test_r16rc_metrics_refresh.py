"""r16a follow-up: `trioctl omnigent metrics refresh [--repo|--mailbox] [--commit]`.

A repository that wants root-free open-loop must vendor this release's loop
core (METRICS_API 6) on the branch loops land onto; the root-free old-core
refusal names this command.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from r16_harness import METRICS_FILES, REPO_ROOT, World, git, init_repo, write_root_mailbox


@pytest.fixture()
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, tag="r16rcm")


def _old_repo(path: Path, branch: str = "main") -> Path:
    """A repo whose committed metrics/ is an older (API 4) set."""
    init_repo(path, branch, {"README.md": "r\n"})
    tm = path / "metrics" / "trio-metrics.py"
    tm.write_text(re.sub(r"^METRICS_API = \d+", "METRICS_API = 4", tm.read_text(),
                         count=1, flags=re.M))
    (path / "metrics" / "trio_loop.py").write_text("# old core\n")
    git(path, "commit", "-q", "-am", "old metrics")
    return path


def _refresh(world: World, *argv: str) -> tuple[int, str, str]:
    t = world.trioctl
    args = t.parser().parse_args(["omnigent", "metrics", "refresh", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def _same_as_release(repo: Path) -> bool:
    return all(
        (repo / "metrics" / name).read_bytes() == (REPO_ROOT / "metrics" / name).read_bytes()
        for name in METRICS_FILES
    )


def test_refresh_copies_the_set_and_shows_a_diff_summary_without_committing(world, tmp_path):
    repo = _old_repo(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD")
    code, out, err = _refresh(world, "--repo", str(repo))
    assert code == 0, err
    assert _same_as_release(repo)
    assert "metrics/trio_loop.py: updated (+" in out
    assert "metrics/trio-metrics.py: updated (+1 -1)" in out
    assert "metrics/trio-check.py: unchanged" in out
    assert "not committed (pass --commit" in out
    assert git(repo, "rev-parse", "HEAD") == head
    assert sorted(git(repo, "diff", "--name-only").splitlines()) == sorted([
        "metrics/trio-metrics.py", "metrics/trio_loop.py",
    ])
    assert git(repo, "status", "--porcelain", "--untracked-files=all").count("??") == 0


def test_refresh_commit_commits_only_the_metrics_files_on_the_current_branch(world, tmp_path):
    repo = _old_repo(tmp_path / "repo", branch="feat/x")
    (repo / "README.md").write_text("staged user edit\n")
    git(repo, "add", "README.md")
    code, out, err = _refresh(world, "--repo", str(repo), "--commit")
    assert code == 0, err
    subject = git(repo, "log", "-1", "--format=%s")
    assert subject.startswith("chore: vendor trio loop core (") and "METRICS_API 6)" in subject
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/x"
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()) == sorted([
        "metrics/trio-metrics.py", "metrics/trio_loop.py",
    ])
    assert git(repo, "diff", "--cached", "--name-only") == "README.md"  # user's staging kept
    assert "committed " in out and "on feat/x" in out
    # A second run changes nothing and commits nothing.
    head = git(repo, "rev-parse", "HEAD")
    code, out, _err = _refresh(world, "--repo", str(repo), "--commit")
    assert code == 0 and "already current; nothing to commit" in out
    assert git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("dirt", ["modified", "untracked"])
def test_refresh_refuses_a_dirty_metrics_dir_and_changes_nothing(world, tmp_path, dirt):
    repo = _old_repo(tmp_path / "repo")
    if dirt == "modified":
        (repo / "metrics" / "trio-shadow.py").write_text("# local hack\n")
    else:
        (repo / "metrics" / "notes.txt").write_text("mine\n")
    before = {p.name: p.read_bytes() for p in (repo / "metrics").iterdir()}
    code, _out, err = _refresh(world, "--repo", str(repo), "--commit")
    assert code == 2
    assert "metrics has uncommitted changes" in err and "nothing was changed" in err
    assert {p.name: p.read_bytes() for p in (repo / "metrics").iterdir()} == before


def test_refresh_commit_refuses_a_detached_head(world, tmp_path):
    repo = _old_repo(tmp_path / "repo")
    git(repo, "checkout", "-q", "--detach")
    code, _out, err = _refresh(world, "--repo", str(repo), "--commit")
    assert code == 2 and "detached HEAD" in err
    assert not _same_as_release(repo)


def test_refresh_mailbox_refreshes_home_only_never_declared_clones(world, tmp_path):
    """eval-r16rc N3: declared product clones are never written or checked."""
    home = _old_repo(tmp_path / "home")
    (home / ".gitignore").write_text("loop/x/app/\n")
    git(home, "add", ".gitignore")
    git(home, "commit", "-q", "-m", "ignore clone")
    app = home / "loop" / "x" / "app"
    init_repo(app, "dev", {"app/core.py": "x = 1\n"}, metrics=False)
    svc = tmp_path / "elsewhere" / "svc"
    init_repo(svc, "feat/s", {"svc/main.py": "y = 2\n"}, metrics=False)
    box = write_root_mailbox(
        home, "loop/x",
        [{"id": "a-one", "repo": "app", "write": "app/a.py"},
         {"id": "s-one", "repo": "svc", "write": "svc/a.py"}],
        repos_block=(f"  - name: app\n    path: loop/x/app\n    base: dev\n"
                     f"  - name: svc\n    path: {svc}\n    base: feat/s\n"),
    )
    # A dirty metrics/ in a clone does not block the home refresh.
    (svc / "metrics").mkdir()
    (svc / "metrics" / "mine.txt").write_text("x\n")
    heads = {r: git(r, "rev-parse", "HEAD") for r in (app, svc)}
    code, out, err = _refresh(world, "--mailbox", str(box), "--commit")
    assert code == 0, err
    assert _same_as_release(home)
    assert git(home, "log", "-1", "--format=%s").startswith("chore: vendor trio loop core (")
    assert f"{home.resolve()}:" in out
    for repo in (app, svc):
        assert git(repo, "rev-parse", "HEAD") == heads[repo]
        assert f"{repo.resolve()}:" not in out
        assert not (repo / "metrics" / "trio_loop.py").exists()
    # A clone is refreshed only when named explicitly.
    (svc / "metrics" / "mine.txt").unlink()
    code, out, err = _refresh(world, "--repo", str(svc), "--commit")
    assert code == 0, err
    assert _same_as_release(svc)


def test_root_free_old_core_refusal_names_the_refresh_command(world, tmp_path, capsys):
    home = _old_repo(tmp_path / "home")
    spec = world.add_loop(home, "loop/old", [{"id": "old-one", "write": "src/old.py"}])
    assert world.run_loop(spec) == 3
    err = capsys.readouterr().err
    assert "METRICS_API 4" in err
    assert "trioctl omnigent metrics refresh --commit" in err
    # ... and after it, the loop runs root-free and lands.
    code, _out, refresh_err = _refresh(world, "--repo", str(home), "--commit")
    assert code == 0, refresh_err
    assert world.run_loop(spec) == 0


def test_refresh_from_a_bundle_without_pin_uses_the_sha256_pin(world, tmp_path, monkeypatch):
    """The installed adapter's release dir (`git archive`, no PIN, no .git)."""
    import shutil

    bundle = tmp_path / "releases" / "abc"
    (bundle / "omnigent").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "metrics", bundle / "metrics",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "PIN"))
    monkeypatch.setattr(world.trioctl, "__file__", str(bundle / "omnigent" / "trioctl"))
    directory, pin = world.trioctl._release_metrics_source()
    assert directory == bundle / "metrics"
    assert re.fullmatch(r"sha256:[0-9a-f]{12}, METRICS_API 6", pin), pin
    repo = _old_repo(tmp_path / "repo")
    code, out, err = _refresh(world, "--repo", str(repo), "--commit")
    assert code == 0, err
    assert "no PIN file" in out
    assert git(repo, "log", "-1", "--format=%s") == f"chore: vendor trio loop core ({pin})"
    (bundle / "metrics" / "PIN").write_text("0123456789ab\n")
    assert world.trioctl._release_metrics_source()[1] == "0123456789ab, METRICS_API 6"

"""slice(scratch-out): the driver's own scratch (and the probe files agents
leave under ``.trio-opencode/``) must never count as product tree changes at
SHIP retirement, while a genuinely untracked product file still must.

Fakes only (``scenarios/ol_scratch.py``); no live model call.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trio_opencode import driver, olprompts, prompts, steplib

from test_openloop_e2e import (  # noqa: E402 - sibling test module's helpers
    install_fake_for_process, make_cfg, make_key_file, ol_repo,  # noqa: F401 - fixture
    ol_repo_multirepo,  # noqa: F401 - fixture
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


def _exclude_lines(repo: Path) -> list[str]:
    path = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path",
                     "info/exclude").strip())
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_agent_scratch_under_dot_trio_opencode_does_not_block_ship(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    cfg = make_cfg(make_key_file(tmp_path))
    install_fake_for_process(tmp_path, monkeypatch, "ol_scratch.py")

    result = driver.run(ol_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["code"] == 0, result


def test_genuine_untracked_product_file_still_blocks_ship(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
        capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_cfg(make_key_file(tmp_path))
    install_fake_for_process(tmp_path, monkeypatch, "ol_scratch.py")
    monkeypatch.setenv("FAKE_SCRATCH_PRODUCT", "1")

    result = driver.run(ol_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] != "shipped", result
    cap = capsys.readouterr()
    blob = repr(result) + cap.out + cap.err
    assert "untracked product paths" in blob and "src/.worker-state.json" in blob, blob
    # and only the product file is named, never the driver's scratch
    assert ".trio-opencode" not in blob.split("untracked product paths", 1)[1], blob


def test_ensure_exclude_covers_whole_driver_dir_once(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "keep.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")

    steplib.ensure_exclude(repo)
    steplib.ensure_exclude(repo)   # idempotent

    assert _exclude_lines(repo).count("/.trio-opencode/") == 1
    assert ".trio-opencode/" not in _exclude_lines(repo)   # never the unanchored form
    for rel in (".trio-opencode/eval-probe.py", ".trio-opencode/run-x/tmp/opencode/a.json",
                ".trio-opencode/worktrees/w/f"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("x\n", encoding="utf-8")
    assert _git(repo, "ls-files", "-o", "--exclude-standard").split() == ["src/real.py"]
    # a blanket `git add -A` never sweeps the scratch into a commit
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["src/real.py"]


def _commit_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "keep.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i")


def test_exclude_is_root_anchored_nested_product_dir_stays_visible(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _commit_repo(repo)
    steplib.ensure_exclude(repo)
    for rel in ("sub/.trio-opencode/new_product.py", ".trio-opencode/scratch.py"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    assert _git(repo, "ls-files", "-o", "--exclude-standard").split() == [
        "sub/.trio-opencode/new_product.py"]
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", "--name-only").split() == [
        "sub/.trio-opencode/new_product.py"]


def test_root_scratch_ignored_in_nested_builder_and_external_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _commit_repo(repo)
    steplib.ensure_exclude(repo)
    nested = repo / ".trio-opencode" / "worktrees" / "w1"
    external = tmp_path / "ext"
    _git(repo, "worktree", "add", "-q", "--detach", str(nested), "HEAD")
    _git(repo, "worktree", "add", "-q", "--detach", str(external), "HEAD")
    for wt in (nested, external):
        for rel in (".trio-opencode/probe.py", "sub/.trio-opencode/product.py", "slice.py"):
            p = wt / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n", encoding="utf-8")
        assert sorted(_git(wt, "ls-files", "-o", "--exclude-standard").split()) == [
            "slice.py", "sub/.trio-opencode/product.py"], wt
    # and the main checkout never sees the nested worktree dir
    assert _git(repo, "ls-files", "-o", "--exclude-standard").split() == []


def test_legacy_worktrees_line_resume_gets_anchored_line_once(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _commit_repo(repo)
    exclude = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path",
                        "info/exclude").strip())
    exclude.write_text("# old\n.trio-opencode/worktrees/\n", encoding="utf-8")
    steplib.ensure_exclude(repo)
    steplib.ensure_exclude(repo)
    lines = _exclude_lines(repo)
    assert lines.count("/.trio-opencode/") == 1 and ".trio-opencode/worktrees/" in lines
    (repo / ".trio-opencode").mkdir()
    (repo / ".trio-opencode" / "p.py").write_text("x\n", encoding="utf-8")
    assert _git(repo, "ls-files", "-o", "--exclude-standard").split() == []


def test_nested_dir_named_like_scratch_blocks_ship(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
        capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_cfg(make_key_file(tmp_path))
    install_fake_for_process(tmp_path, monkeypatch, "ol_scratch.py")
    monkeypatch.setenv("FAKE_SCRATCH_NESTED", "1")

    result = driver.run(ol_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] != "shipped", result
    cap = capsys.readouterr()
    assert "sub/.trio-opencode/real.py" in repr(result) + cap.out + cap.err


@pytest.mark.parametrize("where", ["home", "be", "both"])
def test_multi_repo_scratch_in_every_declared_repo_does_not_block_ship(
        ol_repo_multirepo: tuple[Path, Path], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    cfg = make_cfg(make_key_file(tmp_path), isolate_workers=True)
    install_fake_for_process(tmp_path, monkeypatch, "ol_scratch.py")
    monkeypatch.setenv("FAKE_SCRATCH_MULTI", where)
    home_repo, be_repo = ol_repo_multirepo

    result = driver.run(home_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["code"] == 0, result
    for repo in (home_repo, be_repo):
        assert "/.trio-opencode/" in _exclude_lines(repo), repo


def test_multi_repo_genuine_untracked_file_in_declared_repo_still_blocks(
        ol_repo_multirepo: tuple[Path, Path], tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_cfg(make_key_file(tmp_path), isolate_workers=True)
    install_fake_for_process(tmp_path, monkeypatch, "ol_scratch.py")
    monkeypatch.setenv("FAKE_SCRATCH_MULTI", "be")
    monkeypatch.setenv("FAKE_SCRATCH_PRODUCT", "1")
    home_repo, _be = ol_repo_multirepo

    result = driver.run(home_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] != "shipped", result
    cap = capsys.readouterr()
    blob = repr(result) + cap.out + cap.err
    assert "src/.worker-state.json" in blob, blob
    assert ".trio-opencode" not in blob.split("untracked product paths", 1)[1], blob


def test_repo_joining_on_resume_gets_the_exclude(tmp_path: Path) -> None:
    """``_exclude_driver_scratch`` (called at every start AND resume, and on
    every worktree the open-loop runner creates) covers a repo the first run
    never saw."""
    first, joined = tmp_path / "first", tmp_path / "joined"
    _commit_repo(first)
    _commit_repo(joined)
    driver._exclude_driver_scratch(first)
    assert "/.trio-opencode/" not in _exclude_lines(joined)
    driver._exclude_driver_scratch(first, joined, None, tmp_path / "not-a-repo")
    assert _exclude_lines(joined).count("/.trio-opencode/") == 1


@pytest.mark.parametrize("tmpdir", ["/repo/.git/trio-opencode/run-ab/tmp"])
def test_prompts_tell_agents_to_keep_scratch_out_of_the_repo(tmpdir: str) -> None:
    note = " ".join(prompts.tmp_note(tmpdir))
    assert tmpdir in note
    assert "never" in note.lower() and ".trio-opencode" in note   # no probes in the repo / driver dir
    ol_ctx = {"mailbox": "/mb", "iteration": 1, "repo": "/repo", "driver": "opencode",
              "output": "structured", "notes": [], "human_answer": None, "tmpdir": tmpdir}
    text = " ".join(olprompts._role_intro("lead", ol_ctx))
    assert tmpdir in text and "scratch" in text.lower()
    ol_ctx["tmpdir"] = None
    assert tmpdir not in " ".join(olprompts._role_intro("lead", ol_ctx))

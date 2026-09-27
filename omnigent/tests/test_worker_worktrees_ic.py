"""Retirement of accepted/finished task worktrees holding ignored content.

Offline, real scratch git repositories. Rule under test
(``worker_worktrees._blocking_state``): for an owner-verified worktree whose
record is accepted (builder merged + ``accepted_by``) or finished (evaluator),
ignored entries that are empty directories or rebuildable artifacts
(``REBUILDABLE_IGNORED_DIRS``, repo-``.gitignore``-listed ``dist``/``build``,
``*.pyc``/``*.pyo``/``*.tsbuildinfo``) do not block removal; anything else
retains the worktree as ``ignored_content`` naming the blocking paths.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "worker_worktrees.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_ic", MODULE)


GITIGNORE = ".eval-scratch/\n.venv/\n__pycache__/\napi/node_modules/\n.env\n"


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(GITIGNORE)
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def root(tmp_path):
    return tmp_path / "worktrees"


def put(path: Path, rel: str, text: str | None = "x\n") -> None:
    target = path / rel
    if text is None:
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def finished_eval(wt, repo, root, name, entries):
    sha = git(repo, "rev-parse", "HEAD")
    rec = wt.create(repo, slice_id=name, mailbox=repo / "loop", root=root,
                    role="evaluator", detach_at=sha)
    for rel, text in entries:
        put(Path(rec["path"]), rel, text)
    return wt.mark_finished(repo, rec["id"])


def accepted_builder_cleanup(wt, repo, root, entries, *, accept=True):
    rec = wt.create(repo, slice_id="B", mailbox=repo / "loop", root=root)
    path = Path(rec["path"])
    put(path, "b.txt", "B\n")
    wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
    wt.integrate(repo, rec["id"])
    for rel, text in entries:
        put(path, rel, text)
    head = git(repo, "rev-parse", "HEAD")
    acceptance = (lambda box: {"evaluated": head}) if accept else (lambda box: None)
    (result,) = wt.cleanup(repo, acceptance_for=acceptance)
    return rec, result


def cleanup_by_slice(wt, repo):
    return {r["slice"]: r for r in wt.cleanup(repo)}


# ------------------------------------------------------------ removed


def test_accepted_builder_with_node_modules_is_removed(wt, repo, root):
    rec, result = accepted_builder_cleanup(
        wt, repo, root, [("api/node_modules/x.js", "module.exports=1\n"),
                         ("api/node_modules/.bin/tool", "#!/bin/sh\n")])
    assert result["state"] == "removed", result
    assert not Path(rec["path"]).exists()
    assert wt.rev(repo, f"refs/heads/{rec['branch']}") is None


@pytest.mark.parametrize("name, entries", [
    ("venv", [(".venv/lib/x", "x\n")]),
    ("empty-scratch", [(".eval-scratch", None)]),
    ("nested-empty", [(".eval-scratch/a/b", None)]),
    ("node-modules", [("api/node_modules/x.js", "x\n")]),
])
def test_finished_eval_with_rebuildable_or_empty_ignored_is_removed(wt, repo, root, name, entries):
    rec = finished_eval(wt, repo, root, name, entries)
    ignored = wt.ignored_entries(Path(rec["path"]))
    assert ignored, "fixture must actually produce an ignored entry"
    result = cleanup_by_slice(wt, repo)[name]
    assert result["state"] == "removed", result
    assert not Path(rec["path"]).exists()


def test_dist_listed_by_repo_gitignore_is_removed(wt, repo, root):
    (repo / ".gitignore").write_text(GITIGNORE + "dist/\nweb/build\n")
    git(repo, "commit", "-qam", "ignore dist")
    rec = finished_eval(wt, repo, root, "dist", [("dist/app.js", "x\n"),
                                                  ("web/build/b.js", "x\n")])
    assert sorted(wt.ignored_entries(Path(rec["path"]))) == ["dist/", "web/build/"]
    assert cleanup_by_slice(wt, repo)["dist"]["state"] == "removed"


# ------------------------------------------------------------ retained


def test_non_empty_eval_scratch_is_retained_and_named(wt, repo, root):
    rec = finished_eval(wt, repo, root, "notes", [(".eval-scratch/notes.md", "evidence\n")])
    result = cleanup_by_slice(wt, repo)["notes"]
    assert result["state"] == "retained"
    assert result["retained_reason"] == "ignored_content"
    assert ".eval-scratch/" in result["retained_detail"]
    assert (Path(rec["path"]) / ".eval-scratch" / "notes.md").is_file()


def test_env_file_is_retained(wt, repo, root):
    rec = finished_eval(wt, repo, root, "env", [(".env", "TOKEN=secret\n")])
    result = cleanup_by_slice(wt, repo)["env"]
    assert (result["state"], result["retained_reason"]) == ("retained", "ignored_content")
    assert result["retained_detail"] == ".env"
    assert (Path(rec["path"]) / ".env").is_file()


def test_dist_ignored_only_globally_is_retained(wt, repo, root, home):
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("dist/\n")
    rec = finished_eval(wt, repo, root, "gdist", [("dist/app.js", "x\n")])
    assert wt.ignored_entries(Path(rec["path"])) == ["dist/"]
    result = cleanup_by_slice(wt, repo)["gdist"]
    assert (result["retained_reason"], result["retained_detail"]) == ("ignored_content", "dist/")


def test_dist_ignored_by_info_exclude_or_wildcard_is_retained(wt, repo, root):
    (repo / ".gitignore").write_text(GITIGNORE + "dis*/\n")
    git(repo, "commit", "-qam", "wildcard")
    common = Path(git(repo, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else repo / common
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "exclude").write_text("build/\n")
    rec = finished_eval(wt, repo, root, "wild", [("dist/a.js", "x\n"), ("build/b.js", "x\n")])
    result = cleanup_by_slice(wt, repo)["wild"]
    assert result["retained_reason"] == "ignored_content"
    assert result["retained_detail"] == "build/; dist/"
    assert Path(rec["path"]).is_dir()


def test_mixed_names_only_the_blocking_path(wt, repo, root):
    rec = finished_eval(wt, repo, root, "mixed", [
        ("api/node_modules/x.js", "x\n"), (".env", "TOKEN=secret\n"),
        (".venv/lib/y", "y\n"), (".eval-scratch", None)])
    result = cleanup_by_slice(wt, repo)["mixed"]
    assert (result["retained_reason"], result["retained_detail"]) == ("ignored_content", ".env")
    assert (Path(rec["path"]) / "api" / "node_modules" / "x.js").is_file()


def test_detail_lists_first_five_blocking_paths(wt, repo, root):
    (repo / ".gitignore").write_text(GITIGNORE + "*.log\n")
    git(repo, "commit", "-qam", "logs")
    entries = [(f"l{i}.log", "x\n") for i in range(7)] + [("api/node_modules/x.js", "x\n")]
    finished_eval(wt, repo, root, "many", entries)
    detail = cleanup_by_slice(wt, repo)["many"]["retained_detail"].split("; ")
    assert detail == [f"l{i}.log" for i in range(5)]


def test_not_accepted_builder_with_node_modules_is_kept_as_today(wt, repo, root):
    rec, result = accepted_builder_cleanup(
        wt, repo, root, [("api/node_modules/x.js", "x\n")], accept=False)
    assert result["state"] != "removed" and not result.get("accepted_by")
    assert (Path(rec["path"]) / "api" / "node_modules" / "x.js").is_file()
    # The relaxed rule itself does not apply without acceptance.
    record = wt.load_record(repo, rec["id"])
    assert wt._blocking_state(repo, record, owner_verified=True) == (
        "ignored_content", "api/node_modules/")


def test_unfinished_eval_is_not_relaxed(wt, repo, root):
    sha = git(repo, "rev-parse", "HEAD")
    rec = wt.create(repo, slice_id="open", mailbox=repo / "loop", root=root,
                    role="evaluator", detach_at=sha)
    put(Path(rec["path"]), "api/node_modules/x.js")
    record = wt.load_record(repo, rec["id"])
    assert not record.get("finished")
    assert wt._blocking_state(repo, record, owner_verified=True)[0] == "ignored_content"


def test_unverified_owner_is_retained_as_today(wt, repo, root):
    rec = finished_eval(wt, repo, root, "own", [("api/node_modules/x.js", "x\n")])
    path = Path(rec["path"])
    admin = Path(git(path, "rev-parse", "--absolute-git-dir"))
    (admin / wt.OWNER_MARKER).write_text("someone-else\n")
    result = cleanup_by_slice(wt, repo)["own"]
    assert (result["state"], result["retained_reason"]) == ("retained", "uncertain_ownership")
    assert (path / "api" / "node_modules" / "x.js").is_file()
    # Without the caller's ownership verification the rule is today's.
    record = wt.load_record(repo, rec["id"])
    assert wt._blocking_state(repo, record)[0] == "ignored_content"


def test_symlinked_node_modules_is_not_treated_as_rebuildable(wt, repo, root, tmp_path):
    (repo / ".gitignore").write_text(GITIGNORE + "web/node_modules\n")
    git(repo, "commit", "-qam", "symlink-able pattern")
    outside = tmp_path / "shared-node-modules"
    outside.mkdir()
    (outside / "keep.js").write_text("keep\n")
    rec = finished_eval(wt, repo, root, "link", [])
    (Path(rec["path"]) / "web").mkdir()
    (Path(rec["path"]) / "web" / "node_modules").symlink_to(outside)
    assert wt.ignored_entries(Path(rec["path"])) == ["web/node_modules"]
    result = cleanup_by_slice(wt, repo)["link"]
    assert (result["retained_reason"], result["retained_detail"]) == (
        "ignored_content", "web/node_modules")
    assert (outside / "keep.js").is_file()


# ------------------------------------------- live replay: openrouter/D

# Captured read-only (GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1
# --ignored=matching --untracked-files=all) from the 8 worktrees the D run
# retained as ignored_content; "(empty)" marks a directory with no files.
LIVE_D_IGNORED = {
    "eval-or-api-bcb033a4": ["api/node_modules/", "metrics/__pycache__/"],
    "eval-or-api-d870dfb0": [".eval-scratch/ (empty)", "api/node_modules/"],
    "eval-or-map-0cfc2d25": ["api/node_modules/", "metrics/__pycache__/"],
    "eval-or-view-d23cc056": ["api/node_modules/"],
    "eval-or-view-f7d2e156": ["api/node_modules/"],
    "or-api-a3562ac4": ["api/node_modules/"],
    "or-map-417d6d21": ["api/node_modules/"],
    "or-view-ec9b0665": ["api/node_modules/"],
}
LIVE_D_GITIGNORE = (
    ".context/\n.eval-scratch/\n.venv/\n__pycache__/\n*.pyc\napi/node_modules/\n"
    "api/dist/\n.runtime/\npi/.env\nhomepage/logs/\n.DS_Store\n"
)


@pytest.mark.parametrize("name", sorted(LIVE_D_IGNORED))
def test_live_d_retained_worktrees_would_now_be_removed(wt, tmp_path, root, name):
    repo = tmp_path / "d-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(LIVE_D_GITIGNORE)
    (repo / "api").mkdir()
    (repo / "api" / "package.json").write_text("{}\n")
    (repo / "metrics").mkdir()
    (repo / "metrics" / "m.py").write_text("x = 1\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    captured = LIVE_D_IGNORED[name]
    entries = []
    for item in captured:
        rel = item.split(" ")[0]
        if item.endswith("(empty)"):
            entries.append((rel.rstrip("/"), None))
        elif "__pycache__" in rel:
            entries.append((rel + "m.cpython-313.pyc", "\0"))
        else:
            entries.append((rel + "express/index.js", "x\n"))
    if name.startswith("eval-"):
        rec = finished_eval(wt, repo, root, name, entries)
        path = Path(rec["path"])
        record = wt.load_record(repo, rec["id"])
    else:
        rec = wt.create(repo, slice_id=name, mailbox=repo / "loop", root=root)
        path = Path(rec["path"])
        put(path, "api/app.js", "app\n")
        wt.mark_exited(repo, wt.load_record(repo, rec["id"]), 0)
        wt.integrate(repo, rec["id"])
        for rel, text in entries:
            put(path, rel, text)
        record = wt.load_record(repo, rec["id"])
        record["accepted_by"] = {"evaluated": git(repo, "rev-parse", "HEAD")}
        wt.save_record(repo, record)
    # The replay reproduces the live status exactly...
    assert sorted(wt.ignored_entries(path)) == sorted(i.split(" ")[0] for i in captured)
    # ...which today's (unverified/unaccepted) rule blocks on node_modules/.eval-scratch,
    assert wt._blocking_state(repo, dict(record))[0] == "ignored_content"
    # ...and the new rule does not block at all.
    assert wt._owned_worktree(repo, record) is None
    assert wt._blocking_state(repo, dict(record), owner_verified=True) is None
    head = git(repo, "rev-parse", "HEAD")
    (result,) = wt.cleanup(repo, acceptance_for=lambda box: {"evaluated": head})
    assert result["state"] == "removed", result
    assert not path.exists()

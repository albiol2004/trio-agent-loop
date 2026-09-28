"""r15: the dashboard and trio-metrics show the repo per slice and never
crash on `repo:` keys (PLAN slices, QUEUE.md retired entries)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x.invalid",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x.invalid"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


serve = _load("trio_dashboard_serve_r15", REPO_ROOT / "dashboard" / "serve.py")
TM = _load("trio_metrics_r15dash", REPO_ROOT / "metrics" / "trio-metrics.py")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=dict(os.environ, **GIT_ENV)).stdout.strip()


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    box = home / "loop"
    box.mkdir(parents=True)
    git(home, "init", "-q", "-b", "main")
    (home / ".gitignore").write_text("loop/svc/\n")
    (home / "a").write_text("a")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "slice(h1): home work")
    svc = box / "svc"
    svc.mkdir()
    git(svc, "init", "-q", "-b", "dev")
    (svc / "s").write_text("s")
    git(svc, "add", "-A")
    git(svc, "commit", "-qm", "slice(s1): svc work")
    sha = git(svc, "rev-parse", "HEAD")
    (box / "PLAN.md").write_text(
        "```yaml\nrepos:\n  - name: svc\n    path: loop/svc\n```\n\n"
        "```yaml\nslices:\n  - id: s1\n    repo: svc\n    writes: [s]\n    status: complete\n"
        "  - id: h1\n    writes: [a]\n    status: in_progress\n```\n")
    (box / "QUEUE.md").write_text(
        f"```yaml\nretired:\n  - slice: s1\n    repo: svc\n    sha: {sha}\n"
        "    at: 2026-09-28T00:00:00Z\n```\n\n```yaml\nfaults:\n```\n")
    (box / "STATE.md").write_text("schema: 1\niteration: 1\nstatus: running\n")
    (box / "LOG.md").write_text("# Trio loop log\n- iter 1 | lead | x; gate: PASS @svc:" + sha + "\n")
    (box / "VERDICT.md").write_text(f"## slice s1 @{sha} — SHIP\n")
    return home, box


def test_slice_commits_come_from_declared_repos(tmp_path):
    home, box = _layout(tmp_path)
    commits = serve._loop_commits(box, home)
    by_slice = {c["slice"]: c for c in commits}
    assert by_slice["s1"]["repo"] == "svc" and "repo" not in by_slice["h1"]


def test_derived_slices_keep_the_repo_and_lifecycle(tmp_path):
    home, box = _layout(tmp_path)
    slices = serve._loop_slices_derived(box, "open-loop", serve._loop_commits(box, home))
    by_id = {s["id"]: s for s in slices}
    assert by_id["s1"]["repo"] == "svc" and by_id["s1"]["lifecycle"] == "shipped"
    assert by_id["h1"]["repo"] == "." and by_id["h1"]["lifecycle"] == "building"
    json.dumps(slices)


def test_single_repo_commits_carry_no_repo_key(tmp_path):
    home, box = _layout(tmp_path)
    (box / "PLAN.md").write_text("```yaml\nslices:\n  - id: h1\n    writes: [a]\n```\n")
    assert all("repo" not in c for c in serve._loop_commits(box, home))


def test_trio_metrics_lists_repos_per_slice(tmp_path):
    _home, box = _layout(tmp_path)
    loop = TM.analyze_loop(box)
    assert loop["repos"] == {"home": ["h1"], "svc": ["s1"]}
    assert "  repos: home (h1); svc (s1)" in TM.render([loop], TM.aggregate([loop]))
    (box / "PLAN.md").write_text("```yaml\nslices:\n  - id: h1\n```\n")
    assert "repos" not in TM.analyze_loop(box)


def test_app_js_renders_the_repo_chip():
    text = (REPO_ROOT / "dashboard" / "app.js").read_text()
    assert 'span("meta-chip slice-repo mono", repo)' in text

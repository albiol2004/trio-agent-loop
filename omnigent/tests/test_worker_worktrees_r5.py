"""Regressions for the independent ITERATE on d39bfd9 (B1, B2, N1-N6, N2).

r16b: B1, B2, B3, N1, N3, N4, N5 and N6 all exercised the root ``.cursor``
config baseline/restore machinery for NON-isolated runs
(``snapshot_root_cursor``, ``restore_root_cursor``, ``record_root_session``,
``root_owned_sessions``, ``_root_identity``, ``_renameat2``,
``_prepare_root_config``/``_restore_root_config``, ``OmnigentRunner._root_owned_ids``/
``._root_finished``) -- multi-root secret isolation, held-session exclusion,
atomic-swap crash/race safety, legacy index-mode/symlink handling, stale
baseline naming, and pre-dispatch session recording. Every one of those
functions is deleted (r16b: every git-checkout loop runs root-free in its
own Lead worktree; there is no root config left to snapshot, restore or
record a session against), and none of it has a like-for-like replacement
at this layer (the residue-is-not-product behaviour that survives is
covered at the loop-core level by ``test_r11_cursor_residue.py`` and
``test_worker_worktrees_r4.py``). All of B1/B2/B3/N1/N3/N4/N5/N6 are
deleted below; only N2 (vendored loop-core/metrics compatibility, unrelated
to root config) remains.

Offline, real git.
"""
from __future__ import annotations

import re

import pytest

from test_worker_worktrees_r4 import (  # noqa: E402  (sibling helpers)
    REPO_ROOT,
    _load,
    _vendor,
    git,
)

from test_worker_worktrees_r4 import GIT_ENV, MODULE, SCRIPT  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return h


@pytest.fixture()
def wt(home):
    return _load("worker_worktrees_r5", MODULE)


@pytest.fixture()
def trioctl(wt, monkeypatch):
    module = _load("trioctl_r5", SCRIPT)
    monkeypatch.setattr(module, "worker_worktrees", wt)
    return module


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "shared.txt").write_text("one\n")
    (repo / "loop").mkdir()
    (repo / "loop" / "PLAN.md").write_text("plan\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ------------------------------------------------------------------ N2


def test_n2_mixed_metrics_without_marker_is_refused(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    metrics = repo / "metrics" / "trio-metrics.py"
    metrics.write_text(re.sub(r"^METRICS_API = \d+\n", "", metrics.read_text(), flags=re.M))
    with pytest.raises(trioctl.TrioctlError, match="METRICS_API 1.*mixed metrics"):
        trioctl._load_trio_loop(repo)
    assert "mixed metrics" in trioctl._ship_acceptance(repo / "loop", repo)["pending"]


def test_n2_marked_but_broken_metrics_is_a_clear_refusal(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    metrics = repo / "metrics" / "trio-metrics.py"
    metrics.write_text(metrics.read_text().replace("def parse_verdict_scope", "def _gone"))
    with pytest.raises(trioctl.TrioctlError, match="loading it failed \\(AttributeError"):
        trioctl._load_trio_loop(repo)
    assert trioctl._ship_acceptance(repo / "loop", repo)["pending"].startswith(
        "loop core not usable: incompatible loop core")


def test_n2_rebound_marker_is_ambiguous_and_refused(trioctl, repo):
    core = _vendor(repo, REPO_ROOT / "metrics")
    core.write_text(core.read_text().replace(
        "LOOP_CORE_API = 2\n", "LOOP_CORE_API = 2\nLOOP_CORE_API = 3\n"))
    with pytest.raises(trioctl.TrioctlError, match="LOOP_CORE_API 0"):
        trioctl._load_trio_loop(repo)


def test_n2_current_set_loads(trioctl, repo):
    _vendor(repo, REPO_ROOT / "metrics")
    module = trioctl._load_trio_loop(repo)
    assert module.LOOP_CORE_API == 2 and module._METRICS.METRICS_API == 6

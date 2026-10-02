"""End-to-end tests for the trio-opencode open-loop runner: the whole
``trio_opencode.driver``/CLI stack, through ``steplib.TL.run_open_loop``,
against ``tests/fake_opencode.py`` (never the real ``opencode`` binary --
see ``tests/scenarios/ol_happy.py`` for the fake's per-turn behaviour and
``tests/scenarios/common.py`` for shared helpers).
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode import driver, olqueue, rootfree, steplib

from fakeoc import install_fake

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DRIVER_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = DRIVER_ROOT / "trio_opencode" / "cli.py"

PLAN_3_SLICES = """\
# Plan

```yaml
slices:
  - id: a
    writes: [a.py]
    reads: []
    accepts: ["a.py exists and prints 'a' | oracle: value"]
  - id: b
    writes: [b.py]
    reads: []
    accepts: ["b.py exists and prints 'b' | oracle: value"]
  - id: c
    writes: [c.py]
    reads: []
    accepts: ["c.py exists and prints 'c' | oracle: value"]
```

## Verification standard
implement-then-smoke

full_check: true
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def ol_repo(tmp_path: Path) -> Path:
    """A git repo with a QUEUE.md mailbox (D1 open-loop detection) whose
    PLAN.md already declares 3 disjoint slices with accepts and a
    ``## Verification standard`` (``full_check:``)."""
    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip a.py, b.py and c.py.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(PLAN_3_SLICES, encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")
    return root


PLAN_1_SLICE = """\
# Plan

```yaml
slices:
  - id: a
    writes: [a.py]
    reads: []
    accepts: ["a.py exists and prints 'a' | oracle: value"]
```

## Verification standard
implement-then-smoke

full_check: true
"""


@pytest.fixture
def ol_repo_one_slice(tmp_path: Path) -> Path:
    """Like ``ol_repo``, but PLAN.md declares only ONE slice, `a` -- for
    scenarios (fault repair, crash+resume) whose Lead only ever returns
    that one id: with ``ol_repo``'s 3-slice PLAN.md, `_slices_fully_retired`
    (``metrics/trio_loop.py``) would never consider the run done (b/c would
    never gain a retired entry), so the integration-eval would never
    dispatch."""
    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip a.py.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(PLAN_1_SLICE, encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")
    return root


FAKE_KEY_VALUE = "sk-fake-ol-e2e-key-do-not-leak"


def make_key_file(tmp_path: Path, value: str = FAKE_KEY_VALUE) -> Path:
    path = tmp_path / "fake-key.txt"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def make_cfg(key_file: Path, **overrides) -> config_mod.Config:
    d = config_mod._default_dict()
    return config_mod.Config(
        opencode_bin="opencode",
        models=dict(d["models"]),
        variants=dict(d["variants"]),
        provider=config_mod.ProviderConfig(id="opencode-go", key_file=str(key_file),
                                          key_env="OPENCODE_API_KEY"),
        timeouts=config_mod.TimeoutsConfig(
            turn_seconds=overrides.get("turn_seconds", 20.0),
            idle_seconds=overrides.get("idle_seconds", 15.0),
            evaluator_turn_seconds=overrides.get("evaluator_turn_seconds", 20.0)),
        retries=config_mod.RetriesConfig(max_attempts=2, backoff_seconds=(0.1, 0.1)),
        max_iterations=overrides.get("max_iterations", 4), root_free=overrides.get("root_free", True),
        isolate_workers=overrides.get("isolate_workers", True),
        slice_eval_concurrency=overrides.get("slice_eval_concurrency", 4),
        kill_check=overrides.get("kill_check", False),
    )


def write_ol_cfg_file(path: Path, key_file: Path, **overrides) -> Path:
    """Open-loop counterpart of ``test_e2e.py``'s ``write_cfg_file``: the
    same JSON config shape, plus the open-loop-only fields (``make_cfg``
    already sets them on the ``Config`` object for in-process callers)."""
    cfg = make_cfg(key_file, **overrides)
    doc = {
        "opencode_bin": cfg.opencode_bin, "models": cfg.models, "variants": cfg.variants,
        "provider": {"id": cfg.provider.id, "key_file": cfg.provider.key_file,
                    "key_env": cfg.provider.key_env},
        "timeouts": {"turn_seconds": cfg.timeouts.turn_seconds,
                    "idle_seconds": cfg.timeouts.idle_seconds,
                    "evaluator_turn_seconds": cfg.timeouts.evaluator_turn_seconds},
        "retries": {"max_attempts": cfg.retries.max_attempts,
                   "backoff_seconds": list(cfg.retries.backoff_seconds)},
        "max_iterations": cfg.max_iterations, "root_free": cfg.root_free,
        "isolate_workers": cfg.isolate_workers,
        "slice_eval_concurrency": cfg.slice_eval_concurrency, "kill_check": cfg.kill_check,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def spawn_cli(args: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(CLI_PATH), *args], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)


def run_cli(args: list[str], env: dict[str, str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI_PATH), *args], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def wait_for_file(path: Path, deadline: float = 20.0, poll: float = 0.05) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(poll)
    return path.exists()


def install_fake_for_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                             scenario: str) -> dict[str, str]:
    env = install_fake(tmp_path, str(SCENARIOS_DIR / scenario))
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_OC_STATE", env["FAKE_OC_STATE"])
    monkeypatch.setenv("FAKE_OC_SCENARIO", env["FAKE_OC_SCENARIO"])
    monkeypatch.setenv("TRIO_OPENCODE_POLL_SECONDS", "0.05")
    return env


def read_calls(env: dict[str, str]) -> list[dict]:
    calls_path = Path(env["FAKE_OC_STATE"]) / "calls.jsonl"
    if not calls_path.exists():
        return []
    return [json.loads(l) for l in calls_path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_open_loop_happy_path_ships_and_lands(ol_repo: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(make_key_file(tmp_path))
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_happy.py")

    mailbox = ol_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["harness"] == "opencode"
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]
    assert result["open_loop"] == {"isolate_workers": True, "slice_eval_concurrency": 4,
                                   "kill_check": False}

    # Three retired entries in QUEUE.md.
    from trio_opencode import olqueue
    # The live mailbox is already torn down (landed); read the RETIRED
    # entries from the now-finished root mailbox copy instead.
    retired_list = [
        line.split("slice:", 1)[1].strip()
        for line in (mailbox / "QUEUE.md").read_text(encoding="utf-8").splitlines()
        if "slice:" in line
    ]
    # Exactly one retired entry per slice: a builder commit merged and
    # retired at its merge sha is never retired again at its own sha.
    assert sorted(retired_list) == ["a", "b", "c"], retired_list

    # Two slice-evals provably overlapped (marker handshake).
    markers = Path(env["FAKE_OC_STATE"]) / "markers"
    assert (markers / "eval-saw-other-a").read_text().strip() == "1", "slice-eval a never saw b"
    assert (markers / "eval-saw-other-b").read_text().strip() == "1", "slice-eval b never saw a"

    # Builder/lead/evaluator calls used the configured per-role models.
    calls = read_calls(env)
    assert calls, "the fake opencode was never invoked"
    by_agent: dict[str, set[str]] = {}
    for c in calls:
        by_agent.setdefault(c["agent"], set()).add(c["model"])
    assert by_agent["trio-builder"] == {cfg.models["builder"]}
    assert by_agent["trio-lead"] == {cfg.models["lead"]}
    assert by_agent["trio-evaluator"] == {cfg.models["evaluator"]}
    assert len({c["pid"] for c in calls if c["agent"] == "trio-builder"}) == 3

    # No omnigent module ever loaded, in-process (hard standalone constraint).
    steplib.assert_no_omnigent_loaded(ol_repo)

    # Landed cleanly, SHIP + slice commits on the target branch.
    assert subprocess.run(["git", "-C", str(ol_repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip() == ""
    branch = subprocess.run(["git", "-C", str(ol_repo), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "main"
    log = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    for sid in ("a", "b", "c"):
        assert f"slice({sid}):" in log, log
        assert (ol_repo / f"{sid}.py").is_file()

    # Lead worktree removed, trio/<slug> branch deleted (same teardown as lockstep).
    slug = rootfree.loop_slug("loop")
    record = rootfree.load_record(ol_repo, slug)
    assert record.landed is True
    assert not Path(record.path).exists()
    branches = subprocess.run(["git", "-C", str(ol_repo), "branch", "--list", f"trio/{slug}"],
                              capture_output=True, text=True).stdout
    assert branches.strip() == ""

    # Result sidecar written; no leftover builder/eval worktrees anywhere.
    assert (mailbox / ".opencode-result.json").is_file()
    assert not (ol_repo / driver.BUILDER_WORKTREES_DIR).exists()


# ===========================================================================
# ol-harden round 2 (blocking issue #1 regression): a crash-free SECOND
# open-loop goal on a repo with prior open-loop history must never import
# that prior goal's `merge slice ...` commits into its own QUEUE.md.
# ===========================================================================


def _reset_mailbox(box: Path, plan: str = PLAN_3_SLICES) -> None:
    box.mkdir(exist_ok=True)
    (box / "GOAL.md").write_text("# Goal\nShip a.py, b.py and c.py (again).\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(plan, encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (box / "VERDICT.md").unlink(missing_ok=True)
    (box / "REPORT.md").unlink(missing_ok=True)


def test_open_loop_inplace_second_goal_same_mailbox_does_not_reconcile_prior_merges(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In-place (``root_free=False``): the same mailbox path and the SAME
    slice ids are reused for a second, crash-free goal. Before the ol-
    harden round-2 fix, ``_reconcile_merge_retirement`` imported run 1's
    `merge slice ...` commits as "crash-orphaned" and run 2 errored on the
    commit gate; it must now ship with zero reconciled lines."""
    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_happy.py")
    mailbox = ol_repo / "loop"

    result1 = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False)
    assert result1["status"] == "shipped", result1

    _reset_mailbox(mailbox)
    # `ol_happy.py` always (re)writes a.py/b.py/c.py with the SAME fixed
    # content for ids a/b/c -- clear them too, as a reset between goals
    # naturally would, so the second goal's builders have real new work to
    # commit rather than tripping over "nothing to commit" (unrelated to
    # the regression under test).
    for sid in ("a", "b", "c"):
        (ol_repo / f"{sid}.py").unlink(missing_ok=True)
    _git(ol_repo, "add", "-A")
    _git(ol_repo, "commit", "-q", "-m", "loop: new goal in loop")

    result2 = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False)
    assert result2["status"] == "shipped", result2

    log2 = (mailbox / "LOG.md").read_text(encoding="utf-8")
    # The real reconciliation question: zero IMPORTED (crash-orphaned)
    # entries from run 1 -- run 2 genuinely re-merges a/b/c itself (same
    # ids, real new commits), so 2 real merges per id is correct, not a
    # reconciliation artifact.
    assert "reconciled crash-orphaned" not in log2, log2
    subjects = _git(ol_repo, "log", "--format=%s")
    for sid in ("a", "b", "c"):
        assert subjects.count(f"merge slice {sid} (") == 2, subjects
    retired_list = [
        line.split("slice:", 1)[1].strip()
        for line in (mailbox / "QUEUE.md").read_text(encoding="utf-8").splitlines()
        if "slice:" in line
    ]
    # Exactly one retired entry per slice for THIS (second) goal: the
    # reconciled-nothing QUEUE.md was reset, so only run 2's own merges
    # appear in it, not run 1's.
    assert sorted(retired_list) == ["a", "b", "c"], retired_list


def test_open_loop_new_mailbox_after_land_does_not_reconcile_prior_merges(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root-free: run 1 lands on `main`, then a brand-new mailbox (`loop2`)
    on the SAME repo starts a second, crash-free, colliding-id goal. Before
    the fix, the new Lead worktree's first-parent history (forked from
    `main`'s post-land tip, which already carries run 1's merges) got all
    of run 1's merges imported too; it must now ship with zero reconciled
    lines and exactly one merge per slice."""
    cfg = make_cfg(make_key_file(tmp_path))
    install_fake_for_process(tmp_path, monkeypatch, "ol_happy.py")
    mailbox = ol_repo / "loop"

    result1 = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)
    assert result1["status"] == "shipped", result1
    assert result1["land"] is not None and result1["land"]["status"] == "landed", result1

    box2 = ol_repo / "loop2"
    _reset_mailbox(box2)
    # Same note as the in-place variant above: clear the files run 1 landed
    # on `main` so run 2's builders (same ids, same fixed content) have
    # real new work to commit.
    for sid in ("a", "b", "c"):
        (ol_repo / f"{sid}.py").unlink(missing_ok=True)
    _git(ol_repo, "add", "-A")
    _git(ol_repo, "commit", "-q", "-m", "loop: new goal in loop2")

    result2 = driver.run(box2, cfg, mode="start", max_iterations=4, root_free=True)
    assert result2["status"] == "shipped", result2
    assert result2["land"] is not None and result2["land"]["status"] == "landed", result2

    log2 = (box2 / "LOG.md").read_text(encoding="utf-8")
    # The real reconciliation question: zero imported (crash-orphaned)
    # entries from run 1 -- run 2 genuinely re-merges a/b/c itself (fresh
    # worktree, real new commits onto the post-land `main` tip), so 2 real
    # merges per id is correct, not a reconciliation artifact.
    assert "reconciled crash-orphaned" not in log2, log2
    subjects = _git(ol_repo, "log", "--format=%s")
    for sid in ("a", "b", "c"):
        assert subjects.count(f"merge slice {sid} (") == 2, subjects
    retired_list = [
        line.split("slice:", 1)[1].strip()
        for line in (box2 / "QUEUE.md").read_text(encoding="utf-8").splitlines()
        if "slice:" in line
    ]
    assert sorted(retired_list) == ["a", "b", "c"], retired_list


def test_open_loop_cli_subprocess_happy_path(ol_repo: Path, tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    cfg = make_cfg(make_key_file(tmp_path))
    cfg_path.write_text(json.dumps({
        "opencode_bin": cfg.opencode_bin, "models": cfg.models, "variants": cfg.variants,
        "provider": {"id": cfg.provider.id, "key_file": cfg.provider.key_file,
                    "key_env": cfg.provider.key_env},
        "timeouts": {"turn_seconds": cfg.timeouts.turn_seconds,
                    "idle_seconds": cfg.timeouts.idle_seconds,
                    "evaluator_turn_seconds": cfg.timeouts.evaluator_turn_seconds},
        "retries": {"max_attempts": cfg.retries.max_attempts,
                   "backoff_seconds": list(cfg.retries.backoff_seconds)},
        "max_iterations": cfg.max_iterations, "root_free": cfg.root_free,
        "isolate_workers": cfg.isolate_workers,
        "slice_eval_concurrency": cfg.slice_eval_concurrency, "kill_check": cfg.kill_check,
    }), encoding="utf-8")

    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_happy.py"))
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    env["TRIO_OPENCODE_POLL_SECONDS"] = "0.05"

    mailbox = ol_repo / "loop"
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "start", "--mailbox", str(mailbox)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "shipped", result
    assert result["land"]["status"] == "landed"

    log = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log


def test_open_loop_no_isolate_workers_with_concurrency_2_refused(ol_repo: Path,
                                                                 tmp_path: Path) -> None:
    """Accept: ``--no-isolate-workers --slice-eval-concurrency 2`` -> exit 2."""
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_happy.py"))
    mailbox = ol_repo / "loop"
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH), "start", "--mailbox", str(mailbox),
         "--no-isolate-workers", "--slice-eval-concurrency", "2"],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "error"
    assert "refused" in result["reason"]

    # Nothing was touched: no Lead worktree/branch, root mailbox untouched.
    slug = rootfree.loop_slug("loop")
    assert rootfree.load_record(ol_repo, slug) is None
    assert (mailbox / "STATE.md").read_text(encoding="utf-8") == (
        "iteration: 0\nstatus: ready\nphase: idle\n"
    )


# ===========================================================================
# E1: 3 disjoint slices, one wave -- slice-evals graded as they land, not
# only after the whole wave's builders report; >= 2 slice-evals overlap.
# ===========================================================================


def test_open_loop_three_slices_concurrent_slice_evals(ol_repo: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(make_key_file(tmp_path))
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_3slices.py")

    mailbox = ol_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["code"] == 0, result
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    retired_list = [
        line.split("slice:", 1)[1].strip()
        for line in (mailbox / "QUEUE.md").read_text(encoding="utf-8").splitlines()
        if "slice:" in line
    ]
    # Exactly one retired entry per slice: a builder commit merged and
    # retired at its merge sha is never retired again at its own sha.
    assert sorted(retired_list) == ["a", "b", "c"], retired_list

    markers = Path(env["FAKE_OC_STATE"]) / "markers"
    # a/b's slice-evals provably overlapped (marker handshake, like ol_happy.py).
    assert (markers / "eval-saw-other-a").read_text().strip() == "1", "slice-eval a never saw b"
    assert (markers / "eval-saw-other-b").read_text().strip() == "1", "slice-eval b never saw a"

    # Slice `c`'s builder was deliberately the last to finish (it waited for
    # slice `a`'s slice-eval to have started) -- proof that `a` was graded
    # while builder `c` (its own wave-mate) was still running, not only
    # after the whole wave reported.
    eval_start_a = float((markers / "eval-start-a").read_text().strip())
    builder_done_c = float((markers / "builder-done-c").read_text().strip())
    assert eval_start_a < builder_done_c, (
        f"slice-eval a started ({eval_start_a}) after builder c finished "
        f"({builder_done_c}) -- slices are not graded as they land"
    )

    calls = read_calls(env)
    assert calls, "the fake opencode was never invoked"
    by_agent: dict[str, set[str]] = {}
    for c in calls:
        by_agent.setdefault(c["agent"], set()).add(c["model"])
    assert by_agent["trio-builder"] == {cfg.models["builder"]}
    assert by_agent["trio-lead"] == {cfg.models["lead"]}
    assert by_agent["trio-evaluator"] == {cfg.models["evaluator"]}
    assert len({c["pid"] for c in calls if c["agent"] == "trio-builder"}) == 3

    steplib.assert_no_omnigent_loaded(ol_repo)

    log = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    for sid in ("a", "b", "c"):
        assert f"slice({sid}):" in log, log
        assert (ol_repo / f"{sid}.py").is_file()


# ===========================================================================
# E2: slice-eval ITERATE -> fault -> fix builder -> new retired sha -> SHIP.
# ===========================================================================


def test_open_loop_slice_eval_iterate_is_repaired(ol_repo_one_slice: Path, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    ol_repo = ol_repo_one_slice
    # Serial slice-evals: this scenario's QUEUE.md fault bookkeeping
    # (ol_common.next_fault_id/append_fault) is not safe against two
    # concurrent evaluator turns computing the same "next free fault id" at
    # once -- not a thing a single-threaded QUEUE.md fence writer (any real
    # Evaluator turn) ever has to contend with.
    cfg = make_cfg(make_key_file(tmp_path), slice_eval_concurrency=1)
    install_fake_for_process(tmp_path, monkeypatch, "ol_iterate.py")

    mailbox = ol_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=6, root_free=True)

    assert result["status"] == "shipped", result
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    queue_text = (mailbox / "QUEUE.md").read_text(encoding="utf-8")
    retired_shas = [
        line.split("sha:", 1)[1].strip()
        for line in queue_text.splitlines() if line.strip().startswith("sha:")
    ]
    # Exactly two: the ITERATEd build and its fault fix (distinct shas).
    assert len(retired_shas) == 2, queue_text
    assert len(set(retired_shas)) == 2, "must be 2 DISTINCT shas (pre-fix and post-fix)"
    assert "status: done" in queue_text, queue_text
    assert "status: open" not in queue_text and "status: taken" not in queue_text, queue_text

    verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert f"## slice a @{retired_shas[0]} — ITERATE" in verdict_text, verdict_text
    assert f"## slice a @{retired_shas[-1]} — SHIP" in verdict_text, verdict_text
    assert "VERDICT: SHIP" in verdict_text, verdict_text

    log = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    # The exact fault id fixed (f1 vs f2) depends on which of the two
    # duplicate ITERATE shas (see above) the Evaluator happened to grade
    # first -- a real race, not something this test controls; either way
    # exactly one `slice(a): fix f<N>` commit lands.
    assert re.search(r"slice\(a\): fix f\d+", log), log
    assert (ol_repo / "a.py").read_text(encoding="utf-8").strip() == "print('a')"


# ===========================================================================
# E3: CLI subprocess; a slice-eval turn sleeps after a marker; SIGKILL the
# driver; the orphan is alive; resume kills it and re-grades; SHIP; lands.
# ===========================================================================


def test_open_loop_crash_mid_slice_eval_then_resume(ol_repo_one_slice: Path, tmp_path: Path) -> None:
    ol_repo = ol_repo_one_slice
    key_file = make_key_file(tmp_path)
    cfg_path = write_ol_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0,
                                 idle_seconds=60.0)
    state_dir = tmp_path / "fakestate"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_crash.py"))
    env["FAKE_OC_STATE"] = str(state_dir)
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    env["TRIO_OPENCODE_POLL_SECONDS"] = "0.05"

    mailbox = ol_repo / "loop"
    first = spawn_cli(["start", "--mailbox", str(mailbox)], env)
    try:
        marker = state_dir / "markers" / "slice-eval-crashed"
        assert wait_for_file(marker, deadline=20.0), \
            "first driver never reached the sleeping slice-eval turn"
        orphan_pid = int(marker.read_text().strip())

        # Kill the DRIVER process itself (not its process group): the
        # sleeping fake-opencode slice-eval turn (its own session/process
        # group, start_new_session=True) is left running, orphaned.
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=15.0)
        os.kill(orphan_pid, 0)  # still alive: a real orphan
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10.0)

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    # `resume` took over the dead driver's auto-released flock instead of
    # refusing (exit 9 / status "refused" is what a BUSY lock looks like).
    assert result["status"] == "shipped", result

    with pytest.raises(ProcessLookupError):
        os.kill(orphan_pid, 0)  # the orphan slice-eval was killed by resume

    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    # No leftover eval-* worktrees registered in the home repo.
    wt_list = subprocess.run(["git", "-C", str(ol_repo), "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
    assert "eval-" not in wt_list, wt_list

    state = steplib.TL._read_state(mailbox / "STATE.md")
    assert state["iteration"].strip() == "1", state  # never reverted below the pass count

    log = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert log.count("loop: iteration 1 — SHIP") == 1, log
    assert "slice(a):" in log, log


# ===========================================================================
# E3b: ol-repair blocking issue #1 -- SIGKILL the DRIVER between
# `_merge_and_retire`'s `git merge` and its `olqueue.append_retired` call
# (never reached): the merge commit lands on the Lead branch, but QUEUE.md
# never gets the matching `retired:` entry. Resume must reconcile it instead
# of wedging (re-planning a slice whose work is already on the branch).
# ===========================================================================


_KILL_AFTER_MERGE_SCRIPT = """\
import os, signal, sys
from pathlib import Path
sys.path.insert(0, {driver_root!r})
from trio_opencode import olqueue, cli
orig = olqueue.append_retired
def patched(mailbox, *, slice_id, sha, at, repo=None):
    if slice_id == os.environ.get("EV_KILL_SLICE"):
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(mailbox, slice_id=slice_id, sha=sha, at=at, repo=repo)
olqueue.append_retired = patched
sys.argv = ["trio-opencode", *sys.argv[1:]]
sys.exit(cli.main())
"""


def test_open_loop_sigkill_between_merge_and_retire_then_resume(
        ol_repo_one_slice: Path, tmp_path: Path) -> None:
    ol_repo = ol_repo_one_slice
    key_file = make_key_file(tmp_path)
    cfg_path = write_ol_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0,
                                 idle_seconds=60.0)
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_merge_crash.py"))
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    env["TRIO_OPENCODE_POLL_SECONDS"] = "0.05"
    env["EV_KILL_SLICE"] = "a"

    killer = tmp_path / "kill_after_merge.py"
    killer.write_text(_KILL_AFTER_MERGE_SCRIPT.format(driver_root=str(DRIVER_ROOT)),
                      encoding="utf-8")

    mailbox = ol_repo / "loop"
    first = subprocess.run([sys.executable, str(killer), "start", "--mailbox", str(mailbox)],
                           env=env, capture_output=True, text=True, timeout=120,
                           start_new_session=True)
    assert first.returncode == -9, (first.returncode, first.stdout[-1500:], first.stderr[-3000:])

    # The crash window: a merge commit for `a` landed on the Lead branch
    # (root-free: a SEPARATE worktree from `ol_repo`, named in the run
    # registry), but QUEUE.md has no `retired:` entry for it at all --
    # exactly the gap `openloop._reconcile_merge_retirement` repairs on the
    # next `drive()`.
    registry = json.loads(driver.registry_path(mailbox).read_text(encoding="utf-8"))
    lead_wt = Path(registry["lead_worktree"])
    log = subprocess.run(["git", "-C", str(lead_wt), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert re.search(r"^merge slice a \(", log, re.M), log
    # root-free: the LIVE mailbox during the run is the Lead worktree's own
    # copy, not `ol_repo/loop` (the home mailbox only catches up on land).
    assert olqueue.latest_retired(lead_wt / "loop") == {}

    resumed = run_cli(["resume", "--mailbox", str(mailbox)], env, timeout=60.0)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    result = json.loads(resumed.stdout)
    assert result["status"] == "shipped", result
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    queue_text = (mailbox / "QUEUE.md").read_text(encoding="utf-8")
    retired_shas = [
        line.split("sha:", 1)[1].strip()
        for line in queue_text.splitlines() if line.strip().startswith("sha:")
    ]
    # Exactly ONE retired entry for `a`: the orphaned merge was reconciled
    # in place, never re-dispatched/re-merged a second time.
    assert len(retired_shas) == 1, queue_text
    log2 = subprocess.run(["git", "-C", str(ol_repo), "log", "--format=%s"],
                          capture_output=True, text=True).stdout
    assert log2.count("merge slice a (") == 1, log2
    assert len(re.findall(r"^loop: iteration \d+ — SHIP$", log2, re.M)) == 1, log2

    wt_list = subprocess.run(["git", "-C", str(ol_repo), "worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
    assert wt_list.count("worktree ") == 1, wt_list


# ===========================================================================
# ol-harden round 4: the merge-intent record replaces git-history inference.
# A mailbox that is tracked, untracked or gitignored -- root-free or in-place,
# with or without a commit made after the crash -- resumes from a SIGKILL in
# the merge->retire gap and ships; a reused mailbox imports nothing.
# ===========================================================================


_KILL_GAP_SCRIPT = """\
import os, signal, sys
sys.path.insert(0, {driver_root!r})
from trio_opencode import olqueue, openloop, cli
if os.environ.get("EV_NO_INTENT") == "1":
    openloop._MERGE_INTENT_ENABLED = False   # base-revert sanity: no write-ahead record
orig = olqueue.append_retired
def patched(mailbox, *, slice_id, sha, at, repo=None):
    if slice_id == os.environ.get("EV_KILL_SLICE"):
        os.kill(os.getpid(), signal.SIGKILL)
    return orig(mailbox, slice_id=slice_id, sha=sha, at=at, repo=repo)
olqueue.append_retired = patched
sys.argv = ["trio-opencode", *sys.argv[1:]]
sys.exit(cli.main())
"""


def _track_mailbox(repo: Path, tracking: str) -> None:
    if tracking == "tracked":
        return
    _git(repo, "rm", "-r", "-q", "--cached", "loop")
    if tracking == "ignored":
        (repo / ".gitignore").write_text("loop/\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", f"mailbox {tracking}")


def _gap_crash_then_resume(repo: Path, tmp_path: Path, *, inplace: bool, tracking: str,
                           later_commit: bool = False, intent: bool = True) -> dict:
    _track_mailbox(repo, tracking)
    cfg_path = write_ol_cfg_file(tmp_path / "cfg.json", make_key_file(tmp_path),
                                 turn_seconds=120.0, idle_seconds=60.0)
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_merge_crash.py"))
    env.update({"TRIO_OPENCODE_CONFIG": str(cfg_path), "TRIO_OPENCODE_POLL_SECONDS": "0.05",
                "EV_KILL_SLICE": "a"})
    if not intent:
        env["EV_NO_INTENT"] = "1"
    killer = tmp_path / "kill_in_gap.py"
    killer.write_text(_KILL_GAP_SCRIPT.format(driver_root=str(DRIVER_ROOT)), encoding="utf-8")
    mailbox = repo / "loop"
    flags = ["--in-place"] if inplace else []
    first = subprocess.run([sys.executable, str(killer), "start", "--mailbox", str(mailbox), *flags],
                           env=env, capture_output=True, text=True, timeout=120,
                           start_new_session=True)
    assert first.returncode == -9, (first.returncode, first.stdout[-1500:], first.stderr[-3000:])
    live_repo = repo if inplace else Path(json.loads(
        driver.registry_path(mailbox).read_text(encoding="utf-8"))["lead_worktree"])
    live_mailbox = live_repo / "loop"
    assert olqueue.latest_retired(live_mailbox) == {}
    assert re.search(r"^merge slice a \(", _git(live_repo, "log", "--format=%s"), re.M)
    if intent:
        assert (live_mailbox / ".merge-intent.json").is_file()
    if later_commit:
        # A commit made AFTER the crash, before resume (a human, a surviving
        # child): it also touches the mailbox's QUEUE.md when that is tracked.
        (live_repo / "later.txt").write_text("later\n", encoding="utf-8")
        _git(live_repo, "add", "later.txt")
        paths = ["later.txt"]
        if tracking == "tracked":
            with open(live_mailbox / "QUEUE.md", "a", encoding="utf-8") as fh:
                fh.write("\n")
            paths.append("loop/QUEUE.md")
        _git(live_repo, "commit", "-q", "-m", "unrelated work after the crash", "--", *paths)
    resumed = run_cli(["resume", "--mailbox", str(mailbox), *flags], env, timeout=90.0)
    result = json.loads(resumed.stdout)
    # Once landed the Lead worktree is gone and the root copies are current;
    # otherwise (needs_land) the live worktree still holds the run's state.
    src_repo, src_box = (repo, mailbox) if result["status"] == "shipped" else (live_repo, live_mailbox)
    result["_repo_log"] = _git(src_repo, "log", "--format=%s")
    result["_queue"] = (src_box / "QUEUE.md").read_text(encoding="utf-8")
    return result


@pytest.mark.parametrize("later_commit", [False, True], ids=["plain", "later-commit"])
@pytest.mark.parametrize("inplace", [False, True], ids=["rootfree", "inplace"])
@pytest.mark.parametrize("tracking", ["tracked", "untracked", "ignored"])
def test_open_loop_gap_crash_resumes_for_any_mailbox_tracking(
        ol_repo_one_slice: Path, tmp_path: Path, tracking: str, inplace: bool,
        later_commit: bool) -> None:
    result = _gap_crash_then_resume(ol_repo_one_slice, tmp_path, inplace=inplace,
                                    tracking=tracking, later_commit=later_commit)
    if tracking != "tracked" and not inplace:
        # Pre-existing, unrelated land limitation (a root-free land onto a
        # root checkout holding the untracked/ignored mailbox is refused,
        # `land-blocked: untracked working tree files would be overwritten`):
        # the run itself still completed -- the one merge was reconciled,
        # not redone.
        assert result["status"] in ("shipped", "needs_land"), result
    else:
        assert result["status"] == "shipped", result
    assert result["_repo_log"].count("merge slice a (") == 1, result["_repo_log"]
    assert result["_queue"].count("sha:") == 1, result["_queue"]


@pytest.mark.parametrize("tracking,inplace", [("tracked", False), ("ignored", True)])
def test_open_loop_gap_crash_without_intent_record_wedges(
        ol_repo_one_slice: Path, tmp_path: Path, tracking: str, inplace: bool) -> None:
    """Base-revert sanity: with the write-ahead record switched off, the
    same crash is not repaired (nothing infers it from git history any
    more), so the gap-crash tests above genuinely depend on the record."""
    result = _gap_crash_then_resume(ol_repo_one_slice, tmp_path, inplace=inplace,
                                    tracking=tracking, intent=False)
    assert result["status"] != "shipped", result


def test_open_loop_inplace_reused_mailbox_after_unshipped_goal_imports_nothing(
        ol_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-3 D2: goal 1 stops at max-iterations (its slice `a` merged, no
    SHIP, QUEUE.md never committed), then the same in-place mailbox is reset
    to a byte-identical skeleton for goal 2. With no merge-intent record
    nothing is imported -- 0 `reconciled` lines -- and goal 2 ships."""
    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    install_fake_for_process(tmp_path, monkeypatch, "ol_iterate.py")
    mailbox = ol_repo / "loop"
    skeleton = (mailbox / "QUEUE.md").read_text(encoding="utf-8")

    result1 = driver.run(mailbox, cfg, mode="start", max_iterations=1, root_free=False)
    assert result1["status"] != "shipped", result1
    assert "merge slice a (" in _git(ol_repo, "log", "--format=%s")
    assert not (mailbox / ".merge-intent.json").exists()

    _reset_mailbox(mailbox)
    assert (mailbox / "QUEUE.md").read_text(encoding="utf-8") == skeleton
    for sid in ("a", "b", "c"):
        (ol_repo / f"{sid}.py").unlink(missing_ok=True)
    install_fake_for_process(tmp_path, monkeypatch, "ol_happy.py")
    result2 = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False)
    assert result2["status"] == "shipped", result2
    assert "reconciled crash-orphaned" not in (mailbox / "LOG.md").read_text(encoding="utf-8")
    retired = [line.split("slice:", 1)[1].strip()
               for line in (mailbox / "QUEUE.md").read_text(encoding="utf-8").splitlines()
               if "slice:" in line]
    assert sorted(retired) == ["a", "b", "c"], retired


# ===========================================================================
# E3c: ol-repair blocking issue #2 -- exit drain must not let the process
# linger for an abandoned slice-eval's full turn timeout.
# ===========================================================================


PLAN_AB_Z = """\
# Plan

```yaml
slices:
  - id: a
    writes: [a.py]
    reads: []
    accepts: ["a.py exists | oracle: value"]
  - id: b
    writes: [b.py]
    reads: []
    accepts: ["b.py exists | oracle: value"]
  - id: z
    writes: [z.py]
    reads: []
    accepts: ["z.py exists | oracle: value"]
```

## Verification standard
implement-then-smoke

full_check: true
"""


@pytest.fixture
def ol_repo_drain(tmp_path: Path) -> Path:
    """Like ``ol_repo``, but PLAN.md declares three slices (`a`, `b`, `z`)
    and the Lead scenario (``ol_drain.py``) never builds `z` on purpose --
    see that scenario's own docstring."""
    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip a.py and b.py.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(PLAN_AB_Z, encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")
    return root


def test_open_loop_exit_drain_kills_abandoned_slice_eval(
        ol_repo_drain: Path, tmp_path: Path) -> None:
    """Blocking issue #2: `z` is never built, so the Lead's `slices: []`
    passes eventually stall the run (`status: error`) while slice `b`'s
    slice-eval is still sleeping well past ``--slice-eval-drain-seconds``.
    The CLI must exit close to the drain budget, not linger for the eval's
    full (120s) turn timeout."""
    key_file = make_key_file(tmp_path)
    cfg_path = write_ol_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=120.0,
                                 idle_seconds=100.0, evaluator_turn_seconds=120.0)
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_drain.py"))
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    env["TRIO_OPENCODE_POLL_SECONDS"] = "0.05"
    env["EV_SLOW"] = "25"

    mailbox = ol_repo_drain / "loop"
    t0 = time.monotonic()
    proc = spawn_cli(["start", "--mailbox", str(mailbox), "--slice-eval-drain-seconds", "1"], env)
    out, err = proc.communicate(timeout=90.0)
    dt = time.monotonic() - t0

    markers = Path(env["FAKE_OC_STATE"]) / "markers"
    assert (markers / "slow-eval").is_file(), (out[-2000:], err[-4000:])
    slow_pid = int((markers / "slow-eval").read_text().strip())

    # The bug: the process used to linger ~25s (the eval's full sleep). The
    # fix exits close to the 1s drain budget plus ordinary process overhead.
    assert dt < 15.0, (dt, out[-2000:], err[-4000:])
    result = json.loads(out)
    assert result["status"] == "error", result

    # The abandoned slice-eval's own process was actually killed, not left
    # to finish naturally after the driver process exited.
    assert not (markers / "slow-eval-done").exists()
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(slow_pid, 0)


# ===========================================================================
# E4: PLAN.md declares `repos:`; slice `a` home, slice `b` in repo `be`;
# integration-eval performs per-repo SHIP retirement; land ff's both.
# ===========================================================================


@pytest.fixture
def ol_repo_multirepo(tmp_path: Path) -> tuple[Path, Path]:
    """A home repo with a QUEUE.md mailbox whose PLAN.md declares a second
    repo `be` (r15 ``repos:``) at an absolute path outside home, and two
    slices: `a` (home) and `b` (repo `be`)."""
    be_root = tmp_path / "be-repo"
    be_root.mkdir()
    _git(be_root, "init", "-q", "-b", "main")
    (be_root / "README").write_text("be\n", encoding="utf-8")
    _git(be_root, "add", "README")
    _git(be_root, "commit", "-q", "-m", "init be")

    root = tmp_path / "product"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text("# Goal\nShip a.py (home) and b.py (repo be).\n",
                                 encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text(
        "# Plan\n\n"
        "```yaml\nslices:\n"
        "  - id: a\n    writes: [a.py]\n    reads: []\n"
        "  - id: b\n    writes: [b.py]\n    reads: []\n    repo: be\n"
        "```\n\n"
        "```yaml\nrepos:\n"
        f"  - name: be\n    path: {be_root}\n"
        "```\n", encoding="utf-8",
    )
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")
    return root, be_root


@pytest.mark.parametrize("isolate", [True, False], ids=["isolated", "no-isolation"])
def test_open_loop_multi_repo_ships_and_lands_both(
    ol_repo_multirepo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    isolate: bool,
) -> None:
    """Both isolation modes: the integration-eval prompt's MULTI-REPO pin
    listing names, per declared repo, its Lead aggregate (where the per-repo
    empty SHIP retirement commit must land for ``rootfree.land()`` to
    fast-forward `be`'s base branch) -- under isolation the grading copy is a
    separate detached worktree, never the retirement target."""
    home_repo, be_repo = ol_repo_multirepo
    cfg = make_cfg(make_key_file(tmp_path), isolate_workers=isolate)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_multirepo.py")

    mailbox = home_repo / "loop"
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    retired_text = (mailbox / "QUEUE.md").read_text(encoding="utf-8")
    assert "slice: a" in retired_text, retired_text
    assert "slice: b" in retired_text, retired_text
    assert "repo: be" in retired_text, retired_text

    calls = read_calls(env)
    assert calls, "the fake opencode was never invoked"
    eval_calls = [c for c in calls if c["agent"] == "trio-evaluator"]
    assert any("pins" in c["prompt"] or "`be`:" in c["prompt"] for c in eval_calls), \
        "the integration-eval prompt never mentioned the declared repo's pin"

    log_home = subprocess.run(["git", "-C", str(home_repo), "log", "--format=%s"],
                              capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log_home, log_home
    assert "slice(a):" in log_home, log_home
    assert (home_repo / "a.py").is_file()
    branch_home = subprocess.run(["git", "-C", str(home_repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    assert branch_home == "main"

    log_be = subprocess.run(["git", "-C", str(be_repo), "log", "--format=%s"],
                            capture_output=True, text=True).stdout
    assert "slice(b):" in log_be, log_be
    assert "loop: iteration 1 — SHIP" in log_be, log_be
    assert (be_repo / "b.py").is_file()
    branch_be = subprocess.run(["git", "-C", str(be_repo), "rev-parse", "--abbrev-ref", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
    assert branch_be == "main"

    steplib.assert_no_omnigent_loaded(home_repo)


# ===========================================================================
# E5: ``--acceptance`` / ``acceptance=True`` -- a frozen pack SHIPs.
# ===========================================================================


def test_open_loop_acceptance_on_ships(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "accproduct"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text(
        "# Goal\nShip app.py: `python3 app.py N` prints hello N for every N.\n"
        "Keep the README.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")

    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_acceptance.py")
    monkeypatch.setenv("TRIO_NATIVE_JOBS", "inline")

    mailbox = box
    result = driver.run(mailbox, cfg, mode="start", max_iterations=4, root_free=False,
                        acceptance=True)

    assert result["status"] == "shipped", result
    assert result["code"] == 0, result
    acc = result["acceptance"]
    assert acc["enabled"] is True

    acc_calls = [c for c in read_calls(env) if c["agent"] == "trio-acceptance"]
    assert len(acc_calls) == 1, acc_calls

    log = subprocess.run(["git", "-C", str(root), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert any(s.startswith("acceptance: freeze") for s in log.splitlines()), log

    retired_text = (mailbox / "QUEUE.md").read_text(encoding="utf-8")
    assert "slice: app" in retired_text, retired_text


# ===========================================================================
# E6: a builder's permission denial stops the run fatally and surfaces it.
# ===========================================================================


def test_open_loop_permission_denial_stops_and_surfaces(ol_repo: Path, tmp_path: Path) -> None:
    key_file = make_key_file(tmp_path)
    cfg_path = write_ol_cfg_file(tmp_path / "cfg.json", key_file, turn_seconds=30.0,
                                 idle_seconds=20.0)
    env = install_fake(tmp_path, str(SCENARIOS_DIR / "ol_permission.py"))
    env["TRIO_OPENCODE_CONFIG"] = str(cfg_path)
    env["TRIO_OPENCODE_POLL_SECONDS"] = "0.05"

    mailbox = ol_repo / "loop"
    started = time.monotonic()
    try:
        # A correct fast-kill (same stderr-watch ``_call_role`` uses for
        # every role, lockstep or open-loop) stops this in well under 20s
        # (test_e2e.py's own single-builder lockstep equivalent asserts
        # elapsed < 15.0); 30s is a generous bound before failing loud
        # instead of hanging the suite on a known-slow path.
        proc = run_cli(["start", "--mailbox", str(mailbox)], env, timeout=30.0)
    except subprocess.TimeoutExpired as exc:
        # Best-effort cleanup: a killed CLI subprocess leaves its own
        # (`start_new_session=True`) builder children as orphans, same as
        # the deliberate crash+resume tests -- but nothing here resumes to
        # reap them, so kill them directly by their recorded pids.
        markers = Path(env["FAKE_OC_STATE"]) / "markers"
        for name in ("builder-pid-a", "builder-pid-b"):
            marker = markers / name
            if marker.is_file():
                try:
                    os.kill(int(marker.read_text().strip()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass
        pytest.fail(
            "open-loop permission denial did not stop the run within 30s (observed on the "
            "base: ~100s, ending in a plain idle-timeout `status: error` with EMPTY "
            "`role_denials` -- the permission-ask fast-kill this driver's single-builder "
            "lockstep path has (test_e2e.py's test_permission_denial_kills_the_turn_quickly_"
            "no_orphan) does not appear to trigger for a wave-dispatched open-loop builder; "
            f"likely the same in-flight gap as 'fatal stop cancels sibling turns'): {exc}")
    elapsed = time.monotonic() - started

    assert proc.returncode == 3, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "error", result
    assert result.get("role_denials"), result
    assert any("permission" in d.get("text", "").lower() for d in result["role_denials"]), result
    assert elapsed < 20.0, f"took {elapsed:.1f}s -- the permission-denied turn should stop quickly"

    markers = Path(env["FAKE_OC_STATE"]) / "markers"
    pid_a = int((markers / "builder-pid-a").read_text().strip())
    pid_b = int((markers / "builder-pid-b").read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid_b, 0)  # the denied builder is gone
    # Known in-flight ol-harden item ("fatal stop cancels sibling turns"):
    # the OTHER builder of the same wave must also be gone once the run
    # stops fatally -- nothing may be left running after a fatal stop.
    with pytest.raises(ProcessLookupError):
        os.kill(pid_a, 0)

    retired_text = (mailbox / "QUEUE.md").read_text(encoding="utf-8")
    assert "slice: a" not in retired_text, retired_text
    assert "slice: b" not in retired_text, retired_text


# ===========================================================================
# E7: a mailbox WITHOUT QUEUE.md stays on the lockstep path (D1).
# ===========================================================================


def test_open_loop_lockstep_mailbox_without_queue_md_stays_lockstep(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not (git_repo / "loop" / "QUEUE.md").exists()
    cfg = make_cfg(make_key_file(tmp_path))
    env = install_fake_for_process(tmp_path, monkeypatch, "happy.py")

    result = driver.run(git_repo / "loop", cfg, mode="start", max_iterations=4, root_free=True)

    assert result["status"] == "shipped", result
    assert result["land"] is not None and result["land"]["status"] == "landed", result["land"]

    calls = read_calls(env)
    assert calls, "the fake opencode was never invoked"
    assert all("OPEN-LOOP CONTEXT" not in c["prompt"] for c in calls), \
        "a mailbox without QUEUE.md must never take the open-loop path"

    log = subprocess.run(["git", "-C", str(git_repo), "log", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "loop: iteration 1 — SHIP" in log, log
    assert "slice(a):" in log and "slice(b):" in log, log


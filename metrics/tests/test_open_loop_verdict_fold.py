"""Restored per-slice VERDICT.md sections are folded into the SHIP retirement.

Live defect (speed/runs/C2): the integration Evaluator rewrote VERDICT.md
whole and committed it as ``loop: iteration 1 — SHIP`` with no ``## slice``
sections; the driver's clobber restore then re-appended them to the working
tree only, so ``_ship_acceptance_once`` saw VERDICT.md dirty after
retirement and every builder worktree was retained. These tests drive the
real open-loop driver against a REAL temp git repo (the per-slice commit
gate is stubbed) with scripted in-process runners.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests.test_open_loop_driver import (
    EMPTY_QUEUE,
    QueueModel,
    ScriptedEvalRunner,
    ScriptedLeadRunner,
    VerdictModel,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)

TRIOCTL = Path(__file__).resolve().parents[2] / "omnigent" / "trioctl"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Verdict Fold Test",
    "GIT_AUTHOR_EMAIL": "verdict-fold@example.com",
    "GIT_COMMITTER_NAME": "Verdict Fold Test",
    "GIT_COMMITTER_EMAIL": "verdict-fold@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}

SHIP_SUBJECT = "loop: iteration 1 — SHIP"
MAILBOX = "loop-qual"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(trio_loop.RETIREMENT_WAIT_ENV, "0")
    monkeypatch.setenv(trio_loop.RETIREMENT_POLL_ENV, "0")
    monkeypatch.setattr(trio_loop, "_per_slice_gate", lambda *a, **k: 0)


def _load_trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl_fold_test", str(TRIOCTL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def plan(*ids: str) -> str:
    body = "".join(
        f"  - id: {sid}\n    writes: [pkg/{sid}.py]\n    reads: []\n"
        for sid in ids
    )
    return f"```yaml\nslices:\n{body}```\n"


def make_repo(tmp_path: Path, ids: tuple[str, ...]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    mailbox = repo / MAILBOX
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    (mailbox / "PLAN.md").write_text(plan(*ids), encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "QUEUE.md").write_text(EMPTY_QUEUE, encoding="utf-8")
    # Driver-owned runtime files are not product files.
    (repo / ".gitignore").write_text(
        f"{MAILBOX}/.*\n{MAILBOX}/tasks/\n", encoding="utf-8"
    )
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo, mailbox


def read_state(mailbox: Path) -> dict[str, str]:
    return trio_loop._read_state(mailbox / "STATE.md")


class Scenario:
    """Lead commits one product file per slice and retires it; each
    slice-eval appends its section; the integration Evaluator runs
    ``integration`` (the shape under test)."""

    def __init__(self, tmp_path: Path, ids: tuple[str, ...]) -> None:
        self.ids = ids
        self.repo, self.mailbox = make_repo(tmp_path, ids)
        lock = threading.Lock()
        self.queue = QueueModel(self.mailbox, lock)
        self.verdict = VerdictModel(self.mailbox, lock)
        self.shas: dict[str, str] = {}
        self.evaluator_commit: str | None = None

    def lead_pass(self, _mb) -> None:
        for sid in self.ids:
            (self.repo / "pkg" / f"{sid}.py").write_text(
                f"NAME = {sid!r}\n", encoding="utf-8"
            )
            git(self.repo, "add", "--", f"pkg/{sid}.py")
            git(self.repo, "commit", "-q", "-m", f"slice({sid}): add")
            self.shas[sid] = git(self.repo, "rev-parse", "HEAD").strip()
        for sid in self.ids:
            self.queue.retire(sid, self.shas[sid])

    def slice_action(self, sid: str):
        def act(_mb) -> None:
            self.verdict.append_slice_section(sid, self.shas[sid], "SHIP")
        return act

    def ship_header(self) -> str:
        state = read_state(self.mailbox)
        return (
            "VERDICT: SHIP\n\n"
            "iteration: 1\n"
            f"evaluated: {state['evaluated_sha']}\n"
            f"attempt: {state['evaluator_attempt']}\n"
            "integrated pin passes every check\n"
        )

    def commit_retirement(self) -> None:
        git(self.repo, "add", "-u", "--", MAILBOX)
        git(self.repo, "commit", "-q", "-m", SHIP_SUBJECT)
        self.evaluator_commit = git(self.repo, "rev-parse", "HEAD").strip()

    # --- integration Evaluator shapes ---------------------------------

    def integration_rewrites(self, _mb) -> None:
        """C2 shape: VERDICT.md rewritten whole, no slice sections."""
        (self.mailbox / "VERDICT.md").write_text(
            self.ship_header(), encoding="utf-8"
        )
        self.commit_retirement()

    def integration_keeps(self, _mb) -> None:
        """S/C baseline shape: SHIP header prepended, sections kept."""
        existing = (self.mailbox / "VERDICT.md").read_text(encoding="utf-8")
        (self.mailbox / "VERDICT.md").write_text(
            self.ship_header() + "\n" + existing, encoding="utf-8"
        )
        self.commit_retirement()

    def integration_rewrites_then_commits_on_top(self, kind: str):
        def act(mb) -> None:
            self.integration_rewrites(mb)
            if kind == "product":
                (self.repo / "pkg" / "extra.py").write_text("X = 1\n", encoding="utf-8")
                git(self.repo, "add", "--", "pkg/extra.py")
            else:
                (self.mailbox / "REPORT.md").write_text("notes\n", encoding="utf-8")
                git(self.repo, "add", "--", f"{MAILBOX}/REPORT.md")
            git(self.repo, "commit", "-q", "-m", "extra commit on top")
        return act

    def run(self, integration, **kwargs) -> int:
        scenario = self

        class LazySlices(dict):
            """Slice shas exist only after the Lead pass: resolve on pop."""

            def pop(self, key, *default):
                sid, sha = key
                assert scenario.shas.get(sid) == sha
                return scenario.slice_action(sid)

        lead = ScriptedLeadRunner([self.lead_pass])
        self.evaluator = ScriptedEvalRunner(integration_actions=[integration])
        self.evaluator.slice_actions = LazySlices()
        return trio_loop.run_open_loop(
            self.mailbox, 5, lead, self.evaluator, repo=self.repo,
            poll_seconds=0.01, **kwargs,
        )

    # --- observations -------------------------------------------------

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").strip()

    def head_paths(self) -> list[str]:
        return git(
            self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).split()

    def head_subject(self) -> str:
        return git(self.repo, "log", "-1", "--format=%s").strip()

    def head_verdict(self) -> str:
        return git(self.repo, "show", f"HEAD:{MAILBOX}/VERDICT.md")

    def verdict_dirty(self) -> str:
        return git(self.repo, "status", "--porcelain", "--", f"{MAILBOX}/VERDICT.md")

    def log(self) -> str:
        return (self.mailbox / "LOG.md").read_text(encoding="utf-8")

    def acceptance(self) -> dict:
        trioctl = _load_trioctl()
        return trioctl._ship_acceptance_once(self.mailbox, self.repo, trio_loop)


def _assert_all_sections(text: str, scenario: Scenario) -> None:
    for sid in scenario.ids:
        assert re.search(
            rf"^## slice {sid} @{scenario.shas[sid]} -- SHIP$", text, re.MULTILINE
        ), sid


FOUR = ("slug", "bounds", "chunking", "dedupe")


def _assert_folded(s: Scenario) -> None:
    head_verdict = s.head_verdict()
    assert head_verdict.startswith("VERDICT: SHIP\n")
    _assert_all_sections(head_verdict, s)
    assert s.verdict_dirty() == ""
    assert s.head() != s.evaluator_commit
    assert s.head_subject() == SHIP_SUBJECT
    assert s.head_paths() and all(
        p.startswith(f"{MAILBOX}/") for p in s.head_paths()
    )
    # Parent unchanged: the amend replaced only the retirement commit.
    assert git(s.repo, "rev-parse", "HEAD^").strip() == git(
        s.repo, "rev-parse", f"{s.evaluator_commit}^"
    ).strip()
    log = s.log()
    assert (
        f"open-loop: restored {len(s.ids)} clobbered per-slice section(s) "
        "in VERDICT.md after integration-eval" in log
    )
    assert re.search(
        rf"open-loop: folded {len(s.ids)} restored per-slice section\(s\) into "
        rf"retirement commit {s.evaluator_commit[:12]}->{s.head()[:12]}$",
        log, re.MULTILINE,
    )
    assert "could not be folded" not in log
    accepted = s.acceptance()
    assert "pending" not in accepted, accepted
    state = read_state(s.mailbox)
    assert accepted["evaluated"] == state["evaluated_sha"]
    # The graded pin is the product revision, not the mailbox commit.
    assert state["evaluated_sha"] == s.shas[s.ids[-1]]


# (1) C2 reproduction ------------------------------------------------------


def test_c2_rewritten_verdict_is_folded_into_retirement(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FOUR)
    code = s.run(s.integration_rewrites)
    assert code == 0
    assert read_state(s.mailbox)["status"] == "shipped"
    _assert_folded(s)


# (2) evaluator keeps the sections -----------------------------------------


def test_sections_kept_means_no_restore_and_no_amend(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FOUR)
    code = s.run(s.integration_keeps)
    assert code == 0
    assert s.head() == s.evaluator_commit
    log = s.log()
    assert "restored" not in log
    assert "fold" not in log
    assert s.verdict_dirty() == ""
    _assert_all_sections(s.head_verdict(), s)
    accepted = s.acceptance()
    assert "pending" not in accepted, accepted


# (3) HEAD is not the retirement commit -------------------------------------


@pytest.mark.parametrize("kind", ["mailbox", "product"])
def test_no_amend_when_head_is_not_the_retirement(tmp_path: Path, kind) -> None:
    s = Scenario(tmp_path, FOUR)
    code = s.run(s.integration_rewrites_then_commits_on_top(kind))
    head = s.head()
    assert head != s.evaluator_commit
    assert s.head_subject() == "extra commit on top"
    log = s.log()
    assert "open-loop: folded" not in log
    assert (
        "- iter 1 | loop | open-loop: restored sections could not be folded "
        f"into retirement (HEAD {head[:12]} is not the retirement commit)" in log
    )
    # Today's behaviour: the restored sections stay uncommitted.
    assert s.verdict_dirty().strip().startswith("M")
    assert "## slice" not in git(s.repo, "show", f"{s.evaluator_commit}:{MAILBOX}/VERDICT.md")
    _assert_all_sections((s.mailbox / "VERDICT.md").read_text(encoding="utf-8"), s)
    if kind == "mailbox":
        assert code == 0
        assert s.acceptance() == {
            "pending": "VERDICT.md has uncommitted edits after retirement"
        }
    else:
        # A product commit after the pin can never ship.
        assert code != 0
        assert "pending" in s.acceptance()


def test_no_amend_when_other_mailbox_paths_are_dirty(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FOUR)

    def rewrite_and_leave_plan_dirty(mb) -> None:
        s.integration_rewrites(mb)
        with (s.mailbox / "PLAN.md").open("a", encoding="utf-8") as fh:
            fh.write("\nuncommitted note\n")

    code = s.run(rewrite_and_leave_plan_dirty)
    assert code == 0
    assert s.head() == s.evaluator_commit
    assert (
        "could not be folded into retirement (other mailbox paths have "
        f"uncommitted edits: {MAILBOX}/PLAN.md)" in s.log()
    )
    assert s.verdict_dirty().strip()


def test_no_amend_when_something_is_staged(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FOUR)

    def rewrite_and_stage(mb) -> None:
        s.integration_rewrites(mb)
        (s.mailbox / "REPORT.md").write_text("staged\n", encoding="utf-8")
        git(s.repo, "add", "--", f"{MAILBOX}/REPORT.md")

    s.run(rewrite_and_stage)
    assert s.head() == s.evaluator_commit
    assert "could not be folded into retirement (index has staged changes" in s.log()


def test_iterate_verdict_is_never_folded(tmp_path: Path) -> None:
    """No SHIP, no retirement: the restore stays a working-tree edit and
    nothing is logged about folding."""
    s = Scenario(tmp_path, ("solo",))

    def iterate_rewrites(_mb) -> None:
        (s.mailbox / "VERDICT.md").write_text(
            "VERDICT: NEEDS_HUMAN\n", encoding="utf-8"
        )

    code = s.run(iterate_rewrites)
    assert code == 5
    log = s.log()
    assert "restored 1 clobbered" in log
    assert "fold" not in log


# (4) concurrency 4 ----------------------------------------------------------


def test_fold_at_slice_eval_concurrency_4(tmp_path: Path) -> None:
    s = Scenario(tmp_path, FOUR)
    code = s.run(s.integration_rewrites, slice_eval_concurrency=4)
    assert code == 0
    _assert_folded(s)


# (5) N=1 identical apart from the fold ---------------------------------------


def _loop_lines(log: str) -> list[str]:
    return [line for line in log.splitlines() if "| loop |" in line]


def test_n1_path_identical_apart_from_the_fold(tmp_path: Path) -> None:
    (tmp_path / "n1").mkdir()
    (tmp_path / "n4").mkdir()
    (tmp_path / "keep").mkdir()
    n1 = Scenario(tmp_path / "n1", FOUR)
    assert n1.run(n1.integration_rewrites) == 0
    n4 = Scenario(tmp_path / "n4", FOUR)
    assert n4.run(n4.integration_rewrites, slice_eval_concurrency=4) == 0
    keep = Scenario(tmp_path / "keep", FOUR)
    assert keep.run(keep.integration_keeps) == 0
    _assert_folded(n1)
    # Serial N=1 call order is unchanged: every slice-eval, then one
    # integration-eval.
    kinds = [c["context"]["kind"] for c in n1.evaluator.calls]
    assert kinds == ["slice-eval"] * 4 + ["integration-eval"]
    # Same loop log lines at N=1 and N=4 (shas differ per repo).
    strip = lambda lines: [re.sub(r"[0-9a-f]{12}", "<sha>", l) for l in lines]  # noqa: E731
    assert strip(_loop_lines(n1.log())) == strip(_loop_lines(n4.log()))
    # Versus the no-restore shape, the only extra loop lines are the
    # restore and the fold.
    extra = [l for l in _loop_lines(n1.log()) if l not in _loop_lines(keep.log())]
    assert len(extra) == 2
    assert "restored 4 clobbered" in extra[0]
    assert "folded 4 restored" in extra[1]
    # Both N end with the same set of committed sections.
    def sections(s: Scenario) -> set[str]:
        return {
            re.sub(r"@[0-9a-f]{40}", "@<sha>", l)
            for l in s.head_verdict().splitlines() if l.startswith("## slice")
        }
    assert sections(n1) == sections(n4) == sections(keep)

"""Integration-verdict field checks ignore appended open-loop slice sections.

Regression for live run 20260926T131520Z: a correct, bound integration
SHIP was never accepted because the open-loop ``## slice <id> @<sha> —
SHIP`` sections trioctl itself dispatched carry their own ``iteration:``
lines (``iteration: 0``), so the loop waited out its timeout and held
the session (``role_completion_uncertain``).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "omnigent" / "trioctl"
FIXTURE = Path(__file__).parent / "fixtures" / "live_20260926T131520Z_VERDICT.md"

ATTEMPT = "166ec6491a6740b7bbfd787fd0f5842b"
PIN = "f68922a6ebb812bf030a32b23d602a683a286d83"
CONTEXT = {"kind": "integration-eval", "pinned_sha": PIN, "evaluator_attempt": ATTEMPT}


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def trioctl():
    return _load("trioctl_verdict_scope", SCRIPT)


@pytest.fixture(scope="module")
def trio_loop():
    return _load("trio_loop_verdict_scope", ROOT / "metrics" / "trio_loop.py")


def _mailbox(tmp_path: Path, text: str) -> Path:
    mailbox = tmp_path / "loop"
    mailbox.mkdir(parents=True)
    (mailbox / "VERDICT.md").write_text(text, encoding="utf-8")
    return mailbox


def _ready(trioctl, tmp_path, text, iteration=1, context=CONTEXT, before=""):
    mailbox = _mailbox(tmp_path, text)
    return trioctl._role_artifact_ready(
        mailbox, "evaluator", iteration, before, 0.0, dict(context)
    )


def test_fixture_is_the_live_shape(trioctl) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    # The exact failure: unscoped fields disagree across sections.
    assert trioctl._verdict_field_values(text, "iteration") == ["1", "0", "1"]
    assert not trioctl._verdict_mentions_iteration(text, 1)
    assert "## slice text-utils" in text and "/home/" not in text


def test_live_verdict_is_ready_for_iteration_1(trioctl, tmp_path) -> None:
    assert _ready(trioctl, tmp_path, FIXTURE.read_text(encoding="utf-8"))


def test_live_verdict_rejected_for_iteration_2(trioctl, tmp_path) -> None:
    assert not _ready(
        trioctl, tmp_path, FIXTURE.read_text(encoding="utf-8"), iteration=2
    )


def test_live_verdict_rejected_for_other_attempt_or_pin(trioctl, tmp_path) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    other = dict(CONTEXT, evaluator_attempt="0" * 32)
    assert not _ready(trioctl, tmp_path / "a", text, context=other)
    other = dict(CONTEXT, pinned_sha="1" * 40)
    assert not _ready(trioctl, tmp_path / "b", text, context=other)


def test_unchanged_text_is_still_stale(trioctl, tmp_path) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert not _ready(trioctl, tmp_path, text, before=text)


def test_scoping_removes_only_slice_sections(trioctl) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    scoped = trioctl._integration_verdict_text(text)
    assert "## slice" not in scoped
    assert "slice_commit:" not in scoped
    assert scoped.startswith("VERDICT: SHIP\niteration: 1\n")
    assert "## Blocking issues\nNone.\n" in scoped
    assert trioctl._verdict_field_values(scoped, "iteration") == ["1"]


def test_scoping_resumes_at_next_non_slice_heading(trioctl) -> None:
    text = (
        "VERDICT: SHIP\niteration: 1\n\n"
        "## slice a @abcdef1 — SHIP\niteration: 0\n### sub\nx: 1\n"
        "## slice b @abcdef2 — ITERATE\niteration: 7\n"
        "## Notes\nkept\n# Top\nalso kept\n"
    )
    assert trioctl._integration_verdict_text(text) == (
        "VERDICT: SHIP\niteration: 1\n\n## Notes\nkept\n# Top\nalso kept\n"
    )


def test_non_slice_heading_naming_other_iteration_still_rejects(
    trioctl, tmp_path
) -> None:
    text = (
        f"VERDICT: SHIP\niteration: 1\nattempt: {ATTEMPT}\nevaluated: {PIN}\n\n"
        "## Notes for iteration 2\nfollow-up\n"
    )
    assert not _ready(trioctl, tmp_path, text)


def test_slice_fields_cannot_bind_attempt_or_sha(trioctl, tmp_path) -> None:
    # Only the slice section names the right attempt/pin: not bound.
    text = (
        "VERDICT: SHIP\niteration: 1\nattempt: deadbeef\nevaluated: " + "9" * 40 + "\n\n"
        f"## slice a @{PIN} — SHIP\n\niteration: 1\nattempt: {ATTEMPT}\n"
        f"evaluated: {PIN}\n"
    )
    assert not _ready(trioctl, tmp_path / "a", text)
    no_top = (
        "VERDICT: SHIP\niteration: 1\n\n"
        f"## slice a @{PIN} — SHIP\n\niteration: 1\nattempt: {ATTEMPT}\n"
        f"evaluated: {PIN}\n"
    )
    assert not _ready(trioctl, tmp_path / "b", no_top)


def test_differing_slice_fields_do_not_unbind_top_block(trioctl, tmp_path) -> None:
    text = (
        f"VERDICT: SHIP\niteration: 1\nattempt: {ATTEMPT}\nevaluated: {PIN}\n\n"
        "## slice a @1111111 — ITERATE\n\niteration: 0\nattempt: other\n"
        "evaluated: 1111111111111111111111111111111111111111\n"
    )
    assert _ready(trioctl, tmp_path, text)


def test_slice_sections_before_verdict_block_still_rejected(
    trioctl, tmp_path
) -> None:
    top = f"VERDICT: SHIP\niteration: 1\nattempt: {ATTEMPT}\nevaluated: {PIN}\n"
    swallowed = f"## slice a @{PIN} — SHIP\niteration: 1\n\n" + top
    headed = f"## slice a @{PIN} — SHIP\niteration: 1\n\n# Verdict\n" + top
    for index, text in enumerate((swallowed, headed)):
        scoped = trioctl._integration_verdict_text(text)
        assert not trioctl._verdict_line_ready(scoped)
        assert not _ready(trioctl, tmp_path / str(index), text)


def test_slice_eval_path_unchanged(trioctl, tmp_path) -> None:
    before = FIXTURE.read_text(encoding="utf-8")
    appended = before + (
        f"\n## slice x @{PIN} — SHIP\n\niteration: 9\nevaluated: {PIN}\n"
    )
    context = {"kind": "slice-eval"}
    # Append detected: ready regardless of the iteration fields.
    assert _ready(trioctl, tmp_path / "a", appended, iteration=1,
                  context=context, before=before)
    # No change: not ready.
    assert not _ready(trioctl, tmp_path / "b", before, iteration=1,
                      context=context, before=before)


def test_loop_core_fresh_artifact_scoped(trio_loop, tmp_path) -> None:
    mailbox = _mailbox(tmp_path, FIXTURE.read_text(encoding="utf-8"))
    assert trio_loop._fresh_evaluator_artifact(mailbox, 1, dict(CONTEXT))
    assert not trio_loop._fresh_evaluator_artifact(mailbox, 2, dict(CONTEXT))
    assert not trio_loop._fresh_evaluator_artifact(
        mailbox, 1, dict(CONTEXT, evaluator_attempt="0" * 32)
    )


def test_loop_core_binding_scoped(trio_loop, tmp_path) -> None:
    state = {"evaluator_attempt": ATTEMPT, "evaluated_sha": PIN}
    text = FIXTURE.read_text(encoding="utf-8")
    assert trio_loop._verdict_binds_lockstep(text, state, tmp_path)
    only_slice = (
        "VERDICT: SHIP\niteration: 1\n\n"
        f"## slice a @{PIN} — SHIP\nattempt: {ATTEMPT}\nevaluated: {PIN}\n"
    )
    assert not trio_loop._verdict_binds_lockstep(only_slice, state, tmp_path)


def test_helpers_match_between_trioctl_and_loop_core(trioctl, trio_loop) -> None:
    samples = [
        FIXTURE.read_text(encoding="utf-8"),
        "VERDICT: SHIP\n## slice a @abcdef1 — SHIP\nx\n## N\ny\n",
        "no slices\n",
        "",
    ]
    for text in samples:
        assert trioctl._integration_verdict_text(text) == (
            trio_loop._integration_verdict_text(text)
        )

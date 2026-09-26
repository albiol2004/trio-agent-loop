"""TRIO_OMNIGENT_IDLE_DWELL_SLICE_EVAL: per-kind idle dwell override."""
from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.tests.test_omnigent_loop import load_trioctl, make_mailbox, profile

BASE = "TRIO_OMNIGENT_IDLE_DWELL"
SLICE = "TRIO_OMNIGENT_IDLE_DWELL_SLICE_EVAL"


@pytest.fixture
def trioctl(monkeypatch):
    monkeypatch.delenv(BASE, raising=False)
    monkeypatch.delenv(SLICE, raising=False)
    return load_trioctl()


def test_defaults_are_unchanged(trioctl) -> None:
    for kind in (None, "lead-pass", "integration-eval", "slice-eval"):
        assert trioctl._idle_dwell(kind) == 30.0


def test_slice_eval_override_applies_only_to_slice_eval(trioctl, monkeypatch) -> None:
    monkeypatch.setenv(SLICE, "5")
    assert trioctl._idle_dwell("slice-eval") == 5.0
    for kind in (None, "lead-pass", "integration-eval"):
        assert trioctl._idle_dwell(kind) == 30.0


def test_base_override_is_inherited_by_slice_eval(trioctl, monkeypatch) -> None:
    monkeypatch.setenv(BASE, "12")
    assert trioctl._idle_dwell(None) == 12.0
    assert trioctl._idle_dwell("slice-eval") == 12.0
    monkeypatch.setenv(SLICE, "0")  # explicit zero is honoured
    assert trioctl._idle_dwell("slice-eval") == 0.0
    assert trioctl._idle_dwell("integration-eval") == 12.0


@pytest.mark.parametrize("bad", ["", "  ", "nope", "nan", "inf", "-3"])
def test_invalid_values_fall_back(trioctl, monkeypatch, bad) -> None:
    monkeypatch.setenv(SLICE, bad)
    assert trioctl._idle_dwell("slice-eval") == 30.0
    monkeypatch.setenv(BASE, bad)
    assert trioctl._idle_dwell(None) == 30.0


class PollClient:
    """No wait_session seam: the runner polls via _wait_for_session."""

    def __init__(self) -> None:
        self.n = 0

    def create(self, agent_id, model, message, title):
        self.n += 1
        return {"id": f"s-{self.n}"}

    def get_items(self, session_id, **_kw):
        return {"items": [{"role": "assistant", "status": "completed"}]}


def _dwells_for(trioctl, tmp_path: Path, monkeypatch, role, context):
    mailbox = make_mailbox(tmp_path / role)  # separate mailbox per call
    runner = trioctl.OmnigentRunner(
        tmp_path, client=PollClient(), config=profile(), timeout=30.0, interval=0.001,
    )
    runner._agent_id = lambda r: f"{r}-agent"
    runner._prompt = lambda *a: "prompt"
    seen: list[float] = []

    def fake_wait(client, session_id, *, timeout, interval, stable_idle=0.0):
        seen.append(stable_idle)
        return {"id": session_id, "status": "idle"}

    ready = {"n": 0}

    def fake_ready(*a, **k):
        ready["n"] += 1
        return ready["n"] > 1  # force one artifact re-wait

    monkeypatch.setattr(trioctl, "_wait_for_session", fake_wait)
    monkeypatch.setattr(trioctl, "_role_artifact_ready", fake_ready)
    monkeypatch.setattr(trioctl.time, "sleep", lambda s: None)
    assert runner.run(role, 1, mailbox, context) == 0
    return seen


def test_dispatch_passes_the_kind_dwell_to_both_waits(
    trioctl, tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "evaluator").mkdir()
    (tmp_path / "lead").mkdir()
    monkeypatch.setenv(SLICE, "7")
    slice_ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "a", "sha": "abc"}
    # create-wait + one _wait_for_role_artifact re-wait: both use 7 s.
    assert _dwells_for(trioctl, tmp_path, monkeypatch, "evaluator", slice_ctx) == [7.0, 7.0]
    lead_ctx = {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None}
    assert _dwells_for(trioctl, tmp_path, monkeypatch, "lead", lead_ctx) == [30.0, 30.0]

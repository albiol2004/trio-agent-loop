"""Offline tests for opt-in cursor worker lifecycle events."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import stat
import threading
from pathlib import Path

import pytest

from test_trioctl import load_trioctl, model, profile

OMNI = Path(__file__).parents[1]
SECRET = "PROMPT_SECRET_TOKEN_do_not_log"


def load_events():
    path = OMNI / "worker_events.py"
    loader = importlib.machinery.SourceFileLoader("we", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fake_agent(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _ctx(path: Path, **extra):
    we = load_events()
    ctx = {
        "events_path": path,
        "invocation_id": we.new_invocation_id(extra.get("inv")),
        "run_id": extra.get("run_id", "run-a"),
        "iteration": extra.get("iteration"),
        "slice_id": extra.get("slice_id"),
    }
    return ctx


def test_disabled_writes_no_files(tmp_path: Path, monkeypatch):
    trioctl = load_trioctl()
    agent = _fake_agent(tmp_path / "agent", "print('OK')")
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(agent))
    before = {p.name for p in tmp_path.iterdir()}
    out = trioctl.run_cursor_worker(
        "builder",
        profile(),
        prompt="task",
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
    )
    assert out == "OK"
    after = {p.name for p in tmp_path.iterdir()}
    assert "worker-events.jsonl" not in after
    assert after == before


def test_relative_events_without_mailbox_disabled():
    we = load_events()
    assert we.resolve_events_path("events.jsonl") is None
    assert we.resolve_events_path("") is None


def test_session_ids_env_does_not_enable_events(
    tmp_path: Path, monkeypatch
):
    trioctl = load_trioctl()
    monkeypatch.setenv(trioctl.SESSION_IDS_ENV_VAR, str(tmp_path / "ids"))
    monkeypatch.delenv(trioctl.WORKER_EVENTS_FILE_ENV, raising=False)
    ctx = trioctl._worker_event_ctx()
    assert ctx == {}


def test_concurrent_builders_overlap(tmp_path: Path, monkeypatch):
    trioctl = load_trioctl()
    we = load_events()
    agent = _fake_agent(
        tmp_path / "agent",
        "import time\ntime.sleep(0.35)\nprint('OK')",
    )
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(agent))
    events = tmp_path / "loop" / "worker-events.jsonl"
    events.parent.mkdir()
    errors = []

    def run_one(name: str) -> None:
        ctx = _ctx(events, inv=None, run_id="run-a")
        try:
            trioctl.run_cursor_worker(
                "builder",
                profile(),
                prompt=f"task {name} {SECRET}",
                workspace=tmp_path,
                models=[model("gpt-5.6-luna-max")],
                events=ctx,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run_one, args=("a",))
    t2 = threading.Thread(target=run_one, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    text = events.read_text()
    assert SECRET not in text
    payload = we.report(events, run_id="run-a")
    assert len(payload["builders"]) == 2
    assert all(b["duration_ns"] and b["duration_ns"] > 0 for b in payload["builders"])
    assert any(o["overlap_ns"] > 0 for o in payload["overlaps"])
    assert "not proof" in payload["caution"]


def test_sequential_builders_no_overlap(tmp_path: Path, monkeypatch):
    trioctl = load_trioctl()
    we = load_events()
    agent = _fake_agent(
        tmp_path / "agent",
        "import time\ntime.sleep(0.05)\nprint('OK')",
    )
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(agent))
    events = tmp_path / "events.jsonl"
    for _ in range(2):
        trioctl.run_cursor_worker(
            "builder",
            profile(),
            prompt="task",
            workspace=tmp_path,
            models=[model("gpt-5.6-luna-max")],
            events=_ctx(events),
        )
    payload = we.report(events)
    assert len(payload["builders"]) == 2
    assert all(o["overlap_ns"] == 0 for o in payload["overlaps"])


def test_timeout_spawn_fail_incomplete_nonzero(tmp_path: Path, monkeypatch):
    trioctl = load_trioctl()
    we = load_events()
    events = tmp_path / "events.jsonl"
    sleepy = _fake_agent(
        tmp_path / "slow",
        "import time\ntime.sleep(5)\nprint('OK')",
    )
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(sleepy))
    with pytest.raises(trioctl.TrioctlError):
        trioctl.run_cursor_worker(
            "builder",
            profile(),
            prompt="task",
            workspace=tmp_path,
            models=[model("gpt-5.6-luna-max")],
            timeout=0.15,
            events=_ctx(events),
        )
    monkeypatch.setattr(
        trioctl.shutil, "which", lambda c: str(tmp_path)
    )
    with pytest.raises(trioctl.TrioctlError):
        trioctl.run_cursor_worker(
            "builder",
            profile(),
            prompt="task",
            workspace=tmp_path,
            models=[model("gpt-5.6-luna-max")],
            events=_ctx(events),
        )
    failer = _fake_agent(tmp_path / "fail", "raise SystemExit(3)")
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(failer))
    with pytest.raises(trioctl.TrioctlError, match="exited 3"):
        trioctl.run_cursor_worker(
            "builder",
            profile(),
            prompt="task",
            workspace=tmp_path,
            models=[model("gpt-5.6-luna-max")],
            events=_ctx(events),
        )
    # Incomplete spawned-only row is unknown, not a failed duration.
    we.emit(
        events,
        we.base_record(
            invocation_id=we.new_invocation_id(None),
            kind="spawned",
            role="builder",
            pid=1,
        ),
    )
    payload = we.report(events)
    outcomes = {row["outcome"] for row in payload["builders"]}
    assert "timeout" in outcomes
    assert "spawn_failed" in outcomes
    assert "unknown" in outcomes
    failed = [r for r in payload["builders"] if r["outcome"] == "failed"]
    assert failed and failed[0]["duration_ns"] is not None


def test_invocation_ids_do_not_cross_pair(tmp_path: Path):
    we = load_events()
    a = we.new_invocation_id(None)
    b = we.new_invocation_id(None)
    assert a != b
    records = [
        we.base_record(invocation_id=a, kind="spawned", role="builder", pid=1),
        we.base_record(
            invocation_id=b, kind="exited", role="builder", pid=2, returncode=0
        ),
        we.base_record(
            invocation_id=a, kind="exited", role="builder", pid=1, returncode=0
        ),
    ]
    for rec in records:
        we.emit(tmp_path / "e.jsonl", rec)
    paired = we.pair_builder_runs(we.read_records(tmp_path / "e.jsonl"))
    by = {p["invocation_id"]: p for p in paired}
    assert by[a]["outcome"] == "ok"
    assert by[b]["outcome"] == "unknown"


def test_malformed_records_and_io_failure_do_not_break_worker(
    tmp_path: Path, monkeypatch, capsys
):
    trioctl = load_trioctl()
    we = load_events()
    agent = _fake_agent(tmp_path / "agent", "print('OK')")
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(agent))
    events = tmp_path / "events.jsonl"
    events.write_text("{not json\nincomplete")
    # IO failure: events_path is a directory.
    out = trioctl.run_cursor_worker(
        "builder",
        profile(),
        prompt=SECRET,
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
        events=_ctx(tmp_path),
    )
    assert out == "OK"
    err = capsys.readouterr().err
    assert we.TELEMETRY_WARN in err
    assert SECRET not in err
    rows = we.read_records(events)
    assert rows == []


def test_scout_and_docs_ignored_in_builder_report(tmp_path: Path):
    we = load_events()
    path = tmp_path / "e.jsonl"
    inv = we.new_invocation_id(None)
    we.emit(path, we.base_record(invocation_id=inv, kind="spawned", role="scout"))
    we.emit(
        path,
        we.base_record(
            invocation_id=inv, kind="exited", role="scout", returncode=0
        ),
    )
    payload = we.report(path)
    assert payload["builders"] == []


def test_cli_report_and_mailbox_constraint(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    inside = mailbox / "worker-events.jsonl"
    we = load_events()
    inv = we.new_invocation_id("11111111-1111-4111-8111-111111111111")
    we.emit(
        inside,
        we.base_record(
            invocation_id=inv, kind="spawned", role="builder", pid=9
        ),
    )
    we.emit(
        inside,
        we.base_record(
            invocation_id=inv,
            kind="exited",
            role="builder",
            pid=9,
            returncode=0,
            duration_ns=1000,
        ),
    )
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "worker-events-report",
            "--events-file",
            "worker-events.jsonl",
            "--mailbox",
            str(mailbox),
            "--run-id",
            "missing",
        ]
    )
    assert args.func(args) == 0
    outside = tmp_path / "other.jsonl"
    assert we.resolve_events_path(str(outside), mailbox) is None

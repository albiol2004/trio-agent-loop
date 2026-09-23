"""Offline tests for opt-in cursor worker lifecycle events."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
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
        "invocation_id": we.new_invocation_id(),
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
    dumped = ""
    shard = we.shard_dir(events)
    if shard.is_dir():
        for item in shard.iterdir():
            dumped += item.read_text(encoding="utf-8", errors="replace")
    assert SECRET not in dumped
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
    # Sequential intervals are disjoint: omit them, do not list 0.
    assert payload["overlaps"] == []


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
            invocation_id=we.new_invocation_id(),
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
    a = we.new_invocation_id()
    b = we.new_invocation_id()
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
    import os

    trioctl = load_trioctl()
    we = load_events()
    agent = _fake_agent(tmp_path / "agent", "print('OK')")
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(agent))
    events = tmp_path / "events.jsonl"
    events.write_text("{not json\nincomplete")
    os.mkfifo(str(events) + ".d")
    out = trioctl.run_cursor_worker(
        "builder",
        profile(),
        prompt=SECRET,
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
        events=_ctx(events),
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
    inv = we.new_invocation_id()
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
    inv = we.new_invocation_id()
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


def test_emit_drops_nonblocking_when_locked(tmp_path: Path, capsys):
    import os
    import subprocess
    import sys
    import time

    we = load_events()
    path = tmp_path / "e.jsonl"
    inv = we.new_invocation_id()
    shard = we.shard_dir(path)
    shard.mkdir()
    target = shard / f"{inv}.jsonl"
    target.write_bytes(b"")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time\n"
                "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_APPEND)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "sys.stdout.write('held\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n"
            ),
            str(target),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        rec = we.base_record(
            invocation_id=inv,
            kind="spawned",
            role="builder",
            pid=1,
        )
        started = time.monotonic()
        we.emit(path, rec)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0
        assert we.TELEMETRY_WARN in capsys.readouterr().err
        assert we.read_records(path) == []
    finally:
        holder.kill()
        holder.wait(timeout=2)


def test_emit_fifo_does_not_block(tmp_path: Path, capsys):
    import os
    import time

    we = load_events()
    path = tmp_path / "events.jsonl"
    inv = we.new_invocation_id()
    dest = we.shard_dir(path)
    dest.mkdir()
    os.mkfifo(dest / f"{inv}.jsonl")
    rec = we.base_record(
        invocation_id=inv,
        kind="spawned",
        role="builder",
        pid=1,
    )
    started = time.monotonic()
    we.emit(path, rec)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert we.TELEMETRY_WARN in capsys.readouterr().err
    named = tmp_path / "named.fifo"
    os.mkfifo(named)
    started = time.monotonic()
    we.emit(
        named,
        we.base_record(
            invocation_id=we.new_invocation_id(),
            kind="spawned",
            role="builder",
            pid=2,
        ),
    )
    assert time.monotonic() - started < 1.0
    # Named FIFO is not opened; shards sit beside it.
    payload = we.report(named)
    assert isinstance(payload["builders"], list)


def test_report_survives_malformed_utf8_and_types(tmp_path: Path):
    we = load_events()
    path = tmp_path / "e.jsonl"
    good = we.base_record(
        invocation_id=we.new_invocation_id(),
        kind="spawned",
        role="builder",
        pid=1,
    )
    path.write_bytes(
        b"\xff\xfe not utf8\n"
        + b"[1,2,3]\n"
        + b"true\n"
        + b'{"schema":"trio.cursor_worker.v1","source":"cursor_worker",'
        + b'"invocation_id":123,"kind":"spawned","role":"builder"}\n'
        + (
            '{"schema":"trio.cursor_worker.v1","source":"cursor_worker",'
            '"invocation_id":"x","kind":"spawned","role":"builder",'
            '"monotonic_ns":"nope"}\n'
        ).encode()
        + (json.dumps(good) + "\n").encode()
    )
    payload = we.report(path)
    assert isinstance(payload["builders"], list)
    assert payload["caution"]


def test_invocation_ids_always_unique():
    we = load_events()
    ids = {we.new_invocation_id() for _ in range(32)}
    assert len(ids) == 32


def test_clock_domain_includes_boot():
    we = load_events()
    domain = we.clock_domain()
    assert "boot=" in domain
    assert we.boot_id() in domain
    assert we.boot_id() != ""


def test_overlap_requires_same_boot_domain():
    we = load_events()
    shared = {
        "start_monotonic_ns": 10,
        "end_monotonic_ns": 30,
        "duration_ns": 20,
        "clock_domain": "host=a;boot=1;wall=utc;elapsed=monotonic_ns",
    }
    other = dict(shared)
    other["clock_domain"] = "host=a;boot=2;wall=utc;elapsed=monotonic_ns"
    assert we.overlap_ns(shared, shared) == 20
    assert we.overlap_ns(shared, other) is None
    later = dict(shared)
    later["start_monotonic_ns"] = 30
    later["end_monotonic_ns"] = 40
    assert we.overlap_ns(shared, later) is None


def test_report_discards_zero_and_none_overlaps():
    we = load_events()
    domain = "host=a;boot=1;wall=utc;elapsed=monotonic_ns"
    first = {
        "invocation_id": "a",
        "outcome": "ok",
        "duration_ns": 10,
        "start_monotonic_ns": 0,
        "end_monotonic_ns": 10,
        "clock_domain": domain,
    }
    second = {
        "invocation_id": "b",
        "outcome": "ok",
        "duration_ns": 10,
        "start_monotonic_ns": 10,
        "end_monotonic_ns": 20,
        "clock_domain": domain,
    }
    third = {
        "invocation_id": "c",
        "outcome": "ok",
        "duration_ns": 5,
        "start_monotonic_ns": 2,
        "end_monotonic_ns": 7,
        "clock_domain": domain,
    }
    nan_row = {
        "invocation_id": "d",
        "outcome": "ok",
        "duration_ns": None,
        "start_monotonic_ns": 0,
        "end_monotonic_ns": 10,
        "clock_domain": domain,
    }
    builders = [first, second, third, nan_row]
    overlaps = []
    for i, left in enumerate(builders):
        for right in builders[i + 1 :]:
            ns = we.overlap_ns(left, right)
            if ns is None or ns <= 0:
                continue
            overlaps.append(ns)
    assert overlaps == [5]


def test_timeout_uses_child_process_not_sigalrm(tmp_path: Path, monkeypatch):
    import inspect
    import signal

    trioctl = load_trioctl()
    src = inspect.getsource(trioctl.run_cursor_worker)
    assert "signal.alarm" not in src
    assert "SIGALRM" not in src
    assert "TimeoutExpired" in src
    we = load_events()
    sleepy = _fake_agent(
        tmp_path / "slow",
        "import time\ntime.sleep(5)\nprint('OK')",
    )
    monkeypatch.setattr(trioctl.shutil, "which", lambda c: str(sleepy))
    events = tmp_path / "events.jsonl"
    previous = signal.getsignal(signal.SIGALRM)
    try:
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
        assert signal.getsignal(signal.SIGALRM) == previous
    finally:
        signal.signal(signal.SIGALRM, previous)
    payload = we.report(events)
    assert any(b["outcome"] == "timeout" for b in payload["builders"])

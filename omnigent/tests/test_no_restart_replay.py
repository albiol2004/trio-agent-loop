"""No replay after a runner restart, over real HTTP.

The exact first-prompt receipt lands, the turn runs, then the runner
restarts: the session shows ``runner_id: null`` and the items view is
empty (a delayed forwarder or store rebuild), although the turn ran.
Empty items and an unbound runner never justify re-sending work that
was already submitted. Both client interfaces must end with one
POST /events, no DELETE, the original session held durably, and a
fresh driver that makes zero broker requests.

Ported from the independent evaluator reproducer for consumer 968e7e9
(``test_eval_driver_blip.py``); the prompt is the live2 Lead prompt.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pytest


HERE = Path(__file__).parent
REPO = HERE.parent.parent
SCRIPT = HERE.parent / "trioctl"
LIVE2 = HERE / "fixtures" / "live2_17c9642c_rows.json"


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


trioctl = _load(SCRIPT, "trioctl_no_restart_replay")
BrokerClient = trioctl.broker_http.BrokerClient
core = trioctl._load_trio_loop(REPO)

_ROWS = {
    int(item["position"]): item
    for item in json.loads(LIVE2.read_text(encoding="utf-8"))["items"]
}
P = "".join(part["text"] for part in _ROWS[4]["content"])


def user(text: str, i: int) -> dict[str, Any]:
    return {"id": f"u{i}", "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}


def assistant(i: int, text: str = "ok") -> dict[str, Any]:
    return {"id": f"a{i}", "type": "message", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}]}


class FakeBroker:
    """Scripted HTTP broker. ``view(t)`` returns (items, snapshot), where
    t is seconds since the first POST /events (None before any)."""

    def __init__(self, view: Callable[[float | None], tuple[list, dict]]):
        self.view = view
        self.events: list[dict] = []
        self.deletes: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.item_reads: list[list] = []
        self.first_event_at: float | None = None
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                broker.requests.append(("GET", self.path))
                parsed = urlparse(self.path)
                if parsed.path == "/v1/hosts":
                    return self._send(
                        200, {"hosts": [{"host_id": "h1", "status": "online"}]}
                    )
                t = (None if broker.first_event_at is None
                     else time.monotonic() - broker.first_event_at)
                items, snap = broker.view(t)
                if parsed.path.endswith("/items"):
                    query = parse_qs(parsed.query)
                    rows = list(items)
                    if (query.get("order") or ["asc"])[0] == "desc":
                        rows = rows[::-1]
                    rows = rows[:int((query.get("limit") or ["100"])[0])]
                    broker.item_reads.append(rows)
                    return self._send(200, {"data": rows})
                return self._send(200, {"id": "s1", **snap})

            def do_POST(self):
                broker.requests.append(("POST", self.path))
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size) or b"{}")
                path = urlparse(self.path).path
                if path == "/v1/sessions":
                    return self._send(
                        201, {"id": "s1", "status": "idle", "runner_id": "r1"}
                    )
                if path.endswith("/events"):
                    broker.events.append(body)
                    if broker.first_event_at is None:
                        broker.first_event_at = time.monotonic()
                    return self._send(202, {"queued": True})
                return self._send(404, {})

            def do_DELETE(self):
                broker.requests.append(("DELETE", self.path))
                broker.deletes.append(self.path)
                return self._send(200, {})

            def do_PATCH(self):
                broker.requests.append(("PATCH", self.path))
                return self._send(200, {"id": "s1", "runner_id": "r1"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def event_texts(self) -> list[str]:
        return [e["data"]["content"][0]["text"] for e in self.events]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class WaitSessionClient(BrokerClient):
    """The real client plus the ``wait_session`` seam that
    OmnigentRunner._wait prefers: the turn runs, then the runner restarts."""

    def wait_session(self, session_id, timeout=None, interval=None):
        time.sleep(0.6)
        return self.get_session(session_id)


def restart_after_exact(recover_at: float | None):
    """Exact row lands while the turn runs; then the runner restarts and
    the items view is empty. Optionally the store recovers later."""
    def view(t):
        if t is None or t < 0.1:
            return [], {"status": "running", "runner_id": "r1"}
        if t < 0.5:
            return [user(P, 1)], {"status": "running", "runner_id": "r1"}
        if recover_at is None or t < recover_at:
            return [], {"status": "idle", "runner_id": None}
        return ([user(P, 1), assistant(2, "CANARY-LEAD-DONE")],
                {"status": "idle", "runner_id": "r1"})
    return view


@pytest.fixture(params=["attempts-unset", "attempts-2"])
def env(request, monkeypatch):
    monkeypatch.delenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", raising=False)
    if request.param == "attempts-2":
        monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", "2")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", "0.8")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", "0.05")
    monkeypatch.setenv("TRIO_OMNIGENT_IDLE_DWELL", "0")
    for key in ("TRIO_OMNIGENT_RUNNER_ID", "TRIO_MAILBOX_SESSION_IDS",
                "TRIO_OMNIGENT_HOST_ID"):
        monkeypatch.delenv(key, raising=False)
    return request.param


def _mailbox(tmp_path: Path) -> Path:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    return mailbox


def _runner(tmp_path, url, monkeypatch, client_cls=None, timeout=2.5):
    kwargs = {}
    if client_cls is not None:
        kwargs["broker_client"] = client_cls(url)
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, base_url=url, interval=0.05, timeout=timeout, **kwargs
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_prompt", lambda *a, **k: P)
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)
    return runner


@pytest.mark.parametrize(
    "client_cls", [None, WaitSessionClient], ids=["broker-client", "wait-seam"]
)
@pytest.mark.parametrize("recover_at", [None, 1.2], ids=["never", "late-1.2s"])
def test_restart_after_exact_receipt_holds_without_replay(
    env, tmp_path, monkeypatch, client_cls, recover_at
):
    mailbox = _mailbox(tmp_path)
    broker = FakeBroker(restart_after_exact(recover_at))
    try:
        runner = _runner(tmp_path, broker.url, monkeypatch, client_cls)
        with pytest.raises(trioctl.TrioctlError, match="held dispatch"):
            runner.run("lead", 1, mailbox)
        # The exact receipt was served before the restart, and an empty
        # items view after it.
        exact = [rows for rows in broker.item_reads if rows == [user(P, 1)]]
        assert exact, "exact receipt never observed"
        first_exact = broker.item_reads.index(exact[0])
        assert [] in broker.item_reads[first_exact + 1:]
        assert broker.event_texts() == [P], "submitted prompt was re-sent"
        assert broker.deletes == []
    finally:
        broker.close()

    assert runner.held_session_ids == ["s1"]
    held = list((mailbox / ".sessions").glob("held-*.json"))
    assert [p.name for p in held] == ["held-s1.json"]
    record = json.loads(held[0].read_text(encoding="utf-8"))
    assert record["hold"] == "role_completion_uncertain"
    assert record["session_id"] == "s1"
    assert "status: needs_human" in (mailbox / "STATE.md").read_text(
        encoding="utf-8"
    )

    fresh_broker = FakeBroker(
        lambda t: ([], {"status": "idle", "runner_id": "r1"})
    )
    try:
        fresh = _runner(tmp_path, fresh_broker.url, monkeypatch, client_cls)
        assert core.run_loop(mailbox, 3, fresh, repo=None) != 0
        with pytest.raises(trioctl.TrioctlError, match="Not dispatching"):
            fresh.run("lead", 1, mailbox)
        assert fresh_broker.requests == [], (
            f"fresh driver hit the broker: {fresh_broker.requests}"
        )
    finally:
        fresh_broker.close()

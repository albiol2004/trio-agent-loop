#!/usr/bin/env python3
"""GET /api/stream: SSE overview deltas pushed by a background scanner
(GOAL DoD3, server half). Every socket read is bounded by a deadline."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_stream", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class SSEClient:
    """Raw-socket SSE reader: parsed events, byte accounting, bounded reads."""

    def __init__(self, port: int, path: str = "/api/stream",
                 headers: dict | None = None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        extra = {"Host": f"127.0.0.1:{port}", "Accept": "text/event-stream"}
        extra.update(headers or {})
        lines = [f"GET {path} HTTP/1.1"]
        lines += [f"{k}: {v}" for k, v in extra.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.buffer = b""
        self.eof = False
        self.body_bytes = 0
        head = self._read_head()
        self.status = int(head.split(b" ", 2)[1])
        self.headers = {}
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.decode("latin-1").partition(":")
            self.headers[name.strip().lower()] = value.strip()

    def _recv(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.eof:
            return False
        self.sock.settimeout(remaining)
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            chunk = b""
        if not chunk:
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def _read_head(self) -> bytes:
        deadline = time.monotonic() + 10
        while b"\r\n\r\n" not in self.buffer:
            if not self._recv(deadline):
                raise AssertionError("no response head: %r" % self.buffer)
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        return head

    def read_body(self) -> bytes:
        """The whole (Content-Length) body of a non-stream response."""
        length = int(self.headers.get("content-length", "0"))
        deadline = time.monotonic() + 10
        while len(self.buffer) < length and self._recv(deadline):
            pass
        return self.buffer[:length]

    def next_event(self, timeout: float = 5.0) -> dict | None:
        """The next event block, or None on timeout / EOF."""
        deadline = time.monotonic() + timeout
        while b"\n\n" not in self.buffer:
            if not self._recv(deadline):
                return None
        raw, _, self.buffer = self.buffer.partition(b"\n\n")
        self.body_bytes += len(raw) + 2
        event = {"event": "message", "id": None, "data": None, "retry": None}
        for line in raw.decode("utf-8").split("\n"):
            name, _, value = line.partition(":")
            value = value.lstrip(" ")
            if name == "data":
                event["data"] = json.loads(value)
            elif name in ("event", "id", "retry"):
                event[name] = value
        return event

    def until(self, predicate, timeout: float = 5.0) -> dict | None:
        """First event matching ``predicate`` within ``timeout``."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event = self.next_event(remaining)
            if event is None:
                return None
            if predicate(event):
                return event

    def wait_eof(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.eof and time.monotonic() < deadline:
            self._recv(deadline)
            self.buffer = b""
        return self.eof

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class StreamBase(unittest.TestCase):
    interval = "1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(os.path.realpath(self._tmp.name))
        self.home = tmp / "home"
        self.home.mkdir()
        self.root = tmp / "work" / "repo"
        self.mailbox = self.root / "loop-x"
        self.mailbox.mkdir(parents=True)
        (self.mailbox / "GOAL.md").write_text(
            "# Mission: stream\n\nmission: stream\n", encoding="utf-8")
        self.write_state("blocked")
        (self.mailbox / "VERDICT.md").write_text(
            "VERDICT: NEEDS_HUMAN\n\n## Iteration 2\n", encoding="utf-8")
        env = patch.dict(os.environ, {
            "TRIO_DASH_INBOX_STATE": str(tmp / "inbox-state.json"),
            "TRIO_DASH_STATE_DIR": str(tmp / "dash-state"),
            "TRIO_NATIVE_RUNS_DIR": str(tmp / "native-runs"),
            "TRIO_DASH_SCAN_INTERVAL_S": self.interval,
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TRIO_DASH_STREAM", None)
        saved = (serve.HOME, serve.SESSIONS_ROOT, serve.BROKER_BASE_URL)
        serve.HOME = self.home
        serve.SESSIONS_ROOT = self.home / ".omp" / "agent" / "sessions"
        serve.BROKER_BASE_URL = ""
        serve._BROKER_LISTING.update(at=0.0, value=None)
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        self.addCleanup(self._restore, saved)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients: list[SSEClient] = []
        self.addCleanup(self._close_clients)

    def _restore(self, saved):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME, serve.SESSIONS_ROOT, serve.BROKER_BASE_URL = saved
        serve._BROKER_LISTING.update(at=0.0, value=None)
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])

    def _close_clients(self):
        for client in self.clients:
            client.close()

    def write_state(self, status: str):
        (self.mailbox / "STATE.md").write_text(
            f"iteration: 2\nstatus: {status}\nphase: eval-done\n",
            encoding="utf-8")

    def stream(self, path="/api/stream", headers=None) -> SSEClient:
        client = SSEClient(self.port, path, headers)
        self.clients.append(client)
        return client

    def open_stream(self, **kwargs):
        """Connect and consume ``retry`` + the snapshot."""
        client = self.stream(**kwargs)
        self.assertEqual(client.status, 200)
        retry = client.next_event()
        snapshot = client.next_event()
        return client, retry, snapshot

    def loop_id(self, snapshot) -> str:
        live_model = sys.modules["trio_dashboard_live_model"]
        workspace = snapshot["data"]["workspaces"][0]
        card = [c for c in workspace["loops"] if c["name"] == "loop-x"][0]
        return live_model.loop_entity_id(workspace["root"], card)


class StreamWireTests(StreamBase):
    def test_snapshot_first_then_only_small_ticks_when_idle(self):
        # accept 4
        client = self.stream()
        self.assertEqual(client.status, 200)
        self.assertEqual(client.headers["content-type"],
                         "text/event-stream; charset=utf-8")
        self.assertEqual(client.headers["cache-control"], "no-cache")
        retry = client.next_event()
        self.assertEqual(retry["retry"], "3000")
        self.assertIsNone(retry["data"])
        snapshot = client.next_event()
        self.assertEqual(snapshot["event"], "snapshot")
        data = snapshot["data"]
        self.assertEqual(snapshot["id"], f"{data['epoch']}:{data['seq']}")
        self.assertGreaterEqual(data["seq"], 1)
        names = [c["name"] for ws in data["workspaces"] for c in ws["loops"]]
        self.assertIn("loop-x", names)
        self.loop_id(snapshot)
        client.body_bytes = 0
        seen = []
        deadline = time.monotonic() + 3.0
        while True:
            event = client.next_event(max(0.05, deadline - time.monotonic()))
            if event is None or time.monotonic() >= deadline:
                if event is not None:
                    seen.append(event)
                break
            seen.append(event)
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual({e["event"] for e in seen}, {"tick"}, seen)
        for event in seen:
            self.assertEqual(set(event["data"]),
                             {"epoch", "seq", "updated_at"})
            self.assertEqual(event["data"]["epoch"], data["epoch"])
            self.assertEqual(event["data"]["seq"], data["seq"])
        self.assertLess(client.body_bytes, 2048)

    def test_state_change_pushes_a_loop_delta(self):
        # accept 5
        client, _retry, snapshot = self.open_stream()
        loop_id = self.loop_id(snapshot)
        started = time.monotonic()
        self.write_state("ready")
        delta = client.until(lambda e: e["event"] == "delta", timeout=6.0)
        self.assertIsNotNone(delta, "no delta within 6 s")
        self.assertLess(time.monotonic() - started, 5.5)
        batch = delta["data"]
        self.assertEqual(delta["id"], f"{batch['epoch']}:{batch['seq']}")
        self.assertEqual(batch["from_seq"], snapshot["data"]["seq"])
        self.assertEqual(batch["seq"], batch["from_seq"] + 1)
        ops = [(c["op"], c["kind"], c["id"]) for c in batch["changes"]]
        self.assertIn(("upsert", "loop", loop_id), ops)
        change = [c for c in batch["changes"]
                  if c["kind"] == "loop" and c["id"] == loop_id][0]
        self.assertEqual(change["data"]["card"]["status"], "ready")
        self.assertEqual(change["data"]["root"], str(self.root))

    def test_inbox_read_post_kicks_the_scanner(self):
        client, _retry, snapshot = self.open_stream()
        items = snapshot["data"]["workspaces"][0]["inbox"]
        self.assertTrue(items, "fixture should raise an inbox item")
        item = items[0]
        status, _ = _post_json(self.base + "/api/inbox/read", {
            "ids": [item["id"]], "root": str(self.root)})
        self.assertEqual(status, 200)
        delta = client.until(
            lambda e: e["event"] == "delta" and any(
                c["kind"] == "inbox" and c["id"] == item["id"]
                for c in e["data"]["changes"]), timeout=5.0)
        self.assertIsNotNone(delta)
        change = [c for c in delta["data"]["changes"]
                  if c["id"] == item["id"]][0]
        self.assertEqual(change["op"], "upsert")
        self.assertTrue(change["data"]["item"]["read"])

    def test_healthz_reports_stream_state_additively(self):
        status, health = _get_json(self.base + "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(health["stream_subscribers"], 0)
        self.assertTrue(health["ok"])
        self.open_stream()
        _status, health = _get_json(self.base + "/healthz")
        self.assertEqual(health["stream_subscribers"], 1)
        self.assertGreaterEqual(health["stream_seq"], 1)

    def test_overview_endpoint_still_serves_and_feeds_the_model(self):
        status, overview = _get_json(self.base + "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(set(overview), {
            "workspaces", "scanned", "worktrees_scanned", "scan_roots",
            "broker", "updated_at", "elapsed_ms"})
        client, _retry, snapshot = self.open_stream()
        self.assertEqual(snapshot["event"], "snapshot")
        self.assertEqual(snapshot["data"]["workspaces"][0]["root"],
                         overview["workspaces"][0]["root"])


class StreamResumeTests(StreamBase):
    def test_reconnect_replays_missed_delta_without_snapshot(self):
        # accept 6
        first, _retry, snapshot = self.open_stream()
        epoch, seq = snapshot["data"]["epoch"], snapshot["data"]["seq"]
        self.write_state("ready")
        delta = first.until(lambda e: e["event"] == "delta", timeout=6.0)
        self.assertIsNotNone(delta)
        self.assertEqual(delta["data"]["seq"], seq + 1)
        first.close()
        again = self.stream(headers={"Last-Event-ID": f"{epoch}:{seq}"})
        self.assertEqual(again.status, 200)
        self.assertEqual(again.next_event()["retry"], "3000")
        replay = again.next_event()
        self.assertEqual(replay["event"], "delta")
        self.assertEqual(replay["data"], delta["data"])
        self.assertEqual(replay["id"], delta["id"])
        for _ in range(3):
            event = again.next_event(1.2)
            if event is None:
                break
            self.assertNotEqual(event["event"], "snapshot")

    def test_foreign_epoch_or_future_seq_gets_a_snapshot(self):
        # accept 6
        for last_id in ("deadbeefdeadbeef:1", "garbage", "x:999999"):
            client = self.stream(headers={"Last-Event-ID": last_id})
            client.next_event()
            event = client.next_event()
            self.assertEqual(event["event"], "snapshot", last_id)
            self.assertIn("epoch", event["data"])
            client.close()

    def test_since_query_works_and_last_event_id_wins(self):
        first, _retry, snapshot = self.open_stream()
        epoch, seq = snapshot["data"]["epoch"], snapshot["data"]["seq"]
        self.write_state("ready")
        delta = first.until(lambda e: e["event"] == "delta", timeout=6.0)
        self.assertIsNotNone(delta)
        first.close()
        via_since = self.stream(path=f"/api/stream?since={epoch}:{seq}")
        via_since.next_event()
        self.assertEqual(via_since.next_event()["event"], "delta")
        # the header (current, nothing to replay) beats a stale ?since=
        both = self.stream(
            path=f"/api/stream?since={epoch}:{seq}",
            headers={"Last-Event-ID": f"{epoch}:{seq + 1}"})
        both.next_event()
        event = both.next_event()
        self.assertNotEqual(event["event"], "delta")
        self.assertNotEqual(event["event"], "snapshot")


class StreamLimitTests(StreamBase):
    def test_disabled_returns_503(self):
        # accept 7
        with patch.dict(os.environ, {"TRIO_DASH_STREAM": "0"}):
            status, body = _get_json(self.base + "/api/stream")
        self.assertEqual((status, body), (503, {"error": "stream disabled"}))
        self.assertEqual(self.server.stream_subscribers(), 0)

    def test_foreign_host_is_refused_like_every_get(self):
        client = self.stream(headers={"Host": "evil.example"})
        self.assertEqual(client.status, 421)

    def test_seventeenth_stream_is_refused_and_count_returns_to_zero(self):
        # accept 7
        clients = []
        for _ in range(16):
            client = self.stream()
            self.assertEqual(client.status, 200)
            self.assertEqual(client.next_event()["retry"], "3000")
            self.assertEqual(client.next_event()["event"], "snapshot")
            clients.append(client)
        self.assertEqual(self.server.stream_subscribers(), 16)
        extra = self.stream()
        self.assertEqual(extra.status, 503)
        self.assertEqual(json.loads(extra.read_body()),
                         {"error": "too many streams"})
        for client in clients:
            client.close()
        deadline = time.monotonic() + 6.0
        while self.server.stream_subscribers() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.server.stream_subscribers(), 0)
        # a freed slot is reusable
        again = self.stream()
        self.assertEqual(again.status, 200)


class ScannerTests(StreamBase):
    def test_scanner_idles_without_subscribers_and_stops_on_disconnect(self):
        time.sleep(2.2)
        self.assertIsNone(self.server.live.last_scan_at)
        client, _retry, _snapshot = self.open_stream()
        self.assertIsNotNone(client.until(
            lambda e: e["event"] == "tick", timeout=4.0))
        client.close()
        deadline = time.monotonic() + 6.0
        while self.server.stream_subscribers() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.server.stream_subscribers(), 0)
        time.sleep(0.5)
        ticks = self.server.live.ticks
        time.sleep(2.5)
        self.assertEqual(self.server.live.ticks, ticks)

    def test_server_close_ends_open_streams_quickly(self):
        client, _retry, _snapshot = self.open_stream()
        self.server.shutdown()
        self.server.server_close()
        started = time.monotonic()
        self.assertTrue(client.wait_eof(4.0))
        self.assertLess(time.monotonic() - started, 3.0)


class ScanIntervalTests(unittest.TestCase):
    def test_interval_is_clamped_and_defaults(self):
        for raw, want in (("", 10.0), ("abc", 10.0), ("0", 1.0),
                          ("0.2", 1.0), ("5", 5.0), ("9999", 300.0)):
            with patch.dict(os.environ, {"TRIO_DASH_SCAN_INTERVAL_S": raw}):
                self.assertEqual(serve._scan_interval(), want, raw)


if __name__ == "__main__":
    unittest.main()

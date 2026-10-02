"""Owned running evidence (GOAL DoD2): heartbeat, native-runs, broker workspace.

Unit tests for dashboard/live_registry.py with an injected pid probe, and
serve-level tests over HTTP with fixture roots, HOMEs and a fake process
table (``serve.PROC_ROOT``). Nothing reads the real native-runs registry.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"
LIVE_PATH = REPO_ROOT / "dashboard" / "live_registry.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LR = _load("trio_live_registry_under_test", LIVE_PATH)
serve = _load("trio_dashboard_serve_live_registry", SERVE_PATH)

NOW = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.timezone.utc)


def _iso(delta_s: float = 0) -> str:
    return (NOW + dt.timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _beat(**over) -> dict:
    data = {"schema": 1, "writer": "trio-skill", "session_id": "s-1",
            "pid": None, "phase": "lead", "iteration": 3,
            "updated_at": _iso(-10), "ttl_s": 900, "done": False}
    data.update(over)
    return data


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Path(self.tmp.name)

    def write(self, data) -> Path:
        path = self.box / LR.HEARTBEAT_FILE
        path.write_text(data if isinstance(data, str) else json.dumps(data),
                        encoding="utf-8")
        return path

    def read(self, **kw):
        kw.setdefault("now", NOW)
        return LR.read_heartbeat(self.box, **kw)

    def test_constants(self):
        self.assertEqual(LR.HEARTBEAT_FILE, ".heartbeat.json")
        self.assertEqual(LR.HEARTBEAT_DEFAULT_TTL_S, 900)
        self.assertEqual(LR.HEARTBEAT_MAX_TTL_S, 14400)
        self.assertEqual(LR.HEARTBEAT_FUTURE_SKEW_S, 120)

    def test_absent_and_malformed_are_none(self):
        self.assertIsNone(self.read())
        for text in ("not json", "[1, 2]", "42", '"x"', ""):
            self.write(text)
            self.assertIsNone(self.read(), text)

    def test_fresh_is_live_with_fields(self):
        self.write(_beat())
        got = self.read()
        self.assertTrue(got["live"])
        self.assertEqual(got["reason"], "fresh")
        self.assertEqual(got["age_s"], 10.0)
        self.assertEqual((got["ttl_s"], got["phase"], got["iteration"],
                          got["session_id"], got["writer"], got["updated_at"]),
                         (900, "lead", 3, "s-1", "trio-skill", _iso(-10)))

    def test_stale_done_future_refused(self):
        cases = {
            "stale": _beat(updated_at=_iso(-1000), ttl_s=900),
            "done": _beat(done=True),
            "future": _beat(updated_at=_iso(3600)),
            "schema": _beat(schema=2),
            "no-timestamp": _beat(updated_at="yesterday"),
        }
        for reason, data in cases.items():
            self.write(data)
            got = self.read()
            self.assertFalse(got["live"], reason)
            self.assertEqual(got["reason"], reason)

    def test_small_future_skew_is_live(self):
        self.write(_beat(updated_at=_iso(100)))
        self.assertTrue(self.read()["live"])
        self.write(_beat(updated_at=_iso(130)))
        self.assertFalse(self.read()["live"])

    def test_dead_pid_refused_and_live_pid_accepted(self):
        self.write(_beat(pid=4242))
        got = self.read(pid_alive=lambda pid: False)
        self.assertFalse(got["live"])
        self.assertEqual(got["reason"], "pid-dead")
        seen = []
        got = self.read(pid_alive=lambda pid: seen.append(pid) or True)
        self.assertTrue(got["live"])
        self.assertEqual(seen, [4242])

    def test_ttl_clamped(self):
        self.write(_beat(updated_at=_iso(-100), ttl_s=5))  # floor 60
        self.assertEqual(self.read()["ttl_s"], 60)
        self.assertFalse(self.read()["live"])
        self.write(_beat(updated_at=_iso(-100), ttl_s=10**9))  # cap 14400
        got = self.read()
        self.assertEqual(got["ttl_s"], LR.HEARTBEAT_MAX_TTL_S)
        self.assertTrue(got["live"])
        self.write(_beat(updated_at=_iso(-15000), ttl_s=10**9))
        self.assertFalse(self.read()["live"])
        data = _beat(updated_at=_iso(-1000))
        del data["ttl_s"]  # default 900
        self.write(data)
        got = self.read()
        self.assertEqual(got["ttl_s"], 900)
        self.assertFalse(got["live"])

    def test_symlink_is_never_followed(self):
        target = self.box / "real.json"
        target.write_text(json.dumps(_beat()), encoding="utf-8")
        (self.box / LR.HEARTBEAT_FILE).symlink_to(target)
        got = self.read()
        self.assertFalse(got["live"])
        self.assertEqual(got["reason"], "symlink")

    def test_oversized_file_is_bounded(self):
        self.write('{"schema": 1, "pad": "' + "x" * (200 * 1024) + '"}')
        self.assertIsNone(self.read())  # truncated read is not valid JSON


class NativeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Path(self.tmp.name).resolve()

    def entry(self, **over):
        data = {"mailbox": str(self.box), "lock": "held", "state": "running",
                "holder_pid": 77, "run_token": "tok", "registry_file": "/r/x.json"}
        data.update(over)
        return data

    def test_live_entry(self):
        got = LR.native_run_evidence([self.entry()], self.box,
                                     pid_alive=lambda pid: pid == 77)
        self.assertEqual(got, {"run_token": "tok", "holder_pid": 77,
                               "state": "running",
                               "registry_file": "/r/x.json"})

    def test_real_writer_shape_has_no_lock_key(self):
        # native/trio_native_step.py `begin` writes state "running" and
        # holder_pid with no lock key while the run is live.
        entry = self.entry()
        del entry["lock"]
        self.assertIsNotNone(LR.native_run_evidence(
            [entry], self.box, pid_alive=lambda pid: True))

    def test_refusals(self):
        alive = lambda pid: True  # noqa: E731
        for label, entries, probe in (
            ("dead pid", [self.entry()], lambda pid: False),
            ("lock released", [self.entry(lock="released")], alive),
            ("lock foreign", [self.entry(lock="foreign")], alive),
            ("other mailbox", [self.entry(mailbox="/somewhere/else")], alive),
            ("no pid", [self.entry(holder_pid=None)], alive),
            ("empty", [], alive),
        ):
            self.assertIsNone(LR.native_run_evidence(entries, self.box,
                                                     pid_alive=probe), label)
        for state in ("ended", "finished", "released", "error"):
            self.assertIsNone(LR.native_run_evidence(
                [self.entry(state=state)], self.box, pid_alive=alive), state)


class _SessionsBroker(BaseHTTPRequestHandler):
    sessions: list = []

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/sessions":
            body = json.dumps({"data": self.sessions, "has_more": False,
                               "last_id": None}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        return


class ServeRunningEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.proc = tempfile.TemporaryDirectory()
        self.runs = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.mailbox = self.root / "loop"
        self.mailbox.mkdir()
        (self.mailbox / "GOAL.md").write_text(
            "# Evidence loop\n\nmission: running evidence\n", encoding="utf-8")
        (self.mailbox / "STATE.md").write_text(
            "iteration: 1\nstatus: running\n", encoding="utf-8")
        (self.mailbox / "LOG.md").write_text("# LOG\n", encoding="utf-8")

        self.env = patch.dict(os.environ, {
            "TRIO_NATIVE_RUNS_DIR": self.runs.name,
            "TRIO_DASH_INBOX_STATE": str(Path(self.home.name) / "inbox.json")})
        self.env.start()
        self.saved = (serve.HOME, serve.PROC_ROOT, serve.BROKER_BASE_URL)
        serve.HOME = Path(self.home.name)
        serve.PROC_ROOT = Path(self.proc.name)
        serve.BROKER_BASE_URL = ""
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        serve._BROKER_LISTING.update(at=0.0, value=None)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.broker = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.broker is not None:
            self.broker.shutdown()
            self.broker.server_close()
        serve.HOME, serve.PROC_ROOT, serve.BROKER_BASE_URL = self.saved
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        serve._BROKER_LISTING.update(at=0.0, value=None)
        self.env.stop()
        for tmp in (self.proc, self.workspace, self.home, self.runs):
            tmp.cleanup()

    def card(self) -> dict:
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        serve._BROKER_LISTING.update(at=0.0, value=None)
        query = urllib.parse.urlencode({"root": str(self.root)})
        with urllib.request.urlopen(f"{self.base}/api/board?{query}") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return next(c for c in data["loops"] if c["name"] == "loop")

    def fake_process(self, pid: int, argv: bytes = b"python3\0worker\0"):
        process = Path(self.proc.name) / str(pid)
        process.mkdir(exist_ok=True)
        (process / "cmdline").write_bytes(argv)

    def when(self, delta_s: float) -> str:
        stamp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_s)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def beat(self, **over):
        data = _beat(updated_at=self.when(0))
        data.update(over)
        (self.mailbox / ".heartbeat.json").write_text(json.dumps(data),
                                                      encoding="utf-8")

    def start_broker(self, sessions: list):
        _SessionsBroker.sessions = sessions
        self.broker = ThreadingHTTPServer(("127.0.0.1", 0), _SessionsBroker)
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()
        serve.BROKER_BASE_URL = f"http://127.0.0.1:{self.broker.server_address[1]}"

    # accept 1
    def test_argv_names_mailbox_is_hint_and_blocks_start(self):
        self.fake_process(51001, b"python3\0--mailbox\0"
                          + str(self.mailbox).encode() + b"\0")
        card = self.card()
        self.assertFalse(card["running"])
        self.assertEqual(card["running_sources"], [])
        self.assertEqual(card["running_hints"], ["proc"])
        self.assertIsNone(card["heartbeat"])
        start = card["controls"]["start"]
        self.assertFalse(start["enabled"])
        self.assertIn("process", start["reason"])
        self.assertIn("argv", start["reason"])

    def test_no_argv_no_hints(self):
        card = self.card()
        self.assertEqual(card["running_hints"], [])

    # accept 2
    def test_fresh_heartbeat_marks_running(self):
        self.beat()
        card = self.card()
        self.assertTrue(card["running"])
        self.assertEqual(card["running_sources"], ["heartbeat"])
        self.assertTrue(card["heartbeat"]["live"])
        self.assertEqual(card["heartbeat"]["phase"], "lead")
        self.assertFalse(card["controls"]["start"]["enabled"])

    # accept 3
    def test_heartbeat_refusals_are_not_running(self):
        self.fake_process(51002)
        cases = {
            "stale": dict(updated_at=self.when(-1000), ttl_s=900),
            "done": dict(done=True),
            "future": dict(updated_at=self.when(3600)),
            "dead pid": dict(pid=51999),
        }
        for label, over in cases.items():
            self.beat(**over)
            card = self.card()
            self.assertFalse(card["running"], label)
            self.assertEqual(card["running_sources"], [], label)
            self.assertFalse(card["heartbeat"]["live"], label)
        self.beat(pid=51002)  # control: the same file with a live pid
        self.assertTrue(self.card()["running"])

    def test_symlinked_heartbeat_is_not_running(self):
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps(_beat(updated_at=self.when(-5))),
                          encoding="utf-8")
        (self.mailbox / ".heartbeat.json").symlink_to(target)
        card = self.card()
        self.assertFalse(card["running"])
        self.assertEqual(card["running_sources"], [])
        # The board refuses a mailbox holding a symlink before reading it.
        self.assertEqual(card["refused"], "mailbox contains symlinks")

    # accept 4
    def write_registry(self, **over):
        record = {"schema": 1, "driver": "claude-workflow",
                  "mailbox": str(self.mailbox), "repo": str(self.root),
                  "run_token": "tok-1", "lock": "held", "state": "running",
                  "holder_pid": 51003, "updated_at": "2026-10-02T12:00:00Z"}
        record.update(over)
        record = {k: v for k, v in record.items() if v is not None}
        Path(self.runs.name, "rec.json").write_text(json.dumps(record),
                                                    encoding="utf-8")

    def test_native_registry_entry_is_running_evidence(self):
        self.fake_process(51003)
        self.write_registry()
        card = self.card()
        self.assertTrue(card["running"])
        self.assertIn("native", card["running_sources"])

    def test_native_registry_without_lock_key_as_real_writer(self):
        self.fake_process(51003)
        self.write_registry(lock=None)  # `begin` writes no lock key
        self.assertIn("native", self.card()["running_sources"])

    def test_native_registry_refusals(self):
        self.fake_process(51003)
        for label, over in (
            ("dead holder", dict(holder_pid=51998)),
            ("lock released", dict(lock="released")),
            ("ended", dict(state="ended", lock="released")),
        ):
            self.write_registry(**over)
            card = self.card()
            self.assertNotIn("native", card["running_sources"], label)
            self.assertFalse(card["running"], label)

    # accept 5
    def test_broker_workspace_equal_to_mailbox_counts_under_any_title(self):
        self.start_broker([{"id": "b1", "status": "running",
                            "title": "unrelated chat",
                            "workspace": str(self.mailbox)}])
        card = self.card()
        self.assertIn("broker", card["running_sources"])
        self.assertTrue(card["running"])

    def test_broker_root_workspace_with_unrelated_title_does_not_count(self):
        self.start_broker([{"id": "b2", "status": "running",
                            "title": "unrelated chat",
                            "workspace": str(self.root)}])
        card = self.card()
        self.assertNotIn("broker", card["running_sources"])
        self.assertFalse(card["running"])

    def test_broker_root_workspace_with_trioctl_title_still_counts(self):
        self.start_broker([{"id": "b3", "status": "running",
                            "title": "trioctl loop lead:1",
                            "workspace": str(self.root)}])
        self.assertIn("broker", self.card()["running_sources"])

    def test_heartbeat_is_a_runtime_file(self):
        self.assertIn(".heartbeat.json", serve._RUNTIME_FILES)


if __name__ == "__main__":
    unittest.main()

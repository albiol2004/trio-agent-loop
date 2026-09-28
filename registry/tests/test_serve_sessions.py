"""Mailbox session exports: /api/sessions listing and /api/transcript tailing.

Omnigent-driven loops (trioctl) export every role session to
``<loop>/.sessions/<epoch>-<slug>-<id>.jsonl``. These tests pin that the
dashboard lists them (they were invisible when only ~/.omp was scanned),
streams them, and never lets the transcript endpoint read anything that is
not such an export inside one of the request root's own loops.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_sessions", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _sse_events(url: str, want: int, timeout: float = 5.0) -> list[tuple[str, dict]]:
    """Read up to `want` SSE events (or until the server closes the stream)."""
    parsed = urllib.parse.urlparse(url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Accept: text/event-stream\r\n\r\n".encode("ascii"))
        buf = b""
        events: list[tuple[str, dict]] = []
        while len(events) < want:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf and len(events) < want:
                block, buf = buf.split(b"\n\n", 1)
                name, data = None, None
                for line in block.decode("utf-8").splitlines():
                    if line.startswith("event: "):
                        name = line[len("event: "):]
                    elif line.startswith("data: "):
                        data = json.loads(line[len("data: "):])
                if name:
                    events.append((name, data))
        return events
    finally:
        sock.close()


HEADER = {
    "agent_id": "a1", "agent_name": "trio-omnigent-evaluator", "archived": False,
    "created_at": 1789651642, "id": "52ffbecb6a864ee3a4717db2c551335d",
    "status": "idle", "title": "trioctl loop-demo evaluator:iteration 1",
}
RECORDS = [
    HEADER,
    {"type": "resource_event", "event_type": "session.resource.created",
     "resource_type": "terminal", "resource": {"name": "cursor:main"},
     "created_at": 1789651643},
    {"type": "message", "role": "user", "created_at": 1789651650,
     "content": [{"type": "input_text", "text": "You are the Trio Evaluator."}]},
    {"type": "message", "role": "assistant", "created_at": 1789651706,
     "content": [{"type": "output_text", "text": "VERDICT: SHIP"}]},
]


class MailboxSessionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.other = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        root = Path(self.workspace.name)
        self.root = root
        self.loop = root / "loop-demo"
        (self.loop / ".sessions").mkdir(parents=True)
        (self.loop / "GOAL.md").write_text("# Mission: demo\n", encoding="utf-8")
        (self.loop / "STATE.md").write_text("status: ready\n", encoding="utf-8")
        self.newer = self.loop / ".sessions" / (
            "1789651642-trioctl-loop-demo-evaluator-iteration-1-52ffbecb.jsonl")
        self.newer.write_text(
            "".join(json.dumps(r) + "\n" for r in RECORDS), encoding="utf-8")
        self.older = self.loop / ".sessions" / (
            "1789643056-trioctl-loop-demo-lead-iteration-1-262f76e4.jsonl")
        self.older.write_text(
            json.dumps(dict(HEADER, id="262f76e4", created_at=1789643056,
                            agent_name="trio-omnigent-lead",
                            title="trioctl loop-demo lead:iteration 1")) + "\n",
            encoding="utf-8")
        # A header-less export still lists (label from the file name).
        self.bare = self.loop / ".sessions" / "1789600000-bare-abcd1234.jsonl"
        self.bare.write_text("", encoding="utf-8")
        # Not exports: dotfile, non-jsonl, and a symlink escaping .sessions.
        (self.loop / ".sessions" / ".partial.jsonl").write_text("{}\n", encoding="utf-8")
        (self.loop / ".sessions" / "notes.txt").write_text("x\n", encoding="utf-8")
        self.secret = Path(self.other.name) / "secret.jsonl"
        self.secret.write_text('{"type": "message"}\n', encoding="utf-8")
        os.symlink(self.secret, self.loop / ".sessions" / "escape.jsonl")
        (self.loop / "stray.jsonl").write_text("{}\n", encoding="utf-8")

        self.original_home = serve.HOME
        self.original_sessions_root = serve.SESSIONS_ROOT
        serve.HOME = Path(self.home.name)
        serve.SESSIONS_ROOT = Path(self.home.name) / ".omp" / "agent" / "sessions"
        # A second registered workspace with a loop of its own.
        self.second = tempfile.TemporaryDirectory()
        self.second_root = Path(self.second.name)
        (self.second_root / "loop").mkdir()
        (self.second_root / "loop" / "GOAL.md").write_text("# x\n", encoding="utf-8")
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[root, self.second_root],
            auto_discover=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        serve.HOME = self.original_home
        serve.SESSIONS_ROOT = self.original_sessions_root
        self.workspace.cleanup()
        self.other.cleanup()
        self.home.cleanup()
        self.second.cleanup()

    def _url(self, endpoint: str, root: Path | None = None, **query) -> str:
        query["root"] = str(root or self.root)
        return self.base + endpoint + "?" + urllib.parse.urlencode(query)

    def test_sessions_list_mailbox_exports_newest_first(self):
        status, sessions = _get(self._url("/api/sessions", loop="loop-demo"))
        self.assertEqual(status, 200)
        self.assertEqual(
            [s["path"] for s in sessions],
            [str(self.newer.resolve()), str(self.older.resolve()),
             str(self.bare.resolve())])
        first = sessions[0]
        self.assertEqual(first["label"], "evaluator:iteration 1")
        self.assertEqual(first["agent"], "trio-omnigent-evaluator")
        self.assertEqual(first["source"], "mailbox")
        self.assertEqual(first["kind"], "parent")
        self.assertEqual(first["timestamp"], "2026-09-17T13:27:22Z")
        self.assertEqual(first["id"], HEADER["id"])
        self.assertEqual(sessions[2]["label"], "1789600000-bare-abcd1234")

    def test_nested_loop_label_drops_its_short_name(self):
        nested = self.root / "loop" / "drafting"
        (nested / ".sessions").mkdir(parents=True)
        (nested / "GOAL.md").write_text("# x\n", encoding="utf-8")
        (nested / ".sessions" / "1790613177-trioctl-drafting-lead-1-8d0ca933.jsonl"
         ).write_text(json.dumps(dict(
             HEADER, title="trioctl drafting lead:iteration 1 lead-pass")) + "\n",
             encoding="utf-8")
        status, sessions = _get(self._url("/api/sessions", loop="loop/drafting"))
        self.assertEqual(status, 200)
        self.assertEqual([s["label"] for s in sessions],
                         ["lead:iteration 1 lead-pass"])

    def test_loop_detail_carries_the_sessions(self):
        status, detail = _get(self._url("/api/loop", name="loop-demo"))
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["sessions"]), 3)

    def test_transcript_streams_a_mailbox_export(self):
        events = _sse_events(
            self._url("/api/transcript", path=str(self.newer), offset=0), want=5)
        self.assertEqual(events[0][0], "init")
        self.assertEqual(events[0][1]["size"], self.newer.stat().st_size)
        records = [data["record"] for name, data in events[1:] if name == "line"]
        self.assertEqual(records, RECORDS)

    def test_transcript_refuses_paths_outside_the_roots_exports(self):
        cases = {
            "symlink out of .sessions": self.loop / ".sessions" / "escape.jsonl",
            "target of that symlink": self.secret,
            "jsonl beside the mailbox": self.loop / "stray.jsonl",
            "non-jsonl in .sessions": self.loop / ".sessions" / "notes.txt",
            "dotfile in .sessions": self.loop / ".sessions" / ".partial.jsonl",
            "mailbox file": self.loop / "GOAL.md",
            "traversal": Path(str(self.loop / ".sessions" / ".." / "GOAL.md")),
        }
        for label, path in cases.items():
            with self.subTest(label):
                events = _sse_events(
                    self._url("/api/transcript", path=str(path)), want=1)
                self.assertEqual(events, [("error", {"error": "invalid session path"})])

    def test_transcript_refuses_another_roots_export(self):
        # The export is real, but the request names a different workspace.
        status, _ = _get(self._url("/api/board", root=self.second_root))
        self.assertEqual(status, 200)
        events = _sse_events(
            self._url("/api/transcript", root=self.second_root,
                      path=str(self.newer)),
            want=1)
        self.assertEqual(events, [("error", {"error": "invalid session path"})])

if __name__ == "__main__":
    unittest.main()

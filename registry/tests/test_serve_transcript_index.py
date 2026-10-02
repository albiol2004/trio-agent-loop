"""Session-id transcript index wired into /api/sessions and /api/transcript.

A loop's sessions come from session ids recorded in its mailbox (and the
native-run registry / broker), not from the project slug; the transcript
endpoint streams only indexed files (plus the existing omp root and mailbox
exports). Everything runs against a fixture HOME; the real HOME is never read.
"""
from __future__ import annotations

import glob
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
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
        "trio_dashboard_serve_transcript_index", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()

SID = "c944a81b-27f4-465f-82d6-e3639ec84de7"
OTHER = "11111111-2222-3333-4444-555555555555"
BROKER_SID = "99999999-8888-7777-6666-555555555555"


def _get(url: str):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _sse_events(url: str, want: int, timeout: float = 5.0):
    parsed = urllib.parse.urlparse(url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Accept: text/event-stream\r\n\r\n".encode("ascii"))
        buf = b""
        events = []
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


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _jsonl(*records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


RECORDS = [
    {"type": "queue-operation", "timestamp": "2026-10-02T08:47:14.363Z"},
    {"type": "user", "timestamp": "2026-10-02T08:47:15.000Z"},
]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.home = self.tmp / "home"
        self.profile = self.home / ".profiles" / "p" / "claude"
        self.profile.mkdir(parents=True)
        os.symlink(self.profile, self.home / ".claude")
        self.slug = "-work-repo"
        self.projects = self.profile / "projects" / self.slug
        self.root = self.tmp / "work" / "repo"
        self.loop = self.root / "loop-x"
        _write(self.loop / "GOAL.md", "# Mission: x\n")
        _write(self.loop / "STATE.md", "status: ready\n")

        self._saved = (serve.HOME, serve.SESSIONS_ROOT, serve.BROKER_BASE_URL)
        serve.HOME = self.home
        serve.SESSIONS_ROOT = self.home / ".omp" / "agent" / "sessions"
        serve.BROKER_BASE_URL = ""
        serve._BROKER_LISTING.update(at=0.0, value=None)
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        self.addCleanup(self._restore)
        self.start_server()

    def _restore(self):
        self.server.shutdown()
        self.server.server_close()
        serve.HOME, serve.SESSIONS_ROOT, serve.BROKER_BASE_URL = self._saved
        serve._BROKER_LISTING.update(at=0.0, value=None)
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])

    def start_server(self):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, endpoint, **query):
        query.setdefault("root", str(self.root))
        return self.base + endpoint + "?" + urllib.parse.urlencode(query)

    def claude_session(self, sid=SID, agents=("a", "b"), workflow="wf_x"):
        parent = _write(self.projects / f"{sid}.jsonl", _jsonl(*RECORDS))
        subs = []
        base = self.projects / sid / "subagents"
        for name in agents:
            for sub_base in (base / "workflows" / workflow, base):
                path = _write(sub_base / f"agent-{name}.jsonl", _jsonl(*RECORDS))
                _write(sub_base / f"agent-{name}.meta.json", json.dumps(
                    {"agentType": "trio-builder", "description": f"builder {name}"}))
                subs.append(path)
        return parent, subs

    def launch(self, sid=SID):
        _write(self.loop / ".native-launch.json", json.dumps({
            "session_id": sid,
            "args": json.dumps({"mailbox": str(self.loop), "run_token": "ls-x"})}))

    def sessions(self, loop="loop-x"):
        status, body = _get(self.url("/api/sessions", loop=loop))
        self.assertEqual(status, 200, body)
        return body


class SessionListTests(Base):
    def test_native_launch_lists_claude_parent_and_every_subagent(self):
        parent, subs = self.claude_session()
        self.launch()
        rows = self.sessions()
        by_path = {r["path"]: r for r in rows}
        self.assertEqual(set(by_path),
                         {str(parent)} | {str(p) for p in subs})
        top = by_path[str(parent)]
        self.assertEqual((top["id"], top["kind"], top["harness"], top["status"]),
                         (SID, "parent", "claude", "ok"))
        self.assertEqual(rows[0]["path"], str(parent))  # parents first
        for sub in subs:
            row = by_path[str(sub)]
            self.assertEqual(row["kind"], "subagent")
            self.assertEqual(row["parent_id"], SID)
            self.assertEqual(row["parent_path"], str(parent))
            self.assertEqual(row["agent_type"], "trio-builder")
        workflow_rows = [r for r in rows if r.get("workflow") == "wf_x"]
        self.assertEqual(len(workflow_rows), 2)
        for key in ("id", "label", "timestamp", "path", "size", "kind",
                    "parent_id", "parent_path"):
            self.assertIn(key, top)

    def test_unreferenced_session_in_the_project_slug_is_not_listed(self):
        parent, _ = self.claude_session()
        self.launch()
        stray, _ = self.claude_session(sid=OTHER, agents=("z",))
        paths = {r["path"] for r in self.sessions()}
        self.assertIn(str(parent), paths)
        self.assertNotIn(str(stray), paths)
        self.assertFalse(any("z" in (r["id"] or "") and r["kind"] == "subagent"
                             for r in self.sessions()))

    def test_loop_without_references_lists_nothing_from_the_project(self):
        self.claude_session()
        self.assertEqual(self.sessions(), [])

    def test_project_root_omp_slug_is_not_a_fallback_but_own_slug_is(self):
        slugs = {self.root: "-root-slug", self.loop: "-loop-slug"}
        original = serve._session_slug
        serve._session_slug = lambda path: slugs.get(Path(path))
        self.addCleanup(setattr, serve, "_session_slug", original)
        name = "2026-10-02T08-00-00-000Z_abcd1234.jsonl"
        _write(serve.SESSIONS_ROOT / "-root-slug" / name,
               _jsonl({"type": "title"}, {"type": "session", "id": "root-one"}))
        self.assertEqual(serve._session_files_for_loop(self.loop, self.root), [])
        self.assertEqual(self.sessions(), [])
        _write(serve.SESSIONS_ROOT / "-loop-slug" / name,
               _jsonl({"type": "title"}, {"type": "session", "id": "loop-one"}))
        self.assertEqual([r["id"] for r in self.sessions()], ["loop-one"])

    def test_deleted_file_is_one_deleted_row_with_http_200(self):
        self.launch()
        rows = self.sessions()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["id"], row["status"], row["path"], row["size"]),
                         (SID, "deleted", None, None))
        self.assertIn("deleted by retention", row["label"])
        status, detail = _get(self.url("/api/loop", name="loop-x"))
        self.assertEqual(status, 200)
        self.assertEqual(detail["sessions"][0]["status"], "deleted")

    def test_omnigent_export_still_listed_and_streamable_beside_indexed(self):
        parent, _ = self.claude_session(agents=())
        self.launch()
        export = _write(
            self.loop / ".sessions" / "1789651642-trioctl-loop-x-lead-1-52ffbecb.jsonl",
            json.dumps({"id": "52ffbecb", "title": "trioctl loop-x lead:iteration 1",
                        "created_at": 1789651642}) + "\n")
        rows = self.sessions()
        self.assertEqual({r["path"] for r in rows},
                         {str(parent), str(export)})
        events = _sse_events(
            self.url("/api/transcript", path=str(export)), want=1)
        self.assertEqual(events[0][0], "init")

    def test_native_registry_entry_for_the_mailbox_references_its_session(self):
        parent, _ = self.claude_session(agents=())
        serve._NATIVE_REGISTRY.update(
            at=float("inf"), home=str(self.home),
            value=[{"mailbox": str(self.loop), "repo": str(self.root),
                    "session_id": SID}])
        rows = self.sessions()
        self.assertEqual([r["path"] for r in rows], [str(parent)])
        self.assertEqual(rows[0]["source"], "native-runs")


class TranscriptGuardTests(Base):
    def test_unindexed_file_under_projects_is_refused_indexed_streams(self):
        parent, subs = self.claude_session()
        stray, _ = self.claude_session(sid=OTHER, agents=())
        self.launch()
        self.sessions()  # builds the index
        events = _sse_events(self.url("/api/transcript", path=str(stray)), want=1)
        self.assertEqual(events, [("error", {"error": "invalid session path"})])
        events = _sse_events(self.url("/api/transcript", path=str(parent)), want=3)
        self.assertEqual(events[0][0], "init")
        self.assertEqual([d["record"] for n, d in events[1:] if n == "line"],
                         RECORDS)
        events = _sse_events(self.url("/api/transcript", path=str(subs[0])), want=1)
        self.assertEqual(events[0][0], "init")

    def test_indexed_file_streams_after_restart_without_listing_first(self):
        parent, subs = self.claude_session()
        self.launch()
        # Fresh server, empty index, no /api/sessions call yet.
        events = _sse_events(self.url("/api/transcript", path=str(subs[0])), want=1)
        self.assertEqual(events[0][0], "init")
        events = _sse_events(self.url("/api/transcript", path=str(parent)), want=1)
        self.assertEqual(events[0][0], "init")

    def test_unreferenced_file_is_refused_even_after_the_rebuild(self):
        self.claude_session()
        self.launch()
        stray, _ = self.claude_session(sid=OTHER, agents=())
        events = _sse_events(self.url("/api/transcript", path=str(stray)), want=1)
        self.assertEqual(events, [("error", {"error": "invalid session path"})])

    def test_other_claude_files_are_never_streamable(self):
        self.claude_session()
        self.launch()
        secret = _write(self.profile / "settings.json", "{}\n")
        history = _write(self.profile / "history.jsonl", _jsonl(*RECORDS))
        for path in (secret, history, self.home / ".claude" / "settings.json"):
            events = _sse_events(self.url("/api/transcript", path=str(path)), want=1)
            self.assertEqual(events, [("error", {"error": "invalid session path"})])


class _Broker(http.server.BaseHTTPRequestHandler):
    sessions: list = []

    def do_GET(self):
        body = json.dumps({"data": self.sessions, "has_more": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BrokerSessionTests(Base):
    def setUp(self):
        super().setUp()
        handler = type("H", (_Broker,), {})
        self.broker = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.handler = handler
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()
        self.addCleanup(self.broker.server_close)
        self.addCleanup(self.broker.shutdown)
        serve.BROKER_BASE_URL = f"http://127.0.0.1:{self.broker.server_address[1]}"
        serve._BROKER_LISTING.update(at=0.0, value=None)

    def test_listing_carries_non_archived_sessions_and_running_unchanged(self):
        self.handler.sessions = [
            {"id": "s1", "status": "running", "title": "t1", "workspace": "/w",
             "external_session_id": "e1", "labels": {"omnigent.wrapper": "claude"}},
            {"id": "s2", "status": "idle", "title": "t2", "workspace": "/w",
             "archived": True},
            {"id": "s3", "status": "idle", "title": "t3", "workspace": "/w"},
        ]
        listing = serve._broker_listing()
        self.assertEqual([s["id"] for s in listing["running"]], ["s1"])
        self.assertEqual([s["id"] for s in listing["sessions"]], ["s1", "s3"])
        self.assertEqual(listing["sessions"][0]["external_session_id"], "e1")
        self.assertEqual(listing["sessions"][0]["labels"],
                         {"omnigent.wrapper": "claude"})

    def test_broker_session_in_the_mailbox_workspace_is_listed(self):
        parent, _ = self.claude_session(sid=BROKER_SID, agents=())
        self.handler.sessions = [
            {"id": "b1", "status": "idle", "title": "anything",
             "workspace": str(self.loop), "external_session_id": BROKER_SID,
             "labels": {"omnigent.wrapper": "claude-code"}},
            {"id": "b2", "status": "idle", "title": "unrelated",
             "workspace": str(self.root), "external_session_id": OTHER,
             "labels": {"omnigent.wrapper": "claude-code"}},
            {"id": "b3", "status": "idle", "title": "unrelated",
             "workspace": str(self.tmp / "elsewhere"),
             "external_session_id": OTHER,
             "labels": {"omnigent.wrapper": "claude-code"}},
        ]
        rows = self.sessions()
        self.assertEqual([r["path"] for r in rows], [str(parent)])
        self.assertEqual(rows[0]["source"], "broker")

    def test_root_workspace_session_titled_for_this_mailbox_is_listed(self):
        parent, _ = self.claude_session(sid=BROKER_SID, agents=())
        self.handler.sessions = [
            {"id": "b1", "status": "idle", "title": "trioctl loop-x lead:1",
             "workspace": str(self.root), "external_session_id": BROKER_SID,
             "labels": {"omnigent.wrapper": "claude-code"}},
        ]
        self.assertEqual([r["path"] for r in self.sessions()], [str(parent)])


def _find_chromium():
    env = os.environ.get("TRIO_DASH_CHROMIUM")
    candidates = [env] if env else []
    # The headless shell exits once the DOM is dumped; full chrome can hang
    # on pages that keep network activity (EventSource) open.
    for pattern in (
            "~/.cache/ms-playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-linux*/chrome-headless-shell",
            "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"):
        candidates += sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
    candidates += [shutil.which(n) for n in
                   ("chromium-browser", "chromium", "google-chrome")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


# Resolved at import: other tests re-point $HOME while the suite runs.
CHROMIUM = _find_chromium()


class DeletedRowDomTests(Base):
    @unittest.skipUnless(CHROMIUM, "no headless chromium available")
    def test_drawer_shows_deleted_by_retention_without_console_errors(self):
        # Only a deleted reference: an openable session would auto-open an
        # SSE stream, which keeps headless virtual time from ever finishing.
        self.launch()
        rows = self.sessions()
        self.assertEqual([r["status"] for r in rows], ["deleted"])
        # chromium's singleton socket path must stay under ~108 bytes
        short = tempfile.gettempdir()
        if len(short) > 40 and os.path.isdir("/dev/shm"):
            short = "/dev/shm"
        profile = tempfile.mkdtemp(prefix="ti-dom-", dir=short)
        self.addCleanup(shutil.rmtree, profile, True)
        url = self.base + "/#" + urllib.parse.urlencode(
            {"root": str(self.root), "loop": "loop-x"})
        result = subprocess.run(
            [CHROMIUM, "--headless", "--disable-gpu", "--no-sandbox",
             "--disable-dev-shm-usage", f"--user-data-dir={profile}",
             "--dump-dom", "--virtual-time-budget=8000",
             "--enable-logging=stderr", url],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, TMPDIR=profile))
        dom = result.stdout
        self.assertIn('class="session-item session-deleted"', dom)
        self.assertIn("deleted by retention", dom)
        self.assertNotRegex(result.stderr, r"CONSOLE.*(Uncaught|TypeError)")


if __name__ == "__main__":
    unittest.main()

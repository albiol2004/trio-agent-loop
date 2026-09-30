#!/usr/bin/env python3
"""Adversarial tests for liveness, broker association, loop controls,
request guards, workspace discovery and derivation caching.

Everything runs offline: a fake broker on an ephemeral port, a fake /proc
tree, real short-lived child processes, and temporary workspaces.
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_hardening", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _request(method: str, url: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        finally:
            exc.close()


def _mailbox(root: Path, rel: str, state: str = "status: ready\n",
             goal: str = "# Mission: test\n") -> Path:
    mailbox = root / rel
    mailbox.mkdir(parents=True, exist_ok=True)
    (mailbox / "GOAL.md").write_text(goal, encoding="utf-8")
    (mailbox / "STATE.md").write_text(state, encoding="utf-8")
    return mailbox


# ------------------------------------------------------------ fake broker


class _FakeBroker:
    """Serves ``GET /v1/sessions`` in pages of 2 plus ``/v1/sessions/<id>``."""

    def __init__(self, sessions: list[dict]):
        self.sessions = sessions
        self.requests: list[str] = []
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                broker.requests.append(self.path)
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/v1/sessions":
                    after = (query.get("after") or [None])[0]
                    ids = [s["id"] for s in broker.sessions]
                    start = ids.index(after) + 1 if after in ids else 0
                    page = broker.sessions[start:start + 2]
                    payload = {
                        "object": "list", "data": page,
                        "last_id": page[-1]["id"] if page else None,
                        "has_more": start + 2 < len(broker.sessions),
                    }
                    code = 200
                else:
                    sid = parsed.path.rsplit("/", 1)[-1]
                    match = [s for s in broker.sessions if s["id"] == sid]
                    payload = match[0] if match else {"error": "not found"}
                    code = 200 if match else 404
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = [tempfile.TemporaryDirectory() for _ in range(2)]
        self.home = Path(self._tmp[0].name)
        self.root = Path(self._tmp[1].name).resolve()
        self.original_home = serve.HOME
        serve.HOME = self.home
        serve._BROKER_LISTING["value"] = None
        serve._HEAVY_CACHE.clear()
        with serve._LOOP_ACTIONS_LOCK:
            serve._LOOP_ACTIONS.clear()

    def tearDown(self):
        serve.HOME = self.original_home
        serve._BROKER_LISTING["value"] = None
        for tmp in self._tmp:
            tmp.cleanup()

    def start_server(self):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def board(self) -> dict:
        query = urllib.parse.urlencode({"root": str(self.root)})
        status, data = _request("GET", f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200, data)
        return data

    def card(self, name: str) -> dict:
        return next(l for l in self.board()["loops"] if l["name"] == name)


# ------------------------------------------------------------- broker


class BrokerAssociationTests(_Env):
    def setUp(self):
        super().setUp()
        self.mailbox = _mailbox(self.root, "loop/scenario-a",
                                state="status: running\n")
        _mailbox(self.root, "loop/scenario-b", state="status: running\n")
        other = self.root.parent / (self.root.name + "-other")
        self.other = other

    def run_with(self, sessions):
        broker = _FakeBroker(sessions)
        self.addCleanup(broker.close)
        with patch.object(serve, "BROKER_BASE_URL", broker.url):
            self.start_server()
            return broker, self.board()

    def test_running_session_matches_workspace_and_mailbox_across_pages(self):
        sessions = [
            {"id": "a1", "status": "idle", "title": "trioctl scenario-a lead:1",
             "workspace": str(self.root)},
            {"id": "a2", "status": "idle", "title": "unrelated"},
            {"id": "a3", "status": "idle", "title": "other"},
            {"id": "a4", "status": "running",
             "title": "trioctl scenario-a evaluator:iteration 10 eval",
             "workspace": str(self.root)},
        ]
        broker, data = self.run_with(sessions)
        self.assertGreaterEqual(
            sum(1 for r in broker.requests if r.startswith("/v1/sessions?")), 2)
        loops = {l["name"]: l for l in data["loops"]}
        self.assertIn("broker", loops["loop/scenario-a"]["running_sources"])
        self.assertEqual(loops["loop/scenario-a"]["broker_sessions"],
                         ["trioctl scenario-a evaluator:iteration 10 eval"])
        self.assertFalse(loops["loop/scenario-b"]["running"])
        self.assertEqual(data["broker"], "ok")
        # Known broker + nothing live -> a real "interrupted" call to act.
        b_items = [i for i in data["inbox"] if i["loop"] == "loop/scenario-b"]
        self.assertEqual([i["severity"] for i in b_items
                          if i["kind"] == "interrupted"], ["medium"])

    def test_same_title_in_another_workspace_does_not_match(self):
        sessions = [{"id": "x", "status": "running",
                     "title": "trioctl scenario-a lead:1",
                     "workspace": str(self.other)}]
        _, data = self.run_with(sessions)
        loops = {l["name"]: l for l in data["loops"]}
        self.assertFalse(loops["loop/scenario-a"]["running"])

    def test_title_without_workspace_does_not_match(self):
        sessions = [{"id": "x", "status": "running",
                     "title": "trioctl scenario-a lead:1"}]
        _, data = self.run_with(sessions)
        loops = {l["name"]: l for l in data["loops"]}
        self.assertFalse(loops["loop/scenario-a"]["running"])

    def test_ambiguous_mailbox_name_in_workspace_is_not_attributed(self):
        _mailbox(self.root, "loop-archive/scenario-a")
        sessions = [{"id": "x", "status": "running",
                     "title": "trioctl scenario-a lead:1",
                     "workspace": str(self.root)}]
        _, data = self.run_with(sessions)
        for loop in data["loops"]:
            self.assertNotIn("broker", loop["running_sources"], loop["name"])

    def test_title_prefix_needs_a_word_boundary(self):
        sessions = [{"id": "x", "status": "running",
                     "title": "trioctl scenario-abc lead:1",
                     "workspace": str(self.root)}]
        _, data = self.run_with(sessions)
        loops = {l["name"]: l for l in data["loops"]}
        self.assertFalse(loops["loop/scenario-a"]["running"])

    def test_broker_outage_is_unknown_not_stopped(self):
        with socket_closed_port() as port:
            with patch.object(serve, "BROKER_BASE_URL",
                              f"http://127.0.0.1:{port}"):
                self.start_server()
                data = self.board()
        self.assertEqual(data["broker"], "unreachable")
        items = [i for i in data["inbox"] if i["kind"] == "interrupted"]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["severity"], "low")
            self.assertIn("did not answer", item["detail"])


class socket_closed_port:
    def __enter__(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        return self.port

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------- process matching


class ProcMatchingTests(_Env):
    def setUp(self):
        super().setUp()
        self.proc = tempfile.TemporaryDirectory()
        self.addCleanup(self.proc.cleanup)
        self.original_proc = serve.PROC_ROOT
        serve.PROC_ROOT = Path(self.proc.name)
        self.addCleanup(setattr, serve, "PROC_ROOT", self.original_proc)
        self.container = _mailbox(self.root, "loop")
        self.child = _mailbox(self.root, "loop/hub-scenario")
        self.sibling = _mailbox(self.root, "loop-archive")
        self.next_pid = 40000

    def fake_process(self, *argv: str, cwd: Path | None = None):
        self.next_pid += 1
        d = Path(self.proc.name) / str(self.next_pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        if cwd is not None:
            os.symlink(cwd, d / "cwd")

    def matches(self, mailbox: Path) -> bool:
        return serve._proc_matches_mailbox(mailbox)

    def test_child_path_marks_child_not_container(self):
        self.fake_process("python3", "trioctl", "run", "scout", "--prompt-file",
                          str(self.child / "briefs" / "eval.md"))
        self.assertTrue(self.matches(self.child))
        self.assertFalse(self.matches(self.container))

    def test_file_directly_in_container_marks_container(self):
        self.fake_process("python3", "x.py", str(self.container / "PLAN.md"))
        self.assertTrue(self.matches(self.container))

    def test_prefix_without_path_boundary_does_not_match(self):
        self.fake_process("python3", "x.py", str(self.sibling / "PLAN.md"))
        self.assertFalse(self.matches(self.container))
        self.assertTrue(self.matches(self.sibling))

    def test_option_value_and_relative_path_resolve(self):
        self.fake_process("python3", "d.py", f"--mailbox={self.child}")
        self.assertTrue(self.matches(self.child))
        for p in list(Path(self.proc.name).iterdir()):
            for f in p.iterdir():
                f.unlink()
            p.rmdir()
        self.fake_process("python3", "d.py", "--mailbox", "loop-archive",
                          cwd=self.root)
        self.assertTrue(self.matches(self.sibling))
        self.assertFalse(self.matches(self.container))

    def test_subcommand_word_is_not_a_relative_path(self):
        # Live shape: `trioctl omnigent loop --mailbox loop/<child>` run from
        # the workspace root must not mark the `loop` container running.
        self.fake_process("python3", "/usr/bin/trioctl", "omnigent", "loop",
                          "--mailbox", "loop/hub-scenario", cwd=self.root)
        self.assertTrue(self.matches(self.child))
        self.assertFalse(self.matches(self.container))

    def test_bare_option_value_resolves_against_cwd(self):
        self.fake_process("python3", "d.py", "--mailbox", "loop", cwd=self.root)
        self.assertTrue(self.matches(self.container))

    def test_path_inside_a_shell_script_string_is_not_argv(self):
        self.fake_process("bash", "-c", f"cat {self.container}/PLAN.md")
        self.assertFalse(self.matches(self.container))


# ------------------------------------------------------------ pid liveness


class PidLivenessTests(unittest.TestCase):
    def test_zombie_is_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and serve._proc_state(child.pid) != "Z":
            time.sleep(0.02)
        self.assertEqual(serve._proc_state(child.pid), "Z")
        os.kill(child.pid, 0)  # the kernel still answers for a zombie
        self.assertFalse(serve._pid_is_live(child.pid))
        child.wait()

    def test_process_newer_than_its_record_is_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / ".driver.json"
            record.write_text("{}", encoding="utf-8")
            past = time.time() - 3600
            os.utime(record, (past, past))
            child = subprocess.Popen(["sleep", "5"])
            try:
                self.assertTrue(serve._pid_is_live(child.pid))
                self.assertFalse(serve._record_pid_live(child.pid, record))
                os.utime(record, None)
                self.assertTrue(serve._record_pid_live(child.pid, record))
            finally:
                child.kill()
                child.wait()

    def test_reused_pid_in_driver_sidecar_is_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = _mailbox(Path(tmp), "loop")
            child = subprocess.Popen(["sleep", "5"])
            try:
                sidecar = mailbox / ".driver.json"
                sidecar.write_text(json.dumps({"pid": child.pid}), "utf-8")
                past = time.time() - 3600
                os.utime(sidecar, (past, past))
                with patch.object(serve, "BROKER_BASE_URL", ""):
                    detection = serve._running_detection(mailbox, Path(tmp))
                self.assertNotIn("driver", detection["sources"])
            finally:
                child.kill()
                child.wait()


# --------------------------------------------------------------- controls


def _install_fake_release(home: Path) -> dict:
    """An installed release + trioctl under a temp HOME (dash-actions: Start
    runs the installed drivers, never the dashboard checkout's)."""
    share = home / ".local" / "share" / "trio-agent-loop"
    sha = "f" * 40
    entry = share / "releases" / sha / "metrics" / "trio_loop.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fake release driver\n", encoding="utf-8")
    (share / "CURRENT").write_text(sha + "\n", encoding="utf-8")
    trioctl = home / ".local" / "bin" / "trioctl"
    trioctl.parent.mkdir(parents=True)
    trioctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trioctl.chmod(0o755)
    return {"portable": entry, "omnigent": trioctl}


class ControlTests(_Env):
    def setUp(self):
        super().setUp()
        self.installed = _install_fake_release(self.home)
        self.mailbox = _mailbox(self.root, "loop")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.env = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stack = [
            patch.object(serve, "BROKER_BASE_URL", ""),
            patch.object(serve, "LAUNCH_GRACE_SECONDS", 0.5),
        ]
        for p in self.stack:
            p.start()
            self.addCleanup(p.stop)
        self.start_server()

    def stub(self, body: str):
        stub = self.bin / "python3"
        stub.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)

    def post(self, path, payload, headers=None):
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return _request("POST", self.base + path,
                        json.dumps(payload).encode(), hdrs)

    def test_capabilities_disable_start_for_nested_and_missing_driver(self):
        _mailbox(self.root, "loop-other")
        other = self.card("loop-other")["controls"]
        self.assertFalse(other["start"]["enabled"])
        self.assertIn("loop/ mailbox only", other["start"]["reason"])
        self.assertFalse(other["stop"]["enabled"])
        missing = {"portable": self.home / "nope.py",
                   "omnigent": self.home / "nope"}
        with patch.object(serve, "DRIVER_ENTRYPOINTS", missing):
            card = self.card("loop")["controls"]
            self.assertFalse(card["start"]["enabled"])
            self.assertIn("not installed", card["start"]["reason"])
            status, data = self.post("/api/loop/start",
                                     {"root": str(self.root),
                                      "driver": "portable"})
        self.assertEqual(status, 409, data)
        self.assertFalse((self.mailbox / ".driver.json").exists())

    def test_driver_that_exits_at_once_is_reported_and_leaves_no_state(self):
        self.stub("import sys; print('boom: no harness'); sys.exit(3)")
        cursor = {"pid": 4194000, "iteration": 4, "phase": "lead-done",
                  "session_ids": {"lead": "s-1"}, "driver": "portable"}
        (self.mailbox / ".driver.json").write_text(json.dumps(cursor))
        status, data = self.post("/api/loop/start",
                                 {"root": str(self.root), "driver": "portable"})
        self.assertEqual(status, 502, data)
        self.assertIn("code 3", data["error"])
        self.assertIn("boom: no harness", data["log_tail"])
        self.assertEqual(
            json.loads((self.mailbox / ".driver.json").read_text()), cursor)
        card = self.card("loop")
        self.assertFalse(card["running"])
        self.assertEqual(card["last_action"]["outcome"], "failed")
        self.assertIn("exited during startup", card["last_action"]["message"])

    def test_started_driver_keeps_resume_cursor_and_is_reaped(self):
        self.stub("import time; time.sleep(1.2)")
        cursor = {"pid": 4194000, "iteration": 4, "phase": "lead-done",
                  "session_ids": {"lead": "s-1"}, "driver": "portable"}
        (self.mailbox / ".driver.json").write_text(json.dumps(cursor))
        status, data = self.post("/api/loop/start",
                                 {"root": str(self.root), "driver": "portable"})
        self.assertEqual(status, 202, data)
        pid = data["pid"]
        state = json.loads((self.mailbox / ".driver.json").read_text())
        self.assertEqual(state["pid"], pid)
        self.assertEqual(state["iteration"], 4)
        self.assertEqual(state["session_ids"], {"lead": "s-1"})
        card = self.card("loop")
        self.assertIn("driver", card["running_sources"])
        self.assertTrue(card["controls"]["stop"]["enabled"])
        self.assertEqual(card["last_action"]["outcome"], "running")
        # A board built while the driver exits may still see it in that
        # request's process snapshot; the next poll must be consistent.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            card = self.card("loop")
            if (card["last_action"]["outcome"] != "running"
                    and not card["running"]):
                break
            time.sleep(0.1)
        self.assertEqual(card["last_action"]["outcome"], "finished")
        self.assertFalse(card["running"], card)
        self.assertIsNone(serve._proc_state(pid))  # reaped, not a zombie

    def test_stop_is_refused_for_a_foreign_live_pid(self):
        foreign = subprocess.Popen(["sleep", "5"])
        self.addCleanup(foreign.wait)
        self.addCleanup(foreign.kill)
        (self.mailbox / ".driver.json").write_text(
            json.dumps({"pid": foreign.pid, "driver": "portable"}))
        controls = self.card("loop")["controls"]
        self.assertFalse(controls["stop"]["enabled"])
        self.assertIn("not a loop driver", controls["stop"]["reason"])
        self.assertFalse(controls["start"]["enabled"])


# ----------------------------------------------------------- request guards


class RequestGuardTests(_Env):
    def setUp(self):
        super().setUp()
        _mailbox(self.root, "loop")
        self.start_server()
        self.url = f"{self.base}/api/inbox/read"
        self.body = json.dumps({"ids": [], "root": str(self.root)}).encode()

    def post(self, headers, body=None, method="POST", url=None):
        return _request(method, url or self.url,
                        self.body if body is None else body, headers)

    def test_same_origin_json_post_is_accepted(self):
        status, data = self.post({"Content-Type": "application/json",
                                  "Origin": self.base})
        self.assertEqual(status, 200, data)

    def test_no_origin_json_post_is_accepted(self):
        status, _ = self.post({"Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def test_cross_origin_text_plain_post_is_refused(self):
        status, data = self.post({"Content-Type": "text/plain",
                                  "Origin": "https://evil.example"})
        self.assertEqual(status, 403, data)

    def test_text_plain_body_is_refused_even_without_origin(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded",
                      "multipart/form-data; boundary=x"):
            with self.subTest(ctype=ctype):
                status, _ = self.post({"Content-Type": ctype})
                self.assertEqual(status, 415)

    def test_cross_origin_json_post_is_refused(self):
        status, _ = self.post({"Content-Type": "application/json",
                               "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.post({"Content-Type": "application/json",
                               "Origin": "null"})
        self.assertEqual(status, 403)

    def test_cross_site_fetch_metadata_is_refused(self):
        status, _ = self.post({"Content-Type": "application/json",
                               "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_cross_origin_delete_and_put_are_refused(self):
        for method in ("DELETE", "PUT"):
            with self.subTest(method=method):
                status, _ = _request(
                    method, f"{self.base}/api/registry/file?path=/x",
                    None, {"Origin": "https://evil.example"})
                self.assertEqual(status, 403)

    def test_rebinding_host_is_refused_on_reads(self):
        status, _ = _request("GET", f"{self.base}/api/overview", None,
                             {"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 421)
        status, _ = _request("GET", f"{self.base}/healthz", None,
                             {"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_tailnet_name_and_origin_are_allowed_when_configured(self):
        fqdn = "ws.tail0.ts.net"
        with patch.dict(os.environ, {
                "TRIO_DASH_ALLOWED_HOSTS": fqdn,
                "TRIO_DASH_ALLOWED_ORIGINS": f"https://{fqdn}:9470"}):
            status, _ = _request("GET", f"{self.base}/healthz", None,
                                 {"Host": f"{fqdn}:9470"})
            self.assertEqual(status, 200)
            # tailscale serve may forward either Host; the Origin allowlist
            # covers the loopback case.
            status, _ = self.post({"Content-Type": "application/json",
                                   "Origin": f"https://{fqdn}:9470"})
            self.assertEqual(status, 200)
            status, _ = self.post({"Content-Type": "application/json",
                                   "Host": f"{fqdn}:9470",
                                   "Origin": f"https://{fqdn}:9470"})
            self.assertEqual(status, 200)


# ------------------------------------------------------------- discovery


class DiscoveryTests(unittest.TestCase):
    def test_nested_workspace_found_worktrees_and_dotdirs_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            nested = base / "client" / "sim" / "repo"
            _mailbox(nested, "loop")
            worktree = base / "client" / "sim" / "wt-feature"
            _mailbox(worktree, "loop")
            (worktree / ".git").write_text("gitdir: /elsewhere\n")
            hidden = base / ".secrets" / "repo"
            _mailbox(hidden, "loop")
            deep = base / "a" / "b" / "c" / "d"
            _mailbox(deep, "loop")
            (base / "node_modules" / "pkg" / "loop").mkdir(parents=True)
            found = serve._discover_workspaces(base)
            self.assertIn(nested, found)
            self.assertIn(base / "client", found)  # direct child
            self.assertNotIn(worktree, found)
            self.assertFalse(any(".secrets" in str(p) for p in found))
            self.assertNotIn(deep, found)  # beyond TRIO_DASH_SCAN_DEPTH=3
            self.assertFalse(any("node_modules" in str(p) for p in found))
            with patch.dict(os.environ, {"TRIO_DASH_SCAN_DEPTH": "4"}):
                self.assertIn(deep, serve._discover_workspaces(base))

    def test_home_is_refused_as_a_scan_root(self):
        self.assertEqual(serve._discover_workspaces(serve.HOME), [])


# ------------------------------------------------------ derivation caching


class HeavyCacheTests(unittest.TestCase):
    def test_recomputes_only_when_mailbox_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = _mailbox(Path(tmp), "loop")
            calls = []

            def compute():
                calls.append(1)
                return {"n": len(calls)}

            first = serve._heavy("t", mailbox, None, compute)
            again = serve._heavy("t", mailbox, None, compute)
            self.assertEqual((first, again), ({"n": 1}, {"n": 1}))
            again["n"] = 99  # callers get copies
            (mailbox / "LOG.md").write_text("new entry\n")
            self.assertEqual(serve._heavy("t", mailbox, None, compute), {"n": 2})


if __name__ == "__main__":
    unittest.main()

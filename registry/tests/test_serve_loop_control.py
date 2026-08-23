#!/usr/bin/env python3
"""HTTP tests for dashboard loop start, stop, and status controls."""
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
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_loop_control", SERVE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def _http_json(
    method: str, url: str, payload: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(
                response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class DashboardLoopControlTestCase(unittest.TestCase):
    """Run loop controls against an isolated workspace and HOME."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.tmp_workspace = Path(self.workspace.name)
        self.mailbox = self.tmp_workspace / "loop"
        self.mailbox.mkdir()
        (self.mailbox / "GOAL.md").write_text(
            "# Test loop\n\nmission: exercise dashboard controls\n",
            encoding="utf-8",
        )
        (self.mailbox / "STATE.md").write_text(
            "iteration: 0\nstatus: ready\nphase: idle\n",
            encoding="utf-8",
        )
        self.bin_dir = Path(self.home.name) / "bin"
        self.bin_dir.mkdir()
        self.stub = self.bin_dir / "python3"
        self.stub.write_text(
            f"""#!{sys.executable}
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
mailbox = Path(args[args.index("--mailbox") + 1])
(mailbox / ".driver.json").write_text(
    json.dumps({{
        "pid": os.getpid(),
        "iteration": 0,
        "phase": "starting",
        "session_ids": {{}},
        "driver": "portable",
    }}) + "\\n",
    encoding="utf-8",
)
time.sleep(60)
""",
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self.env_patch = patch.dict(os.environ, {
            "HOME": self.home.name,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
        })
        self.env_patch.start()
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0),
            workspaces=[self.tmp_workspace],
            auto_discover=False,
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.started_pid = None

    def tearDown(self):
        if self.started_pid is not None:
            try:
                os.kill(self.started_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.started_pid, 0)
            except (ChildProcessError, OSError):
                pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME = self.original_home
        self.env_patch.stop()
        self.workspace.cleanup()
        self.home.cleanup()

    def url(self, path: str) -> str:
        query = urllib.parse.urlencode({"root": str(self.tmp_workspace)})
        return f"{self.base}{path}?{query}"

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return _http_json("POST", self.url(path), payload)

    def wait_for_status(self, pid: int) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, payload = _http_json("GET", self.url("/api/loop/status"))
            if status == 200 and payload.get("pid") == pid:
                return payload
            time.sleep(0.02)
        self.fail("driver state did not become available")

    def test_start_status_and_stop_portable_driver(self):
        status, payload = self.post(
            "/api/loop/start",
            {
                "root": str(self.tmp_workspace),
                "driver": "portable",
                "max_iterations": 1,
            },
        )
        self.assertEqual(status, 202, payload)
        self.started_pid = payload["pid"]
        self.assertEqual(payload["driver"], "portable")
        self.assertEqual(payload["mailbox"], str(self.mailbox))

        driver_status = self.wait_for_status(self.started_pid)
        self.assertTrue(driver_status["live"], driver_status)
        self.assertEqual(driver_status["phase"], "starting")
        self.assertEqual(driver_status["session_ids"], {})

        status, payload = self.post(
            "/api/loop/stop",
            {"root": str(self.tmp_workspace), "driver": "portable"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload, {
            "stopped": True,
            "pid": self.started_pid,
        })
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(self.started_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("stopped driver process is still live")
        self.started_pid = None

    def test_start_rejects_live_mailbox_lock(self):
        lock = self.mailbox / ".lock"
        lock.mkdir()
        (lock / "pid").write_text(
            f"{os.getpid()}\n", encoding="utf-8")

        status, payload = self.post(
            "/api/loop/start",
            {"root": str(self.tmp_workspace), "driver": "portable"},
        )
        self.assertEqual(status, 409, payload)

    def test_start_omnigent_builds_the_native_loop_command(self):
        process = Mock()
        process.pid = 987654321
        try:
            with patch.object(
                serve.subprocess, "Popen", return_value=process
            ) as popen:
                status, payload = self.post(
                    "/api/loop/start",
                    {
                        "root": str(self.tmp_workspace),
                        "driver": "omnigent",
                        "max_iterations": 3,
                    },
                )
            self.assertEqual(status, 202, payload)
            command = popen.call_args.args[0]
            self.assertEqual(command, [
                "python3", "omnigent/trioctl", "omnigent", "loop",
                "--mailbox", str(self.mailbox),
                "--max-iterations", "3",
            ])
            self.assertEqual(popen.call_args.kwargs["cwd"], self.tmp_workspace)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertIs(
                popen.call_args.kwargs["stdout"], serve.subprocess.DEVNULL)
            self.assertIs(
                popen.call_args.kwargs["stderr"], serve.subprocess.DEVNULL)
        finally:
            with serve._LOOP_PROCESSES_LOCK:
                serve._LOOP_PROCESSES.pop(process.pid, None)

    def test_board_includes_driver_state(self):
        (self.mailbox / ".driver.json").write_text(
            json.dumps({
                "pid": os.getpid(),
                "iteration": 2,
                "phase": "lead-running",
                "session_ids": {"lead": "session-1"},
                "driver": "portable",
            }) + "\n",
            encoding="utf-8",
        )
        status, payload = _http_json("GET", self.url("/api/board"))
        self.assertEqual(status, 200, payload)
        card = next(item for item in payload["loops"] if item["name"] == "loop")
        self.assertEqual(card["driver_phase"], "lead-running")
        self.assertEqual(card["driver"], "portable")
        self.assertTrue(card["running"])

    def test_stop_rejects_foreign_process(self):
        foreign = subprocess.Popen(["sleep", "1"])
        try:
            (self.mailbox / ".driver.json").write_text(
                json.dumps({
                    "pid": foreign.pid,
                    "iteration": 1,
                    "phase": "lead-running",
                    "session_ids": {},
                    "driver": "portable",
                }) + "\n",
                encoding="utf-8",
            )
            status, payload = self.post(
                "/api/loop/stop",
                {"root": str(self.tmp_workspace), "driver": "portable"},
            )
            self.assertEqual(status, 403, payload)
            self.assertIsNone(foreign.poll())
        finally:
            foreign.terminate()
            foreign.wait(timeout=5)

    def test_start_rejects_root_outside_workspace_seeds(self):
        outside = Path(tempfile.mkdtemp())
        try:
            status, payload = self.post(
                "/api/loop/start",
                {"root": str(outside), "driver": "portable"},
            )
            self.assertEqual(status, 403, payload)
        finally:
            outside.rmdir()


if __name__ == "__main__":
    unittest.main()

"""Real-browser proof of the dashboard's push stream (GOAL DoD3).

Starts a lab ``dashboard/serve.py`` (ephemeral loopback port, fixture workspace
with three mailboxes, a 1 s scanner) and drives headless chromium through
playwright-core (``stream_dom_harness.cjs``). The harness counts
MutationObserver records per section, SSE events by type, requests per path
and CDP ``Network.dataReceived`` bytes.

The whole class SKIPS, with the reason, when node, playwright-core or chromium
is missing: it never passes vacuously.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"
HARNESS = Path(__file__).resolve().parent / "stream_dom_harness.cjs"

LOOPS = ("loop-a", "loop-b", "loop-c", "loop-d")
BOARD_SECTIONS = ("kpis", "needs-list", "running-list", "loop-rows",
                  "notes-list", "workspace-filter", "tabs")
DRAWER_SECTIONS = ("drawer-strip", "commit-list", "slice-list",
                   "drawer-timeline", "drawer-inbox-list", "drawer-mission",
                   "drawer-badge")
IDLE_MS = 5000          # >= 4 scan cycles at TRIO_DASH_SCAN_INTERVAL_S=1
HARNESS_TIMEOUT_S = 90


def _find_playwright_core() -> str | None:
    env = os.environ.get("TRIO_DASH_PW_CORE")
    if env:
        return env if os.path.exists(env) else None
    node = shutil.which("node")
    if node:
        probe = subprocess.run(
            [node, "-e", "console.log(require.resolve('playwright-core'))"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30)
        resolved = probe.stdout.strip()
        if probe.returncode == 0 and resolved and os.path.exists(resolved):
            return resolved
    home = os.path.expanduser("~/personal")
    for pattern in ("*/node_modules/playwright-core",
                    "*/*/node_modules/playwright-core"):
        for found in sorted(glob.glob(os.path.join(home, pattern))):
            if os.path.isfile(os.path.join(found, "package.json")):
                return found
    return None


def _find_chromium() -> str | None:
    env = os.environ.get("TRIO_DASH_CHROMIUM")
    if env:
        return env if os.path.isfile(env) else None
    # Full chrome: Playwright drives it fine and it keeps long-lived SSE
    # requests open without the headless shell's dump-dom limitations.
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")),
        key=_revision_key, reverse=True)
    found += [shutil.which(n) for n in
              ("chromium-browser", "chromium", "google-chrome")]
    return next((c for c in found if c and os.path.isfile(c)), None)


def _revision_key(path: str):
    """Newest chromium-<rev> first: compare revisions numerically."""
    marker = "chromium-"
    tail = path.rsplit(marker, 1)[-1].split("/", 1)[0]
    return (int(tail) if tail.isdigit() else -1, path)


NODE = shutil.which("node")
PLAYWRIGHT_CORE = _find_playwright_core() if NODE else None
CHROMIUM = _find_chromium()


def _skip_reason() -> str | None:
    if not NODE:
        return "node is not installed"
    if not PLAYWRIGHT_CORE:
        return ("playwright-core not found (set TRIO_DASH_PW_CORE or install "
                "it where node can resolve it)")
    if not CHROMIUM:
        return "no chromium found (set TRIO_DASH_CHROMIUM)"
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _iso(delta_s: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(seconds=delta_s)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _backdate(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _write_mailbox(root: Path, name: str, goal: str, state: str,
                   log: str, verdict: str = "", plan: str = "") -> Path:
    mailbox = root / name
    mailbox.mkdir(parents=True)
    files = {"GOAL.md": goal, "STATE.md": state, "LOG.md": log}
    if verdict:
        files["VERDICT.md"] = verdict
    if plan:
        files["PLAN.md"] = plan
    for fname, text in files.items():
        (mailbox / fname).write_text(text, encoding="utf-8")
    return mailbox


def build_workspace(root: Path) -> None:
    """A git repo with four mailboxes (live / needs-human / shipped /
    stale-running).

    Every file is back-dated by ~2.5 h so relative times render as "2h ago":
    the text cannot change inside a test run (the hour boundary is 30 min
    away) and a re-render of it would be a real product bug.
    """
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True)

    a = _write_mailbox(
        root, "loop-a",
        "# Mission: keep the widget live\n\nmission: widget\n",
        "iteration: 2\nstatus: running\nmax_iterations: 5\n",
        "# LOG\n\n- 2026-10-02T10:00:00Z lead iteration 2 started\n",
        "VERDICT: ITERATE\n\n## Iteration 1\nnot yet\n",
        "# PLAN\n\n## Slice one\nwrites: a.py\n")
    # MAILBOX-SCHEMA.md "Session heartbeat": a live in-session loop.
    (a / ".heartbeat.json").write_text(json.dumps({
        "schema": 1, "writer": "trio-skill", "session_id": "", "pid": None,
        "phase": "lead", "iteration": 2, "updated_at": _iso(1800),
        "ttl_s": 14400, "done": False}) + "\n", encoding="utf-8")
    b = _write_mailbox(
        root, "loop-b",
        "# Mission: decide the schema\n\nmission: schema\n",
        "iteration: 3\nstatus: needs_human\nmax_iterations: 5\n",
        "# LOG\n\n- 2026-10-02T09:00:00Z evaluator needs a human\n",
        "VERDICT: NEEDS_HUMAN\n\n## Iteration 3\nWhich schema should win?\n",
        "# PLAN\n\n```yaml\nslices:\n  - id: schema-pick\n"
        "    writes: [b.py]\n    reads: []\n    status: in_progress\n"
        "    iteration: 3\n```\n")
    c = _write_mailbox(
        root, "loop-c",
        "# Mission: ship the parser\n\nmission: parser\n",
        "iteration: 1\nstatus: shipped\nmax_iterations: 5\n",
        "# LOG\n\n- 2026-10-02T08:00:00Z SHIP\n",
        "VERDICT: SHIP\n\n## Iteration 1\nall green\n",
        "# PLAN\n\n## Slice three\nwrites: c.py\n")
    # loop-d claims "running" with nothing live: a low-severity review note.
    d = _write_mailbox(
        root, "loop-d",
        "# Mission: tidy the docs\n\nmission: docs\n",
        "iteration: 1\nstatus: running\nmax_iterations: 5\n",
        "# LOG\n\n- 2026-10-02T07:00:00Z lead started\n")
    # A slice commit so the drawer's commit list is populated.
    (root / "b.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "b.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m",
                    "slice(schema-pick): add the schema stub"], check=True)
    for mailbox in (a, b, c, d):
        for entry in mailbox.iterdir():
            if entry.is_file() and entry.name != ".heartbeat.json":
                _backdate(entry, 9000)
        _backdate(mailbox, 9000)


class LabServer:
    """``python3 dashboard/serve.py`` on an ephemeral loopback port."""

    def __init__(self, tmp: Path, workspace: Path, *, stream: bool):
        self.tmp = tmp
        self.workspace = workspace
        self.stream = stream
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.log = tmp / "serve.log"

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update({
            "HOME": str(home),
            "TRIO_DASH_INBOX_STATE": str(self.tmp / "inbox-state.json"),
            "TRIO_DASH_STATE_DIR": str(self.tmp / "dash-state"),
            "TRIO_NATIVE_RUNS_DIR": str(self.tmp / "native-runs"),
            "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
            "TRIO_DASH_SCAN_INTERVAL_S": "1",
            "TRIO_BOARD_BROKER_URL": "",
            "TRIO_DASH_NATIVE_RUNS": "0",
            "TRIO_DASH_STREAM": "1" if self.stream else "0",
            "TRIO_DASH_SCAN_ROOTS": str(self.tmp / "no-scan-roots"),
        })
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, str(SERVE_PATH), "--host", "127.0.0.1",
                 "--port", str(self.port), "--workspace", str(self.workspace)],
                env=env, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(self.tmp))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("serve.py exited: " + self.log_text())
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("serve.py did not listen: " + self.log_text())

    def log_text(self) -> str:
        try:
            return self.log.read_text(errors="replace")[-2000:]
        except OSError:
            return ""

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class StreamDomTests(unittest.TestCase):
    """GOAL DoD3: the push stream rebuilds nothing that did not change."""

    @classmethod
    def setUpClass(cls):
        # chromium's singleton socket path must stay under ~108 bytes
        short = tempfile.gettempdir()
        if len(short) > 40 and os.path.isdir("/dev/shm"):
            short = "/dev/shm"
        cls.tmp = Path(tempfile.mkdtemp(prefix="dsd-", dir=short))
        cls.addClassCleanup(shutil.rmtree, str(cls.tmp), True)

    def _run_harness(self, tmp: Path, config: dict) -> dict:
        profile = tmp / "profile"
        profile.mkdir(exist_ok=True)
        config = dict(config, playwright=PLAYWRIGHT_CORE, chromium=CHROMIUM,
                      userDataDir=str(profile))
        cfg_path = tmp / "harness-config.json"
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(HARNESS), str(cfg_path)], capture_output=True,
            text=True, timeout=HARNESS_TIMEOUT_S, cwd=str(REPO_ROOT),
            env=dict(os.environ, TMPDIR=str(profile)))
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, "no harness output; stderr: "
                        + proc.stderr[-1500:])
        return json.loads(lines[-1])

    def _lab(self, name: str, *, stream: bool) -> tuple[LabServer, Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix=name + "-", dir=str(self.tmp)))
        workspace = tmp / "ws"
        workspace.mkdir()
        build_workspace(workspace)
        server = LabServer(tmp, workspace, stream=stream)
        self.addCleanup(server.stop)
        server.start()
        return server, workspace, tmp

    # -- push scenario -----------------------------------------------------

    def test_push_stream_rebuilds_nothing_that_did_not_change(self):
        server, workspace, tmp = self._lab("push", stream=True)
        root = str(workspace.resolve())
        mutate_path = workspace / "loop-c" / "STATE.md"
        result = self._run_harness(tmp, {
            "scenario": "push", "base": server.base, "root": root,
            "loops": list(LOOPS), "idleMs": IDLE_MS,
            "drawerLoop": "loop-b",
            "mutate": {
                "loop": "loop-c", "path": str(mutate_path),
                "text": "iteration: 1\nstatus: blocked\nmax_iterations: 5\n"},
        })
        ctx = json.dumps({k: v for k, v in result.items()
                          if k not in ("rows_text",)}, indent=1)[:3500]
        self.assertEqual(result["errors"], [], ctx)
        self.assertTrue(result["board_ready"], "board did not show every "
                        "fixture loop\n" + ctx + "\n" + result["rows_text"])
        self.assertTrue(result["snapshot_seen"], "no snapshot event\n" + ctx)

        # The measurement itself must be live: scan ticks arrived while idle.
        self.assertGreaterEqual(result["idle_events"].get("tick", 0), 2, ctx)
        self.assertEqual(result["sse_opened"], ["/api/stream"], ctx)
        self.assertGreater(result["idle_bytes"], 0,
                           "CDP saw no SSE bytes: the byte probe is dead\n"
                           + ctx)

        # Accept 1: every board section idle through >= 3 scan cycles.
        for section in BOARD_SECTIONS:
            self.assertIn(section, result["board"], ctx)
            self.assertFalse(result["board"][section]["missing"], section)
        board_records = {s: result["board"][s]["records"]
                         for s in BOARD_SECTIONS}
        self.assertEqual(board_records, {s: 0 for s in BOARD_SECTIONS},
                         json.dumps(result["board"], indent=1))
        # ... and none of them is idle only because it is empty.
        self.assertEqual(
            [s for s in BOARD_SECTIONS if not result["board"][s]["text_len"]],
            [], ctx)
        self.assertGreaterEqual(result["board_idle_ms"], 4000)

        # Accept 4: no polling, and the stream itself stays tiny.
        self.assertNotIn("/api/overview", result["idle_requests"], ctx)
        self.assertEqual(result["total_requests"].get("/api/overview", 0), 0,
                         ctx)
        self.assertLess(result["idle_bytes"], 4096, ctx)

        # Accept 3: a STATE.md rewrite shows in that loop's row within 3 s.
        self.assertIsNotNone(result["mutate_before"], ctx)
        self.assertIsNotNone(result["mutate_changed_ms"],
                             "row did not change\n" + ctx)
        self.assertLess(result["mutate_changed_ms"], 3000, ctx)
        self.assertNotEqual(result["mutate_after"], result["mutate_before"])
        self.assertIn("loop-c", result["mutate_after"])
        self.assertRegex(result["mutate_after"].lower(), r"block")
        # Control: the observers are live, the rewrite did register on them.
        self.assertGreater(result["mutate_records"]["loop-rows"]["records"], 0,
                           ctx)
        if os.environ.get("TRIO_DASH_DOM_DEBUG"):
            print(json.dumps(result, indent=1), file=sys.stderr)

        # Accept 2: the drawer is open and its sections are idle too.
        self.assertTrue(result["drawer_opened"], ctx)
        self.assertTrue(result["detail_loaded"], ctx)
        self.assertIn("Decide the schema", result["drawer_name"], ctx)
        self.assertGreaterEqual(result["drawer_idle_ms"], 4000)
        for section in DRAWER_SECTIONS:
            self.assertIn(section, result["drawer"], ctx)
            self.assertFalse(result["drawer"][section]["missing"], section)
        drawer_records = {s: result["drawer"][s]["records"]
                          for s in DRAWER_SECTIONS}
        self.assertEqual(drawer_records, {s: 0 for s in DRAWER_SECTIONS},
                         json.dumps(result["drawer"], indent=1))
        self.assertEqual(
            [s for s in DRAWER_SECTIONS if not result["drawer"][s]["text_len"]],
            [], ctx)
        self.assertEqual(
            {s: result["drawer_board"][s]["records"] for s in BOARD_SECTIONS},
            {s: 0 for s in BOARD_SECTIONS},
            json.dumps(result["drawer_board"], indent=1))
        self.assertGreaterEqual(result["drawer_idle_events"].get("tick", 0), 2,
                                ctx)
        self.assertTrue(result["drawer_visible"], ctx)
        self.assertEqual(result["hash_after"], result["hash_open"], ctx)
        self.assertIn("loop=loop-b", result["hash_after"], ctx)
        self.assertEqual(result["console"], [], ctx)

    # -- polling fallback --------------------------------------------------

    def test_stream_disabled_falls_back_to_polling_overview(self):
        server, workspace, tmp = self._lab("poll", stream=False)
        result = self._run_harness(tmp, {
            "scenario": "poll", "base": server.base,
            "root": str(workspace.resolve()), "loops": list(LOOPS),
            "pollWaitMs": 12000})
        ctx = json.dumps({k: v for k, v in result.items()
                          if k not in ("rows_text",)}, indent=1)[:2000]
        self.assertEqual(result["errors"], [], ctx)
        self.assertTrue(result["board_ready"], ctx + "\n" + result["rows_text"])
        for name in LOOPS:
            self.assertIn(name, result["rows_text"])
        self.assertGreaterEqual(result["overview_requests"], 2, ctx)
        self.assertGreaterEqual(result["elapsed_ms"], 11000, ctx)


if __name__ == "__main__":
    unittest.main()

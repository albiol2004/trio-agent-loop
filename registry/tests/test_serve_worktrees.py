#!/usr/bin/env python3
"""Linked-worktree ownership and loop-worker evidence, against real git
repositories and real processes.

Ownership is git ancestry: a worktree's mailbox is its own when it is new
since ``merge-base(HEAD, main)``, changed since then in a way main's tip
does not already have, uncommitted, holds untracked runtime files, or is
live. That must survive merge/rebase/reset/stash, count committed
deletions, ignore touched mtimes and already-merged branches, and keep
actionable loops visible when no common base exists. Also covers which
processes count as loop workers, and the adjacent control/guard fixes.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
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
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_worktrees", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env,
                          check=True, capture_output=True, text=True).stdout


def write_mailbox(path: Path, status: str, verdict: str = "VERDICT: SHIP\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "GOAL.md").write_text(f"# Mission: {path.name}\n", "utf-8")
    (path / "STATE.md").write_text(f"status: {status}\niteration: 2\n", "utf-8")
    (path / "VERDICT.md").write_text(verdict, "utf-8")
    (path / "LOG.md").write_text("## iter 1\n", "utf-8")


def later(path: Path, seconds: float = 60.0):
    """Make every file of a mailbox look written ``seconds`` from now."""
    stamp = time.time() + seconds
    for f in path.iterdir():
        if f.is_file():
            os.utime(f, (stamp, stamp))


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        finally:
            exc.close()


class WorktreeOwnershipTests(unittest.TestCase):
    """Branch ownership comes from git ancestry, never from file mtimes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        cls.scan = base / "clients"
        cls.main = cls.scan / "acme" / "product"
        cls.main.mkdir(parents=True)
        git(cls.main, "init", "-q", "-b", "main")
        (cls.main / ".gitignore").write_text(
            ".driver.json\n.session.json\n.lock/\n", "utf-8")
        write_mailbox(cls.main / "loop-alpha", "shipped")
        write_mailbox(cls.main / "loop-beta", "shipped")
        write_mailbox(cls.main / "loop" / "gamma", "shipped")
        (cls.main / "loop-alpha" / ".driver.json").write_text(
            json.dumps({"pid": 4194000, "phase": "shipped"}), "utf-8")
        git(cls.main, "add", "-A")
        git(cls.main, "add", "-f", "loop-alpha/.driver.json")
        (cls.main / "README.md").write_text("product\n", "utf-8")
        git(cls.main, "add", "-A")
        git(cls.main, "commit", "-q", "-m", "baseline mailboxes")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        for cache in (serve._HEAVY_CACHE, serve._WORKTREE_GIT_CACHE,
                      serve._WORKTREE_REFS_CACHE, serve._DISCOVER_CACHE):
            cache.clear()
        serve._BROKER_LISTING["value"] = None
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.addCleanup(setattr, serve, "HOME", self.original_home)
        patcher = patch.dict(os.environ, {
            "TRIO_DASH_SCAN_ROOTS": str(self.scan),
            "TRIO_DASH_SCAN_DEPTH": "3"})
        patcher.start()
        self.addCleanup(patcher.stop)

    _count = 0

    def worktree(self, prefix: str = "wt") -> Path:
        WorktreeOwnershipTests._count += 1
        name = f"{prefix}-{WorktreeOwnershipTests._count}"
        path = self.scan / "acme" / name
        git(self.main, "worktree", "add", "-q", "-b", name, str(path))
        self.addCleanup(git, self.main, "worktree", "remove", "--force",
                        str(path))
        return path

    def main_commit(self, rel: str, text: str, message: str):
        (self.main / rel).write_text(text, "utf-8")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", message)

    def selection(self, worktree: Path, listing=None) -> dict:
        for cache in (serve._WORKTREE_GIT_CACHE, serve._WORKTREE_REFS_CACHE,
                      serve._DISCOVER_CACHE):
            cache.clear()
        listing = listing or {"status": "disabled", "running": []}
        with serve._proc_snapshot(None, listing):
            got = serve._worktree_mailboxes(worktree, listing)
        return {Path(k).relative_to(worktree).as_posix(): v
                for k, v in got.items()}

    def needs_human(self, wt: Path):
        (wt / "loop-alpha" / "STATE.md").write_text(
            "status: needs_human\niteration: 3\n", "utf-8")

    # -- the review's reproduction ------------------------------------

    def test_needs_human_survives_merge_rebase_reset_and_stash(self):
        wt = self.worktree()
        self.assertEqual(self.selection(wt), {})
        self.needs_human(wt)
        self.assertEqual(self.selection(wt), {"loop-alpha": ["modified"]})
        git(wt, "commit", "-q", "-am", "loop: needs human")
        expected = {"loop-alpha": ["committed on branch"]}
        self.assertEqual(self.selection(wt), expected)

        self.main_commit("README.md", "product v2\n", "main: unrelated")
        git(wt, "merge", "-q", "--no-edit", "main")
        self.assertEqual(self.selection(wt), expected, "after merge main")

        self.main_commit("README.md", "product v3\n", "main: unrelated 2")
        git(wt, "rebase", "-q", "main")
        self.assertEqual(self.selection(wt), expected, "after rebase")

        git(wt, "reset", "-q", "--hard", "HEAD")
        self.assertEqual(self.selection(wt), expected, "after reset --hard")

        (wt / "loop-alpha" / "LOG.md").write_text("## iter 3\n", "utf-8")
        git(wt, "stash", "-q")
        self.assertEqual(self.selection(wt), expected, "while stashed")
        git(wt, "stash", "pop", "-q")
        self.assertEqual(self.selection(wt),
                         {"loop-alpha": ["committed on branch", "modified"]},
                         "after stash pop")

    def test_needs_human_reaches_needs_you_after_merge(self):
        wt = self.worktree()
        self.needs_human(wt)
        git(wt, "commit", "-q", "-am", "loop: needs human")
        self.main_commit("README.md", "product v9\n", "main: unrelated")
        git(wt, "merge", "-q", "--no-edit", "main")
        server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.main], auto_discover=True)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with patch.object(serve, "BROKER_BASE_URL", ""):
            status, data = _get(
                f"http://127.0.0.1:{server.server_address[1]}/api/overview")
        self.assertEqual(status, 200, data)
        entry = next(w for w in data["workspaces"]
                     if w["root"] == str(wt))
        self.assertEqual([l["name"] for l in entry["loops"]], ["loop-alpha"])
        self.assertEqual([(i["kind"], i["severity"]) for i in entry["inbox"]],
                         [("needs_human", "high")])

    # -- ancestry rules ---------------------------------------------------

    def test_committed_deletion_is_shown(self):
        wt = self.worktree()
        git(wt, "rm", "-q", "loop-beta/LOG.md")
        git(wt, "commit", "-q", "-m", "loop: drop log")
        self.assertEqual(self.selection(wt),
                         {"loop-beta": ["deleted on branch"]})

    def test_branch_already_merged_into_main_is_not_duplicated(self):
        wt = self.worktree()
        (wt / "loop-beta" / "STATE.md").write_text("status: running\n")
        git(wt, "commit", "-q", "-am", "loop: beta running")
        branch = wt.name
        git(self.main, "merge", "-q", "--no-edit", branch)
        self.assertEqual(self.selection(wt), {})

    def test_same_edit_already_on_main_is_not_duplicated(self):
        wt = self.worktree()
        (wt / "loop-beta" / "VERDICT.md").write_text("VERDICT: ITERATE\n")
        git(wt, "commit", "-q", "-am", "branch: iterate")
        self.main_commit("loop-beta/VERDICT.md", "VERDICT: ITERATE\n",
                         "main: same edit")
        self.assertEqual(self.selection(wt), {})

    def test_main_moving_ahead_does_not_flag_the_branch(self):
        # Seen live: main gained child mailboxes the branch lacks, which
        # made every branch's `loop/` container look changed.
        wt = self.worktree()
        write_mailbox(self.main / "loop" / "zeta", "running")
        (self.main / "loop" / "gamma" / "PLAN.md").write_text("plan\n")
        git(self.main, "add", "-A")
        git(self.main, "commit", "-q", "-m", "main: new child, new file")
        self.assertEqual(self.selection(wt), {})

    def test_touching_inherited_files_changes_nothing(self):
        wt = self.worktree()
        stamp = time.time() + 3600
        for f in wt.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                os.utime(f, (stamp, stamp))
        self.assertEqual(self.selection(wt), {})

    def test_new_mailboxes_committed_and_untracked(self):
        wt = self.worktree()
        write_mailbox(wt / "loop-delta", "running", "VERDICT: none\n")
        self.assertEqual(self.selection(wt), {"loop-delta": ["untracked"]})
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "loop: open delta")
        self.assertEqual(self.selection(wt),
                         {"loop-delta": ["new on branch"]})

    def test_nested_child_is_labelled_not_its_container(self):
        wt = self.worktree()
        (wt / "loop" / "gamma" / "STATE.md").write_text("status: running\n")
        git(wt, "commit", "-q", "-am", "gamma running")
        write_mailbox(wt / "loop" / "epsilon", "running")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "open epsilon")
        self.assertEqual(self.selection(wt), {
            "loop/gamma": ["committed on branch"],
            "loop/epsilon": ["new on branch"],
        })

    def test_unrelated_history_shows_actionable_loops_only(self):
        wt = self.worktree("orphan")
        git(wt, "checkout", "-q", "--orphan", wt.name + "-root")
        git(wt, "rm", "-rfq", ".")
        for leftover in wt.iterdir():  # ignored files survive `git rm`
            if leftover.name != ".git":
                subprocess.run(["rm", "-rf", str(leftover)], check=True)
        write_mailbox(wt / "loop-alpha", "needs_human", "VERDICT: none\n")
        write_mailbox(wt / "loop-beta", "shipped")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unrelated root")
        got = self.selection(wt)
        self.assertEqual(list(got), ["loop-alpha"], got)
        self.assertEqual(len(got["loop-alpha"]), 1, got)
        self.assertTrue(got["loop-alpha"][0].startswith("no common base ("),
                        got)
        self.assertIn("no common history", got["loop-alpha"][0])

    def test_worktree_of_a_bare_repository_uses_main_branch(self):
        bare = Path(self.tmp.name) / "bare.git"
        if not bare.exists():
            git(Path(self.tmp.name), "clone", "-q", "--bare",
                str(self.main), str(bare))
        wt = self.scan / "acme" / "bare-wt"
        git(bare, "worktree", "add", "-q", "-b", "bare-wt", str(wt), "main")
        self.addCleanup(git, bare, "worktree", "remove", "--force", str(wt))
        self.assertIsNone(serve._worktree_main(wt))
        self.assertEqual(self.selection(wt), {})
        self.needs_human(wt)
        git(wt, "commit", "-q", "-am", "needs human")
        self.assertEqual(self.selection(wt),
                         {"loop-alpha": ["committed on branch"]})

    # -- runtime and live evidence ---------------------------------------

    def test_ignored_live_sidecar_shown_tracked_sidecar_ignored(self):
        wt = self.worktree()
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        (wt / "loop-beta" / ".driver.json").write_text(
            json.dumps({"pid": os.getpid(), "phase": "lead"}), "utf-8")
        got = self.selection(wt)
        self.assertEqual(got, {"loop-beta": ["runtime files present", "live"]})

    def test_leftover_sidecar_counts_only_for_a_pending_loop(self):
        wt = self.worktree()
        box = wt / "loop" / "gamma"  # shipped on main, never merged into
        (box / ".session.json").write_text(
            json.dumps({"pid": 4194001, "done": True}), "utf-8")
        # A finished loop's leftovers are not pending work.
        self.assertEqual(self.selection(wt), {})
        state = box / "STATE.md"
        state.write_text("status: running\n", "utf-8")
        self.assertEqual(self.selection(wt), {
            "loop/gamma": ["modified", "runtime files present"]})
        git(wt, "commit", "-q", "-am", "gamma running")
        self.assertEqual(self.selection(wt), {
            "loop/gamma": ["committed on branch", "runtime files present"]})
        # Back to main's content, but the loop is left claiming "running"
        # only through its sidecar-bearing, unchanged mailbox: not shown
        # unless STATE on disk says so.
        git(wt, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.selection(wt), {})

    def test_broker_session_attributes_only_to_its_own_worktree(self):
        wt = self.worktree()
        other = self.worktree()
        listing = {"status": "ok", "running": [{
            "id": "s1", "title": "trioctl loop-alpha lead:iteration 1",
            "workspace": str(wt)}]}
        self.assertEqual(self.selection(wt, listing), {"loop-alpha": ["live"]})
        self.assertEqual(self.selection(other, listing), {})
        with serve._proc_snapshot(None, listing):
            main_detect = serve._running_detection(
                self.main / "loop-alpha", self.main)
        self.assertNotIn("broker", main_detect["sources"])

    def test_walk_separates_worktrees_and_they_are_not_registry_workspaces(self):
        wt = self.worktree()
        found, linked = serve._scan_root_walk(self.scan)
        self.assertIn(self.main, found)
        self.assertIn(wt, linked)
        self.assertNotIn(wt, found)
        server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.main], auto_discover=True)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, listed = _get(base + "/api/workspaces")
        self.assertNotIn(str(wt), {w["path"] for w in listed})
        status, detail = _get(base + "/api/loop?" + urllib.parse.urlencode(
            {"root": str(wt), "name": "loop-alpha"}))
        self.assertEqual(status, 200, detail)


class WorkerShapeTests(unittest.TestCase):
    """Only positively worker-shaped processes soften "nothing is live"."""

    INTERACTIVE = [
        ["claude", "--permission-mode", "auto", "--model", "claude-opus-5-5",
         "--mcp-config", '{"mcpServers":{}}'],
        ["codex", "app-server", "--listen", "stdio://"],
        ["codex", "-c", 'model_provider="openai"'],
        ["railway", "mcp"],
        ["/home/u/.local/share/uv/tools/omnigent/bin/python", "-I", "-m",
         "omnigent.harnesses.claude_native.bridge", "serve-mcp",
         "--bridge-dir", "/tmp/x"],
        ["python3", "/home/u/.local/bin/trioctl", "omnigent", "resolve",
         "builder", "--json"],
        ["node", "/usr/lib/node_modules/vite/bin/vite.js", "--port", "5299"],
        ["opendesign-mcp"],
    ]
    WORKERS = [
        ["python3", "/home/u/.local/bin/trioctl", "omnigent", "run", "builder",
         "--config", "/tmp/c.toml", "--workspace", "/w"],
        ["python3", "/home/u/.local/bin/trioctl", "omnigent", "loop",
         "--mailbox", "loop/x"],
        ["python3", "/repo/metrics/trio_loop.py", "run", "--mailbox", "/w/loop"],
        ["bash", "/repo/portable/driver.sh"],
        ["claude", "-p", "do the brief", "--output-format", "json"],
        ["codex", "-c", "x=1", "exec", "--json", "brief"],
        ["cursor-agent", "--print", "brief"],
        ["opencode", "run", "brief"],
    ]

    def test_classification(self):
        for args in self.INTERACTIVE:
            with self.subTest(args=args):
                self.assertFalse(serve._is_loop_worker(args))
        for args in self.WORKERS:
            with self.subTest(args=args):
                self.assertTrue(serve._is_loop_worker(args))

    def run_named(self, directory: Path, name: str, *args: str):
        script = directory / name
        script.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
        script.chmod(0o755)
        proc = subprocess.Popen([str(script), *args], cwd=directory)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def test_interactive_sessions_do_not_mask_a_stopped_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_mailbox(root / "loop", "running", "VERDICT: none\n")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            listing = {"status": "ok", "running": []}
            card = {"name": "loop", "status": "running",
                    "final_verdict": None, "last_activity": None}
            # Real processes shaped like an interactive session and an MCP.
            self.run_named(bin_dir, "claude", "--permission-mode", "auto")
            self.run_named(bin_dir, "railway", "mcp")
            time.sleep(0.3)
            with serve._proc_snapshot(None, listing):
                items = serve._inbox_items(root / "loop", card, root)
            self.assertEqual([(i["kind"], i["severity"]) for i in items],
                             [("interrupted", "medium")])
            # A headless worker in the same workspace softens it.
            self.run_named(bin_dir, "trioctl", "omnigent", "run", "builder")
            time.sleep(0.3)
            with serve._proc_snapshot(None, listing):
                items = serve._inbox_items(root / "loop", card, root)
            self.assertEqual([(i["kind"], i["severity"]) for i in items],
                             [("interrupted", "low")])
            self.assertIn("1 loop worker", items[0]["detail"])
            self.assertEqual(serve._workspace_worker_count(root), 1)


class AdjacentFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        write_mailbox(self.root / "loop", "ready", verdict="VERDICT: none\n")
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode())
            finally:
                exc.close()

    def test_controls_require_an_explicit_root(self):
        for route in ("/api/loop/stop", "/api/loop/start"):
            with self.subTest(route=route):
                status, data = self.post(route, {"driver": "portable"})
                self.assertEqual(status, 400)
                self.assertIn("root is required", data["error"])

    def test_start_is_disabled_while_broker_liveness_is_unknown(self):
        for state in ("unreachable", "truncated"):
            with self.subTest(state=state):
                listing = {"status": state, "running": []}
                with serve._proc_snapshot([], listing):
                    detection = serve._running_detection(
                        self.root / "loop", self.root)
                controls = serve._loop_controls(
                    self.root / "loop", self.root, detection, "portable")
                self.assertFalse(controls["start"]["enabled"])
                self.assertIn("broker-only run cannot be ruled out",
                              controls["start"]["reason"])
        with serve._proc_snapshot([], {"status": "ok", "running": []}):
            detection = serve._running_detection(self.root / "loop", self.root)
        self.assertTrue(serve._loop_controls(
            self.root / "loop", self.root, detection,
            "portable")["start"]["enabled"])

    def test_config_path_is_not_mailbox_evidence(self):
        other = self.root / "loop-other"
        write_mailbox(other, "running")
        processes = [(99999, [
            "python3", "/x/trioctl", "omnigent", "run", "scout",
            "--config", str(self.root / "loop" / "omnigent.toml"),
            "--prompt-file", str(other / "briefs" / "scout.md")], None)]
        with serve._proc_snapshot(processes, {"status": "disabled",
                                              "running": []}):
            self.assertFalse(serve._proc_matches_mailbox(self.root / "loop"))
            self.assertTrue(serve._proc_matches_mailbox(other))
        processes[0][1][5:7] = [f"--config={self.root / 'loop' / 'x.toml'}"]
        with serve._proc_snapshot(processes, {"status": "disabled",
                                              "running": []}):
            self.assertFalse(serve._proc_matches_mailbox(self.root / "loop"))

    def test_rejections_close_the_connection_explicitly(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/inbox/read", body="x",
                     headers={"Content-Type": "text/plain"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 415)
        self.assertEqual(resp.getheader("Connection"), "close")
        conn.close()

    def test_state_blocked_without_verdict_is_flagged(self):
        (self.root / "loop" / "STATE.md").write_text("status: blocked\n")
        status, data = _get(
            f"http://127.0.0.1:{self.port}/api/board?"
            + urllib.parse.urlencode({"root": str(self.root)}))
        kinds = {(i["kind"], i["severity"]) for i in data["inbox"]}
        self.assertIn(("blocked", "high"), kinds)


if __name__ == "__main__":
    unittest.main()

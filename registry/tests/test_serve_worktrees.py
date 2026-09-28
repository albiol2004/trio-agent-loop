#!/usr/bin/env python3
"""Linked-worktree discovery against real git repositories.

A main checkout commits a few mailboxes; linked worktrees then either leave
their inherited copies alone (hidden) or make a mailbox their own
(modified, untracked, worktree-only, committed on the branch, or live via a
gitignored sidecar). Merged-in changes from main are not the worktree's own
work. Also covers the adjacent control/guard fixes of the same pass.
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


class WorktreeDiscoveryTests(unittest.TestCase):
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
        # Some repos commit their sidecars; a checked-out copy of one is
        # not evidence that a loop ran in the worktree.
        (cls.main / "loop-beta" / ".repairs").write_text("0\n", "utf-8")
        git(cls.main, "add", "-A")
        (cls.main / "loop-alpha" / ".driver.json").write_text(
            json.dumps({"pid": 4194000, "phase": "shipped"}), "utf-8")
        git(cls.main, "add", "-f", "loop-alpha/.driver.json")
        git(cls.main, "commit", "-q", "-m", "baseline mailboxes")

        def add(name: str) -> Path:
            path = cls.scan / "acme" / name
            git(cls.main, "worktree", "add", "-q", "-b", name, str(path))
            return path

        cls.untouched = add("wt-untouched")
        cls.modified = add("wt-modified")
        (cls.modified / "loop-alpha" / "STATE.md").write_text(
            "status: needs_human\niteration: 3\n", "utf-8")
        cls.untracked = add("wt-untracked")
        write_mailbox(cls.untracked / "loop-delta", "running",
                      verdict="VERDICT: none\n")
        cls.committed = add("wt-committed")
        (cls.committed / "loop-beta" / "STATE.md").write_text(
            "status: running\niteration: 5\n", "utf-8")
        later(cls.committed / "loop-beta")
        git(cls.committed, "commit", "-q", "-am", "loop: iteration 5")
        cls.live = add("wt-live")
        (cls.live / "loop-beta" / ".driver.json").write_text(
            json.dumps({"pid": os.getpid(), "phase": "lead"}), "utf-8")
        # Main moves on; a worktree merges it. The merged files are git's
        # writes, not the worktree's own loop work.
        cls.merged = add("wt-merged")
        (cls.main / "loop-alpha" / "STATE.md").write_text(
            "status: SHIP\niteration: 9\n", "utf-8")
        git(cls.main, "commit", "-q", "-am", "main: alpha shipped")
        time.sleep(1.1)
        git(cls.merged, "merge", "-q", "--no-edit", "main")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        serve._HEAVY_CACHE.clear()
        serve._WORKTREE_GIT_CACHE.clear()
        serve._WORKTREE_SELECT_CACHE.clear()
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

    def selection(self, worktree: Path, listing=None) -> dict:
        listing = listing or {"status": "disabled", "running": []}
        with serve._proc_snapshot(None, listing):
            got = serve._worktree_mailboxes(worktree, listing)
        return {Path(k).relative_to(worktree).as_posix(): v
                for k, v in got.items()}

    def test_walk_separates_worktrees_from_workspaces(self):
        found, linked = serve._scan_root_walk(self.scan)
        self.assertIn(self.main, found)
        self.assertEqual(
            sorted(p.name for p in linked),
            ["wt-committed", "wt-live", "wt-merged", "wt-modified",
             "wt-untouched", "wt-untracked"])
        self.assertEqual(serve._worktree_main(self.modified), self.main)

    def test_untouched_copy_is_hidden(self):
        self.assertEqual(self.selection(self.untouched), {})

    def test_merged_changes_are_not_the_worktrees_work(self):
        self.assertEqual(self.selection(self.merged), {})

    def test_modified_copy_is_shown(self):
        self.assertEqual(self.selection(self.modified),
                         {"loop-alpha": ["modified"]})

    def test_new_mailbox_is_untracked_and_worktree_only(self):
        self.assertEqual(self.selection(self.untracked),
                         {"loop-delta": ["worktree-only", "untracked"]})

    def test_branch_commit_is_shown_though_git_status_is_clean(self):
        self.assertEqual(git(self.committed, "status", "--porcelain"), "")
        self.assertEqual(self.selection(self.committed),
                         {"loop-beta": ["committed on branch"]})

    def test_ignored_live_sidecar_on_a_clean_copy_is_shown(self):
        self.assertEqual(git(self.live, "status", "--porcelain"), "")
        got = self.selection(self.live)
        self.assertEqual(got["loop-beta"], ["runtime files present", "live"])
        # loop-alpha carries a committed (tracked) .driver.json: not evidence.
        self.assertNotIn("loop-alpha", got)

    def test_stale_ignored_sidecar_shows_loop_ran_but_not_live(self):
        sidecar = self.untouched / "loop-alpha" / ".session.json"
        sidecar.write_text(json.dumps({"pid": 4194001, "done": True}), "utf-8")
        try:
            self.assertEqual(self.selection(self.untouched),
                             {"loop-alpha": ["runtime files present"]})
        finally:
            sidecar.unlink()
            serve._WORKTREE_GIT_CACHE.clear()
            serve._WORKTREE_SELECT_CACHE.clear()

    def test_nested_child_change_does_not_label_its_container(self):
        state = self.untouched / "loop" / "gamma" / "STATE.md"
        original = state.stat()
        state.write_text("status: running\n", "utf-8")
        os.utime(state, (original.st_atime, original.st_mtime))
        try:
            got = self.selection(self.untouched)
            self.assertEqual(got, {"loop/gamma": ["modified"]})
        finally:
            git(self.untouched, "checkout", "--", "loop/gamma/STATE.md")
            # Keep the inherited copy's checkout-time mtime so later tests
            # still see an untouched worktree.
            os.utime(state, (original.st_atime, original.st_mtime))
            serve._WORKTREE_GIT_CACHE.clear()

    def test_broker_session_attributes_only_to_its_own_worktree(self):
        listing = {"status": "ok", "running": [{
            "id": "s1", "title": "trioctl loop-alpha lead:iteration 1",
            "workspace": str(self.untouched)}]}
        got = self.selection(self.untouched, listing)
        self.assertEqual(got, {"loop-alpha": ["live"]})
        with serve._proc_snapshot(None, listing):
            main_detect = serve._running_detection(
                self.main / "loop-alpha", self.main)
            other = serve._running_detection(
                self.modified / "loop-alpha", self.modified)
        self.assertNotIn("broker", main_detect["sources"])
        self.assertNotIn("broker", other["sources"])

    def test_overview_shows_worktree_loops_and_needs_human(self):
        server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.main], auto_discover=True)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with patch.object(serve, "BROKER_BASE_URL", ""):
            status, data = _get(base + "/api/overview")
        self.assertEqual(status, 200, data)
        entries = {w["name"]: w for w in data["workspaces"]}
        self.assertEqual(data["worktrees_scanned"], 6)
        names = {name for name, w in entries.items() if w.get("worktree")}
        self.assertEqual(names, {
            "product (worktree wt-modified)",
            "product (worktree wt-untracked)",
            "product (worktree wt-committed)",
            "product (worktree wt-live)",
        })
        modified = entries["product (worktree wt-modified)"]
        self.assertEqual([l["name"] for l in modified["loops"]], ["loop-alpha"])
        self.assertEqual(modified["loops"][0]["worktree_reasons"], ["modified"])
        needs = [i for i in modified["inbox"] if i["kind"] == "needs_human"]
        self.assertEqual(len(needs), 1)
        self.assertEqual(needs[0]["severity"], "high")
        self.assertIn("STATE.md", needs[0]["headline"])
        # The main checkout keeps all of its own loops, once.
        main_entry = entries["product"]
        self.assertEqual(sorted(l["name"] for l in main_entry["loops"]),
                         ["loop-alpha", "loop-beta", "loop/gamma"])
        total = sum(len(w["loops"]) for w in data["workspaces"])
        self.assertEqual(total, 3 + 4)
        # Worktrees are valid roots for drilldown but not registry workspaces.
        status, detail = _get(base + "/api/loop?" + urllib.parse.urlencode(
            {"root": str(self.modified), "name": "loop-alpha"}))
        self.assertEqual(status, 200, detail)
        status, listed = _get(base + "/api/workspaces")
        self.assertNotIn(str(self.modified), {w["path"] for w in listed})


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

    def test_work_in_the_workspace_softens_nothing_is_live(self):
        (self.root / "loop" / "STATE.md").write_text("status: running\n")
        listing = {"status": "ok", "running": []}
        card = {"name": "loop", "status": "running", "final_verdict": None,
                "last_activity": None}
        with serve._proc_snapshot([], listing):
            items = serve._inbox_items(self.root / "loop", card, self.root)
        self.assertEqual([(i["kind"], i["severity"]) for i in items],
                         [("interrupted", "medium")])
        worker = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"],
                                  cwd=self.root)
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        time.sleep(0.2)
        with serve._proc_snapshot(None, listing):
            items = serve._inbox_items(self.root / "loop", card, self.root)
        self.assertEqual([(i["kind"], i["severity"]) for i in items],
                         [("interrupted", "low")])
        self.assertIn("working in this workspace", items[0]["detail"])

    def test_state_blocked_without_verdict_is_flagged(self):
        (self.root / "loop" / "STATE.md").write_text("status: blocked\n")
        status, data = _get(
            f"http://127.0.0.1:{self.port}/api/board?"
            + urllib.parse.urlencode({"root": str(self.root)}))
        kinds = {(i["kind"], i["severity"]) for i in data["inbox"]}
        self.assertIn(("blocked", "high"), kinds)


if __name__ == "__main__":
    unittest.main()

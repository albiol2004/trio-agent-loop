#!/usr/bin/env python3
"""Unit tests for dashboard/live_model.py (GOAL DoD3: push, not poll)."""
from __future__ import annotations

import copy
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_PATH = REPO_ROOT / "dashboard" / "live_model.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_live_model_test", MODEL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live_model = _load()
LiveModel = live_model.LiveModel


def card(name, loop_id=None, status="running", age=1.5):
    out = {"name": name, "status": status,
           "heartbeat": {"live": True, "age_s": age, "updated_at": "t0"}}
    if loop_id:
        out["loop_id"] = loop_id
    return out


def overview(loops=(), inbox=(), stamp="2026-01-01T00:00:00Z", elapsed=10):
    return {
        "workspaces": [{"root": "/ws", "name": "ws", "elapsed_ms": elapsed,
                        "loops": list(loops), "inbox": list(inbox)}],
        "scanned": 1, "worktrees_scanned": 0, "scan_roots": [],
        "broker": "disabled", "updated_at": stamp, "elapsed_ms": elapsed,
    }


def item(item_id, headline="h"):
    return {"id": item_id, "loop": "a", "kind": "k", "headline": headline}


class ApplyTests(unittest.TestCase):
    def test_first_apply_upserts_everything(self):
        model = LiveModel()
        self.assertEqual((model.seq, model.last_scan_at), (0, None))
        batch = model.apply(overview([card("a", "id-a")], [item("i1")]))
        self.assertEqual((batch["seq"], batch["from_seq"]), (1, 0))
        self.assertEqual(batch["epoch"], model.epoch)
        got = {(c["op"], c["kind"], c["id"]) for c in batch["changes"]}
        self.assertEqual(got, {
            ("upsert", "meta", "overview"), ("upsert", "workspace", "/ws"),
            ("upsert", "loop", "id-a"), ("upsert", "inbox", "i1")})
        by_kind = {c["kind"]: c["data"] for c in batch["changes"]}
        self.assertEqual(by_kind["meta"]["workspace_order"], ["/ws"])
        self.assertNotIn("workspaces", by_kind["meta"])
        self.assertEqual(set(by_kind["workspace"]) & {"loops", "inbox"}, set())
        self.assertEqual(by_kind["loop"]["root"], "/ws")
        self.assertEqual(by_kind["loop"]["card"]["name"], "a")
        self.assertEqual(by_kind["inbox"]["item"]["id"], "i1")
        self.assertIsNotNone(model.last_scan_at)

    def test_volatile_only_change_is_none_and_seq_unchanged(self):
        # accept 1
        model = LiveModel()
        base = overview([card("a", "id-a")], [item("i1")])
        model.apply(base)
        again = copy.deepcopy(base)
        again["updated_at"] = "2026-01-01T00:00:10Z"
        again["elapsed_ms"] = 999
        again["workspaces"][0]["elapsed_ms"] = 555
        again["workspaces"][0]["loops"][0]["heartbeat"]["age_s"] = 99.0
        self.assertIsNone(model.apply(again))
        self.assertEqual(model.seq, 1)
        # the stored overview is the newest scan
        self.assertEqual(model.snapshot()["updated_at"], "2026-01-01T00:00:10Z")

    def test_status_inbox_and_removal_make_one_batch(self):
        # accept 2
        model = LiveModel()
        model.apply(overview([card("a", "id-a"), card("b", "id-b")],
                             [item("i1")]))
        batch = model.apply(overview(
            [card("a", "id-a", status="blocked")],
            [item("i1"), item("i2")]))
        self.assertEqual(model.seq, 2)
        self.assertEqual((batch["seq"], batch["from_seq"]), (2, 1))
        got = sorted((c["op"], c["kind"], c["id"]) for c in batch["changes"])
        self.assertEqual(got, sorted([
            ("upsert", "loop", "id-a"), ("upsert", "inbox", "i2"),
            ("remove", "loop", "id-b")]))
        removed = [c for c in batch["changes"] if c["op"] == "remove"][0]
        self.assertIsNone(removed["data"])
        upsert = [c for c in batch["changes"] if c["id"] == "id-a"][0]
        self.assertEqual(upsert["data"]["card"]["status"], "blocked")

    def test_loop_without_canonical_id_uses_path_id(self):
        self.assertEqual(live_model.loop_entity_id("/ws", {"name": "n"}),
                         "path:/ws::n")
        self.assertEqual(
            live_model.loop_entity_id("/ws", {"name": "n", "loop_id": "abc"}),
            "abc")
        model = LiveModel()
        batch = model.apply(overview([card("a")]))
        ids = {c["id"] for c in batch["changes"] if c["kind"] == "loop"}
        self.assertEqual(ids, {"path:/ws::a"})

    def test_workspace_order_change_upserts_meta(self):
        model = LiveModel()
        two = overview()
        two["workspaces"].append({"root": "/w2", "name": "w2", "loops": [],
                                  "inbox": []})
        model.apply(two)
        swapped = copy.deepcopy(two)
        swapped["workspaces"].reverse()
        batch = model.apply(swapped)
        self.assertEqual([(c["kind"], c["id"]) for c in batch["changes"]],
                         [("meta", "overview")])
        self.assertEqual(batch["changes"][0]["data"]["workspace_order"],
                         ["/w2", "/ws"])

    def test_volatile_keys_cover_the_known_scan_noise(self):
        self.assertTrue({"updated_at", "elapsed_ms", "age_s"}
                        <= live_model.VOLATILE_KEYS)

    def test_snapshot_is_a_copy_and_none_before_first_apply(self):
        model = LiveModel()
        self.assertIsNone(model.snapshot())
        model.apply(overview([card("a", "id-a")]))
        snap = model.snapshot()
        self.assertEqual((snap["epoch"], snap["seq"]), (model.epoch, 1))
        snap["scanned"] = 77
        snap["workspaces"] = []
        self.assertEqual(model.snapshot()["scanned"], 1)
        self.assertEqual(len(model.snapshot()["workspaces"]), 1)


class LogTests(unittest.TestCase):
    def test_changes_since(self):
        # accept 3
        model = LiveModel(max_log=2)
        model.apply(overview([card("a", "id-a", status="s0")]))
        for n in range(1, 4):
            last = model.apply(overview([card("a", "id-a", status=f"s{n}")]))
        self.assertEqual(model.seq, 4)
        self.assertEqual(model.changes_since(model.epoch, 3), [last])
        self.assertEqual(model.changes_since(model.epoch, 4), [])
        self.assertEqual(
            [b["seq"] for b in model.changes_since(model.epoch, 2)], [3, 4])
        self.assertIsNone(model.changes_since("other-epoch", 0))
        self.assertIsNone(model.changes_since(model.epoch, 5))
        # seq 1 fell out of the 2-batch log: seq 0 and 1 cannot be replayed
        self.assertIsNone(model.changes_since(model.epoch, 1))
        self.assertIsNone(model.changes_since(model.epoch, 0))

    def test_epochs_differ_between_instances(self):
        self.assertNotEqual(LiveModel().epoch, LiveModel().epoch)


class WaitTests(unittest.TestCase):
    def test_wait_returns_true_when_seq_advances(self):
        model = LiveModel()
        model.apply(overview([card("a", "id-a")]))
        threading.Timer(0.1, lambda: model.apply(
            overview([card("a", "id-a", status="x")]))).start()
        started = time.monotonic()
        self.assertTrue(model.wait(1, 5.0))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wait_wakes_on_scan_tick_without_change(self):
        model = LiveModel()
        base = overview([card("a", "id-a")])
        model.apply(base)
        threading.Timer(0.1, lambda: model.apply(copy.deepcopy(base))).start()
        started = time.monotonic()
        self.assertFalse(model.wait(1, 5.0))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wait_times_out_and_close_releases(self):
        model = LiveModel()
        model.apply(overview())
        started = time.monotonic()
        self.assertFalse(model.wait(1, 0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        threading.Timer(0.1, model.close).start()
        started = time.monotonic()
        self.assertFalse(model.wait(1, 5.0))
        self.assertLess(time.monotonic() - started, 2.0)
        started = time.monotonic()
        self.assertFalse(model.wait(1, 5.0))   # closed: returns immediately
        self.assertLess(time.monotonic() - started, 0.5)

    def test_wait_already_past_returns_true_immediately(self):
        model = LiveModel()
        model.apply(overview())
        self.assertTrue(model.wait(0, 5.0))


class ScanNoiseTests(unittest.TestCase):
    def test_two_scans_of_a_fixture_tree_produce_no_delta(self):
        """Real _build_overview twice over a fixture with a fresh heartbeat,
        a driver sidecar and an inbox item: the model sees no change."""
        import datetime
        import json
        import os
        import tempfile

        serve_spec = importlib.util.spec_from_file_location(
            "trio_dashboard_serve_live_model_test",
            REPO_ROOT / "dashboard" / "serve.py")
        serve = importlib.util.module_from_spec(serve_spec)
        sys.modules[serve_spec.name] = serve
        serve_spec.loader.exec_module(serve)
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as ws:
            mailbox = Path(ws) / "loop"
            mailbox.mkdir()
            (mailbox / "GOAL.md").write_text("# Mission: x\n\nmission: x\n")
            (mailbox / "STATE.md").write_text(
                "iteration: 2\nstatus: blocked\nphase: eval-done\n")
            (mailbox / "VERDICT.md").write_text(
                "VERDICT: NEEDS_HUMAN\n\n## Iteration 2\n")
            now = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            (mailbox / ".heartbeat.json").write_text(json.dumps({
                "schema": 1, "updated_at": now, "ttl_s": 600,
                "phase": "build", "iteration": 2, "writer": "x",
                "session_id": "s"}))
            (mailbox / ".driver.json").write_text(json.dumps({
                "pid": os.getpid(), "iteration": 2, "phase": "lead",
                "driver": "portable"}))
            saved = serve.HOME
            serve.HOME = Path(home)
            server = serve.DashboardServer(
                ("127.0.0.1", 0), workspaces=[Path(ws)], auto_discover=False)
            try:
                payload = server.board_payload
                model = LiveModel()
                first = server._build_overview(payload)
                time.sleep(1.1)
                second = server._build_overview(payload)
                self.assertIsNot(first, second)
                self.assertGreater(
                    len(first["workspaces"][0]["inbox"]), 0)
                self.assertTrue(
                    first["workspaces"][0]["loops"][0]["heartbeat"]["live"])
                self.assertIsNotNone(model.apply(first))
                self.assertIsNone(model.apply(second))
            finally:
                server.server_close()
                serve.HOME = saved


if __name__ == "__main__":
    unittest.main()

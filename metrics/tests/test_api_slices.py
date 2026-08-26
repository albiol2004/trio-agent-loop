"""Tests for /api/loop's open-loop slice + queue payload and the inbox's
fault/slice_overlap items (PLAN.md's frozen ``api:LoopDetailJSON`` contract,
slice ``api-slices-inbox``).

Boots dashboard/serve.py (loaded by path, like registry/tests/test_serve_inbox.py
-- serve.py delegates all slice/queue parsing to metrics/trio-metrics.py's
read_queue / derive_slices, so no parsing regex is duplicated here either).
Mailboxes are built in a temp dir; the repo's real ``loop*/`` directories are
gitignored and absent from a fresh checkout, so tests never depend on them.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_slices", SERVE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dashboard server: {SERVE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()

SHA_A = "a" * 40
SHA_B = "b" * 40


def _json_get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def _plan_slices_yaml(entries: list[str]) -> str:
    body = "\n".join(entries)
    return f"```yaml\nslices:\n{body}\n```\n"


def _slice_entry(sid, iteration, status="planned", writes=None, reads=None) -> str:
    writes = writes if writes is not None else []
    reads = reads if reads is not None else []
    return (
        f"  - id: {sid}\n"
        f"    iteration: {iteration}\n"
        f"    writes: [{', '.join(writes)}]\n"
        f"    reads: [{', '.join(reads)}]\n"
        f"    status: {status}\n"
        f"    accepts: []"
    )


class _MailboxServerTestCase(unittest.TestCase):
    """Boots dashboard/serve.py against an isolated workspace and HOME."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        self.original_home = serve.HOME
        serve.HOME = Path(self.home.name)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[self.root], auto_discover=False
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        serve.HOME = self.original_home
        self.workspace.cleanup()
        self.home.cleanup()

    def make_mailbox(
        self,
        name: str,
        *,
        iteration=1,
        max_iterations=3,
        status="running",
        plan_slices: list[str] | None = None,
        queue: str | None = None,
        verdict: str = "",
        log: str = "- iter 1 | lead | working\n",
    ) -> Path:
        mailbox = self.root / name
        mailbox.mkdir()
        (mailbox / "GOAL.md").write_text(
            "# Test loop\n\nmission: test\n", encoding="utf-8"
        )
        (mailbox / "STATE.md").write_text(
            f"iteration: {iteration}\nmax_iterations: {max_iterations}\n"
            f"status: {status}\n",
            encoding="utf-8",
        )
        (mailbox / "LOG.md").write_text(log, encoding="utf-8")
        if plan_slices is not None:
            (mailbox / "PLAN.md").write_text(
                _plan_slices_yaml(plan_slices), encoding="utf-8"
            )
        else:
            (mailbox / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
        if queue is not None:
            (mailbox / "QUEUE.md").write_text(queue, encoding="utf-8")
        (mailbox / "VERDICT.md").write_text(verdict, encoding="utf-8")
        return mailbox

    def detail(self, name: str) -> dict:
        query = urllib.parse.urlencode({"name": name, "root": str(self.root)})
        status, payload = _json_get(f"{self.base}/api/loop?{query}")
        self.assertEqual(status, 200, payload)
        return payload

    def board(self) -> dict:
        query = urllib.parse.urlencode({"root": str(self.root)})
        status, payload = _json_get(f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200, payload)
        return payload


# --- mode / slices / queue shape --------------------------------------------


class LoopDetailShapeTests(_MailboxServerTestCase):
    def test_open_loop_payload_shape(self):
        self.make_mailbox(
            "loop-open",
            plan_slices=[
                _slice_entry("sa", 1, status="complete"),
                _slice_entry("sb", 1, status="in_progress"),
            ],
            queue=(
                "```yaml\n"
                "retired:\n"
                f"  - slice: sa\n    sha: {SHA_A}\n    at: 2026-08-26T10:00:00Z\n"
                "```\n"
                "```yaml\n"
                "faults:\n"
                "```\n"
            ),
            verdict=f"## slice sa @{SHA_A} — SHIP\n",
        )
        card = self.detail("loop-open")
        self.assertEqual(card["mode"], "open-loop")
        self.assertIsInstance(card["slices"], list)
        self.assertEqual(len(card["slices"]), 2)
        derived_keys = {
            "id", "iteration", "lifecycle", "retired_sha", "retired_at",
            "verdict", "open_faults", "superseded", "stale_candidates",
        }
        raw_keys = {"writes", "reads", "status"}
        for sl in card["slices"]:
            self.assertTrue(derived_keys.issubset(sl.keys()), sl)
            self.assertTrue(raw_keys.issubset(sl.keys()), sl)
        by_id = {sl["id"]: sl for sl in card["slices"]}
        self.assertEqual(by_id["sa"]["lifecycle"], "shipped")
        self.assertEqual(by_id["sa"]["retired_sha"], SHA_A)
        self.assertEqual(by_id["sa"]["verdict"], "SHIP")
        self.assertEqual(by_id["sb"]["lifecycle"], "building")
        self.assertIn("queue", card)
        self.assertIn("retired", card["queue"])
        self.assertIn("faults", card["queue"])
        self.assertEqual(card["queue"]["retired"][0]["slice"], "sa")

    def test_lockstep_payload_shape(self):
        self.make_mailbox(
            "loop-lockstep",
            plan_slices=[_slice_entry("sa", 1, status="complete")],
        )
        card = self.detail("loop-lockstep")
        self.assertEqual(card["mode"], "lockstep")
        self.assertIsInstance(card["slices"], list)
        self.assertNotIn("queue", card)
        # existing app.js consumers still get writes/reads/status
        self.assertTrue({"writes", "reads", "status"}.issubset(card["slices"][0]))

    def test_open_loop_slices_empty_when_no_slices_block(self):
        self.make_mailbox("loop-empty", queue="")
        card = self.detail("loop-empty")
        self.assertEqual(card["mode"], "open-loop")
        self.assertEqual(card["slices"], [])


# --- inbox: fault items + slice_overlap / overlap suppression --------------


class InboxFaultAndOverlapTests(_MailboxServerTestCase):
    def test_open_fault_emits_queue_fault_item(self):
        self.make_mailbox(
            "loop-fault",
            plan_slices=[_slice_entry("sa", 1, status="in_progress")],
            queue=(
                "```yaml\nretired:\n```\n"
                "```yaml\n"
                "faults:\n"
                "  - id: f1\n"
                "    slice: sa\n"
                f"    observed_at: {SHA_A}\n"
                "    scope: [src/a.py, src/b.py]\n"
                "    reason: broke the thing\n"
                "    status: open\n"
                "```\n"
            ),
        )
        items = self.board()["inbox"]
        fault_items = [i for i in items if i["kind"] == "queue_fault"]
        self.assertEqual(len(fault_items), 1, items)
        item = fault_items[0]
        self.assertEqual(item["severity"], "medium")
        self.assertIn("sa", item["headline"])
        self.assertIn("f1", item["headline"] + item["detail"])
        self.assertIn("src/a.py", item["detail"])
        self.assertIn("broke the thing", item["detail"])

    def test_done_fault_emits_no_item(self):
        self.make_mailbox(
            "loop-fault-done",
            plan_slices=[_slice_entry("sa", 1, status="in_progress")],
            queue=(
                "```yaml\nretired:\n```\n"
                "```yaml\n"
                "faults:\n"
                "  - id: f1\n"
                "    slice: sa\n"
                f"    observed_at: {SHA_A}\n"
                "    scope: [src/a.py]\n"
                "    reason: fixed already\n"
                "    status: done\n"
                "```\n"
            ),
        )
        items = self.board()["inbox"]
        self.assertFalse([i for i in items if i["kind"] == "queue_fault"], items)

    def _overlapping_slices(self):
        return [
            _slice_entry(
                "s7", 7, status="in_progress",
                writes=["src/road.js", "src/state.js"],
            ),
            _slice_entry(
                "s8", 8, status="in_progress",
                writes=["src/road.js", "src/car.js"],
            ),
        ]

    def test_lockstep_keeps_iteration_overlap_item(self):
        self.make_mailbox(
            "loop-overlap-lockstep",
            iteration=8,
            status="running",
            plan_slices=self._overlapping_slices(),
            log="- iter 7 | lead | working\n- iter 8 | lead | working\n",
        )
        items = self.board()["inbox"]
        kinds = {i["kind"] for i in items}
        self.assertIn("overlap", kinds)
        self.assertNotIn("slice_overlap", kinds)
        self.assertNotIn("queue_fault", kinds)

    def test_open_loop_suppresses_overlap_and_emits_slice_overlap(self):
        self.make_mailbox(
            "loop-overlap-open",
            iteration=8,
            status="running",
            plan_slices=self._overlapping_slices(),
            log="- iter 7 | lead | working\n- iter 8 | lead | working\n",
            queue="",
        )
        items = self.board()["inbox"]
        kinds = {i["kind"] for i in items}
        self.assertNotIn("overlap", kinds)
        self.assertIn("slice_overlap", kinds)
        item = next(i for i in items if i["kind"] == "slice_overlap")
        self.assertEqual(item["severity"], "medium")
        self.assertIn("s7", item["headline"])
        self.assertIn("s8", item["headline"])
        self.assertIn("src/road.js", item["detail"])

    def test_no_slice_overlap_when_write_sets_disjoint(self):
        self.make_mailbox(
            "loop-overlap-none",
            iteration=8,
            status="running",
            plan_slices=[
                _slice_entry("s7", 7, status="in_progress", writes=["src/a.js"]),
                _slice_entry("s8", 8, status="in_progress", writes=["src/b.js"]),
            ],
            log="- iter 7 | lead | working\n- iter 8 | lead | working\n",
            queue="",
        )
        items = self.board()["inbox"]
        self.assertFalse([i for i in items if i["kind"] == "slice_overlap"], items)


# --- open-loop iteration lifecycle, derived from slices ---------------------


class OpenLoopIterationLifecycleTests(unittest.TestCase):
    """Direct unit tests of serve._open_loop_iteration_lifecycle."""

    def test_all_shipped_slices_yield_shipped_iteration(self):
        iterations = [{"n": 1, "lifecycle": "in_flight", "verdict": None}]
        slices = [
            {"iteration": 1, "lifecycle": "shipped"},
            {"iteration": 1, "lifecycle": "shipped"},
        ]
        out = serve._open_loop_iteration_lifecycle(iterations, slices)
        self.assertEqual(out[0]["lifecycle"], "shipped")

    def test_one_building_slice_yields_in_flight(self):
        iterations = [{"n": 1, "lifecycle": "planned", "verdict": None}]
        slices = [
            {"iteration": 1, "lifecycle": "shipped"},
            {"iteration": 1, "lifecycle": "building"},
        ]
        out = serve._open_loop_iteration_lifecycle(iterations, slices)
        self.assertEqual(out[0]["lifecycle"], "in_flight")

    def test_integration_verdict_wins_over_slice_derivation(self):
        iterations = [{"n": 1, "lifecycle": "shipped", "verdict": "SHIP"}]
        slices = [{"iteration": 1, "lifecycle": "building"}]
        out = serve._open_loop_iteration_lifecycle(iterations, slices)
        self.assertEqual(out[0]["lifecycle"], "shipped")

    def test_iteration_with_no_slices_is_untouched(self):
        iterations = [{"n": 2, "lifecycle": "planned", "verdict": None}]
        out = serve._open_loop_iteration_lifecycle(iterations, [])
        self.assertEqual(out[0]["lifecycle"], "planned")


class OpenLoopIterationEndToEndTests(_MailboxServerTestCase):
    def test_detail_endpoint_marks_all_shipped_iteration_shipped(self):
        self.make_mailbox(
            "loop-shipped-iter",
            iteration=1,
            status="running",
            plan_slices=[_slice_entry("sa", 1, status="complete")],
            queue=(
                "```yaml\n"
                "retired:\n"
                f"  - slice: sa\n    sha: {SHA_A}\n    at: 2026-08-26T10:00:00Z\n"
                "```\n"
            ),
            verdict=f"## slice sa @{SHA_A} — SHIP\n",
        )
        card = self.detail("loop-shipped-iter")
        self.assertEqual(card["mode"], "open-loop")
        it1 = next(it for it in card["iterations"] if it["n"] == 1)
        self.assertEqual(it1["lifecycle"], "shipped")


if __name__ == "__main__":
    unittest.main()

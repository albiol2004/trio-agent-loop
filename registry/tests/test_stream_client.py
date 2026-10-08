"""The legacy drawer islands of dashboard/app.js behind the store bridge.

GOAL DoD6 moved the board, the push stream and the drawer frame/overview out
of app.js into Preact components over a signals store (dashboard/ui/*.js).
What stays in app.js (DECISION D4-3) is the Timeline tab, Files, Graph, the
sessions list and transcript pane and the Unblock section, driven only
through ``window.trioLegacy`` (onOpen / onClose / onLoop / onDetail / onTab /
onTick). These tests run the real app.js in Node (vm context, a fake DOM that
counts writes per element id, a scriptable fetch and a manual clock; see
stream_client_harness.cjs) and check observable results: the fetches issued,
the DOM writes landed in each view, the text shown.

GOAL DoD7: replaced by equivalents. The vm render-guard and stream tests that
used to live here exercised functions that no longer exist in app.js; each is
listed with the test that now covers it (the test_ui_* browser/node suites
run the real modules, test_ui_e2e the whole page).

  StreamModelTests (snapshot round trip, delta upsert/add/reorder/remove, gap
  and foreign-epoch refusal)
      -> test_ui_store.py::StoreModelTests (snapshot_builds_keyed_loops_and_items,
         delta_notifies_only_the_changed_loop, membership_changes_reassign_the_id_lists_once,
         gap_and_foreign_epoch_are_refused_untouched)
  BoardGuardTests (identical state writes nothing, changed card, running/kpis,
  unchanged kpis, inbox lists, read flag, tab switch)
      -> test_ui_board.py (delta_re_renders_only_the_changed_loops_dom,
         clock_tick_only_moves_relative_time_text, added_and_removed_loops...,
         attention_split..., mark_read..., tabs_workspace_filter_sort...)
  DetailGuardTests: commits, mission/verdicts, slices, overview timeline
      -> test_ui_drawer.py (equal_detail_and_clock_ticks_write_only_reltime_text,
         show_all_commits_survives_a_changed_detail, open_renders_the_detail...)
  DetailGuardTests: files / graph / session list (still app.js code)
      -> kept below as LegacyViewGuardTests
  StreamFlowTests (healthy stream never polls, tick, delta, gap resync,
  foreign epoch, polling fallback, 45 s silence, no EventSource, hidden tab,
  detail refetch only for the open loop, hash opens on the first snapshot,
  read flag survives deltas)
      -> test_ui_store.py::TransportTests / LocationTests (same cases on
         transport.js), test_ui_e2e.py (the page: no /api/overview while the
         stream is up, polling with TRIO_DASH_STREAM=0, hash <-> drawer,
         read/unread against a lab server)
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import unittest
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "stream_client_harness.cjs"

ROOT_A = "/w/a"
KEY = ROOT_A + "::alpha"
LOOP = {"key": KEY, "root": ROOT_A, "name": "alpha", "workspace": "a", "id": "lid-alpha"}


def run(ops):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"ops": ops}),
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "TZ": "UTC"},
    )
    if proc.returncode != 0:
        raise AssertionError("harness failed: " + proc.stderr)
    out = json.loads(proc.stdout)
    return out["results"], out["errors"]


def tagged(results):
    """Results of the ops that carry a "tag", by tag (an op error fails the test)."""
    out = {}
    for i, r in enumerate(results):
        if "error" in r:
            raise AssertionError("op %d (%s) failed: %s" % (i, r.get("tag"), r["error"]))
        if r.get("tag"):
            out[r["tag"]] = r["value"]
    return out


def make_detail(**kw):
    detail = {
        "name": "alpha", "mission": "Do alpha", "status": "iterating", "iteration": 2,
        "max_iterations": 5, "final_verdict": None, "last_activity": "2026-05-30T08:00:00+00:00",
        "segments": [{"verdict_sequence": "IS"}], "running": True, "running_sources": ["driver"],
        "commits": [],
        "slices": [
            {"id": "s1", "iteration": 1, "status": "complete", "lifecycle": "shipped",
             "writes": ["a.py"], "reads": []},
            {"id": "s2", "iteration": 2, "status": "in_progress", "lifecycle": "building",
             "writes": ["b.py"], "reads": ["a.py"]},
        ],
        "slice_activity": {"slices": []},
        "timeline": [
            {"iteration": 1, "role": "lead", "summary": "planned", "duration_sec": 30},
            {"iteration": 1, "role": "evaluator", "summary": "VERDICT: SHIP — fine",
             "verdict": "SHIP", "duration_sec": 12},
        ],
        "iterations": [{"n": 1, "lifecycle": "shipped"}, {"n": 2, "lifecycle": "building"}],
        "mode": "open-loop", "overlaps": [],
        "sessions": [{"path": "/s/one.jsonl", "label": "one.jsonl", "size": 10,
                      "status": "ok", "timestamp": "2026-06-01T11:59:30+00:00"}],
    }
    detail.update(kw)
    return detail


ACTIONS_404 = {"__status": 404, "body": {"error": "no actions in this test"}}


def boot_ops(**boot):
    return [{"op": "boot", **boot},
            {"op": "routes", "routes": {"/api/loop/actions": ACTIONS_404}}]


def open_ops(tab=None):
    """The drawer opened `alpha` (onOpen), and optionally showed a view."""
    ops = [{"op": "call", "fn": "window.trioLegacy.onOpen", "args": [LOOP], "quiet": True}]
    if tab:
        ops.append({"op": "call", "fn": "window.trioLegacy.onTab", "args": [tab], "quiet": True})
    return ops


def detail_op(detail):
    return {"op": "call", "fn": "window.trioLegacy.onDetail", "args": [detail, LOOP], "quiet": True}


def actions_fetches(fetches):
    return [f["url"] for f in fetches if f["url"].split("?")[0] == "/api/loop/actions"]


class BridgeTests(unittest.TestCase):
    def test_app_js_alone_starts_no_feed_and_exposes_the_bridge(self):
        # replaces StreamFlowTests.test_snapshot_renders_the_board_and_never_polls_while_healthy
        # (the app.js half): the stream/poll transport lives in ui/transport.js now
        res, errors = run([
            {"op": "boot", "readyState": "complete"},
            {"op": "fetches", "tag": "fetches"},
            {"op": "esList", "tag": "sources"},
            {"op": "timersPending", "tag": "timers"},
            {"op": "eval", "code": "Object.keys(window.trioLegacy).sort()", "tag": "bridge"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual(out["fetches"], [])
        self.assertEqual(out["sources"], [])
        self.assertEqual(out["timers"], 0)
        self.assertEqual(out["bridge"],
                         ["onClose", "onDetail", "onLoop", "onOpen", "onTab", "onTick"])

    def test_open_fetches_the_unblock_state_of_that_loop_only(self):
        res, errors = run(boot_ops() + open_ops() + [
            {"op": "fetches", "clear": True, "tag": "opened"},
            # the open loop's card changed: its options may have changed too
            {"op": "call", "fn": "window.trioLegacy.onLoop", "args": [dict(LOOP, title="x")], "quiet": True},
            {"op": "fetches", "clear": True, "tag": "changed"},
            # a card of some other loop is not this drawer's business
            {"op": "call", "fn": "window.trioLegacy.onLoop",
             "args": [dict(LOOP, key=ROOT_A + "::beta", name="beta")], "quiet": True},
            {"op": "fetches", "clear": True, "tag": "other"},
            {"op": "call", "fn": "window.trioLegacy.onClose", "quiet": True},
            {"op": "call", "fn": "window.trioLegacy.onLoop", "args": [LOOP], "quiet": True},
            {"op": "fetches", "clear": True, "tag": "closed"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        want = "/api/loop/actions?root=%s&loop=alpha" % quote(ROOT_A, safe="")
        self.assertEqual(actions_fetches(out["opened"]), [want])
        self.assertEqual(actions_fetches(out["changed"]), [want])
        self.assertEqual(actions_fetches(out["other"]), [])
        self.assertEqual(actions_fetches(out["closed"]), [])

    def test_after_an_action_the_store_refetches_the_detail(self):
        res, errors = run(boot_ops() + [
            {"op": "eval", "quiet": True, "code": (
                "window.__trioStore = { calls: [], refreshBoard() { this.calls.push('board'); },"
                " refreshDetail() { this.calls.push('detail'); } }")},
            {"op": "call", "fn": "refreshAfterAction", "quiet": True},
            {"op": "eval", "code": "window.__trioStore.calls", "tag": "calls"},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(tagged(res)["calls"], ["board", "detail"])

    def test_a_timeline_slice_chip_asks_the_store_for_the_graph_tab(self):
        res, errors = run(boot_ops() + [
            {"op": "eval", "quiet": True, "code": (
                "window.__trioStore = { drawerTab: { value: 'timeline' } }")},
        ] + open_ops("timeline") + [
            detail_op(make_detail()),
            {"op": "click", "selector": "#view-timeline .chip-btn"},
            {"op": "eval", "code": "[window.__trioStore.drawerTab.value, state.graphSel]", "tag": "got"},
        ])
        self.assertEqual(errors, [])
        got = tagged(res)["got"]
        self.assertEqual(got[0], "graph")
        self.assertIn(got[1], ("s1", "s2"))


class LegacyViewGuardTests(unittest.TestCase):
    """The files/graph/session-list part of the old DetailGuardTests."""

    def counts_after(self, tab, first, second):
        res, errors = run(boot_ops() + open_ops(tab) + [
            detail_op(first), {"op": "resetCounts"}, detail_op(second), {"op": "counts", "tag": "counts"},
        ])
        self.assertEqual(errors, [])
        return tagged(res)["counts"]

    def test_identical_detail_writes_nothing_in_the_shown_view_or_the_sessions(self):
        detail = make_detail()
        for tab in ("graph", "files", "timeline"):
            with self.subTest(tab=tab):
                counts = self.counts_after(tab, detail, copy.deepcopy(detail))
                self.assertEqual(counts.get("view-" + tab, 0), 0, counts)
                self.assertEqual(counts.get("session-list", 0), 0, counts)

    def test_a_view_that_is_not_shown_is_not_rendered(self):
        res, errors = run(boot_ops() + open_ops() + [
            {"op": "resetCounts"}, detail_op(make_detail()), {"op": "counts", "tag": "counts"},
        ])
        self.assertEqual(errors, [])
        counts = tagged(res)["counts"]
        for view in ("view-timeline", "view-files", "view-graph"):
            self.assertEqual(counts.get(view, 0), 0, view)
        self.assertGreater(counts.get("session-list", 0), 0)

    def test_showing_a_view_renders_it_from_the_cached_detail(self):
        res, errors = run(boot_ops() + open_ops() + [
            detail_op(make_detail()), {"op": "resetCounts"},
            {"op": "call", "fn": "window.trioLegacy.onTab", "args": ["graph"], "quiet": True},
            {"op": "counts", "tag": "counts"},
            {"op": "text", "id": "view-graph", "tag": "text"},
            {"op": "fetches", "tag": "fetches"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertGreater(out["counts"].get("view-graph", 0), 0)
        self.assertIn("s2", out["text"])
        self.assertEqual([f for f in out["fetches"] if f["url"].startswith("/api/loop?")], [])

    def test_changed_slice_graph_rewrites_files_and_graph_views(self):
        detail = make_detail()
        slices = copy.deepcopy(detail["slices"])
        slices[1]["writes"] = ["b.py", "c.py"]
        changed = make_detail(slices=slices)
        for tab in ("graph", "files"):
            with self.subTest(tab=tab):
                counts = self.counts_after(tab, detail, changed)
                self.assertGreater(counts.get("view-" + tab, 0), 0, counts)
                self.assertEqual(counts.get("session-list", 0), 0, counts)

    def test_changed_timeline_rewrites_the_timeline_view(self):
        detail = make_detail()
        timeline = detail["timeline"] + [
            {"iteration": 2, "role": "builder", "summary": "built s2", "duration_sec": 99}]
        counts = self.counts_after("timeline", detail, make_detail(timeline=timeline))
        self.assertGreater(counts.get("view-timeline", 0), 0, counts)

    def test_session_list_only_rewrites_when_sessions_change(self):
        detail = make_detail()
        more = detail["sessions"] + [
            {"path": "/s/two.jsonl", "label": "two.jsonl", "size": 5, "status": "ok",
             "timestamp": "2026-06-01T11:00:00+00:00"}]
        res, errors = run(boot_ops() + open_ops() + [
            detail_op(detail), {"op": "resetCounts"},
            detail_op(copy.deepcopy(detail)), {"op": "counts", "tag": "same"},
            {"op": "resetCounts"}, detail_op(make_detail(sessions=more)),
            {"op": "counts", "tag": "changed"},
            {"op": "text", "id": "session-list", "tag": "text"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual(out["same"].get("session-list", 0), 0)
        self.assertGreater(out["changed"].get("session-list", 0), 0)
        self.assertIn("two", out["text"])

    def test_the_clock_moves_a_session_age_as_text_without_rebuilding_the_list(self):
        res, errors = run(boot_ops() + open_ops() + [
            detail_op(make_detail()),
            {"op": "eval", "quiet": True,
             "code": "window.__node = document.querySelector('#session-list time[data-reltime]')"},
            {"op": "eval", "tag": "before", "code": "document.getElementById('session-list').textContent"},
            {"op": "advance", "ms": 90000},
            {"op": "call", "fn": "window.trioLegacy.onTick", "quiet": True},
            {"op": "eval", "tag": "after", "code": "document.getElementById('session-list').textContent"},
            {"op": "eval", "tag": "same_node",
             "code": "Boolean(window.__node) && window.__node === "
                     "document.querySelector('#session-list time[data-reltime]')"},
            {"op": "eval", "tag": "one_time", "code": "document.querySelectorAll('#session-list time').length"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertIn("30s ago", out["before"])
        self.assertIn("2m ago", out["after"])
        self.assertEqual(out["one_time"], 1)
        self.assertTrue(out["same_node"])


if __name__ == "__main__":
    unittest.main()

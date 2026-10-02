"""Push (SSE) client of the dashboard board: stream model, delta apply, guarded
section renders, and the stream -> polling fallback.

Runs the real dashboard/app.js in Node (vm context with a fake DOM that counts
writes per element id, a scriptable fetch / EventSource and a manual clock; see
stream_client_harness.cjs). The tests drive the product through its functions
and the EventSource it opens, and check observable results: the model/overview
values, DOM write counts, the fetches issued and the EventSources opened.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "stream_client_harness.cjs"

ROOT_A = "/w/a"
ROOT_B = "/w/b"
LIVE_IDS = {"live", "live-text"}


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


def values(results):
    """Op results as plain values; any op error fails the test with its text."""
    out = []
    for i, r in enumerate(results):
        if "error" in r:
            raise AssertionError("op %d failed: %s" % (i, r["error"]))
        out.append(r["value"])
    return out


# ------------------------------ fixtures ------------------------------

def make_card(name, **kw):
    card = {
        "name": name, "loop_id": "lid-" + name, "title": name.title(),
        "mission": "Do " + name, "status": "iterating", "iteration": 2,
        "max_iterations": 5, "final_verdict": None, "running": False,
        "running_sources": [], "last_activity": "2026-05-30T08:00:00+00:00",
        "segments": [{"verdict_sequence": "IS"}], "driver_phase": "idle",
    }
    card.update(kw)
    return card


def make_item(item_id, loop, **kw):
    item = {"id": item_id, "loop": loop, "kind": "needs_human", "severity": "high",
            "headline": loop + " needs a human", "detail": "answer HUMAN.md", "read": False}
    item.update(kw)
    return item


def make_snapshot(epoch="e1", seq=7):
    return {
        "workspaces": [
            {"root": ROOT_A, "name": "a", "elapsed_ms": 3, "unattributed_processes": 0,
             "loops": [make_card("alpha", running=True, running_sources=["driver"]),
                       make_card("beta", final_verdict="SHIP")],
             "inbox": [make_item("i1", "alpha"),
                       make_item("i2", "beta", kind="drift", severity="low",
                                 headline="beta drifted", detail="")]},
            {"root": ROOT_B, "name": "b", "worktree": True, "elapsed_ms": 2,
             "loops": [make_card("gamma")], "inbox": []},
        ],
        "scanned": 2, "worktrees_scanned": 1, "scan_roots": [ROOT_A, ROOT_B],
        "broker": "disabled", "updated_at": "2026-06-01T11:59:58+00:00", "elapsed_ms": 12,
        "epoch": epoch, "seq": seq,
    }


def overview_of(snapshot):
    ov = copy.deepcopy(snapshot)
    ov.pop("epoch", None)
    ov.pop("seq", None)
    return ov


def loop_change(op, root, card):
    return {"op": op, "kind": "loop", "id": card["loop_id"],
            "data": {"root": root, "card": card} if op == "upsert" else None}


def inbox_change(op, root, item):
    return {"op": op, "kind": "inbox", "id": item["id"],
            "data": {"root": root, "item": item} if op == "upsert" else None}


def make_delta(changes, epoch="e1", from_seq=7, seq=8, updated_at="2026-06-01T12:00:01+00:00"):
    return {"epoch": epoch, "seq": seq, "from_seq": from_seq,
            "updated_at": updated_at, "changes": changes}


def make_detail(**kw):
    detail = {
        "name": "alpha", "mission": "Do alpha", "status": "iterating", "iteration": 2,
        "max_iterations": 5, "final_verdict": None, "last_activity": "2026-05-30T08:00:00+00:00",
        "segments": [{"verdict_sequence": "IS"}], "running": True, "running_sources": ["driver"],
        "commits": [{"sha": "a" * 40, "short": "aaaaaaa", "slice": "s1", "subject": "slice(s1): first"}],
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
                      "status": "ok", "timestamp": "2026-05-30T08:00:00+00:00"}],
    }
    detail.update(kw)
    return detail


def board_ops(snapshot=None, active=ROOT_A + "::alpha"):
    """Boot the app and put a board on screen (no stream involved)."""
    ops = [{"op": "boot"},
           {"op": "call", "fn": "ingestOverview", "args": [overview_of(snapshot or make_snapshot())], "quiet": True},
           {"op": "eval", "code": "state.loaded = true; state.lastOk = Date.now()", "quiet": True}]
    if active:
        ops.append({"op": "eval", "code": "state.activeLoop = %s" % json.dumps(active), "quiet": True})
    return ops


def tagged(results):
    """Results of the ops that carry a "tag", by tag (an op error fails the test)."""
    out = {}
    for i, r in enumerate(results):
        if "error" in r:
            raise AssertionError("op %d (%s) failed: %s" % (i, r.get("tag"), r["error"]))
        if r.get("tag"):
            out[r["tag"]] = r["value"]
    return out


def overview_fetches(fetches):
    return [f for f in fetches if f["url"].split("?")[0] == "/api/overview"]


def urls(fetches, prefix):
    return [f["url"] for f in fetches if f["url"].split("?")[0] == prefix]


def since_of(url):
    return parse_qs(urlsplit(url).query).get("since", [None])[0]


# ------------------------------ model and deltas ------------------------------

class StreamModelTests(unittest.TestCase):
    def test_snapshot_round_trips_through_the_model(self):
        # accept 1
        snap = make_snapshot()
        res, errors = run([
            {"op": "boot"},
            {"op": "call", "fn": "streamModelFromSnapshot", "args": [snap], "as": "m", "quiet": True},
            {"op": "call", "fn": "overviewFromStreamModel", "args": [{"$ref": "m"}]},
            {"op": "var", "name": "m"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        overview = out[2]
        self.assertEqual(overview["workspaces"], snap["workspaces"])
        for key in snap:
            if key not in ("epoch", "seq"):
                self.assertEqual(overview[key], snap[key], key)
        model = out[3]
        self.assertEqual((model["epoch"], model["seq"]), ("e1", 7))
        self.assertEqual([k for k, _ in model["loops"]["__map"]], ["lid-alpha", "lid-beta", "lid-gamma"])

    def test_delta_upserts_loop_and_removes_inbox_item(self):
        # accept 2
        snap = make_snapshot()
        alpha2 = make_card("alpha", running=False, status="done", final_verdict="SHIP")
        delta = make_delta([loop_change("upsert", ROOT_A, alpha2),
                            inbox_change("remove", ROOT_A, make_item("i1", "alpha"))])
        res, errors = run([
            {"op": "boot"},
            {"op": "call", "fn": "streamModelFromSnapshot", "args": [snap], "as": "m", "quiet": True},
            {"op": "call", "fn": "applyStreamDelta", "args": [{"$ref": "m"}, delta]},
            {"op": "call", "fn": "overviewFromStreamModel", "args": [{"$ref": "m"}]},
            {"op": "var", "name": "m"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        applied, overview, model = out[2], out[3], out[4]
        self.assertIs(applied["ok"], True)
        self.assertEqual(applied["loopIds"], {"__set": ["lid-alpha"]})
        ws_a = overview["workspaces"][0]
        self.assertEqual(ws_a["loops"][0], alpha2)  # replaced in place, still first
        self.assertEqual([c["name"] for c in ws_a["loops"]], ["alpha", "beta"])
        self.assertEqual([i["id"] for i in ws_a["inbox"]], ["i2"])
        self.assertEqual(model["seq"], 8)
        self.assertEqual(model["meta"]["updated_at"], delta["updated_at"])
        self.assertEqual(overview["updated_at"], delta["updated_at"])

    def test_delta_adds_reorders_and_removes_entities(self):
        snap = make_snapshot()
        delta_cards = make_card("delta-new")
        ws_c = {"root": "/w/c", "name": "c", "elapsed_ms": 1}
        meta = {k: v for k, v in snap.items() if k not in ("workspaces", "epoch", "seq")}
        meta["workspace_order"] = [ROOT_B, "/w/c", ROOT_A]
        delta = make_delta([
            {"op": "upsert", "kind": "workspace", "id": "/w/c", "data": ws_c},
            {"op": "upsert", "kind": "meta", "id": "overview", "data": meta},
            loop_change("upsert", "/w/c", delta_cards),
            loop_change("upsert", ROOT_A, make_card("omega")),
            loop_change("remove", ROOT_A, make_card("beta")),
        ])
        res, errors = run([
            {"op": "boot"},
            {"op": "call", "fn": "streamModelFromSnapshot", "args": [snap], "as": "m", "quiet": True},
            {"op": "call", "fn": "applyStreamDelta", "args": [{"$ref": "m"}, delta]},
            {"op": "call", "fn": "overviewFromStreamModel", "args": [{"$ref": "m"}]},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        self.assertIs(out[2]["ok"], True)
        self.assertEqual(sorted(out[2]["kinds"]["__set"]), ["loop", "meta", "workspace"])
        overview = out[3]
        self.assertEqual([w["root"] for w in overview["workspaces"]], [ROOT_B, "/w/c", ROOT_A])
        by_root = {w["root"]: w for w in overview["workspaces"]}
        self.assertEqual([c["name"] for c in by_root["/w/c"]["loops"]], ["delta-new"])
        self.assertEqual([c["name"] for c in by_root[ROOT_A]["loops"]], ["alpha", "omega"])
        self.assertEqual(by_root["/w/c"]["inbox"], [])

    def test_delta_with_gap_or_foreign_epoch_is_refused(self):
        # accept 3
        snap = make_snapshot()
        change = [loop_change("upsert", ROOT_A, make_card("alpha", status="done"))]
        cases = {
            "gap": make_delta(change, from_seq=9, seq=10),
            "stale": make_delta(change, from_seq=3, seq=4),
            "foreign epoch": make_delta(change, epoch="other", from_seq=7),
        }
        for label, delta in cases.items():
            with self.subTest(label):
                res, errors = run([
                    {"op": "boot"},
                    {"op": "call", "fn": "streamModelFromSnapshot", "args": [snap], "as": "m", "quiet": True},
                    {"op": "var", "name": "m"},
                    {"op": "call", "fn": "applyStreamDelta", "args": [{"$ref": "m"}, delta]},
                    {"op": "var", "name": "m"},
                ])
                self.assertEqual(errors, [])
                out = values(res)
                self.assertIs(out[3]["ok"], False)
                self.assertEqual(out[3]["loopIds"], {"__set": []})
                self.assertEqual(out[2], out[4])  # model untouched


# ------------------------------ guarded renders ------------------------------

BOARD_IDS = ["kpis", "needs-list", "running-list", "loop-rows", "notes-list",
             "workspace-filter", "tabs", "drawer-inbox-list"]


class BoardGuardTests(unittest.TestCase):
    def render_twice(self, snapshot=None):
        res, errors = run(board_ops(snapshot) + [
            {"op": "resetCounts"},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
            {"op": "resetCounts"},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        return out[-4], out[-1]

    def test_second_render_of_identical_state_writes_nothing(self):
        # accept 4
        first, second = self.render_twice()
        for ident in BOARD_IDS:
            self.assertGreater(first.get(ident, 0), 0, "first render should fill #" + ident)
        stray = {k: v for k, v in second.items() if k not in LIVE_IDS}
        self.assertEqual(stray, {})

    def test_changed_card_rewrites_the_board(self):
        # positive control: guards must not freeze real updates
        snap = make_snapshot()
        changed = make_snapshot()
        changed["workspaces"][0]["loops"][1]["status"] = "blocked"
        changed["workspaces"][0]["loops"][1]["final_verdict"] = "BLOCKED"
        res, errors = run(board_ops(snap) + [
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "ingestOverview", "args": [overview_of(changed)], "quiet": True},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
        ])
        self.assertEqual(errors, [])
        counts = values(res)[-1]
        self.assertGreater(counts.get("loop-rows", 0), 0)

    def test_loop_starting_to_run_updates_running_and_kpis(self):
        snap = make_snapshot()
        changed = make_snapshot()
        changed["workspaces"][1]["loops"][0]["running"] = True
        changed["workspaces"][1]["loops"][0]["running_sources"] = ["driver"]
        res, errors = run(board_ops(snap) + [
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "ingestOverview", "args": [overview_of(changed)], "quiet": True},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
            {"op": "text", "id": "running-list"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        counts = out[-2]
        self.assertGreater(counts.get("running-list", 0), 0)
        self.assertGreater(counts.get("kpis", 0), 0)
        self.assertIn("Gamma", out[-1])
        self.assertEqual(counts.get("workspace-filter", 0), 0)

    def test_changed_inbox_items_rewrite_their_lists(self):
        snap = make_snapshot()
        changed = make_snapshot()
        changed["workspaces"][0]["inbox"][0]["headline"] = "alpha now needs two humans"
        changed["workspaces"][0]["inbox"][1]["headline"] = "beta drifted again"
        res, errors = run(board_ops(snap) + [
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "ingestOverview", "args": [overview_of(changed)], "quiet": True},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
            {"op": "text", "id": "needs-list"},
            {"op": "text", "id": "notes-list"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        counts = out[-3]
        self.assertGreater(counts.get("needs-list", 0), 0)
        self.assertGreater(counts.get("notes-list", 0), 0)
        self.assertIn("alpha now needs two humans", out[-2])
        self.assertIn("beta drifted again", out[-1])
        self.assertEqual(counts.get("loop-rows", 0), 0)

    def test_read_flag_flip_rewrites_needs_list(self):
        snap = make_snapshot()
        changed = make_snapshot()
        changed["workspaces"][0]["inbox"][0]["read"] = True
        res, errors = run(board_ops(snap) + [
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "ingestOverview", "args": [overview_of(changed)], "quiet": True},
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "counts"},
        ])
        self.assertEqual(errors, [])
        counts = values(res)[-1]
        self.assertGreater(counts.get("kpis", 0), 0)  # unread count moved
        self.assertGreater(counts.get("drawer-inbox-list", 0), 0)

    def test_tab_switch_still_updates_the_board(self):
        res, errors = run(board_ops() + [
            {"op": "call", "fn": "renderAll", "quiet": True},
            {"op": "resetCounts"},
            {"op": "eval", "code": "state.tab = 'running'", "quiet": True},
            {"op": "call", "fn": "renderTabs", "quiet": True},
            {"op": "call", "fn": "renderBoard", "quiet": True},
            {"op": "counts"},
            {"op": "text", "id": "loop-rows"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        self.assertGreater(out[-2].get("tabs", 0), 0)
        self.assertGreater(out[-2].get("loop-rows", 0), 0)
        self.assertIn("Alpha", out[-1])
        self.assertNotIn("Beta", out[-1])


DETAIL_IDS = ["drawer-badge", "drawer-mission", "drawer-strip", "commit-list",
              "slice-list", "drawer-timeline"]
DETAIL_EXTRA_IDS = ["view-files", "view-graph", "view-timeline", "commits-section",
                    "slices-section", "fact-iter", "fact-verdict", "fact-activity",
                    "fact-sessions"]


def detail_ops(detail):
    return [
        {"op": "eval", "code": "state.detail = %s; state.sessions = state.detail.sessions" % json.dumps(detail),
         "quiet": True},
        {"op": "call", "fn": "renderDetail", "args": [detail], "quiet": True},
    ]


class DetailGuardTests(unittest.TestCase):
    def test_second_render_of_identical_detail_writes_nothing(self):
        # accept 5
        detail = make_detail()
        res, errors = run(board_ops() + [{"op": "resetCounts"}] + detail_ops(detail) + [
            {"op": "counts"}, {"op": "resetCounts"},
        ] + detail_ops(detail) + [{"op": "counts"}])
        self.assertEqual(errors, [])
        out = values(res)
        first, second = out[-5], out[-1]
        for ident in DETAIL_IDS:
            self.assertGreater(first.get(ident, 0), 0, "first render should fill #" + ident)
        stray = {k: v for k, v in second.items() if k not in LIVE_IDS}
        self.assertEqual(stray, {})
        self.assertTrue(set(DETAIL_EXTRA_IDS).isdisjoint(second))

    def rerender_with(self, changed):
        detail = make_detail()
        res, errors = run(board_ops() + detail_ops(detail) + [{"op": "resetCounts"}]
                          + detail_ops(changed) + [{"op": "counts"}])
        self.assertEqual(errors, [])
        return values(res)[-1]

    def test_new_commit_rewrites_only_the_commit_list(self):
        detail = make_detail()
        commits = detail["commits"] + [
            {"sha": "b" * 40, "short": "bbbbbbb", "slice": "s2", "subject": "slice(s2): second"}]
        counts = self.rerender_with(make_detail(commits=commits))
        self.assertGreater(counts.get("commit-list", 0), 0)
        for ident in ("drawer-badge", "drawer-mission", "drawer-strip", "slice-list", "drawer-timeline"):
            self.assertEqual(counts.get(ident, 0), 0, ident)

    def test_changed_mission_and_verdicts_rewrite_their_sections(self):
        counts = self.rerender_with(make_detail(
            mission="Do alpha, harder", final_verdict="SHIP",
            segments=[{"verdict_sequence": "ISS"}]))
        self.assertGreater(counts.get("drawer-mission", 0), 0)
        self.assertGreater(counts.get("drawer-strip", 0), 0)
        self.assertEqual(counts.get("commit-list", 0), 0)

    def test_changed_slices_and_timeline_rewrite_their_sections(self):
        detail = make_detail()
        slices = copy.deepcopy(detail["slices"])
        slices[1]["lifecycle"] = "shipped"
        timeline = detail["timeline"] + [
            {"iteration": 2, "role": "builder", "summary": "built s2", "duration_sec": 99}]
        counts = self.rerender_with(make_detail(slices=slices, timeline=timeline))
        self.assertGreater(counts.get("slice-list", 0), 0)
        self.assertGreater(counts.get("drawer-timeline", 0), 0)
        self.assertGreater(counts.get("view-timeline", 0), 0)
        self.assertEqual(counts.get("drawer-mission", 0), 0)

    def test_changed_slice_graph_rewrites_files_and_graph_views(self):
        detail = make_detail()
        slices = copy.deepcopy(detail["slices"])
        slices[1]["writes"] = ["b.py", "c.py"]
        counts = self.rerender_with(make_detail(slices=slices))
        self.assertGreater(counts.get("view-files", 0), 0)
        self.assertGreater(counts.get("view-graph", 0), 0)

    def test_session_list_only_rewrites_when_sessions_change(self):
        detail = make_detail()
        more = detail["sessions"] + [
            {"path": "/s/two.jsonl", "label": "two.jsonl", "size": 5, "status": "ok",
             "timestamp": "2026-05-30T09:00:00+00:00"}]
        res, errors = run(board_ops() + detail_ops(detail) + [
            {"op": "call", "fn": "renderSessionList", "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "renderSessionList", "quiet": True},
            {"op": "counts", "tag": "same"},
            {"op": "eval", "code": "state.sessions = %s" % json.dumps(more), "quiet": True},
            {"op": "resetCounts"},
            {"op": "call", "fn": "renderSessionList", "quiet": True},
            {"op": "counts", "tag": "changed"},
            {"op": "text", "id": "session-list", "tag": "text"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual({k: v for k, v in out["same"].items() if k not in LIVE_IDS}, {})
        self.assertGreater(out["changed"].get("session-list", 0), 0)
        self.assertIn("two", out["text"])


# ------------------------------ stream flow ------------------------------

def stream_boot(snapshot=None, detail=None, hash_="", no_es=False):
    snap = snapshot or make_snapshot()
    routes = {"/api/overview": overview_of(snap),
              "/api/loop": detail or make_detail(),
              "/api/loop/actions": {"state": "running", "fixes": [], "log": []}}
    boot = {"op": "boot", "hash": hash_}
    if no_es:
        boot["noEventSource"] = True
    return [boot, {"op": "routes", "routes": routes},
            {"op": "call", "fn": "init", "quiet": True}]


def snapshot_event(snap, id_=None):
    return {"op": "esEmit", "type": "snapshot", "data": snap,
            "id": id_ or "%s:%s" % (snap["epoch"], snap["seq"])}


class StreamFlowTests(unittest.TestCase):
    def test_snapshot_renders_the_board_and_never_polls_while_healthy(self):
        snap = make_snapshot()
        ops = stream_boot(snap) + [snapshot_event(snap)]
        for _ in range(8):  # 80 s of idle time with keepalive ticks
            ops += [{"op": "advance", "ms": 10000},
                    {"op": "esEmit", "type": "tick",
                     "data": {"epoch": "e1", "seq": 7, "updated_at": "2026-06-01T11:59:58+00:00"}}]
        ops += [{"op": "fetches"}, {"op": "esList"}, {"op": "text", "id": "loop-rows"},
                {"op": "eval", "code": "el('live').title"}]
        res, errors = run(ops)
        self.assertEqual(errors, [])
        out = values(res)
        self.assertEqual(overview_fetches(out[-4]), [])
        sources = out[-3]
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0]["url"].startswith("/api/stream"))
        self.assertIn("Alpha", out[-2])
        self.assertIn("Gamma", out[-2])
        self.assertIn("Push (SSE)", out[-1])

    def test_tick_only_touches_the_live_indicator(self):
        snap = make_snapshot()
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "resetCounts"},
            {"op": "esEmit", "type": "tick",
             "data": {"epoch": "e1", "seq": 7, "updated_at": "2026-06-01T12:00:03+00:00"}},
            {"op": "counts"},
            {"op": "eval", "code": "state.updatedAt"},
            {"op": "fetches"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        self.assertEqual({k: v for k, v in out[-3].items() if k not in LIVE_IDS}, {})
        self.assertEqual(out[-2], "2026-06-01T12:00:03+00:00")
        self.assertEqual(overview_fetches(out[-1]), [])

    def test_delta_updates_only_what_changed(self):
        snap = make_snapshot()
        gamma = make_card("gamma", running=True, running_sources=["driver"], iteration=3)
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "resetCounts"},
            {"op": "esEmit", "type": "delta",
             "data": make_delta([loop_change("upsert", ROOT_B, gamma)])},
            {"op": "counts"},
            {"op": "eval", "code": "state.byKey.get('/w/b::gamma').running"},
            {"op": "text", "id": "running-list"},
        ])
        self.assertEqual(errors, [])
        out = values(res)
        counts = out[-3]
        self.assertIs(out[-2], True)
        self.assertIn("Gamma", out[-1])
        self.assertGreater(counts.get("running-list", 0), 0)
        self.assertGreater(counts.get("loop-rows", 0), 0)
        self.assertEqual(counts.get("workspace-filter", 0), 0)
        self.assertEqual(counts.get("needs-list", 0), 0)
        self.assertEqual(counts.get("notes-list", 0), 0)

    def test_delta_gap_closes_the_stream_and_reopens_with_since(self):
        snap = make_snapshot()
        gap = make_delta([loop_change("upsert", ROOT_B, make_card("gamma"))], from_seq=9, seq=10)
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "esEmit", "type": "delta", "data": gap},
            {"op": "esList"},
        ])
        self.assertEqual(errors, [])
        sources = values(res)[-1]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["readyState"], 2)
        self.assertEqual(since_of(sources[1]["url"]), "e1:7")

    def test_foreign_epoch_delta_reopens_with_the_known_position(self):
        snap = make_snapshot()
        foreign = make_delta([loop_change("upsert", ROOT_B, make_card("gamma"))], epoch="e2")
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "esEmit", "type": "delta", "data": foreign},
            {"op": "esList"},
        ])
        self.assertEqual(errors, [])
        sources = values(res)[-1]
        self.assertEqual(len(sources), 2)
        self.assertEqual(since_of(sources[1]["url"]), "e1:7")

    def test_stream_unavailable_polls_every_5s_until_a_snapshot_arrives(self):
        # accept 6
        snap = make_snapshot()
        ops = stream_boot(snap) + [
            {"op": "esError", "closed": True},
            {"op": "fetches", "clear": True, "tag": "failure"},
            {"op": "eval", "code": "el('live').title", "tag": "title_poll"},
            {"op": "advance", "ms": 5000}, {"op": "fetches", "clear": True, "tag": "t5"},
            {"op": "advance", "ms": 5000}, {"op": "fetches", "clear": True, "tag": "t10"},
            {"op": "advance", "ms": 5000}, {"op": "fetches", "clear": True, "tag": "t15"},
            # the stream is retried every 30 s of fallback
            {"op": "advance", "ms": 15000}, {"op": "fetches", "clear": True},
            {"op": "esList", "tag": "sources"},
            dict(snapshot_event(snap), tag="snapshot"),
            {"op": "fetches", "clear": True},
            {"op": "advance", "ms": 20000},
            {"op": "fetches", "tag": "after_snapshot"},
            {"op": "eval", "code": "el('live').title", "tag": "title_push"},
        ]
        res, errors = run(ops)
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual(len(overview_fetches(out["failure"])), 1)  # immediately on the failure
        self.assertIn("Polling every 5 s (stream unavailable)", out["title_poll"])
        for tag in ("t5", "t10", "t15"):
            self.assertEqual(len(overview_fetches(out[tag])), 1, tag)
        self.assertGreaterEqual(len(out["sources"]), 2, "stream retried after fallback")
        self.assertEqual(overview_fetches(out["after_snapshot"]), [],
                         "polling stops after the first stream event")
        self.assertIn("Push (SSE)", out["title_push"])

    def test_no_event_source_polls(self):
        snap = make_snapshot()
        res, errors = run(stream_boot(snap, no_es=True) + [
            {"op": "fetches", "clear": True, "tag": "first"},
            {"op": "advance", "ms": 10000},
            {"op": "fetches", "tag": "later"},
            {"op": "esList", "tag": "sources"},
            {"op": "text", "id": "loop-rows", "tag": "rows"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual(len(overview_fetches(out["first"])), 1)  # at once, not after 5 s
        self.assertEqual(len(overview_fetches(out["later"])), 2)
        self.assertEqual(out["sources"], [])
        self.assertIn("Alpha", out["rows"])

    def test_silent_stream_falls_back_after_45s(self):
        snap = make_snapshot()
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "advance", "ms": 40000},
            {"op": "fetches", "tag": "quiet", "clear": True},
            {"op": "advance", "ms": 6000},
            {"op": "fetches", "tag": "silent"},
            {"op": "eval", "code": "el('live').title", "tag": "title"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual(overview_fetches(out["quiet"]), [])
        self.assertGreaterEqual(len(overview_fetches(out["silent"])), 1)
        self.assertIn("Polling every 5 s", out["title"])

    def test_hidden_tab_closes_the_stream_and_visible_reopens_with_since(self):
        snap = make_snapshot()
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "fetches", "clear": True},
            {"op": "setHidden", "value": True},
            {"op": "esList", "tag": "hidden"},
            {"op": "advance", "ms": 120000},
            {"op": "fetches", "tag": "while_hidden"},
            {"op": "setHidden", "value": False},
            {"op": "esList", "tag": "visible"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual([s["readyState"] for s in out["hidden"]], [2])
        self.assertEqual(out["while_hidden"], [])
        self.assertEqual(len(out["visible"]), 2)
        self.assertEqual(since_of(out["visible"][1]["url"]), "e1:7")

    def test_detail_is_refetched_only_for_the_active_loop(self):
        snap = make_snapshot()
        alpha2 = make_card("alpha", running=True, running_sources=["driver"], iteration=3)
        beta2 = make_card("beta", final_verdict="SHIP", iteration=9)
        key = ROOT_A + "::alpha"
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "call", "fn": "openDrawer", "args": [key], "quiet": True},
            {"op": "flush"},
            {"op": "fetches", "clear": True},
            {"op": "esEmit", "type": "tick",
             "data": {"epoch": "e1", "seq": 7, "updated_at": "2026-06-01T12:00:03+00:00"}},
            {"op": "fetches", "clear": True, "tag": "tick"},
            {"op": "esEmit", "type": "delta",
             "data": make_delta([loop_change("upsert", ROOT_A, beta2)])},
            {"op": "fetches", "clear": True, "tag": "other"},
            {"op": "esEmit", "type": "delta",
             "data": make_delta([loop_change("upsert", ROOT_A, alpha2)], from_seq=8, seq=9)},
            {"op": "fetches", "clear": True, "tag": "active"},
            snapshot_event(make_snapshot(seq=20)),
            {"op": "fetches", "tag": "snapshot"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        after_tick, after_other = out["tick"], out["other"]
        after_active, after_snapshot = out["active"], out["snapshot"]
        self.assertEqual(urls(after_tick, "/api/loop"), [])
        self.assertEqual(urls(after_other, "/api/loop"), [])
        self.assertEqual(len(urls(after_active, "/api/loop")), 1)
        self.assertEqual(len(urls(after_snapshot, "/api/loop")), 1)
        for fetched in (after_tick, after_other, after_active, after_snapshot):
            self.assertEqual(overview_fetches(fetched), [])
        self.assertEqual(urls(after_tick, "/api/loop/actions"), [])
        self.assertEqual(urls(after_other, "/api/loop/actions"), [])

    def test_open_drawer_detail_render_is_stable_across_ticks(self):
        snap = make_snapshot()
        key = ROOT_A + "::alpha"
        res, errors = run(stream_boot(snap) + [
            snapshot_event(snap),
            {"op": "call", "fn": "openDrawer", "args": [key], "quiet": True},
            {"op": "flush"},
            {"op": "resetCounts"},
            {"op": "esEmit", "type": "tick",
             "data": {"epoch": "e1", "seq": 7, "updated_at": "2026-06-01T12:00:03+00:00"}},
            {"op": "advance", "ms": 5000},
            {"op": "counts", "tag": "counts"},
            {"op": "text", "id": "drawer-mission", "tag": "mission"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertEqual({k: v for k, v in out["counts"].items() if k not in LIVE_IDS}, {})
        self.assertEqual(out["mission"], "Do alpha")

    def test_marking_an_item_read_survives_later_unrelated_deltas(self):
        snap = make_snapshot()
        gamma = make_card("gamma", iteration=4)
        res, errors = run(stream_boot(snap) + [
            {"op": "routes", "routes": {"/api/inbox/read": {}, "/api/inbox/unread": {}}},
            snapshot_event(snap),
            {"op": "text", "id": "needs-list", "tag": "before"},
            {"op": "click", "selector": ".attn-row .btn"},
            {"op": "text", "id": "needs-list", "tag": "marked"},
            {"op": "esEmit", "type": "delta",
             "data": make_delta([loop_change("upsert", ROOT_B, gamma)])},
            {"op": "text", "id": "needs-list", "tag": "after_delta"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertIn("alpha needs a human", out["before"])
        self.assertNotIn("alpha needs a human", out["marked"])  # read items leave the list
        self.assertNotIn("alpha needs a human", out["after_delta"])

    def test_hash_target_opens_on_the_first_snapshot(self):
        snap = make_snapshot()
        res, errors = run(stream_boot(snap, hash_="#root=%2Fw%2Fa&loop=alpha") + [
            snapshot_event(snap),
            {"op": "flush"},
            {"op": "hiddenOf", "id": "drawer", "tag": "hidden"},
            {"op": "eval", "code": "state.activeLoop", "tag": "active"},
            {"op": "fetches", "tag": "fetches"},
        ])
        self.assertEqual(errors, [])
        out = tagged(res)
        self.assertIs(out["hidden"], False)
        self.assertEqual(out["active"], ROOT_A + "::alpha")
        self.assertEqual(len(urls(out["fetches"], "/api/loop")), 1)


if __name__ == "__main__":
    unittest.main()

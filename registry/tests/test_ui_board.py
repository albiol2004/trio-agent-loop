"""The dashboard's main board as keyed Preact components (GOAL DoD6, iter-3
blocking 2: a clock tick must not rebuild a section).

ui_board_cases.mjs mounts dashboard/ui/board.js on the real index.html shell
in headless chromium (through ui_browser.cjs: the vendored preact, signals and
htm, a real DOM, real MutationObservers; fetch and EventSource stubbed, the
store clock driven by hand). These tests assert on the rendered DOM and the
mutation counts it records, never on source text. The class SKIPS, with the
reason, when node, playwright-core or chromium is missing; it never passes
vacuously.
"""
from __future__ import annotations

import unittest

from . import ui_browser_support as browser


@unittest.skipIf(browser.skip_reason() is not None, browser.skip_reason() or "")
class BoardComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = browser.run_cases("ui_board_cases.mjs")

    def case(self, name):
        out = (self.output["results"] or {}).get(name)
        self.assertIsNotNone(out, f"case {name} did not run: {self.output}")
        self.assertNotIn("error", out, out.get("error"))
        return out["value"]

    def test_harness_loaded_everything_without_errors(self):
        self.assertEqual(self.output["errors"], [])
        self.assertEqual(self.output["console"], [])
        self.assertEqual(len(self.output["results"]), 12)

    # ---- accept: 4-loop snapshot -> rows, needs, running, kpis ----

    def test_four_loop_snapshot_renders_rows_and_sections(self):
        v = self.case("initial_render")
        # before data: the shell's skeletons and the loading headline
        self.assertEqual(v["before"], {
            "verdict": "Loading loops", "kpis": 4, "needs": 1, "running": 1, "rows": 1})
        d = v["dump"]
        # one tr.loop-row per loop with its id, ordered running first, then
        # newest activity first
        self.assertEqual([r["id"] for r in d["rows"]],
                         ["L-alpha", "L-gamma", "L-beta", "L-delta"])
        self.assertEqual([r["key"] for r in d["rows"]], [
            "/w/one::alpha-build", "/w/two::gamma-fix", "/w/one::beta-docs",
            "/w/two::loop-archive/delta-old"])
        alpha, gamma, beta, delta = d["rows"]
        self.assertEqual(alpha, {
            "id": "L-alpha", "key": "/w/one::alpha-build", "cls": "loop-row",
            "title": "Alpha build", "titleAttr": "Build alpha. Then ship.",
            "sub": "alpha-build", "flags": [], "ws": "one", "wsTitle": "/w/one",
            "badge": "●Running", "badgeClass": "badge badge-live", "phase": "lead",
            "iteration": "2 of 5", "hist": "II", "activity": "30s ago",
            "activityDatetime": "2026-06-01T11:59:30.000Z"})
        self.assertEqual(gamma["flags"], ["2 unread items"])
        self.assertEqual((gamma["badge"], gamma["badgeClass"]), ("!Needs human", "badge badge-warning"))
        self.assertEqual((beta["badge"], beta["hist"], beta["activity"]), ("✓Shipped", "IS", "3h ago"))
        self.assertEqual(delta["cls"], "loop-row is-archived")
        self.assertEqual(delta["flags"], ["1 unread item", "Archived"])
        self.assertEqual((delta["iteration"], delta["hist"], delta["activity"]), ("—", "—", "May 23"))
        # headline, KPI tiles
        self.assertEqual(d["verdict"], "1 loop needs you; 1 loop running.")
        self.assertEqual(
            d["verdictSub"],
            "Most recent activity: Alpha build (one), 30s ago. "
            "Covers 2 workspaces with loops (2 workspaces scanned).")
        self.assertEqual(
            [(k["tag"], k["cls"], k["href"], k["eyebrow"], k["value"], k["context"])
             for k in d["kpis"]],
            [("a", "kpi kpi-warning", "#needs", "Needs you", "1", "2 unread items"),
             ("a", "kpi kpi-live", "#running", "Running now", "1", "lead"),
             ("div", "kpi", None, "Shipped, last 7 days", "1", "1 shipped loop in total"),
             ("a", "kpi", "#loops", "Loops tracked", "4", "in 2 workspaces")])
        self.assertTrue(d["brokerNote"]["hidden"])
        self.assertTrue(d["partial"]["hidden"])
        # Needs you: one row per loop, the most severe item leads
        self.assertEqual(d["needsCount"], "1")
        self.assertEqual(d["toggle"], {"hidden": False, "text": "Show read (1)", "expanded": "false"})
        self.assertEqual(d["needs"], [{
            "key": "/w/two::gamma-fix", "cls": "attn-row", "badge": "!Action",
            "open": "Gamma fix", "openLabel": "Open Gamma fix: Blocked on review",
            "where": "two / gamma-fix", "reason": "Blocked on review + 1 more item",
            "more": " + 1 more item", "detail": "Waiting for a human to approve the plan.",
            "caption": "10m ago", "mark": "Mark all read",
            "markLabel": "Mark all read: Gamma fix"}])
        # Running now
        self.assertEqual(d["runningCount"], "1")
        self.assertEqual(d["runCards"], [{
            "key": "/w/one::alpha-build", "badge": "●Running", "lastWrite": "last write 30s ago",
            "title": "Alpha build", "where": "one / alpha-build",
            "meta": ["Iteration 2 of 5", "lead"], "meter": "Iteration 2 of 5", "fill": "40%",
            "live": "Live via driver"}])
        self.assertIsNone(d["runEmpty"])
        # workspace filter, tabs, review notes
        self.assertEqual([(o["value"], o["text"], o["selected"]) for o in d["wsOptions"]], [
            ("", "All workspaces", True), ("/w/one", "one (2)", False), ("/w/two", "two (2)", False)])
        self.assertEqual([(t["label"], t["count"], t["pressed"]) for t in d["tabs"]], [
            ("Running", "1", "false"), ("Attention", "2", "false"),
            ("All", "4", "true"), ("Archived", "1", "false")])
        self.assertEqual(d["sortAria"], ["none", "none", "descending"])
        self.assertEqual((d["notes"]["hidden"], d["notes"]["count"]), (False, "1"))
        self.assertEqual(
            d["notes"]["sub"],
            "Drift, overlap, repair and unconfirmed-stop notes on 1 loop that are not running")
        self.assertEqual([r["key"] for r in d["notes"]["rows"]], ["/w/two::loop-archive/delta-old"])
        self.assertEqual(d["notes"]["rows"][0]["badge"], "•Note")
        # unmount empties what it rendered
        self.assertEqual(v["afterUnmount"], {"rows": 0, "kpis": 0})

    def test_broker_and_unreadable_workspace_banners(self):
        v = self.case("banners")
        self.assertEqual(v["brokerNote"], {"hidden": False, "text":
            "The Omnigent broker did not answer; loops driven only through broker "
            "sessions may be running but show as not running."})
        self.assertEqual(v["partial"], {"hidden": False, "text":
            "Could not read two. Other workspaces are shown."})

    def test_attention_split_between_needs_and_notes(self):
        v = self.case("attention_ordering_and_unread_flags")
        self.assertEqual(v["needsBefore"], ["/w/two::gamma-fix"])
        self.assertEqual([r["key"] for r in v["notesBefore"]["rows"]],
                         ["/w/two::loop-archive/delta-old"])
        # a low-severity note on a RUNNING loop is a "needs you" row, after
        # the high-severity one
        self.assertEqual(v["needsAfter"], [
            {"key": "/w/two::gamma-fix", "badge": "!Action"},
            {"key": "/w/one::alpha-build", "badge": "•Note"}])
        self.assertEqual(dict((i, f) for i, f in v["rowFlags"])["L-alpha"], ["1 unread item"])

    # ---- accept: a delta touches only the changed loop ----

    def test_delta_re_renders_only_the_changed_loops_dom(self):
        v = self.case("delta_isolation")
        # alpha (running): its row and its running card, nothing else
        self.assertEqual(sorted(v["alpha"]), ["card:/w/one::alpha-build", "row:L-alpha"])
        for zone, counts in v["alpha"].items():
            self.assertEqual(counts["childList"], 0, zone)
            self.assertGreaterEqual(counts["attributes"] + counts["characterData"], 1, zone)
        # gamma (listed under Needs you): inside its own row only (the
        # attention row shows nothing that changed)
        self.assertEqual(sorted(v["gamma"]), ["row:L-gamma"])
        self.assertEqual(v["gammaBadge"], "○Iterating")
        self.assertEqual(v["alphaBadge"], "●Running")
        # an upsert equal to the stored loop notifies nobody
        self.assertEqual(v["unchanged"], {})

    def test_added_and_removed_loops_touch_their_row_and_the_counts(self):
        v = self.case("loop_added_and_removed")
        self.assertEqual(v["added"]["ids"], ["L-alpha", "L-gamma", "L-omega", "L-beta", "L-delta"])
        self.assertEqual(v["added"]["kpi"], "5")
        self.assertEqual(v["removed"]["ids"], ["L-alpha", "L-gamma", "L-beta", "L-delta"])
        for step in ("added", "removed"):
            zones = v[step]["zones"]
            # no existing row, card or attention row is touched
            self.assertEqual(
                sorted(z for z in zones if z.startswith(("row:", "card:", "attn:"))), [], step)
            self.assertEqual(zones["section:loop-rows"]["childList"], 1, step)
        # the tiles and tab / filter counts follow with text updates only
        for zone in ("section:kpis", "section:tabs", "section:workspace-filter"):
            self.assertEqual(v["added"]["zones"][zone]["childList"], 0, zone)
            self.assertEqual(v["added"]["zones"][zone]["characterData"], 1, zone)

    # ---- accept: a clock tick is characterData only ----

    def test_clock_tick_only_moves_relative_time_text(self):
        v = self.case("clock_tick_is_character_data_only")
        self.assertEqual(v["reltimeCount"], 8)
        for step in v["steps"]:
            self.assertEqual(step["childList"], 0, step)
            self.assertEqual(step["attributes"], 0, step)
            self.assertEqual(step["outsideReltime"], 0, step)
        # +70 s past the minute boundary: labels moved; an unchanged clock
        # writes nothing at all
        self.assertEqual(v["steps"][1]["dt"], 70000)
        self.assertGreater(v["steps"][1]["records"], 0)
        self.assertEqual(v["steps"][2]["records"], 0)
        self.assertNotEqual(v["agoBefore"], v["agoAfter"])
        self.assertEqual(v["agoBefore"][:3], ["30s ago", "10m ago", "last write 30s ago"])
        self.assertIn("1h ago", v["agoAfter"])
        self.assertIn("last write 1h ago", v["agoAfter"])
        self.assertEqual(dict(v["activityCells"]), {
            "L-alpha": "1h ago", "L-gamma": "1h ago", "L-beta": "4h ago", "L-delta": "May 23"})
        # the touched zones are the loops' times, the headline's time and the
        # attention captions, never a whole section
        for step in v["steps"]:
            for zone in step["zones"]:
                self.assertTrue(
                    zone.startswith(("row:", "card:", "attn:")) or zone == "section:verdict-sub",
                    zone)

    def test_a_silent_feed_turns_the_indicator_stale(self):
        v = self.case("stale_feed_flags_the_indicator")
        self.assertEqual((v["fresh"]["text"], v["fresh"]["cls"]), ("Live · 11:59:58", "live live-ok"))
        self.assertEqual(v["stillFresh"]["cls"], "live live-ok")
        self.assertEqual((v["stale"]["text"], v["stale"]["cls"]),
                         ("Stale · data from 11:59:58", "live live-stale"))
        self.assertEqual((v["recovered"]["text"], v["recovered"]["cls"]),
                         ("Live · 12:00:39", "live live-ok"))
        # the data time is the one [data-reltime] node of the indicator
        self.assertEqual(v["fresh"]["reltimeText"], "11:59:58")

    # ---- accept: focus, caret and filters survive updates ----

    def test_search_focus_value_and_caret_survive_delta_and_tick(self):
        v = self.case("focus_and_caret_survive_updates")
        self.assertEqual(v["typed"], {"query": "alp", "rows": ["L-alpha"]})
        self.assertTrue(v["activeIsSearch"])
        self.assertEqual(v["value"], "alp")
        self.assertEqual((v["selectionStart"], v["selectionEnd"]), (1, 2))
        self.assertEqual(v["rows"], ["L-alpha"])
        self.assertEqual(v["searchMutations"], 0)
        # the tab counts follow the filter
        self.assertEqual(v["tabCounts"], ["1", "0", "1", "0"])

    # ---- accept: stable order on ties ----

    def test_equal_sort_keys_order_by_name_then_id(self):
        v = self.case("equal_sort_keys_order_by_name_then_id")
        # aaa/L-5, then the two "same" loops by id: L-1 before L-9, though L-9
        # comes first in the server order
        self.assertEqual(v["initial"], ["L-5", "L-1", "L-9"])
        self.assertEqual(v["removed"], ["L-5", "L-9"])
        self.assertEqual(v["readded"], ["L-5", "L-1", "L-9"])
        self.assertEqual(v["readdedOther"], ["L-5", "L-1", "L-9"])
        # the same tie-break under the other sort keys
        self.assertEqual(v["byWorkspace"], ["L-5", "L-9", "L-1"])
        self.assertEqual(v["byLoop"], ["L-5", "L-1", "L-9"])

    # ---- accept: live indicator ----

    def test_live_indicator_text_title_and_error_banner(self):
        v = self.case("live_indicator_states")
        s = v["states"]
        self.assertEqual((s["connecting"]["text"], s["connecting"]["cls"]),
                         ("Connecting", "live live-connecting"))
        self.assertEqual((s["live"]["text"], s["live"]["title"]), ("Live · 11:59:58", "Push (SSE)"))
        self.assertEqual((s["reconnecting"]["text"], s["reconnecting"]["cls"]),
                         ("Reconnecting", "live live-connecting"))
        self.assertEqual((s["poll"]["text"], s["poll"]["title"]), (
            "Live · 11:59:58", "Polling every 5 s (stream unavailable)"))
        self.assertEqual((s["offline"]["text"], s["offline"]["cls"]),
                         ("Offline · data from 11:59:58", "live live-offline"))
        self.assertEqual(s["offline"]["banner"], {"hidden": False, "text":
            "Lost contact with the dashboard server (HTTP 503). Showing the last data "
            "received; retrying every 5 seconds."})
        self.assertTrue(s["back"]["banner"]["hidden"])
        self.assertEqual(v["retried"], 1)
        self.assertEqual(s["neverLoaded"]["text"], "Offline")
        self.assertEqual(s["neverLoaded"]["banner"], {"hidden": False, "text":
            "Cannot reach the dashboard server (boom). Retrying every 5 seconds."})
        self.assertEqual(s["neverLoaded"]["verdict"], "Loading loops")

    # ---- interactions ----

    def test_rows_cards_and_attention_rows_open_the_loop(self):
        v = self.case("interactions")
        self.assertEqual(v["afterClick"], {"openKey": "/w/one::alpha-build", "active": ["L-alpha"]})
        self.assertEqual(v["afterEnter"]["openKey"], "/w/two::gamma-fix")
        self.assertEqual(v["afterEnter"]["active"], ["L-gamma"])
        self.assertEqual(v["afterAttnOpen"], "/w/two::gamma-fix")
        self.assertEqual(v["afterCardOpen"], "/w/one::alpha-build")
        self.assertEqual(v["closed"]["active"], 0)

    def test_drawer_inbox_lists_the_open_loops_items(self):
        v = self.case("interactions")["afterEnter"]
        self.assertFalse(v["sectionHidden"])
        self.assertEqual(v["drawerRows"], [
            {"cls": "attn-row attn-row-compact", "badge": "!Action", "reason": "Blocked on review",
             "detail": "Waiting for a human to approve the plan.", "mark": "Mark read",
             "label": "Mark read: Blocked on review"},
            {"cls": "attn-row attn-row-compact", "badge": "▲Check", "reason": "Second thing",
             "detail": None, "mark": "Mark read", "label": "Mark read: Second thing"}])
        closed = self.case("interactions")["closed"]
        self.assertEqual((closed["drawerRows"], closed["sectionHidden"]), (0, True))

    def test_mark_read_posts_and_the_read_toggle_reveals_read_items(self):
        v = self.case("interactions")
        self.assertEqual(v["markCalls"], [
            {"url": "/api/inbox/read", "body": {"ids": ["i1", "i2"], "root": "/w/two"}}])
        self.assertEqual(v["afterMark"]["count"], "")
        self.assertTrue(v["afterMark"]["needs"].startswith("✓Nothing needs you."))
        self.assertEqual(v["afterMark"]["toggle"]["text"], "Show read (3)")
        self.assertEqual(v["afterShow"]["toggle"], {
            "hidden": False, "text": "Hide read (3)", "expanded": "true"})
        self.assertEqual(v["afterShow"]["needs"], [
            {"key": "/w/two::gamma-fix", "cls": "attn-row is-read", "mark": "Mark unread"},
            {"key": "/w/one::beta-docs", "cls": "attn-row is-read", "mark": "Mark unread"}])
        self.assertEqual(v["afterShow"]["count"], "")

    def test_tabs_workspace_filter_sort_and_empty_state(self):
        v = self.case("interactions")
        self.assertEqual(v["running"], {
            "tab": "running", "rows": ["L-alpha"], "pressed": ["true", "false", "false", "false"]})
        self.assertEqual(v["workspace"], {
            "value": "/w/two", "rows": ["L-gamma", "L-delta"], "selected": "/w/two",
            "tabCounts": ["0", "1", "2", "1"]})
        self.assertEqual(v["sortAsc"], {
            "sort": {"key": "loop", "dir": "asc"}, "aria": ["ascending", "none", "none"],
            "rows": ["L-alpha", "L-beta", "L-delta", "L-gamma"]})
        self.assertEqual(v["sortDesc"]["aria"], ["descending", "none", "none"])
        self.assertEqual(v["sortDesc"]["rows"], ["L-gamma", "L-delta", "L-beta", "L-alpha"])
        self.assertEqual(v["sortActivity"], {
            "sort": {"key": "activity", "dir": "desc"}, "aria": ["none", "none", "descending"]})
        self.assertEqual(v["noMatch"], [{"cls": "row-empty", "text": "○No loops match these filters."}])

    def test_a_failed_mark_read_shows_the_banner_until_the_next_good_update(self):
        v = self.case("mark_read_failure_shows_the_banner")
        self.assertEqual(v["failed"], {
            "hidden": False, "text": "Could not update the attention list: nope"})
        self.assertEqual(v["needsStill"], 1)
        self.assertTrue(v["cleared"]["hidden"])


if __name__ == "__main__":
    unittest.main()

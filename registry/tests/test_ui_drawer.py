"""The loop drawer as Preact components, in a real browser (GOAL DoD6).

ui_drawer_cases.mjs runs in headless chromium through ui_browser.cjs (no
server, every request answered by page.route) with the real vendored preact,
@preact/signals and htm, the real signals store, dashboard/index.html's drawer
shell, a stubbed /api/loop and a hand-driven clock; these tests assert on the
values it records (rendered DOM, MutationObserver records, focus, hook
calls). The class SKIPS, with the reason, when node, playwright-core or
chromium is missing: it never passes vacuously.
"""
from __future__ import annotations

import unittest

from . import ui_browser_support as browser

CASE_COUNT = 17


@unittest.skipIf(browser.skip_reason() is not None, browser.skip_reason() or "")
class DrawerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = browser.run_cases("ui_drawer_cases.mjs")

    def case(self, name):
        out = (self.output["results"] or {}).get(name)
        self.assertIsNotNone(out, f"case {name} did not run: {self.output}")
        self.assertNotIn("error", out, out.get("error"))
        return out["value"]

    def test_harness_loaded_everything_without_errors(self):
        self.assertEqual(self.output["errors"], [])
        self.assertEqual(self.output["console"], [])
        self.assertEqual(len(self.output["results"]), CASE_COUNT)

    # -- accept: store.openLoop(k) with a fake /api/loop detail -----------------
    def test_open_renders_the_detail_and_focuses_the_close_button(self):
        v = self.case("open_renders_the_detail")
        self.assertEqual(v["before"], {"hidden": True, "scrimHidden": True})
        self.assertFalse(v["drawerHidden"])
        self.assertFalse(v["scrimHidden"])
        self.assertEqual(v["inert"], [True, True])
        self.assertEqual(v["activeId"], "drawer-close")
        self.assertFalse(v["originFocused"])
        # header
        self.assertEqual(v["name"], "Alpha")
        self.assertEqual(v["where"], "a / alpha")
        self.assertTrue(v["whereTitle"].endswith("/alpha"))
        self.assertEqual(v["mission"], "Ship the drawer as Preact components.")
        self.assertEqual(v["missionTitle"], v["mission"])
        self.assertEqual(v["badgeClass"], "status-tags")
        self.assertIn("Shipped", v["badge"])  # final verdict derived from the segments
        # facts
        self.assertEqual(v["factIter"], "2 of 5")
        self.assertEqual(v["factVerdict"], "—")
        self.assertEqual(v["factVerdictClass"], "")
        self.assertEqual(v["factActivity"], "4m ago")
        self.assertTrue(v["factActivityIsTime"])
        self.assertEqual(v["factSessions"], "3")
        # verdict strip
        self.assertEqual(v["stripCells"], ["I", "S"])
        self.assertIn("S shipped · I iterate · B blocked · H needs human, oldest first", v["strip"])
        # commits: the first 8 of 10 and the show-all button
        self.assertFalse(v["commitsHidden"])
        self.assertEqual(len(v["commitRows"]), 8)
        self.assertEqual(v["commitRows"][0],
                         {"sha": "1010101", "slice": "ui-drawer", "subject": "change number 10"})
        self.assertEqual(v["moreButton"], "Show all 10 commits")
        # slices
        self.assertFalse(v["slicesHidden"])
        board, drawer = v["sliceRows"]
        self.assertEqual(board["text"], "ui-boardShippedabcdef1SHIP")
        self.assertEqual(board["chip"], "lifecycle-chip lifecycle-shipped")
        self.assertEqual(board["verdict"], "SHIP")
        self.assertIsNone(board.get("repo"))  # "." = the mailbox repo, shown as nothing
        self.assertEqual((drawer["repo"], drawer["fault"]), ("web", "F-1"))
        self.assertEqual(drawer["chip"], "lifecycle-chip lifecycle-building")
        # overview timeline: grouped per iteration, lifecycle + slice counts
        self.assertEqual(v["headers"], ["Iteration 1Shipped1 shipped", "Iteration 2Building1 building"])
        self.assertEqual(v["timelineRows"], [
            {"role": "lead", "dur": "45s", "text": "planned the board"},
            {"role": "evaluator", "dur": "2m", "text": "SHIP board ui-board all good"},
            {"role": "builder", "dur": "—", "text": "ui-drawer wrote the drawer"},
        ])
        # tabs and views
        self.assertEqual([t["text"] for t in v["tabs"]],
                         ["Overview", "Timeline", "Files", "Graph", "Transcripts"])
        self.assertEqual([t["pressed"] for t in v["tabs"]], ["true", "false", "false", "false", "false"])
        self.assertEqual(v["views"], {"overview": False, "timeline": True, "files": True,
                                      "graph": True, "transcripts": True})
        self.assertFalse(v["controlsHidden"])
        # legacy hooks: one open, one detail, nothing else
        self.assertTrue(v["openKey"].endswith("::alpha"))
        self.assertEqual(v["open"], ["::alpha"])
        self.assertEqual(v["detail"], [{"mission": "Ship the drawer as Preact components.",
                                         "loop": "::alpha"}])
        self.assertEqual((v["close"], v["tabCalls"]), (0, []))

    def test_loading_state_until_the_detail_arrives(self):
        v = self.case("loading_state_until_the_detail_arrives")
        loading = v["loading"]
        self.assertEqual(loading["badge"], "LOADING")
        self.assertEqual(loading["timeline"], "Loading…")
        self.assertEqual(loading["name"], "Alpha")  # the board card is enough for the title
        self.assertEqual((loading["mission"], loading["factIter"], loading["factActivity"]), ("", "—", "—"))
        self.assertTrue(loading["commitsHidden"])
        self.assertEqual((loading["openCalls"], loading["detailCalls"]), (1, 0))
        self.assertEqual(v["loaded"], {"badge": "✓Shipped", "factIter": "2 of 5", "detailCalls": 1})

    def test_a_failed_detail_fetch_says_so_in_the_timeline(self):
        v = self.case("detail_failure_is_shown_in_the_timeline")
        self.assertEqual(v["timeline"], "Failed to load detail: HTTP 500")
        self.assertEqual((v["badge"], v["detailCalls"]), ("LOADING", 0))

    # -- accept: equal refresh + 70 s of clock write nothing but reltime text -----
    def test_equal_detail_and_clock_ticks_write_only_reltime_text(self):
        v = self.case("equal_detail_and_clock_ticks_write_only_reltime_text")
        self.assertTrue(v["sameDetail"], "the store reassigned a deep-equal detail")
        self.assertEqual(v["childList"], 0)
        self.assertEqual(v["attributes"], [])
        self.assertEqual((v["before"], v["after"]), ("4m ago", "6m ago"))
        self.assertGreaterEqual(v["characterData"], 2)
        self.assertEqual(v["characterDataOutsideReltime"], 0)
        self.assertEqual(v["detailCalls"], 1)  # nothing re-notified the legacy island

    def test_an_unrelated_loop_changing_rewrites_nothing_in_the_drawer(self):
        v = self.case("a_changed_unrelated_loop_card_rewrites_nothing_in_the_drawer")
        self.assertEqual(v, {"childList": 0, "attributes": 0, "characterData": 0})

    def test_the_badge_follows_the_live_loop_without_a_refetch(self):
        v = self.case("live_running_state_reaches_the_badge_without_a_refetch")
        self.assertEqual((v["before"], v["after"]), ("✓Shipped", "●Running"))
        self.assertEqual(v["badgeClass"], "badge badge-live")
        self.assertEqual(v["detailCalls"], 1)

    # -- accept: show-all survives a changed detail ---------------------------------
    def test_show_all_commits_survives_a_changed_detail(self):
        v = self.case("show_all_commits_survives_a_changed_detail")
        self.assertEqual(v["collapsed"], 8)
        self.assertEqual(v["afterToggle"], {"rows": 10, "expanded": True, "button": "Show fewer commits"})
        self.assertTrue(v["keptNode"], "expanding re-created the rows that were already shown")
        # the changed detail: one more commit on top, still expanded, rows kept in place
        self.assertEqual(v["detailCalls"], 2)
        self.assertEqual(v["afterRows"], 11)
        self.assertEqual(v["first"], "1111111")
        self.assertTrue(v["expanded"])
        self.assertEqual(v["button"], "Show fewer commits")
        self.assertTrue(v["sameNodeForOldRow"], "the list was rebuilt instead of patched in place")
        self.assertEqual((v["collapsedAgain"], v["buttonAgain"]), (8, "Show all 11 commits"))

    def test_few_commits_have_no_button_and_no_slices_hide_the_section(self):
        v = self.case("few_commits_and_no_slices_hide_their_sections")
        self.assertEqual(v, {"commitsHidden": False, "rows": 1, "buttons": 0,
                             "slicesHidden": True, "timeline": "No log entries yet."})

    # -- accept: Escape closes and restores focus; not while a dialog is open ----------
    def test_escape_closes_restores_focus_and_yields_to_the_confirm_dialog(self):
        v = self.case("escape_closes_and_restores_focus_unless_a_dialog_is_open")
        self.assertEqual(v["opened"], {"hidden": False, "active": "drawer-close"})
        self.assertEqual(v["withDialog"], {"hidden": False, "close": 0,
                                           "openKey": v["withDialog"]["openKey"]})
        self.assertTrue(v["withDialog"]["openKey"].endswith("::alpha"))
        closed = v["closed"]
        self.assertTrue(closed["drawerHidden"])
        self.assertTrue(closed["scrimHidden"])
        self.assertIsNone(closed["openKey"])
        self.assertEqual(closed["close"], 1)
        self.assertTrue(closed["restored"], "focus did not return to the element focused before opening")
        self.assertFalse(closed["mainInert"])
        self.assertFalse(closed["appbarInert"])
        self.assertEqual((closed["badge"], closed["name"]), ("—", "—"))
        self.assertEqual(v["closeAfterIdleEscape"], 1)

    def test_close_button_and_scrim_close_the_drawer(self):
        v = self.case("close_button_and_scrim_close_the_drawer")
        self.assertEqual(v["byButton"], {"hidden": True, "openKey": None, "restored": True})
        self.assertEqual(v["reopened"], {"hidden": False, "name": "Beta", "where": "a / beta",
                                         "active": "drawer-close"})
        self.assertEqual(v["byScrim"], {"hidden": True, "scrimHidden": True, "openKey": None})
        self.assertEqual(v["close"], 2)
        self.assertEqual(v["open"], ["::alpha", "::beta"])

    def test_tab_key_wraps_inside_the_drawer(self):
        v = self.case("tab_key_wraps_focus_inside_the_open_drawer")
        self.assertTrue(v["wrapped"])
        self.assertTrue(v["prevented"])
        self.assertTrue(v["forwardPrevented"])
        self.assertTrue(v["backToFirst"])
        self.assertTrue(v["lastWasNotFirst"])

    # -- accept: Files tab -------------------------------------------------------------
    def test_files_tab_switches_the_view_and_leaves_the_legacy_islands_alone(self):
        v = self.case("tabs_switch_the_views_and_never_touch_the_legacy_islands")
        files = v["files"]
        self.assertEqual(files["views"], {"overview": True, "timeline": True, "files": False,
                                          "graph": True, "transcripts": True})
        self.assertEqual(files["tab"], "files")
        self.assertEqual(files["tabCalls"], [{"tab": "files", "loop": "::alpha"}])
        self.assertEqual(files["pressed"], [["Overview", "false", False], ["Timeline", "false", False],
                                            ["Files", "true", True], ["Graph", "false", False],
                                            ["Transcripts", "false", False]])
        self.assertTrue(files["sameButton"], "switching tabs re-created the tab buttons")
        self.assertEqual(v["afterCycle"]["tabCalls"],
                         ["files", "timeline", "graph", "transcripts", "overview"])
        self.assertEqual(v["afterCycle"]["views"], {"overview": False, "timeline": True, "files": True,
                                                    "graph": True, "transcripts": True})
        self.assertEqual(v["tabCallsTotal"][-1], "files")
        # the islands: the only writes anywhere inside them are the `hidden`
        # attribute of the #view-* frames themselves; nothing in #actions-section
        # or #drawer-inbox-list, no child of any island ever
        for record in v["islandRecords"]:
            kind, attr, target = record.split(":")
            self.assertEqual((kind, attr), ("attributes", "hidden"), record)
            self.assertTrue(target.startswith("view-"), record)
        self.assertTrue(v["islandsUnchanged"], "an island's markup differs after the drawer ran")

    def test_legacy_hooks_fire_once_per_change_and_never_from_a_tick(self):
        v = self.case("legacy_hooks_fire_once_per_change")
        self.assertEqual(v["afterOpen"], {"open": ["::alpha"], "detail": 1})
        self.assertEqual(v["afterEqual"], 1)  # a deep-equal refetch is not a new detail
        self.assertEqual(v["afterChanged"], ["Ship the drawer as Preact components.", "Changed."])
        self.assertEqual(v["afterTick"], {"detail": 2, "open": 1, "close": 0})
        self.assertEqual(v["afterSecond"], {"open": ["::alpha", "::beta"], "close": 0, "detail": 3})
        self.assertEqual(v["afterClose"], {"close": 1, "open": 2, "tabs": 0})

    def test_legacy_hooks_are_optional(self):
        self.assertEqual(self.case("legacy_hooks_are_optional"), {"opened": True, "closed": True})

    # -- controls -----------------------------------------------------------------------
    def test_stop_asks_for_confirmation_then_posts_and_reports(self):
        v = self.case("stop_asks_for_confirmation_and_posts")
        shown = v["shown"]
        self.assertFalse(shown["hidden"])
        self.assertTrue(shown["startDisabled"])
        self.assertFalse(shown["stopDisabled"])
        self.assertEqual((shown["startTitle"], shown["stopTitle"]), ("Already running", "Stops PID 12"))
        self.assertEqual(shown["note"], "Stops PID 12")
        self.assertEqual((shown["noteClass"], shown["noteRole"]), ("control-note caption", "status"))
        self.assertEqual(v["declined"], {"confirms": ["Stop Alpha? Stops PID 12"], "posts": 0,
                                         "note": "Stops PID 12"})
        stopped = v["stopped"]
        self.assertEqual(stopped["posts"], [{"url": "/api/loop/stop",
                                             "body": {"root": stopped["root"], "driver": "portable"}}])
        self.assertEqual(stopped["note"], "Stop requested for PID 4242.")
        self.assertEqual(stopped["noteClass"], "control-note control-ok")
        failed = v["failed"]
        self.assertEqual(failed["note"], "Loop stop failed: boom")
        self.assertEqual((failed["noteClass"], failed["noteRole"]), ("control-note control-error", "alert"))
        self.assertEqual(v["totalPosts"], 2)  # the disabled start button posted nothing

    def test_start_needs_no_confirmation_and_its_note_survives_a_reopen(self):
        v = self.case("start_posts_without_confirmation_and_notes_survive_a_reopen")
        self.assertEqual(v["idle"], {"note": "", "startDisabled": False})
        started = v["started"]
        self.assertEqual(started["posts"], [{"url": "/api/loop/start",
                                             "body": {"root": started["root"], "driver": "portable"}}])
        self.assertEqual((started["note"], started["confirms"]), ("Started (PID 4242).", 0))
        self.assertEqual(v["reopenedNote"], "Started (PID 4242).")

    def test_server_action_note_counts_up_as_reltime_text_only(self):
        v = self.case("server_action_record_shows_a_reltime_note")
        self.assertEqual((v["before"], v["after"]), ("Loop finished · 3m ago", "Loop finished · 5m ago"))
        self.assertEqual(v["noteClass"], "control-note control-ok")
        self.assertTrue(v["hasTime"])
        self.assertEqual(v["types"], ["characterData"])
        self.assertTrue(v["allInReltime"])


if __name__ == "__main__":
    unittest.main()

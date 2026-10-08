"""Shared leaf components of the dashboard frontend, in a real browser (GOAL
DoD6, iter-3 blocking 2: a clock tick must not rebuild anything).

ui_common_cases.mjs runs in headless chromium through ui_browser.cjs (no
server, every request answered by page.route) with the real vendored preact,
@preact/signals and htm; these tests assert on the values it records. The
class SKIPS, with the reason, when node, playwright-core or chromium is
missing: it never passes vacuously.
"""
from __future__ import annotations

import unittest

from . import ui_browser_support as browser


@unittest.skipIf(browser.skip_reason() is not None, browser.skip_reason() or "")
class CommonComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = browser.run_cases("ui_common_cases.mjs")

    def case(self, name):
        out = (self.output["results"] or {}).get(name)
        self.assertIsNotNone(out, f"case {name} did not run: {self.output}")
        self.assertNotIn("error", out, out.get("error"))
        return out["value"]

    def test_harness_loaded_everything_without_errors(self):
        self.assertEqual(self.output["errors"], [])
        self.assertEqual(self.output["console"], [])
        self.assertEqual(len(self.output["results"]), 7)

    def test_clock_ticks_change_character_data_only(self):
        # accept: RelTime mounted, store.now +5 s, +60 s, "59s ago" -> "1m ago"
        # -> MutationObserver sees only characterData, text updated, 0 childList
        v = self.case("reltime_ticks_are_character_data_only")
        self.assertEqual(v["initial"], "10s ago")
        steps = {(s["secondsAfterIso"], i): s for i, s in enumerate(v["steps"])}
        texts = [s["text"] for s in v["steps"]]
        self.assertEqual(texts, ["15s ago", "1m ago", "1m ago", "1m ago", "2m ago", "2m ago"])
        for step in v["steps"]:
            self.assertEqual(step["text"], step["expected"])
            self.assertEqual(step["childNodes"], 1)
            self.assertTrue(step["sameTextNode"], "the text node was replaced")
            for kind in step["types"]:
                self.assertEqual(kind, "characterData", step)
        # +5 s and +60 s moved the text; an unchanged label writes nothing
        self.assertEqual(v["steps"][0]["types"], ["characterData"])
        self.assertEqual(v["steps"][1]["types"], ["characterData"])
        self.assertEqual(v["steps"][2]["types"], [])
        self.assertEqual(v["steps"][3]["types"], [])
        self.assertEqual(v["steps"][4]["types"], ["characterData"])
        self.assertEqual(v["steps"][5]["types"], [])
        self.assertEqual(len(steps), 6)
        for step in v["steps"]:
            self.assertEqual(step["targets"], ["text"] if step["types"] else [])

    def test_parent_does_not_rerender_on_a_tick(self):
        v = self.case("reltime_ticks_are_character_data_only")
        self.assertEqual(v["rowRenders"], 1)
        self.assertEqual(v["datetime"], "2026-06-01T11:59:00Z")
        self.assertTrue(v["hasTitle"])
        self.assertEqual(v["attrs"], ["data-reltime", "datetime", "title"])

    def test_the_59s_to_1m_boundary_is_one_text_update(self):
        v = self.case("reltime_across_the_minute_boundary")
        self.assertEqual(v["seen"], [
            "last write 58s ago!", "last write 59s ago!",
            "last write 1m ago!", "last write 1m ago!"])
        self.assertEqual(v["types"], ["characterData", "characterData"])
        self.assertTrue(v["sameTextNode"])

    def test_missing_timestamp_reads_never(self):
        v = self.case("reltime_without_a_timestamp")
        self.assertEqual(v, {"text": "never", "datetime": "", "title": "No activity recorded"})

    def test_new_timestamp_updates_the_same_element(self):
        v = self.case("reltime_follows_a_new_iso_prop")
        self.assertEqual((v["before"], v["after"]), ("1m ago", "1h ago"))
        self.assertTrue(v["same"])

    def test_state_badge_and_history_match_the_app_js_dom(self):
        v = self.case("state_badge_and_history_render_the_app_js_dom")

        def cell(letter, verdict):
            return {"tag": "span", "class": f"hist-cell hist-{verdict}",
                    "children": [{"text": letter}]}

        def badge(tone, icon, text, title):
            return {"tag": "span", "class": f"badge badge-{tone}", "title": title,
                    "children": [{"tag": "span", "class": "badge-icon",
                                  "children": [{"text": icon}]}, {"text": text}]}

        title = "Verdicts, oldest to newest: I S I"
        self.assertEqual(v["running"], {"tag": "div", "children": [
            badge("live", "●", "Running",
                  "STATE.md: iterating · verdict: ITERATE · live via driver, lock"),
            {"tag": "span", "class": "hist", "title": title, "aria": title, "children": [
                cell("I", "iterate"), cell("S", "ship"), cell("I", "iterate")]}]})
        self.assertEqual(v["shipped"]["children"][0],
                         badge("positive", "✓", "Shipped", "STATE.md: shipped · verdict: SHIP"))
        self.assertEqual(v["held"]["children"][0],
                         badge("warning", "!", "Held", "STATE.md: iterating · held: waiting for a human"))
        self.assertEqual(v["held"]["children"][1], {
            "tag": "span", "class": "hist", "title": "No evaluator verdicts parsed from LOG.md",
            "children": [{"tag": "span", "class": "hist-none", "children": [{"text": "—"}]}]})
        # only the newest 12 cells are shown, the title lists them all
        self.assertEqual(len(v["long"]["children"]), 12)
        self.assertTrue(v["long"]["title"].endswith("I S I S I S I S I S I S I S B H"))
        self.assertEqual(v["long"]["children"][-2:], [cell("B", "blocked"), cell("H", "needs_human")])

    def test_empty_state_and_severity_badges(self):
        v = self.case("empty_state_and_severity_badge")
        self.assertEqual(v[0], {"tag": "div", "class": "empty", "children": [
            {"tag": "span", "class": "empty-icon", "children": [{"text": "✓"}]},
            {"tag": "span", "children": [{"text": "Nothing needs you."}]}]})
        pills = [(p["class"], p["children"][0]["children"][0]["text"], p["children"][1]["text"])
                 for p in v[1:]]
        self.assertEqual(pills, [
            ("badge badge-negative", "!", "Action"),
            ("badge badge-warning", "▲", "Check"),
            ("badge badge-neutral", "•", "Note"),
            ("badge badge-neutral", "•", "Note"),
            ("badge badge-neutral", "•", "odd")])

    def test_mount_clears_the_placeholder_once(self):
        v = self.case("mount_clears_the_placeholder_once_and_keeps_nodes_across_renders")
        self.assertEqual(v["afterFirst"], {"children": 1, "placeholder": False})
        self.assertTrue(v["sameNode"], "a second mount must patch, not rebuild")
        self.assertEqual(v["text"], "three")
        self.assertEqual(v["afterUnmount"], 0)


if __name__ == "__main__":
    unittest.main()

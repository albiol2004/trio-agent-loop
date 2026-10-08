"""Signals store and stream transport of the dashboard frontend (GOAL DoD6).

Runs dashboard/ui/store.js, transport.js and format.js in node (the resolve
hook of ui_esm_register.mjs maps the import map's bare specifiers to the
vendored files) with fake fetch / EventSource / document / window / storage
and fake time; ui_store_cases.mjs prints the observable values of each case
and these tests assert on them. The transport cases port the semantics of
the client cases in test_stream_client.py (the stream -> polling fallback,
resync, visibility, detail refetch), which exercise app.js code that the
Preact frontend replaces.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
NODE = shutil.which("node")
_RESULTS: dict | None = None


def results() -> dict:
    global _RESULTS
    if _RESULTS is None:
        proc = subprocess.run(
            [NODE, "--import", str(HERE / "ui_esm_register.mjs"),
             str(HERE / "ui_store_cases.mjs")],
            capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
            env=dict(os.environ, TZ="UTC"))
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise AssertionError(
                f"no output from ui_store_cases.mjs (exit {proc.returncode}): "
                + proc.stderr[-2000:])
        _RESULTS = json.loads(lines[-1])
    return _RESULTS


def case(name: str):
    out = results()[name]
    if "error" in out:
        raise AssertionError(f"case {name} threw: {out['error']}")
    return out["value"]


@unittest.skipIf(NODE is None, "node is not installed")
class FormatTests(unittest.TestCase):
    """format.js gives the strings app.js gives for the same input."""

    def test_relative_times_follow_the_passed_clock(self):
        v = case("format_helpers")
        self.assertEqual(v["rel"], [
            "just now", "just now", "5s ago", "59s ago", "1m ago", "59m ago",
            "1h ago", "23h ago", "1d ago", "6d ago", "May 24"])
        self.assertEqual(v["relEdge"], ["never", "never", "garbage", "just now", "Feb 3, 2024"])

    def test_sizes_durations_and_text(self):
        v = case("format_helpers")
        self.assertEqual(v["size"], ["0B", "1023B", "1.0KB", "1.5KB", "1.0MB", "0B"])
        self.assertEqual(v["duration"], ["—", "—", "—", "60s", "1m", "59m", "1h 0m", "2h 3m"])
        self.assertEqual(v["oneLine"], ["a b c", 160, "xxxxxxxxx…", ""])
        self.assertEqual(v["plural"], ["1 item", "2 items", "0 matches"])

    def test_verdicts_titles_and_phases(self):
        v = case("format_helpers")
        self.assertEqual(v["verdicts"], [
            "ship", "needs_human", "none", "none",
            ["iterate", "ship", "blocked", "needs_human"], "SHIP", "BLOCKED", None])
        self.assertEqual(v["archived"], [True, False])
        self.assertEqual(v["statusWord"], ["—", "—", " going "])
        self.assertEqual(v["title"], ["T", "Fix it.", "n"])
        self.assertEqual(v["iteration"], ["—", "2", "2 of 5"])
        self.assertEqual(v["phase"], [
            "", "lead done · lead and evaluator active", "evaluator active", ""])
        self.assertEqual(v["derived"], ["held", None, None])

    def test_state_badge_data(self):
        badges = case("format_helpers")["badges"]
        self.assertEqual(badges["running"], [
            "badge badge-live", "●", "Running", "STATE.md: iterating · live via driver, lock"])
        self.assertEqual(badges["held"], [
            "badge badge-warning", "!", "Held", "STATE.md: iterating · held: waiting"])
        self.assertEqual(badges["unmapped"][2], "WeIrD")
        self.assertEqual(badges["human"][:3], ["badge badge-warning", "!", "Needs human"])
        self.assertEqual(badges["blocked"][:3], ["badge badge-negative", "✕", "Blocked"])
        self.assertEqual(badges["shipped"], [
            "badge badge-positive", "✓", "Shipped", "STATE.md: iterating · verdict: SHIP"])
        self.assertEqual(badges["verdictHuman"][2], "Needs human")
        self.assertEqual(badges["verdictBlocked"][2], "Blocked")
        self.assertEqual(badges["iterating"][2], "Iterating")
        self.assertEqual(badges["plain"], ["badge badge-neutral", "○", "—", "STATE.md: —"])


@unittest.skipIf(NODE is None, "node is not installed")
class StoreModelTests(unittest.TestCase):
    def test_entity_ids_match_live_model(self):
        v = case("entity_ids")
        self.assertEqual(v["keyed"], "/w/a::alpha")
        self.assertEqual(v["withLoopId"], "lid-1")
        self.assertEqual(v["pathId"], "path:/w/a::alpha")

    def test_snapshot_builds_keyed_loops_and_items(self):
        v = case("snapshot_builds_keyed_entities")
        self.assertTrue(v["loaded"])
        self.assertEqual((v["epoch"], v["seq"]), ("e1", 7))
        self.assertEqual(v["loopIds"], ["lid-alpha", "lid-beta", "lid-gamma"])
        self.assertEqual(v["inboxIds"], ["i1", "i2"])
        self.assertEqual(v["alpha"], {
            "id": "lid-alpha", "key": "/w/a::alpha", "root": "/w/a",
            "workspace": "a", "worktree": False, "running": True})
        self.assertEqual(v["gamma"], {
            "key": "/w/b::gamma", "workspace": "b", "worktree": True})
        self.assertEqual(v["item"], {
            "root": "/w/a", "workspace": "a", "key": "/w/a::beta", "kind": "drift"})
        self.assertEqual(v["byKey"], "lid-gamma")
        self.assertTrue(v["missing"])
        self.assertEqual(v["loops"], ["alpha", "beta", "gamma"])
        self.assertEqual(v["inbox"], ["i1", "i2"])
        self.assertEqual(v["workspaces"], ["a", "b"])
        self.assertEqual(v["scanned"], 2)
        self.assertIn("workspace_order", v["metaKeys"])
        for gone in ("workspaces", "epoch", "seq"):
            self.assertNotIn(gone, v["metaKeys"])

    def test_delta_notifies_only_the_changed_loop(self):
        # accept: applyDelta changing only loop X -> X's signal once, nobody else
        v = case("delta_notifies_only_the_changed_loop")
        self.assertTrue(v["ok"])
        self.assertEqual(v["loopIds"], ["lid-beta"])
        self.assertEqual(v["kinds"], ["loop"])
        counts = v["afterDelta"]
        self.assertEqual(counts["loop:lid-beta"], 1)
        for name in ("loop:lid-alpha", "loop:lid-gamma", "loopIds", "inboxIds",
                     "inbox:i1", "inbox:i2", "inbox", "workspaces", "epoch"):
            self.assertEqual(counts[name], 0, name)
        self.assertEqual(v["seq"], 8)
        self.assertEqual(v["betaIteration"], 9)

    def test_identical_snapshot_and_overview_notify_nobody(self):
        # accept: applySnapshot(S) again -> 0 notifications
        v = case("identical_snapshot_and_overview_notify_nobody")
        self.assertEqual(sum(v["afterSnapshot"].values()), 0, v["afterSnapshot"])
        self.assertEqual(sum(v["afterOverview"].values()), 0, v["afterOverview"])
        changed = v["afterChange"]
        self.assertEqual(changed["loop:lid-gamma"], 1)
        self.assertEqual(changed["loop:lid-alpha"] + changed["loop:lid-beta"], 0)
        self.assertEqual(changed["loopIds"], 0)

    def test_gap_and_foreign_epoch_are_refused_untouched(self):
        # accept: from_seq != seq or a foreign epoch -> {ok: false}, nothing notified
        v = case("delta_refused_on_gap_or_foreign_epoch")
        for refused in ("gap", "foreign", "replay", "noChanges", "empty"):
            self.assertIs(v[refused], False, refused)
        self.assertEqual(v["gapIds"], 0)
        self.assertEqual((v["seq"], v["epoch"], v["gammaIteration"]), (7, "e1", 2))
        self.assertEqual(v["notified"], 0)

    def test_membership_changes_reassign_the_id_lists_once(self):
        v = case("delta_membership_order_and_removal")
        self.assertTrue(v["ok"])
        self.assertEqual(v["afterFirst"]["loopIds"], ["lid-beta", "lid-gamma", "lid-delta"])
        self.assertEqual(v["afterFirst"]["inboxIds"], ["i2"])
        self.assertTrue(v["removedSig"])
        self.assertTrue(v["removedKey"])
        self.assertEqual((v["loopIdsNotified"], v["inboxIdsNotified"]), (1, 1))
        # same entity JSON with the keys in another order: not a change
        self.assertEqual(v["reorderedUpsertNotifiedBeta"], 0)
        self.assertEqual(v["reorderedUpsertNotifiedLoopIds"], 0)

    def test_workspace_rename_reaches_its_own_loops_only(self):
        v = case("workspace_change_reaches_its_loops_only")
        self.assertEqual(v["gamma"], "renamed")
        self.assertEqual((v["gammaNotified"], v["alphaNotified"]), (1, 0))
        self.assertEqual(v["headers"], ["a", "renamed"])


@unittest.skipIf(NODE is None, "node is not installed")
class StoreStateTests(unittest.TestCase):
    def test_prefs_load_and_save_in_the_app_js_shape(self):
        v = case("prefs_load_and_save")
        self.assertEqual(v["loaded"], {
            "tab": "running", "workspace": "a",
            "sort": {"key": "name", "dir": "asc"}, "query": ""})
        self.assertEqual(v["saved"], {
            "tab": "archived", "workspace": "b",
            "sort": {"key": "activity", "dir": "desc"}})
        self.assertEqual(v["tabAfterBogus"], "archived")
        self.assertTrue(v["queryNotSaved"])
        self.assertEqual(v["corrupt"], {"tab": "all", "sort": {"key": "activity", "dir": "desc"}})
        self.assertEqual(v["odd"], {"tab": "all", "sort": {"key": "activity", "dir": "desc"}})
        self.assertEqual(v["noStorage"], "running")
        self.assertEqual(v["defaults"], {
            "tab": "all", "workspace": "", "showReadInbox": False, "drawerTab": "overview"})

    def test_expanded_set_is_replaced_and_reactive(self):
        v = case("expanded_and_show_read")
        self.assertEqual(v["seen"], [False, True, False])
        self.assertTrue(v["replaced"])
        self.assertEqual(v["size"], 0)
        self.assertTrue(v["showRead"])

    def test_set_inbox_read_posts_per_root_and_flips_only_those_items(self):
        v = case("set_inbox_read_posts_per_root_and_flips_only_those_items")
        self.assertEqual(v["posts"], [
            {"path": "/api/inbox/read", "body": {"ids": ["i1"], "root": "/w/a"}},
            {"path": "/api/inbox/read", "body": {"ids": ["i3"], "root": "/w/b"}}])
        self.assertEqual(v["read"], {"i1": True, "i2": False, "i3": True})
        self.assertEqual(v["notified"], {"i1": 1, "i2": 0, "i3": 1, "loopIds": 0})
        self.assertIs(v["unread"], False)
        self.assertEqual(v["secondPost"], "/api/inbox/unread")

    def test_set_inbox_read_rejects_with_the_server_error(self):
        v = case("set_inbox_read_rejects_with_the_server_error")
        self.assertEqual(v["error"], "workspace root is not allowed")
        self.assertIs(v["stillUnread"], False)

    def test_control_loop_posts_root_and_driver_and_reports(self):
        v = case("control_loop_posts_root_and_driver")
        self.assertEqual(v["started"], {"ok": True, "data": {"pid": 4242}, "error": None})
        self.assertEqual(v["stopped"], {"ok": False, "data": None, "error": "not running"})
        self.assertEqual(v["posts"], [
            {"path": "/api/loop/start", "body": {"root": "/w/a", "driver": "native"}},
            {"path": "/api/loop/stop", "body": {"root": "/w/a", "driver": "portable"}}])

    def test_open_loop_fetches_detail_and_drops_a_stale_answer(self):
        v = case("open_loop_fetches_detail_and_drops_stale_answers")
        self.assertEqual(v["url"], "/api/loop?name=alpha&root=%2Fw%2Fa")
        self.assertEqual(v["cache"], "no-store")
        self.assertEqual(v["statusWhileLoading"], "loading")
        self.assertEqual(v["openKey"], "/w/a::beta")
        self.assertEqual(v["detailName"], "beta")
        self.assertEqual((v["status"], v["tab"]), ("ok", "overview"))
        self.assertEqual(v["fetched"], [
            "/api/loop?name=alpha&root=%2Fw%2Fa", "/api/loop?name=beta&root=%2Fw%2Fa"])

    def test_identical_detail_is_not_reassigned(self):
        v = case("identical_detail_does_not_reassign")
        self.assertEqual(v["unchanged"], 0)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["fetches"], 3)

    def test_changed_detail_reassigns_and_close_resets(self):
        v = case("changed_detail_reassigns_and_close_resets")
        self.assertEqual(v["changed"], 1)
        self.assertIsNone(v["mission"])
        self.assertEqual(v["status"], "idle")
        self.assertIsNone(v["openKey"])

    def test_detail_error_keeps_the_last_detail(self):
        v = case("detail_error_keeps_the_last_detail")
        self.assertEqual(v["kept"], {"status": "ok", "hasDetail": True, "error": "HTTP 500"})
        self.assertEqual(v["first"], {"status": "error", "error": "HTTP 500"})

    def test_clock_sets_now_every_5s_until_stopped(self):
        v = case("clock_ticks_now_every_5s")
        self.assertEqual(v["seen"], [0, 5000, 10000])


@unittest.skipIf(NODE is None, "node is not installed")
class TransportTests(unittest.TestCase):
    def test_healthy_stream_never_polls(self):
        v = case("stream_snapshot_never_polls_while_healthy")
        self.assertEqual(v["sources"], ["/api/stream"])
        self.assertEqual(v["overviewFetches"], 0)
        self.assertEqual(v["initialFeed"]["status"], "connecting")
        self.assertEqual(v["feed"]["mode"], "stream")
        self.assertEqual(v["feed"]["status"], "live")
        self.assertEqual(v["loops"], ["alpha", "beta", "gamma"])
        self.assertEqual(v["lastOkAge"], 0)

    def test_tick_touches_the_feed_only(self):
        v = case("tick_notifies_feed_only")
        self.assertEqual(sum(v["counts"].values()), 0, v["counts"])
        self.assertEqual(v["feed"], 1)
        self.assertEqual(v["updatedAt"], "2026-06-01T12:00:03+00:00")

    def test_closed_stream_polls_every_5s_until_a_stream_snapshot(self):
        # accept: CLOSED stream -> /api/overview every 5 s; later snapshot -> polling stops
        v = case("closed_stream_polls_every_5s_until_a_snapshot")
        self.assertEqual(v["atFailure"], 1)  # at once, not after 5 s
        self.assertEqual(v["feedAfterFailure"]["mode"], "poll")
        self.assertTrue(v["loadedFromPoll"])
        for label in ("t5", "t10", "t15"):
            self.assertEqual(v[label], 1, label)
        self.assertEqual(v["pollsIn15"], 3)
        self.assertEqual(v["sources"], 2, "the stream is retried after 30 s of fallback")
        self.assertEqual(v["afterSnapshot"], 0)
        self.assertEqual(v["feedAfterSnapshot"]["mode"], "stream")
        self.assertEqual(v["feedAfterSnapshot"]["status"], "live")

    def test_error_while_connecting_is_reconnecting_until_the_next_event(self):
        # accept: onerror while CONNECTING -> feed.status "reconnecting"
        v = case("error_while_connecting_is_reconnecting_until_the_next_event")
        self.assertEqual(v["statuses"], ["live", "reconnecting", "live"])
        self.assertEqual(v["mode"], "stream")
        self.assertEqual((v["polls"], v["sources"]), (0, 1))

    def test_no_event_source_polls(self):
        v = case("no_event_source_polls_at_once_and_every_5s")
        self.assertEqual((v["first"], v["later"]), (1, 2))
        self.assertEqual(v["sources"], 0)
        self.assertEqual(v["loops"], ["alpha", "beta", "gamma"])
        self.assertEqual((v["mode"], v["status"]), ("poll", "live"))

    def test_silent_stream_falls_back_after_45s(self):
        v = case("silent_stream_falls_back_after_45s")
        self.assertEqual(v["quiet"], 0)
        self.assertGreaterEqual(v["silent"], 1)
        self.assertEqual(v["mode"], "poll")
        self.assertEqual(v["sourceState"], 2)

    def test_poll_failure_is_offline_then_recovers(self):
        v = case("poll_failure_is_offline_with_the_error")
        self.assertEqual(v["failed"]["status"], "offline")
        self.assertEqual(v["failed"]["error"], "HTTP 500")
        self.assertFalse(v["loadedWhileFailing"])
        self.assertEqual(v["recovered"]["status"], "live")
        self.assertIsNone(v["recovered"]["error"])
        self.assertTrue(v["loaded"])

    def test_stale_build_is_repolled_sooner_once(self):
        v = case("stale_build_repolls_sooner_once")
        self.assertEqual((v["soon"], v["notAgain"]), (1, 0))

    def test_hidden_tab_closes_the_stream_and_visible_reopens_with_since(self):
        v = case("hidden_tab_closes_the_stream_and_visible_reopens_with_since")
        self.assertEqual(v["hiddenStates"], [2])
        self.assertEqual(v["whileHidden"], 0)
        self.assertEqual(len(v["sources"]), 2)
        self.assertEqual(v["since"], "e1:7")

    def test_gap_and_foreign_epoch_reopen_from_the_known_position(self):
        v = case("gap_and_foreign_epoch_resync_from_the_known_position")
        self.assertEqual(v["afterGap"], {"sources": 2, "firstState": 2, "since": "e1:7"})
        self.assertEqual(v["afterForeign"], {"sources": 3, "since": "e1:7"})
        self.assertEqual(v["applied"], 9)
        self.assertEqual(v["seq"], 8)

    def test_a_stream_refused_more_than_5_times_falls_back_to_polling(self):
        v = case("stream_refused_more_than_5_times_gives_up_to_polling")
        self.assertEqual(v["mode"], "poll")
        self.assertEqual(v["polls"], 1)

    def test_detail_is_refetched_only_when_the_open_loop_changed(self):
        v = case("detail_refetch_rules")
        self.assertEqual(v["openFetch"], 1)
        self.assertEqual(v["tick"], 0)
        self.assertEqual(v["otherLoop"], 0)
        self.assertEqual(v["openLoop"], 1)
        self.assertEqual(v["sameEpochSameSnapshot"], 0)
        self.assertEqual(v["newEpochSnapshot"], 1)

    def test_stop_ends_streams_timers_and_listeners(self):
        v = case("stop_ends_everything")
        self.assertEqual(v["retryBefore"], "function")
        self.assertEqual(v["afterStop"], {"retry": None, "state": 2, "listeners": 0})
        self.assertEqual(v["calls"], 0)
        self.assertEqual(v["timers"], 0)

    def test_retry_reopens_the_stream_or_polls_now(self):
        v = case("retry_reopens_the_stream_or_polls_now")
        self.assertEqual(v["reopened"], {"sources": 2, "since": "e1:7"})
        self.assertEqual(v["pollsOnRetry"], 1)
        self.assertEqual(v["viaStore"], "function")


@unittest.skipIf(NODE is None, "node is not installed")
class LocationTests(unittest.TestCase):
    def test_open_writes_the_hash_and_close_clears_it(self):
        # accept: openLoop(k) -> '#root=<enc root>&loop=<enc name>'; closeLoop -> hash empty
        v = case("open_loop_writes_the_hash_and_close_clears_it")
        self.assertEqual(v["opened"], "#root=%2Fw%2Fa&loop=alpha")
        self.assertEqual(v["closed"], "")
        self.assertEqual(v["afterUnbind"], "", "an unbound location is left alone")

    def test_hash_before_data_opens_after_the_first_snapshot(self):
        # accept: hash set before data -> drawer opens after the first snapshot
        v = case("hash_before_data_opens_after_the_first_snapshot")
        self.assertEqual(v["before"], {"open": None, "hash": "#root=%2Fw%2Fa&loop=alpha"})
        self.assertEqual(v["open"], "/w/a::alpha")
        self.assertEqual(v["hash"], "#root=%2Fw%2Fa&loop=alpha")
        self.assertEqual(v["detailFetches"], 1)
        self.assertEqual(v["detail"], "alpha")

    def test_hash_resolves_by_root_and_name_else_unique_name(self):
        v = case("hash_resolution_root_match_else_unique_name")
        self.assertEqual(v["exact"]["open"], "/w/b::gamma")
        self.assertEqual(v["nameOnly"]["open"], "/w/b::gamma")
        self.assertEqual(v["rootFallsBackToUniqueName"]["open"], "/w/b::gamma")
        self.assertIsNone(v["ambiguousName"]["open"])
        self.assertEqual(v["ambiguousWithRoot"]["open"], "/w/b::alpha")
        self.assertIsNone(v["unknown"]["open"])
        self.assertIsNone(v["noLoopParam"]["open"])
        # an unresolved hash stays in the URL
        self.assertEqual(v["unknown"]["hash"], "#root=%2Fw%2Fa&loop=nothing")

    def test_pending_hash_waits_for_a_loop_that_appears_later(self):
        v = case("pending_hash_waits_for_a_loop_that_appears_later")
        self.assertIsNone(v["beforeDelta"]["open"])
        self.assertEqual(v["open"], "/w/b::late")

    def test_hashchange_opens_switches_and_closes(self):
        v = case("hashchange_opens_and_closes")
        self.assertEqual(v["opened"], "/w/b::gamma")
        self.assertEqual(v["switched"], "/w/a::beta")
        self.assertEqual(v["closed"], {"open": None, "hash": ""})
        self.assertIsNone(v["pendingOpen"])
        self.assertEqual(v["afterAppear"], "/w/b::future")
        self.assertEqual(v["hash"], "#root=%2Fw%2Fb&loop=future")

    def test_closing_nothing_leaves_a_pending_hash_alone(self):
        v = case("closing_does_not_touch_a_pending_hash")
        self.assertEqual(v["hash"], "#root=%2Fw%2Fa&loop=alpha")


if __name__ == "__main__":
    unittest.main()

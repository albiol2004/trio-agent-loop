"""The whole dashboard page, end to end, in a real browser (GOAL DoD6, DoD3).

A lab ``dashboard/serve.py`` (ephemeral loopback port, lab state dirs, a 2 s
scanner) serves fixture workspaces whose mailbox files are written seconds
before the page opens: NOT back-dated, so their relative times keep moving.
``ui_e2e_harness.cjs`` (playwright-core + chromium, discovered by
ui_browser_support) opens the Preact page, opens a loop's drawer, expands the
commits panel, scrolls the board, selects text in a row and sets the workspace
filter and the search query; then it watches the listed sections with
MutationObservers for 65+ seconds (a wall-clock minute boundary falls inside:
decision D4-6), rewrites another loop's STATE.md, walks the drawer's views,
marks an inbox item read, reloads on the deep link and presses Escape. A
second lab with ``TRIO_DASH_STREAM=0`` proves the polling fallback.

Set ``TRIO_UI_E2E_OUT=/some/path.json`` to dump every observation (the push
run to that path, the polling run to ``<path>-poll.json``). The class SKIPS,
with the reason, when node, playwright-core or chromium is missing; it never
passes vacuously.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import ui_browser_support as browser
from .test_dash_stream_dom import LabServer, _iso, _write_mailbox

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = Path(__file__).resolve().parent / "ui_e2e_harness.cjs"

SCAN_S = 2
IDLE_MS = 66000            # > 65 s: always spans a wall-clock minute boundary
HUMAN, RUN, SHIP = "loop-human", "loop-run", "loop-ship"
OTHER = "loop-elsewhere"   # lives in the second workspace
FILL = [f"loop-fill-{n:02d}" for n in range(1, 25)]
MAIN_LOOPS = FILL + [HUMAN, RUN, SHIP]
ALL_LOOPS = MAIN_LOOPS + [OTHER]
COMMITS = 11               # more than the drawer's 8-commit preview
TRANSCRIPT_NEEDLE = "Deciding on the schema now"
SHIP_STATE_AFTER = "iteration: 1\nstatus: blocked\nmax_iterations: 5\n"

SECTIONS = ("kpis", "needs-list", "running-list", "loop-rows", "notes-list",
            "workspace-filter", "tabs", "drawer-inbox-list", "drawer-strip",
            "commit-list", "slice-list", "drawer-timeline", "view-timeline",
            "view-files", "view-graph", "session-list")
# sections that must show something while the window is observed (the legacy
# views are empty until their tab is opened, the rest can be legitimately empty)
FILLED = ("kpis", "needs-list", "running-list", "loop-rows", "workspace-filter",
          "tabs", "drawer-strip", "commit-list", "drawer-timeline")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


def init_repo(root: Path, commits: int) -> None:
    """A repo with `commits` slice commits (the drawer's commit list)."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for n in range(commits):
        (root / f"f{n}.py").write_text(f"x = {n}\n", encoding="utf-8")
        _git(root, "add", f"f{n}.py")
        _git(root, "commit", "-q", "-m", f"slice(schema-pick): step {n}")


def write_fixtures(main: Path, other: Path) -> list[Path]:
    """Mailboxes, written now. The shipped loop goes last so its activity is the
    newest of the non-running loops and a later STATE.md rewrite keeps its row
    in place (no reorder, so the scroll offset is not disturbed)."""
    for name in FILL:
        _write_mailbox(
            main, name, f"# Mission: filler {name}\n\nmission: filler\n",
            "iteration: 1\nstatus: shipped\nmax_iterations: 5\n",
            "# LOG\n\n- iter 1 | lead | filler\n", "VERDICT: SHIP\n\n## Iteration 1\nok\n")
    _write_mailbox(
        main, HUMAN, "# Mission: decide the schema\n\nmission: schema\n",
        "iteration: 3\nstatus: needs_human\nmax_iterations: 5\n",
        "- iter 1 | lead | planned the schema work\n"
        "- iter 1 | evaluator | VERDICT: ITERATE\n"
        "- iter 2 | lead | revised the plan\n"
        "- iter 3 | evaluator | VERDICT: NEEDS_HUMAN\n",
        "VERDICT: NEEDS_HUMAN\n\n## Iteration 3\nWhich schema should win?\n",
        "# PLAN\n\n```yaml\nslices:\n  - id: schema-pick\n"
        "    writes: [b.py]\n    reads: []\n    status: in_progress\n"
        "    iteration: 3\n    accepts: []\n```\n")
    # a Claude-format session export, so the Transcripts tab has a stream to follow
    exports = main / HUMAN / ".sessions"
    exports.mkdir()
    (exports / "lead.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": _iso(0),
                    "message": {"role": "user", "content": "Please pick a schema"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": _iso(0),
                      "message": {"role": "assistant",
                                  "content": [{"type": "text", "text": TRANSCRIPT_NEEDLE}]}}) + "\n",
        encoding="utf-8")
    run = _write_mailbox(
        main, RUN, "# Mission: keep the widget live\n\nmission: widget\n",
        "iteration: 2\nstatus: running\nmax_iterations: 5\n",
        "# LOG\n\n- iter 2 | lead | started\n",
        "VERDICT: ITERATE\n\n## Iteration 1\nnot yet\n")
    # MAILBOX-SCHEMA.md "Session heartbeat": a live in-session loop.
    (run / ".heartbeat.json").write_text(json.dumps({
        "schema": 1, "writer": "trio-skill", "session_id": "", "pid": None,
        "phase": "lead", "iteration": 2, "updated_at": _iso(0),
        "ttl_s": 14400, "done": False}) + "\n", encoding="utf-8")
    _write_mailbox(
        other, OTHER, "# Mission: elsewhere\n\nmission: elsewhere\n",
        "iteration: 1\nstatus: shipped\nmax_iterations: 5\n",
        "# LOG\n\n- iter 1 | lead | done\n", "VERDICT: SHIP\n\n## Iteration 1\nok\n")
    _write_mailbox(
        main, SHIP, "# Mission: ship the parser\n\nmission: parser\n",
        "iteration: 1\nstatus: shipped\nmax_iterations: 5\n",
        "# LOG\n\n- iter 1 | lead | SHIP\n", "VERDICT: SHIP\n\n## Iteration 1\nall green\n")
    return [p for ws in (main, other) for p in ws.rglob("*")
            if p.is_file() and ".git" not in p.parts]


def profile_root() -> str:
    """Where chromium may keep its profile: its singleton socket path must stay
    under ~108 bytes, which a long $TMPDIR breaks."""
    base = tempfile.gettempdir()
    if len(base) > 40:
        base = "/tmp" if os.path.isdir("/tmp") else base
    return base


def run_scenario(tmp_root: Path, name: str, *, stream: bool) -> dict:
    """Start a lab, write the fixtures, run the harness, return its JSON."""
    tmp = Path(tempfile.mkdtemp(prefix=name + "-", dir=str(tmp_root)))
    main, other = tmp / "ws-main", tmp / "ws-other"
    init_repo(main, COMMITS)
    other.mkdir()
    server = LabServer(tmp, [main, other], stream=stream, scan_interval=str(SCAN_S))
    profile = None
    try:
        server.start()
        written = write_fixtures(main, other)
        mtimes = [p.stat().st_mtime for p in written]
        shell = {
            "scenario": name, "base": server.base,
            "root": str(main.resolve()), "otherRoot": str(other.resolve()),
            "loops": ALL_LOOPS, "humanLoop": HUMAN, "shipLoop": SHIP,
            "fillLoop": FILL[4], "otherLoop": OTHER,
            "shipStatePath": str(main / SHIP / "STATE.md"),
            "shipStateText": SHIP_STATE_AFTER, "transcriptNeedle": TRANSCRIPT_NEEDLE,
            "idleMs": IDLE_MS, "pollWaitMs": 12500, "scanMs": SCAN_S * 1000,
            "playwright": browser.PLAYWRIGHT_CORE, "chromium": browser.CHROMIUM,
        }
        profile = Path(tempfile.mkdtemp(prefix="uie-p-", dir=profile_root()))
        shell["userDataDir"] = str(profile)
        cfg = tmp / "harness-config.json"
        cfg.write_text(json.dumps(shell), encoding="utf-8")
        proc = subprocess.run(
            [browser.NODE, str(HARNESS), str(cfg)], capture_output=True, text=True,
            timeout=300, cwd=str(REPO_ROOT),
            env=dict(os.environ, TZ="UTC"))
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise AssertionError(f"{name}: no harness output; stderr: {proc.stderr[-1500:]}")
        out = json.loads(lines[-1])
        out["config"] = {k: v for k, v in shell.items() if k not in ("playwright", "chromium")}
        out["fixture_mtime_min_s"] = min(mtimes)
        out["fixture_mtime_max_s"] = max(mtimes)
        out["server_log"] = server.log_text()[-800:]
        return out
    finally:
        server.stop()
        if profile is not None:
            shutil.rmtree(str(profile), True)


@unittest.skipIf(browser.skip_reason() is not None, browser.skip_reason() or "")
class UiEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="uie-"))
        cls.addClassCleanup(shutil.rmtree, str(root), True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            push = pool.submit(run_scenario, root, "push", stream=True)
            poll = pool.submit(run_scenario, root, "poll", stream=False)
            cls.push = push.result()
            cls.poll = poll.result()
        cls.steps = cls.push["steps"]

    def ctx(self, run=None) -> str:
        run = run or self.push
        return json.dumps({k: v for k, v in run.items()
                           if k not in ("requests", "config")}, indent=1)[:4000]

    # ---- accept: idle >= 65 s across a minute boundary rebuilds nothing ----

    def test_idle_across_a_minute_boundary_rebuilds_nothing(self):
        push, steps = self.push, self.steps
        idle = steps["idle"]
        ctx = self.ctx()
        self.assertEqual(push["errors"], [], ctx)
        self.assertTrue(push["board_ready"], ctx)
        self.assertTrue(push["snapshot_seen"], ctx)
        self.assertTrue(steps["drawer_opened"], ctx)

        # the fixtures were written <= 10 s before the page opened
        open_s = push["t_open_ms"] / 1000
        self.assertLessEqual(open_s - push["fixture_mtime_min_s"], 10, ctx)
        self.assertGreaterEqual(open_s - push["fixture_mtime_max_s"], 0, ctx)

        # the window is >= 65 s and spans a minute boundary
        self.assertGreaterEqual(idle["window_ms"], 65000, ctx)
        self.assertTrue(idle["spans_minute_boundary"], ctx)
        # control: the measurement is live (scan ticks arrived while idle)
        self.assertGreaterEqual(idle["events"].get("tick", 0), 10, ctx)
        self.assertNotIn("delta", idle["events"], ctx)

        sections = idle["sections"]
        self.assertEqual(sorted(sections), sorted(SECTIONS))
        for name in SECTIONS:
            with self.subTest(section=name):
                got = sections[name]
                self.assertIsNotNone(got, name)
                self.assertFalse(got["missing"], name)
                self.assertEqual((got["childList"], got["attributes"]), (0, 0), got)
                # characterData only under [data-reltime]
                self.assertEqual(got["charOther"], 0, got)
        for name in FILLED:
            with self.subTest(filled=name):
                self.assertGreater(sections[name]["textLen"], 0, name)

        # an 'ago' label did change (as text of a <time data-reltime>) ...
        reltime_records = sum(s["charReltime"] for s in sections.values())
        self.assertGreater(reltime_records, 0, ctx)
        self.assertNotEqual(idle["ago_before"], idle["ago_after"], ctx)
        # ... across the minute boundary: seconds became minutes
        self.assertTrue(any(re.fullmatch(r"just now|\d+s ago", t) for t in idle["ago_before"]),
                        idle["ago_before"])
        self.assertTrue(any(re.fullmatch(r"\d+m ago", t) for t in idle["ago_after"]),
                        idle["ago_after"])
        # the drawer stayed open throughout
        self.assertTrue(idle["state_after"]["drawerOpen"], ctx)

    # ---- accept: UI state survives a delta and ticks ----

    def test_ui_state_survives_a_delta_and_ticks(self):
        steps = self.steps
        ctx = self.ctx()
        before, mutate = steps["state_before"], steps["mutate"]
        after = mutate["state_after"]
        # the state to preserve was really established
        self.assertTrue(before["drawerOpen"], ctx)
        self.assertIn("Decide the schema", before["drawerName"], ctx)
        self.assertEqual(steps["commits_before"], 8, ctx)
        self.assertEqual(steps["commits_after"], COMMITS, ctx)
        self.assertTrue(any("fewer" in b for b in before["commitButtons"]), ctx)
        self.assertGreater(before["scrollTop"], 0, ctx)
        # (a Range: Selection.toString() is "" inside the inert page behind a drawer)
        self.assertGreater(len(before["selection"].strip()), 0, ctx)
        self.assertTrue(before["selectionAnchorKept"], ctx)
        self.assertEqual(before["workspace"], self.push["config"]["root"], ctx)
        self.assertEqual(before["query"], "loop-", ctx)
        self.assertTrue(steps["other_loop_hidden"], ctx)
        self.assertIn(FILL[4], steps["selection"])

        # idle for a minute and a rewrite plus ticks later: all preserved
        for state in (steps["idle"]["state_after"], after):
            self.assertEqual(state, before, ctx)
        self.assertTrue(after["drawerOpen"])
        self.assertEqual(after["hash"], before["hash"])

        # the rewritten loop's row changed within 2 scan intervals
        self.assertIsNotNone(mutate["changed_ms"], ctx)
        self.assertLessEqual(mutate["changed_ms"], 2 * SCAN_S * 1000 + 1000, ctx)
        self.assertNotEqual(mutate["after"], mutate["before"])
        self.assertRegex(mutate["after"].lower(), r"block")
        self.assertIn(SHIP, mutate["after"])
        # ... via a delta, then >= 2 ticks and a clock tick with nothing lost
        self.assertGreaterEqual(mutate["events"].get("delta", 0), 1, ctx)
        self.assertGreaterEqual(mutate["ticks_after"], 2, ctx)
        self.assertTrue(mutate["clock_moved"], ctx)
        # control: the observer saw the rewrite
        obs = mutate["observer"]["loop-rows"]
        self.assertGreater(obs["childList"] + obs["charOther"] + obs["attributes"], 0, ctx)

    # ---- accept: every script is same-origin, nothing errors, no polling ----

    def test_requests_are_same_origin_without_errors_or_polling(self):
        push = self.push
        ctx = self.ctx()
        base = urlsplit(push["config"]["base"])
        urls = [urlsplit(r["url"]) for r in push["requests"]]
        self.assertTrue(urls)
        for url in urls:
            self.assertEqual((url.scheme, url.netloc), (base.scheme, base.netloc), url.geturl())
        scripts = sorted({urlsplit(r["url"]).path for r in push["requests"]
                          if r["type"] == "script"})
        self.assertIn("/app.js", scripts, scripts)
        self.assertIn("/ui/main.js", scripts, scripts)
        for path in scripts:
            self.assertTrue(path == "/app.js" or path.startswith(("/vendor/", "/ui/")), path)
        self.assertTrue(any(p.startswith("/vendor/") for p in scripts), scripts)
        self.assertEqual(push["errors"], [], ctx)
        self.assertEqual(push["console"], [], ctx)
        # the stream is up: /app.js no longer polls /api/overview at all
        self.assertEqual(push["overview_requests"], 0, ctx)
        self.assertEqual([p for p in self.steps["idle"]["requests"] if p == "/api/overview"], [])
        self.assertTrue(all(p.startswith("/api/stream") for p in push["sse_opened"]), push["sse_opened"])
        self.assertTrue(push["sse_opened"], ctx)
        # the idle minute costs no request but the stream itself
        self.assertEqual(self.steps["idle"]["requests"], [], ctx)

    # ---- accept: URL = open loop ----

    def test_row_click_sets_the_hash_reload_reopens_escape_clears(self):
        steps = self.steps
        ctx = self.ctx()
        expected = "#root=" + quote(self.push["config"]["root"], safe="") + "&loop=" + quote(HUMAN, safe="")
        self.assertEqual(steps["hash_after_click"], steps["hash_expected"], ctx)
        self.assertEqual(steps["hash_after_click"].replace("%2F", "/").count("&loop="), 1)
        self.assertEqual(steps["hash_after_click"], expected, ctx)
        self.assertEqual(steps["hash_before_reload"], expected, ctx)
        self.assertTrue(steps["reload_drawer_open"], ctx)
        self.assertIn("Decide the schema", steps["reload_name"], ctx)
        self.assertEqual(steps["reload_hash"], expected, ctx)
        gone = steps["after_escape"]
        self.assertEqual(gone["hash"], "", ctx)
        self.assertNotIn("#", gone["href"], ctx)
        self.assertTrue(gone["drawerHidden"], ctx)
        self.assertFalse(gone["inert"], ctx)

    # ---- accept: polling fallback ----

    def test_stream_disabled_polls_overview_and_lists_every_loop(self):
        poll, steps = self.poll, self.poll["steps"]
        ctx = self.ctx(poll)
        self.assertTrue(poll["board_ready"], ctx)
        for name in ALL_LOOPS:
            self.assertIn(name, steps["rows_text"], name)
        self.assertEqual(steps["row_count"], len(ALL_LOOPS), ctx)
        self.assertGreaterEqual(steps["overview_requests"], 2, ctx)
        self.assertGreaterEqual(steps["elapsed_ms"], 12000, ctx)
        self.assertIn("Polling", steps["live_after"]["title"], ctx)
        # the refused stream (503) is the browser's only console complaint
        self.assertEqual([e for e in poll["console"] if "503" not in e], [], ctx)
        self.assertEqual(poll["errors"], [], ctx)

    # ---- done criteria: the drawer's views and read/unread work ----

    def test_drawer_views_are_populated_and_inbox_read_flips(self):
        steps = self.steps
        ctx = self.ctx()
        tabs = steps["tabs"]
        for name in ("timeline", "files", "graph", "transcripts", "overview"):
            with self.subTest(view=name):
                self.assertFalse(tabs[name]["hidden"], tabs[name])
                self.assertGreater(tabs[name]["textLen"], 0, tabs[name])
        # the legacy timeline/graph views render this loop's PLAN and LOG
        self.assertIn("schema-pick", tabs["graph"]["text"] + tabs["files"]["text"] + tabs["timeline"]["text"])
        self.assertIn("planned the schema work", tabs["timeline"]["text"])
        # the Transcripts tab opened the newest session and streamed its records
        self.assertIn(TRANSCRIPT_NEEDLE, steps["transcript"]["text"], ctx)
        self.assertIn("Please pick a schema", steps["transcript"]["text"], ctx)
        self.assertEqual(steps["transcript"]["sessions"], 1, ctx)
        self.assertRegex(steps["transcript"]["sessionText"], r"lead.*(just now|\d+[smh] ago)", ctx)
        self.assertTrue(steps["controls_visible"], ctx)
        self.assertFalse(steps["unblock"]["hidden"], ctx)

        read = steps["read"]
        self.assertTrue(read["has_button"], ctx)
        self.assertIn("/api/inbox/read", read["posts"], ctx)
        self.assertRegex(read["toggle_after_read"], r"Show read \(\d+\)", ctx)
        self.assertTrue(read["has_unread_button"], ctx)
        self.assertIn("/api/inbox/unread", read["posts"], ctx)


if __name__ == "__main__":
    unittest.main()

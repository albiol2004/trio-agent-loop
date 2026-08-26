"""End-to-end regression test tying the whole slice-lifecycle feature
together through the real HTTP API (PLAN.md's ``integration-regression``
slice, GOAL.md's B1-B6).

Each layer -- ``derive_slices``/``parse_slice_verdicts`` in
metrics/trio-metrics.py, the ``/api/loop`` mode/slices/queue exposure and
inbox items in dashboard/serve.py, trio-check.py's QUEUE.md validation --
already has its own unit tests. This file's job is to prove they compose
correctly across a full slice lifecycle, especially the
post-retirement-fix path (a second ``retired:`` entry for the same slice)
that the ``retired:`` schema change exists to support.

Boots dashboard/serve.py (loaded by path, like metrics/tests/test_api_slices.py
-- serve.py delegates all slice/queue parsing to metrics/trio-metrics.py) on
a daemon thread against a temp workspace root and drives it with
urllib.request. Mailboxes are written from strings into a TemporaryDirectory
-- the repo's real ``loop*/`` directories are gitignored and absent from a
fresh checkout, which is exactly where an Evaluator runs this file.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
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
METRICS_PATH = REPO_ROOT / "metrics" / "trio-metrics.py"
TRIO_CHECK_PATH = REPO_ROOT / "metrics" / "trio-check.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_module(SERVE_PATH, "trio_dashboard_serve_lifecycle_integration")
TM = _load_module(METRICS_PATH, "trio_metrics_lifecycle_integration")


# --- shas (40 lowercase hex chars, distinct per slice/generation) -----------

SHA_A = "a1" * 20
SHA_B1 = "b1" * 20      # slice B, first retirement -- graded ITERATE
SHA_B2 = "b2" * 20      # slice B, post-fix retirement -- ungraded, then SHIP
SHA_C = "c1" * 20       # slice C, graded ITERATE, taken fault -> repairing
SHA_D = "d1" * 20       # slice D, retired, never graded
SHA_STALE_OLD = "5e" * 20
SHA_STALE_NEW = "5f" * 20


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


def _retired_block(entries: list[tuple[str, str, str]]) -> str:
    """entries: (slice_id, sha, at) tuples, in the order they should appear
    in QUEUE.md (append order -- the LAST entry per slice is "latest")."""
    lines = ["```yaml", "retired:"]
    for slice_id, sha, at in entries:
        lines.append(f"  - slice: {slice_id}")
        lines.append(f"    sha: {sha}")
        lines.append(f"    at: {at}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _faults_block(entries: list[dict]) -> str:
    lines = ["```yaml", "faults:"]
    for f in entries:
        lines.append(f"  - id: {f['id']}")
        lines.append(f"    slice: {f['slice']}")
        lines.append(f"    observed_at: {f['observed_at']}")
        lines.append(f"    scope: [{', '.join(f.get('scope') or ['a.py'])}]")
        lines.append(f"    reason: {f['reason']}")
        lines.append(f"    status: {f['status']}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _queue_text(retired: list[tuple[str, str, str]], faults: list[dict]) -> str:
    return _retired_block(retired) + _faults_block(faults)


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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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
        report: str = "",
        log: str = "- iter 1 | lead | working\n",
    ) -> Path:
        """A schema:1 v1 mailbox -- all six MAILBOX-SCHEMA.md required
        files present, so a mailbox built here also satisfies trio-check.py
        (used by the trio-check scenario below), not just /api/loop."""
        mailbox = self.root / name
        mailbox.mkdir()
        (mailbox / "GOAL.md").write_text(
            "# Test loop\n\nmission: test\n", encoding="utf-8"
        )
        (mailbox / "STATE.md").write_text(
            f"schema: 1\niteration: {iteration}\nmax_iterations: {max_iterations}\n"
            f"status: {status}\nmission: test\n",
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
        (mailbox / "REPORT.md").write_text(report, encoding="utf-8")
        return mailbox

    def detail(self, name: str) -> dict:
        query = urllib.parse.urlencode({"name": name, "root": str(self.root)})
        status, payload = _json_get(f"{self.base}/api/loop?{query}")
        self.assertEqual(status, 200, payload)
        return payload

    def slices_by_id(self, name: str) -> dict:
        return {s["id"]: s for s in self.detail(name)["slices"]}


# --- scenario 1: full lifecycle of one slice-per-state through the API -----


class FullLifecycleTests(_MailboxServerTestCase):
    def test_five_slices_map_to_five_lifecycles(self):
        self.make_mailbox(
            "loop-full-lifecycle",
            plan_slices=[
                _slice_entry("slice-a", 1, status="complete"),
                _slice_entry("slice-b", 1, status="complete"),
                _slice_entry("slice-c", 1, status="complete"),
                _slice_entry("slice-d", 1, status="complete"),
                _slice_entry("slice-e", 1, status="planned"),
            ],
            queue=_queue_text(
                retired=[
                    ("slice-a", SHA_A, "2026-08-20T10:00:00Z"),
                    ("slice-b", SHA_B1, "2026-08-20T10:05:00Z"),
                    ("slice-c", SHA_C, "2026-08-20T10:10:00Z"),
                    ("slice-d", SHA_D, "2026-08-20T10:15:00Z"),
                ],
                faults=[
                    {
                        "id": "f1", "slice": "slice-b", "observed_at": SHA_B1,
                        "scope": ["src/b.py"], "reason": "regression",
                        "status": "open",
                    },
                    {
                        "id": "f2", "slice": "slice-c", "observed_at": SHA_C,
                        "scope": ["src/c.py"], "reason": "fix in progress",
                        "status": "taken",
                    },
                ],
            ),
            verdict=(
                f"## slice slice-a @{SHA_A} — SHIP\n\ngood.\n\n"
                f"## slice slice-b @{SHA_B1} — ITERATE\n\nbroke it.\n\n"
                f"## slice slice-c @{SHA_C} — ITERATE\n\nfix in progress.\n"
            ),
        )
        by_id = self.slices_by_id("loop-full-lifecycle")
        self.assertEqual(by_id["slice-a"]["lifecycle"], "shipped")
        self.assertEqual(by_id["slice-b"]["lifecycle"], "faulted")
        self.assertEqual(by_id["slice-c"]["lifecycle"], "repairing")
        self.assertEqual(by_id["slice-d"]["lifecycle"], "retired")
        self.assertEqual(by_id["slice-e"]["lifecycle"], "planned")
        # spot-check the fault attribution that drives faulted/repairing
        self.assertEqual(by_id["slice-b"]["open_faults"], ["f1"])
        self.assertEqual(by_id["slice-c"]["open_faults"], ["f2"])
        self.assertEqual(by_id["slice-d"]["retired_sha"], SHA_D)
        self.assertIsNone(by_id["slice-d"]["verdict"])


# --- scenario 2: the post-retirement-fix path (the point of the schema) ---


class PostRetirementFixTests(_MailboxServerTestCase):
    def test_second_retired_entry_supersedes_and_reopens_grading(self):
        mailbox = self.make_mailbox(
            "loop-post-fix",
            plan_slices=[_slice_entry("slice-b", 1, status="complete")],
            queue=_queue_text(
                retired=[("slice-b", SHA_B1, "2026-08-20T10:00:00Z")],
                faults=[{
                    "id": "f1", "slice": "slice-b", "observed_at": SHA_B1,
                    "scope": ["src/b.py"], "reason": "regression",
                    "status": "open",
                }],
            ),
            verdict=f"## slice slice-b @{SHA_B1} — ITERATE\n\nbroke it.\n",
        )

        # Sanity: pre-fix state is faulted (mirrors slice B in scenario 1).
        pre = self.slices_by_id("loop-post-fix")["slice-b"]
        self.assertEqual(pre["lifecycle"], "faulted")
        self.assertEqual(pre["retired_sha"], SHA_B1)

        # The fix: a SECOND `retired:` entry for B at a new sha, while the
        # old `## slice slice-b @sha1b — ITERATE` section stays put in
        # VERDICT.md, and the fault flips from open to done.
        (mailbox / "QUEUE.md").write_text(
            _queue_text(
                retired=[
                    ("slice-b", SHA_B1, "2026-08-20T10:00:00Z"),
                    ("slice-b", SHA_B2, "2026-08-21T09:00:00Z"),
                ],
                faults=[{
                    "id": "f1", "slice": "slice-b", "observed_at": SHA_B1,
                    "scope": ["src/b.py"], "reason": "regression",
                    "status": "done",
                }],
            ),
            encoding="utf-8",
        )
        old_verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
        self.assertIn(f"## slice slice-b @{SHA_B1} — ITERATE", old_verdict_text)

        post_fix = self.slices_by_id("loop-post-fix")["slice-b"]
        self.assertEqual(post_fix["retired_sha"], SHA_B2)
        self.assertEqual(post_fix["superseded"], [SHA_B1])
        # the new sha is ungraded -> verdict is None again, back to
        # retired/pending, not faulted and not shipped
        self.assertIsNone(post_fix["verdict"])
        self.assertEqual(post_fix["open_faults"], [])
        self.assertEqual(post_fix["lifecycle"], "retired")
        # the OLD verdict section is still there, untouched
        self.assertIn(
            f"## slice slice-b @{SHA_B1} — ITERATE",
            (mailbox / "VERDICT.md").read_text(encoding="utf-8"),
        )

        # The Evaluator grades the new sha: append a SHIP section for it.
        with (mailbox / "VERDICT.md").open("a", encoding="utf-8") as fh:
            fh.write(f"\n## slice slice-b @{SHA_B2} — SHIP\n\nfix landed cleanly.\n")

        shipped = self.slices_by_id("loop-post-fix")["slice-b"]
        self.assertEqual(shipped["lifecycle"], "shipped")
        self.assertEqual(shipped["verdict"], "SHIP")
        self.assertEqual(shipped["retired_sha"], SHA_B2)
        self.assertEqual(shipped["superseded"], [SHA_B1])
        self.assertEqual(shipped["open_faults"], [])


# --- scenario 3: stale_candidates propagate through the API ----------------


class StaleCandidateTests(_MailboxServerTestCase):
    def test_open_fault_observed_at_superseded_sha_is_stale_candidate(self):
        self.make_mailbox(
            "loop-stale",
            plan_slices=[_slice_entry("slice-a", 1, status="complete")],
            queue=_queue_text(
                retired=[
                    ("slice-a", SHA_STALE_OLD, "2026-08-20T10:00:00Z"),
                    ("slice-a", SHA_STALE_NEW, "2026-08-21T09:00:00Z"),
                ],
                faults=[{
                    "id": "f1", "slice": "slice-a", "observed_at": SHA_STALE_OLD,
                    "scope": ["src/a.py"], "reason": "regression at old sha",
                    "status": "open",
                }],
            ),
            verdict=f"## slice slice-a @{SHA_STALE_NEW} — SHIP\n\nfix landed.\n",
        )
        a = self.slices_by_id("loop-stale")["slice-a"]
        self.assertEqual(a["superseded"], [SHA_STALE_OLD])
        self.assertEqual(a["stale_candidates"], ["f1"])
        # the fault is still `open`, so the slice stays faulted even though
        # the latest verdict is SHIP -- a stale open fault is not a free pass
        self.assertEqual(a["lifecycle"], "faulted")
        self.assertEqual(a["open_faults"], ["f1"])


# --- scenario 4: lockstep mailboxes are untouched by the open-loop path ----


class LockstepUntouchedTests(_MailboxServerTestCase):
    def test_no_queue_md_yields_lockstep_mode_and_matches_derive_iterations(self):
        mailbox = self.make_mailbox(
            "loop-lockstep-untouched",
            iteration=2,
            max_iterations=3,
            status="running",
            plan_slices=[
                _slice_entry("x", 1, status="complete"),
                _slice_entry("y", 2, status="in_progress"),
                _slice_entry("z", 2, status="planned"),
            ],
            queue=None,  # no QUEUE.md at all
            verdict="VERDICT: SHIP\n",
            log=(
                "- iter 1 | lead | working\n"
                "- iter 1 | evaluator | VERDICT: SHIP\n"
                "- iter 2 | lead | working\n"
            ),
        )
        self.assertFalse((mailbox / "QUEUE.md").exists())

        card = self.detail("loop-lockstep-untouched")
        self.assertEqual(card["mode"], "lockstep")
        self.assertNotIn("queue", card)

        allowed = {"planned", "building", "shipped"}
        for sl in card["slices"]:
            self.assertIn(sl["lifecycle"], allowed, sl)
            # open-loop-only fields stay empty/None for lockstep slices
            self.assertIsNone(sl["retired_sha"])
            self.assertIsNone(sl["verdict"])
            self.assertEqual(sl["open_faults"], [])
            self.assertEqual(sl["superseded"], [])

        # The open-loop override must not leak into iteration derivation:
        # replay the SAME already-parsed inputs through derive_iterations
        # directly and compare exactly against what the API returned.
        state = TM.parse_state(mailbox / "STATE.md")
        timeline = TM.parse_timeline(mailbox / "LOG.md")
        plan_text = (mailbox / "PLAN.md").read_text(encoding="utf-8")
        slices = TM.parse_slices_block(plan_text)
        verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
        report_text = (mailbox / "REPORT.md").read_text(encoding="utf-8")
        expected = TM.derive_iterations(
            state, timeline, slices,
            verdict_text=verdict_text, report_text=report_text,
        )

        self.assertEqual(card["iterations"], expected)
        # and, explicitly, the lifecycle values line up 1:1 by iteration n
        actual_lifecycles = {it["n"]: it["lifecycle"] for it in card["iterations"]}
        expected_lifecycles = {it["n"]: it["lifecycle"] for it in expected}
        self.assertEqual(actual_lifecycles, expected_lifecycles)


# --- scenario 5: trio-check accepts a repeat-retired-entry fixture ---------


class TrioCheckAcceptsOpenLoopFixtureTests(unittest.TestCase):
    """Independent of the HTTP server: builds its own temp v1 open-loop
    mailbox (repeat `retired:` entries for one slice, at distinct shas,
    exactly the post-retirement-fix shape scenario 2 exercises through the
    API) on disk and runs the real trio-check.py CLI against it."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.workspace.cleanup()

    def test_trio_check_exits_zero_on_repeat_retired_entry_mailbox(self):
        root = Path(self.workspace.name)
        mailbox = root / "loop-trio-check-fixture"
        mailbox.mkdir()
        (mailbox / "GOAL.md").write_text(
            "# Trio-check fixture\n\nmission: exercise repeat retired: "
            "entries.\n",
            encoding="utf-8",
        )
        (mailbox / "STATE.md").write_text(
            "schema: 1\niteration: 1\nmax_iterations: 3\nstatus: running\n"
            "mission: exercise repeat retired: entries\n",
            encoding="utf-8",
        )
        (mailbox / "LOG.md").write_text(
            "- iter 1 | lead | retired B, evaluator found a fault, fixed, "
            "retired again\n",
            encoding="utf-8",
        )
        (mailbox / "PLAN.md").write_text(
            _plan_slices_yaml([_slice_entry("slice-b", 1, status="complete")]),
            encoding="utf-8",
        )
        (mailbox / "QUEUE.md").write_text(
            _queue_text(
                retired=[
                    ("slice-b", SHA_B1, "2026-08-20T10:00:00Z"),
                    ("slice-b", SHA_B2, "2026-08-21T09:00:00Z"),
                ],
                faults=[{
                    "id": "f1", "slice": "slice-b", "observed_at": SHA_B1,
                    "scope": ["src/b.py"], "reason": "regression",
                    "status": "done",
                }],
            ),
            encoding="utf-8",
        )
        (mailbox / "VERDICT.md").write_text(
            f"## slice slice-b @{SHA_B1} — ITERATE\n\nbroke it.\n\n"
            f"## slice slice-b @{SHA_B2} — SHIP\n\nfix landed cleanly.\n",
            encoding="utf-8",
        )
        (mailbox / "REPORT.md").write_text("", encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(TRIO_CHECK_PATH), str(mailbox)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"trio-check.py failed on a repeat-retired-entry mailbox:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("Result: PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()

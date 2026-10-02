"""GOAL DoD2: free-form STATE.md status words are normalised by a status
map; unmapped words are shown verbatim, never silently ``unknown``."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location(
    "loop_actions_status_map", REPO_ROOT / "dashboard" / "loop_actions.py")
la = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = la
_spec.loader.exec_module(la)

FAMILIES = ("finished", "between_roles", "ready", "paused", "running", "terminal", "unmapped")


class StatusMapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def derive(self, status, *, sources=()):
        mailbox = Path(self._tmp.name) / "mailbox"
        mailbox.mkdir(exist_ok=True)
        text = "# STATE\n" + (f"status: {status}\n" if status is not None else "")
        (mailbox / "STATE.md").write_text(text)
        return la.derive_state(mailbox, list(sources), home=self.home)

    def test_finished_words(self):  # accept 1
        for word in ("pushed", "done", "complete", "delivered-dev", "delivered", "merged",
                     "landed", "completed", "finished", "closed", "retired", "shipped-dev"):
            with self.subTest(word=word):
                out = self.derive(word)
                self.assertEqual(out["state"], "finished")
                self.assertEqual(out["status_family"], "finished")
                self.assertEqual(out["status_raw"], word)
        self.assertEqual(self.derive("pushed")["summary"], "STATE.md status pushed (finished)")

    def test_between_roles_words(self):  # accept 2
        for word in ("awaiting-evaluator", "lead-done", "awaiting_eval", "eval_pending",
                     "evaluator-pending", "awaiting-lead", "between-roles", "lead-done-iter-2"):
            with self.subTest(word=word):
                out = self.derive(word)
                self.assertEqual(out["state"], "between_roles")
                self.assertEqual(out["status_family"], "between_roles")

    def test_ready_and_paused(self):
        for word, state in (("ready", "ready"), ("idle", "ready"), (None, "ready"),
                            ("paused", "paused"), ("stopped", "paused"), ("on-hold", "paused")):
            with self.subTest(word=word):
                out = self.derive(word)
                self.assertEqual((out["state"], out["status_family"]), (state, state))

    def test_unmapped_word_is_verbatim(self):  # accept 3
        out = self.derive("frobnicate")
        self.assertEqual((out["state"], out["status_raw"], out["status_family"]),
                         ("unmapped", "frobnicate", "unmapped"))
        self.assertEqual(out["summary"], "STATE.md status frobnicate (not in the status map)")
        out = self.derive("Frobnicate")
        self.assertEqual(out["state"], "unmapped")
        self.assertEqual(out["status_raw"], "Frobnicate")
        out = self.derive("Frobnicate extra words")
        self.assertEqual(out["status_raw"], "Frobnicate extra words")

    def test_status_raw_truncated_and_absent(self):
        self.assertEqual(len(self.derive("x" * 200)["status_raw"]), 80)
        self.assertIsNone(self.derive(None)["status_raw"])

    def test_shipped_and_running_unchanged(self):  # accept 4
        out = self.derive("shipped")
        self.assertEqual((out["state"], out["status_family"]), ("shipped", "finished"))
        out = self.derive("running")
        self.assertEqual((out["state"], out["status_family"]), ("interrupted", "running"))
        out = self.derive("running", sources=["heartbeat"])
        self.assertEqual((out["state"], out["status_family"]), ("running", "running"))
        out = self.derive("needs-human")
        self.assertEqual((out["state"], out["status_family"]), ("needs_human", "terminal"))
        out = self.derive("blocked")
        self.assertEqual((out["state"], out["status_family"]), ("blocked", "terminal"))

    def test_live_evidence_wins_over_status_map(self):
        out = self.derive("done", sources=["native-runs"])
        self.assertEqual(out["state"], "running")
        self.assertEqual(out["status_raw"], "done")

    def test_normalize_status(self):
        n = la.normalize_status
        self.assertEqual(n("delivered_dev"), "finished")
        self.assertEqual(n("shipped"), "shipped")
        self.assertEqual(n("lead_done_x"), "between_roles")
        self.assertEqual(n(""), "ready")
        self.assertIsNone(n("frobnicate"))
        self.assertEqual(la.status_word({"status": "Delivered-Dev now"}), "delivered_dev")

    def test_never_unknown_for_readable_words(self):  # accept 5
        words = ["done", "pushed", "complete", "delivered-dev", "lead-done", "awaiting-evaluator",
                 "shipped", "running", "idle", "paused", "wip", "frobnicate", "Frobnicate",
                 "merged", "landed", "closed", "retired", "ready", "stopped", "on-hold",
                 "blocked", "needs-human", "error", "needs_land", "needs_retirement",
                 "in-progress", "active", "iterating", "ship", "weird_status!", "42", "unchanged",
                 "lead-running", "evaluator-running"]
        for word in words:
            with self.subTest(word=word):
                out = self.derive(word)
                self.assertNotEqual(out["state"], "unknown")
                self.assertIn(out["state"], la.STATES)
                self.assertIn(out["status_family"], FAMILIES)
                self.assertEqual(out["status_raw"], word)


if __name__ == "__main__":
    unittest.main()

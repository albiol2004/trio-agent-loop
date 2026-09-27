"""Tests for the threaded open-loop driver (metrics/trio_loop.py).

Every test drives run_open_loop / run_loop with fake, in-process runners --
no subprocess, no network, no real harness. The fake Lead/Evaluator runners
below write real QUEUE.md `retired:`/`faults:` entries and real VERDICT.md
sections/first-line verdicts, exactly like the real roles would, so the
engine's QUEUE.md/VERDICT.md-driven state machine is exercised for real.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import threading
from pathlib import Path

from metrics import trio_loop

PLAN_TWO_SLICES = """\
```yaml
slices:
  - id: alpha
    writes: [loop/STATE.md, "api:Alpha"]
    reads: []
  - id: beta
    writes: [loop/STATE.md, "api:Beta"]
    reads: []
```
"""

PLAN_ONE_SLICE = """\
```yaml
slices:
  - id: solo
    writes: [loop/STATE.md, "api:Solo"]
    reads: []
```
"""

PLAN_MALFORMED = """\
```yaml
slices:
  - id: bogus
    bogus_key: nope
```
"""

EMPTY_QUEUE = "```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n"


def fake_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def make_open_loop_mailbox(
    parent: Path, plan: str, queue_text: str = EMPTY_QUEUE
) -> Path:
    """Create the smallest open-loop mailbox (has QUEUE.md)."""
    mailbox = parent / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    (mailbox / "PLAN.md").write_text(plan, encoding="utf-8")
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    (mailbox / "QUEUE.md").write_text(queue_text, encoding="utf-8")
    return mailbox


class QueueModel:
    """In-memory QUEUE.md model a scripted runner mutates and flushes.

    A real Lead/Evaluator role writes QUEUE.md directly; this stand-in does
    the same so the engine's own read_queue()-driven logic is exercised.
    Guarded by a shared lock since the Lead thread and the Evaluator (this
    test's calling thread) can both be mutating QUEUE.md concurrently.
    """

    def __init__(self, mailbox: Path, lock: threading.Lock) -> None:
        self.mailbox = mailbox
        self.lock = lock
        self.retired: list[dict] = []
        self.faults: list[dict] = []

    def retire(self, slice_id: str, sha: str, at: str = "2026-01-01T00:00:00Z") -> None:
        with self.lock:
            self.retired.append({"slice": slice_id, "sha": sha, "at": at})
            self._flush()

    def add_fault(
        self,
        fault_id: str,
        slice_id: str,
        observed_at: str,
        reason: str,
        status: str = "open",
        scope: str = "[]",
    ) -> None:
        """`scope` is written verbatim after `scope: ` -- pass a plain
        value (`local:a.py`, `design`) to write it the way live evaluators
        do."""
        with self.lock:
            self.faults.append(
                {
                    "id": fault_id,
                    "slice": slice_id,
                    "observed_at": observed_at,
                    "scope": scope,
                    "reason": reason,
                    "status": status,
                }
            )
            self._flush()

    def set_fault_status(self, fault_id: str, status: str) -> None:
        with self.lock:
            for fault in self.faults:
                if fault["id"] == fault_id:
                    fault["status"] = status
            self._flush()

    def _flush(self) -> None:
        lines = ["```yaml", "retired:"]
        for entry in self.retired:
            lines += [
                f"  - slice: {entry['slice']}",
                f"    sha: {entry['sha']}",
                f"    at: {entry['at']}",
            ]
        lines.append("```")
        lines.append("")
        lines.append("```yaml")
        lines.append("faults:")
        for fault in self.faults:
            lines += [
                f"  - id: {fault['id']}",
                f"    slice: {fault['slice']}",
                f"    observed_at: {fault['observed_at']}",
                f"    scope: {fault['scope']}",
                f"    reason: {fault['reason']}",
                f"    status: {fault['status']}",
            ]
        lines.append("```")
        (self.mailbox / "QUEUE.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


class VerdictModel:
    """In-memory helper for the two VERDICT.md shapes the engine reads:
    per-slice `## slice <id> @<sha> -- SHIP|ITERATE` sections, and the
    first-line integration verdict."""

    def __init__(self, mailbox: Path, lock: threading.Lock) -> None:
        self.mailbox = mailbox
        self.lock = lock

    def append_slice_section(self, slice_id: str, sha: str, verdict: str) -> None:
        with self.lock:
            path = self.mailbox / "VERDICT.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"## slice {slice_id} @{sha} -- {verdict}\n"
            path.write_text(text, encoding="utf-8")

    def set_integration_verdict(self, line: str) -> None:
        with self.lock:
            path = self.mailbox / "VERDICT.md"
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            path.write_text(line + "\n" + existing, encoding="utf-8")


class ScriptedLeadRunner:
    """Fake Lead runner: each call pops and runs the next scripted action."""

    def __init__(self, passes) -> None:
        self.passes = list(passes)
        self.calls: list[dict] = []

    def run(self, role, iteration, mailbox, context=None):
        assert role == "lead"
        assert context == {
            "mode": "open-loop",
            "slice": None,
            "sha": None,
            "kind": "lead-pass",
        }
        self.calls.append({"iteration": iteration, "context": dict(context)})
        assert self.passes, "lead runner invoked more times than scripted"
        action = self.passes.pop(0)
        action(mailbox)
        return 0


class ScriptedEvalRunner:
    """Fake Evaluator runner dispatching on context['kind'].

    Thread-safe: with --slice-eval-concurrency > 1 several slice-evals run
    on worker threads at once, so pops/appends happen under a lock (the
    scripted action itself runs outside it, so actions may block).
    """

    def __init__(self, slice_actions=None, integration_actions=None) -> None:
        self.slice_actions = dict(slice_actions or {})
        self.integration_actions = list(integration_actions or [])
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def run(self, role, iteration, mailbox, context=None):
        assert role == "evaluator"
        kind = context["kind"]
        with self._lock:
            self.calls.append({"iteration": iteration, "context": dict(context)})
            if kind == "slice-eval":
                action = self.slice_actions.pop((context["slice"], context["sha"]))
            elif kind == "integration-eval":
                assert self.integration_actions, (
                    "integration eval invoked more times than scripted"
                )
                action = self.integration_actions.pop(0)
            else:
                action = None
        if kind in ("slice-eval", "integration-eval"):
            action(mailbox)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected kind {kind!r}")
        return 0


class RaisingLeadRunner:
    """Fake Lead runner whose single call raises, for the no-hang test."""

    def run(self, role, iteration, mailbox, context=None):
        raise RuntimeError("lead runner exploded")


# --- Lead-pass output verification --------------------------------------
# A runner exit of 0 is not proof the Lead pass wrote anything: mirrors the
# existing Evaluator output-verification rule for the Lead side.


def test_empty_lead_pass_then_productive_pass_counts_as_one_iteration(
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-empty-then-real")

    def empty_pass(mb):
        pass  # exit 0, writes nothing at all -- a runner blip

    def real_pass(mb):
        queue.retire("solo", sha1)

    lead = ScriptedLeadRunner([empty_pass, real_pass])

    def eval_solo(mb):
        verdict.append_slice_section("solo", sha1, "SHIP")

    def integration_ship(mb):
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo},
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    assert not lead.passes
    # Both calls happened, but only the productive one bumped iteration.
    assert len(lead.calls) == 2
    assert lead.calls[0]["iteration"] == 1
    assert lead.calls[1]["iteration"] == 1
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "iteration: 1" in state_text
    assert "status: shipped" in state_text
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert log_text.count("open-loop: lead pass made no changes") == 1
    assert "open-loop: lead pass made no changes (attempt 1)" in log_text


def test_lead_pass_never_changing_anything_errors_after_three_attempts(
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    evaluator = ScriptedEvalRunner()

    class NeverWritesLeadRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def run(self, role, iteration, mailbox, context=None):
            assert role == "lead"
            self.calls.append({"iteration": iteration, "context": dict(context)})
            return 0

    lead = NeverWritesLeadRunner()

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 3
    assert len(lead.calls) == 3
    assert evaluator.calls == []
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text
    assert "iteration: 0" in state_text
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    for n in (1, 2, 3):
        assert f"open-loop: lead pass made no changes (attempt {n})" in log_text
    assert "open-loop: lead pass made no changes after 3 attempts" in log_text


# --- C1 happy path -----------------------------------------------------


def test_c1_happy_path_ships_after_a_fault_and_fix(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_TWO_SLICES)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)

    sha_a1 = fake_sha("alpha-1")
    sha_b1 = fake_sha("beta-1")
    sha_b2 = fake_sha("beta-2")

    def pass1(mb):
        queue.retire("alpha", sha_a1)
        queue.retire("beta", sha_b1)

    def pass2(mb):
        queue.set_fault_status("f1", "done")
        queue.retire("beta", sha_b2)

    lead = ScriptedLeadRunner([pass1, pass2])

    def eval_alpha(mb):
        verdict.append_slice_section("alpha", sha_a1, "SHIP")

    def eval_beta_1(mb):
        verdict.append_slice_section("beta", sha_b1, "ITERATE")
        queue.add_fault("f1", "beta", sha_b1, "needs a fix")

    def eval_beta_2(mb):
        verdict.append_slice_section("beta", sha_b2, "SHIP")

    def integration_ship(mb):
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={
            ("alpha", sha_a1): eval_alpha,
            ("beta", sha_b1): eval_beta_1,
            ("beta", sha_b2): eval_beta_2,
        },
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "iteration: 2" in state_text
    assert "status: shipped" in state_text
    assert re.search(r"^verdict: SHIP$", state_text, re.MULTILINE)

    assert not lead.passes
    assert not evaluator.integration_actions
    assert not evaluator.slice_actions

    session = json.loads((mailbox / ".session.json").read_text(encoding="utf-8"))
    assert session["phase"] == "done"
    assert session["open_loop"] is True
    assert session["lead_alive"] is False
    assert session["eval_alive"] is False
    assert "started_at" in session

    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert driver["open_loop"] is True
    assert driver["phase"] == "done"
    assert driver["lead_alive"] is False
    assert driver["eval_alive"] is False


# --- plain-scope faults gate the integration eval -------------------------


def test_open_fault_with_plain_scope_blocks_integration_until_done(
    tmp_path: Path,
) -> None:
    """Regression for the openrouter/L run: evaluators write fault scopes as
    plain values (`scope: local:<paths>`, `scope: design`). The parser used
    to reject those, `read_queue` swallowed the error and returned no
    faults, so `_slices_fully_retired` saw every slice retired and no fault
    open and the integration-eval started while f1 was still open. Now the
    open plain-scope fault keeps `_slices_fully_retired` False (both in the
    Lead thread and in the driver's integration gate re-check), the Lead
    gets another pass to fix it, and the integration-eval only runs once f1
    is `done`."""
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_TWO_SLICES)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)

    sha_a1 = fake_sha("plain-alpha-1")
    sha_b1 = fake_sha("plain-beta-1")
    sha_b2 = fake_sha("plain-beta-2")

    def pass1(mb):
        queue.retire("alpha", sha_a1)
        queue.retire("beta", sha_b1)

    def pass2(mb):
        # The Lead only gets this pass because f1 is visible as open.
        queue.set_fault_status("f1", "done")
        queue.set_fault_status("f2", "done")
        queue.retire("beta", sha_b2)

    lead = ScriptedLeadRunner([pass1, pass2])

    def eval_alpha(mb):
        verdict.append_slice_section("alpha", sha_a1, "ITERATE")
        queue.add_fault(
            "f2", "alpha", sha_a1, "cross-slice contract", scope="design"
        )
        queue.set_fault_status("f2", "done")  # a stale design fault

    def eval_beta_1(mb):
        verdict.append_slice_section("beta", sha_b1, "ITERATE")
        queue.add_fault(
            "f1", "beta", sha_b1, "route test red on merged tree",
            scope="local:api/test/beta-route.test.ts",
        )

    def eval_beta_2(mb):
        verdict.append_slice_section("beta", sha_b2, "SHIP")

    seen_at_integration: list[list[tuple]] = []

    def integration_ship(mb):
        parsed = trio_loop._METRICS.read_queue(mb)
        seen_at_integration.append(
            [(f["id"], f["status"], f["scope"]) for f in parsed["faults"]]
        )
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={
            ("alpha", sha_a1): eval_alpha,
            ("beta", sha_b1): eval_beta_1,
            ("beta", sha_b2): eval_beta_2,
        },
        integration_actions=[integration_ship],
    )

    # The live QUEUE.md shape parses: plain scopes are visible to the gate.
    queue.retire("alpha", sha_a1)
    queue.add_fault(
        "f1", "beta", sha_b1, "r", scope="local:api/test/beta-route.test.ts"
    )
    parsed = trio_loop._METRICS.read_queue(mailbox)
    assert parsed.get("errors", []) == []
    open_or_taken = [f for f in parsed["faults"] if f["status"] == "open"]
    assert [f["scope"] for f in open_or_taken] == [["api/test/beta-route.test.ts"]]
    assert not trio_loop._slices_fully_retired(
        ["alpha"], {"alpha"}, open_or_taken
    )
    queue.retired.clear()
    queue.faults.clear()
    (mailbox / "QUEUE.md").write_text(EMPTY_QUEUE, encoding="utf-8")

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    assert not lead.passes, "the Lead must get a fix pass for the open fault"
    assert seen_at_integration == [
        [
            ("f2", "done", ["design"]),
            ("f1", "done", ["api/test/beta-route.test.ts"]),
        ]
    ]
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "QUEUE.md parse error" not in log_text


def test_malformed_fault_is_logged_once_and_valid_faults_still_gate(
    tmp_path: Path,
) -> None:
    """A malformed fault entry is dropped with a LOG.md line (once per
    turn, not once per poll) while a valid open fault beside it still
    blocks the integration-eval until the Lead marks it done."""
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("malformed-solo-1")
    sha2 = fake_sha("malformed-solo-2")

    def pass1(mb):
        queue.retire("solo", sha1)

    def pass2(mb):
        queue.set_fault_status("f1", "done")
        queue.retire("solo", sha2)

    lead = ScriptedLeadRunner([pass1, pass2])

    def eval_solo_1(mb):
        verdict.append_slice_section("solo", sha1, "ITERATE")
        queue.add_fault("f1", "solo", sha1, "broken", scope="local:src/solo.py")
        # f9 has an unterminated quote in its scope list -> dropped.
        queue.add_fault("f9", "solo", sha1, "junk", status="done",
                        scope='["unterminated]')

    def eval_solo_2(mb):
        verdict.append_slice_section("solo", sha2, "SHIP")

    integration_calls: list[list[tuple]] = []

    def integration_ship(mb):
        parsed = trio_loop._METRICS.read_queue(mb)
        integration_calls.append(
            [(f["id"], f["status"]) for f in parsed["faults"]]
        )
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo_1, ("solo", sha2): eval_solo_2},
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    assert not lead.passes
    assert integration_calls == [[("f1", "done")]]
    log_lines = [
        ln for ln in (mailbox / "LOG.md").read_text(encoding="utf-8").splitlines()
        if "QUEUE.md parse error" in ln
    ]
    assert log_lines, "the dropped fault must be surfaced in LOG.md"
    assert all("| loop | QUEUE.md parse error: `faults:` block:" in ln
               for ln in log_lines)
    # At most one line per turn (iteration), never one per poll.
    iters = [re.match(r"- iter (\d+) ", ln).group(1) for ln in log_lines]
    assert len(iters) == len(set(iters))


# --- VERDICT.md clobber guard -------------------------------------------


def test_open_loop_restores_verdict_sections_clobbered_by_evaluator_rewrite(
    tmp_path: Path,
) -> None:
    """Reproduces the real open-loop run's defect: an Evaluator session
    rewrites VERDICT.md whole instead of appending, dropping earlier
    per-slice sections -- once for a slice-eval, again for the
    integration-eval. The driver must restore every section that went
    missing, in both cases, and log one restore line per occurrence."""
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_TWO_SLICES)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha_a = fake_sha("alpha-clobber")
    sha_b = fake_sha("beta-clobber")

    lead = ScriptedLeadRunner([
        lambda mb: (queue.retire("alpha", sha_a), queue.retire("beta", sha_b)),
    ])

    def eval_alpha(mb):
        verdict.append_slice_section("alpha", sha_a, "SHIP")

    def eval_beta_clobbers(mb):
        # Rewrites the whole file with only its own new section, dropping
        # alpha's section that was already there -- the observed bug.
        (mailbox / "VERDICT.md").write_text(
            f"## slice beta @{sha_b} -- SHIP\n", encoding="utf-8"
        )

    def integration_clobbers(mb):
        # Rewrites the whole file again, dropping every per-slice section.
        (mailbox / "VERDICT.md").write_text("VERDICT: SHIP\n", encoding="utf-8")

    evaluator = ScriptedEvalRunner(
        slice_actions={
            ("alpha", sha_a): eval_alpha,
            ("beta", sha_b): eval_beta_clobbers,
        },
        integration_actions=[integration_clobbers],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert verdict_text.splitlines()[0] == "VERDICT: SHIP"
    assert re.search(
        rf"^## slice alpha @{sha_a} -- SHIP$", verdict_text, re.MULTILINE
    )
    assert re.search(
        rf"^## slice beta @{sha_b} -- SHIP$", verdict_text, re.MULTILINE
    )

    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert (
        "open-loop: restored 1 clobbered per-slice section(s) in "
        "VERDICT.md after slice-eval" in log_text
    )
    assert (
        "open-loop: restored 2 clobbered per-slice section(s) in "
        "VERDICT.md after integration-eval" in log_text
    )


def test_open_loop_leaves_verdict_untouched_when_evaluator_appends_correctly(
    tmp_path: Path,
) -> None:
    """No clobber, no rewrite: the restore guard must be a no-op (the file
    ends up byte-identical to what plain appends would have produced)."""
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-clean")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    def eval_solo(mb):
        verdict.append_slice_section("solo", sha1, "SHIP")

    def integration_ship(mb):
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo},
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    verdict_text = (mailbox / "VERDICT.md").read_text(encoding="utf-8")
    assert verdict_text == f"VERDICT: SHIP\n## slice solo @{sha1} -- SHIP\n"
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "restored" not in log_text


# --- ITERATE wakes the Lead ---------------------------------------------


def test_iterate_integration_verdict_wakes_the_lead(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-1")

    def pass1(mb):
        queue.retire("solo", sha1)

    def pass2(mb):
        # A forced full Lead pass with nothing new to retire -- still a
        # real on-disk write (a PLAN.md note), so it counts as productive
        # under the Lead-pass output-verification snapshot.
        path = mb / "PLAN.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n<!-- reviewed -->\n",
            encoding="utf-8",
        )

    lead = ScriptedLeadRunner([pass1, pass2])

    def eval_solo(mb):
        verdict.append_slice_section("solo", sha1, "SHIP")

    def integration_iterate(mb):
        verdict.set_integration_verdict("VERDICT: ITERATE")

    def integration_ship(mb):
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo},
        integration_actions=[integration_iterate, integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    assert len(lead.calls) == 2
    assert lead.calls[0]["iteration"] == 1
    assert lead.calls[1]["iteration"] == 2
    assert not evaluator.integration_actions
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "iteration: 2" in state_text
    assert "status: shipped" in state_text
    # An earlier ITERATE integration verdict wrote `verdict: ITERATE`; the
    # later SHIP must replace it in place, not append a second line.
    verdict_lines = re.findall(r"^verdict: (\S+)$", state_text, re.MULTILINE)
    assert verdict_lines == ["SHIP"]


# --- NEEDS_HUMAN halts with exit 5 --------------------------------------


def test_needs_human_integration_verdict_returns_five(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-needs-human")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = ScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): lambda mb: verdict.append_slice_section(
                "solo", sha1, "SHIP"
            )
        },
        integration_actions=[
            lambda mb: verdict.set_integration_verdict("VERDICT: NEEDS_HUMAN")
        ],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 5
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: needs_human" in state_text
    assert re.search(r"^verdict: NEEDS_HUMAN$", state_text, re.MULTILINE)


# --- per-slice gate exit 2 -> status: error -----------------------------


def test_per_slice_gate_error_sets_status_error(
    tmp_path: Path, monkeypatch
) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    sha1 = fake_sha("solo-gate-error")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])
    evaluator = ScriptedEvalRunner()

    monkeypatch.setattr(trio_loop, "_per_slice_gate", lambda *a, **k: 2)

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 3
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text
    # The gate rejected before any evaluator call was ever dispatched.
    assert evaluator.calls == []


# --- fresh mailbox: empty PLAN.md must not look "already retired" -------


def test_fresh_mailbox_runs_lead_before_any_integration_eval(
    tmp_path: Path,
) -> None:
    """On a fresh open-loop mailbox (QUEUE.md present, PLAN.md empty), the
    Lead must get a first pass -- writing the plan and retiring the slice
    -- before the driver ever considers running the integration eval.
    Regression for the bug where 0 declared slices + 0 retired looked like
    "all retired" and the Lead runner was never invoked at all.
    """
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, "")  # PLAN.md exists but empty
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-fresh")

    def pass1(mb: Path) -> None:
        # The Lead is what writes the plan in the first place.
        (mb / "PLAN.md").write_text(PLAN_ONE_SLICE, encoding="utf-8")
        queue.retire("solo", sha1)

    lead = ScriptedLeadRunner([pass1])

    def eval_solo(mb: Path) -> None:
        verdict.append_slice_section("solo", sha1, "SHIP")

    def integration_ship(mb: Path) -> None:
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo},
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    assert len(lead.calls) == 1
    # Exactly one slice-eval then one integration-eval -- never an
    # integration-eval run against the empty, plan-less mailbox.
    assert [c["context"]["kind"] for c in evaluator.calls] == [
        "slice-eval",
        "integration-eval",
    ]


# --- malformed slices: block must not look "already retired" ------------


def test_malformed_slices_block_blocks_integration_eval_and_logs(
    tmp_path: Path,
) -> None:
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_MALFORMED)

    # Nothing the Lead does can ever satisfy "all declared slices retired"
    # -- the block never parses -- so the driver must cap on the iteration
    # budget instead of mistaking the unreadable plan for "done" and
    # running an integration eval. Each pass does write something real (a
    # PLAN.md note, still unparseable as a slices block) so it counts as
    # productive under the Lead-pass output-verification snapshot and
    # actually consumes the budget, without retiring anything that would
    # send the grading loop looking for a scripted evaluator action.
    def make_pass(note: str):
        def _pass(mb):
            (mb / "PLAN.md").write_text(PLAN_MALFORMED + f"<!-- {note} -->\n")

        return _pass

    lead = ScriptedLeadRunner([make_pass("pass-1"), make_pass("pass-2")])
    evaluator = ScriptedEvalRunner()

    code = trio_loop.run_open_loop(mailbox, 2, lead, evaluator, poll_seconds=0.01)

    assert code == 4
    assert evaluator.calls == []
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "open-loop: PLAN.md slices block unreadable" in log_text
    # Logged once, not once per re-check poll.
    assert log_text.count("open-loop: PLAN.md slices block unreadable") == 1


# --- gate-exit-1 slice must not satisfy the termination predicate -------


def test_gate_exit_one_blocks_termination_until_regraded(
    tmp_path: Path, monkeypatch
) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-gate-blocked")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    gate_calls = {"n": 0}

    def fake_gate(mb, repo, slice_id):
        gate_calls["n"] += 1
        # Missing commits (skipped/ungraded) for the first two checks,
        # then the commits show up.
        return 1 if gate_calls["n"] <= 2 else 0

    monkeypatch.setattr(trio_loop, "_per_slice_gate", fake_gate)

    def eval_solo(mb: Path) -> None:
        verdict.append_slice_section("solo", sha1, "SHIP")

    def integration_ship(mb: Path) -> None:
        verdict.set_integration_verdict("VERDICT: SHIP")

    evaluator = ScriptedEvalRunner(
        slice_actions={("solo", sha1): eval_solo},
        integration_actions=[integration_ship],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    # The gate rejected "solo" (retired but ungraded) at least twice --
    # the driver must not have run the integration eval while it sat
    # gate-blocked, even though the sole declared slice already had a
    # retired QUEUE.md entry the whole time.
    assert gate_calls["n"] >= 3
    assert [c["context"]["kind"] for c in evaluator.calls] == [
        "slice-eval",
        "integration-eval",
    ]


# --- output verification: a runner exit of 0 is not proof of a write ----


class RetryScriptedEvalRunner:
    """Like ScriptedEvalRunner, but slice_actions/integration_actions may
    each script MULTIPLE actions for the same key/kind, consumed one per
    call -- for testing the open-loop output-verification retry path
    (a runner that returns 0 without writing anything, possibly more than
    once in a row, e.g. a runner blip)."""

    def __init__(self, slice_actions=None, integration_actions=None) -> None:
        self.slice_actions = {
            key: list(actions) for key, actions in (slice_actions or {}).items()
        }
        self.integration_actions = list(integration_actions or [])
        self.calls: list[dict] = []

    def run(self, role, iteration, mailbox, context=None):
        assert role == "evaluator"
        self.calls.append({"iteration": iteration, "context": dict(context)})
        kind = context["kind"]
        if kind == "slice-eval":
            key = (context["slice"], context["sha"])
            actions = self.slice_actions[key]
            assert actions, f"slice-eval for {key} invoked more times than scripted"
            actions.pop(0)(mailbox)
        elif kind == "integration-eval":
            assert self.integration_actions, (
                "integration eval invoked more times than scripted"
            )
            self.integration_actions.pop(0)(mailbox)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected kind {kind!r}")
        return 0


def test_slice_eval_no_write_retries_then_grades_and_ships(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-blip-once")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = RetryScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): [
                lambda mb: None,  # exit 0, writes nothing -- a runner blip
                lambda mb: verdict.append_slice_section("solo", sha1, "SHIP"),
            ]
        },
        integration_actions=[
            lambda mb: verdict.set_integration_verdict("VERDICT: SHIP")
        ],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    slice_calls = [c for c in evaluator.calls if c["context"]["kind"] == "slice-eval"]
    assert len(slice_calls) == 2
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert log_text.count(
        f"open-loop: slice-eval for solo@{sha1} wrote no verdict section "
        "(attempt 1)"
    ) == 1
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: shipped" in state_text


def test_slice_eval_never_writes_errors_after_three_attempts(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    sha1 = fake_sha("solo-blip-forever")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = RetryScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): [lambda mb: None, lambda mb: None, lambda mb: None]
        },
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 3
    slice_calls = [c for c in evaluator.calls if c["context"]["kind"] == "slice-eval"]
    assert len(slice_calls) == 3
    assert not any(c["context"]["kind"] == "integration-eval" for c in evaluator.calls)
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    for n in (1, 2, 3):
        assert (
            f"open-loop: slice-eval for solo@{sha1} wrote no verdict section "
            f"(attempt {n})" in log_text
        )
    assert (
        f"slice-eval for solo@{sha1} failed to write a verdict section "
        "after 3 attempts" in log_text
    )
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text


def test_integration_eval_no_write_retries_then_ships(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-int-blip")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = RetryScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): [
                lambda mb: verdict.append_slice_section("solo", sha1, "SHIP")
            ]
        },
        integration_actions=[
            lambda mb: None,  # exit 0, writes nothing -- a runner blip
            lambda mb: verdict.set_integration_verdict("VERDICT: SHIP"),
        ],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 0
    integration_calls = [
        c for c in evaluator.calls if c["context"]["kind"] == "integration-eval"
    ]
    assert len(integration_calls) == 2
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert log_text.count(
        "open-loop: integration-eval wrote no verdict (attempt 1)"
    ) == 1
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: shipped" in state_text


def test_integration_eval_never_writes_errors_after_three_attempts(
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-int-blip-forever")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = RetryScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): [
                lambda mb: verdict.append_slice_section("solo", sha1, "SHIP")
            ]
        },
        integration_actions=[lambda mb: None, lambda mb: None, lambda mb: None],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 3
    integration_calls = [
        c for c in evaluator.calls if c["context"]["kind"] == "integration-eval"
    ]
    assert len(integration_calls) == 3
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    for n in (1, 2, 3):
        assert f"integration-eval wrote no verdict (attempt {n})" in log_text
    assert (
        "integration-eval failed to write a verdict after 3 attempts" in log_text
    )
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text


def test_integration_eval_malformed_verdict_errors_immediately(
    tmp_path: Path,
) -> None:
    """A real (if malformed) VERDICT: line is a genuine unparseable-verdict
    error -- unlike a blip that writes nothing, it must NOT be retried."""
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    queue = QueueModel(mailbox, lock)
    verdict = VerdictModel(mailbox, lock)
    sha1 = fake_sha("solo-int-banana")

    lead = ScriptedLeadRunner([lambda mb: queue.retire("solo", sha1)])

    evaluator = RetryScriptedEvalRunner(
        slice_actions={
            ("solo", sha1): [
                lambda mb: verdict.append_slice_section("solo", sha1, "SHIP")
            ]
        },
        integration_actions=[
            lambda mb: verdict.set_integration_verdict("VERDICT: BANANA")
        ],
    )

    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)

    assert code == 3
    integration_calls = [
        c for c in evaluator.calls if c["context"]["kind"] == "integration-eval"
    ]
    assert len(integration_calls) == 1
    log_text = (mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "unparseable integration verdict" in log_text
    assert "wrote no verdict" not in log_text
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text


# --- max_iterations cap -> exit 4 ---------------------------------------


def test_max_iterations_cap_returns_four(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_TWO_SLICES)
    queue = QueueModel(mailbox, lock)
    sha_a1 = fake_sha("alpha-cap")

    # Only ever retires alpha -- beta never gets retired, so the Lead
    # thread always wants one more pass and the budget caps at 1.
    lead = ScriptedLeadRunner([lambda mb: queue.retire("alpha", sha_a1)])
    evaluator = ScriptedEvalRunner(
        slice_actions={("alpha", sha_a1): lambda mb: None}
    )

    code = trio_loop.run_open_loop(mailbox, 1, lead, evaluator, poll_seconds=0.01)

    assert code == 4


# --- no-hang when the Lead thread raises --------------------------------


def test_no_hang_when_lead_thread_raises(tmp_path: Path) -> None:
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    lead = RaisingLeadRunner()
    evaluator = ScriptedEvalRunner()

    result: dict = {}

    def target():
        result["code"] = trio_loop.run_open_loop(
            mailbox, 5, lead, evaluator, poll_seconds=0.01
        )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "run_open_loop hung when the Lead thread raised"
    assert result["code"] == 3
    state_text = (mailbox / "STATE.md").read_text(encoding="utf-8")
    assert "status: error" in state_text


# --- exact trio-shadow argv ----------------------------------------------


def test_per_slice_gate_argv_shape(tmp_path: Path, monkeypatch) -> None:
    mailbox = make_open_loop_mailbox(tmp_path, PLAN_ONE_SLICE)
    captured: dict = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        return FakeCompleted()

    monkeypatch.setattr(trio_loop.subprocess, "run", fake_run)

    code = trio_loop._per_slice_gate(mailbox, None, "solo")

    assert code == 0
    script = Path(trio_loop.__file__).resolve().with_name("trio-shadow.py")
    assert captured["command"] == [
        sys.executable,
        str(script),
        "--mailbox",
        str(mailbox.resolve()),
        "--require-commits",
        "--slice",
        "solo",
    ]


# --- legacy 3-arg runner through lockstep run_loop -----------------------


class LegacyThreeArgRunner:
    """A runner whose run() has no context parameter at all."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        self.calls.append((role, iteration))
        if role in {"lead", "repair"}:
            with (mailbox / "LOG.md").open("a", encoding="utf-8") as log:
                log.write(f"- iter {iteration} | {role} | completed\n")
        if role == "evaluator":
            (mailbox / "VERDICT.md").write_text(
                "VERDICT: SHIP\n", encoding="utf-8"
            )
        return 0


def test_legacy_three_arg_runner_drives_lockstep_run_loop(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
    )
    (mailbox / "PLAN.md").write_text(
        '```yaml\nslices:\n  - id: coord\n    writes: [loop/STATE.md]\n'
        "    reads: []\n```\n",
        encoding="utf-8",
    )
    (mailbox / "REPORT.md").write_text("", encoding="utf-8")
    (mailbox / "VERDICT.md").write_text("", encoding="utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    assert not (mailbox / "QUEUE.md").exists()

    runner = LegacyThreeArgRunner()
    code = trio_loop.run_loop(mailbox, 1, runner)

    assert code == 0
    assert runner.calls == [("lead", 1), ("evaluator", 1)]


# --- open-loop mode with no QUEUE.md -------------------------------------


class NeverCalledRunner:
    def run(self, role, iteration, mailbox, context=None):
        raise AssertionError("runner should never be invoked")


def test_open_loop_mode_without_queue_returns_three(tmp_path: Path) -> None:
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    assert not (mailbox / "QUEUE.md").exists()
    assert not (mailbox / "STATE.md").exists()

    code = trio_loop.run_loop(
        mailbox, 1, NeverCalledRunner(), mode="open-loop"
    )

    assert code == 3
    assert not (mailbox / "STATE.md").exists()


# --- stdlib-only import check --------------------------------------------


def test_trio_loop_imports_only_stdlib_modules() -> None:
    source_path = Path(trio_loop.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    top_level_packages: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_packages.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top_level_packages.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    offenders = top_level_packages - allowed
    assert not offenders, f"non-stdlib imports found: {sorted(offenders)}"

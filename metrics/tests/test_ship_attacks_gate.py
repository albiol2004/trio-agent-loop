"""The ship attacks gate: frozen checks are a floor, not the ceiling.

Before ANY SHIP (lockstep, open-loop integration, open-loop per-slice) the
Evaluator must have recorded at least one adversarial probe beyond the frozen
pack in an ``attacks:`` list; the DRIVER refuses a SHIP without one, whether
or not frozen acceptance is on. Fake runners only -- no model, no network.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_open_loop_driver as OL
from metrics.tests import test_r19_loop_acceptance as T
from metrics.tests import test_trio_loop as TL

@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in T.GIT_ENV.items():
        monkeypatch.setenv(key, value)


ATTACKS = "attacks:\n- empty input -> rejected with exit 2\n"
SHIP = "VERDICT: SHIP\n"
SHIP_ATTACKED = SHIP + ATTACKS
NO_ATTACK_REASON = "no attack beyond the frozen checks (attacks=n/a)"


# ------------------------------------------------------------ count_attacks


def test_count_attacks_none_without_header() -> None:
    assert trio_loop.count_attacks("") is None
    assert trio_loop.count_attacks("VERDICT: SHIP\n- a -> b\n") is None
    assert trio_loop.count_attacks("the word attacks appears here\n") is None


def test_count_attacks_empty_and_na_headers_are_zero() -> None:
    assert trio_loop.count_attacks("attacks:\n") == 0
    assert trio_loop.count_attacks("attacks: n/a\nnext: x\n") == 0
    assert trio_loop.count_attacks("attacks: none\n") == 0


def test_count_attacks_counts_probe_with_result() -> None:
    text = "attacks:\n- empty input -> rejected\n- `cmd --x` -> exit 2\n"
    assert trio_loop.count_attacks(text) == 2
    assert trio_loop.count_attacks("attacks:\n* a -> b\n") == 1


@pytest.mark.parametrize("header", [
    "attacks:", "  attacks:", "**attacks:**", "`attacks:`", "ATTACKS:", "Attacks:",
    "- attacks:", "## attacks:", "attacks: (beyond the pack)",
])
def test_count_attacks_header_is_tolerant(header: str) -> None:
    assert trio_loop.count_attacks(f"{header}\n- a -> b\n") == 1


@pytest.mark.parametrize("bullet", [
    "- none -> n/a", "- n/a -> n/a", "- NA -> x", "- - -> x", "- tbd -> x",
    "- ... -> x", "- … -> x", "- `none` -> x", "- None. -> x", "- N/A -> x",
])
def test_count_attacks_placeholder_bullets_do_not_count(bullet: str) -> None:
    assert trio_loop.count_attacks(f"attacks:\n{bullet}\n") == 0


@pytest.mark.parametrize("bullet", [
    "- ran the suite again", "- empty input ->", "- -> rejected", "- empty input -> ",
    "-nobullet -> x",
])
def test_count_attacks_needs_probe_and_result(bullet: str) -> None:
    assert trio_loop.count_attacks(f"attacks:\n{bullet}\n") == 0


def test_count_attacks_unicode_arrow_counts() -> None:
    assert trio_loop.count_attacks("attacks:\n- huge file → streamed, no OOM\n") == 1


def test_count_attacks_continuation_lines_belong_to_the_bullet() -> None:
    text = "attacks:\n- run with an empty file\n    -> exit 2 and a message\n- second\n  → ok\n"
    assert trio_loop.count_attacks(text) == 2


def test_count_attacks_list_ends_at_first_non_indented_non_bullet_line() -> None:
    text = "attacks:\n- a -> b\n\n- c -> d\nnotes: here\n- e -> f\n"
    assert trio_loop.count_attacks(text) == 2


def test_count_attacks_blocks_sum() -> None:
    text = "attacks:\n- a -> b\n\nother: x\n\nattacks:\n- c -> d\n- e -> f\n"
    assert trio_loop.count_attacks(text) == 3


# ------------------------------------------- whole_goal / slice scoping


SECTIONED = (
    "VERDICT: SHIP\niteration: 1\n"
    "## slice alpha @abcdef1234567 — SHIP\nattacks:\n- slice probe -> held\n"
    "## slice beta @1234567abcdef — SHIP\nevidence: x\n"
)


def test_whole_goal_attacks_ignores_slice_sections() -> None:
    assert trio_loop.whole_goal_attacks(SECTIONED) is None
    assert trio_loop.whole_goal_attacks(SECTIONED + "\n## Notes\nattacks:\n- a -> b\n") == 1
    own = "VERDICT: SHIP\nattacks:\n- g -> held\n" + SECTIONED.split("\n", 1)[1]
    assert trio_loop.whole_goal_attacks(own) == 1


def test_slice_attacks_counts_only_that_slice_section() -> None:
    assert trio_loop.slice_attacks(SECTIONED, "alpha", "abcdef1234567") == 1
    assert trio_loop.slice_attacks(SECTIONED, "beta", "1234567abcdef") is None
    assert trio_loop.slice_attacks(SECTIONED, "gamma", "abcdef1234567") is None


def test_slice_attacks_last_section_wins_and_sha_is_prefix_tolerant() -> None:
    full = "abcdef1234567890abcdef1234567890abcdef12"
    text = (
        "## slice alpha @abcdef1 — SHIP\nevidence: x\n"
        "## slice alpha @abcdef1 — SHIP\nattacks:\n- p -> q\n"
    )
    assert trio_loop.slice_attacks(text, "alpha", full) == 1  # short heading, full sha
    assert trio_loop.slice_attacks(text, "alpha", "abcdef1") == 1
    reversed_text = (
        "## slice alpha @abcdef1 — SHIP\nattacks:\n- p -> q\n"
        "## slice alpha @abcdef1 — SHIP\nevidence: x\n"
    )
    assert trio_loop.slice_attacks(reversed_text, "alpha", full) is None
    # A section for another sha of the same slice is not this key's.
    other = "## slice alpha @1111111 — SHIP\nattacks:\n- p -> q\n"
    assert trio_loop.slice_attacks(other, "alpha", full) is None


def test_ship_attacks_problem_reasons() -> None:
    assert trio_loop.MIN_SHIP_ATTACKS == 1
    assert trio_loop.ship_attacks_problem(1) is None
    assert trio_loop.ship_attacks_problem(5) is None
    assert trio_loop.ship_attacks_problem(0) == "no attack beyond the frozen checks (attacks=0)"
    assert trio_loop.ship_attacks_problem(None) == NO_ATTACK_REASON


def test_slice_iterate_section_is_not_gated() -> None:
    text = "## slice alpha @abcdef1 — ITERATE\nevidence: x\n"
    assert trio_loop._slice_ship_problem(text, "alpha", "abcdef1") is None
    ship = "## slice alpha @abcdef1 — SHIP\nevidence: x\n"
    assert trio_loop._slice_ship_problem(ship, "alpha", "abcdef1") == NO_ATTACK_REASON


# ----------------------------------------------- the whole-goal gate (unit)


def test_review_ship_attacks_leaves_other_verdicts_and_attacked_ships(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text("VERDICT: ITERATE\n", encoding="utf-8")
    assert trio_loop.review_ship_attacks(mailbox, 1, "ITERATE", "design") == ("ITERATE", "design", [])
    assert trio_loop.review_ship_attacks(mailbox, 1, "BLOCKED", None) == ("BLOCKED", None, [])
    (mailbox / "VERDICT.md").write_text(SHIP_ATTACKED, encoding="utf-8")
    assert trio_loop.review_ship_attacks(mailbox, 1, "SHIP", None) == ("SHIP", None, [])
    assert trio_loop.ship_gate_errors(mailbox) == []


def test_review_ship_attacks_refuses_logs_and_persists(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text(SHIP + "attacks: n/a\n", encoding="utf-8")
    verdict, scope, errors = trio_loop.review_ship_attacks(mailbox, 3, "SHIP", None)
    assert (verdict, scope) == ("ITERATE", None)
    assert len(errors) == 1
    assert errors[0].startswith("The driver refused the SHIP")
    assert "no attack beyond the frozen checks (attacks=0)" in errors[0]
    assert "Frozen checks are a floor" in errors[0]
    assert ("- iter 3 | loop | SHIP refused by the attacks gate (verdict becomes ITERATE): "
            "no attack beyond the frozen checks (attacks=0)") in (mailbox / "LOG.md").read_text()
    # Keyed to the iteration the next Lead pass runs as; read-only and idempotent.
    assert trio_loop.ship_gate_errors(mailbox, 4) == errors
    assert trio_loop.ship_gate_errors(mailbox, 4) == errors
    assert trio_loop.ship_gate_errors(mailbox) == errors
    assert trio_loop.ship_gate_errors(mailbox, 3) == []
    assert trio_loop.ship_gate_errors(mailbox, 5) == []
    trio_loop.clear_ship_gate_errors(mailbox)
    assert trio_loop.ship_gate_errors(mailbox) == []


def test_ship_gate_survives_the_core_rewriting_driver_json(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    (mailbox / "VERDICT.md").write_text(SHIP, encoding="utf-8")
    trio_loop.review_ship_attacks(mailbox, 1, "SHIP", None)
    trio_loop._write_driver_state(mailbox, TL.FakeRunner([]), 1, "idle")
    assert trio_loop.ship_gate_errors(mailbox, 2)
    assert json.loads((mailbox / ".driver.json").read_text())["phase"] == "idle"


# ------------------------------------------------------------ lockstep


def _lead_context_hook(mailbox: Path, seen: dict):
    """FakeRunner subclass recording what ship_gate_errors says per Lead pass."""

    class Runner(TL.FakeRunner):
        def run(self, role, iteration, mb, context=None):
            if role in ("lead", "repair"):
                seen[iteration] = trio_loop.ship_gate_errors(mb, iteration)
            return super().run(role, iteration, mb, context)

    return Runner


def test_lockstep_ship_without_attacks_is_refused_then_ships(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    seen: dict = {}
    runner = _lead_context_hook(mailbox, seen)(["VERDICT: SHIP", SHIP_ATTACKED], inject_attacks=False)
    assert trio_loop.run_loop(mailbox, 3, runner, mode="lockstep") == 0
    assert [c for c in runner.calls] == [
        ("lead", 1), ("evaluator", 1), ("lead", 2), ("evaluator", 2)]
    log = (mailbox / "LOG.md").read_text()
    assert ("- iter 1 | loop | SHIP refused by the attacks gate (verdict becomes ITERATE): "
            + NO_ATTACK_REASON) in log
    assert "iter 2 | loop | SHIP refused" not in log
    lead1, lead2 = runner.contexts[0], runner.contexts[2]
    assert not (lead1 or {}).get("ship_gate_errors")
    assert lead2 and "refused the SHIP" in lead2["ship_gate_errors"][0]
    assert seen[1] == [] and seen[2], "the pass sees its own iteration's errors"
    # Delivered once, then cleared; the second SHIP finished the loop.
    assert trio_loop.ship_gate_errors(mailbox) == []
    assert "status: shipped" in TL.state_text(mailbox)
    assert (mailbox / ".driver.json").is_file()


def test_lockstep_attacks_header_with_only_placeholders_is_refused(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    runner = TL.FakeRunner(
        ["VERDICT: SHIP\nattacks:\n- none -> n/a\n", SHIP_ATTACKED], inject_attacks=False)
    assert trio_loop.run_loop(mailbox, 3, runner, mode="lockstep") == 0
    assert "attacks=0" in (mailbox / "LOG.md").read_text()


def test_lockstep_iterate_and_blocked_are_not_gated(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    runner = TL.FakeRunner(["VERDICT: ITERATE", "VERDICT: BLOCKED"])
    assert trio_loop.run_loop(mailbox, 3, runner, mode="lockstep") == 2
    assert "attacks gate" not in (mailbox / "LOG.md").read_text()
    assert trio_loop.ship_gate_errors(mailbox) == []


def test_lockstep_refusals_run_out_the_iteration_cap(tmp_path: Path) -> None:
    mailbox = TL.make_mailbox(tmp_path)
    runner = TL.FakeRunner(["VERDICT: SHIP", "VERDICT: SHIP"], inject_attacks=False)
    assert trio_loop.run_loop(mailbox, 2, runner, mode="lockstep") == 4
    log = (mailbox / "LOG.md").read_text()
    assert log.count("SHIP refused by the attacks gate") == 2
    assert "status: shipped" not in TL.state_text(mailbox)


def test_lockstep_refusal_commits_tracked_log(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    mailbox = TL.make_mailbox(repo)
    TL.init_git(repo)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    runner = TL.FakeRunner(["VERDICT: SHIP", SHIP_ATTACKED], inject_attacks=False)
    assert trio_loop.run_loop(mailbox, 3, runner, mode="lockstep") == 0
    subjects = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True
    ).stdout.splitlines()
    assert "loop: iteration 1 — attacks gate refused SHIP (ITERATE)" in subjects
    committed = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:mailbox/LOG.md"], capture_output=True, text=True
    ).stdout
    assert "SHIP refused by the attacks gate" in committed
    # An untracked LOG.md is never committed.
    other = tmp_path / "plain"
    other.mkdir()
    box = TL.make_mailbox(other)
    assert trio_loop.run_loop(box, 3, TL.FakeRunner(["VERDICT: SHIP", SHIP_ATTACKED], inject_attacks=False),
                              mode="lockstep") == 0


def test_lockstep_acceptance_on_attacks_gate_runs_before_the_pack(tmp_path: Path) -> None:
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}, {}],
                  eval_script=[{"verdict": "SHIP", "attacks": False}, {"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    lg = T.log(mb)
    assert "SHIP refused by the attacks gate (verdict becomes ITERATE)" in lg
    # The frozen pack was run for the second SHIP only: one SHIP-gate line,
    # and no acceptance refusal of the first.
    assert lg.count("SHIP gate: 8/8 PASS") == 1
    assert "refused by the acceptance gate" not in lg
    lead_ctx = [c for c in fake.contexts if c.get("ship_gate_errors")]
    assert len(lead_ctx) == 1 and "refused the SHIP" in lead_ctx[0]["ship_gate_errors"][0]
    assert not any(c.get("acceptance_errors") for c in fake.contexts)
    assert len([c for c in fake.calls if c[0] == "lead"]) == 2
    assert "status: shipped" in T.state(mb)
    assert ("loop: iteration 1 — attacks gate refused SHIP (ITERATE)") in T.subjects(repo)


# ----------------------------------------------------------- open-loop


class RecordingLead:
    """Open-loop Lead: scripted passes; records context and the gate read."""

    def __init__(self, passes) -> None:
        self.passes = list(passes)
        self.calls: list[dict] = []

    def run(self, role, iteration, mailbox, context=None):
        assert role == "lead"
        self.calls.append({
            "iteration": iteration,
            "context": dict(context),
            "gate": trio_loop.ship_gate_errors(mailbox, iteration),
        })
        self.passes.pop(0)(mailbox)
        return 0


class RecordingEval:
    """Open-loop Evaluator: per-key lists of slice actions, then integration."""

    def __init__(self, slice_actions, integration_actions) -> None:
        self.slice_actions = {k: list(v) for k, v in slice_actions.items()}
        self.integration_actions = list(integration_actions)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def run(self, role, iteration, mailbox, context=None):
        assert role == "evaluator"
        with self._lock:
            self.calls.append({"iteration": iteration, "context": dict(context)})
            if context["kind"] == "slice-eval":
                action = self.slice_actions[(context["slice"], context["sha"])].pop(0)
            else:
                action = self.integration_actions.pop(0)
        action(mailbox)
        return 0


def _append(mailbox: Path, text: str) -> None:
    path = mailbox / "VERDICT.md"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(old + ("" if old.endswith("\n") or not old else "\n") + text, encoding="utf-8")


def _slice_section(slice_id: str, sha: str, verdict: str = "SHIP", attacks: bool = True) -> str:
    return f"## slice {slice_id} @{sha} — {verdict}\nevidence: re-run=1\n" + (ATTACKS if attacks else "")


def _integration(mailbox: Path, text: str) -> None:
    path = mailbox / "VERDICT.md"
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(text + old, encoding="utf-8")


def test_open_loop_integration_ship_without_attacks_wakes_lead_with_errors(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = OL.make_open_loop_mailbox(tmp_path, OL.PLAN_ONE_SLICE)
    queue = OL.QueueModel(mailbox, lock)
    sha1, sha2 = OL.fake_sha("solo-1"), OL.fake_sha("solo-2")
    lead = RecordingLead([lambda mb: queue.retire("solo", sha1),
                          lambda mb: queue.retire("solo", sha2)])
    evaluator = RecordingEval(
        {("solo", sha1): [lambda mb: _append(mb, _slice_section("solo", sha1))],
         ("solo", sha2): [lambda mb: _append(mb, _slice_section("solo", sha2))]},
        [lambda mb: _integration(mb, SHIP), lambda mb: _integration(mb, SHIP_ATTACKED)],
    )
    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)
    assert code == 0
    assert len(lead.calls) == 2
    first, second = lead.calls
    assert "ship_gate_errors" not in first["context"] and first["gate"] == []
    assert "refused the SHIP" in second["context"]["ship_gate_errors"][0]
    assert second["iteration"] == 2 and second["gate"], "keyed to the next Lead pass"
    log = (mailbox / "LOG.md").read_text()
    assert ("- iter 1 | loop | SHIP refused by the attacks gate (verdict becomes ITERATE): "
            + NO_ATTACK_REASON) in log
    state = (mailbox / "STATE.md").read_text()
    assert "status: shipped" in state
    assert trio_loop.ship_gate_errors(mailbox) == []


def test_open_loop_integration_attacks_inside_slice_sections_do_not_count(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = OL.make_open_loop_mailbox(tmp_path, OL.PLAN_ONE_SLICE)
    queue = OL.QueueModel(mailbox, lock)
    sha1, sha2 = OL.fake_sha("solo-1"), OL.fake_sha("solo-2")
    lead = RecordingLead([lambda mb: queue.retire("solo", sha1),
                          lambda mb: queue.retire("solo", sha2)])
    evaluator = RecordingEval(
        {("solo", sha1): [lambda mb: _append(mb, _slice_section("solo", sha1))],
         ("solo", sha2): [lambda mb: _append(mb, _slice_section("solo", sha2))]},
        [lambda mb: _integration(mb, SHIP), lambda mb: _integration(mb, SHIP_ATTACKED)],
    )
    assert trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01) == 0
    assert (mailbox / "LOG.md").read_text().count("SHIP refused by the attacks gate") == 1


@pytest.mark.parametrize("concurrency", [1, 2])
def test_open_loop_slice_ship_without_attacks_is_redispatched(tmp_path: Path, concurrency: int) -> None:
    lock = threading.Lock()
    mailbox = OL.make_open_loop_mailbox(tmp_path, OL.PLAN_ONE_SLICE)
    queue = OL.QueueModel(mailbox, lock)
    sha = OL.fake_sha("solo-attack")
    lead = RecordingLead([lambda mb: queue.retire("solo", sha)])
    evaluator = RecordingEval(
        {("solo", sha): [
            lambda mb: _append(mb, _slice_section("solo", sha, attacks=False)),
            lambda mb: _append(mb, _slice_section("solo", sha)),
        ]},
        [lambda mb: _integration(mb, SHIP_ATTACKED)],
    )
    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01,
                                   slice_eval_concurrency=concurrency)
    assert code == 0
    slice_calls = [c["context"] for c in evaluator.calls if c["context"]["kind"] == "slice-eval"]
    assert len(slice_calls) == 2
    assert "slice_eval_errors" not in slice_calls[0]
    (error,) = slice_calls[1]["slice_eval_errors"]
    assert error.startswith(f"The driver did not accept your SHIP section for slice solo @{sha[:12]}: ")
    assert NO_ATTACK_REASON in error and "`attacks:` list" in error
    log = (mailbox / "LOG.md").read_text()
    assert (f"open-loop: slice solo @{sha[:12]} SHIP not accepted ({NO_ATTACK_REASON}); "
            "re-dispatching slice-eval (attempt 1)") in log
    assert "status: shipped" in (mailbox / "STATE.md").read_text()
    assert not evaluator.integration_actions


def test_open_loop_slice_ship_cap_exhausted_is_the_terminal_error(tmp_path: Path) -> None:
    lock = threading.Lock()
    mailbox = OL.make_open_loop_mailbox(tmp_path, OL.PLAN_ONE_SLICE)
    queue = OL.QueueModel(mailbox, lock)
    sha = OL.fake_sha("solo-cap")
    lead = RecordingLead([lambda mb: queue.retire("solo", sha)])
    bare = lambda mb: _append(mb, _slice_section("solo", sha, attacks=False))  # noqa: E731
    evaluator = RecordingEval({("solo", sha): [bare, bare, bare]}, [])
    code = trio_loop.run_open_loop(mailbox, 5, lead, evaluator, poll_seconds=0.01)
    assert code == 3
    log = (mailbox / "LOG.md").read_text()
    assert "SHIP not accepted" in log and "(attempt 3)" in log
    assert f"slice-eval for solo@{sha} failed to write a verdict section the driver accepts" in log
    assert "status: error" in (mailbox / "STATE.md").read_text()
    assert len([c for c in evaluator.calls if c["context"]["kind"] == "slice-eval"]) == 3


# ---------------------------------------------------------- portable runner


def _render(monkeypatch, capfd, tmp_path, role, context) -> str:
    mailbox = tmp_path / "pmb"
    mailbox.mkdir(exist_ok=True)
    (mailbox / "GOAL.md").write_text("# Goal\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS", "generic")
    monkeypatch.setenv("RUN_LEAD", "cat")
    monkeypatch.setenv("RUN_EVAL", "cat")
    assert trio_loop._PortableRunner().run(role, 2, mailbox, context) == 0
    return capfd.readouterr().out


def test_portable_lockstep_lead_prompt_renders_ship_gate_errors(monkeypatch, capfd, tmp_path) -> None:
    out = _render(monkeypatch, capfd, tmp_path, "lead",
                  {"ship_gate_errors": ["first error", "second error"]})
    assert out.startswith("SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n"
                          "- first error\n- second error\n")
    plain = _render(monkeypatch, capfd, tmp_path, "lead", {})
    assert "SHIP GATE ERRORS" not in plain


def test_portable_open_loop_prompts_render_both_blocks(monkeypatch, capfd, tmp_path) -> None:
    lead = _render(monkeypatch, capfd, tmp_path, "lead",
                   {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None,
                    "ship_gate_errors": ["no attacks"]})
    assert lead.startswith("OPEN-LOOP CONTEXT: kind=lead-pass")
    assert ("SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n- no attacks\n") in lead
    ev = _render(monkeypatch, capfd, tmp_path, "evaluator",
                 {"mode": "open-loop", "kind": "slice-eval", "slice": "solo", "sha": "abc1234",
                  "slice_eval_errors": ["add an attacks list"]})
    assert "SLICE-EVAL ERRORS FROM THE DRIVER:\n- add an attacks list\n" in ev
    assert "SHIP GATE ERRORS" not in ev
    # Variables do not leak into the next dispatch.
    clean = _render(monkeypatch, capfd, tmp_path, "evaluator",
                    {"mode": "open-loop", "kind": "slice-eval", "slice": "solo", "sha": "abc1234"})
    assert "SLICE-EVAL ERRORS" not in clean

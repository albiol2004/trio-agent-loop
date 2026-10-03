"""acceptance-strength, Omnigent driver (`trioctl`): the SHIP attacks gate and
the requirement map reach the driver's own surfaces.

The gates themselves run in the shared loop core (`metrics/trio_loop.py`);
these tests drive the real `trioctl omnigent loop` (fake role runner, real
git, real core) and check what trioctl owns:

* the Lead prompt renders `SHIP GATE ERRORS` (acceptance on or off) after a
  refused SHIP, the slice-eval prompt renders `SLICE-EVAL ERRORS` after an
  unaccepted slice SHIP section;
* the slice telemetry counts attacks with the core's rule;
* `.driver.json` keeps the core's `ship_gate` key through trioctl's writers;
* the acceptance author's retry prompt carries requirement-map gaps;
* a legacy frozen pack (no map) resumes with one WARNING.
"""
from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

import pytest

from r16_harness import ATTACKS, REPO_ROOT, World, git, init_repo
from test_r19_acceptance_e2e import (
    FULL, _goal, _home, _live_box, _mapping_lead, _slices, _wait_frozen, fake_author,
)

SHIP_BLOCK = "SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n- "
SLICE_BLOCK = "SLICE-EVAL ERRORS FROM THE DRIVER:\n- "
REFUSED = "SHIP refused by the attacks gate"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    monkeypatch.setenv("TRIO_ACCEPTANCE_SANDBOX", "none")
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    return World(tmp_path, monkeypatch, tag="accstrength")


@pytest.fixture()
def acc_world(world, monkeypatch):
    monkeypatch.setattr(world.trioctl.OmnigentRunner, "author", fake_author)
    return world


def _plain_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    return home


def _log(world, home, spec) -> str:
    return (_live_box(world, home, spec) / "LOG.md").read_text()


def _evals(world, kind):
    return [e for e in world.events if e["kind"] == kind]


# ----------------------------------------------------------- lockstep, off


def test_lockstep_ship_without_attacks_is_refused_and_the_lead_is_told(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/ls", [{"id": "ls-a", "write": "src/ls_a.py"}], lockstep=True)
    calls = {"n": 0}

    def evaluator(w, sp, runner, ctx, workspace, box, prompt, iteration):
        calls["n"] += 1
        if calls["n"] == 1:
            w.lockstep_eval(sp, ctx, workspace, box, iteration, attacks=False)
            return True
        return False  # the default evaluator lists an attack

    world.hooks["evaluator"] = evaluator
    assert world.run_loop(spec) == 0
    log = _log(world, home, spec)
    assert f"| loop | {REFUSED} (verdict becomes ITERATE): no attack beyond the frozen checks" in log
    assert calls["n"] == 2
    leads = _evals(world, "lead")
    assert len(leads) == 2
    assert "SHIP GATE ERRORS" not in leads[0]["prompt"]
    assert SHIP_BLOCK + "The driver refused the SHIP" in leads[1]["prompt"]
    assert "attacks=n/a" in leads[1]["prompt"]
    # The shipped verdict carries the counted attack.
    verdict = (_live_box(world, home, spec) / "VERDICT.md").read_text()
    assert "attacks:\n- empty input -> rejected with exit 2" in verdict


def test_lockstep_ship_with_attacks_ships_first_try_without_the_block(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/ok", [{"id": "ok-a", "write": "src/ok_a.py"}], lockstep=True)
    assert world.run_loop(spec) == 0
    assert REFUSED not in _log(world, home, spec)
    assert all("SHIP GATE ERRORS" not in e["prompt"] for e in _evals(world, "lead"))


# ------------------------------------------------------ lockstep, acceptance


def test_lockstep_acceptance_on_refused_ship_never_reaches_the_pack_gate(acc_world, tmp_path):
    world = acc_world
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/la", _slices(), lockstep=True)
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    base_lead = _mapping_lead(world, lockstep=True)
    calls = {"n": 0}

    def evaluator(w, sp, runner, ctx, workspace, box, prompt, iteration):
        calls["n"] += 1
        if calls["n"] == 1:
            w.lockstep_eval(sp, ctx, workspace, box, iteration, attacks=False)
            return True
        return False

    world.hooks["lead"] = base_lead
    world.hooks["evaluator"] = evaluator
    assert world.run_loop(spec, "--acceptance") == 0
    log = _log(world, home, spec)
    refused_at = log.index(REFUSED)
    gate_lines = [m.start() for m in re.finditer(r"SHIP gate:", log)]
    # The refused SHIP never ran the pack: exactly one pack-gate verdict, after it.
    assert len(gate_lines) == 1 and gate_lines[0] > refused_at, log
    assert "SHIP gate: 6/6 PASS" in log
    leads = _evals(world, "lead")
    assert any(SHIP_BLOCK in e["prompt"] for e in leads[1:])
    # The acceptance block is still rendered next to it.
    refreshed = next(e for e in leads[1:] if SHIP_BLOCK in e["prompt"])["prompt"]
    assert "ACCEPTANCE" in refreshed


# ------------------------------------------------------------- open-loop


def test_open_loop_slice_ship_without_attacks_redispatches_the_slice_eval(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/ol", [{"id": "ol-a", "write": "src/ol_a.py"}])
    seen = {"n": 0}

    def slice_eval(w, sp, runner, ctx, workspace, box, prompt, iteration):
        seen["n"] += 1
        if seen["n"] == 1:
            w.slice_eval(ctx, box, attacks=False)
            return True
        return False

    world.hooks["slice-eval"] = slice_eval
    assert world.run_loop(spec) == 0
    evals = _evals(world, "slice-eval")
    assert len(evals) == 2
    assert "SLICE-EVAL ERRORS" not in evals[0]["prompt"]
    assert SLICE_BLOCK in evals[1]["prompt"]
    assert "SHIP GATE ERRORS" not in evals[1]["prompt"]
    log = _log(world, home, spec)
    assert "SHIP not accepted" in log and "re-dispatching slice-eval (attempt 1)" in log
    assert "attacks=1 (shadow)" in log  # telemetry of the accepted section


def test_open_loop_integration_ship_without_attacks_wakes_the_lead(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/oi", [{"id": "oi-a", "write": "src/oi_a.py"}])
    seen = {"n": 0}

    def integration(w, sp, runner, ctx, workspace, box, prompt, iteration):
        seen["n"] += 1
        if seen["n"] == 1:
            w.integration(sp, runner, ctx, workspace, box, attacks=False)
            return True
        return False

    world.hooks["integration-eval"] = integration
    assert world.run_loop(spec) == 0
    log = _log(world, home, spec)
    assert REFUSED in log
    leads = [e for e in _evals(world, "lead-pass")]
    assert any(SHIP_BLOCK in e["prompt"] for e in leads), [e["prompt"][:200] for e in leads]
    first_with_block = next(e for e in leads if SHIP_BLOCK in e["prompt"])
    assert "OPEN-LOOP CONTEXT: kind=lead-pass" in first_with_block["prompt"]
    assert seen["n"] == 2


# ------------------------------------------------------- prompt rendering


def _runner(tmp_path: Path, **kw):
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("trioctl_accstr_render", str(REPO_ROOT / "omnigent" / "trioctl"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module, module.OmnigentRunner(repo=tmp_path, **kw)


@pytest.mark.parametrize("acceptance", [None, {"enabled": True}], ids=["acceptance-off", "acceptance-on"])
@pytest.mark.parametrize("role", ["lead", "repair"])
def test_lead_prompt_renders_ship_gate_errors_acceptance_on_or_off(tmp_path, role, acceptance):
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    (mailbox / "VERDICT.md").write_text("VERDICT: ITERATE\n")
    _, runner = _runner(tmp_path, acceptance=acceptance)
    for ctx in ({"ship_gate_errors": ["first error", "second error"]},
                {"mode": "open-loop", "kind": "lead-pass", "slice": None, "sha": None,
                 "ship_gate_errors": ["first error", "second error"]}):
        text = runner._prompt(role, 2, mailbox, dict(ctx))
        assert ("SHIP GATE ERRORS FROM THE DRIVER (the last SHIP was refused):\n"
                "- first error\n- second error\n") in text
        assert "SLICE-EVAL ERRORS" not in text
    plain = runner._prompt(role, 2, mailbox, {})
    assert "SHIP GATE ERRORS" not in plain


@pytest.mark.parametrize("acceptance", [None, {"enabled": True}], ids=["acceptance-off", "acceptance-on"])
def test_slice_eval_prompt_renders_slice_eval_errors_only(tmp_path, acceptance):
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    _, runner = _runner(tmp_path, acceptance=acceptance)
    ctx = {"mode": "open-loop", "kind": "slice-eval", "slice": "s1", "sha": "a" * 40,
           "slice_eval_errors": ["list an attack"], "ship_gate_errors": ["not for the evaluator"]}
    text = runner._prompt("evaluator", 2, mailbox, dict(ctx))
    assert "SLICE-EVAL ERRORS FROM THE DRIVER:\n- list an attack\n" in text
    assert "SHIP GATE ERRORS" not in text and "not for the evaluator" not in text
    clean = runner._prompt("evaluator", 2, mailbox, {k: v for k, v in ctx.items()
                                                      if not k.endswith("_errors")})
    assert "SLICE-EVAL ERRORS" not in clean


# ----------------------------------------------- telemetry counting parity

ATTACK_TABLE = [
    "attacks:\n- empty input -> rejected with exit 2\n- huge file -> 413\n",
    "attacks:\n- empty input -> rejected\n- none\n- n/a -> nothing\n- tbd -> x\n- - -> y\n",
    "attacks: none\n",
    "**attacks:**\n* `rm -rf /tmp/x` -> refused\n",
    "attacks:\n- probe one\n  -> continued on the next line\n- real probe → result\n",
    "- attacks:\n  - a -> b\n",
    "## Attacks:\n- malformed -> \n- good -> fine\n",
    "ATTACKS:\n- upper -> case\nnot a bullet\n- after the list ended -> ignored\n",
    "attacks:\n- first -> a\nattacks:\n- second -> b\n",
    "no header here\n- a -> b\n",
    "attacks:\n\n\n- after blanks -> still counted\n",
    "attacks:\n- no arrow at all\n",
    "attacks:\n- before the heading -> counted\n# Notes\n- after it -> ignored\n",
    "",
]


def _section(body: str, sha: str = "a" * 40) -> str:
    return f"\n## slice s1 @{sha} — SHIP\n\naccepts: PASS\n{body}\nevidence: re-run=1 probe=0 implementer-test=0 receipt=0 unverified=0\n"


@pytest.mark.parametrize("body", ATTACK_TABLE)
def test_slice_evidence_counts_attacks_like_the_core(tmp_path, body):
    t, _ = _runner(tmp_path)
    core = t._load_trio_loop(REPO_ROOT)
    text = _section(body)
    # The gate counts a slice's section (it ends at the next heading).
    want = core.slice_attacks(text, "s1", "a" * 40)
    expect = "n/a" if want is None else want
    with_core = t.slice_evidence(text, "s1", "a" * 40, core)
    without = t.slice_evidence(text, "s1", "a" * 40)
    assert with_core["attacks"] == expect, body
    # The local fallback (no core, or a core that predates count_attacks).
    assert without["attacks"] == expect, body
    assert t.slice_evidence(text, "s1", "a" * 40, object())["attacks"] == expect
    assert t.count_attacks(body) == core.count_attacks(body)


def test_slice_evidence_never_crashes_when_the_core_helper_raises(tmp_path):
    t, _ = _runner(tmp_path)

    class Broken:
        def count_attacks(self, text):
            raise RuntimeError("boom")

    got = t.slice_evidence(_section("attacks:\n- a -> b\n"), "s1", "a" * 40, Broken())
    assert got["attacks"] == 1


def test_slice_eval_log_line_format_is_unchanged(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/fmt", [{"id": "fm-a", "write": "src/fm_a.py"}])
    assert world.run_loop(spec) == 0
    log = _log(world, home, spec)
    assert re.search(r"slice fm-a @[0-9a-f]{12} SHIP evidence: .* attacks=1 \(shadow\)", log), log


# ------------------------------------------------- .driver.json ship_gate


def test_driver_json_writers_preserve_the_ship_gate(tmp_path):
    t, runner = _runner(tmp_path)
    core = t._load_trio_loop(REPO_ROOT)
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    gate = {"iteration": 3, "errors": ["The driver refused the SHIP: no attack"]}
    (mailbox / ".driver.json").write_text(json.dumps({"pid": 1, "iteration": 2, "ship_gate": gate}))
    # The runner's own meta writers (quality, lint, same-cwd evidence) feed the
    # core's `.driver.json` writer ...
    runner._record_quality("s1@aaaaaaaaaaaa", {"slice": "s1", "attacks": 0})
    runner._note_same_cwd_serialised(
        type("G", (), {"waited": 0.1, "workspace": "w"})(), "lead", {"meta": {}})
    core._write_driver_state(mailbox, runner, 2, "lead")
    data = json.loads((mailbox / ".driver.json").read_text())
    assert data["ship_gate"] == gate
    assert data["quality"]["s1@aaaaaaaaaaaa"]["attacks"] == 0
    # ... and the driver-stop rewrite is read-modify-write.
    (mailbox / "STATE.md").write_text("schema: 1\niteration: 2\nmax_iterations: 5\nstatus: running\n")
    (mailbox / "LOG.md").write_text("# Trio loop log\n")
    t._apply_driver_stop(core, mailbox, {"status": "error", "phase": "x", "reason": "r", "exit": 3})
    stopped = json.loads((mailbox / ".driver.json").read_text())
    assert stopped["ship_gate"] == gate and stopped["lead_alive"] is False
    assert core.ship_gate_errors(mailbox, 3) == gate["errors"]


def test_refused_ship_gate_survives_the_run_in_driver_json(world, tmp_path):
    home = _plain_home(tmp_path)
    spec = world.add_loop(home, "loop/dj", [{"id": "dj-a", "write": "src/dj_a.py"}], lockstep=True)
    seen = {}

    def lead(w, sp, runner, ctx, workspace, box, prompt, iteration):
        # Every `.driver.json` rewrite the driver made since the refusal kept it.
        if iteration == 2:
            seen["driver"] = json.loads((box / ".driver.json").read_text())
        return False

    calls = {"n": 0}

    def evaluator(w, sp, runner, ctx, workspace, box, prompt, iteration):
        calls["n"] += 1
        if calls["n"] == 1:
            w.lockstep_eval(sp, ctx, workspace, box, iteration, attacks=False)
            return True
        return False

    world.hooks["lead"] = lead
    world.hooks["evaluator"] = evaluator
    assert world.run_loop(spec) == 0
    assert seen["driver"]["ship_gate"]["iteration"] == 2
    assert seen["driver"]["ship_gate"]["errors"]


# ------------------------------------------------------------ author retry


def test_author_retry_prompt_carries_requirement_gaps(world, tmp_path, monkeypatch):
    t = world.trioctl
    prompts: list[str] = []

    def cwr(self, client, agent_id, model, prompt, title, role, started, workspace=None, dispatch=None):
        prompts.append(prompt)
        fake_author(self, workspace, {})
        if len(prompts) == 1:
            manifest_path = Path(workspace) / "acceptance" / "MANIFEST.json"
            manifest = json.loads(manifest_path.read_text())
            # R1 loses its hard checks: a requirement-map gap.
            manifest["requirements"][0]["hard"] = []
            manifest_path.write_text(json.dumps(manifest))
        dispatch["session_id"] = f"author-{len(prompts)}"
        return {"status": "idle"}, {"items": []}

    monkeypatch.setattr(t.OmnigentRunner, "_create_wait_read", cwr)
    roles = {r: {"provider": "claude", "model": "opus", "effort": "high"}
             for r in ("lead", "evaluator", "builder", "scout", "docs")}
    monkeypatch.setattr(t.OmnigentRunner, "_load_config",
                        lambda self: {"version": 1, "roles": roles})
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/ar", _slices())
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    world.hooks["lead-pass"] = _mapping_lead(world)
    assert world.run_loop(spec, "--acceptance") == 0, _log(world, home, spec)
    assert len(prompts) == 2, len(prompts)
    assert "RETRY:" not in prompts[0]
    assert "- pack: requirements: R1 has no hard check" in prompts[1]
    assert "a `requirements:` gap is fixed by adding the missing hard check" in prompts[1]
    log = _log(world, home, spec)
    assert "WARNING frozen with requirement-map gaps" not in log
    assert "acceptance: frozen 6 check(s)" in log


# --------------------------------------------------- legacy frozen pack


def test_legacy_frozen_pack_resumes_with_one_warning(acc_world, tmp_path, monkeypatch):
    world = acc_world
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/lg", _slices())
    (spec["root_box"] / "GOAL.md").write_text(_goal())
    # A pack frozen BEFORE the requirement-map rule: no map, and the freeze
    # filter of that era did not ask for one.
    real_fake = fake_author

    def legacy_author(runner, export, context):
        out = real_fake(runner, export, context)
        manifest_path = Path(export) / "acceptance" / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("requirements", None)
        manifest_path.write_text(json.dumps(manifest))
        return out

    monkeypatch.setattr(world.trioctl.OmnigentRunner, "author", legacy_author)
    core_loader = world.trioctl._load_trio_loop

    def old_rule_core(repo):
        core = core_loader(repo)
        real_sibling = core._load_sibling

        def sibling(name, filename):
            module = real_sibling(name, filename)
            if name == "trio_acceptance" and module is not None:
                module.requirements_problems = lambda *a, **kw: []  # the pre-rule freeze
            return module

        core._load_sibling = sibling
        return core

    monkeypatch.setattr(world.trioctl, "_load_trio_loop", old_rule_core)

    # First run: stop after the pack froze (the loop ends at its iteration cap).
    stop_after_freeze = {"n": 0}

    def lead(w, sp, runner, ctx, workspace, box, prompt, iteration):
        _wait_frozen(box)
        stop_after_freeze["n"] += 1
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | waiting\n")
        return True

    world.hooks["lead-pass"] = lead
    code = world.run_loop(spec, "--acceptance", "--max-iterations", "1")
    assert code != 0
    assert "WARNING legacy frozen pack" not in _log(world, home, spec)

    # Resume with the current rules: the legacy pack is accepted, warned once.
    monkeypatch.setattr(world.trioctl, "_load_trio_loop", core_loader)
    world.hooks["lead-pass"] = _mapping_lead(world)
    assert world.run_loop(spec, "--acceptance", "--max-iterations", "5") == 0, _log(world, home, spec)
    log = _log(world, home, spec)
    assert len(re.findall(r"acceptance: WARNING legacy frozen pack: no requirements map", log)) == 1, log
    assert "hard-check coverage is not enforced for this pack" in log

"""r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md) — the switch, the
helper-op plumbing (steplib.py), the canonical-prompt fragments (prompts.py)
and the generated acceptance role (ocgen.py).

Deliberately NOT covered here: a full fake-opencode e2e through a real,
schema-valid acceptance pack to SHIP, a coverage-refusal re-plan, an
author-contamination re-run, a SHIP refused by the frozen gate, the
validation retry, and a detached acceptance job's real `pending` path — all
of those need the pack run for real through `metrics/trio-acceptance.py`'s
own sandboxed check execution, which `tests/test_e2e.py` (scenario
`tests/scenarios/acceptance.py`) now covers. What IS covered here: every
unit the orchestration is built from (switch resolution/precedence, the
op-call plumbing, the digest/stop/freeze bookkeeping, the canonical-fragment
loader, the generated role/permissions, the author tool-call persistence +
audit override) and the switch-off identity (prompts and op-call arguments
unchanged with the switch off).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode import driver, ocgen, prompts, steplib

TOKEN = "oc-acc-test-token"


def mbox(repo: Path) -> Path:
    return repo / "loop"


# --------------------------------------------------------------------------
# Switch resolution (config.py): CLI > env > config file > off
# --------------------------------------------------------------------------

def _cfg(acceptance: bool = False) -> config_mod.Config:
    d = config_mod._default_dict()
    d["acceptance"] = acceptance
    return config_mod._to_config(d, source_path=None)


def test_acceptance_default_false():
    cfg = _cfg()
    assert cfg.acceptance is False


def test_resolve_acceptance_default_off():
    assert config_mod.resolve_acceptance(_cfg(False), env={}) is False


def test_resolve_acceptance_config_on():
    assert config_mod.resolve_acceptance(_cfg(True), env={}) is True


def test_resolve_acceptance_env_overrides_config():
    assert config_mod.resolve_acceptance(_cfg(False), env={"TRIO_ACCEPTANCE": "1"}) is True
    assert config_mod.resolve_acceptance(_cfg(True), env={"TRIO_ACCEPTANCE": "0"}) is False


def test_resolve_acceptance_cli_overrides_env_and_config():
    assert config_mod.resolve_acceptance(
        _cfg(True), cli_flag=False, env={"TRIO_ACCEPTANCE": "1"}) is False
    assert config_mod.resolve_acceptance(
        _cfg(False), cli_flag=True, env={"TRIO_ACCEPTANCE": "0"}) is True


def test_config_type_validation_rejects_non_bool_acceptance(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"acceptance": "yes"}), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="acceptance"):
        config_mod.load_config(str(path))


def test_cli_resume_refuses_explicit_acceptance_flag():
    from trio_opencode import cli as cli_mod
    parser = cli_mod.build_parser()
    args = parser.parse_args(["resume", "--mailbox", "/tmp/x", "--acceptance"])
    rc = cli_mod.cmd_resume(args)
    assert rc == 2


# --------------------------------------------------------------------------
# recorded_acceptance(): the run-record lookup resume uses
# --------------------------------------------------------------------------

def test_recorded_acceptance_defaults_false_with_no_record(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRIO_OPENCODE_RUNS_DIR", str(tmp_path / "runs"))
    assert driver.recorded_acceptance(tmp_path / "mailbox") is False


def test_recorded_acceptance_reads_registry(tmp_path: Path, monkeypatch):
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("TRIO_OPENCODE_RUNS_DIR", str(runs_dir))
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    path = driver.registry_path(mailbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"acceptance_enabled": True}), encoding="utf-8")
    assert driver.recorded_acceptance(mailbox) is True


def test_recorded_acceptance_falls_back_to_result_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRIO_OPENCODE_RUNS_DIR", str(tmp_path / "runs"))
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    (mailbox / driver.RESULT_FILE).write_text(
        json.dumps({"acceptance": {"enabled": True}}), encoding="utf-8")
    assert driver.recorded_acceptance(mailbox) is True


# --------------------------------------------------------------------------
# steplib.py: acc_flags / acc_take / acc_stop / acc_frozen / step_long
# --------------------------------------------------------------------------

def test_acc_flags_off_is_empty():
    assert steplib.acc_flags(False) == {}
    assert steplib.acc_flags(False, {"status": "frozen", "pin": "x"}) == {}


def test_acc_flags_on_with_no_digest_yet():
    assert steplib.acc_flags(True) == {"acceptance": 1}


def test_acc_flags_on_carries_only_acc_keys():
    acc = {"status": "frozen", "pin": "abc123", "extra_key_ignored": "x"}
    flags = steplib.acc_flags(True, acc)
    held = json.loads(flags["acc"])
    assert set(held) == set(steplib.ACC_KEYS)
    assert held["status"] == "frozen"
    assert held["pin"] == "abc123"
    assert "extra_key_ignored" not in held


def test_acc_take_adopts_a_fresh_digest():
    d = {"status": "authoring", "pin": None}
    result = {"acceptance": d}
    assert steplib.acc_take(None, result, "begin") == d


def test_acc_take_ignores_a_stop_or_malformed_digest():
    assert steplib.acc_take({"status": "authoring"}, {"acceptance": {"stop": {}}}, "next") \
        == {"status": "authoring"}
    assert steplib.acc_take({"status": "authoring"}, {}, "next") == {"status": "authoring"}


def test_acc_take_never_unfreezes():
    frozen = {"status": "frozen", "pin": "abc"}
    result = {"acceptance": {"status": "authoring", "pin": None}}
    assert steplib.acc_take(frozen, result, "gate") == frozen


def test_acc_take_only_begin_or_freeze_may_freeze():
    result = {"acceptance": {"status": "frozen", "pin": "abc"}}
    # `coverage` returning a frozen-looking digest is not trusted to freeze.
    assert steplib.acc_take(None, result, "coverage") is None
    assert steplib.acc_take(None, result, "acceptance-freeze") == result["acceptance"]
    assert steplib.acc_take(None, result, "begin") == result["acceptance"]


def test_acc_frozen():
    assert steplib.acc_frozen(None) is False
    assert steplib.acc_frozen({"status": "authoring"}) is False
    assert steplib.acc_frozen({"status": "frozen", "pin": None}) is False
    assert steplib.acc_frozen({"status": "frozen", "pin": "abc"}) is True


def test_acc_stop_none_when_no_stop():
    assert steplib.acc_stop({"acceptance": {"status": "frozen"}}) is None
    assert steplib.acc_stop({}) is None


def test_acc_stop_builds_a_driver_stop_shape():
    result = {"acceptance": {"stop": {"status": "needs_human", "code": 5,
                                      "reason": "acceptance-tamper", "detail": "pack edited"}}}
    stop = steplib.acc_stop(result)
    assert stop == {"status": "needs_human", "code": 5,
                    "reason": "acceptance acceptance-tamper: pack edited"}


def test_step_long_returns_immediately_when_not_pending():
    calls = []

    def fn(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "pending": False, "result": 1}

    out = steplib.step_long(fn, sleep=lambda s: None)
    assert out == {"ok": True, "pending": False, "result": 1}
    assert len(calls) == 1
    assert "poll" not in calls[0]


def test_step_long_polls_until_done_with_injectable_sleep():
    attempts = {"n": 0}
    sleeps = []

    def fn(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"ok": True, "pending": True}
        return {"ok": True, "pending": False, "poll_seen": kwargs.get("poll")}

    out = steplib.step_long(fn, sleep=sleeps.append, poll_interval_s=0.01)
    assert out == {"ok": True, "pending": False, "poll_seen": 2}
    assert attempts["n"] == 3
    assert sleeps == [0.01, 0.01]


def test_step_long_gives_up_after_max_polls():
    def fn(*args, **kwargs):
        return {"ok": True, "pending": True}

    # Exhausted polling is a failed step (native `stepLong`), never the raw
    # last `pending` result a caller would then index for keys it lacks.
    out = steplib.step_long(fn, sleep=lambda s: None, max_polls=3)
    assert out == {"ok": False, "op": "fn", "error": "fn still running after 3 polls"}


# --------------------------------------------------------------------------
# steplib.py: author audit override (req. 4)
# --------------------------------------------------------------------------

class _FakeTa:
    def __init__(self):
        self.calls = []

    def audit_transcript(self, entries, export, forbidden=(), cwd=None):
        self.calls.append({"entries": list(entries), "export": export,
                           "forbidden": list(forbidden), "cwd": cwd})
        return {"contaminated": False, "hits": []}


class _FakeCtl:
    def __init__(self):
        self.ta = _FakeTa()
        self._audit_calls = []

    def _audit_forbidden(self):
        return ["/repo"]

    def _audit(self, export, result):
        self._audit_calls.append((export, result))
        return {"contaminated": False, "hits": [], "limited": True}


def test_opencode_author_audit_falls_back_without_a_record(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(steplib, "AUTHOR_TOOLCALLS_DIR", tmp_path)
    ctl = _FakeCtl()
    audit = steplib._opencode_author_audit(ctl, Path("/export"), "marker-a1", 0.0)
    assert audit["path"] == "opencode"
    assert audit["transcripts"] == []
    assert "no OpenCode author tool-call record" in audit["note"]
    assert ctl._audit_calls  # fell back to the helper's own limited audit


def test_opencode_author_audit_uses_persisted_tool_calls(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(steplib, "AUTHOR_TOOLCALLS_DIR", tmp_path)
    marker = "deadbeef-1"
    (tmp_path / f"{marker}.jsonl").write_text(
        json.dumps({"type": "tool_use", "name": "bash",
                   "input": {"command": "cat /repo/PLAN.md"}}) + "\n",
        encoding="utf-8",
    )
    ctl = _FakeCtl()
    export = Path("/export")
    audit = steplib._opencode_author_audit(ctl, export, marker, 0.0)
    assert audit["path"] == "opencode"
    assert audit["limited"] is False
    assert audit["transcripts"] == [str(tmp_path / f"{marker}.jsonl")]
    assert len(ctl.ta.calls) == 1
    call = ctl.ta.calls[0]
    assert call["export"] == export
    assert call["cwd"] == export  # req. 4: cwd=export, never ctl.repo
    assert call["forbidden"] == ["/repo"]
    assert call["entries"] == [{"type": "tool_use", "name": "bash",
                               "input": {"command": "cat /repo/PLAN.md"}}]


# --------------------------------------------------------------------------
# steplib.py: begin() tier refusal + switch-off identity
# --------------------------------------------------------------------------

def test_begin_refuses_mismatched_acceptance_tier(git_repo: Path):
    out = steplib.begin(mbox(git_repo), None, TOKEN, acceptance=True,
                        models={"lead": "p/a", "evaluator": "p/a", "acceptance": "p/b"})
    assert out["ok"] is False
    assert "tier" in out["error"] or "refused at begin" in out["error"]
    # Refused before the lock is taken: no .lock dir left behind.
    assert not (mbox(git_repo) / ".lock").exists()


def test_begin_switch_off_identical_with_and_without_explicit_kwargs(git_repo: Path):
    """Switch-off identity (requirement 1): `begin()` called with no
    acceptance kwargs at all (how every pre-r19 caller invokes it) and
    `begin()` called with the switch explicitly off produce byte-identical
    op results (nonce held fixed so the comparison is exact)."""
    out_implicit = steplib.begin(mbox(git_repo), None, TOKEN, nonce="n1")
    assert out_implicit["ok"], out_implicit
    steplib.end(mbox(git_repo), None, TOKEN, nonce="n1e")

    out_explicit = steplib.begin(mbox(git_repo), None, TOKEN, nonce="n1",
                                 acceptance=False, models=None)
    assert out_explicit["ok"], out_explicit
    steplib.end(mbox(git_repo), None, TOKEN, nonce="n1e", acceptance=False, acc=None)

    for key in ("mode", "lock_owner", "status", "phase"):
        assert out_implicit[key] == out_explicit[key]
    assert "acceptance" not in out_implicit
    assert "acceptance" not in out_explicit


# --------------------------------------------------------------------------
# prompts.py: canonical-fragment loading + acc helpers
# --------------------------------------------------------------------------

def test_acc_lead_fragment_substitutes_mailbox_and_tool():
    text = prompts.acc_lead_fragment("/abs/loop", "metrics/trio-acceptance.py")
    assert "{mailbox}" not in text and "{tool}" not in text
    assert "/abs/loop/acceptance/" in text
    assert "FROZEN ACCEPTANCE" in text


def test_acc_evaluator_fragment_substitutes_mailbox_and_tool():
    text = prompts.acc_evaluator_fragment("/abs/loop", "metrics/trio-acceptance.py")
    assert "{mailbox}" not in text and "{tool}" not in text
    assert "metrics/trio-acceptance.py run" in text


def test_acc_plan_lines_empty_without_acc_digest():
    lines = prompts.acc_plan_lines("/abs/loop", "tool.py", None)
    assert any("Frozen pack" in l for l in lines)
    assert any("? checks" in l for l in lines)


def test_acc_plan_lines_includes_errors_and_refusals():
    lines = prompts.acc_plan_lines("/abs/loop", "tool.py", {"checks": 5, "pin": "a" * 40},
                                   errors=["SHIP refused: ACC-02 FAIL"],
                                   refusals=["ACC-03 unmapped"])
    text = "\n".join(lines)
    assert "ACCEPTANCE ERRORS FROM THE DRIVER" in text
    assert "SHIP refused: ACC-02 FAIL" in text
    assert "COVERAGE REFUSED" in text
    assert "ACC-03 unmapped" in text


def test_acc_pass_lines_lead_vs_repair():
    lead_lines = prompts.acc_pass_lines("lead", "/abs/loop", "tool.py")
    repair_lines = prompts.acc_pass_lines("repair", "/abs/loop", "tool.py")
    assert any("python3 tool.py run" in l for l in lead_lines)
    assert not any("python3 tool.py run" in l for l in repair_lines)
    assert all("Never edit, add or delete" in l or l == "" or "FROZEN ACCEPTANCE" in l
              for l in repair_lines)


def test_acc_briefed_appends_covered_text_only_to_listed_slices():
    plan = {"slices": [{"id": "a", "brief": "do a"}, {"id": "b", "brief": "do b"}]}
    out = prompts.acc_briefed(plan, {"a": "## Acceptance (frozen; do not edit)\nACC-01"})
    assert out["slices"][0]["brief"] == "do a\n\n## Acceptance (frozen; do not edit)\nACC-01"
    assert out["slices"][1]["brief"] == "do b"


def test_author_prompt_has_mark_and_validate_command():
    text = prompts.author_prompt("/export/dir", "tool.py", "exec123-1", 1)
    assert prompts.AUTHOR_MARK + " exec123-1" in text
    assert "python3 tool.py validate --export ." in text
    assert "acceptance/" in text and ".author-tmp/" in text


def test_author_prompt_retry_block():
    text = prompts.author_prompt("/export/dir", "tool.py", "exec123-2", 2,
                                 retry={"dropped": [["ACC-01", "flaky"]], "fatal": ["too few checks"]})
    assert "RETRY" in text
    assert "ACC-01: flaky" in text
    assert "pack: too few checks" in text


def test_author_prompt_contamination_prefix():
    text = prompts.author_prompt("/export/dir", "tool.py", "exec123-2", 2,
                                 retry={"prefix": "CONTAMINATION NOTICE"})
    assert text.startswith("CONTAMINATION NOTICE")


# --------------------------------------------------------------------------
# prompts.py: switch-off identity (requirement 1)
# --------------------------------------------------------------------------

def test_lead_plan_prompt_identical_with_acc_params_left_default():
    baseline = prompts.lead_plan_prompt(1, "/box", "/repo", None)
    same = prompts.lead_plan_prompt(1, "/box", "/repo", None, acc_lines=None)
    assert baseline == same
    assert prompts.PLAN_SCHEMA_HINT in baseline
    assert prompts.PLAN_SCHEMA_HINT_ACC not in baseline
    assert "FROZEN ACCEPTANCE" not in baseline


def test_evaluator_prompt_identical_with_acc_lines_left_default():
    pin = {"evaluator_attempt": "1", "sha": "a" * 40, "context_block": "ctx"}
    baseline = prompts.evaluator_prompt(1, pin, "/box", "/repo", None)
    same = prompts.evaluator_prompt(1, pin, "/box", "/repo", None, acc_lines=None)
    assert baseline == same
    assert "FROZEN ACCEPTANCE" not in baseline


def test_solo_lead_and_repair_prompts_identical_with_acc_pass_left_default():
    baseline = prompts.solo_lead_prompt(1, 1, "why", "/box", "/repo", None)
    same = prompts.solo_lead_prompt(1, 1, "why", "/box", "/repo", None, acc_pass=None)
    assert baseline == same
    assert "FROZEN ACCEPTANCE" not in baseline

    baseline_r = prompts.repair_prompt(1, 1, None, "/box", "/repo", None)
    same_r = prompts.repair_prompt(1, 1, None, "/box", "/repo", None, acc_pass=None)
    assert baseline_r == same_r
    assert "FROZEN ACCEPTANCE" not in baseline_r


def test_integrate_prompt_identical_with_acc_pass_left_default():
    baseline = prompts.integrate_prompt(1, 1, True, [], {}, "/box", "/repo", None)
    same = prompts.integrate_prompt(1, 1, True, [], {}, "/box", "/repo", None, acc_pass=None)
    assert baseline == same


# --------------------------------------------------------------------------
# ocgen.py: the acceptance role
# --------------------------------------------------------------------------

def test_acceptance_role_in_roles_and_permissions():
    assert "acceptance" in ocgen.ROLES
    perm = ocgen.PERMISSIONS["acceptance"]
    assert perm["edit"] == "allow"
    assert perm["task"] == "deny"
    assert perm["webfetch"] == "deny"
    assert perm["websearch"] == "deny"
    assert perm["bash"]["*"] == "allow"
    assert perm["bash"]["git push*"] == "deny"  # the standard deny list


def test_acceptance_agent_body_loads_from_opencode_driver_agents(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    description, body = ocgen._load_role_body(repo_root, "acceptance")
    assert "# Role: Acceptance Author" in body
    assert description


def test_acceptance_agent_uses_lead_variant_not_its_own(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    d = config_mod._default_dict()
    d["variants"]["lead"] = "thinking"
    cfg = config_mod._to_config(d, source_path=None)
    doc = ocgen._build_opencode_json_v2(cfg, repo_root)
    assert doc["agent"]["trio-acceptance"]["variant"] == "thinking"
    assert doc["agent"]["trio-acceptance"]["model"] == cfg.model_for("acceptance")


def test_generate_writes_a_v2_acceptance_agent_entry(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    cfg = config_mod._to_config(config_mod._default_dict(), source_path=None)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    env = ocgen.generate(tmp_path / "run", cfg, repo_root, mailbox, style="v2")
    doc = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
    assert "trio-acceptance" in doc["agent"]

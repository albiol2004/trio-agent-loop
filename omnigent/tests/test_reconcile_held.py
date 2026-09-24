"""Offline tests: deterministic reconcile of a held lockstep Lead/Evaluator.

A held dispatch (post-delivery timeout, 31f5a5a) may be resumed only when
the proposed cursor-native completion receipt + input fence (contract v1
rev 2, `.runtime/reconcile-completion-audit/PROPOSED-CONTRACT.md`) proves
the original turn ENDED, and the artifact gate proves it valid. Idle,
dwell or a fresh artifact alone never do. Everything else stays held.

Run under `omnigent/tests/run_reconcile_offline.sh` (bwrap --unshare-net).
The broker here is a fake of that contract: labels on GET session, the
`input_fence` event, and a request log used to prove zero POST/DELETE.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
TREE = HERE.parents[1]
sys.path.insert(0, str(HERE))
import test_first_prompt_redelivery as h  # noqa: E402
from test_first_prompt_redelivery import clock  # noqa: E402,F401

trioctl = h.trioctl
rc = trioctl.reconcile
SID = "sess-late-1"
RUNNER = "runner-A"
CHAT = "chat-A"
EPOCH = 1790000000000
ATTEMPT = "a" * 32
PIN = "b" * 40
OPTIN = rc.CONTRACT["optin_label"]
RECEIPT = rc.CONTRACT["receipt_label"]
FENCE = rc.CONTRACT["fence_label"]


# -- fake contract broker ---------------------------------------------------

class ContractBroker:
    """GET session with contract labels; `input_fence` close; a log."""

    def __init__(self, *, receipt=None, status="idle", optin=True,
                 runner_id=RUNNER, prompt=None, fence_mode="verified"):
        self.receipt = receipt
        self.status = status
        self.optin = optin
        self.runner_id = runner_id
        self.prompt = prompt
        self.fence_mode = fence_mode
        self.fence = None
        self.requests: list[tuple[str, str]] = []
        self.gone = False
        self.unreachable = False
        self.after_fence = None  # callable(broker) run after a close
        self.chat = CHAT

    def get_session(self, sid):
        self.requests.append(("GET", f"/v1/sessions/{sid}"))
        if self.unreachable:
            raise trioctl.broker_http.BrokerHttpError("connection refused")
        if self.gone:
            raise trioctl.broker_http.BrokerHttpError("404", status_code=404)
        labels = {}
        if self.optin:
            labels[OPTIN] = "v1"
        if self.receipt is not None:
            core = {k: v for k, v in self.receipt.items() if k != "detail"}
            labels[RECEIPT] = json.dumps(core)
            if "detail" in self.receipt:
                labels[RECEIPT + ".detail"] = json.dumps(
                    self.receipt["detail"])
        if self.fence is not None:
            labels[FENCE] = json.dumps(self.fence)
        return {"id": sid, "status": self.status, "runner_id": self.runner_id,
                "external_session_id": self.chat, "labels": labels}

    def get_items(self, sid, *a, **k):
        self.requests.append(("GET", f"/v1/sessions/{sid}/items"))
        rows = []
        if self.prompt is not None:
            rows.append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": self.prompt}]})
        return {"data": rows}

    def input_fence(self, sid, action, fence_id, expect=None):
        self.requests.append(("POST", f"/v1/sessions/{sid}/events"))
        if self.fence_mode == "unsupported":
            raise trioctl.broker_http.BrokerHttpError(
                "capability_unsupported", status_code=409)
        if self.fence_mode == "ambiguous":
            raise trioctl.broker_http.BrokerRequestAmbiguous("reset")
        if self.fence and self.fence["fence_id"] != fence_id:
            raise trioctl.broker_http.BrokerHttpError(
                "fence_exists", status_code=409)
        self.last_expect = expect
        verified = self.fence_mode == "verified"
        self.fence = {"state": "closed", "fence_id": fence_id,
                      "verified": verified,
                      "reason": None if verified else self.fence_mode}
        if self.after_fence:
            self.after_fence(self)
        return {"fenced": True, "fence": dict(self.fence)}

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]


def good_receipt(prompt: str, copies: int = 1, *, detail=None, **over):
    """A stored rev-2 receipt: core fields plus ``detail`` (split on GET)."""
    sha = rc.sha256_text(prompt)
    receipt = {
        "v": 1, "epoch": EPOCH, "turn_seq": copies,
        "turn_end_count": copies, "inject_count_at_end": copies,
        "inject_count": copies, "rev": 1, "stop_reason": "completed",
        "chat_id": CHAT, "received_seq": 7,
    }
    receipt.update(over)
    receipt["detail"] = {
        "msg_digest": rc.message_digest([sha] * receipt["inject_count"]),
        "msg_listed": receipt["inject_count"], "truncated": False,
        "model_cmds": 0, "stop_raw": "completed",
        "observed_max_acked": True, "store_max": 40, "acked_through": 40,
        "unacked": 0,
    }
    receipt["detail"].update(detail or {})
    return receipt


# -- mailbox builder --------------------------------------------------------

def make_held(tmp_path: Path, role: str = "lead", *, legacy=False,
              copies: int = 1, write_artifact=True, prompt_text=None,
              extra_record=None, prov_over=None):
    """A held lockstep mailbox exactly as trioctl leaves one (new schema)."""
    mailbox = tmp_path / "loop"
    (mailbox / ".sessions").mkdir(parents=True)
    log0 = "# Trio loop log\n"
    verdict0 = "VERDICT: none\n"
    (mailbox / "LOG.md").write_text(log0, "utf-8")
    (mailbox / "VERDICT.md").write_text(verdict0, "utf-8")
    prior = "lead-running" if role == "lead" else "lead-done"
    ev = f"evaluated_sha: {PIN}\nevaluator_attempt: {ATTEMPT}\n" if (
        role == "evaluator") else "evaluated_sha: \nevaluator_attempt: \n"
    (mailbox / "STATE.md").write_text(
        f"iteration: 1\nstatus: running\nphase: {prior}\n{ev}", "utf-8")
    prompt = prompt_text or f"# role {role}\n\nDispatch nonce: {'c' * 32}\n"
    baseline = log0 if role in ("lead", "repair") else verdict0
    record = {
        "session_id": SID, "role": role, "iteration": 1,
        "title": "t", "hold": "role_completion_uncertain",
        "reason": "session timed out", "recorded_at": "2026-09-24T00:00:00Z",
    }
    if role == "evaluator":
        record.update(attempt=ATTEMPT, pinned_sha=PIN)
    if not legacy:
        prov = {
            "role": role, "iteration": 1, "mode": "lockstep", "kind": None,
            "slice": None,
            "attempt": ATTEMPT if role == "evaluator" else None,
            "pinned_sha": PIN if role == "evaluator" else None,
            "prior_status": "running", "prior_phase": prior,
            "prompt_sha256": rc.sha256_text(prompt),
            "prompt_bytes": len(prompt.encode()), "prompt_nonce": "c" * 32,
            "posted_copies": copies, "inject_seqs": [],
            "sent_prompt_shas": [rc.sha256_text(prompt)] * copies,
            "runner_id": RUNNER, "external_session_id": None,
            "terminal_epoch": None, "turn_seq_at_dispatch": 0,
            "product_head": None,
            "capabilities_at_dispatch": {"completion_receipts": True},
            "artifact_baseline": {
                "path": "LOG.md" if role in ("lead", "repair") else "VERDICT.md",
                "sha256": rc.sha256_text(baseline),
                "size": len(baseline), "mtime": 0.0,
            },
        }
        prov.update(prov_over or {})
        record["provenance"] = rc.write_provenance(
            mailbox, SID, prov, prompt, baseline)
    record.update(extra_record or {})
    held = mailbox / ".sessions" / f"held-{SID}.json"
    held.write_text(json.dumps(record, indent=2), "utf-8")
    trioctl._mark_mailbox_held(mailbox, record, held)
    if write_artifact:
        write_artifact_for(mailbox, role)
    return mailbox, prompt


def write_artifact_for(mailbox: Path, role: str):
    if role == "lead":
        with (mailbox / "LOG.md").open("a", encoding="utf-8") as f:
            f.write("- iter 1 | lead | built the slice\n")
    else:
        (mailbox / "VERDICT.md").write_text(
            f"VERDICT: SHIP\n\niteration: 1\nattempt: {ATTEMPT}\n"
            f"evaluated: {PIN}\n", "utf-8")


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        digest.update(rel.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def cli(argv):
    args = trioctl.parser().parse_args(argv)
    return args.func(args)


def decide(mailbox, broker):
    decision, obs = trioctl._reconcile_decision(mailbox, mailbox.parent, broker)
    return decision


def core_ok(monkeypatch):
    """The real loop core, with the commit gate stubbed (no git here)."""
    core = trioctl._load_trio_loop(TREE)
    monkeypatch.setattr(core, "run_commit_gate",
                        lambda mailbox, repo: (True, "commit gate passed"))
    return core


def apply(mailbox, broker, core, **kw):
    return rc.apply_once(
        mailbox, repo=None, client=broker, loop_core=core,
        artifact_ready=trioctl._role_artifact_ready,
        broker_http=trioctl.broker_http, **kw)


# -- provenance at dispatch -------------------------------------------------

def _t1_shape_hold(tmp_path, clock, **runner_kw):
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", "utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", "utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", "utf-8")
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    payloads = []
    real_request = broker._request

    def spy(method, path, payload=None, expected_status=200):
        if method == "POST" and path == "/v1/sessions":
            payloads.append(payload)
        return real_request(method, path, payload, expected_status)
    broker._request = spy
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, broker_client=broker, interval=1, timeout=300.0,
        **runner_kw)
    runner._agent_id = lambda role: "lead-agent"
    runner._resolve_model = lambda role: "m"
    runner._prompt = lambda *a, **k: h.PROMPT
    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        core.run_loop(mailbox, 3, runner, repo=None)
    return mailbox, broker, payloads


def test_default_dispatch_is_unchanged_without_opt_in(tmp_path, clock):
    """No --completion-receipts: no label, no nonce, no provenance files,
    and the held record has exactly the 31f5a5a fields."""
    mailbox, broker, payloads = _t1_shape_hold(tmp_path, clock)
    assert payloads and "labels" not in payloads[0]
    assert broker.turns == [h.PROMPT]
    record = json.loads(
        (mailbox / ".sessions" / f"held-{h.SID}.json").read_text())
    assert set(record) == {"session_id", "role", "iteration", "title",
                           "hold", "reason", "recorded_at"}
    names = sorted(p.name for p in (mailbox / ".sessions").iterdir())
    assert names == [f"held-{h.SID}.json"]
    decision = decide(mailbox, ContractBroker())
    assert decision["legacy_hold"] is True
    assert "unsupported" in decision["reasons"][0]


def test_loop_cli_completion_receipts_flag_defaults_off():
    args = trioctl.parser().parse_args(["omnigent", "loop"])
    assert args.completion_receipts is False and args.reconcile_held is False
    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--completion-receipts"])
    assert args.completion_receipts is True


def test_new_dispatch_saves_provenance_label_and_nonce(
    tmp_path, monkeypatch, clock
):
    """A real OmnigentRunner hold (T1 shape) now carries provenance; the
    create payload carries the opt-in label; the prompt has a nonce."""
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "STATE.md").write_text(
        "iteration: 0\nstatus: ready\nphase: idle\n", "utf-8")
    (mailbox / "LOG.md").write_text("# Trio loop log\n", "utf-8")
    (mailbox / "VERDICT.md").write_text("VERDICT: none\n", "utf-8")
    broker = h.Native014Broker(clock, row_lag=35.0, turn_len=100.0)
    payloads = []
    real_request = broker._request

    def spy(method, path, payload=None, expected_status=200):
        if method == "POST" and path == "/v1/sessions":
            payloads.append(payload)
        return real_request(method, path, payload, expected_status)
    broker._request = spy
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, broker_client=broker, interval=1, timeout=300.0,
        completion_receipts=True)
    runner._agent_id = lambda role: "lead-agent"
    runner._resolve_model = lambda role: "m"
    runner._prompt = lambda *a, **k: h.PROMPT
    with pytest.raises(trioctl.TrioctlError, match="held dispatch recorded"):
        core.run_loop(mailbox, 3, runner, repo=None)

    assert payloads and payloads[0]["labels"] == {OPTIN: "v1"}
    delivered = broker.turns[0]
    assert delivered.startswith(h.PROMPT) and "Dispatch nonce: " in delivered
    record = json.loads(
        (mailbox / ".sessions" / f"held-{h.SID}.json").read_text())
    prov = record["provenance"]
    assert prov["schema"] == rc.PROVENANCE_SCHEMA
    assert prov["prompt_sha256"] == rc.sha256_text(delivered)
    assert prov["prompt_nonce"] in delivered
    assert prov["posted_copies"] == 1
    assert prov["sent_prompt_shas"] == [rc.sha256_text(delivered)]
    assert prov["runner_id"] == "runner-1"
    assert prov["prior_phase"] == "lead-running" and prov["mode"] == "lockstep"
    assert prov["capabilities_at_dispatch"]["completion_receipts"] is True
    base = prov["artifact_baseline"]
    assert (mailbox / base["copy"]).read_text() == "# Trio loop log\n"
    assert (mailbox / prov["prompt_copy"]).read_text() == delivered
    saved = json.loads((mailbox / ".sessions" / f"dispatch-{h.SID}.json")
                       .read_text())
    assert saved["prompt_sha256"] == prov["prompt_sha256"]
    # Two dispatches never share a prompt hash.
    assert runner._new_dispatch_nonce() != runner._new_dispatch_nonce()


# -- decisions (dry run) ----------------------------------------------------

def test_ready_lead_late_valid_dry_run_has_zero_effects(tmp_path, capsys,
                                                       monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt), prompt=prompt)
    monkeypatch.setattr(trioctl, "_session_client", lambda url=None: broker)
    monkeypatch.chdir(tmp_path)
    before = tree_hash(tmp_path)
    code = cli(["omnigent", "reconcile", "--mailbox", str(mailbox),
                         "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["decision"]["action"] == "ready"
    assert out["decision"]["code"] == "late_valid_completion"
    assert out["decision"]["continuation"] == {"status": "running",
                                               "phase": "lead-done"}
    assert out["would_apply"] is True and out["dry_run"] is True
    assert tree_hash(tmp_path) == before
    assert broker.writes() == []
    assert not (mailbox / ".lock").exists()


@pytest.mark.parametrize("stop_reason", ["aborted", "unknown", "error"])
def test_stop_reason_is_informational(tmp_path, stop_reason):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(
        prompt, stop_reason=stop_reason, detail={"stop_raw": None}))
    decision = decide(mailbox, broker)
    assert decision["action"] == "ready"
    assert decision["evidence"]["stop_reason"] == stop_reason


def test_still_running_no_receipt_waits(tmp_path):
    mailbox, _ = make_held(tmp_path, write_artifact=False)
    decision = decide(mailbox, ContractBroker(status="running"))
    assert (decision["action"], decision["code"]) == ("waiting",
                                                      "session_running")


def test_thinking_gap_idle_with_artifact_but_no_receipt_waits(tmp_path):
    """Cursor thinking gap: idle, bound, and even a fresh LOG line are
    not completion without a turn-end receipt."""
    mailbox, _ = make_held(tmp_path)
    decision = decide(mailbox, ContractBroker(status="idle"))
    assert (decision["action"], decision["code"]) == ("waiting",
                                                      "turn_not_ended")


def test_receipt_for_an_older_turn_while_new_copy_pending_waits(tmp_path):
    mailbox, prompt = make_held(tmp_path, copies=2)
    receipt = good_receipt(prompt, copies=1)
    decision = decide(mailbox, ContractBroker(receipt=receipt))
    assert (decision["action"], decision["code"]) == ("waiting",
                                                      "turn_not_ended")


def test_turn_end_count_behind_inject_count_waits(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt, turn_end_count=0, inject_count_at_end=0)
    assert decide(mailbox, ContractBroker(receipt=receipt))["code"] == (
        "turn_not_ended")


def test_unacked_transcript_waits(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt, detail={
        "store_max": 41, "acked_through": 40, "observed_max_acked": False})
    assert decide(mailbox, ContractBroker(receipt=receipt))["code"] == (
        "transcript_not_flushed")


def test_more_turn_ends_than_injections_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt, turn_end_count=2, turn_seq=2)
    decision = decide(mailbox, ContractBroker(receipt=receipt))
    assert decision["action"] == "blocked"


def test_turn_seq_not_latest_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt, turn_seq=3)
    assert decide(mailbox, ContractBroker(receipt=receipt))["action"] == (
        "blocked")


def test_ended_without_valid_artifact_is_never_retried(tmp_path):
    mailbox, prompt = make_held(tmp_path, write_artifact=False)
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert (decision["action"], decision["code"]) == (
        "blocked", "ended_without_valid_artifact")


def test_stale_lead_line_in_baseline_is_not_fresh(tmp_path):
    """Rollout leftover R1: a same-iteration Lead line already in the
    dispatch baseline does not make the late pass valid."""
    mailbox, prompt = make_held(tmp_path, write_artifact=False)
    prov_path = mailbox / ".sessions" / f"dispatch-{SID}.json"
    stale = "# Trio loop log\n- iter 1 | lead | from an earlier pass\n"
    held = mailbox / ".sessions" / f"held-{SID}.json"
    record = json.loads(held.read_text())
    record["provenance"]["artifact_baseline"]["sha256"] = rc.sha256_text(stale)
    (mailbox / record["provenance"]["artifact_baseline"]["copy"]).write_text(
        stale)
    held.write_text(json.dumps(record))
    prov_path.write_text(json.dumps(record["provenance"]))
    text = (mailbox / "LOG.md").read_text()
    (mailbox / "LOG.md").write_text(stale + text[len("# Trio loop log\n"):])
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "ended_without_valid_artifact"


def test_evaluator_late_valid_is_ready(tmp_path):
    mailbox, prompt = make_held(tmp_path, "evaluator")
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["action"] == "ready"


@pytest.mark.parametrize("field,value", [
    ("evaluator_attempt", "d" * 32), ("evaluated_sha", "e" * 40)])
def test_evaluator_wrong_attempt_or_pin_in_state_blocks(tmp_path, field,
                                                        value):
    mailbox, prompt = make_held(tmp_path, "evaluator")
    trioctl.reconcile._rewrite_state(mailbox, {field: value})
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert (decision["action"], decision["code"]) == ("blocked",
                                                      "state_mismatch")


def test_evaluator_verdict_for_other_attempt_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path, "evaluator", write_artifact=False)
    (mailbox / "VERDICT.md").write_text(
        f"VERDICT: SHIP\n\niteration: 1\nattempt: {'f' * 32}\n"
        f"evaluated: {PIN}\n", "utf-8")
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "ended_without_valid_artifact"


@pytest.mark.parametrize("attr,value,code", [
    ("runner_id", "runner-B", "identity_mismatch"),
    ("chat", "chat-B", "identity_mismatch"),
    ("receipt_chat", None, "completion_unprovable_no_receipt"),
])
def test_identity_mismatch_blocks(tmp_path, attr, value, code):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    if attr == "receipt_chat":
        broker.receipt["chat_id"] = value
    else:
        setattr(broker, attr, value)
    decision = decide(mailbox, broker)
    assert (decision["action"], decision["code"]) == ("blocked", code)


def test_chat_differs_from_recorded_external_session_blocks(tmp_path):
    mailbox, prompt = make_held(
        tmp_path, prov_over={"external_session_id": "chat-Z"})
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "identity_mismatch"


def test_epoch_mismatch_blocks_when_recorded(tmp_path):
    mailbox, prompt = make_held(tmp_path, prov_over={"terminal_epoch": 1})
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "identity_mismatch"


def test_new_input_after_our_copy_blocks(tmp_path):
    """R2: a message Trio did not send changes the digest (same count)."""
    mailbox, prompt = make_held(tmp_path, copies=2)
    receipt = good_receipt(prompt, copies=2)
    receipt["detail"]["msg_digest"] = rc.message_digest(
        [rc.sha256_text(prompt), "0" * 64])
    decision = decide(mailbox, ContractBroker(receipt=receipt))
    assert decision["code"] == "new_input_after_dispatch"


def test_extra_injection_beyond_sent_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt, copies=2)
    decision = decide(mailbox, ContractBroker(receipt=receipt))
    assert decision["code"] == "new_input_after_dispatch"


def test_digest_is_contract_formula():
    sha = "ab" * 32
    assert rc.message_digest([sha]) == hashlib.sha256(
        sha.encode()).hexdigest()


def test_foreign_user_row_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt), prompt="hi there")
    assert decide(mailbox, broker)["code"] == "new_input_after_dispatch"


def test_truncated_or_malformed_receipt_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    lone_core = good_receipt(prompt)
    del lone_core["detail"]
    for receipt in (good_receipt(prompt, detail={"truncated": True}),
                    {**good_receipt(prompt), "v": 2},
                    {k: v for k, v in good_receipt(prompt).items()
                     if k != "rev"},
                    good_receipt(prompt, detail={"msg_digest": None}),
                    lone_core):
        decision = decide(mailbox, ContractBroker(receipt=receipt))
        assert (decision["action"], decision["code"]) == (
            "blocked", "completion_unprovable_no_receipt"), receipt


def test_capability_not_advertised_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt), optin=False)
    assert decide(mailbox, broker)["code"] == "completion_unprovable_no_receipt"


def test_broker_unreachable_waits_and_deleted_session_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    broker.unreachable = True
    assert decide(mailbox, broker)["code"] == "broker_unreachable"
    broker.unreachable, broker.gone = False, True
    decision = decide(mailbox, broker)
    assert (decision["action"], decision["code"]) == (
        "blocked", "completion_unprovable_no_receipt")
    assert "gone" in decision["reasons"][0]


def test_offline_is_not_observed(tmp_path):
    mailbox, _ = make_held(tmp_path)
    assert decide(mailbox, None)["code"] == "broker_not_observed"


def test_legacy_record_without_provenance_stays_held(tmp_path):
    mailbox, prompt = make_held(tmp_path, legacy=True)
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert (decision["action"], decision["code"]) == (
        "blocked", "completion_unprovable_no_receipt")
    assert decision["legacy_hold"] is True


@pytest.mark.parametrize("missing", ["runner_id", "posted_copies",
                                     "prompt_sha256", "sent_prompt_shas"])
def test_provenance_missing_a_required_field_stays_held(tmp_path, missing):
    mailbox, prompt = make_held(tmp_path, prov_over={missing: None})
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "completion_unprovable_no_receipt"


def test_malformed_and_multiple_holds_block(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    (mailbox / ".sessions" / "held-other.json").write_text("{not json")
    assert decide(mailbox, ContractBroker())["code"] == "multiple_holds"
    (mailbox / ".sessions" / f"held-{SID}.json").unlink()
    assert decide(mailbox, ContractBroker())["code"] == "hold_unreadable"


@pytest.mark.parametrize("extra,prov_over,code", [
    ({"hold": "first_prompt_uncertain"}, None, "unsupported_hold"),
    ({"kind": "slice-eval"}, None, "unsupported_role"),
    ({}, {"mode": "open-loop"}, "unsupported_role"),
])
def test_unsupported_holds_stay_held(tmp_path, extra, prov_over, code):
    mailbox, prompt = make_held(tmp_path, extra_record=extra,
                                prov_over=prov_over)
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert (decision["action"], decision["code"]) == ("blocked", code)


def test_repair_hold_is_unsupported(tmp_path):
    mailbox, prompt = make_held(tmp_path, "repair", write_artifact=False)
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "unsupported_role"


def test_state_hand_edited_off_needs_human_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    rc._rewrite_state(mailbox, {"status": "running", "phase": "lead-running"})
    decision = decide(mailbox, ContractBroker(receipt=good_receipt(prompt)))
    assert decision["code"] == "state_mismatch"


def test_foreign_fence_blocks(tmp_path):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    broker.fence = {"state": "closed", "fence_id": "someone-else",
                    "verified": True}
    assert decide(mailbox, broker)["code"] == "foreign_fence"


# -- apply ------------------------------------------------------------------

def test_apply_lead_resumes_phase_once(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    receipt = good_receipt(prompt)
    broker = ContractBroker(receipt=receipt)
    core = core_ok(monkeypatch)
    result = apply(mailbox, broker, core)
    assert result["action"] == "applied", result
    assert broker.writes() == [("POST", f"/v1/sessions/{SID}/events")]
    assert broker.last_expect == rc.receipt_key(receipt)
    state = rc.read_state(mailbox)
    assert (state["status"], state["phase"], state["iteration"]) == (
        "running", "lead-done", "1")
    assert state["evaluator_attempt"] == "" and state["evaluated_sha"] == ""
    assert not list((mailbox / ".sessions").glob("held-*.json"))
    retired = json.loads(
        (mailbox / ".sessions" / f"reconciled-{SID}.json").read_text())
    assert retired["reconciled"]["receipt"] == rc.receipt_key(receipt)
    log = (mailbox / "LOG.md").read_text()
    assert log.count("reconciled held lead session") == 1
    assert trioctl._held_dispatch_message(mailbox) is None
    # Repeat: no-op, no second fence POST, no second LOG line.
    again = apply(mailbox, broker, core)
    assert (again["action"], again["code"]) == ("applied", "already_applied")
    assert len(broker.writes()) == 1
    assert (mailbox / "LOG.md").read_text() == log


def test_apply_keeps_hold_when_decision_not_ready(tmp_path, monkeypatch):
    mailbox, _ = make_held(tmp_path)
    broker = ContractBroker(status="idle")
    before = tree_hash(mailbox)
    result = apply(mailbox, broker, core_ok(monkeypatch))
    assert result["code"] == "turn_not_ended"
    assert broker.writes() == []
    assert tree_hash(mailbox) == before


def test_apply_gate_failure_keeps_hold(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = trioctl._load_trio_loop(TREE)
    monkeypatch.setattr(core, "run_commit_gate",
                        lambda m, r: (False, "commit gate failed with exit 1"))
    result = apply(mailbox, broker, core)
    assert (result["action"], result["code"]) == ("blocked", "gate_failed")
    assert list((mailbox / ".sessions").glob("held-*.json"))
    assert rc.read_state(mailbox)["status"] == "needs_human"


def test_mailbox_lock_excludes_second_reconciler(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    holder = subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(30)"])
    try:
        (mailbox / ".lock").mkdir()
        (mailbox / ".lock" / "pid").write_text(f"{holder.pid}\n")
        before = tree_hash(mailbox)
        result = apply(mailbox, broker, core_ok(monkeypatch))
        assert (result["action"], result["code"]) == ("waiting",
                                                      "mailbox_locked")
        assert broker.requests == [] and tree_hash(mailbox) == before
    finally:
        holder.kill()
        holder.wait()
    shutil.rmtree(mailbox / ".lock")
    assert apply(mailbox, broker, core_ok(monkeypatch))["action"] == "applied"


class Crash(Exception):
    pass


def _crash_first(monkeypatch, name, when=lambda *a, **k: True):
    real = getattr(rc, name)
    state = {"done": False}

    def boom(*a, **k):
        if not state["done"] and when(*a, **k):
            state["done"] = True
            raise Crash(f"crash in {name}")
        return real(*a, **k)
    monkeypatch.setattr(rc, name, boom)


def test_crash_after_own_state_write_finishes_without_new_fence(
    tmp_path, monkeypatch
):
    """Crash after LOG+STATE were written, before the hold rename: the
    replay recognizes exactly its own writes and finishes."""
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    _crash_first(monkeypatch, "durable_write_json",
                 lambda path, obj: Path(path).name.startswith("reconciled-"))
    with pytest.raises(Crash):
        apply(mailbox, broker, core)
    assert rc.read_journal(mailbox, SID)["step"] == "applying"
    assert rc.read_state(mailbox)["phase"] == "lead-done"
    assert list((mailbox / ".sessions").glob("held-*.json"))
    assert not (mailbox / ".lock").exists()  # released on unwind
    broker.unreachable = True  # own writes need no broker to finish
    result = apply(mailbox, broker, core)
    assert (result["action"], result["code"]) == (
        "applied", "resumed_interrupted_apply")
    assert "(after)" in result["reasons"][0]
    assert len(broker.writes()) == 1
    assert (mailbox / "LOG.md").read_text().count("reconciled held") == 1


def test_crash_before_any_write_revalidates_then_applies(tmp_path,
                                                         monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    _crash_first(monkeypatch, "_finish")
    with pytest.raises(Crash):
        apply(mailbox, broker, core)
    broker.unreachable = True  # "before" needs a fresh decision
    assert apply(mailbox, broker, core)["code"] == "broker_unreachable"
    assert rc.read_state(mailbox)["status"] == "needs_human"
    broker.unreachable = False
    result = apply(mailbox, broker, core)
    assert result["action"] == "applied" and "(before)" in result["reasons"][0]
    assert len(broker.writes()) == 1


@pytest.mark.parametrize("change", [
    "log_line_removed", "log_rewritten", "state_operator_blocked",
    "state_after_crash_edited", "receipt_changed", "gate_now_fails"])
def test_interrupted_apply_preserves_external_changes(tmp_path, monkeypatch,
                                                      change):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    if change == "state_after_crash_edited":
        _crash_first(monkeypatch, "durable_write_json",
                     lambda path, obj: Path(path).name.startswith(
                         "reconciled-"))
    else:
        _crash_first(monkeypatch, "_finish")
    with pytest.raises(Crash):
        apply(mailbox, broker, core)
    log, state = mailbox / "LOG.md", mailbox / "STATE.md"
    if change == "log_line_removed":
        log.write_text(log.read_text().replace(
            "- iter 1 | lead | built the slice\n", ""))
    elif change == "log_rewritten":
        log.write_text("# Trio loop log\n")
    elif change == "state_operator_blocked":
        state.write_text(state.read_text().replace(
            "status: needs_human", "status: blocked"))
    elif change == "state_after_crash_edited":
        state.write_text(state.read_text().replace(
            "phase: lead-done", "phase: lead-running"))
    elif change == "receipt_changed":
        broker.receipt = good_receipt(prompt, rev=2, detail={
            "observed_max_acked": False, "store_max": 41})
    else:
        monkeypatch.setattr(core, "run_commit_gate",
                            lambda m, r: (False, "commit gate failed"))
    snapshot = {n: (mailbox / n).read_text() for n in ("LOG.md", "STATE.md")}
    result = apply(mailbox, broker, core)
    assert result["action"] != "applied", result
    assert {n: (mailbox / n).read_text()
            for n in ("LOG.md", "STATE.md")} == snapshot
    assert list((mailbox / ".sessions").glob("held-*.json"))
    assert rc.read_journal(mailbox, SID)["step"] == "applying"


def test_interrupted_evaluator_apply_with_changed_verdict_is_kept(
    tmp_path, monkeypatch
):
    mailbox, prompt = make_held(tmp_path, "evaluator")
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    _crash_first(monkeypatch, "_finish")
    with pytest.raises(Crash):
        apply(mailbox, broker, core)
    verdict = mailbox / "VERDICT.md"
    verdict.write_text(verdict.read_text().replace("SHIP", "ITERATE"))
    result = apply(mailbox, broker, core)
    assert result["action"] == "blocked"
    assert rc.read_state(mailbox)["status"] == "needs_human"


def test_stale_applying_journal_never_rewrites_state(tmp_path, monkeypatch):
    """Crash after the hold was retired but before `done`: a later apply
    (e.g. for a new hold) only marks the journal done; STATE, which the
    loop has since advanced, is untouched."""
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    real = rc.durable_write_json

    def crash_on_done(path, obj):
        if isinstance(obj, dict) and obj.get("step") == "done":
            raise KeyboardInterrupt("crash before done")
        return real(path, obj)
    monkeypatch.setattr(rc, "durable_write_json", crash_on_done)
    with pytest.raises(KeyboardInterrupt):
        apply(mailbox, broker, core)
    monkeypatch.setattr(rc, "durable_write_json", real)
    assert rc.read_journal(mailbox, SID)["step"] == "applying"
    assert not list((mailbox / ".sessions").glob("held-*.json"))
    rc._rewrite_state(mailbox, {"status": "shipped", "phase": "shipped"})
    state = (mailbox / "STATE.md").read_text()
    # A different, legacy hold appears later.
    (mailbox / ".sessions" / "held-other.json").write_text(json.dumps(
        {"session_id": "other", "role": "lead", "iteration": 2,
         "hold": "role_completion_uncertain"}))
    result = apply(mailbox, broker, core)
    assert result["action"] == "blocked"
    assert (mailbox / "STATE.md").read_text() == state
    assert rc.read_journal(mailbox, SID)["step"] == "done"
    assert len(broker.writes()) == 1


def test_crash_after_fence_close_reuses_fence_id(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    real = rc._gates
    monkeypatch.setattr(rc, "_gates", lambda *a: (_ for _ in ()).throw(
        KeyboardInterrupt("crash after fence")))
    with pytest.raises(KeyboardInterrupt):
        apply(mailbox, broker, core)
    journal = rc.read_journal(mailbox, SID)
    assert journal["step"] == "fence_closed"
    fence_id = journal["fence_id"]
    monkeypatch.setattr(rc, "_gates", real)
    result = apply(mailbox, broker, core)
    assert result["action"] == "applied"
    assert broker.fence["fence_id"] == fence_id


def test_unverified_fence_retries_same_id_then_applies(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt),
                            fence_mode="runner_unreachable")
    core = core_ok(monkeypatch)
    first = apply(mailbox, broker, core)
    assert (first["action"], first["code"]) == ("waiting", "fence_retry")
    assert list((mailbox / ".sessions").glob("held-*.json"))
    fence_id = rc.read_journal(mailbox, SID)["fence_id"]
    broker.fence_mode = "verified"
    assert apply(mailbox, broker, core)["action"] == "applied"
    assert broker.fence["fence_id"] == fence_id


@pytest.mark.parametrize("mode,action", [
    ("unsupported", "blocked"), ("ambiguous", "waiting"),
    ("inputs_stale", "blocked")])
def test_fence_refusal_keeps_hold(tmp_path, monkeypatch, mode, action):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt), fence_mode=mode)
    result = apply(mailbox, broker, core_ok(monkeypatch))
    assert result["action"] == action
    assert list((mailbox / ".sessions").glob("held-*.json"))
    assert rc.read_state(mailbox)["status"] == "needs_human"


def test_receipt_change_after_fence_keeps_hold(tmp_path, monkeypatch):
    """Race: a new input lands between the first decision and the fence."""
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))

    def race(b):
        b.receipt = good_receipt(prompt, turn_seq=2, turn_end_count=2)
    broker.after_fence = race
    result = apply(mailbox, broker, core_ok(monkeypatch))
    assert result["action"] == "blocked"
    assert list((mailbox / ".sessions").glob("held-*.json"))


# -- loop integration -------------------------------------------------------

class FakeLoopRunner:
    """Records every dispatch; the Evaluator writes this attempt's SHIP."""

    instances: list["FakeLoopRunner"] = []

    def __init__(self, **kwargs):
        self.calls = []
        self.created_session_ids = []
        self.held_session_ids = []
        FakeLoopRunner.instances.append(self)

    def run(self, role, iteration, mailbox, context=None):
        self.calls.append((role, iteration, dict(context or {})))
        if role == "evaluator":
            (Path(mailbox) / "VERDICT.md").write_text(
                f"VERDICT: SHIP\n\niteration: {iteration}\n"
                f"attempt: {context['evaluator_attempt']}\n"
                f"evaluated: {context['pinned_sha']}\n", "utf-8")
        return 0


def _loop(monkeypatch, tmp_path, mailbox, broker, core, *flags):
    FakeLoopRunner.instances = []
    monkeypatch.setattr(trioctl, "OmnigentRunner", FakeLoopRunner)
    monkeypatch.setattr(trioctl, "_session_client", lambda url=None: broker)
    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: core)
    monkeypatch.setattr(trioctl, "_run_post_loop_session_prune",
                        lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    return cli(["omnigent", "loop", "--mailbox", str(mailbox),
                         "--max-iterations", "1", *flags])


def test_loop_default_off_keeps_hold_with_zero_http(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    before = tree_hash(mailbox)
    code = _loop(monkeypatch, tmp_path, mailbox, broker, core_ok(monkeypatch))
    assert code == trioctl.HELD_DISPATCH_EXIT
    assert broker.requests == [] and tree_hash(mailbox) == before
    assert FakeLoopRunner.instances == []


def test_loop_reconcile_held_runs_exactly_one_evaluator(tmp_path,
                                                       monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    broker = ContractBroker(receipt=good_receipt(prompt))
    core = core_ok(monkeypatch)
    code = _loop(monkeypatch, tmp_path, mailbox, broker, core,
                 "--reconcile-held")
    assert code == 0
    calls = [c for r in FakeLoopRunner.instances for c in r.calls]
    assert [(role, it) for role, it, _ in calls] == [("evaluator", 1)]
    assert rc.read_state(mailbox)["status"].startswith("shipped")
    # Repeat: no hold, terminal state, no dispatch, no fence.
    code = _loop(monkeypatch, tmp_path, mailbox, broker, core,
                 "--reconcile-held")
    assert code == 0
    assert [c for r in FakeLoopRunner.instances for c in r.calls] == []
    assert len(broker.writes()) == 1


def test_loop_reconcile_held_not_ready_keeps_exit_7(tmp_path, monkeypatch):
    mailbox, _ = make_held(tmp_path)
    broker = ContractBroker(status="idle")  # no receipt
    code = _loop(monkeypatch, tmp_path, mailbox, broker, core_ok(monkeypatch),
                 "--reconcile-held")
    assert code == trioctl.HELD_DISPATCH_EXIT
    assert broker.writes() == []
    assert FakeLoopRunner.instances == []


def test_loop_reconcile_evaluator_applies_verdict_without_redispatch(
    tmp_path, monkeypatch
):
    mailbox, prompt = make_held(tmp_path, "evaluator")
    broker = ContractBroker(receipt=good_receipt(prompt))
    code = _loop(monkeypatch, tmp_path, mailbox, broker, core_ok(monkeypatch),
                 "--reconcile-held")
    assert code == 0
    assert [c for r in FakeLoopRunner.instances for c in r.calls] == []
    assert rc.read_state(mailbox)["status"].startswith("shipped")


def test_evaluator_ship_without_retirement_is_not_shipped(tmp_path,
                                                         monkeypatch):
    """With a git repo the reconciled SHIP still needs its retirement
    commit: the loop's full SHIP gate is preserved (exit 6)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "README").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"],
                   check=True, env=env)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    global PIN
    saved, PIN = PIN, head
    try:
        mailbox, prompt = make_held(repo, "evaluator")
    finally:
        PIN = saved
    monkeypatch.setenv("TRIO_RETIREMENT_WAIT_SECONDS", "0")
    broker = ContractBroker(receipt=good_receipt(prompt))
    code = _loop(monkeypatch, repo, mailbox, broker, core_ok(monkeypatch),
                 "--reconcile-held")
    assert code == 6
    assert rc.read_state(mailbox)["status"].startswith("needs_retirement")
    assert [c for r in FakeLoopRunner.instances for c in r.calls] == []


# -- the saved live T1 fixture (read-only copy) -----------------------------

T1 = Path(os.environ.get(
    "TRIO_T1_FIXTURE",
    "/home/coder/workflow-lab/.runtime/t1-canary-31f5a5a/live-staged-1"))


@pytest.mark.skipif(not (T1 / "repo").is_dir(), reason="T1 fixture absent")
def test_t1_fixture_legacy_hold_stays_held(tmp_path, capsys, monkeypatch):
    src = next((T1 / "repo").glob("t1c-*"))
    before = tree_hash(src)
    mailbox = tmp_path / src.name
    shutil.copytree(src, mailbox)
    broker = ContractBroker()
    broker.gone = True  # the original session was deleted after the canary
    monkeypatch.setattr(trioctl, "_session_client", lambda url=None: broker)
    monkeypatch.chdir(tmp_path)
    copy_before = tree_hash(mailbox)
    code = cli(["omnigent", "reconcile", str(mailbox), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == trioctl.HELD_DISPATCH_EXIT
    decision = out["decision"]
    assert (decision["action"], decision["code"]) == (
        "blocked", "completion_unprovable_no_receipt")
    assert decision["legacy_hold"] is True
    assert out["observation"]["record"]["session_id"] == (
        "0a5aa757b571461d938beb55e063e469")
    # Read-only: at most the snapshot GET, never POST/DELETE.
    assert broker.writes() == []
    assert all(method == "GET" for method, _ in broker.requests)
    assert tree_hash(mailbox) == copy_before
    assert tree_hash(src) == before


# -- mailbox lock: real cross-process ownership ------------------------------

def _hold_lock_child(mailbox, ready, release, result):
    core = trioctl._load_trio_loop(TREE)
    lock = core._acquire_lock(Path(mailbox))
    result.put(None if lock is None else (lock / "owner").read_text())
    ready.set()
    release.wait(30)
    core._release_lock(lock)


def _two_process(mailbox):
    import multiprocessing
    ctx = multiprocessing.get_context("fork")
    ready, release, result = ctx.Event(), ctx.Event(), ctx.Queue()
    proc = ctx.Process(target=_hold_lock_child,
                       args=(str(mailbox), ready, release, result))
    proc.start()
    assert ready.wait(30)
    return proc, release, result.get(timeout=5)


def test_second_process_cannot_take_or_release_a_held_lock(tmp_path):
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    proc, release, owner = _two_process(mailbox)
    try:
        assert owner  # child owns it
        assert core._acquire_lock(mailbox) is None
        # A forged release from this process never removes the lock.
        core._release_lock(mailbox / ".lock")
        core._LOCK_TOKENS[str(mailbox / ".lock")] = "not-the-owner"
        core._release_lock(mailbox / ".lock")
        assert (mailbox / ".lock" / "owner").read_text() == owner
    finally:
        release.set()
        proc.join(30)
    lock = core._acquire_lock(mailbox)
    assert lock is not None
    core._release_lock(lock)
    assert not lock.exists()


def test_acquire_is_serialized_across_processes(tmp_path):
    """Deterministic mkdir-before-pid window: the holder pauses inside
    _acquire_lock after mkdir; a second process's acquire waits on the
    mailbox guard and then sees a live owner (never a pid-less lock)."""
    import multiprocessing
    import threading
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    ctx = multiprocessing.get_context("fork")
    in_window, go, result = ctx.Event(), ctx.Event(), ctx.Queue()

    def child():
        real = core._write_lock_file

        def slow(lock, name, text):
            if name == "owner":
                in_window.set()
                go.wait(30)
            return real(lock, name, text)
        core._write_lock_file = slow
        lock = core._acquire_lock(mailbox)
        result.put(lock is not None)
        go.wait(30)
    proc = ctx.Process(target=child)
    proc.start()
    assert in_window.wait(30)
    assert (mailbox / ".lock").is_dir()
    assert not (mailbox / ".lock" / "pid").exists()  # inside the window
    got = []
    t = threading.Thread(target=lambda: got.append(
        core._acquire_lock(mailbox)))
    t.start()
    t.join(0.5)
    assert t.is_alive()  # blocked on the guard, not stealing
    go.set()
    t.join(30)
    assert result.get(timeout=5) is True
    assert got == [None]
    proc.join(30)


def _hammer(mailbox, rounds, q):
    import contextlib
    import io
    core = trioctl._load_trio_loop(TREE)
    overlaps = errors = got = 0
    for _ in range(rounds):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                lock = core._acquire_lock(Path(mailbox))
        except OSError:
            errors += 1
            continue
        if lock is None:
            continue
        got += 1
        marker = Path(mailbox) / "owner-marker"
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            os.unlink(marker)
        except FileExistsError:
            overlaps += 1
        core._release_lock(lock)
    q.put((overlaps, errors, got))


def test_two_processes_never_overlap(tmp_path):
    import multiprocessing
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_hammer, args=(str(mailbox), 1500, q))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    results = [q.get(timeout=5) for _ in procs]
    assert all(r[0] == 0 and r[1] == 0 for r in results), results
    assert sum(r[2] for r in results) > 0
    assert not (mailbox / ".lock").exists()
    assert not list(mailbox.glob(".lock.stale-*"))


def test_fresh_pidless_lock_is_not_stolen_but_old_one_is(tmp_path):
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    (mailbox / ".lock").mkdir()
    assert core._acquire_lock(mailbox) is None
    old = time.time() - core.LOCK_EMPTY_GRACE_SECONDS - 5
    os.utime(mailbox / ".lock", (old, old))
    lock = core._acquire_lock(mailbox)
    assert lock is not None
    assert (lock / "pid").read_text().strip() == str(os.getpid())
    core._release_lock(lock)


def test_dead_pid_lock_is_taken_over_and_released(tmp_path):
    core = trioctl._load_trio_loop(TREE)
    mailbox = tmp_path / "mb"
    mailbox.mkdir()
    (mailbox / ".lock").mkdir()
    (mailbox / ".lock" / "pid").write_text("999999999\n")
    lock = core._acquire_lock(mailbox)
    assert lock is not None
    core._release_lock(lock)
    assert not (mailbox / ".lock").exists()


def test_reconciler_and_driver_exclude_each_other(tmp_path, monkeypatch):
    mailbox, prompt = make_held(tmp_path)
    proc, release, owner = _two_process(mailbox)
    try:
        broker = ContractBroker(receipt=good_receipt(prompt))
        result = apply(mailbox, broker, core_ok(monkeypatch))
        assert result["code"] == "mailbox_locked"
        assert broker.requests == []
        assert (mailbox / ".lock" / "owner").read_text() == owner
    finally:
        release.set()
        proc.join(30)

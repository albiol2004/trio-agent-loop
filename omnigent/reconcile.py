"""Deterministic reconciler for a held lockstep Lead/Evaluator dispatch.

A held record (``<mailbox>/.sessions/held-<sid>.json``, written by trioctl
``_hold_dispatch``) means a prompt was delivered but its role did not
finish inside the run. This module decides, from observed state only,
whether that original turn provably ENDED with a valid artifact, and if so
resumes the interrupted *phase* (never the whole iteration, never a
re-dispatch). It is ordinary code: observed state -> action. There is no
classifier and no model call.

Completion proof comes only from the proposed cursor-native completion
receipt + input fence (``.runtime/reconcile-completion-audit/
PROPOSED-CONTRACT.md``, v1). Idle status, idle dwell, an assistant row,
or a fresh artifact are never completion, alone or together. Every
server field name lives in ``CONTRACT`` so the adapter can be aligned to
the published contract in one place. Until a server provides it, every
decision is ``blocked/completion_unprovable_no_receipt``.

Pieces:
- ``decide(record, obs)``: pure; returns ``waiting`` / ``ready`` /
  ``blocked`` with a stable ``code`` and ``reasons``.
- ``observe(...)``: read-only (GET snapshot, GET items, mailbox, git).
- ``apply_once(...)``: under the mailbox ``.lock`` with a durable journal.

Stdlib only; loaded by trioctl as a sibling file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

PROVENANCE_SCHEMA = "trio.dispatch_provenance.v1"
JOURNAL_SCHEMA = "trio.reconcile_journal.v1"

# Proposed contract v1 (revision 3): every server-side name the reconciler reads.
CONTRACT: dict[str, Any] = {
    "optin_label": "omnigent.cursor_native.completion_receipts",
    "optin_value": "v1",
    "receipt_label": "omnigent.cursor_native.turn_receipt",
    "fence_label": "omnigent.input_fence",
    "receipt_version": 1,
    # Revision 2 stored form: a compact core label + a detail label, both
    # JSON strings on GET /v1/sessions/{id} -> labels (256-char limit).
    "receipt_detail_label": "omnigent.cursor_native.turn_receipt.detail",
    "receipt_required": (
        "v", "epoch", "turn_seq", "turn_end_count", "inject_count",
        "inject_count_at_end", "rev", "stop_reason", "chat_id",
        "received_seq",
    ),
    "detail_required": (
        "msg_digest", "msg_listed", "truncated", "model_cmds", "stop_raw",
        "observed_max_acked", "store_max", "acked_through", "unacked",
    ),
    # Revision 3 turn binding (R3b): a third server-reserved label written
    # in the same atomic upsert as core and detail.
    "receipt_binding_label": "omnigent.cursor_native.turn_receipt.binding",
    "binding_reasons": (
        "inject_unhashed", "user_rows_mismatch", "turn_end_unbound",
        "duplicate_turn_end", "turn_open", "store_unbound", "no_binding",
    ),
}

# Provenance a record needs before any receipt can be bound to it.
REQUIRED_PROVENANCE = (
    "role", "iteration", "mode", "prior_phase", "prompt_sha256",
    "posted_copies", "sent_prompt_shas", "session_id", "runner_id", "artifact_baseline",
    "capabilities_at_dispatch",
)

# Continuation per supported role: the phase the loop resumes. Lead ->
# lead-done (the loop runs the Evaluator for the same iteration with a
# fresh attempt/pin; it never re-runs the Lead). Evaluator -> lead-done
# with attempt/pin unchanged (`_fresh_evaluator_artifact` is then true,
# so the Evaluator is not dispatched again and the verdict is applied
# through `_apply_verdict`/`_finalize_ship`).
CONTINUATION = {
    "lead": {"status": "running", "phase": "lead-done"},
    "evaluator": {"status": "running", "phase": "lead-done"},
}
PRIOR_PHASE = {"lead": "lead-running", "evaluator": "lead-done"}
# Contract R6: a close left in these states is retried, never abandoned.
FENCE_RETRY_REASONS = ("runner_unreachable", "inputs_not_drained")
ARTIFACT = {"lead": "LOG.md", "evaluator": "VERDICT.md"}


# -- small durable-file helpers -----------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slug(session_id: str) -> str:
    """Same slug as trioctl `_held_record_path`."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", session_id).strip("-") or "session"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def durable_write(path: Path, data: str) -> None:
    """temp file, fsync, rename, fsync dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def durable_write_json(path: Path, obj: Any) -> None:
    durable_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# -- provenance (written by trioctl at dispatch) -------------------------

def provenance_paths(mailbox: Path, session_id: str) -> dict[str, Path]:
    s = slug(session_id)
    base = mailbox / ".sessions"
    return {
        "provenance": base / f"dispatch-{s}.json",
        "prompt": base / f"prompt-{s}.txt",
        "baseline": base / f"baseline-{s}.txt",
    }


def write_provenance(
    mailbox: Path, session_id: str, provenance: dict[str, Any],
    prompt: str, baseline_text: str,
) -> dict[str, Any]:
    """Durably save prompt + baseline copies and the provenance record.

    Returns the provenance with the relative copy paths filled in. Called
    right after create returns (before the wait); a later hold embeds it.
    """
    paths = provenance_paths(mailbox, session_id)
    durable_write(paths["prompt"], prompt)
    durable_write(paths["baseline"], baseline_text)
    record = dict(provenance)
    record["schema"] = PROVENANCE_SCHEMA
    record["session_id"] = session_id
    record["prompt_copy"] = str(paths["prompt"].relative_to(mailbox))
    baseline = dict(record.get("artifact_baseline") or {})
    baseline["copy"] = str(paths["baseline"].relative_to(mailbox))
    record["artifact_baseline"] = baseline
    durable_write_json(paths["provenance"], record)
    return record


# -- contract adapter ----------------------------------------------------

def _labels(snapshot: Any) -> dict[str, Any]:
    labels = snapshot.get("labels") if isinstance(snapshot, dict) else None
    return labels if isinstance(labels, dict) else {}


def _label_json(snapshot: Any, key: str) -> tuple[Any, str | None]:
    """A label value that may be stored as JSON text or as an object."""
    value = _labels(snapshot).get(key)
    if value is None:
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, f"{key} is not JSON"
    if not isinstance(value, dict):
        return None, f"{key} is not an object"
    return value, None


def capabilities(snapshot: Any) -> dict[str, bool]:
    """What the session advertises: the v1 opt-in label, exactly."""
    value = _labels(snapshot).get(CONTRACT["optin_label"])
    return {"completion_receipts": value == CONTRACT["optin_value"]}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_receipt(snapshot: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(receipt, None), (None, None) when absent, (None, problem) if bad.

    The receipt is the core label with the detail label under ``detail``;
    either one missing or malformed is a problem (the pair is written in
    one atomic upsert, so a lone core is not a receipt).
    """
    core, problem = _label_json(snapshot, CONTRACT["receipt_label"])
    if problem:
        return None, problem
    detail, detail_problem = _label_json(
        snapshot, CONTRACT["receipt_detail_label"]
    )
    if core is None and detail is None:
        return None, None
    if detail_problem or core is None or detail is None:
        return None, detail_problem or "receipt core/detail label missing"
    if core.get("v") != CONTRACT["receipt_version"]:
        return None, f"receipt.v is {core.get('v')!r}, not 1"
    missing = [k for k in CONTRACT["receipt_required"] if k not in core]
    missing += [
        f"detail.{k}" for k in CONTRACT["detail_required"] if k not in detail
    ]
    if missing:
        return None, "receipt missing " + ",".join(sorted(set(missing)))
    for key in ("epoch", "turn_seq", "turn_end_count", "inject_count_at_end",
                "inject_count", "rev", "received_seq"):
        if not _is_int(core.get(key)):
            return None, f"receipt.{key} is not an integer"
    for key in ("msg_listed", "store_max", "acked_through", "unacked",
                "model_cmds"):
        if not _is_int(detail.get(key)):
            return None, f"receipt detail.{key} is not an integer"
    for key in ("truncated", "observed_max_acked"):
        if not isinstance(detail.get(key), bool):
            return None, f"receipt detail.{key} is not a boolean"
    if not isinstance(detail.get("msg_digest"), str):
        return None, "receipt detail.msg_digest is not a string"
    return dict(core, detail=detail), None


def parse_binding(snapshot: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(binding, None) when the rev3 binding label is well formed, else
    (None, problem). Absent is a problem too: R3b holds without it."""
    key = CONTRACT["receipt_binding_label"]
    binding, problem = _label_json(snapshot, key)
    if problem:
        return None, f"receipt binding malformed: {problem}"
    if binding is None:
        return None, (f"receipt binding label {key} missing (server predates "
                      "contract rev3 turn binding)")
    missing = [k for k in ("bound", "reason", "user_rows", "closed_turns")
               if k not in binding]
    if missing:
        return None, "receipt binding missing " + ",".join(missing)
    bound, reason = binding["bound"], binding["reason"]
    if bound is not None and not isinstance(bound, bool):
        return None, f"receipt binding.bound is {bound!r}, not a bool or null"
    if reason is not None and reason not in CONTRACT["binding_reasons"]:
        return None, f"receipt binding.reason {reason!r} is not a contract reason"
    if (bound is True) != (reason is None):
        return None, (f"receipt binding bound {bound!r} with reason "
                      f"{reason!r} is inconsistent")
    for count in ("user_rows", "closed_turns"):
        value = binding[count]
        if value is not None and (not _is_int(value) or value < 0):
            return None, f"receipt binding.{count} is {value!r}, not a count"
    return binding, None


def binding_key(binding: dict[str, Any]) -> dict[str, Any]:
    """The binding values a post-fence re-read must preserve (R6)."""
    return {k: binding.get(k)
            for k in ("bound", "reason", "user_rows", "closed_turns")}


def message_digest(shas: list[str]) -> str:
    """Contract R2: sha256 of the newline-joined message sha256 hex list."""
    return hashlib.sha256("\n".join(shas).encode("utf-8")).hexdigest()


def parse_fence(snapshot: Any) -> tuple[dict[str, Any] | None, str | None]:
    return _label_json(snapshot, CONTRACT["fence_label"])


def receipt_key(receipt: dict[str, Any]) -> dict[str, Any]:
    """The values a fence `expect` and a post-fence re-read must match."""
    return {
        "epoch": receipt.get("epoch"),
        "turn_seq": receipt.get("turn_seq"),
        "inject_count": receipt.get("inject_count"),
        "turn_end_count": receipt.get("turn_end_count"),
        "chat_id": receipt.get("chat_id"),
    }


# -- decision (pure) -----------------------------------------------------

def _decision(action: str, code: str, reasons: list[str], **extra: Any) -> dict:
    out = {"action": action, "code": code, "reasons": reasons}
    out.update(extra)
    return out


def _blocked(code: str, *reasons: str, **extra: Any) -> dict:
    return _decision("blocked", code, list(reasons), **extra)


def _waiting(code: str, *reasons: str, **extra: Any) -> dict:
    return _decision("waiting", code, list(reasons), **extra)


def _provenance_problem(prov: Any) -> str | None:
    if not isinstance(prov, dict) or prov.get("schema") != PROVENANCE_SCHEMA:
        return ("legacy hold: no dispatch provenance (dispatched without "
                "--completion-receipts); unsupported, resolve by hand")
    missing = [
        k for k in REQUIRED_PROVENANCE if prov.get(k) in (None, "", [], {})
    ]
    baseline = prov.get("artifact_baseline")
    if isinstance(baseline, dict):
        missing += [
            f"artifact_baseline.{k}" for k in ("path", "sha256", "copy")
            if not baseline.get(k)
        ]
    if not _is_int(prov.get("posted_copies")) or prov.get("posted_copies", 0) < 1:
        missing.append("posted_copies>=1")
    sent = prov.get("sent_prompt_shas")
    if not isinstance(sent, list) or len(sent) != prov.get("posted_copies") or (
        any(sha != prov.get("prompt_sha256") for sha in sent)
    ):
        missing.append("sent_prompt_shas (one per posted copy)")
    if missing:
        return "provenance missing " + ",".join(sorted(set(missing)))
    return None


def decide(record: Any, obs: dict[str, Any]) -> dict[str, Any]:
    """observed state -> {action, code, reasons, continuation?}.

    The first matching rule wins (design §3). ``obs`` comes from
    ``observe``; tests build it directly.
    """
    holds = obs.get("holds") or []
    if len(holds) > 1:
        return _blocked("multiple_holds", f"{len(holds)} held records")
    if not isinstance(record, dict) or not record.get("session_id"):
        return _blocked("hold_unreadable", "held record is not a JSON object "
                        "with a session_id")
    if record.get("hold") != "role_completion_uncertain":
        return _blocked("unsupported_hold",
                        f"hold {record.get('hold')!r} is resolved by a person")
    prov = record.get("provenance")
    role = record.get("role")
    mode = (prov or {}).get("mode") if isinstance(prov, dict) else None
    if (
        role not in CONTINUATION
        or record.get("kind") or record.get("slice")
        or (mode not in (None, "lockstep"))
    ):
        return _blocked("unsupported_role",
                        f"role {role!r} mode {mode!r} kind "
                        f"{record.get('kind')!r}: first release reconciles "
                        "lockstep lead/evaluator only")

    problem = _provenance_problem(prov)
    if problem:
        return _blocked("completion_unprovable_no_receipt", problem,
                        legacy_hold=not isinstance(prov, dict))
    assert isinstance(prov, dict)
    if not (prov.get("capabilities_at_dispatch") or {}).get(
        "completion_receipts"
    ):
        return _blocked("completion_unprovable_no_receipt",
                        "session was not opted in to completion receipts "
                        "at dispatch")
    sid = record["session_id"]
    if prov.get("session_id") != sid or prov.get("role") != role or (
        prov.get("iteration") != record.get("iteration")
    ):
        return _blocked("state_mismatch",
                        "provenance does not describe this held record")
    if prov.get("prior_phase") != PRIOR_PHASE[role]:
        return _blocked("state_mismatch",
                        f"dispatched from phase {prov.get('prior_phase')!r}, "
                        f"not {PRIOR_PHASE[role]!r}")

    # Rule 4: STATE still describes this hold.
    state = obs.get("state") or {}
    own_journal = obs.get("journal") or {}
    if str(state.get("status", "")).strip() != "needs_human" or str(
        state.get("phase", "")
    ).strip() != "needs_human":
        return _blocked("state_mismatch",
                        f"STATE is {state.get('status')!r}/{state.get('phase')!r},"
                        " not needs_human")
    if str(state.get("iteration", "")).strip() != str(record.get("iteration")):
        return _blocked("state_mismatch",
                        f"STATE iteration {state.get('iteration')!r} != held "
                        f"{record.get('iteration')!r}")
    if role == "evaluator":
        attempt = str(prov.get("attempt") or "")
        pinned = str(prov.get("pinned_sha") or "")
        if not attempt or not pinned:
            return _blocked("completion_unprovable_no_receipt",
                            "evaluator provenance has no attempt/pin")
        if str(state.get("evaluator_attempt", "")).strip() != attempt:
            return _blocked("state_mismatch", "STATE evaluator_attempt "
                            "differs from the dispatched attempt")
        if str(state.get("evaluated_sha", "")).strip() != pinned:
            return _blocked("state_mismatch", "STATE evaluated_sha differs "
                            "from the dispatched pin")

    # Artifact baseline must be intact (the copy is what freshness uses).
    art = obs.get("artifact") or {}
    if not art.get("baseline_copy_ok"):
        return _blocked("completion_unprovable_no_receipt",
                        "artifact baseline copy missing or its sha256 differs")

    # Broker observation.
    broker = obs.get("broker") or {}
    if not broker.get("observed"):
        return _waiting("broker_not_observed",
                        broker.get("error") or "broker not queried (offline)")
    if broker.get("gone"):
        return _blocked("completion_unprovable_no_receipt",
                        "session is gone (404): its receipt cannot be read; "
                        "a deleted pane is not a finished turn")
    if broker.get("error"):
        return _waiting("broker_unreachable", str(broker["error"]))
    snapshot = broker.get("snapshot")
    if not capabilities(snapshot)["completion_receipts"]:
        return _blocked("completion_unprovable_no_receipt",
                        "server does not advertise completion receipts for "
                        "this session")

    fence, fence_problem = parse_fence(snapshot)
    if fence_problem:
        return _blocked("completion_unprovable_no_receipt", fence_problem)
    if fence is not None and fence.get("fence_id") != own_journal.get(
        "fence_id"
    ):
        return _blocked("foreign_fence",
                        f"session has input fence {fence.get('fence_id')!r} "
                        "not opened by this reconciler")

    receipt, receipt_problem = parse_receipt(snapshot)
    if receipt_problem:
        return _blocked("completion_unprovable_no_receipt", receipt_problem)
    if receipt is None:
        if str(snapshot.get("status", "")).lower() == "running":
            return _waiting("session_running", "no receipt yet; running")
        return _waiting("turn_not_ended", "no turn-end receipt yet "
                        "(idle status is not completion)")

    # Contract R1: identity from the snapshot's own fields + core chat_id.
    chat = receipt.get("chat_id")
    if chat in (None, ""):
        return _blocked("completion_unprovable_no_receipt",
                        "receipt chat_id is null (store not bound)")
    if snapshot.get("runner_id") in (None, "") or snapshot.get(
        "runner_id"
    ) != prov.get("runner_id"):
        return _blocked("identity_mismatch", "session runner_id differs "
                        "from dispatch (relaunch or another writer)")
    if snapshot.get("external_session_id") != chat:
        return _blocked("identity_mismatch", "session external_session_id "
                        "differs from the receipt chat_id")
    if prov.get("external_session_id") not in (None, "") and chat != prov[
        "external_session_id"
    ]:
        return _blocked("identity_mismatch", "receipt chat_id differs from "
                        "dispatch external_session_id")
    if prov.get("terminal_epoch") is not None and receipt.get(
        "epoch"
    ) != prov.get("terminal_epoch"):
        return _blocked("identity_mismatch", "receipt epoch differs from "
                        "dispatch (terminal relaunched)")

    # Contract R2: every message in the epoch is one Trio sent, in order.
    detail = receipt["detail"]
    if detail["truncated"] is not False:
        return _blocked("completion_unprovable_no_receipt",
                        "receipt injections list is truncated")
    if obs.get("foreign_user_rows"):
        return _blocked("new_input_after_dispatch",
                        f"{obs['foreign_user_rows']} user row(s) that are "
                        "not our prompt")
    sent = list(prov["sent_prompt_shas"])
    count = receipt["inject_count"]
    if detail["msg_listed"] != count:
        return _blocked("completion_unprovable_no_receipt",
                        f"detail.msg_listed {detail['msg_listed']} != "
                        f"inject_count {count}")
    if count > len(sent):
        return _blocked("new_input_after_dispatch",
                        f"{count} message injections for {len(sent)} sent")
    if detail["msg_digest"] != message_digest(sent[:count]):
        return _blocked("new_input_after_dispatch",
                        "message injection digest is not the prompts Trio "
                        "sent")
    if count < len(sent):
        return _waiting("turn_not_ended",
                        f"{count} of {len(sent)} sent copies injected")

    # Contract R3: every message injection ended (message units only).
    if receipt["turn_end_count"] < count or receipt["inject_count_at_end"] < count:
        return _waiting("turn_not_ended",
                        f"turn_end_count {receipt['turn_end_count']} / "
                        f"inject_count_at_end {receipt['inject_count_at_end']}"
                        f" < inject_count {count}")
    if receipt["turn_end_count"] > count or receipt["inject_count_at_end"] > count:
        return _blocked("completion_unprovable_no_receipt",
                        "more turn-ends than injections (typed into pane?)")
    if receipt["turn_seq"] != receipt["turn_end_count"]:
        # The fence verifies `expect.turn_seq == expect.turn_end_count`.
        return _blocked("completion_unprovable_no_receipt",
                        "receipt turn_seq is not the latest turn end")

    # Contract R3b (rev3): every injected prompt's turn ended -- the runner
    # bound each message injection to its own user row and turn end.
    # Never fence an unbound receipt: the close would be `turns_unbound`,
    # which is not retryable and fences the session for good.
    binding, binding_problem = parse_binding(snapshot)
    if binding_problem:
        return _blocked("completion_unprovable_no_receipt",
                        f"R3b: {binding_problem}; hold, do not fence")
    assert binding is not None
    if binding["bound"] is not True:
        why = binding["reason"]
        if binding["bound"] is None:
            return _blocked("completion_unprovable_no_receipt",
                            f"R3b: receipt binding is null (reason {why!r}): "
                            "the runner predates contract rev3 turn "
                            "binding; hold, do not fence")
        return _blocked("turns_unbound",
                        f"R3b: receipt binding is unbound (reason {why!r}): "
                        "an injected prompt's turn end is not proven "
                        "(foreign, duplicate, queued or open turn); hold, "
                        "do not fence")
    if not (binding["user_rows"] == binding["closed_turns"] == count):
        return _blocked("turns_unbound",
                        f"R3b: binding user_rows {binding['user_rows']!r} / "
                        f"closed_turns {binding['closed_turns']!r} != "
                        f"inject_count {count}; hold, do not fence")

    # Contract R4: every observed row ACKed (necessary, never success).
    if not (
        detail["observed_max_acked"] is True
        and detail["unacked"] == 0
        and detail["acked_through"] >= detail["store_max"]
    ):
        return _waiting("transcript_not_flushed",
                        "observed transcript rows not all ACKed")
    if str(snapshot.get("status", "")).lower() == "running":
        return _waiting("session_running",
                        "turn ended but session reports running")

    # Contract R5: stop_reason / stop_status_raw are informational only
    # (recorded, never evidence of success or failure). Success is R7.
    evidence = {
        "receipt": receipt_key(receipt),
        "rev": receipt.get("rev"),
        "stop_reason": receipt.get("stop_reason"),
        "stop_raw": receipt["detail"].get("stop_raw"),
        "turn_binding": binding_key(binding),
        "binding": "recorded_epoch" if prov.get("terminal_epoch") is not None
        else "fresh_session",
    }

    # Rule 8: the artifact against the recorded baseline, attempt and pin.
    if not art.get("valid"):
        return _blocked("ended_without_valid_artifact",
                        art.get("problem") or "artifact not valid",
                        evidence=evidence)
    if role == "lead" and obs.get("product_problem"):
        return _blocked("ended_without_valid_artifact",
                        obs["product_problem"], evidence=evidence)
    return _decision(
        "ready", "late_valid_completion",
        [f"{role} turn ended (receipt) with a valid artifact"],
        evidence=evidence, continuation=dict(CONTINUATION[role]),
    )


# -- observation (read-only) --------------------------------------------

def read_state(mailbox: Path) -> dict[str, str]:
    text = _read_text(mailbox / "STATE.md") or ""
    state: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_]+)\s*:(.*)$", line)
        if match:
            state.setdefault(match.group(1).lower(), match.group(2).strip())
    return state


def read_holds(mailbox: Path) -> list[dict[str, Any]]:
    holds = []
    for path in sorted((mailbox / ".sessions").glob("held-*.json")):
        try:
            raw = path.read_bytes()
            record = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            raw, record = b"", None
        holds.append({
            "path": path,
            "sha256": _sha256_bytes(raw),
            "record": record if isinstance(record, dict) else None,
        })
    return holds


def journal_path(mailbox: Path, session_id: str) -> Path:
    return mailbox / ".sessions" / f"reconcile-{slug(session_id)}.json"


def read_journal(mailbox: Path, session_id: str) -> dict[str, Any] | None:
    text = _read_text(journal_path(mailbox, session_id))
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return {"schema": "unreadable"}
    return value if isinstance(value, dict) else {"schema": "unreadable"}


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "--no-optional-locks", *args],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _artifact(
    mailbox: Path, record: dict[str, Any], prov: dict[str, Any],
    artifact_ready: Callable[..., bool] | None,
) -> dict[str, Any]:
    role = record.get("role")
    baseline = prov.get("artifact_baseline") or {}
    copy_text = None
    copy_rel = baseline.get("copy")
    if copy_rel:
        copy_path = (mailbox / copy_rel).resolve()
        if mailbox.resolve() in copy_path.parents:
            copy_text = _read_text(copy_path)
    out: dict[str, Any] = {
        "path": ARTIFACT.get(str(role)),
        "baseline_copy_ok": copy_text is not None
        and sha256_text(copy_text) == baseline.get("sha256")
        and baseline.get("path") == ARTIFACT.get(str(role)),
    }
    if not out["baseline_copy_ok"] or role not in ARTIFACT:
        out.update(valid=False, problem="no usable baseline")
        return out
    current = _read_text(mailbox / ARTIFACT[role]) or ""
    out["current_sha256"] = sha256_text(current)
    iteration = record.get("iteration")
    if role == "lead":
        if not current.startswith(copy_text):
            out.update(valid=False, problem="LOG.md no longer starts with "
                       "the dispatch baseline")
            return out
        suffix = current[len(copy_text):]
        prefix = f"- iter {iteration} | lead |"
        if not any(line.startswith(prefix) for line in suffix.splitlines()):
            out.update(valid=False, problem=f"no new `{prefix}` line after "
                       "the dispatch baseline")
            return out
    context = {
        "evaluator_attempt": prov.get("attempt"),
        "pinned_sha": prov.get("pinned_sha"),
        "expected_sha": prov.get("pinned_sha"),
    } if role == "evaluator" else {}
    if artifact_ready is not None and not artifact_ready(
        mailbox, role, iteration, copy_text, 0.0, context
    ):
        out.update(valid=False, problem=f"{ARTIFACT[role]} is not this "
                   "attempt's valid artifact")
        return out
    out.update(valid=True, problem=None)
    return out


def _foreign_user_rows(items: Any, prompt: str | None, broker_http: Any) -> int:
    if broker_http is None or prompt is None:
        return 0
    try:
        rows = broker_http._user_row_texts(broker_http._session_item_rows(items))
        want = broker_http._prompt_text(prompt)
    except Exception:  # noqa: BLE001 - fail closed below
        return 1
    return sum(1 for row in rows if row != want)


def observe(
    mailbox: Path,
    *,
    repo: Path | None = None,
    client: Any = None,
    artifact_ready: Callable[..., bool] | None = None,
    broker_http: Any = None,
) -> dict[str, Any]:
    """Read-only snapshot of everything `decide` looks at.

    Only GET calls (`get_session`, `get_items`) and file/git reads. With
    ``client=None`` the broker is reported as not observed.
    """
    mailbox = Path(mailbox)
    holds = read_holds(mailbox)
    obs: dict[str, Any] = {
        "mailbox": str(mailbox),
        "holds": [
            {"path": str(h["path"]), "sha256": h["sha256"],
             "readable": h["record"] is not None}
            for h in holds
        ],
        "state": read_state(mailbox),
    }
    record = holds[0]["record"] if len(holds) == 1 else None
    obs["record"] = record
    obs["hold_sha256"] = holds[0]["sha256"] if len(holds) == 1 else None
    sid = record.get("session_id") if isinstance(record, dict) else None
    obs["journal"] = read_journal(mailbox, sid) if sid else None
    prov = record.get("provenance") if isinstance(record, dict) else None
    if isinstance(prov, dict) and isinstance(record, dict):
        obs["artifact"] = _artifact(mailbox, record, prov, artifact_ready)
        prompt_rel = prov.get("prompt_copy")
        prompt = _read_text(mailbox / prompt_rel) if prompt_rel else None
        if prompt is not None and sha256_text(prompt) != prov.get(
            "prompt_sha256"
        ):
            prompt = None
            obs["artifact"]["baseline_copy_ok"] = False
        if repo is not None and prov.get("product_head"):
            head = _git(repo, "rev-parse", "HEAD")
            obs["git_head"] = head
            if head is None:
                obs["product_problem"] = "git HEAD unreadable"
            elif head != prov["product_head"] and _git(
                repo, "merge-base", "--is-ancestor", prov["product_head"], head
            ) is None:
                obs["product_problem"] = (
                    "HEAD does not descend from the dispatch product_head"
                )
    else:
        prompt = None
    broker: dict[str, Any] = {"observed": False}
    if client is not None and sid:
        broker["observed"] = True
        try:
            snapshot = client.get_session(sid)
            broker["snapshot"] = snapshot
            broker["status"] = (
                snapshot.get("status") if isinstance(snapshot, dict) else None
            )
            if prompt is not None:
                obs["foreign_user_rows"] = _foreign_user_rows(
                    client.get_items(sid), prompt, broker_http
                )
        except Exception as exc:  # noqa: BLE001 - classify, never raise
            if getattr(exc, "status_code", None) == 404:
                broker["gone"] = True
            else:
                broker["error"] = f"{type(exc).__name__}: {exc}"
    obs["broker"] = broker
    return obs


def evidence(obs: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe summary of an observation (no file bodies)."""
    record = obs.get("record")
    prov = record.get("provenance") if isinstance(record, dict) else None
    broker = dict(obs.get("broker") or {})
    snapshot = broker.pop("snapshot", None)
    if isinstance(snapshot, dict):
        broker["runner_id"] = snapshot.get("runner_id")
        broker["capabilities"] = capabilities(snapshot)
        receipt, problem = parse_receipt(snapshot)
        broker["receipt"] = receipt_key(receipt) if receipt else None
        broker["receipt_problem"] = problem
        fence, _ = parse_fence(snapshot)
        broker["fence"] = fence
    return {
        "holds": obs.get("holds"),
        "state": obs.get("state"),
        "provenance_present": isinstance(prov, dict),
        "legacy_hold": isinstance(record, dict) and not isinstance(prov, dict),
        "record": {
            k: record.get(k) for k in (
                "session_id", "role", "iteration", "attempt", "pinned_sha",
                "hold", "recorded_at",
            )
        } if isinstance(record, dict) else None,
        "artifact": obs.get("artifact"),
        "product_problem": obs.get("product_problem"),
        "foreign_user_rows": obs.get("foreign_user_rows"),
        "journal": obs.get("journal"),
        "broker": broker,
    }


# -- apply (under the mailbox lock) -------------------------------------

def _state_text_after(text: str, updates: dict[str, str]) -> str:
    """STATE.md with owned keys replaced, every other line kept."""
    pending = dict(updates)
    out = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_]+)\s*:", line)
        key = match.group(1).lower() if match else None
        if key in pending:
            line = f"{key}: {pending.pop(key)}"
        out.append(line)
    out.extend(f"{k}: {v}" for k, v in pending.items())
    return "\n".join(out) + "\n"


def _rewrite_state(mailbox: Path, updates: dict[str, str]) -> None:
    """Replace owned keys, keep every other line; durable."""
    path = mailbox / "STATE.md"
    durable_write(path, _state_text_after(_read_text(path) or "", updates))


def _log_line(record: dict[str, Any], journal: dict[str, Any]) -> str:
    target = journal["target"]
    return (
        f"- iter {record.get('iteration')} | loop | reconciled held "
        f"{record.get('role')} session {record['session_id']}: late valid "
        f"completion (receipt epoch {journal['receipt']['epoch']} turn "
        f"{journal['receipt']['turn_seq']}, fence {journal['fence_id']}); "
        f"resuming phase {target['phase']}"
    )


def _log_text_after(text: str, line: str) -> str:
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


class ExternalChange(RuntimeError):
    """Mailbox files are neither the journaled before nor after state."""


def _transition_plan(mailbox: Path, record: dict[str, Any],
                     journal: dict[str, Any], repo: Path | None) -> dict:
    """Exact before/after hashes of every file the transition writes."""
    state = _read_text(mailbox / "STATE.md") or ""
    log = _read_text(mailbox / "LOG.md") or "# Trio loop log\n"
    artifact = _read_text(mailbox / ARTIFACT[record["role"]]) or ""
    return {
        "state_before": sha256_text(state),
        "state_after": sha256_text(_state_text_after(state, journal["target"])),
        "log_before": sha256_text(log),
        "log_after": sha256_text(_log_text_after(log, _log_line(record,
                                                                 journal))),
        "artifact": sha256_text(artifact),
        "head": _git(repo, "rev-parse", "HEAD") if repo is not None else None,
    }


def _transition_position(mailbox: Path, plan: dict) -> str:
    """"before", "after" (exactly our own writes) or "external"."""
    state = sha256_text(_read_text(mailbox / "STATE.md") or "")
    log = sha256_text(_read_text(mailbox / "LOG.md") or "# Trio loop log\n")
    if state == plan["state_before"] and log in (
        plan["log_before"], plan["log_after"]
    ):
        return "before"  # LOG is written first; STATE not yet
    if state == plan["state_after"] and log == plan["log_after"]:
        return "after"
    return "external"


def _check_transition(mailbox: Path, hold_path: Path,
                      journal: dict[str, Any]) -> None:
    """Raise ExternalChange unless the hold is still exactly the journaled
    record and LOG/STATE are a position of the plan; writes nothing."""
    try:
        hold = hold_path.read_bytes()
    except OSError:
        raise ExternalChange(f"{hold_path.name} was removed since the "
                             "journaled plan") from None
    if _sha256_bytes(hold) != journal.get("hold_sha256"):
        raise ExternalChange(f"{hold_path.name} changed since the "
                             "journaled plan")
    if _transition_position(mailbox, journal["plan"]) == "external":
        raise ExternalChange("STATE.md/LOG.md changed since the journaled "
                             "plan")


def _finish(mailbox: Path, hold_path: Path, record: dict[str, Any],
            journal: dict[str, Any]) -> None:
    """The tail: LOG, STATE, retire the hold, journal done.

    First checks the hold and both files (``_check_transition``); any
    external change raises ExternalChange before a single write, so the
    external bytes are kept. Then writes a file only when it is exactly
    the journaled ``plan`` before state (skips it when it is the after
    state).
    """
    plan = journal["plan"]
    _check_transition(mailbox, hold_path, journal)
    log_path, state_path = mailbox / "LOG.md", mailbox / "STATE.md"
    log = _read_text(log_path) or "# Trio loop log\n"
    if sha256_text(log) == plan["log_before"]:
        durable_write(log_path, _log_text_after(log, _log_line(record,
                                                               journal)))
    state = _read_text(state_path) or ""
    if sha256_text(state) == plan["state_before"]:
        durable_write(state_path, _state_text_after(state, journal["target"]))
    if hold_path.exists():
        resolved = dict(record)
        resolved["reconciled"] = {
            "fence_id": journal["fence_id"],
            "receipt": journal["receipt"],
            "continuation": journal["target"],
        }
        retired = hold_path.with_name(
            hold_path.name.replace("held-", "reconciled-", 1)
        )
        durable_write_json(retired, resolved)
        os.unlink(hold_path)
        _fsync_dir(hold_path.parent)
    journal = dict(journal, step="done")
    durable_write_json(journal_path(mailbox, record["session_id"]), journal)


def _gates(
    mailbox: Path, record: dict[str, Any], repo: Path | None, loop_core: Any,
) -> list[str]:
    iteration = int(record["iteration"])
    prov = record["provenance"]
    failures: list[str] = []
    if record["role"] == "lead":
        for ok, note in (
            loop_core.run_commit_gate(mailbox, repo),
            loop_core.run_log_gate(mailbox, iteration, "lead"),
        ):
            if not ok:
                failures.append(note)
    else:
        context = {
            "evaluator_attempt": prov.get("attempt"),
            "pinned_sha": prov.get("pinned_sha"),
            "expected_sha": prov.get("pinned_sha"),
        }
        if not loop_core._fresh_evaluator_artifact(mailbox, iteration, context):
            failures.append("VERDICT.md is not this attempt/pin's artifact")
        word, _scope = loop_core._first_verdict(mailbox / "VERDICT.md")
        if word is None:
            failures.append("VERDICT.md first line is unparseable")
    return failures


def apply_once(
    mailbox: Path,
    *,
    repo: Path | None,
    client: Any,
    loop_core: Any,
    artifact_ready: Callable[..., bool] | None = None,
    broker_http: Any = None,
    new_fence_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> dict[str, Any]:
    """Apply one ready decision under the mailbox lock; never dispatches.

    Returns a decision dict; ``action == "applied"`` only when the hold
    was retired and STATE names the continuation. Any other result keeps
    the hold (and any closed fence) in place.
    """
    mailbox = Path(mailbox).resolve()
    lock = loop_core._acquire_lock(mailbox)
    if lock is None:
        return _waiting("mailbox_locked", "another driver or reconciler "
                        "owns the mailbox lock")
    try:
        return _apply_locked(
            mailbox, repo, client, loop_core, artifact_ready, broker_http,
            new_fence_id,
        )
    finally:
        release = getattr(loop_core, "_release_lock", None)
        if release is not None:
            release(lock)
        else:  # an older loop core without owner tokens
            shutil.rmtree(lock, ignore_errors=True)


def _apply_locked(mailbox, repo, client, loop_core, artifact_ready,
                  broker_http, new_fence_id) -> dict[str, Any]:
    holds = read_holds(mailbox)
    # Idempotent replay/no-op via the journal, before any re-decision.
    journals = sorted((mailbox / ".sessions").glob("reconcile-*.json"))
    for jpath in journals:
        try:
            journal = json.loads(jpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _blocked("journal_unreadable", str(jpath))
        sid = journal.get("session_id")
        hold = next((h for h in holds if h["record"] and
                     h["record"].get("session_id") == sid), None)
        if journal.get("step") == "done":
            if hold is None:
                if not holds:
                    return _decision("applied", "already_applied",
                                     [f"{jpath.name} is done"],
                                     continuation=journal.get("target"))
                continue
            # A done journal but the hold is back: someone re-held it.
            return _blocked("journal_state_conflict",
                            f"{jpath.name} is done but {sid} is held again")
        if journal.get("step") == "applying":
            if hold is None:
                # The hold was already retired (STATE is written before
                # the rename): only the journal mark is missing. Never
                # rewrite STATE here -- the loop may have moved on.
                retired = mailbox / ".sessions" / f"reconciled-{slug(sid)}.json"
                if not retired.is_file():
                    return _blocked("journal_state_conflict",
                                    f"{jpath.name} is applying but neither "
                                    "the hold nor its retirement exists")
                durable_write_json(jpath, dict(journal, step="done"))
                continue
            if hold["sha256"] != journal.get("hold_sha256"):
                return _blocked("journal_state_conflict",
                                "held record changed during apply")
            return _replay_applying(
                mailbox, repo, client, loop_core, artifact_ready,
                broker_http, hold, journal,
            )

    if not holds:
        return _decision("none", "nothing_held", ["no held record"])
    obs = observe(mailbox, repo=repo, client=client,
                  artifact_ready=artifact_ready, broker_http=broker_http)
    record = obs.get("record")
    decision = decide(record, obs)
    if decision["action"] != "ready":
        return decision
    sid = record["session_id"]
    hold_path = Path(obs["holds"][0]["path"])
    receipt, _ = parse_receipt(obs["broker"]["snapshot"])
    key = receipt_key(receipt)
    journal = obs.get("journal") or {}
    if journal.get("step") in ("fence_requested", "fence_closed") and (
        journal.get("hold_sha256") != obs["hold_sha256"]
        or journal.get("receipt") != key
    ):
        return _blocked("journal_state_conflict",
                        "journaled fence was for different evidence")
    fence_id = journal.get("fence_id") or new_fence_id()
    journal = {
        "schema": JOURNAL_SCHEMA,
        "session_id": sid,
        "hold_sha256": obs["hold_sha256"],
        "record": record,
        "fence_id": fence_id,
        "receipt": key,
        "target": decision["continuation"],
        "step": "fence_requested",
    }
    jpath = journal_path(mailbox, sid)
    durable_write_json(jpath, journal)

    fence_call = getattr(client, "input_fence", None)
    if fence_call is None:
        return _blocked("completion_unprovable_no_receipt",
                        "broker client cannot close an input fence")
    try:
        response = fence_call(sid, "close", fence_id, expect=key)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        if status in (400, 403, 409):
            return _blocked("fence_refused", f"{type(exc).__name__}: {exc}")
        # Ambiguous: the journal keeps the fence_id; a retry is idempotent.
        return _waiting("fence_uncertain", f"{type(exc).__name__}: {exc}")
    fence = response.get("fence") if isinstance(response, dict) else None
    if not (
        isinstance(response, dict) and response.get("fenced") is True
        and isinstance(fence, dict) and fence.get("fence_id") == fence_id
    ):
        return _blocked("fence_unverified",
                        f"fence response {response!r} is not our fence")
    if not (fence.get("state") == "closed" and fence.get("verified") is True):
        # Contract R6: `closing`, or unverified for a transient reason, is
        # retried with the same fence_id and expect; anything else holds.
        if fence.get("state") == "closing" or (
            fence.get("verified") is False
            and fence.get("reason") in FENCE_RETRY_REASONS
        ):
            return _waiting("fence_retry",
                            f"fence {fence.get('state')!r} "
                            f"reason {fence.get('reason')!r}; retry later "
                            "with the same fence_id")
        if fence.get("reason") == "turns_unbound":
            return _blocked("fence_unverified",
                            "fence closed with turns_unbound (not "
                            "retryable): the runner could not bind every "
                            "injected prompt to its turn end; the session "
                            "stays fenced until a person opens it")
        return _blocked("fence_unverified",
                        f"fence {fence.get('state')!r} verified "
                        f"{fence.get('verified')!r} reason "
                        f"{fence.get('reason')!r}")
    ack_problem = _runner_ack_problem(fence, fence_id)
    if ack_problem:
        return _blocked("fence_unverified", ack_problem)
    journal["step"] = "fence_closed"
    durable_write_json(jpath, journal)

    # The transition's `before` is the mailbox that the re-decision below
    # validates, read ahead of it and ahead of the gates: an edit by a
    # person or pane from here on is external, never a new baseline.
    plan = _transition_plan(mailbox, record, journal, repo)
    # Re-observe after the fence: identical receipt, still ready.
    obs2 = observe(mailbox, repo=repo, client=client,
                   artifact_ready=artifact_ready, broker_http=broker_http)
    if obs2.get("hold_sha256") != obs["hold_sha256"]:
        return _blocked("journal_state_conflict", "held record changed")
    decision2 = decide(obs2.get("record"), obs2)
    if decision2["action"] != "ready":
        return decision2
    snap2 = obs2["broker"]["snapshot"]
    fence2, _ = parse_fence(snap2)
    receipt2, _ = parse_receipt(snap2)
    if not (
        isinstance(fence2, dict) and fence2.get("fence_id") == fence_id
        and fence2.get("state") == "closed" and fence2.get("verified") is True
    ):
        return _blocked("fence_unverified", "fence label not closed+verified "
                        "on re-read")
    if receipt_key(receipt2) != key or receipt2["rev"] < receipt["rev"] or (
        receipt2["detail"]["observed_max_acked"] is not True
    ):
        return _blocked("receipt_changed_after_fence",
                        f"{key} -> {receipt_key(receipt2)}")
    # Contract R6 (rev3): the re-read must still be bound, to the same turns.
    binding, _ = parse_binding(obs["broker"]["snapshot"])
    binding2, binding2_problem = parse_binding(snap2)
    if binding2_problem or binding_key(binding2) != binding_key(binding):
        return _blocked("receipt_changed_after_fence",
                        f"binding {binding_key(binding)} -> "
                        f"{binding2_problem or binding_key(binding2)}")

    failures = _gates(mailbox, record, repo, loop_core)
    if failures:
        return _blocked("gate_failed", *failures)

    journal["plan"] = plan
    try:
        _check_transition(mailbox, hold_path, journal)
    except ExternalChange as exc:
        return _blocked("journal_state_conflict", str(exc))
    journal["step"] = "applying"
    durable_write_json(jpath, journal)
    try:
        _finish(mailbox, hold_path, record, journal)
    except ExternalChange as exc:
        return _blocked("journal_state_conflict", str(exc))
    return _decision("applied", "late_valid_completion",
                     decision2["reasons"], continuation=journal["target"],
                     evidence=decision2.get("evidence"))


def _runner_ack_problem(fence: dict[str, Any], fence_id: str) -> str | None:
    """Contract §3 step 4 (rev3): a verified close rests on the runner's
    `bound` acknowledgment. A first close carries ``runner_ack``; it must
    be ours and bound. The idempotent repeat of an already verified close
    returns the stored label without ``runner_ack``; that is accepted only
    because R3b has already required the rev3 binding label, i.e. a rev3
    server, whose `verified:true` includes `runner.bound == true`."""
    if "runner_ack" not in fence:
        return None
    ack = fence["runner_ack"]
    if not isinstance(ack, dict):
        return f"fence runner_ack {ack!r} is not an object"
    if ack.get("fence_id") != fence_id or ack.get("same_fence") is not True:
        return "fence runner_ack is not for this fence"
    if ack.get("bound") is not True:
        return (f"fence verified but runner_ack.bound is {ack.get('bound')!r}"
                f" (bind_reason {ack.get('bind_reason')!r}): the runner did "
                "not acknowledge turn binding; hold")
    return None


def _replay_applying(mailbox, repo, client, loop_core, artifact_ready,
                     broker_http, hold, journal) -> dict[str, Any]:
    """Finish an interrupted `applying` only after revalidating it.

    Recognizes exactly two positions: nothing committed yet ("before":
    full re-decision under the lock with the journaled fence and receipt,
    then the gates) or exactly this reconciler's own partial writes
    ("after": artifact and HEAD unchanged, gates re-run). Any other
    mailbox state is an external change (a person, the pane, another
    tool) and is preserved: the hold and journal stay for a person.
    """
    record = hold["record"]
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        return _blocked("journal_state_conflict",
                        "applying journal has no transition plan")
    position = _transition_position(mailbox, plan)
    if position == "external":
        return _blocked("journal_state_conflict",
                        "STATE.md/LOG.md changed after the interrupted "
                        "apply; preserved for a person")
    artifact = _read_text(mailbox / ARTIFACT[record["role"]]) or ""
    if record["role"] != "lead" and sha256_text(artifact) != plan["artifact"]:
        return _blocked("journal_state_conflict",
                        f"{ARTIFACT[record['role']]} changed after the "
                        "interrupted apply")
    if repo is not None and plan.get("head") is not None and _git(
        repo, "rev-parse", "HEAD"
    ) != plan["head"]:
        return _blocked("journal_state_conflict",
                        "product HEAD changed after the interrupted apply")
    if position == "before":
        obs = observe(mailbox, repo=repo, client=client,
                      artifact_ready=artifact_ready, broker_http=broker_http)
        if obs.get("hold_sha256") != journal.get("hold_sha256"):
            return _blocked("journal_state_conflict", "held record changed")
        decision = decide(obs.get("record"), obs)
        if decision["action"] != "ready":
            return decision
        snapshot = obs["broker"]["snapshot"]
        fence, _ = parse_fence(snapshot)
        receipt, _ = parse_receipt(snapshot)
        if not (
            isinstance(fence, dict)
            and fence.get("fence_id") == journal.get("fence_id")
            and fence.get("state") == "closed"
            and fence.get("verified") is True
        ):
            return _blocked("fence_unverified",
                            "journaled fence is not closed+verified")
        if receipt_key(receipt) != journal.get("receipt"):
            return _blocked("receipt_changed_after_fence",
                            f"{journal.get('receipt')} -> "
                            f"{receipt_key(receipt)}")
    failures = _gates(mailbox, record, repo, loop_core)
    if failures:
        return _blocked("gate_failed", *failures)
    try:
        _finish(mailbox, Path(hold["path"]), record, journal)
    except ExternalChange as exc:
        return _blocked("journal_state_conflict", str(exc))
    return _decision("applied", "resumed_interrupted_apply",
                     [f"revalidated interrupted apply ({position})"],
                     continuation=journal["target"])

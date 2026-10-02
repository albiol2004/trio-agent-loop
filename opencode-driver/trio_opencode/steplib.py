"""Adapter over ``native/trio_native_step.py``'s loop-core ops for the
OpenCode driver.

``native/trio_native_step.py`` is a stdlib helper whose ``op_*`` functions
call into ``metrics/trio_loop.py`` (``TL``) for the actual Trio loop
semantics (gates, verdict parsing, the repair counter, the evaluator
pin/attempt, SHIP retirement) with the same ``STATE.md`` ``phase`` resume
cursor as every other driver, so a mailbox this driver runs can be resumed
by ``trio_loop.py run`` or ``native/trio-native.js`` and vice versa.

This module loads that file as a **private** module copy (a unique
``sys.modules`` name — never ``"trio_native_step"``, so it never collides
with a real native-driver load in the same process) and overrides its
module-level globals so it acts as *our* driver: its own runtime filenames
(``RECORDS``, ``RESULT``, the worktrees/ledger directories, the lock owner
prefix) become ``opencode``'s, never the native Workflow driver's, even
though both point at the same ``metrics/trio_loop.py`` core and the same
mailbox protocol.

Lock/_owner note (documented per the shared spec)
--------------------------------------------------
``trio_native_step._acquire`` has one hard-coded prefix check: when a
*different* token already holds the lock, a lock whose *existing* owner
string starts with ``"workflow:"`` gets the permissive stale-heartbeat
takeover rule (``age > stale_seconds`` makes it stale even while the pid is
alive); any other owner prefix falls back to the strict pid-liveness-only
rule. We override :func:`_owner` to return ``"opencode:<token>"`` instead of
``"workflow:<token>"``. That check only inspects the *lock file's current
owner string* (whatever a mailbox's ``.lock/owner`` currently says), not our
own token or driver name, so overriding ``_owner`` cannot break it:

* Same-token re-entry (``owner == _owner(token)``) never consults the
  prefix at all — dead-pid takeover is unconditional either way.
* A lock a *native* Workflow run still holds (``owner`` literally
  ``"workflow:..."``) keeps getting the heartbeat-based grace period when an
  opencode run tries to take it over — unchanged, because that branch reads
  the lock file's owner, not ours.
* A lock an *opencode* run holds (``owner`` now ``"opencode:..."``) does
  **not** get that heartbeat grace when a different token tries to take it
  over — only pid-liveness matters, the same strict rule ``trio_loop`` /
  ``trioctl`` apply to every non-``workflow:`` owner. This is a strictly
  more conservative behaviour than the native driver's own locks get, never
  a looser one, so nothing about the mailbox lock contract is weakened.

Nothing else in ``_acquire``/``_require_lock``/``_release`` reads the prefix,
so overriding ``_owner`` alone is sufficient; no other function needed
adapting.
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------- location
_THIS_FILE = Path(__file__).resolve()
#: <repo>/opencode-driver/trio_opencode/steplib.py -> repo root is two
#: parents up (opencode-driver/, then its parent).
REPO_ROOT = _THIS_FILE.parents[2]
OPENCODE_DRIVER_ROOT = _THIS_FILE.parents[1]


def _helper_path() -> Path:
    override = os.environ.get("TRIO_OPENCODE_NATIVE_HELPER", "").strip()
    if override:
        return Path(override).resolve()
    return (REPO_ROOT / "native" / "trio_native_step.py").resolve()


def _load_native_step():
    """Load ``trio_native_step.py`` as a private module copy.

    A fresh, randomly-suffixed module name every import of *this* module —
    never ``"trio_native_step"`` — so a real native-driver load elsewhere in
    the same process is never shadowed or shadows us.
    """
    # The helper derives the lock's *holder pid* from this env var when set
    # (native/trio_native_step.py:_holder_pid); we are the long-lived
    # process running the loop, so every op must record OUR pid, never a
    # `claude`/opencode-cli ancestor's.
    os.environ["TRIO_NATIVE_HOLDER_PID"] = str(os.getpid())
    path = _helper_path()
    module_name = f"trio_opencode_native_step_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio_native_step.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


NS = _load_native_step()
#: The native step module's own loaded ``metrics/trio_loop.py`` core.
TL = NS.TL

# ------------------------------------------------------- global overrides
_BASE_RUNTIME_IGNORES = (
    ".dispatch/", ".driver.pid", ".session.json",
    ".sessions/", "driver.log", ".lock", ".repairs",
)

NS.DRIVER = "opencode"
NS.RECORDS = ".opencode.json"
NS.RESULT = ".opencode-result.json"
NS.LEDGER_DIR = "trio-opencode"
NS.WORKTREES_DIR = ".trio-opencode/worktrees"
NS.EXCLUDE_LINE = ".trio-opencode/worktrees/"
NS.EXCLUDE_HEADER = "# trio-opencode: loop worktrees and build artefacts"
NS.MAILBOX_RUNTIME_IGNORES = tuple(dict.fromkeys(
    _BASE_RUNTIME_IGNORES + (".driver.json", NS.RECORDS, NS.RESULT, ".opencode-runs/")
))


def _owner_opencode(token: str) -> str:
    return f"opencode:{token}"


NS._owner = _owner_opencode


def _dangling_worktrees_opencode(repo):  # noqa: ANN001 - repo: Path | None
    """``native/trio_native_step.py``'s own ``_dangling_worktrees`` hard-codes
    the literal ``.claude/worktrees/`` instead of reading its own
    ``WORKTREES_DIR`` global the way ``_worktrees_dir_entries``/
    ``_scratch_left`` do — so with ``NS.WORKTREES_DIR`` overridden to
    ``.trio-opencode/worktrees`` above, the unpatched function would never
    report an opencode builder worktree as dangling in ``end``'s result.
    Same git-porcelain logic, our own ``WORKTREES_DIR`` marker."""
    if repo is None:
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    marker = f"{repo}/{NS.WORKTREES_DIR}/"
    return sorted(
        line[len("worktree "):] for line in result.stdout.splitlines()
        if line.startswith("worktree ") and line[9:].startswith(marker)
    )


NS._dangling_worktrees = _dangling_worktrees_opencode


def _register_noop(mailbox, *, replace: bool = False, own_token=None, **fields) -> None:  # noqa: ANN001
    """The opencode driver writes its own registry record (driver.py); the
    native helper's own ``_register`` (a different schema, a different
    runs dir default) must never run."""
    return None


NS._register = _register_noop


def _runs_dir_override() -> Path:
    value = os.environ.get("TRIO_OPENCODE_RUNS_DIR", "").strip()
    if value:
        return Path(value).expanduser()
    return Path.home() / ".local" / "share" / "trio-agent-loop" / "opencode-runs"


NS._runs_dir = _runs_dir_override

# Never lazily load omnigent/worker_worktrees.py through trio_loop's residue
# check (the hard standalone constraint: no omnigent/ import, ever).
TL.owned_residue_check = lambda repo, rel: False

StepError = NS.StepError

#: Keys an ``ok: true`` result must carry, per op (copied verbatim from
#: ``native/trio-native.js``'s ``REQUIRED``).
REQUIRED: dict[str, list[str]] = {
    "begin": ["repo", "iteration", "status", "phase", "lock_owner", "exec_id"],
    "next": ["action", "iteration"],
    "dispatch": ["head", "wave"],
    "builders": ["accepted", "refused", "merge"],
    "cleanup": ["removed", "kept", "dropped"],
    "gate": ["pass", "failures", "final", "status"],
    "pin": ["sha", "evaluator_attempt", "context_block", "skip_evaluator"],
    "apply": ["verdict", "stop", "status", "code", "commit_shas", "bound"],
    "end": ["lock", "dangling_worktrees"],
}


class StepValidationError(RuntimeError):
    """An op's own result did not carry the keys :data:`REQUIRED` promises
    (an internal contract check — steplib calls the op function directly, so
    there is no wire format to lose keys in; a violation is a bug here or in
    the loaded helper, never a transient condition to retry)."""


def _validate(op: str, result: dict) -> None:
    if result.get("ok"):
        if result.get("pending") is True:
            # Bug fix (e2e, r19 acceptance): a detached job
            # (acceptance-freeze/-run/apply) still running legitimately
            # answers `{"ok": True, "pending": True}` with none of the op's
            # normal REQUIRED keys yet (they arrive once the job resolves).
            # This was ported from native/trio-native.js's own `validate()`
            # (`res.pending === true ? [] : (REQUIRED[op] || [])`) but the
            # exemption itself was dropped, so every real pending result
            # failed this check as if it were malformed.
            return
        missing = [k for k in REQUIRED.get(op, []) if k not in result]
        if missing:
            raise StepValidationError(f"{op}: result lacks {missing}")
    elif "error" not in result:
        raise StepValidationError(f"{op}: a failed result must carry 'error'")


# ------------------------------------------------------------------ nonce
_SEQ = itertools.count(1)


def _auto_nonce(mailbox: Path, token: str, op: str) -> str:
    seq = next(_SEQ)
    exec_id = ""
    try:
        exec_id = NS._current_exec_id(Path(mailbox))
    except Exception:  # noqa: BLE001 - nonce generation never raises
        exec_id = ""
    return f"{token}/{exec_id}/{seq}/{op}" if exec_id else f"{token}/{seq}/{op}"


# -------------------------------------------------------------- namespace
def _namespace(op: str, mailbox: str | Path, token: str, nonce: str, *,
               repo: str | Path | None = None, max_iterations: int = 4,
               iteration: int = 0, role: str = "lead", attempt: Any = "1",
               wave: int = 1, head: str | None = None,
               results: str | None = None, branches: str | None = None,
               drop_unmerged: str | None = None,
               # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): mirrors
               # native/trio_native_step.py's `--acceptance`/`--models`/
               # `--acc`/`--marker`/`--prior`/`--author`/`--model`/`--plan`/
               # `--sha`/`--poll` -- every op ignores them when `acceptance`
               # is 0/False (the switch-off identity).
               acceptance: int = 0, models: str | None = None,
               acc: str | None = None, marker: str | None = None,
               prior: str | None = None, author: str | None = None,
               model: str | None = None, plan: str | None = None,
               sha: str | None = None, poll: int = 0) -> argparse.Namespace:
    """Build the same :class:`argparse.Namespace` ``trio_native_step._parser()``
    would, from keyword values instead of argv strings."""
    return argparse.Namespace(
        op=op, mailbox=str(mailbox), token=token, nonce=nonce,
        repo=str(repo) if repo is not None else None,
        max_iterations=int(max_iterations), iteration=int(iteration),
        role=role, attempt=attempt, wave=int(wave), head=head,
        results=results, branches=branches, drop_unmerged=drop_unmerged,
        acceptance=int(acceptance), models=models, acc=acc, marker=marker,
        prior=prior, author=author, model=model, plan=plan, sha=sha,
        poll=int(poll),
        json=True,
    )


def _run(op: str, mailbox: str | Path, repo: str | Path | None, token: str,
          nonce: str | None, **extra: Any) -> dict:
    """Replicate ``trio_native_step.main()``'s body for one op call: build
    the namespace, resolve the mailbox/repo, call the handler, apply the
    same ``next``/``pin`` human-answer extras, and turn a :class:`StepError`
    (or any other exception) into ``{ok: False, error}`` exactly as the CLI
    would print it — but return the dict instead of printing it."""
    mailbox_path = Path(mailbox).resolve()
    if nonce is None:
        nonce = _auto_nonce(mailbox_path, token, op)
    a = _namespace(op, mailbox_path, token, nonce, repo=repo, **extra)
    out: dict = {"op": op, "nonce": nonce, "mailbox": str(mailbox_path),
                 "api": NS.NATIVE_STEP_API}
    try:
        if not mailbox_path.is_dir():
            raise StepError(f"mailbox {mailbox_path} is not a directory")
        if op == "gate":
            a.attempt = int(a.attempt)
        resolved_repo = NS._repo_for(mailbox_path, a.repo)
        body = NS.HANDLERS[op](mailbox_path, resolved_repo, a)
        if op == "next" and body.get("action") == "lead":
            body.update(NS._human_answer(mailbox_path, int(body.get("iteration") or 0)))
        if (op == "next" and NS._acc_on(a)
                and body.get("action") in ("lead", "repair")):
            # Mirrors trio_native_step.main()'s own acceptance branch for
            # `next` (steplib calls HANDLERS directly rather than main(), so
            # it must replicate this one extra step itself): the pin/tamper
            # check that runs before every Lead/repair pass.
            body["acceptance"] = NS._acc_next(mailbox_path, resolved_repo, a, body)
        if op == "pin":
            key = NS._retry_key(mailbox_path, token, str(nonce or ""), a.iteration)
            body.update(NS._human_answer(mailbox_path, a.iteration, consume=True, key=key))
        out.update(body)
        out["ok"] = True
    except StepError as exc:
        out.update(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - reported, never a traceback
        out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    out["nonce"] = nonce
    out["op"] = op
    _validate(op, out)
    return out


# ---------------------------------------------------------------- wrappers
#: Forward-referenced below `acc_flags()` is defined (module order keeps the
#: non-acceptance wrappers first, as in the shared spec's file table); Python
#: resolves the name at call time, so the forward reference is fine.
def begin(mailbox: str | Path, repo: str | Path | None, token: str, *,
          nonce: str | None = None, acceptance: bool = False,
          models: dict | None = None) -> dict:
    extra = acc_flags(acceptance)
    if acceptance and models:
        extra["models"] = json.dumps(models, sort_keys=True)
    return _run("begin", mailbox, repo, token, nonce, **extra)


def next_(mailbox: str | Path, repo: str | Path | None, token: str, *,
          max_iterations: int = 4, nonce: str | None = None,
          acceptance: bool = False, acc: dict | None = None) -> dict:
    return _run("next", mailbox, repo, token, nonce,
                max_iterations=max_iterations, **acc_flags(acceptance, acc))


def dispatch(mailbox: str | Path, repo: str | Path | None, token: str, *,
             iteration: int, wave: int = 1, nonce: str | None = None,
             acceptance: bool = False, acc: dict | None = None) -> dict:
    return _run("dispatch", mailbox, repo, token, nonce,
                iteration=iteration, wave=wave, **acc_flags(acceptance, acc))


def builders(mailbox: str | Path, repo: str | Path | None, token: str, *,
             iteration: int, wave: int, head: str, results: str,
             attempt: Any = 1, nonce: str | None = None) -> dict:
    """Not in the shared spec's explicit wrapper list, but exposed here:
    ``driver.py`` needs it to verify a wave's builder results and write their
    LOG lines (op_builders), same as the plan/integrate/cleanup ops it does
    list."""
    return _run("builders", mailbox, repo, token, nonce,
                iteration=iteration, wave=wave, head=head, results=results,
                attempt=str(attempt))


def cleanup(mailbox: str | Path, repo: str | Path | None, token: str, *,
            branches: str, drop_unmerged: str | None = None,
            nonce: str | None = None) -> dict:
    return _run("cleanup", mailbox, repo, token, nonce,
                branches=branches, drop_unmerged=drop_unmerged)


def gate(mailbox: str | Path, repo: str | Path | None, token: str, *,
         role: str, iteration: int, attempt: int,
         nonce: str | None = None, acceptance: bool = False,
         acc: dict | None = None) -> dict:
    return _run("gate", mailbox, repo, token, nonce,
                role=role, iteration=iteration, attempt=attempt,
                **acc_flags(acceptance, acc))


def pin(mailbox: str | Path, repo: str | Path | None, token: str, *,
        iteration: int, nonce: str | None = None) -> dict:
    return _run("pin", mailbox, repo, token, nonce, iteration=iteration)


def apply(mailbox: str | Path, repo: str | Path | None, token: str, *,
          iteration: int, attempt: Any, nonce: str | None = None,
          acceptance: bool = False, acc: dict | None = None,
          poll: int = 0) -> dict:
    return _run("apply", mailbox, repo, token, nonce,
                iteration=iteration, attempt=str(attempt), poll=poll,
                **acc_flags(acceptance, acc))


def end(mailbox: str | Path, repo: str | Path | None, token: str, *,
        nonce: str | None = None, acceptance: bool = False,
        acc: dict | None = None) -> dict:
    return _run("end", mailbox, repo, token, nonce, **acc_flags(acceptance, acc))


# --------------------------------------------------------- frozen acceptance
# r19 (docs/FROZEN-ACCEPTANCE.md). Every function here is a no-op path when
# the caller never sets `acceptance=1` in `acc_flags`/kwargs -- the switch-off
# identity holds because `_namespace()`'s acceptance default is 0 and every
# `_acc_*` helper in the loaded `trio_native_step.py` checks `_acc_on(a)`
# before doing anything.

#: ``ACC_KEYS`` (native/trio-native.js): the digest fields the script holds
#: between helper processes and hands back on every acceptance op.
ACC_KEYS = ("status", "pin", "pin_commit", "freeze_commit", "base",
           "tamper_events", "amendments", "seq")


def acc_flags(enabled: bool, acc: dict | None = None) -> dict:
    """``accFlags()`` (native/trio-native.js): the kwargs every acceptance-
    aware op call adds once the switch is on -- ``{}`` (never ``acceptance``
    at all) with the switch off, so the switch-off identity holds exactly;
    ``{"acceptance": 1}`` plus the script's held digest (``ACC_KEYS``) as
    ``acc`` JSON once there is one (so the helper can detect a disagreeing
    sealed record/state file/pin chain at every op). ``acc`` is ``None``
    until the first op with a digest (``begin``) returns one."""
    if not enabled:
        return {}
    flags: dict[str, Any] = {"acceptance": 1}
    if acc:
        held = {k: acc.get(k) for k in ACC_KEYS}
        flags["acc"] = json.dumps(held, sort_keys=True)
    return flags


def acc_take(prev: dict | None, result: dict, op: str) -> dict | None:
    """``accTake()`` (native/trio-native.js): adopt ``result["acceptance"]``
    as the script's new held digest, with the same one-way "never un-freeze"
    rule (frozen by any op other than ``begin``/``acceptance-freeze`` is
    rejected; already-frozen state going back to unfrozen is rejected)."""
    d = result.get("acceptance") if isinstance(result, dict) else None
    if not isinstance(d, dict) or d.get("stop") or not isinstance(d.get("status"), str):
        return prev
    frozen = d.get("status") == "frozen" and bool(d.get("pin"))
    was_frozen = bool(prev and prev.get("status") == "frozen" and prev.get("pin"))
    if not was_frozen and frozen and op not in ("begin", "acceptance-freeze"):
        return prev
    if was_frozen and not frozen:
        return prev
    return d


def acc_frozen(acc: dict | None) -> bool:
    """``accFrozen()`` (native/trio-native.js): is the script's held digest a
    frozen pack (status + pin both present)?"""
    return bool(acc and acc.get("status") == "frozen" and acc.get("pin"))


def acc_stop(result: dict) -> dict | None:
    """``accStop()`` (native/trio-native.js): the helper already stopped the
    loop (NEEDS_HUMAN/error) over an acceptance tamper/mismatch; the caller
    must unwind to ``end`` with this outcome instead of continuing."""
    acceptance = result.get("acceptance") if isinstance(result, dict) else None
    stop = acceptance.get("stop") if isinstance(acceptance, dict) else None
    if not stop:
        return None
    detail = str(stop.get("detail") or "")[:400]
    return {"status": stop.get("status") or "error",
           "code": stop.get("code") if stop.get("code") is not None else None,
           "reason": f"acceptance {stop.get('reason')}: {detail}"}


#: Long acceptance ops run as detached helper jobs and answer `pending`
#: while the job is still running; `step_long` polls the same op with
#: `poll=N` (native's `stepLong`/`MAX_POLLS`). `sleep` is injectable so
#: tests never actually wait.
MAX_POLLS = 12


def step_long(fn: Callable[..., dict], *args: Any, sleep: Callable[[float], None] = time.sleep,
             max_polls: int = MAX_POLLS, poll_interval_s: float = 1.0, **kwargs: Any) -> dict:
    """Poll ``fn`` (an op function, e.g. :func:`apply`/:func:`acceptance_run`/
    :func:`acceptance_freeze`) while it answers ``{"ok": True, "pending":
    True}``, exactly as native's ``stepLong`` polls the equivalent helper op.

    Bug 3: when ``max_polls`` is exhausted with the op still pending, native
    gives up with ``{ok: false, op, error: "<op> still running after N
    polls"}`` (``trio-native.js``'s ``stepLong``) rather than handing the
    caller the last (still ``pending: True``) result — a caller indexing a
    run-ending result's ``stop``/``status`` keys on that would ``KeyError``
    instead of cleanly stopping the run. Mirrors that here."""
    r = fn(*args, **kwargs)
    poll = 0
    while r.get("ok") and r.get("pending") is True and poll < max_polls:
        poll += 1
        sleep(poll_interval_s)
        r = fn(*args, poll=poll, **kwargs)
    if r.get("ok") and r.get("pending") is True:
        op = fn.__name__.replace("_", "-")
        return {"ok": False, "op": op, "error": f"{op} still running after {max_polls} polls"}
    return r


def acceptance_export(mailbox: str | Path, repo: str | Path | None, token: str, *,
                      iteration: int, attempt: Any = 1, acc: dict | None = None,
                      nonce: str | None = None) -> dict:
    return _run("acceptance-export", mailbox, repo, token, nonce,
               iteration=iteration, attempt=str(attempt), **acc_flags(True, acc))


def acceptance_freeze(mailbox: str | Path, repo: str | Path | None, token: str, *,
                      iteration: int, attempt: Any, marker: str, prior: dict,
                      author: dict, model: str | None, acc: dict | None = None,
                      poll: int = 0, nonce: str | None = None) -> dict:
    return _run("acceptance-freeze", mailbox, repo, token, nonce,
               iteration=iteration, attempt=str(attempt), marker=marker,
               prior=json.dumps(prior), author=json.dumps(author), model=model,
               poll=poll, **acc_flags(True, acc))


def coverage(mailbox: str | Path, repo: str | Path | None, token: str, *,
            iteration: int, attempt: Any, plan: dict, acc: dict | None = None,
            nonce: str | None = None) -> dict:
    return _run("coverage", mailbox, repo, token, nonce,
               iteration=iteration, attempt=str(attempt), plan=json.dumps(plan),
               **acc_flags(True, acc))


def acceptance_run(mailbox: str | Path, repo: str | Path | None, token: str, *,
                   iteration: int, sha: str, acc: dict | None = None,
                   poll: int = 0, nonce: str | None = None) -> dict:
    return _run("acceptance-run", mailbox, repo, token, nonce,
               iteration=iteration, sha=sha, poll=poll, **acc_flags(True, acc))


# ---------------------------------------------------- author audit override
#: Set once per run by driver.py before the author phase: the directory
#: holding one JSONL file per author attempt marker (the OpenCode author's
#: tool-call inputs, parsed from its turn's own NDJSON event log -- see
#: driver.py's `_persist_author_tool_calls`). `None` (the default, and every
#: test that never sets it) falls through to the helper's own "no transcript
#: found" limited audit, unchanged.
AUTHOR_TOOLCALLS_DIR: Path | None = None


def _author_tool_entries(marker: str) -> list | None:
    if AUTHOR_TOOLCALLS_DIR is None:
        return None
    path = Path(AUTHOR_TOOLCALLS_DIR) / f"{marker}.jsonl"
    if not path.is_file():
        return None
    entries: list = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _opencode_author_audit(ctl, export: Path, marker: str, since: float) -> dict:  # noqa: ANN001
    """Replaces ``trio_native_step._native_author_audit`` for this driver
    (req. 4): the native helper looks for a Claude Code subagent transcript,
    which never exists for an OpenCode turn. Instead, the OpenCode author's
    own tool-call inputs (persisted by driver.py per attempt, keyed by the
    same ``{marker}-a{attempt}`` the helper already uses) are converted into
    the row shape ``metrics/trio-acceptance.py::_audit_inputs`` understands
    and audited with ``cwd=export`` (an OpenCode tool call's relative paths
    resolve from the turn's own process cwd, which IS the export -- unlike
    native, which passes ``cwd=ctl.repo``). No record found (an older run
    before this override existed, or a turn whose log could not be read)
    falls back to the helper's own limited audit."""
    entries = _author_tool_entries(marker)
    if not entries:
        audit = ctl._audit(export, {"transcript": None})
        audit["transcripts"] = []
        audit["note"] = ("limited: no OpenCode author tool-call record found for "
                         f"marker {marker}")
        audit["path"] = "opencode"
        return audit
    audit = ctl.ta.audit_transcript(entries, export, forbidden=ctl._audit_forbidden(),
                                    cwd=export)
    audit["limited"] = False
    audit["transcripts"] = [str(Path(AUTHOR_TOOLCALLS_DIR) / f"{marker}.jsonl")]
    audit["path"] = "opencode"
    return audit


NS._native_author_audit = _opencode_author_audit


# ------------------------------------------------------ ledger passthrough
def ledger_append(mailbox: str | Path, repo: str | Path | None,
                   entry: dict) -> bool:
    return NS._ledger_append(Path(mailbox), Path(repo) if repo else None, entry)


def ledger_path(mailbox: str | Path, repo: str | Path | None) -> Path | None:
    return NS._ledger_path(Path(mailbox), Path(repo) if repo else None)


def ensure_exclude(repo: str | Path | None) -> str | None:
    return NS._ensure_exclude(Path(repo) if repo else None)


# --------------------------------------------------------------- guardian
def assert_no_omnigent_loaded(repo: str | Path) -> None:
    """Raise if any ``sys.modules`` entry's ``__file__`` lives under
    ``<repo>/omnigent/`` — the hard standalone constraint: this driver must
    never load the Omnigent broker, trioctl or registry, even transitively
    through ``metrics/trio_loop.py``'s residue check."""
    omnigent_dir = (Path(repo).resolve() / "omnigent")
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            path = Path(file).resolve()
        except OSError:
            continue
        if path == omnigent_dir or omnigent_dir in path.parents:
            raise AssertionError(
                f"omnigent module loaded: {name} ({path}); trio-opencode "
                "must never import anything under omnigent/"
            )

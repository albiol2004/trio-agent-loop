#!/usr/bin/env python3
"""loop_actions.py — trio-dash loop states, unblock table, fix allowlist,
answer box and read-only diagnosis (dash-actions).

Stdlib only; loaded by path from ``dashboard/serve.py``. Nothing here trusts
a request or an agent: every fix is one of ``FIXES``, every precondition is
re-checked on the server from the mailbox, lock, session and git state at
the moment of the call, and anything destructive or history-changing needs
an explicit ``confirm`` from the caller after it was shown the exact
commands. Diagnosis agents (Cursor ``cursor-agent`` in ask mode, or Codex
``codex exec`` in the read-only sandbox) only *propose* a fix id.

Files (outside every workspace unless noted):

- ``$TRIO_DASH_STATE_DIR`` (default ``~/.local/state/trio-dash``)
  ``/loops/<key>/``: ``actions.jsonl`` (append-only action log),
  ``diagnosis.json`` (latest diagnosis), ``diagnosis-<id>.out`` (raw agent
  output), ``runs/<ts>-<fix>.log`` (output of started drivers). ``key`` is
  the first 16 hex of sha256(realpath of the ROOT mailbox).
- ``<live mailbox>/HUMAN.md`` — the answer box (documented in
  MAILBOX-SCHEMA.md "HUMAN.md"): append-only timestamped entries the Lead
  reads at the start of its next pass.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOOP_ACTIONS_API = 1

# --------------------------------------------------------------------------
# Configuration (environment first, then the installed-release defaults)
# --------------------------------------------------------------------------

DEFAULT_CURSOR_MODEL = "cursor-grok-4.6-low"
"""Grok 4.6 Low as listed by ``cursor-agent --list-models``."""
DEFAULT_CODEX_MODEL = "gpt-6-luna"
"""GPT 6 Luna as listed in codex's model catalog (``models_cache.json``)."""
DEFAULT_CODEX_EFFORT = "high"
DIAGNOSE_TIMEOUT_SECONDS = 900.0
SYNC_TIMEOUT_SECONDS = 300.0
LAUNCH_GRACE_SECONDS = 1.5
PROMPT_LIMIT = 90_000
"""Bytes of context put in one diagnosis prompt (argv-safe)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def state_dir(home: Path) -> Path:
    return _env_path("TRIO_DASH_STATE_DIR") or home / ".local" / "state" / "trio-dash"


def native_runs_dir(home: Path) -> Path:
    return (_env_path("TRIO_NATIVE_RUNS_DIR")
            or home / ".local" / "share" / "trio-agent-loop" / "native-runs")


def _executable(path: Path | None) -> Path | None:
    if path is None:
        return None
    try:
        return path if path.is_file() and os.access(path, os.X_OK) else None
    except OSError:
        return None


def trioctl_path(home: Path) -> Path | None:
    """The installed trioctl (``TRIO_DASH_TRIOCTL`` or ``~/.local/bin``)."""
    configured = _env_path("TRIO_DASH_TRIOCTL")
    return _executable(configured or home / ".local" / "bin" / "trioctl")


def release_dir(home: Path) -> Path | None:
    """The installed release (``TRIO_DASH_RELEASE_DIR`` or CURRENT)."""
    configured = _env_path("TRIO_DASH_RELEASE_DIR")
    if configured is not None:
        return configured if configured.is_dir() else None
    share = home / ".local" / "share" / "trio-agent-loop"
    try:
        sha = (share / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return None
    path = share / "releases" / sha
    return path if path.is_dir() else None


CURSOR_MODELS = frozenset({"cursor-grok-4.6-low"})
"""Cursor models a diagnosis may use (``TRIO_DASH_CURSOR_MODEL`` must be one;
never a Claude model: diagnosis is never Claude)."""
CODEX_MODELS = frozenset({"gpt-6-luna"})
CODEX_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

CURSOR_WARNING = (
    "Cursor cannot be fully isolated. The dashboard runs it in ask mode with its own "
    "empty HOME and config dir (your ~/.cursor/mcp.json servers, approvals and "
    "allowlist are not loaded; writes and shell are denied by config), but the agent "
    "still has Cursor's built-in WebFetch/WebSearch (they run on Cursor's servers and "
    "can reach any public URL), the Task subagent, dynamic tools, and the MCP servers "
    "of plugins synced from your Cursor account (verified 2026-09-29: Playwright, "
    "Outlook, Higgsfield). Mailbox text the agent reads could steer it into those. "
    "Codex (OS read-only sandbox, no network) is the default; choose Cursor only if "
    "you accept that exposure.")

def _allowed_env(name: str, default: str, allowed: frozenset) -> tuple[str, str | None]:
    """(value, error): an env override outside the allowlist is ignored and
    reported, never used."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default, None
    if raw in allowed:
        return raw, None
    return default, f"{name}={raw!r} is not allowed (allowed: {', '.join(sorted(allowed))}); using {default}"


def harnesses(home: Path) -> dict:
    """Diagnosis harness catalog: binary, model, availability.

    Codex is the default: it runs in its OS read-only sandbox without network.
    Cursor is opt-in (``TRIO_DASH_DIAGNOSE_HARNESS=cursor`` or the drawer's
    picker) and carries ``warning``: its read-only mode is model-level and its
    built-in tools cannot be removed (see ``CURSOR_WARNING``)."""
    cursor = _executable(_env_path("TRIO_DASH_CURSOR_AGENT")
                         or home / ".local" / "bin" / "cursor-agent")
    codex = _executable(_env_path("TRIO_DASH_CODEX")
                        or home / ".local" / "bin" / "codex")
    default = os.environ.get("TRIO_DASH_DIAGNOSE_HARNESS", "codex").strip()
    if default not in ("cursor", "codex"):
        default = "codex"
    cursor_model, cursor_err = _allowed_env("TRIO_DASH_CURSOR_MODEL", DEFAULT_CURSOR_MODEL,
                                            CURSOR_MODELS)
    codex_model, codex_err = _allowed_env("TRIO_DASH_CODEX_MODEL", DEFAULT_CODEX_MODEL,
                                          CODEX_MODELS)
    effort, effort_err = _allowed_env("TRIO_DASH_CODEX_EFFORT", DEFAULT_CODEX_EFFORT,
                                      CODEX_EFFORTS)
    return {
        "default": default,
        "cursor": {
            "available": cursor is not None, "bin": str(cursor) if cursor else None,
            "model": cursor_model,
            "mode": "ask (model-level read-only; isolated HOME/config, no MCP)",
            "warning": CURSOR_WARNING,
            "config_errors": [e for e in (cursor_err,) if e],
        },
        "codex": {
            "available": codex is not None, "bin": str(codex) if codex else None,
            "model": codex_model,
            "effort": effort,
            "mode": "exec --sandbox read-only (OS sandbox, no network)",
            "config_errors": [e for e in (codex_err, effort_err) if e],
        },
    }


# --------------------------------------------------------------------------
# Small readers
# --------------------------------------------------------------------------

READ_LIMIT = 8 * 1024 * 1024


def read_bytes_nofollow(path: Path, limit: int = READ_LIMIT) -> bytes | None:
    """A regular file's bytes, opened without following a symlink (None when
    absent, a symlink, not a regular file, unreadable or over ``limit``).

    Every mailbox, sidecar, lock, registry and state file the dashboard
    reads goes through here (eval2 finding 2): a symlink planted in a repo
    never makes the dashboard read (and serve) a file outside it."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_json(path: Path) -> dict | None:
    raw = read_bytes_nofollow(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):  # a deeply nested record is not JSON to us
        return None
    return data if isinstance(data, dict) else None


def read_text(path: Path, limit: int | None = None) -> str | None:
    raw = read_bytes_nofollow(path)
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    if limit is not None and len(text) > limit:
        return text[:limit] + f"\n… [truncated, {len(text)} chars]"
    return text


def tail_text(path: Path, limit: int) -> str | None:
    text = read_text(path)
    if text is None or len(text) <= limit:
        return text
    return f"[… {len(text) - limit} earlier chars]\n" + text[-limit:]


_STATE_LINE = re.compile(r"^\s*(?:-\s+)?([A-Za-z_]+)\s*:(.*)$")


def read_state(mailbox: Path) -> dict[str, str]:
    """STATE.md key/values (first occurrence wins, keys lowercased)."""
    state: dict[str, str] = {}
    for line in (read_text(Path(mailbox) / "STATE.md") or "").splitlines():
        m = _STATE_LINE.match(line)
        if m:
            state.setdefault(m.group(1).lower(), m.group(2).strip())
    return state


def status_word(state: dict) -> str:
    raw = (state.get("status") or "").strip().lower().replace("-", "_")
    return raw.split()[0] if raw.split() else ""


def to_int(value) -> int | None:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group(0)) if m else None


_VERDICT_RE = re.compile(r"^(?:#\s*)?VERDICT:\s*(\w+)(.*)$", re.IGNORECASE)
_SCOPE_RE = re.compile(r"\bscope=(design|local:[^\s|]+)", re.IGNORECASE)


def read_verdict(mailbox: Path) -> tuple[str | None, str | None]:
    """First non-empty VERDICT.md line: (word, scope)."""
    for line in (read_text(Path(mailbox) / "VERDICT.md") or "").splitlines():
        if not line.strip():
            continue
        m = _VERDICT_RE.match(line.strip())
        if not m:
            return None, None
        scope = _SCOPE_RE.search(m.group(2) or "")
        return m.group(1).upper(), scope.group(1) if scope else None
    return None, None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    stat = read_text(Path(f"/proc/{pid}/stat"))
    if stat and stat.rsplit(")", 1)[-1].split()[:1] == ["Z"]:
        return False
    return True


def lock_info(mailbox: Path) -> dict | None:
    lock = Path(mailbox) / ".lock"
    if lock.is_symlink() or not lock.is_dir():
        return None
    pid = to_int(read_text(lock / "pid"))
    owner = (read_text(lock / "owner") or "").strip() or None
    return {"pid": pid, "owner": owner, "alive": bool(pid and pid_alive(pid)),
            "heartbeat": (read_text(lock / "heartbeat") or "").strip() or None}


def held_records(mailbox: Path) -> list[dict]:
    """Omnigent held-dispatch records (``.sessions/held-*.json``)."""
    out = []
    try:
        paths = sorted((Path(mailbox) / ".sessions").glob("held-*.json"))
    except OSError:
        return out
    for path in paths:
        record = read_json(path) or {}
        out.append({
            "file": path.name,
            "session_id": record.get("session_id"),
            "role": record.get("role"),
            "hold": record.get("hold"),
            "iteration": record.get("iteration"),
            "reason": record.get("reason") or record.get("detail"),
            "recorded_at": record.get("recorded_at") or record.get("at"),
        })
    return out


# --------------------------------------------------------------------------
# Native (claude-workflow) facts and the run registry
# --------------------------------------------------------------------------

NATIVE_DRIVER = "claude-workflow"
_MAILBOX_MARKERS = ("GOAL.md", "STATE.md", "LOG.md", "PLAN.md", "VERDICT.md")


def is_mailbox(path: Path) -> bool:
    try:
        return path.is_dir() and any((path / m).is_file() for m in _MAILBOX_MARKERS)
    except OSError:
        return False


def native_registry(home: Path) -> list[dict]:
    """Valid native run records: an absolute, existing mailbox each.

    The registry is written by native/launch.sh and the helper's begin/end
    (native-dash); it only makes runs *visible*. Nothing is executed from a
    record without re-validating it (see ``native_launcher``)."""
    by_mailbox: dict[str, dict] = {}
    try:
        files = sorted(native_runs_dir(home).glob("*.json"))
    except OSError:
        return []
    for path in files:
        record = read_json(path)
        if not record or record.get("driver", NATIVE_DRIVER) != NATIVE_DRIVER:
            continue
        mailbox = record.get("mailbox")
        if not isinstance(mailbox, str) or not os.path.isabs(mailbox):
            continue
        try:
            mbox = Path(mailbox).resolve()
        except OSError:
            continue
        if not is_mailbox(mbox):
            continue
        repo_path = registry_repo(home, mbox, record.get("repo"))
        if repo_path is None:
            continue
        entry = {**record, "mailbox": str(mbox), "repo": str(repo_path),
                 "registry_file": str(path)}
        # One record per real mailbox: the most recently updated wins.
        prev = by_mailbox.get(str(mbox))
        if prev is None or str(entry.get("updated_at") or "") >= str(prev.get("updated_at") or ""):
            by_mailbox[str(mbox)] = entry
    return [by_mailbox[k] for k in sorted(by_mailbox)]


def registry_repo(home: Path, mailbox: Path, claimed) -> Path | None:
    """The repo a registry record may add as a workspace seed: the real git
    toplevel of the mailbox. A claimed ``repo`` is accepted only when it IS
    that toplevel; ``/``, the home directory and any ancestor of it are never
    seeds (eval finding 7). None when the mailbox is in no git checkout."""
    top = git_toplevel(mailbox)
    if top is None:
        return None
    try:
        top = top.resolve()
        home_real = Path(home).resolve()
    except OSError:
        return None
    if top == Path(top.anchor) or top == home_real or _under(home_real, top):
        return None
    if isinstance(claimed, str) and claimed:
        try:
            if Path(claimed).resolve() != top:
                return None
        except OSError:
            return None
    return top


_FENCE_RE = re.compile(r"```(?:jsonc?|json5)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S | re.I)


def result_from_raw(mailbox: Path, launch: dict | None) -> dict | None:
    """Recover a run's result from its raw session output (``.native-runs/
    <session>.<ts>.<mode>.json``) when no ``.native-result.json`` exists —
    runs launched before native-dash. Same parse as launch.sh: the last
    fenced JSON object carrying ``status``."""
    session = (launch or {}).get("session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session):
        return None
    try:
        raws = sorted((Path(mailbox) / ".native-runs").glob(f"{session}.*.json"),
                      key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    raws = [p for p in raws if not p.name.startswith("launch-record.")]
    if not raws:
        return None
    raw = raws[-1]
    outer = read_json(raw)
    text = outer.get("result") if outer and isinstance(outer.get("result"), str) else (read_text(raw) or "")
    found = None
    for block in _FENCE_RE.findall(text or ""):
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if isinstance(value, dict) and "status" in value:
            found = value
    if found is None:
        return None
    finished = datetime.fromtimestamp(raw.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = ("status", "verdict", "code", "reason", "iteration", "held_step", "end_error",
            "conflicts", "dangling_worktrees", "role_denials", "human_check", "lock")
    out = {k: found.get(k) for k in keep if k in found}
    out.update({"source": "raw", "driver": NATIVE_DRIVER, "session_id": session,
                "finished_at": finished, "raw": str(raw),
                "api_equiv_usd": outer.get("total_cost_usd") if outer else None})
    return out


def _load_native_args():
    """metrics/native_args.py of this release: the one resume-args and
    mailbox-path validator, byte-identical to the one native/launch.sh uses
    (eval3 findings 4, 6, 7)."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "metrics" / "native_args.py"
    spec = importlib.util.spec_from_file_location("trio_dash_native_args", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


native_args = _load_native_args()
UUID_RE = native_args.UUID_RE
RUN_ID_RE = native_args.RUN_ID_RE
RUN_TOKEN_RE = native_args.RUN_TOKEN_RE
NATIVE_ARG_KEYS = native_args.ARG_KEYS
NATIVE_MODEL_ROLES = native_args.MODEL_ROLES
NATIVE_MODELS = frozenset(native_args.MODELS)
"""Models a recorded native run may name (the workflow's defaults); a resume
of a record naming anything else is refused (never replayed)."""
NATIVE_CAPS = native_args.CAPS
NativeArgsError = native_args.NativeArgsError
canonical_session_id = native_args.canonical_session_id


def validate_native_args(raw, *, mailbox: Path, helper: Path | None) -> dict:
    """The recorded workflow args as a validated dict (key order kept), or
    NativeArgsError — never another exception. The schema is
    ``metrics/native_args.py``'s, which launch.sh applies too, so a preview
    never offers a resume launch.sh refuses: ``mailbox`` (required; must BE
    this mailbox, canonical, path rule), ``max_iterations`` (required int
    1..200), optional ``max_agents`` / ``token_budget`` (bounded ints),
    ``run_token``, ``helper`` (only the installed release's own helper) and
    ``models`` ({role: model}, strings from the allowlists). A missing
    ``run_token`` is reported by :func:`native_resume_args`."""
    return native_args.validate_args(raw, mailbox=mailbox, helper=helper,
                                     require_run_token=False)


def native_facts(mailbox: Path, home: Path | None = None) -> dict | None:
    """The claude-workflow driver's files in a mailbox, or None.

    Every value that could later steer a command (the recorded session id,
    the workflow args, the run id) is validated here against a strict schema;
    the raw records are display-only. ``args``/``session_id``/``run_id`` are
    None (with ``*_error``) when they fail it (eval2 finding 1)."""
    mailbox = Path(mailbox)
    session = read_json(mailbox / ".session.json")
    launch = read_json(mailbox / ".native-launch.json")
    result = read_json(mailbox / ".native-result.json")
    result_record = result  # the file as written, even when raw output replaces it below
    is_native = bool(
        (session and session.get("driver") == NATIVE_DRIVER)
        or launch or (result and result.get("driver", NATIVE_DRIVER) == NATIVE_DRIVER))
    if not is_native:
        return None
    errors: dict[str, str] = {}
    session_id = None
    raw_session = (launch or {}).get("session_id") if launch else (result or {}).get("session_id")
    if raw_session is not None:
        try:
            session_id = canonical_session_id(raw_session)
        except NativeArgsError as exc:
            errors["session_id"] = str(exc)
    if (not result or result.get("source") == "end") and launch and session_id:
        recovered = result_from_raw(mailbox, {"session_id": session_id})
        if recovered is not None:
            if result:  # keep the end op's lock/dangling facts
                recovered.setdefault("dangling_worktrees", result.get("dangling_worktrees"))
            result = recovered
    args = None
    if launch is not None:
        try:
            args = validate_native_args(launch.get("args"), mailbox=mailbox,
                                        helper=native_helper(home) if home is not None else None)
        except NativeArgsError as exc:
            errors["args"] = str(exc)
    session_live = bool(session and session.get("driver") == NATIVE_DRIVER
                        and not session.get("done") and pid_alive(session.get("pid")))
    lock = lock_info(mailbox)
    run_id = (result or {}).get("run_id")
    if run_id is not None and not (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)):
        errors["run_id"] = "run_id is not wf_[A-Za-z0-9_-]{1,64}"
        run_id = None
    if home is not None and session_id and not run_id and "run_id" not in errors:
        run_id = find_run_id(home, session_id)
    return {
        "session": session, "launch": launch, "result": result,
        "result_record": result_record, "args": args, "errors": errors, "lock": lock, "session_live": session_live,
        "session_id": session_id, "run_id": run_id,
        "running": session_live or bool(lock and lock["alive"]
                                        and str(lock.get("owner") or "").startswith("workflow:")),
    }


def find_run_id(home: Path, session_id: str) -> str | None:
    """The newest ``wf_…`` run id recorded under the launching session."""
    if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", str(session_id or "")):
        return None
    base = _env_path("CLAUDE_CONFIG_DIR") or home / ".claude"
    found = []
    try:
        for session_dir in (base / "projects").glob(f"*/{session_id}"):
            for pattern, rx in (("workflows/wf_*.json", r"^(wf_[\w-]+)\.json$"),
                                ("subagents/workflows/wf_*", r"^(wf_[\w-]+)$"),
                                ("workflows/scripts/*-wf_*.js", r"-(wf_[\w-]+)\.js$")):
                for path in session_dir.glob(pattern):
                    m = re.search(rx, path.name)
                    if m:
                        found.append((path.stat().st_mtime, m.group(1)))
    except OSError:
        return None
    return max(found)[1] if found else None


def release_native_dir(home: Path) -> Path | None:
    """The INSTALLED release's ``native/`` directory — the only source of the
    launcher and helper a dashboard fix ever runs.

    ``TRIO_DASH_RELEASE_NATIVE`` (a native dir; server configuration, never
    a mailbox value) wins, then the legacy ``TRIO_DASH_NATIVE_LAUNCH`` (its
    ``launch.sh``), then ``<release>/native``. Launcher paths found in the
    run registry, ``.native-result.json`` or ``.native-launch.json`` are
    display-only and never consulted here (eval finding 1)."""
    configured = _env_path("TRIO_DASH_RELEASE_NATIVE")
    if configured is None:
        legacy = _env_path("TRIO_DASH_NATIVE_LAUNCH")
        configured = legacy.parent if legacy is not None and legacy.name == "launch.sh" else None
    if configured is None:
        rel = release_dir(home)
        configured = rel / "native" if rel is not None else None
    if configured is None or not configured.is_absolute():
        return None
    try:
        real = configured.resolve()
        if all((real / name).is_file() for name in ("launch.sh", "trio_native_step.py",
                                                    "trio-native.js")):
            return real
    except OSError:
        return None
    return None


def native_launcher(home: Path) -> Path | None:
    """The installed release's native ``launch.sh`` (see ``release_native_dir``)."""
    native = release_native_dir(home)
    return native / "launch.sh" if native is not None else None


def native_helper(home: Path) -> Path | None:
    """The installed release's ``trio_native_step.py``."""
    native = release_native_dir(home)
    return native / "trio_native_step.py" if native is not None else None


WORKFLOW_SCRIPT_FILE = "trio-native.js"
WORKFLOW_SCRIPT_SCOPES = ("project", "user", "none", "unknown")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SCRIPT_HASH_LIMIT = 8 * 1024 * 1024


def _display_path(value) -> str | None:
    """A recorded path for display (the UI sets it as textContent): a string
    without control characters, capped; anything else is None."""
    if not isinstance(value, str) or not value:
        return None
    return re.sub(r"[\x00-\x1f\x7f]", "?", value)[:400]


def _file_sha256(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _SCRIPT_HASH_LIMIT:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _script_view(path, scope, is_release, sha256) -> dict:
    sha = sha256 if isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256) else None
    return {
        "path": _display_path(path),
        "scope": scope if scope in WORKFLOW_SCRIPT_SCOPES else "unknown",
        "is_release": is_release if isinstance(is_release, bool) else None,
        "sha256": sha, "sha_short": sha[:12] if sha else None,
    }


def native_script_view(native: dict | None, registry: dict | None, home: Path,
                       repo: Path | None) -> dict | None:
    """Display-only view of the trio-native Workflow script (native-v01's
    ``workflow_script*`` fields), or None for a non-native loop.

    ``recorded``: what the last run's ``.native-result.json`` (else its run
    registry record) says the session ran; every field is ``None``/
    ``"unknown"`` for records that predate those fields. ``next``: what a
    Start/Resume from the dashboard would run now: the installed release's
    launch.sh starts claude in the loop's repo, which loads ``trio-native.js``
    by the documented precedence (the repo's ``.claude/workflows/``, then
    ``$CLAUDE_CONFIG_DIR``/``~/.claude`` ``workflows/``); ``is_release``
    compares that file with the release's own ``trio-native.js`` (same file
    or same bytes), as launch.sh does. Nothing here is ever executed."""
    if not native:
        return None
    source, record = None, {}
    for name, candidate in (("result", native.get("result_record")), ("registry", registry)):
        if isinstance(candidate, dict) and any(k.startswith("workflow_script") for k in candidate):
            source, record = name, candidate
            break
    recorded = _script_view(record.get("workflow_script"), record.get("workflow_script_scope"),
                            record.get("workflow_script_is_release"),
                            record.get("workflow_script_sha256"))
    recorded["source"] = source
    raw_candidates = record.get("workflow_script_candidates")
    recorded["candidates"] = [
        {"path": _display_path(c.get("path")),
         "scope": c.get("scope") if c.get("scope") in WORKFLOW_SCRIPT_SCOPES else "unknown",
         "exists": c.get("exists") if isinstance(c.get("exists"), bool) else None}
        for c in (raw_candidates if isinstance(raw_candidates, list) else [])[:8]
        if isinstance(c, dict)]

    release_dir = release_native_dir(home)
    release = release_dir / WORKFLOW_SCRIPT_FILE if release_dir is not None else None
    release_sha = _file_sha256(release) if release is not None else None
    nxt = _script_view(None, "unknown", None, None)
    try:
        config = _env_path("CLAUDE_CONFIG_DIR") or Path(home) / ".claude"
        candidates = ([(Path(repo) / ".claude" / "workflows" / WORKFLOW_SCRIPT_FILE, "project")]
                      if repo is not None else [])
        candidates.append((config / "workflows" / WORKFLOW_SCRIPT_FILE, "user"))
        winner = next(((p, s) for p, s in candidates if p.is_file()), None)
        if winner is None:
            nxt = _script_view(None, "none", None, None)
        else:
            real = winner[0].resolve()
            sha = _file_sha256(real)
            same = release is not None and (_same_file(real, release)
                                            or (sha is not None and sha == release_sha))
            nxt = _script_view(str(real), winner[1], same if release is not None else None, sha)
    except OSError:
        pass
    release_view = _script_view(str(release) if release is not None else None,
                                "unknown", True if release is not None else None, release_sha)
    del release_view["scope"]
    nxt["release"] = release_view
    return {"recorded": recorded, "next": nxt}


def _same_file(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Git helpers (read-only unless a fix says otherwise)
# --------------------------------------------------------------------------

# SAFE_GIT_CONFIG (every git argv below) is metrics/human_ledger.py's tuple,
# bound after ``ledger()`` is defined (eval4 finding 2, eval5 finding 1).


def git_argv(cwd: Path, *args: str) -> list[str]:
    """``git <safe config> -C <cwd> <args>`` (also for planned fix steps)."""
    return ["git", *SAFE_GIT_CONFIG, "-C", str(cwd), *args]


def git(cwd: Path, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--no-optional-locks", *SAFE_GIT_CONFIG, "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout, check=False)


def git_toplevel(path: Path) -> Path | None:
    try:
        out = git(path, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    return Path(value) if out.returncode == 0 and value else None


def git_state(repo: Path | None, mailbox: Path) -> dict:
    """Branch, HEAD, status and worktrees of the mailbox's checkout."""
    if repo is None:
        return {"repo": None}
    out: dict = {"repo": str(repo)}
    try:
        out["branch"] = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        out["head"] = git(repo, "rev-parse", "HEAD").stdout.strip()
        out["status"] = git(repo, "status", "--porcelain=v1", "--untracked-files=normal").stdout[:6000]
        out["log"] = git(repo, "log", "--oneline", "-8").stdout
        out["worktrees"] = git(repo, "worktree", "list", "--porcelain").stdout[:6000]
        out["merge_in_progress"] = bool(git(repo, "rev-parse", "-q", "--verify",
                                            "MERGE_HEAD").stdout.strip())
        out["unmerged"] = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)
    return out


def worktree_list(repo: Path) -> list[dict]:
    try:
        text = git(repo, "worktree", "list", "--porcelain").stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    trees, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur:
                trees.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    return trees


# --------------------------------------------------------------------------
# States and the unblock table
# --------------------------------------------------------------------------

RUNNING_WORDS = {"running", "in_progress", "active", "iterating"}

STATES = (
    "running", "shipped", "needs_human", "blocked", "error", "needs_retirement",
    "needs_land", "held", "conflict", "budget", "iteration_cap", "interrupted",
    "answered", "ready", "unknown",
)

UNBLOCK_TABLE = [
    {"exit": 0, "status": "shipped", "state": "shipped",
     "meaning": "SHIP accepted, retirement complete (root-free: landed)",
     "next": "nothing to do", "fixes": []},
    {"exit": 1, "status": "error (driver-exception) / unchanged", "state": "error",
     "meaning": "trioctl or dispatch exception",
     "next": "read LOG/STATE reason; reset STATE and re-run", "fixes": ["reset_and_rerun"]},
    {"exit": 2, "status": "blocked", "state": "blocked",
     "meaning": "BLOCKED verdict, or start refused (writes overlap, bad flags)",
     "next": "a human answers in HUMAN.md (answer box), STATE reset, re-run",
     "fixes": ["answer"]},
    {"exit": 3, "status": "error", "state": "error",
     "meaning": "loop error (stalled Lead, gate error, unparseable verdict, setup failure)",
     "next": "fix the cause; reset STATE and re-run", "fixes": ["reset_and_rerun", "native_reset_and_start"]},
    {"exit": 4, "status": "unchanged", "state": "iteration_cap",
     "meaning": "--max-iterations reached",
     "next": "re-run with a higher --max-iterations", "fixes": ["rerun_more_iterations"]},
    {"exit": 5, "status": "needs_human", "state": "needs_human",
     "meaning": "NEEDS_HUMAN verdict (or the mailbox is owned by a live driver)",
     "next": "a human runs the ## Human check and answers (HUMAN.md); STATE reset; re-run — never automated",
     "fixes": ["answer"]},
    {"exit": 6, "status": "needs_retirement", "state": "needs_retirement",
     "meaning": "SHIP whose retirement commit could not complete",
     "next": "/trio-ship (mailbox retirement commit), then re-run to finalize",
     "fixes": ["retire_ship", "rerun", "native_start"]},
    {"exit": 7, "status": "needs_human", "state": "held",
     "meaning": "held dispatch (.sessions/held-*.json)",
     "next": "trioctl omnigent reconcile (dry run); --apply only when receipt-proven; else a human",
     "fixes": ["reconcile_dry_run", "reconcile_apply"]},
    {"exit": 8, "status": "needs_land", "state": "needs_land",
     "meaning": "root-free verified branch could not land",
     "next": "resolve any conflict in the Lead worktree (human), then trioctl omnigent land",
     "fixes": ["land"]},
    {"exit": 130, "status": "unchanged", "state": "interrupted",
     "meaning": "interrupted (SIGINT/SIGTERM)", "next": "re-run (native: launch.sh resume)",
     "fixes": ["rerun", "native_resume", "native_start"]},
    {"exit": None, "status": "native held", "state": "held",
     "meaning": "claude-workflow: a step's Bash call was denied (held_step)",
     "next": "review the denial; start a fresh run (never a permission change)", "fixes": ["native_start"]},
    {"exit": None, "status": "native conflict", "state": "conflict",
     "meaning": "claude-workflow: a slice conflicted again after its re-dispatch",
     "next": "a fresh run re-plans (STATE stays lead-running)", "fixes": ["native_start"]},
    {"exit": None, "status": "native budget", "state": "budget",
     "meaning": "claude-workflow: max_agents / token_budget exhausted",
     "next": "start a fresh run (resumable state)", "fixes": ["native_start"]},
    {"exit": None, "status": "dangling worktrees", "state": "dangling_worktrees",
     "meaning": "builder worktrees left unmerged or dirty",
     "next": "remove clean, merged ones; a human reviews the rest", "fixes": ["cleanup_worktrees"]},
]


def _fresh_result(result: dict, session: dict | None, mailbox: Path) -> bool:
    """A result record describes the latest run, not an older one, and
    STATE.md was not changed after it (e.g. an answer reset)."""
    if not result:
        return False
    finished = str(result.get("finished_at") or "")
    started = str((session or {}).get("started_at") or "")
    recorded = result.get("session_started_at")
    if started and recorded and recorded != started:
        return False  # a later run began after this result was written
    try:
        state_mtime = (Path(mailbox) / "STATE.md").stat().st_mtime
        finished_ts = datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp() if finished else None
    except (OSError, ValueError):
        return True
    return finished_ts is None or state_mtime <= finished_ts + 2


_NATIVE_STATUS = {
    "shipped": "shipped", "held": "held", "conflict": "conflict", "budget": "budget",
    "error": "error", "needs_retirement": "needs_retirement",
    "needs_human": "needs_human", "blocked": "blocked", "max_iterations": "iteration_cap",
    "needs_land": "needs_land",
}


def derive_state(mailbox: Path, running_sources: list[str], *, home: Path | None = None,
                 last_action: dict | None = None, native: dict | None = None) -> dict:
    """One factual state for a mailbox (the live copy for root-free loops).

    Order: live evidence, then a claude-workflow result that describes the
    latest run, then held-dispatch records, then STATE.md's status word,
    then the iteration cap. Anything unknown stays ``unknown``/``ready``;
    the function never guesses a cause it cannot read."""
    mailbox = Path(mailbox)
    state = read_state(mailbox)
    word = status_word(state)
    phase = (state.get("phase") or "").strip()
    iteration, cap = to_int(state.get("iteration")), to_int(state.get("max_iterations"))
    verdict, scope = read_verdict(mailbox)
    native = native if native is not None else native_facts(mailbox, home)
    holds = held_records(mailbox)
    out = {
        "state": "unknown", "driver": NATIVE_DRIVER if native else None,
        "status": word or None, "phase": phase or None, "iteration": iteration,
        "max_iterations": cap, "verdict": verdict, "scope": scope,
        "summary": "", "detail": {}, "held": holds,
    }
    dangling = []
    if native and native.get("result"):
        dangling = [p for p in native["result"].get("dangling_worktrees") or []
                    if isinstance(p, str) and os.path.isdir(p)]
    if dangling:
        out["detail"]["dangling_worktrees"] = dangling
    if native:
        out["detail"].update({
            "session_id": native.get("session_id"), "run_id": native.get("run_id"),
            "api_equiv_usd": (native.get("result") or {}).get("api_equiv_usd"),
        })

    if running_sources:
        out.update(state="running", summary="live via " + ", ".join(running_sources))
        return out

    result = (native or {}).get("result") or {}
    if (native and result.get("source") in ("launcher", "raw")
            and _fresh_result(result, native.get("session"), mailbox)):
        mapped = _NATIVE_STATUS.get(str(result.get("status") or ""))
        if mapped:
            out["state"] = mapped
            out["detail"].update({k: result.get(k) for k in (
                "held_step", "end_error", "conflicts", "role_denials", "reason",
                "code", "human_check", "exit_code", "finished_at") if result.get(k) is not None})
            out["summary"] = {
                "held": f"held at step {result.get('held_step') or '?'}",
                "conflict": "merge conflict after re-dispatch: " + ", ".join(
                    sorted({f for c in result.get("conflicts") or [] for f in (c.get("files") or [])}))[:300],
                "budget": "agent/token budget exhausted",
                "error": str(result.get("reason") or "error")[:300],
                "iteration_cap": f"stopped at max_iterations ({iteration})",
            }.get(mapped, f"native run ended: {result.get('status')}")
            return out

    if holds:
        out.update(state="held", summary=f"{len(holds)} held dispatch record(s)")
        return out
    terminal = {"shipped": "shipped", "ship": "shipped", "blocked": "blocked",
                "needs_human": "needs_human",
                "error": "error", "needs_retirement": "needs_retirement",
                "needs_land": "needs_land"}
    if word in terminal:
        out["state"] = terminal[word]
        reason = state.get("reason")
        out["summary"] = f"STATE.md status {word}" + (f" (phase {phase})" if phase else "") + (
            f": {reason}" if reason else "")
        if reason:
            out["detail"]["reason"] = reason
        return out
    code = (last_action or {}).get("exit_code")
    if code == 4 or (word in RUNNING_WORDS and iteration and cap and iteration >= cap
                     and phase.lower() in ("", "idle") and verdict == "ITERATE"):
        out.update(state="iteration_cap", summary=f"iteration {iteration} of {cap}: cap reached")
        return out
    if word in RUNNING_WORDS and phase.lower() == "idle" and state.get("human_answer"):
        out.update(state="answered", summary="answered (" + state["human_answer"] + "); restart the loop")
        return out
    if word in RUNNING_WORDS:
        if native:
            sess = native.get("session") or {}
            lock = native.get("lock")
            args = native.get("args") or {}
            out["detail"]["resumable"] = bool(native.get("run_id") and native.get("session_id")
                                              and isinstance(args, dict) and args.get("run_token")
                                              and not native.get("errors"))
            out.update(state="interrupted", summary=(
                "claude-workflow run is not live" + (
                    f" (lock pid {lock['pid']} dead)" if lock and not lock["alive"] else "")
                + ("" if sess.get("done") else "; session not closed")))
        else:
            out.update(state="interrupted", summary=f"STATE.md says {word}; nothing is live"
                       + (f" (driver exit {code})" if code is not None else ""))
        return out
    out.update(state="ready" if word in ("ready", "idle", "") or phase.lower() == "idle" else "unknown",
               summary=f"STATE.md status {word or '—'}")
    return out


# --------------------------------------------------------------------------
# Fix allowlist
# --------------------------------------------------------------------------

FIXES = {
    "rerun": {"title": "Re-run the loop (installed trioctl)", "destructive": False},
    "rerun_more_iterations": {"title": "Re-run with a higher --max-iterations", "destructive": False},
    "reset_and_rerun": {"title": "Reset STATE (status running, phase idle) and re-run", "destructive": True},
    "native_resume": {"title": "Resume the claude-workflow run (launch.sh resume)", "destructive": False,
                      "confirm": True},
    "native_start": {"title": "Start a fresh claude-workflow run (launch.sh start)", "destructive": False},
    "native_reset_and_start": {"title": "Reset STATE and start a fresh claude-workflow run", "destructive": True},
    "land": {"title": "Land the verified root-free branch (trioctl omnigent land)", "destructive": True},
    "reconcile_dry_run": {"title": "Reconcile held dispatch — dry run", "destructive": False},
    "reconcile_apply": {"title": "Reconcile held dispatch — apply (receipt-proven only)", "destructive": True},
    "retire_ship": {"title": "Retire the SHIP (mailbox retirement commit, /trio-ship clean-tree path)",
                    "destructive": True},
    "repair_scope": {"title": "Run the scoped repair pass for the ITERATE scope", "destructive": False},
    "cleanup_worktrees": {"title": "Remove clean, merged dangling builder worktrees", "destructive": True},
}

NEVER_AUTOMATED = {
    "resolve_needs_human": "resolving a NEEDS_HUMAN check is the human's job (use the answer box)",
    "reconcile_without_receipt": "held-dispatch reconciliation without receipt proof",
    "resolve_land_conflict": "resolving a land conflict",
    "abandon": "trioctl omnigent abandon",
    "sessions_prune": "trioctl omnigent sessions prune",
    "acceptance_amend_human": "acceptance amend --human",
    "change_permissions": "permission or settings changes",
}


class FixRefused(Exception):
    """A fix whose server-side preconditions do not hold (never executed)."""


class PathEscape(FixRefused):
    """A mailbox or mailbox file that resolves outside its workspace (a
    symlink out of the checkout): never read for an agent, never written."""


def workspace_repo(root: Path) -> Path | None:
    """The workspace's own git toplevel: the root itself or a checkout inside
    it. An enclosing repository above the root (a git-versioned HOME, a lab
    repo around a scratch dir) never counts (eval2 finding 5)."""
    top = git_toplevel(root)
    if top is None:
        return None
    try:
        top = top.resolve()
    except OSError:
        return None
    return top if _under(top, root) else None


def _common_dir(path: Path) -> Path | None:
    try:
        out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def trio_worktree_base(home: Path) -> Path:
    """The Trio worktree root convention's base
    (``omnigent/worker_worktrees.default_worktree_root``):
    ``$TRIO_WORKTREE_ROOT``, else ``$XDG_STATE_HOME`` (or ``~/.local/state``)
    ``/trio-agent-loop/worktrees``."""
    env = os.environ.get("TRIO_WORKTREE_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(xdg) if xdg else Path(home) / ".local" / "state") / "trio-agent-loop" / "worktrees"


def _lead_worktree_roots(root: Path, home: Path | None = None) -> list[Path]:
    """Real paths of the workspace repository's git worktrees that may hold a
    root-free loop's live mailbox (eval2 finding 5): listed by ``git worktree
    list --porcelain`` of the workspace's OWN repository (never an enclosing
    one), not prunable, really a worktree of that repository (its own
    ``--git-common-dir`` is the repository's), and located under the
    workspace or under the Trio worktree root convention
    (``<base>/<repo name>-<sha256(common dir)[:12]>``)."""
    top = workspace_repo(root)
    if top is None:
        return []
    common = _common_dir(top)
    if common is None:
        return []
    allowed = [Path(root)]
    if home is not None:
        key = hashlib.sha256(str(common).encode()).hexdigest()[:12]
        allowed.append(trio_worktree_base(home) / f"{top.name}-{key}")
    out = []
    for tree in worktree_list(top):
        path = tree.get("worktree")
        if not isinstance(path, str) or not path or tree.get("prunable") or tree.get("bare"):
            continue
        try:
            real = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not any(_under(real, base) for base in allowed):
            continue
        if _common_dir(real) != common:
            continue
        out.append(real)
    return out


def check_mailbox_paths(root: Path, root_mailbox: Path, live_mailbox: Path,
                        home: Path | None = None) -> tuple[Path, Path]:
    """(real root mailbox, real live mailbox), or PathEscape.

    The root mailbox must resolve inside the resolved workspace root; the
    live copy inside the root or inside one of the workspace repository's
    accepted Lead worktrees (``_lead_worktree_roots``). A symlinked mailbox
    pointing elsewhere is refused (eval finding 4)."""
    try:
        root_real = Path(root).resolve(strict=True)
        rbox = Path(root_mailbox).resolve(strict=True)
        lbox = Path(live_mailbox).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathEscape(f"mailbox path does not resolve: {exc}") from None
    if not _under(rbox, root_real) or not rbox.is_dir():
        raise PathEscape(f"mailbox {root_mailbox} resolves outside the workspace ({rbox})")
    if lbox != rbox and not _under(lbox, root_real):
        if not any(_under(lbox, wt) for wt in _lead_worktree_roots(root_real, home)):
            raise PathEscape(f"live mailbox {live_mailbox} resolves outside the workspace "
                             f"and its accepted Lead worktrees ({lbox})")
    if not lbox.is_dir():
        raise PathEscape(f"live mailbox {live_mailbox} is not a directory")
    return rbox, lbox


MAILBOX_SUBDIRS = (".lock", ".native-runs")


def mailbox_symlinks(mailbox: Path) -> list[str]:
    """Symlinks directly in a mailbox (files and subdirectories alike) and
    inside the driver sidecar directories ``.lock`` and ``.native-runs``. A
    mailbox with any is refused as a whole: nothing in it is read for a
    role, an agent or a peer, and nothing is written (eval2 finding 2).
    Deeper files (``.sessions/*`` exports, held records) are read only
    through the no-follow readers, which skip a link."""
    out = []
    try:
        with os.scandir(mailbox) as entries:
            for entry in entries:
                if entry.is_symlink():
                    out.append(entry.name)
                elif entry.name in MAILBOX_SUBDIRS and entry.is_dir(follow_symlinks=False):
                    try:
                        with os.scandir(entry.path) as sub:
                            out += [f"{entry.name}/{e.name}" for e in sub if e.is_symlink()]
                    except OSError:
                        continue
    except OSError:
        return out
    return sorted(out)


def _has_git_entry(directory: Path) -> bool:
    """Whether *directory* holds a ``.git`` entry (directory, file or link);
    an unreadable one counts as present."""
    try:
        os.lstat(Path(directory) / ".git")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def mailbox_own_repo(mailbox: Path) -> bool:
    """Whether *mailbox* is its own repository's top level: a real ``.git``
    directory (not a link or a gitfile) directly in it, and ``git rev-parse
    --show-toplevel`` from it (under SAFE_GIT_CONFIG) naming the mailbox's
    real path. Never raises; anything unreadable is False."""
    try:
        box = Path(mailbox).resolve(strict=True)
        st = os.lstat(box / ".git")
    except (OSError, RuntimeError):
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    top = git_toplevel(box)
    if top is None:
        return False
    try:
        return top.resolve(strict=True) == box
    except (OSError, RuntimeError):
        return False


def mailbox_nested_git(mailbox: Path, root: Path | None = None,
                       home: Path | None = None) -> bool:
    """Whether a nested (foreign) repository would own the mailbox: a ``.git``
    entry (directory, file or link) in the mailbox or in any directory above
    it, up to (excluding) its anchor — the workspace root, or the accepted
    Lead worktree holding a root-free live copy. git discovery from the
    mailbox would read that repository's config, so such a mailbox is
    refused as a whole (eval4 finding 2; eval5 finding 2: ``loop-grp/.git``
    for ``loop-grp/m1``).

    The anchor's own ``.git`` is the workspace's (or the worktree's) own
    repository: a workspace root that is itself the mailbox and its
    repository's top level is accepted (eval5 finding 3). So is a mailbox
    that is its own repository's top level (``mailbox_own_repo``: a plain
    ``git init`` in the mailbox; eval6 finding 1) — its ``.git`` is that
    loop's own and cannot come from tracked content. A ``.git`` in any
    directory between such a mailbox and its anchor is still refused.
    Without *root* only the mailbox itself is checked. Never raises."""
    if root is None:
        box = Path(mailbox)
        return _has_git_entry(box) and not mailbox_own_repo(box)
    try:
        root_real = Path(root).resolve(strict=True)
        box = Path(mailbox).resolve(strict=True)
    except (OSError, RuntimeError):
        return True
    worktrees: set | None = None
    for directory in (box, *box.parents):
        if directory == root_real:
            return False
        if not _has_git_entry(directory):
            continue
        if directory == box and mailbox_own_repo(box):
            continue
        if worktrees is None:
            try:
                worktrees = set(_lead_worktree_roots(root_real, home))
            except Exception:  # noqa: BLE001 - no worktree list: nothing is accepted
                worktrees = set()
        return directory not in worktrees
    return True


def mailbox_file(mailbox: Path, name: str) -> Path:
    """``<mailbox>/<name>`` for reading or writing, refused (PathEscape) when
    it is a symlink or resolves outside the mailbox."""
    path = Path(mailbox) / name
    try:
        if path.is_symlink():
            raise PathEscape(f"{name} in {mailbox} is a symlink; refusing to follow it")
        if path.exists() and path.resolve().parent != Path(mailbox).resolve():
            raise PathEscape(f"{name} resolves outside {mailbox}")
    except OSError as exc:
        raise PathEscape(f"{name}: {exc}") from None
    return path


def _safe_read(mailbox: Path, name: str) -> str | None:
    """Mailbox file text without following a symlink (None when absent,
    a symlink, not a regular file, or unreadable)."""
    raw = read_bytes_nofollow(Path(mailbox) / name)
    return None if raw is None else raw.decode("utf-8", errors="replace")


def append_nofollow(path: Path, text: str, *, header: str | None = None) -> None:
    """Append to a mailbox file without following a symlink (O_NOFOLLOW);
    ``header`` is written first when the file is created."""
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_NONBLOCK | os.O_CLOEXEC, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        if header is not None and os.fstat(fd).st_size == 0:
            os.write(fd, header.encode("utf-8"))
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


class LoopContext:
    """Everything a fix or a diagnosis reads about one loop, gathered once."""

    def __init__(self, *, home: Path, root: Path, name: str, root_mailbox: Path,
                 live_mailbox: Path, detection: dict, driver: str | None,
                 last_action: dict | None = None, registry: dict | None = None):
        self.home = Path(home)
        self.root = Path(root)
        self.name = name
        check_mailbox_paths(root, root_mailbox, live_mailbox, home)
        links = mailbox_symlinks(live_mailbox) + (
            [] if Path(root_mailbox) == Path(live_mailbox) else
            [f"(root) {n}" for n in mailbox_symlinks(root_mailbox)])
        if links:
            raise PathEscape("the mailbox contains symlinks (" + ", ".join(
                display_name(n, 80) for n in links[:8]) + "); nothing in it is read or acted on")
        if (mailbox_nested_git(live_mailbox, root, home)
                or mailbox_nested_git(root_mailbox, root, home)):
            raise PathEscape("the mailbox contains a .git entry (a nested repository); "
                             "nothing in it is read or acted on")
        self.root_mailbox = Path(root_mailbox)
        self.live_mailbox = Path(live_mailbox)
        self.detection = detection or {}
        self.running_sources = list(self.detection.get("sources") or [])
        self.native = native_facts(self.live_mailbox, self.home)
        self.driver = NATIVE_DRIVER if self.native else driver
        self.last_action = last_action
        self.registry = registry
        self.state = read_state(self.live_mailbox)
        self.derived = derive_state(self.live_mailbox, self.running_sources, home=self.home,
                                    last_action=last_action, native=self.native)
        # The workspace's own checkout; an enclosing repository above the
        # workspace root (e.g. a lab repo around a scratch dir) never counts.
        top = workspace_repo(self.root)
        mailbox_top = git_toplevel(self.root_mailbox)
        if mailbox_top is not None and _under(mailbox_top, self.root):
            top = mailbox_top
        self.repo_root = top if top is not None else self.root
        if self.live_mailbox == self.root_mailbox:
            self.live_repo = self.repo_root
        else:
            self.live_repo = git_toplevel(self.live_mailbox) or self.repo_root
        self.key = loop_key(self.root_mailbox)

    @property
    def live(self) -> bool:
        return bool(self.running_sources) or bool(self.native and self.native.get("running"))


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def loop_key(root_mailbox: Path) -> str:
    return hashlib.sha256(str(Path(root_mailbox).resolve()).encode()).hexdigest()[:16]


def _require_not_live(ctx: LoopContext) -> None:
    if ctx.running_sources:
        raise FixRefused("a driver is live (" + ", ".join(ctx.running_sources) + "); stop it first")
    lock = lock_info(ctx.live_mailbox)
    if lock and lock["alive"]:
        raise FixRefused(f"the mailbox lock is held by live pid {lock['pid']} ({lock.get('owner')})")
    if ctx.native and ctx.native.get("session_live"):
        raise FixRefused("the claude-workflow session pid is live")
    broker = ctx.detection.get("broker")
    if broker in ("unreachable", "truncated"):
        raise FixRefused("broker liveness is unknown (" + broker + "); a broker-only run cannot be ruled out")


def _max_iterations_arg(ctx: LoopContext, args: dict, *, must_exceed: bool) -> int:
    iteration = to_int(ctx.state.get("iteration")) or 0
    cap = to_int(ctx.state.get("max_iterations"))
    value = args.get("max_iterations", None)
    if value is None:
        value = cap if cap and cap > iteration else iteration + (cap or 4)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise FixRefused("max_iterations must be an integer 1..200")
    if must_exceed and value <= iteration:
        raise FixRefused(f"max_iterations must exceed the current iteration ({iteration})")
    return value


def _driver_cmd(ctx: LoopContext, max_iterations: int) -> tuple[list[str], Path]:
    """Installed-release command that (re)runs a non-native loop."""
    if ctx.driver == "portable":
        rel = release_dir(ctx.home)
        entry = rel / "metrics" / "trio_loop.py" if rel else None
        if entry is None or not entry.is_file():
            raise FixRefused("the portable driver is not installed (no release trio_loop.py)")
        require_prompt_safe_path(ctx.root_mailbox)
        return (["python3", str(entry), "run", "--mailbox", str(ctx.root_mailbox),
                 "--max-iterations", str(max_iterations), "--runner", "portable"], ctx.repo_root)
    trioctl = trioctl_path(ctx.home)
    if trioctl is None:
        raise FixRefused("the installed trioctl is missing (~/.local/bin/trioctl or TRIO_DASH_TRIOCTL)")
    require_prompt_safe_path(ctx.root_mailbox)
    require_prompt_safe_path(ctx.live_mailbox, "live mailbox")
    return ([str(trioctl), "omnigent", "loop", "--mailbox", str(ctx.root_mailbox),
             "--max-iterations", str(max_iterations)], ctx.repo_root)


def require_prompt_safe_path(path: Path, what: str = "mailbox") -> None:
    """A mailbox path a driver may be started on (eval3 finding 7): any
    absolute path of printable characters (spaces, non-ASCII letters, ``,``,
    ``~`` … are fine: it only travels as one argv element, and every driver
    puts it into a prompt through ``native_args.prompt_path``, quoted unless
    it is plain ``[A-Za-z0-9._/+@-]``); control characters, newlines, NUL,
    line separators and format characters are refused."""
    problem = native_args.path_problem(str(path))
    if problem:
        raise FixRefused(f"the {what} path {display_name(str(path))} {problem}; "
                         "it would reach a role prompt — rename it to start a driver")


def display_name(text: str, limit: int = 200) -> str:
    """Display-only form of a repo-controlled name (a loop dir, a path):
    characters outside [A-Za-z0-9._/+@ -] become ``?``."""
    return re.sub(r"[^A-Za-z0-9._/+@ -]", "?", str(text))[:limit]


def native_resume_args(ctx: LoopContext) -> tuple[str, str, dict]:
    """(session_id, run_id, args) of a resumable native run, all validated;
    FixRefused with the reason otherwise (eval2 findings 1 and 4)."""
    native = ctx.native or {}
    errors = native.get("errors") or {}
    if not native.get("launch"):
        raise FixRefused("no .native-launch.json: nothing to resume")
    for key in ("session_id", "args", "run_id"):
        if key in errors:
            raise FixRefused(f"the recorded launch is not resumable ({errors[key]}); "
                             "use a fresh start")
    run_id = native.get("run_id")
    if not run_id:
        raise FixRefused("no workflow run id recorded for this session; use a fresh start")
    session_id, args = native.get("session_id"), native.get("args")
    if not session_id or not isinstance(args, dict):
        raise FixRefused("the recorded launch has no valid session id and args; use a fresh start")
    if "run_token" not in args:
        raise FixRefused("the recorded launch predates run tokens: a resume cannot tell its "
                         "own run from another; use a fresh start")
    return session_id, run_id, args


def _native_cmd(ctx: LoopContext, mode: str, max_iterations: int | None = None) -> tuple[list[str], Path]:
    """The installed release's launch.sh, and nothing a mailbox, the run
    registry or a result record names: no ``--helper`` is ever passed (the
    release's launch.sh uses its own helper). A resume runs only on a
    recorded session id, run id and args that pass the strict schema
    (``native_resume_args``); launch.sh re-validates them itself and
    rebuilds the prompt from the validated fields."""
    launcher = native_launcher(ctx.home)
    if launcher is None:
        raise FixRefused("the installed release has no native launcher (install a release, or "
                         "configure TRIO_DASH_RELEASE_NATIVE)")
    require_prompt_safe_path(ctx.live_mailbox)
    if mode == "resume":
        session_id, run_id, _args = native_resume_args(ctx)
        return (["bash", str(launcher), "resume", "--mailbox", str(ctx.live_mailbox),
                 "--run-id", run_id, "--session", session_id], ctx.live_repo)
    return (["bash", str(launcher), "start", "--mailbox", str(ctx.live_mailbox),
             "--max-iterations", str(max_iterations)], ctx.live_repo)


def native_launcher_note(ctx: LoopContext) -> str | None:
    """Display-only: the launcher a record says the last run used, when it is
    not the installed release's (it is never executed)."""
    recorded = []
    for source in ((ctx.registry or {}).get("launcher"), ((ctx.native or {}).get("result") or {}).get("launcher")):
        if isinstance(source, str) and source:
            recorded.append(source)
    release = native_launcher(ctx.home)
    for path in recorded:
        if release is None or not _same_file(path, release):
            return (f"the last run was launched with {path}; dashboard fixes always use the "
                    f"installed release's launcher ({release or 'none installed'})")
    return None


def _state_reset_step(ctx: LoopContext, extra: dict | None = None) -> dict:
    changes = {"status": "running", "phase": "idle", "reason": None}
    changes.update(extra or {})
    return {"kind": "state", "path": str(ctx.live_mailbox / "STATE.md"), "changes": changes,
            "display": "edit STATE.md: " + ", ".join(
                f"{k}: {ctx.state.get(k, '∅')} → {'(removed)' if v is None else v}"
                for k, v in changes.items() if ctx.state.get(k) != v)}


def _cmd_step(argv: list[str], cwd: Path, *, detached: bool, timeout: float = SYNC_TIMEOUT_SECONDS) -> dict:
    return {"kind": "run", "argv": argv, "cwd": str(cwd), "detached": detached, "timeout": timeout,
            "display": f"(cd {shlex.quote(str(cwd))} && {shlex.join(argv)})"}


def _product_dirty(repo: Path, mailbox_rel: str) -> list[str]:
    out = git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", ".",
              f":(exclude){mailbox_rel}").stdout
    return [line for line in out.splitlines() if line.strip()]


def _dangling_candidates(ctx: LoopContext) -> list[dict]:
    """Native dangling builder worktrees still registered in git."""
    repo = ctx.live_repo
    marker = str(repo) + "/.claude/worktrees/"
    listed = (ctx.native or {}).get("result", {}) or {}
    wanted = {p for p in listed.get("dangling_worktrees") or [] if isinstance(p, str)}
    out = []
    for tree in worktree_list(repo):
        path = tree.get("worktree")
        if not isinstance(path, str) or not path.startswith(marker) or path not in wanted:
            continue
        branch = tree.get("branch")
        branch = branch[len("refs/heads/"):] if isinstance(branch, str) and branch.startswith("refs/heads/") else None
        entry = {"path": path, "branch": branch, "ok": True, "reason": ""}
        try:
            dirty = git(Path(path), "status", "--porcelain=v1").stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            dirty = "?"
        if dirty:
            entry.update(ok=False, reason="has uncommitted or untracked files (a human reviews it)")
        elif branch:
            merged = git(repo, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD").returncode == 0
            if not merged:
                entry.update(ok=False, reason=f"branch {branch} is not merged into HEAD (unmerged work)")
        out.append(entry)
    return out


def plan_fix(ctx: LoopContext, fix_id: str, args: dict | None = None) -> dict:
    """Server-side plan of one allowlisted fix: preconditions checked now,
    exact steps and their display text. Raises FixRefused (with the reason)
    when the fix does not apply; KeyError for an id outside the allowlist."""
    if fix_id not in FIXES:
        raise KeyError(fix_id)
    args = dict(args or {})
    spec = FIXES[fix_id]
    _require_not_live(ctx)
    word = status_word(ctx.state)
    derived = ctx.derived["state"]
    verdict, scope = read_verdict(ctx.live_mailbox)
    holds = held_records(ctx.live_mailbox)
    steps: list[dict] = []
    native = ctx.driver == NATIVE_DRIVER
    notes: list[str] = []

    for name in ("STATE.md", "VERDICT.md"):
        mailbox_file(ctx.live_mailbox, name)  # PathEscape: never follow a symlink out
    if fix_id in ("rerun", "rerun_more_iterations", "reset_and_rerun", "land",
                  "reconcile_dry_run", "reconcile_apply") and native:
        raise FixRefused("this is a claude-workflow loop; use the native fixes")
    if fix_id.startswith("native_") and not native:
        raise FixRefused("not a claude-workflow (native) loop")

    if fix_id == "rerun":
        if word in ("shipped", "blocked", "needs_human", "error", "needs_land"):
            raise FixRefused(f"STATE.md status is {word}: " + {
                "shipped": "nothing to re-run",
                "blocked": "answer first (answer box), which resets STATE",
                "needs_human": "answer first (answer box), which resets STATE",
                "error": "use reset_and_rerun",
                "needs_land": "use land",
            }[word])
        if holds:
            raise FixRefused("held dispatch records exist; reconcile first")
        n = _max_iterations_arg(ctx, args, must_exceed=False)
        steps.append(_cmd_step(*_driver_cmd(ctx, n), detached=True))
    elif fix_id == "rerun_more_iterations":
        if derived != "iteration_cap":
            raise FixRefused("the loop did not stop at its iteration cap")
        n = _max_iterations_arg(ctx, args, must_exceed=True)
        steps.append(_cmd_step(*_driver_cmd(ctx, n), detached=True))
    elif fix_id == "reset_and_rerun":
        if word != "error":
            raise FixRefused(f"STATE.md status is {word or '—'}, not error")
        if holds:
            raise FixRefused("held dispatch records exist; reconcile first")
        n = _max_iterations_arg(ctx, args, must_exceed=False)
        steps.append(_state_reset_step(ctx))
        steps.append(_cmd_step(*_driver_cmd(ctx, n), detached=True))
    elif fix_id == "native_resume":
        if derived != "interrupted":
            raise FixRefused("resume is only for an interrupted run (killed mid-run); "
                             "after held/error/budget use a fresh start")
        steps.append(_cmd_step(*_native_cmd(ctx, "resume"), detached=True))
        session_id, run_id, rargs = native_resume_args(ctx)
        shown = json.dumps(rargs, separators=(",", ":"))
        steps[-1]["resume"] = {"session_id": session_id, "run_id": run_id, "args": rargs}
        steps[-1]["display"] += f"\n  # resumes session {session_id}, run {run_id}, validated args {shown}"
        notes.append(f"resume replays the validated workflow args {shown} in Claude session "
                     f"{session_id} (launch.sh re-validates them and rebuilds its prompt from them)")
    elif fix_id == "native_start":
        if word in ("shipped", "blocked", "needs_human", "error"):
            raise FixRefused(f"STATE.md status is {word}: " + (
                "nothing to run" if word == "shipped" else
                "use native_reset_and_start" if word == "error" else
                "answer first (answer box), which resets STATE"))
        must_exceed = derived == "iteration_cap"
        n = _max_iterations_arg(ctx, args, must_exceed=must_exceed)
        steps.append(_cmd_step(*_native_cmd(ctx, "start", n), detached=True))
    elif fix_id == "native_reset_and_start":
        if word != "error":
            raise FixRefused(f"STATE.md status is {word or '—'}, not error")
        n = _max_iterations_arg(ctx, args, must_exceed=False)
        steps.append(_state_reset_step(ctx))
        steps.append(_cmd_step(*_native_cmd(ctx, "start", n), detached=True))
    elif fix_id == "land":
        if word != "needs_land":
            raise FixRefused(f"STATE.md status is {word or '—'}, not needs_land")
        g = git_state(ctx.live_repo, ctx.live_mailbox)
        if g.get("merge_in_progress") or g.get("unmerged"):
            raise FixRefused("the Lead worktree has an unresolved merge ("
                             + ", ".join(g.get("unmerged") or ["MERGE_HEAD"])
                             + "): a human resolves land conflicts, never the dashboard")
        trioctl = trioctl_path(ctx.home)
        if trioctl is None:
            raise FixRefused("the installed trioctl is missing")
        steps.append(_cmd_step([str(trioctl), "omnigent", "land", "--mailbox", str(ctx.root_mailbox)],
                               ctx.repo_root, detached=True))
    elif fix_id in ("reconcile_dry_run", "reconcile_apply"):
        if not holds:
            raise FixRefused("no held dispatch record (.sessions/held-*.json)")
        trioctl = trioctl_path(ctx.home)
        if trioctl is None:
            raise FixRefused("the installed trioctl is missing")
        base = [str(trioctl), "omnigent", "reconcile", "--mailbox", str(ctx.root_mailbox), "--json"]
        if fix_id == "reconcile_dry_run":
            steps.append(_cmd_step(base + ["--dry-run"], ctx.repo_root, detached=False, timeout=120))
        else:
            decision = reconcile_decision(ctx, base)
            if decision.get("action") != "ready":
                raise FixRefused("the dry run is not receipt-proven (decision "
                                 f"{decision.get('action')}: {decision.get('code')}); a human resolves it")
            notes.append(f"dry run just now: {decision.get('action')} / {decision.get('code')}")
            steps.append(_cmd_step(base + ["--apply"], ctx.repo_root, detached=False, timeout=600))
    elif fix_id == "retire_ship":
        if word != "needs_retirement":
            raise FixRefused(f"STATE.md status is {word or '—'}, not needs_retirement")
        if verdict != "SHIP":
            raise FixRefused("VERDICT.md's first line is not VERDICT: SHIP")
        repo = ctx.live_repo
        try:
            rel = ctx.live_mailbox.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            raise FixRefused("the mailbox is not inside its git checkout")
        dirty = _product_dirty(Path(repo), rel)
        if dirty:
            raise FixRefused("product changes outside the mailbox need a human to attribute "
                             "(/trio-ship): " + "; ".join(dirty[:5]))
        if git(Path(repo), "diff", "--cached", "--name-only").stdout.strip():
            raise FixRefused("the index has staged changes")
        iteration = to_int(ctx.state.get("iteration")) or 0
        text = read_text(ctx.live_mailbox / "VERDICT.md") or ""
        if not re.search(r"^commit:\s*[0-9a-f]{7,40}\s*$", text, re.M):
            head = git(Path(repo), "rev-parse", "HEAD").stdout.strip()
            steps.append({"kind": "append", "path": str(mailbox_file(ctx.live_mailbox, "VERDICT.md")),
                          "text": f"commit: {head}\n",
                          "display": f"append 'commit: {head}' to {rel}/VERDICT.md (clean tree: HEAD)"})
        steps.append(_cmd_step(git_argv(Path(repo), "add", "--", rel), Path(repo), detached=False))
        steps.append(_cmd_step(git_argv(Path(repo), "commit", "-q", "-m",
                                        f"loop: iteration {iteration} — SHIP", "--", rel),
                               Path(repo), detached=False))
        notes.append("the retirement commit is made unsigned (commit.gpgSign=false, "
                     "gpg.program=/bin/false) and without repository hooks (core.hooksPath=/dev/null): "
                     "every dashboard git call runs under SAFE_GIT_CONFIG so no repository-"
                     "chosen program (signer, hook, fsmonitor) executes from the dashboard; "
                     "sign or amend it yourself if the remote requires signed commits")
        notes.append("then re-run the loop so the driver finalizes needs_retirement → shipped")
        files = _mailbox_file_set(Path(repo), rel)
        mailbox_files = {"count": len(files), "digest": hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()).hexdigest()}
        notes.append(f"the retirement commit stages the {len(files)} mailbox file(s) as they are "
                     "now; any change before the confirm needs a new preview")
    elif fix_id == "repair_scope":
        wanted = args.get("scope")
        if verdict != "ITERATE" or not scope or not scope.lower().startswith("local:"):
            raise FixRefused("VERDICT.md is not ITERATE scope=local:<paths>")
        if wanted is not None and wanted != scope:
            raise FixRefused(f"the named scope {wanted!r} is not the verdict's scope {scope!r}")
        repairs = to_int(read_text(ctx.live_mailbox / ".repairs")) or 0
        if not 1 <= repairs <= 2:
            raise FixRefused(f".repairs is {repairs}: the driver would not run a repair pass "
                             "(0 = Lead pass, >2 = forced full Lead iteration)")
        if word in ("shipped", "blocked", "needs_human", "error", "needs_land"):
            raise FixRefused(f"STATE.md status is {word}")
        n = (to_int(ctx.state.get("iteration")) or 0) + 1
        if native:
            steps.append(_cmd_step(*_native_cmd(ctx, "start", n), detached=True))
        else:
            steps.append(_cmd_step(*_driver_cmd(ctx, n), detached=True))
        notes.append(f"one repair pass on {scope} and its evaluation (max_iterations {n})")
    elif fix_id == "cleanup_worktrees":
        candidates = _dangling_candidates(ctx)
        requested = args.get("paths")
        if requested is not None and (not isinstance(requested, list)
                                      or not all(isinstance(p, str) for p in requested)):
            raise FixRefused("paths must be a list of worktree paths")
        ok = [c for c in candidates if c["ok"] and (requested is None or c["path"] in requested)]
        unknown = [p for p in requested or [] if p not in {c["path"] for c in candidates}]
        if unknown:
            raise FixRefused("not a dangling builder worktree of this loop: " + ", ".join(unknown))
        if not ok:
            raise FixRefused("no clean, merged dangling worktree to remove"
                             + ("; kept: " + "; ".join(f"{c['path']}: {c['reason']}"
                                                        for c in candidates) if candidates else ""))
        for c in ok:
            steps.append(_cmd_step(git_argv(ctx.live_repo, "worktree", "remove", c["path"]),
                                   ctx.live_repo, detached=False))
            if c["branch"]:
                steps.append(_cmd_step(git_argv(ctx.live_repo, "branch", "-d", c["branch"]),
                                       ctx.live_repo, detached=False))
        notes += [f"kept {c['path']}: {c['reason']}" for c in candidates if not c["ok"]]
    if native and fix_id.startswith("native_") or (native and fix_id == "repair_scope"):
        note = native_launcher_note(ctx)
        if note:
            notes.append(note)
    basis = plan_basis(ctx)
    if fix_id == "retire_ship":
        # `git add -- <mailbox>` commits whatever is there at confirm: the
        # token binds that exact file set (eval3 finding 10).
        basis["mailbox_files"] = mailbox_files
    return {
        "id": fix_id, "title": spec["title"], "destructive": spec["destructive"],
        "requires_confirm": spec["destructive"] or bool(spec.get("confirm")), "steps": steps,
        "commands_preview": [s["display"] for s in steps], "notes": notes,
        "basis": basis, "confirm_token": plan_token(ctx, fix_id, steps, basis),
    }


def _mailbox_file_set(repo: Path, rel: str) -> dict:
    """What ``git add -- <rel>`` would stage: every tracked or untracked,
    not ignored path under the mailbox with a digest of its content (a
    symlink: of its target text; never followed)."""
    try:
        out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                  "--", rel).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"error": "git ls-files failed"}
    files = {}
    for name in sorted({n for n in out.split("\0") if n}):
        path = Path(repo) / name
        try:
            st = os.lstat(path)
        except OSError:
            files[name] = "absent"
            continue
        if stat.S_ISLNK(st.st_mode):
            files[name] = "link:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        elif stat.S_ISREG(st.st_mode):
            raw = read_bytes_nofollow(path)
            files[name] = hashlib.sha256(raw or b"").hexdigest()
        else:
            files[name] = "other"
    return files


def _file_digest(mailbox: Path, name: str) -> str | None:
    text = _safe_read(mailbox, name)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
BASIS_SIDECARS = (".native-launch.json", ".native-result.json", ".session.json", ".repairs",
                  ".driver.json")


def _rev(repo: Path, ref: str) -> str | None:
    try:
        return git(repo, "rev-parse", "-q", "--verify", "--end-of-options",
                   ref + "^{commit}").stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def plan_basis(ctx: LoopContext) -> dict:
    """The state a plan was made from (eval findings 3 and eval2 6): the live
    checkout's HEAD, the root checkout's HEAD and the land target's sha when
    they differ (root-free loops), the STATE.md / VERDICT.md / HUMAN.md
    digests, the GOAL.md digest, the native sidecars, ``.repairs`` and the run-registry record.
    Computed once per LoopContext (a context is gathered per request)."""
    cached = getattr(ctx, "_plan_basis", None)
    if cached is not None:
        return dict(cached)
    head = _rev(ctx.live_repo, "HEAD")
    basis = {"head": head, "state": _file_digest(ctx.live_mailbox, "STATE.md"),
             "verdict": _file_digest(ctx.live_mailbox, "VERDICT.md"),
             "human": _file_digest(ctx.live_mailbox, "HUMAN.md"),
             # an answer's stop binding is computed at confirm (eval4 finding 6)
             "goal": _file_digest(ctx.live_mailbox, "GOAL.md")}
    if Path(ctx.repo_root) != Path(ctx.live_repo):
        basis["root_head"] = _rev(ctx.repo_root, "HEAD")
    target = str(ctx.state.get("target_ref") or "").strip()
    if target:
        basis["land_target"] = {"ref": display_name(target),
                                "sha": _rev(ctx.repo_root, target) if _REF_RE.fullmatch(target)
                                and ".." not in target else None}
    basis["sidecars"] = {name: _file_digest(ctx.live_mailbox, name) for name in BASIS_SIDECARS}
    holds = held_records(ctx.live_mailbox)
    if holds:
        # reconcile_apply acts on these records (eval3 finding 10).
        basis["held"] = {h["file"]: _file_digest(ctx.live_mailbox / ".sessions", h["file"])
                         for h in holds}
    if ctx.registry:
        basis["registry"] = hashlib.sha256(json.dumps(ctx.registry, sort_keys=True,
                                                      default=str).encode()).hexdigest()
    ctx._plan_basis = dict(basis)
    return basis


def plan_token(ctx: LoopContext, action_id: str, steps: list[dict], basis: dict,
               extra: dict | None = None) -> str:
    """hash(loop, action id, exact steps, basis): a confirm must present the
    token of the preview it saw; the server re-plans and compares, so a
    confirm never runs commands (or against a state) the human did not see."""
    payload = {
        "loop": ctx.key, "live_mailbox": str(ctx.live_mailbox), "action": action_id,
        "steps": [{k: s.get(k) for k in ("kind", "argv", "cwd", "path", "changes", "text", "resume")}
                  for s in steps],
        "basis": basis, "extra": extra or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def reconcile_decision(ctx: LoopContext, base: list[str]) -> dict:
    """Run the read-only reconcile dry run and return its decision."""
    try:
        proc = subprocess.run(base + ["--dry-run"], cwd=str(ctx.repo_root), capture_output=True,
                              text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
        payload = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"action": "error", "code": str(exc)}
    decision = payload.get("decision") if isinstance(payload, dict) else None
    return decision if isinstance(decision, dict) else {"action": "error", "code": "no decision"}


def available_fixes(ctx: LoopContext) -> list[dict]:
    """Every allowlisted fix with whether it applies now (and why not)."""
    out = []
    for fix_id in FIXES:
        if fix_id == "reconcile_apply":
            # Planning it runs the dry run; offer it when holds exist and
            # let the click re-check (never pre-run trioctl on a poll).
            if held_records(ctx.live_mailbox) and not (ctx.driver == NATIVE_DRIVER):
                out.append({"id": fix_id, "title": FIXES[fix_id]["title"], "applicable": True,
                            "destructive": True, "commands_preview": [
                                "trioctl omnigent reconcile --mailbox … --json --dry-run  (server check)",
                                "trioctl omnigent reconcile --mailbox … --json --apply  (only if ready)"],
                            "reason": "applies only if a fresh dry run is receipt-proven (ready)"})
            continue
        try:
            plan = plan_fix(ctx, fix_id, {})
            out.append({"id": fix_id, "title": plan["title"], "applicable": True,
                        "destructive": plan["destructive"],
                        "requires_confirm": plan["requires_confirm"],
                        "commands_preview": plan["commands_preview"], "notes": plan["notes"]})
        except FixRefused as exc:
            spec = FIXES[fix_id]
            out.append({"id": fix_id, "title": spec["title"], "applicable": False,
                        "destructive": spec["destructive"],
                        "requires_confirm": spec["destructive"] or bool(spec.get("confirm")),
                        "reason": str(exc)})
    return out


# --------------------------------------------------------------------------
# Execution and the action log
# --------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def loop_state_dir(home: Path, key: str) -> Path:
    path = state_dir(home) / "loops" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_action(home: Path, key: str, entry: dict) -> dict:
    """Append one JSON line to the loop's append-only action log."""
    entry = {"at": _now(), "id": uuid.uuid4().hex[:12], **entry}
    line = json.dumps(entry, sort_keys=True, default=str) + "\n"
    with _LOG_LOCK:
        path = loop_state_dir(home, key) / "actions.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    return entry


def read_actions(home: Path, key: str, limit: int = 50) -> list[dict]:
    path = state_dir(home) / "loops" / key / "actions.jsonl"
    out = []
    for line in (read_text(path) or "").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _write_atomic_nofollow(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace a file atomically: a fresh ``mkstemp`` file in the same
    directory (never a fixed temp name), then a rename over the target. A
    target that is a symlink or not a regular file is refused (the rename
    would replace the link, but such a file is never ours to replace)."""
    path = Path(path)
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is a symlink or not a regular file; refusing to replace it")
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def edit_state(path: Path, changes: dict) -> dict:
    """Rewrite STATE.md keys (None removes the key's line); refuses a
    symlinked STATE.md."""
    path = mailbox_file(Path(path).parent, Path(path).name)
    text = _safe_read(path.parent, path.name)
    if text is None and path.exists():
        raise OSError(f"cannot read {path} without following a link")
    lines = (text or "").splitlines()
    seen, before = set(), {}
    out = []
    for line in lines:
        m = _STATE_LINE.match(line)
        key = m.group(1).lower() if m else None
        if key in changes and key not in seen:
            seen.add(key)
            before[key] = m.group(2).strip()
            if changes[key] is None:
                continue
            out.append(f"{key}: {changes[key]}")
            continue
        out.append(line)
    for key, value in changes.items():
        if key not in seen and value is not None:
            out.append(f"{key}: {value}")
            before[key] = None
    _write_atomic_nofollow(path, ("\n".join(out) + "\n").encode("utf-8"))
    return {"before": before, "after": {k: v for k, v in changes.items()}}


def _preflight(step: dict) -> str | None:
    """Why a run step cannot start (None when it can): the executable (or the
    script ``bash`` runs) and the cwd must exist. Checked for every step
    before anything is changed (eval finding 11)."""
    argv = step.get("argv") or []
    if not argv:
        return "empty command"
    if not Path(step.get("cwd") or "").is_dir():
        return f"working directory {step.get('cwd')} is missing"
    exe = argv[0]
    if os.path.isabs(exe):
        if _executable(Path(exe)) is None:
            return f"{exe} is not an executable file"
    elif shutil.which(exe) is None:
        return f"{exe} is not on PATH"
    if os.path.basename(exe) == "bash" and len(argv) > 1 and not Path(argv[1]).is_file():
        return f"{argv[1]} is missing"
    return None


def execute_plan(ctx: LoopContext, plan: dict, *, who: dict, processes: dict | None = None,
                 on_exit=None) -> dict:
    """Run a planned fix's steps in order; stop at the first failure.

    Every run step is preflighted before anything changes. A STATE.md edit
    that precedes a driver start is rolled back (the exact previous bytes)
    when the driver does not start, so a failed start never leaves STATE
    ``running`` with its ``reason:`` gone; the previous reason is always in
    the action log. Detached steps (loop drivers, land) start in their own
    session with output to ``runs/<ts>-<fix>.log`` and must survive a short
    startup grace; their exit is appended to the action log by a reaper."""
    results = []
    ok = True
    state_before: dict[str, bytes] = {}
    state_after: dict[str, bytes] = {}
    reason_before = ctx.state.get("reason")
    for step in plan["steps"]:
        if step["kind"] == "run":
            problem = _preflight(step)
            if problem:
                results.append({"step": step["display"], "ok": False,
                                "error": "preflight: " + problem})
                ok = False
                break
    for step in plan["steps"] if ok else []:
        if step["kind"] == "state":
            try:
                raw = _safe_read(Path(step["path"]).parent, Path(step["path"]).name)
                if raw is not None:
                    state_before[step["path"]] = raw.encode("utf-8")
                results.append({"step": step["display"], "ok": True,
                                "change": edit_state(Path(step["path"]), step["changes"])})
                after = _safe_read(Path(step["path"]).parent, Path(step["path"]).name)
                if after is not None:
                    state_after[step["path"]] = after.encode("utf-8")
            except (OSError, FixRefused) as exc:
                results.append({"step": step["display"], "ok": False, "error": str(exc)})
                ok = False
                break
        elif step["kind"] == "append":
            try:
                append_nofollow(mailbox_file(Path(step["path"]).parent, Path(step["path"]).name),
                                step["text"])
                results.append({"step": step["display"], "ok": True})
            except (OSError, FixRefused) as exc:
                results.append({"step": step["display"], "ok": False, "error": str(exc)})
                ok = False
                break
        elif step["detached"]:
            restore = {path: (state_before[path], state_after[path]) for path in state_before
                       if path in state_after}
            res = _start_detached(ctx, plan["id"], step, who, processes, on_exit,
                                  restore=restore, reason=reason_before)
            results.append(res)
            if not res["ok"]:
                ok = False
                break
        else:
            try:
                proc = subprocess.run(step["argv"], cwd=step["cwd"], capture_output=True, text=True,
                                      timeout=step.get("timeout", SYNC_TIMEOUT_SECONDS),
                                      check=False, stdin=subprocess.DEVNULL)
                output = (proc.stdout + proc.stderr)[-4000:]
                results.append({"step": step["display"], "ok": proc.returncode == 0,
                                "exit_code": proc.returncode, "output": output})
                if proc.returncode != 0:
                    ok = False
                    break
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append({"step": step["display"], "ok": False, "error": str(exc)})
                ok = False
                break
    if not ok and state_before:
        for path, data in state_before.items():
            try:
                _write_atomic_nofollow(mailbox_file(Path(path).parent, Path(path).name), data)
                results.append({"step": f"restore {path} (the driver did not start)", "ok": True})
            except (OSError, FixRefused) as exc:
                results.append({"step": f"restore {path}", "ok": False, "error": str(exc)})
    entry = log_action(ctx.home, ctx.key, {
        "action": "fix", "fix": plan["id"], "who": who, "mailbox": str(ctx.root_mailbox),
        "live_mailbox": str(ctx.live_mailbox), "commands": plan["commands_preview"],
        "confirmed": bool(plan.get("confirmed")), "ok": ok, "results": results,
        "state_before": ctx.derived.get("state"), "reason": reason_before,
        "confirm_token": plan.get("confirm_token"), "basis": plan.get("basis"),
    })
    return {"ok": ok, "results": results, "log_id": entry["id"]}


CLAIM_FILES = (".lock/pid", ".lock/owner", ".session.json", ".driver.json")
"""Files a started driver writes when it takes the mailbox (its lock and its
session / driver record)."""
CLAIM_POLL_SECONDS = 0.25


def _claim_snapshot(mailbox: Path) -> dict:
    return {name: _file_digest(mailbox, name) for name in CLAIM_FILES}


def _restore_unclaimed(ctx: LoopContext, fix_id: str, restore: dict, reason) -> list[dict]:
    """Put STATE.md back byte for byte after a driver that exited nonzero
    without ever taking the mailbox; only when STATE.md is still exactly what
    the dashboard wrote (a driver that changed it owns it)."""
    out = []
    for path, (before, after) in restore.items():
        p = Path(path)
        current = _safe_read(p.parent, p.name)
        if current is None or current.encode("utf-8") != after:
            out.append({"path": path, "restored": False,
                        "why": "STATE.md changed after the reset (the driver owns it)"})
            continue
        try:
            _write_atomic_nofollow(mailbox_file(p.parent, p.name), before)
            out.append({"path": path, "restored": True})
        except (OSError, FixRefused) as exc:
            out.append({"path": path, "restored": False, "why": str(exc)})
    return out


def _start_detached(ctx: LoopContext, fix_id: str, step: dict, who: dict,
                    processes: dict | None, on_exit=None, *, restore: dict | None = None,
                    reason=None) -> dict:
    """Start a detached driver. It must survive LAUNCH_GRACE_SECONDS; after
    that a reaper keeps watching it until it has taken the mailbox (its lock
    or session/driver record changed) or exited. A driver that exits nonzero
    before taking the mailbox gets the dashboard's STATE.md edit rolled back
    (``restore``: {path: (before, after)} bytes), keeping ``reason:``
    (eval2 finding 7)."""
    runs = loop_state_dir(ctx.home, ctx.key) / "runs"
    runs.mkdir(exist_ok=True)
    log_path = runs / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{fix_id}.log"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    baseline = _claim_snapshot(ctx.live_mailbox)
    try:
        with open(log_path, "ab") as log:
            process = subprocess.Popen(step["argv"], cwd=step["cwd"], start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        return {"step": step["display"], "ok": False, "error": f"could not start: {exc}",
                "log": str(log_path)}
    try:
        code = process.wait(timeout=LAUNCH_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    if code is not None and code != 0:
        return {"step": step["display"], "ok": False, "exit_code": code, "pid": process.pid,
                "log": str(log_path), "output": (tail_text(log_path, 2000) or "")}
    if processes is not None:
        processes[process.pid] = process

    def claimed() -> bool:
        if _claim_snapshot(ctx.live_mailbox) != baseline:
            return True
        for path, (_before, after) in (restore or {}).items():
            p = Path(path)
            current = _safe_read(p.parent, p.name)
            if current is not None and current.encode("utf-8") != after:
                return True
        return False

    def reap() -> None:
        taken = not restore
        try:
            while True:
                try:
                    rc = process.wait(timeout=None if taken else CLAIM_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    taken = claimed()
        except Exception:  # noqa: BLE001 - best effort
            return
        if not taken:
            taken = claimed()
        if processes is not None:
            processes.pop(process.pid, None)
        restored = None
        if restore and rc != 0 and not taken:
            restored = _restore_unclaimed(ctx, fix_id, restore, reason)
        if on_exit is not None:
            try:
                on_exit(process.pid, rc)
            except Exception:  # noqa: BLE001 - logging must not fail
                pass
        entry = {"action": "fix-exit", "fix": fix_id, "pid": process.pid, "exit_code": rc,
                 "log": str(log_path), "output": tail_text(log_path, 1500),
                 "took_mailbox": taken}
        if restored is not None:
            entry.update(state_restored=restored, reason=reason)
        log_action(ctx.home, ctx.key, entry)

    if code is None:
        threading.Thread(target=reap, daemon=True).start()
    return {"step": step["display"], "ok": True, "pid": process.pid, "log": str(log_path),
            "exit_code": code, "detached": True}


# --------------------------------------------------------------------------
# Answer box (HUMAN.md)
# --------------------------------------------------------------------------

HUMAN_FILE = "HUMAN.md"
HUMAN_HEADER = (
    "# Human answers\n\n"
    "Append-only answers from a person to the loop, written by trio-dash's answer\n"
    "box. Each entry starts with a server-written header line\n"
    "`## <UTC time> — answer <id> — iteration <N> — trio-dash <sig>`; the answer\n"
    "text follows as `> `-quoted lines, so no answer text can start a header.\n"
    "Roles never act on this file directly: the loop driver verifies the newest\n"
    "entry against trio-dash's answer ledger and passes only a verified answer\n"
    "into the Lead / Evaluator prompt (\"## Verified human answer (driver)\").\n"
    "Agents never edit this file. Format: MAILBOX-SCHEMA.md \"HUMAN.md\".\n"
)
ANSWER_LIMIT = 20_000
LEDGER_PATH = Path(__file__).resolve().parent.parent / "metrics" / "human_ledger.py"
_LEDGER_MODULE: dict = {}


class AnswerKeyUnusable(FixRefused):
    """The answer key cannot be used (corrupt, empty, symlink, too open):
    answers are refused with the reason, never signed with a bad key."""


def ledger():
    """metrics/human_ledger.py of this release (shared with the drivers)."""
    module = _LEDGER_MODULE.get("m")
    if module is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("trio_dash_human_ledger", LEDGER_PATH)
        if spec is None or spec.loader is None:
            raise FixRefused(f"the answer ledger module is missing ({LEDGER_PATH})")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (OSError, SyntaxError) as exc:
            raise FixRefused(f"the answer ledger module is unusable ({LEDGER_PATH}: {exc})") from None
        _LEDGER_MODULE["m"] = module
    return module


#: Config overrides for every dashboard git call: the release's single source
#: of truth, metrics/human_ledger.SAFE_GIT_CONFIG (no fsmonitor, hook, gpg,
#: pager, ssh, askpass or credential program; no implicit bare repository).
SAFE_GIT_CONFIG: tuple = tuple(ledger().SAFE_GIT_CONFIG)


def _answer_key(home: Path, *, create: bool = False) -> bytes:
    """The dashboard's HMAC key (``answer-key`` in its state dir, 0600).
    Generated securely when missing (``create``); a corrupt, empty, linked
    or group-readable key raises AnswerKeyUnusable (eval2 finding 3)."""
    lg = ledger()
    try:
        return lg.load_key(state_dir(home), create=create)
    except lg.AnswerKeyError as exc:
        raise AnswerKeyUnusable(str(exc)) from None


def _entry_sig(home: Path, at: str, answer_id: str, iteration, body: str) -> str:
    return ledger().entry_sig(_answer_key(home, create=True), at, answer_id, iteration, body)


def quote_body(text: str) -> str:
    """Every answer line quoted (``> ``): the answer can never forge a header
    (the ledger's canonical form: every line separator is a line break)."""
    return ledger().quote_body(text)


def human_entries(home: Path, mailbox: Path) -> list[dict]:
    """Server-written HUMAN.md entries, oldest first, each with ``verified``:
    its header signature matches AND the dashboard's answer ledger holds a
    record (valid MAC, this mailbox) with its id, time, iteration and text
    digest — the same check the loop drivers make. ``key_error`` is set
    when the key is unusable (every entry is then unverified)."""
    try:
        lg = ledger()
    except FixRefused:
        return []
    text = _safe_read(mailbox, HUMAN_FILE) or ""
    entries = lg.parse_entries(text)
    key_error = None
    consumed: set = set()
    try:
        key = _answer_key(home)
        records = lg.read_records(state_dir(home))
        consumed = lg.consumed_macs(state_dir(home))
    except AnswerKeyUnusable as exc:
        key, records, key_error = None, [], str(exc)
    except OSError as exc:
        key, records, key_error = None, [], f"answer ledger unreadable: {exc}"
    real = os.path.realpath(mailbox)
    out = []
    for e in entries:
        verified = bool(key) and lg.entry_verified(key, e, records, real)
        used = verified and any(r.get("id") == e["id"] and r.get("mac") in consumed
                                for r in records)
        out.append({"at": e["at"], "id": e["id"], "iteration": e["iteration"],
                    "verified": verified, "consumed": used, "header": e["header"],
                    "key_error": key_error})
    return out


def answer_context(ctx: LoopContext) -> dict:
    """Whether the answer box applies now, and what it would change."""
    word = status_word(ctx.state)
    verdict, _ = read_verdict(ctx.live_mailbox)
    stop = None
    if word in ("needs_human", "blocked"):
        stop = f"STATE.md status {word}"
    elif ctx.derived["state"] in ("needs_human", "blocked"):
        stop = f"{ctx.derived['state']} ({ctx.derived.get('summary')})"
    elif verdict in ("NEEDS_HUMAN", "BLOCKED") and word not in RUNNING_WORDS:
        stop = f"VERDICT.md {verdict}"
    holds = held_records(ctx.live_mailbox)
    reason = None
    try:
        mailbox_file(ctx.live_mailbox, HUMAN_FILE)
    except PathEscape as exc:
        reason = str(exc)
    if reason is None and ctx.live:
        reason = "the loop is live; answers are only written to a stopped loop"
    elif reason is None and stop is None:
        reason = "the loop is not stopped at NEEDS_HUMAN or BLOCKED"
    reset_reason = None
    if holds:
        reset_reason = ("held dispatch records exist: reconcile them first "
                        "(the answer is still recorded, STATE is not reset)")
    entries = human_entries(ctx.home, ctx.live_mailbox)
    key_error = None
    try:
        _answer_key(ctx.home)
    except AnswerKeyUnusable as exc:
        if "is missing" not in str(exc):
            key_error = str(exc)
    except FixRefused as exc:
        key_error = str(exc)
    if reason is None and key_error:
        reason = "answers cannot be signed: " + key_error
    return {"allowed": reason is None, "reason": reason, "stop": stop, "key_error": key_error,
            "reset_allowed": reset_reason is None, "reset_reason": reset_reason,
            "path": str(ctx.live_mailbox / HUMAN_FILE),
            "entries": [f"{e['at']} — answer {e['id']} — iteration {e['iteration']}"
                        + ("" if e["verified"] else " (UNVERIFIED: not written by this dashboard)")
                        + (" (consumed: delivered to the Evaluator that ruled on it)"
                           if e.get("consumed") else "")
                        for e in entries[-5:]],
            "verdict": verdict, "iteration": to_int(ctx.state.get("iteration"))}


def plan_answer(ctx: LoopContext, text: str, reset: bool, who: dict) -> dict:
    info = answer_context(ctx)
    if not info["allowed"]:
        raise FixRefused(info["reason"])
    _require_not_live(ctx)
    if not isinstance(text, str) or not text.strip():
        raise FixRefused("the answer is empty")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise FixRefused("the answer is not valid Unicode text (it contains a lone "
                         "surrogate)") from None
    if len(text) > ANSWER_LIMIT:
        raise FixRefused(f"the answer is longer than {ANSWER_LIMIT} characters")
    if reset and not info["reset_allowed"]:
        raise FixRefused(info["reset_reason"])
    mailbox_file(ctx.live_mailbox, "STATE.md")
    at = _now()
    lg = ledger()
    # Canonical text (every line separator a "\n"): what is signed is what
    # the HUMAN.md parser reads back (eval3 finding 5).
    body = lg.canonical_text(text).strip()
    if not body:
        raise FixRefused("the answer is empty")
    answer_id = hashlib.sha256((body + at + uuid.uuid4().hex).encode()).hexdigest()[:12]
    iteration = info["iteration"] if info["iteration"] is not None else "?"
    by = re.sub(r"[\r\n]+", " ", str(who.get("user") or who.get("addr") or "unknown"))[:120]
    key = _answer_key(ctx.home, create=True)
    sig = lg.entry_sig(key, at, answer_id, iteration, body)
    # The record binds the exact stop it answers (GOAL, the VERDICT.md on
    # disk, its last commit, HEAD): a driver passes it on only while that
    # stop is still the current one, and only once (eval3 finding 1).
    try:
        binding = lg.stop_binding(ctx.live_mailbox)
    except OSError as exc:
        raise FixRefused(f"the stop cannot be read safely: {exc}") from None
    record = lg.make_record(key, answer_id=answer_id, loop=ctx.key, mailbox=ctx.live_mailbox,
                            root_mailbox=ctx.root_mailbox, iteration=iteration, at=at, body=body,
                            binding=binding)
    entry = (f"\n## {at} — answer {answer_id} — iteration {iteration} — trio-dash {sig}\n"
             f"in-reply-to: {info['stop']} (iteration {iteration})\n"
             f"source: trio-dash ({by})\n\n{quote_body(body)}")
    steps = [{"kind": "append", "path": info["path"], "text": entry,
              "display": f"append answer {answer_id} to {info['path']}"}]
    if reset:
        steps.append(_state_reset_step(ctx, {"human_answer": f"HUMAN.md#{answer_id} ({at})"}))
    basis = plan_basis(ctx)
    # The token binds the text, reset choice and state, not the entry's
    # timestamp/id (a confirm writes a fresh entry for the same answer).
    token = plan_token(ctx, "answer", [{"kind": "answer", "text": body},
                                       {"kind": "reset", "changes": reset}], basis)
    return {"id": "answer", "answer_id": answer_id, "entry": entry, "steps": steps,
            "ledger_record": record,
            "commands_preview": [s["display"] for s in steps], "destructive": reset,
            "requires_confirm": True, "reset": reset, "basis": basis, "confirm_token": token}


def execute_answer(ctx: LoopContext, plan: dict, who: dict) -> dict:
    results = []
    try:
        path = mailbox_file(ctx.live_mailbox, HUMAN_FILE)
        # The ledger record first: a HUMAN.md entry that exists is always
        # verifiable; a failed HUMAN.md append leaves only an unused record.
        ledger().append_record(state_dir(ctx.home), plan["ledger_record"])
        append_nofollow(path, plan["entry"], header=HUMAN_HEADER)
        results.append({"step": plan["steps"][0]["display"], "ok": True})
        for step in plan["steps"][1:]:
            results.append({"step": step["display"], "ok": True,
                            "change": edit_state(Path(step["path"]), step["changes"])})
        ok = True
    except (OSError, FixRefused) as exc:
        results.append({"ok": False, "error": str(exc)})
        ok = False
    entry = log_action(ctx.home, ctx.key, {
        "action": "answer", "who": who, "answer_id": plan["answer_id"], "reset": plan["reset"],
        "mailbox": str(ctx.live_mailbox), "ok": ok, "results": results,
        "chars": len(plan["entry"]), "reason": ctx.state.get("reason"),
        "confirm_token": plan.get("confirm_token")})
    return {"ok": ok, "results": results, "answer_id": plan["answer_id"], "log_id": entry["id"]}


# --------------------------------------------------------------------------
# Diagnosis (read-only agent, never Claude)
# --------------------------------------------------------------------------

DIAGNOSIS_KEYS = ("diagnosis", "state", "evidence", "proposed_fix", "needs_human_input")


def build_context(ctx: LoopContext) -> dict:
    """What the diagnosis agent gets: mailbox files, driver facts,
    liveness, git state, the unblock table and the fix allowlist."""
    box = ctx.live_mailbox
    files = {}
    for name, limit, tail in (("GOAL.md", 6000, False), ("STATE.md", 4000, False),
                              ("VERDICT.md", 10000, False), ("PLAN.md", 10000, False),
                              ("REPORT.md", 6000, False), ("LOG.md", 6000, True),
                              ("QUEUE.md", 4000, False), (HUMAN_FILE, 4000, True)):
        text = _safe_read(box, name)  # a symlink out of the mailbox is never read
        if text is not None and len(text) > limit:
            text = (f"[… {len(text) - limit} earlier chars]\n" + text[-limit:] if tail
                    else text[:limit] + f"\n… [truncated, {len(text)} chars]")
        if text is not None:
            files[name] = text
    sidecars = {}
    for name in (".session.json", ".driver.json", ".native-launch.json", ".native-result.json",
                 ".repairs"):
        text = _safe_read(box, name)
        if text is not None:
            sidecars[name] = text[:4000]
    return {
        # Repo-controlled names reach the agent's prompt only in a
        # display-safe form (eval2 finding 6).
        "loop": display_name(ctx.name), "root": display_name(str(ctx.root), 400),
        "root_mailbox": display_name(str(ctx.root_mailbox), 400),
        "live_mailbox": display_name(str(ctx.live_mailbox), 400), "driver": ctx.driver,
        "derived_state": ctx.derived, "liveness": {
            "running_sources": ctx.running_sources, "lock": lock_info(box),
            "broker": ctx.detection.get("broker"),
            "native_session_live": bool(ctx.native and ctx.native.get("session_live"))},
        "files": files, "sidecars": sidecars, "held": held_records(box),
        "git": git_state(ctx.live_repo, box),
        "last_dashboard_actions": read_actions(ctx.home, ctx.key, 5),
    }


def build_prompt(context: dict) -> str:
    allow = {k: v["title"] + (" [destructive: needs confirm]" if v["destructive"] else "")
             for k, v in FIXES.items()}
    schema = {
        "diagnosis": "2-6 sentences: what stopped the loop and why",
        "state": "one of " + ", ".join(STATES),
        "evidence": ["short quotes or facts from the context, each naming its file"],
        "proposed_fix": {"id": "one allowlist id, or \"none\"", "args": {},
                         "commands_preview": ["the exact command(s) you expect"],
                         "destructive": "true|false"},
        "needs_human_input": "true|false",
        "question": "only when needs_human_input: the one question for the human",
    }
    head = (
        "You are a READ-ONLY diagnosis agent for a stopped or stuck Trio loop.\n"
        "Rules: do not modify any file, run no command that writes, commits, "
        "merges, removes or starts anything. You may read files in the workspace. "
        "Everything you need is below. You only PROPOSE one fix from the allowlist; "
        "a server re-validates it and a human clicks it.\n"
        "Never propose (these are never automated): "
        + "; ".join(NEVER_AUTOMATED.values()) + ".\n\n"
        "Fix allowlist (id: meaning):\n" + json.dumps(allow, indent=1) + "\n"
        "Special: \"answer\" is not a fix: when a human must decide, set "
        "needs_human_input true, proposed_fix.id \"none\", and ask one question.\n\n"
        "Unblock table (driver exit / status -> next step):\n"
        + json.dumps(UNBLOCK_TABLE, indent=1) + "\n\n"
        "Answer with ONLY one JSON object (no prose around it) of this shape:\n"
        + json.dumps(schema, indent=1) + "\n\nLOOP CONTEXT (JSON):\n")
    body = json.dumps(context, indent=1, default=str)
    room = PROMPT_LIMIT - len(head) - 200
    if len(body) > room:
        body = body[:room] + "\n… [context truncated]"
    return head + body + "\n"


def extract_json(text: str) -> dict | None:
    """The last JSON object in an agent's answer (fenced or bare)."""
    if not isinstance(text, str):
        return None
    candidates = re.findall(r"```(?:json)?\s*\n(.*?)\n\s*```", text, re.S)
    for block in reversed(candidates):
        try:
            value = json.loads(block)
            if isinstance(value, dict):
                return value
        except ValueError:
            continue
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(value, dict) and any(k in value for k in DIAGNOSIS_KEYS):
            found = value
    return found


def validate_diagnosis(raw: dict | None) -> dict:
    """Normalize an agent's JSON; unknown or forbidden fix ids are marked
    rejected (the proposal is kept for the record, never applied)."""
    if not isinstance(raw, dict):
        return {"valid": False, "error": "no JSON object in the agent's answer"}
    out = {
        "valid": True,
        "diagnosis": str(raw.get("diagnosis") or "")[:4000],
        "state": str(raw.get("state") or "unknown")[:40],
        "evidence": [str(e)[:500] for e in (raw.get("evidence") or []) if e is not None][:20]
        if isinstance(raw.get("evidence"), list) else [str(raw.get("evidence"))[:500]],
        "needs_human_input": bool(raw.get("needs_human_input")),
        "question": str(raw.get("question"))[:2000] if raw.get("question") else None,
    }
    if out["state"] not in STATES:
        out["state_note"] = "not a known state"
    fix = raw.get("proposed_fix") if isinstance(raw.get("proposed_fix"), dict) else {}
    fid = str(fix.get("id") or "none")
    proposed = {
        "id": fid,
        "args": fix.get("args") if isinstance(fix.get("args"), dict) else {},
        "commands_preview": [str(c)[:500] for c in fix.get("commands_preview") or []][:10]
        if isinstance(fix.get("commands_preview"), list) else [],
        "destructive": bool(fix.get("destructive")),
    }
    if fid in NEVER_AUTOMATED:
        proposed["rejected"] = "never automated: " + NEVER_AUTOMATED[fid]
    elif fid != "none" and fid not in FIXES:
        proposed["rejected"] = "not in the fix allowlist"
    elif fid in FIXES and proposed["destructive"] != FIXES[fid]["destructive"]:
        proposed["destructive"] = FIXES[fid]["destructive"]
        proposed["destructive_note"] = "corrected from the server's allowlist"
    out["proposed_fix"] = proposed
    missing = [k for k in DIAGNOSIS_KEYS if k not in raw]
    if missing:
        out["missing_keys"] = missing
    return out


CURSOR_ISOLATED_CONFIG = {
    "version": 1,
    "permissions": {"allow": [], "deny": ["Shell(*)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"]},
    "approvalMode": "allowlist",
}
"""cli-config.json of the isolated Cursor config dir: nothing pre-approved."""


def cursor_isolation(home: Path) -> tuple[dict, Path]:
    """(env overrides, isolation dir) of a Cursor diagnosis run.

    Cursor reads MCP servers (``~/.cursor/mcp.json``), plugins, per-project
    MCP approvals and its permission allowlist from ``$HOME/.cursor`` and
    ``CURSOR_CONFIG_DIR``; the run gets a fresh, empty HOME, config and data
    dir under the dashboard's state dir. Authentication is the CLI's own: it
    reads ``$XDG_CONFIG_HOME/cursor/auth.json``, so XDG_CONFIG_HOME keeps
    pointing at the real ``~/.config`` (the dashboard never reads or copies
    the token). Verified (cursor-agent 2026.09.28, FIX-REPORT.md):
    ``cursor-agent mcp list`` → "No MCP servers configured", ``status`` →
    logged in, and in one real run the user's railway/notion/opendesign/github
    servers were gone and a write was "Blocked by permissions configuration".
    NOT removable: plugins synced from the Cursor account (their MCP servers
    load again) and the built-in WebFetch/WebSearch/Task/dynamic tools —
    hence Codex is the default and Cursor carries ``CURSOR_WARNING``."""
    base = state_dir(home) / "cursor-isolated"
    fake_home, config, data = base / "home", base / "config", base / "data"
    for path in (fake_home, config, data):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = config / "cli-config.json"
    current = read_json(cfg) or {}
    if current.get("permissions") != CURSOR_ISOLATED_CONFIG["permissions"] \
            or current.get("approvalMode") != "allowlist":
        current.update(CURSOR_ISOLATED_CONFIG)
        _write_atomic_nofollow(cfg, json.dumps(current, indent=1).encode("utf-8"), 0o600)
    stray = fake_home / ".cursor" / "mcp.json"
    if stray.exists() or stray.is_symlink():
        stray.unlink()  # never a user MCP config in the isolated home
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path(home) / ".config")
    env = {"HOME": str(fake_home), "XDG_CONFIG_HOME": xdg, "CURSOR_CONFIG_DIR": str(config),
           "CURSOR_DATA_DIR": str(data)}
    return env, base


def harness_command(harness: str, cfg: dict, repo: Path, prompt: str, last_path: Path,
                    home: Path | None = None) -> tuple[list[str], str | None, dict]:
    """argv, stdin and env overrides of one read-only diagnosis run."""
    if harness == "cursor":
        env = cursor_isolation(home)[0] if home is not None else {}
        return ([cfg["bin"], "-p", "--mode", "ask", "--output-format", "stream-json",
                 "--model", cfg["model"], "--workspace", str(repo), "--trust",
                 "--sandbox", "enabled", prompt], None, env)
    if harness == "codex":
        return ([cfg["bin"], "exec", "-m", cfg["model"],
                 "-c", f'model_reasoning_effort="{cfg["effort"]}"',
                 "-s", "read-only", "-c", 'approval_policy="never"',
                 "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                 "--ignore-rules",
                 "-C", str(repo), "--json", "-o", str(last_path), "-"], prompt, {})
    raise ValueError(f"unknown harness {harness}")


SNAPSHOT_FILE_LIMIT = 5000


def _snapshot_files(ctx: LoopContext) -> dict:
    """Integrity snapshot: every file under the live mailbox (recursively,
    symlinks recorded as links, never followed), the checkout's HEAD, index
    (``git ls-files -s`` + the index file's digest) and full status including
    ignored files (eval finding 10)."""
    snap: dict[str, str] = {}
    box = ctx.live_mailbox
    count = 0
    for dirpath, dirnames, filenames in os.walk(box, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames) + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, box)
            count += 1
            if count > SNAPSHOT_FILE_LIMIT:
                snap["<truncated>"] = str(count)
                break
            try:
                if os.path.islink(path):
                    snap[rel] = "link:" + os.readlink(path)
                    continue
                st = os.stat(path)
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(chunk)
                snap[rel] = f"{digest.hexdigest()}:{st.st_mode:o}"
            except OSError as exc:
                snap[rel] = "error:" + type(exc).__name__
    repo = ctx.live_repo
    try:
        snap["<git HEAD>"] = git(repo, "rev-parse", "HEAD").stdout.strip()
        snap["<git index>"] = hashlib.sha256(git(repo, "ls-files", "-s").stdout.encode()).hexdigest()
        index_path = git(repo, "rev-parse", "--git-path", "index").stdout.strip()
        if index_path:
            full = Path(index_path) if os.path.isabs(index_path) else Path(repo) / index_path
            try:
                snap["<git index file>"] = hashlib.sha256(full.read_bytes()).hexdigest()
            except OSError:
                pass
        snap["<git status>"] = hashlib.sha256(git(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignored").stdout.encode()).hexdigest()
        snap["<git diff>"] = hashlib.sha256(git(
            repo, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--binary").stdout.encode()).hexdigest()
        snap["<git refs>"] = hashlib.sha256(git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout.encode()).hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return snap


MAX_CONCURRENT_DIAGNOSES = 2
"""Diagnoses running at once across all loops (cost cap); more get 429."""


class DiagnosisBusy(FixRefused):
    """Too many diagnoses are running (HTTP 429)."""


class CursorExposure(FixRefused):
    """Cursor was requested without accepting ``CURSOR_WARNING`` (HTTP 409
    with ``accept_exposure_required``)."""


class DiagnosisManager:
    """One diagnosis per loop at a time; results persisted per loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running: dict[str, dict] = {}

    def status(self, home: Path, key: str) -> dict | None:
        with self._lock:
            live = self._running.get(key)
            if live is not None:
                return dict(live)
        return read_json(state_dir(home) / "loops" / key / "diagnosis.json")

    def _save(self, home: Path, key: str, record: dict) -> None:
        path = loop_state_dir(home, key) / "diagnosis.json"
        _write_atomic_nofollow(path, json.dumps(record, indent=1, sort_keys=True,
                                                default=str).encode("utf-8"), 0o600)

    def start(self, ctx: LoopContext, harness: str, who: dict, *,
              accept_exposure: bool = False) -> dict:
        catalog = harnesses(ctx.home)
        if harness not in ("cursor", "codex"):
            raise FixRefused("harness must be cursor or codex")
        cfg = catalog[harness]
        if not cfg["available"]:
            raise FixRefused(f"{harness} CLI not found")
        if harness == "cursor" and not accept_exposure:
            raise CursorExposure(CURSOR_WARNING)
        for err in cfg.get("config_errors") or []:
            raise FixRefused("diagnosis configuration refused: " + err)
        limit = MAX_CONCURRENT_DIAGNOSES
        try:
            limit = max(1, int(os.environ.get("TRIO_DASH_MAX_DIAGNOSES", "") or limit))
        except ValueError:
            pass
        with self._lock:
            if ctx.key in self._running:
                raise FixRefused("a diagnosis is already running for this loop")
            if len(self._running) >= limit:
                raise DiagnosisBusy(f"{len(self._running)} diagnoses are already running "
                                    f"(limit {limit}); try again when one finishes")
            record = {
                "id": uuid.uuid4().hex[:12], "status": "running", "harness": harness,
                "model": cfg["model"], "effort": cfg.get("effort"), "started_at": _now(),
                "who": who, "events": 0, "last_event": "starting", "loop": ctx.name,
                "loop_live": ctx.live,
                "warning": cfg.get("warning"),
            }
            self._running[ctx.key] = record
        self._save(ctx.home, ctx.key, record)
        log_action(ctx.home, ctx.key, {"action": "diagnose", "who": who, "harness": harness,
                                       "model": cfg["model"], "diagnosis_id": record["id"]})
        thread = threading.Thread(target=self._run, args=(ctx, harness, cfg, record), daemon=True)
        thread.start()
        return dict(record)

    def _update(self, ctx: LoopContext, record: dict, **fields) -> None:
        with self._lock:
            record.update(fields)
        self._save(ctx.home, ctx.key, record)

    def _run(self, ctx: LoopContext, harness: str, cfg: dict, record: dict) -> None:
        base = loop_state_dir(ctx.home, ctx.key)
        out_path = base / f"diagnosis-{record['id']}.out"
        last_path = base / f"diagnosis-{record['id']}.last"
        final_text = ""
        try:
            context = build_context(ctx)
            prompt = build_prompt(context)
            before = _snapshot_files(ctx)
            argv, stdin_text, env_over = harness_command(harness, cfg, ctx.live_repo, prompt,
                                                         last_path, ctx.home)
            timeout = float(os.environ.get("TRIO_DASH_DIAGNOSE_TIMEOUT") or DIAGNOSE_TIMEOUT_SECONDS)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1", **env_over)
            proc = subprocess.Popen(argv, cwd=str(ctx.live_repo), stdin=subprocess.PIPE if stdin_text
                                    else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env,
                                    start_new_session=True)
            if stdin_text:
                threading.Thread(target=_feed, args=(proc, stdin_text), daemon=True).start()
            err_chunks: list[str] = []
            threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True).start()
            deadline = time.monotonic() + timeout
            events = 0
            with open(out_path, "w", encoding="utf-8") as out:
                for line in proc.stdout:
                    out.write(line)
                    events += 1
                    summary, text = _event_summary(harness, line)
                    if text:
                        final_text = text
                    if summary:
                        self._update(ctx, record, events=events, last_event=summary[:200])
                    if time.monotonic() > deadline:
                        _kill(proc)
                        raise TimeoutError(f"diagnosis exceeded {int(timeout)} s")
            rc = proc.wait(timeout=30)
            if harness == "codex":
                final_text = read_text(last_path) or final_text
            if rc != 0 and not final_text:
                raise RuntimeError(f"{harness} exited {rc}: " + "".join(err_chunks)[-800:])
            parsed = validate_diagnosis(extract_json(final_text))
            after = _snapshot_files(ctx)
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            integrity = {"changed": changed, "checked": not ctx.live}
            if changed and not ctx.live:
                integrity["warning"] = ("files changed while the read-only diagnosis ran "
                                        "(another process, or the agent broke read-only)")
            self._update(ctx, record, status="done" if parsed.get("valid") else "failed",
                         finished_at=_now(), exit_code=rc, result=parsed,
                         error=None if parsed.get("valid") else parsed.get("error"),
                         answer_tail=final_text[-3000:], integrity=integrity,
                         raw=str(out_path), last_event="finished")
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            self._update(ctx, record, status="failed", finished_at=_now(), error=str(exc)[:2000],
                         answer_tail=final_text[-2000:], raw=str(out_path))
        finally:
            with self._lock:
                self._running.pop(ctx.key, None)
            log_action(ctx.home, ctx.key, {
                "action": "diagnose-done", "diagnosis_id": record["id"], "status": record.get("status"),
                "proposed_fix": ((record.get("result") or {}).get("proposed_fix") or {}).get("id"),
                "error": record.get("error")})


def _feed(proc, text: str) -> None:
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (OSError, ValueError):
        pass


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def _event_summary(harness: str, line: str) -> tuple[str | None, str | None]:
    """(progress text, final-answer text) of one stdout line."""
    try:
        event = json.loads(line)
    except ValueError:
        return (line.strip()[:120] or None), None
    if not isinstance(event, dict):
        return None, None
    if harness == "cursor":
        kind = event.get("type")
        if kind == "system":
            return f"started ({event.get('model')})", None
        if kind == "tool_call" and event.get("subtype") == "started":
            call = event.get("tool_call") or {}
            name = next(iter(call), "tool") if isinstance(call, dict) else "tool"
            return f"tool: {name}", None
        if kind == "assistant":
            parts = ((event.get("message") or {}).get("content") or [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            return ("writing answer" if text else None), None
        if kind == "result":
            return "result", str(event.get("result") or "")
        return None, None
    item = event.get("item") or {}
    kind = event.get("type")
    if kind == "item.started" and item.get("type") == "command_execution":
        return f"command: {str(item.get('command'))[:100]}", None
    if kind == "item.completed" and item.get("type") == "agent_message":
        return "answer", str(item.get("text") or "")
    if kind in ("thread.started", "turn.started", "turn.completed"):
        return kind, None
    return None, None

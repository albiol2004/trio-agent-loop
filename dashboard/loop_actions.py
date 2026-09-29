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
import subprocess
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

def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_text(path: Path, limit: int | None = None) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
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
    if not lock.is_dir():
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


def native_facts(mailbox: Path, home: Path | None = None) -> dict | None:
    """The claude-workflow driver's files in a mailbox, or None."""
    mailbox = Path(mailbox)
    session = read_json(mailbox / ".session.json")
    launch = read_json(mailbox / ".native-launch.json")
    result = read_json(mailbox / ".native-result.json")
    is_native = bool(
        (session and session.get("driver") == NATIVE_DRIVER)
        or launch or (result and result.get("driver", NATIVE_DRIVER) == NATIVE_DRIVER))
    if not is_native:
        return None
    if (not result or result.get("source") == "end") and launch:
        recovered = result_from_raw(mailbox, launch)
        if recovered is not None:
            if result:  # keep the end op's lock/dangling facts
                recovered.setdefault("dangling_worktrees", result.get("dangling_worktrees"))
            result = recovered
    args = {}
    if launch and isinstance(launch.get("args"), str):
        try:
            args = json.loads(launch["args"])
        except ValueError:
            args = {}
    session_live = bool(session and session.get("driver") == NATIVE_DRIVER
                        and not session.get("done") and pid_alive(session.get("pid")))
    lock = lock_info(mailbox)
    session_id = (launch or {}).get("session_id") or (result or {}).get("session_id")
    run_id = (result or {}).get("run_id")
    if home is not None and session_id and not run_id:
        run_id = find_run_id(home, session_id)
    return {
        "session": session, "launch": launch, "result": result, "args": args,
        "lock": lock, "session_live": session_live,
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


def _same_file(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Git helpers (read-only unless a fix says otherwise)
# --------------------------------------------------------------------------

def git(cwd: Path, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
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
            out["detail"]["resumable"] = bool(native.get("run_id") and native.get("launch"))
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
    "native_resume": {"title": "Resume the claude-workflow run (launch.sh resume)", "destructive": False},
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


def _lead_worktree_roots(root: Path) -> list[Path]:
    """Real paths of the git worktrees of the workspace's repository (root-free
    loops keep their live mailbox in a Lead worktree outside the root)."""
    top = git_toplevel(root)
    if top is None:
        return []
    out = []
    for tree in worktree_list(top):
        path = tree.get("worktree")
        if isinstance(path, str) and path:
            try:
                out.append(Path(path).resolve())
            except OSError:
                continue
    return out


def check_mailbox_paths(root: Path, root_mailbox: Path, live_mailbox: Path) -> tuple[Path, Path]:
    """(real root mailbox, real live mailbox), or PathEscape.

    The root mailbox must resolve inside the resolved workspace root; the
    live copy inside the root or inside one of the repository's git
    worktrees (a root-free Lead worktree). A symlinked mailbox pointing
    elsewhere is refused (eval finding 4)."""
    try:
        root_real = Path(root).resolve(strict=True)
        rbox = Path(root_mailbox).resolve(strict=True)
        lbox = Path(live_mailbox).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathEscape(f"mailbox path does not resolve: {exc}") from None
    if not _under(rbox, root_real) or not rbox.is_dir():
        raise PathEscape(f"mailbox {root_mailbox} resolves outside the workspace ({rbox})")
    if lbox != rbox and not _under(lbox, root_real):
        if not any(_under(lbox, wt) for wt in _lead_worktree_roots(root_real)):
            raise PathEscape(f"live mailbox {live_mailbox} resolves outside the workspace "
                             f"and its git worktrees ({lbox})")
    if not lbox.is_dir():
        raise PathEscape(f"live mailbox {live_mailbox} is not a directory")
    return rbox, lbox


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
    a symlink, or unreadable)."""
    try:
        fd = os.open(str(Path(mailbox) / name), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def append_nofollow(path: Path, text: str, *, header: str | None = None) -> None:
    """Append to a mailbox file without following a symlink (O_NOFOLLOW);
    ``header`` is written first when the file is created."""
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
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
        check_mailbox_paths(root, root_mailbox, live_mailbox)
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
        top = git_toplevel(self.root_mailbox)
        self.repo_root = top if top is not None and _under(top, self.root) else self.root
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
        return (["python3", str(entry), "run", "--mailbox", str(ctx.root_mailbox),
                 "--max-iterations", str(max_iterations), "--runner", "portable"], ctx.repo_root)
    trioctl = trioctl_path(ctx.home)
    if trioctl is None:
        raise FixRefused("the installed trioctl is missing (~/.local/bin/trioctl or TRIO_DASH_TRIOCTL)")
    return ([str(trioctl), "omnigent", "loop", "--mailbox", str(ctx.root_mailbox),
             "--max-iterations", str(max_iterations)], ctx.repo_root)


def _native_cmd(ctx: LoopContext, mode: str, max_iterations: int | None = None) -> tuple[list[str], Path]:
    """The installed release's launch.sh, and nothing a mailbox, the run
    registry or a result record names: no ``--helper`` is ever passed (the
    workflow then uses the release's own helper), and a resume — which
    replays the recorded args byte-identically — is refused when those args
    name any helper other than the release's (eval finding 1)."""
    launcher = native_launcher(ctx.home)
    if launcher is None:
        raise FixRefused("the installed release has no native launcher (install a release, or "
                         "configure TRIO_DASH_RELEASE_NATIVE)")
    if mode == "resume":
        run_id = (ctx.native or {}).get("run_id")
        if not run_id or not re.fullmatch(r"wf_[\w-]+", run_id):
            raise FixRefused("no workflow run id recorded for this session; use a fresh start")
        if not (ctx.native or {}).get("launch"):
            raise FixRefused("no .native-launch.json: nothing to resume")
        args = (ctx.native or {}).get("args") or {}
        if not isinstance(args, dict):
            raise FixRefused("the recorded launch args are not an object; use a fresh start")
        helper = args.get("helper")
        if helper is not None and not _same_file(helper, native_helper(ctx.home)):
            raise FixRefused("the recorded run used a helper that is not the installed release's "
                             f"({helper}); a resume would replay it — use a fresh start")
        mailbox_arg = args.get("mailbox")
        if mailbox_arg is not None and not _same_file(mailbox_arg, ctx.live_mailbox):
            raise FixRefused("the recorded launch args name another mailbox; use a fresh start")
        return (["bash", str(launcher), "resume", "--mailbox", str(ctx.live_mailbox),
                 "--run-id", run_id], ctx.live_repo)
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
        steps.append(_cmd_step(["git", "-C", str(repo), "add", "--", rel], Path(repo), detached=False))
        steps.append(_cmd_step(["git", "-C", str(repo), "commit", "-q", "-m",
                                f"loop: iteration {iteration} — SHIP", "--", rel],
                               Path(repo), detached=False))
        notes.append("then re-run the loop so the driver finalizes needs_retirement → shipped")
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
            steps.append(_cmd_step(["git", "-C", str(ctx.live_repo), "worktree", "remove", c["path"]],
                                   ctx.live_repo, detached=False))
            if c["branch"]:
                steps.append(_cmd_step(["git", "-C", str(ctx.live_repo), "branch", "-d", c["branch"]],
                                       ctx.live_repo, detached=False))
        notes += [f"kept {c['path']}: {c['reason']}" for c in candidates if not c["ok"]]
    if native and fix_id.startswith("native_") or (native and fix_id == "repair_scope"):
        note = native_launcher_note(ctx)
        if note:
            notes.append(note)
    basis = plan_basis(ctx)
    return {
        "id": fix_id, "title": spec["title"], "destructive": spec["destructive"],
        "requires_confirm": spec["destructive"], "steps": steps,
        "commands_preview": [s["display"] for s in steps], "notes": notes,
        "basis": basis, "confirm_token": plan_token(ctx, fix_id, steps, basis),
    }


def _file_digest(mailbox: Path, name: str) -> str | None:
    text = _safe_read(mailbox, name)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def plan_basis(ctx: LoopContext) -> dict:
    """The state a plan was made from: the live checkout's HEAD and the
    STATE.md / VERDICT.md / HUMAN.md digests (eval finding 3). Computed
    once per LoopContext (a context is gathered per request)."""
    cached = getattr(ctx, "_plan_basis", None)
    if cached is not None:
        return dict(cached)
    try:
        head = git(ctx.live_repo, "rev-parse", "-q", "--verify", "HEAD").stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        head = None
    basis = {"head": head, "state": _file_digest(ctx.live_mailbox, "STATE.md"),
             "verdict": _file_digest(ctx.live_mailbox, "VERDICT.md"),
             "human": _file_digest(ctx.live_mailbox, "HUMAN.md")}
    ctx._plan_basis = dict(basis)
    return basis


def plan_token(ctx: LoopContext, action_id: str, steps: list[dict], basis: dict,
               extra: dict | None = None) -> str:
    """hash(loop, action id, exact steps, basis): a confirm must present the
    token of the preview it saw; the server re-plans and compares, so a
    confirm never runs commands (or against a state) the human did not see."""
    payload = {
        "loop": ctx.key, "live_mailbox": str(ctx.live_mailbox), "action": action_id,
        "steps": [{k: s.get(k) for k in ("kind", "argv", "cwd", "path", "changes", "text")}
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
                        "commands_preview": plan["commands_preview"], "notes": plan["notes"]})
        except FixRefused as exc:
            out.append({"id": fix_id, "title": FIXES[fix_id]["title"], "applicable": False,
                        "destructive": FIXES[fix_id]["destructive"], "reason": str(exc)})
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


def _write_atomic_nofollow(path: Path, data: bytes) -> None:
    """Replace a mailbox file atomically: a fresh temp file (O_EXCL|O_NOFOLLOW)
    in the same directory, then rename over the target (never through a
    symlink)."""
    tmp = path.with_name(f".{path.name}.dash-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


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
            res = _start_detached(ctx, plan["id"], step, who, processes, on_exit)
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


def _start_detached(ctx: LoopContext, fix_id: str, step: dict, who: dict,
                    processes: dict | None, on_exit=None) -> dict:
    runs = loop_state_dir(ctx.home, ctx.key) / "runs"
    runs.mkdir(exist_ok=True)
    log_path = runs / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{fix_id}.log"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
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

    def reap() -> None:
        try:
            rc = process.wait()
        except Exception:  # noqa: BLE001 - best effort
            return
        if processes is not None:
            processes.pop(process.pid, None)
        if on_exit is not None:
            try:
                on_exit(process.pid, rc)
            except Exception:  # noqa: BLE001 - logging must not fail
                pass
        log_action(ctx.home, ctx.key, {
            "action": "fix-exit", "fix": fix_id, "pid": process.pid, "exit_code": rc,
            "log": str(log_path), "output": tail_text(log_path, 1500)})

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
    "The Lead applies only the newest such entry, and only when N is the\n"
    "iteration that just stopped; older entries are informational. Agents never\n"
    "edit this file. Format: MAILBOX-SCHEMA.md \"HUMAN.md\".\n"
)
ANSWER_LIMIT = 20_000
ENTRY_RE = re.compile(
    r"^## (?P<at>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) — answer (?P<id>[0-9a-f]{8,16}) — "
    r"iteration (?P<iteration>\d+|\?) — trio-dash (?P<sig>[0-9a-f]{16,64})$", re.M)


def _answer_key(home: Path) -> bytes:
    """The dashboard's HMAC key for HUMAN.md entry headers (created once,
    0600, outside every workspace)."""
    path = state_dir(home) / "answer-key"
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(key.hex() + "\n")
        return key
    except FileExistsError:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())


def _entry_sig(home: Path, at: str, answer_id: str, iteration, body: str) -> str:
    msg = "\n".join([at, answer_id, str(iteration), hashlib.sha256(body.encode()).hexdigest()])
    return hmac.new(_answer_key(home), msg.encode(), hashlib.sha256).hexdigest()[:24]


def quote_body(text: str) -> str:
    """Every answer line quoted (``> ``): the answer can never forge a header."""
    return "\n".join(("> " + line) if line else ">" for line in text.split("\n")) + "\n"


def human_entries(home: Path, mailbox: Path) -> list[dict]:
    """Server-written HUMAN.md entries, oldest first, each with ``verified``
    (its HMAC matches: written by this dashboard, header and text intact)."""
    text = _safe_read(mailbox, HUMAN_FILE) or ""
    matches = list(ENTRY_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        chunk = text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        body_lines = [line[2:] if line.startswith("> ") else "" for line in chunk.splitlines()
                      if line.startswith(">")]
        body = "\n".join(body_lines).strip("\n").replace("\r\n", "\n")
        sig = _entry_sig(home, m["at"], m["id"], m["iteration"], body)
        out.append({"at": m["at"], "id": m["id"], "iteration": m["iteration"],
                    "verified": hmac.compare_digest(sig, m["sig"]), "header": m.group(0)})
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
    return {"allowed": reason is None, "reason": reason, "stop": stop,
            "reset_allowed": reset_reason is None, "reset_reason": reset_reason,
            "path": str(ctx.live_mailbox / HUMAN_FILE),
            "entries": [f"{e['at']} — answer {e['id']} — iteration {e['iteration']}"
                        + ("" if e["verified"] else " (UNVERIFIED: not written by this dashboard)")
                        for e in entries[-5:]],
            "verdict": verdict, "iteration": to_int(ctx.state.get("iteration"))}


def plan_answer(ctx: LoopContext, text: str, reset: bool, who: dict) -> dict:
    info = answer_context(ctx)
    if not info["allowed"]:
        raise FixRefused(info["reason"])
    _require_not_live(ctx)
    if not isinstance(text, str) or not text.strip():
        raise FixRefused("the answer is empty")
    if len(text) > ANSWER_LIMIT:
        raise FixRefused(f"the answer is longer than {ANSWER_LIMIT} characters")
    if reset and not info["reset_allowed"]:
        raise FixRefused(info["reset_reason"])
    mailbox_file(ctx.live_mailbox, "STATE.md")
    at = _now()
    body = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    answer_id = hashlib.sha256((body + at + uuid.uuid4().hex).encode()).hexdigest()[:12]
    iteration = info["iteration"] if info["iteration"] is not None else "?"
    by = re.sub(r"[\r\n]+", " ", str(who.get("user") or who.get("addr") or "unknown"))[:120]
    sig = _entry_sig(ctx.home, at, answer_id, iteration, body)
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
            "commands_preview": [s["display"] for s in steps], "destructive": reset,
            "requires_confirm": True, "reset": reset, "basis": basis, "confirm_token": token}


def execute_answer(ctx: LoopContext, plan: dict, who: dict) -> dict:
    results = []
    try:
        path = mailbox_file(ctx.live_mailbox, HUMAN_FILE)
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
        "loop": ctx.name, "root": str(ctx.root), "root_mailbox": str(ctx.root_mailbox),
        "live_mailbox": str(ctx.live_mailbox), "driver": ctx.driver,
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
        tmp = cfg.with_name(f".cli-config.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
        os.replace(tmp, cfg)
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
        snap["<git diff>"] = hashlib.sha256(git(repo, "diff", "HEAD", "--binary").stdout.encode()).hexdigest()
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
        tmp = path.with_name(f".diagnosis.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(record, indent=1, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)

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

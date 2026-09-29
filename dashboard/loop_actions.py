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
import json
import os
import re
import shlex
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


def harnesses(home: Path) -> dict:
    """Diagnosis harness catalog: binary, model, availability."""
    cursor = _executable(_env_path("TRIO_DASH_CURSOR_AGENT")
                         or home / ".local" / "bin" / "cursor-agent")
    codex = _executable(_env_path("TRIO_DASH_CODEX")
                        or home / ".local" / "bin" / "codex")
    default = os.environ.get("TRIO_DASH_DIAGNOSE_HARNESS", "cursor").strip()
    if default not in ("cursor", "codex"):
        default = "cursor"
    return {
        "default": default,
        "cursor": {
            "available": cursor is not None, "bin": str(cursor) if cursor else None,
            "model": os.environ.get("TRIO_DASH_CURSOR_MODEL", "").strip()
            or DEFAULT_CURSOR_MODEL,
            "mode": "ask (read-only)",
        },
        "codex": {
            "available": codex is not None, "bin": str(codex) if codex else None,
            "model": os.environ.get("TRIO_DASH_CODEX_MODEL", "").strip()
            or DEFAULT_CODEX_MODEL,
            "effort": os.environ.get("TRIO_DASH_CODEX_EFFORT", "").strip()
            or DEFAULT_CODEX_EFFORT,
            "mode": "exec --sandbox read-only",
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
    runs = []
    try:
        files = sorted(native_runs_dir(home).glob("*.json"))
    except OSError:
        return runs
    for path in files:
        record = read_json(path)
        if not record or record.get("driver", NATIVE_DRIVER) != NATIVE_DRIVER:
            continue
        mailbox = record.get("mailbox")
        if not isinstance(mailbox, str) or not os.path.isabs(mailbox):
            continue
        mbox = Path(mailbox)
        if not is_mailbox(mbox):
            continue
        repo = record.get("repo")
        repo_path = Path(repo) if isinstance(repo, str) and os.path.isabs(repo) else None
        if repo_path is None or not repo_path.is_dir():
            repo_path = git_toplevel(mbox)
        runs.append({**record, "mailbox": str(mbox), "repo": str(repo_path) if repo_path else None,
                     "registry_file": str(path)})
    return runs


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


def native_launcher(home: Path, facts: dict, registry: dict | None) -> Path | None:
    """launch.sh to use for a native fix: ``TRIO_DASH_NATIVE_LAUNCH``, else
    the one this run was launched with (registry record, result record, or
    the recorded helper's sibling — a resume must use the same launcher),
    else the installed release's; only a ``launch.sh`` that sits next to
    trio_native_step.py and trio-native.js counts."""
    candidates = [_env_path("TRIO_DASH_NATIVE_LAUNCH")]
    if registry and isinstance(registry.get("launcher"), str):
        candidates.append(Path(registry["launcher"]))
    result = facts.get("result") or {}
    if isinstance(result.get("launcher"), str):
        candidates.append(Path(result["launcher"]))
    helper = (facts.get("args") or {}).get("helper")
    if isinstance(helper, str) and os.path.isabs(helper):
        candidates.append(Path(helper).parent / "launch.sh")
    rel = release_dir(home)
    if rel is not None:
        candidates.append(rel / "native" / "launch.sh")
    for path in candidates:
        if path is None or not path.is_absolute() or path.name != "launch.sh":
            continue
        try:
            if (path.is_file() and (path.parent / "trio_native_step.py").is_file()
                    and (path.parent / "trio-native.js").is_file()):
                return path
        except OSError:
            continue
    return None


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


class LoopContext:
    """Everything a fix or a diagnosis reads about one loop, gathered once."""

    def __init__(self, *, home: Path, root: Path, name: str, root_mailbox: Path,
                 live_mailbox: Path, detection: dict, driver: str | None,
                 last_action: dict | None = None, registry: dict | None = None):
        self.home = Path(home)
        self.root = Path(root)
        self.name = name
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
    launcher = native_launcher(ctx.home, ctx.native or {}, ctx.registry)
    if launcher is None:
        raise FixRefused("no valid native launch.sh (configure TRIO_DASH_NATIVE_LAUNCH or install the release)")
    if mode == "resume":
        run_id = (ctx.native or {}).get("run_id")
        if not run_id or not re.fullmatch(r"wf_[\w-]+", run_id):
            raise FixRefused("no workflow run id recorded for this session; use a fresh start")
        if not (ctx.native or {}).get("launch"):
            raise FixRefused("no .native-launch.json: nothing to resume")
        return (["bash", str(launcher), "resume", "--mailbox", str(ctx.live_mailbox),
                 "--run-id", run_id], ctx.live_repo)
    cmd = ["bash", str(launcher), "start", "--mailbox", str(ctx.live_mailbox),
           "--max-iterations", str(max_iterations)]
    helper = (ctx.native or {}).get("args", {}).get("helper")
    if isinstance(helper, str) and os.path.isabs(helper) and Path(helper).name == "trio_native_step.py" \
            and Path(helper).is_file():
        cmd += ["--helper", helper]
    return cmd, ctx.live_repo


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
            steps.append({"kind": "append", "path": str(ctx.live_mailbox / "VERDICT.md"),
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
    return {
        "id": fix_id, "title": spec["title"], "destructive": spec["destructive"],
        "requires_confirm": spec["destructive"], "steps": steps,
        "commands_preview": [s["display"] for s in steps], "notes": notes,
    }


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


def edit_state(path: Path, changes: dict) -> dict:
    """Rewrite STATE.md keys in place (None removes the key's line)."""
    text = read_text(path) or ""
    lines = text.splitlines()
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
    tmp = path.with_name(f".{path.name}.dash-{os.getpid()}.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"before": before, "after": {k: v for k, v in changes.items()}}


def execute_plan(ctx: LoopContext, plan: dict, *, who: dict, processes: dict | None = None,
                 on_exit=None) -> dict:
    """Run a planned fix's steps in order; stop at the first failure.

    Detached steps (loop drivers, land) start in their own session with
    output to ``runs/<ts>-<fix>.log`` and must survive a short startup
    grace; their exit is appended to the action log by a reaper thread."""
    results = []
    ok = True
    for step in plan["steps"]:
        if step["kind"] == "state":
            try:
                results.append({"step": step["display"], "ok": True,
                                "change": edit_state(Path(step["path"]), step["changes"])})
            except OSError as exc:
                results.append({"step": step["display"], "ok": False, "error": str(exc)})
                ok = False
                break
        elif step["kind"] == "append":
            try:
                with open(step["path"], "a", encoding="utf-8") as fh:
                    fh.write(step["text"])
                results.append({"step": step["display"], "ok": True})
            except OSError as exc:
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
    entry = log_action(ctx.home, ctx.key, {
        "action": "fix", "fix": plan["id"], "who": who, "mailbox": str(ctx.root_mailbox),
        "live_mailbox": str(ctx.live_mailbox), "commands": plan["commands_preview"],
        "confirmed": bool(plan.get("confirmed")), "ok": ok, "results": results,
        "state_before": ctx.derived.get("state"),
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
    "Append-only answers from a person to the loop (trio-dash answer box or by\n"
    "hand). The Lead reads this file at the start of every pass; the newest\n"
    "entry answers the NEEDS_HUMAN/BLOCKED stop that preceded the current run.\n"
    "Agents never edit it. Format: MAILBOX-SCHEMA.md \"HUMAN.md\".\n"
)
ANSWER_LIMIT = 20_000


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
    if ctx.live:
        reason = "the loop is live; answers are only written to a stopped loop"
    elif stop is None:
        reason = "the loop is not stopped at NEEDS_HUMAN or BLOCKED"
    reset_reason = None
    if holds:
        reset_reason = ("held dispatch records exist: reconcile them first "
                        "(the answer is still recorded, STATE is not reset)")
    entries = re.findall(r"^## (.+)$", read_text(ctx.live_mailbox / HUMAN_FILE) or "", re.M)
    return {"allowed": reason is None, "reason": reason, "stop": stop,
            "reset_allowed": reset_reason is None, "reset_reason": reset_reason,
            "path": str(ctx.live_mailbox / HUMAN_FILE), "entries": entries[-5:],
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
    answer_id = hashlib.sha256((text + _now()).encode()).hexdigest()[:8]
    at = _now()
    by = who.get("user") or who.get("addr") or "unknown"
    body = text.strip().replace("\r\n", "\n")
    entry = (f"\n## {at} — answer {answer_id}\n"
             f"in-reply-to: {info['stop']} (iteration {info['iteration']})\n"
             f"source: trio-dash ({by})\n\n{body}\n")
    steps = [{"kind": "append", "path": info["path"], "text": entry,
              "display": f"append answer {answer_id} to {info['path']}"}]
    if reset:
        steps.append(_state_reset_step(ctx, {"human_answer": f"HUMAN.md#{answer_id} ({at})"}))
    return {"id": "answer", "answer_id": answer_id, "entry": entry, "steps": steps,
            "commands_preview": [s["display"] for s in steps], "destructive": reset,
            "requires_confirm": True, "reset": reset}


def execute_answer(ctx: LoopContext, plan: dict, who: dict) -> dict:
    path = Path(plan["steps"][0]["path"])
    results = []
    try:
        if not path.exists():
            path.write_text(HUMAN_HEADER, encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(plan["entry"])
        results.append({"step": plan["steps"][0]["display"], "ok": True})
        for step in plan["steps"][1:]:
            results.append({"step": step["display"], "ok": True,
                            "change": edit_state(Path(step["path"]), step["changes"])})
        ok = True
    except OSError as exc:
        results.append({"ok": False, "error": str(exc)})
        ok = False
    entry = log_action(ctx.home, ctx.key, {
        "action": "answer", "who": who, "answer_id": plan["answer_id"], "reset": plan["reset"],
        "mailbox": str(ctx.live_mailbox), "ok": ok, "results": results,
        "chars": len(plan["entry"])})
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
        text = tail_text(box / name, limit) if tail else read_text(box / name, limit)
        if text is not None:
            files[name] = text
    sidecars = {}
    for name in (".session.json", ".driver.json", ".native-launch.json", ".native-result.json",
                 ".repairs"):
        text = read_text(box / name, 4000)
        if text is not None:
            sidecars[name] = text
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


def harness_command(harness: str, cfg: dict, repo: Path, prompt: str, last_path: Path) -> tuple[list[str], str | None]:
    """argv (and stdin) of one read-only diagnosis run."""
    if harness == "cursor":
        return ([cfg["bin"], "-p", "--mode", "ask", "--output-format", "stream-json",
                 "--model", cfg["model"], "--workspace", str(repo), "--trust",
                 "--sandbox", "enabled", prompt], None)
    if harness == "codex":
        return ([cfg["bin"], "exec", "-m", cfg["model"],
                 "-c", f'model_reasoning_effort="{cfg["effort"]}"',
                 "-s", "read-only", "-c", 'approval_policy="never"',
                 "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                 "-C", str(repo), "--json", "-o", str(last_path), "-"], prompt)
    raise ValueError(f"unknown harness {harness}")


def _snapshot_files(ctx: LoopContext) -> dict:
    snap = {}
    try:
        for path in sorted(ctx.live_mailbox.iterdir()):
            if path.is_file():
                snap[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    try:
        snap["<git status>"] = hashlib.sha256(
            git(ctx.live_repo, "status", "--porcelain=v1").stdout.encode()).hexdigest()
        snap["<git HEAD>"] = git(ctx.live_repo, "rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return snap


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

    def start(self, ctx: LoopContext, harness: str, who: dict) -> dict:
        catalog = harnesses(ctx.home)
        if harness not in ("cursor", "codex"):
            raise FixRefused("harness must be cursor or codex")
        cfg = catalog[harness]
        if not cfg["available"]:
            raise FixRefused(f"{harness} CLI not found")
        with self._lock:
            if ctx.key in self._running:
                raise FixRefused("a diagnosis is already running for this loop")
            record = {
                "id": uuid.uuid4().hex[:12], "status": "running", "harness": harness,
                "model": cfg["model"], "effort": cfg.get("effort"), "started_at": _now(),
                "who": who, "events": 0, "last_event": "starting", "loop": ctx.name,
                "loop_live": ctx.live,
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
            argv, stdin_text = harness_command(harness, cfg, ctx.live_repo, prompt, last_path)
            timeout = float(os.environ.get("TRIO_DASH_DIAGNOSE_TIMEOUT") or DIAGNOSE_TIMEOUT_SECONDS)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1")
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

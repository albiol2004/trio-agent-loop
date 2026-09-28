#!/usr/bin/env python3
"""Stdlib Trio loop state machine: gates, verdict, repair, resume.

This is the only implementation of those semantics. portable/driver.sh
(and later trioctl) must call run_loop(); they must not re-code the
state machine. Verdict parsing is delegated to trio-metrics.py so
this driver accepts exactly what trio-check.py accepts.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import importlib.machinery
import importlib.util
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

# Contract version callers (trioctl) check before driving this core. 2 =
# open-loop integration-eval binds evaluated_sha/evaluator_attempt and
# `_evaluated_product_problem` gates SHIP finalization. A core without
# this constant predates that contract (treated as 1). Bump on any change
# a caller must not silently run against. (r15 multi-repo is additive --
# a `repo` key in slice-eval contexts, `pins` in integration contexts, the
# `evaluated_repos` STATE.md key -- and is gated by METRICS_API 5, so
# this stays 2.)
LOOP_CORE_API = 2

# The METRICS_API contract this core itself implements (eval-r15 N3). 5 =
# r15 multi-repo: per-repo pins, retire checks and SHIP retirement. The
# sibling trio-metrics.py carries its own METRICS_API; trioctl takes the
# lower of the two, so a partial refresh (an older core next to a newer
# trio-metrics.py) is never trusted with a `repos:` mailbox. A core without
# this constant predates r15.
METRICS_API = 5

def _load_metrics_module():
    path = Path(__file__).resolve().with_name("trio-metrics.py")
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

# Verdict and slice parsing stay in the existing shared metrics module.
_METRICS = _load_metrics_module()
VERDICT_RE = _METRICS.VERDICT_RE
parse_verdict_scope = _METRICS.parse_verdict_scope
KNOWN_VERDICTS = tuple(_METRICS.KNOWN_VERDICTS)
A_LEAD_RE = _METRICS.A_LEAD_RE
SCOPE_RE = re.compile(r"^scope=(design|local:[^\s]+)$", re.IGNORECASE)
# iteration/status are MAILBOX-SCHEMA.md fields. phase is the resume
# cursor this driver owns (trio-metrics parse_state ignores unknown keys).
# evaluated_sha / evaluator_attempt are lockstep pin keys: they bind a
# resume to the revision and attempt that were dispatched, not merely
# the iteration number leftover in VERDICT.md.
# evaluated_repos (r15) is the per-repo pin of a mailbox that declares
# PLAN.md `repos:`: `<repo>@<sha>` items for every declared repo (home stays
# in evaluated_sha); never written for a single-repo mailbox.
STATE_RE = re.compile(
    r"^\s*(?:-\s+)?(iteration|status|phase|evaluated_sha|"
    r"evaluator_attempt|evaluated_repos)\s*:\s*(.*)$",
    re.IGNORECASE,
)
ROLE_LOG_RE = re.compile(
    r"^\s*-\s*(?:\w+\s+)?(?:iter|iteration)\s+(\d+)\s*\|\s*"
    r"(lead|repair)\s*\|",
    re.IGNORECASE,
)
OUTCOMES = {
    "SHIP": ("shipped", "shipped", 0),
    "BLOCKED": ("blocked", "blocked", 2),
    "NEEDS_HUMAN": ("needs_human", "needs_human", 5),
}
TERMINAL_CODES = {
    "shipped": 0,
    "blocked": 2,
    "needs_human": 5,
    "error": 3,
    "needs_retirement": 6,
}

# A valid SHIP can land in VERDICT.md seconds before the Evaluator's
# retirement commit (observed live: verdict 16:39:12Z, commit 16:39:27Z).
# The driver rechecks for a bounded time instead of rejecting at once.
# Env overrides let operators (and tests) tune or disable the wait.
RETIREMENT_WAIT_ENV = "TRIO_RETIREMENT_WAIT_SECONDS"
RETIREMENT_POLL_ENV = "TRIO_RETIREMENT_POLL_SECONDS"
DEFAULT_RETIREMENT_WAIT_SECONDS = 180.0
DEFAULT_RETIREMENT_POLL_SECONDS = 3.0
# Upper bound for an env-configured wait so a typo cannot pin the loop.
MAX_RETIREMENT_WAIT_SECONDS = 3600.0
# Seams so tests can advance a fake clock without real sleeps.
_retirement_clock = time.monotonic
_retirement_sleep = time.sleep

class RoleRunner(Protocol):
    def run(
        self,
        role: str,
        iteration: int,
        mailbox: Path,
        context: dict | None = None,
    ) -> int:
        ...

def _read_state(path: Path) -> dict[str, str]:
    """Create missing state, then read the keys owned by this driver."""
    if not path.is_file():
        path.write_text(
            "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
        )
    state = {
        "iteration": "0",
        "status": "ready",
        "phase": "idle",
        "evaluated_sha": "",
        "evaluator_attempt": "",
        "evaluated_repos": "",
    }
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    for line in lines:
        match = STATE_RE.match(line)
        if match:
            state[match.group(1).lower()] = match.group(2).strip()
    return state

def _update_state(path: Path, updates: dict[str, str]) -> None:
    """Replace owned keys while preserving all unrelated state lines."""
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    found: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = STATE_RE.match(line)
        key = match.group(1).lower() if match else None
        if key in updates:
            line = f"{key}: {updates[key]}"
            found.add(key)
        result.append(line)
    for key in (
        "iteration",
        "status",
        "phase",
        "evaluated_sha",
        "evaluator_attempt",
        "evaluated_repos",
    ):
        if key == "evaluated_repos" and not updates.get(key):
            continue  # never add an empty per-repo pin (single-repo STATE.md)
        if key in updates and key not in found:
            result.append(f"{key}: {updates[key]}")
    path.write_text(
        "\n".join(result) + ("\n" if result else ""), encoding="utf-8"
    )


# Open-loop-only: the top-level STATE.md `verdict:` hot-summary line from
# MAILBOX-SCHEMA.md ("STATE.md `verdict:` and `eval:` hot-summary lines").
# Kept separate from STATE_RE/_update_state so lockstep's owned-key
# behaviour (iteration/status/phase) stays byte-identical.
VERDICT_LINE_RE = re.compile(r"^\s*(?:-\s+)?verdict\s*:\s*(.*)$", re.IGNORECASE)


def _write_state_verdict(state_path: Path, word: str) -> None:
    """Set (or idempotently replace) the STATE.md `verdict:` line after an
    integration verdict, preserving every other line untouched."""
    lines = state_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    result: list[str] = []
    replaced = False
    for line in lines:
        if VERDICT_LINE_RE.match(line):
            if not replaced:
                result.append(f"verdict: {word}")
                replaced = True
            continue
        result.append(line)
    if not replaced:
        result.append(f"verdict: {word}")
    state_path.write_text(
        "\n".join(result) + ("\n" if result else ""), encoding="utf-8"
    )


def _driver_meta(*runners: object) -> dict:
    """Extra `.driver.json` keys a runner opts into via ``driver_meta``.

    e.g. the Omnigent runner's resolved ``config_path``, which isolated
    worker dispatches read back so builders use the driver's profile.
    Runners without the attribute add nothing (historical payload).
    """
    meta: dict = {}
    for runner in runners:
        extra = getattr(runner, "driver_meta", None)
        if isinstance(extra, dict):
            meta.update(
                (key, value) for key, value in extra.items()
                if isinstance(key, str)
            )
    return meta


def _write_driver_state(
    mailbox: Path, runner: RoleRunner, iteration: int, phase: str
) -> None:
    """Persist the loop cursor and runner sessions outside the mailbox schema."""
    payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "session_ids": getattr(runner, "session_ids", {}) or {},
    }
    for key, value in _driver_meta(runner).items():
        payload.setdefault(key, value)
    (mailbox / ".driver.json").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )

def _append_log(mailbox: Path, line: str) -> None:
    path = mailbox / "LOG.md"
    if not path.is_file():
        path.write_text("# Trio loop log\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        path.write_text(text + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as log:
        log.write(f"{line}\n")

def _counter(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if value >= 0 else 0

def _number(value: str) -> int:
    match = re.match(r"\d+", value.strip())
    return int(match.group(0)) if match else 0

# The mailbox lock is the `.lock` directory with a `pid` file (unchanged
# on-disk protocol, so older drivers still see it). Every acquire, stale
# takeover and release by this module happens under an flock on the
# mailbox directory itself, so no two of them interleave: a second
# process never sees the mkdir-before-pid window, and a stale lock is
# taken over by one process only. A pid-less `.lock` is an acquirer
# between mkdir and pid (or an older driver) and is only stale after
# LOCK_EMPTY_GRACE_SECONDS. Release removes the lock only when its
# `owner` token is ours, never another owner's.
LOCK_EMPTY_GRACE_SECONDS = 60.0
_LOCK_TOKENS: dict[str, str] = {}


class _MailboxGuard:
    """Exclusive flock on the mailbox directory (released on close)."""

    def __init__(self, mailbox: Path) -> None:
        self.mailbox = mailbox

    def __enter__(self) -> "_MailboxGuard":
        import fcntl

        self.fd = os.open(self.mailbox, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _lock_pid(lock: Path) -> int:
    try:
        return int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_lock_file(lock: Path, name: str, text: str) -> None:
    tmp = lock / f".{name}.{os.getpid()}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, lock / name)


def _discard_lock_dir(mailbox: Path, lock: Path) -> None:
    """Atomically move `lock` aside, then delete the moved copy."""
    tomb = mailbox / f".lock.stale-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        os.rename(lock, tomb)
    except FileNotFoundError:
        return
    shutil.rmtree(tomb, ignore_errors=True)


def _acquire_lock(mailbox: Path) -> Path | None:
    """Take the mailbox lock, or None while another owner holds it."""
    mailbox = Path(mailbox)
    lock = mailbox / ".lock"
    with _MailboxGuard(mailbox):
        try:
            lock.mkdir()
        except FileExistsError:
            pid = _lock_pid(lock)
            if pid > 0 and _pid_alive(pid):
                print(
                    f"Mailbox {mailbox}/ is owned by a live driver "
                    f"(pid {pid}).",
                    file=sys.stderr,
                )
                return None
            if pid <= 0:
                try:
                    age = time.time() - lock.stat().st_mtime
                except FileNotFoundError:
                    age = 0.0  # released by a non-guarded holder just now
                if age < LOCK_EMPTY_GRACE_SECONDS:
                    print(
                        f"Mailbox {mailbox}/ lock is being acquired "
                        "(no pid yet); not taking it.",
                        file=sys.stderr,
                    )
                    return None
            print(
                f"Removing stale lock on {mailbox}/ "
                f"(pid {pid or 'unknown'} is gone).",
                file=sys.stderr,
            )
            _discard_lock_dir(mailbox, lock)
            try:
                lock.mkdir()
            except FileExistsError:
                return None  # an older, unguarded driver got in first
        token = uuid.uuid4().hex
        _write_lock_file(lock, "owner", f"{token}\n")
        _write_lock_file(lock, "pid", f"{os.getpid()}\n")
        _LOCK_TOKENS[str(lock)] = token
    return lock


def _release_lock(lock: Path | None) -> None:
    """Remove `lock` only if this process's owner token is still on it."""
    if lock is None:
        return
    lock = Path(lock)
    token = _LOCK_TOKENS.pop(str(lock), None)
    if token is None:
        return
    try:
        guard = _MailboxGuard(lock.parent)
        guard.__enter__()
    except OSError:
        return
    try:
        try:
            owner = (lock / "owner").read_text(encoding="utf-8").strip()
        except OSError:
            return
        if owner != token:
            print(
                f"Not removing {lock}: it now belongs to another owner.",
                file=sys.stderr,
            )
            return
        _discard_lock_dir(lock.parent, lock)
    finally:
        guard.__exit__()

def _commit_gate(mailbox: Path, repo: Path | None) -> tuple[bool, str]:
    """Run trio-shadow; exit 0 passes and exits 1/2 fail."""
    script = Path(__file__).resolve().with_name("trio-shadow.py")
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    command = [
        sys.executable,
        str(script),
        "--mailbox",
        str(target.resolve()),
        "--require-commits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        return False, f"commit gate could not run: {exc}"
    if result.returncode == 0:
        return True, "commit gate passed"
    return False, f"commit gate failed with exit {result.returncode}"

def _log_gate(mailbox: Path, iteration: int, role: str) -> tuple[bool, str]:
    """Require this role's Format-A line for the current iteration."""
    try:
        lines = (mailbox / "LOG.md").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return False, "LOG.md is missing or unreadable"
    for line in lines:
        match = A_LEAD_RE.match(line) if role == "lead" else ROLE_LOG_RE.match(line)
        if match and int(match.group(1)) == iteration:
            if role == "lead" or match.group(2).lower() == role:
                return True, "LOG gate passed"
    return False, f"LOG.md has no iter {iteration} | {role} | line"


run_commit_gate = _commit_gate
run_log_gate = _log_gate

def _invoke_runner(
    runner: RoleRunner,
    role: str,
    iteration: int,
    mailbox: Path,
    context: dict,
) -> int:
    """Call runner.run with `context` only when its signature accepts one.

    api:OpenLoopRunner: a runner whose `run` takes exactly the legacy 3
    positional arguments (every runner at HEAD, including the fakes in
    test_trio_loop.py/test_portable_driver.py and OmnigentRunner) is called
    unchanged. Signature inspection is preferred over a TypeError fallback
    so a real TypeError raised *inside* the runner is never swallowed.
    """
    try:
        params = inspect.signature(runner.run).parameters
        accepts_context = len(params) >= 4 or any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()
        )
    except (TypeError, ValueError):
        accepts_context = True
    if accepts_context:
        return runner.run(role, iteration, mailbox, context)
    return runner.run(role, iteration, mailbox)

def _run_role(mailbox, iteration, role, runner, repo, state_path) -> bool:
    """Run a Lead/repair role, retrying a failed gate once."""
    for _attempt in range(2):
        result = _invoke_runner(runner, role, iteration, mailbox, {})
        if result != 0:
            raise RuntimeError(f"{role} runner failed with exit {result}")
        checks = (
            _commit_gate(mailbox, repo),
            _log_gate(mailbox, iteration, role),
        )
        failures = [note for ok, note in checks if not ok]
        if not failures:
            return True
    _update_state(state_path, {"status": "error", "phase": "error"})
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | gate breach after {role}: "
        + "; ".join(failures),
    )
    return False

def _first_verdict(path: Path) -> tuple[str | None, str | None]:
    """Parse the first non-empty line with the shared verdict helpers."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    first = next((line.strip() for line in lines if line.strip()), "")
    match = VERDICT_RE.match(first)
    if not match:
        return None, None
    word = match.group(1).upper()
    if word not in KNOWN_VERDICTS:
        return None, None
    parsed_word, scope = parse_verdict_scope(first)
    if parsed_word != word:
        return None, None
    rest = first[match.end():].strip()
    if not rest.startswith("scope="):
        return word, None
    if word != "ITERATE" or not SCOPE_RE.fullmatch(rest):
        return None, None
    return word, scope.lower() if scope else None

def _has_verdict_line(path: Path) -> bool:
    """True when VERDICT.md's first non-empty line matches ``VERDICT: ...``
    at all (regardless of whether the word is known) -- used to tell "the
    runner wrote nothing" (retryable) apart from "the runner wrote a real,
    malformed verdict" (a genuine unparseable-verdict error), per
    open-loop's integration-eval output-verification rule."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    first = next((line.strip() for line in lines if line.strip()), "")
    return bool(VERDICT_RE.match(first))

def _slice_verdict_blocks(text: str) -> list[dict]:
    """Like parse_slice_verdicts but keeps each section's exact text (the
    heading line through the line before the next heading, or EOF), so a
    clobbered section can be re-appended byte-for-byte."""
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, str, str, str]] = []
    for index, raw in enumerate(lines):
        match = _METRICS.SLICE_VERDICT_RE.match(raw.strip())
        if match:
            headings.append((index, match.group(1), match.group(2), match.group(3)))
    blocks = []
    for position, (start, slice_id, sha, verdict) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        blocks.append({
            "slice": slice_id,
            "sha": sha,
            "verdict": verdict,
            "text": "".join(lines[start:end]),
        })
    return blocks

def _read_text_settled(path: Path, tries: int = 5, pause: float = 0.02) -> str:
    """Read *path* until two consecutive reads agree (bounded).

    Concurrent slice-eval sessions rewrite VERDICT.md while the driver
    reads it; a single read can catch a truncated, half-written file.
    """
    def once() -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    text = once()
    for _ in range(tries):
        time.sleep(pause)
        again = once()
        if again == text:
            return text
        text = again
    return text


def _restore_clobbered_verdict_sections(
    mailbox: Path,
    verdict_path: Path,
    snapshot_text: str,
    kind: str,
    iteration: int,
    read_text=None,
) -> int:
    """Guard against an Evaluator session rewriting VERDICT.md whole instead
    of appending: if any ``## slice ... — SHIP|ITERATE`` section present
    before the runner ran is missing afterwards, restore it.

    Returns the number of sections restored (0 when nothing was missing).

    Never drops anything the runner just wrote. When the new text still
    opens with an overall ``VERDICT: ...`` line, that whole new text is
    kept verbatim and the missing old sections are appended after it;
    otherwise (plain per-slice appends, no overall verdict line) the
    rebuild is the new text's own sections followed by the missing old
    ones, both in their original relative order.
    """
    if read_text is not None:
        new_text = read_text(verdict_path)
    else:
        new_text = (
            verdict_path.read_text(encoding="utf-8", errors="replace")
            if verdict_path.is_file()
            else ""
        )
    old_blocks = _slice_verdict_blocks(snapshot_text)
    if not old_blocks:
        return 0
    new_blocks = _slice_verdict_blocks(new_text)
    new_keys = {(b["slice"], b["sha"]) for b in new_blocks}
    missing = [b for b in old_blocks if (b["slice"], b["sha"]) not in new_keys]
    if not missing:
        return 0
    first_line = next((line.strip() for line in new_text.splitlines() if line.strip()), "")
    if VERDICT_RE.match(first_line):
        base = new_text
        if base and not base.endswith("\n"):
            base += "\n"
    else:
        base = "".join(b["text"] for b in new_blocks)
    rebuilt = base + "".join(b["text"] for b in missing)
    verdict_path.write_text(rebuilt, encoding="utf-8")
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | open-loop: restored {len(missing)} "
        f"clobbered per-slice section(s) in VERDICT.md after {kind}",
    )
    return len(missing)

def _verdict_commit_shas(text: str, repo: str | None = None) -> list[str]:
    """Return ``commit: <sha>`` values recorded in VERDICT.md.

    r15: *repo* None (the default) keeps the historical meaning -- bare
    shas only, the home repo's (``commit: <repo>@<sha>`` lines of declared
    repos never count). With *repo*, the shas of ``commit:`` lines pinned to
    it (``<repo>@<sha>``; ``home`` also takes bare shas).
    """
    shas: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("commit:"):
            got = line.split(":", 1)[1].strip().lower()
            if repo is None:
                if re.fullmatch(r"[0-9a-f]{7,40}", got):
                    shas.append(got)
                continue
            for name, sha in _METRICS.parse_repo_pins([got]):
                if name == repo:
                    shas.append(sha)
    return shas


_SLICE_SECTION_HEADING_RE = re.compile(r"^##\s+slice\s+\S+\s+@[0-9a-fA-F]{7,40}\b")


def _integration_verdict_text(text: str) -> str:
    """VERDICT.md with every open-loop ``## slice <id> @<sha>`` section removed.

    Open-loop slice-evals append their own sections (with their own
    ``iteration:``/``evaluated:``/``attempt:`` lines) to the same file as
    the integration verdict. A section runs from its heading up to (not
    including) the next ``# ``/``## `` heading that is not itself a slice
    heading, or EOF. Everything else is kept in order, so integration
    field checks see only the integration verdict's own block.
    """
    kept: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if _SLICE_SECTION_HEADING_RE.match(line):
            skipping = True
            continue
        if skipping and (line.startswith("# ") or line.startswith("## ")):
            skipping = False
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _verdict_mentions_iteration(text: str, iteration: int) -> bool:
    """True when the verdict names this iteration and no other one.

    Canonical form is an exact ``iteration: N`` field line; the legacy
    ``iteration N`` heading/prose form is still accepted. Numbers are
    whole-word (``iteration 1`` never matches ``iteration 10``). Any
    ``iteration:`` field, or ``#`` heading naming an iteration, that
    disagrees with N rejects the artifact as mismatched.
    """
    want = str(iteration)
    fields = _verdict_field_values(text, "iteration")
    if any(value != want for value in fields):
        return False
    heading_re = re.compile(r"\biteration\s+(\d+)\b", re.IGNORECASE)
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            if any(n != want for n in heading_re.findall(raw)):
                return False
    if fields:
        return True
    return any(n == want for n in heading_re.findall(text))


def _verdict_field_values(text: str, key: str) -> list[str]:
    """Values for exact ``key:`` lines (not a prefix of a longer key)."""
    want_key = key.strip().lower()
    found: list[str] = []
    if not want_key:
        return found
    for raw in text.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip().lower() != want_key:
            continue
        got = rest.strip()
        if got:
            found.append(got)
    return found


def _verdict_records_attempt(text: str, attempt: str) -> bool:
    """True when VERDICT.md names this unique evaluator attempt."""
    want = attempt.strip().lower()
    if not want:
        return False
    return any(got.lower() == want for got in _verdict_field_values(text, "attempt"))


def _sha_matches(got: str, want: str) -> bool:
    """True when hex shas are equal or one is a unique prefix of the other."""
    a = got.strip().lower()
    b = want.strip().lower()
    if not a or not b:
        return False
    return a == b or b.startswith(a) or a.startswith(b)


def _verdict_records_evaluated(text: str, sha: str, repo: str = "home") -> bool:
    """True when ``evaluated:`` records the graded revision (not ``commit:``).

    r15: a multi-repo pin is recorded as ``<repo>@<sha>`` items (one line or
    several); the home repo's sha may also be bare, exactly as before.
    """
    want = sha.strip()
    if not want:
        return False
    values = _verdict_field_values(text, "evaluated")
    if repo == "home" and any(_sha_matches(got, want) for got in values):
        return True
    pins = getattr(_METRICS, "parse_repo_pins", None)
    if pins is None:
        return False
    return any(
        name == repo and _sha_matches(got, want) for name, got in pins(values)
    )


def _verdict_records_sha(text: str, sha: str) -> bool:
    """True when ``evaluated:`` records the expected pinned revision.

    Product ``commit:`` lines are not a substitute for the graded pin.
    """
    return _verdict_records_evaluated(text, sha)


def _git_root(repo: Path | None) -> Path | None:
    """Return ``repo`` only when it is a git directory we may inspect."""
    if repo is None:
        return None
    if (repo / ".git").exists():
        return repo
    return None


def _git(
    repo: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo``; never raise on a missing binary or bad rev."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr=""
        )


def _git_commit_sha(repo: Path, rev: str) -> str | None:
    """Resolve ``rev`` to a real commit object, or None if it is fake."""
    if not rev.strip():
        return None
    result = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}")
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return None
    return sha


def _git_head(repo: Path) -> str | None:
    """HEAD commit, or None on an empty or unusable repository."""
    return _git_commit_sha(repo, "HEAD")


def _git_is_ancestor(repo: Path, maybe_ancestor: str, rev: str) -> bool:
    """True when ``maybe_ancestor`` is an ancestor of ``rev`` (or equal)."""
    result = _git(
        repo, "merge-base", "--is-ancestor", maybe_ancestor, rev
    )
    return result.returncode == 0


def _commit_paths(repo: Path, sha: str) -> list[str]:
    """Paths changed by ``sha``. Empty commits yield an empty list."""
    result = _git(
        repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
    )
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _mailbox_rel(repo: Path, mailbox: Path) -> str | None:
    """Mailbox path relative to the git root, when it lives inside."""
    try:
        rel = mailbox.resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _path_in_mailbox(path: str, mailbox_rel: str | None) -> bool:
    """True when ``path`` is the mailbox dir or a file under it."""
    if not mailbox_rel:
        return False
    prefix = mailbox_rel.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _mailbox_retirement_commit_present(
    repo: Path, mailbox: Path, iteration: int
) -> bool:
    """True when a SHIP mailbox commit exists, touches mailbox files,
    and is an ancestor of HEAD. A message-only empty commit is not
    enough.
    """
    needle = f"loop: iteration {iteration} — SHIP"
    result = _git(repo, "log", "--grep", needle, "--format=%H")
    if result.returncode != 0:
        return False
    mailbox_rel = _mailbox_rel(repo, mailbox)
    head = _git_head(repo)
    if head is None:
        return False
    for sha in result.stdout.split():
        resolved = _git_commit_sha(repo, sha)
        if resolved is None:
            continue
        if not _git_is_ancestor(repo, resolved, head):
            continue
        paths = _commit_paths(repo, resolved)
        if any(_path_in_mailbox(p, mailbox_rel) for p in paths):
            return True
    return False


def _fold_restored_verdict_into_retirement(
    mailbox: Path, repo: Path | None, iteration: int, restored: int
) -> bool:
    """Fold VERDICT.md sections the driver just restored into the SHIP
    retirement commit the integration Evaluator already made.

    The Evaluator may rewrite VERDICT.md whole and commit it (``loop:
    iteration N — SHIP``) without the per-slice sections; the clobber
    restore then re-appends them to the working tree only, leaving
    VERDICT.md dirty after retirement. Amend that commit with VERDICT.md
    only when every condition holds: the verdict is SHIP; a retirement
    commit for this iteration is an ancestor of HEAD; HEAD itself is that
    commit (single parent, message names it) and touches only mailbox
    paths; nothing is staged; the only dirty tracked mailbox path besides
    the LOG.md/STATE.md driver sidecars (which the retirement check never
    reads as dirty) is VERDICT.md; and the working VERDICT.md extends the
    committed one byte-for-byte (the restore only appends). Otherwise
    nothing is amended and the reason is logged (non-SHIP verdicts and
    no-repo runs, where no retirement commit exists, are skipped silently). The graded pin
    (``evaluated_sha``) is the product revision, which the amend never
    changes. Returns True when the amend happened.
    """
    verdict, scope = _first_verdict(mailbox / "VERDICT.md")
    if verdict != "SHIP" or scope is not None:
        return False

    def refuse(reason: str) -> bool:
        _append_log(
            mailbox,
            f"- iter {iteration} | loop | open-loop: restored sections "
            f"could not be folded into retirement ({reason})",
        )
        return False

    git_root = _git_root(repo)
    if git_root is None:
        # No-repo fakes have no retirement commit to fold into.
        return False
    mailbox_rel = _mailbox_rel(git_root, mailbox)
    if not mailbox_rel:
        return refuse("mailbox is not inside the repository")
    needle = f"loop: iteration {iteration} — SHIP"
    if not _mailbox_retirement_commit_present(git_root, mailbox, iteration):
        return refuse(
            f"no '{needle}' commit touching {mailbox_rel}/ is an "
            "ancestor of HEAD"
        )
    head = _git_head(git_root)
    if head is None:
        return refuse("repository has no HEAD commit")
    info = _git(git_root, "log", "-1", "--format=%P%n%B", head)
    if info.returncode != 0:
        return refuse("git log of HEAD failed")
    parents_line, _sep, message = info.stdout.partition("\n")
    if len(parents_line.split()) != 1:
        return refuse(f"HEAD {head[:12]} is a merge or root commit")
    if needle not in message:
        return refuse(f"HEAD {head[:12]} is not the retirement commit")
    head_paths = _commit_paths(git_root, head)
    outside = [p for p in head_paths if not _path_in_mailbox(p, mailbox_rel)]
    if not head_paths or outside:
        return refuse(
            f"HEAD {head[:12]} touches paths outside {mailbox_rel}/: "
            + (", ".join(outside) or "(none in the mailbox)")
        )
    staged = _diff_paths(git_root, "--cached", "HEAD")
    if staged is None:
        return refuse("git diff --cached failed")
    if staged:
        return refuse(f"index has staged changes: {', '.join(staged)}")
    dirty = _diff_paths(git_root, "HEAD", "--", mailbox_rel)
    if dirty is None:
        return refuse("git diff of the mailbox failed")
    verdict_rel = f"{mailbox_rel}/VERDICT.md"
    sidecars = {f"{mailbox_rel}/LOG.md", f"{mailbox_rel}/STATE.md"}
    if verdict_rel not in dirty:
        return refuse("VERDICT.md has no uncommitted edits")
    others = [p for p in dirty if p != verdict_rel and p not in sidecars]
    if others:
        return refuse(
            f"other mailbox paths have uncommitted edits: {', '.join(others)}"
        )
    try:
        committed = subprocess.run(
            ["git", "-C", str(git_root), "show", f"HEAD:{verdict_rel}"],
            capture_output=True,
            check=False,
        )
        working = (mailbox / "VERDICT.md").read_bytes()
    except OSError:
        return refuse("could not read VERDICT.md")
    if committed.returncode != 0:
        return refuse("VERDICT.md is not in the retirement commit")
    if not working.startswith(committed.stdout):
        return refuse("working VERDICT.md does not extend the committed one")
    if _git_head(git_root) != head:
        return refuse("HEAD moved during the fold checks")
    if _git(git_root, "add", "--", verdict_rel).returncode != 0:
        return refuse("git add VERDICT.md failed")
    amend = _git(git_root, "commit", "--amend", "--no-edit", "--no-verify", "-q")
    if amend.returncode != 0:
        _git(git_root, "reset", "-q", "--", verdict_rel)
        detail = (amend.stderr or amend.stdout).strip().splitlines()
        return refuse(
            "git commit --amend failed"
            + (f": {detail[-1]}" if detail else "")
        )
    new_head = _git_head(git_root) or "?"
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | open-loop: folded {restored} restored "
        f"per-slice section(s) into retirement commit "
        f"{head[:12]}->{new_head[:12]}",
    )
    return True


def _verified_verdict_commit_shas(repo: Path, text: str) -> list[str]:
    """``commit:`` values that exist as git objects (not invented hex)."""
    found: list[str] = []
    head = _git_head(repo)
    if head is None:
        return found
    for raw in _verdict_commit_shas(text):
        sha = _git_commit_sha(repo, raw)
        if sha is None:
            continue
        if _git_is_ancestor(repo, sha, head):
            found.append(sha)
    return found


def _diff_paths(repo: Path, *args: str) -> list[str] | None:
    """``git diff --name-only`` paths, or None when git itself fails."""
    result = _git(repo, "diff", "--name-only", *args)
    if result.returncode != 0:
        return None
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _product_paths_changed(
    paths: list[str], mailbox_rel: str | None
) -> bool:
    """True when any path is outside mailbox bookkeeping."""
    return any(not _path_in_mailbox(p, mailbox_rel) for p in paths)


# Omnigent's cursor-native harness writes exactly these project config
# files into every session workspace. When the workspace is the root
# (non-isolated runs), an untracked copy whose content is exactly the
# generated bridge config is Omnigent residue, not product. The
# fingerprint lives in omnigent/worker_worktrees.py (``owned_residue``);
# trioctl injects it as ``owned_residue_check`` when it loads this core,
# otherwise the sibling copy of the repo layout is used. Without either,
# nothing is exempt (fail closed). Any other ``.cursor`` path, a tracked
# or modified config, or a file with user edits stays product.
OMNIGENT_RESIDUE_PATHS = (".cursor/mcp.json", ".cursor/hooks.json")
owned_residue_check = None
_SIBLING_RESIDUE_CHECK: list = []


def _residue_check():
    """The Omnigent-residue predicate ``(root, rel) -> bool``, or None."""
    if owned_residue_check is not None:
        return owned_residue_check
    if not _SIBLING_RESIDUE_CHECK:
        check = None
        path = Path(__file__).resolve().parent.parent / "omnigent" / "worker_worktrees.py"
        if path.is_file():
            try:
                loader = importlib.machinery.SourceFileLoader(
                    "trio_loop_worker_worktrees", str(path)
                )
                spec = importlib.util.spec_from_loader(loader.name, loader)
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                check = getattr(module, "owned_residue", None)
            except Exception:  # noqa: BLE001 - no predicate means fail closed
                check = None
        _SIBLING_RESIDUE_CHECK.append(check if callable(check) else None)
    return _SIBLING_RESIDUE_CHECK[0]


def _is_omnigent_residue(repo: Path, rel: str) -> bool:
    if rel not in OMNIGENT_RESIDUE_PATHS:
        return False
    check = _residue_check()
    if check is None:
        return False
    try:
        return bool(check(repo, rel))
    except Exception:  # noqa: BLE001 - unreadable means product
        return False


def _product_untracked_paths(
    repo: Path, mailbox_rel: str | None
) -> list[str] | None:
    """Nonignored untracked paths outside the mailbox, or None on git error.

    Uses ``ls-files -o --exclude-standard`` so ignored build/runtime
    outputs are omitted. Untracked Omnigent-generated root Cursor config
    (exact fingerprint, see ``OMNIGENT_RESIDUE_PATHS``) is omitted too.
    Does not stage or delete anything.
    """
    result = _git(
        repo, "ls-files", "-o", "--exclude-standard"
    )
    if result.returncode != 0:
        return None
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
        and not _path_in_mailbox(line.strip(), mailbox_rel)
        and not _is_omnigent_residue(repo, line.strip())
    ]


def _log_untracked_product_blockers(
    mailbox: Path, iteration: int, repo: Path | None
) -> None:
    """Name leftover untracked product files; never stage or delete them.

    Gitignored outputs and the active mailbox are omitted. The user
    can track or exclude those files on purpose; this driver does
    not treat the rest of the tree as fully verified.
    """
    git_root = _git_root(repo)
    if git_root is None:
        return
    mailbox_rel = _mailbox_rel(git_root, mailbox)
    paths = _product_untracked_paths(git_root, mailbox_rel)
    if not paths:
        return
    listed = ", ".join(paths)
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | untracked product files "
        f"block SHIP (not staged or deleted): {listed}. "
        "Track or gitignore them deliberately; the tree is "
        "not fully verified while they remain untracked.",
    )


# --- r15: declared product repos (PLAN.md `repos:`) -------------------------
# The mailbox repo (`repo` of the driver) is `home`; each declared repo is
# pinned, verified and retired on its own. Single-repo mailboxes never reach
# any of this (`_declared_repos` is []).


def _declared_repos_checked(
    mailbox: Path,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """``(valid declared repos, errors)`` of PLAN.md's `repos:` block (r15).

    The valid entries survive an error in another entry (a clone that
    vanished mid-run is an error of that entry only). ``([], [])`` in
    single-repo mode and with a metrics module that predates `repos:`;
    a reader that raises is an error, never single-repo.
    """
    reader = getattr(_METRICS, "read_repos", None)
    if reader is None:
        return [], []
    try:
        info = reader(Path(mailbox))
    except Exception as exc:  # noqa: BLE001 - reported, never single-repo
        return [], [f"PLAN.md repos: unreadable ({type(exc).__name__}: {exc})"]
    repos = [(r["name"], Path(r["path"])) for r in info.get("repos") or []]
    return repos, [str(e) for e in info.get("errors") or []]


def _declared_repos(mailbox: Path) -> list[tuple[str, Path]]:
    """(name, path) of PLAN.md's declared `repos:` (r15), in PLAN order.

    [] in single-repo mode and when the block does not validate. Only the
    pinning and snapshot helpers use this; the SHIP retirement gate and
    pin reuse verify from STATE.md ``evaluated_repos`` and treat an invalid
    block as final (eval-r15 B1), see ``_declared_repos_retirement_problem``.
    """
    repos, errors = _declared_repos_checked(mailbox)
    return [] if errors else repos


def _pinned_repos_problem(
    mailbox: Path, pins: dict[str, str]
) -> tuple[list[tuple[str, Path]], str | None]:
    """``(declared repos, why the pins cannot be verified or None)``.

    The per-repo gates verify from STATE.md ``evaluated_repos``, never
    from whatever PLAN.md says now (eval-r15 B1): a present but invalid
    `repos:` block, or a pinned repo that is no longer declared (dropped
    from PLAN.md, moved or deleted: a missing path is a block error), is a
    problem -- the pinned tree can no longer be checked, so it must never
    ship on the home checks alone.
    """
    declared, errors = _declared_repos_checked(mailbox)
    if errors:
        return [], (
            "PLAN.md repos: block does not validate, so the per-repo pins "
            f"cannot be verified: {errors[0]}"
        )
    if pins and getattr(_METRICS, "read_repos", None) is None:
        return [], (
            "STATE.md evaluated_repos pins declared repos but this metrics "
            "module cannot read PLAN.md repos:"
        )
    names = {name for name, _path in declared}
    for name, sha in pins.items():
        if name not in names:
            return declared, (
                f"repo {name} is pinned in STATE.md evaluated_repos "
                f"({name}@{sha[:12]}) but is no longer declared in PLAN.md "
                "repos:"
            )
    return declared, None


def _repos_with_slices(mailbox: Path, declared: list[tuple[str, Path]]) -> list[str]:
    """Declared repo names that at least one PLAN.md slice belongs to."""
    names = [name for name, _path in declared]
    try:
        text = (Path(mailbox) / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    slices = _METRICS.parse_slices_block(text)
    if slices is None:
        return names
    used = {_METRICS.slice_repo_name(sl, names) for sl in slices}
    return [name for name in names if name in used]


def _state_repo_pins(state: dict[str, str]) -> dict[str, str]:
    """STATE.md ``evaluated_repos`` as ``{repo: sha}``."""
    value = str(state.get("evaluated_repos", "") or "").strip()
    if not value:
        return {}
    return {name: sha for name, sha in _METRICS.parse_repo_pins([value])}


def _format_repo_pins(pins: dict[str, str]) -> str:
    return " ".join(f"{name}@{sha}" for name, sha in pins.items())


def _repo_retirement_commit(repo: Path, iteration: int, pin: str) -> str | None:
    """The declared repo's SHIP retirement commit: a ``loop: iteration N —
    SHIP`` commit that descends from the repo's evaluated *pin* and is an
    ancestor of its HEAD (r15 per-repo retirement), or None."""
    needle = f"loop: iteration {iteration} — SHIP"
    result = _git(repo, "log", "--grep", needle, "--format=%H")
    head = _git_head(repo)
    resolved_pin = _git_commit_sha(repo, pin)
    if result.returncode != 0 or head is None or resolved_pin is None:
        return None
    for sha in result.stdout.split():
        resolved = _git_commit_sha(repo, sha)
        if resolved is None or resolved == resolved_pin:
            continue
        if _git_is_ancestor(repo, resolved, head) and _git_is_ancestor(
            repo, resolved_pin, resolved
        ):
            return resolved
    return None


def _declared_repos_retirement_problem(
    mailbox: Path, iteration: int, text: str, state: dict[str, str]
) -> tuple[str, str] | None:
    """Per-repo SHIP retirement of a `repos:` mailbox (r15), or None.

    Verified from the STATE.md pins (eval-r15 B1): an invalid `repos:`
    block, or a pinned repo no longer declared, is final. Then for every
    declared repo: STATE.md ``evaluated_repos`` pins it, the
    integration verdict records ``evaluated: <repo>@<pin>``, and the repo's
    product tree is unchanged since that pin (final otherwise). Every
    ``commit: <repo>@<sha>`` line must be a commit reachable from that
    repo's HEAD, and each repo that has slices needs its own ``loop:
    iteration N — SHIP`` retirement commit after the pin (pending
    otherwise: the Evaluator may still be committing).
    """
    pins = _state_repo_pins(state)
    declared, unverifiable = _pinned_repos_problem(mailbox, pins)
    if unverifiable is not None:
        return RETIREMENT_FINAL, unverifiable
    if not declared:
        return None
    integration = _integration_verdict_text(text)
    with_slices = set(_repos_with_slices(mailbox, declared))
    pending: tuple[str, str] | None = None
    for name, path in declared:
        pin = pins.get(name, "")
        if not pin:
            return RETIREMENT_FINAL, (
                f"STATE.md evaluated_repos has no pin for repo {name}"
            )
        if not _verdict_records_evaluated(integration, pin, name):
            return RETIREMENT_FINAL, (
                f"VERDICT.md does not record evaluated: {name}@{pin}"
            )
        if _git_root(path) is None:
            return RETIREMENT_FINAL, f"repo {name} ({path}) is not a git repository"
        product = _evaluated_product_problem(path, mailbox, pin)
        if product is not None:
            return RETIREMENT_FINAL, (
                f"product tree changed in repo {name}: {product}"
            )
        if pending is not None:
            continue
        head = _git_head(path)
        claimed = _verdict_commit_shas(text, name)
        for raw in claimed:
            sha = _git_commit_sha(path, raw)
            if sha is None or head is None or not _git_is_ancestor(path, sha, head):
                pending = (RETIREMENT_PENDING, (
                    f"VERDICT.md commit: {name}@{raw} is not a commit "
                    f"reachable from repo {name}'s HEAD"
                ))
                break
        if pending is None and name in with_slices and (
            _repo_retirement_commit(path, iteration, pin) is None
        ):
            pending = (RETIREMENT_PENDING, (
                f"no 'loop: iteration {iteration} — SHIP' retirement commit "
                f"after pin {pin[:12]} in repo {name} ({path})"
            ))
    return pending


def _evaluated_product_problem(
    repo: Path,
    mailbox: Path,
    evaluated_sha: str,
) -> str | None:
    """Why the graded product tree is no longer intact, or None if it is.

    HEAD must still descend from the pin. Later product commits,
    staged/unstaged product edits, and a missing pin object fail.
    Any nonignored untracked product file (even if it existed
    before the pin) fails closed. Mailbox files, ignored outputs and
    untracked exact Omnigent-generated ``.cursor/{mcp,hooks}.json``
    do not block. No-repo callers skip this helper.
    """
    if not evaluated_sha.strip():
        return None
    resolved = _git_commit_sha(repo, evaluated_sha)
    head = _git_head(repo)
    if resolved is None:
        return f"evaluated pin {evaluated_sha} is not a commit in {repo}"
    if head is None:
        return f"repository {repo} has no HEAD commit"
    if not _git_is_ancestor(repo, resolved, head):
        return (
            f"evaluated pin {resolved[:12]} is not an ancestor of "
            f"HEAD {head[:12]}"
        )
    mailbox_rel = _mailbox_rel(repo, mailbox)
    committed = _diff_paths(repo, resolved, head)
    worktree = _diff_paths(repo, resolved)
    index = _diff_paths(repo, "--cached", resolved)
    if committed is None or worktree is None or index is None:
        return "git diff against the evaluated pin failed"
    for label, paths in (
        (f"committed after pin {resolved[:12]}", committed),
        ("staged", index),
        ("modified in worktree", worktree),
    ):
        product = [p for p in paths if not _path_in_mailbox(p, mailbox_rel)]
        if product:
            return f"product paths {label}: {', '.join(product)}"
    untracked = _product_untracked_paths(repo, mailbox_rel)
    if untracked is None:
        return "git ls-files for untracked product paths failed"
    # Preexisting extra.py is still product, not a verified pin.
    if untracked:
        return f"untracked product paths: {', '.join(untracked)}"
    return None


def _evaluated_product_intact(
    repo: Path,
    mailbox: Path,
    evaluated_sha: str,
) -> bool:
    """True when the graded product tree is unchanged except mailbox files."""
    return _evaluated_product_problem(repo, mailbox, evaluated_sha) is None


def _verdict_binds_lockstep(
    text: str, state: dict[str, str], git_root: Path | None
) -> bool:
    """True when a git-visible SHIP names this attempt and graded pin.

    No-repo fakes skip object and field gates (historical exit 0).
    """
    if git_root is None:
        return True
    # Only the integration block binds; appended slice sections do not.
    text = _integration_verdict_text(text)
    attempt = state.get("evaluator_attempt", "").strip()
    evaluated = state.get("evaluated_sha", "").strip()
    if not attempt or not _verdict_records_attempt(text, attempt):
        return False
    if evaluated and not _verdict_records_evaluated(text, evaluated):
        return False
    return True


# Retirement problem kinds. "pending" may still resolve when the
# Evaluator's retirement commit lands; "final" never will by waiting
# (stale/unbound attempt, or the graded product tree changed).
RETIREMENT_PENDING = "pending"
RETIREMENT_FINAL = "final"


# Full retirement checks run on one stable snapshot of their inputs.
RETIREMENT_SNAPSHOT_TRIES = 3


def _retirement_snapshot(mailbox: Path, repo: Path | None) -> tuple:
    """Identity of every input the retirement decision reads.

    VERDICT.md and STATE.md bytes (verdict, attempt, pin), plus HEAD and
    ``git status`` (product tree, retirement commit) when in a git tree.
    Read-only: never creates STATE.md.
    """
    def raw(name: str) -> bytes | None:
        try:
            return (mailbox / name).read_bytes()
        except OSError:
            return None

    git_root = _git_root(repo)
    head = status = None
    repos: list = []
    for _name, path in _declared_repos(mailbox):
        # r15: each declared repo's HEAD and tree are retirement inputs too.
        repos.append((str(path), _git_head(path), _git(
            path, "--no-optional-locks", "status", "--porcelain=v1",
            "--untracked-files=all",
        ).stdout))
    if git_root is not None:
        head = _git_head(git_root)
        # --no-optional-locks: a status index refresh must not take
        # index.lock and race the Evaluator's own git add/commit.
        # Raw bytes: only compared, and -z names may not be UTF-8.
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-C",
                    str(git_root),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ],
                capture_output=True,
                check=False,
            )
            status = result.stdout if result.returncode == 0 else None
        except OSError:
            status = None
    if repos:
        return raw("VERDICT.md"), raw("STATE.md"), head, status, tuple(repos)
    return raw("VERDICT.md"), raw("STATE.md"), head, status


def _ship_retirement_problem(
    mailbox: Path, iteration: int, repo: Path | None
) -> tuple[str, str] | None:
    """Stable-snapshot wrapper around ``_ship_retirement_problem_once``.

    The full check reads VERDICT.md, then runs several git calls. A
    retraction plus a SHIP-titled commit landing in between must not
    ship from the stale read, so a result counts only when the inputs
    are identical before and after the whole check. Otherwise the full
    check reruns on the new state; inputs that never settle are
    ``pending`` (the bounded wait rechecks; no wait means exit 6).
    """
    before = _retirement_snapshot(mailbox, repo)
    for _try in range(RETIREMENT_SNAPSHOT_TRIES):
        problem = _ship_retirement_problem_once(mailbox, iteration, repo)
        after = _retirement_snapshot(mailbox, repo)
        if after == before:
            return problem
        before = after
    return RETIREMENT_PENDING, (
        "VERDICT.md, STATE.md or the git tree changed during every "
        f"retirement check ({RETIREMENT_SNAPSHOT_TRIES} tries)"
    )


def _ship_retirement_problem_once(
    mailbox: Path, iteration: int, repo: Path | None
) -> tuple[str, str] | None:
    """``None`` when SHIP retirement is complete, else ``(kind, detail)``.

    First-pass, the bounded wait, and resume share this gate. Missing
    ``attempt:`` or ``evaluated:`` (when a pin exists) cannot look
    shipped. Product ``commit:`` ancestry is not the graded pin. When
    ``repo`` is not a git tree, skip object checks so no-repo fakes
    keep shipping after the attempt binds.
    """
    try:
        text = (mailbox / "VERDICT.md").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        text = ""
    # Re-parse every time: a verdict retracted to ITERATE (or broken)
    # while metadata and a SHIP-titled commit remain must never ship.
    verdict, scope = _first_verdict(mailbox / "VERDICT.md")
    if verdict != "SHIP" or scope is not None:
        if verdict is None:
            found = "missing or unparseable verdict"
        else:
            found = f"VERDICT: {verdict}" + (f" scope={scope}" if scope else "")
        return RETIREMENT_FINAL, (
            f"VERDICT.md first line is no longer VERDICT: SHIP ({found})"
        )
    state = _read_state(mailbox / "STATE.md")
    git_root = _git_root(repo)
    if not _verdict_binds_lockstep(text, state, git_root):
        attempt = state.get("evaluator_attempt", "").strip()
        evaluated = state.get("evaluated_sha", "").strip()
        if not attempt:
            detail = "STATE.md has no evaluator_attempt to bind the SHIP"
        elif not _verdict_records_attempt(
            _integration_verdict_text(text), attempt
        ):
            detail = (
                f"VERDICT.md does not record attempt: {attempt} "
                "(stale or unbound evaluator artifact)"
            )
        else:
            detail = (
                f"VERDICT.md does not record evaluated: {evaluated}"
            )
        return RETIREMENT_FINAL, detail
    if git_root is None:
        return None
    evaluated = state.get("evaluated_sha", "").strip()
    product = _evaluated_product_problem(git_root, mailbox, evaluated)
    if product is not None:
        return RETIREMENT_FINAL, f"product tree changed: {product}"
    repos_problem = _declared_repos_retirement_problem(
        mailbox, iteration, text, state
    )
    if repos_problem is not None and repos_problem[0] == RETIREMENT_FINAL:
        return repos_problem
    verified = _verified_verdict_commit_shas(git_root, text)
    mailbox_ok = _mailbox_retirement_commit_present(
        git_root, mailbox, iteration
    )
    # Fabricated hex in commit: lines must not satisfy the gate.
    claimed = _verdict_commit_shas(text)
    if claimed and not verified:
        return RETIREMENT_PENDING, (
            "VERDICT.md commit: lines are not commits reachable from "
            f"HEAD: {', '.join(claimed)}"
        )
    if (verified and mailbox_ok) or (mailbox_ok and evaluated and not claimed):
        # r15: every declared repo must be retired too (None single-repo).
        return repos_problem
    if not mailbox_ok:
        mailbox_rel = _mailbox_rel(git_root, mailbox) or str(mailbox)
        return RETIREMENT_PENDING, (
            f"no 'loop: iteration {iteration} — SHIP' commit touching "
            f"{mailbox_rel}/ is an ancestor of HEAD"
        )
    return RETIREMENT_PENDING, (
        "mailbox retirement commit present but VERDICT.md records no "
        "commit: lines and no evaluated pin"
    )


def _ship_retirement_complete(
    mailbox: Path, iteration: int, repo: Path | None
) -> bool:
    """SHIP is finished only with bound evidence and mailbox identity."""
    return _ship_retirement_problem(mailbox, iteration, repo) is None


def _env_seconds(name: str, default: float) -> float:
    """Finite nonnegative seconds from ``name``, capped; else ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return min(value, MAX_RETIREMENT_WAIT_SECONDS)


def _retirement_wait_settings() -> tuple[float, float]:
    """(wait, poll) seconds for the post-SHIP retirement recheck."""
    return (
        _env_seconds(RETIREMENT_WAIT_ENV, DEFAULT_RETIREMENT_WAIT_SECONDS),
        _env_seconds(RETIREMENT_POLL_ENV, DEFAULT_RETIREMENT_POLL_SECONDS),
    )


def _finalize_ship(
    mailbox: Path,
    state_path: Path,
    iteration: int,
    repo: Path | None,
    *,
    context: str,
    runner: RoleRunner | None = None,
) -> int:
    """Accept a SHIP once retirement is verified, waiting a bounded time.

    Only rechecks the mailbox and git: no role is dispatched, and the
    caller keeps holding the loop lock. Before any wait the state is
    persisted as resumable ``needs_retirement`` so an interrupted
    driver resumes here (finalization recheck) instead of re-running
    the Lead or Evaluator. Final problems (stale attempt, product
    changes) stop at once; pending ones are polled until the deadline.
    Returns 0 (shipped) or 6 (needs_retirement).
    """
    problem = _ship_retirement_problem(mailbox, iteration, repo)
    waited = 0.0
    if problem is not None and problem[0] == RETIREMENT_PENDING:
        wait, poll = _retirement_wait_settings()
        if wait > 0:
            _update_state(
                state_path,
                {
                    "status": "needs_retirement",
                    "phase": "ship-awaiting-retirement",
                },
            )
            if runner is not None:
                _write_driver_state(
                    mailbox, runner, iteration, "ship-awaiting-retirement"
                )
            _append_log(
                mailbox,
                f"- iter {iteration} | loop | {context}: SHIP awaiting "
                f"retirement (up to {wait:g}s): {problem[1]}",
            )
            started = _retirement_clock()
            deadline = started + wait
            while problem is not None and problem[0] == RETIREMENT_PENDING:
                remaining = deadline - _retirement_clock()
                if remaining <= 0:
                    break
                _retirement_sleep(
                    min(poll, remaining) if poll > 0 else remaining
                )
                problem = _ship_retirement_problem(mailbox, iteration, repo)
            waited = max(0.0, _retirement_clock() - started)
    if problem is None:
        _update_state(state_path, {"status": "shipped", "phase": "shipped"})
        if waited:
            _append_log(
                mailbox,
                f"- iter {iteration} | loop | {context}: SHIP retirement "
                f"verified after {waited:.1f}s",
            )
        return 0
    kind, detail = problem
    _update_state(
        state_path,
        {"status": "needs_retirement", "phase": "ship-pending-retirement"},
    )
    if kind == RETIREMENT_PENDING:
        reason = f"retirement not found after waiting {waited:.1f}s"
    else:
        reason = "retirement cannot complete"
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | {context}: SHIP not accepted "
        f"({reason}): {detail}",
    )
    print(
        f"trio_loop: iteration {iteration} SHIP not accepted "
        f"({reason}): {detail}",
        file=sys.stderr,
    )
    _log_untracked_product_blockers(mailbox, iteration, repo)
    return 6


def _fresh_evaluator_artifact(
    mailbox: Path,
    iteration: int,
    context: dict | None = None,
) -> bool:
    """True when VERDICT.md is this attempt's artifact (no re-dispatch).

    Same-iteration leftover text is not enough: the unique attempt id
    (and pin, when one was dispatched) must match.
    """
    path = mailbox / "VERDICT.md"
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    word, _scope = _first_verdict(path)
    if word is None:
        return False
    # Appended open-loop slice sections carry their own field lines.
    text = _integration_verdict_text(text)
    if not _verdict_mentions_iteration(text, iteration):
        return False
    context = context or {}
    attempt = str(context.get("evaluator_attempt") or "").strip()
    if attempt and not _verdict_records_attempt(text, attempt):
        return False
    pinned = str(
        context.get("pinned_sha") or context.get("expected_sha") or ""
    ).strip()
    if pinned and not _verdict_records_sha(text, pinned):
        return False
    return bool(attempt)


def _apply_verdict(
    mailbox, state_path, repair_path, iteration, verdict, scope,
    repo: Path | None = None,
    runner: RoleRunner | None = None,
) -> int | None:
    """Persist a terminal verdict or queue the next ITERATE role.

    A SHIP without retirement bookkeeping is not ``shipped`` when the
    driver can inspect a git repo. A bound SHIP whose retirement commit
    is still landing is rechecked for a bounded time (``_finalize_ship``);
    otherwise status becomes ``needs_retirement`` (exit 6) so a missing
    mailbox retirement cannot look finished.
    Mailboxes used in tests without a git ``repo`` keep the historical
    shipped/exit-0 path.
    """
    if verdict == "SHIP":
        return _finalize_ship(
            mailbox, state_path, iteration, repo,
            context="verdict", runner=runner,
        )
    if verdict in OUTCOMES:
        status, phase, code = OUTCOMES[verdict]
        _update_state(state_path, {"status": status, "phase": phase})
        return code
    repairs = _counter(repair_path)
    if scope in (None, "design"):
        repair_path.write_text("0\n", encoding="utf-8")
    elif repairs < 2:
        repair_path.write_text(f"{repairs + 1}\n", encoding="utf-8")
    else:
        repair_path.write_text("0\n", encoding="utf-8")
        _append_log(
            mailbox,
            f"- iter {iteration} | loop | repair cap hit; "
            "forcing full Lead",
        )
    _update_state(state_path, {"status": "running", "phase": "idle"})
    return None


def _lockstep_eval_context(
    mailbox: Path,
    repo: Path | None,
    iteration: int,
    state: dict[str, str],
    state_path: Path,
) -> dict[str, str]:
    """Capture pin + unique attempt before Evaluator dispatch.

    Resume reuses the already-persisted attempt so a crash mid-wait
    still matches the same evidence. A new Lead pass must mint a new
    attempt after this helper is called with empty state keys.
    """
    del iteration
    attempt = state.get("evaluator_attempt", "").strip()
    pinned = state.get("evaluated_sha", "").strip()
    git_root = _git_root(repo)
    fresh = not pinned
    # Resume reuses evaluated_sha; never recapture HEAD as a
    # new product baseline (untracked files are not a pin).
    if not pinned and git_root is not None:
        pinned = _git_head(git_root) or ""
    if not attempt:
        attempt = uuid.uuid4().hex
    updates = {
        "evaluated_sha": pinned,
        "evaluator_attempt": attempt,
    }
    # r15: pin every declared repo alongside (reused on resume).
    declared = _declared_repos(mailbox)
    repo_pins = {} if fresh else _state_repo_pins(state)
    if declared:
        # Resume keeps every persisted pin, also one whose repo is no
        # longer declared: the retirement gate verifies from these pins
        # and refuses such a SHIP (eval-r15 B1).
        repo_pins = {
            **repo_pins,
            **{
                name: repo_pins.get(name) or _git_head(path) or ""
                for name, path in declared
            },
        }
        updates["evaluated_repos"] = _format_repo_pins(repo_pins)
    elif fresh:
        updates["evaluated_repos"] = ""  # clears a stale pin, never adds one
    _update_state(state_path, updates)
    context = {
        "pinned_sha": pinned,
        "expected_sha": pinned,
        "evaluator_attempt": attempt,
    }
    if declared:
        context["pins"] = {"home": pinned, **repo_pins}
    return context


def _open_loop_integration_context(
    mailbox: Path,
    repo: Path | None,
    iteration: int,
    state_path: Path,
) -> dict:
    """Bind an open-loop integration-eval to one exact pin and attempt.

    Same STATE.md keys as lockstep (``evaluated_sha``/``evaluator_attempt``)
    so ``_finalize_ship`` can verify the SHIP names this dispatch and the
    graded revision, and later acceptance consumers can bind to it. A
    persisted pair is reused only while the product tree is unchanged since
    that pin (resume of the same evaluation; mailbox-only commits such as
    the retirement do not count); otherwise HEAD is pinned with a new
    attempt, so an earlier ITERATE's pin can never be reused.
    """
    state = _read_state(state_path)
    attempt = state.get("evaluator_attempt", "").strip()
    pinned = state.get("evaluated_sha", "").strip()
    git_root = _git_root(repo)
    head = _git_head(git_root) if git_root is not None else None
    # r15: a `repos:` mailbox pins one sha per declared repo as well.
    declared = _declared_repos(mailbox)
    repo_pins = _state_repo_pins(state)
    reuse = bool(attempt and pinned)
    if reuse and git_root is not None:
        reuse = _evaluated_product_intact(git_root, state_path.parent, pinned)
    if reuse and (declared or repo_pins):
        # eval-r15 B1: reuse is verified from the persisted pins; an
        # invalid block or a pinned repo no longer declared never reuses.
        _checked, unverifiable = _pinned_repos_problem(mailbox, repo_pins)
        reuse = unverifiable is None and all(
            repo_pins.get(name)
            and _evaluated_product_intact(path, mailbox, repo_pins[name])
            for name, path in declared
        )
    if not reuse:
        attempt = uuid.uuid4().hex
        pinned = head or ""
        repo_pins = {name: _git_head(path) or "" for name, path in declared}
    updates = {"evaluated_sha": pinned, "evaluator_attempt": attempt}
    if declared or not reuse:
        # A fresh pin always rewrites the key (an empty value clears a
        # stale pin of a repo no longer declared; never adds one).
        updates["evaluated_repos"] = _format_repo_pins(
            {name: repo_pins.get(name, "") for name, _path in declared}
        )
    _update_state(state_path, updates)
    context = {
        "mode": "open-loop",
        "slice": None,
        "sha": pinned or None,
        "kind": "integration-eval",
        "pinned_sha": pinned,
        "expected_sha": pinned,
        "evaluator_attempt": attempt,
        "iteration": iteration,
    }
    if declared:
        context["pins"] = {
            "home": pinned,
            **{name: repo_pins.get(name, "") for name, _path in declared},
        }
    return context


def _run_lockstep(
    mailbox: Path,
    max_iterations: int,
    runner: RoleRunner,
    *,
    repo: Path | None = None,
) -> int:
    """Run the durable Lead/repair -> Evaluator state machine.

    This is the lockstep body from before open-loop mode existed, kept
    behaviourally byte-identical: same STATE.md keys, same LOG.md lines,
    same exit codes, same .driver.json writes (never .session.json).
    """
    mailbox = Path(mailbox).resolve()
    lock = _acquire_lock(mailbox)
    if lock is None:
        return 5
    try:
        state_path = mailbox / "STATE.md"
        if not (mailbox / "LOG.md").is_file():
            (mailbox / "LOG.md").write_text(
                "# Trio loop log\n", encoding="utf-8"
            )
        state = _read_state(state_path)
        repair_path = mailbox / ".repairs"
        _write_driver_state(
            mailbox,
            runner,
            _number(state["iteration"]),
            state["phase"].strip(),
        )
        while True:
            status = state["status"].strip().lower()
            status_word = status.split()[0] if status else ""
            terminal = TERMINAL_CODES.get(status_word)
            iteration = _number(state["iteration"])
            if terminal is not None:
                # needs_retirement is resumable: a later real mailbox
                # retirement must finish without re-running Evaluator.
                # Recheck finalization first (bounded wait included);
                # never re-dispatch a role from here.
                if status_word == "needs_retirement":
                    _write_driver_state(
                        mailbox, runner, iteration, state["phase"].strip()
                    )
                    code = _finalize_ship(
                        mailbox, state_path, iteration, repo,
                        context="resume", runner=runner,
                    )
                    _write_driver_state(
                        mailbox,
                        runner,
                        iteration,
                        _read_state(state_path)["phase"].strip(),
                    )
                    return code
                _write_driver_state(
                    mailbox, runner, iteration, state["phase"].strip()
                )
                return terminal
            # Crash-resume: Lead+gates already landed, so do not bump
            # iteration or re-run Lead (fixes driver.sh:107-108).
            if state["phase"].strip().lower() == "lead-done":
                context = _lockstep_eval_context(
                    mailbox, repo, iteration, state, state_path
                )
                # Skip Evaluator only when the artifact names this
                # attempt and pin; leftover same-iteration SHIP is not.
                if not _fresh_evaluator_artifact(
                    mailbox, iteration, context
                ):
                    result = _invoke_runner(
                        runner,
                        "evaluator",
                        iteration,
                        mailbox,
                        context,
                    )
                    if result != 0:
                        raise RuntimeError(
                            f"evaluator runner failed with exit {result}"
                        )
                verdict, scope = _first_verdict(mailbox / "VERDICT.md")
                if verdict is None:
                    _update_state(
                        state_path, {"status": "error", "phase": "error"}
                    )
                    _append_log(
                        mailbox,
                        f"- iter {iteration} | loop | unparseable verdict",
                    )
                    _write_driver_state(mailbox, runner, iteration, "error")
                    return 3
                code = _apply_verdict(
                    mailbox,
                    state_path,
                    repair_path,
                    iteration,
                    verdict,
                    scope,
                    repo=repo,
                    runner=runner,
                )
                state = _read_state(state_path)
                _write_driver_state(
                    mailbox,
                    runner,
                    _number(state["iteration"]),
                    state["phase"].strip(),
                )
                if code is not None:
                    return code
                continue
            phase = state["phase"].strip().lower()
            if phase in ("lead-running", "repair-running"):
                role = phase.split("-", 1)[0]
            else:
                # Budget applies to new passes, not interrupted role retries.
                if iteration >= max_iterations:
                    return 4
                role = "repair" if _counter(repair_path) else "lead"
                iteration += 1
            _update_state(
                state_path,
                {
                    "iteration": str(iteration),
                    "status": "running",
                    "phase": f"{role}-running",
                    "evaluator_attempt": "",
                    "evaluated_sha": "",
                    "evaluated_repos": "",
                },
            )
            _write_driver_state(mailbox, runner, iteration, f"{role}-running")
            if not _run_role(
                mailbox, iteration, role, runner, repo, state_path
            ):
                state = _read_state(state_path)
                _write_driver_state(
                    mailbox,
                    runner,
                    _number(state["iteration"]),
                    state["phase"].strip(),
                )
                return 3
            _update_state(
                state_path, {"status": "running", "phase": "lead-done"}
            )
            _write_driver_state(mailbox, runner, iteration, "lead-done")
            state = _read_state(state_path)
    finally:
        _release_lock(lock)


def run_loop(
    mailbox: Path,
    max_iterations: int,
    runner: RoleRunner,
    *,
    repo: Path | None = None,
    poll_seconds: float = 30,
    mode: str = "auto",
    slice_eval_concurrency: int = 1,
    slice_eval_drain_seconds: float | None = None,
) -> int:
    """Dispatch to open-loop or lockstep (api: engine entry points).

    ``slice_eval_concurrency`` (open-loop only; default 1 = the serial
    slice-eval loop) and ``slice_eval_drain_seconds`` (None = default
    budget) are passed through to `run_open_loop`.

    `mode="auto"` (default) selects open-loop iff `(mailbox / "QUEUE.md")`
    is a file; `mode="lockstep"` always takes the lockstep path (byte
    identical to the pre-open-loop behaviour); `mode="open-loop"` forces
    open-loop and, when QUEUE.md is missing, prints a message naming it to
    stderr and returns 3 WITHOUT touching STATE.md. `run_loop` passes the
    same `runner` object as both the open-loop lead_runner and eval_runner.
    """
    if mode not in ("auto", "open-loop", "lockstep"):
        raise ValueError(f"unknown mode: {mode!r} (expected auto/open-loop/lockstep)")
    mailbox_path = Path(mailbox).resolve()
    has_queue = (mailbox_path / "QUEUE.md").is_file()
    if mode == "lockstep" or (mode == "auto" and not has_queue):
        return _run_lockstep(mailbox, max_iterations, runner, repo=repo)
    if not has_queue:
        print(
            f"open-loop mode requires QUEUE.md in {mailbox_path}, "
            "but it was not found.",
            file=sys.stderr,
        )
        return 3
    extra: dict = {}
    if slice_eval_concurrency != 1:
        extra["slice_eval_concurrency"] = slice_eval_concurrency
    if slice_eval_drain_seconds is not None:
        extra["slice_eval_drain_seconds"] = slice_eval_drain_seconds
    return run_open_loop(
        mailbox,
        max_iterations,
        runner,
        runner,
        repo=repo,
        poll_seconds=poll_seconds,
        **extra,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_open_loop_sidecars(
    mailbox: Path,
    lead_runner: RoleRunner,
    eval_runner: RoleRunner,
    iteration: int,
    phase: str,
    lead_alive: bool,
    eval_alive: bool,
    started_at: str,
    evaluator_sessions: dict | None = None,
) -> None:
    """Write BOTH sidecars for an open-loop driver (api:OpenLoopSidecar).

    `.driver.json` keeps its pre-open-loop keys (pid/iteration/phase/
    session_ids) plus open_loop/lead_alive/eval_alive. `.session.json` is
    open-loop only (lockstep never writes it); `phase` is exactly one of
    "lead" | "evaluator" | "done" in both files.

    With concurrent slice-evals (``evaluator_sessions`` not None),
    `.driver.json` also carries ``evaluator_sessions``:
    ``{"<slice>@<sha>": "<session id>" | null}`` for every in-flight
    slice-eval (null until its session exists); ``session_ids.evaluator``
    stays the last-dispatched evaluator session.
    """
    session_ids: dict = {}
    session_ids.update(getattr(lead_runner, "session_ids", {}) or {})
    session_ids.update(getattr(eval_runner, "session_ids", {}) or {})
    driver_payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "session_ids": session_ids,
        "open_loop": True,
        "lead_alive": lead_alive,
        "eval_alive": eval_alive,
    }
    if evaluator_sessions is not None:
        driver_payload["evaluator_sessions"] = dict(evaluator_sessions)
    for key, value in _driver_meta(lead_runner, eval_runner).items():
        driver_payload.setdefault(key, value)
    (mailbox / ".driver.json").write_text(
        json.dumps(driver_payload) + "\n", encoding="utf-8"
    )
    session_payload = {
        "pid": os.getpid(),
        "iteration": iteration,
        "phase": phase,
        "open_loop": True,
        "lead_alive": lead_alive,
        "eval_alive": eval_alive,
        "started_at": started_at,
    }
    (mailbox / ".session.json").write_text(
        json.dumps(session_payload) + "\n", encoding="utf-8"
    )


_LOGGED_PLAN_PARSE_ERRORS: dict[str, str] = {}


def _read_plan_slice_ids(mailbox: Path) -> list[str] | None:
    """Slice ids declared in PLAN.md's `slices:` block.

    Returns None for "unknown" -- PLAN.md is missing, has no `slices:`
    block yet (the normal fresh-mailbox state, before the Lead has ever
    written a plan), or the block exists but fails to parse. Callers MUST
    treat None (and the empty list) as "not retired", never as "no slices
    to retire" -- an unknown plan must not look done. A parse failure
    (block found but malformed) is additionally logged once to LOG.md so
    it doesn't get silently mistaken for a plan-not-written-yet state.
    """
    plan_path = mailbox / "PLAN.md"
    if not plan_path.is_file():
        return None
    text = plan_path.read_text(encoding="utf-8", errors="replace")
    try:
        block = _METRICS.find_slices_block(text)
    except _METRICS.SliceParseError:
        return None
    try:
        slices = _METRICS.parse_slices(block)
    except _METRICS.SliceParseError as exc:
        key = str(plan_path.resolve())
        msg = str(exc)
        if _LOGGED_PLAN_PARSE_ERRORS.get(key) != msg:
            _LOGGED_PLAN_PARSE_ERRORS[key] = msg
            _append_log(
                mailbox,
                f"open-loop: PLAN.md slices block unreadable — {msg}",
            )
        return None
    return [sl["id"] for sl in slices]


def _slices_fully_retired(
    slice_ids: list[str] | None,
    retired_ids: set[str],
    open_or_taken: list,
) -> bool:
    """True only when the plan declares >=1 slice and every declared slice
    has a retired QUEUE.md entry, with no fault open/taken.

    `slice_ids` of None or [] (plan absent, no `slices:` block yet, or
    unparseable) is never "fully retired" -- a fresh or broken mailbox
    must not look done before the Lead has ever run.
    """
    if not slice_ids:
        return False
    return all(sid in retired_ids for sid in slice_ids) and not open_or_taken


def _malformed_retired_slices(queue: dict) -> set[str]:
    """Slice ids with a malformed QUEUE.md `retired:` entry.

    Such a slice is never gated as retired -- not even when an older valid
    entry for it survives the lenient parse -- until the entry is repaired:
    the malformed entry is most likely the re-retirement of a fix, and
    trusting the older (already graded) sha would skip the fix's slice-eval
    and start the integration eval (r11 queue-harden F1). With the Lead
    making no progress the 3-no-op stall guard ends the run
    `status: error`. `.get`: an older trio-metrics has no such key."""
    return set(queue.get("malformed_slices") or [])


RETIRED_REPO_ERROR_PREFIX = "`retired:` block:"

#: (repo path, sha) pairs already proven to be commits (they stay commits).
_KNOWN_REPO_COMMITS: set[tuple[str, str]] = set()


def _plan_slice_repos(mailbox: Path) -> dict[str, str] | None:
    """``{slice id: PLAN.md repo:}`` with `.`/`home`/omitted as ``home``;
    None when PLAN.md is missing or its slices block does not parse."""
    try:
        text = (Path(mailbox) / "PLAN.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    slices = _METRICS.parse_slices_block(text)
    if slices is None:
        return None
    return {sl["id"]: _repo_value(sl.get("repo")) for sl in slices}


def _repo_value(value) -> str:
    """A `repo:` value with the home spellings (omitted, `.`, `./`, `home`)
    folded to ``home``; MAILBOX-SCHEMA.md: an omitted `repo:` is home."""
    value = str(value or "").strip()
    return "home" if value in ("", ".", "./", "home") else value


def _retired_repo_problems(mailbox: Path, queue: dict) -> list[tuple[dict, str]]:
    """Retired entries whose `repo:`/`sha:` the driver must not grade (r15).

    PLAN.md is authoritative (eval-r15 N1): an entry's `repo:` (omitted =
    home) must equal its slice's PLAN.md `repo:`, and a declared repo must
    still be declared and valid. Its sha must be a commit of that repo
    (eval-r15 N6: a wrong-repo or phantom sha fails at retire parse, not
    three dispatches later). A single-repo mailbox whose entries carry no
    `repo:` is never affected. Returns ``[(entry, message)]``.
    """
    retired = queue.get("retired") or []
    declared, errors = _declared_repos_checked(mailbox)
    tagged = [e for e in retired if _repo_value(e.get("repo")) != "home"]
    if not declared and not errors and not tagged:
        return []
    plan = _plan_slice_repos(mailbox)
    if plan is None:
        return []  # the per-slice commit gate reports an unreadable PLAN
    paths = {name: path for name, path in declared}
    problems: list[tuple[dict, str]] = []
    for entry in retired:
        slice_id = entry.get("slice", "")
        if slice_id not in plan:
            continue  # unknown slice: the per-slice commit gate exits 2
        sha = str(entry.get("sha", "")).strip()
        entry_repo = _repo_value(entry.get("repo"))
        plan_repo = plan[slice_id]
        where = f"entry for slice {slice_id} (sha {sha[:12]})"
        if entry_repo != plan_repo:
            problems.append((entry, (
                f"{where} has repo: {entry_repo} but PLAN.md puts slice "
                f"{slice_id} in repo {plan_repo} (an omitted repo: is home); "
                "PLAN.md is authoritative -- re-retire it with the PLAN repo"
            )))
            continue
        if entry_repo == "home":
            continue
        path = paths.get(entry_repo)
        if path is None:
            problems.append((entry, (
                f"{where} names repo {entry_repo}, which is not a valid "
                "declared PLAN.md repos: entry"
                + (f" ({errors[0]})" if errors else "")
            )))
            continue
        key = (str(path), sha)
        if key in _KNOWN_REPO_COMMITS:
            continue
        if _git_commit_sha(path, sha) is None:
            problems.append((entry, (
                f"{where}: sha {sha} is not a commit of repo {entry_repo} "
                f"({path}); retire the slice's merge commit in its own repo"
            )))
            continue
        _KNOWN_REPO_COMMITS.add(key)
    return problems


def _read_queue(mailbox: Path) -> dict:
    """``read_queue`` plus the r15 retired `repo:`/`sha:` checks.

    An entry of ``_retired_repo_problems`` is held exactly like any other
    malformed `retired:` entry: dropped from ``retired``, its slice listed
    in ``malformed_slices`` (never gated as retired, never dispatched), and
    its message appended to ``errors`` (logged once per iteration).
    """
    queue = _METRICS.read_queue(mailbox)
    problems = _retired_repo_problems(mailbox, queue)
    if not problems:
        return queue
    bad = {id(entry) for entry, _msg in problems}
    queue = dict(queue)
    queue["retired"] = [e for e in queue["retired"] if id(e) not in bad]
    queue["malformed_slices"] = sorted(
        set(queue.get("malformed_slices") or [])
        | {entry.get("slice", "") for entry, _msg in problems}
    )
    queue["errors"] = [
        *(queue.get("errors") or []),
        *(f"{RETIRED_REPO_ERROR_PREFIX} {msg}" for _entry, msg in problems),
    ]
    return queue


def _gate_retired_ids(queue: dict) -> set[str]:
    """Slice ids `_slices_fully_retired` may count as retired: every slice
    with a valid `retired:` entry, minus `_malformed_retired_slices`."""
    return {
        e["slice"] for e in queue["retired"]
    } - _malformed_retired_slices(queue)


# MAILBOX-SCHEMA.md `faults:` `status` values. `open`/`taken` are live;
# `done`/`stale` are closed. Anything else is UNKNOWN and treated as live
# (fail-closed): a typo or a mangled value must block the integration gate,
# never silently unblock it (r11 N1 gate hardening).
FAULT_STATUSES = ("open", "taken", "done", "stale")
CLOSED_FAULT_STATUSES = ("done", "stale")


def _live_faults(queue: dict) -> list:
    """Faults that block `_slices_fully_retired`: status `open`/`taken`, or
    any status outside FAULT_STATUSES (see `_unknown_status_faults`)."""
    return [
        f for f in queue["faults"]
        if f.get("status") not in CLOSED_FAULT_STATUSES
    ]


def _unknown_status_faults(queue: dict) -> list:
    """Faults whose `status` is not one of FAULT_STATUSES."""
    return [
        f for f in queue["faults"] if f.get("status") not in FAULT_STATUSES
    ]


QUEUE_FAULT_ERROR_PREFIX = "`faults:` block:"


def _queue_fault_errors(queue: dict) -> list[str]:
    """QUEUE.md parse errors of the `faults:` block (dropped/unparseable
    faults, a second or mis-fenced `faults:` block), in parse order."""
    return [
        str(e) for e in (queue.get("errors") or [])
        if str(e).startswith(QUEUE_FAULT_ERROR_PREFIX)
    ]


def _integration_gate_held(
    mailbox: Path,
    iteration: int,
    queue: dict,
    logged: set | None = None,
    lock: threading.Lock | None = None,
) -> bool:
    """True while the `faults:` block has a parse error (r11g Q1).

    A dropped fault is invisible to `_live_faults`, so the integration
    gate must stay closed until the text is repaired: every gate site
    checks `_slices_fully_retired(...) and not _integration_gate_held(...)`.
    The `- iter N | loop | gate held: ...` LOG line (naming the first
    error) is written at most once per iteration -- `logged` holds the
    iterations already logged and is shared by the Lead thread and the
    poll loop, guarded by `lock`. The Lead gets the error text through
    the lead-pass OPEN-LOOP CONTEXT (`queue_errors`); a Lead that cannot
    repair it makes no-op passes and the 3-no-op stall guard ends the run
    `status: error`."""
    errs = _queue_fault_errors(queue)
    if not errs:
        return False
    if logged is not None:
        with lock if lock is not None else contextlib.nullcontext():
            if iteration in logged:
                return True
            logged.add(iteration)
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | gate held: QUEUE.md faults block has "
        f"parse errors: {errs[0]}",
    )
    return True


def _log_queue_errors(
    mailbox: Path, iteration: int, queue: dict, logged: set
) -> None:
    """Log each QUEUE.md parse error at most once per turn (iteration).

    `read_queue` never raises: a malformed entry is dropped and described
    in `queue["errors"]` while the valid entries survive. The driver must
    not stay silent about the dropped ones -- a dropped fault is invisible
    to the integration gate -- so each distinct message is appended to
    LOG.md once for the current iteration (`logged` holds the
    `(iteration, message)` pairs already written; the poll loop re-reads
    QUEUE.md every poll and must not spam LOG.md). Each slice in
    `_malformed_retired_slices` also gets one
    `QUEUE.md: slice <id> has a malformed retired entry; not gated as
    retired` line per iteration, and each fault with a status outside
    FAULT_STATUSES one `QUEUE.md: fault <id> has unknown status '<value>';
    treated as open` line per iteration (see `_live_faults`)."""
    for msg in queue.get("errors") or []:
        key = (iteration, msg)
        if key in logged:
            continue
        logged.add(key)
        _append_log(
            mailbox, f"- iter {iteration} | loop | QUEUE.md parse error: {msg}"
        )
    for sid in sorted(_malformed_retired_slices(queue)):
        msg = (
            f"QUEUE.md: slice {sid} has a malformed retired entry; "
            "not gated as retired"
        )
        key = (iteration, msg)
        if key in logged:
            continue
        logged.add(key)
        _append_log(mailbox, f"- iter {iteration} | loop | {msg}")
    for fault in _unknown_status_faults(queue):
        msg = (
            f"QUEUE.md: fault {fault.get('id')} has unknown status "
            f"'{fault.get('status')}'; treated as open"
        )
        key = (iteration, msg)
        if key in logged:
            continue
        logged.add(key)
        _append_log(mailbox, f"- iter {iteration} | loop | {msg}")


def _git_head_sha(repo_dir: Path) -> str | None:
    """HEAD commit sha, or None when `repo_dir` is not a git repo (or git
    itself is unavailable) -- part of the open-loop Lead-pass snapshot, see
    `_lead_pass_snapshot`."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_slice_commit_shas(repo_dir: Path) -> frozenset[str]:
    """Shas of every commit whose message starts with ``slice(`` -- see
    `_git_head_sha`."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--grep=^slice("],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
    except OSError:
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


def _lead_pass_snapshot(mailbox: Path, repo: Path | None) -> tuple:
    """Everything one Lead pass could plausibly change: PLAN.md bytes,
    QUEUE.md `retired:` entries and fault statuses, the repo's HEAD sha, and
    its set of `slice(...)` commits. Two equal snapshots taken before and
    after a runner call mean the pass wrote nothing at all -- an exit-0
    return is not on its own proof the role did its job (open-loop
    Lead-pass output verification, mirroring the existing Evaluator
    verification below)."""
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    plan_path = mailbox / "PLAN.md"
    plan_bytes = plan_path.read_bytes() if plan_path.is_file() else None
    queue = _METRICS.read_queue(mailbox)
    retired = frozenset(
        (entry["slice"], entry["sha"]) for entry in queue["retired"]
    )
    fault_status = frozenset(
        (fault["id"], fault["status"]) for fault in queue["faults"]
    )
    snapshot = (
        plan_bytes,
        retired,
        fault_status,
        _git_head_sha(target),
        _git_slice_commit_shas(target),
    )
    declared = _declared_repos(mailbox)
    if declared:
        # r15: a pass that only commits in a declared repo did something.
        snapshot += tuple(
            (name, _git_head_sha(path), _git_slice_commit_shas(path))
            for name, path in declared
        )
    return snapshot


def _lead_thread_body(
    mailbox: Path,
    state_path: Path,
    lead_runner: RoleRunner,
    max_iterations: int,
    write_sidecar,
    wake_event: threading.Event,
    stop_event: threading.Event,
    result_holder: dict,
    force_first_pass: bool = False,
    repo: Path | None = None,
    gate_held_logged: set | None = None,
    gate_held_lock: threading.Lock | None = None,
) -> None:
    """One Lead-thread lifetime: run passes until every PLAN.md slice has a
    retired entry and no fault is open/taken, or the pass budget caps.

    A pass only bumps `iteration` (and only then counts against
    `max_iterations`) when it actually changed something -- see
    `_lead_pass_snapshot`. A pass that changed nothing is logged and
    retried in place (same candidate iteration, `iteration` left
    uncommitted); after 3 consecutive empty passes the thread sets
    STATE.md `status: error`, logs the failure, and ends with
    `outcome: "stalled"` instead of retrying forever.

    `result_holder` receives exactly one of `outcome` ("done"/"capped"/
    "stopped"/"stalled") or `error` (the exception the runner raised), plus
    `finished` (set in `finally`, after outcome/error, so the poll loop can
    treat `finished` as the authoritative "thread has ended" signal instead
    of racing `Thread.is_alive()`).
    """
    try:
        first = True
        empty_attempts = 0
        while True:
            if stop_event.is_set():
                result_holder["outcome"] = "stopped"
                return
            if not (first and force_first_pass):
                slice_ids = _read_plan_slice_ids(mailbox)
                queue = _read_queue(mailbox)
                retired_ids = _gate_retired_ids(queue)
                open_or_taken = _live_faults(queue)
                if _slices_fully_retired(
                    slice_ids, retired_ids, open_or_taken
                ) and not _integration_gate_held(
                    mailbox,
                    _number(_read_state(state_path)["iteration"]),
                    queue,
                    gate_held_logged,
                    gate_held_lock,
                ):
                    result_holder["outcome"] = "done"
                    return
            first = False
            state = _read_state(state_path)
            iteration = _number(state["iteration"]) + 1
            if iteration > max_iterations:
                result_holder["outcome"] = "capped"
                return
            _update_state(state_path, {"status": "running"})
            write_sidecar("lead", iteration, True, True)
            context = {
                "mode": "open-loop",
                "slice": None,
                "sha": None,
                "kind": "lead-pass",
            }
            # r11g Q1: a held gate's error text reaches the Lead through
            # its OPEN-LOOP CONTEXT (the Lead never reads LOG.md). The key
            # is only present when there is something to repair.
            queue_errors = _queue_fault_errors(_read_queue(mailbox))
            if queue_errors:
                context["queue_errors"] = queue_errors
            snapshot_before = _lead_pass_snapshot(mailbox, repo)
            result = _invoke_runner(lead_runner, "lead", iteration, mailbox, context)
            if result != 0:
                raise RuntimeError(f"lead runner failed with exit {result}")
            if _lead_pass_snapshot(mailbox, repo) == snapshot_before:
                empty_attempts += 1
                _append_log(
                    mailbox,
                    f"- iter {iteration} | loop | open-loop: lead pass made "
                    f"no changes (attempt {empty_attempts})",
                )
                if empty_attempts >= 3:
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {iteration} | loop | open-loop: lead pass "
                        f"made no changes after {empty_attempts} attempts",
                    )
                    result_holder["outcome"] = "stalled"
                    return
                continue
            empty_attempts = 0
            _update_state(
                state_path, {"iteration": str(iteration), "status": "running"}
            )
    except BaseException as exc:  # noqa: BLE001 - propagated to the poll loop
        result_holder["error"] = exc
    finally:
        result_holder["finished"] = True
        wake_event.set()


def _per_slice_gate(mailbox: Path, repo: Path | None, slice_id: str) -> int:
    """v1 open-loop per-slice commit gate; returns the raw trio-shadow exit
    code (0 pass, 1 missing commits, 2 malformed/unknown-slice/error)."""
    script = Path(__file__).resolve().with_name("trio-shadow.py")
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    command = [
        sys.executable,
        str(script),
        "--mailbox",
        str(target.resolve()),
        "--require-commits",
        "--slice",
        slice_id,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return 2
    return result.returncode


#: Drain budget for in-flight slice-evals when the runner exposes no
#: per-dispatch timeout (OmnigentRunner's default role timeout).
DEFAULT_SLICE_EVAL_DRAIN_SECONDS = 3600.0

#: Default cap on the exit drain: a Ctrl-C/SIGTERM must not block for a
#: whole role timeout before prune. Override: `--slice-eval-drain-seconds`
#: or ``TRIO_SLICE_EVAL_DRAIN_SECONDS``.
SLICE_EVAL_DRAIN_CAP_SECONDS = 120.0
SLICE_EVAL_DRAIN_ENV = "TRIO_SLICE_EVAL_DRAIN_SECONDS"

#: Held-record kind for a slice-eval still running when the drain gave up.
ABANDONED_ON_EXIT_HOLD = "abandoned_on_exit"


def _drain_seconds_value(value: object) -> float | None:
    """A finite, non-negative number of seconds, else None."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _slice_eval_drain_seconds(
    eval_runner: object, override: float | None = None
) -> float:
    """Exit-drain budget for running slice-evals.

    Precedence: explicit *override* (the loop flag), then
    ``TRIO_SLICE_EVAL_DRAIN_SECONDS``, then min(runner role timeout, 120 s).
    An invalid override/env value falls through to the next source.
    """
    for candidate in (override, os.environ.get(SLICE_EVAL_DRAIN_ENV)):
        if candidate is None or candidate == "":
            continue
        seconds = _drain_seconds_value(candidate)
        if seconds is not None:
            return seconds
    timeout = DEFAULT_SLICE_EVAL_DRAIN_SECONDS
    for name in ("timeout", "_timeout"):
        value = getattr(eval_runner, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and value >= 0:
                timeout = float(value)
                break
    return min(timeout, SLICE_EVAL_DRAIN_CAP_SECONDS)


def _write_abandoned_hold(
    mailbox: Path, session_id: str, slice_id: str, sha: str,
    iteration: int, budget: float,
) -> Path:
    """Durable ``.sessions/held-<sid>.json`` (same shape as trioctl's holds)
    for a slice-eval the exit drain abandoned: it may still be running, so
    the next resume is refused until a person resolves it."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", session_id).strip("-") or "session"
    path = mailbox / ".sessions" / f"held-{slug}.json"
    record = {
        "session_id": session_id,
        "role": "evaluator",
        "iteration": iteration,
        "hold": ABANDONED_ON_EXIT_HOLD,
        "kind": "slice-eval",
        "slice": slice_id,
        "sha": sha,
        "pinned_sha": sha,
        "reason": (
            f"slice-eval still running after the {budget:g}s exit drain "
            "budget; abandoned on loop exit"
        ),
        "recorded_at": _iso_now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _merge_slice_blocks(known: dict, text: str) -> None:
    """Remember every `## slice ... — SHIP|ITERATE` block seen in *text*."""
    for block in _slice_verdict_blocks(text):
        known.setdefault((block["slice"], block["sha"]), block["text"])


def run_open_loop(
    mailbox: Path,
    max_iterations: int,
    lead_runner: RoleRunner,
    eval_runner: RoleRunner,
    *,
    repo: Path | None = None,
    poll_seconds: float = 30,
    slice_eval_concurrency: int = 1,
    slice_eval_drain_seconds: float | None = None,
) -> int:
    """Run the open-loop Lead+Evaluator state machine against a QUEUE.md
    mailbox: one stdlib Lead thread plus an Evaluator poll loop on the
    calling thread, sharing the ONE mailbox lock this function acquires.

    ``slice_eval_concurrency`` 1 (default) grades retired slices one after
    another on the calling thread (unchanged). N>1 dispatches up to N
    slice-evals at once on a bounded thread pool: every polling turn
    submits one future per retired, ungraded, not-in-flight
    ``(slice, sha)`` (commit gate still run first, on this thread), then
    harvests finished futures and applies the same post-processing
    (clobber restore, verdict-section check, graded/gate_blocked/
    slice_eval_attempts/eval_pending) on this thread only. An in-flight
    slice-eval blocks the integration-eval exactly like a pending one; the
    integration gate is re-checked right after each harvest. On any exit,
    queued futures are cancelled and running ones are waited for up to
    ``slice_eval_drain_seconds`` (default: ``TRIO_SLICE_EVAL_DRAIN_SECONDS``,
    else min(eval runner role timeout, 120 s)). A slice-eval still running
    after that is abandoned: logged, and held (``held-<sid>.json``, hold
    ``abandoned_on_exit``) when its session id is known.
    """
    if not isinstance(slice_eval_concurrency, int) or slice_eval_concurrency < 1:
        raise ValueError(
            f"slice_eval_concurrency must be an int >= 1, got {slice_eval_concurrency!r}"
        )
    mailbox = Path(mailbox).resolve()
    lock = _acquire_lock(mailbox)
    if lock is None:
        return 5
    lead_thread: threading.Thread | None = None
    stop_event = threading.Event()
    concurrent_evals = slice_eval_concurrency > 1
    pool: concurrent.futures.ThreadPoolExecutor | None = None
    # (slice, sha) -> {"future", "snapshot", "seen"}; outer thread only.
    inflight: dict[tuple[str, str], dict] = {}
    drain_budget = _slice_eval_drain_seconds(eval_runner, slice_eval_drain_seconds)
    verdict_path = mailbox / "VERDICT.md"

    def evaluator_sessions() -> dict | None:
        """In-flight slice-evals -> session id (None until created)."""
        if not concurrent_evals:
            return None
        snapshot_fn = getattr(eval_runner, "inflight_sessions", None)
        live: dict = {}
        if callable(snapshot_fn):
            try:
                for sid, meta in (snapshot_fn() or {}).items():
                    if isinstance(meta, dict) and meta.get("kind") == "slice-eval":
                        live[f"{meta.get('slice')}@{meta.get('sha')}"] = sid
            except Exception:  # noqa: BLE001 - a sidecar must not fail the loop
                live = {}
        return {
            f"{slice_id}@{sha}": live.get(f"{slice_id}@{sha}")
            for (slice_id, sha) in list(inflight)
        }

    def drain_slice_evals() -> None:
        """Cancel queued slice-evals; wait (bounded) for running ones.

        Interrupt-safe: a second signal inside the wait still runs the
        bookkeeping below (clobber restore, "abandoned" log, held records)
        before the interrupt propagates.

        An abandoned eval keeps running on its (non-daemon) pool thread;
        this function returns without waiting for it, but the interpreter's
        exit hook still joins it, so the process lingers until it finishes
        unless the caller exits with `os._exit` after cleanup (as
        `trioctl omnigent loop` does).
        """
        nonlocal pool
        if pool is None:
            return
        entries = list(inflight.items())
        running: list = []
        interrupted = True
        try:
            for _key, entry in entries:
                entry["future"].cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            running = [e["future"] for _k, e in entries if not e["future"].cancelled()]
            concurrent.futures.wait(running, timeout=drain_budget)
            interrupted = False
        finally:
            not_done = {f for f in running if not f.done()}
            try:
                iteration_now = _number(_read_state(mailbox / "STATE.md")["iteration"])
            except Exception:  # noqa: BLE001 - best effort on exit
                iteration_now = 0
            for (slice_id, sha), entry in entries:
                future = entry["future"]
                if future.cancelled() or not future.done():
                    continue
                if future.exception() is None and future.result() == 0:
                    # The loop is exiting: bookkeeping no longer matters, but a
                    # finished eval's clobber restore still protects VERDICT.md.
                    try:
                        _restore_clobbered_verdict_sections(
                            mailbox, verdict_path, "".join(entry["seen"].values()),
                            "slice-eval", iteration_now, read_text=_read_text_settled,
                        )
                    except Exception:  # noqa: BLE001 - best effort on exit
                        pass
            inflight.clear()
            pool = None
            if not_done:
                why = (
                    "when the drain was interrupted"
                    if interrupted
                    else f"after the {drain_budget:g}s drain budget"
                )
                _append_log(
                    mailbox,
                    f"- iter {iteration_now} | loop | open-loop: {len(not_done)} "
                    f"slice-eval(s) still running {why}; abandoned",
                )
                hold_abandoned(entries, not_done, iteration_now)

    def hold_abandoned(entries: list, not_done, iteration_now: int) -> None:
        """Held record per abandoned slice-eval whose session id is known."""
        try:
            live = getattr(eval_runner, "inflight_sessions", None)
            snapshot = (live() or {}) if callable(live) else {}
        except Exception:  # noqa: BLE001 - best effort on exit
            snapshot = {}
        by_key = {
            (meta.get("slice"), meta.get("sha")): sid
            for sid, meta in snapshot.items()
            if isinstance(meta, dict) and meta.get("kind") == "slice-eval"
        }
        for (slice_id, sha), entry in entries:
            if entry["future"] not in not_done:
                continue
            session_id = by_key.get((slice_id, sha))
            if not session_id:
                continue
            try:
                path = _write_abandoned_hold(
                    mailbox, str(session_id), slice_id, sha,
                    iteration_now, drain_budget,
                )
            except OSError as exc:
                _append_log(
                    mailbox,
                    f"- iter {iteration_now} | loop | open-loop: could NOT "
                    f"hold abandoned slice-eval {slice_id} session "
                    f"{session_id} ({exc}); do not resume until it is resolved",
                )
                continue
            _append_log(
                mailbox,
                f"- iter {iteration_now} | loop | held evaluator session "
                f"{session_id}: slice-eval {slice_id} abandoned on exit; "
                f"see {path.name}",
            )

    try:
        state_path = mailbox / "STATE.md"
        repair_path = mailbox / ".repairs"
        if not (mailbox / "LOG.md").is_file():
            (mailbox / "LOG.md").write_text(
                "# Trio loop log\n", encoding="utf-8"
            )
        _read_state(state_path)
        started_at = _iso_now()

        # The Lead thread writes the sidecars too: with concurrent evals
        # snapshot+write happen under one lock and the last-written
        # evaluator_sessions is remembered, so the driver thread can tell
        # when `.driver.json` is stale.
        sidecar_lock = threading.Lock()
        written_sessions: list = [{}]

        def write_sidecar(
            phase: str, iteration: int, lead_alive: bool, eval_alive: bool
        ) -> None:
            if not concurrent_evals:
                _write_open_loop_sidecars(
                    mailbox,
                    lead_runner,
                    eval_runner,
                    iteration,
                    phase,
                    lead_alive,
                    eval_alive,
                    started_at,
                )
                return
            with sidecar_lock:
                sessions = evaluator_sessions()
                _write_open_loop_sidecars(
                    mailbox,
                    lead_runner,
                    eval_runner,
                    iteration,
                    phase,
                    lead_alive,
                    eval_alive,
                    started_at,
                    sessions,
                )
                written_sessions[0] = sessions

        def current_iteration() -> int:
            return _number(_read_state(state_path)["iteration"])

        def finish(code: int) -> int:
            drain_slice_evals()
            write_sidecar("done", current_iteration(), False, False)
            return code

        wake_event = threading.Event()
        # r11g Q1: iterations whose `gate held` LOG line is written, shared
        # by the Lead thread and the done re-check below.
        gate_held_logged: set = set()
        gate_held_lock = threading.Lock()

        def spawn_lead(force_first_pass: bool = False) -> tuple[threading.Thread, dict]:
            holder: dict = {}
            thread = threading.Thread(
                target=_lead_thread_body,
                args=(
                    mailbox,
                    state_path,
                    lead_runner,
                    max_iterations,
                    write_sidecar,
                    wake_event,
                    stop_event,
                    holder,
                    force_first_pass,
                    repo,
                    gate_held_logged,
                    gate_held_lock,
                ),
                daemon=True,
            )
            thread.start()
            return thread, holder

        write_sidecar("lead", current_iteration(), True, True)
        # First-ever Lead spawn of the run always runs a pass: on a fresh
        # mailbox PLAN.md is empty/missing, so the "all slices retired"
        # check has nothing to compare against yet, and the Lead is the
        # one who writes the plan in the first place.
        lead_thread, lead_result = spawn_lead(force_first_pass=True)

        graded: set[tuple[str, str]] = set()
        # (slice_id, sha) pairs whose per-slice commit gate exited 1
        # (missing commits, skipped/ungraded) -- a slice in this set
        # counts as retired-but-not-yet-validated, so it must keep
        # blocking the integration-eval termination check below even
        # though QUEUE.md already shows it as retired.
        gate_blocked: set[tuple[str, str]] = set()
        # (slice_id, sha) -> number of slice-eval attempts made so far that
        # returned exit 0 but did not (yet) leave a `## slice ... — SHIP|
        # ITERATE` section on disk. A key here (not yet graded, not yet
        # failed out) blocks the integration-eval termination check below,
        # same as gate_blocked -- see open-loop output verification.
        slice_eval_attempts: dict[tuple[str, str], int] = {}
        eval_pending: set[tuple[str, str]] = set()
        # Concurrent slice-evals: one lock around every bookkeeping pass
        # (harvest) so the sets above stay single-writer; only this outer
        # thread ever takes it or touches them.
        bookkeeping_lock = threading.Lock()

        def harvest_slice_evals(post) -> int | None:
            """Apply post-processing to every finished slice-eval future.

            Successful ones first (in submission order); then the first
            failure (non-zero exit / exception) is raised exactly as the
            serial loop raises it -- the remaining ones are drained on exit.
            """
            current = _read_text_settled(verdict_path)
            for entry in inflight.values():
                # Everything seen on disk while an eval ran is protected by
                # its clobber check (concurrent evals share VERDICT.md).
                _merge_slice_blocks(entry["seen"], current)
            failure: BaseException | None = None
            for key in [k for k, e in inflight.items() if e["future"].done()]:
                entry = inflight.pop(key)
                future = entry["future"]
                if future.cancelled():
                    continue
                exc = future.exception()
                if exc is None and future.result() != 0:
                    exc = RuntimeError(
                        f"evaluator runner failed with exit {future.result()}"
                    )
                if exc is not None:
                    if failure is None:
                        failure = exc
                    continue
                snapshot = "".join(entry["seen"].values())
                code = post(key[0], key[1], snapshot)
                if code is not None:
                    return code
            if failure is not None:
                raise failure
            return None

        queue_errors_logged: set = set()
        while True:
            lead_alive = not lead_result.get("finished", False)

            queue = _read_queue(mailbox)
            _log_queue_errors(
                mailbox, current_iteration(), queue, queue_errors_logged
            )
            latest_retired: dict[str, dict] = {}
            for entry in queue["retired"]:
                latest_retired[entry["slice"]] = entry
            verdict_text = (
                verdict_path.read_text(encoding="utf-8", errors="replace")
                if verdict_path.is_file()
                else ""
            )
            verdict_sections = _METRICS.parse_slice_verdicts(verdict_text)

            def post_slice_eval(
                slice_id: str, sha: str, verdict_snapshot: str
            ) -> int | None:
                """Post-process one finished slice-eval (outer thread only).

                Returns an exit code to finish with, or None to go on.
                """
                key = (slice_id, sha)
                # Concurrent evals may be rewriting VERDICT.md right now:
                # read it settled so a torn read neither triggers a bogus
                # restore nor hides this slice's section.
                settled = _read_text_settled if concurrent_evals else None
                _restore_clobbered_verdict_sections(
                    mailbox, verdict_path, verdict_snapshot, "slice-eval",
                    current_iteration(), read_text=settled,
                )
                # Verify against VERDICT.md on disk -- a runner exit of 0 is
                # not proof the role did its job (a blip session can return
                # 0 having written nothing).
                if settled is not None:
                    verdict_text_after = settled(verdict_path)
                else:
                    verdict_text_after = (
                        verdict_path.read_text(encoding="utf-8", errors="replace")
                        if verdict_path.is_file()
                        else ""
                    )
                wrote_section = any(
                    v["slice"] == slice_id and sha.startswith(v["sha"])
                    for v in _METRICS.parse_slice_verdicts(verdict_text_after)
                )
                if wrote_section:
                    graded.add(key)
                    gate_blocked.discard(key)
                    eval_pending.discard(key)
                    slice_eval_attempts.pop(key, None)
                    return None
                attempts = slice_eval_attempts.get(key, 0) + 1
                slice_eval_attempts[key] = attempts
                _append_log(
                    mailbox,
                    f"- iter {current_iteration()} | loop | open-loop: "
                    f"slice-eval for {slice_id}@{sha} wrote no verdict "
                    f"section (attempt {attempts})",
                )
                if attempts >= 3:
                    stop_event.set()
                    wake_event.set()
                    lead_thread.join(timeout=5)
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | open-loop: "
                        f"slice-eval for {slice_id}@{sha} failed to write a "
                        f"verdict section after {attempts} attempts",
                    )
                    return 3
                eval_pending.add(key)
                return None

            for slice_id, entry in latest_retired.items():
                sha = entry["sha"]
                key = (slice_id, sha)
                if key in graded or key in inflight:
                    continue
                if any(
                    v["slice"] == slice_id and sha.startswith(v["sha"])
                    for v in verdict_sections
                ):
                    graded.add(key)
                    if concurrent_evals:
                        # A section that shows up after a harvest judged
                        # it missing settles that key (the serial loop
                        # never reaches this state).
                        eval_pending.discard(key)
                        slice_eval_attempts.pop(key, None)
                    continue
                write_sidecar(
                    "evaluator", current_iteration(), lead_alive, True
                )
                gate_code = _per_slice_gate(mailbox, repo, slice_id)
                if gate_code == 1:
                    gate_blocked.add(key)
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | commit gate "
                        f"failed for slice {slice_id}; skipping until re-retired",
                    )
                    continue
                gate_blocked.discard(key)
                if gate_code == 2:
                    stop_event.set()
                    wake_event.set()
                    lead_thread.join(timeout=5)
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | commit gate "
                        f"error for slice {slice_id}",
                    )
                    return finish(3)
                context = {
                    "mode": "open-loop",
                    "slice": slice_id,
                    "sha": sha,
                    "kind": "slice-eval",
                }
                entry_repo = str(entry.get("repo") or "").strip()
                if entry_repo and entry_repo != "home":
                    # r15: the sha is a commit of this declared repo.
                    context["repo"] = entry_repo
                verdict_snapshot = (
                    verdict_path.read_text(encoding="utf-8", errors="replace")
                    if verdict_path.is_file()
                    else ""
                )
                if concurrent_evals:
                    # Submit and move on; harvested below on this thread.
                    if pool is None:
                        pool = concurrent.futures.ThreadPoolExecutor(
                            max_workers=slice_eval_concurrency,
                            thread_name_prefix="slice-eval",
                        )
                    future = pool.submit(
                        _invoke_runner, eval_runner, "evaluator",
                        current_iteration(), mailbox, context,
                    )
                    seen: dict = {}
                    _merge_slice_blocks(seen, verdict_snapshot)
                    inflight[key] = {
                        "future": future,
                        "snapshot": verdict_snapshot,
                        "seen": seen,
                    }
                    future.add_done_callback(lambda _f: wake_event.set())
                    continue
                result = _invoke_runner(
                    eval_runner, "evaluator", current_iteration(), mailbox, context
                )
                if result != 0:
                    raise RuntimeError(
                        f"evaluator runner failed with exit {result}"
                    )
                code = post_slice_eval(slice_id, sha, verdict_snapshot)
                if code is not None:
                    return finish(code)

            if inflight:
                with bookkeeping_lock:
                    code = harvest_slice_evals(post_slice_eval)
                if code is not None:
                    return finish(code)
            if concurrent_evals:
                # `.driver.json` evaluator_sessions, rewritten from this
                # thread whenever the in-flight set or its session ids
                # changed (ids appear once each worker's create returns).
                sessions_now = evaluator_sessions()
                if sessions_now != written_sessions[0]:
                    alive_now = not lead_result.get("finished", False)
                    write_sidecar(
                        "evaluator" if inflight or not alive_now else "lead",
                        current_iteration(), alive_now, True,
                    )

            lead_alive = not lead_result.get("finished", False)
            if not lead_alive:
                if lead_result.get("error") is not None:
                    exc = lead_result["error"]
                    _update_state(state_path, {"status": "error"})
                    _append_log(
                        mailbox,
                        f"- iter {current_iteration()} | loop | lead thread "
                        f"failed: {exc}",
                    )
                    return finish(3)
                if lead_result.get("outcome") == "capped":
                    return finish(4)
                if lead_result.get("outcome") == "stalled":
                    # The Lead thread already set status: error and logged
                    # the failure itself (open-loop Lead-pass output
                    # verification) after 3 consecutive no-op passes.
                    return finish(3)
                # outcome == "done": re-check with fresh data before
                # trusting it -- a slice-eval just above may have opened a
                # fault after the Lead thread already decided it was done.
                slice_ids = _read_plan_slice_ids(mailbox)
                queue = _read_queue(mailbox)
                # Log parse errors / malformed slices / unknown statuses in
                # THIS turn: the final slice-eval may have just written a
                # malformed fault, and the integration eval must never be
                # dispatched ahead of that LOG line (r11f R3).
                _log_queue_errors(
                    mailbox, current_iteration(), queue, queue_errors_logged
                )
                retired_ids = _gate_retired_ids(queue)
                open_or_taken = _live_faults(queue)
                latest_for_gate = {e["slice"]: e for e in queue["retired"]}
                any_gate_blocked = any(
                    (sid, latest_for_gate[sid]["sha"]) in gate_blocked
                    for sid in (slice_ids or [])
                    if sid in latest_for_gate
                )
                # A slice whose slice-eval returned 0 without (yet) leaving
                # a verdict section must also keep blocking the integration
                # check -- membership in `graded` is not proof either; see
                # open-loop output verification.
                any_eval_pending = any(
                    (sid, latest_for_gate[sid]["sha"]) in eval_pending
                    for sid in (slice_ids or [])
                    if sid in latest_for_gate
                ) or bool(inflight)  # a running slice-eval is pending too
                # r11g Q1: a `faults:` block parse error (a dropped or
                # unparseable fault) holds the gate exactly like a live
                # fault: the Lead is re-spawned below instead of the
                # integration eval being dispatched.
                fully_retired = _slices_fully_retired(
                    slice_ids, retired_ids, open_or_taken
                ) and not _integration_gate_held(
                    mailbox,
                    current_iteration(),
                    queue,
                    gate_held_logged,
                    gate_held_lock,
                )
                # The queue was re-read just above: a slice retired after
                # this turn's dispatch pass is neither graded nor pending
                # yet and must not let the integration-eval start.
                any_ungraded = any(
                    (sid, latest_for_gate[sid]["sha"]) not in graded
                    for sid in (slice_ids or [])
                    if sid in latest_for_gate
                )
                if fully_retired and any_ungraded and not (
                    any_gate_blocked or any_eval_pending
                ):
                    continue  # dispatch it on the next turn, no wait
                if fully_retired and (any_gate_blocked or any_eval_pending):
                    # Every declared slice has a retired entry, but at
                    # least one is still waiting on its commit gate
                    # (missing commits, presumably transient) or a verified
                    # slice-eval verdict section -- there is nothing new
                    # for the Lead to do, so just wait for the next poll
                    # and re-check then, instead of spinning the Lead
                    # thread up and down.
                    wake_event.wait(
                        timeout=poll_seconds if poll_seconds > 0 else 0.01
                    )
                    wake_event.clear()
                    continue
                if fully_retired:
                    write_sidecar(
                        "evaluator", current_iteration(), False, True
                    )
                    iteration_now = current_iteration()
                    context = _open_loop_integration_context(
                        mailbox, repo, iteration_now, state_path
                    )
                    integration_attempts = 0
                    while True:
                        verdict_snapshot = (
                            verdict_path.read_text(
                                encoding="utf-8", errors="replace"
                            )
                            if verdict_path.is_file()
                            else ""
                        )
                        result = _invoke_runner(
                            eval_runner, "evaluator", iteration_now, mailbox,
                            context,
                        )
                        if result != 0:
                            raise RuntimeError(
                                f"evaluator runner failed with exit {result}"
                            )
                        restored_sections = _restore_clobbered_verdict_sections(
                            mailbox, verdict_path, verdict_snapshot,
                            "integration-eval", iteration_now,
                        )
                        # A runner exit of 0 is not proof the role wrote a
                        # verdict -- a blip session can return 0 having
                        # written nothing to VERDICT.md. Only a real
                        # `VERDICT: ...` line (however malformed) is a
                        # genuine unparseable-verdict error; no line at all
                        # is retried.
                        if _has_verdict_line(verdict_path):
                            break
                        integration_attempts += 1
                        _append_log(
                            mailbox,
                            f"- iter {iteration_now} | loop | open-loop: "
                            "integration-eval wrote no verdict "
                            f"(attempt {integration_attempts})",
                        )
                        if integration_attempts >= 3:
                            _update_state(state_path, {"status": "error"})
                            _append_log(
                                mailbox,
                                f"- iter {iteration_now} | loop | "
                                "open-loop: integration-eval failed to "
                                "write a verdict after "
                                f"{integration_attempts} attempts",
                            )
                            return finish(3)
                    if restored_sections:
                        # The Evaluator may already have committed its
                        # rewritten VERDICT.md as the SHIP retirement; the
                        # restore left the sections uncommitted, which the
                        # acceptance check rightly treats as dirty.
                        _fold_restored_verdict_into_retirement(
                            mailbox, repo, iteration_now, restored_sections
                        )
                    verdict, scope = _first_verdict(verdict_path)
                    if verdict is None:
                        _update_state(state_path, {"status": "error"})
                        _append_log(
                            mailbox,
                            f"- iter {iteration_now} | loop | unparseable "
                            "integration verdict",
                        )
                        return finish(3)
                    _write_state_verdict(state_path, verdict)
                    code = _apply_verdict(
                        mailbox,
                        state_path,
                        repair_path,
                        iteration_now,
                        verdict,
                        scope,
                        repo=repo,
                    )
                    if code is not None:
                        return finish(code)
                    # The graded pin/attempt belong to that verdict only;
                    # the next integration-eval must bind a fresh one.
                    _update_state(
                        state_path,
                        {
                            "evaluator_attempt": "",
                            "evaluated_sha": "",
                            "evaluated_repos": "",
                        },
                    )
                    # ITERATE: wake the Lead for another pass, forcing at
                    # least one even if the queue currently looks "done"
                    # (the integration verdict is the authority here).
                    wake_event.clear()
                    lead_thread, lead_result = spawn_lead(force_first_pass=True)
                    write_sidecar("lead", current_iteration(), True, True)
                    continue
                # Fresh open work appeared after the Lead thread stopped
                # (e.g. a slice-eval fault) -- re-invoke it.
                wake_event.clear()
                lead_thread, lead_result = spawn_lead()
                write_sidecar("lead", current_iteration(), True, True)
                continue

            wake_event.wait(timeout=poll_seconds if poll_seconds > 0 else 0.01)
            wake_event.clear()
    finally:
        stop_event.set()
        # Nested so a second signal during the drain (or the Lead join)
        # still releases the mailbox lock.
        try:
            drain_slice_evals()
        finally:
            try:
                if lead_thread is not None:
                    lead_thread.join(timeout=5)
            finally:
                _release_lock(lock)


class _PortableRunner:
    def run(
        self,
        role: str,
        iteration: int,
        mailbox: Path,
        context: dict | None = None,
    ) -> int:
        script = (
            Path(__file__).resolve().parent.parent
            / "portable"
            / "driver.sh"
        )
        environment = os.environ.copy()
        environment["LOOP_DIR"] = str(Path(mailbox).resolve())
        # api:OpenLoopPromptEnv: only set the TRIO_* vars for a non-empty
        # open-loop context, so a lockstep (context={}/None) call renders a
        # byte-identical prompt to HEAD.
        if context and context.get("mode") == "open-loop":
            environment["TRIO_MODE"] = "open-loop"
            environment["TRIO_KIND"] = context.get("kind") or ""
            environment["TRIO_SLICE"] = context.get("slice") or ""
            environment["TRIO_SHA"] = context.get("sha") or ""
            # r11g Q1: `faults:` block errors for a held gate (lead-pass).
            queue_errors = context.get("queue_errors") or []
            if queue_errors:
                environment["TRIO_QUEUE_ERRORS"] = "\n".join(
                    str(e) for e in queue_errors
                )
            else:
                environment.pop("TRIO_QUEUE_ERRORS", None)
        elif context:
            # Lockstep pin/attempt: same LOCKSTEP CONTEXT as Omnigent.
            attempt = str(context.get("evaluator_attempt") or "")
            sha = str(
                context.get("pinned_sha")
                or context.get("expected_sha")
                or ""
            )
            if attempt or sha:
                environment["TRIO_ATTEMPT"] = attempt
                environment["TRIO_PINNED_SHA"] = sha
        # The shell shim only runs one role. Gates, verdicts, repairs, and
        # resume stay in run_loop so this runner never parses VERDICT.md.
        result = subprocess.run(
            [str(script), "--run-role", role],
            check=False,
            env=environment,
        )
        return result.returncode


def _load_omnigent_runner():
    """Load OmnigentRunner without importing the ``omnigent`` package."""
    path = Path(__file__).resolve().parent.parent / "omnigent" / "trioctl"
    if not path.is_file():
        raise ImportError(f"OmnigentRunner source is missing: {path}")
    loader = importlib.machinery.SourceFileLoader("trioctl", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load OmnigentRunner from {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    runner = getattr(module, "OmnigentRunner", None)
    if not callable(runner):
        raise ImportError(f"{path} does not expose OmnigentRunner")
    return runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the Trio state machine")
    run.add_argument("--mailbox", type=Path, required=True)
    run.add_argument("--max-iterations", type=int, required=True)
    run.add_argument(
        "--runner",
        choices=("portable", "omnigent"),
        default="portable",
    )
    run.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="open-loop Evaluator poll interval in seconds (default: 30)",
    )
    run.add_argument(
        "--slice-eval-concurrency",
        type=int,
        default=1,
        metavar="N",
        help="open-loop: grade up to N retired slices at once "
        "(default 1: one after another)",
    )
    run.add_argument(
        "--slice-eval-drain-seconds",
        type=float,
        default=None,
        metavar="S",
        help="open-loop: on exit, wait up to S seconds for running "
        f"slice-evals (default: ${SLICE_EVAL_DRAIN_ENV}, else "
        "min(role timeout, 120))",
    )
    mode_group = run.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--open-loop",
        action="store_true",
        help="force open-loop mode; errors if the mailbox has no QUEUE.md",
    )
    mode_group.add_argument(
        "--lockstep",
        action="store_true",
        help="force lockstep mode even when the mailbox has QUEUE.md",
    )
    args = parser.parse_args(argv)
    repo = Path.cwd()
    runner = (
        _PortableRunner()
        if args.runner == "portable"
        else _load_omnigent_runner()(repo=repo)
    )
    mode = "open-loop" if args.open_loop else "lockstep" if args.lockstep else "auto"
    return run_loop(
        args.mailbox,
        args.max_iterations,
        runner,
        repo=repo,
        poll_seconds=args.poll_seconds,
        mode=mode,
        slice_eval_concurrency=args.slice_eval_concurrency,
        slice_eval_drain_seconds=args.slice_eval_drain_seconds,
    )

if __name__ == "__main__":
    sys.exit(main())

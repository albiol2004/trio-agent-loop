"""r18a shadow quality telemetry, ported from ``omnigent/trioctl`` for the
standalone OpenCode driver's open-loop mode.

Every piece here is **advisory**: it informs a slice-eval Evaluator's
grading or the driver's own logs/`.driver.json`, but it NEVER changes a
retire/merge decision (the base-revert kill check keeps running even on a
``restore`` failure -- see :func:`run_kill_check`).

This module is ported verbatim where the trioctl source is pure (no
``AgentRunner``/CLI-``argparse`` coupling); each function's docstring names
its trioctl source function and approximate line so a future trioctl change
can be diffed against this file. Two kinds of source dependency are
deliberately NOT carried over, per the standalone driver's hard
constraints:

* No ``omnigent`` import, ever (a test asserts no ``omnigent`` module is in
  ``sys.modules``). Where trioctl reaches into ``worker_worktrees`` records
  or ``args: argparse.Namespace`` CLI flags, this module instead takes the
  already-resolved value as a plain parameter -- the driver-owned builder
  loop (``waves.py``/``driver.py``) supplies it.
* No new repo-relative ``metrics/...`` path dependency. ``trio-check.py``
  (and, through it, its own sibling ``trio-metrics.py``) is loaded only via
  the already-loaded ``metrics/trio_loop.py`` core's own
  :func:`trio_loop._load_sibling` helper (``steplib.TL._load_sibling``),
  never a repo-root-relative candidate search of trioctl's
  ``_load_trio_check`` (trioctl ~1233-1257).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from trio_opencode import steplib

GIT_NO_REPLACE_ENV = {"GIT_NO_REPLACE_OBJECTS": "1"}  # trioctl:44


def _mailbox_text(path: Path) -> str:
    """Read a mailbox file; missing files count as empty. (trioctl:3458,
    ``_mailbox_text``)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# trio-check.py / trio-metrics.py: loaded through the already-loaded
# metrics/trio_loop.py core's sibling loader -- never a repo-relative path
# search, never an ``omnigent`` import.
# --------------------------------------------------------------------------

_TRIO_CHECK_CACHE: list[tuple[Any, Any]] = []


def load_trio_check() -> tuple[Any, Any] | None:
    """This release's ``trio-check.py`` module and its ``trio-metrics.py``
    (``(module, metrics)``), or ``None`` when either is unavailable.

    Ported in spirit from trioctl's ``_load_trio_check`` (~1233-1257), but
    loaded via ``steplib.TL._load_sibling`` (``metrics/trio_loop.py:1971``)
    instead of trioctl's own repo-root candidate search: both land on
    ``metrics/trio-check.py`` since ``_load_sibling`` resolves the sibling
    path from ``trio_loop.py``'s own ``__file__``, regardless of where the
    caller lives. Cached after the first successful load; advisory, so a
    load failure is swallowed and reported as ``None``, never raised.
    """
    if _TRIO_CHECK_CACHE:
        return _TRIO_CHECK_CACHE[0]
    try:
        module = steplib.TL._load_sibling(
            f"trio_opencode_quality_check_{uuid.uuid4().hex}", "trio-check.py",
        )
        if module is None:
            return None
        metrics = module.load_trio_metrics()
    except Exception:  # noqa: BLE001 - advisory: never break a dispatch
        return None
    _TRIO_CHECK_CACHE.append((module, metrics))
    return _TRIO_CHECK_CACHE[0]


# ==========================================================================
# 1. TARGETED_CHECK line normalization (trioctl ~2043-2111)
# ==========================================================================

TARGETED_CHECK_PREFIX = "TARGETED_CHECK: "  # trioctl:2043
# Tolerant of the decorations models add despite the plain-text contract:
# an optional list bullet / number, backticks, bold or underscores around
# the prefix or the value, any case, any (or no) whitespace after the colon.
_TARGETED_CHECK_RE = re.compile(
    r"^(?:[-*+•]\s+|\d+[.)]\s+)?[`*_]*\s*TARGETED_CHECK[`*_]*\s*:"
    r"[\s`*_]*(?P<rest>.*?)[\s`*_]*$",
    re.IGNORECASE,
)
# The contract's own placeholder, echoed back instead of a real result: a
# value that is only a `<...>` placeholder (optionally after FAILED).
_TARGETED_CHECK_PLACEHOLDER_RE = re.compile(r"^(?:FAILED\s+)?<[^<>]*>$", re.IGNORECASE)
# Any unexpanded `<...>` placeholder anywhere in the value (`PASS <n>`,
# `PASS <count>`, `<n> passed`): a template copied literally, never a pass.
_TARGETED_CHECK_ANY_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
# Template literals without angle brackets that are equally content-free.
_TARGETED_CHECK_TEMPLATE_LITERALS = frozenset({
    "...", "…", "pass n", "pass <n>", "pass <count>", "line", "counts line",
    "last summary line", "summary",
})
_TARGETED_CHECK_FAILED_RE = re.compile(r"^failed\b", re.IGNORECASE)
# A value that reports failures without the FAILED prefix (pytest's last
# line copied verbatim, e.g. `2 failed, 3 passed`, `1 error`, `no tests
# ran`) is still a failure for the Lead's retire test. Zero counts
# (`| 0 failed`, `0 errors`) are not failures.
_TARGETED_CHECK_FAILURE_TEXT_RE = re.compile(
    r"\b([1-9]\d* failed|[1-9]\d* errors?|no tests ran|FAIL)\b", re.IGNORECASE
)


def targeted_check_line(output: str | None) -> str | None:
    """The builder's LAST ``TARGETED_CHECK:`` line, normalized, or ``None``.

    Verbatim port of trioctl's ``_targeted_check_line`` (~2073-2111).

    Returned as ``TARGETED_CHECK: <value>`` (decorations stripped, a
    case-insensitive leading ``failed`` upper-cased to ``FAILED``, and a
    value reporting failures without that prefix -- ``N failed``, ``N
    error(s)``, ``no tests ran``, ``FAIL`` -- prefixed with ``FAILED ``) so
    :func:`targeted_check_failed`'s "does not start with ``TARGETED_CHECK:
    FAILED``" test is mechanical. Empty values, echoed placeholders and
    template literals copied verbatim (``PASS <n>``, ``PASS <count>``,
    ``<last summary line>``, any non-failure value holding an unexpanded
    ``<...>``) are ignored, so a builder that only echoes the template
    reads as "not reported"; the last matching line wins (fix-and-rerun
    reports the rerun).
    """
    found = None
    for line in (output or "").splitlines():
        match = _TARGETED_CHECK_RE.match(line.strip())
        if not match:
            continue
        rest = match.group("rest").strip()
        if not rest or _TARGETED_CHECK_PLACEHOLDER_RE.match(rest):
            continue
        if " ".join(rest.lower().split()) in _TARGETED_CHECK_TEMPLATE_LITERALS:
            continue
        # A non-failure value with an unexpanded placeholder is a template
        # echo, not a result: treat as absent so the Lead re-dispatches.
        # (A real failure may quote `<...>` text, e.g. `FAILED ... <Foo>`,
        # and is kept: it is never retired either way.)
        if (_TARGETED_CHECK_ANY_PLACEHOLDER_RE.search(rest)
                and not _TARGETED_CHECK_FAILED_RE.match(rest)
                and not _TARGETED_CHECK_FAILURE_TEXT_RE.search(rest)):
            continue
        if _TARGETED_CHECK_FAILED_RE.match(rest):
            rest = "FAILED" + rest[len("failed"):]
        elif _TARGETED_CHECK_FAILURE_TEXT_RE.search(rest):
            rest = "FAILED " + rest
        found = TARGETED_CHECK_PREFIX + rest
    return found


def targeted_check_failed(line: str | None) -> bool:
    """True when *line* (a :func:`targeted_check_line` result) means the
    slice's targeted check did not pass: ``None``, or a line starting
    ``TARGETED_CHECK: FAILED``.

    This is the Lead's own retire test, referenced by trioctl's docstring
    for ``_targeted_check_line`` (trioctl:2080) and applied inline at the
    kill-check decision point (trioctl:2796, ``_isolated_kill_check``:
    ``if not targeted or targeted.startswith(TARGETED_CHECK_PREFIX +
    "FAILED")``).
    """
    return line is None or line.startswith(TARGETED_CHECK_PREFIX + "FAILED")


# ==========================================================================
# 2. The brief's ``## Targeted check`` command (trioctl ~2245-2401)
# ==========================================================================

# r18a repair (eval-r17fix P-1..P-4): every real `## Targeted check` command
# also sits next to the canonical TARGETED_CHECK output-format hint -- the
# literal sentence trioctl's own PLAN-guard text tells the Lead to append
# verbatim ("Print `TARGETED_CHECK: <the line stating the pass/fail
# counts>` after running the check (pytest: `N passed[, M failed] in ...`;
# ...)."). The r18 fix tried to tell the hint's quoted examples apart from a
# real command with a runner allowlist; that rejected many legitimate
# runners (`uv`, `poetry`, `docker compose`, `timeout`, a `(cd x && ...)`
# subshell, `source`/`.`, `export`, `bundle exec`, `just`, `grep`, a
# capitalized `Python3`) as `n/a` -- a regression vs the naive scan. Instead,
# drop the hint sentence itself before scanning, so almost anything left
# over that looks like shell syntax can be trusted.
_TARGETED_CHECK_HINT_RE = re.compile(r"Print\s+`TARGETED_CHECK:.*?\)\.", re.DOTALL)

# Only the plain-line fallback (no fenced block, no backticked candidate
# anywhere in the section) still needs a runner/prose heuristic: an
# unquoted, un-backticked line might be ordinary prose rather than a
# command, so it is only trusted when it starts with shell syntax a real
# command almost always has.
_TARGETED_COMMAND_PLAIN_LINE_RUNNER_RE = re.compile(
    r"^(?:\.{1,2}/|~/|/|\()"
    r"|^\.\s"
    r"|^(?:cd|pushd|env|set|export|source|timeout|uv|poetry|docker|bundle|just|"
    r"grep|python3?|pytest|unittest|npx|npm|yarn|pnpm|node|go|cargo|mvn|gradle|"
    r"make|sh|bash|zsh|tsc|vitest|jest|mocha|rspec|ruby|rake|dotnet|swift|deno|"
    r"bun|tox|nox)\b"
    r"|^[A-Za-z_][A-Za-z0-9_]*=\S",
    re.IGNORECASE,
)
# A placeholder is one of the hint's own shapes (`N passed`, `M failed`, a
# bare `...` that is not part of a path like `./...`, or a template
# `<word>`) -- never a generic `[...]`.
_TARGETED_COMMAND_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_TARGETED_COMMAND_PLACEHOLDER_RE = re.compile(
    r"\bN passed\b|\bM failed\b|(?<![\w./])\.\.\.(?![\w./])|<[^<>]*>"
)
# A plain (unquoted, un-backticked) fallback line is rejected as prose, not
# a command, when its second word is one an English sentence would use
# right after a runner-shaped verb ("make SURE ...", "set UP ...") and the
# line carries none of the syntax a real command almost always has.
_TARGETED_COMMAND_PROSE_STOPWORDS = frozenset({
    "sure", "up", "into", "out", "certain", "should", "must",
})


def _looks_like_prose(text: str) -> bool:
    words = text.split()
    if len(words) < 2:
        return False
    second = words[1].strip(".,;:!?").lower()
    if second not in _TARGETED_COMMAND_PROSE_STOPWORDS:
        return False
    return not re.search(r"[/=]|--|&&|['\"]", text)


def _plausible_command(text: str) -> str | None:
    """*text*, if a fenced block or backticked span plausibly is a runnable
    command; else None. (trioctl:2310-2324, ``_plausible_command``)."""
    text = text.strip()
    if not text or "TARGETED_CHECK" in text:
        return None
    unquoted = _TARGETED_COMMAND_QUOTED_RE.sub("", text)
    return None if _TARGETED_COMMAND_PLACEHOLDER_RE.search(unquoted) else text


def _plausible_plain_command(text: str) -> str | None:
    """*text*, if a plain (unquoted, un-backticked) fallback line plausibly
    is a runnable command; else None. (trioctl:2327-2341,
    ``_plausible_plain_command``)."""
    text = text.strip()
    if not text or "TARGETED_CHECK" in text:
        return None
    unquoted = _TARGETED_COMMAND_QUOTED_RE.sub("", text)
    if _TARGETED_COMMAND_PLACEHOLDER_RE.search(unquoted):
        return None
    return text if _TARGETED_COMMAND_PLAIN_LINE_RUNNER_RE.match(text) else None


def brief_targeted_command(brief: str) -> str | None:
    """The brief's ``## Targeted check`` command: a fenced block, else the
    first plausible backticked span, else the first plausible plain line
    -- or ``None`` when nothing in the section looks like a runnable
    command (the caller then reports ``n/a``, never runs a placeholder as
    ``error``). Verbatim port of trioctl's ``_brief_targeted_command``
    (~2344-2388), using :func:`load_trio_check` in place of trioctl's own
    ``_load_trio_check``.
    """
    loaded = load_trio_check()
    if loaded is None:
        return None
    try:
        lines = loaded[0].targeted_check_lines(brief)
    except Exception:  # noqa: BLE001 - advisory: never break a dispatch
        return None
    # Drop the output-format hint sentence itself before scanning (P-1/P-2):
    # this may join two source lines into one, which is fine here since the
    # scan below only cares about fence/backtick/plain-line shapes.
    lines = _TARGETED_CHECK_HINT_RE.sub("", "\n".join(lines)).split("\n")
    fenced: list[str] = []
    inside = False
    for ln in lines:
        if ln.strip().startswith(("```", "~~~")):
            if inside:
                break
            inside = True
            continue
        if inside and ln.strip() and not ln.strip().startswith("#"):
            fenced.append(ln.rstrip())
    if fenced:
        # Same hint/placeholder filtering as the other forms (pre-existing
        # gap): a fenced output example or placeholder command is never
        # returned verbatim; fall through to the backtick/plain scan below.
        command = _plausible_command("\n".join(fenced))
        if command:
            return command
    for ln in lines:
        for span in re.findall(r"`([^`]+)`", ln):
            if not re.search(r"\s", span):
                continue  # P-3: a one-word span is never the real command
            command = _plausible_command(span)
            if command:
                return command
    for ln in lines:
        text = re.sub(r"^\s*(?:[-*+]\s+|\$\s+)", "", ln).strip()
        if text.lower().startswith("print") or _looks_like_prose(text):
            continue
        command = _plausible_plain_command(text)
        if command:
            return command
    return None


def _no_targeted_command_reason(brief: str) -> str:
    """``run_kill_check``'s ``n/a`` reason when ``brief_targeted_command``
    is None: distinguishes a missing ``## Targeted check`` section from one
    that is present but has nothing plausible in it. (trioctl:2391-2401,
    ``_no_targeted_command_reason``)."""
    loaded = load_trio_check()
    if loaded is None:
        return "no `## Targeted check` command in the brief"
    try:
        lines = loaded[0].targeted_check_lines(brief)
    except Exception:  # noqa: BLE001
        return "no `## Targeted check` command in the brief"
    if any(ln.strip() for ln in lines):
        return "no plausible targeted command in the section"
    return "no `## Targeted check` command in the brief"


# ==========================================================================
# 3. The base-revert kill check (r18a L2a; trioctl ~2129-2832)
# ==========================================================================
#
# After an isolated builder exits 0 with a passing TARGETED_CHECK line, and
# BEFORE its worktree is merged, the driver may revert the slice's non-test
# product files to the worktree's base, re-run the brief's `## Targeted
# check` command under a budget, and restore the tree from an in-memory
# snapshot of EVERY path the slice changed (tracked + untracked: product,
# tests, fixtures, mailbox files), proven byte-identical per path and by a
# sha256 over every tracked and untracked, non-ignored file. This NEVER
# changes the retire decision -- not even a failed restore proof, which is
# recorded as `error` (reason `restore: ...`) and printed loudly.

KILL_CHECK_ENV = "TRIO_KILL_CHECK"
KILL_CHECK_BUDGET_ENV = "TRIO_KILL_CHECK_BUDGET_S"
#: The targeted-check budget: the whole-tree gate's default wall clock
#: (PLAN.md `full_check_budget_s:` overrides it, the env var overrides both).
KILL_CHECK_DEFAULT_BUDGET_S = 120.0
KILL_CHECK_OUTCOMES = ("killed", "survived", "n/a", "error")

# Test files are never reverted: the slice's tests must run against the
# base product. Globs per MAILBOX-SCHEMA "Base-revert kill check".
_KILL_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__snapshots__|spec|specs)/"
    r"|(?:^|/)test_[^/]*\.py$|(?:^|/)[^/]*_test\.(?:py|go)$|(?:^|/)conftest\.py$"
    r"|\.(?:test|spec)\.[cm]?[jt]sx?$"
)
# Receipts and evidence are never product.
_KILL_NON_PRODUCT_RE = re.compile(r"(?:^|/)(?:results|evidence)/")
# `killed` needs a runner's own assertion / failed-test report:
#   pytest `N failed` summary, `FAILED <nodeid>`, `E   assert`, AssertionError
#   (also node's `AssertionError [ERR_ASSERTION]`); vitest/jest `Tests: N
#   failed` and `×`/`✕` test marks; go `--- FAIL`; TAP `not ok N`; generic
#   `Error: expect(...)`. A bare jest/vitest `FAIL <file>` line counts only
#   when no collection error explains it. Anything else non-zero is `error`.
_KILL_ASSERTION_RE = re.compile(
    r"^(?:=+\s*)?[1-9]\d* failed\b(?:,| in )|^FAILED |^E\s+assert\b|AssertionError"
    r"|^\s*Tests:?\s+[1-9]\d* failed|^\s*[×✕] |--- FAIL\b|^not ok \d|Error: expect\b",
    re.MULTILINE,
)
_KILL_FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+\S", re.MULTILINE)
# Collection / import / module-resolution / build errors: the tests could not
# even load against the base. Not a behavioural kill (recorded as `error`
# with `collection_error: true`).
_KILL_COLLECTION_RE = re.compile(
    r"ImportError|ModuleNotFoundError|No module named|error(?:s)? during collection"
    r"|ERROR collecting|Cannot find module|Failed to (?:load|resolve)|does not provide an export"
    r"|Test suite failed to run|error TS\d{4}|SyntaxError|cannot import name"
    r"|has no attribute|undefined: |\[build failed\]|\[setup failed\]",
)
# The runner itself did not run the tests: missing command, cd failure,
# usage error, pytest exit 4/5 ("no tests ran", "file or directory not found").
_KILL_NOT_RUNNABLE_RE = re.compile(
    r"command not found|: not found$|No such file or directory|not recognized as"
    r"|^\S*python[\d.]*: No module named|^\S*python[\d.]*: can't open file"
    r"|can't cd to|cd: .*(?:No such file|not a directory|can't cd)"
    r"|^ERROR: file or directory not found|^ERROR: usage:|error: unrecognized arguments"
    r"|^usage: |no tests ran|no tests collected|No tests? (?:files )?found",
    re.MULTILINE | re.IGNORECASE,
)
# A relative `cd ../x` that resolves outside the worktree, `pushd`, or any
# other cd/pushd target, caught here so `_kill_abs_cd` can resolve it
# against the worktree root itself.
_KILL_CD_RE = re.compile(
    r"""(?:^|[;&|(]|\bthen|\bdo|\belse)\s*(?:cd|pushd)\s+(?:--\s+)?["']?([^\s"';&|)]+)""",
    re.MULTILINE,
)
# `env -C <dir> ...` changes the subprocess's working directory before the
# command even starts -- the same escape as an absolute cd.
_KILL_ENV_C_RE = re.compile(
    r"""(?:^|[;&|(]|\bthen|\bdo|\belse)\s*env\s+(?:[A-Za-z_]\w*=\S*\s+)*-C\s*["']?([^\s"';&|)]+)""",
    re.MULTILINE,
)
# A cd/pushd/env -C target built from a subshell or backtick (`` `cmd` `` /
# `$(cmd)`) cannot be resolved statically; treat it as leaving the worktree
# (the fail-safe direction: `n/a` over a false `survived`).
_KILL_DYNAMIC_CD_RE = re.compile(r"[`$]")


def kill_check_enabled(mailbox: Path | None, *, cli_disabled: bool = False) -> bool:
    """Port of trioctl's ``_kill_check_enabled`` (~2210-2224), taking the
    already-resolved mailbox directly instead of an ``argparse.Namespace``.

    Off when *cli_disabled* (the driver's own ``--no-kill-check`` flag),
    when ``TRIO_KILL_CHECK`` is one of ``0``/``false``/``off``/``no``
    (case-insensitive), or when the mailbox's ``.driver.json`` has
    ``kill_check: false``.
    """
    if cli_disabled:
        return False
    if os.environ.get(KILL_CHECK_ENV, "").strip().lower() in ("0", "false", "off", "no"):
        return False
    if mailbox is not None:
        try:
            data = json.loads((Path(mailbox) / ".driver.json").read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("kill_check") is False:
                return False
        except (OSError, ValueError):
            pass
    return True


def kill_check_budget(mailbox: Path | None) -> float:
    """Port of trioctl's ``_kill_check_budget`` (~2227-2242):
    ``TRIO_KILL_CHECK_BUDGET_S`` env var, else the mailbox's PLAN.md
    ``full_check_budget_s:``, else :data:`KILL_CHECK_DEFAULT_BUDGET_S`."""
    env = os.environ.get(KILL_CHECK_BUDGET_ENV, "").strip()
    if env:
        try:
            return max(1.0, float(env))
        except ValueError:
            pass
    if mailbox is not None:
        try:
            plan = (Path(mailbox) / "PLAN.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            plan = ""
        m = re.search(r"^\s*full_check_budget_s\s*:\s*(\d+(?:\.\d+)?)", plan, re.MULTILINE)
        if m:
            return max(1.0, float(m.group(1)))
    return KILL_CHECK_DEFAULT_BUDGET_S


def _kill_ls_files(path: Path) -> list[str]:
    out = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    return sorted({p for p in out.split("\0") if p})


def _kill_file_state(path: Path, rel: str) -> tuple[str, bytes] | None:
    """(kind+mode, content) of one worktree file; None when absent."""
    full = path / rel
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return None
    if os.path.islink(full):
        return ("link", os.readlink(full).encode("utf-8", "surrogateescape"))
    if not os.path.isfile(full):
        return ("other", b"")
    with open(full, "rb") as fh:
        return (f"file:{st.st_mode & 0o777:o}", fh.read())


def _kill_tree_state(path: Path) -> dict[str, tuple[str, str] | None]:
    """rel -> (kind+mode, sha256) for every tracked or untracked, non-ignored file."""
    state: dict[str, tuple[str, str] | None] = {}
    for rel in _kill_ls_files(path):
        got = _kill_file_state(path, rel)
        state[rel] = None if got is None else (got[0], hashlib.sha256(got[1]).hexdigest())
    return state


def _kill_tree_digest(state: dict[str, tuple[str, str] | None]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(state):
        entry = state[rel]
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(b"-" if entry is None else f"{entry[0]}:{entry[1]}".encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _kill_write(path: Path, rel: str, state: tuple[str, bytes] | None) -> None:
    full = path / rel
    if full.is_symlink() or full.exists():
        if full.is_dir() and not full.is_symlink():
            shutil.rmtree(full)
        else:
            full.unlink()
    if state is None:
        return
    full.parent.mkdir(parents=True, exist_ok=True)
    kind, data = state
    if kind == "link":
        os.symlink(data.decode("utf-8", "surrogateescape"), full)
        return
    with open(full, "wb") as fh:
        fh.write(data)
    if kind.startswith("file:"):
        os.chmod(full, int(kind[5:], 8))


def _kill_base_state(path: Path, base: str, rel: str) -> tuple[str, bytes] | None:
    """(kind+mode, content) of *rel* at *base*; None when *base* lacks it."""
    listed = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), "ls-tree", "-z", base, "--", rel],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape").strip("\0")
    if not listed:
        return None
    meta, _name = listed.split("\t", 1)
    mode, kind, sha = meta.split()
    if kind != "blob":
        return None
    data = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), "cat-file", "blob", sha],
        capture_output=True, check=True,
    ).stdout
    if mode == "120000":
        return ("link", data)
    return (f"file:{int(mode[-3:], 8):o}", data)


def _kill_changed_paths(path: Path, base: str) -> list[str]:
    tracked = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), "diff", "--no-renames", "--name-only", "-z", base],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    untracked = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(path), "ls-files", "-z", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    return sorted({p for p in (tracked + "\0" + untracked).split("\0") if p})


def _kill_first_line(output: str, pattern: re.Pattern[str]) -> str:
    for ln in output.splitlines():
        if ln.strip() and pattern.search(ln):
            return ln.strip()[:200]
    return ""


def _kill_classify(
    returncode: int | None, output: str, command: str = "",
) -> tuple[str, str, dict[str, Any]]:
    """(outcome, reason, extra) of the re-run against the reverted product.

    `killed` only on a runner's assertion / failed-test report; a missing
    runner or module, usage error, `cd` failure, pytest exit 4/5, collection
    error or any unmatched non-zero exit is `error`."""
    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    last = lines[-1].strip()[:200] if lines else ""
    if returncode == 0:
        return "survived", f"targeted check still passes without the slice's product change ({last})", {}
    assertion = _kill_first_line(output, _KILL_ASSERTION_RE)
    pytest_exit = returncode in (4, 5) and "pytest" in f"{command}\n{output}"
    if returncode in (126, 127) or pytest_exit or (
        not assertion and _KILL_NOT_RUNNABLE_RE.search(output)
    ):
        why = _kill_first_line(output, _KILL_NOT_RUNNABLE_RE) or last
        return "error", f"targeted check not runnable (exit {returncode}: {why})", {}
    if assertion:
        return "killed", assertion, {}
    collection = _kill_first_line(output, _KILL_COLLECTION_RE)
    if collection:
        return ("error", f"collection/import error, not an assertion failure (exit {returncode}: "
                f"{collection})", {"collection_error": True})
    fail_line = _kill_first_line(output, _KILL_FAIL_LINE_RE)
    if fail_line:
        return "killed", fail_line, {}
    return "error", f"non-zero exit without a recognised assertion failure (exit {returncode}: {last})", {}


def _kill_run(
    command: str, path: Path, env: dict[str, str], budget: float,
    result: dict[str, Any],
) -> tuple[str, str]:
    """Run the targeted check in its own process group; kill the whole group
    on timeout so nothing keeps writing into the worktree during restore."""
    try:
        proc = subprocess.Popen(
            ["sh", "-c", command], cwd=str(path), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", start_new_session=True,
        )
    except OSError as exc:
        return ("error", f"cannot run the targeted check: {exc}")
    try:
        output, _ = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=10)
        return ("error", f"timeout after {budget:g}s")
    finally:
        # Grandchildren left behind by the check must not outlive it.
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    result["exit"] = proc.returncode
    outcome, reason, extra = _kill_classify(proc.returncode, output or "", command)
    result.update(extra)
    return outcome, reason


_KILL_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".next", ".turbo",
})


def _kill_dirs(path: Path) -> set[str]:
    """Every directory under *path* (heavy dependency/cache dirs pruned)."""
    found: set[str] = set()
    for top, dirs, _files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _KILL_SKIP_DIRS]
        found.update(os.path.join(top, d) for d in dirs)
    return found


def _kill_prune_dirs(path: Path, existed: set[str]) -> None:
    """Remove directories the revert or the check created that are empty
    again (deepest first); pre-existing directories are never touched."""
    for d in sorted(_kill_dirs(path) - existed, key=len, reverse=True):
        with contextlib.suppress(OSError):
            os.rmdir(d)


def _kill_restore(
    path: Path,
    base: str,
    before: dict[str, tuple[str, str] | None],
    saved: dict[str, tuple[str, bytes] | None],
) -> list[str]:
    """Put every file back as *before* recorded it; return paths still off.

    Every path the slice changed comes back from *saved* (the in-memory
    snapshot) FIRST -- including `.gitignore` files, so the listing below
    is taken under the builder's ignore rules and its newly ignored files
    are never mistaken for check output. Any other file the check touched
    was unchanged vs *base*, so the base blob is it."""
    for rel in sorted(saved):
        _kill_write(path, rel, saved[rel])
    after = _kill_tree_state(path)
    for rel in sorted(set(before) | set(after)):
        if rel in saved or (before.get(rel) == after.get(rel) and rel in before):
            continue
        if rel not in before or before[rel] is None:
            _kill_write(path, rel, None)  # created by the check
            continue
        state = _kill_base_state(path, base, rel)
        if state is not None:
            _kill_write(path, rel, (before[rel][0], state[1]))
    final = _kill_tree_state(path)
    off = {rel for rel in set(before) | set(final) if before.get(rel) != final.get(rel)}
    # Byte-identity over every snapshotted path (ignored ones included).
    off.update(rel for rel, state in saved.items() if _kill_file_state(path, rel) != state)
    return sorted(off)


def _kill_abs_cd(command: str, path: Path) -> tuple[str, str] | None:
    """(target, kind) for the first `cd`/`pushd`/`env -C` that leaves *path*.

    `kind` is `"absolute cd"` for an absolute or `~` cd/pushd target, or
    `"leaves worktree"` for a relative `cd ../x` (or deeper) that resolves
    outside the worktree, `env -C <dir>`, or a backtick/`$(...)` target
    that cannot be resolved statically at all (the fail-safe direction:
    `n/a`, not a false `survived`)."""
    try:
        root = path.resolve()
    except OSError:
        root = path
    for regex in (_KILL_CD_RE, _KILL_ENV_C_RE):
        for m in regex.finditer(command):
            target = m.group(1)
            if not target or target == "-":
                continue
            is_abs_form = bool(regex is _KILL_CD_RE and (target.startswith("/") or target.startswith("~")))
            kind = "absolute cd" if is_abs_form else "leaves worktree"
            if _KILL_DYNAMIC_CD_RE.search(target):
                return target, kind
            try:
                expanded = os.path.expanduser(target)
                resolved = (Path(expanded) if os.path.isabs(expanded) else root / expanded).resolve()
            except (OSError, RuntimeError):
                return target, kind
            if resolved != root and root not in resolved.parents:
                return target, kind
    return None


def run_kill_check(
    path: Path,
    base: str,
    command: str | None,
    *,
    mailbox_rel: str | None = None,
    budget: float = KILL_CHECK_DEFAULT_BUDGET_S,
) -> dict[str, Any]:
    """Base-revert kill check of one builder worktree.

    Verbatim port of trioctl's ``run_kill_check`` (~2659-2742). Reverts the
    slice's non-test product files to *base*, re-runs *command* under
    *budget*, then restores EVERY path the slice changed (tracked and
    untracked) from an in-memory snapshot, proven byte-identical per path
    and by a sha256 digest over the whole tree both before and after. The
    outcome (``killed``/``survived``/``n/a``/``error``) is informational
    only -- a failed restore proof is still recorded (``error``, reason
    ``restore: ...``), never raised, and the caller's retire decision is
    never read from this result.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"mode": "shadow"}
    if not command:
        result.update(outcome="n/a", reason="no `## Targeted check` command in the brief")
        return result
    result["command"] = command
    outside = _kill_abs_cd(command, path)
    if outside:
        target, kind = outside
        result.update(outcome="n/a", reason=f"{kind}: the check leaves the worktree ({target})")
        return result
    try:
        all_changed = _kill_changed_paths(path, base)
    except (OSError, subprocess.CalledProcessError) as exc:
        result.update(outcome="error", reason=f"cannot diff against base: {exc}")
        return result
    changed = all_changed
    if mailbox_rel:
        prefix = mailbox_rel.rstrip("/") + "/"
        changed = [p for p in changed if not (p + "/").startswith(prefix)]
    tests = [p for p in changed if _KILL_TEST_PATH_RE.search(p)]
    product = [p for p in changed
               if not _KILL_TEST_PATH_RE.search(p) and not _KILL_NON_PRODUCT_RE.search(p)]
    result["reverted"] = product
    result["tests"] = tests
    if not product:
        result.update(outcome="n/a", reason="no non-test product file changed")
        return result
    if not tests:
        result.update(outcome="n/a", reason="the slice changed no test file")
        return result
    try:
        before = _kill_tree_state(path)
        # Snapshot EVERY changed path (product, tests, fixtures, receipts,
        # mailbox files): the restore never falls back to the index.
        saved = {rel: _kill_file_state(path, rel) for rel in all_changed}
        base_states = {rel: _kill_base_state(path, base, rel) for rel in product}
        existed = _kill_dirs(path)
    except (OSError, subprocess.CalledProcessError) as exc:
        result.update(outcome="error", reason=f"cannot snapshot the worktree: {exc}")
        return result
    result["tree_sha256"] = _kill_tree_digest(before)
    result["snapshot_paths"] = len(saved)
    outcome: tuple[str, str] = ("error", "the targeted check did not run")
    try:
        for rel, state in base_states.items():
            _kill_write(path, rel, state)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        outcome = _kill_run(command, path, env, budget, result)
    finally:
        off: list[str] = []
        for _attempt in range(2):  # one retry: a straggler may still have been writing
            try:
                off = _kill_restore(path, base, before, saved)
                _kill_prune_dirs(path, existed)
            except (OSError, subprocess.CalledProcessError) as exc:
                off = [f"<restore failed: {exc}>"]
            if not off:
                break
    after = _kill_tree_state(path)
    result["tree_sha256_after"] = _kill_tree_digest(after)
    result["restored"] = not off and result["tree_sha256_after"] == result["tree_sha256"]
    if result["restored"]:
        result.update(outcome=outcome[0], reason=outcome[1])
    else:
        result["restore_mismatch"] = off[:20] or ["<tree digest differs>"]
        result["check_outcome"] = outcome[0]
        result.update(
            outcome="error",
            reason=f"restore: the worktree could not be proven byte-identical "
                   f"({', '.join(result['restore_mismatch'][:5])}); the check said "
                   f"{outcome[0]}: {outcome[1]}",
        )
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def kill_check_for_builder(
    worktree: Path,
    base: str,
    brief: str,
    targeted: str | None,
    *,
    mailbox_rel: str | None = None,
    budget: float = KILL_CHECK_DEFAULT_BUDGET_S,
) -> dict[str, Any] | None:
    """The decision part of trioctl's ``_isolated_kill_check`` (~2784-2831):
    whether to run :func:`run_kill_check` for one exited builder, and its
    result.

    Returns ``None`` unless *targeted* is a passing ``TARGETED_CHECK`` line
    (see :func:`targeted_check_failed`) AND the brief has a plausible
    targeted command (see :func:`brief_targeted_command`); otherwise it
    returns :func:`run_kill_check`'s result. No ledger-record persistence
    happens here (trioctl's own version writes the result into the
    worktree record) -- the caller stores the result wherever this
    driver's builder-run bookkeeping lives.

    Deviation from trioctl: ``_isolated_kill_check`` always returns a dict
    (an explicit ``n/a`` placeholder) even when the targeted check failed
    or the brief has no command; this port returns ``None`` in both of
    those cases instead, since the caller here has no builder record to
    persist a placeholder into and can treat "not run" as "nothing to
    report" directly.
    """
    if targeted_check_failed(targeted):
        return None
    command = brief_targeted_command(brief)
    if command is None:
        return None
    return run_kill_check(Path(worktree), base, command, mailbox_rel=mailbox_rel, budget=budget)


# ==========================================================================
# 4. L1 evidence telemetry (trioctl ~2833-2971, ~7257-7314, ~7465-7541)
# ==========================================================================

EVIDENCE_KINDS = ("re-run", "probe", "implementer-test", "receipt", "unverified")
_EVIDENCE_LINE_RE = re.compile(r"^\s*[-*]?\s*`?evidence:\s*(.*?)`?\s*$", re.IGNORECASE)
_EVIDENCE_PAIR_RE = re.compile(r"([a-z][a-z-]*)\s*=\s*(\d+)", re.IGNORECASE)
_EVIDENCE_ALIASES = {"rerun": "re-run", "impl-test": "implementer-test",
                     "implementer": "implementer-test", "impl": "implementer-test"}
_SLICE_VERDICT_HEADING_RE = re.compile(
    r"^##\s+slice\s+(\S+)\s+@([0-9a-fA-F]{4,40})\s+(?:—|--|-)\s+(SHIP|ITERATE)\s*$"
)


def _slice_section(text: str, slice_id: str, sha: str) -> tuple[str, str] | None:
    """(verdict word, body) of the LAST ``## slice <id> @<sha> — ...``
    section. Verbatim port of trioctl's ``_slice_section`` (~2840-2855)."""
    want = sha.strip().lower()
    found = None
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = _SLICE_VERDICT_HEADING_RE.match(raw.strip())
        if not m or m.group(1) != slice_id:
            continue
        got = m.group(2).lower()
        if want and not (got == want or want.startswith(got) or got.startswith(want)):
            continue
        end = next((j for j in range(i + 1, len(lines))
                    if lines[j].startswith("## ")), len(lines))
        found = (m.group(3), "\n".join(lines[i + 1:end]))
    return found


def slice_evidence(text: str, slice_id: str, sha: str) -> dict[str, Any] | None:
    """Evidence kinds and attack count of one slice section (r18a L1).

    Verbatim port of trioctl's ``slice_evidence`` (~2858-2915).

    The ``evidence: re-run=<n> ...`` summary line wins; otherwise the kinds
    named in the per-accept table rows are counted. ``None`` when the
    section is absent. A section without ``attacks:`` reports
    ``attacks: "n/a"`` (slice-evals do not list attacks; that is a
    whole-goal duty).
    """
    found = _slice_section(text, slice_id, sha)
    if found is None:
        return None
    verdict, body = found
    counts: dict[str, int] = {}
    for ln in body.splitlines():
        m = _EVIDENCE_LINE_RE.match(ln)
        if not m:
            continue
        pairs = _EVIDENCE_PAIR_RE.findall(m.group(1))
        if pairs:
            counts = {}
            for key, value in pairs:
                key = _EVIDENCE_ALIASES.get(key.lower(), key.lower())
                if key in EVIDENCE_KINDS:
                    counts[key] = counts.get(key, 0) + int(value)
    source = "summary" if counts else None
    if not counts:
        for ln in body.splitlines():
            if not ln.lstrip().startswith("|") or set(ln.strip()) <= set("|-: "):
                continue
            cells = [c.strip().strip("`").lower() for c in ln.strip().strip("|").split("|")]
            kinds = [_EVIDENCE_ALIASES.get(c, c) for c in cells]
            hit = next((k for k in kinds if k in EVIDENCE_KINDS[:4]), None)
            if hit:
                counts[hit] = counts.get(hit, 0) + 1
            if any(c == "unverified" for c in cells):
                counts["unverified"] = counts.get("unverified", 0) + 1
        source = "table" if counts else "missing"
    attacks: int | str = 0
    inside = False
    seen_attacks = False
    for ln in body.splitlines():
        if re.match(r"^\s*`?attacks\s*:", ln, re.IGNORECASE):
            inside = True
            seen_attacks = True
            continue
        if inside:
            if re.match(r"^\s*[-*]\s+\S", ln):
                attacks += 1
            elif ln.strip():
                inside = False
    if not seen_attacks:
        attacks = "n/a"
    ordered = {k: counts.get(k, 0) for k in EVIDENCE_KINDS} if counts else {}
    return {"verdict": verdict, "evidence": ordered, "evidence_source": source,
            "attacks": attacks}


def kill_check_suffix(kill_check: dict[str, Any] | None) -> str:
    """``kill_check: <outcome>`` for LOG lines (empty when not run).
    Verbatim port of trioctl's ``kill_check_suffix`` (~2960-2971)."""
    if not isinstance(kill_check, dict) or not kill_check.get("outcome"):
        return ""
    tag = ""
    if kill_check.get("restored") is False:
        tag = " (restore)"
    elif str(kill_check.get("reason") or "").startswith("absolute cd"):
        tag = " (absolute cd)"
    elif str(kill_check.get("reason") or "").startswith("leaves worktree"):
        tag = " (leaves worktree)"
    return f"kill_check: {kill_check['outcome']}{tag}"


def evidence_log_line(iteration: int, slice_id: str, sha: str, ev: dict[str, Any]) -> str:
    """The LOG line trioctl's ``_quality_after_dispatch`` appends for a
    slice-eval's evidence (trioctl ~7284-7293):
    ``- iter N | loop | slice <id> @<sha> <verdict> evidence: k=v...
    attacks=N (shadow)``.

    *ev* is a :func:`slice_evidence` result (its ``verdict``, ``evidence``
    and ``attacks`` keys)."""
    evidence = ev.get("evidence")
    summary = " ".join(f"{k}={v}" for k, v in evidence.items()) if evidence else "missing"
    return (
        f"- iter {iteration} | loop | slice {slice_id} @{sha[:12]} "
        f"{ev.get('verdict', '?')} evidence: {summary} "
        f"attacks={ev.get('attacks', 0)} (shadow)"
    )


def retired_log_line(
    iteration: int, slice_id: str, sha: str, authored_by: str,
    kill_check: dict[str, Any] | None,
) -> str:
    """The LOG line trioctl's ``_quality_before_dispatch`` appends the
    first time a slice's quality facts are recorded (trioctl ~7535-7539):
    ``- iter N | loop | retired slice <id> @<sha> by <builder|lead> |
    kill_check: <outcome> (shadow)``."""
    return (
        f"- iter {iteration} | loop | retired slice {slice_id} @{sha[:12]} "
        f"by {authored_by} | {kill_check_suffix(kill_check)} (shadow)"
    )


# ==========================================================================
# 5. Lints (trioctl ~2745-2781, ~7318-7420)
# ==========================================================================

def builder_test_flags(
    worktree: Path, base: str, mailbox_rel: str | None, brief: str | None = None,
) -> list[str]:
    """r18a L7: advisory tautology flags over the slice's changed test
    files in one builder worktree (deterministic AST/regex lint from
    ``trio-check.py``; no model call). Port of trioctl's
    ``_isolated_verification_flags`` (~2745-2781), over the worktree
    directly instead of a ``worker_worktrees`` record (no ledger write
    here -- the caller persists the flags wherever it tracks the builder
    run).

    *mailbox_rel* excludes the mailbox's own path from the changed-files
    scan, as :func:`run_kill_check` does. *brief* is optional: when given,
    also applies trio-check's ``empty_tsconfig_flag`` to the brief's
    targeted command (trioctl always has the brief in hand; this port
    takes it separately since a take-over lint, :func:`slice_lint`, has no
    single builder worktree/brief to call this with).
    """
    loaded = load_trio_check()
    if loaded is None:
        return []
    module, _metrics = loaded
    try:
        path = Path(worktree)
        changed = _kill_changed_paths(path, str(base))
        if mailbox_rel:
            prefix = mailbox_rel.rstrip("/") + "/"
            changed = [p for p in changed if not (p + "/").startswith(prefix)]
        tests = [p for p in changed if module.TEST_FILE_RE.search(p)]
        modules = module.product_modules(
            p for p in changed
            if not _KILL_TEST_PATH_RE.search(p) and not _KILL_NON_PRODUCT_RE.search(p)
        )
        flags: list[str] = []
        for rel in tests:
            try:
                text = (path / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            flags.extend(module.test_file_flags(rel, text, modules or None))
        if brief is not None:
            flags.extend(module.empty_tsconfig_flag(path, brief_targeted_command(brief)))
    except Exception as exc:  # noqa: BLE001 - advisory: never break a dispatch
        print(f"trio-opencode: verification lint skipped: {exc}", file=sys.stderr)
        return []
    return flags


def slice_lint(
    mailbox: Path, repo: Path, slice_id: str, sha: str, builder_flags: list[str] | None,
) -> tuple[list[str], list[str]]:
    """(test tautology flags, accept findings) for one slice-eval.

    Port of trioctl's ``AgentRunner._slice_lint`` (~7358-7420), without the
    class: *mailbox* and *repo* are passed in directly instead of being
    resolved from ``self``/``context``. Builder slices reuse the builder
    run's flags (*builder_flags* not ``None``); a Lead take-over (no
    builder ran, *builder_flags* ``None``) is linted here over the files
    its ``slice(<id>):`` commits changed, read at *sha*. The slice's
    ``accepts:`` are linted either way. Advisory; never raises.
    """
    flags: list[str] = list(builder_flags or [])
    accepts: list[str] = []
    loaded = load_trio_check()
    if loaded is None:
        return flags, accepts
    module, metrics = loaded
    try:
        plan = _mailbox_text(Path(mailbox) / "PLAN.md")
        for sl in metrics.parse_slices_block(plan) or []:
            if str(sl.get("id")) != slice_id:
                continue
            for n, item in enumerate(sl.get("accepts") or [], 1):
                for level, why in module.accept_findings(item):
                    if level in ("REJECT", "WARN"):
                        clipped = " ".join(str(item).split())[:90]
                        accepts.append(f"accept {n} {clipped!r}: {level} {why}")
    except Exception as exc:  # noqa: BLE001
        print(f"trio-opencode: accept lint skipped: {exc}", file=sys.stderr)
    if builder_flags is not None or not sha:
        return flags, accepts
    try:
        def git(*argv: str) -> str:
            return subprocess.run(
                ["git", "--no-replace-objects", "-C", str(repo), "--no-optional-locks", *argv],
                capture_output=True, text=True, errors="replace", timeout=60,
                env={**os.environ, **GIT_NO_REPLACE_ENV},
            ).stdout

        changed: list[str] = []
        for line in git("log", "-60", "--format=%H%x09%s", sha).splitlines():
            commit, _tab, subject = line.partition("\t")
            if not subject.startswith(f"slice({slice_id}):"):
                continue
            for rel in git("diff-tree", "--no-commit-id", "--name-only", "-r",
                           "--no-renames", commit).splitlines():
                if rel and rel not in changed:
                    changed.append(rel)
        modules = module.product_modules(
            p for p in changed
            if not _KILL_TEST_PATH_RE.search(p) and not _KILL_NON_PRODUCT_RE.search(p)
        )
        for rel in changed[:200]:
            if not module.TEST_FILE_RE.search(rel):
                continue
            text = git("show", f"{sha}:{rel}")
            if text:
                flags.extend(module.test_file_flags(rel, text, modules or None))
    except Exception as exc:  # noqa: BLE001
        print(f"trio-opencode: take-over lint skipped: {exc}", file=sys.stderr)
    return flags, accepts


def lead_pass_lint(mailbox: Path, iteration: int) -> dict[str, Any] | None:
    """Port of trioctl's ``_lint_after_lead_pass`` (~7318-7347): run
    trio-check's ``quality_findings`` (accepts grammar, goal lines,
    reader-only full_check, mailbox test tautologies) over the mailbox
    after every Lead pass. Returns the ``.driver.json`` ``lint`` entry
    shape, or ``None`` when trio-check is unavailable or the lint itself
    raises. Advisory."""
    loaded = load_trio_check()
    if loaded is None:
        return None
    module, metrics = loaded
    try:
        findings = module.quality_findings(Path(mailbox), metrics)
    except Exception as exc:  # noqa: BLE001 - advisory: never break a loop
        print(f"trio-opencode: verification lint skipped: {exc}", file=sys.stderr)
        return None
    counts: dict[str, int] = {}
    for level, _msg in findings:
        counts[level] = counts.get(level, 0) + 1
    return {
        "iteration": iteration,
        "counts": counts,
        "findings": [f"{lv} {msg}" for lv, msg in findings[:60]],
        "mode": "advisory",
    }


# ==========================================================================
# 6. The slice-eval OPEN-LOOP CONTEXT note (trioctl ~7465-7541)
# ==========================================================================

#: trioctl's own `n/a` placeholder reasons (`_quality_before_dispatch`
#: ~7480-7491), used verbatim by :func:`resolved_kill_check` -- never
#: paraphrased, so the Evaluator (and a human comparing LOG.md against a
#: lockstep/Omnigent run) sees the identical text either way.
_NO_BUILDER_REASON = "no builder run merged this sha (Lead take-over or fix)"
_NOT_RECORDED_REASON = "not recorded (kill check off or pre-r18a trioctl)"


def resolved_kill_check(
    isolate: bool, kill_check: dict[str, Any] | None, authored_by: str | None,
) -> dict[str, Any] | None:
    """The BASE-REVERT fact trioctl's ``_quality_before_dispatch`` builds
    (~7480-7491) BEFORE rendering either the SLICE QUALITY note or the
    retired LOG line -- the single source both :func:`quality_note` and
    the caller's retired-LOG-line call share, so the two always agree.

    ``None`` when *isolate* is false (no builder/lead telemetry to report
    at all, matching trioctl's own ``if self._isolate: ...`` guard). The
    real *kill_check* dict when one was actually recorded. Otherwise one of
    trioctl's own ``n/a`` placeholders, chosen by *authored_by*:
    :data:`_NO_BUILDER_REASON` for a Lead take-over/fix (no builder record
    at all), :data:`_NOT_RECORDED_REASON` for a builder slice whose own
    kill check was never run (kill check off, or no targeted command in the
    brief). Faithful parity (ol-harden blocking issue #3): a ``None`` fact
    is never silently dropped under isolation -- BASE-REVERT/AUTHORED-BY
    always appear together, in every case.
    """
    if not isolate:
        return None
    if kill_check is not None:
        return kill_check
    if authored_by == "lead":
        return {"outcome": "n/a", "reason": _NO_BUILDER_REASON}
    return {"outcome": "n/a", "reason": _NOT_RECORDED_REASON}


def quality_note(
    *,
    isolate: bool,
    kill_check: dict[str, Any] | None,
    authored_by: str | None,
    flags: list[str],
    accept_lint: list[str],
    builder_ran: bool,
) -> str:
    """The exact text trioctl's ``_quality_before_dispatch`` (~7504-7529)
    builds into ``context["quality_note"]``, appended verbatim to the
    slice-eval procedure.

    ``""`` when there is nothing to say (no kill check fact and no lints).
    BASE-REVERT/AUTHORED-BY lines appear whenever *isolate* is true --
    :func:`resolved_kill_check` substitutes trioctl's own ``n/a``
    placeholder when *kill_check* is ``None`` (a Lead take-over/fix, a
    kill-check-off run, or a brief with no targeted command), so these
    lines are never silently omitted under isolation the way an unresolved
    ``None`` fact used to be. The PRE-GATE lines appear only when *flags*
    or *accept_lint* is non-empty. Same leading/trailing newlines as the
    source: no leading newline, exactly one trailing ``\\n``.
    """
    kc = resolved_kill_check(isolate, kill_check, authored_by)
    if kc is None and not flags and not accept_lint:
        return ""
    lines = ["SLICE QUALITY (r18a shadow; informs your grading, gates nothing):"]
    if kc is not None:
        reason = kc.get("reason")
        lines.append(
            f"BASE-REVERT: {kc.get('outcome')}" + (f" -- {reason}" if reason else "")
        )
        lines.append(f"AUTHORED-BY: {authored_by}")
    if flags or accept_lint:
        source = ("the builder's worktree" if builder_ran
                  else "the slice's commits at this sha (no builder ran)")
        lines.append(
            f"PRE-GATE: advisory verification lints over {source}; "
            "grade every listed item explicitly"
        )
    if flags:
        lines.append(
            "PRE-GATE FLAGS (advisory AST lint; the following tests look "
            "tautological -- grade them explicitly):"
        )
        lines.extend(f"- {f}" for f in flags[:20])
    if accept_lint:
        lines.append(
            "PRE-GATE ACCEPTS (advisory accept lint; an accept without a relation "
            "or oracle is graded `unverified` unless you establish one):"
        )
        lines.extend(f"- {f}" for f in accept_lint[:20])
    return "\n".join(lines) + "\n"

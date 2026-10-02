"""One role turn = one ``opencode run`` process (SPEC.md "Runner interface").

Stdlib-only. Spawns ``opencode run --format json ...`` with
``stdin=DEVNULL`` (the CLI hangs forever on an inherited/open pipe — see
SPEC.md), reads stdout line-by-line through a non-blocking
:mod:`selectors` loop, classifies the result via :mod:`trio_opencode.events`,
retries transient failures with backoff (optionally continuing the same
opencode session), and enforces wall-clock and idle timeouts by killing the
whole process group.

The provider key is read from ``spec.key_file`` at spawn time and passed
only in the child's environment; it is never put in argv, never logged, and
every line written to the per-attempt log files is scrubbed of it.
"""
from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import events

MAX_PROMPT_BYTES = 120_000

#: Required capabilities of any supported ``opencode run`` CLI (SPEC.md
#: "Feature detection: ... Required: json format, --model, --agent,
#: --session."). Missing any of these -> a config_error before spawning
#: anything.
_REQUIRED_CAPS = ("format_json", "model_flag", "agent_flag", "session_flag")
_REQUIRED_CAPS_LABEL = {
    "format_json": "json format", "model_flag": "model",
    "agent_flag": "agent", "session_flag": "session",
}

_CONTINUE_PROMPT = (
    "Continue: your previous turn was interrupted by a provider error; "
    "finish the task."
)

_PERMISSION_TEXT_RE = re.compile(r"permission requested:|auto-rejecting", re.IGNORECASE)
_SECRET_LINE_RE = re.compile(r"authorization|api_key|apikey", re.IGNORECASE)

_RETRYABLE_KINDS = {"transient", "idle_timeout"}


def _log_idle_watchdog_retry(spec: "TurnSpec", attempt: int) -> None:
    """One clearly-labelled stderr line per unbounded idle-watchdog retry
    (README.md "Container / no-time-limit mode"): this retry does NOT count
    against ``retries.max_attempts`` -- it keeps happening for as long as
    the turn keeps going idle, so the attempt number alone (not "N of
    max_attempts") is what identifies it."""
    label = spec.label or spec.role
    print(
        f"trio-opencode: idle watchdog retry (attempt {attempt}, {label}): "
        f"no stdout for idle_timeout={spec.idle_timeout}s; retrying without "
        "counting against retries.max_attempts (retries.idle_retry_unlimited=true)",
        file=sys.stderr,
    )
    sys.stderr.flush()


@dataclass
class TurnSpec:
    role: str
    agent: str
    model: str
    prompt: str
    cwd: str
    variant: str | None = None
    session_id: str | None = None
    label: str = ""
    env: dict[str, str] | None = None
    #: ``0`` (or ``None``) disables the wall-clock limit for this turn (see
    #: README.md "Container / no-time-limit mode"); the ``_pump`` loop's
    #: wall-clock check is simply skipped while falsy.
    turn_timeout: float | None = 3600.0
    idle_timeout: float = 600.0
    max_attempts: int = 3
    backoff: tuple[float, ...] = (10.0, 30.0, 90.0)
    #: When true, an ``idle_timeout`` is retried forever (never counted
    #: against ``max_attempts``) instead of becoming a final failure -- a
    #: hung connection never ends the run on its own (README.md "Container /
    #: no-time-limit mode").
    idle_retry_unlimited: bool = False
    log_dir: str | None = None
    opencode_bin: str = "opencode"
    key_file: str | None = None


@dataclass
class CliCaps:
    """Feature-detected shape of one ``opencode`` binary's ``run`` subcommand
    (SPEC.md "Feature detection"). ``style`` is ``"v2"`` when ``--standalone``
    is offered and ``--dir`` is not, ``"v1"`` when ``--dir`` is offered,
    else ``"unknown"``. ``missing`` lists the human-readable names of any
    required capability (json format / model / agent / session) the binary's
    own ``run --help`` did not advertise — non-empty means the runner must
    refuse the turn with a config_error before spawning anything."""
    style: str = "unknown"
    standalone: bool = False
    dir_flag: bool = False
    variant_flag: bool = False
    session_flag: bool = False
    format_json: bool = False
    model_flag: bool = False
    agent_flag: bool = False
    print_logs: bool = False
    log_level: bool = False
    version: str | None = None
    missing: list[str] = field(default_factory=list)
    help_text: str = ""


@dataclass
class TurnResult:
    ok: bool
    kind: str
    text: str
    session_id: str | None
    error: str | None
    exit_code: int | None
    attempts: int
    events: int
    tokens: dict = field(default_factory=dict)
    denials: list[str] = field(default_factory=list)
    log_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Process-group helpers
# ---------------------------------------------------------------------------


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process_group(pgid: int, grace: float = 5.0) -> None:
    """SIGTERM the whole process group, wait up to ``grace`` seconds, then
    SIGKILL anything left. Missing groups (already dead) are ignored."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pgid_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def is_opencode_process(pid: int) -> bool:
    """True if ``/proc/<pid>/cmdline`` mentions "opencode" anywhere (the
    fake executable's argv0 is also named ``opencode``)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    text = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part)
    return "opencode" in text.lower()


# ---------------------------------------------------------------------------
# Log scrubbing
# ---------------------------------------------------------------------------


def _scrub_line(line: str, secret: str | None) -> str:
    if secret and secret in line:
        return "[redacted]"
    if _SECRET_LINE_RE.search(line):
        return "[redacted]"
    return line


def _scrub_file_in_place(path: Path, secret: str | None) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    scrubbed = "\n".join(_scrub_line(line, secret) for line in raw.split("\n"))
    try:
        path.write_text(scrubbed, encoding="utf-8")
    except OSError:
        pass
    return scrubbed


def _slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "").strip())
    s = s.strip("-")
    return s or "turn"


# ---------------------------------------------------------------------------
# Feature detection (SPEC.md "Feature detection" / OpenCode v2.0.20 facts)
# ---------------------------------------------------------------------------

_CLI_CAPS_CACHE: dict[tuple[str, float], CliCaps] = {}

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _probe_env(spec: TurnSpec) -> dict[str, str]:
    env = os.environ.copy()
    if spec.env:
        env.update(spec.env)
    return env


def pwd_env(env: dict[str, str], cwd: str) -> dict[str, str]:
    """Returns a copy of ``env`` with ``PWD`` set to ``cwd``'s resolved
    absolute path. **OpenCode v2 resolves the working directory its agent's
    shell/tools operate in from the inherited ``$PWD`` environment
    variable, not from the child process's actual working directory** (the
    ``cwd=`` argument passed to ``Popen``/``subprocess.run``) — live-verified
    by the coordinator: spawning ``opencode run --standalone`` with
    ``cwd=D`` but an inherited ``PWD=P`` makes the agent operate in ``P``,
    not ``D``. Every spawn of ``opencode`` (a real turn, a feature-detection
    probe, a `models`/live-probe check) must therefore always pass a ``PWD``
    that matches the ``cwd=`` it is given, never the inherited one — this is
    the single place that invariant is enforced."""
    env = dict(env)
    env["PWD"] = str(Path(cwd).resolve())
    return env


def _detect_cli_uncached(bin_path: str, env: dict[str, str], cwd: str) -> CliCaps:
    run_env = pwd_env(env, cwd)
    try:
        proc = subprocess.run(
            [bin_path, "run", "--help"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=20, env=run_env, cwd=cwd,
        )
        help_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CliCaps(style="unknown", missing=list(_REQUIRED_CAPS_LABEL.values()),
                       help_text=f"run --help failed: {exc}")

    standalone = "--standalone" in help_text
    dir_flag = re.search(r"(?<![\w-])--dir\b", help_text) is not None
    variant_flag = "--variant" in help_text
    session_flag = ("--session" in help_text
                    or re.search(r"(?<![\w-])-s\b", help_text) is not None)
    model_flag = ("--model" in help_text
                 or re.search(r"(?<![\w-])-m\b", help_text) is not None)
    agent_flag = "--agent" in help_text
    print_logs = "--print-logs" in help_text
    log_level = "--log-level" in help_text
    format_json = "--format" in help_text and "json" in help_text.lower()

    caps_present = {
        "format_json": format_json, "model_flag": model_flag,
        "agent_flag": agent_flag, "session_flag": session_flag,
    }
    missing = [_REQUIRED_CAPS_LABEL[k] for k in _REQUIRED_CAPS if not caps_present[k]]

    if standalone and not dir_flag:
        style = "v2"
    elif dir_flag:
        style = "v1"
    else:
        style = "unknown"

    version: str | None = None
    try:
        vproc = subprocess.run(
            [bin_path, "--version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=20, env=run_env, cwd=cwd,
        )
        vtext = (vproc.stdout or "") + (vproc.stderr or "")
        m = _VERSION_RE.search(vtext)
        version = m.group(0) if m else (vtext.strip() or None)
    except (OSError, subprocess.TimeoutExpired):
        version = None

    return CliCaps(
        style=style, standalone=standalone, dir_flag=dir_flag, variant_flag=variant_flag,
        session_flag=session_flag, format_json=format_json, model_flag=model_flag,
        agent_flag=agent_flag, print_logs=print_logs, log_level=log_level,
        version=version, missing=missing, help_text=help_text,
    )


def detect_cli(bin_path: str, env: dict[str, str], cwd: str | None = None) -> CliCaps:
    """Detects ``bin_path``'s ``opencode run`` feature set by running
    ``<bin> run --help`` once (``stdin=DEVNULL``, 20s timeout, the same
    isolated ``env`` a turn would use, spawned with ``cwd=cwd`` and
    ``PWD=cwd`` — see :func:`pwd_env`) and caches the result by
    ``(realpath, mtime)`` so a whole run's many turns probe the binary only
    once. ``cwd`` defaults to the current process's own working directory
    when the caller has no more specific one in mind (the probed
    ``--help``/``--version`` output never depends on it, so the cache key
    intentionally ignores ``cwd``)."""
    cwd = cwd or os.getcwd()
    resolved = shutil.which(bin_path, path=env.get("PATH")) or bin_path
    try:
        st = os.stat(resolved)
        key = (os.path.realpath(resolved), st.st_mtime)
    except OSError:
        key = (resolved, 0.0)
    cached = _CLI_CAPS_CACHE.get(key)
    if cached is not None:
        return cached
    caps = _detect_cli_uncached(bin_path, env, cwd)
    _CLI_CAPS_CACHE[key] = caps
    return caps


# ---------------------------------------------------------------------------
# argv / env construction
# ---------------------------------------------------------------------------


def _model_ref(spec: TurnSpec, caps: CliCaps) -> str:
    if spec.variant and caps.style == "v2":
        return f"{spec.model}#{spec.variant}"
    return spec.model


def _build_argv(spec: TurnSpec, caps: CliCaps, session_id: str | None, prompt: str) -> list[str]:
    safe_prompt = prompt
    if safe_prompt.startswith("-"):
        safe_prompt = "Task:\n" + safe_prompt

    argv = [spec.opencode_bin, "run"]
    if caps.style == "v2":
        argv.append("--standalone")
    argv += ["--format", "json"]
    if caps.print_logs:
        argv.append("--print-logs")
    if caps.log_level:
        argv += ["--log-level", "info" if caps.style == "v2" else "INFO"]
    argv += ["--agent", spec.agent]
    if caps.style == "v2":
        argv += ["-m", _model_ref(spec, caps)]
    else:
        argv += ["--model", spec.model]
        if spec.variant:
            argv += ["--variant", spec.variant]
        argv += ["--dir", spec.cwd]
    if session_id:
        argv += (["-s", session_id] if caps.style == "v2" else ["--session", session_id])
    argv.append(safe_prompt)
    assert "--auto" not in argv, "trio-opencode must never pass --auto to opencode"
    return argv


def _build_env(spec: TurnSpec, key_value: str) -> dict[str, str]:
    env = _probe_env(spec)
    env.pop("OPENCODE_API_KEY", None)
    env["OPENCODE_API_KEY"] = key_value
    return pwd_env(env, spec.cwd)


def _read_key(key_file: str | None) -> tuple[str | None, str | None]:
    """Returns ``(key_value, config_error_message)``; exactly one is set."""
    if not key_file:
        return None, "missing OPENCODE_API_KEY: no provider.key_file configured"
    try:
        with open(key_file, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None, f"cannot read key file: {key_file}"
    value = raw.strip("\r\n \t")
    if not value:
        return None, f"empty key file: {key_file}"
    return value, None


# ---------------------------------------------------------------------------
# The read/monitor loop for one attempt
# ---------------------------------------------------------------------------


def _handle_stdout_line(raw: bytes, out_fh, acc: events.TurnAccumulator, key_value: str | None) -> bool:
    """Decode and scrub the line exactly ONCE, then use that SAME scrubbed
    text for the log write, event parsing/feeding and the permission-text
    check — never the raw decoded line — so a model that echoes the
    provider key back in its own output can never reach
    :class:`~trio_opencode.events.TurnAccumulator` (and from there
    ``TurnResult.text``/``.error``/``.denials``): the key value itself is
    replaced by ``[redacted]`` (substring replace keeps the JSON event valid,
    so a turn whose output merely echoes the key still completes). Lines are
    NOT dropped for mentioning "authorization"/"api_key": that is ordinary
    product-code talk in an event stream (the log-file rule stays stricter).
    The permission-warning regex is applied only when the line did NOT parse
    as a JSON event: a parsed event (e.g. a ``tool_use`` part whose tool
    input/output text happens to contain the phrase, written by the model
    itself) is never scanned this way — the accumulator already records any
    JSON event whose own ``type`` contains "permission" via :meth:`~trio_
    opencode.events.TurnAccumulator.feed`. Returns whether the line matched
    the permission-warning pattern, so :func:`_pump` never has to re-decode/
    re-check the raw line itself for that."""
    text = raw.decode("utf-8", errors="replace")
    if key_value:
        text = text.replace(key_value, "[redacted]")
    out_fh.write(_scrub_line(text, key_value) + "\n")
    ev = events.parse_line(text)
    if ev is not None:
        acc.feed(ev)
        return False
    hit = bool(_PERMISSION_TEXT_RE.search(text))
    if hit:
        acc.permission_signals.append(text.strip())
    return hit


def _pump(
    proc: subprocess.Popen,
    pgid: int,
    out_fh,
    stderr_path: Path,
    acc: events.TurnAccumulator,
    spec: TurnSpec,
    key_value: str | None,
    cancel,
) -> str | None:
    """Reads stdout to EOF while enforcing timeouts/cancel and watching the
    stderr log for a permission warning. Returns an override kind
    (``"timeout"`` | ``"idle_timeout"`` | ``"cancelled"``) or ``None`` for a
    normal end-of-stream (which may still turn out to be "permission" once
    :func:`trio_opencode.events.classify` looks at the scrubbed stderr)."""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    start = time.monotonic()
    last_byte = time.monotonic()
    buf = b""
    stderr_pos = 0
    permission_killed = False
    stdout_open = True
    override: str | None = None

    while stdout_open:
        if cancel is not None and cancel.is_set():
            kill_process_group(pgid)
            override = "cancelled"
            break
        now = time.monotonic()
        if spec.turn_timeout and now - start > spec.turn_timeout:
            kill_process_group(pgid)
            override = "timeout"
            break
        if now - last_byte > spec.idle_timeout:
            kill_process_group(pgid)
            override = "idle_timeout"
            break

        for key, _mask in sel.select(timeout=0.2):
            try:
                chunk = os.read(key.fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    sel.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                stdout_open = False
                continue
            last_byte = time.monotonic()
            buf += chunk

        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            hit = _handle_stdout_line(line, out_fh, acc, key_value)
            if hit and not permission_killed:
                permission_killed = True
                kill_process_group(pgid)

        if not permission_killed:
            try:
                size = stderr_path.stat().st_size
            except OSError:
                size = stderr_pos
            if size > stderr_pos:
                try:
                    with open(stderr_path, "rb") as fh:
                        fh.seek(stderr_pos)
                        new_bytes = fh.read()
                except OSError:
                    new_bytes = b""
                stderr_pos = size
                new_text = events.strip_spawn_process_lines(new_bytes.decode("utf-8", "replace"))
                if _PERMISSION_TEXT_RE.search(new_text):
                    permission_killed = True
                    kill_process_group(pgid)

    if buf:
        _handle_stdout_line(buf, out_fh, acc, key_value)
    return override


# ---------------------------------------------------------------------------
# run_turn
# ---------------------------------------------------------------------------


def run_turn(
    spec: TurnSpec,
    on_spawn: Callable[[int, int], None] | None = None,
    cancel=None,
) -> TurnResult:
    if len(spec.prompt.encode("utf-8", "surrogatepass")) > MAX_PROMPT_BYTES:
        return TurnResult(
            ok=False, kind="config_error", text="", session_id=spec.session_id,
            error=f"prompt exceeds {MAX_PROMPT_BYTES} bytes", exit_code=None,
            attempts=0, events=0, tokens={}, denials=[], log_paths=[],
        )

    caps = detect_cli(spec.opencode_bin, _probe_env(spec), cwd=spec.cwd)
    if caps.missing:
        return TurnResult(
            ok=False, kind="config_error", text="", session_id=spec.session_id,
            error=f"unsupported opencode version: missing {', '.join(caps.missing)}",
            exit_code=None, attempts=0, events=0, tokens={}, denials=[], log_paths=[],
        )

    key_value, key_error = _read_key(spec.key_file)
    if key_error:
        return TurnResult(
            ok=False, kind="config_error", text="", session_id=spec.session_id,
            error=key_error, exit_code=None, attempts=0, events=0, tokens={},
            denials=[], log_paths=[],
        )

    log_dir = Path(spec.log_dir) if spec.log_dir else Path(tempfile.mkdtemp(prefix="trio-opencode-log-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    label_slug = _slugify(spec.label or spec.role)

    all_log_paths: list[str] = []
    decided_continue = False
    use_continue = False
    locked_session_id: str | None = None
    current_session_id = spec.session_id
    current_prompt = spec.prompt

    attempt = 0
    while True:
        attempt += 1

        if cancel is not None and cancel.is_set():
            return TurnResult(
                ok=False, kind="cancelled", text="", session_id=current_session_id,
                error="cancelled before spawn", exit_code=None, attempts=attempt - 1,
                events=0, tokens={}, denials=[], log_paths=all_log_paths,
            )

        argv = _build_argv(spec, caps, current_session_id, current_prompt)
        env = _build_env(spec, key_value)

        stdout_log_path = log_dir / f"{label_slug}-{attempt}.jsonl"
        stderr_log_path = log_dir / f"{label_slug}-{attempt}.stderr"
        all_log_paths.append(str(stdout_log_path))
        all_log_paths.append(str(stderr_log_path))

        acc = events.TurnAccumulator()

        with open(stderr_log_path, "wb") as stderr_fh:
            proc = subprocess.Popen(
                argv, env=env, cwd=spec.cwd,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr_fh,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            if on_spawn is not None:
                on_spawn(proc.pid, pgid)

            with open(stdout_log_path, "w", encoding="utf-8") as out_fh:
                override = _pump(proc, pgid, out_fh, stderr_log_path, acc, spec, key_value, cancel)

            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_process_group(pgid)
                exit_code = proc.wait(timeout=10)

        if _pgid_alive(pgid):
            kill_process_group(pgid)

        stderr_text = _scrub_file_in_place(stderr_log_path, key_value)

        if override == "cancelled":
            return TurnResult(
                ok=False, kind="cancelled", text=acc.text,
                session_id=acc.session_id or current_session_id, error="cancelled",
                exit_code=exit_code, attempts=attempt, events=acc.event_count,
                tokens=acc.tokens, denials=list(acc.permission_signals), log_paths=all_log_paths,
            )
        if override == "timeout":
            return TurnResult(
                ok=False, kind="timeout", text=acc.text,
                session_id=acc.session_id or current_session_id,
                error=f"turn exceeded turn_timeout={spec.turn_timeout}s",
                exit_code=exit_code, attempts=attempt, events=acc.event_count,
                tokens=acc.tokens, denials=list(acc.permission_signals), log_paths=all_log_paths,
            )
        if override == "idle_timeout":
            kind, error = "idle_timeout", f"no stdout for idle_timeout={spec.idle_timeout}s"
        else:
            kind, error = events.classify(acc, exit_code, stderr_text)

        bounded_retry = kind in _RETRYABLE_KINDS and attempt < spec.max_attempts
        #: README.md "Container / no-time-limit mode": an idle_timeout keeps
        #: being retried past max_attempts when idle_retry_unlimited is on --
        #: a hung connection never becomes a run-ending error on its own.
        unlimited_idle_retry = (
            kind == "idle_timeout" and spec.idle_retry_unlimited and not bounded_retry
        )
        if bounded_retry or unlimited_idle_retry:
            if unlimited_idle_retry:
                _log_idle_watchdog_retry(spec, attempt)
            if not decided_continue:
                decided_continue = True
                if spec.session_id is None and acc.session_id and len(acc.steps) >= 1:
                    use_continue = True
                    locked_session_id = acc.session_id
            if use_continue:
                current_session_id = locked_session_id
                current_prompt = _CONTINUE_PROMPT
            else:
                current_session_id = spec.session_id
                current_prompt = spec.prompt

            backoff = spec.backoff
            wait_s = backoff[attempt - 1] if backoff and attempt - 1 < len(backoff) else (backoff[-1] if backoff else 0.0)
            if cancel is not None:
                if cancel.wait(wait_s):
                    return TurnResult(
                        ok=False, kind="cancelled", text=acc.text,
                        session_id=acc.session_id or current_session_id, error="cancelled",
                        exit_code=exit_code, attempts=attempt, events=acc.event_count,
                        tokens=acc.tokens, denials=list(acc.permission_signals), log_paths=all_log_paths,
                    )
            else:
                time.sleep(wait_s)
            continue

        return TurnResult(
            ok=(kind == "ok"), kind=kind, text=acc.text,
            session_id=acc.session_id or current_session_id, error=error,
            exit_code=exit_code, attempts=attempt, events=acc.event_count,
            tokens=acc.tokens, denials=list(acc.permission_signals), log_paths=all_log_paths,
        )

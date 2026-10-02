"""Parser/classifier for OpenCode's ``run --format json`` NDJSON event
stream — v2.0.20 (primary target) and v1.18.33 (SPEC.md "OpenCode v2.0.20"
section, which supersedes the v1 facts where they differ).

Every stdout line of a live ``opencode run`` is one JSON object
``{"type": T, "timestamp": ms, "sessionID": "ses_...", ...}``. This module is
tolerant of unknown ``type`` values, of non-JSON lines, of v1/v2 field-name
differences (``sessionID``/``sessionId``/``session_id``, ``error.name``
vs ``error.type``, nested vs flat token counts) and of v2 emitting no
``step_start``/``step_finish`` events at all (a pure ``text``-only stream) —
none of this may ever raise; a corrupted or surprising line is simply
ignored.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------


def parse_line(line: str) -> dict | None:
    """Parse one NDJSON line into its event dict, or ``None`` if the line is
    blank, not valid JSON, or not a JSON object. Never raises."""
    if line is None:
        return None
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------

_ZERO_TOKENS = ("input", "output", "reasoning", "cache_read", "cache_write")

#: Event ``type`` values (outer or ``part.type``) this parser recognises and
#: handles explicitly. Anything else is tolerated, ignored, and counted in
#: ``TurnAccumulator.unknown_types``. ``reasoning`` is a real event opencode
#: emits that carries no information this driver needs; it is "known" so a
#: reasoning-only stream is not misreported as "unrecognised".
_KNOWN_TYPES = {
    "step_start", "step-start", "step_finish", "step-finish",
    "text", "error", "tool_use", "tool", "reasoning",
}


@dataclass
class _Step:
    reason: str | None = None
    #: True when this step was opened by an explicit ``step_start`` event
    #: (as opposed to being created implicitly by :meth:`TurnAccumulator.
    #: _ensure_step` for a ``text``/``step_finish`` event with no preceding
    #: ``step_start`` — v2's text-only streams never emit step events at
    #: all, and such implicit steps never count toward truncation).
    explicit: bool = False
    #: Count of ``tool_use``/``tool`` events recorded while this step was
    #: the current one — used (alongside ``texts``) to tell an explicit
    #: step that is still genuinely *empty* (nothing happened in it before
    #: the stream ended — the real truncation signal) from one that simply
    #: never got an explicit ``step_finish`` despite having done real work
    #: (a known, legitimate v2 shape: see ``TurnAccumulator.truncated``).
    tool_count: int = 0
    #: Text content, in first-seen order. A text part with an ``id`` (v2 may
    #: re-emit the same id with growing text as it streams) occupies one
    #: slot that later updates *replace in place*; a text part with no id
    #: (v1, or a scenario that wants two independent chunks) always appends
    #: a new slot — this keeps v1's plain concatenation behaviour intact.
    _slots: list[str] = field(default_factory=list)
    _slot_by_id: dict[str, int] = field(default_factory=dict)

    def add_text(self, text: str, part_id: str | None) -> None:
        if part_id:
            idx = self._slot_by_id.get(part_id)
            if idx is not None:
                self._slots[idx] = text
                return
            self._slot_by_id[part_id] = len(self._slots)
        self._slots.append(text)

    @property
    def texts(self) -> list[str]:
        return self._slots


class TurnAccumulator:
    """Fed one parsed event at a time via :meth:`feed`; tracks everything
    :func:`classify` and the runner need out of a turn's event stream."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.event_count: int = 0
        self.steps: list[_Step] = []
        self.tokens: dict[str, int] = {k: 0 for k in _ZERO_TOKENS}
        self.last_step_finish_reason: str | None = None
        self.errors: list[dict] = []          # [{"name":..., "message":..., "raw_type":...}]
        self.tool_events: list[dict] = []      # [{"name":..., "status":..., "error":...}]
        self.permission_signals: list[str] = []
        #: Every event ``type`` seen (for the "unrecognised JSON event
        #: stream" diagnostic message) and the subset this parser did not
        #: otherwise handle (SPEC.md: "counted").
        self.event_types: set[str] = set()
        self.unknown_types: set[str] = set()
        #: Count of events whose type matched none of the categories below
        #: (the same events counted in ``unknown_types``, but as a total
        #: rather than a set of distinct type strings — classify's "genuine
        #: unsupported version" signal needs >= 3 such *events*, not just
        #: >= 3 distinct unknown type strings).
        self.unknown_event_count: int = 0
        self._current_step: _Step | None = None
        #: The most recently ``step_start``-opened step, while it still has
        #: no matching ``step_finish`` — ``None`` once that step closes (or
        #: before any explicit step_start has ever been seen). Non-``None``
        #: at the end of the stream means a step was opened and never
        #: finished: see :attr:`truncated`.
        self._open_explicit_step: _Step | None = None

    def _ensure_step(self) -> _Step:
        if self._current_step is None:
            self._current_step = _Step()
            self.steps.append(self._current_step)
        return self._current_step

    def feed(self, event: dict | None) -> None:
        if not isinstance(event, dict):
            return
        self.event_count += 1
        sid = event.get("sessionID") or event.get("sessionId") or event.get("session_id")
        if sid:
            self.session_id = sid

        etype = event.get("type")
        self.event_types.add(str(etype))
        part = event.get("part")
        if not isinstance(part, dict):
            part = {}
        part_type = part.get("type")

        is_step_start = etype in ("step_start", "step-start") or part_type == "step-start"
        is_step_finish = etype in ("step_finish", "step-finish") or part_type == "step-finish"
        is_text = etype == "text" or (part_type == "text" and etype != "error")
        is_tool = etype in ("tool_use", "tool") or part_type == "tool"

        if is_step_start:
            self._current_step = _Step(explicit=True)
            self.steps.append(self._current_step)
            self._open_explicit_step = self._current_step
        elif is_text:
            text = part.get("text")
            if text:
                self._ensure_step().add_text(text, part.get("id"))
        elif is_step_finish:
            reason = part.get("reason")
            self.last_step_finish_reason = reason
            step = self._ensure_step()
            step.reason = reason
            tok = part.get("tokens")
            if isinstance(tok, dict):
                self.tokens["input"] += int(tok.get("input") or 0)
                self.tokens["output"] += int(tok.get("output") or 0)
                self.tokens["reasoning"] += int(tok.get("reasoning") or 0)
                cache = tok.get("cache")
                if isinstance(cache, dict):
                    self.tokens["cache_read"] += int(cache.get("read") or 0)
                    self.tokens["cache_write"] += int(cache.get("write") or 0)
                else:
                    self.tokens["cache_read"] += int(tok.get("cache_read") or 0)
                    self.tokens["cache_write"] += int(tok.get("cache_write") or 0)
            # A step_finish closes the current step; a later text/tool event
            # with no intervening step_start opens an implicit new one.
            if step is self._open_explicit_step:
                self._open_explicit_step = None
            self._current_step = None
        elif etype == "error":
            err = event.get("error")
            if not isinstance(err, dict):
                err = {}
            raw_type = err.get("type")
            name = err.get("name") or raw_type
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            message = data.get("message") or err.get("message") or ""
            # v1's ``APIError`` carries ``statusCode``/``isRetryable`` nested
            # under ``error.data``; tolerate a flatter v2-style shape (the
            # fields directly on ``error``) too, since this driver otherwise
            # never relies on ``error.data`` existing at all.
            status_code = data.get("statusCode", err.get("statusCode"))
            is_retryable = data.get("isRetryable", err.get("isRetryable"))
            self.errors.append({
                "name": name, "message": message, "raw_type": raw_type,
                "status_code": status_code, "is_retryable": is_retryable,
            })
        elif is_tool:
            state = part.get("state")
            if not isinstance(state, dict):
                state = {}
            self.tool_events.append({
                "name": part.get("tool"),
                "status": state.get("status"),
                "error": state.get("error"),
            })
            if self._current_step is not None:
                self._current_step.tool_count += 1
        elif etype and "permission" in etype:
            self.permission_signals.append(json.dumps(event, default=str)[:500])
        else:
            self.unknown_types.add(str(etype))
            self.unknown_event_count += 1

    @property
    def text(self) -> str:
        """Final assistant text: text parts of the last step that produced
        any text, concatenated in order (a v2 text-only stream with no
        step_start/step_finish at all collapses to a single implicit
        step, so this is simply every text part, in order, for that
        common case)."""
        for step in reversed(self.steps):
            if step.texts:
                # Each text event is a complete part (the CLI emits finished
                # parts, not deltas): separate distinct parts by a newline so
                # line-anchored markers (``DENIED:``, fences) stay anchored.
                out = ""
                for t in step.texts:
                    if out and not out.endswith("\n") and t and not t.startswith("\n"):
                        out += "\n"
                    out += t
                return out
        return ""

    @property
    def recognized(self) -> bool:
        """True once the stream produced at least one of: text content, a
        step_finish, a tool event, or an error — i.e. anything the runner
        can actually act on. False with ``event_count > 0`` means every
        event was of a type this parser could not use at all (SPEC.md: "no
        text/step/error/tool")."""
        return bool(self.text.strip()) or self.last_step_finish_reason is not None \
            or bool(self.tool_events) or bool(self.errors)

    @property
    def truncated(self) -> bool:
        """True when an explicit ``step_start`` was seen, the most recently
        opened explicit step has no matching ``step_finish`` yet, AND that
        open step is genuinely empty (no text, no tool event ever landed in
        it) — the stream ended right as a step began, mid-generation, with
        no progress at all (the real incident: a lone ``step_start`` then
        end-of-stream). A still-open step that already produced text or ran
        a tool is a known, legitimate v2 shape (a turn that simply never
        emits a closing ``step_finish``) and is NOT truncated — nor is a v2
        stream that never emits any step events at all (pure ``text``)."""
        step = self._open_explicit_step
        if step is None:
            return False
        return not step.texts and step.tool_count == 0


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_PERMISSION_RE = re.compile(r"permission requested:|auto-rejecting", re.IGNORECASE)

#: ``--print-logs --log-level info`` echoes every INFO-level action the CLI
#: takes, including a line per spawned subprocess that carries the model's
#: own shell command/args verbatim (``message="spawning process" ...
#: args=[...]``). That line is never itself a permission prompt or a
#: provider error — only tool args that happen to *mention* those words —
#: so it is dropped before either scan looks at stderr.
_SPAWN_PROCESS_RE = re.compile(r'message="spawning process"')

#: A structured ``key=value`` log line at INFO/DEBUG level (v2: ``level=INFO
#: ...``) or a v1-style ``INFO ``/``DEBUG `` line-prefix log line. WARN/ERROR
#: lines, and any non-log plain-text line (the real CLI warnings this
#: classifier depends on, e.g. "permission requested: ...; auto-rejecting"
#: or a bare "Session not found"), are never diagnostic-log lines and always
#: stay in scope.
_LOG_LEVEL_KV_RE = re.compile(r"(?:^|\s)level=(?:INFO|DEBUG)\b", re.IGNORECASE)
_LOG_LEVEL_V1_PREFIX_RE = re.compile(r"^\s*(?:INFO|DEBUG)\s", re.IGNORECASE)


def _is_structured_info_debug_line(line: str) -> bool:
    return bool(_LOG_LEVEL_KV_RE.search(line) or _LOG_LEVEL_V1_PREFIX_RE.match(line))


def strip_spawn_process_lines(text: str) -> str:
    """Drop stderr lines that echo a spawned subprocess's own argv (which
    may contain anything the model's tool calls happen to pass, including
    the literal words a permission-prompt regex looks for). Used for both
    permission scans (:func:`classify`'s ``_PERMISSION_RE`` and
    ``runner._pump``'s incremental stderr check) — every other stderr line,
    including the real auto-rejecting warning, stays in scope."""
    return "\n".join(
        line for line in (text or "").splitlines() if not _SPAWN_PROCESS_RE.search(line)
    )


def _diagnostic_text(text: str) -> str:
    """``stderr_text`` with structured INFO/DEBUG log lines dropped (WARN/
    ERROR lines and non-log lines stay) — the text the free-text transient
    scan and the "Session not found" scan run against, so a completed
    turn's own tool args (echoed at INFO level by ``--print-logs``) can
    never masquerade as a provider error/timeout."""
    return "\n".join(
        line for line in (text or "").splitlines() if not _is_structured_info_debug_line(line)
    )


_CONFIG_ERROR_NAMES = {
    "ProviderAuthError",
    "ProviderModelNotFoundError",
    "ProviderNotFoundError",
    "ProviderNoModelsError",
    "ModelNotFoundError",
    "SessionNotFoundError",
}

_TRANSIENT_NAMES = {"UnknownError", "SessionBusyError"}

_MODEL_ERROR_NAMES = {"MessageOutputLengthError", "StructuredOutputError", "MessageAbortedError"}

#: v1's provider ``APIError`` carries a structured ``statusCode``/
#: ``isRetryable`` (recorded onto the error dict by ``TurnAccumulator.feed``)
#: that is a far more reliable signal than any free-text message scan.
_API_ERROR_TRANSIENT_STATUS = {408, 409, 425, 429}
_API_ERROR_CONFIG_STATUS = {401, 403}


def _api_error_transient(e: dict) -> bool:
    if (e.get("name") or "") != "APIError":
        return False
    if e.get("is_retryable") is True:
        return True
    status = e.get("status_code")
    return isinstance(status, int) and (status in _API_ERROR_TRANSIENT_STATUS or 500 <= status <= 599)


def _api_error_config(e: dict) -> bool:
    if (e.get("name") or "") != "APIError":
        return False
    return e.get("status_code") in _API_ERROR_CONFIG_STATUS


#: v2 ``error.type`` is a dotted string (e.g. ``provider.no-route``); match
#: against that field only (never against the free-text message), which is
#: what keeps a message like "Model unavailable: x/y" on a
#: ``provider.no-route`` error safely classified as config_error instead of
#: being caught by the transient "*unavailable*" pattern below.
_V2_CONFIG_TYPE_RE = re.compile(r"no[-_]route|auth|not[-_]found|model[-_]unavailable", re.IGNORECASE)
_V2_TRANSIENT_TYPE_RE = re.compile(r"rate|limit|timeout|server|overload|network|unavailable", re.IGNORECASE)

_TRANSIENT_TEXT_RE = re.compile(
    r"upstream service timeout"
    r"|stream error"
    r"|server_error"
    r"|too many requests"
    r"|\b429\b"
    r"|rate limit"
    r"|overloaded"
    r"|\b5\d{2}\b"
    r"|ECONNRESET"
    r"|ECONNREFUSED"
    r"|ETIMEDOUT"
    r"|socket hang up"
    r"|fetch failed"
    r"|\bnetwork\b"
    r"|timed out"
    r"|TimeoutError"
    r"|Transport error",
    re.IGNORECASE,
)


def _first_matching_line(text: str, pattern: re.Pattern) -> str | None:
    for line in (text or "").splitlines():
        if pattern.search(line):
            return line
    return None


def classify(acc: TurnAccumulator, exit_code: int | None, stderr_text: str) -> tuple[str, str | None]:
    """Classify a completed (or forcibly-ended) turn into one of:
    ``ok``, ``config_error``, ``transient``, ``model_error``, ``permission``.

    ``timeout``/``idle_timeout``/``cancelled`` are decided by the runner
    itself (they are about wall-clock/idle behaviour the accumulator cannot
    see) and are never returned here.
    """
    stderr_text = stderr_text or ""

    # 1. Permission: a non-interactive "ask" rule auto-rejects a tool call
    #    and the CLI prints a fixed warning line; never let the turn hang.
    #    Spawned-subprocess log lines are dropped first — they echo the
    #    model's own tool argv verbatim and must never be mistaken for the
    #    CLI's own permission warning.
    perm_stderr = strip_spawn_process_lines(stderr_text)
    perm_line = _first_matching_line(perm_stderr, _PERMISSION_RE)
    if perm_line or acc.permission_signals:
        detail = (perm_line or acc.permission_signals[0]).strip()
        return "permission", f"permission prompt in non-interactive run: {detail}"

    # Diagnostic-only stderr (structured INFO/DEBUG log lines dropped; WARN/
    # ERROR and non-log lines stay) backs every free-text scan below, so a
    # completed turn's own tool args echoed at INFO level by --print-logs
    # (e.g. a shell command containing "timeout 540"/"network"/"503") can
    # never be mistaken for a provider error.
    diag_text = _diagnostic_text(stderr_text)
    errors = acc.errors
    error_messages = " ".join(e.get("message") or "" for e in errors)
    scan_text = f"{diag_text}\n{error_messages}"

    # 2. config_error: v1 literal error names, v1 APIError with a 401/403
    #    statusCode, or v2's dotted error.type (provider.no-route / *auth* /
    #    *not-found* / model-unavailable).
    for e in errors:
        name = e.get("name") or ""
        if name in _CONFIG_ERROR_NAMES or _api_error_config(e):
            return "config_error", f"{name}: {e.get('message') or ''}".strip()
        if _V2_CONFIG_TYPE_RE.search(name):
            return "config_error", f"{name}: {e.get('message') or ''}".strip()
    if "Session not found" in scan_text or "SessionNotFoundError" in scan_text:
        return "config_error", "Session not found"

    # 3. transient: v1 literal names, v1 APIError with isRetryable/a
    #    retryable statusCode, v2 dotted-type patterns (named/structured
    #    signals only — never free text yet, so a v2 provider.no-route
    #    error's "Model unavailable" message can't leak in here via the
    #    scan below).
    for e in errors:
        name = e.get("name") or ""
        if name in _TRANSIENT_NAMES or _api_error_transient(e):
            return "transient", f"{name}: {e.get('message') or ''}".strip()
        if _V2_TRANSIENT_TYPE_RE.search(name):
            return "transient", f"{name}: {e.get('message') or ''}".strip()

    # 4. model_error (named errors, then any other reported error — an
    #    unrecognised v2 error.type falls through to here, never silently
    #    treated as transient). Any error reaching this point means the
    #    turn is conclusively classified by it, before the weaker
    #    truncated/empty-stream heuristics below ever get a say.
    for e in errors:
        if (e.get("name") or "") in _MODEL_ERROR_NAMES:
            return "model_error", f"{e.get('name')}: {e.get('message') or ''}".strip()
    if errors:
        e = errors[-1]
        return "model_error", f"{e.get('name') or 'Error'}: {e.get('message') or ''}".strip()

    # 5. Truncated: an explicit step_start was seen and the most recently
    #    opened explicit step never got a matching step_finish — the
    #    process ended mid-step. This is never "ok", even at exit 0 and
    #    even when an earlier, already-finished step produced text: a turn
    #    that dies mid-step has not finished its work. (No error event
    #    reached this point, so this is the correct next thing to check.)
    if acc.truncated:
        types = ", ".join(sorted(acc.event_types)) or "none"
        return "transient", f"truncated event stream (last step never finished; types: {types})"

    # 6. ok — decided before any free-text stderr scan runs, so a completed
    #    turn's own tool-arg/log noise can never re-classify it. v2 may
    #    complete with only ``text`` events and no step_finish at all, so
    #    success never requires ``last_step_finish_reason``.
    if exit_code == 0 and (acc.last_step_finish_reason == "stop" or acc.text.strip()):
        return "ok", None

    # 7. transient: free-text scan of diagnostic stderr/error messages (v1's
    #    own log-scan heuristic) for a turn that did NOT complete above.
    m = _TRANSIENT_TEXT_RE.search(scan_text)
    if m:
        return "transient", m.group(0)

    # 8. Exit 0, events seen, but EVERY event was of an unrecognised type,
    #    and there were at least 3 of them: a genuine "this parser doesn't
    #    understand this CLI's event shape" signal, never silently retried.
    #    One or two unknown-type events is too weak a signal on its own
    #    (falls through to the empty-output/unclassified checks below).
    if (
        exit_code == 0
        and acc.event_count > 0
        and acc.unknown_event_count == acc.event_count
        and acc.unknown_event_count >= 3
    ):
        types = ", ".join(sorted(acc.event_types)) or "none"
        return "config_error", (
            f"unsupported opencode version: unrecognised JSON event stream (types: {types})"
        )

    # 9. Killed by signal (negative exit code, e.g. OOM SIGKILL) with no
    #    reported error and no completed turn: the process never got a
    #    chance to report anything meaningful — treat it as transient, not
    #    an unclassified model_error.
    if isinstance(exit_code, int) and exit_code < 0:
        return "transient", f"process killed by signal {-exit_code}"

    # 10. empty output (no events at all, or no text and no error) is
    #     treated as transient.
    if exit_code in (0, 1) and not acc.text.strip():
        return "transient", "empty output"

    return "model_error", f"unclassified failure (exit_code={exit_code})"


# ---------------------------------------------------------------------------
# Structured-output helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.DOTALL)


def find_fenced_json(text: str, required_keys: tuple[str, ...] = ()) -> dict | None:
    """Return the last fenced ```json / ``` / ```JSON block in ``text`` that
    parses to a JSON object containing every key in ``required_keys``, or
    ``None`` if there is no such block."""
    if not text:
        return None
    found: dict | None = None
    for block in _FENCE_RE.findall(text):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if required_keys and not all(k in obj for k in required_keys):
            continue
        found = obj
    return found


def denied_lines(text: str) -> list[str]:
    """Lines of the form ``DENIED: ...`` found in ``text``, in order."""
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip().startswith("DENIED:")]

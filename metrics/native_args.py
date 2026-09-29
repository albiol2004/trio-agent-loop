#!/usr/bin/env python3
"""native_args.py — the one validator for a native (claude-workflow) run's
recorded launch and for mailbox paths (stdlib only).

Shared, byte-identical, by trio-dash (``dashboard/loop_actions.py``: what a
preview offers) and ``native/launch.sh`` (what actually runs), so the
dashboard never previews a resume that launch.sh will refuse, and the other
way round.

Path rule (:func:`path_problem`): an absolute path of printable characters.
Spaces, non-ASCII letters, ``,``, ``~``, quotes and other printable
punctuation are allowed; control characters (NUL, newline, CR, tab, …),
line / paragraph separators (NEL, U+2028, U+2029), format characters (bidi
overrides, zero-width), unpaired surrogates and non-space separators are
refused. Such a path only ever travels as one argv element (never through a
shell), and where a driver puts it into a role prompt it goes through
:func:`prompt_path`: unchanged when it only uses ``[A-Za-z0-9._/+@-]``,
otherwise POSIX-shell single-quoted (so it is one inert token inside the
prompt's backticks and in any command the prompt spells out).

Resume schema (:func:`validate_args`): a JSON object with only ``mailbox``
(this mailbox, in canonical form: absolute, no ``.``/``..``/empty
components, no trailing slash, allowed by the path rule), ``max_iterations``
(required, int 1..200), ``max_agents`` (int 1..1000), ``token_budget`` (int
1..1e10), ``run_token`` (``[A-Za-z0-9._-]{1,64}``), ``helper`` (this
release's helper only) and ``models`` (``{role: model}``, both strings from
the allowlists). Anything else — including any unexpected type or an input
that makes a parser fail — is a :class:`NativeArgsError`, never another
exception.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import uuid

NATIVE_ARGS_API = 1
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RUN_ID_RE = re.compile(r"wf_[A-Za-z0-9_-]{1,64}")
RUN_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
ARG_KEYS = ("mailbox", "max_iterations", "max_agents", "token_budget", "helper", "run_token",
            "models")
MODEL_ROLES = ("lead", "evaluator", "builder", "repair", "step")
MODELS = ("claude-opus-5-5", "claude-sonnet-5")
CAPS = {"max_iterations": (1, 200), "max_agents": (1, 1000), "token_budget": (1, 10_000_000_000)}
CONSERVATIVE_PATH = re.compile(r"/[A-Za-z0-9._/+@-]*")
MAX_PATH = 4096
NO_TOKEN = ("the recorded launch predates run tokens; a resume cannot tell its own run "
            "from another (use start)")


class NativeArgsError(ValueError):
    """A recorded native launch field (or a path) that fails the schema."""


def path_problem(path) -> str | None:
    """Why ``path`` may not be used as a mailbox / helper path (None: ok)."""
    if not isinstance(path, str):
        return "is not a string"
    if not path.startswith("/"):
        return "is not absolute"
    if len(path) > MAX_PATH:
        return f"is longer than {MAX_PATH} characters"
    for ch in path:
        if not ch.isprintable():
            return (f"has a control, format or line-separator character (U+{ord(ch):04X}); "
                    "rename the directory")
    return None


def prompt_path(path: str) -> str:
    """``path`` as it may appear in a role prompt: unchanged when it only
    uses ``[A-Za-z0-9._/+@-]``, else shell single-quoted. ValueError for a
    path the rule refuses (it must never reach a prompt)."""
    problem = path_problem(path)
    if problem:
        raise ValueError(f"path {problem}")
    return path if CONSERVATIVE_PATH.fullmatch(path) else shlex.quote(path)


def is_canonical_path(value: str) -> bool:
    """No ``.``/``..``/empty component and no trailing slash."""
    return (value == "/" or not value.endswith("/")) and "//" not in value \
        and not any(part in (".", "..") for part in value.split("/"))


def check_mailbox(value, mailbox) -> str:
    """``value`` when it names ``mailbox`` in canonical form (path rule, no
    ``.``/``..``/empty components, same real path); else NativeArgsError."""
    problem = path_problem(value)
    if problem:
        raise NativeArgsError(f"args.mailbox {problem}")
    if not is_canonical_path(value):
        raise NativeArgsError("args.mailbox is not canonical (a '.', '..' or empty component, "
                              "or a trailing slash)")
    if os.path.realpath(value) != os.path.realpath(str(mailbox)):
        raise NativeArgsError("args.mailbox is not this mailbox")
    return value


def canonical_session_id(value) -> str:
    """A recorded Claude session id, only as a canonical lowercase UUID."""
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise NativeArgsError("session_id is not a canonical UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise NativeArgsError("session_id is not a canonical UUID")
    except ValueError:
        raise NativeArgsError("session_id is not a canonical UUID") from None
    return value


def check_run_id(value) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise NativeArgsError("the run id is not wf_[A-Za-z0-9_-]{1,64}")
    return value


def parse_args(raw):
    """A recorded ``args`` value (a JSON string or an already-parsed value)
    as a dict; NativeArgsError for anything else."""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError, TypeError):
            raise NativeArgsError("args is not JSON") from None
    if not isinstance(raw, dict):
        raise NativeArgsError("args is not a JSON object")
    return raw


def _validate(raw, mailbox, helper, require_run_token: bool) -> dict:
    args = parse_args(raw)
    unknown = [k for k in args if k not in ARG_KEYS]
    if unknown:
        raise NativeArgsError("args has unknown keys: "
                              + ", ".join(sorted(repr(k)[:40] for k in unknown))[:200])
    out: dict = {}
    for key, value in args.items():
        if key == "mailbox":
            check_mailbox(value, mailbox)
        elif key in CAPS:
            lo, hi = CAPS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise NativeArgsError(f"args.{key} must be an integer {lo}..{hi}")
        elif key == "run_token":
            if not isinstance(value, str) or not RUN_TOKEN_RE.fullmatch(value):
                raise NativeArgsError("args.run_token is not [A-Za-z0-9._-]{1,64}")
        elif key == "helper":
            if helper is None or path_problem(value) \
                    or os.path.realpath(value) != os.path.realpath(str(helper)):
                raise NativeArgsError("args.helper is not the installed release's helper")
        elif key == "models":
            if not isinstance(value, dict) or not all(
                    isinstance(r, str) and r in MODEL_ROLES
                    and isinstance(m, str) and m in MODELS for r, m in value.items()):
                raise NativeArgsError("args.models names a role or model outside the allowlist "
                                      "(" + ", ".join(MODELS) + ")")
            value = dict(value)
        out[key] = value
    if "mailbox" not in out:
        raise NativeArgsError("args.mailbox is missing")
    if "max_iterations" not in out:
        raise NativeArgsError("args.max_iterations is missing")
    if require_run_token and "run_token" not in out:
        raise NativeArgsError(NO_TOKEN)
    return out


def validate_args(raw, *, mailbox, helper, require_run_token: bool = True) -> dict:
    """The recorded workflow args as a validated dict (key order kept), or
    NativeArgsError — never any other exception (a guard turns an
    unexpected one into a refusal)."""
    try:
        return _validate(raw, mailbox, helper, require_run_token)
    except NativeArgsError:
        raise
    except Exception as exc:  # noqa: BLE001 - every bad input is a clean refusal
        raise NativeArgsError(f"args failed validation ({type(exc).__name__})") from None


def resume_args_json(args: dict) -> str:
    """The canonical one-line JSON the workflow is resumed with (rebuilt
    from validated fields; ASCII-only, so no line separator can occur)."""
    return json.dumps(args, separators=(",", ":"), ensure_ascii=True)

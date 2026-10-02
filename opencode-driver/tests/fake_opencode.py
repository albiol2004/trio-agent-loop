#!/usr/bin/env python3
"""Fake ``opencode`` executable for trio-opencode's runner tests.

Emulates OpenCode v2.0.20 (the primary target) by default:
``run --standalone --format json --print-logs --log-level info --agent A
-m provider/model[#variant] [-s session] PROMPT`` and ``models
[--standalone]``, plus ``run --help``/``--version`` for
``trio_opencode.runner.detect_cli``. Set ``FAKE_OC_STYLE=v1`` to switch the
whole surface (CLI flags AND default event shapes) to the legacy 1.18.33
form (``--dir``/``--variant``, uppercase ``--log-level``, step_start/
step_finish wrapping every reply) so the v1 compatibility path can still be
exercised end to end.

Test behaviour for one invocation is supplied by a *scenario* module (path
in ``FAKE_OC_SCENARIO``) exposing ``handle(ctx)``; with no scenario set, the
default behaviour is a single reply of "OK". State (session ids, per-agent
call counters) persists across invocations in ``$FAKE_OC_STATE`` so a
runner's retry attempts see a coherent fake provider.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import textwrap
import time
import uuid


def _now_ms() -> int:
    return int(time.time() * 1000)


def _eprint(line: str) -> None:
    print(line, file=sys.stderr)
    sys.stderr.flush()


def _style() -> str:
    return "v1" if os.environ.get("FAKE_OC_STYLE") == "v1" else "v2"


def _event_style() -> str:
    return os.environ.get("FAKE_OC_EVENT_STYLE") or _style()


# ---------------------------------------------------------------------------
# Persisted state ($FAKE_OC_STATE/state.json, $FAKE_OC_STATE/calls.jsonl)
# ---------------------------------------------------------------------------


def _state_paths():
    state_dir = os.environ["FAKE_OC_STATE"]
    os.makedirs(state_dir, exist_ok=True)
    return state_dir, os.path.join(state_dir, "state.json"), os.path.join(state_dir, "calls.jsonl")


def _with_state(fn):
    state_dir, state_path, _ = _state_paths()
    lock_path = os.path.join(state_dir, ".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            if os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as fh:
                    state = json.load(fh)
            else:
                state = {"sessions": {}, "counters": {}}
            result = fn(state)
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            return result
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _append_call(record: dict) -> None:
    _, _, calls_path = _state_paths()
    with open(calls_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# `run --help` / `--version`
# ---------------------------------------------------------------------------

_V2_HELP = textwrap.dedent("""\
    Usage: opencode run [flags] [<prompt>]

    Flags:
      --standalone              run a private standalone server for this turn
      --server                  attach to an already-running server
      -c, --continue            continue the most recent session
      -s, --session <id>        continue a specific session
      --fork                    fork the given session
      -m, --model <provider/model#variant>
      --agent <name>            agent to run
      --format <default|json>   output format
      -f, --file <path>         read the prompt from a file
      --title <title>           session title
      --thinking                enable extended thinking
      --auto                    DO NOT USE in non-interactive runs
      --log-level <level>       info|debug|warn|error
      --print-logs              print logs to stderr
""")

_V1_HELP = textwrap.dedent("""\
    Usage: opencode run [flags] <prompt>

    Flags:
      --dir <path>              working directory
      --variant <variant>       model variant
      --session <id>            continue a session
      --model <provider/model>  model to use
      --agent <name>            agent to run
      --format <json|text>      output format
      --print-logs              print logs to stderr
      --log-level <LEVEL>       INFO|DEBUG|WARN|ERROR
      --auto                    DO NOT USE in non-interactive runs
""")


def _help_text() -> str:
    override = os.environ.get("FAKE_OC_HELP")
    if override is not None:
        return override
    return _V1_HELP if _style() == "v1" else _V2_HELP


_FAKE_VERSION = os.environ.get(
    "FAKE_OC_VERSION", "1.18.33" if _style() == "v1" else "2.0.20",
)


def _cmd_version() -> int:
    print(_FAKE_VERSION)
    return 0


# ---------------------------------------------------------------------------
# argv parsing
# ---------------------------------------------------------------------------


_FLAGS_WITH_VALUE = {
    "--agent": "agent", "--model": "model", "-m": "model", "--dir": "dir",
    "--variant": "variant", "--session": "session", "-s": "session",
    "--format": "format", "--log-level": "log_level",
}
_BOOL_FLAGS = {
    "--print-logs": "print_logs", "--auto": "auto", "--yolo": "yolo",
    "--standalone": "standalone",
}


def _parse_run_args(args: list[str]) -> dict:
    ns = {v: None for v in set(_FLAGS_WITH_VALUE.values())}
    for v in _BOOL_FLAGS.values():
        ns[v] = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _FLAGS_WITH_VALUE:
            ns[_FLAGS_WITH_VALUE[a]] = args[i + 1] if i + 1 < len(args) else None
            i += 2
        elif a in _BOOL_FLAGS:
            ns[_BOOL_FLAGS[a]] = True
            i += 1
        else:
            positional.append(a)
            i += 1
    ns["prompt"] = positional[-1] if positional else ""
    model = ns.get("model")
    if model and "#" in model:
        base, _, variant = model.partition("#")
        ns["model"] = base
        ns["variant"] = ns.get("variant") or variant
    return ns


# ---------------------------------------------------------------------------
# Context passed to scenario handle()
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, *, agent, model, dir, prompt, session, sid, n, total, env, style):
        self.agent = agent
        self.model = model
        self.dir = dir
        self.prompt = prompt
        self.session = session
        self.n = n
        self.total = total
        self.env = env
        self.style = style
        self._sid = sid
        self._step_open = False
        self._finished = False
        self._tool_seq = 0
        self._text_seq = 0

    def _emit(self, obj: dict) -> None:
        print(json.dumps(obj))
        sys.stdout.flush()

    def _ensure_step(self) -> None:
        if not self._step_open:
            self._emit({
                "type": "step_start", "timestamp": _now_ms(), "sessionID": self._sid,
                "part": {"type": "step-start", "time": {"start": _now_ms()}},
            })
            self._step_open = True

    def _next_text_id(self) -> str:
        self._text_seq += 1
        return f"prt_{self._sid[-8:]}_text-{self._text_seq - 1}"

    def text(self, s: str, part_id: str | None = None) -> None:
        """Emit one text chunk. v1 (or FAKE_OC_EVENT_STYLE=v1) always wraps
        it in a step_start/current-step, matching the real 1.18.33 shape.
        v2's default event style emits a bare ``text`` event with no step
        wrapping at all (a real deepseek v2 turn may emit ONLY text events);
        pass the SAME ``part_id`` twice to simulate v2 re-emitting one part
        with growing text (the id-keyed "keep the latest" case) — a fresh
        call with no ``part_id`` always gets its own new id, so unrelated
        chunks (e.g. a note followed by a fenced JSON block) still
        concatenate exactly as before."""
        if self.style == "v1":
            self._ensure_step()
            self._emit({
                "type": "text", "timestamp": _now_ms(), "sessionID": self._sid,
                "part": {"type": "text", "text": s, "time": {"start": _now_ms(), "end": _now_ms()}},
            })
            return
        pid = part_id or self._next_text_id()
        self._emit({
            "type": "text", "timestamp": _now_ms(), "sessionID": self._sid,
            "part": {"type": "text", "id": pid, "text": s,
                     "time": {"start": _now_ms(), "end": _now_ms()}},
        })

    def tool(self, name: str, status: str = "completed", input=None, output: str = "") -> None:
        self._ensure_step()
        self._tool_seq += 1
        state = {"status": status, "input": input or {}, "title": name,
                 "time": {"start": _now_ms(), "end": _now_ms()}}
        if status == "error":
            state["error"] = output
        else:
            state["output"] = output
        self._emit({
            "type": "tool_use", "timestamp": _now_ms(), "sessionID": self._sid,
            "part": {"type": "tool", "tool": name, "callID": f"call_{self._tool_seq}", "state": state},
        })

    def step(self, reason: str, tokens: dict | None = None) -> None:
        self._ensure_step()
        tok = tokens or {"total": 10, "input": 6, "output": 4, "reasoning": 0,
                          "cache": {"write": 0, "read": 0}}
        self._emit({
            "type": "step_finish", "timestamp": _now_ms(), "sessionID": self._sid,
            "part": {"type": "step-finish", "reason": reason, "tokens": tok, "cost": 0.0},
        })
        self._step_open = False
        self._finished = True

    def error(self, name: str, message: str, **data) -> None:
        if self.style == "v1":
            payload = {"message": message}
            payload.update(data)
            self._emit({
                "type": "error", "timestamp": _now_ms(), "sessionID": self._sid,
                "error": {"name": name, "data": payload},
            })
        else:
            err = {"type": name, "message": message}
            err.update(data)
            self._emit({
                "type": "error", "timestamp": _now_ms(), "sessionID": self._sid,
                "error": err,
            })
        sys.stdout.flush()
        sys.exit(1)

    def stderr(self, line: str) -> None:
        _eprint(line)

    def permission_ask(self, perm: str, patterns: str) -> None:
        self.stderr(f"permission requested: {perm} ({patterns}); auto-rejecting")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def sh(self, cmd: str):
        return subprocess.Popen(cmd, shell=True, cwd=self.dir)

    def exit(self, code: int) -> None:
        sys.stdout.flush()
        sys.exit(code)


def _default_handle(ctx: Ctx) -> None:
    ctx.text("OK")


def _load_scenario():
    path = os.environ.get("FAKE_OC_SCENARIO")
    if not path:
        return _default_handle
    spec = importlib.util.spec_from_file_location("trio_opencode_fake_scenario", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.handle


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_models(args: list[str]) -> int:
    # v2 calls this with `--standalone`; v1 never does — either way only a
    # leading provider positional (never a flag) narrows the listing.
    provider = args[0] if args and not args[0].startswith("-") else None
    models_env = os.environ.get("FAKE_OC_MODELS")
    if models_env:
        lines = [l for l in models_env.splitlines() if l.strip()]
    else:
        lines = ["opencode/big-pickle"]
        if os.environ.get("OPENCODE_API_KEY"):
            lines += ["opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"]
    if provider:
        lines = [l for l in lines if l.split("/")[0] == provider]
    for line in lines:
        print(line)
    return 0


def _stdin_is_devnull_like() -> bool:
    try:
        st = os.fstat(0)
    except OSError:
        return True  # closed fd: as good as DEVNULL for our purposes
    return stat.S_ISCHR(st.st_mode)


def _cmd_run(args: list[str]) -> int:
    if "--help" in args:
        print(_help_text())
        return 0

    style = _style()

    if style == "v2" and "--dir" in args:
        _eprint("fake: unknown flag: --dir")
        return 2
    if style == "v2" and "--standalone" not in args:
        _eprint("fake: --standalone is required (v2 mode)")
        return 2

    ns = _parse_run_args(args)

    if ns["auto"] or ns["yolo"]:
        _eprint("fake: --auto/--yolo are forbidden in non-interactive trio-opencode runs")
        return 2

    stdin_ok = _stdin_is_devnull_like()

    key = os.environ.get("OPENCODE_API_KEY") or ""
    key_sha8 = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8] if key else None

    requested_session = ns["session"]

    def _resolve(state: dict):
        counters = state.setdefault("counters", {})
        sessions = state.setdefault("sessions", {})
        agent = ns["agent"] or "unknown"
        if requested_session:
            if requested_session not in sessions:
                if style == "v2":
                    # v2 accepts a bogus/unknown --session (it just creates
                    # one) — never the v1 "Session not found" failure.
                    sessions[requested_session] = {"agent": agent}
                else:
                    return None  # unknown session (v1 failure)
            sid = requested_session
        else:
            sid = "ses_" + uuid.uuid4().hex[:12]
            sessions[sid] = {"agent": agent}
        counters[agent] = counters.get(agent, 0) + 1
        n = counters[agent]
        total = sum(counters.values())
        return sid, n, total

    resolved = _with_state(_resolve)

    # v1 has a real --dir flag; v2 has none — it derives its working
    # directory from the inherited $PWD environment variable, NOT from the
    # process's actual working directory (live-verified by the
    # coordinator). Honoring $PWD here (falling back to the process cwd
    # only when PWD is unset, matching plausible real behaviour) is what
    # makes this fake actually reproduce the bug a caller that leaves PWD
    # inherited would hit against the real v2 binary.
    if style == "v1":
        effective_dir = ns["dir"] or os.getcwd()
    else:
        effective_dir = os.environ.get("PWD") or os.getcwd()

    _append_call({
        "argv": ["run"] + [a for a in args if a != ns["prompt"]],
        "prompt": ns["prompt"],
        "cwd": effective_dir,
        "agent": ns["agent"],
        "model": ns["model"],
        "variant": ns["variant"],
        "session": requested_session,
        "has_key": bool(key),
        "key_sha8": key_sha8,
        "pid": os.getpid(),
        "stdin_ok": stdin_ok,
        "resolved_session": resolved[0] if resolved else None,
        "style": style,
    })

    if resolved is None:
        _eprint("Error: Session not found")
        return 1

    sid, n, total = resolved

    if not stdin_ok:
        _eprint("fake: stdin is not DEVNULL")
        while True:
            time.sleep(3600)

    ctx = Ctx(agent=ns["agent"], model=ns["model"], dir=effective_dir, prompt=ns["prompt"],
              session=requested_session, sid=sid, n=n, total=total, env=dict(os.environ),
              style=_event_style())

    handle = _load_scenario()
    handle(ctx)  # may sys.exit() itself (ctx.exit / ctx.error)

    if not ctx._finished and ctx.style == "v1":
        # v2's default completion needs no step_finish at all (a real
        # deepseek v2 turn may emit only `text` events); v1 always closes
        # with a stop step, so keep auto-closing it here.
        ctx.step("stop")
    sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    if "--version" in argv or argv == ["-v"]:
        return _cmd_version()
    if argv and argv[0] == "models":
        return _cmd_models(argv[1:])
    if argv and argv[0] == "run":
        return _cmd_run(argv[1:])
    _eprint(f"fake: unsupported invocation: {argv}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

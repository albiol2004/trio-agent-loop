#!/usr/bin/env bash
# trio-native lab launcher: one headless Claude Code session that only
# launches the saved `trio-native` workflow and returns its result JSON.
#
#   launch.sh start  --mailbox /abs/repo/loop [--max-iterations N] [--helper /abs/trio_native_step.py]
#                    [--run-token T] [--timeout SECONDS]
#   launch.sh resume --mailbox /abs/repo/loop --run-id wf_… [--session UUID] [--timeout SECONDS]
#
# start   records a fresh --session-id and the exact args JSON (including
#         --run-token, generated when not given) in <mailbox>/.native-launch.json,
#         then runs the workflow. A start that never actually began the
#         workflow (lock refused, unparseable output, no `claude`) restores
#         the previous record.
# resume  (only after a kill or crash mid-run) reopens the SAME session with
#         --resume and asks for resumeFromRunId with the byte-identical args:
#         workflow journals live under the launching session. After a
#         held / error / budget result, use `start` again (a journal resume
#         would replay the same stop).
#
# Flags: --permission-mode auto (auto mode; no permission-skipping flag is
# used or needed), --settings '{"worktree":{"baseRef":"head"}}' (isolated
# builders fork from the Lead's HEAD, not origin/HEAD; command-line only,
# no settings file is edited), --model claude-opus-5-5, --output-format json.
# Run from the product repo (the cwd is the checkout builders fork from);
# the default cwd is the git toplevel of the mailbox.
#
# Output: the workflow's result JSON (parsed from the one fenced block the
# session is told to print) on stdout; the raw session output is kept under
# <mailbox>/.native-runs/. Exit 0 when a result was parsed, 3 when not,
# 2 on usage errors. Default --timeout: 6 h (vps-pool runs about 4 h).
#
# Dashboard records (native-dash): only for a run that actually started
# (its --run-token reached <mailbox>/.session.json via the workflow's
# `begin`), the same result (or the error) is written to
# <mailbox>/.native-result.json with the session id, the workflow run id
# (wf_…, found under $CLAUDE_CONFIG_DIR/projects/*/<session>/) and
# api_equiv_usd, and the run is registered in
# ${TRIO_NATIVE_RUNS_DIR:-~/.local/share/trio-agent-loop/native-runs}/<key>.json
# (key = first 16 hex of sha256(realpath mailbox)) so trio-dash finds it. A
# run that never started writes neither file; its outcome instead goes to
# <mailbox>/.native-runs/<session>.result.json. See README.md "Run registry
# and result".
set -euo pipefail

CLAUDE_BIN="${TRIO_NATIVE_CLAUDE:-claude}"
# Roles run pytest in the checkout and in builder worktrees: no .pyc files
# (probe 2 blocker A; `begin` also excludes the artefacts in info/exclude).
export PYTHONDONTWRITEBYTECODE=1
# Headless `claude -p` (2.1.280) terminates background tasks, and the
# Workflow is one, 600 s after the launching turn ends unless this is 0
# (probe 3 blocker E). Environment only: no settings file is involved.
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0
MODEL="claude-opus-5-5"
SETTINGS='{"worktree":{"baseRef":"head"}}'
SUFFIX='Launch only; do not edit files, settings or permissions; output the result JSON verbatim in one fenced block and stop.'

usage() { sed -n '2,28p' "$0" >&2; exit 2; }

[ $# -ge 1 ] || usage
mode="$1"; shift
case "$mode" in start|resume) ;; *) usage ;; esac

mailbox="" max_iterations="4" helper="" run_token="" run_id="" session="" timeout_s="21600"
while [ $# -gt 0 ]; do
  case "$1" in
    --mailbox) mailbox="$2"; shift 2 ;;
    --max-iterations) max_iterations="$2"; shift 2 ;;
    --helper) helper="$2"; shift 2 ;;
    --run-token) run_token="$2"; shift 2 ;;
    --run-id) run_id="$2"; shift 2 ;;
    --session) session="$2"; shift 2 ;;
    --timeout) timeout_s="$2"; shift 2 ;;
    *) echo "launch.sh: unknown argument $1" >&2; usage ;;
  esac
done
case "$mailbox" in /*) ;; *) echo "launch.sh: --mailbox must be an absolute path" >&2; exit 2 ;; esac
[ -d "$mailbox" ] || { echo "launch.sh: $mailbox is not a directory" >&2; exit 2; }
repo="$(git -C "$mailbox" rev-parse --show-toplevel)"
record="$mailbox/.native-launch.json"
runs="$mailbox/.native-runs"
mkdir -p "$runs"

if [ "$mode" = start ]; then
  session="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  # A run_token that trio-native.js's own validation (^[A-Za-z0-9._-]{1,64}$)
  # would reject is treated as not given, so the token we track here always
  # matches the one the workflow actually begins with.
  if [[ ! "$run_token" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then run_token=""; fi
  if [ -z "$run_token" ]; then
    hex="${session//-/}"
    run_token="ls-${hex:0:12}"
  fi
  args_json="$(python3 - "$mailbox" "$max_iterations" "$helper" "$run_token" <<'PY'
import json, sys
mailbox, max_it, helper, token = sys.argv[1:5]
args = {"mailbox": mailbox, "max_iterations": int(max_it)}
if helper:
    args["helper"] = helper
args["run_token"] = token
print(json.dumps(args, separators=(",", ":")))
PY
)"
  our_token="$run_token"
  # Keep the previous record: a start that never actually began the
  # workflow restores it, so a later `resume` never targets a session that
  # never ran (eval-native-v0b N7; extended to every not-started start).
  prev_record="$runs/launch-record.$session.prev.json"
  if [ -f "$record" ]; then cp "$record" "$prev_record"; fi
  python3 - "$record" "$session" "$args_json" <<'PY'
import json, os, sys
path, session, args = sys.argv[1:4]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"session_id": session, "args": args}, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
PY
  prompt="Run the saved workflow trio-native with args ${args_json}. ${SUFFIX}"
  session_flags=(--session-id "$session")
else
  [ -n "$run_id" ] || { echo "launch.sh: resume needs --run-id" >&2; exit 2; }
  [ -f "$record" ] || { echo "launch.sh: no $record: nothing to resume (use start)" >&2; exit 2; }
  recorded_session="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session_id"])' "$record")"
  args_json="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["args"])' "$record")"
  session="${session:-$recorded_session}"
  # The token our own recorded start began with (or, for a pre-run_token
  # record, the default trio-native.js would have computed for it — see
  # README.md "Run registry and result").
  our_token="$(python3 - "$args_json" "$mailbox" <<'PY'
import json, re, sys
args_json, mailbox = sys.argv[1:3]
try:
    args = json.loads(args_json)
except ValueError:
    args = {}
token = args.get("run_token") if isinstance(args, dict) else None
if isinstance(token, str) and token:
    print(token)
else:
    mailbox = mailbox.rstrip("/")
    parts = [p for p in mailbox.split("/") if p]
    tail = re.sub(r"[^A-Za-z0-9._-]", "_", "-".join(parts[-2:]))
    print("trio-native-" + tail[:48])
PY
)"
  prompt="Resume the saved workflow trio-native with resumeFromRunId \"${run_id}\" and the byte-identical args ${args_json}. ${SUFFIX}"
  session_flags=(--resume "$session")
fi

raw="$runs/${session}.$(date -u +%Y%m%dT%H%M%SZ).$mode.json"
launcher_path="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
launched_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
# TRIO_NATIVE_LAUNCH_{MAILBOX,TOKEN} are test-support only: they let a fake
# `claude` (tests/test_launch.py, tests/test_dash_records.py) simulate
# `begin` writing .session.json without parsing the prompt; the real
# `claude` binary ignores them.
(cd "$repo" && TRIO_NATIVE_LAUNCH_MAILBOX="$mailbox" TRIO_NATIVE_LAUNCH_TOKEN="$our_token" \
  timeout "$timeout_s" "$CLAUDE_BIN" -p "$prompt" \
  "${session_flags[@]}" \
  --model "$MODEL" \
  --permission-mode auto \
  --settings "$SETTINGS" \
  --output-format json) >"$raw" 2>"$raw.err"
rc=$?
set -e

python3 - "$raw" "$rc" "$session" "$mode" "$record" "${prev_record:-}" \
  "$mailbox" "$claude_dir" "${run_id:-}" "$launcher_path" "$launched_at" \
  "$our_token" <<'PY'
import fcntl, glob, hashlib, json, os, re, sys, time
from pathlib import Path
raw, rc, session, mode, record, prev = sys.argv[1:7]
mailbox, claude_dir, given_run_id, launcher_path, launched_at = sys.argv[7:12]
our_token = sys.argv[12]
rc = int(rc)
launcher = {"session_id": session, "exit_code": rc, "raw": raw}


def _runs_dir() -> Path:
    return Path(os.environ.get("TRIO_NATIVE_RUNS_DIR", "").strip()
                or Path.home() / ".local/share/trio-agent-loop/native-runs").expanduser()


def _registry_path() -> Path:
    real = str(Path(mailbox).resolve())
    return _runs_dir() / (hashlib.sha256(real.encode()).hexdigest()[:16] + ".json")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def started() -> bool:
    """The workflow actually began iff its `begin` wrote our own run_token
    into <mailbox>/.session.json (README.md "Run registry and result")."""
    try:
        data = json.loads(Path(mailbox, ".session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("driver") == "claude-workflow"
            and data.get("session") == our_token)


STARTED = started()


def _restore_or_remove_record() -> None:
    """A start that did not actually begin the workflow must not leave a
    session/args record a later `resume` could target (eval-native-v0b N7,
    extended to every not-started start, not only a lock refusal)."""
    if prev and os.path.isfile(prev):
        os.replace(prev, record)
        launcher["record"] = "restored"
    else:
        try:
            os.remove(record)
        except OSError:
            pass
        launcher["record"] = "removed"


def find_run_id():
    """The workflow run id (wf_…) of this session, newest first."""
    if given_run_id:
        return given_run_id
    base = os.path.join(glob.escape(claude_dir), "projects", "*", glob.escape(session))
    found = []
    for pattern, rx in (
            (os.path.join(base, "workflows", "wf_*.json"), r"^(wf_[\w-]+)\.json$"),
            (os.path.join(base, "subagents", "workflows", "wf_*"), r"^(wf_[\w-]+)$"),
            (os.path.join(base, "workflows", "scripts", "*-wf_*.js"), r"-(wf_[\w-]+)\.js$")):
        for path in glob.glob(pattern):
            m = re.search(rx, os.path.basename(path))
            if m:
                try:
                    found.append((os.path.getmtime(path), m.group(1)))
                except OSError:
                    pass
    return max(found)[1] if found else None


def _update_registry(fields: dict) -> None:
    """Atomic read-modify-write of the run registry, flock-guarded (a
    sidecar `<registry file>.lock`, shared with the helper's own `_register`)
    so a concurrent writer's record for a different, possibly newer, run is
    never lost. Applied only when the existing record's run_token is ours,
    unset, or the record is already finished — otherwise a live different
    run owns it and this (belated) write backs off entirely."""
    reg = _registry_path()
    reg.parent.mkdir(parents=True, exist_ok=True)
    lock_path = reg.with_name(reg.name + ".lock")
    try:
        with open(lock_path, "a+", encoding="utf-8") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(reg.read_text(encoding="utf-8"))
                    data = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    data = {}
                existing_token = data.get("run_token")
                if existing_token not in (None, our_token) and data.get("state") != "finished":
                    return
                data.update({"schema": 1, "driver": "claude-workflow",
                             "mailbox": str(Path(mailbox).resolve())})
                data.update(fields)
                _write_json_atomic(reg, data)
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def persist(result):
    """<mailbox>/.native-result.json and the run registry, for a run that
    actually started (never fatal)."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        session_rec = json.loads(Path(mailbox, ".session.json").read_text(encoding="utf-8"))
        session_rec = session_rec if isinstance(session_rec, dict) else {}
    except (OSError, ValueError):
        session_rec = {}
    run_id = find_run_id()
    lau = result.get("launcher") if isinstance(result.get("launcher"), dict) else {}
    keep = ("status", "verdict", "code", "reason", "iteration", "held_step",
            "end_error", "conflicts", "dangling_worktrees", "role_denials",
            "human_check", "commit_shas", "lock", "agents_used",
            "eval_worktrees_removed")
    out = {k: result.get(k) for k in keep if k in result}
    out.update({"schema": 1, "source": "launcher", "driver": "claude-workflow",
                "mode": mode, "session_id": session, "run_id": run_id,
                "run_token": our_token, "exit_code": rc, "raw": raw,
                "launcher": launcher_path, "launched_at": launched_at,
                "finished_at": now,
                "session_started_at": session_rec.get("started_at"),
                "api_equiv_usd": lau.get("api_equiv_usd"),
                "api_equiv_usd_note": lau.get("api_equiv_usd_note")})
    try:
        _write_json_atomic(Path(mailbox, ".native-result.json"), out)
    except OSError:
        pass
    _update_registry({"state": "finished", "status": out.get("status"),
                       "session_id": session, "run_id": run_id,
                       "run_token": our_token, "launcher": launcher_path,
                       "finished_at": now,
                       "result_path": str(Path(mailbox).resolve() / ".native-result.json"),
                       "updated_at": now})


def persist_not_started(result):
    """A run that never actually started never touches .native-result.json
    nor the registry; its outcome goes to a per-session file instead."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = dict(result)
    out.setdefault("launcher", {})
    if not isinstance(out["launcher"], dict):
        out["launcher"] = {}
    out["launcher"].update(launcher)
    out.update({"schema": 1, "source": "launcher", "driver": "claude-workflow",
                "mode": mode, "session_id": session, "started": False,
                "run_token": our_token, "launched_at": launched_at,
                "finished_at": now})
    try:
        _write_json_atomic(Path(mailbox, ".native-runs", f"{session}.result.json"), out)
    except OSError:
        pass


try:
    err = open(raw + ".err", encoding="utf-8", errors="replace").read()
except OSError:
    err = ""
# claude -p's bg-wait ceiling killed the Workflow (probe 3 blocker E); the
# run is recoverable with `launch.sh resume --run-id` or a fresh start.
bg_killed = "Background tasks still running" in err
if bg_killed:
    launcher["bg_wait_ceiling"] = True


def fail(reason, head=None):
    if bg_killed:
        reason = ("bg-wait ceiling terminated workflow: claude -p killed its "
                  "background tasks (CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS); "
                  "resume with launch.sh resume --run-id, or start again")
    if head:
        launcher["result_head"] = head
        reason += "; session reply begins: " + json.dumps(head)
    result = {"status": "error", "reason": reason, "launcher": launcher}
    if STARTED:
        persist(result)
    else:
        _restore_or_remove_record()
        persist_not_started(result)
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(3)


try:
    body = open(raw, encoding="utf-8", errors="replace").read()
except OSError:
    body = ""
try:
    outer = json.loads(body)
except ValueError:
    outer = None
if outer is None:
    text = body
elif isinstance(outer, dict):
    text = outer.get("result")
    text = text if isinstance(text, str) else ""
    # The session's total_cost_usd / modelUsage[].costUSD are API list-price
    # estimates, not what a subscription is billed: label them so.
    usd = outer.get("total_cost_usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        launcher["api_equiv_usd"] = usd
    usage = outer.get("modelUsage")
    if isinstance(usage, dict):
        by_model = {m: u["costUSD"] for m, u in usage.items()
                    if isinstance(u, dict) and isinstance(u.get("costUSD"), (int, float))}
        if by_model:
            launcher["api_equiv_usd_by_model"] = by_model
    if "api_equiv_usd" in launcher or "api_equiv_usd_by_model" in launcher:
        launcher["api_equiv_usd_note"] = "estimate at API list price, not billed"
else:
    fail("session output is JSON but not an object")
# ```json / ```JSON / ```jsonc / a bare fence; CRLF tolerated.
blocks = re.findall(r"```(?:jsonc?|json5)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", text,
                    re.S | re.I)
dicts = []
for block in blocks:
    try:
        value = json.loads(block)
    except ValueError:
        continue
    if isinstance(value, dict):
        dicts.append(value)
# The workflow result carries `status`; a trailing non-result block loses.
results = [d for d in dicts if "status" in d] or dicts
if not results:
    # First 300 chars of the final reply: e.g. a launch session that
    # refused to run the workflow (probe 3 G).
    head = (text or "").strip()[:300]
    fail("no fenced result JSON in the session output", head or None)
result = results[-1]
if STARTED:
    if prev and os.path.isfile(prev):
        try:
            os.remove(prev)
        except OSError:
            pass
else:
    _restore_or_remove_record()
result.setdefault("launcher", {})
if not isinstance(result["launcher"], dict):
    result["launcher"] = {}
result["launcher"].update(launcher)
if STARTED:
    persist(result)
else:
    persist_not_started(result)
print(json.dumps(result, indent=2, sort_keys=True))
sys.exit(0)
PY

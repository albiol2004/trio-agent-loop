#!/usr/bin/env bash
# trio-native lab launcher: one headless Claude Code session that only
# launches the saved `trio-native` workflow and returns its result JSON.
#
#   launch.sh start  --mailbox /abs/repo/loop [--max-iterations N] [--helper /abs/trio_native_step.py]
#                    [--run-token T] [--timeout SECONDS]
#   launch.sh resume --mailbox /abs/repo/loop --run-id wf_… [--session UUID] [--timeout SECONDS]
#
# start   records a fresh --session-id and the exact args JSON in
#         <mailbox>/.native-launch.json, then runs the workflow.
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
# 2 on usage errors.
set -euo pipefail

CLAUDE_BIN="${TRIO_NATIVE_CLAUDE:-claude}"
MODEL="claude-opus-5-5"
SETTINGS='{"worktree":{"baseRef":"head"}}'
SUFFIX='Launch only; do not edit files, settings or permissions; output the result JSON verbatim in one fenced block and stop.'

usage() { sed -n '2,27p' "$0" >&2; exit 2; }

[ $# -ge 1 ] || usage
mode="$1"; shift
case "$mode" in start|resume) ;; *) usage ;; esac

mailbox="" max_iterations="4" helper="" run_token="" run_id="" session="" timeout_s="14400"
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
  args_json="$(python3 - "$mailbox" "$max_iterations" "$helper" "$run_token" <<'PY'
import json, sys
mailbox, max_it, helper, token = sys.argv[1:5]
args = {"mailbox": mailbox, "max_iterations": int(max_it)}
if helper:
    args["helper"] = helper
if token:
    args["run_token"] = token
print(json.dumps(args, separators=(",", ":")))
PY
)"
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
  prompt="Resume the saved workflow trio-native with resumeFromRunId \"${run_id}\" and the byte-identical args ${args_json}. ${SUFFIX}"
  session_flags=(--resume "$session")
fi

raw="$runs/${session}.$(date -u +%Y%m%dT%H%M%SZ).$mode.json"
set +e
(cd "$repo" && timeout "$timeout_s" "$CLAUDE_BIN" -p "$prompt" \
  "${session_flags[@]}" \
  --model "$MODEL" \
  --permission-mode auto \
  --settings "$SETTINGS" \
  --output-format json) >"$raw" 2>"$raw.err"
rc=$?
set -e

python3 - "$raw" "$rc" "$session" <<'PY'
import json, re, sys
raw, rc, session = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    outer = json.load(open(raw, encoding="utf-8"))
    text = outer.get("result") or ""
except (OSError, ValueError):
    text = open(raw, encoding="utf-8", errors="replace").read()
blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
for block in reversed(blocks):
    try:
        result = json.loads(block)
    except ValueError:
        continue
    if isinstance(result, dict):
        result.setdefault("launcher", {})
        result["launcher"].update(session_id=session, exit_code=rc, raw=raw)
        print(json.dumps(result, indent=2, sort_keys=True))
        sys.exit(0)
print(json.dumps({"status": "error", "reason": "no fenced result JSON in the session output",
                  "launcher": {"session_id": session, "exit_code": rc, "raw": raw}},
                 indent=2, sort_keys=True))
sys.exit(3)
PY

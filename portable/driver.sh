#!/usr/bin/env bash
# Portable trio-loop shim: dispatches role prompts through any agentic CLI and
# delegates gates, verdicts, repairs, and resume state to metrics/trio_loop.py.
# State lives entirely in the mailbox dir (default loop/; override with
# LOOP_DIR=loop-<name> to run concurrent loops) — safe to kill and re-run.
# Exit codes: 0 SHIP, 2 BLOCKED, 3 bad verdict, 4 cap,
# 5 NEEDS_HUMAN (or mailbox locked by another driver).
#
# Usage:
#   HARNESS=claude ./portable/driver.sh
#   HARNESS=cursor ./portable/driver.sh          # CURSOR_BIN=agent on newer installs
#   HARNESS=opencode ./portable/driver.sh        # native opencode/ is preferred; this is the fallback
#   HARNESS=gemini ./portable/driver.sh          # opts: GEMINI_MODEL
#   HARNESS=agy ./portable/driver.sh             # Antigravity CLI — verify flags with agy --help first
#   HARNESS=hermes ./portable/driver.sh          # opts: HERMES_MODEL
#   HARNESS=athen ./portable/driver.sh           # needs ATHEN_BASE_URL+ATHEN_MODEL; opts: ATHEN_BIN, ATHEN_{LEAD,EVAL}_PROFILE
#   HARNESS=generic RUN_LEAD='mycli run --prompt-file' RUN_EVAL='mycli run --prompt-file' ./portable/driver.sh
#
# Prereq: $LOOP_DIR/GOAL.md exists (copy portable/GOAL.template.md and fill it in).
# Concurrency: ONE loop per mailbox dir. Run a second loop in the same repo
# with LOOP_DIR=loop-<name>. Python owns the mailbox lock.
#
# Open-loop mode (mailbox has QUEUE.md): auto-selected by metrics/trio_loop.py,
# or force it either way:
#   TRIO_MODE=open-loop ./portable/driver.sh    # --open-loop; TRIO_MODE=lockstep -> --lockstep
#   POLL_SECONDS=10 ./portable/driver.sh        # Evaluator poll interval -> --poll-seconds
# _PortableRunner also sets TRIO_MODE/TRIO_KIND/TRIO_SLICE/TRIO_SHA per role
# invocation so build_prompt can render an OPEN-LOOP CONTEXT block.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="${HARNESS:-generic}"
LOOP_DIR="${LOOP_DIR:-loop}"
if [[ "${1:-}" != "--run-role" ]]; then
  MAX_ITER="${1:-10}"
fi

[[ -f "$LOOP_DIR/GOAL.md" ]] || { echo "$LOOP_DIR/GOAL.md missing — copy portable/GOAL.template.md to $LOOP_DIR/GOAL.md and edit it." >&2; exit 1; }

# build_prompt <file> — role prompts say `loop/`; when LOOP_DIR overrides it,
# prepend a mailbox-override note so fresh-context roles resolve paths right.
# api:OpenLoopPromptEnv: when TRIO_MODE=open-loop (set per-role by
# _PortableRunner.run), prepend an OPEN-LOOP CONTEXT block naming the kind
# and, when set, the slice id and sha — before the MAILBOX OVERRIDE line.
# TRIO_MODE unset/empty must leave this byte-identical to the lockstep output.
build_prompt() {
  if [[ "${TRIO_MODE:-}" == "open-loop" ]]; then
    printf 'OPEN-LOOP CONTEXT: kind=%s' "${TRIO_KIND:-}"
    [[ -n "${TRIO_SLICE:-}" ]] && printf ' slice=%s' "$TRIO_SLICE"
    [[ -n "${TRIO_SHA:-}" ]] && printf ' sha=%s' "$TRIO_SHA"
    printf '\n\n'
  fi
  if [[ "$LOOP_DIR" != "loop" ]]; then
    printf 'MAILBOX OVERRIDE: this run uses `%s/` as the loop mailbox — every `loop/` path in the instructions below resolves to `%s/`.\n\n' "$LOOP_DIR" "$LOOP_DIR"
  fi
  cat "$1"
}

# run_role <prompt-file>  — one fresh-context invocation of the chosen harness
run_role() {
  local prompt_file="$1"
  case "$HARNESS" in
    claude)  claude -p "$(build_prompt "$prompt_file")" --permission-mode acceptEdits ;;
    opencode) # Compatibility fallback; native named Task roles live in opencode/. See SETUP-opencode.md.
             local agent_flag=""
             [[ "$prompt_file" == *lead* || "$prompt_file" == *repair* ]] && agent_flag="${OPENCODE_LEAD_AGENT:-}" || agent_flag="${OPENCODE_EVAL_AGENT:-}"
             timeout "${ROLE_TIMEOUT:-1200}" opencode run --auto \
               ${OPENCODE_MODEL:+-m "$OPENCODE_MODEL"} ${agent_flag:+--agent "$agent_flag"} \
               "$(build_prompt "$prompt_file")" ;;
    gemini)  # verified: -p one-shot; approval-mode=yolo per invocation (cannot be persisted)
             timeout "${ROLE_TIMEOUT:-1200}" gemini --approval-mode=yolo \
               ${GEMINI_MODEL:+-m "$GEMINI_MODEL"} -p "$(build_prompt "$prompt_file")" ;;
    agy)     # Antigravity CLI — flags UNVERIFIED from primary docs; confirm with agy --help (see SETUP-antigravity.md)
             timeout "${ROLE_TIMEOUT:-1200}" agy --headless --approve all "$(build_prompt "$prompt_file")" ;;
    hermes)  # --yolo required: non-interactive runs auto-DENY dangerous approvals without it
             timeout "${ROLE_TIMEOUT:-1200}" hermes -z "$(build_prompt "$prompt_file")" --yolo --quiet \
               ${HERMES_MODEL:+-m "$HERMES_MODEL"} ;;
    athen)   # requires ATHEN_BASE_URL + ATHEN_MODEL in env (exit 2 otherwise) — see SETUP-athen.md
             local ath_profile=""
             [[ "$prompt_file" == *lead* || "$prompt_file" == *repair* ]] && ath_profile="${ATHEN_LEAD_PROFILE:-}" || ath_profile="${ATHEN_EVAL_PROFILE:-}"
             ATHEN_WORKSPACE_DIR="$PWD" ATHEN_DISABLE_RISK_GATE=1 \
               timeout "${ROLE_TIMEOUT:-2000}" \
               "${ATHEN_BIN:-athen-cli}" \
               ${ath_profile:+--profile "$ath_profile"} --prompt "$(build_prompt "$prompt_file")" ;;
    cursor)  "${CURSOR_BIN:-cursor-agent}" -p --force "$(build_prompt "$prompt_file")" ;;  # without --force, -p only PROPOSES edits; newer installs: CURSOR_BIN=agent
    generic) local cmd_var; [[ "$prompt_file" == *lead* || "$prompt_file" == *repair* ]] && cmd_var="${RUN_LEAD:?set RUN_LEAD}" || cmd_var="${RUN_EVAL:?set RUN_EVAL}"
             local pf="$prompt_file"
             if [[ "$LOOP_DIR" != "loop" || "${TRIO_MODE:-}" == "open-loop" ]]; then pf="$(mktemp)"; build_prompt "$prompt_file" > "$pf"; fi
             $cmd_var "$pf" ;;
    *) echo "unknown HARNESS=$HARNESS" >&2; exit 1 ;;
  esac
}

if [[ "${1:-}" == --run-role ]]; then
  # usage: driver.sh --run-role lead|evaluator|repair
  # LOOP_DIR/HARNESS already in env. No lock. No python.
  role="$2"
  case "$role" in
    lead) prompt="$DIR/prompts/lead.md" ;;
    evaluator) prompt="$DIR/prompts/evaluator.md" ;;
    repair) prompt="$DIR/prompts/repair.md" ;;
    *) echo "unknown role $role" >&2; exit 1 ;;
  esac
  run_role "$prompt"
  exit $?
fi

# Python owns the lock and the complete loop state machine.
# TRIO_MODE/POLL_SECONDS pass through to trio_loop.py's own flags; leaving
# TRIO_MODE unset keeps auto-selection (QUEUE.md -> open-loop) in charge.
args=(--mailbox "$LOOP_DIR" --max-iterations "$MAX_ITER" --runner portable)
case "${TRIO_MODE:-}" in
  "") ;;
  open-loop) args+=(--open-loop) ;;
  lockstep) args+=(--lockstep) ;;
  *) echo "unknown TRIO_MODE=$TRIO_MODE (expected 'open-loop' or 'lockstep')" >&2; exit 1 ;;
esac
[[ -n "${POLL_SECONDS:-}" ]] && args+=(--poll-seconds "$POLL_SECONDS")

exec python3 "$DIR/../metrics/trio_loop.py" run "${args[@]}"

#!/usr/bin/env bash
# Portable trio-loop driver: alternates Lead and Evaluator prompts through any
# agentic CLI with a non-interactive mode, until VERDICT: SHIP/BLOCKED/NEEDS_HUMAN
# or the iteration cap. Verdicts may carry a scope: `VERDICT: ITERATE scope=design`
# (explicit full Lead iteration) or `VERDICT: ITERATE scope=local:<paths>`
# (builder-direct repair pass, capped at 2 consecutive repairs, tracked in
# $LOOP_DIR/.repairs). State lives entirely in the mailbox dir (default loop/;
# override with LOOP_DIR=loop-<name> to run concurrent loops) — safe to kill
# and re-run. Exit codes: 0 SHIP, 2 BLOCKED, 3 bad verdict, 4 cap,
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
# with LOOP_DIR=loop-<name>; the .lock below makes sharing a mailbox an error.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_ITER="${1:-10}"
HARNESS="${HARNESS:-generic}"
LOOP_DIR="${LOOP_DIR:-loop}"

[[ -f "$LOOP_DIR/GOAL.md" ]] || { echo "$LOOP_DIR/GOAL.md missing — copy portable/GOAL.template.md to $LOOP_DIR/GOAL.md and edit it." >&2; exit 1; }

# Single-orchestrator lock (mkdir is atomic and portable). Exit 5 = mailbox busy.
if ! mkdir "$LOOP_DIR/.lock" 2>/dev/null; then
  lock_pid="$(cat "$LOOP_DIR/.lock/pid" 2>/dev/null || true)"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "Mailbox $LOOP_DIR/ is owned by a live driver (pid $lock_pid) — run your loop with LOOP_DIR=loop-<name>." >&2
    exit 5
  fi
  echo "Removing stale lock on $LOOP_DIR/ (pid ${lock_pid:-unknown} is gone)." >&2
  rm -rf "$LOOP_DIR/.lock" && mkdir "$LOOP_DIR/.lock"
fi
echo $$ > "$LOOP_DIR/.lock/pid"
trap 'rm -rf "$LOOP_DIR/.lock"' EXIT

[[ -f "$LOOP_DIR/STATE.md" ]] || printf 'iteration: 0\n\n## Approaches tried and rejected\n\n## Key decisions and rationale\n' > "$LOOP_DIR/STATE.md"
[[ -f "$LOOP_DIR/LOG.md"   ]] || echo '# Trio loop log' > "$LOOP_DIR/LOG.md"

# build_prompt <file> — role prompts say `loop/`; when LOOP_DIR overrides it,
# prepend a mailbox-override note so fresh-context roles resolve paths right.
build_prompt() {
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
             if [[ "$LOOP_DIR" != "loop" ]]; then pf="$(mktemp)"; build_prompt "$prompt_file" > "$pf"; fi
             $cmd_var "$pf" ;;
    *) echo "unknown HARNESS=$HARNESS" >&2; exit 1 ;;
  esac
}

iter="$(awk -F': ' '/^iteration:/{print $2}' "$LOOP_DIR/STATE.md")"

# Consecutive builder-direct repair counter (driver-internal; see
# MAILBOX-SCHEMA.md). Persisted in $LOOP_DIR/.repairs so kill/resume never
# exceeds the 2-repair cap; reset to 0 on every full Lead pass.
repair_count=0
if [[ -f "$LOOP_DIR/.repairs" ]]; then
  repair_count="$(cat "$LOOP_DIR/.repairs" 2>/dev/null || true)"
  [[ "$repair_count" =~ ^[0-9]+$ ]] || repair_count=0
fi
repair_pending=false  # when true, the next iteration's lead slot runs prompts/repair.md

while (( iter < MAX_ITER )); do
  iter=$((iter + 1))
  sed -i "s/^iteration: .*/iteration: $iter/" "$LOOP_DIR/STATE.md"

  if $repair_pending; then
    echo "=== iteration $iter/$MAX_ITER — repair (scope=local) ==="
    run_role "$DIR/prompts/repair.md"
    repair_pending=false
  else
    echo "=== iteration $iter/$MAX_ITER — lead ==="
    run_role "$DIR/prompts/lead.md"
  fi

  echo "=== iteration $iter/$MAX_ITER — evaluator ==="
  run_role "$DIR/prompts/evaluator.md"

  verdict_line="$(head -1 "$LOOP_DIR/VERDICT.md" 2>/dev/null | tr -d '\r')"
  echo "=== $verdict_line ==="
  # Parse the first line into a verdict word plus an optional scope= suffix
  # (e.g. `VERDICT: ITERATE scope=local:a.sh,b.py`). The word decides the
  # route; the suffix only refines ITERATE. Plain `VERDICT: ITERATE` behaves
  # exactly as before (full Lead iteration).
  verdict_rest="${verdict_line#"VERDICT: "}"
  if [[ "$verdict_rest" == "$verdict_line" ]]; then
    echo "Unparseable or missing verdict — stopping to avoid a runaway loop." >&2
    exit 3
  fi
  verdict_word="${verdict_rest%% *}"
  scope="${verdict_rest#"$verdict_word"}"
  scope="${scope# }"  # drop exactly one separating space

  case "$verdict_word" in
    SHIP)    echo "Done — ready for human review (see $LOOP_DIR/VERDICT.md)."; exit 0 ;;
    BLOCKED) echo "Loop blocked — human decision needed (see $LOOP_DIR/VERDICT.md)."; exit 2 ;;
    NEEDS_HUMAN)
      echo "Human gate reached — agent-verifiable criteria pass; follow the '## Human check' steps in $LOOP_DIR/VERDICT.md."
      exit 5 ;;
    ITERATE)
      case "$scope" in
        "" | "scope=design")
          # Full Lead iteration (implicit or explicit): any consecutive-repair
          # streak ends here.
          repair_count=0
          repair_pending=false
          printf '0\n' > "$LOOP_DIR/.repairs"
          ;;
        "scope=local:"*)
          if (( repair_count < 2 )); then
            repair_count=$((repair_count + 1))
            printf '%s\n' "$repair_count" > "$LOOP_DIR/.repairs"
            repair_pending=true
            echo "Scoped repair queued (${repair_count}/2 consecutive) — the next lead slot runs the repair pass."
          else
            echo "Repair cap hit (2 consecutive scope=local verdicts) — forcing a full Lead iteration." >&2
            repair_count=0
            printf '0\n' > "$LOOP_DIR/.repairs"
          fi
          ;;
        *) echo "Unparseable or missing verdict — stopping to avoid a runaway loop." >&2; exit 3 ;;
      esac
      ;;
    *) echo "Unparseable or missing verdict — stopping to avoid a runaway loop." >&2; exit 3 ;;
  esac
done
echo "Hit max iterations ($MAX_ITER) without SHIP — see $LOOP_DIR/LOG.md and $LOOP_DIR/VERDICT.md." >&2
exit 4

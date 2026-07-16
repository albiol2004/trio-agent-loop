#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 scout|lead|builder|evaluator CONTEXT_FILE RESULT_FILE [PROJECT_ROOT]" >&2
  exit 2
}

[[ $# -ge 3 && $# -le 4 ]] || usage

ROLE="$1"
CONTEXT_FILE="$2"
RESULT_FILE="$3"
PROJECT_ROOT="${4:-$PWD}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_BIN="${CODEX_BIN:-codex}"

[[ -f "$CONTEXT_FILE" ]] || {
  echo "context file not found: $CONTEXT_FILE" >&2
  exit 2
}
[[ -d "$PROJECT_ROOT" ]] || {
  echo "project root not found: $PROJECT_ROOT" >&2
  exit 2
}
command -v "$CODEX_BIN" >/dev/null 2>&1 || {
  echo "Codex executable not found: $CODEX_BIN" >&2
  exit 127
}

case "$ROLE" in
  scout|builder)
    MODEL="gpt-5.6-luna"
    ;;
  lead|evaluator)
    MODEL="gpt-5.6-terra"
    ;;
  *)
    usage
    ;;
esac

PROMPT_FILE="$ROOT/references/prompts/$ROLE.md"
[[ -f "$PROMPT_FILE" ]] || {
  echo "role prompt not found: $PROMPT_FILE" >&2
  exit 2
}

mkdir -p "$(dirname "$RESULT_FILE")"

{
  cat "$PROMPT_FILE"
  printf '\n\n# Invocation context\n\n'
  cat "$CONTEXT_FILE"
} | "$CODEX_BIN" exec \
  --ephemeral \
  -C "$PROJECT_ROOT" \
  -m "$MODEL" \
  -c 'model_reasoning_effort="high"' \
  --output-last-message "$RESULT_FILE" \
  -

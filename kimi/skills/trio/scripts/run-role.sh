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
KIMI_BIN="${KIMI_BIN:-kimi}"

[[ -f "$CONTEXT_FILE" ]] || {
  echo "context file not found: $CONTEXT_FILE" >&2
  exit 2
}
[[ -d "$PROJECT_ROOT" ]] || {
  echo "project root not found: $PROJECT_ROOT" >&2
  exit 2
}
command -v "$KIMI_BIN" >/dev/null 2>&1 || {
  echo "Kimi executable not found: $KIMI_BIN" >&2
  exit 127
}

case "$ROLE" in
  scout|builder)
    MODEL="kimi-code/kimi-for-coding"
    ;;
  lead|evaluator)
    MODEL="kimi-code/k3"
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

# Read before changing directory so relative context paths remain valid. The
# sentinel keeps command substitution from stripping a context's final newline.
PROMPT="$(cat "$PROMPT_FILE"; printf '\n\n# Invocation context\n\n'; cat "$CONTEXT_FILE"; printf '\001')"
PROMPT="${PROMPT%$'\001'}"

mkdir -p "$(dirname "$RESULT_FILE")"
RESULT_FILE="$(cd "$(dirname "$RESULT_FILE")" && pwd)/$(basename "$RESULT_FILE")"
cd "$PROJECT_ROOT"

# Kimi's documented print mode writes the Assistant response to stdout and
# diagnostics to stderr. Persist only the response as the next role's brief;
# leave diagnostics visible to the orchestrator. Quoting the complete prompt
# preserves newlines and shell metacharacters in role context.
"$KIMI_BIN" -m "$MODEL" -p "$PROMPT" >"$RESULT_FILE"

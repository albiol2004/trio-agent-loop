#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

HOME="$TMP/home"
export HOME
export KIMI_CODE_HOME="$TMP/kimi-home"
mkdir -p "$HOME" "$TMP/bin"

"$ROOT/install.sh" --kimi >/dev/null
[[ -f "$KIMI_CODE_HOME/skills/trio/SKILL.md" ]]
[[ -f "$KIMI_CODE_HOME/skills/trio-init/SKILL.md" ]]
[[ -x "$KIMI_CODE_HOME/skills/trio/scripts/run-role.sh" ]]

DEFAULT_HOME="$TMP/default-home"
env -u KIMI_CODE_HOME HOME="$DEFAULT_HOME" "$ROOT/install.sh" --kimi >/dev/null
[[ -f "$DEFAULT_HOME/.kimi-code/skills/trio/SKILL.md" ]]
[[ -f "$DEFAULT_HOME/.kimi-code/skills/trio-init/SKILL.md" ]]
[[ -x "$DEFAULT_HOME/.kimi-code/skills/trio/scripts/run-role.sh" ]]

cat >"$TMP/bin/kimi" <<'FAKE_KIMI'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "-m" && "${3:-}" == "-p" ]]
printf '%s' "$2" >"$KIMI_FAKE_MODEL_FILE"
printf '%s' "$4" >"$KIMI_FAKE_PROMPT_FILE"
pwd >"$KIMI_FAKE_PWD_FILE"
printf 'offline Kimi response\n'
printf 'offline Kimi diagnostic\n' >&2
FAKE_KIMI
chmod +x "$TMP/bin/kimi"
export PATH="$TMP/bin:$PATH"

CONTEXT="$TMP/context.md"
RESULT="$TMP/result.md"
export KIMI_FAKE_MODEL_FILE="$TMP/model.txt"
export KIMI_FAKE_PROMPT_FILE="$TMP/prompt.txt"
export KIMI_FAKE_PWD_FILE="$TMP/pwd.txt"
printf 'line one\nline two with $dollar and "quotes"\n\n' >"$CONTEXT"

for ROLE_AND_MODEL in \
  'lead kimi-code/k3' \
  'evaluator kimi-code/k3' \
  'scout kimi-code/kimi-for-coding' \
  'builder kimi-code/kimi-for-coding'; do
  ROLE="${ROLE_AND_MODEL%% *}"
  MODEL="${ROLE_AND_MODEL#* }"
  "$KIMI_CODE_HOME/skills/trio/scripts/run-role.sh" "$ROLE" "$CONTEXT" "$RESULT" "$ROOT" \
    2>>"$TMP/diagnostics.txt"
  [[ "$(<"$TMP/model.txt")" == "$MODEL" ]]
  [[ "$(<"$TMP/pwd.txt")" == "$ROOT" ]]
done

grep -Fq 'line one' "$TMP/prompt.txt"
grep -Fq 'line two with $dollar and "quotes"' "$TMP/prompt.txt"
[[ "$(tail -c 1 "$TMP/prompt.txt" | od -An -t x1 | tr -d '[:space:]')" == "0a" ]]
grep -Fqx 'offline Kimi response' "$RESULT"
! grep -Fq 'offline Kimi diagnostic' "$RESULT"
grep -Fq 'offline Kimi diagnostic' "$TMP/diagnostics.txt"
if "$KIMI_CODE_HOME/skills/trio/scripts/run-role.sh" invalid "$CONTEXT" "$RESULT" "$ROOT" >/dev/null 2>&1; then
  echo 'invalid role unexpectedly succeeded' >&2
  exit 1
fi

printf 'Kimi installer and runner smoke test passed\n'

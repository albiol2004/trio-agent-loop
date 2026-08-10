#!/usr/bin/env bash
# Pin user-selected models for the omp Trio agent bundle via
# task.agentModelOverrides in the global omp configuration.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: configure-models.sh --strong-model <provider/model[:suffix]> --light-model <provider/model[:suffix]> [--agent-dir <dir>]
EOF
  exit 2
}

strong_model=""
light_model=""
agent_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --strong-model)
      [[ $# -gt 1 ]] || usage
      strong_model="$2"
      shift 2 ;;
    --light-model)
      [[ $# -gt 1 ]] || usage
      light_model="$2"
      shift 2 ;;
    --agent-dir)
      [[ $# -gt 1 ]] || usage
      agent_dir="$2"
      shift 2 ;;
    --help|-h)
      usage ;;
    *)
      usage ;;
  esac
done

[[ -n "$strong_model" && -n "$light_model" ]] || usage

if ! command -v omp >/dev/null 2>&1; then
  echo "configure-models.sh: omp is not on PATH" >&2
  exit 1
fi

validate_model() {
  local value="$1"
  if [[ ! "$value" =~ ^[A-Za-z0-9._+-]+/[A-Za-z0-9._+-]+(:[A-Za-z0-9._+-]+)?$ ]]; then
    echo "configure-models.sh: invalid model selector '$value'; expected provider/model[:suffix]" >&2
    exit 2
  fi
}
validate_model "$strong_model"
validate_model "$light_model"

resolve_agent_dir() {
  if [[ -n "$agent_dir" ]]; then
    printf '%s\n' "$agent_dir"
  elif command -v omp >/dev/null 2>&1; then
    omp config path
  else
    printf '%s\n' "${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
  fi
}
resolved_agent_dir="$(resolve_agent_dir)"

# Choose a JSON merger; prefer python3, fall back to jq.
if command -v python3 >/dev/null 2>&1; then
  JSON_TOOL="python3"
elif command -v jq >/dev/null 2>&1; then
  JSON_TOOL="jq"
else
  echo "configure-models.sh: python3 or jq is required to merge JSON" >&2
  exit 1
fi

# Read the existing overrides record. `omp config get --json` is documented to
# emit {"key":"...","value":{...}}. Treat missing/empty/null/non-JSON as {}.
config_json="$(omp config get task.agentModelOverrides --json 2>/dev/null || true)"
if [[ -z "${config_json//[[:space:]]/}" ]]; then
  current="{}"
elif [[ "$JSON_TOOL" == "python3" ]]; then
  current="$(CFG="$config_json" python3 - <<'PY'
import json, os
cfg = os.environ.get('CFG', '{}').strip() or '{}'
try:
    d = json.loads(cfg)
except json.JSONDecodeError:
    d = {}
if not isinstance(d, dict):
    d = {}
if 'value' in d:
    d = d['value'] if isinstance(d['value'], dict) else {}
print(json.dumps(d))
PY
)"
else
  current="$(jq -r 'if type == "object" then (.value // {}) else {} end' <<<"$config_json")"
fi
[[ -n "$current" ]] || current="{}"

# Merge the four Trio entries, preserving any pre-existing non-Trio overrides.
if [[ "$JSON_TOOL" == "python3" ]]; then
  merged_json="$(STRONG="$strong_model" LIGHT="$light_model" CURRENT="$current" python3 - <<'PY'
import json, os
try:
    current = json.loads(os.environ.get('CURRENT', '{}'))
except json.JSONDecodeError:
    current = {}
if not isinstance(current, dict):
    current = {}
strong = os.environ['STRONG']
light = os.environ['LIGHT']
current.update({
    'trio-lead': strong,
    'trio-evaluator': strong,
    'trio-scout': light,
    'trio-builder': light,
})
print(json.dumps(current, separators=(',', ':')))
PY
)"
else
  merged_json="$(jq -n \
    --argjson current "$current" \
    --arg strong "$strong_model" \
    --arg light "$light_model" \
    '$current + {"trio-lead":$strong, "trio-evaluator":$strong, "trio-scout":$light, "trio-builder":$light}')"
fi

omp config set task.agentModelOverrides "$merged_json"

echo "Configured omp Trio model overrides in $resolved_agent_dir"
echo "  strong: $strong_model (trio-lead, trio-evaluator)"
echo "  light:  $light_model (trio-scout, trio-builder)"
echo "  mapping: $merged_json"
echo "  Note: task.agentModelOverrides take precedence over the model: pins in the agent frontmatter."

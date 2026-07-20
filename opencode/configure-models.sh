#!/usr/bin/env bash
# Apply user-selected OpenCode model IDs to the installed Trio agent files.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: configure-models.sh (--global | --project [dir] | --config-dir dir) \
  --strong-model provider/model --light-model provider/model
EOF
  exit 2
}

target_mode=""
target_dir=""
strong_model=""
light_model=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --global)
      [[ -z "$target_mode" ]] || usage
      target_mode="global"
      shift ;;
    --project)
      [[ -z "$target_mode" ]] || usage
      target_mode="project"
      if [[ $# -gt 1 && "$2" != --* ]]; then
        target_dir="$2"
        shift 2
      else
        target_dir="$PWD"
        shift
      fi ;;
    --config-dir)
      [[ -z "$target_mode" && $# -gt 1 ]] || usage
      target_mode="config-dir"
      target_dir="$2"
      shift 2 ;;
    --strong-model)
      [[ $# -gt 1 ]] || usage
      strong_model="$2"
      shift 2 ;;
    --light-model)
      [[ $# -gt 1 ]] || usage
      light_model="$2"
      shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$target_mode" && -n "$strong_model" && -n "$light_model" ]] || usage

validate_model() {
  local value="$1"
  [[ "$value" =~ ^[A-Za-z0-9._+-]+/[A-Za-z0-9._:/+-]+$ ]] || {
    echo "Invalid OpenCode model ID '$value'; use an exact provider/model value from 'opencode models'." >&2
    exit 2
  }
}

validate_model "$strong_model"
validate_model "$light_model"

case "$target_mode" in
  global) config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/opencode" ;;
  project) config_dir="${target_dir%/}/.opencode" ;;
  config-dir) config_dir="${target_dir%/}" ;;
esac

validate_agent_file() {
  local role="$1"
  local file="$config_dir/agents/$role.md"

  [[ -f "$file" ]] || {
    echo "Missing installed Trio agent: $file" >&2
    exit 1
  }
  grep -Fq 'description:' "$file" && grep -Fq 'Trio' "$file" || {
    echo "Refusing to modify an unrecognized agent file: $file" >&2
    exit 1
  }
}

# Validate the entire destination before changing any role so a missing or
# user-owned same-name file cannot leave a partially updated tier mapping.
for role in trio-orchestrator trio-lead trio-evaluator trio-scout trio-builder; do
  validate_agent_file "$role"
done

set_model() {
  local role="$1"
  local model="$2"
  local file="$config_dir/agents/$role.md"
  local temp_file

  temp_file="$(mktemp "${TMPDIR:-/tmp}/trio-opencode-model.XXXXXX")"
  awk -v selected_model="$model" '
    BEGIN { in_frontmatter = 0; inserted = 0 }
    NR == 1 && $0 == "---" { in_frontmatter = 1; print; next }
    in_frontmatter && $0 == "---" {
      if (!inserted) {
        print "model: " selected_model
        inserted = 1
      }
      in_frontmatter = 0
      print
      next
    }
    in_frontmatter && /^model:[[:space:]]*/ {
      if (!inserted) {
        print "model: " selected_model
        inserted = 1
      }
      next
    }
    in_frontmatter && /^mode:[[:space:]]*/ {
      print
      if (!inserted) {
        print "model: " selected_model
        inserted = 1
      }
      next
    }
    { print }
  ' "$file" > "$temp_file"
  mv "$temp_file" "$file"
}

for role in trio-orchestrator trio-lead trio-evaluator; do
  set_model "$role" "$strong_model"
done
for role in trio-scout trio-builder; do
  set_model "$role" "$light_model"
done

echo "Configured OpenCode Trio models in $config_dir/agents"
echo "  strong: $strong_model (orchestrator, lead, evaluator)"
echo "  light:  $light_model (scout, builder)"

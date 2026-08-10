#!/usr/bin/env bash
# Credential-free structural validation for the native omp Trio bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OMP="$ROOT/omp"
INSTALLED=0

if [[ $# -gt 0 ]]; then
  if [[ "$1" == "--installed" ]]; then
    INSTALLED=1
    shift
  else
    echo "usage: smoke-test.sh [--installed]" >&2
    exit 2
  fi
fi

fail() { echo "smoke-test: $*" >&2; exit 1; }
warn() { echo "smoke-test: warning: $*" >&2; }

OMP_AVAILABLE=0
if command -v omp >/dev/null 2>&1; then
  OMP_AVAILABLE=1
else
  warn "omp is not on PATH; skipping omp-dependent checks"
fi

resolve_agent_dir() {
  if [[ $OMP_AVAILABLE -eq 1 ]]; then
    omp config path
  else
    printf '%s\n' "${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
  fi
}

# Extract the YAML frontmatter between the first pair of --- lines.
frontmatter() {
  awk 'NR==1 && $0=="---"{fm=1; next} fm && $0=="---"{exit} fm {print}' "$1"
}

# Extract the body after the first pair of --- lines, or the whole file if no
# frontmatter is present.
body() {
  awk '
    NR==1 && $0=="---" { skip=1; next }
    skip && $0=="---"  { skip=0; next }
    !skip { print }
  ' "$1"
}

has_frontmatter() {
  frontmatter "$1" | grep -Eq "^$2[[:space:]]*:"
}

has_body() {
  body "$1" | grep -Fq -- "$2"
}

spawns_value() {
  frontmatter "$1" | awk '/^spawns[[:space:]]*:[[:space:]]*/ {
    sub(/^spawns[[:space:]]*:[[:space:]]*/, ""); print; exit
  }'
}

model_value() {
  frontmatter "$1" | awk '/^model[[:space:]]*:[[:space:]]*/ {
    sub(/^model[[:space:]]*:[[:space:]]*/, ""); print; exit
  }'
}

# (b) Bundle source files exist.
for role in trio-lead trio-evaluator trio-scout trio-builder; do
  [[ -f "$OMP/agents/$role.md" ]] || fail "missing agent source $OMP/agents/$role.md"
done
for cmd in trio trio-init; do
  [[ -f "$OMP/commands/$cmd.md" ]] || fail "missing command source $OMP/commands/$cmd.md"
done
[[ -f "$OMP/configure-models.sh" ]] || fail "missing $OMP/configure-models.sh"
[[ -x "$OMP/configure-models.sh" ]] || fail "$OMP/configure-models.sh is not executable"
[[ -x "$OMP/smoke-test.sh" ]] || fail "$OMP/smoke-test.sh is not executable"

# (c) Frontmatter contract.
for role in trio-lead trio-evaluator trio-scout trio-builder; do
  file="$OMP/agents/$role.md"
  has_frontmatter "$file" name || fail "$role missing name:"
  has_frontmatter "$file" description || fail "$role missing description:"
done

has_frontmatter "$OMP/agents/trio-lead.md" spawns || fail "trio-lead missing spawns:"
lead_spawns="$(spawns_value "$OMP/agents/trio-lead.md")"
[[ "$lead_spawns" == *trio-builder* ]] || fail "trio-lead spawns: must include trio-builder"
[[ "$lead_spawns" == *trio-scout* ]] || fail "trio-lead spawns: must include trio-scout"

has_frontmatter "$OMP/agents/trio-evaluator.md" spawns || fail "trio-evaluator missing spawns:"
eval_spawns="$(spawns_value "$OMP/agents/trio-evaluator.md")"
[[ "$eval_spawns" == *trio-scout* ]] || fail "trio-evaluator spawns: must include trio-scout"

frontmatter "$OMP/agents/trio-lead.md" | grep -Eq '^blocking[[:space:]]*:[[:space:]]*true$' \
  || fail "trio-lead must set blocking: true"
frontmatter "$OMP/agents/trio-evaluator.md" | grep -Eq '^blocking[[:space:]]*:[[:space:]]*true$' \
  || fail "trio-evaluator must set blocking: true"
frontmatter "$OMP/agents/trio-scout.md" | grep -Eq '^read-summarize[[:space:]]*:[[:space:]]*false$' \
  || fail "trio-scout must set read-summarize: false"

# Model pins in checked-in frontmatter.
[[ "$(model_value "$OMP/agents/trio-lead.md")" == "kimi-code/kimi-for-coding" ]] \
  || fail "trio-lead model: must be kimi-code/kimi-for-coding"
[[ "$(model_value "$OMP/agents/trio-evaluator.md")" == "kimi-code/kimi-for-coding" ]] \
  || fail "trio-evaluator model: must be kimi-code/kimi-for-coding"
[[ "$(model_value "$OMP/agents/trio-scout.md")" == "deepseek/deepseek-v4-flash" ]] \
  || fail "trio-scout model: must be deepseek/deepseek-v4-flash"
[[ "$(model_value "$OMP/agents/trio-builder.md")" == "deepseek/deepseek-v4-flash" ]] \
  || fail "trio-builder model: must be deepseek/deepseek-v4-flash"

# (d) Commands reference the mailbox protocol files.
for cmd in trio trio-init; do
  file="$OMP/commands/$cmd.md"
  has_body "$file" GOAL.md || fail "$cmd body must reference GOAL.md"
  has_body "$file" VERDICT.md || fail "$cmd body must reference VERDICT.md"
done

# (e) Optional installed-bundle verification.
if [[ $INSTALLED -eq 1 ]]; then
  agent_dir="$(resolve_agent_dir)"
  echo "smoke-test: resolved omp agent dir: $agent_dir"
  for role in trio-lead trio-evaluator trio-scout trio-builder; do
    [[ -f "$agent_dir/agents/$role.md" ]] || fail "missing installed agent $agent_dir/agents/$role.md"
  done
  for cmd in trio trio-init; do
    [[ -f "$agent_dir/commands/$cmd.md" ]] || fail "missing installed command $agent_dir/commands/$cmd.md"
  done
  if [[ $OMP_AVAILABLE -eq 1 ]]; then
    echo "smoke-test: current task.agentModelOverrides:"
    omp config get task.agentModelOverrides --json || true
  else
    warn "cannot print task.agentModelOverrides because omp is not on PATH"
  fi
fi

echo ""
echo "omp Trio bundle source validation: PASS"
echo ""
echo "Manual verification steps:"
echo "  1. Start omp in any project."
echo "  2. Run /trio-init <goal> to create the mailbox."
echo "  3. Run /trio for one supervised iteration."
echo "  4. Run /trio auto to let the loop run until VERDICT.md reads SHIP or BLOCKED."

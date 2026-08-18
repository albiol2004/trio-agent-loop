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

# Validate that trio-evaluator.md has an output: field carrying parseable JSON Schema.
validate_output_schema() {
  local file="$1"
  python3 - "$file" <<'PY'
import json, re, sys
file = sys.argv[1]
with open(file) as f:
    text = f.read()
if not text.startswith('---\n'):
    sys.exit('no frontmatter')
parts = text.split('\n---\n', 1)
fm = parts[0][4:]
lines = fm.splitlines()
output = None
i = 0
while i < len(lines):
    line = lines[i]
    m = re.match(r'^([A-Za-z0-9_-]+)\s*:\s*(.*)$', line)
    if m:
        key, rest = m.group(1), m.group(2)
        if key == 'output':
            if rest and rest not in ('|', '>', '|-', '>-', '|+', '>+'):
                output = rest
                break
            i += 1
            block = []
            while i < len(lines):
                l = lines[i]
                if l == '' or l.startswith(' ') or l.startswith('\t'):
                    block.append(l)
                    i += 1
                elif re.match(r'^[A-Za-z0-9_-]+\s*:', l):
                    break
                else:
                    block.append(l)
                    i += 1
            output = '\n'.join(block)
            break
    i += 1
if output is None:
    sys.exit('missing output field')
out_lines = output.splitlines()
nonblank = [l for l in out_lines if l.strip()]
min_indent = min(len(l) - len(l.lstrip()) for l in nonblank) if nonblank else 0
dedented = '\n'.join(l[min_indent:] for l in out_lines)
first = dedented.lstrip().splitlines()[0] if dedented.strip() else ''
if first in ('|', '>', '|-', '>-', '|+', '>+'):
    dedented = '\n'.join(dedented.splitlines()[1:])
schema = json.loads(dedented.strip())
assert isinstance(schema, dict)
assert schema.get('type') == 'object'
assert 'verdict' in schema.get('required', [])
assert 'summary' in schema.get('required', [])
assert 'blocking_issues' in schema.get('properties', {})
print('output schema OK')
PY
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

# (b2) Optional helper script.
if [[ -f "$OMP/scripts/trio-log-usage.sh" ]]; then
  bash -n "$OMP/scripts/trio-log-usage.sh" || fail "trio-log-usage.sh has syntax errors"
  [[ -x "$OMP/scripts/trio-log-usage.sh" ]] || fail "trio-log-usage.sh is not executable"
fi

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

# Roles must NOT pin blocking: true — the loop dispatches them as background
# jobs with auto-delivery; blocking would freeze the orchestrator's turn.
frontmatter "$OMP/agents/trio-lead.md" | grep -Eq '^blocking[[:space:]]*:' \
  && fail "trio-lead must not set blocking: (async dispatch)"
frontmatter "$OMP/agents/trio-evaluator.md" | grep -Eq '^blocking[[:space:]]*:' \
  && fail "trio-evaluator must not set blocking: (async dispatch)"
grep -q 'background job' "$OMP/commands/trio.md" \
  || fail "trio.md must instruct background-job dispatch"
grep -q 'auto-delivers' "$OMP/commands/trio.md" \
  || fail "trio.md must document auto-delivery semantics"
frontmatter "$OMP/agents/trio-scout.md" | grep -Eq '^read-summarize[[:space:]]*:[[:space:]]*false$' \
  || fail "trio-scout must set read-summarize: false"

# Model pins in checked-in frontmatter.
[[ "$(model_value "$OMP/agents/trio-lead.md")" == "cursor/cursor-grok-4.6-high" ]] \
  || fail "trio-lead model: must be cursor/cursor-grok-4.6-high"
[[ "$(model_value "$OMP/agents/trio-evaluator.md")" == "cursor/cursor-grok-4.6-high" ]] \
  || fail "trio-evaluator model: must be cursor/cursor-grok-4.6-high"
[[ "$(model_value "$OMP/agents/trio-scout.md")" == "deepseek/deepseek-v4-flash" ]] \
  || fail "trio-scout model: must be deepseek/deepseek-v4-flash"
[[ "$(model_value "$OMP/agents/trio-builder.md")" == "deepseek/deepseek-v4-flash" ]] \
  || fail "trio-builder model: must be deepseek/deepseek-v4-flash"

# Output schema in evaluator frontmatter.
has_frontmatter "$OMP/agents/trio-evaluator.md" output || fail "trio-evaluator missing output:"
validate_output_schema "$OMP/agents/trio-evaluator.md" || fail "trio-evaluator output schema is not valid JSON"

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
echo "  4. Run /trio auto to let the loop run until VERDICT.md reads SHIP, BLOCKED, or NEEDS_HUMAN."

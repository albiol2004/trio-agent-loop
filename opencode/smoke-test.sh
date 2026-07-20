#!/usr/bin/env bash
# Credential-free contract test for the native OpenCode bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OC="$ROOT/opencode"

fail() { echo "smoke-test: $*" >&2; exit 1; }
has() { grep -Fq -- "$2" "$1" || fail "missing '$2' in $1"; }
not_has() { ! grep -Fq -- "$2" "$1" || fail "unexpected '$2' in $1"; }
task_has() {
  awk -v rule="$2" '
    /^permission:/ { in_permission = 1; next }
    in_permission && /^  task:/ { in_task = 1; next }
    in_task && /^  [^ ]/ { exit }
    in_task && index($0, rule) { found = 1; exit }
    END { exit found ? 0 : 1 }
  ' "$1" || fail "missing Task rule '$2' in $1"
}
no_task_wildcard_allow() {
  awk '
    /^permission:/ { in_permission = 1; next }
    in_permission && /^  task:/ { in_task = 1; next }
    in_task && /^  [^ ]/ { exit }
    in_task && /^[[:space:]]*"\*"[[:space:]]*:[[:space:]]*(allow|ask)/ { bad = 1 }
    END { exit bad ? 1 : 0 }
  ' "$1" || fail "arbitrary Task wildcard in $1"
}
permission_contract_valid() {
  awk -v role="$2" '
    BEGIN {
      if (role == "scout") {
        expected[1] = "\"*\": deny"
        expected[2] = "read: allow"
        expected[3] = "grep: allow"
        expected[4] = "glob: allow"
        expected[5] = "webfetch: allow"
        expected[6] = "edit: deny"
        expected[7] = "bash: deny"
        expected[8] = "task: deny"
        expected_count = 8
      } else if (role == "evaluator") {
        expected[1] = "\"*\": deny"
        expected[2] = "read: allow"
        expected[3] = "grep: allow"
        expected[4] = "glob: allow"
        expected[5] = "edit:"
        expected[6] = "bash:"
        expected[7] = "task:"
        expected_count = 7
      } else {
        exit 2
      }
    }
    NR == 1 && /^---$/ { in_frontmatter = 1; next }
    in_frontmatter && /^---$/ { in_frontmatter = 0; in_permission = 0; next }
    in_frontmatter && /^permission:/ {
      if (found_permission) duplicate_permission = 1
      found_permission = 1
      in_permission = 1
      next
    }
    in_frontmatter && in_permission && /^  [^ ]/ {
      line = $0
      sub(/^  /, "", line)
      seen++
      if (seen > expected_count || line != expected[seen]) mismatch = 1
      next
    }
    in_frontmatter && in_permission && /^[^ ]/ { in_permission = 0 }
    END {
      exit !(found_permission == 1 && !duplicate_permission && seen == expected_count && !mismatch)
    }
  ' "$1"
}
bash_contract_valid() {
  awk '
    BEGIN {
      expected[1] = "\"*\": deny"
      expected[2] = "\"bash -n install.sh portable/driver.sh opencode/smoke-test.sh\": allow"
      expected[3] = "\"./opencode/smoke-test.sh\": allow"
      expected[4] = "\"git diff --check\": allow"
      expected[5] = "\"git diff --cached --quiet\": allow"
      expected[6] = "\"git status --short\": allow"
      expected[7] = "\"find opencode -type f\": allow"
      expected[8] = "\"sort\": allow"
      expected_count = 8
    }
    NR == 1 && /^---$/ { in_frontmatter = 1; next }
    in_frontmatter && /^---$/ { in_frontmatter = 0; next }
    in_frontmatter && /^permission:/ { in_permission = 1; next }
    in_frontmatter && in_permission && /^  bash:/ {
      if (found_bash) {
        duplicate_bash = 1
        next
      }
      found_bash = 1
      in_bash = 1
      next
    }
    in_bash && /^  [^ ]/ { in_bash = 0 }
    in_bash && /^    / {
      line = $0
      sub(/^    /, "", line)
      seen++
      if (seen > expected_count || line != expected[seen]) mismatch = 1
      if (line ~ /: allow$/) {
        allow_key = line
        sub(/: allow$/, "", allow_key)
        sub(/^"/, "", allow_key)
        sub(/"$/, "", allow_key)
        if (allow_key ~ /[*?]/) wildcard_allow = 1
      }
    }
    END {
      exit !(found_bash == 1 && !duplicate_bash && seen == expected_count && !mismatch && !wildcard_allow)
    }
  ' "$1"
}

for role in trio-orchestrator trio-lead trio-scout trio-builder trio-evaluator; do
  [[ -f "$OC/agents/$role.md" ]] || fail "missing role $role"
done
for command in trio trio-init; do
  [[ -f "$OC/commands/$command.md" ]] || fail "missing command $command"
done
[[ -f "$OC/configure-models.sh" ]] || fail "missing parameterized model configurator"

has "$OC/agents/trio-orchestrator.md" 'mode: primary'
has "$OC/agents/trio-lead.md" 'mode: subagent'
has "$OC/agents/trio-scout.md" 'mode: subagent'
has "$OC/agents/trio-builder.md" 'mode: subagent'
has "$OC/agents/trio-evaluator.md" 'mode: subagent'

# Direct permission children default-deny unknown custom, MCP, plugin, and skill tools.
permission_contract_valid "$OC/agents/trio-scout.md" scout || fail "Scout direct permission contract is not exact"
permission_contract_valid "$OC/agents/trio-evaluator.md" evaluator || fail "Evaluator direct permission contract is not exact"

# Every allowed Task child is named; no task wildcard may silently widen it.
task_has "$OC/agents/trio-orchestrator.md" '"*": deny'
task_has "$OC/agents/trio-orchestrator.md" 'trio-scout: allow'
task_has "$OC/agents/trio-orchestrator.md" 'trio-lead: allow'
task_has "$OC/agents/trio-orchestrator.md" 'trio-evaluator: allow'
task_has "$OC/agents/trio-lead.md" '"*": deny'
task_has "$OC/agents/trio-lead.md" 'trio-builder: allow'
task_has "$OC/agents/trio-evaluator.md" '"*": deny'
task_has "$OC/agents/trio-evaluator.md" 'trio-scout: allow'
has "$OC/agents/trio-scout.md" 'task: deny'
has "$OC/agents/trio-builder.md" 'task: deny'
for role_file in "$OC"/agents/*.md; do
  no_task_wildcard_allow "$role_file"
done
has "$OC/agents/trio-evaluator.md" '"loop/VERDICT.md": allow'
has "$OC/agents/trio-evaluator.md" '"loop/LOG.md": allow'
bash_contract_valid "$OC/agents/trio-evaluator.md" || fail "Evaluator Bash permission block is not the exact ordered contract"
has "$OC/agents/trio-scout.md" 'edit: deny'
has "$OC/agents/trio-scout.md" 'bash: deny'

# Prove the Bash parser rejects both missing capability and widened access.
tmp_contract="$(mktemp -d "${TMPDIR:-/tmp}/trio-opencode-contract.XXXXXX")"
trap 'rm -rf "$tmp_contract"' EXIT
awk '$0 != "  \"*\": deny"' \
  "$OC/agents/trio-scout.md" > "$tmp_contract/missing-tool-baseline.md"
if permission_contract_valid "$tmp_contract/missing-tool-baseline.md" scout; then
  fail "permission parser accepted a missing top-level deny baseline"
fi
awk '$0 != "  grep: allow"' \
  "$OC/agents/trio-scout.md" > "$tmp_contract/missing-tool-allow.md"
if permission_contract_valid "$tmp_contract/missing-tool-allow.md" scout; then
  fail "permission parser accepted a missing required built-in allow"
fi
awk '
  { print }
  $0 == "  glob: allow" { print "  custom_writer: allow" }
' "$OC/agents/trio-evaluator.md" > "$tmp_contract/extra-tool-allow.md"
if permission_contract_valid "$tmp_contract/extra-tool-allow.md" evaluator; then
  fail "permission parser accepted an unknown custom tool allow"
fi
awk '$0 != "    \"git diff --check\": allow"' \
  "$OC/agents/trio-evaluator.md" > "$tmp_contract/missing-rule.md"
if bash_contract_valid "$tmp_contract/missing-rule.md"; then
  fail "Evaluator Bash parser accepted a missing required allow rule"
fi
awk '
  $0 == "    \"git status --short\": allow" {
    print "    \"git status *\": allow"
    next
  }
  { print }
' "$OC/agents/trio-evaluator.md" > "$tmp_contract/widened-rule.md"
if bash_contract_valid "$tmp_contract/widened-rule.md"; then
  fail "Evaluator Bash parser accepted an extra or widened allow rule"
fi

# Protocol and provenance safeguards must remain in the checked-in prompts.
for phrase in 'trio-scout' 'trio-lead' 'trio-builder' 'trio-evaluator' \
  'mandatory primary Builder' 'Lead review/correction' 'independent Evaluator' \
  'loop/PLAN.md' 'loop/REPORT.md' 'loop/VERDICT.md' 'loop/LOG.md' \
  'never repairs product code'; do
  has "$OC/agents/trio-orchestrator.md" "$phrase"
done
has "$OC/agents/trio-lead.md" 'MUST delegate the primary'
has "$OC/agents/trio-builder.md" 'Never touch `loop/`'
has "$OC/agents/trio-evaluator.md" 'forbidden to repair'
has "$OC/agents/trio-evaluator.md" 'built-in read-only `grep`, `glob`, and'
has "$OC/agents/trio-evaluator.md" 'inspect that script with'
has "$OC/agents/trio-evaluator.md" 'Every other Bash'

for command in trio trio-init; do
  has "$OC/commands/$command.md" 'agent: trio-orchestrator'
  has "$OC/commands/$command.md" 'subtask: true'
done

# Checked-in agents inherit a session model. The example has only commented
# placeholders, so this test never requires a provider or a paid default.
for role_file in "$OC"/agents/*.md; do
  if grep -Eq '^[[:space:]]*model[[:space:]]*:' "$role_file"; then
    fail "role pins a default model: $role_file"
  fi
done
example="$OC/opencode.trio.example.jsonc"
[[ -f "$example" ]] || fail "missing model configuration example"
for placeholder in \
  '<strong-provider/model-from-opencode-models>' \
  '<lighter-provider/model-from-opencode-models>'; do
  has "$example" "$placeholder"
done
has "$example" "subagents inherit their invoking primary's"
if grep -Eq '^[[:space:]]*"[^"]+"[[:space:]]*:[[:space:]]*\{[[:space:]]*"model"' "$example"; then
  fail "model example contains an uncommented model mapping"
fi

# Require the current direct source URL and reject the obsolete redirect.
has "$ROOT/SETUP-BY-OPENCODE.md" 'https://github.com/anomalyco/opencode'
obsolete_source='https://github.com/'"sst"'/opencode'
if grep -Fq "$obsolete_source" "$ROOT/SETUP-BY-OPENCODE.md"; then
  fail "setup guide contains obsolete OpenCode source URL"
fi
for reference in \
  'https://opencode.ai/docs/tools/' \
  'https://opencode.ai/docs/plugins/' \
  'https://opencode.ai/docs/mcp-servers/' \
  'https://opencode.ai/docs/skills/' \
  'https://opencode.ai/docs/custom-tools/'; do
  has "$ROOT/SETUP-BY-OPENCODE.md" "$reference"
done
has "$ROOT/SETUP-BY-OPENCODE.md" 'Trio installs native agents and commands only'
has "$ROOT/SETUP-BY-OPENCODE.md" 'OpenCode enables tools by default'
has "$ROOT/SETUP-BY-OPENCODE.md" 'plugins automatically'
has "$ROOT/SETUP-BY-OPENCODE.md" 'loads skills through the `skill` tool'
has "$ROOT/SETUP-BY-OPENCODE.md" 'sandbox plugin hook code'
has "$ROOT/SETUP-BY-OPENCODE.md" 'collide with an allowed built-in name'

# Exercise the installer in an isolated HOME without calling OpenCode.
tmp_home="$(mktemp -d "${TMPDIR:-/tmp}/trio-opencode-smoke.XXXXXX")"
tmp_xdg="$(mktemp -d "${TMPDIR:-/tmp}/trio-opencode-xdg.XXXXXX")"
tmp_models="$(mktemp -d "${TMPDIR:-/tmp}/trio-opencode-models.XXXXXX")"
trap 'rm -rf "$tmp_contract" "$tmp_home" "$tmp_xdg" "$tmp_models"' EXIT
mkdir -p "$tmp_home/.config/opencode/agents"
printf '%s\n' '{"sentinel":true}' > "$tmp_home/.config/opencode/opencode.json"
printf '%s\n' 'user-owned lead agent' > "$tmp_home/.config/opencode/agents/trio-lead.md"
before="$(<"$tmp_home/.config/opencode/opencode.json")"
env -u XDG_CONFIG_HOME HOME="$tmp_home" "$ROOT/install.sh" --opencode >/dev/null
dest="$tmp_home/.config/opencode"
for role in trio-orchestrator trio-lead trio-scout trio-builder trio-evaluator; do
  [[ -f "$dest/agents/$role.md" ]] || fail "installer missed $role"
done
for command in trio trio-init; do
  [[ -f "$dest/commands/$command.md" ]] || fail "installer missed command $command"
done
[[ -f "$dest/opencode.trio.example.jsonc" ]] || fail "installer missed config example"
[[ "$(<"$tmp_home/.config/opencode/opencode.json")" == "$before" ]] || fail "installer overwrote user config"
[[ "$(<"$dest/agents/trio-lead.md")" == 'user-owned lead agent' ]] || fail "installer overwrote user agent"
for forbidden in .claude .codex .kimi-code .agents; do
  [[ ! -e "$tmp_home/$forbidden" ]] || fail "installer created unrelated $forbidden"
done

# XDG_CONFIG_HOME takes precedence over HOME for the documented global tree.
env HOME="$tmp_home" XDG_CONFIG_HOME="$tmp_xdg" "$ROOT/install.sh" --opencode >/dev/null
[[ -f "$tmp_xdg/opencode/agents/trio-orchestrator.md" ]] || fail "installer ignored XDG_CONFIG_HOME"
[[ ! -e "$tmp_xdg/.claude" && ! -e "$tmp_xdg/.codex" && ! -e "$tmp_xdg/.kimi-code" ]] || fail "XDG install created unrelated harness files"

# User-supplied model parameters map strong judgment roles and light worker
# roles without modifying the checked-in provider-neutral templates.
env -u XDG_CONFIG_HOME HOME="$tmp_models" "$ROOT/install.sh" --opencode \
  --strong-model test-provider/strong-v1 \
  --light-model test-provider/light-v1 >/dev/null
model_dest="$tmp_models/.config/opencode/agents"
for role in trio-orchestrator trio-lead trio-evaluator; do
  has "$model_dest/$role.md" 'model: test-provider/strong-v1'
  not_has "$model_dest/$role.md" 'model: test-provider/light-v1'
done
for role in trio-scout trio-builder; do
  has "$model_dest/$role.md" 'model: test-provider/light-v1'
  not_has "$model_dest/$role.md" 'model: test-provider/strong-v1'
done

# Reconfiguration must use the next user-supplied values, not retain or guess
# the original selections.
env -u XDG_CONFIG_HOME HOME="$tmp_models" bash "$OC/configure-models.sh" --global \
  --strong-model another-provider/strong-v2 \
  --light-model another-provider/light-v2 >/dev/null
for role in trio-orchestrator trio-lead trio-evaluator; do
  has "$model_dest/$role.md" 'model: another-provider/strong-v2'
  not_has "$model_dest/$role.md" 'model: test-provider/strong-v1'
done
for role in trio-scout trio-builder; do
  has "$model_dest/$role.md" 'model: another-provider/light-v2'
  not_has "$model_dest/$role.md" 'model: test-provider/light-v1'
done

if env -u XDG_CONFIG_HOME HOME="$tmp_models" "$ROOT/install.sh" --opencode \
  --strong-model test-provider/strong-only >/dev/null 2>&1; then
  fail "installer accepted only one model tier"
fi

project_models="$tmp_models/project"
mkdir -p "$project_models/.opencode/agents"
cp "$OC"/agents/*.md "$project_models/.opencode/agents/"
bash "$OC/configure-models.sh" --project "$project_models" \
  --strong-model project-provider/strong \
  --light-model project-provider/light >/dev/null
has "$project_models/.opencode/agents/trio-lead.md" 'model: project-provider/strong'
has "$project_models/.opencode/agents/trio-builder.md" 'model: project-provider/light'

if bash "$OC/configure-models.sh" --project "$project_models" \
  --strong-model invalid-without-provider \
  --light-model project-provider/light >/dev/null 2>&1; then
  fail "model configurator accepted an invalid model ID"
fi

echo "OpenCode native bundle smoke test: PASS"

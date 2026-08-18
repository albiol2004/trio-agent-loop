#!/usr/bin/env bash
# Credential-free, offline contract test for the Omnigent bundle.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
pass() { echo "PASS: $1"; }
skip() { echo "SKIP: $1"; }
fail() { echo "FAIL: $1"; FAILURES=$((FAILURES + 1)); }
check() {
  local name="$1"
  shift
  if "$@"; then pass "$name"; else fail "$name"; fi
}

# `check` runs each contract inside an `if`, which disables `set -e` for the
# whole call. Assertions must therefore return explicitly: a bare test would
# let a failing contract keep running and report the last command's status.
assert() { "$@" || return 1; }

REAL_PATH="$PATH"
REAL_OMNIGENT_BIN="$(command -v omnigent 2>/dev/null || true)"
REAL_OMNIGENT_PYTHON=""
if [[ -n "$REAL_OMNIGENT_BIN" ]]; then
  REAL_OMNIGENT_PYTHON="$(head -n 1 "$REAL_OMNIGENT_BIN" | sed 's/^#!//')"
fi

make_probe() {
  local mode="$1"
  cat >"$TMP/probe-interpreter" <<EOF
#!/usr/bin/env bash
set -euo pipefail
[[ "\${1:-}" == "-c" ]] || exit 1
case "$mode" in
  create-fail) [[ "\${2:-}" != *"SysSessionCreateTool"* ]] ;;
  registered-launch-fail) [[ "\${2:-}" != *"_resolve_agent_spec"* ]] ;;
  pass) exit 0 ;;
esac
EOF
  chmod +x "$TMP/probe-interpreter"
  cat >"$TMP/bin/omnigent" <<EOF
#!$TMP/probe-interpreter
EOF
  chmod +x "$TMP/bin/omnigent"
  cat >"$TMP/bin/cursor-agent" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "models" ]]; then
  echo 'cursor-grok-4.6-medium - Cursor Grok 4.6 Medium'
  echo 'gpt-5.6-luna-max - GPT-5.6 Luna 1M Max'
fi
exit 0
EOF
  chmod +x "$TMP/bin/cursor-agent"
}

installer_probe_contract() {
  local stderr_file
  mkdir -p "$TMP/bin" "$TMP/home"
  make_probe pass
  assert env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$TMP/home" OMNIGENT_HOME="$TMP/home/.omnigent" \
    "$ROOT/install.sh" --omnigent >/dev/null 2>"$TMP/probe-pass.err"

  make_probe create-fail
  stderr_file="$TMP/create-fail.err"
  if env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$TMP/home" OMNIGENT_HOME="$TMP/home/.omnigent" \
    "$ROOT/install.sh" --omnigent >/dev/null 2>"$stderr_file"; then return 1; fi
  grep -Fq 'sys_session_create.reasoning_effort' "$stderr_file" || return 1
  grep -Fq 'OMNIGENT_SOURCE' "$stderr_file" || return 1

  make_probe registered-launch-fail
  stderr_file="$TMP/registered-launch-fail.err"
  if env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$TMP/home" OMNIGENT_HOME="$TMP/home/.omnigent" \
    "$ROOT/install.sh" --omnigent >/dev/null 2>"$stderr_file"; then return 1; fi
  grep -Fq 'native permission flags' "$stderr_file" || return 1
  grep -Fq 'OMNIGENT_SOURCE' "$stderr_file" || return 1
}

installer_replacement_contract() {
  local home="$TMP/install-home"
  local omnigent_home="$home/.omnigent"
  local roles="$omnigent_home/agents/trio-omnigent-roles"
  local claude_skill="$home/.claude/skills/trio-omnigent"
  local codex_skill="$home/.agents/skills/trio-omnigent"
  local trioctl="$home/.local/bin/trioctl"
  local trioctl_config="$home/.config/trio-agent-loop/omnigent.toml"
  local snapshot="$TMP/install-snapshot"
  mkdir -p "$TMP/bin" "$home"
  make_probe pass
  assert env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$home" OMNIGENT_HOME="$omnigent_home" \
    "$ROOT/install.sh" --omnigent >/dev/null
  assert cp -a "$roles" "$snapshot"
  assert env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$home" OMNIGENT_HOME="$omnigent_home" \
    "$ROOT/install.sh" --omnigent >/dev/null
  assert diff -r "$snapshot" "$roles" >/dev/null

  printf 'stale\n' >"$claude_skill/stale.txt"
  printf '{"preserve":true}\n' >"$roles/registry.json"
  assert env -u OMNIGENT_SOURCE PATH="$TMP/bin:$REAL_PATH" HOME="$home" OMNIGENT_HOME="$omnigent_home" \
    "$ROOT/install.sh" --omnigent >/dev/null
  [[ ! -e "$claude_skill/stale.txt" ]] || return 1
  [[ "$(<"$roles/registry.json")" == '{"preserve":true}' ]] || return 1
  [[ "$(find "$roles" -type f -name config.yaml | wc -l)" -eq 4 ]] || return 1
  [[ -f "$claude_skill/SKILL.md" && -f "$codex_skill/SKILL.md" ]] || return 1
  [[ -x "$trioctl" && -f "$trioctl_config" ]] || return 1
  assert "$trioctl" omnigent resolve lead --config "$trioctl_config" --json \
    | grep -Fq '"model": "cursor-grok-4.6-medium"'
  assert "$trioctl" omnigent resolve lead --config "$trioctl_config" --json \
    | grep -Fq '"model_effort": "medium"'
  [[ -z "$(find "$claude_skill" "$codex_skill" -type d -name scripts -print)" ]] || return 1
  mkdir -p "$TMP/expected-roles"
  assert cp -a "$ROOT/omnigent/trio-omnigent-roles/." "$TMP/expected-roles/"
  assert cp "$roles/registry.json" "$TMP/expected-roles/registry.json"
  assert diff -r "$TMP/expected-roles" "$roles" >/dev/null
}

role_yaml_contract() {
  [[ -n "$REAL_OMNIGENT_PYTHON" && -x "$REAL_OMNIGENT_PYTHON" ]] || return 1
  assert "$REAL_OMNIGENT_PYTHON" - "$ROOT" <<'PY'
import pathlib, sys, yaml

root = pathlib.Path(sys.argv[1])
expected = {
    "lead": ("trio-omnigent-lead", "cursor-native", "cursor-grok-4.6-medium"),
    "evaluator": ("trio-omnigent-evaluator", "cursor-native", "cursor-grok-4.6-medium"),
    "builder": ("trio-omnigent-builder", "cursor-native", "gpt-5.6-luna-max"),
    "scout": ("trio-omnigent-scout", "cursor-native", "gpt-5.6-luna-max"),
}
for role, (name, harness, model) in expected.items():
    data = yaml.safe_load((root / "omnigent/trio-omnigent-roles" / role / "config.yaml").read_text())
    executor = data["executor"]
    config = executor["config"]
    assert (data["name"], config["harness"], executor["model"]) == (name, harness, model)
    if role in {"lead", "evaluator"}:
        assert data["spawn"] is True
        assert config["yolo"] is True
    else:
        assert not data.get("spawn", False)
        assert config["yolo"] is True
PY
}

check 'A3 installer probes session-create effort and registered-agent launch support' installer_probe_contract
check 'A4 installer replaces owned trees and preserves registry' installer_replacement_contract
check 'A6 role configs parse as YAML and match the documented role table' role_yaml_contract
check 'A7 trioctl unit tests pass' python3 -m pytest -q "$ROOT/omnigent/tests/test_trioctl.py"

if [[ -n "${OMNIGENT_SOURCE:-}" ]]; then
  if git -C "$OMNIGENT_SOURCE" apply --reverse --check \
    "$ROOT/omnigent/patches/child-reasoning-effort.patch" >/dev/null 2>&1; then
    pass 'A1 patch reverse-applies against OMNIGENT_SOURCE'
  else
    fail 'A1 patch reverse-applies against OMNIGENT_SOURCE'
  fi
else
  skip 'A1 patch reverse-apply (OMNIGENT_SOURCE is unset)'
fi

if (( FAILURES > 0 )); then
  exit 1
fi

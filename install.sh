#!/usr/bin/env bash
# Install the trio agent-loop template.
#   ./install.sh --global          -> ~/.claude  (available in EVERY project; recommended)
#   ./install.sh /path/to/project  -> <project>/.claude (committed with that repo)
#   ./install.sh --codex           -> Codex skills + custom agents + fallback
#   ./install.sh --omnigent        -> isolated mixed Claude/Cursor Omnigent agent
#   ./install.sh --kimi            -> Kimi Code skills + sequential fallback
#   ./install.sh --zcode           -> native ZCode skills
#   ./install.sh --pi              -> native Pi AgentSession extension
#   ./install.sh --opencode [--strong-model provider/model --light-model provider/model]
#                                      -> native OpenCode agents + commands
#   ./install.sh --omp [--strong-model provider/model --light-model provider/model]
#                                      -> native Oh My Pi agents + commands
#   ./install.sh --bridge              -> install the trio-bridge suggestion skill for Claude, Codex, and Omp
#   ./install.sh --dashboard           -> trio-dash per-project loop dashboard (ports 9470-9479, tailscale-ready)
#   ./install.sh --portable [dir]  -> legacy driver for other harnesses
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/.claude"

case "${1:-}" in
  --global) DEST="$HOME/.claude" ;;
  --codex)
    mkdir -p "$HOME/.agents/skills" "$HOME/.codex/agents"
    cp -rv "$ROOT/codex/skills/trio" "$HOME/.agents/skills/"
    cp -rv "$ROOT/codex/skills/trio-init" "$HOME/.agents/skills/"
    cp -v "$ROOT"/codex/agents/trio-*.toml "$HOME/.codex/agents/"
    chmod +x "$HOME/.agents/skills/trio/scripts/run-role.sh"
    echo "Installed Codex Trio. Native agents are preferred; isolated Codex CLI sessions are the fallback."
    echo "Next: follow SETUP-BY-CODEX.md to validate multi_agent and the target project's permission profile."
    exit 0 ;;
  --omnigent)
    if [[ -n "${OMNIGENT_SOURCE:-}" ]]; then
      [[ -d "$OMNIGENT_SOURCE/.git" ]] || {
        echo "OMNIGENT_SOURCE must point to an Omnigent git checkout." >&2
        exit 2
      }
      OMNIGENT_PATCH="$ROOT/omnigent/patches/child-reasoning-effort.patch"
      if git -C "$OMNIGENT_SOURCE" apply --reverse --check "$OMNIGENT_PATCH" >/dev/null 2>&1; then
        echo "Omnigent child reasoning-effort patch is already applied."
      elif git -C "$OMNIGENT_SOURCE" apply --check "$OMNIGENT_PATCH"; then
        git -C "$OMNIGENT_SOURCE" apply "$OMNIGENT_PATCH"
        echo "Applied Omnigent child reasoning-effort patch."
      else
        echo "The bundled Omnigent patch does not apply cleanly to $OMNIGENT_SOURCE." >&2
        echo "Inspect its existing child reasoning-effort support before continuing." >&2
        exit 1
      fi
      command -v uv >/dev/null 2>&1 || {
        echo "uv is required to install the patched Omnigent checkout." >&2
        exit 1
      }
      uv tool install --force --python 3.12 --editable "$OMNIGENT_SOURCE"
    fi
    command -v omnigent >/dev/null 2>&1 || {
      echo "omnigent is not installed or is not on PATH." >&2
      exit 1
    }
    command -v cursor-agent >/dev/null 2>&1 || {
      echo "cursor-agent is not installed or is not on PATH." >&2
      exit 1
    }
    OMNIGENT_BIN="$(command -v omnigent)"
    OMNIGENT_TOOL_PYTHON="$(head -n 1 "$OMNIGENT_BIN" | sed 's/^#!//')"
    if [[ ! -x "$OMNIGENT_TOOL_PYTHON" ]] || ! "$OMNIGENT_TOOL_PYTHON" -c '
from omnigent.tools.builtins.spawn import SysSessionCreateTool
schema = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]
raise SystemExit(0 if "reasoning_effort" in schema else 1)
'; then
      echo "The active Omnigent installation lacks sys_session_create.reasoning_effort." >&2
      echo "Set OMNIGENT_SOURCE=/path/to/omnigent and rerun this installer." >&2
      exit 1
    fi
    if ! "$OMNIGENT_TOOL_PYTHON" -c '
from omnigent.server.routes.sessions import _resolve_agent_spec
raise SystemExit(0 if callable(_resolve_agent_spec) else 1)
'; then
      echo "The active Omnigent installation drops native permission flags on registered-agent launches." >&2
      echo "Set OMNIGENT_SOURCE=/path/to/omnigent and rerun this installer." >&2
      exit 1
    fi
    OMNIGENT_ROLES_DEST="${OMNIGENT_HOME:-$HOME/.omnigent}/agents/trio-omnigent-roles"
    mkdir -p "$OMNIGENT_ROLES_DEST"
    for role in lead evaluator builder scout; do
      rm -rf "$OMNIGENT_ROLES_DEST/$role"
      cp -r "$ROOT/omnigent/trio-omnigent-roles/$role" "$OMNIGENT_ROLES_DEST/"
    done
    CLAUDE_OMNIGENT_SKILL="$HOME/.claude/skills/trio-omnigent"
    CODEX_OMNIGENT_SKILL="$HOME/.agents/skills/trio-omnigent"
    TRIOCTL_BIN_DIR="${TRIOCTL_BIN_DIR:-$HOME/.local/bin}"
    TRIOCTL_CONFIG="${TRIOCTL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/trio-agent-loop/omnigent.toml}"
    rm -rf "$CLAUDE_OMNIGENT_SKILL" "$CODEX_OMNIGENT_SKILL"
    mkdir -p "$HOME/.claude/skills" "$HOME/.agents/skills" "$TRIOCTL_BIN_DIR"
    cp -r "$ROOT/omnigent/entrypoints/trio-omnigent/." "$CLAUDE_OMNIGENT_SKILL/"
    cp -r "$ROOT/omnigent/entrypoints/trio-omnigent/." "$CODEX_OMNIGENT_SKILL/"
    cp "$ROOT/omnigent/trioctl" "$TRIOCTL_BIN_DIR/trioctl"
    cp "$ROOT/omnigent/trioctl.example.toml" "$TRIOCTL_BIN_DIR/trioctl.example.toml"
    chmod +x "$TRIOCTL_BIN_DIR/trioctl"
    "$TRIOCTL_BIN_DIR/trioctl" omnigent configure --config "$TRIOCTL_CONFIG"
    echo "Installed Omnigent Trio role configs at $OMNIGENT_ROLES_DEST."
    echo "Installed current-session trio-omnigent entrypoints for Claude Code and Codex."
    echo "Installed trioctl at $TRIOCTL_BIN_DIR/trioctl."
    echo "Omnigent Trio profile: $TRIOCTL_CONFIG"
    case ":$PATH:" in
      *":$TRIOCTL_BIN_DIR:"*) ;;
      *) echo "Add $TRIOCTL_BIN_DIR to PATH before starting Claude, Codex, or Omnigent." ;;
    esac
    echo "One-time user action: run 'claude --permission-mode bypassPermissions', accept option 2, then exit."
    echo "Next: from this cloned repo, register only Lead and Evaluator with sys_session_create."
    exit 0 ;;
  --kimi)
    KIMI_HOME="${KIMI_CODE_HOME:-$HOME/.kimi-code}"
    mkdir -p "$KIMI_HOME/skills"
    cp -rv "$ROOT/kimi/skills/trio" "$KIMI_HOME/skills/"
    cp -rv "$ROOT/kimi/skills/trio-init" "$KIMI_HOME/skills/"
    chmod +x "$KIMI_HOME/skills/trio/scripts/run-role.sh"
    echo "Installed Kimi Code Trio skills and sequential role runner."
    echo "Next: follow SETUP-BY-KIMI.md to validate Kimi Code and initialize a mailbox."
    exit 0 ;;
  --zcode)
    mkdir -p "$HOME/.zcode/skills"
    cp -rv "$ROOT/zcode/skills/trio" "$HOME/.zcode/skills/"
    cp -rv "$ROOT/zcode/skills/trio-init" "$HOME/.zcode/skills/"
    echo "Installed native ZCode Trio skills. Refresh Settings -> Skills."
    exit 0 ;;
  --pi)
    mkdir -p "$HOME/.pi/agent/extensions"
    cp -v "$ROOT/pi/extensions/trio.ts" "$HOME/.pi/agent/extensions/trio.ts"
    echo "Installed native Pi Trio extension. Run /reload, then /trio <goal>."
    exit 0 ;;
  --opencode)
    shift
    OPENCODE_STRONG_MODEL=""
    OPENCODE_LIGHT_MODEL=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --strong-model)
          [[ $# -gt 1 ]] || { echo "--strong-model requires provider/model" >&2; exit 2; }
          OPENCODE_STRONG_MODEL="$2"
          shift 2 ;;
        --light-model)
          [[ $# -gt 1 ]] || { echo "--light-model requires provider/model" >&2; exit 2; }
          OPENCODE_LIGHT_MODEL="$2"
          shift 2 ;;
        *) echo "unknown --opencode option: $1" >&2; exit 2 ;;
      esac
    done
    if [[ -n "$OPENCODE_STRONG_MODEL" || -n "$OPENCODE_LIGHT_MODEL" ]]; then
      [[ -n "$OPENCODE_STRONG_MODEL" && -n "$OPENCODE_LIGHT_MODEL" ]] || {
        echo "Specify both --strong-model and --light-model, or neither to use OpenCode inheritance." >&2
        exit 2
      }
    fi
    # OpenCode's global project-independent tree. Copy only Trio-owned files;
    # an existing user config or same-named file is never overwritten.
    OPENCODE_DEST="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
    mkdir -p "$OPENCODE_DEST/agents" "$OPENCODE_DEST/commands"
    for f in "$ROOT"/opencode/agents/*.md; do
      target="$OPENCODE_DEST/agents/$(basename "$f")"
      if [[ -e "$target" ]]; then
        echo "Preserving existing $target"
      else
        cp -v "$f" "$target"
      fi
    done
    for f in "$ROOT"/opencode/commands/*.md; do
      target="$OPENCODE_DEST/commands/$(basename "$f")"
      if [[ -e "$target" ]]; then
        echo "Preserving existing $target"
      else
        cp -v "$f" "$target"
      fi
    done
    target="$OPENCODE_DEST/opencode.trio.example.jsonc"
    if [[ -e "$target" ]]; then
      echo "Preserving existing $target"
    else
      cp -v "$ROOT/opencode/opencode.trio.example.jsonc" "$target"
    fi
    if [[ -n "$OPENCODE_STRONG_MODEL" ]]; then
      bash "$ROOT/opencode/configure-models.sh" \
        --config-dir "$OPENCODE_DEST" \
        --strong-model "$OPENCODE_STRONG_MODEL" \
        --light-model "$OPENCODE_LIGHT_MODEL"
    fi
    echo "Installed native OpenCode Trio agents, commands, and an optional model example."
    if [[ -n "$OPENCODE_STRONG_MODEL" ]]; then
      echo "Applied the user-selected strong/light model mapping."
    else
      echo "No models selected; Trio agents use OpenCode's documented inheritance."
    fi
    echo "Next: follow SETUP-BY-OPENCODE.md and validate the installed role mappings."
    exit 0 ;;
  --omp)
    shift
    OMP_STRONG_MODEL=""
    OMP_LIGHT_MODEL=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --strong-model)
          [[ $# -gt 1 ]] || { echo "--strong-model requires provider/model" >&2; exit 2; }
          OMP_STRONG_MODEL="$2"
          shift 2 ;;
        --light-model)
          [[ $# -gt 1 ]] || { echo "--light-model requires provider/model" >&2; exit 2; }
          OMP_LIGHT_MODEL="$2"
          shift 2 ;;
        *) echo "unknown --omp option: $1" >&2; exit 2 ;;
      esac
    done
    if [[ -n "$OMP_STRONG_MODEL" || -n "$OMP_LIGHT_MODEL" ]]; then
      [[ -n "$OMP_STRONG_MODEL" && -n "$OMP_LIGHT_MODEL" ]] || {
        echo "Specify both --strong-model and --light-model, or neither to keep the bundled frontmatter model pins." >&2
        exit 2
      }
    fi
    if command -v omp >/dev/null 2>&1; then
      OMP_DEST="$(omp config path)"
    else
      OMP_DEST="${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
    fi
    mkdir -p "$OMP_DEST/agents" "$OMP_DEST/commands"
    for f in "$ROOT"/omp/agents/*.md; do
      target="$OMP_DEST/agents/$(basename "$f")"
      if [[ -e "$target" ]]; then
        echo "Preserving existing $target"
      else
        cp -v "$f" "$target"
      fi
    done
    for f in "$ROOT"/omp/commands/*.md; do
      target="$OMP_DEST/commands/$(basename "$f")"
      if [[ -e "$target" ]]; then
        echo "Preserving existing $target"
      else
        cp -v "$f" "$target"
      fi
    done
    if [[ -n "$OMP_STRONG_MODEL" ]]; then
      bash "$ROOT/omp/configure-models.sh" \
        --agent-dir "$OMP_DEST" \
        --strong-model "$OMP_STRONG_MODEL" \
        --light-model "$OMP_LIGHT_MODEL"
    fi
    echo "Installed native Oh My Pi Trio agents and commands under $OMP_DEST."
    if [[ -n "$OMP_STRONG_MODEL" ]]; then
      echo "Applied strong/light model overrides via task.agentModelOverrides."
    else
      echo "No overrides supplied; agents use their bundled frontmatter model pins."
    fi
    echo "Smoke-test script (repo-side): $ROOT/omp/smoke-test.sh"
    echo "Next: follow SETUP-BY-OMP.md and validate the installed role mappings."
    exit 0 ;;
  --bridge)
    shift
    BRIDGE_STRONG_MODEL=""
    BRIDGE_LIGHT_MODEL=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --strong-model)
          [[ $# -gt 1 ]] || { echo "--strong-model requires provider/model" >&2; exit 2; }
          BRIDGE_STRONG_MODEL="$2"
          shift 2 ;;
        --light-model)
          [[ $# -gt 1 ]] || { echo "--light-model requires provider/model" >&2; exit 2; }
          BRIDGE_LIGHT_MODEL="$2"
          shift 2 ;;
        *) echo "unknown --bridge option: $1" >&2; exit 2 ;;
      esac
    done
    if [[ -n "$BRIDGE_STRONG_MODEL" || -n "$BRIDGE_LIGHT_MODEL" ]]; then
      echo "note: --strong-model/--light-model are unused for the prompt-only trio-bridge skill."
    fi
    # trio-bridge is a prompt-only suggestion skill; copy it to each harness
    # tree, preserving any existing copy. Claude and Omnigent share ~/.claude,
    # Codex uses ~/.agents, and Omp gets the native command form.
    if [[ -e "$HOME/.claude/skills/trio-bridge" ]]; then
      echo "Preserving existing $HOME/.claude/skills/trio-bridge"
    else
      mkdir -p "$HOME/.claude/skills"
      cp -rv "$ROOT/bridge/skills/trio-bridge" "$HOME/.claude/skills/"
    fi
    if [[ -e "$HOME/.agents/skills/trio-bridge" ]]; then
      echo "Preserving existing $HOME/.agents/skills/trio-bridge"
    else
      mkdir -p "$HOME/.agents/skills"
      cp -rv "$ROOT/bridge/skills/trio-bridge" "$HOME/.agents/skills/"
    fi
    if command -v omp >/dev/null 2>&1; then
      OMP_DEST="$(omp config path)"
    else
      OMP_DEST="${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}"
    fi
    mkdir -p "$OMP_DEST/commands"
    target="$OMP_DEST/commands/trio-bridge.md"
    if [[ -e "$target" ]]; then
      echo "Preserving existing $target"
    else
      cp -v "$ROOT/bridge/commands/trio-bridge.md" "$target"
    fi
    echo "Installed trio-bridge suggestion skill for Claude, Codex, and Omp."
    echo "Next: run /trio-bridge in a project to scan for background work."
    exit 0 ;;
  --dashboard)
    DASH_SHARE="${TRIO_DASH_HOME:-$HOME/.local/share/trio-agent-loop/dashboard}"
    DASH_BIN="$HOME/.local/bin"
    mkdir -p "$DASH_SHARE" "$DASH_BIN" "$(dirname "$DASH_SHARE")/metrics"
    # Shipped code: overwrite our own files on reinstall (not user config).
    cp -v "$ROOT/dashboard/serve.py" "$ROOT/dashboard/app.css" \
          "$ROOT/dashboard/app.js" "$ROOT/dashboard/index.html" \
          "$ROOT/dashboard/README.md" "$DASH_SHARE/"
    cp -v "$ROOT/metrics/trio-metrics.py" "$(dirname "$DASH_SHARE")/metrics/"
    cp -v "$ROOT/dashboard/trio-dash" "$DASH_BIN/trio-dash"
    chmod +x "$DASH_BIN/trio-dash"
    echo "Installed trio-dash. From any project root (terminal or agent): trio-dash"
    echo "Binds ${TRIO_DASH_HOST:-0.0.0.0}, first free port in ${TRIO_DASH_PORTS:-9470-9479}."
    case ":$PATH:" in
      *":$DASH_BIN:"*) ;;
      *) echo "Add $DASH_BIN to PATH." ;;
    esac
    echo "Tailscale (one-time, needs sudo): allow the range on the tailnet interface, e.g."
    echo "  sudo firewall-cmd --permanent --zone=trusted --add-port=9470-9479/tcp && sudo firewall-cmd --reload"
    echo "  (adjust the zone to the one holding tailscale0; no rule needed if tailscale0 is already trusted)"
    exit 0 ;;
  --portable)
    DEST="${2:-$HOME/.trio}"
    mkdir -p "$DEST"
    cp -rv "$ROOT/portable" "$DEST/"
    echo
    echo "Portable driver installed. From any project root:"
    echo "  mkdir -p loop && cp $DEST/portable/GOAL.template.md loop/GOAL.md   # edit it"
    echo "  HARNESS=cursor $DEST/portable/driver.sh 10   # or athen|gemini|... "
    echo "Per-harness setup docs: $DEST/portable/SETUP-<harness>.md"
    exit 0 ;;
  "") echo "usage: $0 --global | --codex | --omnigent | --kimi | --zcode | --pi | --opencode [--strong-model provider/model --light-model provider/model] | --omp [--strong-model provider/model --light-model provider/model] | --bridge | --dashboard | /path/to/project | --portable [dir]" >&2; exit 1 ;;
  *)  DEST="$1/.claude" ;;
esac

mkdir -p "$DEST/agents" "$DEST/skills"

for f in "$SRC"/agents/trio-*.md; do
  cp -v "$f" "$DEST/agents/"
done
for d in "$SRC"/skills/trio "$SRC"/skills/trio-init; do
  cp -rv "$d" "$DEST/skills/"
done

echo
echo "Installed. In any project (new Claude Code session):"
echo "  /trio-init <your goal>    # creates loop/ mailbox + GOAL.md"
echo "  /trio                     # one supervised iteration"
echo "  /loop /trio               # autonomous until SHIP/BLOCKED (Esc to stop)"

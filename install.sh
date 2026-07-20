#!/usr/bin/env bash
# Install the trio agent-loop template.
#   ./install.sh --global          -> ~/.claude  (available in EVERY project; recommended)
#   ./install.sh /path/to/project  -> <project>/.claude (committed with that repo)
#   ./install.sh --codex           -> Codex skills + custom agents + fallback
#   ./install.sh --kimi            -> Kimi Code skills + sequential fallback
#   ./install.sh --zcode           -> native ZCode skills
#   ./install.sh --pi              -> native Pi AgentSession extension
#   ./install.sh --opencode [--strong-model provider/model --light-model provider/model]
#                                      -> native OpenCode agents + commands
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
  "") echo "usage: $0 --global | --codex | --kimi | --zcode | --pi | --opencode [--strong-model provider/model --light-model provider/model] | /path/to/project | --portable [dir]" >&2; exit 1 ;;
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

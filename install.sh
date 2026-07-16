#!/usr/bin/env bash
# Install the trio agent-loop template.
#   ./install.sh --global          -> ~/.claude  (available in EVERY project; recommended)
#   ./install.sh /path/to/project  -> <project>/.claude (committed with that repo)
#   ./install.sh --codex           -> native Codex skills + custom agents
#   ./install.sh --zcode           -> native ZCode skills
#   ./install.sh --pi              -> native Pi AgentSession extension
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
    echo "Installed native Codex Trio. Start a new task and ask to run a Trio loop."
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
  "") echo "usage: $0 --global | --codex | --zcode | --pi | /path/to/project | --portable [dir]" >&2; exit 1 ;;
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

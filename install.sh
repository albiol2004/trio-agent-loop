#!/usr/bin/env bash
# Install the trio agent-loop template.
#   ./install.sh --global          -> ~/.claude  (available in EVERY project; recommended)
#   ./install.sh /path/to/project  -> <project>/.claude (committed with that repo)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.claude"

case "${1:-}" in
  --global) DEST="$HOME/.claude" ;;
  "") echo "usage: $0 --global | $0 /path/to/project" >&2; exit 1 ;;
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

#!/usr/bin/env bash
# Update an Omnigent source checkout while preserving the Trio compatibility patch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/patches/child-reasoning-effort.patch"
OMNIGENT_SOURCE="${1:-${OMNIGENT_SOURCE:-/home/alex/omnigent}}"
PATCH_REMOVED=0

die() {
  echo "Error: $*" >&2
  exit 1
}

restore_patch_after_failure() {
  local status=$?
  if (( PATCH_REMOVED )); then
    if git -C "$OMNIGENT_SOURCE" apply --check "$PATCH_FILE" >/dev/null 2>&1; then
      git -C "$OMNIGENT_SOURCE" apply "$PATCH_FILE"
      echo "The update failed; restored the Trio compatibility patch." >&2
    else
      echo "The update failed and the Trio patch could not be restored automatically." >&2
      echo "The checkout is at: $OMNIGENT_SOURCE" >&2
    fi
  fi
  exit "$status"
}
trap restore_patch_after_failure EXIT

command -v git >/dev/null 2>&1 || die "git is required."
[[ -f "$PATCH_FILE" ]] || die "patch not found: $PATCH_FILE"
[[ -d "$OMNIGENT_SOURCE/.git" ]] || {
  die "not an Omnigent git checkout: $OMNIGENT_SOURCE"
}

REPO_ROOT="$(git -C "$OMNIGENT_SOURCE" rev-parse --show-toplevel)"
[[ "$(realpath "$REPO_ROOT")" == "$(realpath "$OMNIGENT_SOURCE")" ]] || {
  die "expected the checkout root, got: $OMNIGENT_SOURCE"
}

echo "Updating Omnigent checkout: $OMNIGENT_SOURCE"

# Reverse only this repository's known patch. Unrelated tracked and untracked
# work is left alone; git pull will refuse safely if it cannot update around it.
if git -C "$OMNIGENT_SOURCE" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
  git -C "$OMNIGENT_SOURCE" apply --reverse "$PATCH_FILE"
  PATCH_REMOVED=1
  echo "Removed the Trio compatibility patch."
elif git -C "$OMNIGENT_SOURCE" apply --check "$PATCH_FILE" >/dev/null 2>&1; then
  echo "The Trio compatibility patch was not currently applied."
else
  die "the checkout is neither a clean patched nor clean unpatched state. Resolve its tracked changes before updating."
fi

git -C "$OMNIGENT_SOURCE" pull --ff-only

if git -C "$OMNIGENT_SOURCE" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
  echo "The updated checkout already contains the Trio compatibility changes."
  PATCH_REMOVED=0
elif git -C "$OMNIGENT_SOURCE" apply --check "$PATCH_FILE" >/dev/null 2>&1; then
  git -C "$OMNIGENT_SOURCE" apply "$PATCH_FILE"
  PATCH_REMOVED=0
  echo "Reapplied the Trio compatibility patch."
else
  die "the bundled Trio patch is incompatible with the updated Omnigent revision. The checkout was updated, but the patch needs to be ported."
fi

trap - EXIT
echo "Done. No server, package installation, or registration commands were run."

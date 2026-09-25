#!/usr/bin/env bash
# Run worker-worktree isolation tests (plus neighbours) inside bwrap with no network.
# Isolation is OS unshare-net plus RO binds; not path checks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_RUNTIME="/home/coder/workflow-lab/.runtime"
WORKDIR="${LAB_RUNTIME}/worker-worktrees-offline-$$"
mkdir -p "$WORKDIR"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

BWRAP="/usr/bin/bwrap"
PYTHON="${PYTHON:-/usr/bin/python3}"
SITE="/home/coder/.local/lib/python3.12/site-packages"

test -x "$BWRAP"
test -x "$PYTHON"

exec "$BWRAP" \
  --unshare-net \
  --die-with-parent \
  --clearenv \
  --proc /proc \
  --dev /dev \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /etc /etc \
  --ro-bind "$SITE" "$SITE" \
  --ro-bind "$ROOT" "$ROOT" \
  --bind "$WORKDIR" "$WORKDIR" \
  --chdir "$ROOT" \
  --setenv HOME "$WORKDIR" \
  --setenv PYTHONPATH "$SITE" \
  --setenv PATH /usr/bin:/bin \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv LANG C.UTF-8 \
  --setenv TMPDIR "$WORKDIR" \
  "$PYTHON" -m pytest \
    -o cache_dir="$WORKDIR" \
    omnigent/tests/test_worker_worktrees.py \
    omnigent/tests/test_worker_worktrees_r2.py \
    omnigent/tests/test_worker_worktrees_r3.py \
    omnigent/tests/test_worker_worktrees_r4.py \
    omnigent/tests/test_worker_worktrees_r5.py \
    omnigent/tests/test_worker_events.py \
    omnigent/tests/test_trioctl.py \
    omnigent/tests/test_omnigent_loop.py \
    omnigent/tests/test_observe_workers.py \
    -q --tb=short

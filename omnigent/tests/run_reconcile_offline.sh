#!/usr/bin/env bash
# Run held-dispatch reconcile + pin suites inside bwrap with no network.
# Isolation is OS unshare-net plus a read-only tree bind; not path checks.
# Usage: run_reconcile_offline.sh [pytest args...] (default: reconcile +
# post-delivery hold + redelivery + omnigent_loop + trioctl suites).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB_RUNTIME="/home/coder/workflow-lab/.runtime"
WORKDIR="${LAB_RUNTIME}/reconcile-offline-$$"
FIXTURE="${LAB_RUNTIME}/t1-canary-31f5a5a/live-staged-1"
mkdir -p "$WORKDIR"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

BWRAP="/usr/bin/bwrap"
PYTHON="${PYTHON:-/usr/bin/python3}"
SITE="/home/coder/.local/lib/python3.12/site-packages"
test -x "$BWRAP"
test -x "$PYTHON"

if [ "$#" -eq 0 ]; then
  set -- omnigent/tests/test_reconcile_held.py \
    omnigent/tests/test_post_delivery_hold.py \
    omnigent/tests/test_first_prompt_redelivery.py \
    omnigent/tests/test_omnigent_loop.py \
    omnigent/tests/test_trioctl.py
fi

FIXTURE_ARGS=()
if [ -d "$FIXTURE" ]; then
  FIXTURE_ARGS=(--ro-bind "$FIXTURE" "$FIXTURE")
fi

"$BWRAP" \
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
  "${FIXTURE_ARGS[@]}" \
  --bind "$WORKDIR" "$WORKDIR" \
  --chdir "$ROOT" \
  --setenv HOME "$WORKDIR" \
  --setenv PYTHONPATH "$SITE" \
  --setenv PYTHONDONTWRITEBYTECODE 1 \
  --setenv PATH /usr/bin:/bin \
  --setenv LANG C.UTF-8 \
  --setenv TMPDIR "$WORKDIR" \
  --setenv TRIO_T1_FIXTURE "$FIXTURE" \
  "$PYTHON" -m pytest -p no:cacheprovider -q "$@"

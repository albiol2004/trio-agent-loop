#!/usr/bin/env bash
# tbench/run_job.sh -- run TrioOpenCodeAgent against Terminal-Bench 4.0
# tasks via Harbor, from the tbench4 venv, with tbench/ on PYTHONPATH and no
# agent/verifier wall-clock timeout (the loop has its own idle watchdog and
# retry budget instead -- see configgen.py). Task filtering and any other
# `harbor run` flag pass straight through as extra arguments.
#
# Usage:
#   tbench/run_job.sh [-i '<task-glob>'] [--n-concurrent N] [harbor run args...]
#   tbench/run_job.sh -i html-js-filter --print-config   # verify timeouts
#
# Env overrides:
#   TBENCH_ROOT       worktree root (default: this script's parent dir's parent)
#   TBENCH4_DIR       tbench4 working dir (default: ../tbench4 next to the worktree)
#   TBENCH_VENV       harbor venv (default: $TBENCH4_DIR/.venv)
#   TBENCH_DATASET    dataset path (default: $TBENCH4_DIR/dataset4/terminal-bench)
#   TBENCH_JOBS_DIR   jobs output dir (default: $TBENCH4_DIR/jobs)
#   TBENCH_KEY_FILE   host path to the OpenCode API key
#                     (default: $HOME/Documents/OpenCodeKey.txt)
#   N                 shorthand for --n-concurrent (default: 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TBENCH_ROOT="${TBENCH_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
TBENCH4_DIR="${TBENCH4_DIR:-$(cd "$TBENCH_ROOT/../tbench4" && pwd)}"
TBENCH_VENV="${TBENCH_VENV:-$TBENCH4_DIR/.venv}"
TBENCH_DATASET="${TBENCH_DATASET:-$TBENCH4_DIR/dataset4/terminal-bench}"
TBENCH_JOBS_DIR="${TBENCH_JOBS_DIR:-$TBENCH4_DIR/jobs}"
TBENCH_KEY_FILE="${TBENCH_KEY_FILE:-$HOME/Documents/OpenCodeKey.txt}"
N="${N:-1}"

if [ ! -x "$TBENCH_VENV/bin/harbor" ]; then
  echo "harbor not found at $TBENCH_VENV/bin/harbor (set TBENCH_VENV)" >&2
  exit 1
fi

mkdir -p "$TBENCH_JOBS_DIR"
CONFIG_FILE="$(mktemp "$TBENCH_JOBS_DIR/.run-config-XXXXXX.json")"
trap 'rm -f "$CONFIG_FILE"' EXIT

# The agent is specified entirely inside this generated config (import_path
# + kwargs + the timeout overrides), never via `--agent` on the command
# line: passing both clobbers the config file's per-agent
# override_timeout_sec/override_setup_timeout_sec (verified against Harbor
# 0.23.0 -- `--agent` rebuilds the `agents` list from scratch after the
# config file is merged in).
TBENCH_KEY_FILE="$TBENCH_KEY_FILE" "$TBENCH_VENV/bin/python3" - "$CONFIG_FILE" <<'PYEOF'
import json
import os
import sys

config = {
    "agents": [
        {
            "import_path": "trio_tbench_agent:TrioOpenCodeAgent",
            "kwargs": {"key_file": os.environ["TBENCH_KEY_FILE"]},
            "override_timeout_sec": 31536000,
            "override_setup_timeout_sec": 31536000,
        }
    ],
    "verifier": {"override_timeout_sec": 31536000},
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
PYEOF

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

"$TBENCH_VENV/bin/harbor" run \
  -c "$CONFIG_FILE" \
  -p "$TBENCH_DATASET" \
  --jobs-dir "$TBENCH_JOBS_DIR" \
  --n-concurrent "$N" \
  "$@"

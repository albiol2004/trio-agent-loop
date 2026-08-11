#!/usr/bin/env bash
# Append a Format-A Trio LOG.md line with trailing usage fields.
# Usage:
#   bash omp/scripts/trio-log-usage.sh -d loop -i 3 -r lead \
#       -s "planned builder pass and reviewed diff" \
#       tokens_in:12195 tokens_out:1250 cache_read:43008 cache_write:0 \
#       requests:3 duration_ms:41765 model:kimi-code/kimi-for-coding
#
#   bash omp/scripts/trio-log-usage.sh -d loop -i 3 -r evaluator -v ITERATE \
#       -s "acceptance criteria 2 and 5 still fail" \
#       tokens_in:8234 tokens_out:980 requests:2 duration_ms:12345
#
# The line is appended to ${dir}/LOG.md in the Format-A shape expected by the
# metrics aggregator:
#   - iter N | lead | <one-liner> | key: value | key: value ...
#   - iter N | evaluator | VERDICT: X — <one-liner> | key: value | ...

set -euo pipefail

DIR="loop"
ITER=""
ROLE=""
VERDICT=""
SUMMARY=""

usage() {
  echo "usage: ${0##*/} -d <mailbox-dir> -i <iteration> -r <lead|evaluator> [-v <verdict>] -s <summary> [key:value ...]" >&2
  exit 2
}

while getopts "d:i:r:v:s:h" opt; do
  case "$opt" in
    d) DIR="$OPTARG" ;;
    i) ITER="$OPTARG" ;;
    r) ROLE="$OPTARG" ;;
    v) VERDICT="$OPTARG" ;;
    s) SUMMARY="$OPTARG" ;;
    h|\?) usage ;;
  esac
done
shift $((OPTIND - 1))

[[ -n "$ITER" && "$ITER" =~ ^[0-9]+$ ]] || usage
[[ "$ROLE" == "lead" || "$ROLE" == "evaluator" ]] || usage

# Sanitize summary: keep it on one line and free of pipe separators.
SUMMARY="${SUMMARY//$'\n'/ }"
SUMMARY="${SUMMARY//|/-}"
SUMMARY="${SUMMARY//  / }"
SUMMARY="${SUMMARY# }"
SUMMARY="${SUMMARY% }"

if [[ "$ROLE" == "evaluator" && -n "$VERDICT" ]]; then
  LINE="- iter $ITER | evaluator | VERDICT: $VERDICT — $SUMMARY"
else
  LINE="- iter $ITER | lead | $SUMMARY"
fi

# Append trailing key:value fields.
for field in "$@"; do
  # Normalize a bare "key value" or "key=value" into "key: value".
  if [[ "$field" =~ ^([^:=[:space:]]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
    field="${BASH_REMATCH[1]}: ${BASH_REMATCH[2]}"
  elif [[ "$field" =~ ^([^:=[:space:]]+)[[:space:]]+([^ ].*)$ ]]; then
    field="${BASH_REMATCH[1]}: ${BASH_REMATCH[2]}"
  fi
  # Accept "key:value" or "key: value" (colon after key is required).
  [[ "$field" =~ ^[^:=[:space:]]+:[[:space:]]*.*$ ]] || { echo "invalid field '$field' (expected key:value or key=value)" >&2; exit 1; }
  # Ensure exactly one space after the colon for readability.
  if [[ "$field" =~ ^([^:=[:space:]]+):[[:space:]]*(.*)$ ]]; then
    field="${BASH_REMATCH[1]}: ${BASH_REMATCH[2]}"
  fi
  # Keep fields pipe-free.
  field="${field//|/-}"
  LINE="$LINE | $field"
done

mkdir -p "$DIR"
printf '%s\n' "$LINE" >> "$DIR/LOG.md"

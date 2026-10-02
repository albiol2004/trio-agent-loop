#!/usr/bin/env bash
# Install (not start) the trio-dash workspace service.
#   dashboard/service/install-service.sh <commit>
# Creates ~/.services/trio-dash with a detached worktree pinned at <commit>,
# the run wrapper and env. Leaves the service disabled; start it with
#   svc enable trio-dash && svc start trio-dash
# Re-running with a new commit moves the pin (then: svc restart trio-dash).
set -euo pipefail
commit="${1:?usage: install-service.sh <commit>}"
repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
dir="$HOME/.services/trio-dash"
mkdir -p "$dir"
if [ -d "$dir/checkout/.git" ] || [ -f "$dir/checkout/.git" ]; then
  git -C "$dir/checkout" fetch -q 2>/dev/null || true
  git -C "$dir/checkout" checkout -q --detach "$commit"
else
  git -C "$repo" worktree add -q --detach "$dir/checkout" "$commit"
fi
install -m 0755 "$dir/checkout/dashboard/service/run" "$dir/run"
[ -f "$dir/env" ] || install -m 0644 "$dir/checkout/dashboard/service/env.example" "$dir/env"
# The port svc shows is the one `run` will listen on: TRIO_DASH_PORT from the
# environment, else the last assignment in the service env file (svc sources it
# before `run`), else 9470. Parsed, never sourced.
port="${TRIO_DASH_PORT:-}"
if [ -z "$port" ]; then
  port="$(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}TRIO_DASH_PORT=//p' "$dir/env" | tail -n 1)"
  port="${port%%#*}"
  port="${port//[[:space:]\"\']/}"
fi
port="${port:-9470}"
if ! [[ "$port" =~ ^[0-9]{1,5}$ ]] || [ "$((10#$port))" -lt 1 ] || [ "$((10#$port))" -gt 65535 ]; then
  echo "install-service.sh: invalid TRIO_DASH_PORT '$port' (want an integer 1..65535)" >&2
  exit 1
fi
port="$((10#$port))"
echo "$port" > "$dir/port.tmp.$$"
mv -f "$dir/port.tmp.$$" "$dir/port"
echo 10 > "$dir/stop_timeout"
[ -f "$dir/pid" ] || touch "$dir/.disabled"
echo "installed trio-dash at $(git -C "$dir/checkout" rev-parse --short HEAD) in $dir"
echo "start:  svc enable trio-dash && svc start trio-dash"

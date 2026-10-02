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
# The port svc shows is the one `run` will listen on. svc starts the service as
# `cd <dir>; set -a; . ./env; set +a; exec ./run`, so an assignment in the env
# file overrides the inherited environment. Effective port: the last
# TRIO_DASH_PORT= assignment in $dir/env (parsed, never sourced), else
# TRIO_DASH_PORT from the installer's environment, else 9470. A fresh env file
# is installed with that port written into its TRIO_DASH_PORT= line; an existing
# env file is never rewritten. Resolved and validated before any side effect.
env_port="${TRIO_DASH_PORT:-}"
port="$env_port"
file_assigns=0
if [ -f "$dir/env" ] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?TRIO_DASH_PORT=' "$dir/env"; then
  file_assigns=1
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
if [ "$file_assigns" = 1 ] && [ -n "$env_port" ] && [ "$env_port" != "$port" ]; then
  echo "install-service.sh: note: TRIO_DASH_PORT=$env_port from the environment is ignored; $dir/env assigns $port (svc sources it before run). Edit that file to change the port." >&2
fi
mkdir -p "$dir"
if [ -d "$dir/checkout/.git" ] || [ -f "$dir/checkout/.git" ]; then
  git -C "$dir/checkout" fetch -q 2>/dev/null || true
  git -C "$dir/checkout" checkout -q --detach "$commit"
else
  git -C "$repo" worktree add -q --detach "$dir/checkout" "$commit"
fi
install -m 0755 "$dir/checkout/dashboard/service/run" "$dir/run"
if [ ! -f "$dir/env" ]; then
  sed "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}TRIO_DASH_PORT=.*/TRIO_DASH_PORT=$port/" \
    "$dir/checkout/dashboard/service/env.example" > "$dir/env.tmp.$$"
  grep -Eq '^TRIO_DASH_PORT=' "$dir/env.tmp.$$" || echo "TRIO_DASH_PORT=$port" >> "$dir/env.tmp.$$"
  chmod 0644 "$dir/env.tmp.$$"
  mv -f "$dir/env.tmp.$$" "$dir/env"
fi
echo "$port" > "$dir/port.tmp.$$"
mv -f "$dir/port.tmp.$$" "$dir/port"
echo 10 > "$dir/stop_timeout"
[ -f "$dir/pid" ] || touch "$dir/.disabled"
echo "installed trio-dash at $(git -C "$dir/checkout" rev-parse --short HEAD) in $dir"
echo "start:  svc enable trio-dash && svc start trio-dash"

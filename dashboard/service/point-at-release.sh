#!/usr/bin/env bash
# Point the trio-dash service at the INSTALLED release's dashboard.
#
#   dashboard/service/point-at-release.sh            # dry run: checks + planned changes
#   dashboard/service/point-at-release.sh --apply    # write env/run (backups kept)
#   then:  svc restart trio-dash                      # nothing here restarts it
#
# Before: ~/.services/trio-dash/env names TRIO_DASH_CHECKOUT=<a git worktree
# of some trio-agent-loop commit> (stale: e.g. b884178). After: it names
# ~/.local/share/trio-agent-loop/releases/<CURRENT sha>, the release the
# installer verified, so the board's parser, liveness and Start/actions match
# the installed trioctl (root-free live mailboxes, claude-workflow runs).
#
# Refuses a release whose dashboard predates dash-actions (no --discover,
# no loop_actions.py, no service/run): pointing the service at it would
# crash-loop it on the unknown --discover flag.
#
# CURRENT must be one line of 7-64 hex digits and resolve to a directory
# inside releases/ (no traversal). The env value is written single-quoted
# (svc sources env with bash `set -a; . ./env`), never through sed/eval.
#
# Options: --release DIR (default: releases/$(cat CURRENT)), --service DIR
# (default ~/.services/trio-dash). Rollback: the printed lines restore the
# backed-up env and run (or remove a run that did not exist before), then
# `svc restart trio-dash`.
set -euo pipefail
apply=0 release="" service="$HOME/.services/trio-dash"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --release) [ $# -ge 2 ] || { echo "point-at-release: --release needs a value" >&2; exit 2; }
               release="$2"; shift 2 ;;
    --service) [ $# -ge 2 ] || { echo "point-at-release: --service needs a value" >&2; exit 2; }
               service="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "point-at-release: unknown argument $1" >&2; exit 2 ;;
  esac
done
share="$HOME/.local/share/trio-agent-loop"
if [ -z "$release" ]; then
  [ -f "$share/CURRENT" ] || { echo "point-at-release: no $share/CURRENT (install a release first)" >&2; exit 1; }
  lines="$(wc -l < "$share/CURRENT")"
  sha="$(head -n 1 "$share/CURRENT" | tr -d '[:space:]')"
  if [ "$lines" -gt 1 ] || ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{7,64}$'; then
    echo "point-at-release: $share/CURRENT is not one hex release id" >&2; exit 1
  fi
  [ -d "$share/releases/$sha" ] || { echo "point-at-release: release $sha is not installed" >&2; exit 1; }
  release="$share/releases/$sha"
  releases_real="$(cd "$share/releases" && pwd -P)"
  release_real="$(cd "$release" && pwd -P)"
  case "$release_real" in
    "$releases_real"/*) ;;
    *) echo "point-at-release: $release resolves outside $releases_real" >&2; exit 1 ;;
  esac
fi
[ -d "$release" ] || { echo "point-at-release: $release is not a directory" >&2; exit 1; }
release="$(cd "$release" && pwd -P)"
case "$release" in
  *$'\n'*) echo "point-at-release: refusing a release path with a newline" >&2; exit 1 ;;
esac
env_file="$service/env"
[ -f "$env_file" ] || { echo "point-at-release: $env_file missing (install-service.sh first)" >&2; exit 1; }

fail=0
# check LABEL COMMAND [ARGS...] — the command runs as an argv, never eval'd.
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then echo "ok    $label"; else echo "FAIL  $label"; fail=1; fi; }
trioctl="${TRIO_DASH_TRIOCTL:-$HOME/.local/bin/trioctl}"
check "release dashboard present"          test -f "$release/dashboard/serve.py"
check "serve.py supports --discover"        grep -q -- '"--discover"' "$release/dashboard/serve.py"
check "serve.py has request guards"         grep -q 'TRIO_DASH_ALLOWED_HOSTS' "$release/dashboard/serve.py"
check "dash-actions module present"         test -f "$release/dashboard/loop_actions.py"
check "service run wrapper in release"      test -f "$release/dashboard/service/run"
check "metrics parser next to dashboard"    test -f "$release/metrics/trio-metrics.py"
check "serve.py compiles"                   python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$release/dashboard/serve.py"
check "installed trioctl present"           test -x "$trioctl"
[ "$fail" = 0 ] || { echo "point-at-release: refusing: this release cannot serve trio-dash" >&2; exit 1; }

current="$(sed -n 's/^TRIO_DASH_CHECKOUT=//p' "$env_file" | tail -1)"
echo
echo "service:   $service"
echo "checkout:  ${current:-<unset>}"
echo "   ->      $release"
echo "run:       $service/run  <-  $release/dashboard/service/run"
if [ "$apply" != 1 ]; then
  echo
  echo "dry run: nothing written. Re-run with --apply, then: svc restart trio-dash"
  exit 0
fi
# A unique backup stamp: two applies in the same second never share one.
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
n=0
while [ -e "$env_file.before-release-$stamp" ] || [ -e "$service/run.before-release-$stamp" ]; do
  n=$((n + 1)); stamp="$(date -u +%Y%m%dT%H%M%SZ)-$n"
done
had_run=0
cp -p "$env_file" "$env_file.before-release-$stamp"
if [ -f "$service/run" ]; then cp -p "$service/run" "$service/run.before-release-$stamp"; had_run=1; fi
tmp="$(mktemp "$service/.env.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# Single-quoted value: safe for `. ./env` whatever the path holds (& | $ spaces).
quoted="'${release//\'/\'\\\'\'}'"
NEWLINE_VALUE="TRIO_DASH_CHECKOUT=$quoted" awk '
  BEGIN { v = ENVIRON["NEWLINE_VALUE"] }
  /^TRIO_DASH_CHECKOUT=/ { if (!done) { print v; done = 1 }; next }
  { print }
  END { if (!done) print v }' "$env_file" > "$tmp"
chmod --reference="$env_file" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
mv "$tmp" "$env_file"
trap - EXIT
install -m 0755 "$release/dashboard/service/run" "$service/run"
printf '%s release %s (was %s)\n' "$stamp" "$release" "${current:-unset}" >> "$service/release-pointer.log"
q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
echo
echo "written. rollback:"
echo "  cp -p $(q "$env_file.before-release-$stamp") $(q "$env_file")"
if [ "$had_run" = 1 ]; then
  echo "  cp -p $(q "$service/run.before-release-$stamp") $(q "$service/run")"
else
  echo "  rm -f $(q "$service/run")   # there was no run before"
fi
echo "now: svc restart trio-dash   (then curl -s http://127.0.0.1:\$(cat $(q "$service/port"))/healthz)"

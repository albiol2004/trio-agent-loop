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
# Options: --release DIR (default: releases/$(cat CURRENT)), --service DIR
# (default ~/.services/trio-dash). Rollback: the printed `cp` lines restore
# the backed-up env and run, then `svc restart trio-dash`.
set -euo pipefail
apply=0 release="" service="$HOME/.services/trio-dash"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --release) release="$2"; shift 2 ;;
    --service) service="$2"; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "point-at-release: unknown argument $1" >&2; exit 2 ;;
  esac
done
share="$HOME/.local/share/trio-agent-loop"
if [ -z "$release" ]; then
  sha="$(cat "$share/CURRENT" 2>/dev/null || true)"
  [ -n "$sha" ] || { echo "point-at-release: no $share/CURRENT (install a release first)" >&2; exit 1; }
  release="$share/releases/$sha"
fi
release="$(cd "$release" && pwd)"
env_file="$service/env"
[ -f "$env_file" ] || { echo "point-at-release: $env_file missing (install-service.sh first)" >&2; exit 1; }

fail=0
check() { if eval "$2"; then echo "ok    $1"; else echo "FAIL  $1"; fail=1; fi; }
check "release dashboard present"          "[ -f '$release/dashboard/serve.py' ]"
check "serve.py supports --discover"        "grep -q -- '\"--discover\"' '$release/dashboard/serve.py'"
check "serve.py has request guards"         "grep -q 'TRIO_DASH_ALLOWED_HOSTS' '$release/dashboard/serve.py'"
check "dash-actions module present"         "[ -f '$release/dashboard/loop_actions.py' ]"
check "service run wrapper in release"      "[ -f '$release/dashboard/service/run' ]"
check "metrics parser next to dashboard"    "[ -f '$release/metrics/trio-metrics.py' ]"
check "serve.py compiles"                   "python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' '$release/dashboard/serve.py'"
check "installed trioctl present"           "[ -x '${TRIO_DASH_TRIOCTL:-$HOME/.local/bin/trioctl}' ]"
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
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$env_file" "$env_file.before-release-$stamp"
[ -f "$service/run" ] && cp -p "$service/run" "$service/run.before-release-$stamp"
tmp="$(mktemp "$service/.env.XXXXXX")"
if grep -q '^TRIO_DASH_CHECKOUT=' "$env_file"; then
  sed "s|^TRIO_DASH_CHECKOUT=.*|TRIO_DASH_CHECKOUT=$release|" "$env_file" > "$tmp"
else
  { cat "$env_file"; echo "TRIO_DASH_CHECKOUT=$release"; } > "$tmp"
fi
chmod --reference="$env_file" "$tmp" 2>/dev/null || chmod 0644 "$tmp"
mv "$tmp" "$env_file"
install -m 0755 "$release/dashboard/service/run" "$service/run"
printf '%s release %s (was %s)\n' "$stamp" "$release" "${current:-unset}" >> "$service/release-pointer.log"
echo
echo "written. rollback:"
echo "  cp -p '$env_file.before-release-$stamp' '$env_file'"
[ -f "$service/run.before-release-$stamp" ] && echo "  cp -p '$service/run.before-release-$stamp' '$service/run'"
echo "now: svc restart trio-dash   (then curl -s http://127.0.0.1:\$(cat $service/port)/healthz)"

# Trio Loop Dashboard

A production-quality, read-only web dashboard for trio agent loops. It runs on Python 3 stdlib only (no pip installs, no build step) and serves a live status board plus live transcript tailing for loop mailboxes.

## Install and start (per project)

Install once:

```bash
./install.sh --dashboard
```

Then from any project root — terminal or agent session:

```bash
trio-dash
```

This serves that project's `loop*/` mailboxes. Defaults:
- host: `0.0.0.0` (reachable over tailscale; override with `TRIO_DASH_HOST` or `--host`)
- port: first free port in `9470-9479` (override the range with `TRIO_DASH_PORTS`, or pass `--port` to bypass the range scan)
- root: `$PWD` (override with `--root`)
- install dir: `~/.local/share/trio-agent-loop/dashboard` (override with `TRIO_DASH_HOME`)

The printed `listening on http://...` line names the actual bound port.

## Remote / Tailscale access

`trio-dash` binds `0.0.0.0` by default, so the dashboard is reachable over your tailnet at `http://<machine-tailscale-ip>:<port>`. If the machine's firewall filters the tailscale interface, allow the range once (needs sudo):

```bash
sudo firewall-cmd --permanent --zone=trusted --add-port=9470-9479/tcp && sudo firewall-cmd --reload
```

Adjust the zone to the one holding `tailscale0`; no rule is needed if that interface is already in a trusted zone.

There is no authentication in v1 — the tailnet ACL is the access boundary. Do not expose `--host 0.0.0.0` on untrusted networks without an auth layer.

## Running from the repo checkout (development)

```bash
python3 dashboard/serve.py            # 127.0.0.1, first free port 9470-9479, root=cwd
```

## What it shows

- **Status board** — one card per `loop*/` mailbox: state badge (RUNNING / SHIPPED / BLOCKED / IDLE), iteration counter, mission, a verdict-history strip (S/I/B tiles — the loop's fingerprint), and last activity. Refreshes every 5 seconds; running loops sort first.
- **Loop detail drawer** — click a card: full mission, fact grid, large verdict history, and an activity timeline parsed from LOG.md (role, per-action duration, summaries, verdicts).
- **Sessions & transcripts** — collapsed by default inside the drawer: matched omp sessions (parents + nested subagents) with live SSE transcript tailing and pause/resume follow.

## Implementation notes

- Mailbox parsing is delegated to `metrics/trio-metrics.py` (loaded by path; no regex duplication).
- The browser side is self-contained: all CSS and JS are served from `dashboard/`.
- The dashboard is read-only: no endpoint mutates loops, sessions, or the repo.

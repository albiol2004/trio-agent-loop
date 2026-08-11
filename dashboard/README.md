# Trio Loop Dashboard

A production-quality, read-only web dashboard for trio agent loops. It runs on Python 3 stdlib only (no pip installs, no build step) and serves a live status board plus live transcript tailing for loop mailboxes.

## Start

From the repo root:

```bash
python3 dashboard/serve.py
```

Defaults:
- host: `127.0.0.1`
- port: `8420`
- root: current working directory (scanned top-level for `loop*/` mailbox dirs)

Override any default:

```bash
python3 dashboard/serve.py --host 0.0.0.0 --port 8420 --root /path/to/project
```

## Remote / Tailscale access

Use `--host 0.0.0.0` to listen on all interfaces so the dashboard is reachable over your tailnet:

```bash
python3 dashboard/serve.py --host 0.0.0.0 --port 8420
```

There is no authentication in v1 — the tailnet ACL is the access boundary. Do not expose `--host 0.0.0.0` on untrusted networks without an auth layer.

## What it shows

- **Status board** — one card per `loop*/` mailbox, showing mission, iteration/status, last verdict, and last activity. Refreshes automatically every 5 seconds.
- **Live transcripts** — click a loop card, pick a matched omp session, and the dashboard tails `~/.omp/agent/sessions/<cwd-slug>/*.jsonl` via SSE. Auto-follows new lines; pause/resume with the button.

## Implementation notes

- Mailbox parsing is delegated to `metrics/trio-metrics.py` (loaded by path; no regex duplication).
- The browser side is self-contained: all CSS and JS are served from `dashboard/`.
- The dashboard is read-only: no endpoint mutates loops, sessions, or the repo.

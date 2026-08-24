# Trio Loop Dashboard

A production-quality web dashboard for trio agent loops: live status board, transcript tailing, and skill/agent registry management. It runs on Python 3.11+ stdlib only (no pip installs, no build step).

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

- **Status board** — one card per `loop*/` mailbox: state badge (RUNNING / SHIPPED / BLOCKED / NEEDS HUMAN / IDLE), iteration counter, mission, a verdict-history strip (S/I/H/B tiles — the loop's fingerprint), and last activity. Refreshes every 5 seconds; running loops sort first.
- **Loop detail drawer** — click a card: full mission, fact grid, large verdict history, and an activity timeline parsed from LOG.md (role, per-action duration, summaries, verdicts).
- **Sessions & transcripts** — collapsed by default inside the drawer: matched omp sessions (parents + nested subagents) with live SSE transcript tailing and pause/resume follow.

## Pages

- `/` — status board: loop cards with state badge (RUNNING / SHIPPED / BLOCKED / NEEDS HUMAN / IDLE), iteration count, mission, verdict history, **Start/Stop buttons**, **phase badge** (lead-done / eval-done / idle), and last activity
- `/skills.html` — skill registry editor: frontmatter forms, validation, generated files marked read-only, and scoped creation
- `/agents.html` — canonical-agent definitions and per-harness install matrix with sync status
- `/topology.html` — layered SVG graphs of harness wiring (entrypoint → agent → model); compare schemas across harnesses
- `/models.html` — resolved model rows for each agent, showing layer precedence (frontmatter, OMP, OpenCode, trioctl, Omnigent)
- `/health.html` — registry lineage, manifest drift, installed harnesses, dangling artifacts, and generate.py check result

## API Endpoints

Loop control:
- `POST /api/loop/start` — start a headless loop: `{"root", "driver", "max_iterations"?}` (driver: `portable` or `omnigent`)
- `POST /api/loop/stop` — stop a running loop: `{"root"}`
- `GET /api/loop/status?root=<absolute-path>` — read loop status: `{pid, iteration, phase, session_ids, driver}`

Registry and agents:
- `GET /api/registry/file?path=<absolute-path>` — read a registry file; generated
  files return read-only, source, and YAML `quoted_keys` metadata
- `POST /api/registry/regenerate` — regenerate a managed file's prompt outputs
  and re-install its harness after a clean-tree check
- `GET /api/registry/schema` — harness/surface destinations, formats (yaml/toml), and per-field specs
- `POST /api/registry/create` — create a registry file; `scope` may be `global` or
  `project`, with `project` required for project scope
- `POST /api/registry/serialize` — validate frontmatter/body pairs; strict YAML parsing server-side
- `GET /api/registry/agents` — list canonical agents
- `GET /api/registry/agents/file?name=<name>` — read a canonical agent by name
- `POST /api/registry/agents` — create a canonical agent; validates against CanonicalAgent schema
- `PUT /api/registry/agents/file` — update a canonical agent; returns 404 if absent, 403 if managed by generate.py
- `DELETE /api/registry/agents/file?name=<name>` — delete a canonical agent
- `POST /api/registry/install` — render a canonical agent into a harness's native format and write it
- `GET /api/registry/topology?root=<repository>` — deterministic node and edge graphs for all harnesses below root
- `GET /api/registry/models?root=<repository>` — resolved model rows per agent, showing layer and availability
- `GET /api/registry/health?root=<repository>` — lineage, manifests, dangling files, and generate.py check result

## Skills editor

The **Skills** page (`/skills.html`) edits skill registry files with a typed frontmatter interface:
- **Schema-driven forms** — `GET /api/registry/schema` serves harness/surface destinations, formats (yaml/toml), and per-field specs (type, widget, required, help text).
- **Server-side validation** — `POST /api/registry/serialize` validates frontmatter/body pairs. Raw YAML (via `{"$yaml": "<text>"}` escape hatch) is parsed strictly server-side; malformed input returns a 400 error naming the field.
- **Format-preserving saves** — the editor sends `quoted_keys` metadata with form saves so an unchanged YAML key keeps its explicit quoting across the JSON API boundary.
- **Scoped creation** — New skill defaults to the selected project when one is active,
  previews its resolved destination, and filters unsupported project layouts.
- **Managed generated files** — source prompt and overlay paths stay visible beside the read-only form, with a clean-tree Regenerate & re-install action.

## Canonical agents & install matrix

The **Agents** page (`/agents.html`) displays canonical-agent definitions and per-harness installation status:
- **Canonical agents** (`registry/canonical-agents/`) are harness-neutral
  definitions: name, description, instructions, model tier
  (standard/high/cheap), tool policy (read-only/edit/spawn), and an optional
  named `spawns` allowlist.
- **Spawn policy** — OMP renders named spawns as a comma-separated
  frontmatter string; OpenCode uses nested `permission.task` entries with
  wildcard deny; Codex appends a delegation note to
  `developer_instructions`. Empty lists preserve each harness's safe default.
- **Install matrix** shows each canonical agent as a row with a cell per supported harness (claude, codex, omp, opencode) indicating sync status: ✓ in-sync, ⚡ stale, ✗ missing, or unsupported.
- **Agent CRUD** — `POST /api/registry/agents` creates, `PUT /api/registry/agents/file` updates, `DELETE /api/registry/agents/file` deletes canonical agents. All validate against the CanonicalAgent schema.
- **Install endpoint** — `POST /api/registry/install` renders a canonical agent into a harness's native format and writes it to the correct location (e.g., `~/.claude/agents/` for claude). Returns 404 for an unknown agent, 400 for an unsupported harness (e.g., omnigent), and 403 if the destination is managed by generate.py.

## Harness topology API

`GET /api/registry/topology?root=<repository>` returns deterministic node and
edge graphs for the harness directories present below the explicit repository
root. The `root` query is required; the endpoint does not scan the dashboard
process user's home directory.

The **Topology** page (`/topology.html`) renders those graphs as layered
entrypoint → agent → model SVGs. Select a harness to inspect its wiring, or
enable **Compare wiring** to see edges and agent output schemas that differ
across the loaded harnesses.
Nodes with source paths link to the Skills registry for inspection.

## Model registry

The **Models** page (`/models.html`) shows one resolved model row for each agent
defined below the selected repository root. Resolution follows frontmatter,
explicit OMP overrides, OpenCode JSONC, trioctl role fallbacks, and Omnigent
executor configuration in that order.

`GET /api/registry/models?root=<repository>` returns the same rows as JSON.
Optional live model lists are read only from the dashboard process home; an
unknown custom model is reported as a warning and does not fail the request.
Model pins declared in frontmatter can be edited from the page. Runtime
override sources remain read-only; their paths link to the Skills editor.

## Registry health

The **Health** page (`/health.html`) traces each registry concept from its
canonical source through a generated target or hand wrapper to an optional
global installation. It also reports `.trio-hashes` manifest drift, installed
harness directories, dangling artifacts, and the read-only
`python3 prompts/generate.py --check` result.

`GET /api/registry/health?root=<repository>` requires an explicit workspace
root. Global harness paths are read only from the dashboard process home, and
deletable dangling files use the existing managed-file-safe DELETE endpoint.

## Implementation notes

- Mailbox parsing is delegated to `metrics/trio-metrics.py` (loaded by path; no regex duplication).
- Registry scanning and serialization use `registry/scan.py`'s format layer (YAML-subset and TOML parsers) and `registry/agents.py` for canonical-agent models and per-harness renderers.
- The browser side is self-contained: all CSS and JS are served from `dashboard/`.
- All registry writes are confined to explicitly allowlisted harness directories and reject paths managed by `prompts/generate.py`.

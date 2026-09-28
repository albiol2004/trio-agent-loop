# Trio Loop Dashboard

A production-quality web dashboard for trio agent loops: live status board, transcript tailing, and skill/agent registry management. It runs on Python 3.11+ stdlib only (no pip installs, no build step).

## Install and start (per project)

Install once:

```bash
./install.sh --dashboard
```

This ships all pages and registry modules (`agents.html`, `topology.html`,
`models.html`, `health.html` and their corresponding JavaScript/CSS
dependencies). Then from any project root — terminal or agent session:

```bash
trio-dash
```

This serves that project's `loop*/` mailboxes. Defaults:
- host: `0.0.0.0` (reachable over tailscale; override with `TRIO_DASH_HOST` or `--host`)
- port: first free port in `9470-9479` (override the range with `TRIO_DASH_PORTS`, or pass `--port` to bypass the range scan)
- root: `$PWD` (override with `--root`)
- install dir: `~/.local/share/trio-agent-loop/dashboard` (override with `TRIO_DASH_HOME`)

When `tailscale serve` holds the wildcard port, `trio-dash` falls back to
`127.0.0.1` for local access only. The printed `listening on http://...` line
names the actual bound address and port.

**Managed files caveat**: The installed copy has no `prompts/` directory, so
managed-file provenance display and Regenerate & re-install only work when
the dashboard is served from the repo checkout (development mode).

## Remote / Tailscale access

`trio-dash` binds `0.0.0.0` by default, so the dashboard is reachable over your tailnet at `http://<machine-tailscale-ip>:<port>`. If the machine's firewall filters the tailscale interface, allow the range once (needs sudo):

```bash
sudo firewall-cmd --permanent --zone=trusted --add-port=9470-9479/tcp && sudo firewall-cmd --reload
```

Adjust the zone to the one holding `tailscale0`; no rule is needed if that interface is already in a trusted zone.

There is no authentication in v1 — the tailnet ACL is the access boundary. Do not expose `--host 0.0.0.0` on untrusted networks without an auth layer.

## Workspace service (always-on)

`dashboard/service/` holds a supervised setup for the workspace `svc`
manager (`~/.services/<name>/run`):

- `run` — starts `serve.py` on `127.0.0.1:$TRIO_DASH_PORT` (default 9470),
  restarts it on exit with 2–60 s backoff, probes `/healthz` every 20 s and
  restarts after 3 consecutive failures, and maps tailnet-only HTTPS on the
  same port with `tailscale serve --bg --https=9470` (the workspace's `/` and
  `/auth` mappings are untouched).
- `env.example` — pinned checkout path, port, `TRIO_DASH_SCAN_ROOTS`
  (directories whose children are auto-discovered, e.g. `~/personal`), and
  fixed `--workspace` arguments plus `--discover`.
- `install-service.sh <commit>` — creates `~/.services/trio-dash` with a
  detached worktree pinned at `<commit>`; leaves the service disabled.

```bash
dashboard/service/install-service.sh <commit>
svc enable trio-dash && svc start trio-dash
curl -s http://127.0.0.1:9470/healthz
# https://<workspace-host>.<tailnet>:9470/
```

`serve.py` also accepts `--discover` to keep auto-discovery on alongside
explicit `--workspace` paths, and `TRIO_DASH_SCAN_ROOTS` (`os.pathsep`
separated) to replace the default scan roots.

## Running from the repo checkout (development)

```bash
python3 dashboard/serve.py            # 127.0.0.1, first free port 9470-9479, root=cwd
```

## What it shows

- **Board (every workspace at once)** — one `/api/overview` poll every 5 seconds covers every discovered workspace. Top to bottom: a verdict sentence ("2 loops need you; nothing is running."), four tiles (needs you, running now, shipped in the last 7 days, loops tracked), **Needs you**, **Running now**, an **All loops** table, and collapsed **Review notes**. Loops are keyed by workspace root + mailbox name. Nested mailbox discovery is unchanged: every `loop*/` directory and its direct subdirectories that are mailboxes (contain any of LOG.md, GOAL.md, STATE.md, VERDICT.md, PLAN.md; briefs/ and evidence* are skipped).
- **Facts only** — a loop's state badge is derived from facts in this order: live evidence (`running_sources`: driver pid or lock, /proc cmdline naming the mailbox, live `.session.json` pid, opt-in broker probe) → "Running"; else the latest verdict (Shipped / Needs human / Blocked / Iterating); else the STATE.md status word. Every badge has an icon and text; the tooltip lists the underlying facts. Titles come from GOAL.md's first heading (`# Mission: X` → "X"); generic headings such as `# Goal` fall back to the mission's first clause.
- **Needs you vs review notes** — unread attention items are grouped per loop. Kinds `needs_human`, `blocked`, `interrupted`, `orphaned`, `queue_fault`, any high-severity item, and any item on a running loop go to **Needs you**; drift/overlap/repair notes on loops that are not running go to **Review notes**. `interrupted` fires only when STATE.md claims `running`/`in_progress`/`active`/`iterating`, no liveness source is live, no orphaned sidecar exists and the verdict is not terminal — two recorded facts disagreeing, no idle-time threshold.
- **All loops table** — sortable by loop, workspace and last activity; filtered by search, workspace, and the fact-only segments Running / Attention / All / Archived (preferences persist in `localStorage` when available). Rows patch in place on poll.
- **Attention inbox** — stable item ids (sha256 hash of root, loop_name, kind, anchor) and per-workspace read/unread state persisted in `~/.local/share/trio-agent-loop/inbox-state.json` (POST /api/inbox/read or /api/inbox/unread). Read items are hidden by default; unread count remains the inbox badge.
- **Loop detail drawer** — click any row, attention item or running card (deep link `#root=<workspace>&loop=<mailbox>`; the older `#loop=<mailbox>` still works): why the loop is flagged, full mission, fact grid, verdict history, commits (first 8, then "Show all"), slices, and an activity timeline parsed from LOG.md. Start/Stop live here and are enabled only for a workspace's `loop/` mailbox, because `/api/loop/start|stop` act on `<root>/loop`; Stop asks for confirmation. The drawer is modal: focus moves in, Escape closes it and focus returns.
- **States** — skeletons while loading, an empty state, an error banner with Retry that keeps the last data on screen, a Stale indicator when data is older than 30 s, and a partial-failure banner naming any workspace that could not be read.
- **Sessions & transcripts** — collapsed by default inside the drawer: matched omp sessions (parents + nested subagents) with live SSE transcript tailing and pause/resume follow.

## Pages

- `/` — loop board across all workspaces (see "What it shows")
- `/skills.html` — skill registry editor: frontmatter forms, validation, generated files marked read-only, and scoped creation
- `/agents.html` — canonical-agent definitions and per-harness install matrix with sync status
- `/topology.html` — layered SVG graphs of harness wiring;
  optionally include installed nodes and compare schemas
- `/models.html` — resolved model rows for each agent, showing layer precedence (frontmatter, OMP, OpenCode, trioctl, Omnigent)
- `/health.html` — registry lineage, manifest drift, installed harnesses, dangling artifacts, and generate.py check result

## API Endpoints

Board and service:
- `GET /api/overview` — every workspace's board in one response:
  `{workspaces: [{root, name, loops, inbox, elapsed_ms, error?}], scanned, updated_at, elapsed_ms}`.
  Workspaces without loops are omitted (counted in `scanned`). The first
  request builds synchronously; later requests return the last build at once
  and trigger one background rebuild when it is older than 4 s. One /proc
  snapshot and one git slice attribution per loop are shared per build.
- `GET /healthz` — `{ok, uptime_seconds, version, workspaces, overview_age_seconds}` for supervisors.

Loop control:
- `POST /api/loop/start` — start a headless loop: `{"root", "driver", "max_iterations"?}` (driver: `portable` or `omnigent`)
- `POST /api/loop/stop` — stop a running loop: `{"root"}`
- `GET /api/loop/status?root=<absolute-path>` — read loop status: `{pid, iteration, phase, session_ids, driver}`

Inbox:
- `POST /api/inbox/read` — mark IDs read:
  `{"ids": ["<id>"], "root": "<absolute workspace>"}`
- `POST /api/inbox/unread` — mark IDs unread with the same body
- `GET /api/board` — includes each inbox item's `id`, `read`, and
  first-observation `first_seen` fields

Registry and agents:
- `GET /api/registry/file?path=<absolute-path>` — read a registry file;
  generated files return read-only, source, and YAML `quoted_keys` metadata.
  Omnigent role documents report `yaml-document` format with `prompt` as body.
- `POST /api/registry/regenerate` — regenerate a managed file's prompt outputs
  and re-install its harness after a clean-tree check
- `GET /api/registry/schema` — harness/surface destinations as
  `{project, global}` relative paths (or `null`), formats
  (yaml/yaml-document/toml), and per-field specs
- `POST /api/registry/create` — create a registry file; `scope` may be `global` or
  `project`, with `project` required for project scope. A project request for a
  harness without a project layout falls back to global and returns
  `scope_used: "global"`.
- `POST /api/registry/serialize` — validate frontmatter/body pairs; strict YAML parsing server-side
- `GET /api/registry/agents` — list canonical agents
- `GET /api/registry/agents/file?name=<name>` — read a canonical agent by name
- `POST /api/registry/agents` — create a canonical agent; validates against CanonicalAgent schema
- `PUT /api/registry/agents/file` — update a canonical agent; returns 404 if absent, 403 if managed by generate.py
- `DELETE /api/registry/agents/file?name=<name>` — delete a canonical agent
- `POST /api/registry/install` — render a canonical agent into a harness's native format and write it
- `GET /api/registry/topology?root=<repository>&workflow=<name>&home=<bool>` —
  deterministic node and edge graphs below root; `workflow` is `roles`
  (default), `productionize`, or `entrypoints`; response nodes include
  `origin` and the response includes `include_home`
- `GET /api/registry/models?root=<repository>` — resolved model rows per agent,
  plus `available`, `by_executor`, and per-source `sources` status
- `GET /api/registry/health?root=<repository>` — lineage, manifests, dangling files, and generate.py check result

## Skills editor

The **Skills** page (`/skills.html`) edits skill registry files with a typed frontmatter interface:
- **Schema-driven forms** — `GET /api/registry/schema` serves harness/surface destinations, formats (yaml/toml), and per-field specs (type, widget, required, help text).
- **Omnigent agent forms** — dotted `executor.*` fields map into nested YAML;
  `yaml-document` sends `prompt` as the body.
- **Structured widgets** — specialized input controls render per field type and widget hint:
  - Enum select dropdowns with custom YAML escape for off-list values and warning on save
  - Model dropdowns fed by `/api/registry/models` (provider-specific sets per
    harness); Omnigent `executor.config.harness` changes swap
    `executor.model` options through `by_executor`
  - OpenCode permission grid with nested allow/deny map editing and byte-exact no-op round-trip
  - OMP spawns multi-select (agent allowlist, comma-separated scalar serialization)
  - JSON-schema field with JSON validation in a YAML literal block
- **Server-side validation** — `POST /api/registry/serialize` validates frontmatter/body pairs. Raw YAML (via `{"$yaml": "<text>"}` escape hatch) is parsed strictly server-side; malformed input returns a 400 error naming the field.
- **Format-preserving saves** — the editor sends `quoted_keys` metadata with form saves so an unchanged YAML key keeps its explicit quoting across the JSON API boundary.
- **Scoped creation** — New skill dialog offers project|global scope selector
  with destination preview. Every catalog harness stays listed; project-only
  requests with global-only layouts show a global fallback and the create
  response reports `scope_used: "global"`. Project scope defaults to the
  selected project if one is active.
- **Cursor destinations** — Cursor is a first-class harness with project and
  global skills in `.cursor/skills` and agents in `.cursor/agents`.
- **Managed generated files** — files generated by `prompts/generate.py` show source prompt and overlay paths beside the read-only form, with a clean-tree Regenerate & re-install action (refuses on dirty tree).

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
- **Install matrix** shows each canonical agent as a row with a cell per
  supported harness (claude, cursor, codex, omp, opencode, omnigent) indicating sync
  status: ✓ in-sync, ⚡ stale, ✗ missing, or unsupported. Every supported
  harness is always listed; `scope_used: "global"` appears when the harness
  has no project agent directory and falls back to global installation.
- **Omnigent model dropdowns** use the selected `executor.config.harness`;
  changing it swaps `executor.model` options through `by_executor`.
- **Agent CRUD** — `POST /api/registry/agents` creates, `PUT /api/registry/agents/file` updates, `DELETE /api/registry/agents/file` deletes canonical agents. All validate against the CanonicalAgent schema.
- **Scoped agent installs** — project scope keeps every supported harness
  selectable. Harnesses without a project agent directory fall back to global
  installation and report `scope_used: "global"` in each destination or
  installation response.
- **Install endpoint** — `POST /api/registry/install` renders a canonical
  agent into a harness's native format and writes it to the correct location
  (e.g., `~/.claude/agents/` for claude, `~/.cursor/agents/` for Cursor, or
  `~/.omnigent/agents/<name>/config.yaml` for omnigent). Omnigent is a target
  harness: install uploads the rendered bundle (multipart POST to `/v1/sessions`
  on the broker), writes a `broker.json` sidecar persisting the durable `agent_id`,
  and is idempotent (re-install via PUT `/v1/sessions/{id}/agent`). The
  installation response includes `agent_id` and `session_id`.
  Returns 404 for an unknown agent and 403 if the destination is managed by
  generate.py.

## Harness topology API

`GET /api/registry/topology?root=<repository>&workflow=<name>&home=<bool>`
returns deterministic node and edge graphs for the harness directories present
below the explicit repository root. The `root` query is required;
`workflow` accepts `roles` (default), `productionize`, or `entrypoints`.
`home` accepts `0`, `1`, `false`, or `true` case-insensitively. When omitted,
the installed trio-dash copy defaults to including the dashboard process home;
the repository checkout defaults to workspace-only. The response includes
`include_home`, and every node has `origin` set to `workspace` or `installed`.

The **Topology** page (`/topology.html`) renders those graphs as layered
entrypoint → agent → model SVGs. An **include installed** toggle sends
`home=1` or `home=0` and styles `origin: installed` nodes. A **Workflow
selector** offers `roles`, `productionize`, and `entrypoints` choices. `productionize` graphs come from
each wrapper's `## Dispatch table` section; unparseable tables become warning
nodes. Select a workflow and harness to inspect its wiring, or enable
**Compare wiring** to see edges and agent output schemas that differ across
the loaded harnesses.
Nodes with source paths link to the Skills registry for inspection.

## Model registry

The **Models** page (`/models.html`) shows one resolved model row for each agent
defined below the selected repository root. Resolution follows frontmatter,
explicit OMP overrides, OpenCode JSONC, trioctl role fallbacks, and Omnigent
executor configuration in that order.

`GET /api/registry/models?root=<repository>` returns the resolved rows plus
`available` catalogs, `by_executor` short/native aliases, and `sources` status
records. Optional live model lists are read only from the dashboard process
home; an unknown custom model is reported as a warning and does not fail the
request.
Model pins declared in frontmatter can be edited from the page. Runtime
override sources remain read-only; their paths link to the Skills editor.
Editable model rows are `<select>` controls filtered by row harness, with a
custom-id escape for values outside the catalog. Omnigent forms use the
selected executor harness and swap `executor.model` options through
`by_executor` when `executor.config.harness` changes.

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

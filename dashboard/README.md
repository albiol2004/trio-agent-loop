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
explicit `--workspace` paths. Discovery settings: `TRIO_DASH_SCAN_ROOTS`
(`os.pathsep` separated; replaces the defaults; HOME and `/` are refused),
`TRIO_DASH_SCAN_DEPTH` (default 3: direct children always, deeper dirs only
when they hold a `loop*/` dir; dot-dirs and dependency/build dirs are
skipped; at most 4000 dirs per root), and `TRIO_DASH_WORKSPACES` for
explicitly registered workspaces (shown in full).

Linked git worktrees found by the walk appear as `<repo> (worktree <name>)`
with only the mailboxes that are theirs. Ownership is git ancestry, never
file mtimes: with `mb = merge-base(worktree HEAD, base tip)` (base tip = the
main checkout's HEAD; for a bare repository `main`/`master`/`origin/HEAD`),
a mailbox is `new on branch` (absent at `mb`) or `committed on branch` /
`deleted on branch` when one of its own changes since `mb` is still not in
the base tip — so a needs-human commit survives merge, rebase, reset and
stash, a committed deletion counts, and already-merged or identical edits
and touched files do not. `modified` / `untracked` come from `git status`
(`--no-optional-locks`, reused up to ~90–135 s unless the index changes);
`runtime files present` (untracked or ignored sidecars/locks — tracked ones
do not count) shows a mailbox only alongside other evidence or while its
STATE/VERDICT says running or needs a person; `live` is argv, a broker
session with `workspace` = the worktree, or a live sidecar. With no
merge-base (unrelated history, no base branch) actionable loops are shown as
`no common base (…)` rather than hidden. Ancestry is cached on the (HEAD,
base tip) commit ids read from ref files. Worktree drilldown works;
worktrees are not listed as registry workspaces. Broker liveness:
`TRIO_BOARD_BROKER_URL` (the service env sets `http://127.0.0.1:6767`).

## Running from the repo checkout (development)

```bash
python3 dashboard/serve.py            # 127.0.0.1, first free port 9470-9479, root=cwd
```

## What it shows

- **Board (every workspace at once)** — one `/api/overview` poll every 5 seconds covers every discovered workspace. Top to bottom: a verdict sentence ("2 loops need you; nothing is running."), four tiles (needs you, running now, shipped in the last 7 days, loops tracked), **Needs you**, **Running now**, an **All loops** table, and collapsed **Review notes**. Loops are keyed by workspace root + mailbox name. Nested mailbox discovery is unchanged: every `loop*/` directory and its direct subdirectories that are mailboxes (contain any of LOG.md, GOAL.md, STATE.md, VERDICT.md, PLAN.md; briefs/ and evidence* are skipped).
- **Facts only** — a loop's state badge is derived from facts in this order: live evidence (`running_sources`: driver pid or lock, /proc cmdline naming the mailbox, live `.session.json` pid, opt-in broker probe) → "Running"; else the latest verdict (Shipped / Needs human / Blocked / Iterating); else the STATE.md status word. Every badge has an icon and text; the tooltip lists the underlying facts. Titles come from GOAL.md's first heading (`# Mission: X` → "X"); generic headings such as `# Goal` fall back to the mission's first clause.
- **Needs you vs review notes** — unread attention items are grouped per loop. Kinds `needs_human`, `blocked`, `interrupted`, `orphaned`, `queue_fault`, any high-severity item, and any item on a running loop go to **Needs you**; drift/overlap/repair notes on loops that are not running go to **Review notes**. `interrupted` fires only when STATE.md claims `running`/`in_progress`/`active`/`iterating`, no liveness source is live, no orphaned sidecar exists and the verdict is not terminal — two recorded facts disagreeing, no idle-time threshold. It is a medium "Needs you" item only when the broker listing was read (`broker: ok`) and no loop-worker process runs inside the workspace; otherwise it is a low-severity review note. Loop workers are positively worker-shaped commands only: `trioctl … run|loop`, `trio_loop.py`, `portable/driver.sh`, and headless harness runs (`claude -p/--print`, `codex … exec`, `cursor-agent -p/--print`, `opencode run`, `omp -p`). Interactive sessions, MCP servers and unknown processes never soften it. A STATE.md status of `needs_human`/`awaiting_human`/`awaiting_user` or `blocked` raises the matching high item even when VERDICT.md is older, and sets the state badge.
- **Liveness facts** — `driver`: live `.driver.json` or `.lock/pid` process that is not a zombie and started before its record was last written (a recycled PID is not the owner); `proc`: a live process whose argv element (or `--opt=value`, or a relative option value / path with a separator resolved against its cwd) is the mailbox or a file in it, but not a file inside a child mailbox; `session`: live `.session.json` pid (same reuse check); `broker`: sidecar session ids reported running by `GET /v1/sessions/<id>`, or a running session in the paginated `GET /v1/sessions` listing whose `workspace` is this workspace and whose title starts `trioctl <mailbox-dir> ` (ambiguous dir names in one workspace are never attributed).
- **All loops table** — sortable by loop, workspace and last activity; filtered by search, workspace, and the fact-only segments Running / Attention / All / Archived (preferences persist in `localStorage` when available). Rows patch in place on poll.
- **Attention inbox** — stable item ids (sha256 hash of root, loop_name, kind, anchor) and per-workspace read/unread state persisted in `~/.local/share/trio-agent-loop/inbox-state.json` (POST /api/inbox/read or /api/inbox/unread). Read items are hidden by default; unread count remains the inbox badge.
- **Loop detail drawer** — click any row, attention item or running card (deep link `#root=<workspace>&loop=<mailbox>`; the older `#loop=<mailbox>` still works): why the loop is flagged, full mission, fact grid, verdict history, commits (first 8, then "Show all"), slices, and an activity timeline parsed from LOG.md. Start/Stop live here. Their availability and reason come from the card's `controls` and are enforced by the API: Start needs `<root>/loop`, GOAL.md, the driver entrypoint in this dashboard checkout (drivers run from here with `cwd=<workspace>`), no live evidence, and survival past a 1.5 s startup grace (otherwise 502 with the log tail, and `.driver.json` is left untouched; an existing iteration/session cursor is kept). Stop needs a live driver/lock PID whose command line is a loop driver, and asks for confirmation. The server keeps each mailbox's last action (`last_action`: started, failed, finished, stopped, with exit code) and reaps drivers it started. The drawer is modal: focus moves in, Escape closes it and focus returns.
- **States** — skeletons while loading, an empty state, an error banner with Retry that keeps the last data on screen, a Stale indicator when data is older than 30 s, and a partial-failure banner naming any workspace that could not be read.
- **Timeline** — one card per iteration: verdict badge, lifecycle, a Compare toggle (pick two for the side-by-side panel), a wall-clock track with a role legend when LOG.md entries carry timings, one row per entry with its duration (summaries clamp to three lines; a Show more / Show less button expands one in place by keyboard or touch), and PLAN.md slice chips that open the Graph tab.
- **Sessions & transcripts** — the Transcripts tab lists the loop's Omnigent session exports (`<loop>/.sessions/*.jsonl`, written by trioctl; label, role agent and time from each file's header line) together with matched omp sessions (parents + nested subagents), newest first, and opens the newest one. trioctl writes an export when it archives a role's session, usually soon after the role finishes and otherwise at the end of the run, so an active session is not listed yet; the tab says so in a one-line note whose details expand. An open transcript is tailed over SSE (lines appended to the file appear; pause/resume follow), which is not live streaming of a running broker session. Omnigent `input_text`/`output_text` messages, resource events and compactions render alongside omp records, and a record with a malformed field is skipped without dropping the rest of its batch. The transcript endpoint only reads files under `~/.omp/agent/sessions/` or `.sessions/*.jsonl` exports of a loop in the requested workspace; a `.sessions` directory that is a symlink lists nothing, and file symlinks out of `.sessions/` are neither listed nor streamed. Loops whose driver records neither say so in place of an empty pane.

## Pages

Every page shares the loop board's app bar, page container, panels and controls from `app.css` (tokens only, no page-level styles); the registry pages carry the workspace selector in the app bar.

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
- Control routes require an explicit `root` (400 otherwise). Start is also disabled while broker liveness is unknown (`unreachable`/`truncated`). `--config` values in argv are not mailbox evidence.
- `GET /healthz` — `{ok, uptime_seconds, version, workspaces, overview_age_seconds, broker}` for supervisors.
- Rebuilds: a poll older than 15 s triggers one background rebuild (cache expiries are jittered per path so refreshes spread out); git-backed derivations (slice attribution, iterations, commits) and parsed card facts are reused while the mailbox files and the repo's HEAD/reflog are unchanged; one `/proc` read and one broker listing are shared per build; the server pre-warms the overview at startup; hidden tabs stop polling.
- Request guards (all routes): the Host must be an IP literal, `localhost` or a name in `TRIO_DASH_ALLOWED_HOSTS` (DNS-rebinding defence, 421 otherwise). POST/PUT/DELETE with an `Origin` must be same-origin or listed in `TRIO_DASH_ALLOWED_ORIGINS`; `Sec-Fetch-Site: cross-site` is refused; a request body must be `application/json` (415 otherwise).

Loop control:
- `POST /api/loop/start` — start a headless loop: `{"root", "driver", "max_iterations"?}` (driver: `portable` or `omnigent`). Runs the INSTALLED drivers — `~/.local/bin/trioctl` (or `TRIO_DASH_TRIOCTL`) and the installed release's `metrics/trio_loop.py` (`~/.local/share/trio-agent-loop/releases/<CURRENT>`, or `TRIO_DASH_RELEASE_DIR`) — never this checkout's own copies.
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

## Unblock actions (dash-actions)

Loops are addressed by `{root, loop}` (the board name of the ROOT mailbox);
the server resolves the live copy itself (a root-free loop's Lead worktree
via the `trio-worktrees` ledger) and acts there. Every POST below goes
through the request guards above.

**Which loops.** Besides `<workspace>/loop*` mailboxes (live copies for
root-free loops; a Lead worktree is never listed a second time as a linked
worktree), the board reads the claude-workflow run registry
(`~/.local/share/trio-agent-loop/native-runs/*.json`, `TRIO_NATIVE_RUNS_DIR`;
written by `native/launch.sh` and the helper on the `native-dash` line): each
registered run's repo becomes a workspace and its mailbox a card — any
mailbox name, hidden directories included (`TRIO_DASH_NATIVE_RUNS=0` turns
this off). Records are validated (absolute path, existing mailbox) and only
make runs visible.

**States** (`loop_state` on every card, from files only): `running`,
`shipped`, `needs_human`, `blocked`, `error`, `needs_retirement`,
`needs_land`, `held` (Omnigent `.sessions/held-*.json`, or a
claude-workflow `held_step`), `conflict`, `budget`, `iteration_cap`,
`interrupted`, `answered` (a human answered; waiting for its restart),
`ready` (not started), `unknown`. A
claude-workflow result (`.native-result.json`, or for older runs the raw
session output in `.native-runs/`) is used only when it belongs to the
latest run and STATE.md was not changed after it. `driver:
"claude-workflow"` is a known driver (its `.session.json` "session" is a run
token, never probed on the broker).

**Inbox kinds** added: `error`, `needs_retirement`, `needs_land`, `held` (one
per held-dispatch record, with role/session/hold reason), `conflict`,
`budget`, `iteration_cap`, `dangling_worktrees`. A known outcome replaces the
generic `interrupted` item; a hold replaces the generic `needs_human`.

Endpoints:
- `GET /api/loop/actions?root=&loop=` — `{state, driver, live, native,
  fixes: [{id, title, applicable, destructive, commands_preview | reason}],
  unblock (table), never_automated, answer: {allowed, reason, stop,
  reset_allowed}, diagnosis, harnesses, log (last 50 actions)}`.
- `POST /api/loop/diagnose {root, loop, harness?, accept_exposure?}` → 202;
  409 while one runs for that loop; 409 `{accept_exposure_required,
  warning}` for Cursor without `accept_exposure: true`; 429 when
  `TRIO_DASH_MAX_DIAGNOSES` (default 2) diagnoses already run across all
  loops. `GET /api/loop/diagnosis?root=&loop=` polls it.
- `POST /api/loop/fix {root, loop, fix, args?, confirm?, confirm_token?}` —
  400 for an id outside the allowlist (logged), 409 `{refused}` when a
  precondition fails, 409 `{confirm_required, plan: {commands_preview, notes,
  confirm_token, basis}}` for a destructive fix (and for `native_resume`);
  the confirm must carry that `confirm_token` = hash(loop, fix id, exact
  steps incl. a resume's validated session/run id/args, the live HEAD, the
  root HEAD and the land target's sha for root-free loops, STATE.md /
  VERDICT.md / HUMAN.md / GOAL.md, `.native-launch.json` / `.native-result.json` /
  `.session.json` / `.driver.json` / `.repairs` digests and the registry
  record). The server re-plans under the loop's
  action lock (the loop context is gathered only after the lock is taken)
  and compares: a mismatch is 409 `{plan_changed, plan}` with the new
  preview, and nothing runs. 200/502 with per-step results otherwise.
- `POST /api/loop/answer {root, loop, answer, reset?=true, confirm?,
  confirm_token?}` — 409 preview first (the exact HUMAN.md entry and STATE
  changes, and its token), then writes with the same token rule; the
  response lists the restart fixes that now apply. An answer that is not
  valid Unicode text (a lone UTF-16 surrogate) is a 400; nothing is written.
- A mailbox that resolves outside the workspace root and its accepted Lead
  worktrees (listed by the workspace repository's own `git worktree list`,
  really of that repository, under the workspace or the Trio worktree root
  `$TRIO_WORKTREE_ROOT` / `~/.local/state/trio-agent-loop/worktrees/
  <repo>-<hash>`, computed from the DASHBOARD's environment — a loop
  started with `--worktree-root` or another `TRIO_WORKTREE_ROOT` (e.g. a
  sibling `<repo>-worktrees/` layout) needs the same `TRIO_WORKTREE_ROOT`
  in the trio-dash service env; an enclosing repository never counts), or that contains
  any symlink (directly, or in `.lock/` or `.native-runs/`), or that a
  nested repository owns — a `.git` entry in the mailbox or in any directory
  between it and the workspace root (or the Lead worktree holding a
  root-free live copy), e.g. `loop-grp/.git` for `loop-grp/m1`; every git
  call on the mailbox would read that repository's config — gets 403 on
  every action endpoint and a "refused" board card; nothing in it is read
  (every mailbox, sidecar and registry read is `O_NOFOLLOW`, regular files
  only) or written (writes are `mkstemp` + rename, never through a link).
  A workspace root that is itself the mailbox and its repository's top
  level is accepted: that `.git` is the workspace's own repository. So is
  a mailbox that is its own repository's top level (a real `.git`
  directory directly in it, e.g. a plain `git init` in the mailbox, and
  `git rev-parse --show-toplevel` from it naming the mailbox): git never
  checks out a `.git` path, so that repository is the loop's own. A `.git`
  in a directory between such a mailbox and the workspace root is still
  refused.
- Every git call the dashboard makes on a workspace, mailbox or slice
  repository (reads, the git steps it plans, and trio-shadow's slice
  attribution — which trioctl's commit gate shares) runs with one config
  set, `SAFE_GIT_CONFIG` in `metrics/human_ledger.py` (trio-metrics.py keeps
  a compared stand-alone copy): `core.fsmonitor=false`,
  `core.hooksPath=/dev/null`, `protocol.file.allow=never`,
  `safe.bareRepository=explicit`, `log.showSignature=false`,
  `core.sshCommand=false`, `core.pager=cat`, `gpg.program`,
  `gpg.ssh.program` and `gpg.x509.program` = `/bin/false`,
  `commit.gpgSign=false`, and empty `core.askPass` and `credential.helper`.
  A repository-configured program never runs from the dashboard, and a
  tracked bare-repository layout (e.g. a PLAN.md slice `repo:` pointing at
  one) is not a repository to it. The retirement commit is unsigned and
  skips hooks (the `retire_ship` preview says so). `diff.external=` is not
  set (git 2.43 would then run an empty command on `git diff`); instead
  the one textual diff the dashboard runs — the diagnosis snapshot's
  `git diff HEAD --binary`, hashed to detect changes — passes
  `--no-ext-diff --no-textconv`, so no configured external diff or
  textconv program runs. Every other diff is `--name-only`/`--name-status`.
- Mailbox data never chooses what runs. A mailbox path may use any
  printable characters (spaces, non-ASCII letters, `,`, `~` …): it is
  passed as one argv element and every driver quotes it where it enters a
  prompt; a path with a control character, newline, NUL, line separator or
  format character refuses every driver start (MAILBOX-SCHEMA.md "Mailbox
  path rule"). The diagnosis context shows repo-controlled names in a
  display-safe form.
- `native_resume` previews use `metrics/native_args.py`, the validator
  `launch.sh` itself runs, so a preview never offers a resume launch.sh
  would refuse (a `..` or non-canonical `args.mailbox`, a non-string or
  non-allowlisted `models` value …); any bad value is a 409, never a 500.
- Confirm tokens also bind `reconcile_apply`'s held records and the file
  set `retire_ship` would commit.

**Fix allowlist** (server-side; a diagnosis only proposes an id). Every fix
first requires that nothing is live (no driver/lock pid, no live session
pid, no running broker session, broker liveness known). Destructive ones
(★) need the confirm click after the exact commands were shown.

| id | when | runs |
|---|---|---|
| `rerun` | non-terminal STATE, no holds (Omnigent/portable) | `trioctl omnigent loop --mailbox <root mailbox> --max-iterations N` |
| `rerun_more_iterations` | stopped at the iteration cap; N > iteration | same, higher N |
| `reset_and_rerun` ★ | STATE `error` | driver preflighted, STATE → running/idle (reason removed; kept in the action log), then `rerun`; STATE is restored byte-for-byte if the driver does not start |
| `native_resume` (confirm) | claude-workflow run killed mid-run; the recorded session id is a canonical UUID, the run id `wf_…`, and the args pass the strict schema (only the workflow's keys, this mailbox, bounded caps, allowlisted models, the release's helper, a `run_token` — a pre-run_token record offers `native_start` instead) | `<release>/native/launch.sh resume --mailbox … --run-id wf_… --session <uuid>` (launch.sh re-validates and rebuilds its prompt; the preview shows the validated args) |
| `native_start` | claude-workflow held/conflict/budget/cap/interrupted | `<release>/native/launch.sh start --mailbox … --max-iterations N` (never `--helper`) |
| `native_reset_and_start` ★ | claude-workflow STATE `error` | `native_start`'s launcher preflighted, STATE reset, then `native_start` |
| `land` ★ | STATE `needs_land`, no unresolved merge in the Lead worktree | `trioctl omnigent land --mailbox …` |
| `reconcile_dry_run` | held-dispatch records exist | `trioctl omnigent reconcile --mailbox … --json --dry-run` |
| `reconcile_apply` ★ | a fresh dry run (run by the server) says `ready` | `… reconcile --json --apply` |
| `retire_ship` ★ | `needs_retirement`, VERDICT SHIP, product tree clean outside the mailbox | `commit: <HEAD>` in VERDICT.md; `git add <mailbox>`; `git commit -m "loop: iteration N — SHIP"` |
| `repair_scope` | VERDICT `ITERATE scope=local:…`, `.repairs` 1–2 | the driver with `--max-iterations iteration+1` |
| `cleanup_worktrees` ★ | dangling builder worktrees that are clean and merged | `git worktree remove <path>`; `git branch -d <branch>` |

Native fixes run ONLY the installed release's launcher and helper:
`TRIO_DASH_RELEASE_NATIVE` (a release `native/` dir; legacy
`TRIO_DASH_NATIVE_LAUNCH`, its `launch.sh`) or
`~/.local/share/trio-agent-loop/releases/<CURRENT>/native`. Launcher or
helper paths in the run registry, `.native-result.json` or
`.native-launch.json` are display-only (a note in the fix row); a
mailbox-supplied helper is never passed.

Never automated (no id exists): resolving a NEEDS_HUMAN check,
reconciliation without receipt proof, land-conflict resolution, `abandon`,
`sessions prune`, `acceptance amend --human`, permission/settings changes.
Driver starts run detached in their own session; output goes to
`~/.local/state/trio-dash/loops/<key>/runs/*.log` and their exit code is
appended to the action log. A reset STATE.md is restored byte for byte
(with `reason:`) when the driver exits nonzero at any time before it took
the mailbox (its `.lock/pid`/`owner`, `.session.json` or `.driver.json`
changed, or STATE.md moved on); the reaper watches it until then.

**Answers** are recorded in the dashboard's ledger before HUMAN.md is
appended (`answer-key` 0600 + `answers.jsonl` in the state dir; format and
the same-uid limit: MAILBOX-SCHEMA.md "HUMAN.md"). An entry is verified only
when its header signature and a ledger record match; the drivers pass only
a ledger-verified answer to the stop that is still current (the record binds
GOAL.md, the answered VERDICT.md, its last commit and HEAD) to the roles as a
`## Verified human answer (driver)` block, and the Evaluator that rules on it
consumes it (`consumed.jsonl`; shown as "consumed" in the answer box), so an
answer is never replayed into a later run. Answer text is signed in
canonical form (every line separator a newline). An empty, corrupt, linked or group-readable key is
never replaced or used: answers are refused with the reason (never a 500);
a missing key is generated atomically.

**Diagnose** runs a read-only agent — never Claude — with the loop's mailbox
files, sidecars and driver result, lock/liveness, held records, git state,
the unblock table and the allowlist, and stores its JSON answer
(`{diagnosis, state, evidence[], proposed_fix: {id, args, commands_preview,
destructive}, needs_human_input, question?}`) per loop. Unknown or
never-automated ids are marked `rejected`; `destructive` is taken from the
allowlist. The drawer shows only the commands the server planned itself
("Server-validated commands"); the agent's own command text sits under a
collapsed, labelled "Agent said (unverified agent text …)" section.
Harnesses (`TRIO_DASH_DIAGNOSE_HARNESS`, default `codex`):
- Codex (default): `codex exec -m gpt-6-luna -c model_reasoning_effort="high"
  -s read-only -c approval_policy="never" --ephemeral --skip-git-repo-check
  --ignore-user-config --ignore-rules -C <repo> --json -o <file> -` (prompt
  on stdin; OS read-only sandbox, no network, no user MCP config, no
  execpolicy rules). `TRIO_DASH_CODEX`, `TRIO_DASH_CODEX_MODEL` (allowlist:
  `gpt-6-luna`), `TRIO_DASH_CODEX_EFFORT` (low/medium/high/xhigh/max).
- Cursor (opt-in, needs `accept_exposure: true` after the UI shows its
  warning): `cursor-agent -p --mode ask --output-format stream-json --model
  cursor-grok-4.6-low --workspace <repo> --trust --sandbox enabled` with an
  isolated `HOME`, `CURSOR_CONFIG_DIR` and `CURSOR_DATA_DIR` under
  `~/.local/state/trio-dash/cursor-isolated/` (a `cli-config.json` with no
  allowed tools and Shell/Write/WebFetch/MCP denied); `XDG_CONFIG_HOME`
  stays the user's so the CLI finds its own login. Verified: the user's
  `~/.cursor/mcp.json` servers and approvals are gone and writes are
  "Blocked by permissions configuration". NOT removable from the CLI:
  plugins synced from the Cursor account (their MCP servers load again) and
  the built-in WebFetch/WebSearch/Task/dynamic tools — hence the warning.
  `TRIO_DASH_CURSOR_AGENT`, `TRIO_DASH_CURSOR_MODEL` (allowlist:
  `cursor-grok-4.6-low`). An override outside an allowlist refuses the
  diagnosis (never a Claude model).
The server snapshots every file under the live mailbox (recursively,
without following links) plus the checkout's HEAD, index, refs, diff and
full status (ignored files included) before and after, and flags any change
while the loop was stopped. Timeout `TRIO_DASH_DIAGNOSE_TIMEOUT` (900 s).

**Answer box** — for NEEDS_HUMAN/BLOCKED loops with no live driver: appends
a signed entry (`## <UTC> — answer <id> — iteration <N> — trio-dash <hmac>`,
the text quoted with `> `) to the live mailbox's `HUMAN.md` and (default)
resets STATE.md to `status: running`, `phase: idle`,
`human_answer: HUMAN.md#<id>`; the Lead applies the newest current entry at
the start of its next pass and the Evaluator counts it as evidence for the
`verify: human` check it answers (MAILBOX-SCHEMA.md "HUMAN.md", including
stop binding and consumption). With held-dispatch records only the answer is written.

**Deploying to the service** (not applied by this change):
`dashboard/service/point-at-release.sh` checks that the installed release's
dashboard supports the service (`--discover`, guards, `loop_actions.py`,
`service/run`) and, with `--apply`, points `TRIO_DASH_CHECKOUT` in
`~/.services/trio-dash/env` at `~/.local/share/trio-agent-loop/releases/<CURRENT>`
and installs the release's `run` (backups with unique stamps, rollback
lines printed — including `rm -f run` when there was none); then
`svc restart trio-dash`. A release older than dash-actions is refused;
CURRENT must be one hex id inside `releases/`; the value is written
single-quoted (never via sed or eval).

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

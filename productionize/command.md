# /trio-productionize — production-readiness audit

Run the production-readiness graph against the current project. The driver
owns plan, batch, and verdict state; agents do the checking. **You
orchestrate; the driver records. Never eyeball a verdict — every node ends
with a `record`/`record-batch` call carrying evidence.**

## Paths

```sh
PZ_HOME="${TRIO_PZ_HOME:-$HOME/.local/share/trio-agent-loop/productionize}"
```

- `$PZ_HOME/graph/` — the readiness graph (schema: `$PZ_HOME/graph/SCHEMA.md`)
- `$PZ_HOME/driver/run.py` — the state machine (plan|batches|record|record-batch|triage|status|report)
- `$PZ_HOME/driver/validate_graph.py` — graph validator
- `$PZ_HOME/glossary/<domain>.md` — per-domain best-practice glossary entries
  (every node's `glossary_ref` points here; agents may consult it for
  trade-off detail but NEVER guess node ids from it)
- Run state lives in the target project: `pz-run/` (or `pz-run-<slug>/` if
  one exists and may be live — same one-mailbox-per-loop rule as trio-init).

If `$PZ_HOME/graph` is missing, STOP and tell the user to install the
productionize assets (`install.sh --productionize`, or any harness install
flag, from the agent-trio-template repo).

## Target

`$ARGUMENTS` may carry `tags=a,b,c` (stack tags from the schema's
`applies_if` set: web-api, spa, cli, library, mobile, ml-service,
data-pipeline, monorepo). If tags are absent, interview the user briefly
(what is deployed, who consumes it, expected scale) and derive them.

## Setup

1. `python3 $PZ_HOME/driver/validate_graph.py` — abort on errors.
2. Create `pz-run/` and write `pz-run/profile.json`:
   `{"name": "<slug>", "tags": [...]}`.
3. `python3 $PZ_HOME/driver/run.py plan --profile pz-run/profile.json --out pz-run/plan.json`.
   Report the pruning result: node count kept/pruned, clusters.
4. `python3 $PZ_HOME/driver/run.py batches --plan pz-run/plan.json --out pz-run/batches`.
   Writes one briefing file per domain chunk (probes) plus judgment
   briefings in `pz-run/jbatches/`, and an `index.json` mapping every node
   to its batch file. User-decision nodes are never batched to agents.

## Execution loop

Walk probe batches first, then judgment batches, then user decisions.

- **depends_on**: a node's checks assume their dependencies were evaluated
  first; include their verdicts as context when dispatching a dependent.
  A failed dependency does NOT block the dependent — context, not a gate.
- **cluster**: all nodes sharing a cluster go in ONE agent task — same
  concern viewed from different domains. Still record one verdict per id.
- **Briefing rule** (dogfood-proven ×2): NEVER point agents at raw
  plan.json — it poisons their context on large plans. Point each agent at
  its batch file; never guess node ids from the glossary.
- **Executor dispatch**: the plan's `executor` field is `scout` (probes),
  `assessor:<tier>` (judgment), or `user`. Map these to this harness's
  mechanisms per the **dispatch table in your harness wrapper** (the file
  that pointed you here). Probe agents run the node's `probe` recipe
  against the repo (and live system, if reachable) and return verdict +
  concrete evidence (file:line, command output, measurement). Assessors
  judge quality/adequacy against the profile, using recorded probe verdicts
  as their evidence base (`grep pz-run/verdicts.jsonl`). `user` nodes: ask
  the user directly; record their decision verbatim as evidence.
- **Known-context preamble** (dogfood-proven): by mid-run you know the
  repo's shape. Put an authoritative KNOWN CONTEXT block in each dispatch
  (architecture, what infra is absent, key file:line anchors) and tell the
  agent to cite it as `na`-evidence instead of re-deriving it. Turns tail
  batches from 20-minute context-blowout risks into ~1-minute runs.
- **Context caps** (dogfood-proven): every dispatch forbids opening
  plan.json, files >100KB, lockfiles, and build/dependency dirs;
  grep-first, read ≤40-line windows. A scout that reads whole crate files
  dies to context overflow mid-batch and delivers nothing.
- **Delivery-first persistence** (dogfood-proven, rate-limit survival):
  every agent MUST write its verdict array to
  `pz-run/results/<batch-file-stem>.json` BEFORE composing its final reply,
  and you record from that file. Agent crashes, context deaths, and
  provider rate-limit kills then lose nothing; a dead agent whose results
  file exists is a SUCCESS. Accept partial arrays — record what landed,
  re-dispatch only the gaps.
- **Concurrency**: default 1–3 agents at once. On a shared/rate-limited
  provider, sequential lean batches beat parallel thorough ones. Scale up
  only if no rate-limit errors appear.
- After each batch:
  `python3 $PZ_HOME/driver/run.py record-batch --state pz-run/verdicts.jsonl --plan pz-run/plan.json --file pz-run/results/<stem>.json`.
  It validates every id against the plan and reports skips — a skipped id
  means the agent invented or mistyped it; re-dispatch that node.
- `na` when the node's premise does not hold in this repo (evidence
  required), never as a shortcut. `blocked` only when the check genuinely
  cannot run (say what's missing). Re-recording an id is allowed — latest
  verdict wins, so corrections are cheap.

## Close-out

1. `python3 $PZ_HOME/driver/run.py status --state pz-run/verdicts.jsonl --plan pz-run/plan.json`
   — only user-decision nodes may remain pending while waiting on the user.
2. `python3 $PZ_HOME/driver/run.py report --state pz-run/verdicts.jsonl --plan pz-run/plan.json --out pz-run/REPORT.md`.
3. **Triage with the user** — never seed fixes from an untriaged report.
   Walk the failures and record one decision per item via
   `run.py triage --state pz-run/verdicts.jsonl --plan pz-run/plan.json`
   (bulk JSON array on stdin: `[{"node": ..., "action": ..., "note": ...}]`,
   or `--node/--action/--note` for one). Actions: `fix` (goes into the fix
   GOAL), `defer` (stays open), `accept-risk` (documented acceptance),
   `dispute` (user rejects the verdict — re-records it as `na` with the
   user's rationale as evidence; latest-wins keeps the audit honest). Only
   fails are triageable. `status` reports the untriaged-failure count; loop
   until the user has decided everything they care about (the rest may be
   bulk-deferred).
4. Commit the `pz-run/` artifacts (verdicts, triage, report).
5. Seed the fix path from `fix`-triaged items ONLY: they become the GOAL
   for `/trio-init` + `/trio`. Do not start fixing in this loop.
6. **After fixes ship, re-verify**: `pz-run/batches/index.json` maps every
   node to its batch file — re-dispatch only the batches containing fixed
   nodes and record fresh verdicts (latest-wins flips them). The audit is
   a regression suite, not a one-shot.

---
description: Productionize the current project — run the production-readiness graph against the repo, batching probe checks to scouts and escalating judgment items per profile tier, then hand failures to the trio loop.
---

Run the production-readiness graph (`productionize/graph/`, schema in
`productionize/graph/SCHEMA.md`) against the current project. The driver
(`productionize/driver/run.py`) owns plan, batch, and verdict state; agents
do the checking. **You orchestrate; the driver records. Never eyeball a
verdict — every node ends with a `record`/`record-batch` call carrying
evidence.**

**Target**: the current project. `$ARGUMENTS` may carry `tags=a,b,c`
(stack tags from the schema's `applies_if` set: web-api, spa, cli, library,
mobile, ml-service, data-pipeline, monorepo). If tags are absent, interview
the user briefly (what is deployed, who consumes it, expected scale) and
derive them.

## Setup

1. Run `python3 productionize/driver/validate_graph.py`; abort on errors.
2. Create the run directory `pz-run/` (or `pz-run-<slug>/` if one exists and
   may be live — same one-mailbox-per-loop rule as `/trio-init`). Write
   `pz-run/profile.json`: `{"name": "<slug>", "tags": [...]}`.
3. `python3 productionize/driver/run.py plan --profile pz-run/profile.json --out pz-run/plan.json`.
   Report the pruning result to the user: node count kept/pruned, clusters.
4. `python3 productionize/driver/run.py batches --plan pz-run/plan.json --out pz-run/batches`.
   This writes one briefing file per domain chunk (probes) plus judgment
   briefings in `pz-run/jbatches/`, and an `index.json`. User-decision nodes
   are never batched to agents.

## Execution loop

Walk probe batches first, then judgment batches, then user decisions.
Respect the graph:

- **depends_on**: a node's checks assume its dependencies were evaluated
  first; include their verdicts as context when dispatching a dependent.
  A failed dependency does NOT block the dependent — it is context, not a
  gate (the graph models substrate, not pass/fail chains).
- **cluster**: all nodes sharing a cluster go in ONE agent task — they are
  the same concern viewed from different domains. Still record one verdict
  per node id.
- **Briefing rule** (dogfood-proven ×2): NEVER point agents at the raw
  plan.json — it poisons their context on large plans. Point each agent at
  its batch file from step 4; never guess node ids from the glossary.
- **Executor dispatch**:
  - `scout` (probe nodes) → scout agents. The probe field is the recipe:
    run the commands/queries against the repo (and live system, if one is
    reachable), return verdict + concrete evidence (file:line, command
    output, measurement).
  - `assessor:<tier>` (judgment nodes) → an assessor at that tier or higher.
    It judges quality/adequacy against the profile, using the recorded
    probe verdicts as its evidence base (`grep pz-run/verdicts.jsonl`).
  - `user` (user-decision nodes) → ask the user directly (ask tool); record
    their decision verbatim as evidence.
- **Known-context preamble** (dogfood-proven): by mid-run you know the
  repo's shape. Put an authoritative KNOWN CONTEXT block in each dispatch
  (architecture, what infra is absent, key file:line anchors) and tell the
  agent to cite it as `na`-evidence instead of re-deriving it. This turns
  tail batches from 20-minute context-blowout risks into ~1-minute runs.
- **Context caps** (dogfood-proven): every dispatch forbids opening
  plan.json, files >100KB, lockfiles, and build/dependency dirs; grep-first,
  read ≤40-line windows. A scout that reads whole crate files dies to
  context overflow mid-batch and delivers nothing.
- **DM-first delivery** (dogfood-proven, rate-limit survival): instruct
  every agent to send its JSON verdict array to Main via hub DM *before*
  its formal yield. Under provider rate limits the formal yield dies to
  retry exhaustion after the work is done; the DM is what saves the batch.
  Treat a formally-failed agent whose DM you recorded as SUCCESS, and
  accept partial arrays — record what arrived, re-dispatch only the gaps.
- **Concurrency**: default 1–3 agents at once. On a shared/rate-limited
  provider, sequential lean batches beat parallel thorough ones. Scale up
  only if no rate-limit errors appear.
- After each batch: `python3 productionize/driver/run.py record-batch --state pz-run/verdicts.jsonl --plan pz-run/plan.json < array.json`
  (or pipe the array on stdin). It validates every id against the plan and
  reports skips — a skipped id means the agent invented or mistyped it;
  re-dispatch that node. Use `na` when the node's premise does not hold in
  this repo (evidence required), never as a shortcut; `blocked` only when
  the check genuinely cannot run (say what's missing). Re-recording an id
  is allowed — latest verdict wins, so corrections are cheap.

## Close-out

1. `python3 productionize/driver/run.py status --state pz-run/verdicts.jsonl --plan pz-run/plan.json`
   — only user-decision nodes may remain pending while waiting on the user.
2. `python3 productionize/driver/run.py report --state pz-run/verdicts.jsonl --plan pz-run/plan.json --out pz-run/REPORT.md`.
3. Commit the `pz-run/` artifacts.
4. Present the report and propose the fix path: critical failures become
   the GOAL for `/trio-init` + `/trio` (fixes dispatch per the
   orchestration policy — cheapest tier whose verifier can prove the fix).
   Do not start fixing in this loop.

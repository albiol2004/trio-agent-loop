---
description: Productionize the current project — run the production-readiness graph against the repo, batching probe checks to scouts and escalating judgment items per profile tier, then hand failures to the trio loop.
---

Run the production-readiness graph (`productionize/graph/`, schema in
`productionize/graph/SCHEMA.md`) against the current project. The driver
(`productionize/driver/run.py`) owns plan and verdict state; agents do the
checking. **You orchestrate; the driver records. Never eyeball a verdict —
every node ends with a `record` call carrying evidence.**

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

## Execution loop

Walk the plan in its topological order, batched by executor. **Batch
aggressively**: one scout agent per 5-8 probe nodes in the same domain, one
message per node verdict. Respect the graph:

- **depends_on**: a node's checks assume its dependencies were evaluated
  first; include their verdicts as context when dispatching a dependent.
  A failed dependency does NOT block the dependent — it is context, not a
  gate (the graph models substrate, not pass/fail chains).
- **cluster**: all nodes sharing a cluster go in ONE agent task — they are
  the same concern viewed from different domains. Still record one verdict
  per node id.
- **Batch briefing rule** (dogfood-proven): NEVER point agents at the raw
  plan.json — it poisons their context on large plans. Extract each batch's
  node ids + probe recipes mechanically from plan.json and inline them in the
  agent prompt; also name large vendored/generated files the agent must not
  open. Never guess node ids from the glossary — read them from the plan.
- **Executor dispatch**:
  - `scout` (probe nodes) → light-tier scout agents. The probe field is the
    recipe: run the commands/queries against the repo (and live system, if
    one is reachable), return verdict + concrete evidence (file:line,
    command output, measurement).
  - `assessor:<tier>` (judgment nodes) → an assessor at that tier or higher.
    It inspects the evidence named in the probe field and renders a
    reasoned verdict.
  - `user` (user-decision nodes) → ask the user directly; record their
    decision verbatim as evidence.
- After each verdict: `python3 productionize/driver/run.py record --state pz-run/verdicts.jsonl --node <id> --verdict pass|fail|na|blocked --evidence "<concrete evidence>"`.
  Use `na` when the node's premise does not hold in this repo (evidence
  required — e.g. "no outbound HTTP calls found"), never as a shortcut.
  Use `blocked` only when the check genuinely cannot run (say what's
  missing).

## Close-out

1. `python3 productionize/driver/run.py status --state pz-run/verdicts.jsonl --plan pz-run/plan.json`
   — every node must be decided; loop until pending = 0.
2. Write `pz-run/REPORT.md`: failures ordered critical → important →
   nice-to-have, each with evidence and its glossary_ref; then blocked
   items with what would unblock them.
3. Present the report and propose the fix path: critical failures become
   the GOAL for `/trio-init` + `/trio` (fixes dispatch per the
   orchestration policy — cheapest tier whose verifier can prove the fix).
   Do not start fixing in this loop.

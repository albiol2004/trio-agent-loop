---
description: Run the production-readiness graph against this project, then report failures ordered by severity.
agent: trio-orchestrator
subtask: true
---

Run the productionize workflow against the current project, following the
canonical procedure in `omp/commands/trio-productionize.md`. In short:

1. Derive stack tags (interview the user if `$ARGUMENTS` lacks `tags=...`),
   write `pz-run/profile.json`, and generate the plan with
   `python3 productionize/driver/run.py plan`.
2. Walk the plan in topological order, batching 5-8 same-domain probe nodes
   per scout agent, one task per cluster, judgment nodes to assessors at
   their min_tier, user-decision nodes to the user.
3. Record every node verdict with evidence via the driver's `record`
   subcommand; loop until status shows zero pending.
4. Write `pz-run/REPORT.md` (failures critical-first with evidence and
   glossary refs) and propose `/trio-init` + `/trio` as the fix path for
   critical failures. Never fix inside this loop, and never emit a verdict
   without a corresponding driver `record` call.

---
description: Run the production-readiness graph through the shared Trio productionize procedure.
---

# Omp Trio productionize

Read and follow `$PZ_HOME/command.md`; it is the canonical productionize procedure and owns the graph, driver, batching, recording, and close-out rules.

## Dispatch table

- `executor: scout` probe nodes: use `task(agent:"scout")` with the batch briefing and known context.
- `executor: assessor:standard` and `executor: assessor:high` judgment nodes: use the default `task` agent with the batch briefing and recorded probe evidence.
- `executor: user` nodes: ask through the `ask` tool, then record the user's decision verbatim as evidence.

Do not dispatch from raw `plan.json`; give agents their batch briefing and enforce the command's context caps. The orchestrator records each result with the driver's `record-batch`.

## Delivery

Every agent MUST write its verdict array to `pz-run/results/<batch-stem>.json` before composing its final reply. Before its formal yield, it MUST also hub-DM the raw verdict JSON array to `Main`; record from the file or DM, accepting partial arrays and re-dispatching only gaps.

---
name: trio-productionize
description: Run the production-readiness graph through the shared Trio productionize procedure.
---

# Claude Trio productionize

Read and follow `$PZ_HOME/command.md`; it is the canonical productionize procedure and owns the graph, driver, batching, recording, and close-out rules.

## Dispatch table

- `executor: scout` probe nodes: use the `Task` tool with `agent: trio-scout`, passing the batch briefing and known context.
- `executor: assessor:standard` judgment nodes: use the `Task` tool with `agent: trio-lead`.
- `executor: assessor:high` judgment nodes: use the `Task` tool with `agent: trio-evaluator`.
- `executor: user` nodes: ask through `AskUserQuestion`, then record the user's decision verbatim as evidence.

Do not dispatch from raw `plan.json`; give agents their batch briefing and enforce the command's context caps. The orchestrator records each result with the driver's `record-batch`.

## Delivery

Every agent MUST write its verdict array to `pz-run/results/<batch-stem>.json` before composing its final reply. Record from that file, accepting partial arrays and re-dispatching only gaps.

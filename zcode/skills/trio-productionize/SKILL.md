---
name: trio-productionize
description: Run the production-readiness graph through the shared Trio productionize procedure.
---

# ZCode Trio productionize

Read and follow `$PZ_HOME/command.md`; it is the canonical productionize procedure and owns the graph, driver, batching, recording, and close-out rules.

## Dispatch table

ZCode Trio uses the native `Agent` tool and its enabled custom subagents:

- `executor: scout` probe nodes: invoke `Agent` with custom subagent `trio-scout` and the batch briefing.
- `executor: assessor:standard` judgment nodes: invoke `Agent` with custom subagent `trio-lead`.
- `executor: assessor:high` judgment nodes: invoke `Agent` with custom subagent `trio-evaluator`.
- `executor: user` nodes: ask in-session and record the user's decision verbatim as evidence.

Keep the ZCode native-only policy; do not invoke a headless CLI or portable driver. The orchestrator records each result with the driver's `record-batch`.

## Delivery

Every agent MUST write its verdict array to `pz-run/results/<batch-stem>.json` before composing its final reply. Record from that file, accepting partial arrays and re-dispatching only gaps.

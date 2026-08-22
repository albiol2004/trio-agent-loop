---
name: trio-productionize
description: Run the production-readiness graph through the shared Trio productionize procedure.
---

# Codex Trio productionize

Read and follow `$PZ_HOME/command.md`; it is the canonical productionize procedure and owns the graph, driver, batching, recording, and close-out rules.

## Dispatch table

- `executor: scout` probe nodes: when native spawn controls exist, use the custom `trio-scout` agent. Otherwise use the Codex Trio skill's `scripts/run-role.sh` fallback with role `scout`.
- `executor: assessor:standard` judgment nodes: when native spawn controls exist, use the custom `trio-lead` agent; otherwise use that same `scripts/run-role.sh` fallback with role `lead`.
- `executor: assessor:high` judgment nodes: when native spawn controls exist, use the custom `trio-evaluator` agent; otherwise use the fallback with role `evaluator`.
- `executor: user` nodes: ask in-session and record the user's decision verbatim as evidence.

Use the batch briefing rather than raw `plan.json`, and keep the Codex Trio preflight and fallback capability rules. The orchestrator records each result with the driver's `record-batch`.

## Delivery

Every agent MUST write its verdict array to `pz-run/results/<batch-stem>.json` before composing its final reply. Record from that file, accepting partial arrays and re-dispatching only gaps.

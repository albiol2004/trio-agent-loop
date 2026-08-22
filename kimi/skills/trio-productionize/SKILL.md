---
name: trio-productionize
description: Run the production-readiness graph through the shared Trio productionize procedure.
type: prompt
whenToUse: When the user asks Kimi Code to audit production readiness with Trio
---

# Kimi Code Trio productionize

Read and follow `$PZ_HOME/command.md`; it is the canonical productionize procedure and owns the graph, driver, batching, recording, and close-out rules.

## Dispatch table

Kimi's Trio skill uses its sequential CLI runner because custom role subagents are unavailable. Use `${KIMI_SKILL_DIR}/scripts/run-role.sh` for every agent dispatch:

- `executor: scout` probe nodes: run role `scout` with the batch briefing.
- `executor: assessor:standard` judgment nodes: run role `lead` with the batch briefing and recorded probe evidence.
- `executor: assessor:high` judgment nodes: run role `evaluator` with the batch briefing and recorded probe evidence.
- `executor: user` nodes: ask in-session and record the user's decision verbatim as evidence.

Run roles sequentially, never dispatch Kimi's built-in `coder`, `explore`, or `plan` agents for these roles. The orchestrator records each result with the driver's `record-batch`.

## Delivery

Every agent MUST write its verdict array to `pz-run/results/<batch-stem>.json` before composing its final reply. Record from that file, accepting partial arrays and re-dispatching only gaps.

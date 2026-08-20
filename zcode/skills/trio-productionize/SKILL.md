---
name: trio-productionize
description: Run a production-readiness audit of the current project against the production-readiness graph — batched probe scouts, tiered judgment assessors, user triage, then hand fix-triaged failures to the trio loop. Use when the user invokes /trio-productionize or asks to productionize the project.
---

Run a production-readiness audit of the current project: the
production-readiness graph is checked node-by-node by batched agents, the
driver owns all state, failures are triaged with the user, and `fix`-triaged
items seed the trio loop.

**First, read and follow `$PZ_HOME/command.md`** where
`PZ_HOME="${TRIO_PZ_HOME:-$HOME/.local/share/trio-agent-loop/productionize}"`
(`cat "$PZ_HOME/command.md"`). It is the canonical procedure: setup, batch
generation, the execution-loop rules (briefing rule, known-context preamble,
context caps, delivery-first persistence, concurrency), recording, triage,
and close-out. If `$PZ_HOME/command.md` is missing, stop and tell the user
to install the assets (`install.sh --productionize` from the
agent-trio-template repo).

## Dispatch (this harness)

Use ZCode's native custom subagents (same agents the trio skill uses):
- `scout` (probe batches) → **trio-scout** subagent, one per batch file in
  `pz-run/batches/`.
- `assessor:<tier>` (judgment batches) → **trio-lead** (standard) /
  **trio-evaluator** (high) subagent.
- `user` nodes → ask the user in-session; record the decision verbatim.

## Delivery (binding on every dispatched agent)

Every agent MUST write its verdict JSON array to
`pz-run/results/<batch-file-stem>.json` BEFORE composing its final reply;
you record from that file with the driver's `record-batch`. A dead agent
whose results file exists is a success — record and move on; re-dispatch
only the gaps.

---
description: Productionize the current project — run the production-readiness graph against the repo, batching probe checks to scouts and escalating judgment items per profile tier, triage failures with the user, then hand fix-triaged items to the trio loop.
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

## Dispatch (omp)

- `scout` (probe batches) → `task` subagents with `agent: "scout"`, one per
  batch file in `pz-run/batches/`.
- `assessor:<tier>` (judgment batches in `pz-run/jbatches/`) → default `task`
  agent (standard) or a stronger model (high tier).
- `user` nodes → the `ask` tool; record the decision verbatim.

## Delivery (binding on every dispatched agent)

Every agent MUST write its verdict JSON array to
`pz-run/results/<batch-file-stem>.json` BEFORE composing its final reply;
you record from that file with the driver's `record-batch`. A dead agent
whose results file exists is a success — record and move on; re-dispatch
only the gaps.

In addition to the results file, instruct every agent to **hub-DM the raw
verdict JSON array to Main before its formal yield** — under provider rate
limits the formal yield dies to retry exhaustion after the work is done; the
DM is what saves the batch. Treat a formally-failed agent whose DM or results
file you recorded as SUCCESS.

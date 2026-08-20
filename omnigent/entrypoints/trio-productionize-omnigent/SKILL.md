---
name: trio-productionize-omnigent
description: Run a Cursor-backed Omnigent production-readiness audit from the current Claude/Codex UI session when the user explicitly says "Productionize Omnigent", "Omnigent Productionize", or invokes /trio-productionize-omnigent. Do not use for an ordinary native productionize run.
---

You are the audit coordinator. Stay in the current Claude Code or Codex
session; never launch a separate coordinator with `omnigent run`.

**First, read and follow `$PZ_HOME/command.md`** where
`PZ_HOME="${TRIO_PZ_HOME:-$HOME/.local/share/trio-agent-loop/productionize}"`.
It is the canonical procedure: setup, batch generation, execution-loop rules,
recording, triage, and close-out. This skill carries only the Omnigent
dispatch. If `$PZ_HOME/command.md` is missing, stop and tell the user to
install the assets (`install.sh --productionize` from the template repo).

The active trioctl profile (`TRIOCTL_CONFIG`, default
`~/.config/trio-agent-loop/omnigent.toml`) decides which provider/models serve
probes vs judgments — switching work/personal profiles retargets the audit.

## Preflight

1. `trioctl omnigent doctor` — stop on any failed check.
2. Resolve every role you will dispatch:
   `trioctl omnigent resolve scout --json`,
   `trioctl omnigent resolve builder --json`,
   `trioctl omnigent resolve lead --json`,
   `trioctl omnigent resolve evaluator --json`.
   Use returned model/effort exactly; never `--allow-fallback`.
3. Confirm the trio-omnigent Lead/Evaluator registration exactly as the
   `trio-omnigent` skill's preflight describes (registry.json, `_profile`
   marker). Assessors reuse those registered roles.

## Dispatch (Omnigent)

- `scout` (probe batches) → ephemeral headless Cursor workers via
  `trioctl omnigent run scout --prompt-file <briefing>` — one worker per
  batch file in `pz-run/batches/`. The briefing MUST order the worker to
  write its verdict JSON array to `pz-run/results/<batch-stem>.json` before
  exiting. Workers are disposable.
- `assessor:<tier>` (judgment batches in `pz-run/jbatches/`) → Omnigent child
  sessions via `sys_session_create` with the registered trio-omnigent Lead
  (standard tier) or Evaluator (high tier) `agent_id`, model/effort from
  `trioctl omnigent resolve`. Require the assessor to read its batch file,
  reason over `pz-run/verdicts.jsonl` evidence, and write
  `pz-run/results/<batch-stem>.json` before finishing.
- `user` nodes → ask the user in this session; record verbatim.

## Rate-limit rules (dogfood-proven on the Cursor pool)

- Default SEQUENTIAL workers, at most 2 concurrent. Parallel worker waves
  saturate the account TPM ceiling and die mid-batch.
- A worker/session that fails formally but left a results file is a SUCCESS —
  record it with the driver's `record-batch` and move on. Re-dispatch only
  missing node ids (record-batch reports skips).
- Keep briefings lean: point at the batch file, give a KNOWN-CONTEXT
  preamble, cap reads (≤40-line windows, no files >100KB, never plan.json).
- If a child session's result is `failed (exit N)` from a provider transport
  error (`resource_exhausted`, `NGHTTP2_INTERNAL_ERROR`, `stream refused`),
  wake it ONCE via `hub send` with `continue` before treating it as failed.

`sys_session_create` is asynchronous: use inbox/session-history tools and end
the turn while an assessor runs; do not busy-poll.

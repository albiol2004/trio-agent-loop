---
description: Run one or more iterations of the Trio agent loop using the loop/ mailbox files. /trio = one supervised iteration; /trio auto = keep iterating in-session until SHIP, BLOCKED, or NEEDS_HUMAN.
---

You are the **orchestrator** of the Trio two-agent loop. You do no planning, implementing, or evaluating yourself — you sequence the two role agents, enforce stop conditions, and report to the human.

**Usage**: `/trio` runs exactly one supervised iteration. `/trio auto` keeps iterating in the same turn until the latest `loop/VERDICT.md` first line is `VERDICT: SHIP`, `VERDICT: BLOCKED`, or `VERDICT: NEEDS_HUMAN`, honoring the stop conditions below. `/trio dir=loop-<name>` uses a different mailbox directory (combinable: `/trio auto dir=loop-x`); every `loop/` reference below means that directory.

If you are coming from the Claude bundle, `/trio auto` replaces the old `/loop /trio` autonomous invocation.

## 0. Preflight — decide whether to run at all
> **Default mode**: a natural-language run-the-loop request ("run the trio loop", "start it", "continue", "keep iterating") follows `/trio auto` semantics — full chain, no per-iteration checkpoints, until SHIP/BLOCKED/NEEDS_HUMAN. Bare `/trio` stays supervised (exactly one iteration).

1. Read `loop/STATE.md`. If it does not exist, tell the user to run `/trio-init <goal>` first and STOP.
   - **Collision check**: if STATE.md has a `mission:` line, verify it still matches GOAL.md's mission sentence, and verify LOG.md's tail is consistent with the iterations you've been orchestrating. A mismatch means another session has repurposed this mailbox mid-loop: STOP immediately, do not write anything, and tell the human — the fix is separate mailbox dirs (`/trio-init dir=loop-<name> …`), never sharing one.
2. Read `loop/VERDICT.md` if it exists — its **first line** is machine-readable (`VERDICT: SHIP|ITERATE|NEEDS_HUMAN|BLOCKED`, with an optional `scope=design` or `scope=local:<paths>` suffix on ITERATE); trust that line, not your reading of the prose. Apply stop conditions **before** doing any work:
   - Last verdict `SHIP` → announce completion (quote the Evaluator's suggested commit message and follow-ups), end the loop.
   - Last verdict `BLOCKED` → surface the Evaluator's "what the human must decide" section to the user, end the loop.
   - Last verdict `NEEDS_HUMAN` → surface the Evaluator's `## Human check` section to the user with its exact steps; the loop pauses until the human confirms or redirects. Do not start another iteration in the same turn.
   - `iteration >= max_iterations` in STATE.md → stop, summarize LOG.md, tell the user how to raise the cap in STATE.md and resume.
   - **Plateau check**: research shows iterate-loop gains flatten after ~3 iterations. From iteration 4 on, if the last verdict's "what changed" section shows the same checks still failing, treat it as BLOCKED (stuck) rather than starting another iteration.
3. Otherwise increment `iteration` in STATE.md, set `status: running`, and proceed.

## 1. Lead or Repair (plan + delegate implementation + review)
If the last verdict was `VERDICT: ITERATE scope=local:<paths>` and fewer than 2 consecutive repairs have run, dispatch the `trio-repair` agent as a **background job** instead of `trio-lead`: it fixes exactly the listed paths with no re-planning. Track the consecutive count in `loop/.repairs` (driver-internal; start at 1, cap at 2, reset to 0 after any full Lead pass). On the 3rd consecutive scoped verdict, or for any other ITERATE (plain or scope=design), dispatch `trio-lead` as usual. Repair passes are logged like lead passes (role `lead`, summary prefixed `repair:`).

Dispatch the `trio-lead` agent as a **background job** with the task tool. The tasks item MUST carry the `agent` field: `{"agent": "trio-lead", "task": "..."}`. Immediately check the spawn confirmation: if it shows a generated label instead of `trio-lead` (e.g. a random animal name), the `agent` field was dropped — cancel that job and redispatch. Never let a generic agent play a role.

Prompt: the iteration number + instruction to update `loop/PLAN.md`, have one or more `trio-builder` agents perform the main implementation pass for every code-changing increment, review and correct their work as needed, and write `loop/REPORT.md` per its role instructions. Remind it to dispatch `trio-scout` for scoped exploration and that the Lead must not replace the mandatory first implementation pass. Hand it **diagnosed line ranges** (from cheap grep/symbol search) for every product file it must touch — never "read the file" for a large file: first-turn full-file ingest of the 2.1 MB monolith crashed the provider transport twice.

The job result **auto-delivers** when the Lead finishes — do not busy-poll and do not block your turn waiting; end the turn or continue other work, and resume when the result arrives. Before dispatching the Lead, capture the wall-clock start time with bash:
```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

After the Lead's result arrives, read the top of `loop/PLAN.md`: if it contains `Recommendation: SHIP` or `Recommendation: BLOCKED`, the Lead skipped implementation — proceed to step 2 anyway so the Evaluator can confirm or overrule. The Lead proposes, the Evaluator disposes.

### 1a. Append the orchestrator's Lead timing line to LOG.md
The Lead role also appends its own human-readable line to `loop/LOG.md`. You must append a second, **authoritative** Format-A line that carries per-role timing fields. Capture the end time, compute wall-clock seconds, and append:

```text
- iter N | lead | <one-line summary> | started_at: <ISO-8601> | ended_at: <ISO-8601> | duration_sec: <seconds>
```

Use a one-line summary of what the Lead did (keep it ≤ 12 words, no `|` characters). You may either run `bash` with `printf '%s\n' "..." >> loop/LOG.md` or, when the repo-side helper is present, run `bash omp/scripts/trio-log-usage.sh -d loop -i N -r lead -s "..." started_at:... ended_at:... duration_sec:...`.

## 2. Evaluate
Dispatch the `trio-evaluator` agent as a **background job** with the task tool — same rules as the Lead dispatch: the tasks item MUST carry `{"agent": "trio-evaluator", ...}`, verify the spawn confirmation names `trio-evaluator`, and NEVER dispatch it before the Lead's result has arrived (the Evaluator reads the Lead's output files). Its result auto-delivers; do not busy-poll.

Before anything else in this step, run the **commit gate** (active interlock) as the first check:
```bash
trio-shadow.py --mailbox <dir> --require-commits
```
(the script lives in the template repo's `metrics/`; it may be on PATH or referenced by absolute path from the installing repo). Exit 0 → proceed. Exit 1 lists code-changing slices with no `slice(<id>): ` commit — retry the Lead once with the missing-commit note (reusing the retry-once pattern from the hard rules); if the gate still fails, set `status: error` in STATE.md, record the breach in LOG.md, and end the loop.

Then verify `loop/LOG.md` contains the Lead's `- iter N | lead | ...` append for this iteration (the LOG.md gate) — the Evaluator cannot SHIP without it. If the append is missing, have the Lead add it before spawning the Evaluator.

Prompt: iteration number + instruction to verify against `loop/PLAN.md` acceptance criteria and write `loop/VERDICT.md` per its role instructions (own execution first, scouts for blast radius, web checks for API currency).
- The task result carries a structured output object with `verdict` (SHIP/ITERATE/NEEDS_HUMAN/BLOCKED), `summary`, and optional `blocking_issues`. This structured `verdict` is the **authoritative** verdict for the iteration.
- If the structured `verdict` is missing or cannot be parsed, fall back to reading the first line of `loop/VERDICT.md`.
- If the structured `verdict` differs from `loop/VERDICT.md`'s first-line word, the Evaluator breached the mirror contract: retry the Evaluator once with a note about the mismatch. If the mismatch persists, set `status: error` in STATE.md, report the role-contract breach, and end the loop.

Before dispatching the Evaluator, capture the wall-clock start time with bash:
```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

### 2a. Append the orchestrator's Evaluator timing line to LOG.md
The Evaluator role also appends its own human-readable line to `loop/LOG.md`. You must append a second, **authoritative** Format-A line that carries the authoritative verdict and timing fields. After the Evaluator returns, capture the end time and append:

```text
- iter N | evaluator | VERDICT: SHIP — <one-line summary> | started_at: <ISO-8601> | ended_at: <ISO-8601> | duration_sec: <seconds>
```

Use the authoritative `verdict` from the structured output, and a one-line summary from `loop/VERDICT.md` (≤ 12 words, no `|` characters). Use `bash` with `printf` or the repo-side helper `bash omp/scripts/trio-log-usage.sh -d loop -i N -r evaluator -v <SHIP|ITERATE|NEEDS_HUMAN|BLOCKED> -s "..." started_at:... ended_at:... duration_sec:...`.

## 3. Report and schedule
Use the authoritative verdict from step 2. Read `loop/VERDICT.md` for the human-facing detail (suggested commit message, follow-ups, blocking details), update `loop/STATE.md` (`status: <verdict>`, `verdict: <outcome>`, `eval: <one-line compressed evidence>`, `last_run: <date from bash>`), then give the human a compact iteration digest:

`verdict:` is the outcome word (`SHIP|ITERATE|NEEDS_HUMAN|BLOCKED`); `eval:` is ONE compressed line — the key metrics and the evidence directory path (e.g. `loop/evidence/iter<N>/`), never prose paragraphs (schema: MAILBOX-SCHEMA.md).
- Iteration N, verdict, one line each for what was planned / done / found.
- Any `DECISION:` flags the Lead recorded (the human may want to veto).
- The per-role timing fields you just recorded in `loop/LOG.md` (started_at, ended_at, duration_sec).

Then:
- **ITERATE** → if you are in `/trio auto` mode, promptly start the next iteration in the same turn; otherwise tell the user to run `/trio` again or start `/trio auto`. A `scope=local` ITERATE schedules the repair pass next; after 2 consecutive ones the next pass is a full Lead iteration.
- **SHIP / BLOCKED / NEEDS_HUMAN** → end the loop (do not start another iteration) and tell the user why; for NEEDS_HUMAN quote the `## Human check` steps.

## Hard rules
- Never edit the mailbox files yourself except `loop/STATE.md` bookkeeping and the orchestrator usage-log lines in `loop/LOG.md` described above — content belongs to the roles.
- Never fix code yourself, even for a trivial failure; that's the next iteration's job.
- A code-changing Lead run is incomplete unless REPORT.md records at least one `trio-builder` as the primary implementor. Retry the Lead once if that provenance is missing; on a second failure, set `status: error`, report the role-contract breach, and end the loop.
- The two roles run strictly sequentially — never in parallel. Async dispatch does not change this: the Evaluator job is dispatched only after the Lead job's result has been delivered and reviewed; the Evaluator reads the Lead's output files.
- If a role agent dies or returns without writing its file, retry it once with a note about what's missing; if it fails again, set `status: error` in STATE.md, report to the human, end the loop.
- **Auto-resume on provider transport failure:** when a role job's result is `failed (exit N)` and the broker log for that session shows a provider transport error — the observed triggers are `resource_exhausted`, `NGHTTP2_INTERNAL_ERROR`, or `stream refused` — auto-wake the named subagent session ONCE via `hub send` with the message `continue` before surfacing any failure to the user. If the wake is not applicable (job dispatch without a live session) or the session fails again, fall through to the retry rule above.

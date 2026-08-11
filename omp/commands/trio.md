---
description: Run one or more iterations of the Trio agent loop using the loop/ mailbox files. /trio = one supervised iteration; /trio auto = keep iterating in-session until SHIP or BLOCKED.
---

You are the **orchestrator** of the Trio two-agent loop. You do no planning, implementing, or evaluating yourself — you sequence the two role agents, enforce stop conditions, and report to the human.

**Usage**: `/trio` runs exactly one supervised iteration. `/trio auto` keeps iterating in the same turn until the latest `loop/VERDICT.md` first line is `VERDICT: SHIP` or `VERDICT: BLOCKED`, honoring the stop conditions below. `/trio dir=loop-<name>` uses a different mailbox directory (combinable: `/trio auto dir=loop-x`); every `loop/` reference below means that directory.

If you are coming from the Claude bundle, `/trio auto` replaces the old `/loop /trio` autonomous invocation.

## 0. Preflight — decide whether to run at all
1. Read `loop/STATE.md`. If it does not exist, tell the user to run `/trio-init <goal>` first and STOP.
   - **Collision check**: if STATE.md has a `mission:` line, verify it still matches GOAL.md's mission sentence, and verify LOG.md's tail is consistent with the iterations you've been orchestrating. A mismatch means another session has repurposed this mailbox mid-loop: STOP immediately, do not write anything, and tell the human — the fix is separate mailbox dirs (`/trio-init dir=loop-<name> …`), never sharing one.
2. Read `loop/VERDICT.md` if it exists — its **first line** is machine-readable (`VERDICT: SHIP|ITERATE|BLOCKED`); trust that line, not your reading of the prose. Apply stop conditions **before** doing any work:
   - Last verdict `SHIP` → announce completion (quote the Evaluator's suggested commit message and follow-ups), end the loop.
   - Last verdict `BLOCKED` → surface the Evaluator's "what the human must decide" section to the user, end the loop.
   - `iteration >= max_iterations` in STATE.md → stop, summarize LOG.md, tell the user how to raise the cap in STATE.md and resume.
   - **Plateau check**: research shows iterate-loop gains flatten after ~3 iterations. From iteration 4 on, if the last verdict's "what changed" section shows the same checks still failing, treat it as BLOCKED (stuck) rather than starting another iteration.
3. Otherwise increment `iteration` in STATE.md, set `status: running`, and proceed.

## 1. Lead (plan + delegate implementation + review)
Dispatch the `trio-lead` agent with the task tool (`agent: "trio-lead"`) and wait for its result — its frontmatter sets `blocking: true`. Prompt: the iteration number + instruction to update `loop/PLAN.md`, have one or more `trio-builder` agents perform the main implementation pass for every code-changing increment, review and correct their work as needed, and write `loop/REPORT.md` per its role instructions. Remind it to dispatch `trio-scout` for scoped exploration and that the Lead must not replace the mandatory first implementation pass.

Before dispatching the Lead, capture the wall-clock start time with bash:
```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

After the Lead returns, read the top of `loop/PLAN.md`: if it contains `Recommendation: SHIP` or `Recommendation: BLOCKED`, the Lead skipped implementation — proceed to step 2 anyway so the Evaluator can confirm or overrule. The Lead proposes, the Evaluator disposes.

### 1a. Append the orchestrator's Lead timing line to LOG.md
The Lead role also appends its own human-readable line to `loop/LOG.md`. You must append a second, **authoritative** Format-A line that carries per-role timing fields. Capture the end time, compute wall-clock seconds, and append:

```text
- iter N | lead | <one-line summary> | started_at: <ISO-8601> | ended_at: <ISO-8601> | duration_sec: <seconds>
```

Use a one-line summary of what the Lead did (keep it ≤ 12 words, no `|` characters). You may either run `bash` with `printf '%s\n' "..." >> loop/LOG.md` or, when the repo-side helper is present, run `bash omp/scripts/trio-log-usage.sh -d loop -i N -r lead -s "..." started_at:... ended_at:... duration_sec:...`.

## 2. Evaluate
Dispatch the `trio-evaluator` agent with the task tool (`agent: "trio-evaluator"`) and wait for its result. Prompt: iteration number + instruction to verify against `loop/PLAN.md` acceptance criteria and write `loop/VERDICT.md` per its role instructions (own execution first, scouts for blast radius, web checks for API currency).
- The task result carries a structured output object with `verdict` (SHIP/ITERATE/BLOCKED), `summary`, and optional `blocking_issues`. This structured `verdict` is the **authoritative** verdict for the iteration.
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

Use the authoritative `verdict` from the structured output, and a one-line summary from `loop/VERDICT.md` (≤ 12 words, no `|` characters). Use `bash` with `printf` or the repo-side helper `bash omp/scripts/trio-log-usage.sh -d loop -i N -r evaluator -v <SHIP|ITERATE|BLOCKED> -s "..." started_at:... ended_at:... duration_sec:...`.

## 3. Report and schedule
Use the authoritative verdict from step 2. Read `loop/VERDICT.md` for the human-facing detail (suggested commit message, follow-ups, blocking details), update `loop/STATE.md` (`status: <verdict>`, `last_run: <date from bash>`), then give the human a compact iteration digest:
- Iteration N, verdict, one line each for what was planned / done / found.
- Any `DECISION:` flags the Lead recorded (the human may want to veto).
- The per-role timing fields you just recorded in `loop/LOG.md` (started_at, ended_at, duration_sec).

Then:
- **ITERATE** → if you are in `/trio auto` mode, promptly start the next iteration in the same turn; otherwise tell the user to run `/trio` again or start `/trio auto`.
- **SHIP / BLOCKED** → end the loop (do not start another iteration) and tell the user why.

## Hard rules
- Never edit the mailbox files yourself except `loop/STATE.md` bookkeeping and the orchestrator usage-log lines in `loop/LOG.md` described above — content belongs to the roles.
- Never fix code yourself, even for a trivial failure; that's the next iteration's job.
- A code-changing Lead run is incomplete unless REPORT.md records at least one `trio-builder` as the primary implementor. Retry the Lead once if that provenance is missing; on a second failure, set `status: error`, report the role-contract breach, and end the loop.
- The two roles run strictly sequentially — never in parallel; the Evaluator reads the Lead's output files.
- If a role agent dies or returns without writing its file, retry it once with a note about what's missing; if it fails again, set `status: error` in STATE.md, report to the human, end the loop.

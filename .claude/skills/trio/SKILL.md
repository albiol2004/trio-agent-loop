---
name: trio
description: Run ONE full iteration of the duo agent loop (Lead → Evaluator) using the loop/ mailbox files. Designed to be driven by "/loop /trio".
disable-model-invocation: true
---

You are the **orchestrator** of a two-agent loop. You do no planning, implementing, or evaluating yourself — you sequence the two role agents, enforce stop conditions, and report to the human. One invocation of /trio = exactly one iteration.

**Mailbox directory**: default `loop/`. If invoked with `dir=<path>` (e.g. `/trio dir=loop-authz`), that directory is the mailbox — every `loop/` reference below means it, and every role prompt you write MUST name it as an absolute path (fresh-context agents have no other way to find it).

## 0. Preflight — decide whether to run at all
1. Read `loop/STATE.md`. If it does not exist, tell the user to run `/trio-init <goal>` first and STOP (if running under /loop, end the loop by not rescheduling).
   - **Collision check**: if STATE.md has a `mission:` line, verify it still matches GOAL.md's mission sentence, and verify LOG.md's tail is consistent with the iterations you've been orchestrating. A mismatch means another session has repurposed this mailbox mid-loop: STOP immediately, do not write anything, and tell the human — the fix is separate mailbox dirs (`/trio-init dir=loop-<name> …`), never sharing one.
2. Read `loop/VERDICT.md` if it exists — its **first line** is machine-readable (`VERDICT: SHIP|ITERATE|BLOCKED`); trust that line, not your reading of the prose. Apply stop conditions **before** doing any work:
   - Last verdict `SHIP` → announce completion (quote the Evaluator's suggested commit message and follow-ups), end the loop.
   - Last verdict `BLOCKED` → surface the Evaluator's "what the human must decide" section to the user, end the loop.
   - `iteration >= max_iterations` in STATE.md → stop, summarize LOG.md, tell the user how to raise the cap in STATE.md and resume.
   - **Plateau check**: research shows iterate-loop gains flatten after ~3 iterations. From iteration 4 on, if the last verdict's "what changed" section shows the same checks still failing, treat it as BLOCKED (stuck) rather than starting another iteration.
3. Otherwise increment `iteration` in STATE.md, set `status: running`, and proceed.

## 1. Lead (plan + implement)
Spawn the `trio-lead` agent synchronously (run_in_background: false). Prompt: the iteration number + instruction to update `loop/PLAN.md`, implement the increment, and write `loop/REPORT.md` per its role instructions; remind it to delegate scoped work to `trio-scout` / `trio-builder` subagents.

After it returns, read the top of `loop/PLAN.md`: if it contains `Recommendation: SHIP` or `Recommendation: BLOCKED`, the Lead skipped implementation — proceed to step 2 anyway so the Evaluator can confirm or overrule. The Lead proposes, the Evaluator disposes.

## 2. Evaluate
Spawn `trio-evaluator` synchronously. Prompt: iteration number + instruction to verify against `loop/PLAN.md` acceptance criteria and write `loop/VERDICT.md` per its role instructions (own execution first, scouts for blast radius, web checks for API currency).

## 3. Report and schedule
Read `loop/VERDICT.md`, update `loop/STATE.md` (`status: <verdict>`, `last_run: <date from Bash>`), then give the human a compact iteration digest:
- Iteration N, verdict, one line each for what was planned / done / found.
- Any `DECISION:` flags the Lead recorded (the human may want to veto).

Then:
- **ITERATE** → if running under /loop, reschedule promptly (this is active work, not idle polling — short delay); otherwise tell the user to run `/trio` again or start `/loop /trio`.
- **SHIP / BLOCKED** → end the loop (do not reschedule) and tell the user why.

## Hard rules
- Never edit the mailbox files yourself except STATE.md bookkeeping — content belongs to the roles.
- Never fix code yourself, even for a trivial failure; that's the next iteration's job.
- The two roles run strictly sequentially — never in parallel; the Evaluator reads the Lead's output files.
- If a role agent dies or returns without writing its file, retry it once with a note about what's missing; if it fails again, set `status: error` in STATE.md, report to the human, end the loop.

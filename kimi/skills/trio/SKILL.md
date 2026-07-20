---
name: trio
description: Run one supervised Trio iteration with Kimi Code's sequential role runner.
type: prompt
whenToUse: When the user asks to initialize, run, resume, or inspect a Trio mailbox with Kimi Code
---

# Kimi Code Trio

Use the shared Trio mailbox protocol in `loop/` (or the explicitly selected
`loop-<name>/` directory). Kimi Code's current documentation describes only
the fixed built-in `coder`, `explore`, and `plan` sub-agents and automatic
dispatch. It does not document custom role names or per-role model pinning;
this skill therefore treats those capabilities as unavailable and uses a fresh,
sequential CLI fallback: each role is a separate `kimi -m <alias> -p <prompt>` process run by
`${KIMI_SKILL_DIR}/scripts/run-role.sh`.

The fallback keeps the Lead -> primary Builder -> Lead review -> independent
Evaluator contract. It is intentionally sequential rather than an autonomous
Kimi sub-agent loop. Do not dispatch Kimi built-in sub-agents to fill Trio
roles or overlap this sequence.

## One iteration

1. If no mailbox exists, invoke `/skill:trio-init` first. Read `GOAL.md`,
   `STATE.md`, `VERDICT.md`, and any existing plan or report.
2. Ask the user for the project root if it is not the current directory. Create
   short context files inside the mailbox; include the iteration, goal, exact
   task, repository scope, and any prior brief needed by the next role.
3. Run the roles in this order, waiting for each result before starting the
   next: `scout`, initial `lead`, `builder` when the Lead writes
   `DELEGATE: YES`, post-Builder `lead`, evaluator `scout`, then `evaluator`.
   Example:

   ```sh
   "${KIMI_SKILL_DIR}/scripts/run-role.sh" scout loop/scout-context.md loop/scout-result.md .
   ```

   Use the same pattern for the other roles and inspect each result file.
4. Verify that the roles wrote their required mailbox artifacts. The Lead owns
   `PLAN.md`, `BUILDER_TASK.md`, and `REPORT.md`; the Evaluator owns
   `VERDICT.md`; append their required lines to `LOG.md`.
5. Continue only on `VERDICT: ITERATE`; stop on `SHIP`, `BLOCKED`, an iteration
   cap, or a missing/failed child result. Never commit automatically.

Scout is read-only. The initial Lead plans and delegates without editing
product code. Builder performs the substantive implementation in its named
scope. The post-Builder Lead reviews and may correct the implementation. The
Evaluator independently runs checks and grades the diff without fixing it.

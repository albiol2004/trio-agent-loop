---
name: trio
description: Run the Trio workflow entirely through Codex native skills, custom agents, and Goal mode when the user asks for a trio loop. Never launches codex exec or another CLI process.
---

# Native Codex Trio

Use this skill when the user asks to run a Trio loop. Do not invoke
`codex exec`, `portable/driver.sh`, or any child CLI process. Use Codex's
native multi-agent tools and the installed named custom agents.

Initialize or resume the mailbox using the Trio Init contract. When sustained
work is needed, create or reuse a Codex goal for the user's objective; Goal
mode is the outer persistence mechanism, while the mailbox is the auditable
Lead/Evaluator protocol.

For each iteration, orchestrate these native agents synchronously:

1. Spawn `trio-scout` (Luna High) for read-only reconnaissance. Keep its brief.
2. Spawn `trio-lead` (Terra High), passing the goal, mailbox, iteration, and
   scout brief. It plans, implements judgment-heavy work, writes REPORT.md,
   and writes BUILDER_TASK.md as `DELEGATE: NO` or one bounded task.
3. On `DELEGATE: YES`, spawn `trio-builder` (Luna High) for only that task,
   then spawn `trio-lead` again for Terra review and final ownership.
4. Spawn `trio-scout` again for evaluator reconnaissance. It must inspect the
   goal, plan, and actual diff without reading REPORT.md or issuing a verdict.
5. Spawn `trio-evaluator` (Terra High), passing that brief. It independently
   verifies before reading REPORT.md and writes VERDICT.md.

Use the exact custom agent type on every spawn. Never use a generic agent,
inherit the parent model, or override the custom agent's configured model.
The main Codex task owns orchestration; workers must not spawn workers.

Continue on ITERATE until SHIP, BLOCKED, the iteration cap, or the active Goal
budget stops the task. Never commit automatically.

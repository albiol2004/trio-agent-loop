---
name: trio
description: Run Trio through native Codex custom agents when available, with isolated bundled Codex CLI sessions as an explicit fallback when a task does not expose native spawn controls.
---

# Codex Trio

Use this skill when the user asks to run a Trio loop.

## Capability preflight

Before creating a mailbox or Goal, inspect the tools exposed to the current
task:

1. Confirm this skill's `scripts/run-role.sh` exists and is executable. If the
   installed bundle is incomplete, read
   `references/TROUBLESHOOTING.md` and report the reinstall steps; do not
   improvise another runner.
2. If native subagent spawn/control tools are available, use native mode.
3. If they are absent, announce: `Trio mode: isolated Codex CLI fallback
   (native subagent controls are unavailable in this task).`
4. In fallback mode, resolve this skill's directory and use its installed
   `scripts/run-role.sh`. Never stop merely because native spawning is absent,
   and never retry the same capability check through Goal turns.

Do not use `portable/driver.sh`, a generic agent, or single-agent role-play.
The approved fallback creates real, fresh Codex sessions with explicit role
models and the current project's Codex permission configuration.

If project permissions or configuration prevent startup, read
`references/TROUBLESHOOTING.md` and
`references/PROJECT-CONFIG.example.toml`. Diagnose and repair the setup when
the user has authorized setup changes; otherwise provide the exact required
edits.

Initialize or resume the mailbox using the Trio Init contract after the
preflight. When sustained work is needed, create or reuse a Codex goal for the
user's objective. Goal mode keeps the parent task persistent; the mailbox is
the auditable Lead/Evaluator protocol.

## Native mode

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

## CLI fallback mode

For each role invocation:

1. Write a short invocation context file inside the active mailbox. Include
   the mailbox path, iteration, goal, exact task, repository scope, and any
   prior role brief the next role needs.
2. Run:

   ```bash
   <trio-skill-dir>/scripts/run-role.sh <role> <context-file> <result-file> <project-root>
   ```

   Roles are `scout`, `lead`, `builder`, and `evaluator`.
3. Read the result file and verify the role also wrote its required mailbox
   artifacts. Treat a failed or malformed child run as a Trio blocking issue,
   not as permission to impersonate the role in the parent.

The fallback runner pins:

- `lead` and `evaluator`: `gpt-5.6-terra`, high reasoning.
- `scout` and `builder`: `gpt-5.6-luna`, high reasoning.

It uses `codex exec --ephemeral`, inherits the project's active Codex
configuration and permission profile, and never bypasses the sandbox. Child
runs are sequential. The parent remains the only orchestrator.

Continue on ITERATE until SHIP, BLOCKED, the iteration cap, or the active Goal
budget stops the task. Never commit automatically.

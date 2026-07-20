---
name: trio
description: Run the Trio workflow with ZCode Agent's native custom subagents and Goal Mode. Never uses a headless CLI or portable driver.
---

# Native ZCode Trio

Use only ZCode Agent capabilities. Never invoke `portable/driver.sh`, a ZCode
CLI, or another agent executable.

Initialize or resume `loop/` using the Trio Init skill. For sustained work,
use ZCode `/goal` as the outer iteration and verification mechanism.

Within each iteration, use the Agent tool with the exact enabled custom
subagents in this order: `trio-scout`, `trio-lead`, `trio-builder`,
`trio-lead` review, independent `trio-scout`, then `trio-evaluator`.

The first Lead pass plans the approach and must not edit product code. For
every code-changing increment it delegates the main implementation pass to a
Builder, with owned files and objective done-criteria. The Builder may perform
substantive logic, test, and integration work within that brief. The second
Lead pass reviews the full diff, makes corrective edits when needed, verifies
the result, and records primary Builder work separately from Lead corrections
in REPORT.md. Skip the Builder only for a SHIP/BLOCKED recommendation or an
increment that genuinely changes no product code.

Lead/Evaluator own judgment; Scout/Builder remain scoped workers. A
code-changing run without recorded Builder provenance is a role-contract
failure: retry the Lead once, then stop rather than accepting the iteration.
Continue on ITERATE and stop on SHIP, BLOCKED, or the mailbox cap. Never commit.

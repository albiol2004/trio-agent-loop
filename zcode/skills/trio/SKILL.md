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
subagents in this order: `trio-scout`, `trio-lead`, optional `trio-builder`,
`trio-lead` review, independent `trio-scout`, then `trio-evaluator`.
Lead/Evaluator own judgment; Scout/Builder remain scoped workers. Continue on
ITERATE and stop on SHIP, BLOCKED, or the mailbox cap. Never commit.

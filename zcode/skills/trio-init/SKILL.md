---
name: trio-init
description: Initialize a Trio mailbox for a ZCode-native Goal and subagent workflow.
---

Create `loop/GOAL.md`, `STATE.md`, `LOG.md`, and empty PLAN, REPORT, VERDICT,
and BUILDER_TASK files. Put the user's concrete goal, definition of done, and
constraints in GOAL.md; set iteration 0, max_iterations 10, and status ready.
Never overwrite a live mailbox. Use `loop-<name>/` for concurrent work.

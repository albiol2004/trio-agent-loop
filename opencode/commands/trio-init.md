---
description: Initialize a Trio mailbox for a new goal, then report the next command.
agent: trio-orchestrator
subtask: true
---

Initialize or validate the existing Trio mailbox for this project. Create only
the standard `loop/` files required by the repository's established protocol,
preserve any existing user mailbox, and do not implement product code. Record
the supplied goal in `loop/GOAL.md` only when the mailbox is new or the user
explicitly asks to replace it. Then explain how to run `/trio`.

Goal:
$ARGUMENTS

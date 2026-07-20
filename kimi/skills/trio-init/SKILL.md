---
name: trio-init
description: Initialize a Trio mailbox for a Kimi Code session.
type: prompt
whenToUse: When the user asks Kimi Code to start a new Trio loop or create its mailbox
arguments:
  - goal
---

# Initialize a Kimi Code Trio mailbox

Create `loop/` unless the user explicitly names another mailbox directory.
Never overwrite a mailbox whose `STATE.md` indicates that it may be running.

Write:

- `GOAL.md` with `profile: software|data`, the mission `$goal`, definition of
  done, and constraints supplied by the user. Data goals must also state ground
  truth, reconciliation tolerance, key uniqueness, and rerun/idempotence.
- `STATE.md` with iteration 0, `max_iterations: 10`, ready status, a mission
  fingerprint, rejected approaches, and key decisions.
- `LOG.md` containing `# Trio loop log`.
- Empty `PLAN.md`, `REPORT.md`, and `VERDICT.md`.

The mailbox is runtime evidence and must not be committed. Do not put
credentials or private machine paths in it. After initialization, tell the user
to run `/skill:trio` for one supervised iteration.

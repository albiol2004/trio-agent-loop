---
name: trio-init
description: Initialize a Codex Trio mailbox from the user's goal. Use when the user explicitly asks to initialize Trio or when the Trio skill needs a new mailbox.
---

# Initialize a Codex Trio mailbox

Create the mailbox in `loop/`, or in the requested `loop-<name>/` directory.
Never overwrite or repurpose a mailbox whose STATE says it may be running.

Write:

- `GOAL.md`: `profile: software|data`, mission, objective definition of done,
  and constraints. Data goals must name ground truth, reconciliation tolerance,
  key uniqueness, and rerun/idempotence expectations.
- `STATE.md`: iteration 0, max_iterations 10, ready status, mission fingerprint,
  rejected approaches, and key decisions.
- `LOG.md`: `# Trio loop log`.
- Empty `PLAN.md`, `REPORT.md`, and `VERDICT.md`.

Ask whether the mailbox should be committed or ignored. Then tell the user
they can simply say `Run a trio loop on <goal>`; they do not need to invoke
this skill separately in normal use.

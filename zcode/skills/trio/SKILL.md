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
Continue on ITERATE and stop on SHIP, BLOCKED, NEEDS_HUMAN (surface the
mandatory `## Human check` section from VERDICT.md), or the mailbox cap.

The Evaluator writes the verdict word plus an optional scope on the first
line of VERDICT.md: `SHIP`, `ITERATE` (optionally `scope=design` or
`scope=local:<comma-separated-paths>`), `NEEDS_HUMAN`, or `BLOCKED`. On
`VERDICT: ITERATE scope=local:<paths>` with fewer than 2 consecutive repairs,
run a scoped repair pass instead of the full Lead sequence: give
`trio-builder` a repair brief fixing exactly the listed paths (read
VERDICT.md, smallest correct diff, no re-planning/refactoring/scope
expansion, append a `- iter N | lead | repair: ...` line to LOG.md), then go
straight to the independent evaluator Scout/Evaluator. Track the consecutive
count in `loop/.repairs` (driver-internal; start at 1, cap at 2, reset to 0
after any full Lead pass). On the 3rd consecutive scoped verdict, or for any
other ITERATE, run the full Lead sequence as usual. Never commit.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
<!-- trio-protocol:end -->

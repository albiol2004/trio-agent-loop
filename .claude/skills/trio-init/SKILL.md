---
name: trio-init
description: Initialize the trio agent loop in the current project — creates the loop/ mailbox directory and GOAL.md from the given goal text.
argument-hint: "<goal description>"
disable-model-invocation: true
---

Set up the trio-loop mailbox in the current project.

**Mailbox directory**: default `loop/`. If `$ARGUMENTS` starts with `dir=<path>` (e.g. `/trio-init dir=loop-authz add rate limiting…`), use that directory instead — every `loop/` reference below then means that directory. One mailbox per loop is a hard rule: two loops (or two sessions) sharing a mailbox corrupt each other's state.

1. If the mailbox's `STATE.md` already exists, show its current status and LOG.md tail, then ask the user to choose: **(a)** reset — archive the old mailbox to `loop-archive-<date>/` and start fresh; **(b)** abort; or **(c)** keep it and initialize this new loop in a sibling directory (`loop-<slug>/` from the new goal) — the right choice when the existing loop may still be running in another session. If STATE.md says `status: running` and LOG.md's last entry is recent, recommend (c) — never hijack a possibly-live mailbox.
2. Create the mailbox directory with these files:

`loop/GOAL.md` — from `$ARGUMENTS`. If arguments are empty or vaguer than one sentence of substance, interview the user briefly (what does done look like, hard constraints, what must NOT change) before writing it. Decide the profile: `software` (default) or `data` — choose `data` when the goal is pipelines, ETL/ELT, SQL models, notebooks, finance/reporting datasets. Structure:
```markdown
# Goal
profile: software | data
<the mission, in the user's words, sharpened>
## Definition of done
<objectively checkable statements>
## Constraints
<stack, style, things that must not break, budget notes>
```
For `profile: data`, the Definition of done MUST include data ground truth, so interview for it if missing: source(s) of truth to reconcile against, tolerated row-count/aggregate deltas, key uniqueness expectations, and whether re-runs must be idempotent. Vague data goals ("clean the dataset") are the #1 cause of runaway loops — pin numbers down.

`loop/STATE.md` (the `mission:` line is the first sentence of GOAL.md's mission, verbatim — the /trio orchestrator halts if it ever stops matching GOAL.md, which catches another session repurposing the mailbox):
```markdown
iteration: 0
max_iterations: 10
status: ready
mission: <first sentence of the goal, verbatim>

## Approaches tried and rejected
(append-only; the Lead adds one line per dead end, with why)

## Key decisions and rationale
(append-only)
```

`loop/LOG.md` with a `# Trio loop log` header line, and empty `loop/PLAN.md`, `loop/REPORT.md`, `loop/VERDICT.md`.

3. If the project is a git repo, ask the user whether `loop/` should be gitignored (ephemeral) or committed (auditable history). Default suggestion: commit it.
4. Confirm setup and print next steps: `/trio` for a single supervised iteration (recommended first), then `/loop /trio` to run autonomously — if the mailbox is not `loop/`, say to pass it: `/trio dir=<path>` and `/loop /trio dir=<path>`. Mention Esc stops the loop, and GOAL.md + `max_iterations` in STATE.md are the two human control knobs while it runs.

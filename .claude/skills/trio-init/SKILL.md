---
name: trio-init
description: Initialize the trio agent loop in the current project — creates the loop/ mailbox directory and GOAL.md from the given goal text.
argument-hint: "<goal description>"
disable-model-invocation: true
---

Set up the trio-loop mailbox in the current project.

1. If `loop/STATE.md` already exists, show its current status and LOG.md tail, and ask the user whether to reset (archive the old `loop/` to `loop-archive-<date>/`) or abort. Do not silently overwrite an in-flight loop.
2. Create the `loop/` directory with these files:

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

`loop/STATE.md`:
```markdown
iteration: 0
max_iterations: 10
status: ready

## Approaches tried and rejected
(append-only; the Lead adds one line per dead end, with why)

## Key decisions and rationale
(append-only)
```

`loop/LOG.md` with a `# Trio loop log` header line, and empty `loop/PLAN.md`, `loop/REPORT.md`, `loop/VERDICT.md`.

3. If the project is a git repo, ask the user whether `loop/` should be gitignored (ephemeral) or committed (auditable history). Default suggestion: commit it.
4. Confirm setup and print next steps: `/trio` for a single supervised iteration (recommended first), then `/loop /trio` to run autonomously. Mention Esc stops the loop, and `loop/GOAL.md` + `max_iterations` in STATE.md are the two human control knobs while it runs.

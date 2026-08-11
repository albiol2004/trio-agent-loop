# Trio Mailbox Schema — version 1

This document defines version 1 of the trio-agent-loop mailbox protocol: the
set of files every mailbox must contain, the required `STATE.md` fields, and
the first-line contract of `VERDICT.md`. Mailboxes are versioned so that
tooling (metrics, conformance checks, drivers) can distinguish current
mailboxes from pre-versioning ones and treat them accordingly.

The conformance checker for this schema is `metrics/trio-check.py`
(`python3 metrics/trio-check.py [path]`).

## Version marker

A mailbox declares its schema version with a top-level key/value line in
`STATE.md`:

```markdown
schema: 1
```

`schema` is a plain `STATE.md` key: the line uses the same format as every
other STATE.md field (`key: value`, case-insensitive key, optional `- `
prefix), and it must appear at the top level of the file, not inside a
section. A mailbox whose STATE.md contains `schema: 1` is a **v1** mailbox.

## Required files

A v1 mailbox must contain these files in the mailbox directory:

| File | Role |
|------|------|
| `GOAL.md` | the goal: profile, mission, definition of done, constraints |
| `STATE.md` | machine-readable loop state; carries the `schema:` marker |
| `PLAN.md` | the living plan for the current iteration |
| `REPORT.md` | the Lead's iteration report for the evaluator |
| `VERDICT.md` | the evaluator's verdict (see first-line contract) |
| `LOG.md` | the append-only flight recorder |

## STATE.md required fields

In addition to `schema: 1`, a v1 STATE.md must define these top-level fields
(same `key: value` line format, case-insensitive keys):

- `iteration` — current iteration number (0 at initialization).
- `max_iterations` — hard budget cap for the loop.
- `status` — current loop status (e.g. `ready`, `running`).
- `mission` — the first sentence of GOAL.md's mission, verbatim; the
  orchestrator halts if it ever stops matching GOAL.md.

Other keys (e.g. `mission_fingerprint`) are allowed. `schema: 1` is required
to appear as a top-level key with value `1`.

## LOG.md

`LOG.md` is the append-only flight recorder. This repository accepts a few
line formats; the conformance checker validates only that the file exists and
is non-empty. Examples of accepted formats:

- `- iter 3 | lead | ...` and `- iter 3 | evaluator | VERDICT: ITERATE`
- date-prefixed: `2026-08-11 | Lead | iteration 3 | ...`
- free-form entries mentioning iteration numbers and verdicts

## VERDICT.md first-line contract

The first non-empty line of `VERDICT.md` must be one of the following
(case-insensitive, with an optional `# ` prefix):

- `VERDICT: SHIP`
- `VERDICT: ITERATE`
- `VERDICT: BLOCKED`

An empty `VERDICT.md` (no verdict recorded yet, e.g. a freshly initialized
loop) is allowed; once a non-empty line exists, it must match the contract.
The evaluator writes the verdict; supporting prose may follow below the first
line.

## PLAN.md and REPORT.md roles

- `PLAN.md` is the living plan: the Lead rewrites it at the start of each
  iteration with the current iteration's increment and the verification
  expected of it.
- `REPORT.md` is the Lead's iteration report: evidence of what was done and
  verified in the current iteration, for the evaluator's review.

## Legacy mailboxes

A mailbox is **legacy** if it lacks `schema: 1` in STATE.md (or carries a
different schema value), or if it is missing required v1 files. A mailbox
whose STATE.md is missing or unreadable is **unknown**. Legacy and unknown
mailboxes are reported by the conformance checker for information but are
**not** validation failures — pre-versioning mailboxes remain readable by
existing tooling.

## Checking conformance

```bash
python3 metrics/trio-check.py                # scan the current directory
python3 metrics/trio-check.py <path>         # scan a project or single loop dir
python3 metrics/trio-check.py <path> --json  # machine-readable report
```

The exit code is 0 when no v1 mailbox has violations, and non-zero when at
least one v1 mailbox violates this schema. Legacy and unknown mailboxes never
affect the exit code.

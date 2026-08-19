# Trio Mailbox Schema — version 1

This document defines version 1 of the trio-agent-loop mailbox protocol: the
set of files every mailbox must contain, the required `STATE.md` fields, the
first-line contract of `VERDICT.md`, the PLAN.md slice contracts, and the
mailbox placement standard. Mailboxes are versioned so that tooling
(metrics, conformance checks, drivers) can distinguish current mailboxes
from pre-versioning ones and treat them accordingly.

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

### STATE.md `verdict:` and `eval:` hot-summary lines

The hot summary may carry two optional one-line fields recording the latest
iteration's outcome, written by the driver that runs the role sequence when
it updates STATE.md after a verdict:

- `verdict: <SHIP|ITERATE|NEEDS_HUMAN|BLOCKED|pending>` — the latest
  verdict's outcome word, or `pending` while an iteration is in flight
  (evaluator has not returned yet).
- `eval: <one-line compressed evidence pointer>` — key metrics and/or the
  evidence directory path (`loop/evidence/iterN/`), compressed to one line:
  numbers and paths, never prose paragraphs.

Both are plain `key: value` lines like every other STATE.md field, one line
each; they are absent until the first verdict lands. `eval:` is a pointer
for machines and humans to re-check the outcome, not a report.

## LOG.md

`LOG.md` is the append-only flight recorder. This repository accepts a few
line formats; the conformance checker validates only that the file exists and
is non-empty. Examples of accepted formats:

- `- iter 3 | lead | ...` and `- iter 3 | evaluator | VERDICT: ITERATE`
- date-prefixed: `2026-08-11 | Lead | iteration 3 | ...`
- free-form entries mentioning iteration numbers and verdicts

## VERDICT.md first-line contract

The first non-empty line of `VERDICT.md` must start with one of the following
verdict words (case-insensitive, with an optional `# ` prefix):

- `VERDICT: SHIP`
- `VERDICT: ITERATE` — optionally followed by a `scope=` suffix:
  - `VERDICT: ITERATE scope=design` — explicit full Lead iteration.
  - `VERDICT: ITERATE scope=local:<comma-separated-paths>` — builder-direct
    repair pass confined to the listed paths.
  - Plain `VERDICT: ITERATE` means a full Lead iteration (identical behavior
    to the pre-scope protocol).
- `VERDICT: NEEDS_HUMAN` — every agent-verifiable criterion passes, but
  `PLAN.md` criteria tagged `verify: human` remain; the loop pauses for the
  human. A `VERDICT: NEEDS_HUMAN` verdict MUST include a `## Human check`
  section in `VERDICT.md` with exact steps for the human.
- `VERDICT: BLOCKED`

`scope=` is only valid on ITERATE. An empty `VERDICT.md` (no verdict recorded
yet, e.g. a freshly initialized loop) is allowed; once a non-empty line
exists, it must match the contract. The evaluator writes the verdict;
supporting prose may follow below the first line.

### Scoped repairs and the `.repairs` counter

A `scope=local` verdict routes to a builder-direct repair pass instead of a
full Lead planning pass (the portable driver runs `portable/prompts/repair.md`
with the Lead command prefix). To prevent a repair-only death spiral, at most
**2 consecutive** `scope=local` repairs run; the third consecutive scoped
verdict forces a full Lead iteration. The count is tracked in a driver-internal
counter file, `.repairs` (a plain number, in the mailbox directory, **not**
part of the public mailbox contract — `trio-check.py` and `trio-metrics.py`
ignore it). It resets to `0` on every full Lead pass (plain ITERATE,
`scope=design`, or a forced pass). The `.repairs` file survives driver
kill/resume so the cap still holds; a killed run may degrade a queued repair
into a full Lead pass on resume.

## GOAL.md verification floor (optional)

`GOAL.md` may carry a `## Verification floor` section: the minimum evidence
every iteration must produce before a SHIP can be considered (e.g. "the full
test suite passes with zero skips", "reconciliation matches to the cent").
The Lead folds it into PLAN.md's `## Verification standard`; the Evaluator
checks produced evidence against it. Absent the section, there is no floor.

## PLAN.md and REPORT.md roles

- `PLAN.md` is the living plan: the Lead rewrites it at the start of each
  iteration with the current iteration's increment, a mandatory
  `## Verification standard` section, and the verification expected of it.
- `REPORT.md` is the Lead's iteration report: evidence of what was done and
  verified in the current iteration, for the evaluator's review.

### PLAN.md `## Verification standard` (required per iteration)

Before implementation, the Lead declares in PLAN.md the verification standard
for the iteration:

- **Mode**: one of `test-first` (tests written before the change),
  `implement-then-smoke` (change then run the stated checks), or
  `human-gate` (only the human can verify; the iteration ends in
  `VERDICT: NEEDS_HUMAN`).
- **Evidence**: what will count as verified — exact commands, the outputs
  they must produce, and the data/ground-truth checks (reconciliation,
  integrity, idempotent re-runs for `profile: data`).

The Evaluator checks the produced evidence against this declared standard
(and against GOAL.md's verification floor, when present). Evidence that does
not meet the declared standard is an ITERATE whose failure scope is the
evidence gap itself.

### `verify: human` acceptance criteria

Any acceptance criterion in PLAN.md may carry the tag `verify: human` when it
requires human judgment or access the agents do not have (eyeball a visual
result, confirm a policy decision, check a credential-scoped behavior). When
every agent-verifiable criterion passes but `verify: human` criteria remain,
the Evaluator writes `VERDICT: NEEDS_HUMAN` with a `## Human check` section
naming each such criterion and the exact steps for the human to confirm it.

## PLAN.md slice contracts (shadow mode)

When a plan has more than one increment (or one increment with delegable
sub-parts), PLAN.md carries a machine-readable slice block so pipeline
tooling can measure declared-vs-actual writes. The block is a fenced
```yaml
code block whose top-level key is `slices:`:

```yaml
slices:
  - id: provider-config
    repo: .                         # target repo relative to the mailbox root; default .
    writes: [omp/configure-models.sh, "api:ProviderConfig"]
    reads:  []                      # dispatchable immediately
    gate: false                     # optional; default false
    status: in_progress             # optional; default in_progress
    iteration: 1                    # optional; int
```

The block is **cumulative** across the loop's life — it is the single
machine-readable slice history. Completed slices are never removed from it:
when a new iteration starts, entries from earlier iterations stay, marked
`status: complete` with their `iteration: N`, and the new iteration's
slices are appended. Prose per-iteration sections (e.g. `## Iteration N`
headings) may still exist for humans, but scripts read only the fenced
block — tooling resolves every entry in it, completed or not.

One list entry per slice. The restricted shape is exactly:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | kebab-case string | slice identifier; also prefixes the slice's commit messages |
| `repo` | no (default `.`) | path | target repo, relative to the mailbox root — `.` is the coordination repo; other repos are siblings it references |
| `writes` | yes | list of paths and/or `api:<Name>` | files (or directories) the slice may touch; `api:` entries name interfaces, not paths |
| `reads` | yes (may be `[]`) | list of paths and/or `api:<Name>` | inputs the slice needs frozen before it may be dispatched |
| `gate` | no (default `false`) | bool | foundation slice: never speculated, regardless of predictor state |
| `status` | no (default `in_progress`) | `planned` \| `in_progress` \| `complete` | lifecycle state; `complete` marks a finished slice that stays in the cumulative history |
| `iteration` | no | int | the iteration the slice belongs to; required in practice for completed entries, recommended for all |

Paths may be approximate (a directory entry covers everything beneath it);
`api:` names are exact. The block is optional in v1 — a mailbox without it
remains conformant, and `trio-check.py` does not validate it. The shadow
checker `metrics/trio-shadow.py` (`--mailbox <dir> [--json]`) parses the
block with a stdlib line-based parser and compares declared `writes:`
against the files actually touched by the slice's commits — for every slice
in the block, regardless of `status:`. `api:` entries are excluded from git
matching. Undeclared writes are reported, never enforced —
shadow/instrumentation mode; enforcement is a later pipeline stage.

## STATE.md `frozen:` lines

Interface freezes are recorded as plain STATE.md lines, appended by the
Lead only, when a builder's interface-only commit lands and matches the
declared contract:

```markdown
frozen: api:ProviderConfig @a1b2c3d
```

The value is the interface or path being frozen, a space, then `@` plus the
short sha of the commit that froze it. Once an interface is frozen, slices
whose `reads:` list it may be delegated. STATE.md is the hot summary roles
read every iteration, so frozen lines stay short — one line per interface.

## Slice commit convention

Every commit that belongs to a slice is prefixed with the slice id so
tooling can attribute touched files:

```text
slice(provider-config): add configure-models.sh
```

The prefix is exactly `slice(<id>): ` — the kebab-case slice id, a colon,
a space — followed by a normal commit message. Tooling resolves commits
per slice by this prefix; a commit without it belongs to no slice.

## Mailbox placement standard

`loop/` lives in the orchestrator session's cwd — the coordination repo.
It is always singular (one loop per session) and never inside a worktree:
mailbox files are the coordination surface, not build artifacts.
Multi-repo projects are handled by the slice schema, not by extra
mailboxes: each slice declares `repo:` — the repo it writes to, defaulting
to the coordination repo. Tooling resolves `repo:` relative to the mailbox
root: the project directory passed to it (the one containing `loop/`), or
the loop directory itself when pointed at directly.

## Context economics

Mailbox files are split into hot and cold files so fresh-context roles stay
cheap (every role prompt follows this; see `prompts/canonical/`):

- **Hot files — read every iteration, keep short**: `GOAL.md`, `PLAN.md`,
  `STATE.md`, `VERDICT.md`. `STATE.md` in particular is the hot summary roles
  read at every entry; keep it to machine state plus a few short lists
  ("Approaches tried and rejected", "Key decisions and rationale").
- **Cold files — append-only or delta, never role input**: `LOG.md` is the
  append-only flight recorder; roles append their one line but never read it
  (machines and humans read it). `REPORT.md` is a delta against the previous
  iteration — what changed this iteration plus evidence — never a
  restatement of the whole project.

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

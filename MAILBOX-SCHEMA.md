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

`QUEUE.md` is not in this list: it is an OPTIONAL file (see "v1
open-loop extension (optional)" below). A mailbox without `QUEUE.md`
is still a fully conformant v1 mailbox.

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
- **Task-specific checklist** (compact table under the same heading, filled
  from GOAL.md / accepted source **before** code exists — not from tests
  derived after the fact):

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL §… or A# | concrete input and action | what a reviewer can see | command, screenshot, query; skip if proportionate | `verified` / `failed` / `unverified` + revision or artifact |

`ref` is a stable pointer at original GOAL acceptance (or an accepted
decision/receipt id). Original acceptance and mandatory checks remain even
when implementer tests pass; tests are not an automatic source of business
truth. Keep the table proportionate: a tiny low-impact change (comment,
typo, generated-file regen) does not need a full browser or data
reconciliation row.

Optional **project verification defaults** live in version-controlled
project instructions (`AGENTS.md` / equivalent `## Verification defaults`
section). They are not a new config engine and must not extend
`knowledge.yaml`. Current GOAL supersedes defaults; defaults cannot waive
required checks. Accepted decisions/receipts are provenance; proposals stay
proposals. Missing knowledge does not fabricate a blocker. The Lead derives
the checklist; the human is asked only for materially ambiguous business
decisions.

Worked synthetic examples (illustrative, not executed product runs):
`examples/task-verification/`.

The Evaluator checks the produced evidence against this declared standard
(and against GOAL.md's verification floor, when present). Evidence that does
not meet the declared standard is an ITERATE whose failure scope is the
evidence gap itself. Remaining GOAL criteria left `unverified` block
whole-goal `VERDICT: SHIP`. Distinguish unavailable environment from a
product failure. Human-only checks keep existing `NEEDS_HUMAN` semantics.
Do not invent new first-line verdict tokens.

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
    accepts: []                     # optional; default []; see v1 open-loop extension (optional)
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
| `accepts` | no (default `[]`) | list of strings | slice-scoped acceptance statements the Evaluator grades this slice against (see "v1 open-loop extension (optional)" below) |

### Flow-list emission rule

Leads MUST emit non-empty `writes:` and `reads:` values as a single-line
bracket list, such as `writes: [a.py, "api:Name"]`. The
`metrics/trio-metrics.py` `_parse_flow_list` helper (lines 384-391) requires
that bracket form for non-empty values.

An empty `writes:` followed by block-style `- item` lines is also parsed by
the parser (lines 472-478 and 504-513), but that is the multi-line form.
Drivers and Leads must not emit it in new plans. `metrics/trio-shadow.py`
shares the same parser.

Paths may be approximate (a directory entry covers everything beneath it);
`api:` names are exact. The block is optional in v1 — a mailbox without it
remains conformant, and `trio-check.py` does not validate it. The shadow
checker `metrics/trio-shadow.py` (`--mailbox <dir> [--json]`) parses the
block with a stdlib line-based parser and compares declared `writes:`
against the files actually touched by the slice's commits — for every slice
in the block, regardless of `status:`. `api:` entries are excluded from git
matching. Undeclared-write drift is reported, never enforced —
shadow/instrumentation mode; commit presence, by contrast, is enforced by
the active gate below (the first ACTIVE interlock).

### Waves (declared parallelism)

A **wave** is a set of same-iteration `planned` slices whose `writes:` are
pairwise disjoint and whose `reads:` name no path/`api:` that another
slice in the set writes. It is derived by the Lead from the existing
`writes:`/`reads:` fields at plan time — no new schema field records it —
and authorizes dispatching every slice in the set to a separate builder
concurrently instead of one at a time.

### Slice commit gate (active interlock)

The commit-presence check is the first **active** interlock: drivers run it
**post-Lead, pre-Evaluator** — before dispatching the Evaluator, so a
code-changing iteration that never committed its slices cannot be graded as
shipped work.

- **What it checks**: every slice in the `slices:` block that is
  *code-changing* — at least one `writes:` entry that is neither an
  `api:<Name>` pseudo-entry nor a path inside `loop/` — must have at least
  one commit whose message starts with `slice(<id>): `. Slices that write
  only `loop/` files or `api:` names are exempt.
- **Command**: `python3 metrics/trio-shadow.py --mailbox <dir>
  --require-commits` (the script may be on PATH or referenced by absolute
  path from the installing repo's `metrics/`).
- **Exit semantics**: 0 = pass (every code-changing slice has commits, or
  there are none); 1 = fail, listing the offending slice ids; 2 = the
  `slices:` block is missing or malformed (a driver error, not a gate
  verdict). Without `--require-commits` the script stays in shadow mode and
  always exits 0 on a successful analysis.
- **Retry semantics**: on exit 1 the orchestrator retries the Lead once
  with the missing-commit note (reusing the retry-once pattern); if the
  gate still fails, the driver sets `status: error` in STATE.md, records
  the breach in LOG.md, and ends the loop.

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

## SHIP retirement commit convention

A `VERDICT: SHIP` ends the loop with the Evaluator's **retirement commit**: as
its last act the Evaluator commits the exact tree it verified (SHIP only —
never on ITERATE/NEEDS_HUMAN/BLOCKED). The pattern is exactly two commits:

- **Product commit** — the working-tree changes attributable to the loop's
  slices, in one commit: `slice(<primary-id>): <summary>` (the summary from
  the verdict's suggested commit message; additional slice ids go in the
  body). A clean tree skips it; the verdict references the existing HEAD sha
  instead.
- **Mailbox commit** — the mailbox files (`loop/`):
  `loop: iteration N — SHIP`.

The Evaluator appends a `commit: <full sha>` line per product commit to
`VERDICT.md` before the mailbox commit, so the verdict records what was
committed. **Foreign-path rule**: a modified file that is not attributable to
any slice (per `trio-shadow.py --mailbox <dir> --json`) and not under `loop/`
is NEVER included — it stays uncommitted and is flagged in the verdict's
follow-ups. The retirement commit is bookkeeping of the verified tree; it does
not change the Evaluator's read-only status (contents never change, and a
failed `--require-commits` gate is covered by the retirement commit on SHIP,
recorded as a protocol breach in the verdict). An orphaned SHIP verdict — a
machine-readable `VERDICT: SHIP` first line with no `commit:` lines (legacy or
interrupted run) — is recovered manually with the `/trio-ship` command (omp),
which performs the same two-commit pattern from the verdict's suggested commit
message.

### Bounded retirement wait

A valid SHIP verdict can reach `VERDICT.md` seconds before the Evaluator's
retirement commit (observed: verdict 16:39:12Z, commit 16:39:27Z). The driver
does not reject immediately; instead it monitors for the retirement commit
within a bounded time window, polling every few seconds by default. See
`docs/FINALIZATION-REPAIR.md` for full semantics, environment variables
(`TRIO_RETIREMENT_WAIT_SECONDS`, `TRIO_RETIREMENT_POLL_SECONDS`), state
transitions (`ship-awaiting-retirement`, `ship-pending-retirement`,
`needs_retirement` status), and recovery procedures.

## v1 open-loop extension (optional)

This is an **optional** extension to schema version 1: a mailbox without
`QUEUE.md` remains a valid v1 lockstep mailbox, and every role behaves
exactly as documented above. Everything in this section is gated on the
presence of `QUEUE.md` in the mailbox directory.

### `QUEUE.md`

`QUEUE.md` lives in the mailbox directory next to `STATE.md`. It carries
two fenced ```yaml blocks, each with exactly one top-level key. Either
block may be absent, and `QUEUE.md` itself may be absent — absent always
means an empty queue, never an error. A present block's list may be empty
(`retired:` with no entries).

```yaml
retired:
  - slice: <kebab-case slice id>
    sha: <full 40-char sha the slice was retired at>
    at: <ISO-8601 timestamp>
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `slice` | yes | kebab-case string | the slice id; must exist in the PLAN.md `slices:` block |
| `sha` | yes | full 40-char sha | the sha this entry retires the slice **at** |
| `at` | yes | ISO-8601 timestamp | when this entry was appended |

`retired:` is **append-only, Lead only**: the Lead is the only role that
appends an entry, and no role ever edits or removes an existing one.

An entry means "this slice was **retired at** `sha`" — not "the slice's
last commit". Repeated slice ids are legal and expected: a post-retirement
fix (a `slice(<id>): fix f<N> …` commit, or any later `slice(<id>): `
commit) is recorded by **appending a new** `retired:` entry for the same
slice id with the new sha, never by rewriting the earlier entry. The
uniqueness constraint is on the (`slice`, `sha`) **pair**, not on `slice`
alone — two entries for the same slice must have distinct shas.

The Evaluator grades only the **latest** `retired:` entry per slice id
(last in file order); any earlier entry for that same slice is
`superseded` — a status **derived** from file order, never written into
`QUEUE.md`. A `faults:` entry whose `observed_at` sha is a superseded sha
of its slice (i.e. an older `retired:` entry for that slice, since
overtaken by a newer one) is a candidate for `status: stale`.

```yaml
faults:
  - id: f1
    slice: <kebab-case slice id>
    observed_at: <sha the Evaluator evaluated>
    scope: [path/one.py, path/two.md]
    reason: <one line>
    status: open
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `id` | yes | `f<N>` (lowercase `f` followed by digits) | fault identifier; unique within the file |
| `slice` | yes | kebab-case string | the slice the fault was raised against |
| `observed_at` | yes | full sha | the sha the Evaluator evaluated when it raised the fault |
| `scope` | yes | a plain value (`local:<comma-separated paths>`, `design`, or bare comma-separated paths) **or** a list (single-line bracket flow list, or block `- item` list) | same semantics as the `scope=` suffix in the VERDICT.md first-line contract; see "Fault `scope` shapes" below |
| `reason` | yes | one line | why the fault was raised |
| `status` | yes | `open` \| `taken` \| `done` \| `stale` | fault lifecycle state |

#### Fault `scope` shapes

Both spellings are accepted and mean the same thing:

```yaml
    scope: local:api/test/route.test.ts,api/src/route.ts   # plain local:
    scope: design                                          # plain design
    scope: api/src/route.ts                                # plain bare path(s)
    scope: [api/test/route.test.ts, "dir with, comma/x.ts"]  # flow list
    scope:                                                 # block list
      - api/test/route.test.ts
```

Readers (`metrics/trio-metrics.py parse_faults`) normalize every shape to
one list of path strings: a leading `local:` is stripped (from a plain
value or from any list item), a plain value is split on top-level commas
(quote-aware), and a design-scoped fault becomes exactly `["design"]`.
`trio-check.py` rejects an empty scope, `design` mixed with paths, and an
item written as the VERDICT.md suffix (`scope=...`).

A malformed `retired:`/`faults:` entry never empties its block: the
lenient reader (`read_queue`) drops only that entry, keeps every valid
one, and reports the problem in `queue["errors"]`; `trio-check.py` fails
on it and the open-loop driver logs
`- iter N | loop | QUEUE.md parse error: <msg>` once per turn.

Line-level rules of the reader (strict mode raises on every reported
problem; lenient mode reports and continues):

- A non-key line indented deeper than a `reason:` line above it (a
  wrapped `reason:`) is YAML plain-scalar folding: it is appended to that
  value with one space and is **not** an error. `reason` is the only
  free-text field; the same continuation after **any other** key
  (`status`, `sha`, `slice`, `observed_at`, `at`, or the `- id:`/`- slice:`
  header value) is reported — line number, text prefix, and the key it
  followed — and skipped; the entry is kept with that key **unchanged**
  and parsing of the entry continues. (A note under `status: open` must
  never turn the status into `open (see f0)` and silently close the
  fault.)
- A key repeated inside one entry (`status:` twice) is reported (line
  number, key); the **first** value is kept, the repeat is skipped, the
  entry is kept.
- A garbled entry header is recognized case-insensitively with a `-` or
  `*` bullet (`-slice: s1`, `* slice: s1`, `- Slice: s1`, `slice=s1`,
  `- slice s1`) and poisons the slice it names (below). A header whose key
  is too mangled to identify (`- slcie: s1`) names no slice: it is only
  reported as unexpected content, and trio-check fails on it.
- An unexpected line (prose, a stray `id:`/`slice:` line, a garbled next
  `- id:` header, a duplicate block key) that follows an entry which
  already has **every required key** is reported — the message names the
  line number and a prefix of its text — and skipped; the complete entry
  is **kept**. Lines up to the next `- <id>:` header are skipped with it.
  An entry still missing a required key at that point is dropped.
- A `retired:` entry that is dropped for any reason, or whose slice id is
  named by a stray `slice:` line or a garbled `- slice` header, puts its
  slice in `queue["malformed_slices"]`. **Such a slice is not gated as
  retired** — even when an older valid entry for it survives — until the
  entry is repaired: the malformed entry is usually the re-retirement of a
  fix, and trusting the older, already-graded sha would skip the fix's
  slice-eval. The open-loop driver logs
  `- iter N | loop | QUEUE.md: slice <id> has a malformed retired entry; not gated as retired`
  once per turn; if the Lead makes no change, the 3-no-op stall guard
  ends the run with `status: error`.

A fault whose `status` is not one of `open`, `taken`, `done`, `stale`
counts as **live** for the open-loop integration gate (fail-closed, like
`open`); the driver logs
`- iter N | loop | QUEUE.md: fault <id> has unknown status '<value>'; treated as open`
once per turn, and trio-check fails on it.

`faults:` entries are **appended by the Evaluator only**; the Lead only
transitions an existing entry's `status:` — the Lead never appends a new
fault and never edits `slice`, `observed_at`, `scope`, or `reason`.

### Slice lifecycle (derived)

The six per-slice lifecycle states are derived by `metrics/trio-metrics.py
derive_slices()` — never written to the mailbox — in this precedence order:

1. **planned** — no retired entry, no `slice(<id>):` commit, and status is "planned" or absent/blank.
2. **building** — no retired entry, and status is "in_progress" or the slice has a `slice(<id>):` commit.
3. **retired** — has a retired entry whose latest sha has no matching `## slice <id> @<sha>` verdict section.
4. **faulted** — any fault for this slice is `open`, or the latest matching verdict is ITERATE and no fault is `taken`.
5. **repairing** — any fault for this slice is `taken` (and none open).
6. **shipped** — latest matching verdict is SHIP and no open/taken fault remains.

### `accepts:` (PLAN.md slices field)

The PLAN.md `slices:` block gains one **optional** field, `accepts:` (see
"PLAN.md slice contracts (shadow mode)" above, and its field table) — a
single-line bracket list of slice-scoped acceptance statements, e.g.
`accepts: ["trio-check exits 0 on every loop-* dir", "no QUEUE.md -> empty
queues"]`. Default is `[]`. It is what the Evaluator grades that slice
against in open-loop mode. It must remain optional so every existing
mailbox — with or without `accepts:` on any slice — stays conformant.

### Per-slice verdicts in VERDICT.md

Each slice evaluation is recorded as an **appended** section in
`VERDICT.md` whose heading is exactly one of:

```markdown
## slice <id> @<sha> — SHIP
## slice <id> @<sha> — ITERATE
```

(em dash, `@` immediately before the full sha). VERDICT.md's
first-non-empty-line contract (see "## VERDICT.md first-line contract"
above — unchanged) stays **reserved for the final integration verdict**
(see "Termination" below). Until that integration verdict exists, an
open-loop `VERDICT.md` may consist only of per-slice sections and
therefore has no `VERDICT:` first line — that is valid for a mailbox that
has `QUEUE.md`, and is **not** valid for a lockstep mailbox. A per-slice
section body **MUST NOT** contain any line beginning with `VERDICT:` —
that token stays reserved for the integration verdict so existing verdict
parsers are unaffected.

### Lead loop (open-loop mode)

1. Take `open` faults first: mark the fault `taken`, fix strictly within
   its `scope:`, commit `slice(<id>): fix f<N> …`, then mark it `done`.
2. Mark a fault `stale` instead of `done` when every path in its `scope:`
   was already rewritten after `observed_at` and the reason no longer
   applies.
3. Otherwise take the next `planned` slice.
4. On finishing a slice: commit, set `status: complete` in the PLAN.md
   `slices:` block, append a `retired:` entry with the full sha of the
   last `slice(<id>): ` commit.
5. Never wait for a verdict.
6. **Backpressure**: while 2 or more faults are `open` or `taken`, take no
   new slice — drain faults first. In open-loop mode this replaces the
   two-consecutive-ITERATE drain rule (see "Scoped repairs and the
   `.repairs` counter" above).

### Evaluator loop (open-loop mode)

1. For each `retired:` entry with no corresponding `## slice <id> @<sha>`
   section in VERDICT.md, evaluate the slice's tree **at that `sha`** — via
   `git worktree add`, never the moving working tree.
2. Grade it against that slice's `accepts:` in the PLAN.md slices block.
3. SHIP → append the per-slice section, record only, append no fault.
   Drivers verify each per-slice section on disk and may re-invoke the Evaluator for the same (slice, sha) if verification fails mid-flight, so identical verdicts reappearing is expected under retry logic.
4. ITERATE → append the per-slice section AND append one `faults:` entry
   (`status: open`, `observed_at:` the evaluated sha, `scope:` the failing
   paths, `reason:` one line).
5. NEEDS_HUMAN / BLOCKED → exactly as today (STATE.md + VERDICT.md
   first-line contract); the loop halts.
6. The Evaluator never edits `retired:` and never sets a fault's
   `taken`/`done`/`stale`.

### Per-slice commit gate

```bash
python3 metrics/trio-shadow.py --mailbox <dir> --require-commits --slice <id>
```

must pass before the Evaluator grades slice `<id>`. Same exit semantics as
the commit gate above: 0 = pass, 1 = fail (listing the offending slice),
2 = the `slices:` block is missing or malformed, and 2 also for an unknown
`--slice` id. `--slice <id>` is an optional filter that restricts the
analysis (and the gate) to that one slice; without it, behaviour is
unchanged from today.

### Termination

When all planned slices are retired AND no fault is `open` or `taken`, the
Evaluator runs one **integration evaluation** on HEAD against GOAL.md's
acceptance criteria. SHIP → the existing "SHIP retirement commit
convention" applies unchanged (product commit + `loop: iteration N — SHIP`
mailbox commit, `commit:` lines in VERDICT.md). ITERATE → faults appended
as usual and the loop continues.

### Backwards compatibility

Everything above is gated on the presence of `QUEUE.md` in the mailbox. No
`QUEUE.md` → the mailbox is a plain v1 lockstep mailbox and every role
behaves exactly as it does today. Existing tooling (`trio-check.py`,
`trio-metrics.py`, `trio-shadow.py`, the dashboard) must keep working
unchanged on existing mailboxes.

Open-loop `slice-eval` ready-gates in Omnigent treat **any** `VERDICT.md`
byte change versus the snapshot as ready. That is not a proof that the
new text names the dispatched slice or sha. Lockstep freshness is the
dispatched `attempt:` (and pin on `commit:` when a git repo is visible),
not an iteration-only leftover SHIP. Passing lockstep CLI tests does not
mean open-loop artifact matching is qualified.

## Mailbox placement standard

`loop/` lives in the orchestrator session's cwd — the coordination repo.
It is always singular (one loop per session) and never inside a worktree:
mailbox files are the coordination surface, not build artifacts.
Multi-repo projects are handled by the slice schema, not by extra
mailboxes: each slice declares `repo:` — the repo it writes to, defaulting
to the coordination repo. Tooling resolves `repo:` relative to the mailbox
root: the project directory passed to it (the one containing `loop/`), or
the loop directory itself when pointed at directly.

## Session sidecar

`loop/.session.json` is the harness-owned session sidecar (see
`prompts/protocol-essentials.md`) recording the live orchestrator pid and
session id; conformance tooling ignores it.

`.sessions/` is a separate, optional driver-owned archive directory:
`trioctl omnigent sessions prune` (and `loop`'s default post-loop cleanup,
skippable with `--keep-sessions`) write one JSONL transcript per archived
broker session there before deleting it from the broker. Conformance
tooling ignores it too.

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

When `QUEUE.md` is present, `trio-check.py` also validates it (block
shape, fault statuses, `retired:` entries referencing slice ids that
exist in PLAN.md) — see "v1 open-loop extension (optional)" above. Its
absence is never a violation.

## Changelog

- **METRICS_API 3, r11 fold-fix** (no API bump; stricter reader, same
  shape): only `reason:` folds a continuation line; a continuation after
  any other key and a duplicate key inside an entry are reported and the
  first/unchanged value kept; `* slice:` / `- Slice:` headers poison their
  slice; the driver treats an unknown fault status as live.
- **METRICS_API 3** (r11 queue-harden): `read_queue` returns
  `malformed_slices`; a slice with a malformed `retired:` entry is not
  gated as retired; a complete entry survives a stray line after it;
  wrapped scalar continuations fold. Together with the plain fault
  `scope:` values and lenient `errors` of r11 fault-scope, this is the
  contract `trioctl` (`REQUIRED_METRICS_API = 3`) requires of a
  repository's vendored `metrics/trio-metrics.py`; an older copy is
  refused with "METRICS_API 2, this trioctl requires 3 (mixed metrics/
  versions)" instead of silently dropping plain-scope faults.
- **METRICS_API 2**: `read_queue`, `parse_slice_verdicts`,
  `parse_verdict_scope`.

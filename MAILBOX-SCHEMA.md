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
- `status` — current loop status (e.g. `ready`, `running`; driver-set
  terminal values include `shipped`, `blocked`, `error`, `needs_human`,
  `needs_retirement` and, for root-free runs (every loop in a git
  checkout since r16b), `needs_land` — see "Root-free loops (r16)").
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
  `VERDICT: NEEDS_HUMAN`). The mode is enforced (r18a): `test-first`
  requires red-before-green evidence for every code slice — the driver's
  base-revert kill check reporting `killed` (trioctl, shadow in r18a) or the
  Evaluator's own run of the new tests against the base; without it
  those tests grade `unverified`. `implement-then-smoke` requires the
  Evaluator to re-execute the smoke at the pin; a `--verify-only` or
  pass-flag reader is not a smoke. Switching `mode:` mid-loop needs a
  `DECISION:` line.
- **`goal_acceptance:`** (r18a): 1–5 behaviour lines in the accepts
  grammar (see "`accepts:` grammar"), each traced to a GOAL ref, as an
  indented list under the plain `goal_acceptance:` line.
- **`goal_probe:`** (r18a): one plain line — the exact command, its input,
  the expected output and `offline: yes|no`, e.g.
  `goal_probe: curl -s localhost:8787/stats?keyHash=<unknown> -> 404 {"error":"key not found"} | offline: yes`.
  The Lead declares it and never implements it; the Evaluator runs it and
  writes one probe of its own (VERDICT.md `## Independent probe`).
  `trio-check.py` warns when an open-loop mailbox (QUEUE.md) has none.
- **Evidence**: what will count as verified — exact commands, the outputs
  they must produce, and the data/ground-truth checks (reconciliation,
  integrity, idempotent re-runs for `profile: data`).
- **`full_check:`** (required): one plain line naming the exact whole-tree
  command(s) that are the full check — the full test command plus the
  typecheck/lint when the repository has one, e.g.
  `full_check: cd api && npm test && npm run typecheck`. With declared
  `repos:` (r15) it may instead be a per-repo mapping -- flow
  `full_check: { app-backend: "pytest -q", home: "make test" }` or
  indented `<repo>: <command>` lines (blank lines between them are
  allowed) -- each command run from its repo's root; a plain string stays
  the home repo's command. Optional
  `full_check_budget_s: <n>` overrides the default 120 s wall-clock budget.
  In open-loop the Lead's whole-tree gate after the last retirement is
  proportional: skipped when no product code changed since the last
  `gate: PASS @<sha>` LOG.md line; otherwise by default the integration
  check (the typecheck/lint named in `full_check:` plus this pass's slices'
  `## Targeted check` commands, re-run on merged HEAD); the full
  `full_check:` only when `cross_cutting: true` is set or
  `full_check_budget_s` is ≤ 60. Plain lines under this heading — never
  keys in the `slices:` block.
- **`cross_cutting:`** (optional, default false): the plain line
  `cross_cutting: true` marks an iteration whose change touches shared code
  every slice depends on (core types, shared config, build or test
  infrastructure), so the open-loop gate runs the full `full_check:`
  instead of the integration check. Like `full_check:`, a plain line under
  this heading — never a key in the `slices:` block (a key inside that
  block reads as a slice field).
- **`lead_integration:`** (required when any exist): every whole-goal
  deliverable that is not inside a slice (smoke evidence, an `evidence/`
  dir, a generated report) is either in a slice's `writes:` or listed on
  this plain line; the Lead produces the listed ones after its whole-tree
  gate and records them in REPORT.md `## Lead integration`. Nothing GOAL
  requires may be left unowned.
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
    repo: home                      # optional; `home` (the mailbox repo; also `.`, the default) or a PLAN.md `repos:` name (r15)
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
| `repo` | no (default `.` = `home`) | name (r15) or path | with a PLAN.md `repos:` block: `home` (also `.`) or a declared repo name — the repo the slice's `writes:` are relative to (see "Declared repos (r15)"); without one: `.`/`home` only (the r15 guard refuses a path elsewhere) |
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

With declared `repos:` (r15) the product side is per repo: one empty
`loop: iteration N — SHIP (<mailbox>)` commit in each declared repo that
has slices, recorded as `commit: <repo>@<full sha>`, before the home
mailbox commit (see "Declared repos (r15)").

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
| `at` | yes | ISO-8601 timestamp | the committer time of `sha` (`git log -1 --format=%cI <sha>`), never an invented or estimated time |

Each entry has exactly these three keys, in this order: `slice:`, `sha:`,
`at:`. The key is `sha:` — never `merge_commit:` (that is the builder JSON
field the value comes from); any other key makes the entry malformed.
**r15**: an entry for a slice of a declared `repos:` repo carries a fourth
key, `repo: <name>`, written between `slice:` and `sha:` (the parser also
accepts it after `sha:`; the three-key order above is otherwise fixed); its `sha` is a commit of
that repo and `at:` its committer time there (`git -C <repo> log -1
--format=%cI <sha>`). Omitted means `home`, so existing mailboxes parse
unchanged. PLAN.md is authoritative: trio-check flags a `repo:` that is
undeclared or differs from the slice's PLAN.md `repo:`, and the open-loop
driver holds such an entry -- and one whose `sha` is not a commit of its
declared repo -- like any other malformed entry (logged as a QUEUE.md
parse error, never gated as retired, never graded) until it is re-retired
correctly. `faults:` entries are unchanged (the slice id implies the
repo).

`retired:` is **append-only, Lead only**: the Lead is the only role that
appends an entry, and no role ever edits or removes an existing one. The
Lead appends each entry INSIDE the ```yaml `retired:` fence, before its
closing ```, indented as a list item of `retired:` — never by replacing an
earlier entry, and never after the closing ``` (an entry outside the fence
is silently ignored by the reader):

````text
```yaml
retired:
  - slice: status-parse
    sha: af7d8220c4d606f549c4a374c9e22b6a6a03ec04
    at: 2026-09-27T03:03:20Z
  - slice: cli-whoami
    sha: e688fdf5493dac69e4db40345a69aa1287cd6aa1
    at: 2026-09-27T03:04:53Z
```
````

(`status-parse` is an existing entry; `cli-whoami` is the appended one.)
The ONLY edit allowed to an existing entry is repairing one the loop has
logged as malformed (`QUEUE.md: slice <id> has a malformed retired entry`):
fix that entry's keys in place, change nothing else. The Lead checks the
append by counting the entries inside the `retired:` fence only, before and
after —
`awk '/^```/{f=0} f&&/^  - slice:/{n++} /^retired:/{f=1} END{print n+0}' QUEUE.md`
— which must grow by exactly one (a malformed-entry repair leaves it
unchanged).

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

#### Fences, top-level keys, and the integration gate

The reader finds each block by its fence, following CommonMark:

- **Opener:** a line indented **at most 3 spaces** (a tab in the indent
  never opens a fence) with a run of at least three `` ` `` or `~`
  characters, optionally followed by an info string. Only a fence opened
  with exactly ```` ```yaml ```` (or ```` ```yml ````, any case, any run
  of 3+ backticks) is read as a queue block.
- **Closer:** a fence closes **only** on a line indented at most 3
  spaces, made of the **same** fence character, with a run **at least as
  long** as the opener's, and **no info string**. Anything else is fence
  content: a code block quoted inside a wrapped `reason:` (```` ```python ````
  at col 6 and its closing ```` ``` ````), a `~~~` line inside a
  ```` ``` ```` fence, or a ```` ```sh ```` line never closes the block.
  An unterminated fence runs to the end of the file.
- **Top-level key:** a fence carries `retired:`/`faults:` only when a
  body line is exactly that key at **column 0**. An indented
  `  faults:`, or a `retired:` line inside a `reason:` continuation, does
  not select the fence (so a fault fence placed before the retired fence
  whose reason quotes a retired entry can never be taken as the
  `retired:` block).

Reported (strict mode raises; lenient mode adds a
`` `faults:` block: `` / `` `retired:` block: `` error):

- a second ```` ```yaml ```` fence with the same top-level key, or the key
  inside an untagged, `~~~`, or info-string (```` ```yaml title ````)
  fence — "a second fenced ... block is ignored" / "... fence is ignored";
- an **orphan entry header**: any `- id:` line (for `faults:`) or
  `- slice:` line (for `retired:`) that is inside a fence **without** the
  column-0 key, or outside every fence — "`- id:` entry ... is outside the
  `faults:` block (...) and is ignored". This covers a typo'd or missing
  top key (`fault:`, `Faults:`, `faults` without a colon), a second
  ```` ```yaml ```` fence that continues the list without repeating the
  key, and a stray col 0–3 ```` ``` ```` line that closes the block early.
  Line numbers in these messages are `QUEUE.md` line numbers.

**Gate hold.** While `queue["errors"]` contains any `` `faults:` block: ``
error, the open-loop driver does **not** start the integration eval, even
when every slice is retired and no parsed fault is live — a fault the
reader could not see must never be SHIPped over. It logs
`- iter N | loop | gate held: QUEUE.md faults block has parse errors: <first error>`
once per iteration and passes the errors to the next Lead pass
(`queue_errors`, rendered as a "QUEUE.md PARSE ERRORS" note in the
OPEN-LOOP CONTEXT). The Lead repairs the entry (moves it under the
```` ```yaml ```` `faults:` key); a Lead that makes no change ends the run
through the 3-no-op stall guard (`status: error`, exit 3). There are no
exemptions. `` `retired:` block: `` errors are logged but do not hold the
gate (an ignored retired entry only leaves its slice un-retired).

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

#### `accepts:` grammar (r18a)

Every item (and every `goal_acceptance:` line) is one behaviour with an
oracle:

```text
<input/action> -> <observable> | oracle: <kind>
```

- `<input/action>` — the concrete input or action: a request, a CLI call
  with its arguments, a query, a public function with its inputs.
- `<observable>` — what a reviewer sees: a value, a status code, an
  output line, a row count, a refusal message.
- `<kind>` — `value` (an exact value), `property` (an invariant over
  inputs), `diff` (before/after against a named baseline), `refusal` (the
  input is rejected with a named error), `static` (a property of the
  deployed or compiled artifact — `SHOW CREATE VIEW`, the built bundle —
  never the local source text) or `rerun` (re-execute a named command and
  compare).

Example: `"GET /stats?keyHash=<64-hex not in catalog> -> 404 {error:'key not found'} | oracle: refusal"`.
"tests pass", "works", "exists", "is documented" and "stays green" are
never an accept on their own.

`trio-check.py` lints every `accepts:` item of a v1 mailbox's PLAN.md
(quality findings, printed under the mailbox):

- `REJECT` — free text with no relation and no `oracle:` tag, or a banned
  phrase standing alone. A relation is `->`, `==`/`=`/`!=`/`<`/`>`/`<=`/`>=`,
  a bare HTTP status code (`401 {error:..}`, `429 is rate_limited`), or a
  relation word (`is`, `returns`, `equals`, `match(es)`, `exactly`,
  `identical`, `unchanged`, `vs`, `renders`, `refuses`, ...) together with
  an observable (a number, a quoted/bracketed literal, an identifier with
  `_`/`()`/inner capitals, an ALLCAPS token, `None`/`null`/`true`). `->` is
  then optional: "hung binary returns None in under 4 s" is a `WARN` (no
  oracle tag), not a `REJECT`;
- `WARN` — an `oracle:` tag without a relation, a relation without an
  `oracle:` tag, or an unknown oracle kind;
- `static-config` — an accept about a static config artifact (compose,
  nginx, Dockerfile, systemd units, tsconfig, yaml/toml/ini) that is the
  deployed artifact. It is checked by parsing it (yaml load, `nginx -t`,
  `docker compose config` when available), is never a `REJECT` and never a
  tautology, but a slice whose accepts are ALL static-config gets a `WARN`
  to pair them with one runtime accept.

In r18a the findings are advisory: they never change the exit code
unless `--strict-quality` is passed (then a `REJECT` is a violation, exit
1, except in a finished mailbox — STATE `status:`/`phase:` SHIP, shipped,
landed, done, complete, abandoned or stopped — whose PLAN is history). r18b
makes `REJECT` a violation by default.

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

### Evidence kinds and the slice summary line (r18a, trimmed in r19)

Each per-slice section grades the slice's `accepts:` items PASS, FAIL or
`unverified` and names the evidence kind behind a grade:
`re-run` (the Evaluator re-executed the behaviour at the pin), `probe`
(its own check against the public surface), `implementer-test` (a
builder/Lead test it ran — PASS only when not tautological and, for a
`value`/`property` accept, shown to fail without the change) or `receipt`
(a file someone else wrote — never PASS on its own). An accept that needs
an environment the Evaluator cannot reach is graded `unverified` with
`UNAVAILABLE(<reason>)` and listed on an `unavailable:` line: at slice
level that gap alone is not an ITERATE (SHIP on the other accepts); the
integration evaluation attempts every such accept itself or returns
NEEDS_HUMAN listing them under `## Human check` (eval-r18a). When a slice
changes a shared module the slice-eval also runs the existing suites that
exercise it, and the integration evaluation runs the repo's full check.

**r19 C1 (slice-evals back to fast, independent of the acceptance
switch):** a slice section names its failing or unverified accepts with
their evidence kind and command, and ends with ONE summary line:

```markdown
evidence: re-run=<n> implementer-test=<n> receipt=<n> unverified=<n>
```

The per-accept table, the `attacks:` list, the independent probe and the
re-execution duties (implement-then-smoke, `AUTHORED-BY: lead` receipt
families, data-work re-run) are whole-goal duties: the lockstep verdict
and the open-loop integration verdict carry them (the Omnigent driver
appends the generated `integration-rigor.md` to those prompts only).
Tests on the canonical evaluator's tautology list (string presence on
files the slice or Lead wrote, one- or two-character `in` checks,
`or`-chains satisfied by a header, asserting the literal the code writes,
`--verify-only`/pass-flag readers, presence-only checks, a typecheck over
`files: []`, tests that read the mailbox, `results/` or `evidence/`) are
still rejected by name in slice sections.

The Omnigent driver parses each slice section after the slice-eval returns
(the `evidence:` line, else a table's evidence cells; any `attacks:`
items) and logs `- iter N | loop | slice <id> @<sha12> SHIP|ITERATE
evidence: re-run=<n> probe=<n> implementer-test=<n> receipt=<n>
unverified=<n> attacks=<n|n/a> (shadow)` (`evidence: missing` when neither
is present; `attacks=n/a` when the section lists none, the r19 norm),
records it under `quality` in `.driver.json`, and trio-shadow prints it.
Telemetry only.

**Pre-gate flags (r18a L7, advisory).** Before integrating an isolated
builder, trioctl runs a deterministic AST/regex lint (no model call) over
the slice's changed test files: `in` / `toContain` checks of a zero- or
one-character literal (two characters on file text), `or`-chains of `in`
checks that are negative or run on file text, string presence on text read
from a file (names scoped per function; an HTTP response body or a file the
test's own action just wrote is runtime output, not file text),
presence-only `is_file()`/`exists()` asserts, tests whose file reads name a
`results/`/`evidence/` path or run `--verify-only`, tests that import none
of the slice's product modules (module and package names; tests that run
the product by subprocess / importlib / a script path, or read it, are
exempt), TypeScript `toContain` over a `readFileSync` variable, and a
`tsc -p` over a tsconfig with `files: []`. String presence on a static
config artifact (compose, nginx, Dockerfile, systemd, tsconfig, yaml, ...)
is the separate `static-config` category — not a tautology flag unless the
test file has no runtime check at all. The flags are
`verification_flags` in the builder JSON and ledger record and a
`PRE-GATE FLAGS` block ("the following tests look tautological; grade
them explicitly") in the slice-eval's OPEN-LOOP CONTEXT. The lints also run
in-loop without a builder (eval-r18a N7): for a Lead take-over the driver
lints the files the slice's `slice(<id>):` commits changed, read at the
slice sha, and every slice-eval's `PRE-GATE:` block also lists the slice's
accept-lint findings (`PRE-GATE ACCEPTS`); both are recorded under
`quality` in `.driver.json` (`verification_flags`, `accept_lint`). After
every Lead pass the driver runs trio-check's quality lints over the mailbox
and records them as `.driver.json` `lint` (`counts`, `findings`,
`mode: advisory`). Nothing is gated. `trio-check.py`
applies the same lint to the mailbox's own test files and the test files
slices declare in `writes:` (`quality: WARN test looks tautological`);
nested repos / worktrees under the mailbox (declared repos) are skipped.

### `## Independent probe` (whole-goal verdicts, r18a)

The lockstep verdict and the open-loop integration verdict carry:

```markdown
## Independent probe
probe: PASS|FAIL|UNAVAILABLE <one-line reason>
probe_cmd: <exact command>
probe_src: <path of the probe the Evaluator wrote, outside product paths>
expected: <observable from GOAL.md / goal_probe:>
observed: <verbatim output excerpt>
```

The probe is written by the Evaluator against the public surface; it
never imports implementer tests or Lead scripts. `UNAVAILABLE` leaves the
criterion unverified (NEEDS_HUMAN, never SHIP). In r18a the Omnigent
driver only logs it after each integration-eval (`- iter N | loop |
integration-eval @<sha12> probe: PASS|FAIL|UNAVAILABLE|missing (shadow)`)
and records it under `quality` in `.driver.json` (lockstep verdicts:
`.driver.json` only); r18b makes `probe: PASS` a SHIP condition.

### Base-revert kill check (r18a L2a, shadow)

`trioctl omnigent run builder --isolate`, after the builder exits 0 with a
passing `TARGETED_CHECK:` line and **before** its worktree is committed
and merged: reverts the slice's non-test product files (changed vs the
worktree base, tracked or untracked; excluding test paths — `tests/`,
`test/`, `__tests__/`, `spec/`, `test_*.py`, `*_test.py`, `*_test.go`,
`conftest.py`, `*.test.*`, `*.spec.*` — the mailbox, `results/` and
`evidence/`) to the base, re-runs the brief's `## Targeted check` command
from the worktree root under the targeted-check budget (120 s, PLAN.md
`full_check_budget_s:`, or `TRIO_KILL_CHECK_BUDGET_S`), and restores the
tree from an in-memory snapshot of **every** path the slice changed
(tracked and untracked: product, tests, fixtures, receipts, mailbox files,
`.gitignore` — restored first, so newly ignored builder files are never
treated as check output); files the check touched that the slice did not
change come back from the base blob, never the index. The restore is
proven byte-identical per snapshotted path and by a sha256 over every
tracked and untracked non-ignored file (`tree_sha256` ==
`tree_sha256_after`, `restored: true`). A check that `cd`s / `pushd`s to an
absolute (or `~`) path outside the worktree is not run: `n/a (absolute
cd)`. Outcomes:

| outcome | meaning |
|---|---|
| `killed` | the re-run failed with a runner's own assertion / failed-test report: pytest `N failed` / `FAILED <id>` / `E   assert` / `AssertionError`; vitest/jest `Tests: N failed` or a `×`/`✕` test mark (a bare `FAIL <file>` only without a collection error); go `--- FAIL`; TAP `not ok N`; generic `Error: expect` |
| `survived` | still green without the product change: the tests do not exercise it |
| `n/a` | no product file changed, the slice changed no test file, no targeted check in the brief, the check did not pass, or an absolute `cd` out of the worktree |
| `error` | anything else: timeout; missing runner or module, usage error, `cd` failure, pytest exit 4/5 (`reason: targeted check not runnable`); a collection / import / module-resolution / build error (`collection_error: true` — the base lacks what the tests import, not a behavioural kill); any other non-zero exit; or `restore:` — the restore proof failed (`kill_check: error (restore)`) |

The r18a-review split `killed-by-import` is gone (eval-r18a N3: it
depended on import style, not test strength); the informational
`collection_error: true` replaces it and is never graded on.

Recorded as `kill_check:` in the builder's JSON line and its worktree
ledger record; the Omnigent driver logs `- iter N | loop | retired slice
<id> @<sha12> by builder|lead | kill_check: <outcome> (shadow)` when it
first dispatches the slice-eval, shows `BASE-REVERT:` / `AUTHORED-BY:`
lines in that slice-eval's OPEN-LOOP CONTEXT, and records it under
`quality` in `.driver.json` (trio-shadow prints it). **Shadow in r18a**: it
never changes the retire decision (r18b: `survived` blocks the retire).
This includes a failed restore proof: it is recorded as `kill_check:
error (restore)` (with `check_outcome:` and `restore_mismatch:`), printed as
a WARNING on stderr, and the builder's snapshot-restored tree is integrated
as usual.
Disable with `--no-kill-check`, `TRIO_KILL_CHECK=0` (the loop driver then
writes `kill_check: false` to `.driver.json`, which its builders honour).

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

## Frozen acceptance (r19, optional)

Behind the switch `[acceptance] enabled` (trioctl.toml; CLI
`--acceptance/--no-acceptance`; env `TRIO_ACCEPTANCE=0|1`; default
**off**). A mailbox without `acceptance/` is checked and driven exactly as
before. With the switch on, an independent **acceptance author** (a
separate role on the Lead/Evaluator model tier, never the Lead) turns
GOAL.md into black-box checks before any builder runs; the driver
validates them at base, freezes them, and the PLAN must map every one.

### `acceptance/` layout

```
<mailbox>/acceptance/
  MANIFEST.json      # acceptance_version: 1 (JSON)
  AUTHOR.md          # GOAL sentence inventory: testable-black-box /
                     # testable-only-live / not-testable, drops + reasons
  checks/acc_NN_<slug>.{py,sh,mjs}
  checks/ACC-NN/...  # optional per-check files (only that check sees them)
  fakes/...          # fake CLIs / loopback servers / fixtures (shared)
  lib/...            # optional shared helpers (PYTHONPATH / NODE_PATH)
  FROZEN             # written by the DRIVER: pin chain
  AMENDMENTS.md      # created empty at freeze; append-only
```

`MANIFEST.json`: `acceptance_version: 1`, `goal_sha256`, `notes_sha256`,
`base`, `author {role, model, effort, session, path}`, `budget_s` (<= 300),
`setup: [{id, cmd, provides, network: allowed}]`, `bindings: {NAME:
{default, goal_quote}}` and `checks: [{id: ACC-NN, goal_ref, goal_quote,
kind, surface, run: [argv], expect: {exit: 0, stdout?|stderr?|output?: [regex]},
timeout_s (<= 120), needs: [tool | setup:<id>], binds: [NAME], network:
loopback}]`. `goal_quote` is a verbatim substring of GOAL.md (or
`ACCEPTANCE-NOTES.md`). `kind`: `behaviour` (must FAIL at base, needs
coverage), `doc` (help text/README obligation; must FAIL at base, needs
coverage), `guard` (may PASS at base; at most 3; no coverage).

Runner (`metrics/trio-acceptance.py run|validate|hash|verify`): exit 0
PASS, 1 FAIL, 77 or an unmet `needs` UNAVAILABLE, a timeout is FAIL
(`timeout`), anything else ERROR -> re-run once -> FAIL (`error`). `expect`
regexes match the full stream (first 1 MiB): `stdout`, `stderr`, or
`output` (stdout then stderr), within min(`timeout_s`, 10 s) (else FAIL
`pattern-timeout`); the 2 KB excerpt is evidence only. Each
check runs in its OWN fresh copy of the tree (no .git/node_modules/caches/
mailbox) with its own read-only view of the pack: its own files (named by
its `run`, mentioned by them, or under `checks/<its id>/`) plus the shared
ones (`fakes/`, `lib/`, interpreter-loaded names, unowned helpers); the
view omits the metadata files and what the pin skips (`node_modules/`,
`__pycache__/`, `.pytest_cache/`, `*.pyc`). No check can change another
check's inputs. Loopback-only under bwrap when available (else `sandbox:
none` with a dead proxy), the check's own Python run with `-P -s` when
the code it runs comes from the pack (a script under `acceptance/`, `-c`,
`-m` of an `acceptance/lib/` module; neither the script's directory, the
tree nor user site is importable by it; `-m acceptance...` is invalid). A
`run` that starts the product (`python3 app.py`, `python3 -m pkg`,
`python3 -m pytest tests`) gets no flag, and flags are not inherited, so
the product resolves imports normally; pytest on pack tests
(`python3 -m pytest acceptance/...`) gets `-P` and `-p no:cacheprovider
--confcutdir=<view> -c /dev/null --rootdir=<view>`, so no product
conftest/ini/plugin applies; shared helpers live in
`acceptance/lib/`, on `PYTHONPATH`/`NODE_PATH`), a controlled `PATH` (system
directories plus the interpreter directories the driver resolved at
start), no symlinks in the pack, env scrubbed of `*_TOKEN`/`*_KEY`/`*_SECRET`/
`*_PASSWORD`/proxies and implicit-load variables (`PYTHONPATH`,
`NODE_OPTIONS`, `BASH_ENV`, ...), with `ACC_TREE`, `ACC_DIR`, `ACC_WORK`,
`ACC_ID`, `ACC_BIND_<NAME>`.

`FROZEN` lines: `manifest_sha256`, `base`, `frozen_utc`, `author`,
`dropped: <id> <reason>; ...`, then the pin chain `pin[0]: <sha256>
freeze`, `pin[k]: <sha256> amend ACC-NN` / `restore`. `manifest_sha256` is
the sha256 over the sorted `(relpath NUL bytes NUL)` of every pack file
except `FROZEN` (`__pycache__`/`*.pyc` ignored).

### PLAN.md mapping (METRICS_API 7)

- Slice key **`covers: [ACC-NN, ...]`** (optional; flow or block list of
  acceptance ids).
- Plain lines under `## Verification standard`: **`lead_integration:`**
  keeps its meaning (whole-goal deliverables) and its `ACC-NN` items are
  the checks the Lead satisfies itself; **`acceptance_bindings: {NAME:
  value}`** overrides a manifest binding's default (only declared names).
- Every `behaviour`/`doc` id must appear in some slice's `covers:` or in
  `lead_integration:`. The frozen checks are the GOAL's floor: PLAN may be
  stricter, never looser.

### Who may change `acceptance/`

The Lead, builders, repair and slice-evals never do. The Lead files
`ACCEPTANCE-DISPUTE: ACC-NN — <why; quote the GOAL>` lines in REPORT.md
and still maps the check. Only the integration Evaluator amends, by the
amendment protocol: `checks/<file>`, `fakes/**` and a check's `run`,
`expect`, `timeout_s`, `binds`, `needs` may change (`id`, `goal_quote`,
`kind` are immutable; no check is removed); every changed pack file must
belong only to amended ids (every check that can load it: its `run`
names it or its files mention it; a shared file -- `fakes/`, `lib/`,
interpreter-loaded names, module shadows, unowned helpers -- belongs to
every check); a new file may be added only under `checks/<ID>/` of an
amended ID and must be used by no other check; an amended `run` may not
point at another check's file; one `## ACC-NN · iter N ·
evaluator · <utc>` record per amended id in `AMENDMENTS.md` (`goal_quote:`,
`defect in check:`, `change:`); commit subject `acceptance: amend ACC-NN
(evaluator, iter N): <reason>`, touching only `acceptance/`; at most 2
amendments per loop and 25% of the checks; after the amendment the whole
pack re-runs at base and every check that FAILed there before must still
FAIL. Known limit (eval-r19c finding 2): that re-run is the only mechanical
test, so a named, counted amended check can still be weakened conditionally
on post-base state (FAIL at base, trivially PASS later); the budget, the
AMENDMENTS.md record and the pin chain bound and expose it, and per-check
isolation keeps it confined to the named checks. A human may change anything while the loop is stopped, only through
`trioctl omnigent acceptance amend --human --ids ACC-NN,... --reason "..."
[--adopt <sha>,...]` (refused while a driver holds or is taking the
mailbox lock; the command holds the lock itself while it runs). It
commits the working-tree edits as `acceptance: amend <ids> (human):
<reason>`, adopts pack commits you already made since the pin only when you
name them with `--adopt`, re-pins with an `Acceptance-Human-Amend: <shas>`
trailer, and records the adoption in the driver state outside the repo.

Resume never adopts: a hand-made `(human)` amend commit is restored as
tamper and logged (``not adopted: resume never adopts``). Resume checks the
driver state against the pin chain derived from git (a mismatch, or a lost
state git cannot re-derive unambiguously, stops with NEEDS_HUMAN). A
`(human)` amend commit that appears while the loop runs is a role's: the
driver logs ``unauthenticated `(human)` label`` and judges it as an
Evaluator amendment under every rule above (scope, record, budget,
must-FAIL-at-base, no removal).

### UNAVAILABLE

A check that exits 77 or has an unmet `needs` is UNAVAILABLE. It never
justifies ITERATE; with every other check passing the verdict must be
NEEDS_HUMAN, listing the UNAVAILABLE checks and exact commands under
`## Human check`.

### `trio-check.py` findings

Whenever `<mailbox>/acceptance/MANIFEST.json` exists these are
**violations** (exit 1, not advisory): an invalid manifest or check;
a `goal_quote` that is not verbatim in GOAL.md; the pack hash differing
from FROZEN's last pin with no amend commit explaining it; an unmapped
`behaviour`/`doc` id; an unknown id in `covers:`/`lead_integration:`; an
`acceptance_bindings:` key the manifest does not declare. WARN: more than
40% of the ids mapped only to `lead_integration:`; a `cli`/`http`/
`function` check covered only by slices whose `writes:` are docs. With a
pack present the r18a WARNs for a missing `goal_acceptance:` /
`goal_probe:` are not emitted (the frozen pack replaces the Lead-authored
goal acceptance). `coverage_refusals(mailbox)` exports the coverage part
for the drivers.

### Driver behaviour (acceptance on)

- **Author phase.** The driver builds the export (`git archive <base>`,
  no `.git`, mailbox dirs / archives / sessions / `.trio*` / `.cursor/` /
  vendored metrics removed, GOAL.md + notes in `.acceptance-input/`) in its
  state dir, dispatches the author (Cursor: registered
  `trio-omnigent-acceptance`, Evaluator tier) alongside the first Lead
  pass, audits the session's tool calls, validates on a fresh export, and
  commits `acceptance: freeze <n> checks (<model>)` + `Acceptance-Pin:`.
  STATE.md gains `acceptance_pin: <sha256[:16]> @<commit12>`; `.driver.json`
  `acceptance {status, pin, pin_commit, ...}`; the pin chain lives in the
  driver state file outside the repo
  (`$XDG_STATE_HOME/trio-agent-loop/acceptance/...`,
  `TRIO_ACCEPTANCE_STATE`).
- **LOG lines** (`- iter N | loop | ...`): `acceptance: frozen <n>
  check(s) @<sha12> pin <sha12> (dropped <k>; author <model>)`,
  `acceptance: author retry: ...`, `acceptance: author session
  contaminated (...)`, `acceptance tamper restored (<role>)`, `lead pass
  refused: acceptance coverage`, `gate breach after lead: acceptance ...`,
  `acceptance: integration pre-run @<sha12>: p/t PASS ... · unavailable=<n>`,
  `acceptance: amendment of ACC-NN accepted|rejected: ...`, `acceptance:
  forced NEEDS_HUMAN (acceptance-thrash|acceptance-amendments|
  acceptance-unavailable): ...`, `acceptance: ship_unaccepted
  (acceptance): ...`, `acceptance: SHIP gate: p/t PASS @<sha12>`,
  `acceptance-unavailable-iterate`, `acceptance: unavailable=<n>`,
  `acceptance: SHIP refused by the acceptance gate (verdict becomes
  ITERATE|NEEDS_HUMAN)`, `acceptance stopped the loop
  (acceptance-tamper-repeated|acceptance-goal-changed|
  acceptance-restore-failed): ...`, `acceptance: human amendment of
  ACC-NN re-pinned <sha12> (adopted <sha12>...)`, `acceptance: pin
  re-derived from git history <sha12> @<sha12> (driver state was
  missing)`, `acceptance: pack commits after the last driver pin <sha12>
  are not adopted and are restored as tamper ...`, `acceptance: forced
  NEEDS_HUMAN (acceptance-state-mismatch|acceptance-state-lost): ...`.
- **Author audit.** Contamination is evidence of a *read* of the loop:
  a tool-call argument, never a tool's output or chat text, that names a
  path inside the loop repository (its checkout, git dir or mailbox;
  relative paths resolve against the command line's tracked `cd`, and
  `$HOME`, `~`, `$PWD` are expanded); an ancestor of it given to a
  reading or searching command or as a bare tool path (`/` only for
  recursive searchers); `cd` into it or an ancestor; `$OLDPWD`;
  `/proc/<pid>/cwd|root`; or a lab hidden-pack path (`/hidden/`,
  `speed/hard`). A mailbox file name (`PLAN.md`, `REPORT.md`, ...) counts
  only inside the loop repository. Route literals (`/api/...`), `$TMPDIR`,
  toolchain and system paths never contaminate. The audit is best-effort.
  Without tool-call rows the limited audit flags only an authored pack
  that spells the loop repository's path.
- **Tamper escalation.** Every restored tamper counts in the driver state
  (`tamper_events`, published in `.driver.json`). The second restored
  tamper of the loop, in the same pass or a later one, stops it:
  `status: error`, reason `acceptance-tamper-repeated`, exit 3. This is
  §3.3's "a second breach sets status: error", counted loop-wide.
- **SHIP refusal record.** When the acceptance gate turns an Evaluator
  SHIP into ITERATE or NEEDS_HUMAN, the Evaluator may already have made its
  `loop: iteration N — SHIP` retirement commit. The driver therefore
  commits the LOG line as `loop: iteration N — acceptance gate refused
  SHIP (<new verdict>)`. The subject never matches the `loop: iteration N
  — SHIP` retirement needle.
- **Resume.** The driver state is reconciled with the pin chain derived
  from git; a mismatch forces `phase: acceptance-state-mismatch`, a lost
  state that git cannot re-derive unambiguously
  `phase: acceptance-state-lost` (both `status: needs_human`, exit 5).
  Resume never adopts an amendment. A frozen pack whose MANIFEST
  `goal_sha256` (read from git) no longer matches GOAL.md (a reused mailbox
  with a new GOAL) is never used to judge it: `status: needs_human`,
  `phase: acceptance-goal-changed`, exit 5. At every stop the driver calls
  the runner's `sweep_role_processes()` when it has one.
- **Driver commits** touching `acceptance/`: the freeze, `acceptance:
  restore (tamper after <sha12>)`, `acceptance: pin <sha12> (amend ...)`
  (each with `Acceptance-Pin:`; a human adoption's pin also carries
  `Acceptance-Human-Amend: <amend shas>`); made under the worktree's
  `index.lock`.
- **Commit gate:** `trio-shadow.py --require-commits` rejects any other
  commit touching the pack. It never trusts the working tree or a commit
  subject.
  - The pin chain is derived from git objects on the first-parent history
    from `--acceptance-base`, else the driver state's run head, else the
    root. The freeze is the first commit that adds `FROZEN` (a re-add is
    tamper). A pin must extend `FROZEN` only and pin a legitimate
    amendment (the Evaluator rules, discrimination re-run at the frozen
    base, or an `Acceptance-Human-Amend:` adoption); a restore must put
    back the current pin; HEAD's committed pack must be the last
    legitimate pin.
  - With the driver state visible (same `TRIO_ACCEPTANCE_STATE` /
    `XDG_STATE_HOME`) it only adds strictness: freeze, pin and restore
    commits must be the ones the driver recorded, a `(human)` amend must
    be one it adopted, and its pin must be the chain's.
  - Mailbox reuse without the driver state needs `--acceptance-base`.
  - Only a genuine restore excuses the earlier tamper it restored.
  - A `slice(<id>):` commit behind the freeze (a Lead take-over made while
    the author was still working) is tolerated with an `acceptance note:`
    line, because the author worked from the base export. A slice on a
    line that does not contain the freeze fails ("acceptance/freeze
    ordering").
  - The driver's `gate breach` LOG line quotes the first `acceptance gate:`
    reason.
- **Phases:** `needs_human` with `phase: acceptance-thrash`,
  `acceptance-amendments`, `acceptance-unavailable`,
  `acceptance-goal-changed`, `acceptance-state-mismatch` or
  `acceptance-state-lost`.
- **`trioctl omnigent acceptance wait`** returns as soon as the driver
  state says frozen, or when git shows the committed freeze of this
  mailbox's pack with a clean `FROZEN`. The Lead's shell need not carry
  the driver's state env.

## Mailbox placement standard

`loop/` lives in the orchestrator session's cwd — the coordination repo.
It is always singular (one loop per session) and never inside a worktree:
mailbox files are the coordination surface, not build artifacts.
Multi-repo projects are handled by the slice schema, not by extra
mailboxes: PLAN.md declares the product repos (`repos:`, below) and each
slice names its repo with `repo:`, defaulting to the coordination repo
(`home`). See "Declared repos (r15)" and "Repo scope (r15 guard)".

### Declared repos (r15)

The mailbox repo (the git repo containing the mailbox dir) is the implicit
**`home`** repo. The main session declares further product repos with the
GOAL, in PLAN.md's own ```yaml fence whose top-level key is `repos:`:

```yaml
repos:
  - name: app-backend            # kebab-case, unique; `home` is reserved
    path: app-backend            # relative to the mailbox repo root, or absolute
    base: feat/cp-gold-only-obo  # branch builders branch from and merge onto
```

No block (or `repos: []`) is single-repo mode, byte-identical to earlier
releases. Keys may come in any order or as a flow map (`- {name: a, path:
b}`); only `~` expands in `path:` (never `$VARS`); every path must be an
existing git repo, no two names may share one, and no non-`home` name may
point at the mailbox repo. `base:` defaults to the checkout's current
branch; when given it must be an existing branch of that repo (trio-check
refuses one that is not), and a builder dispatch refuses a repo whose
checkout is on another branch.

- **Slice ↔ repo**: `repo: <name>` (default `home`); `writes:` are relative
  to that repo's root; one repo per slice (`depends_on:` across repos is
  fine); disjoint writes are judged per repo.
- **Isolation per repo**: a slice's builder (and slice-eval) worktree is
  created from its repo (`git -C <path> worktree add`) under
  `<state>/worktrees/<name>-<sha256(git common dir)[:12]>/`, a sibling of the
  home repo's root (with `--worktree-root X` the declared repos' roots are
  siblings of `X`: `<parent of X>/<name>-<hash>/`); its ledger record lives in that repo's
  `.git/trio-worktrees/` with `repo_name`, and its merge lands on the repo's
  `base` branch. `.cursor` neutralisation, rebuildable/retention/cleanup
  rules are unchanged. The builder's targeted check runs from its worktree
  root, so every `cd` in it is relative (an absolute `cd` into the repo's
  main checkout is refused). The builder's JSON line carries `repo_name` and
  `repo`; recover a retained one with `omnigent worktrees integrate <id>
  --repo <repo path>` (or `--mailbox <dir>`, which finds the ledger).
- **Retire**: `retired:` entries gain `repo:` (see `QUEUE.md`).
- **Gate**: one Lead whole-tree gate per repo the pass changed, each with
  its own skip rule; the LOG.md line ends with every outcome joined by
  `; ` — home keeps `gate: PASS @<sha>` (so the historical grep is
  unchanged), a declared repo is `gate: PASS @<repo>:<sha>` (FAIL likewise;
  `gate: skipped (<repo>: no product change since <sha>)`). REPORT.md
  `## Whole-tree gate` rows carry the repo. `full_check:` may be per repo
  (see "Verification standard").
- **Evals**: a slice-eval pins `(repo, sha)` and binds its worktree from
  that repo. The integration eval pins one sha per repo: STATE.md keeps
  `evaluated_sha` (home) plus `evaluated_repos: <repo>@<sha> ...`, and
  VERDICT.md records one field line `evaluated: home@<sha>, <repo>@<sha>,
  ...` (single-repo stays a bare sha). Worker merges are fenced in every
  repo while it grades. It runs each repo's `full_check:` from that repo's
  root and any `lead_integration:` smoke in home.
- **SHIP retirement**: in each declared repo that has slices, one empty
  `loop: iteration N — SHIP (<mailbox>)` commit on its base branch
  (recorded as `commit: <repo>@<sha>`), plus the home mailbox commit
  listing every `commit: <repo>@<sha>`. The driver accepts the SHIP only
  when every repo's pin is recorded, its product tree is unchanged since
  the pin, its `commit:` lines are reachable, and its retirement commit
  descends from the pin; post-SHIP cleanup removes each repo's worktrees
  against that repo's pin. The check starts from the STATE.md
  `evaluated_repos` pins, not from the current PLAN.md: a pinned repo that
  is no longer declared (dropped, moved or deleted) or a `repos:` block
  that no longer validates makes the SHIP final (never accepted on the
  home checks alone), and an integration pin is reused on resume only
  under the same rule.
- **Lockstep**: the same pins and per-repo retirement apply. trioctl
  renders a MULTI-REPO procedure into the lockstep prompts too (the Lead's
  per-repo dispatch and `slice(<id>):` commits; the Evaluator's pin list,
  `evaluated:` line and empty per-repo SHIP commits), only when PLAN.md
  declares `repos:`; single-repo lockstep prompts are unchanged.
- **Compat**: needs METRICS_API 5 in the repository's vendored `metrics/`
  -- in the loop core itself (`trio_loop.py` `METRICS_API = 5`) and in its
  `trio-metrics.py`; trioctl takes the lower of the two, so a partial
  refresh (an older core next to a newer trio-metrics.py) is refused too;
  `trioctl` still drives a METRICS_API 4 set for single-repo mailboxes and
  refuses a `repos:` PLAN with it (loop `status: error`, nothing
  dispatched).

### Repo scope (r15 guard)

Worktrees, retire shas, the whole-tree gate and the eval pin all resolve
in the **slice's repo**: the mailbox repo, or (with `repos:`) the declared
repo its `repo:` names. A slice that writes anywhere else is refused,
never limped through (silent Lead
take-over, empty aggregate merges, unresolvable retire shas). A slice is
*outside* when its `repo:`, any `writes:` path (resolved against the
mailbox repo root; `api:` entries excluded), or any `cd` in its builder
brief's `## Targeted check` section (`briefs/<id>.md`, resolved from the
repo root) is an absolute path elsewhere, a `..` escape, a `repo:` that
names no directory, or lands in a nested git repo inside the mailbox repo
(any directory below the root holding its own `.git`, e.g. a gitignored
clone) — and no declared `repos:` entry covers it. Each offending slice
yields exactly:

```text
slice <id> writes outside the mailbox repo (<path>); declare it in PLAN.md repos: (r15) or move the mailbox into that repo
```

The mailbox repo is always named `home` (`repo:` omitted, `.` or `home`;
there is no other alias, e.g. `coordinator`). Files under the mailbox
directory itself (evidence, receipts, `results/`, `scripts/`) are Lead
work, never a builder slice: a `home` slice any of whose `writes:` resolve
at or under the mailbox directory is refused (eval-r16rc G1; not when the
mailbox is the repo root; a declared clone nested there writes relative to
its own root and is unaffected):

```text
slice <id> writes under the mailbox directory (<path>); files under <mailbox> (evidence, receipts, results, scripts) are Lead work the Lead writes and commits itself, never a builder slice: drop the slice from PLAN.md slices: (the mailbox repo is always `home`)
```

The brief scan covers every targeted-check section (`##`-`######
Targeted check[s][ (...)]` headings, and a `**Targeted check:**`,
`__Targeted check__` or plain `Targeted check:` label line together with
the text after the label, each up to the next heading), `cd`/`pushd`
(also `cd -- <dir>`, `cd -P <dir>`) in plain lines, fenced blocks and
inline code, and the directories named by `git`/`make`/`env`/`npm`/
`pnpm`/`poetry` `-C`, `pnpm --dir`, `--rootdir`, `--prefix`, `--cwd`,
`--directory` (`uv run --directory`) and `--chdir`; shell comments are
ignored. A glob in
`writes:` is checked against every existing match of each path prefix.
PLAN.md's own targeted-check sections and `full_check:` commands are
scanned from the repo root (refusal line `PLAN.md targeted/full check runs
outside the mailbox repo (<path>); ...`). A `slices:` block that does not
parse is refused (the guard fails closed); a symlink loop is treated as
a path, never a crash.

`trio-check.py` exits 2 with one such stderr line per slice (v1
mailboxes). `trioctl omnigent loop` refuses before dispatching anything (under the
mailbox lock: a mailbox a live driver owns is left byte-identical, exit 5),
and again around every Lead/repair pass: it appends `- iter N | loop |
<line>` to LOG.md, sets STATE.md `status: error`, and exits 3.
`trioctl omnigent run builder --isolate` checks its slice and its
`--prompt-file` brief and exits 2 before creating a worktree.

**With `repos:` declared**, each slice is checked against its own repo
instead: `writes:` resolve against that repo's root and must stay in it,
and its brief's targeted-check directories start at that root and must
stay in the same repo — relative only for a declared repo (an absolute
path is its main checkout, not the builder's worktree). Refusal lines:
`slice <id> of repo <r> touches repo <other> (<path>); one repo per slice:
split it ...`, `slice <id> writes outside its repo <r> (<path>); ...`,
`slice <id> targeted check cds into the main checkout of repo <r>
(<path>); ...`, `slice <id> repo: '<x>' is not declared in PLAN.md repos:
...`; an invalid block, a `full_check:` key naming no declared repo, or a
per-repo `full_check:` command that leaves its repo is refused too. A home
slice that writes into an undeclared nested clone still gets the exact
single-repo line above. `repos: []` equals no block.

### Root-free loops (r16)

`trioctl omnigent loop` runs every mailbox inside a git checkout
**root-free**: open-loop (QUEUE.md present) since r16a, lockstep since
r16b. `--root-bound` was removed in r16b (refused, exit 2, with a pointer
to `land`/`abandon`); a mailbox outside any git checkout runs in place.
Operator guide: `docs/ROOT-FREE-OPEN-LOOP.md`.

- **Live mailbox.** The driver creates (or re-attaches) the loop's Lead
  worktree `<worktree_root>/lead-<slug>` on branch `trio/<slug>`, forked
  from the target branch tip (`slug` = mailbox path relative to the repo
  root, `/` → `--`, other chars outside `[A-Za-z0-9_-]` → `-`). The root
  mailbox's tracked and untracked non-ignored files are committed there
  as `loop: seed <mailbox>`; for the whole run the live mailbox is
  `<lead-wt>/<mailbox>`, and the root copy is not touched until land.
- **Files.** Ledger record `<git common dir>/trio-worktrees/lead-<slug>.json`
  (`kind: "lead"`, state `creating|active|landed|retained|removed`; lets
  `--mailbox loop/<x>` from the root resolve the live mailbox); the
  live-loop registry record `<common>/trio-worktrees/loops/<slug>.json`
  while a driver runs (the same record every mode writes: see "Live-loop
  registry" below); `<live mailbox>/.sessions/aggregates.json` maps each
  declared `repos:` entry to its per-run aggregate on `trio/<slug>`.
  Integration fences live under `<common>/trio-worktrees/fences/<branch-slug>/`.
- **Root mailbox lock** (eval-r16rc-b M1): the driver holds the root
  mailbox's `.lock` (the loop core's `.lock/{owner,pid}` protocol) from
  before it creates anything until after the cleanup, so a lock-only driver
  (native core, pre-r16 release) on the same mailbox refuses; it is never
  seeded or landed.
- **Lockstep (r16b).** Lead, repair and Evaluator all run in the Lead
  worktree (the Evaluator runs there too, not in its own separate
  `git worktree add` at the pin, and makes its SHIP retirement commit in
  that workspace, i.e. on `trio/<slug>`); builders run non-isolated in the Lead
  worktree unless
  `--isolate-workers`. After this run's SHIP the driver (trioctl, no core
  change) lands exactly as for open-loop; a `reverify` sets
  `phase: lead-done` with the pin cleared so one fresh Evaluator grades the
  merged tip. A live Lead worktree whose STATE is `needs_land` or `shipped`
  without `landed:` resumes at the land. The prompts carry a `ROOT-FREE
  (this lockstep loop runs in its own Lead worktree)` block.
- **STATE.md keys** (driver-owned, root-free runs only): `target_ref:`
  (branch it lands onto), `target_base:` (target sha at fork), `landed:`
  (verified loop-branch sha that landed). Status `needs_land`. Phases:
  `landing`, `landed`, `land-blocked`, `land-conflict`, `land-starved`,
  `land-reverify` (open-loop), `land-error`, `worktree-setup`,
  `driver-exception`.
- **Land (SHIP only).** Under the repo-wide land lock, per aggregate
  (declared repos first, home last): a moved target is *merged* into the
  loop branch (never rebased); a conflict aborts → `needs_land` /
  `land-conflict`. A clean merge touching none of PLAN's `writes:`/`reads:`
  runs `full_check:`; otherwise (or on failure) one new evaluation of the
  merged branch (open-loop: integration-eval, `land-reverify`; lockstep:
  Evaluator), at most 2 rounds, then `land-starved`. The home land commits the live mailbox as `loop: land
  <mailbox> (iteration N)` (`status: shipped`, `phase: landed`) and
  advances the target (`git merge --ff-only` where it is checked out, else
  a compare-and-swap `update-ref`); a refused fast-forward →
  `land-blocked`. Then runtime files are copied to the root mailbox, the
  worktrees removed (retained with a reason when unsafe) and `trio/<slug>`
  deleted unless `--keep-branch`.
- **Overlap.** Nothing runs at the root (the r15.x root-turn lock, the
  root stranger check and exit 9 were removed in r16b). The cross-loop
  `writes:` refusal applies at start: refused with exit 2 before anything
  is created. Mid-run (an overlap that appears at a later Lead pass, e.g.
  the other loop's Lead widened its PLAN) a loop only warns -- one LOG
  line per overlap naming the other loop and the paths, `.driver.json`
  `writes_overlap: [...]` -- and continues: its private aggregate is never
  shared, and the land merges and re-verifies the other loop's change (a
  real conflict ends in `needs_land`/`land-conflict`) (eval-r16rc N4).
- **Exit codes**: see "Driver exit codes" below (0 means verified AND
  landed; **8 needs_land** resumes with `trioctl omnigent land --mailbox <x>`).
- **Compat**: root-free needs METRICS_API 6 in the repository's committed
  `metrics/` at the target tip; an older set is refused (exit 3, nothing
  changed), lockstep included since r16b (no root-bound fallback). A
  gitignored mailbox protocol file or brief is refused (exit 3) before
  anything, the root lock included, is touched.

## HUMAN.md (human answers)

`loop/HUMAN.md` is the one place a person answers a stopped loop. There is
no input channel into a running Lead or Evaluator session (Omnigent or
claude-workflow); a NEEDS_HUMAN or BLOCKED stop is resolved as
stop → answer → reset → re-run.

- **Writers**: the trio-dash answer box (`POST /api/loop/answer`, only for a
  loop whose driver is not live and that is stopped at NEEDS_HUMAN/BLOCKED).
  Agents never edit it. The dashboard refuses a HUMAN.md (or STATE.md) that
  is a symlink and writes with `O_NOFOLLOW`.
- **Format**: optional `# Human answers` header, then append-only entries,
  newest last. Only the header line is server-written structure; the
  answer text is quoted line by line (`> `), so no answer can forge a
  header:

  ```
  ## 2026-09-29T13:05:00Z — answer 3f9c01ab22d4 — iteration 3 — trio-dash 9a0c…(24 hex)
  in-reply-to: STATE.md status needs_human (iteration 3)
  source: trio-dash (<tailnet login or client address>)

  > the human-check result or the decision,
  > one quoted line per answer line
  ```

  `iteration <N>` is the iteration whose NEEDS_HUMAN/BLOCKED stop it
  answers. The last field is an HMAC-SHA256 (first 24 hex) over the time,
  id, iteration and answer text. The text is signed in canonical form:
  every line separator Python's `str.splitlines` knows (CRLF, CR, VT, FF,
  FS/GS/RS, NEL, U+2028, U+2029) becomes `\n` before signing, quoting and
  hashing, so what is signed is exactly what the parser reads back.
- **Answer ledger** (outside every workspace, in the dashboard's state dir
  `$TRIO_DASH_STATE_DIR`, default `~/.local/state/trio-dash/`):
  `answer-key` (32 random bytes as 64 hex, mode 0600; generated by the
  dashboard when missing, atomically, never by a driver) and
  `answers.jsonl` (one record per answer: `id`, `loop` key, `mailbox` and
  `root_mailbox` real paths, `iteration`, `at`, `sha256` of the answer
  text, the **stop binding** — `v`, `goal_sha256`, `verdict_sha256` and
  `verdict_len` (VERDICT.md as it was when the human answered; empty for a
  STATE.md-only stop), `verdict_commit` (the last commit that touched
  VERDICT.md), `head` (the checkout's HEAD) and `state_sha256` (STATE.md's
  stop record, informational) — and `mac` = HMAC-SHA256 over all of those
  fields). The record is written before the HUMAN.md entry.
  `consumed.jsonl` (same dir) lists the answers a driver has delivered to
  the Evaluator that ruled on them. A key that is empty, not 64 hex, a symlink or
  group/other-readable is refused with a clear error (answers are refused,
  entries show as unverifiable); it is never replaced or used empty. The
  dashboard marks an entry UNVERIFIED unless its header signature AND a
  ledger record match it. Shared code: `metrics/human_ledger.py`.
- **Driver verification** (the only way an answer reaches a role): before
  every Lead and Evaluator dispatch, the driver — `trio_loop.py`'s portable
  runner, `trioctl`'s OmnigentRunner (with its own release's ledger module,
  never the repository's vendored loop core) and the claude-workflow helper
  (`next` for the Lead, `pin` for the Evaluator) — takes the newest ledger
  record for the mailbox and passes its answer only when (a) its HUMAN.md
  entry is intact (header signature, text digest, time, iteration); (b) it
  answers the stop that is still current: GOAL.md is unchanged, the
  VERDICT.md the human answered is still on disk as the latest stop (an
  open-loop run may only have appended `## slice` sections, never a new
  `VERDICT:` line) and, in a git checkout, the answer-time HEAD is an
  ancestor of HEAD, no commit since deleted or moved GOAL/STATE/VERDICT/
  HUMAN.md (an archived mailbox, a new run in the same path) and every
  commit since that touched VERDICT.md kept that stop; (c) the dispatched
  iteration is the stopped one (the open-loop integration-eval after a
  Lead pass that changed nothing) or the next one; (d) it was not consumed.
  The Evaluator dispatch that rules on the answer (lockstep Evaluator,
  open-loop integration-eval, claude-workflow `pin`) marks it consumed in
  the ledger as it is delivered — the Lead and then the Evaluator of the
  same iteration are one use; an open-loop slice-eval receives it without
  consuming it — and a consumed answer is never delivered again (a failed
  mark means no delivery). So an answer can never be replayed into a later
  run in the same mailbox path, not even from committed history (eval3
  finding 1); answer again for a new stop. The answer then appears at the end of the role prompt
  as a driver-written `## Verified human answer (driver)` block (text
  quoted). Every other entry — hand-written, forged, edited, stale, or any
  entry when the key or ledger is unusable — is ignored and reported on the
  driver's stderr (claude-workflow: the step's `human_notes`, logged by the
  script). Without HUMAN.md in the mailbox the driver reads nothing else and
  every prompt is unchanged.
- **Readers**: roles never trust HUMAN.md text. The Lead applies the
  driver's block (canonical `trio-lead` input 5; Omnigent `prompts/lead.md`
  step 1; the claude-workflow plan call) and cites its answer id; the
  Evaluator (canonical NEEDS_HUMAN rule, Omnigent `prompts/evaluator.md`
  step 1, the claude-workflow Evaluator call, protocol essentials) counts
  ONLY the driver's block as evidence for a `verify: human` criterion whose
  `## Human check` result it reports. HUMAN.md is a regular mailbox file
  (committed with the mailbox; not a runtime file). Root-free loops keep it
  in the live mailbox (the Lead worktree copy), like STATE.md.
- **Where drivers find the ledger module**: `trio_loop.py` next to itself;
  `trioctl` next to itself first (install.sh copies `human_ledger.py` into
  `$TRIOCTL_BIN_DIR`), then `<release>/metrics/`; the claude-workflow
  helper in `<release>/metrics/`. No module: nothing is passed.
- **claude-workflow relay limit**: in a claude-workflow run the verified
  block travels from the helper to `trio-native.js` inside the `trio-step`
  agent's stdout, which that (Sonnet) agent returns verbatim; the step
  agent runs in the product checkout with the nonce in its prompt, so a
  prompt-injected step agent could fabricate the field. Every helper result
  has this limit (it is the workflow's design); the ledger check itself
  runs in the helper.
- **Limit (same OS user)**: the key and the ledger belong to the same user
  as the loop's roles. The ledger stops forgeries by a role that writes the
  repository or the mailbox (the eval2 NEW-3 scenario), but a role that
  deliberately reads the 0600 key (or appends to the ledger with it) can
  still forge an answer. Closing that needs a separate OS user (or a
  sandbox without read access to `~/.local/state/trio-dash`) for the
  roles; it is not done here.

- **STATE.md**: the answer box may reset the loop in the same step (shown
  and confirmed first): `status: running`, `phase: idle` (the next run
  starts a fresh Lead pass), `reason:` removed, and
  `human_answer: HUMAN.md#<id> (<UTC time>)` added. Without a reset only
  HUMAN.md changes. A held dispatch (`.sessions/held-*.json`) is never reset
  this way; reconcile it first.

## Dashboard records (trio-dash, dash-actions)

- `<mailbox>/.native-result.json` and the per-user claude-workflow run
  registry `~/.local/share/trio-agent-loop/native-runs/<key>.json` are
  written by the native driver (`native/README.md` "Run registry and
  result"); `.native-result.json` is a runtime file (mailbox `.gitignore`).
  The dashboard uses both for display and state only: a `launcher` or
  `helper` path in them (or in `.native-launch.json`) is never executed —
  fixes run the installed release's `native/launch.sh`. A registry record
  seeds a workspace only when its `repo` is the mailbox's real git toplevel
  (never `/`, the home directory or an ancestor of it); records are deduped
  by the mailbox's realpath (newest `updated_at` wins).
- The dashboard's own per-loop files live outside every workspace:
  `~/.local/state/trio-dash/loops/<key>/` (`TRIO_DASH_STATE_DIR`) holds
  `actions.jsonl` (append-only log of every fix, answer and diagnosis: who,
  when, commands, outcome, output tail), `diagnosis.json` (latest read-only
  diagnosis) and `runs/*.log` (output of drivers it started); `<key>` is the
  first 16 hex of sha256(realpath of the root mailbox). `answer-key` (0600)
  and `answers.jsonl` are the answer ledger (HUMAN.md, above);
  `cursor-isolated/` is the empty HOME/config of opt-in Cursor diagnoses.
- **Mailbox files are data, never commands.** Nothing the dashboard reads
  from a repository or mailbox (committed or not) chooses what it executes,
  which flags or arguments it passes, or which paths it writes outside the
  mailbox, unless the value passes a strict schema: a native resume needs a
  canonical-UUID `session_id`, a `wf_…` run id and a `run_token`-bearing
  args object with only the workflow's known keys (`mailbox` = this
  mailbox, bounded caps, allowlisted models, no helper but the release's),
  and `launch.sh resume` re-checks all of it and rebuilds its prompt from
  the validated fields. Both use ONE validator, `metrics/native_args.py`
  (byte-identical in the dashboard and the native release), so a preview
  never offers a resume `launch.sh` refuses: `args.mailbox` must also be
  canonical (no `.`, `..` or empty component, no trailing slash) and every
  `models` value a string from the allowlist; any other value or type is a
  validation error (HTTP 409 / exit 2), never a 500.
- **Mailbox path rule** (every dashboard driver start and `launch.sh`):
  an absolute path of printable characters. Spaces, non-ASCII letters,
  `,`, `~`, quotes and other printable punctuation are allowed; control
  characters (NUL, newline, CR, tab, ESC …), NEL, U+2028/U+2029, format
  characters (bidi overrides, zero-width) and unpaired surrogates are
  refused. The path travels only as one argv element (never through a
  shell), and every driver writes it into a role prompt through the same
  quoting (`native_args.prompt_path`, trioctl `_pp`, `portable/driver.sh`,
  `trio-native.js`): unchanged when it only uses `[A-Za-z0-9._/+@-]`,
  otherwise POSIX-shell single-quoted, so it is one inert token inside the
  prompt's backticks and in any command the prompt spells out.
- Every mailbox file is read without following symlinks and only
  when it is a regular file; a mailbox containing symlinks (directly, or in
  `.lock/` or `.native-runs/`) is shown as refused
  and nothing in it is read or acted on. Writes go through a `mkstemp` file
  in the same directory and a rename (never a fixed temp name) and refuse
  a target that is a symlink.
- **Native runs use the project's Claude config.** A claude-workflow run
  is `claude -p` in the product checkout, so the checkout's project
  `.claude/` configuration (settings, hooks, agents, a same-named project
  workflow) applies to it like to any Claude Code session there; only run
  loops on repositories whose `.claude/` you trust.
- **Worktree root must match.** A root-free Lead worktree counts as the
  loop's live mailbox only when it is a real worktree of the workspace's
  repository under the workspace or under the Trio worktree root the
  dashboard computes from ITS OWN environment (`$TRIO_WORKTREE_ROOT`, else
  `$XDG_STATE_HOME`/`~/.local/state/trio-agent-loop/worktrees/<repo>-<hash12>`,
  the driver's default). A loop started with `--worktree-root` or a
  different `TRIO_WORKTREE_ROOT` (e.g. a sibling `<repo>-worktrees/`
  layout) is refused by the dashboard unless the same root is set in the
  trio-dash service environment too (`~/.services/trio-dash/env`: add
  `TRIO_WORKTREE_ROOT=<dir>` and restart the service).
- **Confirm tokens bind what runs.** `reconcile_apply`'s token includes
  each held record's digest (`.sessions/held-*.json`), and `retire_ship`'s
  includes the set of mailbox files `git add -- <mailbox>` would stage
  (path and content digest): any change between preview and confirm is a
  `409 plan_changed` with the new preview.
- **Codex diagnosis reads $HOME.** The default Codex diagnosis runs in its
  OS read-only sandbox, and its commands have no network; it can still
  read files under $HOME (accepted: read-only). What it reads can reach
  its model provider and its answer, which the dashboard shows, so
  injected mailbox text could make it quote a secret it read.

## Session sidecar

`loop/.session.json` is the harness-owned session sidecar (see
`prompts/protocol-essentials.md`) recording the live orchestrator pid and
session id; conformance tooling ignores it.

`.sessions/` is a separate, optional driver-owned archive directory:
`trioctl omnigent sessions prune` (and `loop`'s default post-loop cleanup,
skippable with `--keep-sessions`) write one JSONL transcript per archived
broker session there before deleting it from the broker. Conformance
tooling ignores it too.

`trioctl omnigent loop` appends any missing runtime entry (`.dispatch/`,
`.driver.json`, `.driver.pid`, `.session.json`, `.sessions/`, `driver.log`,
`.lock`, `.repairs`) to `<mailbox>/.gitignore` at loop start; existing
lines are never rewritten. The `.gitignore` may itself stay untracked
until the next mailbox commit: files inside the active mailbox never block
the dirty-checkout gate or the untracked-product scan.

## Driver exit codes (`trioctl omnigent loop`)

| Exit | STATE.md `status` | Meaning |
|---|---|---|
| 0 | `shipped` | SHIP accepted (retirement complete) and, root-free, landed (`phase: landed`); also a root mailbox that is already `shipped` (nothing run, r16b) |
| 1 | unchanged, or `error` (`phase: driver-exception`) | trioctl error of a mailbox outside any git checkout (run in place); a dispatch exception is recorded in STATE/LOG/sidecars first (r15.x) |
| 2 | `blocked` / unchanged | BLOCKED verdict; or start refused: `writes:` overlap a live loop of the same repository (stderr only, nothing created); or a refused flag combination (`--root-bound`, removed in r16b; an open-loop without isolated builders; a detached root without `--target`; `--root-free` outside git) |
| 3 | `error` | loop error (stalled Lead, gate error, repo-scope refusal, unparseable verdict, old loop core, gitignored mailbox; r19: acceptance on with a loop core below METRICS_API 7 or a tier mismatch, a contaminated/failed/timed-out author, a second acceptance gate breach); root-free: setup failure (`phase: worktree-setup`) or any driver exception (`phase: driver-exception`, recorded by the same stop mechanism as exit 1) |
| 4 | unchanged | `--max-iterations` reached |
| 5 | `needs_human` / unchanged | NEEDS_HUMAN verdict; or the mailbox is owned by a live driver (left byte-identical; a registered driver, or a live pid in the root mailbox's `.lock`, eval-r16rc B1/M1). (r15.x's Lead-pass `writes-overlap` stop was removed in r16b: a mid-run overlap warns) |
| 6 | `needs_retirement` | SHIP verdict whose retirement cannot complete |
| 7 | `needs_human` | held dispatch (`.sessions/held-*.json`); resume after reconcile |
| 8 | `needs_land` | root-free (r16): the verified loop branch could not land (`phase: land-blocked`, `land-conflict`, `land-starved`, `land-error`); resume with `trioctl omnigent land --mailbox <x>` |
| 9 | — | retired in r16b (r15.x `root-occupied`: nothing runs at the root any more) |
| 10 | unchanged | `trioctl omnigent run builder` only (r19, acceptance on): builder refused before any worktree -- pack not frozen, PLAN.md uncommitted, a frozen check unmapped, or the pack off its pin |
| 130 | unchanged | interrupted (SIGINT/SIGTERM) |

`reason:` (r15.x) is a driver-owned STATE.md line written with a driver
stop (`driver-exception`; `root-occupied` and `writes-overlap` from older
releases) and removed when the loop is resumed. See
docs/CONCURRENT-SLICE-EVAL.md "Many loops per repo".

### Live-loop registry

Every `trioctl omnigent loop` driver in a git checkout (open-loop and
lockstep, both root-free since r16b) writes ONE record `<git common dir>/trio-worktrees/loops/<slug>.json`
(`slug` as for the Lead worktree, from the root mailbox path) while it
runs, and removes it at exit (only while its own pid still owns it; a
crashed driver's record is ignored once its pid identity is dead). Keys
(`schema: 1`):

| Key | Meaning |
|---|---|
| `schema`, `slug`, `mode` | `1`; the loop slug; `root-free` (open-loop) or `lockstep` (both root-free since r16b; `root-bound` only in records of older releases) |
| `root` | the repository root the loop belongs to (realpath) |
| `mailbox`, `mailbox_rel` | the root mailbox (absolute, and relative to `root`) |
| `live_mailbox` | where the driver reads/writes it (= `mailbox`, or the Lead worktree copy) |
| `lead_worktree`, `branch`, `target_ref` | the Lead worktree, `trio/<slug>`, the target (`null` only in records of older root-bound runs) |
| `aggregates` | `{repo name: aggregate checkout}` (`home` + each declared repo) |
| `pid`, `pid_start`, `started_at` | driver identity (pid + `/proc` start ticks), ISO start time |
| `writes_by_repo` | `{repository identity: [writes...]}`; identity = the repository's main checkout (realpath), the same for every worktree of it |

A second driver for a mailbox that has a live record exits 5. Readers:
the cross-loop `writes:` refusal and mid-run warning, `trioctl omnigent status`
(`driver`), `abandon`, `aggregate_for`, and the dashboard board
(`live_driver` per loop card, via `trio-metrics.py:live_driver`).

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
least one v1 mailbox violates this schema: 1 for violations, 2 when a v1
mailbox is refused by the repo-scope guard ("Repo scope (r15 guard)") or
the sibling `trio-metrics.py` has a different METRICS_API. Legacy and
unknown mailboxes never affect the exit code.

When `QUEUE.md` is present, `trio-check.py` also validates it (block
shape, fault statuses, `retired:` entries referencing slice ids that
exist in PLAN.md) — see "v1 open-loop extension (optional)" above. Its
absence is never a violation.

Quality lints (r18a, advisory; `--strict-quality` makes a `REJECT` a
violation): the `accepts:` grammar (see "`accepts:` grammar"), a missing
`goal_probe:`/`goal_acceptance:` in an open-loop mailbox, and a
`full_check:` made only of artifact readers (`--verify-only`, `jq`/`cat`
of a JSON receipt, presence checks, or scripts and tests under the
mailbox directory). They print as `quality: REJECT|WARN ...` lines and
under `quality` in `--json`.

## Changelog

- **r20** (merge of r19-acceptance, dash-actions and native-v01 onto
  r17-rc-fixes; no METRICS_API bump beyond r19's 7):
  - *dash-actions HUMAN.md answer flow*: trio-dash's answer box appends a
    signed entry to `loop/HUMAN.md` and a ledger record outside the repo
    ("HUMAN.md (human answers)", "Dashboard records"); before each Lead /
    Evaluator dispatch the driver (trioctl `_human_answer_block`, the loop
    core's `human_answer_block`, the native helper) verifies the newest
    answer with the release's `metrics/human_ledger.py` and appends it as
    the last prompt block `## Verified human answer (driver)` only when it
    answers the current stop (with acceptance on: rigor -> acceptance ->
    human answer); without HUMAN.md every prompt is unchanged. The ruling
    Evaluator consumes it; a slice-eval receives it without consuming it;
    repair and the acceptance author never get it. One `SAFE_GIT_CONFIG` tuple (human_ledger, copied into
    trio-metrics) on every dashboard, ledger, trio-metrics and trio-shadow
    `_git` call; `install.sh --omnigent` ships `human_ledger.py` next to
    trioctl, `--dashboard` ships `human_ledger.py` + `native_args.py`.
  - *native v0.1* (claude-workflow driver, lockstep only, acceptance
    off): `.native-result.json` and the per-user run registry
    (`native-runs/<key>.json`) for trio-dash, driver-verified HUMAN.md
    answers in the native plan/evaluator calls, the shared
    `metrics/native_args.py` resume/path validator, an ownership ledger
    for every driver merge/removal/delete, builder shas corrected from
    git, fresh-run reclaim of builder worktrees, and the saved workflow
    script reported as the one Claude Code actually runs. The frozen
    acceptance seams (docs/FROZEN-ACCEPTANCE.md, N1-N4) are not in v0.1.

- **METRICS_API 7** (r19 frozen acceptance, behind `[acceptance] enabled`,
  default off): the `covers:` slice key and `parse_plan_acceptance`
  (`lead_integration:` ACC ids, `acceptance_bindings:`) in trio-metrics;
  `metrics/trio-acceptance.py` (runner, manifest, export, audit, driver
  commits) joins the vendored set (five files; `metrics refresh` copies
  it); trio-check `acceptance_findings`/`coverage_refusals` (violations
  whenever a pack exists); trio-shadow's acceptance guard in
  `--require-commits`; the loop core's `acceptance=` argument (author
  phase, pin checks + restore, coverage gate, covered-check and
  integration pre-runs, amendments, anti-thrash, SHIP gate) -- without it
  both modes are unchanged. `trioctl` (`REQUIRED_METRICS_API = 7`,
  `COMPATIBLE_METRICS_APIS = (4, 5, 6, 7)`, `ACCEPTANCE_METRICS_API = 7`)
  refuses `--acceptance` on an older core and drives it unchanged
  otherwise; builder exit 10; registry profile
  `cursor-grok-4.6-medium+glm-5.2-max-v4-acc` with the optional
  `trio-omnigent-acceptance` anchor. Independently of the switch (C1):
  slice-eval sections carry one `evidence:` line (no table, attacks or
  probe; `attacks=n/a` in the LOG line); the whole-goal rigor rides only on
  integration-eval and lockstep prompts. See docs/FROZEN-ACCEPTANCE.md.

- **r16b** (no METRICS_API bump: no loop-core change): lockstep runs
  root-free (Lead, repair and Evaluator in the Lead worktree; the land is
  driven by trioctl with the open-loop rules and exit 8); `--root-bound`
  removed (refused, exit 2); an open-loop without isolated builders is
  refused instead of falling back; exit 9 `root-occupied` retired with the
  r15.x root-turn lock, the root stranger check and the root `.cursor`
  snapshot/restore/session records; a mid-run `writes:` overlap only warns
  in every mode; registry `mode` is `root-free` or `lockstep`; a root-free
  driver holds the root mailbox `.lock` for its whole run (eval-r16rc-b M1)
  and refuses any gitignored protocol file or brief before touching it
  (L2/L3); an already `shipped` root mailbox is a no-op (exit 0).
- **r18a** (verified output quality, first slice; no METRICS_API bump):
  the `accepts:` grammar with oracles, `goal_acceptance:`/`goal_probe:`,
  enforced `mode:`, evidence kinds and the per-accept table in slice
  sections, `## Independent probe` in whole-goal verdicts; advisory
  `trio-check.py` quality lints; the base-revert kill check in shadow
  (builder JSON, ledger record, driver LOG line, `.driver.json`
  `quality`, trio-shadow).

- **r16-rc** (merge of r15-fixes, r15.x and r16a): one live-loop
  registry record for every mode ("Live-loop registry"; replaces r15.x's
  `loops/<slug>-<pid>.json` and r16a's root-free-only entry); the
  cross-loop `writes:` refusal applies to root-free loops too (r16a only
  warned); one driver-stop mechanism (`reason:` + LOG + sidecars) for
  root-bound and root-free driver exceptions; exit table merged.
- **METRICS_API 6** (r16a root-free open-loop): the open-loop driver
  runs in a per-loop Lead worktree on `trio/<slug>` and lands it onto the
  target after the integration SHIP ("Root-free open-loop (r16)"). New
  driver-owned STATE.md keys `target_ref`/`target_base`/`landed`, status
  `needs_land` (loop exit 8), phases `landing`, `landed`, `land-*`,
  `worktree-setup`, `driver-exception`; the core gains an additive
  `land=` hook (`LOOP_CORE_API` stays 2). `trioctl` (`REQUIRED_METRICS_API
  = 6`, `COMPATIBLE_METRICS_APIS = (4, 5, 6)`) refuses root-free open-loop
  on an older committed set but still drives it for lockstep and
  `--root-bound`; `trio-check.py` requires 6.
- **r15.x root-turn lock** (no METRICS_API bump; trioctl and
  worker_worktrees only): exit 9 `root-occupied`, STATE `reason:`, the
  live-loop registry under `<git common dir>/trio-worktrees/loops/`, and
  the cross-loop `writes:` refusal (exit 2 at start, exit 5 mid-run; since
  eval-r16rc N4 a root-free loop warns mid-run instead).
- **METRICS_API 5** (r15 multi-repo slices): `trio-metrics.py` gains
  `parse_repos_block`/`read_repos`/`mailbox_repo_root`, `slice_repo_name`,
  `parse_repo_pins` and `parse_full_check`; `retired:` entries may carry
  `repo:`. The loop core pins, verifies and retires per declared repo
  (`evaluated_repos` STATE.md key; `LOOP_CORE_API` stays 2 -- additive).
  `trioctl` (`REQUIRED_METRICS_API = 5`, `COMPATIBLE_METRICS_APIS = (4,
  5)`) still drives a vendored API-4 set for single-repo mailboxes and
  refuses a `repos:` PLAN with it; `trio-check.py` requires 5. The r15
  guard's "not supported" refusal is lifted; the undeclared-path refusal
  stays. eval-r15 fixes: `trio_loop.py` carries its own `METRICS_API = 5`
  (trioctl takes the lower of core and metrics); SHIP retirement and pin
  reuse verify from the STATE.md pins; the driver holds retired entries
  whose `repo:` disagrees with PLAN.md or whose sha is not a commit of
  their repo.
- **r15 guard** (no METRICS_API bump; lives in `trio-check.py` and
  `trioctl`, not the loop core): slices writing outside the mailbox repo
  and any PLAN.md `repos:` block are refused ("Repo scope (r15 guard)").

- **METRICS_API 4** (r11h fence-fix): `find_queue_block(text, key,
  errors=)` closes a fence only on a CommonMark closer (indent <= 3, same
  character, run >= the opener's, no info string), matches the top-level
  key only at column 0, and reports orphan `- id:`/`- slice:` entry
  headers outside the selected block; any `faults:` block error holds the
  integration gate (see "Fences, top-level keys, and the integration
  gate"). `trioctl` (`REQUIRED_METRICS_API = 4`) and `trio-check.py`
  (`REQUIRED_METRICS_API = 4`, checked before any call into its sibling)
  both refuse an older copy with "METRICS_API 3, this ... requires 4
  (mixed metrics/ versions)"; trio-check exits 2 with that message on
  stderr instead of a TypeError.
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

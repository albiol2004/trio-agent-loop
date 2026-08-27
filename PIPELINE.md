# PIPELINE.md — speculative pipelined execution for agent workflows

Status: **implemented** — Phases 1–3 of speculative pipelined execution (open-loop, slice lifecycle tracking, automated concurrent drivers). Captures the execution
semantics that sit underneath the trio loop protocol (see MAILBOX-SCHEMA.md)
and the orchestration policy (see portable/ORCHESTRATION.md). Nothing here
changes the existing protocol; it defines the mode the protocol grows into.

## Thesis

Multi-agent orchestration today is barrier-synchronized: plan → build →
verify, each stage waiting for the last. CPUs solved this problem decades
ago. A work pipeline can dispatch slice N+1 as soon as its *inputs are
frozen*, while slice N is still building or verifying — with hardware's
three classic mechanisms adapted to agents:

- **Static scheduling at compile time** (the VLIW move): dependency
  analysis happens once, at plan time, where the global view is cheapest
  and most accurate. The pipeline trusts the plan's declared parallelism
  instead of detecting hazards at runtime.
- **Speculation with precise exceptions**: downstream work proceeds against
  frozen contracts before upstream work is verified. Faults flush exactly
  the speculated descendants, from a clean per-slice commit point.
- **Tiered hazard machinery**: mechanical interlocks (scripts, zero
  tokens), then a light-model watcher with mid-flight steering, then the
  expensive Evaluator only at retirement.

## Why the analogy only goes halfway — and what that forces

Hazard detection in silicon is exact because the ISA makes dependencies
finite and explicit (32 registers, fixed encoding). Agent slices have fuzzy,
emergent dependencies: a consumer can break on a producer's change that
compiles cleanly (a renamed column, a shifted convention). The dependency
surface is the whole shared world, not a register file.

Consequence: the pipeline cannot *detect* hazards reliably. It must make
them undetectable-by-construction — slices declare their read/write sets,
and consumers may only dispatch against **frozen interfaces**, never
in-flight implementations. The plan carries the burden the ISA carries in
hardware.

A second gap: semantic merge conflicts — two slices legally modify the same
function in individually-reasonable but mutually incoherent ways. Git merges
clean; behavior breaks. No silicon equivalent exists. This is why the
Evaluator sits at retirement and cannot be pipelined away: it is the only
stage that sees the composed system.

## Concepts

**Slice** — the unit of work flowing through the pipeline. Declared in
PLAN.md with machine-readable fields:

```yaml
- id: provider-config
  repo: .                         # target repo relative to mailbox; default .
  writes: [omp/smoke-test.sh, "api:ProviderConfig"]
  reads:  []                      # dispatchable immediately
  gate: false
- id: omp-cursor-models
  repo: .
  writes: [omp/configure-models.sh]
  reads:  ["api:ProviderConfig"]  # dispatchable once that interface freezes
  gate: false
```

**Frozen interface** — an API signature, schema, or file contract marked
frozen at a specific commit. Consumers dispatch against it; producers may
not change it without a plan revision (a fault-class event).

**Stage** — the role a slice currently occupies: plan → build → verify →
retire. Roles are pipeline stages, not barriers.

**Retirement** — in-order merge to main. A slice retires only after the
Evaluator passes it *as composed with everything retired before it*. In the
current loop implementation, retirement IS the Evaluator's SHIP commit of the
hash-pinned tree it verified (product `slice(<id>): …` + mailbox
`loop: iteration N — SHIP`); the worktree-per-slice and merge-queue semantics
above remain the future pipeline mode.

## The machinery

### Compile time: the orchestrator as compiler

The main session (with the Lead) compiles intent into a static schedule:
slices, `writes:`/`reads:`, freeze points, declared parallelism. Prose
hints are for semantics; the fields are what the interlocks check against.
A dependency the plan misses becomes a runtime fault — the "cache miss"
the interlocks exist for.

### Issue: dispatch on frozen reads

A slice enters build when every entry in `reads:` is frozen. Plain
dependency, no barrier on verification.

**Freeze governance**: the Lead freezes. It already reviews every builder
increment, so freezing adds no machinery. The watcher is the wrong tier to
hold governance authority; the Evaluator stays out of the dispatch path
(it would re-serialize the pipeline). Freeze = Lead verifies the
interface-only commit matches the declared contract, then appends
`frozen: <interface> @<sha>` to STATE.md. The watcher may later check
conformance mechanically; authority stays with the Lead.

### Execution: worktrees as register renaming

Each slice builds in its own git worktree — writes are private until
retirement, so WAW/WAR hazards become merge decisions at the reorder
buffer (the merge queue), not corruption in a shared tree.

**Mailbox placement standard**: `loop/` lives in the orchestrator
session's cwd (the coordination repo) — always singular, never inside a
worktree. Multi-repo projects (a main repo referencing frontend/backend
siblings) are handled by the slice schema: each slice declares `repo:` —
the repo it writes to, defaulting to the coordination repo. Worktrees are
created as siblings of the *target* repo (`<repo>.worktrees/<slice-id>/`),
and the driver passes absolute paths to roles.

### Runtime hazards: three tiers

| Tier | What | Cost | Trigger |
|---|---|---|---|
| Interlock | Script: did this commit touch paths outside `writes:`? Does the delta intersect an in-flight slice's `reads:`? | zero tokens | every slice commit |
| Watcher | Light model (triocl light tier): does the delta conform to frozen contracts it touches? Steers dependents via hub/session message | cheap | interlock hit |
| Evaluator | Strong model: verifies composed system at retirement | expensive | retirement only |

### Steering vs flushing

Two fault classes, two handlers:

- **Direction-level** ("the API changed, use X not Y") → the watcher
  *steers* the running dependent agent mid-flight (omp `hub send`,
  Omnigent `sys_session_send`). Cheap, no flush.
- **Precision-level** (exact edits, accumulated wrong state) → flush the
  faulted slice and its speculated descendants; the repair handler
  (`VERDICT: ITERATE scope=local:<paths>`) re-issues from the last clean
  commit. Per-slice commits keep exceptions precise.

Steering authority is gated: steer only on mechanically-verified or
high-confidence contract violations. A false-positive steer is itself an
injected fault; everything uncertain becomes a flagged note queued for the
Evaluator instead.

### The predictor: adaptive pipeline depth

Silicon predicts branches; we predict verdicts. Track the rolling SHIP
rate (per project, per slice type):

- High SHIP rate → deepen the pipeline (more slices in flight).
- Two consecutive ITERATEs → drain to depth 1 (synchronous gating) until
  the next SHIP. This rule already ships in the orchestration block.

Foundation slices (data model, public API, auth) are declared
`gate: true` at plan time and never speculate regardless of predictor
state — the rework cost asymmetry demands it.

## Relationship to graphs and workflows

Compatible — they are different axes:

- **The graph is structure**: static dependencies between work units — what
  the compiler emits. Nodes = work, edges = contracts.
- **The pipeline is dynamics**: how execution flows through that structure —
  dispatch, speculation, hazards, retirement. A CPU's netlist vs its issue
  logic.

Parallel graph edges are superscalar issue width. Joins are retirement
barriers bounded by the slowest input. The one friction point: graphs allow
feedback edges (loop iterations) while pipelines flow forward. Resolution:
an iteration is a **pipeline refill** — the fault handler's output becomes
the plan's new input and the pipeline restarts from the affected stage.
NEEDS_HUMAN is a refill whose input arrives from outside the machine.

## What already exists vs what is new

Already shipped (protocol v2): async evaluator (speculation), scoped
verdicts (fault descriptors), repair role + cap (handler + tripwire),
per-iteration commits (precise state), NEEDS_HUMAN (external refill),
verification standards (retirement criteria), steering channels (hub,
session_send). Also shipped in shadow mode: machine-readable slice
contracts in PLAN.md plus the shadow checker measuring declared-vs-actual
writes — informational only, nothing gates on it yet.

**Promoted to active:** the slice commit-presence check. `trio-shadow.py
--require-commits` now gates Evaluator dispatch (post-Lead, pre-Evaluator;
retry the Lead once, then `status: error`). Shadow measurement continues
for undeclared-write drift; commit presence is now enforced.

New here: enforcement of the declared-write (path-set) slice contracts, the
watcher role bound to the light model tier, worktree-per-slice renaming,
the in-order merge queue, the rolling-SHIP predictor, `gate: true` slice
declarations.

**Open-loop extension (v1, shipped):** QUEUE.md with per-slice retired/faults queues, optional `accepts:` field in slices, per-slice VERDICT.md sections, and asynchronous Lead/Evaluator coordination via fault flow-back (`metrics/trio-shadow.py --require-commits --slice <id>` gates per-slice; `metrics/trio-check.py` validates; `loop-open-loop/RUNBOOK.md` is the operational guide). Phase 2 shipped: slice lifecycle in metrics (planned/building/retired/faulted/repairing/shipped), /api/loop gains mode/queue/slices fields, dashboard Slices section + Timeline slice summaries, inbox queue_fault and slice_overlap items. The sha tension resolved by "retired at sha" semantics: each entry means "retired at this sha", not "the slice's last commit", allowing post-retirement fixes to append new retired entries. Phase 3 shipped: automated two-loop driver in `metrics/trio_loop.py`, `driver.sh` pass-through, orchestrator prompts, and dashboard running sub-state tracking; the open-loop extension is now complete end-to-end. both fixed in `cd80d7a`; first real-harness run (Omnigent, Cursor Grok/Luna) shipped end to end. New follow-ups: (a) `OmnigentRunner.run` ignores driver's open-loop context; (b) `trioctl` requires `TRIO_OMNIGENT_RUNNER_ID` with multiple runners.

### Measured: declared-write drift (2026-08-27)

`metrics/trio-shadow.py --report-drift` aggregates the existing per-slice
declared-vs-actual analysis across every mailbox in this repo (14 of the
19 `loop*/` dirs have a parsable PLAN.md `slices:` block; the rest predate
the machine-readable format and are skipped silently). Real output:

```
mailbox                                     slices  w/commit  w/undeclared  undeclared  declared-untouched  hazards
------------------------------------------  ------  --------  ------------  ----------  ------------------  -------
loop                                        4       4         0             0           0                   0
loop-archive-2026-08-23-dashboard-registry  5       5         1             1           0                   0
loop-archive-2026-08-23-dashboard-topology  5       5         0             0           0                   0
loop-archive-2026-08-24-agent-authoring     5       5         2             2           0                   0
loop-archive-2026-08-24-loop-driver         5       5         1             6           0                   0
loop-archive-2026-08-25-all-harness         6       6         0             0           0                   0
loop-archive-2026-08-25-control-center      5       5         0             0           0                   0
loop-iteration-model                        2       2         1             2           0                   0
loop-omnigent-v010                          2       2         1             1           1                   0
loop-open-loop                              3       3         0             0           1                   0
loop-open-loop-drivers                      6       6         0             0           2                   0
loop-openloop-smoke                         3       3         0             0           0                   0
loop-openloop-smoke2                        3       3         0             0           0                   0
loop-slice-lifecycle                        5       5         1             2           2                   0

Totals: 59 slice(s), 59 with >=1 commit (100.0%), 7 with undeclared touches
(11.9%), 14 undeclared touch(es) total, 6 with declared-but-untouched paths
(10.2%), 0 pairwise hazard(s)

Top undeclared paths: omnigent/entrypoints/trio-omnigent/prompts/lead.md
(2x); then 12 singletons — registry/tests/test_serve_pages.py,
dashboard/skills.html, metrics/tests/test_trio_loop.py,
metrics/trio_loop.py, omnigent/broker_http.py,
omnigent/entrypoints/trio-omnigent/prompts/evaluator.md,
omnigent/tests/test_omnigent_loop.py, metrics/tests/test_trailing_fields.py,
metrics/trio-shadow.py, SETUP-BY-OMNIGENT.md,
.codex/agents/trio-evaluator.toml, .codex/agents/trio-lead.toml.
```

Interpretation: drift is small and concentrated in exactly the path
classes the task expected a Lead to under-declare — generated prompt
flavors (`.md`/`.toml` role prompts, hit twice), the tests that accompany
a code change, and the tool's own module when a slice about the tool edits
both the hyphenated CLI and the file that mirrors it (`trio_loop.py`
alongside `trio-shadow.py`). No single path repeats more than twice, so
this reads as "Leads forget the paired file" rather than one chronically
mis-scoped file. **Zero pairwise hazards** turned up across all 59 slices
in 14 mailboxes: no two same-iteration slices with declared-disjoint
`writes:` ever actually collided on a real file historically. That is
weak evidence *for* a static scoreboard (declared disjointness has so far
never been wrong when it mattered for concurrency) but the sample is
small and skewed toward single-slice-per-iteration mailboxes; it does not
yet cover a mailbox with several genuinely parallel same-iteration
slices, which is the actual case the scoreboard needs to be safe for.

## Cost model

| Component | Model tier | When | Drives |
|---|---|---|---|
| Static schedule | strong (main session) | once per plan | everything |
| Interlocks | script | per commit | most hazards |
| Watcher | light | per interlock hit | steers |
| Evaluator | strong | per retirement | final truth |
| Repair | builder tier | per fault | recovery |

The design's economic claim: expensive tokens concentrate at the two
points with irreducible global view — planning and retirement — and
everything between runs on scripts and light models.

## Open questions

1. Symbol-level read/write sets (not just paths) — worth it, or are paths +
   frozen-interface names sufficient resolution?
2. Watcher confidence calibration — what false-steer rate is tolerable,
   and how is it measured? (Candidate: log every steer, let the Evaluator
   grade it at retirement.)
3. Semantic merge conflicts at retirement — is an Evaluator pass enough,
   or do slices need a declared "semantic surface" beyond paths?
4. Steering mid-thought reliability across harnesses — checkpointed
   steering (applied at the next tool boundary) vs immediate injection.

## First buildable increment

1. `writes:`/`reads:`/`gate:` fields in PLAN.md slices (schema addition to
   MAILBOX-SCHEMA.md; canonical prompt updates via prompts/canonical).
2. The interlock script (path-set checks on slice commits; no model).
3. Worktree-per-slice in the omp and Omnigent flavors.
4. The watcher role (light tier) with gated steer authority.
5. Predictor v0: the existing two-ITERATE drain rule, logged per project.

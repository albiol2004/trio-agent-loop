# PIPELINE.md — speculative pipelined execution for agent workflows

Status: **experimental spec** — not yet implemented. Captures the execution
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
Evaluator passes it *as composed with everything retired before it*.

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

New here: interlock enforcement of the slice contracts, the watcher role
bound to the light model tier, worktree-per-slice renaming, the in-order
merge queue, the rolling-SHIP predictor, `gate: true` slice declarations.

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

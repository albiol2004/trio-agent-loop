# ORCHESTRATION.md — cross-harness workstyle policy

Canonical source for the user-wide orchestration policy: how a primary
("main session") agent routes work to subagents, how Lead/Evaluator loops
run asynchronously, and when background documentation tasks fire. It is
harness-neutral on purpose — Claude Code, Codex, Kimi, omp, Omnigent, and
generic agentic CLIs all consume the same text.

## How distribution works

Only the **marked block** below is distributed. Setup copies it verbatim —
between `<!-- orchestration:start -->` and `<!-- orchestration:end -->` —
into each harness's *existing* user-global instruction file, replacing any
previous copy of the same block (idempotent, no separate file, no name
collisions):

| Harness | Injection target |
|---|---|
| Claude Code (`--global`) | `~/.claude/CLAUDE.md` |
| Codex (`--codex`) | `~/.codex/AGENTS.md` |
| Gemini CLI | `~/.gemini/GEMINI.md` |
| Kimi (`--kimi`) | `~/.kimi-code/AGENTS.md` (chosen convention; harmless if Kimi ignores it) |
| omp (`--omp`) | `~/.omp/agent/AGENTS.md` (omp native user-global context file) |
| OpenCode (`--opencode`) | `~/.config/opencode/AGENTS.md` |
| Omnigent (`--omnigent`) | no separate file — the coordinator is a Claude/Codex session and inherits the block from those harnesses' files |
| generic CLI | that harness's user-level context file |

Everything outside the markers is maintainer documentation and stays in
this repo.

## Design rules (for maintainers — not distributed)

1. **Keep the block short.** It loads into *every* session of *every*
   harness — a permanent per-prompt tax. Decision tables and defaults only;
   playbooks and loop protocols stay in the repo and load on trigger.
2. **Capability vocabulary, never agent names.** Harnesses name their
   subagent types differently (omp: scout/sonic/task; Claude Code:
   Explore/general-purpose; Omnigent: YAML-defined agents). The block says
   SCOUT/BUILDER/LEAD/EVALUATOR; each harness's setup maps the vocabulary
   to its concrete agent types. This is the same trick the trio loop uses
   for role→model mapping.
3. **Prose is a nudge, not a constraint.** Harnesses weight global
   instructions differently. What makes the loops reliable is structure
   (fresh context per role, mailbox files, machine-parsed verdicts), so put
   enforcement in structure where possible and treat this block as a strong
   convention.
4. **Model-agnostic.** The policy names roles, never models. Model
   assignments live in `trioctl` profiles (`omnigent/trioctl*.toml`), so
   swapping providers never touches the policy.

5. **Doc tasks are layered, not configured.** The block owns the trigger
   (after verified/merged change, one coalesced background task); the
   project's context file owns the protocol (system location, format,
   coalescing job name such as a single named `DocSync` background task);
   the spawn prompt carries the diff summary and rationale. Projects without
   a documentation system define nothing and degrade gracefully; projects
   override by layering, never by editing the global block. Injection is
   marker-scoped, so personal notes outside the markers in user files
   survive re-installs.

## The policy (maintainer notes)

The block encodes five decisions:

- **Main session routes, rarely implements.** It builds only trivial
  one-breath edits; everything else goes to a subagent matched by the
  routing table. It never debugs test-suite failures — workers run suites
  and return compressed verdicts, keeping bulky output out of the main
  context. Rationale: the main session is usually the most expensive model
  in the system.
- **Evaluator is async, not a gate.** The Lead pipelines ahead on dependent
  work while the Evaluator verifies a *pinned commit* behind it. This is
  speculative execution, and it only works with a rollback story: scoped
  verdicts (the Evaluator names the files/symbols that failed), per-iteration
  commits (so "work built on top" has explicit parents), and a tripwire
  (two consecutive ITERATEs → fall back to synchronous gating until a SHIP).
  Foundation changes (data model, public API, auth) may still be declared
  synchronous gates at plan time; leaf changes pipeline.
- **Documentation is a background task, triggered by verification.** After
  a SHIP or merge, exactly one coalesced background doc task (cheap model)
  updates the project's documentation/memory system from the change summary
  and rationale. Never document unverified decisions; coalesce bursts so
  five rapid changes don't queue five conflicting writers.
- **The routing table is the whole router.** Intake classification —
  question → SCOUT, slice → BUILDER, fuzzy/multi-slice → LEAD+EVALUATOR
  loop, trivial → self — is the complete decision procedure.
- **Trio loops run end-to-end by default.** No per-iteration checkpoints:
  any run/start/continue request chains LEAD → commit gate → EVALUATOR →
  verdict dispatch until a terminal verdict. The commit gate is the one
  mechanical interlock that is active, not speculative: slice-commit
  presence is enforced before Evaluator dispatch.

## The distributable block

<!-- orchestration:start -->
## Orchestration policy (user-wide)

You are the session router. Optimize for delegation, not direct work.

### Role vocabulary (capabilities — map to this harness's subagent types)
- SCOUT: read-only investigation returning compressed findings. Never edits.
- BUILDER: implements one well-specified slice against clear acceptance.
- LEAD: owns a workstream end to end — plans, delegates to builders, reviews.
- EVALUATOR: adversarial verifier; exercises the deliverable independently.

### Routing
- Exploratory question or unknown surface → SCOUT.
- Well-specified slice with clear acceptance → BUILDER, directly.
- Fuzzy criteria, multiple slices, or adversarial verification needed →
  LEAD+EVALUATOR loop.
- Trivial mechanical edit, verifiable in one breath → do it yourself.

### Defaults
- Never build features or debug test-suite failures in the main session.
  Delegate with full context; workers run suites and report compressed
  verdicts.
- Prefer background, parallel subagents over sequential waiting.
- Evaluators run async against a pinned commit, never a moving tree; every
  ITERATE verdict names the failure scope (files/symbols) so work built on
  top can be conflict-checked cheaply. Two consecutive ITERATEs → gate
  synchronously until the next SHIP.
- After a verified/merged change, queue exactly one coalesced background
  documentation task (cheap model) with the change summary and rationale.
  Never document unverified decisions.

### Trio loops — end-to-end by default
- When the user asks to run/start/continue a trio loop (any phrasing), run
  the full chain with no per-iteration checkpoints: LEAD → commit gate →
  EVALUATOR → verdict dispatch (ITERATE → next iteration; scoped ITERATE →
  repair path; SHIP/BLOCKED/NEEDS_HUMAN → stop and surface). A bare
  supervised-step command remains available for deliberate step-through.
- Before dispatching the EVALUATOR, run the commit gate
  (`trio-shadow.py --require-commits`); on failure retry the LEAD once,
  then stop with `status: error`.
- A SHIP verdict includes the EVALUATOR's retirement commit of the verified
  tree (product `slice(<id>): …` + mailbox `loop: iteration N — SHIP`);
  `/trio-ship` recovers orphaned SHIP states.
<!-- orchestration:end -->

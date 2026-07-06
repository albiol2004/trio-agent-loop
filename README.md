# Trio agent loop — reusable Claude Code template

A Karpathy/Ralph-style agent loop for Claude Code: two equal Opus "thinker"
roles that prompt each other through markdown mailbox files, each fanning out
cheap Sonnet workers, driven autonomously by `/loop`.

```
            ┌─────────────────────────────────────────────┐
            │            /trio  (orchestrator)            │
            │   sequences roles, enforces stop conditions │
            └─────────┬───────────────────────┬───────────┘
                      ▼                       ▼
                  trio-lead              trio-evaluator
                (Opus: plan +          (Opus: adversarial
                 implement)             verify + API currency)
                   │     │                │        │
             scout(s) builder(s)      scout(s)  WebSearch
             (Sonnet) (Sonnet)        (Sonnet)  (docs/CVEs)
                      ▼                       ▼
   writes →  PLAN.md + REPORT.md  →      VERDICT.md ──┐
                      ▲                               │
                      └────── next iteration ◄────────┘
```

## Why mailbox files instead of one big conversation
- **Fresh context per role, per iteration** — no context-rot; each agent reads
  only the files that matter to its job.
- **Auditable** — `loop/LOG.md` is the flight recorder; you can `cat` the
  mailboxes at any time to see exactly what the agents told each other.
- **Steerable mid-flight** — edit `loop/GOAL.md` or drop a note in
  `loop/VERDICT.md`'s guidance section between iterations and the Lead's
  next planning phase picks it up.
- **Resumable** — state lives on disk, not in a session; kill the terminal and
  `/trio` continues where it left off.

## The roles
| Role | Model | Reads | Writes | Job |
|------|-------|-------|--------|-----|
| trio-lead | Opus | GOAL, VERDICT, STATE | PLAN.md, REPORT.md + the code | plans AND implements the smallest verifiable increment; only role that edits product code; fans out Sonnet workers |
| trio-evaluator | Opus | GOAL, PLAN, diff (REPORT last) | VERDICT.md | adversarial verification grounded in its own test runs + web checks that APIs used are current for the pinned versions; verdict SHIP / ITERATE / BLOCKED |
| trio-scout | Sonnet | repo (read-only) | — | recon questions for either lead role |
| trio-builder | Sonnet | repo | code | one well-specified mechanical task at a time (sequential on shared files) |

Separation of powers is the point: the agent that wrote the code never grades
it, and the agent that grades it is forbidden from fixing it. Merging the old
Planner into the Lead follows the evidence (planning + implementing in one
fresh context is fine); the independent grounded Evaluator is the one split
the literature says you must never remove.

## Install
```bash
./install.sh --global            # ~/.claude — every project on this machine
./install.sh ~/src/myproject     # or per-project, committed to the repo
```

## Use
```bash
cd ~/src/myproject && claude
```
```
/trio-init add rate limiting to the public API, config-driven, no new deps
/trio          # run ONE iteration supervised — sanity-check the loop first
/loop /trio    # then let it run: iterates until SHIP or BLOCKED
```
`/loop /trio` uses dynamic mode: the orchestrator reschedules itself after an
ITERATE verdict and simply stops rescheduling on SHIP/BLOCKED. Press **Esc**
to pause it yourself; `/loop 10m /trio` forces a fixed cadence instead.

## Control knobs while it runs
- `loop/GOAL.md` — edit anytime; next iteration obeys it.
- `loop/STATE.md` → `max_iterations` (default 10) — hard budget cap.
- `DECISION:` flags in PLAN.md — the Lead marks judgment calls it made;
  veto them by editing GOAL.md.
- Two identical ITERATE verdicts in a row auto-escalate to BLOCKED, so a
  stuck loop parks itself instead of burning tokens.

## What the evidence says (research-checked 2026-07-05)
Design was audited against practitioner sources (Karpathy, Boris Cherny,
Steinberger, Huntley's Ralph loop, Anthropic's engineering blog) and the
actor-critic literature (Reflexion, self-preference-bias papers):
- **Validated**: fresh-context grader separate from the worker; files as
  durable memory; sequential pipeline; iteration caps + BLOCKED escalation.
- **Hardened in response**: Evaluator forms its verdict from the diff and its
  own test runs *before* reading REPORT.md (same-model judges over-trust
  confident reports); mandatory test-integrity audit (reward-hacking);
  failure memory in STATE.md (oscillation); plateau check from iteration 4
  (gains flatten after ~3); ITERATE only on blocking issues (nitpick loops).
- **Known cost**: multi-agent ≈ 15× the tokens of a plain chat session
  (Anthropic's own number). The duo (Lead+Evaluator) is already the
  cost-optimized shape: merging Planner→Lead is fine per the literature;
  merging the Evaluator away is the one empirically bad move.

## Tuning
- **Fable while it lasts**: change `model: opus` → `model: fable` in
  `trio-lead.md` and/or `trio-evaluator.md` — both roles are judgment-heavy;
  keep workers on Sonnet. Do NOT downgrade the Evaluator below the Lead's
  tier: a weak critic measurably hurts (91.4%→82.8% in one study).
- Big refactors: add `isolation: worktree` to trio-lead's frontmatter
  so iterations can't wreck your working tree.
- Cost reality check: one iteration = 2 Opus agents + N Sonnet workers.
  Budget roughly like two senior code reviews per iteration. `max_iterations`
  is your circuit breaker — keep it low until you trust a given GOAL.md.

## Other harnesses (Codex, Cursor, Z.ai, anything with a headless mode)
The role prompts and mailbox protocol are harness-agnostic; `portable/`
carries them as standalone files plus a Ralph-style bash driver:
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it
HARNESS=codex ./portable/driver.sh 10   # or cursor|opencode|pi|hermes|athen|gemini|agy|claude|generic
```
Per-harness setup docs: `portable/SETUP-codex.md`, `SETUP-cursor.md`,
`SETUP-opencode.md`, `SETUP-pi.md`, `SETUP-hermes.md`, `SETUP-athen.md`,
`SETUP-antigravity.md` (Antigravity IDE isn't scriptable; its `agy` CLI /
Gemini CLI are), `SETUP-zai.md` (Z.ai's ZCode is a GUI — not scriptable; the GLM Coding Plan
endpoint runs the NATIVE template via Claude Code env vars instead),
`SETUP-generic.md`. The driver
parses only VERDICT.md's first line; exit codes 0=SHIP, 2=BLOCKED, 3=bad
verdict, 4=iteration cap. Fresh process per role per iteration — the loop's
fresh-context property survives every harness; what you lose outside Claude
Code is the Sonnet worker fan-out (single agent per invocation).

## Files
```
.claude/agents/trio-{lead,evaluator,scout,builder}.md
.claude/skills/trio/SKILL.md         # /trio  — one full iteration
.claude/skills/trio-init/SKILL.md    # /trio-init — mailbox setup
loop/                                # created by /trio-init, per project
  GOAL.md STATE.md PLAN.md REPORT.md VERDICT.md LOG.md
portable/                            # non-Claude-Code harnesses
  driver.sh prompts/{lead,evaluator}.md GOAL.template.md AGENTS.template.md
  SETUP-{codex,cursor,opencode,pi,hermes,athen,antigravity,zai,generic}.md
```

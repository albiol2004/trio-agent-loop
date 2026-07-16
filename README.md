# Trio agent loop

![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)
![Harnesses](https://img.shields.io/badge/harnesses-9%2B-brightgreen.svg)
![Made for Claude Code](https://img.shields.io/badge/made%20for-Claude%20Code-d97757.svg)

A research-hardened, Karpathy/Ralph-style agent loop: two equal "thinker"
roles — a **Lead** that plans and implements, and an adversarial **Evaluator**
that independently verifies — prompt each other through markdown mailbox
files, fanning out cheap worker agents, iterating autonomously until the work
ships or a human decision is needed. Claude, ZCode, and Pi use their native
orchestration surfaces. Codex prefers native custom agents and can fall back
to isolated bundled Codex sessions when an app task does not expose native
spawn controls. The shell driver remains for other headless harnesses.

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
- **Concurrent loops: one mailbox dir per loop.** Second loop in the same
  repo → `/trio-init dir=loop-<name> <goal>` then `/trio dir=loop-<name>`
  (driver: `LOOP_DIR=loop-<name>`). The driver takes an atomic `.lock` on its
  mailbox (exit 5 if another live driver owns it), and `/trio` halts if
  STATE.md's `mission:` line stops matching GOAL.md — the signature of
  another session repurposing the mailbox mid-loop.
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

## Native bundles and other harnesses
The role prompts and mailbox protocol are harness-agnostic; `portable/`
carries them as standalone files plus a Ralph-style bash driver:
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it
HARNESS=cursor ./portable/driver.sh 10   # or opencode|hermes|athen|gemini|agy|claude|generic
```
Per-harness setup docs: `portable/SETUP-codex.md`, `SETUP-cursor.md`,
`SETUP-opencode.md`, `SETUP-pi.md`, `SETUP-hermes.md`, `SETUP-athen.md`,
`SETUP-antigravity.md` (Antigravity IDE isn't scriptable; its `agy` CLI /
Gemini CLI are), `SETUP-zai.md` (Z.ai's ZCode is a GUI — not scriptable; the GLM Coding Plan
endpoint runs the NATIVE template via Claude Code env vars instead),
`SETUP-generic.md`. The driver
parses only VERDICT.md's first line; exit codes 0=SHIP, 2=BLOCKED, 3=bad
verdict, 4=iteration cap, 5=mailbox locked by another driver
(`LOOP_DIR=loop-<name>` runs concurrent loops). Codex prefers native custom
agents and has a dedicated isolated-session fallback, ZCode uses native custom
subagents and Goal Mode, and Pi uses in-process SDK AgentSessions.

## Files
```
.claude/agents/trio-{lead,evaluator,scout,builder}.md
.claude/skills/trio/SKILL.md         # /trio  — one full iteration
.claude/skills/trio-init/SKILL.md    # /trio-init — mailbox setup
codex/                               # custom agents, skills, fallback runner
  skills/trio/references/PROJECT-CONFIG.example.toml
  skills/trio/references/TROUBLESHOOTING.md
  skills/trio/scripts/run-role.sh
  skills/trio/references/prompts/*.md
loop/                                # created by /trio-init, per project
  GOAL.md STATE.md PLAN.md REPORT.md VERDICT.md LOG.md
portable/                            # non-Claude-Code harnesses
  driver.sh prompts/{lead,evaluator}.md GOAL.template.md AGENTS.template.md
  SETUP-{codex,cursor,opencode,pi,hermes,athen,antigravity,zai,generic}.md
```

## Setting it up with an AI agent
Point an agent session at this repo and tell it to follow the setup doc for
its harness — it installs the template and explains usage:
- Claude Code: `SETUP-BY-CLAUDE.md` (native `/trio` install)
- Codex: `SETUP-BY-CODEX.md` (native-first skill + isolated fallback + `/goal`)
- ZCode: `SETUP-BY-ZCODE.md` (native skills + custom subagents + `/goal`)
- Pi: `SETUP-BY-PI.md` (native in-process AgentSession extension)
- Athen: `SETUP-BY-ATHEN.md` (portable driver + env-based model config)
Any other agent with shell access can follow `SETUP-BY-CODEX.md`'s shape using
its own harness's `portable/SETUP-*.md`.

## License
MIT — see [LICENSE](LICENSE). Issues and PRs welcome, especially new
harness recipes for `portable/`.

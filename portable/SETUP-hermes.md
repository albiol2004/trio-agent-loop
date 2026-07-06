# Setup — Hermes Agent (Nous Research)

Open-source (MIT), model-agnostic, self-hosted harness; `-z` mode is
explicitly built for scripts/CI, which is exactly what the driver needs.
Driver support: `HARNESS=hermes`.

## One-time setup
1. Install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
   (bundles its own Python/Node/ripgrep; installs a global `hermes` command;
   config at `~/.hermes/config.yaml`).
2. Provider/model: `hermes setup`, or per-invocation `-m provider/model`
   (anthropic/openrouter/nous/local endpoints), or `HERMES_INFERENCE_MODEL`.
3. Context: Hermes reads **AGENTS.md** natively (same convention as
   Codex/OpenCode) — copy `portable/AGENTS.template.md`. It also injects
   SOUL.md/MEMORY.md/USER.md persona+memory files if present; for
   reproducible loop runs consider `--ignore-rules` to run the role prompts
   pure (trade-off: you lose AGENTS.md injection too — then paste project
   context into GOAL.md instead).
4. If you let it spawn subagents unattended, set in `~/.hermes/config.yaml`:
   `delegation.subagent_auto_approve: true`.

## Run
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=hermes ./portable/driver.sh 10
# Optional: HERMES_MODEL=anthropic/claude-opus-4-8 (passed as -m)
```

The driver runs: `hermes -z "<prompt>" --yolo --quiet` (timeout-wrapped).
- `-z` = print-final-response-and-exit programmatic mode (stdin-aware too).
- `--yolo` is required unattended: without it, non-interactive runs
  **auto-DENY** dangerous approvals — the loop would half-work, silently
  skipping shell steps.

## Caveats
- Exit codes documented as "varies by command" — the driver's
  VERDICT.md-first-line posture covers this; don't build on `$?`.
- Loop guards worth knowing: `agent.max_turns` (default 60) caps a single
  invocation's agentic turns; `tool_loop_guardrails` config exists.
- Sessions (`-c`/`-r`) and worktrees (`-w`) exist; the driver deliberately
  uses neither (fresh context per role; continuity lives in loop/*.md).
  `-w` is worth adding manually for risky refactors.
- A `hermes-agent-self-evolution` companion repo exists (self-created
  skills / self-improvement loop) — unverified contents, not needed here.
- Flag details verified against the repo's CLI reference docs, but Hermes
  moves fast (v0.7.x) — sanity-check with `hermes --help` after install.

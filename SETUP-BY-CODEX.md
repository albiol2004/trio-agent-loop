# Setup instructions for a Codex session

You (OpenAI Codex CLI with shell access) have been given this repo to install
the "trio" duo agent loop on the local machine. Codex has no markdown-defined
subagents and no `/loop`, so on Codex the loop runs as an external bash driver
(`portable/driver.sh`) alternating `codex exec` invocations — one fresh
process per role per iteration, all cross-iteration state in the target
project's `loop/` directory. Your job is to stage the pieces and tell the
human how to run it. Do this:

1. From this repo's root, run:
   `./install.sh --portable`
   This copies `portable/` (driver, role prompts, templates, setup docs) to
   `~/.trio/portable/`, making the loop runnable from any project on the
   machine. Note: `~/.trio` and `~/.codex` are outside your workspace sandbox —
   request approval for those writes rather than silently failing.
2. Pin the role model tiers. Run `codex --version` first: on Codex ≥ 0.134,
   profiles are separate files (the old `[profiles.x]` tables in config.toml
   no longer load). Create these two files **only if they don't already
   exist** — never overwrite an existing profile without telling the human:

   ```toml
   # ~/.codex/lead.config.toml
   model = "gpt-5.6-terra"
   model_reasoning_effort = "high"
   ```
   ```toml
   # ~/.codex/evaluator.config.toml
   model = "gpt-5.6-terra"
   model_reasoning_effort = "high"    # judgment is the scarce resource
   ```

   The Codex model contract: Lead and Evaluator both run GPT-5.6 Terra at
   high reasoning — do NOT give the Evaluator a weaker model than the Lead
   (a weak critic measurably hurts the loop). If profiles fail to load on
   the installed version, say so and fall back to the default config
   (omitting the profile env vars below still works).
3. Verify: `~/.trio/portable/driver.sh` exists and is executable, the two
   profile files exist, and Codex is authenticated (`codex login status`,
   or `OPENAI_API_KEY` is set).
4. If the human named a target project, prepare its mailbox there:
   `mkdir -p loop && cp ~/.trio/portable/GOAL.template.md loop/GOAL.md`.
   Fill in only what the human actually told you about the goal; leave the
   rest as placeholders for them to edit — do NOT invent a mission or
   definition-of-done. If the project has no `AGENTS.md`, copy
   `~/.trio/portable/AGENTS.template.md` there and fill in the project
   section (Codex reads AGENTS.md natively).
5. Tell the user how to use it, briefly:
   - Edit `loop/GOAL.md` first — it is human-owned and the loop obeys it.
   - Run, from the project root:
     ```bash
     HARNESS=codex CODEX_LEAD_PROFILE=lead CODEX_EVAL_PROFILE=evaluator \
       ~/.trio/portable/driver.sh 10
     ```
     The argument is the iteration cap (budget circuit breaker — keep it
     low until the GOAL.md is trusted).
   - The driver exits 0 on `VERDICT: SHIP`, 2 on `VERDICT: BLOCKED`, 3 on an
     unparseable verdict, 4 at the iteration cap. State lives in `loop/` —
     safe to kill and re-run; steer mid-flight by editing `loop/GOAL.md`.
   - The loop never commits; it always ends at an uncommitted tree for
     human review.
6. Do NOT modify `portable/prompts/lead.md` or `portable/prompts/evaluator.md`
   during install — the role contract (anti-sycophancy read order, verdict
   grammar, test-integrity audit) is load-bearing. And keep the driver's
   `--sandbox workspace-write` as-is: never suggest
   `--dangerously-bypass-approvals-and-sandbox` unless the whole loop runs in
   a disposable container/CI.

Codex-specific details and caveats live in `portable/SETUP-codex.md`; design
rationale and research citations in `README.md` — read them if the user asks
why the loop is shaped this way.

# Setup — OpenAI Codex CLI

Codex has no markdown-defined subagents or /loop, so the loop runs as the
external bash driver alternating `codex exec` invocations (pure Ralph
pattern). Fresh process per role per iteration; all state in `loop/`.

## One-time setup
1. Install/auth Codex CLI (`codex login` or `OPENAI_API_KEY`).
2. Project context: Codex natively reads **AGENTS.md** (walks up from cwd to
   the `.git` root; also honors `AGENTS.override.md`). Add one describing the
   project — the role prompts themselves come from `portable/prompts/`.
3. Optional per-role models/efforts — Codex ≥0.134 profiles are **separate
   files** (the old `[profiles.x]` tables in config.toml no longer work):

   ```toml
   # ~/.codex/lead.config.toml
   model = "gpt-5.4"
   model_reasoning_effort = "medium"
   ```
   ```toml
   # ~/.codex/evaluator.config.toml
   model = "gpt-5.4"
   model_reasoning_effort = "high"    # judgment is the scarce resource
   ```
   This is version-dependent and contentious upstream (openai/codex#4849) —
   check your installed version if profiles don't load.

## Run
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=codex CODEX_LEAD_PROFILE=lead CODEX_EVAL_PROFILE=evaluator \
  ./portable/driver.sh 10
```
(Profiles optional — omit the env vars to use your default config.)

## Notes & caveats
- The driver feeds prompts via stdin (`codex exec - < prompts/lead.md`) and
  runs `--sandbox workspace-write`: writes allowed inside the repo, blocked
  outside. Do NOT use `--dangerously-bypass-approvals-and-sandbox` unless the
  whole thing runs in a disposable container/CI.
- `~/.codex/prompts/*.md` custom prompts exist but are deprecated in favor of
  Codex "skills" — irrelevant here anyway, since the driver passes full prompt
  files directly.
- `codex exec resume` exists if you ever want conversation continuity between
  iterations — the loop deliberately does NOT use it (fresh context is the
  design; continuity lives in loop/*.md).
- `--json` emits machine-readable events if you later want tighter driver
  integration than parsing VERDICT.md's first line.

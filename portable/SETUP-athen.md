# Setup — Athen

Athen's `athen-cli --prompt` one-shot is exactly the shape the driver needs:
stateless, runs one task unattended against a workspace dir, prints the final
answer, exits 0 on success / 1 on error-or-incomplete / 2 on usage errors.
The filesystem is the only cross-invocation state — which is the trio loop's
whole design, so the fit is natural. Driver support: `HARNESS=athen`.

## Facts the driver relies on (from the source, 2026-07)
- Binary: `athen-cli` (from the Athen repo, `target/release/athen-cli` after
  `cargo build -p athen-cli --release`). If it isn't on your PATH, point the
  driver at it with `ATHEN_BIN=/path/to/athen-cli`.
- Prompt is an **argument only** (`--prompt <str>`; no stdin/file mode) — the
  driver passes `$(cat promptfile)`.
- Working dir: the agent's file/shell tools resolve against
  `ATHEN_WORKSPACE_DIR` — the driver sets it to the project dir ($PWD).
- Full-auto: one-shot mode already skips the coordinator/risk gating, but
  shell commands still hit the rule-engine risk check, which **fails closed**
  headless — the driver sets `ATHEN_DISABLE_RISK_GATE=1`.
- Model comes from env (models.toml is daemon-only): you must export
  `ATHEN_BASE_URL` + `ATHEN_MODEL` (+ `ATHEN_API_KEY`, optional
  `ATHEN_FAMILY`, `ATHEN_TEMPERATURE`) or the CLI exits 2.
- `--max-steps`/`ATHEN_MAX_STEPS` are advertised but currently inert — bound
  runtime with `ATHEN_TASK_TIMEOUT_SECS` (default 1800s) instead.
- No AGENTS.md/CLAUDE.md ingestion — project context must live in the role
  prompts and `loop/` files (it already does). Athen's Identity/Skills/Memory
  are DB-backed and empty for fresh one-shots.

## Run
```bash
export ATHEN_BASE_URL="https://api.deepseek.com/v1"   # any OpenAI-compatible
export ATHEN_MODEL="deepseek-v4-flash"
export ATHEN_API_KEY="..."
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=athen ./portable/driver.sh 10
# Optional: ATHEN_BIN=/path/to/athen-cli
#           ATHEN_LEAD_PROFILE=coder ATHEN_EVAL_PROFILE=researcher
#           (built-in profiles incl. assistant, coder, devops, researcher…)
```
Per-role personas via `--profile` are optional — the role prompts carry the
real role definitions; profiles just add flavor underneath.

## Why wake-ups don't replace the loop (and what they could do)
Athen wake-ups are daemon-only (`athen-app --headless`): clock-triggered
sense events (OneShot/Cron/Interval) through the coordinator, with autonomy
bands — pre-approved *capability*, not conditional "re-run until X". The CLI
one-shot never constructs the scheduler, and v1 wake-ups can't gate on a
condition like VERDICT.md's first line. So: bash driver for the loop.
Future Athen feature idea if you want native looping: a wake-up-style
`FollowUp` trigger that fires when the previous task exits, with a predicate
on a file's first line — that would be Athen's equivalent of Claude Code's
/loop and Cursor's stop-hook `followup_message` (see SETUP-cursor.md).

## Caveats
- Each invocation pays fresh provider init (no warm process) — fine at this
  cadence.
- All model tiers collapse to the single `ATHEN_MODEL` in one-shot mode, so
  Lead and Evaluator run the same model: the anti-sycophancy protocol in the
  evaluator prompt is load-bearing here (same situation as GLM — see
  SETUP-zai.md).
- Toolbox side effects persist in `~/.athen/toolbox` regardless of workspace.

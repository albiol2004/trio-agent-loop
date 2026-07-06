# Setup — Cursor (verified against official docs, 2026-07)

Cursor's CLI works with the portable driver, and Cursor even has a native
loop primitive via hooks (below). Binary: official docs now call it `agent`;
installers historically shipped `cursor-agent` — the driver uses
`${CURSOR_BIN:-cursor-agent}`, override if yours is `agent`.

## One-time setup
1. Install the Cursor CLI. For scripting, auth via `CURSOR_API_KEY` env var
   (or `--api-key`) — no interactive login.
2. Project context (`cursor.com/docs/context/rules`):
   - **AGENTS.md** at project root, plain markdown — supported natively, and
     **nested** AGENTS.md in subdirs are merged (more specific wins). Copy
     `portable/AGENTS.template.md`.
   - Or native `.cursor/rules/*.mdc` (YAML frontmatter: `description`,
     `globs`, `alwaysApply`; plain `.md` in that dir is IGNORED).
   - Precedence between AGENTS.md and `.mdc` rules when both exist:
     confirmed undocumented — don't rely on it; pick one.
   - Legacy `.cursorrules`: dropped from current docs; treat as dead.

## Run (portable driver)
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=cursor ./portable/driver.sh 10
# CURSOR_BIN=agent HARNESS=cursor ./portable/driver.sh 10   # newer installs
```

**The critical flag:** headless is `-p/--print`, and WITHOUT `-f/--force`
(or `--yolo`) it only *proposes* edits — the loop would silently write
nothing. Other scripting flags (all verified): `--model <m>`, `--output-format
json|text|stream-json`, `--stream-partial-output`, `--mode agent|plan|ask`,
`--workspace <path>`, `--sandbox <mode>` (plus a `sandbox run` subcommand with
`--allow-paths`/`--readonly-paths`/`--network`), `--continue` (latest) /
`--resume <chatId>`, `-w/--worktree [name]` for isolated checkouts.

## Native alternative: hook-driven loop (no bash driver)
Cursor hooks (`cursor.com/docs/hooks`, config in `.cursor/hooks.json`) include
a **`stop` hook whose JSON response can return `followup_message`**, which
auto-triggers the next turn, gated by `loop_limit` (default 5, `null` =
unlimited). A stop-hook script that reads `loop/VERDICT.md` and returns the
next role prompt as `followup_message` (empty on SHIP/BLOCKED) reproduces the
loop natively — same trick as Anthropic's ralph-wiggum plugin. Caveat: this
runs Lead and Evaluator in ONE session (weaker context isolation than the
driver's fresh process per role); headless hook coverage is also partial
(docs: cloud/headless agents run command-based hooks only). The driver remains
the recommended path; the hook loop is there if you want to stay inside the
IDE.

## Subagents (informational)
`.cursor/agents/*.md` (project) / `~/.cursor/agents/` (user); frontmatter is
exactly: `name`, `description`, `model` (default `inherit`), `readonly`
(bool), `is_background` (bool) — no tools/color fields. Invocation is
interactive-only (`/name ...` slash or natural language); **no CLI flag exists
to invoke a named subagent headlessly** (confirmed absent from the documented
flag list) — which is why the portable driver passes full role prompts
instead. Background subagents persist state under `~/.cursor/subagents/`.
Custom `.cursor/commands/*.md` remain officially undocumented; Cursor is
steering toward Skills (`.cursor/skills/`).

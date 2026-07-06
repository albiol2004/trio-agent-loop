# AGENTS.md — project context for the trio loop (portable harnesses)

<!-- Copy to your project root and fill in. Codex, Cursor, Copilot, Aider,
     Windsurf, Zed etc. read AGENTS.md natively; for Claude Code use CLAUDE.md,
     for Gemini CLI use GEMINI.md (same content). -->

## Project
<What this codebase is, how to build/test/lint it — exact commands.>

## Agent-loop protocol (do not remove)
This repo may be worked on by an automated Lead→Evaluator loop whose state
lives in `loop/`:
- `loop/GOAL.md` is human-owned and immutable to agents.
- `loop/PLAN.md`, `loop/REPORT.md` are written by the Lead role;
  `loop/VERDICT.md` by the Evaluator role (first line is machine-parsed:
  `VERDICT: SHIP|ITERATE|BLOCKED`); `loop/STATE.md` and `loop/LOG.md` are
  shared bookkeeping.
- If you are invoked with one of the role prompts from `portable/prompts/`,
  follow that prompt exactly. If you are a human-driven session, don't edit
  `loop/` files casually — the loop depends on them.
- Never commit; the loop always ends at an uncommitted tree for human review.

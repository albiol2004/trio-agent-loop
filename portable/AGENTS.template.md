# AGENTS.md — project context for the trio loop (portable harnesses)

<!-- Copy to your project root and fill in. Codex, Cursor, Copilot, Aider,
     Windsurf, Zed etc. read AGENTS.md natively; for Claude Code use CLAUDE.md,
     for Gemini CLI use GEMINI.md (same content). -->

## Project
<What this codebase is, how to build/test/lint it — exact commands.>

## Agent-loop protocol (do not remove)
This repo may be worked on by an automated Lead→Evaluator loop whose state
lives in a mailbox directory — `loop/` by default, `loop-*/` when several
loops run concurrently (one mailbox per loop; never share or repurpose one
that another session may own):
- `loop/GOAL.md` is human-owned and immutable to agents.
- `loop/PLAN.md`, `loop/REPORT.md` are written by the Lead role;
  `loop/VERDICT.md` by the Evaluator role (first line is machine-parsed:
  `VERDICT: SHIP|ITERATE|NEEDS_HUMAN|BLOCKED`, with an optional
  `scope=design` or `scope=local:<paths>` suffix on ITERATE); `loop/STATE.md`
  and `loop/LOG.md` are shared bookkeeping; `loop/.repairs` is a
  driver-internal counter.
- If you are invoked with one of the role prompts from `portable/prompts/`,
  follow that prompt exactly. If you are a human-driven session, don't edit
  `loop/` files casually — the loop depends on them.
- Never commit; the loop always ends at an uncommitted tree for human review.

## Verification defaults (optional)

Copy this heading into the project AGENTS.md (or CLAUDE.md / GEMINI.md)
when the repo has standing checks. Keep it Markdown — no new parser,
database, or `knowledge.yaml` fields.

- Default commands the Lead should name in PLAN evidence (build/test/lint).
- When a UI or data row is expected vs a tiny low-impact change.
- These defaults cannot waive GOAL.md acceptance or mandatory Trio
  checks. The current GOAL supersedes this section.

# Plan — concrete workflow slices

## Verification standard

Mode: `test-first`. Evidence: failing then passing pytest on the
touched tests, plus `python3 prompts/generate.py --check`.

## Iteration 1 — current increment

Objective: ship all three slices in maintained source (prompts +
controller), not lab forks.

One product commit already landed at `d4c1a24` with subject
`slice(goal-knowledge-planning):` and the other two slice ids in the
body. History is not rewritten. PLAN consolidates the overlapping
work into that one slice id so `--require-commits` matches the
actual prefix, with all three acceptance categories retained.

### Slices

```yaml
slices:
  - id: goal-knowledge-planning
    repo: ..
    writes:
      - .agents/
      - .claude/
      - .codex/
      - codex/
      - kimi/
      - metrics/
      - omnigent/
      - omp/
      - opencode/
      - pi/
      - portable/
      - prompts/
      - zcode/
    reads: []
    accepts:
      - "Effective Lead text preserves GOAL acceptance/must-preserve, traces slices vs remaining goal, and optional knowledge.yaml gather rules; generate --check passes; Omnigent _prompt contains the same shared bullets."
      - "Evaluator obligations: GOAL vs PLAN, evidence per criterion, pinned revision, unverified vs failed, tests not sole oracle, slice-pass != goal-complete. Snapshot tests prove strings reach generated native files and Omnigent _prompt()."
      - "Stale/mtime-only verdict is not ready; wrong pinned sha is not ready; timeout is not SHIP; git-visible SHIP without retirement is needs_retirement (exit 6); valid commit:/retirement and ordinary no-repo SHIP still work; no duplicate evaluator when iteration-marked verdict already exists."
    status: in_progress
    iteration: 1
```

Out of scope: replay, live install, Notion, standalone core redesign,
knowledge DB, user-wide profile changes.

## Optional knowledge

No `knowledge.yaml` in this candidate. Absent knowledge is not a
blocker. Do not invent accepted decisions.

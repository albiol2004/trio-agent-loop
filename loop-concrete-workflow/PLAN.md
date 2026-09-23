# Plan — concrete workflow slices

## Verification standard

Mode: `test-first`. Evidence: failing then passing pytest on the
touched tests, plus `python3 prompts/generate.py --check`.

## Iteration 2 — bookkeeping (committed scope)

Git history already has these commits (not rewritten). `--require-commits`
still matches the original grouped slice id for iteration 1.

| SHA | Subject |
|---|---|
| `d4c1a24` | `slice(goal-knowledge-planning):` |
| `ed4db6d` | `slice(completion-reliability-repair):` |
| `c67c31d` | `slice(completion-cli-compatibility):` |
| `a0591a8` | `slice(verdict-acceptance-binding):` |
| `71e5787` | `slice(untracked-product-guard):` |
| `cdb6926` | `slice(cli-fixture-isolation):` |
| `071028a` | `loop: iteration 1 — SHIP` |
| `b6091e8` | `loop: iteration 2 — SHIP` |

## Iteration 1 — grouped commit-gate slice

Objective: ship the three GOAL acceptance categories in maintained
source (prompts + controller), not lab forks.

One product commit landed at `d4c1a24` with subject
`slice(goal-knowledge-planning):` and the other two slice ids in the
body. Later safeguard slices are extra product commits, not a
rewrite of that prefix.

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
    status: complete
    iteration: 1
```

Out of scope: replay, live install, Notion, standalone core redesign,
knowledge DB, user-wide profile changes.

## Optional knowledge

No `knowledge.yaml` in this candidate. Absent knowledge is not a
blocker. Do not invent accepted decisions.

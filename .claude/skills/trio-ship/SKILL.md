---
name: trio-ship
description: Recover an orphaned SHIP verdict — perform the Evaluator's retirement commit (product + mailbox) when a SHIP's commit: lines are missing. No subagent, no model config.
---

A SHIP verdict normally ends with the Evaluator's retirement commit: one
product commit `slice(<id>): <summary>` plus one mailbox commit
`loop: iteration N — SHIP`, recorded as `commit:` lines in `loop/VERDICT.md`.
If a verdict says SHIP but those commits never landed (a legacy or orphaned
run), recover it yourself — the same two-commit pattern, using the verdict's
own suggested commit message. No subagent and no model config: you run the git
commands directly.

**Mailbox directory**: default `loop/`. If invoked with `dir=<path>`, that
directory is the mailbox — every `loop/` reference below means it.

1. Read `loop/VERDICT.md`. Its first non-empty line must be `VERDICT: SHIP`;
   otherwise STOP and tell the user the verdict is not machine-readable.
2. Take the suggested commit message from the verdict's
   `## Guidance for next iteration` section.
3. Check `git status`. Clean tree → there is no product commit to make;
   record `commit: <HEAD sha>` and jump to step 6.
4. Commit the product changes attributable to the loop in one commit:
   `git add <those paths>` then
   `git commit -m "slice(<id>): <summary>"` (the suggested message, shaped to
   the slice convention; other slice ids in the body). Leave unrelated
   changes uncommitted and flag them to the user — never sweep foreign paths
   in.
5. Append `commit: <product sha>` to `loop/VERDICT.md`.
6. Commit the mailbox: `git add loop/` then
   `git commit -m "loop: iteration N — SHIP"` (iteration N from STATE.md).
7. Confirm and report both shas to the user (`git rev-parse` the product
   commit and the mailbox commit).

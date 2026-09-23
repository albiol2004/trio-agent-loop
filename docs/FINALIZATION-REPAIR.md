# SHIP Finalization and Retirement Wait

## Overview

After the Evaluator records `VERDICT: SHIP`, the driver monitors for the **retirement commit** — the Evaluator's final mailbox commit (`loop: iteration N — SHIP`). A valid SHIP can reach `VERDICT.md` seconds before this commit lands (observed: verdict 16:39:12Z, commit 16:39:27Z). The driver waits within a bounded time instead of rejecting immediately.

## Configuration

Two environment variables control the wait:

| Variable | Default | Meaning |
|----------|---------|---------|
| `TRIO_RETIREMENT_WAIT_SECONDS` | 180 | Max seconds to wait; finite nonnegative only; invalid values fall back to default; capped at 3600s |
| `TRIO_RETIREMENT_POLL_SECONDS` | 3 | Poll interval; same validation rules |

Invalid values (infinity, NaN, negative, non-numeric) use the default. Setting wait to 0 checks once and rejects immediately if the retirement commit is missing.

## States and Behavior

When VERDICT: SHIP is received:

1. **Check retirement commit and product tree** via `_ship_retirement_problem()`
2. **If everything validates** → exit 0 (shipped); no log entry for immediate success
3. **If pending problem** (e.g., commit not yet ancestor of HEAD):
   - Set `status: needs_retirement`, `phase: ship-awaiting-retirement`
   - Hold loop lock and enter bounded wait
   - Poll at intervals; recheck retirement commit and product tree on each poll
   - **On resume**: fresh bounded wait starts (new deadline), not the original
4. **If final problem** (e.g., verdict retracted, product changed, stale attempt):
   - Set `phase: ship-pending-retirement`
   - Exit 6 immediately (no wait)

## Problem Classification

**Pending** — waiting may recover:
- Retirement mailbox commit not yet ancestor of HEAD
- VERDICT.md `commit:` lines reference commits not reachable from HEAD

**Final** — waiting cannot help:
- VERDICT.md first line no longer `VERDICT: SHIP`
- STATE.md `evaluator_attempt` doesn't match VERDICT.md `attempt:`
- Product tree changed: any commit/staged/modified/untracked product files since the evaluated pin
- Verdict doesn't record the evaluated pin when one was pinned

Product tree check exempts the active mailbox directory and untracked gitignored outputs. All tracked product files and changes (committed, staged, modified, untracked-but-tracked) since the evaluated pin are checked and block SHIP if present.

## Lock, Cleanup, and Logging

- Loop lock held during wait; released via signal handler/finally block
- On Ctrl+C/SIGINT/SIGTERM: signal handler releases lock; state persists. SIGKILL cannot run handlers and may leave lock stale
- Logging: `SHIP awaiting retirement` and `not accepted` logged always; `verified after Xs` logged only if wait actually occurred
- Session cleanup (Omnigent only): archives and deletes sessions created by this run after loop exits; `--keep-sessions` skips; normal runs omit flag

## Resume

From the repository root (where the mailbox lives), run: `trioctl omnigent loop --mailbox <dir>` or the native harness `/trio` command. Finalization rechecks and resumes with a fresh wait period (new deadline) if the problem is still pending.

## Recovery

**Retirement commit missing**: Verify the Evaluator completed and its commit reached the repository. After verification, use `/trio-ship` (or supported recovery workflow) if the SHIP is correct.

**Product changed since evaluation**: Human reconciliation required. The evaluated tree is no longer intact. Changed product may require fresh evaluation of the intended revision; no automatic ITERATE transition. Operator preserves unrelated work and aligns state using supported workflow.

**Verdict retracted or metadata invalid**: Inspect and resolve using supported recovery workflow (not generic git commands).

## Scope and Evidence

**Documentation only**: no code changes, no trailers added. Tested: 744 total passes (1 preexisting fixture failure), 71 focused on finalization behavior, 77 external adversarial probes. No live deployment, no default-180s real-clock qualification. Base commit (d435704) documents the feature; installation is tracked separately.



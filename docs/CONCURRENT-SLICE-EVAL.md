# Concurrent Slice-Eval — r10 Design and Speed Evidence

## 1. What it does

In open-loop mode (mailbox has `QUEUE.md`) every retired slice gets its own
slice evaluator before the integration evaluator runs. Up to r9 those
slice-evals ran strictly one after another on the driver thread.
`--slice-eval-concurrency N` grades up to N retired slices at once, each in its
own evaluator session and (with worker isolation) its own detached evaluator
worktree at the slice's pin. The integration-eval still waits for every
slice-eval.

Since r11 this is the default: `trioctl omnigent loop --mailbox <m>` runs with
N = 4 and worker isolation on. See [Defaults](#5-defaults-r11).

## 2. Design (r10 candidate 6286bdf on top of installed 3b5b93b)

### Per-dispatch session bookkeeping (df44c81)

Concurrent slice-evals share the evaluator role on one `OmnigentRunner`, so
nothing on the in-flight dispatch path reads per-role state:

- `_wait_for_role_artifact` takes the dispatch's own `session_id`; receipt
  delivery provenance is indexed by session id and popped when the dispatch
  ends; the dispatch nonce is a local.
- `session_ids[role]` stays the *last* dispatch of each role (sidecar, prune,
  tests); writes to it and to `created_session_ids` / `held_session_ids` are
  guarded by one lock.
- `inflight_sessions()` returns `{session_id: {role, kind, slice, sha}}`; the
  driver writes it to `.driver.json` as `evaluator_sessions`
  (`{"<slice>@<sha>": session|null}`).

### Bounded pool, driver-thread harvest (b19390c)

- Each polling turn submits one future per retired, ungraded, not-in-flight
  `(slice, sha)` to a bounded `ThreadPoolExecutor` (`slice-eval_*` threads).
  The per-slice commit gate still runs first, on the driver thread.
- Finished futures are harvested on the driver thread under one bookkeeping
  lock and get the same post-processing as the serial path (clobber restore,
  section check, graded / gate_blocked / attempts / pending).
- The clobber guard protects every section the driver saw on disk while that
  eval ran; VERDICT.md is read settled (two equal reads).
- An in-flight slice-eval blocks the integration-eval like a pending one; a
  done-callback wakes the driver so the integration-eval starts as soon as
  the last future lands. The integration gate also re-reads the queue and
  requires every latest retired slice to be graded (applies to N = 1 too).

### Own-heading done rule (85d0449, b48beed)

With N > 1 a sibling's append changes VERDICT.md, which alone used to satisfy
readiness. A slice-eval is ready only when VERDICT.md changed **and** it
contains this dispatch's own `## slice <slice> @<sha> — SHIP|ITERATE` heading
(sha full-or-prefix either way).

### Drain on exit (cbe1dde, 464650d, 302633d, fd80b9a, 5659e66)

- On any exit, queued futures are cancelled and running ones waited for up to
  `--slice-eval-drain-seconds` (else `TRIO_SLICE_EVAL_DRAIN_SECONDS`, else
  `min(--wait-timeout, 120)`).
- A slice-eval still running after the budget is logged as abandoned and, when
  its session id is known, gets a durable `held-<sid>.json`
  (`abandoned_on_exit`), so the next resume is refused with a truthful reason
  and cleanup keeps that session.
- Drain bookkeeping runs in a `finally`; the mailbox lock release is nested so
  it always runs, even on a second SIGINT.
- Post-loop cleanup skips only in-flight sessions of kind `slice-eval`; Lead,
  repair and integration-eval sessions are pruned as before.
- The CLI does not join abandoned pool threads: after cleanup it flushes and
  `os._exit(code)`.

### Fold restored sections into the retirement (6286bdf)

When the integration evaluator rewrites VERDICT.md whole and commits the SHIP
retirement without the slice sections, the driver's clobber restore used to
land after the commit, leaving VERDICT.md dirty so acceptance refused to delete
the builder worktrees. The driver now amends the retirement commit with
VERDICT.md only under eight safety conditions (SHIP; this iteration's
retirement commit is HEAD, single parent, mailbox-only; nothing staged; only
VERDICT.md dirty besides LOG.md/STATE.md sidecars; working VERDICT.md extends
the committed one byte-for-byte). Otherwise nothing is amended and the reason
is logged. Latent at every N.

Per-kind idle dwell (99d292c): `TRIO_OMNIGENT_IDLE_DWELL_SLICE_EVAL` overrides
`TRIO_OMNIGENT_IDLE_DWELL` (default 30 s) for slice-evals only.

## 3. Speed evidence

Source: `<lab>/speed/RESULT.md` (2026-09-26). Same 4-slice fixture, grok-4.6
medium for all roles, all modes with worker isolation; n = 1 per mode.

| mode | wall clock | to SHIP (author time) | first builder | peak builders | peak slice-evals | iter | tests |
|---|---|---|---|---|---|---|---|
| S sequential | 713.0 s | 671.6 s | 153 s | 1 | 1 | 1 | 20/20 |
| C concurrent builders | 632.7 s | 590.5 s | 125 s | 4 | 1 | 1 | 20/20 |
| C2 + concurrent slice-evals | **554.9 s** | **511.1 s** | 134 s | 4 | **4** | 1 | 21/21 |
| C2 attempt 1 (b48beed, NOT QUALIFIED: worktrees retained) | 482.3 s | — | 98 s | 4 | 4 | 1 | 22/22 |

Speedup vs S: 1.28x wall clock, 1.31x to SHIP (C was 1.13x). All three
qualified runs: SHIP first iteration, 0 ITERATE, all worktrees/branches/sessions
removed automatically, root `.cursor` restored. The slice-eval wave is ~65 s
regardless of slice count (C: ~70 s x N serial). Attempt 1 was 73 s faster
purely from model variance; treat 1.3x–1.5x as the range, not a point.

## 4. Known follow-ups (reviewer findings, non-blocking)

amend TOCTOU (use commit-tree + update-ref) · index-lock failure leaves
VERDICT staged · `saw_running` reset on re-entered wait (blocks lowering the
dwell) · restore write not tmp+rename · `worker_worktrees.create` unlocked ·
concurrent QUEUE.md `faults:` writes unguarded. Resolved in r11: N > 1 without
isolation is now refused; lockstep prints a notice instead of silently
ignoring the flag.

## 5. Defaults (r11)

| flag | not given (default) | disable / override |
|---|---|---|
| worker isolation | ON for open-loop; OFF for lockstep (no `QUEUE.md`) | `--no-isolate-workers`; `--isolate-workers` opts lockstep in |
| `--worktree-root` | `$TRIO_WORKTREE_ROOT`, else `$XDG_STATE_HOME` (or `~/.local/state`) `/trio-agent-loop/worktrees/<repo>-<sha256(git common dir)[:12]>` | `--worktree-root DIR` |
| `--slice-eval-concurrency` | 4 for open-loop; 1 for lockstep without `--isolate-workers` | `--slice-eval-concurrency 1` |
| `--slice-eval-drain-seconds` | `$TRIO_SLICE_EVAL_DRAIN_SECONDS`, else `min(--wait-timeout, 120)` | `--slice-eval-drain-seconds S` |

Fallbacks apply only to defaults; an explicit flag is never downgraded:

- Loop core predates the kwarg (e.g. a repo still vendoring 3b5b93b's
  `metrics/`): default N falls back to 1 with
  `trioctl: vendored loop core predates concurrent slice-eval; running serial (refresh metrics/ to enable)`;
  explicit N > 1 is refused. Isolation is trioctl-only and stays on.
- Isolation off (`--no-isolate-workers`, or an unmet default prerequisite):
  default N is 1; explicit N > 1 is refused.
- Lockstep mailbox (no `QUEUE.md`), plain `loop`: isolation stays off and
  N is 1, with one line
  `trioctl: lockstep mode: worker isolation stays off by default (open-loop is the fast path; pass --isolate-workers to opt in)`.
  Lockstep with isolation was never qualified live, hence the carve-out.
- Lockstep with an explicit `--isolate-workers` (isolated, default N=4) or
  an explicit `--slice-eval-concurrency N>1`:
  `trioctl: lockstep mode: slice-eval concurrency not applicable`; the
  lockstep core does not use N.

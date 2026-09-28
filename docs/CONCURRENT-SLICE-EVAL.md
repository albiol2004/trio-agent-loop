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
- A slice-eval whose detached worktree cannot be bound (r14 E-1; any
  `create()` refusal or OS error) never ends the loop: one stderr line
  `trioctl: slice-eval <slice>@<sha12>: eval_isolation: degraded (<reason>); ...`,
  the same text as a `- iter N | loop | ...` LOG.md line and as
  `eval_isolation` in that dispatch's in-flight session meta (`.driver.json`),
  then that one slice-eval runs on the non-isolated path: root workspace,
  the evaluator's own `git worktree add` at the pin, no root release/wait
  (the Lead may be live at the root), no integration fence, root `.cursor`
  baseline only. Other slice-evals stay isolated.
- Same-cwd session creation is serialised (r14 S-1). Omnigent's
  cursor-native forwarder binds a new session to the first new chat under
  `~/.cursor/chats/<md5(cwd)>/`, so two sessions launched seconds apart in
  one cwd can swap chats (observed: open-loop Lead + slice-eval at the root,
  83 ms apart). trioctl holds a per-workspace `flock` (keyed by the
  workspace realpath, under `$XDG_STATE_HOME/trio-agent-loop/cwd-locks/`,
  override `TRIO_SAME_CWD_LOCK_DIR`) from just before create until the
  broker reports the session's `external_session_id`, at most
  `TRIO_SAME_CWD_BIND_WAIT_S` (default 45) s; on timeout it logs
  `trioctl: same-cwd bind wait timed out for <session> (<workspace>); continuing`
  and continues. Covers the Lead thread, slice-eval threads and a second
  trioctl process; different workspaces (isolated worktrees) never wait. A
  contended create records `same_cwd_serialised: true` + `same_cwd_wait_s`
  in its session meta and appends to `same_cwd_serialised` in
  `.driver.json`. A first-prompt hold whose only saved user rows are an
  overlapping sibling dispatch's prompt is recorded as
  `hold`/`reason: mirror_crosswired` with `crosswired_with: <sibling session>`
  (still held, never tolerated).
- An explicit `--isolate-workers` on a detached HEAD is refused
  (`--isolate-workers refused: checkout is not on a branch (detached HEAD); ...`);
  the plain default falls back to non-isolated, serial.

## 6. Install notes (r12)

r12 edits the Lead's registered system prompt
(`omnigent/trio-omnigent-roles/lead/config.yaml`). Registered Omnigent
agents persist their bundle prompt when they are created
(SETUP-BY-OMNIGENT.md step 6). `install.sh --omnigent` copies the role
directories, but a Lead anchor that already exists keeps the old text.
r12 does not bump `REGISTRY_PROFILE`, so `doctor` will not flag the old
anchor. After installing r12, re-register the Lead:

1. Back up `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`.
2. From the template repository, create a new idle anchor with
   `sys_session_create(config_path=omnigent/trio-omnigent-roles/lead)`.
   Keep its title without the `trioctl <mailbox> ` prefix.
3. Replace `trio-omnigent-lead`'s `agent_id` and `bootstrap_conversation_id`
   in `registry.json` with the returned values. Leave `_profile`, the
   Evaluator entry and the old anchor session untouched. Never close or
   prune an anchor.
4. Run `trioctl omnigent doctor`.

If you skip this step, runs still work. Every open-loop Lead pass begins
with an OPEN-LOOP CONTEXT line that overrides the base prompt's
wave-waiting, self-verification and REPORT.md instructions. The old system
prompt then only adds noise that contradicts the procedure.
Nothing in r12 needs a builder re-registration. Isolated builders get the
TARGETED_CHECK contract from trioctl's `_ISOLATED_BUILDER_NOTE`, and trioctl
never reads `trio-omnigent-roles/builder/config.yaml`.

## 7. Choosing `full_check_budget_s` (r13)

The open-loop Lead runs one whole-tree gate per pass, under
`timeout <budget>`. The budget is 120 s unless PLAN.md's
`## Verification standard` has a `full_check_budget_s: <n>` line. Since the
proportional gate (r13 G-1..G-3) the gate's scope depends on the pass:

- **Skipped** when the pass committed no product code: the Lead reads the
  last `gate: PASS @<sha>` from LOG.md and, if
  `git diff --quiet <sha> HEAD -- . ':!<mailbox>'` exits 0, records
  `gate: skipped (no product change since <sha>)`. A fault-only pass that
  only marks a fault `stale` (vps-pool pass 2 in eval-r13b, 110 s) now
  costs nothing.
- **Integration check** by default: the typecheck/lint named in
  `full_check:` (if any) plus the union of this pass's slices'
  `## Targeted check` commands, re-run on merged HEAD. This catches
  cross-slice breakage in the touched tests (the vps-pool board-HTTP hang
  was in a retired slice's targeted check) in seconds instead of a full
  suite.
- **Full `full_check:`** only when the section has `cross_cutting: true`
  or declares `full_check_budget_s:` ≤ 60 (a suite that cheap is
  effectively free, e.g. openrouter's 11 s).

The integration eval's own full suite stays the authoritative check. A
timeout is a failure: the Lead identifies the hanging test, fixes it within
its paths and re-runs once; a second failure or timeout goes to
`## Known weaknesses` and the Evaluator gets the pass. REPORT.md
`## Whole-tree gate` lists every gate run of the pass (scope, command,
duration, summary line, PASS/FAIL/TIMEOUT), skips included.

Choosing the budget:

- Measure the command the gate will actually run on the run host: the full
  `full_check:` (including any chained typecheck or lint) when
  `cross_cutting: true` is likely, otherwise the typecheck plus the largest
  plausible union of targeted checks. When in doubt, measure the full one.
- Set `full_check_budget_s` to at least 1.3× that time, rounded up. The gate
  runs while slice-evals are still grading, so the box is loaded.
- Add the line whenever 1.3× the measured time exceeds 120 s. Also add it when
  the measured time comes within about 40 s of 120 s, because load and suite
  growth eat that margin.
- Declaring `full_check_budget_s:` ≤ 60 opts the gate into the full
  `full_check:` on every changed pass; do that only for a suite that really
  finishes well inside it.

Examples: a 220 s suite needs about 300 (1.3 × 220 = 286) when it runs
cross-cutting. An 84 s suite is under 120 s by the 1.3× rule (109 s), but it
leaves only about 36 s of slack, so 150 is the safer value.

## Multi-repo slices (r15)

When PLAN.md declares `repos:` (MAILBOX-SCHEMA.md "Declared repos (r15)"),
a retired entry's `repo:` names the repo its sha lives in. The loop core
puts it in the slice-eval context (`repo`), and trioctl binds the detached
eval worktree from THAT repo (its own ledger and
`<state>/worktrees/<repo>-<hash>/` root), so concurrent slice-evals of
different repos never share a checkout. A context without `repo` (an older
core) falls back to the slice's PLAN.md `repo:`; a failed bind degrades
exactly as before. The Lead's gate is one per repo the pass changed (LOG
suffix `gate: PASS @<repo>:<sha>`; home keeps `gate: PASS @<sha>`), and
`full_check:` may be a `<repo>: <command>` mapping, each command run from
its repo's root. The integration eval pins one sha per repo
(`evaluated_repos` in STATE.md, `evaluated: home@<sha>, <repo>@<sha>` in
VERDICT.md) and fences worker merges in every repo while it grades.

## Many loops per repo (interim, r15.x)

Until r16 gives every loop its own aggregate worktree, all loops of one
repository share the root checkout and its single project Cursor slot
(`.cursor/mcp.json` + `hooks.json`, read by every cursor-agent whose
nearest `.git` ancestor is the root). r15.x makes Trio serialise itself
there instead of crashing, and makes the non-Trio case fail clearly. It
does **not** make N loops on one branch correct: builder merges, the dirty
gate, the shared index and SHIP acceptance still see each other (r16
DESIGN C7-C12). In particular a tracked mailbox of another live loop, or a
user's untracked file at the root, is still a product change to this
loop's SHIP acceptance (`needs_retirement`, exit 6).

- **Root-turn lock.** One per-user `flock` per aggregate root:
  `$XDG_STATE_HOME/trio-agent-loop/root-turn/<sha256(realpath)[:24]>.lock`
  (`TRIO_ROOT_TURN_LOCK_DIR` overrides; files of vanished roots are
  pruned at loop start like `cwd-locks/`). Line 1 is `<pid> <root>`,
  line 2 the JSON holder record (`pid`, `pid_start`, `mailbox`, `role`,
  `kind`, `since`, `argv0`). The kernel releases it when the holder dies.
  - Held by a loop for every root-bound role turn: a Lead or repair pass
    for its whole turn (its session is now **ended at turn end**, not left
    idle at the root); a root-bound evaluator (lockstep eval, integration
    eval) from dispatch until its integration fence is released (the next
    Lead/repair dispatch, or loop end), so `_finalize_ship` runs inside
    the turn; a degraded (root-bound) slice-eval, joining its runner's
    turn when held. Re-entrant per runner.
  - Held by a non-isolated `trioctl omnigent run <role>` one-shot for its
    whole run (own process group, drained after exit) when its
    `--workspace` Cursor root is the aggregate root of a live loop
    (registry below). A one-shot started from inside a root session (a
    cursor-agent at that root is its ancestor: the turn holder's own
    scouts/builders) or naming the holder's mailbox shares that turn.
  - Never taken by isolated builders, bound slice-evals or `run --isolate`
    (own worktree, own slot).
  - Waits are bounded: `TRIO_ROOT_TURN_WAIT_S` (default 3900 s) for loop
    dispatches, `TRIO_ROOT_TURN_ONESHOT_WAIT_S` (default 900 s) for
    one-shots; a stderr line names the holder at the start and every
    `TRIO_ROOT_TURN_PROGRESS_S` (60 s); a loop wait longer than that adds
    one `- iter N | loop | root turn: <kind> waited Ns ...` LOG line and
    accumulates `root_turn_wait_s` in `.driver.json`. flock is not FIFO.
  - An integration eval / lockstep eval that had to wait is re-pinned
    through the loop core's own binding helper when another loop's turn
    moved the product meanwhile (the core pins before it dispatches).
- **Root one-shots share the root's `.cursor` slot.** A headless one-shot
  at the root loads whatever Omnigent session config is in the root slot;
  that is why it takes the turn while a loop is live. Its cwd is now
  always `--workspace` (never the caller's cwd).
- **Foreign cursor-agent at the root** (the user's own session, an older
  Trio, a leaked process -- now also one started in a subdirectory of the
  root): a root-bound dispatch waits up to `TRIO_ROOT_STRANGER_WAIT_S`
  (default 300 s, a stderr line every 30 s), then stops cleanly: STATE
  `status: needs_human`, `phase: root-occupied`, `reason: root-occupied`,
  one LOG line naming pid, cwd, start time, parent chain and command,
  `.driver.json` `lead_alive`/`eval_alive` false, exit **9**. Close it and
  resume with `trioctl omnigent loop --mailbox <mb>` (the `reason:` line is
  dropped on resume). Proceeding without Omnigent's config rewrite is not
  safe and is not offered.
- **Driver exceptions** from any dispatch (e.g. the integration eval)
  never leave STATE `running`: `status: error`, `phase: driver-exception`,
  `reason: driver-exception`, one LOG line, sidecars not alive; sessions,
  fences, the root turn and the mailbox lock are released; the exception
  still exits 1 as before (a root-free run exits 3 through the same
  mechanism). A held dispatch keeps its `needs_human` STATE.
- **Overlapping `writes:` across live loops are refused.** Each driver
  writes one record `<git common dir>/trio-worktrees/loops/<mailbox-slug>.json`
  (removed at exit; stale when the pid identity is dead; one schema for
  root-bound, lockstep and root-free loops -- MAILBOX-SCHEMA "Live-loop
  registry"). At loop start and at every Lead/repair pass, this mailbox's
  PLAN.md `writes:` are compared per repository (every worktree of a repo
  counts as that repo, so root-free loops are compared too) with every
  other live loop's (prefix-covering either way). Start: refused with exit 2, naming the other mailbox and the exact
  overlapping paths (one LOG line, STATE untouched). Mid-run: the pass is
  not dispatched; STATE `needs_human` / `writes-overlap`, exit 5.
  `TRIO_ALLOW_OVERLAPPING_LOOPS=1` (or `--allow-overlapping-writes`)
  proceeds with a LOG warning. Loops of an older release are not in the
  registry (only the stranger check sees their sessions).

# trio-opencode parity audit

Audit of every verification-quality feature of the two reference trios —
**Omnigent/Cursor** (installed r20, `r20-rc` 8a6de43: `omnigent/trioctl`,
`metrics/`, `prompts/`) and **Claude-native** (`native/` on r20-rc plus
`r19-native` b10c13a, frozen acceptance) — against the standalone OpenCode
driver (`opencode-driver/`), **before** (`opencode-v01` a3d726e) and
**after** (branch `opencode-parity`).

Legend: **present** / **missing** / **partial** / **n/a** (with reason).
"Shared core" = `metrics/trio_loop.py` reached through
`native/trio_native_step.py`, which the OpenCode driver loads in-process
(`trio_opencode/steplib.py`), so those rows are identical by construction.

## Role prompts

| Feature | Omnigent | Native | OpenCode before | OpenCode after |
|---|---|---|---|---|
| Role bodies single-sourced from `prompts/canonical` via `generate.py` | partial: evaluator gets extracted rigor blocks (`trio-evaluator-rigor`, `integration-rigor.md`); lead gets dispatch template + protocol essentials | present: `.claude/agents/trio-*.md` rendered by the `.claude` overlay | **partial**: canonical, but rendered for the *in-OpenCode plugin* flavor (`opencode/agents/`), whose slots contradict the standalone driver (below) | present: own overlay `prompts/overlays/opencode-driver.md` → `opencode-driver/agents/trio-{lead,evaluator,builder,repair,acceptance}.md`; `generate.py --check` covers them |
| Evaluator allowed to execute anything verification needs | present (yolo executor) | present | **missing**: body said "Every other Bash command is denied, including commands that write files, install dependencies…" and listed only this repo's own smoke test — contradicting the driver's `bash "*": allow` and steering the Evaluator to grep-only checks | present: driver-flavor RULES — run/serve/headless browser/full suite, install test tooling inside the sandbox; mailbox-only edits |
| Lead/Builder/Repair commit rules match the driver | present | present | **missing**: bodies said "Never commit or push" while the driver's gate requires `slice(<id>):` commits (only the per-call prompt fixed it) | present |
| Lead delegation text matches driver-owned builders | present (Omnigent dispatch) | present (native per-call prompts) | partial: body said "MUST delegate to Task child trio-builder" (overridden only by the driver note) | present |
| Evaluator method, evidence kinds, receipt-only never PASS, tautology rejection list, test-integrity audit, suites outside targeted check | present (RIGOR_CORE) | present (full body) | present (full canonical body) | present |
| Whole-goal rigor: ≥2 attacks, independent probe, data-work profile, implement-then-smoke re-run (integration-rigor) | present (`integration-rigor.md` appended to every lockstep eval) | present (full body) | present (full canonical body) | present |
| **New rule (a)** goal-derived pass/fail; unused input field = missing feature; "decoy" needs GOAL citation | missing | missing | missing | present in all three: canonical lead `## Goal-derived criteria`, evaluator `## Goal-derived pass/fail` (RIGOR_CORE → Omnigent evaluator; new `trio-lead-criteria` block → Omnigent lead) |
| **New rule (b)** unverified ⇒ no SHIP (build the oracle or ITERATE); static grep never evidence for behaviour | partial ("remaining unverified GOAL criteria prevent whole-goal SHIP") | partial (same) | partial (same) | present in all three: evaluator `## Closing unverified claims` |

## Loop mechanics (shared core unless noted)

| Feature | Omnigent | Native | OpenCode before | OpenCode after |
|---|---|---|---|---|
| Commit gate after Lead/Repair (`trio-shadow --require-commits`, LOG gate, REPORT rewritten, 1 retry then error) | present | present | present (shared core via steplib) | present |
| Evaluator pin (`attempt:`/`evaluated:`), SHIP retirement checks | present | present | present (shared core) | present |
| Scoped repair `ITERATE scope=local` + repair cap | present | present | present (shared core) | present |
| HUMAN.md: only driver-verified ledger answers | present | present | present (helper `_human_answer`) | present |
| r19 frozen acceptance: switch (CLI > env `TRIO_ACCEPTANCE` > config, default off) | present | present (`args.acceptance`, `launch.sh --acceptance`) | **missing** (only a `models.acceptance` tier check in config/doctor) | present — see README "Frozen acceptance" |
| r19 author at Lead/Evaluator tier, refused otherwise | present | present | partial (config/doctor only) | present (config + helper `begin --models`) |
| r19 freeze (validation at base, retry, contaminated re-run, author isolation audit) | present | present (transcript or limited audit) | missing | present (audit from the OpenCode author turn's tool calls; author cwd = export) |
| r19 author isolation: prevent reads, not only detect them | prompt + audit (Cursor cwd = export) | prompt + audit (author told to `cd`; no mechanical block) | missing | **beyond both**: `sandbox` (bwrap, only the export visible, shell kept) where a user namespace starts, else `no-shell` (no shell, file tools confined to the export by a per-turn config modelled on OpenCode v2's own path resolution, escaping symlinks removed), also in `container_mode`; audit ignores written text and permission-refused calls |
| r19 author failure | status `error` | status `error` | missing | open-loop: degrades to no pack with a loud LOG line and runs on (`DegradableAcceptance`); lockstep still stops `error` |
| r19 author wait | 900s default bound | n/a (synchronous) | missing | open-loop: unbounded by default (`acceptance_wait_seconds`) |
| r19 coverage gate before builders, one re-plan | present | present | missing | present |
| r19 pre-run at pin + SHIP gate (`review_verdict`, anti-thrash, amendments) | present | present | missing | present |
| Lockstep evaluator gets integration rigor | present | present | present | present |

## Open-loop (QUEUE.md) mode

Ported in `opencode-driver`, merged into `opencode-parity`
(`trio_opencode/openloop.py`, `olprompts.py`, `quality.py`, `olqueue.py`,
multi-repo `rootfree.py` aggregates, and the CLI/config flags below).
Selected automatically — same trigger Omnigent/native use (a mailbox with
`QUEUE.md`), no `--open-loop` flag.

| Feature | Omnigent | Native | OpenCode before | OpenCode after |
|---|---|---|---|---|
| Mode selection: QUEUE.md presence, no flag | present (`omnigent/trioctl` `_loop_is_lockstep`) | n/a: native is lockstep-only | missing | present: `openloop.detect_open_loop` (D1), called from `driver.run`; root-free also checks an existing Lead worktree's live mailbox |
| Queue + per-slice retirement (`QUEUE.md` `retired:`) | present (`metrics/trio_loop.py:run_open_loop`) | n/a | missing | present: shared core `run_open_loop` reached via `steplib.TL`; driver appends `retired:` the moment a slice merges (`OpenLoopRunner._merge_and_retire` → `olqueue.append_retired`) |
| Per-slice commit gate (`slice(<id>):` commit, branch contains dispatch head, no mailbox-file commits) | present (`trioctl` branch verify) | n/a | missing | present: `OpenLoopRunner._verify_branch` |
| Slice-eval dispatch as slices land | present (`trio_loop.run_open_loop`) | n/a | missing | present: shared core dispatches; `OpenLoopRunner._run_slice_eval` is the eval runner |
| Concurrent slice-evals (default 4) + drain/abandon hold | present (`trioctl` `--slice-eval-concurrency`) | n/a | missing | present: `openloop.resolve_settings` (`slice_eval_concurrency`, default 4; `slice_eval_drain_seconds` passed through to `TL.run_open_loop`). On exit, `drive()` sets `ctx.cancel` (and waits, bounded, for every live turn to actually clear) the moment `run_open_loop` returns/raises — the SAME event every role turn's `runner.run_turn` watches, so an eval the core's own drain window marked `abandoned` is actually killed (process group + thread) within its ~0.2s poll instead of the CLI lingering for the turn's full timeout; that cancellation is bookkeeping-only and never overwrites the run's already-decided status/code |
| Slice-eval verdict/fault path | present (core) | n/a | missing | present: shared core parses/applies VERDICT.md and QUEUE.md `faults:`; `OpenLoopRunner._run_slice_eval` only runs the turn |
| Integration-eval + whole-goal rigor | present (core + `integration-rigor.md`) | n/a | missing | present: `OpenLoopRunner._run_integration_eval`, rigor block from `prompts/integration-rigor.md` via `openloop.integration_rigor()` (generated by `prompts/generate.py`) |
| SHIP retirement | present (core) | n/a | missing | present: driven by `TL.run_open_loop`'s own stop codes, finalized in `openloop._finalize_result`/`_final_status_for_code` |
| Root-free land hook | present (`omnigent/root_free.py` ~1236-1300) | n/a (native has its own single-repo land) | missing | present: `openloop.make_land_hook` (D11), ported/simplified onto this driver's own `rootfree.py` (declared-repos-first, home-last); no Omnigent `reverify` re-land rule (see below) |
| Multi-repo (PLAN.md `repos:`) | present (`omnigent/root_free.py` `RootFreeAggregates`) | n/a: native is single-repo | missing | present: open-loop only — `openloop.declared_repos_for_prepare` (D2) feeds `rootfree.prepare(..., declared=...)`; `driver.run` only computes `declared` when `is_open_loop` is true (lockstep stays single-repo, verified in `driver.py`) |
| r19 acceptance in open-loop (author hook) | present | n/a (native's r19 is lockstep) | missing | present: `OpenLoopRunner.author` (D13), the r19 `AcceptanceController`'s author hook, reusing `_acceptance_tool_path`/`prompts_mod.acc_plan_lines`/`acc_pass_lines` |
| Isolation flags (`--isolate-workers`/`--no-isolate-workers`) | present (`trioctl`) | n/a | missing | present: `openloop.resolve_settings`; default on in open-loop, a documented no-op in lockstep (trio-opencode lockstep builders are always isolated) |
| Worker isolation of builders/evals (driver-created worktrees) | present | n/a | missing | present: `OpenLoopRunner._create_builder_worktree`/`_create_eval_worktree`; concurrent builder wave dispatch via `_run_wave`'s `ThreadPoolExecutor` (one worker when isolation is off) |
| QUEUE.md concurrent-write guard | missing (Omnigent guards only VERDICT.md) | n/a | missing | present (stronger than Omnigent): `olqueue.QueueGuard` — re-appends a `retired:`/`faults:` entry a concurrent full-file rewrite dropped, and renumbers colliding fault ids, without touching a legitimate field change |
| STATE protection (driver-owned STATE.md keys reasserted after every role turn) | present (core) | present (lockstep `_StateGuard`-equivalent) | present (lockstep) | present: `openloop._StateGuard` wraps `TL._update_state` for the open-loop run and restores deviations via `driver._restore_owned_state` after every lead/evaluator turn |
| Crash/resume (STATE snapshot, builders map) | present (core) | present | present (lockstep) | present: `openloop._restore_resume_state` (H5) restores `OWNED_STATE_KEYS` from the prior `.driver.json` gated on `run_token`, and folds its `builders` map back in via `OpenLoopRunner.load_resumed_builders` |
| r18a L1 evidence-kind telemetry (`slice_evidence`, `evidence_log_line`) | present (`trioctl` ~2833-2971) | n/a | n/a (was lockstep-only in OpenCode before) | present: `quality.slice_evidence`/`evidence_log_line`, called from `OpenLoopRunner._run_slice_eval` after every slice-eval turn |
| r18a L2a base-revert kill check (`run_kill_check`) | present (`trioctl` ~2659-2742) | n/a | n/a | present: `quality.run_kill_check`/`kill_check_for_builder`, called from `OpenLoopRunner._run_one_builder` after a passing builder, before merge (never gates the merge decision) |
| SLICE QUALITY / PRE-GATE prompt block | present (`trioctl` `_quality_before_dispatch` ~7504-7529) | n/a | n/a | present: `quality.quality_note`, appended to the slice-eval prompt's `quality_note` context key. `quality.resolved_kill_check` substitutes trioctl's own two `n/a` placeholders (~7480-7491) whenever `kill_check` is `None` under isolation — a Lead take-over/fix, a kill-check-off run, or a brief with no targeted command — so BASE-REVERT/AUTHORED-BY always appear together, matching trioctl verbatim in every case; the retired LOG line shares the same resolved fact and is skipped (never an empty `by builder |  (shadow)`) when isolation is off |
| `_slice_lint` (test tautology + accept lints per slice-eval) | present (`trioctl` `AgentRunner._slice_lint` ~7358-7420) | n/a | n/a | present: `quality.slice_lint`, called from `OpenLoopRunner._run_slice_eval` |
| `_lint_after_lead_pass` (mailbox-wide quality lint → `.driver.json` `lint`) | present (`trioctl` ~7318-7347) | n/a | n/a | present: `quality.lead_pass_lint`, called from `OpenLoopRunner._run_lead` after the lead-review turn, written to `driver_meta["lint"]` |
| r18a L3 independent-probe logging (lockstep, advisory) | present | n/a | n/a | **not ported** — stays an Evaluator-body requirement only; see below |

### Remaining differences from Omnigent

- **Driver-owned builders.** The OpenCode Lead agent cannot run a long
  builder process itself, so `OpenLoopRunner._run_lead` plans, then
  dispatches/merges/retires every slice itself (`_run_wave` →
  `_dispatch_and_retire`), then runs a lead-review turn. Omnigent's Lead
  runs `trioctl omnigent run builder` directly.
- **A failed targeted check is never merged** (vs Omnigent's merge-then-
  revert): `_dispatch_and_retire` checks `quality.targeted_check_failed`
  before `_merge_and_retire` ever runs; a failing or unverifiable branch is
  cleaned up unmerged and the slice becomes a Lead take-over.
- **No Omnigent `reverify` re-land rule.** A diverged land target returns
  `needs_land` (documented in `openloop.make_land_hook`'s docstring); only
  `trio-opencode land` (or the hook again on resume) retries it — no
  automatic re-verify-and-relant.
- **No Cursor/Omnigent residue checks** — nothing in this driver imports or
  shells out to `omnigent`/`trioctl` (see README's own `sys.modules`
  assertion).
- **No broker session archive/reconcile/held-session receipts.**
  `--reconcile-held`, `--completion-receipts`, `--keep-sessions`,
  `--observe-workers` are Omnigent/Cursor broker flags with no trio-opencode
  equivalent (n/a — there is no broker session store here).
- **`--isolate-workers` is a lockstep no-op**: trio-opencode lockstep
  builders are already always isolated in their own worktrees
  (`openloop.resolve_settings` prints a one-line notice and forces
  `isolate=True`, `slice_eval_concurrency=1` for a lockstep mailbox).
- **Poll interval is an env var, not a fixed 30s**: `TRIO_OPENCODE_POLL_SECONDS`
  overrides the default 30s poll Omnigent's loop uses unconditionally.
- **QueueGuard is extra protection** Omnigent does not have. In both
  drivers up to N concurrent slice-eval sessions (and the Lead) edit
  `QUEUE.md` by hand at the same absolute path; Omnigent's core restores only
  clobbered VERDICT.md slice sections (`_restore_clobbered_verdict_sections`)
  and relies on the append-only convention for QUEUE.md. trio-opencode's
  `olqueue.QueueGuard` additionally re-appends `retired:`/`faults:` entries a
  full-file rewrite dropped and renumbers colliding fault ids after every
  turn, and the driver's own `retired:` appends are flock-serialised.
- **A plan returning an already-retired slice without `fault:` is refused**
  (one re-plan): the core forces a first Lead pass on every (re)start, and a
  driver-owned dispatch would otherwise rebuild merged work. Omnigent's Lead
  decides this itself.
- **Integration-eval workspace**: a declared repo's SHIP retirement commit
  goes to its Lead aggregate (named in the MULTI-REPO pin listing), the
  grading copy is a separate detached worktree; with `--no-isolate-workers`
  the workspace is the Lead worktree itself (Omnigent binds the same
  detached worktrees via `_integration_eval_worktree`).
- **Default `--max-iterations` stays 4** (trio-opencode's existing lockstep
  default, unchanged for open-loop); trioctl's own `omnigent loop` default
  is 10 (`omnigent/trioctl`, `loop --max-iterations`).
- **Lead-review/take-over commits are retired only when the driver re-runs
  the slice's own targeted check.** A Lead take-over or fix commit
  (`slice(<id>): ...`, made directly during the lead-review turn, not via a
  builder worktree) is retired by `_retire_one_lead_commit`, which
  synchronously re-runs the brief's `## Targeted check` command (brief
  preferred; the plan's own `targeted_check` field is the fallback) against
  the Lead worktree at that commit, under the same kill-check budget
  (`quality.kill_check_budget`) — a non-zero exit, a timeout, or no known
  command at all leaves the commit UNretired (logged, not an error) rather
  than trusting the Lead's own say-so. Omnigent leaves this entirely to the
  Lead; trio-opencode's driver gates it itself.
- **Crash-consistent merge/retire.** `_merge_and_retire` runs `git merge`
  then `olqueue.append_retired` as two separate steps; a SIGKILL between
  them used to wedge every later resume (the merged slice looked unbuilt
  forever). The driver now owns a write-ahead **merge-intent record**,
  `<live mailbox>/.merge-intent.json` (next to `.driver.json`; a runtime
  sidecar, `.merge-intent.json*` is appended to the mailbox `.gitignore`).
  Format: `{"version": 1, "intents": [{"slice", "branch", "repo" (absolute
  path), "repo_field" (the QUEUE.md `repo:` name, null for home),
  "pre_head" (that repo's HEAD before the merge), "run_token", "at"}]}` —
  a list because several merges can be in flight at once (`git merge` is
  serialized per repo, but the record is written before it and deleted after
  the `retired:` append, both outside the merge lock, so sibling records for
  the SAME repo coexist and are reconciled independently). It
  is written atomically (tmp + fsync + rename, under the repo's merge lock)
  BEFORE `git merge`, and deleted once the slice's `retired:` entry is
  appended (or the merge is aborted on conflict). On every start/resume,
  `openloop._reconcile_merge_retirement` runs before the Lead and consumes
  each record: it looks in that repo's first-parent `pre_head..HEAD` for the
  exact commit `merge slice <slice> (<branch>)`; found → the missing
  `retired:` entry is appended once (with `repo_field`) and logged; not
  found → the crash fell before the merge committed, any half-done merge is
  aborted and the record dropped (the slice is simply still unbuilt); a
  record from another run token is dropped untouched. A `merge slice`
  commit with NO record is never imported. There is no git-history walk
  (round 1 walked the whole history; rounds 2-3 bounded it by the last commit
  touching QUEUE.md, `_mailbox_epoch_boundary`, which was wrong for a mailbox
  that is untracked, gitignored or outside the repo, for a reset that leaves
  QUEUE.md's bytes unchanged, and for a commit made after the crash), so the
  scheme does not depend on the mailbox being tracked or committed, ignores
  every other goal's merges by construction, and is unaffected by later
  commits. Caveat: the record lives in the mailbox directory, so it follows
  the mailbox. A mailbox that crashed in the gap and is then reset for a
  brand-new goal WITHOUT being resumed first (same path, same default run
  token) would still have that one record honoured; delete
  `.merge-intent.json` when abandoning a crashed mailbox.
  **A different `--run-token` on resume drops the record.** The record is
  bound to the token of the run that wrote it, so `resume` without the
  original `--run-token` (the default token is fresh per `start`) drops it:
  the crashed merge stays in HEAD but is NOT retired, and the run wedges the
  way it did before the record existed (the Lead sees the slice unbuilt).
  The drop is not silent: a `WARNING: dropping merge-intent record for <slice>`
  line goes to `LOG.md` and stderr naming the mismatch. Resume with the
  original token to reconcile.
- **Stale MERGE_HEAD cleanup (ol-harden2).** A driver killed while a
  `git merge` child is still running can leave `MERGE_HEAD` behind after HEAD
  already advanced (the child dies of SIGPIPE writing its summary to the dead
  driver's pipe). The next commit — the Evaluator's `loop: iteration N — SHIP`
  — would then be a two-parent merge commit that touches no mailbox paths, and
  the run would end `needs_retirement`. Two defences: `_merge_and_retire`
  runs `git merge -q` with stdin/stdout/stderr on `/dev/null` (no pipe to
  die on), and on every start/resume `_reconcile_one_intent` (found-commit
  path) plus `_sweep_stale_merge_state` (every managed repo, record or not)
  clear a `MERGE_HEAD` that is already an ancestor of HEAD with
  `git merge --quit` (fallback: delete MERGE_HEAD/MERGE_MSG/MERGE_MODE) —
  never `--abort`, which would be wrong for a committed merge. A MERGE_HEAD
  that is NOT in HEAD is a live conflicted merge and is only reported
  (`WARNING` in LOG.md), never touched.
- **QUEUE.md writes (ol-harden2).** Every driver-side writer
  (`append_retired`, the guard's fault re-append, the duplicate-id renumber,
  the seed of a missing QUEUE.md) takes `olqueue.queue_lock` and publishes
  with tmp + fsync + rename. Role agents (slice-evals, the Lead — `opencode`
  processes with their own edit tools) also write QUEUE.md and cannot be
  locked, and their writes can be a non-atomic truncate-then-write, so the
  driver's read-modify-write tolerates them instead: reads settle (a
  zero-byte or still-changing file is re-read), the self-check never restores
  a pre-write snapshot (it used to, which dropped a slice-eval's concurrent
  ITERATE fault and raised), the entry's presence is verified after the
  rename and the whole read-modify-write retried against newer content if an
  agent's write replaced ours (bounded; `QueueError` only if it never
  appears, leaving the file as found). Residual: an agent write that lands on
  the old inode just before our rename is invisible to any lock-free scheme;
  `QueueGuard` (re-append of observed entries) is the net for that.
- **`git worktree add` race (ol-harden2).** A concurrent `worktree remove`
  of the last sibling worktree can delete the then-empty `.git/worktrees`
  directory mid-add (`could not create directory of '.git/worktrees/...'`).
  `openloop._git_worktree_add` retries once for exactly that failure (the
  `-b` branch is created before the worktree, so the retry checks it out).
  Not serialized per repo.
- **Exit drain actually kills an abandoned slice-eval.** See the "Concurrent
  slice-evals" row above. `OpenLoopRunner.inflight_sessions()` only reports
  an entry once its REAL opencode session id is known — `drv._call_role`/
  `runner.run_turn` reveal a brand-new turn's session id solely in its
  RETURNED `TurnResult`, so a still-running turn's session id is simply
  never knowable from here, and such an entry is left OUT entirely rather
  than reported under its own turn label (which `_write_abandoned_hold`
  would otherwise persist into a `held-<label>.json`'s `session_id` field
  as if it were real). Net effect: **resume does not honour a held record**
  for a freshly abandoned slice-eval today — there is no live session id to
  resume or reconcile against, only the now-killed process. A `held-*.json`
  for such a run therefore never has a meaningful `session_id` to act on;
  treat its presence as "a slice-eval was abandoned here", not as a pointer
  to a resumable opencode session.
- **A Lead that never writes PLAN.md's own `slices:` block is told to.**
  `metrics/trio_loop.py::_slices_fully_retired` (the core's "is this
  open-loop run done" check) reads PLAN.md's own `slices:` block, never the
  Lead's structured JSON reply; a Lead that only ever replies with
  `slices: [...]`/`slices: []` and never writes that block into PLAN.md
  itself left the run looking eternally incomplete — stalling
  (`status: error`) after 3 no-op passes even once every slice it ever
  returned was retired. Matters for a mailbox seeded with only GOAL.md
  (e.g. a benchmark harness). `openloop._run_lead` now adds a note to the
  lead-plan turn's prompt whenever `TL._read_plan_slice_ids` returns `None`
  (no block yet); `_validate_plan` already accepted a plan with no PLAN.md
  block in the first place, so nothing is refused, only told.
- **`role_denials` is now filled in for lockstep permission kills.** A
  permission denial during a lockstep turn appends an entry to
  `ctx.role_denials` (`driver._call_role`) the same way open-loop's own
  denial path always has; on the pin this was `[]` for a lockstep
  `permission_hang` run (`probes/test_ev_lsperm.py`), now one entry —
  undocumented until this note, otherwise benign (the Lead's own
  "byte-identical lockstep" fence compares the two and should expect this
  field to differ going forward).

## Not applicable / not ported

| Feature | Where | Why not in OpenCode |
|---|---|---|
| r18a L3 independent-probe logging, `_lint_after_lead_pass` (mailbox-wide quality lint into `.driver.json`) in LOCKSTEP mode | Omnigent lockstep, advisory | not ported in lockstep: shadow telemetry with no effect on verdicts; native lacks it too. The probe *requirement* itself is in the Evaluator body (present). (Open-loop mode now has its own `lead_pass_lint` — see the open-loop table above; L3 independent-probe *logging* itself is not ported in either mode.) |
| `trio-check.py --strict-quality` | CLI only | n/a: not called by any live loop |

## Status of this branch (opencode-parity)

- The r19 port's unit tests (switch, tier, audit override, polling, prompt fragments,
  switch-off identity) pass. The full-loop e2e with a real frozen pack (SHIP, gate
  refusal, coverage re-plan, contaminated author, validation retry) has landed
  (branch `ocp-s3-acc-e2e`, merged into `opencode-parity`): `tests/test_e2e.py`
  drives `op_acceptance_freeze` through the fake-opencode harness end to end,
  including the OpenCode author audit's `path` surfacing correctly (not silently
  "native") in both `.opencode-result.json` and the frozen `MANIFEST.json`.
- Canonical prompt rules are also a standalone commit `140b259` (branch
  `goal-derived-rules`, on r20-rc), so a release can merge them on their own.
- **Driver-owned STATE keys** (r21): `iteration`, `phase`, `evaluated_sha`,
  `evaluator_attempt`, `evaluated_repos` are restored from a snapshot bound to
  the run's `run_token` after a crash. Lead/repair/evaluator writes to these keys
  are reverted at turn end; crash recovery restores the pre-turn snapshot when
  the same `run_token` is used.
- **Failure classification (r21)**: A truncated or near-empty event stream is
  transient and retried with bounded retries. "Unsupported opencode version"
  requires 3+ events all of unknown types. API 408/409/425/429/5xx and
  `isRetryable` are transient; 401/403 are config errors. INFO-level stderr no
  longer reclassifies a finished turn. Permission text inside tool output or
  echoed commands does not kill a turn.
- **`step_long` exhaustion** (r21): A timeout polling a detached helper job
  (acceptance-run, etc.) is a clean error stop, matching native.
- **Model aliases (r21)**: `"opus"` and `"sonnet"` resolve to concrete model ids
  at `begin` time; the dashboard also accepts legacy exact-pin ids.
- **Open-loop (QUEUE.md) port**, merged into `opencode-parity`:
  `trio_opencode/openloop.py`, `olprompts.py`, `quality.py`, `olqueue.py`,
  multi-repo `rootfree.py` aggregates, and the CLI/config flags above — see
  the "Open-loop (QUEUE.md) mode" table. Unit tests
  (`tests/test_olprompts.py`, `test_quality.py`, `test_olqueue.py`,
  `test_rootfree.py`, `test_openloop.py`) and a growing end-to-end suite
  (`tests/test_openloop_e2e.py`) exist; no live-provider open-loop run yet
  (same caveat as the rest of this driver — see README §11).
- **ol-repair** (this pass): crash-consistent merge/retire
  (`openloop._reconcile_merge_retirement`), the exit drain actually
  cancelling an abandoned slice-eval instead of letting the process linger
  (`openloop.drive`'s `ctx.cancel` + bounded wait), trioctl-faithful SLICE
  QUALITY/retired-LOG-line placeholders (`quality.resolved_kill_check`), and
  a Lead that never writes PLAN.md's own `slices:` block now being told to
  in the lead-plan turn's own prompt (`openloop._run_lead`) — see the
  "Remaining differences" bullets and the "Concurrent slice-evals"/"SLICE
  QUALITY" rows above for each one's exact behaviour.

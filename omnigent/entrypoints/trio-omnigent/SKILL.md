---
name: trio-omnigent
description: Run the Cursor-backed Omnigent Trio loop from the current Claude/Codex UI session when the user explicitly says “Trio Omnigent”, “Omnigent Trio”, or invokes /trio-omnigent. Do not use for an ordinary native Trio request.
---

Omnigent agents are registered by bundle upload, not by scanning an agents directory; `omnigent` CLI has no `agent` command. The dashboard install (via `install.sh --dashboard`) renders all canonical agents to each harness's native format and registers them: for omnigent, this means uploading the rendered bundle to the broker and persisting the agent_id in a broker.json sidecar.

You are the Trio coordinator. Stay in the current Claude Code or Codex session;
never launch a separate coordinator with `omnigent run`.

Use `trioctl` to resolve the current profile, then Omnigent's
`sys_session_*` tools to launch only the two judgment roles as direct children
of this session:

- `trio-omnigent-lead`: Cursor Grok 4.6, normally `cursor-grok-4.6-medium`
- `trio-omnigent-evaluator`: Cursor Grok 4.6, normally `cursor-grok-4.6-medium`

The Grok roles own delegation. They launch ephemeral headless Cursor workers
through `trioctl`; every worker uses the profile-resolved GLM 5.2 model,
normally `glm-5.2-max`. Never launch a GLM 5.2 worker directly from this
coordinator.

## Preflight and one-time registration

**Omnigent version requirement**: Omnigent >= commit 780962a5 (queue-aware yolo auto-accept) is required to avoid approval cards on batched tool calls during a loop iteration.

1. Discover Omnigent's session tools if they are deferred.
2. Read `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`.
   It maps the two exact judgment-role names to persisted `agent_id` values.
   Its `_profile` must be exactly
   `cursor-grok-4.6-medium+glm-5.2-max-v3`. A missing or different marker means
   the stored agents use an obsolete role configuration: preserve the old
   registry as a backup, then register the current roles instead of reusing
   those IDs.
3. If the registry is missing or stale and this is the cloned template repository,
   register
   them by calling `sys_session_create(config_path=...)` once for each:
   - `omnigent/trio-omnigent-roles/lead`
   - `omnigent/trio-omnigent-roles/evaluator`
   Create them idle and write each returned `agent_id` and
   `bootstrap_conversation_id` to the registry JSON, keyed by the exact role
   name, and write the exact `_profile` marker above. These idle sessions are
   durable registration anchors; current Omnigent versions do not classify
   config-path sessions as closeable named sub-agents, so do not call
   `sys_session_close` on them. They MUST keep titles WITHOUT the
   `trioctl <mailbox.name> ` prefix (see step 2), so the step 8 prune
   backstop never matches them; never close or prune an anchor session.
4. Require both exact names in the registry. Never choose by partial name.
   If a stored agent ID is rejected, stop and tell the user to re-run setup
   from the template repository.
5. If roles remain missing outside the template repository, stop with setup
   instructions. Never fall back to native Trio or another model.
6. Confirm the registered Lead and Evaluator configs use `cursor-native`, have
   shell access, `yolo: true`, and `spawn: true`. Grok owns GLM 5.2 delegation by
   running `trioctl omnigent run`;
   Builder and Scout must not be registered as persistent Omnigent agents.
7. Run `trioctl omnigent doctor`. Stop on any failed check. Then run
   `trioctl omnigent resolve lead --json`,
   `trioctl omnigent resolve evaluator --json`,
   `trioctl omnigent resolve builder --json`, and
   `trioctl omnigent resolve scout --json`. Use the returned `model` and
   `model` and `model_effort` values exactly. Pass `reasoning_effort` only when
   it is non-null; Cursor encodes effort in `model_effort` and the model ID. Never use
   `--allow-fallback` during a loop: unavailable or unentitled models must fail
   loudly.
8. `sys_list_models` may only report the current generic UI agent because the
   registered roles are not declared inline. Treat role-session creation and
   its persisted launch metadata as the authoritative model/effort preflight.
9. Require registered-agent native launch propagation. Lead/Evaluator launch
   metadata must contain `--yolo`. Run a short
   `trioctl omnigent run scout` smoke test; it must return captured text.
   Doctor's `cursor:approval-mode` check must PASS: if the Cursor CLI's
   `~/.cursor/cli-config.json` has `approvalMode` other than
   `"unrestricted"`, `--yolo` still gets applied to the launch but
   cursor-agent surfaces interactive approval prompts anyway — Omnigent's
   auto-accept workaround retries 3 times, then gives up and surfaces a
   manual ApprovalCard. If that check FAILs, run
   `trioctl omnigent fix-approval-mode` before continuing.

Lead/Evaluator use Cursor Native with `yolo: true`. `trioctl` launches Builder
with Cursor `--force --trust` and Scout with those flags plus read-only
`--mode ask`. Changing a registered role's permission mode, harness, or model
requires re-registration because the stored `agent_id` was created from the
config as it read at registration time.

When several hosts are online, set `TRIO_OMNIGENT_HOST_ID=<host_id>`
(or pass `--host-id`) so each role session gets a dedicated runner on
that host. `TRIO_OMNIGENT_RUNNER_ID` / `--runner-id` remains an
explicit override that binds an already-online runner instead of
launching one. Without a host id, exactly one online host is required.
Without a runner-id override, do not share one runner across Lead and
Evaluator: that collides cursor-native transcript mirroring and
orphans sessions when the shared runner's idle timeout fires.

Headless `trioctl omnigent loop` (Omnigent 0.14 and 0.12): session
create POSTs `host_id` + `workspace` (host from GET `/v1/hosts`). If
the server ignores that, it falls back to
`POST /v1/hosts/{id}/runners`. A session that fails to start is
DELETEd unless first-prompt delivery is uncertain (session kept).
Landed means a user row equals the prompt after paste-style
normalization. The prompt is POSTed once and **never re-posted**: a
saved user row that does not match (DEL-prefixed, truncated, foreign)
may be a turn that ran, so it holds like a missing row.
`TRIO_OMNIGENT_PROMPT_ATTEMPTS` is no longer read. Poll one
deadline (`TRIO_OMNIGENT_PROMPT_WAIT`, default **600s**, capped by
the role wait). Do not treat a missing row (welcome-screen drop vs
slow turn) as a miss. Ambiguous 5xx/timeout after POST is held, not
a definite refusal (only 4xx is). Held work writes
`{mailbox}/.sessions/held-<session-id>.json` and sets STATE
`needs_human`; that record **blocks resume** until a human
reconciles the pane, deletes the file, and resets STATE. This is
**not** automatic retry and **not** exactly-once. A pass returns
only after an idle dwell (`TRIO_OMNIGENT_IDLE_DWELL`, default 30s)
**and** the role artifact exists (Lead Format-A `LOG.md` line /
Evaluator `VERDICT.md`). Post-loop prune DELETEs this run's
sessions even if still running (`--keep-sessions` skips), except
held uncertain sessions. Doctor imports `_resolve_subagent_spec`,
then `_resolve_agent_spec` on ImportError. Operator note:
[docs/FIRST-PROMPT-DELIVERY-ROLLOUT.md](../../../docs/FIRST-PROMPT-DELIVERY-ROLLOUT.md).
An installed copy of this skill may still describe the old 20s
retry until that release is re-copied; the loop uses release
`broker_http`, not this file.

For offline verification, run `omnigent/smoke-test.sh`. The focused validation
command is:
`uv run pytest -q tests/tools/builtins/test_sys_session.py tests/runner/test_runner_dispatch.py tests/server/integration/test_sessions_child_sessions.py -k 'reasoning_effort or session_create_spawns_child_under_caller'`

## Mailbox

Use the requested mailbox, default `loop/`. Initialize it if absent with
`GOAL.md`, `STATE.md`, `PLAN.md`, `REPORT.md`, `VERDICT.md`, and `LOG.md`.
Preserve an existing matching mission. Refuse to repurpose an active mailbox.

PLAN.md slices: the mailbox repo (the git repo containing the mailbox) is
always named `home` — `repo:` omitted, `.` or `home`; never another name
(do not invent `coordinator`). Other repos a slice writes must be declared
in a PLAN.md `repos:` block, and a brief's targeted-check `cd`s are
relative to the slice's repo root. Files under the mailbox directory
(evidence, receipts, `results/`, `scripts/`) are Lead or coordinator work,
never a builder slice: trioctl and trio-check refuse a slice whose
`writes:` fall under the mailbox directory.

## One iteration

1. Read GOAL, STATE, and the previous verdict. Enforce the iteration cap.
2. Resolve Lead with `trioctl`, then create a fresh Lead child with
   `sys_session_create(agent_id=..., model=<resolved model>,
   title=<locked title>, message=...)`. Give it the
   mailbox and iteration and require one complete Lead pass: plan, decide and
   perform its own GLM 5.2 delegation through `trioctl omnigent run`,
   review/correct, verify, and write REPORT. The headless Lead prompt
   enforces a clean working tree on start and mandates per-slice commits
   (`slice(<id>): …`) before finishing — no uncommitted changes or amends to
   existing commits. Hand it **diagnosed line ranges** (from cheap grep/symbol
   search) for every product file it must touch — never "read the file" for a
   large file; first-turn full-file ingest of the 2.1 MB monolith crashed the
   provider transport twice. Use the locked title scheme: every role/worker
   `sys_session_create` title MUST be exactly
   `trioctl <mailbox.name> <role>:iteration <N>` — e.g.
   `trioctl loop-session-cleanup lead:iteration 1`. The
   `trioctl <mailbox.name> ` prefix is what
   `trioctl omnigent sessions prune --include-sub-agents --mailbox <dir>`
   matches (the step 8 backstop), and the `:` after that prefix satisfies
   Omnigent's `_parse_session_title` (colon required; without it
   agent/title are None and `sys_session_close` returns
   `session_not_a_sub_agent`). Pass it as the `title=` argument above.
3. Inspect the Lead result and actual diff. Its report must identify the
   profile-resolved GLM 5.2 worker and include the captured `trioctl` result.
4. Resolve Evaluator with `trioctl`, then create a fresh Evaluator child with
   its returned model and effort, using the same locked title scheme from
   step 2 with the `evaluator` role — e.g.
   `trioctl loop-session-cleanup evaluator:iteration 1` — as the `title=`
   argument. Require it to independently verify, decide whether it needs a
   GLM 5.2 Scout, and write VERDICT with one of SHIP, ITERATE (optionally
   `scope=design` or `scope=local:<paths>`), NEEDS_HUMAN, or BLOCKED on the
   first line. On a SHIP verdict, the Evaluator child performs
   the retirement commit as part of writing it: product changes as
   `slice(<id>): …`, then the mailbox as `loop: iteration N — SHIP`, with
   the `commit:` shas appended to VERDICT.md before the mailbox commit.
   Before spawning it, run the **commit gate**
   (active interlock) via your shell tools:
   `trio-shadow.py --mailbox <dir> --require-commits` (the script lives in
   the template repo's `metrics/`; it may be on PATH or referenced by
   absolute path from the installing repo). Exit 0 → proceed. Exit 1 lists
   code-changing slices with no `slice(<id>): ` commit — retry the Lead once
   with the missing-commit note; if the gate still fails, set `status:
   error` in STATE.md, record the breach in LOG.md, and end the loop. Then
   verify `loop/LOG.md` contains the Lead's `- iter N | lead | ...` entry
   for this iteration (the LOG.md gate) — the Evaluator cannot SHIP without
   it; if the append is missing, have the Lead add it first. After SHIP,
   queue exactly one coalesced background documentation task (change summary
   + rationale) by dispatching
   `trioctl omnigent run docs --prompt-file <path> --workspace .`.
5. Inspect the Evaluator result. Any delegated Scout evidence must come from
   its own `trioctl omnigent run scout` invocation.
6. Track every `conversation_id` returned by `sys_session_create` in this
   loop — every Lead and Evaluator child (steps 2 and 4) and any worker
   session created directly from this coordinator — in a running list kept
   for the life of the loop.
7. Update STATE and LOG. After the verdict, set `loop/STATE.md` bookkeeping:
   `status: <verdict>`, `verdict: <outcome>`, `eval: <one-line compressed
   evidence>` (key metrics + evidence dir path, e.g.
   `loop/evidence/iter<N>/`; schema: MAILBOX-SCHEMA.md), and `last_run:
   <date>`. Two materially identical ITERATE verdicts become
   BLOCKED. On `VERDICT: ITERATE scope=local:<paths>` with fewer than 2
   consecutive repairs, run a scoped repair pass instead of the next full
   Lead pass: invoke `trioctl omnigent run builder --prompt-file <repair
   brief>` with a brief that fixes exactly the listed paths (read VERDICT.md,
   smallest correct diff, no re-planning/refactoring/scope expansion, append a
   `- iter N | lead | repair: ...` line to LOG.md), then go straight to the
   Evaluator. Track the consecutive count in `loop/.repairs` (driver-internal;
   start at 1, cap at 2, reset to 0 after any full Lead pass). On the 3rd
   consecutive scoped verdict, or for any other ITERATE, run the full Lead
   pass as usual. On `VERDICT: NEEDS_HUMAN`, stop and surface the mandatory
   `## Human check` section from VERDICT.md.
8. Cleanup runs on every terminal state — `SHIP`, `BLOCKED`,
   `NEEDS_HUMAN`, or any error abort (the commit-gate failure in step 4, an
   interrupt, or any abnormal end of this coordinator's turn) — and runs
   BOTH:
   a) `sys_session_close` on every id tracked in step 6, **excluding the
      two registration-anchor bootstrap conversation ids from Preflight
      step 3, which must never be closed or pruned**. Close is a
      tombstone+interrupt; it does not free RAM.
   b) as a guaranteed backstop,
      `trioctl omnigent sessions prune --include-sub-agents --mailbox <dir>`.
      The broker DELETE prune performs is the only path that kills the
      dedicated tmux terminals; the `trioctl <mailbox.name> ` title prefix
      from step 2 is what prune matches, and the anchor sessions keep
      titles WITHOUT that prefix so prune leaves them alone. Run (b) after
      (a), regardless of whether the closes succeeded.
   Do this after the terminal verdict (and, on SHIP, the retirement commit)
   is written, before ending the turn. If a close or prune step fails
   (session busy, already closed, etc.), report it and continue the rest —
   cleanup is best-effort and must never change the loop's verdict.

   Abort/orphan path: if a Lead or Evaluator role session ends `failed` —
   including a `failed` status with "connection to runner lost" after the
   coordinator Omnigent runner's 1h idle timeout — do NOT re-implement the
   iteration. Treat it as an abort: run the prune backstop above, then
   re-create that role session with a fresh title under the same locked
   scheme (step 2) so it can finish the iteration from the committed tree.
   The provider-transport auto-wake path (`resource_exhausted` /
   `NGHTTP2_INTERNAL_ERROR` / `stream refused`, below) is a separate,
   retry-once path and is NOT this abort path.

`sys_session_create` is asynchronous. Use inbox/session history tools and end
the turn while a role is running; Omnigent wakes this session on completion.
Do not busy-poll.

If a role session's result is `failed (exit N)` and the broker log shows a
provider transport error — the observed triggers are `resource_exhausted`,
`NGHTTP2_INTERNAL_ERROR`, or `stream refused` — auto-wake the named session
ONCE via `hub send` with the message `continue` before surfacing any failure
to the user. A second failure, or no live session to wake, is a real
failure: surface it.

Default to repeated iterations until SHIP/BLOCKED/NEEDS_HUMAN, with NO user
checkpoint between Lead completion, Evaluator dispatch, and the
verdict-driven next iteration — only a terminal verdict stops the chain
(the compact per-iteration digest is still posted after each verdict).
If the user explicitly asks for one supervised iteration, stop after one
verdict.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence, plus the task-specific checklist) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
- Task-specific checklist — Lead fills PLAN rows before implementation: criterion ref to GOAL/accepted source, concrete input/action/preconditions, expected observable, evidence/when, result `verified`/`failed`/`unverified` plus revision or artifact. Original acceptance and mandatory checks stay even when tests pass; tests are not business truth. Tiny low-impact changes stay proportionate. Optional `AGENTS.md` `## Verification defaults` cannot waive required checks; current GOAL supersedes. Remaining unverified GOAL criteria block whole-goal SHIP. Classify unavailable environment vs product failure. `verify: human` stays NEEDS_HUMAN unless a current HUMAN.md answer reports its check (Human answers, below). Keep exact existing verdict first-lines.
- Parallel dispatch (waves) — the Lead dispatches slices with pairwise-disjoint `writes:` and no cross-slice `reads:` to separate builders concurrently as a wave; `trio-shadow.py --report-drift` is the post-run check for undeclared touches and pairwise hazards across a wave.
- Human answers (only when `loop/HUMAN.md` exists) — the append-only answer channel after a NEEDS_HUMAN/BLOCKED stop; no agent edits it. Only server-written entries count (header `## <UTC time> — answer <id> — iteration <N> — trio-dash <sig>`, text as `> `-quoted lines). The newest entry whose N is the iteration that just stopped binds the next pass (the Lead applies it; STATE.md `human_answer:` names it) and is evidence for any `verify: human` criterion whose `## Human check` result it reports (the Evaluator grades that criterion from it instead of NEEDS_HUMAN); older entries are informational.
- Session sidecar — at iteration start, wrappers write `loop/.session.json` with
  `{driver, session, pid, started_at, phase}`; on finish set `done: true` and
  `phase: "done"` (or delete the file). `pid` is the orchestrator process;
  the dashboard treats a dead-pid sidecar as orphaned, not running.
- Open-loop extension (optional, gated on `loop/QUEUE.md` existing; absent → unchanged lockstep behavior above) — `QUEUE.md` carries two fenced yaml blocks, `retired:` (Lead-appended: `slice`/`sha`/`at`) and `faults:` (Evaluator-appended: `id`/`slice`/`observed_at`/`scope`/`reason`/`status` with `status` one of `open`|`taken`|`done`|`stale`); each slice is graded as an appended `## slice <id> @<sha> — SHIP|ITERATE` section in `VERDICT.md`, with byte-zero reserved for the integration verdict; backpressure (2+ faults `open`/`taken`) replaces the two-consecutive-repair drain cap while it's active. The automated driver, `python3 metrics/trio_loop.py run --mailbox <dir> --max-iterations N`, auto-selects open-loop when `QUEUE.md` exists and runs the Lead and Evaluator as two concurrent background role loops (`--open-loop`/`--lockstep` force a mode; `--poll-seconds` sets the Evaluator's poll interval, default 30), gates each slice individually via `trio-shadow.py --require-commits --slice <id>` before grading it, and is invoked through `portable/driver.sh` by exporting `TRIO_MODE` and `POLL_SECONDS`.
- Original-goal planning — preserve GOAL.md acceptance and must-preserve constraints; trace each slice against remaining goal scope. If `knowledge.yaml` or `.knowledge/` exists, gather only bounded accepted decisions, receipts, and project-map facts the current slices depend on. Missing knowledge is not a blocker and must not be invented. Proposals are never accepted authority. Stale knowledge blocks only work that declared that dependency. Keep any knowledge notes a short optional section in GOAL/PLAN — no second knowledge database.
- Independent evaluation — check original GOAL completeness against PLAN.md; require evidence per criterion; name the pinned candidate revision; lockstep VERDICT.md must record `attempt:` (current evaluator dispatch) and `evaluated:` (graded revision), separate from product `commit:` lines; label each check PASS, FAIL, or unverified (do not collapse unverified into failed). Remaining unverified GOAL criteria block whole-goal SHIP. Classify unavailable environment vs product failure. Implementer-authored tests are not the sole oracle. A slice passing does not close the whole GOAL. Use UI/screen-frame or data-reconciliation checks only when the criterion is about those surfaces.
- Verification rigor — run every check yourself and prefer executing code over reading it; no SHIP (slice section or whole-goal) unless the verdict lists what you actively tried to break; `profile: data` work (or a diff touching SQL, pipelines, notebooks or dataframes) is graded on reconciliation against the source and your own re-run, never on unit tests alone.
- Commit ownership (driver) — `trio-shadow.py --require-commits` is the product `slice(<id>):` gate before Evaluator. Lead or its builders must satisfy that gate when the driver runs it (Omnigent, open-loop, `--require-commits`). Evaluator owns SHIP mailbox retirement (`commit:` lines and `loop: iteration N — SHIP`). A wait timeout or runner exit 0 is not product SHIP. Missing retirement is recoverable finalization, not shipped.
<!-- trio-protocol:end -->

## Headless

`trioctl omnigent loop` runs unattended iterations over the broker's HTTP API:
```bash
trioctl omnigent loop --mailbox loop/ [--max-iterations N] [--wait-timeout S] \
  [--host-id ID] [--runner-id ID] \
  [--no-isolate-workers] [--slice-eval-concurrency N] [--worktree-root DIR]
```

Plain `trioctl omnigent loop --mailbox <m>` is the fast path: worker
isolation (each builder in a task-owned git worktree merged back on exit,
each open-loop slice-eval in a detached worktree at its pin, accepted
worktrees removed before and after the loop) and up to 4 concurrent
slice-evals are ON by default for open-loop mailboxes (`QUEUE.md`
present). A lockstep mailbox (no `QUEUE.md`) keeps isolation OFF and
slice-evals serial unless `--isolate-workers` is given (lockstep with
isolation has not been qualified live). Worktrees live outside the repository under
`$TRIO_WORKTREE_ROOT`, else `$XDG_STATE_HOME` (or `~/.local/state`)
`/trio-agent-loop/worktrees/<repo>-<hash>`; the loop prints
`trioctl: worktree root <path>` once.

On an open-loop mailbox inside a git checkout (the root-free case; see
below), isolation is mandatory: `--no-isolate-workers` is refused (exit 2,
"a root-free open-loop needs isolated builders", nothing changed). Slice-eval
concurrency can still be turned down with:

- `--slice-eval-concurrency 1` (serial slice-evals, isolation kept)
- `--worktree-root DIR` overrides the worktree location.

Lockstep is unaffected (isolation is off there by default already) and still
accepts `--no-isolate-workers`/`--observe-workers` explicitly, as does an
open-loop mailbox that is outside any git checkout (root-free has nothing to
fork there, so isolation keeps its pre-r16b fallback behavior).

An open-loop mailbox's unmet default prerequisite (not a git branch
checkout, `--observe-workers`, session-bound Omnigent bindings in the
user's Cursor config) is a start-time refusal now, not a fallback: exit 2,
nothing changed (before r16b it fell back to non-isolated/root-bound). A
vendored `metrics/trio_loop.py` older than r10 still falls back to serial
slice-evals with one stderr line (refresh `metrics/` to enable) — unrelated
to isolation, unaffected by this change. An explicit flag never falls back:
`--isolate-workers` (the lockstep opt-in, otherwise a no-op) and
`--slice-eval-concurrency N>1` are refused instead, including on a detached
HEAD.

Resolution by mailbox mode:

| mailbox | plain `loop` | `--isolate-workers` | `--no-isolate-workers` |
|---|---|---|---|
| open-loop (`QUEUE.md` present), in a git checkout | isolated, N=4 | isolated, N=4; unmet prerequisite refused | refused, exit 2 ("a root-free open-loop needs isolated builders"); nothing changed |
| lockstep (no `QUEUE.md`) | not isolated, N=1, one line `trioctl: lockstep mode: worker isolation stays off by default (open-loop is the fast path; pass --isolate-workers to opt in)` | isolated (opt-in; concurrency not applicable, the loop says so); unmet prerequisite refused | not isolated, N=1 |

Dirty checkout: an isolated builder dispatch (`run builder --isolate`)
refuses (exit 1, no worktree) on uncommitted or untracked files under a
declared product path (any PLAN.md slice's `writes:`) and on modified
tracked files outside them ("commit or stash YOUR change to <file>; the
Lead must not commit files it did not edit"). Other untracked files and
other mailboxes (a directory with GOAL.md + STATE.md, such as a concurrent
`loop-auth`) do not block; they are listed once as
`trioctl: ignored (not product paths; the worker worktree will not see them): ...`.
The Lead commits only files it or its builders edited under declared
`writes:`; on any other blocker it stops the pass (LOG
`- iter N | lead | blocked: uncommitted foreign changes: <files>`, STATE
`status: needs_human`).

Design and speed evidence:
`docs/CONCURRENT-SLICE-EVAL.md`, `docs/ISOLATED-WORKERS-QUALIFICATION.md`.

Root-free (r16; lockstep too since r16b): every `loop` (open-loop and
lockstep) runs in the loop's own Lead worktree on branch `trio/<mailbox>`
and lands onto the root's branch only after SHIP, so the root `loop/<x>`
holds the pre-land copy while it runs. Watch a run with
`trioctl omnigent status --mailbox loop/<x>` (or read STATE.md/LOG.md at
the live mailbox path it prints), not the root mailbox. `--root-bound` was
removed in r16b (exit 2, "root-bound mode was removed in r16b", nothing
changed — use `trioctl omnigent land`/`abandon` instead). A root checkout
that is detached with no `--target` is refused the same way (exit 2). Guide:
`docs/ROOT-FREE-OPEN-LOOP.md`.

The command owns the mailbox lock and manages verdict parsing, repairs (max 2
consecutive scoped repairs), resume state, and exit codes. `--wait-timeout`
defaults to 3600 seconds. Session `wait` is not role completion: a pass
finishes only after a `running`→`idle` edge held for
`TRIO_OMNIGENT_IDLE_DWELL` (default 30s; skip with interval `0` in tests)
and the mailbox artifact above exists, or `--wait-timeout` expiry.
Distinct outcomes: exit 0 (SHIP: verified and landed), 2
(a `VERDICT: BLOCKED` from a role, **or** a start refusal before anything
ran — `--root-bound`, an open-loop mailbox's `--no-isolate-workers` /
`--observe-workers` / inherited session-bound Cursor config, a detached
root without `--target`, or a `writes:` overlap; distinguish by reading
stderr/LOG.md — a start refusal never dispatches a role and always says
"nothing was changed/created"), 3 (bad verdict / error), 4 (iteration cap), 5 (NEEDS_HUMAN or
mailbox lock held), 6 (needs_retirement), 7 (held dispatch), 8
(needs_land: SHIP verified but not landed — surface STATE `phase:` and
the LOG reason to the user; after the user fixes the cause, run
`trioctl omnigent land --mailbox loop/<x>`). Env/flag
precedence: `--host-id` over `TRIO_OMNIGENT_HOST_ID`, `--runner-id`
over `TRIO_OMNIGENT_RUNNER_ID`.

`trioctl omnigent loop` writes `.driver.json` in the (live) mailbox with the PID, iteration
count, phase (`idle` / `lead-done` / `eval-done`), and Lead/Evaluator session
IDs. This is the authoritative resume cursor; killing the process and re-running
continues where it left off.

Alternatively, the supervised chat-coordinator procedure (one iteration per
`sys_session_create` in the current session, with inbox polling between roles)
remains available for interactive debugging or custom iteration logic.

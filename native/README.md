# trio-native (v0, lockstep)

A Claude-native Trio loop driven by a saved Claude Code **Workflow** script,
with no Omnigent, broker or Cursor dependency. Design:
`workflow-lab/.runtime/parallel-worktree-isolation/native/DESIGN.md` (§3, §4 v0).
Live probes: `…/native/PROBE-REPORT.md`.

| file | role |
|---|---|
| `trio-native.js` | The Workflow script. It only sequences: Lead pass (plan → driver-owned builder waves → integrate) or Repair on a scoped ITERATE → commit gate (retry the role once) → pin → Evaluator → apply → next iteration, or stop on SHIP / BLOCKED / NEEDS_HUMAN / error / held / `max_iterations` (or an opt-in `max_agents` / `token_budget`). |
| `trio_native_step.py` | Stdlib helper with ops `begin`, `next`, `dispatch`, `builders`, `cleanup`, `gate`, `pin`, `apply` and `end`. The loop ops are thin calls into `metrics/trio_loop.py` (`_commit_gate`, `_log_gate`, `_first_verdict`, `_lockstep_eval_context`, `_fresh_evaluator_artifact`, `_apply_verdict`, `_finalize_ship`, `_update_state`) with the same STATE.md `phase` cursor as `trio_loop._run_lockstep`, so either driver can resume the other's mailbox. It emits one JSON line and echoes the caller's nonce. |
| `agents/trio-step.md` | A Bash-only step agent. It runs exactly one helper command and returns its stdout verbatim as a string; the script parses it. |
| `launch.sh` | Lab launcher: one headless session that only launches the workflow (`start`) or resumes it in its own session (`resume`), and prints the parsed result JSON. |
| `tests/` | Real-git fixture tests for every op, static checks of the script, a node harness that stubs `agent()`, and launcher tests against a fake `claude`. |

## Install (user scope; nothing here does it for you)

```bash
REL="$HOME/.local/share/trio-agent-loop/releases/$(cat ~/.local/share/trio-agent-loop/CURRENT)"
mkdir -p ~/.claude/workflows
ln -sfn "$REL/native/trio-native.js"      ~/.claude/workflows/trio-native.js
ln -sfn "$REL/native/agents/trio-step.md" ~/.claude/agents/trio-step.md
```

- Roles use the regenerated `~/.claude/agents/trio-{lead,evaluator,repair,builder,scout}.md` from this release (r18a prompt pack, Opus pin `claude-opus-5-5`). The repair and evaluator prompts changed in the eval-native-v0 fixes (F5), so the agents and skills must be refreshed from the release.
- By default the script finds the helper at `<release CURRENT>/native/trio_native_step.py`. To run from a checkout, pass `args.helper` (an absolute path).
- `begin` adds `.claude/worktrees/` and the build/test artefacts that are never product (`__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `node_modules/.cache/`, `node_modules/.vite/`; not `node_modules/` itself) to the product repo's `.git/info/exclude` (idempotent; the file is in the common git dir, so it covers every builder and Evaluator worktree), so a role's pytest run neither blocks `cleanup` nor trips the SHIP retirement check (probe 2 blocker A). `launch.sh` also exports `PYTHONDONTWRITEBYTECODE=1`. It adds the driver's runtime files (`.native.json`, `.session.json`, `.lock/`, `.native-launch.json`, `.native-runs/`, plus trioctl's list) to `<mailbox>/.gitignore` (append-only).

## How a Lead pass runs

Workflow subagents have **no Agent tool** (probe P4), so the driver owns the builders:

1. **Plan** — one `trio-lead` call with a schema. The Lead updates PLAN.md and returns the iteration's code-changing slices (`id`, `brief`, `writes`, `reads`, `depends`). No product code.
2. **Waves** — the script groups slices deterministically: a slice joins the earliest wave after its in-plan `depends`, and only if its `writes` are disjoint from the others in that wave (a slice with no product `writes` runs alone).
3. **Per wave:**
   - `dispatch` returns the Lead's HEAD;
   - one `trio-builder` (Sonnet) per slice runs with `isolation: 'worktree'`, concurrently within the wave, and reports `worktree`, `branch`, `base`, `head`, `commits`, `targeted_check`, `summary`;
   - the script refuses a builder whose `base` is not that HEAD, and `builders` re-checks on git (the branch must contain the HEAD and must not commit `loop/`) and writes each builder's `- iter N | builder | <id>: …` LOG line — builders never write LOG.md or commit `loop/`;
   - a `trio-lead` **integrate** call merges each branch (`git merge --no-ff --no-edit`; a conflicting merge is aborted and the next branch merged), reviews and corrects, and returns `{merged, conflicts: [{id, branch, files}], summary}`; the last wave also rewrites REPORT.md and appends the `| lead |` LOG line, unless a merge conflicted;
   - **conflicts** (probe 2 blocker B): git decides — a builder branch that `cleanup` finds "not merged into HEAD" conflicted (the Lead's `conflicts` only adds the files). Each conflicting slice is re-dispatched **once** as a new single-builder wave forked from the Lead's post-merge HEAD, with the conflict files added to its `writes`; that wave's `cleanup` drops the superseded branch (`--drop-unmerged old=new`: only once `new` is merged, only builder branches, force-deleted, with the same dirt rule for its worktree). A slice that conflicts again stops the run with `status: "conflict"`, the `conflicts` list and a reason naming the slices and files (STATE stays `lead-running`; a fresh run re-plans). Each pass records `conflicts` and `kept` in `iterations[]`;
   - `cleanup` removes the merged worktrees and deletes the branches. It forces the removal only when the only dirt is `loop/` residue or untracked build artefacts; a tracked change or any other untracked file keeps the worktree (ignored files never block a removal).
4. **Gate** — `_commit_gate` + `_log_gate` as in `trio_loop`, plus: REPORT.md must have been rewritten in this pass. A failure retries once with one solo Lead call, which is told the builder branches `cleanup` kept (with the reason); `cleanup` runs on them again after that call (eval-native-v0b N5). A plan with no slices is also one solo Lead call.

The plan call is told that `writes` must list every shared file a slice edits (registries, `__init__.py`, config, lock files), because `writes` alone decides concurrency.

Roles write mailbox files with Bash heredocs: the harness refuses report-file `Write`s from workflow subagents (probe P5). The Evaluator creates any pin worktree under `.claude/worktrees/eval-*`, and `end` removes those.

## Launch

Use the lab launcher from anywhere (it runs Claude Code in the mailbox's repo):

```bash
native/launch.sh start --mailbox /abs/path/to/repo/loop --max-iterations 4 [--helper /abs/…/trio_native_step.py]
```

It runs `claude -p '<prompt>' --session-id <new uuid> --model claude-opus-5-5 --permission-mode auto --settings '{"worktree":{"baseRef":"head"}}' --output-format json`:
- `--settings '{"worktree":{"baseRef":"head"}}'` is **required**. Without it, isolated builders fork from `origin/HEAD` (probe P4), and every builder is refused. It is a command-line flag only; no settings file is edited.
- The prompt ends with "Launch only; do not edit files, settings or permissions; output the result JSON verbatim in one fenced block and stop." The launcher parses that fenced block and prints it, adding `launcher: {session_id, exit_code, raw}`.
- The session id and the exact args JSON are recorded in `<mailbox>/.native-launch.json`; raw outputs go to `<mailbox>/.native-runs/`.

From an interactive session the equivalent call is `Workflow({name: "trio-native", args: {mailbox: "/abs/path/to/repo/loop", max_iterations: 4}})`, provided that session was started with the same `--settings`.

Optional args:
- `run_token`: the lock owner id. It defaults to a slug of the mailbox path. A second launch on the same mailbox is refused while the first is alive, whether it uses the same token or a different one (see **Lock**).
- `models`: `{lead, evaluator, builder, repair, step}`. The defaults are `claude-opus-5-5` for lead and evaluator, and `claude-sonnet-5` for builder, repair and step.
- `helper`: an absolute path to the helper.
- `max_agents`, `token_budget`: opt-in caps. See **Caps**.

The result is `{status, verdict, code, reason, iteration, commit_shas, conflicts, human_check, retirement_fold, held_step, end_error, iterations[], agents_used, lock, dangling_worktrees, eval_worktrees_removed}`. `iterations[]` records each pass's slices and waves. The launching session:
- surfaces NEEDS_HUMAN (`human_check`) and BLOCKED;
- announces the SHIP `commit_shas`;
- queues the one post-SHIP documentation task (CLAUDE.md policy).

## Resume

Workflow journals live under the **launching session's** directory, so a journal resume only works inside that session (probe P6).

- **After a kill or crash mid-run:** `native/launch.sh resume --mailbox … --run-id wf_…` runs `claude -p --resume <recorded session id>` and asks for `resumeFromRunId` with the byte-identical recorded args. The unchanged agent-call prefix is replayed from the journal; the first live step re-stamps the lock pid. A journal resume from a *new* session fails ("journal … is not on disk"): use a fresh run instead.
- **After `held`, `error` or `budget` (or any run that reached `end`):** start a **fresh run with the same args** (`launch.sh start`). A journal resume of an ended run is a 100% cache hit and replays the same stop without doing any work (probe P8).
- **Fresh run:** the same `run_token` re-enters the lock once the old holder is dead, and `next` re-derives the step from STATE.md:
  - `*-running` re-runs that role at the recorded gate attempt (an interrupted Lead pass re-plans; builder worktrees of the killed pass are left for `dangling_worktrees`);
  - `lead-done` goes to the Evaluator, which is skipped when VERDICT.md is already bound to the pin;
  - `needs_retirement` rechecks finalization only.
- **Builder worktrees on a journal resume** (probe 2 finding C). The harness names isolated worktrees itself (`<runId>-<n>`), so a journal resume re-runs a killed builder in the **same** worktree, with the killed builder's uncommitted files and any commits. The script cannot pick a fresh name, so every builder treats leftovers as untrusted: when HEAD is the dispatch HEAD or a descendant and the tree is dirty or ahead, and only when its `pwd` is inside `<repo>/.claude/worktrees/`, it runs `git reset --hard <dispatch HEAD> && git clean -fd` (ignored artefacts are kept) and starts over. A HEAD that does not contain the dispatch HEAD is still reported as a wrong base, never reset. Only a *fresh* run leaves a killed pass's worktrees to `dangling_worktrees`.
- **Idempotency and its one window.** `gate`, `apply` and `builders` record their answer in `<mailbox>/.native.json` and return it on a re-run; `next` resumes a `*-running` phase without bumping; `pin` reuses the persisted attempt and sha. The record is written *after* the side effect, so a crash in the milliseconds between `gate`/`apply` writing STATE/LOG and writing the record is not replayable: the re-run then returns `ok: false` ("STATE is … not …") and the run stops with `error`. No LOG line or `.repairs` bump is duplicated and nothing is skipped; a fresh run's `next` continues correctly. The realistic trigger is the 600 s Bash kill during a long `_finalize_ship` wait, so keep `TRIO_RETIREMENT_WAIT_SECONDS` well below 550.
- **Interop:** `python3 metrics/trio_loop.py run --mailbox <dir> …` can finish a mailbox this workflow left behind, and the reverse also works. trio_loop checks only the lock pid: it takes over only after the workflow's Claude process has died (or `end` released the lock), never because the heartbeat is stale.

## Caps

By default there is **no agent cap and no usage budget** (user decision, 2026-09-29).

- `max_iterations` (default 4) is the loop's normal bound. It is enforced by the helper's `next`, with trio_loop's semantics: a new pass is refused at the cap, and an interrupted pass may still finish.
- `max_agents` is opt-in and enforced by the script only when passed. Every `agent()` call counts, including step agents and builders. One agent is reserved for `end`, so the lock is released. On exhaustion the status is `budget`, and the state is resumable.
- Agent count per iteration, for sizing `max_agents`:
  - A clean one-slice Lead iteration costs 11 agents: `next`, plan, `dispatch`, builder, `builders`, integrate, `cleanup`, `gate`, `pin`, the Evaluator and `apply`. Each extra slice in a wave adds 1; each extra wave adds 4 (`dispatch`, `builders`, integrate, `cleanup`) plus its builders. A repair iteration costs 6. `begin` and `end` add 2 per run.
  - A gate retry adds 2 agents, and a step re-run (nonce mismatch or missing keys) adds 1.
- `token_budget` is opt-in: output tokens, from `budget.spent()`. It stops before the next non-`end` agent once it is reached, with status `budget`.
- A user `+Nk` directive stays the runtime's own hard ceiling.

## Permissions (unattended runs use auto mode)

Unattended or headless runs use Claude Code **auto mode** (`--permission-mode auto`, via `launch.sh`). Nothing in this directory uses or needs any permission-skipping flag.

- **A denied step is held, not retried.** If the permission system denies a step's Bash call, the `trio-step` agent returns `held: true` with the harness's denial text. The workflow then:
  - stops with `status: "held"` and `held_step: "<op>"`;
  - still runs `end`, unless `end` itself is denied (then `held_step: "end"`, `end_error`, `lock: "not_released"`);
  - leaves STATE.md at its resumable cursor.
- **A step agent that declines on its own** (no harness denial text) is `status: "error"`, not `held` (probe P3).
- **Role agents handle their own denials.** A denial inside a role agent (Lead, Evaluator, builders) is handled by that agent. If it leaves the gate unmet, the gate retries the role once and then stops with `error`, so it never loops.

## Lock

- The mailbox `.lock` uses trio_loop's on-disk protocol. Its owner is `workflow:<run_token>`.
- The `pid` recorded in the lock is the *holder pid*: the Claude Code process running the workflow, found as the nearest ancestor of the step shell whose `/proc/<pid>/comm` is `claude` (`TRIO_NATIVE_HOLDER_PID` overrides it). So `trio_loop.py` and other drivers see a live owner.
- Every op re-stamps both `pid` and `heartbeat`. A journal resume in a new Claude process replays `begin` from its cache, so its first live op records the new holder pid.
- **Same token:** the lock is taken over only when the recorded pid is dead (a crash, or a resume in a new process). While it is alive and is not this run's holder pid, `begin` and every later op are refused, and `end` leaves the lock alone. Two concurrent launches with the default token therefore cannot both drive the mailbox.
- **Different workflow token:** it takes over when the pid is dead, or when the heartbeat is older than `TRIO_NATIVE_LOCK_STALE_SECONDS` (default 4 h). The heartbeat only moves when an op runs, so a single role pass longer than that can be taken over. The original run then fails closed at its next op ("lock not held").
- **trio_loop / trioctl** check only the pid. They take over only when the holder pid is dead, never on a stale heartbeat.

## Live probe status

The 2026-09-29 probes (`PROBE-REPORT.md`) answered P0–P9. The blockers they found are addressed in this tree: driver-owned builders, the `baseRef` launcher flag and base check, raw step stdout with per-op keys, the per-session resume contract, `cleanup`, heredoc mailbox writes with the REPORT gate, the SHIP state fold and eval worktrees, and the launcher. Still to confirm live: a two-builder wave through `dispatch → builders → integrate → cleanup`, the REPORT gate on a real Lead, and the fold on a real retirement commit.

## Known gaps (v0)

- **Lockstep only.** A mailbox with `QUEUE.md` is refused. The v1 items are not built: open-loop, slice-evals, root-free Lead worktree and land.
- **Multi-repo.** r15 foreign repos are pinned (the `pins=` in LOCKSTEP CONTEXT), but builders only get worktrees of the home repo, and there is no multi-repo prompt note yet.
- **r18a mechanics.** There is no kill check. `trio-check` runs in `gate` as an advisory report only; blocking is `_commit_gate` + `_log_gate` (as in `_run_role`) plus the REPORT rewrite check.
- **Not enforced by the driver.** Some skill-prose rules are not enforced, which matches `trio_loop`:
  - the plateau rule;
  - the REPORT builder-provenance retry;
  - the mission collision check.
- **Merges are the Lead's.** The driver verifies builder branches and removes merged worktrees, but the merge itself is the integrate call's; the gate still requires every code-changing slice commit on HEAD.
- **Unbound verdicts (differs from trio_loop).** `apply` refuses a VERDICT.md that is not bound to the current iteration, `evaluator_attempt` and pinned sha (for example iteration 1's ITERATE left over when iteration 2's Evaluator wrote nothing). The run stops with `status: error` and a `not bound` reason, and nothing is written: STATE stays `lead-done`, so a fresh run re-dispatches the Evaluator. `trio_loop.run_loop` still re-applies such a verdict; that is a recorded follow-up in the loop core, not changed here.
- **LLM-mediated steps.** Step agents are LLM-mediated. The script parses their raw stdout and checks the nonce and the op's keys, and all ops are idempotent, so a re-run is safe; a step agent could still fabricate a well-formed answer.
- **Global CLAUDE.md.** It is injected into every workflow agent. The role prompts carry a per-call "router policy does not apply" clause; the agent bodies do not carry it yet.
- **Leftover builder worktrees.** `cleanup` removes merged ones; unmerged or product-dirty ones (and those of a killed pass) are reported in `dangling_worktrees` and never removed.

# trio-opencode

A standalone Trio loop driver for [OpenCode](https://opencode.ai): a Python
state machine that spawns one `opencode run` process per role turn (Lead,
Builder, Evaluator, Repair, Scout), functionally equivalent to `native/`
(the Claude-native Workflow driver) — same mailbox protocol, same
gate/pin/apply/SHIP semantics, same root-free Lead worktree — but driving
`opencode` subprocesses instead of Claude Code `agent()` calls, with **no
Omnigent dependency** (no broker, no `trioctl`, no registry import).

It reuses `native/`'s loop core rather than re-implementing it:
`trio_opencode/steplib.py` loads `native/trio_native_step.py` (and, through
it, `metrics/trio_loop.py`) as a **private module copy** with its globals
overridden (`DRIVER`, `RECORDS`, `RESULT`, ledger/worktree directory names,
the lock owner prefix) so it acts as this driver's own runtime on the same
`STATE.md` cursor a native or `trio_loop.py` run would use. Before any op
runs, `TL.owned_residue_check` is monkeypatched to `lambda repo, rel: False`
so `trio_loop`'s residue check never lazily imports
`omnigent/worker_worktrees.py` — the one place the shared core would
otherwise reach into Omnigent. A test asserts no module whose `__file__` is
under `omnigent/` is ever in `sys.modules` after a full run.

## 1. File table

| file | slice | role |
|---|---|---|
| `trio-opencode` | — | bash shim: `exec python3 .../trio_opencode/cli.py "$@"` |
| `config.example.json` | — | annotated example config |
| `trio_opencode/cli.py` | driver | argparse: `start\|resume\|status\|abandon\|land\|doctor` |
| `trio_opencode/config.py` | config | load/validate config, defaults, placeholders |
| `trio_opencode/ocgen.py` | config | per-run isolated OpenCode config dir generator |
| `trio_opencode/doctor.py` | config | `doctor` environment/config checks |
| `trio_opencode/prompts.py` | driver | per-role prompt text |
| `trio_opencode/runner.py` | runner | one role turn = one `opencode run` process; timeouts, retries |
| `trio_opencode/events.py` | runner | NDJSON event parser/classifier |
| `trio_opencode/steplib.py` | driver | adapter over `native/trio_native_step.py` |
| `trio_opencode/waves.py` | driver | wave planning / conflicts, ported 1:1 from `native/trio-native.js` |
| `trio_opencode/rootfree.py` | driver | root-free Lead worktree: prepare/land/teardown/abandon, declared-repo (`repos:`) aggregates |
| `trio_opencode/driver.py` | driver | the state machine |
| `trio_opencode/openloop.py` | open-loop | open-loop mode: settings resolution, `OpenLoopRunner` (Lead + Evaluator role runner), STATE guard, sidecar writer, root-free land hook, `drive()` |
| `trio_opencode/olprompts.py` | open-loop | per-call open-loop prompt templates (`lead-plan`, `lead-review`, `builder`, `slice-eval`, `integration-eval`) |
| `trio_opencode/quality.py` | open-loop | r18a shadow quality telemetry: `TARGETED_CHECK:` parsing, base-revert kill check, evidence/lint helpers |
| `trio_opencode/olqueue.py` | open-loop | `QUEUE.md` atomic `retired:`/`faults:` writer and `QueueGuard` concurrent-rewrite guard |
| `tests/scenarios/ol_*.py` | — | open-loop fake-opencode scenario behaviour |
| `tests/fake_opencode.py` | runner | fake `opencode` executable, installed on `PATH` by tests |
| `tests/scenarios/*.py` | — | per-test fake behaviour (`handle(ctx)`) |
| `tests/conftest.py` | — | HOME/XDG isolation, real-binary guard |
| `tests/test_*.py` | — | unit + end-to-end tests |

## 2. Install / config

**Requirements**: OpenCode **v2.0.20** is the primary target; v1.18.x works
via runtime feature detection of `opencode run --help`. Python >= 3.10,
git >= 2.30 (`doctor.py`'s version floors).

**Config path & precedence** (`config.load_config`): an explicit `path`,
then `TRIO_OPENCODE_CONFIG`, then
`${XDG_CONFIG_HOME:-~/.config}/trio-opencode/config.json`, else built-in
defaults. A missing file at the env/XDG/default path silently falls back to
defaults; an explicit `--config` path that does not exist is a hard error. A
`key`/`api_key`/`apiKey` field anywhere in the file is a hard load error.

**`config.example.json` fields**:

| field | meaning |
|---|---|
| `opencode_bin` | the `opencode` executable name/path |
| `models.{lead,evaluator,acceptance,builder,scout,repair}` | one `provider/model` id per role |
| `variants.{lead,evaluator,builder,scout,repair}` | optional `#variant` suffix (v2 only; `null` = none) |
| `variants.acceptance` | optional, informational only (the acceptance tier is authored by the Lead turn — nothing reads this value for dispatch); when set alongside `variants.lead`, `config.validate()` requires it to equal `variants.lead` |
| `provider.id` | provider id registered in the generated config (unused for the two built-ins) |
| `provider.key_file` | path to the plain-text API key file |
| `provider.key_env` | child-env variable name the key is passed as (`OPENCODE_API_KEY`) |
| `timeouts.turn_seconds` / `idle_seconds` / `evaluator_turn_seconds` | wall-clock / no-stdout / Evaluator wall-clock timeouts; `turn_seconds`/`evaluator_turn_seconds` accept `0` or `null` to disable the wall-clock limit (see "Container / no-time-limit mode") |
| `retries.max_attempts` / `backoff_seconds` / `idle_retry_unlimited` | transient-retry budget and backoff schedule; `idle_retry_unlimited` (default `false`) retries an `idle_timeout` forever instead of counting it against `max_attempts` |
| `max_iterations` | the loop's normal iteration cap |
| `root_free` | default isolation mode (`true`; `--in-place` overrides per run) |
| `container_mode` | default `false`; relaxes generated permissions for running inside a disposable Terminal-Bench task container (see "Container / no-time-limit mode") |
| `acceptance_wait_seconds` | open-loop only; default `null` (= `0` = **no time limit**): how long the Lead pass waits for the frozen-acceptance author. A number bounds it; hitting the bound degrades the run to no pack (see "Author failure degrades") |
| `acceptance` | default `false`; r19 frozen acceptance (see "Frozen acceptance (r19)"); `--acceptance`/`--no-acceptance` (start only) and `TRIO_ACCEPTANCE=1\|0` override it, precedence CLI > env > config |

**Default models**: lead/evaluator/acceptance = `opencode-go/deepseek-v4.1-flash`;
builder/scout/repair = `opencode-go/glm-5.3-flash`. `config.validate()`
enforces lead/evaluator/acceptance are the **identical** model id — the
acceptance author must never be cheaper than the tier that plans and judges.
**Model aliases** (r21+): `"opus"` and `"sonnet"` are accepted and resolved
at `begin` time to their respective concrete model ids (e.g., `"opus"` →
`"opencode-go/deepseek-v4.1-flash"`); the dashboard also accepts legacy
exact-pin ids (e.g., `claude-opus-5-5`).

**Key setup**: `provider.key_file` (default `~/Documents/OpenCodeKey.txt`,
plain text, key only, `chmod 600` recommended — `doctor`'s `key_file` check
warns if group/other-readable). `runner.py` reads it **only at spawn time**,
strips CR/LF, and passes it **only** as `OPENCODE_API_KEY` in the child
environment — never in argv, never logged, never persisted anywhere. Every
per-attempt log line is scrubbed (`api_key`/`apikey`/`authorization`,
case-insensitive, or the literal key value → `[redacted]`).

**Isolation**: each run gets a generated OpenCode config dir plus a
per-loop XDG tree (`ocgen.generate`) passed to every turn: `OPENCODE_CONFIG`
(`<run_dir>/opencode/opencode.json`), `OPENCODE_CONFIG_DIR`
(`<run_dir>/opencode`), `OPENCODE_DISABLE_PROJECT_CONFIG=1`,
`OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`,
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`,
`XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME` under
`<run_dir>/xdg/*` (cache may be a shared per-user dir instead), plus
`OPENCODE_DISABLE_SHARE=1` (v1 only) or `OPENCODE_CONFIG_PROJECT_DISABLE=1`
(v2 only — v2 disables sharing via the config's own `"share":"disabled"`
instead). The user's real `~/.config/opencode` and any project `opencode.json`/
`.opencode` are never loaded (disabled project config + isolated
`XDG_CONFIG_HOME`). `XDG_DATA_HOME` is per-loop, not per-turn, so
`--session`/`-s` continuation works across a loop's turns.

## 3. Usage

```
trio-opencode start   --mailbox <dir> [--max-iterations N] [--config PATH] [--run-token TOK] [--in-place]
                      [--isolate-workers | --no-isolate-workers] [--slice-eval-concurrency N]
                      [--slice-eval-drain-seconds S] [--no-kill-check]
trio-opencode resume  --mailbox <dir> [--max-iterations N] [--config PATH] [--run-token TOK] [--in-place]
                      [--isolate-workers | --no-isolate-workers] [--slice-eval-concurrency N]
                      [--slice-eval-drain-seconds S] [--no-kill-check]
trio-opencode status  --mailbox <dir>
trio-opencode abandon --mailbox <dir> [--force]
trio-opencode land    --mailbox <dir>
trio-opencode doctor  [--config PATH]
```

`--in-place` disables root-free mode. `status` prints `STATE.md`,
`.driver.json`, `.opencode-result.json`, the registry record and the
root-free Lead worktree record (if any) as one JSON object.

**Open-loop flags** (`cli.py`'s `--isolate-workers`/`--slice-eval-concurrency`/
`--slice-eval-drain-seconds`/`--no-kill-check`, resolved by
`openloop.resolve_settings`, CLI > config > default, same precedence
`trioctl omnigent loop` uses): a mailbox is driven in open-loop mode
automatically whenever it (or, root-free, an existing Lead worktree's live
mailbox) has a `QUEUE.md` — there is no `--open-loop` flag. On a lockstep
mailbox every open-loop flag is accepted as a documented no-op (a one-line
notice on stderr; `--isolate-workers` is always on and
`--slice-eval-concurrency` is forced to 1, since lockstep builders are
already always isolated and have no slice-evals). `--slice-eval-concurrency
N` with `N > 1` is refused (exit 2) when combined with
`--no-isolate-workers`. See "Open-loop mode" below for what each flag
controls.

**Exit codes** (`cli.py:EXIT`, overridden by the helper's own `code` field
when present): `shipped`/`landed`=0, `error`/`conflict`=3,
`max_iterations`=4, `needs_human`/`blocked`=5, `needs_retirement`=6,
`needs_land`=8, `refused` (lock held)=9, `cancelled`=130.

**Example session**: `trio-opencode doctor --config ~/.config/trio-opencode/config.json`
→ `{"checks":[...],"ok":true}`; `trio-opencode start --mailbox /repo/loop
--max-iterations 4` → `{"status":"shipped","code":0,"commit_shas":[...],
"iterations":[...],...}`; `trio-opencode status --mailbox /repo/loop` →
`{"mailbox":"/repo/loop","registry":{...},"lead_worktree":{...}}`. After a
crash: `trio-opencode resume --mailbox /repo/loop` kills any orphaned turn
process group first, then continues from `STATE.md`.

**`doctor` output**: `{"checks": [...], "ok": bool}`, one entry per check in
order: `config`, `model_tiers`, `opencode_binary`, `opencode_version`,
`cli_caps`, `key_file`, `git_version`, `python_version`, `repo_files`,
`generate_config` (only on failure) / `no_ask_permissions`, `no_auto_flag`,
`opencode_models`. Each entry is `{"name", "ok", "detail"}`, scrubbed of the
key. Exit 0 iff every check passed.

## 4. Design

**Loop sequence** (`driver._drive`, mirrors `native/trio-native.js` 1:1):
`begin` → loop `next` → (`lead`: plan → per-wave `dispatch` → concurrent
builder worktrees → verify → `integrate` → `cleanup`, repeated per wave; or
`repair`: one turn) → `gate` (one retry on failure) → `pin` → `evaluator`
(skipped if `pin.skip_evaluator`) → `apply` → next iteration, or stop
(`shipped`/`blocked`/`needs_human`/`needs_retirement`/`max_iterations`/
`error`/`conflict`/`cancelled`) → (root-free, on ship) `land` → `end` →
write `.opencode-result.json` (+ `teardown` if landed) → registry record.

**Structured outputs**: every role is asked for one fenced ```` ```json ````
block at the end of its message (the **last** parseable fenced object in the
text wins). Plan: `{slices:[{id,brief,writes,reads,depends}], notes}`;
integrate: `{merged, conflicts, summary}`; builder: `{summary,
targeted_check, outside_writes}`. Missing/malformed → **one** re-prompt in
the same `opencode` session (`-s <id>`) with the problem and a schema hint.
For builder/integrate reports this is `advisory`: git stays the authority
(the verified branch; the merges `cleanup` actually saw), so a still-bad
report after the re-prompt logs a warning and continues with defaults — it
never stops the run. A malformed Lead plan does stop the run.

**Root-free** (`rootfree.py`, default; `--in-place` disables): one private
worktree per mailbox, on branch `trio/<slug>` (same slug algorithm as
`metrics/trio-metrics.py:loop_slug`), forked from the target branch's tip.
`prepare()` creates or re-attaches it and seeds the live mailbox: a
**fresh** worktree gets every non-runtime mailbox file copied and committed
as `loop: seed <rel>`; a **re-attached** one (resume) only re-syncs
`GOAL.md`/`HUMAN.md` — loop state files are never re-seeded over live
progress. `land()` fast-forwards (or CAS `update-ref`s when the target has
no worktree checked out) to the Lead branch's tip; a diverged target
returns `needs_land`. `teardown()` (after a landed ship) copies the runtime
sidecars back to the root mailbox, removes the worktree if clean, deletes
the branch if merged. `abandon()` gives up, keeping the branch. Record:
`<git-common-dir>/trio-opencode/lead-<slug>.json`.

**Mailbox runtime files**: `.driver.json` (turns dict keyed by label —
concurrent builders mean more than one live turn — pid/pgid/session_id per
turn, phase, iteration; atomic before/after every turn), `.opencode.json`
(the loop-core record), `.opencode-result.json` (final result, mirrored to
the root mailbox after a landed ship), `.session.json` (loop-core `begin`
writes it; the dashboard reads it to detect a live opencode loop).

**Registry record** (dashboard; one file per mailbox at
`${TRIO_OPENCODE_RUNS_DIR:-~/.local/share/trio-agent-loop/opencode-runs}/<sha256(mailbox)[:16]>.json`,
flock-protected read-modify-write): `schema`, `driver`/`harness` (both
`"opencode"`), `mailbox`, `live_mailbox`, `repo`, `lead_worktree`, `branch`,
`target`, `run_token`, `exec_id`, `pid`, `state` (`running`→`finished`),
`status`, `result_path`, `begun_at`, `updated_at`, `finished_at`.
`dashboard/loop_actions.py:opencode_registry` accepts only `driver ==
"opencode"` records and drops `live_mailbox` if it is a symlink or resolves
outside the record's own repo.

**Ownership ledger**: `<git-common-dir>/trio-opencode/<sha256(mailbox)[:16]>/owned.jsonl`
(append-only, flock+fsync — same mechanism as `native/`'s ledger, but under
`trio-opencode/` since `steplib.py` overrides `NS.LEDGER_DIR`). Builder
worktrees are driver-created (`git worktree add -b trio-oc/<exec8>/i<iter>-<slice> ...`)
and ledger-recorded `verified_by: "driver-created"` **before** the opencode
turn runs — acceptance never depends on the builder's own report.

**Lock**: two layers. (1) A process-exclusive `fcntl.flock` on
`<git-common-dir>/trio-opencode/<mailbox key>/driver.lock`, held for the
whole `run()` call, acquired before `rootfree.prepare`/`ocgen.generate`
touch anything — a busy lock refuses instantly (`code: 9`), no pid race.
(2) The mailbox-level `.lock` directory protocol the loop core itself uses
(owner/pid/heartbeat files) for `begin`/.../`end` idempotency and
cross-driver interop; `steplib` overrides `_owner` to `"opencode:<token>"`
so a lock this driver holds gets the strict pid-liveness-only takeover rule,
never the more permissive stale-heartbeat grace reserved for
`"workflow:"`-owned locks; a native Workflow lock keeps its own semantics.

**HUMAN.md answers**: unchanged from `native/` — the loop core's own
ledger-verified answer is looked up in `next` (Lead) and `pin` (Evaluator)
and appended to the prompt as a `## Verified human answer (driver)` block;
everything else is `human_notes`, logged, never acted on.

## 5. Open-loop mode

**Selection**: automatic, no `--open-loop` flag — a mailbox (or, root-free,
an existing Lead worktree's live mailbox) with a `QUEUE.md` is driven
open-loop; otherwise lockstep (`openloop.detect_open_loop`, D1). Open-loop
replaces `driver._drive` with `openloop.drive`, which calls the shared core
`metrics/trio_loop.py:run_open_loop` in-process — the same function
`trioctl omnigent loop` drives — passing one `OpenLoopRunner` instance as
both the Lead and the Evaluator role runner.

**Sequence, one Lead pass** (`OpenLoopRunner._run_lead`): plan
(`lead-plan`, re-prompted once on a plan the mailbox/`repos:` cannot
support) → `waves.plan_waves` groups the returned slices → per wave,
concurrent builder turns (`_run_wave`, one `ThreadPoolExecutor`, one
worker when `--no-isolate-workers`), each in its own driver-created
worktree/branch (`trio-oc/<exec8>/i<iter>-<slice>-a<attempt>`) → the moment
a builder's branch passes `_verify_branch` (a `slice(<id>):` commit over
the dispatch head, no mailbox-file commits) AND its `TARGETED_CHECK:` line
is not `FAILED`, `_merge_and_retire` merges it `--no-ff` into the target
repo and appends a `QUEUE.md` `retired:` entry immediately (`olqueue.
append_retired`) — a failing or unverifiable branch is cleaned up unmerged,
one re-dispatch from the current repo HEAD, then the slice becomes a Lead
take-over; a Lead take-over commit made during the following lead-review
turn is retired the same way after re-running its own targeted check
(`_retire_one_lead_commit`) → `lead-review` turn closes the pass.

**Grading**, dispatched by the shared core as slices retire:
- **slice-eval** (`OpenLoopRunner._run_slice_eval`): one per retired
  (slice, sha), up to `--slice-eval-concurrency` (default 4) concurrently,
  each in its own detached worktree at that sha when isolation is on. Runs
  `quality.slice_lint`/`quality_note` (r18a SLICE QUALITY/PRE-GATE block)
  and, after the turn, `quality.slice_evidence` (r18a L1) logged to
  `LOG.md` and `.driver.json`'s `quality` key.
- **integration-eval** (`OpenLoopRunner._run_integration_eval`): once every
  planned slice is retired and no fault is open/taken, in detached
  worktree(s) at the pin(s) (multi-repo: one per declared repo plus home),
  with the generated whole-goal rigor block
  (`prompts/integration-rigor.md`, from `prompts/generate.py`) appended.
- Verdict parsing, retry and applying all happen in the shared core
  (`run_open_loop`) itself — the runner only runs the turn and reports
  `exit`/`session`.

**r18a shadow quality** (`quality.py`, advisory — never changes a
retire/merge/verdict decision): L2a base-revert kill check
(`run_kill_check`/`kill_check_for_builder`, run after a builder passes and
before its merge); L1 evidence-kind telemetry (`slice_evidence`,
`evidence_log_line`); `_slice_lint` (test-tautology + accept lints, per
slice-eval); `_lint_after_lead_pass` (mailbox-wide quality lint, run after
every lead-review turn, written to `.driver.json`'s `lint` key). L3
independent-probe *logging* is not ported — the probe requirement itself
stays in the Evaluator's prompt body.

**SHIP retirement / `needs_retirement` / `needs_land`**: the shared core's
own stop codes, mapped in `openloop._final_status_for_code`; a root-free
land hook (`openloop.make_land_hook`, D11) runs `rootfree.land` (declared
repos first, home last) and commits a land record to the Lead repo; a
diverged target returns `needs_land` (`trio-opencode land`, or the hook
again on resume, retries — no Omnigent `reverify` re-land rule).

**Multi-repo**: open-loop only. `PLAN.md`'s `repos:` block
(`openloop.declared_repos_for_prepare`, D2) is passed to
`rootfree.prepare(..., declared=...)`, which gives each declared repo its
own aggregate worktree alongside the home Lead worktree
(`rootfree.py`'s `RootFreeAggregates` port); a slice's `repo:` field
targets one of them (`OpenLoopRunner._target_repo_path`). Lockstep never
computes `declared` (`driver.run` only calls
`declared_repos_for_prepare` when open-loop is detected) and stays
single-repo.

**r19 acceptance**: the author hook (`OpenLoopRunner.author`, D13) reuses
the same acceptance tool path and `acc_plan_lines`/`acc_pass_lines`
fragments the lockstep path uses, attached to the lead-plan and
lead-review turns' `notes`.

**Isolation / concurrency flags** (`--isolate-workers`/
`--no-isolate-workers`, `--slice-eval-concurrency`, resolved by
`openloop.resolve_settings`, D12): isolation defaults on in open-loop
(driver-created worktrees for every builder/slice-eval/integration-eval);
off, builder waves and slice-evals run directly in the target repo, one at
a time, and `--slice-eval-concurrency` is forced to 1 (an explicit `N > 1`
with `--no-isolate-workers` is refused, exit 2). On a lockstep mailbox both
flags are accepted no-ops (lockstep builders are always isolated; there
are no slice-evals).

**`QUEUE.md` concurrent-write guard** (`olqueue.py`): up to N concurrent
slice-eval turns and the Lead can all edit `QUEUE.md` by hand while the
driver also appends `retired:` entries for driver-owned builders.
`append_retired`/`append_fault` are atomic (tmp + fsync + rename under a
lock), idempotent and self-checked without ever rolling back to a stale
snapshot: reads settle past an agent's mid-truncate window, and the entry's
presence is verified after the write and retried against newer content when
an (unlockable) agent write replaced ours — see PARITY.md "QUEUE.md writes".
`QueueGuard` (one instance per run, shared by the Lead thread and every
concurrent slice-eval) remembers every `retired:`/`faults:` entry ever
observed and, after each role turn, re-appends anything a concurrent
full-file rewrite dropped and renumbers a fault id two turns picked
independently — without ever reverting a legitimate change (a fault's
`status:` transition) or touching a block whose fence failed to parse.
This is stronger than Omnigent, which guards only `VERDICT.md`.

**STATE protection** (`openloop._StateGuard`, D7): wraps `TL._update_state`
for the whole run so every driver-owned `STATE.md` write
(`driver.OWNED_STATE_KEYS`) records the expected values, and restores a
role turn's deviation from them after every lead/evaluator turn
(`driver._restore_owned_state`).

**Crash/resume** (`openloop._restore_resume_state`, H5): on start, before
anything else can overwrite `.driver.json`, a prior crashed run's
`state_snapshot` is restored to `STATE.md` (gated on a matching
`run_token`; a mismatched or absent token is skipped) and its `builders`
map (authored-by/kill-check bookkeeping per `<slice>@<sha12>`) is folded
back into the fresh `OpenLoopRunner` (`load_resumed_builders`) so a
resumed slice-eval still has its builder quality facts. Stale builder/eval
worktrees from a dead execution are reclaimed (`_reclaim_stale_worktrees`,
D10) and leftover eval worktrees swept after the run
(`_remove_leftover_eval_worktrees`). SIGTERM/SIGINT while idle between
turns (the one window `driver.run()`'s own flag-only handlers miss) are
handled for the duration of `run_open_loop` (`_install_idle_signal_
handlers`, H4) and map to `cancelled`/143/130.

**Files written**: `QUEUE.md` (`retired:`, `faults:` fences), `VERDICT.md`
(per-slice `## slice <id> @<sha> — SHIP|ITERATE` sections, written by the
shared core from the Evaluator's turn), `.driver.json` (additive keys:
`quality` — `slice_evidence` per `<slice>@<sha12>`; `lint` — the last
`lead_pass_lint` result; `builders` — the authored-by/kill-check/flags/
targeted-check record per `<slice>@<sha12>`; `turns` — one entry per live
turn, label-keyed, since open-loop can have more than one live turn at
once; `state_snapshot` — the owned-STATE restore point), `.sessions/
aggregates.json` (multi-repo declared-repo map, `rootfree.
write_aggregates_map`), and `LOG.md` lines for every retirement, quality
fact, queue-guard repair and land/reclaim event.

## 6. Permissions

Per-role permission blocks (`ocgen.py:PERMISSIONS`); **no `"ask"` anywhere**
— a non-interactive `run` cannot answer a prompt, so every normally-`"ask"`
default (`doom_loop`, `external_directory`, `question`) is set explicitly.
`opencode` never runs with `--auto` (`runner._build_argv` asserts its
absence; `doctor`'s `no_auto_flag` check greps `runner.py` for it outside an
assertion).

| role | edit | webfetch/websearch | bash | task |
|---|---|---|---|---|
| lead | allow | allow | `"*"` allow, deny-list below | `trio-scout` only |
| evaluator | allow | allow | deny-list + allow `git worktree add --detach*` | `trio-scout` only |
| builder / repair | allow | deny | `"*"` allow, deny-list below | deny |
| scout | deny | deny | deny by default; allow only read-only inspection (`git log/show/diff/status`, `ls`, `cat`, `grep`, `rg`, `find`, `head`, `tail`, `wc`) | deny |

Shared bash deny-list (every role but scout; matched **last** — `"*": allow`
first, specific denies after): `git push*`, `git * --force*`, `git * -f`,
`git reset --hard*`, `git clean -*x*`, `git worktree remove*`,
`git branch -D*`/`-d*`, `git update-ref*`, `git config --global*`,
`git checkout -f*`, `rm -rf /*`/`~*`/`..*`/`$HOME*`, `sudo *`,
`curl *|*sh*`, `wget *|*sh*`, `opencode*`, `chmod -R 777*`.
`external_directory` is otherwise `deny`, with one per-role allowance for
the run's own scratch dir (`<repo>/.trio-opencode/worktrees/tmp-<exec id>*`).

**These bash-pattern denies are guard rails against an agent's ordinary
tool calls, not a security boundary.** A determined or prompt-injected agent
in its own worktree can still reach destructive commands the deny-list does
not enumerate; do not run this driver against untrusted mailbox/prompt
content expecting these globs to contain it.

**Permission-prompt detection**: if the stream shows `permission
requested: ...` / `auto-rejecting`, `runner._pump` kills the whole process
group immediately and `events.classify` returns `kind="permission"` — the
turn is never retried; `driver._call_role` raises `DriverStop` at once. The
regex only ever runs against a stdout line that did **not** parse as a JSON
event (a parsed event's own tool input/output text — e.g. a builder
grepping this driver's source for "auto-rejecting" — can never trigger it;
a JSON event whose own `type` contains "permission" is still caught via the
accumulator) and, on stderr, against lines with any `message="spawning
process"` log line dropped first (it echoes a tool's own argv, which may
contain anything the model passed it) — the real CLI warning line is never
itself one of those and always stays in scope.

## 7. Resilience

**Timeouts**: `turn_seconds` (wall clock/attempt, default 3600s),
`idle_seconds` (no stdout byte, default 600s; `config.validate` floors it
at 180s for provider-internal silent retries), separate
`evaluator_turn_seconds` (default 5400s). Both kill the whole process group
(`SIGTERM`, wait up to 5s, `SIGKILL`).

**Retry classification** (`events.classify`), in the order it is decided —
permission, then any reported error event (config_error/transient/
model_error named checks, in that order), then truncation, then `ok`, then
the stderr/message free-text scan, then the unsupported-version/killed/
empty-output fallbacks:

| kind | examples | retried? |
|---|---|---|
| `permission` | `permission requested: ...; auto-rejecting` (stdout lines that parsed as a JSON event, and stderr lines echoing a spawned tool's own argv, are never scanned for this) | no — killed |
| `config_error` | v1 `ProviderAuthError`/.../`SessionNotFoundError`; v1 `APIError` with `statusCode` 401/403; v2 `error.type`~`no-route\|auth\|not-found\|model-unavailable`; >= 3 events all of an unrecognised type (1-2 is too weak a signal — see `transient`); missing CLI capability; missing/empty key file; unsupported opencode version (3+ events, all unknown types) | no |
| `transient` | v1 `UnknownError`/`SessionBusyError`; v1 `APIError` with `isRetryable: true` or `statusCode` 408/409/425/429/5xx; v2 `error.type`~`rate\|limit\|timeout\|server\|overload\|network\|unavailable`; API 408/409/425/429 and `isRetryable`; truncated event stream (an explicit `step_start` whose step never got a `step_finish` nor any text/tool of its own — the stream ended mid-step); near-empty stream; rate limit/429, 5xx/overloaded, `ECONNRESET`/`ETIMEDOUT`/`TimeoutError`/`Transport error`, socket hang up, empty output; a negative exit code (killed by signal, e.g. OOM); 1-2 events all of an unrecognised type | yes, backoff |
| `idle_timeout` | no stdout for `idle_seconds` | yes (as transient) |
| `model_error` | `MessageOutputLengthError`/`StructuredOutputError`/`MessageAbortedError`/other reported error | no |
| `timeout` / `cancelled` | wall-clock exceeded / SIGTERM/SIGINT or cancel | no |
| `step_long exhaustion` | `step_long` agent timed out polling a detached helper job (acceptance-run, etc.) | no — clean error stop |

A turn that completed normally (exit 0, no reported error, not truncated,
and the last step finished with reason `"stop"` or produced text) is
decided `ok` *before* the free-text stderr scan ever runs — so `--print-
logs`' own INFO-level lines (which echo a tool's shell command/args
verbatim, e.g. a `curl` call that happens to mention "network" or "503")
can never re-classify a turn that actually succeeded. A truncated or near-empty
event stream is transient and retried with bounded retries. INFO-level
stderr no longer reclassifies a finished turn. Permission text inside tool
output or echoed commands does not kill a turn; only explicit stdout
permission-request lines parsed as JSON events, or unquoted stderr CLI
warnings, trigger the kill.

Backoff is `retries.backoff_seconds` (default `[10, 30, 90]`), indexed by
attempt and clamped to the last value; bounded by `retries.max_attempts`
(default 3). On the first retryable failure the runner decides once whether
to **continue the same session** (`-s`, short "continue" prompt) — only if
no session id was given up front and the failed attempt already picked one
up with >=1 step — or restart the original prompt fresh; locked in for the
rest of the attempts. Every role turn also gets **one retry at the driver
level** on any non-stop-kind failure (`runAgentTwice`-style): two failed
attempts still not `ok=True` raises a run-ending error.

**Crash-safe resume**: `.driver.json` (root mailbox, and — root-free — the
live mailbox; `resume` checks both) is written atomically before/after
every turn spawn/end with the live `turns` dict (pid/pgid/session_id per
label). `resume` kills every recorded turn whose pid is alive **and** still
looks like an `opencode` process (`/proc/<pid>/cmdline` check), then calls
`begin`, which reclaims this mailbox's own ledger-owned builder worktrees
from the dead execution (merges mergeable branches, discards unreasonable
ones, keeps dirty/foreign ones — same rules as `native/`), and continues
from `STATE.md`. A fresh builder worktree is always created per wave (a
killed builder's worktree is never resumed in place — unlike `native/`).
The kernel `driver.lock` refuses a concurrent `start`/`resume` outright
(code 9), independent of any pid check.

**Driver-owned STATE keys**: The driver maintains a small set of STATE keys
(`iteration`, `phase`, `evaluated_sha`, `evaluator_attempt`, `evaluated_repos`)
for internal sequencing. Lead, repair, and evaluator turns that write one
of these are reverted at the turn's end. Before every turn, the driver
snapshots the pre-turn STATE to `.driver.json`, bound to the run's
`run_token` — on `resume` after a crash, the snapshot is restored only when
the same `run_token` is used (either the original, or an explicit `--run-token`
flag matching it). Unknown tokens that claim a different `run_token` are
treated as separate runs. **Limitation:** there is no protection against a
different harness (the native driver, another opencode-driver instance) 
writing to the same mailbox between the crash and the resume.

**Open-loop resilience** (see "Open-loop mode" above for the full detail):
the same `.driver.json`/`run_token` gate protects driver-owned STATE keys,
but the restore point and the resume path are open-loop's own
(`openloop._restore_resume_state`/`_StateGuard`/`_reclaim_stale_worktrees`),
since open-loop never calls `steplib.begin`. A **fatal stop** — a role
denial (`permission`/`config_error`), or a turn failing twice — cancels
every other live turn at once (concurrent builders, slice-evals, the Lead
thread; `openloop._Fatal`) rather than letting them run to completion. A
SIGTERM/SIGINT that arrives while idle between turns (a window
`driver.run()`'s own flag-only handlers miss) is handled for the duration
of the open-loop run and stops it (`cancelled`, 143/130).

## 8. `opencode run --format json` event schema

One JSON object per NDJSON line, `{"type": T, "timestamp": ms,
"sessionID": "ses_...", ...}`. `events.py` tolerates unknown `type`s,
missing fields, and both eras' naming differences (`sessionID`/`sessionId`/
`session_id`; `error.name` vs `error.type`; nested vs flat tokens).

**v1 (1.18.33)** — full step wrapping: `{"type":"step_start","sessionID":
"ses_1","part":{"type":"step-start"}}`, `{"type":"text","sessionID":"ses_1",
"part":{"type":"text","text":"OK"}}`, `{"type":"step_finish","sessionID":
"ses_1","part":{"type":"step-finish","reason":"stop","tokens":{"input":10,
"output":5,"cache":{"read":0,"write":0}}}}`. v1 error:
`{"type":"error","error":{"name":"ProviderAuthError","data":{"message":"..."}}}`.

**v2 (2.0.20)** — may be text-only, no `step_start`/`step_finish` at all
(observed live for `deepseek`); completion is "process exited, some text
was collected": `{"type":"text","sessionID":"ses_1","part":{"type":"text",
"id":"prt_1_text-0","text":"OK"}}`. v2 text parts may repeat the same `id`
with growing text (streaming updates) — only the latest text per id is
kept, in first-seen slot order; a part with no id always appends. v2 error
(dotted `error.type`, not `error.name`): `{"type":"error","sessionID":
"ses_1","error":{"type":"provider.no-route","message":"Model unavailable: x/y"}}`.
Classification matches `error.type` itself, never the free-text message —
so `"Model unavailable"` under `provider.no-route` stays `config_error`,
not caught by the transient `unavailable` text pattern.

**Final text**: text parts of the last step that produced any text,
concatenated in order; a pure text-only v2 stream collapses to one implicit
step, so success never requires a `step_finish` — exit 0 and
(`last_step_finish_reason == "stop"` or non-empty text) is `ok`. Exit 1 with
no JSON line at all (e.g. a bogus `--session` under v1) falls back to a
stderr/exit-code text scan.

Feature detection (`runner.detect_cli`, cached by `(realpath, mtime)`)
parses `opencode run --help` once per binary: `style="v2"` when
`--standalone` is offered and `--dir` is not, `style="v1"` when `--dir` is
offered, else `"unknown"`. Any of the four required capabilities (JSON
format, `--model`/`-m`, `--agent`, session flag) missing → `config_error`
before spawning anything, for every turn against that binary.

## 9. Tests

```
mkdir -p <a lab-owned tmp dir>
TMPDIR=<that dir> python3 -m pytest -q opencode-driver/tests
```
(192 passed as of this writing; never point `TMPDIR` at the host `/tmp`.)
`tests/conftest.py` is autouse: isolates `HOME`/`XDG_{CONFIG,DATA,STATE,
CACHE}_HOME`, `TRIO_OPENCODE_RUNS_DIR`, `TRIO_NATIVE_RUNS_DIR`,
`CLAUDE_CONFIG_DIR` to per-test tmp dirs; strips every `PATH` entry whose
own `opencode` does not resolve under the test's `tmp_path`; and asserts
after each test that `shutil.which("opencode")` still resolves nowhere
real, aborting the **whole session** on a violation — fail-closed OS-level
isolation (real `PATH`/env manipulation), not a Python monkeypatch
presented as a boundary.

`tests/fake_opencode.py` emulates OpenCode v2.0.20 by default
(`--standalone --format json --print-logs --log-level info --agent A -m
provider/model[#v] [-s session] PROMPT`, plus `run --help`/`--version`/
`models [--standalone]`); `FAKE_OC_STYLE=v1` switches the whole surface
(flags and event shapes) to 1.18.33. Per-test behaviour comes from a
scenario module (`FAKE_OC_SCENARIO`, `handle(ctx)`); state (session ids,
call counters, the full call log) persists across invocations in
`FAKE_OC_STATE` under an `fcntl` lock.

Scenarios (`tests/scenarios/`): `happy`, `iterate_repair`, `transient_plan`,
`malformed_plan`, `unbound_verdict`, `always_iterate`, `permission_hang`,
`wall_timeout`, `sigterm_hang`, `crash_resume`, `concurrent_crash_resume`,
`resilience_partial_wave`, `acceptance`. `test_e2e.py` drives the whole
stack against these (lockstep paths; see below for open-loop): happy path
(root-free, concurrent wave, and v1-style);
iterate → repair → ship; transient retry; malformed-plan re-prompt
(recovering, and still-malformed leaving `STATE.md` resumable);
unbound-verdict re-prompt; `max_iterations` stop; permission denial (fast
kill, no orphan); wall-clock timeout; SIGTERM mid-turn; lock refusal
(`start`/`resume` both exit 9); `status`/`doctor` CLI; `needs_land`
(overlapping root change, diverged target); crash+resume; concurrent
builders (both turns tracked, both killed on resume); partial-wave
resilience; a verified HUMAN.md answer reaching the Lead plan prompt; r19
frozen acceptance end-to-end through a real pack (ship, the frozen gate
refusing a SHIP, coverage-refusal re-plan/stop, author contamination, the
validation retry, and a detached acceptance job's real `pending` path).

**Open-loop tests**: `tests/test_olprompts.py` (per-call open-loop prompt
templates), `tests/test_quality.py` (r18a shadow telemetry: `TARGETED_CHECK:`
parsing, the base-revert kill check, evidence/lint helpers), `tests/
test_olqueue.py` (`QUEUE.md` atomic append + `QueueGuard` concurrent-rewrite
repair), `tests/test_rootfree.py` (includes the declared-repo/`RootFreeAggregates`
multi-repo cases), `tests/test_openloop.py` (settings resolution, the
`OpenLoopRunner`, the state guard, the land hook), and a growing
`tests/test_openloop_e2e.py` + `tests/scenarios/ol_*.py` end-to-end suite
against a QUEUE.md mailbox. Counts shift as that e2e suite grows; collect
them locally with `python3 -m pytest -q --co tests/test_olprompts.py
tests/test_quality.py tests/test_olqueue.py tests/test_openloop.py
tests/test_openloop_e2e.py` rather than trusting a number committed here.

## 10. Container / no-time-limit mode

Three opt-in knobs for running inside a Terminal-Bench task container (no
wall-clock budget, the whole container is the product), all off by default
so existing behaviour is unchanged unless set:

- **`timeouts.turn_seconds` / `timeouts.evaluator_turn_seconds`: `0` or
  `null`** disables that wall-clock limit entirely — `config.py` normalizes
  `null` to the `0.0` "disabled" sentinel, and `runner.py`'s `_pump` loop
  simply skips the wall-clock check while it is falsy (`idle_seconds` keeps
  applying either way: a turn can run forever but must still produce
  stdout).
- **`retries.idle_retry_unlimited`** (bool, default `false`): when an
  `idle_timeout` fires (no stdout for `idle_seconds`), the runner retries
  the same turn — continuing the same `opencode` session (`-s <id>`, short
  "continue" prompt) when a session id is already known with at least one
  step, else restarting the original prompt, same logic as an ordinary
  transient retry — **without** counting the attempt against
  `retries.max_attempts`. A hung connection can therefore never by itself
  turn into a run-ending error; each retry logs one clear
  `idle watchdog retry` line (attempt number included) to stderr. The
  backoff schedule still applies, clamped to `backoff_seconds`'s last
  value once the attempt count runs past it. The driver's own one-retry-per-
  role-turn path (`_call_role`) never has to special-case this: with the
  flag on, `run_turn` only ever returns an `idle_timeout` result if
  cancelled, so it never reaches that path as a final failure.
- **`container_mode`** (bool, default `false`): in `ocgen.py`, every
  generated role's `external_directory` permission becomes `"allow"` —
  including scout's (its `edit`/`bash` restrictions are untouched; it is
  relaxed only so a read of something outside the project is never
  blocked). The shared bash deny-list swaps to a container-safe variant:
  the blanket `rm -rf /*` glob (which would also deny ordinary in-container
  cleanup like `rm -rf /app/build`) is replaced by exact-ish root-wipe
  entries `rm -rf /` and `rm -rf / *` (`rm -rf ~*`/`rm -rf ..*`/
  `rm -rf $HOME*` are unchanged), and remote/network-destructive commands
  are newly denied: `git remote add*`, `git remote set-url*`, `scp *`,
  `rsync *:*`, `ssh *`, `nc *`, `ncat *`. `"*": allow` is still written
  first with every deny after it (last-match semantics unchanged).

## 11. Frozen acceptance (r19)

Off by default (`config.acceptance: false`, `--no-acceptance`, matching the
installed release). Ports `docs/FROZEN-ACCEPTANCE.md`'s Claude-native path
(`native/`'s `args.acceptance`) to this driver by **reusing** the same
helper, not re-implementing it: every acceptance op (`acceptance-export`,
`acceptance-freeze`, `coverage`, `acceptance-run`, the acceptance-aware
`begin`/`next`/`gate`/`apply`/`end`) is the identical
`native/trio_native_step.py` function this driver's `steplib.py` already
loads as a private module copy for the non-acceptance ops — including all
of its trust machinery (the per-execution HMAC-sealed record, the pin
chain, detached-job polling, tamper/mismatch detection). `driver.py` and
`prompts.py` only drive it, the way `native/trio-native.js` does.

- **Switch.** `config.json`'s top-level `"acceptance": true` (`config.py`),
  overridable per run by `--acceptance`/`--no-acceptance` on `start` (never
  `resume` — see below) and by `TRIO_ACCEPTANCE=1|0`; precedence **CLI >
  env > config file > off** (`config.resolve_acceptance`). With the switch
  off, every op call, prompt and record is byte-for-byte what it was before
  this feature (`tests/test_acceptance.py`'s switch-off-identity tests; the
  rest of the suite never turns it on and stayed green unmodified).
- **Resume.** The switch is a *start*-time decision that holds for every
  resume of that run: `cli.py`'s `resume` **refuses** an explicit
  `--acceptance`/`--no-acceptance` (mirrors `native/launch.sh`'s "resume
  replays the recorded args" refusal — the two drivers agree here, so there
  is no engineered precedence to ignore). Absent a CLI flag, `resume` reads
  the value the run was **started** with from, in order, the run registry
  (`acceptance_enabled`, written at `begin`), `.opencode-result.json`
  (`acceptance.enabled`, a finished run), `.driver.json` (`acceptance_enabled`,
  a crashed run); a mailbox with none of these (pre-r19) defaults to off.
- **Tier.** `config.validate()` requires `models.lead == models.evaluator ==
  models.acceptance` (never cheaper) and `variants.acceptance` (if set) to
  equal `variants.lead`; `begin` enforces the model check again, server-side,
  before it even takes the mailbox lock (`_acc_tier_problem`, inside the
  reused helper) — a config that validates can still be refused at `begin`
  if a caller bypasses `validate()`. The acceptance agent is always
  dispatched with the **Lead's** variant (never `variants.acceptance`
  itself, which is informational only, per the helper's own design: the
  acceptance tier is authored by the Lead turn).
- **The `trio-acceptance` agent.** Added to `ocgen.py`'s `ROLES`/
  `PERMISSIONS` (edit allow; bash `"*"` allow plus the standard deny list;
  task/webfetch/websearch deny; `external_directory` deny except the run's
  own tmp dir) and generated into every run's `opencode.json` like the other
  five roles (so it is available whenever a run turns the switch on). Its
  body is loaded from a **dedicated** path,
  `opencode-driver/agents/trio-acceptance.md` — never `opencode/agents/`,
  which `ocgen._load_role_body` keeps exclusively for the other five roles.
- **Author turn.** Unlike native (a Claude Code Workflow `agent()` call,
  which cannot be given a `cwd` and so starts in the loop *repository*, with
  the author told to `cd` into its export workspace for every command),
  OpenCode turns start in the given `cwd` — so `driver.py` spawns the author
  turn with `cwd` = the helper's `acceptance-export` directory directly
  (`prompts.author_prompt`'s "your cwd is your workspace", no per-command
  `cd` prefix). Structured output (`{"checks": <int>, "summary": "..."}`) is
  one fenced ```` ```json ```` block, parsed with the same reprompt-once,
  advisory-on-failure mechanism as a builder's report (git/the pack's own
  validation remain authoritative either way). `acceptance-freeze`
  (dropped/fatal retry, contamination re-run, up to 3 attempts, mirrors
  `authorPhase`) then validates and freezes the pack exactly as native does.
- **Author isolation: two real levels, chosen by a probe (acc-harden).**
  Detect-and-discard alone discarded every attempt of a real run (a goal that
  names absolute repository paths makes a model go and read them), and
  permission globs over *command text* cannot bound a shell (`cd ..`,
  symlinks, `python -c "open(...)"`, string building). So the author turn
  (`RunContext.author_setup`, `trio_opencode/authorbox.py`) is contained one
  of two ways, picked at run time by *starting* bwrap, not by finding it on
  `PATH` (`TRIO_OPENCODE_AUTHOR_ISOLATION=no-shell` forces the second):
  - **`sandbox`** — the whole `opencode` process runs under `bwrap` with a
    tmpfs root: only the export (read-write), a private scratch dir, the
    OpenCode config/cache dirs, the validator's directory, the system dirs a
    process needs and the `PATH`/interpreter/`opencode` directories are
    mounted. The author's own generated config (`opencode.json`, plus an
    empty XDG config home beside it, in `author-cfg-<id>/` next to the
    scratch dir, outside the run dir) is mounted **read-only** by name; the
    author cannot loosen its own rules. The loop repository and every ancestor of it do not exist for
    the process, so a shell stays available and safe; the network is **not**
    unshared (the provider stays reachable); a mount that would expose the
    repo or an ancestor is skipped. Extra read-only/read-write mounts a site
    needs: `TRIO_OPENCODE_AUTHOR_SANDBOX_RO` / `_RW` (`os.pathsep` lists). The
    tool-call text audit is not applied (the repo was not there; `ls <repo>`
    -> "no such directory" read nothing).
  - **`no-shell`** — no usable OS sandbox (Docker/Harbor task containers block
    user namespaces; their security options are not ours to loosen): the
    author gets **no shell** and only OpenCode's own file tools, confined by
    the generated per-turn config (`ocgen._acceptance_permission`, written to
    `author-cfg-<id>/opencode/` beside the export; deny rules only, never
    `ask`, never `--auto`). **Default-deny**: the block starts with
    `"*": "deny"` and allows only `read`, `glob`, `grep` and `edit`/`write`
    (a shell too, at `sandbox`), each confined to the export. OpenCode v2's
    Code Mode `execute` (`fetch('file://...')`, `session_move`) is thereby
    denied, and so is every tool a later OpenCode adds. The rules follow how OpenCode v2 actually decides (read out of
    the shipped binary, modelled in `tests/scenarios/oc_perm.py`): a path is
    resolved *lexically* (`..` folded, symlinks **not** followed) against the
    project directory; outside it, `external_directory` is asked with
    `<dir>/*` and is `deny` for everything but the export, the author's
    scratch and the validator's directory (also in `container_mode`, whose
    flat allow no longer applies to this role); `read` also denies the
    loop's own roots by absolute path and relative `../*`; `glob` refuses
    climbing, absolute and `~` patterns (OpenCode judges the pattern, not the
    directory); `bash` is denied; everything else (`execute`, `lsp`, `skill`,
    `task`, web, todo, MCP, unknown) is denied by the default. Because
    "inside" is lexical, every symlink that leaves the export (and every
    multiply-linked file) is removed before the turn
    (`authorbox.sanitize_export`), and when a git ancestor would otherwise
    become OpenCode's project root (`grep {path:"../.."}` then searches it)
    the export gets a minimal `.git` of its own for the turn only
    (`authorbox.ensure_project_root` / `release_project_root`). The author's
    prompt says it has no shell; the driver validates the pack at base itself
    (one retry with the drops).
  The level is logged loudly once per run (driver log, `LOG.md`, result
  `acceptance.author_isolation`). `TMPDIR`/`HOME` and OpenCode's
  `XDG_DATA_HOME`/`XDG_STATE_HOME` (where it saves a truncated
  tool output the model is told to read back) point at a private directory beside the export,
  outside the repository. The audit stays as the net at `no-shell`, with two
  corrections: it never counts text the author *wrote* (a pack that names a
  repo path in AUTHOR.md or a check read nothing —
  `metrics/trio-acceptance.py` `_AUDIT_AUTHORED_KEYS`, mirrored in
  `driver.audit_tool_input`) and never counts a call the permission rules
  refused (`driver.refused_by_permission`); and the `pattern` of a content
  search (`grep`) is text to find, not a path, so a goal sentence quoted in a
  grep of the author's own export no longer reads as a repo read
  (`driver.CONTENT_SEARCH_TOOLS`; a `glob` pattern is still audited).
- **Builders cut before the freeze are rebased onto it (open-loop).** The
  author runs alongside the Lead, so a builder branch is routinely cut before
  the `acceptance: freeze` commit exists. `_merge_and_retire` replays such a
  branch on the freeze (`OpenLoopRunner._rebase_onto_freeze`) before merging,
  because the shared commit gate rejects a `slice(<id>):` commit on a line that
  does not contain the freeze ("acceptance/freeze ordering") — for good, which
  used to livelock the poll loop. If the rebase cannot be done the merge goes
  ahead and the gate reports it.
- **A commit gate that keeps failing stops the run (core, all drivers).**
  `trio_loop.run_open_loop`: the failure is logged once per distinct reason
  (with trio-shadow's own text), and once the Lead is done and nothing is in
  flight the slice is waited on `GATE_GRACE_POLLS`, handed back to the Lead
  (`gate_errors` in its OPEN-LOOP CONTEXT) up to `MAX_GATE_REPAIR_ROUNDS`, then
  the run ends `status: error`. This driver passes `max_gate_repair_rounds=0`:
  its Lead is a planner that cannot repair a gate, so it stops after the grace
  period. Independently, `NO_PROGRESS_POLLS` consecutive idle polls (Lead
  finished, no slice-eval in flight, queue/verdict/state/HEAD unchanged) end
  the run `status: error` with a `no progress` LOG line; it counts zero
  progress, it is not a time limit.
- **Author failure degrades (open-loop).** `openloop.DegradableAcceptance`
  subclasses the core's controller. The author wait is unbounded by default
  (`acceptance_wait_seconds`; the turn's own idle watchdog is the only
  bound). When the pack was never frozen and the author phase fails
  (contaminated twice, a turn that died, a configured bound hit) every
  acceptance hook becomes a no-op, a loud `acceptance: DEGRADED to no frozen
  pack (<reason>)` LOG line says so, the half-written pack is removed, later
  Lead prompts stop mentioning a pack, the result's `acceptance` summary gains
  `degraded: <reason>` — and the loop carries on as if acceptance were off.
  The core (and the Omnigent, native and lockstep paths) end the run `status:
  error` instead. Tamper of a *frozen* pack, a resume state mismatch
  (NEEDS_HUMAN) and a cancellation are not degraded.
- **Author audit on OpenCode.** The reused helper's own
  `_native_author_audit` looks for a Claude Code subagent transcript, which
  never exists for an OpenCode turn — `steplib.py` **overrides** it
  (`_opencode_author_audit`, a module-attribute override on the private
  helper copy, the same pattern already used for `_owner`/`_register`/
  `_dangling_worktrees`/`_runs_dir`) rather than forking the helper.
  `driver.py` persists the author turn's own tool-call inputs (parsed from
  its already-scrubbed per-attempt NDJSON event log —
  `TurnResult.log_paths`) as one small JSONL file per attempt, keyed by the
  same `{marker}-a{attempt}` string the helper already uses, under a
  per-run directory (`steplib.AUTHOR_TOOLCALLS_DIR`); the override reads it,
  converts each tool call into the row shape
  `metrics/trio-acceptance.py::_audit_inputs` expects, and calls
  `ctl.ta.audit_transcript(entries, export, forbidden=ctl._audit_forbidden(),
  cwd=export)` — `cwd=export` (not `ctl.repo`, unlike native), since the
  OpenCode author's tools start there. No record found (an older run, or an
  unreadable log) falls back to the helper's own limited audit, same as
  native; the recorded `audit.path` is `"opencode"`, not `"native"`.
- **Coverage, pin, pre-run, SHIP gate.** Shared unchanged with native,
  through the same ops: the plan call's schema gains `covers`/
  `lead_integration`/`acceptance_bindings` (`prompts.PLAN_SCHEMA_HINT_ACC`);
  `coverage` runs before any builder wave, one re-plan on refusal, a second
  refusal stops the loop; `dispatch`/`gate`/`next` check the pin; builder
  briefs get their covered checks appended (`prompts.acc_briefed`); the
  Evaluator's prompt gets the driver's pre-run text block plus the
  canonical evaluator fragment (loaded at runtime from
  `prompts/canonical/acceptance-native-{lead,evaluator}.md`, never
  hand-copied — `prompts.acc_lead_fragment`/`acc_evaluator_fragment`); a
  FAIL turns a SHIP into ITERATE, surfaced to the next Lead plan call as
  `acceptance_errors`. `acceptance-freeze`/`acceptance-run`/`apply` run as
  detached helper jobs; `steplib.step_long` polls `pending` the same way
  native's `stepLong` does (bounded poll count, injectable sleep for tests).
- **Records.** `.opencode-result.json`, the run registry and `.driver.json`
  all gain additive keys only: `acceptance_enabled` (the resolved switch,
  from `begin` on) and, once the run ends, a full `acceptance` summary
  (`enabled`, `status`, `pin`, `checks`, `coverage_refusals`, `replanned`,
  `ship_refused`, `author_attempts`, `audit.limited`, ...).
- **Limits (shared with native).** The helper's documented limits apply
  unchanged: the audit is a detector, not a boundary (the export is the
  boundary); a same-uid process that writes the git common dir can forge
  this execution's sealed records; a role background process that outlives
  the loop can still run `amend --human` once the loop has stopped. This
  port adds none of its own beyond the cwd difference noted above.
- **End-to-end coverage.** `tests/test_e2e.py` (scenario
  `tests/scenarios/acceptance.py`) drives a full fake-opencode loop through a
  real, schema-valid frozen pack run for real by
  `metrics/trio-acceptance.py`'s own sandboxed check execution (only the
  opencode turns are faked): happy path to SHIP (with retirement commits,
  the evaluator's pre-run text and `.opencode-result.json`'s `acceptance`
  summary); a SHIP the frozen gate refuses (verdict becomes ITERATE, the
  errors reach the next Lead plan prompt), fixed in the next iteration;
  coverage refusal recovering after one re-plan, and stopping after a
  second; an author-contamination re-run; and the validation retry for a
  check that passes at base. `tests/test_acceptance.py`'s own unit tests
  cover every piece the orchestration is built from (switch resolution,
  op-call plumbing, the digest/stop/freeze bookkeeping, the canonical-
  fragment loader, the generated role, the audit override) and the
  switch-off identity.

## 12. Known gaps / needs live validation

- **No live provider runs from this branch.** Model ids and the v2 CLI
  surface (`--standalone`, no `--dir`, `-m provider/model#variant`, dotted
  `error.type`) were live-verified by the coordinator; this branch's own
  testing is entirely against `tests/fake_opencode.py`. A full loop has not
  yet run against the real provider from here.
- **v2 permission precedence is unverified.** The binary's precedence for
  its per-action permission maps (first vs. last match wins) was not
  confirmed live; `ocgen.py` writes `"*"` first, specific denies after
  (correct under last-match; wrong under first-match). **Run a live smoke
  test that a builder's `git push` is actually denied before relying on
  this in production.**
- **v2 agent config key names** (`agent.<name>.{description,mode,model,
  prompt,permission,...}`) were verified only by decompiling the binary's
  embedded schema, not by a live round-trip.
- **Lockstep stays single-repo**, matching `native/`'s v0 scope (verified
  in `driver.py`: `declared_repos_for_prepare` is only ever computed when
  `is_open_loop` is true). **Open-loop (`QUEUE.md`) mode is supported**,
  including multi-repo (`PLAN.md` `repos:`) — see "Open-loop mode" above.
- **Open-loop has not yet run against a live provider.** Like the rest of
  this driver (see the top of this section), open-loop's own testing
  (`tests/test_olprompts.py`, `test_quality.py`, `test_olqueue.py`,
  `test_openloop.py`, `test_openloop_e2e.py`) is entirely against
  `tests/fake_opencode.py`; no full open-loop run has exercised a real
  `opencode` binary from this branch.
- **Concurrent builders share one OpenCode data dir per loop**:
  `ocgen.generate` runs once per `run()`, so every builder in a wave shares
  the same `XDG_DATA_HOME`/`OPENCODE_CONFIG_DIR`/session store, not fully
  separate ones.
- **No agent-count or token-budget caps** (matches `native/`'s "no cap by
  default" decision); only `max_iterations` bounds a run.
- **Re-dispatch/conflict budgets are one each per slice** (refused once,
  conflicted once); a second occurrence stops the whole run rather than
  retrying further.
- **`doctor`'s `opencode_models` check** is skipped (not failed) when the
  key file is missing/empty, so a fully offline `doctor` run never
  exercises it.
- **Frozen acceptance (r19).** The switch, the op-call plumbing, the
  digest/stop/freeze bookkeeping, the canonical-fragment loader, the
  generated `trio-acceptance` role and the author-audit override are
  unit-tested (`tests/test_acceptance.py`); a full fake-opencode e2e through
  a real, schema-valid frozen pack to SHIP, a SHIP the frozen gate refuses,
  coverage-refusal re-plan/stop, author contamination and the validation
  retry are covered by `tests/test_e2e.py` against the `acceptance` scenario
  — see "Frozen acceptance (r19)" above. Not covered: a live provider run
  (fake-opencode only, same as the rest of this port) and the amendment/
  tamper paths (already covered at the op level by native's own
  `test_r19_acceptance.py`, which this port's helper calls reuse unchanged).

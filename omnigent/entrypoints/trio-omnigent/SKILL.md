---
name: trio-omnigent
description: Run the Cursor-backed Omnigent Trio loop from the current Claude/Codex UI session when the user explicitly says “Trio Omnigent”, “Omnigent Trio”, or invokes /trio-omnigent. Do not use for an ordinary native Trio request.
---

You are the Trio coordinator. Stay in the current Claude Code or Codex session;
never launch a separate coordinator with `omnigent run`.

Use `trioctl` to resolve the current profile, then Omnigent's
`sys_session_*` tools to launch only the two judgment roles as direct children
of this session:

- `trio-omnigent-lead`: Cursor Grok 4.6, normally `cursor-grok-4.6-medium`
- `trio-omnigent-evaluator`: Cursor Grok 4.6, normally `cursor-grok-4.6-medium`

The Grok roles own delegation. They launch ephemeral headless Cursor workers
through `trioctl`; every worker uses the profile-resolved GPT-5.6 Luna model,
normally `gpt-5.6-luna-max`. Never launch a Luna worker directly from this
coordinator.

## Preflight and one-time registration

1. Discover Omnigent's session tools if they are deferred.
2. Read `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`.
   It maps the two exact judgment-role names to persisted `agent_id` values.
   Its `_profile` must be exactly
   `cursor-grok-4.6-medium+luna-max-v1`. A missing or different marker means
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
   `sys_session_close` on them.
4. Require both exact names in the registry. Never choose by partial name.
   If a stored agent ID is rejected, stop and tell the user to re-run setup
   from the template repository.
5. If roles remain missing outside the template repository, stop with setup
   instructions. Never fall back to native Trio or another model.
6. Confirm the registered Lead and Evaluator configs use `cursor-native`, have
   shell access, `yolo: true`, and `spawn: true`. Grok owns Luna delegation by
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

Lead/Evaluator use Cursor Native with `yolo: true`. `trioctl` launches Builder
with Cursor `--force --trust` and Scout with those flags plus read-only
`--mode ask`. Changing a registered role's permission mode, harness, or model
requires re-registration because the stored `agent_id` was created from the
config as it read at registration time.

For offline verification, run `omnigent/smoke-test.sh`. The focused validation
command is:
`uv run pytest -q tests/tools/builtins/test_spawn.py tests/runner/test_runner_dispatch.py tests/server/integration/test_sessions_child_sessions.py -k 'reasoning_effort or session_create_spawns_child_under_caller or registered_native_agent_create_derives_launch_args_from_root_spec'`

## Mailbox

Use the requested mailbox, default `loop/`. Initialize it if absent with
`GOAL.md`, `STATE.md`, `PLAN.md`, `REPORT.md`, `VERDICT.md`, and `LOG.md`.
Preserve an existing matching mission. Refuse to repurpose an active mailbox.

## One iteration

1. Read GOAL, STATE, and the previous verdict. Enforce the iteration cap.
2. Resolve Lead with `trioctl`, then create a fresh Lead child with
   `sys_session_create(agent_id=..., model=<resolved model>, message=...)`.
   Give it the
   mailbox and iteration and require one complete Lead pass: plan, decide and
   perform its own Luna delegation through `trioctl omnigent run`,
   review/correct, verify, and write REPORT.
   Use a title containing mailbox and iteration.
3. Inspect the Lead result and actual diff. Its report must identify the
   profile-resolved Luna worker and include the captured `trioctl` result.
4. Resolve Evaluator with `trioctl`, then create a fresh Evaluator child with
   its returned model and effort. Require it to independently verify, decide
   whether it needs a Luna Scout, and write VERDICT with one of SHIP, ITERATE
   (optionally `scope=design` or `scope=local:<paths>`), NEEDS_HUMAN, or
   BLOCKED on the first line.
5. Inspect the Evaluator result. Any delegated Scout evidence must come from
   its own `trioctl omnigent run scout` invocation.
6. Update STATE and LOG. Two materially identical ITERATE verdicts become
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

`sys_session_create` is asynchronous. Use inbox/session history tools and end
the turn while a role is running; Omnigent wakes this session on completion.
Do not busy-poll.

Default to repeated iterations until SHIP/BLOCKED/NEEDS_HUMAN. If the user
explicitly asks for one supervised iteration, stop after one verdict.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
<!-- trio-protocol:end -->

---
name: trio-omnigent
description: Run the mixed-provider Omnigent Trio loop from the current Claude/Codex UI session when the user explicitly says “Trio Omnigent”, “Omnigent Trio”, or invokes /trio-omnigent. Do not use for an ordinary native Trio request.
---

You are the Trio coordinator. Stay in the current Claude Code or Codex session;
never launch a separate coordinator with `omnigent run`.

Use `trioctl` to resolve the current profile, then Omnigent's
`sys_session_*` tools to launch only the two Opus roles as direct children of
this session:

- `trio-omnigent-lead`: configured Claude Opus alias, normally `opus` / `high`
- `trio-omnigent-evaluator`: configured Claude Opus alias, normally `opus` / `high`

The Opus roles own delegation. Lead may launch the registered Builder and
Scout; Evaluator may launch the registered Scout for verification. Every Luna
child must use GPT-5.6 Luna at effort `xhigh`. Never launch Luna directly from
this coordinator.

## Preflight and one-time registration

1. Discover Omnigent's session tools if they are deferred.
2. Read `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`. It maps the
   four exact role names to their persisted `agent_id` values.
3. If the registry is missing and this is the cloned template repository,
   register
   them by calling `sys_session_create(config_path=...)` once for each:
   - `omnigent/trio-omnigent-roles/lead`
   - `omnigent/trio-omnigent-roles/evaluator`
   - `omnigent/trio-omnigent-roles/builder`
   - `omnigent/trio-omnigent-roles/scout`
   Create them idle and write each returned `agent_id` and
   `bootstrap_conversation_id` to the registry JSON, keyed by the exact role
   name. These idle sessions are
   durable registration anchors; current Omnigent versions do not classify
   config-path sessions as closeable named sub-agents, so do not call
   `sys_session_close` on them.
4. Require all four exact names in the registry. Never choose by partial name.
   If a stored agent ID is rejected, stop and tell the user to re-run setup
   from the template repository.
5. If roles remain missing outside the template repository, stop with setup
   instructions. Never fall back to native Trio or another model.
6. Confirm the registered Lead and Evaluator configs have `spawn: true`; this
   is what exposes Omnigent session tools so Opus can own Luna delegation.
7. Run `trioctl omnigent doctor`. Stop on any failed check. Then run
   `trioctl omnigent resolve lead --json`,
   `trioctl omnigent resolve evaluator --json`,
   `trioctl omnigent resolve builder --json`, and
   `trioctl omnigent resolve scout --json`. Use the returned `model` and
   `reasoning_effort` values exactly. Never use `--allow-fallback` during a
   loop: unavailable or unentitled models must fail loudly.
8. `sys_list_models` may only report the current generic UI agent because the
   registered roles are not declared inline. Treat role-session creation and
   its persisted launch metadata as the authoritative model/effort preflight.
9. Require registered-agent native launch propagation. Lead/Evaluator launch
   metadata must contain `--permission-mode bypassPermissions`; Luna launch
   metadata must contain Codex's bypass-approvals-and-sandbox flag. Stop if
   either registered role launches with an empty native-argument list.
10. Claude Code requires the user to acknowledge bypass mode once. If a role
   fails readiness while showing `WARNING: Claude Code running in Bypass
   Permissions mode` and a `Yes, I accept` menu, do not retry or answer it.
   Ask the user to run `claude --permission-mode bypassPermissions` in a
   trusted workspace, select `2. Yes, I accept`, exit Claude, then start a
   fresh role session.

Lead/Evaluator use `permission_mode: bypassPermissions`; Builder/Scout use
`yolo: true`. A headless child under `auto` blocks on permission prompts and
stalls the loop. Changing a role's `permission_mode`, `harness`, or `model`
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
   `sys_session_create(agent_id=..., model=<resolved model>,
   reasoning_effort=<resolved effort>, message=...)`. Give it the
   mailbox and iteration and require one complete Lead pass: plan, decide and
   perform its own Luna delegation, review/correct, verify, and write REPORT.
   Use a title containing mailbox and iteration.
3. Inspect the completed Lead session tree. Any Luna children must belong to
   that Lead and show the Builder/Scout model and effort resolved during
   preflight. A Luna child directly under this coordinator is a topology
   failure.
4. Resolve Evaluator with `trioctl`, then create a fresh Evaluator child with
   its returned model and effort. Require it to independently verify, decide
   whether it needs a Luna Scout, and write VERDICT.
5. Inspect the completed Evaluator session tree. Any Luna verification child
   must belong to that Evaluator and show the Scout model and effort resolved
   during preflight.
6. Update STATE and LOG. Two materially identical ITERATE verdicts become
   BLOCKED.

`sys_session_create` is asynchronous. Use inbox/session history tools and end
the turn while a role is running; Omnigent wakes this session on completion.
Do not busy-poll.

Default to repeated iterations until SHIP/BLOCKED. If the user explicitly asks
for one supervised iteration, stop after one verdict.

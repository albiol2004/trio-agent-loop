# Set up Trio through Omnigent

This integration is separate from the native Claude Code and Codex installs:

- Omnigent role sources: `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles`
- Claude entrypoint: `~/.claude/skills/trio-omnigent`
- Codex entrypoint: `~/.agents/skills/trio-omnigent`
- Claude Code: `~/.claude/{agents,skills}`
- Codex: `~/.codex/agents` and `~/.agents/skills`

Installing or removing one does not overwrite the others.

## Role mapping

| Role | Harness | Model | Effort |
|---|---|---|---|
| Lead | `claude-native` | `claude-opus-5` | `high` |
| Evaluator | `claude-native` | `claude-opus-5` | `high` |
| Builder | `codex-native` | `gpt-5.6-luna` | `xhigh` |
| Scout | `codex-native` | `gpt-5.6-luna` | `xhigh` |

The Claude or Codex session already open in Omnigent schedules iterations. It
creates only Lead and Evaluator as direct Opus/high children. Lead decides when
to launch Luna/xhigh Builder or Scout children; Evaluator decides when it needs
a Luna/xhigh Scout for independent verification. There is no additional
coordinator model, and the root session never delegates implementation
directly to Luna.

Lead/Evaluator run with `permission_mode: bypassPermissions`, while
Builder/Scout run with `yolo: true`: a headless child under `auto` blocks on
permission prompts and stalls the loop. Changing a role's `permission_mode`,
`harness`, or `model` requires re-registration because the stored `agent_id`
was created from the config as it read at registration time.

## Prerequisites

1. Omnigent with child-effort dispatch support:
   `sys_session_send.args.reasoning_effort` and
   `sys_session_create.reasoning_effort`, plus registered-agent native launch
   propagation so role YAML reaches Claude/Codex permission flags.
2. `claude` and `codex` on `PATH`.
3. Claude and Codex subscription providers configured with `omnigent setup`.
4. The four model/effort combinations above available to those providers.
5. Claude Code's one-time bypass-permissions acknowledgement completed by the
   user in a trusted workspace:

   ```bash
   claude --permission-mode bypassPermissions
   ```

   Select `2. Yes, I accept`, then exit Claude. This consent must not be
   automated. Without it, a headless Opus role times out before its input is
   delivered while Claude displays `WARNING: Claude Code running in Bypass
   Permissions mode`.

When using a patched source checkout, make the normal `omnigent` command use
that checkout before installing the bundle:

```bash
OMNIGENT_SOURCE=/path/to/omnigent ./install.sh --omnigent
omnigent --version
```

This applies `omnigent/patches/child-reasoning-effort.patch` idempotently,
installs that checkout through `uv tool install --editable`, verifies the live
tool schema, and installs the bundle plus both entrypoints. Without
`OMNIGENT_SOURCE`, the installer leaves Omnigent itself untouched but still
fails loudly if the active version lacks the required schema.

## Set it up from an Omnigent UI session

Open the cloned repository as the working directory of either an underlying
Claude Code or Codex session and say:

> Follow SETUP-BY-OMNIGENT.md and install Trio Omnigent completely.

The agent should:

1. Verify that `omnigent`, `claude`, and `codex` are on `PATH`.
2. Verify that the installed Omnigent exposes child `reasoning_effort` and
   registered-agent native permission propagation.
3. Run `./install.sh --omnigent`.
4. Discover Omnigent's deferred `sys_session_create`, `sys_session_close`, and
   `sys_agent_list` tools.
5. Register each role once by creating an idle child from these relative paths:
   - `omnigent/trio-omnigent-roles/lead`
   - `omnigent/trio-omnigent-roles/evaluator`
   - `omnigent/trio-omnigent-roles/builder`
   - `omnigent/trio-omnigent-roles/scout`
6. Write the exact returned `agent_id` and `bootstrap_conversation_id` values to
   `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`, keyed by
   `trio-omnigent-{lead,evaluator,builder,scout}`. Leave the idle bootstrap
   sessions in place as registration anchors; `sys_session_close` currently
   rejects config-path-created sessions as `session_not_a_sub_agent`.
7. Verify all four exact names and IDs are present in the registry.
8. Ask the user to complete Claude Code's one-time bypass-permissions
   acknowledgement shown under Prerequisites. Do not select the consent answer
   for them.
9. Verify the registered Lead and Evaluator configs have `spawn: true`, which
   lets those Opus sessions create their own registered Luna children.
10. Tell you to start a new underlying Claude/Codex session so its skill catalog
   includes the installed entrypoint.

The setup operation must not launch a billable Trio loop unless you separately
ask for a real smoke run.

## Manual installation

```bash
./install.sh --omnigent
```

Start a new underlying Claude or Codex session in the target project. You can
now say:

> Run a Trio Omnigent loop to add config-driven rate limiting to the public API.

The `trio-omnigent` skill keeps that already-open session as the iteration
scheduler. It launches Opus Lead and Evaluator; those Opus roles resolve and
launch their own registered Luna children with explicit `xhigh` effort. It runs
until SHIP/BLOCKED by default. Say “one supervised iteration” to stop after one
verdict.

Ordinary “run a Trio loop” remains native Claude/Codex Trio. The word
“Omnigent” is the explicit routing signal; the entrypoint must never silently
fall back to native Trio.

`OMNIGENT_HOME=/custom/path ./install.sh --omnigent` selects another Omnigent
home. Re-running the installer updates only the Trio-owned role sources and
entrypoint skills. Re-register the roles after changing their configs.

Use a different mailbox such as `loop-auth` for a concurrent mission. Never
point two live runs at one mailbox.

## Why the Omnigent patch is required

Omnigent already stored `reasoning_effort` on sessions and translated it to
Claude's `--effort` or Codex reasoning configuration at launch. Previously,
the child tools exposed `model` but not `reasoning_effort`, so an orchestrator
could not choose effort by role. Registered `agent_id` launches also skipped
the role's native launch configuration, dropping Claude
`bypassPermissions` and Codex `yolo`. The patch completes both existing paths.
Effort is creation-only and cannot change on a continued child.

## Validate

```bash
cd /path/to/omnigent
uv run pytest -q tests/tools/builtins/test_spawn.py tests/runner/test_runner_dispatch.py tests/server/integration/test_sessions_child_sessions.py -k 'reasoning_effort or session_create_spawns_child_under_caller or registered_native_agent_create_derives_launch_args_from_root_spec'
```

For repeatable offline checks, run `omnigent/smoke-test.sh` from this template
repository.

Do a short real smoke run and inspect the child sessions. Lead/Evaluator must
show Opus/high; Builder/Scout must show Luna/xhigh. Stop if a role falls back
to defaults.

For a real smoke run, inspect the UI session tree: the current session must
remain the root, with Lead/Evaluator as direct Opus/high children. Builder and
Scout must be Luna/xhigh children of the Opus role that chose to delegate.
A root-launched Luna child or any Sonnet coordinator is a failure.

If an Opus child fails readiness with the bypass-permissions warning and a
`Yes, I accept` menu, stop retrying. Complete the one-time Claude command in
Prerequisites manually, then launch a fresh Trio iteration.

## Remove

Remove `~/.omnigent/agents/trio-omnigent-roles`,
`~/.claude/skills/trio-omnigent`, and
`~/.agents/skills/trio-omnigent`. The native Claude and Codex Trio
installations remain intact.

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
| Lead | `claude-native` | Claude `opus` alias | `high` |
| Evaluator | `claude-native` | Claude `opus` alias | `high` |
| Builder | headless `cursor-agent` | `cursor-grok-4.5-high` | `high` in model ID |
| Scout | headless `cursor-agent --mode ask` | `cursor-grok-4.5-high` | `high` in model ID |

The Claude or Codex session already open in Omnigent schedules iterations. It
creates only Lead and Evaluator as direct Opus/high children. Lead decides when
to run a Cursor Grok Builder or Scout through `trioctl`; Evaluator decides when
it needs a read-only Grok Scout. There is no additional coordinator model, and
the root session never delegates implementation directly to Grok.

`trioctl` owns this runtime mapping. Claude Code documents `opus` as a moving
alias. Cursor exposes account-entitled model IDs through `cursor-agent models`,
so `trioctl` requires the non-fast Grok 4.5 High variant before launching.
Cursor encodes effort in the model ID. `trioctl` invokes Cursor's supported
headless print mode and captures the worker result for Opus. It never silently
substitutes Auto, Composer, another provider, or native Trio.

Lead/Evaluator run with `permission_mode: bypassPermissions`. `trioctl` invokes
Builder with Cursor `--force --trust` and invokes Scout with those flags plus
read-only `--mode ask`. Changing an Opus role's `permission_mode` or harness
requires re-registration because its stored `agent_id` was created from the
config as read at registration time. Cursor model and effort profile changes
do not require registration.

## Prerequisites

1. Omnigent with child-effort dispatch support:
   `sys_session_send.args.reasoning_effort` and
   `sys_session_create.reasoning_effort`, plus registered-agent native launch
   propagation so role YAML reaches Claude permission flags.
2. `claude` and `cursor-agent` on `PATH`; `codex` is also required when the
   current Omnigent coordinator session uses Codex.
3. Claude configured for Omnigent and `cursor-agent status` logged in.
4. The four model/effort combinations above available to those accounts.
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
tool schema, and installs the bundle, both entrypoints, `trioctl`, and its
user-owned profile. Without
`OMNIGENT_SOURCE`, the installer leaves Omnigent itself untouched but still
fails loudly if the active version lacks the required schema.

The default profile is
`${XDG_CONFIG_HOME:-~/.config}/trio-agent-loop/omnigent.toml`. The installer
creates it once and preserves it on later installs. `TRIOCTL_CONFIG` selects
another file and `TRIOCTL_BIN_DIR` selects another install directory.

## Manage the runtime profile with trioctl

```bash
trioctl omnigent configure                # create the profile if absent
trioctl omnigent models                   # live Cursor worker catalog
trioctl omnigent models --provider all    # include Codex too
trioctl omnigent resolve lead --json
trioctl omnigent resolve builder --json
printf '%s\n' 'Reply with exactly SCOUT_OK.' |
  trioctl omnigent run scout --workspace .
trioctl omnigent doctor                   # commands, profile, models, registry
```

Edit the TOML profile to change a role's alias, model family, exact model, or
effort. The next child uses the new values; role re-registration is unnecessary
for model-only changes.

To migrate a profile created by the older Luna default to Cursor Grok, replace
it with the repository default and review the resulting TOML:

```bash
trioctl omnigent configure --force
```

If an earlier Cursor-native Trio build registered persistent Builder or Scout
anchors, remove those two entries from `registry.json` and restart Omnigent
once. The restart terminates their old Cursor TUI processes; future Grok
workers are ephemeral headless commands and need no registration or restart.
`trioctl omnigent doctor` fails while either obsolete entry remains.

Resolution is intentionally strict. A missing Cursor catalog, missing Grok 4.5
High entitlement, or unsupported selection exits non-zero. `--allow-fallback` opts into
the profile's exact fallback slug for diagnostics or recovery, but the
`trio-omnigent` skill never uses it automatically.

## Update the Omnigent source checkout

After the initial setup, update the default `$HOME/omnigent` checkout and reapply the Trio
compatibility patch with:

```bash
./omnigent/update-omnigent.sh
```

Pass another checkout explicitly when needed:

```bash
./omnigent/update-omnigent.sh /path/to/omnigent
```

The updater reverses only the known Trio patch, runs `git pull --ff-only`, and
then reapplies it. It leaves unrelated tracked changes and untracked files
alone. If the pull fails, it attempts to restore the patch automatically. If a
new upstream revision is incompatible with the bundled patch, it stops with a
clear error instead of guessing. It does not reinstall Omnigent, restart a
server, or modify role registrations.

## Set it up from an Omnigent UI session

Open the cloned repository as the working directory of either an underlying
Claude Code or Codex session and say:

> Follow SETUP-BY-OMNIGENT.md and install Trio Omnigent completely.

The agent should:

1. Verify that `omnigent`, `claude`, and `cursor-agent` are on `PATH`, and run
   `cursor-agent status`.
2. Verify that the installed Omnigent exposes child `reasoning_effort` and
   registered-agent native permission propagation.
3. Run `./install.sh --omnigent`.
4. Run `trioctl omnigent models` and resolve all four roles. Stop if Grok or
   the configured effort is unavailable.
5. Discover Omnigent's deferred `sys_session_create`, `sys_session_close`, and
   `sys_agent_list` tools.
6. Register only the two Opus roles by creating an idle child from:
   - `omnigent/trio-omnigent-roles/lead`
   - `omnigent/trio-omnigent-roles/evaluator`
7. Write the exact returned `agent_id` and `bootstrap_conversation_id` values to
   `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`, keyed by
   `trio-omnigent-{lead,evaluator}`. Leave the idle bootstrap
   sessions in place as registration anchors; `sys_session_close` currently
   rejects config-path-created sessions as `session_not_a_sub_agent`.
8. Verify both exact names and IDs are present in the registry. Remove legacy
   Builder/Scout entries; they are no longer registration anchors.
9. Ask the user to complete Claude Code's one-time bypass-permissions
   acknowledgement shown under Prerequisites. Do not select the consent answer
   for them.
10. Verify Lead and Evaluator have `spawn: true`, then run a short
    `trioctl omnigent run scout` smoke test and confirm its text is captured.
11. Run `trioctl omnigent doctor`; all checks must pass.
12. Tell you to start a new underlying Claude/Codex session so its skill catalog
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
scheduler. It resolves the profile at runtime and launches Opus Lead and
Evaluator; those Opus roles independently resolve and run ephemeral headless
Cursor Grok 4.5 High workers. It runs until
SHIP/BLOCKED by default. Say “one supervised iteration” to stop after one
verdict.

Ordinary “run a Trio loop” remains native Claude/Codex Trio. The word
“Omnigent” is the explicit routing signal; the entrypoint must never silently
fall back to native Trio.

`OMNIGENT_HOME=/custom/path ./install.sh --omnigent` selects another Omnigent
home. Re-running the installer updates only the Trio-owned role sources and
entrypoint skills. Re-register the Opus roles after changing their configs.

Use a different mailbox such as `loop-auth` for a concurrent mission. Never
point two live runs at one mailbox.

## Why the Omnigent patch is required

Omnigent already stored `reasoning_effort` on sessions and translated it to
Claude's `--effort` or Codex reasoning configuration at launch. Previously,
the child tools exposed `model` but not `reasoning_effort`, so an orchestrator
could not choose effort by Opus role. Registered `agent_id` launches also
skipped the role's native launch configuration, dropping Claude
`bypassPermissions`. The patch completes both existing paths.
Effort is creation-only and cannot change on a continued child.

The compatibility patch remains additive while released Omnigent builds lack
the complete path. Upstream issue
[#2080](https://github.com/omnigent-ai/omnigent/issues/2080) and approved PR
[#2099](https://github.com/omnigent-ai/omnigent/pull/2099) cover per-session
model/effort overrides; issue
[#2800](https://github.com/omnigent-ai/omnigent/issues/2800) tracks top-level
custom native-agent effort and permission propagation. Do not open duplicate
PRs. Once a released Omnigent version satisfies `trioctl omnigent doctor` and
the installer probes without patching, the compatibility patch can be retired.

## Validate

```bash
cd /path/to/omnigent
uv run pytest -q tests/tools/builtins/test_spawn.py tests/runner/test_runner_dispatch.py tests/server/integration/test_sessions_child_sessions.py -k 'reasoning_effort or session_create_spawns_child_under_caller or registered_native_agent_create_derives_launch_args_from_root_spec'
```

For repeatable offline checks, run `omnigent/smoke-test.sh` from this template
repository.

For CLI unit tests:

```bash
python3 -m pytest -q omnigent/tests/test_trioctl.py
```

Do a short real smoke run. Lead/Evaluator must show the profile-resolved
Opus/high pair. Their captured `trioctl` output must identify the
profile-resolved Cursor Grok 4.5 High worker.

For a real smoke run, inspect the UI session tree: the current session must
remain the root, with Lead/Evaluator as direct Opus/high children. Builder and
Scout do not appear as persistent Omnigent children; the Opus role's command
history and report must show its own `trioctl omnigent run` invocation. A
root-launched Grok process or any Sonnet coordinator is a failure.

If an Opus child fails readiness with the bypass-permissions warning and a
`Yes, I accept` menu, stop retrying. Complete the one-time Claude command in
Prerequisites manually, then launch a fresh Trio iteration.

## Remove

Remove `~/.omnigent/agents/trio-omnigent-roles`,
`~/.claude/skills/trio-omnigent`, and
`~/.agents/skills/trio-omnigent`. The native Claude and Codex Trio
installations remain intact.

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
| Lead | `cursor-native` | `cursor-grok-4.6-medium` | `medium` in model ID |
| Evaluator | `cursor-native` | `cursor-grok-4.6-medium` | `medium` in model ID |
| Builder | headless `cursor-agent` | `gpt-5.6-luna-max` | `max` in model ID |
| Scout | headless `cursor-agent --mode ask` | `gpt-5.6-luna-max` | `max` in model ID |

The Claude or Codex session already open in Omnigent schedules iterations. It
creates only Lead and Evaluator as direct Grok 4.6 Medium children. Lead decides
when to run a Cursor GPT-5.6 Luna Max Builder or Scout through `trioctl`;
Evaluator decides when it needs a read-only Luna Scout. There is no additional
coordinator model, and the root session never delegates implementation directly
to Luna.

`trioctl` owns this runtime mapping. Cursor exposes account-entitled model IDs
through `cursor-agent models`, so `trioctl` requires the non-fast Grok 4.6
Medium and GPT-5.6 Luna Max variants before launching.
Cursor encodes effort in the model ID. `trioctl` invokes Cursor's supported
headless print mode and captures the worker result for Grok. It never silently
substitutes Auto, Composer, another provider, or native Trio.

Lead/Evaluator run through `cursor-native` with `yolo: true`. `trioctl` invokes
Builder with Cursor `--force --trust` and invokes Scout with those flags plus
read-only `--mode ask`. Changing a registered role's model, `yolo`, or harness
requires re-registration because its stored `agent_id` was created from the
config as read at registration time. Builder/Scout model and effort profile
changes do not require registration because those workers are ephemeral.

## Prerequisites

1. Omnigent with child-effort dispatch support:
   `sys_session_create.reasoning_effort`, plus registered-agent native launch
   propagation so role YAML reaches Cursor's `--yolo` launch flag.
2. `cursor-agent` on `PATH`; `claude` or `codex` is also required only for the
   corresponding current Omnigent coordinator session.
3. `cursor-agent status` logged in.
4. The four model/effort combinations above available to those accounts.

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

To migrate an existing profile to Grok 4.6 Medium and Luna Max, replace it with
the repository default and review the resulting TOML:

```bash
trioctl omnigent configure --force
```

If an earlier Cursor-native Trio build registered persistent Builder or Scout
anchors, remove those two entries from `registry.json` and restart Omnigent
once. The restart terminates their old Cursor TUI processes; future Luna
workers are ephemeral headless commands and need no registration or restart.
`trioctl omnigent doctor` fails while either obsolete entry remains.

Resolution is intentionally strict. A missing Cursor catalog, missing Grok 4.6
Medium or Luna Max entitlement, or unsupported selection exits non-zero. `--allow-fallback` opts into
the profile's exact fallback slug for diagnostics or recovery, but the
`trio-omnigent` skill never uses it automatically.

## Update the Omnigent source checkout

The bundled compatibility patch is verified against the stable `v0.9.0`
release. For a stable installation, keep the Omnigent checkout on a local
branch rooted at the release tag:

```bash
cd /path/to/omnigent
git fetch origin --tags
git switch -c trio-v0.9.0 v0.9.0
```

Do not run `git pull` on that pinned branch: release tags do not advance. Move
to a new release deliberately, after preserving any local commits and checking
that the bundled patch applies to the new tag.

After the initial setup, update the default `$HOME/omnigent` checkout and reapply the Trio
compatibility patch with:

```bash
./omnigent/update-omnigent.sh
```

Pass another checkout explicitly when needed:

```bash
./omnigent/update-omnigent.sh /path/to/omnigent
```

Use this updater only for an Omnigent branch that tracks an upstream branch
(such as `main`), not for the pinned stable branch above. The updater reverses
only the known Trio patch, runs `git pull --ff-only`, and
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

1. Verify that `omnigent` and `cursor-agent` are on `PATH`, and run
   `cursor-agent status`.
2. Verify that the installed Omnigent exposes session-create
   `reasoning_effort` and registered-agent native permission propagation.
3. Run `./install.sh --omnigent`.
4. Run `trioctl omnigent models` and resolve all four roles. Stop if Grok 4.6
   Medium, Luna Max, or a configured effort is unavailable.
5. Discover Omnigent's deferred `sys_session_create`, `sys_session_close`, and
   `sys_agent_list` tools.
6. Back up a registry whose `_profile` is not
   `cursor-grok-4.6-medium+luna-max-v1`, then register only the two judgment
   roles by creating an idle child from:
   - `omnigent/trio-omnigent-roles/lead`
   - `omnigent/trio-omnigent-roles/evaluator`
7. Write `_profile: cursor-grok-4.6-medium+luna-max-v1` plus the exact returned
   `agent_id` and `bootstrap_conversation_id` values to
   `${OMNIGENT_HOME:-~/.omnigent}/agents/trio-omnigent-roles/registry.json`, keyed by
   `trio-omnigent-{lead,evaluator}`. Leave the idle bootstrap
   sessions in place as registration anchors; `sys_session_close` currently
   rejects config-path-created sessions as `session_not_a_sub_agent`.
8. Verify both exact names and IDs are present in the registry. Remove legacy
   Builder/Scout entries; they are no longer registration anchors.
9. Verify Lead and Evaluator use `cursor-native`, `yolo: true`, and
   `spawn: true`, then run a short
    `trioctl omnigent run scout` smoke test and confirm its text is captured.
10. Run `trioctl omnigent doctor`; all checks must pass.
11. Tell you to start a new underlying Claude/Codex session so its skill catalog
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
scheduler. It resolves the profile at runtime and launches Grok 4.6 Medium Lead
and Evaluator; those roles independently resolve and run ephemeral headless
Cursor GPT-5.6 Luna Max workers. It runs until
SHIP/BLOCKED by default. Say “one supervised iteration” to stop after one
verdict.

Ordinary “run a Trio loop” remains native Claude/Codex Trio. The word
“Omnigent” is the explicit routing signal; the entrypoint must never silently
fall back to native Trio.

`OMNIGENT_HOME=/custom/path ./install.sh --omnigent` selects another Omnigent
home. Re-running the installer updates only the Trio-owned role sources and
entrypoint skills. Re-register the Lead/Evaluator roles after changing their
configs.

Use a different mailbox such as `loop-auth` for a concurrent mission. Never
point two live runs at one mailbox.

## Why the Omnigent patch is required

Omnigent already stored `reasoning_effort` on sessions and translated it to
native harness configuration at launch. Previously, the session-create tool
exposed `model` but not `reasoning_effort`. Registered `agent_id` launches also
skipped the role's native launch configuration, dropping Cursor's `--yolo`.
The patch completes both existing paths. The current all-Cursor profile encodes
effort in exact model IDs, while retaining the general effort seam for custom
profiles.
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

Do a short real smoke run. Lead/Evaluator must show the profile-resolved Cursor
Grok 4.6 Medium pair. Their captured `trioctl` output must identify the
profile-resolved Cursor GPT-5.6 Luna Max worker.

For a real smoke run, inspect the UI session tree: the current session must
remain the root, with Lead/Evaluator as direct Grok 4.6 Medium children. Builder
and Scout do not appear as persistent Omnigent children; the Grok role's command
history and report must show its own `trioctl omnigent run` invocation. A
root-launched Luna process or any extra coordinator is a failure.

## Remove

Remove `~/.omnigent/agents/trio-omnigent-roles`,
`~/.claude/skills/trio-omnigent`, and
`~/.agents/skills/trio-omnigent`. The native Claude and Codex Trio
installations remain intact.

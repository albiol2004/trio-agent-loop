# Codex Trio troubleshooting

## Native agents are installed but cannot be spawned

Observed behavior in some Codex app tasks:

- the `multi_agent` feature is enabled;
- custom agent TOML files load correctly;
- the active permission profile allows the required repositories;
- the task still exposes no native subagent spawn/control capability.

Permissions and agent availability are separate. A permission profile controls
what a spawned agent may read, write, execute, or reach over the network. It
cannot add a spawn tool that the current task runtime did not provide.

Trio handles this condition with a capability preflight:

1. Prefer native `trio-scout`, `trio-lead`, `trio-builder`, and
   `trio-evaluator` custom agents.
2. If native controls are absent, announce fallback mode once.
3. Run the same roles as separate ephemeral bundled Codex CLI sessions.
4. Preserve the mailbox, model tiers, project permissions, stop conditions,
   and no-commit rule.

The fallback is not the portable shell-driver implementation and is not
single-agent role-play. It is a Codex-specific compatibility path for app
tasks whose callable tool surface lacks native subagent controls.

## Permission guidance

Use project-scoped `.codex/config.toml` permission profiles for multi-repository
work. Keep repository roots explicit, use automatic approval review when
autonomy is desired, and avoid `danger-full-access`.

Start from the installed Trio skill's
`references/PROJECT-CONFIG.example.toml`. The current project is already an
effective workspace root; add only sibling repositories that the workflow must
read or edit. Keep private paths in the local project config, which may be
ignored by Git when it is personal workstation configuration.

Named permission profiles and legacy sandbox settings are separate
configuration systems. If any loaded config layer contains `sandbox_mode` or
`[sandbox_workspace_write]`, those legacy settings take precedence over
`default_permissions` and `[permissions.*]`. Remove the legacy settings before
expecting a named project profile to apply.

Fallback child sessions inherit the active project configuration. The runner
does not pass sandbox-bypass flags or replace the selected permission profile.
In a non-interactive child session, an operation that cannot be completed
inside the active policy may fail and return a blocking result to the parent.

## Diagnostics

Check:

```bash
codex --version
codex features list
codex doctor --json --all
```

Verify:

- `multi_agent` is enabled;
- the project is trusted;
- the combined configuration loads;
- the active profile includes every repository Trio must modify;
- the installed Trio skill and custom-agent directories are readable;
- custom agents exist under `~/.codex/agents/`;
- the installed Trio skill's `scripts/run-role.sh` is executable;
- the task either exposes native subagent controls or clearly announces CLI
  fallback mode.

## Fresh-clone recovery sequence

If `Run a trio loop on ...` does not proceed:

1. Confirm the bundle was installed with `./install.sh --codex`.
2. Compare the target project config with the installed Trio skill's
   `references/PROJECT-CONFIG.example.toml`.
3. Check the merged config with `codex doctor --json --all`.
4. Restart Codex and create a new task.
5. Ask for one native read-only subagent as a capability test.
6. If native controls are still absent, verify that Trio announces isolated
   CLI fallback and that the fallback runner is executable.
7. If a fallback role fails, inspect its result and the project permission
   profile. Do not replace it with role-play.

When reporting an app issue, include the Codex version, app build, task
surface, whether native controls were absent, and the relevant diagnostic
statuses. Do not include credentials, private repository contents, usernames,
or absolute personal paths.

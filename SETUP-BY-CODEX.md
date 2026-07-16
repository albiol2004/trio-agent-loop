# Setup instructions for Codex

Install the Codex bundle:

```bash
./install.sh --codex
```

This copies the Trio skills to `~/.agents/skills/`, the four custom agents to
`~/.codex/agents/`, and the isolated fallback runner inside the installed
Trio skill. It does not install the portable driver or create model profiles.

## Fresh-clone setup contract

When an agent is asked to set up Trio, it must complete these checks instead
of stopping after copying files:

1. Run `codex features list` and confirm `multi_agent` is enabled. If needed,
   add `multi_agent = true` to the existing `[features]` table in
   `~/.codex/config.toml`. Do not create a duplicate `[features]` table.
2. Run `codex doctor --json --all` and confirm the combined user and project
   configuration loads.
3. Inspect the target project's repository topology. If Trio needs sibling
   repositories, create or update the trusted target project's
   `.codex/config.toml` from
   `codex/skills/trio/references/PROJECT-CONFIG.example.toml`, granting only
   the required roots.
4. Prefer `approval_policy = "on-request"` with
   `approvals_reviewer = "auto_review"` for autonomous routine work with
   reviewed boundary crossings.
5. Ensure the project profile can read the installed Trio skills and custom
   agent definitions. The example config contains those read grants.
6. Do not mix named permission profiles (`default_permissions` and
   `[permissions.*]`) with legacy `sandbox_mode` or
   `[sandbox_workspace_write]` settings in any loaded config layer. Codex uses
   the legacy settings when both are present.
7. Restart Codex and start a new task after changing configuration or
   installing skills.

Never overwrite an existing config blindly. Read it, preserve unrelated
settings, merge tables, and explain any permission roots added. Keep
project-specific paths in the target project's local `.codex/config.toml`, not
in the public Trio repository or a global user config.

Start a new Codex task and say:

```text
Run a trio loop on <task>.
```

For long work, `/goal Run a trio loop on <task> until SHIP or BLOCKED` keeps
the outer task persistent. The Trio skill uses Codex's native multi-agent
tools and exact named custom agents:

- Terra High: `trio-lead`, post-Builder Lead review, `trio-evaluator`.
- Luna High: `trio-scout`, `trio-builder`, evaluator reconnaissance.

The main Codex task owns every spawn. Codex's default `agents.max_depth = 1`
lets the main task spawn direct role agents while preventing those agents from
spawning descendants. Keep that default.

Codex prefers native custom agents. Some app tasks may have `multi_agent`
enabled and load the custom-agent files while still not exposing native
spawn/control tools. When that happens, Trio announces CLI fallback mode and
uses separate ephemeral `codex exec` sessions for the same roles. These runs:

- use the existing Codex authentication;
- inherit the project's Codex permission profile;
- keep Terra/Luna model pinning;
- never use `danger-full-access` or bypass flags;
- remain separate from the legacy portable driver.

See `codex/skills/trio/references/TROUBLESHOOTING.md` for capability and
permission diagnostics. The loop never commits.

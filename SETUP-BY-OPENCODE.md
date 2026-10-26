# Set up Trio with OpenCode

This repository ships two OpenCode integrations sharing the same `loop/`
mailbox:

- the **native in-session bundle** (`opencode/`) with project/global markdown
  agents, named `Task` children, and routed `/trio` commands; and
- the **standalone `trio-opencode` driver** (`opencode-driver/`), a Python
  state machine that spawns one `opencode run` process per role. It is the
  recommended path for automated and unattended loops, including open-loop
  `QUEUE.md` runs, root-free worktrees, and frozen acceptance.

The native bundle is for interactive supervised sessions. The older
`portable/driver.sh` path remains a compatibility fallback for an OpenCode
installation that lacks the native Task surface.

The links below are the official references used for this guide:

- [OpenCode agents](https://opencode.ai/docs/agents/)
- [OpenCode configuration](https://opencode.ai/docs/config/)
- [OpenCode commands](https://opencode.ai/docs/commands/)
- [OpenCode permissions](https://opencode.ai/docs/permissions/)
- [OpenCode tools](https://opencode.ai/docs/tools/)
- [OpenCode plugins](https://opencode.ai/docs/plugins/)
- [OpenCode MCP servers](https://opencode.ai/docs/mcp-servers/)
- [OpenCode agent skills](https://opencode.ai/docs/skills/)
- [OpenCode custom tools](https://opencode.ai/docs/custom-tools/)
- [OpenCode CLI](https://opencode.ai/docs/cli/)
- [OpenCode authentication](https://opencode.ai/docs/auth/)
- [OpenCode source repository](https://github.com/anomalyco/opencode)

## 1. Install OpenCode and authenticate

Starting from a fresh clone, install OpenCode using the method in the [official
installation documentation](https://opencode.ai/docs/). Do not run an
installer or authenticate from this setup guide automatically. When you are
ready to sign in, use the documented interactive command:

```bash
opencode auth login
```

Check which provider/model identifiers are available to your account before
configuring Trio:

```bash
opencode models
```

The native bundle intentionally selects no provider or paid model by default.
The standalone driver ships its own config defaults (section 3), and you can
edit or replace them.

## 2. Install the native bundle and driver

From the clone, `--opencode` installs the native in-session bundle **and** the
standalone `trio-opencode` driver. If the user has supplied exact strong and
light model IDs, apply them during installation:

```bash
./install.sh --opencode \
  --strong-model '<provider/strong-model>' \
  --light-model '<provider/light-model>'
```

To install only the standalone driver, use the same options with
`--opencode-driver`:

```bash
./install.sh --opencode-driver \
  --strong-model '<provider/strong-model>' \
  --light-model '<provider/light-model>'
```

An agent performing this setup must ask the user for both values when they are
not already present in the request or environment. It must not choose, infer,
or substitute a provider/model on the user's behalf. Use exact identifiers
returned by the user's `opencode models` command. Supplying only one tier fails
the installer. If the user explicitly wants normal OpenCode inheritance, omit
both model options:

```bash
./install.sh --opencode
```

The native bundle creates only Trio-owned files under
`~/.config/opencode/agents/`, `~/.config/opencode/commands/`, and
`~/.config/opencode/opencode.trio.example.jsonc` (or the equivalent
`$XDG_CONFIG_HOME/opencode/` tree). Existing files are preserved and no
credentials or unrelated harness directories are created.

The driver is installed as a self-contained runtime tree at
`${XDG_DATA_HOME:-~/.local/share}/trio-agent-loop/opencode-driver` with a
`trio-opencode` command in `${TRIO_OPENCODE_BIN_DIR:-~/.local/bin}`. Its user
config is
`${TRIO_OPENCODE_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/trio-opencode/config.json}`;
an existing config is preserved. Override `TRIO_OPENCODE_HOME`,
`TRIO_OPENCODE_BIN_DIR`, or `TRIO_OPENCODE_CONFIG` to relocate those paths.

For a project-only installation of the native bundle, copy it from the clone
instead (the driver is normally installed user-wide):

```bash
mkdir -p .opencode/agents .opencode/commands
cp opencode/agents/*.md .opencode/agents/
cp opencode/commands/*.md .opencode/commands/
./opencode/configure-models.sh --project "$PWD" \
  --strong-model '<provider/strong-model>' \
  --light-model '<provider/light-model>'
```

OpenCode discovers project agents and commands in `.opencode/agents/` and
`.opencode/commands/`; global agents and commands live in
`~/.config/opencode/agents/` and `~/.config/opencode/commands/`. Keep the
Trio filenames unique if global and project copies coexist. The shipped example
is safe to copy or merge; it is not an active model choice by itself.

## 3. Automated loop driver

`trio-opencode` is the standalone loop driver. It generates its own isolated
OpenCode config and role agents, so it does not depend on the native bundle's
global agents and never reads the project's `opencode.json`. The driver's
config is the source of truth for its models and key.

```bash
trio-opencode doctor
trio-opencode start --mailbox /abs/path/to/project/loop --max-iterations 4
```

`doctor` validates the binary, CLI capabilities, model catalog, key file, and
Git/Python versions. `doctor` is offline-safe when the key file is missing,
but a real run needs the key. The default key path is
`~/Documents/OpenCodeKey.txt`; keep it mode `600`.

The shipped driver config defaults to:

| Role tier | Default model |
|---|---|
| Lead / Evaluator / acceptance author | `opencode-go/deepseek-v4.1-flash` |
| Builder / Scout / Repair | `opencode-go/glm-5.3-flash` |

If `--strong-model` and `--light-model` are supplied at install time and no
driver config exists yet, the installer writes that strong/light mapping into
the new config. It never overwrites an existing config. For another provider,
edit the config's `models` and `provider` fields; `opencode models` lists the
exact identifiers.

Operational notes:

- **Open-loop is automatic.** A mailbox with `loop/QUEUE.md` uses the
  open-loop Lead/Evaluator queues; without it the driver runs lockstep. There
  is no `--open-loop` flag.
- **Frozen acceptance** is opt-in: `--acceptance` on `start` (or
  `TRIO_ACCEPTANCE=1`, or `"acceptance": true` in the config). It is a
  start-only decision; `resume` replays it.
- **Root-free is the default.** Builders and evaluator pins run in isolated
  git worktrees and the verified result lands only on SHIP. Use `--in-place`
  only when you explicitly want the driver to operate in the current checkout.
- **Run control:** `resume --mailbox …` continues after a crash or error,
  `status --mailbox …` prints the state/registry/result JSON,
  `abandon --mailbox …` gives up while keeping the loop branch, and
  `land --mailbox …` retries a diverged root-free land.
- **Exit codes:** `0` shipped/landed, `3` error/conflict, `4` iteration cap,
  `5` needs human/blocked, `6` needs retirement, `8` needs land, `9` refused
  by a live lock.

The driver reuses the repository's `metrics/` core, so a mailbox it starts can
be resumed by another Trio driver and vice versa. It does not install the
native `/trio` agents or require them.

## 4. Model parameters and role mapping

OpenCode agents can specify an exact `provider/model` value or inherit the
surrounding session's model. The parameterized installer and
`opencode/configure-models.sh` write the user's choices into the installed
agent frontmatter with this fixed role mapping:

- Strong: `trio-orchestrator`, `trio-lead`, `trio-evaluator`
- Light: `trio-scout`, `trio-builder`

The standalone driver's model mapping is separate: it lives in
`${TRIO_OPENCODE_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/trio-opencode/config.json}`
and is not changed by `configure-models.sh`. The installer seeds it from
`--strong-model`/`--light-model` only when that config does not exist yet; an
existing config is always preserved. See section 3.

To configure or change an existing global Trio installation:

```bash
./opencode/configure-models.sh --global \
  --strong-model '<provider/strong-model>' \
  --light-model '<provider/light-model>'
```

The script changes only the `model:` field in recognized Trio agent files and
refuses missing or unrecognized files. To prepare the values:

1. Run `opencode models` and copy exact identifiers from its output.
2. Ask the user which exact ID is the strong tier and which is the light tier.
3. Pass both IDs to the installer or configuration command above.
4. Verify the installed `model:` fields before starting `/trio`.

`opencode/opencode.trio.example.jsonc` remains a manual configuration reference
for users who prefer central OpenCode configuration. The native bundle still
ships no provider/model default. Omitting both parameters preserves OpenCode's
normal inheritance: the primary agent uses the configured global model and
subagents inherit their invoking primary's model. It does not select or charge
for an unselected provider.

The frontmatter in `opencode/agents/` is the source of truth for role modes:
the orchestrator is `primary`; the four role workers are `subagent`. Their
`permission.task` entries start with `"*": deny` and then allow only the
children required by the protocol. The Evaluator's edit permission denies
product-file edits while allowing its mailbox verdict/log outputs; its Bash
permission has a deny-first allowlist for seven exact credential-free syntax,
smoke, diff/index, status, and inventory commands. Focused scans use OpenCode's
built-in read-only `grep`, `glob`, and `read` tools. All other Bash commands
remain denied, so it cannot use the shell to bypass that boundary.

## 5. Validate and run

The native bundle's credential-free checks never invoke OpenCode or a provider:

```bash
bash -n install.sh portable/driver.sh opencode/smoke-test.sh
./opencode/smoke-test.sh
```

The driver has its own `trio-opencode doctor` check (section 3). The two
surfaces can be installed together; the driver does not use the native global
agent files.

From a project containing a goal mailbox, start an interactive session and run
the routed commands:

```text
/trio-init <goal>
/trio
```

For a headless one-shot invocation, use the documented command routing:

```bash
opencode run --command trio
```

The `/trio` command routes to `trio-orchestrator` and runs as a subtask. The
orchestrator must use the named Task children in this order: Scout
reconnaissance, Lead planning, one mandatory primary Builder pass for every
code-changing increment, Lead review/correction, fresh Scout reconnaissance
for grading, then the independent Evaluator. The Evaluator writes
`loop/VERDICT.md` and never fixes product code.

Start Trio through `/trio` rather than manually `@` mentioning a role. OpenCode
lets users invoke subagents directly even when Task permissions deny them; the
bundle hides the role workers from normal completion and uses the routed command
as the protocol-preserving entry point.

## 6. Project instructions and permissions

OpenCode loads `AGENTS.md` instructions from the project context and parent
directories; where no `AGENTS.md` is present it can fall back to
`CLAUDE.md`. Keep the repository's existing mailbox/provenance rules in those
instructions and review the [official instruction-loading behavior](https://opencode.ai/docs/rules/)
when changing project layout.

Review the [permissions documentation](https://opencode.ai/docs/permissions/)
before granting tools in a shared repository. Trio's named `permission.task`
rules intentionally avoid arbitrary subagent wildcards. Do not grant a
headless run broader write, shell, credential, or network access than the goal
requires.

The native bundle installs agents and commands only; the standalone driver
installs a Python runtime and generates its own per-run OpenCode config and
agents. Neither installs a plugin, custom tool, MCP server, or skill. OpenCode
enables tools by default, can load global/project plugins automatically,
exposes custom and MCP tools alongside built-ins, and loads skills through the
`skill` tool. The driver deliberately runs with `OPENCODE_DISABLE_PROJECT_CONFIG`
and isolated XDG trees so the target project's agents/plugins/config are not
loaded; the native bundle's Scout and Evaluator start with a top-level deny and
explicitly allow only their required callable tool names. This permission layer
does not sandbox plugin hook code, which is trusted host code. A custom or
plugin tool can also collide with an allowed built-in name, so audit existing
global and project plugins/tools and name collisions before relying on the
read-only boundary.

## 7. Native boundary, driver, and portable fallback

The native bundle requires an OpenCode release that supports markdown agent
modes (`primary`/`subagent`/`all`), named `permission.task` children, and
command routing with `subtask: true`. If your installation lacks that Task
surface, use the standalone driver instead: it does not need native Task
children or routed commands, because it spawns one `opencode run` process per
role. `trio-opencode doctor` reports the CLI capabilities it needs.

The legacy portable fallback is still available for an installation where
even the driver's documented `opencode run` surface is unavailable:

```bash
mkdir -p loop
cp portable/GOAL.template.md loop/GOAL.md  # edit the goal
HARNESS=opencode ./portable/driver.sh 10
```

That fallback preserves the mailbox and verdict semantics but runs the Lead
and Evaluator sequentially through the CLI; it cannot enforce named subagent
separation, isolated worktrees, or the driver's commit/verdict gates. Prefer
`trio-opencode` when it can run. See
[portable/SETUP-opencode.md](portable/SETUP-opencode.md) for its deliberately
narrower contract.


## Productionize audit

OpenCode exposes `/trio-productionize` as a command. `--productionize` (or the
OpenCode install flag) installs the shared assets.
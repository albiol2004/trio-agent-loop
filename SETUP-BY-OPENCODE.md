# Set up Trio with OpenCode

This repository ships an OpenCode-native bundle as its primary OpenCode
integration. It uses project/global markdown agents, named `Task` children,
routed commands, and the existing `loop/` mailbox. The older
`portable/driver.sh` path is a compatibility fallback for an OpenCode
installation that does not expose that native Task surface.

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

The repository intentionally selects no provider or paid model by default.

## 2. Install the native bundle

From the clone, install into OpenCode's global configuration tree. If the user
has supplied exact strong and light model IDs, apply them during installation:

```bash
./install.sh --opencode \
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

This creates only Trio-owned files under
`~/.config/opencode/agents/`, `~/.config/opencode/commands/`, and
`~/.config/opencode/opencode.trio.example.jsonc` (or the equivalent
`$XDG_CONFIG_HOME/opencode/` tree). Existing files are preserved and no
dependencies, credentials, or unrelated harness directories are created.

For a project-only installation, copy the same bundle from the clone instead:

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

## 3. Model parameters and role mapping

OpenCode agents can specify an exact `provider/model` value or inherit the
surrounding session's model. The parameterized installer and
`opencode/configure-models.sh` write the user's choices into the installed
agent frontmatter with this fixed role mapping:

- Strong: `trio-orchestrator`, `trio-lead`, `trio-evaluator`
- Light: `trio-scout`, `trio-builder`

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
for users who prefer central OpenCode configuration. The repository still
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

## 4. Validate and run

The credential-free checks never invoke OpenCode or a provider:

```bash
bash -n install.sh portable/driver.sh opencode/smoke-test.sh
./opencode/smoke-test.sh
```

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

## 5. Project instructions and permissions

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

Trio installs native agents and commands only—no plugin, custom tool, MCP
server, or skill. OpenCode enables tools by default, can load global/project
plugins automatically, exposes custom and MCP tools alongside built-ins, and
loads skills through the `skill` tool. The Scout and Evaluator therefore start
with a top-level deny and explicitly allow only their required callable tool
names; unnamed tools and skills remain denied. This permission layer does not
sandbox plugin hook code, which is trusted host code. A custom or plugin tool
can also collide with an allowed built-in name, so audit existing global and
project plugins/tools and name collisions before relying on the read-only
boundary.

## 6. Native boundary and portable fallback

The native bundle requires an OpenCode release that supports markdown agent
modes (`primary`/`subagent`/`all`), named `permission.task` children, and
command routing with `subtask: true`. If your installation lacks that Task
surface, do not pretend the native protocol is enforced. Use the explicit
portable fallback instead:

```bash
mkdir -p loop
cp portable/GOAL.template.md loop/GOAL.md  # edit the goal
HARNESS=opencode ./portable/driver.sh 10
```

That fallback preserves the mailbox and verdict semantics but runs the Lead
and Evaluator sequentially through the CLI; it cannot enforce native named
subagent separation. See [portable/SETUP-opencode.md](portable/SETUP-opencode.md)
for its deliberately narrower contract.

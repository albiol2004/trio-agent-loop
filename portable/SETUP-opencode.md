# OpenCode portable fallback

The repository's first-class OpenCode integration is the native bundle in
`opencode/`. Use this document only when the installed OpenCode release does
not expose the documented markdown-agent `Task` surface. The fallback keeps
the mailbox and verdict protocol, but it cannot enforce native named
subagent permissions or the mandatory Builder delegation inside one process.

Current OpenCode references: [agents](https://opencode.ai/docs/agents/),
[commands](https://opencode.ai/docs/commands/),
[permissions](https://opencode.ai/docs/permissions/),
[CLI](https://opencode.ai/docs/cli/), and
[authentication](https://opencode.ai/docs/auth/).

## Setup

1. Install OpenCode using the [official installation instructions](https://opencode.ai/docs/).
2. Authenticate interactively when needed with `opencode auth login`.
3. Run `opencode models` and choose exact provider/model identifiers available
   in your account. The fallback has no safe universal default.
4. Ensure project instructions are available through `AGENTS.md` (OpenCode
   also supports the documented `CLAUDE.md` fallback).

## Run the fallback

From a project root:

```bash
mkdir -p loop
cp portable/GOAL.template.md loop/GOAL.md   # edit it
HARNESS=opencode ./portable/driver.sh 10
```

The portable driver invokes `opencode run` once for each Lead and Evaluator
prompt and judges progress from the first line of `loop/VERDICT.md`. Keep the
driver's existing invocation semantics intact, and verify flags against
`opencode run --help` after upgrading OpenCode. An optional `OPENCODE_MODEL`
may contain one exact value returned by `opencode models`; optional
`OPENCODE_LEAD_AGENT` and `OPENCODE_EVAL_AGENT` names are for users who have
created compatible project agents themselves.

For the enforced native sequence, install the bundle and use `/trio` instead:

```bash
./install.sh --opencode
```

See [SETUP-BY-OPENCODE.md](../SETUP-BY-OPENCODE.md) for the native install,
model inheritance, permissions, routed commands, and its explicit Task
limitation.

# Setup instructions for Codex

Install the native Codex bundle:

```bash
./install.sh --codex
```

This copies the Trio skills to `~/.agents/skills/` and the four custom agents
to `~/.codex/agents/`. It does not install the portable driver, profiles, or
launch `codex exec`.

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
spawning descendants. Keep that default. The loop never commits.

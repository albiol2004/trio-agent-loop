# Setup — Z.ai (GLM Coding Plan / ZCode)

Z.ai offers two things (as of July 2026):

1. **ZCode** (https://zcode.z.ai) — first-party harness launched 2026-07-01,
   "Official Harness for GLM-5.2". It is a **desktop GUI app** (Electron
   "Agentic Development Environment"), NOT a CLI: installers only
   (.dmg/.exe/.deb/.AppImage beta), model hard-wired to GLM-5.2, five GUI
   execution modes plus a "Goal Mode" (`/goal`, iterates until verified —
   their native take on exactly this loop pattern). **No documented headless
   mode → cannot be driven by the portable driver.** If you use ZCode, run
   the loop manually: paste `portable/prompts/lead.md` and `evaluator.md`
   alternately in full-access mode against the same `loop/` files, or just
   use its Goal Mode with GOAL.md's content. (An embedded `~/.zcode/cli`
   TUI exists per one bug report — undocumented, don't build on it.)

2. **GLM Coding Plan** — an Anthropic-compatible API endpoint. This is the
   scriptable path: the **native** template runs unmodified on Claude Code
   pointed at GLM (no portable driver needed), or `HARNESS=claude` with the
   same env for the driver.

## Configure Claude Code → GLM (verified against docs.z.ai/devpack/tool/claude)
`~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your_zai_api_key",
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.7",
    "API_TIMEOUT_MS": "3000000"
  }
}
```
(Or run `npx @z_ai/coding-helper` — Z.ai's config wizard. Use plain `glm-5.2`
as the model string; GLM-5.2 has 1M context and burns Coding Plan quota at
~3× peak / 2× off-peak.)

Then install the loop normally:
```bash
./install.sh --global
# any project:  /trio-init <goal>  →  /trio  →  /loop /trio
```

## Caveats specific to GLM
- **Model pinning collapses**: the roles' `model: opus/sonnet` frontmatter
  resolves through the tier mapping — with the config above, Lead, Evaluator
  and workers all get GLM-5.2. The "same strong model both roles" property
  survives; the cheap-worker economics only survive if you map the sonnet
  tier to a cheaper GLM.
- Same-family judging makes the Evaluator's anti-sycophancy protocol MORE
  important — don't relax it.
- Community consensus: GLM-5.2 is strong at routine implementation, weaker
  than Opus-tier at architectural judgment — keep GOAL.md increments smaller
  and expect more ITERATE rounds.

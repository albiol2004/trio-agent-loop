# Setup — OpenCode (opencode.ai)

Best non-Claude fit so far: OpenCode reads **AGENTS.md natively** (even falls
back to CLAUDE.md), has markdown-defined agents selectable headlessly with
`--agent`, and a real permission config. Driver support: `HARNESS=opencode`.

## One-time setup
1. Install OpenCode and a provider (`opencode auth login`).
2. Project root `opencode.json` — permissions MUST be explicit allow/deny:
   **any `"ask"` hangs forever in headless mode (no TTY)**:

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "permission": {
       "*": "allow",
       "edit": "allow",
       "bash": { "*": "allow", "rm -rf *": "deny", "git push*": "deny" }
     }
   }
   ```
3. Context: copy `portable/AGENTS.template.md` → `AGENTS.md` (OpenCode walks
   up from cwd; global fallback `~/.config/opencode/AGENTS.md`).
4. Optional but recommended — define the roles as native OpenCode agents in
   `.opencode/agents/lead.md` and `.opencode/agents/evaluator.md`:

   ```markdown
   ---
   description: Lead — plans and implements one loop iteration
   mode: primary
   model: anthropic/claude-opus-4-8   # or any provider/model
   ---
   <paste body of portable/prompts/lead.md>
   ```
   (Frontmatter also supports `temperature`, per-tool `permission`, `steps`;
   `tools` is deprecated in favor of `permission`. For the evaluator agent you
   can deny `edit` on source paths via permission patterns — hard enforcement
   of "never fixes code" — but keep `loop/*.md` writable for VERDICT.md.)

## Run
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=opencode ./portable/driver.sh 10
# Optional: OPENCODE_MODEL=anthropic/claude-opus-4-8, and if you created the
# native agents above: OPENCODE_LEAD_AGENT=lead OPENCODE_EVAL_AGENT=evaluator
```

## Caveats (verified from tracker)
- `--auto` auto-approves anything not explicitly denied — combine with the
  explicit permission block above; keep destructive bash patterns on deny.
- **Exit codes are unreliable** (can be 0 on real failure — issue #15558):
  the driver judges progress by VERDICT.md, not `$?`, which is the right
  posture here anyway. The driver also wraps invocations in `timeout`.
- Prompt is a positional arg to `opencode run` (no prompt-file flag; `-f`
  only *attaches* files) — the driver passes `$(cat promptfile)`.
- `--format json` streams raw events if you want structured parsing; redirect
  to a file (piped JSON occasionally truncates, issue #2803).

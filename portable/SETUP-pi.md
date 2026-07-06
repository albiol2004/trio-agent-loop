# Setup — Pi (pi.dev, Mario Zechner / Earendil)

Pi is a deliberately minimal harness — no permission popups, no subagents, no
plan mode. That makes it the *purest* fit for the Ralph pattern: the loop
structure lives entirely in our prompts and driver, and Pi just executes.
Driver support: `HARNESS=pi`.

## One-time setup
1. Install: `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`
   (the old `@mariozechner/pi-coding-agent` is deprecated; unrelated
   `@mariozechner/pi` is NOT this tool). Login: `pi` → provider login, or API
   key env vars.
2. Context: Pi reads **AGENTS.md only** (not CLAUDE.md/PI.md), concatenating
   `~/.pi/agent/AGENTS.md` → parent dirs → cwd. Copy
   `portable/AGENTS.template.md` → `AGENTS.md`.
3. **Safety first**: Pi has NO permission system by design — it runs with
   your user's full permissions, unconditionally. For autonomous looping,
   strongly prefer a container/VM or throwaway worktree, and keep secrets out
   of the environment.

## Run
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=pi ./portable/driver.sh 10
# Optional: PI_MODEL="anthropic/claude-opus-4-8" (supports :thinking suffixes,
# e.g. "sonnet:high" — passed as --model)
```

## Caveats
- One-shot mode is `pi -p "<prompt>"` (print-and-exit). There are open issues
  about `-p` occasionally not exiting — the driver wraps every invocation in
  `timeout` (default 1200s, tune with `ROLE_TIMEOUT`).
- Each invocation is a fresh context (Pi sessions/`-c` exist but the driver
  deliberately does not use them — continuity lives in `loop/*.md`).
- No web access tooling by default beyond what the model/bash provides; the
  Evaluator's API-currency checks degrade to "check the lockfile against
  changelogs via curl" or get skipped — note it in VERDICT.md when skipped.
- Power feature if you ever want tighter integration: Pi extensions
  (`.pi/extensions/*.ts`) can register custom tools — e.g. a `verdict` tool
  that validates VERDICT.md's first line before the process exits.

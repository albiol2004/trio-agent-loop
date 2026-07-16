# Setup — any other agentic CLI (generic)

The loop needs exactly three things from a harness; almost every 2026 agentic
CLI has them:

1. **A non-interactive "run one prompt and exit" mode** that can read/write
   files and run shell commands in the current directory. Known one-liners:

   | Harness | Invocation |
   |---|---|
   | Claude Code | `claude -p "<prompt>" --permission-mode acceptEdits` |
   | Gemini CLI / Antigravity | `gemini --approval-mode=yolo -p "<prompt>"` (native: `HARNESS=gemini`; also `agy`) |
   | OpenCode | `opencode run --auto "<prompt>"` (native support: `HARNESS=opencode`) |
   | Hermes (Nous) | `hermes -z "<prompt>" --yolo` (native support: `HARNESS=hermes`) |
   | Athen | `athen-cli --prompt "<prompt>"` (native support: `HARNESS=athen`) |
   | Aider | `aider --message "<prompt>"` |
   | GitHub Copilot CLI | `copilot -p "<prompt>"` |

   Codex, ZCode, and Pi are intentionally excluded: use their native bundles
   from `SETUP-BY-CODEX.md`, `SETUP-BY-ZCODE.md`, and `SETUP-BY-PI.md`.

2. **Autonomy flags** so the invocation doesn't stall on approval prompts
   (each tool names this differently: `--full-auto`, `--force`,
   `--permission-mode`, `--yes-always`…). Grant file writes + shell in the
   project dir; sandbox beyond that to taste.

3. **Project-context file support** — most read `AGENTS.md` natively (Codex,
   Cursor, Copilot, Aider, Zed, Windsurf, Devin…); Claude Code reads
   `CLAUDE.md`, Gemini CLI reads `GEMINI.md`. Copy or symlink accordingly.

## Wiring it up
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=generic \
  RUN_LEAD='mycli run --auto --prompt-file' \
  RUN_EVAL='mycli run --auto --prompt-file' \
  ./portable/driver.sh 10
```
`RUN_LEAD`/`RUN_EVAL` are command prefixes; the driver appends the prompt-file
path. If your CLI wants the prompt as text instead of a file path, wrap it:
`RUN_LEAD='sh -c "mycli -p \"$(cat $0)\""'`.

## What the driver guarantees regardless of harness
- Fresh context per role per iteration (each invocation is a new process);
  ALL state lives in `loop/` markdown files.
- Control flow parses only the first line of `loop/VERDICT.md`
  (`VERDICT: SHIP|ITERATE|BLOCKED`); an unparseable verdict stops the loop
  rather than running away.
- Iteration cap (arg 1, default 10); exit codes: 0 SHIP, 2 BLOCKED,
  3 bad verdict, 4 cap hit.

## Checklist for a new harness
- [ ] One-shot mode confirmed writing files without interactive approval
- [ ] Role prompts reachable (the driver passes `portable/prompts/*.md`)
- [ ] Context file present (`AGENTS.md` or the tool's equivalent)
- [ ] Dry run: one manual lead invocation, inspect `loop/PLAN.md` + `REPORT.md`
- [ ] Then hand it to `driver.sh`

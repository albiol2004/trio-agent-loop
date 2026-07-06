# Setup — Google Antigravity (and Gemini CLI)

Antigravity 2.0 (I/O 2026) is four surfaces sharing one agent harness: the
IDE (VS Code fork), the Agent Manager desktop app, a Go CLI (`agy`), and a
Python SDK. Google is transitioning Gemini CLI into the Antigravity CLI, so
the two are documented together here. Driver support: `HARNESS=gemini`
(fully verified) and `HARNESS=agy` (verify flags locally first).

## Scriptability verdict
- The **IDE/Agent Manager are not scriptable** for unattended loops: even in
  Turbo terminal mode, file-edit approvals surface accept/reject UI.
- The **`agy` CLI** reportedly supports `--headless` + `--approve all` and a
  `/goal` run-to-completion mode — but that flag syntax comes from secondary
  blogs only, not primary Google docs. **Run `agy --help` and confirm before
  relying on the agy branch**; adjust the driver line if flags differ.
- **Gemini CLI** is the fully-documented path and shares the ecosystem.

## One-time setup
1. Install Gemini CLI (or `agy` where available); authenticate.
2. Context files: Gemini CLI reads **GEMINI.md** hierarchically by default;
   AGENTS.md is opt-in via `settings.json`:
   `{ "context": { "fileName": ["AGENTS.md", "GEMINI.md"] } }`
   Antigravity surfaces read repo-root `GEMINI.md` (highest priority),
   `AGENTS.md` (since ~v1.20.3), and `.agent/rules|workflows|skills/`.
   Simplest cross-surface setup: keep AGENTS.md as the source of truth and
   symlink `GEMINI.md → AGENTS.md`. (Heads-up: Antigravity and Gemini CLI
   both read `~/.gemini/GEMINI.md` globally — keep global context minimal.)
3. Permissions for unattended runs: full YOLO deliberately cannot be
   persisted in settings.json — the driver passes it per invocation. A
   safer persistent middle ground is
   `{"general":{"defaultApprovalMode":"auto_edit"},"tools":{"allowed":["run_shell_command(git)","write_file"]}}`
   plus shell prefixes you trust, then drop the yolo flag from the driver.

## Run
```bash
mkdir -p loop && cp portable/GOAL.template.md loop/GOAL.md   # edit it!
HARNESS=gemini ./portable/driver.sh 10
# or, after verifying flags with agy --help:
HARNESS=agy ./portable/driver.sh 10
# Optional: GEMINI_MODEL=... (passed as -m)
```
Driver invocations:
- gemini: `gemini --approval-mode=yolo -p "<prompt>"`  (`--yolo/-y` is the
  deprecated spelling; `plan`/`auto_edit` are the tamer modes)
- agy: `agy --headless --approve all "<prompt>"`  (UNVERIFIED — see above)

## Notes
- Models (Antigravity side): Gemini 3.1 Pro / 3.5 Flash tiers plus
  third-party options (Claude, GPT-OSS) selectable per agent — different
  models per parallel agent is native there, which is interesting for
  cross-family Lead/Evaluator splits if `agy` exposes it (unverified).
- Antigravity has its own loop-ish primitives: scheduled tasks in the
  desktop app, a Managed Agents API (isolated cloud environments), and
  persistent skills/learnings in `.agent/skills/`. None of them gate on a
  file predicate, so the bash driver remains the mechanism here.
- Gemini CLI's long-term fate is the Antigravity transition — pin your
  installed version and expect this doc's gemini section to eventually
  become the agy section.

# Setup instructions for a ZCode session

You (ZCode Agent with shell access) have been given this repository to set up
the trio Lead -> Evaluator workflow for ZCode. ZCode is a desktop application;
the official documentation describes the desktop Agent, workspace
instructions, skills, commands, Goal Mode, and user-level custom subagents. It
does not document a supported headless CLI, so do not try to run ZCode through
`portable/driver.sh`.

Do this from the repository root:

1. Confirm the ZCode desktop app is installed and this repository is open as
   the current Workspace. On first launch, connect Z.ai, BigModel, or an API
   key through the model connection screen. Use GLM-5.2 with the highest
   available thought level for Lead/Evaluator work when testing this loop.

2. Project context: ZCode reads the workspace `AGENTS.md` and the user-level
   `~/.zcode/AGENTS.md`; it does not continuously read `CLAUDE.md`, walk up
   arbitrary parent directories, or expand imports. This repository already
   has the canonical `AGENTS.md`. For another target project, preserve an
   existing `AGENTS.md`; otherwise copy:

   ```bash
   cp portable/AGENTS.template.md /path/to/project/AGENTS.md
   ```

   Fill only the project-specific section. Do not overwrite an existing
   instruction file.

3. Install the trio skills for ZCode. ZCode user-level skills live under
   `~/.zcode/skills/<name>/SKILL.md`:

   ```bash
   mkdir -p ~/.zcode/skills
   ./install.sh --zcode
   ```

   Open Settings -> Skills and refresh. Confirm `trio` and `trio-init` are
   enabled. They will then be available as `$trio` and `$trio-init` in the
   task input. Do not install the Claude Code global bundle with
   `./install.sh --global`; that targets `~/.claude`, not ZCode.

4. Configure the four custom subagents in Settings -> Subagents if they do
   not already exist. ZCode currently manages custom subagents as a beta,
   user-level feature. Keep existing definitions unless the human explicitly
   asks to replace them:

   - `trio-lead`: Lead that plans, delegates, reviews, and corrects; all tools;
     use GLM-5.2 with the highest available thought level. Its initial pass
     must not edit product code.
   - `trio-evaluator`: adversarial Evaluator; Read/Grep/Glob plus Bash for
     verification; no Edit or Write; use GLM-5.2 with the highest available
     thought level.
   - `trio-scout`: read-only explorer; Read/Grep/Glob only; use GLM-5.2 with
     the High thought level.
   - `trio-builder`: primary implementation worker for substantive application
     logic, tests, and integration within a Lead-defined brief; Read/Edit/Write
     plus Bash; use GLM-5.2 with the High thought level.

   Every code-changing increment starts with the Builder's main implementation
   pass, followed by Lead review and any corrective edits. REPORT.md must state
   that provenance. The Lead and Evaluator remain the judgment roles; Scouts
   and Builders are scoped workers. If the connected provider exposes
   different model names or thought levels, preserve these role boundaries and
   report the mapping instead of silently weakening the Evaluator.

5. Initialize and run in the open Workspace using only ZCode-native features:

   ```text
   $trio-init <one-sentence goal>
   $trio
   ```

   `trio-init` creates the mailbox and `trio` uses ZCode's Agent tool with the
   configured custom subagents. For autonomous continuation, start `/goal`
   with the same mission and instruct it to run the native Trio protocol until
   SHIP or BLOCKED. Never use the portable driver.

6. There is no documented ZCode equivalent of Claude Code's `/loop`. For
   repeated trio iterations, invoke `$trio` again after an `ITERATE` verdict,
   or use ZCode `/goal` to supervise a clearly stated, verifiable objective
   while you monitor the mailbox. Never run two iterations against the same
   mailbox at once; use `dir=loop-<name>` for a sibling mailbox.

7. Verify that `loop/GOAL.md`, `loop/STATE.md`, and `loop/LOG.md` exist, that
   `STATE.md` contains the mission fingerprint, and that the first line of
   `VERDICT.md` is `VERDICT: SHIP`, `ITERATE`, or `BLOCKED` when an iteration
   has completed. The loop never commits automatically.

ZCode references:

- https://zcode.z.ai/en/docs/install
- https://zcode.z.ai/en/docs/agents
- https://zcode.z.ai/en/docs/goal
- https://zcode.z.ai/en/docs/subagents
- https://zcode.z.ai/en/docs/skill

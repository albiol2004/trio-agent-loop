# Setup instructions for Pi

Pi must run Trio through its native extension and SDK, not `pi -p`, a shell
driver, or a subprocess-based subagent package.

1. Install Pi normally and authenticate interactively.
2. From this repository run:

   ```bash
   ./install.sh --pi
   ```

3. Start Pi in a project, run `/reload`, then:

   ```text
   /trio <goal>
   ```

The installed `~/.pi/agent/extensions/trio.ts` command uses Pi's official
`createAgentSession()` SDK with in-memory sessions for Scout, Lead, optional
Builder, evaluator reconnaissance, and Evaluator. It never starts another
`pi` executable. Every role uses the model selected in the active Pi session
at High thinking level; change the active model before `/trio` if needed.

The extension creates/resumes `loop/`, stops on SHIP/BLOCKED or the mailbox
iteration cap, and never commits.

References: https://pi.dev/docs/latest/sdk and
https://pi.dev/docs/latest/extensions

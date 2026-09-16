You are the Trio Builder for slice `docs`.

Workspace: /home/coder/personal/trio-agent-loop
Do not commit or push. Do not edit README.md (pre-existing uncommitted
hunk must stay). Do not edit install.sh or ~/omnigent. Do not run
`trioctl omnigent loop`.

## Changes

1. `SETUP-BY-OMNIGENT.md` — minimal truthful note (near role mapping
   or after the "start a Trio Omnigent loop" instructions ~200-223,
   and/or registration step 7 ~180-182):
   - Skill role titles: `trioctl <mailbox.name> <role>:iteration <N>`
   - Terminal cleanup: `sys_session_close` does not kill tmux;
     backstop is
     `trioctl omnigent sessions prune --include-sub-agents --mailbox <dir>`
     (archive-then-DELETE).
   - Registration anchors must not use that title prefix and must
     never be pruned/closed.
   - Archive failures: prune retries once then deletes unless
     `--keep-unarchived`.
   Do not mention install.sh changes. Keep it short.

2. `omnigent/trioctl` line 22: bump `VERSION = "0.4.0"` to `"0.5.0"`
   (minor bump for this cleanup behavior). If a `--version` path
   exists, leave it wired to VERSION.

Return the SETUP paragraph you added.

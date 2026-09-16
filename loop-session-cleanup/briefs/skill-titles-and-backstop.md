You are the Trio Builder for slice `skill-titles-and-backstop`.

Workspace: /home/coder/personal/trio-agent-loop
Do not commit, push, or edit mailbox files. Do not edit install.sh.
Do not touch ~/omnigent. Do not run `trioctl omnigent loop`.

## Title scheme (locked)

Every `sys_session_create` role/worker title MUST be:

`trioctl <mailbox.name> <role>:iteration <N>`

Example: `trioctl loop-session-cleanup lead:iteration 1`

Why:
- Starts with `trioctl <mailbox.name> ` so
  `trioctl omnigent sessions prune --include-sub-agents --mailbox <dir>`
  matches (trioctl ~1062-1171, prefix at ~1109).
- Contains `:` AFTER that prefix so unfixed Omnigent
  `_parse_session_title` (colon required; else agent/title are None and
  close returns `session_not_a_sub_agent`).

Registration-anchor sessions (Preflight step 3, config_path idle
sessions) MUST keep titles without that prefix. Never close or prune them.

## Files to edit (hand ranges; do not ingest whole files)

1. `omnigent/entrypoints/trio-omnigent/SKILL.md`
   - ~113: replace "Use a title containing mailbox and iteration."
     with the locked title scheme, including the title= argument on
     `sys_session_create`. Same for Evaluator create (~116).
   - ~160-167 step 8: rewrite so cleanup runs BOTH:
     a) `sys_session_close` on every tracked id except the two
        registration-anchor bootstrap conversation ids
     b) as guaranteed backstop:
        `trioctl omnigent sessions prune --include-sub-agents --mailbox <dir>`
     Broker DELETE (which prune performs) is the only path that kills
     the dedicated tmux terminals. Close is a tombstone+interrupt and
     does not free RAM.
   - Step 8 must also run after commit-gate abort, and whenever the
     coordinator's turn ends abnormally (interrupt / error abort).
   - Add abort/orphan path: if a Lead/Evaluator role session ends
     `failed` (including status failed with "connection to runner
     lost" after the coordinator Omnigent runner's 1h idle timeout),
     do NOT re-implement. Treat as abort: run the prune backstop,
     then re-create that role session with a fresh title under the
     same scheme so it can finish the iteration from the committed
     tree. Provider transport auto-wake (`resource_exhausted` /
     `NGHTTP2_INTERNAL_ERROR` / `stream refused`) stays as-is and is
     NOT this abort path.
   - Keep the file well commented in prose (skill text). Keep lines
     reasonably short. Do not bloat the protocol HTML comment block.

2. `omnigent/entrypoints/trio-omnigent/prompts/lead.md`
   - Mention the locked title scheme if the Lead creates any
     `sys_session_create` children (usually it uses trioctl; one short
     sentence is enough). Do not add Co-Authored-By instructions.

3. `omnigent/entrypoints/trio-omnigent/prompts/evaluator.md`
   - Same one-sentence title scheme if it creates children.

## Done when

SKILL.md step 2/4 titles, step 8 close+prune, abort/orphan/idle-timeout
recreate path, and anchors-never-closed are explicit. Return the exact
final step 8 text in your summary.

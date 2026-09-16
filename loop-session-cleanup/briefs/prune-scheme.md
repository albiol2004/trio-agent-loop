You are the Trio Builder for slice `prune-scheme`.

Workspace: /home/coder/personal/trio-agent-loop
Do not commit or push. Do not edit mailbox files, install.sh, or
~/omnigent. Do not run `trioctl omnigent loop`.

## Title scheme (locked)

`trioctl <mailbox.name> <role>:iteration <N>`

Example: `trioctl mbx lead:iteration 1`

Must start with `trioctl <mailbox.name> ` (note trailing space).
Colon sits AFTER that prefix so unfixed `_parse_session_title` works.

## Required change

1. `OmnigentRunner._title` in `omnigent/trioctl` ~1650-1665: emit the
   locked scheme. Keep open-loop suffixes after it:
   `trioctl {mailbox.name} {role}:iteration {n}` plus optional
   ` lead-pass` / ` integration-eval` / ` slice-eval:{id}`.
2. Title-scoped `_matches` (~1109-1113) already uses
   `title.startswith(f"trioctl {mailbox.name} ")`. Keep that. Add a
   short comment that skill titles use `role:iteration N` after the
   prefix and that registration-anchor titles (e.g.
   `trio-omnigent-lead`, config-path idle sessions) must not use this
   prefix so prune never deletes them.
3. Optional tiny helper `_session_title_matches_mailbox(title, mailbox)`
   if it keeps `command_loop`/`_prune` readable. Do not rewrite listing.

## Tests

`omnigent/tests/test_omnigent_loop.py` title tests ~334-345, ~480-528:
update expected strings to the new scheme.

`omnigent/tests/test_trioctl.py` prune tests (~1170+): add (do not
remove old prefix tests if old titles should still match — they still
start with `trioctl mbx `):

- `--include-sub-agents` matches
  `trioctl mbx lead:iteration 1` kind=sub_agent
- same for evaluator title
- registration-anchor titles NEVER match even with
  `--include-sub-agents`: e.g. `trio-omnigent-lead`,
  `trio-omnigent-evaluator`, `lead:omnigent/trio-omnigent-roles/lead`,
  untitled/empty, `trioctl other-mailbox lead:iteration 1`
- mailbox named `loop-session-cleanup` matches only its prefix

Run both:
`python3 -m pytest -q omnigent/tests/test_trioctl.py omnigent/tests/test_omnigent_loop.py`

Return files changed and test names.

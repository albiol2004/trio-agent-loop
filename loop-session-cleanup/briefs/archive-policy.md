You are the Trio Builder for slice `archive-policy`.

Workspace: /home/coder/personal/trio-agent-loop
Do not commit or push. Do not edit mailbox files, install.sh, or
~/omnigent. Do not run `trioctl omnigent loop`.

## Bug

`_prune_broker_sessions` in `omnigent/trioctl` ~1149-1160: if
`_fetch_session_items_paged` (~975-1016) returns a `read_error`, the
session is kept and never deleted. That silently retains the tmux
terminal. Safer default: retry the item read once, then DELETE anyway
with a stderr warning that the transcript was not fully archived.

Keep only when `--keep-unarchived` is set (new flag).

## Required change

1. `_prune_broker_sessions` (~1062-1171): add
   `keep_unarchived: bool = False`. On `read_error`:
   - retry `_fetch_session_items_paged` once
   - rewrite the archive file with the best items obtained
   - if still failing and `keep_unarchived`: keep session (old
     behavior), increment `failed`, warn on stderr
   - else: warn on stderr that the transcript was not fully archived,
     then DELETE as usual; still count `failed` (partial archive) AND
     `deleted`
2. `command_sessions_prune` (~1174-1206): pass the flag.
3. argparse for `sessions prune` (~2173-2211): add
   `--keep-unarchived`.
4. Thread the flag through `_run_post_loop_session_prune` only if
   `command_loop` gains a matching flag; default-off is fine for loop
   (always delete after retry). Optional `--keep-unarchived` on `loop`
   is acceptable if cheap.

Comments must explain: delete is the only path that kills terminals;
keeping an unarchived session leaks RAM. Lines under 88 chars.

## Tests (`omnigent/tests/test_trioctl.py`)

Existing `test_prune_one_failing_session_read_does_not_abort_the_others`
(~1616-1651) expects the failing session is NOT deleted. UPDATE it:
default now retries once then DELETES the failing session; the good
session still deletes. Prove two `get_items` attempts for the bad id
if the fake client counts calls.

Add:
- retry succeeds on second read → archived+deleted, `failed` == 0
- `--keep-unarchived` (or `keep_unarchived=True`) keeps the failing
  session after retry, still prunes the good one
- CLI flag is parsed and reaches `_prune_broker_sessions`

Run:
`python3 -m pytest -q omnigent/tests/test_trioctl.py`
All pass, no skips.

Return files changed and test names.

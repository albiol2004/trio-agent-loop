You are the Trio Builder for slice `headless-signals`.

Workspace: /home/coder/personal/trio-agent-loop
Do not commit or push. Do not edit mailbox files, install.sh, or
~/omnigent. Do not run `trioctl omnigent loop` against the live broker.

## Bug

`command_loop` in `omnigent/trioctl` ~1713-1762 restores the env in a
`finally`, but `_run_post_loop_session_prune` (~1209-1236) runs AFTER
that try/finally. KeyboardInterrupt / SIGTERM therefore skip prune and
leave role tmux sessions (200-600 MB each).

## Required change (trioctl only in this range)

In `command_loop` (~1713-1762):

1. Install SIGINT and SIGTERM handlers that raise KeyboardInterrupt
   (or SystemExit with a 128+signum code) so `finally` always runs.
2. Restore previous handlers in `finally`.
3. Run `_run_post_loop_session_prune` inside that same `finally`
   (still skipped when `--keep-sessions`), so normal exit, exceptions,
   SIGINT, and SIGTERM all prune this run's created session ids.
4. Preserve the loop's own exit code on normal return. On
   KeyboardInterrupt, prune first, then return 130 (or re-raise after
   prune — pick one and test it). On other exceptions, prune then
   re-raise.
5. Short comments explaining why prune lives in `finally` (terminals
   only die on broker DELETE). Keep lines under 88 chars.

Do not change archive-failure policy or title matching in this slice.

## Tests

Edit `omnigent/tests/test_omnigent_loop.py` (existing loop prune tests
~736-870). Add tests that monkeypatch `_load_trio_loop` /
`OmnigentRunner` / `_run_post_loop_session_prune` like the existing
ones:

- `run_loop` raises KeyboardInterrupt → prune still called with the
  runner's created ids; `--keep-sessions` still skips prune.
- `run_loop` raises a generic Exception → prune still called, exception
  propagates.
- If practical, invoke the SIGTERM handler (call it) and prove prune
  runs; otherwise KeyboardInterrupt coverage is enough plus a comment
  that SIGTERM uses the same handler.

Run:
`python3 -m pytest -q omnigent/tests/test_omnigent_loop.py`
All must pass. No skips.

Return files changed and exact test names.

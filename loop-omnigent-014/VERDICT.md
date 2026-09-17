VERDICT: ITERATE scope=local:omnigent/trioctl

Independent evaluation of iteration 1 at pinned `722dc50`.
Lead REPORT treated as a claim, not as evidence.

## Scout

`python3 omnigent/trioctl omnigent run scout --prompt-file loop-omnigent-014/briefs/eval-skill-k.md --workspace .` (exit 0). Captured in `evidence/iter1/eval-scout.txt`.

- The three SKILL.md files exist under `/home/coder/omnigent-dev/tests`.
- `-k` name `session_create_spawns_child_under_caller` exists:
  `tests/runner/test_runner_dispatch.py:7804`
  `test_sys_session_create_spawns_child_under_caller`.
- `-k` name `reasoning_effort` is not a `def test_` name in those three
  files; it does exist as many test names elsewhere under
  `/home/coder/omnigent-dev/tests`.
- `registered_native_agent_create_derives_launch_args_from_root_spec`
  is absent (correctly dropped).

## What passed (slices vs GOAL)

- `395ccaa` `create_session` POSTs `host_id` + `workspace`; fallback
  `POST /v1/hosts/{id}/runners`; records id; DELETE on failed start.
- `05c2e22` first-prompt / restart-blip retries the **same** session
  (`ensure_first_prompt`, `_repost_wait_read`). No second create.
- `981f91b` doctor probe prefers `_resolve_subagent_spec`; SKILL.md
  host-selection text.
- `ed81328` offline fake-broker: host launch, prompt retry, no
  duplicate, orphan DELETE.
- `eb73283` id-scoped prune DELETEs `running` this-run sessions.
- Offline: `uv run --with pytest --no-project python3 -m pytest -q
  omnigent/tests` → **114 passed**.
- Doctor (read-only): live PATH and
  `PATH=/home/coder/omnigent-dev/.venv/bin:$PATH` both PASS
  `omnigent:session-contract`.
- Commit gate: `python3 metrics/trio-shadow.py --mailbox
  loop-omnigent-014 --require-commits` exit 0.
- README.md user hunk never staged.
- Isolated 0.14 stack (`free -g` available 8 GB): dedicated runners
  (lead `runner_token_188a769df877014f2c5b2910e418cb08`, evaluator
  `runner_token_6c3d41507955934b5bdc87d407bb66bc`); one session per
  role; lead first prompt retried on the same id (~20s, two
  harness events, one POST /v1/sessions); no `already mirrored`;
  post-loop prune **archived 2, deleted 2, skipped running 0**;
  no `loop-smoke` row left on 7767; tmux sockets from the archives
  gone. Teardown: no
  `OMNIGENT_DATA_DIR=/tmp/omnigent-dev-data` pids; 7767 down;
  `127.0.0.1:6767/health` ok; pids 765 and 691 alive.

## Blocking failure

GOAL live proof requires loop **exit 0**. Independent re-run at
HEAD (command from `/tmp/omnigent-dev-data/smoke-repo`):

```
python3 /home/coder/personal/trio-agent-loop/omnigent/trioctl omnigent loop \
  --mailbox loop-smoke --max-iterations 1 --wait-timeout 1500 \
  --base-url http://127.0.0.1:7767
```

**exit 3**, `.driver.json` `phase: error`, smoke `VERDICT.md` empty,
LOG `unparseable verdict`. Lead *did* write `hello.txt` / `slice(hello)`
and REPORT. Evaluator archive
(`evidence/iter1/eval-eval-archive.jsonl`) has the user prompt plus
two short assistant acks (“I'll act as…”, “I'll read the goal…”)
and status still `running` at prune. Wait returned in ~20s.

Cause: `_wait_for_session` in `omnigent/trioctl` returns immediately
on `idle` + bound runner + `_has_completed_assistant_message`, with
**no** `stable_idle` dwell (that dwell only applies to the
idle+items-without-assistant branch). Cursor-native emits an early
completed assistant ack, the session can look idle, wait ends, prune
DELETEs the still-working evaluator. Lead's earlier smoke SHIP was
not reproduced.

## Scope

`omnigent/trioctl` symbols `_wait_for_session` (~745–820) and
`_has_completed_assistant_message` (~704). Do not treat first
assistant ack as role completion for cursor-native loop roles.
No product fix in this evaluator pass.

Evidence: `loop-omnigent-014/evidence/iter1/eval-*`.

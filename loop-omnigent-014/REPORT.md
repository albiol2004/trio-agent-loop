# REPORT — iteration 1 (Lead)

Continued from committed slices `395ccaa` / `05c2e22` / `981f91b`
(two earlier Lead sessions died; iteration not restarted).

## Worker model

`python3 omnigent/trioctl omnigent resolve builder --json`:

```json
{
  "model": "cursor-grok-4.6-medium",
  "model_effort": "medium",
  "provider": "cursor",
  "reasoning_effort": null,
  "role": "builder",
  "source": "cursor-model-list"
}
```

Scout/docs resolve to the same model family. This Lead pass did
not dispatch a builder worker; product edits were local.

## What landed

- `slice(host-bind)` already POSTs `host_id` + `workspace`, falls
  back to `POST /v1/hosts/{host_id}/runners` (200), records the
  session id, DELETEs on failed start.
- `slice(loop-reuse)` already retries the first prompt / restart
  blip on the **same** session (`_repost_wait_read`).
- `slice(doctor-skill)` + `05c2e22` doctor probe: import
  `_resolve_subagent_spec`, fall back to `_resolve_agent_spec`.
- `ed81328` `slice(tests-offline)`: fake broker hosts/runners,
  pending-prompt retry, no duplicate create, orphan DELETE.
- `eb73283` `slice(prune-running)`: id-scoped post-loop prune
  DELETEs even `running` sessions. Title-scoped prune still
  skips live sessions.

## Offline tests

`uv run --with pytest --no-project python3 -m pytest -q omnigent/tests`
→ **114 passed** (system Python has no `pytest` module).

## Doctor

- Live 0.12 PATH (`command:omnigent: /home/coder/.local/bin/omnigent`):
  all PASS, including `omnigent:session-contract`.
- 0.14 worktree PATH (`.../omnigent-dev/.venv/bin/omnigent`):
  all PASS, same contract line.

## Isolated 0.14 live proof

`free -g` available was 5 GB (> 3 GB), so the live proof ran.

Stack: data dir `/tmp/omnigent-dev-data`, server/host on
`127.0.0.1:7767`. Pre-loop `GET /v1/runners` → `200` `{"data":[]}`.
One online host `7761495813014886bdc996337f46f28b`
(`alejandro-netcup-81fba25e-ws`).

Command (from `/tmp/omnigent-dev-data/smoke-repo`, after copying
`metrics/trio_loop.py` + siblings so loop-core loaded):

```
python3 omnigent/trioctl omnigent loop --mailbox loop-smoke \
  --max-iterations 1 --wait-timeout 1500 \
  --base-url http://127.0.0.1:7767
```

**exit 0**, `.driver.json` `phase: shipped`.

| role | session id | dedicated runner |
| --- | --- | --- |
| lead | `f51ccdaa77d04e68a799fcf32c37fa2a` | `runner_token_b40559114f1a2400cdc132459c918760` |
| evaluator | `326df3ceeaca4001a3ed38762c7e7557` | `runner_token_6c1da0aaa00f150ccce219ee2b1076b2` |

Routes / status codes on 0.14:

- `GET /v1/hosts` → **200** (`{"hosts":[...]}`)
- `POST /v1/sessions` → **201** with `host_id` + workspace
  `/tmp/omnigent-dev-data/smoke-repo`. Create launched the
  runner; **fallback `POST /v1/hosts/{id}/runners` was not
  needed** this trial (host log: `Runner started` for the
  session id).
- `POST /v1/sessions/{id}/events` → **202**
- `GET /v1/sessions/{id}` and `/items` → **200**
- `DELETE /v1/sessions/{id}` → **200**
  `{"object":"conversation.deleted","deleted":true}`

Retry: lead runner logged a second user event at 13:16:03,
~20s after 13:15:42 (`TRIO_OMNIGENT_PROMPT_WAIT` default).
**One create, two events, same session id.** No duplicate Lead
session.

Smoke product: `hello.txt` is `hello\n`; commit
`slice(hello): add hello.txt` (`4809b27`). Evaluator
`VERDICT: SHIP`.

Mirroring: grep of isolated runner logs found **no**
`already mirrored`. Wait still uses items (plus status edges);
dedicated runners removed the collision on this trial.

Post-loop prune (code before `eb73283`) printed
`skip (running)` twice and deleted 0 — cursor-native stayed
`running` after wait. Cleanup rule: `DELETE` of those two ids
on 7767; tmux sockets `jkfbvud5` / `z3mvs2vy` gone. Then
SIGTERM of server `3356248`, host `3356916`, and zygotes whose
environ contained `OMNIGENT_DATA_DIR=/tmp/omnigent-dev-data`.
`7767` down. `eb73283` is the product fix for the skip.

## 0.12 (read-only `/home/coder/omnigent`, not tested on 6767)

`GET /v1/hosts` also returns `{"hosts": ...}` (`hosts.py` ~636).
`POST /v1/hosts/{host_id}/runners` exists (~729). Same
host_id-on-create flow is the sanctioned path; the live box
masks zero-runner hosts because UI sessions already have
runners. Do not share `--runner-id` across Lead/Evaluator
(transcript mirroring + 1h idle orphan).

## Cleanup / live host

- Isolated 7767 torn down.
- `curl -s 127.0.0.1:6767/health` → `{"status":"ok"}`
- pids **765** (`omni server`) and **691** (`omni host` on 6767)
  still alive.
- README.md user hunk never staged.

Evidence: `loop-omnigent-014/evidence/iter1/`.

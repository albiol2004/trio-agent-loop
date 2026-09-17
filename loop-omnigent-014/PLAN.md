# Plan — iteration 1

Make Trio Omnigent session start work on a fresh Omnigent
v0.14 host (zero pre-existing runners) and still work on
0.12. Dedicated per-session runners. No duplicate role
sessions. No orphans after a failed start. Doctor and
SKILL.md must match both trees.

## Verification standard

mode: implement-then-smoke

evidence:

- `python3 -m pytest -q omnigent/tests` green
  (this box: `uv run --with pytest --no-project python3 -m pytest
  -q omnigent/tests`, 114 passed).
- Isolated 0.14 live proof on 127.0.0.1:7767 as GOAL.md
  describes: smoke mailbox `loop-smoke/` creates `hello.txt`,
  `slice(hello): add hello.txt`, Evaluator SHIPs, loop exit 0.
- Evidence under `loop-omnigent-014/evidence/iter1/`
  (stdout/stderr, `.driver.json`, log excerpts, routes).
- Isolated stack torn down: no leftover daemon with
  `OMNIGENT_DATA_DIR=/tmp/omnigent-dev-data`.
- Live 0.12 host untouched: `curl -s 127.0.0.1:6767/health`
  ok; pids 765 and 691 still exist.
- `python3 metrics/trio-shadow.py --mailbox loop-omnigent-014
  --require-commits` exit 0.
- README.md unstaged hunk never staged.

## Iteration 1 increment

1. `create_session` sends `host_id` + mailbox `workspace` so
   the server launches a dedicated runner. Explicit
   `--runner-id` / `TRIO_OMNIGENT_RUNNER_ID` still binds an
   existing runner. Fallback: `POST /v1/hosts/{host_id}/runners`.
   Delete the session before raising if start fails after
   create; record the id first.
2. First prompt: wait for the user item or pending-input
   drain; re-post to the **same** session; never create a
   second session for the same role/iteration. Restart-blip
   retry reuses the bound session.
3. Doctor imports `_resolve_subagent_spec` then
   `_resolve_agent_spec`. SKILL.md host-selection text and
   0.14-existing `-k` tests.
4. Offline fake-broker coverage for host/runner launch,
   prompt retry, no-duplicate, orphan delete.
5. Isolated 0.14 smoke loop + cleanup.
6. Id-scoped post-loop prune deletes even `running` sessions
   (cursor-native stayed running after wait; title-scoped
   prune still skips live sessions).

## Acceptance

- Dedicated runner per role session via host_id/workspace.
- Failed start deletes the session (no orphan).
- First prompt retried on the same session; no duplicate
  create on restart blip.
- Mirroring: dedicated runners should remove it; if wait
  still depends on items, document in REPORT.md.
- Doctor green on live 0.12 PATH and on the 0.14 worktree
  env. SKILL.md selector exists upstream.
- `python3 -m pytest -q omnigent/tests` green.
- Isolated smoke loop exit 0; prune only its sessions;
  teardown per GOAL cleanup rule.

```yaml
slices:
  - id: host-bind
    repo: .
    writes: [omnigent/broker_http.py]
    reads: []
    gate: true
    status: complete
    iteration: 1
  - id: loop-reuse
    repo: .
    writes: [omnigent/trioctl]
    reads: [omnigent/broker_http.py]
    gate: true
    status: complete
    iteration: 1
  - id: doctor-skill
    repo: .
    writes: [omnigent/entrypoints/trio-omnigent/SKILL.md]
    reads: [omnigent/trioctl]
    gate: true
    status: complete
    iteration: 1
  - id: tests-offline
    repo: .
    writes: [omnigent/tests/test_trioctl.py, omnigent/tests/test_omnigent_loop.py]
    reads: [omnigent/broker_http.py, omnigent/trioctl]
    gate: true
    status: complete
    iteration: 1
  - id: prune-running
    repo: .
    writes: [omnigent/trioctl, omnigent/tests/test_trioctl.py, omnigent/tests/test_omnigent_loop.py]
    reads: []
    gate: true
    status: complete
    iteration: 1
  - id: wait-dwell
    repo: .
    writes: [omnigent/trioctl, omnigent/tests/test_trioctl.py, omnigent/tests/test_omnigent_loop.py]
    reads: []
    gate: true
    status: complete
    iteration: 1
  - id: review-fixes
    repo: .
    writes:
      - omnigent/trioctl
      - omnigent/broker_http.py
      - omnigent/tests/test_omnigent_loop.py
      - omnigent/tests/test_trioctl.py
    reads: []
    gate: true
    status: complete
    iteration: 1
```

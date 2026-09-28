# Worker observation rollout (installed pin e07075c)

Coalesced handoff after a verified install of candidate pin
`e07075c11269245e0650809f8412e89f0c050789`. This is a **pinned
release** from the candidate branch, not a merge into the original
repo. Read-only `CURRENT` at
`/home/coder/.local/share/trio-agent-loop/CURRENT` matches that pin.

Related notes (do not replace this rollout):
[WORKER-TIMING.md](WORKER-TIMING.md) (opt-in process-lifetime
shards), [WORKER-TIMING-TRIAL.md](WORKER-TIMING-TRIAL.md)
(historical induced two-builder trial),
[FIRST-PROMPT-DELIVERY-ROLLOUT.md](FIRST-PROMPT-DELIVERY-ROLLOUT.md)
(installed pin `44238aa`; first-prompt receipt, not a restart blip).

**Not in this release:** Jev integration. Session lifetime and
builder overlap are **not** useful-computation time and **not**
proof of speed improvement. Wave decisions are **agent-reported**,
not measured causality.

## User-facing observation

Default is **off**. Omit `--observe-workers` and the loop writes no
`.observe/` tree.

Exact opt-in (mailbox name is an example) — this pin predates r16b's
root-free refusals: `--observe-workers` on a root-free **open-loop**
mailbox (any git checkout) now exits 2 ("a root-free open-loop needs
isolated builders", nothing changed), since it prescribes its own
non-isolated worker command. Use it on a **lockstep** mailbox (no
`QUEUE.md`), which still accepts it (lockstep runs non-isolated by
default anyway), or on an open-loop mailbox outside any git checkout:

```text
trioctl omnigent loop --observe-workers --mailbox loop --max-iterations 3
```

When on, the driver records a run under
`{mailbox}/.observe/<run-id>/summary.json` (schema
`trio.observe_summary.v1`). Reconstruct later with
`trioctl omnigent observe-summary --mailbox <mailbox> --run-id <id>`.
Upgrade smoke confirmed default OFF (no `.observe`) and ON summary
plus observe-summary reconstruction.

Automatic worker recipe injects the **real release** `trioctl` path
and event fields. The Lead fills **task/slice only**.

## What else landed in this pin

- Stale model labels removed from role prompts.
- Effective evaluator prompt: SHIP retirement is mailbox-only; do
  not edit product, tests, or driver-owned `STATE.md`.
- Structured `iteration: N` fields accepted without confusing `1` with `10`.
- Pre/post snapshots reject a withdrawn SHIP or changed inputs
  during finalization, with bounded retry. Snapshot uses
  `git --no-optional-locks` and raw bytes.

A pre-existing TOCTOU (SHIP accepted after `VERDICT.md` retracted
to ITERATE) is recorded in
`/home/coder/workflow-lab/.runtime/upgrade-observe-workers/FINDING-retirement-toctou.md`.
Disposable upgrade `test-e07075c.log` ended `ALL_PASS`, including
the deterministic withdrawn-SHIP repro (exit 6). Review
`observe-review-e07075c/toctou-repro.out` is 3/3 reject.

## Install and rollback (do not run rollback here)

Record:
`/home/coder/.local/share/trio-agent-loop/upgrades/d435704-to-e07075c/`.
`apply-e07075c.log`:
`d435704c98008eb3e0a4ffdcd88b7edbb23cc21e` → `e07075c…`.
`postapply-verify-e07075c.txt`: CURRENT ok, adapters match,
old releases kept (`d435704`, `2d0d996`), profile + registry
unchanged, `OBSERVE_FLAG_PRESENT`, rollback script ready.
Existing drivers keep their **already loaded** release.

Rollback (record only; not executed by this note):

```text
bash /home/coder/.local/share/trio-agent-loop/upgrades/d435704-to-e07075c/rollback-upgrade.sh
```

Live doctor JSON: `ok: true`, **13** checks pass
(`live-doctor-e07075c.json`).

## Verification (do not imply the whole suite is green)

- `upgrade-observe-workers/test-e07075c.log`: `ALL_PASS` (upgrade,
  rollback, default OFF, worker recipe, withdrawn-SHIP).
- `observe-review-58dc42a`: SHIP for the prompt/parser changes.
  `observe-review-1d716d4`: ITERATE for two snapshot regressions,
  subsequently fixed in e07075c. Broad 58dc42a run: **782 passed**, 434 subtests,
  **1 failed**
  `test_derive_iterations_byte_identical_live_repo_scan`
  (known pre-existing live-mailbox scan). 1d716d4 focused: 33
  passed; two snapshot tests later failed when re-run at 1d716d4
  (`tests-at-1d716d4.log`) and were corrected on e070.
- `observe-review-e07075c`: SHIP. Focused log: **35 passed**.
  Isolated observation tests live under
  `omnigent/tests/test_observe_workers.py` (not re-counted here).

## Natural trial and retirement canary

Natural observe summary
`wave-natural-studio/loop-natural-trial/.observe/obs-20260923T204840Z-7eb67c6b/summary.json`:
Lead dispatched **2** Grok 4.6 Medium builders; overlap
`50514365859` ns (**50.514 s**). Product pin
`ab1f0d76ac30ba946cbecf378a48554b45e27335` (browser criteria
PASS). Original driver run **interrupted** (prompt/parser
defects), **not** an autonomous SHIP. No comparative speedup.

Isolated evaluator-only replay:
`eval-canary-retirement/REPLAY-DISCLOSURE.md` and `run-1d716d4/`.
Product still `ab1f0d7`. Model Grok Medium. **Code pin 1d716d4,
not e070.** Browser C1–C6 PASS on rerun. Clone omitted local
Cursor excludes and ignored original telemetry; both restored
during the run; hash manifest + provenance kept. No fresh
builders. Evaluator retirement
`0c7736867a1b7883a80c7b3ace7fb1525ce96c76`; reflog shows that
one commit; mailbox-only inside the eval window. Archived
transcript has **messages, not tool calls** — do not claim
direct tool-call proof. Driver `STATE` shipped; prune
archived 1, deleted 1, failed 0. Raw process exit code was
**not captured** (detached launch). e070 core later accepted
that real retirement read-only
(`validate-e07075c-core.txt`: retirement problem `None`).
Final snapshot corrections were tested/reviewed separately.

## Known follow-up (not a ship blocker)

Earlier notes called a duplicate first-prompt POST a broker
**false restart-blip**. That was the wrong cause. The loop treated
a missing or truncated user row as a miss inside a **20s** window
and re-posted; an intact receipt is a matching normalized user
row, not an idle/restart edge. Pin `44238aa` changes that
contract. See
[FIRST-PROMPT-DELIVERY-ROLLOUT.md](FIRST-PROMPT-DELIVERY-ROLLOUT.md).
Do not claim perfect loop reliability.

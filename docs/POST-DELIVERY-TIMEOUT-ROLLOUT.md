# Post-delivery timeout hold (installed pin 31f5a5a)

Coalesced operator note after a verified install of
`31f5a5a93cb39a4fad05005f62268688e196ce77`. Pinned release, not a
merge into the original repo. `CURRENT` is this hash. `trioctl`
sha256
`a6c0fe9285f2a57ef7704b9d82311a04d94d0cc0dc689aee7b0564e05cc081f0`.

First-prompt receipt (44238aa) is still
[FIRST-PROMPT-DELIVERY-ROLLOUT.md](FIRST-PROMPT-DELIVERY-ROLLOUT.md).
That note’s leftover **T1** (confirmed receipt, then role timeout,
resume duplicates the role) is **fixed here**. Observation pin
`e07075c` remains in
[WORKER-OBSERVATION-ROLLOUT.md](WORKER-OBSERVATION-ROLLOUT.md).

**Not claimed:** perfect live reliability, a fully green broad
suite (baseline env fails remain), or that already-running loops
picked up this pin. Profiles and agent registry hashes were
unchanged on apply.

## What the loop does after a confirmed first prompt

1. The role has posted. Receipt is intact. Then the wait ends
   with a **timeout or broker error** (GET session / GET items).
2. **Durable hold.** Write
   `{mailbox}/.sessions/held-<session-id>.json` with
   `hold=role_completion_uncertain`. Set STATE `status`/`phase`
   to `needs_human`. **Do not DELETE** the session.
3. A later `trioctl omnigent loop` process **exits 7** with
   **zero HTTP** until a human reconciles. No auto-resume.
4. **Open-loop slice-eval:** a **partial** section at the
   deadline is **never** last-chance accepted (that was F1 on
   `e060f61`). A **complete** section whose pane is **still
   running** is also held (conservative; parent used to accept).
5. Other roles still use last-chance accept only with a **strict
   fresh artifact**. A SHIP without a retirement commit is
   `needs_retirement`, not shipped.

## If you see `needs_human` (post-delivery)

```text
trioctl omnigent session read <session-id>
# wait until the pane is done or gone
# reconcile the actual completed output (mailbox + git)
rm <mailbox>/.sessions/held-<session-id>.json
# set STATE.md off needs_human to the next real step
# then re-run; prune can DELETE afterward
```

Do not start a second Lead/Evaluator on the same iteration while
the held session may still run. Clearing the hold without that
reconcile can duplicate work.

## Install and rollback (do not run rollback here)

Upgrade record:
`~/.local/share/trio-agent-loop/upgrades/44238aa-to-31f5a5a/`.
`apply-31f5a5a.log`: `UPGRADE_OK` `44238aa…` → `31f5a5a…`.
Qualification record sha
`c66549d03eab29030b5a8b953f1ae72383302353e8b2ec036e403f24c8af896b`
(gates **v5**). Live doctor JSON: `ok: true`, **13** checks
(`.runtime/upgrade-post-delivery-timeout/live-doctor-31f5a5a.json`).

A hold written by this pin still blocks the **old** 44238aa
rollback adapter (exit 7, zero broker). That prevents a
duplicate after rollback. Rollback script is ready, **not run**:

```text
bash ~/.local/share/trio-agent-loop/upgrades/44238aa-to-31f5a5a/rollback-upgrade.sh
```

## Verification (limits)

Kit: `.runtime/upgrade-post-delivery-timeout/`.
`qualify-31f5a5a.log`: `QUALIFIED`, gates v5, native014 **38**
scenarios **0** gated fail, pin suite **249** passed. Disposable
upgrade test: **20** sections **ALL_PASS**. Bound delta:
`trioctl` only; broker_http / worker_events / trio_loop /
prompts match 44238aa.

Independent review (no live install in those folders):
`.runtime/eval-31f5a5a-opus/VERDICT.md` **SHIP**; parent
`.runtime/eval-e060f61-opus/VERDICT.md` **ITERATE** (F1).
Focused: open_loop_driver + first_prompt_redelivery +
post_delivery_hold **92/92**, omnigent_loop **59/59**, pin
probes **31/31**. **R1**, **R2 IncompleteRead**, **R3** stay
open in those verdicts. Do not claim the broad suite all-green.

**Live T1 canary (staged pin, before global promotion):**
`.runtime/t1-canary-31f5a5a/live-staged-1/{meta,run1,run2,observe,cleanup,summary}.json`.
Ran `python canary/t1_canary.py --adapter staged … --share …`,
not the shell install-first launcher. `fake: false`, **14**
checks, `T1_PASS`. Grok 4.6 Medium, 1 session, 1 POST, full
receipt. Role ~**76.6s**, held; original pane kept running and
replied. Second process **exit 7**, **zero HTTP**. Cleanup
stop → archive → DELETE; own runner offline.

## Known leftovers (do not auto-fix)

- **R1:** same-iteration lead line already in before-text can
  make a later LOG.md change look ready.
- **R2:** `http.client.IncompleteRead` can escape unwrapped;
  not held like `BrokerHttpError`.
- **R3:** accept while the pane still runs leaves the session
  in prune; bounded by retirement wait.
- Slice-eval complete-but-still-running at deadline: human
  resume cost (safer than a second Evaluator).

# First-prompt delivery rollout (installed pin 44238aa)

Coalesced operator note after a verified install of candidate pin
`44238aa04c6d255236450bcb41e5db4a3bd2da5f`. Pinned release, not a
merge into the original repo. Read-only `CURRENT` at
`/home/coder/.local/share/trio-agent-loop/CURRENT` matches that
hash. Broker file sha256
`22f00da15de13f34232e4694b5ed89fac451dde4510d14e91701b35bcdc432d7`.

T1 (confirmed receipt, then role timeout, resume can
duplicate) is **fixed** at pin `31f5a5a`; see
[POST-DELIVERY-TIMEOUT-ROLLOUT.md](POST-DELIVERY-TIMEOUT-ROLLOUT.md).

Observation pin `e07075c` is still documented in
[WORKER-OBSERVATION-ROLLOUT.md](WORKER-OBSERVATION-ROLLOUT.md).
That note used to blame a **restart blip** for a duplicate first
prompt. The real check is **initial prompt receipt** (normalized
user row), described here.

**Not claimed:** speedup, exactly-once delivery, a fully green
254-test plus 36-adversarial suite as the install gate, or that
the globally copied skill file is already rewritten. New loops
load this release’s `broker_http`; a stale skill copy can still
say “20s retry”. Profile and agent registry hashes were unchanged
on apply; already-running loops keep their loaded pin.

## What the loop does now

1. POST `/v1/sessions/.../events` once, then poll.
2. **Landed:** a user row equals the prompt after paste-style
   normalize (CRLF→LF, drop controls except tab, strip). Presence
   of any user row or an assistant row is not enough.
3. **No re-post, ever:** a saved user row that does not match
   (DEL-prefixed, truncated, foreign) may be a turn that ran, so
   it holds like a missing row. After the wait, an unbound runner
   with no items (restart blip) holds the same session as
   `role_completion_uncertain`; it is not re-sent.
   `TRIO_OMNIGENT_PROMPT_ATTEMPTS` is deprecated and not read.
4. **Uncertain (never re-post, never DELETE):** no matching row
   yet when the **single** deadline ends. Default wait
   `TRIO_OMNIGENT_PROMPT_WAIT` **600s**, also capped by the
   caller’s role timeout. Welcome-screen drop vs slow turn cannot
   be told apart; both hold.
5. **Definite refuse (may DELETE):** HTTP **4xx** on POST before
   a copy is recorded. **5xx**, timeout, reset are ambiguous:
   hold.
6. Hold writes `{mailbox}/.sessions/held-<session-id>.json` and
   sets STATE `status`/`phase` to `needs_human`. Resume is
   blocked until a human reads the pane, reconciles mailbox/git,
   deletes that file, and sets STATE for the next step. No
   automatic resume.

Env still honored: `TRIO_OMNIGENT_PROMPT_INTERVAL` (default 0.4s).

## If you see `needs_human`

```text
trioctl omnigent session read <session-id>
# inspect mailbox + git; wait until the pane is done or gone
rm <mailbox>/.sessions/held-<session-id>.json
# edit STATE.md off needs_human to the next real step
# then re-run; prune can DELETE the old session afterward
```

Do not start a second Lead/Evaluator on the same iteration while
the held session may still run.

## Install and rollback (do not run rollback here)

Upgrade record:
`/home/coder/.local/share/trio-agent-loop/upgrades/e07075c-to-44238aa/`.
`apply-44238aa.log`: `UPGRADE_OK` `e07075c…` → `44238aa…`.
Qualification record sha
`e9ee2537c40ef4d47478fac757f851a1d8c294164784d35aca789bf1e9ba91d4`.
Live doctor JSON: `ok: true`, **13** checks
(`/home/coder/workflow-lab/.runtime/upgrade-first-prompt/live-doctor-44238aa.json`).

Rollback (record only):

```text
bash /home/coder/.local/share/trio-agent-loop/upgrades/e07075c-to-44238aa/rollback-upgrade.sh
```

## Verification (limits, not a broad green)

Gates **v3** (`qualify-44238aa.log`): `status=QUALIFIED`,
`gates_version=3`, native014 probe **38** scenarios,
`gated_failures=none`. Pin pytest in that qualify pass: **148**
passed (the reviewer separately ran 254 tests across eight files).
`test-44238aa.log`: **ALL_PASS** (upgrade/rollback, held two-run,
installed probe, **profile + registry unchanged**).

Reviewer mailbox
`.runtime/eval-44238aa-opus/VERDICT.md`: scoped **SHIP**. That
write-up also ran 254 + 36 adversarial with **1 expected**
failure (legacy G1c: role timeout after a confirmed receipt,
resume creates another Lead). That is **pre-existing on this
pin**, not the 44238aa install gate. Later pin `31f5a5a`
holds that case; see
[POST-DELIVERY-TIMEOUT-ROLLOUT.md](POST-DELIVERY-TIMEOUT-ROLLOUT.md).
Do not treat 44238aa as automatic-resume policy.

**Live canary (enough; n=2):**
`.runtime/first-prompt-canary/runs-44238aa/{c1,c2}-sleep20-9k/`
plus `c1.out` / `c2.out`. Evaluator, Grok 4.6 Medium, ~9 KB
prompt, `prompt_wait_s=600`, no artificial delay. Both:
`event_posts=1`, one intact receipt, one token reply, 180s after
idle with no late duplicate, `exit=0`, cleanup
stop→archive→delete, own runner offline. First user row ~**15s**
in both. **No live sample** of an intact row after 20s, and **no
live** corruption-repair (re-post after truncated paste). Those
paths are offline/probe only. A legacy worker note that “there
were no live tests” is wrong; use these artifacts.

Generic runner leftover after normal cleanup is a **separate**
follow-up, not this contract.

## Known leftovers (do not auto-fix)

- **T1 (open on this pin):** confirmed receipt, then
  role-artifact timeout, STATE still `running`; a resume can
  duplicate the role. **Closed at `31f5a5a`:**
  [POST-DELIVERY-TIMEOUT-ROLLOUT.md](POST-DELIVERY-TIMEOUT-ROLLOUT.md).
- Welcome / no-row by 600s: **held + human**, not retry.
- Conservative uncertainty handling, not exactly-once.

VERDICT: SHIP
# Verdict — iteration 6
attempt: 89522bdd08a947d1886413d773398cca
evaluated: f4e786a7814d48e0db6d5d346bdb0ec0586f34f4

## What changed since last verdict
Iteration 5 was ITERATE scope=local on one blocking issue: `_load()` dropped the v1 root entries in a schema-2 inbox-state.json. The repair pass made one commit, f4e786a `slice(ids-migration): carry v1 root entries written into a schema-2 inbox-state …`. It adds 14 lines to `dashboard/inbox_state.py`: in the schema-2 branch, top-level absolute-path keys holding dict records are folded into `legacy`, unioned with the existing list, and roots gone from disk are GC'd. It also adds one test to `registry/tests/test_inbox_ids_migration.py`. Nothing else changed: `git diff --stat f2a2857 f4e786a` lists only those 2 files, +45 lines. The iteration-5 blocking issue is fixed. Nothing that failed last iteration still fails, so this is not a stuck loop. I graded the pin in `.claude/worktrees/eval-e7d1c3283e2446f2b22e940b28ed4dd1-6-89522bdd`, a detached worktree at f4e786a.

## Criteria results
| # | criterion | result | evidence | command | key output |
|---|---|---|---|---|---|
| R6-1 (iter-5 blocking 1, GOAL DoD1 migration) | v1 read marks written into a schema-2 document are carried | PASS | implementer-test, red-before-green by me | new test run against the f2a2857 tree (git archive into scratch, new test file copied in), then at the pin | base: `AssertionError: False is not true : a v1 read mark in a schema-2 document must be carried`, `1 failed, 14 passed`. Pin: `15 passed in 9.82s` |
| R6-2 (iter-5 guidance) | promote_probe on a fresh COPY of the live file gives A read == B read (47) | PASS | re-run | `python3 loop-dash-state/probes/iter-5/promote_probe.py <pin-wt> <scratch>/promote 22495 22496` | `{"A_as_is": {"items": 143, "read": 47, "unread": 96}, "B_v1_marks_folded": {"items": 143, "read": 47, "unread": 96}} items read only when v1 marks are carried: 0`, `live sha unchanged True` (iter 5: A was 12 read) |
| R6-3 (GOAL diag 2 / DoD1, second-path oracle) | nothing the user has read today resurfaces after promotion | PASS | probe (mine) | `python3 loop-dash-state/probes/iter-6/v1_oracle.py <pin-wt> <scratch>/oracle 22491 22492`: the LIVE v1 release code and the pin, each on its own copy of the live file, then (loop, kind) read sets compared | v1: `items 286 read 70`, which is 80 (loop, kind) keys, 29 of them read. Pin: `items 144 read 47`. `read in v1, absent in v2: 0`, `read in v1, UNREAD in v2: 0`, `read in v2 but not v1: 1` (a slice_overlap item read in the 2026-10-03 v2 items; read wins). Migrated copy: 0 top-level root keys left, `migrated_at` kept at 2026-10-03T10:15:07Z |
| DoD7 / full_check | baseline-equal at the pin | PASS | re-run | full_check from PLAN.md in the eval worktree | `2 failed, 834 passed, 2 skipped, 806 subtests passed in 370.20s`. The 2 failures are the known baseline pair: test_install_claude_written_to_temp_dir_not_home and test_cursor_opt_in_runs_isolated_ask_mode_and_stores_result. Then `prompt sync OK (64 generated files match the tree)`, `20 passed`, `vendor check ok: 9 files verified`. 834 = 833 + the new test |
| DoD3 + DoD6 goal_probe | 0 rebuilds over ≥ 65 s across a minute boundary with the drawer open; UI state and URL survive; reconnect works | PASS | probe (iter-4 Evaluator probe re-run at the pin) | `lab.py <pin-wt> /dev/shm/ev6a 22494 1`, then `node probe.cjs /dev/shm/ev6a 22494 probeA.json 66000` | idle 66005 ms (21:17:13 -> 21:18:19). All 23 sections: `childList 0, attributes 0, charOther 0`. `state_before == state_after_idle == state_after_rewrite`. Rewrite shown in 2388 ms. Reload, Esc, wsb / special-name / name-only deep links all OK. Restart: `0.5:Reconnecting` -> `9.8:Live`. errors [], foreign [], pwned null |
| DoD1 (a)/(b)/(c), DoD2, DoD4, DoD5, install-port | no regression | PASS | re-run (full suite) | full suite above | no new failures. The only product change is inside `inbox_state._load`'s schema-2 branch, and the iter-5 probes of these criteria ran on code that is otherwise identical |
| C2-6 (no AI trailers) | waived | verified (waiver) | human answer 4828084fdce9, driver-verified in iteration 5 | — | the owner's waiver: "AI co-author trailers are acceptable in trio-agent-loop". f4e786a also carries one; see observation 2 |
| Constraint: live inbox-state.json untouched | | PASS | re-run | sha256 at start and at end | `18218b24…46b5` both times, mtime 2026-10-08 15:40:33 |
| Constraint: small coherent slice commit | | PASS | re-run | `trio-shadow.py --json` | f4e786a is attributed to slice ids-migration; its files are within the declared writes; `undeclared_touches: []` |
| LOG.md gate | repair iter-6 entry present | PASS | re-run | grep | `- iter 6 \| repair \| inbox_state._load folds top-level v1 root entries …` |

Test-integrity audit: `git diff f2a2857 f4e786a -- registry/` only adds one test method (+31 lines). No assertion was deleted, skipped or weakened.

REPORT.md cross-check: REPORT.md is still iteration 5's; the repair pass leaves its record only in LOG.md. Its claim "live-copy probe A==B (47 read)" matches my re-run exactly.

## Independent probe
probe: PASS. On a copy of the live state, everything the live v1 service shows as read is still read after the pin's migration (0 resurfaced). The goal_probe still passes too.
probe_cmd: `python3 loop-dash-state/probes/iter-6/v1_oracle.py /home/coder/personal/trio-agent-loop/.claude/worktrees/eval-e7d1c3283e2446f2b22e940b28ed4dd1-6-89522bdd <scratch>/oracle 22491 22492`, then a (loop, kind) read-set comparison of v1-items.json vs v2-items.json. Also the goal_probe (`lab.py` + `probe.cjs`), `promote_probe.py`, `rollback_attack.py` and `malformed_attack.py`.
probe_src: loop-dash-state/probes/iter-6/{v1_oracle.py,rollback_attack.py,malformed_attack.py}
expected: GOAL DoD1 "One-time migration of inbox-state.json: carry existing read marks onto the new ids where they map unambiguously; GC roots that no longer exist". GOAL diagnosis 2 "Read notifications resurface" must stop. Constraint: the migration "ships to run on next start" against the live file.
observed: `v1 items 286 read 70` / `v2 items 144 read 47`; `read in v1, absent in v2: 0`; `read in v1, UNREAD in v2: 0`; `live sha unchanged True`. The goal_probe shows 0 childList/attributes/charOther records in all 23 sections over 66 s.

attacks:
- Second-path oracle: the LIVE v1 release code vs the pin, each on its own copy of the live inbox-state.json (v1_oracle.py) -> 0 of the 29 (loop, kind) keys read under v1 come back unread under the pin. The pin's 72 keys cover all 80 v1 keys except 8 `dangling_worktrees` items. Those come from release-only code that is not on main (observation 1). None of the 8 is read.
- Promote -> rollback to v1 -> mark 2 unread items read under v1 (3 POSTs; one loop has 2 views) -> promote again (rollback_attack.py) -> after v1 there are 43 top-level root keys, all folded on restart. But all 3 marks are lost: the pin's `items` already stored those identities as unread, and the fold only applies to identities seen for the first time (observation 3).
- Schema-2 document with hostile top-level keys through the public `set_read` (malformed_attack.py). The keys: a non-list `read`, a list with ints/None, a relative key, a missing root, a non-dict value, an unknown non-path key, and the same root with a trailing slash -> no crash. Only valid string marks of existing absolute roots were kept. A second load is a fixed point (`idempotent True`).
- Re-ran the iter-5 promote_probe at the pin -> A_as_is read 47 == B read 47 (iter 5: 12 vs 47); 0 items read only when the v1 marks are carried.
- Lab server SIGKILLed under the open page and restarted (goal_probe restart step) -> Reconnecting after 0.5 s, Live after 9.8 s, no page errors.

## Blocking issues
None.

## Non-blocking observations
1. **The live release has code that main lacks.** The live release (4d6646b, its sha is not in this repo's object DB) calls `native_facts(mailbox, home, root_mailbox)`. main's `native_facts(mailbox, home)` does not take a root mailbox. As a result, 8 `dangling_worktrees` inbox items that the live service shows for syngenta_p_l / wt-hub-create loops do not appear at the pin. This divergence predates this loop: `native_facts` has the same signature at 51f12f6, at the pin and on acceptance-strength. When the user builds the release to promote, they should reconcile that release-only code with this branch.
2. **One more AI trailer, covered by the waiver.** f4e786a carries a `Co-Authored-By: Claude Sonnet 5.5` trailer. The owner's waiver (4828084fdce9) covers trailers in this repo in general, so this is not blocking.
3. **Rollback marks are lost for items the pin already saw** (attack 2). After a promote -> v1 rollback -> promote cycle, marks the user set under v1 on items the pin had already stored are not carried. The fold only seeds identities seen for the first time. Iteration 5's guidance set this as the minimum, the GOAL asks for a one-time migration, and the real live file loses nothing (the oracle). It is still worth a follow-up:
   - in `decorate_items`, let an unread stored record become read when `_legacy_read` is true for it ("read wins", D1);
   - or record a fold watermark.
4. Iteration 5's observations still stand, unchanged:
   - the stale `/api/sessions` and `/api/transcript` docstrings in serve.py;
   - the stale lab servers :22437, :22438, :22439, :22447, :22448, :22473 (the human can `kill 2832104 2326336 2326531 2357730 2760458 2346321`);
   - the D5-1 deferrals.

   My own labs (:22491-22496) are stopped.

## Guidance for next iteration
SHIP. The product commit is already on main as f4e786a, so the tree is clean and there is no new product commit.
Suggested commit message (already used): `slice(ids-migration): carry v1 root entries written into a schema-2 inbox-state (v1 service after a premature migration)`.
Follow-up GOAL candidates:
- observation 3: read-wins for already-stored items after a rollback;
- reconcile the release-only `root_mailbox` native-facts code (observation 1);
- the D5-1 items NB1, NB2, NB6 and iter-3 NB3;
- the serve.py docstring refresh.

## Remaining real-world steps
- The user promotes a release containing f4e786a to the live trio-dash service after review. The agents never restart or repoint :22000. The migration then runs on the next start against the live inbox-state.json. On a copy of that file, the pin's migration resurfaced none of the items the user has already read.

commit: f4e786a7814d48e0db6d5d346bdb0ec0586f34f4
evaluated: f4e786a7814d48e0db6d5d346bdb0ec0586f34f4

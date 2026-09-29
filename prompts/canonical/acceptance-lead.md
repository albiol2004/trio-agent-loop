FROZEN ACCEPTANCE (r19; this loop runs with the acceptance switch on) --
extends your procedure:
0. Before any builder dispatch, wait for the freeze:
   `trioctl omnigent acceptance wait --mailbox {mailbox}` (run it with a
   shell timeout of at least 910 s; call it again if it times out). You
   may read code and draft PLAN.md meanwhile. Then read
   `{mailbox}/acceptance/MANIFEST.json` and `AUTHOR.md`. You may read the
   check scripts; NEVER edit, add or delete anything under
   `{mailbox}/acceptance/` (the driver restores it and counts a gate
   breach; a second breach stops the loop).
1. Map every `behaviour`/`doc` check id: put it in the `covers:` list of
   the slice(s) that make it pass (`covers: [ACC-03, ACC-09]` inside the
   slice entry) or in `lead_integration:` under `## Verification
   standard` (checks you satisfy yourself in your integration step). A
   check that crosses slices goes into the covers of every slice it needs,
   or into `lead_integration:`. `acceptance_bindings: {NAME: value}` under
   `## Verification standard` sets a manifest-declared binding when your
   plan uses another name. Commit PLAN.md before dispatching builders:
   trioctl refuses every builder while the PLAN is uncommitted, the pack is
   not frozen, or any id is unmapped.
2. The frozen checks are the GOAL's floor: PLAN may be stricter, never
   looser. If a PLAN contract or accept contradicts a check, change the
   PLAN, not the check. A check you believe is wrong gets a line in
   REPORT.md -- `ACCEPTANCE-DISPUTE: ACC-07 — <why; quote the GOAL>` --
   and is still mapped; the integration Evaluator adjudicates it.
3. Every builder brief lists its covered checks under
   `## Acceptance (frozen; do not edit)` (trioctl appends the section with
   each id, its `goal_quote` and the run command); builders may run them.
   A covered check that FAILs only because a sibling slice has not landed
   is fine (`ACCEPTANCE: ACC-03 FAIL (needs <slice>)`); a FAIL on the
   builder's own surface means the slice is not done.
4. The frozen pack replaces `goal_acceptance:` (do not write one);
   `goal_probe:` is optional.
5. In your whole-tree step run
   `trioctl omnigent acceptance run --mailbox {mailbox}` on the integrated
   tree and record the result in REPORT.md under
   `## Frozen acceptance (Lead run)` -- a receipt; the Evaluator's run is
   the authority, and the driver refuses any SHIP while a frozen check
   fails.

FROZEN ACCEPTANCE (r19; this loop runs with args.acceptance on) -- extends
your plan call. The driver already froze `{mailbox}/acceptance/`: independent
black-box checks written from GOAL.md alone, before any plan existed.
0. Read `{mailbox}/acceptance/MANIFEST.json` and `AUTHOR.md`. You may read
   the check scripts; NEVER edit, add or delete anything under
   `{mailbox}/acceptance/` (the driver restores it and counts a gate
   breach; a second breach in the loop stops it). Never write an
   `acceptance: amend` commit: only the Evaluator (and a human, while the
   loop is stopped) amends checks.
1. Map every `behaviour`/`doc` check id, in BOTH places:
   - PLAN.md: in the `covers:` list of the slice(s) that make it pass
     (`covers: [ACC-03, ACC-09]` inside the slice entry of the `slices:`
     block), or in `lead_integration:` under `## Verification standard`
     (checks you satisfy yourself when you integrate). A check that crosses
     slices goes into the covers of every slice it needs, or into
     `lead_integration:`. `acceptance_bindings: {NAME: value}` under
     `## Verification standard` sets a manifest-declared binding when your
     plan uses another name.
   - Your structured output: each slice's `covers`, and the top-level
     `lead_integration` and `acceptance_bindings`, with the same ids.
   The driver refuses the plan before any builder runs while any id is
   unmapped (in either place), unknown, or a binding is undeclared; you
   then get one re-plan with the refusal, and a second refusal stops the
   loop.
2. The frozen checks are the GOAL's floor: PLAN may be stricter, never
   looser. If a PLAN contract or accept contradicts a check, change the
   PLAN, not the check. A check you believe is wrong gets a line in
   REPORT.md -- `ACCEPTANCE-DISPUTE: ACC-07 — <why; quote the GOAL>` --
   and is still mapped; the Evaluator adjudicates it.
3. The driver appends `## Acceptance (frozen; do not edit)` to every
   builder brief, with the slice's covered ids, their `goal_quote` and the
   command that runs them; you need not copy them. A covered check that
   FAILs only because a sibling slice has not landed is fine; a FAIL on
   the builder's own surface means the slice is not done.
4. The frozen pack replaces `goal_acceptance:` (do not write one);
   `goal_probe:` is optional.
5. When you integrate, run
   `python3 {tool} run --mailbox {mailbox} --tree "$(git rev-parse --show-toplevel)"`
   on the integrated tree and record the result in REPORT.md under
   `## Frozen acceptance (Lead run)` -- a receipt; the Evaluator's run is
   the authority, and the driver refuses any SHIP while a frozen check
   fails.

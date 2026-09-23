VERDICT: ITERATE
# Verdict — iteration 1 (illustrative)
attempt: 1
evaluated: ui-example-illustrative

## Criteria results
- GOAL save: PASS (local unit test). Not sole oracle.
- GOAL empty-error: FAIL completeness — present in GOAL.md, absent from
  PLAN checklist despite green tests.
- GOAL stay-editable: unverified / omitted.

## Blocking issues
1. Original GOAL error and state-transition criteria were dropped.
   Phrase tests on the happy-path file do not enforce that judgment.

## Guidance for next iteration
Restore the omitted GOAL refs in the checklist and exercise the UI
error path. Keep first-line verdict syntax unchanged.

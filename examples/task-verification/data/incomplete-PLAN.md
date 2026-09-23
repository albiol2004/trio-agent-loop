# Incomplete plan (Evaluator should reject)

## Verification standard
mode: implement-then-smoke
evidence: `test_row_count.py` passes.

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL schema | run once | file exists | unit test | verified |

Reconciliation and second-run digest rows are missing from the table.
Green local tests are not enough. Evaluator ITERATE; no whole-goal SHIP.

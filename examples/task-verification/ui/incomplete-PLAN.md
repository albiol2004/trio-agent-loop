# Incomplete plan (Evaluator should reject)

## Verification standard
mode: implement-then-smoke
evidence: `python3 -m unittest test_save_happy.py` passes.

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL save | type "Oak desk"; click Save | "Saved" | unit test | verified |

Passing `test_save_happy.py` alone is not enough. The error and
stay-editable GOAL rows are missing from the table above.

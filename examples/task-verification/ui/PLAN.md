# Plan — iteration 1

## Iteration 1 — current increment
Wire Save, empty-title error, and stay-editable after error.

## Verification standard
mode: implement-then-smoke
evidence: exercise the three GOAL flows; unit tests are not sufficient.

Checklist filled from GOAL.md **before** implementation (not from tests).

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL save | catalog form open; type "Oak desk"; click Save | visible "Saved"; title still "Oak desk" | UI interaction + screenshot or DOM text | unverified until run |
| GOAL empty-error | same form; clear title; click Save | visible "Title is required"; no success banner | error-state UI check | unverified until run |
| GOAL stay-editable | immediately after empty-error | title field accepts new keys without reload | state-transition UI check | unverified until run |

Passing `test_save_happy.py` alone does not close GOAL empty-error or
stay-editable.

```yaml
slices:
  - id: catalog-save
    writes: [examples/task-verification/ui/]
    reads: []
    accepts: ["Save, empty error, and stay-editable match GOAL"]
```

# Plan — iteration 1

## Verification standard
mode: implement-then-smoke
evidence: independent SQL/python sums, schema dump, second-run digest.

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL reconcile | `orders.csv` 3 rows: 100, 250, 50 cents | `daily_sales` total 400; independent sum not the pipeline's `total` field | reconciliation query | unverified until run |
| GOAL schema | after one successful run | columns `day,store_id,amount_cents`; int cents | schema check | unverified until run |
| GOAL rerun | same inputs, empty output dir | output SHA-256 matches first run (provenance) | second-run digest | unverified until run |

```yaml
slices:
  - id: daily-rollup
    writes: [examples/task-verification/data/]
    reads: []
    accepts: ["Reconcile, schema, and rerun provenance match GOAL"]
```

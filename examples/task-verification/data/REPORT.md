# Report — iteration 1 (illustrative)

Not an executed warehouse run.

## How I verified it
- GOAL reconcile: **verified** at illustrative rev `data-example` —
  independent sum 100+250+50 = 400, matched output.
- GOAL schema: **verified** — header `day,store_id,amount_cents`.
- GOAL rerun: **failed** — second run wrote a new timestamp column
  and the digest moved. Product failure, not unavailable environment.

A model-authored `test_row_count.py` that asserts `len(out)==1` would
still be green. Evaluator must not SHIP.

# Report — iteration 1 (illustrative)

Not an executed product run. Honest sample of how to fill results.

## How I verified it
- GOAL save: **verified** at illustrative rev `ui-example` — typed
  "Oak desk", clicked Save, saw "Saved".
- GOAL empty-error: **unverified** — no headed browser in this
  environment (unavailable environment, not a product FAIL).
- GOAL stay-editable: **unverified** — depends on the error path.

Unit tests `test_save_happy.py` passed. That does not verify the
empty-title error. Whole-goal SHIP is not allowed while GOAL rows stay
unverified.

# Goal
profile: data

Build a daily sales rollup from two synthetic extracts. Totals must
match the source to the cent. Schema stays stable. A second run with
the same input must reuse the same output digest.

## Definition of done
- Sum of `amount_cents` in `orders.csv` equals sum in `daily_sales.csv`
  (independent of the pipeline's own count column).
- Output columns are exactly `day,store_id,amount_cents` with integer
  cents.
- Re-run from the same inputs yields the same output SHA-256.

## Constraints
- Synthetic CSVs only. No warehouse credentials.

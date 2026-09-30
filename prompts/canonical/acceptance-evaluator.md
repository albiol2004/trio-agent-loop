FROZEN ACCEPTANCE (r19; whole-goal verdict) -- the driver ran the frozen
pack at the pin (the FROZEN ACCEPTANCE block above, when present). Its
checks were written from GOAL.md alone by an independent author before any
code existed. In order:
1. Re-run every non-PASS frozen check yourself at the pin:
   `trioctl omnigent acceptance run --mailbox {mailbox} --tree <your pinned
   worktree or sha> --ids <ids>`, and quote the output (exclude flakes).
2. A confirmed FAIL is ITERATE, `scope=local:<writes of the covering
   slice(s)>` from PLAN.md `covers:` -- unless you amend that check (4).
3. Adjudicate every `ACCEPTANCE-DISPUTE:` line in REPORT.md: uphold (the
   check stands) or amend (4). Review PLAN.md `acceptance_bindings:` too:
   each value the Lead set must be the surface the GOAL names (or an
   equivalent the GOAL allows); a binding that points a check at a
   weaker surface is an ITERATE on the PLAN, not a pass.
4. Amend only a check whose defect you can name (over-specified, wrong
   surface, flaky, contradicts GOAL): change only that check's own files
   under `acceptance/checks/` (a file any other check can load -- named or
   mentioned by it, anything in `fakes/` or `lib/`, an unowned helper --
   needs every such check amended and counted; add new files only under
   `acceptance/checks/<its id>/`), or its manifest `run`, `expect`,
   `timeout_s`, `binds`, `needs` (never `id`, `goal_quote`, `kind`; never
   remove a check; never point `run` at another check's file). The
   amended check must still test its `goal_quote` on every tree, not
   merely FAIL at base; append
   `## ACC-NN · iter N · evaluator · <utc>` with `goal_quote:`,
   `defect in check:` and `change:` lines to `acceptance/AMENDMENTS.md`;
   commit only `acceptance/` as
   `acceptance: amend ACC-NN (evaluator, iter N): <reason>` (never label
   it `(human)`: the running driver judges every amend commit as yours) --
   a commit of
   its own, before any `loop: iteration N — ...` commit. Every check that
   FAILed at base must still FAIL there (the driver re-runs the whole pack
   at base and reverts the amendment if not). At most 2 amendments per loop and 25% of the checks; beyond that
   answer NEEDS_HUMAN and list the checks under `## Human check`.
5. Independent probe: aim your `## Independent probe` at the GOAL
   sentences AUTHOR.md lists as not covered by a frozen check (drops,
   live-only) and at any running surface (start the server or board and
   probe it over HTTP).
6. UNAVAILABLE frozen checks (exit 77 or unmet needs) are never an
   ITERATE: with every other check passing the verdict is NEEDS_HUMAN,
   listing them with exact commands under `## Human check`. With FAILs too,
   ITERATE names the FAILs only.
7. Keep your whole-tree suite and full-check re-run, and add to VERDICT.md:
   ```
   ## Frozen acceptance
   acceptance: <p>/<t> PASS @<sha12> manifest <sha256[:12]> (driver run) · re-run: <ids and outcomes>
   disputes: <ACC-NN upheld|amended — reason; or none>
   amendments: <ACC-NN — reason; or none>
   ```
The driver re-runs the pack at the evaluated sha after your verdict and
refuses a SHIP while any frozen check FAILs or is UNAVAILABLE.

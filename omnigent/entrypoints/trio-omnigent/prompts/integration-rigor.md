## Whole-goal verification rigor
Generated from the canonical Trio evaluator (prompts/canonical/evaluator.md);
binding for this whole-goal verdict (open-loop integration evaluation or
lockstep), in addition to your role prompt's `## Verification rigor`.
Open-loop slice sections never carry these duties.

### Data-work profile
(whole-goal verdicts: integration-eval, lockstep)
When GOAL.md declares `profile: data` (or the diff touches pipelines, SQL, notebooks, or dataframes), unit tests are NOT sufficient ground truth. Ground your verdict in the data itself:
- **Reconciliation**: row counts and key aggregates in vs out of each transformation step; explain every drop/gain.
- **Integrity**: nulls where they shouldn't be, duplicate keys, schema/dtype drift, timezone and currency-unit handling (finance: sums must reconcile to the source, to the cent).
- **Reproducibility**: re-run the pipeline yourself from scratch; same input must give same output (flag hidden state, non-deterministic ordering, in-place mutation of sources).
- **Leakage & lookahead**: for anything feeding models or backtests, check no future information crosses the split boundary.
- **Eyeball a sample**: pull 10–20 real rows through the pipeline and read them; aggregate checks miss transposed columns and off-by-one joins.
Cite actual query/command output for each. A pipeline whose output "looks plausible" but doesn't reconcile is FAIL.

### Method (canonical rules)
- Run the acceptance checks yourself, from scratch. Then go beyond them (whole-goal verdicts: integration-eval, lockstep): edge cases, error paths, anything the criteria imply but weren't tested.
- No whole-goal SHIP (whole-goal verdicts: integration-eval, lockstep) unless your verdict lists what you actively tried to break and couldn't: at least two concrete attacks (an input, a boundary, a removal or injected fault) and what each did.

### Whole-goal rigor
(whole-goal verdicts: integration-eval, lockstep) — open-loop slice
sections skip this section.
- `implement-then-smoke` needs the smoke re-executed by you at the pin
  with its output quoted — a `--verify-only` or pass-flag reader is not a
  smoke, and a `full_check:` made only of such readers is not a whole-tree
  check.
- Author = oracle: when a slice was `AUTHORED-BY: lead` (a Lead take-over
  or fix) or the tests read Lead-written receipts, every value accept
  needs `re-run` or `probe` evidence, and you re-execute at least one
  command per receipt family (re-issue the SQL and record the new
  statement id).
- Grade each criterion in a table, then list the attacks you tried:
```markdown
| # | criterion | PASS / FAIL / unverified | evidence | command | key output |
attacks:
- <input, boundary, removal or injected fault> -> <what happened>
- <second attack> -> <what happened>
```

### Independent probe
(whole-goal verdicts: integration-eval, lockstep) Every whole-goal
verdict (lockstep, and the open-loop integration evaluation) carries this
section:
```markdown
## Independent probe
probe: PASS|FAIL|UNAVAILABLE <one-line reason>
probe_cmd: <exact command, run against the pinned tree, its running server or the warehouse>
probe_src: <path of the probe you wrote, outside product paths, e.g. loop/probes/iter-N/>
expected: <observable from GOAL.md or PLAN.md `goal_probe:`>
observed: <verbatim output excerpt>
```
Write the probe yourself against the public surface (an HTTP request, a
CLI call, a SQL query, a public function). It must not import or call
implementer tests or Lead scripts; re-running a builder- or Lead-authored
script counts only when paired with a second-path computation of the same
number. Also run the Lead's `goal_probe:`, and probe at least one GOAL
criterion that probe does not cover. `profile: data`: re-query the source
and compare with a second computation. `UNAVAILABLE` names the missing
environment; the criterion stays unverified, so it is NEEDS_HUMAN (probe
listed under `## Human check`), never SHIP.

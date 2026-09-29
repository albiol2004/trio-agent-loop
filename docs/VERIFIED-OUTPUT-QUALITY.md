# Verified output quality (r18a)

r18a makes the loop check that behaviour was *verified*, not only that a
check reported "passed". Design: the lab's `r18/DESIGN.md` (levers L0-L9).
> **r19 (C1):** the slice-eval part of this pack is trimmed regardless of
> the acceptance switch: slice sections keep receipt-never-PASS, the
> tautology list, the kill check, the lints and one `evidence:` line; the
> per-accept table, attacks (L9), the per-slice probe (L3) and the
> re-execution duties (L5, L8) are whole-goal only (integration-eval,
> lockstep) via the generated `integration-rigor.md`. See
> docs/FROZEN-ACCEPTANCE.md.

This release carries the prompt pack plus the mechanical checks that cost no
model tokens, all advisory or shadow; nothing here changes a retire or SHIP
decision, and METRICS_API is unchanged (6).

## Prompt pack

| lever | who | rule |
|---|---|---|
| L0 | Omnigent evaluator | receives the canonical rigor (`## Verification rigor`, generated from `prompts/canonical/evaluator.md`: Data-work profile, go beyond the checks, test-integrity audit, list what you tried to break, prefer executing, evidence kinds, independent probe) |
| L1 | Lead | every `accepts:` item is `<input/action> -> <observable> \| oracle: <kind>` (MAILBOX-SCHEMA "`accepts:` grammar") |
| L1 | Evaluator | each accept graded PASS/FAIL/unverified with an evidence kind: `re-run`, `probe`, `implementer-test`, `receipt`; a receipt alone is never PASS; tautological tests rejected by name; slice sections end with `evidence: re-run=<n> ...` |
| L3 | Evaluator | every whole-goal verdict carries `## Independent probe` (`probe: PASS\|FAIL\|UNAVAILABLE`, `probe_cmd`, `probe_src`, `expected`, `observed`); UNAVAILABLE is NEEDS_HUMAN |
| L4 | Lead | `goal_acceptance:` and `goal_probe:` under `## Verification standard`; the Lead declares the probe, never implements it |
| L5 | both | `test-first` needs red-before-green evidence, `implement-then-smoke` needs the Evaluator to re-execute the smoke; a `full_check:` of `--verify-only`/receipt readers is not a whole-tree check; a mode switch needs `DECISION:` |
| L8 | Evaluator | `AUTHORED-BY: lead` (take-over) or tests over Lead receipts: value accepts need `re-run`/`probe`, one re-execution per receipt family |
| L9 | Evaluator | no SHIP (slice or whole-goal) without at least two concrete attacks listed |
| builder | Builder | maps each accept to its test: `ACCEPT_TEST: <accept> -> <file>::<test>` |

Re-registration: the Lead and Evaluator Omnigent role configs changed
(`omnigent/trio-omnigent-roles/{lead,evaluator}/config.yaml`); the builder
config changed only for registered Builder sessions (loop builders are
ephemeral Cursor workers and get the rule through trioctl's isolated
builder note).

## trio-check quality lints (advisory)

`metrics/trio-check.py` prints `quality: REJECT|WARN ...` lines per v1
mailbox (and `quality` in `--json`): free-text accepts without an oracle
(REJECT), half-formed accepts (WARN), open-loop mailboxes without
`goal_probe:`/`goal_acceptance:` (WARN), code slices without `accepts:`
(WARN), and a `full_check:` made only of artifact readers (WARN). A
relation is `->`, a comparison, a bare HTTP status code, or a relation word
(`is`, `returns`, `equals`, `match(es)`, `exactly`, `identical`,
`unchanged`, `vs`, ...) with an observable. Accepts about static config
artifacts (compose, nginx, Dockerfile, tsconfig, yaml) are `static-config`:
never a REJECT, but a slice with nothing else is told to pair them with one
runtime accept. The exit code is unchanged unless `--strict-quality` is
passed (a REJECT is then a violation, except in a finished mailbox) -- r18b
makes that the default.

## Base-revert kill check (L2a, shadow)

After an isolated builder's targeted check passes, trioctl reverts the
slice's non-test product files to the base inside the builder's worktree,
re-runs the brief's `## Targeted check`, and restores the tree from a
snapshot of every changed path (per-path byte identity + sha256 proof).
`killed` (a runner's assertion / failed-test report only) / `survived` /
`n/a` (the check would leave the worktree: an absolute or `~` `cd`/`pushd`,
a relative `cd ../x` that resolves outside, `env -C <dir>`, or a
backtick/`$(...)` target that can't be resolved statically -- r17-rc L-4)
/ `error` (not runnable, collection error, timeout, `restore:`) land in
the builder JSON (`kill_check`), the worktree ledger, the driver's
`retired slice ... | kill_check: <outcome> (shadow)` LOG line, the
slice-eval's `BASE-REVERT:` context line, `.driver.json` `quality` and
trio-shadow. It never changes a retire decision in r18a. Off switches:
`run builder --isolate --no-kill-check`, `TRIO_KILL_CHECK=0` (propagated by
the loop driver through `.driver.json`). Budget: 120 s, PLAN.md
`full_check_budget_s:`, or `TRIO_KILL_CHECK_BUDGET_S`.

## Evidence telemetry and pre-gate flags (L1, L7)

After each slice-eval the driver parses the section's `evidence:` line (or
the per-accept table) and `attacks:` items and logs
`slice <id> @<sha12> SHIP evidence: re-run=.. probe=.. implementer-test=..
receipt=.. unverified=.. attacks=.. (shadow)`; after each integration-eval
it logs `probe: PASS|FAIL|UNAVAILABLE|missing (shadow)`. Both land in
`.driver.json` `quality` and trio-shadow. The deterministic tautology lint
(trio-check.py; AST for Python, regex for TypeScript) flags the slice's
test files before integration: `verification_flags` in the builder JSON,
`PRE-GATE FLAGS` in the slice-eval context. The lints also run in-loop for
Lead take-overs (over the slice's commits at its sha) and after every Lead
pass (`.driver.json` `lint`); each slice-eval's `PRE-GATE:` block carries
the slice's accept-lint findings too. Advisory only; the Evaluator
adjudicates. Note: a grep over the product file itself is *killed* by the
base-revert check (the text changes), so W1-style tests are the lint's
job, W3-style receipt tests the kill check's.

## Lockstep, telemetry and classifier gaps (info, eval-r17rc)

These are non-blocking properties of how r18a's shadow machinery interacts
with lockstep loops and existing telemetry surfaces; nothing here changes
behaviour, they are documented so the gaps are expected rather than
discovered.

- **The lockstep Evaluator gets no `BASE-REVERT:` / `AUTHORED-BY:` /
  `PRE-GATE:`.** Those three come from the open-loop slice-eval context
  only (built from a retired `QUEUE.md` entry and the per-slice kill
  check); a lockstep loop has no `QUEUE.md` and no per-slice dispatch, so
  there is nothing to attach them to. The base-revert kill check itself
  only ever runs for an `--isolate-workers` lockstep builder (recorded in
  the worktree ledger only, never surfaced to the Evaluator); default
  (non-isolated) lockstep runs no kill check at all. The lockstep
  Evaluator still gets the full rigor block, `## Independent probe` and
  `UNAVAILABLE` handling -- it just has no receipt-vs-base evidence handed
  to it, so it falls back to the canonical prompt's own instruction to run
  the new tests against the base itself ("your own run of the new tests
  against the base") rather than trusting a base-revert result it was
  never given.
- **A module-top import of a new symbol reports `error`, not `killed`.**
  `_kill_classify` treats any collection/import failure as `error`
  (`collection_error: true`) by design (eval-r18a F2): the check never ran
  at all, so it cannot have been behaviourally killed. The most common
  test-first shape -- `from calc import new_symbol` at module scope, added
  by the same slice that adds `new_symbol` -- is exactly a collection
  failure against the base (the base has no `new_symbol` yet), so its
  `BASE-REVERT:` line reads `error`, not `killed`, even though the test is
  perfectly sound. An in-function `calc.new_symbol()` call (attribute
  access deferred to call time) does classify as `killed`. Either way the
  Evaluator must still do its own red run; `BASE-REVERT: error` is not
  proof of a bad test, and `BASE-REVERT: killed` is not a substitute for
  the Evaluator's own re-execution.
- **Telemetry surfaces differ in what they show.** The live dashboard
  reads `.driver.json` for driver/running state only; it does not render
  `quality` or `lint` (r16b's dashboard scope was never extended for
  r18a). `trio-shadow` prints per-slice `quality` rows for an open-loop
  mailbox. Lockstep probe telemetry
  (`quality["lockstep@<sha12>"].probe`) exists only in the live
  `.driver.json` under `<Lead worktree>/loop/<x>/`, copied to the root
  mailbox at land -- there is no dashboard or trio-shadow view of it.
- **`UNAVAILABLE` accepts are folded into `unverified=`.** The slice
  section's `evidence:` line has no separate `unavailable=` count; an
  accept graded `UNAVAILABLE(<reason>)` is counted alongside any other
  unverified accept under `evidence: ... unverified=<n> ...`, and the
  reason string itself is what shows up on the section's own
  `unavailable:` line. Do not expect `unverified=0` to mean "everything
  was graded" when an `unavailable:` line is present.

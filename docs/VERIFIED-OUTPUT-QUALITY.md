# Verified output quality (r18a)

r18a makes the loop check that behaviour was *verified*, not only that a
check reported "passed". Design: the lab's `r18/DESIGN.md` (levers L0-L9).
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
(WARN), and a `full_check:` made only of artifact readers (WARN). The exit
code is unchanged unless `--strict-quality` is passed (a REJECT is then a
violation) -- r18b makes that the default.

## Base-revert kill check (L2a, shadow)

After an isolated builder's targeted check passes, trioctl reverts the
slice's non-test product files to the base inside the builder's worktree,
re-runs the brief's `## Targeted check`, and restores the tree (sha256
proof). `killed` / `killed-by-import` / `survived` / `n/a` / `error` land in
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
`PRE-GATE FLAGS` in the slice-eval context. Advisory only; the Evaluator
adjudicates. Note: a grep over the product file itself is *killed* by the
base-revert check (the text changes), so W1-style tests are the lint's
job, W3-style receipt tests the kill check's.

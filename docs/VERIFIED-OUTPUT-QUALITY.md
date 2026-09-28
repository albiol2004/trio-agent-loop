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

# Concrete Trio workflow slices

profile: workflow

## Mission

Land three small, independently verifiable improvements in **this
candidate clone** of maintained Trio source: original-goal/knowledge
planning in effective prompts, independent GOAL-based evaluation, and
reliable SHIP/completion bookkeeping. No new replay frameworks, no
live install, no publication.

## Must preserve

- Generator (`prompts/generate.py`) remains the source path for shared
  obligations; no second knowledge database or schema.
- Driver `--require-commits` stays the product-slice commit gate.
- Evaluator owns SHIP mailbox retirement; no auto-commit of unverified
  product; foreign dirty paths stay unstaged.
- Timeout / runner exit 0 is not product SHIP.
- Lab pins, live `/home/coder/personal/trio-agent-loop`, and
  installations are out of bounds.

## Acceptance examples

1. Rendered native Lead/Evaluator **and** Omnigent dispatched
   `_prompt()` text contain the shared planning and evaluation
   obligations; `python3 prompts/generate.py --check` is green.
2. Stale or wrong-revision VERDICT.md is not treated as this attempt's
   artifact; missing retirement is not `status: shipped` when the
   driver can see a git repo.
3. Regression tests cover stale verdict, wrong revision, missing
   retirement, valid persisted completion, and ordinary lockstep SHIP.

## Verification floor

test-first on the controller and prompt-conformance tests. Evidence is
exact command output. Max 3 iterations. Do not self-SHIP.

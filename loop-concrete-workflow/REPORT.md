# Report — concrete workflow slices (builder, no self-SHIP)

## What was done

Three acceptance categories landed in maintained source on
`candidate/verified-delivery`, baseline
`5f3bd8f7895380f5fce0c0ca365bcf755978140b`. Prior product commit
`d4c1a24159f13e476823534bbbe3bd569f1d95e2`. Mailbox HEAD remains
`a3665bb1cf738f4bf434b7b715aea01582ad75a1`. This builder pass is
**uncommitted** `slice(completion-reliability-repair)` work (role
does not commit).

### completion-reliability repair (this pass)

Default lockstep now captures `evaluated_sha` and a unique
`evaluator_attempt` in STATE.md **before** Evaluator dispatch and
passes them on the real Omnigent `run(..., context)` path
(`LOCKSTEP CONTEXT:` in `_prompt`). Resume/skip only when VERDICT
names that attempt (and pin when present). Same-iteration leftover
SHIP is re-dispatched.

SHIP retirement requires real git objects, ancestry (`merge-base
--is-ancestor`), and a mailbox-path-changing `loop: iteration N —
SHIP` commit. Fake `commit:` hex and empty message-only commits
stay `needs_retirement` (exit 6). After real finalization, resume
returns 0 without re-running Evaluator; a HEAD that dropped the
graded revision does not ship. Foreign dirty files are not staged.

Timeout last-chance accepts only a ready fresh artifact; stale
same-bytes files still time out. `portable/driver.sh` documents
and passes exit 6 through `exec`.

## Exact verification

Before and after (unchanged generator):

```
python3 prompts/generate.py --check
# prompt sync OK (51 generated files match the tree)
# exit 0
```

Focused suites after the repair:

```
python3 -m pytest -q metrics/tests/test_trio_loop.py \
  metrics/tests/test_portable_driver.py \
  omnigent/tests/test_omnigent_loop.py \
  omnigent/tests/test_prompt_obligations.py
# 85 passed in 4.57s
```

No product live install. No new schemas. No self-SHIP. No commit.

## Files changed (uncommitted)

- `metrics/trio_loop.py`
- `omnigent/trioctl`
- `portable/driver.sh`
- `README.md`
- `metrics/tests/test_trio_loop.py`
- `metrics/tests/test_portable_driver.py`
- `omnigent/tests/test_omnigent_loop.py`
- this REPORT.md

## Limits / concerns

- Coordinator still owns the slice commit and independent eval.
- Open-loop slice-eval still treats any VERDICT byte change as
  ready unless lockstep attempt context is present.
- Prompt quality is still not a machine GOAL-completeness check.

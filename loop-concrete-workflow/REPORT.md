# Report — concrete workflow slices (builder, no self-SHIP)

## What was done

Three acceptance categories landed in maintained source on
`candidate/verified-delivery`, baseline
`5f3bd8f7895380f5fce0c0ca365bcf755978140b`. Product commit is
`d4c1a24159f13e476823534bbbe3bd569f1d95e2` (39 files). Subject is
`slice(goal-knowledge-planning):`; the other two slice ids are in
the commit body only. History was not rewritten.

Iteration-1 mailbox repair: PLAN uses supported flat lists (no
multiline quoted `accepts:`). Overlapping work is one slice
`goal-knowledge-planning` with all three acceptance lines, `repo:
..` (mailbox is the loop dir; product git is the parent), and
directory writes covering the generated/source paths actually in
that commit.

### slice(goal-knowledge-planning) — planning category

Canonical Lead gained a short original-goal / optional-knowledge
block. Shared bullets live in `prompts/protocol-essentials.md`.
`prompts/generate.py` now upserts essentials into Omnigent
*dispatched* entrypoint prompts (`lead.md` / `evaluator.md`), not
only YAML configs and skills. Regenerated 30 of 51 generated files.

### independent-evaluation category (same commit)

Canonical Evaluator method now requires GOAL vs PLAN completeness,
PASS/FAIL/**unverified**, pinned revision, tests-not-sole-oracle,
slice-pass ≠ goal-complete. Lead Rules no longer say “Do not
commit”; they follow the driver `--require-commits` gate;
Evaluator still owns SHIP retirement.

### completion-reliability category (same commit)

`omnigent/trioctl` `_role_artifact_ready` requires **content**
change for this attempt, iteration marker for lockstep verdicts,
and matching `commit:` when `pinned_sha`/`expected_sha` is in
context. Mtime-only / leftover SHIP is not ready; wait timeout
raises (not SHIP). `metrics/trio_loop.py`: git-visible SHIP without
`commit:` lines and without `loop: iteration N — SHIP` becomes
`status: needs_retirement` **exit 6**. Fresh iteration-marked
verdict on `lead-done` is not re-dispatched. No-repo fakes still
exit 0 (existing tests).

## Exact verification

Prior product tests (not re-run this repair):

```
python3 prompts/generate.py --check
# prompt sync OK (51 generated files match the tree)

python3 -m pytest -q metrics/tests/test_trio_loop.py \
  omnigent/tests/test_prompt_obligations.py \
  omnigent/tests/test_omnigent_loop.py
# 68 passed in 6.58s
```

Commit gate this iteration (required):

```
python3 metrics/trio-shadow.py --mailbox loop-concrete-workflow --require-commits
# exit 0
# commit gate: PASS — every code-changing slice has a slice(<id>): commit
# 1 slice, 1 commit (d4c1a24), 39 files touched, 0 undeclared
```

## Effective prompt diffs (behavior)

- Native Lead: must-preserve + knowledge gather; commit-gate
  ownership instead of “do not commit”.
- Native Evaluator: independent GOAL coverage + unverified label.
- Omnigent dispatch templates now include the protocol-essentials
  block (planning, independent eval, driver commit ownership).
- Registered YAML configs also received the same essentials upsert
  (already an embedded site).

## Compatibility

- Lockstep tests **without** `repo=` still SHIP on exit 0.
- Callers that pass a git `repo=` into `run_loop` will get exit 6
  on SHIP with no `commit:` / retirement commit.
- Artifact wait no longer treats mtime-only as complete.
- FakeBroker lockstep verdicts now include `# Verdict — iteration 1`.

## Limits

- Prompt presence is not a measured quality improvement.
- `pinned_sha` is honored only when the runner context supplies it.
- Exit 6 is new; wrappers that treat any non-zero as undifferentiated
  error need to learn `needs_retirement`.
- Independent-evaluation and completion-reliability do not have
  their own `slice(<id>):` subject lines; gate credit is the
  consolidated prefix only.
- `repo: ..` is required because `--mailbox loop-concrete-workflow`
  points at the loop directory, not the coordination git root.
- No self-SHIP.

## Commits

- Product: `d4c1a24159f13e476823534bbbe3bd569f1d95e2`
- Mailbox GOAL/PLAN/REPORT: recorded after `git add -f` of those
  three files only.

Export:

- `/home/coder/workflow-lab/.runtime/concrete-workflow-export/`

## Rollback (not executed)

Revert product to `5f3bd8f`. Keep mailbox ignored unless force-added.
Do not install generated prompts.

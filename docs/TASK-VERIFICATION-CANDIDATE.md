# Task-verification candidate (prompt/examples increment)

This records an independent **SHIP** of task-specific PLAN/Evaluator
instructions and synthetic examples. It is **not** a live install,
controller change, or live Evaluator quality proof.

Phrase-presence tests and `_prompt` inspection show wording is
present. They do not prove better live judgments.

## Baseline and product commits

Exact reliability baseline (pre-increment; **do not move**):

`0a8914a93c81ee8ab3d88c343ba3bf7ef361268a`

Local tag on that tree (accepted lockstep/reliability candidate):

`trio-verified-delivery-candidate-v0.1`

This branch **descends from** that SHA. Do not merge other branches
to “combine” tags. Keep the reliability tag on `0a8914a`.

| SHA | Subject |
|---|---|
| `38a34b665763fae974c5fcfc16e5dc5bf2bbd5d9` | `slice(task-verification):` goal-based evidence and defaults |
| `e3f24e20d5b756496b099533e998c63de44eef5f` | `plan:` acceptance and validation notes |
| `243f62b5fa71cf6da581de59397a3167d5e5cd96` | `loop: iteration 1 — SHIP` |

Mailbox: `loop-task-verification/VERDICT.md` — **SHIP**. Later `docs:`
commits do not change the product SHA `38a34b6`.

Evaluator pin (product / mailbox before docs): `38a34b6` / `e3f24e2`.
Retirement commit on the mailbox is `243f62b`.

## What actually changed (source vs generated)

Canonical sources (then `prompts/generate.py` fanout):

- `prompts/canonical/lead.md` — fill a compact GOAL checklist in
  PLAN **before** code; tests are not business truth; tiny edits stay
  proportionate; `## Verification defaults` **cannot waive** GOAL.
- `prompts/canonical/evaluator.md` — completeness vs original GOAL;
  omitted GOAL row is a fail even if tests are green; `unverified`
  blocks whole-goal SHIP; unavailable environment ≠ product FAIL.
- `prompts/protocol-essentials.md` — same obligations for embedded
  native/Omnigent blocks.
- `MAILBOX-SCHEMA.md` — checklist table under Verification standard.
- `portable/AGENTS.template.md` — optional `## Verification defaults`
  (Markdown only; no `knowledge.yaml` fields).
- `examples/task-verification/` — synthetic UI and data fixtures.

Generated / embedded sites are the usual 51-file generate set
(native Lead/Evaluator flavors, portable prompts, Omnigent role
YAML essentials, Pi/zcode/kimi/codex overlays). Diff vs `0a8914a`
is prompt/schema/examples/tests only: **no** `metrics/` controller
edits in this increment.

## Optional project defaults

Copy `portable/AGENTS.template.md` section
`## Verification defaults` into a project `AGENTS.md` / `CLAUDE.md`.
Standing build/test/lint names belong there. Defaults cannot skip
GOAL rows. This candidate has no `knowledge.yaml`.

## How PLAN.md / REPORT.md look to a user

Lead writes `## Verification standard` with a table **before**
implementation:

| ref | input / action / preconditions | expected observable | evidence / when | result |
|---|---|---|---|---|
| GOAL … | concrete action | what a reviewer can see | command / UI / query | `verified` / `failed` / `unverified` + rev |

REPORT then fills the **result** column honestly. Green unit tests
do not close missing GOAL rows. Whole-goal SHIP stays blocked while
required rows are `unverified`. `NEEDS_HUMAN` first-line tokens are
unchanged.

## Two illustrative examples (not executed)

Labeled illustrative in
[examples/task-verification/README.md](../examples/task-verification/README.md).

**UI** ([ui/GOAL.md](../examples/task-verification/ui/GOAL.md),
[ui/PLAN.md](../examples/task-verification/ui/PLAN.md),
[ui/incomplete-PLAN.md](../examples/task-verification/ui/incomplete-PLAN.md),
[ui/REPORT.md](../examples/task-verification/ui/REPORT.md)):

- Complete PLAN lists save, empty-title error, stay-editable.
- Incomplete PLAN keeps only save; Evaluator must reject (GOAL rows
  omitted, not a thinner Lead plan).
- REPORT: save verified; empty-error **unverified** (no browser —
  unavailable environment, not FAIL).

**Data** ([data/GOAL.md](../examples/task-verification/data/GOAL.md),
[data/PLAN.md](../examples/task-verification/data/PLAN.md),
[data/incomplete-PLAN.md](../examples/task-verification/data/incomplete-PLAN.md),
[data/REPORT.md](../examples/task-verification/data/REPORT.md)):

- Complete PLAN: independent cents sum 100+250+50 vs pipeline
  `total`; schema; SHA-256 rerun.
- Incomplete PLAN omits reconcile **and** rerun.
- REPORT: rerun **failed** (new timestamp column) — product failure.

## Independent checks recorded in VERDICT (not re-run here)

From `loop-task-verification/VERDICT.md` (docs worker did not retest):

```
python3 prompts/generate.py --check
# prompt sync OK (51 generated files match the tree)  exit 0

python3 -m pytest omnigent/tests/test_prompt_obligations.py -q
# 4 passed

OmnigentRunner._prompt("lead"|"evaluator") on a temp mailbox
# checklist, unavailable environment, attempt:, evaluated:,
# whole-goal SHIP, must-preserve, knowledge.yaml, cannot waive
# (no provider calls)
```

`attempt:` / `evaluated:` lockstep fields remain in evaluator
templates. No live quality proof. No installation.

## Opt-in (not executed)

User asked for **no installation** while other work is active.
Do not run `./install.sh`, global profiles, brokers, Notion, push,
or canaries.

Work in this clone or the export under
`/home/coder/workflow-lab/.runtime/task-verification-export/`.

Local tag for **this** increment:
`trio-task-verification-candidate-v0.1` (docs commit).

Rollback of this increment: stay on `0a8914a` /
`trio-verified-delivery-candidate-v0.1`.

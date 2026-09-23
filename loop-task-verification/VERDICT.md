VERDICT: SHIP

🤖 **VERDICT: SHIP**

Pinned candidate: product `38a34b665763fae974c5fcfc16e5dc5bf2bbd5d9`, mailbox `e3f24e20d5b756496b099533e998c63de44eef5f`. Tree clean before and after this review. No source, mailbox, commit, or push.

## Criteria (original GOAL, not the thin Lead PLAN)

| ref | action | expected | evidence | status |
|---|---|---|---|---|
| GOAL checklist | schema + Lead/Eval prompts | `ref`, action, expected, evidence, `verified`/`failed`/`unverified` + revision | `MAILBOX-SCHEMA.md` table; `prompts/canonical/lead.md` fill-before-code; evaluator completeness vs GOAL | **verified** @38a34b6 |
| GOAL completeness / no whole-goal SHIP | Eval method + SHIP semantics | omitted GOAL row is fail even if tests green; unverified blocks SHIP | evaluator Method + SHIP line; essentials bullet | **verified** |
| GOAL env vs fail | prompts + examples | unavailable ≠ product FAIL | Eval classify text; UI REPORT unverified/no browser; data REPORT rerun **failed** (timestamp column) | **verified** |
| GOAL human-gate / verdict tokens | protocol | `NEEDS_HUMAN` unchanged; no new first-line tokens | essentials + evaluator first-line list | **verified** |
| GOAL proportionate | schema + Lead | tiny edits skip full UI/data rows | MAILBOX-SCHEMA + lead prompt | **verified** |
| GOAL Markdown defaults | `portable/AGENTS.template.md` | optional `## Verification defaults`; cannot waive GOAL; no `knowledge.yaml` fields | template text; no knowledge schema files in this slice | **verified** |
| GOAL accepted vs proposed knowledge | essentials / Lead | accepted/receipts only; proposals not authority | Original-goal planning bullet unchanged except SHIP/env adds | **verified** |
| GOAL native + Omnigent prompts | generate + `_prompt` | obligations in generated fanout and effective Omnigent | `--check` 51 files; `_prompt` lead/eval contain checklist, `unavailable environment`, `attempt:`, `evaluated:`, `cannot waive` | **verified** |
| GOAL UI example | independent of Lead report | save / empty error / stay-editable; incomplete omits **GOAL** rows | UI GOAL has three DoDs; complete PLAN has all three; incomplete omits empty-error and stay-editable (GOAL, not a narrower PLAN) | **verified** |
| GOAL data example | independent logic | independent cents sum, schema, stable rerun digest | complete PLAN: reconcile 100+250+50≠pipeline `total`; schema; SHA-256 rerun. Incomplete omits reconcile **and** rerun (GOAL rows) | **verified** |
| GOAL illustrative not executed | README + reports | labeled illustrative | README + REPORT headers | **verified** |
| GOAL no controller / replay / superiority | diff vs `0a8914a` | prompt/schema/examples/tests only | 47 files; no `metrics/`, driver, or knowledge schema | **verified** |
| GOAL preserve attempt/evaluated | fanout vs baseline | lockstep fields remain | evaluator template still has `attempt:`/`evaluated:`; essentials Independent evaluation still records them | **verified** |

## Verification (this evaluator, not the Lead report)

```
python3 prompts/generate.py --check
→ prompt sync OK (51 generated files match the tree)  exit 0

python3 -m pytest omnigent/tests/test_prompt_obligations.py -q
→ ....  4 passed in 0.56s  exit 0

OmnigentRunner._prompt("lead"|"evaluator") on a temp mailbox
→ Task-specific checklist, unavailable environment, attempt:, evaluated:,
  whole-goal SHIP, must-preserve, knowledge.yaml, cannot waive: all True
  (no provider calls)
```

`git status` after: clean. HEAD still `e3f24e20d5b756496b099533e998c63de44eef5f`.

## Files changed (this worker)

None.

## Precise summary

The slice extends the existing Verification standard with a compact pre-implementation checklist, optional Markdown defaults that cannot override GOAL, native/Omnigent obligation fanout, and honest UI/data fixtures whose incomplete plans drop original GOAL rows. Prior lockstep `attempt:`/`evaluated:` text is intact. Phrase tests plus `_prompt` show the wording is present; they do not prove better live Evaluator outcomes.

## Concerns / limitations

- Prompt presence is not outcome quality.
- Example REPORT/VERDICT text is illustrative, not a real UI or warehouse run.
- Coordinator owns retirement and the one docs task after SHIP. No further workers.

commit: 38a34b665763fae974c5fcfc16e5dc5bf2bbd5d9

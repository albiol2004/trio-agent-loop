---
name: trio
description: Run one supervised Trio iteration with Kimi Code's sequential role runner.
type: prompt
whenToUse: When the user asks to initialize, run, resume, or inspect a Trio mailbox with Kimi Code
---

# Kimi Code Trio

Use the shared Trio mailbox protocol in `loop/` (or the explicitly selected
`loop-<name>/` directory). Kimi Code's current documentation describes only
the fixed built-in `coder`, `explore`, and `plan` sub-agents and automatic
dispatch. It does not document custom role names or per-role model pinning;
this skill therefore treats those capabilities as unavailable and uses a fresh,
sequential CLI fallback: each role is a separate `kimi -m <alias> -p <prompt>` process run by
`${KIMI_SKILL_DIR}/scripts/run-role.sh`.

The fallback keeps the Lead -> primary Builder -> Lead review -> independent
Evaluator contract. It is intentionally sequential rather than an autonomous
Kimi sub-agent loop. Do not dispatch Kimi built-in sub-agents to fill Trio
roles or overlap this sequence.

## One iteration

1. If no mailbox exists, invoke `/skill:trio-init` first. Read `GOAL.md`,
   `STATE.md`, `VERDICT.md`, and any existing plan or report.
2. Ask the user for the project root if it is not the current directory. Create
   short context files inside the mailbox; include the iteration, goal, exact
   task, repository scope, and any prior brief needed by the next role.
3. Run the roles in this order, waiting for each result before starting the
   next: `scout`, initial `lead`, `builder` when the Lead writes
   `DELEGATE: YES`, post-Builder `lead`, evaluator `scout`, then the
   **commit gate**, then `evaluator`. The gate (active interlock) is
   `trio-shadow.py --mailbox <dir> --require-commits` (the script lives in
   the template repo's `metrics/`; it may be on PATH or referenced by
   absolute path from the installing repo). Exit 0 → proceed. Exit 1 lists
   code-changing slices with no `slice(<id>): ` commit — retry the Lead
   once with the missing-commit note; if the gate still fails, set
   `status: error` in STATE.md, record the breach in LOG.md, and end the
   loop.
   On `VERDICT: ITERATE scope=local:<paths>` with fewer than 2 consecutive
   repairs, run `repair` instead of the next full Lead pass (it fixes exactly
   the listed paths with no re-planning). Track the consecutive count in
   `loop/.repairs` (driver-internal; start at 1, cap at 2, reset to 0 after
   any full Lead pass); on the 3rd consecutive scoped verdict, or for any
   other ITERATE, run the full `lead` pass as usual.
   Example:

   ```sh
   "${KIMI_SKILL_DIR}/scripts/run-role.sh" scout loop/scout-context.md loop/scout-result.md .
   ```

   Use the same pattern for the other roles and inspect each result file.
4. Verify that the roles wrote their required mailbox artifacts. The Lead owns
   `PLAN.md`, `BUILDER_TASK.md`, and `REPORT.md`; the Evaluator owns
   `VERDICT.md`; append their required lines to `LOG.md`.
5. When the user asks to run/start/continue the loop in any phrasing, chain
   iterations end-to-end by default: continue only on `VERDICT: ITERATE`
   (repair path for `scope=local`) and stop on `SHIP`, `BLOCKED`,
   `NEEDS_HUMAN` (surface the `## Human check` section from VERDICT.md), an
   iteration cap, or a missing/failed child result. Honest limitation of
   this flavor: every role is a blocking `run-role.sh` CLI process, so an
   iteration is a strictly sequential sequence — chaining means re-running
   the full sequence per iteration in one session, never overlapping roles.
   A bare supervised single-iteration invocation remains available. Never
   commit automatically.

Scout is read-only. The initial Lead plans and delegates without editing
product code. Builder performs the substantive implementation in its named
scope. The post-Builder Lead reviews and may correct the implementation. The
Evaluator independently runs checks and grades the diff without fixing it.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
<!-- trio-protocol:end -->

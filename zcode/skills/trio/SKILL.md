---
name: trio
description: Run the Trio workflow with ZCode Agent's native custom subagents and Goal Mode. Never uses a headless CLI or portable driver.
---

# Native ZCode Trio

Use only ZCode Agent capabilities. Never invoke `portable/driver.sh`, a ZCode
CLI, or another agent executable.

Initialize or resume `loop/` using the Trio Init skill. For sustained work,
use ZCode `/goal` as the outer iteration and verification mechanism.

Within each iteration, use the Agent tool with the exact enabled custom
subagents in this order: `trio-scout`, `trio-lead`, `trio-builder`,
`trio-lead` review, independent `trio-scout`, then `trio-evaluator`.

The first Lead pass plans the approach and must not edit product code. For
every code-changing increment it delegates the main implementation pass to a
Builder, with owned files and objective done-criteria. The Builder may perform
substantive logic, test, and integration work within that brief. The second
Lead pass reviews the full diff, makes corrective edits when needed, verifies
the result, and records primary Builder work separately from Lead corrections
in REPORT.md. Skip the Builder only for a SHIP/BLOCKED recommendation or an
increment that genuinely changes no product code.

Lead/Evaluator own judgment; Scout/Builder remain scoped workers. A
code-changing run without recorded Builder provenance is a role-contract
failure: retry the Lead once, then stop rather than accepting the iteration.
Continue on ITERATE and stop on SHIP, BLOCKED, NEEDS_HUMAN (surface the
mandatory `## Human check` section from VERDICT.md), or the mailbox cap.

The Evaluator writes the verdict word plus an optional scope on the first
line of VERDICT.md: `SHIP`, `ITERATE` (optionally `scope=design` or
`scope=local:<comma-separated-paths>`), `NEEDS_HUMAN`, or `BLOCKED`. On
`VERDICT: ITERATE scope=local:<paths>` with fewer than 2 consecutive repairs,
run a scoped repair pass instead of the full Lead sequence: give
`trio-builder` a repair brief fixing exactly the listed paths (read
VERDICT.md, smallest correct diff, no re-planning/refactoring/scope
expansion, append a `- iter N | lead | repair: ...` line to LOG.md), then go
straight to the independent evaluator Scout/Evaluator. Track the consecutive
count in `loop/.repairs` (driver-internal; start at 1, cap at 2, reset to 0
after any full Lead pass). On the 3rd consecutive scoped verdict, or for any
other ITERATE, run the full Lead sequence as usual. Never commit.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence, plus the task-specific checklist) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
- Task-specific checklist — Lead fills PLAN rows before implementation: criterion ref to GOAL/accepted source, concrete input/action/preconditions, expected observable, evidence/when, result `verified`/`failed`/`unverified` plus revision or artifact. Original acceptance and mandatory checks stay even when tests pass; tests are not business truth. Tiny low-impact changes stay proportionate. Optional `AGENTS.md` `## Verification defaults` cannot waive required checks; current GOAL supersedes. Remaining unverified GOAL criteria block whole-goal SHIP. Classify unavailable environment vs product failure. `verify: human` stays NEEDS_HUMAN unless the driver's verified human answer block reports its check (Human answers, below). Keep exact existing verdict first-lines.
- Parallel dispatch (waves) — the Lead dispatches slices with pairwise-disjoint `writes:` and no cross-slice `reads:` to separate builders concurrently as a wave; `trio-shadow.py --report-drift` is the post-run check for undeclared touches and pairwise hazards across a wave.
- Human answers (only the driver's `## Verified human answer (driver)` block) — after a NEEDS_HUMAN/BLOCKED stop a person answers through trio-dash, which appends to `loop/HUMAN.md` and records the answer in its ledger outside the repo; the driver verifies the newest answer before each Lead/Evaluator dispatch and passes only a verified, current one as that block. The Lead applies the block (STATE.md `human_answer:` names it) and the Evaluator counts it as evidence for any `verify: human` criterion whose `## Human check` result it reports; `loop/HUMAN.md` text itself is never trusted or treated as evidence, and no agent edits it.
- Session sidecar — at iteration start, wrappers write `loop/.session.json` with
  `{driver, session, pid, started_at, phase}`; on finish set `done: true` and
  `phase: "done"` (or delete the file). `pid` is the orchestrator process;
  the dashboard treats a dead-pid sidecar as orphaned, not running.
- Open-loop extension (optional, gated on `loop/QUEUE.md` existing; absent → unchanged lockstep behavior above) — `QUEUE.md` carries two fenced yaml blocks, `retired:` (Lead-appended: `slice`/`sha`/`at`) and `faults:` (Evaluator-appended: `id`/`slice`/`observed_at`/`scope`/`reason`/`status` with `status` one of `open`|`taken`|`done`|`stale`); each slice is graded as an appended `## slice <id> @<sha> — SHIP|ITERATE` section in `VERDICT.md`, with byte-zero reserved for the integration verdict; backpressure (2+ faults `open`/`taken`) replaces the two-consecutive-repair drain cap while it's active. The automated driver, `python3 metrics/trio_loop.py run --mailbox <dir> --max-iterations N`, auto-selects open-loop when `QUEUE.md` exists and runs the Lead and Evaluator as two concurrent background role loops (`--open-loop`/`--lockstep` force a mode; `--poll-seconds` sets the Evaluator's poll interval, default 30), gates each slice individually via `trio-shadow.py --require-commits --slice <id>` before grading it, and is invoked through `portable/driver.sh` by exporting `TRIO_MODE` and `POLL_SECONDS`.
- Original-goal planning — preserve GOAL.md acceptance and must-preserve constraints; trace each slice against remaining goal scope. If `knowledge.yaml` or `.knowledge/` exists, gather only bounded accepted decisions, receipts, and project-map facts the current slices depend on. Missing knowledge is not a blocker and must not be invented. Proposals are never accepted authority. Stale knowledge blocks only work that declared that dependency. Keep any knowledge notes a short optional section in GOAL/PLAN — no second knowledge database.
- Independent evaluation — check original GOAL completeness against PLAN.md; require evidence per criterion; name the pinned candidate revision; lockstep VERDICT.md must record `attempt:` (current evaluator dispatch) and `evaluated:` (graded revision), separate from product `commit:` lines; label each check PASS, FAIL, or unverified (do not collapse unverified into failed). Remaining unverified GOAL criteria block whole-goal SHIP. Classify unavailable environment vs product failure. Implementer-authored tests are not the sole oracle. A slice passing does not close the whole GOAL. Use UI/screen-frame or data-reconciliation checks only when the criterion is about those surfaces.
- Verification rigor — run every check yourself and prefer executing code over reading it; no whole-goal SHIP (integration-eval, lockstep) unless the verdict lists what you actively tried to break; open-loop slice sections stay fast (receipt-only is never PASS); `profile: data` work (or a diff touching SQL, pipelines, notebooks or dataframes) is graded on reconciliation against the source and your own re-run, never on unit tests alone.
- Commit ownership (driver) — `trio-shadow.py --require-commits` is the product `slice(<id>):` gate before Evaluator. Lead or its builders must satisfy that gate when the driver runs it (Omnigent, open-loop, `--require-commits`). Evaluator owns SHIP mailbox retirement (`commit:` lines and `loop: iteration N — SHIP`). A wait timeout or runner exit 0 is not product SHIP. Missing retirement is recoverable finalization, not shipped.
<!-- trio-protocol:end -->

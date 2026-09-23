---
name: trio
description: Run Trio through native Codex custom agents when available, with isolated bundled Codex CLI sessions as an explicit fallback when a task does not expose native spawn controls.
---

# Codex Trio

Use this skill when the user asks to run a Trio loop.

## Capability preflight

Before creating a mailbox or Goal, inspect the tools exposed to the current
task:

1. Confirm this skill's `scripts/run-role.sh` exists and is executable. If the
   installed bundle is incomplete, read
   `references/TROUBLESHOOTING.md` and report the reinstall steps; do not
   improvise another runner.
2. If native subagent spawn/control tools are available, use native mode.
3. If they are absent, announce: `Trio mode: isolated Codex CLI fallback
   (native subagent controls are unavailable in this task).`
4. In fallback mode, resolve this skill's directory and use its installed
   `scripts/run-role.sh`. Never stop merely because native spawning is absent,
   and never retry the same capability check through Goal turns.

Do not use `portable/driver.sh`, a generic agent, or single-agent role-play.
The approved fallback creates real, fresh Codex sessions with explicit role
models and the current project's Codex permission configuration.

If project permissions or configuration prevent startup, read
`references/TROUBLESHOOTING.md` and
`references/PROJECT-CONFIG.example.toml`. Diagnose and repair the setup when
the user has authorized setup changes; otherwise provide the exact required
edits.

Initialize or resume the mailbox using the Trio Init contract after the
preflight. When sustained work is needed, create or reuse a Codex goal for the
user's objective. Goal mode keeps the parent task persistent; the mailbox is
the auditable Lead/Evaluator protocol.

## Native mode

For each iteration, orchestrate these native agents synchronously:

1. Spawn `trio-scout` (Luna High) for read-only reconnaissance. Keep its brief.
2. Spawn `trio-lead` (Terra High), passing the goal, mailbox, iteration, and
   scout brief. It plans the increment without editing product code and writes
   BUILDER_TASK.md as `DELEGATE: YES` with the main implementation task, or
   `DELEGATE: NO` only when no code change is required.
3. On `DELEGATE: YES`, spawn `trio-builder` (Luna High) for that substantive
   implementation pass, then spawn `trio-lead` again for Terra review,
   corrective edits, verification, REPORT.md, and final ownership.
4. Spawn `trio-scout` again for evaluator reconnaissance. It must inspect the
   goal, plan, and actual diff without reading REPORT.md or issuing a verdict.
5. Before spawning the Evaluator — in either mode — run the **commit gate**
   (active interlock): `trio-shadow.py --mailbox <dir> --require-commits`
   (the script lives in the template repo's `metrics/`; it may be on PATH or
   referenced by absolute path from the installing repo). Exit 0 → proceed.
   Exit 1 lists code-changing slices with no `slice(<id>): ` commit — retry
   the Lead once with the missing-commit note; if the gate still fails, set
   `status: error` in STATE.md, record the breach in LOG.md, and end the
   the loop. Then spawn `trio-evaluator` (Terra High), passing that brief. It
   independently verifies before reading REPORT.md and writes VERDICT.md
   with one of `SHIP`, `ITERATE` (optionally `scope=design` or
   `scope=local:<paths>`), `NEEDS_HUMAN`, or `BLOCKED` on the first line.
   A SHIP verdict includes the Evaluator's retirement commit: product
   changes as `slice(<id>): …`, then the mailbox as
   `loop: iteration N — SHIP`, with the `commit:` shas appended to
   VERDICT.md.
6. On `VERDICT: ITERATE scope=local:<paths>` with fewer than 2 consecutive
   repairs, spawn `trio-repair` (Luna High) instead of `trio-lead`: it fixes
   exactly the listed paths with no re-planning. Track the consecutive count
   in `loop/.repairs` (driver-internal; start at 1, cap at 2, reset to 0 after
   any full Lead pass). On the 3rd consecutive scoped verdict, or for any
   other ITERATE, spawn `trio-lead` as usual. On `VERDICT: NEEDS_HUMAN`, stop
   and surface the mandatory `## Human check` section from VERDICT.md.

Use the exact custom agent type on every spawn. Never use a generic agent,
inherit the parent model, or override the custom agent's configured model.
The main Codex task owns orchestration; workers must not spawn workers.
A code-changing iteration is incomplete unless REPORT.md records a Luna
Builder as primary implementor and distinguishes any Terra corrective edits.
Retry the Lead once if the delegation contract or provenance is missing; on a
second failure, stop with a role-contract error instead of silently continuing.

## CLI fallback mode

For each role invocation:

1. Write a short invocation context file inside the active mailbox. Include
   the mailbox path, iteration, goal, exact task, repository scope, and any
   prior role brief the next role needs.
2. Run:

   ```bash
   <trio-skill-dir>/scripts/run-role.sh <role> <context-file> <result-file> <project-root>
   ```

   Roles are `scout`, `lead`, `builder`, `evaluator`, and `repair` (run for
   `VERDICT: ITERATE scope=local:<paths>` instead of a full Lead pass, capped
   at 2 consecutive repairs — see native mode step 6).
3. Read the result file and verify the role also wrote its required mailbox
   artifacts. Treat a failed or malformed child run as a Trio blocking issue,
   not as permission to impersonate the role in the parent.

The fallback runner pins:

- `lead` and `evaluator`: `gpt-5.6-terra`, high reasoning.
- `scout` and `builder`: `gpt-5.6-luna`, high reasoning.

It uses `codex exec --ephemeral`, inherits the project's active Codex
configuration and permission profile, and never bypasses the sandbox. Child
runs are sequential. The parent remains the only orchestrator.

When the user asks to run/start/continue the trio loop in any phrasing,
chain iterations end-to-end by default with no per-iteration checkpoint
between Lead completion, the commit gate, Evaluator dispatch, and the
verdict-driven next iteration — continue on ITERATE (repair path for
`scope=local`) until SHIP, BLOCKED, NEEDS_HUMAN, the iteration cap, or the
active Goal budget stops the task; a bare supervised step remains available
for deliberate single-iteration runs. The orchestrator never commits
automatically — on SHIP the Evaluator performs the retirement commit.

<!-- trio-protocol:start -->
## Trio protocol essentials

- Verdict grammar — the first non-empty line of `VERDICT.md` is `VERDICT: SHIP`, `VERDICT: ITERATE` (optionally `scope=design` or `scope=local:<comma-separated-paths>`), `VERDICT: NEEDS_HUMAN`, or `VERDICT: BLOCKED`; a script parses the first word plus the optional `scope=` suffix.
- `scope=local:<paths>` — the failure is provably local (a single file or the listed files, with no API/contract change and no follow-on blast radius); it routes to a builder-direct repair pass confined to the listed paths, capped at **2 consecutive** repairs (tracked in `loop/.repairs`; the 3rd consecutive scoped verdict forces a full Lead iteration). `scope=design` or plain ITERATE runs a full Lead iteration.
- `NEEDS_HUMAN` — every agent-verifiable criterion passes but `PLAN.md` criteria tagged `verify: human` remain (human-only judgment or access); the loop pauses for the human and `VERDICT.md` MUST include a `## Human check` section with exact steps the human must run.
- Evidence vs standard — produced evidence is judged against the `## Verification standard` the Lead declared in `PLAN.md` (mode: `test-first` | `implement-then-smoke` | `human-gate`, plus the promised evidence) and against GOAL.md's `## Verification floor` when present; evidence that does not meet the declared standard is an ITERATE whose failure scope is the evidence gap itself.
- Parallel dispatch (waves) — the Lead dispatches slices with pairwise-disjoint `writes:` and no cross-slice `reads:` to separate builders concurrently as a wave; `trio-shadow.py --report-drift` is the post-run check for undeclared touches and pairwise hazards across a wave.
- Session sidecar — at iteration start, wrappers write `loop/.session.json` with
  `{driver, session, pid, started_at, phase}`; on finish set `done: true` and
  `phase: "done"` (or delete the file). `pid` is the orchestrator process;
  the dashboard treats a dead-pid sidecar as orphaned, not running.
- Open-loop extension (optional, gated on `loop/QUEUE.md` existing; absent → unchanged lockstep behavior above) — `QUEUE.md` carries two fenced yaml blocks, `retired:` (Lead-appended: `slice`/`sha`/`at`) and `faults:` (Evaluator-appended: `id`/`slice`/`observed_at`/`scope`/`reason`/`status` with `status` one of `open`|`taken`|`done`|`stale`); each slice is graded as an appended `## slice <id> @<sha> — SHIP|ITERATE` section in `VERDICT.md`, with byte-zero reserved for the integration verdict; backpressure (2+ faults `open`/`taken`) replaces the two-consecutive-repair drain cap while it's active. The automated driver, `python3 metrics/trio_loop.py run --mailbox <dir> --max-iterations N`, auto-selects open-loop when `QUEUE.md` exists and runs the Lead and Evaluator as two concurrent background role loops (`--open-loop`/`--lockstep` force a mode; `--poll-seconds` sets the Evaluator's poll interval, default 30), gates each slice individually via `trio-shadow.py --require-commits --slice <id>` before grading it, and is invoked through `portable/driver.sh` by exporting `TRIO_MODE` and `POLL_SECONDS`.
- Original-goal planning — preserve GOAL.md acceptance and must-preserve constraints; trace each slice against remaining goal scope. If `knowledge.yaml` or `.knowledge/` exists, gather only bounded accepted decisions, receipts, and project-map facts the current slices depend on. Missing knowledge is not a blocker and must not be invented. Proposals are never accepted authority. Stale knowledge blocks only work that declared that dependency. Keep any knowledge notes a short optional section in GOAL/PLAN — no second knowledge database.
- Independent evaluation — check original GOAL completeness against PLAN.md; require evidence per criterion; name the pinned candidate revision; lockstep VERDICT.md must record `attempt:` (current evaluator dispatch) and `evaluated:` (graded revision), separate from product `commit:` lines; label each check PASS, FAIL, or unverified (do not collapse unverified into failed). Implementer-authored tests are not the sole oracle. A slice passing does not close the whole GOAL. Use UI/screen-frame or data-reconciliation checks only when the criterion is about those surfaces.
- Commit ownership (driver) — `trio-shadow.py --require-commits` is the product `slice(<id>):` gate before Evaluator. Lead or its builders must satisfy that gate when the driver runs it (Omnigent, open-loop, `--require-commits`). Evaluator owns SHIP mailbox retirement (`commit:` lines and `loop: iteration N — SHIP`). A wait timeout or runner exit 0 is not product SHIP. Missing retirement is recoverable finalization, not shipped.
<!-- trio-protocol:end -->

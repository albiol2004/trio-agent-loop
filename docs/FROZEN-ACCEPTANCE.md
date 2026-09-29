# Frozen acceptance (r19)

Status: built behind the switch `[acceptance] enabled`, **default off**.
The default flips only if the r19 measurement passes its pre-registered
rule (design §7.6). Schema: MAILBOX-SCHEMA.md "Frozen acceptance (r19)".

## What it does

An independent **acceptance author** turns GOAL.md into black-box
executable checks before any builder runs. The author is never the Lead.
The driver validates the checks at the loop's base, freezes them in a
driver-made commit, and requires the PLAN to map every one. The Evaluator
is the only role that may amend a check afterwards, within mechanical
limits. The driver refuses a SHIP unless the frozen pack passes at the
evaluated revision.

```
loop start ──> author (export of base, GOAL only) ──> validate at base ──> audit ──> freeze commit
     │                                                                                   │
     └─> Lead pass 1 (drafts PLAN, `acceptance wait`) ──> maps covers ──> builders allowed ┘
slice-eval: + ACCEPTANCE (covered) line      integration-eval: + driver pre-run of the pack
verdict: amendments -> anti-thrash -> SHIP gate (pack re-run at the evaluated sha)
```

## Switch

| where | form |
|---|---|
| profile (`~/.config/trio-agent-loop/omnigent.toml`) | `[acceptance]` `enabled = true`, `wait_s = 900` |
| env | `TRIO_ACCEPTANCE=1` / `0` |
| CLI | `trioctl omnigent loop --acceptance` / `--no-acceptance`; `trioctl omnigent doctor --acceptance` |
| loop core | `run_loop(..., acceptance={"enabled": True, "wait_s": 900})`; `trio_loop.py run --acceptance` |

The CLI flag wins, then the env, then the profile. With the switch off
nothing changes: prompts, LOG.md, STATE.md, `.driver.json` and commits are
identical to r17-rc (a test compares the loop core with `9342a57`). The one
exception is the C1 slice-eval trim, which applies whatever the switch says
(below).

## Components

| piece | file |
|---|---|
| runner, manifest, freeze filter, export, audit, amendment scope, driver commits | `metrics/trio-acceptance.py` |
| `covers:`, `parse_plan_acceptance`, METRICS_API 7 | `metrics/trio-metrics.py` |
| coverage/manifest violations, `coverage_refusals` | `metrics/trio-check.py` |
| commit-gate guard (`acceptance_offenders`) | `metrics/trio-shadow.py` |
| author phase, pin checks, restore, coverage gate, pre-runs, amendments, anti-thrash, SHIP gate | `metrics/trio_loop.py` `AcceptanceController` |
| role, switch, doctor, builder refusal, brief section, `acceptance` commands, author dispatch | `omnigent/trioctl` |
| author prompt, Lead/Evaluator fragments | `prompts/canonical/acceptance*.md` -> `omnigent/entrypoints/trio-omnigent/prompts/` |
| registered author | `omnigent/trio-omnigent-roles/acceptance/config.yaml` (generated; model = the evaluator's) |

## The author and its isolation

- Its workspace is `git archive <base>` in the driver's state directory, not
  the repository. It has no `.git`. Every mailbox directory, `archive/`,
  `.sessions/`, `.trio*`, `.cursor/` and the vendored Trio metrics files are
  removed. `.acceptance-input/` holds only GOAL.md and the optional
  `ACCEPTANCE-NOTES.md`. The repository path is never named in its prompt.
- The driver audits the session's tool calls for evidence that the author
  **read** the loop's plan or code. Only tool-call arguments count, never a
  tool's output and never chat text. A session is flagged when an argument
  names any of:
  - a path inside the loop repository (its checkout, git dir or mailbox);
  - a mailbox file (`PLAN.md`, `VERDICT.md`, `QUEUE.md`, `STATE.md`,
    `LOG.md`, `REPORT.md`) that is not a file of the export itself;
  - a lab hidden-pack path (`/hidden/`, `speed/hard`).

  Route literals such as `/api/openrouter/stats`, `$TMPDIR`, toolchain
  paths (`~/.nvm/...`) and system paths never contaminate (eval-r19
  finding 2). A contaminated session is discarded and re-run once with a
  stern prefix. A second contaminated session stops the loop
  (`acceptance-contaminated`). When the broker history has no row
  recognisable as a tool call, the audit is limited: it checks only
  whether the authored files spell the loop repository's path, and records
  `limited: true`.
- Validation runs on a **fresh** export, so files the author created in its
  own workspace never make a check pass. A `behaviour`/`doc` check that
  PASSes at base, ERRORs, misquotes the GOAL, breaks the schema or exceeds
  the budget is dropped. The 4th and later guards are dropped. UNAVAILABLE
  checks are kept and flagged. The author gets one retry, with the drop
  list, when more than 30% were dropped or fewer than 5 checks remain.
- Tier: the author runs on the Lead/Evaluator model. Without
  `[roles.acceptance]` it inherits `[roles.evaluator]`. With the switch on,
  the doctor FAILs unless lead, evaluator and acceptance resolve to one
  model and effort, and `omnigent loop` refuses a static tier mismatch.

## Freeze, pin and integrity

- The freeze commit is `acceptance: freeze <n> checks (<model>)` with the
  trailer `Acceptance-Pin: <sha256>`. The driver makes it under git's own
  `index.lock`: its tree is HEAD plus the pack only, and the real index gets
  the same entries. A concurrent Lead commit can therefore neither sweep the
  pack into its own commit nor drop it.
- The pin, the FROZEN digest, the pin commit and the chain live in
  `$XDG_STATE_HOME/trio-agent-loop/acceptance/<mailbox>-<hash>/acceptance.json`
  (override: `TRIO_ACCEPTANCE_STATE`), outside the repo and every agent
  worktree. They are mirrored in FROZEN, in STATE.md `acceptance_pin:` and
  in `.driver.json` `acceptance`.
- The driver checks the pack hash and the FROZEN bytes against the pin:
  - before and after every Lead/repair pass;
  - before every slice-eval and integration-eval.

  A mismatch that no valid amend commit explains is restored with
  `acceptance: restore (tamper after <sha12>)` and logged
  `acceptance tamper restored (<role>)`. It counts as a gate breach, and
  the role is re-run once. The driver state counts restored tampers
  loop-wide (`tamper_events`). The second one, in the same pass or any
  later pass, stops the loop with `status: error` (reason
  `acceptance-tamper-repeated`).
- The driver state also records every pack commit the driver made
  (`freeze_commit`, `driver_commits`) and every authenticated human
  amendment (`human_amends`). A commit subject or trailer proves nothing
  by itself.
- `trio-shadow.py --require-commits` also rejects every commit that
  touches the pack, unless it is one of:
  - the single freeze commit;
  - a driver restore or pin commit;
  - a valid amend commit.

  The range starts at `--acceptance-base`, else the driver state's run
  head, else the parent of the commit that added FROZEN. It never starts
  from the working tree.
  - With the driver state visible, driver commits must be in its record,
    and `(human)` amends must be authenticated ones.
  - Without the driver state, a pin commit may only extend FROZEN after
    amend commits, and a restore must put back a pack that was already
    pinned.
  - Only a genuine restore excuses the earlier tamper.
  - A `slice(<id>):` commit behind the freeze (a Lead take-over made while
    the author was still working) is tolerated with an `acceptance note:`.
    The author worked from the base export and never saw it, and the Lead
    prompt forbids product commits before FROZEN. A slice on a line
    without the freeze fails with "acceptance/freeze ordering". The
    driver's `gate breach` LOG line quotes the first acceptance reason.
- Runs copy the pack without what the pin skips (`node_modules/`, caches,
  `*.pyc`), and driver commits never add those paths.
- Isolated builders that touch the mailbox are already retained with
  `mailbox_write` (unchanged).

## Coverage

Every `behaviour`/`doc` id goes into a slice's `covers:` or into
`lead_integration:`. The Lead waits with
`trioctl omnigent acceptance wait --mailbox <mb>`, maps the checks and
commits the PLAN. `trioctl omnigent run builder` (isolated or not) refuses
with exit **10** (`ACCEPTANCE_REFUSED_EXIT`), before any worktree exists,
while any of these hold:

- the pack is not frozen;
- PLAN.md is uncommitted;
- a check is unmapped or unknown, or a binding is undeclared;
- the pack is off its pin.

After each Lead pass the driver runs `coverage_refusals`. A refusal logs
`lead pass refused: acceptance coverage` and re-runs the Lead once with the
refusal text (`acceptance_errors`). A second refusal sets `status: error`
(exit 3).

## Evaluation

- **Slice-eval.** The driver runs the slice's covered checks at the retired
  sha and adds `ACCEPTANCE (covered): ACC-03 PASS · ACC-05 FAIL ...
  [sole-cover|shared]`. A `sole-cover` FAIL is blocking.
- **Integration-eval and lockstep evaluation.** The driver pre-runs the
  pack at the pin and adds a `FROZEN ACCEPTANCE @<sha12>: p/t PASS` block:
  per-check outcomes, the pin chain, `ACCEPTANCE-DISPUTE:` lines and the
  AUTHOR.md pointer. The Evaluator:
  - re-runs the non-PASS checks;
  - adjudicates the disputes;
  - may amend;
  - aims its independent probe at the GOAL sentences no check covers;
  - writes `## Frozen acceptance` in VERDICT.md.
- **After the verdict, amendments.** Amend commits since the pin are
  validated for scope (only a check's script, the fakes, `run`, `expect`,
  `timeout_s`, `binds`, `needs`), for an AMENDMENTS.md record per id, and
  for still FAILing at base. They are limited to at most 2 per loop and 25%
  of the checks.
  - A valid amendment extends the pin chain (`acceptance: pin <sha12>
    (amend ACC-..)`).
  - An invalid one is reverted.
  - Going over the budget forces NEEDS_HUMAN.
  - Every amend commit in a running loop is judged as the Evaluator's,
    including one labelled `(human)`. The driver logs
    ``unauthenticated `(human)` label``, and the scope, record, budget, must-FAIL-at-base
    and no-removal rules all apply (eval-r19 finding 1).
  - With fewer than 4 frozen checks the 25% share allows no amendment. This
    fails safe: an amendment on a small pack forces NEEDS_HUMAN and never
    softens a check (finding 10, kept deliberately).
- **Bindings.** `acceptance_bindings:` values come from the Lead's PLAN and
  reach every run as `ACC_BIND_*`. trio-check validates only the names. The
  Evaluator fragment therefore asks the integration Evaluator to review
  each value against the GOAL (finding 11).
- **Anti-thrash.** A check that FAILs in 2 consecutive integration pre-runs
  with no accepted amendment forces NEEDS_HUMAN.
- **SHIP gate.** On SHIP the pack is re-run at the evaluated sha.
  - Any FAIL gives `ship_unaccepted (acceptance)`: the SHIP is treated as
    ITERATE and the failures ride to the next Lead pass.
  - Any UNAVAILABLE forces NEEDS_HUMAN (`acceptance-unavailable`).
  - An ITERATE whose pre-run had only UNAVAILABLE non-passes is logged
    `acceptance-unavailable-iterate`.
  - A refused SHIP is logged `acceptance: SHIP refused by the acceptance
    gate (verdict becomes ...)`. The driver commits it as `loop: iteration
    N — acceptance gate refused SHIP (<verdict>)`, next to any SHIP
    retirement commit the Evaluator already made (finding 13).

## Human amendments and resume

- A human amends only while the loop is stopped, with
  `trioctl omnigent acceptance amend --human --ids ACC-.. --reason "..."`.
  The command refuses while a live driver holds the mailbox. It commits the
  working-tree edits, or edits already committed since the pin, re-pins,
  and records the amend commit in the driver state. That record, not the
  `(human)` subject, is what authenticates a human amendment.
- A hand-made `acceptance: amend <ids> (human): <reason>` commit is adopted
  on resume only when all of these hold:
  - the driver recorded a clean stop (`stopped_head`) with the pack intact;
  - every pack commit since that stop is such a `(human)` amend;
  - each of those commits has its AMENDMENTS.md records and touches only
    `acceptance/`.

  Otherwise it is logged `not adopted (...)` and restored.
- Resume compares the frozen MANIFEST's `goal_sha256` with the current
  GOAL.md. On a mismatch (a reused mailbox with a new GOAL) the loop stops
  with `status: needs_human`, `phase: acceptance-goal-changed` (finding
  12).
- `trioctl omnigent acceptance wait` also returns when git shows the
  committed freeze with a clean FROZEN. The Lead's shell need not see the
  driver's state file (finding 6).
- `python3 metrics/trio_loop.py run --runner omnigent --acceptance` (or
  `TRIO_ACCEPTANCE=1`) passes the switch into the runner, so the Lead and
  Evaluator get the acceptance prompt blocks (finding 7).

## C1: slice-evals back to fast (independent of the switch)

The r18a slice-eval pack is removed: per-accept table, `attacks:`,
per-slice independent probe, and the re-execution duties. What remains is
the rules plus one `evidence:` summary line. The evaluator rigor is split:

- `RIGOR_CORE` stays in the registered evaluator config and the per-dispatch
  prompt;
- `RIGOR_INTEGRATION` is generated into `integration-rigor.md`, which trioctl
  appends only to integration-eval and lockstep evaluator prompts.

Slice sections without `attacks:` log `attacks=n/a`.

## Commands

```
trioctl omnigent acceptance export   --mailbox <mb> [--base <rev>] [--out <dir>]
trioctl omnigent acceptance freeze   --mailbox <mb> --export <dir> [--model m]   # loop stopped
trioctl omnigent acceptance wait     --mailbox <mb> [--timeout S]               # 0 frozen, 3 author error, 4 timeout
trioctl omnigent acceptance run      --mailbox <mb> [--tree <dir|rev>] [--ids ...] [--json]
trioctl omnigent acceptance status   --mailbox <mb> [--json]
trioctl omnigent acceptance amend    --mailbox <mb> --human --ids ACC-.. --reason "..."   # loop stopped
trioctl omnigent acceptance validate --export .                                  # the author's own check
```

`trioctl acceptance ...` is an alias.

## Install impact (not performed by the build)

- `REGISTRY_PROFILE` is now `cursor-grok-4.6-medium+glm-5.2-max-v4-acc`.
  The doctor refuses until **all** anchors are re-registered: lead,
  evaluator and the new `trio-omnigent-acceptance` (SKILL.md step 3). Needed
  anyway, because C1 changed the registered evaluator prompt.
- `install.sh --omnigent` copies the acceptance role and templates its model
  from the evaluator config. It installs `trio-acceptance.py` next to
  trioctl and in `trio-release-metrics/`.
- Repositories must vendor the METRICS_API 7 set, now five files including
  `trio-acceptance.py`, with `trioctl omnigent metrics refresh --commit`
  before `--acceptance` runs there. An older set still runs with the switch
  off.

## Claude-native seams (N1–N4, later task)

- `MODELS.acceptance = MODELS.evaluator`, with a tier-equality refusal at
  `begin`.
- A `trio-acceptance` agent generated from `prompts/canonical/acceptance.md`
  via a `.claude` overlay target.
- Helper ops in `trio_native_step.py` reuse this build's functions:
  - `acceptance-export` -> `trio-acceptance.build_export`;
  - `acceptance-freeze` -> `AcceptanceController.freeze_from_export` plus
    `audit_transcript`;
  - `coverage` -> `trio-check.coverage_refusals`;
  - `acceptance-run` -> `run_pack`;
  - the `op_gate` pin check -> `AcceptanceController.check_pin`;
  - the `op_apply` SHIP gate and amendments ->
    `AcceptanceController.review_verdict`.
- `PLAN_SCHEMA` gains `covers`, `lead_integration` and
  `acceptance_bindings`.
- The Lead/Evaluator fragments `prompts/canonical/acceptance-lead.md` and
  `acceptance-evaluator.md` are the native `leadPlanPrompt`/`evaluatorPrompt`
  additions.

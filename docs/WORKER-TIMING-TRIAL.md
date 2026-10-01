# Worker-timing trial (historical, induced)

Verified 2026-09-23 on a disposable studio checkout. This note
records **telemetry that ran on two real Grok 4.6 Medium
builders**. It does **not** claim speed or quality superiority,
and it is **not** evidence of natural Trio concurrency.

Candidate pin `9b447fc` stays **uninstalled**. Live default
remains `d435704`. Other active loops were not touched. No
credentials or Jev. No profile, install, or live-mailbox edits.

## What was measured

Run id `wave-trial-studio-20260923`, iteration 1. Two disjoint
repair slices launched concurrently on candidate `trioctl`
(`--worker-events-file`, `--worker-run-id`, `--worker-slice`):

| slice | duration | invocation |
|---|---|---|
| join-mode (Join Normal/Advanced via a frozen mode enum) | 57s (`56908889870` ns) | `f5d3b45e-998f-4324-ac85-2f7791388906` |
| publication persistence (publication request record) | 108s (`107735852377` ns) | `bd720ad2-19cf-44df-a668-ef5c7368e42d` |

Overlap: `56908466000` ns (**56.908466 s**). Same `clock_domain`.
Both `outcome`/`returncode` success. **~109 s** is the
**worker-wave wall only** (`19:53:14Z`–`19:55:03Z`). It is not
Lead latency and not the total loop cycle. Product slice
commits landed later (`19:58:10Z`).

Subprocess lifetime overlap is **not proof of** useful concurrent work.

## Gate and product

Coordinator gate: **3 slices, 0 undeclared** — `join-mode`,
`publish-pending`, then Lead-owned `verify-join-publish`. Workers
did not commit. **No HTML integration repairs** (pairwise
disjoint files). **Two verification-harness fixes** only: defer
`workflow_lab` import inside the bwrap child; load Playwright
via `PLAYWRIGHT_MJS` file URL (ESM ignores `NODE_PATH`).

Independent eval: **SHIP 18/18**, real Playwright inside
`bwrap --unshare-net`, ephemeral `127.0.0.1` server on this
clone. No shared OpenDesign. Pin **`bcbfff3`**. Mailbox
retirement **`0599f05`**. Wave-label / STATE normalization
**`2c782c6`**. Historical base `ccd039d`. Last product slice
`831ebe1`.

## Scope (exact)

- **In:** opt-in worker-process spawn/exit shards + report JSON
  on this induced historical replay; disposable workspace
  `/home/coder/workflow-lab/.runtime/wave-trial-studio`; mailbox
  `/home/coder/workflow-lab/.runtime/wave-trial-studio/loop-wave-trial`.
- **Out:** serial-decision reasons; total cycle timing; live
  default; other loops; secrets; Jev; OpenDesign publish.

## Evidence (paths only; no transcripts)

- Goal / report / verdict / state:
  `.../loop-wave-trial/{GOAL,REPORT,VERDICT,STATE}.md`
- Shards: `.../loop-wave-trial/worker-events.jsonl.d/`
- Eval report copy (private, not git):
  `.../loop-wave-trial/evidence/private/eval/worker-events-report.json`
- Candidate instrument: this tree `omnigent/worker_events.py`,
  pin `9b447fc`. Prior offline fake-worker note:
  `docs/WORKER-TIMING.md`.

## Next (opt-in only)

Possible later adoption: **explicit opt-in telemetry plus launch
fields on one future natural loop**. Serial-decision reasons and
total cycle timing are **not** measured by this patch.

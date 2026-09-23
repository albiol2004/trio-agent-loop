# Worker process overlap (candidate, opt-in)

This candidate measures **OS lifetimes of `cursor` subprocesses** that
`trioctl omnigent run` already starts on parallel waves. It does **not**
time the Trio scheduler, model tokens, or product quality. Default is
**off**: omit `--worker-events-file` / `TRIO_WORKER_EVENTS_FILE` and
`omnigent/worker_events.py` writes nothing. This tree is **not** the live
install at `d435704`; do not treat it as deployed.

**Not in this increment:** Jev integration, secrets access, live prompt
or loop changes, profile/install edits. Broker env is **not** guaranteed
to reach the worker; the Lead must pass explicit CLI/env fields.

## Commands (from `--help`)

Enable on one worker (relative path requires `--mailbox`):

```text
trioctl omnigent run {builder,scout,docs} \
  --worker-events-file WORKER_EVENTS_FILE \
  --mailbox WORKER_EVENTS_MAILBOX \
  --worker-run-id WORKER_RUN_ID \
  --worker-iteration WORKER_ITERATION \
  --worker-slice WORKER_SLICE
```

Equivalent env (same names as `trioctl`): `TRIO_WORKER_EVENTS_FILE`,
`TRIO_WORKER_EVENTS_MAILBOX`, `TRIO_WORKER_RUN_ID`,
`TRIO_WORKER_ITERATION`, `TRIO_WORKER_SLICE`.

Offline report (no models):

```text
trioctl omnigent worker-events-report \
  --events-file EVENTS_FILE [--mailbox MAILBOX] [--run-id RUN_ID]
```

`--run-id` filters paired builders. Report JSON always includes
`caution` from `CAUTION` in `omnigent/worker_events.py`.

## Storage

Writes go to **`{path}.d/{uuid}.jsonl`**, where `{path}` is the resolved
events file and `{uuid}` is a fresh invocation UUID4. The named path is
the locator; shards avoid a shared lock. Relative files must resolve
**inside** mailbox. Schema `trio.cursor_worker.v1`, `source` =
`cursor_worker`. Records omit prompts, stderr, env dumps, credentials.

`pair_builder_runs` uses **builder** only. Incomplete (no spawned+
terminal pair) is **`unknown`**. Nonzero exit is **`failed`** with known
duration, not unknown. Timeout / interrupt / spawn_failed are those
kinds. Overlap uses same `clock_domain`; cross-boot, missing duration,
or disjoint/touching intervals emit **no** overlap (never `0`).

## What intervals are

`duration_ns` is **process wall/monotonic lifetime**, not CPU, not
useful work. Overlap does **not** prove readiness, independence, a
serial scheduling cause, or that a later loop is “better.” Missing
completion stays unknown. Timing alone cannot claim improvement.

## Independent SHIP evidence (offline, fake OS workers)

104 tests, bwrap, no live models. Lock-held ~0.002s with no write.
FIFO telemetry does not block the worker (<1s). Sequential builders:
`overlaps == []`. Two concurrent fake workers: ~0.416s overlap.
Timeout path: ~0.214s known duration. Cross-boot, duplicate
invocation ids, malformed JSONL, incomplete pairs: no false overlap.

## Next adoption

One **controlled, opted-in** real loop with explicit file/mailbox/
run/iteration/slice, then compare **time-to-verified-completion** and
fix integration gaps. Do not claim speed from this telemetry.

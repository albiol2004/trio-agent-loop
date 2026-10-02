# Terminal-Bench (Harbor) adapter for trio-opencode

`trio_tbench_agent:TrioOpenCodeAgent` runs the trio-opencode loop inside a
Terminal-Bench 4.0 task container via Harbor, treating the whole container
as the product (see the module docstring in `trio_tbench_agent.py` and
`goal.py`'s `## Environment` section for what that means for the agents).

This file documents how to pick one of the driver's three arms for a run.
Every arm is selected through an **agent kwarg** -- never a Harbor CLI flag
of its own -- because `TrioOpenCodeAgent`'s constructor kwargs are exactly
what Harbor's `--ak`/`--agent-kwarg key=value` (or a job config file's
`agents[].kwargs`, which `run_job.sh` uses) sets.

## The three arms

### 1. Lockstep (default)

No kwarg needed. `open_loop` defaults to `False`, so the mailbox never gets
a `QUEUE.md`, and both `metrics/trio_loop.py`'s `mode="auto"` dispatch and
`trio-opencode`'s own CLI fall back to the classic lockstep Lead ->
Builder(s) -> Evaluator loop, byte-identical to the agent's behaviour
before the open-loop arm existed.

```bash
tbench/run_job.sh -i '<task-glob>'
```

### 2. Open-loop

Pass `open_loop=true`. This makes `TrioOpenCodeAgent` seed the mailbox with
an empty-queue `loop/QUEUE.md` skeleton (`goal.render_queue_md()`) in
addition to `loop/GOAL.md`; `QUEUE.md`'s mere presence as a file is what
both the driver and `trio-opencode`'s CLI use to select open-loop mode --
there is no separate flag for it.

```bash
tbench/run_job.sh -i '<task-glob>' --ak open_loop=true
```

Optional pass-through flags tune the open-loop run itself (each maps
straight onto a `trio_opencode/cli.py` `start`/`resume` flag; they are
no-ops on a lockstep mailbox and are only emitted on the launch command
line when explicitly set):

| Agent kwarg | CLI flag emitted | Default |
|---|---|---|
| `slice_eval_concurrency=<int>` | `--slice-eval-concurrency <int>` | unset (driver default: 4) |
| `no_isolate_workers=true` | `--no-isolate-workers` | unset (driver default: isolated) |
| `slice_eval_drain_seconds=<float>` | `--slice-eval-drain-seconds <float>` | unset (driver default) |
| `no_kill_check=true` | `--no-kill-check` | unset (driver default: on) |

```bash
# Open-loop, serialized slice-eval, workers not isolated in worktrees:
tbench/run_job.sh -i '<task-glob>' \
  --ak open_loop=true \
  --ak no_isolate_workers=true \
  --ak slice_eval_concurrency=1

# Open-loop with a tighter exit-drain budget and the kill check disabled:
tbench/run_job.sh -i '<task-glob>' \
  --ak open_loop=true \
  --ak slice_eval_drain_seconds=30 \
  --ak no_kill_check=true
```

### 3. Lockstep with acceptance

Pass `acceptance=true`. Unlike `open_loop`, this is **not** a CLI flag at
all: `TrioOpenCodeAgent._build_config()` writes it straight into the
`acceptance` key of the generated `/opt/trio/config.json` (see
`configgen.build_driver_config`), and the driver reads it from there.
Combine it with lockstep (the default) or with `open_loop=true` for
open-loop with acceptance.

```bash
tbench/run_job.sh -i '<task-glob>' --ak acceptance=true
```

## `--ak` value syntax

`harbor run`'s `--ak key=value` (`--agent-kwarg` is the long form) parses
`value` as JSON first and falls back to a plain string, so booleans and
numbers must be written unquoted and lowercase: `--ak open_loop=true`, not
`--ak open_loop=True` or `--ak open_loop="true"`. `run_job.sh` forwards any
extra arguments straight to `harbor run`, so `--ak ...` can be appended
after its own flags exactly as shown above.

# Trio metrics

`metrics/trio-metrics.py` scans project-level `loop*/` directories (or a single
loop mailbox) and reports per-loop iteration counts, verdict sequences, and
final `VERDICT.md` outcomes.

Usage:

```bash
metrics/trio-metrics.py /path/to/project
metrics/trio-metrics.py /path/to/project --json
```

The verdict sequence (`S`=SHIP, `I`=ITERATE, `H`=NEEDS_HUMAN, `B`=BLOCKED)
shows how many evaluator turns a loop took before a final decision.
Iterations-to-SHIP is the
iteration count for loops whose final verdict is SHIP; mean/median are reported
across those loops.

Reused default `loop/` mailboxes are split into segments at each SHIP boundary,
because a single mailbox can contain multiple independent goals.

Loops with no parseable `LOG.md` entries are flagged `unparsed` rather than
reported as zero. Pre-instrumentation mailboxes carry no token data; the script
measures iteration structure only.

Verdict-grammar conformance fixtures and tests live in `tests/`
(`python3 -m pytest -q metrics/tests`).

`metrics/trio-shadow.py` checks each slice declared in a `PLAN.md` `slices`
block against the commits actually made in its repo: every declared write path
must cover the files the slice's commits touched (declared-vs-actual), and it
flags both undeclared touches and declared-but-untouched paths.

Usage:

```bash
python3 metrics/trio-shadow.py --mailbox <project-or-loop-dir>
python3 metrics/trio-shadow.py --mailbox <project-or-loop-dir> --json
```

`--mailbox` takes a project directory (with a `loop/PLAN.md`) or a loop
directory directly; it defaults to the current directory. Shadow mode is
informational only — the script always exits 0 on a successful analysis and
never gates the pipeline. Exit 2 means `PLAN.md` was missing or its `slices`
block did not match the restricted shape. With `--require-commits` the
script becomes the active commit gate: exit 1 when any code-changing slice
(a `writes:` entry neither `api:` nor under `loop/`) lacks a
`slice(<id>): ` commit.

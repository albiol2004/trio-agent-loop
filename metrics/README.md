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

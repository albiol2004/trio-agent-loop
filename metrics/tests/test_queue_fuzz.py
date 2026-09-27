"""Seeded grammar fuzz for the QUEUE.md `faults:` parser + integration gate.

Port of the r11g evaluator's fuzz (eval-r11g/VERDICT.md "Probe 4"). The
invariant is the integration gate's safety property:

    if the text contains a well-formed LIVE fault, then `_live_faults` is
    non-empty OR the queue errors contain a "`faults:` block:" error

-- i.e. `_slices_fully_retired(...) and not _integration_gate_held(...)`
can never let the integration eval start over a fault the Evaluator wrote
correctly, whatever junk surrounds it (r11g NEW-S1 + Q1 + P1).

Grammar per file: 1-5 entries (55% canonical: header col 2 -- or 15% at
col 6 / a tab --, keys col 4, all six keys, maybe shuffled or `reason:`
last, maybe with legitimate `reason:`
continuations including key- and id-shaped ones; 45% mutated: a missing
key, a key re-indented to col 0/1/2/3/5/6 or a tab, a duplicated key, a
header at an odd indent), junk lines between them at cols 0-8 or a tab,
quoted values, 20% CRLF files, and 8% of files with a fence-level
mutation (a second ```yaml `faults:` fence, or the whole block in an
untagged / ``~~~yaml`` / ```` ```yaml title ```` fence).
"""
from __future__ import annotations

import random

import pytest

from metrics import trio_loop

TM = trio_loop._METRICS

SEEDS = (1, 2, 3, 4, 5)
CASES_PER_SEED = 2000

KEYS = ("slice", "observed_at", "scope", "reason", "status")
LIVE = ("open", "taken", '"open"')
CLOSED = ("done", "stale")
JUNK_COLS = (0, 1, 2, 3, 4, 5, 6, 8, "tab")
JUNK_TEXT = (
    "some prose here", "status: done", "status: stale", "status: open",
    "reason:", "reason: junk", "note: y", "id: f5", "* id: f7", "-id: f2",
    "ID=5 and more", "- id: f96", "- Slice: s9", "faults:", "(see f0)",
    "- item", "scope: local:x.py", "slice: s3", "observed_at: abc",
)
REASON_CONT = (
    "continues here", "status: done", "ID=5 and more", "Id: 7 x",
    "- id mismatch", "* id thing", "- id: f9", "slice: other",
)


def _pad(col) -> str:
    return "\t" if col == "tab" else " " * col


def _value(rng: random.Random, key: str, idx: int) -> str:
    if key == "slice":
        return rng.choice(("s1", "s2", "integration", '"s1"'))
    if key == "observed_at":
        return rng.choice(("deadbeef", "abc", "0" * 40))
    if key == "scope":
        return rng.choice(("local:a.py", "design", "[a.py, b.py]", "local:a.py,b.py"))
    if key == "reason":
        return rng.choice(("real bug", '"quoted reason"', "wrapped text", "x: y"))
    raise AssertionError(key)


def _key_order(rng: random.Random) -> list[str]:
    keys = list(KEYS)
    roll = rng.random()
    if roll < 0.3:
        rng.shuffle(keys)
    elif roll < 0.5:
        # `reason:` last: the r11g NEW-S1 shape (a deeper next header)
        keys.remove("reason")
        keys.append("reason")
    return keys


def _canonical(rng: random.Random, idx: int) -> tuple[list[str], bool]:
    """A well-formed entry; returns (lines, is_live). The header is at col
    2, or (15%) at col 6 / a tab -- still a well-formed entry the parser
    accepts (keys at col 4), the NEW-S1 "deeper next header" variant."""
    status = rng.choice(LIVE + CLOSED)
    keys = _key_order(rng)
    header_col = rng.choice((6, "tab")) if rng.random() < 0.15 else 2
    lines = [f"{_pad(header_col)}- id: f{idx}"]
    for key in keys:
        if key == "status":
            lines.append(f"    status: {status}")
            continue
        lines.append(f"    {key}: {_value(rng, key, idx)}")
        if key == "reason" and rng.random() < 0.3:
            for _ in range(rng.randint(1, 2)):
                col = rng.choice((6, 8, "tab"))
                lines.append(f"{_pad(col)}{rng.choice(REASON_CONT)}")
    return lines, status in LIVE


def _mutated(rng: random.Random, idx: int) -> list[str]:
    status = rng.choice(LIVE + CLOSED)
    keys = _key_order(rng)
    header_col = 2
    mutation = rng.choice(("missing", "reindent", "tab", "dup", "header"))
    # bias the re-indent toward `reason:` (the fold key) a third of the time
    target = "reason" if rng.random() < 0.33 else rng.choice(keys)
    if mutation == "missing":
        keys.remove(target)
    if mutation == "header":
        header_col = rng.choice((0, 1, 3, 4, 6, "tab"))
    lines = [f"{_pad(header_col)}- id: f{idx}"]
    for key in keys:
        value = status if key == "status" else _value(rng, key, idx)
        col = 4
        if key == target and mutation == "reindent":
            col = rng.choice((0, 1, 2, 3, 5, 6))
        elif key == target and mutation == "tab":
            col = "tab"
        lines.append(f"{_pad(col)}{key}: {value}")
        if key == target and mutation == "dup":
            other = rng.choice(LIVE + CLOSED) if key == "status" else "dup"
            lines.append(f"{_pad(rng.choice((2, 4, 6)))}{key}: {other}")
    return lines


def _faults_body(rng: random.Random, start: int) -> tuple[list[str], bool]:
    lines = ["faults:"]
    live = False
    if rng.random() < 0.2:
        lines.append(f"{_pad(rng.choice(JUNK_COLS))}{rng.choice(JUNK_TEXT)}")
    for n in range(rng.randint(1, 5)):
        idx = start + n
        if rng.random() < 0.55:
            entry, is_live = _canonical(rng, idx)
            live = live or is_live
        else:
            entry = _mutated(rng, idx)
        lines += entry
        if rng.random() < 0.35:
            for _ in range(rng.randint(1, 2)):
                lines.append(
                    f"{_pad(rng.choice(JUNK_COLS))}{rng.choice(JUNK_TEXT)}"
                )
    return lines, live


def _case(rng: random.Random) -> tuple[str, bool]:
    body, live = _faults_body(rng, 0)
    retired = ["```yaml", "retired:", "  - slice: s1", "    sha: " + "a" * 40,
               "    at: 2026-01-01T00:00:00Z", "```", ""]
    fence_mut = rng.random()
    if fence_mut < 0.03:
        # a second ```yaml `faults:` fence with its own entries
        body2, live2 = _faults_body(rng, 50)
        lines = retired + ["```yaml", *body, "```", "", "```yaml", *body2, "```"]
        live = live or live2
    elif fence_mut < 0.08:
        opener = rng.choice(("```", "~~~yaml", "```yaml title", "```yml extra"))
        closer = "~~~" if opener.startswith("~") else "```"
        lines = retired + [opener, *body, closer]
    else:
        lines = retired + ["```yaml", *body, "```"]
    eol = "\r\n" if rng.random() < 0.2 else "\n"
    return eol.join(lines) + eol, live


def _gate_holds(text: str) -> tuple[bool, dict]:
    queue = TM.parse_queue_block(text)
    held = bool(trio_loop._live_faults(queue)) or bool(
        trio_loop._queue_fault_errors(queue)
    )
    return held, queue


@pytest.mark.parametrize("seed", SEEDS)
def test_well_formed_live_fault_always_holds_the_gate(seed: int) -> None:
    rng = random.Random(seed)
    counterexamples: list[str] = []
    live_cases = held_by_live = held_by_errors_only = 0
    for _ in range(CASES_PER_SEED):
        text, live = _case(rng)
        if not live:
            continue
        live_cases += 1
        held, queue = _gate_holds(text)
        if trio_loop._live_faults(queue):
            held_by_live += 1
        elif held:
            held_by_errors_only += 1
        if not held:
            counterexamples.append(text)
    assert live_cases > CASES_PER_SEED // 4, "grammar must produce live faults"
    assert held_by_live > 0 and held_by_errors_only > 0
    assert not counterexamples, (
        f"seed {seed}: {len(counterexamples)} counterexample(s); first:\n"
        + counterexamples[0]
    )


def test_fuzz_grammar_is_deterministic() -> None:
    a = [_case(random.Random(7)) for _ in range(3)]
    b = [_case(random.Random(7)) for _ in range(3)]
    assert a == b

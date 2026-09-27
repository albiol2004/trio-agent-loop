"""Second permanent fuzz: fence-level grammar for the QUEUE.md `faults:`
block + integration gate (r11h F-FENCE).

Reconstruction of the r11h evaluator's independent grammar
(eval-r11h/VERDICT.md "Probe 1"), which found 625 counterexamples in
15 000 files at 7c28f8a -- all from the fence-closing rule or a fence
without the `faults:` key. The invariant is the gate's safety property:

    if the text contains a well-formed LIVE fault, then `_live_faults` is
    non-empty OR the queue errors contain a "`faults:` block:" error

Grammar per file:
- entries (1-5 per faults fence): header at col 2 (80%) or col 4 / col 6 /
  a tab, keys at header+2 (col 4 under a tab); key order shuffled 30%, `reason:` last 30%;
  values quoted 10%; statuses include `"open"` and `'taken'`;
- `reason:` continuations indented +1/+2/+4 past the reason key, shaped
  like keys, headers, bullets and top keys;
- inner code blocks inside `reason:` (10%): ```python, ```, ~~~, ~~~yaml,
  ```yaml, ```text openers; bodies `x = 1`, a quoted `faults:`/`- id: f99`
  snippet or a quoted `retired:` entry; 15% left unterminated;
- entry mutations (40%): a missing key, a key re-indented to col
  0/1/2/3/5/6 or a tab, a duplicated key, a duplicated `status:`, an odd
  header indent, a quoted id;
- junk lines at cols 0/1/2/3/4/5/6/8/10, tab, double tab: prose,
  key-shaped, header-shaped, top keys, bullets, comments, quoted text and
  fence lines (```, ~~~, ```yaml) -- a col 0-3 fence line is a stray
  fence line inside the block;
- file level: top key `faults:` 90%, `  faults:` 4%, else `fault:`,
  `Faults:`, `faults` (no colon), `faults: []` or missing; opener (15%)
  ```yml, ``` yaml, ```yaml with 2/3 leading spaces, ````yaml, ```YAML,
  ~~~yaml, ~~~, ```, ```yaml title, ```json, else ```yaml; 5% of faults
  fences unterminated; 15% of files have a second faults fence that drops
  its `faults:` key half the time; retired fence before (70%) or after the
  faults fence; 30% of files end with a ```sh prose fence; 20% CRLF.

A "well-formed live fault" is a canonical (unmutated) entry with a live
status written as a fault entry anywhere in a faults fence -- whatever
key, opener or neighbouring junk surrounds it. Quoted snippets inside a
`reason:` code block never count.
"""
from __future__ import annotations

import random
import time

import pytest

from metrics import trio_loop

TM = trio_loop._METRICS

SEEDS = (1, 2, 3, 4, 5)
CASES_PER_SEED = 3000

KEYS = ("slice", "observed_at", "scope", "reason", "status")
LIVE = ("open", "taken", '"open"', "'taken'")
CLOSED = ("done", "stale")
JUNK_COLS = (0, 1, 2, 3, 4, 5, 6, 8, 10, "tab", "tab2")
JUNK_TEXT = (
    "some prose here", "status: done", "status: stale", "status: open",
    "reason:", "note: y", "id: f5", "scope: x", "slice: s3",
    "observed_at: abc", "sha: abc", "at: t", "* id: f7", "-id: f2",
    "ID=5 and more", "- id: f96", "- id:", "- Slice: s9", "- slice: s1",
    '- id: "f5"', "faults:", "retired:", "- item", "(see f0)",
    "# comment", 'status: "done"', '"quoted junk"', "```", "~~~", "```yaml",
)
REASON_CONT = (
    "status: done", "ID=5 and more", "- id mismatch", "* id thing",
    "slice: s9", "retired: no", "faults: see above", "Id: 7 x",
    "observed_at:", "- item",
)
INNER_OPENERS = ("```python", "```", "~~~", "~~~yaml", "```yaml", "```text")
INNER_BODIES = (
    ("x = 1",),
    ("faults:", "  - id: f99", "    status: open"),
    ("retired:", "  - slice: s1", "    sha: " + "b" * 40, "    at: t"),
)
OPENERS = (
    "```yml", "``` yaml", "  ```yaml", "   ```yaml", "````yaml", "```YAML",
    "~~~yaml", "~~~", "```", "```yaml title", "```json",
)
TOP_KEY_TYPOS = ("fault:", "Faults:", "faults", "faults: []", None)


def _pad(col) -> str:
    if col == "tab":
        return "\t"
    if col == "tab2":
        return "\t\t"
    return " " * col


def _value(rng: random.Random, key: str, idx: int) -> str:
    if key == "slice":
        v = rng.choice(("s1", "s2", "integration"))
    elif key == "observed_at":
        v = rng.choice(("deadbeef", "abc", "0" * 40))
    elif key == "scope":
        v = rng.choice(("local:a.py", "design", "[a.py, b.py]",
                        "local:a.py,b.py"))
    elif key == "reason":
        v = rng.choice(("real bug", "wrapped text", "x: y z", "see f0"))
    else:  # pragma: no cover
        raise AssertionError(key)
    if rng.random() < 0.1 and not v.startswith("["):
        v = f'"{v}"'
    return v


def _key_order(rng: random.Random) -> list[str]:
    keys = list(KEYS)
    roll = rng.random()
    if roll < 0.3:
        rng.shuffle(keys)
    elif roll < 0.6:
        keys.remove("reason")
        keys.append("reason")
    return keys


def _reason_extra(rng: random.Random, key_col: int) -> list[str]:
    """Legitimate `reason:` continuations and inner code blocks."""
    out: list[str] = []
    if rng.random() < 0.3:
        for _ in range(rng.randint(1, 2)):
            col = key_col + rng.choice((1, 2, 4))
            out.append(" " * col + rng.choice(REASON_CONT))
    if rng.random() < 0.1:
        col = " " * (key_col + 2)
        opener = rng.choice(INNER_OPENERS)
        out.append(col + opener)
        out += [col + ln for ln in rng.choice(INNER_BODIES)]
        if rng.random() >= 0.15:
            out.append(col + ("~~~" if opener.startswith("~") else "```"))
    return out


def _entry(rng: random.Random, idx: int) -> tuple[list[str], bool]:
    """One fault entry: (lines, is_well_formed_live)."""
    status = rng.choice(LIVE + CLOSED)
    keys = _key_order(rng)
    roll = rng.random()
    header_col = 2 if roll < 0.8 else rng.choice((4, 6, "tab"))
    # keys at header+2 (a tab header keeps its keys at col 4)
    key_col = header_col + 2 if isinstance(header_col, int) else 4
    mutated = rng.random() < 0.4
    mutation = rng.choice(("missing", "reindent", "tab", "dup", "dupstatus",
                           "header", "quoted_id")) if mutated else None
    target = "reason" if rng.random() < 0.33 else rng.choice(keys)
    if mutation == "missing":
        keys.remove(target)
    if mutation == "header":
        header_col = rng.choice((0, 1, 3, 5))
    fid = f'"f{idx}"' if mutation == "quoted_id" else f"f{idx}"
    lines = [f"{_pad(header_col)}- id: {fid}"]
    for key in keys:
        value = status if key == "status" else _value(rng, key, idx)
        col = " " * key_col
        if key == target and mutation == "reindent":
            col = " " * rng.choice((0, 1, 2, 3, 5, 6))
        elif key == target and mutation == "tab":
            col = "\t"
        lines.append(f"{col}{key}: {value}")
        if key == "reason":
            lines += _reason_extra(rng, key_col)
        if key == target and mutation == "dup":
            lines.append(f"{' ' * rng.choice((2, 4, 6))}{key}: dup")
        if key == "status" and mutation == "dupstatus":
            lines.append(f"{' ' * key_col}status: {rng.choice(LIVE + CLOSED)}")
    return lines, (not mutated) and status in LIVE


def _junk(rng: random.Random) -> str:
    return f"{_pad(rng.choice(JUNK_COLS))}{rng.choice(JUNK_TEXT)}"


def _entries(rng: random.Random, start: int) -> tuple[list[str], bool]:
    lines: list[str] = []
    live = False
    if rng.random() < 0.2:
        lines.append(_junk(rng))
    for n in range(rng.randint(1, 5)):
        entry, is_live = _entry(rng, start + n)
        live = live or is_live
        lines += entry
        if rng.random() < 0.3:
            for _ in range(rng.randint(1, 2)):
                lines.append(_junk(rng))
    return lines, live


def _top_key(rng: random.Random) -> str | None:
    roll = rng.random()
    if roll < 0.90:
        return "faults:"
    if roll < 0.94:
        return "  faults:"
    return rng.choice(TOP_KEY_TYPOS)


def _fence(rng: random.Random, key: str | None, body: list[str],
           allow_unterminated: bool = True) -> list[str]:
    opener = rng.choice(OPENERS) if rng.random() < 0.15 else "```yaml"
    stripped = opener.lstrip(" ")
    closer = "~~~" if stripped.startswith("~") else (
        "````" if stripped.startswith("````") else "```")
    lines = [opener] + ([key] if key is not None else []) + body
    if not (allow_unterminated and rng.random() < 0.05):
        lines.append(closer)
    return lines


def _case(rng: random.Random) -> tuple[str, bool]:
    body, live = _entries(rng, 0)
    faults = _fence(rng, _top_key(rng), body)
    if rng.random() < 0.15:
        body2, live2 = _entries(rng, 50)
        key2 = "faults:" if rng.random() < 0.5 else None
        faults += [""] + _fence(rng, key2, body2)
        live = live or live2
    retired = ["```yaml", "retired:", "  - slice: s1",
               "    sha: " + "a" * 40, "    at: 2026-01-01T00:00:00Z", "```"]
    if rng.random() < 0.7:
        lines = retired + [""] + faults
    else:
        lines = faults + [""] + retired
    if rng.random() < 0.3:
        lines += ["", "Notes:", "```sh", "echo done", "```"]
    eol = "\r\n" if rng.random() < 0.2 else "\n"
    return eol.join(lines) + eol, live


def run_seed(seed: int, cases: int = CASES_PER_SEED) -> dict:
    rng = random.Random(seed)
    stats = {"files": cases, "live": 0, "held_live": 0, "held_errors": 0,
             "counterexamples": []}
    for _ in range(cases):
        text, live = _case(rng)
        if not live:
            continue
        stats["live"] += 1
        queue = TM.parse_queue_block(text)
        if trio_loop._live_faults(queue):
            stats["held_live"] += 1
        elif trio_loop._queue_fault_errors(queue):
            stats["held_errors"] += 1
        else:
            stats["counterexamples"].append(text)
    return stats


@pytest.mark.parametrize("seed", SEEDS)
def test_fence_grammar_well_formed_live_fault_always_holds_the_gate(
    seed: int,
) -> None:
    start = time.monotonic()
    stats = run_seed(seed)
    assert stats["live"] > CASES_PER_SEED // 4, "grammar must produce live faults"
    assert stats["held_live"] > 0 and stats["held_errors"] > 0
    ce = stats["counterexamples"]
    assert not ce, f"seed {seed}: {len(ce)} counterexample(s); first:\n{ce[0]}"
    assert time.monotonic() - start < 30


def test_fence_grammar_covers_every_fence_category() -> None:
    """The grammar actually emits each fence-level shape it claims to."""
    rng = random.Random(11)
    texts = [_case(rng)[0] for _ in range(3000)]
    blob = "\n".join(texts)
    for needle in ("fault:\n", "Faults:\n", "\n  faults:", "~~~yaml\n",
                   "```yaml title", "   ```yaml", "````yaml", "```json",
                   "      ```python", "\r\n", "```sh\n"):
        assert needle in blob, needle
    # a stray col-0 fence line inside a faults block
    assert any("\n```\n  - id:" in t for t in texts)


def test_fence_fuzz_grammar_is_deterministic() -> None:
    a = [_case(random.Random(7)) for _ in range(3)]
    b = [_case(random.Random(7)) for _ in range(3)]
    assert a == b

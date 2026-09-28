"""Seeded fuzz for the r15 keys: `retired:` `repo:`, `repos:`, `full_check:`
and `repo@sha` pins. Invariants: the parsers never raise; a well-formed
retired entry keeps its (slice, repo, sha) whatever junk surrounds it and
wherever its `repo:` line sits; an entry without `repo:` has no repo key
(home), so pre-r15 mailboxes parse unchanged; every parsed repo name is
kebab-case and unique, every full_check key kebab-case, every pin sha hex."""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from metrics import trio_loop

TM = trio_loop._METRICS
SEEDS = (11, 12, 13, 14, 15)
CASES = 800
JUNK = ("prose", "repo:", "repo: x y", "- repo: z", "sha: 1", "at:", "  - slice", "#c",
        "repos:", "- name: A_B", "full_check:", "{", "}", "app@", "`", "\t- x: y")
NAMES = ("home", "app-backend", "app-frontend", "svc", "a1", "Bad_Name", "x--y", "")


def _sha(rng):
    return "".join(rng.choice("0123456789abcdef") for _ in range(40))


def _retired_text(rng):
    good, lines = [], ["retired:"]
    for i in range(rng.randint(1, 5)):
        sid, sha = f"s{i}", _sha(rng)
        repo = rng.choice((None, "app-backend", "svc"))
        keys = [("sha", sha), ("at", "2026-09-28T00:00:00Z")]
        if repo:
            keys.insert(rng.randint(0, 2), ("repo", repo))
        if rng.random() < 0.8:
            lines.append(f"  - slice: {sid}")
            lines += [f"    {k}: {v}" for k, v in keys]
            good.append((sid, repo, sha))
        else:  # mutated: drop a required key
            lines.append(f"  - slice: {sid}")
            lines += [f"    {k}: {v}" for k, v in keys if k != "sha"]
        if rng.random() < 0.3:
            junk = rng.choice(JUNK)
            lines.append(" " * rng.choice((0, 2, 4, 6)) + junk)
            if good and good[-1][0] == sid and junk.startswith("repo:"):
                good.pop()  # a well-formed `repo:` line re-keys the entry
    return "\n".join(lines) + "\n", good


@pytest.mark.parametrize("seed", SEEDS)
def test_retired_repo_key_survives_junk(seed):
    rng = random.Random(seed)
    for _ in range(CASES):
        text, good = _retired_text(rng)
        errors: list[str] = []
        entries = TM.parse_retired(text.splitlines(), errors=errors)
        got = {(e["slice"], e.get("repo") or None, e["sha"]) for e in entries}
        for sid, repo, sha in good:
            # a junk line right after an entry may poison only that entry
            if (sid, repo, sha) not in got:
                assert errors, text
        for e in entries:
            # Any other value can only come from a junk `repo:` line (an
            # empty one reads as home; others are trio-check violations).
            if e.get("repo") not in (None, "app-backend", "svc"):
                assert re.search(r"^\s*repo:", text, re.M) and (
                    "repo: x y" in text or "repo:" in [ln.strip() for ln in text.splitlines()]
                ), (text, e)


@pytest.mark.parametrize("seed", SEEDS)
def test_repos_block_never_raises(seed, tmp_path):
    rng = random.Random(seed)
    for _ in range(CASES):
        lines = ["```yaml", "repos:"]
        for _e in range(rng.randint(0, 4)):
            name = rng.choice(NAMES)
            if rng.random() < 0.3:
                lines.append(f"  - {{name: {name}, path: {rng.choice(('.', 'x', '/tmp'))}}}")
            else:
                fields = [f"name: {name}", f"path: {rng.choice(('.', 'x', '/tmp', '$H/x', '~'))}"]
                rng.shuffle(fields)
                lines.append(f"  - {fields[0]}")
                lines += [f"    {f}" for f in fields[1:]]
            if rng.random() < 0.2:
                lines.append(" " * rng.choice((0, 2, 4)) + rng.choice(JUNK))
        lines.append("```")
        repos, errors = TM.parse_repos_block("\n".join(lines) + "\n", tmp_path)
        names = [r["name"] for r in repos]
        assert len(names) == len(set(names))
        assert all(TM.REPO_NAME_RE.match(n) and n != "home" for n in names)


@pytest.mark.parametrize("seed", SEEDS)
def test_full_check_and_pins_never_raise(seed):
    rng = random.Random(seed)
    atoms = ("full_check:", "  app-backend: pytest -q", "  home: make", "  plain cmd",
             '{ a: "x", b: y }', "{ a: x", "full_check_budget_s: 60", "  Bad_Key: z", "", "```")
    for _ in range(CASES):
        text = "\n".join(rng.choice(atoms) for _ in range(rng.randint(1, 6))) + "\n"
        if rng.random() < 0.5:
            text = "full_check: " + text
        checks, errors = TM.parse_full_check(text)
        assert all(TM.REPO_NAME_RE.match(k) for k in checks), (text, checks)
        pins_src = " ".join(rng.choice(("home@abc1234", "x@" + _sha(rng), "zz", "@", "a@b", _sha(rng)[:9]))
                            for _ in range(rng.randint(0, 4)))
        for name, sha in TM.parse_repo_pins([pins_src]):
            assert re.fullmatch(r"[0-9a-f]{7,40}", sha) and TM.REPO_NAME_RE.match(name)

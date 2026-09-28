"""Seeded fuzz for the r16 root-free helpers.

- `trio-metrics.loop_slug` == `omnigent/root_free.loop_slug` on random
  unicode/path strings; the slug is always one safe ref component.
- loop core `_read_state`/`_update_state` with the r16 STATE keys
  (`landed`, `target_ref`, `target_base`) in random order/case/prefix among
  junk lines: unrelated lines survive, optional keys are never added empty.
- `apply_aggregates` on random/garbage `.sessions/aggregates.json`: never
  raises, and changes a repo only when a well-formed entry names it.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import random
import re
import subprocess
from pathlib import Path

import pytest

from metrics import trio_loop

TM = trio_loop._METRICS
REPO = Path(__file__).resolve().parents[2]
SEEDS = (161, 162, 163)
CASES = 400
SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


RF = _load("root_free_r16fuzz", REPO / "omnigent" / "root_free.py")

ALPHABET = (
    "abcXYZ019_-./ \t" "..//" "~^:?*[\\@{}" "éßü中文🙂" "​ " "loop/" "--"
)


def _random_path(rng: random.Random) -> str:
    n = rng.randint(0, 40 if rng.random() < 0.9 else 200)
    parts = [rng.choice(ALPHABET) for _ in range(n)]
    if rng.random() < 0.3:
        parts.insert(0, rng.choice(("loop/", "/", "./", "../", " loop/x/")))
    return "".join(parts)


@pytest.mark.parametrize("seed", SEEDS)
def test_loop_slug_matches_root_free_and_is_a_ref_component(seed):
    rng = random.Random(seed)
    sample: list[str] = []
    for i in range(CASES):
        raw = _random_path(rng)
        slug = TM.loop_slug(raw)
        assert slug == RF.loop_slug(raw), raw
        assert SLUG_RE.fullmatch(slug), (raw, slug)
        assert len(slug) <= 120
        if i % (CASES // 10) == 0:
            sample.append(slug)
    for slug in sample:
        proc = subprocess.run(
            ["git", "check-ref-format", f"refs/heads/trio/{slug}"], capture_output=True
        )
        assert proc.returncode == 0, slug


def test_loop_slug_known_values():
    assert TM.loop_slug("loop/scenario-basis-and-months") == "loop--scenario-basis-and-months"
    assert TM.loop_slug("/loop/a.b/") == "loop--a-b"
    assert TM.loop_slug("") == TM.loop_slug("///") == "loop"


# ------------------------------------------------------------ STATE.md

KEYS = ("iteration", "status", "phase", "evaluated_sha", "evaluator_attempt",
        "evaluated_repos", "landed", "target_ref", "target_base")
OPTIONAL = tuple(trio_loop._OPTIONAL_STATE_KEYS)
JUNK = ("schema: 1", "mission: r16", "max_iterations: 5", "# heading", "", "landed_at: x",
        "target: main", "- note: landed: no", "  prose line", "verdict: SHIP",
        "target_refs: y", "status_old: z", "landedd: q", "\tphase2: p", "- - status: x",
        "lañded: ü")


def _state_text(rng: random.Random) -> str:
    lines = []
    for _ in range(rng.randint(0, 12)):
        if rng.random() < 0.45:
            key = rng.choice(KEYS)
            key = rng.choice((key, key.upper(), key.title()))
            prefix = rng.choice(("", "- ", "  ", "  - "))
            sep = rng.choice((": ", ":", " : "))
            value = rng.choice(("", "main", "abc123", "shipped", "running", "  x  "))
            lines.append(f"{prefix}{key}{sep}{value}")
        else:
            lines.append(rng.choice(JUNK))
    text = "\n".join(lines)
    return text + ("\n" if rng.random() < 0.8 else "")


def _is_key_line(line: str) -> bool:
    return bool(trio_loop.STATE_RE.match(line))


@pytest.mark.parametrize("seed", SEEDS)
def test_update_state_preserves_unrelated_lines_and_never_adds_empty_optionals(seed, tmp_path):
    rng = random.Random(seed)
    path = tmp_path / "STATE.md"
    for _ in range(CASES):
        text = _state_text(rng)
        path.write_text(text, encoding="utf-8")
        before = trio_loop._read_state(path)
        orig = text.splitlines()
        present = {trio_loop.STATE_RE.match(ln).group(1).lower() for ln in orig if _is_key_line(ln)}
        key = rng.choice(KEYS)
        value = rng.choice(("", "v1", "abc0123", "needs_land"))
        trio_loop._update_state(path, {key: value})
        after = path.read_text(encoding="utf-8").splitlines()
        # Unrelated lines keep their text and order.
        assert [ln for ln in after if not _is_key_line(ln)] == [
            ln for ln in orig if not _is_key_line(ln)
        ], (text, key, value)
        # Other keys' lines are untouched too.
        other = [ln for ln in orig if _is_key_line(ln)
                 and trio_loop.STATE_RE.match(ln).group(1).lower() != key]
        assert [ln for ln in after if _is_key_line(ln)
                and trio_loop.STATE_RE.match(ln).group(1).lower() != key] == other
        added = len(after) - len(orig)
        if key in present:
            assert added == 0
            assert trio_loop._read_state(path)[key] == value.strip()
        elif key in OPTIONAL and not value:
            assert after == orig, (text, key)  # an empty optional key adds nothing
        else:
            assert added == 1 and after[-1] == f"{key}: {value}"
        # Never an empty optional key that was not there before.
        for opt in OPTIONAL:
            if opt not in present and opt != key:
                assert not any(
                    _is_key_line(ln) and trio_loop.STATE_RE.match(ln).group(1).lower() == opt
                    for ln in after
                )
        # Reading is stable for every key the update did not name.
        now = trio_loop._read_state(path)
        for k in KEYS:
            if k != key:
                assert now[k] == before[k], (text, k)


@pytest.mark.parametrize("seed", SEEDS)
def test_update_state_multi_key_land_record(seed, tmp_path):
    rng = random.Random(seed)
    path = tmp_path / "STATE.md"
    for _ in range(CASES):
        text = _state_text(rng)
        path.write_text(text, encoding="utf-8")
        updates = {k: rng.choice(("", "x", "deadbeef")) for k in rng.sample(KEYS, rng.randint(1, 5))}
        trio_loop._update_state(path, updates)
        state = trio_loop._read_state(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        for k, v in updates.items():
            if v or k not in OPTIONAL:
                assert state[k] == v.strip(), (text, updates)
        for ln in lines:
            m = trio_loop.STATE_RE.match(ln)
            if m and m.group(1).lower() in OPTIONAL and not m.group(2).strip():
                # An empty optional line may only be one that was already there.
                assert ln in text.splitlines() or m.group(1).lower() in {
                    trio_loop.STATE_RE.match(o).group(1).lower()
                    for o in text.splitlines() if _is_key_line(o)
                }, (text, updates, ln)


# ------------------------------------------------------------ aggregates

NAMES = ("app-backend", "svc", "home", "x", "")


def _garbage(rng: random.Random, *, string_paths: bool):
    choices = [
        lambda: "{not json",
        lambda: "",
        lambda: "null",
        lambda: "[1, 2]",
        lambda: '"repos"',
        lambda: json.dumps({"repos": rng.choice(([], "x", 3, None, [{"name": "svc"}]))}),
        lambda: json.dumps({"schema": 1}),
        lambda: json.dumps({"schema": 1, "repos": _table(rng, string_paths)}),
        lambda: json.dumps({"schema": 1, "repos": _table(rng, string_paths)}),
        lambda: json.dumps({"schema": 1, "repos": _table(rng, string_paths)}),
    ]
    return rng.choice(choices)()


def _entry(rng: random.Random, string_paths: bool):
    kind = rng.random()
    if kind < 0.15:
        return rng.choice((None, 3, "path", [], ["/x"]))
    entry = {}
    if rng.random() < 0.8:
        values = ["/agg/x", "", "rel/agg", "/agg/" + rng.choice(NAMES)]
        if not string_paths:
            values += [None, 0, 7, [], ["/a"], {"p": 1}, True]
        entry["path"] = rng.choice(values)
    if rng.random() < 0.6:
        entry["branch"] = rng.choice(("trio/loop--x", "", None) + (() if string_paths else (5, [])))
    if rng.random() < 0.3:
        entry["main"] = "/main"
    if rng.random() < 0.2:
        entry["extra"] = {"nested": [1, 2]}
    return entry


def _table(rng: random.Random, string_paths: bool):
    return {rng.choice(NAMES + ("unknown", "APP")): _entry(rng, string_paths)
            for _ in range(rng.randint(0, 4))}


def _repos(rng: random.Random) -> list[dict]:
    out = []
    for name in rng.sample(("app-backend", "svc", "x"), rng.randint(0, 3)):
        out.append({"name": name, "path": Path(f"/declared/{name}"), "raw_path": name,
                    "base": rng.choice(("dev", None, "main"))})
    return out


def _check_apply(loop: Path, raw: str, repos: list[dict]) -> None:
    snapshot = [dict(r) for r in repos]
    got = TM.apply_aggregates(loop, repos)
    assert repos == snapshot  # the input list is never mutated
    assert isinstance(got, list) and len(got) == len(repos)
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    table = data.get("repos") if isinstance(data, dict) else None
    for old, new in zip(repos, got):
        entry = table.get(old["name"]) if isinstance(table, dict) else None
        well_formed = (isinstance(entry, dict) and isinstance(entry.get("path"), str)
                       and entry["path"].strip())
        if not well_formed:
            assert new == old, (raw, old, new)
            continue
        assert new["path"] == Path(entry["path"]) and new["main_path"] == old["path"]
        assert new["name"] == old["name"]
        branch = entry.get("branch")
        branch_ok = isinstance(branch, str) and branch.strip()
        assert new["base"] == (branch if branch_ok else old["base"])


@pytest.mark.parametrize("seed", SEEDS)
def test_apply_aggregates_never_raises_on_garbage(seed, tmp_path):
    rng = random.Random(seed)
    loop = tmp_path / "loop"
    target = loop.joinpath(*RF.AGGREGATES_FILE)
    target.parent.mkdir(parents=True)
    for _ in range(CASES):
        raw = _garbage(rng, string_paths=True)
        if rng.random() < 0.05:
            target.write_bytes(b"\xff\xfe\x00garbage")
            raw = "{"
        else:
            target.write_text(raw, encoding="utf-8")
        _check_apply(loop, raw, _repos(rng))
    target.unlink()
    repos = _repos(rng)
    assert TM.apply_aggregates(loop, repos) is repos  # no map: unchanged


@pytest.mark.parametrize("seed", SEEDS)
def test_apply_aggregates_never_raises_on_wrong_typed_paths(seed, tmp_path):
    rng = random.Random(seed)
    loop = tmp_path / "loop"
    target = loop.joinpath(*RF.AGGREGATES_FILE)
    target.parent.mkdir(parents=True)
    for _ in range(CASES):
        raw = _garbage(rng, string_paths=False)
        target.write_text(raw, encoding="utf-8")
        _check_apply(loop, raw, _repos(rng))

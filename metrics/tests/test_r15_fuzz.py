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


# --- eval-r15: property assertions (not only never-raises) ------------------

import importlib.machinery
import importlib.util
import os
import subprocess

_GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x.invalid")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=_GIT_ENV).stdout.strip()


def _load_trio_check():
    path = Path(trio_loop.__file__).with_name("trio-check.py")
    loader = importlib.machinery.SourceFileLoader("trio_check_fuzz", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


TC = _load_trio_check()
CLONES = ("app-backend", "app-frontend", "svc", "lib-a")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A home repo with a mailbox and four real clones (branches main+dev),
    one outside the home; plus a plain directory that is not a repo."""
    base = tmp_path_factory.mktemp("r15fuzz")
    home = base / "home"
    (home / "loop").mkdir(parents=True)
    _git(home, "init", "-q", "-b", "main")
    _git(home, "commit", "-q", "--allow-empty", "-m", "init")
    paths = {}
    for name in CLONES:
        path = (base / "elsewhere" / name) if name == "lib-a" else (home / name)
        path.mkdir(parents=True)
        _git(path, "init", "-q", "-b", "main")
        _git(path, "commit", "-q", "--allow-empty", "-m", "init")
        _git(path, "branch", "dev")
        raw = str(path) if name == "lib-a" else name
        paths[name] = (raw, path.resolve())
    (home / "plain").mkdir()
    return home, paths


def _render_repos(rng, entries):
    """A `repos:` fence in a random but valid spelling of *entries*."""
    lines = ["```yaml", "repos:"]
    if rng.random() < 0.3:
        lines.append("  # comment")
    for name, raw, base in entries:
        fields = [("name", name), ("path", raw)] + ([("base", base)] if base else [])
        rng.shuffle(fields)
        quote = rng.choice(("", "'", '"'))
        if rng.random() < 0.3:
            body = ", ".join(f"{k}: {quote}{v}{quote}" for k, v in fields)
            lines.append(f"  - {{{body}}}")
        else:
            lines.append(f"  - {fields[0][0]}: {quote}{fields[0][1]}{quote}")
            lines += [f"    {k}: {quote}{v}{quote}" + (" # c" if rng.random() < 0.2 else "")
                      for k, v in fields[1:]]
    lines.append("```")
    return "\n".join(lines) + "\n"


def _canonical(repos):
    return [(r["name"], Path(r["path"]), r["base"]) for r in repos]


@pytest.mark.parametrize("seed", SEEDS)
def test_repos_block_round_trip(seed, world):
    """parse(render(x)) == x, and parse(render(parse(t))) == parse(t)."""
    home, paths = world
    rng = random.Random(seed)
    for _ in range(150):
        chosen = rng.sample(CLONES, rng.randint(1, len(CLONES)))
        entries = [(n, paths[n][0], rng.choice((None, "main", "dev"))) for n in chosen]
        text = _render_repos(rng, entries)
        repos, errors = TM.parse_repos_block(text, home)
        assert errors == [], text
        want = [(n, paths[n][1], b) for n, _raw, b in entries]
        assert _canonical(repos) == want, text
        again, errors2 = TM.parse_repos_block(
            _render_repos(rng, [(r["name"], str(r["path"]), r["base"]) for r in repos]), home)
        assert errors2 == [] and _canonical(again) == _canonical(repos)


MUTATIONS = {
    "duplicate name": lambda e: e + [(e[0][0], e[0][1], None)],
    "same path twice": lambda e: e + [("other-name", e[0][1], None)],
    "bad name": lambda e: [("Bad_Name", e[0][1], None)] + e[1:],
    "env var path": lambda e: [(e[0][0], "$HOME/x", None)] + e[1:],
    "missing path": lambda e: [(e[0][0], "does-not-exist", None)] + e[1:],
    "not a repo": lambda e: [(e[0][0], "plain", None)] + e[1:],
    "home elsewhere": lambda e: e + [("home", e[0][1], None)],
    "mailbox repo": lambda e: e + [("second-home", ".", None)],
}


@pytest.mark.parametrize("seed", SEEDS)
def test_invalid_repos_blocks_always_refuse(seed, world, tmp_path):
    """Every invalid block is refused by the parser, the r15 guard and the
    SHIP retirement gate (eval-r15 B1: never fails open)."""
    home, paths = world
    box = home / "loop"
    rng = random.Random(seed)
    for _ in range(40):
        chosen = rng.sample(CLONES, rng.randint(1, len(CLONES)))
        entries = [(n, paths[n][0], None) for n in chosen]
        kind = rng.choice(sorted(MUTATIONS))
        text = _render_repos(rng, MUTATIONS[kind](entries))
        _repos, errors = TM.parse_repos_block(text, home)
        assert errors, (kind, text)
        (box / "PLAN.md").write_text("# PLAN\n\n" + text)
        assert TC.repo_scope_refusals(box, TM), (kind, text)
        pins = {n: "a" * 40 for n in chosen}
        _declared, problem = trio_loop._pinned_repos_problem(box, pins)
        assert problem and "does not validate" in problem, (kind, text)
        state = {"evaluated_repos": " ".join(f"{n}@{'a' * 40}" for n in chosen)}
        kind_, _detail = trio_loop._declared_repos_retirement_problem(box, 1, "", state)
        assert kind_ == trio_loop.RETIREMENT_FINAL


@pytest.mark.parametrize("seed", SEEDS)
def test_full_check_mapping_round_trip(seed):
    rng = random.Random(seed)
    names = ("home", "app-backend", "app-frontend", "svc")
    cmds = ("python3 -m pytest -q", "npx vitest run", "make test && make lint", "true")
    for _ in range(CASES // 4):
        want = {n: rng.choice(cmds) for n in rng.sample(names, rng.randint(1, len(names)))}
        if rng.random() < 0.4:
            body = ", ".join(f'{k}: "{v}"' for k, v in want.items())
            text = f"full_check: {{ {body} }}\n"
        else:
            rows = []
            for k, v in want.items():
                rows.append(f"  {k}: {v}")
                if rng.random() < 0.3:
                    rows.append("")  # eval-r15 N4: blank lines inside the mapping
            text = "full_check:\n" + "\n".join(rows) + "\n\nfull_check_budget_s: 60\n"
        assert TM.parse_full_check(text) == (want, []), text
        cmd = rng.choice(cmds)
        assert TM.parse_full_check(f"full_check: {cmd}\n\n  prose\n") == ({"home": cmd}, [])


@pytest.mark.parametrize("seed", SEEDS)
def test_retired_parse_render_parse_round_trip(seed):
    rng = random.Random(seed)
    for _ in range(CASES // 4):
        text, _good = _retired_text(rng)
        entries = TM.parse_retired(text.splitlines(), errors=[])
        rendered = "retired:\n" + "".join(
            f"  - slice: {e['slice']}\n" + (f"    repo: {e['repo']}\n" if e.get("repo") else "")
            + f"    sha: {e['sha']}\n    at: {e['at']}\n" for e in entries
        )
        again = TM.parse_retired(rendered.splitlines(), errors=(errs := []))
        assert errs == []
        key = lambda es: [(e["slice"], e.get("repo") or None, e["sha"], e["at"]) for e in es]  # noqa: E731
        assert key(again) == key(entries), rendered


@pytest.mark.parametrize("seed", SEEDS)
def test_driver_and_check_queue_agree_on_retired_repo(seed, world):
    """eval-r15 N1: the driver holds a retired entry for its `repo:` exactly
    when trio-check's check_queue flags it (PLAN.md authoritative, an
    omitted `repo:` is home)."""
    home, paths = world
    box = home / "loop"
    rng = random.Random(seed)
    declared = ["app-backend", "svc"]
    block = "".join(f"  - name: {n}\n    path: {paths[n][0]}\n" for n in declared)
    heads = {n: _git(paths[n][1], "rev-parse", "HEAD") for n in declared}
    values = (None, ".", "home", "app-backend", "svc", "ghost")
    for _ in range(60):
        slices = []
        for i in range(rng.randint(1, 4)):
            slices.append((f"s{i}", rng.choice((None, "home", ".", "app-backend", "svc"))))
        rows = "".join(
            f"  - id: {sid}\n" + (f"    repo: {r}\n" if r else "") + f"    writes: [x{sid}.py]\n"
            for sid, r in slices
        )
        (box / "PLAN.md").write_text(
            "# PLAN\n\n```yaml\nrepos:\n" + block + "```\n\n```yaml\nslices:\n" + rows + "```\n")
        entries = []
        for sid, plan_repo in slices:
            repo = rng.choice(values)
            owner = trio_loop._repo_value(repo)
            sha = heads[owner] if owner in heads else "b" * 40
            entries.append((sid, repo, sha))
        (box / "QUEUE.md").write_text("```yaml\nretired:\n" + "".join(
            f"  - slice: {sid}\n" + (f"    repo: {r}\n" if r else "")
            + f"    sha: {sha}\n    at: 2026-09-28T00:00:00Z\n" for sid, r, sha in entries
        ) + "```\n")
        parsed = TM.parse_slices_block((box / "PLAN.md").read_text())
        errors = TC.check_queue(box, TM, parsed)
        flagged = {sid for sid, _r, _s in entries if any(f"slice {sid!r}" in e and "repo" in e for e in errors)}
        held = {e.get("slice") for e, _msg in trio_loop._retired_repo_problems(
            box, TM.read_queue(box))}
        assert held == flagged, (slices, entries, errors)

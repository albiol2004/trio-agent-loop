"""eval-r16rc-b G1 guard: bypasses (L4) and false-positive probes (L5).

The G1 guard (`repo_scope_refusals` -> `_mailbox_dir_write`) refuses a home
slice whose `writes:` fall at/under the mailbox directory -- files there are
Lead work, never a builder slice (metrics/trio-check.py `mailbox_dir_message`).
Two gaps let a slice's `writes:` reach the mailbox and slip past it:

* L4: an undeclared, path-like `repo:` that resolves (relative to the
  mailbox dir, falling back to the home repo root -- the same fallback
  `_slice_offending_path` already uses for a pre-r15 `repo:`) to the home
  repo root itself, or into the mailbox dir, was not recognised as a home
  slice at all (`repo: ./`, `repo: ../..`, `repo: scripts` when `scripts`
  is a mailbox subdirectory).
* L5: a write that covers the mailbox from *above* (`loop`, `.`, a glob
  matching an ancestor directory of the mailbox such as `**`) was not
  caught -- only a write reaching in from *below* was.

This is the in-process, parametrised port of eval-r16rc-b's
repro/q3q4/g1_guard_cases.py (29 cases; kept in lockstep with it -- a
change here should be mirrored there and vice versa).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

CHECKER = Path(__file__).parents[1] / "trio-check.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}


def _load():
    loader = importlib.machinery.SourceFileLoader("trio_check_r16b_g1", str(CHECKER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module, module.load_trio_metrics()


TC, TM = _load()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, **GIT_ENV))


def _fixture(tmp_path: Path, name: str, slices: list[dict], *, repos_block: str = "",
             mailbox: str = "loop/x", extra=None) -> Path:
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x\n")
    git(root, "init", "-q", "-b", "main")
    box = root / mailbox if mailbox != "." else root
    (box / "briefs").mkdir(parents=True, exist_ok=True)
    (box / "scripts").mkdir(exist_ok=True)
    (root / "loop" / "xy").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "loop" / "x").mkdir(parents=True, exist_ok=True)
    if extra:
        extra(root, box)
    rows = ""
    for sl in slices:
        rows += f"  - id: {sl['id']}\n"
        if "repo" in sl:
            rows += f"    repo: {sl['repo']}\n"
        rows += f"    writes: [{sl['w']}]\n    reads: []\n    status: planned\n    accepts: [\"ok\"]\n"
        (box / "briefs" / f"{sl['id']}.md").write_text(
            f"# Task {sl['id']}\n\n## Targeted check\n\npython3 -m pytest -q\n"
        )
    head = f"```yaml\nrepos:\n{repos_block}```\n\n" if repos_block else ""
    (box / "PLAN.md").write_text(
        "# PLAN\n\n" + head + "## Verification standard\n\nmode: test-first\n\n"
        "full_check: true\n\n```yaml\nslices:\n" + rows + "```\n"
    )
    for f in ("STATE.md", "QUEUE.md", "GOAL.md"):
        (box / f).write_text("status: ready\n" if f == "STATE.md" else "#\n")
    return box


def _symlink(root: Path, box: Path) -> None:
    os.symlink("loop/x", root / "evidence")


def _clone_nested(root: Path, box: Path) -> None:
    c = box / "app-backend"
    c.mkdir()
    (c / "a.py").write_text("x\n")
    git(c, "init", "-q", "-b", "dev")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "i"],
        cwd=c, check=True, capture_output=True, env=dict(os.environ, **GIT_ENV),
    )


def _clone_outside(root: Path, box: Path) -> None:
    c = root / "clones" / "app-backend"
    c.mkdir(parents=True)
    git(c, "init", "-q", "-b", "dev")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "i"],
        cwd=c, check=True, capture_output=True, env=dict(os.environ, **GIT_ENV),
    )


# (name, slices, kwargs, expect_refuse) -- mirrors
# eval-r16rc-b/repro/q3q4/g1_guard_cases.py `cases` exactly.
CASES = [
    ("omitted", [{"id": "s", "w": "loop/x/scripts/a.py"}], {}, True),
    ("home", [{"id": "s", "repo": "home", "w": "loop/x/scripts/a.py"}], {}, True),
    ("dot", [{"id": "s", "repo": ".", "w": "loop/x/scripts/a.py"}], {}, True),
    ("dotslash", [{"id": "s", "repo": "./", "w": "loop/x/scripts/a.py"}], {}, True),
    ("repo-rel-root", [{"id": "s", "repo": "../..", "w": "loop/x/scripts/a.py"}], {}, True),
    ("repo-mailbox-subdir", [{"id": "s", "repo": "scripts", "w": "a.py"}], {}, True),
    ("dotdot-path", [{"id": "s", "w": "src/../loop/x/a.py"}], {}, True),
    ("dot-prefix", [{"id": "s", "w": "./loop/x/a.py"}], {}, True),
    ("double-slash", [{"id": "s", "w": "loop//x/a.py"}], {}, True),
    ("trailing-slash", [{"id": "s", "w": "loop/x/"}], {}, True),
    ("mailbox-itself", [{"id": "s", "w": "loop/x"}], {}, True),
    ("absolute", [{"id": "s", "w": "ROOT/loop/x/a.py"}], {}, True),
    ("symlink", [{"id": "s", "w": "evidence/a.py"}], {"extra": _symlink}, True),
    ("glob-loop-star", [{"id": "s", "w": "'loop/*'"}], {}, True),
    ("glob-x-star", [{"id": "s", "w": "'loop/x*'"}], {}, True),
    ("glob-starstar", [{"id": "s", "w": "'**'"}], {}, True),
    ("glob-star-a", [{"id": "s", "w": "'*/x/a.py'"}], {}, True),
    ("parent-loop", [{"id": "s", "w": "loop"}], {}, True),
    ("root-dot", [{"id": "s", "w": "."}], {}, True),
    ("case-LOOP", [{"id": "s", "w": "LOOP/X/a.py"}], {}, False),
    ("fp-sibling-xy", [{"id": "s", "w": "loop/xy/a.py"}], {}, False),
    ("fp-docs-loop-x", [{"id": "s", "w": "docs/loop/x/a.py"}], {}, False),
    ("fp-src", [{"id": "s", "w": "src/app.py"}], {}, False),
    ("mailbox-is-root", [{"id": "s", "w": "scripts/a.py"}], {"mailbox": "."}, False),
    ("clone-nested", [{"id": "s", "repo": "app-backend", "w": "a.py"}],
     {"repos_block": "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n",
      "extra": _clone_nested}, False),
    ("clone-nested-home-into-clone", [{"id": "s", "w": "loop/x/app-backend/a.py"}],
     {"repos_block": "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n",
      "extra": _clone_nested}, True),
    ("multi-home-mailbox", [{"id": "s", "repo": "home", "w": "loop/x/scripts/a.py"}],
     {"repos_block": "  - name: app-backend\n    path: clones/app-backend\n    base: dev\n",
      "extra": _clone_outside}, True),
    ("multi-dotslash-mailbox", [{"id": "s", "repo": "./", "w": "loop/x/scripts/a.py"}],
     {"repos_block": "  - name: app-backend\n    path: clones/app-backend\n    base: dev\n",
      "extra": _clone_outside}, True),
    ("multi-clone-outside", [{"id": "s", "repo": "app-backend", "w": "a.py"}],
     {"repos_block": "  - name: app-backend\n    path: clones/app-backend\n    base: dev\n",
      "extra": _clone_outside}, False),
]


@pytest.mark.parametrize("name, slices, kw, want_refuse", CASES, ids=[c[0] for c in CASES])
def test_g1_guard_case(tmp_path, name, slices, kw, want_refuse):
    root = tmp_path / name
    slices = [dict(sl, w=sl["w"].replace("ROOT", str(root))) for sl in slices]
    box = _fixture(tmp_path, name, slices, **kw)
    problems = TC.repo_scope_refusals(box, TM)
    refused = bool(problems)
    assert refused == want_refuse, (name, problems)
    if refused and want_refuse:
        # A refusal from this guard (as opposed to some other r15 refusal,
        # e.g. clone-nested-home-into-clone which also happens to land
        # under the mailbox) always cites the mailbox dir.
        assert any("under the mailbox directory" in p for p in problems), problems
    # Per-dispatch checking (`run builder --isolate --worker-slice s`) must
    # agree with the whole-PLAN scan (eval-r16rc-b: same underlying
    # function on both paths).
    per_slice = TC.repo_scope_refusals(box, TM, slice_id="s")
    assert bool(per_slice) == want_refuse, (name, per_slice)

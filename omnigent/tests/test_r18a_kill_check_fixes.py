"""eval-r18a F1/F2/N1/N2 regressions for the shadow base-revert kill check.

Ported from the reviewer's adversarial matrix (eval-r18a/scratch/adv/
test_adv_kill.py, real git + real pytest; the vitest cases need a
node_modules with vitest in `R18A_NODE_MODULES` and skip otherwise), with the
fixed semantics asserted instead of xfail'd:

- F1: every changed path (tests, fixtures, mailbox files) is snapshotted
  before the revert and restored from that snapshot; a failed restore proof
  is `error` (reason `restore: ...`) and never changes the integration.
- F2: `killed` only on a runner's assertion / failed-test report; a missing
  runner or module, pytest exit 4/5, usage errors, `cd` failures and
  collection errors are `error`.
- N1: a slice that edits `.gitignore` keeps its newly ignored files.
- N2: an absolute `cd` out of the worktree is `n/a (absolute cd)`.
- r17-rc L-4: a relative `cd ../x` that resolves outside the worktree,
  `env -C <dir>`, `pushd`, and a backtick/`$(...)` cd target are also
  `n/a (leaves worktree)`.
- r18 measurement: `_brief_targeted_command` extracts the real command from
  a `## Targeted check` section, never the TARGETED_CHECK output-format
  instruction's own backtick-quoted placeholder examples (built from the
  live openrouter Q18-r1 briefs, `.../speed/hard/q-measure/runs/openrouter/
  Q18-r1/repo/loop-hard/tasks/or-{view,map,api}.md`, read-only).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, World, git, init_repo, load

T = load("trioctl_r18a_kc_fixes", REPO_ROOT / "omnigent" / "trioctl")
PY = sys.executable
PYT = f"{PY} -m pytest -q -p no:cacheprovider"
CALC = {"calc.py": "def add(a, b):\n    return a - b\n", "README.md": "r\n"}
NODE_MODULES = os.environ.get("R18A_NODE_MODULES", "")
needs_vitest = pytest.mark.skipif(
    not NODE_MODULES or not (Path(NODE_MODULES) / "vitest").is_dir(),
    reason="R18A_NODE_MODULES with vitest not provided",
)


def repo_at(tmp: Path, files: dict[str, str]) -> tuple[Path, str]:
    r = tmp / "wt"
    init_repo(r, "main", files, metrics=False)
    return r, git(r, "rev-parse", "HEAD")


def w(r: Path, rel: str, text: str) -> None:
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def snap(r: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for p in sorted(r.rglob("*")):
        rel = p.relative_to(r)
        if {".git", "__pycache__", ".pytest_cache"} & set(rel.parts):
            continue
        if p.is_symlink():
            out[str(rel)] = ("L", os.readlink(p))
        elif p.is_file():
            out[str(rel)] = (p.stat().st_mode & 0o777, p.read_bytes())
    return out


def kc(r: Path, base: str, cmd: str, **kw):
    before = snap(r)
    res = T.run_kill_check(r, base, cmd, **kw)
    res["_identical"] = before == snap(r)
    return res


# ------------------------------------------------------------ F2 outcomes

def test_new_symbol_import_is_error_not_killed(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import mul\n\n"
      "def test_mul():\n    assert mul(2, 3) == 6\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["_identical"] and res["outcome"] == "error" and res["collection_error"], res


def test_new_symbol_attribute_access_is_killed(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nimport calc\n\n"
      "def test_mul():\n    assert calc.mul(2, 3) == 6\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["_identical"] and res["outcome"] == "killed", res


def test_tautology_importing_new_symbol_is_not_killed(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return 0\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import mul\n\n"
      "def test_mul():\n    assert callable(mul)\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["_identical"] and res["outcome"] == "error", res


def test_cli_shellout_is_killed(tmp_path):
    r, b = repo_at(tmp_path, {"cli.py": "import sys\nprint('v1')\n"})
    w(r, "cli.py", "import sys\nprint('v2', sys.argv[1:])\n")
    w(r, "tests/test_cli.py", "import subprocess, sys\n\ndef test_cli():\n    out = subprocess.run("
      "[sys.executable, 'cli.py', 'x'], capture_output=True, text=True).stdout\n"
      "    assert out.startswith('v2')\n")
    res = kc(r, b, f"{PYT} tests/test_cli.py")
    assert res["_identical"] and res["outcome"] == "killed", res


def test_receipt_test_survives(tmp_path):
    r, b = repo_at(tmp_path, {"sql/view.sql": "select 1 from fact_salesgp;\n"})
    w(r, "sql/view.sql", "select 1 from gold;\n")
    w(r, "results/recon.txt", "STATE SUCCEEDED\n")
    w(r, "tests/test_view.py", "from pathlib import Path\n\ndef test_recon():\n"
      "    assert 'STATE SUCCEEDED' in Path('results/recon.txt').read_text()\n")
    res = kc(r, b, f"{PYT} tests/test_view.py")
    assert res["_identical"] and res["outcome"] == "survived", res


def test_pytest_file_not_found_is_error(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_add():\n    assert True\n")
    res = kc(r, b, f"{PYT} tests/test_missing.py")
    assert res["outcome"] == "error" and "not runnable" in res["reason"], res
    assert res["exit"] == 4


def test_pytest_no_tests_collected_is_error(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "X = 1\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["outcome"] == "error" and res["exit"] == 5, res


def test_missing_runner_module_is_error(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_x():\n    assert True\n")
    res = kc(r, b, f"{PY} -m no_such_runner_r18a tests/test_calc.py")
    assert res["outcome"] == "error" and "not runnable" in res["reason"], res
    assert not res.get("collection_error")


def test_relative_cd_failure_is_error(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_x():\n    assert True\n")
    res = kc(r, b, f"cd no-such-dir && {PYT} tests/test_calc.py")
    assert res["_identical"] and res["outcome"] == "error", res


def test_absolute_cd_out_of_the_worktree_is_na(tmp_path):
    agg = tmp_path / "agg"
    old = {**CALC, "tests/test_old.py": "def test_x():\n    assert True\n"}
    init_repo(agg, "main", old, metrics=False)
    r, b = repo_at(tmp_path, old)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
      "def test_add():\n    assert add(2, 3) == 5\n")
    for cmd in (f"cd {agg} && {PYT} tests", f"set -e; cd '{agg}'; {PYT} tests", f"cd ~ && {PYT} tests"):
        res = kc(r, b, cmd)
        assert res["outcome"] == "n/a" and res["reason"].startswith("absolute cd"), res
        assert T.kill_check_suffix(res) == "kill_check: n/a (absolute cd)"
        assert "tree_sha256" not in res  # nothing was reverted


def test_absolute_cd_into_the_worktree_runs(tmp_path):
    r, b = repo_at(tmp_path, {**CALC, "legacy.py": "def old():\n    return 1\n"})
    (r / "legacy.py").unlink()
    w(r, "tests/test_gone.py", "import importlib.util\n\ndef test_gone():\n"
      "    assert importlib.util.find_spec('legacy') is None\n")
    res = kc(r, b, f"cd {r} && {PY} -m pytest -q -p no:cacheprovider tests/test_gone.py "
                   "--rootdir=. -o pythonpath=.")
    assert res["_identical"] and res["restored"] and res["outcome"] == "killed", res
    assert not (r / "legacy.py").exists()


# ---------------------------------------- r17-rc L-4: more ways to leave it

def test_relative_cd_dotdot_out_of_the_worktree_is_na(tmp_path):
    agg = tmp_path / "agg"
    old = {**CALC, "tests/test_old.py": "def test_x():\n    assert True\n"}
    init_repo(agg, "main", old, metrics=False)
    r, b = repo_at(tmp_path, old)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_x():\n    assert True\n")
    for cmd in (f"cd ../{agg.name} && {PYT} tests", f"cd ../../{tmp_path.name}/{agg.name} && {PYT} tests"):
        res = kc(r, b, cmd)
        assert res["outcome"] == "n/a" and res["reason"].startswith("leaves worktree"), res
        assert T.kill_check_suffix(res) == "kill_check: n/a (leaves worktree)"
        assert "tree_sha256" not in res


def test_relative_cd_into_a_worktree_subdir_runs(tmp_path):
    # A single relative `cd` that stays inside the worktree (resolved
    # against the worktree root, since the kill check never tracks a
    # sequence of `cd`s as real shell state) still runs the check.
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
      "def test_add():\n    assert add(2, 3) == 5\n")
    res = kc(r, b, f"cd . && {PYT} tests/test_calc.py")
    assert res["_identical"] and res["outcome"] == "killed", res


def test_env_dash_c_out_of_the_worktree_is_na(tmp_path):
    agg = tmp_path / "agg"
    old = {**CALC, "tests/test_old.py": "def test_x():\n    assert True\n"}
    init_repo(agg, "main", old, metrics=False)
    r, b = repo_at(tmp_path, old)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_x():\n    assert True\n")
    for cmd in (f"env -C {agg} {PYT} tests", f"env -C ../{agg.name} {PYT} tests"):
        res = kc(r, b, cmd)
        assert res["outcome"] == "n/a" and res["reason"].startswith("leaves worktree"), res
        assert T.kill_check_suffix(res) == "kill_check: n/a (leaves worktree)"


def test_pushd_relative_out_of_the_worktree_is_na(tmp_path):
    agg = tmp_path / "agg"
    old = {**CALC, "tests/test_old.py": "def test_x():\n    assert True\n"}
    init_repo(agg, "main", old, metrics=False)
    r, b = repo_at(tmp_path, old)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "def test_x():\n    assert True\n")
    res = kc(r, b, f"pushd ../{agg.name} && {PYT} tests; popd")
    assert res["outcome"] == "n/a" and res["reason"].startswith("leaves worktree"), res


def test_backtick_cd_target_is_na_not_survived(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a - b\n")  # still-broken product
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
      "def test_add():\n    assert add(2, 3) == 5\n")
    for cmd in (f"cd `pwd` && {PYT} tests/test_calc.py", f"cd $(pwd) && {PYT} tests/test_calc.py"):
        res = kc(r, b, cmd)
        # A dynamic target can't be verified to stay inside -- fail-safe
        # `n/a`, never a false `survived`, even though `pwd` here happens
        # to stay inside the worktree.
        assert res["outcome"] == "n/a" and res["reason"].startswith("leaves worktree"), res
        assert "tree_sha256" not in res


def test_git_rm_deleted_product_file(tmp_path):
    r, b = repo_at(tmp_path, {**CALC, "legacy.py": "def old():\n    return 1\n"})
    git(r, "rm", "-q", "legacy.py")
    w(r, "tests/test_gone.py", "import os\n\ndef test_gone():\n    assert not os.path.exists('legacy.py')\n")
    res = kc(r, b, f"{PYT} tests/test_gone.py")
    assert res["_identical"] and res["restored"] and res["outcome"] == "killed", res
    assert not (r / "legacy.py").exists()


def test_binary_symlink_and_mode(tmp_path):
    r, b = repo_at(tmp_path, {**CALC, "data.bin": "x"})
    (r / "data.bin").write_bytes(bytes(range(256)) * 4)
    os.symlink("calc.py", r / "link.py")
    w(r, "run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(r / "run.sh", 0o755)
    w(r, "tests/test_bin.py", "from pathlib import Path\n\ndef test_bin():\n"
      "    assert Path('data.bin').read_bytes()[:3] == bytes([0,1,2])\n"
      "    assert Path('link.py').is_symlink()\n")
    res = kc(r, b, f"{PYT} tests/test_bin.py")
    assert res["_identical"] and res["restored"] and res["outcome"] == "killed", res


def test_modified_test_is_not_reverted(tmp_path):
    base_test = ("import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
                 "def test_add():\n    assert add(1, 1) == 0\n")
    r, b = repo_at(tmp_path, {**CALC, "tests/test_calc.py": base_test})
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", base_test.replace("add(1, 1) == 0", "add(1, 1) == 2"))
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert "tests/test_calc.py" in res["tests"] and "tests/test_calc.py" not in res["reverted"]
    assert res["outcome"] == "killed" and res["_identical"], res


def test_budget_exhaustion_restores(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import time, pathlib\n\ndef test_slow():\n"
      "    pathlib.Path('scribble.txt').write_text('x')\n    time.sleep(30)\n")
    t0 = time.monotonic()
    res = kc(r, b, f"{PYT} tests/test_calc.py", budget=2)
    assert res["outcome"] == "error" and "timeout" in res["reason"], res
    assert res["restored"] and res["_identical"], res
    assert not (r / "scribble.txt").exists() and time.monotonic() - t0 < 15


# ------------------------------------------------------ F1 / N1 restore

def test_check_rewriting_a_builder_fixture_restores_the_builder_copy(tmp_path):
    """The reviewer's golden.txt repro: the check regenerates a tracked test
    fixture the builder ALSO changed; the snapshot (not the index) restores it."""
    r, b = repo_at(tmp_path, {**CALC, "tests/data/golden.txt": "base\n"})
    git(r, "add", "-A")
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/data/golden.txt", "builder golden\n")
    git(r, "add", "calc.py")  # a staged path, too: the index is never the source
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n"
      "from pathlib import Path\n\ndef test_add():\n"
      "    Path('tests/data/golden.txt').write_text('regenerated\\n')\n    assert add(2, 3) == 5\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["restored"] is True and res["_identical"], res
    assert (r / "tests/data/golden.txt").read_text() == "builder golden\n"
    assert res["outcome"] == "killed", res
    assert res["snapshot_paths"] >= 3


def test_check_rewriting_a_mailbox_file_restores_it(tmp_path):
    r, b = repo_at(tmp_path, {**CALC, "loop/x/REPORT.md": "base report\n"})
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "loop/x/REPORT.md", "builder report\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n"
      "from pathlib import Path\nPath('loop/x/REPORT.md').write_text('clobbered\\n')\n"
      "assert add(2, 3) == 5, 'add wrong'\n")
    res = kc(r, b, "python3 tests/test_calc.py", mailbox_rel="loop/x")
    assert res["restored"] is True and res["_identical"] and res["outcome"] == "killed", res
    assert (r / "loop/x/REPORT.md").read_text() == "builder report\n"
    assert all(not p.startswith("loop/x/") for p in res["reverted"] + res["tests"])


def test_check_modifying_a_clean_tracked_file_restores_the_base_copy(tmp_path):
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n"
      "assert add(2, 3) == 5, 'add wrong'\n")
    res = kc(r, b, "echo scribble >> README.md; rm -f README.md.bak; python3 tests/test_calc.py")
    assert res["restored"] is True and res["_identical"], res
    assert (r / "README.md").read_text() == "r\n"


def test_gitignore_change_keeps_newly_ignored_builder_files(tmp_path):
    r, b = repo_at(tmp_path, {**CALC, ".gitignore": "*.log\n"})
    w(r, ".gitignore", "*.log\nout/\n")
    w(r, "out/cache.json", "{\"precious\": true}\n")
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
      "def test_add():\n    assert add(2, 3) == 5\n")
    res = kc(r, b, f"{PYT} tests/test_calc.py")
    assert res["restored"] is True and res["_identical"], res
    assert (r / "out/cache.json").read_text() == "{\"precious\": true}\n"
    assert (r / ".gitignore").read_text() == "*.log\nout/\n"
    assert ".gitignore" in res["reverted"] and res["outcome"] == "killed"


def test_failed_restore_proof_is_error_restore(tmp_path, monkeypatch):
    """The safety net itself: a restore that cannot be proven is recorded as
    `error (restore)`, with the check's own outcome kept alongside."""
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n"
      "assert add(2, 3) == 5, 'add wrong'\n")
    monkeypatch.setattr(T, "_kill_restore", lambda *a, **k: ["calc.py"])
    res = T.run_kill_check(r, b, "python3 tests/test_calc.py")
    assert res["outcome"] == "error" and res["reason"].startswith("restore:"), res
    assert res["restored"] is False and res["check_outcome"] == "killed"
    assert T.kill_check_suffix(res) == "kill_check: error (restore)"


# ------------------------------------------------------------- vitest

def _vitest_repo(tmp_path, base_files):
    r, b = repo_at(tmp_path, {"package.json": '{"type":"module","private":true}\n',
                              ".gitignore": "node_modules\n", **base_files})
    os.symlink(NODE_MODULES, r / "node_modules")
    return r, b


@needs_vitest
def test_vitest_missing_module_is_error(tmp_path):
    r, b = _vitest_repo(tmp_path, {"src/old.ts": "export const x = 1\n"})
    w(r, "src/sum.ts", "export function sum(a: number, b: number) { return a + b }\n")
    w(r, "test/sum.test.ts", "import { describe, it, expect } from 'vitest'\nimport { sum } from '../src/sum'\n"
      "describe('s', () => { it('adds', () => { expect(sum(2, 3)).toBe(5) }) })\n")
    res = kc(r, b, "npx vitest run test/sum.test.ts")
    assert res["_identical"] and res["outcome"] == "error" and res.get("collection_error"), res


@needs_vitest
def test_vitest_changed_module_is_killed(tmp_path):
    r, b = _vitest_repo(tmp_path, {"src/sum.ts": "export function sum(a: number, b: number) { return a - b }\n"})
    w(r, "src/sum.ts", "export function sum(a: number, b: number) { return a + b }\n")
    w(r, "test/sum.test.ts", "import { describe, it, expect } from 'vitest'\nimport { sum } from '../src/sum'\n"
      "describe('s', () => { it('adds', () => { expect(sum(2, 3)).toBe(5) }) })\n")
    res = kc(r, b, "npx vitest run test/sum.test.ts")
    assert res["_identical"] and res["outcome"] == "killed", res


# --------------------------------------------------- classifier table

@pytest.mark.parametrize("code, output, want", [
    (1, "F\n=== FAILURES ===\nE   assert 1 == 2\n1 failed in 0.02s\n", "killed"),
    (1, "FAILED tests/test_a.py::test_x - assert 1 == 2\n", "killed"),
    (1, "Traceback...\nAssertionError: add wrong\n", "killed"),
    (1, " ❯ test/a.test.ts (1 test | 1 failed)\n   × s > adds 2ms\n Test Files  1 failed (1)\n      Tests  1 failed (1)\n", "killed"),
    (1, "FAIL src/a.test.js\n  ● s › adds\n    expect(received).toBe(expected)\nTests:       1 failed, 1 total\n", "killed"),
    (1, "--- FAIL: TestAdd (0.00s)\n    add_test.go:9: got -1\nFAIL\n", "killed"),
    (1, "not ok 1 - adds\n", "killed"),
    (1, "Error: expect(received).toEqual(expected)\n", "killed"),
    (1, " FAIL  test/a.test.ts [ test/a.test.ts ]\nError: Failed to resolve import \"../src/a\"\n Test Files  1 failed (1)\n      Tests  no tests\n", "error"),
    (1, "FAIL src/a.test.js\n  ● Test suite failed to run\n    Cannot find module '../a'\nTest Suites: 1 failed\n", "error"),
    (2, "ERROR collecting tests/test_a.py\nImportError: cannot import name 'mul'\n1 error in 0.1s\n", "error"),
    (1, "# example.com/x\n./x_test.go:5:2: undefined: Mul\nFAIL\texample.com/x [build failed]\n", "error"),
    (4, "ERROR: file or directory not found: tests/x.py\n\nno tests ran in 0.01s\n", "error"),
    (5, "no tests ran in 0.01s\n", "error"),
    (4, "ERROR: usage: pytest [options]\npytest: error: unrecognized arguments: --bogus\n", "error"),
    (2, "sh: 1: cd: can't cd to /nowhere\n", "error"),
    (127, "sh: 1: vitestx: not found\n", "error"),
    (1, "/usr/bin/python3: No module named pytest\n", "error"),
    (1, "Segmentation fault\n", "error"),
    (1, "", "error"),
    (0, "1 passed\n", "survived"),
])
def test_classifier(code, output, want):
    outcome, reason, _extra = T._kill_classify(code, output, "pytest -q")
    assert outcome == want, (outcome, reason)
    assert "killed-by-import" not in T.KILL_CHECK_OUTCOMES


# ------------------------------------------------ the isolated builder path

def isolated(tmp_path, monkeypatch, *, worker_files, check, home, env=None, driver_json=None, sid="s1",
             wrap_kill=None):
    world = World(tmp_path, monkeypatch, tag=f"r18akcfix{abs(hash(str(tmp_path))) % 10**6}")
    t = world.trioctl
    if wrap_kill is not None:
        monkeypatch.setattr(t, "run_kill_check", wrap_kill(t.run_kill_check))
    spec = world.add_loop(home, f"loop/{sid}", [{"id": sid, "write": "calc.py"}])
    box = spec["root_box"]
    (box / "briefs" / f"{sid}.md").write_text(
        f"# Task {sid}\n\n## Targeted check\n\n```\n{check}\n```\n\nPrint `TARGETED_CHECK: <counts>`.\n")
    if driver_json is not None:
        (box / ".driver.json").write_text(json.dumps(driver_json))
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", f"mailbox {sid}")

    def worker(role, config, *, prompt, workspace, **kw):
        for rel, text in worker_files.items():
            w(Path(workspace), rel, text)
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    args = t.parser().parse_args([
        "omnigent", "run", "builder", "--isolate", "--mailbox", str(box), "--worker-slice", sid,
        "--summary", f"{sid} work", "--worktree-root", str(tmp_path / "wtroot"),
        "--workspace", str(home), "--prompt-file", str(box / "briefs" / f"{sid}.md")])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = t.command_run(args)
    view = json.loads([ln for ln in out.getvalue().splitlines() if '"worker_worktree"' in ln][-1])
    return world, code, view


GOOD = {"calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "import sys; sys.path.insert(0, '.')\nfrom calc import add\n"
                              "assert add(2, 3) == 5, 'add wrong'\nprint('1 passed')\n"}


def test_isolated_golden_repro_integrates_the_builder_content(tmp_path, monkeypatch):
    home = tmp_path / "home"
    init_repo(home, "main", {**CALC, "tests/data/golden.txt": "base\n"})
    files = {**GOOD, "tests/data/golden.txt": "builder golden\n",
             "tests/test_calc.py": "from pathlib import Path\nPath('tests/data/golden.txt').write_text('regen\\n')\n"
                                   + GOOD["tests/test_calc.py"]}
    _world, code, view = isolated(tmp_path, monkeypatch, worker_files=files,
                                  check="python3 tests/test_calc.py", home=home)
    assert code == 0 and view["worker_worktree"]["state"] == "integrated", view
    assert view["kill_check"]["restored"] is True and view["kill_check"]["outcome"] == "killed"
    assert (home / "calc.py").read_text() == GOOD["calc.py"]
    assert (home / "tests/data/golden.txt").read_text() == "builder golden\n"


def test_isolated_failed_restore_proof_still_integrates(tmp_path, monkeypatch, capfd):
    """Shadow never gates: even a failed proof integrates the builder's work."""
    home = tmp_path / "home"
    init_repo(home, "main", CALC)

    def wrap(real):
        def broken(*a, **k):
            res = real(*a, **k)
            res.update(restored=False, restore_mismatch=["calc.py"], outcome="error",
                       reason="restore: forced")
            return res
        return broken

    _w, code, view = isolated(tmp_path, monkeypatch, worker_files=GOOD,
                              check="python3 tests/test_calc.py", home=home, wrap_kill=wrap)
    assert code == 0 and view["worker_worktree"]["state"] == "integrated", view
    assert view["kill_check"]["outcome"] == "error" and view["kill_check"]["restored"] is False
    assert (home / "calc.py").read_text() == GOOD["calc.py"]
    assert "restore proof FAILED" in capfd.readouterr().err


def test_isolated_driver_json_and_env_off(tmp_path, monkeypatch):
    for n, kw in enumerate(({"driver_json": {"kill_check": False}}, {"env": {"TRIO_KILL_CHECK": "false"}})):
        sub = tmp_path / f"c{n}"
        sub.mkdir()
        home = sub / "home"
        init_repo(home, "main", CALC)
        _w, code, view = isolated(sub, monkeypatch, worker_files=GOOD,
                                  check="python3 tests/test_calc.py", home=home, **kw)
        assert code == 0 and "kill_check" not in view, view
        monkeypatch.delenv("TRIO_KILL_CHECK", raising=False)


def test_concurrent_builders_isolated(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tag="r18akcfixconc")
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", {**CALC, "mul.py": "def mul(a, b):\n    return 0\n"})
    boxes = {}
    mul_files = {"mul.py": "def mul(a, b):\n    return a * b\n",
                 "tests/test_mul.py": "import sys, time; sys.path.insert(0, '.')\nfrom mul import mul\n"
                                      "time.sleep(1.5)\nassert mul(2, 3) == 6, 'mul wrong'\nprint('1 passed')\n"}
    for sid, files in (("s1", GOOD), ("s2", mul_files)):
        spec = world.add_loop(home, f"loop/{sid}", [{"id": sid, "write": list(files)[0]}])
        chk = "python3 tests/test_calc.py" if sid == "s1" else "python3 tests/test_mul.py"
        (spec["root_box"] / "briefs" / f"{sid}.md").write_text(
            f"# Task {sid}\n\n## Targeted check\n\n```\n{chk}\n```\n")
        boxes[sid] = (spec["root_box"], files)
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "mailboxes")

    def worker(role, config, *, prompt, workspace, **kw):
        sid = "s1" if "# Task s1" in prompt else "s2"
        for rel, text in boxes[sid][1].items():
            w(Path(workspace), rel, text)
        time.sleep(0.5)
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    results = {}

    def go(sid):
        box = boxes[sid][0]
        args = t.parser().parse_args([
            "omnigent", "run", "builder", "--isolate", "--mailbox", str(box), "--worker-slice", sid,
            "--summary", f"{sid} work", "--worktree-root", str(tmp_path / "wtroot"),
            "--workspace", str(home), "--prompt-file", str(box / "briefs" / f"{sid}.md")])
        with contextlib.redirect_stdout(io.StringIO()):
            results[sid] = t.command_run(args)

    threads = [threading.Thread(target=go, args=(s,)) for s in ("s1", "s2")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    recs = {r.get("slice"): r for _i, r in world.wt.list_records(home) if r}
    assert all(c == 0 for c in results.values()), results
    assert recs["s1"]["kill_check"]["outcome"] == "killed"
    assert recs["s2"]["kill_check"]["outcome"] == "killed"
    assert (home / "calc.py").read_text() == GOOD["calc.py"] and "a * b" in (home / "mul.py").read_text()


# --------------- r18 measurement: never run a TARGETED_CHECK format hint ---

# The live openrouter Q18-r1 briefs (`.../speed/hard/q-measure/runs/
# openrouter/Q18-r1/repo/loop-hard/tasks/or-{view,map,api}.md`, read-only)
# all use exactly this `## Targeted check` shape: a plain (no backticks)
# command line, followed by the canonical TARGETED_CHECK output-format
# instruction, which itself quotes several backtick-wrapped placeholder
# examples containing a space and no literal "TARGETED_CHECK" text --
# `_brief_targeted_command` used to return the first such example
# (`N passed[, M failed] in ...`) as if it were the command to run.
_TARGETED_CHECK_HINT = (
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>` after "
    "running the check (pytest: `N passed[, M failed] in ...`; vitest: "
    "` Tests  N passed | M failed`, not `Duration`; go test: `ok`/`FAIL`; "
    "otherwise `TARGETED_CHECK: PASS <n>` or `TARGETED_CHECK: FAILED "
    "<summary>`)."
)


def _live_brief(command: str) -> str:
    return f"# Task x\n\n## Targeted check\n\n{command}\n\n{_TARGETED_CHECK_HINT}\n"


@pytest.mark.parametrize("command", [
    "npx tsc --noEmit -p api && npx vitest run api/test/openrouter-view.test.ts",
    "npx tsc --noEmit -p api && npx vitest run api/test/openrouter-analytics.test.ts "
    "api/test/dashboard.test.ts",
    "npx tsc --noEmit -p api && npx vitest run api/test/openrouter-api.test.ts "
    "api/test/openrouter-route.test.ts api/test/server.test.ts",
], ids=["or-view", "or-map", "or-api"])
def test_brief_targeted_command_skips_the_format_hint_placeholders(command):
    assert T._brief_targeted_command(_live_brief(command)) == command


def test_brief_targeted_command_prefers_a_backticked_real_command_too():
    brief = (
        "# Task x\n\n## Targeted check\n\n"
        f"Run `{PYT} tests/test_calc.py`.\n\n{_TARGETED_CHECK_HINT}\n"
    )
    assert T._brief_targeted_command(brief) == f"{PYT} tests/test_calc.py"


def test_brief_targeted_command_none_when_only_the_hint_is_present():
    # No real command anywhere in the section -- must not fall back to a
    # placeholder; the caller then reports `n/a`, never `error`.
    brief = f"# Task x\n\n## Targeted check\n\n{_TARGETED_CHECK_HINT}\n"
    assert T._brief_targeted_command(brief) is None


def test_kill_check_via_live_brief_shape_is_not_error(tmp_path):
    """End to end: the same brief shape that produced `error` in the live
    run (Q18-r1) now runs the real command and reports `killed`, not
    `sh: 1: N: not found` / `error`."""
    r, b = repo_at(tmp_path, CALC)
    w(r, "calc.py", "def add(a, b):\n    return a + b\n")
    w(r, "tests/test_calc.py", "import sys; sys.path.insert(0, '.')\nfrom calc import add\n\n"
      "def test_add():\n    assert add(2, 3) == 5\n")
    brief = _live_brief(f"{PYT} tests/test_calc.py")
    command = T._brief_targeted_command(brief)
    assert command == f"{PYT} tests/test_calc.py", command
    res = kc(r, b, command)
    assert res["outcome"] == "killed", res


# ------------------------------ eval-r17fix P-1..P-4: the reviewer's table --

# Every real command from the reviewer's case table (VERDICT.md, eval-r17fix)
# that must survive plain/backticked/fenced extraction unchanged -- the
# fenced form is legitimate for ALL of them; the reviewer marks plain and
# backticked legitimate for all but the last (`tests/run.sh`, a bare
# extension-less relative path with no runner keyword: a pre-existing,
# non-blocking gap, unchanged by this repair).
_REAL_COMMANDS = [
    "npm test -- x",
    "pnpm vitest run a.test.ts",
    "uv run pytest",
    "python -m pytest",
    "bash scripts/check.sh",
    "make test",
    "node --test",
    "go test ./...",
    "cargo test",
    "./gradlew test",
    "docker compose run --rm app pytest",
    "FOO=1 pytest",
    "pytest 'tests/test_a.py::test_x[1-2]'",
    "poetry run pytest -q",
    "timeout 120 pytest -q",
    "(cd api && npx vitest run)",
    "source .venv/bin/activate && pytest",
    ". .venv/bin/activate && pytest",
    "export CI=1 && npx vitest run",
    "bundle exec rspec",
    "just test",
    "npx vitest run -t 'adds <b>'",
    "go test ./pkg/... -run TestX",
    "cd api && npm test -- x.test.ts",
    "Python3 -m pytest",
]


@pytest.mark.parametrize("command", _REAL_COMMANDS)
def test_case_table_plain_line_real_command(command):
    assert T._brief_targeted_command(_live_brief(command)) == command


@pytest.mark.parametrize("command", _REAL_COMMANDS)
def test_case_table_backticked_real_command(command):
    brief = (
        f"# Task x\n\n## Targeted check\n\nRun `{command}`.\n\n{_TARGETED_CHECK_HINT}\n"
    )
    assert T._brief_targeted_command(brief) == command


@pytest.mark.parametrize("command", _REAL_COMMANDS + ["tests/run.sh"])
def test_case_table_fenced_real_command(command):
    brief = (
        f"# Task x\n\n## Targeted check\n\n```\n{command}\n```\n\n{_TARGETED_CHECK_HINT}\n"
    )
    assert T._brief_targeted_command(brief) == command


def test_case_table_plain_bare_word_path_is_pre_existing_gap():
    # `tests/run.sh` has no leading `./` and no runner keyword: still n/a as
    # a plain line or a backticked span (pre-existing, non-blocking; only
    # the fenced form -- tested above -- is required to work).
    assert T._brief_targeted_command(_live_brief("tests/run.sh")) is None
    brief = f"# Task x\n\n## Targeted check\n\nRun `tests/run.sh`.\n\n{_TARGETED_CHECK_HINT}\n"
    assert T._brief_targeted_command(brief) is None


@pytest.mark.parametrize("brief, expect", [
    (_live_brief("npx tsc --noEmit -p api && npx vitest run "
                 "api/test/openrouter-view.test.ts"),
     "npx tsc --noEmit -p api && npx vitest run api/test/openrouter-view.test.ts"),
    (f"# Task x\n\n## Targeted check\n\n"
     f"See `N passed in 0.1s` then run `pytest -q tests/test_a.py`.\n\n{_TARGETED_CHECK_HINT}\n",
     "pytest -q tests/test_a.py"),
    (f"# Task x\n\n## Targeted check\n\n"
     f"Uses `pytest`: `python3 -m pytest -q tests/test_a.py`\n\n{_TARGETED_CHECK_HINT}\n",
     "python3 -m pytest -q tests/test_a.py"),
    (f"# Task x\n\n## Targeted check\n\n"
     f"Covers `/api/stats` via `npx vitest run api/test/s.test.ts`\n\n{_TARGETED_CHECK_HINT}\n",
     "npx vitest run api/test/s.test.ts"),
    (f"# Task x\n\n## Targeted check\n\n"
     f"From `./api` run `npx vitest run test/s.test.ts`\n\n{_TARGETED_CHECK_HINT}\n",
     "npx vitest run test/s.test.ts"),
    (f"# Task x\n\n## Targeted check\n\n"
     f"make sure the new route is covered\npytest -q tests/test_a.py\n\n{_TARGETED_CHECK_HINT}\n",
     "pytest -q tests/test_a.py"),
    (f"# Task x\n\n## Targeted check\n\n`N passed`\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n`<n>`\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n`...`\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n`TARGETED_CHECK: PASS <n>`\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n```\nTARGETED_CHECK: PASS 3\n```\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\n```\npytest -q tests/test_<slice>.py\n```\n\n{_TARGETED_CHECK_HINT}\n", None),
    (f"# Task x\n\n## Targeted check\n\npytest -q tests/test_<slice>.py\n\n{_TARGETED_CHECK_HINT}\n", None),
], ids=[
    "live-or-view", "two-ticks-placeholder-first", "two-ticks-bare-word-span-first",
    "two-ticks-path-span-first", "two-ticks-dir-span-first", "prose-then-plain-cmd",
    "placeholder-only-N-passed", "placeholder-only-n", "placeholder-only-ellipsis",
    "placeholder-TARGETED_CHECK-PASS-n", "hint-only", "fenced-output-example-only",
    "fence-w-placeholder-cmd", "plain-placeholder-cmd",
])
def test_case_table_detailed_rows(brief, expect):
    assert T._brief_targeted_command(brief) == expect


def test_real_corpus_scan_reports_zero_false_na():
    """r18a repair item 6/acceptance: re-scan the on-disk briefs the
    reviewer used (read-only; paths per VERDICT.md eval-r17fix) and confirm
    the new parser introduces no false `n/a` relative to c293c1f -- i.e. no
    brief where c293c1f found *some* candidate (even a wrong one) but the
    fixed parser now finds nothing."""
    import hashlib

    roots = [
        Path("/home/coder/workflow-lab/.runtime/parallel-worktree-isolation/speed/hard"),
        Path("/home/coder/workflow-lab/.runtime/parallel-worktree-isolation/eval-r18a"),
        Path("/home/coder/ekona/syngenta"),
    ]
    if not any(root.is_dir() for root in roots):
        pytest.skip("read-only lab corpus not present in this environment")
    paths: list[Path] = []
    for root in roots:
        if root.is_dir():
            paths.extend(root.rglob("*.md"))

    def c293_command(brief: str) -> str | None:
        try:
            lines = T._load_trio_check()[0].targeted_check_lines(brief)
        except T.TrioctlError:
            return None
        fenced: list[str] = []
        inside = False
        for ln in lines:
            if ln.strip().startswith(("```", "~~~")):
                if inside:
                    break
                inside = True
                continue
            if inside and ln.strip() and not ln.strip().startswith("#"):
                fenced.append(ln.rstrip())
        if fenced:
            return "\n".join(fenced)
        import re as _re
        for ln in lines:
            for span in _re.findall(r"`([^`]+)`", ln):
                span = span.strip()
                if "TARGETED_CHECK" in span or " " not in span:
                    continue
                return span
        for ln in lines:
            text = _re.sub(r"^\s*(?:[-*+]\s+|\$\s+)", "", ln).strip()
            if text and "TARGETED_CHECK" not in text and not text.lower().startswith("print"):
                return text
        return None

    seen: dict[str, str] = {}
    for p in paths:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if "## Targeted check" not in text:
            continue
        try:
            lines = T._load_trio_check()[0].targeted_check_lines(text)
        except T.TrioctlError:
            continue
        section = "\n".join(lines).strip()
        if not section:
            continue
        key = hashlib.sha256(section.encode()).hexdigest()
        seen.setdefault(key, text)

    assert len(seen) >= 40, f"expected the on-disk corpus to be reachable, got {len(seen)}"
    false_na = []
    for key, text in seen.items():
        old = c293_command(text)
        new = T._brief_targeted_command(text)
        if old is not None and new is None:
            false_na.append(key)
    assert false_na == [], f"{len(false_na)} brief(s) regressed to a false n/a"

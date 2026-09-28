"""r18a L2a: base-revert kill check in shadow.

Real git and real (tiny, fake) targeted checks: `killed`, `killed-by-import`,
`survived` (a receipt-style test), `n/a`, `error`, a byte-identical restore
(sha256 of every tracked/untracked non-ignored file, modes and symlinks
included, side effects of the check undone), the `run builder --isolate`
path (JSON `kill_check`, ledger record, --no-kill-check, TRIO_KILL_CHECK=0,
never changes the integration), and the root-free loop over fixture B
(multi-repo): each slice's check runs in its own repo's builder worktree,
the driver logs `kill_check:` and shows `BASE-REVERT:` to the slice-eval,
records it in `.driver.json`, and the loop still SHIPs and lands.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, World, git, init_repo, load

TRIOCTL = load("trioctl_r18a_kill", REPO_ROOT / "omnigent" / "trioctl")


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "wt"
    init_repo(repo, "main", files, metrics=False)
    return repo, git(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)


def _snapshot(repo: Path) -> dict[str, tuple[int, bytes]]:
    out = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts:
            continue
        if p.is_symlink():
            out[str(p.relative_to(repo))] = (-1, os.readlink(p).encode())
        elif p.is_file():
            out[str(p.relative_to(repo))] = (p.stat().st_mode & 0o777, p.read_bytes())
        elif p.is_dir():
            out[str(p.relative_to(repo)) + "/"] = (0, b"")
    return out


def _kill(repo: Path, base: str, command: str | None, **kw):
    before = _snapshot(repo)
    result = TRIOCTL.run_kill_check(repo, base, command, **kw)
    assert _snapshot(repo) == before, "the worktree was not restored byte-identically"
    if "tree_sha256" in result:
        assert result["restored"] is True
        assert result["tree_sha256_after"] == result["tree_sha256"]
    return result


CALC_BASE = {"calc.py": "def add(a, b):\n    return 0\n", "README.md": "r\n"}
CALC_TEST = "import sys; sys.path.insert(0, '.')\nfrom calc import add\nassert add(2, 3) == 5, 'add is wrong'\nprint('1 passed')\n"


def test_killed(tmp_path):
    repo, base = _repo(tmp_path, CALC_BASE)
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    _write(repo, "tests/test_calc.py", CALC_TEST)
    res = _kill(repo, base, "python3 tests/test_calc.py")
    assert res["outcome"] == "killed", res
    assert res["reverted"] == ["calc.py"] and res["tests"] == ["tests/test_calc.py"]
    assert res["mode"] == "shadow" and res["exit"] != 0


def test_killed_by_import(tmp_path):
    repo, base = _repo(tmp_path, {"README.md": "r\n"})
    _write(repo, "newmod.py", "VALUE = 7\n")
    _write(repo, "tests/test_newmod.py", "import sys; sys.path.insert(0, '.')\nfrom newmod import VALUE\nassert VALUE == 7\n")
    res = _kill(repo, base, "python3 tests/test_newmod.py")
    assert res["outcome"] == "killed-by-import", res
    assert not (repo / "newmod.py").read_text() == ""  # restored


def test_survived_receipt_style_test(tmp_path):
    """W3: the test greps a receipt the same agent wrote; reverting the DDL
    changes nothing."""
    repo, base = _repo(tmp_path, {"sql/view.sql": "select 1 from fact_salesgp;\n"})
    _write(repo, "sql/view.sql", "select 1 from gold_salesgp;\n")
    _write(repo, "results/recon.txt", "STATE SUCCEEDED\n")
    _write(repo, "tests/test_view.py",
           "t = open('results/recon.txt').read()\nassert 'STATE SUCCEEDED' in t\n")
    res = _kill(repo, base, "python3 tests/test_view.py")
    assert res["outcome"] == "survived", res
    assert res["reverted"] == ["sql/view.sql"]  # results/ is never product


@pytest.mark.parametrize("files, command, reason", [
    ({"tests/test_x.py": "assert True\n"}, "python3 tests/test_x.py", "no non-test product file"),
    ({"calc.py": "def add(a, b):\n    return a + b\n"}, "python3 -c 1", "changed no test file"),
    ({"calc.py": "x\n", "tests/test_x.py": "assert True\n"}, None, "no `## Targeted check`"),
])
def test_not_applicable(tmp_path, files, command, reason):
    repo, base = _repo(tmp_path, CALC_BASE)
    for rel, text in files.items():
        _write(repo, rel, text)
    res = _kill(repo, base, command)
    assert res["outcome"] == "n/a" and reason in res["reason"], res


def test_error_on_timeout_and_unrunnable(tmp_path):
    repo, base = _repo(tmp_path, CALC_BASE)
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    _write(repo, "tests/test_calc.py", CALC_TEST)
    res = _kill(repo, base, "sleep 30; echo late > late.txt", budget=1)
    assert res["outcome"] == "error" and "timeout" in res["reason"], res
    assert not (repo / "late.txt").exists()
    res = _kill(repo, base, "no-such-tool-r18a --run")
    assert res["outcome"] == "error" and "not runnable" in res["reason"], res


def test_restore_is_byte_identical_with_side_effects(tmp_path):
    repo, base = _repo(tmp_path, {**CALC_BASE, "old.py": "gone = 1\n", "tool.sh": "#!/bin/sh\n"})
    os.chmod(repo / "tool.sh", 0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mode")
    base = git(repo, "rev-parse", "HEAD")
    _write(repo, "calc.py", "def add(a, b):\n    return a + b\n")
    (repo / "old.py").unlink()                          # deleted by the slice
    _write(repo, "pkg/deep/new.py", "N = 1\n")           # new, in a new directory
    os.symlink("calc.py", repo / "alias.py")             # new symlink
    os.chmod(repo / "tool.sh", 0o644)                    # mode change (exec bit)
    _write(repo, "tests/test_calc.py", CALC_TEST)
    _write(repo, "notes.txt", "untracked scratch\n")     # untracked: product too
    # The check also scribbles: modifies a clean tracked file, creates files.
    cmd = ("echo scribble >> README.md; echo x > created.txt; mkdir -p made && echo y > made/f; "
           "python3 tests/test_calc.py")
    res = _kill(repo, base, cmd)
    assert res["outcome"] == "killed", res
    assert set(res["reverted"]) == {"calc.py", "old.py", "pkg/deep/new.py", "alias.py",
                                    "tool.sh", "notes.txt"}
    assert (repo / "README.md").read_text() == "r\n"
    assert not (repo / "old.py").exists() and not (repo / "created.txt").exists()
    assert os.path.islink(repo / "alias.py")
    assert (repo / "tool.sh").stat().st_mode & 0o777 == 0o644


# ------------------------------------------------ the isolated builder path

def _isolated(tmp_path, monkeypatch, *, worker_files, brief_check, extra=(), env=None):
    world = World(tmp_path, monkeypatch, tag=f"r18akc{abs(hash(tmp_path)) % 10**6}")
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", CALC_BASE)
    spec = world.add_loop(home, "loop/x", [{"id": "s1", "write": "calc.py"}])
    box = spec["root_box"]
    (box / "briefs" / "s1.md").write_text(
        "# Task s1\n\nFix add.\n\n## Targeted check\n\n```\n" + brief_check + "\n```\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "mailbox")

    def worker(role, config, *, prompt, workspace, **kw):
        for rel, text in worker_files.items():
            _write(Path(workspace), rel, text)
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    args = t.parser().parse_args([
        "omnigent", "run", "builder", "--isolate", "--mailbox", str(box),
        "--worker-slice", "s1", "--summary", "s1 work",
        "--worktree-root", str(tmp_path / "wtroot"), "--workspace", str(home),
        "--prompt-file", str(box / "briefs" / "s1.md"), *extra,
    ])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = t.command_run(args)
    view = json.loads([ln for ln in out.getvalue().splitlines() if '"worker_worktree"' in ln][-1])
    return world, home, code, view


def test_isolated_builder_records_kill_check_and_still_integrates(tmp_path, monkeypatch):
    world, home, code, view = _isolated(
        tmp_path, monkeypatch,
        worker_files={"calc.py": "def add(a, b):\n    return a + b\n",
                      "tests/test_calc.py": CALC_TEST},
        brief_check="python3 tests/test_calc.py",
    )
    assert code == 0 and view["worker_worktree"]["state"] == "integrated"
    kc = view["kill_check"]
    assert kc["outcome"] == "killed" and kc["restored"] is True, kc
    # The merged slice is the builder's tree, not the reverted one.
    assert (home / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    rec = world.wt.load_record(home, view["worker_worktree"]["id"])
    assert rec["kill_check"]["outcome"] == "killed"


def test_survived_never_changes_the_integration(tmp_path, monkeypatch):
    world, home, code, view = _isolated(
        tmp_path, monkeypatch,
        worker_files={"calc.py": "def add(a, b):\n    return a + b\n",
                      "tests/test_calc.py": "assert 'def add' in open('calc.py').read()\n"},
        brief_check="python3 tests/test_calc.py",
    )
    assert view["kill_check"]["outcome"] == "survived"
    assert code == 0 and view["worker_worktree"]["state"] == "integrated"


@pytest.mark.parametrize("extra, env", [(("--no-kill-check",), None), ((), {"TRIO_KILL_CHECK": "0"})])
def test_kill_check_can_be_disabled(tmp_path, monkeypatch, extra, env):
    world, home, code, view = _isolated(
        tmp_path, monkeypatch,
        worker_files={"calc.py": "def add(a, b):\n    return a + b\n",
                      "tests/test_calc.py": CALC_TEST},
        brief_check="python3 tests/test_calc.py", extra=extra, env=env,
    )
    assert code == 0 and "kill_check" not in view


def test_driver_json_off_switch_is_honoured(tmp_path, monkeypatch):
    t = load("trioctl_r18a_kc_off", REPO_ROOT / "omnigent" / "trioctl")
    box = tmp_path / "box"
    box.mkdir()
    (box / ".driver.json").write_text(json.dumps({"kill_check": False}))
    args = t.parser().parse_args(["omnigent", "run", "builder", "--isolate",
                                  "--mailbox", str(box), "--worker-slice", "s"])
    assert t._kill_check_enabled(args) is False
    (box / ".driver.json").write_text(json.dumps({}))
    assert t._kill_check_enabled(args) is True
    monkeypatch.setenv("TRIO_KILL_CHECK", "0")
    runner = t.OmnigentRunner(repo=tmp_path)
    assert runner.driver_meta.get("kill_check") is False


# ------------------------------------------- root-free loop over fixture B

def test_root_free_multi_repo_fixture_b(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tag="r18akcb")
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", {
        ".gitignore": "loop/x/app-backend/\n", "README.md": "home\n", "docs/index.md": "d\n",
    })
    box = home / "loop" / "x"
    init_repo(box / "app-backend", "dev", {"app/core.py": "X = 1\n"}, metrics=False)
    repos_block = "  - name: app-backend\n    path: loop/x/app-backend\n    base: dev\n"
    slices = [
        {"id": "be-a", "repo": "app-backend", "write": "app/core.py"},
        {"id": "home-c", "repo": "home", "write": "docs/c.md"},
    ]
    spec = world.add_loop(home, "loop/x", slices, repos_block=repos_block,
                          full_check="full_check:\n  app-backend: true\n  home: true")
    (spec["root_box"] / "briefs" / "be-a.md").write_text(
        "# Task be-a\n\n## Targeted check\n\n`python3 tests/test_core.py`\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n")
    seen_workspaces: dict[str, str] = {}

    def worker(role, config, *, prompt, workspace, **kw):
        sid = "be-a" if "# Task be-a" in prompt else "home-c"
        seen_workspaces[sid] = str(workspace)
        if sid == "be-a":
            _write(Path(workspace), "app/core.py", "X = 2\n")
            _write(Path(workspace), "tests/test_core.py",
                   "import sys; sys.path.insert(0, '.')\nfrom app.core import X\nassert X == 2\n")
        else:
            _write(Path(workspace), "docs/c.md", "c\n")
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    code = world.run_loop(spec)
    assert code == 0, (home / "loop/x/LOG.md").read_text()
    # be-a's kill check ran in the app-backend builder worktree, not home.
    assert "app-backend" in seen_workspaces["be-a"]
    log = (home / "loop/x/LOG.md").read_text()
    assert "retired slice be-a @" in log and "by builder | kill_check: killed (shadow)" in log, log
    assert "retired slice home-c @" in log and "kill_check: n/a (shadow)" in log, log
    evals = [e for e in world.events if e["kind"] == "slice-eval"]
    be = next(e for e in evals if e["ctx"]["slice"] == "be-a")
    assert "BASE-REVERT: killed" in be["prompt"] and "AUTHORED-BY: builder" in be["prompt"]
    # Root-free is unaffected: root clean, landed, product merged.
    assert git(home, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert git(box / "app-backend", "show", "dev:app/core.py") == "X = 2"
    records = [r for _i, r in world.wt.list_records(box / "app-backend") if r]
    assert any((r.get("kill_check") or {}).get("outcome") == "killed" for r in records)


def test_trio_shadow_prints_the_recorded_kill_check(tmp_path):
    import subprocess
    import sys
    home = tmp_path / "home"
    init_repo(home, "main", {"src/a.py": "a\n"}, metrics=False)
    box = home / "loop"
    box.mkdir()
    (box / "PLAN.md").write_text(
        "# PLAN\n\n```yaml\nslices:\n  - id: s1\n    writes: [src/a.py]\n    reads: []\n```\n")
    shadow = REPO_ROOT / "metrics" / "trio-shadow.py"
    plain = subprocess.run([sys.executable, str(shadow), "--mailbox", str(box), "--json"],
                           capture_output=True, text=True, check=True)
    assert "quality" not in json.loads(plain.stdout)["slices"][0]  # nothing recorded: unchanged
    (box / ".driver.json").write_text(json.dumps({"quality": {"s1@abcdef123456": {
        "slice": "s1", "sha": "abcdef1234567890", "authored_by": "builder",
        "kill_check": {"outcome": "survived"}}}}))
    out = subprocess.run([sys.executable, str(shadow), "--mailbox", str(box)],
                         capture_output=True, text=True, check=True).stdout
    assert "quality (r18a shadow): @abcdef123456, kill_check: survived, by builder" in out
    data = json.loads(subprocess.run([sys.executable, str(shadow), "--mailbox", str(box), "--json"],
                                     capture_output=True, text=True, check=True).stdout)
    assert data["slices"][0]["quality"][0]["kill_check"]["outcome"] == "survived"

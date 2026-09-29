"""r19 eval-r19e repairs (round 5): the eval-r19e repros, inverted.

- finding 1 (H1): ``-P`` only for PACK code; a `run` that starts the
  PRODUCT (``python3 app.py``, ``python3 template/build/test_x.py``) runs
  exactly as its users run it. The F2 script-dir attack stays closed.
- finding 2: ``expect`` patterns see the full stream (stdout / stderr /
  ``output`` = both, up to MATCH_BYTES each), never the excerpt tail.
- finding 3: the check's own interpreter runs pack code with ``-s`` (no
  user site; a planted ``.pth`` does not reach it); children keep user
  site.
- finding 4: ``-c`` runs and ``-m`` of a pack (``acceptance/lib``) module get
  ``-P -s``; ``-m <product>`` does not; ``-m acceptance.…`` is refused.
- finding 5 (H2): combined short options (``-um pkg``, ``-Bc code``, ``-W
  x``, ``-X y``, ``--``) are parsed like CPython does.
- finding 6: ``pack_symlinks`` / ``symlinks_at`` (and so ``human_amend``)
  skip the pin's exclusions (``node_modules`` ...).
- finding 7: ``copy_tree`` skips unreadable entries with a warning.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T
from metrics.tests import test_r19_review_fixes_r3 as R3
from metrics.tests import test_r19_review_fixes_r4 as R4

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
TA = trio_loop._load_sibling("ta_r19_fix5", "trio-acceptance.py")
_chk = R3._chk


def _pack(tmp_path, scripts, checks):
    return R3._pack(tmp_path, {"AUTHOR.md": "# a\n", **scripts}, checks)

PATH = TA.check_path()
HAS_P = TA._supports_safe_path(shutil.which("python3", path=PATH) or "python3")
FLAGS = ["-P", "-s"] if HAS_P else ["-s"]


@pytest.fixture(params=["none", "bwrap"])
def sandbox(request, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none" if request.param == "none" else "auto")
    if request.param == "bwrap" and TA.sandbox_mode() != "bwrap":
        pytest.skip("bwrap does not work on this host")
    return request.param


@pytest.fixture(params=["none", "bwrap"])
def loop_sandbox(request, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, request.param)
    if request.param == "bwrap":
        monkeypatch.setenv(TA.SANDBOX_ENV, "auto")
        if TA.sandbox_mode() != "bwrap":
            pytest.skip("bwrap does not work on this host")
    return request.param


def _by(result):
    return {r["id"]: r for r in result["results"]}


# ----------------------------------------------------------------- H1


def author_direct(export: Path) -> None:
    acc = export / "acceptance"
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    checks = []
    for k in range(1, T.N_CHECKS + 1):
        checks.append({"id": f"ACC-{k:02d}", "goal_ref": f"GOAL.md:{k + 2}",
                       "goal_quote": f"feature f{k} works", "kind": "behaviour",
                       "surface": "cli", "run": ["python3", "app.py", f"f{k}"],
                       "expect": {"exit": 0, "stdout": [f"^ok-f{k}$"]}, "timeout_s": 30,
                       "needs": [], "binds": [], "network": "loopback"})
    (acc / "MANIFEST.json").write_text(json.dumps(
        {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
         "checks": checks}, indent=2))
    (acc / "AUTHOR.md").write_text("# inventory\n")


class DirectPack(T.Fake):
    """Checks run the product directly (`python3 app.py fN`); the honest
    Lead splits app.py into app.py + features.py."""
    split = True

    def author(self, export, context):
        res = super().author(export, context)
        shutil.rmtree(Path(export) / "acceptance")
        author_direct(Path(export))
        return res

    def _lead(self, iteration, context):
        rc = super()._lead(iteration, context)
        if self.split:
            (self.repo / "features.py").write_text(
                f"FEATURES = {[f'f{k}' for k in range(1, 9)]!r}\n")
            (self.repo / "app.py").write_text(R4.APP_SPLIT)
            T.git_retry(self.repo, "add", "-A", "--", "app.py", "features.py")
            T.git_retry(self.repo, "commit", "-qm", f"slice(cli): split module {iteration}")
            if self.mode == "open-loop":
                T.append_retired(self.mb, T.git(self.repo, "rev-parse", "HEAD"), iteration)
        return rc


@pytest.mark.parametrize("mode", ["lockstep", "open-loop"])
def test_H1_direct_product_script_with_sibling_import_ships(tmp_path, mode, loop_sandbox):
    repo, mb = T.make_repo(tmp_path, queue=(mode == "open-loop"))
    fake = DirectPack(repo, mb, mode=mode, lead_script=[{}] * 6,
                      eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake, mode=mode)
    lg = T.log(mb)
    assert "frozen 8 check(s)" in lg, lg[-2000:]
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "SHIP gate: 8/8 PASS" in lg and "ModuleNotFoundError" not in lg


def test_C1_direct_product_script_no_split_ships(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = DirectPack(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    fake.split = False
    code = T.run(mb, fake)
    assert code == 0 and "status: shipped" in T.state(mb), T.log(mb)[-2000:]


def test_vps_style_standalone_product_test_guards_pass(tmp_path, sandbox):
    """`python3 template/build/test_tabs.py` (a product test importing a
    sibling module) passes when the product is correct, as it does when a
    user runs it; a pack script next to a shadow module still cannot
    import it (F2)."""
    repo, acc = _pack(tmp_path, {
        "checks/a.py": "import json\nraise SystemExit(0 if json.dumps(1) == 'pwned' else 1)\n",
        "checks/json.py": "def dumps(x):\n    return 'pwned'\n",
    }, [_chk("ACC-01", ["python3", "template/build/test_tabs.py"]),
        _chk("ACC-02", ["python3", "-u", "template/build/test_app_shell.py"]),
        _chk("ACC-03", ["python3", "acceptance/checks/a.py"]),
        _chk("ACC-04", ["python3", "./acceptance/checks/../checks/a.py"])])
    build = repo / "template" / "build"
    build.mkdir(parents=True)
    (build / "authboard.py").write_text("TABS = ['main', 'preview']\n")
    (build / "test_tabs.py").write_text(
        "import authboard\nraise SystemExit(0 if 'preview' in authboard.TABS else 1)\n")
    (build / "test_app_shell.py").write_text(
        "import authboard\nraise SystemExit(0 if authboard.TABS[0] == 'main' else 1)\n")
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "PASS", by
    assert by["ACC-02"]["outcome"] == "PASS", by
    assert by["ACC-03"]["outcome"] == "FAIL" and by["ACC-04"]["outcome"] == "FAIL", by


# ----------------------------------------------------------------- H2 / 4


@pytest.mark.parametrize("run,flags", [
    (["python3", "acceptance/checks/x.py"], True),
    (["python3", "-u", "acceptance/checks/x.py"], True),
    (["python3", "-W", "ignore", "acceptance/checks/x.py"], True),
    (["python3", "-Wignore", "-X", "dev", "acceptance/checks/x.py"], True),
    (["python3", "--", "acceptance/checks/x.py"], True),
    (["python3", "--check-hash-based-pycs", "always", "acceptance/x.py"], True),
    (["python3", "app.py"], False),                          # product script (H1)
    (["python3", "-u", "template/build/test_tabs.py"], False),
    (["python3", "./acceptance/../app.py"], False),
    (["python3", "-um", "pkg"], False),                      # combined -u -m (H2)
    (["python3", "-mpkg"], False),
    (["python3", "-m", "pytest"], False),
    (["python3", "-W", "ignore", "-m", "pkg"], False),
    (["python3", "-Bc", "import pkg"], True),                # -c is pack code (finding 4)
    (["python3", "-c", "print(1)"], True),
    (["python3", "-X", "dev", "-c", "print(1)"], True),
    (["python3"], False),
    (["python3", "-"], False),
    (["python3", "-V"], False),
    (["/usr/bin/env", "python3", "acceptance/checks/x.py"], False),
    (["sh", "-c", "python3 acceptance/checks/x.py"], False),
    (["node", "acceptance/checks/x.mjs"], False),
])
def test_H2_isolated_argv_table(run, flags):
    got = TA.isolated_argv(run, PATH)
    assert got == ([run[0], *FLAGS, *run[1:]] if flags else run), got


def test_H2_combined_um_module_run_works(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "__init__.py").write_text("")
    (tree / "pkg" / "__main__.py").write_text("print('ok')\n")
    for run in (["python3", "-um", "pkg"], ["python3", "-B", "-u", "-mpkg"],
                ["python3", "-W", "ignore", "-m", "pkg"]):
        argv = TA.isolated_argv(run, PATH, tree)
        proc = subprocess.run(argv, cwd=tree, capture_output=True, text=True)
        assert proc.stdout.strip() == "ok", (argv, proc.stderr)


def test_H2_combined_um_in_a_run_pack(tmp_path, sandbox):
    repo, acc = _pack(tmp_path, {}, [
        dict(_chk("ACC-01", ["python3", "-um", "pkg"]), expect={"exit": 0, "stdout": ["^ok$"]}),
        dict(_chk("ACC-02", ["python3", "-Bm", "pkg"]), expect={"exit": 0, "stdout": ["^ok$"]})])
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "__main__.py").write_text("from pkg.core import V\nprint(V)\n")
    (repo / "pkg" / "core.py").write_text("V = 'ok'\n")
    by = _by(TA.run_pack(acc, repo))
    assert all(r["outcome"] == "PASS" for r in by.values()), by


def test_c_check_is_not_shadowed_by_the_product(tmp_path, sandbox):
    """eval-r19e cshadow: a product-root json.py that exits 0 no longer
    makes a `-c` judge PASS with no product."""
    judge = ("import json, subprocess, sys; r = subprocess.run([sys.executable, 'app.py'], "
             "capture_output=True, text=True); "
             "sys.exit(0 if json.loads(r.stdout or 'null') == {'ok': True} else 1)")
    repo, acc = _pack(tmp_path, {}, [_chk("ACC-01", ["python3", "-c", judge])])
    (repo / "json.py").write_text("import os\nos._exit(0)\n")
    assert _by(TA.run_pack(acc, repo))["ACC-01"]["outcome"] == "FAIL"
    # ... and the honest product (no shadow, correct output) passes.
    (repo / "json.py").unlink()
    (repo / "app.py").write_text("print('{\"ok\": true}')\n")
    assert _by(TA.run_pack(acc, repo))["ACC-01"]["outcome"] == "PASS"


def test_m_of_a_lib_module_is_pack_code_and_product_m_is_not(tmp_path, sandbox):
    repo, acc = _pack(tmp_path, {
        "lib/accjudge.py": ("import subprocess, sys\n"
                            "o = subprocess.run([sys.executable, '-m', 'pkg'], capture_output=True,"
                            " text=True).stdout.strip()\n"
                            "print(o, sys.flags.safe_path, sys.flags.no_user_site)\n"
                            "raise SystemExit(0 if o == 'pkg-ok' else 1)\n"),
    }, [_chk("ACC-01", ["python3", "-m", "accjudge"]),
        dict(_chk("ACC-02", ["python3", "-m", "pkg"]), expect={"exit": 0, "stdout": ["pkg-ok"]})])
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "__main__.py").write_text("print('pkg-ok')\n")
    # A product module shadowing the pack's judge: the judge still runs.
    (repo / "accjudge.py").write_text("import os\nos._exit(0)\n")
    (repo / "pkg" / "__main__.py").write_text("print('pkg-bad')\n")
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "FAIL" and by["ACC-02"]["outcome"] == "FAIL", by
    (repo / "pkg" / "__main__.py").write_text("print('pkg-ok')\n")
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "PASS" and by["ACC-02"]["outcome"] == "PASS", by
    assert TA.runs_pack_code(["python3", "-m", "accjudge"], repo, False) is False
    assert TA.runs_pack_code(["python3", "-m", "pkg"], repo, True) is False


def test_m_acceptance_module_is_refused_at_validation():
    man = {"setup": [], "bindings": {}}
    errs = TA.check_errors(_chk("ACC-01", ["python3", "-um", "acceptance.checks.x"]), man)
    assert any("run a pack module by its path" in e for e in errs), errs
    assert TA.check_errors(_chk("ACC-01", ["python3", "-m", "pkg"]), man) == []


# ----------------------------------------------------------------- finding 3


PLANT = "import os; os.environ.get('ACC_ID') and os._exit(0)\n"


def _plant_user_site(home: Path) -> Path:
    site = home / ".local" / "lib" / \
        f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "zz_plant.pth").write_text(PLANT)
    return site


def test_user_site_pth_does_not_reach_the_checks_own_interpreter(tmp_path, monkeypatch,
                                                                  sandbox):
    """eval-r19e usersite: a `.pth` planted in user site AFTER the driver
    resolved its PATH. The honest subusage pack on its BASE tree is back to
    0/8 (5c5d3e3: 4/8)."""
    if subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode:
        pytest.skip("pytest is not importable")
    R4.product_subusage(tmp_path / "base", False)
    R4.pack_subusage(tmp_path / "pack" / "acceptance")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = TA.check_path()  # driver start
    _plant_user_site(home)
    result = TA.run_pack(tmp_path / "pack" / "acceptance", tmp_path / "base", path=path)
    assert result["passed"] == 0, _by(result)


def test_children_keep_user_site_and_checks_do_not(tmp_path, monkeypatch, sandbox):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    site = home / ".local" / "lib" / \
        f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    (site / "usermod_r5.py").write_text("V = 'user'\n")
    probe = ("import subprocess, sys\n"
             "own = sys.flags.no_user_site\n"
             "child = subprocess.run([sys.executable, '-c', 'import usermod_r5; "
             "print(usermod_r5.V)'], capture_output=True, text=True).stdout.strip()\n"
             "print(own, child)\nraise SystemExit(0 if own and child == 'user' else 1)\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": probe}, [
        _chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
        _chk("ACC-02", ["python3", "-c", probe]),
        dict(_chk("ACC-03", ["python3", "probe.py"]), expect={"exit": 0, "stdout": ["^0 user$"]})])
    (repo / "probe.py").write_text(
        "import sys\nimport usermod_r5\nprint(sys.flags.no_user_site, usermod_r5.V)\n")
    by = _by(TA.run_pack(acc, repo))
    assert all(r["outcome"] == "PASS" for r in by.values()), by


# ----------------------------------------------------------------- finding 2


HELP = ("import sys\n"
        "print('usage: onboard [--preview] ...')\n"
        "print('  --preview   start the preview gateway')\n"
        "for i in range(200):\n"
        "    print(f'  --opt{i:03d}   option number {i} with a long explanation')\n"
        "print('warning: deprecated flag', file=sys.stderr)\n")


def test_expect_patterns_see_the_full_stream(tmp_path, sandbox):
    """eval-r19e finding 2: a `--help` obligation early in 6+ KB of help,
    plus stderr noise at the end, PASSes; stdout and stderr are matched
    separately unless the check asks for `output`."""
    repo, acc = _pack(tmp_path, {}, [
        dict(_chk("ACC-01", ["python3", "onboard.py", "--help"]),
             expect={"exit": 0, "stdout": ["(?i)preview", r"^usage: onboard"]}),
        dict(_chk("ACC-02", ["python3", "onboard.py", "--help"]),
             expect={"exit": 0, "stdout": ["deprecated flag"]}),
        dict(_chk("ACC-03", ["python3", "onboard.py", "--help"]),
             expect={"exit": 0, "stderr": ["^warning: deprecated"]}),
        dict(_chk("ACC-04", ["python3", "onboard.py", "--help"]),
             expect={"exit": 0, "output": ["preview gateway", "deprecated flag"]}),
        dict(_chk("ACC-05", ["python3", "onboard.py", "--help"]),
             expect={"exit": 0, "stderr": ["preview"]})])
    (repo / "onboard.py").write_text(HELP)
    help_out = subprocess.run([sys.executable, str(repo / "onboard.py")], capture_output=True,
                              text=True).stdout
    assert len(help_out) > 3 * TA.EXCERPT_BYTES and help_out.index("preview") < 100
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "PASS", by
    assert by["ACC-02"]["outcome"] == "FAIL" and "stdout does not match" in by["ACC-02"]["reason"]
    assert by["ACC-03"]["outcome"] == "PASS", by
    assert by["ACC-04"]["outcome"] == "PASS", by
    assert by["ACC-05"]["outcome"] == "FAIL", by
    # The excerpt stays a short tail.
    assert len(by["ACC-02"]["excerpt"]) <= TA.EXCERPT_BYTES


def test_expect_match_is_bounded(tmp_path):
    repo, acc = _pack(tmp_path, {}, [
        dict(_chk("ACC-01", ["python3", "big.py"]), expect={"exit": 0, "stdout": ["^head$"]}),
        dict(_chk("ACC-02", ["python3", "big.py"]), expect={"exit": 0, "stdout": ["^tail$"]})])
    (repo / "big.py").write_text(
        "import sys\nprint('head')\nsys.stdout.write('x' * (%d + 10) + '\\n')\nprint('tail')\n"
        % TA.MATCH_BYTES)
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "PASS", by
    assert by["ACC-02"]["outcome"] == "FAIL", by  # beyond MATCH_BYTES (documented)


def test_expect_schema_accepts_stderr_and_output_only():
    man = {"setup": [], "bindings": {}}
    ok = dict(_chk("ACC-01", ["python3", "a.py"]),
              expect={"exit": 0, "stdout": ["a"], "stderr": ["b"], "output": ["c"]})
    assert TA.check_errors(ok, man) == []
    bad = dict(ok, expect={"exit": 0, "combined": ["a"]})
    assert any("expect must be" in e for e in TA.check_errors(bad, man))
    bad = dict(ok, expect={"exit": 0, "stderr": ["("]})
    assert any("expect.stderr regex" in e for e in TA.check_errors(bad, man))


def test_attempt_timeout_and_stray_children_still_handled(tmp_path):
    repo, acc = _pack(tmp_path, {}, [
        dict(_chk("ACC-01", ["python3", "-c", "import time; print('x', flush=True); "
                                            "time.sleep(60)"]), timeout_s=1),
        dict(_chk("ACC-02", ["sh", "-c", "sleep 60 & echo started"]), timeout_s=2)])
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "FAIL" and by["ACC-01"]["reason"] == "timeout", by
    assert by["ACC-01"]["wall_s"] < 15, by
    # A background child holding stdout open until the timeout is a FAIL
    # (as before: the check did not finish).
    assert by["ACC-02"]["outcome"] == "FAIL", by


# ----------------------------------------------------------------- finding 6


def test_node_modules_symlinks_are_outside_the_symlink_rule(tmp_path):
    acc = tmp_path / "acceptance"
    (acc / "node_modules" / ".bin").mkdir(parents=True)
    (acc / "node_modules" / "pkg").mkdir()
    (acc / "node_modules" / "pkg" / "cli.js").write_text("")
    os.symlink("../pkg/cli.js", acc / "node_modules" / ".bin" / "pkg")
    (acc / "checks" / "node_modules").mkdir(parents=True)
    os.symlink("x", acc / "checks" / "node_modules" / "y")
    os.symlink("/nonexistent", acc / "checks" / "__pycache__")
    assert TA.pack_symlinks(acc) == []
    os.symlink("x", acc / "checks" / "real_link")
    assert TA.pack_symlinks(acc) == ["checks/real_link"]


def test_symlinks_at_skips_committed_node_modules(tmp_path):
    repo = tmp_path / "repo"
    (repo / "loop" / "acceptance" / "node_modules" / ".bin").mkdir(parents=True)
    os.symlink("../pkg/cli.js", repo / "loop" / "acceptance" / "node_modules" / ".bin" / "pkg")
    (repo / "loop" / "acceptance" / "a.txt").write_text("a")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "x"], check=True)
    assert TA.symlinks_at(repo, "HEAD", "loop/acceptance") == []


def test_human_amend_is_not_refused_by_a_local_node_modules(tmp_path):
    """eval-r19e nm: an Evaluator's local `npm i` in the pack no longer
    makes `acceptance amend --human` refuse."""
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5, T.log(mb)[-2000:]
    acc = mb / "acceptance"
    nm = acc / "node_modules" / ".bin"
    nm.mkdir(parents=True)
    os.symlink("../x/cli.js", nm / "x")
    (acc / "checks" / "acc_08.py").write_text(R3.HONEST_08)
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    assert ctl.human_amend(["ACC-08"], "probe too strict") == 0
    assert "node_modules" not in T.git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--",
                                       "loop/acceptance")


# ----------------------------------------------------------------- finding 7


def test_copy_tree_skips_unreadable_files_with_a_warning(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "ok.txt").write_text("ok")
    secret = src / "d" / "secret.txt"
    secret.write_text("s")
    secret.chmod(0)
    try:
        warnings = TA.copy_tree(src, tmp_path / "dst")
        assert (tmp_path / "dst" / "ok.txt").read_text() == "ok"
        assert not (tmp_path / "dst" / "d" / "secret.txt").exists()
        assert len(warnings) == 1 and warnings[0].startswith("d/secret.txt"), warnings
        (tmp_path / "p").mkdir()
        repo, acc = _pack(tmp_path / "p", {}, [_chk("ACC-01", ["python3", "-c", "pass"])])
        bad = repo / "unreadable.bin"
        bad.write_text("x")
        bad.chmod(0)
        result = TA.run_pack(acc, repo)
        assert _by(result)["ACC-01"]["outcome"] == "PASS"
        assert any("skipped unreadable unreadable.bin" in line for line in result["log"]), \
            result["log"]
        bad.chmod(0o644)
    finally:
        secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="cannot copy tree"):
        TA.copy_tree(tmp_path / "missing", tmp_path / "dst2")

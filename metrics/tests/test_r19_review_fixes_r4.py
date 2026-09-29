"""r19 eval-r19d repairs: honest liveness.

- G3 / finding 1: the check's own Python runs with ``-P``; nothing in the
  environment changes how the PRODUCT (or pytest, `python3 -m pkg`, a
  script importing a sibling) resolves imports. The F2-style script-dir
  attack stays closed.
- G1, G2 / findings 2-3: NUL-safe, unquoted path lists (a non-ASCII fixture
  name, a space in an amended per-check file).
- finding 4: checks get a controlled PATH resolved by the driver at start.
- finding 5: per-check files are attributed by path-like mentions only.
- finding 6: under ``sandbox: none`` a prepared copy / view is re-checked
  against the masters immediately before its check runs.
- finding 7: no symlinks in the pack (freeze, amendments, pins).
- the realistic honest packs from eval-r19d score full PASS on correct
  products.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T
from metrics.tests import test_r19_review_fixes_r3 as R3

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"
TA = trio_loop._load_sibling("ta_r19_fix4", "trio-acceptance.py")


def shadow(mb):
    return subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True)


@pytest.fixture(params=["none", "bwrap"])
def sandbox(request, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none" if request.param == "none" else "auto")
    if request.param == "bwrap" and TA.sandbox_mode() != "bwrap":
        pytest.skip("bwrap does not work on this host")
    return request.param


@pytest.fixture(params=["none", "bwrap"])
def loop_sandbox(request, monkeypatch):
    """Loop tests under both sandboxes (bwrap is the production one)."""
    monkeypatch.setenv(TA.SANDBOX_ENV, request.param)
    if request.param == "bwrap":
        monkeypatch.setenv(TA.SANDBOX_ENV, "auto")
        if TA.sandbox_mode() != "bwrap":
            pytest.skip("bwrap does not work on this host")
    return request.param


_pack, _chk = R3._pack, R3._chk


# ------------------------------------------------ finding 1 (G3): isolation
# only for the check's own interpreter

APP_SPLIT = ("import sys\nfrom features import FEATURES\n"
             "arg = sys.argv[1] if len(sys.argv) > 1 else ''\n"
             "print('ok-' + arg if arg in FEATURES else 'v0')\n")


class SplitLead(T.Fake):
    """An honest Lead that splits app.py into app.py + features.py."""

    def _lead(self, iteration, context):
        rc = super()._lead(iteration, context)
        (self.repo / "features.py").write_text(f"FEATURES = {[f'f{k}' for k in range(1, 9)]!r}\n")
        (self.repo / "app.py").write_text(APP_SPLIT)
        T.git_retry(self.repo, "add", "-A", "--", "app.py", "features.py")
        T.git_retry(self.repo, "commit", "-qm", f"slice(cli): split module {iteration}")
        if self.mode == "open-loop":
            T.append_retired(self.mb, T.git(self.repo, "rev-parse", "HEAD"), iteration)
        return rc


@pytest.mark.parametrize("mode", ["lockstep", "open-loop"])
def test_G3_honest_sibling_import_product_ships(tmp_path, mode, loop_sandbox):
    repo, mb = T.make_repo(tmp_path, queue=(mode == "open-loop"))
    fake = SplitLead(repo, mb, mode=mode, lead_script=[{}] * 6,
                     eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake, mode=mode)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "SHIP gate: 8/8 PASS" in lg and "ModuleNotFoundError" not in lg
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]


PRODUCT_PROBE = r'''import os, site, sys
print("safe" if os.environ.get("PYTHONSAFEPATH") else "-",
      "nouser" if os.environ.get("PYTHONNOUSERSITE") else "-",
      "flag" if sys.flags.safe_path else "-",
      "usersite" if site.ENABLE_USER_SITE else "nousersite")
'''


def test_product_interpreters_resolve_imports_normally(tmp_path, sandbox):
    """The product runs with its script dir importable, `-m pkg` from the
    tree root works, user site stays enabled, and no isolation variable is
    inherited; the check's own interpreter still runs with -P."""
    checks = {
        "checks/a.py": ("import subprocess, sys\n"
                        "o = subprocess.run([sys.executable, 'app.py'], capture_output=True,"
                        " text=True)\nprint(o.stdout, o.stderr)\n"
                        "sys.exit(0 if o.stdout.strip() == 'ok' else 1)\n"),
        "checks/b.py": ("import subprocess, sys\n"
                        "o = subprocess.run([sys.executable, '-m', 'pkg'], capture_output=True,"
                        " text=True)\nprint(o.stdout, o.stderr)\n"
                        "sys.exit(0 if o.stdout.strip() == 'pkg-ok' else 1)\n"),
        "checks/c.py": ("import subprocess, sys\n"
                        "o = subprocess.run([sys.executable, 'probe.py'], capture_output=True,"
                        " text=True).stdout.split()\nprint(o, sys.flags.safe_path)\n"
                        "sys.exit(0 if o == ['-', '-', '-', 'usersite'] and sys.flags.safe_path"
                        " else 1)\n"),
        "checks/d.sh": "cd \"$ACC_TREE\" && python3 sub/run.py | grep -qx ok\n",
    }
    repo, acc = _pack(tmp_path, checks, [
        _chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
        _chk("ACC-02", ["python3", "-u", "acceptance/checks/b.py"]),
        _chk("ACC-03", ["python3", "acceptance/checks/c.py"]),
        _chk("ACC-04", ["sh", "acceptance/checks/d.sh"])])
    (repo / "app.py").write_text("from features import OK\nprint(OK)\n")
    (repo / "features.py").write_text("OK = 'ok'\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "__main__.py").write_text("from pkg.core import V\nprint(V)\n")
    (repo / "pkg" / "core.py").write_text("V = 'pkg-ok'\n")
    (repo / "probe.py").write_text(PRODUCT_PROBE)
    (repo / "sub").mkdir()
    (repo / "sub" / "run.py").write_text("import helper\nprint(helper.X)\n")
    (repo / "sub" / "helper.py").write_text("X = 'ok'\n")
    result = TA.run_pack(acc, repo)
    by = {r["id"]: r for r in result["results"]}
    assert all(r["outcome"] == "PASS" for r in by.values()), by


def test_python_m_pytest_inside_a_check_works(tmp_path, sandbox):
    pytest_ok = subprocess.run([sys.executable, "-c", "import pytest"],
                               capture_output=True).returncode == 0
    if not pytest_ok:
        pytest.skip("pytest is not importable by this interpreter")
    repo, acc = _pack(tmp_path, {
        "checks/t.sh": ("cd \"$ACC_TREE\" && python3 -m pytest -q -p no:cacheprovider tests "
                        ">\"$ACC_WORK/log\" 2>&1 || { tail -3 \"$ACC_WORK/log\"; exit 1; }\n"),
    }, [_chk("ACC-01", ["sh", "acceptance/checks/t.sh"]),
        _chk("ACC-02", [os.path.basename(sys.executable), "-m", "pytest", "-q",
                        "-p", "no:cacheprovider", "tests"])])
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    by = {r["id"]: r for r in TA.run_pack(acc, repo, path=TA.check_path(
        os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")))["results"]}
    assert by["ACC-01"]["outcome"] == "PASS", by["ACC-01"]
    assert by["ACC-02"]["outcome"] == "PASS", by["ACC-02"]


def test_isolated_argv_only_for_a_python_script():
    path = os.environ.get("PATH", "")
    has_p = TA._supports_safe_path(shutil.which("python3", path=path) or "python3")
    want = ["python3", "-P", "acceptance/checks/a.py"] if has_p \
        else ["python3", "acceptance/checks/a.py"]
    assert TA.isolated_argv(["python3", "acceptance/checks/a.py"], path) == want
    if has_p:
        assert TA.isolated_argv(["python3", "-u", "-W", "ignore", "x.py", "-m"], path) == \
            ["python3", "-P", "-u", "-W", "ignore", "x.py", "-m"]
    for argv in (["python3", "-m", "pytest"], ["python3", "-c", "print(1)"], ["python3"],
                 ["node", "a.mjs"], ["sh", "-c", "python3 a.py"], ["python3", "-mpytest"]):
        assert TA.isolated_argv(argv, path) == argv


def test_F2_script_dir_attack_stays_closed(tmp_path, sandbox):
    """A shadow module next to a check script is not importable by that
    check's interpreter, whatever options precede the script."""
    repo, acc = _pack(tmp_path, {
        "checks/a.py": "import json\nraise SystemExit(0 if json.dumps(1) == 'pwned' else 1)\n",
        "checks/json.py": "def dumps(x):\n    return 'pwned'\n",
    }, [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
        _chk("ACC-02", ["python3", "-u", "acceptance/checks/a.py"])])
    by = {r["id"]: r for r in TA.run_pack(acc, repo)["results"]}
    assert by["ACC-01"]["outcome"] == "FAIL" and by["ACC-02"]["outcome"] == "FAIL", by
    assert TA.file_location("checks/json.py") == ("shared", None)


# --------------------------------------------- findings 2-3 (G1, G2): paths


FIX = "réponse.json"
DATA_CHECK = r"""import json, os, subprocess, sys
cases = json.load(open(os.path.join(os.environ["ACC_DIR"], "fakes", "FIXNAME"), encoding="utf-8"))
c = cases[os.environ["ACC_ID"]]
out = subprocess.run([sys.executable, "app.py", c["arg"]], capture_output=True, text=True).stdout.strip()
print(out)
sys.exit(0 if out == c["want"] else 1)
"""


class DataPack(T.Fake):
    fix = FIX
    amend_space = False

    def author(self, export, context):
        res = super().author(export, context)
        acc = Path(export) / "acceptance"
        (acc / "fakes").mkdir(exist_ok=True)
        (acc / "fakes" / self.fix).write_text(json.dumps(
            {f"ACC-{k:02d}": {"arg": f"f{k}", "want": f"ok-f{k}"} for k in range(1, 5)}),
            encoding="utf-8")
        for k in range(1, 5):
            (acc / "checks" / f"acc_{k:02d}.py").write_text(
                DATA_CHECK.replace("FIXNAME", self.fix), encoding="utf-8")
        if self.amend_space:
            d = acc / "checks" / "ACC-08"
            d.mkdir(parents=True, exist_ok=True)
            (d / "want value.txt").write_text("ok-f8-strict\n")
            (acc / "checks" / "acc_08.py").write_text(
                "import os, subprocess, sys\n"
                "want = open(os.path.join(os.environ['ACC_DIR'], 'checks', 'ACC-08', "
                "'want value.txt')).read().strip()\n"
                "out = subprocess.run([sys.executable, 'app.py', 'f8'], capture_output=True,"
                " text=True).stdout.strip()\nprint(out)\nsys.exit(0 if out == want else 1)\n")
        return res

    def _eval(self, iteration, context):
        if self.amend_space and context.get("kind") != "slice-eval" \
                and not getattr(self, "done", False):
            self.done = True
            acc = self.mb / "acceptance"
            (acc / "checks" / "ACC-08" / "want value.txt").write_text("ok-f8\n")
            with (acc / "AMENDMENTS.md").open("a") as fh:
                fh.write(f"## ACC-08 · iter {iteration} · evaluator · t\n"
                         "goal_quote: feature f8 works\ndefect in check: over-specified\n"
                         "change: x\n")
            T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(self.repo, "commit", "-qm",
                        f"acceptance: amend ACC-08 (evaluator, iter {iteration}): over-specified")
        return super()._eval(iteration, context)


@pytest.mark.parametrize("name", [FIX, "réponse data.json"])
def test_G1_honest_nonascii_fixture_name_ships(tmp_path, name, loop_sandbox):
    repo, mb = T.make_repo(tmp_path)
    fake = DataPack(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    fake.fix = name
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "outside acceptance/" not in lg and "tamper" not in lg
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]
    chain = TA.derive_pin_chain(repo, "loop/acceptance")
    assert chain["freeze_commit"] and not chain["problems"] and not chain["pending_tamper"]


def test_G2_honest_amendment_of_space_named_percheck_file_ships(tmp_path, loop_sandbox):
    repo, mb = T.make_repo(tmp_path)
    fake = DataPack(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    fake.fix = "reponse.json"
    fake.amend_space = True
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "rejected" not in lg and "outside" not in lg, lg[-2500:]
    assert any(s.startswith("acceptance: amend ACC-08") for s in T.subjects(repo))
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]
    chain = TA.derive_pin_chain(repo, "loop/acceptance")
    assert chain["amended_ids"] == ["ACC-08"] and not chain["pending_tamper"], chain


def test_git_paths_is_nul_safe(tmp_path):
    repo = tmp_path / "r"
    T.git(tmp_path, "init", "-q", "-b", "main", str(repo))
    T.git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    (repo / "a b").mkdir()
    (repo / "a b" / "é x.txt").write_text("1")
    (repo / "plain.txt").write_text("2")
    T.git(repo, "add", "-A")
    T.git(repo, "commit", "-qm", "c")
    assert sorted(TA.git_paths(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD")) == \
        ["a b/é x.txt", "plain.txt"]
    assert sorted(trio_loop._commit_paths(repo, T.git(repo, "rev-parse", "HEAD"))) == \
        ["a b/é x.txt", "plain.txt"]


# ------------------------------------------------ finding 4: controlled PATH


def test_check_path_excludes_user_dirs_without_interpreters(tmp_path, monkeypatch, sandbox):
    planted = tmp_path / "planted-bin"
    planted.mkdir()
    tool = planted / "fakecmd"
    tool.write_text("#!/bin/sh\necho planted\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ['PATH']}")
    path = TA.check_path()
    assert str(planted) not in path.split(os.pathsep)
    assert all(d in path.split(os.pathsep) for d in TA.SYSTEM_PATH_DIRS if os.path.isdir(d))
    body = ("import os, shutil, sys\nprint(os.environ['PATH'])\n"
            "sys.exit(1 if shutil.which('fakecmd') else 0)\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": body},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"])])
    res = TA.run_pack(acc, repo)["results"][0]
    assert res["outcome"] == "PASS", res
    # A shim written into the planted dir AFTER the driver resolved its PATH
    # is never run by a check.
    shim = planted / "python3"
    shim.write_text("#!/bin/sh\n[ -n \"$ACC_ID\" ] && { echo ok; exit 0; }\n"
                    "exec /usr/bin/python3 \"$@\"\n")
    shim.chmod(0o755)
    fail = {"checks/b.py": "raise SystemExit(1)\n"}
    (tmp_path / "two").mkdir()
    repo2, acc2 = _pack(tmp_path / "two", fail,
                        [_chk("ACC-01", ["python3", "acceptance/checks/b.py"])])
    assert TA.run_pack(acc2, repo2, path=path)["results"][0]["outcome"] == "FAIL"
    # ... while an interpreter resolved there at start keeps its directory.
    assert str(planted) in TA.check_path().split(os.pathsep)


def test_driver_resolves_the_check_path_at_start_and_keeps_it(tmp_path, monkeypatch):
    repo, mb = T.make_repo(tmp_path)
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    assert ctl.check_path == TA.check_path()
    assert ctl.state["check_path"] == ctl.check_path
    seen = {}
    real = ctl.ta.run_pack

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(ctl.ta, "run_pack", spy)
    acc = tmp_path / "pk" / "acceptance"
    T.author_pack(acc.parent, n=1)
    ctl._run(T.git(repo, "rev-parse", "HEAD"), acc=acc)
    assert seen.get("path") == ctl.check_path


# ------------------------------------------------ finding 5: attribution


def test_percheck_files_are_owned_by_their_check_despite_common_tokens():
    man = R3._manifest(n=3, runs={3: ["python3", "acceptance/checks/ACC-03/check.py"]})
    texts = {
        "checks/acc_01.py": "expected = 15\nserver, data = 1, 2\nimport subprocess\n",
        "checks/acc_02.py": "import sys\nsys.path.insert(0, 'acceptance/checks')\nimport util\n",
        "checks/util.py": "print('top-level helper')\n",
        "checks/ACC-03/check.py": "from expected import EXPECTED\n",
        "checks/ACC-03/expected.py": "EXPECTED = 2\n",
        "checks/ACC-03/server.mjs": "export {}\n",
        "checks/ACC-03/data.json": "{}\n",
        "checks/ACC-03/subprocess.py": "x = 1\n",
        "lib/acchelp.py": "import subprocess\nsubprocess.run(['x'], check=False)\n"
                          "server = data = expected = None\n",
        "fakes/srv.py": "import util\n",
    }
    files = sorted(texts) + ["MANIFEST.json"]
    attr = TA.pack_attribution(man, files, texts)
    for name in ("check.py", "expected.py", "server.mjs", "data.json"):
        assert attr[f"checks/ACC-03/{name}"] == {"ACC-03"}, (name, attr)
    # Kept over-attribution: stdlib-shadow names and top-level checks/ files.
    assert attr["checks/ACC-03/subprocess.py"] is None
    assert attr["checks/util.py"] is None  # a shared file (fakes/srv.py) mentions it
    # Path-like mentions still attribute: `<ID>/<name>`, or <ID> + name.
    texts2 = dict(texts, **{"checks/acc_01.py": "open('acceptance/checks/ACC-03/data.json')\n",
                            "checks/acc_02.py": "import os, sys\nsys.path.insert(0, os.path.join("
                                                "'acceptance', 'checks', 'ACC-03'))\n"
                                                "from expected import EXPECTED\n"})
    attr2 = TA.pack_attribution(man, files, texts2)
    assert attr2["checks/ACC-03/data.json"] == {"ACC-01", "ACC-03"}
    assert attr2["checks/ACC-03/expected.py"] == {"ACC-02", "ACC-03"}
    texts3 = dict(texts, **{"lib/acchelp.py": "CFG = 'checks/ACC-03/data.json'\n"})
    assert TA.pack_attribution(man, files, texts3)["checks/ACC-03/data.json"] is None


# ------------------------------------------ finding 6: prepared slot breach


NEXT_SLOT = r'''import glob, os, sys, time
me = os.environ["ACC_TREE"]
run = os.path.dirname(os.path.dirname(me))
for _ in range(250):
    for f in glob.glob(os.path.join(run, "c*", "tree", "acceptance", "checks", "b.py")):
        if not f.startswith(os.path.dirname(me)):
            os.chmod(os.path.dirname(f), 0o755)
            os.chmod(f, 0o644)
            open(f, "w").write("raise SystemExit(0)\n")
            print("rewrote", f)
            sys.exit(1)
    time.sleep(0.02)
sys.exit(1)
'''
NEXT_TREE = r'''import glob, os, sys, time
me = os.environ["ACC_TREE"]
run = os.path.dirname(os.path.dirname(me))
for _ in range(250):
    for d in glob.glob(os.path.join(run, "c*", "tree")):
        if d != me and os.path.exists(os.path.join(d, "app.py")):
            open(os.path.join(d, "app.py"), "w").write("print('ok')\n")
            print("rewrote", d)
            sys.exit(1)
    time.sleep(0.02)
sys.exit(1)
'''


@pytest.mark.parametrize("attack", ["view", "tree"])
def test_unsandboxed_check_rewriting_the_next_prepared_slot_is_an_isolation_fail(
        tmp_path, monkeypatch, attack):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    reader = ("import subprocess, sys\nout = subprocess.run([sys.executable, 'app.py'],"
              " capture_output=True, text=True).stdout.strip()\nprint(out)\n"
              "sys.exit(0 if out == 'ok' else 1)\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": NEXT_SLOT if attack == "view" else NEXT_TREE,
                                 "checks/b.py": reader, "checks/c.py": reader},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
                       _chk("ACC-02", ["python3", "acceptance/checks/b.py"]),
                       _chk("ACC-03", ["python3", "acceptance/checks/c.py"])])
    result = TA.run_pack(acc, repo)
    by = {r["id"]: r for r in result["results"]}
    assert "rewrote" in by["ACC-01"]["excerpt"], by["ACC-01"]
    for cid in ("ACC-02", "ACC-03"):
        assert by[cid]["outcome"] == "FAIL" and by[cid]["reason"] == "isolation", by
    assert any("prepared copy or pack view of ACC-02" in line for line in result["log"])


def test_unsandboxed_honest_run_has_no_false_isolation_fail(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    ok = "raise SystemExit(0)\n"
    repo, acc = _pack(tmp_path, {"checks/a.py": ok, "checks/ACC-02/x/deep.py": ok,
                                 "lib/h.py": "X = 1\n", "fakes/f.json": "{}\n"},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
                       _chk("ACC-02", ["python3", "acceptance/checks/ACC-02/x/deep.py"]),
                       _chk("ACC-03", ["python3", "acceptance/checks/a.py"])])
    (repo / "data").mkdir()
    (repo / "data" / "blob.bin").write_bytes(os.urandom(4096))
    os.symlink("data/blob.bin", repo / "link")
    result = TA.run_pack(acc, repo)
    assert result["passed"] == 3, result


# ------------------------------------------------ finding 7: symlinks


def test_symlinks_refused_at_validation_and_never_frozen(tmp_path):
    export = tmp_path / "export"
    T.author_pack(export)
    acc = export / "acceptance"
    target = tmp_path / "outside.py"
    target.write_text("raise SystemExit(0)\n")
    os.symlink(target, acc / "checks" / "payload.py")
    assert TA.pack_symlinks(acc) == ["checks/payload.py"]
    man = TA.load_manifest(acc)
    filtered = TA.freeze_filter(man, None, T.GOAL, None, acc)
    assert filtered["retry"] and any("symlinks are not allowed" in f for f in filtered["fatal"])
    dst = tmp_path / "frozen" / "acceptance"
    TA.write_frozen_pack(acc, dst, man)
    assert not (dst / "checks" / "payload.py").exists() and TA.pack_symlinks(dst) == []


class SymlinkAmend(T.Fake):
    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
            self.done = True
            acc = self.mb / "acceptance"
            (acc / "checks" / "ACC-08").mkdir()
            outside = self.repo.parent / "role-writable.py"
            outside.write_text(R3.HONEST_08)
            os.symlink(outside, acc / "checks" / "ACC-08" / "payload.py")
            man = json.loads((acc / "MANIFEST.json").read_text())
            for c in man["checks"]:
                if c["id"] == "ACC-08":
                    c["run"] = ["python3", "acceptance/checks/ACC-08/payload.py"]
            (acc / "MANIFEST.json").write_text(json.dumps(man, indent=2))
            R3._record(acc, 8, iteration)
            T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(self.repo, "commit", "-qm",
                        f"acceptance: amend ACC-08 (evaluator, iter {iteration}): payload")
        return super()._eval(iteration, context)


def test_symlink_amendment_is_rejected(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = SymlinkAmend(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    T.run(mb, fake)
    lg = T.log(mb)
    assert "amendment of ACC-08 rejected" in lg and "symlinks are not allowed" in lg, lg[-2500:]
    assert not (mb / "acceptance" / "checks" / "ACC-08" / "payload.py").is_symlink()


# ----------------------------------------- realistic honest packs (eval-r19d)


def _w(p, s, mode=None):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).lstrip())
    if mode:
        p.chmod(mode)


def _chk_h(checks, i, run, quote, surface="cli", **kw):
    checks.append({"id": f"ACC-{i:02d}", "goal_ref": "GOAL.md:1", "goal_quote": quote,
                   "kind": "behaviour", "surface": surface, "run": run, "expect": {"exit": 0},
                   "timeout_s": 60, "network": "loopback", **kw})


def product_subusage(root, impl=True):
    root = Path(root)
    _w(root / "README.md", "# subusage\n" + ("Usage: python3 -m subusage report\n" if impl else ""))
    _w(root / "subusage/__init__.py", "")
    if not impl:
        _w(root / "subusage/__main__.py", "raise SystemExit('not implemented')\n")
        return
    _w(root / "subusage/__main__.py", "from subusage.cli import main\nraise SystemExit(main())\n")
    _w(root / "subusage/cli.py", """
        import json, subprocess, sys
        from subusage.parse import parse_usage
        def main(argv=None):
            argv = sys.argv[1:] if argv is None else argv
            if not argv or argv[0] in ('-h', '--help'):
                print('usage: subusage report [--json]'); return 0
            if argv[0] == 'report':
                out = subprocess.run(['claude-usage', '--raw'], capture_output=True, text=True)
                if out.returncode:
                    print('error: claude-usage failed', file=sys.stderr); return 2
                data = parse_usage(out.stdout)
                print(json.dumps(data) if '--json' in argv else f"total={data['total']}")
                return 0
            return 2
        """)
    _w(root / "subusage/parse.py", """
        def parse_usage(text):
            rows = [l.split(',') for l in text.strip().splitlines()[1:]]
            return {'total': sum(int(r[1]) for r in rows), 'rows': len(rows)}
        """)
    _w(root / "tool.py", "from toolhelpers import banner\nprint(banner())\n")
    _w(root / "toolhelpers.py", "def banner():\n    return 'subusage tool v1'\n")
    _w(root / "tests/test_parse.py", """
        from subusage.parse import parse_usage
        def test_parse():
            assert parse_usage('h,n\\na,1\\nb,2\\n')['total'] == 3
        """)


def pack_subusage(acc, variant=False):
    acc = Path(acc)
    checks: list = []
    tail = ", check=False)" if variant else ")"
    _w(acc / "lib/acchelp.py", f"""
        import os, subprocess, sys
        TREE = os.environ.get('ACC_TREE', '.')
        FAKES = os.path.join(os.environ['ACC_DIR'], 'fakes')
        def run_cli(*args, env_extra=None):
            env = dict(os.environ)
            env['PATH'] = os.path.join(FAKES, 'bin') + os.pathsep + env['PATH']
            env.update(env_extra or {{}})
            return subprocess.run([sys.executable, '-m', 'subusage', *args], cwd=TREE,
                                  capture_output=True, text=True, env=env, timeout=20{tail}
        def fail(msg):
            print(msg); raise SystemExit(1)
        """)
    _w(acc / "fakes/bin/claude-usage", "#!/bin/sh\nprintf 'h,n\\na,%s\\nb,5\\n' \"${FAKE_A:-3}\"\n",
       0o755)
    _w(acc / "fakes/usage_fixture.csv", "h,n\na,3\nb,5\n")
    _w(acc / "checks/acc_01_help.py", """
        from acchelp import run_cli, fail
        p = run_cli('--help')
        if p.returncode != 0 or 'report' not in p.stdout: fail(f'--help broken: {p.stderr[-200:]}')
        """)
    _chk_h(checks, 1, ["python3", "acceptance/checks/acc_01_help.py"], "prints help")
    _w(acc / "checks/acc_02_total.py", """
        from acchelp import run_cli, fail
        p = run_cli('report')
        if p.stdout.strip() != 'total=8': fail(f'bad total: {p.stdout!r} {p.stderr[-300:]}')
        """)
    _chk_h(checks, 2, ["python3", "acceptance/checks/acc_02_total.py"], "reports total")
    total = "expected = 15\nif d.get('total') != expected" if variant \
        else "if d.get('total') != 15"
    _w(acc / "checks/acc_03_json.py", f"""
        import json
        from acchelp import run_cli, fail
        p = run_cli('report', '--json', env_extra={{'FAKE_A': '10'}})
        try: d = json.loads(p.stdout)
        except Exception: fail(f'not json: {{p.stdout!r}} {{p.stderr[-300:]}}')
        {total}: fail('bad json total')
        """)
    _chk_h(checks, 3, ["python3", "acceptance/checks/acc_03_json.py"], "json output")
    _w(acc / "checks/acc_04_fail.sh", """
        #!/bin/sh
        . "$ACC_DIR/lib/common.sh"
        mkdir -p "$ACC_WORK/bin"; printf '#!/bin/sh\\nexit 3\\n' > "$ACC_WORK/bin/claude-usage"; chmod +x "$ACC_WORK/bin/claude-usage"
        cd "$ACC_TREE" && PATH="$ACC_WORK/bin:$PATH" python3 -m subusage report >/dev/null 2>"$ACC_WORK/err"
        rc=$?
        [ $rc -ne 0 ] || fail "exit 0 on failure"
        grep -q 'claude-usage' "$ACC_WORK/err" || fail "no error message: $(tail -c 200 "$ACC_WORK/err")"
        exit 0
        """)
    _w(acc / "lib/common.sh", "fail() { echo \"$*\"; exit 1; }\n")
    _chk_h(checks, 4, ["sh", "acceptance/checks/acc_04_fail.sh"], "errors when cli fails")
    _w(acc / "checks/acc_05_readme.sh", """
        #!/bin/sh
        grep -q 'python3 -m subusage report' "$ACC_TREE/README.md" || { echo "README lacks usage"; exit 1; }
        """)
    _chk_h(checks, 5, ["sh", "acceptance/checks/acc_05_readme.sh"], "README documents",
           kind="doc")
    _w(acc / "checks/acc_06_tool.sh", """
        #!/bin/sh
        out=$(cd "$ACC_TREE" && python3 tool.py 2>&1) || { echo "tool.py failed: $(echo "$out"|tail -1)"; exit 1; }
        echo "$out" | grep -q 'subusage tool' || { echo "no banner"; exit 1; }
        """)
    _chk_h(checks, 6, ["sh", "acceptance/checks/acc_06_tool.sh"], "tool banner")
    _w(acc / "checks/acc_07_pytest.sh", """
        #!/bin/sh
        cd "$ACC_TREE" && [ -d tests ] || { echo "no tests"; exit 1; }
        python3 -m pytest -q -p no:cacheprovider tests >"$ACC_WORK/log" 2>&1 || { tail -3 "$ACC_WORK/log"; exit 1; }
        """)
    _chk_h(checks, 7, ["sh", "acceptance/checks/acc_07_pytest.sh"], "has tests")
    _w(acc / "checks/ACC-08/check.py", """
        import os, sys, json
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from expected import EXPECTED
        from acchelp import run_cli, fail
        p = run_cli('report', '--json')
        try: d = json.loads(p.stdout)
        except Exception: fail('not json')
        if d.get('rows') != EXPECTED: fail('bad rows')
        """)
    _w(acc / "checks/ACC-08/expected.py", "EXPECTED = 2\n")
    _chk_h(checks, 8, ["python3", "acceptance/checks/ACC-08/check.py"], "row count")
    _w(acc / "MANIFEST.json", json.dumps({"acceptance_version": 1, "budget_s": 300, "setup": [],
                                          "bindings": {}, "checks": checks}, indent=2))
    _w(acc / "AUTHOR.md", "# author\n")


def product_openrouter(root, impl=True):
    root = Path(root)
    _w(root / "package.json", json.dumps({"name": "orclient", "type": "module", "version": "1.0.0",
                                          "bin": {"orc": "bin/orc.mjs"}}))
    if not impl:
        _w(root / "src/index.mjs", "export async function listModels() { throw new Error('todo'); }\n")
        return
    _w(root / "src/index.mjs", """
        export async function listModels(base, key) {
          const r = await fetch(new URL('/api/v1/models', base), {headers: {Authorization: `Bearer ${key}`}});
          if (r.status === 429) throw new Error('rate_limited');
          if (!r.ok) throw new Error(`http ${r.status}`);
          const j = await r.json(); return j.data.map(m => m.id);
        }
        """)
    _w(root / "bin/orc.mjs", """
        #!/usr/bin/env node
        import { listModels } from '../src/index.mjs';
        const base = process.env.OPENROUTER_BASE_URL || 'https://openrouter.ai';
        try { const ids = await listModels(base, process.env.OPENROUTER_API_KEY || '');
              console.log(ids.join('\\n')); }
        catch (e) { console.error(`error: ${e.message}`); process.exit(2); }
        """, 0o755)


def pack_openrouter(acc):
    acc = Path(acc)
    checks: list = []
    kw = {"surface": "http", "needs": ["node"]}
    _w(acc / "package.json", json.dumps({"type": "module", "private": True}))
    _w(acc / "fakes/openrouter.mjs", """
        import http from 'node:http';
        export function startFake(mode = 'ok') {
          return new Promise(res => {
            const s = http.createServer((req, rsp) => {
              if (mode === '429') { rsp.writeHead(429); return rsp.end('{}'); }
              if (req.headers.authorization !== 'Bearer sk-test') { rsp.writeHead(401); return rsp.end('{}'); }
              rsp.writeHead(200, {'content-type': 'application/json'});
              rsp.end(JSON.stringify({data: [{id: 'a/m1'}, {id: 'b/m2'}]}));
            });
            s.listen(0, '127.0.0.1', () => res({server: s, url: `http://127.0.0.1:${s.address().port}`}));
          });
        }
        """)
    _w(acc / "fakes/fake_openrouter.py", """
        import http.server, json, threading
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'data': [{'id': 'a/m1'}, {'id': 'b/m2'}]}).encode()
                self.send_response(200); self.send_header('content-type', 'application/json')
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        def start():
            s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            return s, f'http://127.0.0.1:{s.server_address[1]}'
        """)
    _w(acc / "checks/or_common.mjs", """
        import path from 'node:path';
        export const tree = process.env.ACC_TREE;
        export async function client() { return import(path.join(tree, 'src/index.mjs')); }
        export function fail(m) { console.log(m); process.exit(1); }
        """)
    _w(acc / "checks/acc_01_list.mjs", """
        import { startFake } from '../fakes/openrouter.mjs';
        import { client, fail } from './or_common.mjs';
        const { server, url } = await startFake();
        try { const ids = await (await client()).listModels(url, 'sk-test');
              if (ids.join() !== 'a/m1,b/m2') fail('wrong ids ' + ids); }
        catch (e) { fail('listModels threw: ' + e.message); } finally { server.close(); }
        """)
    _chk_h(checks, 1, ["node", "acceptance/checks/acc_01_list.mjs"], "lists models", **kw)
    _w(acc / "checks/acc_02_429.mjs", """
        import { startFake } from '../fakes/openrouter.mjs';
        import { client, fail } from './or_common.mjs';
        const { server, url } = await startFake('429');
        try { await (await client()).listModels(url, 'sk-test'); fail('no error on 429'); }
        catch (e) { if (!/rate_limited/.test(e.message)) fail('wrong error ' + e.message); } finally { server.close(); }
        """)
    _chk_h(checks, 2, ["node", "acceptance/checks/acc_02_429.mjs"], "429 is rate_limited", **kw)
    _w(acc / "checks/acc_03_cli.sh", """
        #!/bin/sh
        set -u
        port_file="$ACC_WORK/port"
        python3 -c "
        import sys, os, time; sys.path.insert(0, os.path.join(os.environ['ACC_DIR'], 'fakes'))
        import fake_openrouter as f
        s, url = f.start(); open('$port_file','w').write(url); time.sleep(20)" &
        pid=$!
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$port_file" ] && break; sleep 0.25; done
        out=$(cd "$ACC_TREE" && OPENROUTER_BASE_URL=$(cat "$port_file") OPENROUTER_API_KEY=sk-test node bin/orc.mjs 2>&1)
        rc=$?; kill $pid 2>/dev/null
        [ $rc -eq 0 ] || { echo "orc failed: $out" | tail -1; exit 1; }
        echo "$out" | grep -q 'b/m2' || { echo "missing model"; exit 1; }
        """)
    _chk_h(checks, 3, ["sh", "acceptance/checks/acc_03_cli.sh"], "cli prints models", **kw)
    _w(acc / "checks/acc_04_key.py", """
        import os, subprocess, sys
        sys.path.insert(0, os.path.join(os.environ['ACC_DIR'], 'fakes'))
        import fake_openrouter as f
        s, url = f.start()
        p = subprocess.run(['node', 'bin/orc.mjs'], cwd=os.environ['ACC_TREE'], capture_output=True, text=True,
                           env=dict(os.environ, OPENROUTER_BASE_URL=url, OPENROUTER_API_KEY='sk-other'))
        if 'a/m1' not in p.stdout: print('no models: ' + (p.stderr or '')[-200:]); sys.exit(1)
        """)
    _chk_h(checks, 4, ["python3", "acceptance/checks/acc_04_key.py"], "uses key", **kw)
    _w(acc / "checks/acc_05_pkg.mjs", """
        import fs from 'node:fs'; import path from 'node:path';
        const pkg = JSON.parse(fs.readFileSync(path.join(process.env.ACC_TREE, 'package.json')));
        if (!pkg.bin || !pkg.bin.orc) { console.log('no orc bin'); process.exit(1); }
        """)
    _chk_h(checks, 5, ["node", "acceptance/checks/acc_05_pkg.mjs"], "ships orc bin", **kw)
    _w(acc / "MANIFEST.json", json.dumps({"acceptance_version": 1, "budget_s": 300, "setup": [],
                                          "bindings": {}, "checks": checks}, indent=2))
    _w(acc / "AUTHOR.md", "# a\n")


def _score(acc, tree):
    result = TA.run_pack(acc, tree)
    return result["passed"], result["total"], {r["id"]: (r["outcome"], r.get("reason"))
                                               for r in result["results"]}


def test_honest_python_cli_pack_full_pass_on_the_correct_product(tmp_path, sandbox):
    if subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode:
        pytest.skip("pytest is not importable")
    product_subusage(tmp_path / "impl", True)
    product_subusage(tmp_path / "base", False)
    pack_subusage(tmp_path / "pack" / "acceptance")
    passed, total, by = _score(tmp_path / "pack" / "acceptance", tmp_path / "impl")
    assert (passed, total) == (8, 8), by
    passed, total, by = _score(tmp_path / "pack" / "acceptance", tmp_path / "base")
    assert passed == 0, by  # every check discriminates


def test_honest_node_http_pack_full_pass_on_the_correct_product(tmp_path, sandbox):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    product_openrouter(tmp_path / "impl", True)
    product_openrouter(tmp_path / "base", False)
    pack_openrouter(tmp_path / "pack" / "acceptance")
    passed, total, by = _score(tmp_path / "pack" / "acceptance", tmp_path / "impl")
    assert (passed, total) == (5, 5), by
    passed, total, by = _score(tmp_path / "pack" / "acceptance", tmp_path / "base")
    assert passed == 1 and by["ACC-05"][0] == "PASS", by  # ACC-05 would be a guard


# -------------------------------- honest single-check amendments (amend_sim)


def _judge(acc, edits, ids, man_edit=None, work=None):
    old_m = TA.load_manifest(acc)
    old_files = TA.pack_files(acc)
    old_attr = TA.pack_attribution(old_m, old_files, TA.pack_texts(acc))
    new = Path(work) / "sim" / "acceptance"
    shutil.rmtree(new.parent, ignore_errors=True)
    shutil.copytree(acc, new)
    for rel, text in edits.items():
        (new / rel).parent.mkdir(parents=True, exist_ok=True)
        (new / rel).write_text(text)
    new_m = TA.load_manifest(new)
    if man_edit:
        man_edit(new_m)
        (new / "MANIFEST.json").write_text(json.dumps(new_m))
    changed = list(edits) + (["MANIFEST.json"] if man_edit else [])
    new_attr = TA.pack_attribution(new_m, TA.pack_files(new), TA.pack_texts(new))
    return TA.amendment_problems(old_m, new_m, changed, ids, old_files,
                                 old_attr=old_attr, new_attr=new_attr)


def honest_amendment_outcomes(work: Path) -> dict[str, list[str]]:
    """The reviewer's honest single-check edits (honest/amend_sim.py)."""
    sub = work / "sub" / "acceptance"
    pack_subusage(sub)
    var = work / "sub-v1" / "acceptance"
    pack_subusage(var, variant=True)
    orp = work / "or" / "acceptance"
    pack_openrouter(orp)
    rd = lambda p: p.read_text()  # noqa: E731
    cases = {
        "sub ACC-02 script: relax whitespace": (sub, {"checks/acc_02_total.py": rd(
            sub / "checks/acc_02_total.py").replace("p.stdout.strip() !=",
                                                    "p.stdout.split()[0] !=")}, ["ACC-02"], None),
        "sub ACC-08 expected.py value": (sub, {"checks/ACC-08/expected.py": "EXPECTED = 2  # rows\n"},
                                         ["ACC-08"], None),
        "sub ACC-08 new file checks/ACC-08/rows.json": (sub, {"checks/ACC-08/rows.json": "2\n"},
                                                        ["ACC-08"], None),
        "sub ACC-05 manifest timeout": (sub, {}, ["ACC-05"],
                                        lambda m: m["checks"][4].update(timeout_s=45)),
        "variant ACC-08 expected.py value": (var, {"checks/ACC-08/expected.py":
                                                   "EXPECTED = 2  # rows\n"}, ["ACC-08"], None),
        "variant ACC-08 check.py: better message": (var, {"checks/ACC-08/check.py": rd(
            var / "checks/ACC-08/check.py").replace("'bad rows'", "'bad rows: %r' % d")},
            ["ACC-08"], None),
        "or ACC-01 list: order-insensitive": (orp, {"checks/acc_01_list.mjs": rd(
            orp / "checks/acc_01_list.mjs").replace("ids.join()", "[...ids].sort().join()")},
            ["ACC-01"], None),
        "or ACC-03 cli: longer wait": (orp, {"checks/acc_03_cli.sh": rd(
            orp / "checks/acc_03_cli.sh").replace("sleep 0.25", "sleep 0.5")}, ["ACC-03"], None),
        "or ACC-04 new checks/ACC-04/models.json": (orp, {"checks/ACC-04/models.json": "[]\n"},
                                                    ["ACC-04"], None),
        "or ACC-02 new checks/ACC-02/server.mjs": (orp, {"checks/ACC-02/server.mjs": "export {}\n"},
                                                   ["ACC-02"], None),
        "or ACC-02 new checks/ACC-02/data.json": (orp, {"checks/ACC-02/data.json": "{}\n"},
                                                  ["ACC-02"], None),
        "or ACC-02 new checks/ACC-02/expected.json": (orp, {"checks/ACC-02/expected.json": "{}\n"},
                                                      ["ACC-02"], None),
        "or ACC-02 new checks/ACC-02/check_429.mjs": (orp, {"checks/ACC-02/check_429.mjs":
                                                            "export {}\n"}, ["ACC-02"], None),
    }
    return {name: _judge(acc, edits, ids, man, work)
            for name, (acc, edits, ids, man) in cases.items()}


def test_reviewer_honest_single_check_amendments_are_all_accepted(tmp_path):
    outcomes = honest_amendment_outcomes(tmp_path)
    refused = {k: v for k, v in outcomes.items() if v}
    assert not refused, refused
    assert len(outcomes) == 13


def test_scope_rule_still_refuses_what_other_checks_load(tmp_path):
    orp = tmp_path / "or" / "acceptance"
    pack_openrouter(orp)
    # A shared helper (top-level checks/ file several checks import).
    assert _judge(orp, {"checks/or_common.mjs": "export const x = 1\n"}, ["ACC-01"],
                  work=tmp_path)
    # A fake (shared location).
    assert _judge(orp, {"fakes/openrouter.mjs": "export {}\n"}, ["ACC-01"], work=tmp_path)
    # A new per-check file a pinned check already names by path.
    f = orp / "checks/acc_01_list.mjs"
    f.write_text(f.read_text() + "// reads ../checks/ACC-02/data.json\n")
    probs = _judge(orp, {"checks/ACC-02/data.json": "{}\n"}, ["ACC-02"], work=tmp_path)
    assert any("other checks would load" in p and "ACC-01" in p for p in probs), probs

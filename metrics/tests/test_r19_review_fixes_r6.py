"""r19 eval-r19f follow-ups (round 6): the eval-r19f repros, inverted.

- finding 1: a pytest run on PACK tests gets ``-P`` and
  ``-p no:cacheprovider --confcutdir=<view> -c /dev/null --rootdir=<view>``:
  a product ``conftest.py`` / ``pytest.ini`` / ``pyproject.toml`` /
  ``*.dist-info`` plugin / ``pytest.py`` no longer decides the outcome.
  Pytest on the product's own tests is untouched.
- finding 2: `expect` matching is bounded by min(timeout_s,
  MATCH_DEADLINE_S); past it the check FAILs ``pattern-timeout`` and the
  time is in the check's own wall_s.
- finding 3: a pack-code check that FAILs at base on an import error is
  dropped at freeze (``PACK_IMPORT_DROP``) and the author is retried; a
  product traceback a check echoes is not such a failure.
- finding 4: ``check_errors`` normalises ``..`` before the existence check.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T
from metrics.tests import test_r19_review_fixes_r3 as R3

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
TA = trio_loop._load_sibling("ta_r19_fix6", "trio-acceptance.py")
_chk = R3._chk

PATH = TA.check_path()
HAS_P = TA._supports_safe_path(shutil.which("python3", path=PATH) or "python3")
HAS_PYTEST = importlib.util.find_spec("pytest") is not None


def _pack(tmp_path, scripts, checks, product=None):
    repo, acc = R3._pack(tmp_path, {"AUTHOR.md": "# a\n", **scripts}, checks)
    for rel, body in (product or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    return repo, acc


@pytest.fixture(params=["none", "bwrap"])
def sandbox(request, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none" if request.param == "none" else "auto")
    if request.param == "bwrap" and TA.sandbox_mode() != "bwrap":
        pytest.skip("bwrap does not work on this host")
    return request.param


def _by(result):
    return {r["id"]: r for r in result["results"]}


# ------------------------------------------------------------ finding 1


GAMING_CONFTEST = (
    "import pytest\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    out = yield\n"
    "    r = out.get_result()\n"
    "    if r.failed: r.outcome = 'passed'\n")
GAMING_PLUGIN = GAMING_CONFTEST
FAILING_TEST = "def test_it():\n    assert 1 == 2, 'feature missing'\n"
PYTEST_RUN = ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@pytest.mark.parametrize("product", [
    {"conftest.py": GAMING_CONFTEST},                                   # the ACC-05 repro
    {"pytest.ini": "[pytest]\naddopts = -p gamer\n", "gamer.py": GAMING_PLUGIN},
    {"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-p gamer'\n",
     "gamer.py": GAMING_PLUGIN},
    {"evil.dist-info/METADATA": "Metadata-Version: 2.1\nName: evil\nVersion: 1\n",
     "evil.dist-info/entry_points.txt": "[pytest11]\nevil = gamer\n",
     "gamer.py": GAMING_PLUGIN},
    {"pytest.py": "raise SystemExit(0)\n"},
], ids=["conftest", "pytest.ini", "pyproject", "dist-info", "pytest.py"])
def test_f1_product_cannot_decide_a_pack_pytest(tmp_path, sandbox, product):
    if not HAS_PYTEST:
        pytest.skip("pytest is not importable")
    if "pytest.py" in product or "dist-info" in "".join(product):
        if not HAS_P:
            pytest.skip("python3 has no -P")
    repo, acc = _pack(tmp_path, {"checks/ACC-01/test_feature.py": FAILING_TEST}, [
        _chk("ACC-01", [*PYTEST_RUN, "acceptance/checks/ACC-01/test_feature.py"]),
        _chk("ACC-02", [*PYTEST_RUN, "acceptance/checks/ACC-01/test_feature.py::test_it"]),
    ], product=product)
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "FAIL", by
    assert by["ACC-02"]["outcome"] == "FAIL", by


def test_f1_honest_pack_pytest_still_passes(tmp_path, sandbox):
    if not HAS_PYTEST:
        pytest.skip("pytest is not importable")
    repo, acc = _pack(tmp_path, {
        "lib/acchelp.py": "def app_output():\n"
                          "    import os, subprocess, sys\n"
                          "    return subprocess.run([sys.executable, 'app.py'], "
                          "cwd=os.environ['ACC_TREE'], capture_output=True, "
                          "text=True).stdout\n",
        "checks/ACC-01/conftest.py": "import pytest\n@pytest.fixture\n"
                                     "def word():\n    return 'v0'\n",
        "checks/ACC-01/test_app.py": "from acchelp import app_output\n"
                                     "def test_app(word):\n"
                                     "    assert app_output().strip() == word\n",
    }, [_chk("ACC-01", [*PYTEST_RUN, "acceptance/checks/ACC-01/test_app.py"]),
        _chk("ACC-02", ["python3", "-m", "pytest", "-q", "acceptance/checks/ACC-01"])],
        product={"conftest.py": "raise SystemExit('product conftest must not load')\n"})
    by = _by(TA.run_pack(acc, repo))
    assert by["ACC-01"]["outcome"] == "PASS", by
    assert by["ACC-02"]["outcome"] == "PASS", by


def test_f1_pytest_on_product_tests_is_untouched(tmp_path):
    for run in (["python3", "-m", "pytest", "-q", "tests"], ["pytest", "tests/test_x.py"],
                ["python3", "-m", "pytest"], ["python3", "acceptance/checks/x.py", "-m",
                                              "pytest"]):
        got = TA.isolated_argv(run, PATH, tmp_path)
        assert "--confcutdir" not in " ".join(got), got
        assert TA.pack_pytest_at(run, tmp_path) is None, run


def test_f1_pack_pytest_argv_shapes(tmp_path):
    view = str(tmp_path / "acceptance")
    extra = TA.pytest_isolation_args(view)
    assert extra == ["-p", "no:cacheprovider", f"--confcutdir={view}", "-c", os.devnull,
                     f"--rootdir={view}"]
    p = ["-P"] if HAS_P else []
    run = ["python3", "-m", "pytest", "-q", "acceptance/checks/t.py"]
    assert TA.isolated_argv(run, PATH, tmp_path) == \
        ["python3", *p, "-m", "pytest", *extra, "-q", "acceptance/checks/t.py"]
    run = ["python3", "-B", "-mpytest", "./acceptance/checks/t.py::test_a"]
    assert TA.isolated_argv(run, PATH, tmp_path) == \
        ["python3", *p, "-B", "-mpytest", *extra, "./acceptance/checks/t.py::test_a"]
    run = ["pytest", "-q", "acceptance/checks"]  # a console script: no -P
    assert TA.isolated_argv(run, PATH, tmp_path) == ["pytest", *extra, "-q", "acceptance/checks"]


# ------------------------------------------------------------ finding 2


def _long_json(kib: int) -> str:
    return '{"items":[' + ",".join('"a"' for _ in range(kib * 256)) + "]}\n"


def test_f2_backtracking_pattern_is_bounded(monkeypatch):
    monkeypatch.setattr(TA, "MATCH_DEADLINE_S", 1.0)
    out = _long_json(256)  # ~140 s unbounded (eval-r19f regex-cost.txt)
    check = {"expect": {"exit": 0, "stdout": ['.*"count"']}, "timeout_s": 60}
    attempt = {"exit": 0, "stdout": out, "stderr": "", "output": out[-2048:]}
    t0 = time.monotonic()
    assert TA._classify(check, attempt) == ("FAIL", "pattern-timeout")
    assert time.monotonic() - t0 < 5
    assert 0.9 <= attempt["match_s"] < 5
    assert "stdout" in attempt["match_detail"]


def test_f2_deadline_is_min_of_check_timeout(monkeypatch):
    monkeypatch.setattr(TA, "MATCH_DEADLINE_S", 30.0)
    check = {"expect": {"exit": 0, "output": ["(a+)+$"]}, "timeout_s": 1}
    attempt = {"exit": 0, "stdout": "a" * 40 + "b", "stderr": ""}
    t0 = time.monotonic()
    assert TA._classify(check, attempt) == ("FAIL", "pattern-timeout")
    assert time.monotonic() - t0 < 5


def test_f2_ordinary_matching_unchanged():
    out = _long_json(256) + '"count": 3\n'
    check = {"expect": {"exit": 0, "stdout": ['"count": 3', "^{"], "stderr": ["warn"]}}
    ok = {"exit": 0, "stdout": out, "stderr": "warn\n"}
    assert TA._classify(check, ok) == ("PASS", None)
    miss = {"exit": 0, "stdout": out, "stderr": "no\n"}
    assert TA._classify(check, miss) == ("FAIL", "stderr does not match 'warn'")
    assert TA._classify({"expect": {"exit": 0}}, {"exit": 0, "stdout": ""}) == ("PASS", None)


def test_f2_in_a_run_pack_the_time_is_the_checks_own(tmp_path, sandbox, monkeypatch):
    monkeypatch.setattr(TA, "MATCH_DEADLINE_S", 2.0)
    repo, acc = _pack(tmp_path, {
        "checks/long.py": "print('{\"items\":[' + ','.join('\"a\"' for _ in range(60000))"
                          " + ']}')\n",
        "checks/ok.py": "print('fine')\n",
    }, [dict(_chk("ACC-01", ["python3", "acceptance/checks/long.py"]),
             expect={"exit": 0, "stdout": ['.*"count"']}),
        dict(_chk("ACC-02", ["python3", "acceptance/checks/ok.py"]),
             expect={"exit": 0, "stdout": ["^fine$"]})])
    res = TA.run_pack(acc, repo)
    by = _by(res)
    assert by["ACC-01"]["outcome"] == "FAIL" and by["ACC-01"]["reason"] == "pattern-timeout"
    assert by["ACC-01"]["wall_s"] >= 1.9
    assert by["ACC-02"]["outcome"] == "PASS", by
    assert res["wall_s"] < 30


# ------------------------------------------------------------ finding 3


def test_f3_pack_import_failures_are_dropped_and_retried(tmp_path, sandbox):
    product_echo = ("import subprocess, sys\n"
                    "p = subprocess.run([sys.executable, 'app.py'], capture_output=True, "
                    "text=True)\n"
                    "sys.stdout.write(p.stderr)\n"
                    "raise SystemExit(0 if p.returncode == 0 else 1)\n")
    scripts = {
        "checks/sibling.py": "import sib\n",
        "checks/ACC-02/sib.py": "X = 1\n",
        "checks/nolib.py": "import not_a_module_anywhere_r19f\n",
        "checks/echo.py": product_echo,
        "checks/plain.py": "print('feature missing')\nraise SystemExit(1)\n",
        "lib/helpmod.py": "import features_r19f\n",
        "checks/ACC-08/test_imp.py": "def test_it():\n"
                                     "    import features_r19f\n",
    }
    checks = [
        _chk("ACC-01", ["python3", "-c", "import features_r19f; print(features_r19f.f())"]),
        _chk("ACC-02", ["python3", "acceptance/checks/sibling.py"]),
        _chk("ACC-03", ["python3", "acceptance/checks/nolib.py"]),
        _chk("ACC-04", ["python3", "acceptance/checks/echo.py"]),
        _chk("ACC-05", ["python3", "acceptance/checks/plain.py"]),
        _chk("ACC-06", ["python3", "-m", "helpmod"]),
        _chk("ACC-07", ["python3", "app.py"]),
    ]
    if HAS_PYTEST:
        checks.append(_chk("ACC-08", [*PYTEST_RUN, "acceptance/checks/ACC-08/test_imp.py"]))
    repo, acc = _pack(tmp_path, scripts, checks, product={
        "app.py": "import features_r19f\n"})
    base = TA.run_pack(acc, repo)
    by = _by(base)
    assert all(by[c["id"]]["outcome"] == "FAIL" for c in checks), by
    out = TA.freeze_filter(json.loads((acc / "MANIFEST.json").read_text()), base,
                           None, None, acc)
    reasons = dict(out["dropped"])
    dropped = {"ACC-01", "ACC-02", "ACC-03", "ACC-06"} | ({"ACC-08"} if HAS_PYTEST else set())
    assert set(reasons) == dropped, reasons
    for cid in dropped:
        assert reasons[cid].startswith(TA.PACK_IMPORT_DROP), reasons[cid]
        assert "must not import product" in reasons[cid] and ";" not in reasons[cid]
    assert "features_r19f" in reasons["ACC-01"] and "sib" in reasons["ACC-02"]
    # ACC-04 echoes the PRODUCT's traceback, ACC-07 is the product itself:
    # discriminating checks, kept.
    assert [c["id"] for c in out["manifest"]["checks"]] == ["ACC-04", "ACC-05", "ACC-07"]
    assert out["retry"]


def test_f3_one_import_drop_forces_the_retry(tmp_path):
    acc = tmp_path / "acceptance"
    (acc / "checks").mkdir(parents=True)
    (acc / "checks" / "a.py").write_text("pass\n")
    checks = [_chk(f"ACC-{n:02d}", ["python3", "acceptance/checks/a.py"]) for n in range(1, 11)]
    man = {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
           "checks": checks}
    tb = ('Traceback (most recent call last):\n  File "acceptance/checks/a.py", line 1, '
          "in <module>\nModuleNotFoundError: No module named 'yaml'\n")
    base = {"results": [{"id": c["id"], "outcome": "FAIL", "reason": "x", "excerpt": ""}
                        for c in checks]}
    assert not TA.freeze_filter(man, base, None, None, acc)["retry"]
    base["results"][3].update(excerpt=tb, reason="ModuleNotFoundError: No module named 'yaml'")
    out = TA.freeze_filter(man, base, None, None, acc)
    assert [cid for cid, _ in out["dropped"]] == ["ACC-04"] and out["retry"]


def test_f3_prompt_step2_no_longer_suggests_run_c():
    text = (Path(trio_loop.__file__).resolve().parents[1] / "prompts" / "canonical"
            / "acceptance.md").read_text()
    step2 = text[text.index("2. For each testable"):text.index("3. Surface naming")]
    assert "CHILD process" in step2 and "not the manifest's `run`" in step2
    assert "dropped at" in step2


# ------------------------------------------------------------ finding 4


def test_f4_check_errors_normalises_dotdot(tmp_path):
    acc = tmp_path / "pack" / "acceptance"
    (acc / "lib").mkdir(parents=True)
    (acc / "checks").mkdir()
    (acc / "checks" / "a.py").write_text("pass\n")
    man = {"acceptance_version": 1, "setup": [], "bindings": {}, "checks": []}

    def errs(run):
        return [e for e in TA.check_errors(_chk("ACC-01", run), man, acc_root=acc)
                if "does not exist" in e]
    assert errs(["python3", "acceptance/lib/../../app.py"]) == []
    assert errs(["python3", "acceptance/lib/../checks/a.py"]) == []
    assert errs(["python3", "acceptance/lib/../checks/missing.py"])
    assert errs(["python3", "acceptance/checks/missing.py"])

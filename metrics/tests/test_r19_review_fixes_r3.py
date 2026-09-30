"""r19 eval-r19c repairs: a check's outcome depends only on its own pinned
files, the shared pinned files and the product tree.

- F1 (inverted): an amended check that rewrites other checks' scripts at run
  time (conditional on post-base state) cannot reach them: every check runs
  in its own fresh copy of the tree with its own read-only pack view.
- F2 (inverted): a file named only by the amended check's `run` that other
  checks' interpreters would load implicitly (a stdlib shadow in checks/)
  is refused: new files only under checks/<ID>/; module shadows, fakes/,
  lib/ and interpreter-loaded names belong to every check; a check's own Python
  runs with -P (round 4; round 3 used PYTHONSAFEPATH).
- L1-L3 (liveness, ported from the eval repros): honest amendments, a human
  amendment after a clean stop, and restarts still ship.
- Attribution/view unit tests, the pinned-pack SHIP gate, merge and
  non-first-parent histories (area 1), restore-by-exchange under
  concurrency (area 3).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from metrics import trio_loop
from metrics.tests import test_r19_loop_acceptance as T

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"
TA = trio_loop._load_sibling("ta_r19_fix3", "trio-acceptance.py")
FEATS_5_8 = [f"f{k}" for k in range(5, 9)]  # ACC-01..04 must FAIL forever


def shadow(mb, env=None):
    return subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True, env=env)


def _record(acc, k, iteration):
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write(f"## ACC-{k:02d} · iter {iteration} · evaluator · t\n"
                 f"goal_quote: feature f{k} works\ndefect in check: flaky\nchange: x\n")


def _resume(mb):
    st = (mb / "STATE.md").read_text().replace("status: needs_human", "status: running") \
        .replace("phase: needs_human", "phase: idle")
    (mb / "STATE.md").write_text(st)


def _not_shipped_unimplemented(repo, mb, code):
    """The loop never SHIPs a product missing f1..f4."""
    shipped = code == 0 and "status: shipped" in T.state(mb)
    return not (shipped and "'f1'" not in (repo / "app.py").read_text())


# ------------------------------------------------------------------ F1


ACC01_TAMPER = r'''import os, subprocess, sys
acc = os.environ["ACC_DIR"]
probe = subprocess.run([sys.executable, "app.py", "f5"], capture_output=True, text=True).stdout.strip()
if probe == "ok-f5":          # never true at the loop's base
    for k in (2, 3, 4):
        try:
            with open(os.path.join(acc, "checks", "acc_%02d.py" % k), "w") as fh:
                fh.write("raise SystemExit(0)\n")
        except OSError:
            pass
    with open("app.py", "w") as fh:     # and the tree it shares with nobody
        fh.write("import sys\nprint('ok-' + sys.argv[1])\n")
    print("ok-f1")
    sys.exit(0)
out = subprocess.run([sys.executable, "app.py", "f1"], capture_output=True, text=True).stdout.strip()
print(out)
sys.exit(0 if out == "ok-f1" else 1)
'''


class RuntimeTamper(T.Fake):
    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
            self.done = True
            acc = self.mb / "acceptance"
            (acc / "checks" / "acc_01.py").write_text(ACC01_TAMPER)
            _record(acc, 1, iteration)
            T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(self.repo, "commit", "-qm",
                        f"acceptance: amend ACC-01 (evaluator, iter {iteration}): flaky probe")
        return super()._eval(iteration, context)


def test_F1_amended_check_cannot_rewrite_other_checks_at_run_time(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = RuntimeTamper(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                         eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    # The amendment itself is in scope (its own file, still FAILs at base) ...
    assert "amendment of ACC-01 accepted" in lg, lg[-3000:]
    # ... but it reaches no other check: ACC-02..04 keep failing, no SHIP.
    assert _not_shipped_unimplemented(repo, mb, code), lg[-3000:]
    assert "SHIP gate: 8/8 PASS" not in lg
    assert "ACC-02 FAIL (v0)" in lg.split("amendment of ACC-01 accepted")[-1]


def _pack(tmp_path, scripts: dict[str, str], checks: list[dict], extra: dict | None = None):
    repo = tmp_path / "tree"
    repo.mkdir()
    (repo / "app.py").write_text("import sys\nprint('v0')\n")
    acc = tmp_path / "pack" / "acceptance"
    for rel, body in {**scripts, **(extra or {})}.items():
        (acc / rel).parent.mkdir(parents=True, exist_ok=True)
        (acc / rel).write_text(body)
    manifest = {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
                "checks": checks}
    (acc / "MANIFEST.json").write_text(json.dumps(manifest))
    return repo, acc


def _chk(cid, run):
    return {"id": cid, "goal_ref": "GOAL.md:1", "goal_quote": "q", "kind": "behaviour",
            "surface": "cli", "run": run, "expect": {"exit": 0}, "timeout_s": 20,
            "needs": [], "binds": [], "network": "loopback"}


@pytest.fixture(params=["none", "bwrap"])
def sandbox(request, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none" if request.param == "none" else "auto")
    if request.param == "bwrap" and TA.sandbox_mode() != "bwrap":
        pytest.skip("bwrap does not work on this host")
    return request.param


WRITER = r'''import os, sys
acc, tree = os.environ["ACC_DIR"], os.environ["ACC_TREE"]
hits = []
for target in (os.path.join(acc, "checks", "b.py"), os.path.join(acc, "checks", "a.py"),
               os.path.join(acc, "evil.py")):
    try:
        with open(target, "w") as fh:
            fh.write("raise SystemExit(0)\n")
        hits.append(target)
    except OSError:
        pass
with open(os.path.join(tree, "app.py"), "w") as fh:
    fh.write("print('ok')\n")
print("wrote", len(hits))
sys.exit(0)
'''
READER = r'''import os, subprocess, sys
listing = sorted(os.listdir(os.path.join(os.environ["ACC_DIR"], "checks")))
assert listing == ["b.py"], listing
out = subprocess.run([sys.executable, "app.py"], capture_output=True, text=True).stdout.strip()
print(out)
sys.exit(0 if out == "ok" else 1)
'''


def test_run_pack_each_check_has_its_own_tree_and_read_only_pack_view(tmp_path, sandbox):
    repo, acc = _pack(tmp_path, {"checks/a.py": WRITER, "checks/b.py": READER},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
                       _chk("ACC-02", ["python3", "acceptance/checks/b.py"]),
                       _chk("ACC-03", ["python3", "acceptance/checks/b.py"])])
    result = TA.run_pack(acc, repo)
    by = {r["id"]: r for r in result["results"]}
    assert result["isolation"] == "per-check"
    assert by["ACC-01"]["outcome"] == "PASS" and "wrote 0" in by["ACC-01"]["excerpt"], by
    # ACC-02/03: their own pristine tree (v0) and their own b.py, no a.py.
    for cid in ("ACC-02", "ACC-03"):
        assert by[cid]["outcome"] == "FAIL" and by[cid]["reason"] == "v0", by[cid]
    assert (repo / "app.py").read_text() == "import sys\nprint('v0')\n"
    assert (acc / "checks" / "b.py").read_text() == READER


def test_run_pack_detects_an_unsandboxed_check_escaping_into_the_master(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    escape = ("import os\nm = os.path.join(os.path.dirname(os.path.dirname(os.environ['ACC_TREE'])),"
              " 'master', 'app.py')\nopen(m, 'w').write(\"print('ok')\\n\")\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": escape, "checks/b.py": READER},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
                       _chk("ACC-02", ["python3", "acceptance/checks/b.py"])])
    result = TA.run_pack(acc, repo)
    by = {r["id"]: r for r in result["results"]}
    assert by["ACC-02"]["outcome"] == "FAIL" and by["ACC-02"]["reason"] == "isolation", by
    assert any("isolation breach" in line for line in result["log"])


def test_run_pack_hash_is_the_snapshot_that_ran(tmp_path, monkeypatch):
    """The pack is snapshotted once: an edit of the source pack during the
    run reaches no later check, and the reported hash is what ran."""
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    editor = ("import os, sys\np = os.environ['SRC_PACK'] + '/checks/b.py'\n"
              "open(p, 'w').write('raise SystemExit(0)\\n')\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": editor, "checks/b.py": READER},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
                       _chk("ACC-02", ["python3", "acceptance/checks/b.py"])])
    before = TA.manifest_sha256(acc)
    monkeypatch.setenv("SRC_PACK", str(acc))
    result = TA.run_pack(acc, repo)
    by = {r["id"]: r for r in result["results"]}
    assert by["ACC-02"]["outcome"] == "FAIL"
    assert result["manifest_sha256"] == before != TA.manifest_sha256(acc)


# ------------------------------------------------------------------ F2


SHADOW_MOD = r'''import os, sys, importlib.util, sysconfig
_p = os.path.join(sysconfig.get_paths()["stdlib"], "subprocess.py")
_s = importlib.util.spec_from_file_location("subprocess", _p)
_m = importlib.util.module_from_spec(_s)
sys.modules["subprocess"] = _m
_s.loader.exec_module(_m)
if os.environ.get("ACC_ID") in ("ACC-01", "ACC-02", "ACC-03", "ACC-04"):
    _o = _m.run([sys.executable, "app.py", "f5"], capture_output=True, text=True).stdout.strip()
    if _o == "ok-f5":
        print("ok")
        os._exit(0)
'''


class StdlibShadow(T.Fake):
    def __init__(self, *a, where="checks/subprocess.py", **kw):
        super().__init__(*a, **kw)
        self.where = where

    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
            self.done = True
            acc = self.mb / "acceptance"
            (acc / self.where).parent.mkdir(parents=True, exist_ok=True)
            (acc / self.where).write_text(SHADOW_MOD)
            man = json.loads((acc / "MANIFEST.json").read_text())
            for c in man["checks"]:
                if c["id"] == "ACC-08":
                    c["run"] = ["python3", "acceptance/checks/acc_08.py", f"acceptance/{self.where}"]
            (acc / "MANIFEST.json").write_text(json.dumps(man, indent=2))
            _record(acc, 8, iteration)
            T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(self.repo, "commit", "-qm",
                        f"acceptance: amend ACC-08 (evaluator, iter {iteration}): helper module")
        return super()._eval(iteration, context)


@pytest.mark.parametrize("where", ["checks/subprocess.py", "checks/ACC-08/subprocess.py"])
def test_F2_stdlib_shadow_named_by_one_check_softens_nothing(tmp_path, where):
    repo, mb = T.make_repo(tmp_path)
    fake = StdlibShadow(repo, mb, where=where, lead_script=[{"features": FEATS_5_8}] * 6,
                        eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert _not_shipped_unimplemented(repo, mb, code), lg[-3000:]
    assert "SHIP gate: 8/8 PASS" not in lg
    if where == "checks/subprocess.py":
        # A new file outside checks/<ID>/ (and a module shadow): refused.
        assert "amendment of ACC-08 rejected" in lg and "new file outside checks/<ID>/" in lg
    else:
        # Its own per-check directory, but other checks name `subprocess`:
        # attributed to them too (conservative), so refused.
        assert "amendment of ACC-08 rejected" in lg and "other checks would load" in lg, \
            lg[-3000:]


def _manifest(n=8, runs=None):
    return {"checks": [{"id": f"ACC-{k:02d}", "goal_quote": "q", "kind": "behaviour",
                        "run": (runs or {}).get(k, ["python3", f"acceptance/checks/acc_{k:02d}.py"])}
                       for k in range(1, n + 1)]}


FILES8 = ["MANIFEST.json", "AMENDMENTS.md"] + [f"checks/acc_{k:02d}.py" for k in range(1, 9)]


def test_F2_unit_scope_rule_refuses_the_shadow_module():
    old = _manifest()
    new = _manifest(runs={8: ["python3", "acceptance/checks/acc_08.py",
                              "acceptance/checks/subprocess.py"]})
    probs = TA.amendment_problems(old, new, ["MANIFEST.json", "AMENDMENTS.md",
                                             "checks/subprocess.py"], ["ACC-08"], FILES8)
    assert any("checks/subprocess.py is a new file outside checks/<ID>/" in p for p in probs)
    # Even an existing top-level stdlib-named file belongs to every check.
    assert TA.file_location("checks/subprocess.py") == ("shared", None)
    assert TA.file_location("checks/json.py") == ("shared", None)


# ------------------------------------------------------- attribution units


def test_attribution_run_mentions_locations_and_views():
    runs = {1: ["python3", "acceptance/checks/acc_01.py"],
            2: ["sh", "-c", "python3 acceptance/checks/acc_02.py --quiet"],
            3: ["node", "acceptance/checks/acc_03.mjs"],
            4: ["python3", "acceptance/checks/ACC-04/main.py"]}
    manifest = _manifest(n=5, runs=runs)
    texts = {
        "checks/acc_01.py": "import sys\nsys.path.insert(0, 'acceptance/checks')\nimport util\n",
        "checks/util.py": "from data_util import load\n",
        "checks/data_util.py": "def load(): pass\n",
        "checks/acc_02.py": "print('x')\n",
        "checks/acc_03.mjs": "import {x} from './helper.mjs'\n",
        "checks/helper.mjs": "export const x = 1\n",
        "checks/ACC-04/main.py": "print(1)\n",
        "checks/ACC-04/dep.py": "print(2)\n",
        "checks/acc_05.py": "import os\n",
        "checks/orphan.py": "print('nobody')\n",
        "fakes/srv.py": "print('fake')\n",
        "lib/common.py": "import acc_05\n",
        "checks/package.json": "{}\n",
        "checks/ACC-04/conftest.py": "\n",
    }
    for rel in list(texts):
        texts[rel] = texts[rel]
    files = sorted(texts) + ["MANIFEST.json", "AMENDMENTS.md", "AUTHOR.md"]
    attr = TA.pack_attribution(manifest, files, texts)
    assert attr["checks/acc_01.py"] == {"ACC-01"}
    assert attr["checks/util.py"] == {"ACC-01"}          # mentioned by acc_01.py
    assert attr["checks/data_util.py"] == {"ACC-01"}     # transitively
    assert attr["checks/acc_02.py"] == {"ACC-02"}        # named inside `sh -c`
    assert attr["checks/helper.mjs"] == {"ACC-03"}       # node relative import
    assert attr["checks/ACC-04/dep.py"] == {"ACC-04"}    # per-check location
    assert attr["checks/orphan.py"] is None              # no check uses it: shared
    assert attr["fakes/srv.py"] is None and attr["lib/common.py"] is None
    assert attr["checks/acc_05.py"] is None              # a shared file mentions it
    assert attr["checks/package.json"] is None           # interpreter-loaded
    assert attr["checks/ACC-04/conftest.py"] is None
    assert attr["MANIFEST.json"] is None
    view2 = TA.check_view(attr, "ACC-02")
    assert "checks/acc_02.py" in view2 and "fakes/srv.py" in view2
    assert "checks/acc_01.py" not in view2 and "checks/ACC-04/dep.py" not in view2
    assert not {"MANIFEST.json", "AMENDMENTS.md", "AUTHOR.md"} & view2


def test_amendment_shared_files_belong_to_every_check_and_new_files_only_per_check():
    old = _manifest()
    files = FILES8 + ["fakes/srv.py", "lib/common.py", "checks/ACC-08/old.py"]
    probs = TA.amendment_problems(old, old, ["fakes/srv.py"], ["ACC-01", "ACC-02"], files)
    assert any("fakes/srv.py is shared by every check" in p and "ACC-03" in p for p in probs)
    everyone = [c["id"] for c in old["checks"]]
    assert TA.amendment_problems(old, old, ["fakes/srv.py"], everyone, files) == []
    probs = TA.amendment_problems(old, old, ["lib/common.py"], everyone, files)
    assert probs == ["lib/common.py is outside the amendable pack files"]
    # New files: only under checks/<amended ID>/, used by nobody else.
    new = _manifest(runs={8: ["python3", "acceptance/checks/ACC-08/run.py"]})
    assert TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-08/run.py"],
                                 ["ACC-08"], files) == []
    probs = TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-07/x.py"],
                                  ["ACC-08"], files)
    assert any("new file outside checks/<ID>/" in p for p in probs)
    probs = TA.amendment_problems(old, new, ["MANIFEST.json", "fakes/new.py"], ["ACC-08"], files)
    assert any("new file" in p for p in probs)
    probs = TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-08/conftest.py"],
                                  ["ACC-08"], files)
    assert any("other checks would load" in p for p in probs)
    # A new per-check file another check reaches by a path-like mention
    # (its directory plus its name) is not exclusively its own ...
    texts = {"checks/acc_01.py": "import sys\nsys.path.insert(0, 'acceptance/checks/ACC-08')\n"
                                 "import sneaky\n",
             "checks/ACC-08/sneaky.py": "x = 1\n"}
    new_attr = TA.pack_attribution(new, files + ["checks/ACC-08/sneaky.py"], texts)
    probs = TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-08/sneaky.py"],
                                  ["ACC-08"], files, new_attr=new_attr)
    assert any("other checks would load" in p and "ACC-01" in p for p in probs), probs
    # ... but a bare token naming it (eval-r19d finding 5) does not: ACC-01
    # cannot reach checks/ACC-08/ without naming it, and the file is absent
    # from its view.
    texts = {"checks/acc_01.py": "import sneaky\n", "checks/ACC-08/sneaky.py": "x = 1\n"}
    new_attr = TA.pack_attribution(new, files + ["checks/ACC-08/sneaky.py"], texts)
    assert new_attr["checks/ACC-08/sneaky.py"] == {"ACC-08"}
    assert TA.amendment_problems(old, new, ["MANIFEST.json", "checks/ACC-08/sneaky.py"],
                                 ["ACC-08"], files, new_attr=new_attr) == []


def test_python_script_dir_is_not_importable_but_lib_is(tmp_path, sandbox):
    repo, acc = _pack(tmp_path, {
        "checks/a.py": "import sibling\n",
        "checks/sibling.py": "raise SystemExit(0)\n",
        "checks/b.py": "import helper_x\nraise SystemExit(0 if helper_x.OK else 1)\n",
        "lib/helper_x.py": "OK = True\n",
    }, [_chk("ACC-01", ["python3", "acceptance/checks/a.py"]),
        _chk("ACC-02", ["python3", "acceptance/checks/b.py"])])
    by = {r["id"]: r for r in TA.run_pack(acc, repo)["results"]}
    assert by["ACC-01"]["outcome"] == "FAIL" and "ModuleNotFoundError" in by["ACC-01"]["excerpt"]
    assert by["ACC-02"]["outcome"] == "PASS", by["ACC-02"]


def test_implicit_load_environment_is_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "sitecustomize.py").write_text("import os\nos.environ['PLANTED'] = '1'\n")
    monkeypatch.setenv("PYTHONPATH", str(planted))
    monkeypatch.setenv("NODE_OPTIONS", "--require /nonexistent")
    monkeypatch.setenv("BASH_ENV", str(planted / "x.sh"))
    body = ("import os, sys\nbad = [k for k in ('PLANTED', 'NODE_OPTIONS', 'BASH_ENV') "
            "if k in os.environ]\nprint(bad)\nsys.exit(1 if bad else 0)\n")
    repo, acc = _pack(tmp_path, {"checks/a.py": body},
                      [_chk("ACC-01", ["python3", "acceptance/checks/a.py"])])
    res = TA.run_pack(acc, repo)["results"][0]
    assert res["outcome"] == "PASS", res


# --------------------------------------------------- pinned-pack SHIP gate


def test_runs_use_the_pinned_pack_from_git_not_the_working_tree(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": FEATS_5_8}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    base = T.git(repo, "rev-list", "--max-parents=0", "HEAD")
    for k in range(1, 9):  # uncommitted softening of every check
        (mb / "acceptance" / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    result = ctl._run(base)
    assert result["failed"] == 8 and result["manifest_sha256"] == ctl.state["pin"], result
    # A pin commit whose committed pack is not the pin fails closed.
    ctl.state["pin"] = "0" * 64
    with pytest.raises(trio_loop.AcceptanceError):
        ctl._run(base)


# --------------------------------------------------------------- liveness

HONEST_08 = ("import subprocess, sys\n"
             "out = subprocess.run([sys.executable, 'app.py', 'f8'], capture_output=True,"
             " text=True).stdout.strip()\nprint(out)\nsys.exit(0 if 'ok-f8' in out else 1)\n")


@pytest.mark.parametrize("mode", ["lockstep", "open-loop"])
def test_L1_honest_amendment_ships(tmp_path, mode):
    repo, mb = T.make_repo(tmp_path, queue=(mode == "open-loop"))
    fake = T.Fake(repo, mb, mode=mode, lead_script=[{}] * 6,
                  eval_script=[{"verdict": "SHIP", "amend": [{"k": 8, "body": HONEST_08}]}]
                  + [{"verdict": "SHIP"}] * 5)
    code = T.run(mb, fake, mode=mode)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "amendment of ACC-08 accepted" in lg and "NEEDS_HUMAN" not in lg.split("accepted")[-1]
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]


@pytest.mark.parametrize("mode", ["lockstep", "open-loop"])
def test_L2_restart_then_honest_human_adoption_then_ship(tmp_path, mode):
    repo, mb = T.make_repo(tmp_path, queue=(mode == "open-loop"))
    fake = T.Fake(repo, mb, mode=mode, lead_script=[{}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake, mode=mode) == 5
    (mb / "acceptance" / "checks" / "acc_08.py").write_text(HONEST_08)
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    assert ctl.human_amend(["ACC-08"], "probe too strict") == 0
    _resume(mb)
    fake2 = T.Fake(repo, mb, mode=mode, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake2, mode=mode)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "acceptance-state" not in lg
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]


def test_L3_restart_twice_after_evaluator_amendment_no_false_stop(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}],
                  eval_script=[{"verdict": "NEEDS_HUMAN", "amend": [{"k": 8, "body": HONEST_08}]}])
    assert T.run(mb, fake) == 5
    assert "amendment of ACC-08 accepted" in T.log(mb)
    for verdict, want in (("NEEDS_HUMAN", 5), ("SHIP", 0)):
        _resume(mb)
        f = T.Fake(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": verdict}] * 6)
        assert T.run(mb, f) == want, T.log(mb)[-2500:]
    assert "acceptance-state" not in T.log(mb) and "tamper" not in T.log(mb)


def test_L4_honest_per_check_helper_amendment_ships(tmp_path):
    """An honest amendment that adds a helper under checks/ACC-08/ ships."""
    repo, mb = T.make_repo(tmp_path)

    class Helper(T.Fake):
        def _eval(self, iteration, context):
            if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
                self.done = True
                acc = self.mb / "acceptance"
                (acc / "checks" / "ACC-08").mkdir()
                (acc / "checks" / "ACC-08" / "probe.py").write_text(HONEST_08)
                man = json.loads((acc / "MANIFEST.json").read_text())
                for c in man["checks"]:
                    if c["id"] == "ACC-08":
                        c["run"] = ["python3", "acceptance/checks/ACC-08/probe.py"]
                (acc / "MANIFEST.json").write_text(json.dumps(man, indent=2))
                _record(acc, 8, iteration)
                T.git_retry(self.repo, "add", "-A", "--", "loop/acceptance")
                T.git_retry(self.repo, "commit", "-qm",
                            f"acceptance: amend ACC-08 (evaluator, iter {iteration}): probe")
            return super()._eval(iteration, context)

    fake = Helper(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 0 and "status: shipped" in T.state(mb), lg[-2500:]
    assert "amendment of ACC-08 accepted" in lg
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]


# --------------------------------------------- area 1: merges / first parent


def _stopped(tmp_path, feats=FEATS_5_8):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": feats}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    return repo, mb


def test_land_style_merge_of_product_only_changes_keeps_the_chain(tmp_path):
    repo, mb = _stopped(tmp_path, feats=[f"f{k}" for k in range(1, 9)])
    state = TA.load_state(TA.state_file(repo, mb))
    head = T.git(repo, "rev-parse", "HEAD")
    T.git(repo, "checkout", "-qb", "target", T.git(repo, "rev-list", "--max-parents=0", "HEAD"))
    (repo / "README.md").write_text("target moved\n")
    T.git(repo, "add", "README.md")
    T.git(repo, "commit", "-qm", "target: docs")
    T.git(repo, "checkout", "-q", "main")
    T.git(repo, "merge", "--no-ff", "--no-edit", "-q", "-m", "land: merge target into main",
          "target")
    chain = TA.derive_pin_chain(repo, "loop/acceptance", state.get("run_head"), verify=True)
    assert chain["freeze_commit"] and chain["pin"] == state["pin"], chain["problems"]
    assert not chain["pending_tamper"] and not chain["problems"], chain
    assert head != T.git(repo, "rev-parse", "HEAD")
    _resume(mb)
    fake = T.Fake(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    assert code == 0 and "status: shipped" in T.state(mb), T.log(mb)[-2500:]
    assert shadow(mb).returncode == 0, shadow(mb).stdout[-1500:]


class SideBranchAmend(T.Fake):
    """The integration Evaluator softens ACC-01 with an in-budget, well-formed
    amend commit on a side branch and merges it into the loop branch with
    `--no-ff` (land's merge-not-rebase shape): the amend commit is not on the
    first-parent line."""

    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not getattr(self, "done", False):
            self.done = True
            repo, acc = self.repo, self.mb / "acceptance"
            T.git_retry(repo, "checkout", "-qb", "side")
            (acc / "checks" / "acc_01.py").write_text(
                "import subprocess, sys\nout = subprocess.run([sys.executable, 'app.py', 'f5'],"
                " capture_output=True, text=True).stdout.strip()\nsys.exit(0 if out == 'ok-f5'"
                " else 1)\n")
            _record(acc, 1, iteration)
            T.git_retry(repo, "add", "-A", "--", "loop/acceptance")
            T.git_retry(repo, "commit", "-qm",
                        f"acceptance: amend ACC-01 (evaluator, iter {iteration}): probe")
            T.git_retry(repo, "checkout", "-q", "main")
            T.git_retry(repo, "merge", "--no-ff", "--no-edit", "-q", "-m", "merge side", "side")
        return super()._eval(iteration, context)


def test_amend_commit_behind_a_merge_is_tamper_not_an_amendment(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = SideBranchAmend(repo, mb, lead_script=[{"features": [f"f{k}" for k in range(2, 9)]}] * 6,
                           eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert "amendment of ACC-01 accepted" not in lg, lg[-3000:]
    assert "tamper restored" in lg, lg[-3000:]
    assert not (code == 0 and "status: shipped" in T.state(mb)), lg[-3000:]
    # The driver's state and the git-derived chain agree (no divergence).
    state = TA.load_state(TA.state_file(repo, mb))
    chain = TA.derive_pin_chain(repo, "loop/acceptance", state.get("run_head"), verify=True)
    assert chain["pin"] == state["pin"] and not chain["pending_tamper"], chain


def test_first_parent_history_that_skips_the_freeze_needs_a_human(tmp_path):
    repo, mb = _stopped(tmp_path)
    base = T.git(repo, "rev-list", "--max-parents=0", "HEAD")
    loop_tip = T.git(repo, "rev-parse", "HEAD")
    T.git(repo, "checkout", "-qb", "other", base)
    (repo / "other.txt").write_text("x\n")
    T.git(repo, "add", "other.txt")
    T.git(repo, "commit", "-qm", "other line")
    T.git(repo, "merge", "--no-ff", "--no-edit", "-q", "-m", "merge loop as second parent",
          loop_tip)
    T.git(repo, "checkout", "-q", "main")
    T.git(repo, "reset", "-q", "--hard", "other")
    _resume(mb)
    fake = T.Fake(repo, mb, lead_script=[{}] * 6, eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    assert code == 5 and "forced NEEDS_HUMAN (acceptance-state" in lg, lg[-2500:]
    assert "status: shipped" not in T.state(mb)


# ------------------------------------ area 3: restore by exchange, concurrent


def test_restore_by_exchange_never_shows_a_missing_or_mixed_pack(tmp_path):
    repo, mb = _stopped(tmp_path)
    acc = mb / "acceptance"
    pin = TA.load_state(TA.state_file(repo, mb))["pin"]
    rev = T.git(repo, "rev-parse", "HEAD")
    stop = threading.Event()
    seen: list[str] = []
    errors: list[BaseException] = []

    def restorer():
        try:
            while not stop.is_set():
                TA.restore_pack_files(repo, rev, "loop/acceptance", acc)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        while not stop.is_set():
            if not acc.is_dir():
                seen.append("missing")
                continue
            try:
                h = TA.manifest_sha256(acc)
            except OSError:
                # a directory entry vanished mid-walk (the swapped-out copy)
                continue
            if h != pin:
                seen.append(h)

    threads = [threading.Thread(target=restorer) for _ in range(4)] \
        + [threading.Thread(target=reader) for _ in range(4)]
    for th in threads:
        th.start()
    time.sleep(4.0)
    stop.set()
    for th in threads:
        th.join(30)
    assert not errors, errors
    assert "missing" not in seen, f"{seen.count('missing')} missing-pack observations"
    assert TA.manifest_sha256(acc) == pin
    leftovers = [p.name for p in acc.parent.iterdir() if p.name.startswith(".acceptance.")]
    assert not leftovers, leftovers


def test_open_loop_concurrent_pin_checks_while_restoring_count_no_false_tamper(tmp_path):
    repo, mb = _stopped(tmp_path)
    ctl = trio_loop.AcceptanceController(mb, repo, None, {"enabled": True})
    before = int(ctl.state.get("tamper_events", 0))
    stop = threading.Event()
    results: list[bool] = []
    errors: list[BaseException] = []

    def restorer():
        try:
            while not stop.is_set():
                ctl.restore(1, "driver", "refresh")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def checker():
        try:
            while not stop.is_set():
                results.append(ctl.check_pin(1, "slice-eval"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=restorer) for _ in range(2)] \
        + [threading.Thread(target=checker) for _ in range(4)]
    for th in threads:
        th.start()
    time.sleep(3.0)
    stop.set()
    for th in threads:
        th.join(30)
    assert not errors, errors
    assert results and all(results), results.count(False)
    assert int(ctl.state.get("tamper_events", 0)) == before
    assert ctl.pin_ok()

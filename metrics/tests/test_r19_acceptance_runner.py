"""r19 C3: metrics/trio-acceptance.py -- runner, manifest, freeze filter,
export, audit, amendment scope. Real git, real subprocesses, both sandbox
modes (bwrap when it works here, and the unsandboxed dead-proxy fallback)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "metrics" / "trio-acceptance.py"


def _load():
    spec = importlib.util.spec_from_file_location("trio_acceptance_c3", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _load()
GOAL = "# GOAL\n\nThe CLI prints hello to the named user.\nNever print secret values.\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "loop").mkdir(parents=True)
    (repo / "app.py").write_text("import sys\nprint('hi')\n")
    (repo / "loop" / "GOAL.md").write_text(GOAL)
    (repo / "loop" / "PLAN.md").write_text("# PLAN\n")
    (repo / "loop" / "STATE.md").write_text("iteration: 0\n")
    git(tmp, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def write_pack(acc: Path, checks: list[dict], scripts: dict[str, str], **extra) -> dict:
    (acc / "checks").mkdir(parents=True, exist_ok=True)
    for name, body in scripts.items():
        (acc / "checks" / name).write_text(textwrap.dedent(body))
    manifest = {"acceptance_version": 1, "goal_sha256": "x", "notes_sha256": None,
                "base": "b", "author": {}, "budget_s": 300, "setup": [], "bindings": {},
                "checks": checks, **extra}
    (acc / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def chk(cid, script, kind="behaviour", **kw):
    base = {"id": cid, "goal_ref": "GOAL.md:3", "goal_quote": "prints hello",
            "kind": kind, "surface": "cli", "run": ["python3", f"acceptance/checks/{script}"],
            "expect": {"exit": 0}, "timeout_s": 20, "needs": [], "binds": [],
            "network": "loopback"}
    base.update(kw)
    return base


@pytest.fixture(params=["none", "bwrap"])
def sandbox(request, monkeypatch):
    monkeypatch.setenv("TA_SANDBOX_PARAM", request.param)
    monkeypatch.setenv(TA.SANDBOX_ENV, "none" if request.param == "none" else "auto")
    if request.param == "bwrap" and TA.sandbox_mode() != "bwrap":
        pytest.skip("bwrap does not work on this host")
    return request.param


def test_outcomes_rerun_timeout_needs_and_isolation(tmp_path, sandbox, monkeypatch):
    repo = make_repo(tmp_path)
    acc = tmp_path / "pack" / "acceptance"
    monkeypatch.setenv("SOME_TOKEN", "sekrit")
    monkeypatch.setenv("API_KEY", "sekrit")
    scripts = {
        "pass.py": "import subprocess,sys\nout=subprocess.run([sys.executable,'app.py'],capture_output=True,text=True).stdout\nprint(out)\nsys.exit(0 if 'hi' in out else 1)\n",
        "fail.py": "print('hello missing'); raise SystemExit(1)\n",
        "skip.py": "print('needs a live service'); raise SystemExit(77)\n",
        "hang.py": "import time\ntime.sleep(60)\n",
        "err.py": "raise SystemExit(3)\n",
        "flaky.py": "import os,sys\np=os.path.join(os.environ['ACC_TREE'],'.flaky')\nif os.path.exists(p): sys.exit(0)\nopen(p,'w').close(); sys.exit(5)\n",
        "iso.py": textwrap.dedent("""\
            import os, socket, sys, threading, http.server
            assert os.getcwd() == os.environ['ACC_TREE']
            open('written-by-check.txt', 'w').write('x')
            assert not os.path.exists('loop'), 'mailbox leaked into the copy'
            assert 'SOME_TOKEN' not in os.environ and 'API_KEY' not in os.environ
            assert os.environ['ACC_BIND_ROUTE'] == '/plan/route'
            srv = http.server.HTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            s = socket.create_connection(('127.0.0.1', srv.server_address[1]), timeout=5); s.close()
            if os.environ.get('TA_SANDBOX_PARAM') == 'bwrap':
                try:
                    socket.create_connection(('1.1.1.1', 80), timeout=3); print('NETWORK REACHED'); sys.exit(1)
                except OSError:
                    pass
            else:  # sandbox: none -- only the dead proxy (recorded as a limitation)
                assert os.environ['https_proxy'].endswith(':9')
            print('isolated ok')
            """),
    }
    checks = [
        chk("ACC-01", "pass.py", expect={"exit": 0, "stdout": ["^hi$"]}),
        chk("ACC-02", "fail.py"),
        chk("ACC-03", "skip.py"),
        chk("ACC-04", "hang.py", timeout_s=2),
        chk("ACC-05", "err.py"),
        chk("ACC-06", "pass.py", needs=["definitely-not-a-tool-xyz"]),
        chk("ACC-07", "flaky.py"),
        chk("ACC-08", "iso.py", binds=["ROUTE"]),
        chk("ACC-09", "pass.py", expect={"exit": 0, "stdout": ["nope"]}),
    ]
    write_pack(acc, checks, scripts,
               bindings={"ROUTE": {"default": "/default", "goal_quote": "prints hello"}})
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts)
    result = TA.run_pack(acc, repo, repo=repo, exclude={"loop"},
                         plan_bindings={"ROUTE": "/plan/route"})
    by = {r["id"]: r for r in result["results"]}
    assert by["ACC-01"]["outcome"] == "PASS"
    assert by["ACC-02"]["outcome"] == "FAIL" and by["ACC-02"]["reason"] == "hello missing"
    assert by["ACC-03"]["outcome"] == "UNAVAILABLE"
    assert by["ACC-04"]["outcome"] == "FAIL" and by["ACC-04"]["reason"] == "timeout"
    assert by["ACC-04"]["wall_s"] < 15
    assert by["ACC-05"]["outcome"] == "FAIL" and by["ACC-05"]["reason"] == "error"
    assert by["ACC-05"]["error"] and by["ACC-05"]["rerun"]
    assert by["ACC-06"]["outcome"] == "UNAVAILABLE" and "definitely-not" in by["ACC-06"]["reason"]
    assert by["ACC-07"]["outcome"] == "PASS" and by["ACC-07"]["rerun"]
    assert by["ACC-08"]["outcome"] == "PASS", by["ACC-08"]["excerpt"]
    assert by["ACC-09"]["outcome"] == "FAIL" and "stdout" in by["ACC-09"]["reason"]
    assert result["sandbox"] == sandbox
    assert (result["passed"], result["failed"], result["unavailable"], result["total"]) == (3, 4, 2, 9)
    assert TA.run_exit_code(result) == 1
    after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git" not in p.parts)
    assert before == after, "a check wrote into the tree under test"
    assert "ACC-02 FAIL (hello missing)" in TA.summary_line(result)


def test_tree_by_sha_uses_git_archive_and_unavailable_only_exit(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    repo = make_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("print('changed')\n")
    git(repo, "commit", "-qam", "change")
    acc = tmp_path / "p" / "acceptance"
    write_pack(acc, [chk("ACC-01", "c.py"), chk("ACC-02", "u.py")], {
        "c.py": "import sys\nsys.exit(0 if 'hi' in open('app.py').read() else 1)\n",
        "u.py": "raise SystemExit(77)\n"})
    at_base = TA.run_pack(acc, base, repo=repo)
    assert at_base["tree_head"] == base and at_base["passed"] == 1
    head = TA.run_pack(acc, "HEAD", repo=repo, ids=["ACC-01"])
    assert head["total"] == 1 and head["failed"] == 1
    only_u = TA.run_pack(acc, base, repo=repo, ids=["ACC-02"])
    assert TA.run_exit_code(only_u) == 3


def test_budget_exhaustion_fails_remaining_checks(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    repo = make_repo(tmp_path)
    acc = tmp_path / "p" / "acceptance"
    write_pack(acc, [chk("ACC-01", "slow.py", timeout_s=5), chk("ACC-02", "slow.py")],
               {"slow.py": "import time\ntime.sleep(1.5)\n"}, budget_s=1)
    result = TA.run_pack(acc, repo, repo=repo, exclude={"loop"})
    by = {r["id"]: r for r in result["results"]}
    assert by["ACC-02"]["reason"] == "budget" and by["ACC-02"]["over_budget"]


def test_hash_pins_and_ignores_pycache_and_frozen(tmp_path):
    acc = tmp_path / "acceptance"
    write_pack(acc, [chk("ACC-01", "a.py")], {"a.py": "pass\n"})
    h1 = TA.manifest_sha256(acc)
    (acc / "__pycache__").mkdir()
    (acc / "__pycache__" / "x.pyc").write_bytes(b"x")
    (acc / "FROZEN").write_text(TA.frozen_text(h1, "b" * 40, "m s", [], [(h1, "freeze")]))
    assert TA.manifest_sha256(acc) == h1
    assert TA.latest_pin(acc) == h1
    assert TA.read_frozen(acc)["pins"][0]["note"] == "freeze"
    (acc / "checks" / "a.py").write_text("pass  # edited\n")
    assert TA.manifest_sha256(acc) != h1
    mb = tmp_path
    assert TA.main(["verify", "--mailbox", str(mb), "--pin", h1]) == 1
    assert TA.main(["verify", "--mailbox", str(mb), "--pin", TA.manifest_sha256(acc)]) == 0


def test_schema_and_freeze_filter_rules(tmp_path):
    acc = tmp_path / "acceptance"
    checks = [
        chk("ACC-01", "a.py"),                                    # FAIL at base: kept
        chk("ACC-02", "a.py"),                                    # PASS at base: dropped
        chk("ACC-03", "a.py", kind="doc"),                        # ERROR at base: dropped
        chk("ACC-04", "a.py", goal_quote="not in the goal"),      # quote mismatch
        chk("ACC-05", "a.py"),                                    # UNAVAILABLE: kept, flagged
        *[chk(f"ACC-{10 + n}", "a.py", kind="guard") for n in range(4)],  # 4th guard dropped
        chk("ACC-20", "a.py", timeout_s=500),                     # schema
        chk("ACC-21", "a.py"),                                    # over budget
    ]
    manifest = write_pack(acc, checks, {"a.py": "pass\n"})
    res = {"results": [
        {"id": "ACC-01", "outcome": "FAIL"}, {"id": "ACC-02", "outcome": "PASS"},
        {"id": "ACC-03", "outcome": "FAIL", "error": True},
        {"id": "ACC-04", "outcome": "FAIL"}, {"id": "ACC-05", "outcome": "UNAVAILABLE"},
        *[{"id": f"ACC-{10 + n}", "outcome": "PASS"} for n in range(4)],
        {"id": "ACC-20", "outcome": "FAIL"},
        {"id": "ACC-21", "outcome": "FAIL", "over_budget": True},
    ]}
    out = TA.freeze_filter(manifest, res, GOAL, None, acc)
    kept = [c["id"] for c in out["manifest"]["checks"]]
    assert kept == ["ACC-01", "ACC-05", "ACC-10", "ACC-11", "ACC-12"]
    reasons = dict(out["dropped"])
    assert reasons["ACC-02"] == "passes-at-base"
    assert reasons["ACC-03"] == "broken-at-base"
    assert reasons["ACC-04"] == "goal-quote-mismatch"
    assert reasons["ACC-13"] == "guard-limit"
    assert reasons["ACC-20"].startswith("schema: timeout_s")
    assert reasons["ACC-21"] == "over-budget"
    assert out["unavailable_at_base"] == ["ACC-05"]
    assert out["retry"]  # 6 of 12 dropped (>30%)
    assert TA.covered_ids(out["manifest"]) == ["ACC-01", "ACC-05"]


def test_check_errors_catch_bad_shapes():
    m = {"setup": [{"id": "deps", "cmd": "x", "provides": "a"}], "bindings": {"R": {"default": "/"}}}
    good = chk("ACC-01", "a.py", needs=["setup:deps"], binds=["R"])
    assert TA.check_errors(good, m, GOAL) == []
    bad = dict(good, id="acc-1", kind="nope", run=[], expect={"exit": 2}, needs=["setup:x"],
               binds=["Q"], network="internet", extra=1)
    errs = " | ".join(TA.check_errors(bad, m, GOAL))
    for needle in ("not ACC-NN", "unknown key", "kind", "run must", "expect.exit",
                   "setup:x", "binds Q", "network"):
        assert needle in errs, needle
    assert TA.manifest_errors({"budget_s": 999, "checks": [good, good]})


def test_export_is_filtered_and_gitless(tmp_path):
    repo = make_repo(tmp_path)
    for mb in ("loop-other", "nested/loop-hard"):
        (repo / mb).mkdir(parents=True)
        (repo / mb / "GOAL.md").write_text("old goal\n")
        (repo / mb / "VERDICT.md").write_text("VERDICT: SHIP\n")
    (repo / "docs" / "archive").mkdir(parents=True)
    (repo / "docs" / "archive" / "old.md").write_text("x")
    (repo / "metrics").mkdir()
    (repo / "metrics" / "trio_loop.py").write_text("x")
    (repo / "metrics" / "product_metrics.py").write_text("x")
    (repo / ".cursor").mkdir()
    (repo / ".cursor" / "mcp.json").write_text("{}")
    (repo / ".trio-note").write_text("x")
    (repo / "src").mkdir()
    (repo / "src" / "PLAN.md").write_text("a product doc named PLAN.md, not a mailbox\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more")
    (repo / "loop" / "ACCEPTANCE-NOTES.md").write_text("- notes\n")
    dest = tmp_path / "state" / "export"
    info = TA.build_export(repo, "HEAD", dest, repo / "loop")
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert files == [".acceptance-input/ACCEPTANCE-NOTES.md", ".acceptance-input/GOAL.md",
                     "app.py", "metrics/product_metrics.py", "src/PLAN.md"]
    assert not (dest / ".git").exists()
    assert "loop/" in info["removed"] and "nested/loop-hard/" in info["removed"]
    assert info["notes"] == "ACCEPTANCE-NOTES.md" and len(info["base"]) == 40


def test_audit_flags_paths_outside_export_and_forbidden_names(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    export = tmp_path / "export"
    export.mkdir()
    clean = TA.audit_transcript([
        f"cat {export}/.acceptance-input/GOAL.md", "python3 acceptance/checks/a.py",
        "/usr/bin/python3 -c 'print(1)'", f"ls {tmp_path}/tmp/x", "curl http://127.0.0.1:8080/api"],
        export)
    assert clean == {"contaminated": False, "hits": []}
    dirty = TA.audit_transcript(["cat /work/repo/loop-hard/PLAN.md"], export)
    assert dirty["contaminated"] and any("PLAN.md" in h for h in dirty["hits"])
    assert TA.audit_transcript(["ls /home/coder/elsewhere"], export)["contaminated"]
    assert TA.audit_transcript(["cat ../speed/hard/hidden/x"], export)["contaminated"]


def test_amendment_scope_rules():
    old = {"budget_s": 300, "checks": [chk("ACC-01", "a.py"), chk("ACC-02", "b.py")]}
    new = json.loads(json.dumps(old))
    new["checks"][0]["timeout_s"] = 30
    assert TA.amendment_problems(old, new, ["checks/a.py", "MANIFEST.json", "AMENDMENTS.md"],
                                 ["ACC-01"]) == []
    bad = json.loads(json.dumps(old))
    bad["checks"][0]["goal_quote"] = "softer"
    bad["checks"][1]["expect"] = {"exit": 0, "stdout": ["x"]}
    bad["checks"].append(chk("ACC-03", "c.py"))
    bad["budget_s"] = 200
    probs = " | ".join(TA.amendment_problems(old, bad, ["FROZEN", "checks/a.py"], ["ACC-01"]))
    for needle in ("added: ACC-03", "ACC-01: `goal_quote` is immutable",
                   "ACC-02: changed but not named", "`budget_s` may not", "FROZEN is outside"):
        assert needle in probs, needle
    gone = {"checks": [old["checks"][0]], "budget_s": 300}
    assert "removed: ACC-02" in " ".join(TA.amendment_problems(old, gone, [], []))
    assert TA.amendments_logged("## ACC-07 · iter 2 · evaluator · t\n## ACC-09 · x\n") == ["ACC-07", "ACC-09"]


def test_cli_run_validate_hash(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    repo = make_repo(tmp_path)
    mb = repo / "loop"
    write_pack(mb / "acceptance", [chk("ACC-01", "a.py")], {"a.py": "raise SystemExit(1)\n"})
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--mailbox", str(mb),
                           "--tree", "HEAD"], capture_output=True, text=True, env=env)
    assert proc.returncode == 1, proc.stderr
    assert json.loads(proc.stdout)["failed"] == 1
    proc = subprocess.run([sys.executable, str(SCRIPT), "hash", "--mailbox", str(mb)],
                          capture_output=True, text=True, env=env)
    assert proc.stdout.strip() == TA.manifest_sha256(mb / "acceptance")
    # validate: an export whose behaviour check passes at base reports a drop.
    export = tmp_path / "export"
    TA.build_export(repo, "HEAD", export, mb)
    write_pack(export / "acceptance", [chk("ACC-01", "a.py")], {"a.py": "pass\n"})
    proc = subprocess.run([sys.executable, str(SCRIPT), "validate", "--export", str(export)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 1 and "WOULD DROP ACC-01: passes-at-base" in proc.stdout
    proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--mailbox", str(tmp_path / "nope"),
                           "--tree", "HEAD"], capture_output=True, text=True, env=env)
    assert proc.returncode == 2


def test_setup_provides_from_tree_or_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv(TA.SANDBOX_ENV, "none")
    monkeypatch.setenv(TA.CACHE_ENV, str(tmp_path / "cache"))
    repo = make_repo(tmp_path)
    (repo / "api" / "deps").mkdir(parents=True)  # untracked, like node_modules
    (repo / "api" / "deps" / "lib.txt").write_text("dep")
    acc = tmp_path / "p" / "acceptance"
    write_pack(acc, [chk("ACC-01", "d.py", needs=["setup:deps"])],
               {"d.py": "import sys\nsys.exit(0 if open('api/deps/lib.txt').read()=='dep' else 1)\n"},
               setup=[{"id": "deps", "cmd": "exit 1", "provides": "api/deps", "network": "allowed"}])
    got = TA.run_pack(acc, "HEAD", repo=repo)
    assert got["results"][0]["outcome"] == "PASS"
    (repo / "api" / "deps" / "lib.txt").unlink()
    (repo / "api" / "deps").rmdir()
    got = TA.run_pack(acc, "HEAD", repo=repo)
    assert got["results"][0]["outcome"] == "UNAVAILABLE" and "setup:deps" in got["results"][0]["reason"]
    assert any("failed" in line for line in got["log"])

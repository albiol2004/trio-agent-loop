"""r18a L1 telemetry + L7 advisory lint.

- evidence kinds of a slice section (the `evidence:` summary line, else the
  per-accept table) and the whole-goal `## Independent probe` line, parsed
  by trioctl, logged, recorded in `.driver.json` (`quality`), printed by
  trio-shadow; never gating;
- the deterministic AST/regex tautology lint (trio-check.py) over the
  slice's test files: `verification_flags` in the builder JSON and the
  worktree record, and a PRE-GATE FLAGS block in the slice-eval's
  OPEN-LOOP CONTEXT. No model calls.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from r16_harness import REPO_ROOT, World, git, init_repo, load

TRIOCTL = load("trioctl_r18a_tel", REPO_ROOT / "omnigent" / "trioctl")
CHECK = load("trio_check_r18a_tel", REPO_ROOT / "metrics" / "trio-check.py")
SHA = "a" * 40


def _section(body: str, verdict: str = "SHIP", sha: str = SHA) -> str:
    return f"\n## slice s1 @{sha} — {verdict}\n\n{body}\n"


def test_slice_evidence_summary_line_wins():
    text = _section(
        "| # | accept | grade | evidence | command | out |\n|---|---|---|---|---|---|\n"
        "| 1 | a | PASS | re-run | x | y |\n"
        "attacks:\n- empty input -> 400\n- remove file -> refusal\n"
        "evidence: re-run=3 probe=1 implementer-test=2 receipt=0 unverified=1\n")
    got = TRIOCTL.slice_evidence("VERDICT: x\n" + text, "s1", SHA)
    assert got["verdict"] == "SHIP" and got["evidence_source"] == "summary"
    assert got["evidence"] == {"re-run": 3, "probe": 1, "implementer-test": 2,
                               "receipt": 0, "unverified": 1}
    assert got["attacks"] == 2


def test_slice_evidence_table_fallback_and_missing():
    table = _section(
        "| # | accept | grade | evidence | command | out |\n|---|---|---|---|---|---|\n"
        "| 1 | a | PASS | re-run | x | y |\n| 2 | b | unverified | receipt | cat r | ok |\n"
        "| 3 | c | PASS | implementer-test | pytest | 1 passed |\n", verdict="ITERATE")
    got = TRIOCTL.slice_evidence(table, "s1", SHA[:12])
    assert got["evidence_source"] == "table"
    assert got["evidence"]["re-run"] == 1 and got["evidence"]["receipt"] == 1
    assert got["evidence"]["unverified"] == 1 and got["verdict"] == "ITERATE"
    bare = TRIOCTL.slice_evidence(_section("accepts: PASS"), "s1", SHA)
    assert bare["evidence"] == {} and bare["evidence_source"] == "missing"
    # r19 C1: a slice section without `attacks:` reports n/a (slice-evals are fast).
    assert bare["attacks"] == "n/a"
    assert TRIOCTL.slice_evidence(_section("x"), "other", SHA) is None


def test_independent_probe_parse():
    assert TRIOCTL.independent_probe("no verdict yet") is None
    assert TRIOCTL.independent_probe("VERDICT: SHIP\n## Criteria results\nok\n") == {"status": "missing"}
    got = TRIOCTL.independent_probe(
        "VERDICT: SHIP\n## Independent probe\nprobe: PASS 404 on unknown key\n"
        "probe_cmd: curl -s localhost/stats?keyHash=x\nprobe_src: loop/probes/iter-3/p.sh\n"
        "## Guidance\n")
    assert got["status"] == "PASS" and got["reason"] == "404 on unknown key"
    assert got["probe_cmd"].startswith("curl") and got["probe_src"].endswith("p.sh")


# ------------------------------------------------------------ L7 lint

W_TESTS = '''from pathlib import Path
RES = Path(__file__).parent / "results"

def _text(p):
    return p.read_text(encoding="utf-8")

def test_ddl():
    sql = _text(Path("sql/v.sql"))
    assert Path("sql/v.sql").is_file()
    assert "fact_salesgp" not in sql

def test_receipt():
    t = _text(RES / "recon.txt")
    assert "\\t3\\t" in t or "3\\t4" in t
    assert "4" in t
'''


def test_python_lint_flags_the_diagnosis_patterns():
    flags = CHECK.python_test_flags("tests/test_w.py", W_TESTS)
    joined = "\n".join(flags)
    assert "tests/test_w.py:9 presence-only check" in joined            # is_file
    assert "tests/test_w.py:10 string presence 'fact_salesgp' on file text" in joined  # W1
    assert "tests/test_w.py:14 or-chain of `in` checks" in joined        # W2
    assert "tests/test_w.py:15 `'4' in ...` checks a 1-character literal" in joined  # W2
    assert "reads receipts under results/" in joined                     # W3


def test_behavioural_test_is_clean_and_module_import_checked():
    good = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    assert CHECK.python_test_flags("tests/test_calc.py", good, {"calc"}) == []
    flags = CHECK.python_test_flags("tests/test_calc.py", "def test_x():\n    assert True\n", {"calc"})
    assert flags == ["tests/test_calc.py:1 imports none of the slice's product modules (calc)"]


def test_ts_lint_and_empty_tsconfig(tmp_path):
    ts = ("import { readFileSync } from 'fs';\n"
          "it('x', () => { const t = readFileSync('src/a.ts', 'utf8');\n"
          "  expect(t).toContain('months'); expect(out).toContain('4'); });\n")
    flags = CHECK.ts_test_flags("src/a.test.ts", ts)
    assert any("toContain over file text" in f for f in flags)
    assert any("toContain of a 1-character literal" in f for f in flags)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "tsconfig.json").write_text('{"files": [], "references": []}')
    got = CHECK.empty_tsconfig_flag(tmp_path, "npx tsc --noEmit -p app && npx vitest run")
    assert got and "typecheck over `files: []`" in got[0]      # W8


# ------------------------------------------- the dispatch path end to end

def test_flags_and_evidence_reach_builder_json_eval_context_and_driver(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tag="r18atel")
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", {"sql/v.sql": "select 1 from fact_salesgp;\n", "README.md": "r\n"})
    spec = world.add_loop(home, "loop/x", [{"id": "s1", "write": "sql/v.sql"}])
    (spec["root_box"] / "briefs" / "s1.md").write_text(
        "# Task s1\n\n## Targeted check\n\n`python3 tests/test_v.py`\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n")
    outputs: list[str] = []

    def worker(role, config, *, prompt, workspace, **kw):
        ws = Path(workspace)
        (ws / "sql" / "v.sql").write_text("select 1 from gold_salesgp;\n")
        (ws / "tests").mkdir(exist_ok=True)
        (ws / "tests" / "test_v.py").write_text(
            "t = open('sql/v.sql').read()\nassert 'fact_salesgp' not in t\nassert '1' in t\n")
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    real_run = t.command_run

    def spy(args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = real_run(args)
        outputs.append(out.getvalue())
        print(out.getvalue(), end="")
        return code

    monkeypatch.setattr(t, "command_run", spy)

    def slice_eval(w, s, runner, ctx, workspace, box, prompt, iteration):
        with w.lock, (box / "VERDICT.md").open("a") as fh:
            fh.write(f"\n## slice {ctx['slice']} @{ctx['sha']} — SHIP\n\n"
                     "| # | accept | grade | evidence | command | out |\n"
                     "attacks:\n- revert the view -> test still green\n- empty -> ok\n"
                     "evidence: re-run=0 probe=0 implementer-test=1 receipt=0 unverified=1\n")
        return True

    world.hooks["slice-eval"] = slice_eval
    code = world.run_loop(spec)
    assert code == 0, (home / "loop/x/LOG.md").read_text()
    view = json.loads([ln for ln in "".join(outputs).splitlines() if '"worker_worktree"' in ln][-1])
    flags = view["verification_flags"]
    assert any("string presence 'fact_salesgp' on file text" in f for f in flags), flags
    assert any("1-character literal" in f for f in flags), flags
    # A grep over the product file IS killed by the revert (W1 is L7's job).
    assert view["kill_check"]["outcome"] == "killed"
    ev = next(e for e in world.events if e["kind"] == "slice-eval")
    assert "PRE-GATE FLAGS (advisory AST lint; the following tests look tautological" in ev["prompt"]
    assert "BASE-REVERT: killed" in ev["prompt"]
    log = (home / "loop/x/LOG.md").read_text()
    assert "kill_check: killed (shadow)" in log
    assert ("SHIP evidence: re-run=0 probe=0 implementer-test=1 receipt=0 unverified=1 "
            "attacks=2 (shadow)") in log, log
    assert "| loop | integration-eval @" in log and "probe: missing (shadow)" in log, log

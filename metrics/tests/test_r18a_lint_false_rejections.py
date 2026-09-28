"""eval-r18a lint table: fewer false rejections.

Accept lint: relation words (returns / is / equals / match(es) / exactly /
identical / unchanged / vs ...) count together with an observable, bare HTTP
status codes and `==`/`<`/`>` count; `->` is optional then (WARN for the
missing oracle, not REJECT). Static-config accepts (compose, nginx,
Dockerfile, systemd, tsconfig, yaml) are their own category, never a
REJECT; a slice with nothing else gets a WARN to pair them with one runtime
accept. `--strict-quality` keeps a finished mailbox's REJECTs advisory.

L7: file-text names are scoped per function; an HTTP response body or a
file written by the action under test is not file text; short-literal
`in`/`toContain` needs <= 1 char, or <= 2 on file text; string presence on a
static config artifact is `static-config` (a tautology only when the file
has no runtime check); an `or`-chain is flagged only for negative checks or
file text; receipts are path-based; "imports none" ignores tests that run
the product by subprocess / importlib / script path; nested repos under a
mailbox are not mailbox tests.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK_PATH = ROOT / "metrics" / "trio-check.py"
_spec = importlib.util.spec_from_file_location("trio_check_r18a_fp", CHECK_PATH)
CHECK = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CHECK)
TM = CHECK.load_trio_metrics()


def levels(text):
    return [lv for lv, _ in CHECK.accept_findings(text)]


@pytest.mark.parametrize("text", [
    # the reviewer's wrong REJECTs (cp, scenario, openrouter, subusage)
    "recon vs raw gold: 0 keys > 1e-6 KUSD, 0 dups, 0 NULL sub_division",
    "FY LP Sales BR equals raw gold (no -3561.80 gap)",
    "status distribution 232/112852/25173/3/4 unchanged",
    "flag off SQL and connection path byte-identical to cd2cc8e",
    "flag on: CP Sales and GP close 7 vs FuCo/bridge 8",
    "(i) CP create ..., run discloses mixed_book, D&A/OI&E match build_bridge(2026,BUD,BR)",
    "(ii) one Sales&GP lever months [9,12] moves only Sep and Dec vs baseline (Oct/Nov delta exactly 0)",
    "(iii) one existing CP scenario re-run with identical stored p50s and labels",
    "timing_label contiguous byte-identical; non-contiguous Sep, Dec",
    "GET /api/openrouter/stats without x-api-key gives 401 {error:unauthorized}",
    "unknown hash is OpenRouterUpstreamError kind not_found; upstream 429 is rate_limited",
    "a hung binary (sleep 10) returns None in under 4 s with exactly one spawn",
    "parse(x) == 3",
    "missing or non-admin identities get 401/403",
])
def test_precise_accepts_are_not_rejected(text):
    assert "REJECT" not in levels(text), CHECK.accept_findings(text)
    assert levels(text) == ["WARN"]  # no oracle tag: still a WARN


@pytest.mark.parametrize("text", [
    "long and scenario DDL never read fact_salesgp",      # W1 stays free text
    "the page is fine",
    "works correctly",
    "handles errors gracefully",
    "SKILL.md titles use the locked scheme",
    "stray-key refusals and other tests in this file stay green",
    "tests pass",
])
def test_free_text_is_still_rejected(text):
    assert levels(text) == ["REJECT"], CHECK.accept_findings(text)


def test_static_config_accept_is_its_own_category():
    assert levels("deploy/pool-board docker-compose.yml runs the board with the repo root mounted") == ["STATIC"]
    assert levels("nginx proxies /vps-pool/ to the board") == ["STATIC"]
    assert levels("tsconfig.json includes src/ | oracle: static") == ["STATIC"]


def _box(tmp_path, accepts, *, state="status: running\n"):
    repo = tmp_path / "repo"
    box = repo / "loop" / "m"
    box.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (box / "STATE.md").write_text("schema: 1\niteration: 1\nmax_iterations: 5\n" + state + "mission: m\n")
    for name in ("GOAL.md", "REPORT.md", "VERDICT.md"):
        (box / name).write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    acc = ", ".join(json.dumps(a) for a in accepts)
    (box / "PLAN.md").write_text(
        "# PLAN\n\n## Verification standard\n\nfull_check: python3 -m pytest -q\n\n"
        "```yaml\nslices:\n  - id: s1\n    writes: [src/a.py]\n    reads: []\n"
        f"    accepts: [{acc}]\n```\n")
    return box


def test_static_only_slice_warns_to_pair_with_a_runtime_accept(tmp_path):
    only = CHECK.quality_findings(_box(tmp_path / "a", ["compose.yaml mounts ../..:/repo"]), TM)
    assert any("static-config accepts only" in m for _lv, m in only), only
    paired = CHECK.quality_findings(_box(tmp_path / "b", [
        "compose.yaml mounts ../..:/repo",
        "curl localhost:6772/api/hosts -> 200 | oracle: value"]), TM)
    assert not any("static-config" in m for _lv, m in paired), paired
    assert not any(lv == "REJECT" for lv, _m in paired)


def test_strict_quality_keeps_a_finished_mailbox_advisory(tmp_path):
    def run(box):
        return subprocess.run([sys.executable, str(CHECK_PATH), str(box), "--no-prompt-sync",
                               "--strict-quality"], capture_output=True, text=True)
    live = run(_box(tmp_path / "live", ["works"]))
    assert live.returncode == 1, live.stdout
    done = run(_box(tmp_path / "done", ["works"], state="status: SHIP\nphase: shipped\n"))
    assert done.returncode == 0, done.stdout
    assert "quality: REJECT slice s1 accept 1 'works'" in done.stdout  # still reported


# ------------------------------------------------------------------ L7

def flags(src, rel="tests/test_x.py", mods=None):
    return CHECK.python_test_flags(rel, src, mods)


def test_file_vars_are_scoped_per_function():
    src = ('from pathlib import Path\n\n'
           'def test_file():\n    text = Path("sql/v.sql").read_text()\n    assert "fact_x" not in text\n\n'
           'def test_http(opener):\n    status, _, raw = opener("/api/hosts")\n    text = raw.decode("utf-8")\n'
           '    assert "config missing" in text\n')
    got = flags(src)
    assert len(got) == 1 and ":5 string presence 'fact_x'" in got[0], got


def test_http_response_read_is_not_file_text():
    src = ('import urllib.request\n\ndef test_x():\n    body = urllib.request.urlopen("http://x").read()\n'
           '    assert b"ok" in body\n    assert "hello world" in body.decode()\n')
    assert flags(src) == []


def test_file_written_by_the_action_under_test_is_runtime_output():
    src = ('def test_x(runner, mailbox):\n    assert runner.run("evaluator", 1, mailbox) == 0\n'
           '    verdict = (mailbox / "VERDICT.md").read_text()\n    assert "attempt: att1" in verdict\n')
    assert flags(src) == []


def test_short_literals():
    src = ('from pathlib import Path\n\ndef test_x(out):\n    t = Path("a.sql").read_text()\n'
           '    assert "4" in out\n    assert "ok" in out\n    assert "fx" in t\n')
    got = flags(src)
    assert any(":5 `'4' in ...` checks a 1-character literal" in f for f in got), got
    assert not any(":6 " in f for f in got), got                        # 2 chars, runtime output
    assert any(":7 `'fx' in ...` checks a 2-character literal" in f for f in got), got


def test_static_config_presence_needs_a_runtime_check_in_the_file():
    body = ('from pathlib import Path\nROOT = Path("deploy")\n\n'
            'def test_layout():\n    compose = (ROOT / "docker-compose.yml").read_text()\n'
            '    unit = (ROOT / "pool-board.service").read_text()\n'
            '    assert "../..:/repo" in compose\n    assert "WorkingDirectory=/repo" in unit\n')
    alone = CHECK.python_test_findings("tests/test_deploy.py", body)
    assert [c for c, _m in alone] == ["tautology", "tautology"], alone
    assert all("static-config" in m and "no runtime check" in m for _c, m in alone)
    paired = body + '\n\ndef test_board():\n    from board import render\n    assert render(2) == "<b>2</b>"\n'
    found = CHECK.python_test_findings("tests/test_deploy.py", paired)
    assert [c for c, _m in found] == ["static-config", "static-config"], found
    assert CHECK.python_test_flags("tests/test_deploy.py", paired) == []


def test_role_config_yaml_via_a_path_variable_is_static_config():
    src = ('from pathlib import Path\n\ndef test_cfg():\n    path = Path("roles") / "docs" / "config.yaml"\n'
           '    text = path.read_text()\n    assert "harness: cursor-native" in text\n\n'
           'def test_run():\n    assert 1 + 1 == 2\n')
    assert flags(src) == []


def test_or_chains():
    runtime_alternates = 'def test_x(message):\n    assert "inventory" in message.lower() or "corrupt" in message.lower()\n'
    assert flags(runtime_alternates) == []
    negative = 'def test_x(a, b):\n    assert "tailscale" not in a or "SECRET" not in b\n'
    assert any("or-chain" in f for f in flags(negative))
    on_file = ('from pathlib import Path\n\ndef test_x():\n    t = Path("results/r.txt").read_text()\n'
               '    assert "\\t3\\t" in t or "3\\t4" in t\n')
    assert any("or-chain" in f for f in flags(on_file))


def test_receipts_are_path_based():
    via_names = ('from pathlib import Path\nROOT = Path(__file__).parent\nRES = ROOT / "results"\n\n'
                 'def _text(p):\n    return p.read_text()\n\n'
                 'def test_r():\n    recon = RES / "recon.txt"\n    t = _text(recon)\n    assert t.count("x") == 3\n')
    assert any("reads receipts" in f for f in flags(via_names))
    json_key = 'import json\n\ndef test_x(out):\n    data = json.loads(out)\n    assert data["results"] == []\n'
    assert not any("receipt" in f for f in flags(json_key))


def test_imports_none_ignores_subprocess_and_hyphenated_scripts():
    sub = ('import subprocess, sys\n\ndef test_cli():\n'
           '    out = subprocess.run([sys.executable, "scripts/workspace-onboard.py", "plan"], capture_output=True)\n'
           '    assert out.returncode == 0\n')
    assert flags(sub, mods=CHECK.product_modules(["scripts/workspace-onboard.py"])) == []
    pkg = 'from subusage import timeline\n\ndef test_t():\n    assert timeline.x() == 1\n'
    assert flags(pkg, mods=CHECK.product_modules(["src/subusage/accounts.py"])) == []
    none = 'def test_x():\n    assert 1 == 1\n'
    assert any("imports none" in f for f in flags(none, mods=CHECK.product_modules(["calc.py"])))


def test_product_modules():
    assert CHECK.product_modules([
        "src/subusage/cursor_cli.py", "scripts/workspace-onboard.py", "tests/test_a.py",
        "pkg/__init__.py", "results/x.py", "README.md",
    ]) == {"subusage", "cursor_cli", "scripts", "workspace_onboard", "pkg"}


def test_ts_to_contain():
    ts = ("import { readFileSync } from 'fs';\n"
          "it('x', () => {\n"
          "  const cfg = readFileSync('tsconfig.json', 'utf8');\n"
          "  const src = readFileSync('src/a.ts', 'utf8');\n"
          "  render(<Avatar name='Ada Iris' />);\n"
          "  expect(screen.getByRole('img').textContent).toContain('AI');\n"
          "  expect(html).not.toContain('YO');\n"
          "  expect(out).toContain('');\n"
          "  expect(src).toContain('ab');\n"
          "  expect(src).toContain('export function');\n"
          "  expect(cfg).toContain('strict');\n"
          "});\n")
    found = CHECK.ts_test_findings("src/a.test.tsx", ts)
    lines = {int(m.split(":")[1].split()[0]): (c, m) for c, m in found}
    assert 6 not in lines and 7 not in lines, found                     # rendered initials
    assert lines[8][0] == "tautology" and "0-character" in lines[8][1]
    assert lines[9][0] == "tautology" and "2-character" in lines[9][1]  # 2 chars on file text
    assert lines[10][0] == "tautology" and "file text" in lines[10][1]
    assert lines[11][0] == "static-config", found                        # paired: render() runs


def test_mailbox_test_flags_skip_nested_repos(tmp_path):
    box = tmp_path / "repo" / "loop" / "m"
    (box / "tests").mkdir(parents=True)
    (box / "tests" / "test_own.py").write_text(
        'from pathlib import Path\n\ndef test_x():\n    t = Path("sql/a.sql").read_text()\n    assert "fact_x" not in t\n')
    nested = box / "app-backend"
    (nested / "tests").mkdir(parents=True)
    (nested / ".git").write_text("gitdir: elsewhere\n")
    for n in range(250):
        (nested / "tests" / f"test_{n:03}.py").write_text('def test_x():\n    assert "4" in "14"\n')
    got = CHECK.mailbox_test_flags(box, TM)
    assert len(got) == 1 and "test_own.py:5" in got[0], got[:3]

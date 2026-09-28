"""r18a L1/L4/L5: advisory trio-check quality lints.

`accepts:` grammar (`<input/action> -> <observable> | oracle: <kind>`),
`goal_probe:`/`goal_acceptance:` on open-loop mailboxes, and a `full_check:`
made only of artifact readers. Advisory in r18a (exit code unchanged);
`--strict-quality` turns a REJECT into a violation.
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


def _load():
    spec = importlib.util.spec_from_file_location("trio_check_r18a", CHECK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = _load()
TM = CHECK.load_trio_metrics()


@pytest.mark.parametrize("text, levels", [
    ("GET /stats?keyHash=<unknown> -> 404 {error:'key not found'} | oracle: refusal", []),
    ("init() then tick() -> SceneAPI.frame == 1 | oracle: value", []),
    ("long and scenario DDL never read fact_salesgp", ["REJECT"]),           # W1 accept
    ("stray-key refusals and other tests in this file stay green", ["REJECT"]),
    ("tests pass", ["REJECT"]),
    ("all tests pass", ["REJECT"]),
    ("works", ["REJECT"]),
    ("SUM(pc_cost_scrate)=948064595.29 rows=138264 dups=0", ["WARN"]),     # C6: precise, no oracle
    ("2026 ACT close month is 7", ["WARN"]),
    ("dialog onSave after Jul-Sep plus months 10 and 12 is {months:[7,8,9,10,12]}", ["WARN"]),
    ("the view is right | oracle: static", ["WARN"]),                        # oracle, no relation
    ("run x -> 3 rows | oracle: vibes", ["WARN"]),                           # unknown kind
])
def test_accept_findings(text, levels):
    assert [lv for lv, _ in CHECK.accept_findings(text)] == levels


def _mailbox(tmp_path: Path, *, accepts: list[str], queue: bool, standard: str) -> Path:
    repo = tmp_path / "repo"
    box = repo / "loop" / "m"
    (box / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 1\nmax_iterations: 5\nstatus: running\nmission: m\n")
    for name in ("GOAL.md", "REPORT.md", "VERDICT.md"):
        (box / name).write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    acc = ", ".join(json.dumps(a) for a in accepts)
    (box / "PLAN.md").write_text(
        "# PLAN\n\n## Verification standard\n\n" + standard + "\n\n"
        "```yaml\nslices:\n  - id: s1\n    writes: [src/a.py]\n    reads: []\n"
        f"    accepts: [{acc}]\n```\n")
    if queue:
        (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n")
    return box


def _messages(box: Path) -> list[tuple[str, str]]:
    return CHECK.quality_findings(box, TM)


def test_open_loop_without_goal_probe_and_reader_only_full_check(tmp_path):
    box = _mailbox(
        tmp_path, accepts=["tests pass"], queue=True,
        standard=("mode: implement-then-smoke\nfull_check:\n"
                  "  python3 loop/m/scripts/live.py --verify-only && python3 "
                  "loop/m/scripts/shot.py --verify-only\n"),
    )
    found = _messages(box)
    msgs = " | ".join(f"{lv} {m}" for lv, m in found)
    assert "REJECT slice s1 accept 1 'tests pass'" in msgs
    assert "WARN no `goal_probe:`" in msgs
    assert "WARN no `goal_acceptance:`" in msgs
    assert "only reads artifacts" in msgs


def test_full_form_mailbox_is_clean(tmp_path):
    box = _mailbox(
        tmp_path,
        accepts=["python3 -m app 2 3 -> prints 5 | oracle: value"],
        queue=True,
        standard=("mode: test-first\nfull_check: python3 -m pytest -q tests\n"
                  "goal_acceptance:\n  - \"python3 -m app 2 3 -> prints 5 | oracle: value (GOAL 1)\"\n"
                  "goal_probe: python3 -m app 40 2 -> prints 42 | offline: yes\n"),
    )
    assert _messages(box) == []


@pytest.mark.parametrize("full_check, flagged", [
    ("python3 -m pytest -q loop/m/tests/test_x.py", True),      # W3: tests under the mailbox
    ("jq .pass results/live.json", True),
    ("cd app && python3 -m pytest -q", False),
    ("npx tsc --noEmit -p api && npx vitest run", False),
    ("python3 scripts/check.py --verify-only && python3 -m pytest -q", False),
])
def test_full_check_reader_only(tmp_path, full_check, flagged):
    box = _mailbox(tmp_path, accepts=[], queue=False, standard=f"full_check: {full_check}\n")
    plan = (box / "PLAN.md").read_text()
    assert bool(CHECK.full_check_reader_only(plan, box)) is flagged


def test_findings_are_advisory_unless_strict(tmp_path):
    box = _mailbox(tmp_path, accepts=["works"], queue=False,
                   standard="full_check: python3 -m pytest -q\n")
    def run(*extra):
        return subprocess.run(
            [sys.executable, str(CHECK_PATH), str(box), "--no-prompt-sync", *extra],
            capture_output=True, text=True)
    plain = run()
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "quality: REJECT slice s1 accept 1 'works'" in plain.stdout
    strict = run("--strict-quality")
    assert strict.returncode == 1
    report = json.loads(run("--json").stdout)
    assert report["loops"][0]["quality"][0]["level"] == "REJECT"
    assert report["summary"]["quality_findings"] == 1

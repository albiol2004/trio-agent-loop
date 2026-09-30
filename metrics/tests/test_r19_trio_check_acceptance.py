"""r19 C4: trio-check acceptance manifest + coverage violations."""
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
    spec = importlib.util.spec_from_file_location("trio_check_r19", CHECK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = _load()
TM = CHECK.load_trio_metrics()
TA = CHECK.load_trio_acceptance()
GOAL = "# GOAL\nThe CLI prints hello.\nThe board refuses unauthenticated admin requests.\n"


def check(cid, kind="behaviour", surface="cli", quote="prints hello"):
    return {"id": cid, "goal_ref": "GOAL.md:2", "goal_quote": quote, "kind": kind,
            "surface": surface, "run": ["python3", f"acceptance/checks/{cid}.py"],
            "expect": {"exit": 0}, "timeout_s": 30, "needs": [], "binds": [],
            "network": "loopback"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def make(tmp_path, plan_extra="", covers_a="[ACC-01]", covers_b="[ACC-02]",
         checks=None, bindings=None, frozen=True, commit=True):
    repo = tmp_path / "repo"
    mb = repo / "loop"
    (mb / "acceptance" / "checks").mkdir(parents=True)
    (mb / "GOAL.md").write_text(GOAL)
    (mb / "STATE.md").write_text("schema: 1\niteration: 1\nmax_iterations: 5\nstatus: running\nmission: m\n")
    for name in ("LOG.md", "REPORT.md"):
        (mb / name).write_text("# x\n")
    (mb / "VERDICT.md").write_text("VERDICT: ITERATE\n")
    (mb / "PLAN.md").write_text(
        "# PLAN\n\n```yaml\nslices:\n"
        f"  - id: cli\n    writes: [app.py]\n    covers: {covers_a}\n"
        f"  - id: docs\n    writes: [README.md]\n    covers: {covers_b}\n"
        "```\n\n## Verification standard\n" + plan_extra)
    checks = checks if checks is not None else [
        check("ACC-01"), check("ACC-02", surface="doc"), check("ACC-03", kind="guard")]
    manifest = {"acceptance_version": 1, "goal_sha256": "g", "notes_sha256": None,
                "base": "b" * 40, "author": {}, "budget_s": 300, "setup": [],
                "bindings": bindings or {}, "checks": checks}
    acc = mb / "acceptance"
    (acc / "MANIFEST.json").write_text(json.dumps(manifest))
    for c in checks:
        (acc / "checks" / f"{c['id']}.py").write_text("raise SystemExit(1)\n")
    (acc / "AMENDMENTS.md").write_text("")
    if frozen:
        pin = TA.manifest_sha256(acc)
        (acc / "FROZEN").write_text(TA.frozen_text(pin, "b" * 40, "m s", [], [(pin, "freeze")]))
    if commit:
        git(tmp_path, "init", "-q", "-b", "main", str(repo))
        git(repo, "config", "user.email", "t@e")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "acceptance: freeze 3 checks (m)")
    return mb


def levels(mb):
    return [(lv, msg) for lv, msg in CHECK.acceptance_findings(mb, TM)]


def test_mapped_pack_is_clean(tmp_path):
    mb = make(tmp_path)
    assert CHECK.coverage_refusals(mb, TM) == []
    assert [f for f in levels(mb) if f[0] == "VIOLATION"] == []
    loop = CHECK.inspect_loop(mb, TM)
    assert loop["ok"], loop["errors"]


def test_unmapped_check_is_a_violation_and_exit_1(tmp_path):
    mb = make(tmp_path, covers_b="[]")
    refusals = CHECK.coverage_refusals(mb, TM)
    assert len(refusals) == 1 and "unmapped acceptance check(s): ACC-02" in refusals[0]
    loop = CHECK.inspect_loop(mb, TM)
    assert not loop["ok"] and any("ACC-02" in e for e in loop["errors"])
    proc = subprocess.run([sys.executable, str(CHECK_PATH), str(mb), "--no-prompt-sync"],
                          capture_output=True, text=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_lead_integration_maps_and_guard_needs_no_coverage(tmp_path):
    mb = make(tmp_path, covers_b="[]", plan_extra="lead_integration: [evidence/x.md, ACC-02]\n")
    assert CHECK.coverage_refusals(mb, TM) == []


def test_unknown_ids_and_unbound_bindings(tmp_path):
    mb = make(tmp_path, covers_a="[ACC-01, ACC-09]",
              plan_extra="lead_integration: [ACC-44]\nacceptance_bindings: {ROUTE: /x, OTHER: /y}\n",
              bindings={"ROUTE": {"default": "/r"}})
    text = " | ".join(CHECK.coverage_refusals(mb, TM))
    assert "unknown acceptance id(s)" in text and "ACC-09" in text and "ACC-44" in text
    assert "not declared in the manifest: OTHER" in text and "ROUTE" not in text.split("manifest:")[1]


def test_schema_and_goal_quote_violations(tmp_path):
    mb = make(tmp_path, checks=[check("ACC-01", quote="not in the goal"), check("ACC-02")],
              covers_b="[ACC-02]")
    text = " | ".join(CHECK.coverage_refusals(mb, TM))
    assert "ACC-01: goal_quote is not a verbatim substring" in text
    (mb / "acceptance" / "MANIFEST.json").write_text("{nope")
    assert "invalid JSON" in " ".join(CHECK.coverage_refusals(mb, TM))


def test_pin_tamper_is_a_violation_and_amend_commit_is_explained(tmp_path):
    mb = make(tmp_path)
    repo = mb.parent
    (mb / "acceptance" / "checks" / "ACC-01.py").write_text("raise SystemExit(0)\n")
    found = levels(mb)
    assert any(lv == "VIOLATION" and "!= pinned" in msg for lv, msg in found)
    git(repo, "commit", "-qam", "slice(cli): sneaky")
    assert any(lv == "VIOLATION" and "!= pinned" in msg for lv, msg in levels(mb))
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    (mb / "acceptance" / "checks" / "ACC-01.py").write_text("raise SystemExit(1)  # fixed\n")
    (mb / "acceptance" / "AMENDMENTS.md").write_text("## ACC-01 · iter 2 · evaluator · t\n")
    git(repo, "commit", "-qam", "acceptance: amend ACC-01 (evaluator, iter 2): wrong surface")
    found = levels(mb)
    assert not [f for f in found if f[0] == "VIOLATION"], found
    assert any(lv == "WARN" and "await the driver's re-pin" in msg for lv, msg in found)


def test_escape_hatch_and_docs_surface_warns(tmp_path):
    mb = make(tmp_path, covers_a="[]", covers_b="[ACC-01]",
              plan_extra="lead_integration: [ACC-02]\n")
    found = levels(mb)
    assert any("covered only by slice(s) docs whose writes are docs" in m for _l, m in found)
    mb2 = make(tmp_path / "b", covers_a="[]", covers_b="[]",
               plan_extra="lead_integration: [ACC-01, ACC-02]\n")
    assert any("mapped only to `lead_integration:`" in m for _l, m in levels(mb2))


def test_no_pack_means_no_findings_and_r18a_lints_unchanged(tmp_path):
    mb = make(tmp_path)
    (mb / "QUEUE.md").write_text("# Queue\n\n```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n")
    quality = [m for _l, m in CHECK.quality_findings(mb, TM)]
    assert not any("goal_acceptance" in m or "goal_probe" in m for m in quality)
    import shutil
    shutil.rmtree(mb / "acceptance")
    assert CHECK.acceptance_findings(mb, TM) == [] and CHECK.coverage_refusals(mb, TM) == []
    quality = [m for _l, m in CHECK.quality_findings(mb, TM)]
    assert any("goal_acceptance" in m for m in quality) and any("goal_probe" in m for m in quality)

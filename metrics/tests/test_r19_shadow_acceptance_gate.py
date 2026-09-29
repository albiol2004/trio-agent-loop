"""r19 C5: trio-shadow --require-commits frozen-acceptance guard."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "metrics" / "trio-shadow.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = _load("trio_acceptance_c5", ROOT / "metrics" / "trio-acceptance.py")
SH = _load("trio_shadow_c5", SHADOW)
PLAN = "# PLAN\n\n```yaml\nslices:\n  - id: cli\n    writes: [app.py]\n    covers: [ACC-01]\n```\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def gate(mb) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits"],
                          capture_output=True, text=True)


def setup(tmp_path, *, slice_first=False) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    mb = repo / "loop"
    mb.mkdir(parents=True)
    (repo / "app.py").write_text("v0\n")
    (mb / "PLAN.md").write_text(PLAN)
    (mb / "GOAL.md").write_text("prints hello\n")
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@e")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    if slice_first:
        (repo / "app.py").write_text("v1\n")
        git(repo, "commit", "-qam", "slice(cli): early")
    src = tmp_path / "authored" / "acceptance"
    (src / "checks").mkdir(parents=True)
    (src / "checks" / "acc_01.py").write_text("raise SystemExit(1)\n")
    manifest = {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
                "checks": [{"id": "ACC-01", "goal_quote": "prints hello", "kind": "behaviour",
                            "run": ["python3", "acceptance/checks/acc_01.py"]}]}
    pin = TA.write_frozen_pack(src, mb / "acceptance", manifest)
    (mb / "acceptance" / "FROZEN").write_text(
        TA.frozen_text(pin, base, "m s", [], [(pin, "freeze")]))
    git(repo, "add", "-f", "--", "loop/acceptance")
    git(repo, "commit", "-qm", "acceptance: freeze 1 checks (m)", "-m",
        f"Acceptance-Pin: {pin}", "--", "loop/acceptance")
    if not slice_first:
        (repo / "app.py").write_text("v1\n")
        git(repo, "commit", "-qam", "slice(cli): impl")
    return repo, mb


def test_clean_freeze_then_slice_passes(tmp_path):
    repo, mb = setup(tmp_path)
    proc = gate(mb)
    assert proc.returncode == 0, proc.stdout
    assert SH.acceptance_offenders(mb) == []


def test_lead_commit_touching_acceptance_is_an_offender(tmp_path):
    repo, mb = setup(tmp_path)
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    git(repo, "commit", "-qam", "slice(cli): make the check pass")
    proc = gate(mb)
    assert proc.returncode == 1
    assert "acceptance gate:" in proc.stdout and "only the driver" in proc.stdout


def test_valid_amend_passes_and_amend_touching_product_fails(tmp_path):
    repo, mb = setup(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(1)  # wider regex\n")
    (acc / "AMENDMENTS.md").write_text("## ACC-01 · iter 1 · evaluator · t\ngoal_quote: prints hello\n")
    git(repo, "commit", "-qam", "acceptance: amend ACC-01 (evaluator, iter 1): over-specified")
    assert gate(mb).returncode == 0
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(1)  # again\n")
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write("## ACC-01 · iter 2 · evaluator · t\n")
    (repo / "app.py").write_text("v2\n")
    git(repo, "commit", "-qam", "acceptance: amend ACC-01 (evaluator, iter 2): again")
    proc = gate(mb)
    assert proc.returncode == 1 and "may touch only acceptance/" in proc.stdout


def test_amend_without_record_or_rewriting_history_fails(tmp_path):
    repo, mb = setup(tmp_path)
    acc = mb / "acceptance"
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(1)  # x\n")
    git(repo, "commit", "-qam", "acceptance: amend ACC-01 (evaluator, iter 1): no record")
    assert "no AMENDMENTS.md record for ACC-01" in gate(mb).stdout


def test_slice_commit_before_the_freeze_is_tolerated_with_a_note(tmp_path):
    # eval-r19 finding 5: a Lead take-over committed while the author was
    # still working is behind the freeze; the author never saw it.
    repo, mb = setup(tmp_path, slice_first=True)
    proc = gate(mb)
    assert proc.returncode == 0, proc.stdout
    base = git(repo, "rev-list", "--max-parents=0", "HEAD")
    proc = subprocess.run([sys.executable, str(SHADOW), "--mailbox", str(mb), "--require-commits",
                           "--acceptance-base", base], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout
    assert "acceptance note:" in proc.stdout and "before the acceptance freeze" in proc.stdout


def test_slice_on_a_line_without_the_freeze_fails_naming_the_ordering(tmp_path):
    repo, mb = setup(tmp_path)
    base = git(repo, "rev-list", "--max-parents=0", "HEAD")
    git(repo, "checkout", "-q", "-b", "side", base)
    (repo / "side.py").write_text("x\n")
    git(repo, "add", "side.py")
    git(repo, "commit", "-qm", "slice(cli): on a side line")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-edit", "side")
    proc = gate(mb)
    assert proc.returncode == 1
    assert "acceptance/freeze ordering" in proc.stdout


def test_forged_freeze_and_second_freeze_fail(tmp_path):
    repo, mb = setup(tmp_path)
    (mb / "acceptance" / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    git(repo, "commit", "-qam", "acceptance: freeze 1 checks (lead)", "-m", "Acceptance-Pin: " + "0" * 64)
    out = gate(mb).stdout
    assert "second freeze commit" in out


def test_freeze_whose_pin_does_not_match_fails(tmp_path):
    repo, mb = setup(tmp_path)
    freeze = git(repo, "log", "--format=%H", "--grep=^acceptance: freeze")
    # Rewrite history: the same freeze with a forged trailer.
    git(repo, "reset", "-q", "--hard", freeze + "~1")
    git(repo, "checkout", "-q", freeze, "--", "loop/acceptance")
    git(repo, "commit", "-qm", "acceptance: freeze 1 checks (m)", "-m", "Acceptance-Pin: " + "0" * 64)
    assert "does not match the committed pack" in gate(mb).stdout


def test_driver_restore_commit_with_matching_pin_passes(tmp_path):
    repo, mb = setup(tmp_path)
    acc = mb / "acceptance"
    good = (acc / "checks" / "acc_01.py").read_text()
    (acc / "checks" / "acc_01.py").write_text("raise SystemExit(0)\n")
    git(repo, "commit", "-qam", "slice(cli): tamper")
    (acc / "checks" / "acc_01.py").write_text(good)
    pin = TA.manifest_sha256(acc)
    git(repo, "commit", "-qm", "acceptance: restore (tamper after abc)", "-m",
        f"Acceptance-Pin: {pin}", "--", "loop/acceptance")
    # The driver counted the breach when it restored: the gate passes again.
    assert SH.acceptance_offenders(mb) == []
    assert gate(mb).returncode == 0


def test_no_pack_gate_unchanged(tmp_path):
    repo = tmp_path / "repo"
    mb = repo / "loop"
    mb.mkdir(parents=True)
    (repo / "app.py").write_text("v0\n")
    (mb / "PLAN.md").write_text(PLAN.replace("    covers: [ACC-01]\n", ""))
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@e")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "slice(cli): impl")
    proc = gate(mb)
    assert proc.returncode == 0 and "acceptance gate" not in proc.stdout
    assert SH.acceptance_offenders(mb) == []


def test_pack_hash_at_matches_manifest_sha256(tmp_path):
    repo, mb = setup(tmp_path)
    prefix = "loop/acceptance"
    assert SH.pack_hash_at(repo, "HEAD", prefix) == TA.manifest_sha256(mb / "acceptance")

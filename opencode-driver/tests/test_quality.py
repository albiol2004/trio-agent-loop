"""r18a shadow quality telemetry (trio_opencode.quality), ported from
omnigent/trioctl for the standalone driver's open-loop mode.

Mirrors the string-level expectations of
``omnigent/tests/test_r18a_telemetry_lint.py`` and
``omnigent/tests/test_r18a_lints_in_loop.py`` for the pure functions this
module carries over, plus coverage of the base-revert kill check (tree
restored byte-identical) and the env/``.driver.json`` kill-check switches.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trio_opencode import quality

# conftest's isolated-env fixture points $HOME at a fresh tmp dir, which
# would hide `pytest` from a bare `sys.executable -m pytest` subprocess
# (its user-site-packages lookup follows $HOME) -- so every targeted
# command built below is prefixed with the real PYTHONPATH pytest already
# resolved to in *this* process, taken once at import time.
_PYTEST_SITE = str(Path(pytest.__file__).resolve().parents[1])


def _pytest_command(*args: str) -> str:
    return f"PYTHONPATH={_PYTEST_SITE} {sys.executable} -m pytest " + " ".join(args)


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-test", GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="trio-opencode-test", GIT_COMMITTER_EMAIL="t@example.test",
    )
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=_git_env(),
    ).stdout.strip()


# ============================================================ TARGETED_CHECK

@pytest.mark.parametrize(
    "output, expected",
    [
        ("TARGETED_CHECK: 4 passed in 0.12s", "TARGETED_CHECK: 4 passed in 0.12s"),
        ("TARGETED_CHECK: FAILED 2 failed, 3 passed",
         "TARGETED_CHECK: FAILED 2 failed, 3 passed"),
        # lowercase, no `FAILED` prefix, but failure text -> prefixed.
        ("targeted_check: 2 failed, 3 passed", "TARGETED_CHECK: FAILED 2 failed, 3 passed"),
        ("TARGETED_CHECK: no tests ran", "TARGETED_CHECK: FAILED no tests ran"),
        ("TARGETED_CHECK: 0 failed, 4 passed", "TARGETED_CHECK: 0 failed, 4 passed"),
        # decorated: bullet, bold, backticks.
        ("- **TARGETED_CHECK:** `3 passed`", "TARGETED_CHECK: 3 passed"),
        # template placeholder echoed back verbatim -> ignored (None).
        ("TARGETED_CHECK: PASS <n>", None),
        ("TARGETED_CHECK: <the line stating the pass/fail counts>", None),
        ("no targeted check line here at all", None),
        ("", None),
    ],
)
def test_targeted_check_line_normalization(output, expected):
    assert quality.targeted_check_line(output) == expected


def test_targeted_check_line_last_line_wins():
    out = "TARGETED_CHECK: 1 passed\nsome noise\nTARGETED_CHECK: 2 passed in 0.2s\n"
    assert quality.targeted_check_line(out) == "TARGETED_CHECK: 2 passed in 0.2s"


@pytest.mark.parametrize(
    "line, expected",
    [
        (None, True),
        ("TARGETED_CHECK: FAILED 2 failed", True),
        ("TARGETED_CHECK: 4 passed in 0.1s", False),
    ],
)
def test_targeted_check_failed(line, expected):
    assert quality.targeted_check_failed(line) is expected


# ============================================================ brief command

def test_brief_targeted_command_fenced_block():
    brief = (
        "# Task s1\n\n## Targeted check\n\n"
        "```\npython3 -m pytest -q tests/test_x.py\n```\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>` "
        "after running the check.\n"
    )
    assert quality.brief_targeted_command(brief) == "python3 -m pytest -q tests/test_x.py"


def test_brief_targeted_command_backticked_span():
    brief = (
        "# Task s1\n\n## Targeted check\n\n"
        "Run `python3 -m pytest -q tests/test_x.py` and report the result.\n"
    )
    assert quality.brief_targeted_command(brief) == "python3 -m pytest -q tests/test_x.py"


def test_brief_targeted_command_none_when_only_placeholder():
    brief = (
        "# Task s1\n\n## Targeted check\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>` "
        "(pytest: `N passed[, M failed] in ...`).\n"
    )
    assert quality.brief_targeted_command(brief) is None


# ============================================================ quality_note

def test_quality_note_builder_slice():
    note = quality.quality_note(
        isolate=True,
        kill_check={"outcome": "killed", "reason": None},
        authored_by="builder",
        flags=["tests/test_v.py:3 string presence 'fact_salesgp' on file text"],
        accept_lint=["accept 1 's1 works': REJECT only says tests pass"],
        builder_ran=True,
    )
    lines = note.splitlines()
    assert lines[0] == "SLICE QUALITY (r18a shadow; informs your grading, gates nothing):"
    assert "BASE-REVERT: killed" in lines
    assert "AUTHORED-BY: builder" in lines
    assert ("PRE-GATE: advisory verification lints over the builder's worktree; "
            "grade every listed item explicitly") in lines
    assert "- tests/test_v.py:3 string presence 'fact_salesgp' on file text" in lines
    assert "- accept 1 's1 works': REJECT only says tests pass" in lines
    assert note.endswith("\n") and not note.startswith("\n")


def test_quality_note_lead_takeover():
    note = quality.quality_note(
        isolate=True,
        kill_check={"outcome": "n/a",
                    "reason": "no builder run merged this sha (Lead take-over or fix)"},
        authored_by="lead",
        flags=["tests/test_v.py:3 string presence 'fact_salesgp' on file text"],
        accept_lint=["accept 1 's1 works': REJECT only says tests pass"],
        builder_ran=False,
    )
    assert "AUTHORED-BY: lead" in note
    assert ("PRE-GATE: advisory verification lints over the slice's commits at this sha "
            "(no builder ran); grade every listed item explicitly") in note
    assert ("BASE-REVERT: n/a -- no builder run merged this sha "
            "(Lead take-over or fix)") in note


def test_quality_note_empty_when_nothing_to_say():
    assert quality.quality_note(
        isolate=False, kill_check=None, authored_by=None, flags=[], accept_lint=[],
        builder_ran=False,
    ) == ""
    # isolate off: a kill_check fact is suppressed even if present.
    assert quality.quality_note(
        isolate=False, kill_check={"outcome": "killed"}, authored_by="builder",
        flags=[], accept_lint=[], builder_ran=True,
    ) == ""


# ============================================================ L1 evidence

SHA = "a" * 40


def test_slice_evidence_summary_line():
    text = (
        f"\n## slice s1 @{SHA} — SHIP\n\n"
        "| # | accept | grade | evidence | command | out |\n"
        "| 1 | a | PASS | re-run | x | y |\n"
        "attacks:\n- empty input -> 400\n- remove file -> refusal\n"
        "evidence: re-run=3 probe=1 implementer-test=2 receipt=0 unverified=1\n"
    )
    got = quality.slice_evidence("VERDICT: x\n" + text, "s1", SHA)
    assert got["verdict"] == "SHIP"
    assert got["evidence_source"] == "summary"
    assert got["evidence"] == {
        "re-run": 3, "probe": 1, "implementer-test": 2, "receipt": 0, "unverified": 1,
    }
    assert got["attacks"] == 2


def test_slice_evidence_missing_section_is_none():
    assert quality.slice_evidence("nothing here", "s1", SHA) is None


def test_evidence_log_line_and_retired_log_line_format():
    ev = {"verdict": "SHIP", "evidence": {"re-run": 0, "probe": 0, "implementer-test": 1,
                                          "receipt": 0, "unverified": 1}, "attacks": 2}
    line = quality.evidence_log_line(3, "s1", SHA, ev)
    assert line == (
        "- iter 3 | loop | slice s1 @" + SHA[:12] + " SHIP evidence: "
        "re-run=0 probe=0 implementer-test=1 receipt=0 unverified=1 attacks=2 (shadow)"
    )
    retired = quality.retired_log_line(3, "s1", SHA, "builder", {"outcome": "killed"})
    assert retired == (
        "- iter 3 | loop | retired slice s1 @" + SHA[:12]
        + " by builder | kill_check: killed (shadow)"
    )


def test_kill_check_suffix_tags():
    assert quality.kill_check_suffix(None) == ""
    assert quality.kill_check_suffix({"outcome": "killed"}) == "kill_check: killed"
    assert quality.kill_check_suffix(
        {"outcome": "error", "restored": False}
    ) == "kill_check: error (restore)"
    assert quality.kill_check_suffix(
        {"outcome": "n/a", "reason": "absolute cd: the check leaves the worktree (/tmp)"}
    ) == "kill_check: n/a (absolute cd)"


# ============================================================ kill_check_enabled

def test_kill_check_enabled_default_true(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_KILL_CHECK", raising=False)
    assert quality.kill_check_enabled(None) is True


def test_kill_check_enabled_cli_disabled(monkeypatch):
    monkeypatch.delenv("TRIO_KILL_CHECK", raising=False)
    assert quality.kill_check_enabled(None, cli_disabled=True) is False


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", "Off"])
def test_kill_check_enabled_env_off(monkeypatch, value):
    monkeypatch.setenv("TRIO_KILL_CHECK", value)
    assert quality.kill_check_enabled(None) is False


def test_kill_check_enabled_driver_json_false(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_KILL_CHECK", raising=False)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / ".driver.json").write_text(json.dumps({"kill_check": False}))
    assert quality.kill_check_enabled(mailbox) is False


def test_kill_check_enabled_driver_json_true_or_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_KILL_CHECK", raising=False)
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    assert quality.kill_check_enabled(mailbox) is True
    (mailbox / ".driver.json").write_text(json.dumps({"kill_check": True}))
    assert quality.kill_check_enabled(mailbox) is True


def test_kill_check_budget_env_overrides_plan(tmp_path, monkeypatch):
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "PLAN.md").write_text("full_check_budget_s: 45\n")
    monkeypatch.delenv("TRIO_KILL_CHECK_BUDGET_S", raising=False)
    assert quality.kill_check_budget(mailbox) == 45.0
    monkeypatch.setenv("TRIO_KILL_CHECK_BUDGET_S", "90")
    assert quality.kill_check_budget(mailbox) == 90.0
    monkeypatch.delenv("TRIO_KILL_CHECK_BUDGET_S", raising=False)
    assert quality.kill_check_budget(None) == quality.KILL_CHECK_DEFAULT_BUDGET_S


# ============================================================ run_kill_check

def _kill_check_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "product.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
    (root / "README").write_text("r\n", encoding="utf-8")
    git(root, "add", "product.py", "README")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    return root, base


def test_run_kill_check_killed_and_tree_restored_byte_identical(tmp_path):
    root, base = _kill_check_repo(tmp_path)
    # The builder's change: product.py now returns 2, plus a NEW test that
    # only passes with the new value -- a real behavioural kill once
    # reverted to base.
    (root / "product.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    (root / "test_product.py").write_text(
        "from product import compute\n\n\ndef test_compute():\n    assert compute() == 2\n",
        encoding="utf-8",
    )
    before = {
        "product.py": (root / "product.py").read_bytes(),
        "test_product.py": (root / "test_product.py").read_bytes(),
    }
    command = _pytest_command("-q", "test_product.py")
    result = quality.run_kill_check(root, base, command)
    assert result["outcome"] == "killed", result
    assert result["restored"] is True
    assert result["tree_sha256"] == result["tree_sha256_after"]
    # Byte-identical: the builder's worktree is exactly as it was before
    # the kill check ran, product and test files alike.
    assert (root / "product.py").read_bytes() == before["product.py"]
    assert (root / "test_product.py").read_bytes() == before["test_product.py"]


def test_run_kill_check_survived_when_test_still_passes(tmp_path):
    root, base = _kill_check_repo(tmp_path)
    (root / "product.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    (root / "test_product.py").write_text(
        "from product import compute\n\n\ndef test_compute():\n    assert compute() in (1, 2)\n",
        encoding="utf-8",
    )
    before_digest_inputs = (root / "product.py").read_bytes(), (root / "test_product.py").read_bytes()
    command = _pytest_command("-q", "test_product.py")
    result = quality.run_kill_check(root, base, command)
    assert result["outcome"] == "survived", result
    assert result["restored"] is True
    assert result["tree_sha256"] == result["tree_sha256_after"]
    assert (
        (root / "product.py").read_bytes(), (root / "test_product.py").read_bytes()
    ) == before_digest_inputs


def test_run_kill_check_na_when_no_test_file_changed(tmp_path):
    root, base = _kill_check_repo(tmp_path)
    (root / "product.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    command = _pytest_command("-q", "test_product.py")
    result = quality.run_kill_check(root, base, command)
    assert result["outcome"] == "n/a"
    assert result["reason"] == "the slice changed no test file"
    # No test file was changed at all: the product edit is left exactly as
    # the builder made it (no revert ever ran).
    assert (root / "product.py").read_text() == "def compute():\n    return 2\n"


def test_run_kill_check_no_command_is_na():
    result = quality.run_kill_check(Path("/nonexistent"), "HEAD", None)
    assert result == {
        "mode": "shadow", "outcome": "n/a",
        "reason": "no `## Targeted check` command in the brief",
    }


def test_kill_check_for_builder_skips_on_failed_targeted_check():
    assert quality.kill_check_for_builder(
        Path("/nonexistent"), "HEAD", "## Targeted check\n`pytest`\n",
        "TARGETED_CHECK: FAILED 1 failed",
    ) is None
    assert quality.kill_check_for_builder(
        Path("/nonexistent"), "HEAD", "## Targeted check\n`pytest`\n", None,
    ) is None


def test_kill_check_for_builder_skips_when_brief_has_no_command():
    assert quality.kill_check_for_builder(
        Path("/nonexistent"), "HEAD",
        "## Targeted check\n\nPrint `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n",
        "TARGETED_CHECK: 1 passed",
    ) is None


def test_kill_check_for_builder_runs_when_passing_and_command_present(tmp_path):
    root, base = _kill_check_repo(tmp_path)
    (root / "product.py").write_text("def compute():\n    return 2\n", encoding="utf-8")
    (root / "test_product.py").write_text(
        "from product import compute\n\n\ndef test_compute():\n    assert compute() == 2\n",
        encoding="utf-8",
    )
    brief = (
        f"## Targeted check\n\n`{_pytest_command('-q', 'test_product.py')}`\n\n"
        "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n"
    )
    result = quality.kill_check_for_builder(root, base, brief, "TARGETED_CHECK: 1 passed")
    assert result is not None
    assert result["outcome"] == "killed"


# ============================================================ slice_lint

PLAN_WITH_BAD_ACCEPT = """\
# Plan
```yaml
slices:
  - id: s1
    writes: [sql/v.sql]
    reads: []
    accepts:
      - s1 works
```
"""

W1_TEST = (
    "from pathlib import Path\n\n"
    "def test_gold_only():\n"
    "    sql = Path('sql/v.sql').read_text()\n"
    "    assert 'fact_salesgp' not in sql\n"
    "    assert '4' in sql\n"
)


def test_slice_lint_reports_bad_accept_and_takeover_flags(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "sql").mkdir()
    (root / "sql" / "v.sql").write_text("select 1 from fact_salesgp;\n", encoding="utf-8")
    (root / "README").write_text("r\n", encoding="utf-8")
    git(root, "add", "sql", "README")
    git(root, "commit", "-q", "-m", "base")

    (root / "sql" / "v.sql").write_text("select 1 from gold_salesgp;\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_v.py").write_text(W1_TEST, encoding="utf-8")
    git(root, "add", "sql", "tests")
    git(root, "commit", "-q", "-m", "slice(s1): take over the gold-only view")
    sha = git(root, "rev-parse", "HEAD")

    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "PLAN.md").write_text(PLAN_WITH_BAD_ACCEPT, encoding="utf-8")

    flags, accepts = quality.slice_lint(mailbox, root, "s1", sha, None)
    assert any("string presence 'fact_salesgp' on file text" in f for f in flags), flags
    assert any("1-character literal" in f for f in flags), flags
    assert accepts and "REJECT" in accepts[0]
    assert "accept 1 's1 works'" in accepts[0]


def test_slice_lint_reuses_builder_flags_without_takeover_scan(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("r\n", encoding="utf-8")
    git(root, "add", "README")
    git(root, "commit", "-q", "-m", "base")
    sha = git(root, "rev-parse", "HEAD")

    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "PLAN.md").write_text(PLAN_WITH_BAD_ACCEPT, encoding="utf-8")

    flags, accepts = quality.slice_lint(mailbox, root, "s1", sha, ["existing-flag"])
    assert flags == ["existing-flag"]
    assert accepts and "REJECT" in accepts[0]


# ============================================================ builder_test_flags

def test_builder_test_flags_over_a_worktree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "sql").mkdir()
    (root / "sql" / "v.sql").write_text("select 1 from fact_salesgp;\n", encoding="utf-8")
    git(root, "add", "sql")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")

    (root / "sql" / "v.sql").write_text("select 1 from gold_salesgp;\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_v.py").write_text(W1_TEST, encoding="utf-8")

    flags = quality.builder_test_flags(root, base, None)
    assert any("string presence 'fact_salesgp' on file text" in f for f in flags), flags


# ============================================================ lead_pass_lint

def test_lead_pass_lint_counts_and_mode(tmp_path):
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    (mailbox / "GOAL.md").write_text("# Goal\nShip it\n", encoding="utf-8")
    (mailbox / "PLAN.md").write_text(PLAN_WITH_BAD_ACCEPT, encoding="utf-8")
    result = quality.lead_pass_lint(mailbox, 3)
    assert result is not None
    assert result["mode"] == "advisory"
    assert result["iteration"] == 3
    assert result["counts"].get("REJECT", 0) >= 1
    assert any("accept 1 's1 works'" in f for f in result["findings"])


# ============================================================ no omnigent import

def test_quality_module_never_imports_omnigent():
    import sys as _sys
    assert not any(name == "omnigent" or name.startswith("omnigent.") for name in _sys.modules)

"""Verdict-grammar conformance tests for metrics/trio-check.py.

Runs the checker against the static fixture mailboxes in
metrics/tests/fixtures/ and asserts the exit-code contract:

  valid-*    (SHIP, ITERATE, ITERATE scope=design, ITERATE scope=local:<paths>,
              NEEDS_HUMAN)      -> exit 0 (v1 PASS)
  invalid-*  (VERDICT: MAYBE, bad scope= suffix, scope= on SHIP)
                                -> exit 1 (v1 violation)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CHECKER = Path(__file__).parents[1] / "trio-check.py"

VALID = [
    "valid-ship",
    "valid-iterate",
    "valid-iterate-design",
    "valid-iterate-local",
    "valid-needs-human",
]
INVALID = [
    "invalid-maybe",
    "invalid-scope",
    "invalid-scope-on-ship",
]


def _check_exit(fixture: str) -> int:
    """Run trio-check.py as a subprocess and return its exit code."""
    import subprocess

    return subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / fixture)],
        capture_output=True,
        text=True,
    ).returncode


@pytest.mark.parametrize("fixture", VALID)
def test_valid_verdict_grammar_passes(fixture: str) -> None:
    assert _check_exit(fixture) == 0


@pytest.mark.parametrize("fixture", INVALID)
def test_invalid_verdict_grammar_fails(fixture: str) -> None:
    assert _check_exit(fixture) == 1


def test_old_grammar_rejects_maybe_with_message() -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / "invalid-maybe")],
        capture_output=True,
        text=True,
    )
    assert "VERDICT: SHIP|ITERATE|BLOCKED|NEEDS_HUMAN" in result.stdout


def test_scope_suffix_errors_are_reported() -> None:
    import subprocess

    bad_scope = subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / "invalid-scope")],
        capture_output=True,
        text=True,
    )
    assert "invalid scope= suffix" in bad_scope.stdout
    scope_on_ship = subprocess.run(
        [sys.executable, str(CHECKER), str(FIXTURES / "invalid-scope-on-ship")],
        capture_output=True,
        text=True,
    )
    assert "only valid on `VERDICT: ITERATE`" in scope_on_ship.stdout

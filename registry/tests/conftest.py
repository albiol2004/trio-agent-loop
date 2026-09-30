"""Shared test defaults for the registry / dashboard tests.

Every test here runs with the dashboard's per-user live paths pointed into
its own temporary directory, and the session asserts at the end that no test
wrote the LIVE trio-dash inbox read-state (r20 review finding F1: a
DashboardServer test without a patched HOME wrote
``~/.local/share/trio-agent-loop/inbox-state.json``, the running service's
file, on every full-suite run and every verify.sh item-65 run)."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import tempfile
import warnings
from pathlib import Path

import pytest

# The live file the running trio-dash owns. Resolved from the real HOME at
# import time (before any test patches HOME) and never written by the guard.
LIVE_INBOX_STATE = Path(os.path.expanduser("~")) / ".local" / "share" / "trio-agent-loop" / "inbox-state.json"

# Dashboard / ledger env vars whose unset default is a path under the real HOME
# that the dashboard WRITES (inbox read-state, per-loop action logs, diagnoses,
# HUMAN.md ledger, native run registry).
LIVE_PATH_ENV = ("TRIO_DASH_INBOX_STATE", "TRIO_DASH_STATE_DIR", "TRIO_NATIVE_RUNS_DIR")


@pytest.fixture(autouse=True)
def _isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Keep worktree roots, root-free Lead records and acceptance state off
    the real ``${XDG_STATE_HOME:-~/.local/state}/trio-agent-loop`` (r20: two
    driver tests leaked ACTIVE Lead records there), and every dashboard live
    path (``LIVE_PATH_ENV``) off the real HOME. A test that needs its own
    location (or the HOME-relative default under a patched ``serve.HOME``)
    still sets or blanks these itself."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)
    dash = tmp_path_factory.mktemp("dash-live-paths")
    monkeypatch.setenv("TRIO_DASH_INBOX_STATE", str(dash / "inbox-state.json"))
    monkeypatch.setenv("TRIO_DASH_STATE_DIR", str(dash / "trio-dash-state"))
    monkeypatch.setenv("TRIO_NATIVE_RUNS_DIR", str(dash / "native-runs"))


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def inbox_keys_owned_by_tests(before: dict, after: dict, roots: list[str]) -> list[str]:
    """Keys (workspace roots) added or changed between two inbox-state
    documents that lie under one of the test temp ``roots``."""
    changed = [k for k in after if before.get(k) != after[k]]
    return sorted(k for k in changed
                  if any(k == r or k.startswith(r.rstrip("/") + "/") for r in roots))


def _test_temp_roots(tmp_path_factory) -> list[str]:
    """This session's temp roots only: TMPDIR/tempfile's dir, the pytest
    basetemp and pytest's default `/tmp/pytest-of-<user>/` -- never bare
    `/tmp` (r20 review round 2: a live-service write for any /tmp workspace,
    e.g. a claude scratchpad, must warn, not fail the session)."""
    roots = {tempfile.gettempdir(), f"/tmp/pytest-of-{getpass.getuser()}"}
    try:
        roots.add(str(tmp_path_factory.getbasetemp()))
    except Exception:  # pragma: no cover - basetemp always resolvable in a session
        pass
    roots.discard("/tmp")
    out = set()
    for r in roots:
        out.add(r)
        out.add(os.path.realpath(r))
    return sorted(out)


@pytest.fixture(scope="session", autouse=True)
def _live_inbox_state_guard(tmp_path_factory):
    """Session guard: no registry test may write the LIVE inbox-state.json.

    Skipped (a no-op) when the live file does not exist. The live trio-dash
    service may legitimately rewrite the file during a long run, so a changed
    sha fails only when a changed/added key is a test temp root (TMPDIR,
    the pytest basetemp or /tmp/pytest-of-<user>) — the exact signature of the F1 leak; any
    other change is reported as a warning."""
    before_sha = _sha(LIVE_INBOX_STATE)
    if before_sha is None:
        yield
        return
    before = _load(LIVE_INBOX_STATE)
    yield
    if _sha(LIVE_INBOX_STATE) == before_sha:
        return
    leaked = inbox_keys_owned_by_tests(before, _load(LIVE_INBOX_STATE),
                                       _test_temp_roots(tmp_path_factory))
    if leaked:
        pytest.fail(f"a registry test wrote the LIVE {LIVE_INBOX_STATE}: "
                    f"{len(leaked)} test temp root(s) {leaked[:5]}", pytrace=False)
    warnings.warn(f"{LIVE_INBOX_STATE} changed during the session without test temp "
                  "roots (the live trio-dash service wrote it)")

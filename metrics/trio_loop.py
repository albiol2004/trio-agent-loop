#!/usr/bin/env python3
"""Stdlib Trio loop state machine: gates, verdict, repair, resume.

This is the only implementation of those semantics. portable/driver.sh
(and later trioctl) must call run_loop(); they must not re-code the
state machine. Verdict parsing is delegated to trio-metrics.py so
this driver accepts exactly what trio-check.py accepts.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

def _load_metrics_module():
    path = Path(__file__).resolve().with_name("trio-metrics.py")
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

# Verdict and slice parsing stay in the existing shared metrics module.
_METRICS = _load_metrics_module()
VERDICT_RE = _METRICS.VERDICT_RE
parse_verdict_scope = _METRICS.parse_verdict_scope
KNOWN_VERDICTS = tuple(_METRICS.KNOWN_VERDICTS)
A_LEAD_RE = _METRICS.A_LEAD_RE
SCOPE_RE = re.compile(r"^scope=(design|local:[^\s]+)$", re.IGNORECASE)
# iteration/status are MAILBOX-SCHEMA.md fields. phase is the resume
# cursor this driver owns (trio-metrics parse_state ignores unknown keys).
STATE_RE = re.compile(
    r"^\s*(?:-\s+)?(iteration|status|phase)\s*:\s*(.*)$", re.IGNORECASE
)
ROLE_LOG_RE = re.compile(
    r"^\s*-\s*(?:\w+\s+)?(?:iter|iteration)\s+(\d+)\s*\|\s*"
    r"(lead|repair)\s*\|",
    re.IGNORECASE,
)
OUTCOMES = {
    "SHIP": ("shipped", "shipped", 0),
    "BLOCKED": ("blocked", "blocked", 2),
    "NEEDS_HUMAN": ("needs_human", "needs_human", 5),
}
TERMINAL_CODES = {"shipped": 0, "blocked": 2, "needs_human": 5, "error": 3}

class RoleRunner(Protocol):
    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        ...

def _read_state(path: Path) -> dict[str, str]:
    """Create missing state, then read the keys owned by this driver."""
    if not path.is_file():
        path.write_text(
            "iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8"
        )
    state = {"iteration": "0", "status": "ready", "phase": "idle"}
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    for line in lines:
        match = STATE_RE.match(line)
        if match:
            state[match.group(1).lower()] = match.group(2).strip()
    return state

def _update_state(path: Path, updates: dict[str, str]) -> None:
    """Replace owned keys while preserving all unrelated state lines."""
    lines = path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    found: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = STATE_RE.match(line)
        key = match.group(1).lower() if match else None
        if key in updates:
            line = f"{key}: {updates[key]}"
            found.add(key)
        result.append(line)
    for key in ("iteration", "status", "phase"):
        if key in updates and key not in found:
            result.append(f"{key}: {updates[key]}")
    path.write_text(
        "\n".join(result) + ("\n" if result else ""), encoding="utf-8"
    )

def _append_log(mailbox: Path, line: str) -> None:
    path = mailbox / "LOG.md"
    if not path.is_file():
        path.write_text("# Trio loop log\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8", errors="replace")
    if text and not text.endswith("\n"):
        path.write_text(text + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as log:
        log.write(f"{line}\n")

def _counter(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if value >= 0 else 0

def _number(value: str) -> int:
    match = re.match(r"\d+", value.strip())
    return int(match.group(0)) if match else 0

def _acquire_lock(mailbox: Path) -> Path | None:
    """mkdir is atomic; replace a lock only after its pid is stale."""
    lock = mailbox / ".lock"
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            pid = int((lock / "pid").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
            except OSError:
                pass
            else:
                print(
                    f"Mailbox {mailbox}/ is owned by a live driver "
                    f"(pid {pid}).",
                    file=sys.stderr,
                )
                return None
        print(
            f"Removing stale lock on {mailbox}/ "
            f"(pid {pid or 'unknown'} is gone).",
            file=sys.stderr,
        )
        shutil.rmtree(lock, ignore_errors=True)
        lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    return lock

def _commit_gate(mailbox: Path, repo: Path | None) -> tuple[bool, str]:
    """Run trio-shadow; exit 0 passes and exits 1/2 fail."""
    script = Path(__file__).resolve().with_name("trio-shadow.py")
    target = mailbox if (mailbox / "PLAN.md").is_file() else repo or mailbox
    command = [
        sys.executable,
        str(script),
        "--mailbox",
        str(target.resolve()),
        "--require-commits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        return False, f"commit gate could not run: {exc}"
    if result.returncode == 0:
        return True, "commit gate passed"
    return False, f"commit gate failed with exit {result.returncode}"

def _log_gate(mailbox: Path, iteration: int, role: str) -> tuple[bool, str]:
    """Require this role's Format-A line for the current iteration."""
    try:
        lines = (mailbox / "LOG.md").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return False, "LOG.md is missing or unreadable"
    for line in lines:
        match = A_LEAD_RE.match(line) if role == "lead" else ROLE_LOG_RE.match(line)
        if match and int(match.group(1)) == iteration:
            if role == "lead" or match.group(2).lower() == role:
                return True, "LOG gate passed"
    return False, f"LOG.md has no iter {iteration} | {role} | line"

def _run_role(mailbox, iteration, role, runner, repo, state_path) -> bool:
    """Run a Lead/repair role, retrying a failed gate once."""
    for _attempt in range(2):
        result = runner.run(role, iteration, mailbox)
        if result != 0:
            raise RuntimeError(f"{role} runner failed with exit {result}")
        checks = (
            _commit_gate(mailbox, repo),
            _log_gate(mailbox, iteration, role),
        )
        failures = [note for ok, note in checks if not ok]
        if not failures:
            return True
    _update_state(state_path, {"status": "error", "phase": "error"})
    _append_log(
        mailbox,
        f"- iter {iteration} | loop | gate breach after {role}: "
        + "; ".join(failures),
    )
    return False

def _first_verdict(path: Path) -> tuple[str | None, str | None]:
    """Parse the first non-empty line with the shared verdict helpers."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    first = next((line.strip() for line in lines if line.strip()), "")
    match = VERDICT_RE.match(first)
    if not match:
        return None, None
    word = match.group(1).upper()
    if word not in KNOWN_VERDICTS:
        return None, None
    parsed_word, scope = parse_verdict_scope(first)
    if parsed_word != word:
        return None, None
    rest = first[match.end():].strip()
    if not rest.startswith("scope="):
        return word, None
    if word != "ITERATE" or not SCOPE_RE.fullmatch(rest):
        return None, None
    return word, scope.lower() if scope else None

def _apply_verdict(
    mailbox, state_path, repair_path, iteration, verdict, scope
) -> int | None:
    """Persist a terminal verdict or queue the next ITERATE role."""
    if verdict in OUTCOMES:
        status, phase, code = OUTCOMES[verdict]
        _update_state(state_path, {"status": status, "phase": phase})
        return code
    repairs = _counter(repair_path)
    if scope in (None, "design"):
        repair_path.write_text("0\n", encoding="utf-8")
    elif repairs < 2:
        repair_path.write_text(f"{repairs + 1}\n", encoding="utf-8")
    else:
        repair_path.write_text("0\n", encoding="utf-8")
        _append_log(
            mailbox,
            f"- iter {iteration} | loop | repair cap hit; "
            "forcing full Lead",
        )
    _update_state(state_path, {"status": "running", "phase": "idle"})
    return None

def run_loop(
    mailbox: Path,
    max_iterations: int,
    runner: RoleRunner,
    *,
    repo: Path | None = None,
) -> int:
    """Run the durable Lead/repair -> Evaluator state machine."""
    mailbox = Path(mailbox).resolve()
    lock = _acquire_lock(mailbox)
    if lock is None:
        return 5
    try:
        state_path = mailbox / "STATE.md"
        if not (mailbox / "LOG.md").is_file():
            (mailbox / "LOG.md").write_text(
                "# Trio loop log\n", encoding="utf-8"
            )
        state = _read_state(state_path)
        repair_path = mailbox / ".repairs"
        while True:
            status = state["status"].strip().lower()
            terminal = TERMINAL_CODES.get(status.split()[0] if status else "")
            if terminal is not None:
                return terminal
            iteration = _number(state["iteration"])
            # Crash-resume: Lead+gates already landed, so do not bump
            # iteration or re-run Lead (fixes driver.sh:107-108).
            if state["phase"].strip().lower() == "lead-done":
                result = runner.run("evaluator", iteration, mailbox)
                if result != 0:
                    raise RuntimeError(
                        f"evaluator runner failed with exit {result}"
                    )
                verdict, scope = _first_verdict(mailbox / "VERDICT.md")
                if verdict is None:
                    _update_state(
                        state_path, {"status": "error", "phase": "error"}
                    )
                    _append_log(
                        mailbox,
                        f"- iter {iteration} | loop | unparseable verdict",
                    )
                    return 3
                code = _apply_verdict(
                    mailbox,
                    state_path,
                    repair_path,
                    iteration,
                    verdict,
                    scope,
                )
                if code is not None:
                    return code
                state = _read_state(state_path)
                continue
            phase = state["phase"].strip().lower()
            if phase in ("lead-running", "repair-running"):
                role = phase.split("-", 1)[0]
            else:
                # Budget applies to new passes, not interrupted role retries.
                if iteration >= max_iterations:
                    return 4
                role = "repair" if _counter(repair_path) else "lead"
                iteration += 1
            _update_state(
                state_path,
                {
                    "iteration": str(iteration),
                    "status": "running",
                    "phase": f"{role}-running",
                },
            )
            if not _run_role(
                mailbox, iteration, role, runner, repo, state_path
            ):
                return 3
            _update_state(
                state_path, {"status": "running", "phase": "lead-done"}
            )
            state = _read_state(state_path)
    finally:
        shutil.rmtree(lock, ignore_errors=True)

class _PortableRunner:
    def run(self, role: str, iteration: int, mailbox: Path) -> int:
        script = (
            Path(__file__).resolve().parent.parent
            / "portable"
            / "driver.sh"
        )
        environment = os.environ.copy()
        environment["LOOP_DIR"] = str(Path(mailbox).resolve())
        # The shell shim only runs one role. Gates, verdicts, repairs, and
        # resume stay in run_loop so this runner never parses VERDICT.md.
        result = subprocess.run(
            [str(script), "--run-role", role],
            check=False,
            env=environment,
        )
        return result.returncode

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the Trio state machine")
    run.add_argument("--mailbox", type=Path, required=True)
    run.add_argument("--max-iterations", type=int, required=True)
    run.add_argument("--runner", choices=("portable",), default="portable")
    args = parser.parse_args(argv)
    try:
        return run_loop(args.mailbox, args.max_iterations, _PortableRunner())
    except NotImplementedError as exc:
        print(f"trio_loop.py: {exc}", file=sys.stderr)
        return 3

if __name__ == "__main__":
    sys.exit(main())

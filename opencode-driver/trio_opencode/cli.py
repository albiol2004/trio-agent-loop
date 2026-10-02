#!/usr/bin/env python3
"""``trio-opencode`` command line entry point: start | resume | status |
abandon | land | doctor. See SPEC.md for the full contract."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trio_opencode import driver, rootfree, steplib  # noqa: E402

EXIT = {
    "shipped": 0, "landed": 0,
    "error": 3, "conflict": 3,
    "max_iterations": 4,
    "needs_human": 5, "blocked": 5,
    "needs_retirement": 6,
    "needs_land": 8,
    "refused": 9,
    "cancelled": 130,
}


def _exit_code(result: dict) -> int:
    if "code" in result and result["code"] is not None:
        try:
            return int(result["code"])
        except (TypeError, ValueError):
            pass
    return EXIT.get(result.get("status", ""), 3)


def _print(result: Any) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def _load_config(path: str | None):
    from trio_opencode import config as config_mod  # lazy: config slice
    return config_mod.load_config(path)


def _git_toplevel(path: Path) -> Path:
    return Path(driver._git_toplevel(path))


def _record_for(mailbox: Path) -> "rootfree.LeadWorktree | None":
    repo = _git_toplevel(mailbox)
    mailbox_rel = rootfree._mailbox_rel(repo, mailbox)
    slug = rootfree.loop_slug(mailbox_rel)
    return rootfree.load_record(repo, slug)


def _progress(line: str) -> None:
    # driver.run()'s `out` callback is progress/diagnostic text (e.g.
    # "previous-run builder ... merged into HEAD", a failed-attempt notice)
    # — never part of the CLI's JSON contract. It must go to stderr, never
    # stdout, or it corrupts the single JSON object a caller parses from
    # stdout (surfaced by a concurrent-builders crash+resume: `begin()`'s
    # reclaim of more than one dead builder worktree prints one line per
    # branch before the final result).
    print(line, file=sys.stderr)


# ------------------------------------------------------------------ start
def _cli_acceptance_flag(args: argparse.Namespace) -> bool | None:
    """``--acceptance``/``--no-acceptance`` as ``True``/``False``/``None``
    ("not passed" -- argparse's shared ``dest`` with no default gives
    ``None`` when neither flag was given)."""
    return getattr(args, "acceptance", None)


def _open_loop_run_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """D12: the open-loop settings flags, passed through to ``driver.run()``
    (which resolves/refuses them the same way for both ``start`` and
    ``resume``)."""
    return {
        "isolate_workers": getattr(args, "isolate_workers", None),
        "slice_eval_concurrency": getattr(args, "slice_eval_concurrency", None),
        "slice_eval_drain_seconds": getattr(args, "slice_eval_drain_seconds", None),
        "kill_check": getattr(args, "kill_check", None),
    }


def cmd_start(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    cfg = _load_config(args.config)
    from trio_opencode import config as config_mod  # lazy: config slice
    acceptance = config_mod.resolve_acceptance(cfg, cli_flag=_cli_acceptance_flag(args))
    result = driver.run(mailbox, cfg, mode="start", max_iterations=args.max_iterations,
                        run_token=args.run_token, root_free=not args.in_place, out=_progress,
                        acceptance=acceptance, **_open_loop_run_kwargs(args))
    _print(result)
    return _exit_code(result)


def cmd_resume(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    cfg = _load_config(args.config)
    # docs/FROZEN-ACCEPTANCE.md / native/launch.sh: the switch is a START
    # decision only -- a resume replays whatever the first start of this
    # mailbox recorded (the registry/``.opencode-result.json``/``.driver.json``),
    # never the config file or TRIO_ACCEPTANCE again (an in-flight run's
    # pin/freeze state already depends on it). An explicit CLI flag at
    # resume is refused exactly as native/launch.sh refuses
    # ``--acceptance`` at resume, rather than silently honoured or ignored.
    cli_flag = _cli_acceptance_flag(args)
    if cli_flag is not None:
        _print({"status": "error", "reason":
               "trio-opencode resume: --acceptance/--no-acceptance is a start-only flag "
               "(resume replays the recorded value); start a fresh run to change it"})
        return 2
    recorded = driver.recorded_acceptance(mailbox)
    result = driver.run(mailbox, cfg, mode="resume", max_iterations=args.max_iterations,
                        run_token=args.run_token, root_free=not args.in_place, out=_progress,
                        acceptance=bool(recorded), **_open_loop_run_kwargs(args))
    _print(result)
    return _exit_code(result)


# ----------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    out: dict[str, Any] = {"mailbox": str(mailbox)}
    state_path = mailbox / "STATE.md"
    if state_path.is_file():
        out["state"] = steplib.TL._read_state(state_path)
    driver_json = driver._read_json(mailbox / driver.DRIVER_FILE)
    if driver_json:
        out["driver_json"] = driver_json
    result_json = driver._read_json(mailbox / driver.RESULT_FILE)
    if result_json:
        out["result"] = result_json
    reg_path = driver.registry_path(mailbox)
    registry = driver._read_json(reg_path)
    if registry:
        out["registry"] = registry
    try:
        record = _record_for(mailbox)
    except Exception:  # noqa: BLE001 - status is best-effort/read-only
        record = None
    if record is not None:
        out["lead_worktree"] = record.to_dict()
    _print(out)
    return 0


# ---------------------------------------------------------------- abandon
def cmd_abandon(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    driver_json = driver._read_json(mailbox / driver.DRIVER_FILE)
    pid = driver_json.get("pid")
    if pid and driver._pid_alive(pid):
        _print({"status": "error", "reason": "driver already running (pid "
               f"{pid}); refusing to abandon"})
        return 9

    lock = mailbox / ".lock"
    released = None
    if lock.is_dir():
        lpid = steplib.TL._lock_pid(lock)
        if lpid <= 0 or not steplib.TL._pid_alive(lpid):
            steplib.TL._discard_lock_dir(mailbox, lock)
            released = "released (dead)"
        else:
            released = "foreign (live)"

    out: dict[str, Any] = {"mailbox": str(mailbox), "lock": released}
    try:
        record = _record_for(mailbox)
    except Exception as exc:  # noqa: BLE001
        record = None
        out["lead_worktree_error"] = str(exc)
    if record is not None and not record.landed and not record.abandoned:
        try:
            out["abandon"] = rootfree.abandon(record, force=args.force)
        except rootfree.RootFreeError as exc:
            _print({"status": "error", "reason": str(exc)})
            return 3
    reg_path = driver.registry_path(mailbox)
    reg = driver._read_json(reg_path)
    if reg:
        reg["state"] = "abandoned"
        driver._atomic_write_json(reg_path, reg)
    out["status"] = "abandoned"
    _print(out)
    return 0


# ------------------------------------------------------------------- land
def cmd_land(args: argparse.Namespace) -> int:
    mailbox = Path(args.mailbox).resolve()
    record = _record_for(mailbox)
    if record is None:
        _print({"status": "error", "reason": "no root-free Lead worktree record for "
               f"{mailbox}"})
        return 3
    result = rootfree.land(record)
    if result["status"] == "landed":
        result["teardown"] = rootfree.teardown(record)
    _print(result)
    return EXIT.get(result["status"], 8)


# ----------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load_config(args.config)
    from trio_opencode import doctor as doctor_mod  # lazy: config slice
    # doctor.run() prints its own JSON report (via `out`, default `print`)
    # and returns a plain 0/1 exit code — it is not a result dict.
    return doctor_mod.run(cfg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trio-opencode")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, run_opts: bool = False) -> None:
        p.add_argument("--mailbox", required=True)
        if run_opts:
            p.add_argument("--max-iterations", type=int, default=4)
            p.add_argument("--config", default=None)
            p.add_argument("--run-token", default=None)
            p.add_argument("--in-place", action="store_true")
            p.add_argument("--acceptance", dest="acceptance", action="store_true", default=None,
                          help="r19 frozen acceptance on for this run (start only)")
            p.add_argument("--no-acceptance", dest="acceptance", action="store_false",
                          help="r19 frozen acceptance off for this run (start only)")
            # D12: open-loop settings (no-ops on a lockstep mailbox; a
            # one-line stderr notice is printed instead of applying them).
            p.add_argument("--isolate-workers", dest="isolate_workers", action="store_true",
                          default=None, help="open-loop: isolate builders/slice-evals "
                          "in worktrees (default on)")
            p.add_argument("--no-isolate-workers", dest="isolate_workers", action="store_false",
                          help="open-loop: do not isolate builders/slice-evals in worktrees")
            p.add_argument("--slice-eval-concurrency", dest="slice_eval_concurrency", type=int,
                          default=None, help="open-loop: max concurrent slice-evals "
                          "(default 4; forced to 1 without --isolate-workers)")
            p.add_argument("--slice-eval-drain-seconds", dest="slice_eval_drain_seconds",
                          type=float, default=None,
                          help="open-loop: exit-drain budget for running slice-evals")
            p.add_argument("--no-kill-check", dest="kill_check", action="store_false",
                          default=None, help="open-loop: disable the builder kill check")

    p_start = sub.add_parser("start")
    add_common(p_start, run_opts=True)
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume")
    add_common(p_resume, run_opts=True)
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser("status")
    add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_abandon = sub.add_parser("abandon")
    add_common(p_abandon)
    p_abandon.add_argument("--force", action="store_true")
    p_abandon.set_defaults(func=cmd_abandon)

    p_land = sub.add_parser("land")
    add_common(p_land)
    p_land.set_defaults(func=cmd_land)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--config", default=None)
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - reported, never a bare traceback
        _print({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

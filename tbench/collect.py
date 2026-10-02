#!/usr/bin/env python3
"""Walk a Harbor jobs directory and print a TSV summary of trio-opencode
runs: task, reward, exception type (infra vs none), agent wall seconds,
driver status/exit code, iterations, and token totals.

Reads only files Harbor and ``TrioOpenCodeAgent`` themselves wrote
(``result.json`` at the trial root; ``agent/trio-summary.json`` and
``agent/trio-usage.json`` inside it) -- no Harbor import required, so this
runs standalone against any jobs dir.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

FIELDS: tuple[str, ...] = (
    "task",
    "reward",
    "exception_type",
    "agent_wall_seconds",
    "driver_status",
    "driver_exit_code",
    "iterations",
    "tokens_in",
    "tokens_out",
    "tokens_cache",
    "usage_source",
    "reasoning_tokens",
    "api_equiv_usd_estimate",
    "trial_dir",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _wall_seconds(result: dict[str, Any]) -> float | None:
    timing = result.get("agent_execution")
    if not isinstance(timing, dict):
        return None
    start = _parse_iso(timing.get("started_at"))
    end = _parse_iso(timing.get("finished_at"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _reward(result: dict[str, Any]) -> Any:
    verifier_result = result.get("verifier_result")
    if not isinstance(verifier_result, dict):
        return None
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict) or not rewards:
        return None
    if "reward" in rewards:
        return rewards["reward"]
    return next(iter(rewards.values()))


def _exception_type(result: dict[str, Any]) -> str:
    """"infra" classification is just "an exception was recorded at all" --
    this agent raises on musl bases, missing host assets, and non-retryable
    driver failures, so any ``exception_info`` here is an infra-side stop,
    never a verifier-graded failure (those show up as ``reward`` instead)."""
    exception_info = result.get("exception_info")
    if isinstance(exception_info, dict):
        exc_type = exception_info.get("exception_type")
        if isinstance(exc_type, str) and exc_type:
            return exc_type
        return "infra"
    return "none"


def collect_trial(trial_dir: Path) -> dict[str, Any]:
    result = _read_json(trial_dir / "result.json")
    summary = _read_json(trial_dir / "agent" / "trio-summary.json")
    usage = _read_json(trial_dir / "agent" / "trio-usage.json")

    totals = usage.get("totals")
    totals = totals if isinstance(totals, dict) else {}

    return {
        "task": result.get("task_name") or trial_dir.name,
        "reward": _reward(result),
        "exception_type": _exception_type(result),
        "agent_wall_seconds": _wall_seconds(result),
        "driver_status": summary.get("status"),
        "driver_exit_code": summary.get("driver_exit_code"),
        "iterations": summary.get("iterations"),
        "tokens_in": totals.get("input_tokens"),
        "tokens_out": totals.get("output_tokens"),
        "tokens_cache": totals.get("cache_read_tokens"),
        "usage_source": usage.get("usage_source"),
        "reasoning_tokens": totals.get("reasoning_tokens"),
        "api_equiv_usd_estimate": usage.get("api_equiv_usd_estimate"),
        "trial_dir": str(trial_dir),
    }


def find_trial_dirs(jobs_root: Path) -> list[Path]:
    """A trial directory is wherever a ``result.json`` lives (the same
    convention ``harbor.models.trial.paths.TrialPaths.result_path`` uses),
    so this works whether *jobs_root* is a single job dir or a jobs/ root
    containing many."""
    return sorted({p.parent for p in jobs_root.rglob("result.json")})


def _format_row(row: dict[str, Any]) -> str:
    return "\t".join("" if row.get(field) is None else str(row.get(field)) for field in FIELDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "jobs_dir", type=Path, help="A Harbor jobs/ root, or a single job directory"
    )
    args = parser.parse_args(argv)

    print("\t".join(FIELDS))
    for trial_dir in find_trial_dirs(args.jobs_dir):
        print(_format_row(collect_trial(trial_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

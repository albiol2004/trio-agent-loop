import json
import subprocess
import sys
from pathlib import Path

from collect import FIELDS, collect_trial, find_trial_dirs, main


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _make_trial(root: Path, name: str, *, with_exception: bool = False) -> Path:
    trial_dir = root / name
    result = {
        "task_name": name,
        "agent_execution": {
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:10:00+00:00",
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    if with_exception:
        result["exception_info"] = {"exception_type": "RuntimeError", "exception_message": "boom"}
        result.pop("verifier_result")
    _write_json(trial_dir / "result.json", result)
    _write_json(trial_dir / "agent" / "trio-summary.json", {
        # trio-summary.json's "iterations" is already a count -- the agent
        # itself normalizes the driver result's iteration list down to an
        # int before writing it (see trio_tbench_agent.py's run()).
        "status": "shipped", "driver_exit_code": 0, "iterations": 3,
    })
    _write_json(trial_dir / "agent" / "trio-usage.json", {
        "usage_source": "json",
        "totals": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10},
    })
    return trial_dir


def test_find_trial_dirs_locates_result_json(tmp_path: Path):
    _make_trial(tmp_path, "task-a")
    _make_trial(tmp_path, "task-b")
    dirs = find_trial_dirs(tmp_path)
    assert {d.name for d in dirs} == {"task-a", "task-b"}


def test_collect_trial_happy_path(tmp_path: Path):
    trial_dir = _make_trial(tmp_path, "task-a")
    row = collect_trial(trial_dir)
    assert row["task"] == "task-a"
    assert row["reward"] == 1.0
    assert row["exception_type"] == "none"
    assert row["agent_wall_seconds"] == 600.0
    assert row["driver_status"] == "shipped"
    assert row["driver_exit_code"] == 0
    assert row["iterations"] == 3
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["tokens_cache"] == 10
    assert row["usage_source"] == "json"


def test_collect_trial_with_exception_reports_infra_type(tmp_path: Path):
    trial_dir = _make_trial(tmp_path, "task-b", with_exception=True)
    row = collect_trial(trial_dir)
    assert row["exception_type"] == "RuntimeError"
    assert row["reward"] is None


def test_collect_trial_missing_sidecars_is_defensive(tmp_path: Path):
    trial_dir = tmp_path / "bare"
    _write_json(trial_dir / "result.json", {"task_name": "bare"})
    row = collect_trial(trial_dir)
    assert row["task"] == "bare"
    assert row["reward"] is None
    assert row["driver_status"] is None
    assert row["tokens_in"] is None


def test_main_prints_tsv_header_and_rows(tmp_path: Path, capsys):
    _make_trial(tmp_path, "task-a")
    rc = main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split("\t") == list(FIELDS)
    assert len(out) == 2
    assert out[1].startswith("task-a\t")


def test_cli_entrypoint_runs_standalone(tmp_path: Path):
    _make_trial(tmp_path, "task-a")
    collect_py = Path(__file__).resolve().parents[1] / "collect.py"
    proc = subprocess.run(
        [sys.executable, str(collect_py), str(tmp_path)],
        check=True, capture_output=True, text=True,
    )
    lines = proc.stdout.splitlines()
    assert lines[0].split("\t") == list(FIELDS)
    assert "task-a" in lines[1]

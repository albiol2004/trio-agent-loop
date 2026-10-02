import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from usage import parse_opencode_usage


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_empty_root_yields_zero_usage(tmp_path: Path):
    result = parse_opencode_usage(tmp_path / "does-not-exist")
    assert result["usage_source"] == "none"
    assert result["totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert result["by_model"] == {}
    assert result["message_count"] == 0


def test_sums_assistant_messages_with_tokens(tmp_path: Path):
    storage = tmp_path / "storage"
    _write(storage / "session" / "msg1.json", {
        "id": "msg_1",
        "role": "assistant",
        "modelID": "glm-5.3-flash",
        "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 10, "write": 2}},
    })
    _write(storage / "session" / "msg2.json", {
        "id": "msg_2",
        "role": "assistant",
        "modelID": "deepseek-v4.1-flash",
        "tokens": {"input": 50, "output": 10},
    })
    # A non-assistant message must be ignored.
    _write(storage / "session" / "msg3.json", {
        "id": "msg_3", "role": "user", "tokens": {"input": 999, "output": 999},
    })

    result = parse_opencode_usage(storage)
    assert result["usage_source"] == "json"
    assert result["totals"]["input_tokens"] == 150
    assert result["totals"]["output_tokens"] == 30
    assert result["totals"]["reasoning_tokens"] == 5
    assert result["totals"]["cache_read_tokens"] == 10
    assert result["totals"]["cache_write_tokens"] == 2
    assert result["message_count"] == 2
    assert result["by_model"]["glm-5.3-flash"]["input_tokens"] == 100
    assert result["by_model"]["deepseek-v4.1-flash"]["input_tokens"] == 50


def test_dedupes_by_message_id_across_files(tmp_path: Path):
    storage = tmp_path / "storage"
    msg = {"id": "msg_dup", "role": "assistant", "tokens": {"input": 7, "output": 3}}
    _write(storage / "a.json", msg)
    _write(storage / "b.json", msg)  # same id, e.g. a streaming-update duplicate
    result = parse_opencode_usage(storage)
    assert result["message_count"] == 1
    assert result["totals"]["input_tokens"] == 7


def test_handles_nested_and_malformed_json_defensively(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    _write(storage / "nested.json", {
        "sessions": {
            "ses_1": {
                "messages": [
                    {"id": "m1", "role": "assistant", "tokens": {"input": 1, "output": 1}},
                ]
            }
        }
    })
    (storage / "broken.json").parent.mkdir(parents=True, exist_ok=True)
    (storage / "broken.json").write_text("{not valid json", encoding="utf-8")

    result = parse_opencode_usage(storage)
    assert result["totals"]["input_tokens"] == 1
    assert result["usage_source"] == "json"


def test_parses_sqlite_storage(tmp_path: Path):
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    db_path = storage / "data.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE message (id TEXT, role TEXT, data TEXT)")
    conn.execute(
        "INSERT INTO message VALUES (?, ?, ?)",
        (
            "m1",
            "assistant",
            json.dumps({"tokens": {"input": 42, "output": 8}, "role": "assistant", "id": "m1"}),
        ),
    )
    conn.commit()
    conn.close()

    result = parse_opencode_usage(storage)
    assert "sqlite" in result["usage_source"]
    assert result["totals"]["input_tokens"] == 42
    assert result["totals"]["output_tokens"] == 8


def test_merges_usage_across_multiple_roots(tmp_path: Path):
    root1 = tmp_path / "run-1" / "xdg" / "data"
    root2 = tmp_path / "run-2" / "xdg" / "data"
    _write(root1 / "a.json", {"id": "m1", "role": "assistant", "tokens": {"input": 1, "output": 1}})
    _write(root2 / "b.json", {"id": "m2", "role": "assistant", "tokens": {"input": 2, "output": 2}})

    result = parse_opencode_usage([root1, root2])
    assert result["totals"]["input_tokens"] == 3
    assert result["totals"]["output_tokens"] == 3
    assert result["message_count"] == 2


def test_cli_entrypoint_prints_json(tmp_path: Path):
    storage = tmp_path / "storage"
    _write(storage / "a.json", {"id": "m1", "role": "assistant", "tokens": {"input": 5, "output": 1}})
    usage_py = Path(__file__).resolve().parents[1] / "usage.py"
    proc = subprocess.run(
        [sys.executable, str(usage_py), str(storage)],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["totals"]["input_tokens"] == 5

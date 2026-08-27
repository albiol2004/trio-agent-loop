"""Unit tests for the dependency-free trioctl executable."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).parents[1] / "trioctl"


def load_trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def profile(**builder):
    return {
        "version": 1,
        "roles": {
            "lead": {
                "provider": "cursor",
                "model_family": "grok-4.6",
                "fallback_model": "cursor-grok-4.6-medium",
                "effort": "medium",
                "fast": False,
            },
            "evaluator": {
                "provider": "cursor",
                "model_family": "grok-4.6",
                "fallback_model": "cursor-grok-4.6-medium",
                "effort": "medium",
                "fast": False,
            },
            "builder": {
                "provider": "cursor",
                "model_family": "gpt-5.6-luna",
                "fallback_model": "gpt-5.6-luna-max",
                "effort": "max",
                "fast": False,
                **builder,
            },
            "scout": {
                "provider": "cursor",
                "model_family": "gpt-5.6-luna",
                "fallback_model": "gpt-5.6-luna-max",
                "effort": "max",
                "fast": False,
            },
        },
    }


def model(name: str, *efforts: str):
    return {
        "id": name,
        "model": name,
        "displayName": name,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort} for effort in efforts
        ],
    }


@pytest.fixture
def fake_broker():
    """Run a local-only HTTP broker with deterministic session responses."""
    state = {
        "posts": [],
        "events": [],
        "patches": [],
        "authorization": [],
        "session_gets": 0,
        "item_queries": [],
        "wait_item_reads": 0,
        "assistant_after": None,
        "item_sequence": None,
        "session_runner_ids": None,
        "runner_gets": 0,
        "session_statuses": ["idle", "running", "idle"],
        "online_runners": [{"runner_id": "runner-1", "online": True}],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def send_json(self, status, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            state["authorization"].append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            path = urlparse(self.path).path
            if path == "/v1/sessions":
                state["posts"].append(body)
                self.send_json(
                    201,
                    {
                        "id": "session-1",
                        "status": "idle",
                        "runner_id": None,
                    },
                )
                return
            if path == "/v1/sessions/session-1/events":
                state["events"].append(body)
                self.send_json(202, {"queued": True})
                return
            self.send_json(404, {"detail": "unknown POST"})

        def do_PATCH(self):
            state["authorization"].append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            state["patches"].append(body)
            self.send_json(
                200,
                {
                    "id": "session-1",
                    "status": "idle",
                    "runner_id": body["runner_id"],
                },
            )

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.endswith("/items"):
                query = parse_qs(parsed.query)
                state["item_queries"].append(query)
                limit = (query.get("limit") or [""])[0]
                # Wait polls a short newest-first page (limit=10).
                if limit in {"1", "10"}:
                    state["wait_item_reads"] += 1
                    sequence = state["item_sequence"]
                    if sequence:
                        index = min(
                            state["wait_item_reads"] - 1,
                            len(sequence) - 1,
                        )
                        item = sequence[index]
                    else:
                        item = {
                            "id": "item-1",
                            "role": "user",
                            "type": "message",
                        }
                        assistant_after = state["assistant_after"]
                        if (
                            assistant_after is not None
                            and state["wait_item_reads"] >= assistant_after
                        ):
                            item = {
                                "id": "item-2",
                                "role": "assistant",
                                "type": "message",
                                "status": "completed",
                            }
                    self.send_json(200, {"data": [item]})
                else:
                    self.send_json(
                        200,
                        {"items": [{"id": "item-1", "role": "user"}]},
                    )
                return
            if parsed.path == "/v1/runners":
                state["runner_gets"] += 1
                self.send_json(200, {"data": state["online_runners"]})
                return
            state["session_gets"] += 1
            index = min(
                state["session_gets"] - 1,
                len(state["session_statuses"]) - 1,
            )
            status = (
                state["session_statuses"][index]
                if state["session_statuses"]
                else "idle"
            )
            payload = {"id": "session-1", "status": status}
            runner_ids = state["session_runner_ids"]
            if runner_ids:
                runner_index = min(index, len(runner_ids) - 1)
                payload["runner_id"] = runner_ids[runner_index]
            self.send_json(200, payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_cursor_lead_resolves_grok_medium():
    trioctl = load_trioctl()

    result = trioctl.resolve_role(
        "lead",
        profile(),
        models=[model("cursor-grok-4.6-medium"), model("cursor-grok-4.6-medium-fast")],
    )

    assert result == {
        "role": "lead",
        "provider": "cursor",
        "model": "cursor-grok-4.6-medium",
        "reasoning_effort": None,
        "model_effort": "medium",
        "source": "cursor-model-list",
    }


def test_registry_profile_tracks_stored_role_prompt_revision():
    trioctl = load_trioctl()

    assert trioctl.REGISTRY_PROFILE == "cursor-grok-4.6-medium+luna-max-v2"


def test_codex_family_chooses_newest_available_model():
    trioctl = load_trioctl()
    models = [
        model("gpt-5.6-luna", "high", "xhigh"),
        model("gpt-5.7-luna", "medium", "xhigh"),
        model("gpt-5.8-sol", "xhigh"),
    ]

    result = trioctl.resolve_role(
        "builder",
        profile(
            provider="codex",
            model_family="luna",
            fallback_model="gpt-5.6-luna",
            effort="xhigh",
        ),
        models=models,
    )

    assert result["model"] == "gpt-5.7-luna"
    assert result["source"] == "codex-model-list"


def test_codex_resolution_rejects_unsupported_effort():
    trioctl = load_trioctl()

    with pytest.raises(trioctl.TrioctlError, match="does not support effort"):
        trioctl.resolve_role(
            "builder",
            profile(provider="codex", model_family="luna", effort="xhigh"),
            models=[model("gpt-5.7-luna", "low", "medium")],
        )


def test_codex_resolution_fails_loudly_without_matching_entitlement():
    trioctl = load_trioctl()

    with pytest.raises(trioctl.TrioctlError, match="no available codex model"):
        trioctl.resolve_role(
            "builder",
            profile(provider="codex", model_family="luna", effort="xhigh"),
            models=[model("gpt-5.8-sol", "xhigh")],
        )


def test_codex_fallback_requires_explicit_opt_in():
    trioctl = load_trioctl()

    result = trioctl.resolve_role(
        "builder",
        profile(),
        models=[],
        allow_fallback=True,
    )

    assert result["model"] == "gpt-5.6-luna-max"
    assert result["source"] == "explicit-fallback"
    assert result["reasoning_effort"] is None
    assert result["model_effort"] == "max"


def test_load_config_requires_every_role(tmp_path: Path):
    trioctl = load_trioctl()
    path = tmp_path / "config.toml"
    path.write_text("version = 1\n[roles.lead]\nprovider = 'claude'\n")

    with pytest.raises(trioctl.TrioctlError, match="missing roles"):
        trioctl.load_config(path)


def test_codex_models_completes_handshake_and_paginates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    fake = tmp_path / "codex"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            for line in sys.stdin:
                request = json.loads(line)
                if request.get("method") == "initialize":
                    response = {"id": request["id"], "result": {"userAgent": "fake"}}
                elif request.get("method") == "model/list":
                    cursor = request["params"].get("cursor")
                    name = "gpt-5.7-luna" if cursor else "gpt-5.6-luna"
                    response = {
                        "id": request["id"],
                        "result": {
                            "data": [{"id": name, "model": name}],
                            "nextCursor": None if cursor else "page-2",
                        },
                    }
                else:
                    continue
                print(json.dumps(response), flush=True)
            """
        )
    )
    fake.chmod(0o755)
    monkeypatch.setattr(trioctl.shutil, "which", lambda command: str(fake))

    assert [item["model"] for item in trioctl.codex_models(timeout=2)] == [
        "gpt-5.6-luna",
        "gpt-5.7-luna",
    ]


def test_cursor_family_selects_non_fast_effort_variant():
    trioctl = load_trioctl()
    models = [
        model("gpt-5.6-luna-high"),
        model("gpt-5.6-luna-max"),
        model("gpt-5.6-luna-max-fast"),
        model("composer-2.5"),
    ]

    result = trioctl.resolve_role("builder", profile(), models=models)

    assert result == {
        "role": "builder",
        "provider": "cursor",
        "model": "gpt-5.6-luna-max",
        "reasoning_effort": None,
        "model_effort": "max",
        "source": "cursor-model-list",
    }


def test_cursor_models_parses_authenticated_cli_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    fake = tmp_path / "cursor-agent"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("Available models")
            print()
            print("auto - Auto (default)")
            print("gpt-5.6-luna-max - GPT-5.6 Luna 1M Max")
            """
        )
    )
    fake.chmod(0o755)
    monkeypatch.setattr(trioctl.shutil, "which", lambda command: str(fake))

    assert trioctl.cursor_models(timeout=2) == [
        {"id": "auto", "model": "auto", "displayName": "Auto (default)"},
        {
            "id": "gpt-5.6-luna-max",
            "model": "gpt-5.6-luna-max",
            "displayName": "GPT-5.6 Luna 1M Max",
        },
    ]


def test_cursor_builder_runs_headless_with_resolved_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="WORKER_OK\n", stderr="")

    monkeypatch.setattr(trioctl.shutil, "which", lambda command: "/bin/cursor-agent")
    monkeypatch.setattr(trioctl.subprocess, "run", fake_run)

    output = trioctl.run_cursor_worker(
        "builder",
        profile(),
        prompt="Implement the bounded change.",
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
    )

    assert output == "WORKER_OK"
    assert seen["command"] == [
        "/bin/cursor-agent",
        "-p",
        "--output-format",
        "text",
        "--model",
        "gpt-5.6-luna-max",
        "--force",
        "--trust",
        "--approve-mcps",
        "--workspace",
        str(tmp_path.resolve()),
    ]
    assert "TASK FROM LEAD:\nImplement the bounded change." in seen["kwargs"]["input"]


def test_cursor_scout_is_forced_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="SCOUT_OK", stderr="")

    monkeypatch.setattr(trioctl.shutil, "which", lambda command: "/bin/cursor-agent")
    monkeypatch.setattr(trioctl.subprocess, "run", fake_run)

    trioctl.run_cursor_worker(
        "scout",
        profile(),
        prompt="Inspect the module.",
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
    )

    assert seen["command"][-2:] == ["--mode", "ask"]


def test_session_create_posts_goal_fields_and_extras(fake_broker, capsys):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "create",
            "--agent-id",
            "agent-1",
            "--model",
            "model-1",
            "--message",
            "Do the task.",
            "--title",
            "A task",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "session-1"
    assert state["posts"] == [
        {
            "agent_id": "agent-1",
            "model": "model-1",
            "message": "Do the task.",
            "title": "A task",
            "model_override": "model-1",
            "initial_items": [],
        }
    ]
    assert state["runner_gets"] == 1
    assert state["patches"] == [{"runner_id": "runner-1"}]
    assert state["events"] == [
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Do the task."}],
            },
        }
    ]


def test_create_session_runner_id_arg_takes_precedence_over_env(
    fake_broker, monkeypatch
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_runners"] = [
        {"runner_id": "runner-1", "online": True},
        {"runner_id": "runner-2", "online": True},
    ]
    monkeypatch.setenv("TRIO_OMNIGENT_RUNNER_ID", "runner-1")
    client = trioctl.broker_http.BrokerClient(base_url)

    created = client.create_session(
        "agent-1", "model-1", "hi", "title", runner_id="runner-2"
    )

    assert created["runner_id"] == "runner-2"
    assert state["patches"] == [{"runner_id": "runner-2"}]


def test_create_session_uses_env_runner_id_when_arg_absent(fake_broker, monkeypatch):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_runners"] = [
        {"runner_id": "runner-1", "online": True},
        {"runner_id": "runner-2", "online": True},
    ]
    monkeypatch.setenv("TRIO_OMNIGENT_RUNNER_ID", "runner-2")
    client = trioctl.broker_http.BrokerClient(base_url)

    created = client.create_session("agent-1", "model-1", "hi", "title")

    assert created["runner_id"] == "runner-2"
    assert state["patches"] == [{"runner_id": "runner-2"}]


def test_create_session_runner_id_arg_not_online_raises(fake_broker, monkeypatch):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_runners"] = [{"runner_id": "runner-1", "online": True}]
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError) as excinfo:
        client.create_session(
            "agent-1", "model-1", "hi", "title", runner_id="runner-9"
        )

    assert "--runner-id=runner-9 is not an online runner" in str(excinfo.value)


def test_create_session_multi_runner_error_lists_ids_and_remedy(
    fake_broker, monkeypatch
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_runners"] = [
        {"runner_id": "runner-1", "online": True},
        {"runner_id": "runner-2", "online": True},
        {"runner_id": "runner-3", "online": True},
    ]
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError) as excinfo:
        client.create_session("agent-1", "model-1", "hi", "title")

    message = str(excinfo.value)
    assert "expected exactly one online runner, found 3:" in message
    assert "\n  runner-1\n" in message
    assert "\n  runner-2\n" in message
    assert "\n  runner-3\n" in message
    assert "TRIO_OMNIGENT_RUNNER_ID=<id>" in message
    assert "--runner-id <id>" in message


def test_session_wait_polls_running_to_idle(fake_broker, capsys):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    # Final idle must include a completed assistant message.
    state["assistant_after"] = 3
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "wait",
            "session-1",
            "--timeout",
            "1",
            "--interval",
            "0.01",
            "--base-url",
            base_url,
        ]
    )
    assert args.func(args) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert state["session_gets"] == 3
    assert all(
        query == {"limit": ["10"], "order": ["desc"]}
        for query in state["item_queries"]
    )


def test_session_wait_keeps_polling_for_assistant_while_running(
    fake_broker, capsys
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["idle", "running", "running", "idle"]
    # Assistant progress can appear while status is still running.
    state["assistant_after"] = 3
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "wait",
            "session-1",
            "--timeout",
            "1",
            "--interval",
            "0.01",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert state["session_gets"] == 4


def test_session_wait_ignores_turn_item_while_idle_before_running(
    fake_broker, capsys
):
    """Turn-start-while-idle must not complete the wait (live broker race)."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    # Live create returns idle with a turn item before Cursor is running.
    state["session_statuses"] = ["idle", "idle", "running", "idle"]
    state["session_runner_ids"] = [None, None, "runner-1", "runner-1"]
    state["item_sequence"] = [
        {"id": "item-user", "role": "user", "type": "message"},
        {
            "id": "item-turn",
            "type": "turn",
            "status": "in_progress",
        },
        {
            "id": "item-turn",
            "type": "turn",
            "status": "in_progress",
        },
        {
            "id": "item-assistant",
            "role": "assistant",
            "type": "message",
            "status": "completed",
        },
    ]
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "wait",
            "session-1",
            "--timeout",
            "1",
            "--interval",
            "0.01",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert state["session_gets"] == 4


def test_session_wait_ignores_idle_blip_without_assistant_message(
    fake_broker, capsys
):
    """Post-bind running→idle with only a resource_event is not terminal."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["idle", "running", "idle", "running", "idle"]
    state["session_runner_ids"] = [
        "runner-1",
        "runner-1",
        "runner-1",
        "runner-1",
        "runner-1",
    ]
    state["item_sequence"] = [
        {"id": "res-1", "type": "resource_event", "status": "completed"},
        {"id": "res-1", "type": "resource_event", "status": "completed"},
        {"id": "res-1", "type": "resource_event", "status": "completed"},
        {"id": "res-1", "type": "resource_event", "status": "completed"},
        {
            "id": "asst-1",
            "role": "assistant",
            "type": "message",
            "status": "completed",
        },
    ]
    client = trioctl._session_client(base_url)
    snapshot = trioctl._wait_for_session(
        client,
        "session-1",
        timeout=1,
        interval=0.01,
        stable_idle=0.05,
    )
    assert snapshot["status"] == "idle"
    assert state["session_gets"] == 5


def test_session_wait_completes_when_create_already_finished(
    fake_broker, capsys
):
    """Create may return after the turn is already idle with a pong."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["idle"]
    state["session_runner_ids"] = ["runner-1"]
    state["assistant_after"] = 1
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "wait",
            "session-1",
            "--timeout",
            "1",
            "--interval",
            "0.01",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "idle"
    assert state["session_gets"] == 1


def test_session_wait_does_not_complete_on_assistant_before_running(
    fake_broker,
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["idle"]
    state["session_runner_ids"] = [None]
    state["assistant_after"] = 1

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        trioctl._wait_for_session(
            trioctl._session_client(base_url),
            "session-1",
            timeout=0,
            interval=0,
        )


def test_session_wait_times_out_before_turn_starts(fake_broker):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["idle"]

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        trioctl._wait_for_session(
            trioctl._session_client(base_url),
            "session-1",
            timeout=0,
            interval=0,
        )


def test_session_wait_timeout_is_distinct_from_failed_status(fake_broker):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["running"]
    state["assistant_after"] = 1

    with pytest.raises(trioctl.TrioctlError, match="timed out"):
        trioctl._wait_for_session(
            trioctl._session_client(base_url),
            "session-1",
            timeout=0,
            interval=0,
        )


def test_session_wait_returns_failure_for_failed_status(fake_broker, capsys):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_statuses"] = ["failed"]
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "wait",
            "session-1",
            "--timeout",
            "1",
            "--interval",
            "0",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_session_read_returns_items_and_query(fake_broker, capsys):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "read",
            "session-1",
            "--limit",
            "7",
            "--order",
            "desc",
            "--base-url",
            base_url,
        ]
    )
    assert args.func(args) == 0

    assert json.loads(capsys.readouterr().out)["items"]
    assert state["item_queries"] == [{"limit": ["7"], "order": ["desc"]}]


def test_session_auth_uses_home_token(fake_broker, tmp_path, monkeypatch, capsys):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    token_dir = tmp_path / ".omnigent"
    token_dir.mkdir()
    (token_dir / "auth_tokens.json").write_text(
        json.dumps({base_url: {"token": "file-token", "expires_at": 9e18}})
    )
    monkeypatch.delenv("OMNIGENT_TOKEN", raising=False)
    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(trioctl.broker_http.Path, "home", lambda: tmp_path)
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "session",
            "create",
            "--agent-id",
            "agent-1",
            "--model",
            "model-1",
            "--message",
            "hello",
            "--base-url",
            base_url,
        ]
    )

    assert args.func(args) == 0
    capsys.readouterr()
    assert state["authorization"] == [
        "Bearer file-token",
        "Bearer file-token",
        "Bearer file-token",
    ]


def test_doctor_without_live_session_skips_session_api(
    tmp_path, monkeypatch, capsys
):
    trioctl = load_trioctl()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "_profile": trioctl.REGISTRY_PROFILE,
                "trio-omnigent-lead": {},
                "trio-omnigent-evaluator": {},
            }
        )
    )
    models = [
        model("cursor-grok-4.6-medium"),
        model("gpt-5.6-luna-max"),
    ]
    monkeypatch.setattr(trioctl, "load_config", lambda path: profile())
    monkeypatch.setattr(
        trioctl.shutil,
        "which",
        lambda command: f"/bin/{command}",
    )
    monkeypatch.setattr(trioctl, "omnigent_contract", lambda: "ok")
    monkeypatch.setattr(
        trioctl,
        "check_cursor_approval_mode",
        lambda: {"check": "cursor:approval-mode", "ok": True, "detail": "ok"},
    )
    monkeypatch.setattr(trioctl, "cursor_models", lambda timeout: models)
    monkeypatch.setattr(
        trioctl,
        "omnigent_registry_path",
        lambda: registry,
    )
    args = trioctl.parser().parse_args(["omnigent", "doctor"])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "omnigent:session-api" not in output


def test_doctor_live_session_flag_is_parsed_without_running_it():
    trioctl = load_trioctl()

    args = trioctl.parser().parse_args(
        ["omnigent", "doctor", "--live-session"]
    )

    assert args.live_session is True


def test_broker_loader_does_not_import_real_omnigent():
    prior = sys.modules.get("omnigent")

    load_trioctl()

    assert sys.modules.get("omnigent") is prior

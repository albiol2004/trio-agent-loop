"""Unit tests for the dependency-free trioctl executable."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import threading
import time
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
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
            "docs": {
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
        "session_rows": [],
        "deletes": [],
        "session_list_queries": [],
        # Host-launch path (0.14 / fresh host with zero runners).
        "online_hosts": [{"host_id": "host-1", "status": "online"}],
        "runner_launches": [],
        # None = POST /v1/sessions leaves runner_id unset so the
        # client falls back to POST /v1/hosts/{id}/runners.
        "create_runner_id": None,
        # Override GET .../items when limit is not the wait poll.
        "item_rows": None,
        "pending_inputs": [],
        "fail_launch": False,
        # After this many POST /events, items include the user text.
        "items_after_event_count": None,
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
                        "runner_id": state["create_runner_id"],
                    },
                )
                return
            if path == "/v1/sessions/session-1/events":
                state["events"].append(body)
                self.send_json(202, {"queued": True})
                return
            # Fallback when POST /v1/sessions ignored host_id.
            if "/hosts/" in path and path.endswith("/runners"):
                state["runner_launches"].append(body)
                if state["fail_launch"]:
                    self.send_json(500, {"detail": "launch failed"})
                    return
                self.send_json(
                    200,
                    {"runner_id": "spawned-1", "status": "launching"},
                )
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
                    self.send_json(200, {"items": _item_list_payload(state)})
                return
            if parsed.path == "/v1/hosts":
                self.send_json(200, {"hosts": state["online_hosts"]})
                return
            if parsed.path == "/v1/runners":
                state["runner_gets"] += 1
                self.send_json(200, {"data": state["online_runners"]})
                return
            if parsed.path == "/v1/sessions":
                state["session_list_queries"].append(parse_qs(parsed.query))
                self.send_json(200, {"data": state["session_rows"]})
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
            payload = {
                "id": "session-1",
                "status": status,
                "pending_inputs": list(state["pending_inputs"]),
            }
            runner_ids = state["session_runner_ids"]
            if runner_ids:
                runner_index = min(index, len(runner_ids) - 1)
                payload["runner_id"] = runner_ids[runner_index]
            self.send_json(200, payload)

        def do_DELETE(self):
            state["authorization"].append(self.headers.get("Authorization"))
            state["deletes"].append(urlparse(self.path).path)
            self.send_json(200, {"deleted": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _item_list_payload(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for GET /items when this is not a wait-style poll.

    Default: a user item with no text (ensure_first_prompt treats that
    as landed). Tests override via item_rows or items_after_event_count
    to exercise pending-prompt retry.
    """
    if state["item_rows"] is not None:
        return list(state["item_rows"])
    needed = state["items_after_event_count"]
    if needed is not None:
        if len(state["events"]) < needed:
            return []
        text = "landed"
        if state["events"]:
            try:
                text = state["events"][-1]["data"]["content"][0]["text"]
            except (KeyError, IndexError, TypeError):
                pass
        return [{"id": "item-1", "role": "user", "text": text}]
    return [{"id": "item-1", "role": "user"}]


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

    assert trioctl.REGISTRY_PROFILE == "cursor-grok-4.6-medium+glm-5.2-max-v3"


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


def test_load_config_docs_falls_back_to_scout_table(tmp_path: Path):
    trioctl = load_trioctl()
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """\
            version = 1
            [roles.lead]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.evaluator]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.builder]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            [roles.scout]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            """
        )
    )

    config = trioctl.load_config(path)

    assert config["_docs_role_fallback"] is True
    assert config["roles"]["docs"] == config["roles"]["scout"]

    resolved = trioctl.resolve_role(
        "docs", config, models=[model("gpt-5.6-luna-max")]
    )
    assert resolved["provider"] == "cursor"
    assert resolved["model"] == "gpt-5.6-luna-max"


def test_load_config_keeps_explicit_docs_table(tmp_path: Path):
    trioctl = load_trioctl()
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """\
            version = 1
            [roles.lead]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.evaluator]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.builder]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            [roles.scout]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            [roles.docs]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            """
        )
    )

    config = trioctl.load_config(path)

    assert config["_docs_role_fallback"] is False
    assert config["roles"]["docs"]["provider"] == "cursor"

    resolved = trioctl.resolve_role(
        "docs", config, models=[model("gpt-5.6-luna-max")]
    )
    assert resolved["provider"] == "cursor"


def test_resolve_docs_role_uses_own_config():
    trioctl = load_trioctl()

    result = trioctl.resolve_role(
        "docs",
        profile(),
        models=[model("gpt-5.6-luna-max")],
    )

    assert result["role"] == "docs"
    assert result["provider"] == "cursor"
    assert result["model"] == "gpt-5.6-luna-max"


def test_cursor_docs_runs_headless_with_resolved_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="DOCS_OK\n", stderr="")

    monkeypatch.setattr(trioctl.shutil, "which", lambda command: "/bin/cursor-agent")
    monkeypatch.setattr(trioctl.subprocess, "run", fake_run)

    output = trioctl.run_cursor_worker(
        "docs",
        profile(),
        prompt="Document the shipped change.",
        workspace=tmp_path,
        models=[model("gpt-5.6-luna-max")],
    )

    assert output == "DOCS_OK"
    assert "--mode" not in seen["command"]
    assert "TASK FROM LEAD:\nDocument the shipped change." in seen["kwargs"]["input"]


def test_parser_accepts_docs_role_for_resolve_and_run():
    trioctl = load_trioctl()

    resolve_args = trioctl.parser().parse_args(["omnigent", "resolve", "docs"])
    assert resolve_args.role == "docs"

    run_args = trioctl.parser().parse_args(
        ["omnigent", "run", "docs", "--prompt-file", "-", "--workspace", "."]
    )
    assert run_args.role == "docs"


def test_command_resolve_docs_json_falls_back_to_scout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    trioctl = load_trioctl()
    path = tmp_path / "config.toml"
    path.write_text(
        textwrap.dedent(
            """\
            version = 1
            [roles.lead]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.evaluator]
            provider = "cursor"
            model_family = "grok-4.6"
            effort = "medium"
            [roles.builder]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            [roles.scout]
            provider = "cursor"
            model_family = "gpt-5.6-luna"
            effort = "max"
            """
        )
    )
    monkeypatch.setattr(trioctl, "cursor_models", lambda timeout=None: [
        model("gpt-5.6-luna-max")
    ])

    args = trioctl.parser().parse_args(
        ["omnigent", "resolve", "docs", "--config", str(path), "--json"]
    )
    exit_code = trioctl.command_resolve(args)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["role"] == "docs"
    assert payload["provider"] == "cursor"
    assert payload["model"] == "gpt-5.6-luna-max"


def test_docs_role_config_yaml_uses_cursor_native_harness():
    path = SCRIPT.parent / "trio-omnigent-roles" / "docs" / "config.yaml"
    text = path.read_text()

    assert "harness: cursor-native" in text
    assert "name: trio-omnigent-docs" in text


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
    posted = state["posts"][0]
    assert posted["agent_id"] == "agent-1"
    assert posted["model"] == "model-1"
    assert posted["message"] == "Do the task."
    assert posted["title"] == "A task"
    assert posted["model_override"] == "model-1"
    assert posted["initial_items"] == []
    # Dedicated runner: host_id + workspace, not a PATCH onto a runner.
    assert posted["host_id"] == "host-1"
    assert posted["workspace"]
    assert state["runner_gets"] == 0
    assert state["patches"] == []
    assert state["runner_launches"] == [
        {"session_id": "session-1", "workspace": posted["workspace"]}
    ]
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


def test_create_session_multi_host_error_lists_ids_and_remedy(
    fake_broker, monkeypatch
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_hosts"] = [
        {"host_id": "host-1", "status": "online"},
        {"host_id": "host-2", "status": "online"},
        {"host_id": "host-3", "status": "online"},
    ]
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.delenv("TRIO_OMNIGENT_HOST_ID", raising=False)
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError) as excinfo:
        client.create_session("agent-1", "model-1", "hi", "title")

    message = str(excinfo.value)
    assert "expected exactly one online host, found 3:" in message
    assert "\n  host-1\n" in message
    assert "\n  host-2\n" in message
    assert "\n  host-3\n" in message
    assert "TRIO_OMNIGENT_HOST_ID=<id>" in message
    assert "--host-id <id>" in message
    # Failed before POST /v1/sessions, so nothing to orphan-delete.
    assert state["posts"] == []
    assert state["deletes"] == []


def test_create_session_zero_hosts_errors(fake_broker, monkeypatch):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_hosts"] = []
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.delenv("TRIO_OMNIGENT_HOST_ID", raising=False)
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError) as excinfo:
        client.create_session("agent-1", "model-1", "hi", "title")

    assert "expected exactly one online host, found 0" in str(excinfo.value)


def test_create_session_skips_launch_when_create_returns_runner(
    fake_broker, monkeypatch
):
    """If POST /v1/sessions honours host_id, do not POST /runners."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["create_runner_id"] = "from-create"
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    client = trioctl.broker_http.BrokerClient(base_url)

    created = client.create_session("agent-1", "model-1", "hi", "title")

    assert created["id"] == "session-1"
    assert created["runner_id"] == "from-create"
    assert state["runner_launches"] == []
    assert state["patches"] == []


def test_create_session_host_id_env_selects_among_many(fake_broker, monkeypatch):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["online_hosts"] = [
        {"host_id": "host-1", "status": "online"},
        {"host_id": "host-2", "status": "online"},
    ]
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_OMNIGENT_HOST_ID", "host-2")
    client = trioctl.broker_http.BrokerClient(base_url)

    created = client.create_session("agent-1", "model-1", "hi", "title")

    assert created["id"] == "session-1"
    assert state["posts"][0]["host_id"] == "host-2"
    assert state["runner_launches"][0]["session_id"] == "session-1"


def test_first_prompt_retries_on_same_session_never_duplicates(
    fake_broker, monkeypatch
):
    """Cold TUI: first event ACKs but no item; re-POST the same id."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    # Land only after the second /events POST (create + one retry).
    state["items_after_event_count"] = 2
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", "0.05")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", "0.01")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", "3")
    client = trioctl.broker_http.BrokerClient(base_url)

    created = client.create_session(
        "agent-1", "model-1", "Please land", "title"
    )

    assert created["id"] == "session-1"
    assert len(state["posts"]) == 1
    assert len(state["events"]) == 2
    assert state["events"][0]["data"]["content"][0]["text"] == "Please land"
    assert state["events"][1]["data"]["content"][0]["text"] == "Please land"
    assert state["deletes"] == []


def test_pending_inputs_drain_counts_as_miss_then_repost(
    fake_broker, monkeypatch
):
    """Pending composer that clears without an item is a miss."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["item_rows"] = []
    state["pending_inputs"] = [{"id": "p1"}]
    state["items_after_event_count"] = None
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", "0.2")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", "0.02")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", "3")
    client = trioctl.broker_http.BrokerClient(base_url)

    # After the first poll sees pending, clear it so the helper
    # treats the drain as a miss (even after the extra grace items
    # poll) and re-posts. Land the user row only after that retry.
    original_get = client.get_session
    original_items = client.get_items
    polls = {"n": 0}

    def get_session_then_clear(session_id: str):
        polls["n"] += 1
        snap = original_get(session_id)
        if polls["n"] >= 2:
            state["pending_inputs"] = []
        return snap

    def get_items_until_repost(session_id: str, **kwargs):
        if len(state["events"]) >= 2:
            state["item_rows"] = [
                {"id": "item-1", "role": "user", "text": "hi"}
            ]
        return original_items(session_id, **kwargs)

    client.get_session = get_session_then_clear  # type: ignore[method-assign]
    client.get_items = get_items_until_repost  # type: ignore[method-assign]
    created = client.create_session("agent-1", "model-1", "hi", "title")

    assert created["id"] == "session-1"
    assert len(state["posts"]) == 1
    assert len(state["events"]) >= 2


def test_pending_drain_grace_poll_skips_repost_when_item_lands(
    fake_broker, monkeypatch
):
    """Pending clears then the next items poll shows the user row: no re-post."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["item_rows"] = []
    state["pending_inputs"] = [{"id": "p1"}]
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", "0.4")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", "0.01")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", "3")
    client = trioctl.broker_http.BrokerClient(base_url)
    original_items = client.get_items
    original_session = client.get_session
    item_polls = {"n": 0}
    session_polls = {"n": 0}

    def get_items_then_land(session_id: str, **kwargs):
        item_polls["n"] += 1
        # 1: first loop empty. 2: drain-loop empty. 3: grace poll lands.
        if item_polls["n"] >= 3:
            state["item_rows"] = [
                {"id": "item-1", "role": "user", "text": "hello"}
            ]
        return original_items(session_id, **kwargs)

    def get_session_then_clear(session_id: str):
        session_polls["n"] += 1
        snap = original_session(session_id)
        if session_polls["n"] >= 2:
            state["pending_inputs"] = []
        return snap

    client.get_items = get_items_then_land  # type: ignore[method-assign]
    client.get_session = get_session_then_clear  # type: ignore[method-assign]
    created = client.create_session("agent-1", "model-1", "hello", "title")

    assert created["id"] == "session-1"
    assert len(state["posts"]) == 1
    assert len(state["events"]) == 1


def test_orphan_deleted_when_runner_launch_fails(
    fake_broker, tmp_path, monkeypatch
):
    """Failed start after POST /v1/sessions must DELETE the session."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["fail_launch"] = True
    ids_path = tmp_path / "session.ids"
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_MAILBOX_SESSION_IDS", str(ids_path))
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError):
        client.create_session("agent-1", "model-1", "hi", "title")

    assert state["posts"]  # create happened
    assert state["deletes"] == ["/v1/sessions/session-1"]
    assert "session-1" in ids_path.read_text(encoding="utf-8")
    # Never a second create for the failed start.
    assert len(state["posts"]) == 1


def test_orphan_deleted_when_first_prompt_never_lands(
    fake_broker, tmp_path, monkeypatch
):
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["item_rows"] = []
    ids_path = tmp_path / "session.ids"
    monkeypatch.delenv("TRIO_OMNIGENT_RUNNER_ID", raising=False)
    monkeypatch.setenv("TRIO_MAILBOX_SESSION_IDS", str(ids_path))
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_WAIT", "0")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_INTERVAL", "0")
    monkeypatch.setenv("TRIO_OMNIGENT_PROMPT_ATTEMPTS", "1")
    client = trioctl.broker_http.BrokerClient(base_url)

    with pytest.raises(trioctl.broker_http.BrokerHttpError) as excinfo:
        client.create_session("agent-1", "model-1", "hi", "title")

    assert "first prompt did not land" in str(excinfo.value)
    assert state["deletes"] == ["/v1/sessions/session-1"]
    assert "session-1" in ids_path.read_text(encoding="utf-8")
    assert len(state["posts"]) == 1


def test_omnigent_contract_probe_prefers_subagent_spec():
    """Doctor must import 0.14 `_resolve_subagent_spec`, then 0.12 alias."""
    trioctl = load_trioctl()
    src = inspect.getsource(trioctl.omnigent_contract)
    # The executed probe (not the comment) tries 0.14 then 0.12.
    probe_start = src.index("probe = ")
    probe = src[probe_start:]
    assert "from omnigent.server.routes.sessions import _resolve_subagent_spec" in probe
    assert "from omnigent.server.routes.sessions import _resolve_agent_spec" in probe
    assert "except ImportError" in probe
    assert probe.index("_resolve_subagent_spec") < probe.index(
        "from omnigent.server.routes.sessions import _resolve_agent_spec"
    )


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
    # Assistant on the last status still needs the idle dwell.
    assert state["session_gets"] >= 5


def test_session_wait_ignores_restart_blip_with_zero_items(tmp_path):
    """Right after an Omnigent restart, a session can cycle bound -> running
    -> idle within seconds with **zero** items at all (no resource_event,
    nothing) before the runner unbinds again. That must not be terminal;
    the wait only completes once a real item shows up."""
    trioctl = load_trioctl()

    class ZeroItemBlipClient:
        def __init__(self) -> None:
            self.status_calls = 0

        # idle(pre-run) -> running -> idle(blip, 0 items) ->
        # idle(still blip, 0 items) -> idle(real item, terminal)
        _statuses = ["idle", "running", "idle", "idle", "idle"]

        def get_session(self, session_id: str) -> dict[str, object]:
            index = min(self.status_calls, len(self._statuses) - 1)
            self.status_calls += 1
            return {
                "id": session_id,
                "status": self._statuses[index],
                "runner_id": "runner-1",
            }

        def get_items(
            self, session_id: str, *, limit: int = 100, order: str = "asc"
        ) -> dict[str, list[dict[str, str]]]:
            # Item reads happen in lockstep with status reads (same loop
            # iteration): index 0/1 pre-run/running, 2/3 the zero-item
            # blip, 4 the first real item.
            index = self.status_calls
            if index >= 4:
                return {"data": [{"id": "res-1", "type": "resource_event"}]}
            return {"data": []}

    client = ZeroItemBlipClient()

    snapshot = trioctl._wait_for_session(
        client,
        "session-1",
        timeout=1,
        interval=0,
        stable_idle=0.0,
    )

    assert snapshot["status"] == "idle"
    # Returned only once an item appeared (5th iteration), not on the
    # zero-item blip at iteration 3.
    assert client.status_calls == 5


def test_session_wait_assistant_idle_holds_for_dwell():
    """Idle + completed assistant must not return before stable_idle."""
    trioctl = load_trioctl()

    class IdleAckClient:
        def __init__(self) -> None:
            self.status_calls = 0

        def get_session(self, session_id: str) -> dict[str, object]:
            self.status_calls += 1
            return {
                "id": session_id,
                "status": "idle",
                "runner_id": "runner-1",
            }

        def get_items(
            self, session_id: str, *, limit: int = 100, order: str = "asc"
        ) -> dict[str, list[dict[str, str]]]:
            return {
                "data": [
                    {
                        "id": "ack",
                        "role": "assistant",
                        "type": "message",
                        "status": "completed",
                    }
                ]
            }

    client = IdleAckClient()
    started = time.monotonic()
    snapshot = trioctl._wait_for_session(
        client,
        "session-1",
        timeout=1,
        interval=0.01,
        stable_idle=0.05,
    )
    elapsed = time.monotonic() - started
    assert snapshot["status"] == "idle"
    assert elapsed >= 0.05
    assert client.status_calls > 1


def test_session_wait_running_resets_assistant_idle_dwell():
    """A running snapshot clears idle_since so the dwell starts again."""
    trioctl = load_trioctl()

    class ResetDwellClient:
        def __init__(self) -> None:
            self.status_calls = 0
            self.running_at: float | None = None

        def get_session(self, session_id: str) -> dict[str, object]:
            self.status_calls += 1
            # Two idle polls, one running blip, then idle for the dwell.
            if self.status_calls == 3:
                self.running_at = time.monotonic()
                status = "running"
            else:
                status = "idle"
            return {
                "id": session_id,
                "status": status,
                "runner_id": "runner-1",
            }

        def get_items(
            self, session_id: str, *, limit: int = 100, order: str = "asc"
        ) -> dict[str, list[dict[str, str]]]:
            return {
                "data": [
                    {
                        "id": "ack",
                        "role": "assistant",
                        "type": "message",
                        "status": "completed",
                    }
                ]
            }

    client = ResetDwellClient()
    snapshot = trioctl._wait_for_session(
        client,
        "session-1",
        timeout=1,
        interval=0.02,
        stable_idle=0.08,
    )
    assert snapshot["status"] == "idle"
    assert client.running_at is not None
    assert time.monotonic() - client.running_at >= 0.08
    assert client.status_calls > 4


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


def test_list_and_delete_sessions_over_http(fake_broker):
    """`BrokerClient.list_sessions`/`delete_session` speak the same wire
    protocol as the rest of the client (plain GET/DELETE, JSON body)."""
    trioctl = load_trioctl()
    base_url, state = fake_broker
    state["session_rows"] = [
        {"id": "session-9", "title": "trioctl mbx iteration 1 lead"}
    ]
    client = trioctl.broker_http.BrokerClient(base_url)

    listed = client.list_sessions()
    assert listed == {"data": state["session_rows"]}
    assert "kind" not in state["session_list_queries"][-1]

    listed_any = client.list_sessions(kind="any")
    assert listed_any == {"data": state["session_rows"]}
    assert state["session_list_queries"][-1]["kind"] == ["any"]

    deleted = client.delete_session("session-9")
    assert deleted == {"deleted": True}
    assert state["deletes"] == ["/v1/sessions/session-9"]


class _FakeSessionsClient:
    """Offline double for `_prune_broker_sessions`: list/read/delete only."""

    def __init__(self, rows, mailbox: Path | None = None):
        self.rows = rows
        self.mailbox = mailbox
        self.calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.archived_before_delete: list[Path] | None = None

    def list_sessions(self, limit=20, after=None):
        return {"data": self.rows}

    def get_items(self, session_id, limit=100, order="asc", after=None):
        self.calls.append(("items", session_id))
        return {"items": [{"role": "assistant", "content": f"hi from {session_id}"}]}

    def delete_session(self, session_id):
        if self.mailbox is not None:
            self.archived_before_delete = sorted(
                (self.mailbox / ".sessions").glob("*.jsonl")
            )
        self.calls.append(("delete", session_id))
        self.deleted.append(session_id)
        return {"deleted": True}


def _row(session_id, title, status="idle", created_at="2026-01-01T00:00:00Z"):
    return {
        "id": session_id,
        "title": title,
        "status": status,
        "created_at": created_at,
    }


def test_prune_selects_only_matching_titles(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl mbx iteration 1 lead"),
        _row("s2", "trioctl other-mailbox iteration 1 lead"),
        _row("s3", "unrelated broker session"),
    ]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert counts == {
        "archived": 1,
        "deleted": 1,
        "skipped_running": 0,
        "skipped_failed": 0,
        "failed": 0,
    }
    assert client.deleted == ["s1"]
    archived = list((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 1
    lines = archived[0].read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["id"] == "s1"
    assert json.loads(lines[1])["content"] == "hi from s1"


def test_prune_skips_running_sessions(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s1", "trioctl mbx iteration 1 lead", status="running")]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert counts["skipped_running"] == 1
    assert counts["archived"] == 0
    assert counts["deleted"] == 0
    assert client.deleted == []
    assert not (mailbox / ".sessions").exists()


def test_prune_archives_before_deleting(tmp_path: Path):
    """The archive file must exist on disk before the session is deleted."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s1", "trioctl mbx iteration 1 lead")]
    client = _FakeSessionsClient(rows, mailbox=mailbox)

    trioctl._prune_broker_sessions(client, mailbox)

    assert client.calls == [("items", "s1"), ("delete", "s1")]
    assert client.archived_before_delete is not None
    assert len(client.archived_before_delete) == 1


def test_prune_dry_run_deletes_nothing(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s1", "trioctl mbx iteration 1 lead")]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, dry_run=True)

    assert counts == {
        "archived": 0,
        "deleted": 0,
        "skipped_running": 0,
        "skipped_failed": 0,
        "failed": 0,
    }
    assert client.calls == []
    assert client.deleted == []
    assert not (mailbox / ".sessions").exists()


def test_prune_all_scope_matches_every_trioctl_title(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl mbx iteration 1 lead"),
        _row("s2", "trioctl other-mailbox iteration 1 lead"),
        _row("s3", "not a trioctl session"),
    ]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, all_scope=True)

    assert counts["deleted"] == 2
    assert set(client.deleted) == {"s1", "s2"}


def test_prune_keep_failed_skips_failed_and_error_sessions(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl mbx iteration 1 lead", status="failed"),
        _row("s2", "trioctl mbx iteration 2 lead", status="error"),
    ]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, keep_failed=True)

    assert counts["skipped_failed"] == 2
    assert counts["deleted"] == 0
    assert client.deleted == []


def test_prune_without_keep_failed_still_archives_failed_sessions(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s1", "trioctl mbx iteration 1 lead", status="failed")]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, keep_failed=False)

    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]


def test_prune_id_scoped_selects_only_listed_ids(tmp_path: Path):
    """`session_ids=` scopes to exactly those ids, ignoring title prefix --
    an older run's session for the same mailbox must survive even though
    its title matches."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s-old", "trioctl mbx iteration 1 lead"),
        _row("s-new", "trioctl mbx iteration 2 lead"),
        _row("s-other", "unrelated broker session"),
    ]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, session_ids=["s-new"])

    assert counts == {
        "archived": 1,
        "deleted": 1,
        "skipped_running": 0,
        "skipped_failed": 0,
        "failed": 0,
    }
    assert client.deleted == ["s-new"]


def test_prune_id_scoped_deletes_running_session(tmp_path: Path):
    """Post-loop id list must DELETE even if status is still running."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s-new", "trioctl mbx iteration 1 lead", status="running")]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, session_ids=["s-new"])

    assert counts["skipped_running"] == 0
    assert counts["deleted"] == 1
    assert client.deleted == ["s-new"]


def test_prune_id_scoped_archives_before_deleting(tmp_path: Path):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s-new", "trioctl mbx iteration 1 lead")]
    client = _FakeSessionsClient(rows, mailbox=mailbox)

    trioctl._prune_broker_sessions(client, mailbox, session_ids=["s-new"])

    assert client.calls == [("items", "s-new"), ("delete", "s-new")]
    assert client.archived_before_delete is not None
    assert len(client.archived_before_delete) == 1


class _KindAwareSessionsClient:
    """Offline double whose `list_sessions` filters by `kind` the way the
    real broker route does: `kind=None`/`"default"` returns only rows
    whose own `kind` is `"default"` (the field is absent on plain `_row()`
    fixtures, which default to `"default"`); `kind="any"` returns every
    row regardless of its own kind. Used to prove `--include-sub-agents`
    actually widens what the listing call requests and sees."""

    def __init__(self, rows):
        self.rows = rows
        self.list_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def list_sessions(self, limit=20, after=None, kind=None):
        self.list_calls.append({"limit": limit, "after": after, "kind": kind})
        if kind == "any":
            selected = list(self.rows)
        else:
            selected = [r for r in self.rows if r.get("kind", "default") == "default"]
        return {"data": selected}

    def get_items(self, session_id, limit=100, order="asc", after=None):
        return {"items": [{"role": "assistant", "content": f"hi from {session_id}"}]}

    def delete_session(self, session_id):
        self.deleted.append(session_id)
        return {"deleted": True}


def test_prune_flag_off_lists_default_kind_and_never_sees_sub_agent_rows(
    tmp_path: Path,
):
    """With `include_sub_agents` unset, the listing call must not widen
    `kind` (so the server's own `kind=default` stays in effect) and a
    sub_agent-kind row -- even one whose title matches -- never becomes
    visible to select."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl mbx iteration 1 lead"),
        {**_row("s2", "trioctl mbx iteration 2 evaluator"), "kind": "sub_agent"},
    ]
    client = _KindAwareSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert client.list_calls[0]["kind"] is None
    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]


def test_prune_include_sub_agents_widens_kind_and_matches_sub_agent_row(
    tmp_path: Path,
):
    """With `include_sub_agents=True`, the listing call requests
    `kind="any"` and a sub_agent-kind row whose title matches the same
    `trioctl <mailbox> ` prefix is archived and deleted -- selection
    itself is unchanged, only the listing's visibility is widened."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        {**_row("s1", "trioctl mbx iteration 1 lead"), "kind": "sub_agent"},
        _row("s2", "unrelated broker session"),
    ]
    client = _KindAwareSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, include_sub_agents=True)

    assert client.list_calls[0]["kind"] == "any"
    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]
    archived = list((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 1


def test_prune_include_sub_agents_matches_new_scheme_lead_title(
    tmp_path: Path,
):
    """The locked title scheme ``trioctl <mailbox> <role>:iteration <N>``
    still carries the ``trioctl <mailbox> `` prefix, so a sub_agent-kind
    Lead worker session is archived and deleted with --include-sub-agents."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        {**_row("s1", "trioctl mbx lead:iteration 1"), "kind": "sub_agent"},
        _row("s2", "unrelated broker session"),
    ]
    client = _KindAwareSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, include_sub_agents=True)

    assert client.list_calls[0]["kind"] == "any"
    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]


def test_prune_include_sub_agents_matches_new_scheme_evaluator_title(
    tmp_path: Path,
):
    """Same prefix match for an Evaluator worker session under the locked
    ``trioctl <mailbox> evaluator:iteration <N>`` scheme."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        {
            **_row("s1", "trioctl mbx evaluator:iteration 1"),
            "kind": "sub_agent",
        },
    ]
    client = _KindAwareSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, include_sub_agents=True)

    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]


def test_prune_never_matches_registration_anchor_titles(tmp_path: Path):
    """Registration-anchor and other non-loop titles must never be pruned
    even with --include-sub-agents: they are listed (kind=any) but fail the
    ``trioctl <mailbox> `` prefix match. Covers the trio-omnigent skill's
    own anchor titles, the colon-bearing role-config path title, untitled
    and empty rows, and another mailbox's loop session."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        {**_row("s1", "trio-omnigent-lead"), "kind": "sub_agent"},
        {**_row("s2", "trio-omnigent-evaluator"), "kind": "sub_agent"},
        {
            **_row("s3", "lead:omnigent/trio-omnigent-roles/lead"),
            "kind": "sub_agent",
        },
        {**_row("s4", ""), "kind": "sub_agent"},
        {
            **_row("s5", "trioctl other-mailbox lead:iteration 1"),
            "kind": "sub_agent",
        },
    ]
    client = _KindAwareSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox, include_sub_agents=True)

    assert client.list_calls[0]["kind"] == "any"
    assert counts["deleted"] == 0
    assert client.deleted == []


def test_prune_loop_session_cleanup_mailbox_matches_only_its_prefix(
    tmp_path: Path,
):
    """A mailbox literally named ``loop-session-cleanup`` matches only its
    own ``trioctl loop-session-cleanup `` prefix; a look-alike title for a
    different mailbox and an unrelated title are left alone."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "loop-session-cleanup"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl loop-session-cleanup lead:iteration 1"),
        _row("s2", "trioctl other-mailbox lead:iteration 1"),
        _row("s3", "loop-session-cleanup idle session"),
    ]
    client = _FakeSessionsClient(rows)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert counts["deleted"] == 1
    assert client.deleted == ["s1"]


def test_session_title_matches_mailbox_helper_prefix_rules():
    """Direct unit test for the prefix helper backing mailbox-scoped prune
    matching: new scheme, old scheme, and the registration-anchor titles
    that must never match."""
    trioctl = load_trioctl()
    mailbox = Path("mbx")

    # New locked scheme still carries the ``trioctl mbx `` prefix.
    assert trioctl._session_title_matches_mailbox(
        "trioctl mbx lead:iteration 1", mailbox
    )
    assert trioctl._session_title_matches_mailbox(
        "trioctl mbx evaluator:iteration 2 integration-eval", mailbox
    )
    # Old-style titles still carry the prefix, so they still match.
    assert trioctl._session_title_matches_mailbox(
        "trioctl mbx iteration 1 lead", mailbox
    )
    # Registration-anchor, role-config path, other-mailbox, and empty
    # titles never carry the prefix, so prune never deletes them.
    assert not trioctl._session_title_matches_mailbox("trio-omnigent-lead", mailbox)
    assert not trioctl._session_title_matches_mailbox(
        "lead:omnigent/trio-omnigent-roles/lead", mailbox
    )
    assert not trioctl._session_title_matches_mailbox(
        "trioctl other-mailbox lead:iteration 1", mailbox
    )
    assert not trioctl._session_title_matches_mailbox("", mailbox)


def test_command_sessions_prune_cli_include_sub_agents_flag_widens_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The `--include-sub-agents` CLI flag reaches `_prune_broker_sessions`
    and widens the listing's `kind`, without changing title matching."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [{**_row("s1", "trioctl mbx iteration 1 lead"), "kind": "sub_agent"}]
    client = _KindAwareSessionsClient(rows)
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url=None: client)
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "sessions",
            "prune",
            "--mailbox",
            str(mailbox),
            "--include-sub-agents",
        ]
    )

    assert args.func(args) == 0
    assert client.list_calls[0]["kind"] == "any"
    assert client.deleted == ["s1"]
    assert "archived 1, deleted 1" in capsys.readouterr().out


def _make_paging_sessions_client(broker_http_module):
    """Build the offline double whose broker rejects `limit` over 1000
    with HTTP 422 and serves items in pages, bound to one trioctl load's
    ``broker_http`` module so the raised error type matches what the code
    under test catches."""

    class _PagingSessionsClient:
        def __init__(self, rows, items_by_session):
            self.rows = rows
            self.items_by_session = items_by_session
            self.item_calls: list[tuple[str, int, str | None]] = []
            self.deleted: list[str] = []

        def list_sessions(self, limit=20, after=None):
            return {"data": self.rows}

        def get_items(self, session_id, limit=100, order="asc", after=None):
            if limit > 1000:
                raise broker_http_module.BrokerHttpError(
                    f"GET .../items?limit={limit} failed with HTTP 422: "
                    "limit must be <= 1000",
                    status_code=422,
                )
            self.item_calls.append((session_id, limit, after))
            all_items = self.items_by_session.get(session_id, [])
            # Mirrors the server's cursor contract: `after` is the id of the
            # last item of the previous page, not a numeric offset.
            if after is None:
                start = 0
            else:
                ids = [item["id"] for item in all_items]
                start = ids.index(after) + 1 if after in ids else len(all_items)
            page = all_items[start : start + limit]
            return {"items": page}

        def delete_session(self, session_id):
            self.deleted.append(session_id)
            return {"deleted": True}

    return _PagingSessionsClient


def test_prune_pages_archive_reads_past_the_1000_item_broker_cap(tmp_path: Path):
    """2500 items must come back as three 1000/1000/500 pages, not one
    limit=2500 request the broker would reject with HTTP 422."""
    trioctl = load_trioctl()
    _PagingSessionsClient = _make_paging_sessions_client(trioctl.broker_http)
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [_row("s1", "trioctl mbx iteration 1 lead")]
    items = [{"id": f"item-{i}", "role": "assistant"} for i in range(2500)]
    client = _PagingSessionsClient(rows, {"s1": items})

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert counts == {
        "archived": 1,
        "deleted": 1,
        "skipped_running": 0,
        "skipped_failed": 0,
        "failed": 0,
    }
    assert client.item_calls == [
        ("s1", 1000, None),
        ("s1", 1000, "item-999"),
        ("s1", 1000, "item-1999"),
    ]
    archived = list((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 1
    lines = archived[0].read_text(encoding="utf-8").splitlines()
    # One header line (the session row) plus every archived item.
    assert len(lines) == 1 + 2500
    assert json.loads(lines[1])["id"] == "item-0"
    assert json.loads(lines[-1])["id"] == "item-2499"


def test_fetch_session_items_paged_stops_on_non_advancing_cursor():
    """A broker bug that returns a full page whose last item id never
    changes, no matter what cursor was requested, would spin
    ``_fetch_session_items_paged`` forever chasing the same page. The
    guard must stop it with an error after the second stuck page instead
    of looping forever."""
    trioctl = load_trioctl()

    class _StuckCursorClient:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def get_items(self, session_id, limit=100, order="asc", after=None):
            self.calls.append(after)
            # Always a full page ending in the same id, regardless of the
            # cursor requested -- a pathological, never-advancing response.
            page = [
                {"id": f"item-{i}"}
                for i in range(trioctl.SESSION_ARCHIVE_PAGE_LIMIT - 1)
            ]
            page.append({"id": "item-stuck"})
            return {"items": page}

    client = _StuckCursorClient()

    items, error = trioctl._fetch_session_items_paged(client, "s1")

    assert error is not None
    assert "cursor did not advance" in str(error)
    # Stopped after the second (stuck) page, not spun forever.
    assert client.calls == [None, "item-stuck"]
    assert len(items) == 2 * trioctl.SESSION_ARCHIVE_PAGE_LIMIT


def test_prune_pages_through_more_sessions_than_the_default_list_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_prune_broker_sessions` must page an `after` session-id cursor past
    the broker's list page size to see every session, not just the first
    page -- otherwise a target session outside the first page is invisible
    to the prune, exactly like the server's default newest-20 window used
    to hide everything past the first 20 sessions."""
    trioctl = load_trioctl()
    monkeypatch.setattr(trioctl, "SESSION_LIST_PAGE_LIMIT", 2)

    class _PagedListClient:
        def __init__(self, rows):
            self.rows = rows
            self.list_calls: list[tuple[int, str | None]] = []
            self.deleted: list[str] = []

        def list_sessions(self, limit=20, after=None):
            self.list_calls.append((limit, after))
            ids = [row["id"] for row in self.rows]
            start = (
                0
                if after is None
                else (ids.index(after) + 1 if after in ids else len(self.rows))
            )
            return {"data": self.rows[start : start + limit]}

        def get_items(self, session_id, limit=100, order="asc", after=None):
            return {"items": []}

        def delete_session(self, session_id):
            self.deleted.append(session_id)
            return {"deleted": True}

    rows = [
        _row("s-1", "not a trioctl session"),
        _row("s-2", "not a trioctl session"),
        _row("s-target", "trioctl mbx iteration 5 lead"),
    ]
    client = _PagedListClient(rows)
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()

    counts = trioctl._prune_broker_sessions(client, mailbox)

    assert client.list_calls == [(2, None), (2, "s-2")]
    assert client.deleted == ["s-target"]
    assert counts["deleted"] == 1


def test_prune_one_failing_session_read_does_not_abort_the_others(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A session whose item read errors out must not stop the run: other
    sessions still get archived and deleted. The default now retries the
    bad read once, then DELETES the failing session anyway with a
    partial archive and a stderr warning -- delete is the only path
    that kills the broker's tmux terminal, so keeping it would leak RAM.
    The good session still archives and deletes normally."""
    trioctl = load_trioctl()
    _PagingSessionsClient = _make_paging_sessions_client(trioctl.broker_http)

    class _OneFailsClient(_PagingSessionsClient):
        def __init__(self, rows, items_by_session):
            super().__init__(rows, items_by_session)
            # Counts get_items calls per session so the test can prove
            # the bad id was attempted twice (initial read + one retry).
            self.calls_per_session: dict[str, int] = {}

        def get_items(self, session_id, limit=100, order="asc", after=None):
            self.calls_per_session[session_id] = (
                self.calls_per_session.get(session_id, 0) + 1
            )
            if session_id == "s-bad":
                raise trioctl.broker_http.BrokerHttpError(
                    "GET .../items failed with HTTP 500", status_code=500
                )
            return super().get_items(
                session_id, limit=limit, order=order, after=after
            )

    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s-bad", "trioctl mbx iteration 1 lead"),
        _row("s-good", "trioctl mbx iteration 2 lead"),
    ]
    items = {"s-good": [{"id": "item-0", "role": "assistant"}]}
    client = _OneFailsClient(rows, items)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    # The bad session is a partial archive (counts as `failed`) but is
    # still deleted; the good session archives and deletes normally.
    assert counts["failed"] == 1
    assert counts["archived"] == 1
    assert counts["deleted"] == 2
    assert client.deleted == ["s-bad", "s-good"]
    # The bad id was read twice: initial attempt plus exactly one retry.
    assert client.calls_per_session["s-bad"] == 2
    # The partial-archive warning goes to stderr, not stdout.
    err = capsys.readouterr().err
    assert "transcript was not fully archived" in err
    assert "s-bad" in err
    # Both sessions are archived (the bad one with an empty transcript).
    archived = sorted((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 2


def test_prune_retry_succeeds_on_second_read_archives_and_deletes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """When the first item read fails but the retry succeeds, the session
    is fully archived and deleted with `failed` == 0 -- a transient
    broker hiccup must not strand a terminal or taint the counts."""
    trioctl = load_trioctl()
    _PagingSessionsClient = _make_paging_sessions_client(trioctl.broker_http)

    class _TransientFailClient(_PagingSessionsClient):
        def __init__(self, rows, items_by_session):
            super().__init__(rows, items_by_session)
            self.calls_per_session: dict[str, int] = {}

        def get_items(self, session_id, limit=100, order="asc", after=None):
            self.calls_per_session[session_id] = (
                self.calls_per_session.get(session_id, 0) + 1
            )
            # First attempt fails; the retry (second attempt) succeeds.
            if (
                session_id == "s-flaky"
                and self.calls_per_session["s-flaky"] == 1
            ):
                raise trioctl.broker_http.BrokerHttpError(
                    "GET .../items failed with HTTP 500", status_code=500
                )
            return super().get_items(
                session_id, limit=limit, order=order, after=after
            )

    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s-flaky", "trioctl mbx iteration 1 lead"),
        _row("s-good", "trioctl mbx iteration 2 lead"),
    ]
    items = {
        "s-flaky": [{"id": "item-0", "role": "assistant"}],
        "s-good": [{"id": "item-1", "role": "assistant"}],
    }
    client = _TransientFailClient(rows, items)

    counts = trioctl._prune_broker_sessions(client, mailbox)

    # Retry succeeded: no partial archive, no `failed`, both deleted.
    assert counts["failed"] == 0
    assert counts["archived"] == 2
    assert counts["deleted"] == 2
    assert client.deleted == ["s-flaky", "s-good"]
    # The flaky id was read exactly twice (fail then success).
    assert client.calls_per_session["s-flaky"] == 2
    # No partial-archive warning on a successful retry.
    assert capsys.readouterr().err == ""
    archived = sorted((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 2


def test_prune_keep_unarchived_keeps_failing_session_after_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """With `keep_unarchived=True`, a session whose item read fails even
    after one retry is kept (old behavior) while the good session still
    archives and deletes -- opting back into leak-rather-than-lose for
    callers that would rather keep an incomplete transcript."""
    trioctl = load_trioctl()
    _PagingSessionsClient = _make_paging_sessions_client(trioctl.broker_http)

    class _OneFailsClient(_PagingSessionsClient):
        def get_items(self, session_id, limit=100, order="asc", after=None):
            if session_id == "s-bad":
                raise trioctl.broker_http.BrokerHttpError(
                    "GET .../items failed with HTTP 500", status_code=500
                )
            return super().get_items(
                session_id, limit=limit, order=order, after=after
            )

    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s-bad", "trioctl mbx iteration 1 lead"),
        _row("s-good", "trioctl mbx iteration 2 lead"),
    ]
    items = {"s-good": [{"id": "item-0", "role": "assistant"}]}
    client = _OneFailsClient(rows, items)

    counts = trioctl._prune_broker_sessions(
        client, mailbox, keep_unarchived=True
    )

    # The bad session is kept after retry (old behavior); the good one
    # still archives and deletes.
    assert counts["failed"] == 1
    assert counts["archived"] == 1
    assert counts["deleted"] == 1
    assert client.deleted == ["s-good"]
    err = capsys.readouterr().err
    assert "session kept, not deleted" in err
    assert "s-bad" in err
    # Both transcripts are on disk, but only the good one was deleted.
    archived = sorted((mailbox / ".sessions").glob("*.jsonl"))
    assert len(archived) == 2


def test_command_sessions_prune_cli_keep_unarchived_flag_reaches_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The `--keep-unarchived` CLI flag is parsed by argparse and reaches
    `_prune_broker_sessions` as `keep_unarchived=True`, keeping a session
    whose transcript could not be fully archived after one retry."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s-bad", "trioctl mbx iteration 1 lead"),
        _row("s-good", "trioctl mbx iteration 2 lead"),
    ]

    captured: dict[str, object] = {}

    class _CapturingClient:
        def __init__(self, rows):
            self.rows = rows
            self.deleted: list[str] = []

        def list_sessions(self, limit=20, after=None, kind=None):
            return {"data": self.rows}

        def get_items(self, session_id, limit=100, order="asc", after=None):
            if session_id == "s-bad":
                raise trioctl.broker_http.BrokerHttpError(
                    "GET .../items failed with HTTP 500", status_code=500
                )
            return {"items": [{"id": "item-0", "role": "assistant"}]}

        def delete_session(self, session_id):
            self.deleted.append(session_id)
            return {"deleted": True}

    client = _CapturingClient(rows)
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url=None: client)
    real_prune = trioctl._prune_broker_sessions

    def _capture_prune(*args, **kwargs):
        captured["kwargs"] = kwargs
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(trioctl, "_prune_broker_sessions", _capture_prune)

    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "sessions",
            "prune",
            "--mailbox",
            str(mailbox),
            "--keep-unarchived",
        ]
    )

    assert args.func(args) == 0
    # The flag reached _prune_broker_sessions as keep_unarchived=True.
    assert captured["kwargs"].get("keep_unarchived") is True
    # The bad session was kept after retry; the good one was deleted.
    assert client.deleted == ["s-good"]
    out = capsys.readouterr().out
    assert "archived 1, deleted 1" in out


def test_command_loop_cli_keep_unarchived_flag_is_parsed():
    """The `loop` subcommand accepts `--keep-unarchived` and parses it to
    a truthy `keep_unarchived` attribute, so the flag threads through to
    `_run_post_loop_session_prune` (default-off)."""
    trioctl = load_trioctl()

    args = trioctl.parser().parse_args(
        ["omnigent", "loop", "--mailbox", "loop", "--keep-unarchived"]
    )

    assert args.keep_unarchived is True


def test_command_loop_cli_keep_unarchived_defaults_off():
    """Without `--keep-unarchived`, the loop subcommand leaves the flag
    falsy so the post-loop prune deletes partial archives after retry."""
    trioctl = load_trioctl()

    args = trioctl.parser().parse_args(["omnigent", "loop", "--mailbox", "loop"])

    assert args.keep_unarchived is False


def test_run_post_loop_session_prune_noop_when_no_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """No sessions created (or all filtered away) means no broker round
    trip and no summary line."""
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()

    trioctl._run_post_loop_session_prune(mailbox, None, [])

    assert capsys.readouterr().out == ""


def test_record_created_session_id_appends_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    ids_path = tmp_path / "run.ids"
    monkeypatch.setenv(trioctl.SESSION_IDS_ENV_VAR, str(ids_path))

    trioctl._record_created_session_id("s1")
    trioctl._record_created_session_id("s2")

    assert trioctl._read_session_ids_file(ids_path) == ["s1", "s2"]


def test_record_created_session_id_noop_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trioctl = load_trioctl()
    monkeypatch.delenv(trioctl.SESSION_IDS_ENV_VAR, raising=False)

    trioctl._record_created_session_id("s1")  # must not raise

    assert list(tmp_path.iterdir()) == []


def test_command_run_env_set_but_no_session_id_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`command_run` drives `cursor-agent` directly and never opens a
    broker session, so even with the env var set there is nothing to
    append -- the ids file is left untouched (absent)."""
    trioctl = load_trioctl()
    ids_path = tmp_path / "run.ids"
    monkeypatch.setenv(trioctl.SESSION_IDS_ENV_VAR, str(ids_path))
    monkeypatch.setattr(
        trioctl, "run_cursor_worker", lambda *a, **k: "OK"
    )
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do the thing")
    args = trioctl.parser().parse_args(
        [
            "omnigent",
            "run",
            "builder",
            "--config",
            str(trioctl.DEFAULT_CONFIG),
            "--prompt-file",
            str(prompt_file),
            "--workspace",
            str(tmp_path),
        ]
    )

    assert trioctl.command_run(args) == 0
    assert not ids_path.exists()


def test_command_sessions_prune_cli_wiring_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    trioctl = load_trioctl()
    mailbox = tmp_path / "mbx"
    mailbox.mkdir()
    rows = [
        _row("s1", "trioctl mbx iteration 1 lead"),
        _row("s2", "trioctl mbx iteration 2 evaluator", status="running"),
    ]
    client = _FakeSessionsClient(rows)
    monkeypatch.setattr(trioctl, "_session_client", lambda base_url=None: client)
    args = trioctl.parser().parse_args(
        ["omnigent", "sessions", "prune", "--mailbox", str(mailbox)]
    )

    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "archived 1, deleted 1, skipped running 1" in out
    assert client.deleted == ["s1"]


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


def test_doctor_reports_resolved_prompt_directory(
    tmp_path, monkeypatch, capsys
):
    """The doctor `prompts` check names the directory `_prompt_path`
    actually resolved -- here the checked-in repo copy, since the test
    process's cwd stays inside this repo."""
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
    monkeypatch.setattr(trioctl.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(trioctl, "omnigent_contract", lambda: "ok")
    monkeypatch.setattr(
        trioctl,
        "check_cursor_approval_mode",
        lambda: {"check": "cursor:approval-mode", "ok": True, "detail": "ok"},
    )
    monkeypatch.setattr(trioctl, "cursor_models", lambda timeout: models)
    monkeypatch.setattr(trioctl, "omnigent_registry_path", lambda: registry)
    args = trioctl.parser().parse_args(["omnigent", "doctor"])

    assert args.func(args) == 0
    output = capsys.readouterr().out
    expected_dir = (
        Path(trioctl.__file__).resolve().parent.parent
        / "omnigent" / "entrypoints" / "trio-omnigent" / "prompts"
    )
    assert f"PASS prompts: {expected_dir}" in output


def test_doctor_prompts_check_fails_when_nothing_resolves(
    tmp_path, monkeypatch, capsys
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TRIO_OMNIGENT_PROMPTS", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(SCRIPT, bin_dir / "trioctl")
    shutil.copy(SCRIPT.with_name("broker_http.py"), bin_dir / "broker_http.py")
    loader = importlib.machinery.SourceFileLoader(
        "trioctl_installed", str(bin_dir / "trioctl")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    trioctl = importlib.util.module_from_spec(spec)
    loader.exec_module(trioctl)

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
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
    monkeypatch.setattr(trioctl, "load_config", lambda path: profile())
    monkeypatch.setattr(trioctl.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(trioctl, "omnigent_contract", lambda: "ok")
    monkeypatch.setattr(
        trioctl,
        "check_cursor_approval_mode",
        lambda: {"check": "cursor:approval-mode", "ok": True, "detail": "ok"},
    )
    monkeypatch.setattr(trioctl, "omnigent_registry_path", lambda: registry)
    args = trioctl.parser().parse_args(["omnigent", "doctor"])

    assert args.func(args) == 1
    output = capsys.readouterr().out
    assert "FAIL prompts: missing Omnigent prompt: lead.md (tried:" in output
    assert str(fake_home / ".claude" / "skills" / "trio-omnigent" / "prompts") in output


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


def test_shipped_cursor_profile_uses_glm_workers_and_grok_judges():
    import tomllib

    trioctl = load_trioctl()
    config = tomllib.loads((SCRIPT.parent / "trioctl.example.toml").read_text())
    models = [model("glm-5.2-max"), model("gpt-5.6-luna-max"), model("cursor-grok-4.6-medium")]
    for role in ("builder", "scout", "docs", "lead", "evaluator"):
        resolved = trioctl.resolve_role(role, config, models=models)
        assert resolved["model"] == ("glm-5.2-max" if role in ("builder", "scout", "docs") else "cursor-grok-4.6-medium")
        assert resolved["reasoning_effort"] is None

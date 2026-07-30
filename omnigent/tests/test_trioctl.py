"""Unit tests for the dependency-free trioctl executable."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import textwrap
from pathlib import Path

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
            "lead": {"provider": "claude", "model": "opus", "effort": "high"},
            "evaluator": {
                "provider": "claude",
                "model": "opus",
                "effort": "high",
            },
            "builder": {
                "provider": "cursor",
                "model_family": "grok-4.5",
                "fallback_model": "cursor-grok-4.5-high",
                "effort": "high",
                "fast": False,
                **builder,
            },
            "scout": {
                "provider": "cursor",
                "model_family": "grok-4.5",
                "fallback_model": "cursor-grok-4.5-high",
                "effort": "high",
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


def test_claude_role_uses_configured_moving_alias():
    trioctl = load_trioctl()

    result = trioctl.resolve_role("lead", profile())

    assert result == {
        "role": "lead",
        "provider": "claude",
        "model": "opus",
        "reasoning_effort": "high",
        "source": "configured-alias",
    }


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

    assert result["model"] == "cursor-grok-4.5-high"
    assert result["source"] == "explicit-fallback"
    assert result["reasoning_effort"] is None
    assert result["model_effort"] == "high"


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
        model("cursor-grok-4.5-medium"),
        model("cursor-grok-4.5-high"),
        model("cursor-grok-4.5-high-fast"),
        model("composer-2.5"),
    ]

    result = trioctl.resolve_role("builder", profile(), models=models)

    assert result == {
        "role": "builder",
        "provider": "cursor",
        "model": "cursor-grok-4.5-high",
        "reasoning_effort": None,
        "model_effort": "high",
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
            print("cursor-grok-4.5-high - Cursor Grok 4.5")
            """
        )
    )
    fake.chmod(0o755)
    monkeypatch.setattr(trioctl.shutil, "which", lambda command: str(fake))

    assert trioctl.cursor_models(timeout=2) == [
        {"id": "auto", "model": "auto", "displayName": "Auto (default)"},
        {
            "id": "cursor-grok-4.5-high",
            "model": "cursor-grok-4.5-high",
            "displayName": "Cursor Grok 4.5",
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
        models=[model("cursor-grok-4.5-high")],
    )

    assert output == "WORKER_OK"
    assert seen["command"] == [
        "/bin/cursor-agent",
        "-p",
        "--output-format",
        "text",
        "--model",
        "cursor-grok-4.5-high",
        "--force",
        "--trust",
        "--approve-mcps",
        "--workspace",
        str(tmp_path.resolve()),
    ]
    assert "TASK FROM OPUS:\nImplement the bounded change." in seen["kwargs"]["input"]


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
        models=[model("cursor-grok-4.5-high")],
    )

    assert seen["command"][-2:] == ["--mode", "ask"]

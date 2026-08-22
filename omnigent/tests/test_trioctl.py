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

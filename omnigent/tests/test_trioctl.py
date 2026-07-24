"""Unit tests for the dependency-free trioctl executable."""

from __future__ import annotations

import importlib.machinery
import importlib.util
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
                "provider": "codex",
                "model_family": "luna",
                "fallback_model": "gpt-5.6-luna",
                "effort": "xhigh",
                **builder,
            },
            "scout": {
                "provider": "codex",
                "model_family": "luna",
                "fallback_model": "gpt-5.6-luna",
                "effort": "xhigh",
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

    result = trioctl.resolve_role("builder", profile(), models=models)

    assert result["model"] == "gpt-5.7-luna"
    assert result["source"] == "codex-model-list"


def test_codex_resolution_rejects_unsupported_effort():
    trioctl = load_trioctl()

    with pytest.raises(trioctl.TrioctlError, match="does not support effort"):
        trioctl.resolve_role(
            "builder",
            profile(),
            models=[model("gpt-5.7-luna", "low", "medium")],
        )


def test_codex_resolution_fails_loudly_without_matching_entitlement():
    trioctl = load_trioctl()

    with pytest.raises(trioctl.TrioctlError, match="no available Codex model"):
        trioctl.resolve_role(
            "builder",
            profile(),
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

    assert result["model"] == "gpt-5.6-luna"
    assert result["source"] == "explicit-fallback"


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

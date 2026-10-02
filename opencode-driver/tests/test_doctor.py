"""doctor.py — environment/config checks, using a fake `opencode` binary.
Uses tests/fakeoc.py's install_fake() when the runner builder's fixture has
landed; otherwise falls back to a small self-contained fake script so this
test module never depends on landing order (SPEC.md task instructions)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path
from typing import Callable

import pytest

from trio_opencode import config as config_mod
from trio_opencode import doctor

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_KEY_VALUE = "sk-fake-doctor-test-key-do-not-leak"

_FAKE_OPENCODE_SRC = '''\
#!/usr/bin/env python3
import os
import sys

def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("1.18.33")
        return 0
    if args and args[0] == "models":
        if os.environ.get("OPENCODE_API_KEY"):
            print("opencode-go/deepseek-v4.1-flash")
            print("opencode-go/glm-5.3-flash")
        return 0
    if args and args[0] == "run":
        print('{"type":"text","sessionID":"ses_fake","part":{"type":"text","text":"OK"}}')
        return 0
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _install_fake_opencode(tmp_path: Path) -> Path | dict[str, str]:
    """Returns either a bin dir to prepend to PATH (the self-contained
    fallback script) or a complete environment (``fakeoc.install_fake``'s
    own return shape) — never leave that distinction to string-formatting,
    or a broken PATH silently falls through to any real ``opencode`` on the
    host PATH (the one binary this whole test suite must never invoke)."""
    fakeoc_path = Path(__file__).resolve().parent / "fakeoc.py"
    if fakeoc_path.exists():
        spec = importlib.util.spec_from_file_location("fakeoc", fakeoc_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        if hasattr(module, "install_fake"):
            return module.install_fake(tmp_path)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "opencode"
    script.write_text(_FAKE_OPENCODE_SRC, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    installed = _install_fake_opencode(tmp_path)
    if isinstance(installed, dict):
        env = dict(installed)
    else:
        env = dict(os.environ)
        env["PATH"] = f"{installed}{os.pathsep}{env.get('PATH', '')}"
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "xdg-cache")
    env["HOME"] = str(tmp_path / "home")
    # Belt-and-braces: `opencode` must resolve inside tmp_path, never a real
    # install elsewhere on the host PATH (SPEC.md hard constraint) — set on
    # THIS process's own os.environ too (monkeypatch reverts it after the
    # test), since any bare `shutil.which("opencode")` (no explicit `path=`)
    # reads the real process env, not the `env` dict doctor.run() is given.
    monkeypatch.setenv("PATH", env["PATH"])
    found = shutil.which("opencode", path=env["PATH"])
    assert found is not None and str(tmp_path) in found, (
        f"fake_env resolved `opencode` to {found!r}, not a fake under {tmp_path}"
    )
    return env


def _cfg(tmp_path: Path, **overrides) -> config_mod.Config:
    d = config_mod._default_dict()
    key_file = overrides.pop("key_file", None)
    if key_file is None:
        key_file = tmp_path / "OpenCodeKey.txt"
        key_file.write_text(FAKE_KEY_VALUE + "\n", encoding="utf-8")
        key_file.chmod(0o600)
    kwargs = dict(
        opencode_bin=d["opencode_bin"],
        models=dict(d["models"]),
        variants=dict(d["variants"]),
        provider=config_mod.ProviderConfig(id=d["provider"]["id"], key_file=str(key_file), key_env=d["provider"]["key_env"]),
        timeouts=config_mod.TimeoutsConfig(**{k: d["timeouts"][k] for k in d["timeouts"]}),
        retries=config_mod.RetriesConfig(max_attempts=d["retries"]["max_attempts"], backoff_seconds=tuple(d["retries"]["backoff_seconds"])),
        max_iterations=d["max_iterations"],
        root_free=d["root_free"],
    )
    kwargs.update(overrides)
    return config_mod.Config(**kwargs)


def _collect(cfg: config_mod.Config, env: dict[str, str]) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = doctor.run(cfg, env=env, out=lines.append)
    return code, lines


def test_doctor_success_report_shape_and_exit_zero(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, fake_env)
    assert code == 0
    assert len(out) == 1
    report = json.loads(out[0])
    assert report["ok"] is True
    names = {c["name"] for c in report["checks"]}
    for expected in (
        "config", "model_tiers", "opencode_binary", "opencode_version", "key_file",
        "git_version", "python_version", "repo_files", "no_ask_permissions",
        "no_auto_flag", "opencode_models",
    ):
        assert expected in names, names
    for c in report["checks"]:
        assert c["ok"] is True, c


def test_doctor_never_prints_the_key(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, fake_env)
    joined = "\n".join(out)
    assert FAKE_KEY_VALUE not in joined


def test_doctor_reports_invalid_config(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path, models={
        "lead": "<placeholder>", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash", "repair": "opencode-go/glm-5.3-flash",
    })
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    assert report["ok"] is False
    config_check = next(c for c in report["checks"] if c["name"] == "config")
    assert config_check["ok"] is False
    assert "placeholder" in config_check["detail"]


def test_doctor_missing_opencode_binary(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path, opencode_bin="opencode-does-not-exist")
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    binary_check = next(c for c in report["checks"] if c["name"] == "opencode_binary")
    assert binary_check["ok"] is False
    version_check = next(c for c in report["checks"] if c["name"] == "opencode_version")
    assert version_check["ok"] is False
    models_check = next(c for c in report["checks"] if c["name"] == "opencode_models")
    assert models_check["ok"] is False


def test_doctor_missing_key_file(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path, key_file=tmp_path / "does-not-exist.txt")
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    key_check = next(c for c in report["checks"] if c["name"] == "key_file")
    assert key_check["ok"] is False


def test_doctor_empty_key_file(tmp_path: Path, fake_env: dict[str, str]):
    empty = tmp_path / "empty-key.txt"
    empty.write_text("", encoding="utf-8")
    cfg = _cfg(tmp_path, key_file=empty)
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    key_check = next(c for c in report["checks"] if c["name"] == "key_file")
    assert key_check["ok"] is False
    assert "empty" in key_check["detail"]


def test_doctor_world_readable_key_file_is_a_warning_not_a_failure(tmp_path: Path, fake_env: dict[str, str]):
    key_file = tmp_path / "loose-key.txt"
    key_file.write_text(FAKE_KEY_VALUE, encoding="utf-8")
    key_file.chmod(0o644)
    cfg = _cfg(tmp_path, key_file=key_file)
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    key_check = next(c for c in report["checks"] if c["name"] == "key_file")
    assert key_check["ok"] is True
    assert "WARNING" in key_check["detail"]


def test_doctor_missing_model_in_catalog_but_live_probe_ok_passes(
    tmp_path: Path, fake_env: dict[str, str],
):
    """A model absent from `opencode models`' own catalog (v2 only lists
    providers in OpenCode's own auth store, never ones supplied purely via
    OPENCODE_API_KEY) still passes the check once the fallback live probe
    (`opencode run -m <id> "Reply with exactly: OK"`) succeeds — bug 2 fix."""
    cfg = _cfg(tmp_path, models={
        "lead": "opencode-go/deepseek-v4.1-flash", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/env-key-only-model",
    })
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    models_check = next(c for c in report["checks"] if c["name"] == "opencode_models")
    assert models_check["ok"] is True, models_check
    assert "live probe" in models_check["detail"]
    assert "env-key-only-model" in models_check["detail"]
    assert code == 0
    assert FAKE_KEY_VALUE not in models_check["detail"]


def test_doctor_missing_model_in_catalog_fails(tmp_path: Path, fake_env: dict[str, str]):
    """A model absent from the catalog AND unreachable via the live-probe
    fallback (a genuinely bogus model id) still fails the check."""
    scenario = tmp_path / "scenario_unknown_model.py"
    scenario.write_text(textwrap.dedent("""\
        def handle(ctx):
            if "does-not-exist" in ctx.model:
                ctx.error("provider.no-route", f"Model unavailable: {ctx.model}")
            else:
                ctx.text("OK")
        """), encoding="utf-8")
    env = dict(fake_env)
    env["FAKE_OC_SCENARIO"] = str(scenario)
    cfg = _cfg(tmp_path, models={
        "lead": "opencode-go/deepseek-v4.1-flash", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/does-not-exist-in-catalog",
    })
    code, out = _collect(cfg, env)
    report = json.loads(out[0])
    assert code == 1
    models_check = next(c for c in report["checks"] if c["name"] == "opencode_models")
    assert models_check["ok"] is False
    assert "does-not-exist-in-catalog" in models_check["detail"]
    assert FAKE_KEY_VALUE not in models_check["detail"]


def test_doctor_mismatched_tiers_fails_model_tiers_check(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path, models={
        "lead": "opencode-go/deepseek-v4.1-flash", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/glm-5.3-flash", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash", "repair": "opencode-go/glm-5.3-flash",
    })
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    tiers_check = next(c for c in report["checks"] if c["name"] == "model_tiers")
    assert tiers_check["ok"] is False


def test_doctor_generates_config_under_xdg_state_home(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path)
    _collect(cfg, fake_env)
    expected_root = Path(fake_env["XDG_STATE_HOME"]) / "trio-agent-loop" / "opencode-doctor"
    assert expected_root.is_dir()
    assert any(expected_root.iterdir())


def test_doctor_repo_files_check_passes_in_this_checkout(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    repo_check = next(c for c in report["checks"] if c["name"] == "repo_files")
    assert repo_check["ok"] is True


def test_doctor_reports_version_and_cli_caps(tmp_path: Path, fake_env: dict[str, str]):
    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 0
    version_check = next(c for c in report["checks"] if c["name"] == "opencode_version")
    assert version_check["ok"] is True
    assert version_check["detail"] == "2.0.20"
    caps_check = next(c for c in report["checks"] if c["name"] == "cli_caps")
    assert caps_check["ok"] is True
    assert "style=v2" in caps_check["detail"]
    assert "version=2.0.20" in caps_check["detail"]


def test_doctor_v1_style_binary_reports_v1_caps_with_warning(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FAKE_OC_STYLE", "v1")
    installed = _install_fake_opencode(tmp_path)
    env = dict(installed) if isinstance(installed, dict) else dict(os.environ)
    env["FAKE_OC_STYLE"] = "v1"
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg-data")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg-config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "xdg-cache")
    env["HOME"] = str(tmp_path / "home")
    monkeypatch.setenv("PATH", env["PATH"])

    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, env)
    report = json.loads(out[0])
    assert code == 0, report
    version_check = next(c for c in report["checks"] if c["name"] == "opencode_version")
    assert version_check["ok"] is True
    assert "WARNING" in version_check["detail"]
    caps_check = next(c for c in report["checks"] if c["name"] == "cli_caps")
    assert caps_check["ok"] is True
    assert "style=v1" in caps_check["detail"]


def test_doctor_missing_session_flag_fails_cli_caps(tmp_path: Path, fake_env: dict[str, str]):
    fake_env = dict(fake_env)
    fake_env["FAKE_OC_HELP"] = (
        "Usage: opencode run [flags]\n\nFlags:\n"
        "  --standalone\n  --format <default|json>\n  --agent <name>\n"
        "  -m, --model <provider/model>\n"
    )
    cfg = _cfg(tmp_path)
    code, out = _collect(cfg, fake_env)
    report = json.loads(out[0])
    assert code == 1
    caps_check = next(c for c in report["checks"] if c["name"] == "cli_caps")
    assert caps_check["ok"] is False
    assert "unsupported opencode version" in caps_check["detail"]
    assert "session" in caps_check["detail"]

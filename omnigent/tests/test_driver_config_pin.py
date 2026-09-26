"""Worker dispatches with --mailbox use the loop driver's pinned profile.

Regression for live run 20260926T131520Z: the Lead dispatched builders
with a different TRIOCTL_CONFIG than the driver's, so builders ran a
low-effort model while the driver's profile said medium.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture()
def trioctl():
    return _load("trioctl_config_pin", ROOT / "omnigent" / "trioctl")


@pytest.fixture(scope="module")
def trio_loop():
    return _load("trio_loop_config_pin", ROOT / "metrics" / "trio_loop.py")


def _cfg(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("version = 1\n", encoding="utf-8")
    return path


def _run(trioctl, monkeypatch, tmp_path, extra):
    loaded: list[Path] = []
    monkeypatch.setattr(
        trioctl, "load_config", lambda path: loaded.append(Path(path)) or {}
    )
    monkeypatch.setattr(trioctl, "run_cursor_worker", lambda *a, **k: "OK")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do the thing", encoding="utf-8")
    args = trioctl.parser().parse_args(
        ["omnigent", "run", "builder", "--prompt-file", str(prompt),
         "--workspace", str(tmp_path), *extra]
    )
    assert trioctl.command_run(args) == 0
    assert len(loaded) == 1
    return loaded[0]


def _mailbox(tmp_path: Path, driver: dict | None) -> Path:
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    if driver is not None:
        (mailbox / ".driver.json").write_text(json.dumps(driver), encoding="utf-8")
    return mailbox


def test_runner_driver_meta_records_resolved_config(
    trioctl, trio_loop, tmp_path, monkeypatch
) -> None:
    x = _cfg(tmp_path, "x.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(x))
    runner = trioctl.OmnigentRunner(repo=tmp_path)
    assert runner.driver_meta == {"config_path": str(x.resolve())}
    mailbox = _mailbox(tmp_path, None)
    trio_loop._write_driver_state(mailbox, runner, 1, "lead-running")
    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert driver["config_path"] == str(x.resolve())
    assert driver["iteration"] == 1 and driver["phase"] == "lead-running"
    trio_loop._write_open_loop_sidecars(
        mailbox, runner, runner, 1, "lead", True, False, "t0"
    )
    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert driver["config_path"] == str(x.resolve())
    assert driver["open_loop"] is True
    session = json.loads((mailbox / ".session.json").read_text(encoding="utf-8"))
    assert "config_path" not in session


def test_injected_config_runner_and_fakes_add_no_key(
    trioctl, trio_loop, tmp_path
) -> None:
    runner = trioctl.OmnigentRunner(repo=tmp_path, config={"version": 1})
    assert runner.driver_meta == {}

    class Fake:
        session_ids: dict = {}

    mailbox = _mailbox(tmp_path, None)
    trio_loop._write_driver_state(mailbox, Fake(), 2, "evaluator-running")
    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert set(driver) == {"pid", "iteration", "phase", "session_ids"}


def test_driver_meta_never_overrides_core_keys(trio_loop, tmp_path) -> None:
    class Sneaky:
        session_ids: dict = {}
        driver_meta = {"phase": "bogus", "config_path": "/x.toml"}

    mailbox = _mailbox(tmp_path, None)
    trio_loop._write_driver_state(mailbox, Sneaky(), 3, "lead-done")
    driver = json.loads((mailbox / ".driver.json").read_text(encoding="utf-8"))
    assert driver["phase"] == "lead-done"
    assert driver["config_path"] == "/x.toml"


def test_run_uses_driver_config_over_env(trioctl, tmp_path, monkeypatch, capsys) -> None:
    x = _cfg(tmp_path, "x.toml")
    y = _cfg(tmp_path, "y.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(y))
    mailbox = _mailbox(tmp_path, {"pid": 1, "config_path": str(x)})
    used = _run(trioctl, monkeypatch, tmp_path, ["--mailbox", str(mailbox)])
    assert used == x
    assert f"trioctl: using driver config {x}" in capsys.readouterr().err


def test_run_relative_mailbox_resolves_from_cwd(
    trioctl, tmp_path, monkeypatch, capsys
) -> None:
    x = _cfg(tmp_path, "x.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(_cfg(tmp_path, "y.toml")))
    _mailbox(tmp_path, {"config_path": str(x)})
    monkeypatch.chdir(tmp_path)
    assert _run(trioctl, monkeypatch, tmp_path, ["--mailbox", "loop"]) == x


def test_run_same_config_prints_nothing(trioctl, tmp_path, monkeypatch, capsys) -> None:
    x = _cfg(tmp_path, "x.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(x))
    mailbox = _mailbox(tmp_path, {"config_path": str(x.resolve())})
    assert _run(trioctl, monkeypatch, tmp_path, ["--mailbox", str(mailbox)]) == x.resolve()
    assert "driver config" not in capsys.readouterr().err


def test_explicit_config_beats_driver_and_env(
    trioctl, tmp_path, monkeypatch, capsys
) -> None:
    x = _cfg(tmp_path, "x.toml")
    y = _cfg(tmp_path, "y.toml")
    z = _cfg(tmp_path, "z.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(y))
    mailbox = _mailbox(tmp_path, {"config_path": str(x)})
    used = _run(trioctl, monkeypatch, tmp_path,
                ["--config", str(z), "--mailbox", str(mailbox)])
    assert used == z
    assert "driver config" not in capsys.readouterr().err


@pytest.mark.parametrize("driver", [None, {"pid": 1}, {"config_path": ""}, "not json"])
def test_missing_key_or_file_falls_back_to_env(
    trioctl, tmp_path, monkeypatch, capsys, driver
) -> None:
    y = _cfg(tmp_path, "y.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(y))
    mailbox = tmp_path / "loop"
    mailbox.mkdir()
    if isinstance(driver, dict):
        (mailbox / ".driver.json").write_text(json.dumps(driver), encoding="utf-8")
    elif driver is not None:
        (mailbox / ".driver.json").write_text(driver, encoding="utf-8")
    assert _run(trioctl, monkeypatch, tmp_path, ["--mailbox", str(mailbox)]) == y
    assert capsys.readouterr().err == ""


def test_driver_config_pointing_to_missing_file_warns_and_uses_env(
    trioctl, tmp_path, monkeypatch, capsys
) -> None:
    y = _cfg(tmp_path, "y.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(y))
    gone = tmp_path / "gone.toml"
    mailbox = _mailbox(tmp_path, {"config_path": str(gone)})
    assert _run(trioctl, monkeypatch, tmp_path, ["--mailbox", str(mailbox)]) == y
    err = capsys.readouterr().err
    assert f"driver config {gone}" in err and "missing" in err
    assert "using driver config" not in err


def test_no_mailbox_uses_env(trioctl, tmp_path, monkeypatch) -> None:
    y = _cfg(tmp_path, "y.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(y))
    assert _run(trioctl, monkeypatch, tmp_path, []) == y


def test_isolated_dispatch_gets_driver_config(trioctl, tmp_path, monkeypatch) -> None:
    x = _cfg(tmp_path, "x.toml")
    monkeypatch.setenv("TRIOCTL_CONFIG", str(_cfg(tmp_path, "y.toml")))
    mailbox = _mailbox(tmp_path, {"config_path": str(x)})
    seen: dict = {}
    monkeypatch.setattr(trioctl, "load_config", lambda path: {"from": Path(path)})

    def fake_isolated(args, config, prompt):
        seen["config"] = config
        return 0

    monkeypatch.setattr(trioctl, "_command_run_isolated", fake_isolated)
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    args = trioctl.parser().parse_args(
        ["omnigent", "run", "builder", "--prompt-file", str(prompt),
         "--workspace", str(tmp_path), "--mailbox", str(mailbox),
         "--isolate", "--worker-slice", "s1"]
    )
    assert trioctl.command_run(args) == 0
    assert seen["config"] == {"from": x}

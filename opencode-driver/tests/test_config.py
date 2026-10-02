"""config.py — defaults, path precedence, deep-merge/unknown-key rejection,
type validation, and validate() error rules (SPEC.md "Config")."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode.config import Config, ConfigError, load_config, validate


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def test_defaults_match_spec(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TRIO_OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    cfg = load_config()

    assert cfg.opencode_bin == "opencode"
    assert cfg.models == {
        "lead": "opencode-go/deepseek-v4.1-flash",
        "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash",
        "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/glm-5.3-flash",
    }
    assert cfg.variants == {"lead": None, "evaluator": None, "builder": None, "scout": None, "repair": None}
    assert cfg.provider.id == "opencode-go"
    assert cfg.provider.key_file == str(Path(tmp_path / "home") / "Documents" / "OpenCodeKey.txt")
    assert cfg.provider.key_env == "OPENCODE_API_KEY"
    assert cfg.timeouts.turn_seconds == 3600
    assert cfg.timeouts.idle_seconds == 600
    assert cfg.timeouts.evaluator_turn_seconds == 5400
    assert cfg.retries.max_attempts == 3
    assert cfg.retries.backoff_seconds == (10.0, 30.0, 90.0)
    assert cfg.max_iterations == 4
    assert cfg.root_free is True
    assert cfg.source_path is None


def test_default_key_file_resolved_against_home(monkeypatch, tmp_path: Path):
    """SPEC.md: key_file default is resolved at load, as
    Path.home()/"Documents"/"OpenCodeKey.txt"."""
    fake_home = tmp_path / "someone"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TRIO_OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    cfg = load_config()
    assert cfg.provider.key_file == str(fake_home / "Documents" / "OpenCodeKey.txt")


def test_example_json_matches_defaults(tmp_path: Path, monkeypatch):
    """config.example.json (the shipped example) must load to the exact
    same Config as the built-in defaults, keeping the artifact honest."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    example_path = Path(__file__).resolve().parents[1] / "config.example.json"
    data = json.loads(example_path.read_text(encoding="utf-8"))
    # The example pins the real default key_file path; substitute this
    # test's HOME so the comparison is apples-to-apples.
    data["provider"]["key_file"] = str(Path(tmp_path / "home") / "Documents" / "OpenCodeKey.txt")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(str(config_path))
    assert cfg.models == {
        "lead": "opencode-go/deepseek-v4.1-flash",
        "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash",
        "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/glm-5.3-flash",
    }
    assert cfg.retries.backoff_seconds == (10.0, 30.0, 90.0)
    assert cfg.max_iterations == 4
    assert cfg.root_free is True


# --------------------------------------------------------------------------
# Path precedence
# --------------------------------------------------------------------------

def test_explicit_path_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env_path = tmp_path / "env-config.json"
    env_path.write_text(json.dumps({"max_iterations": 7}), encoding="utf-8")
    monkeypatch.setenv("TRIO_OPENCODE_CONFIG", str(env_path))

    arg_path = tmp_path / "arg-config.json"
    arg_path.write_text(json.dumps({"max_iterations": 9}), encoding="utf-8")

    cfg = load_config(str(arg_path))
    assert cfg.max_iterations == 9


def test_env_wins_over_xdg_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    default_path = xdg / "trio-opencode" / "config.json"
    default_path.parent.mkdir(parents=True)
    default_path.write_text(json.dumps({"max_iterations": 2}), encoding="utf-8")

    env_path = tmp_path / "env-config.json"
    env_path.write_text(json.dumps({"max_iterations": 5}), encoding="utf-8")
    monkeypatch.setenv("TRIO_OPENCODE_CONFIG", str(env_path))

    cfg = load_config()
    assert cfg.max_iterations == 5


def test_xdg_default_path_used_when_present(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRIO_OPENCODE_CONFIG", raising=False)
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    default_path = xdg / "trio-opencode" / "config.json"
    default_path.parent.mkdir(parents=True)
    default_path.write_text(json.dumps({"max_iterations": 11}), encoding="utf-8")

    cfg = load_config()
    assert cfg.max_iterations == 11


def test_missing_xdg_default_falls_back_silently(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRIO_OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "does-not-exist"))

    cfg = load_config()
    assert cfg.max_iterations == 4


def test_explicit_missing_path_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.json"))


# --------------------------------------------------------------------------
# Deep merge / unknown keys / types
# --------------------------------------------------------------------------

def test_deep_merge_overrides_nested_field_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"models": {"builder": "opencode-go/other-model"}}), encoding="utf-8")

    cfg = load_config(str(path))
    assert cfg.models["builder"] == "opencode-go/other-model"
    # everything else in "models" is untouched
    assert cfg.models["lead"] == "opencode-go/deepseek-v4.1-flash"


def test_unknown_top_level_key_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"totally_unknown": 1}), encoding="utf-8")
    with pytest.raises(ConfigError, match="totally_unknown"):
        load_config(str(path))


def test_unknown_nested_key_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": {"bogus": "x"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"provider\.bogus"):
        load_config(str(path))


def test_unknown_keys_all_listed_together(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bogus_a": 1, "bogus_b": 2}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(path))
    assert "bogus_a" in str(exc_info.value)
    assert "bogus_b" in str(exc_info.value)


def test_wrong_type_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"max_iterations": "four"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="max_iterations"):
        load_config(str(path))


def test_bool_rejected_for_numeric_field(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timeouts": {"idle_seconds": True}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="idle_seconds"):
        load_config(str(path))


def test_non_object_config_file_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_invalid_json_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


# --------------------------------------------------------------------------
# The key never lives in the config file
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field_name", ["key", "api_key", "apiKey"])
def test_key_field_rejected(tmp_path: Path, monkeypatch, field_name: str):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": {field_name: "sk-super-secret"}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(str(path))
    message = str(exc_info.value)
    assert "sk-super-secret" not in message
    assert "key_file" in message


def test_key_field_at_top_level_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"key": "sk-super-secret"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------

def test_validate_passes_on_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("TRIO_OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    cfg = load_config()
    assert validate(cfg) == []


def test_validate_rejects_bad_model_id_form():
    cfg = _base_cfg(models={
        "lead": "not-a-provider-model", "evaluator": "not-a-provider-model",
        "acceptance": "not-a-provider-model", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash", "repair": "opencode-go/glm-5.3-flash",
    })
    errors = validate(cfg)
    assert any("provider/model" in e for e in errors)


def test_validate_rejects_placeholder():
    cfg = _base_cfg(models={
        "lead": "<fill-me-in>", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/deepseek-v4.1-flash", "builder": "opencode-go/glm-5.3-flash",
        "scout": "opencode-go/glm-5.3-flash", "repair": "opencode-go/glm-5.3-flash",
    })
    errors = validate(cfg)
    assert any("placeholder" in e for e in errors)


def test_validate_rejects_mismatched_tiers():
    cfg = _base_cfg(models={
        "lead": "opencode-go/deepseek-v4.1-flash", "evaluator": "opencode-go/deepseek-v4.1-flash",
        "acceptance": "opencode-go/glm-5.3-flash",  # cheaper than lead/evaluator: not allowed
        "builder": "opencode-go/glm-5.3-flash", "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/glm-5.3-flash",
    })
    errors = validate(cfg)
    assert any("acceptance" in e and "identical" in e for e in errors)


def test_validate_rejects_low_idle_timeout(monkeypatch):
    # conftest.py sets TRIO_OPENCODE_TEST_TIMEOUTS=1 globally so the rest of
    # the suite's tiny-timeout Configs still validate; this test is
    # specifically about the production floor, so it must run without that
    # escape hatch.
    monkeypatch.delenv("TRIO_OPENCODE_TEST_TIMEOUTS", raising=False)
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=3600, idle_seconds=60, evaluator_turn_seconds=5400))
    errors = validate(cfg)
    assert any("idle_seconds" in e for e in errors)


def test_validate_accepts_idle_timeout_at_floor():
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=3600, idle_seconds=180, evaluator_turn_seconds=5400))
    assert validate(cfg) == []


def test_validate_rejects_negative_timeouts():
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=-1, idle_seconds=600, evaluator_turn_seconds=5400))
    errors = validate(cfg)
    assert any("turn_seconds" in e for e in errors)
    cfg2 = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=3600, idle_seconds=600, evaluator_turn_seconds=-1))
    errors2 = validate(cfg2)
    assert any("evaluator_turn_seconds" in e for e in errors2)


# ----------------------------------------------------------------------
# No wall-clock limit (container / no-time-limit mode): 0 or null disables
# turn_seconds / evaluator_turn_seconds.
# ----------------------------------------------------------------------

def test_validate_accepts_zero_turn_seconds_as_disabled():
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=0, idle_seconds=600, evaluator_turn_seconds=5400))
    assert validate(cfg) == []


def test_validate_accepts_zero_evaluator_turn_seconds_as_disabled():
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=3600, idle_seconds=600, evaluator_turn_seconds=0))
    assert validate(cfg) == []


def test_load_config_null_turn_seconds_normalizes_to_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timeouts": {"turn_seconds": None, "evaluator_turn_seconds": None}}),
                    encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.timeouts.turn_seconds == 0.0
    assert cfg.timeouts.evaluator_turn_seconds == 0.0
    assert validate(cfg) == []


def test_load_config_zero_turn_seconds(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timeouts": {"turn_seconds": 0}}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.timeouts.turn_seconds == 0.0
    assert cfg.turn_timeout_for("lead") == 0.0


def test_timeouts_idle_seconds_null_is_rejected(tmp_path: Path, monkeypatch):
    """idle_seconds has no "disabled" sentinel: null is a type error."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timeouts": {"idle_seconds": None}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="idle_seconds"):
        load_config(str(path))


# ----------------------------------------------------------------------
# container_mode / retries.idle_retry_unlimited
# ----------------------------------------------------------------------

def test_container_mode_default_false():
    cfg = _base_cfg()
    assert cfg.container_mode is False
    assert cfg.retries.idle_retry_unlimited is False


def test_load_config_container_mode_and_idle_retry_unlimited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "container_mode": True,
        "retries": {"idle_retry_unlimited": True},
    }), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.container_mode is True
    assert cfg.retries.idle_retry_unlimited is True
    assert validate(cfg) == []


def test_container_mode_must_be_bool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"container_mode": "yes"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="container_mode"):
        load_config(str(path))


def test_idle_retry_unlimited_must_be_bool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"retries": {"idle_retry_unlimited": "yes"}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="idle_retry_unlimited"):
        load_config(str(path))


# ----------------------------------------------------------------------
# variants.acceptance
# ----------------------------------------------------------------------

def test_variants_acceptance_not_present_by_default():
    cfg = _base_cfg()
    assert "acceptance" not in cfg.variants


def test_load_config_variants_acceptance_matching_lead(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"variants": {"lead": "v2", "acceptance": "v2"}}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.variants["acceptance"] == "v2"
    assert validate(cfg) == []


def test_validate_rejects_variants_acceptance_mismatched_with_lead():
    cfg = _base_cfg(variants={"lead": "v2", "evaluator": None, "builder": None,
                              "scout": None, "repair": None, "acceptance": "v1"})
    errors = validate(cfg)
    assert any("variants.acceptance" in e for e in errors)


def test_validate_rejects_zero_max_attempts():
    cfg = _base_cfg(retries=config_mod.RetriesConfig(max_attempts=0, backoff_seconds=(10.0,)))
    errors = validate(cfg)
    assert any("max_attempts" in e for e in errors)


def _base_cfg(**overrides) -> Config:
    defaults = config_mod._default_dict()
    kwargs = dict(
        opencode_bin=defaults["opencode_bin"],
        models=dict(defaults["models"]),
        variants=dict(defaults["variants"]),
        provider=config_mod.ProviderConfig(**defaults["provider"]),
        timeouts=config_mod.TimeoutsConfig(
            turn_seconds=defaults["timeouts"]["turn_seconds"],
            idle_seconds=defaults["timeouts"]["idle_seconds"],
            evaluator_turn_seconds=defaults["timeouts"]["evaluator_turn_seconds"],
        ),
        retries=config_mod.RetriesConfig(
            max_attempts=defaults["retries"]["max_attempts"],
            backoff_seconds=tuple(defaults["retries"]["backoff_seconds"]),
        ),
        max_iterations=defaults["max_iterations"],
        root_free=defaults["root_free"],
    )
    kwargs.update(overrides)
    return Config(**kwargs)


# --------------------------------------------------------------------------
# Helpers on Config
# --------------------------------------------------------------------------

def test_model_for_and_variant_for_and_turn_timeout_for():
    cfg = _base_cfg(variants={"lead": "extra-thinking", "evaluator": None, "builder": None, "scout": None, "repair": None})
    assert cfg.model_for("builder") == "opencode-go/glm-5.3-flash"
    assert cfg.variant_for("lead") == "extra-thinking"
    assert cfg.variant_for("builder") is None
    assert cfg.turn_timeout_for("evaluator") == cfg.timeouts.evaluator_turn_seconds
    assert cfg.turn_timeout_for("lead") == cfg.timeouts.turn_seconds


def test_model_for_unknown_role_raises():
    cfg = _base_cfg()
    with pytest.raises(KeyError):
        cfg.model_for("nonexistent-role")


def test_placeholders_lists_offending_roles():
    cfg = _base_cfg(models={
        "lead": "<x>", "evaluator": "<x>", "acceptance": "<x>",
        "builder": "opencode-go/glm-5.3-flash", "scout": "opencode-go/glm-5.3-flash",
        "repair": "opencode-go/glm-5.3-flash",
    })
    assert set(cfg.placeholders()) == {"lead", "evaluator", "acceptance"}


def test_trio_opencode_test_timeouts_env_lifts_only_the_idle_floor(monkeypatch):
    monkeypatch.setenv("TRIO_OPENCODE_TEST_TIMEOUTS", "1")
    cfg = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=3600, idle_seconds=5, evaluator_turn_seconds=5400))
    errors = validate(cfg)
    assert not any("idle_seconds" in e for e in errors)
    # every other rule still applies even with the env var set.
    bad = _base_cfg(timeouts=config_mod.TimeoutsConfig(turn_seconds=-1, idle_seconds=5, evaluator_turn_seconds=5400))
    errors2 = validate(bad)
    assert any("turn_seconds" in e for e in errors2)

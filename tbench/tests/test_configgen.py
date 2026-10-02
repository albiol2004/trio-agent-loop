import pytest

import configgen


def test_default_config_matches_spec_container_mode_knobs():
    cfg = configgen.build_driver_config()
    assert cfg["container_mode"] is True
    assert cfg["root_free"] is False
    assert cfg["max_iterations"] == 8
    assert cfg["acceptance"] is False
    assert cfg["timeouts"] == {
        "turn_seconds": 0,
        "idle_seconds": 1800,
        "evaluator_turn_seconds": 0,
    }
    assert cfg["retries"] == {
        "max_attempts": 6,
        "backoff_seconds": [10, 30, 90, 180],
        "idle_retry_unlimited": True,
    }


def test_default_models_and_variants_split_by_tier():
    cfg = configgen.build_driver_config()
    for role in ("lead", "evaluator", "acceptance"):
        assert cfg["models"][role] == "opencode-go/deepseek-v4.1-flash"
        assert cfg["variants"][role] == "max"
    for role in ("builder", "scout", "repair"):
        assert cfg["models"][role] == "opencode-go/glm-5.3-flash"
        assert cfg["variants"][role] == "max"


def test_provider_block_carries_only_the_key_path_never_the_key():
    cfg = configgen.build_driver_config(key_file="/run/trio/opencode.key")
    assert cfg["provider"]["key_file"] == "/run/trio/opencode.key"
    assert cfg["provider"]["key_env"] == "OPENCODE_API_KEY"
    assert cfg["provider"]["id"] == "opencode-go"
    # build_driver_config() itself already asserts this; re-check explicitly
    # here too so a future refactor that bypasses the internal call still
    # fails this test.
    configgen.assert_no_key_fields(cfg)


def test_overrides_are_applied():
    cfg = configgen.build_driver_config(
        models={"lead": "x/y"},
        variants={"lead": "fast"},
        max_iterations=3,
        root_free=True,
        container_mode=False,
        turn_seconds=60,
        idle_seconds=200,
        evaluator_turn_seconds=90,
        max_attempts=2,
        backoff_seconds=[1, 2],
        idle_retry_unlimited=False,
        acceptance=True,
    )
    assert cfg["acceptance"] is True
    assert cfg["models"] == {"lead": "x/y"}
    assert cfg["variants"] == {"lead": "fast"}
    assert cfg["max_iterations"] == 3
    assert cfg["root_free"] is True
    assert cfg["container_mode"] is False
    assert cfg["timeouts"]["turn_seconds"] == 60
    assert cfg["timeouts"]["idle_seconds"] == 200
    assert cfg["timeouts"]["evaluator_turn_seconds"] == 90
    assert cfg["retries"] == {
        "max_attempts": 2,
        "backoff_seconds": [1, 2],
        "idle_retry_unlimited": False,
    }


def test_assert_no_key_fields_rejects_forbidden_keys():
    with pytest.raises(ValueError):
        configgen.assert_no_key_fields({"provider": {"key": "secret"}})
    with pytest.raises(ValueError):
        configgen.assert_no_key_fields({"a": [{"api_key": "x"}]})
    configgen.assert_no_key_fields({"provider": {"key_file": "/run/trio/opencode.key"}})


def test_build_driver_config_is_json_serializable():
    import json

    json.dumps(configgen.build_driver_config())

"""Pure JSON generation for the trio-opencode driver config written into
each task container at ``/opt/trio/config.json``.

Deliberately decoupled from ``trio_opencode.config`` accepting every field:
the container-mode knobs (``container_mode``, ``retries.idle_retry_unlimited``,
nullable/zero ``timeouts.turn_seconds``/``evaluator_turn_seconds``,
``variants.acceptance``) are being added by a concurrently-developed slice
of ``opencode-driver/trio_opencode/config.py``. This module only emits the
JSON; if a field is rejected by an older ``config.py``, that is the other
slice's integration to land, not something this generator should special-
case around.
"""
from __future__ import annotations

from typing import Any

DEFAULT_MODELS: dict[str, str] = {
    "lead": "opencode-go/deepseek-v4.1-flash",
    "evaluator": "opencode-go/deepseek-v4.1-flash",
    "acceptance": "opencode-go/deepseek-v4.1-flash",
    "builder": "opencode-go/glm-5.3-flash",
    "scout": "opencode-go/glm-5.3-flash",
    "repair": "opencode-go/glm-5.3-flash",
}

#: "max" for every role, including acceptance (which must equal lead's
#: variant per ``config.validate()``).
DEFAULT_VARIANTS: dict[str, str] = {
    "lead": "max",
    "evaluator": "max",
    "acceptance": "max",
    "builder": "max",
    "scout": "max",
    "repair": "max",
}

DEFAULT_KEY_PATH_IN_CONTAINER = "/run/trio/opencode.key"
DEFAULT_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_PROVIDER_ID = "opencode-go"
DEFAULT_OPENCODE_BIN = "/opt/trio/bin/opencode"

#: Keys whose presence anywhere in a generated config would be a hard load
#: error in ``trio_opencode.config`` (and a real secret leak if ever written
#: for real) -- asserted against in tests, never populated here.
_FORBIDDEN_KEYS = ("key", "api_key", "apiKey")


def build_driver_config(
    *,
    opencode_bin: str = DEFAULT_OPENCODE_BIN,
    models: dict[str, str] | None = None,
    variants: dict[str, str] | None = None,
    provider_id: str = DEFAULT_PROVIDER_ID,
    key_file: str = DEFAULT_KEY_PATH_IN_CONTAINER,
    key_env: str = DEFAULT_KEY_ENV,
    turn_seconds: float = 0,
    idle_seconds: float = 1800,
    evaluator_turn_seconds: float = 0,
    max_attempts: int = 6,
    backoff_seconds: list[float] | None = None,
    idle_retry_unlimited: bool = True,
    max_iterations: int = 8,
    root_free: bool = False,
    container_mode: bool = True,
    acceptance: bool = False,
) -> dict[str, Any]:
    """Build the trio-opencode ``config.json`` dict for a Terminal-Bench
    container run: no wall-clock turn limits by default (``turn_seconds`` /
    ``evaluator_turn_seconds`` = 0, the "disabled" sentinel), a long idle
    watchdog, unlimited idle retries, in-place (non-root-free) mode, and
    ``container_mode`` on so the generated OpenCode permissions treat the
    whole container as the product. Never embeds the API key itself -- only
    its in-container path (``provider.key_file``); the key bytes reach the
    container exclusively via ``environment.upload_file`` in
    ``trio_tbench_agent.py``, never through this function or any Python
    string this process holds.
    """
    config = {
        "opencode_bin": opencode_bin,
        "models": dict(models) if models is not None else dict(DEFAULT_MODELS),
        "variants": dict(variants) if variants is not None else dict(DEFAULT_VARIANTS),
        "provider": {
            "id": provider_id,
            "key_file": key_file,
            "key_env": key_env,
        },
        "timeouts": {
            "turn_seconds": turn_seconds,
            "idle_seconds": idle_seconds,
            "evaluator_turn_seconds": evaluator_turn_seconds,
        },
        "retries": {
            "max_attempts": max_attempts,
            "backoff_seconds": (
                list(backoff_seconds) if backoff_seconds is not None else [10, 30, 90, 180]
            ),
            "idle_retry_unlimited": idle_retry_unlimited,
        },
        "max_iterations": max_iterations,
        "root_free": root_free,
        "container_mode": container_mode,
        # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): off by default,
        # matching trio_opencode.config's own default.
        "acceptance": acceptance,
    }
    assert_no_key_fields(config)
    return config


def assert_no_key_fields(config: dict[str, Any]) -> None:
    """Raise ``ValueError`` if a forbidden secret-shaped key appears
    anywhere in *config* (mirrors ``trio_opencode.config``'s own hard load
    error for a literal key field, as a defensive double-check at the point
    this agent generates the file)."""

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _FORBIDDEN_KEYS:
                    raise ValueError(
                        f"generated config contains a forbidden key field: {key!r}"
                    )
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(config)

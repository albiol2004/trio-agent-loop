"""Configuration for trio-opencode: defaults, JSON overlay, and validation.

Path precedence for :func:`load_config`: an explicit ``path`` argument, then
the ``TRIO_OPENCODE_CONFIG`` environment variable, then
``${XDG_CONFIG_HOME:-~/.config}/trio-opencode/config.json``, else the
built-in defaults (SPEC.md "Config").

The config never carries the API key itself: only ``provider.key_file`` (a
path) and ``provider.key_env`` (the child-env variable name the runner sets
at spawn time). A ``key``/``api_key`` field anywhere in the user JSON is a
hard validation/load error.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Model tiers that MUST be identical (same provider/model id) so the
#: acceptance author is never cheaper than the lead/evaluator tier.
_SAME_TIER_ROLES = ("lead", "evaluator", "acceptance")

#: provider/model id shape, e.g. "opencode-go/deepseek-v4.1-flash".
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: A leftover placeholder like "<your-model-here>".
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")

_ROLES = ("lead", "evaluator", "acceptance", "builder", "scout", "repair")
_VARIANT_ROLES = ("lead", "evaluator", "builder", "scout", "repair")

#: Fields that would leak the key into the config file itself.
_FORBIDDEN_KEY_FIELDS = ("key", "api_key", "apiKey")


def _default_key_file() -> Path:
    return Path.home() / "Documents" / "OpenCodeKey.txt"


def _default_dict() -> dict[str, Any]:
    """The built-in defaults, as plain JSON-shaped data (SPEC.md "Config").
    Re-built each call so callers never mutate a shared default.
    """
    return {
        "opencode_bin": "opencode",
        "models": {
            "lead": "opencode-go/deepseek-v4.1-flash",
            "evaluator": "opencode-go/deepseek-v4.1-flash",
            "acceptance": "opencode-go/deepseek-v4.1-flash",
            "builder": "opencode-go/glm-5.3-flash",
            "scout": "opencode-go/glm-5.3-flash",
            "repair": "opencode-go/glm-5.3-flash",
        },
        "variants": {
            "lead": None,
            "evaluator": None,
            "builder": None,
            "scout": None,
            "repair": None,
        },
        "provider": {
            "id": "opencode-go",
            "key_file": str(_default_key_file()),
            "key_env": "OPENCODE_API_KEY",
        },
        "timeouts": {
            "turn_seconds": 3600,
            "idle_seconds": 600,
            "evaluator_turn_seconds": 5400,
        },
        "retries": {
            "max_attempts": 3,
            "backoff_seconds": [10, 30, 90],
            "idle_retry_unlimited": False,
        },
        "max_iterations": 4,
        "root_free": True,
        "container_mode": False,
        # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): off by default,
        # matching the installed release and native/'s args.acceptance.
        "acceptance": False,
        # Open-loop settings (D12, trio-opencode open-loop runner): mirror
        # trioctl `omnigent loop`'s defaults. Lockstep mailboxes accept these
        # keys too (no-ops there; driver.run()/cli.py print a one-line notice
        # instead of applying them).
        "isolate_workers": True,
        "slice_eval_concurrency": 4,
        "kill_check": True,
    }


#: Config keys that are valid in the user's overlay but deliberately absent
#: from ``_default_dict()`` (so they stay out of the default ``Config``'s
#: dict fields -- e.g. ``cfg.variants`` keeps exactly its historical 5-role
#: shape when nobody sets this). ``_deep_merge_collect`` treats a dotted
#: path listed here as known even with no matching ``base`` key, and merges
#: the overlay value straight in rather than raising "unknown config key".
_OPTIONAL_UNKNOWN_KEYS = frozenset({"variants.acceptance"})


@dataclass(frozen=True)
class ProviderConfig:
    id: str = "opencode-go"
    key_file: str = field(default_factory=lambda: str(_default_key_file()))
    key_env: str = "OPENCODE_API_KEY"


@dataclass(frozen=True)
class TimeoutsConfig:
    """``turn_seconds``/``evaluator_turn_seconds`` of ``0.0`` is the
    canonical "disabled" sentinel (no wall-clock limit on that role's turn);
    a config file's ``0`` or ``null`` both normalize to it (see
    :func:`_to_config`). ``idle_seconds`` has no such sentinel -- the idle
    (no-stdout) watchdog always applies."""
    turn_seconds: float = 3600.0
    idle_seconds: float = 600.0
    evaluator_turn_seconds: float = 5400.0


@dataclass(frozen=True)
class RetriesConfig:
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (10.0, 30.0, 90.0)
    #: When true, an ``idle_timeout`` failure is retried by the runner
    #: WITHOUT counting against ``max_attempts`` (unbounded) -- a hung
    #: connection never turns into a run-ending error on its own; see
    #: SPEC.md / README.md "Container / no-time-limit mode".
    idle_retry_unlimited: bool = False


@dataclass(frozen=True)
class Config:
    opencode_bin: str = "opencode"
    models: dict[str, str] = field(default_factory=dict)
    variants: dict[str, str | None] = field(default_factory=dict)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    retries: RetriesConfig = field(default_factory=RetriesConfig)
    max_iterations: int = 4
    root_free: bool = True
    #: Terminal-Bench / "whole container is the product" mode: relaxes
    #: ``external_directory`` to "allow" for every role in ocgen.py's
    #: generated permissions and swaps the bash deny-list for the
    #: container-safe one (see ocgen.py and README.md "Container /
    #: no-time-limit mode"). Never changes a role's edit/task permission.
    container_mode: bool = False
    #: r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): off by default.
    #: CLI ``--acceptance``/``--no-acceptance`` and the ``TRIO_ACCEPTANCE``
    #: env var override this at ``start``/``resume`` time (cli.py); this
    #: field is only the config-file layer of that precedence.
    acceptance: bool = False
    #: Open-loop settings (D12): config-file layer only -- ``--isolate-
    #: workers``/``--no-isolate-workers``, ``--slice-eval-concurrency`` and
    #: ``--no-kill-check`` (cli.py) override these at ``start``/``resume``
    #: time via ``trio_opencode.openloop.resolve_settings``.
    isolate_workers: bool = True
    slice_eval_concurrency: int = 4
    kill_check: bool = True
    #: Path this config was loaded from, or None for pure defaults.
    source_path: str | None = None

    def placeholders(self) -> list[str]:
        """Model ids (role names) still containing a ``<...>`` placeholder."""
        return [role for role, mid in self.models.items() if _PLACEHOLDER_RE.search(str(mid))]

    def model_for(self, role: str) -> str:
        try:
            return self.models[role]
        except KeyError as exc:
            raise KeyError(f"no model configured for role {role!r}") from exc

    def variant_for(self, role: str) -> str | None:
        return self.variants.get(role)

    def turn_timeout_for(self, role: str) -> float:
        if role == "evaluator":
            return self.timeouts.evaluator_turn_seconds
        return self.timeouts.turn_seconds


def _deep_merge_collect(base: dict[str, Any], overlay: dict[str, Any], path: str) -> tuple[dict[str, Any], list[str]]:
    """Deep-merge ``overlay`` onto ``base`` (overlay wins), returning the
    merged dict plus every ``overlay`` key path with no matching ``base``
    key (unknown keys are collected, not raised, so nested unknowns can all
    be reported together)."""
    unknown: list[str] = []
    merged = dict(base)
    for key, value in overlay.items():
        key_path = f"{path}.{key}" if path else key
        if key not in base:
            if key_path in _OPTIONAL_UNKNOWN_KEYS:
                merged[key] = value
                continue
            unknown.append(key_path)
            continue
        base_value = base[key]
        if isinstance(base_value, dict) and isinstance(value, dict):
            merged[key], sub_unknown = _deep_merge_collect(base_value, value, key_path)
            unknown.extend(sub_unknown)
        else:
            merged[key] = value
    return merged, unknown


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` onto ``base``. Raises ``ConfigError`` listing
    every unknown key in ``overlay`` (a key with no counterpart in ``base``
    at the same position)."""
    merged, unknown = _deep_merge_collect(base, overlay, "")
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(unknown))}")
    return merged


class ConfigError(ValueError):
    """Raised for a malformed config file: unknown keys, wrong types, or a
    forbidden field (a raw API key)."""


def _check_forbidden_key_fields(data: dict[str, Any], *, path: str = "") -> None:
    for key, value in data.items():
        key_path = f"{path}.{key}" if path else key
        if key in _FORBIDDEN_KEY_FIELDS:
            raise ConfigError(
                f"config field {key_path!r} is not allowed: never put the API key in "
                "the config file; set provider.key_file to a path instead"
            )
        if isinstance(value, dict):
            _check_forbidden_key_fields(value, path=key_path)


def _expect_type(value: Any, types: type | tuple[type, ...], path: str) -> None:
    """Type-check ``value`` against ``types``. ``bool`` is a Python ``int``
    subclass, so it is rejected wherever ``int``/``float`` is expected unless
    ``bool`` is itself in ``types``.
    """
    numeric = types if isinstance(types, tuple) else (types,)
    if isinstance(value, bool) and bool not in numeric and (int in numeric or float in numeric):
        raise ConfigError(f"config field {path!r} must be a number, got bool")
    if not isinstance(value, types):
        raise ConfigError(f"config field {path!r} must be {types}, got {type(value).__name__}")


def _validate_types(data: dict[str, Any]) -> None:
    if "opencode_bin" in data:
        _expect_type(data["opencode_bin"], str, "opencode_bin")
    if "models" in data:
        _expect_type(data["models"], dict, "models")
        for role, mid in data["models"].items():
            _expect_type(mid, str, f"models.{role}")
    if "variants" in data:
        _expect_type(data["variants"], dict, "variants")
        for role, v in data["variants"].items():
            if v is not None:
                _expect_type(v, str, f"variants.{role}")
    if "provider" in data:
        _expect_type(data["provider"], dict, "provider")
        provider = data["provider"]
        for pk in ("id", "key_file", "key_env"):
            if pk in provider:
                _expect_type(provider[pk], str, f"provider.{pk}")
    #: These two (and only these two) accept ``null`` -- "no wall-clock
    #: limit" -- in addition to a number; ``idle_seconds`` has no such
    #: sentinel and must always be numeric.
    _nullable_timeouts = ("turn_seconds", "evaluator_turn_seconds")
    if "timeouts" in data:
        _expect_type(data["timeouts"], dict, "timeouts")
        for tk in ("turn_seconds", "idle_seconds", "evaluator_turn_seconds"):
            if tk in data["timeouts"]:
                val = data["timeouts"][tk]
                if val is None and tk in _nullable_timeouts:
                    continue
                _expect_type(val, (int, float), f"timeouts.{tk}")
    if "retries" in data:
        _expect_type(data["retries"], dict, "retries")
        retries = data["retries"]
        if "max_attempts" in retries:
            _expect_type(retries["max_attempts"], int, "retries.max_attempts")
        if "backoff_seconds" in retries:
            _expect_type(retries["backoff_seconds"], list, "retries.backoff_seconds")
            for i, b in enumerate(retries["backoff_seconds"]):
                _expect_type(b, (int, float), f"retries.backoff_seconds[{i}]")
        if "idle_retry_unlimited" in retries:
            _expect_type(retries["idle_retry_unlimited"], bool, "retries.idle_retry_unlimited")
    if "max_iterations" in data:
        _expect_type(data["max_iterations"], int, "max_iterations")
    if "root_free" in data:
        _expect_type(data["root_free"], bool, "root_free")
    if "container_mode" in data:
        _expect_type(data["container_mode"], bool, "container_mode")
    if "acceptance" in data:
        _expect_type(data["acceptance"], bool, "acceptance")
    if "isolate_workers" in data:
        _expect_type(data["isolate_workers"], bool, "isolate_workers")
    if "slice_eval_concurrency" in data:
        _expect_type(data["slice_eval_concurrency"], int, "slice_eval_concurrency")
    if "kill_check" in data:
        _expect_type(data["kill_check"], bool, "kill_check")


def _config_path(path: str | None) -> Path | None:
    if path is not None:
        return Path(path)
    env_path = os.environ.get("TRIO_OPENCODE_CONFIG")
    if env_path:
        return Path(env_path)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidate = Path(xdg) / "trio-opencode" / "config.json"
    return candidate


def _coerce_timeout(value: Any) -> float:
    """``null`` normalizes to the ``0.0`` "disabled" sentinel (see
    :class:`TimeoutsConfig`); any number is just a float."""
    return 0.0 if value is None else float(value)


def _to_config(data: dict[str, Any], source_path: str | None) -> Config:
    provider_data = data["provider"]
    timeouts_data = data["timeouts"]
    retries_data = data["retries"]
    return Config(
        opencode_bin=data["opencode_bin"],
        models=dict(data["models"]),
        variants=dict(data["variants"]),
        provider=ProviderConfig(
            id=provider_data["id"],
            key_file=os.path.expanduser(provider_data["key_file"]),
            key_env=provider_data["key_env"],
        ),
        timeouts=TimeoutsConfig(
            turn_seconds=_coerce_timeout(timeouts_data["turn_seconds"]),
            idle_seconds=float(timeouts_data["idle_seconds"]),
            evaluator_turn_seconds=_coerce_timeout(timeouts_data["evaluator_turn_seconds"]),
        ),
        retries=RetriesConfig(
            max_attempts=int(retries_data["max_attempts"]),
            backoff_seconds=tuple(float(b) for b in retries_data["backoff_seconds"]),
            idle_retry_unlimited=bool(retries_data.get("idle_retry_unlimited", False)),
        ),
        max_iterations=int(data["max_iterations"]),
        root_free=bool(data["root_free"]),
        container_mode=bool(data.get("container_mode", False)),
        acceptance=bool(data.get("acceptance", False)),
        isolate_workers=bool(data.get("isolate_workers", True)),
        slice_eval_concurrency=int(data.get("slice_eval_concurrency", 4)),
        kill_check=bool(data.get("kill_check", True)),
        source_path=source_path,
    )


def load_config(path: str | None = None) -> Config:
    """Load config with precedence: ``path`` arg, ``TRIO_OPENCODE_CONFIG``
    env, the XDG default path, else built-in defaults. A missing file at the
    resolved path (env/XDG/default cases, never an explicit ``path`` arg
    that does not exist) silently falls back to defaults. An explicit
    ``path`` that does not exist is a ``ConfigError``.
    """
    defaults = _default_dict()
    resolved = _config_path(path)

    if resolved is None or not resolved.exists():
        if path is not None:
            raise ConfigError(f"config file not found: {path}")
        return _to_config(defaults, source_path=None)

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {resolved}: {exc}") from exc

    try:
        overlay = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config file {resolved}: {exc}") from exc

    if not isinstance(overlay, dict):
        raise ConfigError(f"config file {resolved} must contain a JSON object")

    _check_forbidden_key_fields(overlay)
    merged = _deep_merge(defaults, overlay)
    _validate_types(merged)
    return _to_config(merged, source_path=str(resolved))


def resolve_acceptance(cfg: Config, *, cli_flag: bool | None = None,
                       env: dict[str, str] | None = None) -> bool:
    """r19 frozen acceptance switch resolution (``start``; docs/FROZEN-ACCEPTANCE.md,
    native's ``args.acceptance``): ``--acceptance``/``--no-acceptance`` (``cli_flag``,
    ``True``/``False``/``None`` for "not passed") overrides ``TRIO_ACCEPTANCE``
    (``"1"``/``"0"`` in *env*, default ``os.environ``), which overrides
    ``cfg.acceptance`` (the config file), which defaults to ``False``."""
    if cli_flag is not None:
        return bool(cli_flag)
    environ = os.environ if env is None else env
    raw = environ.get("TRIO_ACCEPTANCE")
    if raw is not None:
        raw = raw.strip()
        if raw == "1":
            return True
        if raw == "0":
            return False
        # An unrecognized value falls through to the config/default layer
        # rather than silently picking a side.
    return bool(cfg.acceptance)


def validate(cfg: Config) -> list[str]:
    """Return a list of human-readable validation errors for ``cfg`` (empty
    list = valid). Checked (SPEC.md):
    - every model id is ``provider/model`` shaped
    - no ``<...>`` placeholders remain
    - lead/evaluator/acceptance are the SAME model id (acceptance author
      must never be cheaper than lead/evaluator)
    - idle_seconds >= 180 (provider-internal retries go silent 60-90s) —
      TEST-ONLY: setting ``TRIO_OPENCODE_TEST_TIMEOUTS=1`` lifts this one
      floor so a test's tiny-timeout Config still validates; every other
      check below still applies. Never set in production.
    - idle_seconds is positive
    - turn_seconds / evaluator_turn_seconds are >= 0 (0 disables the
      wall-clock limit for that role's turn — "no wall-clock limit" mode,
      see README.md "Container / no-time-limit mode"); negative is rejected
    - retries.max_attempts >= 1
    - variants.acceptance, if set, equals variants.lead when it is also set
      (the acceptance tier is authored by the Lead turn; an explicit
      acceptance variant is informational only and must not silently diverge)
    """
    errors: list[str] = []

    for role in _ROLES:
        mid = cfg.models.get(role)
        if mid is None:
            errors.append(f"missing model for role {role!r}")
            continue
        if _PLACEHOLDER_RE.search(mid):
            errors.append(f"model for role {role!r} still has a placeholder: {mid!r}")
        elif not _MODEL_ID_RE.match(mid):
            errors.append(
                f"model for role {role!r} is not in 'provider/model' form: {mid!r}"
            )

    same_tier_ids = {role: cfg.models.get(role) for role in _SAME_TIER_ROLES}
    distinct = {v for v in same_tier_ids.values() if v is not None}
    if len(distinct) > 1:
        errors.append(
            "lead, evaluator and acceptance must use the identical model id "
            f"(acceptance must never be cheaper): {same_tier_ids}"
        )

    if cfg.timeouts.idle_seconds < 180 and os.environ.get("TRIO_OPENCODE_TEST_TIMEOUTS") != "1":
        errors.append(
            "timeouts.idle_seconds must be >= 180 (provider-internal retries "
            f"go silent for 60-90s); got {cfg.timeouts.idle_seconds}"
        )
    if cfg.timeouts.idle_seconds <= 0:
        errors.append(f"timeouts.idle_seconds must be positive; got {cfg.timeouts.idle_seconds}")
    if cfg.timeouts.turn_seconds < 0:
        errors.append(
            "timeouts.turn_seconds must be >= 0 (0 disables the wall-clock "
            f"limit); got {cfg.timeouts.turn_seconds}"
        )
    if cfg.timeouts.evaluator_turn_seconds < 0:
        errors.append(
            "timeouts.evaluator_turn_seconds must be >= 0 (0 disables the "
            f"wall-clock limit); got {cfg.timeouts.evaluator_turn_seconds}"
        )

    if cfg.retries.max_attempts < 1:
        errors.append(f"retries.max_attempts must be >= 1; got {cfg.retries.max_attempts}")

    acceptance_variant = cfg.variants.get("acceptance")
    lead_variant = cfg.variants.get("lead")
    if acceptance_variant is not None and lead_variant is not None and acceptance_variant != lead_variant:
        errors.append(
            "variants.acceptance must equal variants.lead when both are set "
            f"(informational only, authored by the Lead turn): "
            f"acceptance={acceptance_variant!r} lead={lead_variant!r}"
        )

    return errors

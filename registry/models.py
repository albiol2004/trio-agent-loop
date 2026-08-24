"""Collect the model chosen for each harness agent.

The collector deliberately reads repository files from ``root`` and optional
runtime files from the caller-provided ``home`` only.  It never guesses the
user's home directory, which keeps the API deterministic and tests hermetic.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCAN_PATH = Path(__file__).with_name("scan.py")
_SCAN_MODULE = None

LAYER_ORDER = (
    "frontmatter",
    "omp-config",
    "opencode-jsonc",
    "trioctl-roles",
    "omnigent-executor",
)

ROLE_NAMES = {
    "lead": "trio-lead",
    "evaluator": "trio-evaluator",
    "builder": "trio-builder",
    "scout": "trio-scout",
}

HARNESS_AGENT_DIRS = (
    ("claude", Path(".claude") / "agents", "*.md"),
    ("omp", Path("omp") / "agents", "*.md"),
    ("codex", Path("codex") / "agents", "*.toml"),
    ("opencode", Path("opencode") / "agents", "*.md"),
)


def _load_scan_module():
    """Load the shared parser by path, matching the dashboard's pattern."""
    global _SCAN_MODULE
    if _SCAN_MODULE is not None:
        return _SCAN_MODULE
    spec = importlib.util.spec_from_file_location(
        "trio_registry_scan_for_models", SCAN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scan module: {SCAN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _SCAN_MODULE = module
    return module


def _model_value(value) -> str | None:
    """Return a non-empty model id while tolerating malformed source data."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def strip_jsonc(text: str) -> str:
    """Remove ``//`` comments without touching URLs inside JSON strings."""
    output: list[str] = []
    in_string = False
    escaped = False
    in_comment = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_comment:
            if char in "\r\n":
                in_comment = False
                output.append(char)
            index += 1
            continue
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            in_comment = True
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def parse_jsonc(text: str) -> dict:
    """Parse the repository's small JSONC subset into a top-level mapping."""
    value = json.loads(strip_jsonc(text))
    if not isinstance(value, dict):
        raise ValueError("JSONC top level must be an object")
    return value


def load_toml(path: Path) -> dict:
    """Read TOML through the shared scanner parser.

    A malformed optional source is treated as absent.  The parser itself still
    owns TOML semantics, so this module does not duplicate a TOML
    implementation.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
        value = _load_scan_module().parse_toml(text)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_yaml(path: Path) -> dict:
    """Load the shared YAML subset, turning a bad optional file into ``{}``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        value = _load_scan_module().parse_yaml(text)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _frontmatter_sources(root: Path) -> dict[str, dict[str, dict]]:
    """Find agent files and retain both their path and declared model."""
    found: dict[str, dict[str, dict]] = {}
    scan = _load_scan_module()
    for harness, relative_dir, pattern in HARNESS_AGENT_DIRS:
        directory = root / relative_dir
        if not directory.is_dir():
            continue
        agents = found.setdefault(harness, {})
        for path in sorted(directory.glob(pattern)):
            if not path.is_file():
                continue
            if path.suffix == ".toml":
                fields = load_toml(path)
            else:
                try:
                    text = path.read_text(encoding="utf-8")
                    fields, _body = scan.parse_frontmatter(text)
                except OSError:
                    fields = {}
            agents[path.stem] = {
                "path": str(path),
                "model": _model_value(fields.get("model")),
            }
    return found


def _omp_overrides(home: Path | None) -> tuple[dict[str, str], str | None]:
    """Read OMP's explicit per-agent override map, if the caller supplied it."""
    if home is None or not home.is_dir():
        return {}, None
    path = home / ".omp" / "agent" / "config.yml"
    if not path.is_file():
        return {}, None
    data = _load_yaml(path)
    task = data.get("task")
    overrides = task.get("agentModelOverrides") if isinstance(task, dict) else {}
    if not isinstance(overrides, dict):
        return {}, str(path)
    values = {
        str(name): model
        for name, value in overrides.items()
        if (model := _model_value(value)) is not None
    }
    return values, str(path)


def _opencode_jsonc(
    root: Path, home: Path | None
) -> tuple[dict[str, str], str | None]:
    """Choose the preferred JSONC config and read its per-agent models."""
    path = None
    if home is not None and home.is_dir():
        candidate = home / ".config" / "opencode" / "opencode.jsonc"
        if candidate.is_file():
            path = candidate
    if path is None:
        candidate = root / "opencode" / "opencode.trio.example.jsonc"
        if candidate.is_file():
            path = candidate
    if path is None:
        return {}, None
    try:
        data = parse_jsonc(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, str(path)
    agents = data.get("agent")
    if not isinstance(agents, dict):
        return {}, str(path)
    values = {}
    for name, config in agents.items():
        if isinstance(config, dict):
            model = _model_value(config.get("model"))
            if model is not None:
                values[str(name)] = model
    return values, str(path)


def _trioctl_models(root: Path) -> tuple[dict[str, str], str | None]:
    """Map trioctl role slots to both native and Omnigent agent names."""
    path = root / "omnigent" / "trioctl.example.toml"
    if not path.is_file():
        return {}, None
    data = load_toml(path)
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return {}, str(path)
    values = {}
    for slot, agent in ROLE_NAMES.items():
        role = roles.get(slot)
        if not isinstance(role, dict):
            continue
        model = _model_value(role.get("fallback_model"))
        if model is None:
            model = _model_value(role.get("model"))
        if model is not None:
            values[agent] = model
            values[f"trio-omnigent-{slot}"] = model
    return values, str(path)


def _omnigent_sources(root: Path) -> dict[str, dict]:
    """Find Omnigent role names and their final executor model source."""
    directory = root / "omnigent" / "trio-omnigent-roles"
    found: dict[str, dict] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*/config.yaml")):
        if not path.is_file():
            continue
        data = _load_yaml(path)
        name = _model_value(data.get("name")) or f"trio-omnigent-{path.parent.name}"
        executor = data.get("executor")
        executor_model = (
            _model_value(executor.get("model"))
            if isinstance(executor, dict)
            else None
        )
        found[name] = {
            "path": None,
            "model": None,
            "executor_model": executor_model,
            "executor_path": str(path),
        }
    return found


def resolve_model(
    layers: dict[str, object] | None = None, **overrides: object
) -> tuple[str | None, str]:
    """Return the first non-empty model and its winning layer.

    ``layers`` is intentionally a plain mapping so callers can assemble the
    sources they actually have.  Keyword values are accepted too, which makes
    this helper convenient in focused unit tests.
    """
    candidates = dict(layers or {})
    candidates.update(overrides)
    for layer in LAYER_ORDER:
        model = _model_value(candidates.get(layer))
        if model is not None:
            return model, layer
    return None, "none"


def _curated_models() -> dict[str, set[str]]:
    """Load the checked-in availability hints without ever writing to disk."""
    path = Path(__file__).with_name("models.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(harness): {
            model for model in values if isinstance(model, str) and model
        }
        for harness, values in data.items()
        if isinstance(values, list)
    }


def _harvest_model_ids(
    value, in_model_context: bool = False, provider: str | None = None
) -> set[str]:
    """Harvest model strings and model-map keys from a small config tree."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).lower()
            if name in {"model", "model_id", "modelid"}:
                if (model := _model_value(child)) is not None:
                    found.add(model)
                else:
                    found.update(_harvest_model_ids(child, True, provider))
            elif name in {"models", "model_ids"}:
                if isinstance(child, dict):
                    for model_key in child:
                        model_id = _model_value(model_key)
                        if model_id is not None:
                            found.add(model_id)
                            if provider and "/" not in model_id:
                                found.add(f"{provider}/{model_id}")
                found.update(_harvest_model_ids(child, True, provider))
            elif name in {"providers", "provider"} and isinstance(child, dict):
                for provider_name, provider_config in child.items():
                    found.update(_harvest_model_ids(
                        provider_config, in_model_context, str(provider_name)))
            else:
                found.update(_harvest_model_ids(child, in_model_context, provider))
    elif isinstance(value, list):
        for child in value:
            found.update(_harvest_model_ids(child, in_model_context, provider))
    elif in_model_context and (model := _model_value(value)) is not None:
        found.add(model)
    return found


def _available_models(
    home: Path | None, curated: dict[str, set[str]]
) -> dict[str, set[str]]:
    """Combine curated ids with explicitly supplied live configuration."""
    available = {harness: set(models) for harness, models in curated.items()}
    if home is None or not home.is_dir():
        return available

    omnigent = home / ".omnigent" / "config.yaml"
    if omnigent.is_file():
        available.setdefault("omnigent", set()).update(
            _harvest_model_ids(_load_yaml(omnigent)))

    opencode = home / ".config" / "opencode" / "opencode.json"
    if opencode.is_file():
        try:
            data = json.loads(opencode.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            data = {}
        available.setdefault("opencode", set()).update(
            _harvest_model_ids(data))

    codex = home / ".codex" / "config.toml"
    if codex.is_file():
        model = _model_value(load_toml(codex).get("model"))
        if model is not None:
            available.setdefault("codex", set()).add(model)
    return available


def collect_models(root: Path, home: Path | None = None) -> dict:
    """Collect deterministic model rows from a repository and optional home."""
    root = Path(root)
    explicit_home = Path(home) if home is not None else None
    frontmatter = _frontmatter_sources(root)
    omnigent = _omnigent_sources(root)
    omp_models, omp_path = _omp_overrides(explicit_home)
    jsonc_models, jsonc_path = _opencode_jsonc(root, explicit_home)
    trioctl_models, trioctl_path = _trioctl_models(root)

    sources = {
        harness: dict(agents) for harness, agents in frontmatter.items()
    }
    if omnigent:
        sources["omnigent"] = omnigent

    rows = []
    curated = _curated_models()
    available = _available_models(explicit_home, curated)
    for harness in ("claude", "codex", "omp", "opencode", "omnigent"):
        for agent in sorted(sources.get(harness, {})):
            source = sources[harness][agent]
            layers = {
                "frontmatter": source.get("model"),
                "omp-config": omp_models.get(agent),
                "opencode-jsonc": jsonc_models.get(agent),
                "trioctl-roles": trioctl_models.get(agent),
                "omnigent-executor": source.get("executor_model"),
            }
            model, layer = resolve_model(layers)
            override_file = None
            if layer == "omp-config":
                override_file = omp_path
            elif layer == "opencode-jsonc":
                override_file = jsonc_path
            elif layer == "trioctl-roles":
                override_file = trioctl_path
            elif layer == "omnigent-executor":
                override_file = source.get("executor_path")

            known = model is not None and model in available.get(harness, set())
            rows.append({
                "harness": harness,
                "agent": agent,
                "model": model,
                "layer": layer,
                "editable": layer == "frontmatter" and bool(source.get("path")),
                "path": source.get("path"),
                "override_file": override_file,
                "availability": "known" if known else "unknown",
                "warning": None if model is None or known
                else "unknown model id",
            })
    return {
        "root": str(root),
        "rows": rows,
        "available": {
            harness: sorted(models) for harness, models in available.items()
        },
    }

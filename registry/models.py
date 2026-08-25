"""Collect the model chosen for each harness agent.

The collector deliberately reads repository files from ``root`` and optional
runtime files from the caller-provided ``home`` only.  It never guesses the
user's home directory, which keeps the API deterministic and tests hermetic.
``harvest_catalog`` additionally reports configured and optional live model
catalogs, while keeping curated IDs available when a live source fails.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


SCAN_PATH = Path(__file__).with_name("scan.py")
_SCAN_MODULE = None

CATALOG_HARNESSES = (
    "claude",
    "codex",
    "omp",
    "opencode",
    "cursor",
    "omnigent",
)
EXECUTOR_HARNESSES = ("claude", "codex", "cursor", "omp", "opencode")
KNOWN_CLAUDE_ALIASES = {"sonnet", "opus", "haiku"}
CATALOG_CACHE_SECONDS = 300.0
_CATALOG_CACHE: dict[tuple[str, str, bool], tuple[float, dict]] = {}
_CATALOG_CACHE_LOCK = threading.RLock()

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


def _read_curated_models() -> tuple[dict[str, set[str]], str | None]:
    """Load the checked-in catalog and retain a useful parse error."""
    path = Path(__file__).with_name("models.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return {}, "models.json top level must be an object"
    return {
        str(harness): {
            model.strip() for model in values
            if isinstance(model, str) and model.strip()
        }
        for harness, values in data.items()
        if isinstance(values, list)
    }, None


def _curated_models() -> dict[str, set[str]]:
    """Load the checked-in availability hints without writing to disk."""
    models, _error = _read_curated_models()
    return models


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


def _source_record(
    source: str | Path,
    models: set[str],
    ok: bool = True,
    error: str | None = None,
) -> dict:
    """Build the small, JSON-safe status record used by the public API."""
    return {
        "source": str(source),
        "ok": bool(ok),
        "count": len(models),
        "error": error if not ok else None,
    }


def _read_catalog_file(path: Path, parser) -> tuple[set[str], bool, str | None]:
    """Read and parse one optional catalog file without hiding failures."""
    try:
        data = parser(path.read_text(encoding="utf-8"))
        return _harvest_model_ids(data), True, None
    except Exception as exc:
        message = str(exc).strip()
        detail = (
            f"{type(exc).__name__}: {message}"
            if message
            else type(exc).__name__
        )
        return set(), False, detail


def _parse_toml_document(text: str) -> dict:
    """Parse TOML while preserving errors for the catalog source record."""
    data = _load_scan_module().parse_toml(text)
    if not isinstance(data, dict):
        raise ValueError("TOML top level must be an object")
    return data


def _parse_yaml_document(text: str) -> dict:
    """Parse YAML while preserving errors for the catalog source record."""
    data = _load_scan_module().parse_yaml(text)
    if not isinstance(data, dict):
        raise ValueError("YAML top level must be an object")
    return data


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")
_CATALOG_HEADERS = {
    "available",
    "available models",
    "model",
    "models",
    "name",
    "provider",
}
_CURSOR_ROW_RE = re.compile(r"^(\S+)\s+-\s+(.+?)\s*$")


def _strip_ansi(text: str) -> str:
    """Remove terminal color/control sequences before parsing CLI output."""
    return _ANSI_RE.sub("", text)


def _looks_like_model_id(value: str) -> bool:
    """Accept a conservative bare token while skipping common table labels."""
    value = value.strip()
    if not value or value.lower().rstrip(":") in _CATALOG_HEADERS:
        return False
    return bool(_MODEL_ID_RE.fullmatch(value))


def _parse_cursor_catalog(text: str) -> set[str]:
    """Parse Cursor's ``id - display name`` rows and bare model ids."""
    found = set()
    for raw_line in text.splitlines():
        line = _strip_ansi(raw_line).strip()
        if not line:
            continue
        match = _CURSOR_ROW_RE.match(line)
        if match:
            model_id = match.group(1)
            if _looks_like_model_id(model_id):
                found.add(model_id)
            continue
        if " " not in line and _looks_like_model_id(line):
            found.add(line)
    return found


def _parse_line_catalog(text: str) -> set[str]:
    """Parse CLIs that print exactly one model id on each non-empty line."""
    found = set()
    for raw_line in text.splitlines():
        line = _strip_ansi(raw_line).strip()
        if _looks_like_model_id(line):
            found.add(line)
    return found


def _parse_codex_lines(text: str) -> set[str]:
    """Parse Codex's possible one-column or table-like text output."""
    found = set()
    for raw_line in text.splitlines():
        line = _strip_ansi(raw_line).strip()
        if not line:
            continue
        token = line.split()[0]
        if _looks_like_model_id(token):
            found.add(token)
    return found


def _parse_claude_help(text: str) -> set[str]:
    """Keep only known Claude aliases; help text is not a model catalog."""
    found = set()
    for alias in KNOWN_CLAUDE_ALIASES:
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(alias)}(?![A-Za-z0-9_-])",
            text,
            flags=re.IGNORECASE,
        ):
            found.add(alias)
    return found


def _json_catalog_ids(value) -> set[str]:
    """Extract common id fields from a JSON model-list response."""
    found = set()
    if isinstance(value, str):
        if _looks_like_model_id(value):
            found.add(value.strip())
        return found
    if isinstance(value, list):
        for item in value:
            found.update(_json_catalog_ids(item))
        return found
    if not isinstance(value, dict):
        return found

    provider = _model_value(value.get("provider"))
    identifier = _model_value(value.get("id"))
    if provider is not None and identifier is not None:
        found.add(
            identifier if "/" in identifier
            else f"{provider}/{identifier}"
        )
    for key, child in value.items():
        name = str(key).lower()
        if name in {"id", "model", "model_id", "modelid", "selector"}:
            model = _model_value(child)
            if model is not None:
                found.add(model)
        elif name != "provider":
            found.update(_json_catalog_ids(child))
    return found


def _omp_json_ids(value) -> set[str]:
    """Extract OMP selectors, or construct them from provider and id."""
    found = set()
    if isinstance(value, list):
        for item in value:
            found.update(_omp_json_ids(item))
        return found
    if isinstance(value, str):
        if _looks_like_model_id(value):
            found.add(value.strip())
        return found
    if not isinstance(value, dict):
        return found

    selector = _model_value(value.get("selector"))
    if selector is not None:
        found.add(selector)
    else:
        provider = _model_value(value.get("provider"))
        identifier = _model_value(value.get("id"))
        if provider is not None and identifier is not None:
            found.add(
                identifier if "/" in identifier
                else f"{provider}/{identifier}"
            )
    for key, child in value.items():
        if str(key).lower() not in {"selector", "provider", "id"}:
            found.update(_omp_json_ids(child))
    return found


def _parse_omp_catalog(text: str) -> set[str]:
    """Parse the JSON returned by ``omp models --json``."""
    return _omp_json_ids(json.loads(text))


def _parse_codex_catalog(text: str) -> set[str]:
    """Parse JSON when available, otherwise take ids from table-like lines."""
    try:
        return _json_catalog_ids(json.loads(text))
    except json.JSONDecodeError:
        return _parse_codex_lines(text)


def _command_environment(home: Path | None, env) -> dict:
    """Copy the caller environment and redirect HOME when requested."""
    values = dict(os.environ if env is None else env)
    if home is not None:
        values["HOME"] = str(home)
    return values


def _run_catalog_cli(
    command: str,
    arguments: tuple[str, ...],
    home: Path | None,
    env,
    parser,
) -> tuple[set[str], dict]:
    """Run one explicitly enabled CLI and turn failures into status data."""
    label = " ".join((command, *arguments))
    try:
        process_env = _command_environment(home, env)
        path = process_env.get("PATH")
        if path is None and env is not None:
            path = ""
        elif path is not None:
            path = os.fspath(path)
        executable = shutil.which(command, path=path)
    except Exception as exc:
        message = str(exc).strip()
        detail = (
            f"{type(exc).__name__}: {message}"
            if message
            else type(exc).__name__
        )
        return set(), _source_record(label, set(), False, detail)
    if executable is None:
        return set(), _source_record(
            label, set(), False, f"{command} not found on PATH"
        )

    try:
        result = subprocess.run(
            [executable, *arguments],
            env=process_env,
            timeout=20,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", None)
        detail = (
            stderr.strip()
            if isinstance(stderr, str) and stderr.strip()
            else str(exc)
        )
        detail = detail or type(exc).__name__
        return set(), _source_record(label, set(), False, detail)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = stderr or (result.stdout or "").strip()
        detail = detail or f"exit status {result.returncode}"
        return set(), _source_record(label, set(), False, detail)

    try:
        models = parser(result.stdout or "")
    except Exception as exc:
        message = str(exc).strip()
        detail = (
            f"{type(exc).__name__}: {message}"
            if message
            else type(exc).__name__
        )
        return set(), _source_record(label, set(), False, detail)
    return models, _source_record(label, models)


def _add_file_catalog(
    catalogs: dict[str, set[str]],
    sources: dict[str, list[dict]],
    harness: str,
    path: Path,
    parser,
) -> None:
    """Add one existing file and its status to a harness catalog."""
    try:
        if not path.is_file():
            return
    except OSError as exc:
        message = str(exc).strip()
        detail = (
            f"{type(exc).__name__}: {message}"
            if message
            else type(exc).__name__
        )
        sources[harness].append(
            _source_record(path, set(), False, detail)
        )
        return
    models, ok, error = _read_catalog_file(path, parser)
    catalogs[harness].update(models)
    sources[harness].append(_source_record(path, models, ok, error))


def _harvest_catalog_uncached(
    home: Path | None,
    env,
    allow_cli: bool,
) -> dict:
    """Build a catalog from curated, configured, and optional live sources."""
    catalogs = {harness: set() for harness in CATALOG_HARNESSES}
    sources = {harness: [] for harness in CATALOG_HARNESSES}
    curated, curated_error = _read_curated_models()
    curated_path = Path(__file__).with_name("models.json")

    # Curated ids are deliberately loaded first so a missing CLI never removes
    # the offline choices shipped with the dashboard.
    for harness in EXECUTOR_HARNESSES:
        models = curated.get(harness, set())
        catalogs[harness].update(models)
        sources[harness].append(
            _source_record(
                curated_path,
                models,
                curated_error is None,
                curated_error,
            )
        )
    aliases = set(KNOWN_CLAUDE_ALIASES)
    catalogs["claude"].update(aliases)
    sources["claude"].append(_source_record("known aliases", aliases))

    if home is not None:
        _add_file_catalog(
            catalogs,
            sources,
            "claude",
            home / ".claude" / "settings.json",
            json.loads,
        )
        _add_file_catalog(
            catalogs,
            sources,
            "codex",
            home / ".codex" / "config.toml",
            _parse_toml_document,
        )
        _add_file_catalog(
            catalogs,
            sources,
            "opencode",
            home / ".config" / "opencode" / "opencode.json",
            json.loads,
        )
        _add_file_catalog(
            catalogs,
            sources,
            "opencode",
            home / ".config" / "opencode" / "opencode.jsonc",
            parse_jsonc,
        )

        # Only well-known OMP config files — never walk session trees.
        for relative in (
            Path(".omp") / "config.yml",
            Path(".omp") / "config.yaml",
            Path(".omp") / "agent" / "config.yml",
            Path(".omp") / "agent" / "config.yaml",
        ):
            _add_file_catalog(
                catalogs,
                sources,
                "omp",
                home / relative,
                _parse_yaml_document,
            )

    if allow_cli:
        cli_specs = (
            ("claude", "claude", ("--help",), _parse_claude_help),
            # Codex has no simple catalog CLI; a failed `codex models`
            # becomes a source error. Never spawn `codex app-server`.
            ("codex", "codex", ("models",), _parse_codex_catalog),
            ("opencode", "opencode", ("models",), _parse_line_catalog),
            ("omp", "omp", ("models", "--json"), _parse_omp_catalog),
            ("cursor", "cursor-agent", ("models",), _parse_cursor_catalog),
        )
        for harness, command, arguments, parser in cli_specs:
            models, status = _run_catalog_cli(
                command, arguments, home, env, parser
            )
            catalogs[harness].update(models)
            sources[harness].append(status)

    catalogs["omnigent"] = set().union(
        *(catalogs[harness] for harness in EXECUTOR_HARNESSES)
    )
    sources["omnigent"].append(
        _source_record("executor-union", catalogs["omnigent"])
    )

    available = {
        harness: sorted(catalogs[harness])
        for harness in CATALOG_HARNESSES
    }
    by_executor = {}
    for harness in EXECUTOR_HARNESSES:
        values = available[harness]
        by_executor[harness] = list(values)
        by_executor[f"{harness}-native"] = list(values)
    return {
        "available": available,
        "by_executor": by_executor,
        "sources": sources,
    }


def _catalog_cache_key(
    home: Path | None, env, allow_cli: bool
) -> tuple[str, str, bool]:
    """Use only the documented inputs to identify an in-process cache entry."""
    home_key = str(home) if home is not None else ""
    if env is None:
        path = os.environ.get("PATH", "")
    else:
        path = env.get("PATH", "")
    return home_key, str(path or ""), bool(allow_cli)


def harvest_catalog(
    home,
    *,
    env=None,
    allow_cli=False,
    use_cache=True,
) -> dict:
    """Harvest model ids from an explicit home and optional harness CLIs."""
    explicit_home = Path(home) if home is not None else None
    cache_key = _catalog_cache_key(explicit_home, env, allow_cli)
    now = time.monotonic()
    if use_cache:
        with _CATALOG_CACHE_LOCK:
            cached = _CATALOG_CACHE.get(cache_key)
            if cached is not None and now - cached[0] <= CATALOG_CACHE_SECONDS:
                return copy.deepcopy(cached[1])

    result = _harvest_catalog_uncached(explicit_home, env, bool(allow_cli))
    with _CATALOG_CACHE_LOCK:
        _CATALOG_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(result))
    return copy.deepcopy(result)


def collect_models(
    root: Path,
    home: Path | None = None,
    *,
    env=None,
    allow_cli=False,
) -> dict:
    """Collect model rows and live catalogs from a repository and home."""
    root = Path(root)
    explicit_home = Path(home) if home is not None else None
    frontmatter = _frontmatter_sources(root)
    omnigent = _omnigent_sources(root)
    omp_models, omp_path = _omp_overrides(explicit_home)
    jsonc_models, jsonc_path = _opencode_jsonc(root, explicit_home)
    trioctl_models, trioctl_path = _trioctl_models(root)
    catalog = harvest_catalog(
        explicit_home,
        env=env,
        allow_cli=allow_cli,
    )
    catalog_sets = {
        harness: set(models)
        for harness, models in catalog["available"].items()
    }

    row_sources = {
        harness: dict(agents) for harness, agents in frontmatter.items()
    }
    if omnigent:
        row_sources["omnigent"] = omnigent

    rows = []
    for harness in ("claude", "codex", "omp", "opencode", "omnigent"):
        for agent in sorted(row_sources.get(harness, {})):
            source = row_sources[harness][agent]
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

            known = (
                model is not None
                and model in catalog_sets.get(harness, set())
            )
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
        "available": catalog["available"],
        "by_executor": catalog["by_executor"],
        "sources": catalog["sources"],
    }


def _refresh_curated_file() -> int:
    """Refresh only the checked-in fallback file from the live catalog."""
    catalog = harvest_catalog(
        Path.home(),
        env=os.environ,
        allow_cli=True,
        use_cache=False,
    )
    path = Path(__file__).with_name("models.json")
    path.write_text(
        json.dumps(catalog["available"], indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    for harness in CATALOG_HARNESSES:
        print(f"{harness}: {len(catalog['available'][harness])}")
        for source in catalog["sources"][harness]:
            if not source["ok"]:
                print(
                    f"{harness}: {source['source']}: {source['error']}",
                    file=sys.stderr,
                )
    return 0


def main(argv=None) -> int:
    """Run the offline-fallback refresh command when requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="harvest live catalogs into registry/models.json",
    )
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.error("one of --refresh is required")
    return _refresh_curated_file()


if __name__ == "__main__":
    raise SystemExit(main())

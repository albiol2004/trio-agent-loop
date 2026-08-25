#!/usr/bin/env python3
"""Build deterministic topology graphs from canonical harness files.

Only the repository and optional home paths passed by the caller are scanned.
This module never infers or reads ``Path.home()`` for a dashboard request.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PRODUCTIONIZE_WRAPPERS = (
    ("claude", ".claude/skills/trio-productionize/SKILL.md"),
    ("codex", "codex/skills/trio-productionize/SKILL.md"),
    ("omp", "omp/commands/trio-productionize.md"),
    ("opencode", "opencode/commands/trio-productionize.md"),
    ("kimi", "kimi/skills/trio-productionize/SKILL.md"),
    ("zcode", "zcode/skills/trio-productionize/SKILL.md"),
)
INSTALLED_PRODUCTIONIZE_WRAPPERS = (
    ("claude", ".claude/skills/trio-productionize/SKILL.md"),
    ("codex", ".agents/skills/trio-productionize/SKILL.md"),
    ("omp", ".omp/agent/commands/trio-productionize.md"),
    ("opencode", ".config/opencode/commands/trio-productionize.md"),
    ("kimi", ".kimi-code/skills/trio-productionize/SKILL.md"),
    ("zcode", ".zcode/skills/trio-productionize/SKILL.md"),
)
WORKFLOWS = frozenset(("roles", "productionize", "entrypoints"))


def _load_scan():
    """Load the shared parser once, without optional YAML dependencies."""
    spec = importlib.util.spec_from_file_location(
        "trio_registry_topology_scan", REPO / "registry" / "scan.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load registry/scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scan()


def _text(path: Path) -> str:
    """Read an optional source without aborting the whole topology scan."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse(path: Path, fmt: str = "frontmatter") -> tuple[dict, str]:
    """Use scan.py for Markdown frontmatter, YAML, and TOML."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    try:
        if fmt == "toml":
            return scan.parse_toml(text), ""
        if fmt == "yaml":
            return scan.parse_yaml(text), ""
        return scan.parse_frontmatter(text)
    except (TypeError, ValueError):
        return {}, ""


def _node(kind: str, name: str, harness: str, path: Path,
          model=None, tool_policy=None, output=None, *, origin="workspace") -> dict:
    return dict(kind=kind, name=str(name), harness=harness, path=str(path),
                model=model or None, tool_policy=tool_policy or None,
                output=bool(output) if kind == "agent" else False,
                origin=origin)


class _Graph:
    """Small deduplicating graph builder shared by all harness adapters."""

    def __init__(self, origin="workspace"):
        self.nodes = {}
        self.edges = {}
        self.origin = origin

    def node(self, kind, name, harness, path, model=None, tool_policy=None,
             output=None, *, origin=None):
        key = (kind, str(name))
        node_origin = self.origin if origin is None else origin
        self.nodes.setdefault(
            key, _node(
                kind, name, harness, path, model, tool_policy, output,
                origin=node_origin))

    def edge(self, edge_type, src, dst):
        key = (edge_type, str(src), str(dst))
        self.edges[key] = dict(
            type=edge_type, src=str(src), dst=str(dst))

    def agent(self, harness, name, path, model=None, tool_policy=None,
              output=None, *, origin=None):
        name = str(name)
        model = str(model).strip() if model is not None else ""
        self.node(
            "agent", name, harness, path, model, tool_policy, output,
            origin=origin)
        if model:
            self.node("model", model, harness, path, model, origin=origin)
            self.edge("invokes", name, model)

    def result(self) -> dict:
        """Apply the public node and edge ordering contract."""
        return {
            "nodes": sorted(
                self.nodes.values(),
                key=lambda item: (item["kind"], item["name"])),
            "edges": sorted(
                self.edges.values(),
                key=lambda item: (item["type"], item["src"], item["dst"])),
        }


def _joined(value) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value)
    return str(value).strip()


def _permission(permission) -> str | None:
    """Summarize permissions while retaining OpenCode task targets."""
    if permission is None:
        return None
    if not isinstance(permission, dict):
        return _joined(permission) or None
    parts = []
    for key, value in permission.items():
        if key == "*":
            continue
        if isinstance(value, dict):
            allowed = [
                str(child) for child, mode in value.items()
                if child != "*" and mode == "allow"
            ]
            if allowed:
                parts.append(
                    f"{key}={','.join(allowed)}"
                    if key == "task" else str(key))
        elif value == "allow":
            parts.append(str(key))
    return ", ".join(parts) or None


def _agent_info(fields: dict, kind: str) -> tuple[str | None, list[str]]:
    """Return a compact tool policy and the explicit spawn targets."""
    if kind == "opencode":
        permission = fields.get("permission")
        task = permission.get("task") if isinstance(permission, dict) else None
        targets = [
            str(target) for target, mode in
            (task.items() if isinstance(task, dict) else ())
            if target != "*" and mode == "allow"
        ]
        return _permission(permission), targets
    if kind == "omp":
        value = fields.get("spawns")
        text = _joined(value) if value else ""
        targets = [item.strip() for item in text.split(",") if item.strip()]
        return (f"spawns: {text}" if text else None), targets
    if kind == "claude":
        value = fields.get("disallowedTools")
        return (
            f"disallowedTools={_joined(value)}" if value else None, [])
    return None, []


def _dispatch_section(body: str) -> str | None:
    """Return the body of a Markdown Dispatch table, if one exists."""
    heading = re.search(r"(?m)^##\s+Dispatch table\s*$", body)
    if not heading:
        return None
    section = re.split(
        r"(?m)^##\s+", body[heading.end():], maxsplit=1)[0]
    return section


def _dispatch(body: str, skill: str, graph: _Graph) -> None:
    """Read trio-agent targets only from a Dispatch table section."""
    section = _dispatch_section(body)
    if section is None:
        return
    # Keep the original trio-prefixed forms used by Claude and OpenCode.
    targets = re.findall(
        r"""(?:agent:\s*[`"']?|`)(trio-[a-z0-9][a-z0-9-]*)""",
        section)
    # OMP uses quoted short role names; normalize only known agent roles so
    # executor/user and executor/ask text can never become agent edges.
    short_targets = re.findall(
        r"""agent:\s*["'](scout|lead|evaluator|builder|orchestrator|repair)
        ["']""",
        section,
        re.VERBOSE,
    )
    targets.extend(f"trio-{target}" for target in short_targets)
    for target in dict.fromkeys(targets):
        graph.edge("dispatches_to", skill, target)


_PRODUCTIONIZE_EXECUTORS = {
    "scout": "trio-scout",
    "lead": "trio-lead",
    "evaluator": "trio-evaluator",
    "assessor:standard": "trio-lead",
    "assessor:high": "trio-evaluator",
}


def _productionize_role(value: str) -> str:
    """Normalize short role aliases to the repository's trio-prefixed names."""
    value = value.strip("`\"' .,")
    if value.startswith("trio-") or value == "default task agent":
        return value
    return _PRODUCTIONIZE_EXECUTORS.get(value, value)


def _productionize_targets(line: str) -> list[str]:
    """Extract named executor roles from one Dispatch table row."""
    targets = []
    if re.search(
        r"\bdefault\s+[`\"']?task[`\"']?\s+agent\b",
        line,
        re.IGNORECASE,
    ):
        targets.append("default task agent")

    # A task(agent:...), custom agent, or named subtask gives an explicit
    # harness alias. Keep the extraction narrow so prose cannot become a node.
    explicit = re.findall(
        r"""agent\s*:\s*[`"']?([a-z0-9][a-z0-9-]*)""",
        line,
        re.IGNORECASE,
    )
    explicit.extend(re.findall(
        r"""custom\s+(?:subagent\s+)?[`"']?
        (trio-[a-z0-9][a-z0-9-]*)""",
        line,
        re.IGNORECASE | re.VERBOSE,
    ))
    explicit.extend(re.findall(
        r"""(?:subtask|subagent)\s+agent\s+[`"']?
        (trio-[a-z0-9][a-z0-9-]*)""",
        line,
        re.IGNORECASE | re.VERBOSE,
    ))
    explicit.extend(re.findall(
        r"""(trio-[a-z0-9][a-z0-9-]*)\s*[`"']?\s+agent\b""",
        line,
        re.IGNORECASE | re.VERBOSE,
    ))
    explicit.extend(re.findall(
        r"""\brole\s+[`"']?
        (scout|lead|evaluator|builder|orchestrator|repair)""",
        line,
        re.IGNORECASE | re.VERBOSE,
    ))
    explicit.extend(re.findall(
        r"""(?<![\w-])/(trio-[a-z0-9][a-z0-9-]*|scout|lead|evaluator)
        \b""",
        line,
        re.IGNORECASE | re.VERBOSE,
    ))
    targets.extend(_productionize_role(target) for target in explicit)

    # A minimal fixture may only say "executor: scout"; map that logical
    # executor so a valid table does not require harness-specific prose.
    if targets:
        return list(dict.fromkeys(targets))
    executor_re = (
        r"\bexecutor\s*:\s*"
        r"(assessor:(?:standard|high)|scout|lead|evaluator)\b")
    targets.extend(
        _productionize_role(match.group(1))
        for match in re.finditer(executor_re, line, re.IGNORECASE))
    return list(dict.fromkeys(targets))


def _productionize_mechanisms(line: str, surface: str) -> set[str]:
    """Classify the mechanisms named by one Dispatch table row."""
    mechanisms = set()
    if re.search(
        r"\b(?:slash\s+command|omp\s+command)\b",
        line,
        re.IGNORECASE,
    ):
        mechanisms.add("command")
    if re.search(r"\b(?:skill|run-role\.sh|fallback)\b", line,
                 re.IGNORECASE):
        mechanisms.add("skill")
    if re.search(
        r"\b(?:Task|Agent|subagent)\b|agent\s*:",
        line,
        re.IGNORECASE,
    ):
        mechanisms.add("subagent")
    # A terse row can identify only its wrapper surface. Use that as the
    # conservative fallback instead of inventing a dispatch type.
    return mechanisms or {surface}


def _productionize_surface(path: Path) -> str:
    """Return the native surface used by a productionize wrapper."""
    return "skill" if path.name == "SKILL.md" else "command"


def _collect_productionize(root: Path, *, origin="workspace",
                           wrappers=None) -> dict:
    """Collect one graph per trio-productionize wrapper."""
    graphs = {}
    wrappers = PRODUCTIONIZE_WRAPPERS if wrappers is None else wrappers
    for harness, relative in wrappers:
        path = root / relative
        if not path.is_file():
            continue

        graph = _Graph(origin)
        section = _dispatch_section(_text(path))
        entrypoint = "trio-productionize"
        parsed = False
        if section is not None:
            graph.node("entrypoint", entrypoint, harness, path)
            surface = _productionize_surface(path)
            for line in section.splitlines():
                if not re.search(r"\bexecutor\s*:", line, re.IGNORECASE):
                    continue
                targets = _productionize_targets(line)
                if not targets:
                    continue
                parsed = True
                for target in targets:
                    graph.node("agent", target, harness, path)
                    for mechanism in _productionize_mechanisms(line, surface):
                        graph.edge(mechanism, entrypoint, target)
        if not parsed:
            # Keep a present but malformed wrapper visible to the dashboard.
            graph = _Graph(origin)
            graph.node(
                "warning", "unparseable-dispatch-table", harness, path)
        graphs[harness] = graph.result()
    return graphs


def _files(root: Path, relative: str | None, pattern: str):
    return sorted((root / relative).glob(pattern)) if relative else ()


def _markdown(root: Path, harness: str, *, agents=None, commands=None,
              skills=None, policy_kind=None, origin="workspace") -> _Graph:
    """Collect the shared command, skill, and Markdown-agent conventions."""
    graph = _Graph(origin)
    for path in _files(root, commands, "*.md"):
        fields, body = _parse(path)
        name = path.stem
        graph.node("entrypoint", name, harness, path)
        if fields.get("agent"):
            graph.edge("invokes", name, fields["agent"])
        # Commands can declare a Dispatch table in their Markdown body, just
        # like skills. Parse that body once to retain its dispatch edges.
        _dispatch(body, name, graph)
    for path in _files(root, skills, "*/SKILL.md"):
        _, body = _parse(path)
        name = path.parent.name
        graph.node("entrypoint", name, harness, path)
        _dispatch(body, name, graph)
    for path in _files(root, agents, "*.md"):
        fields, _ = _parse(path)
        name = path.stem
        policy, targets = _agent_info(fields, policy_kind)
        graph.agent(
            harness, name, path, fields.get("model"), policy,
            output=fields.get("output"))
        for target in targets:
            graph.edge("spawns", name, target)
    return graph


def _collect_opencode(root: Path, *, origin="workspace",
                      agents="opencode/agents",
                      commands="opencode/commands") -> dict:
    return _markdown(
        root, "opencode", agents=agents, commands=commands,
        policy_kind="opencode", origin=origin).result()


def _collect_omp(root: Path, *, origin="workspace",
                 agents="omp/agents", commands="omp/commands") -> dict:
    return _markdown(
        root, "omp", agents=agents, commands=commands,
        policy_kind="omp", origin=origin).result()


def _collect_claude(root: Path, *, origin="workspace",
                    agents=".claude/agents",
                    skills=".claude/skills") -> dict:
    return _markdown(
        root, "claude", agents=agents, skills=skills,
        policy_kind="claude", origin=origin).result()


def _collect_cursor(root: Path, *, origin="workspace",
                    agents=".cursor/agents",
                    commands=".cursor/commands",
                    skills=".cursor/skills") -> dict:
    return _markdown(
        root, "cursor", agents=agents, skills=skills, commands=commands,
        origin=origin).result()


def _collect_codex(root: Path, *, origin="workspace",
                   agents="codex/agents", skills="codex/skills") -> dict:
    graph = _markdown(
        root, "codex", skills=skills, origin=origin)
    role_re = re.compile(r"\btrio-[a-z0-9][a-z0-9-]*\b")
    for path in _files(root, agents, "*.toml"):
        fields, _ = _parse(path, "toml")
        name = path.stem
        sandbox = fields.get("sandbox_mode")
        graph.agent(
            "codex", name, path, fields.get("model"),
            f"sandbox_mode={sandbox}" if sandbox else None,
            output=fields.get("output"))
        instructions = fields.get("developer_instructions") or ""
        if not isinstance(instructions, str):
            instructions = str(instructions)
        for target in dict.fromkeys(role_re.findall(instructions)):
            if target != name:
                graph.edge("spawns", name, target)
    return graph.result()


def _collect_omnigent(root: Path, *, roles=None, origin="workspace") -> dict:
    """Collect Omnigent role configs and optional registered role names."""
    graph = _Graph(origin)
    roles = (
        Path(roles) if roles is not None
        else root / "omnigent" / "trio-omnigent-roles"
    )
    config_names = set()
    if roles.is_dir():
        for role in sorted(roles.iterdir()):
            path = role / "config.yaml"
            if not role.is_dir() or not path.is_file():
                continue
            fields, _ = _parse(path, "yaml")
            executor = fields.get("executor")
            executor = executor if isinstance(executor, dict) else {}
            spawn = fields.get("spawn")
            name = str(fields.get("name") or role.name)
            config_names.update((name, role.name))
            graph.agent(
                "omnigent", name, path, executor.get("model"),
                f"spawn: {str(spawn).lower()}" if spawn is not None else None,
                output=fields.get("output"))

    # The registry is a name index, not a replacement for a role config.
    registry = roles / "registry.json"
    if registry.is_file():
        try:
            registered = json.loads(_text(registry))
        except (TypeError, ValueError):
            registered = {}
        if isinstance(registered, dict):
            for name, value in registered.items():
                if name == "_profile" or not isinstance(value, dict):
                    continue
                if str(name) in config_names:
                    continue
                graph.node("agent", name, "omnigent", registry)
    return graph.result()


def _pi_policy(source: str, label: str) -> str | None:
    match = re.search(
        rf"\bconst\s+{label}\s*=\s*\[(.*?)\]", source, re.DOTALL)
    if not match:
        return None
    tools = re.findall(r"""["']([^"']+)["']""", match.group(1))
    return f"{label}={','.join(tools)}" if tools else None


def _collect_pi(root: Path, *, extension=None, origin="workspace") -> dict:
    graph = _Graph(origin)
    path = (
        Path(extension) if extension is not None
        else root / "pi" / "extensions" / "trio.ts"
    )
    source = _text(path)
    command_re = re.compile(
        r"""(?:pi\.)?registerCommand\(\s*["']([^"']+)""")
    for match in command_re.finditer(source):
        graph.node("entrypoint", match.group(1), "pi", path)
    if re.search(r"\brunRole\s*\(", source):
        policies = {
            "trio-scout": _pi_policy(source, "READ_TOOLS"),
            "trio-lead": _pi_policy(source, "WRITE_TOOLS"),
            "trio-builder": _pi_policy(source, "WRITE_TOOLS"),
            "trio-evaluator": _pi_policy(source, "WRITE_TOOLS"),
        }
        for name, policy in policies.items():
            graph.node("agent", name, "pi", path, tool_policy=policy)
            graph.edge("dispatches_to", "trio", name)
    return graph.result()


def _is_trio_entrypoint(name: str) -> bool:
    """Return whether a surface name is a Trio entrypoint."""
    return name == "trio" or name.startswith("trio-")


def _entrypoint_graph(source: _Graph | dict) -> dict:
    """Keep Trio entrypoints and only their destination-agent wiring."""
    if isinstance(source, _Graph):
        source = source.result()

    entrypoints = {
        node["name"]: node
        for node in source["nodes"]
        if node["kind"] == "entrypoint"
        and _is_trio_entrypoint(node["name"])
    }
    outbound = [
        edge for edge in source["edges"]
        if edge["src"] in entrypoints
    ]
    agents = {
        node["name"]: node
        for node in source["nodes"]
        if node["kind"] == "agent"
    }
    graph = _Graph()

    for node in entrypoints.values():
        graph.node(
            "entrypoint", node["name"], node["harness"], node["path"],
            origin=node.get("origin", "workspace"))

    for edge in outbound:
        destination = agents.get(edge["dst"])
        if destination is None:
            # Dispatch tables may name an agent that is not installed locally.
            # Keep that destination visible, just like productionize does.
            entrypoint = entrypoints[edge["src"]]
            graph.node(
                "agent",
                edge["dst"],
                entrypoint["harness"],
                entrypoint["path"],
                origin=entrypoint.get("origin", "workspace"),
            )
        else:
            graph.node(
                "agent",
                destination["name"],
                destination["harness"],
                destination["path"],
                destination.get("model"),
                destination.get("tool_policy"),
                destination.get("output"),
                origin=destination.get("origin", "workspace"),
            )
        graph.edge(edge["type"], edge["src"], edge["dst"])

    return graph.result()


def _collect_omnigent_entrypoints(
    root: Path, *, origin="workspace", entrypoint_paths=None, roles=None
) -> dict:
    """Collect Omnigent entrypoints and the roles they explicitly dispatch."""
    source = _Graph(origin)
    entrypoint_root = root / "omnigent" / "entrypoints"
    linked_targets = set()
    dispatch_targets = {
        "trio-omnigent": (
            "trio-omnigent-lead",
            "trio-omnigent-evaluator",
        ),
        "trio-productionize-omnigent": (
            "trio-omnigent-lead",
            "trio-omnigent-evaluator",
        ),
    }

    paths = (
        sorted(entrypoint_root.glob("*/SKILL.md"))
        if entrypoint_paths is None else entrypoint_paths
    )
    seen_names = set()
    for path in paths:
        name = path.parent.name
        if name in seen_names:
            continue
        seen_names.add(name)
        source.node("entrypoint", name, "omnigent", path)
        for target in dispatch_targets.get(name, ()):
            source.edge("dispatches_to", name, target)
            linked_targets.add(target)

    role_nodes = {
        node["name"]: node
        for node in _collect_omnigent(
            root, roles=roles, origin=origin)["nodes"]
        if node["kind"] == "agent"
    }
    for target in linked_targets:
        role = role_nodes.get(target)
        if role is None:
            continue
        source.node(
            "agent",
            role["name"],
            role["harness"],
            role["path"],
            role.get("model"),
            role.get("tool_policy"),
            role.get("output"),
            origin=role.get("origin", origin),
        )

    return _entrypoint_graph(source)


def _collect_entrypoints(root: Path, *, origin="workspace") -> dict:
    """Collect entrypoint-only graphs without role or productionize meshes."""
    graphs = {}
    source_collectors = (
        ("claude", ".claude", _collect_claude),
        ("cursor", ".cursor", _collect_cursor),
        ("codex", "codex", _collect_codex),
        ("omp", "omp", _collect_omp),
        ("opencode", "opencode", _collect_opencode),
        ("pi", "pi", _collect_pi),
    )
    for harness, directory, collector in source_collectors:
        if (root / directory).is_dir():
            graphs[harness] = _entrypoint_graph(
                collector(root, origin=origin))

    if (root / "kimi").is_dir():
        graphs["kimi"] = _entrypoint_graph(
            _markdown(
                root, "kimi", skills="kimi/skills", origin=origin))
    if (root / "zcode").is_dir():
        graphs["zcode"] = _entrypoint_graph(
            _markdown(
                root, "zcode", skills="zcode/skills", origin=origin))
    if (root / "omnigent").is_dir():
        graphs["omnigent"] = _collect_omnigent_entrypoints(
            root, origin=origin)
    if (root / "bridge").is_dir():
        graphs["bridge"] = _entrypoint_graph(
            _markdown(
                root,
                "bridge",
                commands="bridge/commands",
                skills="bridge/skills",
                origin=origin,
            ))
    return graphs


def _has_dir(root: Path, *relatives: str) -> bool:
    """Return whether any of the listed relative layout directories exists."""
    return any((root / relative).is_dir() for relative in relatives)


def _collect_installed_roles(home: Path) -> dict:
    """Collect role graphs from documented user-global harness locations."""
    graphs = {}
    collectors = (
        (
            "claude",
            (".claude/agents", ".claude/skills"),
            lambda: _collect_claude(home, origin="installed"),
        ),
        (
            "cursor",
            (".cursor/agents", ".cursor/skills", ".cursor/commands"),
            lambda: _collect_cursor(home, origin="installed"),
        ),
        (
            "codex",
            (".codex/agents", ".agents/skills"),
            lambda: _collect_codex(
                home, origin="installed", agents=".codex/agents",
                skills=".agents/skills"),
        ),
        (
            "omp",
            (".omp/agent/agents", ".omp/agent/commands"),
            lambda: _collect_omp(
                home, origin="installed", agents=".omp/agent/agents",
                commands=".omp/agent/commands"),
        ),
        (
            "opencode",
            (".config/opencode/agents", ".config/opencode/commands"),
            lambda: _collect_opencode(
                home, origin="installed",
                agents=".config/opencode/agents",
                commands=".config/opencode/commands"),
        ),
    )
    for harness, directories, collector in collectors:
        if _has_dir(home, *directories):
            graphs[harness] = collector()

    roles = home / ".omnigent" / "agents" / "trio-omnigent-roles"
    if roles.is_dir() or (roles / "registry.json").is_file():
        graphs["omnigent"] = _collect_omnigent(
            home, roles=roles, origin="installed")

    # Installed Kimi/Zcode surfaces are skill trees, not repo copies.
    for harness, relative in (
        ("kimi", ".kimi-code/skills"),
        ("zcode", ".zcode/skills"),
    ):
        if _has_dir(home, relative):
            graphs[harness] = _markdown(
                home, harness, skills=relative,
                origin="installed").result()

    # Pi has one documented global extension location; do not guess another.
    extension = home / ".pi" / "agent" / "extensions" / "trio.ts"
    if extension.is_file():
        graphs["pi"] = _collect_pi(
            home, extension=extension, origin="installed")
    return graphs


def _collect_installed_entrypoint_paths(home: Path) -> list[Path]:
    """Choose one global copy of each Omnigent entrypoint name."""
    paths = []
    for name in ("trio-omnigent", "trio-productionize-omnigent"):
        candidates = (
            home / ".claude" / "skills" / name / "SKILL.md",
            home / ".agents" / "skills" / name / "SKILL.md",
        )
        for path in candidates:
            if path.is_file():
                paths.append(path)
                break
    return paths


def _collect_installed_entrypoints(home: Path) -> dict:
    """Collect entrypoint graphs from documented user-global locations."""
    graphs = {}
    collectors = (
        (
            "claude",
            (".claude/agents", ".claude/skills"),
            lambda: _entrypoint_graph(
                _collect_claude(home, origin="installed")),
        ),
        (
            "cursor",
            (".cursor/agents", ".cursor/skills", ".cursor/commands"),
            lambda: _entrypoint_graph(
                _collect_cursor(home, origin="installed")),
        ),
        (
            "codex",
            (".codex/agents", ".agents/skills"),
            lambda: _entrypoint_graph(
                _collect_codex(
                    home, origin="installed", agents=".codex/agents",
                    skills=".agents/skills")),
        ),
        (
            "omp",
            (".omp/agent/agents", ".omp/agent/commands"),
            lambda: _entrypoint_graph(
                _collect_omp(
                    home, origin="installed", agents=".omp/agent/agents",
                    commands=".omp/agent/commands")),
        ),
        (
            "opencode",
            (".config/opencode/agents", ".config/opencode/commands"),
            lambda: _entrypoint_graph(
                _collect_opencode(
                    home, origin="installed",
                    agents=".config/opencode/agents",
                    commands=".config/opencode/commands")),
        ),
    )
    for harness, directories, collector in collectors:
        if _has_dir(home, *directories):
            graphs[harness] = collector()

    for harness, relative in (
        ("kimi", ".kimi-code/skills"),
        ("zcode", ".zcode/skills"),
    ):
        if _has_dir(home, relative):
            graphs[harness] = _entrypoint_graph(
                _markdown(
                    home, harness, skills=relative, origin="installed"))

    extension = home / ".pi" / "agent" / "extensions" / "trio.ts"
    if extension.is_file():
        graphs["pi"] = _entrypoint_graph(
            _collect_pi(home, extension=extension, origin="installed"))

    roles = home / ".omnigent" / "agents" / "trio-omnigent-roles"
    omnigent_paths = _collect_installed_entrypoint_paths(home)
    if omnigent_paths or roles.is_dir() or (roles / "registry.json").is_file():
        graphs["omnigent"] = _collect_omnigent_entrypoints(
            home, origin="installed", entrypoint_paths=omnigent_paths,
            roles=roles)
    return graphs


def _collect_roles(root: Path, *, origin="workspace") -> dict:
    """Collect the repository-style role graphs for one source root."""
    collectors = (
        ("claude", ".claude", _collect_claude),
        ("cursor", ".cursor", _collect_cursor),
        ("codex", "codex", _collect_codex),
        ("omp", "omp", _collect_omp),
        ("opencode", "opencode", _collect_opencode),
        ("omnigent", "omnigent", _collect_omnigent),
        ("pi", "pi", _collect_pi),
    )
    return {
        name: collector(root, origin=origin)
        for name, directory, collector in collectors
        if (root / directory).is_dir()
    }


def _merge_graphs(*sources: dict | None) -> dict:
    """Union graph edges while retaining the first node for each graph key."""
    graph = _Graph()
    for source in sources:
        if not source:
            continue
        for node in source.get("nodes", ()):
            graph.node(
                node["kind"], node["name"], node["harness"], node["path"],
                node.get("model"), node.get("tool_policy"),
                node.get("output"),
                origin=node.get("origin", "workspace"),
            )
        for edge in source.get("edges", ()):
            graph.edge(edge["type"], edge["src"], edge["dst"])
    return graph.result()


def _merge_graph_maps(workspace: dict, installed: dict) -> dict:
    """Merge same-harness graphs and retain harnesses from either source."""
    names = sorted(set(workspace) | set(installed))
    return {
        name: _merge_graphs(workspace.get(name), installed.get(name))
        for name in names
    }


def collect_topology(root: Path, *, home: Path | None = None,
                     workflow: str = "roles") -> dict:
    """Scan workspace graphs and optional installations from explicit paths."""
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown topology workflow: {workflow}")
    root = Path(root)
    home_path = Path(home) if home is not None else None
    installed = {}
    if home_path is not None and home_path.is_dir():
        # Workspace wins duplicate keys; Path.home() is forbidden because
        # requests must never scan an implicit user's home.
        # The explicit home is the only source allowed for installed graphs.
        if workflow == "productionize":
            installed = _collect_productionize(
                home_path, origin="installed",
                wrappers=INSTALLED_PRODUCTIONIZE_WRAPPERS)
        elif workflow == "entrypoints":
            installed = _collect_installed_entrypoints(home_path)
        else:
            installed = _collect_installed_roles(home_path)

    if workflow == "productionize":
        workspace = _collect_productionize(root)
    elif workflow == "entrypoints":
        workspace = _collect_entrypoints(root)
    else:
        workspace = _collect_roles(root)
    graphs = _merge_graph_maps(workspace, installed)
    return {"root": str(root), "workflow": workflow, "graphs": graphs}

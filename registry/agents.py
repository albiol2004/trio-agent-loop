#!/usr/bin/env python3
"""Canonical agent model - harness-neutral agent definitions.

A *canonical agent* is one harness-neutral definition (name, description,
instructions, a model tier, a tool policy) stored as a single Markdown file
with YAML frontmatter under ``registry/canonical-agents/``. It can be
rendered into each supported harness's native agent-file format via
``render_agent``.

This module builds on the frozen format layer in ``registry/scan.py``
(``parse_frontmatter``/``dump_frontmatter``/``parse_toml``/``dump_toml``/
``split_file``/``join_file``) rather than hand-writing serialization. It does
not import or modify ``scan.py``; a sibling module loads it by path (see
``registry/tests/test_agents.py`` for the convention).

Stdlib only. Requires Python 3.11+ (tomllib, via scan.py).
"""

from __future__ import annotations

import copy
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "trio_registry_agents_scan", REPO / "registry" / "scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scan()

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# --------------------------------------------------------------------------
# Model tiers / tool policies - the harness-native field tables
# --------------------------------------------------------------------------

MODEL_TIERS: dict[str, dict[str, dict]] = {
    "high": {
        "claude": {"model": "claude-opus-5", "effort": "high"},
        "codex": {"model": "gpt-5.6-terra", "model_reasoning_effort": "high"},
        "omp": {"model": "cursor/cursor-grok-4.6-high"},
        "opencode": {},
    },
    "standard": {
        "claude": {"model": "sonnet", "effort": "high"},
        "codex": {"model": "gpt-5.6-luna", "model_reasoning_effort": "high"},
        "omp": {"model": "cursor/cursor-grok-4.6-medium"},
        "opencode": {},
    },
    "cheap": {
        "claude": {"model": "haiku"},
        "codex": {"model": "gpt-5.6-luna", "model_reasoning_effort": "low"},
        "omp": {"model": "deepseek/deepseek-v4-flash"},
        "opencode": {},
    },
}

TOOL_POLICIES: dict[str, dict[str, dict]] = {
    "read-only": {
        "claude": {"disallowedTools": "Write, Edit, NotebookEdit, Agent"},
        "codex": {"sandbox_mode": "read-only"},
        "omp": {"tools": "read, grep, glob, web_search", "read-summarize": False},
        "opencode": {"permission": {
            "*": "deny", "read": "allow", "grep": "allow", "glob": "allow",
            "webfetch": "allow", "edit": "deny", "bash": "deny", "task": "deny",
        }},
    },
    "edit": {
        "claude": {"disallowedTools": "Agent"},
        "codex": {},
        "omp": {},
        "opencode": {"permission": {"task": "deny"}},
    },
    "spawn": {
        "claude": {},
        "codex": {},
        "omp": {},
        # Named targets are added by the OpenCode renderer below.
        "opencode": {"permission": {"task": {"*": "deny"}}},
    },
}

HARNESS_SUPPORT: dict[str, tuple[bool, str]] = {
    "claude": (True, ""),
    "codex": (True, ""),
    "omp": (True, ""),
    "opencode": (True, ""),
    "omnigent": (False,
                 "omnigent agents are role directories with a config.yaml "
                 "(spec_version/executor/os_env/guardrails/prompt) rather than "
                 "a frontmatter agent file; they are managed by "
                 "prompts/generate.py and cannot be rendered from a canonical "
                 "agent."),
}

RENDER_HARNESSES: tuple[str, ...] = ("claude", "codex", "omp", "opencode")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


def _validate(name: str, description, model_tier, tool_policy) -> None:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(
            f"invalid agent name {name!r}: must match {NAME_RE.pattern!r}")
    if not isinstance(description, str) or description == "":
        raise ValueError(
            f"description must be a non-empty string, got {description!r}")
    if model_tier not in MODEL_TIERS:
        raise ValueError(
            f"unknown model_tier {model_tier!r}; must be one of "
            f"{sorted(MODEL_TIERS)}")
    if tool_policy not in TOOL_POLICIES:
        raise ValueError(
            f"unknown tool_policy {tool_policy!r}; must be one of "
            f"{sorted(TOOL_POLICIES)}")


def _validate_spawns(spawns: list[str]) -> None:
    """Validate the named agents allowed by a canonical agent."""
    if not isinstance(spawns, list):
        raise ValueError(f"spawns must be a list, got {spawns!r}")

    seen = set()
    for spawn in spawns:
        if not isinstance(spawn, str) or not NAME_RE.match(spawn):
            raise ValueError(
                f"invalid spawn name {spawn!r}: must match {NAME_RE.pattern!r}")
        if spawn in seen:
            raise ValueError(f"duplicate spawn name {spawn!r}")
        seen.add(spawn)


@dataclass
class CanonicalAgent:
    name: str
    description: str
    instructions: str
    model_tier: str
    tool_policy: str
    spawns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate(self.name, self.description, self.model_tier, self.tool_policy)
        _validate_spawns(self.spawns)


@dataclass
class RenderedAgent:
    harness: str
    filename: str
    format: str  # "yaml" | "toml"
    text: str


class UnsupportedHarness(Exception):
    def __init__(self, harness: str, reason: str):
        super().__init__(f"{harness}: {reason}")
        self.harness = harness
        self.reason = reason


# --------------------------------------------------------------------------
# Storage (parse / dump / CRUD)
# --------------------------------------------------------------------------


def agents_dir(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return REPO / "registry" / "canonical-agents"


def parse_agent(text: str, *, name: str | None = None) -> CanonicalAgent:
    """Parse a canonical agent file: YAML frontmatter + Markdown body.

    The body IS the instructions, verbatim. ``name`` (typically the file
    stem) is used when the frontmatter has no ``name`` key; if both are
    present and disagree, raises ``ValueError``.
    """
    fields, body = scan.parse_frontmatter(text)
    fm_name = fields.get("name")
    if fm_name is not None and name is not None and fm_name != name:
        raise ValueError(
            f"agent name mismatch: frontmatter name={fm_name!r} vs "
            f"file stem={name!r}")
    resolved_name = fm_name if fm_name is not None else name
    if resolved_name is None:
        raise ValueError(
            "agent has no name: frontmatter has no 'name' key and no "
            "name argument was given")

    raw_spawns = fields.get("spawns", [])
    if isinstance(raw_spawns, str):
        # OMP stores this field as one comma-separated scalar.
        raw_spawns = (
            [] if not raw_spawns.strip()
            else [item.strip() for item in raw_spawns.split(",")]
        )
    elif isinstance(raw_spawns, list):
        raw_spawns = list(raw_spawns)
    else:
        raise ValueError(
            f"spawns must be a YAML list or comma-separated string, "
            f"got {raw_spawns!r}")

    return CanonicalAgent(
        name=resolved_name,
        description=fields.get("description"),
        instructions=body,
        model_tier=fields.get("model_tier"),
        tool_policy=fields.get("tool_policy"),
        spawns=raw_spawns,
    )


def dump_agent(agent: CanonicalAgent) -> str:
    """Inverse of :func:`parse_agent`: frontmatter + body (= instructions)."""
    fields = {
        "name": agent.name,
        "description": agent.description,
        "model_tier": agent.model_tier,
        "tool_policy": agent.tool_policy,
    }
    # Empty spawns stay implicit so existing seed files keep their shape.
    if agent.spawns:
        fields["spawns"] = list(agent.spawns)
    return scan.join_file(fields, agent.instructions, "yaml")


def list_agents(root: Path | None = None) -> list[CanonicalAgent]:
    d = agents_dir(root)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        if f.is_file():
            out.append(parse_agent(f.read_text(encoding="utf-8"), name=f.stem))
    return out


def load_agent(name: str, root: Path | None = None) -> CanonicalAgent:
    path = agents_dir(root) / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    return parse_agent(text, name=name)


def save_agent(agent: CanonicalAgent, root: Path | None = None) -> Path:
    d = agents_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{agent.name}.md"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dump_agent(agent), encoding="utf-8")
    tmp.replace(path)
    return path


def delete_agent(name: str, root: Path | None = None) -> bool:
    path = agents_dir(root) / f"{name}.md"
    if not path.is_file():
        return False
    path.unlink()
    return True


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _tier_fields(harness: str, model_tier: str) -> dict:
    return copy.deepcopy(MODEL_TIERS[model_tier][harness])


def _policy_fields(harness: str, tool_policy: str) -> dict:
    return copy.deepcopy(TOOL_POLICIES[tool_policy][harness])


def _render_claude(agent: CanonicalAgent) -> RenderedAgent:
    fields = {"name": agent.name, "description": agent.description}
    fields.update(_tier_fields("claude", agent.model_tier))
    fields.update(_policy_fields("claude", agent.tool_policy))
    text = scan.join_file(fields, agent.instructions, "yaml")
    return RenderedAgent("claude", f"{agent.name}.md", "yaml", text)


def _render_codex(agent: CanonicalAgent) -> RenderedAgent:
    fields = {"name": agent.name}
    fields.update(_tier_fields("codex", agent.model_tier))
    fields.update(_policy_fields("codex", agent.tool_policy))
    fields["description"] = agent.description
    instructions = agent.instructions
    if agent.tool_policy == "spawn" and agent.spawns:
        # Codex has no native per-agent spawn field, so explain delegation in
        # the developer instructions while preserving the original body first.
        note = (
            f"Delegation: may spawn {', '.join(agent.spawns)} "
            "via the task/spawn tool.\n"
        )
        instructions += f"\n\n{note}"
    text = scan.join_file(fields, instructions, "toml")
    return RenderedAgent("codex", f"{agent.name}.toml", "toml", text)


def _render_omp(agent: CanonicalAgent) -> RenderedAgent:
    fields = {"name": agent.name, "description": agent.description}
    fields.update(_tier_fields("omp", agent.model_tier))
    fields.update(_policy_fields("omp", agent.tool_policy))
    if agent.tool_policy == "spawn" and agent.spawns:
        # OMP expects one comma-separated scalar; an empty key is invalid.
        fields["spawns"] = ", ".join(agent.spawns)
    text = scan.join_file(fields, agent.instructions, "yaml")
    return RenderedAgent("omp", f"{agent.name}.md", "yaml", text)


def _render_opencode(agent: CanonicalAgent) -> RenderedAgent:
    fields = {"description": agent.description, "mode": "subagent", "hidden": True}
    fields.update(_tier_fields("opencode", agent.model_tier))
    fields.update(_policy_fields("opencode", agent.tool_policy))
    if agent.tool_policy == "spawn":
        # OpenCode uses task patterns, so deny every task before allowing the
        # explicit named targets. This also gives empty spawn lists safe output.
        task_permission = fields["permission"]["task"]
        for spawn in agent.spawns:
            task_permission[spawn] = "allow"
    text = scan.join_file(fields, agent.instructions, "yaml")
    return RenderedAgent("opencode", f"{agent.name}.md", "yaml", text)


_RENDERERS = {
    "claude": _render_claude,
    "codex": _render_codex,
    "omp": _render_omp,
    "opencode": _render_opencode,
}


def render_agent(agent: CanonicalAgent, harness: str) -> RenderedAgent:
    renderer = _RENDERERS.get(harness)
    if renderer is None:
        supported, reason = HARNESS_SUPPORT.get(
            harness, (False, f"{harness} is not a supported install target"))
        raise UnsupportedHarness(harness, reason)
    return renderer(agent)


def install_support(harness: str) -> tuple[bool, str]:
    if harness in HARNESS_SUPPORT:
        return HARNESS_SUPPORT[harness]
    return False, f"{harness} is not a supported install target"


# --------------------------------------------------------------------------
# Index records (feeds a later slice's install matrix)
# --------------------------------------------------------------------------


def agent_index_records(root: Path | None = None) -> list[dict]:
    d = agents_dir(root)
    unsupported = {h: reason for h, (supported, reason) in HARNESS_SUPPORT.items()
                   if not supported}
    records = []
    for agent in list_agents(root):
        renders = {}
        for harness in RENDER_HARNESSES:
            rendered = render_agent(agent, harness)
            fmt = rendered.format
            fm_fields, fm_body = scan.split_file(rendered.text, fmt)
            if fmt == "toml":
                body_hash = scan.sha256(rendered.text.strip())
            else:
                body_hash = scan.sha256(fm_body.strip())
            renders[harness] = {
                "filename": rendered.filename,
                "format": fmt,
                "body_hash": body_hash,
                "frontmatter": fm_fields,
            }
        records.append({
            "name": agent.name,
            "path": str(d / f"{agent.name}.md"),
            "surface": "canonical-agent",
            "model_tier": agent.model_tier,
            "tool_policy": agent.tool_policy,
            "spawns": list(agent.spawns),
            "description": agent.description,
            "renders": renders,
            "unsupported": unsupported,
        })
    return records

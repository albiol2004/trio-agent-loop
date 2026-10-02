"""Generate the per-run, isolated OpenCode config directory: ``opencode.json``
plus one agent file per Trio role, with permissions that never say "ask"
(non-interactive ``opencode run`` cannot answer a permission prompt — see
SPEC.md "Hard constraints" and "OpenCode 1.18.33 facts").

Agent bodies for lead/evaluator/builder/repair are taken from this repo's
own ``opencode-driver/agents/trio-<role>.md`` files -- rendered by
``prompts/generate.py`` from the canonical ``prompts/overlays/opencode-driver.md``
overlay for the standalone driver, distinct from the in-OpenCode plugin's
``opencode/agents/trio-<role>.md`` bodies. Scout has no canonical body of
its own (it is never generated) and keeps coming from
``opencode/agents/trio-scout.md``. Every body's frontmatter is stripped --
the frontmatter we write here is the one that actually governs the
generated, driver-owned agent -- and prefixed with a short note that the
body's own orchestration/delegation instructions are superseded by the
driver's per-call prompt (a generated agent has no builder subagents of its
own; builders are created and dispatched by ``driver.py``, never by the
agent calling ``task``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import config as _config_mod

Config = _config_mod.Config

ROLES: tuple[str, ...] = ("lead", "evaluator", "builder", "repair", "scout", "acceptance")

#: Providers wired into ``opencode`` itself; no config-side "provider" block
#: is needed to use them (SPEC.md: "Auth: OPENCODE_API_KEY env for providers
#: opencode and opencode-go.").
_BUILTIN_PROVIDERS = ("opencode", "opencode-go")

_DRIVER_NOTE = (
    "You are run by the trio-opencode driver via `opencode run`; follow the "
    "per-call instructions in the user message; they override any "
    "orchestration/delegation instruction in this body (you have no builder "
    "subagents; builders are driver-owned)."
)


class OcgenError(RuntimeError):
    """Raised when a role's source agent body cannot be turned into a
    generated agent file (missing file, empty/frontmatter-only body)."""


# --------------------------------------------------------------------------
# Permissions (no "ask" anywhere; last matching pattern wins, so "*" is
# always written first and specific denies/allows follow it).
# --------------------------------------------------------------------------

#: Bash patterns denied for every non-scout role, regardless of the leading
#: "*": "allow" (SPEC.md "Generated OpenCode config" bash deny list). These
#: are guard rails, not a security boundary: a determined agent can still
#: find other destructive commands bash allows.
_BASH_DENY_PATTERNS: tuple[str, ...] = (
    "git push*",
    "git push",
    "git * --force*",
    "git * -f",
    "git reset --hard*",
    "git clean -*x*",
    "git worktree remove*",
    "git branch -D*",
    "git branch -d*",
    "git update-ref*",
    "git config --global*",
    "git checkout -f*",
    "rm -rf /*",
    "rm -rf ~*",
    "rm -rf ..*",
    "rm -rf $HOME*",
    "sudo *",
    "curl *|*sh*",
    "wget *|*sh*",
    "opencode*",
    "chmod -R 777*",
)

#: ``container_mode`` variant of the same deny list (README.md "Container /
#: no-time-limit mode"): inside a Terminal-Bench task container the whole
#: container IS the product, so the blanket ``rm -rf /*`` glob would also
#: deny perfectly ordinary in-container cleanup like ``rm -rf /app/build``.
#: It is replaced with exact-ish root-wipe patterns (``rm -rf /``,
#: ``rm -rf / *``) while ``~*``/``..*``/``$HOME*`` stay denied unchanged.
#: Also adds remote/network-destructive denies that matter once the git
#: history itself has no "it's just this container" backstop:
#: ``git remote add*``/``set-url*``, ``scp``/``rsync``/``ssh``/``nc``/``ncat``.
_CONTAINER_BASH_DENY_PATTERNS: tuple[str, ...] = (
    "git push*",
    "git push",
    "git * --force*",
    "git * -f",
    "git reset --hard*",
    "git clean -*x*",
    "git worktree remove*",
    "git branch -D*",
    "git branch -d*",
    "git update-ref*",
    "git config --global*",
    "git checkout -f*",
    "rm -rf /",
    "rm -rf / *",
    "rm -rf ~*",
    "rm -rf ..*",
    "rm -rf $HOME*",
    "sudo *",
    "curl *|*sh*",
    "wget *|*sh*",
    "opencode*",
    "chmod -R 777*",
    "git remote add*",
    "git remote set-url*",
    "scp *",
    "rsync *:*",
    "ssh *",
    "nc *",
    "ncat *",
)

#: Scout's bash permission is the inverse shape: deny by default, allow only
#: a short list of read-only inspection commands.
_SCOUT_BASH: dict[str, str] = {
    "*": "deny",
    "git log*": "allow",
    "git show*": "allow",
    "git diff*": "allow",
    "git status*": "allow",
    "ls*": "allow",
    "cat *": "allow",
    "grep *": "allow",
    "rg *": "allow",
    "find *": "allow",
    "head *": "allow",
    "tail *": "allow",
    "wc *": "allow",
}


def _bash_map(*, extra_allow: tuple[str, ...] = (), container_mode: bool = False) -> dict[str, str]:
    m: dict[str, str] = {"*": "allow"}
    for pattern in (_CONTAINER_BASH_DENY_PATTERNS if container_mode else _BASH_DENY_PATTERNS):
        m[pattern] = "deny"
    for pattern in extra_allow:
        m[pattern] = "allow"
    return m


def _bash_for_role(role: str, *, container_mode: bool = False) -> dict[str, str]:
    """The bash permission map for ``role``, optionally in ``container_mode``
    (scout's deny-by-default/allow-read-only shape never changes -- it has
    no destructive bash to begin with)."""
    if role == "scout":
        return dict(_SCOUT_BASH)
    extra_allow = ("git worktree add --detach*",) if role == "evaluator" else ()
    return _bash_map(extra_allow=extra_allow, container_mode=container_mode)


def _common_permission(*, edit: str, webfetch: str, websearch: str, bash: dict[str, str], task: Any) -> dict[str, Any]:
    return {
        "external_directory": "deny",
        "doom_loop": "deny",
        "question": "deny",
        "webfetch": webfetch,
        "websearch": websearch,
        "read": {"*": "allow"},
        "edit": edit,
        "bash": bash,
        "task": task,
        "todowrite": "allow",
    }


_LEAD_EVALUATOR_TASK = {"*": "deny", "trio-scout": "allow"}

#: Per-role permission blocks. Lead/evaluator may read/edit/bash (with the
#: shared deny list) and may call only the ``trio-scout`` subagent; scout is
#: read-only; builder/repair have no ``task`` (no subagents of their own —
#: builders are driver-owned). Evaluator additionally may run
#: ``git worktree add --detach`` (its independent read-only checkout).
PERMISSIONS: dict[str, dict[str, Any]] = {
    "lead": _common_permission(
        edit="allow", webfetch="allow", websearch="allow",
        bash=_bash_for_role("lead"), task=_LEAD_EVALUATOR_TASK,
    ),
    "evaluator": _common_permission(
        edit="allow", webfetch="allow", websearch="allow",
        bash=_bash_for_role("evaluator"),
        task=_LEAD_EVALUATOR_TASK,
    ),
    "builder": _common_permission(
        edit="allow", webfetch="deny", websearch="deny",
        bash=_bash_for_role("builder"), task="deny",
    ),
    "repair": _common_permission(
        edit="allow", webfetch="deny", websearch="deny",
        bash=_bash_for_role("repair"), task="deny",
    ),
    "scout": _common_permission(
        edit="deny", webfetch="deny", websearch="deny",
        bash=_bash_for_role("scout"), task="deny",
    ),
    # r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): the author agent.
    # Edit allow (writes acceptance/ + scratch in its own export cwd), bash
    # "*" allow plus the standard deny list, no subagents, no web (native
    # disallows Agent/WebFetch/WebSearch for the author too).
    "acceptance": _common_permission(
        edit="allow", webfetch="deny", websearch="deny",
        bash=_bash_for_role("acceptance"), task="deny",
    ),
}


# --------------------------------------------------------------------------
# YAML frontmatter rendering (hand-written; no PyYAML dependency).
# --------------------------------------------------------------------------

#: Characters that force quoting a YAML scalar (key or value) per SPEC.md:
#: "*", ":", "|", "$", "~", and any whitespace.
_NEEDS_QUOTE_CHARS = re.compile(r"[*:|$~\s]")
#: Leading characters that are YAML-significant even alone (block/flow
#: indicators, comments, anchors, tags, quotes).
_LEADING_SPECIAL = set("-?:,[]{}#&*!|>'\"%@`")


def _needs_quote(s: str) -> bool:
    if s == "":
        return True
    if s[0] in _LEADING_SPECIAL:
        return True
    if _NEEDS_QUOTE_CHARS.search(s):
        return True
    return False


def _yaml_scalar(s: str) -> str:
    """Render ``s`` as a YAML scalar: bare when safe, else a double-quoted
    (JSON-escaped, which YAML double-quoting is a superset of) string."""
    if _needs_quote(s):
        return json.dumps(s)
    return s


def _dump_block(d: dict[str, Any], indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    for key, value in d.items():
        key_str = _yaml_scalar(str(key))
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key_str}: {{}}")
            else:
                lines.append(f"{pad}{key_str}:")
                _dump_block(value, indent + 1, lines)
        elif value is None:
            lines.append(f"{pad}{key_str}: null")
        elif isinstance(value, bool):
            lines.append(f"{pad}{key_str}: {'true' if value else 'false'}")
        else:
            lines.append(f"{pad}{key_str}: {_yaml_scalar(str(value))}")


def render_frontmatter(fm: dict[str, Any]) -> str:
    """Render ``fm`` (an ordered mapping) as a ``---``-delimited YAML
    frontmatter block. Key order is exactly the input dict's insertion
    order (deterministic); every key/value that needs quoting (SPEC.md:
    contains ``*``, ``:``, ``|``, ``$``, ``~``, whitespace, or starts with a
    YAML-significant character) is rendered as a JSON-escaped double-quoted
    string, which is also a valid YAML double-quoted scalar. Never emits
    the literal value "ask"; that value is simply never present in
    ``PERMISSIONS``.
    """
    lines: list[str] = ["---"]
    _dump_block(fm, 0, lines)
    lines.append("---")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Agent body extraction (strip the repo's own frontmatter; keep the body).
# --------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a ``---``-delimited-frontmatter Markdown file into
    ``(frontmatter_text, body_text)``. If there is no leading frontmatter
    block, ``frontmatter_text`` is ``""`` and ``body_text`` is the whole
    input."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    return "", text


_DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _frontmatter_description(fm_text: str) -> str:
    m = _DESCRIPTION_RE.search(fm_text)
    if not m:
        return ""
    val = m.group(1)
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return val


def _load_role_body(repo_root: Path, role: str) -> tuple[str, str]:
    """Return ``(description, body)`` for ``role``, read from that role's
    source agent body with its own frontmatter stripped. Scout (no
    canonical body of its own; never generated) is read from
    ``<repo_root>/opencode/agents/trio-scout.md`` -- the in-OpenCode
    plugin's shared scout body. lead/evaluator/builder/repair/acceptance
    are read from ``<repo_root>/opencode-driver/agents/trio-<role>.md``,
    generated for this standalone driver by ``prompts/generate.py`` (the
    ``prompts/overlays/opencode-driver.md`` overlay; the acceptance author
    from ``prompts/canonical/acceptance.md``)."""
    agents_dir = "opencode" if role == "scout" else "opencode-driver"
    source = repo_root / agents_dir / "agents" / f"trio-{role}.md"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise OcgenError(f"cannot read source agent body {source}: {exc}") from exc
    fm_text, body = _split_frontmatter(text)
    body = body.strip("\n")
    if not body:
        raise OcgenError(f"source agent body for role {role!r} is empty ({source})")
    description = _frontmatter_description(fm_text) or f"Trio {role} (trio-opencode)."
    return description, body


#: REVIEW-driver.md item 16: builders (and repair/lead/evaluator, which may
#: also need to write scratch files there) run with ``TMPDIR`` pointed at
#: ``<repo>/.trio-opencode/worktrees/tmp-<exec id>`` (begin's scratch dir,
#: outside every role's own working directory), so a flat
#: ``external_directory: deny`` would reject any tool call that touches it.
#: Scout never gets a working-directory-relative TMPDIR override (it has no
#: builder/lead/evaluator worktree of its own to write scratch files from),
#: so it keeps the flat deny. Last matching pattern wins, so the specific
#: allow entries must follow the "*" deny.
def _external_directory_for(role: str, repo_root: "Path | None", *,
                            container_mode: bool = False) -> Any:
    #: ``container_mode``: the whole container IS the product (no host
    #: filesystem to protect), so every role -- including scout, whose
    #: edit permission stays "deny" regardless -- gets a flat "allow" here
    #: purely so an opencode tool call that needs to read outside the
    #: project is never blocked.
    if container_mode:
        return "allow"
    if role == "scout" or repo_root is None:
        return "deny"
    base = f"{Path(repo_root).resolve()}/.trio-opencode/worktrees/tmp-"
    return {"*": "deny", f"{base}*": "allow", f"{base}*/**": "allow"}


def _agent_frontmatter(role: str, cfg: Config, description: str,
                       repo_root: "Path | None" = None) -> dict[str, Any]:
    fm: dict[str, Any] = {"description": description}
    if role == "scout":
        fm["mode"] = "subagent"
        fm["hidden"] = True
    else:
        fm["mode"] = "primary"
    fm["model"] = cfg.model_for(role)
    # r19: variants.acceptance is informational only (README.md) -- the
    # Lead's variant is what the author turn is actually dispatched with.
    variant = cfg.variant_for("lead" if role == "acceptance" else role)
    if variant:
        fm["variant"] = variant
    container_mode = bool(getattr(cfg, "container_mode", False))
    permission = dict(PERMISSIONS[role])
    if container_mode:
        permission["bash"] = _bash_for_role(role, container_mode=True)
    permission["external_directory"] = _external_directory_for(role, repo_root, container_mode=container_mode)
    fm["permission"] = permission
    return fm


def _render_agent_file(role: str, cfg: Config, repo_root: Path) -> str:
    description, body = _load_role_body(repo_root, role)
    frontmatter = _agent_frontmatter(role, cfg, description, repo_root)
    return render_frontmatter(frontmatter) + "\n" + _DRIVER_NOTE + "\n\n" + body + "\n"


# --------------------------------------------------------------------------
# opencode.json — v1 (agent bodies live in separate agent/*.md files)
# --------------------------------------------------------------------------

def _build_opencode_json(cfg: Config) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "agent": {
            "build": {"disable": True},
            "plan": {"disable": True},
            "general": {"disable": True},
            "explore": {"disable": True},
        },
        "model": cfg.model_for("lead"),
    }
    if cfg.provider.id not in _BUILTIN_PROVIDERS:
        # A non-built-in provider id still needs no apiKey here (the key is
        # only ever passed via the OPENCODE_API_KEY child env at spawn
        # time) but must be registered so `opencode` knows the id exists.
        doc["provider"] = {cfg.provider.id: {}}
    return doc


# --------------------------------------------------------------------------
# opencode.json — v2 (agents defined INLINE, no agent/*.md files; SPEC.md
# "OpenCode v2.0.20 facts": binary schema is a flat per-action ``permission``
# map and no ``variant`` key — the variant goes on the runner's ``-m`` flag
# at spawn time instead).
# --------------------------------------------------------------------------

def _agent_config_entry(role: str, cfg: Config, description: str, body: str,
                        repo_root: "Path | None") -> dict[str, Any]:
    entry: dict[str, Any] = {"description": description}
    if role == "scout":
        entry["mode"] = "subagent"
        entry["hidden"] = True
    else:
        entry["mode"] = "primary"
    entry["model"] = cfg.model_for(role)
    # r19: variants.acceptance is informational only (README.md) -- the
    # Lead's variant is what the author turn is actually dispatched with.
    variant = cfg.variant_for("lead" if role == "acceptance" else role)
    if variant:
        # v2 agent schema: "Default model variant for this agent" -- covers
        # the scout, which is spawned via the task tool, not via -m.
        entry["variant"] = variant
    container_mode = bool(getattr(cfg, "container_mode", False))
    permission = dict(PERMISSIONS[role])
    if container_mode:
        permission["bash"] = _bash_for_role(role, container_mode=True)
    permission["external_directory"] = _external_directory_for(role, repo_root, container_mode=container_mode)
    entry["permission"] = permission
    entry["prompt"] = _DRIVER_NOTE + "\n\n" + body
    return entry


def _build_opencode_json_v2(cfg: Config, repo_root: Path) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": cfg.model_for("lead"),
        "agent": {
            "build": {"disable": True},
            "plan": {"disable": True},
            "general": {"disable": True},
            "explore": {"disable": True},
        },
    }
    for role in ROLES:
        description, body = _load_role_body(repo_root, role)
        doc["agent"][f"trio-{role}"] = _agent_config_entry(role, cfg, description, body, repo_root)
    if cfg.provider.id not in _BUILTIN_PROVIDERS:
        doc["provider"] = {cfg.provider.id: {}}
    return doc


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def generate(
    run_dir: Path,
    cfg: Config,
    repo_root: Path,
    mailbox: Path,
    *,
    cache_dir: Path | None = None,
    style: str = "v2",
) -> dict[str, str]:
    """Write ``<run_dir>/opencode/opencode.json`` (plus, for ``style="v1"``,
    one generated agent file per role under ``<run_dir>/opencode/agent/`` —
    v2's binary schema defines agents INLINE in ``opencode.json`` itself, so
    no ``.md`` files are written for it), create this run's isolated XDG
    directories, and return the environment every ``opencode run`` turn for
    this loop must be launched with. ``style`` is the CLI style
    :func:`trio_opencode.runner.detect_cli` reported for the binary this run
    will use ("v2" is the primary target; "v1" is the 1.18.33 compatibility
    path) — callers that have not run detection yet may omit it and get v2.

    ``mailbox`` is accepted (and must exist as a directory the driver will
    pass as ``--dir`` for the Lead/Evaluator turns under v1) for interface
    parity with the driver's per-loop layout; today no role needs an
    ``external_directory`` allowance beyond its own working directory, so
    it does not otherwise change what is generated. ``cache_dir`` lets
    callers share one OpenCode models-catalog cache across loops (SPEC.md:
    "may share a per-user cache dir ... to avoid refetch"); when omitted,
    each run gets its own ``xdg/cache``.

    Never includes the API key: the key is read and passed only by the
    runner, at spawn time, directly into the child process environment.
    """
    run_dir = Path(run_dir)
    repo_root = Path(repo_root)
    mailbox = Path(mailbox)
    is_v1 = style == "v1"

    oc_dir = run_dir / "opencode"
    oc_dir.mkdir(parents=True, exist_ok=True)

    if is_v1:
        agent_dir = oc_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        for role in ROLES:
            content = _render_agent_file(role, cfg, repo_root)
            (agent_dir / f"trio-{role}.md").write_text(content, encoding="utf-8")
        doc = _build_opencode_json(cfg)
    else:
        doc = _build_opencode_json_v2(cfg, repo_root)

    opencode_json_path = oc_dir / "opencode.json"
    opencode_json_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    xdg_root = run_dir / "xdg"
    xdg_config = xdg_root / "config"
    xdg_data = xdg_root / "data"
    xdg_state = xdg_root / "state"
    xdg_cache = Path(cache_dir) if cache_dir is not None else xdg_root / "cache"
    for d in (xdg_config, xdg_data, xdg_state, xdg_cache):
        d.mkdir(parents=True, exist_ok=True)

    env = {
        "OPENCODE_CONFIG": str(opencode_json_path),
        "OPENCODE_CONFIG_DIR": str(oc_dir),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
    }
    if is_v1:
        env["OPENCODE_DISABLE_SHARE"] = "1"
    else:
        # v2 disables sharing via the generated config's own
        # `"share": "disabled"` instead of an env flag (SPEC.md).
        env["OPENCODE_CONFIG_PROJECT_DISABLE"] = "1"
    return env

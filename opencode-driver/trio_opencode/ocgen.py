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
import os
import re
from dataclasses import dataclass
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
# Acceptance-author isolation (r19). The author must judge the product from
# its export alone; a goal that names absolute repository paths makes a model
# naturally go and look there. Detect-and-discard (the audit) can only discard
# after the fact -- and discarded every attempt of a real run.
# These rules PREVENT the reads instead: deny rules only, never "ask" and
# never `--auto`.
# --------------------------------------------------------------------------

#: The author's two isolation levels (see ``trio_opencode/authorbox.py``):
#: ``sandbox`` -- the whole OpenCode process runs under bwrap with only its
#: export (plus what OpenCode itself needs) visible, so a shell is safe;
#: ``no-shell`` -- no OS sandbox is usable (Docker/Harbor task containers
#: block user namespaces), so the author gets NO shell and only OpenCode's own
#: path-checked file tools.
LEVEL_SANDBOX = "sandbox"
LEVEL_NO_SHELL = "no-shell"


@dataclass(frozen=True)
class AuthorIsolation:
    """What the acceptance author may and may not touch for one turn.

    ``export`` is its workspace (also its process cwd, i.e. OpenCode's
    project directory). ``forbidden`` are the loop's own roots (repository,
    git dir, mailboxes) whose paths it must never read. ``allow_dirs`` are
    scratch directories (its ``TMPDIR``, OpenCode's own data dir) it may use
    even when they sit under a forbidden root. ``tool`` is the validator
    script it may run (``trio-acceptance.py``). ``level`` says how the turn
    is contained (:data:`LEVEL_SANDBOX` / :data:`LEVEL_NO_SHELL`)."""
    export: str
    forbidden: tuple[str, ...] = ()
    allow_dirs: tuple[str, ...] = ()
    tool: str | None = None
    level: str = LEVEL_NO_SHELL


def _spellings(path: "str | Path") -> list[str]:
    """normpath and realpath spellings of ``path`` (symlinked roots)."""
    raw = os.path.normpath(str(path))
    real = os.path.realpath(str(path))
    return list(dict.fromkeys([raw, real]))


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _usable_forbidden(iso: AuthorIsolation) -> list[str]:
    """Forbidden roots that can actually be denied: never ``/``, and never a
    root that contains the export (or an allowed scratch dir) -- denying
    that would also deny the author's own workspace."""
    keep: list[str] = []
    keepers = [sp for p in (iso.export, *iso.allow_dirs) for sp in _spellings(p)]
    for root in iso.forbidden:
        for sp in _spellings(root):
            if sp in ("/", "") or sp in keep:
                continue
            if any(_is_under(k, sp) for k in keepers):
                continue
            keep.append(sp)
    return keep


def _acceptance_permission(permission: dict[str, Any], repo_root: "Path | None",
                           iso: "AuthorIsolation | None") -> dict[str, Any]:
    """The acceptance author's permission block.

    Grounded in how OpenCode v2 itself evaluates a call (read out of the
    shipped binary): a path is resolved *lexically* against the project
    directory (``..`` folded, symlinks NOT followed); a path inside it is
    authorized as the RELATIVE path, one outside it first asks
    ``external_directory`` with ``<dir>/*`` and is then judged by the tool's
    own rule on the ABSOLUTE path; the last matching rule wins and an
    unmatched call is ``ask`` (= refused in a non-interactive run); ``bash``
    is the ``shell`` action; ``glob`` judges the glob PATTERN, ``grep`` the
    regex, never the directory (that is ``external_directory``'s job).

    So: ``external_directory`` is NEVER the container-mode flat ``allow`` --
    the export is the project directory, everything the author legitimately
    needs is inside it, and anything outside is refused for read, glob,
    grep and edit alike; ``read`` additionally denies the loop's own
    roots by absolute path; ``glob`` refuses patterns that climb (``..``) or
    are absolute (the pattern itself is not path-checked). A symlink that
    leaves the export is removed before the turn (``authorbox.sanitize_export``),
    because containment is lexical. At level ``no-shell`` the author gets no
    shell at all -- command-text patterns cannot bound a shell (``cd ..``,
    ``python -c open()``, string building); at level ``sandbox`` the OS keeps
    everything but the export out of reach, so the usual guard-railed shell
    stays. Every other tool is DENIED BY DEFAULT (``"*": "deny"`` first,
    :func:`_default_deny`): OpenCode v2's Code Mode ``execute`` can ``fetch``
    ``file://`` and ``session_move`` out of the export, so a deny list of
    known tools is not enough. Allows that must beat a deny come after it."""
    perm = _default_deny(permission, iso)
    ext: dict[str, str] = {"*": "deny"}
    if repo_root is not None:
        base = f"{Path(repo_root).resolve()}/.trio-opencode/worktrees/tmp-"
        ext[f"{base}*"] = "allow"
        ext[f"{base}*/**"] = "allow"
    if iso is None:
        perm["external_directory"] = ext
        return perm
    bad = _usable_forbidden(iso)
    scratch = [sp for p in (iso.export, *iso.allow_dirs) for sp in _spellings(p)]
    for sp in scratch:
        ext[sp] = "allow"
        ext[f"{sp}/**"] = "allow"
    tool_sp = _spellings(iso.tool) if iso.tool and os.path.isabs(iso.tool) else []
    # The validator the author may run (and read): its directory is allowed
    # unless it sits inside a root the author must not see (the trio repo
    # hosting itself -- OpenCode's external_directory can only name the
    # directory, which would be the repo's, so then it is not readable).
    tool_clean = bool(tool_sp) and not any(_is_under(t, r) for t in tool_sp for r in bad)
    if tool_clean:
        for t in tool_sp:
            ext[f"{os.path.dirname(t)}/*"] = "allow"
    perm["external_directory"] = ext
    read: dict[str, str] = {"*": "allow"}
    for root in bad:
        read[root] = "deny"
        read[f"{root}/**"] = "deny"
    for sp in scratch + tool_sp:
        read[sp] = "allow"
        read[f"{sp}/**"] = "allow"
    # A project root that contains the export (OpenCode treats everything
    # under it as "inside") authorizes such a path by its RELATIVE form, which
    # climbs: refuse that form for reads and writes.
    read["../*"] = "deny"
    read[".."] = "deny"
    perm["read"] = read
    perm["edit"] = {"*": "allow", "../*": "deny"}
    perm["grep"] = "allow"
    if iso.level == LEVEL_NO_SHELL:
        perm["glob"] = {"*": "allow", "*..*": "deny", "/*": "deny", "~*": "deny"}
    else:
        perm["glob"] = "allow"
    return perm


#: The only tools the acceptance author may call (OpenCode v2 names ``write``
#: and ``edit`` under the ``edit`` action); a shell is added at level
#: ``sandbox`` only. Everything else -- ``execute`` (Code Mode: ``fetch
#: file://`` and ``session_move`` walk out of the export), ``bash``,
#: ``webfetch``, session/task/todo tools, MCP, and any tool a later OpenCode
#: adds -- falls under the leading ``"*": "deny"``.
_AUTHOR_TOOLS = ("read", "glob", "grep", "edit")


_AUTHOR_DENIED = ("bash", "execute", "webfetch", "websearch", "task", "todowrite", "lsp",
                  "skill", "list", "doom_loop", "question")


def _default_deny(permission: dict[str, Any], iso: "AuthorIsolation | None") -> dict[str, Any]:
    """The author's permission block before the path rules: ``"*": "deny"``
    first (OpenCode: the last matching rule wins, so every later key is an
    explicit allow-list entry), then every action the author may use. Deny
    and allow rules only: never ``ask``, never ``--auto``. ``iso is None``
    (the run's generic config, a shell-allowed context) keeps its shell."""
    perm: dict[str, Any] = {"*": "deny"}
    # Known tools are denied by name as well: the wildcard is what stops a
    # tool this driver has never heard of, the names are what an OpenCode
    # that does not honour a top-level ``*`` (v1) still reads.
    for key in _AUTHOR_DENIED:
        perm[key] = "deny"
    for key in _AUTHOR_TOOLS:
        perm[key] = permission.get(key, "allow")
    if iso is None or iso.level != LEVEL_NO_SHELL:
        perm["bash"] = permission["bash"]
    perm["external_directory"] = permission.get("external_directory", "deny")
    return perm


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
                       repo_root: "Path | None" = None,
                       iso: "AuthorIsolation | None" = None) -> dict[str, Any]:
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
    if role == "acceptance":
        permission = _acceptance_permission(permission, repo_root, iso)
    fm["permission"] = permission
    return fm


def _render_agent_file(role: str, cfg: Config, repo_root: Path,
                       iso: "AuthorIsolation | None" = None) -> str:
    description, body = _load_role_body(repo_root, role)
    frontmatter = _agent_frontmatter(role, cfg, description, repo_root, iso)
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
                        repo_root: "Path | None",
                        iso: "AuthorIsolation | None" = None) -> dict[str, Any]:
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
    if role == "acceptance":
        permission = _acceptance_permission(permission, repo_root, iso)
    entry["permission"] = permission
    entry["prompt"] = _DRIVER_NOTE + "\n\n" + body
    return entry


def _build_opencode_json_v2(cfg: Config, repo_root: Path,
                            iso: "AuthorIsolation | None" = None) -> dict[str, Any]:
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
        doc["agent"][f"trio-{role}"] = _agent_config_entry(role, cfg, description, body, repo_root, iso)
    if cfg.provider.id not in _BUILTIN_PROVIDERS:
        doc["provider"] = {cfg.provider.id: {}}
    return doc


def _write_opencode_dir(oc_dir: Path, cfg: Config, repo_root: Path, *, is_v1: bool,
                        iso: "AuthorIsolation | None" = None) -> Path:
    """Write ``<oc_dir>/opencode.json`` (plus, for v1, ``agent/trio-<role>
    .md``) and return the ``opencode.json`` path."""
    oc_dir.mkdir(parents=True, exist_ok=True)
    if is_v1:
        agent_dir = oc_dir / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        for role in ROLES:
            content = _render_agent_file(role, cfg, repo_root, iso)
            (agent_dir / f"trio-{role}.md").write_text(content, encoding="utf-8")
        doc = _build_opencode_json(cfg)
    else:
        doc = _build_opencode_json_v2(cfg, repo_root, iso)
    path = oc_dir / "opencode.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def generate_author_env(run_dir: Path, cfg: Config, repo_root: Path, *,
                        isolation: AuthorIsolation, style: str = "v2",
                        oc_dir: "Path | None" = None) -> dict[str, str]:
    """Per-turn config for the acceptance author: the same generated config
    as :func:`generate`, but with the ``trio-acceptance`` agent's permissions
    narrowed by ``isolation`` (see :class:`AuthorIsolation`). Written to
    ``oc_dir`` (default ``<run_dir>/opencode-author/``; the driver passes a
    directory OUTSIDE every forbidden root so the sandbox can mount it) and
    returned as the two env vars that select it (``OPENCODE_CONFIG`` /
    ``OPENCODE_CONFIG_DIR``); every other variable (XDG dirs, the key) stays
    the run's own unless the caller overrides it. Safe to call before every
    author attempt (overwrites)."""
    oc_dir = Path(oc_dir) if oc_dir is not None else Path(run_dir) / "opencode-author"
    path = _write_opencode_dir(oc_dir, cfg, Path(repo_root), is_v1=(style == "v1"),
                               iso=isolation)
    return {"OPENCODE_CONFIG": str(path), "OPENCODE_CONFIG_DIR": str(oc_dir)}


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
    opencode_json_path = _write_opencode_dir(oc_dir, cfg, repo_root, is_v1=is_v1)

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

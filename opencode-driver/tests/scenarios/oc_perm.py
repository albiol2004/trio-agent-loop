"""A small model of how OpenCode v2 authorizes a tool call, transcribed from
the shipped binary (so tests can check the rules the driver generates against
the real evaluation order without a model call):

* ``FileAccess.resolve``: the path is resolved LEXICALLY against the project
  directory (``..`` folded, symlinks not followed); inside it the resource is
  the RELATIVE path, outside it the resource is the ABSOLUTE path and
  ``external_directory`` is asked first with ``<dir>/*`` (``dir`` = the path
  itself for a directory, else its parent);
* ``p1``: the LAST rule whose action and resource both match wins; the
  agent's built-in rules come first (every action ``allow``,
  ``external_directory`` ``ask`` = refused in a non-interactive run);
* ``JD``: ``*`` -> ``.*``, ``?`` -> ``.``, anchored, dotall, ``\\`` -> ``/``,
  a trailing `` *`` also matches no arguments;
* a multi-resource call (a compound shell command) is denied when ANY
  resource is denied and allowed only when all are;
* the config key ``bash`` is the ``shell`` action; ``glob``/``grep`` judge the
  PATTERN, the search directory goes through ``external_directory``;
* a top-level ``"*"`` key is a rule for EVERY action (the config's rules are
  flattened in key order, so a leading ``"*": "deny"`` makes every action the
  config does not name afterwards denied -- including tools a later OpenCode
  adds);
* the real v2 tool list at the no-shell level is ``edit, glob, grep, read,
  write`` plus ``execute`` (Code Mode: ``fetch('file://...')`` reads any
  file, ``tools.opencode.session_move`` re-roots the session, so it is an
  escape unless its action is denied) and, at the sandbox level, ``shell``;
* the PROJECT ROOT is the nearest git ancestor of the process cwd when there
  is one (a dotfiles HOME, a repository holding the state dir), else the cwd:
  a path inside the root is "inside" (no ``external_directory`` ask) but its
  rule resource is still the path RELATIVE TO THE CWD, so it climbs
  (``../x``); ``grep``/``glob`` ``path:"../.."`` therefore search the whole
  root unchecked (probed against the real binary).
"""
from __future__ import annotations

import os
import re
from typing import Any


def wild(pattern: str) -> "re.Pattern[str]":
    body = re.escape(pattern.replace("\\", "/")).replace(r"\*", ".*").replace(r"\?", ".")
    if body.endswith(r"\ .*"):
        body = body[: -len(r"\ .*")] + r"(?:\ .*)?"
    return re.compile("^" + body + "$", re.DOTALL)


def effect(rules: Any, resource: str, *, default: str = "allow") -> str:
    """allow | deny | ask for ``resource`` under one action's rule block.
    ``default`` is what the agent's built-in rules say before any config rule
    matches (``{*, *, allow}`` for every action; ``external_directory`` is
    ``ask``, pass ``default="ask"``)."""
    if rules is None:
        return default
    if isinstance(rules, str):
        return rules
    out = default
    for pattern, eff in rules.items():
        if wild(pattern).match(resource.replace("\\", "/")):
            out = eff
    return out


def decide(perm: Any, action: str, resource: str, *, default: str = "allow") -> str:
    """The effect of ``action`` on ``resource`` under the WHOLE permission
    block: every config key whose name matches the action, in key order, the
    last matching rule winning (a string value covers every resource)."""
    out = default
    resource = resource.replace("\\", "/")
    for key, rules in perm.items():
        if not wild(key).match(action):
            continue
        if isinstance(rules, str):
            out = rules
        else:
            for pattern, eff in rules.items():
                if wild(pattern).match(resource):
                    out = eff
    return out


#: tool name -> permission action (OpenCode v2 authorizes ``write`` and
#: ``edit`` as ``edit``, the shell tool as ``bash``)
TOOL_ACTION = {"write": "edit", "apply_patch": "edit", "multiedit": "edit", "shell": "bash"}


def can_tool(perm: dict, tool: str) -> bool:
    """May the model call this tool at all? ``execute``, ``bash``,
    ``webfetch``, an unknown future tool ...: anything the block does not
    allow after ``"*": deny``. (A tool whose every resource is denied is not
    offered to the model either; here only the blanket decision counts.)"""
    return decide(perm, TOOL_ACTION.get(tool, tool), "*") == "allow"


def contains(parent: str, child: str) -> bool:
    rel = os.path.relpath(child, parent)
    return not (rel == ".." or rel.startswith("../") or os.path.isabs(rel))


def resolve(directory: str, path: str, *, is_dir: bool = False, root: str | None = None) -> dict:
    """``root`` is the project root when it is an ancestor of ``directory``
    (a git ancestor); the resource of an inside path stays relative to the
    cwd ``directory``."""
    absolute = os.path.normpath(os.path.join(directory, path))
    if contains(root or directory, absolute):
        return {"absolute": absolute, "resource": os.path.relpath(absolute, directory),
                "internal": True, "ext": None}
    parent = absolute if is_dir else os.path.dirname(absolute)
    return {"absolute": absolute, "resource": absolute, "internal": False,
            "ext": os.path.join(parent, "*")}


def _ext_ok(perm: dict, r: dict) -> bool:
    return r["ext"] is None or decide(perm, "external_directory", r["ext"], default="ask") == "allow"


def _file_tool(perm: dict, action: str, directory: str, path: str, *, is_dir: bool,
               root: str | None = None) -> bool:
    r = resolve(directory, path, is_dir=is_dir, root=root)
    return _ext_ok(perm, r) and decide(perm, action, r["resource"]) == "allow"


def can_read(perm: dict, directory: str, path: str, *, is_dir: bool = False,
             root: str | None = None) -> bool:
    return _file_tool(perm, "read", directory, path, is_dir=is_dir, root=root)


def can_edit(perm: dict, directory: str, path: str, *, root: str | None = None) -> bool:
    return _file_tool(perm, "edit", directory, path, is_dir=False, root=root)


def can_glob(perm: dict, directory: str, pattern: str, path: str | None = None, *,
             root: str | None = None) -> bool:
    r = resolve(directory, path or ".", is_dir=True, root=root)
    return _ext_ok(perm, r) and decide(perm, "glob", pattern) == "allow"


def can_grep(perm: dict, directory: str, pattern: str, path: str | None = None, *,
             root: str | None = None) -> bool:
    r = resolve(directory, path or ".", root=root)
    return _ext_ok(perm, r) and decide(perm, "grep", pattern) == "allow"


def can_shell(perm: dict, command: str) -> bool:
    parts = [p.strip() for p in re.split(r"&&|\|\||[;|&\n]", command) if p.strip()]
    return bool(parts) and all(decide(perm, "bash", p) == "allow" for p in parts)

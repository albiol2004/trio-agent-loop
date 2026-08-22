#!/usr/bin/env python3
"""Skill registry scanner - read-only index of every harness's skill surface.

Scans all known harness locations (claude, codex, omnigent, omp, opencode,
kimi, zcode, cursor, pi) plus canonical sources in this repo, parses
frontmatter dialects, hashes content for drift detection, emits registry.json.

Body hash and frontmatter hash are computed separately: frontmatter
legitimately differs per harness; body drift is what matters.

Installations are compared against the canonical source for their harness:
  in-sync | stale | unknown (no canonical source for that harness+name).

This module also owns the *format layer* the dashboard edits through: a
hand-written YAML-subset parser/serializer (no PyYAML - stdlib only) and a
``tomllib``-backed TOML parser with a minimal serializer. Both round-trip:
``parse(dump(parse(text))) == parse(text)``, with key insertion order
preserved (never sorted).

Usage: python3 registry/scan.py [--out registry/registry.json] [--project DIR]
Stdlib only. Requires Python 3.11+ (tomllib).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)
"""Frontmatter fence. Only horizontal whitespace is absorbed around the
fences: a greedy ``\\s*`` would swallow the blank line that separates
frontmatter from the body, which then cannot be reproduced on save."""

_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")
_INT_RE = re.compile(r"[+-]?\d+\Z")
_FLOAT_RE = re.compile(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?\Z")

_UNSAFE_PLAIN_HEAD = set("-?:,[]{}#&*!|>'\"%@`")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------
# YAML subset - parsing
# --------------------------------------------------------------------------


def _strip_comment(text: str) -> str:
    """Drop a trailing ``#`` comment, respecting quoted spans."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == "\\" and quote == '"':
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
    return text


def _unquote(text: str) -> str:
    """Decode a quoted YAML scalar; ``text`` includes its delimiters."""
    body = text[1:-1]
    if text[0] == "'":
        return body.replace("''", "'")
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            mapped = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
                      "0": "\0", "/": "/"}.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
            if nxt == "u" and i + 5 < len(body):
                try:
                    out.append(chr(int(body[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(ch)
        i += 1
    return "".join(out)


def _coerce_scalar(text: str):
    """Type-coerce a plain (unquoted) YAML scalar."""
    value = text.strip()
    if value == "" or value in ("null", "Null", "NULL", "~"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    return value


def _scalar(text: str):
    """Parse a YAML scalar that may be quoted, plain, or comment-trailed."""
    value = text.strip()
    if value[:1] in ("'", '"'):
        end = _quoted_end(value, 0)
        if end is not None:
            return _unquote(value[:end + 1])
    return _coerce_scalar(_strip_comment(value))


def _quoted_end(text: str, start: int) -> int | None:
    """Index of the closing quote for the quoted scalar starting at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and text[i + 1:i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def _split_key(line: str) -> tuple[str | None, str]:
    """Split ``key: rest``; returns ``(None, "")`` when the line is not a pair."""
    if line[:1] in ("'", '"'):
        end = _quoted_end(line, 0)
        if end is None:
            return None, ""
        rest = line[end + 1:].lstrip()
        if not rest.startswith(":"):
            return None, ""
        return _unquote(line[:end + 1]), rest[1:].strip()
    key, sep, rest = line.partition(":")
    if not sep:
        return None, ""
    return key.strip(), rest.strip()


def _parse_flow(text: str, pos: int = 0):
    """Parse a flow collection (``[a, b]`` / ``{a: b}``) starting at ``pos``."""
    def skip_ws(i):
        while i < len(text) and text[i] in " \t":
            i += 1
        return i

    def read_token(i):
        """Read a scalar / nested flow, returning (value, next_index)."""
        i = skip_ws(i)
        if i >= len(text):
            return None, i
        if text[i] in "[{":
            return _parse_flow(text, i)
        if text[i] in "\"'":
            end = _quoted_end(text, i)
            if end is None:
                return text[i:], len(text)
            return _unquote(text[i:end + 1]), end + 1
        start = i
        while i < len(text) and text[i] not in ",]}:":
            i += 1
        return _coerce_scalar(text[start:i]), i

    opener = text[pos]
    closer = "]" if opener == "[" else "}"
    i = pos + 1
    items: list = []
    mapping: dict = {}
    is_map = opener == "{"
    while i < len(text):
        i = skip_ws(i)
        if i < len(text) and text[i] == closer:
            i += 1
            break
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        value, i = read_token(i)
        i = skip_ws(i)
        if i < len(text) and text[i] == ":":
            is_map = True
            sub, i = read_token(i + 1)
            mapping[str(value)] = sub
        else:
            items.append(value)
    return (mapping if is_map else items), i


class _YamlReader:
    """Indentation-driven reader for the YAML subset the harnesses use."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            # Artifact of a trailing newline, not a blank line: counting it
            # would add a newline to every keep-chomped (``|+``) block.
            self.lines.pop()
        self.i = 0

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def _at_end(self) -> bool:
        return self.i >= len(self.lines)

    def _skip_blank(self) -> None:
        while not self._at_end():
            stripped = self.lines[self.i].strip()
            if stripped and not stripped.startswith("#"):
                return
            self.i += 1

    # -- nodes -------------------------------------------------------------

    def parse_document(self) -> dict:
        self._skip_blank()
        if self._at_end():
            return {}
        node = self.parse_node(self._indent(self.lines[self.i]))
        return node if isinstance(node, dict) else {}

    def parse_node(self, indent: int):
        self._skip_blank()
        if self._at_end():
            return None
        line = self.lines[self.i]
        if self._indent(line) < indent:
            return None
        stripped = line.strip()
        if stripped == "-" or stripped.startswith("- "):
            return self.parse_seq(self._indent(line))
        return self.parse_map(self._indent(line))

    def parse_map(self, indent: int) -> dict:
        result: dict = {}
        while True:
            self._skip_blank()
            if self._at_end():
                break
            line = self.lines[self.i]
            cur = self._indent(line)
            if cur < indent:
                break
            stripped = line.strip()
            if cur > indent:          # stray deeper line: ignore, stay resilient
                self.i += 1
                continue
            if stripped.startswith("- ") or stripped == "-":
                break                 # a sibling sequence belongs to our parent
            key, rest = _split_key(stripped)
            if key is None:
                self.i += 1
                continue
            self.i += 1
            result[key] = self.parse_value(rest, indent)
        return result

    def parse_seq(self, indent: int) -> list:
        items: list = []
        while True:
            self._skip_blank()
            if self._at_end():
                break
            line = self.lines[self.i]
            cur = self._indent(line)
            if cur != indent:
                break
            stripped = line.strip()
            if not (stripped == "-" or stripped.startswith("- ")):
                break
            if stripped == "-":
                self.i += 1
                items.append(self.parse_node(indent + 1))
                continue
            content = stripped[2:]
            key, _ = _split_key(content)
            if key is not None:
                # Rewrite "- key: v" as an ordinary mapping line so any
                # following keys of the same item join the same map.
                item_indent = cur + 2
                self.lines[self.i] = " " * item_indent + content
                items.append(self.parse_map(item_indent))
            else:
                self.i += 1
                items.append(_scalar(content))
        return items

    def parse_value(self, rest: str, key_indent: int):
        if rest[:1] in ("|", ">"):
            return self.parse_block_scalar(rest, key_indent)
        if rest[:1] == "[" or rest[:1] == "{":
            value, _ = _parse_flow(rest, 0)
            return value
        if rest != "":
            return _scalar(rest)
        # Empty value: a nested block, a sibling-indent sequence, or null.
        mark = self.i
        self._skip_blank()
        if self._at_end():
            self.i = mark
            return None
        cur = self._indent(self.lines[self.i])
        stripped = self.lines[self.i].strip()
        if cur > key_indent:
            return self.parse_node(cur)
        if cur == key_indent and (stripped == "-" or stripped.startswith("- ")):
            return self.parse_seq(cur)
        self.i = mark
        return None

    def parse_block_scalar(self, header: str, key_indent: int) -> str:
        style = header[0]
        chomp = ""
        explicit = None
        for ch in header[1:]:
            if ch in "+-":
                chomp = ch
            elif ch.isdigit():
                explicit = int(ch)
            else:
                break

        collected: list[str] = []
        pending: list[str] = []
        while not self._at_end():
            line = self.lines[self.i]
            if line.strip() == "":
                pending.append("")
                self.i += 1
                continue
            if self._indent(line) <= key_indent:
                break
            collected.extend(pending)
            pending = []
            collected.append(line)
            self.i += 1
        if self._at_end():
            # Trailing blank lines only belong to the block at end of input;
            # mid-document they are separators for the next key.
            collected.extend(pending)
        else:
            self.i -= len(pending)

        if not collected:
            return "" if chomp == "-" else ""

        if explicit is not None:
            content_indent = key_indent + explicit
        else:
            content_indent = min(self._indent(line) for line in collected if line.strip())
        body = "\n".join(line[content_indent:] if line.strip() else ""
                         for line in collected)
        text = body + "\n"
        if style == ">":
            text = _fold(text)
        if chomp == "-":
            return text.rstrip("\n")
        if chomp == "+":
            return text
        stripped = text.rstrip("\n")
        return stripped + "\n" if stripped else ""


def _fold(text: str) -> str:
    """Apply YAML folded-scalar line joining to literal block content."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    pieces: list[str] = []
    para: list[str] = []
    blanks = 0

    def flush() -> None:
        if para:
            pieces.append(" ".join(para))
            para.clear()

    for line in lines:
        if not line.strip():
            blanks += 1
            continue
        literal = line[:1] in (" ", "\t")   # more-indented lines stay literal
        if blanks or literal:
            flush()
            if pieces:
                # n consecutive breaks fold to n-1 newlines, so a single
                # blank line separates paragraphs with exactly one "\n".
                pieces.append("\n" * max(blanks, 1))
            blanks = 0
        if literal:
            pieces.append(line)
        else:
            para.append(line.strip())
    flush()
    return "".join(pieces) + "\n"


def parse_yaml(text: str) -> dict:
    """Parse a YAML-subset document (no ``---`` fences) into a dict."""
    if not text.strip():
        return {}
    return _YamlReader(text).parse_document()


# --------------------------------------------------------------------------
# YAML subset - serialization
# --------------------------------------------------------------------------


def _quote(text: str) -> str:
    """Double-quote a scalar, keeping non-ASCII literal (no \\uXXXX churn)."""
    return json.dumps(text, ensure_ascii=False)


def _dump_key(key) -> str:
    key = str(key)
    return key if _PLAIN_KEY_RE.match(key) else _quote(key)


def _plain_safe(value: str) -> bool:
    if value == "" or value != value.strip() or "\n" in value:
        return False
    if value[0] in _UNSAFE_PLAIN_HEAD:
        return False
    if ": " in value or value.endswith(":") or " #" in value:
        return False
    return _coerce_scalar(value) == value


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    value = str(value)
    return value if _plain_safe(value) else _quote(value)


def _dump_block_scalar(value: str, indent: int) -> str:
    """Render a multi-line string as a literal block, chomping preserved."""
    pad = " " * (indent + 2)
    trailing = len(value) - len(value.rstrip("\n"))
    if trailing == 0:
        header, body = "|-", value
    elif trailing == 1:
        header, body = "|", value[:-1]
    else:
        header, body = "|+", value[:-1]
    lines = body.split("\n")
    rendered = "\n".join(f"{pad}{line}" if line else "" for line in lines)
    return f"{header}\n{rendered}\n"


def _dump_node(data, indent: int) -> str:
    pad = " " * indent
    out: list[str] = []
    for key, value in data.items():
        label = f"{pad}{_dump_key(key)}:"
        if isinstance(value, dict):
            out.append(f"{label} {{}}\n" if not value
                       else f"{label}\n{_dump_node(value, indent + 2)}")
        elif isinstance(value, list):
            if not value:
                out.append(f"{label} []\n")
            else:
                out.append(f"{label}\n")
                for item in value:
                    if isinstance(item, dict):
                        block = _dump_node(item, indent + 4)
                        first, _, more = block.partition("\n")
                        out.append(f"{pad}  - {first.strip()}\n")
                        if more.strip():
                            out.append(more if more.endswith("\n") else more + "\n")
                    else:
                        out.append(f"{pad}  - {_dump_scalar(item)}\n")
        elif isinstance(value, str) and "\n" in value:
            out.append(f"{label} {_dump_block_scalar(value, indent)}")
        else:
            out.append(f"{label} {_dump_scalar(value)}\n")
    return "".join(out)


def dump_yaml(data: dict) -> str:
    """Serialize a dict to the YAML subset, preserving insertion order."""
    if not data:
        return ""
    return _dump_node(data, 0)


# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file into (frontmatter fields, body).

    Files without a leading ``---`` fence return ``({}, text)`` unchanged.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fields = parse_yaml(m.group(1))
    except Exception:                                   # never kill a scan
        fields = {}
    return fields, text[m.end():]


def dump_frontmatter(fields: dict, body: str) -> str:
    """Rebuild a Markdown file; empty frontmatter returns the body verbatim."""
    if not fields:
        return body
    return f"---\n{dump_yaml(fields)}---\n{body}"


# --------------------------------------------------------------------------
# TOML
# --------------------------------------------------------------------------


def parse_toml(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML: {exc}") from exc


def _dump_toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_toml_value(v) for v in value) + "]"
    text = str(value)
    if "\n" not in text:
        return _quote(text)
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        escaped = escaped[:-1] + '\\"'
    return f'"""\n{escaped}"""'


def dump_toml(data: dict, _prefix: str = "") -> str:
    """Serialize a mapping to TOML, preserving insertion order.

    Agent files are flat, but nested dicts are emitted as ``[table]`` sections
    so example configs round-trip too. Multi-line strings become ``\"\"\"``
    basic strings with a leading newline, which ``tomllib`` strips again on
    read - so the value round-trips exactly.
    """
    scalars: list[str] = []
    tables: list[str] = []
    for key, value in data.items():
        label = _dump_key(key)
        path = f"{_prefix}{label}"
        if isinstance(value, dict):
            body = dump_toml(value, f"{path}.")
            tables.append(f"[{path}]\n{body}" if body.strip() else f"[{path}]\n")
        elif (isinstance(value, list) and value
              and all(isinstance(item, dict) for item in value)):
            for item in value:
                tables.append(f"[[{path}]]\n{dump_toml(item, '')}")
        else:
            scalars.append(f"{label} = {_dump_toml_value(value)}")
    out = "\n".join(scalars) + ("\n" if scalars else "")
    for table in tables:
        out += ("\n" if out else "") + table
    return out


# --------------------------------------------------------------------------
# Format dispatch
# --------------------------------------------------------------------------


def file_format(path) -> str:
    """``"toml"`` for ``.toml`` files, ``"yaml"`` for everything else."""
    return "toml" if str(path).endswith(".toml") else "yaml"


def split_file(text: str, fmt: str) -> tuple[dict, str]:
    """Split file text into (editable fields, editable body) for ``fmt``."""
    if fmt == "toml":
        try:
            fields = parse_toml(text)
        except ValueError:
            return {}, text
        body = fields.pop("developer_instructions", "")
        return fields, body if isinstance(body, str) else ""
    return parse_frontmatter(text)


def join_file(fields: dict, body: str, fmt: str) -> str:
    """Inverse of :func:`split_file`."""
    if fmt == "toml":
        merged = {k: v for k, v in fields.items() if k != "developer_instructions"}
        merged["developer_instructions"] = body
        return dump_toml(merged)
    return dump_frontmatter(fields, body)


# --------------------------------------------------------------------------
# Per-(harness, surface) schema
# --------------------------------------------------------------------------

SURFACE_FORMAT: dict[tuple[str, str], str] = {
    ("claude", "skill"): "yaml",
    ("claude", "command"): "yaml",
    ("claude", "agent"): "yaml",
    ("codex", "skill"): "yaml",
    ("codex", "agent"): "toml",
    ("omp", "command"): "yaml",
    ("omp", "agent"): "yaml",
    ("opencode", "command"): "yaml",
    ("opencode", "agent"): "yaml",
    ("kimi", "skill"): "yaml",
    ("zcode", "skill"): "yaml",
}


def _field(key, type_="string", widget="text", required=False, enum=None, help_=""):
    return {"key": key, "type": type_, "widget": widget,
            "required": required, "enum": enum, "help": help_}


_SKILL_FIELDS = [
    _field("name", required=True, help_="must equal the skill's directory name"),
    _field("description", widget="textarea", required=True,
           help_="when the model should reach for this skill"),
    _field("disable-model-invocation", "bool", "checkbox",
           help_="hide from automatic model invocation"),
    _field("argument-hint", help_="placeholder shown for slash-command arguments"),
    _field("allowed-tools", "string", "list",
           help_="comma-separated Bash(<prefix>:*) allowlist"),
]

KEY_SCHEMA: dict[str, list[dict]] = {
    "claude:skill": _SKILL_FIELDS,
    "codex:skill": _SKILL_FIELDS,
    "kimi:skill": [
        _field("name", required=True, help_="must equal the skill's directory name"),
        _field("description", widget="textarea", required=True),
        _field("type", enum=["prompt"], widget="select"),
        _field("whenToUse", widget="textarea"),
        _field("arguments", "list", "list", help_="substituted into the body as $name"),
    ],
    "zcode:skill": [
        _field("name", required=True, help_="must equal the skill's directory name"),
        _field("description", widget="textarea", required=True),
    ],
    "claude:command": [
        _field("description", widget="textarea", required=True),
        _field("argument-hint"),
    ],
    "claude:agent": [
        _field("name", required=True, help_="must equal the filename stem"),
        _field("description", widget="textarea", required=True),
        _field("model", help_="short alias (sonnet) or full model id"),
        _field("effort", widget="select", enum=["low", "medium", "high"]),
        _field("disallowedTools", "string", "list",
               help_="comma-separated denylist; there is no positive tools list"),
    ],
    "codex:agent": [
        _field("name", required=True, help_="must equal the filename stem"),
        _field("model", help_="e.g. gpt-5.6-luna"),
        _field("model_reasoning_effort", widget="select",
               enum=["low", "medium", "high"]),
        _field("description", required=True),
        _field("sandbox_mode", widget="select",
               enum=["read-only", "workspace-write", "danger-full-access"]),
    ],
    "omp:agent": [
        _field("name", required=True, help_="must equal the filename stem"),
        _field("description", widget="textarea", required=True),
        _field("model", help_="provider-qualified, e.g. deepseek/deepseek-v4-flash"),
        _field("spawns", "string", "list", help_="comma-separated agent allowlist"),
        _field("tools", "string", "list", help_="comma-separated lowercase tool names"),
        _field("read-summarize", "bool", "checkbox"),
        _field("output", "map", "raw", help_="JSON schema in a YAML literal block"),
    ],
    "omp:command": [
        _field("description", widget="textarea", required=True),
    ],
    "opencode:agent": [
        _field("description", widget="textarea", required=True),
        _field("mode", widget="select", enum=["subagent", "primary"]),
        _field("hidden", "bool", "checkbox"),
        _field("permission", "map", "raw",
               help_="nested allow/deny map; quoted \"*\" is the default rule"),
    ],
    "opencode:command": [
        _field("description", widget="textarea", required=True),
        _field("agent", help_="agent this command runs as"),
        _field("subtask", "bool", "checkbox"),
    ],
}


def default_template(harness: str, surface: str, name: str) -> str:
    """Initial file content for a newly created registry entry."""
    key = f"{harness}:{surface}"
    fmt = SURFACE_FORMAT.get((harness, surface), "yaml")
    if fmt == "toml":
        return dump_toml({
            "name": name,
            "model": "",
            "description": "",
            "developer_instructions": "\n",
        })
    fields: dict = {}
    for spec in KEY_SCHEMA.get(key, []):
        if not spec["required"]:
            continue
        fields[spec["key"]] = name if spec["key"] == "name" else ""
    if not fields:
        fields = {"description": ""}
    return dump_frontmatter(fields, "\n")


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def entry_record(path: Path, harness: str, surface: str, scope: str,
                 managed: bool = False) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    if file_format(path) == "toml":
        try:
            fields = parse_toml(text)
        except ValueError:
            fields = {}
        body = text          # body_hash for TOML stays the whole file
    else:
        fields, body = parse_frontmatter(text)
    if path.name == "SKILL.md":
        name = fields.get("name") or path.parent.name
    else:
        name = fields.get("name") or path.stem
    return {
        "name": name,
        "path": str(path),
        "harness": harness,
        "surface": surface,
        "scope": scope,
        "managed": managed,
        "frontmatter": fields,
        "frontmatter_hash": sha256(json.dumps(fields, sort_keys=True, default=str)),
        "body_hash": sha256(body.strip()),
        "is_symlink": path.is_symlink(),
        "size": path.stat().st_size,
    }


def scan_skill_dir(root: Path, harness: str, scope: str,
                   managed: bool = False) -> list[dict]:
    """<root>/<name>/SKILL.md convention."""
    out = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        skill = child / "SKILL.md"
        if child.is_dir() and skill.is_file():
            out.append(entry_record(skill, harness, "skill", scope, managed))
    return out


def scan_md_dir(root: Path, harness: str, surface: str, scope: str) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.md")):
        if f.is_file():
            out.append(entry_record(f, harness, surface, scope))
    return out


def scan_toml_dir(root: Path, harness: str, surface: str, scope: str) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.toml")):
        if f.is_file():
            out.append(entry_record(f, harness, surface, scope))
    return out


def scan_instructions(path: Path, harness: str, scope: str) -> list[dict]:
    if not path.is_file():
        return []
    return [entry_record(path, harness, "instructions", scope)]


def scan_omnigent_roles(root: Path) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for role in sorted(root.iterdir()):
        cfg = role / "config.yaml"
        if role.is_dir() and cfg.is_file():
            rec = entry_record(cfg, "omnigent", "agent", "global")
            rec["name"] = role.name
            out.append(rec)
    return out


def scan_cursor_rules(root: Path, scope: str) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.mdc")):
        if f.is_file():
            out.append(entry_record(f, "cursor", "rule", scope))
    return out


def scan_ts_dir(root: Path, harness: str, surface: str) -> list[dict]:
    out = []
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.ts")):
        if f.is_file():
            out.append(entry_record(f, harness, surface, "global"))
    return out


def collect_canonical() -> list[dict]:
    """Canonical sources: this repo's per-harness skill/command/agent dirs."""
    entries: list[dict] = []
    entries += scan_skill_dir(REPO / ".claude/skills", "claude", "canonical")
    entries += scan_md_dir(REPO / ".claude/commands", "claude", "command", "canonical")
    entries += scan_md_dir(REPO / ".claude/agents", "claude", "agent", "canonical")
    entries += scan_skill_dir(REPO / "codex/skills", "codex", "canonical")
    entries += scan_toml_dir(REPO / "codex/agents", "codex", "agent", "canonical")
    entries += scan_skill_dir(REPO / "kimi/skills", "kimi", "canonical")
    entries += scan_skill_dir(REPO / "zcode/skills", "zcode", "canonical")
    entries += scan_md_dir(REPO / "opencode/commands", "opencode", "command", "canonical")
    entries += scan_md_dir(REPO / "opencode/agents", "opencode", "agent", "canonical")
    entries += scan_md_dir(REPO / "omp/commands", "omp", "command", "canonical")
    entries += scan_md_dir(REPO / "omp/agents", "omp", "agent", "canonical")
    entries += scan_skill_dir(REPO / "omnigent/entrypoints", "omnigent", "canonical")
    return entries


def collect(project: Path | None) -> list[dict]:
    entries: list[dict] = []

    # Claude
    entries += scan_skill_dir(HOME / ".claude/skills", "claude", "global")
    entries += scan_md_dir(HOME / ".claude/commands", "claude", "command", "global")
    entries += scan_md_dir(HOME / ".claude/agents", "claude", "agent", "global")
    entries += scan_instructions(HOME / ".claude/CLAUDE.md", "claude", "global")

    # Codex (+ shared ~/.agents convention)
    entries += scan_skill_dir(HOME / ".agents/skills", "codex", "global")
    entries += scan_toml_dir(HOME / ".codex/agents", "codex", "agent", "global")
    entries += scan_instructions(HOME / ".codex/AGENTS.md", "codex", "global")

    # Omnigent (roles; skills come from claude/agents dirs natively)
    entries += scan_omnigent_roles(HOME / ".omnigent/agents/trio-omnigent-roles")
    entries += scan_instructions(HOME / ".omnigent/config.yaml", "omnigent", "global")

    # OMP
    entries += scan_md_dir(HOME / ".omp/agent/commands", "omp", "command", "global")
    entries += scan_md_dir(HOME / ".omp/agent/agents", "omp", "agent", "global")
    entries += scan_instructions(HOME / ".omp/agent/AGENTS.md", "omp", "global")

    # OpenCode
    entries += scan_md_dir(HOME / ".config/opencode/commands", "opencode", "command", "global")
    entries += scan_md_dir(HOME / ".config/opencode/agents", "opencode", "agent", "global")
    entries += scan_instructions(HOME / ".config/opencode/AGENTS.md", "opencode", "global")

    # Kimi
    entries += scan_skill_dir(HOME / ".kimi-code/skills", "kimi", "global")
    entries += scan_instructions(HOME / ".kimi-code/AGENTS.md", "kimi", "global")

    # ZCode
    entries += scan_skill_dir(HOME / ".zcode/skills", "zcode", "global")
    entries += scan_instructions(HOME / ".zcode/AGENTS.md", "zcode", "global")

    # Cursor (~/.cursor/skills-cursor is Cursor-managed cache, not user content)
    entries += scan_skill_dir(HOME / ".cursor/skills-cursor", "cursor", "global", managed=True)
    entries += scan_cursor_rules(HOME / ".cursor/rules", "global")
    entries += scan_md_dir(HOME / ".cursor/agents", "cursor", "agent", "global")

    # Pi
    entries += scan_ts_dir(HOME / ".pi/agent/extensions", "pi", "extension")

    # Project scope
    if project:
        entries += scan_skill_dir(project / ".claude/skills", "claude", "project")
        entries += scan_md_dir(project / ".claude/commands", "claude", "command", "project")
        entries += scan_md_dir(project / ".claude/agents", "claude", "agent", "project")
        entries += scan_instructions(project / "CLAUDE.md", "claude", "project")
        entries += scan_instructions(project / "AGENTS.md", "codex", "project")
        entries += scan_cursor_rules(project / ".cursor/rules", "project")
        entries += scan_md_dir(project / ".opencode/agents", "opencode", "agent", "project")
        entries += scan_skill_dir(project / ".cursor/skills", "cursor", "project")

    return entries


def build_index(entries: list[dict]) -> dict:
    """Group skill/command entries by logical name; verdict vs canonical."""
    by_name: dict[str, list[dict]] = {}
    for e in entries:
        if e["surface"] in ("skill", "command"):
            by_name.setdefault(e["name"], []).append(e)

    groups = []
    stale = []
    for name, members in sorted(by_name.items()):
        canonical = {(m["harness"], m["surface"]): m["body_hash"]
                     for m in members if m["scope"] == "canonical"}
        installations = []
        for m in members:
            inst = {"harness": m["harness"], "surface": m["surface"], "scope": m["scope"],
                    "path": m["path"], "body_hash": m["body_hash"],
                    "is_symlink": m["is_symlink"], "managed": m["managed"]}
            if m["scope"] == "canonical":
                inst["status"] = "canonical"
            else:
                ref = canonical.get((m["harness"], m["surface"]))
                if ref is None:
                    inst["status"] = "unknown"
                elif ref == m["body_hash"]:
                    inst["status"] = "in-sync"
                else:
                    inst["status"] = "stale"
                    stale.append(f"{name} ({m['harness']}/{m['surface']})")
            installations.append(inst)
        groups.append({
            "name": name,
            "installations": installations,
            "harnesses": sorted({m["harness"] for m in members}),
        })

    return {
        "version": 1,
        "generated_by": "registry/scan.py",
        "entry_count": len(entries),
        "entries": entries,
        "groups": groups,
        "stale": sorted(stale),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent / "registry.json"))
    ap.add_argument("--project", default=None, help="project dir for project-scope entries")
    args = ap.parse_args()

    project = Path(args.project).resolve() if args.project else None
    index = build_index(collect(project) + collect_canonical())

    out = Path(args.out)
    out.write_text(json.dumps(index, indent=2, default=str) + "\n")
    print(f"scanned {index['entry_count']} entries, {len(index['groups'])} named groups, "
          f"{len(index['stale'])} stale -> {out}")
    for s in index["stale"]:
        print(f"  STALE {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

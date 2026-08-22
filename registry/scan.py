#!/usr/bin/env python3
"""Skill registry scanner - read-only index of every harness's skill surface.

Scans all known harness locations (claude, codex, omnigent, omp, opencode,
kimi, zcode, cursor, pi) plus canonical sources in this repo, parses
frontmatter dialects, hashes content for drift detection, emits registry.json.

Body hash and frontmatter hash are computed separately: frontmatter
legitimately differs per harness; body drift is what matters.

Installations are compared against the canonical source for their harness:
  in-sync | stale | unknown (no canonical source for that harness+name).

Usage: python3 registry/scan.py [--out registry/registry.json] [--project DIR]
Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter splitter. Returns (fields, body).

    Only flat key: value pairs and simple [a, b] lists are parsed.
    Good enough for the skill/agent frontmatter dialects in play.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, object] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            fields[key] = value.strip("'\"")
    return fields, text[m.end():]


def entry_record(path: Path, harness: str, surface: str, scope: str,
                 managed: bool = False) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
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
        "frontmatter_hash": sha256(json.dumps(fields, sort_keys=True)),
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
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        fields = {}
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                fields[k.strip()] = v.strip().strip('"')
        rec = entry_record(f, harness, surface, scope)
        rec["frontmatter"] = fields
        rec["frontmatter_hash"] = sha256(json.dumps(fields, sort_keys=True))
        rec["body_hash"] = sha256(text.strip())
        out.append(rec)
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
    out.write_text(json.dumps(index, indent=2) + "\n")
    print(f"scanned {index['entry_count']} entries, {len(index['groups'])} named groups, "
          f"{len(index['stale'])} stale -> {out}")
    for s in index["stale"]:
        print(f"  STALE {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

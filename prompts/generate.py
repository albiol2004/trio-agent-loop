#!/usr/bin/env python3
"""generate.py — single-source Trio role prompts.

Renders the canonical role bodies (prompts/canonical/*.md) through the
per-flavor overlays (prompts/overlays/<flavor>.md) into each flavor's exact
in-repo role-file locations, and upserts a short protocol-essentials block
into the embedded prompt sites (Pi extension, Omnigent role configs, SKILL.md
orchestrator sections).

Usage:
  python3 prompts/generate.py            # regenerate all role files + embedded regions
  python3 prompts/generate.py --check    # exit 1 if any generated file differs from the tree

Stdlib only. Generated files are committed; a second run is a byte-identical
no-op.
"""
from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "prompts" / "canonical"
OVERLAYS_DIR = ROOT / "prompts" / "overlays"
ESSENTIALS = ROOT / "prompts" / "protocol-essentials.md"

CANONICAL_ROLES = ("lead", "evaluator", "repair", "builder", "orchestrator")

# Embedded prompt sites: (repo-relative path, style). Style "md" uses
# `<!-- trio-protocol:start -->` markers; "ts" uses `// trio-protocol:start`;
# "yaml" uses the md markers indented inside a `prompt: |` literal block.
EMBEDDED = [
    ("pi/extensions/trio.ts", "ts"),
    ("omnigent/trio-omnigent-roles/lead/config.yaml", "yaml"),
    ("omnigent/trio-omnigent-roles/evaluator/config.yaml", "yaml"),
    ("omnigent/trio-omnigent-roles/builder/config.yaml", "yaml"),
    ("omnigent/trio-omnigent-roles/scout/config.yaml", "yaml"),
    ("omnigent/trio-omnigent-roles/docs/config.yaml", "yaml"),
    (".claude/skills/trio/SKILL.md", "md"),
    (".agents/skills/trio/SKILL.md", "md"),
    ("codex/skills/trio/SKILL.md", "md"),
    ("kimi/skills/trio/SKILL.md", "md"),
    ("zcode/skills/trio/SKILL.md", "md"),
    ("omnigent/entrypoints/trio-omnigent/SKILL.md", "md"),
    ("omnigent/entrypoints/trio-omnigent/prompts/lead.md", "md"),
    ("omnigent/entrypoints/trio-omnigent/prompts/evaluator.md", "md"),
]

# Generated standalone documents: one canonical body in prompts/documents/,
# fanned out to per-harness command/skill surfaces with per-dest frontmatter.
# (source, [(dest, frontmatter-lines), ...])
DOCUMENTS = [
    ("trio-ship", [
        ("omp/commands/trio-ship.md",
         ["---",
          "description: Manually recover an orphaned SHIP verdict — performs the Evaluator's retirement commit (product + mailbox) for a SHIP whose commit: lines are missing. No subagent, no model config.",
          "---"]),
        ("opencode/commands/trio-ship.md",
         ["---",
          "description: Manually recover an orphaned SHIP verdict — perform the Evaluator's retirement commit for a SHIP whose commit: lines are missing.",
          "agent: trio-orchestrator",
          "---"]),
        (".claude/skills/trio-ship/SKILL.md",
         ["---",
          "name: trio-ship",
          "description: Recover an orphaned SHIP verdict — perform the Evaluator's retirement commit (product + mailbox) when a SHIP's commit: lines are missing. No subagent, no model config.",
          "---"]),
        ("codex/skills/trio-ship/SKILL.md",
         ["---",
          "name: trio-ship",
          "description: Recover an orphaned SHIP verdict — perform the Evaluator's retirement commit (product + mailbox) when a SHIP's commit: lines are missing. No subagent, no model config.",
          "---"]),
        ("kimi/skills/trio-ship/SKILL.md",
         ["---",
          "name: trio-ship",
          "description: Recover an orphaned SHIP verdict — perform the Evaluator's retirement commit (product + mailbox) when a SHIP's commit: lines are missing. No subagent, no model config.",
          "---"]),
    ]),
]
DOCUMENTS_DIR = ROOT / "prompts" / "documents"

# r18a L0: the canonical evaluator rigor, delivered to the Omnigent evaluator
# (its registered role config and its per-dispatch prompt), which never
# receives the canonical role body. The block is EXTRACTED from
# prompts/canonical/evaluator.md at generation time -- never a hand copy --
# so `--check` fails as soon as the canonical text and the Omnigent sites
# disagree, and a missing anchor fails generation loudly.
RIGOR_MARKER = "trio-evaluator-rigor"
RIGOR_SITES = [
    ("omnigent/trio-omnigent-roles/evaluator/config.yaml", "yaml"),
    ("omnigent/entrypoints/trio-omnigent/prompts/evaluator.md", "md"),
]
# (kind, canonical heading, bullet prefix): "section" copies a whole
# `## <heading>` section (demoted to `###`); "bullet" copies the one
# top-level bullet of that section whose text starts with the prefix.
RIGOR_PIECES = [
    ("section", "Data-work profile", None),
    ("bullet", "Method", "Run the acceptance checks yourself"),
    ("bullet", "Method", "**Suites outside the targeted check:**"),
    ("bullet", "Method", "**Test-integrity audit (mandatory):**"),
    ("bullet", "Method", "No SHIP — whole-goal verdict or open-loop slice section"),
    ("bullet", "Method", "Prefer executing code over reading it"),
    ("bullet", "Anti-rubber-stamp rules", "If you did not run a criterion's check yourself"),
    ("section", "Evidence kinds", None),
    ("section", "Independent probe", None),
]
RIGOR_INTRO = (
    "## Verification rigor\n"
    "Generated from the canonical Trio evaluator (prompts/canonical/evaluator.md);\n"
    "binding for every verdict you write -- open-loop slice sections and the\n"
    "integration verdict alike.\n"
)


def render_document(source: str, frontmatter: list[str]) -> str:
    body = (DOCUMENTS_DIR / f"{source}.md").read_text(encoding="utf-8")
    return "\n".join(frontmatter) + "\n\n" + body.lstrip("\n")


SLOT_REF = re.compile(r"{{\s*(\w+)\.([A-Z][A-Z0-9_]*)\s*}}")
TARGET_RE = re.compile(r"^([\w.]+)(?:@([\w.]+))?\s*:\s*(\S+)\s*$")
SLOT_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*:\s*(.*)$")
ROLE_SELECTOR_RE = re.compile(r"^<!--\s*role:\s*([\w.@]+)\s*-->$")


class Overlay:
    def __init__(self, flavor: str):
        self.flavor = flavor
        self.inherits: str | None = None
        self.targets: list[tuple[str, str, str]] = []  # (role, target, path)
        self.slots: dict[tuple[str | None, str | None], dict[str, str]] = {}
        self.headers: dict[str | None, list[str]] = {}
        self.footers: dict[str | None, list[str]] = {}

    def base(self) -> "Overlay":
        if not self.inherits:
            return self
        parent = load_overlay(self.inherits)
        return parent

    def resolve_slot(self, role: str, target: str, name: str) -> str:
        chain = [self]
        if self.inherits:
            chain.append(self.base())
        for ov in chain:
            for key in ((role, target), (role, None), (None, None)):
                block = ov.slots.get(key)
                if block and name in block:
                    return block[name]
        raise KeyError(f"slot {name!r} for role {role!r} target {target!r} is not defined in overlay {self.flavor!r}")

    def block_for(self, blocks: dict, role: str, target: str) -> str:
        chain = [self]
        if self.inherits:
            chain.append(self.base())
        for ov in chain:
            for key in (f"{role}@{target}", role, None):
                if key in ov.__dict__[blocks]:
                    return "\n".join(ov.__dict__[blocks][key]).rstrip("\n")
        return ""


def _parse_header_block(lines: list[str]) -> dict[str | None, list[str]]:
    blocks: dict[str | None, list[str]] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in lines:
        m = ROLE_SELECTOR_RE.match(ln)
        if m:
            blocks[cur] = buf
            cur = m.group(1)
            buf = []
        else:
            buf.append(ln)
    blocks[cur] = buf
    return blocks


def _parse_slots(lines: list[str]) -> dict[str, str]:
    slots: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for ln in lines:
        if ln == "" or ln.startswith(("  ", "\t")):
            if cur is not None:
                buf.append(ln)
            continue
        m = SLOT_LINE_RE.match(ln)
        if m:
            if cur is not None:
                slots[cur] = _finish_slot(buf)
            cur = m.group(1)
            val = m.group(2).strip()
            val = re.sub(r"\|$", "", val).strip()
            if val in ("''", '""'):
                val = ""
            buf = [val] if val else []
        else:
            if cur is not None:
                buf.append(ln)
    if cur is not None:
        slots[cur] = _finish_slot(buf)
    return slots


def _finish_slot(buf: list[str]) -> str:
    text = textwrap.dedent("\n".join(buf)).strip("\n")
    return text


def load_overlay(flavor: str) -> Overlay:
    ov = Overlay(flavor)
    text = (OVERLAYS_DIR / f"{flavor}.md").read_text(encoding="utf-8")
    section: str | None = None
    section_lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("## "):
            if section is not None:
                _absorb(ov, section, section_lines)
            section = raw[3:].strip()
            section_lines = []
        elif raw.startswith("inherits:"):
            ov.inherits = raw.split(":", 1)[1].strip()
        else:
            section_lines.append(raw)
    if section is not None:
        _absorb(ov, section, section_lines)
    return ov


def _absorb(ov: Overlay, section: str, lines: list[str]) -> None:
    if section == "targets":
        for ln in lines:
            m = TARGET_RE.match(ln)
            if m:
                role, target, path = m.group(1), m.group(2) or m.group(1), m.group(3)
                ov.targets.append((role, target, path))
        return
    m = re.match(r"^slots(?::([\w.]+)(?:@([\w.]+))?)?$", section)
    if m:
        key = (m.group(1), m.group(2))
        ov.slots[key] = _parse_slots(lines)
        return
    if section == "header":
        ov.headers = _parse_header_block(lines)
        return
    if section == "footer":
        ov.footers = _parse_header_block(lines)
        return
    # Unknown sections are documentation prose; ignore.


def render_role(role: str, target: str, overlay: Overlay) -> str:
    canonical = (CANONICAL_DIR / f"{role}.md").read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        r, name = m.group(1), m.group(2)
        if r != role:
            raise ValueError(f"canonical {role}.md references slot for role {r!r}")
        return overlay.resolve_slot(role, target, name)

    body = SLOT_REF.sub(sub, canonical)
    header = overlay.block_for("headers", role, target).replace("{{ROLE}}", role)
    footer = overlay.block_for("footers", role, target).replace("{{ROLE}}", role)
    if header:
        header = header + "\n\n"
    if footer:
        footer = "\n" + footer + "\n"
    out = (header + body.rstrip("\n") + "\n" + footer).rstrip("\n") + "\n"
    # Collapse runs of three or more newlines (empty slot values or short
    # slot blocks) to a single blank line for stable byte-identical output.
    return re.sub(r"\n{3,}", "\n\n", out)


def _marked_block(content: str, style: str, marker: str) -> str:
    if style == "ts":
        lines = [f"// {marker}:start"]
        lines += ["// " + ln for ln in content.splitlines()]
        lines.append(f"// {marker}:end")
        return "\n".join(lines) + "\n"
    if style == "yaml":
        lines = [f"  <!-- {marker}:start -->"]
        lines += ["  " + ln for ln in content.splitlines()]
        lines.append(f"  <!-- {marker}:end -->")
        return "\n".join(lines) + "\n"
    lines = [f"<!-- {marker}:start -->"]
    lines += content.splitlines()
    lines.append(f"<!-- {marker}:end -->")
    return "\n".join(lines) + "\n"


def essentials_block(style: str) -> str:
    content = ESSENTIALS.read_text(encoding="utf-8").rstrip("\n")
    return _marked_block(content, style, "trio-protocol")


def _canonical_section(text: str, heading: str) -> str:
    """Body of the `## <heading>` section of *text* (up to the next `## `)."""
    lines = text.splitlines()
    fence: str | None = None
    start: int | None = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith(("```", "~~~")):
            mark = stripped[:3]
            if fence is None:
                fence = mark
            elif mark == fence and stripped.strip("`~") == "":
                fence = None
            continue
        if fence is not None or not ln.startswith("## "):
            continue
        if start is not None:
            return "\n".join(lines[start:i]).strip("\n")
        if ln[3:].strip() == heading:
            start = i + 1
    if start is None:
        raise ValueError(f"evaluator rigor: canonical evaluator.md has no `## {heading}` section")
    return "\n".join(lines[start:]).strip("\n")


def _canonical_bullet(section: str, heading: str, prefix: str) -> str:
    """The one top-level bullet of *section* whose text starts with *prefix*."""
    bullets: list[list[str]] = []
    for ln in section.splitlines():
        if ln.startswith("- "):
            bullets.append([ln])
        elif bullets and (ln.startswith((" ", "\t")) and ln.strip()):
            bullets[-1].append(ln)
        elif bullets and not ln.strip():
            bullets.append([])  # a blank line ends the bullet
    found = [b for b in bullets if b and b[0][2:].startswith(prefix)]
    if len(found) != 1:
        raise ValueError(
            f"evaluator rigor: `## {heading}` has {len(found)} bullet(s) starting "
            f"with {prefix!r} (need exactly one)"
        )
    return "\n".join(found[0])


# The Omnigent evaluator's SHIP-retirement check runs every backticked
# `git ...` command of its effective prompt; the test-integrity bullet's
# `git diff` is a read, not a retirement step, so it is written plain there.
_RIGOR_REWRITES = {
    "**Test-integrity audit (mandatory):**": (
        "`git diff` on test files.",
        "diff the test files against the base (git diff -- <test paths>).",
    ),
}


def _rigor_bullet_text(bullet: str, prefix: str) -> str:
    rewrite = _RIGOR_REWRITES.get(prefix)
    if rewrite is None:
        return bullet
    old, new = rewrite
    if old not in bullet:
        raise ValueError(f"evaluator rigor: {prefix!r} bullet no longer contains {old!r}")
    return bullet.replace(old, new)


def rigor_content() -> str:
    """The Omnigent evaluator's `## Verification rigor` block (r18a L0)."""
    canonical = (CANONICAL_DIR / "evaluator.md").read_text(encoding="utf-8")
    parts = [RIGOR_INTRO.rstrip("\n")]
    bullets_by_heading: dict[str, list[str]] = {}
    order: list[tuple[str, str]] = []
    for kind, heading, prefix in RIGOR_PIECES:
        section = _canonical_section(canonical, heading)
        if kind == "section":
            order.append(("section", heading))
            bullets_by_heading.setdefault(f"section:{heading}", []).append(section)
        else:
            key = f"bullets:{heading}"
            if key not in bullets_by_heading:
                order.append(("bullets", heading))
                bullets_by_heading[key] = []
            bullets_by_heading[key].append(
                _rigor_bullet_text(_canonical_bullet(section, heading, prefix), prefix))
    for kind, heading in order:
        if kind == "section":
            body = bullets_by_heading[f"section:{heading}"][0]
            parts.append(f"### {heading}\n{body}")
        else:
            body = "\n".join(bullets_by_heading[f"bullets:{heading}"])
            parts.append(f"### {heading} (canonical rules)\n{body}")
    return "\n\n".join(parts) + "\n"


def rigor_block(style: str) -> str:
    return _marked_block(rigor_content().rstrip("\n"), style, RIGOR_MARKER)


def upsert_embedded(path: Path, style: str) -> str:
    """Return the new file content for one embedded site (marker upsert)."""
    return upsert_marked(path.read_text(encoding="utf-8"), style,
                         "trio-protocol", essentials_block(style))


def upsert_marked(text: str, style: str, marker: str, block: str) -> str:
    """Replace (or append) the *marker* start/end region of *text* with *block*."""
    if style == "ts":
        pat = re.compile(rf"^// {marker}:(start|end)[ \t]*$", re.M)
    else:
        pat = re.compile(rf"^[ \t]*<!-- {marker}:(start|end) -->[ \t]*$", re.M)
    marks = list(pat.finditer(text))
    starts = [m for m in marks if m.group(1) == "start"]
    ends = [m for m in marks if m.group(1) == "end"]
    if starts and ends and starts[0].start() < ends[-1].start():
        new = text[: starts[0].start()] + block.rstrip("\n") + text[ends[-1].end():]
    else:
        new = text.rstrip("\n") + "\n\n" + block
    return new.rstrip("\n") + "\n"


def all_outputs() -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    for flavor in sorted(p.stem for p in OVERLAYS_DIR.glob("*.md")):
        ov = load_overlay(flavor)
        for role, target, relpath in ov.targets:
            outputs[ROOT / relpath] = render_role(role, target, ov)
    for relpath, style in EMBEDDED:
        path = ROOT / relpath
        if path.is_file():
            outputs[path] = upsert_embedded(path, style)
    for relpath, style in RIGOR_SITES:
        path = ROOT / relpath
        if path.is_file():
            text = outputs.get(path, path.read_text(encoding="utf-8"))
            outputs[path] = upsert_marked(text, style, RIGOR_MARKER, rigor_block(style))
    for source, dests in DOCUMENTS:
        for relpath, frontmatter in dests:
            outputs[ROOT / relpath] = render_document(source, frontmatter)
    return outputs


def all_output_sources() -> dict[Path, dict[str, str | None]]:
    """Return the prompt inputs responsible for every generated destination."""
    sources: dict[Path, dict[str, str | None]] = {}
    for flavor in sorted(p.stem for p in OVERLAYS_DIR.glob("*.md")):
        ov = load_overlay(flavor)
        for role, _target, relpath in ov.targets:
            sources[ROOT / relpath] = {
                "kind": "role",
                "prompt": f"prompts/canonical/{role}.md",
                "overlay": f"prompts/overlays/{flavor}.md",
            }
    for relpath, _style in EMBEDDED:
        path = ROOT / relpath
        if path.is_file():
            sources[path] = {
                "kind": "embedded",
                "prompt": "prompts/protocol-essentials.md",
                "overlay": None,
            }
    for source, dests in DOCUMENTS:
        for relpath, _frontmatter in dests:
            sources[ROOT / relpath] = {
                "kind": "document",
                "prompt": f"prompts/documents/{source}.md",
                "overlay": None,
            }
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify generated files match the tree; exit 1 on drift")
    args = parser.parse_args(argv)

    outputs = all_outputs()
    if args.check:
        drifted = []
        for path, content in sorted(outputs.items()):
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                drifted.append(f"{path.relative_to(ROOT)}  (missing)")
                continue
            if current != content:
                drifted.append(f"{path.relative_to(ROOT)}  (differs)")
        if drifted:
            print("prompt drift detected — run `python3 prompts/generate.py`:", file=sys.stderr)
            for line in drifted:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"prompt sync OK ({len(outputs)} generated files match the tree)")
        return 0

    written = 0
    for path, content in sorted(outputs.items()):
        rel = path.relative_to(ROOT)
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            print(f"  unchanged {rel}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written += 1
        print(f"  wrote {rel}")
    print(f"regenerated {written} of {len(outputs)} generated files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

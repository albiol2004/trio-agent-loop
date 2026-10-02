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
import json
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
#
# r19 C1 (slice-evals back to fast): the rigor is split. RIGOR_CORE is the
# cheap part every Omnigent evaluator dispatch carries (it is embedded in
# the registered role config and the per-dispatch prompt); RIGOR_INTEGRATION
# is the whole-goal part (attacks, independent probe, data-work re-run, "go
# beyond", the implement-then-smoke and receipt-family re-executions),
# generated into its own prompt file that trioctl appends only to
# integration-eval and lockstep evaluator prompts -- never to slice-evals.
RIGOR_CORE = [
    ("bullet", "Method", "**Suites outside the targeted check:**"),
    ("bullet", "Method", "**Test-integrity audit (mandatory):**"),
    ("bullet", "Method", "Prefer executing code over reading it"),
    ("bullet", "Anti-rubber-stamp rules", "If you did not run a criterion's check yourself"),
    ("section", "Evidence kinds", None),
    ("section", "Goal-derived pass/fail", None),
    ("section", "Closing unverified claims", None),
]
RIGOR_INTEGRATION = [
    ("section", "Data-work profile", None),
    ("bullet", "Method", "Run the acceptance checks yourself"),
    ("bullet", "Method", "No whole-goal SHIP"),
    ("section", "Whole-goal rigor", None),
    ("section", "Independent probe", None),
]
# Back-compat name (r18a): the full rigor, core then integration.
RIGOR_PIECES = RIGOR_CORE + RIGOR_INTEGRATION
RIGOR_INTRO = (
    "## Verification rigor\n"
    "Generated from the canonical Trio evaluator (prompts/canonical/evaluator.md);\n"
    "binding for every verdict you write -- open-loop slice sections and the\n"
    "integration verdict alike. Whole-goal verdicts (integration-eval,\n"
    "lockstep) also carry `## Whole-goal verification rigor`, which trioctl\n"
    "appends to those dispatch prompts only.\n"
)
INTEGRATION_RIGOR_PATH = "omnigent/entrypoints/trio-omnigent/prompts/integration-rigor.md"
#: The same generated block for the standalone OpenCode driver, which
#: appends it to its open-loop integration-eval per-call prompt (it must
#: never read files under omnigent/).
OPENCODE_DRIVER_INTEGRATION_RIGOR_PATH = "opencode-driver/prompts/integration-rigor.md"
INTEGRATION_RIGOR_INTRO = (
    "## Whole-goal verification rigor\n"
    "Generated from the canonical Trio evaluator (prompts/canonical/evaluator.md);\n"
    "binding for this whole-goal verdict (open-loop integration evaluation or\n"
    "lockstep), in addition to your role prompt's `## Verification rigor`.\n"
    "Open-loop slice sections never carry these duties.\n"
)


# r19 frozen acceptance (Omnigent side; the Claude-native N1-N4 seams reuse
# the same canonical sources later). Canonical sources, copied verbatim to
# the Omnigent per-dispatch prompt directory; trioctl renders the lead and
# evaluator fragments only while the acceptance switch is on.
ACCEPTANCE_PROMPTS = [
    ("acceptance.md", "omnigent/entrypoints/trio-omnigent/prompts/acceptance.md"),
    ("acceptance-lead.md", "omnigent/entrypoints/trio-omnigent/prompts/acceptance-lead.md"),
    ("acceptance-evaluator.md",
     "omnigent/entrypoints/trio-omnigent/prompts/acceptance-evaluator.md"),
]
#: The registered author agent: generated whole, its executor model copied
#: from the evaluator's registered config (same tier by construction;
#: install.sh re-templates it from the installed evaluator config).
ACCEPTANCE_ROLE_CONFIG = "omnigent/trio-omnigent-roles/acceptance/config.yaml"
EVALUATOR_ROLE_CONFIG = "omnigent/trio-omnigent-roles/evaluator/config.yaml"
_EXECUTOR_MODEL_RE = re.compile(r"^executor:\n(?:  .*\n)*?  model: (\S+)\s*$", re.M)


def acceptance_role_config() -> str:
    evaluator = (ROOT / EVALUATOR_ROLE_CONFIG).read_text(encoding="utf-8")
    m = _EXECUTOR_MODEL_RE.search(evaluator)
    if m is None:
        raise ValueError(f"{EVALUATOR_ROLE_CONFIG}: no executor model to template")
    body = (CANONICAL_DIR / "acceptance.md").read_text(encoding="utf-8")
    body = body.replace("`{export}`", "your workspace (the current directory)")
    body = body.replace("{export}", "your workspace")
    prompt = "\n".join(("  " + ln) if ln else "" for ln in body.rstrip("\n").splitlines())
    return (
        "spec_version: 1\n"
        "name: trio-omnigent-acceptance\n"
        "description: Independent Trio acceptance author (r19) that turns GOAL.md into frozen "
        "black-box checks before any code is written; same tier as the Evaluator.\n"
        "spawn: false\n"
        "executor:\n"
        "  type: omnigent\n"
        f"  model: {m.group(1)}\n"
        "  config: {harness: cursor-native, yolo: true}\n"
        "os_env:\n"
        "  type: caller_process\n"
        "  cwd: .\n"
        "  sandbox: {type: none}\n"
        "guardrails:\n"
        "  policies:\n"
        "    blast_radius:\n"
        "      type: function\n"
        "      on: [tool_call]\n"
        "      function:\n"
        "        path: omnigent.inner.nessie.policies.blast_radius\n"
        "        arguments: {gate_pushes: false}\n"
        "prompt: |\n"
        "  <!-- generated by prompts/generate.py from prompts/canonical/acceptance.md -->\n"
        + prompt + "\n"
    )


# r19 N1/N3 (Claude-native frozen acceptance). The native author agent is
# generated from the same canonical author prompt, its model taken from the
# `.claude` overlay's evaluator header (same tier by construction); the
# native Lead/Evaluator additions are embedded into the Workflow script as
# JS string constants (the script cannot read files).
CLAUDE_ACCEPTANCE_AGENT = ".claude/agents/trio-acceptance.md"
NATIVE_SCRIPT = "native/trio-native.js"
NATIVE_ACCEPTANCE_MARKER = "trio-native-acceptance"
NATIVE_ACCEPTANCE_FRAGMENTS = (
    ("ACC_LEAD_FRAGMENT", "acceptance-native-lead.md"),
    ("ACC_EVALUATOR_FRAGMENT", "acceptance-native-evaluator.md"),
)
_OMNIGENT_VALIDATE = ("End by running `trioctl omnigent acceptance validate --export .` from\n"
                      "`{export}`")
_NATIVE_VALIDATE = ("End by running the validate command your prompt gives\n"
                    "(`python3 <trio-acceptance.py> validate --export .` from `{export}`)")


def _claude_evaluator_model() -> str:
    text = (OVERLAYS_DIR / ".claude.md").read_text(encoding="utf-8")
    m = re.search(r"^name: trio-evaluator\n(?:.*\n)*?model: (\S+)\s*$", text, re.M)
    if m is None:
        raise ValueError("prompts/overlays/.claude.md: no trio-evaluator model to template")
    return m.group(1)


def _native_acceptance_body() -> str:
    """Canonical acceptance.md rewritten for a native (non-Omnigent)
    acceptance author: the validate sentence in its native form, and every
    `{export}` mention replaced by "your workspace" (a native author has no
    `{export}` substitution of its own -- its workspace just IS the export
    directory). Shared by every native acceptance-agent flavor (Claude,
    OpenCode); each flavor then adds its own intro sentence and
    frontmatter."""
    body = (CANONICAL_DIR / "acceptance.md").read_text(encoding="utf-8")
    if _OMNIGENT_VALIDATE not in body:
        raise ValueError("prompts/canonical/acceptance.md: the validate sentence changed; "
                         "update generate.py's native rewrite")
    body = body.replace(_OMNIGENT_VALIDATE, _NATIVE_VALIDATE)
    body = body.replace("in your workspace `{export}`", "in your workspace")
    body = body.replace("from `{export}`)", "in your workspace)")
    body = body.replace("`{export}`", "your workspace")
    body = body.replace("{export}", "your workspace")
    return body


def claude_acceptance_agent() -> str:
    body = _native_acceptance_body()
    body = body.replace("# Role: Acceptance Author — frozen acceptance, once per loop\n",
                        "# Role: Acceptance Author — frozen acceptance, once per loop\n\n"
                        "Your workspace is the export directory your prompt names (outside the "
                        "repository; your tools start elsewhere, so use its absolute path).\n", 1)
    return (
        "---\n"
        "name: trio-acceptance\n"
        "description: Independent Trio acceptance author (r19) for the trio-native workflow. Turns "
        "GOAL.md into frozen black-box checks before any code is written; same tier as the "
        "Evaluator. Never the Lead, the Evaluator or a builder.\n"
        f"model: {_claude_evaluator_model()}\n"
        "effort: high\n"
        "disallowedTools: Agent, WebFetch, WebSearch\n"
        "---\n"
        "<!-- generated by prompts/generate.py from prompts/canonical/acceptance.md -->\n\n"
        + body.lstrip("\n")
    )


# r19 (OpenCode standalone driver): the acceptance author body for
# trio-opencode. Another slice wires it into ocgen/driver; this one only
# generates the body. Minimal frontmatter (only `description:`) because the
# driver writes the real model/permissions itself, same as every other
# opencode-driver agent body.
OPENCODE_ACCEPTANCE_AGENT = "opencode-driver/agents/trio-acceptance.md"


def opencode_acceptance_agent() -> str:
    body = _native_acceptance_body()
    body = body.replace(
        "# Role: Acceptance Author — frozen acceptance, once per loop\n",
        "# Role: Acceptance Author — frozen acceptance, once per loop\n\n"
        "Your workspace is the export directory the prompt names: it is outside "
        "the repository and is your current working directory.\n", 1)
    return (
        "---\n"
        "description: Independent Trio acceptance author (r19) for the standalone trio-opencode "
        "driver. Turns GOAL.md into frozen black-box checks before any code is written; same tier "
        "as the Evaluator. Never the Lead, the Evaluator or a builder.\n"
        "---\n"
        "<!-- generated by prompts/generate.py from prompts/canonical/acceptance.md -->\n\n"
        + body.lstrip("\n")
    )


def native_acceptance_block() -> str:
    lines = [f"// {NATIVE_ACCEPTANCE_MARKER}:start"]
    for name, source in NATIVE_ACCEPTANCE_FRAGMENTS:
        text = (CANONICAL_DIR / source).read_text(encoding="utf-8")
        lines.append(f"const {name} = {json.dumps(text, ensure_ascii=False)}")
    lines.append(f"// {NATIVE_ACCEPTANCE_MARKER}:end")
    return "\n".join(lines) + "\n"


def native_script_content() -> str:
    path = ROOT / NATIVE_SCRIPT
    return upsert_marked(path.read_text(encoding="utf-8"), "ts", NATIVE_ACCEPTANCE_MARKER,
                         native_acceptance_block())


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


_FENCE_OPEN_RE = re.compile(r"^([`~])\1{2,}")


def _canonical_section(text: str, heading: str, *, source: str = "evaluator.md",
                       context: str = "evaluator rigor") -> str:
    """Body of the `## <heading>` section of *text* (up to the next `## `).
    *source*/*context* name the canonical file and the caller's error
    context, for a meaningful message when the anchor heading is missing --
    callers extracting from a different canonical file than evaluator.md
    pass their own. Fence tracking is CommonMark-length-aware (a closing
    fence needs >= the opening run's length of the same character) so a
    longer fence (e.g. ```` ` ```` wrapping a literal ``` example, as
    lead.md's open-loop retirement example does) is not mistaken for two
    separate fences that leave the parser stuck "inside" for the rest of
    the file."""
    lines = text.splitlines()
    fence_char: str | None = None
    fence_len = 0
    start: int | None = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if fence_char is not None:
            if set(stripped) == {fence_char} and len(stripped) >= fence_len:
                fence_char = None
            continue
        m = _FENCE_OPEN_RE.match(stripped)
        if m:
            fence_char = m.group(1)
            fence_len = len(stripped) - len(stripped.lstrip(fence_char))
            continue
        if not ln.startswith("## "):
            continue
        if start is not None:
            return "\n".join(lines[start:i]).strip("\n")
        if ln[3:].strip() == heading:
            start = i + 1
    if start is None:
        raise ValueError(f"{context}: canonical {source} has no `## {heading}` section")
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


def _rigor_text(pieces: list, intro: str) -> str:
    canonical = (CANONICAL_DIR / "evaluator.md").read_text(encoding="utf-8")
    parts = [intro.rstrip("\n")]
    bullets_by_heading: dict[str, list[str]] = {}
    order: list[tuple[str, str]] = []
    for kind, heading, prefix in pieces:
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


def rigor_content() -> str:
    """The Omnigent evaluator's `## Verification rigor` block (r18a L0,
    r19 C1: the core part only)."""
    return _rigor_text(RIGOR_CORE, RIGOR_INTRO)


def integration_rigor_content() -> str:
    """The whole-goal `## Whole-goal verification rigor` prompt (r19 C1)."""
    return _rigor_text(RIGOR_INTEGRATION, INTEGRATION_RIGOR_INTRO)


def rigor_block(style: str) -> str:
    return _marked_block(rigor_content().rstrip("\n"), style, RIGOR_MARKER)


# The canonical Trio lead's "## Goal-derived criteria" section, delivered to
# the Omnigent Lead the same way RIGOR_CORE reaches the Omnigent Evaluator:
# EXTRACTED from prompts/canonical/lead.md at generation time, never a hand
# copy, into the Lead's registered role config and its per-dispatch prompt.
LEAD_CRITERIA_MARKER = "trio-lead-criteria"
LEAD_CRITERIA_HEADING = "Goal-derived criteria"
LEAD_CRITERIA_SITES = [
    ("omnigent/trio-omnigent-roles/lead/config.yaml", "yaml"),
    ("omnigent/entrypoints/trio-omnigent/prompts/lead.md", "md"),
]
LEAD_CRITERIA_INTRO = (
    "## Goal-derived criteria\n"
    "Generated from the canonical Trio lead (prompts/canonical/lead.md);\n"
    "binding for every plan you write.\n"
)


def lead_criteria_content() -> str:
    """The Omnigent Lead's `## Goal-derived criteria` block, extracted from
    the canonical Trio lead."""
    canonical = (CANONICAL_DIR / "lead.md").read_text(encoding="utf-8")
    body = _canonical_section(canonical, LEAD_CRITERIA_HEADING,
                              source="lead.md", context="lead criteria")
    return LEAD_CRITERIA_INTRO.rstrip("\n") + "\n\n" + body + "\n"


def lead_criteria_block(style: str) -> str:
    return _marked_block(lead_criteria_content().rstrip("\n"), style, LEAD_CRITERIA_MARKER)


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
    for relpath, style in LEAD_CRITERIA_SITES:
        path = ROOT / relpath
        if path.is_file():
            text = outputs.get(path, path.read_text(encoding="utf-8"))
            outputs[path] = upsert_marked(text, style, LEAD_CRITERIA_MARKER,
                                          lead_criteria_block(style))
    outputs[ROOT / INTEGRATION_RIGOR_PATH] = integration_rigor_content()
    outputs[ROOT / OPENCODE_DRIVER_INTEGRATION_RIGOR_PATH] = integration_rigor_content()
    for source, dest in ACCEPTANCE_PROMPTS:
        outputs[ROOT / dest] = (CANONICAL_DIR / source).read_text(encoding="utf-8")
    outputs[ROOT / ACCEPTANCE_ROLE_CONFIG] = acceptance_role_config()
    outputs[ROOT / CLAUDE_ACCEPTANCE_AGENT] = claude_acceptance_agent()
    outputs[ROOT / OPENCODE_ACCEPTANCE_AGENT] = opencode_acceptance_agent()
    if (ROOT / NATIVE_SCRIPT).is_file():
        outputs[ROOT / NATIVE_SCRIPT] = native_script_content()
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
    for source, dest in ACCEPTANCE_PROMPTS:
        sources[ROOT / dest] = {"kind": "document",
                                "prompt": f"prompts/canonical/{source}", "overlay": None}
    sources[ROOT / ACCEPTANCE_ROLE_CONFIG] = {
        "kind": "document", "prompt": "prompts/canonical/acceptance.md", "overlay": None}
    sources[ROOT / CLAUDE_ACCEPTANCE_AGENT] = {
        "kind": "document", "prompt": "prompts/canonical/acceptance.md",
        "overlay": "prompts/overlays/.claude.md"}
    sources[ROOT / OPENCODE_ACCEPTANCE_AGENT] = {
        "kind": "document", "prompt": "prompts/canonical/acceptance.md", "overlay": None}
    if (ROOT / NATIVE_SCRIPT).is_file():
        sources[ROOT / NATIVE_SCRIPT] = {
            "kind": "embedded",
            "prompt": "prompts/canonical/acceptance-native-lead.md",
            "overlay": None}
    sources[ROOT / INTEGRATION_RIGOR_PATH] = {
        "kind": "document",
        "prompt": "prompts/canonical/evaluator.md",
        "overlay": None,
    }
    sources[ROOT / OPENCODE_DRIVER_INTEGRATION_RIGOR_PATH] = {
        "kind": "document",
        "prompt": "prompts/canonical/evaluator.md",
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

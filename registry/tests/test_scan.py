#!/usr/bin/env python3
"""Round-trip tests for the registry format layer (registry/scan.py).

Every construct exercised here was taken from a real harness file; the
`RealFileRoundTrip` sweep then re-checks the whole canonical tree, so a
parser regression fails against the actual corpus rather than fixtures.

Run: python3 -m unittest discover -s registry/tests -t .
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load_scan():
    spec = importlib.util.spec_from_file_location(
        "trio_registry_scan_test", REPO / "registry" / "scan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan = _load_scan()


class YamlRoundTripMixin:
    def assert_round_trip(self, text, expected=None):
        """parse(dump(parse(t))) == parse(t), with key order preserved."""
        first = scan.parse_yaml(text)
        if expected is not None:
            self.assertEqual(first, expected)
        dumped = scan.dump_yaml(first)
        second = scan.parse_yaml(dumped)
        self.assertEqual(second, first, f"round-trip changed value\n{dumped}")
        self.assertEqual(_key_order(second), _key_order(first),
                         f"round-trip changed key order\n{dumped}")
        return first, dumped


def _key_order(value):
    """Nested list-of-keys shape, so ordering is compared at every depth."""
    if isinstance(value, dict):
        return [(k, _key_order(v)) for k, v in value.items()]
    if isinstance(value, list):
        return [_key_order(v) for v in value]
    return None


# --------------------------------------------------------------------------
# Nested maps / quoted keys  (opencode permission blocks)
# --------------------------------------------------------------------------

OPENCODE_EVALUATOR = '''description: Independent adversarial Trio evaluator.
mode: subagent
hidden: true
permission:
  "*": deny
  read: allow
  edit:
    "*": deny
    "loop/VERDICT.md": allow
  bash:
    "*": deny
    "python3 metrics/trio-shadow.py *": allow
    "git status *": allow
  task:
    "*": deny
    trio-scout: allow
'''

OPENCODE_SCOUT = '''description: Read-only explorer.
mode: subagent
permission:
  "*": deny
  read: allow
  grep: allow
  glob: allow
  webfetch: allow
  edit: deny
  bash: deny
  task: deny
'''


class NestedMaps(unittest.TestCase, YamlRoundTripMixin):
    def test_evaluator_permission_block(self):
        fields, dumped = self.assert_round_trip(OPENCODE_EVALUATOR)
        perm = fields["permission"]
        self.assertIsInstance(perm, dict)
        self.assertEqual(perm["*"], "deny")
        self.assertEqual(perm["read"], "allow")
        self.assertIsInstance(perm["edit"], dict)
        self.assertEqual(perm["edit"]["loop/VERDICT.md"], "allow")
        self.assertEqual(perm["bash"]["python3 metrics/trio-shadow.py *"], "allow")
        self.assertEqual(perm["task"]["trio-scout"], "allow")
        self.assertNotIn("[object Object]", dumped)

    def test_evaluator_is_not_flattened(self):
        """The old parser produced {'permission': None, 'read': 'allow', ...}."""
        fields = scan.parse_yaml(OPENCODE_EVALUATOR)
        self.assertEqual(list(fields), ["description", "mode", "hidden", "permission"])
        self.assertNotIn("read", fields)
        self.assertNotIn("*", fields)

    def test_scout_flat_wildcard_permission(self):
        fields, _ = self.assert_round_trip(OPENCODE_SCOUT)
        self.assertEqual(fields["permission"]["*"], "deny")
        self.assertEqual(fields["permission"]["webfetch"], "allow")
        self.assertEqual(len(fields["permission"]), 8)

    def test_quoted_key_with_spaces_survives_serialization(self):
        fields = scan.parse_yaml(OPENCODE_EVALUATOR)
        dumped = scan.dump_yaml(fields)
        self.assertIn('"python3 metrics/trio-shadow.py *": allow', dumped)
        self.assertIn('"*": deny', dumped)

    def test_top_level_quoted_star_key(self):
        self.assert_round_trip('"*": deny\nread: allow\n',
                               {"*": "deny", "read": "allow"})

    def test_nested_key_order_is_preserved(self):
        fields = scan.parse_yaml(OPENCODE_EVALUATOR)
        self.assertEqual(list(fields["permission"]),
                         ["*", "read", "edit", "bash", "task"])
        again = scan.parse_yaml(scan.dump_yaml(fields))
        self.assertEqual(list(again["permission"]),
                         ["*", "read", "edit", "bash", "task"])


# --------------------------------------------------------------------------
# Block scalars  (omp output:, omnigent prompt:, folded descriptions)
# --------------------------------------------------------------------------

OMP_OUTPUT = '''name: trio-evaluator
description: Adversarial evaluator.
output: |
  {
    "type": "object",
    "required": ["verdict", "summary"],
    "properties": {
      "verdict": { "enum": ["SHIP", "ITERATE"] },
      "summary": { "type": "string", "description": "<=3 sentences" }
    }
  }
'''


class BlockScalars(unittest.TestCase, YamlRoundTripMixin):
    def test_omp_output_literal_block(self):
        fields, dumped = self.assert_round_trip(OMP_OUTPUT)
        payload = fields["output"]
        self.assertTrue(payload.startswith("{\n"))
        self.assertTrue(payload.endswith("}\n"))
        parsed = json.loads(payload)
        self.assertEqual(parsed["required"], ["verdict", "summary"])
        self.assertEqual(parsed["properties"]["verdict"]["enum"], ["SHIP", "ITERATE"])
        self.assertIn("output: |", dumped)

    def test_literal_block_keeps_inner_indentation(self):
        fields = scan.parse_yaml(OMP_OUTPUT)
        self.assertIn('  "type": "object",', fields["output"])

    def test_folded_clip_keeps_one_trailing_newline(self):
        text = 'description: >\n  Operate Railway infrastructure: sign up\n  or sign in to an account.\nname: use-railway\n'
        fields, _ = self.assert_round_trip(text)
        self.assertEqual(
            fields["description"],
            "Operate Railway infrastructure: sign up or sign in to an account.\n")

    def test_folded_strip_has_no_trailing_newline(self):
        text = 'description: >-\n  Creates and maintains Code Connect\n  template files.\n'
        fields, _ = self.assert_round_trip(text)
        self.assertEqual(fields["description"],
                         "Creates and maintains Code Connect template files.")

    def test_folded_blank_line_becomes_newline(self):
        text = 'description: >\n  First paragraph.\n\n  Second paragraph.\n'
        fields, _ = self.assert_round_trip(text)
        self.assertEqual(fields["description"],
                         "First paragraph.\nSecond paragraph.\n")

    def test_literal_chomp_clip(self):
        fields, dumped = self.assert_round_trip("prompt: |\n  line one\n  line two\n")
        self.assertEqual(fields["prompt"], "line one\nline two\n")
        self.assertIn("prompt: |\n", dumped)

    def test_literal_chomp_strip(self):
        fields, dumped = self.assert_round_trip("prompt: |-\n  line one\n  line two\n")
        self.assertEqual(fields["prompt"], "line one\nline two")
        self.assertIn("prompt: |-\n", dumped)

    def test_literal_chomp_keep(self):
        fields, dumped = self.assert_round_trip("prompt: |+\n  line one\n\n")
        self.assertEqual(fields["prompt"], "line one\n\n")
        self.assertIn("prompt: |+\n", dumped)

    def test_block_scalar_survives_marker_comments(self):
        text = "prompt: |\n  <!-- trio-protocol:start -->\n  # Role: Lead\n  <!-- trio-protocol:end -->\n"
        fields, _ = self.assert_round_trip(text)
        self.assertIn("<!-- trio-protocol:start -->", fields["prompt"])
        self.assertIn("# Role: Lead", fields["prompt"])


# --------------------------------------------------------------------------
# Lists, flow collections, scalars
# --------------------------------------------------------------------------


class ListsAndScalars(unittest.TestCase, YamlRoundTripMixin):
    def test_kimi_arguments_indented_list(self):
        self.assert_round_trip(
            "name: trio-init\ntype: prompt\narguments:\n  - goal\n",
            {"name": "trio-init", "type": "prompt", "arguments": ["goal"]})

    def test_multi_item_block_list(self):
        self.assert_round_trip("on:\n  - tool_call\n  - tool_result\n",
                               {"on": ["tool_call", "tool_result"]})

    def test_flow_list(self):
        self.assert_round_trip("on: [tool_call]\nenvironments: [local, cloud]\n",
                               {"on": ["tool_call"], "environments": ["local", "cloud"]})

    def test_flow_map(self):
        self.assert_round_trip(
            "config: {harness: cursor-native, yolo: true}\n",
            {"config": {"harness": "cursor-native", "yolo": True}})

    def test_booleans_ints_and_null(self):
        self.assert_round_trip(
            "spec_version: 1\nspawn: true\nhidden: false\nfallback: null\n",
            {"spec_version": 1, "spawn": True, "hidden": False, "fallback": None})

    def test_comma_string_is_not_split(self):
        """`spawns: a, b` is a comma STRING in these dialects, not a YAML list."""
        fields, _ = self.assert_round_trip("spawns: trio-builder, trio-scout\n")
        self.assertEqual(fields["spawns"], "trio-builder, trio-scout")

    def test_comment_lines_and_trailing_comments(self):
        fields = scan.parse_yaml("# leading comment\nname: x  # trailing\nmodel: y\n")
        self.assertEqual(fields, {"name": "x", "model": "y"})

    def test_hash_inside_quoted_value_is_not_a_comment(self):
        self.assert_round_trip('argument-hint: "count #items"\n',
                               {"argument-hint": "count #items"})

    def test_colon_in_value_is_quoted_on_dump(self):
        fields, dumped = self.assert_round_trip(
            "description: Invoked on VERDICT: ITERATE scope=local\n")
        self.assertEqual(fields["description"],
                         "Invoked on VERDICT: ITERATE scope=local")
        self.assertIn('"', dumped)

    def test_non_ascii_stays_literal(self):
        _, dumped = self.assert_round_trip("description: fixes — exactly\n")
        self.assertIn("—", dumped)
        self.assertNotIn("\\u2014", dumped)

    def test_deep_nesting(self):
        text = ("guardrails:\n  policies:\n    audit:\n      type: function\n"
                "      on: [tool_call]\n      function:\n"
                "        path: omnigent.policies.audit\n"
                "        arguments: {level: high}\n")
        fields, _ = self.assert_round_trip(text)
        self.assertEqual(
            fields["guardrails"]["policies"]["audit"]["function"]["arguments"],
            {"level": "high"})

    def test_empty_collections(self):
        self.assert_round_trip("tools: []\nconfig: {}\n", {"tools": [], "config": {}})


# --------------------------------------------------------------------------
# Frontmatter splitting
# --------------------------------------------------------------------------


class Frontmatter(unittest.TestCase):
    def test_no_fence_returns_text_unchanged(self):
        text = "# CLAUDE.md\n\nNo frontmatter here.\n"
        fields, body = scan.parse_frontmatter(text)
        self.assertEqual(fields, {})
        self.assertEqual(body, text)

    def test_empty_fields_dump_returns_body_verbatim(self):
        body = "# Instructions\n\nbody text\n"
        self.assertEqual(scan.dump_frontmatter({}, body), body)

    def test_blank_line_after_fence_is_preserved(self):
        text = "---\nname: x\n---\n\n# Body\n"
        fields, body = scan.parse_frontmatter(text)
        self.assertEqual(body, "\n# Body\n")
        self.assertEqual(scan.dump_frontmatter(fields, body), text)

    def test_frontmatter_round_trip(self):
        text = "---\nname: x\ndescription: y\n---\n\nbody\n"
        fields, body = scan.parse_frontmatter(text)
        rebuilt = scan.dump_frontmatter(fields, body)
        self.assertEqual(scan.parse_frontmatter(rebuilt), (fields, body))


# --------------------------------------------------------------------------
# TOML
# --------------------------------------------------------------------------

CODEX_AGENT = '''name = "trio-evaluator"
model = "gpt-5.6-luna"
model_reasoning_effort = "high"
description = "Adversarial evaluator; verifies and never repairs."
sandbox_mode = "read-only"
developer_instructions = """

# Role: Evaluator

Run the checks yourself. Use `git diff` and:

```bash
python3 metrics/trio-shadow.py --require-commits
```

## Method
- Verify, never repair.
"""
'''


class Toml(unittest.TestCase):
    def test_triple_quoted_developer_instructions_round_trip(self):
        fields = scan.parse_toml(CODEX_AGENT)
        self.assertEqual(fields["name"], "trio-evaluator")
        instructions = fields["developer_instructions"]
        self.assertTrue(instructions.startswith("\n# Role: Evaluator"),
                        repr(instructions[:40]))
        self.assertIn("```bash", instructions)
        self.assertIn("`git diff`", instructions)
        again = scan.parse_toml(scan.dump_toml(fields))
        self.assertEqual(again, fields)
        self.assertEqual(list(again), list(fields))

    def test_dump_toml_emits_valid_toml(self):
        fields = scan.parse_toml(CODEX_AGENT)
        tomllib.loads(scan.dump_toml(fields))

    def test_multiline_string_uses_triple_quotes_with_leading_newline(self):
        out = scan.dump_toml({"developer_instructions": "\nline\n"})
        self.assertIn('developer_instructions = """\n', out)
        self.assertEqual(scan.parse_toml(out)["developer_instructions"], "\nline\n")

    def test_embedded_triple_quote_is_escaped(self):
        value = 'text with """ inside\nand a newline'
        out = scan.dump_toml({"k": value})
        self.assertEqual(scan.parse_toml(out)["k"], value)

    def test_backslash_is_escaped(self):
        value = "a \\ backslash\nsecond line"
        self.assertEqual(scan.parse_toml(scan.dump_toml({"k": value}))["k"], value)

    def test_invalid_toml_raises_value_error(self):
        with self.assertRaises(ValueError):
            scan.parse_toml("name = ")

    def test_split_and_join_move_developer_instructions_to_body(self):
        fields, body = scan.split_file(CODEX_AGENT, "toml")
        self.assertNotIn("developer_instructions", fields)
        self.assertIn("# Role: Evaluator", body)
        rebuilt = scan.join_file(fields, body, "toml")
        self.assertEqual(scan.parse_toml(rebuilt), scan.parse_toml(CODEX_AGENT))

    def test_file_format_dispatch(self):
        self.assertEqual(scan.file_format("a/b.toml"), "toml")
        self.assertEqual(scan.file_format("a/b.md"), "yaml")
        self.assertEqual(scan.file_format(Path("x/SKILL.md")), "yaml")


# --------------------------------------------------------------------------
# Schema + templates
# --------------------------------------------------------------------------


class Schema(unittest.TestCase):
    def test_codex_agent_is_the_only_toml_surface(self):
        toml_surfaces = [k for k, v in scan.SURFACE_FORMAT.items() if v == "toml"]
        self.assertEqual(toml_surfaces, [("codex", "agent")])

    def test_opencode_agent_schema_has_no_name_key(self):
        keys = [f["key"] for f in scan.KEY_SCHEMA["opencode:agent"]]
        self.assertNotIn("name", keys)
        self.assertIn("permission", keys)

    def test_omp_command_schema_has_no_name_key(self):
        self.assertEqual([f["key"] for f in scan.KEY_SCHEMA["omp:command"]],
                         ["description"])

    def test_every_field_spec_is_well_formed(self):
        widgets = {"text", "textarea", "checkbox", "select", "list", "raw"}
        types = {"string", "bool", "int", "list", "map"}
        for key, specs in scan.KEY_SCHEMA.items():
            for spec in specs:
                with self.subTest(surface=key, field=spec["key"]):
                    self.assertEqual(set(spec),
                                     {"key", "type", "widget", "required", "enum", "help"})
                    self.assertIn(spec["widget"], widgets)
                    self.assertIn(spec["type"], types)
                    self.assertIsInstance(spec["required"], bool)
                    if spec["widget"] == "select":
                        self.assertTrue(spec["enum"])

    def test_codex_agent_template_is_valid_toml(self):
        text = scan.default_template("codex", "agent", "my-agent")
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["name"], "my-agent")
        self.assertIn("developer_instructions", parsed)

    def test_opencode_agent_template_has_no_name_key(self):
        text = scan.default_template("opencode", "agent", "my-agent")
        fields, _ = scan.parse_frontmatter(text)
        self.assertNotIn("name", fields)
        self.assertIn("description", fields)

    def test_claude_skill_template_has_name(self):
        fields, _ = scan.parse_frontmatter(
            scan.default_template("claude", "skill", "my-skill"))
        self.assertEqual(fields["name"], "my-skill")


# --------------------------------------------------------------------------
# The corpus sweep
# --------------------------------------------------------------------------

CANONICAL_ROOTS = (".claude", "codex", "kimi", "zcode", "opencode", "omp",
                   "omnigent/entrypoints")


def _canonical_files():
    for root in CANONICAL_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for pattern in ("*.md", "*.toml"):
            yield from sorted(base.rglob(pattern))


class RealFileRoundTrip(unittest.TestCase):
    """parse(dump(parse(x))) == parse(x) over the real canonical tree."""

    def test_corpus_is_not_empty(self):
        self.assertGreater(len(list(_canonical_files())), 30)

    def test_every_canonical_file_round_trips(self):
        checked = 0
        for path in _canonical_files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path.relative_to(REPO))):
                if path.suffix == ".toml":
                    fields = scan.parse_toml(text)
                    again = scan.parse_toml(scan.dump_toml(fields))
                    body_ok = True
                else:
                    if not scan.FRONTMATTER_RE.match(text):
                        continue
                    fields, body = scan.parse_frontmatter(text)
                    again, body2 = scan.parse_frontmatter(
                        scan.dump_frontmatter(fields, body))
                    body_ok = body2 == body
                self.assertEqual(again, fields)
                self.assertTrue(body_ok, "body changed on round-trip")
                self.assertEqual(_key_order(again), _key_order(fields))
            checked += 1
        self.assertGreater(checked, 30)

    def test_opencode_evaluator_on_disk_parses_nested(self):
        path = REPO / "opencode/agents/trio-evaluator.md"
        fields, _ = scan.parse_frontmatter(path.read_text(encoding="utf-8"))
        self.assertIsInstance(fields["permission"], dict)
        self.assertIsInstance(fields["permission"]["bash"], dict)
        self.assertTrue(any(" " in key for key in fields["permission"]["bash"]))

    def test_codex_evaluator_body_hash_is_whole_file(self):
        path = REPO / "codex/agents/trio-evaluator.toml"
        record = scan.entry_record(path, "codex", "agent", "canonical")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(record["body_hash"], scan.sha256(text.strip()))
        self.assertEqual(record["name"], "trio-evaluator")

    def test_entry_record_survives_malformed_frontmatter(self):
        broken = REPO / "registry" / "tests" / "__broken_tmp__.md"
        broken.write_text("---\n\tthis: [is not\n---\n\nbody\n", encoding="utf-8")
        try:
            record = scan.entry_record(broken, "claude", "skill", "canonical")
            self.assertIsInstance(record["frontmatter"], dict)
        finally:
            broken.unlink()


if __name__ == "__main__":
    unittest.main()

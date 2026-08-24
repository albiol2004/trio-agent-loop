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
# Strict YAML (opt-in validation path for the serialize endpoint's raw-$yaml
# sub-editor; the lenient parse_yaml default must stay unchanged)
# --------------------------------------------------------------------------


class StrictYaml(unittest.TestCase):
    def test_default_lenient_mode_is_unaffected(self):
        # Same malformed input the strict-mode tests below reject: lenient
        # parse_yaml() must keep coercing it, never raise, for scan_* safety.
        text = 'a:\n    b: 1\n  c: 2\n'
        self.assertEqual(scan.parse_yaml(text), {"a": {"b": 1}})
        self.assertEqual(scan.parse_yaml(text, strict=False), {"a": {"b": 1}})

    def test_strict_valid_input_parses_normally(self):
        fields = scan.parse_yaml(OPENCODE_EVALUATOR, strict=True)
        self.assertEqual(fields["mode"], "subagent")
        self.assertEqual(fields["permission"]["bash"]["git status *"], "allow")

    def test_strict_rejects_mis_indented_nested_line(self):
        # One line under "a:" indented two rather than four spaces: lenient
        # mode silently drops "c" (see VERDICT.md iteration 1).
        with self.assertRaises(ValueError):
            scan.parse_yaml('a:\n    b: 1\n  c: 2\n', strict=True)

    def test_strict_rejects_top_level_sequence(self):
        # Lenient mode returns {} (drops everything) for a document whose
        # top level is a sequence, not a mapping.
        with self.assertRaises(ValueError):
            scan.parse_yaml('- a\nb: 1\n', strict=True)

    def test_strict_rejects_non_key_value_line(self):
        with self.assertRaises(ValueError):
            scan.parse_yaml('a: 1\nthis is not yaml\n', strict=True)

    def test_strict_rejects_tab_indentation(self):
        with self.assertRaises(ValueError):
            scan.parse_yaml('a:\n\tb: 1\n', strict=True)

    def test_strict_rejects_duplicate_key(self):
        with self.assertRaises(ValueError):
            scan.parse_yaml('read: allow\nread: deny\n', strict=True)

    def test_strict_rejects_unclosed_flow_collection(self):
        with self.assertRaises(ValueError):
            scan.parse_yaml('bad: [unclosed\n', strict=True)

    def test_strict_rejects_unterminated_quote(self):
        with self.assertRaises(ValueError):
            scan.parse_yaml('a: "unterminated\n', strict=True)


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
        widgets = {
            "text", "textarea", "checkbox", "select", "list", "raw",
            "permission-grid", "spawns-select", "json-schema",
        }
        types = {"string", "bool", "int", "list", "map"}
        for key, specs in scan.KEY_SCHEMA.items():
            for spec in specs:
                with self.subTest(surface=key, field=spec["key"]):
                    self.assertEqual(set(spec),
                                     {"key", "type", "widget", "required", "enum",
                                      "help", "values_from"})
                    self.assertIn(spec["widget"], widgets)
                    self.assertIn(spec["type"], types)
                    self.assertIsInstance(spec["required"], bool)
                    if spec["widget"] == "select":
                        self.assertTrue(spec["enum"])

    def test_schema_catalog_contains_required_surfaces(self):
        expected = {
            "claude:skill", "claude:command", "claude:agent",
            "codex:agent", "omp:agent", "omp:command",
            "opencode:agent", "opencode:command", "kimi:skill",
            "zcode:skill",
        }
        self.assertTrue(
            expected.issubset(scan.KEY_SCHEMA),
            f"missing surfaces: {expected - scan.KEY_SCHEMA.keys()}")

    def test_schema_enums_match_brief(self):
        expected = {
            ("claude:agent", "effort"): ["low", "medium", "high"],
            ("codex:agent", "model_reasoning_effort"):
                ["low", "medium", "high"],
            ("codex:agent", "sandbox_mode"):
                ["read-only", "workspace-write", "danger-full-access"],
            ("opencode:agent", "mode"): ["subagent", "primary"],
            ("kimi:skill", "type"): ["prompt"],
        }
        for (surface, key), enum in expected.items():
            with self.subTest(surface=surface, field=key):
                spec = next(
                    field for field in scan.KEY_SCHEMA[surface]
                    if field["key"] == key)
                self.assertEqual(spec["enum"], enum)

    def test_schema_model_fields_use_model_sources(self):
        expected = {
            "claude:agent": "models:claude",
            "codex:agent": "models:codex",
            "omp:agent": "models:omp",
        }
        for surface, values_from in expected.items():
            with self.subTest(surface=surface):
                model = next(
                    field for field in scan.KEY_SCHEMA[surface]
                    if field["key"] == "model")
                self.assertEqual(model["values_from"], values_from)

        for surface, fields in scan.KEY_SCHEMA.items():
            for field in fields:
                if (surface, field["key"]) not in {
                    (surface, "model") for surface in expected
                }:
                    with self.subTest(surface=surface, field=field["key"]):
                        self.assertIsNone(field["values_from"])

    def test_schema_uses_specialized_widgets(self):
        expected = {
            ("opencode:agent", "permission"): "permission-grid",
            ("omp:agent", "spawns"): "spawns-select",
            ("omp:agent", "output"): "json-schema",
        }
        for (surface, key), widget in expected.items():
            with self.subTest(surface=surface, field=key):
                spec = next(
                    field for field in scan.KEY_SCHEMA[surface]
                    if field["key"] == key)
                self.assertEqual(spec["widget"], widget)

    def test_schema_help_matches_brief_for_restricted_fields(self):
        claude_tools = next(
            field for field in scan.KEY_SCHEMA["claude:agent"]
            if field["key"] == "disallowedTools")
        self.assertIn("denylist", claude_tools["help"])
        self.assertIn("no positive tools list", claude_tools["help"])

        sandbox = next(
            field for field in scan.KEY_SCHEMA["codex:agent"]
            if field["key"] == "sandbox_mode")
        self.assertIn("optional", sandbox["help"])
        self.assertIn("read-only", sandbox["help"])
        self.assertIn("trio-scout", sandbox["help"])

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


# --------------------------------------------------------------------------
# generated_paths() - which files prompts/generate.py owns
# --------------------------------------------------------------------------


class GeneratedPaths(unittest.TestCase):
    def setUp(self):
        scan.reset_generated_paths_cache()
        self.addCleanup(scan.reset_generated_paths_cache)

    def test_non_empty_and_contains_known_generated_files(self):
        paths = scan.generated_paths()
        self.assertIsInstance(paths, frozenset)
        self.assertGreater(len(paths), 0)
        self.assertIn(str((REPO / ".claude/agents/trio-lead.md").resolve()), paths)
        self.assertIn(str((REPO / "codex/agents/trio-lead.toml").resolve()), paths)

    def test_every_member_is_an_absolute_existing_path(self):
        for p in scan.generated_paths():
            self.assertIsInstance(p, str)
            self.assertTrue(Path(p).is_absolute(), p)
            self.assertTrue(Path(p).exists(), p)

    def test_result_is_memoized(self):
        first = scan.generated_paths()
        second = scan.generated_paths()
        self.assertIs(first, second)
        scan.reset_generated_paths_cache()
        third = scan.generated_paths()
        self.assertEqual(third, first)
        self.assertIsNot(third, first)


class ManagedFlag(unittest.TestCase):
    def setUp(self):
        scan.reset_generated_paths_cache()
        self.addCleanup(scan.reset_generated_paths_cache)

    def test_generated_file_reports_managed_true(self):
        record = scan.entry_record(
            REPO / ".claude/agents/trio-lead.md", "claude", "agent", "canonical")
        self.assertTrue(record["managed"])

    def test_non_generated_canonical_file_reports_managed_false(self):
        # trio-init is a canonical skill that generate.py does not own (its
        # SKILL.md is hand-authored, not rendered from prompts/canonical/*).
        path = REPO / ".claude/skills/trio-init/SKILL.md"
        self.assertNotIn(str(path.resolve()), scan.generated_paths())
        record = scan.entry_record(path, "claude", "skill", "canonical")
        self.assertFalse(record["managed"])

    def test_explicit_managed_true_still_works(self):
        path = REPO / ".claude/skills/trio-init/SKILL.md"
        record = scan.entry_record(path, "claude", "skill", "canonical", managed=True)
        self.assertTrue(record["managed"])


class HashStabilityGuard(unittest.TestCase):
    """The managed-flag change must not move frontmatter_hash/body_hash."""

    def test_collect_canonical_hashes_match_direct_recomputation(self):
        entries = scan.collect_canonical()
        self.assertGreater(len(entries), 30)
        for e in entries:
            path = Path(e["path"])
            text = path.read_text(encoding="utf-8", errors="replace")
            if scan.file_format(path) == "toml":
                try:
                    fields = scan.parse_toml(text)
                except ValueError:
                    fields = {}
                body = text
            else:
                fields, body = scan.parse_frontmatter(text)
            with self.subTest(path=str(path.relative_to(REPO))):
                self.assertEqual(
                    e["frontmatter_hash"],
                    scan.sha256(json.dumps(fields, sort_keys=True, default=str)))
                self.assertEqual(e["body_hash"], scan.sha256(body.strip()))


# --------------------------------------------------------------------------
# build_index() widening: agent grouping + agent_matrix
# --------------------------------------------------------------------------


def _entry(name, harness, surface, scope, body_hash, managed=False, path=None):
    return {
        "name": name,
        "path": path or f"/fake/{harness}/{surface}/{name}",
        "harness": harness,
        "surface": surface,
        "scope": scope,
        "managed": managed,
        "frontmatter": {},
        "frontmatter_hash": "ffff",
        "body_hash": body_hash,
        "is_symlink": False,
        "size": 0,
    }


class BuildIndexAgentGrouping(unittest.TestCase):
    def setUp(self):
        self.entries = [
            _entry("trio", "claude", "skill", "canonical", "aaaa"),
            _entry("trio", "claude", "skill", "global", "aaaa"),
            _entry("trio-lead", "claude", "agent", "canonical", "bbbb"),
            _entry("trio-lead", "claude", "agent", "global", "bbbb"),
            _entry("trio-lead", "codex", "agent", "canonical", "cccc"),
            _entry("trio-lead", "codex", "agent", "global", "dddd"),
            _entry("CLAUDE.md", "claude", "instructions", "global", "eeee"),
        ]
        self.index = scan.build_index(self.entries)

    def test_agent_group_appears(self):
        names = {g["name"] for g in self.index["groups"]}
        self.assertIn("trio-lead", names)

    def test_agent_group_statuses(self):
        group = next(g for g in self.index["groups"] if g["name"] == "trio-lead")
        by_harness_scope = {(i["harness"], i["scope"]): i["status"]
                            for i in group["installations"]}
        self.assertEqual(by_harness_scope[("claude", "canonical")], "canonical")
        self.assertEqual(by_harness_scope[("claude", "global")], "in-sync")
        self.assertEqual(by_harness_scope[("codex", "canonical")], "canonical")
        self.assertEqual(by_harness_scope[("codex", "global")], "stale")

    def test_stale_list_contains_the_agent_mismatch(self):
        self.assertIn("trio-lead (codex/agent)", self.index["stale"])

    def test_instructions_entry_is_not_grouped(self):
        names = {g["name"] for g in self.index["groups"]}
        self.assertNotIn("CLAUDE.md", names)
        total_installations = sum(len(g["installations"]) for g in self.index["groups"])
        self.assertEqual(total_installations, len(self.entries) - 1)


class BuildIndexSkillCommandRegression(unittest.TestCase):
    """Skill/command grouping must be byte-identical to the pre-widening shape."""

    def test_exact_groups_shape_for_skill_entries(self):
        entries = [
            _entry("trio", "claude", "skill", "canonical", "aaaa"),
            _entry("trio", "claude", "skill", "global", "aaaa"),
            _entry("trio", "codex", "skill", "global", "zzzz"),
        ]
        index = scan.build_index(entries)
        self.assertEqual(index["groups"], [{
            "name": "trio",
            "installations": [
                {"harness": "claude", "surface": "skill", "scope": "canonical",
                 "path": entries[0]["path"], "body_hash": "aaaa",
                 "is_symlink": False, "managed": False, "status": "canonical"},
                {"harness": "claude", "surface": "skill", "scope": "global",
                 "path": entries[1]["path"], "body_hash": "aaaa",
                 "is_symlink": False, "managed": False, "status": "in-sync"},
                {"harness": "codex", "surface": "skill", "scope": "global",
                 "path": entries[2]["path"], "body_hash": "zzzz",
                 "is_symlink": False, "managed": False, "status": "unknown"},
            ],
            "harnesses": ["claude", "codex"],
        }])
        self.assertEqual(index["stale"], [])


class BuildIndexAgentMatrix(unittest.TestCase):
    def test_no_canonical_agents_arg_gives_empty_matrix(self):
        entries = [_entry("trio", "claude", "skill", "canonical", "aaaa")]
        index = scan.build_index(entries)
        self.assertEqual(index["agent_matrix"], [])

    def test_empty_canonical_agents_list_gives_empty_matrix(self):
        index = scan.build_index([], [])
        self.assertEqual(index["agent_matrix"], [])

    def test_four_statuses_and_cell_ordering(self):
        entries = [
            # in-sync candidate for claude
            _entry("registry-scout", "claude", "agent", "global", "hash1",
                   path="/home/u/.claude/agents/registry-scout.md"),
            # stale candidate for codex (different body_hash than render)
            _entry("registry-scout", "codex", "agent", "global", "hash-old",
                   path="/home/u/.codex/agents/registry-scout.toml"),
            # (no candidate at all for omp -> missing)
            # (opencode not in renders/unsupported at all)
        ]
        canonical_agents = [{
            "name": "registry-scout",
            "path": "/repo/registry/canonical-agents/registry-scout.md",
            "surface": "canonical-agent",
            "description": "Read-only registry scout.",
            "model_tier": "cheap",
            "tool_policy": "read-only",
            "renders": {
                "claude": {"filename": "registry-scout.md", "format": "yaml",
                           "body_hash": "hash1", "frontmatter": {}},
                "codex": {"filename": "registry-scout.toml", "format": "toml",
                         "body_hash": "hash2", "frontmatter": {}},
                "omp": {"filename": "registry-scout.md", "format": "yaml",
                       "body_hash": "hash3", "frontmatter": {}},
            },
            "unsupported": {"omnigent": "role-based, no per-file agent surface"},
        }]
        index = scan.build_index(entries, canonical_agents)
        self.assertEqual(len(index["agent_matrix"]), 1)
        row = index["agent_matrix"][0]
        self.assertEqual(row["name"], "registry-scout")
        self.assertEqual(row["description"], "Read-only registry scout.")
        self.assertEqual(row["model_tier"], "cheap")
        self.assertEqual(row["tool_policy"], "read-only")
        self.assertEqual(row["harnesses"], ["claude", "codex", "omp", "omnigent"])
        self.assertEqual([c["harness"] for c in row["cells"]],
                         ["claude", "codex", "omp", "omnigent"])
        by_harness = {c["harness"]: c for c in row["cells"]}
        self.assertEqual(by_harness["claude"]["status"], "in-sync")
        self.assertEqual(by_harness["claude"]["path"],
                         "/home/u/.claude/agents/registry-scout.md")
        self.assertIsNone(by_harness["claude"]["reason"])
        self.assertEqual(by_harness["codex"]["status"], "stale")
        self.assertEqual(by_harness["codex"]["path"],
                         "/home/u/.codex/agents/registry-scout.toml")
        self.assertEqual(by_harness["omp"]["status"], "missing")
        self.assertIsNone(by_harness["omp"]["path"])
        self.assertEqual(by_harness["omp"]["filename"], "registry-scout.md")
        self.assertEqual(by_harness["omnigent"]["status"], "unsupported")
        self.assertIsNone(by_harness["omnigent"]["path"])
        self.assertIsNone(by_harness["omnigent"]["filename"])
        self.assertEqual(by_harness["omnigent"]["reason"],
                         "role-based, no per-file agent surface")
        # reason is populated exactly for the unsupported cell
        for harness in ("claude", "codex", "omp"):
            self.assertIsNone(by_harness[harness]["reason"])

    def test_rows_sorted_by_name(self):
        canonical_agents = [
            {"name": "zeta", "path": "/repo/z.md", "surface": "canonical-agent",
             "description": "", "model_tier": "cheap", "tool_policy": "read-only",
             "renders": {"claude": {"filename": "zeta.md", "format": "yaml",
                                     "body_hash": "z", "frontmatter": {}}},
             "unsupported": {}},
            {"name": "alpha", "path": "/repo/a.md", "surface": "canonical-agent",
             "description": "", "model_tier": "cheap", "tool_policy": "read-only",
             "renders": {"claude": {"filename": "alpha.md", "format": "yaml",
                                     "body_hash": "a", "frontmatter": {}}},
             "unsupported": {}},
        ]
        index = scan.build_index([], canonical_agents)
        self.assertEqual([r["name"] for r in index["agent_matrix"]], ["alpha", "zeta"])

    def test_first_matching_entry_wins_when_several_candidates(self):
        entries = [
            _entry("dup", "claude", "agent", "global", "first-hash", path="/first"),
            _entry("dup", "claude", "agent", "global", "second-hash", path="/second"),
        ]
        canonical_agents = [{
            "name": "dup", "path": "/repo/dup.md", "surface": "canonical-agent",
            "description": "", "model_tier": "cheap", "tool_policy": "read-only",
            "renders": {"claude": {"filename": "dup.md", "format": "yaml",
                                   "body_hash": "second-hash", "frontmatter": {}}},
            "unsupported": {},
        }]
        index = scan.build_index(entries, canonical_agents)
        cell = index["agent_matrix"][0]["cells"][0]
        # first entry in `entries` order wins even though it does not match
        # the render hash, per the documented deterministic tie-break.
        self.assertEqual(cell["path"], "/first")
        self.assertEqual(cell["status"], "stale")


if __name__ == "__main__":
    unittest.main()

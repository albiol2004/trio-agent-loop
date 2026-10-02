"""Transcript pane rendering of Claude Code, Codex and omp/Omnigent records.

Runs the real dashboard/app.js appendRecord() in Node (vm context with a
minimal fake DOM, see transcript_render_harness.cjs) and checks what ends up
in the transcript view.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "transcript_render_harness.cjs"


def render(records):
    proc = subprocess.run(
        ["node", str(HARNESS)],
        input=json.dumps({"records": records}),
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "TZ": "UTC"},
    )
    if proc.returncode != 0:
        raise AssertionError("harness failed: " + proc.stderr)
    return json.loads(proc.stdout)


def claude_user(content, **extra):
    return {"type": "user", "timestamp": "2026-01-01T10:00:00.000Z",
            "message": {"role": "user", "content": content}, **extra}


def claude_assistant(content):
    return {"type": "assistant", "timestamp": "2026-01-01T10:00:01.000Z",
            "message": {"role": "assistant", "content": content}}


def codex(rtype, payload):
    return {"timestamp": "2026-01-01T10:00:02.000Z", "type": rtype, "payload": payload}


OMP_RECORDS = [
    {"type": "message", "timestamp": "2026-01-01T10:00:00.000Z",
     "message": {"role": "user", "content": [{"type": "text", "text": "omp question"}]}},
    {"type": "message", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "omp answer"},
        {"type": "thinking", "thinking": "omp thought"},
        {"type": "toolCall", "name": "bash", "intent": "list files",
         "arguments": {"command": "ls"}}]}},
    {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
                                    "isError": True,
                                    "content": [{"type": "text", "text": "boom"}]}},
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "omnigent says"}]},
    {"type": "custom", "customType": "tool_execution_start"},
    {"type": "custom", "customType": "note"},
    {"type": "session", "title": "T", "cwd": "/x"},
    {"type": "model_change", "model": "m1"},
    {"type": "compaction", "summary": "folded"},
    {"agent_name": "a", "title": "t"},
]

# Captured from the pre-change app.js: [className, tag, text] per block, per
# record (times render in UTC, see render()).
OMP_EXPECTED = [
    [["tr-msg tr-user", "user", "user10:00:00omp question"]],
    [["tr-msg tr-assistant", "assistant", "assistantomp answer"],
     ["tr-thinking", "thinking", "thinkingomp thoughtomp thought"],
     ["tr-tool", None, "bashlist files{\n  \"command\": \"ls\"\n}"]],
    [["tr-result is-error", None, "error \u00b7 bashboomboom"]],
    [["tr-msg tr-assistant", "assistant", "assistantomnigent says"]],
    [],
    [["tr-meta", "note", "note"]],
    [["tr-meta", "session", "sessionT"]],
    [["tr-meta", "model", "modelm1"]],
    [["tr-meta", "compacted", "compactedfolded"]],
    [["tr-meta", "session", "sessiona \u00b7 t"]],
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TranscriptRenderTests(unittest.TestCase):
    def test_claude_user_string(self):
        out = render([claude_user("hello live")])
        blocks = out["records"][0]["blocks"]
        self.assertIsNone(out["records"][0]["error"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["className"], "tr-msg tr-user")
        self.assertEqual(blocks[0]["tag"], "user")
        self.assertIn("hello live", blocks[0]["text"])

    def test_claude_assistant_text_thinking_tool(self):
        out = render([claude_assistant([
            {"type": "text", "text": "answer A"},
            {"type": "thinking", "thinking": "ponder", "signature": "x"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "ls -la", "description": "list"}},
        ])])
        rec = out["records"][0]
        self.assertIsNone(rec["error"])
        classes = [b["className"] for b in rec["blocks"]]
        self.assertEqual(classes, ["tr-msg tr-assistant", "tr-thinking", "tr-tool"])
        self.assertIn("answer A", rec["blocks"][0]["text"])
        self.assertIn("ponder", rec["blocks"][1]["text"])
        self.assertIn("Bash", rec["blocks"][2]["text"])
        self.assertIn("ls -la", rec["blocks"][2]["text"])

    def test_claude_tool_result(self):
        out = render([
            claude_assistant([{"type": "tool_use", "id": "t1", "name": "Bash",
                               "input": {"command": "ls"}}]),
            claude_user([{"type": "tool_result", "tool_use_id": "t1",
                          "content": "file1 file2", "is_error": False}]),
            claude_user([{"type": "tool_result", "tool_use_id": "t1",
                          "content": [{"type": "text", "text": "nope"}],
                          "is_error": True}]),
        ])
        ok = out["records"][1]["blocks"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["className"], "tr-result")
        self.assertIn("file1", ok[0]["text"])
        self.assertIn("Bash", ok[0]["text"])  # name resolved from the tool_use id
        bad = out["records"][2]["blocks"]
        self.assertEqual(bad[0]["className"], "tr-result is-error")
        self.assertIn("nope", bad[0]["text"])

    def test_claude_user_blocks_and_meta(self):
        out = render([
            claude_user([{"type": "text", "text": "part one"},
                         {"type": "text", "text": "part two"}]),
            claude_user("<local-command-stdout>ok</local-command-stdout>", isMeta=True),
        ])
        first = out["records"][0]["blocks"]
        self.assertEqual(first[0]["className"], "tr-msg tr-user")
        self.assertIn("part one", first[0]["text"])
        self.assertIn("part two", first[0]["text"])
        meta = out["records"][1]["blocks"]
        self.assertEqual([b["className"] for b in meta], ["tr-meta"])

    def test_claude_other_records_are_single_meta_rows(self):
        records = [
            {"type": "attachment", "attachment": {"type": "hook_success", "content": "hi"}},
            {"type": "system", "content": "compacted the thing", "subtype": "info"},
            {"type": "summary", "summary": "A short summary", "leafUuid": "u"},
            {"type": "queue-operation", "operation": "enqueue", "content": "later"},
            {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {}}},
            {"type": "progress", "data": {"type": "hook_progress"}},
            {"type": "something-new"},
            {"type": "attachment", "attachment": None},
            claude_user("still rendered"),
        ]
        out = render(records)
        for rec in out["records"][:-1]:
            self.assertIsNone(rec["error"])
            self.assertEqual([b["className"] for b in rec["blocks"]], ["tr-meta"])
        self.assertIn("compacted the thing", out["records"][1]["blocks"][0]["text"])
        self.assertIn("A short summary", out["records"][2]["blocks"][0]["text"])
        last = out["records"][-1]
        self.assertIsNone(last["error"])
        self.assertIn("still rendered", last["blocks"][0]["text"])

    def test_codex_message_function_call_and_output(self):
        out = render([
            codex("session_meta", {"id": "abc", "cwd": "/work/repo"}),
            codex("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text",
                                                 "text": "codex says hi"}]}),
            codex("response_item", {"type": "function_call", "name": "shell",
                                    "call_id": "c1",
                                    "arguments": json.dumps({"command": ["ls"]})}),
            codex("response_item", {"type": "function_call_output", "call_id": "c1",
                                    "output": "out1"}),
        ])
        recs = out["records"]
        for rec in recs:
            self.assertIsNone(rec["error"])
        self.assertEqual(recs[0]["blocks"][0]["className"], "tr-meta")
        self.assertIn("/work/repo", recs[0]["blocks"][0]["text"])
        msg = recs[1]["blocks"][0]
        self.assertEqual(msg["className"], "tr-msg tr-assistant")
        self.assertIn("codex says hi", msg["text"])
        call = recs[2]["blocks"][0]
        self.assertEqual(call["className"], "tr-tool")
        self.assertIn("shell", call["text"])
        self.assertIn("ls", call["text"])
        res = recs[3]["blocks"][0]
        self.assertEqual(res["className"], "tr-result")
        self.assertIn("out1", res["text"])
        self.assertIn("shell", res["text"])

    def test_codex_other_records(self):
        out = render([
            codex("response_item", {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "do it"}]}),
            codex("response_item", {"type": "message", "role": "developer",
                                    "content": [{"type": "input_text", "text": "rules"}]}),
            codex("response_item", {"type": "reasoning",
                                    "summary": [{"type": "summary_text", "text": "weighing"}]}),
            codex("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                    "input": "*** Begin Patch"}),
            codex("response_item", {"type": "custom_tool_call_output",
                                    "output": {"content": "patched"}}),
            codex("event_msg", {"type": "token_count"}),
            codex("turn_context", {"cwd": "/x"}),
            codex("compacted", {"message": "m"}),
        ])
        recs = out["records"]
        for rec in recs:
            self.assertIsNone(rec["error"])
        classes = [[b["className"] for b in r["blocks"]] for r in recs]
        self.assertEqual(classes, [
            ["tr-msg tr-user"], ["tr-meta"], ["tr-thinking"], ["tr-tool"],
            ["tr-result"], ["tr-meta"], ["tr-meta"], ["tr-meta"]])
        self.assertIn("weighing", recs[2]["blocks"][0]["text"])
        self.assertIn("Begin Patch", recs[3]["blocks"][0]["text"])
        self.assertIn("patched", recs[4]["blocks"][0]["text"])
        self.assertIn("token_count", recs[5]["blocks"][0]["text"])

    def test_omp_and_omnigent_unchanged(self):
        out = render(OMP_RECORDS)
        got = [[[b["className"], b["tag"], b["text"]] for b in r["blocks"]]
               for r in out["records"]]
        self.assertEqual(got, OMP_EXPECTED)
        self.assertTrue(all(r["error"] is None for r in out["records"]))


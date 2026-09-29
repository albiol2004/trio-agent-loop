#!/usr/bin/env python3
"""One real, tiny, read-only invocation of each diagnosis CLI (opt-in).

Runs only with ``TRIO_DASH_REAL_CLI=1`` (it makes one provider request per
CLI). It uses the exact argv the dashboard builds
(``loop_actions.harness_command``) with a trivial prompt that asks the agent
to read one file and to try to overwrite it, then checks that the answer
parses, the model/flags were accepted, and the file is unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "trio_dash_loop_actions_real", REPO_ROOT / "dashboard" / "loop_actions.py")
la = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = la
spec.loader.exec_module(la)

PROMPT = ('Read the file probe.txt in the workspace, then try to overwrite it with the '
          'word bye using any tool you have. Reply with ONLY a JSON object '
          '{"diagnosis": "<what probe.txt contains>", "state": "unknown", "evidence": [], '
          '"proposed_fix": {"id": "none"}, "needs_human_input": false, "overwrote": true or false}')


@unittest.skipUnless(os.environ.get("TRIO_DASH_REAL_CLI") == "1",
                     "set TRIO_DASH_REAL_CLI=1 to call the real cursor-agent/codex once each")
class RealCliTests(unittest.TestCase):
    def run_harness(self, harness: str) -> tuple[dict, str, str]:
        cfg = la.harnesses(Path.home())[harness]
        self.assertTrue(cfg["available"], f"{harness} CLI missing")
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            (work / "probe.txt").write_text("hello-from-trio-dash\n", encoding="utf-8")
            last = work.parent / f"{work.name}.last"
            argv, stdin = la.harness_command(harness, cfg, work, PROMPT, last)
            proc = subprocess.run(argv, cwd=work, input=stdin, capture_output=True, text=True,
                                  timeout=300, stdin=None if stdin else subprocess.DEVNULL)
            final = ""
            for line in proc.stdout.splitlines():
                _summary, text = la._event_summary(harness, line)
                if text:
                    final = text
            if harness == "codex" and last.exists():
                final = last.read_text() or final
                last.unlink()
            content = (work / "probe.txt").read_text(encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        parsed = la.extract_json(final)
        self.assertIsNotNone(parsed, final[-2000:])
        return parsed, content, proc.stdout

    def test_cursor_agent_ask_mode_grok_low(self):
        parsed, content, stdout = self.run_harness("cursor")
        self.assertEqual(content, "hello-from-trio-dash\n")  # write refused
        self.assertIn("hello-from-trio-dash", parsed["diagnosis"])
        init = json.loads(stdout.splitlines()[0])
        self.assertEqual(init.get("model"), "Grok 4.6 Low")
        print("cursor:", json.dumps(parsed))

    def test_codex_read_only_gpt6_luna_high(self):
        parsed, content, stdout = self.run_harness("codex")
        self.assertEqual(content, "hello-from-trio-dash\n")  # read-only sandbox
        self.assertIn("hello-from-trio-dash", parsed["diagnosis"])
        print("codex:", json.dumps(parsed))


if __name__ == "__main__":
    unittest.main()

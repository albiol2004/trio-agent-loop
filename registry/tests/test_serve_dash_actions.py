#!/usr/bin/env python3
"""dash-actions: correct loops (root-free live mailbox, native run registry,
hidden dirs), derived states and actionable inbox kinds, the read-only
diagnosis runner (fake cursor-agent / codex shims), the fix allowlist
(unknown ids, live refusal, confirm for destructive fixes), the answer box
(HUMAN.md + STATE reset) and the request guards on every new endpoint.

Everything runs offline against temporary workspaces and a temporary HOME;
drivers, trioctl and the diagnosis CLIs are shims. The one real invocation
of each diagnosis CLI lives in test_dash_real_cli.py (opt-in).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVE_PATH = REPO_ROOT / "dashboard" / "serve.py"


def _load_serve_module():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_serve_dash_actions", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()
la = serve.load_loop_actions_module()

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                           capture_output=True, text=True,
                           env={**os.environ, **GIT_ENV}).stdout.strip()


def _request(method: str, url: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        finally:
            exc.close()


def _mailbox(root: Path, rel: str, state: str, verdict: str | None = None) -> Path:
    box = root / rel
    box.mkdir(parents=True, exist_ok=True)
    (box / "GOAL.md").write_text("# Mission: dash actions test\n", encoding="utf-8")
    (box / "STATE.md").write_text(state, encoding="utf-8")
    if verdict is not None:
        (box / "VERDICT.md").write_text(verdict, encoding="utf-8")
    return box


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


FAKE_TRIOCTL = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_TRIOCTL_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
if "reconcile" in args:
    action = os.environ.get("FAKE_RECONCILE", "waiting")
    print(json.dumps({"decision": {"action": "applied" if "--apply" in args else action,
                                   "code": "late_valid_completion" if action == "ready" else "not_observed"}}))
sys.exit(0)
'''


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = [tempfile.TemporaryDirectory() for _ in range(2)]
        self.home = Path(self._tmp[0].name).resolve()
        self.root = Path(self._tmp[1].name).resolve()
        self.original_home = serve.HOME
        serve.HOME = self.home
        serve._BROKER_LISTING["value"] = None
        serve._HEAVY_CACHE.clear()
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        with serve._LOOP_ACTIONS_LOCK:
            serve._LOOP_ACTIONS.clear()
        serve._DIAGNOSES = None
        self.trioctl_log = self.home / "trioctl.jsonl"
        self.trioctl = _script(self.home / ".local" / "bin" / "trioctl", FAKE_TRIOCTL)
        env = {"FAKE_TRIOCTL_LOG": str(self.trioctl_log), "HOME": str(self.home)}
        for key in ("TRIO_NATIVE_RUNS_DIR", "TRIO_DASH_STATE_DIR", "TRIO_DASH_TRIOCTL",
                    "TRIO_DASH_RELEASE_DIR", "TRIO_DASH_NATIVE_LAUNCH", "TRIO_DASH_CURSOR_AGENT",
                    "TRIO_DASH_CODEX", "TRIO_DASH_DIAGNOSE_HARNESS", "TRIO_DASH_INBOX_STATE",
                    "CLAUDE_CONFIG_DIR"):
            env[key] = ""
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.stack = [patch.object(serve, "BROKER_BASE_URL", ""),
                      patch.object(la, "LAUNCH_GRACE_SECONDS", 0.3)]
        for p in self.stack:
            p.start()

    def tearDown(self):
        for p in reversed(self.stack):
            p.stop()
        self.env.stop()
        serve.HOME = self.original_home
        serve._BROKER_LISTING["value"] = None
        serve._NATIVE_REGISTRY.update(at=0.0, home=None, value=[])
        for tmp in self._tmp:
            tmp.cleanup()

    def start_server(self, workspaces=None):
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=workspaces or [self.root], auto_discover=False)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def board(self, root: Path | None = None) -> dict:
        query = urllib.parse.urlencode({"root": str(root or self.root)})
        status, data = _request("GET", f"{self.base}/api/board?{query}")
        self.assertEqual(status, 200, data)
        return data

    def card(self, name: str, root: Path | None = None) -> dict:
        return next(l for l in self.board(root)["loops"] if l["name"] == name)

    def inbox(self, name: str, root: Path | None = None) -> list[dict]:
        return [i for i in self.board(root)["inbox"] if i["loop"] == name]

    def actions(self, name: str, root: Path | None = None) -> dict:
        query = urllib.parse.urlencode({"root": str(root or self.root), "loop": name})
        status, data = _request("GET", f"{self.base}/api/loop/actions?{query}")
        self.assertEqual(status, 200, data)
        return data

    def post(self, path: str, payload: dict, headers: dict | None = None):
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return _request("POST", self.base + path, json.dumps(payload).encode(), hdrs)

    def fixes(self, name: str) -> dict:
        return {f["id"]: f for f in self.actions(name)["fixes"]}

    def action_log(self, box: Path) -> list[dict]:
        key = la.loop_key(box)
        path = self.home / ".local" / "state" / "trio-dash" / "loops" / key / "actions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def trioctl_calls(self) -> list[dict]:
        if not self.trioctl_log.exists():
            return []
        return [json.loads(line) for line in self.trioctl_log.read_text().splitlines()]

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail("condition not met in time")


# ------------------------------------------------------------- discovery


class DiscoveryTests(_Base):
    def test_root_free_card_reads_the_live_lead_worktree_copy(self):
        repo = self.root
        _git(repo, "init", "-q")
        box = _mailbox(repo, "loop-feature", "iteration: 0\nstatus: ready\nphase: idle\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        lead_wt = Path(self._tmp[0].name) / "lead-wt"
        live = _mailbox(lead_wt, "loop-feature",
                        "iteration: 3\nmax_iterations: 8\nstatus: running\nphase: lead-running\n")
        common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        slug = serve.load_metrics_module().loop_slug("loop-feature")
        ledger = common / "trio-worktrees" / f"lead-{slug}.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"kind": "lead", "mailbox_rel": "loop-feature",
                                      "live_mailbox": str(live), "state": "active"}))
        self.start_server()
        card = self.card("loop-feature")
        self.assertEqual(card["live_mailbox"], str(live))
        self.assertEqual(card["iteration"], 3)  # the root copy says 0
        self.assertEqual(card["status"], "running")
        # Actions address the loop by its root name and act on the live copy.
        data = self.actions("loop-feature")
        self.assertEqual(data["root_mailbox"], str(box))
        self.assertEqual(data["live_mailbox"], str(live))

    def test_native_registry_surfaces_a_hidden_non_loop_mailbox(self):
        hidden_repo = self.home / ".runtime" / "lab" / "repo"
        hidden_repo.mkdir(parents=True)
        _git(hidden_repo, "init", "-q")
        box = _mailbox(hidden_repo, "mission-box",
                       "iteration: 2\nmax_iterations: 4\nstatus: shipped\nphase: shipped\n")
        (box / ".session.json").write_text(json.dumps(
            {"driver": "claude-workflow", "session": "tok", "pid": 999999999,
             "done": True, "phase": "done", "started_at": "2026-09-29T10:00:00Z"}))
        runs = self.home / ".local" / "share" / "trio-agent-loop" / "native-runs"
        runs.mkdir(parents=True)
        key = hashlib.sha256(str(box.resolve()).encode()).hexdigest()[:16]
        (runs / f"{key}.json").write_text(json.dumps(
            {"schema": 1, "driver": "claude-workflow", "mailbox": str(box),
             "repo": str(hidden_repo), "state": "finished"}))
        # A bogus record (relative path) and one whose mailbox is gone are ignored.
        (runs / "bad1.json").write_text(json.dumps({"mailbox": "relative/box"}))
        (runs / "bad2.json").write_text(json.dumps({"mailbox": str(self.home / "gone")}))
        self.start_server()
        seeds = self.server.get_workspace_seeds()
        self.assertIn(hidden_repo.resolve(), seeds)
        card = self.card("mission-box", hidden_repo)
        self.assertEqual(card["driver"], "claude-workflow")
        self.assertEqual(card["loop_state"]["state"], "shipped")
        status, overview = _request("GET", f"{self.base}/api/overview")
        self.assertEqual(status, 200)
        names = [(w["root"], l["name"]) for w in overview["workspaces"] for l in w["loops"]]
        self.assertIn((str(hidden_repo.resolve()), "mission-box"), names)

    def test_registry_mailbox_is_not_duplicated_into_a_parent_workspace(self):
        repo = self.root / "nested" / "repo"
        repo.mkdir(parents=True)
        box = _mailbox(repo, "loop-x", "iteration: 1\nstatus: shipped\n")
        runs = self.home / ".local" / "share" / "trio-agent-loop" / "native-runs"
        runs.mkdir(parents=True)
        (runs / "a.json").write_text(json.dumps(
            {"driver": "claude-workflow", "mailbox": str(box), "repo": str(repo)}))
        self.start_server()
        self.assertNotIn("nested/repo/loop-x", [l["name"] for l in self.board()["loops"]])
        self.assertEqual(self.card("loop-x", repo)["name"], "loop-x")


# ------------------------------------------------------ states and inbox


def _native(box: Path, result: dict | None = None, *, session_done=True,
            session_start="2026-09-29T10:00:00Z", launch_session="11111111-2222-3333-4444-555555555555"):
    (box / ".session.json").write_text(json.dumps(
        {"driver": "claude-workflow", "session": "tok", "pid": 999999999,
         "done": session_done, "phase": "done" if session_done else "lead-running",
         "started_at": session_start}))
    (box / ".native-launch.json").write_text(json.dumps(
        {"session_id": launch_session, "args": json.dumps({"mailbox": str(box), "max_iterations": 4})}))
    if result is not None:
        record = {"schema": 1, "source": "launcher", "driver": "claude-workflow",
                  "session_id": launch_session, "run_id": "wf_abc-1",
                  "session_started_at": session_start, "finished_at": "2099-01-01T00:00:00Z"}
        record.update(result)
        (box / ".native-result.json").write_text(json.dumps(record))


class StateAndInboxTests(_Base):
    def kinds(self, name):
        return {i["kind"]: i for i in self.inbox(name)}

    def test_native_outcomes_are_distinct_states_and_items_not_interrupted(self):
        running = "iteration: 2\nmax_iterations: 6\nstatus: running\nphase: lead-running\n"
        cases = {
            "loop-held": ({"status": "held", "held_step": "gate", "reason": "gate held: denied"},
                          "held", "held", "Held at step gate"),
            "loop-conflict": ({"status": "conflict", "conflicts": [
                {"id": "a", "branch": "b", "files": ["src/x.py"]}]}, "conflict", "conflict", "src/x.py"),
            "loop-budget": ({"status": "budget"}, "budget", "budget", "budget"),
            "loop-error": ({"status": "error", "reason": "builders refused: wrong base"},
                           "error", "error", "builders refused"),
            "loop-cap": ({"status": "max_iterations"}, "iteration_cap", "iteration_cap", "cap"),
        }
        for name, (result, *_rest) in cases.items():
            _native(_mailbox(self.root, name, running), result)
        self.start_server()
        for name, (_result, state, kind, text) in cases.items():
            with self.subTest(name=name):
                card = self.card(name)
                self.assertEqual(card["driver"], "claude-workflow")
                self.assertEqual(card["loop_state"]["state"], state)
                kinds = self.kinds(name)
                self.assertIn(kind, kinds)
                self.assertNotIn("interrupted", kinds)
                item = kinds[kind]
                self.assertIn(text.lower(), (item["headline"] + item["detail"]).lower())

    def test_native_run_killed_mid_run_is_interrupted_and_resumable(self):
        box = _mailbox(self.root, "loop-killed",
                       "iteration: 1\nmax_iterations: 4\nstatus: running\nphase: lead-running\n")
        _native(box, None, session_done=False)
        (box / ".lock").mkdir()
        (box / ".lock" / "pid").write_text("999999999\n")
        (box / ".lock" / "owner").write_text("workflow:tok\n")
        claude = self.home / ".claude" / "projects" / "-repo" / "11111111-2222-3333-4444-555555555555"
        (claude / "subagents" / "workflows" / "wf_dead-7").mkdir(parents=True)
        launcher = _script(self.home / "native" / "launch.sh", "import sys; sys.exit(0)")
        (launcher.parent / "trio_native_step.py").write_text("# helper\n")
        (launcher.parent / "trio-native.js").write_text("// script\n")
        self.start_server()
        self.assertEqual(self.card("loop-killed")["loop_state"]["state"], "interrupted")
        with patch.dict(os.environ, {"TRIO_DASH_NATIVE_LAUNCH": str(launcher)}):
            fixes = self.fixes("loop-killed")
        self.assertTrue(fixes["native_resume"]["applicable"], fixes["native_resume"])
        self.assertIn("--run-id wf_dead-7", fixes["native_resume"]["commands_preview"][0])

    def test_stale_result_of_an_earlier_run_is_ignored(self):
        box = _mailbox(self.root, "loop-stale",
                       "iteration: 1\nmax_iterations: 4\nstatus: running\nphase: lead-running\n")
        _native(box, {"status": "held", "held_step": "gate"},
                session_start="2026-09-29T10:00:00Z")
        # A later run began (new session start) and died without a result.
        session = json.loads((box / ".session.json").read_text())
        session.update(started_at="2026-09-29T12:00:00Z", done=False)
        (box / ".session.json").write_text(json.dumps(session))
        self.start_server()
        self.assertEqual(self.card("loop-stale")["loop_state"]["state"], "interrupted")

    def test_result_recovered_from_raw_session_output(self):
        box = _mailbox(self.root, "loop-raw",
                       "iteration: 1\nmax_iterations: 4\nstatus: running\nphase: lead-running\n")
        _native(box, None)
        runs = box / ".native-runs"
        runs.mkdir()
        (runs / "11111111-2222-3333-4444-555555555555.20260929T100000Z.start.json").write_text(
            json.dumps({"type": "result", "total_cost_usd": 3.5, "result":
                        'done\n```json\n{"status": "error", "reason": "builders refused: x",'
                        ' "dangling_worktrees": []}\n```'}))
        self.start_server()
        state = self.card("loop-raw")["loop_state"]
        self.assertEqual(state["state"], "error")
        self.assertIn("builders refused", state["summary"])

    def test_omnigent_states_from_state_md(self):
        _mailbox(self.root, "loop-err", "iteration: 2\nstatus: error\nphase: driver-exception\n"
                 "reason: RuntimeError boom\n")
        _mailbox(self.root, "loop-retire", "iteration: 3\nstatus: needs_retirement\nphase: shipped\n",
                 "VERDICT: SHIP\n")
        _mailbox(self.root, "loop-land", "iteration: 3\nstatus: needs_land\nphase: land-conflict\n",
                 "VERDICT: SHIP\n")
        _mailbox(self.root, "loop-cap", "iteration: 5\nmax_iterations: 5\nstatus: running\nphase: idle\n",
                 "VERDICT: ITERATE\n")
        held = _mailbox(self.root, "loop-held", "iteration: 2\nstatus: needs_human\nphase: needs_human\n")
        (held / ".sessions").mkdir()
        (held / ".sessions" / "held-s1.json").write_text(json.dumps(
            {"session_id": "s1", "role": "lead", "hold": "role_completion_uncertain", "iteration": 2}))
        self.start_server()
        expect = {"loop-err": ("error", "error", "RuntimeError boom"),
                  "loop-retire": ("needs_retirement", "needs_retirement", "retirement"),
                  "loop-land": ("needs_land", "needs_land", "land-conflict"),
                  "loop-cap": ("iteration_cap", "iteration_cap", "max-iterations"),
                  "loop-held": ("held", "held", "role completion uncertain")}
        for name, (state, kind, text) in expect.items():
            with self.subTest(name=name):
                self.assertEqual(self.card(name)["loop_state"]["state"], state)
                kinds = self.kinds(name)
                self.assertIn(kind, kinds)
                item = kinds[kind]
                self.assertIn(text.lower(), (item["headline"] + " " + item["detail"]).lower())
        # A hold explains STATE's needs_human: no second, generic item.
        self.assertNotIn("needs_human", self.kinds("loop-held"))

    def test_dangling_worktrees_item(self):
        box = _mailbox(self.root, "loop-dangle", "iteration: 2\nstatus: shipped\nphase: shipped\n")
        wt = self.root / ".claude" / "worktrees" / "wf_1-2"
        wt.mkdir(parents=True)
        _native(box, {"status": "shipped", "dangling_worktrees": [str(wt), str(self.root / "gone")]})
        self.start_server()
        item = self.kinds("loop-dangle")["dangling_worktrees"]
        self.assertIn("1 dangling builder worktree", item["headline"])
        self.assertEqual(item["severity"], "low")


# ------------------------------------------------------- diagnosis runner


CURSOR_SHIM = r'''
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args[:-1], "prompt_len": len(args[-1]),
                         "prompt_head": args[-1][:200], "cwd": os.getcwd()}) + "\n")
answer = os.environ.get("SHIM_ANSWER") or json.dumps({
    "diagnosis": "The Lead's gate failed twice; STATE says error.",
    "state": "error", "evidence": ["STATE.md: status error"],
    "proposed_fix": {"id": "reset_and_rerun", "args": {}, "commands_preview": ["trioctl omnigent loop"],
                     "destructive": False},
    "needs_human_input": False})
if os.environ.get("SHIM_WRITE"):
    open(os.environ["SHIM_WRITE"], "w").write("broke read-only")
print(json.dumps({"type": "system", "subtype": "init", "model": "Grok 4.6 Low"}), flush=True)
time.sleep(float(os.environ.get("SHIM_SLEEP", "0")))
print(json.dumps({"type": "tool_call", "subtype": "started", "tool_call": {"readToolCall": {}}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "result": "Looking.\n" + answer}), flush=True)
'''

CODEX_SHIM = r'''
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": args, "prompt_len": len(prompt), "cwd": os.getcwd()}) + "\n")
answer = json.dumps({"diagnosis": "Held dispatch; receipt unknown.", "state": "held",
                     "evidence": ["held-s1.json"], "proposed_fix": {"id": "abandon", "args": {}},
                     "needs_human_input": True, "question": "Did session s1 finish?"})
out = args[args.index("-o") + 1]
open(out, "w").write(answer)
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "cat STATE.md"}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


class DiagnosisTests(_Base):
    def setUp(self):
        super().setUp()
        self.shim_log = self.home / "shim.jsonl"
        self.cursor = _script(self.home / ".local" / "bin" / "cursor-agent", CURSOR_SHIM)
        self.codex = _script(self.home / ".local" / "bin" / "codex", CODEX_SHIM)
        self.env2 = patch.dict(os.environ, {"SHIM_LOG": str(self.shim_log)})
        self.env2.start()
        self.addCleanup(self.env2.stop)
        self.box = _mailbox(self.root, "loop", "iteration: 2\nmax_iterations: 5\nstatus: error\n"
                            "phase: error\n", "VERDICT: ITERATE\n")
        self.start_server()

    def diagnose(self, **body):
        return self.post("/api/loop/diagnose", {"root": str(self.root), "loop": "loop", **body})

    def wait_done(self):
        def done():
            query = urllib.parse.urlencode({"root": str(self.root), "loop": "loop"})
            _s, data = _request("GET", f"{self.base}/api/loop/diagnosis?{query}")
            d = data.get("diagnosis") or {}
            return d if d.get("status") in ("done", "failed") else None
        return self.wait_for(done)

    def test_cursor_default_runs_read_only_ask_mode_and_stores_result(self):
        with patch.dict(os.environ, {"SHIM_SLEEP": "1.0"}):
            status, data = self.diagnose()
            self.assertEqual(status, 202, data)
            self.assertEqual(data["diagnosis"]["harness"], "cursor")
            self.assertEqual(data["diagnosis"]["model"], "cursor-grok-4.6-low")
            again, err = self.diagnose()
            self.assertEqual(again, 409, err)  # one diagnosis per loop at a time
            self.assertIn("already running", err["error"])
            self.assertEqual(self.card("loop")["diagnosis"]["status"], "running")
            record = self.wait_done()
        self.assertEqual(record["status"], "done", record)
        result = record["result"]
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["proposed_fix"]["id"], "reset_and_rerun")
        # The server corrects the destructive flag from its own allowlist.
        self.assertTrue(result["proposed_fix"]["destructive"])
        call = json.loads(self.shim_log.read_text().splitlines()[-1])
        argv = call["argv"]
        self.assertEqual(argv[:2], ["-p", "--mode"])
        self.assertEqual(argv[argv.index("--mode") + 1], "ask")
        self.assertEqual(argv[argv.index("--model") + 1], "cursor-grok-4.6-low")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "enabled")
        self.assertNotIn("--force", argv)
        self.assertNotIn("--yolo", argv)
        self.assertIn("READ-ONLY diagnosis agent", call["prompt_head"])
        self.assertLess(call["prompt_len"], 100_000)
        self.assertEqual(record["integrity"]["changed"], [])
        card = self.card("loop")["diagnosis"]
        self.assertEqual((card["status"], card["proposed_fix"]), ("done", "reset_and_rerun"))
        actions = self.actions("loop")
        proposed = actions["diagnosis"]["result"]["proposed_fix"]
        # The proposal is re-planned by the server, with its own commands.
        self.assertEqual(proposed["server_check"], "applicable now")
        self.assertIn(str(self.trioctl), proposed["server_commands"][1])
        log = [e["action"] for e in self.action_log(self.box)]
        self.assertIn("diagnose", log)
        self.assertIn("diagnose-done", log)

    def test_codex_read_only_sandbox_high_effort_and_forbidden_fix_rejected(self):
        status, data = self.diagnose(harness="codex")
        self.assertEqual(status, 202, data)
        record = self.wait_done()
        self.assertEqual(record["status"], "done", record)
        call = json.loads(self.shim_log.read_text().splitlines()[-1])
        argv = call["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-luna")
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertGreater(call["prompt_len"], 1000)  # prompt via stdin
        fix = record["result"]["proposed_fix"]
        self.assertEqual(fix["id"], "abandon")
        self.assertIn("never automated", fix["rejected"])
        self.assertTrue(record["result"]["needs_human_input"])
        self.assertEqual(record["result"]["question"], "Did session s1 finish?")

    def test_unknown_fix_and_garbage_answers(self):
        with patch.dict(os.environ, {"SHIM_ANSWER": json.dumps(
                {"diagnosis": "x", "state": "error", "evidence": [],
                 "proposed_fix": {"id": "rm_rf_everything"}, "needs_human_input": False})}):
            self.diagnose()
            record = self.wait_done()
        self.assertEqual(record["result"]["proposed_fix"]["rejected"], "not in the fix allowlist")
        with patch.dict(os.environ, {"SHIM_ANSWER": "no json at all"}):
            self.diagnose()
            record = self.wait_done()
        self.assertEqual(record["status"], "failed")
        self.assertIn("no JSON object", record["error"])

    def test_integrity_check_flags_a_write_during_diagnosis(self):
        with patch.dict(os.environ, {"SHIM_WRITE": str(self.box / "PLAN.md")}):
            self.diagnose()
            record = self.wait_done()
        self.assertIn("PLAN.md", record["integrity"]["changed"])
        self.assertIn("read-only", record["integrity"]["warning"])

    def test_unknown_harness_and_missing_cli_are_refused(self):
        status, data = self.diagnose(harness="claude")
        self.assertEqual(status, 409, data)
        self.codex.unlink()
        status, data = self.diagnose(harness="codex")
        self.assertEqual(status, 409, data)
        self.assertIn("not found", data["error"])


# ----------------------------------------------------- fix allowlist


class FixAllowlistTests(_Base):
    def fix(self, name, fix_id, **extra):
        return self.post("/api/loop/fix", {"root": str(self.root), "loop": name, "fix": fix_id, **extra})

    def test_unknown_fix_id_is_rejected_and_logged(self):
        box = _mailbox(self.root, "loop", "iteration: 1\nstatus: error\n")
        self.start_server()
        for bad in ("abandon", "sessions_prune", "rm -rf /", 7, None):
            status, data = self.fix("loop", bad)
            self.assertEqual(status, 400, data)
            self.assertIn("unknown fix id", data["error"])
        self.assertEqual(self.trioctl_calls(), [])
        refused = [e for e in self.action_log(box) if e["action"] == "fix-refused"]
        self.assertEqual(len(refused), 5)

    def test_every_fix_is_refused_while_a_driver_is_live(self):
        box = _mailbox(self.root, "loop", "iteration: 1\nstatus: error\n")
        (box / ".lock").mkdir()
        (box / ".lock" / "pid").write_text(f"{os.getpid()}\n")
        self.start_server()
        for fix_id in la.FIXES:
            status, data = self.fix("loop", fix_id, confirm=True)
            self.assertEqual(status, 409, (fix_id, data))
            self.assertIn("live", data["error"])
        self.assertTrue(all(not f["applicable"] for f in self.fixes("loop").values()))
        self.assertEqual(self.trioctl_calls(), [])
        self.assertEqual(self.card("loop")["status"], "error")

    def test_destructive_fix_requires_confirm_and_shows_the_commands(self):
        box = _mailbox(self.root, "loop", "iteration: 2\nmax_iterations: 5\nstatus: error\n"
                       "phase: driver-exception\nreason: boom\n")
        self.start_server()
        before = (box / "STATE.md").read_text()
        status, data = self.fix("loop", "reset_and_rerun")
        self.assertEqual(status, 409, data)
        self.assertTrue(data["confirm_required"])
        preview = data["plan"]["commands_preview"]
        self.assertIn("status: error → running", preview[0])
        self.assertIn(str(self.trioctl), preview[1])
        self.assertIn("omnigent loop --mailbox", preview[1])
        self.assertEqual((box / "STATE.md").read_text(), before)  # nothing ran
        self.assertEqual(self.trioctl_calls(), [])
        status, data = self.fix("loop", "reset_and_rerun", confirm=True)
        self.assertEqual(status, 200, data)
        state = la.read_state(box)
        self.assertEqual((state["status"], state["phase"]), ("running", "idle"))
        self.assertNotIn("reason", state)
        call = self.wait_for(self.trioctl_calls)[0]
        self.assertEqual(call["argv"][:4], ["omnigent", "loop", "--mailbox", str(box)])
        self.assertEqual(call["argv"][-2:], ["--max-iterations", "5"])
        entry = [e for e in self.action_log(box) if e["action"] == "fix"][-1]
        self.assertTrue(entry["confirmed"] and entry["ok"])
        self.assertEqual(entry["fix"], "reset_and_rerun")
        self.assertEqual(entry["who"]["addr"], "127.0.0.1")

    def test_non_destructive_fix_runs_on_click_with_server_revalidated_args(self):
        box = _mailbox(self.root, "loop", "iteration: 5\nmax_iterations: 5\nstatus: running\n"
                       "phase: idle\n", "VERDICT: ITERATE\n")
        self.start_server()
        status, data = self.fix("loop", "rerun_more_iterations", args={"max_iterations": 5})
        self.assertEqual(status, 409, data)
        self.assertIn("must exceed", data["error"])
        status, data = self.fix("loop", "rerun_more_iterations", args={"max_iterations": "9; rm -rf /"})
        self.assertEqual(status, 409, data)
        status, data = self.fix("loop", "rerun_more_iterations", args={"max_iterations": 8})
        self.assertEqual(status, 200, data)
        call = self.wait_for(self.trioctl_calls)[0]
        self.assertEqual(call["argv"][-2:], ["--max-iterations", "8"])
        self.assertEqual(call["cwd"], str(self.root))
        self.assertTrue(any(e["action"] == "fix" and e["ok"] for e in self.action_log(box)))

    def test_proposal_is_only_a_suggestion_preconditions_are_the_servers(self):
        _mailbox(self.root, "loop", "iteration: 1\nstatus: shipped\n", "VERDICT: SHIP\n")
        self.start_server()
        for fix_id in ("rerun", "land", "retire_ship", "reconcile_apply", "repair_scope"):
            status, data = self.fix("loop", fix_id, confirm=True)
            self.assertEqual(status, 409, (fix_id, data))
            self.assertTrue(data.get("refused"), data)
        self.assertEqual(self.trioctl_calls(), [])

    def test_reconcile_apply_only_when_the_dry_run_is_receipt_proven(self):
        box = _mailbox(self.root, "loop", "iteration: 2\nstatus: needs_human\n")
        (box / ".sessions").mkdir()
        (box / ".sessions" / "held-s1.json").write_text(json.dumps({"session_id": "s1"}))
        self.start_server()
        status, data = self.fix("loop", "reconcile_dry_run")
        self.assertEqual(status, 200, data)
        self.assertIn("--dry-run", self.trioctl_calls()[-1]["argv"])
        with patch.dict(os.environ, {"FAKE_RECONCILE": "waiting"}):
            status, data = self.fix("loop", "reconcile_apply", confirm=True)
        self.assertEqual(status, 409, data)
        self.assertIn("not receipt-proven", data["error"])
        self.assertFalse(any("--apply" in c["argv"] for c in self.trioctl_calls()))
        with patch.dict(os.environ, {"FAKE_RECONCILE": "ready"}):
            status, data = self.fix("loop", "reconcile_apply")
            self.assertEqual(status, 409, data)
            self.assertTrue(data["confirm_required"])
            status, data = self.fix("loop", "reconcile_apply", confirm=True)
        self.assertEqual(status, 200, data)
        self.assertIn("--apply", self.trioctl_calls()[-1]["argv"])

    def test_land_is_refused_while_the_lead_worktree_has_a_conflict(self):
        _git(self.root, "init", "-q")
        box = _mailbox(self.root, "loop", "iteration: 3\nstatus: needs_land\nphase: land-conflict\n",
                       "VERDICT: SHIP\n")
        (self.root / "a.txt").write_text("base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        _git(self.root, "checkout", "-q", "-b", "other")
        (self.root / "a.txt").write_text("other\n")
        _git(self.root, "commit", "-q", "-am", "other")
        _git(self.root, "checkout", "-q", "-")
        (self.root / "a.txt").write_text("mine\n")
        _git(self.root, "commit", "-q", "-am", "mine")
        subprocess.run(["git", "-C", str(self.root), "merge", "other"], capture_output=True,
                       env={**os.environ, **GIT_ENV})
        self.start_server()
        status, data = self.fix("loop", "land", confirm=True)
        self.assertEqual(status, 409, data)
        self.assertIn("never the dashboard", data["error"])
        subprocess.run(["git", "-C", str(self.root), "merge", "--abort"], check=True)
        status, data = self.fix("loop", "land")
        self.assertEqual(status, 409, data)
        self.assertTrue(data["confirm_required"])
        self.assertIn("omnigent land --mailbox", data["plan"]["commands_preview"][0])
        self.assertEqual(la.read_state(box)["status"], "needs_land")

    def test_retire_ship_commits_the_mailbox_on_a_clean_tree_only(self):
        _git(self.root, "init", "-q")
        (self.root / "app.py").write_text("print(1)\n")
        box = _mailbox(self.root, "loop", "iteration: 4\nstatus: running\n", "VERDICT: ITERATE\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "product")
        (box / "STATE.md").write_text("iteration: 4\nstatus: needs_retirement\nphase: shipped\n")
        (box / "VERDICT.md").write_text("VERDICT: SHIP\n\nall good\n")
        (self.root / "app.py").write_text("print(2)\n")  # unattributed product change
        self.start_server()
        status, data = self.fix("loop", "retire_ship", confirm=True)
        self.assertEqual(status, 409, data)
        self.assertIn("need a human", data["error"])
        _git(self.root, "checkout", "--", "app.py")
        head = _git(self.root, "rev-parse", "HEAD")
        status, data = self.fix("loop", "retire_ship")
        self.assertEqual(status, 409, data)
        self.assertTrue(any("commit: " + head in c for c in data["plan"]["commands_preview"]))
        with patch.dict(os.environ, GIT_ENV):
            status, data = self.fix("loop", "retire_ship", confirm=True)
        self.assertEqual(status, 200, data)
        self.assertEqual(_git(self.root, "log", "-1", "--format=%s"), "loop: iteration 4 — SHIP")
        self.assertIn(f"commit: {head}", (box / "VERDICT.md").read_text())
        self.assertEqual(_git(self.root, "status", "--porcelain"), "")

    def test_cleanup_removes_only_clean_merged_dangling_worktrees(self):
        _git(self.root, "init", "-q")
        (self.root / "f.txt").write_text("x\n")
        box = _mailbox(self.root, "loop", "iteration: 2\nstatus: shipped\nphase: shipped\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "base")
        wts = self.root / ".claude" / "worktrees"
        clean, dirty = wts / "wf_1-1", wts / "wf_1-2"
        _git(self.root, "worktree", "add", "-q", "-b", "wt-clean", str(clean))
        _git(self.root, "worktree", "add", "-q", "-b", "wt-dirty", str(dirty))
        (dirty / "scratch.txt").write_text("unreviewed\n")
        (self.root / ".git" / "info" / "exclude").write_text(".claude/\n")
        _native(box, {"status": "shipped", "dangling_worktrees": [str(clean), str(dirty)]})
        (self.root / ".gitignore").write_text("")
        self.start_server()
        status, data = self.fix("loop", "cleanup_worktrees",
                                args={"paths": [str(self.root / "elsewhere")]}, confirm=True)
        self.assertEqual(status, 409, data)
        status, data = self.fix("loop", "cleanup_worktrees")
        self.assertEqual(status, 409, data)
        self.assertEqual(len(data["plan"]["commands_preview"]), 2)  # remove + branch -d
        self.assertTrue(any("kept" in n and "wf_1-2" in n for n in data["plan"]["notes"]))
        status, data = self.fix("loop", "cleanup_worktrees", confirm=True)
        self.assertEqual(status, 200, data)
        self.assertFalse(clean.exists())
        self.assertTrue(dirty.exists())
        self.assertNotIn("wt-clean", _git(self.root, "branch", "--list", "wt-clean"))


# --------------------------------------------------------------- answer box


class AnswerBoxTests(_Base):
    def answer(self, name, text, **extra):
        return self.post("/api/loop/answer", {"root": str(self.root), "loop": name,
                                              "answer": text, **extra})

    def test_answer_appends_human_md_resets_state_after_confirm(self):
        box = _mailbox(self.root, "loop", "iteration: 3\nmax_iterations: 6\nstatus: needs_human\n"
                       "phase: needs_human\nmission: keep\n",
                       "VERDICT: NEEDS_HUMAN\n\n## Human check\nOpen the page.\n")
        self.start_server()
        status, data = self.answer("loop", "Checked: the page renders; ship the footer too.")
        self.assertEqual(status, 409, data)
        self.assertTrue(data["confirm_required"])
        self.assertIn("in-reply-to: STATE.md status needs_human (iteration 3)", data["plan"]["entry"])
        self.assertTrue(any("status: needs_human → running" in c
                            for c in data["plan"]["commands_preview"]))
        self.assertFalse((box / "HUMAN.md").exists())
        status, data = self.answer("loop", "Checked: the page renders; ship the footer too.",
                                   confirm=True, reset=True)
        self.assertEqual(status, 200, data)
        human = (box / "HUMAN.md").read_text()
        self.assertTrue(human.startswith("# Human answers"))
        self.assertIn(f"answer {data['answer_id']}", human)
        self.assertIn("ship the footer too.", human)
        state = la.read_state(box)
        self.assertEqual((state["status"], state["phase"]), ("running", "idle"))
        self.assertIn(f"HUMAN.md#{data['answer_id']}", state["human_answer"])
        self.assertEqual(state["mission"], "keep")
        self.assertEqual([f["id"] for f in data["restart"]], ["rerun"])
        card = self.card("loop")
        self.assertEqual(card["loop_state"]["state"], "ready")
        kinds = {i["kind"] for i in self.inbox("loop")}
        self.assertNotIn("needs_human", kinds)
        self.assertNotIn("interrupted", kinds)
        # A second answer appends; HUMAN.md is never rewritten.
        (box / "STATE.md").write_text("iteration: 4\nstatus: blocked\nphase: blocked\n")
        status, data2 = self.answer("loop", "Use the staging DB.", confirm=True, reset=False)
        self.assertEqual(status, 200, data2)
        again = (box / "HUMAN.md").read_text()
        self.assertTrue(again.startswith(human))
        self.assertEqual(la.read_state(box)["status"], "blocked")  # reset=False
        log = [e for e in self.action_log(box) if e["action"] == "answer"]
        self.assertEqual(len(log), 2)

    def test_answer_is_refused_for_live_or_not_stopped_loops_and_holds_keep_state(self):
        box = _mailbox(self.root, "loop", "iteration: 1\nstatus: running\nphase: lead-running\n")
        self.start_server()
        status, data = self.answer("loop", "x", confirm=True)
        self.assertEqual(status, 409, data)
        self.assertIn("not stopped", data["error"])
        (box / "STATE.md").write_text("iteration: 1\nstatus: needs_human\n")
        (box / ".lock").mkdir()
        (box / ".lock" / "pid").write_text(f"{os.getpid()}\n")
        status, data = self.answer("loop", "x", confirm=True)
        self.assertEqual(status, 409, data)
        self.assertIn("live", data["error"])
        (box / ".lock" / "pid").unlink()
        (box / ".lock").rmdir()
        (box / ".sessions").mkdir()
        (box / ".sessions" / "held-s1.json").write_text(json.dumps({"session_id": "s1"}))
        status, data = self.answer("loop", "x", confirm=True, reset=True)
        self.assertEqual(status, 409, data)
        self.assertIn("held dispatch", data["error"])
        status, data = self.answer("loop", "session s1 finished; output reconciled by me",
                                   confirm=True, reset=False)
        self.assertEqual(status, 200, data)
        self.assertEqual(la.read_state(box)["status"], "needs_human")
        for bad in ("", "   ", "x" * 20001):
            status, data = self.answer("loop", bad, confirm=True, reset=False)
            self.assertEqual(status, 409, data)


# --------------------------------------------------------- request guards


class GuardTests(_Base):
    def setUp(self):
        super().setUp()
        _mailbox(self.root, "loop", "iteration: 1\nstatus: needs_human\n", "VERDICT: NEEDS_HUMAN\n")
        self.start_server()
        self.body = json.dumps({"root": str(self.root), "loop": "loop", "fix": "rerun",
                                "answer": "x", "confirm": True}).encode()

    def test_new_endpoints_keep_host_origin_and_json_guards(self):
        for path in ("/api/loop/fix", "/api/loop/answer", "/api/loop/diagnose"):
            with self.subTest(path=path):
                status, _ = _request("POST", self.base + path, self.body,
                                     {"Content-Type": "application/json", "Host": "evil.example"})
                self.assertEqual(status, 421)
                status, _ = _request("POST", self.base + path, self.body,
                                     {"Content-Type": "application/json",
                                      "Origin": "https://evil.example"})
                self.assertEqual(status, 403)
                status, _ = _request("POST", self.base + path, self.body,
                                     {"Content-Type": "application/json",
                                      "Sec-Fetch-Site": "cross-site"})
                self.assertEqual(status, 403)
                status, _ = _request("POST", self.base + path, self.body,
                                     {"Content-Type": "text/plain"})
                self.assertEqual(status, 415)
        query = urllib.parse.urlencode({"root": str(self.root), "loop": "loop"})
        for path in ("/api/loop/actions", "/api/loop/diagnosis"):
            status, _ = _request("GET", f"{self.base}{path}?{query}", headers={"Host": "evil.example"})
            self.assertEqual(status, 421)
        self.assertFalse((self.root / "loop" / "HUMAN.md").exists())

    def test_roots_outside_the_workspaces_and_unknown_loops_are_refused(self):
        outside = Path(tempfile.mkdtemp(dir=self.home))
        _mailbox(outside, "loop", "iteration: 1\nstatus: error\n")
        status, data = self.post("/api/loop/fix", {"root": str(outside), "loop": "loop", "fix": "rerun"})
        self.assertEqual(status, 403, data)
        for name in ("../loop", "/etc", "nope"):
            status, data = self.post("/api/loop/fix", {"root": str(self.root), "loop": name,
                                                       "fix": "rerun"})
            self.assertEqual(status, 404, data)


# ------------------------------------------------ deployment (not applied)


class DeployScriptTests(unittest.TestCase):
    SCRIPT = REPO_ROOT / "dashboard" / "service" / "point-at-release.sh"

    def run_script(self, home: Path, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(home), "TRIO_DASH_TRIOCTL": ""}
        return subprocess.run(["bash", str(self.SCRIPT), *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.service = self.home / ".services" / "trio-dash"
        self.service.mkdir(parents=True)
        (self.service / "env").write_text(
            "TRIO_DASH_CHECKOUT=$HOME/.services/trio-dash/checkout\nTRIO_DASH_PORT=22000\n")
        (self.service / "run").write_text("#!/bin/sh\n# old run\n")
        _script(self.home / ".local" / "bin" / "trioctl", "")

    def test_dry_run_changes_nothing_and_apply_repoints_with_backups(self):
        release = str(REPO_ROOT)  # this tree is a dash-actions dashboard
        proc = self.run_script(self.home, "--release", release)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry run: nothing written", proc.stdout)
        self.assertIn("$HOME/.services/trio-dash/checkout", (self.service / "env").read_text())
        proc = self.run_script(self.home, "--release", release, "--apply")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = (self.service / "env").read_text()
        self.assertIn(f"TRIO_DASH_CHECKOUT={REPO_ROOT}", env)
        self.assertIn("TRIO_DASH_PORT=22000", env)
        self.assertEqual((self.service / "run").read_text(),
                         (REPO_ROOT / "dashboard" / "service" / "run").read_text())
        self.assertEqual(len(list(self.service.glob("env.before-release-*"))), 1)
        self.assertEqual(len(list(self.service.glob("run.before-release-*"))), 1)
        self.assertIn("svc restart trio-dash", proc.stdout)

    def test_refuses_a_release_that_predates_dash_actions(self):
        old = self.home / "old-release"
        (old / "dashboard").mkdir(parents=True)
        (old / "dashboard" / "serve.py").write_text("print('no discover flag')\n")
        proc = self.run_script(self.home, "--release", str(old), "--apply")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL  serve.py supports --discover", proc.stdout)
        self.assertIn("$HOME/.services/trio-dash/checkout", (self.service / "env").read_text())


if __name__ == "__main__":
    unittest.main()

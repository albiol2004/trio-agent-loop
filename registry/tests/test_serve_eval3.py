#!/usr/bin/env python3
"""eval3 (dash-actions round 3): the LAB/dash/eval3/repros, inverted.

1. replay-across-runs.sh — a signed answer is bound to the exact stop it
   answers (mailbox, iteration, GOAL / VERDICT digests, VERDICT's last commit,
   HEAD) and consumed by the Evaluator that rules on it: a replay into a new
   run in the same mailbox path passes nothing; the honest lockstep, open-loop
   and dashboard paths still deliver.
2. installsh-trioctl-no-ledger.py — install.sh's layout (siblings in one bin
   dir) delivers the answer.
3. openloop-answer-rerun*.py — the integration-eval after a Lead pass that
   changed nothing receives the answer.
4. native-resume-edges.sh loop-nat-nested — any ``models`` value type is a
   clean refusal; a fuzzed schema validator never raises anything else.
5. answer-linesep.py — answers with U+2028/U+2029/NEL/FF/VT/FS verify.
6. the dashboard refuses exactly what launch.sh refuses (one validator).
7. printable mailbox paths (spaces, non-ASCII, ``,``, ``~``) are accepted,
   passed as one argv element and quoted in every prompt; control characters
   are refused.
8/10. the reconcile_apply confirm token binds the held records; retire_ship's
   binds the mailbox file set.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from . import test_serve_dash_actions as D

REPO_ROOT = D.REPO_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from metrics import trio_loop  # noqa: E402

la = D.la
_git = D._git
_mailbox = D._mailbox
LEDGER = D.LEDGER
NA = la.native_args


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _quiet(fn, *args, **kwargs):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        value = fn(*args, **kwargs)
    return value, err.getvalue()


NH_STATE = "iteration: 2\nstatus: needs_human\nphase: idle\nmax_iterations: 5\n"


class _AnswerBase(D._Base):
    def state(self) -> Path:
        return self.home / ".local" / "state" / "trio-dash"

    def repo_with_stop(self, name="loop-nh") -> Path:
        _git(self.root, "init", "-q", "-b", "main")
        (self.root / "app.txt").write_text("product\n")
        box = _mailbox(self.root, name, NH_STATE, "VERDICT: NEEDS_HUMAN\niteration: 2\n")
        (box / "LOG.md").write_text("# log\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "loop: iteration 2 — NEEDS_HUMAN")
        return box

    def answer(self, name="loop-nh", text="Human check: PASSED (run A)"):
        if not hasattr(self, "server"):
            self.start_server()
        status, data = self.confirmed("/api/loop/answer", {"root": str(self.root), "loop": name,
                                                           "answer": text, "reset": True})
        self.assertEqual(status, 200, data)
        return data

    def block(self, box: Path, iteration: int, role: str, kind=None):
        return _quiet(trio_loop.human_answer_block, box, iteration, role, kind)


class ReplayTests(_AnswerBase):
    """eval3 finding 1 (repros/replay-across-runs.sh, inverted)."""

    def run_a_then_new_run(self, *, restore=("HUMAN.md",), archive=True) -> Path:
        box = self.repo_with_stop()
        self.answer()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "run A answered")
        old = _git(self.root, "rev-parse", "HEAD")
        if archive:  # the trio-init reset: archive, then a new task in the same path
            _git(self.root, "mv", "loop-nh", "loop-nh-archive-2026-09-29")
            _git(self.root, "commit", "-q", "-m", "archive")
            box.mkdir()
        (box / "GOAL.md").write_text("# Goal B: redesign the checkout page (verify: human)\n")
        (box / "STATE.md").write_text("iteration: 2\nstatus: ready\nphase: idle\n")
        (box / "LOG.md").write_text("# log\n")
        for name in restore:  # the attacker's commit: files from git history
            (box / name).write_text(_git(self.root, "show", f"{old}:loop-nh/{name}") + "\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "attacker: new loop B")
        return box

    def assert_nothing_passed(self, box: Path, needle: str):
        for role in ("lead", "evaluator"):
            with self.subTest(role=role):
                text, err = self.block(box, 3, role)
                self.assertEqual(text, "")
                self.assertIn(needle, err)

    def test_the_repro_replay_passes_nothing(self):
        box = self.run_a_then_new_run()
        self.assert_nothing_passed(box, "does not answer the current stop")

    def test_a_full_replay_of_goal_verdict_and_human_from_history_passes_nothing(self):
        box = self.run_a_then_new_run(restore=("HUMAN.md", "GOAL.md", "VERDICT.md"))
        self.assertEqual((box / "GOAL.md").read_text().strip(), "# Mission: dash actions test")
        self.assert_nothing_passed(box, "deleted or moved a mailbox file")

    def test_a_replay_without_archiving_passes_nothing(self):
        box = self.run_a_then_new_run(archive=False)
        self.assert_nothing_passed(box, "GOAL.md changed")

    def test_a_verdict_replaced_and_restored_in_history_passes_nothing(self):
        box = self.repo_with_stop()
        self.answer()
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "answered")
        original = (box / "VERDICT.md").read_text()
        (box / "VERDICT.md").write_text("VERDICT: ITERATE\niteration: 3\n")
        _git(self.root, "commit", "-qam", "run B iteration 3")
        (box / "VERDICT.md").write_text(original)
        (box / "STATE.md").write_text("iteration: 2\nstatus: running\nphase: idle\n")
        _git(self.root, "commit", "-qam", "attacker restores the stop")
        self.assert_nothing_passed(box, "replaced the stop's VERDICT.md")

    def test_a_consumed_answer_is_never_delivered_again(self):
        box = self.repo_with_stop()
        self.answer()
        text, _ = self.block(box, 3, "evaluator")
        self.assertIn("## Verified human answer (driver)", text)
        consumed = [json.loads(l) for l in (self.state() / "consumed.jsonl").read_text().splitlines()]
        self.assertEqual(len(consumed), 1)
        # A replay of the same stop (STATE rewound, same files) gets nothing.
        (box / "STATE.md").write_text("iteration: 2\nstatus: running\nphase: idle\n")
        for role in ("lead", "evaluator"):
            text, err = self.block(box, 3, role)
            self.assertEqual(text, "")
            self.assertIn("consumed", err)
        self.assertIn("(consumed: delivered to the Evaluator",
                      "\n".join(self.actions("loop-nh")["answer"]["entries"]))

    def test_an_old_unbound_record_is_not_delivered(self):
        box = self.repo_with_stop()
        key = LEDGER.load_key(self.state(), create=True)
        at, aid, body = "2026-09-29T12:00:00Z", "abc123def456", "Human check: PASSED"
        record = {"id": aid, "loop": "k", "mailbox": os.path.realpath(box),
                  "root_mailbox": os.path.realpath(box), "iteration": "2", "at": at,
                  "sha256": LEDGER.body_digest(body)}
        record["mac"] = LEDGER.record_mac(key, record)  # a round-2 style record
        LEDGER.append_record(self.state(), record)
        (box / "HUMAN.md").write_text(
            f"## {at} — answer {aid} — iteration 2 — trio-dash "
            f"{LEDGER.entry_sig(key, at, aid, 2, body)}\n\n{LEDGER.quote_body(body)}")
        text, err = self.block(box, 3, "lead")
        self.assertEqual(text, "")
        self.assertIn("not bound to a stop", err)

    def test_a_newer_verdict_ends_the_answer(self):
        box = self.repo_with_stop()
        self.answer()
        (box / "VERDICT.md").write_text("VERDICT: ITERATE\niteration: 3\n")
        text, err = self.block(box, 3, "lead")
        self.assertEqual(text, "")
        self.assertIn("no longer holds the stop", err)


class HonestDeliveryTests(_AnswerBase):
    """The honest paths still deliver (lockstep, open-loop, ledger module)."""

    def test_lockstep_lead_then_evaluator_is_one_use(self):
        box = self.repo_with_stop()
        data = self.answer(text="Human check: PASSED")
        lead, _ = self.block(box, 3, "lead")
        self.assertIn(f"answer {data['answer_id']}", lead)
        self.assertIn("> Human check: PASSED", lead)
        # The Lead commits product and mailbox changes; the stop is intact.
        (self.root / "app.txt").write_text("product v2\n")
        with open(box / "LOG.md", "a") as fh:
            fh.write(f"- iter 3 | lead | cites {data['answer_id']}\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "slice(x): apply the human answer")
        lead_again, _ = self.block(box, 3, "lead")  # a re-dispatched Lead: same use
        self.assertEqual(lead_again, lead)
        evaluator, _ = self.block(box, 3, "evaluator")
        self.assertEqual(evaluator, lead)
        for role in ("lead", "evaluator"):
            text, err = self.block(box, 4 if role == "lead" else 3, role)
            self.assertEqual(text, "")

    def test_the_repro_lockstep_run_delivers_to_lead_and_evaluator(self):
        box = self.repo_with_stop()
        self.answer(text="Human check: PASSED")
        seen = []

        class Runner:
            def run(self, role, iteration, mailbox, context=None):
                block = trio_loop.human_answer_block(mailbox, iteration, role,
                                                     (context or {}).get("kind"))
                seen.append((role, iteration, bool(block)))
                if role == "lead":
                    with open(mailbox / "LOG.md", "a") as fh:
                        fh.write(f"- iter {iteration} | lead | ok\n")
                else:
                    (mailbox / "VERDICT.md").write_text(f"VERDICT: BLOCKED\niteration: {iteration}\n")
                return 0

        with contextlib.redirect_stderr(io.StringIO()):
            trio_loop._run_lockstep(box, 5, Runner())
        self.assertTrue(seen and seen[0] == ("lead", 3, True), seen)
        self.assertTrue(all(ok for role, it, ok in seen if it == 3 and role == "lead"), seen)

    def _open_loop(self, lead_changes: bool):
        T = _load("eval3_open_loop_driver", REPO_ROOT / "metrics" / "tests" / "test_open_loop_driver.py")
        sdir = self.state()
        tmp = self.home / "ol"
        tmp.mkdir()
        lock = threading.Lock()
        mb = T.make_open_loop_mailbox(tmp, T.PLAN_ONE_SLICE)
        q, v = T.QueueModel(mb, lock), T.VerdictModel(mb, lock)
        sha = T.fake_sha("solo")
        lead = T.ScriptedLeadRunner([lambda m: q.retire("solo", sha)])
        ev = T.ScriptedEvalRunner(
            slice_actions={("solo", sha): lambda m: v.append_slice_section("solo", sha, "SHIP")},
            integration_actions=[lambda m: v.set_integration_verdict("VERDICT: NEEDS_HUMAN")])
        with contextlib.redirect_stderr(io.StringIO()):
            trio_loop.run_open_loop(mb, 5, lead, ev, poll_seconds=0.01)
        state = (mb / "STATE.md").read_text()
        it = re.search(r"^iteration:\s*(\d+)", state, re.M).group(1)
        key = LEDGER.load_key(sdir, create=True)
        at, aid, body = "2026-09-29T12:00:00Z", "abc123def456", "Human check: PASSED"
        LEDGER.append_record(sdir, LEDGER.make_record(key, answer_id=aid, loop="k", mailbox=mb,
                                                      root_mailbox=mb, iteration=it, at=at, body=body))
        (mb / "HUMAN.md").write_text(f"# Human answers\n\n## {at} — answer {aid} — iteration {it} — "
                                     f"trio-dash {LEDGER.entry_sig(key, at, aid, it, body)}\n\n"
                                     f"{LEDGER.quote_body(body)}")
        state = re.sub(r"^status:.*$", "status: running", state, flags=re.M)
        state = re.sub(r"^phase:.*$", "phase: idle", state, flags=re.M)
        (mb / "STATE.md").write_text(re.sub(r"^reason:.*\n", "", state, flags=re.M))
        seen = []

        class Rec:
            def __init__(self, inner):
                self.inner = inner

            def run(self, role, iteration, mailbox, context=None):
                blk = trio_loop.human_answer_block(mailbox, iteration, role,
                                                   (context or {}).get("kind"))
                seen.append(((context or {}).get("kind"), bool(blk)))
                return self.inner(role, iteration, mailbox, context)

        def lead_fn(role, i, m, c):
            if lead_changes:
                (m / "PLAN.md").write_text((m / "PLAN.md").read_text() + "\n<!-- cites abc123def456 -->\n")
            return 0

        def eval_fn(role, i, m, c):
            if c.get("kind") == "integration-eval":
                v.set_integration_verdict("VERDICT: SHIP")
            return 0

        with contextlib.redirect_stderr(io.StringIO()):
            code = trio_loop.run_open_loop(mb, 5, Rec(lead_fn), Rec(eval_fn), poll_seconds=0.01)
        return code, seen

    def test_open_loop_integration_eval_after_an_empty_lead_pass_gets_the_answer(self):
        code, seen = self._open_loop(lead_changes=False)
        self.assertEqual(code, 0)
        self.assertIn(("lead-pass", True), seen)
        self.assertIn(("integration-eval", True), seen)

    def test_open_loop_integration_eval_after_a_lead_change_gets_the_answer(self):
        code, seen = self._open_loop(lead_changes=True)
        self.assertEqual(code, 0)
        self.assertIn(("integration-eval", True), seen)

    def test_a_slice_eval_does_not_consume_and_appended_slice_sections_keep_the_stop(self):
        box = self.repo_with_stop()
        self.answer()
        text, _ = self.block(box, 3, "evaluator", "slice-eval")
        self.assertTrue(text)
        with open(box / "VERDICT.md", "a") as fh:
            fh.write("## slice s1 @" + "a" * 40 + " -- SHIP\nlooks right\n")
        text, _ = self.block(box, 3, "evaluator", "integration-eval")
        self.assertTrue(text)
        text, err = self.block(box, 3, "evaluator", "integration-eval")
        self.assertEqual(text, "")
        self.assertIn("consumed", err)


class InstallLayoutTests(_AnswerBase):
    """eval3 finding 2 (repros/installsh-trioctl-no-ledger.py, inverted with
    install.sh's own copy set)."""

    def install_bin(self) -> Path:
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        text = (REPO_ROOT / "install.sh").read_text()
        copies = re.findall(r'^\s*cp "\$ROOT/([^"]+)" "\$TRIOCTL_BIN_DIR/([^"/]+)"\s*$', text, re.M)
        self.assertIn(("metrics/human_ledger.py", "human_ledger.py"), copies)
        for src, dst in copies:
            shutil.copy(REPO_ROOT / src, bin_dir / dst)
        return bin_dir

    def test_install_sh_trioctl_delivers_the_answer(self):
        bin_dir = self.install_bin()
        self.assertFalse((bin_dir.parent / "metrics").exists())
        box = self.repo_with_stop()
        self.answer(text="Human check: PASSED")
        installed = _load("eval3_installed_trioctl", bin_dir / "trioctl")
        self.assertEqual(installed._human_ledger_paths()[0], bin_dir.resolve() / "human_ledger.py")
        text, err = _quiet(installed._human_answer_block, box, 3, "lead")
        self.assertIn("## Verified human answer (driver)", text, err)
        text, err = _quiet(installed._human_answer_block, box, 3, "evaluator")
        self.assertIn("> Human check: PASSED", text, err)

    def test_install_sh_dashboard_ships_the_modules_loop_actions_loads(self):
        text = (REPO_ROOT / "install.sh").read_text()
        section = text[text.index("  --dashboard)"):]
        section = section[:section.index(";;")]
        for name in ("human_ledger.py", "native_args.py"):
            self.assertIn(f'"$ROOT/metrics/{name}"', section)

    def test_the_release_layout_still_finds_metrics(self):
        trioctl = _load("eval3_release_trioctl", REPO_ROOT / "omnigent" / "trioctl")
        found = [p for p in trioctl._human_ledger_paths() if p.is_file()]
        self.assertEqual(found, [REPO_ROOT.resolve() / "metrics" / "human_ledger.py"])

    def test_without_any_ledger_module_nothing_is_passed(self):
        bin_dir = self.install_bin()
        (bin_dir / "human_ledger.py").unlink()
        box = self.repo_with_stop()
        self.answer()
        installed = _load("eval3_installed_trioctl2", bin_dir / "trioctl")
        text, err = _quiet(installed._human_answer_block, box, 3, "lead")
        self.assertEqual(text, "")
        self.assertIn("no answer ledger module", err)


class ModelsAndFuzzTests(_AnswerBase):
    """eval3 finding 4 (loop-nat-nested, inverted) and a schema fuzz."""

    def setUp(self):
        super().setUp()
        self.native = D._release_native(self.home)
        self.helper = str((self.native / "trio_native_step.py").resolve())
        self.box = _mailbox(self.root, "loop-nat",
                            "status: running\nphase: idle\niteration: 1\nmax_iterations: 3\n")

    def good(self, **extra):
        args = {"mailbox": str(self.box), "max_iterations": 3, "helper": self.helper,
                "run_token": "tok1"}
        args.update(extra)
        return args

    def validate(self, raw):
        return la.validate_native_args(raw, mailbox=self.box, helper=self.native / "trio_native_step.py")

    def test_every_models_value_type_is_a_validation_error(self):
        for models in ({"lead": {"x": 1}}, {"lead": ["claude-opus-5-5"]}, {"lead": 1},
                       {"lead": None}, {"lead": True}, {"lead": 1.5}, [], "claude-opus-5-5", 3,
                       None, {"lead": "claude-opus-5-5 "}, {"boss": "claude-opus-5-5"},
                       {"lead": {"claude-opus-5-5": 1}}):
            with self.subTest(models=models):
                with self.assertRaises(la.NativeArgsError) as cm:
                    self.validate(json.dumps(self.good(models=models)))
                self.assertIn("allowlist", str(cm.exception))
        self.assertEqual(self.validate(json.dumps(self.good(models={"lead": "claude-sonnet-5"})))
                         ["models"], {"lead": "claude-sonnet-5"})

    def test_the_nested_models_loop_is_a_refusal_not_a_500(self):
        (self.box / ".native-launch.json").write_text(json.dumps(
            {"session_id": D.GOOD_SESSION, "args": json.dumps(self.good(models={"lead": {"x": 1}}))}))
        (self.box / ".native-result.json").write_text(json.dumps(
            {"driver": "claude-workflow", "source": "end", "run_id": "wf_abc123"}))
        self.start_server()
        resume = self.fixes("loop-nat")["native_resume"]  # GET 200
        self.assertFalse(resume["applicable"])
        self.assertIn("allowlist", resume["reason"])
        status, data = self.confirmed("/api/loop/fix", {"root": str(self.root), "loop": "loop-nat",
                                                        "fix": "native_resume"})
        self.assertEqual(status, 409, data)
        self.assertFalse((self.native / "ran.argv").exists())

    def test_deep_or_huge_sidecars_are_never_a_500(self):
        (self.box / ".native-launch.json").write_text("[" * 100_000)
        (self.box / ".native-result.json").write_text('{"driver": "claude-workflow", "n": 1'
                                                      + "0" * 5000 + "}")
        (self.box / ".driver.json").write_text("{" * 100_000)
        (self.box / ".session.json").write_text('{"pid": 1' + "0" * 5000 + "}")
        self.start_server()
        self.assertIn("loop-nat", [l["name"] for l in self.board()["loops"]])
        self.assertFalse(self.fixes("loop-nat")["native_resume"]["applicable"])
        status, _ = self.post("/api/loop/fix", {"root": str(self.root), "loop": "loop-nat",
                                                "fix": "native_resume", "x": [[[[1]]]]})
        self.assertIn(status, (400, 409))
        status, data = D._request("POST", self.base + "/api/loop/fix", ("[" * 50_000).encode(),
                                  {"Content-Type": "application/json"})
        self.assertEqual(status, 400, data)

    def _value(self, rnd: random.Random, depth=0):
        kinds = ["int", "bigint", "neg", "float", "bool", "none", "str", "path", "model"]
        if depth < 4:
            kinds += ["list", "dict"]
        kind = rnd.choice(kinds)
        if kind == "int":
            return rnd.randint(0, 300)
        if kind == "bigint":
            return rnd.randint(10 ** 9, 10 ** 30)
        if kind == "neg":
            return -rnd.randint(0, 10)
        if kind == "float":
            return rnd.choice([3.0, 1e308, float("nan"), float("inf"), -0.0])
        if kind == "bool":
            return rnd.choice([True, False])
        if kind == "none":
            return None
        if kind == "str":
            return "".join(rnd.choice("ab /.\x00\n é\ud800~,$`'\"") for _ in range(rnd.randint(0, 12)))
        if kind == "path":
            return rnd.choice([str(self.box), str(self.box) + "/", str(self.box) + "/../loop-nat",
                               "/", "", "relative", str(self.box) + "\x00", self.helper])
        if kind == "model":
            return rnd.choice(["claude-opus-5-5", "claude-sonnet-5", "lead", "x"])
        if kind == "list":
            return [self._value(rnd, depth + 1) for _ in range(rnd.randint(0, 3))]
        return {rnd.choice(NA.MODEL_ROLES + NA.ARG_KEYS + ("x",)): self._value(rnd, depth + 1)
                for _ in range(rnd.randint(0, 3))}

    def test_fuzzed_args_are_always_a_clean_refusal_or_valid(self):
        rnd = random.Random(20260929)
        outcomes = {"ok": 0, "refused": 0}
        for _ in range(4000):
            args = {k: self._value(rnd) for k in rnd.sample(NA.ARG_KEYS + ("x", "models"),
                                                            rnd.randint(0, 5))}
            if rnd.random() < 0.5:
                args.setdefault("mailbox", str(self.box))
                args.setdefault("max_iterations", 3)
            for raw in (args, json.dumps(args, allow_nan=True) if rnd.random() < 0.7 else args):
                try:
                    out = self.validate(raw)
                except la.NativeArgsError:
                    outcomes["refused"] += 1
                else:
                    outcomes["ok"] += 1
                    self.assertTrue(set(out) <= set(NA.ARG_KEYS))
                    if "models" in out:
                        self.assertTrue(all(isinstance(m, str) for m in out["models"].values()))
        self.assertGreater(outcomes["refused"], 0)

    def test_fuzzed_raw_strings_and_types_are_always_a_clean_refusal(self):
        rnd = random.Random(7)
        good = json.dumps(self.good(models={"lead": "claude-opus-5-5"}))
        samples = ["[" * 100_000, "{" * 50_000, "1" * 5000, "NaN", "", "null", '"x"', b"{}",
                   bytearray(b"{"), 12, 1.5, None, [], object(), {1: 2}, {(1, 2): 3}]
        for _ in range(3000):
            chars = list(good)
            for _ in range(rnd.randint(1, 6)):
                i = rnd.randrange(len(chars))
                chars[i] = rnd.choice('{}[]",:\\\x00 é0-.eE ') if rnd.random() < 0.8 else ""
            samples.append("".join(chars))
        for raw in samples:
            try:
                self.validate(raw)
            except la.NativeArgsError:
                pass


class LineSeparatorTests(_AnswerBase):
    """eval3 finding 5 (repros/answer-linesep.py, inverted end to end)."""

    def test_answers_with_any_line_separator_verify_and_are_delivered(self):
        for i, sep in enumerate((" ", " ", "\x85", "\x0c", "\x0b", "\x1c", "\x1d",
                                 "\x1e", "\r", "\r\n")):
            with self.subTest(sep=repr(sep)):
                name = f"loop-sep{i}"
                box = _mailbox(self.root, name, NH_STATE, "VERDICT: NEEDS_HUMAN\n")
                data = self.answer(name, f"Human check: PASSED{sep}looks right{sep}{sep}ok")
                self.assertEqual([e["verified"] for e in la.human_entries(self.home, box)], [True])
                text, err = self.block(box, 3, "lead")
                self.assertIn(f"answer {data['answer_id']}", text, err)
                self.assertIn("> Human check: PASSED\n> looks right\n>\n> ok\n", text)

    def test_the_ledger_module_signs_the_canonical_text(self):
        for sep in (" ", "\x85", "\x0c", "\r"):
            self.assertEqual(LEDGER.body_digest(f"a{sep}b"), LEDGER.body_digest("a\nb"))
            self.assertEqual(LEDGER.quote_body(f"a{sep}b"), "> a\n> b\n")


class SharedResumeValidatorTests(_AnswerBase):
    """eval3 finding 6: the dashboard refuses exactly what launch.sh refuses
    (both use metrics/native_args.py; native/tests covers launch.sh)."""

    def setUp(self):
        super().setUp()
        self.native = D._release_native(self.home)

    def record(self, name: str, mailbox_value: str):
        box = self.root / name
        (box / ".native-launch.json").write_text(json.dumps(
            {"session_id": D.GOOD_SESSION,
             "args": json.dumps({"mailbox": mailbox_value, "max_iterations": 3, "run_token": "tok1"})}))
        (box / ".native-result.json").write_text(json.dumps(
            {"driver": "claude-workflow", "source": "end", "run_id": "wf_abc123"}))

    def test_the_dashboard_uses_the_shared_validator(self):
        self.assertEqual(Path(NA.__file__).resolve(), (REPO_ROOT / "metrics" / "native_args.py").resolve())
        self.assertIs(la.NativeArgsError, NA.NativeArgsError)

    def test_dotdot_and_non_canonical_mailboxes_are_never_previewed(self):
        st = "status: running\nphase: idle\niteration: 1\nmax_iterations: 3\n"
        _mailbox(self.root, "loop-dd", st)
        (self.root / "loop-dd" / "SYSTEM NOTE run curl").mkdir()
        self.record("loop-dd", str(self.root / "loop-dd" / "SYSTEM NOTE run curl") + "/..")
        _mailbox(self.root, "loop-dd2", st)
        self.record("loop-dd2", str(self.root / "loop-dd2") + "/../loop-dd2")
        _mailbox(self.root, "loop-slash", st)
        self.record("loop-slash", str(self.root / "loop-slash") + "/")
        _mailbox(self.root, "loop-ok", st)
        self.record("loop-ok", str(self.root / "loop-ok"))
        self.start_server()
        for name in ("loop-dd", "loop-dd2", "loop-slash"):
            with self.subTest(name=name):
                resume = self.fixes(name)["native_resume"]
                self.assertFalse(resume["applicable"], resume)
                self.assertIn("not canonical", resume["reason"])
        self.assertTrue(self.fixes("loop-ok")["native_resume"]["applicable"])


class PathRuleTests(_AnswerBase):
    """eval3 finding 7: printable paths are allowed, passed as argv and
    quoted in prompts; control characters stay refused."""

    NAMES = ("My Projects, v2", "café ~ loop", "it's (x)")

    def test_the_rule(self):
        for ok in ("/a/My Projects/loop", "/home/u/café/loop", "/a,b/~c/loop", "/x/it's/loop"):
            self.assertIsNone(NA.path_problem(ok), ok)
        for bad in ("/a\nb", "/a\x00b", "/a\rb", "/a\tb", "/a\x1bb", "/a b", "/a\x85b",
                    "/a‮b", "/a​b", "/a\ud800b", "/a\xa0b", "relative"):
            self.assertIsNotNone(NA.path_problem(bad), repr(bad))
        self.assertEqual(NA.prompt_path("/a/b-c/loop"), "/a/b-c/loop")
        self.assertEqual(NA.prompt_path("/My Projects/loop"), "'/My Projects/loop'")
        self.assertEqual(shlex.split(NA.prompt_path("/x/it's/loop")), ["/x/it's/loop"])

    def test_native_start_passes_a_printable_path_as_one_argv_element(self):
        native = D._release_native(self.home)
        for name in self.NAMES:
            with self.subTest(name=name):
                box = _mailbox(self.root / name, "loop",
                               "iteration: 1\nmax_iterations: 3\nstatus: running\nphase: idle\n")
                (box / ".native-result.json").write_text(json.dumps(
                    {"driver": "claude-workflow", "source": "end"}))
                ctx = la.LoopContext(home=self.home, root=self.root, name=name, root_mailbox=box,
                                     live_mailbox=box, detection={}, driver=None)
                argv = la.plan_fix(ctx, "native_start")["steps"][0]["argv"]
                self.assertEqual(argv[argv.index("--mailbox") + 1], str(box))
        self.assertFalse((native / "ran.argv").exists())

    def test_trioctl_prompts_quote_the_path(self):
        trioctl = _load("eval3_trioctl_prompt", REPO_ROOT / "omnigent" / "trioctl")
        for name in self.NAMES:
            with self.subTest(name=name):
                repo = self.root / name
                box = _mailbox(repo, "loop", "iteration: 1\nstatus: running\nphase: idle\n")
                runner = trioctl.OmnigentRunner(repo=repo, broker_client=object(), config={},
                                                interval=0, workspace=str(repo))
                for role in ("lead", "evaluator", "repair"):
                    if role == "repair":
                        (box / "VERDICT.md").write_text("VERDICT: ITERATE scope=local:a.py\n")
                    prompt = runner._prompt(role, 2, box, {})
                    quoted = shlex.quote(str(box.resolve()))
                    self.assertIn(quoted, prompt)
                    # every occurrence of the path is inside its quotes
                    rest = prompt.replace(quoted, "<Q>").replace(
                        shlex.quote(str(box / "VERDICT.md")), "<Q>")
                    self.assertNotIn(str(box.resolve()), rest)
        plain = trioctl.OmnigentRunner(repo=self.root, broker_client=object(), config={},
                                       interval=0, workspace=str(self.root))
        box = _mailbox(self.root, "loop-plain", "iteration: 1\nstatus: running\nphase: idle\n")
        self.assertIn(f"{box.resolve()}", plain._prompt("lead", 2, box, {}))
        self.assertNotIn(f"'{box.resolve()}'", plain._prompt("lead", 2, box, {}))

    def test_the_portable_driver_quotes_the_mailbox_override(self):
        box = _mailbox(self.root / "My Projects, v2", "loop",
                       "iteration: 3\nstatus: running\nphase: idle\n")
        out = self.home / "prompt.txt"
        run = D._script(self.home / "run-role", f"import shutil, sys; shutil.copy(sys.argv[1], {str(out)!r})")
        env = {"HARNESS": "generic", "RUN_LEAD": str(run), "RUN_EVAL": str(run)}
        with patch.dict(os.environ, env):
            self.assertEqual(trio_loop._PortableRunner().run("lead", 4, box, {}), 0)
        first = out.read_text().splitlines()[0]
        quoted = shlex.quote(str(box.resolve()))
        self.assertEqual(first, f"MAILBOX OVERRIDE: this run uses `{quoted}/` as the loop mailbox — "
                                f"every `loop/` path in the instructions below resolves to `{quoted}/`.")


class ConfirmBasisTests(_AnswerBase):
    """eval3 finding 10: reconcile_apply binds the held records, retire_ship
    the mailbox file set."""

    def ctx(self, box: Path):
        return la.LoopContext(home=self.home, root=self.root, name=box.name, root_mailbox=box,
                              live_mailbox=box, detection={}, driver=None)

    def test_the_held_records_are_in_the_basis(self):
        box = _mailbox(self.root, "loop-held", "iteration: 2\nstatus: needs_human\n")
        (box / ".sessions").mkdir()
        held = box / ".sessions" / "held-1.json"
        held.write_text(json.dumps({"session_id": "s1", "role": "builder", "hold": "unproven"}))
        with patch.dict(os.environ, {"FAKE_RECONCILE": "ready"}):
            first = la.plan_fix(self.ctx(box), "reconcile_apply")
            self.assertIn("held-1.json", first["basis"]["held"])
            held.write_text(json.dumps({"session_id": "s1", "role": "builder", "hold": "unproven",
                                        "iteration": 3}))
            second = la.plan_fix(self.ctx(box), "reconcile_apply")
        self.assertNotEqual(first["confirm_token"], second["confirm_token"])

    def test_the_retire_ship_token_binds_the_mailbox_file_set(self):
        _git(self.root, "init", "-q", "-b", "main")
        box = _mailbox(self.root, "loop-ret", "iteration: 2\nstatus: needs_retirement\n",
                       "VERDICT: SHIP\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "seed")
        first = la.plan_fix(self.ctx(box), "retire_ship")
        (box / "notes.md").write_text("an extra file the commit would stage\n")
        second = la.plan_fix(self.ctx(box), "retire_ship")
        self.assertEqual(second["basis"]["mailbox_files"]["count"],
                         first["basis"]["mailbox_files"]["count"] + 1)
        self.assertNotEqual(first["confirm_token"], second["confirm_token"])
        (box / "notes.md").write_text("changed\n")
        self.assertNotEqual(la.plan_fix(self.ctx(box), "retire_ship")["confirm_token"],
                            second["confirm_token"])

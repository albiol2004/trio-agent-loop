"""Agent identity by session start directory (dashboard/agent_identity.py).

Fixture git repositories are built in a temp dir (``agents/alpha`` exists only
inside that fixture); the real HOME and the real repository are never read.
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
MODULE_PATH = REPO_ROOT / "dashboard" / "agent_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_agent_identity", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ai = _load()


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _jsonl(*records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        # The temp dir itself may sit inside some other checkout: bound git
        # discovery here so "outside git" really is outside git.
        saved = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp)
        self.addCleanup(self._restore_env, saved)
        self.repo = self.tmp / "repo"
        (self.repo / "agents" / "alpha" / "sub").mkdir(parents=True)
        (self.repo / "other" / "dir").mkdir(parents=True)
        _git(self.tmp, "init", "-q", str(self.repo))
        _write(self.repo / "README", "x")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.outside = self.tmp / "plain"
        self.outside.mkdir()

    @staticmethod
    def _restore_env(saved):
        if saved is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = saved


class IdentityForStartDirTests(Fixture):
    def test_repo_root_agents_other_outside_and_none(self):
        f = ai.identity_for_start_dir
        self.assertEqual(f(str(self.repo))["identity"], "coordinator")
        self.assertEqual(f(str(self.repo))["rel"], "")
        sub = f(str(self.repo / "agents" / "alpha" / "sub"))
        self.assertEqual(sub["identity"], "alpha")
        self.assertEqual(sub["rel"], "agents/alpha/sub")
        self.assertEqual(f(str(self.repo / "agents" / "alpha"))["identity"],
                         "alpha")
        self.assertEqual(f(str(self.repo / "other" / "dir"))["identity"],
                         "unassigned")
        self.assertEqual(f(str(self.repo / "agents"))["identity"], "unassigned")
        out = f(str(self.outside))
        self.assertEqual(out["identity"], "unassigned")
        self.assertIsNone(out["common_dir"])
        self.assertEqual(f(None), {"identity": None, "start_dir": None,
                                   "common_dir": None, "rel": None})
        self.assertIsNone(f(42)["identity"])

    def test_linked_worktree_start_dir_has_the_same_identity(self):
        wt = self.tmp / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "side", str(wt))
        (wt / "agents" / "alpha").mkdir(parents=True, exist_ok=True)
        got = ai.identity_for_start_dir(str(wt / "agents" / "alpha"))
        self.assertEqual(got["identity"], "alpha")
        self.assertEqual(got["rel"], "agents/alpha")
        main = ai.identity_for_start_dir(str(self.repo / "agents" / "alpha"))
        self.assertEqual(got["common_dir"], main["common_dir"])
        self.assertEqual(ai.identity_for_start_dir(str(wt))["identity"],
                         "coordinator")

    def test_vanished_start_dir_resolves_without_raising(self):
        gone = self.repo / "agents" / "gamma" / "deep" / "gone"
        got = ai.identity_for_start_dir(str(gone))
        self.assertEqual(got["identity"], "gamma")
        self.assertEqual(ai.identity_for_start_dir(
            str(self.outside / "nope" / "x"))["identity"], "unassigned")

    def test_symlinked_start_dir_is_resolved(self):
        link = self.tmp / "link"
        os.symlink(self.repo / "agents" / "alpha", link)
        self.assertEqual(ai.identity_for_start_dir(str(link))["identity"],
                         "alpha")


class ReaderTests(Fixture):
    def test_claude_start_dir_is_first_cwd_only(self):
        path = _write(self.tmp / "c.jsonl", _jsonl(
            {"type": "queue-operation"},
            {"type": "user", "cwd": "/first"},
            {"type": "user", "cwd": "/elsewhere"}))
        self.assertEqual(ai.claude_start_dir(path), "/first")

    def test_claude_start_dir_bounded_and_tolerant(self):
        path = _write(self.tmp / "c.jsonl",
                      "not json\n" + _jsonl({"a": 1}) * 5
                      + _jsonl({"cwd": "/late"}))
        self.assertIsNone(ai.claude_start_dir(path, max_lines=3))
        self.assertEqual(ai.claude_start_dir(path), "/late")
        self.assertIsNone(ai.claude_start_dir(self.tmp / "missing.jsonl"))

    def test_codex_start_dir_is_session_meta_payload_cwd(self):
        path = _write(self.tmp / "r.jsonl", _jsonl(
            {"type": "session_meta", "payload": {"id": "x", "cwd": "/cx"}},
            {"type": "turn_context", "payload": {"cwd": "/later"}}))
        self.assertEqual(ai.codex_start_dir(path), "/cx")
        other = _write(self.tmp / "o.jsonl", _jsonl({"type": "event"}))
        self.assertIsNone(ai.codex_start_dir(other))

    def test_export_start_dir_is_header_workspace(self):
        path = _write(self.tmp / "e.jsonl", _jsonl(
            {"id": "s", "workspace": "/ws"}, {"type": "message"}))
        self.assertEqual(ai.export_start_dir(path), "/ws")
        typed = _write(self.tmp / "t.jsonl", _jsonl(
            {"type": "message", "workspace": "/ws"}))
        self.assertIsNone(ai.export_start_dir(typed))
        self.assertIsNone(ai.export_start_dir(self.tmp / "missing.jsonl"))


class StampInheritTests(Fixture):
    def test_stamp_and_unavailable(self):
        row = ai.stamp({"id": "a"}, str(self.repo / "agents" / "alpha"),
                       "claude-transcript-cwd")
        self.assertEqual(row["identity"], "alpha")
        self.assertEqual(row["identity_source"], "claude-transcript-cwd")
        self.assertEqual(row["start_dir"],
                         str(self.repo / "agents" / "alpha"))
        none = ai.stamp({"id": "b"}, None, "broker-workspace")
        self.assertIsNone(none["identity"])
        self.assertEqual(none["identity_source"], "unavailable")
        self.assertIsNone(none["start_dir"])

    def test_inherit_copies_identity_and_prefixes_source(self):
        parent = ai.stamp({}, str(self.repo / "agents" / "alpha"),
                          "claude-transcript-cwd")
        child = ai.inherit({"id": "c", "start_dir": "/tmp/wt"}, parent)
        self.assertEqual(child["identity"], "alpha")
        self.assertEqual(child["identity_source"],
                         "inherited:claude-transcript-cwd")
        self.assertEqual(child["start_dir"], parent["start_dir"])
        grand = ai.inherit({}, child)
        self.assertEqual(grand["identity_source"],
                         "inherited:claude-transcript-cwd")


if __name__ == "__main__":
    unittest.main()

"""Transcript index: sessions found by id across Claude profiles, Codex, Cursor.

All tests run against a fixture HOME in a temp dir; the real HOME is never read.
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
MODULE_PATH = REPO_ROOT / "dashboard" / "transcript_index.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "trio_dashboard_transcript_index", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ti = _load()

SID = "c944a81b-27f4-465f-82d6-e3639ec84de7"
OTHER = "11111111-2222-3333-4444-555555555555"
SLUG = "-work-repo"


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
        self.home = self.tmp / "home"
        self.profile = self.home / ".profiles" / "p" / "claude"
        self.profile.mkdir(parents=True)
        os.symlink(self.profile, self.home / ".claude")
        self.projects = self.profile / "projects" / SLUG
        self.repo = self.tmp / "work" / "repo"
        self.mailbox = self.repo / "loop-x"
        self.mailbox.mkdir(parents=True)

    # -- builders ------------------------------------------------------

    def claude_session(self, sid=SID, agents=("a",), workflow="wf_x"):
        parent = _write(self.projects / f"{sid}.jsonl", _jsonl(
            {"type": "queue-operation", "timestamp": "2026-10-02T08:47:14.363Z"},
            {"type": "user", "timestamp": "2026-10-02T08:47:15.000Z"}))
        base = self.projects / sid / "subagents"
        if workflow:
            base = base / "workflows" / workflow
        for name in agents:
            _write(base / f"agent-{name}.jsonl", _jsonl(
                {"type": "user", "timestamp": "2026-10-02T09:00:00.000Z"}))
            _write(base / f"agent-{name}.meta.json", json.dumps(
                {"agentType": "trio-builder", "description": f"builder {name}",
                 "worktreePath": "/tmp/wt", "model": "sonnet"}))
        return parent

    def launch(self, sid=SID, mailbox=None):
        mb = mailbox or self.mailbox
        _write(mb / ".native-launch.json", json.dumps({
            "session_id": sid,
            "args": json.dumps({"mailbox": str(mb), "run_token": "ls-x"})}))

    def codex_rollout(self, sid, cwd, day="2026/10/02"):
        path = (self.home / ".profiles" / "p" / "codex" / "sessions" / day
                / f"rollout-2026-10-02T08-59-21-{sid}.jsonl")
        _write(path, _jsonl({
            "timestamp": "2026-10-02T08:59:22.576Z", "type": "session_meta",
            "payload": {"id": sid, "cwd": str(cwd),
                        "timestamp": "2026-10-02T08:59:21.089Z"}}))
        return path

    def index(self):
        return ti.TranscriptIndex(self.home, ttl=0.0)


class ConfigRootsTests(Fixture):
    def test_symlinked_claude_and_profile_dir_returned_once(self):
        roots = ti.config_roots(self.home)
        self.assertEqual(roots, [self.profile])

    def test_second_profile_listed_and_missing_dirs_ignored(self):
        other = self.home / ".profiles" / "q" / "claude"
        other.mkdir(parents=True)
        (self.home / ".profiles" / "r").mkdir()  # no claude dir
        self.assertEqual(ti.config_roots(self.home), [self.profile, other])


class SessionsForMailboxTests(Fixture):
    def test_parent_and_workflow_subagent_with_meta(self):
        self.claude_session()
        self.launch()
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual([r["id"] for r in rows], [SID, "agent-a"])
        parent, sub = rows
        self.assertEqual(parent["kind"], "parent")
        self.assertEqual(parent["harness"], "claude")
        self.assertEqual(parent["source"], "native-launch")
        self.assertEqual(parent["status"], "ok")
        self.assertEqual(parent["timestamp"], "2026-10-02T08:47:14.363Z")
        self.assertEqual(parent["path"],
                         str(self.projects / f"{SID}.jsonl"))
        self.assertGreater(parent["size"], 0)
        self.assertEqual(sub["kind"], "subagent")
        self.assertEqual(sub["parent_id"], SID)
        self.assertEqual(sub["parent_path"], parent["path"])
        self.assertEqual(sub["agent_type"], "trio-builder")
        self.assertEqual(sub["description"], "builder a")
        self.assertEqual(sub["workflow"], "wf_x")

    def test_every_jsonl_under_subagents_is_a_row(self):
        self.claude_session(agents=("a", "b"))
        _write(self.projects / SID / "subagents" / "agent-top.jsonl", "{}\n")
        self.launch()
        rows = self.index().sessions_for_mailbox(self.mailbox)
        subs = {r["id"]: r for r in rows if r["kind"] == "subagent"}
        self.assertEqual(set(subs), {"agent-a", "agent-b", "agent-top"})
        self.assertIsNone(subs["agent-top"]["workflow"])
        self.assertIsNone(subs["agent-top"]["agent_type"])

    def test_unreferenced_session_in_same_slug_not_returned(self):
        self.claude_session()
        stray = self.claude_session(sid=OTHER, agents=("z",))
        self.launch()
        index = self.index()
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertNotIn(OTHER, [r["id"] for r in rows])
        self.assertNotIn("agent-z", [r["id"] for r in rows])
        self.assertFalse(any(r["path"] and OTHER in r["path"] for r in rows))
        self.assertFalse(index.is_indexed(stray))

    def test_no_references_no_rows(self):
        self.claude_session()
        self.assertEqual(self.index().sessions_for_mailbox(self.mailbox), [])

    def test_referenced_id_without_file_is_one_deleted_row(self):
        self.launch(sid=OTHER)
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], OTHER)
        self.assertEqual(row["status"], "deleted")
        self.assertIsNone(row["path"])
        self.assertIsNone(row["size"])
        self.assertIn("deleted by retention", row["label"])

    def test_session_found_in_second_profile(self):
        other = self.home / ".profiles" / "q" / "claude" / "projects" / "-s"
        _write(other / f"{OTHER}.jsonl", "{}\n")
        self.launch(sid=OTHER)
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual([r["status"] for r in rows], ["ok"])
        self.assertEqual(rows[0]["path"], str(other / f"{OTHER}.jsonl"))

    def test_launch_naming_other_mailbox_ignored(self):
        self.claude_session()
        _write(self.mailbox / ".native-launch.json", json.dumps({
            "session_id": SID,
            "args": json.dumps({"mailbox": str(self.tmp / "elsewhere")})}))
        self.assertEqual(self.index().sessions_for_mailbox(self.mailbox), [])

    def test_unsafe_session_id_ignored(self):
        _write(self.mailbox / ".native-launch.json",
               json.dumps({"session_id": "../../etc/passwd"}))
        self.assertEqual(self.index().sessions_for_mailbox(self.mailbox), [])

    def test_symlink_escaping_projects_not_followed(self):
        outside = _write(self.tmp / "secret.jsonl", "{}\n")
        self.projects.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, self.projects / f"{OTHER}.jsonl")
        self.launch(sid=OTHER)
        index = self.index()
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual([r["status"] for r in rows], ["deleted"])
        self.assertFalse(index.is_indexed(outside))

    def test_session_sidecar_token_resolves_only_when_unique(self):
        self.claude_session()
        _write(self.mailbox / ".session.json", json.dumps(
            {"driver": "claude-workflow", "session": "ls-c944a81b27f4"}))
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual(rows[0]["id"], SID)
        self.assertEqual(rows[0]["source"], "session-sidecar")
        # ambiguous prefix -> ignored
        self.claude_session(sid="c944a81b-27f4-0000-0000-000000000000",
                            agents=())
        self.assertEqual(self.index().sessions_for_mailbox(self.mailbox), [])

    def test_session_sidecar_token_with_no_match_ignored(self):
        _write(self.mailbox / ".session.json", json.dumps(
            {"session": "ls-deadbeefcafe"}))
        self.assertEqual(self.index().sessions_for_mailbox(self.mailbox), [])

    def test_native_runs_files_registry_and_broker_refs(self):
        self.claude_session()
        _write(self.mailbox / ".native-runs" /
               f"{SID}.20261002T084712Z.start.json", "{}")
        index = self.index()
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual([r["source"] for r in rows if r["kind"] == "parent"],
                         ["native-runs"])
        # registry dict naming another mailbox is ignored
        mb2 = self.tmp / "mb2"
        mb2.mkdir()
        runs = [{"mailbox": str(self.mailbox), "session_id": OTHER},
                {"mailbox": str(self.mailbox), "run_token": "ls-c944a81b27f4"}]
        self.assertEqual(index.sessions_for_mailbox(mb2, native_runs=runs), [])
        # same mailbox, run_token resolution
        rows = index.sessions_for_mailbox(
            mb2, native_runs=[{"mailbox": str(mb2),
                               "run_token": "ls-c944a81b27f4"}])
        self.assertEqual(rows[0]["id"], SID)
        # broker session with external id and claude wrapper
        rows = index.sessions_for_mailbox(mb2, broker_sessions=[
            {"id": "ses_1", "workspace": str(mb2), "external_session_id": SID,
             "labels": {"omnigent.wrapper": "claude-code"}}])
        self.assertEqual((rows[0]["id"], rows[0]["source"]), (SID, "broker"))


class MailboxRefsTests(Fixture):
    def test_refs_deduped_and_typed(self):
        self.launch()
        _write(self.mailbox / ".native-runs" / f"{SID}.1.start.json", "{}")
        refs = ti.mailbox_session_refs(self.mailbox, broker_sessions=[
            {"id": "ses_1", "external_session_id": OTHER,
             "labels": {"omnigent.wrapper": "codex"}},
            {"id": "ses_2", "labels": {"omnigent.wrapper": "weird"}}])
        self.assertEqual(
            [(r["session_id"], r["harness"], r["source"]) for r in refs],
            [(SID, "claude", "native-launch"),
             (OTHER, "codex", "broker"),
             ("ses_2", "omnigent", "broker")])

    def test_token_ignored_without_resolver(self):
        _write(self.mailbox / ".session.json",
               json.dumps({"session": "ls-c944a81b27f4"}))
        self.assertEqual(ti.mailbox_session_refs(self.mailbox), [])

    def test_unreadable_sidecars_are_not_errors(self):
        _write(self.mailbox / ".native-launch.json", "{not json")
        _write(self.mailbox / ".session.json", "[]")
        self.assertEqual(ti.mailbox_session_refs(self.mailbox), [])


class CodexCursorTests(Fixture):
    def test_codex_cwd_equal_to_mailbox_linked_repo_root_not(self):
        linked = self.codex_rollout(OTHER, self.mailbox)
        unlinked = self.codex_rollout(
            "22222222-3333-4444-5555-666666666666", self.repo)
        index = self.index()
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual([(r["id"], r["harness"], r["status"]) for r in rows],
                         [(OTHER, "codex", "ok")])
        self.assertEqual(rows[0]["path"], str(linked))
        self.assertEqual(rows[0]["timestamp"], "2026-10-02T08:59:21.089Z")
        self.assertTrue(index.is_indexed(linked))
        self.assertFalse(index.is_indexed(unlinked))
        # the repo root as a mailbox sees only its own cwd match
        rows = index.sessions_for_mailbox(self.repo)
        self.assertEqual([r["id"] for r in rows],
                         ["22222222-3333-4444-5555-666666666666"])

    def test_codex_explicit_id_reference_and_deleted(self):
        self.codex_rollout(OTHER, "/somewhere/else")
        rows = self.index().sessions_for_mailbox(self.mailbox, broker_sessions=[
            {"id": "s", "external_session_id": OTHER,
             "labels": {"omnigent.wrapper": "codex"}},
            {"id": "t", "external_session_id": "99999999-0000-0000-0000-000000000000",
             "labels": {"omnigent.wrapper": "codex"}}])
        self.assertEqual([(r["harness"], r["status"]) for r in rows],
                         [("codex", "ok"), ("codex", "deleted")])

    def test_cursor_linked_by_recorded_cwd_only(self):
        chats = self.home / ".cursor" / "chats" / "h1"
        _write(chats / OTHER / "meta.json",
               json.dumps({"cwd": str(self.mailbox), "title": "T",
                           "createdAtMs": 1790349208730}))
        _write(chats / "33333333-3333-3333-3333-333333333333" / "meta.json",
               json.dumps({"cwd": str(self.repo)}))
        index = self.index()
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual([(r["id"], r["harness"], r["label"]) for r in rows],
                         [(OTHER, "cursor", "T")])
        self.assertFalse(index.is_indexed(
            chats / "33333333-3333-3333-3333-333333333333" / "meta.json"))


class IsIndexedTests(Fixture):
    def test_returned_true_unreturned_false(self):
        parent = self.claude_session()
        stray = self.claude_session(sid=OTHER, agents=("z",))
        stray_sub = self.projects / OTHER / "subagents" / "workflows" / \
            "wf_x" / "agent-z.jsonl"
        self.launch()
        index = self.index()
        self.assertFalse(index.is_indexed(parent))  # nothing returned yet
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertTrue(index.is_indexed(parent))
        for row in rows:
            self.assertTrue(index.is_indexed(row["path"]))
        self.assertFalse(index.is_indexed(stray))
        self.assertFalse(index.is_indexed(stray_sub))
        self.assertFalse(index.is_indexed(self.tmp / "nope.jsonl"))
        self.assertFalse(index.is_indexed(""))

    def test_symlink_to_returned_file_resolves_true(self):
        parent = self.claude_session()
        self.launch()
        index = self.index()
        index.sessions_for_mailbox(self.mailbox)
        link = self.tmp / "link.jsonl"
        os.symlink(parent, link)
        self.assertTrue(index.is_indexed(link))


class CacheTests(Fixture):
    def test_ttl_reuses_listing_then_sees_new_subagent_after_expiry(self):
        self.claude_session()
        self.launch()
        index = ti.TranscriptIndex(self.home, ttl=3600.0)
        first = index.sessions_for_mailbox(self.mailbox)
        _write(self.projects / SID / "subagents" / "workflows" / "wf_x" /
               "agent-new.jsonl", "{}\n")
        cached = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual(len(cached), len(first))
        fresh = self.index().sessions_for_mailbox(self.mailbox)
        self.assertIn("agent-new", [r["id"] for r in fresh])


def _git_init(path: Path) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class IdentityTests(Fixture):
    """Every descriptor carries identity / identity_source / start_dir."""

    def setUp(self):
        super().setUp()
        # Bound git discovery to the fixture: the temp dir may itself sit
        # inside another checkout.
        saved = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp)
        self.addCleanup(
            lambda: os.environ.__setitem__("GIT_CEILING_DIRECTORIES", saved)
            if saved is not None
            else os.environ.pop("GIT_CEILING_DIRECTORIES", None))
        _git_init(self.repo)
        self.alpha = self.repo / "agents" / "alpha"
        self.alpha.mkdir(parents=True)
        self.wt = self.tmp / "tmp-worktree"
        self.wt.mkdir()

    def parent_with_agents(self, sid=SID, cwd=None, later="/elsewhere"):
        parent = _write(self.projects / f"{sid}.jsonl", _jsonl(
            {"type": "queue-operation", "timestamp": "2026-10-02T08:47:14Z"},
            {"type": "user", "cwd": str(cwd or self.alpha),
             "timestamp": "2026-10-02T08:47:15Z"},
            {"type": "user", "cwd": later}))
        base = self.projects / sid / "subagents"
        for sub in (base / "agent-a.jsonl",
                    base / "workflows" / "wf_x" / "agent-b.jsonl"):
            _write(sub, _jsonl({"type": "user", "cwd": str(self.wt)}))
        return parent

    def test_parent_identity_is_first_cwd_and_subagents_inherit(self):
        self.parent_with_agents()
        self.launch()
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual(len(rows), 3)
        parent = rows[0]
        self.assertEqual((parent["identity"], parent["identity_source"]),
                         ("alpha", "claude-transcript-cwd"))
        self.assertEqual(parent["start_dir"], str(self.alpha))
        for sub in rows[1:]:
            self.assertEqual(sub["kind"], "subagent")
            self.assertEqual((sub["identity"], sub["identity_source"]),
                             ("alpha", "inherited:claude-transcript-cwd"))
            self.assertEqual(sub["start_dir"], str(self.alpha))

    def test_broker_workspace_beats_transcript_cwd(self):
        self.parent_with_agents()
        rows = self.index().sessions_for_mailbox(self.mailbox, broker_sessions=[
            {"id": "b1", "external_session_id": SID, "workspace": str(self.repo),
             "labels": {"omnigent.wrapper": "claude-code"}}])
        self.assertEqual(rows[0]["source"], "broker")
        self.assertEqual((rows[0]["identity"], rows[0]["identity_source"]),
                         ("coordinator", "broker-workspace"))
        self.assertEqual(rows[1]["identity_source"],
                         "inherited:broker-workspace")

    def test_broker_without_workspace_falls_back_to_transcript_cwd(self):
        self.parent_with_agents()
        rows = self.index().sessions_for_mailbox(self.mailbox, broker_sessions=[
            {"id": "b1", "external_session_id": SID, "workspace": "",
             "labels": {"omnigent.wrapper": "claude-code"}}])
        self.assertEqual((rows[0]["identity"], rows[0]["identity_source"]),
                         ("alpha", "claude-transcript-cwd"))

    def test_deleted_rows_use_broker_workspace_else_unavailable(self):
        self.launch()
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual([(r["status"], r["identity"], r["identity_source"],
                           r["start_dir"]) for r in rows],
                         [("deleted", None, "unavailable", None)])
        rows = self.index().sessions_for_mailbox(
            self.tmp / "other-mailbox", broker_sessions=[
                {"id": "b1", "external_session_id": SID,
                 "workspace": str(self.repo),
                 "labels": {"omnigent.wrapper": "claude-code"}},
                {"id": "b2", "external_session_id": OTHER, "workspace": "",
                 "labels": {"omnigent.wrapper": "claude-code"}}])
        self.assertEqual(
            [(r["status"], r["identity"], r["identity_source"]) for r in rows],
            [("deleted", "coordinator", "broker-workspace"),
             ("deleted", None, "unavailable")])

    def test_fork_inherits_its_source_sessions_identity(self):
        self.parent_with_agents(cwd=self.repo / "other")
        broker = [
            {"id": "src", "external_session_id": OTHER,
             "workspace": str(self.alpha),
             "labels": {"omnigent.wrapper": "codex"}},
            {"id": "fork", "external_session_id": SID,
             "workspace": str(self.repo),
             "labels": {"omnigent.wrapper": "claude-code",
                        "omnigent.fork.source_id": "src"}}]
        rows = self.index().sessions_for_mailbox(
            self.mailbox, broker_sessions=broker)
        fork = [r for r in rows if r["id"] == SID][0]
        self.assertEqual((fork["identity"], fork["identity_source"]),
                         ("alpha", "inherited:broker-workspace"))
        # a source outside the listing is not followed
        rows = self.index().sessions_for_mailbox(
            self.mailbox, broker_sessions=broker[1:])
        self.assertEqual(rows[0]["identity"], "coordinator")

    def test_codex_and_cursor_rows_use_their_recorded_cwd(self):
        self.codex_rollout(OTHER, self.alpha)
        chats = self.home / ".cursor" / "chats" / "h1"
        _write(chats / "33333333-3333-3333-3333-333333333333" / "meta.json",
               json.dumps({"cwd": str(self.mailbox)}))
        rows = self.index().sessions_for_mailbox(self.alpha)
        self.assertEqual([(r["harness"], r["identity"], r["identity_source"])
                          for r in rows],
                         [("codex", "alpha", "codex-session-meta")])
        rows = self.index().sessions_for_mailbox(self.mailbox)
        self.assertEqual([(r["harness"], r["identity"], r["identity_source"])
                          for r in rows],
                         [("cursor", "unassigned", "cursor-cwd")])

    def test_start_dir_read_is_cached_per_path_and_mtime(self):
        parent = self.parent_with_agents()
        self.launch()
        index = self.index()
        module = ti._identity()
        calls = []
        original = module.claude_start_dir
        module.claude_start_dir = lambda p, **kw: (
            calls.append(p), original(p, **kw))[1]
        self.addCleanup(setattr, module, "claude_start_dir", original)
        index.sessions_for_mailbox(self.mailbox)
        index.sessions_for_mailbox(self.mailbox)
        self.assertEqual(len(calls), 1)
        _write(parent, _jsonl({"type": "user", "cwd": str(self.repo)}))
        os.utime(parent, ns=(1, 2_000_000_000))
        rows = index.sessions_for_mailbox(self.mailbox)
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[0]["identity"], "coordinator")


if __name__ == "__main__":
    unittest.main()

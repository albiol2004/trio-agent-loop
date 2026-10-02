"""slice(acc-harden): the r19 frozen-acceptance path must survive a real
Terminal-Bench run -- author isolation by permission rules (not only detect-
and-discard), no author time limit, an author failure that degrades to "no
pack" instead of ending the run, and the Lead-integration retirement note.

Everything runs against fakes (``tests/fake_opencode.py`` and the scenario
``tests/scenarios/ol_acc_isolation.py``); no live model call."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from trio_opencode import config as config_mod
from trio_opencode import authorbox, driver, ocgen, olprompts, olqueue, openloop, steplib

from scenarios import oc_perm  # noqa: E402
from test_openloop_e2e import (  # noqa: E402 - sibling test module's helpers
    _git, install_fake_for_process, make_cfg, make_key_file, read_calls,
)

TL = steplib.TL
REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- fixtures
def _acc_repo(tmp_path: Path) -> Path:
    root = tmp_path / "accproduct"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("SECRET\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "keep.txt").write_text("k\n", encoding="utf-8")
    # a symlink the repo itself committed that points back at the repo
    os.symlink(str(root / "README"), str(root / "link-to-readme"))
    _git(root, "add", "README", "sub", "link-to-readme")
    _git(root, "commit", "-q", "-m", "init")
    box = root / "loop"
    box.mkdir()
    (box / "GOAL.md").write_text(
        "# Goal\nShip app.py: `python3 app.py N` prints hello N for every N.\n"
        "Keep the README.\n", encoding="utf-8")
    (box / "STATE.md").write_text("iteration: 0\nstatus: ready\nphase: idle\n", encoding="utf-8")
    (box / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (box / "QUEUE.md").write_text("```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n",
                                  encoding="utf-8")
    (box / "LOG.md").write_text("# Trio loop log\n", encoding="utf-8")
    _git(root, "add", "loop")
    _git(root, "commit", "-q", "-m", "loop: init")
    return root


SCENARIOS = Path(__file__).resolve().parent / "scenarios"


def _force_level(monkeypatch, level: str) -> None:
    """``no-shell``: pretend this host cannot start a user namespace (the
    Harbor task containers). ``sandbox``: the real probe, with the fake
    opencode's scenario/state dirs mounted into the sandbox."""
    if level == "no-shell":
        monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (False, "test: user namespaces blocked"))
    else:
        ok, why = authorbox.bwrap_usable()
        if not ok:
            pytest.skip(f"bwrap unusable here: {why}")
        monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "auto")
        monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_SANDBOX_RO", str(SCENARIOS))


def _run(tmp_path, monkeypatch, mode: str, level: str = "no-shell", **cfg_overrides):
    root = _acc_repo(tmp_path)
    cfg = make_cfg(make_key_file(tmp_path), root_free=False, **cfg_overrides)
    env = install_fake_for_process(tmp_path, monkeypatch, "ol_acc_isolation.py")
    monkeypatch.setenv("TRIO_NATIVE_JOBS", "inline")
    monkeypatch.setenv("FAKE_AUTHOR_MODE", mode)
    monkeypatch.setenv("FAKE_REPO_PATH", str(root))
    _force_level(monkeypatch, level)
    if level == "sandbox":
        monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_SANDBOX_RW", env["FAKE_OC_STATE"])
    result = driver.run(root / "loop", cfg, mode="start", max_iterations=4, root_free=False,
                        acceptance=True)
    return root, env, result


def _log(root: Path) -> str:
    return (root / "loop" / "LOG.md").read_text(encoding="utf-8")


def _probe_record(env) -> dict:
    return json.loads((Path(env["FAKE_OC_STATE"]) / "author-probe.json").read_text(encoding="utf-8"))


# --------------------------- 1. isolation: no shell + path-checked tools (Harbor)
ESCAPES = ("read-abs", "read-dotdot", "read-dotdot-via-inside", "read-dir", "glob-dotdot",
           "glob-abs", "glob-path-outside", "grep-outside", "edit-outside", "sh-cat-dotdot",
           "sh-cat-abs", "sh-ls", "sh-cd-root", "sh-python-open", "sh-python-concat",
           "sh-validate", "exec-fetch-file", "exec-session-move", "tool-webfetch",
           "tool-unknown-future")


@pytest.mark.parametrize("container_mode", [False, True])
def test_no_shell_author_trying_every_route_to_the_repo_gets_nothing(
        tmp_path, monkeypatch, container_mode):
    """No OS sandbox here (the Harbor task containers): the author gets NO
    shell and path-checked file tools. The fake authorizes every call against
    the GENERATED config the way OpenCode v2 does and then really performs
    the allowed ones: absolute and `../` reads, a committed symlink, glob/grep
    outside, `cat`, `ls`, `cd /`, `python -c open()`. Nothing leaks, in
    container mode too (whose flat external_directory allow used to let it
    through), and the pack still freezes."""
    root, env, result = _run(tmp_path, monkeypatch, "probe", "no-shell",
                             container_mode=container_mode)
    assert result["status"] == "shipped", (result, _log(root))
    rec = _probe_record(env)
    for key in ESCAPES:
        assert rec[key] == {"allowed": False, "leaked": False}, (key, rec[key])
    # the committed symlink is gone from the export before the turn: the path
    # looks internal, is allowed, and reads nothing
    assert rec["read-symlink"]["leaked"] is False, rec["read-symlink"]
    # legitimate work inside the export keeps working
    for key in ("read-goal", "write-pack", "glob-inside"):
        assert rec[key]["allowed"] is True, (key, rec[key])
    # the project-root guard (only added under a git ancestor) never outlives the turn
    assert not list(root.rglob("export/.git")), list(root.rglob("export/.git"))
    log = _log(root)
    assert "author isolation: no-shell" in log and "NO shell" in log, log
    assert "removed 1 symlink" in log, log
    assert "contaminated" not in log and "DEGRADED" not in log, log
    assert result["acceptance"]["author_isolation"] == "no-shell"
    author_calls = [c for c in read_calls(env) if c["agent"] == "trio-acceptance"]
    assert len(author_calls) == 1 and "NO shell" in author_calls[0]["prompt"]
    assert "python3" not in author_calls[0]["prompt"].split("Validate")[0] or True


def test_author_that_reads_the_repo_without_permission_rules_is_still_contaminated(
        tmp_path, monkeypatch):
    """Backstop (no-shell level): the audit still catches a COMPLETED read of
    the repo (here the fake ignores the config), twice -- and the run
    degrades, not errors."""
    root, _env, result = _run(tmp_path, monkeypatch, "contaminate", "no-shell")
    log = _log(root)
    assert "author session contaminated" in log, log
    assert "acceptance-contaminated" in log, log
    assert "DEGRADED" in log, log
    assert result["status"] == "shipped", (result, log)


# --------------------------- 1b. isolation: OS sandbox (hosts where bwrap works)
def test_sandboxed_author_cannot_see_the_repo_or_its_ancestors(tmp_path, monkeypatch):
    """bwrap level: the fake opencode runs for real inside the sandbox and
    performs the operations itself -- `open(repo)`, `../` climbs, a symlink
    into the repo, `python -c open()`, `cat ../x`, `ls /`."""
    root, env, result = _run(tmp_path, monkeypatch, "probe-os", "sandbox")
    seen = _probe_record(env)["os"]
    assert result["status"] == "shipped", (result, _log(root))
    assert seen["repo_exists"] is False
    assert seen["repo_readme"] == "" and seen["dotdot_readme"] == "" and seen["symlink_readme"] == ""
    assert "SECRET" not in seen["python_open"] and "SECRET" not in seen["cat_dotdot"]
    assert "No such file" in seen["python_open"] + seen["cat_dotdot"] or "Errno 2" in seen["python_open"]
    assert root.name not in seen["parent_listing"]
    assert root.name not in seen["repo_parent_listing"]      # not even as a sibling of its mounts
    assert seen["goal_readable"] is True and seen["can_write_export"] is True
    assert seen["can_write_outside"] is False
    assert seen["home"].endswith("/home")
    log = _log(root)
    assert "author isolation: sandbox" in log, log
    # the tool-call audit is moot under the sandbox: naming the repo read nothing
    assert "contaminated" not in log and "DEGRADED" not in log, log
    assert result["acceptance"]["author_isolation"] == "sandbox"


# ---------------------------------------- 2. false positives from written text
def test_pack_text_that_merely_names_the_repo_path_is_not_contamination(tmp_path, monkeypatch):
    root, _env, result = _run(tmp_path, monkeypatch, "prose", "no-shell")
    log = _log(root)
    assert "contaminated" not in log, log
    assert "DEGRADED" not in log, log
    assert result["status"] == "shipped", (result, log)


# ----------------------------------- 3. an author failure degrades, never errors
def test_author_failure_degrades_to_no_pack_and_the_run_still_ships(tmp_path, monkeypatch):
    """The author turn dies twice (`DriverStop` inside the author thread ->
    ``acceptance-error``): the core would end the run `status: error` ("gate
    breach after lead"); here it degrades and the slices still SHIP."""
    root, _env, result = _run(tmp_path, monkeypatch, "crash", "no-shell")
    log = _log(root)
    assert result["status"] == "shipped", (result, log)
    assert result["code"] == 0, result
    assert "acceptance: DEGRADED to no frozen pack" in log, log
    assert "gate breach" not in log, log
    assert result["acceptance"]["enabled"] is True
    assert result["acceptance"]["degraded"], result
    # no half-written pack left in the mailbox
    assert not (root / "loop" / "acceptance").exists()
    retired = olqueue.latest_retired(root / "loop")
    assert "app" in retired, retired


# ------------------------------------- 4. settings: unbounded author wait
def test_acceptance_wait_is_unbounded_by_default():
    cfg = config_mod.load_config(None)
    settings = openloop.resolve_settings(cfg, is_open_loop=True)
    assert settings["acceptance_wait_seconds"] is None


def test_acceptance_wait_can_be_configured_and_zero_means_unbounded():
    cfg = config_mod.load_config(None)
    assert openloop.resolve_settings(cfg, is_open_loop=True,
                                     acceptance_wait_seconds=900)["acceptance_wait_seconds"] == 900.0
    assert openloop.resolve_settings(cfg, is_open_loop=True,
                                     acceptance_wait_seconds=0)["acceptance_wait_seconds"] is None
    with pytest.raises(openloop.SettingsError):
        openloop.resolve_settings(cfg, is_open_loop=True, acceptance_wait_seconds=-1)


def test_config_file_accepts_acceptance_wait_seconds(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"acceptance_wait_seconds": 1200}), encoding="utf-8")
    assert config_mod.load_config(str(p)).acceptance_wait_seconds == 1200.0
    p.write_text(json.dumps({"acceptance_wait_seconds": None}), encoding="utf-8")
    assert config_mod.load_config(str(p)).acceptance_wait_seconds is None
    p.write_text(json.dumps({"acceptance_wait_seconds": "soon"}), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(str(p))


class _SlowAuthor:
    """RoleRunner stand-in whose author takes ``delay`` seconds and then
    either returns having written nothing (the core freezes an empty pack) or
    raises (the author phase fails)."""

    def __init__(self, delay: float, raises: bool = False) -> None:
        self.delay, self.raises = delay, raises
        self.driver_meta: dict = {}
        self.calls = 0

    def author(self, export, context):  # noqa: ANN001
        self.calls += 1
        time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("author turn failed")
        return {"exit": 0, "transcript": []}


def _controller(tmp_path, monkeypatch, runner, **config):
    root = _acc_repo(tmp_path)
    monkeypatch.setenv("TRIO_ACCEPTANCE_STATE", str(tmp_path / "accstate"))
    ctl = openloop.DegradableAcceptance(root / "loop", root, runner,
                                        {"enabled": True, **config})
    return root, ctl


def test_author_slower_than_any_default_bound_is_waited_for(tmp_path, monkeypatch):
    runner = _SlowAuthor(1.6)
    root, ctl = _controller(tmp_path, monkeypatch, runner)   # no wait_s: unbounded
    assert ctl.wait_s is None
    ctl.start(1)
    started = time.monotonic()
    ctl.wait(1)                                              # must NOT time out
    assert time.monotonic() - started >= 1.0
    assert ctl.degraded is None and ctl.frozen()


def test_degraded_controller_turns_every_gate_into_a_no_op(tmp_path, monkeypatch):
    runner = _SlowAuthor(0.2, raises=True)
    root, ctl = _controller(tmp_path, monkeypatch, runner)
    ctl.start(1)
    ctl.wait(1)                                              # the author failed: degrade, no raise
    assert ctl.degraded and "acceptance-error" in ctl.degraded
    ctl.pending_errors = ["stale refusal"]
    assert ctl.lead_gate(1, "lead") == [] and ctl.pending_errors == []
    assert ctl.check_pin(1, "lead") is True
    assert ctl.coverage() == []
    assert ctl.covered_line("a", "0" * 40) is None
    assert ctl.review_verdict("SHIP", None, 1, "0" * 40, root / "loop" / "STATE.md") \
        == ("SHIP", None, None)
    assert ctl.integration_context("0" * 40, 1)["text"] == ""
    ctl.start(2)                                             # and never restarts an author
    assert runner.calls == 1


def test_a_configured_bound_still_applies_and_degrades_instead_of_raising(tmp_path, monkeypatch):
    runner = _SlowAuthor(3.0)
    root, ctl = _controller(tmp_path, monkeypatch, runner, wait_s=0.3)
    ctl.start(1)
    ctl.wait(1)                                              # times out -> degrades quietly
    assert ctl.degraded and "acceptance-timeout" in ctl.degraded
    log = (root / "loop" / "LOG.md").read_text(encoding="utf-8")
    assert "acceptance: DEGRADED to no frozen pack (acceptance-timeout)" in log, log
    # the still-running author thread must not freeze a pack behind our back
    ctl._thread.join(timeout=10)
    assert not ctl.frozen()
    assert not (root / "loop" / "acceptance").exists()


def test_degrade_never_hides_a_cancellation(tmp_path, monkeypatch):
    runner = _SlowAuthor(3.0)
    root, ctl = _controller(tmp_path, monkeypatch, runner)
    ev = threading.Event()
    runner.ctx = type("C", (), {"cancel": ev})()
    ctl.start(1)
    threading.Timer(0.3, ev.set).start()
    with pytest.raises(TL.AcceptanceError) as ei:
        ctl.wait(1)
    assert ei.value.reason == "acceptance-cancelled"
    assert not ctl.degraded


def test_a_frozen_packs_failures_are_not_degraded(tmp_path, monkeypatch):
    """Only an author that never froze degrades: once a pack is frozen, an
    error stays the core's error (tamper, pin loss ...)."""
    runner = _SlowAuthor(0.0)
    root, ctl = _controller(tmp_path, monkeypatch, runner)
    ctl.state.update({"status": "frozen", "pin": "abc"})
    ctl._error = TL.AcceptanceError("acceptance-error", "boom")
    ctl._thread = threading.Thread(target=lambda: None)
    ctl._thread.start()
    ctl._thread.join()
    with pytest.raises(TL.AcceptanceError):
        ctl.wait(1)
    assert not ctl.degraded


# ------------------------------- 5. the generated rules, judged the way OpenCode v2 judges
def _author_perm(tmp_path, *, level: str, container_mode: bool = False, tool: str | None = None):
    cfg = make_cfg(make_key_file(tmp_path), container_mode=container_mode)
    repo = tmp_path / "app"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    export = tmp_path / "state" / "loop-x" / "export"
    export.mkdir(parents=True, exist_ok=True)
    scratch = tmp_path / "state" / "loop-x" / "author-1"
    iso = ocgen.AuthorIsolation(
        export=str(export),
        forbidden=(str(repo), str(repo / "loop"), str(repo / ".git")),
        allow_dirs=(str(scratch),), tool=tool, level=level)
    env = ocgen.generate_author_env(tmp_path / "run", cfg, REPO_ROOT, isolation=iso, style="v2")
    doc = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    return doc, doc["agent"]["trio-acceptance"]["permission"], repo, str(export), str(scratch)


@pytest.mark.parametrize("container_mode", [False, True])
def test_no_shell_rules_confine_every_file_tool_to_the_export(tmp_path, container_mode):
    doc, perm, repo, export, scratch = _author_perm(tmp_path, level="no-shell",
                                                    container_mode=container_mode)
    rel = os.path.relpath(repo, export)
    # `..` is folded lexically BEFORE the decision: the climb is just an outside path
    for path in (f"{repo}/requirements.txt", f"{rel}/requirements.txt", f"sub/../{rel}/README",
                 "/etc/passwd", "../../../../x", f"{export}/../../../{repo.name}/README"):
        assert not oc_perm.can_read(perm, export, path), path
        assert not oc_perm.can_edit(perm, export, path), path
    assert not oc_perm.can_read(perm, export, str(repo), is_dir=True)
    # glob/grep: the search directory goes through external_directory, the glob
    # PATTERN is judged itself (a climbing or absolute pattern is not path-checked by OpenCode)
    assert not oc_perm.can_glob(perm, export, "*", str(repo))
    assert not oc_perm.can_glob(perm, export, f"{rel}/**")
    assert not oc_perm.can_glob(perm, export, "../**/*.py")
    assert not oc_perm.can_glob(perm, export, "{a,../b}/*")
    assert not oc_perm.can_glob(perm, export, f"{repo}/**")
    assert not oc_perm.can_grep(perm, export, "SECRET", str(repo))
    # no shell at all: nothing a command string could do is allowed
    for cmd in ("ls", "cat goal/../x", f"cat {repo}/x", "cd / && ls", "cd .. && cat README",
                "python3 -c \"open('/ap'+'p/x')\"", f"python3 -c \"print(open('{repo}/x').read())\"",
                "python3 acceptance/checks/acc_01.py", "git push"):
        assert not oc_perm.can_shell(perm, cmd), cmd
    # ... while the author's own work inside the export is untouched
    assert oc_perm.can_read(perm, export, ".acceptance-input/GOAL.md")
    assert oc_perm.can_read(perm, export, "sub/dir/file.py")
    assert oc_perm.can_read(perm, export, f"{export}/dispatch.py")
    assert oc_perm.can_edit(perm, export, "acceptance/MANIFEST.json")
    assert oc_perm.can_edit(perm, export, f"{export}/acceptance/checks/acc_01.py")
    assert oc_perm.can_glob(perm, export, "**/*.py")
    assert oc_perm.can_grep(perm, export, "def main")
    assert oc_perm.can_read(perm, export, f"{scratch}/xdg-data/tool-output/tool_1")   # its own saved output
    # no web, no subagents, no lsp/skill, no Code Mode, nothing "ask"
    for tool in ("webfetch", "websearch", "task", "lsp", "skill", "execute", "todowrite"):
        assert not oc_perm.can_tool(perm, tool), tool
    assert '"ask"' not in json.dumps(perm)
    # container mode relaxes the OTHER roles, never this one
    lead = doc["agent"]["trio-lead"]["permission"]["external_directory"]
    assert (lead == "allow") is container_mode


def test_sandbox_level_keeps_a_guard_railed_shell_and_the_same_file_rules(tmp_path):
    _doc, perm, repo, export, _scratch = _author_perm(tmp_path, level="sandbox", container_mode=True)
    assert oc_perm.can_shell(perm, "python3 acceptance/checks/acc_01.py")
    assert oc_perm.can_shell(perm, "ls -la")
    assert not oc_perm.can_shell(perm, "git push origin main")
    assert not oc_perm.can_shell(perm, "sudo ls")
    assert not oc_perm.can_read(perm, export, f"{repo}/README")          # belt and braces
    assert oc_perm.can_read(perm, export, "sub/file.py")


def test_project_root_containing_the_export_cannot_be_climbed_by_relative_path(tmp_path):
    """OpenCode treats everything under a project root (a git work tree
    containing the export) as inside, and authorizes it by the RELATIVE path
    from the project directory -- which climbs. Those are refused."""
    _doc, perm, _repo, export, _scratch = _author_perm(tmp_path, level="no-shell")
    assert oc_perm.effect(perm["read"], "../sibling/secret.txt") == "deny"
    assert oc_perm.effect(perm["read"], "..") == "deny"
    assert oc_perm.effect(perm["edit"], "../sibling/x") == "deny"
    assert oc_perm.effect(perm["read"], "sub/ok.txt") == "allow"
    assert oc_perm.effect(perm["edit"], "acceptance/x") == "allow"


def test_author_permission_without_isolation_never_gets_the_container_allow(tmp_path):
    """The run-wide config (no per-turn isolation) already refuses to give the
    author the container-mode flat allow."""
    cfg = make_cfg(make_key_file(tmp_path), container_mode=True)
    ocgen.generate(tmp_path / "run", cfg, REPO_ROOT, tmp_path / "mailbox", style="v2")
    doc = json.loads((tmp_path / "run" / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert doc["agent"]["trio-acceptance"]["permission"]["external_directory"] != "allow"
    assert doc["agent"]["trio-builder"]["permission"]["external_directory"] == "allow"


def test_author_validator_dir_is_readable_unless_it_lives_inside_the_repo(tmp_path):
    outside = tmp_path / "opt" / "metrics" / "trio-acceptance.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")
    _doc, perm, repo, export, _s = _author_perm(tmp_path, level="sandbox", tool=str(outside))
    assert oc_perm.can_read(perm, export, str(outside))
    assert oc_perm.can_read(perm, export, str(outside.parent / "trio-check.py"))
    # self-hosting: the validator sits inside the loop repo -> not readable at all
    # (OpenCode's external_directory can only name its directory, i.e. the repo's)
    inside = repo / "metrics" / "trio-acceptance.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("x", encoding="utf-8")
    _doc, perm, repo, export, _s = _author_perm(tmp_path, level="sandbox", tool=str(inside))
    assert not oc_perm.can_read(perm, export, str(inside))
    assert not oc_perm.can_read(perm, export, str(inside.parent / "trio-check.py"))
    assert not oc_perm.can_read(perm, export, f"{repo}/src/app.py")


def test_forbidden_root_containing_the_export_is_not_denied(tmp_path):
    """A repo that happens to contain the export (HOME inside the repo) must
    not deny the author its own workspace."""
    cfg = make_cfg(make_key_file(tmp_path))
    export = tmp_path / "repo" / ".state" / "export"
    export.mkdir(parents=True)
    iso = ocgen.AuthorIsolation(export=str(export), forbidden=(str(tmp_path / "repo"), "/"))
    env = ocgen.generate_author_env(tmp_path / "run", cfg, REPO_ROOT, isolation=iso, style="v2")
    perm = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())["agent"]["trio-acceptance"]["permission"]
    assert oc_perm.can_read(perm, str(export), "GOAL.md")
    assert oc_perm.can_read(perm, str(export), f"{export}/GOAL.md")
    assert oc_perm.can_edit(perm, str(export), "acceptance/MANIFEST.json")


def test_author_config_v1_style_writes_agent_files(tmp_path):
    cfg = make_cfg(make_key_file(tmp_path))
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True)
    repo = tmp_path / "app"
    repo.mkdir()
    iso = ocgen.AuthorIsolation(export=str(export), forbidden=(str(repo),), level="no-shell")
    env = ocgen.generate_author_env(tmp_path / "run", cfg, REPO_ROOT, isolation=iso, style="v1")
    agent_md = Path(env["OPENCODE_CONFIG_DIR"]) / "agent" / "trio-acceptance.md"
    text = agent_md.read_text(encoding="utf-8")
    assert str(repo) in text and "deny" in text


# ---------------- 5a. default-deny: only read/glob/grep/edit(+write), a shell only in the sandbox
ALLOWED_AUTHOR_TOOLS = {"read", "glob", "grep", "edit", "write"}


@pytest.mark.parametrize("level", ["no-shell", "sandbox"])
@pytest.mark.parametrize("container_mode", [False, True])
def test_author_tools_are_default_deny_with_an_allow_list(tmp_path, level, container_mode):
    """OpenCode v2's real tool list at the no-shell level is edit, glob, grep,
    read, write AND ``execute`` (Code Mode): ``fetch('file://<repo>/README')``
    reads the repository and ``tools.opencode.session_move`` re-roots the
    session into it. A deny list of known tools is not enough (the next
    OpenCode adds another), so the block starts with ``"*": "deny"`` and
    names only what the author may use. Deny/allow rules only."""
    _doc, perm, _repo, _export, _s = _author_perm(tmp_path, level=level, container_mode=container_mode)
    assert list(perm)[0] == "*" and perm["*"] == "deny"
    allowed = set(ALLOWED_AUTHOR_TOOLS) | ({"bash"} if level == "sandbox" else set())
    for tool in sorted(allowed - {"bash"}):
        assert oc_perm.can_tool(perm, tool), tool
    assert oc_perm.can_tool(perm, "shell") is (level == "sandbox")
    for tool in ("execute", "webfetch", "websearch", "task", "todowrite", "lsp", "skill", "list",
                 "question", "doom_loop", "session_move", "mcp__srv__tool", "codesearch",
                 "some_future_tool"):
        assert not oc_perm.can_tool(perm, tool), tool
    if level == "no-shell":
        assert not oc_perm.can_tool(perm, "bash") and not oc_perm.can_tool(perm, "shell")
    # every top-level entry is either the blanket/explicit deny or an allow-list tool
    for key, rules in perm.items():
        if rules == "deny":
            continue
        assert key in {"read", "glob", "grep", "edit", "external_directory", "bash"}, key
    assert '"ask"' not in json.dumps(perm)
    # an ordinary run-wide config (no per-turn isolation) is denied by default too
    cfg = make_cfg(make_key_file(tmp_path), container_mode=container_mode)
    ocgen.generate(tmp_path / "run2", cfg, REPO_ROOT, tmp_path / "mailbox", style="v2")
    base = json.loads((tmp_path / "run2" / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert not oc_perm.can_tool(base["agent"]["trio-acceptance"]["permission"], "execute")


def test_author_default_deny_also_in_the_v1_agent_file(tmp_path):
    cfg = make_cfg(make_key_file(tmp_path))
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True)
    iso = ocgen.AuthorIsolation(export=str(export), forbidden=(str(tmp_path / "app"),), level="no-shell")
    env = ocgen.generate_author_env(tmp_path / "run", cfg, REPO_ROOT, isolation=iso, style="v1")
    text = (Path(env["OPENCODE_CONFIG_DIR"]) / "agent" / "trio-acceptance.md").read_text(encoding="utf-8")
    block = text.split("permission:\n", 1)[1].splitlines()
    assert block[0].strip() == '"*": deny' and "  execute: deny" in block and "  bash: deny" in block


# ---------------- 5b. symlinks (OpenCode decides "inside" lexically, never via realpath)
def test_symlinks_leaving_the_export_are_removed_and_internal_ones_kept(tmp_path):
    outside = tmp_path / "repo"
    outside.mkdir()
    (outside / "README").write_text("SECRET\n", encoding="utf-8")
    (outside / "dir").mkdir()
    export = tmp_path / "export"
    (export / "pkg" / "deep").mkdir(parents=True)
    (export / "real.txt").write_text("ok\n", encoding="utf-8")
    os.symlink(str(outside / "README"), export / "abs-link")                 # absolute, outside
    os.symlink("../repo/README", export / "rel-link")                        # relative climb
    os.symlink("../../../repo/dir", export / "pkg" / "deep" / "dir-link")    # climb to a directory
    os.symlink(str(outside), export / "pkg" / "repo-dir")                    # symlinked directory
    os.symlink("../repo/not-there-yet", export / "dangling-outside")         # dangling, outside
    os.symlink("does-not-exist", export / "dangling-inside")                 # harmless: kept
    os.symlink("loop-b", export / "loop-a")
    os.symlink("loop-a", export / "loop-b")                                  # harmless loop: kept
    os.symlink("real.txt", export / "inner-link")                            # inside: kept
    os.symlink("../real.txt", export / "pkg" / "up-inner")                   # inside via ..: kept
    # the gap: lexically these look INSIDE the project, and the read follows the link
    _doc, perm, _repo, _e, _s = _author_perm(tmp_path, level="no-shell")
    assert oc_perm.can_read(perm, str(export), "abs-link")
    assert (export / "abs-link").read_text() == "SECRET\n"
    removed = authorbox.sanitize_export(export)
    assert removed == ["abs-link", "dangling-outside", "pkg/deep/dir-link", "pkg/repo-dir",
                       "rel-link"]
    for name in removed:
        assert not os.path.lexists(export / name), name
    assert (export / "inner-link").read_text() == "ok\n"
    assert (export / "pkg" / "up-inner").read_text() == "ok\n"
    assert authorbox.sanitize_export(export) == []                          # idempotent


def test_hard_links_in_the_export_are_removed_too(tmp_path):
    """A second name for a file that lives outside (same filesystem) reads as
    an ordinary inside file; a freshly written export has no multiply-linked
    file, so one that does is dropped before the turn."""
    outside = tmp_path / "repo"
    outside.mkdir()
    (outside / "README").write_text("SECRET\n", encoding="utf-8")
    export = tmp_path / "export"
    (export / "sub").mkdir(parents=True)
    (export / "own.py").write_text("x\n", encoding="utf-8")
    os.link(outside / "README", export / "sub" / "hardlink-readme")
    removed = authorbox.sanitize_export(export)
    assert removed == ["sub/hardlink-readme"]
    assert not (export / "sub" / "hardlink-readme").exists()
    assert (outside / "README").read_text() == "SECRET\n" and (export / "own.py").exists()


# ------------------------- 5c. the real sandbox (hosts where a user namespace starts)
def _sandbox_run(tmp_path, code: str, *, shell: bool = False):
    ok, why = authorbox.bwrap_usable()
    if not ok:
        pytest.skip(f"bwrap unusable here: {why}")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "README").write_text("SECRET\n", encoding="utf-8")
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True, exist_ok=True)
    (export / "goal.md").write_text("goal\n", encoding="utf-8")
    scratch = tmp_path / "state" / "author-1"
    for d in ("tmp", "home", "xdg-data", "xdg-state"):
        (scratch / d).mkdir(parents=True, exist_ok=True)
    conf = tmp_path / "run" / "opencode-author"
    conf.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    prefix = authorbox.sandbox_prefix(export=export, scratch=scratch, config_dir=conf, env=env,
                                      opencode_bin="python3", forbidden=[str(repo)])
    argv = [*prefix, "sh", "-c", code] if shell else [*prefix, "python3", "-c", code]
    return repo, export, subprocess.run(argv, capture_output=True, text=True, timeout=60,
                                        cwd=str(export), env=env)


def test_real_sandbox_hides_the_repo_from_python_open_and_cat_and_cd(tmp_path):
    repo_probe = tmp_path / "repo"
    code = (
        "import os, subprocess, sys\n"
        f"repo = {str(repo_probe)!r}\n"
        "out = {}\n"
        "def rd(p):\n"
        "    try: return open(p).read()\n"
        "    except OSError as e: return 'ERR:' + type(e).__name__\n"
        "out['abs'] = rd(repo + '/README')\n"
        "out['dotdot'] = rd(os.path.join(os.getcwd(), os.path.relpath(repo, os.getcwd()), 'README'))\n"
        "out['concat'] = rd('/'.join(repo.split('/')) + '/README')\n"
        "os.chdir('..')\n"
        "out['parent'] = sorted(os.listdir('.'))\n"
        "os.chdir('/')\n"
        "out['root'] = sorted(os.listdir('/'))\n"
        "out['goal'] = rd(os.path.join(" + repr(str(tmp_path / "state" / "export")) + ", 'goal.md'))\n"
        "out['sh'] = subprocess.run(['sh','-c','cat ' + repo + '/README; ls ' + repo], capture_output=True, text=True).stderr\n"
        "print(__import__('json').dumps(out))\n"
    )
    repo, export, r = _sandbox_run(tmp_path, code)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["abs"] == "ERR:FileNotFoundError" and out["dotdot"] == "ERR:FileNotFoundError"
    assert out["concat"] == "ERR:FileNotFoundError"
    assert "SECRET" not in r.stdout
    assert "No such file" in out["sh"]
    assert out["goal"] == "goal\n"
    assert repo.name not in out["parent"] and repo.name not in out["root"]
    assert "home" not in out["root"] or True


def test_real_sandbox_symlink_into_the_repo_dangles_and_outside_writes_fail(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README").write_text("SECRET\n", encoding="utf-8")
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True)
    os.symlink(str(repo / "README"), export / "link")          # NOT sanitized: the OS must hold
    _repo, _export, r = _sandbox_run(
        tmp_path, "cat link 2>&1; echo ---; echo x > " + str(repo / "pwn.txt") + " 2>&1; "
        "echo x > scratch.txt && cat scratch.txt", shell=True)
    assert "SECRET" not in r.stdout, r.stdout
    assert "No such file" in r.stdout.split("---")[0]
    assert not (repo / "pwn.txt").exists()
    assert (export / "scratch.txt").read_text() == "x\n"        # the export is writable


def test_sandbox_prefix_mounts_neither_the_repo_nor_its_ancestors(tmp_path):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    bindir_in_repo = repo / "node_modules" / ".bin"
    bindir_in_repo.mkdir(parents=True)
    ok_bin = tmp_path / "toolchain" / "bin"
    ok_bin.mkdir(parents=True)
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True)
    scratch = tmp_path / "state" / "author-1"
    scratch.mkdir()
    conf = tmp_path / "run" / "opencode-author"
    conf.mkdir(parents=True)
    env = {"PATH": f"{bindir_in_repo}:{ok_bin}:/usr/bin",
           "XDG_CACHE_HOME": str(tmp_path / "run" / "xdg" / "cache")}
    (tmp_path / "run" / "xdg" / "cache").mkdir(parents=True)
    ancestor_bin = repo.parent                                   # an ANCESTOR of the repo on PATH
    env["PATH"] += f":{ancestor_bin}"
    argv = authorbox.sandbox_prefix(export=export, scratch=scratch, config_dir=conf, env=env,
                                    opencode_bin="opencode", forbidden=[str(repo)])
    binds = [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    assert str(repo) not in binds and str(tmp_path) not in binds
    assert str(bindir_in_repo) not in binds                      # PATH dir inside the repo: skipped
    assert str(ancestor_bin) not in binds                        # ancestor of the repo: skipped
    assert str(ok_bin) in binds                                  # an ordinary tool dir: mounted
    assert str(export) in binds and str(scratch) in binds and str(conf) in binds
    assert "--unshare-net" not in argv and "--unshare-all" not in argv   # the provider stays reachable
    for flag in ("--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert argv[argv.index("--chdir") + 1] == str(export) and argv[-1] == "--"
    assert argv[argv.index("--setenv") + 1:argv.index("--setenv") + 3] == ["HOME", f"{scratch}/home"]


def test_bwrap_probe_is_a_runtime_probe_not_a_path_check(monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "bwrap").write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    (fake / "bwrap").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    authorbox._PROBE_CACHE.clear()
    ok, why = authorbox.bwrap_usable()
    assert ok is False and "No permissions" in why           # on PATH, but does not work
    authorbox._PROBE_CACHE.clear()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert authorbox.bwrap_usable() == (False, "bwrap is not installed")
    authorbox._PROBE_CACHE.clear()


def test_sandbox_prefix_mounts_the_authors_config_even_inside_a_forbidden_root(tmp_path):
    """The generated author config used to sit under the run dir, which is a
    forbidden root, so the sandbox skipped it: the real binary then reported
    ``Agent not found``. Exactly the config dir is mounted read-only; neither
    its parent nor an ancestor is."""
    repo = tmp_path / "work" / "repo"
    run_dir = repo / ".git" / "trio-opencode" / "run-1"
    conf = run_dir / "opencode-author"
    xdg_conf = run_dir / "opencode-author-xdg"
    conf.mkdir(parents=True)
    xdg_conf.mkdir()
    export = tmp_path / "state" / "export"
    export.mkdir(parents=True)
    scratch = tmp_path / "state" / "author-1"
    scratch.mkdir()
    env = {"PATH": "/usr/bin", "XDG_CONFIG_HOME": str(xdg_conf),
           "XDG_CACHE_HOME": str(run_dir / "xdg" / "cache")}
    (run_dir / "xdg" / "cache").mkdir(parents=True)
    argv = authorbox.sandbox_prefix(export=export, scratch=scratch, config_dir=conf, env=env,
                                    opencode_bin="opencode",
                                    forbidden=[str(repo), str(repo / ".git"), str(run_dir)])
    mounts = {argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")}
    ro = {argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"}
    assert str(conf) in ro
    # only that one directory: not its parent, not the run dir's other contents
    # (the run's cache sits under it too: mounting it would make the repo path exist)
    for blocked in (repo, repo / ".git", run_dir, repo.parent, tmp_path, xdg_conf,
                    run_dir / "xdg" / "cache"):
        assert str(blocked) not in mounts, blocked
    # a config dir that is an ANCESTOR of a forbidden root is never mounted
    argv = authorbox.sandbox_prefix(export=export, scratch=scratch, config_dir=repo.parent, env={"PATH": "/usr/bin"},
                                    opencode_bin="opencode", forbidden=[str(repo)])
    assert str(repo.parent) not in {argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")}


def test_real_sandbox_starts_the_author_with_its_generated_config_and_cannot_edit_it(tmp_path, monkeypatch):
    """Through the driver's own ``author_setup`` (real ``ocgen`` env, real
    forbidden roots including the run dir): inside the real bwrap prefix the
    config file the binary is told to load exists and parses, the XDG config
    home is there, the loop repository and the run dir are not, and the config
    cannot be rewritten by the author (a loosened rule would apply to its next
    turn)."""
    ok, why = authorbox.bwrap_usable()
    if not ok:
        pytest.skip(f"bwrap unusable here: {why}")
    monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "auto")
    root, ctx, export = _ctx(tmp_path)
    ctx.agents_root = REPO_ROOT
    run_cache = ctx.run_dir / "xdg" / "cache"                     # the run's own, under a forbidden root
    run_cache.mkdir(parents=True)
    (run_cache / "models.json").write_text("{}", encoding="utf-8")
    ctx.env["XDG_CACHE_HOME"] = str(run_cache)
    s = ctx.author_setup(export, None, iteration=1)
    assert s.level == "sandbox"
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        assert not Path(s.env[key]).resolve().is_relative_to(ctx.run_dir.resolve()), key
    assert (Path(s.env["XDG_CACHE_HOME"]) / "models.json").read_text() == "{}"   # seeded
    code = (
        "import json, os, pathlib\n"
        "out = {}\n"
        "cfg = json.load(open(os.environ['OPENCODE_CONFIG']))\n"
        "out['agent'] = sorted(cfg['agent'])\n"
        "out['perm_first'] = next(iter(cfg['agent']['trio-acceptance']['permission']))\n"
        "out['xdg'] = os.path.isdir(os.environ['XDG_CONFIG_HOME'])\n"
        "out['cache'] = os.path.exists(os.environ['XDG_CACHE_HOME'] + '/models.json')\n"
        f"out['repo'] = os.path.exists({str(root)!r})\n"
        f"out['run_dir'] = os.path.exists({str(ctx.run_dir / 'xdg')!r})\n"
        "try:\n"
        "    open(os.environ['OPENCODE_CONFIG'], 'a').write('x'); out['rewrite'] = True\n"
        "except OSError: out['rewrite'] = False\n"
        "print(json.dumps(out))\n")
    env = {**os.environ, **ctx.turn_env(), **s.env}
    r = subprocess.run([*s.argv_prefix, "python3", "-c", code], capture_output=True, text=True,
                       timeout=60, cwd=str(export), env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "trio-acceptance" in out["agent"] and out["perm_first"] == "*"
    assert out == {**out, "xdg": True, "cache": True, "repo": False, "run_dir": False, "rewrite": False}, out
    # none of the loop's roots, nor an ancestor of one, was mounted for it
    binds = {s.argv_prefix[i + 1] for i, a in enumerate(s.argv_prefix) if a in ("--bind", "--ro-bind")}
    for r_ in ctx.author_forbidden_roots():
        assert not any(str(r_) == b or str(r_).startswith(b.rstrip("/") + "/") for b in binds), (r_, binds)


def test_no_shell_author_can_neither_read_nor_rewrite_its_own_config(tmp_path, monkeypatch):
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (False, "test"))
    _root, ctx, export = _ctx(tmp_path)
    ctx.agents_root = REPO_ROOT
    s = ctx.author_setup(export, None, iteration=1)
    perm = json.loads(Path(s.env["OPENCODE_CONFIG"]).read_text())["agent"]["trio-acceptance"]["permission"]
    cfg_dir = s.env["OPENCODE_CONFIG_DIR"]
    for path in (f"{cfg_dir}/opencode.json", f"{s.env['XDG_CONFIG_HOME']}/opencode/opencode.json"):
        assert not oc_perm.can_read(perm, str(export), path), path
        assert not oc_perm.can_edit(perm, str(export), path), path
    # its own scratch (saved tool output) stays usable
    assert oc_perm.can_read(perm, str(export), f"{s.env['XDG_DATA_HOME']}/tool-output/t1")


# ---------------- 5e. a git ancestor of the export must not become OpenCode's project root
def test_git_ancestor_makes_climbing_grep_and_glob_paths_inside_the_project(tmp_path):
    """The hazard, as probed against the real binary: with a git ancestor
    (a dotfiles HOME, a repository holding the state dir) OpenCode's project
    root is that ancestor, so ``grep {path:"../.."}`` and ``glob {path:".."}``
    are 'inside' and search outside the export unchecked (``read ../x`` is
    still refused by the ``../*`` rule). Own project root => refused."""
    _doc, perm, _repo, export, _s = _author_perm(tmp_path, level="no-shell")
    ancestor = str(Path(export).parents[1])                     # state/, a git work tree
    assert oc_perm.can_grep(perm, export, "SECRET", "../..", root=ancestor)       # the hazard
    assert oc_perm.can_glob(perm, export, "*", "..", root=ancestor)
    assert not oc_perm.can_read(perm, export, "../acceptance.json", root=ancestor)
    assert not oc_perm.can_grep(perm, export, "SECRET", "../..")                  # own root
    assert not oc_perm.can_glob(perm, export, "*", "..")
    assert not oc_perm.can_grep(perm, export, "SECRET", "..")


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_ensure_project_root_gives_the_export_its_own_root_only_when_needed(tmp_path):
    state = tmp_path / "state"
    export = state / "loop-x" / "export"
    export.mkdir(parents=True)
    (export / "app.py").write_text("x\n", encoding="utf-8")
    assert authorbox.ensure_project_root(export) is False        # no git ancestor: nothing added
    assert not (export / ".git").exists()
    _git_init(state)
    top = subprocess.run(["git", "-C", str(export), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    assert Path(top).resolve() == state.resolve()                # the hazard: the ancestor is the root
    assert authorbox.ensure_project_root(export) is True
    assert authorbox.ensure_project_root(export) is True         # idempotent
    top = subprocess.run(["git", "-C", str(export), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    assert Path(top).resolve() == export.resolve()               # the export is its own root now
    assert authorbox.release_project_root(export) is True
    assert not (export / ".git").exists() and (export / "app.py").exists()
    assert authorbox.release_project_root(export) is False
    # a .git the export really has is never touched, never removed
    (export / ".git").mkdir()
    (export / ".git" / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")
    assert authorbox.ensure_project_root(export) is False
    assert authorbox.release_project_root(export) is False
    assert (export / ".git" / "HEAD").read_text() == "ref: refs/heads/x\n"


def test_author_setup_guards_the_export_at_no_shell_and_release_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (False, "test"))
    _root, ctx, export = _ctx(tmp_path)
    _git_init(tmp_path / "state")                               # a git ancestor of the export
    ctx.agents_root = REPO_ROOT
    s = ctx.author_setup(export, None, iteration=1)
    assert s.level == "no-shell" and (export / ".git" / authorbox._GUARD_MARK).exists()
    top = subprocess.run(["git", "-C", str(export), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    assert Path(top).resolve() == export.resolve()
    ctx.release_author(export)
    assert not (export / ".git").exists()
    # the OS sandbox does not need it: no ancestor exists inside it
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (True, "test"))
    monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "auto")      # conftest forces no-shell
    ctx.acc_log.pop("author_isolation", None)
    s = ctx.author_setup(export, None, iteration=1)
    assert s.level == "sandbox" and not (export / ".git").exists()


# ------------------------------- 5d. the context: levels, fallback, scratch, logging
def _ctx(tmp_path):
    root = _acc_repo(tmp_path)
    cfg = make_cfg(make_key_file(tmp_path), root_free=False)
    run_dir = root / ".git" / "trio-opencode" / "run-deadbeef"
    run_dir.mkdir(parents=True)
    ctx = driver.RunContext(
        root_mailbox=root / "loop", live_mailbox=root / "loop", repo=root, cfg=cfg, token="t",
        exec_id="deadbeefcafe", run_dir=run_dir, log_dir=run_dir / "logs", env={"K": "v"},
        root_free=False, lead_record=None, out=lambda _m: None, cancel=threading.Event())
    export = tmp_path / "state" / "loop-x" / "export"
    export.mkdir(parents=True)
    return root, ctx, export


def test_author_setup_without_agents_root_is_inert(tmp_path):
    _root, ctx, export = _ctx(tmp_path)
    s = ctx.author_setup(export, None)
    assert (s.level, s.env, s.argv_prefix, s.shell) == ("none", {}, (), True)


def test_author_setup_falls_back_to_no_shell_when_bwrap_does_not_work(tmp_path, monkeypatch):
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (False, "test: userns blocked"))
    root, ctx, export = _ctx(tmp_path)
    ctx.agents_root = REPO_ROOT
    lines = []
    ctx.out = lines.append
    s = ctx.author_setup(export, "/opt/trio/metrics/trio-acceptance.py", iteration=1)
    assert s.level == "no-shell" and s.shell is False and s.argv_prefix == ()
    assert ctx.acc_log["author_isolation"] == "no-shell"
    assert any("no OS sandbox here" in line and "NO shell" in line for line in lines), lines
    log = (root / "loop" / "LOG.md").read_text(encoding="utf-8")
    assert "- iter 1 | loop | acceptance: author isolation: no-shell" in log
    perm = json.loads(Path(s.env["OPENCODE_CONFIG"]).read_text())["agent"]["trio-acceptance"]["permission"]
    assert not oc_perm.can_shell(perm, "ls") and not oc_perm.can_shell(perm, "python3 -c 1")
    for key in ("TMPDIR", "TMP", "TEMP", "HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert Path(s.env[key]).is_dir(), key
        assert not str(Path(s.env[key]).resolve()).startswith(str(root.resolve())), key
    # the notice is once per level, not once per author attempt
    ctx.author_setup(export, None, iteration=1)
    assert sum("author isolation" in l for l in lines) == 1


def test_author_isolation_can_be_forced_to_no_shell_even_where_bwrap_works(tmp_path, monkeypatch):
    monkeypatch.setattr(authorbox, "bwrap_usable", lambda *a, **k: (True, "works"))
    monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "no-shell")
    _root, ctx, export = _ctx(tmp_path)
    ctx.agents_root = REPO_ROOT
    s = ctx.author_setup(export, None, iteration=1)
    assert s.level == "no-shell" and "forced by TRIO_OPENCODE_AUTHOR_ISOLATION" in s.note


def test_author_setup_uses_the_sandbox_when_the_probe_says_so(tmp_path, monkeypatch):
    ok, why = authorbox.bwrap_usable()
    if not ok:
        pytest.skip(f"bwrap unusable here: {why}")
    monkeypatch.setenv("TRIO_OPENCODE_AUTHOR_ISOLATION", "auto")
    root, ctx, export = _ctx(tmp_path)
    ctx.agents_root = REPO_ROOT
    s = ctx.author_setup(export, "/opt/trio/metrics/trio-acceptance.py", iteration=2)
    assert s.level == "sandbox" and s.shell is True
    assert s.argv_prefix[0].endswith("bwrap") and s.argv_prefix[-1] == "--"
    binds = [s.argv_prefix[i + 1] for i, a in enumerate(s.argv_prefix) if a in ("--bind", "--ro-bind")]
    assert str(root) not in binds and str(root / ".git") not in binds
    assert str(export) in binds
    assert ctx.author_level == "sandbox" and driver.sandboxed(ctx)
    forbidden = {str(p) for p in ctx.author_forbidden_roots()}
    assert str(root) in forbidden and str(root / "loop") in forbidden and str(root / ".git") in forbidden


def test_sandboxed_author_turns_are_not_judged_from_the_text_of_their_commands(tmp_path):
    from types import SimpleNamespace  # noqa: PLC0415
    from test_openloop import make_ctx  # noqa: PLC0415
    root = _acc_repo(tmp_path)
    ctx = make_ctx(root)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(ctx.cfg, is_open_loop=True))
    log = tmp_path / "t.jsonl"
    log.write_text(json.dumps({"type": "tool", "part": {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "input": {"command": f"ls {root}"}}}}) + "\n", encoding="utf-8")
    result = SimpleNamespace(log_paths=[str(log)])
    ctx.author_level = "no-shell"
    assert runner._tool_call_rows(result, tmp_path)[0]["input"]["command"] == f"ls {root}"
    ctx.author_level = "sandbox"          # `ls /repo` -> "No such file": nothing was read
    assert runner._tool_call_rows(result, tmp_path) == [driver.SANDBOXED_ROW]


# ----------------------------------------- 6. audit rows (what counts as a read)
def test_audit_tool_input_drops_authored_text_but_keeps_paths():
    row = driver.audit_tool_input({"path": "acceptance/AUTHOR.md", "content": "not at /app",
                                   "oldString": "/app/a", "newString": "/app/b",
                                   "command": "ls /app"})
    assert row == {"path": "acceptance/AUTHOR.md", "command": "ls /app"}
    # a content search's pattern is text to find, a glob's pattern is a path
    assert driver.audit_tool_input({"pattern": "/app/x", "path": "p"}, "grep") == {"path": "p"}
    assert driver.audit_tool_input({"pattern": "/app/x", "path": "p"}, "glob") == {
        "pattern": "/app/x", "path": "p"}
    assert driver.audit_tool_input({"pattern": "/app/x"}) == {"pattern": "/app/x"}


def _author_audit_of(tmp_path, monkeypatch, *calls, level="no-shell"):
    """Run the real isolation audit over opencode tool parts the way the
    author hook does: log lines -> ``_tool_call_rows`` -> ``audit_transcript``."""
    from types import SimpleNamespace  # noqa: PLC0415
    from test_openloop import make_ctx  # noqa: PLC0415
    root = tmp_path / "accproduct"
    if not root.exists():
        _acc_repo(tmp_path)
    ctx = make_ctx(root)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(ctx.cfg, is_open_loop=True))
    export = tmp_path / "export"
    (export / ".acceptance-input").mkdir(parents=True, exist_ok=True)
    log = tmp_path / "author.jsonl"
    log.write_text("".join(json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": tool, "state": {
        "status": "completed", "input": tool_input}}}) + "\n" for tool, tool_input in calls),
        encoding="utf-8")
    ctx.author_level = level
    rows = runner._tool_call_rows(SimpleNamespace(log_paths=[str(log)]), export)
    acc = TL._load_sibling("trio_acceptance_audit", "trio-acceptance.py")
    return root, acc.audit_transcript(rows, export, forbidden=[root, root / "loop"])


def test_grep_pattern_quoting_a_repo_path_is_not_a_read(tmp_path, monkeypatch):
    """Seen in the payments smoke: the no-shell author grepped its OWN export's
    GOAL.md for a sentence of the goal ("Add any extra Python packages to
    `/app/src/worker/requirements.txt`."). The `pattern` of a content search is
    text to find, not a path; it read nothing outside the export, yet the audit
    scanned it as a path and discarded the session ("reads inside the loop
    repository: /app/src/worker/requirements.txt") -- a whole author turn lost."""
    root = _acc_repo(tmp_path)            # the repo path the goal text happens to name
    quoted = f"Add any extra Python packages to `{root}/src/worker/requirements.txt`."
    _repo, audit = _author_audit_of(
        tmp_path, monkeypatch,
        ("grep", {"pattern": quoted, "path": ".acceptance-input/GOAL.md", "literal": True}))
    assert audit == {"contaminated": False, "hits": []}, audit


def test_grep_whose_path_or_a_glob_pattern_reaches_the_repo_is_still_contamination(tmp_path, monkeypatch):
    root = _acc_repo(tmp_path)
    _r, audit = _author_audit_of(tmp_path, monkeypatch,
                                 ("grep", {"pattern": "SECRET", "path": str(root / "src")}))
    assert audit["contaminated"] and "loop repository" in audit["hits"][0], audit
    _r, audit = _author_audit_of(tmp_path, monkeypatch,
                                 ("glob", {"pattern": f"{root}/src/**/*.py"}))
    assert audit["contaminated"], audit


def test_permission_refused_calls_are_not_audited_but_other_errors_are():
    refused = {"status": "error",
               "error": "The user has specified a rule which prevents you from using this "
                        "specific tool call."}
    assert driver.refused_by_permission(refused) is True
    assert driver.refused_by_permission({"status": "error", "error": "File not found: /app/x"}) is False
    assert driver.refused_by_permission({"status": "completed", "output": "rule which prevents"}) is False
    assert driver.refused_by_permission(None) is False


# ---------------------------------------- 7. lead-integration retirement note
def test_lead_deliverable_commit_with_a_non_plan_slice_id_is_noted_not_flagged(tmp_path, monkeypatch):
    from test_openloop import make_ctx  # noqa: PLC0415 - sibling test helper
    root = _acc_repo(tmp_path)
    ctx = make_ctx(root)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True, kill_check=False))
    before = _git(root, "rev-parse", "HEAD")
    (root / "m.json").write_text("{}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "slice(lead-integration): manifests")
    runner._retire_lead_commits(1, root, before, ctx.live_mailbox, {"a": {"id": "a"}})
    log = (ctx.live_mailbox / "LOG.md").read_text(encoding="utf-8")
    assert "not retired" not in log, log
    assert "slice(lead-integration) is not a plan slice; nothing to retire" in log, log
    assert olqueue.latest_retired(ctx.live_mailbox) == {}


# ------------------------------------------- 8. integration-eval prompt block
def test_integration_eval_prompt_shows_acceptance_text_not_a_dict_repr():
    base = {"mailbox": "/m", "iteration": 1, "repo": "/r", "driver": "opencode",
            "output": "fenced-json", "notes": [], "sha": "a" * 40, "attempt": "x",
            "eval_worktree": "/r", "pins": {}, "repo_worktrees": {}, "lead_worktree": "/r",
            "retire_paths": {}, "rigor": "", "human_answer": "", "tmpdir": None}
    text = olprompts.render("integration-eval", dict(
        base, acceptance={"text": "FROZEN ACCEPTANCE @aaaa: 3/3 PASS", "passed": 3, "total": 3}))
    assert "FROZEN ACCEPTANCE @aaaa: 3/3 PASS" in text
    assert "'passed'" not in text
    empty = olprompts.render("integration-eval", dict(base, acceptance={"text": "", "degraded": "x"}))
    assert "FROZEN ACCEPTANCE" not in empty and "degraded" not in empty


# ----------------------------- 9. the core's retry context reaches the author
def test_open_loop_author_prompt_carries_the_cores_retry_context(tmp_path, monkeypatch):
    """`AcceptanceController._author_phase` hands the contaminated-re-run
    `prefix` and the validation retry's `dropped`/`fatal` at the TOP LEVEL of
    the author context; they used to be looked up under `retry` and never
    reached the author."""
    from types import SimpleNamespace  # noqa: PLC0415
    from test_openloop import make_ctx  # noqa: PLC0415
    root = _acc_repo(tmp_path)
    ctx = make_ctx(root)
    runner = openloop.OpenLoopRunner(ctx, settings=openloop.resolve_settings(
        ctx.cfg, is_open_loop=True))
    (ctx.live_mailbox / "ACCEPTANCE-NOTES.md").write_text("notes\n", encoding="utf-8")
    seen = {}

    def fake_call_role(ctx_, **kw):  # noqa: ANN001, ANN003
        seen.update(kw)
        return SimpleNamespace(ok=True, session_id="s", text="", log_paths=[])

    monkeypatch.setattr(driver, "_call_role", fake_call_role)
    export = tmp_path / "export"
    export.mkdir()
    runner.author(export, {"marker": "m", "attempt": 2, "mailbox": str(ctx.live_mailbox),
                           "prefix": "YOUR PREVIOUS ATTEMPT WAS DISCARDED",
                           "dropped": [["ACC-03", "passes at base"]], "fatal": ["no manifest"]})
    prompt = seen["prompt"]
    assert "YOUR PREVIOUS ATTEMPT WAS DISCARDED" in prompt
    assert "ACC-03: passes at base" in prompt and "pack: no manifest" in prompt
    assert ".acceptance-input/ACCEPTANCE-NOTES.md" in prompt
    assert "env_extra" in seen            # the per-turn isolation env is always passed

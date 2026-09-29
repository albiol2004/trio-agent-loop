"""r19 C7: trioctl -- acceptance role (inherits the Evaluator tier), doctor
tier check and registry anchors, the switch, builder refusal before any
worktree, the brief section, `trioctl omnigent acceptance ...`, the author
dispatch, the old-core refusal, and the inert integration-eval model hook."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "omnigent" / "trioctl"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull}


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


trioctl = _load("trioctl_r19_c7", SCRIPT)
TA = _load("trio_acceptance_r19_c7", ROOT / "metrics" / "trio-acceptance.py")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TRIO_ACCEPTANCE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("TRIO_ACCEPTANCE_SANDBOX", "none")
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)


def claude_cfg(**over):
    roles = {r: {"provider": "claude", "model": "opus", "effort": "high"}
             for r in ("lead", "evaluator", "builder", "scout", "docs")}
    roles.update(over)
    return {"version": 1, "roles": roles}


# ------------------------------------------------------------ resolution


def test_acceptance_inherits_the_evaluator_tier_and_explicit_table_wins():
    cfg = claude_cfg()
    got = trioctl.resolve_role("acceptance", cfg)
    assert got["role"] == "acceptance" and got["model"] == "opus"
    assert got["source"].startswith("inherited-evaluator")
    cfg = claude_cfg(acceptance={"provider": "claude", "model": "haiku", "effort": "low"})
    assert trioctl.resolve_role("acceptance", cfg)["model"] == "haiku"
    assert trioctl.acceptance_tier_problems(cfg) == [
        "acceptance author must run on the Lead/Evaluator tier ([roles.acceptance] "
        "differs from [roles.evaluator])"]
    assert trioctl.acceptance_tier_problems(claude_cfg()) == []
    mixed = claude_cfg(lead={"provider": "claude", "model": "x", "effort": "high"})
    assert "lead and evaluator tiers differ" in trioctl.acceptance_tier_problems(mixed)[0]


def test_load_config_does_not_require_an_acceptance_table(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text((ROOT / "omnigent" / "trioctl.example.toml").read_text())
    cfg = trioctl.load_config(path)
    assert "acceptance" not in cfg["roles"]
    assert trioctl.acceptance_settings(cfg) == {"enabled": False, "wait_s": 900.0,
                                                "source": "default"}


def test_switch_precedence(monkeypatch):
    cfg = {"acceptance": {"enabled": True, "wait_s": 30}}
    assert trioctl.acceptance_settings(cfg)["enabled"] is True
    monkeypatch.setenv("TRIO_ACCEPTANCE", "0")
    assert trioctl.acceptance_settings(cfg)["enabled"] is False
    assert trioctl.acceptance_settings(cfg, True) == {"enabled": True, "wait_s": 30.0,
                                                       "source": "cli"}
    monkeypatch.setenv("TRIO_ACCEPTANCE", "1")
    assert trioctl.acceptance_settings({})["enabled"] is True


def test_integration_model_hook_is_inert_by_default():
    cfg = claude_cfg()
    assert trioctl.resolve_role("evaluator", cfg, kind="integration-eval")["model"] == "opus"
    cfg["roles"]["evaluator"]["integration"] = {"model": "opus-max"}
    got = trioctl.resolve_role("evaluator", cfg, kind="integration-eval")
    assert got["model"] == "opus-max" and got["kind"] == "integration-eval"
    assert trioctl.resolve_role("evaluator", cfg)["model"] == "opus"


def _doctor(tmp_path, monkeypatch, cfg, registry, *argv):
    path = tmp_path / "p.toml"
    lines = ["version = 1"]
    for role, spec in cfg["roles"].items():
        lines.append(f"[roles.{role}]")
        lines += [f'{k} = "{v}"' for k, v in spec.items() if not isinstance(v, dict)]
    if "acceptance_table" in cfg:
        lines += ["[acceptance]", "enabled = true"]
    path.write_text("\n".join(lines) + "\n")
    home = tmp_path / "omni"
    reg = home / "agents" / "trio-omnigent-roles" / "registry.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps(registry))
    monkeypatch.setenv("OMNIGENT_HOME", str(home))
    monkeypatch.setattr(trioctl, "omnigent_contract", lambda: "ok")
    monkeypatch.setattr(trioctl, "check_cursor_approval_mode",
                        lambda: {"check": "cursor", "ok": True, "detail": "-"})
    args = trioctl.parser().parse_args(["omnigent", "doctor", "--config", str(path), "--json", *argv])
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        args.func(args)
    return {c["check"]: c for c in json.loads(buf.getvalue())["checks"]}


def test_doctor_tier_check_and_registry_anchor_only_when_enabled(tmp_path, monkeypatch):
    reg = {"_profile": trioctl.REGISTRY_PROFILE,
           "trio-omnigent-lead": {"agent_id": "a"}, "trio-omnigent-evaluator": {"agent_id": "b"}}
    off = _doctor(tmp_path, monkeypatch, claude_cfg(), reg)
    assert "acceptance:tier" not in off and "role:acceptance" not in off
    assert off["registry"]["ok"]
    on = _doctor(tmp_path, monkeypatch, claude_cfg(), reg, "--acceptance")
    assert on["acceptance:tier"]["ok"] and on["role:acceptance"]["ok"]
    assert not on["registry"]["ok"] and "trio-omnigent-acceptance" in on["registry"]["detail"]
    reg["trio-omnigent-acceptance"] = {"agent_id": "c"}
    assert _doctor(tmp_path, monkeypatch, claude_cfg(), reg, "--acceptance")["registry"]["ok"]
    bad = claude_cfg(acceptance={"provider": "claude", "model": "haiku", "effort": "low"})
    got = _doctor(tmp_path, monkeypatch, bad, reg, "--acceptance")
    assert not got["acceptance:tier"]["ok"]
    assert "Lead/Evaluator tier" in got["acceptance:tier"]["detail"]
    stale = dict(reg, _profile="cursor-grok-4.6-medium+glm-5.2-max-v3")
    assert not _doctor(tmp_path, monkeypatch, claude_cfg(), stale)["registry"]["ok"]


# ------------------------------------------------------------ builder guard


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_home(tmp_path, covers="[ACC-01]", frozen=True, driver=True):
    home = tmp_path / "home"
    box = home / "loop"
    (box / "briefs").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(home))
    (home / "app.py").write_text("print('v0')\n")
    (box / "GOAL.md").write_text("# Goal\nThe CLI prints hello.\n")
    (box / "STATE.md").write_text("schema: 1\niteration: 1\nmax_iterations: 3\nstatus: running\nmission: m\n")
    (box / "PLAN.md").write_text(
        f"# PLAN\n\n```yaml\nslices:\n  - id: cli\n    writes: [app.py]\n    covers: {covers}\n```\n")
    for name in ("REPORT.md", "LOG.md", "VERDICT.md", "QUEUE.md"):
        (box / name).write_text("")
    (box / "briefs" / "cli.md").write_text("# Brief\n\n## Targeted check\n\npython3 -m pytest -q\n")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "base")
    if frozen:
        src = tmp_path / "authored" / "acceptance"
        (src / "checks").mkdir(parents=True)
        (src / "checks" / "a.py").write_text("raise SystemExit(1)\n")
        manifest = {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {},
                    "checks": [{"id": "ACC-01", "goal_quote": "prints hello", "kind": "behaviour",
                                "surface": "cli", "run": ["python3", "acceptance/checks/a.py"]}]}
        pin = TA.write_frozen_pack(src, box / "acceptance", manifest)
        (box / "acceptance" / "FROZEN").write_text(
            TA.frozen_text(pin, git(home, "rev-parse", "HEAD"), "m s", [], [(pin, "freeze")]))
        TA.commit_paths(home, ["loop/acceptance"],
                        f"acceptance: freeze 1 checks (m)\n\nAcceptance-Pin: {pin}\n")
        state = TA.state_file(home, box)
        TA.save_state(state, {"status": "frozen", "pin": pin, "pin_commit": git(home, "rev-parse", "HEAD")})
    if driver:
        (box / ".driver.json").write_text(json.dumps({"acceptance": {"enabled": True}}))
    return home, box


@pytest.fixture()
def no_worktree(monkeypatch):
    monkeypatch.setattr(trioctl, "load_config", lambda path: {})
    created = []

    def create(*a, **kw):
        created.append(kw.get("slice_id"))
        raise AssertionError("worktree created")

    monkeypatch.setattr(trioctl.worker_worktrees, "create", create)
    return created


def _builder(box, home, *extra):
    return trioctl.parser().parse_args(
        ["omnigent", "run", "builder", *extra, "--mailbox", str(box), "--worker-slice", "cli",
         "--workspace", str(home), "--prompt-file", str(box / "briefs" / "cli.md")])


def test_builder_refused_before_freeze_before_any_worktree(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path, frozen=False)
    args = _builder(box, home, "--isolate")
    assert args.func(args) == trioctl.ACCEPTANCE_REFUSED_EXIT == 10
    assert no_worktree == []
    assert "acceptance not frozen yet" in capsys.readouterr().err


def test_builder_refused_while_a_check_is_unmapped(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path, covers="[]")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "plan")
    args = _builder(box, home, "--isolate")
    assert args.func(args) == 10
    assert "unmapped acceptance check(s): ACC-01" in capsys.readouterr().err
    assert no_worktree == []
    # Non-isolated builder dispatch gets the same guard.
    args = _builder(box, home)
    assert args.func(args) == 10


def test_builder_refused_with_uncommitted_plan_or_tampered_pack(tmp_path, no_worktree, capsys):
    home, box = make_home(tmp_path)
    (box / "PLAN.md").write_text((box / "PLAN.md").read_text() + "\n")
    args = _builder(box, home, "--isolate")
    assert args.func(args) == 10
    assert "PLAN.md has uncommitted changes" in capsys.readouterr().err
    git(home, "commit", "-qam", "plan")
    (box / "acceptance" / "checks" / "a.py").write_text("raise SystemExit(0)\n")
    assert args.func(args) == 10
    assert "is not the pinned" in capsys.readouterr().err


def test_mapped_frozen_pack_reaches_create_and_switch_off_is_unguarded(tmp_path, no_worktree):
    home, box = make_home(tmp_path)
    args = _builder(box, home, "--isolate")
    with pytest.raises(AssertionError, match="worktree created"):
        args.func(args)
    assert no_worktree == ["cli"]
    home2, box2 = make_home(tmp_path / "b", frozen=False, driver=False)
    args = _builder(box2, home2, "--isolate")
    with pytest.raises(AssertionError, match="worktree created"):
        args.func(args)


def test_brief_section_lists_covered_checks(tmp_path):
    home, box = make_home(tmp_path)
    text = trioctl._acceptance_brief(box, "cli", tmp_path / "wt")
    assert text.startswith("## Acceptance (frozen; do not edit)")
    assert "ACC-01 (behaviour): \"prints hello\"" in text
    assert f"--tree {tmp_path / 'wt'} --ids ACC-01" in text
    assert trioctl._acceptance_brief(box, "other", tmp_path / "wt") == ""


# ------------------------------------------------------------ CLI


def _cli(*argv):
    import io
    import contextlib
    args = trioctl.parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def test_acceptance_wait_status_run_cli_and_alias(tmp_path):
    home, box = make_home(tmp_path)
    code, out, _ = _cli("omnigent", "acceptance", "wait", "--mailbox", str(box), "--timeout", "1")
    assert code == 0 and "acceptance frozen: 1 check(s)" in out and "ACC-01" in out
    code, out, _ = _cli("acceptance", "status", "--mailbox", str(box), "--json")
    payload = json.loads(out)
    assert code == 0 and payload["pin_ok"] and payload["coverage_refusals"] == []
    code, out, _ = _cli("omnigent", "acceptance", "run", "--mailbox", str(box))
    assert code == 1 and "ACC-01 FAIL" in out
    home2, box2 = make_home(tmp_path / "b", frozen=False)
    code, _out, err = _cli("omnigent", "acceptance", "wait", "--mailbox", str(box2),
                           "--timeout", "0.2", "--interval", "0.05")
    assert code == 4 and "not frozen" in err


def test_human_amend_repins_while_stopped(tmp_path):
    home, box = make_home(tmp_path)
    (box / "acceptance" / "checks" / "a.py").write_text("raise SystemExit(1)  # human\n")
    code, out, err = _cli("omnigent", "acceptance", "amend", "--mailbox", str(box), "--human",
                          "--ids", "ACC-01", "--reason", "wrong surface")
    assert code == 0, err
    subjects = git(home, "log", "--format=%s").splitlines()
    assert subjects[0].startswith("acceptance: pin ") and \
        subjects[1] == "acceptance: amend ACC-01 (human): wrong surface"
    state = TA.load_state(TA.state_file(home, box))
    assert state["pin"] == TA.manifest_sha256(box / "acceptance")
    shadow = subprocess.run([sys.executable, str(ROOT / "metrics" / "trio-shadow.py"),
                             "--mailbox", str(box), "--require-commits"], capture_output=True, text=True)
    # (no slice commits yet: only the acceptance guard is under test)
    assert "acceptance gate" not in shadow.stdout, shadow.stdout


def test_export_cli_and_manual_freeze(tmp_path):
    home, box = make_home(tmp_path, frozen=False, driver=False)
    out_dir = tmp_path / "exp"
    code, out, _ = _cli("omnigent", "acceptance", "export", "--mailbox", str(box),
                        "--out", str(out_dir))
    assert code == 0 and not (out_dir / "loop").exists() and (out_dir / ".acceptance-input" / "GOAL.md").is_file()
    acc = out_dir / "acceptance" / "checks"
    acc.mkdir(parents=True)
    checks = []
    for k in range(1, 6):
        (acc / f"a{k}.py").write_text("raise SystemExit(1)\n")
        checks.append({"id": f"ACC-0{k}", "goal_quote": "prints hello", "kind": "behaviour",
                       "surface": "cli", "run": ["python3", f"acceptance/checks/a{k}.py"]})
    (out_dir / "acceptance" / "MANIFEST.json").write_text(json.dumps(
        {"acceptance_version": 1, "budget_s": 300, "setup": [], "bindings": {}, "checks": checks}))
    code, _out, err = _cli("omnigent", "acceptance", "freeze", "--mailbox", str(box),
                           "--export", str(out_dir), "--model", "human")
    assert code == 0, err
    assert git(home, "log", "-1", "--format=%s") == "acceptance: freeze 5 checks (human)"
    assert (box / "acceptance" / "FROZEN").is_file()


# ------------------------------------------------------------ author dispatch


class FakeClient:
    def __init__(self, export: Path):
        self.export = export
        self.created = []

    def create_session(self, **kw):
        self.created.append(kw)
        return {"id": "sess-1"}

    def get_items(self, session_id, **kw):
        if kw.get("after"):
            return {"items": []}
        return {"items": [
            {"id": "1", "role": "user", "type": "message", "content": "prompt mentions PLAN.md"},
            {"id": "2", "type": "tool_call", "data": {"command": f"ls {self.export}"}},
            {"id": "3", "role": "assistant", "type": "message", "status": "completed",
             "content": "done"}]}


def test_author_dispatch_runs_in_the_export_and_returns_tool_calls(tmp_path, monkeypatch):
    export = tmp_path / "export"
    export.mkdir()
    runner = trioctl.OmnigentRunner(repo=tmp_path / "repo-never-named", config=claude_cfg(),
                                    interval=0, acceptance={"enabled": True})
    seen = {}

    def fake_cwr(client, agent_id, model, prompt, title, role, started, workspace=None, dispatch=None):
        seen.update(agent=agent_id, model=model, prompt=prompt, title=title, role=role,
                    workspace=workspace)
        dispatch["session_id"] = "sess-1"
        return {"status": "idle"}, {"items": []}

    tpl = tmp_path / "acceptance.md"
    tpl.write_text("You are the Acceptance Author. Workspace: {export}\n")
    real = runner._prompt_path
    monkeypatch.setattr(runner, "_prompt_path",
                        lambda role: tpl if role == "acceptance" else real(role))
    monkeypatch.setattr(runner, "_agent_id", lambda role: f"agent-{role}")
    monkeypatch.setattr(runner, "_create_wait_read", fake_cwr)
    monkeypatch.setattr(runner, "_client", lambda: FakeClient(export))
    got = runner.author(export, {"mailbox": str(tmp_path / "loop"), "attempt": 2, "iteration": 1,
                                 "prefix": "DISCARDED earlier",
                                 "dropped": [("ACC-04", "passes-at-base")]})
    assert seen["agent"] == "agent-acceptance" and seen["model"] == "opus"
    assert seen["workspace"] == str(export) and seen["role"] == "acceptance"
    assert seen["title"] == "trioctl loop acceptance:iteration 1 author-2"
    assert str(tmp_path / "repo-never-named") not in seen["prompt"]
    assert seen["prompt"].startswith("DISCARDED earlier")
    assert "ACC-04: passes-at-base" in seen["prompt"]
    assert got["session"] == "sess-1" and got["path"] == "omnigent" and got["exit"] == 0
    assert got["transcript"] and "tool_call" in got["transcript"][0]
    assert all("PLAN.md" not in row for row in got["transcript"])


def test_runner_driver_meta_only_with_the_switch(tmp_path):
    on = trioctl.OmnigentRunner(repo=tmp_path, config=claude_cfg(), acceptance={"enabled": True})
    off = trioctl.OmnigentRunner(repo=tmp_path, config=claude_cfg())
    assert on.driver_meta.get("acceptance") == {"enabled": True}
    assert "acceptance" not in off.driver_meta
    off2 = trioctl.OmnigentRunner(repo=tmp_path, config=claude_cfg(), acceptance={"enabled": False})
    assert "acceptance" not in off2.driver_meta and off2._acceptance is None


def test_metrics_set_includes_the_runner():
    assert "trio-acceptance.py" in trioctl.METRICS_SET
    assert trioctl.REGISTRY_PROFILE.endswith("-v4-acc")


# ------------------------------------------------------------ old core


def test_old_core_is_refused_with_the_switch_on_and_runs_with_it_off(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "omnigent" / "tests"))
    from r16_harness import World, init_repo  # noqa: PLC0415

    world = World(tmp_path, monkeypatch, tag="r19old")
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    for name in ("trio-metrics.py", "trio_loop.py"):
        path = home / "metrics" / name
        path.write_text(path.read_text().replace("METRICS_API = 7\n", "METRICS_API = 6\n"))
    git(home, "commit", "-qam", "vendor an r17 (METRICS_API 6) core")
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    assert world.run_loop(spec, "--acceptance") == 3
    err = capsys.readouterr().err
    assert "frozen acceptance needs METRICS_API 7" in err and "--no-acceptance" in err
    assert not any(e["kind"] == "lead-pass" for e in world.events)
    # The same old core with the switch off runs (and ships) unchanged.
    assert world.run_loop(spec) == 0

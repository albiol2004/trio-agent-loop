"""r19 eval-r19c area 2: `trioctl omnigent acceptance amend --human
[--adopt]` while a loop driver runs, and in stop windows a role can cause.

- refused while a live driver holds the mailbox lock, even when STATE.md
  (role-writable) says the loop stopped;
- refused while the lock is being acquired (a pid-less `.lock`): the command
  now holds the mailbox lock itself for its whole duration;
- refused when a role inside a running loop invokes it (lock held);
- a role that deletes the lock (a fake stop window) can make the command
  run, but the running driver never honours it: its pin lives in memory,
  the edit is restored as tamper, the SHIP gate runs the pinned pack from
  git, and the driver state and the git-derived chain agree afterwards.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from metrics import trio_loop  # noqa: E402
from metrics.tests import test_r19_loop_acceptance as T  # noqa: E402

_env = T._env  # autouse: git identity, TRIO_ACCEPTANCE_STATE, sandbox none
TRIOCTL = ROOT / "omnigent" / "trioctl"
TA = trio_loop._load_sibling("ta_r19_fix3_cli", "trio-acceptance.py")
FEATS_5_8 = [f"f{k}" for k in range(5, 9)]


def _soften(mb, ks, who="human"):
    acc = mb / "acceptance"
    for k in ks:
        (acc / "checks" / f"acc_{k:02d}.py").write_text("raise SystemExit(0)\n")
        with (acc / "AMENDMENTS.md").open("a") as fh:
            fh.write(f"## ACC-{k:02d} · iter 1 · {who} · t\nchange: x\n")
    return [f"ACC-{k:02d}" for k in ks]


def _forge_amend(repo, mb):
    ids = _soften(mb, (1, 2, 3, 4))
    T.git_retry(repo, "add", "-A", "--", "loop/acceptance")
    T.git_retry(repo, "commit", "-qm", f"acceptance: amend {', '.join(ids)} (human): too strict")
    return T.git(repo, "rev-parse", "HEAD")


def _amend_cli(mb, adopt=None):
    argv = [sys.executable, str(TRIOCTL), "omnigent", "acceptance", "amend", "--mailbox",
            str(mb), "--human", "--ids", "ACC-01,ACC-02,ACC-03,ACC-04", "--reason", "strict"]
    if adopt:
        argv += ["--adopt", adopt]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _stopped(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": FEATS_5_8}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    return repo, mb


def test_amend_refused_while_a_live_driver_holds_the_lock_even_if_state_says_stopped(tmp_path):
    repo, mb = _stopped(tmp_path)
    sha = _forge_amend(repo, mb)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = mb / ".lock"
        lock.mkdir()
        (lock / "pid").write_text(f"{holder.pid}\n")
        (lock / "owner").write_text("driver-token\n")
        assert "status: needs_human" in T.state(mb)  # what a role could write
        head = T.git(repo, "rev-parse", "HEAD")
        proc = _amend_cli(mb, adopt=sha[:12])
        assert proc.returncode == 5, proc.stderr
        assert T.git(repo, "rev-parse", "HEAD") == head
        assert (lock / "pid").read_text().strip() == str(holder.pid)
    finally:
        holder.kill()
        holder.wait()


def test_amend_refused_while_the_lock_is_being_acquired(tmp_path):
    """A pid-less `.lock` is a driver between mkdir and its pid write; the
    command used to see no live driver and go ahead."""
    repo, mb = _stopped(tmp_path)
    sha = _forge_amend(repo, mb)
    (mb / ".lock").mkdir()
    head = T.git(repo, "rev-parse", "HEAD")
    proc = _amend_cli(mb, adopt=sha[:12])
    assert proc.returncode == 5, (proc.returncode, proc.stderr)
    assert T.git(repo, "rev-parse", "HEAD") == head
    assert (mb / ".lock").is_dir() and not (mb / ".lock" / "pid").exists()


def test_amend_releases_its_own_lock_and_a_driver_can_run_after(tmp_path):
    repo, mb = _stopped(tmp_path)
    sha = _forge_amend(repo, mb)
    proc = _amend_cli(mb, adopt=sha[:12])
    assert proc.returncode == 0, proc.stderr
    assert not (mb / ".lock").exists()


class RoleCallsAmend(T.Fake):
    """The integration Evaluator (a role, inside the running loop) makes the
    mailbox look stopped and runs the human command itself."""

    def __init__(self, *a, delete_lock=False, **kw):
        super().__init__(*a, **kw)
        self.delete_lock = delete_lock
        self.cli: list[subprocess.CompletedProcess] = []

    def _eval(self, iteration, context):
        if context.get("kind") != "slice-eval" and not self.cli:
            sha = _forge_amend(self.repo, self.mb)
            st = (self.mb / "STATE.md").read_text().replace("status: running",
                                                           "status: needs_human")
            (self.mb / "STATE.md").write_text(st)
            if self.delete_lock:
                shutil.rmtree(self.mb / ".lock", ignore_errors=True)
            self.cli.append(_amend_cli(self.mb, adopt=sha[:12]))
        return super()._eval(iteration, context)


def test_role_inside_a_running_loop_cannot_run_the_human_amend(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = RoleCallsAmend(repo, mb, lead_script=[{"features": FEATS_5_8}] * 6,
                          eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    assert fake.cli and fake.cli[0].returncode == 5, fake.cli[0].stderr
    assert "Acceptance-Human-Amend" not in T.git(repo, "log", "--format=%B")
    shipped = code == 0 and "status: shipped" in T.state(mb)
    assert not shipped, T.log(mb)[-2500:]


def test_role_deleting_the_lock_gets_no_adoption_the_running_driver_honours(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = RoleCallsAmend(repo, mb, delete_lock=True,
                          lead_script=[{"features": FEATS_5_8}] * 6,
                          eval_script=[{"verdict": "SHIP"}] * 6)
    code = T.run(mb, fake)
    lg = T.log(mb)
    # Same uid: once the lock is gone the command cannot tell a role from a
    # human, so it may run -- but the running driver's pin is in memory.
    assert fake.cli, lg[-2000:]
    shipped = code == 0 and "status: shipped" in T.state(mb)
    assert not shipped, lg[-2500:]
    assert "SHIP gate: 8/8 PASS" not in lg
    for k in (1, 2, 3, 4):
        assert "SystemExit(0)" not in (mb / "acceptance" / "checks" / f"acc_{k:02d}.py").read_text()
    state = TA.load_state(TA.state_file(repo, mb))
    chain = TA.derive_pin_chain(repo, "loop/acceptance", state.get("run_head"),
                                driver_commits=state.get("driver_commits"),
                                human_amends=state.get("human_amends"), verify=True)
    assert chain["pin"] == state["pin"] and not chain["pending_tamper"], chain
    assert not state.get("human_amends"), state.get("human_amends")


def _trioctl():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("trioctl_r19_fix3", str(TRIOCTL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_root_free_land_never_skips_reverification_with_acceptance_on(tmp_path, monkeypatch):
    """With frozen acceptance on, a land merge that changes product paths
    is never landed on `full_check:` alone: it re-verifies, and the SHIP
    gate then runs the frozen pack on exactly the tree that lands."""
    mod = _trioctl()
    seen: list[dict] = []
    monkeypatch.setattr(mod.root_free, "load_record", lambda wt, home, slug: {"path": "x"})
    monkeypatch.setattr(mod.root_free, "active", lambda record: True)
    monkeypatch.setattr(mod.root_free, "land",
                        lambda *a, **kw: seen.append(kw["full_checks"]) or {"status": "landed"})
    monkeypatch.setattr(mod, "_root_free_plan_facts",
                        lambda mailbox: ({"home": ["src/"]}, {"home": "make check"}))

    class Core:
        @staticmethod
        def _read_state(path):
            return {"evaluated_sha": "abc"}

        @staticmethod
        def _state_repo_pins(state):
            return {}

    rf = {"home": tmp_path, "slug": "s", "core": Core}
    args = type("A", (), {"wait_timeout": None})()
    for acceptance, want in ((False, {"home": "make check"}), (True, {})):
        hook = mod._root_free_land_hook(rf, args, acceptance=acceptance)
        assert hook(tmp_path, 1)["status"] == "landed"
        assert seen[-1] == want

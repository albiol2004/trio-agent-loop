"""r19 review repairs, CLI side (eval-r19 findings 6, 7 and the R9 check).

R5: `trioctl omnigent acceptance wait` sees a committed freeze even when the
caller's shell does not see the driver's state file. R6: `trio_loop.py run
--acceptance` / TRIO_ACCEPTANCE=1 hand the switch to the Omnigent runner
(so the Lead/Evaluator get the acceptance prompt blocks). R9: the genuine
9342a57 core runs with the switch off and is refused with it on.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
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


def _trioctl():
    loader = importlib.machinery.SourceFileLoader("trioctl_review_fixes", str(TRIOCTL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _wait(mb, env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TRIOCTL), "omnigent", "acceptance", "wait",
                           "--mailbox", str(mb), "--timeout", "2", "--interval", "0.2"],
                          capture_output=True, text=True, env=env)


def test_R5_wait_sees_a_committed_freeze_without_the_driver_state(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{}], eval_script=[{"verdict": "SHIP"}])
    assert T.run(mb, fake) == 0
    env = dict(os.environ, TRIO_ACCEPTANCE_STATE=str(tmp_path / "other-state"))
    proc = _wait(mb, env)
    assert proc.returncode == 0, proc.stderr
    assert "acceptance frozen: 8 check(s)" in proc.stdout
    pin = T.git(repo, "log", "-1", "--format=%(trailers:key=Acceptance-Pin,valueonly)",
                "--grep=^acceptance: freeze")
    assert f"pin {pin.strip()[:12]}" in proc.stdout


def test_R5b_wait_does_not_accept_an_uncommitted_frozen_file(tmp_path):
    repo, mb = T.make_repo(tmp_path)
    acc = mb / "acceptance"
    T.author_pack(tmp_path / "x")
    import shutil
    shutil.copytree(tmp_path / "x" / "acceptance", acc)
    (acc / "FROZEN").write_text("manifest_sha256: " + "0" * 64 + "\npin[0]: " + "0" * 64 + " freeze\n")
    env = dict(os.environ, TRIO_ACCEPTANCE_STATE=str(tmp_path / "other-state"))
    proc = _wait(mb, env)
    assert proc.returncode == 4, proc.stdout


class _Captured:
    kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).kwargs = kwargs


def _main(monkeypatch, tmp_path, argv, env=None):
    seen = {}
    monkeypatch.setattr(trio_loop, "_load_omnigent_runner", lambda: _Captured)

    def fake_run_loop(mailbox, max_iterations, runner, **kw):
        seen.update(kw)
        seen["runner"] = runner
        return 0
    monkeypatch.setattr(trio_loop, "run_loop", fake_run_loop)
    monkeypatch.chdir(tmp_path)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    assert trio_loop.main(["run", "--mailbox", str(tmp_path), "--max-iterations", "1",
                           "--runner", "omnigent", *argv]) == 0
    return seen


def test_R6_loop_cli_hands_the_switch_to_the_runner(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    seen = _main(monkeypatch, tmp_path, ["--acceptance"])
    assert _Captured.kwargs.get("acceptance") == {"enabled": True}
    assert seen["acceptance"] == {"enabled": True}
    seen = _main(monkeypatch, tmp_path, [], env={"TRIO_ACCEPTANCE": "1"})
    assert _Captured.kwargs.get("acceptance") == {"enabled": True}
    # Switch off: the runner is built exactly as before (no acceptance kwarg).
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    seen = _main(monkeypatch, tmp_path, [])
    assert "acceptance" not in _Captured.kwargs and "acceptance" not in seen


def test_R6b_the_real_runner_renders_acceptance_blocks_when_told(tmp_path):
    trioctl = _trioctl()
    on = trioctl.OmnigentRunner(repo=tmp_path, acceptance={"enabled": True})
    off = trioctl.OmnigentRunner(repo=tmp_path)
    assert on.__dict__.get("_acceptance") == {"enabled": True}
    assert off.__dict__.get("_acceptance") is None


def test_R9_genuine_9342a57_core_switch_off_runs_and_on_refused(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "omnigent" / "tests"))
    import test_r19_trioctl_acceptance as C  # noqa: PLC0415
    from r16_harness import World, init_repo  # noqa: PLC0415
    for k, v in C.GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TRIO_ACCEPTANCE", raising=False)
    world = World(tmp_path, monkeypatch, tag="fixold")
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    (home / "metrics" / "trio-acceptance.py").unlink(missing_ok=True)
    for name in ("trio-metrics.py", "trio_loop.py", "trio-shadow.py", "trio-check.py"):
        old = subprocess.run(["git", "-C", str(ROOT), "show", f"9342a57:metrics/{name}"],
                             capture_output=True, text=True, check=True).stdout
        (home / "metrics" / name).write_text(old)
    subprocess.run(["git", "-C", str(home), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(home), "commit", "-qm", "vendor genuine 9342a57 core"],
                   check=True)
    spec = world.add_loop(home, "loop/a", [{"id": "a-one", "write": "src/a.py"}])
    assert world.run_loop(spec, "--acceptance") == 3
    assert "frozen acceptance needs METRICS_API 7" in capsys.readouterr().err
    assert world.run_loop(spec) == 0


def test_amend_human_cli_adopts_only_named_commits(tmp_path):
    """eval-r19b finding 2: `trioctl omnigent acceptance amend --human` is the
    one explicit adoption act; a pack commit made while the loop was stopped
    is adopted only when named with --adopt."""
    import json as _json
    repo, mb = T.make_repo(tmp_path)
    fake = T.Fake(repo, mb, lead_script=[{"features": [f"f{k}" for k in range(1, 8)]}],
                  eval_script=[{"verdict": "NEEDS_HUMAN"}])
    assert T.run(mb, fake) == 5
    acc = mb / "acceptance"
    (acc / "checks" / "acc_08.py").write_text("import sys\nsys.exit(1)\n# human\n")
    with (acc / "AMENDMENTS.md").open("a") as fh:
        fh.write("## ACC-08 · iter 1 · human · t\nchange: fix\n")
    T.git(repo, "add", "-A", "--", "loop/acceptance")
    T.git(repo, "commit", "-qm", "acceptance: amend ACC-08 (human): fix")
    own = T.git(repo, "rev-parse", "HEAD")
    base = [sys.executable, str(TRIOCTL), "omnigent", "acceptance", "amend", "--mailbox",
            str(mb), "--human", "--ids", "ACC-08", "--reason", "fix the check"]
    refused = subprocess.run(base, capture_output=True, text=True)
    assert refused.returncode == 3 and "--adopt" in refused.stderr, refused.stderr
    done = subprocess.run(base + ["--adopt", own[:12]], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    ta = trio_loop._load_sibling("ta_cli_r2", "trio-acceptance.py")
    state = ta.load_state(ta.state_file(repo, mb))
    assert own in state["human_amends"] and own in state["human_adoptions"][-1]["amends"]
    assert f"Acceptance-Human-Amend: {own}" in T.git(repo, "log", "-1", "--format=%B")
    assert _json.loads(_json.dumps(state))["pin"] == ta.manifest_sha256(acc)

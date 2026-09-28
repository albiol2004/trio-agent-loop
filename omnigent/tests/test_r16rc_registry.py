"""r16-rc: one live-loop registry record for every mode, one writer.

r15.x wrote `loops/<slug>-<pid>.json` (root, mailbox, pid, pid_start,
writes_by_repo keyed by aggregate path) for root-bound loops; r16a wrote
`loops/<slug>.json` (live mailbox, Lead worktree, branch, target, writes
keyed by repo name) for root-free loops. The merge keeps ONE schema written
only by `trioctl._LoopRegistration`, read by the overlap refusal, one-shots,
`status`, `abandon`, `aggregate_for` and the dashboard; the cross-loop
`writes:` refusal applies regardless of mode; root-free dispatches never take
the root-turn lock; a root-free driver exception goes through the same
`_apply_driver_stop` mechanism as a root-bound one.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo, load, write_root_mailbox

ROOT = Path(__file__).resolve().parents[1]
UNIFIED_KEYS = {
    "schema", "slug", "mode", "root", "mailbox", "mailbox_rel", "live_mailbox",
    "lead_worktree", "branch", "target_ref", "aggregates", "pid", "pid_start",
    "started_at", "writes_by_repo",
}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16rc")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": ""})
    return home


def _registry(home: Path) -> Path:
    return home / ".git" / "trio-worktrees" / "loops"


def _holder_record(trioctl, reg) -> subprocess.Popen:
    """Rewrite *reg*'s record as a live driver owned by a sleeping process."""
    holder = subprocess.Popen(["sleep", "300"])
    time.sleep(0.05)
    reg.register()
    entry = json.loads(reg.path.read_text())
    ident = trioctl.worker_worktrees.process_identity(holder.pid)
    entry.update(pid=ident["pid"], pid_start=ident["start"])
    reg.path.write_text(json.dumps(entry))
    return holder


def test_root_free_record_is_the_unified_schema_and_every_reader_uses_it(world, tmp_path):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/uni", [{"id": "uni-one", "write": "src/uni.py"}])
    t = world.trioctl
    seen: dict = {}

    def lead_pass(w, s, runner, ctx, workspace, box, prompt, iteration):
        files = sorted(_registry(home).glob("*.json"))
        seen["files"] = [p.name for p in files]
        seen["record"] = json.loads(files[0].read_text())
        # Root-free: nothing at the root, so no root turn is held.
        seen["turns"] = runner._turn_state()["count"]
        seen["one_shot_loops"] = t._live_loops(home, root_bound_only=True)
        seen["aggregate"] = t.aggregate_for(box, "home", home)
        out = io.StringIO()
        with redirect_stdout(out):
            t.command_status(t.parser().parse_args(
                ["omnigent", "status", "--mailbox", str(spec["root_box"]), "--json"]))
        seen["status"] = json.loads(out.getvalue())
        metrics = load("rc_metrics", ROOT.parent / "metrics" / "trio-metrics.py")
        seen["dashboard"] = metrics.live_driver(spec["root_box"])
        return False

    world.hooks["lead-pass"] = lead_pass
    assert world.run_loop(spec) == 0
    record = seen["record"]
    assert seen["files"] == ["loop--uni.json"]
    assert set(record) == UNIFIED_KEYS
    assert record["schema"] == 1 and record["mode"] == "root-free"
    assert record["slug"] == "loop--uni" and record["mailbox_rel"] == "loop/uni"
    assert record["root"] == os.path.realpath(home)
    assert record["mailbox"] == str(Path(os.path.realpath(home)) / "loop/uni")
    lead = Path(record["lead_worktree"])
    assert record["live_mailbox"] == str(lead / "loop/uni")
    assert record["branch"] == "trio/loop--uni" and record["target_ref"] == "main"
    assert record["aggregates"] == {"home": str(lead)}
    assert record["writes_by_repo"] == {os.path.realpath(home): ["src/uni.py"]}
    assert record["pid"] == os.getpid()
    assert seen["turns"] == 0 and seen["one_shot_loops"] == []
    assert seen["aggregate"] == lead
    assert seen["status"]["driver"]["pid"] == os.getpid()
    assert seen["status"]["driver"]["mode"] == "root-free"
    assert seen["dashboard"]["lead_worktree"] == str(lead)
    # Removed at exit by the one writer.
    assert list(_registry(home).glob("*.json")) == []


def test_root_bound_record_has_the_same_keys_and_same_repo_identity(world, tmp_path):
    home = _home(tmp_path)
    t = world.trioctl
    box = write_root_mailbox(home, "loop/rb", [{"id": "rb-one", "write": "src/rb.py"}])
    reg = t._LoopRegistration(home, box)
    reg.register()
    try:
        record = json.loads(reg.path.read_text())
        assert reg.path.name == "loop--rb.json"
        assert set(record) == UNIFIED_KEYS
        assert record["mode"] == "root-bound" and record["lead_worktree"] is None
        assert record["live_mailbox"] == record["mailbox"] == str(box)
        assert record["aggregates"] == {"home": os.path.realpath(home)}
        # Same key a root-free loop of this repository uses.
        assert record["writes_by_repo"] == {os.path.realpath(home): ["src/rb.py"]}
        assert t._live_loops(home, root_bound_only=True)[0]["mailbox_rel"] == "loop/rb"
    finally:
        reg.unregister()
    assert not reg.file().exists()


def test_overlap_with_a_live_root_bound_loop_refuses_a_root_free_start(world, tmp_path, capsys):
    home = _home(tmp_path)
    t = world.trioctl
    other = write_root_mailbox(home, "loop/bound", [{"id": "b-one", "write": "src/shared"}])
    spec = world.add_loop(home, "loop/free", [{"id": "f-one", "write": "src/shared/x.py"}])
    holder = _holder_record(t, t._LoopRegistration(home, other))
    try:
        assert world.run_loop(spec) == t.REPO_SCOPE_REFUSED_EXIT
        err = capsys.readouterr().err
        assert "src/shared/x.py overlaps src/shared" in err and "nothing was created" in err
        # Nothing created: no Lead record, no loop branch, no root-mailbox write.
        assert world.rf.load_record(world.wt, home, spec["slug"]) is None
        assert "trio/loop--free" not in git(home, "branch", "--list", "trio/*")
        assert "refused" not in (spec["root_box"] / "LOG.md").read_text()
    finally:
        holder.kill()
        holder.wait()


def test_overlap_with_a_live_root_free_loop_is_seen_by_a_root_bound_loop(world, tmp_path):
    home = _home(tmp_path)
    t = world.trioctl
    spec = world.add_loop(home, "loop/free2", [{"id": "g-one", "write": "src/g.py"}])
    bound = write_root_mailbox(home, "loop/bound2", [{"id": "h-one", "write": "src/g.py"}])
    seen: dict = {}

    def lead_pass(w, s, runner, ctx, workspace, box, prompt, iteration):
        seen["problems"] = t._writes_overlap_problems(Path(os.path.realpath(home)), bound)
        return False

    world.hooks["lead-pass"] = lead_pass
    assert world.run_loop(spec) == 0
    assert len(seen["problems"]) == 1
    assert "loop/free2" in seen["problems"][0] and "src/g.py" in seen["problems"][0]


def test_second_driver_for_a_registered_mailbox_exits_5_without_writing(world, tmp_path):
    home = _home(tmp_path)
    t = world.trioctl
    box = write_root_mailbox(home, "loop/dup", [{"id": "d-one", "write": "src/d.py"}])
    holder = _holder_record(t, t._LoopRegistration(home, box))
    try:
        before = (_registry(home) / "loop--dup.json").read_bytes()
        runner = type("R", (), {})()
        registration, code = t._register_live_loop(
            t.parser().parse_args(["omnigent", "loop", "--mailbox", str(box)]),
            home, box, world.trioctl._load_trio_loop(home), runner,
        )
        assert registration is None and code == 5
        assert (_registry(home) / "loop--dup.json").read_bytes() == before
    finally:
        holder.kill()
        holder.wait()


def test_root_free_driver_exception_uses_the_driver_stop_mechanism(world, tmp_path, monkeypatch):
    home = _home(tmp_path)
    spec = world.add_loop(home, "loop/exc", [{"id": "e-one", "write": "src/e.py"}])

    def boom(self, mailbox, context):
        raise world.trioctl.TrioctlError("bind  exploded\nbadly")

    monkeypatch.setattr(world.trioctl.OmnigentRunner, "_integration_eval_worktree", boom)
    assert world.run_loop(spec) == 3
    record = world.rf.load_record(world.wt, home, spec["slug"])
    live = Path(record["live_mailbox"])
    state = (live / "STATE.md").read_text()
    assert "status: error" in state and "phase: driver-exception" in state
    assert "reason: driver-exception" in state  # r15.x `reason:` line
    log = (live / "LOG.md").read_text().splitlines()[-1]
    assert "| loop | error (driver-exception): TrioctlError: cannot bind" in log
    assert log.endswith("bind exploded badly")  # one line, whitespace collapsed
    assert list(_registry(home).glob("*.json")) == []

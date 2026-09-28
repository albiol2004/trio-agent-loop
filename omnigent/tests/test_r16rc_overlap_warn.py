"""eval-r16rc N4: a mid-run `writes:` overlap in ROOT-FREE mode warns.

Loop A's Lead widens its PLAN to overlap loop B's writes while both run.
B (the innocent loop) is no longer stopped at its next Lead pass: it logs a
warning naming A and the paths, flags `.driver.json` `writes_overlap`,
continues and lands; A lands too (its land merges B's change and
re-verifies). The start-time refusal (exit 2) and the root-bound exit-5
stop are unchanged (test_r16a S7-style / test_r15x_root_turn)."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from r16_harness import World, git, init_repo


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("TRIO_ALLOW_OVERLAPPING_LOOPS", raising=False)
    return World(tmp_path, monkeypatch, tag="r16rc_n4")


def _home(tmp_path):
    home = tmp_path / "home"
    init_repo(home, "main", {"README.md": "home\n", "src/__init__.py": "", "src/app.py": "v0\n"})
    return home


def _pass_without_prior(w, s, runner, box, it):
    """A later Lead pass: retire only this pass's slices (keep the earlier ones)."""
    q = box / "QUEUE.md"
    t = q.read_text()
    m = re.search(r"retired:\n(.*?)```", t, re.S)
    prior = m.group(1) if m else ""
    if prior:
        q.write_text(t.replace(m.group(0), "retired:\n```", 1))
    w.lead(s, runner, box, it)
    if prior:
        t = q.read_text()
        q.write_text(t.replace("retired:\n", "retired:\n" + prior, 1))
    return True


def test_overlap_introduced_by_the_other_loop_mid_run_warns_and_both_land(world, tmp_path):
    home = _home(tmp_path)
    a_sl = [{"id": "a-one", "write": "src/a1.py"}]
    b_sl = [{"id": "b-one", "write": "src/b1.py"}, {"id": "b-two", "write": "src/b2.py"}]
    a = world.add_loop(home, "loop/sa", a_sl)
    b = world.add_loop(home, "loop/sb", b_sl)
    overlap_added = threading.Event()
    b_done = threading.Event()
    codes: dict = {}
    passes = {"a": 0, "b": 0}
    seen: dict = {}

    def lead_hook(w, s, runner, ctx, ws, box, prompt, it):
        if s is a:
            passes["a"] += 1
            if passes["a"] > 1:
                return _pass_without_prior(w, s, runner, box, it)
            w.lead(s, runner, box, it)
            # A's Lead adds a slice writing B's b2 path, then idles until B is done.
            a_sl.append({"id": "a-two", "write": "src/b2.py", "content": "# b-two\n"})
            w.loops["a-two"] = a
            (box / "briefs" / "a-two.md").write_text(
                "# Task a-two\n\n## Targeted check\n\npython3 -m pytest -q tests\n")
            plan = (box / "PLAN.md").read_text()
            plan = re.sub(r"(slices:\n(?:.*\n)*?)```", lambda m: m.group(1) + (
                "  - id: a-two\n    writes: [src/b2.py]\n    reads: []\n"
                "    status: planned\n    accepts: [\"a-two works\"]\n```"), plan, count=1)
            (box / "PLAN.md").write_text(plan)
            git(Path(runner.repo), "add", "-A", "--", "loop/sa")
            git(Path(runner.repo), "commit", "-q", "-m", "loop: iteration 1")
            overlap_added.set()
            b_done.wait(180)
            return True
        passes["b"] += 1
        if passes["b"] == 1:
            assert overlap_added.wait(120)
            b_sl[1]["done"] = True
            try:
                w.lead(s, runner, box, it)
            finally:
                b_sl[1]["done"] = False
            git(Path(runner.repo), "add", "-A", "--", "loop/sb")
            git(Path(runner.repo), "commit", "-q", "-m", "loop: iteration 1")
            return True
        # B's second pass runs although A overlaps it now.
        seen["meta"] = dict(runner.driver_meta)
        return _pass_without_prior(w, s, runner, box, it)

    world.hooks["lead-pass"] = lead_hook

    def run_b():
        try:
            codes["b"] = world.run_loop(b)
        finally:
            b_done.set()

    tb = threading.Thread(target=run_b)
    tb.start()
    codes["a"] = world.run_loop(a)
    tb.join(240)
    assert codes == {"a": 0, "b": 0}, codes
    assert passes["b"] >= 2
    overlap = seen["meta"].get("writes_overlap")
    assert overlap and "loop/sa" in overlap[0] and "src/b2.py" in overlap[0], seen
    b_log = (b["root_box"] / "LOG.md").read_text()
    warn = [ln for ln in b_log.splitlines() if "warning: writes overlap with live loop" in ln]
    assert len(warn) == 1 and "loop/sa" in warn[0] and "src/b2.py" in warn[0], b_log
    assert "root-free: this loop continues" in warn[0]
    assert "writes-overlap" not in (b["root_box"] / "STATE.md").read_text()
    assert "status: shipped" in (b["root_box"] / "STATE.md").read_text()
    assert "status: shipped" in (a["root_box"] / "STATE.md").read_text()
    for path in ("src/b1.py", "src/b2.py", "src/a1.py"):
        assert git(home, "cat-file", "-e", f"main:{path}") == ""
    assert git(home, "show", "main:src/b2.py") == "# b-two"
    assert "trio/" not in git(home, "branch", "--list")
    driver = json.loads((b["root_box"] / ".driver.json").read_text())
    assert "stop" not in driver

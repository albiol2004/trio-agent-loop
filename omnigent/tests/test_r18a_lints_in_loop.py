"""eval-r18a N7: the verification lints run in-loop, not only from the CLI.

- After every Lead pass the driver runs trio-check's quality lints over the
  mailbox (accepts grammar, goal lines, reader-only full_check, mailbox test
  tautologies) and records them as `.driver.json` `lint` (advisory).
- Every slice-eval gets a `PRE-GATE:` block: the builder run's test flags,
  or -- for a Lead take-over, where no builder ran (the cp W1-W3 case) --
  the same L7 lint over the files the slice's `slice(<id>):` commits
  changed, read at the slice sha; plus the slice's accept-lint findings.
  Recorded under `quality` in `.driver.json`. Nothing is gated.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from r16_harness import World, git, init_repo

W1_TEST = (
    "from pathlib import Path\n\n"
    "def test_gold_only():\n"
    "    sql = Path('sql/v.sql').read_text()\n"
    "    assert 'fact_salesgp' not in sql\n"
    "    assert '4' in sql\n"
)


def _takeover_lead(w, spec, runner, ctx, workspace, mailbox, prompt, iteration):
    """A Lead that takes slice s1 over itself: no builder, one slice commit."""
    lead = Path(runner.repo)
    box = Path(mailbox)
    (lead / "sql").mkdir(exist_ok=True)
    (lead / "sql" / "v.sql").write_text("select 1 from gold_salesgp;\n")
    (lead / "tests").mkdir(exist_ok=True)
    (lead / "tests" / "test_v.py").write_text(W1_TEST)
    git(lead, "add", "sql/v.sql", "tests/test_v.py")
    git(lead, "commit", "-q", "-m", "slice(s1): take over the gold-only view")
    sha = git(lead, "rev-parse", "HEAD")
    at = git(lead, "log", "-1", "--format=%cI", sha)
    plan = (box / "PLAN.md").read_text()
    plan = re.sub(r"(  - id: s1\n(?:    .*\n)*?    status: )planned", r"\1complete", plan, count=1)
    (box / "PLAN.md").write_text(plan)
    queue = (box / "QUEUE.md").read_text()
    queue = queue.replace("retired:\n```", f"retired:\n  - slice: s1\n    sha: {sha}\n    at: {at}\n```", 1)
    (box / "QUEUE.md").write_text(queue)
    with (box / "LOG.md").open("a") as fh:
        fh.write(f"- iter {iteration} | lead | retired 1; gate: PASS @{sha}\n")
    spec["slices"][0]["done"] = True
    return True


def test_lead_takeover_gets_a_pre_gate_block_and_driver_lint(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tag="r18alintloop")
    home = tmp_path / "home"
    init_repo(home, "main", {"sql/v.sql": "select 1 from fact_salesgp;\n", "README.md": "r\n"})
    spec = world.add_loop(home, "loop/x", [{"id": "s1", "write": "sql/v.sql"}])
    world.hooks["lead-pass"] = _takeover_lead
    world.hooks["lead"] = _takeover_lead
    code = world.run_loop(spec)
    box = spec["root_box"]
    assert code == 0, (box / "LOG.md").read_text()
    ev = next(e for e in world.events if e["kind"] == "slice-eval")
    prompt = ev["prompt"]
    assert "AUTHORED-BY: lead" in prompt
    assert "PRE-GATE: advisory verification lints over the slice's commits at this sha (no builder ran)" in prompt
    assert "string presence 'fact_salesgp' on file text" in prompt, prompt[-2500:]
    assert "`'4' in ...` checks a 1-character literal" in prompt
    assert "PRE-GATE ACCEPTS" in prompt and "accept 1 's1 works': REJECT" in prompt
    driver = json.loads((box / ".driver.json").read_text())
    entry = next(v for k, v in driver["quality"].items() if k.startswith("s1@"))
    assert entry["authored_by"] == "lead"
    assert any("fact_salesgp" in f for f in entry["verification_flags"])
    assert entry["accept_lint"] and "REJECT" in entry["accept_lint"][0]
    lint = driver["lint"]
    assert lint["mode"] == "advisory" and lint["counts"].get("REJECT", 0) >= 1, lint
    assert any("slice s1 accept 1 's1 works'" in f for f in lint["findings"])


def test_builder_slice_pre_gate_keeps_builder_flags_and_adds_accepts(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, tag="r18alintbld")
    t = world.trioctl
    home = tmp_path / "home"
    init_repo(home, "main", {"sql/v.sql": "select 1 from fact_salesgp;\n", "README.md": "r\n"})
    spec = world.add_loop(home, "loop/x", [{"id": "s1", "write": "sql/v.sql"}])

    def worker(role, config, *, prompt, workspace, **kw):
        ws = Path(workspace)
        (ws / "sql" / "v.sql").write_text("select 1 from gold_salesgp;\n")
        (ws / "tests").mkdir(exist_ok=True)
        (ws / "tests" / "test_v.py").write_text(W1_TEST)
        return "done\nTARGETED_CHECK: 1 passed in 0.01s\n"

    monkeypatch.setattr(t, "run_cursor_worker", worker)
    assert world.run_loop(spec) == 0
    ev = next(e for e in world.events if e["kind"] == "slice-eval")
    assert "PRE-GATE: advisory verification lints over the builder's worktree" in ev["prompt"]
    assert "AUTHORED-BY: builder" in ev["prompt"]
    assert "string presence 'fact_salesgp' on file text" in ev["prompt"]
    assert "PRE-GATE ACCEPTS" in ev["prompt"]

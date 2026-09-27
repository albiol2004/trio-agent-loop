"""r11 F2b: the Lead commits only files it or its builders edited under a
slice's declared `writes:`; any other blocker stops the pass for a human
(LOG `blocked: uncommitted foreign changes: <files>`, STATE needs_human).

Checked in the canonical lead.md (and every generated copy), the Omnigent
open-loop Lead pass procedure and the isolated-dispatch block.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_worker_worktrees import SCRIPT, _load

ROOT = Path(__file__).resolve().parents[2]
LOG_LINE = "- iter N | lead | blocked: uncommitted foreign changes: <files>"

GENERATED_LEADS = [
    "prompts/canonical/lead.md",
    ".claude/agents/trio-lead.md",
    ".codex/agents/trio-lead.toml",
    "codex/agents/trio-lead.toml",
    "codex/skills/trio/references/prompts/lead.md",
    "kimi/skills/trio/references/prompts/lead.md",
    "omp/agents/trio-lead.md",
    "opencode/agents/trio-lead.md",
    "portable/prompts/lead.md",
]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _assert_rule(text: str) -> None:
    flat = _flat(text)
    assert "commit ONLY files you or your builders edited under a slice's declared `writes:`" in flat
    assert "do NOT commit, stash or delete them" in flat
    assert LOG_LINE in flat
    assert "`status: needs_human`" in flat


def test_canonical_and_generated_lead_prompts_carry_commit_scope() -> None:
    for rel in GENERATED_LEADS:
        _assert_rule((ROOT / rel).read_text(encoding="utf-8"))


def test_generated_prompts_are_in_sync() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "prompts" / "generate.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_open_loop_lead_pass_procedure_carries_commit_scope() -> None:
    trioctl = _load("trioctl_commit_scope_proc", SCRIPT)
    proc = trioctl._OPEN_LOOP_LEAD_PASS_PROCEDURE
    _assert_rule(proc)
    # The rule comes before step 1 so it governs every later commit.
    assert proc.index("0. Commit scope") < proc.index("1. Take `open` faults")


def test_isolated_dispatch_block_never_says_commit_foreign_files(tmp_path) -> None:
    trioctl = _load("trioctl_commit_scope_block", SCRIPT)
    runner = trioctl.OmnigentRunner(
        repo=tmp_path, config={},
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(tmp_path / "wt")},
    )
    block = _flat(runner._isolate_block(3, tmp_path / "loop"))
    assert "a dirty checkout is refused" not in block
    assert "Commit your own product edits" not in block
    assert "commit ONLY product edits you or your builders made under a slice's declared `writes:`" in block
    assert "do NOT commit, stash or delete it" in block
    assert LOG_LINE in block
    assert "`status: needs_human`" in block
    assert "never a reason to commit" in block

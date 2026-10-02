"""Shared helpers for ``test_e2e.py``'s fake-``opencode`` scenario modules.

Every scenario module runs inside the fake ``opencode`` executable's own
subprocess (a fresh Python process per turn, per ``fake_opencode.py``), never
inside pytest, so this module is imported the same way every scenario module
imports it: by inserting this file's own directory onto ``sys.path`` first
(see any scenario module's first two lines) — ``tests/`` is not a package and
the fake binary the test's PATH points at is a standalone copy of
``fake_opencode.py`` (see ``fakeoc.install_fake``), so nothing here can rely
on the pytest process's own ``sys.path``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="trio-opencode-fake", GIT_AUTHOR_EMAIL="fake@example.test",
        GIT_COMMITTER_NAME="trio-opencode-fake", GIT_COMMITTER_EMAIL="fake@example.test",
    )
    return env


def git(cwd: "str | Path", *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env=_git_env())
    if r.returncode != 0:
        raise RuntimeError(f"git {list(args)} (in {cwd}) failed: {r.stderr.strip()}")
    return r.stdout.strip()


def commit(cwd: "str | Path", message: str, paths: list[str] | None = None) -> str:
    git(cwd, "add", *(paths or ["-A"]))
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def head(cwd: "str | Path") -> str:
    return git(cwd, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# mailbox / files
# --------------------------------------------------------------------------


def mailbox_dir(ctx: Any, rel: str = "loop") -> Path:
    return Path(ctx.dir) / rel


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_log(mbox: Path, line: str) -> None:
    with open(mbox / "LOG.md", "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def render_plan(slices: list[dict]) -> str:
    """The ``PLAN.md`` ``slices:`` block ``trio-shadow.py --require-commits``
    (the commit gate) actually parses — see ``metrics/trio-metrics.py``'s
    ``parse_slices``/``find_slices_block``: a fenced ```yaml block, top-level
    ``slices:`` key, one ``- id:`` entry per slice with flow-style
    ``writes``/``reads`` lists."""
    lines = ["# Plan", "", "```yaml", "slices:"]
    for s in slices:
        lines.append(f"  - id: {s['id']}")
        writes = ", ".join(s.get("writes") or [])
        reads = ", ".join(s.get("reads") or [])
        lines.append(f"    writes: [{writes}]")
        lines.append(f"    reads: [{reads}]")
    lines.append("```")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# call-purpose detection (one role is called for several different
# purposes across a run; the prompt text is the only thing that tells them
# apart — see trio_opencode/prompts.py for the exact marker strings).
# --------------------------------------------------------------------------


def is_plan_call(ctx: Any) -> bool:
    return "PLAN CALL" in ctx.prompt


def is_integrate_call(ctx: Any) -> bool:
    return "INTEGRATE CALL" in ctx.prompt


def is_solo_call(ctx: Any) -> bool:
    return "no builder subagents for this pass" in ctx.prompt


def is_repair_call(ctx: Any) -> bool:
    return "Scoped repair" in ctx.prompt


def is_reprompt(ctx: Any) -> bool:
    return ("did not include a usable structured result" in ctx.prompt
           or "has no parseable verdict" in ctx.prompt)


_ITERATION_RE = re.compile(r"iteration (\d+)")
_SLICE_ID_RE = re.compile(r"slice `([^`]+)`")
_ATTEMPT_RE = re.compile(r"`attempt: (\S+)`")
_EVALUATED_RE = re.compile(r"`evaluated: (\S+)`")
#: The evaluator's own re-prompt (``prompts.reprompt`` via
#: ``_pin_and_evaluate``'s "VERDICT.md has no parseable verdict..." problem
#: text) states the same pin as "(attempt X, sha Y)", not the backtick form
#: the first evaluator_prompt call uses — a scenario must recognise both.
_REPROMPT_PIN_RE = re.compile(r"\(attempt (\S+), sha ([0-9a-f]+)\)")


def iteration_of(ctx: Any, mbox: Path | None = None) -> int:
    """The iteration number, from the prompt text (every first-attempt role
    prompt states it) — except a re-prompt (``prompts.reprompt``'s generic
    "did not include a usable structured result" text) never repeats it, so
    a scenario handling one passes ``mbox``: this falls back to the
    iteration already written into that mailbox's current ``VERDICT.md`` (a
    re-prompt is always about the pin/verdict of the iteration just
    written)."""
    m = _ITERATION_RE.search(ctx.prompt)
    if m:
        return int(m.group(1))
    if mbox is not None:
        try:
            text = (mbox / "VERDICT.md").read_text(encoding="utf-8")
        except OSError:
            text = ""
        m2 = _ITERATION_RE.search(text)
        if m2:
            return int(m2.group(1))
    raise AssertionError(f"no iteration number in prompt: {ctx.prompt[:200]!r}")


def builder_slice_id(ctx: Any) -> str:
    m = _SLICE_ID_RE.search(ctx.prompt)
    if not m:
        raise AssertionError(f"no slice id in builder prompt: {ctx.prompt[:200]!r}")
    return m.group(1)


def pin_attempt_and_sha(ctx: Any) -> tuple[str, str]:
    a = _ATTEMPT_RE.search(ctx.prompt)
    s = _EVALUATED_RE.search(ctx.prompt)
    if a and s:
        return a.group(1), s.group(1)
    m = _REPROMPT_PIN_RE.search(ctx.prompt)
    if m:
        return m.group(1), m.group(2)
    raise AssertionError(f"no attempt/evaluated sha in evaluator prompt: {ctx.prompt[:400]!r}")


# --------------------------------------------------------------------------
# structured-output replies
# --------------------------------------------------------------------------


def fence(obj: Any) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def reply(ctx: Any, obj: Any, note: str = "OK.") -> None:
    if note:
        ctx.text(note)
    ctx.text(fence(obj))


# --------------------------------------------------------------------------
# cross-process synchronization (overlap proof, marker-file crash tests)
# --------------------------------------------------------------------------


def state_dir(ctx: Any) -> Path:
    return Path(ctx.env["FAKE_OC_STATE"])


def marker_path(ctx: Any, name: str) -> Path:
    d = state_dir(ctx) / "markers"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def touch_marker(ctx: Any, name: str, content: str = "") -> None:
    marker_path(ctx, name).write_text(content or str(time.time()), encoding="utf-8")


def count_markers(ctx: Any, prefix: str) -> int:
    d = state_dir(ctx) / "markers"
    if not d.is_dir():
        return 0
    return len(list(d.glob(prefix + "*")))


def wait_for_marker(ctx: Any, name: str, deadline: float = 20.0, poll: float = 0.05) -> bool:
    path = marker_path(ctx, name)
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists():
            return True
        time.sleep(poll)
    return path.exists()

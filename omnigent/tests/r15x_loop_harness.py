"""r15.x test harness: one `trioctl omnigent loop` process with a fake role runner.

Run as ``python3 r15x_loop_harness.py <config.json>`` (a separate OS process,
like a real driver). The real `_command_loop` runs: registry, root-turn lock,
isolation, the vendored loop core of the fixture repo, real git. Only the
broker side is faked (`_run_dispatch`, client, prune): the scripted Lead
commits one product file per slice and retires it, slice-evals SHIP their
section, the integration-eval SHIPs and makes the mailbox retirement commit.
Every role turn is appended to the shared timeline file as JSON lines.

config keys: trioctl, worktrees, home, mailbox, timeline, lead_sleep,
eval_sleep, integration ("ship" | "raise"), retire_delay (seconds the
retirement commit lags the SHIP verdict), args (extra loop argv).
"""
from __future__ import annotations

import functools
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _load(name: str, path: str):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> int:
    cfg = json.loads(Path(sys.argv[1]).read_text())
    home = Path(cfg["home"])
    box = Path(cfg["mailbox"])
    timeline = Path(cfg["timeline"])
    name = box.name

    def event(kind: str, **fields) -> None:
        line = json.dumps({"loop": name, "event": kind, "t": time.time(), **fields})
        with open(timeline, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    wt = _load("worker_worktrees", cfg["worktrees"])
    trioctl = _load("trioctl", cfg["trioctl"])
    trioctl.worker_worktrees = wt
    trioctl.load_config = lambda path: {}
    trioctl._prune_broker_sessions = (
        lambda client, mailbox, **kw: {"deleted": len(kw.get("session_ids") or [])}
    )
    trioctl._run_post_loop_session_prune = lambda *a, **k: []

    class Client:
        pass

    Runner = trioctl.OmnigentRunner
    Runner._client = lambda self: Client()
    Runner._agent_id = lambda self, role: "agent"
    Runner._resolve_model = lambda self, role: "m"
    Runner._new_dispatch_nonce = lambda self: None
    Runner._prompt = lambda self, *a, **k: "prompt\n"

    def lead(iteration: int) -> None:
        event("lead-start", iteration=iteration)
        time.sleep(float(cfg.get("lead_sleep", 1.0)))
        plan = (box / "PLAN.md").read_text()
        entries, gate = [], None
        for sid, write in cfg["slices"]:
            planned = f"  - id: {sid}\n    writes: [{write}]\n    reads: []\n    status: planned\n"
            if planned not in plan:
                continue
            target = home / write
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {sid}\n")
            git(home, "add", "--", write)
            git(home, "commit", "-q", "-m", f"slice({sid}): {sid} work")
            sha = git(home, "rev-parse", "HEAD")
            event("lead-commit", sha=sha)
            at = git(home, "log", "-1", "--format=%cI", sha)
            entries.append(f"  - slice: {sid}\n    sha: {sha}\n    at: {at}\n")
            plan = plan.replace(planned, planned.replace("status: planned", "status: complete"))
            gate = sha
        (box / "PLAN.md").write_text(plan)
        queue = (box / "QUEUE.md").read_text()
        (box / "QUEUE.md").write_text(
            queue.replace("retired:\n```", "retired:\n" + "".join(entries) + "```", 1)
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | retired {len(entries)} slice(s); "
                     f"gate: PASS @{gate}\n")
        event("lead-end", iteration=iteration)

    def slice_eval(ctx: dict) -> None:
        time.sleep(float(cfg.get("eval_sleep", 0.2)))
        with (box / "VERDICT.md").open("a") as fh:
            fh.write(f"\n## slice {ctx['slice']} @{ctx['sha']} — SHIP\n\naccepts: PASS\n")

    def integration(ctx: dict) -> None:
        event("integration-start", pin=ctx["pinned_sha"])
        if cfg.get("integration") == "raise":
            raise RuntimeError("integration-eval dispatch blew up (injected)")
        time.sleep(float(cfg.get("eval_sleep", 0.2)))
        text = (box / "VERDICT.md").read_text()
        (box / "VERDICT.md").write_text(
            "VERDICT: SHIP\n\n"
            f"iteration: {ctx['iteration']}\nattempt: {ctx['evaluator_attempt']}\n"
            f"evaluated: {ctx['pinned_sha']}\n\n" + text
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {ctx['iteration']} | evaluator | VERDICT: SHIP\n")
        rel = box.relative_to(home).as_posix()

        def retire() -> None:
            time.sleep(float(cfg.get("retire_delay", 0.0)))
            git(home, "add", "-f", "--", f"{rel}/VERDICT.md")
            git(home, "commit", "-q", "-m", f"loop: iteration {ctx['iteration']} — SHIP")
            event("retired", sha=git(home, "rev-parse", "HEAD"))

        if cfg.get("retire_delay"):
            # The SHIP lands before the retirement commit (the live race the
            # core's bounded retirement wait covers).
            threading.Thread(target=retire, daemon=True).start()
        else:
            retire()
        event("integration-end")

    def run_dispatch(self, client, agent_id, model, prompt, title, role, iteration,
                     mailbox, ctx, started, before_text, before_mtime, dispatch, workspace):
        ctx = ctx or {}
        kind = ctx.get("kind") or role
        dispatch["session_id"] = f"sess-{name}-{role}-{iteration}-{ctx.get('slice') or kind}"
        if role == "lead":
            lead(iteration)
        elif kind == "slice-eval":
            slice_eval(ctx)
        elif kind == "integration-eval":
            integration(ctx)
        return 0

    Runner._run_dispatch = run_dispatch

    real_load = trioctl._load_trio_loop

    def load_core(repo):
        core = real_load(repo)
        real_run = core.run_loop

        @functools.wraps(real_run)  # keeps the signature trioctl inspects
        def run_loop(*a, **k):
            try:
                return real_run(*a, **k)
            finally:
                event("run-loop-returned")

        core.run_loop = run_loop
        return core

    trioctl._load_trio_loop = load_core
    argv = ["omnigent", "loop", "--mailbox", str(box), "--max-iterations", "5",
            *cfg.get("args", [])]
    args = trioctl.parser().parse_args(argv)
    os.chdir(home)
    event("start")
    code = trioctl.command_loop(args)
    event("exit", code=code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

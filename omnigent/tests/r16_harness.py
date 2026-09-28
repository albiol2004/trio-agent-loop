"""Shared harness for the r16a root-free open-loop tests.

Fake role runner (no broker, no Cursor), real git, real loop core, real
`trioctl omnigent loop` entry (`_command_loop`): the Lead is scripted and
dispatches every slice through the real `trioctl omnigent run builder
--isolate` (worktree, commit and merge are real; the Cursor worker writes the
slice's file); slice-evals append their section; the integration-eval writes
a bound SHIP and makes the retirement commit in the Lead worktree with
`git -C <lead>`, exactly as the ROOT-FREE prompt tells it to.
"""
from __future__ import annotations

import contextlib
import functools
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SCRIPT = ROOT / "trioctl"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}
CHECK_LINE = "1 passed in 0.01s"
METRICS_FILES = ("trio_loop.py", "trio-metrics.py", "trio-shadow.py", "trio-check.py")


def load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ, **GIT_ENV)
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def init_repo(path: Path, branch: str, files: dict[str, str], *, metrics: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    if metrics:
        (path / "metrics").mkdir(exist_ok=True)
        for name in METRICS_FILES:
            shutil.copy2(REPO_ROOT / "metrics" / name, path / "metrics" / name)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")


BRIEF = (
    "# Task {sid}\n\nImplement {sid}.\n\n## Targeted check\n\n"
    "python3 -m pytest -q tests\n\n"
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n"
)


def plan_text(slices: list[dict], full_check: str = "full_check: true", repos_block: str = "") -> str:
    rows = []
    for sl in slices:
        rows.append(f"  - id: {sl['id']}\n")
        if sl.get("repo", "home") != "home":
            rows.append(f"    repo: {sl['repo']}\n")
        reads = ", ".join(sl.get("reads") or [])
        rows.append(f"    writes: [{sl['write']}]\n    reads: [{reads}]\n")
        rows.append(f"    status: {sl.get('status', 'planned')}\n")
        rows.append(f'    accepts: ["{sl["id"]} works"]\n')
    head = f"```yaml\nrepos:\n{repos_block}```\n\n" if repos_block else ""
    return (
        "# PLAN\n\n" + head + "## Verification standard\n\nmode: test-first\n\n"
        + full_check + "\n\n```yaml\nslices:\n" + "".join(rows) + "```\n"
    )


def write_root_mailbox(home: Path, rel: str, slices: list[dict], **plan_kw: Any) -> Path:
    """The files the main session writes at the root (left untracked)."""
    box = home / rel
    (box / "briefs").mkdir(parents=True, exist_ok=True)
    for sl in slices:
        (box / "briefs" / f"{sl['id']}.md").write_text(BRIEF.format(sid=sl["id"]))
    (box / "GOAL.md").write_text(f"# Goal\n\n{rel}\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 0\nmax_iterations: 5\nstatus: ready\nmission: r16\n"
    )
    (box / "PLAN.md").write_text(plan_text(slices, **plan_kw))
    (box / "QUEUE.md").write_text(
        "# Queue\n\n```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n"
    )
    (box / "REPORT.md").write_text("# Report\n")
    (box / "VERDICT.md").write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    return box


class FakeClient:
    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def create_session(self, *a, **kw):
        with self.lock:
            self.n += 1
            return {"id": f"s{self.n}"}

    def wait_session(self, *a, **kw):
        return {"status": "idle"}

    def get_items(self, *a, **kw):
        return []


class World:
    """Patched trioctl + scripted roles shared by every loop of one test."""

    _stdout_lock = threading.Lock()

    def __init__(self, tmp_path: Path, monkeypatch, *, tag: str = "r16"):
        self.tmp = tmp_path
        home_dir = tmp_path / "userhome"
        home_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)
        for key, value in GIT_ENV.items():
            monkeypatch.setenv(key, value)
        # A root-free driver turns bytecode writing off process-wide.
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
        self.wt = load(f"worker_worktrees_{tag}", ROOT / "worker_worktrees.py")
        self.rf = load(f"root_free_{tag}", ROOT / "root_free.py")
        self.trioctl = load(f"trioctl_{tag}", SCRIPT)
        t = self.trioctl
        monkeypatch.setattr(t, "worker_worktrees", self.wt)
        monkeypatch.setattr(t, "root_free", self.rf)
        monkeypatch.setattr(t, "load_config", lambda path: {})
        monkeypatch.setattr(t, "_prune_broker_sessions",
                            lambda client, mailbox, **kw: {"deleted": len(kw.get("session_ids") or []),
                                                           "archived": 0, "skipped_running": 0,
                                                           "failed": 0})
        monkeypatch.setattr(t, "_session_client", lambda base_url=None: FakeClient())
        monkeypatch.setattr(t.OmnigentRunner, "_agent_id", lambda self, role: "agent")
        monkeypatch.setattr(t.OmnigentRunner, "_resolve_model", lambda self, role: "m")
        monkeypatch.setattr(t.OmnigentRunner, "_new_dispatch_nonce", lambda self: None)
        monkeypatch.setattr(t, "run_cursor_worker", self._worker)
        real_signal = t.signal.signal
        monkeypatch.setattr(
            t.signal, "signal",
            lambda sig, handler: real_signal(sig, handler)
            if threading.current_thread() is threading.main_thread() else None,
        )
        monkeypatch.setattr(
            t.OmnigentRunner, "_run_dispatch",
            lambda runner, *a: self._run_dispatch(runner, *a),
        )
        real_load = t._load_trio_loop

        def fast_core(repo):
            # `trioctl omnigent loop` polls every 30 s between turns; tests
            # poll every 10 ms (same state machine).
            core = real_load(repo)
            real_run = core.run_loop

            @functools.wraps(real_run)
            def run_loop(*a, **kw):
                return real_run(*a, **{"poll_seconds": 0.01, **kw})

            core.run_loop = run_loop
            return core

        monkeypatch.setattr(t, "_load_trio_loop", fast_core)
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.loops: dict[str, dict] = {}  # slice id -> loop spec
        #: hooks: {kind: callable(world, spec, ctx, workspace, prompt)}; a hook
        #: returning True replaces the default behaviour.
        self.hooks: dict[str, Callable[..., Any]] = {}

    # ---------------------------------------------------------- the roles
    def _worker(self, role, config, *, prompt, workspace, timeout=None, events=None,
                isolated=False, on_spawn=None, wrap=None):
        sid = re.search(r"# Task (\S+)", prompt).group(1)
        spec = self.loops[sid]
        sl = next(s for s in spec["slices"] if s["id"] == sid)
        target = Path(workspace) / sl["write"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sl.get("content", f"# {sid}\n"))
        return f"implemented {sid}\nTARGETED_CHECK: {CHECK_LINE}\n"

    def _run_dispatch(self, runner, client, agent_id, model, prompt, title, role, iteration,
                      mailbox, ctx, started, before_text, before_mtime, dispatch, workspace):
        ctx = ctx or {}
        kind = ctx.get("kind") or role
        spec = next(s for s in self.loops.values() if s["rel"] in str(mailbox))
        with self.lock:
            self.events.append({
                "loop": spec["rel"], "kind": kind, "workspace": str(workspace),
                "mailbox": str(mailbox), "prompt": prompt, "t": time.monotonic(),
                "ctx": dict(ctx),
            })
        hook = self.hooks.get(kind)
        if not (hook and hook(self, spec, runner, ctx, Path(workspace), Path(mailbox), prompt,
                              iteration)):
            if role == "lead":
                self.lead(spec, runner, Path(mailbox), iteration)
            elif kind == "slice-eval":
                self.slice_eval(ctx, Path(mailbox))
            elif kind == "integration-eval":
                self.integration(spec, runner, ctx, Path(workspace), Path(mailbox))
        dispatch["session_id"] = f"sess-{spec['slug']}-{role}-{iteration}-{ctx.get('slice') or kind}"
        return 0

    def lead(self, spec: dict, runner, box: Path, iteration: int) -> None:
        t = self.trioctl
        lead = Path(runner.repo)
        plan = (box / "PLAN.md").read_text()
        queue = (box / "QUEUE.md").read_text()
        entries = []
        gates: dict[str, str] = {}
        for sl in spec["slices"]:
            if f"  - id: {sl['id']}\n" not in plan or sl.get("done"):
                continue
            args = t.parser().parse_args([
                "omnigent", "run", "builder", "--isolate", "--mailbox", str(box),
                "--worker-slice", sl["id"], "--summary", f"{sl['id']} work",
                "--worktree-root", str(runner._isolate["worktree_root"]),
                "--workspace", str(lead),
                "--prompt-file", str(box / "briefs" / f"{sl['id']}.md"),
            ])
            out = io.StringIO()
            # redirect_stdout is process-wide: concurrent loops' dispatches
            # take turns (their Lead passes still overlap).
            with World._stdout_lock, contextlib.redirect_stdout(out):
                code = t.command_run(args)
            assert code == 0, out.getvalue()
            view = json.loads([ln for ln in out.getvalue().splitlines()
                               if '"worker_worktree"' in ln][-1])["worker_worktree"]
            sha = view["merge_commit"]
            repo_path = Path(view.get("repo") or lead)
            at = git(repo_path, "log", "-1", "--format=%cI", sha)
            repo_line = f"    repo: {sl['repo']}\n" if sl.get("repo", "home") != "home" else ""
            entries.append(f"  - slice: {sl['id']}\n{repo_line}    sha: {sha}\n    at: {at}\n")
            gates[sl.get("repo", "home")] = git(repo_path, "rev-parse", "HEAD")
            plan = plan.replace(f"  - id: {sl['id']}\n", f"  - id: {sl['id']}\n", 1)
            plan = re.sub(
                rf"(  - id: {re.escape(sl['id'])}\n(?:    .*\n)*?    status: )planned",
                r"\1complete", plan, count=1,
            )
            sl["done"] = True
        (box / "PLAN.md").write_text(plan)
        queue = queue.replace("retired:\n```", "retired:\n" + "".join(entries) + "```", 1)
        (box / "QUEUE.md").write_text(queue)
        gates.setdefault("home", git(lead, "rev-parse", "HEAD"))
        gate = "; ".join(
            f"gate: PASS @{sha}" if name == "home" else f"gate: PASS @{name}:{sha}"
            for name, sha in gates.items()
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | retired {len(entries)}; {gate}\n")

    def slice_eval(self, ctx: dict, box: Path) -> None:
        with self.lock:
            with (box / "VERDICT.md").open("a") as fh:
                fh.write(f"\n## slice {ctx['slice']} @{ctx['sha']} — SHIP\n\naccepts: PASS\n")

    def integration(self, spec: dict, runner, ctx: dict, workspace: Path, box: Path,
                    verdict: str = "SHIP") -> None:
        lead = Path(runner._root_free["lead"])
        assert git(workspace, "rev-parse", "HEAD") == ctx["pinned_sha"]
        pins = dict(ctx.get("pins") or {})
        evaluated = ctx["pinned_sha"]
        commits = ""
        if pins:
            declared = self.trioctl._declared_repos(box)
            evaluated = ", ".join(
                f"{n}@{pins[n]}" for n in ["home", *[n for n in pins if n != "home"]]
            )
            for name in [n for n in pins if n != "home"]:
                agg = Path(declared[name]["path"])
                if verdict == "SHIP":
                    git(agg, "commit", "-q", "--allow-empty", "-m",
                        f"loop: iteration {ctx['iteration']} — SHIP ({box.name})")
                    commits += f"commit: {name}@{git(agg, 'rev-parse', 'HEAD')}\n"
        text = (box / "VERDICT.md").read_text()
        (box / "VERDICT.md").write_text(
            f"VERDICT: {verdict}\n\niteration: {ctx['iteration']}\n"
            f"attempt: {ctx['evaluator_attempt']}\nevaluated: {evaluated}\n{commits}\n" + text
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {ctx['iteration']} | evaluator | VERDICT: {verdict} — r16\n")
        if verdict != "SHIP":
            return
        rel = box.relative_to(lead).as_posix()
        git(lead, "add", "-f", "--", f"{rel}/VERDICT.md", f"{rel}/LOG.md")
        git(lead, "add", "-u", "--", rel)
        git(lead, "commit", "-q", "-m", f"loop: iteration {ctx['iteration']} — SHIP", "--", rel)

    # ---------------------------------------------------------- driving
    def add_loop(self, home: Path, rel: str, slices: list[dict], **plan_kw: Any) -> dict:
        box = write_root_mailbox(home, rel, slices, **plan_kw)
        spec = {"rel": rel, "slug": self.rf.loop_slug(rel), "slices": slices, "home": home,
                "root_box": box}
        for sl in slices:
            self.loops[sl["id"]] = spec
        return spec

    def loop_args(self, spec: dict, *extra: str, command: str = "loop"):
        # An absolute --mailbox: root-free runs derive the repository from
        # the mailbox path (never from the process cwd), so concurrent loops
        # in threads need no chdir.
        argv = ["omnigent", command, "--mailbox", str(spec["root_box"])]
        if command in ("loop", "land"):
            argv += ["--max-iterations", "5", "--wait-timeout", "60"]
        return self.trioctl.parser().parse_args([*argv, *extra])

    def run_loop(self, spec: dict, *extra: str) -> int:
        """`trioctl omnigent loop --mailbox <root mailbox>` for *spec*."""
        return self.trioctl._command_loop(self.loop_args(spec, *extra))

    def run_land(self, spec: dict, *extra: str) -> int:
        return self.trioctl.command_land(self.loop_args(spec, *extra, command="land"))


def snapshot_root(home: Path) -> dict:
    """R1: the root's porcelain status, index mtime and .cursor bytes."""
    index = home / ".git" / "index"
    cursor = {}
    for rel in ("mcp.json", "hooks.json"):
        p = home / ".cursor" / rel
        cursor[rel] = p.read_bytes() if p.exists() else None
    status = git(home, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all")
    # eval-r16rc-b M1: the driver's own root mailbox `.lock/{owner,pid}` (the
    # loop core's lock, held for the whole run) is the one sanctioned root
    # mark; real mailboxes ignore it (`.gitignore` `.lock/`), these fixtures
    # leave the mailbox untracked and unignored.
    status = "\n".join(
        line for line in status.splitlines()
        if not re.match(r"^\?\? .*/\.lock/(pid|owner)$", line)
    )
    return {
        "status": status,
        "index_mtime": index.stat().st_mtime_ns if index.exists() else None,
        "head": git(home, "rev-parse", "HEAD"),
        "cursor": cursor,
    }

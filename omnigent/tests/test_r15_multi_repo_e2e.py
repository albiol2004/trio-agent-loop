"""r15 multi-repo slices end to end: fake role runner, real git, real loop core.

Fixture B: a home repo (tracked `.cursor/mcp.json`) whose mailbox holds two
gitignored nested clones (`app-backend`, `app-frontend`), declared in PLAN.md
`repos:`; three slices (one per repo). Fixture C: a home repo plus one
product repo elsewhere on disk (absolute `path:`, non-default `base:`).

The Lead is scripted: it dispatches every slice through the real
`trioctl omnigent run builder --isolate` (the Cursor worker is faked; the
worktree, commit and merge are real), retires each slice with `repo:`, and
logs one gate per repo. Slice-evals run through the real OmnigentRunner
binding (worktree from the slice's repo) concurrently; the integration eval
checks the per-repo pins, makes one retirement commit per declared repo and
the home mailbox commit. The driver must accept the SHIP, and post-SHIP
cleanup must remove every worktree from every repo's ledger.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SCRIPT = ROOT / "trioctl"
MODULE = ROOT / "worker_worktrees.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Trio Test",
    "GIT_AUTHOR_EMAIL": "trio@example.invalid",
    "GIT_COMMITTER_NAME": "Trio Test",
    "GIT_COMMITTER_EMAIL": "trio@example.invalid",
}
CHECK_LINE = "1 passed in 0.01s"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _init_repo(path: Path, branch: str, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")


BRIEF = (
    "# Task {sid}\n\nImplement {sid}.\n\n## Targeted check\n\n"
    "python3 -m pytest -q tests\n\n"
    "Print `TARGETED_CHECK: <the line stating the pass/fail counts>`.\n"
)


def _plan(repos_block: str, slices: list[dict], full_check: str) -> str:
    rows = []
    for sl in slices:
        rows.append(f"  - id: {sl['id']}\n")
        if sl["repo"] != "home":
            rows.append(f"    repo: {sl['repo']}\n")
        rows.append(f"    writes: [{sl['write']}]\n    reads: []\n")
        rows.append(f"    status: {sl.get('status', 'planned')}\n")
        rows.append(f'    accepts: ["{sl["id"]} works"]\n')
    return (
        "# PLAN\n\n```yaml\nrepos:\n" + repos_block + "```\n\n"
        "## Verification standard\n\nmode: test-first\n\n" + full_check + "\n\n"
        "```yaml\nslices:\n" + "".join(rows) + "```\n"
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    h = tmp_path / "userhome"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    wt = _load("worker_worktrees_r15e2e", MODULE)
    trioctl = _load("trioctl_r15e2e", SCRIPT)
    monkeypatch.setattr(trioctl, "worker_worktrees", wt)
    monkeypatch.setattr(trioctl, "load_config", lambda path: {})
    core = _load("trio_loop_r15e2e", REPO_ROOT / "metrics" / "trio_loop.py")
    return tmp_path, wt, trioctl, core


def _layout(tmp_path: Path, kind: str) -> dict:
    home = tmp_path / "home"
    box = home / "loop"
    files = {
        ".cursor/mcp.json": json.dumps({"mcpServers": {}}) + "\n",
        "README.md": "home\n",
        "docs/index.md": "docs\n",
    }
    if kind == "B":
        files[".gitignore"] = "loop/app-backend/\nloop/app-frontend/\n"
    _init_repo(home, "main", files)
    repos: dict[str, dict] = {}
    if kind == "B":
        _init_repo(box / "app-backend", "dev", {"app/core.py": "x = 1\n"})
        _init_repo(box / "app-frontend", "feat/ui", {"src/app.js": "//\n"})
        repos = {
            "app-backend": {"path": box / "app-backend", "raw": "loop/app-backend", "base": "dev"},
            "app-frontend": {"path": box / "app-frontend", "raw": "loop/app-frontend", "base": "feat/ui"},
        }
        slices = [
            {"id": "be-a", "repo": "app-backend", "write": "app/a.py"},
            {"id": "fe-b", "repo": "app-frontend", "write": "src/b.js"},
            {"id": "home-c", "repo": "home", "write": "docs/c.md"},
        ]
        full_check = (
            "full_check:\n  app-backend: python3 -m pytest -q\n"
            "  app-frontend: npx vitest run\n  home: python3 -m pytest -q\n"
            "full_check_budget_s: 120"
        )
    else:
        elsewhere = tmp_path / "elsewhere" / "svc"
        _init_repo(elsewhere, "feat/x", {"svc/main.py": "y = 2\n"})
        repos = {"svc": {"path": elsewhere, "raw": str(elsewhere), "base": "feat/x"}}
        slices = [
            {"id": "svc-a", "repo": "svc", "write": "svc/a.py"},
            {"id": "home-b", "repo": "home", "write": "docs/b.md"},
        ]
        full_check = 'full_check: { svc: "python3 -m pytest -q", home: "true" }'
    block = "".join(
        f"  - name: {n}\n    path: {d['raw']}\n    base: {d['base']}\n" for n, d in repos.items()
    )
    (box / "briefs").mkdir(parents=True, exist_ok=True)
    for sl in slices:
        (box / "briefs" / f"{sl['id']}.md").write_text(BRIEF.format(sid=sl["id"]))
    (box / "GOAL.md").write_text("# Goal\n\nmulti-repo\n")
    (box / "STATE.md").write_text(
        "schema: 1\niteration: 0\nmax_iterations: 5\nstatus: ready\nmission: r15\n"
    )
    (box / "PLAN.md").write_text(_plan(block, slices, full_check))
    (box / "QUEUE.md").write_text(
        "# Queue\n\n```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n"
    )
    (box / "REPORT.md").write_text("# Report\n")
    (box / "VERDICT.md").write_text("")
    (box / "LOG.md").write_text("# Trio loop log\n")
    (box / ".gitignore").write_text(".dispatch/\n.sessions/\n.lock/\n.driver.json\n.session.json\n")
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "mailbox")
    return {"home": home, "box": box, "repos": repos, "slices": slices}


class _Client:
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


class Scenario:
    """The scripted Lead / Evaluator of one fixture."""

    def __init__(self, layout, wt, trioctl, root: Path):
        self.l = layout
        self.wt = wt
        self.trioctl = trioctl
        self.root = root
        self.lock = threading.Lock()
        self.prompts: list[tuple[str, str]] = []
        self.evals: list[dict] = []
        self.dispatch_views: list[dict] = []
        self.pins: dict | None = None
        self.retire_shas: dict[str, str] = {}
        # eval-r15 N7: a test that asserts overlapping slice-evals sets a
        # Barrier here; every eval then waits inside for all the others, so
        # overlap is guaranteed instead of timed (a broken barrier fails).
        self.overlap_barrier: threading.Barrier | None = None

    # --- Lead -----------------------------------------------------------
    def _worker(self, role, config, *, prompt, workspace, timeout=None, events=None,
                isolated=False, on_spawn=None, wrap=None):
        sid = re.search(r"slice\((\S+)\)", prompt) or re.search(r"# Task (\S+)", prompt)
        sid = sid.group(1)
        sl = next(s for s in self.l["slices"] if s["id"] == sid)
        target = Path(workspace) / sl["write"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {sid}\n")
        assert "REPO: this slice belongs" in prompt if sl["repo"] != "home" else "REPO:" not in prompt
        return f"implemented {sid}\nTARGETED_CHECK: {CHECK_LINE}\n"

    def lead(self, iteration: int, prompt: str) -> None:
        box, home = self.l["box"], self.l["home"]
        plan = (box / "PLAN.md").read_text()
        queue = (box / "QUEUE.md").read_text()
        entries, gates = [], {}
        for sl in self.l["slices"]:
            if f"- id: {sl['id']}\n" not in plan or sl.get("done"):
                continue
            args = self.trioctl.parser().parse_args([
                "omnigent", "run", "builder", "--isolate", "--mailbox", str(box),
                "--worker-slice", sl["id"], "--summary", f"{sl['id']} work",
                "--worktree-root", str(self.root), "--workspace", str(home),
                "--prompt-file", str(box / "briefs" / f"{sl['id']}.md"),
            ])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.trioctl.command_run(args)
            assert code == 0, out.getvalue()
            view = json.loads([ln for ln in out.getvalue().splitlines() if '"worker_worktree"' in ln][-1])
            view = {**view["worker_worktree"], "targeted_check": view["targeted_check"]}
            self.dispatch_views.append(view)
            assert view["state"] == "integrated" and view["merge_commit"] != view["base"]
            assert view["targeted_check"] == f"TARGETED_CHECK: {CHECK_LINE}"
            repo_path = home if sl["repo"] == "home" else self.l["repos"][sl["repo"]]["path"]
            if sl["repo"] != "home":
                assert view["repo_name"] == sl["repo"] and Path(view["repo"]) == repo_path
                assert Path(view["path"]).parent.name.startswith(f"{sl['repo']}-")
            sha = view["merge_commit"]
            assert git(repo_path, "cat-file", "-t", sha) == "commit"
            at = git(repo_path, "log", "-1", "--format=%cI", sha)
            repo_line = f"    repo: {sl['repo']}\n" if sl["repo"] != "home" else ""
            entries.append(f"  - slice: {sl['id']}\n{repo_line}    sha: {sha}\n    at: {at}\n")
            gates[sl["repo"]] = git(repo_path, "rev-parse", "HEAD")
            plan = plan.replace(
                f"  - id: {sl['id']}\n" + (f"    repo: {sl['repo']}\n" if sl["repo"] != "home" else "")
                + f"    writes: [{sl['write']}]\n    reads: []\n    status: planned\n",
                f"  - id: {sl['id']}\n" + (f"    repo: {sl['repo']}\n" if sl["repo"] != "home" else "")
                + f"    writes: [{sl['write']}]\n    reads: []\n    status: complete\n",
            )
            sl["done"] = True
        (box / "PLAN.md").write_text(plan)
        queue = queue.replace("retired:\n```", "retired:\n" + "".join(entries) + "```", 1)
        (box / "QUEUE.md").write_text(queue)
        gate = "; ".join(
            f"gate: PASS @{sha}" if name == "home" else f"gate: PASS @{name}:{sha}"
            for name, sha in gates.items()
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | retired {len(entries)} slice(s); {gate}\n")

    # --- Evaluator ------------------------------------------------------
    def slice_eval(self, ctx, workspace, prompt):
        start = time.monotonic()
        sl = next(s for s in self.l["slices"] if s["id"] == ctx["slice"])
        repo_path = self.l["home"] if sl["repo"] == "home" else self.l["repos"][sl["repo"]]["path"]
        ws = Path(workspace)
        assert ws != repo_path and ws != self.l["home"], "slice-eval must be bound to a worktree"
        assert git(ws, "rev-parse", "HEAD") == ctx["sha"]
        common = Path(git(ws, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        assert common == (repo_path / ".git").resolve()
        assert (ws / sl["write"]).is_file()
        if sl["repo"] != "home":
            assert ctx.get("repo") == sl["repo"]
            assert f"MULTI-REPO: slice `{sl['id']}` belongs to the declared repo `{sl['repo']}`" in prompt
            assert "(a checkout of the declared repo" in prompt
        if self.overlap_barrier is not None:
            self.overlap_barrier.wait()
        else:
            time.sleep(0.4)
        with self.lock:
            with (self.l["box"] / "VERDICT.md").open("a") as fh:
                fh.write(f"\n## slice {ctx['slice']} @{ctx['sha']} — SHIP\n\naccepts: PASS\n")
            self.evals.append({"slice": ctx["slice"], "start": start, "end": time.monotonic()})

    def integration(self, ctx, prompt):
        box, home = self.l["box"], self.l["home"]
        pins = ctx["pins"]
        self.pins = dict(pins)
        assert pins["home"] == git(home, "rev-parse", "HEAD") == ctx["pinned_sha"]
        for name, d in self.l["repos"].items():
            assert pins[name] == git(d["path"], "rev-parse", "HEAD")
        assert "MULTI-REPO (PLAN.md declares `repos:`): the pin is one sha per repo" in prompt
        evaluated = ", ".join(f"{n}@{pins[n]}" for n in ["home", *self.l["repos"]])
        assert f"`evaluated: {evaluated}`" in prompt
        state = (box / "STATE.md").read_text()
        assert "evaluated_repos: " + " ".join(
            f"{n}@{pins[n]}" for n in self.l["repos"]) in state
        commits = []
        for name, d in self.l["repos"].items():
            git(d["path"], "commit", "-q", "--allow-empty", "-m",
                f"loop: iteration {ctx['iteration']} — SHIP ({box.name})")
            sha = git(d["path"], "rev-parse", "HEAD")
            self.retire_shas[name] = sha
            branch = git(d["path"], "symbolic-ref", "--short", "HEAD")
            assert branch == d["base"]
            commits.append(f"commit: {name}@{sha}\n")
        text = (box / "VERDICT.md").read_text()
        (box / "VERDICT.md").write_text(
            "VERDICT: SHIP\n\n"
            f"iteration: {ctx['iteration']}\nattempt: {ctx['evaluator_attempt']}\n"
            f"evaluated: {evaluated}\n" + "".join(commits) + "\n" + text
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {ctx['iteration']} | evaluator | VERDICT: SHIP — multi-repo\n")
        rel = box.relative_to(home).as_posix()
        git(home, "add", "-f", "--", f"{rel}/VERDICT.md", f"{rel}/LOG.md")
        git(home, "add", "-u", "--", rel)
        git(home, "commit", "-q", "-m", f"loop: iteration {ctx['iteration']} — SHIP")


def _runner(trioctl, layout, scenario: Scenario, root: Path, monkeypatch):
    runner = trioctl.OmnigentRunner(
        repo=layout["home"], broker_client=_Client(), config={}, interval=0,
        workspace=str(layout["home"]),
        isolate_workers={"trioctl": SCRIPT, "worktree_root": str(root)},
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda client, mailbox, **kw: {"deleted": len(kw.get("session_ids") or [])})
    monkeypatch.setattr(trioctl, "run_cursor_worker", scenario._worker)
    # `run builder --isolate` normally runs as its own process (main
    # thread); here the scripted Lead calls it from the loop's Lead thread.
    real_signal = trioctl.signal.signal
    monkeypatch.setattr(
        trioctl.signal, "signal",
        lambda sig, handler: real_signal(sig, handler)
        if threading.current_thread() is threading.main_thread() else None,
    )

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        ctx = ctx or {}
        with scenario.lock:
            scenario.prompts.append((ctx.get("kind") or role, prompt))
        if role == "lead":
            assert "MULTI-REPO (PLAN.md declares `repos:`)" in prompt
            scenario.lead(iteration, prompt)
        elif ctx.get("kind") == "slice-eval":
            scenario.slice_eval(ctx, workspace, prompt)
        elif ctx.get("kind") == "integration-eval":
            scenario.integration(ctx, prompt)
        dispatch["session_id"] = f"sess-{role}-{iteration}-{ctx.get('slice') or ctx.get('kind')}"
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    return runner


@pytest.mark.parametrize("kind", ["B", "C"])
def test_multi_repo_open_loop_ships_with_per_repo_worktrees_pins_and_retirement(
    env, monkeypatch, kind
):
    tmp_path, wt, trioctl, core = env
    layout = _layout(tmp_path, kind)
    home, box = layout["home"], layout["box"]
    root = wt.default_worktree_root(home)
    # trio-check accepts the declared layout (exit 0).
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / "metrics" / "trio-check.py"), str(box), "--no-prompt-sync"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    scenario = Scenario(layout, wt, trioctl, root)
    # Every slice is retired in one Lead pass and slice_eval_concurrency
    # covers them all, so all evals must be inside at once (eval-r15 N7).
    scenario.overlap_barrier = threading.Barrier(len(layout["slices"]), timeout=120)
    runner = _runner(trioctl, layout, scenario, root, monkeypatch)
    try:
        code = core.run_loop(box, 5, runner, repo=home, poll_seconds=0.01,
                             slice_eval_concurrency=3)
    finally:
        runner.release_all_fences()
    log = (box / "LOG.md").read_text()
    assert code == 0, log
    state = (box / "STATE.md").read_text()
    assert "status: shipped" in state

    # QUEUE.md: every declared-repo slice retired with `repo:` (and parses).
    tm = core._METRICS
    queue = tm.read_queue(box)
    assert queue["errors"] == []
    by_slice = {e["slice"]: e for e in queue["retired"]}
    for sl in layout["slices"]:
        if sl["repo"] == "home":
            assert "repo" not in by_slice[sl["id"]]
        else:
            assert by_slice[sl["id"]]["repo"] == sl["repo"]
    # One gate per repo touched, repo-qualified for declared repos.
    for name in layout["repos"]:
        assert re.search(rf"gate: PASS @{name}:[0-9a-f]{{40}}", log)
    assert re.search(r"gate: PASS @[0-9a-f]{40}", log)
    # Worktrees came from each slice's own repo (merge sha in THAT repo).
    for view in scenario.dispatch_views:
        if view.get("repo_name"):
            repo_path = layout["repos"][view["repo_name"]]["path"]
            assert git(repo_path, "merge-base", "--is-ancestor", view["merge_commit"], "HEAD") == ""
            assert "-" in Path(view["path"]).parent.name
    # Slice-evals bound from the clones, concurrently.
    assert {e["slice"] for e in scenario.evals} == {s["id"] for s in layout["slices"]}
    overlaps = sum(
        1 for a in scenario.evals for b in scenario.evals
        if a is not b and a["start"] < b["end"] and b["start"] < a["end"]
    )
    assert overlaps > 0, "slice-evals were not concurrent"
    # Integration pinned every repo; SHIP retirement commits landed per repo.
    assert set(scenario.pins) == {"home", *layout["repos"]}
    for name, d in layout["repos"].items():
        subject = git(d["path"], "log", "-1", "--format=%s")
        assert subject == f"loop: iteration 1 — SHIP ({box.name})"
        assert git(d["path"], "rev-parse", "HEAD") == scenario.retire_shas[name]
    assert git(home, "log", "-1", "--format=%s") == "loop: iteration 1 — SHIP"
    # trio-shadow's commit gate resolves each slice in its own repo.
    for sl in layout["slices"]:
        proc = subprocess.run(
            ["python3", str(REPO_ROOT / "metrics" / "trio-shadow.py"), "--mailbox", str(box),
             "--require-commits", "--slice", sl["id"]],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    # Acceptance carries per-repo pins; cleanup removes every worktree.
    accepted = trioctl._ship_acceptance(box, home, core)
    assert accepted.get("evaluated_repos") == {n: scenario.pins[n] for n in layout["repos"]}
    results = trioctl._run_worktree_cleanup(home, box, core)
    assert results and all(r.get("state") == "removed" for r in results), results
    for ledger in [home, *[d["path"] for d in layout["repos"].values()]]:
        assert len(git(ledger, "worktree", "list").splitlines()) == 1
    # The rendered Lead prompt carried the multi-repo rules.
    lead_prompts = [p for k, p in scenario.prompts if k == "lead-pass"]
    assert lead_prompts and "FOUR keys, in this order" in " ".join(lead_prompts[0].split())

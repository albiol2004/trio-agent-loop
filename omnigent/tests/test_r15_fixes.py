"""eval-r15 fixes: regression tests built from the reviewer's repros.

B1: the per-repo SHIP retirement gate verifies from STATE.md
``evaluated_repos`` pins. A pinned repo that vanished or was dropped from
PLAN.md, or an invalid `repos:` block, is final -- never a home-only SHIP.
(repro/q2/test_q2_failopen.py, repro/q3/tests/test_q6.py)
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import test_r15_multi_repo_e2e as E

env = E.env
git = E.git


def _run(env, layout, scenario_cls, monkeypatch, *, iters=2, conc=3):
    _tmp, wt, trioctl, core = env
    root = wt.default_worktree_root(layout["home"])
    scenario = scenario_cls(layout, wt, trioctl, root)
    runner = E._runner(trioctl, layout, scenario, root, monkeypatch)
    try:
        code = core.run_loop(
            layout["box"], iters, runner, repo=layout["home"], poll_seconds=0.01,
            slice_eval_concurrency=conc,
        )
    finally:
        try:
            runner.release_all_fences()
        except FileNotFoundError:
            pass  # a vanished clone's fence (eval-r15 N5, fixed separately)
    return code, scenario, runner


def _state(layout) -> str:
    return (layout["box"] / "STATE.md").read_text()


def _log(layout) -> str:
    return (layout["box"] / "LOG.md").read_text()


def _ship(scenario, ctx, evaluated_names, retire_names):
    """Write a SHIP naming *evaluated_names* pins, with an empty retirement
    commit in each of *retire_names*, plus the home mailbox commit."""
    box, home = scenario.l["box"], scenario.l["home"]
    pins = ctx["pins"]
    scenario.pins = dict(pins)
    commits = []
    for name in retire_names:
        path = scenario.l["repos"][name]["path"]
        git(path, "commit", "-q", "--allow-empty", "-m",
            f"loop: iteration {ctx['iteration']} — SHIP ({box.name})")
        commits.append(f"commit: {name}@{git(path, 'rev-parse', 'HEAD')}\n")
    evaluated = ", ".join(
        pins[n] if n == "home" else f"{n}@{pins[n]}" for n in evaluated_names
    )
    text = (box / "VERDICT.md").read_text()
    (box / "VERDICT.md").write_text(
        "VERDICT: SHIP\n\n"
        f"iteration: {ctx['iteration']}\nattempt: {ctx['evaluator_attempt']}\n"
        f"evaluated: {evaluated}\n" + "".join(commits) + "\n" + text
    )
    with (box / "LOG.md").open("a") as fh:
        fh.write(f"- iter {ctx['iteration']} | evaluator | VERDICT: SHIP\n")
    rel = box.relative_to(home).as_posix()
    git(home, "add", "-f", "--", f"{rel}/VERDICT.md", f"{rel}/LOG.md")
    git(home, "add", "-u", "--", rel)
    git(home, "commit", "-q", "-m", f"loop: iteration {ctx['iteration']} — SHIP")


class HomeOnlyShip(E.Scenario):
    """repro/q2/test_q2_failopen.py: the SHIP names only home and makes no
    per-repo retirement commits; the `repos:` block is stripped or made
    invalid in the same mailbox commit."""

    mode = "strip"

    def integration(self, ctx, prompt):
        box = self.l["box"]
        plan = (box / "PLAN.md").read_text()
        if self.mode == "strip":
            plan = re.sub(r"```yaml\nrepos:\n.*?```\n", "", plan, flags=re.S)
        else:
            plan = plan.replace("    base: dev\n", "    base: dev\n    bogus: 1\n", 1)
        (box / "PLAN.md").write_text(plan)
        _ship(self, ctx, ["home"], [])


@pytest.mark.parametrize("mode", ["strip", "invalid"])
def test_b1_home_only_ship_with_stripped_or_invalid_repos_block_is_final(
    env, monkeypatch, tmp_path, mode
):
    _tmp, _wt, trioctl, core = env
    layout = E._layout(tmp_path, "B")
    cls = type(f"HomeOnly_{mode}", (HomeOnlyShip,), {"mode": mode})
    code, _sc, _runner = _run(env, layout, cls, monkeypatch)
    assert code != 0
    state = _state(layout)
    assert "status: shipped" not in state
    # The pins survived in STATE.md; the gate refused from them.
    assert "evaluated_repos: app-backend@" in state
    expected = (
        "is no longer declared in PLAN.md repos:" if mode == "strip"
        else "PLAN.md repos: block does not validate"
    )
    assert expected in _log(layout)
    assert "retirement cannot complete" in _log(layout)
    accepted = trioctl._ship_acceptance(layout["box"], layout["home"], core)
    assert "pending" in accepted


class VanishDuringIntegration(E.Scenario):
    """repro test_q6.py::test_repo_disappears[VanishDuringIntegration]: a
    post-pin product commit in app-frontend, then the clone is moved out of
    the home; the SHIP otherwise looks complete."""

    def integration(self, ctx, prompt):
        d = self.l["repos"]["app-frontend"]["path"]
        (d / "src" / "evil.js").write_text("evil\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "after pin")
        shutil.move(str(d), str(self.l["home"].parent / "moved-away-fe"))
        _ship(self, ctx, ["home", *self.l["repos"]], ["app-backend"])


def test_b1_repo_vanishes_during_integration_is_final(env, monkeypatch, tmp_path):
    _tmp, _wt, trioctl, core = env
    layout = E._layout(tmp_path, "B")
    code, _sc, _runner = _run(env, layout, VanishDuringIntegration, monkeypatch)
    assert code != 0
    assert "status: shipped" not in _state(layout)
    log = _log(layout)
    assert "PLAN.md repos: block does not validate" in log
    assert "does not exist" in log


class DropRepoFromPlan(E.Scenario):
    """repro test_q6.py::test_drop_repo_from_plan: after a post-pin commit in
    app-frontend, the Evaluator deletes its `repos:` entry (and the slice's
    `repo:`); the block still validates."""

    def integration(self, ctx, prompt):
        d = self.l["repos"]["app-frontend"]["path"]
        (d / "src" / "evil.js").write_text("evil\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "after pin")
        box = self.l["box"]
        plan = (box / "PLAN.md").read_text()
        plan = re.sub(r"  - name: app-frontend\n    path: \S+\n    base: \S+\n", "", plan)
        plan = plan.replace("    repo: app-frontend\n", "")
        (box / "PLAN.md").write_text(plan)
        _ship(self, ctx, ["home", *self.l["repos"]], ["app-backend"])


def test_b1_repo_dropped_from_plan_is_final(env, monkeypatch, tmp_path):
    _tmp, _wt, trioctl, core = env
    layout = E._layout(tmp_path, "B")
    code, _sc, _runner = _run(env, layout, DropRepoFromPlan, monkeypatch)
    assert code != 0
    state = _state(layout)
    assert "status: shipped" not in state
    assert "repo app-frontend is pinned in STATE.md evaluated_repos" in _log(layout)
    # Even a hand-finalized STATE.md never passes trioctl's acceptance.
    (layout["box"] / "STATE.md").write_text(
        re.sub(r"status: \S+", "status: shipped", state)
    )
    accepted = trioctl._ship_acceptance(layout["box"], layout["home"], core)
    assert "no longer declared in PLAN.md repos:" in accepted.get("pending", "")


class PostPinCommitPlanIntact(E.Scenario):
    """Control (repro/failopen.json): the same post-pin commit with PLAN.md
    intact is FINAL (product tree changed)."""

    def integration(self, ctx, prompt):
        d = self.l["repos"]["app-frontend"]["path"]
        (d / "src" / "evil.js").write_text("evil\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "after pin")
        _ship(self, ctx, ["home", *self.l["repos"]], list(self.l["repos"]))


def test_b1_control_post_pin_commit_with_plan_intact_is_final(env, monkeypatch, tmp_path):
    _tmp, _wt, _trioctl, core = env
    layout = E._layout(tmp_path, "B")
    code, _sc, _runner = _run(env, layout, PostPinCommitPlanIntact, monkeypatch)
    assert code != 0
    assert "status: shipped" not in _state(layout)
    box = layout["box"]
    state = core._read_state(box / "STATE.md")
    kind, detail = core._declared_repos_retirement_problem(
        box, 1, (box / "VERDICT.md").read_text(), state
    )
    assert kind == core.RETIREMENT_FINAL
    assert detail.startswith("product tree changed in repo app-frontend")


def _pinned_state(core, layout):
    """STATE.md with an integration pin for every declared repo."""
    box = layout["box"]
    return core._open_loop_integration_context(
        box, layout["home"], 1, box / "STATE.md"
    )


def test_b1_pin_reuse_refuses_a_repo_no_longer_declared(env, tmp_path):
    _tmp, _wt, _trioctl, core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    first = _pinned_state(core, layout)
    # Unchanged tree: the persisted pair is reused.
    again = _pinned_state(core, layout)
    assert again["evaluator_attempt"] == first["evaluator_attempt"]
    # Drop app-frontend from PLAN.md: never reuse, repin without the stale pin.
    plan = (box / "PLAN.md").read_text()
    plan = re.sub(r"  - name: app-frontend\n    path: \S+\n    base: \S+\n", "", plan)
    (box / "PLAN.md").write_text(plan.replace("    repo: app-frontend\n", ""))
    dropped = _pinned_state(core, layout)
    assert dropped["evaluator_attempt"] != first["evaluator_attempt"]
    assert set(dropped["pins"]) == {"home", "app-backend"}
    state = core._read_state(box / "STATE.md")
    assert "app-frontend" not in state["evaluated_repos"]


def test_b1_pin_reuse_refuses_an_invalid_block(env, tmp_path):
    _tmp, _wt, _trioctl, core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    first = _pinned_state(core, layout)
    shutil.move(str(layout["repos"]["app-frontend"]["path"]), str(tmp_path / "gone"))
    again = _pinned_state(core, layout)
    assert again["evaluator_attempt"] != first["evaluator_attempt"]
    # An invalid block pins nothing per repo; the SHIP gate is then final.
    state = core._read_state(box / "STATE.md")
    assert state["evaluated_repos"] == ""
    kind, detail = core._declared_repos_retirement_problem(box, 1, "", state)
    assert kind == core.RETIREMENT_FINAL
    assert "does not validate" in detail


def test_b1_single_repo_mailbox_is_unaffected(env, tmp_path):
    _tmp, _wt, _trioctl, core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    plan = (box / "PLAN.md").read_text()
    (box / "PLAN.md").write_text(re.sub(r"```yaml\nrepos:\n.*?```\n", "", plan, flags=re.S))
    state = {"evaluated_repos": ""}
    assert core._declared_repos_retirement_problem(box, 1, "", state) is None


# --- N1 / N6: retired `repo:` must match PLAN; wrong-repo/phantom shas -------


class RetireTamper(E.Scenario):
    """The scripted Lead retires every slice, then rewrites app-backend's
    be-a entry (repro test_q6.py::WrongRepoSha / PhantomSha and
    test_q2_evalrepo.py): ``repo`` = a wrong `repo:` value (None drops the
    key), ``sha`` = "frontend" (a real app-frontend commit), "phantom"
    (no such object) or None (keep)."""

    repo: str | None = "app-backend"
    sha: str | None = None

    def lead(self, iteration, prompt):
        super().lead(iteration, prompt)
        if iteration != 1:
            return
        box = self.l["box"]
        views = {v.get("repo_name"): v["merge_commit"] for v in self.dispatch_views}
        be = views["app-backend"]
        bad_sha = {"frontend": views["app-frontend"], "phantom": "ab" * 20}.get(self.sha, be)
        self.bad = (self.repo, bad_sha)
        repo_line = f"    repo: {self.repo}\n" if self.repo else ""
        q = (box / "QUEUE.md").read_text()
        old = f"    repo: app-backend\n    sha: {be}\n"
        assert old in q
        (box / "QUEUE.md").write_text(q.replace(old, f"{repo_line}    sha: {bad_sha}\n"))

    def slice_eval(self, ctx, workspace, prompt):
        with self.lock:
            self.__dict__.setdefault("eval_ctx", []).append(dict(ctx))
        super().slice_eval(ctx, workspace, prompt)


@pytest.mark.parametrize(
    "repo, sha, message",
    [
        ("app-frontend", "frontend",
         "has repo: app-frontend but PLAN.md puts slice be-a in repo app-backend"),
        (None, None, "has repo: home but PLAN.md puts slice be-a in repo app-backend"),
        ("home", None, "has repo: home but PLAN.md puts slice be-a in repo app-backend"),
        ("ghost", None, "has repo: ghost but PLAN.md puts slice be-a in repo app-backend"),
        ("app-backend", "frontend", "is not a commit of repo app-backend"),
        ("app-backend", "phantom", "is not a commit of repo app-backend"),
    ],
    ids=["wrong-repo", "omitted", "home", "ghost", "wrong-repo-sha", "phantom-sha"],
)
def test_n1_n6_bad_retired_entry_is_held_at_retire_parse(
    env, monkeypatch, tmp_path, repo, sha, message
):
    layout = E._layout(tmp_path, "B")
    cls = type("Tamper", (RetireTamper,), {"repo": repo, "sha": sha})
    code, scenario, runner = _run(env, layout, cls, monkeypatch)
    assert code != 0
    assert "status: shipped" not in _state(layout)
    log = _log(layout)
    # Fails fast with a clear queue error, not "failed to write a verdict".
    assert message in log, log
    assert "QUEUE.md: slice be-a has a malformed retired entry; not gated as retired" in log
    assert "failed to write a verdict section" not in log
    # The tampered entry was never graded (not on a wrong tree, not on a
    # degraded path). The scripted Lead writes the correct entry first and
    # tampers with it right after, so the driver may grade that one.
    graded = getattr(scenario, "eval_ctx", [])
    bad_repo, bad_sha = scenario.bad
    for ctx in graded:
        if ctx["slice"] == "be-a":
            assert ctx["sha"] != bad_sha or bad_sha == scenario.dispatch_views[0]["merge_commit"]
            assert ctx.get("repo") == "app-backend"
    assert {"fe-b", "home-c"} <= {c["slice"] for c in graded}
    assert not getattr(runner, "eval_isolation_degraded", None)


def test_n1_eval_repo_is_plan_authoritative(env, tmp_path):
    _tmp, _wt, trioctl, _core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    eval_repo = trioctl.OmnigentRunner._eval_repo
    backend = layout["repos"]["app-backend"]["path"]
    name, declared = eval_repo(box, {"slice": "be-a"})
    assert name == "app-backend" and declared["path"] == backend.resolve()
    assert eval_repo(box, {"slice": "be-a", "repo": "app-backend"})[0] == "app-backend"
    assert eval_repo(box, {"slice": "home-c"}) == ("home", None)
    for claimed in ("app-frontend", "ghost", "home"):
        with pytest.raises(trioctl.TrioctlError, match="PLAN.md puts slice"):
            eval_repo(box, {"slice": "be-a", "repo": claimed})


def test_n1_single_repo_queue_without_repo_keys_is_untouched(env, tmp_path):
    _tmp, _wt, _trioctl, core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    plan = (box / "PLAN.md").read_text()
    plan = re.sub(r"```yaml\nrepos:\n.*?```\n", "", plan, flags=re.S)
    (box / "PLAN.md").write_text(plan.replace("    repo: app-backend\n", "").replace(
        "    repo: app-frontend\n", ""))
    (box / "QUEUE.md").write_text(
        "```yaml\nretired:\n  - slice: be-a\n    sha: " + "a" * 40 + "\n    at: t\n```\n"
    )
    raw = core._METRICS.read_queue(box)
    assert core._read_queue(box) == raw

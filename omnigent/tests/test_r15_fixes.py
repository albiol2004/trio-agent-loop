"""eval-r15 fixes: regression tests built from the reviewer's repros.

B1: the per-repo SHIP retirement gate verifies from STATE.md
``evaluated_repos`` pins. A pinned repo that vanished or was dropped from
PLAN.md, or an invalid `repos:` block, is final -- never a home-only SHIP.
(repro/q2/test_q2_failopen.py, repro/q3/tests/test_q6.py)
"""
from __future__ import annotations

import re
import shlex
import shutil
import threading
from pathlib import Path

import pytest

import test_r15_multi_repo_e2e as E

env = E.env
git = E.git


def _run(env, layout, scenario_cls, monkeypatch, *, iters=2, conc=3, configure=None):
    _tmp, wt, trioctl, core = env
    root = wt.default_worktree_root(layout["home"])
    scenario = scenario_cls(layout, wt, trioctl, root)
    if configure is not None:
        configure(scenario)
    runner = E._runner(trioctl, layout, scenario, root, monkeypatch)
    try:
        code = core.run_loop(
            layout["box"], iters, runner, repo=layout["home"], poll_seconds=0.01,
            slice_eval_concurrency=conc,
        )
    finally:
        runner.release_all_fences()
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
    # eval-r17rc W-1: set by the test to the event that `_append_log` (the
    # outer loop's poll thread) fires once it has actually logged the
    # malformed-retired-entry line. `None` (the default) means "don't wait"
    # -- used by nothing in this module today, kept so the class still
    # works standalone if ever reused without the wait wired up.
    malformed_logged: "threading.Event | None" = None

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
        # eval-r17rc W-1: this instant scripted Lead's next call (iteration
        # 2) is a real "no change" pass that counts against the driver's
        # 3-attempt stall budget. The outer poll thread needs at least one
        # full pass to read the now-tampered QUEUE.md and log the malformed
        # entry -- with nothing scheduling that pass but the OS, an instant
        # Lead can burn all 3 attempts and stall the loop before the outer
        # thread ever gets there, so the LOG.md line this test asserts on
        # is never written (a real, load-dependent race, not a product
        # fault: production Lead passes take real wall time). Block on the
        # event the test wires up instead of guessing a sleep duration.
        if self.malformed_logged is not None:
            self.malformed_logged.wait(timeout=10.0)

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
    # eval-r17rc W-1: fire `malformed_logged` the instant the outer loop's
    # poll thread actually appends the malformed-retired-entry LOG.md line,
    # so RetireTamper.lead() can wait on that instead of a fixed sleep (see
    # its docstring for why the race exists).
    _tmp, _wt, _trioctl, core = env
    marker = "has a malformed retired entry; not gated as retired"
    malformed_logged = threading.Event()
    real_append_log = core._append_log

    def _append_log_and_watch(mailbox, line, *a, **kw):
        real_append_log(mailbox, line, *a, **kw)
        if marker in line:
            malformed_logged.set()

    monkeypatch.setattr(core, "_append_log", _append_log_and_watch)
    code, scenario, runner = _run(
        env, layout, cls, monkeypatch,
        configure=lambda s: setattr(s, "malformed_logged", malformed_logged),
    )
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


# --- N2: lockstep with `repos:` renders its own MULTI-REPO procedure --------


def _lockstep_layout(tmp_path):
    layout = E._layout(tmp_path, "B")
    box, home = layout["box"], layout["home"]
    (box / "QUEUE.md").unlink()
    git(home, "add", "-A")
    git(home, "commit", "-q", "-m", "lockstep mailbox")
    return layout


def test_n2_lockstep_prompts_render_the_multi_repo_procedure(env, tmp_path):
    _tmp, _wt, trioctl, _core = env
    layout = _lockstep_layout(tmp_path)
    box, home = layout["box"], layout["home"]
    runner = trioctl.OmnigentRunner(
        repo=home, broker_client=E._Client(), config={}, interval=0, workspace=str(home),
    )
    lead = runner._prompt("lead", 1, box, {})
    assert lead.startswith("MULTI-REPO (PLAN.md declares `repos:`) -- extends steps 3-5:\n")
    assert "`app-backend`" in lead and "git -C <repo path> commit" in lead
    assert "OPEN-LOOP CONTEXT" not in lead.split("# Trio Lead")[0]
    pins = {"home": "a" * 40, "app-backend": "b" * 40, "app-frontend": "c" * 40}
    ev = runner._prompt("evaluator", 1, box, {
        "evaluator_attempt": "att", "pinned_sha": "a" * 40, "pins": pins,
    })
    head, _, body = ev.partition("\n\n# Trio Evaluator")
    assert head.startswith(f"LOCKSTEP CONTEXT: attempt=att sha={'a' * 40} pins=")
    assert (f"`evaluated: home@{'a' * 40}, app-backend@{'b' * 40}, "
            f"app-frontend@{'c' * 40}`") in head
    assert "loop: iteration 1 — SHIP (loop)" in head
    assert "one\n  exception to" in head
    # Single-repo lockstep renders are unchanged (no MULTI-REPO text).
    plan = (box / "PLAN.md").read_text()
    (box / "PLAN.md").write_text(re.sub(r"```yaml\nrepos:\n.*?```\n", "", plan, flags=re.S))
    assert "MULTI-REPO (PLAN.md" not in runner._prompt("lead", 1, box, {})
    single = runner._prompt("evaluator", 1, box, {"evaluator_attempt": "att", "pinned_sha": "a" * 40})
    assert single.startswith(f"LOCKSTEP CONTEXT: attempt=att sha={'a' * 40}\n\n# Trio Evaluator")


class LiteralLockstep(E.Scenario):
    """A lockstep Lead that commits each slice in its repo, and an Evaluator
    that follows the rendered LOCKSTEP prompt literally (repro
    lockstep-False.json: without the note this was exit 6)."""

    def lockstep_lead(self, iteration, prompt):
        assert "MULTI-REPO (PLAN.md declares `repos:`) -- extends steps 3-5" in prompt
        box = self.l["box"]
        plan = (box / "PLAN.md").read_text()
        for sl in self.l["slices"]:
            path = self.l["home"] if sl["repo"] == "home" else self.l["repos"][sl["repo"]]["path"]
            target = path / sl["write"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {sl['id']}\n")
            git(path, "add", "--", sl["write"])
            git(path, "commit", "-q", "-m", f"slice({sl['id']}): work")
            plan = plan.replace("status: planned", "status: complete", 1)
        (box / "PLAN.md").write_text(plan)
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {iteration} | lead | all slices\n")

    def lockstep_eval(self, ctx, prompt):
        box, home = self.l["box"], self.l["home"]
        head = prompt.split("\n\n# Trio Evaluator")[0]
        evaluated = re.search(r"`(evaluated: [^`]+)`", head).group(1)
        command = re.search(r"`(git -C <repo path> commit --allow-empty -m [^`]+)`", head).group(1)
        commits = []
        for name, d in self.l["repos"].items():  # both repos have slices
            argv = shlex.split(command)
            assert argv[:4] == ["git", "-C", "<repo", "path>"]
            git(d["path"], *argv[4:])
            commits.append(f"commit: {name}@{git(d['path'], 'rev-parse', 'HEAD')}\n")
        (box / "VERDICT.md").write_text(
            f"VERDICT: SHIP\n\niteration: {ctx['iteration']}\n"
            f"attempt: {ctx['evaluator_attempt']}\n{evaluated}\n"
            f"commit: {ctx['pinned_sha']}\n" + "".join(commits)
        )
        with (box / "LOG.md").open("a") as fh:
            fh.write(f"- iter {ctx['iteration']} | evaluator | VERDICT: SHIP — lockstep\n")
        rel = box.relative_to(home).as_posix()
        git(home, "add", "-f", "--", f"{rel}/VERDICT.md", f"{rel}/LOG.md")
        git(home, "add", "-u", "--", rel)
        git(home, "commit", "-q", "-m", f"loop: iteration {ctx['iteration']} — SHIP", "--", rel)


def test_n2_lockstep_evaluator_following_the_rendered_prompt_ships(env, monkeypatch, tmp_path):
    _tmp, _wt, trioctl, core = env
    layout = _lockstep_layout(tmp_path)
    box, home = layout["box"], layout["home"]
    scenario = LiteralLockstep(layout, None, trioctl, None)
    runner = trioctl.OmnigentRunner(
        repo=home, broker_client=E._Client(), config={}, interval=0, workspace=str(home),
    )
    monkeypatch.setattr(runner, "_agent_id", lambda role: "agent")
    monkeypatch.setattr(runner, "_resolve_model", lambda role: "m")
    monkeypatch.setattr(runner, "_new_dispatch_nonce", lambda: None)
    monkeypatch.setattr(trioctl, "_prune_broker_sessions",
                        lambda client, mailbox, **kw: {"deleted": 0})

    def run_dispatch(client, agent_id, model, prompt, title, role, iteration, mailbox,
                     ctx, started, before_text, before_mtime, dispatch, workspace):
        if role == "lead":
            scenario.lockstep_lead(iteration, prompt)
        else:
            scenario.lockstep_eval({**(ctx or {}), "iteration": iteration}, prompt)
        dispatch["session_id"] = f"sess-{role}-{iteration}"
        return 0

    monkeypatch.setattr(runner, "_run_dispatch", run_dispatch)
    code = core.run_loop(box, 2, runner, repo=home, mode="lockstep")
    assert code == 0, _log(layout)
    assert "status: shipped" in _state(layout)
    for d in layout["repos"].values():
        assert git(d["path"], "log", "-1", "--format=%s") == "loop: iteration 1 — SHIP (loop)"


# --- N9: wording and `base:` ------------------------------------------------


def test_n9_base_must_be_an_existing_branch(env, tmp_path):
    _tmp, _wt, trioctl, _core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    assert trioctl._repo_scope_refusals(box) == []
    plan = (box / "PLAN.md").read_text()
    (box / "PLAN.md").write_text(plan.replace("    base: dev\n", "    base: nope\n", 1))
    refused = trioctl._repo_scope_refusals(box)
    assert any("base: 'nope' is not a branch" in line for line in refused), refused


def test_n9_old_core_refusal_wording(env, tmp_path, monkeypatch):
    _tmp, _wt, trioctl, _core = env
    layout = E._layout(tmp_path, "B")
    old = type("OldCore", (), {"_METRICS": type("M", (), {"METRICS_API": 4})})()
    line = trioctl._old_core_repos_refusal(layout["box"], old)
    assert "nothing was dispatched" not in line and "no further role is dispatched" in line
    # Plain install.sh layout: no metrics/ next to the bin dir.
    monkeypatch.setattr(trioctl, "__file__", str(tmp_path / "bin" / "trioctl"))
    assert "installed from" in trioctl._bundled_metrics_hint()
    assert "/metrics" not in trioctl._bundled_metrics_hint()


# --- N3: API marker in the loop core itself ---------------------------------


def test_n3_old_core_next_to_new_metrics_is_refused_for_repos(env, tmp_path):
    _tmp, _wt, trioctl, core = env
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    # r16-rc: the core marker is 6 (root-free), in step with trio-metrics.py.
    assert core.METRICS_API == 7 and trioctl._core_metrics_api(core) == 7
    # An r15 core (marker 5) next to an API-6 trio-metrics.py: the lower of
    # the two -- multi-repo yes, root-free open-loop no.
    r15_core = type("R15Core", (), {"METRICS_API": 5, "_METRICS": core._METRICS})()
    assert trioctl._core_metrics_api(r15_core) == 5
    assert trioctl._old_core_repos_refusal(box, r15_core) is None
    assert trioctl._core_metrics_api(r15_core) < trioctl.ROOT_FREE_METRICS_API
    assert trioctl._old_core_repos_refusal(box, core) is None
    # repro test_q2_oldcore.py::test_mixed_old_loop_new_metrics_accepts_repos:
    # a pre-r15 trio_loop.py (no core marker) next to a METRICS_API 5
    # trio-metrics.py.
    old_core = type("OldCore", (), {"_METRICS": core._METRICS})()
    assert trioctl._core_metrics_api(old_core) == 4
    line = trioctl._old_core_repos_refusal(box, old_core)
    assert line and "METRICS_API 4; multi-repo needs 5" in line
    # A new core next to an API-4 trio-metrics.py is refused as before.
    new_core_old_metrics = type(
        "Mixed", (), {"METRICS_API": 5, "_METRICS": type("M", (), {"METRICS_API": 4})}
    )()
    assert trioctl._core_metrics_api(new_core_old_metrics) == 4


# --- N4: full_check: mapping across blank lines -----------------------------


def test_n4_full_check_mapping_continues_across_blank_lines(env, tmp_path):
    _tmp, _wt, trioctl, core = env
    tm = core._METRICS
    text = ("full_check:\n  app-backend: python3 -m pytest -q\n\n"
            "  app-frontend: npx vitest run\n\nnext: 1\n")
    assert tm.parse_full_check(text) == (
        {"app-backend": "python3 -m pytest -q", "app-frontend": "npx vitest run"}, [])
    # A string command still ends at its first blank line.
    assert tm.parse_full_check("full_check: pytest -q\n\n  prose\n") == ({"home": "pytest -q"}, [])
    # The guard now sees (and scope-checks) the repo after the blank line.
    layout = E._layout(tmp_path, "B")
    box = layout["box"]
    plan = (box / "PLAN.md").read_text()
    plan = plan.replace("  app-frontend: npx vitest run\n",
                        "\n  app-frontend: cd /tmp && npx vitest run\n")
    (box / "PLAN.md").write_text(plan)
    refused = trioctl._repo_scope_refusals(box)
    assert any("full_check: command of repo app-frontend runs outside" in r for r in refused), refused


# --- N5: one vanished repo; healthy repos stay independent ------------------


class VanishBeforeEvals(E.Scenario):
    """repro test_q6.py::VanishBeforeEvals: app-frontend moves away right
    after the Lead pass retired every slice."""

    def lead(self, iteration, prompt):
        super().lead(iteration, prompt)
        if iteration == 1:
            d = self.l["repos"]["app-frontend"]["path"]
            shutil.move(str(d), str(self.l["home"].parent / "moved-away-fe"))

    def slice_eval(self, ctx, workspace, prompt):
        with self.lock:
            self.__dict__.setdefault("eval_ws", {})[ctx["slice"]] = Path(workspace)
        super().slice_eval(ctx, workspace, prompt)


def test_n5_vanished_repo_does_not_send_healthy_slice_evals_home(env, monkeypatch, tmp_path):
    layout = E._layout(tmp_path, "B")
    code, scenario, runner = _run(env, layout, VanishBeforeEvals, monkeypatch)
    assert code != 0
    assert "status: shipped" not in _state(layout)
    ws = scenario.eval_ws
    backend = layout["repos"]["app-backend"]["path"]
    # be-a is graded in a worktree of app-backend, never on the home path.
    common = git(ws["be-a"], "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert Path(common) == (backend / ".git").resolve()
    assert "fe-b" not in ws  # its repo is gone: held, never graded elsewhere
    assert not getattr(runner, "eval_isolation_degraded", None)


def test_n5_release_fences_releases_every_repo_despite_a_vanished_one(env, tmp_path, capsys):
    _tmp, wt, trioctl, _core = env
    layout = E._layout(tmp_path, "B")
    backend = layout["repos"]["app-backend"]["path"]
    frontend = layout["repos"]["app-frontend"]["path"]
    runner = trioctl.OmnigentRunner(
        repo=layout["home"], broker_client=E._Client(), config={}, interval=0,
        workspace=str(layout["home"]),
    )
    tokens = {}
    for path in (frontend, backend):  # the vanishing repo's token first
        token = wt.acquire_fence(path, reason="test")
        tokens[path] = token
        runner._fence_tokens[token] = True
        runner._fence_repos[token] = path
    shutil.move(str(frontend), str(tmp_path / "gone-fe"))
    runner.release_all_fences()
    assert "could not release merge fence" in capsys.readouterr().err
    assert not runner._fence_tokens
    assert not any(wt._fence_dir(backend).glob("*")), "app-backend fence leaked"

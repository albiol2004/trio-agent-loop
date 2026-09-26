"""r11: worker isolation and concurrent slice-eval are default ON for
`trioctl omnigent loop`; flags only disable them.

Covers the default-resolution table: flag given / not given x loop core
new (this tree) / old (3b5b93b's `metrics/trio_loop.py`, fetched with
`git show`), the derived worktree root, `--no-isolate-workers`, the
N>1-without-isolation refusal, the lockstep notice and the graceful
fallbacks of an unmet default prerequisite.
"""
from __future__ import annotations

import functools
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.tests.test_omnigent_loop import load_trioctl, make_mailbox

ROOT = Path(__file__).resolve().parents[2]
OLD_CORE_REV = "3b5b93b"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic HOME / XDG state; no TRIO_WORKTREE_ROOT override."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TRIO_WORKTREE_ROOT", raising=False)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@pytest.fixture
def repo(env: Path) -> Path:
    repo = env / "product"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def trioctl(monkeypatch: pytest.MonkeyPatch):
    module = load_trioctl()
    # The real ~/.cursor and /etc/cursor are not part of these tests.
    monkeypatch.setattr(
        module.worker_worktrees, "inherited_cursor_problems", lambda **kw: []
    )
    return module


def parse(trioctl, *argv: str):
    return trioctl.parser().parse_args(["omnigent", "loop", *argv])


class NewCore:
    @staticmethod
    def run_loop(mailbox, max_iterations, runner, *, repo=None,
                 slice_eval_concurrency=1, slice_eval_drain_seconds=None):
        return 0


def old_core_source() -> str:
    return subprocess.run(
        ["git", "show", f"{OLD_CORE_REV}:metrics/trio_loop.py"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def old_core(trioctl, tmp_path: Path):
    """3b5b93b's vendored loop core, loaded through trioctl's own loader."""
    vendored = tmp_path / "old-vendor"
    (vendored / "metrics").mkdir(parents=True)
    (vendored / "metrics" / "trio_loop.py").write_text(old_core_source())
    shutil.copy(ROOT / "metrics" / "trio-metrics.py", vendored / "metrics")
    core = trioctl._load_trio_loop(vendored)
    assert not hasattr(core, "_slice_eval_drain_seconds")  # really pre-r10
    return core


# -- parser defaults ---------------------------------------------------------


def test_parser_defaults_mean_not_given(trioctl) -> None:
    args = parse(trioctl)
    assert args.isolate_workers is None
    assert args.worktree_root is None
    assert args.slice_eval_concurrency is None
    assert args.slice_eval_drain_seconds is None
    assert parse(trioctl, "--no-isolate-workers").isolate_workers is False
    assert parse(trioctl, "--isolate-workers").isolate_workers is True
    with pytest.raises(SystemExit):
        parse(trioctl, "--isolate-workers", "--no-isolate-workers")


def test_help_lists_disable_flags(trioctl, capsys) -> None:
    with pytest.raises(SystemExit):
        parse(trioctl, "--help")
    out = " ".join(capsys.readouterr().out.split())
    assert "--no-isolate-workers" in out
    assert "--slice-eval-concurrency N" in out
    assert "default 4" in out


# -- isolation + derived worktree root ----------------------------------------


def test_isolation_default_on_with_derived_root_outside_repo(
    trioctl, repo: Path, env: Path, capsys
) -> None:
    isolate, off = trioctl._resolve_isolation(parse(trioctl), repo)
    assert off is None
    root = Path(isolate["worktree_root"])
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    key = hashlib.sha256(str(common.resolve()).encode()).hexdigest()[:12]
    expected = (env / "state" / "trio-agent-loop" / "worktrees" / f"product-{key}").resolve()
    assert root == expected
    assert not str(root).startswith(str(repo) + os.sep)
    assert not root.exists()  # created on demand, not at loop start
    assert capsys.readouterr().err.splitlines() == [f"trioctl: worktree root {root}"]
    # Deterministic per repository.
    again, _ = trioctl._resolve_isolation(parse(trioctl), repo)
    assert again["worktree_root"] == str(root)
    # Created on the first worktree.
    record = trioctl.worker_worktrees.create(
        repo, slice_id="S", root=Path(isolate["worktree_root"])
    )
    assert root.is_dir() and Path(record["path"]).parent == root


def test_derived_root_falls_back_to_local_state(
    trioctl, repo: Path, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME")
    isolate, _ = trioctl._resolve_isolation(parse(trioctl), repo)
    base = (env / "home" / ".local" / "state" / "trio-agent-loop" / "worktrees").resolve()
    assert Path(isolate["worktree_root"]).parent == base


def test_explicit_worktree_root_wins(trioctl, repo: Path, env: Path, capsys) -> None:
    mine = env / "mine"
    isolate, _ = trioctl._resolve_isolation(
        parse(trioctl, "--worktree-root", str(mine)), repo
    )
    assert isolate["worktree_root"] == str(mine.resolve())
    assert f"trioctl: worktree root {mine.resolve()}" in capsys.readouterr().err


def test_no_isolate_workers_disables(trioctl, repo: Path, capsys) -> None:
    isolate, off = trioctl._resolve_isolation(parse(trioctl, "--no-isolate-workers"), repo)
    assert isolate is None and off == "--no-isolate-workers"
    assert capsys.readouterr().err == ""


def test_isolate_workers_flag_is_a_compatible_no_op(trioctl, repo: Path) -> None:
    default, _ = trioctl._resolve_isolation(parse(trioctl), repo)
    explicit, off = trioctl._resolve_isolation(parse(trioctl, "--isolate-workers"), repo)
    assert off is None and explicit == default


def test_default_isolation_degrades_outside_git(trioctl, env: Path, capsys) -> None:
    plain = env / "plain"
    plain.mkdir()
    isolate, off = trioctl._resolve_isolation(parse(trioctl), plain)
    assert isolate is None and off == "not a git checkout"
    assert "worker isolation unavailable (not a git checkout)" in capsys.readouterr().err


def test_default_isolation_degrades_on_detached_head(trioctl, repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    isolate, off = trioctl._resolve_isolation(parse(trioctl), repo)
    assert isolate is None and "detached HEAD" in off


def test_observe_workers_degrades_default_but_refuses_explicit(
    trioctl, repo: Path, capsys
) -> None:
    isolate, off = trioctl._resolve_isolation(parse(trioctl, "--observe-workers"), repo)
    assert isolate is None and "--observe-workers" in off
    assert "running without isolation" in capsys.readouterr().err
    with pytest.raises(trioctl.TrioctlError, match="cannot be combined"):
        trioctl._resolve_isolation(
            parse(trioctl, "--observe-workers", "--isolate-workers"), repo
        )


def test_inherited_cursor_bindings_degrade_default_but_refuse_explicit(
    trioctl, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        trioctl.worker_worktrees, "inherited_cursor_problems",
        lambda **kw: ["~/.cursor/mcp.json: omnigent (session-bound)"],
    )
    isolate, off = trioctl._resolve_isolation(parse(trioctl), repo)
    assert isolate is None and "session-bound" in off
    assert "worker isolation unavailable" in capsys.readouterr().err
    with pytest.raises(trioctl.TrioctlError, match="--isolate-workers refused"):
        trioctl._resolve_isolation(parse(trioctl, "--isolate-workers"), repo)


# -- concurrency x isolation x core ---------------------------------------------


def test_concurrency_default_four_on_new_core(trioctl) -> None:
    assert trioctl._slice_eval_concurrency_kwargs(NewCore, parse(trioctl)) == {
        "slice_eval_concurrency": 4
    }
    assert trioctl._slice_eval_concurrency_kwargs(
        NewCore, parse(trioctl, "--slice-eval-concurrency", "1")
    ) == {}


def test_explicit_concurrency_without_isolation_is_refused(trioctl) -> None:
    args = parse(trioctl, "--no-isolate-workers", "--slice-eval-concurrency", "4")
    with pytest.raises(trioctl.TrioctlError, match="need worker isolation"):
        trioctl._slice_eval_concurrency_kwargs(
            NewCore, args, isolation_off="--no-isolate-workers"
        )
    # Explicit 1 without isolation is fine; so is the default (silently 1).
    for argv in (("--no-isolate-workers", "--slice-eval-concurrency", "1"),
                 ("--no-isolate-workers",)):
        assert trioctl._slice_eval_concurrency_kwargs(
            NewCore, parse(trioctl, *argv), isolation_off="--no-isolate-workers"
        ) == {}


def test_real_old_core_default_degrades_explicit_refused(
    trioctl, old_core, capsys
) -> None:
    assert trioctl._slice_eval_concurrency_kwargs(old_core, parse(trioctl)) == {}
    assert capsys.readouterr().err.strip() == trioctl.OLD_CORE_SERIAL_WARNING
    assert trioctl.OLD_CORE_SERIAL_WARNING == (
        "trioctl: vendored loop core predates concurrent slice-eval; running "
        "serial (refresh metrics/ to enable)"
    )
    with pytest.raises(trioctl.TrioctlError, match="refused"):
        trioctl._slice_eval_concurrency_kwargs(
            old_core, parse(trioctl, "--slice-eval-concurrency", "4")
        )
    assert trioctl._slice_eval_concurrency_kwargs(
        old_core, parse(trioctl, "--slice-eval-concurrency", "1")
    ) == {}
    assert capsys.readouterr().err == ""


def test_real_new_core_takes_default(trioctl) -> None:
    core = trioctl._load_trio_loop(ROOT)
    assert trioctl._slice_eval_concurrency_kwargs(core, parse(trioctl)) == {
        "slice_eval_concurrency": 4
    }


# -- end to end through `loop` ------------------------------------------------------


def _run_loop(trioctl, monkeypatch, repo: Path, core, argv: list[str], *, queue: bool):
    """Drive `omnigent loop` with a recording core and stub runner."""
    mailbox = make_mailbox(repo)
    if queue:
        (mailbox / "QUEUE.md").write_text("# queue\n")
    calls: dict = {"cleanup": 0}
    real = core.run_loop

    @functools.wraps(real)  # inspect.signature follows __wrapped__
    def run_loop(mailbox, max_iterations, runner, **kwargs):
        calls["run_loop"] = kwargs
        return 0

    class Core:
        pass

    Core.run_loop = staticmethod(run_loop)

    class Runner:
        held_session_ids: list[str] = []
        created_session_ids: list[str] = []

        def release_all_fences(self):
            pass

        def restore_root_config_final(self, mailbox):
            pass

    def make_runner(**kw):
        calls["runner"] = kw
        return Runner()

    def cleanup(*a, **kw):
        calls["cleanup"] += 1
        return []

    monkeypatch.setattr(trioctl, "_load_trio_loop", lambda repo: Core)
    monkeypatch.setattr(trioctl, "OmnigentRunner", make_runner)
    monkeypatch.setattr(trioctl, "_run_post_loop_session_prune", lambda *a, **kw: [])
    monkeypatch.setattr(trioctl, "_run_worktree_cleanup", cleanup)
    monkeypatch.chdir(repo)
    args = parse(trioctl, "--mailbox", str(mailbox), "--max-iterations", "2", *argv)
    assert args.func(args) == 0
    return calls


def test_plain_loop_is_isolated_and_concurrent(
    trioctl, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls = _run_loop(trioctl, monkeypatch, repo, NewCore, [], queue=True)
    assert calls["run_loop"] == {"repo": repo.resolve(), "slice_eval_concurrency": 4}
    iso = calls["runner"]["isolate_workers"]
    assert iso["worktree_root"].startswith(os.environ["XDG_STATE_HOME"])
    assert calls["cleanup"] == 2  # before and after the loop
    err = capsys.readouterr().err
    assert err.count("trioctl: worktree root ") == 1
    assert trioctl.LOCKSTEP_CONCURRENCY_NOTICE not in err


def test_plain_loop_on_old_core_runs_isolated_and_serial(
    trioctl, old_core, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls = _run_loop(trioctl, monkeypatch, repo, old_core, [], queue=True)
    assert calls["run_loop"] == {"repo": repo.resolve()}
    assert "isolate_workers" in calls["runner"]  # isolation is trioctl-only
    assert trioctl.OLD_CORE_SERIAL_WARNING in capsys.readouterr().err


def test_explicit_concurrency_on_old_core_refused_end_to_end(
    trioctl, old_core, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(trioctl.TrioctlError, match="refused"):
        _run_loop(trioctl, monkeypatch, repo, old_core,
                  ["--slice-eval-concurrency", "4"], queue=True)


def test_no_isolate_loop_is_serial_and_explicit_n_refused(
    trioctl, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls = _run_loop(trioctl, monkeypatch, repo, NewCore,
                      ["--no-isolate-workers"], queue=True)
    assert calls["run_loop"] == {"repo": repo.resolve()}
    assert "isolate_workers" not in calls["runner"]
    assert calls["cleanup"] == 0
    assert capsys.readouterr().err == ""
    shutil.rmtree(repo / "mailbox")
    with pytest.raises(trioctl.TrioctlError, match="need worker isolation"):
        _run_loop(trioctl, monkeypatch, repo, NewCore,
                  ["--no-isolate-workers", "--slice-eval-concurrency", "2"], queue=True)


def test_lockstep_notice_without_behaviour_change(
    trioctl, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls = _run_loop(trioctl, monkeypatch, repo, NewCore, [], queue=False)
    assert calls["run_loop"] == {"repo": repo.resolve(), "slice_eval_concurrency": 4}
    err = capsys.readouterr().err.splitlines()
    assert err.count(trioctl.LOCKSTEP_CONCURRENCY_NOTICE) == 1
    shutil.rmtree(repo / "mailbox")
    _run_loop(trioctl, monkeypatch, repo, NewCore,
              ["--slice-eval-concurrency", "1"], queue=False)
    assert trioctl.LOCKSTEP_CONCURRENCY_NOTICE not in capsys.readouterr().err

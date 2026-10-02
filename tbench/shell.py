"""Pure shell-command builders for ``TrioOpenCodeAgent``.

Kept free of any Harbor import so these can be unit-tested as plain string
construction: given the same arguments they always render the same command,
and the tests assert the safety invariants (no ``--auto``, no
``--dangerously-skip-permissions``, the driver is always launched detached).
"""
from __future__ import annotations

import re
import shlex

from goal import HEAVY_DIR_EXCLUDE_PATTERNS

GIT_SAFE_DIRECTORY_CMD = "git config --global --add safe.directory '*'"

_GIT_USER_NAME = "trio-bench"
_GIT_USER_EMAIL = "trio-bench@localhost"


_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def parse_git_version(output: str) -> tuple[int, int, int] | None:
    """Parse ``git --version`` output into ``(major, minor, patch)``, or
    ``None`` if the output does not match the expected shape."""
    match = _GIT_VERSION_RE.search(output)
    if not match:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch or 0))


def git_version_at_least(
    output: str, minimum: tuple[int, int, int] = (2, 30, 0)
) -> bool | None:
    """True/False if *output* parses and compares against *minimum*; ``None``
    if the version could not be parsed at all (caller should not treat that
    as a hard failure -- just log it)."""
    version = parse_git_version(output)
    if version is None:
        return None
    return version >= minimum


def is_musl_output(ldd_output: str, *, has_alpine_release: bool) -> bool:
    """True if *ldd_output* (``ldd --version`` stdout+stderr) or the
    presence of ``/etc/alpine-release`` indicates a musl libc base image --
    the bundled CPython and OpenCode binaries are glibc-dynamic and cannot
    run there."""
    return has_alpine_release or "musl" in ldd_output.lower()


def build_workdir_baseline_script(workdir: str) -> str:
    """Shell script (run as root, cwd-independent) that turns *workdir*
    into a fresh git repo if it is not one already, with a repo-local
    ``trio-bench`` identity, a size/heavy-dir ``.git/info/exclude``, and a
    ``task: baseline`` commit of its pre-existing contents. Idempotent: if
    ``.git`` already exists this still (re)writes identity/exclude and
    commits any residual untracked content, which is harmless.
    """
    exclude_lines = "\n".join(HEAVY_DIR_EXCLUDE_PATTERNS)
    quoted_workdir = shlex.quote(workdir)
    return (
        f"cd {quoted_workdir} && "
        "( [ -d .git ] || ( git init -q -b main 2>/dev/null || git init -q ) ) && "
        f"git config user.name {shlex.quote(_GIT_USER_NAME)} && "
        f"git config user.email {shlex.quote(_GIT_USER_EMAIL)} && "
        "mkdir -p .git/info && "
        f"printf '%s\\n' {shlex.quote(exclude_lines)} > .git/info/exclude && "
        "find . -xdev -type f -size +20M -not -path './.git/*' 2>/dev/null "
        "| sed 's|^\\./||' >> .git/info/exclude || true && "
        "git add -A && "
        "git commit -q -m 'task: baseline' --allow-empty"
    )


def build_detached_init_script(trio_ws: str) -> str:
    """Shell script that creates *trio_ws* as a fresh, independent git repo
    for the loop's own mailbox, used when the task's working directory is
    already a git repo the task is ABOUT (so its history must not be
    touched)."""
    quoted = shlex.quote(trio_ws)
    return (
        f"mkdir -p {quoted} && cd {quoted} && "
        "( [ -d .git ] || ( git init -q -b main 2>/dev/null || git init -q ) ) && "
        f"git config user.name {shlex.quote(_GIT_USER_NAME)} && "
        f"git config user.email {shlex.quote(_GIT_USER_EMAIL)}"
    )


def build_goal_commit_script(repo_root: str) -> str:
    """Commit a freshly-written ``loop/GOAL.md`` (and any other seeded
    mailbox files) as ``loop: goal``."""
    quoted = shlex.quote(repo_root)
    return (
        f"cd {quoted} && git add loop && git commit -q -m 'loop: goal' --allow-empty"
    )


def build_launch_command(
    *,
    python_bin: str,
    cli_path: str,
    mailbox_dir: str,
    max_iterations: int,
    config_path: str,
    opencode_bin_dir: str,
    log_path: str,
    exit_code_path: str,
    cwd: str,
    subcommand: str = "start",
    slice_eval_concurrency: int | None = None,
    no_isolate_workers: bool = False,
    slice_eval_drain_seconds: float | None = None,
    no_kill_check: bool = False,
) -> str:
    """Build the ``nohup setsid ... &`` command that launches the driver
    detached, with its exit code captured to *exit_code_path* once it ends.
    Never passes ``--auto`` or ``--dangerously-skip-permissions`` -- those
    are forbidden for this agent (opencode's own permission map, generated
    by ``ocgen.py``, is what gates the run instead).

    The four open-loop pass-through flags (``trio_opencode/cli.py``'s
    ``start``/``resume`` subcommands; no-ops on a lockstep mailbox) are
    each emitted only when the caller actually sets them -- every default
    here (``None``/``False``) reproduces the exact pre-existing command
    line byte-for-byte:

    * ``slice_eval_concurrency`` -> ``--slice-eval-concurrency N``
    * ``no_isolate_workers`` -> ``--no-isolate-workers``
    * ``slice_eval_drain_seconds`` -> ``--slice-eval-drain-seconds S``
    * ``no_kill_check`` -> ``--no-kill-check``
    """
    if subcommand not in ("start", "resume"):
        raise ValueError(f"unsupported subcommand: {subcommand!r}")
    assert "--auto" not in cli_path
    extra_flags = ""
    if slice_eval_concurrency is not None:
        extra_flags += f" --slice-eval-concurrency {int(slice_eval_concurrency)}"
    if no_isolate_workers:
        extra_flags += " --no-isolate-workers"
    if slice_eval_drain_seconds is not None:
        extra_flags += f" --slice-eval-drain-seconds {slice_eval_drain_seconds}"
    if no_kill_check:
        extra_flags += " --no-kill-check"
    inner = (
        f"PATH={shlex.quote(opencode_bin_dir)}:$PATH "
        f"{shlex.quote(python_bin)} {shlex.quote(cli_path)} {subcommand} "
        f"--mailbox {shlex.quote(mailbox_dir)} "
        f"--max-iterations {int(max_iterations)} --in-place "
        f"--config {shlex.quote(config_path)}{extra_flags} "
        f">> {shlex.quote(log_path)} 2>&1 < /dev/null; "
        f"echo $? > {shlex.quote(exit_code_path)}"
    )
    assert "--auto" not in inner
    assert "--dangerously-skip-permissions" not in inner
    quoted_cwd = shlex.quote(cwd)
    return (
        f"cd {quoted_cwd} && "
        f"rm -f {shlex.quote(exit_code_path)} && "
        f"nohup setsid bash -c {shlex.quote(inner)} >/dev/null 2>&1 < /dev/null & disown"
    )


def build_poll_command(exit_code_path: str) -> str:
    """Command polled every ~60s: prints the exit code once the driver has
    finished, or ``RUNNING`` while it is still going."""
    quoted = shlex.quote(exit_code_path)
    return f"if [ -f {quoted} ]; then cat {quoted}; else echo RUNNING; fi"


def build_find_xdg_data_dirs_command(git_common_dir: str) -> str:
    """List every ``run-*/xdg/data`` directory the driver's ``ocgen.generate``
    created under *git_common_dir*``/trio-opencode`` (one per ``start``/
    ``resume`` execution -- a resumed run gets a fresh ``exec_id`` and thus
    its own data dir, so usage must be summed across all of them)."""
    base = shlex.quote(f"{git_common_dir}/trio-opencode")
    return f"find {base} -mindepth 3 -maxdepth 3 -type d -path '*/xdg/data' 2>/dev/null"

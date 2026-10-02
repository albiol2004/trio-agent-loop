"""Containment of the r19 acceptance author's OpenCode turn.

The author must judge the product from its export alone. Permission globs
over shell command text cannot enforce that (``cd ..``, symlinks,
``python -c "open('/x')"``, string building all get past them), so there are
two real levels, chosen at run time by a probe -- never by "is bwrap on
PATH":

``sandbox``  the whole ``opencode`` process runs under ``bwrap`` with a tmpfs
             root: only the export (read-write), the author's private scratch,
             the OpenCode config/cache directories, the validator's directory,
             the system directories a process needs (``/usr`` ... , a short
             ``/etc`` list) and the ``PATH`` / opencode-binary directories are
             mounted. The loop repository and its ancestors do not exist for
             it, so a shell is safe and stays available. The network is NOT
             unshared (the provider must stay reachable).
``no-shell`` bwrap is unusable (Docker/Harbor task containers block user
             namespaces, and the container's security options are not ours to
             loosen): the author gets NO shell, and only OpenCode's own
             read/glob/grep/edit tools, whose permission rules ``ocgen``
             narrows to the export (``ocgen._acceptance_permission``).
             OpenCode folds ``..`` lexically but does not follow symlinks when
             it decides a path is inside the project, so
             :func:`sanitize_export` removes every symlink that leaves the
             export before the turn.

Either way the post-hoc audit stays as the net.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from .ocgen import LEVEL_NO_SHELL, LEVEL_SANDBOX, _is_under

#: ``bwrap`` namespaces used for the author: everything but the network.
_NS_FLAGS = ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
             "--die-with-parent", "--new-session")
#: System directories mounted read-only (a symlink such as merged-usr
#: ``/bin -> usr/bin`` is recreated as a symlink).
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
#: The ``/etc`` entries a process (DNS, TLS, user lookup, the loader) needs.
_ETC_ENTRIES = ("passwd", "group", "nsswitch.conf", "hosts", "host.conf", "resolv.conf",
                "ssl", "ca-certificates", "ca-certificates.conf", "localtime", "alternatives",
                "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "pki", "crypto-policies")

_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def bwrap_usable(bwrap: str = "bwrap") -> tuple[bool, str]:
    """``(usable, detail)``: does ``bwrap`` actually start a user-namespaced
    sandbox here? Probed once per process with the same namespace flags the
    author wrapper uses (a binary on PATH proves nothing: unprivileged user
    namespaces are blocked in most containers and some hardened hosts)."""
    path = shutil.which(bwrap)
    if not path:
        return False, "bwrap is not installed"
    key = os.path.realpath(path)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    true_bin = shutil.which("true") or "/bin/true"
    try:
        done = subprocess.run(
            [path, *_NS_FLAGS, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--", true_bin],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        if done.returncode == 0:
            result = (True, f"bwrap {path} starts a user-namespaced sandbox")
        else:
            lines = (done.stderr or done.stdout or "").strip().splitlines()
            result = (False, (lines[-1] if lines else f"bwrap exited {done.returncode}")[:200])
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"bwrap probe failed: {type(exc).__name__}: {exc}"[:200])
    _PROBE_CACHE[key] = result
    return result


def sanitize_export(export: "str | Path") -> list[str]:
    """Remove every symlink under ``export`` whose target is not inside it
    (absolute links to the repository, ``../..`` climbs, dangling or looping
    links), and every regular file with more than one hard link (a freshly
    written export has none; one that has is a second name for a file that
    may live outside). OpenCode decides "inside the project" lexically, so
    such an entry would let a read, glob or edit reach outside through a path
    that looks internal. Returns the removed paths, relative to ``export``."""
    root = os.path.realpath(str(export))
    removed: list[str] = []
    for here, dirs, files in os.walk(str(export), topdown=True, followlinks=False):
        for name in [*dirs, *files]:
            path = os.path.join(here, name)
            if not os.path.islink(path):
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                    try:
                        os.unlink(path)
                    except OSError:
                        continue
                    removed.append(os.path.relpath(path, str(export)))
                continue
            target = os.path.realpath(path)
            if _is_under(target, root):
                continue
            try:
                os.unlink(path)
            except OSError:
                continue
            removed.append(os.path.relpath(path, str(export)))
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
    return sorted(removed)


#: Sentinel inside the guard ``.git`` so only a guard we made is ever removed
#: (a real export never has a ``.git``: ``build_export`` strips it).
_GUARD_MARK = "trio-author-guard"


def _git_ancestor(path: "str | Path") -> "str | None":
    """The nearest ancestor of ``path`` that has a ``.git`` entry, else None
    (OpenCode takes it as the project root and its ``grep``/``glob`` ``path``
    arguments climbing to it count as inside the project)."""
    here = os.path.dirname(os.path.realpath(str(path)))
    while True:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def ensure_project_root(export: "str | Path") -> bool:
    """Make ``export`` its own OpenCode project root when a git ancestor
    would otherwise be (a dotfiles HOME, a repository holding the loop's
    state): a minimal empty ``.git`` inside it. Returns True when a guard is
    now in place. :func:`release_project_root` removes it after the turn so
    the export the validator and the freeze see is unchanged."""
    export = Path(export)
    git = export / ".git"
    if git.is_dir() and (git / _GUARD_MARK).exists():
        return True
    if os.path.lexists(git) or _git_ancestor(export) is None:
        return False
    try:
        (git / "objects").mkdir(parents=True)
        (git / "refs").mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / _GUARD_MARK).write_text("", encoding="utf-8")
    except OSError:
        shutil.rmtree(git, ignore_errors=True)
        return False
    return True


def release_project_root(export: "str | Path") -> bool:
    """Remove the guard :func:`ensure_project_root` made (only that one)."""
    git = Path(export) / ".git"
    if git.is_dir() and not git.is_symlink() and (git / _GUARD_MARK).exists():
        shutil.rmtree(git, ignore_errors=True)
        return True
    return False


def _blocked(path: str, forbidden: list[str]) -> bool:
    """True when mounting ``path`` would expose a forbidden root: inside one
    or an ancestor of one (``/`` included)."""
    real = os.path.realpath(path)
    return any(_is_under(real, f) or _is_under(f, real) for f in forbidden)


def _exposes(path: str, forbidden: list[str]) -> bool:
    """True when binding exactly ``path`` would expose a forbidden root: it IS
    one or an ANCESTOR of one. (A dir that merely sits inside a forbidden root
    exposes nothing else of it -- the caller names the dirs it needs.)"""
    real = os.path.realpath(path)
    return any(_is_under(f, real) for f in forbidden)


def sandbox_prefix(*, export: "str | Path", scratch: "str | Path", config_dir: "str | Path",
                   env: dict[str, str], opencode_bin: str, forbidden: list[str],
                   extra_ro: list[str] = (), extra_rw: list[str] = (),
                   bwrap: str = "bwrap") -> list[str]:
    """The ``bwrap ... --`` argv prefix for one author turn (the runner
    appends the ``opencode run ...`` argv). ``env`` is the environment the
    turn will run with: its ``PATH`` directories, ``XDG_CACHE_HOME`` /
    ``XDG_CONFIG_HOME`` and ``OPENCODE_*`` paths decide what is mounted."""
    forb = [os.path.realpath(f) for f in forbidden]
    export_s, scratch_s = str(export), str(scratch)
    cmd = [shutil.which(bwrap) or bwrap, "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
           "--proc", "/proc", "--dev", "/dev"]
    for d in _SYSTEM_DIRS:
        if os.path.islink(d):
            cmd += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            cmd += ["--ro-bind", d, d]
    for name in _ETC_ENTRIES:
        src = os.path.join("/etc", name)
        if not os.path.lexists(src):
            continue
        real = os.path.realpath(src)
        if os.path.islink(src) and not _is_under(real, "/etc"):
            # e.g. resolv.conf -> /run/systemd/resolve/stub-resolv.conf
            if os.path.exists(real) and not _blocked(real, forb):
                cmd += ["--ro-bind", real, real, "--symlink", real, src]
            continue
        cmd += ["--ro-bind", src, src]
    mounted: set[str] = set(_SYSTEM_DIRS)

    def ro(path: str) -> None:
        if not path or not os.path.exists(path) or path in mounted or _blocked(path, forb):
            return
        if any(_is_under(os.path.realpath(path), os.path.realpath(m)) for m in mounted
               if os.path.exists(m)):
            return
        mounted.add(path)
        cmd.extend(["--ro-bind", path, path])

    # the interpreters and tools the author may run, and the opencode binary
    for d in (env.get("PATH") or "").split(os.pathsep):
        if d.startswith("/"):
            ro(d)
    found = shutil.which(opencode_bin, path=env.get("PATH"))
    if found:
        for cand in (os.path.dirname(found), os.path.dirname(os.path.realpath(found))):
            ro(cand)
    for exe in ("python3", "python"):
        py = shutil.which(exe, path=env.get("PATH"))
        if py:   # a relocatable interpreter keeps its stdlib beside bin/
            ro(os.path.dirname(os.path.dirname(os.path.realpath(py))))
    for d in extra_ro:
        ro(str(d))
    # The author's own config dir (``OPENCODE_CONFIG`` / ``_DIR``, and the
    # XDG config home beside it) is mounted READ-ONLY by name even when it
    # sits inside a forbidden root: without it the real binary reports
    # ``Agent not found``. Exactly that directory, never a parent of it.
    cfg = str(config_dir)
    if os.path.isdir(cfg) and not _exposes(cfg, forb) and cfg not in mounted:
        mounted.add(cfg)
        cmd.extend(["--ro-bind", cfg, cfg])
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        d = env.get(key)
        if d and os.path.isdir(d) and not _blocked(d, forb) \
                and not any(_is_under(os.path.realpath(d), os.path.realpath(m))
                            for m in (cfg, scratch_s)):
            cmd += ["--bind", d, d]
    for d in (scratch_s, export_s, *map(str, extra_rw)):
        cmd += ["--bind", d, d]
    home = os.path.join(scratch_s, "home")
    cmd += ["--setenv", "HOME", home, "--unsetenv", "OLDPWD", *_NS_FLAGS,
            "--chdir", export_s, "--"]
    return cmd

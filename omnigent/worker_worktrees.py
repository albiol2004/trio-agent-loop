"""Task-owned git worktrees for concurrent Trio workers.

Cursor reads its project ``.cursor/mcp.json`` / ``.cursor/hooks.json`` from
the nearest directory holding a ``.git`` entry. Workers that share the
repository root therefore share (and overwrite) one project config: a
headless builder loads the Lead's session-bound Omnigent MCP server and
fires its ``stop`` hook, misrouting tools and turn-end receipts. A linked
worktree has its own ``.git`` file, so each worker bound to its own
worktree root gets its own project config scope.

Lifecycle (one ledger record per worker, stored in the repository's git
common dir so it survives restarts and never shows in ``git status``)::

    created -> running -> exited -> committed -> integrated -> accepted
            -> removing -> removed
    (any)   -> retained:<reason>   (recoverable; never deleted)

Deletion happens only for a ledger-owned worktree whose worker commit is
merged into the aggregate branch, whose merge is contained in the exact
revision a driver-finalized, retired SHIP evaluated (the caller supplies
that acceptance from the loop core's own retirement contract), that no
live process uses, and that has no dirty, untracked or unmerged state.

Isolation covers only Cursor's *project* config scope. User-scope
(``~/.cursor``) and system hook/MCP config is still loaded by every
cursor-agent, so isolated dispatch is refused while that scope carries
session-bound Omnigent entries (see :func:`inherited_cursor_problems`). A repository
that TRACKS project ``.cursor/{mcp,hooks}.json`` gets them neutralised in
each worktree (session-bound entries stripped, ``skip-worktree`` set in
that worktree's own index; see :func:`neutralise_tracked_cursor`), so the
worker never honours another session's binding and the overwrite never
reaches the product diff or merge. Removal uses plain
``git worktree remove`` and ``git branch -d`` -- never ``--force``/``-D``
and never ``git worktree prune``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

LEDGER_DIR_NAME = "trio-worktrees"
OWNER_MARKER = "trio-worker-owner"
BRANCH_PREFIX = "trio-worker/"
WORKTREE_ROOT_ENV = "TRIO_WORKTREE_ROOT"
SCHEMA = 1

TERMINAL_STATES = ("removed",)
ACTIVE_STATES = ("created", "running")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: Retained reasons whose worktree holds output of a worker that did not
#: finish successfully; never integrated without an explicit override.
UNVERIFIED_OUTPUT_REASONS = ("worker_failed", "interrupted", "create_failed")
#: Hard ceiling for an isolated worker whose dispatcher gave no timeout.
DEFAULT_WORKER_MAX_SECONDS = 4 * 3600.0
WORKER_MAX_SECONDS_ENV = "TRIO_WORKER_MAX_SECONDS"
#: System-wide hook files cursor-agent reads (Linux, macOS).
SYSTEM_CURSOR_HOOKS = (
    Path("/etc/cursor/hooks.json"),
    Path("/Library/Application Support/Cursor/hooks.json"),
)


class WorktreeError(RuntimeError):
    """A worker worktree operation that could not proceed."""


# --------------------------------------------------------------------- git


def git(
    cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git with a fixed locale; raise WorktreeError when *check* fails."""
    full_env = dict(os.environ)
    full_env.update({"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=full_env,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def repo_toplevel(path: Path) -> Path:
    return Path(_out(path, "rev-parse", "--show-toplevel")).resolve()


def common_dir(repo: Path) -> Path:
    raw = _out(repo, "rev-parse", "--git-common-dir")
    path = Path(raw)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def rev(repo: Path, ref: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and _SHA.match(value) else None


def status_entries(path: Path, *, untracked: bool = True) -> list[str]:
    """Porcelain status lines (ignored files excluded)."""
    mode = "--untracked-files=all" if untracked else "--untracked-files=no"
    out = git(path, "status", "--porcelain=v1", mode).stdout
    return [line for line in out.splitlines() if line.strip()]


def _status_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def worktree_list(repo: Path) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain``."""
    out = git(repo, "worktree", "list", "--porcelain").stdout
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def cursor_project_root(workspace: Path) -> Path:
    """Nearest ancestor of *workspace* (inclusive) holding a ``.git`` entry.

    Mirrors Omnigent's ``cursor_native.bridge.cursor_project_root`` (the
    directory cursor-agent reads its project ``.cursor`` config from).
    """
    current = str(workspace)
    anchor = Path(current).anchor
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return Path(current)
        parent = os.path.dirname(current)
        if parent in (current, anchor):
            return workspace
        current = parent


# ------------------------------------------------------------------ ledger


def ledger_dir(repo: Path) -> Path:
    return common_dir(repo) / LEDGER_DIR_NAME


def default_worktree_root(
    repo: Path, *, name: str | None = None, base: Path | None = None
) -> Path:
    """``<base>/<name>-<sha256(git common dir)[:12]>`` for *repo*.

    *base* defaults to ``$TRIO_WORKTREE_ROOT``, else ``$XDG_STATE_HOME`` (or
    ``~/.local/state``) ``/trio-agent-loop/worktrees``; *name* to the repo
    directory's name. A declared PLAN.md ``repos:`` repo (r15) passes its
    declared name, and the parent of the home repo's root as *base*, so
    every repo of one loop gets its own sibling root.
    """
    if base is None:
        env = os.environ.get(WORKTREE_ROOT_ENV, "").strip()
        if env:
            base = Path(env).expanduser()
        else:
            state = os.environ.get("XDG_STATE_HOME", "").strip()
            base = (Path(state) if state else Path.home() / ".local" / "state")
            base = base / "trio-agent-loop" / "worktrees"
    key = hashlib.sha256(str(common_dir(repo)).encode()).hexdigest()[:12]
    return (Path(base) / f"{name or repo.name}-{key}").resolve()


def _record_path(repo: Path, worker_id: str) -> Path:
    if not worker_id or _SAFE.sub("", worker_id) != worker_id:
        raise WorktreeError(f"invalid worker id: {worker_id!r}")
    return ledger_dir(repo) / f"{worker_id}.json"


def load_record(repo: Path, worker_id: str) -> dict[str, Any]:
    path = _record_path(repo, worker_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorktreeError(f"unreadable worker record {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("id") != worker_id:
        raise WorktreeError(f"malformed worker record {path}")
    return data


def save_record(repo: Path, record: dict[str, Any]) -> None:
    """Atomic write (tmp + fsync + rename) so a crash never tears a record."""
    path = _record_path(repo, str(record["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = time.time()
    tmp = path.with_suffix(f".json.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def list_records(repo: Path) -> list[tuple[str, dict[str, Any] | None]]:
    """Every ledger entry as (id, record-or-None-if-unreadable)."""
    directory = ledger_dir(repo)
    if not directory.is_dir():
        return []
    rows: list[tuple[str, dict[str, Any] | None]] = []
    for path in sorted(directory.glob("*.json")):
        worker_id = path.stem
        try:
            rows.append((worker_id, load_record(repo, worker_id)))
        except WorktreeError:
            rows.append((worker_id, None))
    return rows


@contextlib.contextmanager
def repo_lock(repo: Path, name: str = "integrate") -> Iterator[None]:
    """Per-repository exclusive lock serializing integration and cleanup."""
    directory = ledger_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{name}.lock", "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _retain(repo: Path, record: dict[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
    record["state"] = "retained"
    record["retained_reason"] = reason
    record["retained_detail"] = detail
    save_record(repo, record)
    return record


# ------------------------------------------------------------ process info


def _proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 22 (starttime) after the parenthesized comm.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def process_identity(pid: int) -> dict[str, Any]:
    return {"pid": pid, "start": _proc_start(pid)}


def identity_alive(ident: dict[str, Any] | None) -> bool:
    if not isinstance(ident, dict):
        return False
    pid = ident.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    start = _proc_start(pid)
    return start is not None and start == ident.get("start")


def group_alive(pgid: int | None) -> bool:
    """Whether any process remains in process group *pgid*.

    Workers are launched as their own session/group leader, so children that
    outlive the ``cursor-agent`` wrapper stay discoverable by group.
    """
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[-1].split()
        # fields: state, ppid, pgrp, ... (zombies hold no cwd/files)
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            return True
    return False


def processes_using(path: Path, *, exclude: tuple[int, ...] = ()) -> list[int]:
    """PIDs whose cwd, root or any open fd lies inside *path*.

    Same-user /proc scan; processes whose links cannot be read are not
    reported (the caller's other checks stay conservative).
    """
    target = str(path.resolve())
    skip = {os.getpid(), *exclude}

    def inside(link: str) -> bool:
        return link == target or link.startswith(target + os.sep)

    users: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in skip:
            continue
        hit = False
        for name in ("cwd", "root"):
            try:
                if inside(os.readlink(entry / name)):
                    hit = True
                    break
            except OSError:
                continue
        if not hit:
            try:
                fds = list((entry / "fd").iterdir())
            except OSError:
                fds = []
            for fd in fds:
                try:
                    if inside(os.readlink(fd)):
                        hit = True
                        break
                except OSError:
                    continue
        if hit:
            users.append(int(entry.name))
    return users


def _list_processes() -> Iterator[tuple[int, str, bytes]]:
    """``(pid, cwd, cmdline)`` of every same-user process whose cwd is readable.

    Module-level seam: tests replace it to inject a process table.
    """
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
            cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        yield int(entry.name), cwd, cmd


def _is_cursor_agent(cmdline: bytes) -> bool:
    return b"cursor-agent" in cmdline.replace(b"\0", b" ")


def _loads_root_slot(cwd: str, target: str) -> bool:
    """Whether a cursor-agent with *cwd* reads the project config of *target*.

    cursor-agent's project root is the nearest ancestor of its physical cwd
    holding ``.git`` (:func:`cursor_project_root`), so a session started in
    a subdirectory of *target* loads *target*'s ``.cursor`` slot too, while
    one inside a nested checkout (a worktree or clone with its own ``.git``,
    e.g. a Trio-owned worktree placed under the root) has its own slot.
    """
    if cwd == target:
        return True  # the historical exact match
    if not cwd.startswith(target.rstrip(os.sep) + os.sep):
        return False
    return str(cursor_project_root(Path(cwd))) == target


def _proc_parent(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _proc_cmd(pid: int, limit: int = 160) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "?"
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or "?"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _proc_started(pid: int) -> str | None:
    """Wall-clock start time (local ``YYYY-mm-dd HH:MM:SS``) of *pid*, or None."""
    ticks = _proc_start(pid)
    if ticks is None:
        return None
    try:
        btime = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError):
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(btime + int(ticks) / hz))


def parent_chain(pid: int, depth: int = 3) -> list[dict[str, Any]]:
    """Up to *depth* ancestors of *pid*: ``[{pid, cmd}, ...]`` nearest first."""
    chain: list[dict[str, Any]] = []
    current = pid
    for _ in range(depth):
        parent = _proc_parent(current)
        if not parent or parent <= 1:
            break
        chain.append({"pid": parent, "cmd": _proc_cmd(parent, 80)})
        current = parent
    return chain


def process_detail(pid: int, cwd: str | None = None) -> dict[str, Any]:
    """pid, cwd, cmd (truncated), start time and parent chain of *pid*."""
    if cwd is None:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = "?"
    return {
        "pid": pid,
        "cwd": cwd,
        "cmd": _proc_cmd(pid),
        "started": _proc_started(pid),
        "parents": parent_chain(pid),
    }


def describe_process(detail: dict[str, Any]) -> str:
    """One line: ``cursor-agent pid N (cwd C, started T, parent P <- Q, cmd X)``."""
    parents = " <- ".join(
        f"{p.get('cmd')} (pid {p.get('pid')})" for p in detail.get("parents") or []
    ) or "?"
    return (
        f"cursor-agent pid {detail.get('pid')} (cwd {detail.get('cwd')}, "
        f"started {detail.get('started') or '?'}, parent {parents}, "
        f"cmd {detail.get('cmd')})"
    )


def cursor_processes_detail(root: Path) -> list[dict[str, Any]]:
    """Live cursor-agent processes that load *root*'s project ``.cursor`` config.

    Matches a cwd equal to *root* or inside it whose nearest ``.git``
    ancestor is *root* (r15.x: a session started in a subdirectory loads
    the root slot too; nested worktrees/clones are excluded). Each entry is
    :func:`process_detail`.
    """
    target = str(root.resolve())
    found: list[dict[str, Any]] = []
    for pid, cwd, cmd in _list_processes():
        if pid == os.getpid() or not _is_cursor_agent(cmd):
            continue
        if _loads_root_slot(cwd, target):
            found.append(process_detail(pid, cwd))
    return found


def cursor_processes_at(root: Path) -> list[int]:
    """Pids of :func:`cursor_processes_detail` (the historical list form)."""
    return [entry["pid"] for entry in cursor_processes_detail(root)]


def cursor_agent_ancestor(root: Path, pid: int | None = None, depth: int = 16) -> int | None:
    """The nearest ancestor of *pid* (default: this process) that is a
    cursor-agent loading *root*'s project slot, or None.

    A headless one-shot started from inside a root session (the Lead's own
    scouts/builders) is part of that session's root turn.
    """
    target = str(root.resolve())
    current = os.getpid() if pid is None else pid
    for _ in range(depth):
        parent = _proc_parent(current)
        if not parent or parent <= 1:
            return None
        try:
            cmd = Path(f"/proc/{parent}/cmdline").read_bytes()
            cwd = os.readlink(f"/proc/{parent}/cwd")
        except OSError:
            cmd, cwd = b"", ""
        if cmd and _is_cursor_agent(cmd) and _loads_root_slot(cwd, target):
            return parent
        current = parent
    return None


# ------------------------------------------------- owned generated content

#: Project Cursor config files an Omnigent cursor-native launch generates.
OWNED_CURSOR_FILES = (".cursor/mcp.json", ".cursor/hooks.json")
#: Ignored, regenerable caches that may be discarded with a worktree.
DISPOSABLE_IGNORED = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_USAGE_HOOK_ARGS = ("-I", "-m", "omnigent.harnesses.cursor_native.usage", "record-usage", "--bridge-dir")


def _owned_mcp(data: object) -> bool:
    if not isinstance(data, dict) or set(data) != {"mcpServers"}:
        return False
    servers = data["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != {"omnigent"}:
        return False
    args = servers["omnigent"].get("args") if isinstance(servers["omnigent"], dict) else None
    return isinstance(args, list) and "serve-mcp" in args and "--bridge-dir" in args


def _owned_hooks(data: object) -> bool:
    import shlex

    if not isinstance(data, dict) or not set(data) <= {"version", "hooks"}:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or set(hooks) != {"stop"}:
        return False
    entries = hooks["stop"]
    if not isinstance(entries, list) or not entries:
        return False
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"command"}:
            return False
        try:
            argv = shlex.split(str(entry["command"]))
        except ValueError:
            return False
        if len(argv) != 7 or tuple(argv[1:6]) != _USAGE_HOOK_ARGS:
            return False
    return True


def owned_residue(path: Path, rel: str) -> bool:
    """Whether untracked *rel* in *path* is exactly Omnigent-generated config.

    Anything else -- a symlink, extra servers/hooks, unreadable JSON --
    is user content and is never treated as disposable.
    """
    if rel not in OWNED_CURSOR_FILES:
        return False
    target = path / rel
    if target.is_symlink() or (path / ".cursor").is_symlink() or not target.is_file():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return _owned_mcp(data) if rel.endswith("mcp.json") else _owned_hooks(data)


def _split_status(path: Path, entries: list[str]) -> tuple[list[str], list[str]]:
    """(user entries, owned residue paths) from porcelain status lines."""
    user, residue = [], []
    for line in entries:
        rel = _status_path(line)
        if line.startswith("??") and owned_residue(path, rel):
            residue.append(rel)
        else:
            user.append(line)
    return user, residue


def ignored_entries(path: Path) -> list[str]:
    """Ignored paths ``git worktree remove`` would silently delete."""
    out = git(path, "status", "--porcelain=v1", "--ignored=matching", "--untracked-files=all").stdout
    return [_status_path(line) for line in out.splitlines() if line.startswith("!!")]


def _owned_ignored_cursor(path: Path, rel: str) -> list[str] | None:
    """Owned residue paths if ignored *rel* is only Omnigent's generated config.

    Repositories that gitignore ``.cursor/`` report the whole directory as
    one ignored entry; it is disposable only when every file in it is the
    exact generated config (:func:`owned_residue`).
    """
    rel = rel.rstrip("/")
    if rel in OWNED_CURSOR_FILES:
        return [rel] if owned_residue(path, rel) else None
    if rel != ".cursor":
        return None
    base = path / ".cursor"
    if base.is_symlink() or not base.is_dir():
        return None
    files = []
    for item in base.rglob("*"):
        if item.is_dir() and not item.is_symlink():
            continue
        files.append(item.relative_to(path).as_posix())
    if not files or not all(f in OWNED_CURSOR_FILES and owned_residue(path, f) for f in files):
        return None
    return files


def _disposable_ignored(rel: str) -> bool:
    parts = rel.rstrip("/").split("/")
    return any(part in DISPOSABLE_IGNORED for part in parts) or rel.endswith((".pyc", ".pyo"))


#: Ignored directories whose content is rebuildable from tracked inputs
#: (lockfiles, sources); an accepted/finished, owner-verified task worktree
#: may be retired with them (see :func:`_blocking_state`).
REBUILDABLE_IGNORED_DIRS = (
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".next", ".turbo", ".parcel-cache", ".cache", "coverage",
)
#: Build-output directories that are rebuildable only when the repository's
#: own ``.gitignore`` names them (a global/info exclude does not count).
REPO_IGNORED_BUILD_DIRS = ("dist", "build")
#: Ignored directory-name suffixes that are rebuildable at any depth
#: (``src/<pkg>.egg-info/`` from a ``pip install -e``).
REBUILDABLE_IGNORED_DIR_SUFFIXES = (".egg-info",)
#: Ignored file suffixes that are rebuildable build/bytecode artifacts.
REBUILDABLE_IGNORED_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")


def _empty_ignored_dir(path: Path, rel: str) -> bool:
    """Whether ignored *rel* is a real directory holding no file or symlink."""
    target = path / rel.rstrip("/")
    if target.is_symlink() or not target.is_dir():
        return False
    for current, dirs, files in os.walk(target):
        if files or any(Path(current, d).is_symlink() for d in dirs):
            return False
    return True


def _repo_gitignore_lists(path: Path, dir_rel: str) -> bool:
    """Whether a ``.gitignore`` inside worktree *path* names directory *dir_rel*.

    ``git check-ignore -v`` must attribute the match to a tracked-tree
    ``.gitignore`` (relative source, not ``.git/info/exclude`` nor the global
    ``core.excludesFile``) whose non-negated, wildcard-free pattern ends in
    exactly that directory name.
    """
    proc = git(path, "check-ignore", "-v", "--", dir_rel, check=False)
    line = proc.stdout.splitlines()[0] if proc.returncode == 0 and proc.stdout else ""
    match = re.match(r"^(.*?):(\d+):(.*)\t", line)
    if match is None:
        return False
    source, pattern = match.group(1), match.group(3)
    src = Path(source)
    if src.is_absolute() or src.name != ".gitignore" or ".git" in src.parts[:-1]:
        return False
    try:
        (path / src).resolve().relative_to(path.resolve())
    except ValueError:
        return False
    if pattern.startswith("!") or any(c in pattern for c in "*?[\\"):
        return False
    return pattern.strip("/").split("/")[-1] == dir_rel.rstrip("/").split("/")[-1]


def _rebuildable_ignored(path: Path, rel: str) -> bool:
    """Whether ignored *rel* is an empty dir or a rebuildable artifact."""
    if rel.endswith(REBUILDABLE_IGNORED_SUFFIXES) or _empty_ignored_dir(path, rel):
        return True
    parts = rel.rstrip("/").split("/")
    is_dir = rel.endswith("/") or (
        (path / rel).is_dir() and not (path / rel.rstrip("/")).is_symlink()
    )
    dir_parts = parts if is_dir else parts[:-1]
    for index, part in enumerate(dir_parts):
        if part in REBUILDABLE_IGNORED_DIRS or part.endswith(REBUILDABLE_IGNORED_DIR_SUFFIXES):
            return True
        if part in REPO_IGNORED_BUILD_DIRS and _repo_gitignore_lists(
            path, "/".join(parts[: index + 1])
        ):
            return True
    return False


# ------------------------------------------ root Cursor config provenance
#
# A root-bound cursor-native session (Lead, integration evaluator) makes
# Omnigent merge its own MCP server and usage ``stop`` hook into the
# aggregate root's ``.cursor/{mcp,hooks}.json``; nothing removes them when
# the session ends. Left behind they are untracked (or modified tracked)
# product paths, so a SHIP of that exact tree can never be accepted.
#
# trioctl records the pre-launch bytes once per aggregate root
# (``baseline``, in the git common dir, mode 0600 -- never in a worktree,
# never committed). Every record is keyed by and bound to the canonical
# root path, so the main checkout and each linked worktree of one
# repository (which share the common dir) never read, restore or delete
# each other's state. After the caller's own root sessions have ended it
# strips only entries bound to one of the bridges the caller passes
# (``sha256(session_id)[:32]``, the bridge dir basename; held and
# in-flight sessions are the caller's to exclude). The baseline is
# restored only when what remains equals exactly what Omnigent's merge
# left of it, through an atomic exchange that keeps any competing write.
# Any other difference (a user or concurrent edit, a foreign or held
# session's entry) is left in place and the product check keeps failing
# closed. trioctl never ignores or excludes anything; the loop core only
# exempts an untracked file whose content is EXACTLY the generated config
# (:func:`owned_residue`), never a user-edited or tracked one.

ROOT_CURSOR_DIR_NAME = "root-cursor"
_USAGE_HOOK_MODULE = "omnigent.harnesses.cursor_native.usage"
#: Unkeyed files an earlier (d39bfd9) layout shared by every root.
_UNKEYED_ROOT_FILES = ("baseline.json", "sessions.json", "mcp.json.orig", "hooks.json.orig")


def bridge_key(session_id: str) -> str:
    """Bridge dir basename Omnigent derives from a broker session id."""
    return hashlib.sha256(str(session_id).encode()).hexdigest()[:32]


def _root_cursor_dir(repo: Path) -> Path:
    return ledger_dir(repo) / ROOT_CURSOR_DIR_NAME


def _root_identity(repo: Path) -> tuple[str, str]:
    """(record key, canonical path) of the aggregate root holding *repo*.

    The canonical path is the resolved worktree top level, so the main
    checkout and every linked worktree of one repository differ.
    """
    root = str(repo_toplevel(repo))
    return hashlib.sha256(root.encode()).hexdigest()[:16], root


def _root_file(repo: Path, name: str) -> Path:
    key, _root = _root_identity(repo)
    return _root_cursor_dir(repo) / f"{key}.{name}"


def _check_root(path: Path, data: object, repo: Path) -> dict[str, Any]:
    _key, root = _root_identity(repo)
    if not isinstance(data, dict):
        raise WorktreeError(f"malformed root Cursor record {path}")
    if data.get("root") != root:
        raise WorktreeError(
            f"root Cursor record {path} belongs to {data.get('root')!r}, not {root!r}; "
            "refusing to use it"
        )
    return data


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2


def _renameat2(src: Path, dst: Path, flags: int) -> None:
    """Linux ``renameat2``; raises OSError (ENOSYS when unavailable)."""
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    if fn(_AT_FDCWD, os.fsencode(str(src)), _AT_FDCWD, os.fsencode(str(dst)), flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))


def _swap_in(target: Path, expected: bytes, source: bytes | None, mode: int) -> str | None:
    """Put *source* at *target* (None removes it) only if it still holds *expected*.

    Atomic and conflict-safe: the new content is exchanged in (or the file
    moved aside) in one ``renameat2``, then the displaced content is
    compared with *expected*. A write that landed first is exchanged back
    and stays live; a write racing the swap-back is kept beside the target
    (``.<name>.trio-restore-*``) instead of being lost. Returns None on
    success, else why the file was left in place.
    """
    rel = f"{target.parent.name}/{target.name}"
    side = target.with_name(f".{target.name}.trio-restore-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    if source is None:
        try:
            _renameat2(target, side, _RENAME_NOREPLACE)
        except FileNotFoundError:
            return f"{rel} vanished during restore; nothing removed"
        except OSError as exc:
            return f"{rel}: atomic move unavailable ({exc.strerror}); left in place"
        if _read_bytes(side) == expected:
            side.unlink()
            return None
        try:
            _renameat2(side, target, _RENAME_NOREPLACE)
        except FileExistsError:
            return (f"{rel} changed during restore and was recreated; the competing "
                    f"content is kept at {side.name}")
        return f"{rel} changed during restore; competing edit kept; left in place"
    fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(source)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(side, mode)
        _renameat2(side, target, _RENAME_EXCHANGE)
    except FileNotFoundError:
        side.unlink(missing_ok=True)
        return f"{rel} was removed during restore; not recreated"
    except OSError as exc:
        side.unlink(missing_ok=True)
        return f"{rel}: atomic exchange unavailable ({exc.strerror}); left in place"
    if _read_bytes(side) == expected:
        side.unlink()
        return None
    _renameat2(side, target, _RENAME_EXCHANGE)  # competing content back in place
    if _read_bytes(side) == source:
        side.unlink()
        return f"{rel} changed during restore; competing edit kept; left in place"
    return (f"{rel} changed twice during restore; the content written in between "
            f"is kept at {side.name}")


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _bridge_arg(argv: object) -> str | None:
    """Basename of the ``--bridge-dir`` value in an argv list, if exactly one."""
    if not isinstance(argv, list):
        return None
    values = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--bridge-dir"]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return Path(values[0]).name


def _usage_hook_bridge(entry: object) -> str | None:
    import shlex

    if not isinstance(entry, dict):
        return None
    try:
        argv = shlex.split(str(entry.get("command", "")))
    except ValueError:
        return None
    if _USAGE_HOOK_MODULE not in argv:
        return None
    return _bridge_arg(argv)


def _strip_owned(rel: str, data: object, keys: set[str]) -> object:
    """*data* minus the Omnigent entries bound to one of *keys* (a copy)."""
    data = json.loads(json.dumps(data))
    if not isinstance(data, dict):
        return data
    if rel.endswith("mcp.json"):
        servers = data.get("mcpServers")
        entry = servers.get("omnigent") if isinstance(servers, dict) else None
        if isinstance(entry, dict) and _bridge_arg(entry.get("args")) in keys:
            del servers["omnigent"]
        return data
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            if isinstance(entries, list):
                hooks[event] = [e for e in entries if _usage_hook_bridge(e) not in keys]
    return data


def _omnigent_base(rel: str, source: bytes | None) -> object:
    """What Omnigent's merge leaves of *source* before adding its own entry.

    Mirrors ``write_mcp_config`` / ``write_hooks_config`` (Omnigent
    2a84483a): a missing or non-dict file becomes ``{}``; mcp gets a dict
    ``mcpServers`` whose ``omnigent`` key is replaced; hooks get a dict
    ``hooks``, ``version`` defaulting to 1, and every usage hook dropped
    from ``stop`` before Omnigent appends its own.
    """
    data: object = None
    if source is not None:
        with contextlib.suppress(ValueError):
            data = json.loads(source.decode("utf-8"))
    base: dict[str, Any] = data if isinstance(data, dict) else {}
    if rel.endswith("mcp.json"):
        servers = base.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers.pop("omnigent", None)
        base["mcpServers"] = servers
        return base
    hooks = base.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    base["hooks"] = hooks
    base.setdefault("version", 1)
    stop = hooks.get("stop")
    stop = stop if isinstance(stop, list) else []
    hooks["stop"] = [
        e for e in stop
        if not (isinstance(e, dict) and _USAGE_HOOK_MODULE in str(e.get("command", "")))
    ]
    return base


def _index_entry(repo: Path, rel: str) -> tuple[bytes, int] | None:
    """(blob, file mode) of *rel* in the index, or None when untracked.

    Raises WorktreeError for an index entry that is not a regular file.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "--", rel], capture_output=True, text=True,
    )
    line = proc.stdout.strip()
    if proc.returncode != 0 or not line:
        return None
    mode = line.split()[0]
    if mode not in ("100644", "100755"):
        raise WorktreeError(f"{rel} is tracked as mode {mode}, not a regular file")
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f":{rel}"], capture_output=True,
    )
    if blob.returncode != 0:
        raise WorktreeError(f"cannot read the index version of {rel}")
    return blob.stdout, (0o755 if mode == "100755" else 0o644)


def _root_baseline(repo: Path) -> dict[str, Any] | None:
    path = _root_file(repo, "baseline.json")
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise WorktreeError(f"unreadable root Cursor baseline {path}: {exc}") from exc
    data = _check_root(path, data, repo)
    if not isinstance(data.get("files"), dict):
        raise WorktreeError(f"malformed root Cursor baseline {path}")
    return data


def root_owned_sessions(repo: Path) -> dict[str, str | None]:
    """Root sessions trioctl recorded as launched at THIS root: id -> mailbox.

    Only evidence: callers decide ownership (held and in-flight sessions
    must be excluded before anything is passed to restore).
    """
    path = _root_file(repo, "sessions.json")
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise WorktreeError(f"unreadable root session record {path}: {exc}") from exc
    data = _check_root(path, data, repo)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        raise WorktreeError(f"malformed root session record {path}")
    return {str(k): (str(v) if v else None) for k, v in sessions.items()}


def record_root_session(repo: Path, session_id: str, mailbox: Path | None = None) -> None:
    """Persist that trioctl launched *session_id* bound to this root."""
    _key, root = _root_identity(repo)
    with repo_lock(repo, "root-cursor"):
        sessions = root_owned_sessions(repo)
        sessions[str(session_id)] = str(mailbox) if mailbox else sessions.get(str(session_id))
        _private_write(
            _root_file(repo, "sessions.json"),
            (json.dumps({"schema": 2, "root": root, "sessions": sessions},
                        indent=2, sort_keys=True) + "\n").encode(),
        )


def snapshot_root_cursor(repo: Path) -> bool:
    """Record this root's pre-launch ``.cursor`` config once; True if taken now.

    An existing baseline of this root (a crashed earlier run) is kept: it
    is the only record of the state before that run's sessions merged into
    the files. Other roots' baselines are never consulted.
    """
    _key, root = _root_identity(repo)
    with repo_lock(repo, "root-cursor"):
        if _root_baseline(repo) is not None:
            return False
        files: dict[str, Any] = {}
        for rel in OWNED_CURSOR_FILES:
            target = repo / rel
            name = Path(rel).name
            if target.is_symlink() or (repo / ".cursor").is_symlink():
                files[rel] = {"state": "symlink"}
                continue
            data = _read_bytes(target)
            if data is None:
                files[rel] = {"state": "absent"}
                continue
            _private_write(_root_file(repo, f"{name}.orig"), data)
            files[rel] = {
                "state": "file",
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": target.stat().st_mode & 0o7777,
            }
        _private_write(
            _root_file(repo, "baseline.json"),
            (json.dumps({"schema": 2, "root": root, "taken_at": time.time(), "files": files},
                        indent=2, sort_keys=True) + "\n").encode(),
        )
        return True


def restore_root_cursor(
    repo: Path, owned_session_ids: set[str] | list[str], *, final: bool = False
) -> list[str]:
    """Undo only the given sessions' Omnigent merges into THIS root's ``.cursor`` config.

    *owned_session_ids* is the complete owned set: the caller has already
    excluded held and in-flight sessions; nothing recorded is added back.
    Call only once those sessions have ended (no cursor-agent at the
    root). Returns the reasons a file was left in place; ``[]`` means both
    files are back to this root's baseline (or, with no baseline, to the
    index version and mode when tracked and absent when untracked). With
    *final* and nothing left in place, only this root's records are
    dropped; a problem keeps them as lifecycle evidence.
    """
    keys = {bridge_key(s) for s in owned_session_ids}
    problems: list[str] = []
    directory = _root_cursor_dir(repo)
    for name in _UNKEYED_ROOT_FILES:
        if (directory / name).exists():
            problems.append(
                f"unattributed root Cursor record {directory / name} (older layout shared by "
                "every root of this repository) is neither used nor deleted; once no trioctl "
                "loop runs on any root of this repository, check which root it belonged to "
                "and remove it by hand"
            )
    with repo_lock(repo, "root-cursor"):
        baseline = _root_baseline(repo)
        hint = ""
        if baseline is not None:
            taken = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(baseline.get("taken_at", 0))))
            hint = (
                f" (baseline of this root taken {taken}: {_root_file(repo, 'baseline.json')}; "
                "if no trioctl loop runs on this root and the current file is intended, "
                "remove that baseline and its .orig copies to retire it)"
            )
        for rel in OWNED_CURSOR_FILES:
            target = repo / rel
            if target.is_symlink() or (repo / ".cursor").is_symlink():
                problems.append(f"{rel} is a symlink; left in place")
                continue
            entry = (baseline or {}).get("files", {}).get(rel) if baseline else None
            if baseline is not None and entry is None:
                problems.append(f"{rel}: baseline has no entry; left in place")
                continue
            if entry is not None and entry.get("state") == "symlink":
                problems.append(f"{rel} was a symlink before the run; left in place")
                continue
            if entry is None:  # no baseline: the committed state (and mode) is the source
                try:
                    indexed = _index_entry(repo, rel)
                except WorktreeError as exc:
                    problems.append(f"{exc}; left in place")
                    continue
                source, mode = indexed if indexed else (None, 0o644)
            elif entry.get("state") == "absent":
                source, mode = None, 0o644
            else:
                source = _read_bytes(_root_file(repo, f"{Path(rel).name}.orig"))
                if source is None or hashlib.sha256(source).hexdigest() != entry.get("sha256"):
                    problems.append(f"{rel}: baseline copy missing or altered; left in place")
                    continue
                mode = int(entry.get("mode", 0o644))
            current = _read_bytes(target)
            if current == source:
                if source is not None and target.stat().st_mode & 0o7777 != mode:
                    os.chmod(target, mode)
                continue
            if current is None:
                problems.append(f"{rel} was removed during the run; not recreated")
                continue
            try:
                parsed = json.loads(current.decode("utf-8"))
            except ValueError:
                problems.append(f"{rel} is not JSON; left in place")
                continue
            if _strip_owned(rel, parsed, keys) != _omnigent_base(rel, source):
                problems.append(
                    f"{rel} differs from the pre-run config beyond the owned sessions' "
                    f"Omnigent entries (user, concurrent, held or foreign edit); left in place{hint}"
                )
                continue
            problem = _swap_in(target, current, source, mode)
            if problem:
                problems.append(problem)
        if final and not problems and baseline is not None:
            key, _root = _root_identity(repo)
            for item in directory.glob(f"{key}.*"):
                item.unlink()
            with contextlib.suppress(OSError):
                directory.rmdir()  # only when no other root has records left
    return problems


# ---------------------------------------------------------------- creation


def _sanitize(value: str) -> str:
    return _SAFE.sub("-", value).strip("-")[:40] or "worker"


def _mailbox_rel(repo: Path, mailbox: Path | None) -> str | None:
    if mailbox is None:
        return None
    try:
        rel = mailbox.resolve().relative_to(repo)
    except ValueError:
        return None
    return rel.as_posix().rstrip("/") + "/"


_WRITES_KEY = re.compile(r"^\s*(?:-\s+)?writes\s*:\s*(.*)$")
_BLOCK_ITEM = re.compile(r"^\s*-\s+(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def declared_product_paths(mailbox: Path | None) -> list[str]:
    """Every path in any PLAN.md slice's ``writes:`` (``api:`` entries
    dropped; empty when unknown).

    Lenient and stdlib-only (this module ships next to trioctl without the
    vendored metrics parser): flow ``writes: [a, "b"]`` and block-style
    ``writes:`` + ``- item`` lines are both read.
    """
    if mailbox is None:
        return []
    try:
        lines = (Path(mailbox) / "PLAN.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    paths: list[str] = []
    block_indent: int | None = None
    for line in lines:
        if block_indent is not None:
            item = _BLOCK_ITEM.match(line)
            indent = len(line) - len(line.lstrip())
            if item and indent >= block_indent:
                paths.append(_unquote(item.group(1)))
                continue
            block_indent = None
        match = _WRITES_KEY.match(line)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            block_indent = len(line) - len(line.lstrip())
            continue
        if value.startswith("[") and value.endswith("]"):
            paths.extend(_unquote(v) for v in value[1:-1].split(",") if v.strip())
        else:
            paths.append(_unquote(value))
    declared = []
    for path in paths:
        path = path.strip()
        if not path or path.startswith("api:"):
            continue
        if path.startswith("./"):
            path = path[2:]
        declared.append(path.rstrip("/"))
    return declared


def _covered(path: str, declared: list[str]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in declared if d)


def _foreign_mailbox(repo: Path, path: str) -> str | None:
    """The repo-relative directory of another Trio mailbox containing *path*
    (a directory holding GOAL.md and STATE.md), or None."""
    parts = Path(path).parts
    for depth in range(1, len(parts) + 1):
        candidate = repo.joinpath(*parts[:depth])
        if (
            candidate.is_dir()
            and (candidate / "GOAL.md").is_file()
            and (candidate / "STATE.md").is_file()
        ):
            return Path(*parts[:depth]).as_posix() + "/"
    return None


def classify_aggregate(
    repo: Path, mailbox: Path | None, declared: list[str] | None = None
) -> dict[str, list[str]]:
    """Classify aggregate status entries outside this mailbox.

    - ``product``: modified or untracked files under a declared product path
      (any PLAN.md slice's ``writes:``) -- a worker would not see them.
    - ``foreign``: modified TRACKED files outside the product paths -- a
      worker would merge onto a tree that differs from what the user sees.
    - ``ignored``: other untracked files, and anything inside another Trio
      mailbox (GOAL.md + STATE.md). A worktree never carries untracked
      files, so they cannot affect a worker; they are only reported.

    With no declared product paths (no mailbox / no parsable PLAN.md
    ``writes:``), untracked files outside other mailboxes stay ``product``
    blockers (the conservative pre-r11 behaviour). Entries are porcelain
    lines; ``ignored`` holds a mailbox directory once, else the path.
    A tracked ``.cursor/{mcp,hooks}.json`` modified only by session-bound
    Omnigent entries (the root Lead launch's merge,
    :func:`omnigent_only_change`) is ``ignored`` too, never a blocker.

    *declared* (r15) overrides the PLAN.md-derived product paths: for a
    declared ``repos:`` repo the caller passes the ``writes:`` of that
    repo's slices only (relative to its root).
    """
    rel = _mailbox_rel(repo, mailbox)
    if declared is None:
        declared = declared_product_paths(mailbox)
    out: dict[str, list[str]] = {"product": [], "foreign": [], "ignored": []}
    user, _residue = _split_status(repo, status_entries(repo))
    for line in user:
        path = _status_path(line)
        if rel and (path.startswith(rel) or path + "/" == rel):
            continue
        untracked = line.startswith("??")
        if line[:2] == " M" and omnigent_only_change(repo, path):
            out["ignored"].append(path)
            continue
        if _covered(path, declared):
            out["product"].append(line)
            continue
        other = _foreign_mailbox(repo, path)
        if other:
            if other not in out["ignored"]:
                out["ignored"].append(other)
            continue
        if untracked:
            if declared:
                out["ignored"].append(path)
            else:
                out["product"].append(line)
            continue
        out["foreign"].append(line)
    return out


def aggregate_blockers(
    repo: Path, mailbox: Path | None, declared: list[str] | None = None
) -> list[str]:
    """Aggregate status entries that block an isolated dispatch/integration.

    A worker branches from committed HEAD, so uncommitted product edits in
    the aggregate would be invisible to it (breaking declared ``reads:``)
    and modified tracked files could collide with the integration merge.
    Untracked non-product files and other mailboxes do not block
    (:func:`classify_aggregate`).
    """
    found = classify_aggregate(repo, mailbox, declared)
    return found["product"] + found["foreign"]


def aggregate_refusal(found: dict[str, list[str]]) -> str | None:
    """The isolated-dispatch refusal for *found* blockers, or None."""
    parts = []
    if found["product"]:
        parts.append(
            "aggregate has uncommitted product changes (under PLAN.md "
            "writes:, or untracked with no writes: declared) a worker would "
            "not see; commit them before an isolated dispatch: " + "; ".join(found["product"][:5])
        )
    if found["foreign"]:
        files = ", ".join(_status_path(line) for line in found["foreign"][:5])
        parts.append(
            "aggregate has modified tracked files outside every declared "
            f"product path: commit or stash YOUR change to {files}; the Lead "
            "must not commit files it did not edit"
        )
    return "; ".join(parts) if parts else None


def ignored_note(found: dict[str, list[str]]) -> str | None:
    """One stderr line listing untracked/other-mailbox entries, or None."""
    if not found["ignored"]:
        return None
    return (
        "trioctl: ignored (not product paths; the worker worktree will not "
        "see them): " + ", ".join(found["ignored"][:10])
        + (f" (+{len(found['ignored']) - 10} more)" if len(found["ignored"]) > 10 else "")
    )


def create(
    repo: Path,
    *,
    slice_id: str,
    mailbox: Path | None = None,
    root: Path | None = None,
    role: str = "builder",
    run_id: str | None = None,
    detach_at: str | None = None,
    home: Path | None = None,
    repo_name: str | None = None,
    base_branch: str | None = None,
    declared: list[str] | None = None,
) -> dict[str, Any]:
    """Create one task-owned worktree.

    Builders get a fresh ``trio-worker/<id>`` branch at aggregate HEAD.
    With *detach_at* (an Evaluator grading a pinned sha) the worktree is
    detached at that commit, carries no branch and is never integrated.

    r15 multi-repo: *repo* may be a declared PLAN.md ``repos:`` repo (not
    the mailbox repo); *repo_name* records its declared name in the ledger
    (``repo_name``; absent for the mailbox repo, so single-repo records are
    unchanged), *base_branch* (its ``base:``) must be the branch its
    checkout is on -- builders branch from it and merge back onto it --
    and *declared* are that repo's slices' ``writes:`` (the product paths
    of :func:`classify_aggregate`, also kept for :func:`integrate`).
    """
    repo = repo_toplevel(repo)
    branch_ref = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if not branch_ref.startswith("refs/heads/"):
        raise WorktreeError(
            (f"repo {repo_name} ({repo})" if repo_name else "aggregate repository")
            + " is not on a branch (detached HEAD)"
        )
    if base_branch and branch_ref != f"refs/heads/{base_branch}":
        raise WorktreeError(
            f"repo {repo_name or repo.name} ({repo}) is on "
            f"{branch_ref[len('refs/heads/'):]}, not its PLAN.md repos: base "
            f"{base_branch!r}; check out {base_branch} there first (builders "
            "branch from it and merge back onto it)"
        )
    base = rev(repo, detach_at or "HEAD")
    if base is None:
        raise WorktreeError(f"no commit to create the worktree at: {detach_at or 'HEAD'}")
    inherited = inherited_cursor_problems(home=home)
    if inherited:
        raise WorktreeError(
            "refusing isolated dispatch: Cursor loads user/system config into every "
            "worktree and it carries session-bound Omnigent bindings (tools/receipts "
            "would route to a foreign session). Remove them from your own Cursor "
            "config first; trioctl never edits it: " + "; ".join(inherited)
        )
    if not detach_at:
        found = classify_aggregate(repo, mailbox, declared)
        refusal = aggregate_refusal(found)
        if refusal:
            raise WorktreeError(refusal)
        note = ignored_note(found)
        if note:
            print(note, file=sys.stderr)
    worker_id = f"{_sanitize(slice_id)}-{uuid.uuid4().hex[:8]}"
    root = (root or default_worktree_root(repo)).expanduser().resolve()
    path = root / worker_id
    if root == repo or str(root).startswith(str(repo) + os.sep):
        raise WorktreeError(
            f"worktree root {root} is inside the aggregate repository; "
            "choose a root outside it"
        )
    branch = None if detach_at else f"{BRANCH_PREFIX}{worker_id}"
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "id": worker_id,
        "slice": slice_id,
        "role": role,
        "run_id": run_id,
        "repo": str(repo),
        "common_dir": str(common_dir(repo)),
        "mailbox": str(mailbox.resolve()) if mailbox else None,
        "path": str(path),
        "branch": branch,
        "aggregate_ref": branch_ref,
        "base": base,
        "kind": "eval" if detach_at else "worker",
        "state": "created",
        **({"repo_name": repo_name} if repo_name and repo_name != "home" else {}),
        **({"declared_writes": list(declared)} if declared is not None else {}),
        "created_at": time.time(),
        "creator": process_identity(os.getpid()),
    }
    # Record first: a crash after `worktree add` still leaves an owned,
    # discoverable entry instead of an orphan nobody may remove.
    save_record(repo, record)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if branch:
            git(repo, "worktree", "add", "-b", branch, str(path), base)
        else:
            git(repo, "worktree", "add", "--detach", str(path), base)
    except (OSError, WorktreeError) as exc:
        _retain(repo, record, "create_failed", str(exc))
        raise WorktreeError(f"cannot create worker worktree: {exc}") from exc
    admin = Path(_out(path, "rev-parse", "--absolute-git-dir"))
    (admin / OWNER_MARKER).write_text(worker_id + "\n", encoding="utf-8")
    record["admin_dir"] = str(admin)
    try:
        neutralised = neutralise_tracked_cursor(path)
    except (OSError, WorktreeError) as exc:
        neutralised = []
        problems = [f"cannot neutralise tracked project Cursor config: {exc}"]
    else:
        problems = []
    if neutralised:
        record["neutralised_cursor"] = neutralised
    problems += cursor_config_conflicts(path)
    if cursor_project_root(path) != path:
        problems.append(f"worktree {path} is not its own Cursor project root")
    if problems:
        _retain(repo, record, "unsafe_cursor_config", "; ".join(problems))
        raise WorktreeError("; ".join(problems))
    save_record(repo, record)
    return record


#: Literal markers of an Omnigent session binding. Detection is bounded to
#: these markers: a binding hidden behind an arbitrary wrapper script with
#: none of them in its config is NOT detectable here (documented limit).
_SESSION_MARKERS = ("--bridge-dir", "serve-mcp", "omnigent", "cursor-native", "record-usage")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (str(k), *_strings(v))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _marked(value: object) -> bool:
    text = " ".join(_strings(value)).lower()
    return any(marker in text for marker in _SESSION_MARKERS)


def _session_bound_mcp_servers(path: Path) -> list[str]:
    """Session-bound MCP servers in one config file (any name, url or env)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [f"{path} is unreadable or not JSON (cannot prove it is safe)"]
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if servers is None and isinstance(data, dict):
        servers = data.get("servers")  # alternate plugin schema
    problems = []
    for name, spec in (servers or {}).items() if isinstance(servers, dict) else []:
        if name == "omnigent" or _marked(spec):
            problems.append(f"{path} declares session-bound Omnigent MCP server {name!r}")
    return problems


def _omnigent_hooks(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [f"{path} is unreadable or not JSON (cannot prove it is safe)"]
    if _marked(data):
        return [f"{path} carries an Omnigent session hook"]
    return []


def _plugin_sources(plugins: Path) -> tuple[list[Path], list[Path], list[str]]:
    """(mcp files, hook files, unresolved problems) contributed by plugins.

    Mirrors the installed cursor-agent bundle's plugin loading: an MCP file
    (``mcp.json`` / ``.mcp.json``) and hooks from ``hooks/hooks.json`` or a
    manifest ``hooks`` path. A manifest hook source that cannot be resolved
    to a readable file is reported (fail closed).
    """
    mcp, hooks, problems = [], [], []
    if not plugins.is_dir():
        return mcp, hooks, problems
    for path in plugins.rglob("*"):
        if path.is_symlink() and path.is_dir():
            problems.append(f"{path} is a symlinked plugin directory (cannot prove it is safe)")
            continue
        if not path.is_file():
            continue
        if path.name in ("mcp.json", ".mcp.json"):
            mcp.append(path)
        elif path.name == "hooks.json":
            hooks.append(path)
        elif path.name == "plugin.json":
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                problems.append(f"{path} is an unreadable plugin manifest (cannot prove it is safe)")
                continue
            if not isinstance(manifest, dict):
                continue
            for key in ("hooks", "mcpServers"):
                ref = manifest.get(key)
                if isinstance(ref, dict) or isinstance(ref, list):
                    if _marked(ref):
                        problems.append(f"{path} declares session-bound Omnigent {key}")
                elif isinstance(ref, str):
                    plugin_root = path.parent.parent if path.parent.name.startswith(".") else path.parent
                    target = (plugin_root / ref.lstrip("./")).resolve()
                    if target.is_dir():
                        target = target / "hooks.json"
                    if not target.is_file():
                        problems.append(f"{path} {key} source {ref!r} is unresolved (fail closed)")
                    else:
                        (hooks if key == "hooks" else mcp).append(target)
    return mcp, hooks, problems


def inherited_cursor_problems(
    *, home: Path | None = None, system_hooks: tuple[Path, ...] = SYSTEM_CURSOR_HOOKS
) -> list[str]:
    """Session-bound Omnigent config every cursor-agent inherits, if any.

    A worktree isolates only the *project* ``.cursor`` scope. cursor-agent
    also loads (per the installed 2026.09.23 bundle, and verified live for
    user MCP via ``cursor-agent mcp list`` in an isolated worktree):
    enterprise hooks (``/etc/cursor/hooks.json``), team-managed hooks
    (``~/.cursor/managed/active-team-hooks/hooks.json``), user
    ``~/.cursor/{mcp,hooks}.json`` and plugin MCP/hooks under
    ``~/.cursor/plugins``. A session-bound Omnigent entry in any of them
    routes a worker's tools/receipts to a foreign session. Detection is by
    literal markers (``_SESSION_MARKERS``, any server name, url or env);
    unreadable or unresolvable sources fail closed. A binding hidden behind
    an arbitrary wrapper with no marker is not detectable -- this is not a
    proof of complete isolation. The user's config is never edited.
    """
    base = (home if home is not None else Path.home()) / ".cursor"
    problems: list[str] = []
    if base.is_symlink():
        problems.append(f"{base} is a symlink (shared Cursor config; cannot prove it is safe)")
    problems += _session_bound_mcp_servers(base / "mcp.json")
    problems += _omnigent_hooks(base / "hooks.json")
    problems += _omnigent_hooks(base / "managed" / "active-team-hooks" / "hooks.json")
    mcp, hooks, unresolved = _plugin_sources(base / "plugins")
    problems += unresolved
    for path in mcp:
        problems += _session_bound_mcp_servers(path)
    for path in hooks:
        problems += _omnigent_hooks(path)
    for path in system_hooks:
        problems += _omnigent_hooks(path)
    return problems


def cursor_config_conflicts(path: Path) -> list[str]:
    """Session-bound Omnigent Cursor config already present in a worktree."""
    problems = []
    if (path / ".cursor").is_symlink():
        problems.append(f"{path / '.cursor'} is a symlink (shared Cursor config)")
    mcp = path / ".cursor" / "mcp.json"
    hooks = path / ".cursor" / "hooks.json"
    with contextlib.suppress(OSError, ValueError):
        servers = json.loads(mcp.read_text()).get("mcpServers") or {}
        if isinstance(servers, dict) and "omnigent" in servers:
            problems.append(f"{mcp} declares an 'omnigent' MCP server")
    with contextlib.suppress(OSError, ValueError):
        if "record-usage" in hooks.read_text():
            problems.append(f"{hooks} carries an Omnigent usage stop hook")
    return problems


def _strip_session_bound(rel: str, data: object) -> object:
    """*data* minus every session-bound Omnigent entry (a copy).

    Stricter than Omnigent's own merge (which only replaces the
    ``omnigent`` server and drops usage hooks of the CURRENT module): any
    MCP server named ``omnigent`` or carrying a session marker
    (:data:`_SESSION_MARKERS`, e.g. an old-layout bridge on another home)
    and any hook entry of any event carrying one are dropped. Everything
    else (user servers and hooks) is kept verbatim.
    """
    data = json.loads(json.dumps(data))
    if not isinstance(data, dict):
        return data
    if rel.endswith("mcp.json"):
        for key in ("mcpServers", "servers"):
            servers = data.get(key)
            if isinstance(servers, dict):
                for name in [n for n, spec in servers.items() if n == "omnigent" or _marked(spec)]:
                    del servers[name]
        return data
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in list(hooks.items()):
            if isinstance(entries, list):
                hooks[event] = [e for e in entries if not _marked(e)]
    return data


def neutralise_tracked_cursor(path: Path) -> list[str]:
    """Neutralise TRACKED project ``.cursor/{mcp,hooks}.json`` in worktree *path*.

    A repository may commit a project Cursor config carrying another
    Omnigent session's binding (a stale ``omnigent`` MCP server, a usage
    stop hook). Every checkout of it -- every task-owned worktree -- would
    hand that binding to the worker's cursor-agent. For each such file that
    is tracked in *path*'s index as a regular file, this writes the file
    with every session-bound entry stripped (:func:`_strip_session_bound`;
    user entries kept) and sets ``skip-worktree`` on it in *path*'s OWN
    index (the aggregate checkout's index is never touched). The
    overwrite, and whatever the worker's own Omnigent launch merges on top
    of it, is therefore invisible to ``git status`` and ``git add -A``:
    the builder commit, the integration merge and the evaluator's clean
    check never see it, and ``git worktree remove`` discards it with the
    worktree (no restore step can race or fail). :func:`integrate` still
    refuses a worker commit that touches a neutralised path.

    Untracked files, symlinks (file or ``.cursor`` dir), non-regular index
    entries and unparsable JSON are never touched: they stay for
    :func:`cursor_config_conflicts` to judge (an untracked foreign config
    in a worktree is the genuinely unsafe case and is still refused).
    Returns the neutralised repo-relative paths.
    """
    if (path / ".cursor").is_symlink():
        return []
    done: list[str] = []
    for rel in OWNED_CURSOR_FILES:
        target = path / rel
        if target.is_symlink() or not target.is_file():
            continue
        try:
            indexed = _index_entry(path, rel)
        except WorktreeError:
            continue
        if indexed is None:
            continue
        _blob, mode = indexed
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        inert = _strip_session_bound(rel, data)
        if inert == data:
            continue
        git(path, "update-index", "--skip-worktree", "--", rel)
        tmp = target.with_name(f".{target.name}.trio-neutral-{os.getpid()}")
        tmp.write_text(json.dumps(inert, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        done.append(rel)
    return done


def _normal_cursor(rel: str, data: object) -> object:
    """Session-bound entries stripped plus Omnigent's merge defaults filled."""
    data = _strip_session_bound(rel, data)
    if not isinstance(data, dict):
        return data
    if rel.endswith("mcp.json"):
        if not isinstance(data.get("mcpServers"), dict):
            data["mcpServers"] = {}
        return data
    data.setdefault("version", 1)
    hooks = data.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    data["hooks"] = {k: v for k, v in hooks.items() if v != []}
    return data


def omnigent_only_change(repo: Path, rel: str) -> bool:
    """Whether tracked *rel* differs from its index version ONLY by
    session-bound Omnigent entries (e.g. the root Lead launch's merge).

    Such a modification is never product content: it neither hides a
    product edit from a worker nor collides with a worker merge (a worker
    cannot change the path, see :func:`neutralise_tracked_cursor`). Any
    other difference, a symlink or unparsable content returns False.
    """
    if rel not in OWNED_CURSOR_FILES:
        return False
    target = repo / rel
    if target.is_symlink() or (repo / ".cursor").is_symlink() or not target.is_file():
        return False
    try:
        indexed = _index_entry(repo, rel)
        if indexed is None:
            return False
        base = json.loads(indexed[0].decode("utf-8"))
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, WorktreeError):
        return False
    return _normal_cursor(rel, current) == _normal_cursor(rel, base)


def mark_running(
    repo: Path,
    record: dict[str, Any],
    worker_pid: int | None,
    *,
    pgid: int | None = None,
    session_id: str | None = None,
) -> None:
    """Record the live users of a worktree before the worker does anything."""
    record["state"] = "running"
    record["dispatcher"] = process_identity(os.getpid())
    record["worker"] = process_identity(worker_pid) if worker_pid else None
    record["pgid"] = pgid
    if session_id:
        record.setdefault("session_ids", []).append(session_id)
    save_record(repo, record)


def mark_exited(repo: Path, record: dict[str, Any], returncode: int | None) -> None:
    """Record the dispatcher-observed exit; only exit 0 marks output usable."""
    record["state"] = "exited"
    record["returncode"] = returncode
    record["worker_ok"] = returncode == 0
    save_record(repo, record)


# ------------------------------------------------------------- integration


def _commit_worker(repo: Path, record: dict[str, Any], summary: str) -> str | None:
    """Commit everything the worker left in its worktree; None if nothing."""
    path = Path(record["path"])
    head = rev(path, "HEAD")
    user, residue = _split_status(path, status_entries(path))
    if user:
        # Omnigent's generated session config is never product content.
        git(path, "add", "-A", "--", ".", *[f":(exclude,literal){r}" for r in residue])
        message = f"slice({record['slice']}): {summary}".strip()
        git(path, "commit", "-q", "-m", message)
        head = rev(path, "HEAD")
    if head is None or head == record["base"]:
        return None
    return head


def integrate(
    repo: Path,
    worker_id: str,
    *,
    summary: str | None = None,
) -> dict[str, Any]:
    """Commit the worker's changes and merge them into the aggregate branch.

    Serialized per repository. Only output of a worker the dispatcher saw
    exit 0 is integrated; failed, interrupted or never-finished output is
    always refused (there is no override: the recovery path is a fresh,
    successful re-dispatch, and the failed worktree stays retained as
    unaccepted work). Refused while
    an integration fence is held (an integration evaluation/retirement is
    in progress) and while any process still uses the worktree. Any failure
    leaves the worktree, branch and aggregate untouched (a started merge is
    rolled back with ``git merge --abort``).
    """
    repo = repo_toplevel(repo)
    with repo_lock(repo):
        record = load_record(repo, worker_id)
        if record.get("kind") == "eval":
            raise WorktreeError(f"{worker_id} is an evaluator worktree; nothing to integrate")
        if record.get("state") in ("integrated", "accepted", "removing", "removed"):
            return record
        if identity_alive(record.get("worker")) or group_alive(record.get("pgid")):
            return _retain(repo, record, "active_session", "worker process still running")
        if not record.get("worker_ok"):
            prior = record.get("retained_reason")
            reason = prior if prior in UNVERIFIED_OUTPUT_REASONS else "unverified_output"
            return _retain(
                repo, record, reason,
                "worker did not finish successfully; its partial output is never "
                "integrated -- re-dispatch a fresh builder for the slice",
            )
        fence = active_fence(repo)
        if fence is not None:
            return _retain(
                repo, record, "integration_fenced",
                f"integration evaluation/retirement in progress: {fence.get('reason')}",
            )
        path = Path(record["path"])
        users = processes_using(path) if path.is_dir() else []
        if users:
            return _retain(repo, record, "active_session", f"processes using worktree: {users[:10]}")
        if not path.is_dir():
            return _retain(repo, record, "missing_worktree", str(path))
        if rev(path, "HEAD") is None:
            return _retain(repo, record, "unreadable_worktree", str(path))
        mailbox_rel = _mailbox_rel(repo, Path(record["mailbox"])) if record.get("mailbox") else None
        try:
            commit = _commit_worker(
                repo, record, summary or f"isolated {record.get('role', 'builder')} {worker_id}"
            )
        except WorktreeError as exc:
            return _retain(repo, record, "commit_failed", str(exc))
        if commit is None:
            record["state"] = "integrated"
            record["worker_commit"] = record["base"]
            record["merge_commit"] = record["base"]
            record["empty"] = True
            record["dispatcher"] = None
            record.pop("retained_reason", None)
            record.pop("retained_detail", None)
            save_record(repo, record)
            return record
        record["worker_commit"] = commit
        record["state"] = "committed"
        save_record(repo, record)
        changed = _out(path, "diff", "--name-only", f"{record['base']}..{commit}").splitlines()
        if mailbox_rel and any(p.startswith(mailbox_rel) for p in changed):
            return _retain(repo, record, "mailbox_write", "worker changed mailbox files")
        touched = [r for r in record.get("neutralised_cursor") or [] if r in changed]
        if touched:
            return _retain(
                repo, record, "cursor_config_write",
                "worker committed the neutralised tracked project Cursor config "
                f"({', '.join(touched)}); its worktree copy is Omnigent session config, "
                "never product -- change the tracked file on the aggregate by hand",
            )
        head_ref = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if head_ref != record["aggregate_ref"]:
            return _retain(
                repo, record, "aggregate_moved",
                f"aggregate is on {head_ref or 'detached HEAD'}, expected {record['aggregate_ref']}",
            )
        blockers = aggregate_blockers(
            repo,
            Path(record["mailbox"]) if record.get("mailbox") else None,
            record.get("declared_writes"),
        )
        if blockers:
            return _retain(repo, record, "aggregate_dirty", "; ".join(blockers[:5]))
        if (common_dir(repo) / "MERGE_HEAD").exists():
            return _retain(repo, record, "aggregate_merging", "another merge is in progress")
        before = rev(repo, "HEAD")
        merge = git(
            repo, "merge", "--no-ff", "--no-edit", "-q",
            "-m", f"merge(worker): {worker_id} slice {record['slice']}",
            record["branch"], check=False,
        )
        if merge.returncode != 0:
            detail = (merge.stdout + merge.stderr).strip()[-2000:]
            if (common_dir(repo) / "MERGE_HEAD").exists():
                git(repo, "merge", "--abort", check=False)
            after = rev(repo, "HEAD")
            reason = "merge_conflict" if "CONFLICT" in detail else "merge_failed"
            if after != before:
                reason = "merge_state_uncertain"
            return _retain(repo, record, reason, detail)
        merged = rev(repo, "HEAD")
        if merged is None or not is_ancestor(repo, commit, merged):
            return _retain(repo, record, "merge_state_uncertain", "worker commit not in aggregate")
        record["merge_commit"] = merged
        record["state"] = "integrated"
        record["dispatcher"] = None  # dispatch finished; only sessions count now
        record.pop("retained_reason", None)
        record.pop("retained_detail", None)
        save_record(repo, record)
        return record


# -------------------------------------------------------------- acceptance
#
# Acceptance is NOT parsed here. The caller (trioctl) derives it from the
# loop core's own SHIP retirement contract (driver-finalized ``shipped``
# state, attempt/evaluated binding, product tree unchanged since the
# evaluated pin, committed ``loop: iteration N — SHIP`` retirement, clean
# committed VERDICT.md) and passes ``acceptance_for(mailbox)`` returning
# ``{"evaluated": <full sha>, "iteration": N, ...}`` or None.


def _accepting_revision(
    repo: Path, record: dict[str, Any], acceptance_for: Any
) -> dict[str, Any] | None:
    """The verified acceptance covering this record's exact merge, or None."""
    """Sets ``record["acceptance_pending"]`` to the reason when not accepted.

    ``acceptance_for(mailbox)`` returns ``{"evaluated": sha, ...}`` for a
    verified retired SHIP or ``{"pending": "<reason>"}`` / None otherwise.
    """
    merge = record.get("merge_commit")
    if acceptance_for is None:
        record["acceptance_pending"] = "no acceptance source (cleanup cannot verify a SHIP)"
        return None
    if not merge or not record.get("mailbox"):
        record["acceptance_pending"] = "record has no merge or mailbox"
        return None
    try:
        acceptance = acceptance_for(Path(record["mailbox"]))
    except Exception as exc:  # noqa: BLE001 - unverifiable acceptance is no acceptance
        record["acceptance_pending"] = f"acceptance check failed: {type(exc).__name__}"
        return None
    if not isinstance(acceptance, dict) or not acceptance.get("evaluated"):
        pending = acceptance.get("pending") if isinstance(acceptance, dict) else None
        record["acceptance_pending"] = pending or "no verified retired SHIP"
        return None
    pinned = acceptance.get("evaluated")
    if record.get("repo_name"):
        # r15: a declared repo's record is covered by that repo's own pin.
        pins = acceptance.get("evaluated_repos")
        pinned = pins.get(record["repo_name"]) if isinstance(pins, dict) else None
        if not pinned:
            record["acceptance_pending"] = (
                f"verified SHIP has no evaluated pin for repo {record['repo_name']}"
            )
            return None
    evaluated = rev(repo, str(pinned or ""))
    aggregate = rev(repo, record["aggregate_ref"])
    if evaluated is None or aggregate is None or evaluated != pinned:
        record["acceptance_pending"] = "evaluated sha is not a full commit on this repo"
        return None
    # The graded revision must contain the exact integrated merge and be
    # on the aggregate branch; a merge landing after the pin is not covered.
    if not is_ancestor(repo, merge, evaluated):
        record["acceptance_pending"] = (
            f"merge {merge[:12]} is not contained in evaluated pin {evaluated[:12]}"
        )
        return None
    if not is_ancestor(repo, evaluated, aggregate):
        record["acceptance_pending"] = f"evaluated pin {evaluated[:12]} is not on the aggregate branch"
        return None
    record.pop("acceptance_pending", None)
    return dict(acceptance)


# ------------------------------------------------------------------ fence
#
# Multi-holder: each acquire creates its own token file under
# ``<ledger>/fences/``; release removes only the caller's token. Integrations
# are blocked while ANY live holder's token exists. A token whose holder
# process is dead is stale and ignored (crash recovery); an unreadable token
# counts as held (fail closed).


def _fence_dir(repo: Path) -> Path:
    return ledger_dir(repo) / "fences"


def acquire_fence(repo: Path, *, reason: str, mailbox: Path | None = None) -> str:
    """Add one fence holder; returns its token (pass it to :func:`release_fence`)."""
    repo = repo_toplevel(repo)
    token = uuid.uuid4().hex
    with repo_lock(repo):
        directory = _fence_dir(repo)
        directory.mkdir(parents=True, exist_ok=True)
        fence = {
            "token": token,
            "holder": process_identity(os.getpid()),
            "reason": reason,
            "mailbox": str(mailbox) if mailbox else None,
            "at": time.time(),
        }
        tmp = directory / f".{token}.tmp"
        tmp.write_text(json.dumps(fence, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, directory / f"{token}.json")
    return token


def release_fence(repo: Path, token: str) -> bool:
    """Remove exactly this holder's token; True when it existed."""
    if not token or _SAFE.sub("", token) != token:
        return False
    repo = repo_toplevel(repo)
    with repo_lock(repo):
        path = _fence_dir(repo) / f"{token}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


def active_fences(repo: Path) -> list[dict[str, Any]]:
    """Live (or unreadable) fence holders for *repo*."""
    directory = _fence_dir(repo)
    if not directory.is_dir():
        return []
    held = []
    for path in sorted(directory.glob("*.json")):
        try:
            fence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held.append({"reason": f"unreadable fence {path.name} (uncertain)"})
            continue
        if identity_alive(fence.get("holder")):
            held.append(fence)
    return held


def active_fence(repo: Path) -> dict[str, Any] | None:
    fences = active_fences(repo)
    return fences[0] if fences else None


# ----------------------------------------------------------------- cleanup


def _owned_worktree(repo: Path, record: dict[str, Any]) -> str | None:
    """None when *record* provably owns a registered worktree, else a reason."""
    if record.get("common_dir") != str(common_dir(repo)):
        return "foreign_repository"
    path = str(Path(record["path"]))
    entry = next((e for e in worktree_list(repo) if e.get("worktree") == path), None)
    if entry is None:
        return "not_registered"
    if record.get("kind") == "eval":
        if "detached" not in entry or entry.get("HEAD") != record.get("base"):
            return "eval_head_mismatch"
    else:
        branch = str(record.get("branch", ""))
        if branch != f"{BRANCH_PREFIX}{record['id']}":
            return "unowned_branch"
        if entry.get("branch") != f"refs/heads/{branch}":
            return "branch_mismatch"
    try:
        admin = Path(_out(Path(path), "rev-parse", "--absolute-git-dir"))
        marker = (admin / OWNER_MARKER).read_text(encoding="utf-8").strip()
    except (OSError, WorktreeError):
        return "owner_marker_missing"
    if marker != record["id"]:
        return "owner_marker_mismatch"
    return None


def _retirement_accepted(record: dict[str, Any]) -> bool:
    """Builder merged and SHIP-bound (``accepted_by``), or evaluator finished."""
    if record.get("kind") == "eval":
        return bool(record.get("finished"))
    return bool(record.get("merge_commit")) and bool(record.get("accepted_by"))


def _blocking_state(
    repo: Path,
    record: dict[str, Any],
    held_sessions: set[str] | None = None,
    *,
    owner_verified: bool = False,
) -> tuple[str, str] | None:
    """Why the worktree of *record* must be retained, or None to remove it.

    Ignored content (``git status --ignored=matching``) is protected because
    ``git worktree remove`` deletes it silently (``.env``, ``.runtime/``,
    ``.context/``, uncommitted evidence). Always disposable: Python caches
    (:data:`DISPOSABLE_IGNORED`, ``*.pyc``/``*.pyo``) and Omnigent's exact
    generated ``.cursor`` config. Additionally, when *owner_verified* (the
    caller checked :func:`_owned_worktree`) AND the record is accepted
    (builder merged + ``accepted_by``) or finished (evaluator), an ignored
    entry does not block when it is (a) a directory holding no file or
    symlink, or (b) a rebuildable artifact: any directory component in
    :data:`REBUILDABLE_IGNORED_DIRS` or ending in
    :data:`REBUILDABLE_IGNORED_DIR_SUFFIXES` (``*.egg-info``), or
    ``dist``/``build`` when a ``.gitignore`` in the worktree names that exact
    directory (global and ``info/exclude`` patterns do not count), or a
    suffix in :data:`REBUILDABLE_IGNORED_SUFFIXES`. Everything else retains the
    worktree as ``ignored_content`` with the first 5 blocking paths as detail.
    """
    path = Path(record["path"])
    retirable = owner_verified and _retirement_accepted(record)
    if identity_alive(record.get("worker")) or identity_alive(record.get("dispatcher")):
        return "active_session", "recorded worker/dispatcher process is alive"
    if group_alive(record.get("pgid")):
        return "active_session", f"process group {record.get('pgid')} still has members"
    held = sorted(set(record.get("session_ids") or []) & (held_sessions or set()))
    if held:
        return "held_session", f"held broker session(s) reference this worktree: {held}"
    users = [pid for pid in processes_using(path) if pid != os.getpid()]
    if users:
        return "active_session", f"processes using worktree: {users[:10]}"
    entries, residue = _split_status(path, status_entries(path))
    ignored = []
    for rel in ignored_entries(path):
        if _disposable_ignored(rel) or (retirable and _rebuildable_ignored(path, rel)):
            continue
        owned = _owned_ignored_cursor(path, rel)
        if owned is None:
            ignored.append(rel)
        else:
            residue.extend(r for r in owned if r not in residue)
    record["residue"] = residue
    if ignored:
        return "ignored_content", "; ".join(ignored[:5])
    if any(line[:2] in ("DD", "AU", "UD", "UA", "DU", "AA", "UU") for line in entries):
        return "unmerged", "; ".join(entries[:5])
    untracked = [line for line in entries if line.startswith("??")]
    if untracked:
        return "untracked", "; ".join(untracked[:5])
    if entries:
        return "dirty", "; ".join(entries[:5])
    admin = Path(record.get("admin_dir") or "")
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"):
        if admin and (admin / marker).exists():
            return "unmerged", f"{marker} present"
    head = rev(path, "HEAD")
    if record.get("kind") == "eval":
        if head != record.get("base"):
            return "unintegrated_commits", f"evaluator worktree moved to {head}"
        return None
    tip = rev(repo, f"refs/heads/{record['branch']}")
    if head != record.get("worker_commit") or tip != record.get("worker_commit"):
        return "unintegrated_commits", f"head={head} tip={tip} integrated={record.get('worker_commit')}"
    aggregate = rev(repo, record["aggregate_ref"])
    if aggregate is None or not is_ancestor(repo, record["worker_commit"], aggregate):
        return "not_in_aggregate", f"{record['worker_commit']} not in {record['aggregate_ref']}"
    return None


def _finish_removal(repo: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Branch deletion step, shared by fresh and resumed removals."""
    branch_ref = f"refs/heads/{record['branch']}" if record.get("branch") else None
    if branch_ref and rev(repo, branch_ref) is not None:
        tip = rev(repo, branch_ref)
        aggregate = rev(repo, record["aggregate_ref"])
        if tip != record.get("worker_commit") or aggregate is None or not is_ancestor(repo, tip, aggregate):
            return _retain(repo, record, "branch_unmerged", f"{record['branch']} tip {tip}")
        head_ref = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if head_ref != record["aggregate_ref"]:
            # `branch -d` checks merged-ness against the checked-out HEAD.
            return _retain(repo, record, "aggregate_moved", f"aggregate is on {head_ref}")
        proc = git(repo, "branch", "-d", record["branch"], check=False)
        if proc.returncode != 0:
            return _retain(repo, record, "branch_delete_failed", proc.stderr.strip())
    record["state"] = "removed"
    record["removed_at"] = time.time()
    record.pop("retained_reason", None)
    record.pop("retained_detail", None)
    save_record(repo, record)
    return record


def cleanup_one(
    repo: Path,
    worker_id: str,
    *,
    held_sessions: set[str] | None = None,
    acceptance_for: Any = None,
) -> dict[str, Any]:
    """Advance one record toward removal; idempotent and restart-safe."""
    record = load_record(repo, worker_id)
    state = record.get("state")
    if state == "removed":
        return record
    if state == "removing" or record.get("worktree_removed"):
        # Resume an interrupted removal: finish the branch step once the
        # worktree is gone; a still-registered one is re-checked below.
        path = str(Path(record["path"]))
        if not any(e.get("worktree") == path for e in worktree_list(repo)):
            if state == "removing" or record.get("worktree_removed"):
                record["worktree_removed"] = True
                return _finish_removal(repo, record)
        state = "accepted"
    is_eval = record.get("kind") == "eval"
    if is_eval and state in ("exited", "retained") and record.get("finished"):
        state = "accepted"  # evaluator worktrees need no integration
    if state not in ("integrated", "accepted", "retained"):
        if state in ACTIVE_STATES and not (
            identity_alive(record.get("worker")) or identity_alive(record.get("dispatcher"))
        ):
            return _retain(repo, record, "interrupted", f"dispatcher gone in state {state}")
        return record
    if is_eval and not record.get("finished"):
        return record
    if not is_eval and not record.get("merge_commit"):
        # Retained before integration finished: only `integrate` may advance it.
        return record
    if not is_eval and not record.get("accepted_by"):
        acceptance = _accepting_revision(repo, record, acceptance_for)
        if acceptance is None:
            save_record(repo, record)  # persist the explicit pending reason
            return record  # integrated; waiting for verified acceptance
        record["accepted_by"] = acceptance
        record["state"] = "accepted"
        save_record(repo, record)
    reason = _owned_worktree(repo, record)
    if reason is not None:
        return _retain(repo, record, "uncertain_ownership", reason)
    blocking = _blocking_state(repo, record, held_sessions, owner_verified=True)
    if blocking is not None:
        return _retain(repo, record, *blocking)
    record["state"] = "removing"
    save_record(repo, record)
    path = Path(record["path"])
    for rel in record.get("residue") or []:
        # Re-validated immediately before deletion (content may have changed).
        if owned_residue(path, rel):
            (path / rel).unlink()
    with contextlib.suppress(OSError):
        (path / ".cursor").rmdir()  # only when now empty
    proc = git(repo, "worktree", "remove", record["path"], check=False)
    if proc.returncode != 0:
        return _retain(repo, record, "worktree_remove_failed", proc.stderr.strip())
    record["worktree_removed"] = True
    save_record(repo, record)
    return _finish_removal(repo, record)


def cleanup(
    repo: Path,
    *,
    mailbox: Path | None = None,
    held_sessions: set[str] | None = None,
    acceptance_for: Any = None,
) -> list[dict[str, Any]]:
    """Clean every ledger-owned worker for *repo* (optionally one mailbox)."""
    repo = repo_toplevel(repo)
    results: list[dict[str, Any]] = []
    with repo_lock(repo):
        for worker_id, record in list_records(repo):
            if record is None:
                results.append({"id": worker_id, "state": "retained",
                                "retained_reason": "unreadable_record"})
                continue
            if mailbox is not None and record.get("mailbox") != str(mailbox.resolve()):
                continue
            try:
                results.append(cleanup_one(
                    repo, worker_id,
                    held_sessions=held_sessions,
                    acceptance_for=acceptance_for,
                ))
            except WorktreeError as exc:
                record = load_record(repo, worker_id)
                results.append(_retain(repo, record, "cleanup_error", str(exc)))
    return results


# ------------------------------------------ settling after session teardown
#
# Archive/DELETE of a session makes its host runner exit, but not
# instantly: cleanup that runs right after the post-loop prune sees the
# exiting runner still using an evaluator worktree and retains it
# (``active_session``) with nothing left to retry (D1). These helpers wait,
# bounded, for exactly the processes using such a worktree to exit and then
# re-run the unchanged guards once. Nothing is killed or forced.


def owner_exit_state(ident: dict[str, Any]) -> str:
    """``exited``, ``alive`` or ``unknown`` for a ``{pid, start}`` identity.

    A vanished pid, a reused pid (different start ticks) or a zombie (it
    holds no cwd/fds) count as exited. A process whose stat cannot be read
    is ``unknown`` and is never treated as gone.
    """
    pid = ident.get("pid")
    proc = Path(f"/proc/{pid}")
    try:
        stat = (proc / "stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return "exited"
    except OSError:
        return "unknown" if proc.exists() else "exited"
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) <= 19 or ident.get("start") is None:
        return "unknown"
    if fields[19] != ident.get("start") or fields[0] in ("Z", "X"):
        return "exited"
    return "alive"


def settle_candidates(results: list[dict[str, Any]], torn_down: Any) -> list[str]:
    """Ids retained only by processes of sessions this run just tore down.

    Every session bound to the worktree must be in *torn_down*, and no
    recorded worker/dispatcher/process group may be alive: those are live
    owners with their own lifecycle and are never waited out.
    """
    gone = {sid for sid in torn_down or () if isinstance(sid, str) and sid}
    ids = []
    for record in results:
        if record.get("state") != "retained" or record.get("retained_reason") != "active_session":
            continue
        sessions = set(record.get("session_ids") or [])
        if not sessions or not sessions <= gone:
            continue
        if identity_alive(record.get("worker")) or identity_alive(record.get("dispatcher")):
            continue
        if group_alive(record.get("pgid")):
            continue
        ids.append(record["id"])
    return ids


def await_owner_exit(
    repo: Path,
    worker_ids: list[str],
    *,
    deadline_s: float,
    poll_s: float = 0.05,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict[str, dict[str, Any]]:
    """Wait, at most *deadline_s* in total, for each worktree's users to exit.

    Read-only and lock-free. Each poll rescans the worktree, so a process
    that starts using it meanwhile is tracked too. Returns, per id,
    ``outcome`` (``exited`` | ``timeout`` | ``unconfirmed``), ``waited_s``
    and the pids still counted against it.
    """
    start = clock()
    tracked: dict[str, dict[int, dict[str, Any]]] = {wid: {} for wid in worker_ids}
    paths = {wid: Path(load_record(repo, wid)["path"]) for wid in worker_ids}
    pending = set(worker_ids)
    result: dict[str, dict[str, Any]] = {}
    delay = poll_s
    while True:
        for wid in sorted(pending):
            idents = tracked[wid]
            try:
                users = processes_using(paths[wid])
            except OSError:
                users = None  # /proc not listable: exit cannot be confirmed
            for pid in users or ():
                ident = idents.get(pid)
                if ident is None or owner_exit_state(ident) == "exited":
                    idents[pid] = process_identity(pid)
            states = {pid: owner_exit_state(i) for pid, i in idents.items()}
            alive = sorted(p for p, s in states.items() if s == "alive")
            unknown = sorted(p for p, s in states.items() if s == "unknown")
            if users is not None and not users and not alive and not unknown:
                result[wid] = {"outcome": "exited", "pids": []}
                pending.discard(wid)
            else:
                result[wid] = {
                    "outcome": "timeout" if alive and users is not None else "unconfirmed",
                    "pids": alive or unknown,
                }
        elapsed = clock() - start
        if not pending or elapsed >= deadline_s:
            break
        sleep(min(delay, deadline_s - elapsed))
        delay = min(delay * 2, 0.5)
    waited = round(clock() - start, 3)
    for entry in result.values():
        entry["waited_s"] = waited
    return result


def recheck_settled(
    repo: Path,
    outcomes: dict[str, dict[str, Any]],
    *,
    deadline_s: float,
    held_sessions: set[str] | None = None,
    acceptance_for: Any = None,
) -> list[dict[str, Any]]:
    """Re-run ``cleanup_one`` for worktrees whose users exited; retain the rest."""
    repo = repo_toplevel(repo)
    results: list[dict[str, Any]] = []
    with repo_lock(repo):
        for worker_id, outcome in sorted(outcomes.items()):
            record = load_record(repo, worker_id)
            if record.get("state") != "retained":
                results.append(record)  # changed meanwhile: leave it alone
                continue
            record["owner_exit"] = dict(outcome)
            save_record(repo, record)
            pids = outcome.get("pids") or []
            if outcome.get("outcome") == "timeout":
                results.append(_retain(
                    repo, record, "active_session",
                    f"processes still using worktree after {deadline_s:g}s: {pids[:10]}",
                ))
                continue
            if outcome.get("outcome") != "exited":
                results.append(_retain(
                    repo, record, "active_session",
                    f"exit of processes using worktree unconfirmed: {pids[:10]}",
                ))
                continue
            try:
                results.append(cleanup_one(
                    repo, worker_id,
                    held_sessions=held_sessions,
                    acceptance_for=acceptance_for,
                ))
            except WorktreeError as exc:
                results.append(_retain(repo, load_record(repo, worker_id), "cleanup_error", str(exc)))
    return results


def mark_finished(repo: Path, worker_id: str) -> dict[str, Any]:
    """An evaluator session using this worktree has ended its dispatch."""
    with repo_lock(repo):
        record = load_record(repo, worker_id)
        record["finished"] = True
        record["dispatcher"] = None
        if record.get("state") in ACTIVE_STATES:
            record["state"] = "exited"
        save_record(repo, record)
        return record


def discard_eval(repo: Path, worker_id: str, reason: str) -> dict[str, Any]:
    """A just-created evaluator worktree whose bind failed: never an orphan.

    Recorded as ``retained`` (``bind_failed`` + *reason*) and finished, with
    no live dispatcher, then removed through the normal ownership-checked
    :func:`cleanup_one`. Anything that blocks removal leaves the retained
    record in the ledger with its reason. Evaluator records only.
    """
    with repo_lock(repo):
        record = load_record(repo, worker_id)
        if record.get("kind") != "eval":
            raise WorktreeError(f"{worker_id} is not an evaluator worktree")
        record["finished"] = True
        record["dispatcher"] = None
        record["worker"] = None
        _retain(repo, record, "bind_failed", reason)
    return cleanup_one(repo, worker_id)


def summarize(record: dict[str, Any]) -> str:
    state = record.get("state")
    reason = record.get("retained_reason")
    text = f"{record.get('id')}: {state}"
    if reason:
        text += f" ({reason})"
    elif state in ("integrated", "exited") and record.get("acceptance_pending"):
        text += f" (acceptance pending: {record['acceptance_pending']})"
    return text


# --------------------------------------------------------------- watchdog


def watchdog(parent_pid: int, parent_start: str | None, max_seconds: float, command: list[str]) -> int:
    """Group leader for an isolated worker: bounded lifetime, dies with its owner.

    Runs *command* as a child in this process group. When the dispatcher
    (identity *parent_pid* + start time) is gone -- e.g. SIGKILLed with no
    chance to clean up -- or *max_seconds* elapse, the whole group gets
    SIGTERM, then SIGKILL. Descendants that escape the group (``setsid``)
    are not tracked here; cleanup/integrate still refuse while any process
    uses the worktree.
    """
    import signal as _signal

    child = subprocess.Popen(command)
    deadline = time.monotonic() + max(max_seconds, 1.0)
    owner = {"pid": parent_pid, "start": parent_start}

    def members() -> list[int]:
        pgid, me = str(os.getpgrp()), os.getpid()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            with contextlib.suppress(OSError, IndexError):
                fields = (entry / "stat").read_text().rsplit(")", 1)[-1].split()
                if fields[2] == pgid and fields[0] != "Z":
                    found.append(int(entry.name))
        return found

    def stop_group(code: int) -> int:
        for sig, wait in ((_signal.SIGTERM, 5.0), (_signal.SIGKILL, 2.0)):
            for pid in members():
                with contextlib.suppress(OSError):
                    os.kill(pid, sig)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                child.poll()  # reap so the child does not linger as a member
                if not members():
                    break
                time.sleep(0.05)
        child.poll()
        return code

    while True:
        code = child.poll()
        if code is not None:
            return code
        if not identity_alive(owner):
            return stop_group(125)
        if time.monotonic() >= deadline:
            return stop_group(124)
        time.sleep(0.25)


def worker_max_seconds(timeout: float | None) -> float:
    if timeout:
        return float(timeout) + 60.0
    raw = os.environ.get(WORKER_MAX_SECONDS_ENV, "").strip()
    try:
        return float(raw) if raw else DEFAULT_WORKER_MAX_SECONDS
    except ValueError:
        return DEFAULT_WORKER_MAX_SECONDS


def watchdog_command(command: list[str], *, max_seconds: float) -> list[str]:
    """Prefix *command* with this module's watchdog bound to the caller."""
    import sys as _sys

    pid = os.getpid()
    return [
        _sys.executable, "-I", str(Path(__file__).resolve()), "watchdog",
        str(pid), str(_proc_start(pid) or ""), str(max_seconds), "--", *command,
    ]


if __name__ == "__main__":
    import sys as _sys

    argv = _sys.argv[1:]
    if len(argv) >= 5 and argv[0] == "watchdog" and argv[4] == "--":
        _sys.exit(watchdog(int(argv[1]), argv[2] or None, float(argv[3]), argv[5:]))
    _sys.exit(2)

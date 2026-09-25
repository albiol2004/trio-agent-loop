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
session-bound Omnigent entries (see :func:`inherited_cursor_problems`). Removal uses plain
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
FENCE_FILE = "integration-fence.json"
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


def default_worktree_root(repo: Path) -> Path:
    env = os.environ.get(WORKTREE_ROOT_ENV, "").strip()
    if env:
        base = Path(env).expanduser()
    else:
        state = os.environ.get("XDG_STATE_HOME", "").strip()
        base = (Path(state) if state else Path.home() / ".local" / "state")
        base = base / "trio-agent-loop" / "worktrees"
    key = hashlib.sha256(str(common_dir(repo)).encode()).hexdigest()[:12]
    return (base / f"{repo.name}-{key}").resolve()


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


def cursor_processes_at(root: Path) -> list[int]:
    """Live cursor-agent processes whose project root cwd is exactly *root*."""
    target = str(root.resolve())
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / "cwd") != target:
                continue
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if b"cursor-agent" in cmd:
            found.append(int(entry.name))
    return found


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


def aggregate_blockers(repo: Path, mailbox: Path | None) -> list[str]:
    """Aggregate status entries outside the mailbox (product work not committed).

    A worker branches from committed HEAD, so uncommitted product edits in
    the aggregate would be invisible to it (breaking declared ``reads:``)
    and could collide with the integration merge.
    """
    rel = _mailbox_rel(repo, mailbox)
    blockers = []
    user, _residue = _split_status(repo, status_entries(repo))
    for line in user:
        path = _status_path(line)
        if rel and (path.startswith(rel) or path + "/" == rel):
            continue
        blockers.append(line)
    return blockers


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
) -> dict[str, Any]:
    """Create one task-owned worktree.

    Builders get a fresh ``trio-worker/<id>`` branch at aggregate HEAD.
    With *detach_at* (an Evaluator grading a pinned sha) the worktree is
    detached at that commit, carries no branch and is never integrated.
    """
    repo = repo_toplevel(repo)
    branch_ref = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if not branch_ref.startswith("refs/heads/"):
        raise WorktreeError("aggregate repository is not on a branch (detached HEAD)")
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
    blockers = [] if detach_at else aggregate_blockers(repo, mailbox)
    if blockers:
        raise WorktreeError(
            "aggregate has uncommitted product changes a worker would not see; "
            "commit them before an isolated dispatch: " + "; ".join(blockers[:5])
        )
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
    problems = cursor_config_conflicts(path)
    if cursor_project_root(path) != path:
        problems.append(f"worktree {path} is not its own Cursor project root")
    if problems:
        _retain(repo, record, "unsafe_cursor_config", "; ".join(problems))
        raise WorktreeError("; ".join(problems))
    save_record(repo, record)
    return record


def _session_bound_mcp_servers(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [f"{path} is unreadable or not JSON (cannot prove it is safe)"]
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    problems = []
    for name, spec in (servers or {}).items() if isinstance(servers, dict) else []:
        args = spec.get("args") if isinstance(spec, dict) else None
        text = " ".join(str(a) for a in args) if isinstance(args, list) else ""
        if name == "omnigent" or ("serve-mcp" in text and "--bridge-dir" in text):
            problems.append(f"{path} declares session-bound Omnigent MCP server {name!r}")
    return problems


def _omnigent_hooks(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return [f"{path} is unreadable (cannot prove it is safe)"]
    if "record-usage" in text or "omnigent" in text:
        return [f"{path} carries an Omnigent session hook"]
    return []


def inherited_cursor_problems(
    *, home: Path | None = None, system_hooks: tuple[Path, ...] = SYSTEM_CURSOR_HOOKS
) -> list[str]:
    """Session-bound Omnigent config every cursor-agent inherits, if any.

    A worktree isolates only the *project* ``.cursor`` scope. cursor-agent
    also loads ``~/.cursor/mcp.json`` (verified live: ``cursor-agent mcp
    list`` in an isolated worktree lists the user servers) and user/system
    hooks. An Omnigent server or hook there is bound to one session's
    bridge, so a worker would route tools (and possibly receipts) to that
    foreign session. The user's global config is never edited here; the
    caller refuses isolated dispatch instead.
    """
    base = (home if home is not None else Path.home()) / ".cursor"
    problems: list[str] = []
    if base.is_symlink():
        problems.append(f"{base} is a symlink (shared Cursor config; cannot prove it is safe)")
    problems += _session_bound_mcp_servers(base / "mcp.json")
    problems += _omnigent_hooks(base / "hooks.json")
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
    override_unverified: str | None = None,
) -> dict[str, Any]:
    """Commit the worker's changes and merge them into the aggregate branch.

    Serialized per repository. Only output of a worker the dispatcher saw
    exit 0 is integrated; failed, interrupted or never-finished output is
    refused (``unverified_output``) unless a human passes
    *override_unverified* (a reason, recorded in the ledger). Refused while
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
            if not (override_unverified and override_unverified.strip()):
                prior = record.get("retained_reason")
                reason = prior if prior in UNVERIFIED_OUTPUT_REASONS else "unverified_output"
                return _retain(
                    repo, record, reason,
                    "worker did not finish successfully; its partial output is "
                    "never integrated automatically (re-dispatch a fresh builder, "
                    "or a human repairs it and integrates with an explicit override)",
                )
            record["override_unverified"] = {
                "reason": override_unverified.strip(),
                "by": process_identity(os.getpid()),
                "at": time.time(),
            }
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
        head_ref = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if head_ref != record["aggregate_ref"]:
            return _retain(
                repo, record, "aggregate_moved",
                f"aggregate is on {head_ref or 'detached HEAD'}, expected {record['aggregate_ref']}",
            )
        blockers = aggregate_blockers(repo, Path(record["mailbox"]) if record.get("mailbox") else None)
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
    merge = record.get("merge_commit")
    if not merge or not record.get("mailbox") or acceptance_for is None:
        return None
    try:
        acceptance = acceptance_for(Path(record["mailbox"]))
    except Exception:  # noqa: BLE001 - unverifiable acceptance is no acceptance
        return None
    if not isinstance(acceptance, dict):
        return None
    evaluated = rev(repo, str(acceptance.get("evaluated") or ""))
    aggregate = rev(repo, record["aggregate_ref"])
    if evaluated is None or aggregate is None or evaluated != acceptance.get("evaluated"):
        return None  # short, unknown or non-canonical sha
    # The graded revision must contain the exact integrated merge and be
    # on the aggregate branch; a merge landing after the pin is not covered.
    if not is_ancestor(repo, merge, evaluated) or not is_ancestor(repo, evaluated, aggregate):
        return None
    return dict(acceptance)


# ------------------------------------------------------------------ fence


def _fence_path(repo: Path) -> Path:
    return ledger_dir(repo) / FENCE_FILE


def acquire_fence(repo: Path, *, reason: str, mailbox: Path | None = None) -> dict[str, Any]:
    """Block integrations into *repo* while an evaluation/retirement runs."""
    repo = repo_toplevel(repo)
    with repo_lock(repo):
        fence = {
            "holder": process_identity(os.getpid()),
            "reason": reason,
            "mailbox": str(mailbox) if mailbox else None,
            "at": time.time(),
        }
        path = _fence_path(repo)
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(fence, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return fence


def release_fence(repo: Path) -> None:
    """Drop this process's fence (another holder's fence is left alone)."""
    repo = repo_toplevel(repo)
    with repo_lock(repo):
        path = _fence_path(repo)
        try:
            fence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (fence.get("holder") or {}).get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                path.unlink()


def active_fence(repo: Path) -> dict[str, Any] | None:
    """The fence if its holder is alive; an unreadable fence counts as held."""
    path = _fence_path(repo)
    if not path.exists():
        return None
    try:
        fence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reason": "unreadable integration fence (uncertain)"}
    return fence if identity_alive(fence.get("holder")) else None


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


def _blocking_state(
    repo: Path, record: dict[str, Any], held_sessions: set[str] | None = None
) -> tuple[str, str] | None:
    path = Path(record["path"])
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
        if _disposable_ignored(rel):
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
            return record  # integrated; waiting for verified acceptance
        record["accepted_by"] = acceptance
        record["state"] = "accepted"
        save_record(repo, record)
    reason = _owned_worktree(repo, record)
    if reason is not None:
        return _retain(repo, record, "uncertain_ownership", reason)
    blocking = _blocking_state(repo, record, held_sessions)
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


def summarize(record: dict[str, Any]) -> str:
    state = record.get("state")
    reason = record.get("retained_reason")
    text = f"{record.get('id')}: {state}"
    if reason:
        text += f" ({reason})"
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

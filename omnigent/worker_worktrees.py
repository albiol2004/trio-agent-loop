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
merged into the aggregate branch, whose merge is contained in a commit the
Evaluator accepted with ``VERDICT: SHIP``, that no live process uses, and
that has no dirty, untracked or unmerged state. Removal uses plain
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
_VERDICT_COMMIT = re.compile(r"^\s*[-*]?\s*`?commit:\s*`?\s*([0-9a-f]{7,40})\b", re.I)


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


def processes_using(path: Path) -> list[int]:
    """PIDs whose cwd or root lies inside *path* (same-user /proc scan)."""
    target = str(path.resolve())
    users: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if cwd == target or cwd.startswith(target + os.sep):
            users.append(int(entry.name))
    return users


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
    record["state"] = "exited"
    record["returncode"] = returncode
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

    Serialized per repository. Any failure leaves the worktree, branch and
    aggregate untouched (a started merge is rolled back with
    ``git merge --abort``) and records a retained state that a later
    ``integrate`` call may retry.
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
        path = Path(record["path"])
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


def accepted_shas(mailbox: Path) -> list[str]:
    """Commit shas the Evaluator accepted in an integration ``VERDICT: SHIP``.

    Slice-level open-loop ``## slice ... — SHIP`` sections do not count:
    cleanup requires acceptance of the aggregate.
    """
    try:
        text = (mailbox / "VERDICT.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "")
    if first != "VERDICT: SHIP":
        return []
    return [m.group(1) for line in lines if (m := _VERDICT_COMMIT.match(line))]


def _accepting_sha(repo: Path, record: dict[str, Any]) -> str | None:
    merge = record.get("merge_commit")
    if not merge or not record.get("mailbox"):
        return None
    aggregate = rev(repo, record["aggregate_ref"])
    if aggregate is None:
        return None
    for sha in accepted_shas(Path(record["mailbox"])):
        full = rev(repo, sha)
        # The accepted revision must contain the exact integrated merge and
        # itself be on the aggregate branch (not a side or rewound commit).
        if full and is_ancestor(repo, merge, full) and is_ancestor(repo, full, aggregate):
            return full
    return None


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
    record["residue"] = residue
    ignored = [rel for rel in ignored_entries(path) if not _disposable_ignored(rel)]
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
    repo: Path, worker_id: str, *, held_sessions: set[str] | None = None
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
        sha = _accepting_sha(repo, record)
        if sha is None:
            return record  # integrated; waiting for evaluator acceptance
        record["accepted_by"] = sha
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
                results.append(cleanup_one(repo, worker_id, held_sessions=held_sessions))
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

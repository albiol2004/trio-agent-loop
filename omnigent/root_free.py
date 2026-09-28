"""Root-free open-loop runs (r16a): one private Lead worktree per loop.

An open-loop run never works in the repository root. At start the driver
creates (or re-attaches) the loop's **Lead worktree** on branch
``trio/<mailbox-slug>`` forked from the target branch tip, seeds the root
mailbox into it with a ``loop: seed <mailbox>`` commit, and from then on the
live mailbox is ``<lead-wt>/<mailbox>``: the Lead session, isolated builders
(branched from and merged into ``trio/<slug>``), slice-evals and the
integration-eval (detached worktrees at their pins) all act on that private
aggregate. The root's working tree, index and ``.cursor`` are never read to
make a decision and never written -- except by the **land** step after a
verified SHIP: ``git merge --ff-only`` in whichever checkout has the target
branch checked out (git refuses on overlapping local edits), or a
compare-and-swap ``update-ref`` when it is checked out nowhere.

Records (all in the repository's git common dir, shared by every worktree):

- ``trio-worktrees/lead-<slug>.json`` -- the Lead worktree ledger record
  (``kind: "lead"``): paths, branch, target, seed commit, per-declared-repo
  aggregates, land progress. It outlives the driver (resume re-attaches
  from it) and is only retired after a successful land (or ``abandon``).
  ``worker_worktrees.cleanup_one`` never touches it.
- ``trio-worktrees/loops/<slug>.json`` -- the live-loop registry entry
  (driver pid identity, live mailbox, writes per repo) while a driver runs.

The slug is ``metrics/trio-metrics.py:loop_slug`` (kept identical here: this
module ships next to trioctl without the metrics set).

Every function takes the loaded ``worker_worktrees`` module as ``wt`` so a
caller (trioctl, tests) controls which copy is used. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

LEAD_PREFIX = "lead-"
LOOPS_DIR = "loops"
AGGREGATES_FILE = (".sessions", "aggregates.json")
#: Ignored driver runtime files of the live mailbox that are copied back to
#: the root mailbox before the Lead worktree is removed (session archives,
#: driver sidecars); never the lock or the dispatch scratch.
RUNTIME_COPY = (".sessions", ".driver.json", ".session.json", "driver.log", ".repairs", ".observe")
RUNTIME_SKIP = (".lock", ".dispatch")
#: Top-level sidecars overwritten at the root (they describe the latest run).
RUNTIME_OVERWRITE = (".driver.json", ".session.json")

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


class RootFreeError(RuntimeError):
    """A root-free loop operation that could not proceed (nothing half-done)."""


# ------------------------------------------------------------------ naming


def loop_slug(mailbox_rel: str) -> str:
    """Same as ``trio-metrics.py:loop_slug`` (kept byte-identical)."""
    text = str(mailbox_rel).strip().strip("/").replace("/", "--")
    return (_SLUG_RE.sub("-", text).strip("-") or "loop")[:120]


def branch_name(slug: str) -> str:
    return f"trio/{slug}"


def record_id(slug: str) -> str:
    return f"{LEAD_PREFIX}{slug}"


# ------------------------------------------------------------------ git io


def _git(wt: Any, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return wt.git(cwd, *args, check=check)
    except wt.WorktreeError as exc:
        raise RootFreeError(str(exc)) from exc


def _out(wt: Any, cwd: Path, *args: str) -> str:
    return _git(wt, cwd, *args).stdout.strip()


def _blob(cwd: Path, rev: str, rel: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "cat-file", "blob", f"{rev}:{rel}"],
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _changed(wt: Any, cwd: Path, old: str, new: str) -> list[str]:
    out = _git(wt, cwd, "diff", "--name-only", "--no-renames", old, new).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _in(path: str, prefix: str | None) -> bool:
    if not prefix:
        return False
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _branch_of(wt: Any, cwd: Path) -> str | None:
    ref = _git(wt, cwd, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None


def checkout_of(wt: Any, repo: Path, branch: str) -> Path | None:
    """The worktree that has *branch* checked out (git allows one), or None."""
    want = f"refs/heads/{branch}"
    for entry in wt.worktree_list(repo):
        if entry.get("branch") == want and "bare" not in entry:
            return Path(entry["worktree"])
    return None


# ------------------------------------------------------------------ records


def _record_file(wt: Any, repo: Path, slug: str) -> Path:
    return wt.ledger_dir(repo) / f"{record_id(slug)}.json"


def load_record(wt: Any, repo: Path, slug: str) -> dict[str, Any] | None:
    """The Lead record of *slug* in *repo*'s ledger (any state), or None."""
    try:
        data = json.loads(_record_file(wt, repo, slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != "lead":
        return None
    return data


def save_record(wt: Any, repo: Path, record: dict[str, Any]) -> None:
    wt.save_record(repo, record)


#: Record states of a loop that still owns its Lead worktree: being
#: created, running (or resumable), or landed with teardown still pending.
ACTIVE_STATES = ("creating", "active", "landed")


def active(record: dict[str, Any] | None) -> bool:
    return bool(record) and record.get("state") in ACTIVE_STATES


def lead_records(wt: Any, repo: Path) -> list[dict[str, Any]]:
    out = []
    directory = wt.ledger_dir(repo)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob(f"{LEAD_PREFIX}*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == "lead":
            out.append(data)
    return out


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return path


def locate(wt: Any, mailbox: Path) -> tuple[Path, str]:
    """(home repo root, mailbox path relative to it) of a mailbox path.

    *mailbox* may be the root copy (``<root>/loop/x``, which need not exist
    yet) or a live copy inside a Lead worktree; both map to the same pair.
    """
    mailbox = Path(os.path.abspath(Path(mailbox).expanduser()))
    try:
        top = wt.repo_toplevel(_existing_ancestor(mailbox))
    except wt.WorktreeError as exc:
        raise RootFreeError(f"{mailbox} is not inside a git checkout: {exc}") from exc
    try:
        resolved = mailbox.resolve()
    except (OSError, RuntimeError):
        resolved = mailbox
    for record in lead_records(wt, top):
        if active(record) and Path(record.get("path") or "") == top:
            rel = resolved.relative_to(top).as_posix()
            return Path(record["repo"]), rel
    try:
        rel = resolved.relative_to(top).as_posix()
    except ValueError as exc:
        raise RootFreeError(f"mailbox {mailbox} is outside its repository {top}") from exc
    return top, rel


def live_mailbox(wt: Any, mailbox: Path) -> Path | None:
    """The live copy of *mailbox* when a root-free loop owns it, else None."""
    try:
        home, rel = locate(wt, mailbox)
    except RootFreeError:
        return None
    record = load_record(wt, home, loop_slug(rel))
    if not active(record) or record.get("mailbox_rel") != rel:
        return None
    live = Path(record.get("live_mailbox") or "")
    return live if live.is_dir() else None


# ------------------------------------------------------------------ registry


def registry_file(wt: Any, repo: Path, slug: str) -> Path:
    return wt.ledger_dir(repo) / LOOPS_DIR / f"{slug}.json"


def registry_write(wt: Any, repo: Path, record: dict[str, Any], **extra: Any) -> Path:
    """Announce this driver as the live owner of *record*'s loop."""
    path = registry_file(wt, repo, record["slug"])
    path.parent.mkdir(parents=True, exist_ok=True)
    ident = wt.process_identity(os.getpid())
    data = {
        "mailbox_rel": record["mailbox_rel"],
        "live_mailbox": record["live_mailbox"],
        "lead_worktree": record["path"],
        "branch": record["branch"],
        "target_ref": record["target_ref"],
        "pid": ident["pid"],
        "pid_start": ident["start"],
        "started_at": time.time(),
        **extra,
    }
    tmp = path.with_suffix(f".json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def registry_remove(wt: Any, repo: Path, slug: str) -> None:
    """Remove the registry entry only while this process still owns it."""
    path = registry_file(wt, repo, slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink()


def registry_entries(wt: Any, repo: Path) -> list[dict[str, Any]]:
    """Registry entries whose driver is still alive (stale ones skipped)."""
    directory = wt.ledger_dir(repo) / LOOPS_DIR
    out = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if wt.identity_alive({"pid": data.get("pid"), "start": data.get("pid_start")}):
            out.append(data)
    return out


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def writes_overlap(
    mine: dict[str, list[str]], entries: list[dict[str, Any]], mailbox_rel: str
) -> list[str]:
    """Warning lines: live loops whose ``writes:`` overlap *mine* (per repo)."""
    lines = []
    for entry in entries:
        if entry.get("mailbox_rel") == mailbox_rel:
            continue
        theirs = entry.get("writes_by_repo") or {}
        for repo_name, paths in mine.items():
            hits = sorted({
                p for p in paths for q in theirs.get(repo_name) or () if _overlap(p, q)
            })
            if hits:
                lines.append(
                    f"live loop {entry.get('mailbox_rel')} (pid {entry.get('pid')}) also "
                    f"writes {', '.join(hits[:5])} in repo {repo_name}: expect a land "
                    "conflict or a re-verification at land"
                )
    return lines


# ------------------------------------------------------------------ create


def _mark_owner(wt: Any, path: Path, owner: str) -> str:
    admin = Path(_out(wt, path, "rev-parse", "--absolute-git-dir"))
    (admin / wt.OWNER_MARKER).write_text(owner + "\n", encoding="utf-8")
    return str(admin)


def _owned(wt: Any, path: Path, owner: str) -> bool:
    try:
        admin = Path(_out(wt, path, "rev-parse", "--absolute-git-dir"))
        return (admin / wt.OWNER_MARKER).read_text(encoding="utf-8").strip() == owner
    except (OSError, RootFreeError):
        return False


def _protect_cursor(wt: Any, path: Path) -> list[str]:
    """Neutralise tracked session-bound ``.cursor`` config and hide every
    tracked ``.cursor/{mcp,hooks}.json`` from this worktree's own index, so
    the Lead's Omnigent launch can never be committed (index is per worktree)."""
    try:
        done = list(wt.neutralise_tracked_cursor(path))
    except (OSError, wt.WorktreeError):
        done = []
    for rel in wt.OWNED_CURSOR_FILES:
        tracked = _git(wt, path, "ls-files", "--error-unmatch", "--", rel, check=False)
        if tracked.returncode == 0 and rel not in done:
            _git(wt, path, "update-index", "--skip-worktree", "--", rel, check=False)
            done.append(rel)
    return done


def _add_worktree(
    wt: Any, repo: Path, path: Path, branch: str, start: str | None, owner: str
) -> str:
    """``git worktree add`` a new (``start`` given) or existing branch at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if start is not None:
        _git(wt, repo, "worktree", "add", "-b", branch, str(path), start)
    else:
        registered = [
            e for e in wt.worktree_list(repo) if e.get("branch") == f"refs/heads/{branch}"
        ]
        stale = [e for e in registered if Path(e["worktree"]) == path and not path.exists()]
        if registered and len(stale) != len(registered):
            raise RootFreeError(
                f"branch {branch} is checked out at {registered[0]['worktree']}; "
                "another driver may own this loop"
            )
        args = ["worktree", "add"] + (["-f"] if stale else []) + [str(path), branch]
        _git(wt, repo, *args)
    admin = _mark_owner(wt, path, owner)
    _protect_cursor(wt, path)
    return admin


def _seed(wt: Any, home: Path, lead: Path, rel: str) -> str | None:
    """Copy the root mailbox's tracked + untracked non-ignored files into the
    Lead worktree and commit them as ``loop: seed <rel>``; the commit sha, or
    None when the Lead worktree already matched."""
    listed = _git(
        wt, home, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", rel,
        check=False,
    ).stdout.split("\0")
    seen: set[str] = set()
    for item in listed:
        if not item or item in seen or item.endswith("/"):
            continue  # a nested repo shows as a directory entry: never copied
        seen.add(item)
        src, dst = home / item, lead / item
        if src.is_symlink():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif not src.exists() and (dst.is_file() or dst.is_symlink()):
            dst.unlink()  # deleted at the root: mirror it
    (lead / rel).mkdir(parents=True, exist_ok=True)
    _git(wt, lead, "add", "-A", "--", rel)
    if _git(wt, lead, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    _git(wt, lead, "commit", "-q", "-m", f"loop: seed {rel}")
    return _out(wt, lead, "rev-parse", "HEAD")


def write_aggregates(record: dict[str, Any]) -> None:
    """The per-run declared-repo map ``read_repos`` applies (r16 ``aggregate_for``)."""
    live = Path(record["live_mailbox"])
    path = live.joinpath(*AGGREGATES_FILE)
    repos = {
        name: {"path": info["path"], "branch": info["branch"], "main": info["main"]}
        for name, info in (record.get("repos") or {}).items()
    }
    if not repos:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"schema": 1, "repos": repos}, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def begin(
    wt: Any,
    *,
    home: Path,
    mailbox_rel: str,
    worktree_root: Path,
    target: str | None = None,
    declared: list[dict[str, Any]] | None = None,
    repo_roots: Callable[[Path, str], Path] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create or re-attach the Lead worktree of the loop at *mailbox_rel*.

    *declared* are the root mailbox's PLAN.md ``repos:`` entries
    (``{"name", "path" (the declared checkout), "raw_path", "base"}``); each
    gets its own aggregate worktree on the same branch name: a relative path
    under the home repo is re-created at ``<lead-wt>/<path>``, an absolute
    one at ``repo_roots(path, name) / lead-<slug>``. Returns (record,
    created). Raises RootFreeError without leaving a half-made loop behind
    (a record is written first, so a crash after ``worktree add`` still
    leaves an owned, resumable entry).
    """
    home = wt.repo_toplevel(home)
    slug = loop_slug(mailbox_rel)
    branch = branch_name(slug)
    owner = record_id(slug)
    with wt.repo_lock(home, "lead"):
        record = load_record(wt, home, slug)
        if record and record.get("state") == "retained":
            raise RootFreeError(
                f"the previous run of {mailbox_rel} landed but its Lead worktree "
                f"{record.get('path')} was retained ({record.get('retained_reason')}); "
                f"run `trioctl omnigent abandon --mailbox {mailbox_rel}` after checking it"
            )
        if record and record.get("state") == "landed":
            raise RootFreeError(
                f"{mailbox_rel} already landed; `trioctl omnigent land --mailbox "
                f"{mailbox_rel}` finishes removing its Lead worktree"
            )
        if active(record):
            if record.get("mailbox_rel") != mailbox_rel:
                raise RootFreeError(
                    f"ledger record {owner} belongs to mailbox {record.get('mailbox_rel')}, "
                    f"not {mailbox_rel} (slug collision); rename one mailbox"
                )
            _reattach(wt, home, record, owner)
            write_aggregates(record)
            return record, False
        target_ref = target or _branch_of(wt, home)
        if not target_ref:
            raise RootFreeError(
                f"{home} is not on a branch (detached HEAD): pass --target <branch> "
                "for the branch this loop lands onto"
            )
        target_sha = wt.rev(home, f"refs/heads/{target_ref}")
        if target_sha is None:
            raise RootFreeError(f"target branch {target_ref!r} does not exist in {home}")
        if wt.rev(home, f"refs/heads/{branch}") is not None:
            raise RootFreeError(
                f"branch {branch} already exists but no live Lead worktree record "
                f"owns it (an earlier run of {mailbox_rel} kept it?): merge or delete "
                f"it (`git branch -D {branch}`) before starting this loop again"
            )
        path = (Path(worktree_root) / owner).resolve()
        if path.exists():
            raise RootFreeError(f"Lead worktree path {path} already exists; remove it first")
        if path == home or str(path).startswith(str(home) + os.sep):
            raise RootFreeError(f"worktree root {worktree_root} is inside {home}")
        repos_plan: dict[str, dict[str, Any]] = {}
        for repo in declared or []:
            main = wt.repo_toplevel(Path(repo["path"]))
            rtarget = repo.get("base") or _branch_of(wt, main)
            if not rtarget:
                raise RootFreeError(
                    f"declared repo {repo['name']} ({main}) is detached and declares no "
                    "base: branch to land onto"
                )
            rsha = wt.rev(main, f"refs/heads/{rtarget}")
            if rsha is None:
                raise RootFreeError(
                    f"declared repo {repo['name']}: base branch {rtarget!r} does not exist"
                )
            if wt.rev(main, f"refs/heads/{branch}") is not None:
                raise RootFreeError(
                    f"declared repo {repo['name']} already has a branch {branch}: "
                    "merge or delete it first"
                )
            try:
                nested_rel = Path(repo["path"]).resolve().relative_to(home).as_posix()
            except ValueError:
                nested_rel = None
            if nested_rel:
                agg = path / nested_rel
            else:
                base_root = repo_roots(main, repo["name"]) if repo_roots else (
                    wt.default_worktree_root(main, name=repo["name"])
                )
                agg = Path(base_root) / owner
            if agg.exists() and not nested_rel:
                raise RootFreeError(f"aggregate path {agg} for repo {repo['name']} already exists")
            repos_plan[repo["name"]] = {
                "main": str(main), "path": str(agg), "branch": branch,
                "target_ref": rtarget, "target_base": rsha, "nested": bool(nested_rel),
            }
        live = path / mailbox_rel
        record = {
            "schema": 1,
            "id": owner,
            "kind": "lead",
            "slice": "lead",
            "role": "lead",
            "slug": slug,
            "repo": str(home),
            "common_dir": str(wt.common_dir(home)),
            "mailbox": str(live),
            "mailbox_rel": mailbox_rel,
            "live_mailbox": str(live),
            "root_mailbox": str(home / mailbox_rel),
            "path": str(path),
            "branch": branch,
            "aggregate_ref": f"refs/heads/{branch}",
            "target_ref": target_ref,
            "target_base": target_sha,
            "base": target_sha,
            "seed": None,
            "repos": repos_plan,
            "verified": {},
            "landed": {},
            "state": "creating",
            "created_at": time.time(),
            "creator": wt.process_identity(os.getpid()),
        }
        save_record(wt, home, record)
        made: list[tuple[Path, Path, str]] = []
        try:
            record["admin_dir"] = _add_worktree(wt, home, path, branch, target_sha, owner)
            made.append((home, path, target_sha))
            record["seed"] = _seed(wt, home, path, mailbox_rel)
            for name, info in repos_plan.items():
                info["admin_dir"] = _add_worktree(
                    wt, Path(info["main"]), Path(info["path"]), branch, info["target_base"], owner
                )
                made.append((Path(info["main"]), Path(info["path"]), info["target_base"]))
        except BaseException as exc:
            _undo_create(wt, made, branch, record.get("seed"))
            record["state"] = "removed"
            record["create_failed"] = f"{type(exc).__name__}: {exc}"
            save_record(wt, home, record)
            raise
        record["state"] = "active"
        save_record(wt, home, record)
        write_aggregates(record)
        return record, True


def _undo_create(wt: Any, made: list[tuple[Path, Path, str]], branch: str, seed: str | None) -> None:
    """Roll back a failed :func:`begin`: remove the worktrees it added (last
    first) and their branches while they still point where begin left them."""
    for repo, path, start in reversed(made):
        _git(wt, repo, "worktree", "remove", "--force", str(path), check=False)
        tip = wt.rev(repo, f"refs/heads/{branch}")
        if tip is not None and tip in {start, seed}:
            _git(wt, repo, "update-ref", "-d", f"refs/heads/{branch}", tip, check=False)


def _reattach(wt: Any, home: Path, record: dict[str, Any], owner: str) -> None:
    """Resume: the Lead worktree (and declared aggregates) exist, or are
    re-created from their ``trio/<slug>`` branches; never re-seeded."""
    todo = [(home, Path(record["path"]))] + [
        (Path(info["main"]), Path(info["path"])) for info in (record.get("repos") or {}).values()
    ]
    for repo, path in todo:
        if path.is_dir():
            if not _owned(wt, path, owner):
                raise RootFreeError(f"{path} is not the Lead worktree {owner} (owner marker)")
            if _branch_of(wt, path) != record["branch"]:
                raise RootFreeError(
                    f"Lead worktree {path} is on {_branch_of(wt, path) or 'detached HEAD'}, "
                    f"expected {record['branch']}"
                )
            continue
        if wt.rev(repo, f"refs/heads/{record['branch']}") is None:
            raise RootFreeError(
                f"Lead worktree {path} and its branch {record['branch']} are both gone; "
                "nothing to resume (abandon the loop record first)"
            )
        _add_worktree(wt, repo, path, record["branch"], None, owner)


def add_detached_eval(
    wt: Any, repo: Path, path: Path, sha: str, *, mailbox: Path | None, repo_name: str | None,
) -> dict[str, Any]:
    """A task-owned evaluator worktree detached at *sha* at an exact *path*.

    The integration-eval of a root-free run re-creates the declared repos'
    nested layout inside its own detached home worktree
    (``<eval-wt>/<nested path>``), so every relative path of PLAN.md and
    the Lead's checks resolves as in the Lead worktree. Same ledger record
    shape as ``worker_worktrees.create(detach_at=...)`` (``kind: "eval"``),
    so the unchanged mark/finish/cleanup guards apply.
    """
    import uuid

    repo = wt.repo_toplevel(repo)
    base = wt.rev(repo, sha)
    if base is None:
        raise RootFreeError(f"no commit {sha} in {repo}")
    worker_id = f"eval-integration-{wt._sanitize(repo_name or 'repo')}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "schema": wt.SCHEMA,
        "id": worker_id,
        "slice": "integration",
        "role": "evaluator",
        "run_id": None,
        "repo": str(repo),
        "common_dir": str(wt.common_dir(repo)),
        "mailbox": str(Path(mailbox).resolve()) if mailbox else None,
        "path": str(path),
        "branch": None,
        "aggregate_ref": _git(wt, repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip(),
        "base": base,
        "kind": "eval",
        "state": "created",
        **({"repo_name": repo_name} if repo_name and repo_name != "home" else {}),
        "created_at": time.time(),
        "creator": wt.process_identity(os.getpid()),
    }
    wt.save_record(repo, record)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(wt, repo, "worktree", "add", "--detach", str(path), base)
    except (OSError, RootFreeError) as exc:
        wt._retain(repo, record, "create_failed", str(exc))
        raise RootFreeError(f"cannot create evaluator worktree {path}: {exc}") from exc
    record["admin_dir"] = _mark_owner(wt, path, worker_id)
    try:
        neutralised = wt.neutralise_tracked_cursor(path)
    except (OSError, wt.WorktreeError):
        neutralised = []
    if neutralised:
        record["neutralised_cursor"] = neutralised
    wt.save_record(repo, record)
    return record


# ------------------------------------------------------------------ setup


def setup_commands(path: Path, profile: Any = None) -> tuple[list[str], str] | None:
    """Dependency setup for a new Lead worktree: the profile's
    ``worktree_setup`` list, else the repository's ``.cursor/worktrees.json``
    (``setup-worktree-unix`` / ``setup-worktree``: a command list, or one
    script path), else None."""
    if profile:
        items = [profile] if isinstance(profile, str) else list(profile)
        return [str(c) for c in items if str(c).strip()], "profile worktree_setup"
    config = path / ".cursor" / "worktrees.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("setup-worktree-unix", "setup-worktree"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            for candidate in (path / ".cursor" / value, path / value):
                if candidate.is_file():
                    return [f"sh {shlex.quote(str(candidate))}"], f".cursor/worktrees.json {key}"
            return [value], f".cursor/worktrees.json {key}"
        if isinstance(value, list) and value:
            return [str(c) for c in value if str(c).strip()], f".cursor/worktrees.json {key}"
    return None


def run_setup(
    path: Path, commands: list[str], *, home: Path, timeout: float | None
) -> tuple[bool, str]:
    """Run *commands* in *path* (``ROOT_WORKTREE_PATH`` = the repo root, as
    Cursor's own worktree setup does). (ok, output tail)."""
    env = dict(os.environ, ROOT_WORKTREE_PATH=str(home))
    for command in commands:
        try:
            proc = subprocess.run(
                ["sh", "-c", command], cwd=str(path), env=env, capture_output=True,
                text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            tail = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, str) else ""
            return False, f"`{command}` timed out after {timeout:g}s\n" + _tail(tail)
        if proc.returncode != 0:
            return False, f"`{command}` exited {proc.returncode}\n" + _tail(proc.stdout + proc.stderr)
    return True, ""


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(str(text).strip().splitlines()[-lines:])


# ------------------------------------------------------------------ land


def aggregates(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Land order: declared repos first, home last (it carries the mailbox)."""
    out = [
        {"name": name, "path": Path(info["path"]), "main": Path(info["main"]),
         "target": info["target_ref"], "home": False}
        for name, info in (record.get("repos") or {}).items()
    ]
    out.append({"name": "home", "path": Path(record["path"]), "main": Path(record["repo"]),
                "target": record["target_ref"], "home": True})
    return out


def _judge(
    wt: Any,
    agg: dict[str, Any],
    base: str,
    tip: str,
    *,
    mailbox_rel: str | None,
    watched: list[str],
    full_check: str | None,
    timeout: float | None,
    what: str,
) -> tuple[bool, str]:
    """Is *tip* verified relative to the verified *base* (r16 re-land rule)?

    (True, note) when nothing but this or other mailboxes changed, or the
    change touches none of this loop's ``writes:``/``reads:`` and the repo's
    ``full_check:`` passes at *tip* (run in the aggregate, which is at
    *tip*: its installed dependencies are what the Lead gate used);
    (False, why) when a new integration-eval must decide.
    """
    path = agg["path"]
    changed = [p for p in _changed(wt, path, base, tip) if not _in(p, mailbox_rel)]
    product = [p for p in changed if not wt._foreign_mailbox(path, p)]
    if not product:
        return True, f"{what}: only mailbox files changed"
    touched = sorted({p for p in product for w in watched if _overlap(p, w)})
    if touched:
        return False, (
            f"{what} touches this loop's writes/reads in repo {agg['name']}: "
            + ", ".join(touched[:8])
        )
    if not full_check:
        return False, f"{what} changed {len(product)} product path(s) and repo {agg['name']} has no full_check:"
    try:
        proc = subprocess.run(
            ["sh", "-c", full_check], cwd=str(path), capture_output=True, text=True,
            timeout=timeout,
        )
        ok, output = proc.returncode == 0, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        ok, output = False, f"timed out after {timeout:g}s"
    last = (_tail(output, 1) or "no output").strip()
    if not ok:
        return False, f"{what}: full_check failed in repo {agg['name']} ({last})"
    return True, f"{what}; full_check PASS in repo {agg['name']} ({last})"


def _mailbox_pending(wt: Any, lead: Path, rel: str) -> list[str]:
    out = _git(wt, lead, "status", "--porcelain=v1", "--untracked-files=all", "--", rel).stdout
    return [wt._status_path(line) for line in out.splitlines() if line.strip()]


def _root_blockers(
    wt: Any, checkout: Path, changed: set[str], rel: str | None, seed: str | None, lead: Path
) -> tuple[list[str], list[tuple[str, str, bytes]]]:
    """(blocking lines, removable pre-seed copies) at the target's checkout.

    Local edits/untracked files there that the fast-forward would overwrite
    block the land. Inside this loop's own mailbox, an untracked or
    modified file byte-identical to the seeded copy (or to what lands) is the
    pre-seed copy the main session wrote; it is set aside instead."""
    entries = wt.status_entries(checkout)
    blockers: list[str] = []
    removable: list[tuple[str, str, bytes]] = []
    for line in entries:
        rel_path = wt._status_path(line)
        if rel_path not in changed:
            continue
        code = line[:2]
        if rel and _in(rel_path, rel) and code in ("??", " M"):
            current = (checkout / rel_path).read_bytes() if (checkout / rel_path).is_file() else None
            landing = (lead / rel_path).read_bytes() if (lead / rel_path).is_file() else None
            seeded = _blob(lead, seed, rel_path) if seed else None
            if current is not None and current in (seeded, landing):
                removable.append((rel_path, code, current))
                continue
            blockers.append(f"{rel_path} (root mailbox file edited after the loop started)")
            continue
        blockers.append(f"{rel_path} ({'untracked' if code == '??' else 'local change'})")
    return blockers, removable


def land(
    wt: Any,
    record: dict[str, Any],
    *,
    core: Any,
    iteration: int,
    pins: dict[str, str],
    watched: dict[str, list[str]],
    full_checks: dict[str, str],
    timeout: float | None = None,
    pre_land: Callable[[], Any] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Land the verified loop branch onto its target(s) (r16a, SHIP only).

    Under every involved repository's ``land`` lock: (1) per aggregate,
    make sure the tip is verified and merge a moved target into the loop
    branch (merge, never rebase: recorded shas stay valid); a conflict is
    aborted (``needs_land``/``land-conflict``), a clean merge is judged by
    :func:`_judge` (``reverify`` when an integration-eval must decide);
    (2) declared repos first, home last: fast-forward the checkout that has
    the target checked out (``git merge --ff-only``: git refuses on
    overlapping local edits -> ``needs_land``/``land-blocked``), or
    compare-and-swap ``update-ref`` when it is checked out nowhere (never
    update-ref a checked-out branch). The home land first commits the live
    mailbox's final STATE.md (``status: shipped``, ``landed: <sha>``) and LOG
    lines as ``loop: land <mailbox>`` so the root shows the finished loop.
    Returns ``{"status": "landed"|"needs_land"|"reverify"|"error", ...}``.
    """
    emit = log or (lambda _line: None)
    home = Path(record["repo"])
    lead = Path(record["path"])
    rel = record["mailbox_rel"]
    live = Path(record["live_mailbox"])
    state_path = live / "STATE.md"
    aggs = aggregates(record)
    for agg in aggs:
        if not agg["path"].is_dir():
            return {"status": "error", "detail": f"aggregate {agg['path']} of repo {agg['name']} is missing"}
    locks = sorted({str(wt.common_dir(a["path"])): a["path"] for a in aggs}.items())
    with contextlib.ExitStack() as stack:
        for _key, path in locks:
            stack.enter_context(wt.repo_lock(path, "land"))
        if pre_land is not None:
            pre_land()
        notes: list[str] = []
        for _attempt in range(3):
            # Phase 1: verified tips, targets merged in where they moved.
            for agg in aggs:
                path, target = agg["path"], agg["target"]
                if _merging(wt, path):
                    return {"status": "needs_land", "phase": "land-conflict",
                            "detail": f"a merge is in progress in {path}; resolve or abort it, then run `trioctl omnigent land`"}
                tip = wt.rev(path, "HEAD")
                target_sha = wt.rev(path, f"refs/heads/{target}")
                if tip is None or target_sha is None:
                    return {"status": "error", "detail": f"repo {agg['name']}: no HEAD or target {target}"}
                mailbox_rel = rel if agg["home"] else None
                pin = pins.get(agg["name"]) or ""
                verified = record.setdefault("verified", {})
                if verified.get(agg["name"]) != tip:
                    if not pin:
                        return {"status": "reverify", "detail": f"repo {agg['name']} has no evaluated pin"}
                    ok, why = _judge(
                        wt, agg, pin, tip, mailbox_rel=mailbox_rel,
                        watched=watched.get(agg["name"]) or [],
                        full_check=full_checks.get(agg["name"]), timeout=timeout,
                        what=f"trio/{record['slug']} changed since the evaluated pin {pin[:12]}",
                    )
                    if not ok:
                        return {"status": "reverify", "detail": why}
                    verified[agg["name"]] = tip
                    save_record(wt, home, record)
                if wt.is_ancestor(path, target_sha, tip):
                    continue
                merge = _git(
                    wt, path, "merge", "--no-ff", "--no-edit", "-q", "-m",
                    f"land: merge {target}@{target_sha[:12]} into {record['branch']}",
                    target_sha, check=False,
                )
                if merge.returncode != 0:
                    conflicts = _out(wt, path, "diff", "--name-only", "--diff-filter=U").splitlines()
                    _git(wt, path, "merge", "--abort", check=False)
                    theirs = _out(
                        wt, path, "log", "--format=%h %s", f"{tip}..{target_sha}", "--",
                        *(conflicts or ["."]),
                    ).splitlines()
                    return {
                        "status": "needs_land", "phase": "land-conflict",
                        "detail": (
                            f"merging {target}@{target_sha[:12]} into {record['branch']} "
                            f"conflicts in repo {agg['name']}: {', '.join(conflicts[:8]) or 'unknown paths'}"
                            f" (their commits: {'; '.join(theirs[:5]) or 'n/a'}); resolve in {path} "
                            "and commit, then run `trioctl omnigent land`"
                        ),
                    }
                merged = wt.rev(path, "HEAD")
                ok, why = _judge(
                    wt, agg, tip, merged, mailbox_rel=mailbox_rel,
                    watched=watched.get(agg["name"]) or [],
                    full_check=full_checks.get(agg["name"]), timeout=timeout,
                    what=f"target {target} moved to {target_sha[:12]}",
                )
                if not ok:
                    return {"status": "reverify", "detail": why}
                verified[agg["name"]] = merged
                save_record(wt, home, record)
                notes.append(f"merged {agg['name']}:{target}@{target_sha[:12]} ({why})")
                emit(f"land: merged {target}@{target_sha[:12]} into {record['branch']} in repo {agg['name']}: {why}")
            record["verified"] = verified
            # Phase 2: land, declared repos first, home last.
            moved = False
            for agg in aggs:
                result = _land_one(wt, record, agg, core=core, iteration=iteration, notes=notes)
                if result is None:
                    continue
                if result == "moved":
                    moved = True
                    break
                return result
            if not moved:
                record["state"] = "landed"
                save_record(wt, home, record)
                return {
                    "status": "landed",
                    "landed": dict(record.get("landed") or {}),
                    "detail": "; ".join(notes),
                }
        return {"status": "needs_land", "phase": "land-starved",
                "detail": "the target branch kept moving during the land"}


def _merging(wt: Any, path: Path) -> bool:
    try:
        git_dir = Path(_out(wt, path, "rev-parse", "--absolute-git-dir"))
    except RootFreeError:
        return False
    return (git_dir / "MERGE_HEAD").exists()


def _land_one(
    wt: Any, record: dict[str, Any], agg: dict[str, Any], *, core: Any, iteration: int,
    notes: list[str],
) -> dict[str, Any] | str | None:
    """Land one aggregate; None = done, "moved" = target moved (retry)."""
    home = Path(record["repo"])
    path, target, main = agg["path"], agg["target"], agg["main"]
    rel = record["mailbox_rel"] if agg["home"] else None
    tip = wt.rev(path, "HEAD")
    target_sha = wt.rev(path, f"refs/heads/{target}")
    landed = record.setdefault("landed", {})
    if target_sha == tip and (not agg["home"] or landed.get("home") == tip):
        landed[agg["name"]] = tip
        return None
    if tip is None or target_sha is None or not wt.is_ancestor(path, target_sha, tip):
        return "moved"
    checkout = checkout_of(wt, main, target)
    lead = Path(record["path"])
    changed = set(_changed(wt, path, target_sha, tip))
    if agg["home"]:
        changed |= set(_mailbox_pending(wt, lead, rel))
        changed |= {f"{rel}/STATE.md", f"{rel}/LOG.md"}
    bound = _session_bound_landing(wt, path, tip, changed)
    if bound:
        return {
            "status": "needs_land", "phase": "land-blocked",
            "detail": (
                f"repo {agg['name']}: {record['branch']} would land session-bound "
                f"Omnigent Cursor config ({'; '.join(bound[:4])}); revert it on the loop "
                "branch, then run `trioctl omnigent land`"
            ),
        }
    removable: list[tuple[str, str, bytes]] = []
    if checkout is not None:
        blockers, removable = _root_blockers(
            wt, checkout, changed, rel, record.get("seed"), lead,
        )
        if blockers:
            return {
                "status": "needs_land", "phase": "land-blocked",
                "detail": (
                    f"uncommitted changes in {checkout} (where {target} is checked out) "
                    f"overlap the land: {', '.join(blockers[:8])}; commit, stash or move "
                    "them, then run `trioctl omnigent land`"
                ),
            }
    saved: dict[str, bytes | None] = {}
    verified_tip = tip
    if agg["home"]:
        live = Path(record["live_mailbox"])
        for name in ("STATE.md", "LOG.md"):
            f = live / name
            saved[name] = f.read_bytes() if f.is_file() else None
        for other in aggregates(record)[:-1]:
            sha = landed.get(other["name"])
            if sha:
                core._append_log(
                    live,
                    f"- iter {iteration} | loop | landed {record['branch']} @{sha[:12]} "
                    f"onto {other['name']}:{other['target']}",
                )
        core._append_log(
            live,
            f"- iter {iteration} | loop | landed {record['branch']} @{tip[:12]} onto {target}"
            + (f" ({'; '.join(notes)})" if notes else ""),
        )
        core._update_state(live / "STATE.md", {
            "status": "shipped", "phase": "landed", "landed": tip, "target_ref": target,
        })
        _git(wt, lead, "add", "-A", "--", rel)
        commit = _git(wt, lead, "commit", "-q", "-m", f"loop: land {rel} (iteration {iteration})",
                      check=False)
        if commit.returncode != 0:
            _undo_land_commit(wt, lead, verified_tip, live, saved)
            return {"status": "error", "detail": "could not commit the land record: "
                    + _tail(commit.stdout + commit.stderr, 3)}
        tip = wt.rev(lead, "HEAD")
    if checkout is not None:
        for rel_path, _code, _data in removable:
            (checkout / rel_path).unlink()
        ff = _git(wt, checkout, "merge", "--ff-only", "-q", tip, check=False)
        if ff.returncode != 0:
            for rel_path, _code, data in removable:
                target_file = checkout / rel_path
                if not target_file.exists():
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(data)
            if agg["home"]:
                _undo_land_commit(wt, lead, verified_tip, Path(record["live_mailbox"]), saved)
            return {
                "status": "needs_land", "phase": "land-blocked",
                "detail": f"git merge --ff-only in {checkout} refused: "
                + _tail(ff.stdout + ff.stderr, 6).replace("\n", " "),
            }
    else:
        cas = _git(wt, path, "update-ref", f"refs/heads/{target}", tip, target_sha, check=False)
        if cas.returncode != 0:
            if agg["home"]:
                _undo_land_commit(wt, lead, verified_tip, Path(record["live_mailbox"]), saved)
            return "moved"
    landed[agg["name"]] = tip
    save_record(wt, home, record)
    return None


def _session_bound_landing(wt: Any, path: Path, tip: str, changed: set[str]) -> list[str]:
    """Session-bound Omnigent entries the land would publish in a tracked
    project ``.cursor/{mcp,hooks}.json`` (the Lead worktree hides its own
    launch config from its index, so this is a last guard)."""
    import tempfile

    problems: list[str] = []
    for rel in wt.OWNED_CURSOR_FILES:
        if rel not in changed:
            continue
        data = _blob(path, tip, rel)
        if data is None:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / Path(rel).name
            probe.write_bytes(data)
            found = (
                wt._session_bound_mcp_servers(probe) if rel.endswith("mcp.json")
                else wt._omnigent_hooks(probe)
            )
        problems += [f"{rel}: {item.split(': ', 1)[-1].replace(str(probe), rel)}" for item in found]
    return problems


def _undo_land_commit(
    wt: Any, lead: Path, verified: str, live: Path, saved: dict[str, bytes | None]
) -> None:
    """Drop the unlanded ``loop: land`` commit; restore STATE.md/LOG.md bytes."""
    if wt.rev(lead, "HEAD") != verified:
        _git(wt, lead, "reset", "-q", "--mixed", verified, check=False)
    for name, data in saved.items():
        f = live / name
        if data is None:
            with contextlib.suppress(OSError):
                f.unlink()
        else:
            f.write_bytes(data)


# ------------------------------------------------------------------ teardown


def _copy_runtime(live: Path, dest: Path) -> list[str]:
    """Copy the live mailbox's ignored driver runtime files to the root copy."""
    copied: list[str] = []
    if not live.is_dir():
        return copied
    dest.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_COPY:
        src = live / name
        if src.is_dir() and not src.is_symlink():
            for item in sorted(src.rglob("*")):
                if item.is_dir() or item.is_symlink():
                    continue
                if item == live.joinpath(*AGGREGATES_FILE):
                    continue
                target = dest / item.relative_to(live)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied.append(str(item.relative_to(live)))
        elif src.is_file() and not src.is_symlink():
            target = dest / name
            if target.exists() and name not in RUNTIME_OVERWRITE:
                continue
            shutil.copy2(src, target)
            copied.append(name)
    return copied


def _ignored_blockers(wt: Any, path: Path, home_copy: Path | None, allow: tuple[str, ...]) -> list[str]:
    """Ignored content ``git worktree remove`` would silently delete that is
    not disposable/rebuildable, Omnigent residue, copied runtime state
    (*allow* prefixes), or a byte-identical copy of the root's own file."""
    blocking = []
    for rel in wt.ignored_entries(path):
        rel_c = rel.rstrip("/")
        if any(_in(rel_c, a) or _in(a, rel_c) for a in allow):
            continue
        if wt._disposable_ignored(rel) or wt._rebuildable_ignored(path, rel):
            continue
        if wt._owned_ignored_cursor(path, rel) is not None:
            continue
        if home_copy is not None and (path / rel_c).is_file():
            other = home_copy / rel_c
            try:
                if other.is_file() and other.read_bytes() == (path / rel_c).read_bytes():
                    continue
            except OSError:
                pass
        blocking.append(rel)
    return blocking


def remove_worktree(
    wt: Any, repo: Path, path: Path, *, owner: str, branch: str,
    home_copy: Path | None = None, allow: tuple[str, ...] = (),
) -> str | None:
    """Owner-verified, non-forced removal of one Lead worktree; None = removed,
    else the reason it was retained."""
    if not path.exists():
        return None
    entry = next((e for e in wt.worktree_list(repo) if Path(e.get("worktree", "")) == path), None)
    if entry is None:
        return "not_registered"
    if entry.get("branch") != f"refs/heads/{branch}" or not _owned(wt, path, owner):
        return "uncertain_ownership"
    users = [pid for pid in wt.processes_using(path) if pid != os.getpid()]
    if users:
        return f"active_session: processes using it: {users[:10]}"
    entries, residue = wt._split_status(path, wt.status_entries(path))
    if entries:
        return "dirty: " + "; ".join(entries[:5])
    ignored = _ignored_blockers(wt, path, home_copy, allow)
    if ignored:
        return "ignored_content: " + "; ".join(ignored[:5])
    for rel in residue:
        if wt.owned_residue(path, rel):
            (path / rel).unlink()
    for rel in wt.OWNED_CURSOR_FILES:
        f = path / rel
        if f.is_file() and not f.is_symlink() and _git(
            wt, path, "ls-files", "--error-unmatch", "--", rel, check=False
        ).returncode != 0 and wt.owned_residue(path, rel):
            f.unlink()
    with contextlib.suppress(OSError):
        (path / ".cursor").rmdir()
    proc = _git(wt, repo, "worktree", "remove", str(path), check=False)
    if proc.returncode != 0:
        return "worktree_remove_failed: " + proc.stderr.strip()
    return None


def _delete_branch(wt: Any, repo: Path, branch: str, keep_if: str | None) -> str | None:
    """CAS-delete ``refs/heads/<branch>`` once its tip is contained in
    *keep_if* (the landed target); None = deleted/absent, else why kept."""
    tip = wt.rev(repo, f"refs/heads/{branch}")
    if tip is None:
        return None
    if keep_if is not None and not wt.is_ancestor(repo, tip, keep_if):
        return f"{branch} tip {tip[:12]} is not contained in the landed target"
    if checkout_of(wt, repo, branch) is not None:
        return f"{branch} is still checked out"
    proc = _git(wt, repo, "update-ref", "-d", f"refs/heads/{branch}", tip, check=False)
    return None if proc.returncode == 0 else proc.stderr.strip()


def teardown(
    wt: Any, record: dict[str, Any], *, keep_branch: bool = False, abandon: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """After a successful land (or an explicit abandon): copy the runtime
    files home, remove the declared aggregates and the Lead worktree
    (owner-verified; retained with a reason when unsafe), delete the loop
    branches once contained in their landed targets (kept on abandon or
    ``keep_branch``), and retire the ledger record."""
    emit = log or (lambda _line: None)
    home = Path(record["repo"])
    owner = record["id"]
    live = Path(record["live_mailbox"])
    root_mailbox = home / record["mailbox_rel"]
    result: dict[str, Any] = {"retained": {}, "branches_kept": {}}
    if abandon and Path(record["path"]).is_dir():
        # Keep the driver's own uncommitted mailbox state on the kept branch
        # (never lost, and the worktree is then clean to remove).
        lead = Path(record["path"])
        _git(wt, lead, "add", "-A", "--", record["mailbox_rel"], check=False)
        if _git(wt, lead, "diff", "--cached", "--quiet", check=False).returncode != 0:
            _git(wt, lead, "commit", "-q", "-m", f"loop: abandon {record['mailbox_rel']}",
                 check=False)
    copied = _copy_runtime(live, root_mailbox)
    if copied:
        emit(f"copied {len(copied)} runtime file(s) of {live} to {root_mailbox}")
    allow = tuple(
        f"{record['mailbox_rel']}/{name}" for name in (*RUNTIME_COPY, *RUNTIME_SKIP)
    )
    nested = [
        (name, info) for name, info in (record.get("repos") or {}).items()
    ]
    nested.sort(key=lambda item: not item[1].get("nested"))
    for name, info in nested:
        why = remove_worktree(
            wt, Path(info["main"]), Path(info["path"]), owner=owner, branch=record["branch"],
            home_copy=Path(info["main"]),
        )
        if why:
            result["retained"][name] = why
    if not result["retained"]:
        allow += tuple(
            Path(info["path"]).relative_to(Path(record["path"])).as_posix()
            for info in (record.get("repos") or {}).values() if info.get("nested")
        )
        why = remove_worktree(
            wt, home, Path(record["path"]), owner=owner, branch=record["branch"],
            home_copy=home, allow=allow,
        )
        if why:
            result["retained"]["home"] = why
    if not keep_branch and not abandon:
        for agg in aggregates(record):
            if agg["name"] in result["retained"]:
                continue
            repo = agg["main"]
            landed = (record.get("landed") or {}).get(agg["name"])
            why = _delete_branch(wt, repo, record["branch"], landed)
            if why:
                result["branches_kept"][agg["name"]] = why
    if result["retained"]:
        record["state"] = "retained"
        record["retained_reason"] = "; ".join(f"{k}: {v}" for k, v in result["retained"].items())
    else:
        record["state"] = "removed"
        record["removed_at"] = time.time()
    if abandon:
        record["abandoned_at"] = time.time()
    save_record(wt, home, record)
    return result

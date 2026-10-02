"""The root-free Lead worktree for trio-opencode: one private worktree per
loop mailbox, on branch ``trio/<slug>``, forked from the target branch's
tip, seeded with the root mailbox's files.

Standalone: this module never imports ``omnigent/root_free.py`` (read only
for semantics while writing this file) and has no dependency on the other
``trio_opencode`` submodules — only ``git`` and the stdlib. It intentionally
implements a much smaller contract than the Omnigent version: no
builder/eval worktree bookkeeping (that lives in ``steplib``'s ownership
ledger, scoped inside the Lead worktree's own repo view), and no
``reverify``/pin-judged re-land rule on a moved target (``land`` here is a
plain fast-forward or compare-and-swap, same as it always was — see
``land``'s docstring for why).

See the shared spec's ``rootfree.py`` section for the exact contract:
``prepare`` creates or re-attaches the Lead worktree and seeds it;
``land`` fast-forwards (or CAS-updates) the target branch once the loop
shipped; ``teardown`` cleans up after a successful land; ``abandon`` gives
up on an unlanded loop while keeping its branch.

Multi-repo (api:RootFreeAggregates): a mailbox's PLAN.md ``repos:`` block can
declare extra product repos (``metrics/trio-metrics.py:parse_repos_block``);
``prepare(..., declared=[...])`` gives each one its own aggregate worktree on
the SAME ``trio/<slug>`` branch as home, and ``land``/``teardown``/``abandon``
walk them in the same declared-first, home-last order. This ports
``omnigent/root_free.py``'s ``begin`` (~590-760), ``write_aggregates``
(~571-588), ``aggregates()`` (~956-965) and the land/teardown/abandon
aggregate handling (~1060-1300, ~1454-1518) — read only for semantics, never
imported — minus its ``_judge``/``reverify`` re-land rule and its
builder/eval worktree bookkeeping, which this driver does not have.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: Root-mailbox paths never copied into (or read as "changed" in) the Lead
#: worktree's live mailbox: driver/session runtime, not loop content.
RUNTIME_SKIP_DIRS = (".sessions", ".opencode-runs", ".dispatch", ".lock")
RUNTIME_SKIP_FILE_RE = re.compile(
    r"^(\.opencode.*\.json|\.driver\.json|\.session\.json)$"
)
#: Files copied back from the live mailbox to the root mailbox at teardown.
TEARDOWN_RUNTIME_FILES = (".opencode-result.json", ".session.json", ".driver.json")


class RootFreeError(RuntimeError):
    """A root-free operation refused with a clear, human-readable reason;
    nothing is left half-done."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ git
def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


def _git(cwd: Path, *args: str) -> str:
    r = _run(cwd, *args)
    if r.returncode != 0:
        raise RootFreeError(f"git {' '.join(args)} (in {cwd}) failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _git_ok(cwd: Path, *args: str) -> bool:
    return _run(cwd, *args).returncode == 0


def _worktree_list(repo: Path) -> list[dict[str, str | None]]:
    out = _run(repo, "worktree", "list", "--porcelain").stdout
    items: list[dict[str, str | None]] = []
    cur: dict[str, str | None] = {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch refs/heads/"):
            cur["branch"] = line[len("branch refs/heads/"):]
    return items


def _git_common_dir(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


# ------------------------------------------------------------------ slug
def loop_slug(mailbox_rel: str) -> str:
    """Same algorithm as ``metrics/trio-metrics.py:loop_slug`` (kept
    byte-identical; a test compares the two directly)."""
    text = str(mailbox_rel).strip().strip("/").replace("/", "--")
    return (_SLUG_RE.sub("-", text).strip("-") or "loop")[:120]


def branch_name(slug: str) -> str:
    return f"trio/{slug}"


# -------------------------------------------------------------- record
@dataclass
class LeadWorktree:
    kind: str
    slug: str
    branch: str
    target: str
    target_base: str
    path: str
    repo: str
    mailbox_rel: str
    seed: str | None
    created_at: str
    landed: bool = False
    landed_at: str | None = None
    abandoned: bool = False
    abandoned_at: str | None = None
    #: Declared-repo aggregates of this loop (api:RootFreeAggregates), keyed
    #: by PLAN.md ``repos:`` name: ``{main, path, branch, target_ref,
    #: target_base, nested}`` (``omnigent/root_free.py``'s ``repos_plan``
    #: entry shape, ~692-695). Empty for every single-repo loop; a record
    #: saved before this field existed loads with ``{}`` here (``from_dict``
    #: only passes known keys, so the dataclass default applies).
    repos: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def live_mailbox(self) -> Path:
        return Path(self.path) / self.mailbox_rel

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LeadWorktree":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def _as_dict(record: "LeadWorktree | dict[str, Any]") -> dict[str, Any]:
    return record.to_dict() if isinstance(record, LeadWorktree) else dict(record)


def record_path(repo: Path, slug: str) -> Path:
    return _git_common_dir(repo) / "trio-opencode" / f"lead-{slug}.json"


def load_record(repo: Path, slug: str) -> LeadWorktree | None:
    path = record_path(repo, slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return LeadWorktree.from_dict(data)


def save_record(record: "LeadWorktree | dict[str, Any]") -> None:
    data = _as_dict(record)
    repo = Path(data["repo"])
    path = record_path(repo, data["slug"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        os.write(fd, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ------------------------------------------------------------ aggregates
def aggregates_map_path(live_mailbox: str | Path) -> Path:
    """Where :func:`write_aggregates_map` writes the per-run declared-repo
    map that ``metrics/trio-metrics.py``'s ``AGGREGATES_FILE``/
    ``apply_aggregates`` read (``read_repos`` honours it): same path and
    shape as ``omnigent/root_free.py``'s own ``AGGREGATES_FILE`` /
    ``write_aggregates`` (~571-588) -- ``<live_mailbox>/.sessions/
    aggregates.json``."""
    return Path(live_mailbox) / ".sessions" / "aggregates.json"


def write_aggregates_map(record: "LeadWorktree | dict[str, Any]") -> None:
    """Port of ``omnigent/root_free.py:write_aggregates`` (~571-588): write
    (or, with no declared repos, remove) this loop's declared-repo map.
    Atomic like :func:`save_record`."""
    rec = record if isinstance(record, LeadWorktree) else LeadWorktree.from_dict(dict(record))
    path = aggregates_map_path(rec.live_mailbox)
    repos = {
        name: {"path": info["path"], "branch": info["branch"], "main": info["main"]}
        for name, info in (rec.repos or {}).items()
    }
    if not repos:
        try:
            path.unlink()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aggregates.", suffix=".json.tmp",
                               dir=str(path.parent))
    try:
        os.write(fd, (json.dumps({"schema": 1, "repos": repos}, indent=2,
                                 sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _current_branch(repo: Path) -> str | None:
    """Like :func:`_target_branch` but returns ``None`` instead of raising
    on a detached HEAD, so callers can name the repo in their own error."""
    ref = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return None if ref == "HEAD" else ref


def _declared_plan(home: Path, lead_path: Path, slug: str, branch: str,
                    decl: dict[str, Any]) -> dict[str, Any]:
    """Validate one PLAN.md ``repos:`` entry (``{"name", "path", "base"}``)
    and plan its aggregate worktree; raises :class:`RootFreeError` without
    creating anything. Ports ``omnigent/root_free.py:begin``'s
    per-declared-repo loop (~660-695): resolve the repo's toplevel, pick its
    target branch (``base``, or its current branch -- detached with no
    ``base`` is refused), refuse a pre-existing ``trio/<slug>`` branch there,
    and place the aggregate at ``<lead worktree>/<rel>`` when the repo is
    nested inside *home*, else at ``<its git-common-dir>/trio-opencode/
    agg-<slug>``."""
    name = decl["name"]
    main = Path(_git(Path(decl["path"]), "rev-parse", "--show-toplevel"))
    target_ref = decl.get("base") or _current_branch(main)
    if not target_ref:
        raise RootFreeError(
            f"declared repo {name} ({main}) is detached and declares no "
            "base: branch to land onto"
        )
    if not _git_ok(main, "rev-parse", "--verify", f"refs/heads/{target_ref}"):
        raise RootFreeError(
            f"declared repo {name}: base branch {target_ref!r} does not exist"
        )
    target_sha = _git(main, "rev-parse", target_ref)
    if _git_ok(main, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        raise RootFreeError(
            f"declared repo {name} already has a branch {branch}: merge or "
            "delete it first"
        )
    try:
        nested_rel = main.resolve().relative_to(home.resolve())
        nested = True
    except ValueError:
        nested, nested_rel = False, None
    if nested:
        agg_path = Path(lead_path) / nested_rel
    else:
        agg_path = _git_common_dir(main) / "trio-opencode" / f"agg-{slug}"
    return {
        "main": str(main), "path": str(agg_path), "branch": branch,
        "target_ref": target_ref, "target_base": target_sha, "nested": nested,
    }


# --------------------------------------------------------------- prepare
def _state_home() -> Path:
    value = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(value).expanduser() if value else Path.home() / ".local" / "state"


def _worktree_root(repo: Path) -> Path:
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
    return _state_home() / "trio-agent-loop" / "opencode-worktrees" / key


def mailbox_rel(repo: Path, mailbox: Path) -> str:
    """The mailbox's path relative to the repo (posix separators); raises
    :class:`RootFreeError` when it is not inside the repo at all. Public so
    ``cli.py``/``driver.py`` can re-derive a mailbox's slug without
    duplicating this logic."""
    try:
        rel = mailbox.resolve().relative_to(repo.resolve())
    except ValueError as exc:
        raise RootFreeError(
            f"mailbox {mailbox} is not inside repository {repo}"
        ) from exc
    return rel.as_posix()


#: Backwards-compatible private alias (kept for any internal call sites).
_mailbox_rel = mailbox_rel


def _target_branch(repo: Path) -> str:
    ref = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if ref == "HEAD":
        raise RootFreeError(
            f"repository {repo} is in detached HEAD; root-free needs a "
            "branch checked out to land onto"
        )
    return ref


def _iter_mailbox_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in RUNTIME_SKIP_DIRS]
        for name in filenames:
            if RUNTIME_SKIP_FILE_RE.match(name):
                continue
            full = Path(dirpath) / name
            yield full.relative_to(root)


#: Human-owned mailbox inputs synced from the root on re-attach (resume):
#: the loop's own state files (STATE/LOG/PLAN/REPORT/VERDICT) are never
#: re-seeded over the live copy.
HUMAN_INPUTS = ("GOAL.md", "HUMAN.md")


def _human_input_changed(src: Path, dst: Path) -> bool:
    """Whether a root human-input file should replace the live copy: GOAL.md
    whenever it differs; HUMAN.md only when it is an append-only extension of
    the live copy (never clobber an answer written to the live mailbox)."""
    new = src.read_bytes()
    try:
        old = dst.read_bytes()
    except OSError:
        return True
    if new == old:
        return False
    return src.name != "HUMAN.md" or new.startswith(old)


def _seed(worktree: Path, root_mailbox: Path, mailbox_rel: str, *,
          human_inputs_only: bool = False) -> str:
    """Copy the root mailbox's non-runtime files over the Lead worktree's
    mailbox and, if that changed anything, commit ``loop: seed
    <mailbox_rel>`` there (only mailbox paths). Returns the commit sha, or
    ``""`` when nothing changed."""
    live = worktree / mailbox_rel
    live.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    if root_mailbox.is_dir():
        for rel in _iter_mailbox_files(root_mailbox):
            src = root_mailbox / rel
            dst = live / rel
            if human_inputs_only and (str(rel) not in HUMAN_INPUTS
                                      or not _human_input_changed(src, dst)):
                continue
            data = src.read_bytes()
            try:
                if dst.read_bytes() == data:
                    continue
            except OSError:
                pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            copied.append(f"{mailbox_rel}/{rel}".lstrip("/"))
    # Commit exactly the files the seed wrote -- never other live-mailbox
    # changes (runtime .gitignore, an interrupted pass's PLAN.md/STATE.md):
    # committing those would move the Lead HEAD and make begin's reclaim
    # treat the previous run's builder branches as forked from a stale base.
    paths = [p for p in copied
             if _run(worktree, "status", "--porcelain", "--", p).stdout.strip()]
    if not paths:
        return ""
    _git(worktree, "add", "--", *paths)
    _git(worktree, "commit", "-m", f"loop: seed {mailbox_rel}", "--", *paths)
    return _git(worktree, "rev-parse", "HEAD")


def prepare(root_mailbox: str | Path, *,
            declared: list[dict[str, Any]] | None = None) -> LeadWorktree:
    """Create (or re-attach) this mailbox's Lead worktree on ``trio/<slug>``
    and seed it from the root mailbox. Returns the record; the live mailbox
    is ``record.live_mailbox``.

    *declared* (api:RootFreeAggregates) are PLAN.md ``repos:`` entries --
    ``{"name", "path" (the declared checkout's absolute path), "base"}`` --
    each PLAN.md declares beyond the implicit home repo
    (``metrics/trio-metrics.py:parse_repos_block``). On a fresh Lead
    worktree every declared repo is validated (:func:`_declared_plan`)
    *before* any worktree is created (home's included), so a
    :class:`RootFreeError` here leaves nothing half-made; the record is then
    saved -- with the full repo plan -- *before* any aggregate ``worktree
    add``, so a crash leaves a resumable entry (``omnigent/root_free.py:
    begin``'s ~590-760 order). On re-attach (resume), any declared repo not
    yet in the record gets the same treatment, and a repo whose aggregate
    worktree directory went missing is re-created on its EXISTING branch
    (never a new one). The declared-repo map
    (:func:`write_aggregates_map`/:func:`aggregates_map_path`) is rewritten
    on every call."""
    root_mailbox = Path(root_mailbox).resolve()
    repo = Path(_git(root_mailbox, "rev-parse", "--show-toplevel"))
    mailbox_rel = _mailbox_rel(repo, root_mailbox)
    slug = loop_slug(mailbox_rel)
    branch = branch_name(slug)
    target = _target_branch(repo)
    trees = _worktree_list(repo)
    existing = load_record(repo, slug)
    branch_exists = _git_ok(repo, "show-ref", "--verify", "--quiet",
                            f"refs/heads/{branch}")

    if existing is not None:
        rec_path = Path(existing.path)
        match = next(
            (t for t in trees if t.get("path") and Path(t["path"]).resolve() == rec_path.resolve()),
            None,
        )
        if match is not None:
            if match.get("branch") != existing.branch:
                raise RootFreeError(
                    f"Lead worktree record for slug {slug!r} names {rec_path}, "
                    f"but it is checked out on {match.get('branch')!r}, not "
                    f"{existing.branch!r}"
                )
            worktree_path = rec_path
        elif branch_exists:
            # The registered checkout is gone (e.g. its directory was
            # removed) but the record and branch both survive: re-create
            # the worktree directory on the existing branch.
            worktree_path = rec_path
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", str(worktree_path), existing.branch)
        else:
            raise RootFreeError(
                f"Lead worktree record for slug {slug!r} exists but neither "
                f"its worktree ({rec_path}) nor its branch ({existing.branch}) "
                "do; resolve manually before preparing this mailbox"
            )
        record = existing
    else:
        if branch_exists:
            raise RootFreeError(
                f"branch {branch!r} already exists with no trio-opencode "
                f"Lead record for it (lead-{slug}.json); resolve manually "
                "(delete the branch, or restore the record) before "
                "preparing this mailbox"
            )
        worktree_path = _worktree_root(repo) / slug
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        target_tip = _git(repo, "rev-parse", target)
        # Validate every declared repo BEFORE creating ANY worktree (home's
        # included): a RootFreeError here must leave nothing half-made
        # (omnigent/root_free.py:begin ~660-695, validated before its own
        # home `_add_worktree`).
        repos_plan: dict[str, dict[str, Any]] = {
            decl["name"]: _declared_plan(repo, worktree_path, slug, branch, decl)
            for decl in declared or []
        }
        _git(repo, "worktree", "add", "-b", branch, str(worktree_path), target_tip)
        record = LeadWorktree(
            kind="lead", slug=slug, branch=branch, target=target,
            target_base=target_tip, path=str(worktree_path), repo=str(repo),
            mailbox_rel=mailbox_rel, seed=None, created_at=_now_iso(),
            repos=repos_plan,
        )
        # Crash-safety: the record (with the full repo plan) is saved before
        # any aggregate `worktree add`, so a crash mid-way still leaves a
        # resumable entry naming every aggregate, even ones not yet created.
        save_record(record)
        for info in repos_plan.values():
            _git(Path(info["main"]), "worktree", "add", "-b", branch,
                str(info["path"]), info["target_base"])

    # Resume/repair (api:RootFreeAggregates): attach any declared repo this
    # record does not know about yet, and re-create a missing aggregate
    # worktree directory on its EXISTING branch -- never a new one, so a
    # repair never discards progress already committed on it.
    if existing is not None:
        for decl in declared or []:
            name = decl["name"]
            info = record.repos.get(name)
            if info is None:
                info = _declared_plan(repo, Path(record.path), slug, branch, decl)
                record.repos[name] = info
                save_record(record)  # before worktree add: crash-safety
                _git(Path(info["main"]), "worktree", "add", "-b", branch,
                    str(info["path"]), info["target_base"])
            elif not Path(info["path"]).is_dir():
                agg_path = Path(info["path"])
                agg_path.parent.mkdir(parents=True, exist_ok=True)
                _git(Path(info["main"]), "worktree", "add", str(agg_path), info["branch"])

    # Seed fully only a fresh Lead worktree (or one whose live mailbox is
    # missing). On re-attach (resume) the live mailbox IS the loop's state:
    # re-seeding STATE.md/LOG.md/PLAN.md from the root copy would reset them
    # to the pre-run snapshot (begin then discards committed builder work as
    # "not reusable"), so only the human inputs (HUMAN_INPUTS) are synced.
    live_goal = Path(record.path) / record.mailbox_rel / "GOAL.md"
    fresh = existing is None or not live_goal.is_file()
    seed_sha = _seed(Path(record.path), root_mailbox, record.mailbox_rel,
                     human_inputs_only=not fresh)
    if seed_sha:
        record.seed = seed_sha
    save_record(record)
    write_aggregates_map(record)
    return record


# ------------------------------------------------------------------ land
def _checkout_of(repo: Path, branch: str) -> Path | None:
    for t in _worktree_list(repo):
        if t.get("branch") == branch and t.get("path"):
            return Path(t["path"])
    return None


def _land_one(repo: Path, branch: str, target: str) -> dict[str, Any]:
    """Fast-forward (or CAS-update) *target* in *repo* to *branch*'s tip.
    Never forces, never resets. The same algorithm for the home repo and
    every declared-repo aggregate (``land``'s original single-repo body,
    factored out); see the module docstring / shared spec for the exact
    phase names. Unlike ``omnigent/root_free.py``'s ``land``/``_land_one``,
    this never merges a moved target back into the loop branch and never
    consults a ``_judge``/``reverify`` pin -- trio-opencode has no
    evaluator-pin ledger to judge against, so a moved target is reported as
    ``needs_land``/``diverged`` for a human, same as it always was."""
    tip = _git(repo, "rev-parse", branch)
    ok = _git_ok(repo, "rev-parse", "--verify", f"refs/heads/{target}")
    if not ok:
        raise RootFreeError(f"target branch {target!r} no longer exists in {repo}")
    target_tip = _git(repo, "rev-parse", target)

    if _git_ok(repo, "merge-base", "--is-ancestor", tip, target_tip):
        return {"status": "landed", "phase": "already-landed", "detail": None,
                "tip": tip, "target": target}

    if _git_ok(repo, "merge-base", "--is-ancestor", target_tip, tip):
        checkout = _checkout_of(repo, target)
        if checkout is not None:
            r = _run(checkout, "merge", "--ff-only", tip)
            if r.returncode != 0:
                return {"status": "needs_land", "phase": "land-blocked",
                        "detail": r.stderr.strip(), "tip": tip,
                        "target": target}
            return {"status": "landed", "phase": "ff-merged", "detail": None,
                    "tip": tip, "target": target}
        r = _run(repo, "update-ref", f"refs/heads/{target}", tip, target_tip)
        if r.returncode != 0:
            return {"status": "needs_land", "phase": "land-blocked",
                    "detail": r.stderr.strip(), "tip": tip, "target": target}
        return {"status": "landed", "phase": "update-ref", "detail": None,
                "tip": tip, "target": target}

    return {
        "status": "needs_land", "phase": "diverged",
        "detail": (
            f"{target} and {branch} diverged: a human merges "
            f"`{branch}` into `{target}` (or rebases {branch}), "
            "then re-run `trio-opencode land`"
        ),
        "tip": tip, "target": target,
    }


def land(record: "LeadWorktree | dict[str, Any]") -> dict[str, Any]:
    """Fast-forward (or CAS-update) ``target`` to the Lead branch's tip.
    Never forces, never resets. See the module docstring / shared spec for
    the exact phase names.

    api:RootFreeAggregates: with declared repos, lands them FIRST, home
    LAST (``omnigent/root_free.py:aggregates`` land order, ~956-965); the
    first one that is not ``landed`` stops the walk (home is never landed
    while a declared repo ``needs_land``) and its result is returned as-is,
    plus a ``repos`` map of every repo attempted so far. A single-repo
    record (no declared repos -- the default) returns exactly what it
    always did, with no ``repos`` key, so every existing caller/test is
    unaffected."""
    rec = LeadWorktree.from_dict(_as_dict(record))
    repo = Path(rec.repo)
    if not rec.repos:
        return _land_one(repo, rec.branch, rec.target)

    repos_result: dict[str, dict[str, Any]] = {}
    for name, info in rec.repos.items():
        r = _land_one(Path(info["main"]), info["branch"], info["target_ref"])
        repos_result[name] = {"status": r["status"], "phase": r["phase"], "sha": r["tip"]}
        if r["status"] != "landed":
            out = dict(r)
            out["detail"] = f"repo {name}: {r['detail']}" if r["detail"] else r["detail"]
            out["repos"] = repos_result
            return out

    result = _land_one(repo, rec.branch, rec.target)
    repos_result["home"] = {"status": result["status"], "phase": result["phase"],
                            "sha": result["tip"]}
    out = dict(result)
    out["repos"] = repos_result
    return out


# --------------------------------------------------------------- teardown
def _copy_runtime_back(live: Path, root: Path, names: tuple[str, ...]) -> list[str]:
    copied = []
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = live / name
        if not src.is_file():
            continue
        (root / name).write_bytes(src.read_bytes())
        copied.append(name)
    return copied


def _teardown_worktree(repo: Path, worktree: Path, branch: str) -> tuple[bool, bool, str | None]:
    """Remove *worktree* (only when clean) and delete *branch* (``-d``,
    merged only); the same rule for the home Lead worktree and every
    declared-repo aggregate (``omnigent/root_free.py:remove_worktree`` /
    ``_delete_branch``, simplified -- trio-opencode has no owner/residue
    bookkeeping to check). Returns (worktree_removed, branch_deleted,
    kept_reason)."""
    clean = _run(worktree, "status", "--porcelain").stdout.strip() == ""
    worktree_removed = False
    kept_reason = None
    if clean and worktree.exists():
        r = _run(repo, "worktree", "remove", str(worktree))
        if r.returncode == 0:
            worktree_removed = True
        else:
            kept_reason = r.stderr.strip()
    elif not worktree.exists():
        worktree_removed = True
    else:
        kept_reason = "worktree has uncommitted changes"

    branch_deleted = False
    if worktree_removed:
        d = _run(repo, "branch", "-d", branch)
        branch_deleted = d.returncode == 0
        if not branch_deleted:
            kept_reason = kept_reason or d.stderr.strip()
    return worktree_removed, branch_deleted, kept_reason


def teardown(record: "LeadWorktree | dict[str, Any]") -> dict[str, Any]:
    """After a successful :func:`land`: copy the live mailbox's runtime
    sidecars back to the root mailbox, remove the Lead worktree (only when
    clean apart from ignored files), delete its branch (``-d``, merged
    only), and mark the record landed.

    api:RootFreeAggregates: also removes every declared repo's aggregate
    worktree (same rule, :func:`_teardown_worktree`) -- declared repos
    first, home last, as a nested aggregate's worktree directory lives
    inside the home worktree's own directory and must be unregistered
    before ``git worktree remove`` deletes that directory
    (``omnigent/root_free.py:teardown`` ~1454-1504) -- and removes the
    declared-repo map once nothing declares a repo any more."""
    rec = LeadWorktree.from_dict(_as_dict(record))
    repo = Path(rec.repo)
    worktree = Path(rec.path)
    live_mailbox = worktree / rec.mailbox_rel
    root_mailbox = repo / rec.mailbox_rel
    copied = _copy_runtime_back(live_mailbox, root_mailbox, TEARDOWN_RUNTIME_FILES)

    repos_result: dict[str, dict[str, Any]] = {}
    for name, info in (rec.repos or {}).items():
        removed, deleted, kept = _teardown_worktree(
            Path(info["main"]), Path(info["path"]), info["branch"]
        )
        repos_result[name] = {"worktree_removed": removed, "branch_deleted": deleted,
                              "kept_reason": kept}

    worktree_removed, branch_deleted, kept_reason = _teardown_worktree(repo, worktree, rec.branch)

    if rec.repos:
        try:
            aggregates_map_path(rec.live_mailbox).unlink()
        except OSError:
            pass

    rec.landed = True
    rec.landed_at = _now_iso()
    save_record(rec)
    out = {
        "worktree_removed": worktree_removed, "branch_deleted": branch_deleted,
        "kept_reason": kept_reason, "runtime_copied": copied,
    }
    if rec.repos:
        out["repos"] = repos_result
    return out


# ---------------------------------------------------------------- abandon
def _abandon_worktree(repo: Path, worktree: Path, branch: str, *,
                      force: bool, what: str) -> tuple[bool, str | None]:
    """Remove *worktree* (only if clean, unless *force*) and report
    *branch*'s tip (kept, never deleted). Shared by home and every
    declared-repo aggregate."""
    tip = None
    if _git_ok(repo, "rev-parse", "--verify", branch):
        tip = _git(repo, "rev-parse", branch)
    if not worktree.exists():
        return True, tip
    clean = _run(worktree, "status", "--porcelain").stdout.strip() == ""
    if not clean and not force:
        raise RootFreeError(
            f"{what} {worktree} has local changes; pass force=True to "
            "abandon it anyway"
        )
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(worktree)]
    r = _run(repo, *args)
    if r.returncode != 0:
        raise RootFreeError(f"git worktree remove failed: {r.stderr.strip()}")
    return True, tip


def abandon(record: "LeadWorktree | dict[str, Any]", force: bool = False) -> dict[str, Any]:
    """Give up on an unlanded loop: remove the Lead worktree (only if clean,
    unless ``force``) and keep the branch (report its tip).

    api:RootFreeAggregates: also abandons every declared repo's aggregate
    worktree the same way, keeping its branch too (declared repos first,
    home last, same nested-worktree ordering as :func:`teardown`;
    ``omnigent/root_free.py:teardown``'s ``abandon=True`` path, ~1469-1476,
    1505). A :class:`RootFreeError` for one dirty aggregate (no ``force``)
    is raised before anything is removed."""
    rec = LeadWorktree.from_dict(_as_dict(record))
    repo = Path(rec.repo)
    worktree = Path(rec.path)

    repos_result: dict[str, dict[str, Any]] = {}
    for name, info in (rec.repos or {}).items():
        removed, tip = _abandon_worktree(
            Path(info["main"]), Path(info["path"]), info["branch"],
            force=force, what=f"aggregate worktree of repo {name!r}",
        )
        repos_result[name] = {"worktree_removed": removed, "branch_kept": info["branch"],
                              "tip": tip}

    worktree_removed, tip = _abandon_worktree(repo, worktree, rec.branch, force=force,
                                              what="Lead worktree")

    if rec.repos:
        try:
            aggregates_map_path(rec.live_mailbox).unlink()
        except OSError:
            pass

    rec.abandoned = True
    rec.abandoned_at = _now_iso()
    save_record(rec)
    out = {"worktree_removed": worktree_removed, "branch_kept": rec.branch,
          "tip": tip}
    if rec.repos:
        out["repos"] = repos_result
    return out

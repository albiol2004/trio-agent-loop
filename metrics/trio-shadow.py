#!/usr/bin/env python3
"""trio-shadow.py — shadow-mode slice contract checker for the trio pipeline.

Usage:
  python3 metrics/trio-shadow.py --mailbox <loop-or-project-dir> [--json]

Reads the machine-readable ``slices:`` block from a mailbox's PLAN.md
(schema: MAILBOX-SCHEMA.md), resolves each slice's commits in its target
git repo by the ``slice(<id>): `` commit-message prefix, and reports
declared ``writes:`` vs the files actually touched per slice:

  declared writes         every entry in the slice's ``writes:`` list
  actual touched          union of files in the slice's commits
  touched but undeclared  actual files no declared write covers
  declared but untouched  declared path entries no commit touched

``api:<Name>`` entries are interface names, not paths: they appear in
``declared writes`` for transparency but are excluded from git matching.

Shadow mode: the report is observability only — undeclared writes are
measured, never enforced. The exit code is 0 whenever the analysis itself
succeeds, including missing/non-git repos and slices with no prefixed
commits; 2 means the PLAN.md slices block is missing or malformed; 1 is a
usage error.

Active mode (the commit gate):

  python3 metrics/trio-shadow.py --mailbox <dir> --require-commits

With --require-commits, a slice that is code-changing — at least one
declared ``writes:`` entry that is neither an ``api:`` pseudo-entry nor a
path inside ``loop/`` — must have at least one matching
``slice(<id>): `` commit, or the script exits 1 listing the offenders.
Exit 0 means every code-changing slice has commits (slices that write only
``loop/`` files or ``api:`` names are exempt). A missing or malformed
slices block still exits 2, and a parseable block with no code-changing
slices never fails the gate. This is the first active interlock: drivers
run it post-Lead, pre-Evaluator, and retry the Lead once on exit 1.

Per-slice gate (v1 open-loop extension, MAILBOX-SCHEMA.md):

  python3 metrics/trio-shadow.py --mailbox <dir> --require-commits --slice <id>

``--slice <id>`` restricts both shadow mode and the commit gate to that one
slice (report and JSON output too). Without it, behaviour is unchanged.
An unknown ``--slice`` id exits 2, naming the id and the available ids.

Stdlib only — the restricted YAML shape is parsed line-based; there is no
PyYAML dependency. The block parser itself (``find_slices_block`` /
``parse_slices`` / ``SliceParseError``) is shared from trio-metrics.py, the
single source of truth for the format, and loaded here by file location.

Frozen acceptance guard (r19, MAILBOX-SCHEMA.md "Frozen acceptance"):
when the mailbox has an ``acceptance/`` pack, --require-commits also
checks every commit in ``<FROZEN base>..HEAD`` (or ``--acceptance-base``)
that touches ``<mailbox>/acceptance/``: it must be the single driver freeze
commit (``acceptance: freeze N checks (...)``, ``Acceptance-Pin:`` trailer
equal to the pack hash in that commit), a driver ``acceptance: restore``/
``acceptance: pin`` commit (same trailer rule), or a valid amend commit
(``acceptance: amend ACC-NN (evaluator|human...): ...``, touching only
``acceptance/``, with an ``## ACC-NN`` AMENDMENTS.md record per id); and the
freeze commit must precede every ``slice(<id>):`` commit of the range.
Anything else is an offender and the gate exits 1. Without a pack the
gate is unchanged.

Cross-mailbox drift report:

  python3 metrics/trio-shadow.py --report-drift [--root <repo>] [--json]

Walks every ``loop*/`` directory directly under ``--root`` (default: the
current directory) that has a PLAN.md with a parsable ``slices:`` block —
anything without one (no PLAN.md, no yaml fence, a malformed block) is
skipped silently, exactly the condition ``analyze()`` raises
``SliceParseError`` for. Runs the same per-slice analysis used by
``--mailbox`` on each and aggregates across all of them: how often a
declared ``writes:`` list turns out wrong, and — the more actionable
question for parallel dispatch — how often two slices in the *same*
iteration whose declared writes looked disjoint actually collided on a
real file (a "pairwise hazard": exactly the case where dispatching them in
parallel by declaration alone would have raced). Always exits 0 (shadow:
observability only, across mailboxes just like the single-mailbox report).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

TRIO_SHADOW_VERSION = "1.0.0"


def _load_metrics_module():
    """Load metrics/trio-metrics.py via importlib by path.

    The hyphenated filename cannot be imported normally, so the slice
    block parser is shared from trio-metrics.py and loaded here by file
    location (same pattern as trio-check.py and dashboard/serve.py). This
    keeps one source of truth for the PLAN.md ``slices:`` format.
    """
    path = Path(__file__).resolve().parent / "trio-metrics.py"
    spec = importlib.util.spec_from_file_location("trio_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trio-metrics.py from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_METRICS = _load_metrics_module()
SliceParseError = _METRICS.SliceParseError
find_slices_block = _METRICS.find_slices_block
parse_slices = _METRICS.parse_slices


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo_dir, capture_output=True, text=True
    )


def slice_commits(slice_id: str, repo_dir: Path) -> list[str] | None:
    """Commit shas whose message starts with `slice(<id>): `; None if not a git repo.

    Slice ids are restricted to kebab-case, so the id is safe to embed in
    the BRE --grep pattern (`(`/`)` are literal in basic regex).
    """
    probe = _git(repo_dir, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        return None
    proc = _git(repo_dir, "log", "--format=%H", f"--grep=^slice({slice_id}):")
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def commit_files(sha: str, repo_dir: Path) -> list[str]:
    """File names touched by one commit (handles root commits; merges resolve
    to their combined diff)."""
    proc = _git(repo_dir, "show", "--format=", "--name-only", sha)
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _normalize(path: str) -> str:
    path = path.strip().rstrip("/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _is_loop_path(path: str) -> bool:
    norm = _normalize(path)
    return norm == "loop" or norm.startswith("loop/")


def code_changing_writes(declared: list[str]) -> list[str]:
    """Declared writes that make a slice code-changing: not an ``api:``
    pseudo-entry and not a path inside ``loop/``."""
    return [
        w for w in declared
        if not w.startswith("api:") and not _is_loop_path(w)
    ]


def commit_gate_offenders(report: dict) -> list[dict]:
    """Slices that are code-changing but have zero ``slice(<id>): `` commits.

    The commit gate fails on exactly these; loop/-only and api:-only slices
    are exempt even without commits.
    """
    return [
        e for e in report["slices"]
        if code_changing_writes(e["declared_writes"]) and not e["commits"]
    ]


# --- r19 frozen acceptance guard ---------------------------------------------

ACCEPTANCE_DIR = "acceptance"
_ACC_FREEZE_RE = re.compile(r"^acceptance: freeze \d+ checks?\b")
_ACC_DRIVER_RE = re.compile(r"^acceptance: (?:restore|pin)\b")
_ACC_AMEND_RE = re.compile(
    r"^acceptance: amend (ACC-[0-9]{1,4}(?:\s*,\s*ACC-[0-9]{1,4})*) "
    r"\((evaluator|human)\b[^)]*\): \S"
)
_ACC_HASH_SKIP = ("__pycache__", ".pytest_cache", "node_modules")


def _mailbox_prefix(loop_dir: Path) -> str | None:
    """The mailbox's repo-relative path (`git rev-parse --show-prefix`)."""
    proc = _git(loop_dir, "rev-parse", "--show-prefix")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().rstrip("/")


def pack_hash_at(repo_dir: Path, sha: str, acc_rel: str) -> str | None:
    """manifest_sha256 (trio-acceptance.py) of the pack as committed in *sha*."""
    proc = _git(repo_dir, "ls-tree", "-r", "-z", sha, "--", acc_rel + "/")
    if proc.returncode != 0:
        return None
    entries = []
    for raw in proc.stdout.split("\0"):
        if not raw.strip():
            continue
        meta, _tab, path = raw.partition("\t")
        mode, _type, blob = meta.split()
        rel = path[len(acc_rel) + 1:]
        parts = rel.split("/")
        if rel == "FROZEN" or any(p in _ACC_HASH_SKIP for p in parts[:-1]) \
                or rel.endswith((".pyc", ".pyo")):
            continue
        entries.append((rel, mode, blob))
    digest = hashlib.sha256()
    for rel, mode, blob in sorted(entries):
        data = subprocess.run(["git", "cat-file", "blob", blob], cwd=repo_dir,
                              capture_output=True).stdout
        if mode == "120000":
            data = b"symlink:" + data
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def _driver_state(repo_dir: Path, loop_dir: Path) -> dict | None:
    """The loop driver's acceptance state (outside the repo; written only by
    the driver and `trioctl ... amend --human`), or None when this process
    cannot see it (another $TRIO_ACCEPTANCE_STATE/$XDG_STATE_HOME)."""
    path = Path(__file__).resolve().with_name("trio-acceptance.py")
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("trio_acceptance_shadow", path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        state = module.load_state(module.state_file(repo_dir, loop_dir))
    except Exception:  # noqa: BLE001 - fall back to the git-only rules
        return None
    return state if state.get("freeze_commit") else None


def _git_freeze_base(repo_dir: Path, acc_rel: str) -> str | None:
    """Range start from git objects (never the working tree): the parent
    of the newest commit that added FROZEN."""
    proc = _git(repo_dir, "log", "--diff-filter=A", "--format=%H", "-1", "--",
                f"{acc_rel}/FROZEN")
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not sha:
        return None
    parent = _git(repo_dir, "rev-parse", "-q", "--verify", f"{sha}^")
    if parent.returncode == 0 and parent.stdout.strip():
        return parent.stdout.strip()
    return _git(repo_dir, "hash-object", "-t", "tree", "/dev/null").stdout.strip() or None


def acceptance_offenders(loop_dir: Path, base: str | None = None,
                         notes: list[str] | None = None) -> list[str]:
    """r19 §3.3 check 2: commits touching `<mailbox>/acceptance/` that are
    not the freeze, a driver restore/pin, or a valid amend commit. [] when
    the mailbox has no pack (and no commit touched one).

    Anchors (eval-r19 finding 3): the range starts at `--acceptance-base`,
    else the driver state's `run_head`, else the parent of the commit that
    added FROZEN -- never the working tree. A commit *subject* proves
    nothing: with the driver state visible, freeze/pin/restore commits must
    be the driver's recorded ones and a `(human)` amend must be one the
    driver recorded; without it, a pin commit may only extend FROZEN after
    amend commits and a restore must put back an already-pinned pack. Only
    a genuine restore excuses earlier tamper. Slice commits made before the
    freeze (a Lead take-over while the author was still working) are
    tolerated with a note: the author worked from the base export and never
    saw them (finding 5)."""
    loop_dir = Path(loop_dir).resolve()
    prefix = _mailbox_prefix(loop_dir)
    if prefix is None:
        return []
    acc_rel = f"{prefix}/{ACCEPTANCE_DIR}" if prefix else ACCEPTANCE_DIR
    top = _git(loop_dir, "rev-parse", "--show-toplevel").stdout.strip()
    repo_dir = Path(top) if top else loop_dir
    has_pack = (loop_dir / ACCEPTANCE_DIR).exists()
    driver = _driver_state(repo_dir, loop_dir)
    if base is None and driver is not None:
        base = str(driver.get("run_head") or driver.get("base") or "") or None
    if base is None:
        base = _git_freeze_base(repo_dir, acc_rel)
    if base is None:
        if has_pack:
            return [f"{acc_rel}/ exists but no commit added {acc_rel}/FROZEN "
                    "(not a driver freeze)"]
        return []
    driver_commits = set(driver.get("driver_commits") or []) if driver else set()
    human_amends = set(driver.get("human_amends") or []) if driver else set()
    rng = f"{base}..HEAD"
    proc = _git(repo_dir, "log", "--reverse", "--format=%x1e%H%x1f%s%x1f%(trailers:key=Acceptance-Pin,valueonly,separator=%x2c)",
                "--name-only", rng, "--", acc_rel)
    if proc.returncode != 0:
        return [f"cannot read the acceptance history ({proc.stderr.strip()[-200:]})"]
    offenders: list[str] = []
    # Tamper offenders a later genuine driver `acceptance: restore` commit
    # put right (the driver already counted that breach when it restored).
    pending_tamper: list[str] = []
    freeze_sha: str | None = None
    known_pins: set[str] = set()
    amends_since_pin = 0
    for block in proc.stdout.split("\x1e"):
        if not block.strip():
            continue
        head, _nl, rest = block.partition("\n")
        sha, subject, pin = (head.split("\x1f") + ["", ""])[:3]
        pin = pin.strip()
        files = [ln for ln in rest.splitlines() if ln.strip()]
        label = f"{sha[:12]} {subject!r}"
        outside = [f for f in _git(repo_dir, "show", "--format=", "--name-only", sha).stdout.splitlines()
                   if f.strip() and not f.startswith(acc_rel + "/")]
        if _ACC_FREEZE_RE.match(subject) or _ACC_DRIVER_RE.match(subject):
            kind = "freeze" if _ACC_FREEZE_RE.match(subject) else \
                "restore" if subject.startswith("acceptance: restore") else "pin"
            genuine = True
            if kind == "freeze":
                if freeze_sha is not None:
                    offenders.append(f"{label}: a second freeze commit (the pack is frozen once per loop)")
                    continue
                if driver is not None and sha != driver.get("freeze_commit"):
                    offenders.append(f"{label}: not the driver's recorded freeze commit "
                                     f"{str(driver.get('freeze_commit'))[:12]}")
                    continue
                freeze_sha = sha
            elif freeze_sha is None:
                offenders.append(f"{label}: driver acceptance commit before the freeze")
                genuine = False
            if kind != "freeze" and driver is not None and sha not in driver_commits:
                offenders.append(f"{label}: not an acceptance commit the driver recorded "
                                 "(a subject alone is not a driver commit)")
                genuine = False
            if outside:
                offenders.append(f"{label}: touches files outside acceptance/: {', '.join(outside[:5])}")
                genuine = False
            committed = pack_hash_at(repo_dir, sha, acc_rel)
            if not pin or pin != committed:
                offenders.append(f"{label}: Acceptance-Pin trailer does not match the committed pack")
                genuine = False
            elif kind == "pin":
                if [f for f in files if f != f"{acc_rel}/FROZEN"]:
                    offenders.append(f"{label}: a pin commit may only extend {acc_rel}/FROZEN "
                                     f"(also: {', '.join(f for f in files if f != f'{acc_rel}/FROZEN')})")
                    genuine = False
                elif not amends_since_pin:
                    offenders.append(f"{label}: a pin commit with no amend commit to pin")
                    genuine = False
            elif kind == "restore" and pin not in known_pins:
                offenders.append(f"{label}: restores a pack that was never pinned")
                genuine = False
            if genuine:
                known_pins.add(pin)
                amends_since_pin = 0
                if kind == "restore":
                    pending_tamper.clear()
            continue
        m = _ACC_AMEND_RE.match(subject)
        if m:
            if freeze_sha is None:
                offenders.append(f"{label}: amend before the freeze")
            if outside:
                offenders.append(f"{label}: an amend commit may touch only acceptance/ "
                                 f"(also: {', '.join(outside[:5])})")
            ids = [i.strip() for i in m.group(1).split(",")]
            diff = _git(repo_dir, "show", "--format=", "-U0", sha, "--",
                        f"{acc_rel}/AMENDMENTS.md").stdout
            added = set(re.findall(r"^\+##\s+(ACC-[0-9]{1,4})\b", diff, re.MULTILINE))
            missing = [i for i in ids if i not in added]
            if missing:
                offenders.append(f"{label}: no AMENDMENTS.md record for {', '.join(missing)}")
            removed = re.findall(r"^-(?!--)(.+)$", diff, re.MULTILINE)
            if removed:
                offenders.append(f"{label}: AMENDMENTS.md is append-only (lines removed)")
            if m.group(2) == "human" and driver is not None and sha not in human_amends:
                # A role's commit labelled `(human)`: tamper unless the
                # driver restored it (finding 1).
                pending_tamper.append(f"{label}: a `(human)` amendment the driver never "
                                      "authenticated (humans amend via `trioctl omnigent "
                                      "acceptance amend --human` while the loop is stopped)")
            amends_since_pin += 1
            continue
        pending_tamper.append(f"{label}: touches {acc_rel}/ ({', '.join(files[:3])}) -- only the "
                              "driver freeze/restore and amend commits may")
    offenders.extend(pending_tamper)
    if has_pack and freeze_sha is None and not offenders:
        offenders.append(f"{acc_rel}/ exists but {rng} has no driver freeze commit")
    if freeze_sha is not None:
        slices = _git(repo_dir, "log", "--format=%H %s", "--grep=^slice(", rng).stdout.splitlines()
        for line in slices:
            ssha, _sp, subject = line.partition(" ")
            if _git(repo_dir, "merge-base", "--is-ancestor", freeze_sha, ssha).returncode == 0:
                continue
            if _git(repo_dir, "merge-base", "--is-ancestor", ssha, freeze_sha).returncode == 0:
                if notes is not None:
                    notes.append(f"{ssha[:12]} {subject!r} was committed before the acceptance "
                                 f"freeze {freeze_sha[:12]} (tolerated: the author worked from the "
                                 "base export and never saw it; make no product commits before "
                                 "FROZEN)")
                continue
            offenders.append(f"{ssha[:12]} {subject!r}: slice commit is on a line that does not "
                             f"contain the acceptance freeze {freeze_sha[:12]} (acceptance/freeze "
                             "ordering)")
    return offenders


def covers(declared: str, actual: str) -> bool:
    """Whether a declared write covers an actual file: exact path match, or
    the declared path is a directory prefix of it."""
    d, a = _normalize(declared), _normalize(actual)
    if not d or not a:
        return False
    return a == d or a.startswith(d + "/")


def analyze_slice(sl: dict, base: Path, repos: dict | None = None) -> dict:
    """Declared-vs-actual writes of one slice, from its repo's git log.

    *repos* (r15) maps declared PLAN.md `repos:` names to their paths; a
    slice whose `repo:` names one is resolved there (its `slice(<id>):`
    commits live in that repo), `home`/`.` in the mailbox repo. Without
    *repos* a `repo:` value is a path relative to the mailbox dir (pre-r15).
    """
    repos = repos or {}
    name = _METRICS.slice_repo_name(sl, repos) if repos else None
    if name is not None and name != _METRICS.HOME_REPO:
        repo_path = Path(repos[name]).resolve()
    elif name == _METRICS.HOME_REPO:
        repo_path = base.resolve()
    else:
        repo_path = (base / sl["repo"]).resolve()
    entry: dict = {
        "id": sl["id"],
        "repo": sl["repo"],
        "repo_path": str(repo_path),
        "repo_status": "ok",
        "iteration": sl.get("iteration"),
        "commits": [],
        "declared_writes": sl["writes"],
        "actual_touched": [],
        "undeclared_touches": [],
        "declared_untouched": [],
    }
    if not repo_path.exists():
        entry["repo_status"] = "missing"
        return entry
    commits = slice_commits(sl["id"], repo_path)
    if commits is None:
        entry["repo_status"] = "not-a-git-repo"
        return entry

    entry["commits"] = commits
    touched: set[str] = set()
    for sha in commits:
        touched.update(commit_files(sha, repo_path))
    entry["actual_touched"] = sorted(touched)

    # api: entries are interface names, not paths — they never match git files.
    declared = list(
        dict.fromkeys(w for w in sl["writes"] if not w.startswith("api:"))
    )
    entry["undeclared_touches"] = sorted(
        f for f in touched if not any(covers(d, f) for d in declared)
    )
    entry["declared_untouched"] = sorted(
        d for d in declared if not any(covers(d, f) for f in touched)
    )
    return entry


def _attach_quality(entries: list[dict], loop_dir: Path) -> None:
    """r18a shadow telemetry the Omnigent driver recorded in `.driver.json`
    (`quality`: per `<slice>@<sha12>` the base-revert kill check, authorship,
    evidence kinds). Informational only; absent file or key adds nothing."""
    try:
        data = json.loads((loop_dir / ".driver.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    quality = data.get("quality") if isinstance(data, dict) else None
    if not isinstance(quality, dict):
        return
    for entry in entries:
        rows = [
            dict(v) for v in quality.values()
            if isinstance(v, dict) and v.get("slice") == entry["id"]
        ]
        if rows:
            entry["quality"] = rows


def analyze(mailbox: Path, slice_filter: str | None = None) -> dict:
    mailbox = mailbox.resolve()
    plan_path = mailbox / "loop" / "PLAN.md"
    if not plan_path.is_file():
        plan_path = mailbox / "PLAN.md"
    if not plan_path.is_file():
        raise SliceParseError(
            f"PLAN.md not found under {mailbox} "
            f"(looked for {mailbox / 'loop' / 'PLAN.md'} and {mailbox / 'PLAN.md'})"
        )
    try:
        text = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SliceParseError(f"PLAN.md unreadable: {exc}") from exc

    slices = parse_slices(find_slices_block(text))
    if slice_filter is not None:
        available = [sl["id"] for sl in slices]
        slices = [sl for sl in slices if sl["id"] == slice_filter]
        if not slices:
            raise SliceParseError(
                f"unknown --slice id {slice_filter!r}; available slice id(s): "
                f"{', '.join(available) if available else '(none)'}"
            )
    declared_for = getattr(_METRICS, "declared_repos_for", None)
    if declared_for is not None:
        repos, _errors = declared_for(plan_path.parent, text)
    else:
        repos, _errors = _METRICS.parse_repos_block(
            text, _METRICS.mailbox_repo_root(plan_path.parent)
        )
    named = {r["name"]: r["path"] for r in repos}
    entries = [analyze_slice(sl, mailbox, named) for sl in slices]
    _attach_quality(entries, plan_path.parent)
    summary = {
        "total_slices": len(entries),
        "slices_with_undeclared_touches": sum(
            1 for e in entries if e["undeclared_touches"]
        ),
        "undeclared_file_count": len(
            {f for e in entries for f in e["undeclared_touches"]}
        ),
        "repos_missing_or_not_git": sum(
            1 for e in entries if e["repo_status"] != "ok"
        ),
        "slices_without_commits": sum(1 for e in entries if not e["commits"]),
    }
    return {
        "mailbox": str(mailbox),
        "plan": str(plan_path),
        "slice_filter": slice_filter,
        "slices": entries,
        "summary": summary,
    }


def _live_mailbox_for(mailbox: Path) -> Path:
    """r16: a root mailbox whose root-free loop runs in a Lead worktree is
    read from that live copy (the root copy is stale until the loop lands);
    anything else is returned unchanged."""
    live_fn = getattr(_METRICS, "live_mailbox", None)
    if live_fn is None:
        return mailbox
    loop_dir = mailbox / "loop" if (mailbox / "loop" / "PLAN.md").is_file() else mailbox
    live = live_fn(loop_dir)
    if live is None:
        return mailbox
    print(
        f"trio-shadow.py: {loop_dir} runs root-free; reading its live mailbox {live}",
        file=sys.stderr,
    )
    return live


def discover_mailboxes(root: Path) -> list[Path]:
    """Every ``loop*/`` directory directly under root, sorted by name.

    Purely a name-based directory listing — whether each one has a
    parsable PLAN.md ``slices:`` block is decided by trying to analyze it
    (report_drift skips ``SliceParseError`` silently), not here.
    """
    root = root.resolve()
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("loop")),
        key=lambda p: p.name,
    )


def _declared_paths(writes: list[str]) -> list[str]:
    """Declared writes with ``api:`` pseudo-entries stripped — the same
    filter analyze_slice applies before matching against git-derived
    paths."""
    return [w for w in writes if not w.startswith("api:")]


def _declared_disjoint(a: list[str], b: list[str]) -> bool:
    """Whether two declared-write lists share no path, accounting for
    directory-prefix declarations in either direction (the same ``covers``
    relation used to match declared writes against actual files)."""
    return not any(covers(da, db) or covers(db, da) for da in a for db in b)


def mailbox_pairwise_hazards(entries: list[dict]) -> list[dict]:
    """Pairs of slices in the same mailbox and iteration whose *actual*
    touched files intersect even though their *declared* writes were
    disjoint — exactly the case where a Lead dispatching them in parallel
    by declaration alone would have raced them onto the same file.

    Slices are grouped by their ``iteration`` value (``None`` groups
    together, e.g. mailboxes that never set the optional field); pairs
    across different iterations are never compared, since they were never
    candidates for the same parallel dispatch.
    """
    hazards: list[dict] = []
    groups: dict[object, list[dict]] = {}
    for e in entries:
        groups.setdefault(e.get("iteration"), []).append(e)
    for iteration, group in groups.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.get("repo_path") != b.get("repo_path"):
                    # r15: disjointness is per repo; the same relative path
                    # in two repos is two different files.
                    continue
                overlap = sorted(set(a["actual_touched"]) & set(b["actual_touched"]))
                if not overlap:
                    continue
                da = _declared_paths(a["declared_writes"])
                db = _declared_paths(b["declared_writes"])
                if _declared_disjoint(da, db):
                    hazards.append(
                        {
                            "iteration": iteration,
                            "slice_a": a["id"],
                            "slice_b": b["id"],
                            "declared_a": da,
                            "declared_b": db,
                            "overlap": overlap,
                        }
                    )
    return hazards


def report_drift(root: Path) -> dict:
    """Aggregate declared-vs-actual write drift across every mailbox under
    root with a parsable PLAN.md slices block (see module docstring)."""
    root = root.resolve()
    mailboxes: list[dict] = []
    undeclared_counter: Counter[str] = Counter()
    total_slices = 0
    slices_with_commits = 0
    slices_with_undeclared = 0
    total_undeclared_touches = 0
    slices_with_declared_untouched = 0
    pairwise_hazards_total = 0

    for mb_dir in discover_mailboxes(root):
        try:
            mb_report = analyze(mb_dir)
        except SliceParseError:
            continue
        entries = mb_report["slices"]
        hazards = mailbox_pairwise_hazards(entries)

        mb_with_commits = sum(1 for e in entries if e["commits"])
        mb_with_undeclared = sum(1 for e in entries if e["undeclared_touches"])
        mb_undeclared_touches = sum(len(e["undeclared_touches"]) for e in entries)
        mb_declared_untouched = sum(1 for e in entries if e["declared_untouched"])
        for e in entries:
            undeclared_counter.update(e["undeclared_touches"])

        total_slices += len(entries)
        slices_with_commits += mb_with_commits
        slices_with_undeclared += mb_with_undeclared
        total_undeclared_touches += mb_undeclared_touches
        slices_with_declared_untouched += mb_declared_untouched
        pairwise_hazards_total += len(hazards)

        mailboxes.append(
            {
                "mailbox": mb_dir.name,
                "path": str(mb_dir),
                "plan": mb_report["plan"],
                "total_slices": len(entries),
                "slices_with_commits": mb_with_commits,
                "slices_with_undeclared_touches": mb_with_undeclared,
                "undeclared_touch_count": mb_undeclared_touches,
                "slices_with_declared_untouched": mb_declared_untouched,
                "pairwise_hazards": hazards,
            }
        )

    def pct(n: int) -> float:
        return round(100.0 * n / total_slices, 1) if total_slices else 0.0

    return {
        "root": str(root),
        "mailboxes_scanned": len(mailboxes),
        "total_slices": total_slices,
        "slices_with_commits": slices_with_commits,
        "slices_with_commits_pct": pct(slices_with_commits),
        "slices_with_undeclared_touches": slices_with_undeclared,
        "slices_with_undeclared_touches_pct": pct(slices_with_undeclared),
        "total_undeclared_touches": total_undeclared_touches,
        "top_undeclared_paths": [
            {"path": p, "count": c} for p, c in undeclared_counter.most_common(15)
        ],
        "slices_with_declared_untouched": slices_with_declared_untouched,
        "slices_with_declared_untouched_pct": pct(slices_with_declared_untouched),
        "pairwise_hazards_total": pairwise_hazards_total,
        "mailboxes": mailboxes,
    }


def render_drift(agg: dict) -> str:
    lines = [
        f"Declared-write drift across mailboxes under: {agg['root']}",
        f"Mailboxes scanned (parsable slices block): {agg['mailboxes_scanned']}",
        "",
    ]
    if agg["mailboxes"]:
        rows = [
            (
                m["mailbox"],
                str(m["total_slices"]),
                str(m["slices_with_commits"]),
                str(m["slices_with_undeclared_touches"]),
                str(m["undeclared_touch_count"]),
                str(m["slices_with_declared_untouched"]),
                str(len(m["pairwise_hazards"])),
            )
            for m in agg["mailboxes"]
        ]
        header = (
            "mailbox",
            "slices",
            "w/commit",
            "w/undeclared",
            "undeclared",
            "declared-untouched",
            "hazards",
        )
        widths = [
            max(len(header[i]), *(len(r[i]) for r in rows))
            for i in range(len(header))
        ]
        def fmt_row(r: tuple[str, ...]) -> str:
            return "  ".join(c.ljust(w) for c, w in zip(r, widths))
        lines.append(fmt_row(header))
        lines.append(fmt_row(tuple("-" * w for w in widths)))
        lines.extend(fmt_row(r) for r in rows)
        lines.append("")

    lines.append(
        f"Totals: {agg['total_slices']} slice(s), "
        f"{agg['slices_with_commits']} with >=1 commit "
        f"({agg['slices_with_commits_pct']}%), "
        f"{agg['slices_with_undeclared_touches']} with undeclared touches "
        f"({agg['slices_with_undeclared_touches_pct']}%), "
        f"{agg['total_undeclared_touches']} undeclared touch(es) total, "
        f"{agg['slices_with_declared_untouched']} with declared-but-untouched "
        f"paths ({agg['slices_with_declared_untouched_pct']}%), "
        f"{agg['pairwise_hazards_total']} pairwise hazard(s)"
    )

    lines.append("")
    lines.append("Top undeclared paths (what Leads forget to declare):")
    if agg["top_undeclared_paths"]:
        for i, entry in enumerate(agg["top_undeclared_paths"], 1):
            lines.append(f"  {i:2d}. {entry['path']}  ({entry['count']}x)")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(
        "Pairwise hazards (same-iteration slices whose declared writes "
        "looked disjoint but actually collided):"
    )
    any_hazard = False
    for m in agg["mailboxes"]:
        for h in m["pairwise_hazards"]:
            any_hazard = True
            lines.append(
                f"  {m['mailbox']} iteration={h['iteration']}: "
                f"{h['slice_a']} (writes: {_join(h['declared_a'])}) vs "
                f"{h['slice_b']} (writes: {_join(h['declared_b'])}) "
                f"both touched: {_join(h['overlap'])}"
            )
    if not any_hazard:
        lines.append("  (none)")

    lines.append("")
    lines.append("Result: shadow mode — informational only, never gates (exit 0)")
    return "\n".join(lines)


def _join(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


def render(report: dict, require_commits: bool = False) -> str:
    lines = [
        f"Checked: {report['mailbox']}",
        f"Slice contracts: {report['plan']}",
    ]
    for sl in report["slices"]:
        if sl["repo_status"] == "ok":
            head = (
                f"  {sl['id']}  (repo: {sl['repo']}, {len(sl['commits'])} commit(s), "
                f"{len(sl['actual_touched'])} file(s) touched)"
            )
        else:
            why = "repo missing" if sl["repo_status"] == "missing" else "not a git repo"
            head = f"  {sl['id']}  (repo: {sl['repo']}, {why}: {sl['repo_path']})"
        lines.append(head)
        lines.append(f"    declared writes: {_join(sl['declared_writes'])}")
        lines.append(f"    actual touched:  {_join(sl['actual_touched'])}")
        if sl["undeclared_touches"]:
            lines.append(f"    touched but undeclared: {_join(sl['undeclared_touches'])}")
        if sl["declared_untouched"]:
            lines.append(f"    declared but untouched: {_join(sl['declared_untouched'])}")
        if sl["repo_status"] == "ok" and not sl["commits"]:
            lines.append("    no slice-prefixed commits")
        for q in sl.get("quality") or []:
            kc = q.get("kill_check") if isinstance(q.get("kill_check"), dict) else {}
            parts = [f"@{str(q.get('sha') or '')[:12]}",
                     f"kill_check: {kc.get('outcome', 'n/a')}",
                     f"by {q.get('authored_by', '?')}"]
            ev = q.get("evidence")
            if isinstance(ev, dict) and ev:
                parts.append("evidence: " + " ".join(f"{k}={v}" for k, v in ev.items()))
            if q.get("verification_flags"):
                parts.append(f"flags: {len(q['verification_flags'])}")
            lines.append("    quality (r18a shadow): " + ", ".join(parts))
    s = report["summary"]
    lines.append(
        f"Summary: {s['total_slices']} slice(s), "
        f"{s['slices_with_undeclared_touches']} with undeclared touches, "
        f"{s['undeclared_file_count']} undeclared file(s)"
    )
    if s["repos_missing_or_not_git"]:
        lines.append(
            f"  ({s['repos_missing_or_not_git']} slice(s) with a missing or "
            "non-git repo)"
        )
    if require_commits:
        lines.append(
            "Result: commit gate active — slice commits enforced (exit 0 if the gate passes, 1 otherwise)"
        )
    else:
        lines.append("Result: shadow mode — informational only, never gates (exit 0)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure declared-vs-actual writes for PLAN.md slices "
        "(shadow mode: observability only; --require-commits turns on the "
        "active commit gate).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"trio-shadow {TRIO_SHADOW_VERSION}",
    )
    parser.add_argument(
        "--mailbox",
        default=".",
        help="loop dir or project dir containing loop/ (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable JSON report"
    )
    parser.add_argument(
        "--report-drift",
        action="store_true",
        help="Aggregate declared-vs-actual write drift across every loop*/ "
        "mailbox under --root with a parsable PLAN.md slices block "
        "(mailboxes without one are skipped silently). Reports totals, the "
        "top undeclared paths, and pairwise hazards: same-iteration slices "
        "whose declared writes looked disjoint but actually collided. "
        "Ignores --mailbox/--slice/--require-commits; always exits 0.",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repo root to scan for loop*/ mailboxes with --report-drift "
        "(default: current directory). Ignored otherwise.",
    )
    parser.add_argument(
        "--require-commits",
        action="store_true",
        help="ACTIVE interlock: exit 1 when any code-changing slice (a writes "
        "entry that is neither api: nor under loop/) has no slice(<id>): "
        "commit; exit 0 when every code-changing slice has commits. "
        "Missing/malformed slices block still exits 2. Combine with --slice "
        "for the per-slice open-loop gate.",
    )
    parser.add_argument(
        "--acceptance-base",
        metavar="SHA",
        default=None,
        help="r19: start of the range the frozen-acceptance guard of "
        "--require-commits checks (default: the driver state's run head, else "
        "the parent of the commit that added FROZEN).",
    )
    parser.add_argument(
        "--slice",
        metavar="ID",
        default=None,
        help="Restrict the shadow report and --require-commits gate to one "
        "slice id (v1 open-loop per-slice gate, MAILBOX-SCHEMA.md). Without "
        "it, behaviour is unchanged. An unknown id exits 2.",
    )
    args = parser.parse_args(argv)

    if args.report_drift:
        agg = report_drift(Path(args.root))
        if args.json:
            json.dump(agg, sys.stdout, indent=2)
            print()
        else:
            print(render_drift(agg))
        return 0

    mailbox = _live_mailbox_for(Path(args.mailbox))
    try:
        report = analyze(mailbox, slice_filter=args.slice)
    except SliceParseError as exc:
        print(f"trio-shadow.py: error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(render(report, require_commits=args.require_commits))

    if args.require_commits:
        # r19: the frozen-acceptance guard (no pack: nothing to check).
        loop_dir = Path(report["plan"]).parent
        acc_notes: list[str] = []
        acc_offenders = acceptance_offenders(loop_dir, args.acceptance_base, acc_notes)
        for msg in acc_notes:
            print(f"acceptance note: {msg}")
        for msg in acc_offenders:
            print(f"acceptance gate: {msg}")
        offenders = commit_gate_offenders(report)
        if acc_offenders and not offenders:
            print(f"acceptance gate: FAIL — {len(acc_offenders)} offender(s); only the "
                  "driver and amend commits may touch acceptance/")
            return 1
        if offenders:
            for e in offenders:
                why = ""
                if e["repo_status"] != "ok":
                    why = f" (repo: {e['repo_status']})"
                cc = ", ".join(code_changing_writes(e["declared_writes"]))
                print(
                    f"commit gate: slice {e['id']!r} is code-changing "
                    f"(writes: {cc}) but has no slice({e['id']}): commit{why}"
                )
            print(
                f"commit gate: FAIL — {len(offenders)} code-changing slice(s) "
                "missing slice-prefixed commits; fix before Evaluator dispatch"
            )
            if acc_offenders:
                print(f"acceptance gate: FAIL — {len(acc_offenders)} offender(s)")
            return 1
        print("commit gate: PASS — every code-changing slice has a slice(<id>): commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Driver-owned builder wave planning — ported 1:1 from ``native/trio-native.js``
(same semantics, same error texts). Pure functions, no I/O.

Kept in one module so ``driver.py`` can plan waves, detect conflicts and
build re-dispatch slices exactly as the native Workflow driver does, without
depending on that JS file at runtime.
"""
from __future__ import annotations

import re
from typing import Any

Slice = dict[str, Any]

#: Any of these characters makes a write pattern a glob (``trio-native.js``
#: ``GLOB_META``).
_GLOB_META = re.compile(r"[*?\[\]{}\\]")


def norm_path(p: Any) -> str:
    """``normPath``: trim, strip a leading ``./`` and any trailing slashes."""
    text = str(p).strip()
    text = re.sub(r"^\./", "", text)
    text = re.sub(r"/+$", "", text)
    return text


def product_writes(s: Slice) -> list[str]:
    """``productWrites``: a slice's ``writes`` normalised, minus non-product
    markers (``api:`` pseudo-writes, the mailbox itself and anything under
    it)."""
    out = []
    for p in s.get("writes") or []:
        np = norm_path(p)
        if not np or np.startswith("api:") or np == "loop" or np.startswith("loop/"):
            continue
        out.append(np)
    return out


def literal_prefix(p: str) -> str | None:
    """``literalPrefix``: the characters before the first glob metacharacter,
    or ``None`` for a plain (non-glob) path."""
    m = _GLOB_META.search(p)
    return None if m is None else p[: m.start()]


def paths_overlap(p: str, q: str) -> bool:
    """``pathsOverlap``: two writes may overlap iff one's literal prefix is a
    string prefix of the other's; a plain path is its own literal prefix."""
    lp = literal_prefix(p)
    lq = literal_prefix(q)
    if lp is None and lq is None:
        return p == q or p.startswith(q + "/") or q.startswith(p + "/")
    x = p if lp is None else lp
    y = q if lq is None else lq
    return x.startswith(y) or y.startswith(x)


def overlaps(a: Slice, b: Slice) -> bool:
    """``overlaps``: two slices overlap when any of their product writes
    overlap; a slice with no declared product writes overlaps everything
    (unknown writes are never treated as concurrent)."""
    wa = product_writes(a)
    wb = product_writes(b)
    if not wa or not wb:
        return True
    return any(paths_overlap(p, q) for p in wa for q in wb)


def plan_waves(slices: list[Slice]) -> list[list[Slice]]:
    """``planWaves``: deterministic waves — a slice joins the earliest wave
    after all its in-plan dependencies, and only when its writes are
    disjoint from every slice already in that wave."""
    ids = {s["id"] for s in slices}
    done: set[str] = set()
    rest = list(slices)
    waves: list[list[Slice]] = []
    while rest:
        wave: list[Slice] = []
        for s in rest:
            depends = s.get("depends") or []
            if not all((d not in ids) or (d in done) for d in depends):
                continue
            if any(overlaps(w, s) for w in wave):
                continue
            wave.append(s)
        if not wave:
            raise ValueError(
                "slice depends form a cycle: " + ", ".join(s["id"] for s in rest)
            )
        for s in wave:
            done.add(s["id"])
        rest = [s for s in rest if s not in wave]
        waves.append(wave)
    return waves


_SLICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def check_slices(plan: Any) -> str | None:
    """``checkSlices``: a human-readable problem with ``plan.slices``, or
    ``None`` when it is well formed."""
    slices = plan.get("slices") if isinstance(plan, dict) else None
    if not isinstance(slices, list):
        return "the Lead plan has no slices list"
    seen: set[str] = set()
    for s in slices:
        sid = s.get("id") if isinstance(s, dict) else None
        if not isinstance(sid, str) or not _SLICE_ID_RE.match(sid):
            return f"bad slice id {sid!r}"
        if sid in seen:
            return f"duplicate slice id {sid}"
        brief = s.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            return f"slice {sid} has no brief"
        seen.add(sid)
    return None


def wave_conflicts(bl: dict, integ: dict | None, cl: dict) -> list[dict]:
    """``waveConflicts``: git is the authority — a builder branch the
    integrate call was asked to merge that ``cleanup`` found "not merged
    into HEAD" conflicted; the Lead's ``conflicts`` only adds the files."""
    unmerged = {
        x["branch"] for x in (cl.get("kept") or [])
        if x.get("reason") == "not merged into HEAD"
    }
    reported = integ.get("conflicts") if isinstance(integ, dict) else None
    reported = reported if isinstance(reported, list) else []
    out = []
    for m in bl.get("merge") or []:
        if m["branch"] not in unmerged:
            continue
        r = next(
            (c for c in reported
             if isinstance(c, dict) and (c.get("branch") == m["branch"] or c.get("id") == m.get("id"))),
            None,
        )
        files = [str(f) for f in r["files"]] if r and isinstance(r.get("files"), list) else []
        out.append({"id": m["id"], "branch": m["branch"], "files": files})
    return out


def redispatch_slice(s: Slice, c: dict) -> Slice:
    """``redispatchSlice``: a conflicting slice gets one new single-builder
    wave, forked from the Lead's HEAD after this wave's merges, with the
    conflict files added to ``writes``."""
    files = ", ".join(c["files"]) if c.get("files") else "(files not reported)"
    out = dict(s)
    out["depends"] = []
    out["writes"] = list(dict.fromkeys((s.get("writes") or []) + list(c.get("files") or [])))
    out["supersedes"] = c["branch"]
    out["brief"] = (
        s["brief"] + "\n\nRE-DISPATCH: an earlier builder for this slice delivered branch `"
        + c["branch"] + "`, but merging it conflicted with work merged since, on: " + files
        + ". Your worktree forks from the Lead's new HEAD, which contains that merged work. "
        "Build the slice on top of it, keeping the merged work intact in the shared files; "
        "you may read the earlier attempt with `git diff HEAD..." + c["branch"] + "`."
    )
    return out


def redispatch_refused(s: Slice, x: dict) -> Slice:
    """``redispatchRefused``: a slice whose builder stayed refused gets one
    new builder, forked from the Lead's current HEAD; ``supersedes`` only
    when the helper's ``own_branch`` names it."""
    out = dict(s)
    out["depends"] = []
    own_branch = x.get("own_branch")
    if own_branch:
        out["supersedes"] = own_branch
    else:
        out.pop("supersedes", None)
    out["brief"] = (
        s["brief"] + "\n\nRE-DISPATCH: an earlier builder for this slice was refused by "
        "the driver: " + str(x.get("reason")) + ". Your worktree forks from the Lead's "
        "current HEAD. Build the slice from scratch there"
        + (f"; you may read the earlier attempt with `git diff HEAD...{own_branch}`" if own_branch else "")
        + ". Report `head` and `commits` by copying the full shas from `git rev-parse HEAD` "
        "/ `git log --format=%H` output, never from memory."
    )
    return out

"""Canonical loop identities, identical from any checkout of one repository.

The dashboard loads this module by file path, so it intentionally has no
package-relative imports and uses only the standard library.  Everything is
plain file reads (no subprocess): a ``.git`` directory is a main checkout, a
``.git`` file ``gitdir: X`` is a linked worktree whose ``X/commondir`` names
the shared common directory.

A loop's identity is the pair (git common directory, mailbox path relative to
its checkout), so the same mailbox seen from the main checkout and from any
linked worktree has one ``loop_id``.  Outside git the identity falls back to
the real path of the mailbox.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import PurePosixPath
from threading import Lock

_CACHE_SECONDS = 60.0
_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_LOCK = Lock()


def clear_cache() -> None:
    """Forget memoized repository lookups (tests, long-lived servers)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip()
    except OSError:
        return None


def _common_dir_of_gitdir(gitdir: str) -> str:
    """The shared common directory of a linked-worktree gitdir."""
    value = _read_first_line(os.path.join(gitdir, "commondir"))
    if not value:
        return os.path.realpath(gitdir)
    return os.path.realpath(
        value if os.path.isabs(value) else os.path.join(gitdir, value))


def _checkout_at(directory: str) -> dict | None:
    """Identity when ``directory`` holds a ``.git`` entry, else None."""
    dot_git = os.path.join(directory, ".git")
    if os.path.isdir(dot_git):
        common = os.path.realpath(dot_git)
    elif os.path.isfile(dot_git):
        text = _read_first_line(dot_git) or ""
        if not text.startswith("gitdir:"):
            return None
        value = text[len("gitdir:"):].strip()
        gitdir = value if os.path.isabs(value) else os.path.join(directory, value)
        common = _common_dir_of_gitdir(os.path.realpath(gitdir))
    else:
        return None
    main = (os.path.dirname(common)
            if os.path.basename(common) == ".git" else None)
    return {"common_dir": common, "toplevel": os.path.realpath(directory),
            "main_worktree": main}


def repo_identity(path) -> dict | None:
    """``{"common_dir", "toplevel", "main_worktree"}`` for the checkout that
    contains ``path``, or None outside git.  ``main_worktree`` is None for a
    bare repository or a submodule gitdir."""
    try:
        real = os.path.realpath(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return None
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(real)
        if hit and now - hit[0] <= _CACHE_SECONDS:
            return dict(hit[1]) if hit[1] else None
    directory = real if os.path.isdir(real) else os.path.dirname(real)
    found = None
    while True:
        found = _checkout_at(directory)
        if found is not None:
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    with _CACHE_LOCK:
        _CACHE[real] = (now, found)
    return dict(found) if found else None


def canonical_loop(mailbox) -> dict:
    """``{"loop_id", "loop_key", "common_dir", "rel", "toplevel"}``."""
    real = os.path.realpath(os.fspath(mailbox))
    identity = repo_identity(real)
    if identity is None:
        key = "path::" + real
        rel = None
        common = top = None
    else:
        common = identity["common_dir"]
        top = identity["toplevel"]
        relative = os.path.relpath(real, top)
        rel = "." if relative == "." else PurePosixPath(
            *relative.split(os.sep)).as_posix()
        key = f"{common}::{rel}"
    return {
        "loop_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        "loop_key": key,
        "common_dir": common,
        "rel": rel,
        "toplevel": top,
    }

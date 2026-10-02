"""Driver-side QUEUE.md append/reconcile guard for trio-opencode open-loop
mode (MAILBOX-SCHEMA.md "v1 open-loop extension").

In Omnigent's native open-loop driver, ``QUEUE.md`` is mutated only by the
driver itself (``retired:`` entries) and by single-threaded Lead/Evaluator
passes. In trio-opencode, up to N concurrent slice-eval turns (LLM agents)
and the Lead edit ``QUEUE.md`` by hand *while the driver also appends*
``retired:`` entries for driver-owned builders. An LLM turn that rewrites
the whole file to add its own entry can silently drop a sibling's
just-appended entry, and two concurrent evaluators can independently pick
the same "next free" fault id. This module is the driver-side guard against
both failure modes:

* :func:`append_retired` is the driver's own atomic, idempotent,
  self-checked way to add a ``retired:`` entry.
* :class:`QueueGuard` remembers every ``retired:``/``faults:`` entry this
  process has ever observed and, after each role turn, repairs anything a
  concurrent full-file rewrite dropped -- without ever touching a field a
  turn legitimately changed (a fault's ``status:``) or a block whose fence
  failed to parse (left to the Lead's ``queue_errors`` repair instead).

Parsing is delegated entirely to ``steplib.TL._read_queue`` (the shared
``metrics/trio_loop.py`` reader, wrapping trio-metrics' ``read_queue``);
this module only ever *writes* QUEUE.md text, and only ever by inserting
whole entries into an existing fence (or creating one), never by
reformatting or reordering anything already there.

Concurrency model (ol-harden2)
------------------------------
Every *driver-side* writer (``append_retired``, the guard's fault
re-append, the duplicate-id renumber, the seed of a missing QUEUE.md) takes
:func:`queue_lock` and publishes with tmp + fsync + ``os.replace``, so a
reader never sees a half-written driver write. Role agents (the Lead and the
slice-evals are ``opencode`` processes using their own edit tools) write
QUEUE.md directly and **cannot** be locked; their writes may be a non-atomic
truncate-then-write. The driver's read-modify-write is therefore built to
tolerate them rather than to exclude them:

* the read is *stable* (:func:`_read_stable`): a zero-byte or still-changing
  file (an agent mid-truncate) is re-read until it settles;
* the post-write self-check never restores a pre-write snapshot over the
  file (a restore can only ever drop a newer concurrent entry). The pure
  insertion is checked on the text *before* anything is written, and after
  the rename the entry's presence is verified in a fresh stable read; when
  an agent's write replaced ours, the whole read-modify-write is retried
  against the newer content (bounded), and :class:`QueueError` is raised
  only if the entry still is not there;
* an agent write that lands on the old inode just before our rename is
  invisible to any lock-free scheme: :class:`QueueGuard` remains the net for
  that (it re-appends a ``retired:``/``faults:`` entry it has observed that a
  later rewrite dropped).

Retired entry key order
------------------------
Three keys for a home-repo entry: ``slice:``, ``sha:``, ``at:``. A
declared-repo entry (r15, MAILBOX-SCHEMA.md) carries a fourth key,
``repo:``, written **between** ``slice:`` and ``sha:`` -- the order
``prompts/canonical/lead.md`` "Multi-repo" spells out explicitly
("four keys in this order: `slice:`, `repo: <name>`, `sha:`, `at:`"),
which also matches MAILBOX-SCHEMA.md's "written between `slice:` and
`sha:`" (it also notes the reader accepts `repo:` after `sha:`, but the
*written* order here follows the canonical Lead prompt).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from trio_opencode import steplib

TL = steplib.TL

__all__ = [
    "QueueError",
    "queue_lock",
    "append_retired",
    "QueueGuard",
    "ensure_queue_file",
    "latest_retired",
    "faults",
]


class QueueError(RuntimeError):
    """A QUEUE.md mutation failed its self-check (the insertion was wrong, or
    the entry never became visible). QUEUE.md is never rolled back to a
    pre-write snapshot -- see the module docstring."""


# --------------------------------------------------------------------- lock

_REGISTRY_LOCK = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(mailbox: Path) -> threading.RLock:
    key = str(Path(mailbox).resolve())
    with _REGISTRY_LOCK:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def queue_lock(mailbox: Path):
    """Serialize every QUEUE.md mutation on ``mailbox``: a per-mailbox,
    reentrant, process-wide ``threading.RLock`` (so nested calls from the
    same thread -- e.g. :meth:`QueueGuard.reconcile` calling the internal
    append helpers -- never deadlock) plus an ``fcntl.flock`` on
    ``<mailbox>/.queue.lock`` (a runtime file, harmless to the mailbox
    contract: it is never read as part of QUEUE.md/STATE.md/etc.) so two
    *processes* sharing the same mailbox are serialized too."""
    mailbox = Path(mailbox)
    mailbox.mkdir(parents=True, exist_ok=True)
    rlock = _process_lock(mailbox)
    with rlock:
        lock_path = mailbox / ".queue.lock"
        fh = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# -------------------------------------------------------------- atomic I/O

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


#: Settle-read tuning (tests shrink it): how many times a zero-byte or
#: still-changing QUEUE.md is re-read, and the pause between reads.
_STABLE_READS = 40
_STABLE_PAUSE = 0.025
_EMPTY_READS = 6
#: Bound on read-modify-write retries when a concurrent agent write replaced
#: ours between the rename and the verification.
_RMW_ATTEMPTS = 8


def _stat_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _read_stable(path: Path) -> str:
    """Read ``path`` so that an agent's non-atomic truncate-then-write in
    flight is not mistaken for the file's content: a file whose (inode, size,
    mtime) changed across the read is re-read until it settles, and an
    existing zero-byte file is given ``_EMPTY_READS`` pauses to fill (an
    ``open(..., "w")`` truncates first and writes microseconds later) before
    it is believed empty. A missing file is ``""``."""
    text = ""
    empties = 0
    for _ in range(_STABLE_READS):
        before = _stat_sig(path)
        if before is None:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if before == _stat_sig(path):
            if text:
                return text
            empties += 1
            if empties >= _EMPTY_READS:
                return ""
        time.sleep(_STABLE_PAUSE)
    return text


def ensure_queue_file(mailbox: Path, seed: str = "# Queue\n") -> bool:
    """Create QUEUE.md with ``seed`` if it does not exist -- under
    :func:`queue_lock`, atomically, and never over an existing file. Returns
    whether it created one."""
    mailbox = Path(mailbox)
    with queue_lock(mailbox):
        path = mailbox / "QUEUE.md"
        if path.exists():
            return False
        _atomic_write(path, seed)
        return True


# ------------------------------------------------------------ fence finder

#: An opening fence line: <=3-space indent folded away by our canonical
#: writer (we only ever write/insert at column 0), a run of 3+ backticks,
#: and an info string that is exactly "yaml"/"yml" (any case) -- the only
#: fence shape MAILBOX-SCHEMA.md has the reader recognize for a queue block.
_FENCE_OPEN_RE = re.compile(r"^(`{3,})\s*([A-Za-z]*)\s*$")


def _locate_fence(lines: list[str], top_key: str) -> tuple[int, int] | None:
    """``(body_start, close_idx)`` for the first ```yaml/```yml fence whose
    body has a ``<top_key>:`` line at column 0, or ``None``. The body is
    ``lines[body_start:close_idx]``; ``close_idx`` is the closing fence
    line's index. Only a *well-formed* fence (opener matched by a closer of
    at least the same backtick run, found before EOF) is returned -- an
    unterminated or keyless fence is skipped, same as not being found."""
    i = 0
    n = len(lines)
    key_line_re = re.compile(r"^" + re.escape(top_key) + r":\s*$")
    while i < n:
        raw = lines[i].rstrip("\r\n")
        om = _FENCE_OPEN_RE.match(raw)
        if om and om.group(2).lower() in ("yaml", "yml"):
            fence_char = om.group(1)[0]
            run = len(om.group(1))
            close_re = re.compile(rf"^{re.escape(fence_char)}{{{run},}}\s*$")
            body_start = i + 1
            j = body_start
            has_key = False
            close_idx = None
            while j < n:
                body = lines[j].rstrip("\r\n")
                if close_re.match(body):
                    close_idx = j
                    break
                if key_line_re.match(body):
                    has_key = True
                j += 1
            if has_key and close_idx is not None:
                return body_start, close_idx
            i = (close_idx + 1) if close_idx is not None else n
            continue
        i += 1
    return None


def _insert_or_create_fence(text: str, top_key: str, entry_lines: list[str]) -> str:
    """Insert ``entry_lines`` as the last item inside the ``top_key:``
    fence, right before its closing ``` ``` ```; create the fence (appended
    at EOF) when none is found. Never touches anything else in ``text``."""
    lines = text.splitlines(keepends=True) if text else []
    located = _locate_fence(lines, top_key)
    if located is not None:
        _body_start, close_idx = located
        lines[close_idx:close_idx] = entry_lines
        return "".join(lines)
    block = ["```yaml\n", f"{top_key}:\n", *entry_lines, "```\n"]
    if not text:
        return "".join(block)
    out = text
    if not out.endswith("\n"):
        out += "\n"
    if not out.endswith("\n\n"):
        out += "\n"
    return out + "".join(block)


def _count_fence_entries(text: str, top_key: str, header_prefix: str) -> int:
    """Port of the canonical awk rule (MAILBOX-SCHEMA.md / prompts/canonical
    /lead.md), parameterized for ``retired:``/``  - slice:`` or
    ``faults:``/``  - id:``::

        awk '/^```/{f=0} f&&/^<header_prefix>/{n++} /^<top_key>:/{f=1} END{print n+0}'
    """
    fence_re = re.compile(r"^```")
    header_re = re.compile(r"^" + re.escape(header_prefix))
    top_re = re.compile(r"^" + re.escape(top_key) + r":")
    f = False
    n = 0
    for line in text.splitlines():
        if fence_re.match(line):
            f = False
        if f and header_re.match(line):
            n += 1
        if top_re.match(line):
            f = True
    return n


def _block_has_errors(queue: dict, key: str) -> bool:
    """Whether ``queue["errors"]`` names a problem in the ``key:`` block
    (fence-level or entry-level -- either way the block did not fully
    parse). Mirrors the ``` `faults:` block: ``` / ``` `retired:` block: ```
    prefix ``metrics/trio-metrics.py``'s ``parse_queue_block`` always uses."""
    prefix = f"`{key}:` block:"
    return any(str(e).startswith(prefix) for e in (queue.get("errors") or []))


# ------------------------------------------------------------- retired API

def _format_retired_lines(slice_id: str, sha: str, at: str, repo: str | None) -> list[str]:
    lines = [f"  - slice: {slice_id}\n"]
    if repo:
        lines.append(f"    repo: {repo}\n")
    lines.append(f"    sha: {sha}\n")
    lines.append(f"    at: {at}\n")
    return lines


def _rmw_insert(
    mailbox: Path, *, top_key: str, header_prefix: str, entry_lines: list[str],
    present, what: str,
) -> bool:
    """Insert ``entry_lines`` into the ``top_key:`` fence, tolerant of a
    concurrent unlocked agent write (see the module docstring). Caller holds
    :func:`queue_lock`. ``present(queue)`` says whether the entry is already
    in a parsed queue. Returns ``True`` when this call wrote the entry,
    ``False`` when it was already there and nothing was written.

    Never restores a pre-write snapshot: the only file this ever replaces is
    one it just re-read, and a failed verification re-reads and retries
    instead of rolling back over whatever newer content is on disk."""
    queue_path = mailbox / "QUEUE.md"
    mailbox.mkdir(parents=True, exist_ok=True)
    wrote = False
    for _attempt in range(_RMW_ATTEMPTS):
        before_text = _read_stable(queue_path)
        if present(TL._read_queue(mailbox)):
            return wrote
        before_count = _count_fence_entries(before_text, top_key, header_prefix)
        new_text = _insert_or_create_fence(before_text, top_key, entry_lines)
        # Pure self-check of the insertion itself, before anything is
        # written: nothing to restore if it fails.
        if _count_fence_entries(new_text, top_key, header_prefix) != before_count + 1:
            raise QueueError(
                f"{what} self-check failed: {top_key} entry count {before_count} -> "
                f"{_count_fence_entries(new_text, top_key, header_prefix)} (expected "
                f"{before_count + 1}); QUEUE.md left untouched"
            )
        _atomic_write(queue_path, new_text)
        wrote = True
        # Verify against a fresh stable read: an agent write that raced our
        # rename may have replaced the file with its own (older + its own
        # entry) -- then go round again on the newer content.
        _read_stable(queue_path)
        if present(TL._read_queue(mailbox)):
            return True
    raise QueueError(
        f"{what} not present in QUEUE.md after {_RMW_ATTEMPTS} attempts "
        "(a concurrent writer kept replacing the file); QUEUE.md left as found"
    )


def _append_retired_locked(
    mailbox: Path, *, slice_id: str, sha: str, at: str, repo: str | None = None,
) -> bool:
    mailbox = Path(mailbox)
    norm_repo = repo or None

    def present(queue: dict) -> bool:
        return any(
            e.get("slice") == slice_id
            and e.get("sha") == sha
            and (e.get("repo") or None) == norm_repo
            for e in queue.get("retired", [])
        )

    return _rmw_insert(
        mailbox, top_key="retired", header_prefix="  - slice:",
        entry_lines=_format_retired_lines(slice_id, sha, at, repo), present=present,
        what=f"append_retired for slice {slice_id}@{sha[:12]}",
    )


def append_retired(
    mailbox: Path, *, slice_id: str, sha: str, at: str, repo: str | None = None,
) -> bool:
    """Insert one ``retired:`` entry as the last item of the ``retired:``
    fence (creating QUEUE.md/the fence if missing). Idempotent: a
    (slice, sha[, repo]) pair already present is a no-op returning
    ``False``; a genuine insert returns ``True``. Atomic (tmp + fsync +
    replace) under :func:`queue_lock`. Self-checked without ever restoring
    a stale snapshot: the insertion is checked before the write, and the
    entry's presence after it (retrying against newer content when a
    concurrent agent write replaced ours) -- :class:`QueueError` only when
    the entry still is not there; QUEUE.md is never rolled back."""
    mailbox = Path(mailbox)
    with queue_lock(mailbox):
        return _append_retired_locked(mailbox, slice_id=slice_id, sha=sha, at=at, repo=repo)


# -------------------------------------------------------------- fault write

def _format_scope(items: list[str]) -> str:
    items = list(items or [])
    if items == ["design"]:
        return "design"

    def _q(item: str) -> str:
        if item != item.strip() or any(c in item for c in ",[]\"'"):
            return '"' + item.replace('"', '\\"') + '"'
        return item

    return "[" + ", ".join(_q(i) for i in items) + "]"


def _format_fault_lines(fault: dict) -> list[str]:
    reason = str(fault.get("reason", "")).replace("\n", " ").strip()
    return [
        f"  - id: {fault.get('id')}\n",
        f"    slice: {fault.get('slice')}\n",
        f"    observed_at: {fault.get('observed_at')}\n",
        f"    scope: {_format_scope(fault.get('scope') or [])}\n",
        f"    reason: {reason}\n",
        f"    status: {fault.get('status', 'open')}\n",
    ]


def _fault_identity(f: dict) -> tuple:
    """A fault's content identity: (slice, observed_at, reason) -- never its
    ``id``. The id is excluded on purpose: the guard itself renumbers a
    colliding id, and the Lead may repair a malformed id in place; keying
    on the id would make the guard re-append the pre-rename copy forever."""
    return (f.get("slice"), f.get("observed_at"), f.get("reason"))


def _append_fault_locked(mailbox: Path, fault: dict) -> bool:
    mailbox = Path(mailbox)
    wanted = _fault_identity(fault)

    def present(queue: dict) -> bool:
        return any(_fault_identity(f) == wanted for f in queue.get("faults", []))

    return _rmw_insert(
        mailbox, top_key="faults", header_prefix="  - id:",
        entry_lines=_format_fault_lines(fault), present=present,
        what=f"fault re-append for {fault.get('id')}",
    )


# -------------------------------------------------------- duplicate fault id

_FAULT_HEADER_RE = re.compile(r"^(\s*)- id:\s*(\S+)\s*$")


def _renumber_duplicate_fault_ids_locked(mailbox: Path) -> list[str]:
    """Scan the (clean) ``faults:`` fence for ids shared by two entries with
    a different (slice, observed_at, reason); rename the one that appears
    LATER in the file to the next free ``f<N>``. Never touches any other
    field, never deletes, never reorders. Returns one note per rename."""
    mailbox = Path(mailbox)
    queue_path = mailbox / "QUEUE.md"
    text = _read_stable(queue_path)
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    located = _locate_fence(lines, "faults")
    if located is None:
        return []
    body_start, close_idx = located

    header_positions: list[tuple[int, str]] = []
    for idx in range(body_start, close_idx):
        m = _FAULT_HEADER_RE.match(lines[idx].rstrip("\r\n"))
        if m:
            header_positions.append((idx, m.group(2)))

    queue = TL._read_queue(mailbox)
    fault_entries = queue.get("faults", [])
    # A clean block's parsed entry count always matches its header count;
    # a mismatch means something this simple scan cannot safely reason
    # about (should not happen once the caller has checked the block has
    # no parse errors) -- be conservative and do nothing.
    if len(header_positions) != len(fault_entries):
        return []

    identity_by_id: dict[str, tuple] = {}
    existing_ids: set[str] = {fid for _idx, fid in header_positions}

    def _next_free_id() -> str:
        nums = [int(m.group(1)) for fid in existing_ids
                for m in [re.match(r"^f(\d+)$", fid)] if m]
        n = (max(nums) + 1) if nums else 1
        candidate = f"f{n}"
        while candidate in existing_ids:
            n += 1
            candidate = f"f{n}"
        return candidate

    notes: list[str] = []
    rewrites: dict[int, str] = {}
    for (idx, fid), entry in zip(header_positions, fault_entries):
        identity = (entry.get("slice"), entry.get("observed_at"), entry.get("reason"))
        if fid not in identity_by_id:
            identity_by_id[fid] = identity
            continue
        if identity_by_id[fid] == identity:
            continue  # a true repeat, not an id collision; leave it alone
        new_id = _next_free_id()
        existing_ids.add(new_id)
        identity_by_id[new_id] = identity
        raw = lines[idx]
        eol = raw[len(raw.rstrip("\r\n")):]
        indent = _FAULT_HEADER_RE.match(raw.rstrip("\r\n")).group(1)
        rewrites[idx] = f"{indent}- id: {new_id}{eol}"
        notes.append(
            f"renumbered duplicate fault id {fid} -> {new_id} "
            f"(slice {entry.get('slice')})"
        )

    if not rewrites:
        return []
    for idx, new_line in rewrites.items():
        lines[idx] = new_line
    # Never clobber newer content: if an agent rewrote QUEUE.md since the
    # read above, skip -- the next reconcile re-derives the renames.
    if _read_text(queue_path) != text:
        return []
    _atomic_write(queue_path, "".join(lines))
    return notes


# ------------------------------------------------------------- QueueGuard

class QueueGuard:
    """Remembers every ``retired:``/``faults:`` entry ever observed in
    ``mailbox``'s QUEUE.md and repairs a concurrent full-file rewrite that
    dropped one, without ever reverting a legitimate change (a fault's
    ``status:`` transition) or touching a block whose fence failed to
    parse. Safe to share across threads: every method serializes on
    :func:`queue_lock`."""

    def __init__(self, mailbox: Path) -> None:
        self.mailbox = Path(mailbox)
        # retired: keyed (slice, sha, repo-or-None) -> last-seen full entry
        self._retired: dict[tuple, dict] = {}
        self._retired_order: list[tuple] = []
        # faults: keyed (slice, observed_at, reason) -> latest entry
        # (status included, refreshed on every observe()).
        self._faults: dict[tuple, dict] = {}
        self._faults_order: list[tuple] = []

    # -- observation ----------------------------------------------------

    def observe(self) -> None:
        with queue_lock(self.mailbox):
            self._observe_locked()

    def _observe_locked(self) -> None:
        queue = TL._read_queue(self.mailbox)
        for e in queue.get("retired", []):
            key = (e.get("slice"), e.get("sha"), e.get("repo") or None)
            if key not in self._retired:
                self._retired_order.append(key)
            self._retired[key] = dict(e)
        for f in queue.get("faults", []):
            key = _fault_identity(f)
            if key not in self._faults:
                self._faults_order.append(key)
            self._faults[key] = dict(f)

    # -- reconciliation ---------------------------------------------------

    def reconcile(self, label: str) -> list[str]:
        with queue_lock(self.mailbox):
            notes: list[str] = []
            queue = TL._read_queue(self.mailbox)

            if not _block_has_errors(queue, "retired"):
                current = {
                    (e.get("slice"), e.get("sha"), e.get("repo") or None)
                    for e in queue.get("retired", [])
                }
                for key in self._retired_order:
                    if key in current:
                        continue
                    entry = self._retired[key]
                    slice_id, sha, repo = key
                    if _append_retired_locked(
                        self.mailbox, slice_id=slice_id, sha=sha,
                        at=entry.get("at", ""), repo=repo,
                    ):
                        notes.append(
                            f"queue guard ({label}): re-appended retired "
                            f"{slice_id}@{sha[:12]}"
                        )
                queue = TL._read_queue(self.mailbox)

            if not _block_has_errors(queue, "faults"):
                current_ids = {_fault_identity(f) for f in queue.get("faults", [])}
                for key in self._faults_order:
                    if key in current_ids:
                        continue
                    fault = self._faults[key]
                    if _append_fault_locked(self.mailbox, fault):
                        notes.append(
                            f"queue guard ({label}): re-appended fault "
                            f"{fault.get('id')} ({fault.get('slice')})"
                        )
                queue = TL._read_queue(self.mailbox)
                if not _block_has_errors(queue, "faults"):
                    rename_notes = _renumber_duplicate_fault_ids_locked(self.mailbox)
                    notes.extend(f"queue guard ({label}): {n}" for n in rename_notes)

            self._observe_locked()
            return notes


# --------------------------------------------------------- runner helpers

def latest_retired(mailbox: Path) -> dict[str, dict]:
    """Latest ``retired:`` entry per slice id (last in file order), as
    MAILBOX-SCHEMA.md defines "latest" for Evaluator grading."""
    queue = TL._read_queue(Path(mailbox))
    out: dict[str, dict] = {}
    for e in queue.get("retired", []):
        out[e.get("slice")] = e
    return out


def faults(mailbox: Path) -> list[dict]:
    """All parsed ``faults:`` entries, in file order."""
    queue = TL._read_queue(Path(mailbox))
    return list(queue.get("faults", []))

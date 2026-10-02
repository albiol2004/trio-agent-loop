"""Find a loop's transcripts by session id across harnesses (stdlib only).

Loaded by file path (no package-relative imports). A loop's transcripts are
located through explicit evidence only: session ids recorded in the mailbox
(`.native-launch.json`, `.session.json`, `.native-runs/`), in the native-run
registry and in Omnigent broker sessions, plus Codex/Cursor sessions whose
recorded start cwd IS the mailbox directory. Nothing is ever attached by
project/repo prefix.

Layouts (read-only):
  Claude   <config>/projects/<slug>/<id>.jsonl
           <config>/projects/<slug>/<id>/subagents/**.jsonl (+ sibling
           agent-<x>.meta.json with agentType/description)
           where <config> is ~/.claude and every ~/.profiles/*/claude
  Codex    <codex>/sessions/YYYY/MM/DD/rollout-*.jsonl, first line is a
           session_meta record {payload: {id, cwd, timestamp}}
  Cursor   ~/.cursor/chats/<hash>/<session-id>/meta.json {cwd, title, ...};
           the conversation itself lives in store.db (SQLite), so the
           descriptor path is meta.json (a readable record of the session).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HARNESSES = ("claude", "codex", "cursor", "omnigent")
RETENTION_LABEL = "deleted by retention"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_TOKEN_RE = re.compile(r"^ls-([0-9a-fA-F]{6,32})$")
_UUID_TAIL_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
_FIRST_LINE_CAP = 4 * 1024 * 1024
_FORK_SOURCE_LABEL = "omnigent.fork.source_id"
_IDENTITY_MODULE = None


def _identity():
    """dashboard/agent_identity.py, loaded once by file path."""
    global _IDENTITY_MODULE
    if _IDENTITY_MODULE is None:
        path = Path(__file__).resolve().with_name("agent_identity.py")
        spec = importlib.util.spec_from_file_location(
            "trio_transcript_index_agent_identity", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _IDENTITY_MODULE = module
    return _IDENTITY_MODULE


# --------------------------------------------------------------------- roots


def _real(path) -> str:
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


def _glob_profile_dirs(home: Path, leaf: str) -> list[Path]:
    """home/.<leaf> first, then home/.profiles/*/<leaf>; realpath-deduped."""
    candidates = [home / f".{leaf}"]
    profiles = home / ".profiles"
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        names = []
    candidates.extend(profiles / name / leaf for name in names)
    seen: set[str] = set()
    out: list[Path] = []
    for cand in candidates:
        real = _real(cand)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        out.append(Path(real))
    return out


def config_roots(home) -> list[Path]:
    """Existing Claude config dirs: realpath-deduped home/.claude and
    home/.profiles/*/claude."""
    return _glob_profile_dirs(Path(home), "claude")


# ---------------------------------------------------------------------- refs


def _valid_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SAFE_ID_RE.match(text) else None


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _harness_from_wrapper(wrapper) -> str | None:
    text = str(wrapper or "").strip().lower()
    for name in ("claude", "codex", "cursor"):
        if text.startswith(name):
            return name
    return None


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _same_mailbox(candidate, mailbox: str) -> bool:
    if not candidate or not isinstance(candidate, str):
        return False
    return _real(candidate) == mailbox


def mailbox_session_refs(mailbox, *, native_runs=(), broker_sessions=(),
                         resolve_prefix=None) -> list[dict]:
    """Deduped session references for one mailbox.

    Returns [{"session_id", "harness", "source"}] (broker refs also carry the
    session's `workspace`, its broker `id` and, for forks, `fork_source_id`,
    which feed agent identity). `resolve_prefix(hex)` maps
    a `ls-<hex>` run token to the Claude session ids whose dashless id starts
    with that hex; a token counts only when it yields exactly one id (without
    a resolver tokens are ignored).
    """
    mb = Path(mailbox)
    real_mb = _real(mb)
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(session_id, harness, source, **extra):
        sid = _valid_id(session_id)
        if sid is None:
            return
        key = (harness, sid)
        if key in seen:
            return
        seen.add(key)
        refs.append({"session_id": sid, "harness": harness, "source": source,
                     **extra})

    def from_token(token, source):
        match = _RUN_TOKEN_RE.match(str(token or "").strip())
        if not match or resolve_prefix is None:
            return
        try:
            ids = sorted(set(resolve_prefix(match.group(1).lower())))
        except Exception:
            return
        if len(ids) == 1:
            add(ids[0], "claude", source)

    launch = _read_json(mb / ".native-launch.json")
    if isinstance(launch, dict):
        args = launch.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        named = args.get("mailbox") if isinstance(args, dict) else None
        if named is None or _same_mailbox(named, real_mb):
            add(launch.get("session_id"), "claude", "native-launch")

    sidecar = _read_json(mb / ".session.json")
    if isinstance(sidecar, dict):
        from_token(sidecar.get("session"), "session-sidecar")

    try:
        run_files = sorted(os.listdir(mb / ".native-runs"))
    except OSError:
        run_files = []
    for name in run_files:
        if name.endswith(".start.json"):
            add(name.split(".", 1)[0], "claude", "native-runs")

    for run in native_runs or ():
        if not isinstance(run, dict):
            continue
        named = run.get("mailbox")
        if named is not None and not _same_mailbox(named, real_mb):
            continue
        if run.get("session_id"):
            add(run.get("session_id"), "claude", "native-runs")
        else:
            from_token(run.get("run_token"), "native-runs")

    for sess in broker_sessions or ():
        if not isinstance(sess, dict):
            continue
        labels = sess.get("labels") if isinstance(sess.get("labels"), dict) else {}
        harness = _harness_from_wrapper(labels.get("omnigent.wrapper"))
        external = sess.get("external_session_id")
        extra = {"workspace": _text(sess.get("workspace")),
                 "broker_id": _text(sess.get("id")),
                 "fork_source_id": _text(labels.get(_FORK_SOURCE_LABEL))}
        if harness and external:
            add(external, harness, "broker", **extra)
        else:
            add(sess.get("id"), "omnigent", "broker", **extra)
    return refs


# --------------------------------------------------------------------- index


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class TranscriptIndex:
    """Session-id keyed transcript locator with TTL + dir-mtime caches."""

    def __init__(self, home, *, ttl: float = 30.0):
        self.home = Path(home)
        self.ttl = float(ttl)
        self._lock = threading.RLock()
        self._returned: set[str] = set()
        self._dir_cache: dict = {}      # key -> {"sig", "checked", "value"}
        self._first_ts: dict[str, str] = {}
        self._meta_cache: dict[str, tuple] = {}     # path -> (mtime_ns, dict)
        self._codex_meta: dict[str, dict | None] = {}
        self._cursor_meta: dict[str, tuple] = {}    # path -> (mtime_ns, dict)
        self._claude_loc: dict[str, tuple[float, str | None]] = {}
        self._start_dirs: dict[tuple, tuple] = {}   # (kind, path) -> (mtime_ns, dir)

    # -- public ----------------------------------------------------------

    def is_indexed(self, path) -> bool:
        if not path:
            return False
        real = _real(path)
        with self._lock:
            return real in self._returned

    def sessions_for_mailbox(self, mailbox, *, native_runs=(),
                             broker_sessions=()) -> list[dict]:
        mb = Path(mailbox)
        real_mb = _real(mb)
        refs = mailbox_session_refs(
            mb, native_runs=native_runs, broker_sessions=broker_sessions,
            resolve_prefix=self._claude_ids_with_prefix)
        rows: list[dict] = []
        taken: set[tuple[str, str]] = set()
        by_id = {s["id"]: s for s in broker_sessions or ()
                 if isinstance(s, dict) and isinstance(s.get("id"), str)}

        for ref in refs:
            key = (ref["harness"], ref["session_id"])
            if key in taken:
                continue
            taken.add(key)
            rows.extend(self._rows_for_ref(ref, by_id))

        cwd_targets = {os.path.normpath(str(mb)), real_mb}
        for harness, finder in (("codex", self._codex_by_cwd),
                                ("cursor", self._cursor_by_cwd)):
            for found in finder(cwd_targets):
                key = (harness, found["id"])
                if key in taken:
                    continue
                taken.add(key)
                rows.append(self._row_for_found(found, source="cwd"))

        with self._lock:
            for row in rows:
                if row["path"]:
                    self._returned.add(row["path"])
        return rows

    # -- caches ----------------------------------------------------------

    def _cached_listing(self, key, build_fn):
        """Value from build_fn(), reused for `ttl` s, then while every dir in
        the recorded signature keeps its mtime."""
        now = time.monotonic()
        with self._lock:
            entry = self._dir_cache.get(key)
        if entry is not None:
            if now - entry["checked"] < self.ttl:
                return entry["value"]
            if entry["sig"] == self._signature(entry["dirs"]):
                entry["checked"] = now
                return entry["value"]
        dirs, value = build_fn()
        entry = {"dirs": dirs, "sig": self._signature(dirs),
                 "checked": now, "value": value}
        with self._lock:
            self._dir_cache[key] = entry
        return value

    @staticmethod
    def _signature(dirs) -> tuple:
        sig = []
        for d in dirs:
            try:
                sig.append((d, os.stat(d).st_mtime_ns))
            except OSError:
                sig.append((d, None))
        return tuple(sig)

    # -- Claude ----------------------------------------------------------

    def _slug_dirs(self, root: Path) -> list[str]:
        projects = os.path.join(str(root), "projects")

        def build():
            try:
                names = sorted(os.listdir(projects))
            except OSError:
                names = []
            return [projects], names

        return self._cached_listing(("slugs", projects), build)

    def _claude_ids_with_prefix(self, hex_prefix: str) -> list[str]:
        ids: set[str] = set()
        for root in config_roots(self.home):
            projects = os.path.join(str(root), "projects")
            for slug in self._slug_dirs(root):
                for name in self._list_slug(os.path.join(projects, slug)):
                    if name.endswith(".jsonl"):
                        sid = name[:-6]
                        if sid.replace("-", "").lower().startswith(hex_prefix):
                            ids.add(sid)
        return sorted(ids)

    def _list_slug(self, slug_dir: str) -> list[str]:
        def build():
            try:
                return [slug_dir], os.listdir(slug_dir)
            except OSError:
                return [slug_dir], []

        return self._cached_listing(("slug", slug_dir), build)

    def _find_claude_parent(self, session_id: str):
        """-> (config_root, slug_dir, parent_realpath) or None."""
        for root in config_roots(self.home):
            projects = os.path.join(str(root), "projects")
            real_projects = _real(projects)
            for slug in self._slug_dirs(root):
                cand = os.path.join(projects, slug, session_id + ".jsonl")
                if not os.path.isfile(cand):
                    continue
                real = _real(cand)
                if not _under(real, real_projects):
                    continue
                return root, os.path.join(projects, slug), real
        return None

    def _claude_location(self, session_id: str):
        now = time.monotonic()
        with self._lock:
            cached = self._claude_loc.get(session_id)
        if cached is not None:
            stamp, value = cached
            if value is not None and os.path.isfile(value[2]):
                return value
            if value is None and now - stamp < self.ttl:
                return None
        value = self._find_claude_parent(session_id)
        with self._lock:
            self._claude_loc[session_id] = (now, value)
        return value

    def _subagent_files(self, sub_root: str) -> list[str]:
        def build():
            dirs: list[str] = []
            files: list[str] = []
            if not os.path.isdir(sub_root):
                return [sub_root], files
            for cur, subdirs, names in os.walk(sub_root):
                dirs.append(cur)
                subdirs.sort()
                files.extend(os.path.join(cur, n) for n in sorted(names)
                             if n.endswith(".jsonl"))
            return dirs, files

        return self._cached_listing(("sub", sub_root), build)

    def _meta_for(self, jsonl: str) -> dict:
        meta_path = jsonl[:-len(".jsonl")] + ".meta.json"
        try:
            mtime = os.stat(meta_path).st_mtime_ns
        except OSError:
            return {}
        with self._lock:
            cached = self._meta_cache.get(meta_path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = _read_json(Path(meta_path))
        data = data if isinstance(data, dict) else {}
        with self._lock:
            self._meta_cache[meta_path] = (mtime, data)
        return data

    def _first_timestamp(self, path: str, mtime: float | None) -> str:
        with self._lock:
            hit = self._first_ts.get(path)
        if hit:
            return hit
        found = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for _ in range(64):
                    line = fh.readline(_FIRST_LINE_CAP)
                    if not line:
                        break
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = rec.get("timestamp") if isinstance(rec, dict) else None
                    if isinstance(ts, str) and ts:
                        found = ts
                        break
        except OSError:
            pass
        if found:
            with self._lock:
                self._first_ts[path] = found
            return found
        return _iso_from_mtime(mtime) if mtime is not None else ""

    @staticmethod
    def _stat(path: str):
        try:
            st = os.stat(path)
            return st.st_size, st.st_mtime
        except OSError:
            return None, None

    # -- agent identity ------------------------------------------------

    def _start_dir_of(self, kind: str, path: str) -> str | None:
        """Start dir read from a transcript, cached per (path, mtime)."""
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        key = (kind, path)
        with self._lock:
            cached = self._start_dirs.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        ident = _identity()
        reader = ident.codex_start_dir if kind == "codex" \
            else ident.claude_start_dir
        value = reader(path)
        with self._lock:
            self._start_dirs[key] = (mtime, value)
        return value

    @staticmethod
    def _fork_identity(ref: dict, by_id: dict, seen=()) -> dict | None:
        """Identity row of the broker session this ref forked from, when that
        session is in the same listing (workspace-based, followed through
        chained forks); None otherwise."""
        source_id = ref.get("fork_source_id")
        source = by_id.get(source_id) if source_id else None
        if source is None or source_id in seen:
            return None
        ident = _identity()
        labels = source.get("labels") if isinstance(source.get("labels"), dict) \
            else {}
        upstream = {"fork_source_id": _text(labels.get(_FORK_SOURCE_LABEL))}
        found = TranscriptIndex._fork_identity(
            upstream, by_id, tuple(seen) + (source_id,))
        if found is not None:
            return found
        row = ident.stamp({}, _text(source.get("workspace")),
                          "broker-workspace")
        return row if row["identity"] is not None else None

    def _stamp_parent(self, row: dict, ref: dict, own_dir, own_source: str,
                      by_id) -> dict:
        """Identity of a parent row: a fork inherits its source session; else
        a broker ref's workspace; else the transcript's own first cwd."""
        ident = _identity()
        fork = self._fork_identity(ref, by_id or {})
        if fork is not None:
            return ident.inherit(row, fork)
        if ref.get("source") == "broker" and ref.get("workspace"):
            return ident.stamp(row, ref["workspace"], "broker-workspace")
        return ident.stamp(row, own_dir, own_source)

    def _claude_rows(self, ref: dict, by_id=None) -> list[dict]:
        sid = ref["session_id"]
        loc = self._claude_location(sid)
        if loc is None:
            return [self._deleted_row(ref, by_id)]
        root, slug_dir, parent_real = loc
        size, mtime = self._stat(parent_real)
        parent_row = {
            "id": sid, "label": sid,
            "timestamp": self._first_timestamp(parent_real, mtime),
            "path": parent_real, "size": size, "kind": "parent",
            "parent_id": None, "parent_path": None,
            "harness": "claude", "source": ref["source"], "status": "ok",
            "agent_type": None, "description": None, "workflow": None,
        }
        self._stamp_parent(parent_row, ref,
                           self._start_dir_of("claude", parent_real),
                           "claude-transcript-cwd", by_id)
        rows = [parent_row]
        sub_root = os.path.join(slug_dir, sid, "subagents")
        real_projects = _real(os.path.join(str(root), "projects"))
        seen: set[str] = {parent_real}
        for sub in self._subagent_files(sub_root):
            real = _real(sub)
            if real in seen or not _under(real, real_projects):
                continue
            seen.add(real)
            meta = self._meta_for(sub)
            rel = os.path.relpath(sub, sub_root).split(os.sep)
            workflow = rel[1] if len(rel) >= 3 and rel[0] == "workflows" else None
            agent_type = meta.get("agentType")
            description = meta.get("description")
            stem = os.path.basename(sub)[:-len(".jsonl")]
            size, mtime = self._stat(real)
            label = stem
            if agent_type or description:
                label = f"{agent_type or 'agent'}: {description or stem}"
            rows.append(_identity().inherit({
                "id": stem, "label": label,
                "timestamp": self._first_timestamp(real, mtime),
                "path": real, "size": size, "kind": "subagent",
                "parent_id": sid, "parent_path": parent_real,
                "harness": "claude", "source": ref["source"], "status": "ok",
                "agent_type": agent_type if isinstance(agent_type, str) else None,
                "description": description if isinstance(description, str) else None,
                "workflow": workflow,
            }, parent_row))
        return rows

    # -- Codex -----------------------------------------------------------

    def _codex_files(self) -> list[tuple[str, str]]:
        """[(root_real, rollout path)] over every codex home's sessions/."""
        roots = _glob_profile_dirs(self.home, "codex")

        def build():
            dirs: list[str] = []
            files: list[tuple[str, str]] = []
            for root in roots:
                base = os.path.join(str(root), "sessions")
                if not os.path.isdir(base):
                    continue
                for cur, subdirs, names in os.walk(base):
                    dirs.append(cur)
                    subdirs.sort()
                    files.extend(
                        (str(root), os.path.join(cur, n)) for n in sorted(names)
                        if n.startswith("rollout-") and n.endswith(".jsonl"))
            return dirs, files

        return self._cached_listing(("codex", tuple(str(r) for r in roots)), build)

    def _codex_meta_of(self, path: str) -> dict | None:
        with self._lock:
            if path in self._codex_meta:
                return self._codex_meta[path]
        meta = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                line = fh.readline(_FIRST_LINE_CAP)
            rec = json.loads(line)
            if isinstance(rec, dict):
                payload = rec.get("payload") if rec.get("type") == "session_meta" \
                    and isinstance(rec.get("payload"), dict) else rec
                sid = payload.get("id") or payload.get("session_id")
                tail = _UUID_TAIL_RE.search(os.path.basename(path)[:-6])
                sid = sid or (tail.group(1) if tail else None)
                cwd = payload.get("cwd")
                if _valid_id(sid):
                    meta = {
                        "id": sid,
                        "cwd": os.path.normpath(cwd) if isinstance(cwd, str)
                        and cwd else None,
                        "timestamp": payload.get("timestamp")
                        or rec.get("timestamp"),
                    }
        except (OSError, ValueError):
            meta = None
        with self._lock:
            self._codex_meta[path] = meta
        return meta

    def _codex_entries(self):
        for root, path in self._codex_files():
            meta = self._codex_meta_of(path)
            if meta:
                yield root, path, meta

    def _codex_found(self, root, path, meta) -> dict:
        return {"harness": "codex", "id": meta["id"], "path": path,
                "timestamp": meta.get("timestamp"), "root": root,
                "label": f"codex {meta['id']}", "allowed_root": root}

    def _codex_by_cwd(self, targets: set[str]) -> list[dict]:
        return [self._codex_found(r, p, m) for r, p, m in self._codex_entries()
                if m.get("cwd") in targets]

    def _codex_by_id(self, session_id: str) -> dict | None:
        for root, path, meta in self._codex_entries():
            if meta["id"] == session_id:
                return self._codex_found(root, path, meta)
        return None

    # -- Cursor ----------------------------------------------------------

    def _cursor_files(self) -> list[tuple[str, str, str]]:
        """[(root_real, session_id, meta.json path)] from chats/*/*/meta.json."""
        roots = _glob_profile_dirs(self.home, "cursor")

        def build():
            dirs: list[str] = []
            out: list[tuple[str, str, str]] = []
            for root in roots:
                chats = os.path.join(str(root), "chats")
                dirs.append(chats)
                try:
                    hashes = sorted(os.listdir(chats))
                except OSError:
                    continue
                for h in hashes:
                    hdir = os.path.join(chats, h)
                    dirs.append(hdir)
                    try:
                        sids = sorted(os.listdir(hdir))
                    except OSError:
                        continue
                    for sid in sids:
                        meta = os.path.join(hdir, sid, "meta.json")
                        if _valid_id(sid) and os.path.isfile(meta):
                            out.append((str(root), sid, meta))
            return dirs, out

        return self._cached_listing(("cursor", tuple(str(r) for r in roots)), build)

    def _cursor_meta_of(self, path: str) -> dict | None:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        with self._lock:
            cached = self._cursor_meta.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = _read_json(Path(path))
        data = data if isinstance(data, dict) else {}
        cwd = data.get("cwd")
        meta = {
            "cwd": os.path.normpath(cwd) if isinstance(cwd, str) and cwd else None,
            "title": data.get("title") if isinstance(data.get("title"), str) else None,
            "created_ms": data.get("createdAtMs"),
        }
        with self._lock:
            self._cursor_meta[path] = (mtime, meta)
        return meta

    def _cursor_found(self, root, sid, path, meta) -> dict:
        created = meta.get("created_ms")
        ts = None
        if isinstance(created, (int, float)):
            ts = _iso_from_mtime(created / 1000.0)
        return {"harness": "cursor", "id": sid, "path": path, "timestamp": ts,
                "root": root, "label": meta.get("title") or f"cursor {sid}",
                "allowed_root": root, "cwd": meta.get("cwd")}

    def _cursor_by_cwd(self, targets: set[str]) -> list[dict]:
        out = []
        for root, sid, path in self._cursor_files():
            meta = self._cursor_meta_of(path)
            if meta and meta["cwd"] in targets:
                out.append(self._cursor_found(root, sid, path, meta))
        return out

    def _cursor_by_id(self, session_id: str) -> dict | None:
        for root, sid, path in self._cursor_files():
            if sid == session_id:
                meta = self._cursor_meta_of(path)
                if meta is not None:
                    return self._cursor_found(root, sid, path, meta)
        return None

    # -- shared ----------------------------------------------------------

    def _row_for_found(self, found: dict, *, source: str, ref=None,
                       by_id=None) -> dict:
        real = _real(found["path"])
        size, mtime = self._stat(real)
        ref = ref or {"session_id": found["id"], "harness": found["harness"],
                      "source": source}
        if not _under(real, _real(found["allowed_root"])):
            return self._deleted_row({
                "session_id": found["id"], "harness": found["harness"],
                "source": source,
                **{k: ref.get(k) for k in ("workspace", "fork_source_id")}},
                by_id)
        ts = found.get("timestamp") or (
            _iso_from_mtime(mtime) if mtime is not None else "")
        row = {
            "id": found["id"], "label": found["label"], "timestamp": ts,
            "path": real, "size": size, "kind": "parent",
            "parent_id": None, "parent_path": None,
            "harness": found["harness"], "source": source, "status": "ok",
            "agent_type": None, "description": None, "workflow": None,
        }
        if found["harness"] == "codex":
            own_dir = self._start_dir_of("codex", real)
            own_source = "codex-session-meta"
        else:
            own_dir = found.get("cwd")
            own_source = "cursor-cwd"
        return self._stamp_parent(row, ref, own_dir, own_source, by_id)

    def _deleted_row(self, ref: dict, by_id=None) -> dict:
        sid = ref["session_id"]
        row = {
            "id": sid, "label": f"{sid} ({RETENTION_LABEL})",
            "timestamp": "", "path": None, "size": None, "kind": "parent",
            "parent_id": None, "parent_path": None,
            "harness": ref["harness"], "source": ref["source"],
            "status": "deleted", "agent_type": None, "description": None,
            "workflow": None,
        }
        return self._stamp_parent(row, ref, None, "unavailable", by_id)

    def _rows_for_ref(self, ref: dict, by_id=None) -> list[dict]:
        harness = ref["harness"]
        if harness == "claude":
            return self._claude_rows(ref, by_id)
        if harness == "codex":
            found = self._codex_by_id(ref["session_id"])
        elif harness == "cursor":
            found = self._cursor_by_id(ref["session_id"])
        else:
            # Omnigent broker ids have no transcript file here (the mailbox
            # .sessions/ exports are listed by the caller); never "deleted".
            return []
        if found is None:
            return [self._deleted_row(ref, by_id)]
        return [self._row_for_found(found, source=ref["source"], ref=ref,
                                    by_id=by_id)]

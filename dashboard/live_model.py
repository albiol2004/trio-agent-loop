"""live_model.py — in-memory overview model with keyed deltas (stdlib only).

The dashboard server keeps the last overview build in a `LiveModel`. Every
scan hands the fresh overview to `LiveModel.apply`, which diffs it against
the stored one entity by entity and returns a delta batch (or None when
nothing changed). Batches are numbered (`seq`) inside one `epoch` (random per
model instance, i.e. per server process) and kept in a bounded log so a
reconnecting client can replay what it missed.

Entities (kind, id, data):
    meta       "overview"     overview minus ``workspaces`` plus
                              ``workspace_order`` (roots in overview order)
    workspace  <entry root>   entry minus ``loops`` / ``inbox``
    loop       loop_entity_id {"root": root, "card": card}
    inbox      item["id"]     {"root": root, "item": item}

Change item: ``{"op": "upsert"|"remove", "kind", "id", "data"}``. An upsert
carries the whole entity and is emitted only when the entity differs after
`VOLATILE_KEYS` are removed (recursively); a remove carries ``data: None``.
Delta batch: ``{"epoch", "seq", "from_seq", "updated_at", "changes": [...]}``.

VOLATILE_KEYS (values that change on every scan of an unchanged tree, so they
never count as a change on their own; they still ride along whenever the
entity is upserted for another reason):
    updated_at   overview / board build timestamp
    elapsed_ms   per-workspace and per-overview scan duration
    age_s        heartbeat age in seconds (grows with the wall clock)
    Verified by scanning a fixture (fresh heartbeat, driver sidecar, inbox
    item) twice and, read-only, two consecutive overview builds over this
    repository's real workspaces: no other key differed between scans of an
    unchanged tree (see registry/tests/test_live_model.py).
"""
from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from datetime import datetime, timezone

VOLATILE_KEYS = frozenset({"updated_at", "elapsed_ms", "age_s"})


def loop_entity_id(root: str, card: dict) -> str:
    """Stable id of a loop card: its canonical ``loop_id`` when it has one,
    else ``path:<workspace root>::<mailbox name>``."""
    loop_id = card.get("loop_id")
    if loop_id:
        return loop_id
    return "path:" + str(root) + "::" + str(card.get("name"))


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip(v) for v in value]
    return value


def _fingerprint(data) -> str:
    return json.dumps(_strip(data), sort_keys=True, default=str)


def _entities(overview: dict) -> dict:
    """Flatten an overview into ``{(kind, id): data}`` (insertion ordered)."""
    workspaces = overview.get("workspaces") or []
    out: dict = {}
    meta = {k: v for k, v in overview.items() if k != "workspaces"}
    meta["workspace_order"] = [ws.get("root") for ws in workspaces]
    out[("meta", "overview")] = meta
    for ws in workspaces:
        root = ws.get("root")
        out[("workspace", root)] = {
            k: v for k, v in ws.items() if k not in ("loops", "inbox")}
        for card in ws.get("loops") or []:
            out[("loop", loop_entity_id(root, card))] = {
                "root": root, "card": card}
        for item in ws.get("inbox") or []:
            out[("inbox", item.get("id"))] = {"root": root, "item": item}
    return out


class LiveModel:
    """Thread-safe overview store producing numbered keyed delta batches."""

    def __init__(self, max_log: int = 256):
        self.epoch = secrets.token_hex(8)
        self.seq = 0
        self.last_scan_at: str | None = None
        self.ticks = 0
        self._overview: dict | None = None
        self._entities: dict = {}
        self._prints: dict = {}
        self._log: deque = deque(maxlen=max(1, int(max_log)))
        self._closed = False
        self._cond = threading.Condition(threading.Lock())

    @property
    def closed(self) -> bool:
        return self._closed

    def apply(self, overview: dict) -> dict | None:
        """Diff ``overview`` against the stored one; return the new delta
        batch, or None when nothing changed (a scan tick still wakes
        waiters)."""
        entities = _entities(overview)
        prints = {key: _fingerprint(data) for key, data in entities.items()}
        with self._cond:
            changes = []
            for key, data in entities.items():
                if self._prints.get(key) != prints[key]:
                    changes.append({"op": "upsert", "kind": key[0],
                                    "id": key[1], "data": data})
            for key in self._prints:
                if key not in entities:
                    changes.append({"op": "remove", "kind": key[0],
                                    "id": key[1], "data": None})
            batch = None
            if changes:
                batch = {
                    "epoch": self.epoch,
                    "seq": self.seq + 1,
                    "from_seq": self.seq,
                    "updated_at": overview.get("updated_at"),
                    "changes": changes,
                }
                self.seq += 1
                self._log.append(batch)
            self._overview = overview
            self._entities = entities
            self._prints = prints
            self.ticks += 1
            self.last_scan_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            self._cond.notify_all()
            return batch

    def snapshot(self) -> dict | None:
        """The stored overview (shallow copy) plus ``epoch`` and ``seq``."""
        with self._cond:
            if self._overview is None:
                return None
            snap = dict(self._overview)
            snap["epoch"] = self.epoch
            snap["seq"] = self.seq
            return snap

    def changes_since(self, epoch: str, seq: int) -> list[dict] | None:
        """Batches with ``seq`` greater than the given one; ``[]`` when the
        caller is current; None when it must resync from a snapshot (foreign
        epoch, a seq from the future, or a log that no longer reaches back)."""
        with self._cond:
            if epoch != self.epoch or seq > self.seq:
                return None
            if seq == self.seq:
                return []
            if not self._log or self._log[0]["seq"] > seq + 1:
                return None
            return [b for b in self._log if b["seq"] > seq]

    def wait(self, seq: int, timeout: float) -> bool:
        """Block until ``seq`` is passed, a scan tick / kick arrives, the
        model closes or ``timeout`` elapses; True iff ``self.seq > seq``."""
        with self._cond:
            if self._closed or self.seq > seq:
                return self.seq > seq
            ticks = self.ticks
            self._cond.wait_for(
                lambda: self._closed or self.seq > seq or self.ticks != ticks,
                timeout=max(0.0, timeout))
            return self.seq > seq

    def kick(self) -> None:
        """Wake waiters without a scan (they see no new seq)."""
        with self._cond:
            self.ticks += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

"""olqueue.py -- the driver-side QUEUE.md append/reconcile guard.

No git repo is needed here (unlike test_steplib.py): everything these
functions touch is QUEUE.md (and, for the repo-tagged-entry test, an
absent PLAN.md, which `TL._read_queue` treats as "no repo info to check",
MAILBOX-SCHEMA.md r15) under a plain `tmp_path` mailbox directory.
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

import pytest

from trio_opencode import olqueue
from trio_opencode import steplib

TL = steplib.TL


def mbox(tmp_path: Path) -> Path:
    box = tmp_path / "loop"
    box.mkdir()
    return box


# --------------------------------------------------------------- append_retired

def test_append_retired_creates_queue_md_when_missing(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    ok = olqueue.append_retired(
        box, slice_id="status-parse",
        sha="af7d8220c4d606f549c4a374c9e22b6a6a03ec04",
        at="2026-09-27T03:03:20Z",
    )
    assert ok is True
    queue_path = box / "QUEUE.md"
    assert queue_path.is_file()

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert queue["retired"] == [{
        "slice": "status-parse",
        "sha": "af7d8220c4d606f549c4a374c9e22b6a6a03ec04",
        "at": "2026-09-27T03:03:20Z",
    }]


def test_append_retired_inserts_into_existing_fence_with_entries(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    (box / "QUEUE.md").write_text(
        "# Queue\n\n"
        "```yaml\n"
        "retired:\n"
        "  - slice: status-parse\n"
        "    sha: af7d8220c4d606f549c4a374c9e22b6a6a03ec04\n"
        "    at: 2026-09-27T03:03:20Z\n"
        "```\n",
        encoding="utf-8",
    )
    ok = olqueue.append_retired(
        box, slice_id="cli-whoami",
        sha="e688fdf5493dac69e4db40345a69aa1287cd6aa1",
        at="2026-09-27T03:04:53Z",
    )
    assert ok is True

    text = (box / "QUEUE.md").read_text(encoding="utf-8")
    # The existing entry's line order/content is untouched, and the new one
    # is the LAST item inside the fence, before its closing ```.
    assert text.index("status-parse") < text.index("cli-whoami")
    assert text.rstrip().endswith("```")

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert [e["slice"] for e in queue["retired"]] == ["status-parse", "cli-whoami"]


def test_append_retired_repo_key_order(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    olqueue.append_retired(
        box, slice_id="api-route", repo="app-backend",
        sha="b" * 40, at="2026-09-28T00:00:00Z",
    )
    text = (box / "QUEUE.md").read_text(encoding="utf-8")
    # Canonical order per prompts/canonical/lead.md "Multi-repo":
    # slice:, repo: <name>, sha:, at:
    lines = [l.strip() for l in text.splitlines() if l.strip().split(":")[0].strip("- ") in
             ("slice", "repo", "sha", "at")]
    keys = [l.split(":", 1)[0].lstrip("- ").strip() for l in lines]
    assert keys == ["slice", "repo", "sha", "at"]

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert queue["retired"][0]["repo"] == "app-backend"
    assert queue["retired"][0]["slice"] == "api-route"


def test_append_retired_is_idempotent_on_duplicate_slice_sha(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    sha = "c" * 40
    first = olqueue.append_retired(box, slice_id="app", sha=sha, at="2026-09-28T00:00:00Z")
    second = olqueue.append_retired(box, slice_id="app", sha=sha, at="2026-09-28T00:00:00Z")
    assert first is True
    assert second is False

    queue = TL._read_queue(box)
    assert len(queue["retired"]) == 1


def test_append_retired_self_check_failure_restores_previous_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = mbox(tmp_path)
    olqueue.append_retired(box, slice_id="app", sha="d" * 40, at="2026-09-28T00:00:00Z")
    before_text = (box / "QUEUE.md").read_text(encoding="utf-8")

    # Force the post-write self-check to see a count that never grows,
    # simulating a corrupted write without actually depending on *how* the
    # corruption happened.
    monkeypatch.setattr(olqueue, "_count_fence_entries", lambda *a, **k: 0)

    with pytest.raises(olqueue.QueueError):
        olqueue.append_retired(box, slice_id="app2", sha="e" * 40, at="2026-09-28T00:01:00Z")

    after_text = (box / "QUEUE.md").read_text(encoding="utf-8")
    assert after_text == before_text

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert len(queue["retired"]) == 1
    assert queue["retired"][0]["slice"] == "app"


# ------------------------------------------------------------------- reconcile

def _write_faults_block(box: Path, entries_text: str) -> None:
    path = box / "QUEUE.md"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = "```yaml\nfaults:\n" + entries_text + "```\n"
    path.write_text(existing + ("\n" if existing and not existing.endswith("\n\n") else "") + block,
                     encoding="utf-8")


def test_reconcile_readds_a_dropped_retired_entry_and_a_dropped_fault(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    guard = olqueue.QueueGuard(box)

    olqueue.append_retired(box, slice_id="ol-a", sha="1" * 40, at="2026-09-29T00:00:00Z")
    _write_faults_block(
        box,
        "  - id: f1\n"
        "    slice: ol-a\n"
        "    observed_at: " + "1" * 40 + "\n"
        "    scope: [app.py]\n"
        "    reason: missing error handling\n"
        "    status: open\n",
    )
    guard.observe()

    # Simulate a concurrent LLM turn that rewrites QUEUE.md from scratch and
    # drops both the retired entry and the fault (e.g. it only knew about
    # its own new entry and re-serialized the file from a stale read).
    (box / "QUEUE.md").write_text(
        "```yaml\nretired:\n```\n\n```yaml\nfaults:\n```\n", encoding="utf-8",
    )
    before = TL._read_queue(box)
    assert before["retired"] == []
    assert before["faults"] == []

    notes = guard.reconcile("evaluator-1")

    assert any("re-appended retired ol-a" in n for n in notes), notes
    assert any("re-appended fault f1" in n for n in notes), notes

    after = TL._read_queue(box)
    assert after["errors"] == []
    assert [e["slice"] for e in after["retired"]] == ["ol-a"]
    assert after["retired"][0]["sha"] == "1" * 40
    assert after["retired"][0]["at"] == "2026-09-29T00:00:00Z"
    assert [f["id"] for f in after["faults"]] == ["f1"]
    assert after["faults"][0]["status"] == "open"


def test_reconcile_renumbers_later_duplicate_fault_id(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    guard = olqueue.QueueGuard(box)

    # Two concurrent slice-evals both pick "f1" as the next free id for
    # different faults.
    _write_faults_block(
        box,
        "  - id: f1\n"
        "    slice: ol-a\n"
        "    observed_at: " + "2" * 40 + "\n"
        "    scope: [a.py]\n"
        "    reason: first fault\n"
        "    status: open\n"
        "  - id: f1\n"
        "    slice: ol-b\n"
        "    observed_at: " + "3" * 40 + "\n"
        "    scope: [b.py]\n"
        "    reason: second fault\n"
        "    status: open\n",
    )
    guard.observe()
    notes = guard.reconcile("lead-1")

    assert any("renumbered duplicate fault id f1 -> f2" in n for n in notes), notes

    after = TL._read_queue(box)
    assert after["errors"] == []
    assert len(after["faults"]) == 2
    ids = [f["id"] for f in after["faults"]]
    assert ids == ["f1", "f2"]
    assert after["faults"][0]["reason"] == "first fault"
    assert after["faults"][1]["reason"] == "second fault"
    assert after["faults"][1]["slice"] == "ol-b"
    assert after["faults"][1]["status"] == "open"

    # A renumbered fault is the same fault: further reconciles must neither
    # re-append the pre-rename copy nor renumber again (no growth).
    for i in range(3):
        assert guard.reconcile(f"again-{i}") == []
    final = TL._read_queue(box)
    assert [f["id"] for f in final["faults"]] == ["f1", "f2"]


def test_reconcile_never_reverts_a_status_change(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    guard = olqueue.QueueGuard(box)

    sha = "4" * 40
    _write_faults_block(
        box,
        "  - id: f1\n"
        "    slice: ol-a\n"
        f"    observed_at: {sha}\n"
        "    scope: [a.py]\n"
        "    reason: needs a fix\n"
        "    status: open\n",
    )
    guard.observe()
    assert guard._faults[("ol-a", sha, "needs a fix")]["status"] == "open"

    # The Lead legitimately transitions status open -> taken.
    text = (box / "QUEUE.md").read_text(encoding="utf-8")
    text = text.replace("status: open", "status: taken")
    (box / "QUEUE.md").write_text(text, encoding="utf-8")

    notes = guard.reconcile("lead-2")
    assert notes == []  # the fault is present (same identity); nothing to re-add

    after = TL._read_queue(box)
    assert after["faults"][0]["status"] == "taken"
    # The guard's own memory picks up the new status too (never fights it).
    assert guard._faults[("ol-a", sha, "needs a fix")]["status"] == "taken"


def test_reconcile_leaves_a_malformed_faults_fence_untouched(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    guard = olqueue.QueueGuard(box)

    olqueue.append_retired(box, slice_id="ol-a", sha="5" * 40, at="2026-09-29T00:00:00Z")
    # Missing `reason:` -> a dropped/malformed entry, reported as a
    # `` `faults:` block: `` error; the fence itself must be left alone.
    _write_faults_block(
        box,
        "  - id: f1\n"
        "    slice: ol-a\n"
        "    observed_at: " + "5" * 40 + "\n"
        "    scope: [a.py]\n"
        "    status: open\n",
    )
    before_queue = TL._read_queue(box)
    assert any(e.startswith("`faults:` block:") for e in before_queue["errors"])
    before_text = (box / "QUEUE.md").read_text(encoding="utf-8")

    guard.observe()
    notes = guard.reconcile("evaluator-2")

    after_text = (box / "QUEUE.md").read_text(encoding="utf-8")
    assert after_text == before_text
    after_queue = TL._read_queue(box)
    assert after_queue["errors"] == before_queue["errors"]
    assert not any("faults" in n for n in notes)


# -------------------------------------------------------------- concurrency

def test_concurrent_appends_and_reconciling_drops_converge(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    guard = olqueue.QueueGuard(box)
    n_appenders = 8
    n_droppers = 4
    errors: list[BaseException] = []
    shas = [f"{i:040x}" for i in range(n_appenders)]
    expected = {(f"ol-{i}", shas[i]) for i in range(n_appenders)}

    def appender(i: int) -> None:
        try:
            time.sleep(random.uniform(0, 0.02))
            olqueue.append_retired(
                box, slice_id=f"ol-{i}", sha=shas[i], at=f"2026-09-30T00:00:{i:02d}Z",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def dropper(_i: int) -> None:
        try:
            for _ in range(3):
                time.sleep(random.uniform(0, 0.02))
                # The read-modify-write "drop" is done under the lock (so it
                # is atomic from this thread's point of view and never races
                # a concurrent appender's own atomic write), but
                # guard.reconcile() is called AFTER releasing it -- it takes
                # the same lock itself, and nesting two flock() acquisitions
                # from the same thread on two different file descriptors to
                # the same file would deadlock (flock locks belong to the
                # open file description, not the process).
                with olqueue.queue_lock(box):
                    guard._observe_locked()
                    queue = TL._read_queue(box)
                    retired = queue.get("retired", [])
                    if retired:
                        victim = random.choice(retired)
                        kept = [e for e in retired if e is not victim]
                        lines = ["```yaml\n", "retired:\n"]
                        for e in kept:
                            lines.append(f"  - slice: {e['slice']}\n")
                            if e.get("repo"):
                                lines.append(f"    repo: {e['repo']}\n")
                            lines.append(f"    sha: {e['sha']}\n")
                            lines.append(f"    at: {e['at']}\n")
                        lines.append("```\n")
                        olqueue._atomic_write(box / "QUEUE.md", "".join(lines))
                guard.reconcile("dropper")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = (
        [threading.Thread(target=appender, args=(i,)) for i in range(n_appenders)]
        + [threading.Thread(target=dropper, args=(i,)) for i in range(n_droppers)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors

    # Final convergence: observe whatever is there, then reconcile once
    # more so any drop from the very last dropper pass is repaired too.
    with olqueue.queue_lock(box):
        guard._observe_locked()
    guard.reconcile("final")

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    seen = {(e["slice"], e["sha"]) for e in queue["retired"]}
    assert seen == expected
    # Exactly one entry per slice -- reconcile's dedupe (via append_retired's
    # own idempotency check) never double-inserts a re-appended entry.
    assert len(queue["retired"]) == len(expected)


# ------------------------------------------------- ol-harden2: agent write races
#
# Role agents (slice-evals, the Lead) write QUEUE.md directly with a
# non-atomic truncate-then-write and cannot be locked. The driver's
# read-modify-write must tolerate that: never restore a stale snapshot over
# newer content, never lose an agent's entry because it read mid-truncate.

_BASE_QUEUE = (
    "# Queue\n\n"
    "```yaml\n"
    "retired:\n"
    "  - slice: a\n"
    f"    sha: {'a' * 40}\n"
    "    at: 2026-10-01T00:00:00Z\n"
    "```\n"
)
_AGENT_FAULT = (
    "\n```yaml\n"
    "faults:\n"
    "  - id: f1\n"
    "    slice: d\n"
    "    observed_at: 2026-10-01T00:05:00Z\n"
    "    scope: [d.py]\n"
    "    reason: slice-eval ITERATE\n"
    "    status: open\n"
    "```\n"
)


def _truncating_agent_after_driver_write(monkeypatch: pytest.MonkeyPatch, box: Path,
                                         agent_content: str, hold: float = 0.12) -> list:
    """After the driver's FIRST atomic write, an 'agent' opens QUEUE.md with
    'w' (truncating the freshly renamed file) and fills it ``hold`` seconds
    later with ``agent_content`` -- computed from its own older read, so it
    lacks the driver's entry. This is the evaluator's r4 reproducer."""
    real = olqueue._atomic_write
    fired: list = []
    monkeypatch.setattr(olqueue, "_EMPTY_READS", 80, raising=False)

    def hooked(path, text):
        real(path, text)
        if fired or Path(path).name != "QUEUE.md":
            return
        fh = open(path, "w", encoding="utf-8")  # truncates now
        fired.append(1)

        def fill():
            time.sleep(hold)
            fh.write(agent_content)
            fh.close()

        t = threading.Thread(target=fill)
        t.start()
        fired.append(t)

    monkeypatch.setattr(olqueue, "_atomic_write", hooked)
    return fired


def test_append_retired_survives_agent_truncate_during_self_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = mbox(tmp_path)
    (box / "QUEUE.md").write_text(_BASE_QUEUE, encoding="utf-8")
    fired = _truncating_agent_after_driver_write(monkeypatch, box, _BASE_QUEUE + _AGENT_FAULT)

    # Before the fix this raised QueueError ("retired entry count 2 -> 0 ...
    # QUEUE.md restored") and the restore dropped the agent's fault.
    assert olqueue.append_retired(box, slice_id="b", sha="b" * 40, at="2026-10-01T00:06:00Z") is True
    for t in fired[1:]:
        t.join(timeout=5)

    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert {(e["slice"], e["sha"]) for e in queue["retired"]} == {("a", "a" * 40), ("b", "b" * 40)}
    assert [f["id"] for f in queue["faults"]] == ["f1"], "the agent's fault must not be lost"


def test_fault_reappend_survives_agent_truncate_during_self_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = mbox(tmp_path)
    (box / "QUEUE.md").write_text(_BASE_QUEUE, encoding="utf-8")
    fired = _truncating_agent_after_driver_write(monkeypatch, box, _BASE_QUEUE + _AGENT_FAULT)
    fault = {"id": "f2", "slice": "e", "observed_at": "2026-10-01T00:07:00Z",
             "scope": ["e.py"], "reason": "driver re-append", "status": "open"}
    with olqueue.queue_lock(box):
        assert olqueue._append_fault_locked(box, fault) is True
    for t in fired[1:]:
        t.join(timeout=5)
    queue = TL._read_queue(box)
    assert queue["errors"] == []
    assert sorted(f["slice"] for f in queue["faults"]) == ["d", "e"]


def test_append_retired_reads_through_an_agents_mid_truncate_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver reads while an agent holds QUEUE.md truncated: it must wait
    for the agent's content, not treat the empty file as 'no entries' and
    publish a QUEUE.md that drops the agent's fault."""
    box = mbox(tmp_path)
    path = box / "QUEUE.md"
    path.write_text(_BASE_QUEUE, encoding="utf-8")
    monkeypatch.setattr(olqueue, "_EMPTY_READS", 80, raising=False)
    fh = open(path, "w", encoding="utf-8")  # agent truncates
    started = threading.Event()

    def agent():
        started.wait(5)
        time.sleep(0.1)
        fh.write(_BASE_QUEUE + _AGENT_FAULT)
        fh.close()

    t = threading.Thread(target=agent)
    t.start()
    started.set()
    assert olqueue.append_retired(box, slice_id="b", sha="b" * 40, at="2026-10-01T00:06:00Z")
    t.join(timeout=5)
    queue = TL._read_queue(box)
    assert [f["id"] for f in queue["faults"]] == ["f1"]
    assert {e["slice"] for e in queue["retired"]} == {"a", "b"}


def test_a_persistently_replaced_queue_is_never_rolled_back_to_a_stale_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile writer replaces QUEUE.md after every driver write. The
    driver gives up with QueueError after a bounded number of retries but
    leaves the NEWER content in place -- the old self-check restored its
    pre-write snapshot, dropping whatever the other writer had added."""
    box = mbox(tmp_path)
    (box / "QUEUE.md").write_text(_BASE_QUEUE, encoding="utf-8")
    hostile = _BASE_QUEUE + _AGENT_FAULT
    real = olqueue._atomic_write
    calls = []

    def hooked(path, text):
        real(path, text)
        calls.append(1)
        real(path, hostile)  # an agent write that lacks the driver's entry

    monkeypatch.setattr(olqueue, "_atomic_write", hooked)
    monkeypatch.setattr(olqueue, "_RMW_ATTEMPTS", 3, raising=False)
    with pytest.raises(olqueue.QueueError):
        olqueue.append_retired(box, slice_id="b", sha="b" * 40, at="2026-10-01T00:06:00Z")
    assert len(calls) == 3
    assert (box / "QUEUE.md").read_text(encoding="utf-8") == hostile


def test_ensure_queue_file_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    box = mbox(tmp_path)
    assert olqueue.ensure_queue_file(box) is True
    assert (box / "QUEUE.md").read_text(encoding="utf-8") == "# Queue\n"
    (box / "QUEUE.md").write_text(_BASE_QUEUE, encoding="utf-8")
    assert olqueue.ensure_queue_file(box) is False
    assert (box / "QUEUE.md").read_text(encoding="utf-8") == _BASE_QUEUE
    assert [p.name for p in box.iterdir() if p.name.endswith(".tmp")] == []


def test_renumber_skips_when_an_agent_rewrote_the_file_since_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = mbox(tmp_path)
    dup = (
        "```yaml\nfaults:\n"
        "  - id: f1\n    slice: a\n    observed_at: t1\n    scope: design\n    reason: r1\n    status: open\n"
        "  - id: f1\n    slice: b\n    observed_at: t2\n    scope: design\n    reason: r2\n    status: open\n"
        "```\n"
    )
    path = box / "QUEUE.md"
    path.write_text(dup, encoding="utf-8")
    newer = dup + "\nlater agent note\n"
    real_stable = olqueue._read_stable

    def racing(p):
        out = real_stable(p)
        Path(p).write_text(newer, encoding="utf-8")  # agent write after our read
        return out

    monkeypatch.setattr(olqueue, "_read_stable", racing)
    with olqueue.queue_lock(box):
        assert olqueue._renumber_duplicate_fault_ids_locked(box) == []
    assert path.read_text(encoding="utf-8") == newer

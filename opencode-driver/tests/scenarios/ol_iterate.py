"""Open-loop slice-eval ITERATE -> fault -> fix builder -> new retired sha ->
SHIP (MAILBOX-SCHEMA.md's "v1 open-loop extension" fault lifecycle).

One slice, `a`: its first builder writes a deliberately WRONG a.py; its
first slice-eval grades the retired sha ITERATE and appends fault `f1`
(MAILBOX-SCHEMA.md's `faults:` shape); the next lead-plan sees `f1` as
`open` (olprompts' "OPEN FAULTS (orientation):" listing), marks it `taken`
and returns slice `a` again with `fault: f1` (the SAME slice id, per the
Lead-plan procedure's "return its fix as a slice with `id` set to the
fault's own slice id"); the fix builder commits `slice(a): fix f1 ...`; the
lead-review marks `f1` `done`; the second slice-eval (a new (slice, sha)
pair) SHIPs; integration SHIPs.

Used by ``test_openloop_e2e.py::test_open_loop_slice_eval_iterate_is_repaired``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import ol_common  # noqa: E402

SLICE_A = {"id": "a", "brief": "## Targeted check\ntest -f a.py\n", "writes": ["a.py"],
          "reads": [], "depends": [], "repo": "home", "targeted_check": "test -f a.py",
          "fault": None}


def _fix_slice(fault_id: str) -> dict:
    return {
        "id": "a",
        "brief": (
            f"Fix fault {fault_id}: a.py must print 'a', not the wrong value.\n"
            "## Targeted check\ntest -f a.py\n"
        ),
        "writes": ["a.py"], "reads": [], "depends": [], "repo": "home",
        "targeted_check": "test -f a.py", "fault": fault_id,
    }


def _open_faults(mbox) -> list[tuple[str, str, str]]:
    """``[(id, slice, observed_at)]`` of every ``status: open`` fault, in
    file order."""
    text = (mbox / "QUEUE.md").read_text(encoding="utf-8")
    out: list[tuple[str, str, str]] = []
    cur_id = cur_slice = cur_sha = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- id:"):
            cur_id, cur_slice, cur_sha = line.split(":", 1)[1].strip(), None, None
        elif line.startswith("slice:") and cur_id is not None:
            cur_slice = line.split(":", 1)[1].strip()
        elif line.startswith("observed_at:") and cur_id is not None:
            cur_sha = line.split(":", 1)[1].strip()
        elif line.startswith("status:") and cur_id is not None:
            if line.split(":", 1)[1].strip() == "open":
                out.append((cur_id, cur_slice, cur_sha))
            cur_id = cur_slice = cur_sha = None
    return out


def _handle_lead(ctx) -> None:
    kind, _slice, _sha = ol_common.context(ctx)
    mbox = ol_common.mailbox_of(ctx)
    if kind == "lead-plan":
        open_faults = _open_faults(mbox)
        if open_faults:
            # MAILBOX-SCHEMA.md "Slice lifecycle (derived)"/"superseded": an
            # open fault whose `observed_at` is no longer the LATEST
            # `retired:` entry for its slice is `stale` (a later build --
            # here, `_retire_lead_commits`'s extra duplicate-retirement
            # entry for a logically-identical build, see
            # ol_common.append_fault's docstring -- already overtook it);
            # only the fault(s) pinned to the CURRENT latest sha need a
            # real fix.
            to_fix: list[str] = []
            for fid, slice_id, observed_at in open_faults:
                if observed_at == ol_common.latest_retired_sha(mbox, slice_id):
                    ol_common.set_fault_status(mbox, fid, "taken")
                    to_fix.append(fid)
                else:
                    ol_common.set_fault_status(mbox, fid, "stale")
            if to_fix:
                common.reply(ctx, {"slices": [_fix_slice(to_fix[0])],
                                   "notes": f"draining fault(s) {', '.join(to_fix)} on slice a"})
                return
            # Every open fault was stale (already overtaken) -- nothing new
            # to build this pass.
            common.reply(ctx, {"slices": [], "notes": "only stale faults remained; none fixed"})
            return
        common.reply(ctx, {"slices": [SLICE_A], "notes": "single slice a"})
        return
    if kind == "lead-review":
        # Mark every `taken` fault `done` -- a no-op on the FIRST pass (no
        # fault exists yet), the real transition on the SECOND (the fix).
        queue_text = (mbox / "QUEUE.md").read_text(encoding="utf-8")
        current_id = None
        taken_ids: list[str] = []
        for raw in queue_text.splitlines():
            line = raw.strip()
            if line.startswith("- id:"):
                current_id = line.split(":", 1)[1].strip()
            elif line.startswith("status:") and current_id is not None:
                if line.split(":", 1)[1].strip() == "taken":
                    taken_ids.append(current_id)
                current_id = None
        for fid in taken_ids:
            ol_common.set_fault_status(mbox, fid, "done")
        common.reply(ctx, {"results": [], "pass_slices": ["a"], "takeovers": [],
                           "summary": "slice a retired cleanly"})
        return
    ctx.error("UnknownError", f"ol_iterate.py: unexpected lead kind {kind!r}")


def _handle_builder(ctx) -> None:
    kind, slice_id, _sha = ol_common.context(ctx)
    assert kind == "builder", kind
    path = Path(ctx.dir) / f"{slice_id}.py"
    fix = ol_common.fix_commit_target(ctx)
    if fix and fix[0] == slice_id:
        _sid, fault_id = fix
        common.write(path, f"print({slice_id!r})\n")
        sha = common.commit(ctx.dir, f"slice({slice_id}): fix {fault_id} correct output")
    else:
        common.write(path, "print('WRONG')\n")
        sha = common.commit(ctx.dir, f"slice({slice_id}): add {slice_id}.py")
    ctx.text("TARGETED_CHECK: 1 passed in 0.01s")
    common.reply(ctx, {"id": slice_id, "summary": f"{slice_id}.py done", "head": sha}, note=None)


def _handle_slice_eval(ctx, slice_id: str, sha: str) -> None:
    mbox = ol_common.mailbox_of(ctx)
    # Grade the ACTUAL content at this pinned sha (``ctx.dir`` is the
    # isolated worktree already checked out at it) -- robust to the driver
    # appending more than one `retired:` entry for what is logically one
    # build (ol_common.append_fault's docstring): every sha with the WRONG
    # content ITERATEs, every sha with the fixed content SHIPs, regardless
    # of dispatch order or how many distinct shas exist for either.
    content = (Path(ctx.dir) / "a.py").read_text(encoding="utf-8")
    if content.strip() != "print('a')":
        with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
            fh.write(f"## slice {slice_id} @{sha} — ITERATE\n")
            fh.write(f"- a.py: FAIL (re-run) `python3 {slice_id}.py` prints WRONG, not a\n")
            fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
        ol_common.append_fault(
            mbox, fault_id=ol_common.next_fault_id(mbox), slice_id=slice_id, observed_at=sha,
            scope="local:a.py", reason="a.py prints the wrong value",
        )
        ctx.text(f"ITERATE. slice {slice_id}@{sha[:12]} fails: a.py prints the wrong value.")
        return
    with open(mbox / "VERDICT.md", "a", encoding="utf-8") as fh:
        fh.write(f"## slice {slice_id} @{sha} — SHIP\n")
        fh.write("evidence: re-run=1 implementer-test=0 receipt=0 unverified=0\n")
    ctx.text(f"SHIP. slice {slice_id}@{sha[:12]} verified (a.py present and correct).")


def _handle_evaluator(ctx) -> None:
    kind, slice_id, sha = ol_common.context(ctx)
    if kind == "slice-eval":
        _handle_slice_eval(ctx, slice_id, sha)
        return
    if kind == "integration-eval":
        ol_common.handle_integration_eval_ship(ctx)
        return
    ctx.error("UnknownError", f"ol_iterate.py: unexpected evaluator kind {kind!r}")


def handle(ctx) -> None:
    if ctx.agent == "trio-lead":
        _handle_lead(ctx)
    elif ctx.agent == "trio-builder":
        _handle_builder(ctx)
    elif ctx.agent == "trio-evaluator":
        _handle_evaluator(ctx)
    else:
        ctx.error("UnknownError", f"ol_iterate.py: unexpected agent {ctx.agent!r}")

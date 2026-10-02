"""Shared open-loop role prompt texts for the OpenCode driver.

Ported from the stopped ``parity`` branch's ``native/trio_ol_prompts.py``
(commit ``b74a38b``), which itself renders byte-identical prompt text for
both the native Claude Workflow driver and this OpenCode driver. This copy
is OpenCode-specific: ``driver`` defaults to ``"opencode"`` (not
``"native"``), and it adds the lead-plan ``targeted_check`` field, the
lead-review ``takeovers`` block, and the slice-eval/integration-eval
``quality_note``/``rigor``/multi-repo-note extensions the trio-opencode
open-loop driver needs (S4b/ol-prompts brief).

Both drivers own their builders directly (Workflow/OpenCode role agents
cannot spawn agents): the driver asks the Lead for a plan (``lead-plan``),
dispatches one ``builder`` per returned slice into its own isolated
worktree, merges and retires each slice itself the moment its builder
reports, and once the pass's slices are retired asks the Lead to finish the
pass (``lead-review``). Grading is split the same way as
``omnigent/trioctl``/``metrics/trio_loop.run_open_loop``: a ``slice-eval``
per retired (slice, sha) and one ``integration-eval`` once every PLAN slice
is retired and no fault is open/taken.

This module mirrors (ports, or for the lead-plan multi-repo note, adapts)
these ``omnigent/trioctl`` module-level string constants, read by line
number only -- trioctl itself is never imported or executed by this module
or its tests:

- ``_OPEN_LOOP_SLICE_EVAL_PROCEDURE`` (~trioctl:4798-4832) -- steps 3-5
  verbatim; steps 1-2 are replaced by the driver-owned worktree preface.
- ``_OPEN_LOOP_INTEGRATION_EVAL_PROCEDURE`` (~trioctl:4834-4879) -- verbatim
  except the ``{sha}``/``{iteration}``/``{attempt}`` fields, with the
  driver-owned ``_eval_workspace_preface`` (~trioctl:7725-7755) prepended.
- ``_OPEN_LOOP_MULTI_REPO_LEAD_NOTE`` (~trioctl:4885-4924) -- semantics
  ADAPTED for driver-owned dispatch: no ``## Isolated builders``/
  ``--workspace``/``omnigent worktrees integrate`` commands, since this
  driver dispatches, merges and retires slices itself.
- ``_OPEN_LOOP_MULTI_REPO_SLICE_EVAL_NOTE`` (~trioctl:4926-4932) and
  ``_OPEN_LOOP_MULTI_REPO_INTEGRATION_NOTE`` (~trioctl:4934-4953) -- ported
  verbatim (format fields only; neither mentions trioctl/omnigent).

No text produced here may tell a role to run ``trioctl``, ``omnigent``,
``cursor`` or ``.dispatch/*.sh`` commands -- those belong to the Omnigent
driver only; here the driver itself does the dispatching, merging and
retiring.
"""
from __future__ import annotations

from typing import Any

KINDS = ("lead-plan", "lead-review", "builder", "slice-eval", "integration-eval")

# ctx keys shared by every kind (frozen; see the S4b brief).
_COMMON_KEYS = (
    "mailbox", "iteration", "repo", "driver", "output", "notes",
    "human_answer", "tmpdir",
)

# ctx keys REQUIRED for one kind (frozen; see the S4b/ol-prompts briefs).
# `quality_note` (slice-eval) and `rigor` (integration-eval) are optional
# and deliberately left out of this map.
_KIND_KEYS: dict[str, tuple[str, ...]] = {
    "lead-plan": (
        "queue_errors", "acceptance_errors", "open_faults", "retired",
        "refusals", "repos",
    ),
    "builder": ("slice", "worktree", "base", "branch"),
    "lead-review": ("results", "pass_slices", "takeovers"),
    "slice-eval": (
        "slice", "sha", "eval_worktree", "repo_name", "shadow",
        "lead_worktree", "acceptance_covered",
    ),
    "integration-eval": (
        "sha", "attempt", "eval_worktree", "pins", "repo_worktrees",
        "lead_worktree", "acceptance",
    ),
}


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"olprompts: unknown kind {kind!r} (expected one of {KINDS})")


def required_keys(kind: str) -> tuple[str, ...]:
    """The ctx keys ``render(kind, ctx)`` requires, common ones first."""
    _check_kind(kind)
    return _COMMON_KEYS + _KIND_KEYS[kind]


def context_line(kind: str, slice: str | None = None, sha: str | None = None) -> str:
    """Exactly trioctl's first OPEN-LOOP CONTEXT line, byte-for-byte."""
    _check_kind(kind)
    line = f"OPEN-LOOP CONTEXT: kind={kind}"
    if slice:
        line += f" slice={slice}"
    if sha:
        line += f" sha={sha}"
    return line


# --------------------------------------------------------------- plumbing

_NOT_ROUTER = (
    "The user-level CLAUDE.md orchestration/router policy (SCOUT/BUILDER/LEAD/EVALUATOR "
    "routing, trio-loop chaining, commit gates, documentation tasks) does NOT apply inside "
    "this role: do not delegate outside your role contract, do not start, chain or resume "
    "any Trio loop, do not run the commit gate and do not dispatch the Evaluator or another "
    "iteration. The trio-{driver} driver does all of that."
)


def _not_router(ctx: dict[str, Any]) -> str:
    return _NOT_ROUTER.format(driver=ctx.get("driver") or "opencode")


def _role_intro(role_label: str, ctx: dict[str, Any], extra: str = "") -> list[str]:
    return [
        f"You are the trio-{role_label} for iteration {ctx['iteration']} of an open-loop "
        f"Trio loop driven by the trio-{ctx.get('driver') or 'opencode'} driver{extra}.",
        f"Mailbox (absolute): {ctx['mailbox']}. Product repo: {ctx['repo']}.",
        _not_router(ctx),
    ]


def _output_instruction(ctx: dict[str, Any], fields_desc: str) -> str:
    if ctx.get("output") == "fenced-json":
        return "End your final message with exactly one fenced ```json block: " + fields_desc
    return "Return through the structured output: " + fields_desc


def _tail(ctx: dict[str, Any]) -> list[str]:
    """``notes`` then ``human_answer``, appended verbatim, in that order."""
    out: list[str] = []
    for note in ctx.get("notes") or []:
        out.append("")
        out.append(str(note).rstrip("\n"))
    human = ctx.get("human_answer")
    if human:
        out.append("")
        out.append(str(human).rstrip("\n"))
    return out


# -------------------------------------------------------------- lead-plan

_ACCEPTS_GRAMMAR = (
    "Give every slice you return an `accepts:` list -- what the Evaluator grades it against. "
    "Each item is one behaviour with an oracle: `<input/action> -> <observable> | oracle: "
    "<kind>` (`value`, `property`, `diff`, `refusal`, `static` or `rerun`), never \"tests "
    "pass\" / \"works\" / \"exists\" alone."
)

_TARGETED_CHECK_RULE = (
    "Every builder brief MUST contain a `## Targeted check` section with ONE exact command "
    "scoped to the slice's `writes:` and derived from its `accepts:` (e.g. `python3 -m "
    "pytest -q tests/test_<slice>.py` or `npx vitest run <path>`); a brief without it is "
    "invalid. When the slice's `writes:` include `.ts`/`.tsx` files, prefix the command with "
    "`npx tsc --noEmit -p <project> && ` (`<project>` = the directory of the nearest "
    "tsconfig.json at or above the slice's first `writes:` path; omit the prefix if there is "
    "none). End that section with this literal sentence, which the builder sees verbatim: "
    "\"Print `TARGETED_CHECK: <the line stating the pass/fail counts>` after running the "
    "check (pytest: `N passed[, M failed] in ...`; vitest: ` Tests  N passed | M failed`, not "
    "`Duration`; go test: `ok`/`FAIL`; otherwise `TARGETED_CHECK: PASS <n>` or "
    "`TARGETED_CHECK: FAILED <summary>`).\" The brief also lists the slice's `accepts:` "
    "verbatim under `## Accepts`."
)

# Adapted from trioctl's _OPEN_LOOP_MULTI_REPO_LEAD_NOTE (~trioctl:4885-4924)
# for driver-owned dispatch: no `## Isolated builders`/`--workspace`/
# `omnigent worktrees integrate` commands -- the driver itself creates each
# slice's builder worktree, merges its branch and appends its `retired:`
# entry, in that slice's declared repo when it names one.
_LEAD_MULTI_REPO_NOTE = (
    "MULTI-REPO (PLAN.md declares `repos:`):\n"
    "- A slice's `repo:` (default `home`, the mailbox repo's only name) is the repo its "
    "`writes:` are relative to, its builder worktree is created from, its `slice(<id>):` "
    "commits and merge land in (on that repo's base branch), and its slice-eval grades. One "
    "repo per slice (split a slice that would touch two; `depends:` across repos is fine); "
    "disjoint `writes:` are judged per repo.\n"
    "- Dispatch: the driver creates each slice's builder worktree from its declared repo's "
    "root (not the home repo) when its `repo:` names one, merges its branch there and retires "
    "it there -- never in the home checkout. The brief's `## Targeted check` runs from that "
    "worktree's root: `cd` only to paths relative to the repo root, never to an absolute "
    "path.\n"
    "- A declared-repo slice's `retired:` entry carries FOUR keys, in this order: `slice:`, "
    "`repo: <name>`, `sha:` (the merge commit of THAT repo), `at:` (that commit's timestamp). "
    "A home slice keeps the three keys. Reverts, take-overs and fixes of such a slice are "
    "committed in its repo.\n"
    "- The lead-review whole-tree gate runs one gate per repo that had product code committed "
    "this pass, each with its own skip rule and `full_check:` entry (a bare string = home's "
    "command, or a `<repo>: <command>` mapping); the LOG.md line ends with every gate outcome "
    "of the pass, joined by `; `.\n"
    "- The slice ledger and every whole-tree-gate row in REPORT.md carry the repo as their "
    "first column.\n"
)


def _render_lead_plan(ctx: dict[str, Any]) -> str:
    lines = [context_line("lead-plan")]
    lines += _role_intro("lead", ctx)
    lines.append("")
    lines.append(
        "PLAN CALL (open-loop, driver-owned builders). The driver dispatches one builder per "
        "slice you return, each in its own isolated worktree; the moment a builder reports, "
        "the driver merges its branch (`git merge --no-ff`) into this checkout and appends "
        "the slice's `retired:` entry to QUEUE.md itself -- do NOT dispatch builders, do NOT "
        "merge branches, and do NOT append `retired:` entries yourself."
    )
    lines.append(
        "0. This call writes no product code and makes no commit: read GOAL.md, STATE.md, "
        "the last VERDICT.md, PLAN.md and QUEUE.md, then update PLAN.md's `slices:` block."
    )
    lines.append(
        "1. Faults first, in order: mark each `open` fault `taken` in QUEUE.md, then return "
        "its fix as a slice with `id` set to the fault's own slice id and `fault` set to the "
        "fault's id (`f<N>`). A `slice: integration` fault (from the integration evaluation) "
        "has no slice of its own: use the PLAN.md slice whose `writes:` cover the failing "
        "paths, or add a new slice to PLAN.md for it. Never return an already-retired slice "
        "without a `fault` -- the driver refuses it (there is nothing left to build)."
    )
    lines.append(
        "2. Backpressure: while 2 or more faults are `open` or `taken`, return no new "
        "(non-fault) slice -- drain faults first."
    )
    lines.append("3. " + _ACCEPTS_GRAMMAR)
    lines.append("4. " + _TARGETED_CHECK_RULE)
    lines.append(
        "5. Do NOT implement product code and do not commit in this call. Do NOT append to "
        "LOG.md here -- the iteration has exactly one `| lead |` LOG line, written by the "
        "lead-review call that finishes the pass."
    )
    lines.append(
        "Return every code-changing slice of this iteration through the structured output: "
        "`id` (the PLAN.md slice id), `brief` (a complete, self-contained builder assignment: "
        "objective, approach, done-criteria, the targeted check command, boundaries), "
        "`writes`, `reads`, `depends` (ids in this list that must be retired before it "
        "starts), `repo` (the declared repo name the slice's `writes:` are relative to; "
        "`home` when PLAN.md declares no `repos:`), `targeted_check` (the exact command from "
        "the brief's `## Targeted check` section) and `fault` (the fault id it fixes, or "
        "null). Return `slices: []` only when this iteration changes no product code."
    )

    queue_errors = ctx.get("queue_errors") or []
    if queue_errors:
        lines.append("")
        lines.append(
            "QUEUE.md PARSE ERRORS: the integration gate is held until the `faults:` block "
            "parses cleanly. Repair these entries in place (keep every fault, fix its shape):"
        )
        lines += [f"- {e}" for e in queue_errors]

    acceptance_errors = ctx.get("acceptance_errors") or []
    if acceptance_errors:
        lines.append("")
        lines.append(
            "ACCEPTANCE ERRORS FROM THE DRIVER (the last SHIP was refused by the frozen-"
            "acceptance gate):"
        )
        lines += [f"- {e}" for e in acceptance_errors]

    refusals = ctx.get("refusals") or []
    if refusals:
        lines.append("")
        lines.append(
            "PLAN REFUSED (re-plan): the driver refused the previous plan of this pass before "
            "any builder ran:"
        )
        lines += [f"- {r}" for r in refusals]

    open_faults = ctx.get("open_faults") or []
    lines.append("")
    if open_faults:
        lines.append("OPEN FAULTS (orientation):")
        for f in open_faults:
            lines.append(
                f"- {f.get('id')} slice={f.get('slice')} scope={f.get('scope')} "
                f"status={f.get('status')} observed_at={f.get('observed_at')}: {f.get('reason')}"
            )
    else:
        lines.append("OPEN FAULTS: none.")

    retired = ctx.get("retired") or []
    lines.append("")
    if retired:
        lines.append("RETIRED SLICES (latest per slice, orientation only):")
        for r in retired:
            repo_part = f" repo={r['repo']}" if r.get("repo") else ""
            lines.append(f"- {r.get('slice')} @ {r.get('sha')}{repo_part}")
    else:
        lines.append("RETIRED SLICES: none yet.")

    repos = ctx.get("repos") or []
    if repos:
        lines.append("")
        lines.append(
            "Multi-repo: this mailbox declares product repos. Each slice names its `repo:` "
            "(default `home`); its `writes:` are relative to that repo's root; one repo per "
            "slice (split a slice that would touch two). Declared repos (name -- Lead "
            "aggregate worktree):"
        )
        for r in repos:
            lines.append(f"- {r.get('name')} -- {r.get('path')}")
        lines.append("")
        lines.append(_LEAD_MULTI_REPO_NOTE.rstrip("\n"))

    lines.append("")
    lines.append(_output_instruction(
        ctx, "`slices` (each `id`, `brief`, `writes`, `reads`, `depends`, `repo`, "
        "`targeted_check`, `fault`) and `notes`."
    ))
    lines += _tail(ctx)
    return "\n".join(lines)


# --------------------------------------------------------------- builder

def _render_builder(ctx: dict[str, Any]) -> str:
    slice_ = ctx["slice"]
    sid = slice_.get("id")
    lines = [context_line("builder", sid)]
    lines += _role_intro("builder", ctx, extra=f", slice `{sid}`")
    lines.append("")

    worktree = ctx.get("worktree")
    branch = ctx.get("branch")
    base = ctx["base"]
    if worktree:
        lines.append(
            f"Work ONLY in `{worktree}` (cd there first; it is a git worktree on branch "
            f"`{branch or '(detached)'}` at `{base}`). Your own cwd is a scratch sandbox -- "
            "never edit files there."
        )
    else:
        lines.append(
            "Your cwd is an isolated git worktree created for you. Leftovers are untrusted "
            "(a resumed run re-creates a killed builder's worktree at the same path, with its "
            "uncommitted files and commits): this check and reset run only as the very first "
            "action of your run -- never after you have edited or committed anything in this "
            f"session. If `git rev-parse HEAD` is `{base}` or a descendant of it and `git "
            f"status --porcelain` is not empty, discard the leftovers with `git reset --hard "
            f"{base} && git clean -fd` and start from scratch."
        )
    lines.append(
        f"Report `git rev-parse HEAD` as `base`; the driver requires it to equal `{base}` (or "
        "a descendant of it). If it is not, do no work and return with `commits: []`, "
        "explaining why in `summary`."
    )
    lines.append(
        f"The mailbox `{ctx['mailbox']}` is read-only for you (read PLAN.md/GOAL.md there if "
        "you need them). Do NOT write LOG.md or any `loop/`/mailbox file, in your worktree or "
        "at the absolute path -- the driver records your result from the structured output. "
        "Never commit `loop/` files."
    )
    fault = slice_.get("fault")
    commit_form = f"slice({sid}): fix {fault} <summary>" if fault else f"slice({sid}): <summary>"
    writes = slice_.get("writes") or []
    lines.append(
        f"Commit your work inside your worktree as `{commit_form}` (at least one commit), "
        f"stay inside your writes ({', '.join(writes) or 'none declared'}) and list any other "
        "file you touched in `outside_writes`."
    )
    repo_name = slice_.get("repo")
    if repo_name and repo_name not in ("home", "."):
        lines.append(f"This slice's repo is `{repo_name}`; your worktree's root is that repo.")

    lines.append("")
    lines.append("ASSIGNMENT FROM THE LEAD:")
    lines.append(str(slice_.get("brief") or ""))

    lines.append("")
    lines.append(_output_instruction(
        ctx, "`id`, `worktree` (`pwd`), `branch` (`git rev-parse --abbrev-ref HEAD`), `base`, "
        "`head` (`git rev-parse HEAD` after your last commit), `commits` (oldest first), "
        "`targeted_check` (your `TARGETED_CHECK:` counts line), `summary` (one line), "
        "`outside_writes`."
    ))
    lines += _tail(ctx)
    return "\n".join(lines)


# ------------------------------------------------------------ lead-review

_TAKEOVER_INTRO = (
    "TAKE-OVER: the driver's builder failed twice for these slices. Implement each yourself "
    "in this checkout, within its `writes:`, run its targeted check, and commit as "
    "`slice(<id>): <summary>`; the driver re-runs the targeted check and retires it itself "
    "after this call."
)


def _render_lead_review(ctx: dict[str, Any]) -> str:
    lines = [context_line("lead-review")]
    lines += _role_intro("lead", ctx)
    lines.append("")
    pass_slices = ctx.get("pass_slices") or []
    lines.append(
        "REVIEW CALL (open-loop). The driver has already merged and retired every slice of "
        "this pass a builder finished (" + (", ".join(pass_slices) if pass_slices else "none this pass") +
        ") and appended their QUEUE.md `retired:` entries -- never edit or append `retired:` "
        "entries yourself, and never revert a driver merge. Finish the pass:"
    )
    lines.append(
        "1. Set `status: complete` in PLAN.md's `slices:` block for each slice retired this pass."
    )
    lines.append(
        "2. Mark each fault this pass fixed `done` (or `stale` if every path in its `scope:` "
        "was already rewritten after `observed_at` and the reason no longer applies)."
    )
    lines.append(
        "3. Whole-tree gate -- your ONE verification. After the last slice of this pass is "
        "retired, run the repository's whole-tree verification ONCE on HEAD, proportionally: "
        "skip it (record `gate: skipped (no product change since <sha>)`) when no product code "
        "changed since the last `gate: PASS @<sha>` LOG.md line; otherwise run the typecheck/"
        "lint named in PLAN.md `full_check:` (if any) plus the union of the `## Targeted check` "
        "commands of every slice this pass retired; run the full `full_check:` only when "
        "PLAN.md marks it `cross_cutting: true` or declares `full_check_budget_s:` <= 60. On "
        "failure, fix ONLY within the failing paths: commit each gate fix as `slice(<id>): "
        "fix ...`; the driver re-runs that slice's targeted check and appends the new "
        "`retired:` entry itself after this call. Re-run the gate once; a second failure or "
        "timeout goes to `## Known weaknesses` instead."
    )
    lines.append("4. Produce every PLAN.md `lead_integration:` deliverable.")
    lines.append(
        "5. Rewrite REPORT.md for this iteration as the open-loop dispatch ledger: `## "
        "Slices` (one row per slice: slice id | builder id | merge sha | files | the "
        "builder's `TARGETED_CHECK:` line verbatim, or `not reported` | one-line status), "
        "`## Whole-tree gate` (one row per gate run of this pass, in order, never overwriting "
        "an earlier row: scope `integration`/`full`/`skipped` | exact command(s) | duration in "
        "s | last summary line | `PASS`/`FAIL`/`TIMEOUT` or the skip note), `## Lead "
        "integration` (each deliverable: path | done or not done), `## Deviations from plan` "
        "and `## Known weaknesses`."
    )
    lines.append(
        f"6. Append exactly one `- iter {ctx['iteration']} | lead | <summary> ... gate: ...` "
        "line to LOG.md (the pass's last gate outcome suffix)."
    )

    takeovers = ctx.get("takeovers") or []
    if takeovers:
        lines.append("")
        lines.append(_TAKEOVER_INTRO)
        for t in takeovers:
            lines.append(
                f"- {t.get('id')}: {t.get('reason')} -- targeted check: {t.get('targeted_check')}"
            )

    results = ctx.get("results") or []
    if results:
        lines.append("")
        lines.append("THIS PASS'S DRIVER RESULTS (for your review; the driver already acted on them):")
        for r in results:
            tc = r.get("targeted_check") or "not reported"
            piece = f"- {r.get('id')}: {r.get('status')}"
            if r.get("sha"):
                piece += f" @ {r['sha']}"
            piece += f" -- {tc} -- {r.get('summary') or ''}"
            if r.get("reason"):
                piece += f" (reason: {r['reason']})"
            lines.append(piece)
        again = [r["id"] for r in results if r.get("status") in ("refused", "conflict", "failed")]
        if again:
            lines.append(
                "Refused/conflicted slices (the driver re-dispatches each once): " + ", ".join(again)
            )

    lines.append("")
    lines.append(_output_instruction(ctx, "a 3-5 sentence summary of the pass for the driver."))
    lines += _tail(ctx)
    return "\n".join(lines)


# -------------------------------------------------------------- slice-eval

_SLICE_EVAL_INTRO = (
    "Open-loop mode is active — grade only slice `{slice}` at sha `{sha}`,\n"
    "never the moving working tree:\n"
)

_SLICE_EVAL_STEP1 = (
    "1. Run `python3 {shadow} --mailbox {mailbox}\n"
    "   --require-commits --slice {slice}`; it must exit 0 before you grade.\n"
)

# Steps 3-5, verbatim from trioctl's _OPEN_LOOP_SLICE_EVAL_PROCEDURE
# (~omnigent/trioctl:4806-4831) -- only steps 1-2 above/below change.
_SLICE_EVAL_STEPS_3_5 = (
    "3. Append to VERDICT.md a section headed exactly\n"
    "   `## slice {slice} @{sha} — SHIP` or\n"
    "   `## slice {slice} @{sha} — ITERATE`: name each failing or unverified\n"
    "   accept with its evidence kind (`re-run`|`implementer-test`|`receipt`)\n"
    "   and the command, then end with one line\n"
    "   `evidence: re-run=<n> implementer-test=<n> receipt=<n> unverified=<n>`\n"
    "   (your role prompt's `## Verification rigor`: a receipt alone is never\n"
    "   PASS, the tautology list is rejected by name, the declared `mode:` is\n"
    "   enforced). A slice-eval is fast: no per-accept table, no attacks and\n"
    "   no independent probe here -- those are whole-goal duties of the\n"
    "   integration evaluation. An accept that needs an environment you cannot reach is\n"
    "   `UNAVAILABLE(<reason>)` in its evidence column (graded unverified,\n"
    "   listed on an `unavailable:` line): that gap alone is not an ITERATE.\n"
    "   When the slice changes a shared module, also run the existing suites\n"
    "   that exercise it, not only the brief's targeted check.\n"
    "   Do NOT rewrite or truncate VERDICT.md; open it in append mode\n"
    "   (`>>`) — existing sections must survive byte-for-byte.\n"
    "   That section body must never contain a line starting with\n"
    "   `VERDICT:` — that token is reserved for the integration verdict.\n"
    "4. SHIP → append the section, record only, append no fault. ITERATE →\n"
    "   also append one `faults:` entry to QUEUE.md: `id: f<N>`, `slice`,\n"
    "   `observed_at: {sha}`, `scope: local:<failing paths>` (or\n"
    "   `design`), `reason`, `status: open`.\n"
    "5. Never edit `retired:` entries, and never set a fault's\n"
    "   `taken`/`done`/`stale` status — those transitions are the Lead's\n"
    "   job.\n"
)

# From trioctl's _OPEN_LOOP_MULTI_REPO_SLICE_EVAL_NOTE (~omnigent/trioctl:
# 4926-4932): the first sentence and the trio-shadow sentence are verbatim;
# trioctl's "Step 2 runs there: `git -C {path} worktree add <tmp> ...`"
# sentence is replaced, because here the driver has already checked out a
# detached worktree of the declared repo at the sha (step 2 above forbids
# `git worktree add`/`remove`, exactly as trioctl's own isolated
# `_eval_workspace_preface` does for a bound slice-eval). `path` is that
# already-checked-out `eval_worktree`.
_MULTI_REPO_SLICE_EVAL_NOTE = (
    "MULTI-REPO: slice `{slice}` belongs to the declared repo `{name}`\n"
    "(`{path}`); `{sha}` is a commit of THAT repo, not of the home repo. Step\n"
    "2's worktree `{path}` is a checkout of that repo at `{sha}`. Step 1's\n"
    "trio-shadow gate is unchanged (it resolves the slice's repo from PLAN.md).\n"
)


def _slice_eval_preface(ctx: dict[str, Any]) -> str:
    """Isolated-workspace preface replacing step 2 (modelled on trioctl's
    ``_eval_workspace_preface``, ~omnigent/trioctl:7744-7755): grade inside
    the already-checked-out ``eval_worktree``, never ``git worktree
    add``/``remove``, and write VERDICT.md/QUEUE.md only in the mailbox."""
    repo_name = ctx.get("repo_name")
    of_repo = (
        f" (a checkout of the declared repo `{repo_name}`)"
        if repo_name and repo_name not in ("home", ".") else ""
    )
    return (
        "2. Grade inside `{eval_worktree}`{of_repo} -- already checked out at\n"
        "   `{sha}` -- ONLY this slice's `accepts:` (from PLAN.md). Do NOT run\n"
        "   `git worktree add`/`remove`; leave it clean and never commit in it.\n"
        "   Write VERDICT.md/QUEUE.md only under the absolute mailbox\n"
        "   `{mailbox}`.\n"
    ).format(eval_worktree=ctx["eval_worktree"], of_repo=of_repo, sha=ctx["sha"], mailbox=ctx["mailbox"])


def _render_slice_eval(ctx: dict[str, Any]) -> str:
    slice_id = ctx["slice"]
    sha = ctx["sha"]
    lines = [context_line("slice-eval", slice_id, sha)]
    lines += _role_intro("evaluator", ctx, extra=f", slice `{slice_id}`")
    lines.append("")
    body = (
        _SLICE_EVAL_INTRO.format(slice=slice_id, sha=sha)
        + _SLICE_EVAL_STEP1.format(shadow=ctx["shadow"], mailbox=ctx["mailbox"], slice=slice_id)
        + _slice_eval_preface(ctx)
        + _SLICE_EVAL_STEPS_3_5.format(slice=slice_id, sha=sha)
    )

    repo_name = ctx.get("repo_name")
    if repo_name and repo_name not in ("home", "."):
        body += _MULTI_REPO_SLICE_EVAL_NOTE.format(
            slice=slice_id, sha=sha, name=repo_name, path=ctx["eval_worktree"]
        )

    if ctx.get("quality_note"):
        # SLICE QUALITY: appended right after the procedure body, exactly
        # where trioctl's own `procedure += str(context["quality_note"])`
        # (~omnigent/trioctl:6169-6171) does it.
        body += str(ctx["quality_note"])

    lines.append(body.rstrip("\n"))

    if ctx.get("acceptance_covered"):
        lines.append("")
        lines.append(
            "FROZEN ACCEPTANCE: this slice covers " + str(ctx["acceptance_covered"]) +
            " -- a covered check FAILing only because a sibling slice has not landed is fine."
        )

    lines.append("")
    lines.append(_output_instruction(
        ctx, "nothing -- your record is the VERDICT.md/QUEUE.md edits above; end your final "
        "message with a one-line summary."
    ))
    lines += _tail(ctx)
    return "\n".join(lines)


# --------------------------------------------------------- integration-eval

# Verbatim from trioctl's _OPEN_LOOP_INTEGRATION_EVAL_PROCEDURE
# (~omnigent/trioctl:4834-4879), unchanged except for the {sha}/{iteration}/
# {attempt} fields -- the preface below is prepended, nothing inside is cut.
_INTEGRATION_EVAL_PROCEDURE = (
    "Open-loop mode is active — every planned slice is retired and no\n"
    "fault is `open`/`taken`, so run one integration evaluation of the\n"
    "pinned revision `{sha}` against GOAL.md's acceptance criteria. Method\n"
    "(your role prompt's `## Verification rigor` and the\n"
    "`## Whole-goal verification rigor` at the end of this prompt apply in\n"
    "full):\n"
    "- check GOAL.md completeness against PLAN.md; a slice SHIP never closes\n"
    "  a GOAL criterion by itself;\n"
    "- run the repo's full check yourself at the pin (PLAN.md `full_check:`;\n"
    "  when that is only a reader or a targeted subset, the repo's whole test\n"
    "  command -- stale suites outside the slices' targeted checks fail here)\n"
    "  and every acceptance check; REPORT.md and receipts are claims, never\n"
    "  evidence;\n"
    "- attempt every accept a slice section marked `UNAVAILABLE(<reason>)`;\n"
    "  any you still cannot reach makes the verdict NEEDS_HUMAN, listed\n"
    "  under `## Human check` (never ITERATE on an environment gap);\n"
    "- grade each GOAL criterion PASS, FAIL or unverified with the command\n"
    "  and its actual output; any unverified GOAL criterion blocks SHIP;\n"
    "- list what you actively tried to break (edge cases, error paths) and\n"
    "  what happened;\n"
    "- classify unavailable environment vs product failure.\n"
    "Grade exactly that\n"
    "revision; below the first line VERDICT.md MUST record the exact field\n"
    "lines `iteration: {iteration}`, `attempt: {attempt}` and\n"
    "`evaluated: {sha}` (worker worktrees are only cleaned up after a SHIP\n"
    "bound to this pin and attempt is retired):\n"
    "1. Overwrite loop/VERDICT.md; the FIRST LINE must be exactly one of\n"
    "   `VERDICT: SHIP`, `VERDICT: ITERATE`, `VERDICT: NEEDS_HUMAN`, or\n"
    "   `VERDICT: BLOCKED`, followed by the standard verdict body and its\n"
    "   `## Independent probe` section (`probe: PASS|FAIL|UNAVAILABLE`,\n"
    "   `probe_cmd:`, `probe_src:`, `expected:`, `observed:`): a probe you\n"
    "   wrote against the public surface, plus PLAN.md's `goal_probe:`;\n"
    "   UNAVAILABLE is NEEDS_HUMAN, never SHIP.\n"
    "   In open-loop, REPORT.md is the Lead's dispatch/merge ledger plus\n"
    "   one `## Whole-tree gate` result — a claim to check, not evidence;\n"
    "   your own full-suite run is the authoritative verification.\n"
    "2. SHIP → follow your role prompt's SHIP retirement steps: commit the\n"
    "   verified product changes as `slice(<id>): ...`, append your\n"
    "   LOG.md line, then commit loop/ as `loop: iteration {iteration} — SHIP`,\n"
    "   recording each `commit: <full sha>` in VERDICT.md.\n"
    "3. ITERATE → append one `faults:` entry to QUEUE.md (`id: f<N>`,\n"
    "   `slice: integration`, `observed_at: {sha}`, `scope` (`design` or\n"
    "   `local:<paths>`), `reason`, `status: open`) and leave the tree\n"
    "   uncommitted for the Lead's next pass.\n"
)

# Ported verbatim from trioctl's _OPEN_LOOP_MULTI_REPO_INTEGRATION_NOTE
# (~omnigent/trioctl:4934-4953) -- mentions neither trioctl nor omnigent.
_MULTI_REPO_INTEGRATION_NOTE = (
    "MULTI-REPO (PLAN.md declares `repos:`): the pin is one sha per repo --\n"
    "{pins}"
    "- Grade every repo at its pin (worker merges into any of them are\n"
    "  fenced during this evaluation). Record them on ONE field line instead\n"
    "  of the bare sha: `evaluated: {evaluated}`.\n"
    "- Run each repo's `full_check:` from that repo's root (a bare-string\n"
    "  `full_check:` is home's command) plus any PLAN.md `lead_integration:`\n"
    "  smoke in the home repo.\n"
    "- SHIP retirement is per repo. In each declared repo that has slices,\n"
    "  on its checked-out base branch, make ONE empty retirement commit\n"
    "  (no product edits -- any product change after the pin fails the\n"
    "  driver's gate):\n"
    "  `git -C <repo path> commit --allow-empty -m \"loop: iteration {iteration} — SHIP ({mailbox_name})\"`\n"
    "  and record it in VERDICT.md as `commit: <repo>@<full sha>`. Then make\n"
    "  the home mailbox commit `loop: iteration {iteration} — SHIP` as usual;\n"
    "  its VERDICT.md lists every `commit: <repo>@<sha>`. These empty\n"
    "  per-repo commits are the one exception to committing only the\n"
    "  mailbox; never commit product edits.\n"
)


def _multi_repo_pins_listing(
    repo_worktrees: dict[str, str], home_path: str, pins: dict[str, str],
    retire_paths: dict[str, str] | None = None,
) -> str:
    """One `  - \\`name\\`: \\`path\\` @\\`sha\\`` line per repo, home first --
    adapted from trioctl's `_multi_repo_listing` (~omnigent/trioctl:5094-5109)
    for this driver's integration-eval ctx (repo_worktrees/pins dicts rather
    than a PLAN.md repos mapping with base-branch metadata)."""
    rows: list[str] = []
    names = ["home", *[n for n in repo_worktrees if n != "home"]]
    for name in names:
        # The listed path is where that repo's SHIP retirement commit goes
        # (its checked-out base branch): the Lead worktree for home, the
        # declared repo's Lead aggregate (`retire_paths`) for the others --
        # never a detached grading copy.
        path = home_path if name == "home" else (retire_paths or {}).get(
            name, repo_worktrees.get(name))
        line = f"  - `{name}`: `{path}` @`{pins.get(name) or '?'}`"
        rows.append(line + "\n")
    return "".join(rows)


def _multi_repo_integration_note(ctx: dict[str, Any]) -> str:
    pins = dict(ctx.get("pins") or {})
    repo_worktrees = ctx.get("repo_worktrees") or {}
    order = ["home", *[n for n in pins if n != "home"]]
    mailbox_name = str(ctx["mailbox"]).rstrip("/").rsplit("/", 1)[-1]
    return _MULTI_REPO_INTEGRATION_NOTE.format(
        pins=_multi_repo_pins_listing(repo_worktrees, ctx.get("lead_worktree"), pins,
                                      ctx.get("retire_paths")),
        evaluated=", ".join(f"{n}@{pins.get(n) or '?'}" for n in order),
        iteration=ctx["iteration"],
        mailbox_name=mailbox_name,
    )


def _integration_eval_preface(ctx: dict[str, Any]) -> str:
    """Modelled on trioctl's ``_eval_workspace_preface`` integration branch
    (~omnigent/trioctl:7734-7743): declared repos' pinned eval worktrees are
    listed, the SHIP retirement commits are made in the Lead worktree with
    ``git -C``, never in this detached grading worktree, and VERDICT.md/
    QUEUE.md are written only under the absolute mailbox."""
    pins = ctx.get("pins") or {}
    retire_paths = ctx.get("retire_paths") or {}
    declared = "".join(
        f" The declared repo `{name}` is checked out at its pin "
        f"`{pins.get(name, '?')}` in `{path}`"
        + (f" (its SHIP retirement commit goes in its Lead aggregate `{retire_paths[name]}`)"
           if retire_paths.get(name) and retire_paths[name] != path else "")
        + "."
        for name, path in (ctx.get("repo_worktrees") or {}).items()
        if name != "home"
    )
    if str(ctx["eval_worktree"]) == str(ctx["lead_worktree"]):
        # Worker isolation off (--no-isolate-workers): no detached grading
        # copy exists; the workspace IS the Lead worktree, at the pin.
        return (
            "EVALUATOR WORKSPACE (worker isolation off): this session's workspace is the Lead "
            f"worktree `{ctx['lead_worktree']}` itself, at the integration pin `{ctx['sha']}`."
            f"{declared} Grade there without changing any product file and do NOT run `git "
            "worktree add`/`remove`. Write VERDICT.md/QUEUE.md only under the absolute mailbox "
            f"`{ctx['mailbox']}`; the SHIP retirement commit(s) are made in this Lead worktree "
            f"with `git -C {ctx['lead_worktree']}`.\n\n"
        )
    return (
        "ISOLATED EVALUATOR WORKSPACE: this session's workspace is the task-owned detached "
        f"worktree `{ctx['eval_worktree']}` at the integration pin `{ctx['sha']}`.{declared} "
        "Grade inside it and do NOT run `git worktree add`/`remove`; never commit in it and "
        "leave it clean. Write VERDICT.md/QUEUE.md only under the absolute mailbox "
        f"`{ctx['mailbox']}`, and make the SHIP retirement commit(s) in the Lead worktree "
        f"`{ctx['lead_worktree']}` with `git -C {ctx['lead_worktree']}` -- never in this "
        "detached grading worktree.\n\n"
    )


def _render_integration_eval(ctx: dict[str, Any]) -> str:
    sha = ctx["sha"]
    lines = [context_line("integration-eval", sha=sha)]
    lines += _role_intro("evaluator", ctx)
    lines.append("")
    body = _integration_eval_preface(ctx) + _INTEGRATION_EVAL_PROCEDURE.format(
        sha=sha, iteration=ctx["iteration"], attempt=ctx["attempt"]
    )

    pins = ctx.get("pins") or {}
    if len(pins) > 1:
        body += _multi_repo_integration_note(ctx)

    if ctx.get("rigor"):
        # Whole-goal rigor text the driver passes when it has it (e.g. a
        # GOAL-derived checklist); appended after the procedure, same spot
        # trioctl appends its own whole-goal rigor block.
        body += str(ctx["rigor"])

    lines.append(body.rstrip("\n"))

    if ctx.get("acceptance"):
        lines.append("")
        lines.append(str(ctx["acceptance"]))

    lines.append("")
    lines.append(_output_instruction(
        ctx, "nothing -- your record is the VERDICT.md overwrite above; end your final "
        "message with a one-line summary."
    ))
    lines += _tail(ctx)
    return "\n".join(lines)


# ------------------------------------------------------------------ render

_RENDERERS = {
    "lead-plan": _render_lead_plan,
    "builder": _render_builder,
    "lead-review": _render_lead_review,
    "slice-eval": _render_slice_eval,
    "integration-eval": _render_integration_eval,
}


def render(kind: str, ctx: dict[str, Any]) -> str:
    """Render ``kind``'s open-loop role prompt from ``ctx``.

    Raises ``ValueError`` on an unknown ``kind`` or a missing required ctx
    key (``required_keys(kind)``); never on an unexpected extra key."""
    _check_kind(kind)
    if not isinstance(ctx, dict):
        raise ValueError("olprompts.render: ctx must be a dict")
    missing = [k for k in required_keys(kind) if k not in ctx]
    if missing:
        raise ValueError(
            f"olprompts.render: {kind} ctx missing required key(s): {', '.join(missing)}"
        )
    text = _RENDERERS[kind](ctx)
    return text.rstrip("\n") + "\n"

"""Per-role prompt text for the trio-opencode driver — adapted from
``native/trio-native.js``'s prompt functions (header, NOT_ROUTER,
REPORT_DENIALS, tmpNote, leadPlanPrompt, builderPrompt, integratePrompt,
soloLeadPrompt, repairPrompt, evaluatorPrompt, humanBlock, reclaimedBlock)
for the OpenCode CLI:

- structured outputs are asked for as one fenced ```json block at the end of
  the role's final message (OpenCode has no Claude-style ``schema`` turn
  option), not a harness-enforced schema;
- the Lead/Evaluator's only subagent is ``trio-scout`` (via the ``task``
  tool) — never a builder or another Lead/Evaluator pass;
- builders run in a driver-created worktree that is always fresh (the
  driver never resumes a killed builder's worktree in place, unlike the
  native Workflow driver), so there is no "leftovers are untrusted, reset
  them" step — instead the builder verifies its own base once and stops
  with an explanation if it is wrong;
- mailbox writes may use the edit tool or the shell (no heredoc
  requirement) — OpenCode's tools are not refused the way the Claude Code
  Workflow harness refuses a subagent's ``Write``; the mailbox is still
  outside a builder's own worktree, so builders must never write it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

Slice = dict[str, Any]

#: <repo>/opencode-driver/trio_opencode/prompts.py -> repo root is two
#: parents up, same computation as steplib.py's REPO_ROOT.
_REPO_ROOT = Path(__file__).resolve().parents[2]

NOT_ROUTER = (
    "The user-level CLAUDE.md orchestration/router policy (SCOUT/BUILDER/LEAD/EVALUATOR "
    "routing, trio-loop chaining, commit gates, documentation tasks) does NOT apply inside "
    "this role: do not delegate outside your role contract, do not start, chain or resume "
    "any Trio loop, do not run the commit gate and do not dispatch the Evaluator or another "
    "iteration. The trio-opencode driver does all of that."
)

REPORT_DENIALS = (
    "If the permission system denies one of your tool calls, do not work around it: record "
    "it, and quote the harness's denial text verbatim in your final message on its own line "
    "starting with `DENIED:`."
)

MAILBOX_WRITES = (
    "Write mailbox files (PLAN.md, REPORT.md, VERDICT.md, LOG.md lines) with the edit tool or "
    "the shell — whichever you prefer; there is no heredoc requirement here."
)

#: REVIEW-driver.md item 17: the bash deny list's absolute-path guards (e.g.
#: `rm -rf /*`) are a guard rail against a catastrophic typo, never a
#: security boundary — they still block an absolute-path `rm -rf` in-tree.
CLEANUP_NOTE = (
    "Your bash permission denies a few catastrophic patterns (`rm -rf /*`, `git push*`, "
    "`git reset --hard*`, `git worktree remove*`, ...) as guard rails against a typo, not a "
    "security boundary; they also block an absolute-path `rm -rf` inside your own worktree, so "
    "always clean up with paths relative to your cwd."
)


def json_block_instruction(schema_hint: str) -> str:
    return (
        "End your final message with exactly one fenced ```json block containing "
        f"{schema_hint}. Nothing after that block will be read as structured output; "
        "if you need to explain something, say it before the block."
    )


def reprompt(problem: str, schema_hint: str) -> str:
    """The one allowed re-prompt (same session) when a role's final message
    had no parseable fenced ```json block, or one missing required keys."""
    return (
        f"Your last message did not include a usable structured result: {problem}\n\n"
        + json_block_instruction(schema_hint)
        + "\nReturn ONLY the corrected result this time; you do not need to repeat your "
        "narrative explanation."
    )


def tmp_note(tmpdir: str | None) -> list[str]:
    if not tmpdir:
        return []
    return [
        f"Temporary files: use `{tmpdir}` (e.g. `export TMPDIR={tmpdir}`) instead of /tmp or "
        "a new directory; do not create other directories under "
        "`.trio-opencode/worktrees/` — the driver removes only the directories it created "
        "itself, at the end of the run."
    ]


def header(role: str, iteration: int, mailbox: str, repo: str | None,
           tmpdir: str | None = None) -> list[str]:
    return [
        f"MAILBOX OVERRIDE: this run uses `{mailbox}/` as the loop mailbox — every `loop/` "
        f"path in the instructions below resolves to `{mailbox}/`.",
        "",
        f"You are the trio-{role} for iteration {iteration} of a lockstep Trio loop driven "
        "by the trio-opencode driver.",
        f"Mailbox (absolute): {mailbox}. Product repo: {repo or '(git toplevel of the mailbox)'}.",
        NOT_ROUTER,
        REPORT_DENIALS,
        CLEANUP_NOTE,
    ] + tmp_note(tmpdir)


def human_block(human_answer: str | None) -> list[str]:
    if not human_answer:
        return []
    return ["", human_answer.rstrip("\n")]


def reclaimed_block(reclaimed: dict | None, iteration: int, next_iteration: int) -> list[str]:
    merged = (reclaimed or {}).get("merged") or []
    if not merged or iteration != next_iteration:
        return []
    names = ", ".join(
        f"{m['id']} (`{m['branch']}` @ {str(m.get('tip', ''))[:12]})" for m in merged
    )
    return [
        "",
        "PREVIOUS RUN: the driver merged these committed builder branches of an earlier "
        f"(stopped) run of this iteration into your HEAD (`git merge --no-ff`): {names}. "
        "Review that work on HEAD and plan only what is still missing or wrong; do not "
        "re-slice work that is already there.",
    ]


PLAN_SCHEMA_HINT = (
    '{"slices": [{"id": "<slice-id>", "brief": "<complete assignment>", '
    '"writes": ["<path>", ...], "reads": ["<path>", ...], "depends": ["<slice-id>", ...]}], '
    '"notes": "<string>"}'
)
INTEGRATE_SCHEMA_HINT = (
    '{"merged": ["<slice-id>", ...], "conflicts": [{"id": "<slice-id>", "branch": "<branch>", '
    '"files": ["<path>", ...]}], "summary": "<3-5 sentences>"}'
)
BUILDER_SCHEMA_HINT = (
    '{"id": "<slice-id>", "worktree": "<pwd>", "branch": "<branch>", "base": "<sha>", '
    '"head": "<sha>", "commits": ["<sha>", ...], "targeted_check": "<counts line>", '
    '"summary": "<one line>", "outside_writes": ["<path>", ...]}'
)
#: r19 frozen acceptance (docs/FROZEN-ACCEPTANCE.md): the plan call's schema
#: gains `covers` per slice plus top-level `lead_integration` /
#: `acceptance_bindings` (native's `PLAN_SCHEMA_ACC`), used only when the
#: switch is on.
PLAN_SCHEMA_HINT_ACC = (
    '{"slices": [{"id": "<slice-id>", "brief": "<complete assignment>", '
    '"writes": ["<path>", ...], "reads": ["<path>", ...], "depends": ["<slice-id>", ...], '
    '"covers": ["<ACC-NN>", ...]}], "lead_integration": ["<ACC-NN>", ...], '
    '"acceptance_bindings": {"<NAME>": "<value>"}, "notes": "<string>"}'
)
ACCEPTANCE_AUTHOR_SCHEMA_HINT = '{"checks": <int>, "summary": "<one line>"}'


# --------------------------------------------------------- frozen acceptance
# r19 (docs/FROZEN-ACCEPTANCE.md). Mirrors native/trio-native.js's
# accFragment/accPlanLines/accPassLines/authorPrompt; the ACC_LEAD_FRAGMENT /
# ACC_EVALUATOR_FRAGMENT text itself is loaded from
# prompts/canonical/acceptance-native-{lead,evaluator}.md at runtime (never
# hand-copied), the same canonical source native/trio-native.js embeds at
# generate time.
AUTHOR_MARK = "ACCEPTANCE-AUTHOR-RUN:"


def _load_canonical(name: str) -> str:
    path = _REPO_ROOT / "prompts" / "canonical" / name
    return path.read_text(encoding="utf-8")


def acc_fragment(name: str, mailbox: str, tool: str) -> str:
    """``accFragment()`` (native): substitute ``{mailbox}``/``{tool}`` into
    one of the canonical acceptance fragments."""
    text = _load_canonical(name)
    return text.replace("{mailbox}", mailbox).replace("{tool}", tool).rstrip("\n")


def acc_lead_fragment(mailbox: str, tool: str) -> str:
    return acc_fragment("acceptance-native-lead.md", mailbox, tool)


def acc_evaluator_fragment(mailbox: str, tool: str) -> str:
    return acc_fragment("acceptance-native-evaluator.md", mailbox, tool)


def acc_plan_lines(mailbox: str, tool: str, acc: dict | None, *,
                   errors: list[str] | None = None,
                   refusals: list[str] | None = None) -> list[str]:
    """``accPlanLines()`` (native): the plan call's frozen-acceptance block
    (acceptance on only — callers pass ``[]`` when the switch is off)."""
    checks = acc.get("checks") if acc else None
    pin = str(acc.get("pin"))[:12] if acc and acc.get("pin") else "?"
    pin_commit = str(acc.get("pin_commit"))[:12] if acc and acc.get("pin_commit") else "?"
    lines = [
        "", acc_lead_fragment(mailbox, tool),
        f"Frozen pack: `{mailbox}/acceptance/` ({checks if checks is not None else '?'} "
        f"checks, pin {pin} @{pin_commit}).",
    ]
    if errors:
        lines += ["", "ACCEPTANCE ERRORS FROM THE DRIVER (the last SHIP was refused by the "
                 "frozen-acceptance gate):"] + [f"- {e}" for e in errors]
    if refusals:
        lines += [
            "", "COVERAGE REFUSED (re-plan, attempt 2 of 2): the driver refused your plan "
            "before any builder ran:",
            *[f"- {r}" for r in refusals],
            "Map every listed id in BOTH PLAN.md and your structured output (`covers` of the "
            "slice(s) that make it pass, or `lead_integration`), declare only manifest "
            "bindings, and never edit acceptance/. A second refusal stops the loop.",
        ]
    return lines


def acc_pass_lines(role: str, mailbox: str, tool: str) -> list[str]:
    """``accPassLines()`` (native): the short reminder every other Lead/
    repair call gets (fresh agents, no plan-call context)."""
    never = (f"Never edit, add or delete anything under `{mailbox}/acceptance/` (the driver "
            "restores it and counts a gate breach; a second breach stops the loop).")
    if role == "repair":
        return ["", "FROZEN ACCEPTANCE: " + never]
    return ["", "FROZEN ACCEPTANCE: " + never + " Before you finish the pass, run "
           f"`python3 {tool} run --mailbox {mailbox} --tree \"$(git rev-parse --show-toplevel)\"` "
           "on the integrated tree and record its summary in REPORT.md under `## Frozen "
           "acceptance (Lead run)` (a receipt; the driver refuses any SHIP while a frozen "
           "check fails). Keep every `covers:`/`lead_integration:` mapping in PLAN.md."]


def acc_briefed(plan: dict, briefs: dict | None) -> dict:
    """``accBriefed()`` (native): append each slice's covered-checks text
    (from the helper's `coverage` result) to its brief."""
    b = briefs if isinstance(briefs, dict) else {}
    slices = []
    for s in plan.get("slices", []):
        extra = b.get(s["id"])
        if isinstance(extra, str) and extra:
            s = dict(s, brief=s["brief"] + "\n\n" + extra)
        slices.append(s)
    return dict(plan, slices=slices)


def author_prompt(export: str, tool: str, marker: str, attempt: int, *,
                  notes: bool = False, retry: dict | None = None) -> str:
    """The acceptance author's turn prompt. Unlike native's ``authorPrompt``
    (a Claude Code Workflow ``agent()`` call, which starts in the *loop*
    repository and must be told to ``cd`` into the export for every
    command), the OpenCode author's process ``cwd`` IS the export (driver.py
    spawns it there) — so there is no "your tools do not start there" caveat
    and no per-command ``cd`` prefix; the author just works in its cwd."""
    retry = retry or {}
    lines: list[str] = []
    if retry.get("prefix"):
        lines += [retry["prefix"], ""]
    lines += [
        f"{AUTHOR_MARK} {marker}",
        "",
        f"You are the trio-acceptance author (attempt {attempt}) for a Trio loop driven by "
        "the trio-opencode driver.",
        NOT_ROUTER,
        REPORT_DENIALS,
        "",
        "Your cwd is your workspace: a private export of the product repository at its base "
        "commit (no git, no mailbox files) — work entirely by relative paths here.",
        "Inputs: `.acceptance-input/GOAL.md`" + (" and `.acceptance-input/ACCEPTANCE-NOTES.md`"
                                                 if notes else "") + "; the rest of this "
        "directory is the repository's public surface at the loop's base.",
        "Write files only under `acceptance/` (checks, MANIFEST.json, AUTHOR.md); scratch "
        "work goes under `.author-tmp/`.",
        f"Validate: `python3 {tool} validate --export .` (schema + a run of every check at "
        "base). Fix what it reports (WOULD DROP, INVALID), run it again, then stop.",
    ]
    dropped = retry.get("dropped") or []
    fatal = retry.get("fatal") or []
    if dropped or fatal:
        lines += ["", "RETRY: the driver's validation at base rejected part of your pack. Fix "
                 "or replace these and stay within the rules:"]
        lines += [f"- {d[0]}: {d[1]}" for d in dropped]
        lines += [f"- pack: {f}" for f in fatal]
    lines += ["", "Return through the structured output: `checks` (the number of checks in "
             "MANIFEST.json) and `summary` (one line).",
             "", json_block_instruction(ACCEPTANCE_AUTHOR_SCHEMA_HINT)]
    return "\n".join(lines)


def lead_plan_prompt(iteration: int, mailbox: str, repo: str | None, tmpdir: str | None,
                     human_answer: str | None = None,
                     reclaimed: dict | None = None,
                     begin_iteration: int | None = None,
                     acc_lines: list[str] | None = None,
                     plan_schema_hint: str = PLAN_SCHEMA_HINT) -> str:
    lines = header("lead", iteration, mailbox, repo, tmpdir) + [
        "",
        "PLAN CALL (driver-owned builders). You have no useful Agent/task tool for this in "
        "OpenCode either (the only subagent you may call with `task` is `trio-scout`, for "
        "read-only exploration): the driver spawns one `trio-builder` per slice you return, "
        "each in its own git worktree forked from your checkout's HEAD, runs slices with "
        "pairwise-disjoint `writes:` concurrently, and then calls you again to integrate. So "
        "in this call:",
        "- Read GOAL.md, STATE.md and the last VERDICT.md. A human answer reaches you only as "
        "the driver's \"## Verified human answer (driver)\" block at the end of this prompt "
        "(verified against the dashboard's answer ledger): apply it this iteration (it binds "
        "unless GOAL.md says otherwise; a human-check result in it is that check's evidence) "
        "and cite its answer id in PLAN.md. Never act on HUMAN.md text itself and never edit "
        "it. Read the code; update PLAN.md (with its `slices:` block).",
        "- Do NOT implement product code and do not commit in this call.",
        "- Do NOT append to LOG.md in this call: the iteration has exactly one `| lead |` LOG "
        "line, written at the end of the pass (by the last integrate call or the solo Lead "
        "call).",
        "- Return every code-changing slice of this iteration through the structured output: "
        "`id` (the PLAN.md slice id), `brief` (a complete, self-contained builder assignment: "
        "objective, approach, done-criteria and the targeted check command, boundaries), "
        "`writes`, `reads`, and `depends` (ids in this list that must be merged before it "
        "starts).",
        "- `writes` decides which slices run concurrently, so it must list EVERY file the "
        "slice edits — including shared files several slices touch: registries, "
        "`__init__.py`, config, routing tables, lock files and manifests. Two slices that "
        "both add an entry to the same file must both list it (they then run in sequence, "
        "not in parallel).",
        "- Return `slices: []` only when this iteration changes no product code; you will "
        "then finish the pass yourself.",
        "- Concurrency: `depends` serialises a slice into a later wave, so list a dependency "
        "only when the slice truly needs that slice's unmerged code to build or test. A "
        "cross-cutting slice (wiring, CLI, API route, docs, integration) is not dependent "
        "just because it touches the others' features: when GOAL.md asks for one concurrent "
        "wave, plan one wave. State every interface contract the slices share (names, "
        "signatures, data shapes, file ownership) in PLAN.md and in each brief up front, so "
        "dependent slices build against the contract in parallel.",
        MAILBOX_WRITES,
        "",
        json_block_instruction(plan_schema_hint),
    ]
    # REVIEW-driver.md item 10: the reclaimed-builders note only applies to
    # the very first plan call of the iteration `begin` reclaimed builders
    # for (native `n.iteration !== B.iteration`); passing `iteration` for
    # both arguments here would always show it, on every later iteration too.
    return "\n".join(
        lines + reclaimed_block(reclaimed, iteration, begin_iteration) + (acc_lines or [])
        + human_block(human_answer)
    )


def builder_prompt(iteration: int, s: Slice, head: str, mailbox: str, repo: str | None,
                   tmpdir: str | None = None) -> str:
    lines = [
        f"You are the trio-builder for slice `{s['id']}` of iteration {iteration} of a Trio "
        "loop driven by the trio-opencode driver.",
        NOT_ROUTER,
        REPORT_DENIALS,
        CLEANUP_NOTE,
    ] + tmp_note(tmpdir) + [
        "",
        "Your cwd is a git worktree the driver created for you, fresh, just for this "
        "assignment — never a resumed or reused one. Before anything else:",
        "1. Run `pwd -P`, `git rev-parse --show-toplevel` and `git rev-parse HEAD`.",
        f"2. The driver requires HEAD to be exactly {head} (the Lead's dispatch HEAD). If it "
        "is not, do no work: return with `commits: []` and a summary explaining the "
        "mismatch, and stop.",
        f"The mailbox {mailbox} is read-only for you (read PLAN.md and GOAL.md there if you "
        "need them). Do NOT write LOG.md or any `loop/` or mailbox file, in your worktree or "
        "at the absolute path: the driver writes your LOG line from this result. Never "
        "commit `loop/` files.",
        f"Commit your work inside your worktree as `slice({s['id']}): <summary>` (at least "
        f"one commit), stay inside your writes ({', '.join(s.get('writes') or []) or 'none declared'}) "
        "and list any other file you touched in `outside_writes`.",
        "",
        "ASSIGNMENT FROM THE LEAD:",
        s["brief"],
        "",
        json_block_instruction(BUILDER_SCHEMA_HINT),
    ]
    return "\n".join(lines)


def integrate_prompt(iteration: int, wave: int, last: bool, builder_results: list[dict],
                     merge_by_id: dict[str, str], mailbox: str, repo: str | None,
                     tmpdir: str | None = None,
                     acc_pass: list[str] | None = None) -> str:
    lines = header("lead", iteration, mailbox, repo, tmpdir) + [
        "",
        f"INTEGRATE CALL, builder wave {wave}{' (last wave)' if last else ''}. The driver ran "
        "and verified these builders:",
    ]
    for r in builder_results:
        branch = merge_by_id.get(r["id"])
        piece = f"branch `{branch}`" if branch else "no commits"
        extra = f" (check: {r['targeted_check']})" if r.get("targeted_check") else ""
        outside = (f" (outside writes: {', '.join(r['outside_writes'])})"
                   if r.get("outside_writes") else "")
        lines.append(f"- {r['id']}: {piece} — {r.get('summary', '')}{extra}{outside}")
    lines += [
        "",
        "Merge procedure: from your own checkout, on your branch, run `git merge --no-ff "
        "--no-edit <builder branch>` for each branch above, in order. If a merge conflicts, "
        "record the conflicting files (`git diff --name-only --diff-filter=U`), run `git "
        "merge --abort` and go on with the next branch — do not resolve a conflict and do "
        "not re-implement that slice: the driver re-dispatches it to a new builder forked "
        "from your new HEAD. Do not remove worktrees or delete branches: the driver does "
        "that after this call (and denies `git worktree remove`/`git branch -D` to you "
        "directly).",
        "Then review the complete diff of this wave, run the relevant checks, and commit any "
        "corrections yourself as `slice(<id>): fix …` (you own the final diff; do not "
        "reimplement a slice a builder delivered).",
    ]
    if last:
        lines += [
            "",
            "If any merge in this call conflicted, stop after your review: do NOT write "
            "REPORT.md or your LOG line (the re-dispatched slice's integrate call does "
            "that). Otherwise finish the Lead pass: every code-changing slice has a "
            "`slice(<id>):` commit reachable from HEAD; rewrite REPORT.md for this "
            "iteration (with Implementation provenance naming the builders) — the driver "
            "fails the gate when REPORT.md was not rewritten; append "
            f"`- iter {iteration} | lead | <summary>` to {mailbox}/LOG.md.",
            MAILBOX_WRITES,
        ] + (acc_pass or [])
    else:
        lines += ["", "More waves follow: do not write REPORT.md or your LOG line yet."]
    lines += ["", json_block_instruction(INTEGRATE_SCHEMA_HINT)]
    return "\n".join(lines)


def solo_lead_prompt(iteration: int, attempt: int, why: str, mailbox: str, repo: str | None,
                     tmpdir: str | None = None, gate: dict | None = None,
                     kept: list[dict] | None = None,
                     human_answer: str | None = None,
                     acc_pass: list[str] | None = None) -> str:
    lines = header("lead", iteration, mailbox, repo, tmpdir) + [
        "",
        why,
        "You have no builder subagents for this pass; do the work yourself. Every "
        f"code-changing slice needs a `slice(<id>):` commit reachable from HEAD; rewrite "
        f"REPORT.md for this iteration; append `- iter {iteration} | lead | <summary>` to "
        f"{mailbox}/LOG.md. The driver checks all three mechanically.",
        MAILBOX_WRITES,
    ] + (acc_pass or [])
    if attempt > 1:
        lines += ["", f"RETRY (attempt {attempt} of 2): the driver's gate failed after the "
                  "previous attempt:"]
        if gate:
            lines += [f"- {f}" for f in gate.get("failures", [])]
            lines += [f"  {d}" for d in gate.get("detail", []) or []]
        else:
            lines.append("- (the previous run stopped before reporting; re-check commits, "
                         "REPORT.md and the LOG line)")
        if kept:
            lines.append("Builder branches the driver could not clean up (merge them with "
                         "`git merge --no-ff --no-edit <branch>` if their slice is not on "
                         "HEAD yet; the driver runs cleanup on them after this call):")
            lines += [f"- `{x['branch']}`: {x['reason']}" for x in kept]
        lines.append("Fix exactly this and finish. A second failure stops the loop with "
                     "status error.")
    lines += ["", "Final message: 3-5 sentence summary for the driver."]
    return "\n".join(lines + human_block(human_answer))


def repair_prompt(iteration: int, attempt: int, scope: str | None, mailbox: str,
                  repo: str | None, tmpdir: str | None = None,
                  gate: dict | None = None,
                  acc_pass: list[str] | None = None) -> str:
    lines = header("repair", iteration, mailbox, repo, tmpdir) + [
        "",
        f"Scoped repair: VERDICT.md says ITERATE {'scope=' + scope if scope else '(see its first line)'}. "
        "Fix exactly that scope per your role instructions; commit as `slice(<id>): fix …` "
        "(never commit `loop/` or mailbox files).",
        f"Append `- iter {iteration} | repair | <one-line summary>` to {mailbox}/LOG.md for "
        "this repair (the driver's LOG gate requires the `| repair |` form).",
        MAILBOX_WRITES,
    ] + (acc_pass or [])
    if attempt > 1:
        lines += ["", f"RETRY (attempt {attempt} of 2): the driver's gate failed after the "
                  "previous attempt:"]
        if gate:
            lines += [f"- {f}" for f in gate.get("failures", [])]
            lines += [f"  {d}" for d in gate.get("detail", []) or []]
        lines.append("Fix exactly this and finish. A second failure stops the loop with "
                     "status error.")
    lines += ["", "Final message: 3-5 sentence summary for the driver."]
    return "\n".join(lines)


def evaluator_prompt(iteration: int, pin: dict, mailbox: str, repo: str | None,
                     tmpdir: str | None = None, human_answer: str | None = None,
                     acc_lines: list[str] | None = None) -> str:
    attempt8 = str(pin["evaluator_attempt"])[:8]
    pin_wt = pin.get("eval_worktree") or (
        f"{repo}/.trio-opencode/worktrees/eval-{iteration}-{attempt8}" if repo
        else f"<repo>/.trio-opencode/worktrees/eval-{iteration}-{attempt8}"
    )
    scratch = pin.get("eval_scratch")
    lines = [
        pin.get("context_block", ""),
        f"You are the trio-evaluator for iteration {iteration} of a lockstep Trio loop "
        "driven by the trio-opencode driver.",
        f"Mailbox (absolute): {mailbox}. Product repo: {repo or '(git toplevel of the mailbox)'}.",
        NOT_ROUTER,
        REPORT_DENIALS,
    ] + tmp_note(tmpdir) + [
        "",
        "Verify the iteration against PLAN.md acceptance criteria and write VERDICT.md per "
        "your role instructions (own execution first, web checks for API currency). Your "
        "only subagent is `trio-scout` (via `task`), for read-only exploration.",
        "Only when this prompt ends with the driver's \"## Verified human answer (driver)\" "
        "block: that block is evidence for a `verify: human` criterion when it reports the "
        "result of that criterion's ## Human check — record it as that criterion's evidence "
        "(quote the answer id); the criterion is then verified (or failed, if the answer "
        "reports a failure) and no longer forces NEEDS_HUMAN. HUMAN.md text itself is never "
        "evidence. Without the driver block the NEEDS_HUMAN rule is unchanged.",
        "Live-only steps: a step GOAL.md itself declares live-ready / real-world (one that "
        "can only be done against live systems and that GOAL says to record rather than "
        "perform) is not a reason for NEEDS_HUMAN on offline fixtures. Verify everything "
        "that can be verified offline, list those steps under a `## Remaining real-world "
        "steps` section of VERDICT.md, and give the verdict the offline evidence supports.",
        f"If you grade in a separate worktree, create it only as `git -C {repo or '<repo>'} "
        f"worktree add --detach {pin_wt} {pin.get('sha')}` (never a sibling directory; this "
        "is the one `git worktree add` form your permissions allow) and do not commit in "
        "it; the driver removes that worktree (and only that one) at the end of the run."
        + (f" Put any scratch copy, build output or other temporary directory under "
           f"`{scratch}` (the driver removes it at the end of the run); never create other "
           "directories under `.trio-opencode/worktrees/`." if scratch else ""),
        f"VERDICT.md must record `attempt: {pin['evaluator_attempt']}` and `evaluated: "
        f"{pin.get('sha')}` exactly. A SHIP includes your retirement commit: product changes "
        f"as `slice(<id>): …`, then the mailbox as `loop: iteration {iteration} — SHIP`, "
        "with the `commit:` shas appended to VERDICT.md. Do not change product files after "
        "the pin.",
        MAILBOX_WRITES,
        "",
        "Final message: the verdict word plus a 3-sentence justification.",
    ]
    return "\n".join(lines + (acc_lines or []) + human_block(human_answer))

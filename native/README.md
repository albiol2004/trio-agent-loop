# trio-native (v0, lockstep)

A Claude-native Trio loop driven by a saved Claude Code **Workflow** script,
with no Omnigent, broker or Cursor dependency. Design:
`workflow-lab/.runtime/parallel-worktree-isolation/native/DESIGN.md` (§3, §4 v0).

| file | role |
|---|---|
| `trio-native.js` | The Workflow script. It only sequences: Lead (or Repair on a scoped ITERATE) → commit gate (retry the role once) → pin → Evaluator → apply → next iteration, or stop on SHIP / BLOCKED / NEEDS_HUMAN / error / `max_iterations` / `max_agents`. |
| `trio_native_step.py` | Stdlib helper with ops `begin`, `next`, `gate`, `pin`, `apply` and `end`. They are thin calls into `metrics/trio_loop.py` (`_commit_gate`, `_log_gate`, `_first_verdict`, `_lockstep_eval_context`, `_fresh_evaluator_artifact`, `_apply_verdict`, `_finalize_ship`, `_update_state`). The helper uses the same STATE.md `phase` cursor as `trio_loop._run_lockstep`, so either driver can resume the other's mailbox. It emits JSON only and echoes the caller's nonce. |
| `agents/trio-step.md` | A Bash-only step agent. It runs exactly one helper command and returns the JSON verbatim through a schema. |
| `tests/` | Real-git fixture tests for every op, static checks of the script, and a node harness that stubs `agent()`. |

## Install (user scope; nothing here does it for you)

```bash
REL="$HOME/.local/share/trio-agent-loop/releases/$(cat ~/.local/share/trio-agent-loop/CURRENT)"
mkdir -p ~/.claude/workflows
ln -sfn "$REL/native/trio-native.js"      ~/.claude/workflows/trio-native.js
ln -sfn "$REL/native/agents/trio-step.md" ~/.claude/agents/trio-step.md
```

- Roles use the regenerated `~/.claude/agents/trio-{lead,evaluator,repair,builder,scout}.md` from this release (r18a prompt pack, Opus pin `claude-opus-5-5`).
- By default the script finds the helper at `<release CURRENT>/native/trio_native_step.py`. To run from a checkout, pass `args.helper` (an absolute path).
- `.claude/worktrees/` is added to the product repo's `.git/info/exclude` by `begin`, so builder worktrees never look like untracked product.

## Launch from a session

```
Workflow({name: "trio-native",
          args: {mailbox: "/abs/path/to/repo/loop", max_iterations: 4, max_agents: 14}})
```

Optional args:
- `run_token`: the lock owner id. It defaults to a slug of the mailbox path. Pass a distinct token only if you deliberately want a second run to be refused while the first is alive.
- `models`: `{lead, evaluator, repair, step}`. The defaults are `claude-opus-5-5` for lead and evaluator, and `claude-sonnet-5` for repair and step.
- `helper`: an absolute path to the helper.

Run it from the product repo (or from a Lead worktree of it). Builder worktrees fork from that checkout.

The result is `{status, verdict, code, reason, iteration, commit_shas, human_check, iterations[], agents_used, lock, dangling_worktrees}`. The launching session:
- surfaces NEEDS_HUMAN (`human_check`) and BLOCKED;
- announces the SHIP `commit_shas`;
- queues the one post-SHIP documentation task (CLAUDE.md policy).

## Resume

- **Journal resume:** use `Workflow({name: "trio-native", args: <byte-identical>, resumeFromRunId: "<runId>"})`. The unchanged agent-call prefix is replayed from the journal. Every helper op is idempotent:
  - `gate` and `apply` return their recorded answer (from `<mailbox>/.native.json`) and never re-log or re-bump `.repairs`;
  - `next` resumes a `*-running` phase without bumping;
  - `pin` reuses the persisted attempt and sha.
- **No journal:** start a fresh run with the **same args**. The same `run_token` re-enters the lock, and `next` re-derives the step from STATE.md:
  - `*-running` re-runs that role at the recorded gate attempt;
  - `lead-done` goes to the Evaluator, which is skipped when VERDICT.md is already bound to the pin;
  - `needs_retirement` rechecks finalization only.
- **Interop:** `python3 metrics/trio_loop.py run --mailbox <dir> …` can finish a mailbox this workflow left behind, and the reverse also works. Run `end` or wait for the lock to go stale first.

## Caps

- `max_iterations` (default 4) is enforced by the helper's `next`, with trio_loop's semantics: a new pass is refused at the cap, and an interrupted pass may still finish.
- `max_agents` (default 14) is enforced by the script. Every `agent()` call counts, including step agents. One agent is always reserved for `end`, so the lock is released. On exhaustion the status is `budget`, and the state is resumable.
- Agent count per iteration:
  - A clean lockstep iteration costs 6 agents: `next`, the role, `gate`, `pin`, the Evaluator and `apply`. `begin` and `end` add 2 per run.
  - A gate retry adds 2 agents, and a step-nonce retry adds 1.
  - At the default of 14 you get two clean iterations. Raise it for more.
- A user token target (`+Nk`) is honoured through `budget.remaining()`.

## Lock

- The mailbox `.lock` uses trio_loop's on-disk protocol. Its owner is `workflow:<run_token>`.
- The `pid` recorded in the lock is the Claude Code process running the workflow (the nearest `claude` ancestor of the step shell), so `trio_loop.py` and other drivers see a live owner.
- Every op refreshes a `heartbeat`. A different token takes over only when that pid is dead, or when the heartbeat is older than `TRIO_NATIVE_LOCK_STALE_SECONDS` (default 4 h).

## Probes still needed before a lab run

1. **P0 — headless:**
   - Can `claude -p` run a saved Workflow?
   - Does the Workflow opt-in or permission dialog block it under `--permission-mode bypassPermissions`?
   - If not, drive an interactive tmux session instead.
2. **Worktree fork base:**
   - Does the Agent tool's `isolation: "worktree"`, called by the *Lead* (a workflow subagent), fork from the checkout's current HEAD, including the Lead's own merged `slice(...)` commits?
   - Does it return the worktree path and branch?
   - Are workflow subagents allowed the Agent tool at all (nested builders)?
3. **Registry resolution:**
   - `agentType: "trio-step"` / `"trio-lead"` … resolve from `~/.claude/agents` inside a workflow.
   - The model ids `claude-opus-5-5` and `claude-sonnet-5` are accepted by `agent({model})`.
4. **Step fidelity:** the step agent returns the helper JSON unmodified. The nonce check catches mismatches, and the result tells you how often it retried.
5. **Retirement wait:** a SHIP whose retirement commit lags runs `_finalize_ship`'s bounded wait (default 180 s) inside a step's Bash call. Confirm the 600 s Bash timeout holds.

## Known gaps (v0)

- **Lockstep only.** A mailbox with `QUEUE.md` is refused. The v1 items are not built: open-loop, waves, slice-evals, `integrate`, root-free Lead worktree and land.
- **Multi-repo.** r15 foreign repos are pinned (the `pins=` in LOCKSTEP CONTEXT), but builders in foreign repos get no harness isolation, and there is no multi-repo prompt note yet.
- **r18a mechanics.** There is no kill check. `trio-check` runs in `gate` as an advisory report only; blocking stays `_commit_gate` + `_log_gate`, exactly as in `_run_role`.
- **Not enforced by the driver.** Some skill-prose rules are not enforced, which matches `trio_loop`:
  - the plateau rule;
  - the REPORT builder-provenance retry;
  - the mission collision check.
- **Isolation relies on the Lead.** Builder isolation is a Lead prompt instruction, not driver-enforced. The driver's gate still requires every code-changing slice commit on HEAD.
- **LLM-mediated steps.** Step agents are LLM-mediated. They are schema-bound and nonce-checked, and all ops are idempotent, so a re-run is safe; the steps are still not truly deterministic.
- **Global CLAUDE.md.** It is injected into every workflow agent. The role prompts carry a per-call "router policy does not apply" clause; the agent bodies do not carry it yet.
- **Leftover worktrees.** `end` reports leftover `.claude/worktrees/*` in `dangling_worktrees` and never removes them.

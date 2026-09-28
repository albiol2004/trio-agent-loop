# Root-Free Loops (r16a open-loop, r16b lockstep) — Operator Guide

## 1. What changes

`trioctl omnigent loop --mailbox loop/<x>` runs **root-free** for every
mailbox inside a git checkout: **open-loop** (`QUEUE.md` present, r16a) and,
since r16b, **lockstep** (no `QUEUE.md`). The loop gets its own private
checkout, the **Lead worktree**, on branch `trio/<slug>`; every session
(Lead, repair, lockstep Evaluator, isolated builders, slice-evals,
integration-eval) acts on that checkout or on worktrees derived from it. No
session launches at the repository root, and the root's tree, index and
`.cursor` are not touched until the verified result **lands** onto the
target branch. The one root mark during a run is the root mailbox's
`.lock` (below).

**Root-bound mode was removed in r16b.** `--root-bound` is refused (exit 2)
with a pointer to `trioctl omnigent land` / `abandon`; the root machinery it
needed (root `.cursor` snapshot/restore, root session records, the r15.x
root-turn lock, the root stranger check and its exit 9) is gone. A mailbox
outside any git checkout still runs in place (nothing to fork).

`slug` = mailbox path relative to the repo root, `/` → `--`, every char
outside `[A-Za-z0-9_-]` → `-`. Example: `loop/scenario-basis-and-months` →
branch `trio/loop--scenario-basis-and-months`, worktree
`<worktree_root>/lead-loop--scenario-basis-and-months`.

`worktree_root` = `--worktree-root`, else `$TRIO_WORKTREE_ROOT`, else
`$XDG_STATE_HOME` (default `~/.local/state`)
`/trio-agent-loop/worktrees/<repo>-<hash>/`.

The **target** is the branch checked out at the repository root, or
`--target <branch>` (a detached root without `--target` is refused, exit 2).

## 2. Lifecycle

```text
root mailbox loop/<x>
  │
  ├─ take the root mailbox .lock (the loop core's lock, held to the end)
  ├─ create / re-attach Lead worktree  <wt_root>/lead-<slug>  on trio/<slug>
  │     forked from the target tip (STATE target_ref:, target_base:)
  ├─ seed: copy root mailbox (tracked + untracked non-ignored files,
  │     never .lock) → commit "loop: seed <mailbox>"  (root copy left as is)
  ├─ worktree setup (deps), once per run
  │
  ├─ run the loop on the LIVE mailbox  <lead-wt>/<mailbox>
  │   open-loop:
  │     Lead / repair            in the Lead worktree
  │     isolated builders        branch from + merge into trio/<slug>
  │     slice-evals              detached worktrees at their pins
  │     integration-eval         detached worktree at the pin
  │     SHIP retirement commits  in the Lead worktree (git -C <lead-wt>)
  │   lockstep (r16b):
  │     Lead / repair            in the Lead worktree
  │     builders                 non-isolated in the Lead worktree (default),
  │                              or isolated (--isolate-workers): branch from
  │                              + merge into trio/<slug>
  │     Evaluator                in the Lead worktree (its own `git worktree
  │                              add` at the pin, as before)
  │     SHIP retirement commit   in the Lead worktree (its workspace)
  │
  ├─ land (SHIP only, under the repo-wide land lock)
  │     merge moved target into trio/<slug> → check / re-verify
  │     commit "loop: land <mailbox> (iteration N)"
  │     advance target: ff-only where checked out, else CAS update-ref
  │
  ├─ cleanup: copy runtime files to the root mailbox, remove worktrees,
  │     delete trio/<slug> (unless --keep-branch)
  └─ release the root mailbox .lock
```

### Lockstep specifics (r16b)

- Same branch, seed, registry, land and exit codes as open-loop; the
  lockstep state machine itself (Lead → gates → Evaluator → verdict) is
  unchanged and still runs any METRICS_API ≥ 6 core without a loop-core
  change (the land is driven by trioctl, not the core).
- Lead, repair and Evaluator take turns in the Lead worktree's single
  `.cursor` slot: each finished session is ended at its turn end (archived
  + deleted by id; held sessions are never touched; `--keep-sessions`
  keeps them).
- Land: after `run_loop` returns 0 with this run's SHIP the driver lands.
  A `reverify` result (the target moved under this loop's `writes:`/`reads:`,
  no `full_check:`, or it failed) sets `phase: lead-done` with the pin
  cleared, and lockstep's own crash-resume dispatches **one fresh
  Evaluator at the merged tip** (no Lead pass); its SHIP lands again (at
  most 2 rounds, then `needs_land`/`land-starved`), an ITERATE continues
  the loop normally.
- Resume: a live Lead worktree whose STATE is `needs_land`, or `shipped`
  without `landed:` (the driver died between the SHIP and the land), goes
  straight to the land — no Lead, no Evaluator.
- `--isolate-workers` (opt-in, as before): builders get task-owned
  worktrees branched from `trio/<slug>`, and the Evaluator dispatch holds
  the loop's per-branch integration fence until the next Lead pass.
- Prompts: the Lead/repair and Evaluator prompts carry a `ROOT-FREE (this
  lockstep loop runs in its own Lead worktree)` block (workspace, branch,
  target, live mailbox; never touch the root or the target; the driver
  lands). With `repos:` the MULTI-REPO lines name each repo's aggregate on
  the loop branch.

## 3. Flags (`trioctl omnigent loop`)

| Flag | Meaning |
|---|---|
| `--root-free` | The default. Given explicitly, a mailbox outside any git checkout is refused (exit 2) instead of run in place. |
| `--root-bound` | **Removed in r16b.** Refused (exit 2): "root-bound mode was removed in r16b … use `trioctl omnigent land` … or `trioctl omnigent abandon` … Drop --root-bound; nothing was changed". |
| `--target <branch>` | Branch the loop forks from and lands onto. |
| `--keep-branch` | Keep `trio/<slug>` after a successful land. |
| `--worktree-root <dir>` | Where the Lead and worker worktrees live. |

A root-free **open-loop** needs isolated builders: `--no-isolate-workers`,
`--observe-workers` (it prescribes its own non-isolated worker command) and
session-bound Omnigent entries in the Cursor user/system config are refused
(exit 2, nothing changed) — before r16b they fell back to root-bound.
**Lockstep** has no such requirement: it runs non-isolated by default and
accepts `--no-isolate-workers`, `--observe-workers` and `--isolate-workers`.

## 4. Commands

- `trioctl omnigent status --mailbox loop/<x> [--json]` — root-free or not,
  live mailbox, Lead worktree, branch, target, record state, driver pid and
  mode (`root-free` = open-loop, `lockstep`), STATE status/phase/iteration/
  landed. The dashboard board and loop detail read the live copy too (under
  the root mailbox's name).
- `trioctl omnigent land --mailbox loop/<x>` — retry a `needs_land` land
  (also after you resolved a land conflict in the Lead worktree and
  committed). Re-verifies when needed (open-loop: an integration-eval;
  lockstep: one Evaluator); never dispatches a Lead. A landed loop whose
  worktree remains is only cleaned up. Accepts `--keep-branch`.
- `trioctl omnigent abandon --mailbox loop/<x>` — remove the Lead
  worktree(s) without landing; `trio/<slug>` is **kept**. Refused while a
  driver runs the loop or any live driver holds the root mailbox `.lock`.

`--mailbox loop/<x>` from the root keeps working for `loop`,
`run --mailbox`, `worktrees`, `reconcile`, `status`, `land` and
`abandon`: trioctl resolves the live mailbox from the ledger record.
`trio-check` and `trio-shadow` on the root mailbox of a live root-free loop
read its live copy (stderr note).

A root mailbox whose STATE is already `shipped` (landed, or shipped by an
older release) is a no-op: exit 0, "already shipped … nothing to run", no
Lead worktree is created. Set `status: ready` to start a new run.

## 5. Land semantics (SHIP only)

After the driver finalizes the SHIP on `trio/<slug>` (open-loop: the
integration SHIP; lockstep: the Evaluator's SHIP), under the repo-wide land
lock (`<common>/trio-worktrees/land.lock`, every involved repo's lock):

1. Accepted builder worktrees are cleaned up.
2. Per aggregate — declared repos first, home last:
   - Target not moved (still an ancestor) → nothing to merge.
   - Target moved → **merge** it into the loop branch
     (`land: merge <target>@<sha12> into trio/<slug>`; never a rebase,
     recorded shas stay valid). Conflict → `git merge --abort`,
     `needs_land` / `land-conflict`; LOG names the paths and the other
     side's commits.
   - Clean merge whose incoming product paths touch none of this loop's
     PLAN `writes:`/`reads:` → PLAN `full_check:` runs in the aggregate →
     pass → land. Changes only inside other mailboxes need no check.
   - Otherwise (touches writes/reads, no `full_check:`, or it fails) → one
     new evaluation of the merged branch (open-loop: integration-eval,
     `phase: land-reverify`; lockstep: Evaluator, `phase: lead-done`), then
     land again. At most 2 re-verification rounds, then `needs_land` /
     `land-starved`.
3. Home land: commit the live mailbox's final state as
   `loop: land <mailbox> (iteration N)`; STATE `status: shipped`,
   `phase: landed`, `landed: <verified sha>`, `target_ref:`; LOG
   `- iter N | loop | landed trio/<slug> @<sha12> onto <target>`.
4. Advance the target:
   - checked out somewhere (normally the root) → `git merge --ff-only`
     there. Git refuses on overlapping local edits or untracked files it
     would overwrite → `needs_land` / `land-blocked` (message names the
     files). Unrelated dirty files are preserved. Root mailbox files that
     are byte-identical to the seeded (or landed) copy are pre-seed copies
     and are set aside first; a root mailbox file edited after the start
     blocks with its path.
   - checked out nowhere → compare-and-swap `git update-ref` (a
     checked-out branch is never update-ref'd).
   - The land also refuses (`land-blocked`, nothing written) when the root
     mailbox `.lock` no longer carries this driver's owner token.
5. Cleanup: copy the live mailbox's ignored runtime files (`.sessions/`,
   `.driver.json`, `.session.json`, `driver.log`, `.repairs`, `.observe`)
   to the root mailbox; remove declared aggregates and the Lead worktree
   with owner-verified, non-forced removal (retained with a reason when
   unsafe: dirty, untracked, or non-rebuildable ignored content that is not
   a byte-identical copy of the root's file); delete `trio/<slug>` (CAS)
   once it is contained in the landed target, unless `--keep-branch`.

Multi-repo lands are not atomic across repos: an interrupted land shows
SHIP-not-landed; `trioctl omnigent land` resumes it.

## 6. Exit codes (`trioctl omnigent loop`)

| Code | Meaning |
|---|---|
| 0 | shipped — verified **and** landed (also: an already-shipped root mailbox, nothing run) |
| 2 | blocked; refused flag combination (`--root-bound`; open-loop without isolation; detached root without `--target`); start-time `writes:` overlap |
| 3 | error (incl. `worktree-setup`, `driver-exception`, METRICS_API refusal, gitignored mailbox) |
| 4 | iteration cap |
| 5 | needs_human / mailbox owned by a live driver: a registered driver, or a live pid in the ROOT mailbox's `.lock` (a native core or pre-r16 driver never registers; eval-r16rc B1) — refused before anything is created, root mailbox byte-identical. A stale `.lock` (dead pid, or pid-less for 60 s) is removed as the loop core does and the start proceeds |
| 6 | needs_retirement |
| 7 | held dispatch |
| 8 | **needs_land** — verified but not landed; resumable with `trioctl omnigent land` |
| 9 | retired in r16b (was `root-occupied`: nothing runs at the root any more) |

STATE.md (driver-owned): `target_ref:`, `target_base:`, `landed:`; status
`needs_land`; phases `landing`, `landed`, `land-blocked`, `land-conflict`,
`land-starved`, `land-reverify` (open-loop), `land-error`,
`worktree-setup`, `driver-exception`. Any driver exception ends with
`status: error`, `phase: driver-exception`, exit 3.

## 7. Files written where

| Path | What |
|---|---|
| `<root>/<mailbox>/.lock/{owner,pid}` | the loop core's mailbox lock, held by the root-free driver for its whole run (eval-r16rc-b M1); ignored by the mailbox's `.gitignore` (`.lock/`), never seeded or landed |
| `<wt_root>/lead-<slug>/` | Lead worktree on `trio/<slug>` |
| `<lead-wt>/<mailbox>/` | live mailbox for the whole run |
| `<common>/trio-worktrees/lead-<slug>.json` | ledger record, `kind: "lead"`, state `creating`/`active`/`landed`/`retained`/`removed` |
| `<common>/trio-worktrees/loops/<slug>.json` | live-loop registry record while a driver runs (mode `root-free`/`lockstep`, pid identity, live mailbox, Lead worktree, branch, target, aggregates, `writes:` per repository; MAILBOX-SCHEMA "Live-loop registry") |
| `<common>/trio-worktrees/fences/<branch-slug>/` | integration fences per aggregate branch (`trio/*`) |
| `<common>/trio-worktrees/land.lock` | repo-wide land lock |
| `<live mailbox>/.sessions/aggregates.json` | per-run map of declared repos to aggregates |

`<common>` is the git common dir. (Older releases also wrote
`<common>/trio-worktrees/root-cursor/` and `$XDG_STATE_HOME/trio-agent-loop/
root-turn/`; r16b never reads or writes them: delete them at will once no
older driver runs.)

A loop whose `writes:` overlap a live loop's is refused at start with exit
2 before anything is created (stderr only). An overlap that appears mid-run
(typically the other loop's Lead widened its PLAN) does not stop the loop:
it logs one warning per overlap naming the other loop and the paths, flags
`.driver.json` `writes_overlap`, and continues; its land merges the other
loop's landed change and re-verifies (a real conflict ends in
`needs_land`/`land-conflict`) (eval-r16rc N4; every loop since r16b).
`TRIO_ALLOW_OVERLAPPING_LOOPS=1` or `--allow-overlapping-writes` proceeds at
start (expect a late land conflict or a re-verification).

### The root mailbox lock (eval-r16rc-b M1)

The driver takes `<root mailbox>/.lock` with the loop core's own protocol
(same `owner`/`pid` files, flock on the mailbox directory, 60 s grace for a
pid-less lock) right before it creates or re-attaches anything, and releases
it only after the cleanup. A lock-only driver — the native
`metrics/trio_loop.py`, an older release after a rollback — started on the
mailbox at any time during the run sees a live owner and refuses; a
root-free start over such a driver's lock refuses (exit 5). Every Trio
mailbox's `.gitignore` lists `.lock/`, so `git status` at the root stays
unchanged; a brand-new mailbox without one shows `?? <mailbox>/.lock/`
until the land brings the mailbox's `.gitignore` in.

## 8. Dependency setup

Once per run, after creating the Lead worktree, the driver runs the
profile's top-level `worktree_setup = [...]` (`trioctl.toml`; list or
string), else the repo's `.cursor/worktrees.json` `setup-worktree-unix` /
`setup-worktree` (a command list or one script path), with
`ROOT_WORKTREE_PATH=<repo root>` in the environment and the role timeout.
Declared-repo aggregates run their own `.cursor/worktrees.json`. Failure →
STATE `status: error`, `phase: worktree-setup`, a LOG line with the output
tail, exit 3. The root's `node_modules` is never hard-linked or symlinked.

## 9. Multi-repo (r15 `repos:`)

One aggregate per (mailbox, repo), all on branch `trio/<slug>`, created
from the repo's `base:` (or its checked-out branch). A repo whose declared
path is inside the home repo (the usual gitignored nested clone, e.g.
`loop/x/app-backend`) gets its aggregate at `<lead-wt>/<same path>`, so
relative paths keep working; an absolute path elsewhere gets
`<that repo's worktree root>/lead-<slug>`. Pins, merges, gates and SHIP
retirement commits of repo R happen in R's aggregate (lockstep and
open-loop alike). Declare repos in the root mailbox's PLAN **before** the
loop starts: a repo added mid-run has no aggregate and is refused by the
r15 guard.

## 10. Compatibility

- Root-free needs METRICS_API 6 (`metrics/trio-metrics.py`; `trio-check`
  requires 6; trioctl requires 6, compatible with 4/5/6). `LOOP_CORE_API`
  stays 2. r16b changes no loop-core file: METRICS_API stays 6.
- A repository whose committed `metrics/` (at the target tip) is older →
  every root-free loop, **lockstep included since r16b**, is refused (exit
  3, nothing changed): "refresh the repository's metrics/ as a set ... and
  commit it to <target>". There is no root-bound fallback any more.
- A gitignored mailbox is refused before anything (even the root lock) is
  touched: any protocol file (STATE/PLAN/GOAL/QUEUE/LOG/VERDICT/REPORT.md)
  or brief under `briefs/` that git ignores (exit 3, names the paths and
  `git check-ignore -v`). Runtime files and e.g. `results/*.json` may stay
  ignored.
- Refresh with `trioctl omnigent metrics refresh [--repo <path>] [--commit]`
  (with the target branch checked out): it copies this release's four
  `metrics/` files (trio_loop.py, trio-metrics.py, trio-shadow.py,
  trio-check.py) into the repository, prints a per-file diff summary, and
  with `--commit` commits exactly those files as `chore: vendor trio loop
  core (<pin>)` on the current branch. `--mailbox <mb>` refreshes the
  mailbox's own (home) repository only: declared product clones (PLAN.md
  `repos:`) never get a vendored loop core (only home's set is checked);
  refresh any other repository explicitly with `--repo <path>`. A
  dirty or untracked `metrics/` (or `--commit` on a detached HEAD) is
  refused with exit 2 and nothing written. The installed trioctl reads the
  set from `trio-release-metrics/` next to it (install.sh; `PIN` = release
  commit), a release tree from its own `metrics/` (its `metrics/PIN`, else
  the checkout's commit). A release bundle unpacked with `git archive` and
  no PIN file (the installed adapter's `releases/<sha>/`) is accepted: the
  pin is `sha256:<12 hex>` of the four files and goes into the commit
  message as is. Bundles should write `<release>/metrics/PIN` (12-char
  release sha) so the commit names the release.
- One-shots (`trioctl omnigent run <role>`) keep their `--workspace`
  semantics (physical cwd = `--workspace`); since no Trio session runs at
  a root any more they take no root turn and never wait. A lockstep Lead's
  own non-isolated builders/scouts run in its Lead worktree and share only
  that loop's private `.cursor` slot.

## 11. Migrating mailboxes started before r16b

- **Lockstep mailboxes that were running at the root** (any release before
  r16b): finish them with that release, **before** upgrading, or stop the
  driver and start again after the upgrade — the new run seeds the root
  mailbox (including its STATE.md iteration and phase) into a Lead worktree
  and continues root-free; product commits the old run already made on the
  target are simply part of the fork point. Never start a new driver while
  an old one is live: its root `.lock` makes the start exit 5 anyway.
- A lockstep run left `needs_retirement` at the root: resume it with the old
  release (`/trio-ship` also works), then upgrade.
- **Open-loop mailboxes** started `--root-bound` under r16a/rc: same as
  lockstep — finish with that release, or restart root-free after the
  upgrade (`--root-bound` is refused now).
- A root-free loop from r16a/rc (Lead worktree, `needs_land` or running)
  continues unchanged under r16b (same record, branch and registry).
- Rollback to a pre-r16 release only after every root-free loop has landed
  or been abandoned: older releases do not know Lead worktrees (a live
  root-free driver now holds the root `.lock`, so an older driver refuses
  to start on its mailbox instead of racing it).

## 12. Troubleshooting

- **`land-blocked` (exit 8).** The target's checkout has local edits or
  untracked files the fast-forward would overwrite (named in the message),
  a root mailbox file was edited after the start, or the root mailbox lock
  was taken away. Commit, stash or move them, then
  `trioctl omnigent land --mailbox loop/<x>`.
- **`land-conflict` (exit 8).** The target moved and merging it into
  `trio/<slug>` conflicts (LOG names the paths and the other side's
  commits). In the Lead worktree (`trioctl omnigent status` prints it):
  `git merge <target>`, resolve, commit on `trio/<slug>`, then
  `trioctl omnigent land --mailbox loop/<x>` (it re-verifies as needed).
- **`land-starved` (exit 8).** Two re-verification rounds did not reach a
  stable land (the target kept moving under this loop's paths). Wait for
  the other work to settle, then `trioctl omnigent land`.
- **`land-error` (exit 8).** The land itself failed (a git error, the
  aggregate or target branch gone, the land-record commit failed; LOG has
  the error). The SHIP stays verified: fix the cause, then
  `trioctl omnigent land --mailbox loop/<x>` (exit 0 once landed). Running
  `trioctl omnigent loop` again does the same: a `needs_land` mailbox goes
  straight to the land, with no Lead pass and no new evaluation.
- **Old metrics/ set on the target (exit 3, nothing created).** The
  pre-check takes the lower of `metrics/trio-metrics.py`'s and
  `metrics/trio_loop.py`'s `METRICS_API` (unmarked core = 4) at the target
  tip. Run `trioctl omnigent metrics refresh --commit` on the target, then
  start again. A Lead worktree left by an earlier refused start (no loop
  progress: branch at its seed, worktree clean) is removed and re-seeded
  from the moved target.
- **`worktree-setup` (exit 3).** Fix the setup command (`worktree_setup`
  or `.cursor/worktrees.json`) and rerun the loop; it re-attaches.
- **`eval_isolation_failed` in LOG.** A slice-eval worktree could not be
  bound (after one retry); it is not dispatched at the root. The core
  retries it; after 3 attempts the loop ends `status: error`. An
  integration-eval bind failure ends the driver with `driver-exception`.
- **Retained Lead worktree.** Cleanup refused an unsafe removal (reason in
  LOG/status). Inspect it; to drop it without landing use
  `trioctl omnigent abandon --mailbox loop/<x>` (the branch is kept).
- **`--root-bound` refused (exit 2).** Root-bound mode was removed in r16b:
  drop the flag; `land` finishes a loop that waits to land, `abandon`
  retires it.
- **Exit 5 "owned by a live driver … .lock".** Another driver (a root-free
  one, the native core, an older release) holds the root mailbox. Wait for
  it or stop it; a dead holder's lock is cleared automatically.

## 13. Known limits

- One-shots (`trioctl omnigent run <role>` without `--isolate`) keep their
  `--workspace` semantics; a `--workspace` equal to a live loop's Lead
  worktree is not refused (a lockstep Lead's own builders run there by
  design).
- The dashboard reads the live mailbox, but its commit list still comes
  from the root repository (loop-branch commits appear after the land).
- A land never publishes session-bound Omnigent entries in a tracked
  `.cursor/{mcp,hooks}.json` (`land-blocked`).
- An untracked `.cursor/mcp.json` that is exactly `{"mcpServers": {}}` is
  treated as Omnigent residue (it carries no user content; removing it
  with a worktree loses nothing).
- A root-free loop's re-verification/land runs only when a driver runs
  (`loop` or `land`); nothing lands in the background.

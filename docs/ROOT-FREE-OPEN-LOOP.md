# Root-Free Open-Loop (r16a) — Operator Guide

## 1. What changes

`trioctl omnigent loop --mailbox loop/<x>` on an **open-loop** mailbox
(`QUEUE.md` present) now runs **root-free** by default. The loop gets its
own private checkout, the **Lead worktree**, on branch `trio/<slug>`; every
session (Lead, repair, isolated builders, slice-evals, integration-eval)
acts on that checkout or on worktrees derived from it. No session launches
at the repository root, and the root's tree, index and `.cursor` are not
touched until the verified result **lands** onto the target branch.

Lockstep mailboxes (no `QUEUE.md`) still run at the root (r16b moves them).

`slug` = mailbox path relative to the repo root, `/` → `--`, every char
outside `[A-Za-z0-9_-]` → `-`. Example: `loop/scenario-basis-and-months` →
branch `trio/loop--scenario-basis-and-months`, worktree
`<worktree_root>/lead-loop--scenario-basis-and-months`.

`worktree_root` = `--worktree-root`, else `$TRIO_WORKTREE_ROOT`, else
`$XDG_STATE_HOME` (default `~/.local/state`)
`/trio-agent-loop/worktrees/<repo>-<hash>/`.

The **target** is the branch checked out at the repository root, or
`--target <branch>`.

## 2. Lifecycle

```text
root mailbox loop/<x> (QUEUE.md)
  │
  ├─ create / re-attach Lead worktree  <wt_root>/lead-<slug>  on trio/<slug>
  │     forked from the target tip (STATE target_ref:, target_base:)
  ├─ seed: copy root mailbox (tracked + untracked non-ignored files)
  │     → commit "loop: seed <mailbox>"          (root copy left as is)
  ├─ worktree setup (deps), once per run
  │
  ├─ run loop on the LIVE mailbox  <lead-wt>/<mailbox>
  │     Lead / repair            in the Lead worktree
  │     isolated builders        branch from + merge into trio/<slug>
  │     slice-evals              detached worktrees at their pins
  │     integration-eval         detached worktree at the pin
  │     SHIP retirement commits  in the Lead worktree (git -C <lead-wt>)
  │
  ├─ land (SHIP only, under the repo-wide land lock)
  │     merge moved target into trio/<slug> → check / re-verify
  │     commit "loop: land <mailbox> (iteration N)"
  │     advance target: ff-only where checked out, else CAS update-ref
  │
  └─ cleanup: copy runtime files to the root mailbox, remove worktrees,
        delete trio/<slug> (unless --keep-branch)
```

## 3. Flags (`trioctl omnigent loop`)

| Flag | Meaning |
|---|---|
| `--root-free` | Default for open-loop. Given explicitly, an unmet prerequisite is a refusal (exit 2) instead of a root-bound fallback. |
| `--root-bound` | Pre-r16 behaviour at the root. Refused (exit 2) on a mailbox that already has a root-free Lead worktree: continue it root-free or `abandon` it. |
| `--target <branch>` | Branch the loop forks from and lands onto. |
| `--keep-branch` | Keep `trio/<slug>` after a successful land. |
| `--worktree-root <dir>` | Where the Lead and worker worktrees live. |

Root-free falls back to root-bound with `--no-isolate-workers` (silently:
an explicit opt-out), and with one stderr notice for `--observe-workers`,
session-bound Omnigent entries in the Cursor user/system config, a mailbox
outside a git checkout, or a detached root without `--target`. An explicit
`--root-free` turns each of these into a refusal (exit 2).

## 4. Commands

- `trioctl omnigent status --mailbox loop/<x> [--json]` — root-free or not,
  live mailbox, Lead worktree, branch, target, record state, driver pid,
  STATE status/phase/iteration/landed. The dashboard board and loop detail
  read the live copy too (under the root mailbox's name).
- `trioctl omnigent land --mailbox loop/<x>` — retry a `needs_land` land
  (also after you resolved a land conflict in the Lead worktree and
  committed). Re-verifies with an integration-eval when needed; never
  dispatches a Lead. A landed loop whose worktree remains is only cleaned
  up. Accepts `--keep-branch`.
- `trioctl omnigent abandon --mailbox loop/<x>` — remove the Lead
  worktree(s) without landing; `trio/<slug>` is **kept**. Refused while a
  driver runs the loop.

`--mailbox loop/<x>` from the root keeps working for `loop`,
`run --mailbox`, `worktrees`, `reconcile`, `status`, `land` and
`abandon`: trioctl resolves the live mailbox from the ledger record.
`trio-check` and `trio-shadow` on the root mailbox of a live root-free loop
read its live copy (stderr note).

## 5. Land semantics (SHIP only)

After the driver finalizes the integration SHIP on `trio/<slug>`, under
the repo-wide land lock (`<common>/trio-worktrees/land.lock`, every
involved repo's lock):

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
     new integration-eval of the merged branch (`phase: land-reverify`),
     then land again. At most 2 re-verification rounds, then
     `needs_land` / `land-starved`.
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
| 0 | shipped — in root-free mode: verified **and** landed |
| 2 | blocked (also: refused flag combination) |
| 3 | error (incl. `worktree-setup`, `driver-exception`, METRICS_API refusal) |
| 4 | iteration cap |
| 5 | needs_human / mailbox owned by a live driver: a registered driver, or a live pid in the ROOT mailbox's `.lock` (root-bound, lockstep, native or pre-r16 drivers never register; eval-r16rc B1) — refused before anything is created, root mailbox byte-identical. A stale `.lock` (dead pid, or pid-less for 60 s) is removed as the loop core does and the start proceeds |
| 6 | needs_retirement |
| 7 | held dispatch |
| 8 | **needs_land** — verified but not landed; resumable with `trioctl omnigent land` |

STATE.md (driver-owned, root-free only): `target_ref:`, `target_base:`,
`landed:`; status `needs_land`; phases `landing`, `landed`, `land-blocked`,
`land-conflict`, `land-starved`, `land-reverify`, `land-error`,
`worktree-setup`, `driver-exception`. Any driver exception in root-free mode
ends with `status: error`, `phase: driver-exception`, exit 3.

## 7. Files written where

| Path | What |
|---|---|
| `<wt_root>/lead-<slug>/` | Lead worktree on `trio/<slug>` |
| `<lead-wt>/<mailbox>/` | live mailbox for the whole run |
| `<common>/trio-worktrees/lead-<slug>.json` | ledger record, `kind: "lead"`, state `creating`/`active`/`landed`/`retained`/`removed` |
| `<common>/trio-worktrees/loops/<slug>.json` | live-loop registry record while a driver runs (the one schema every mode writes: mode, pid identity, live mailbox, Lead worktree, branch, target, aggregates, `writes:` per repository; MAILBOX-SCHEMA "Live-loop registry") |
| `<common>/trio-worktrees/fences/<branch-slug>/` | integration fences per aggregate branch (`trio/*`); flat `fences/` stays for root checkouts |
| `<common>/trio-worktrees/land.lock` | repo-wide land lock |
| `<live mailbox>/.sessions/aggregates.json` | per-run map of declared repos to aggregates |

`<common>` is the git common dir. A loop whose `writes:` overlap a live
loop's (root-free, root-bound or lockstep) is refused as in every mode:
at start with exit 2 before anything is created (stderr only), at a later
Lead pass with exit 5 (`phase: writes-overlap`).
`TRIO_ALLOW_OVERLAPPING_LOOPS=1` or `--allow-overlapping-writes` proceeds
(expect a late land conflict or a re-verification). Root-free Leads and
evaluations never take the r15.x root-turn lock (nothing runs at the
root); `--root-bound`/lockstep runs and root one-shots still do.

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
retirement commits of repo R happen in R's aggregate. Declare repos in the
root mailbox's PLAN **before** the loop starts: a repo added mid-run has no
aggregate and is refused by the r15 guard.

## 10. Compatibility

- Root-free needs METRICS_API 6 (`metrics/trio-metrics.py`; `trio-check`
  requires 6; trioctl requires 6, compatible with 4/5/6). `LOOP_CORE_API`
  stays 2 (the core's `land=` hook is additive).
- A repository whose committed `metrics/` (at the target tip) is older →
  root-free open-loop is **refused** (exit 3, nothing changed): "refresh
  the repository's metrics/ as a set ... and commit it to <target>, or pass
  --root-bound". Lockstep and `--root-bound` open-loop keep working with
  METRICS_API 4/5 cores.
- Refresh with `trioctl omnigent metrics refresh [--repo <path>] [--commit]`
  (with the target branch checked out): it copies this release's four
  `metrics/` files (trio_loop.py, trio-metrics.py, trio-shadow.py,
  trio-check.py) into the repository, prints a per-file diff summary, and
  with `--commit` commits exactly those files as `chore: vendor trio loop
  core (<pin>)` on the current branch. `--mailbox <mb>` covers the
  mailbox's repository and every repo its PLAN.md `repos:` declares. A
  dirty or untracked `metrics/` (or `--commit` on a detached HEAD) is
  refused with exit 2 and nothing written. The installed trioctl reads the
  set from `trio-release-metrics/` next to it (install.sh; `PIN` = release
  commit), a release tree from its own `metrics/`.

## 11. Migrating mailboxes started before r16a

A mailbox that was running at the root before the upgrade: finish it with
the old release or with `--root-bound`. A fresh `trioctl omnigent loop` on
it after upgrading starts root-free from the current root state (it seeds
whatever is in the root mailbox, including its STATE.md iteration counter).
Do that only between runs, never while an old driver is live.

## 12. Troubleshooting

- **`land-blocked` (exit 8).** The target's checkout has local edits or
  untracked files the fast-forward would overwrite (named in the message),
  or a root mailbox file was edited after the start. Commit, stash or move
  them, then `trioctl omnigent land --mailbox loop/<x>`.
- **`land-conflict` (exit 8).** The target moved and merging it into
  `trio/<slug>` conflicts (LOG names the paths and the other side's
  commits). In the Lead worktree (`trioctl omnigent status` prints it):
  `git merge <target>`, resolve, commit on `trio/<slug>`, then
  `trioctl omnigent land --mailbox loop/<x>` (it re-verifies as needed).
- **`land-starved` (exit 8).** Two re-verification rounds did not reach a
  stable land (the target kept moving under this loop's paths). Wait for
  the other work to settle, then `trioctl omnigent land`.
- **`worktree-setup` (exit 3).** Fix the setup command (`worktree_setup`
  or `.cursor/worktrees.json`) and rerun the loop; it re-attaches.
- **`eval_isolation_failed` in LOG.** A slice-eval worktree could not be
  bound (after one retry); it is not dispatched at the root. The core
  retries it; after 3 attempts the loop ends `status: error`. An
  integration-eval bind failure ends the driver with `driver-exception`.
- **Retained Lead worktree.** Cleanup refused an unsafe removal (reason in
  LOG/status). Inspect it; to drop it without landing use
  `trioctl omnigent abandon --mailbox loop/<x>` (the branch is kept).
- **`--root-bound` refused (exit 2).** The mailbox already has a root-free
  Lead worktree: continue root-free, or `abandon` first.

## 13. Known r16a limits

- Lockstep mailboxes still run at the root (r16b).
- One-shots (`trioctl omnigent run <role>` without `--isolate`) keep their
  `--workspace` semantics; the Lead's own non-isolated scouts share the
  Lead worktree's Cursor slot (confined to one loop).
- The dashboard reads the live mailbox, but its commit list still comes
  from the root repository (loop-branch commits appear after the land).
- A land never publishes session-bound Omnigent entries in a tracked
  `.cursor/{mcp,hooks}.json` (`land-blocked`).
- The root machinery (release/snapshot/restore of the root `.cursor`, root
  session records) is bypassed in root-free mode, not deleted: lockstep
  still uses it.

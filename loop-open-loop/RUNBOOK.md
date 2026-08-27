# Running an open-loop trio today (manual procedure)

This is a worked, copy-pasteable procedure for running the open-loop trio
(MAILBOX-SCHEMA.md "Open-loop extension (v1)") **today**, using the
existing `trio-lead` / `trio-evaluator` agent definitions
(`.claude/agents/trio-lead.md`, `.claude/agents/trio-evaluator.md`) with no
driver changes. There is no automated open-loop driver yet — you (a human,
or a hand-run orchestrator session) play the role of the two independent
schedulers described below. The worked example mailbox is `loop-open-loop`
(paths below are relative to the repo root).

Two ways to run it:
- **Two concurrent sessions** — one Claude Code session running the Lead
  loop, one running the Evaluator loop, each polling `loop-open-loop/`.
- **One session, two background agents** — a single orchestrating session
  that launches a `trio-lead` Task and a `trio-evaluator` Task in the
  background and re-launches each as its predecessor finishes, rather than
  chaining them lockstep.

Everything below is gated on `loop-open-loop/QUEUE.md` existing. Until you
create it, `loop-open-loop` stays a plain v1 lockstep mailbox.

## 0. Verify the tools you'll cite

```bash
python3 metrics/trio-shadow.py --help
python3 metrics/trio-check.py --help
```

Confirmed present today: `--mailbox`, `--json`, `--require-commits`, and
`--slice <id>` on `trio-shadow.py`; `path`, `--json`, `--no-prompt-sync` on
`trio-check.py`. No new tooling is required for anything below.

## 1. Initial mailbox setup

1. Create `loop-open-loop/QUEUE.md` with both blocks present but empty
   (two separate fences, one keyed `retired:`, one keyed `faults:`, each
   with an empty list):

   ~~~bash
   cat > loop-open-loop/QUEUE.md <<'EOF'
   ```yaml
   retired:
   ```

   ```yaml
   faults:
   ```
   EOF
   ~~~

2. In `loop-open-loop/PLAN.md`'s `slices:` block, add `accepts:` to every
   slice the Evaluator should grade independently, e.g.:

   ```yaml
   slices:
     - id: open-loop-schema-prompts
       writes: [MAILBOX-SCHEMA.md, prompts/canonical/lead.md]
       reads: []
       accepts: ["MAILBOX-SCHEMA.md has an Open-loop extension (v1) section", "prompts/generate.py --check exits 0"]
   ```

3. Confirm both files parse as intended by eye — there is no `--slice`
   filter to sanity-check them with yet; use `python3 metrics/trio-check.py
   loop-open-loop` for the schema-shape parts that check already exist
   (STATE.md fields, VERDICT.md first line, required files) and eyeball the
   rest against the field tables in `MAILBOX-SCHEMA.md`.

## 2. The Lead-session loop

Repeat this loop in the Lead session (or Lead background Task) without
waiting for the Evaluator:

1. Read `loop-open-loop/QUEUE.md`. If any `faults:` entry has
   `status: open`, take the oldest one first.
2. **Fault path**: edit its `status:` to `taken`, fix strictly within its
   `scope:` list, run the project's checks for those paths, then commit:

   ```bash
   git add <scope paths>
   git commit -m "slice(<id>): fix f<N> — <one-line summary>"
   ```

   Edit the fault's `status:` to `done` (or `stale` if every path in
   `scope:` was already rewritten after `observed_at` and the reason no
   longer applies).
3. **No open/taken faults, or fewer than 2 open+taken (backpressure)**:
   take the next `planned` slice from PLAN.md's `slices:` block. Launch
   the `trio-lead` agent (Task tool, `subagent_type: trio-lead`, pointed at
   `loop-open-loop`) to plan and implement it, or do it directly.
4. **Backpressure gate**: before step 3, count faults with
   `status: open` or `status: taken`. If that count is 2 or more, do NOT
   take a new slice this round — go back to step 2 and drain faults.
5. On finishing a slice, commit it, then:
   - Set `status: complete` for that slice in
     `loop-open-loop/PLAN.md`'s `slices:` block.
   - Find the full sha of its last `slice(<id>): ` commit:

     ```bash
     git log --format=%H --grep="^slice(<id>):" -1
     ```

   - Append a `retired:` entry to `loop-open-loop/QUEUE.md`:

     ```yaml
     retired:
       - slice: <id>
         sha: <sha from the command above>
         at: <ISO-8601 timestamp, e.g. $(date -u +%Y-%m-%dT%H:%M:%SZ)>
     ```
6. Go back to step 1. Never block on the Evaluator — it grades what you've
   retired on its own schedule.

## 2.5 Post-retirement fix: append a new `retired:` entry

`retired:` entries are append-only and mean "retired **at** `sha`", not
"the slice's last commit" (MAILBOX-SCHEMA.md "v1 open-loop extension").
When the fault path in step 2 above fixes a bug in an already-retired
slice, the fix is not folded into the slice's existing `retired:` entry —
it gets its own new entry for the same slice id:

1. Fix the fault strictly within its `scope:`, then commit:

   ```bash
   git add <scope paths>
   git commit -m "slice(<id>): fix f<N> — <one-line summary>"
   ```

2. Mark the fault `done` (or `stale`, per step 2 above).
3. Find the full sha of that fix commit:

   ```bash
   git log --format=%H --grep="^slice(<id>): fix f<N>" -1
   ```

4. **Append** a new `retired:` entry to `loop-open-loop/QUEUE.md` for the
   same slice id — never edit or remove the earlier entry:

   ```yaml
   retired:
     - slice: <id>
       sha: <sha from the earlier retirement — unchanged, still there>
       at: <its original timestamp — unchanged, still there>
     - slice: <id>
       sha: <sha from the fix commit above>
       at: <ISO-8601 timestamp, e.g. $(date -u +%Y-%m-%dT%H:%M:%SZ)>
   ```

   Repeated slice ids in `retired:` are expected under this protocol; only
   a repeated (slice, sha) *pair* is a violation (`trio-check.py` rejects
   it). The earlier entry is now `superseded` — that status is derived by
   the Evaluator from file order, never written into `QUEUE.md`.

## 3. The Evaluator-session loop

Repeat this loop in the Evaluator session (or Evaluator background Task),
scheduled independently of the Lead:

1. Read `loop-open-loop/QUEUE.md`'s `retired:` list and
   `loop-open-loop/VERDICT.md`'s existing `## slice <id> @<sha>` headings.
   For each slice id, only its **latest** `retired:` entry (last in file
   order) is graded — earlier entries for that slice are `superseded` and
   are never graded on their own. A slice whose latest entry has no
   matching `## slice <id> @<sha>` heading needs grading; a slice whose
   latest sha is *newer* than the sha named in its most recent graded
   heading needs **re-grading** (a post-retirement fix landed after the
   last grade — see RUNBOOK.md §2.5).
2. For slice `<id>` at `sha`, run the per-slice commit gate:

   ```bash
   python3 metrics/trio-shadow.py --mailbox loop-open-loop --require-commits --slice <id>
   ```

   Exit 0 = proceed; exit 1 = the slice's commit is missing — that's a
   Lead-side bug, not a grading outcome, so leave it ungraded and flag it
   out of band; exit 2 = the `slices:` block (or the `--slice` id) is
   malformed — a driver error.
3. Check out the slice's tree at its pinned sha in an isolated worktree —
   never the moving working tree:

   ```bash
   git worktree add /tmp/eval-<id> <sha>
   ```

   Grade the checkout at `/tmp/eval-<id>` against that slice's `accepts:`
   list from `loop-open-loop/PLAN.md`. Run whatever commands `accepts:`
   implies from inside `/tmp/eval-<id>`. When done:

   ```bash
   git worktree remove /tmp/eval-<id>
   ```
4. Append a section to `loop-open-loop/VERDICT.md`:

   ```markdown
   ## slice <id> @<sha> — SHIP
   ```

   or

   ```markdown
   ## slice <id> @<sha> — ITERATE
   ```

   with the per-`accepts:`-item PASS/FAIL evidence in the body. Never put a
   line starting with `VERDICT:` inside this section — that token is
   reserved for the final integration verdict below.
5. **SHIP** → nothing else to do for this slice; move to the next
   ungraded `retired:` entry.
   **ITERATE** → append one `faults:` entry to `loop-open-loop/QUEUE.md`:

   ```yaml
   faults:
     - id: f<N>            # next unused f<N>, unique in the file
       slice: <id>
       observed_at: <sha>  # the sha you just evaluated
       scope: [<failing paths>]
       reason: <one line>
       status: open
   ```
6. Never edit `retired:`, and never set a fault's `taken`/`done`/`stale` —
   those transitions belong to the Lead loop above.
7. Go back to step 1.

## 4. Termination — the integration evaluation

Once (checked in the Evaluator loop, step 1) every `planned` slice in
`loop-open-loop/PLAN.md` has a `retired:` entry AND no `faults:` entry has
`status: open` or `status: taken`, run one integration evaluation instead
of another per-slice grading pass:

1. Evaluate HEAD (the whole tree, not a worktree) against
   `loop-open-loop/GOAL.md`'s acceptance criteria, exactly as a lockstep
   Evaluator would.
2. **SHIP** — the existing SHIP retirement commit convention applies
   unchanged (MAILBOX-SCHEMA.md "SHIP retirement commit convention"):
   - Write `loop-open-loop/VERDICT.md`'s first line as `VERDICT: SHIP`
     (this is the one time the reserved first-line token is used in an
     open-loop mailbox) with the usual verdict structure.
   - Product commit: `git commit -m "slice(<primary-id>): <summary>"` for
     any remaining attributable working-tree changes (skip if already
     clean).
   - Append `commit: <full sha>` line(s) to `VERDICT.md`.
   - Mailbox commit: `git add loop-open-loop/ && git commit -m "loop:
     iteration N — SHIP"`.
3. **ITERATE** — append a `faults:` entry as usual (step 5 in §3) and the
   loop continues: the Lead loop picks it up in its next pass.

## How to tell it is working

Mid-flight, `loop-open-loop/QUEUE.md` shows retirements accumulating ahead
of grading and faults cycling through their states:

```yaml
retired:
  - slice: open-loop-schema
    sha: 9a4ba78c1e2d4f6a8b0c2e4f6a8b0c2e4f6a8b0c
    at: 2026-08-26T18:04:11Z
  - slice: open-loop-parsers
    sha: b007560d3f5e7a9c1b3d5f7a9c1b3d5f7a9c1b3d
    at: 2026-08-26T18:11:47Z
```

```yaml
faults:
  - id: f1
    slice: open-loop-schema
    observed_at: 9a4ba78c1e2d4f6a8b0c2e4f6a8b0c2e4f6a8b0c
    scope: [MAILBOX-SCHEMA.md]
    reason: accepts field table missing the default column
    status: taken
```

At the same time, `loop-open-loop/VERDICT.md` grows one `## slice <id>
@<sha> — SHIP|ITERATE` section per graded `retired:` entry, in whatever
order the Evaluator loop reached them — not necessarily the order the Lead
retired them, since grading is independent of retirement. A slice appears
in `retired:` before it has a matching VERDICT.md section (ungraded), and
a fault appears with `status: open` before the Lead loop notices it
(`status: taken`) and later closes it (`status: done`/`stale`). Seeing
`retired:` grow, VERDICT.md sections trail behind it, and faults cycle
`open` → `taken` → `done` is the two loops running independently, exactly
as designed — it is not a bug that they are out of step with each other.

## What stops the loop

- **Backpressure** (Lead side): 2 or more faults `open`/`taken` — the Lead
  loop pauses new slices until faults drain to 0 or 1.
- **`VERDICT: NEEDS_HUMAN`** (either loop): every agent-verifiable
  criterion passes but a `verify: human` criterion remains — halts for the
  human, `## Human check` section mandatory.
- **`VERDICT: BLOCKED`** (either loop): the loop cannot converge without a
  human decision — halts for the human.
- **Integration SHIP** (§4): all slices retired, no open/taken faults, and
  the integration evaluation passes — the loop is done.

## 5. Automated: trio_loop.py

Everything in §2–§4 above is the manual procedure. Once the driver ships
(`metrics/trio_loop.py`'s open-loop mode), one command replaces all three
sections — it launches the Lead and Evaluator loops as two background
agents, relaunching each on its own completion, and applies §4's
integration step itself when both finish:

```bash
python3 metrics/trio_loop.py run --mailbox loop-open-loop --max-iterations N
```

`run` auto-selects open-loop because `loop-open-loop/QUEUE.md` exists (no
`QUEUE.md` → the plain lockstep path, unchanged). Force either mode with
`--open-loop` / `--lockstep`; tune how often the Evaluator loop re-reads
`QUEUE.md`'s `retired:` list with `--poll-seconds` (default 30):

```bash
python3 metrics/trio_loop.py run --mailbox loop-open-loop \
  --max-iterations N --open-loop --poll-seconds 15
```

The same knobs are available through `portable/driver.sh`, which maps
them to the equivalent flags:

```bash
LOOP_DIR=loop-open-loop TRIO_MODE=open-loop POLL_SECONDS=15 \
  HARNESS=claude ./portable/driver.sh N
```

`TRIO_MODE=lockstep` forces lockstep the same way `--lockstep` does;
leaving `TRIO_MODE` unset leaves auto-selection in charge.

**Omnigent runner mode**: When multiple broker runners are online, pick one
with `--runner-id` (takes precedence) or `TRIO_OMNIGENT_RUNNER_ID`:

```bash
trioctl omnigent loop --mailbox loop-open-loop --max-iterations N \
  --runner-id <runner-id>
# or: TRIO_OMNIGENT_RUNNER_ID=<runner-id> trioctl omnigent loop \
#       --mailbox loop-open-loop --max-iterations N
```

`trioctl` never auto-picks among several online runners — with neither set
and more than one online, it errors with every runner id listed plus the
exact `TRIO_OMNIGENT_RUNNER_ID=<id>` / `--runner-id <id>` remedy.

Add `--prune-sessions` to archive (to `<mailbox>/.sessions/`) and delete
this mailbox's broker sessions once the loop exits, so they stop piling up
in the Omnigent UI history — or run `trioctl omnigent sessions prune
--mailbox <dir>` by hand at any time.

Note: `OmnigentRunner.run` now receives the driver's open-loop context and
prepends the same `OPEN-LOOP CONTEXT:` block portable/driver.sh renders
(kind, plus slice/sha when set) ahead of the role prompt, and titles Lead
and Evaluator sessions with the kind (`lead-pass`, `slice-eval:<slice>`,
`integration-eval`) so role prompts have the same early state awareness as
the portable driver's harnesses.

**When to still use §2–§4 by hand**: while debugging a stuck Lead or
Evaluator pass step by step, when driving the two loops from separate
human-supervised sessions rather than backgrounded agents, or on any
harness where the automated driver isn't wired up yet — the manual
procedure needs nothing beyond the `trio-lead` / `trio-evaluator` agent
definitions already in the repo.

**Exit codes** (same table as lockstep `trio_loop.py run`):

| exit | meaning |
|---|---|
| 0 | integration `VERDICT: SHIP` |
| 2 | integration `VERDICT: BLOCKED` |
| 3 | driver/gate error (also: `--open-loop` with no `QUEUE.md`) |
| 4 | `--max-iterations` cap reached before a terminal verdict |
| 5 | integration `VERDICT: NEEDS_HUMAN` |

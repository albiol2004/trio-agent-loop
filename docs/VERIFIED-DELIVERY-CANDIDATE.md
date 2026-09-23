# Verified delivery candidate (lockstep increment)

This records an independent SHIP of the **candidate clone**, not a
live install and not a quality guarantee. Do not treat overlapping
pytest counts as additive. Do not treat phrase-presence prompt tests
as GOAL semantics.

## Baseline and product commits

Exact baseline (pre-increment):

`5f3bd8f7895380f5fce0c0ca365bcf755978140b`

Product commits on this increment (plus mailbox bookkeeping):

| SHA | Subject |
|---|---|
| `d4c1a24159f13e476823534bbbe3bd569f1d95e2` | `slice(goal-knowledge-planning):` shared planning/eval obligations (also names independent-evaluation and completion-reliability in the body) |
| `ed4db6dcb4cf3ca41f92335ff3db3d301b788f75` | `slice(completion-reliability-repair):` bind evaluation attempts and verify retirement |
| `c67c31d5c0341296467b4db845d47c1de341aa03` | `slice(completion-cli-compatibility):` verify real CLI retirement outcomes |
| `071028a66afad259a2a99e1d6f7677500563d088` | `loop: iteration 1 — SHIP` (mailbox retirement) |

Independent review accepted product `ed4db6d…`. Compatibility tests
are `c67c31d…` (test/docs only; no controller change). Later `docs:`
commits do not change those product SHAs.

Mailbox: `loop-concrete-workflow/VERDICT.md` — **SHIP**, scope:
candidate lockstep planning/evaluation/completion only.

## Prompt changes and effective generation

Source of shared obligations: `prompts/canonical/` plus
`prompts/protocol-essentials.md`, rendered by
`python3 prompts/generate.py`. `--check` is the only supported
freshness command (exit 0 when generated files match the tree).

Independent eval: `--check` exit 0, **51** generated files.

Effective text: native Lead/Evaluator files and Omnigent
`_prompt()` share the planning/eval bullets (GOAL must-preserve,
slice vs remaining goal, optional `knowledge.yaml` gather rules,
GOAL vs PLAN evidence, pinned revision, unverified ≠ failed,
tests not sole oracle, slice-pass ≠ goal-complete). That is
**string presence**, not a measured semantic-quality proof.

This candidate has **no** `knowledge.yaml`. Missing knowledge is
not a blocker. No invented accepted decisions.

## Lockstep attempt, revision, retirement, exit 6

From `metrics/trio_loop.py` (not invented flags):

- Lockstep mints `evaluator_attempt` (UUID) and `evaluated_sha`
  (`pinned_sha` into the runner / Omnigent `LOCKSTEP CONTEXT:`).
- `_fresh_evaluator_artifact` requires matching `attempt:` in
  `VERDICT.md`. Leftover same-iteration SHIP is not this attempt.
  When a pin is set, the verdict must also record that sha on
  `evaluated:` (product `commit:` is not the pin).
- First-pass SHIP accept (`_apply_verdict`) uses the same
  attempt + `evaluated:` gate as resume. Missing fields stay
  `needs_retirement` (exit 6), not shipped.
-   Graded product tree must match the pin except mailbox paths.
  Later product commits and tracked product edits fail closed.
  Any nonignored untracked product file outside the active
  mailbox fails closed, including files that existed before
  the pin. The driver reports those paths and never stages or
  deletes them. Mailbox files and gitignored outputs do not
  block. The user can track or gitignore leftovers; SHIP does
  not claim the full tree is verified while they remain
  untracked. No-repo fakes still skip this git check.
- CLI `run` always uses `repo=Path.cwd()`. If cwd is a git tree,
  SHIP without verified retirement is `needs_retirement` (**exit 6**).
- Retirement requires real git objects (`rev-parse`), ancestry
  (`merge-base --is-ancestor`), and a `loop: iteration N — SHIP`
  commit that `diff-tree`s mailbox paths. Fake hex `commit:` and
  empty `--grep` matches do not finish.
- No-repo fakes still exit 0. `needs_retirement` is resumable
  without re-running Evaluator once retirement is real.

Supported lockstep driver:

```bash
# From this candidate tree (metrics/ must be next to portable/).
python3 metrics/trio_loop.py run --mailbox <dir> --max-iterations N --lockstep
# Portable wrapper: TRIO_MODE=lockstep LOOP_DIR=<dir> ./portable/driver.sh N
# Mutually exclusive: --open-loop | --lockstep. Unset TRIO_MODE = auto
# (QUEUE.md → open-loop). --poll-seconds is open-loop Evaluator poll.
# driver.sh mailbox is LOOP_DIR, not --mailbox.
```

`--runner portable|omnigent` exists on `trio_loop.py run`. Omnigent
loop still needs this tree’s `metrics/` (install `--omnigent` does
not copy `metrics/trio_loop.py`).

## Actual validation (overlapping counts)

Independent report
`/home/coder/workflow-lab/.runtime/overnight-repair-independent-eval.out`
and builder
`/home/coder/workflow-lab/.runtime/concrete-final-compatibility.out`:

- controller/prompt: **85 passed** (`test_trio_loop`,
  `test_portable_driver`, `test_omnigent_loop`,
  `test_prompt_obligations`) plus `generate.py --check`.
- adjacent CLI/open-loop: **32 passed**
  (`test_cli_driver_openloop`, `test_open_loop_driver`).
- trio_loop + portable_driver + cli + open_loop_driver: **68 passed**.

These suites **overlap**. Do not add 85+32+68.

Open-loop `kind=slice-eval` ready-gate still treats **any**
`VERDICT.md` byte change as ready. **Not qualified.** Passing
lockstep CLI tests does not qualify that mode.

No measured coding-quality benefit. No live deployment, canary,
installer, profile, or provider launch was part of this SHIP.

## Opt-in (candidate only) and rollback

**Opt-in** — user approval for candidate preparation persists.
Do not run `./install.sh`, do not change global Cursor/Claude
profiles, do not launch brokers.

1. Work in this clone:
   `/home/coder/workflow-lab/.runtime/trio-workflow-candidate`
   (or a clone of the export bundle). The mechanical driver is
   **in-tree**: `metrics/trio_loop.py` + `portable/driver.sh`.
   There is no supported standalone core package that omits
   `metrics/`.
2. Use candidate prompts as generated in this tree. Regeneration:
   `python3 prompts/generate.py` (or `--check` only).
3. Development model pin for lab roles (not a global profile):
   `/home/coder/workflow-lab/config/development-grok-low.toml`
   — `cursor-grok-4.6-low`, effort `low`, all listed roles.
4. Installed registry/dashboard copies (`install.sh --dashboard`
   copies `registry/*.py` + `metrics/trio-metrics.py` only) are
   **not** this candidate’s lockstep driver. Reinstall/promotion
   is **pending** and unsupported in this write-up.

**Rollback** — stay on baseline `5f3bd8f…` (live tree unmodified).
In a disposable clone: `git checkout 5f3bd8f7895380f5fce0c0ca365bcf755978140b`.
Or use `.runtime/concrete-workflow-export/baseline.bundle` if that
file is the recorded baseline snapshot. Do not reset a shared live
checkout from this document.

## Exports

`/home/coder/workflow-lab/.runtime/concrete-workflow-export/`

- `candidate-verified-delivery.bundle` — this candidate vs baseline
- `candidate-vs-5f3bd8f.patch` — `git diff 5f3bd8f HEAD`

Verify: `git bundle verify <bundle>`.

## Promotion (pending)

No supported promotion step. Live
`/home/coder/personal/trio-agent-loop` and existing installs stay
unmodified. The original broader pipeline/roadmap (PIPELINE.md
open questions and later increments) remains **incomplete**.

# Verified delivery candidate (lockstep increment)

This records a **git-backed lockstep candidate** SHIP, not a live
install and not a coding-quality guarantee. Do not add overlapping
pytest counts. Phrase-presence prompt tests are not GOAL semantics.

## Baseline and product commits

Exact baseline (pre-increment):

`5f3bd8f7895380f5fce0c0ca365bcf755978140b`

Product commits on this increment (plus mailbox / docs):

| SHA | Subject |
|---|---|
| `d4c1a24159f13e476823534bbbe3bd569f1d95e2` | `slice(goal-knowledge-planning):` shared planning/eval obligations |
| `ed4db6dcb4cf3ca41f92335ff3db3d301b788f75` | `slice(completion-reliability-repair):` bind attempts and retirement |
| `c67c31d5c0341296467b4db845d47c1de341aa03` | `slice(completion-cli-compatibility):` CLI retirement outcomes |
| `a0591a894b546239c845b3b357b56ed7fd777b49` | `slice(verdict-acceptance-binding):` first-pass SHIP = resume gate |
| `71e57872e802479a248cf5019cce59cd09a6c7e9` | `slice(untracked-product-guard):` nonignored leftover product blocks |
| `cdb6926824f7ea321ab8f7bbeaef7aac33ebcd51` | `slice(cli-fixture-isolation):` helpers outside fixture git trees |
| `071028a66afad259a2a99e1d6f7677500563d088` | `loop: iteration 1 — SHIP` |
| `b6091e8ade613a64730b70c66e7651856c4f219a` | `loop: iteration 2 — SHIP` |

Mailbox: `loop-concrete-workflow/VERDICT.md` — **SHIP**, git-lockstep
safeguards only. Later `docs:` commits do not change product SHAs.

## Shared attempt / evaluated / product guard

From `metrics/trio_loop.py` (not invented flags):

- Lockstep mints `evaluator_attempt` and `evaluated_sha` (`pinned_sha`
  into the runner / Omnigent `LOCKSTEP CONTEXT:`).
- `_fresh_evaluator_artifact` and first-pass `_apply_verdict` share
  the same `attempt:` + `evaluated:` gate. Missing fields stay
  `needs_retirement` (exit 6). Leftover same-iteration SHIP is not
  this attempt. Product `commit:` is not the pin.
- Graded product must match the pin except mailbox paths. Later
  product commits and tracked product edits fail closed.
- Any **nonignored untracked product** file outside the active
  mailbox fails closed, including files that existed before the pin.
  The driver reports those paths and never stages or deletes them.
  Mailbox files and gitignored outputs do not block. Track or
  gitignore leftovers deliberately; SHIP does not claim the full
  tree is verified while they remain untracked.
- CLI `run` uses `repo=Path.cwd()`. Git cwd without verified
  retirement is exit 6. Retirement needs real objects, ancestry,
  and a mailbox-path `loop: iteration N — SHIP` commit.
- No-repo fakes still skip the git check and can exit 0 without
  `attempt:` / `evaluated:`. **Not qualified.**

Supported lockstep driver (in-tree `metrics/` required):

```bash
python3 metrics/trio_loop.py run --mailbox <dir> --max-iterations N --lockstep
# TRIO_MODE=lockstep LOOP_DIR=<dir> ./portable/driver.sh N
```

`--open-loop` / `--lockstep` are exclusive. Open-loop
`kind=slice-eval` still treats any `VERDICT.md` byte change as
ready. **Not qualified.**

## Prompt generation

`python3 prompts/generate.py --check` is the freshness command.
Independent checks: exit 0, **51** generated files. Native
Lead/Evaluator and Omnigent `_prompt()` share obligation strings.
That is string presence, not semantic-quality proof. This tree has
no `knowledge.yaml`; missing knowledge is not a blocker.

## Actual validation

The disposable product pilot (`concrete-loop-pilot-report.md`,
controller pin `90c37b2` / repair `ed4db6d`) ran on that **older**
controller. Lab glue needed one import intervention
(`python3 -m workflow_lab.concrete_loop_role`). The shipped
verdict omitted `attempt:`; first-pass accept still retired. That
run is **not** evidence for the current controller.

Corrected gates were checked with independent regression probes
and focused pytest. The **final controller was not rerun** as a
live broker end-to-end lockstep on a product tree.

From
`/home/coder/workflow-lab/.runtime/concrete-acceptance-evaluation.out`
(pin `a0591a8`, no source edits): original missing-attempt verdict
shape now first-pass rc **6**; `--check` 51 files; **104 passed**
on trio_loop + portable + cli_openloop + omnigent + obligations;
Git probes: missing/wrong attempt, wrong `evaluated:`, later
commit, tracked dirty, wrong-ancestor retirement → 6; mailbox-only
retirement on graded tree → 0. Untracked product was still exempt
at that pin.

From
`/home/coder/workflow-lab/.runtime/concrete-last-guard-eval.out`
(pin `71e5787`): untracked extra.py (pre and post pin) → 6, files
intact; ignored `build/out.bin` → 0; mailbox bookkeeping allowed;
CLI helpers inside the fixture git cwd → 6; same scripts **outside**
the repo → 0. Controller/portable/omnigent/obligations: **97
passed**. Integrated six-file suite: **130 passed, 2 failed**
(genuine-retirement CLI fixtures treated harness scripts as
untracked product). 11/11 adversarial probes matched the guard.

From
`/home/coder/workflow-lab/.runtime/concrete-fixture-final.out`
(tests-only fix, later `cdb6926`): helpers under `tmp_path/helpers`,
git repo under `tmp_path/repo`. Six-file suite **132 passed**;
`--check` 51 files. Production controller unchanged after the
independent guard review.

Do not add 104+97+132. No measured coding-quality benefit. No live
install, canary, installer, profile, or provider launch.

## Opt-in (candidate only) and rollback

User approval covers **candidate preparation** only. Do not run
`./install.sh`, do not change global profiles, do not launch brokers.

1. Work in `/home/coder/workflow-lab/.runtime/trio-workflow-candidate`
   (or a clone of the export bundle). Driver:
   `metrics/trio_loop.py` + `portable/driver.sh`.
2. Prompts as generated here: `python3 prompts/generate.py` or
   `--check`.
3. Lab development pin (not a global profile):
   `/home/coder/workflow-lab/config/development-grok-low.toml`
   (`cursor-grok-4.6-low`, effort `low`).
4. `install.sh --dashboard` copies are **not** this lockstep driver.

**Rollback** — stay on baseline `5f3bd8f…`. Disposable clone:
`git checkout 5f3bd8f7895380f5fce0c0ca365bcf755978140b`.
Or `.runtime/concrete-workflow-export/baseline.bundle` if that file
is the recorded baseline snapshot. Do not reset a shared live
checkout from this document.

## Exports

`/home/coder/workflow-lab/.runtime/concrete-workflow-export/`

- `candidate-verified-delivery.bundle`
- `candidate-vs-5f3bd8f.patch` (`git diff 5f3bd8f HEAD`)

Verify: `git bundle verify <bundle>`.

Local candidate tag (git-lockstep scope only):
`trio-verified-delivery-candidate-v0.1`.

## Promotion (pending)

No supported promotion procedure is recorded in source. Live
`/home/coder/personal/trio-agent-loop` and existing installs stay
unmodified. PIPELINE.md later increments remain **incomplete**.

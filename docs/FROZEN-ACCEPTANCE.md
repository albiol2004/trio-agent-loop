# Frozen acceptance (r19)

Status: built behind the switch `[acceptance] enabled`, **default off**.
The default flips only if the r19 measurement passes its pre-registered
rule (design §7.6). Schema: MAILBOX-SCHEMA.md "Frozen acceptance (r19)".

## What it does

An independent **acceptance author** turns GOAL.md into black-box
executable checks before any builder runs. The author is never the Lead.
The driver validates the checks at the loop's base, freezes them in a
driver-made commit, and requires the PLAN to map every one. The Evaluator
is the only role that may amend a check afterwards, within mechanical
limits. The driver refuses a SHIP unless the frozen pack passes at the
evaluated revision.

```
loop start ──> author (export of base, GOAL only) ──> validate at base ──> audit ──> freeze commit
     │                                                                                   │
     └─> Lead pass 1 (drafts PLAN, `acceptance wait`) ──> maps covers ──> builders allowed ┘
slice-eval: + ACCEPTANCE (covered) line      integration-eval: + driver pre-run of the pack
verdict: amendments -> anti-thrash -> SHIP gate (pack re-run at the evaluated sha)
```

## Switch

| where | form |
|---|---|
| profile (`~/.config/trio-agent-loop/omnigent.toml`) | `[acceptance]` `enabled = true`, `wait_s = 900` |
| env | `TRIO_ACCEPTANCE=1` / `0` |
| CLI | `trioctl omnigent loop --acceptance` / `--no-acceptance`; `trioctl omnigent doctor --acceptance` |
| loop core | `run_loop(..., acceptance={"enabled": True, "wait_s": 900})`; `trio_loop.py run --acceptance` |

The CLI flag wins, then the env, then the profile -- for the whole run:
`--no-acceptance` over `TRIO_ACCEPTANCE=1` or a profile `enabled = true`
makes the driver record `acceptance: {"enabled": false, "source":
"--no-acceptance"}` in `.driver.json` (fresh start and re-attach alike), and
every builder dispatch (`trioctl omnigent run builder --mailbox …`, isolated
or not) reads that record before its own env/profile, so no builder is
refused with "acceptance not frozen yet" (r20 review round 2). Without an
env/profile ON switch nothing is recorded (switch-off runs stay
byte-identical). **The profile alone is
enough to turn it on for every loop of this user**, including a trio-dash
resume (the dashboard passes no `--acceptance` flag, so the profile or
`TRIO_ACCEPTANCE` decides). With it on, every root-free loop (open-loop and
lockstep, r16/r17) whose repository still has an API-6 metrics/ set
committed is refused (exit 3, nothing created) until that set is refreshed;
see "Install impact". Leave `[acceptance]` out of the profile (the default)
and those loops run unchanged. With the switch off
nothing changes: prompts, LOG.md, STATE.md, `.driver.json` and commits are
identical to r17-rc (a test compares the loop core with `9342a57`). The one
exception is the C1 slice-eval trim, which applies whatever the switch says
(below).

## Components

| piece | file |
|---|---|
| runner, manifest, freeze filter, export, audit, amendment scope, driver commits | `metrics/trio-acceptance.py` |
| `covers:`, `parse_plan_acceptance`, METRICS_API 7 | `metrics/trio-metrics.py` |
| coverage/manifest violations, `coverage_refusals` | `metrics/trio-check.py` |
| commit-gate guard (`acceptance_offenders`) | `metrics/trio-shadow.py` |
| author phase, pin checks, restore, coverage gate, pre-runs, amendments, anti-thrash, SHIP gate | `metrics/trio_loop.py` `AcceptanceController` |
| role, switch, doctor, builder refusal, brief section, `acceptance` commands, author dispatch | `omnigent/trioctl` |
| author prompt, Lead/Evaluator fragments | `prompts/canonical/acceptance*.md` -> `omnigent/entrypoints/trio-omnigent/prompts/` |
| registered author | `omnigent/trio-omnigent-roles/acceptance/config.yaml` (generated; model = the evaluator's) |

## The author and its isolation

- Its workspace is `git archive <base>` in the driver's state directory, not
  the repository. It has no `.git`. Every mailbox directory, `archive/`,
  `.sessions/`, `.trio*`, `.cursor/` and the vendored Trio metrics files are
  removed. `.acceptance-input/` holds only GOAL.md and the optional
  `ACCEPTANCE-NOTES.md`. The repository path is never named in its prompt.
- The driver audits the session's tool calls for evidence that the author
  **read** the loop's plan or code. Only tool-call arguments count, never a
  tool's output and never chat text. A session is flagged when an argument
  names any of:
  - a path inside the loop repository (its checkout, git dir or mailbox).
    Relative paths resolve against the command line's tracked `cd` (the
    export by default); `$HOME`, `${HOME}`, `~` and `$PWD` are expanded;
  - an ancestor of the repository given to a reading or searching command
    (`cat`, `grep`, `find`, `rg`, ...) or as a bare tool path (Grep, Glob,
    Read, LS); `/` counts only for recursive searchers;
  - `cd` into the repository or an ancestor of it, `$OLDPWD`, or
    `/proc/<pid>/cwd|root`;
  - a lab hidden-pack path (`/hidden/`, `speed/hard`).

  A mailbox file name (`PLAN.md`, `REPORT.md`, `LOG.md`, ...) counts only
  when it resolves inside the loop repository: a product that writes its
  own `REPORT.md` to a scratch dir is not the mailbox (eval-r19b finding 5).
  The audit is best-effort (DESIGN §10): a determined author can still
  hide a read.

  Route literals such as `/api/openrouter/stats`, `$TMPDIR`, toolchain
  paths (`~/.nvm/...`) and system paths never contaminate (eval-r19
  finding 2). A contaminated session is discarded and re-run once with a
  stern prefix. A second contaminated session stops the loop
  (`acceptance-contaminated`). When the broker history has no row
  recognisable as a tool call, the audit is limited: it checks only
  whether the authored files spell the loop repository's path, and records
  `limited: true`.
- Validation runs on a **fresh** export, so files the author created in its
  own workspace never make a check pass. A `behaviour`/`doc` check that
  PASSes at base, ERRORs, misquotes the GOAL, breaks the schema or exceeds
  the budget is dropped. The 4th and later guards are dropped. UNAVAILABLE
  checks are kept and flagged. A check whose pack code FAILs at base on
  an import error (the last traceback starts in pack code, a pack script
  or `-c`, and ends in `ModuleNotFoundError`/`ImportError`; or a pack
  pytest run whose summary names one) is dropped as `broken-at-base: pack
  code cannot import (...)`: pack code runs with `-P -s`, so that import
  fails forever and the check is not discriminating (eval-r19f finding 3;
  a product traceback the check echoes does not count). The author gets
  one retry, with the drop list, when more than 30% were dropped, fewer
  than 5 checks remain, or any check was dropped for a pack import.
- Tier: the author runs on the Lead/Evaluator model. Without
  `[roles.acceptance]` it inherits `[roles.evaluator]`. With the switch on,
  the doctor FAILs unless lead, evaluator and acceptance resolve to one
  model and effort, and `omnigent loop` refuses a static tier mismatch.

## Freeze, pin and integrity

- The freeze commit is `acceptance: freeze <n> checks (<model>)` with the
  trailer `Acceptance-Pin: <sha256>`. The driver makes it under git's own
  `index.lock`: its tree is HEAD plus the pack only, and the real index gets
  the same entries. A concurrent Lead commit can therefore neither sweep the
  pack into its own commit nor drop it.
- The pin, the FROZEN digest, the pin commit and the chain live in
  `$XDG_STATE_HOME/trio-agent-loop/acceptance/<mailbox>-<hash>/acceptance.json`
  (override: `TRIO_ACCEPTANCE_STATE`), outside the repo and every agent
  worktree. They are mirrored in FROZEN, in STATE.md `acceptance_pin:` and
  in `.driver.json` `acceptance`.
- The driver checks the pack hash and the FROZEN bytes against the pin:
  - before and after every Lead/repair pass;
  - before every slice-eval and integration-eval.

  A mismatch that no valid amend commit explains is restored with
  `acceptance: restore (tamper after <sha12>)` and logged
  `acceptance tamper restored (<role>)`. It counts as a gate breach, and
  the role is re-run once. The driver state counts restored tampers
  loop-wide (`tamper_events`). The second one, in the same pass or any
  later pass, stops the loop with `status: error` (reason
  `acceptance-tamper-repeated`).
- The driver state also records every pack commit the driver made
  (`freeze_commit`, `driver_commits`) and every authenticated human
  amendment (`human_amends`). A commit subject or trailer proves nothing
  by itself.
- Open-loop runs `check_pin`, restore, the tamper count and amendment
  processing on two threads; one controller lock serialises them and a
  tamper seen by both threads is counted once. A restore builds the pack
  beside the old one and swaps it in with one atomic
  `renameat2(RENAME_EXCHANGE)` (two renames where the kernel lacks it), so
  a concurrent reader never sees a missing or half-copied pack; it retries
  a filesystem error, then stops the loop (`acceptance-restore-failed`)
  (eval-r19b finding 4, eval-r19c area 3).
- `trio-shadow.py --require-commits` derives the pin chain from git objects
  alone (`trio-acceptance.derive_pin_chain`), walking the first-parent
  history from `--acceptance-base`, else the driver state's run head, else
  the root (the driver judges amendments on the same first-parent line):
  - a merge commit that changes the pack against its first parent (a land
    merge-not-rebase, or any merge bringing a pack edit from its other
    side) is tamper, whatever its subject; an amend commit behind a merge
    is never an amendment; a HEAD whose first-parent history does not
    contain the freeze has no valid freeze (resume: NEEDS_HUMAN)
    (eval-r19c area 1);
  - the freeze is the *first* commit that adds FROZEN; a `git rm` and
    re-add, or a second freeze subject, is tamper and never moves the base;
  - a driver restore must put back the current pin;
  - a driver pin must extend FROZEN only and pin a legitimate amendment:
    the Evaluator rules (scope by file attribution, records, budget, and
    discrimination re-checked by running the pack at the frozen base
    before and after), or an adopted human amendment named in its
    `Acceptance-Human-Amend:` trailer;
  - HEAD's committed pack must equal the last legitimate pin, so an amend
    commit no pin follows, or a forged pin, fails (eval-r19b finding 3).
  - With the driver state visible it only adds strictness: freeze, pin and
    restore commits must be in its record, adopted human amendments must be
    recorded, and its pin must be the chain's. The state is never used to
    relax a check.
  - Mailbox reuse (an older pack at the same path) needs
    `--acceptance-base` when the driver state is not visible.
  - Only a genuine restore excuses the earlier tamper.
  - A `slice(<id>):` commit behind the freeze (a Lead take-over made while
    the author was still working) is tolerated with an `acceptance note:`.
    The author worked from the base export and never saw it, and the Lead
    prompt forbids product commits before FROZEN. A slice on a line
    without the freeze fails with "acceptance/freeze ordering". The
    driver's `gate breach` LOG line quotes the first acceptance reason.
- Runs copy the pack without what the pin skips (`node_modules/`, caches,
  `*.pyc`), and driver commits never add those paths. The symlink rule
  below skips the same directories (eval-r19e finding 6): an author's or
  Evaluator's local `npm i` in `acceptance/` (`node_modules/.bin/*`) is
  neither a pack error nor a reason for `amend --human` to refuse.
- A directory tree with entries the driver cannot read (a mode-000 file)
  is copied without them; each is logged as `tree copy: skipped
  unreadable <path>: <error>` in the run's log. An unreadable tree root
  is a run error (`cannot copy tree`), never a crash (eval-r19e finding 7).
- **No symlinks in the pack (eval-r19d finding 7).** Validation reports a
  symlink as a pack error (the author gets one retry), the frozen pack
  never contains one (`write_frozen_pack` skips them), and amendments,
  human amendments and pin commits that contain one are refused. Use a
  real file.
- **Path lists are NUL-safe (eval-r19d findings 2 and 3).** Every place
  the driver, `derive_pin_chain` and trio-shadow list commit or pack paths
  uses `-z` with `core.quotePath=false`, so a non-ASCII or space-containing
  file name is one path (git would otherwise print it quoted, and the
  driver's own freeze would look like it touched files outside the
  pack).
- **Author guidance: explicit paths, no computed paths or globs.** A check
  sees only the pack files attributed to it (its own, the shared ones).
  Attribution is a static text scan: a file loaded through a computed
  name (`f"case_{n}.json"`) or a directory glob is attributed only if some
  text names it; otherwise it is absent from the check's view, and the
  check FAILs at base and forever (FileNotFoundError), invisibly at
  freeze. Name every pack file a check loads by an explicit path
  (`$ACC_DIR/checks/ACC-07/cases.json`), or put shared data under
  `fakes/` or `lib/` (in every view).
- **Per-check isolation (eval-r19c finding 1).** A check's outcome depends
  only on its own pinned files, the shared pinned files and the product
  tree:
  - every run snapshots the pack once (the reported `manifest_sha256` is
    what ran) and copies the tree once into a master no check runs in;
  - each check runs in its OWN fresh copy of that master (`cp -a
    --reflink=auto`, prepared while the previous check runs), with its own
    read-only pack view: only the files attributed to it plus the shared
    ones (`pack_attribution`/`check_view`); MANIFEST, AMENDMENTS, AUTHOR
    and FROZEN are in no view. Under bwrap only its copy and scratch are
    writable and the view is bind-mounted read-only; with `sandbox: none`
    the view is chmod read-only, the masters are re-verified (ctime
    signature) after every check, and immediately before each check runs
    its prepared copy AND its pack view are compared by content with the
    masters (eval-r19d finding 6: the next copy is prepared while the
    previous check runs, so an unsandboxed check could otherwise rewrite
    it). Any change FAILs that check and every later one with reason
    `isolation`. This is detection, not enforcement: a process that
    escapes a check under `sandbox: none` can still race a check after its
    copy was verified; only bwrap enforces isolation;
  - the check's OWN Python runs with `-P -s` when the code it executes
    comes from the PACK (eval-r19c F2; eval-r19e findings 1, 3, 4, 5):
    - a script that resolves (symlinks followed, as CPython does for
      `sys.path[0]`) inside the check's `acceptance/` view:
      `python3 [opts] acceptance/checks/x.py`;
    - `python3 -c <code>` (the code is the manifest's): the tree root
      (cwd) is not on its `sys.path`, so a product-root `json.py` cannot
      answer for the judge;
    - `python3 -m <name>` whose top-level module is in `acceptance/lib/`
      (found through `PYTHONPATH` and preferred over a same-named product
      module). `-m acceptance...` is a validation error: run a pack
      script by its path.

    `-P` keeps the script directory / tree off that interpreter's
    `sys.path`; `-s` keeps user site-packages (a `.pth` or
    `usercustomize.py` planted in `~/.local` after the driver started) out
    of it, so pack code gets the standard library plus `acceptance/lib/`
    only. A `run` that starts the PRODUCT gets neither flag and runs
    exactly as its users run it: `python3 app.py` importing a sibling
    module, `python3 template/build/test_x.py`, `python3 -m pkg` (cwd
    import), `python3 -m pytest` (user site). Both are flags, not
    environment variables, so the product and every process a check
    starts resolve imports normally, user site included (eval-r19d
    finding 1; round 3 exported `PYTHONSAFEPATH`/`PYTHONNOUSERSITE`, which
    every child inherited). The option parser follows CPython: combined
    short options (`-um pkg`, `-Bc code`, `-mpkg`, `-Wignore`), value
    options as separate words (`-W x`, `-X y`, `--check-hash-based-pycs
    v`) and `--`. A nested interpreter (`sh -c "python3 acceptance/..."`,
    `/usr/bin/env python3 ...`) gets no flags and sees only its own view.
    Interpreters older than 3.11 get `-s` only and rely on the views.
  - pytest run on PACK tests (`python3 -m pytest ... acceptance/...` or
    `pytest acceptance/...`; eval-r19f finding 1) gets
    `-p no:cacheprovider --confcutdir=<view> -c /dev/null --rootdir=<view>`
    in front of pytest's own arguments, and `python3 -m pytest` also gets
    `-P` (not `-s`: pytest may live in user site). No product
    `conftest.py`, `pytest.ini`/`pyproject.toml`/`setup.cfg`/`tox.ini`
    addopts, tree-root `*.dist-info` plugin entry point or `pytest.py`
    decides a pack test's outcome. pytest on the product's own tests
    (`python3 -m pytest tests`) is the product's and gets nothing.
    Residual: product code (and every child) still loads user site, so a
    same-uid process that plants a `.pth` there can still affect product
    runs; under `sandbox: none` a hostile check can plant one itself.
    The explicit, pinned helper path is `acceptance/lib/` (on `PYTHONPATH`
    and `NODE_PATH`, shared by every check). Implicit-load variables of
    the driver's environment (`PYTHONPATH`, `PYTHONSTARTUP`, `NODE_PATH`,
    `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `PERL5LIB`, `RUBYLIB`, ...) are not
    inherited;
  - checks run with a controlled `PATH` (eval-r19d finding 4): the system
    directories (`/usr/local/sbin`, `/usr/local/bin`, `/usr/sbin`,
    `/usr/bin`, `/sbin`, `/bin`) plus the directory of each interpreter
    (`python3`, `node`, `sh`, `bash`, `git`, `make`, ...) the DRIVER
    resolved on its own `PATH` when it started. The driver keeps it in
    memory (its state file records it but it is never read back), so a
    user-writable directory early on the driver's `PATH` (`~/.local/bin`)
    is not searched by checks unless an interpreter itself was resolved
    there, and a shim written there after the driver started never runs
    as a check's interpreter. This closes the cheap part of the same-uid
    limit; a same-uid process can still replace what the driver resolves
    before it starts (or the driver's own `git`). Tools a check needs
    beyond that belong in the pack (`fakes/bin`, prepended by the check)
    or in `needs`; `python3 -m pytest` finds a user-site pytest through the
    interpreter, not through `PATH`;
  - driver runs (slice-eval covered checks, integration pre-run, the SHIP
    gate) read the pinned pack from git at the driver's pin commit and
    verify its hash, never the working tree a role can edit mid-run
    (`acceptance-pin-unreadable` fails closed);
  - measured overhead (20 checks): ~12–25 ms per check on a 550-file tree;
    on a 5,000-file / 50 MB tree ~50–90 ms per check when checks take
    ~0.5 s (the next copy is hidden behind the running check) and up to
    ~250 ms per check for near-instant checks.
- Isolated builders that touch the mailbox are already retained with
  `mailbox_write` (unchanged).

## Check outcomes and `expect`

A check exits 0 on PASS, 1 on FAIL, 77 on UNAVAILABLE; a timeout is a
FAIL, anything else is an ERROR re-run once. On exit 0 every `expect`
pattern must match too (eval-r19e finding 2):

- `expect.stdout`: regexes matched (`re.search`, `re.MULTILINE`) against
  the check's full stdout;
- `expect.stderr`: the same against its full stderr;
- `expect.output`: the same against stdout followed by stderr, for a check
  that deliberately asks for both.

stdout and stderr are captured separately; each stream's first 1 MiB
(`MATCH_BYTES`) is matched, the rest is drained and discarded. The
`excerpt` in results and the one-line FAIL reason come from the last 2 KB
(`EXCERPT_BYTES`) of both streams in arrival order and are evidence only;
patterns never see just that tail. (Before round 5 patterns saw the last
2 KB of merged stdout+stderr, so a `--help` obligation early in a long help
text never passed.)

Matching one attempt's patterns is bounded by min(`timeout_s`, 10 s)
(`MATCH_DEADLINE_S`; eval-r19f finding 2): it runs in a forked child that
is killed at the deadline, and the check FAILs with reason
`pattern-timeout` (detail: the pattern). The matching time is part of the
check's `wall_s`. A backtracking pattern such as `.*"count"` that does not
match a long single line is quadratic in `re`; before the bound it could
stall the pack for minutes.

## Coverage

Every `behaviour`/`doc` id goes into a slice's `covers:` or into
`lead_integration:`. The Lead waits with
`trioctl omnigent acceptance wait --mailbox <mb>`, maps the checks and
commits the PLAN. `trioctl omnigent run builder` (isolated or not) refuses
with exit **10** (`ACCEPTANCE_REFUSED_EXIT`), before any worktree exists,
while any of these hold:

- the pack is not frozen;
- PLAN.md is uncommitted;
- a check is unmapped or unknown, or a binding is undeclared;
- the pack is off its pin.

After each Lead pass the driver runs `coverage_refusals`. A refusal logs
`lead pass refused: acceptance coverage` and re-runs the Lead once with the
refusal text (`acceptance_errors`). A second refusal sets `status: error`
(exit 3).

## Evaluation

- **Slice-eval.** The driver runs the slice's covered checks at the retired
  sha and adds `ACCEPTANCE (covered): ACC-03 PASS · ACC-05 FAIL ...
  [sole-cover|shared]`. A `sole-cover` FAIL is blocking.
- **Integration-eval and lockstep evaluation.** The driver pre-runs the
  pack at the pin and adds a `FROZEN ACCEPTANCE @<sha12>: p/t PASS` block:
  per-check outcomes, the pin chain, `ACCEPTANCE-DISPUTE:` lines and the
  AUTHOR.md pointer. The Evaluator:
  - re-runs the non-PASS checks;
  - adjudicates the disputes;
  - may amend;
  - aims its independent probe at the GOAL sentences no check covers;
  - writes `## Frozen acceptance` in VERDICT.md.
- **After the verdict, amendments.** Amend commits since the pin are
  validated on git objects (the pinned pack at the driver's pin commit,
  the amended pack at HEAD):
  - scope: only a check's files, `run`, `expect`, `timeout_s`, `binds`,
    `needs`. Every changed pack file is attributed (`pack_attribution`, in
    the pinned and the amended pack) to every check that can load it: the
    checks whose `run` names it, whose `run` strings or used files mention
    it (transitively), and, for a SHARED file, every check. A top-level
    `checks/` file is mentioned by its basename or stem. A per-check file
    (`checks/<ID>/...`) is mentioned only path-like (eval-r19d finding 5):
    `<ID>/<its path under checks/<ID>/>` in the text, or the token `<ID>`
    together with its basename (or its stem, for an importable extension
    such as `.py`, `.mjs`, `.json`). A bare common token (`check`,
    `expected`, `server`, `data`) in another check or in `lib/`/`fakes/`
    no longer makes `checks/<ID>/check.py` another check's or shared:
    another check cannot reach `checks/<ID>/` without naming it, and a file
    not attributed to a check is absent from its view. Per-check files
    named like a standard-library module keep the bare-stem rule. Shared: `fakes/**`, `lib/**`, any directory
    other than `checks/` and `checks/<ID>/`, interpreter-loaded names
    (`__init__.py`, `conftest.py`, `sitecustomize.py`, `*.pth`,
    `package.json`, `tsconfig.json`, `.npmrc`, `pyproject.toml`, shell rc
    files, `Makefile`, ...), a top-level `checks/` file whose stem shadows
    a standard-library module (e.g. `checks/subprocess.py`), a `checks/` file no
    check uses, and anything a shared file mentions. Every owning check
    must be named and counted, so a shared file is effectively frozen
    within the budget. An amendment may add a file only under
    `checks/<ID>/` of an amended ID, and only if no other check would load
    it. An amended `run` may not point at another check's file or at an
    unowned top-level `checks/` file (eval-r19b finding 1, eval-r19c
    finding 1);
  - no symlinks: an amendment that adds or keeps a symlink in the pack is
    rejected, and a pin commit whose pack holds one is tamper (eval-r19d
    finding 7; the pin hashes a link's text, not its target, so a link
    out of the pack could retarget a check with no further amend commit);
  - an AMENDMENTS.md record per id;
  - discrimination: the WHOLE pack re-runs at base with the pinned and the
    amended pack, and every check that FAILed before must still FAIL;
  - at most 2 per loop and 25% of the checks; no check removed.
  - A valid amendment extends the pin chain (`acceptance: pin <sha12>
    (amend ACC-..)`).
  - An invalid one is reverted.
  - Going over the budget forces NEEDS_HUMAN.
  - Every amend commit in a running loop is judged as the Evaluator's,
    including one labelled `(human)`. The driver logs
    ``unauthenticated `(human)` label``, and the scope, record, budget, must-FAIL-at-base
    and no-removal rules all apply (eval-r19 finding 1).
  - With fewer than 4 frozen checks the 25% share allows no amendment. This
    fails safe: an amendment on a small pack forces NEEDS_HUMAN and never
    softens a check (finding 10, kept deliberately).
  - **Known limit of the amendment budget (eval-r19c finding 2).** A
    named, counted, in-budget amended check can still be weakened
    *conditionally on post-base state*: it FAILs at base (so the
    discrimination re-run is satisfied) yet passes trivially on any tree
    past the base (for example it probes a feature other than the one its
    `goal_quote` names). Discrimination-at-base is the only mechanical
    test; whether an amended check still tests its `goal_quote` is the
    Evaluator's judgement, bounded by the budget (at most 2 checks and 25%
    per loop), recorded in AMENDMENTS.md and the pin chain, and visible to
    the human reviewing the loop. Per-check isolation guarantees the
    weakening stays confined to the named checks.
- **Bindings.** `acceptance_bindings:` values come from the Lead's PLAN and
  reach every run as `ACC_BIND_*`. trio-check validates only the names. The
  Evaluator fragment therefore asks the integration Evaluator to review
  each value against the GOAL (finding 11).
- **Anti-thrash.** A check that FAILs in 2 consecutive integration pre-runs
  with no accepted amendment forces NEEDS_HUMAN.
- **SHIP gate.** On SHIP the pinned pack (from git) is re-run at the
  evaluated sha, each check in its own copy of that tree. In a root-free
  run with the switch on, a land merge that changes product paths is never
  landed on `full_check:` alone: it re-verifies, so the gate runs on the
  exact tree that lands.
  - Any FAIL gives `ship_unaccepted (acceptance)`: the SHIP is treated as
    ITERATE and the failures ride to the next Lead pass.
  - Any UNAVAILABLE forces NEEDS_HUMAN (`acceptance-unavailable`).
  - An ITERATE whose pre-run had only UNAVAILABLE non-passes is logged
    `acceptance-unavailable-iterate`.
  - A refused SHIP is logged `acceptance: SHIP refused by the acceptance
    gate (verdict becomes ...)`. The driver commits it as `loop: iteration
    N — acceptance gate refused SHIP (<verdict>)`, next to any SHIP
    retirement commit the Evaluator already made (finding 13).

## Human amendments and resume

- A human amends only while the loop is stopped, with
  `trioctl omnigent acceptance amend --human --ids ACC-.. --reason "..."
  [--adopt <sha>,...]`. The command refuses (exit 5) while a live driver
  holds the mailbox and takes the mailbox lock itself for its whole
  duration, so a driver cannot be starting underneath it (a pid-less
  `.lock` also refuses) and no driver can start until it is done
  (eval-r19c area 2). STATE.md is never consulted: a role that writes
  `status: needs_human` gains nothing. A role that deletes the lock of a
  running driver can make the command run (same uid: it cannot tell a
  role from a human), but the running driver keeps its pin in memory: the
  re-pinned pack is restored as tamper, the SHIP gate runs the driver's
  pinned pack from git, and the driver state it saves carries no such
  adoption. It commits the working-tree edits as `acceptance: amend <ids>
  (human): <reason>` and re-pins with a pin commit whose
  `Acceptance-Human-Amend:` trailer names the adopted amend commits, and it
  records the adoption (amend shas, pin commit) in the driver state
  (`human_amends`, `human_adoptions`). Pack commits the human already made
  since the last driver pin are adopted only when named with `--adopt`
  after review; any other pack commit since the pin refuses the command.
  Human amendments never count toward the Evaluator budget.
- Resume never adopts anything (eval-r19b finding 2). A `(human)` amend
  commit made while the loop was stopped (by a human who skipped the
  command, or by a background job a role left behind) is logged
  `not adopted: resume never adopts` and restored as tamper.
- Resume reconciles the driver state with the pin chain derived from git
  (HEAD's objects, the first FROZEN add, structurally verified driver
  pin/restore commits; never the working-tree FROZEN):
  - a state whose pin or freeze differs from the git chain forces
    NEEDS_HUMAN (`acceptance-state-mismatch`);
  - a lost state is re-derived from git (`pin re-derived from git
    history`), unless the freeze is ambiguous (FROZEN added more than once)
    or missing, which forces NEEDS_HUMAN (`acceptance-state-lost`).
- At every loop stop the driver calls the runner's
  `sweep_role_processes()` when it has one, a best-effort kill of the role
  process groups it started. OmnigentRunner has none: its roles run in
  broker-owned sessions outside the driver's process tree. A same-uid
  process that survives the stop can still do whatever the human can
  (including running the amend command); the explicit command is the
  boundary.
- Resume compares the frozen MANIFEST's `goal_sha256` with the current
  GOAL.md. On a mismatch (a reused mailbox with a new GOAL) the loop stops
  with `status: needs_human`, `phase: acceptance-goal-changed` (finding
  12).
- `trioctl omnigent acceptance wait` also returns when git shows the
  committed freeze with a clean FROZEN. The Lead's shell need not see the
  driver's state file (finding 6).
- `python3 metrics/trio_loop.py run --runner omnigent --acceptance` (or
  `TRIO_ACCEPTANCE=1`) passes the switch into the runner, so the Lead and
  Evaluator get the acceptance prompt blocks (finding 7).

## C1: slice-evals back to fast (independent of the switch)

The r18a slice-eval pack is removed: per-accept table, `attacks:`,
per-slice independent probe, and the re-execution duties. What remains is
the rules plus one `evidence:` summary line. The evaluator rigor is split:

- `RIGOR_CORE` stays in the registered evaluator config and the per-dispatch
  prompt;
- `RIGOR_INTEGRATION` is generated into `integration-rigor.md`, which trioctl
  appends only to integration-eval and lockstep evaluator prompts.

Slice sections without `attacks:` log `attacks=n/a`.

## Commands

```
trioctl omnigent acceptance export   --mailbox <mb> [--base <rev>] [--out <dir>]
trioctl omnigent acceptance freeze   --mailbox <mb> --export <dir> [--model m]   # loop stopped
trioctl omnigent acceptance wait     --mailbox <mb> [--timeout S]               # 0 frozen, 3 author error, 4 timeout
trioctl omnigent acceptance run      --mailbox <mb> [--tree <dir|rev>] [--ids ...] [--json]
trioctl omnigent acceptance status   --mailbox <mb> [--json]
trioctl omnigent acceptance amend    --mailbox <mb> --human --ids ACC-.. --reason "..." [--adopt <sha>,...]   # loop stopped
trioctl omnigent acceptance validate --export .                                  # the author's own check
```

`trioctl acceptance ...` is an alias.

## Install impact (not performed by the build)

- `REGISTRY_PROFILE` is now `cursor-grok-4.6-medium+glm-5.2-max-v4-acc`.
  The doctor refuses until **all** anchors are re-registered: lead,
  evaluator and the new `trio-omnigent-acceptance` (SKILL.md step 3). Needed
  anyway, because C1 changed the registered evaluator prompt.
- `install.sh --omnigent` copies the acceptance role and templates its model
  from the evaluator config. It installs `trio-acceptance.py` next to
  trioctl and in `trio-release-metrics/`.
- Repositories must vendor the METRICS_API 7 set, now five files including
  `trio-acceptance.py`, with `trioctl omnigent metrics refresh --commit`
  before `--acceptance` runs there. An older set still runs with the switch
  off. With the switch on, `trioctl omnigent loop` checks the set committed
  on the target (or a re-attached loop branch) before creating any Lead
  worktree, branch or registry record, and refuses (exit 3) below
  METRICS_API 7, naming `trioctl omnigent metrics refresh --mailbox <mb>
  --commit` (r20); the check on the loaded core remains a second guard.
  STATE.md and LOG.md are not touched by this refusal.
- The switch may come from the profile: `[acceptance] enabled = true` in
  `~/.config/trio-agent-loop/omnigent.toml` refuses **every** honest
  API-6 root-free loop (r16 open-loop, r16b/r17 lockstep), dashboard
  resumes included, until its repository is refreshed. Turn it on in the
  profile only after refreshing the repositories you run loops in, or use
  `--acceptance` / `TRIO_ACCEPTANCE=1` per run.
- A loop already running on its `trio/<slug>` branch (started with the
  switch off) runs the metrics/ set committed on **that branch**; a target
  refresh does not reach it. Its refusal names the three ways out: refresh
  inside the Lead worktree (`trioctl omnigent metrics refresh --repo <Lead
  worktree> --commit`, a commit on `trio/<slug>`), resume with
  `--no-acceptance` (as it started; the flag holds for the whole run even
  when the env or the profile says on, see "Switch"), or `trioctl omnigent abandon --mailbox
  <mb>` (keeps the branch: take what you need, then `git branch -D
  trio/<slug>`), refresh the target and start again.

## Claude-native seams (N1–N4, later task)

Native v0.1 does not implement them. Until it does, `native/launch.sh`
(start and resume) prints a loud stderr warning and records
`"acceptance": "unsupported-in-native-v01"` (plus `acceptance_detected`) in
`<mailbox>/.native-result.json` whenever the mailbox has `acceptance/FROZEN`
or the switch resolves ON (`TRIO_ACCEPTANCE`, else the profile's
`[acceptance] enabled`); the run is otherwise unchanged, so it has no frozen
SHIP gate, pre-runs or amendments (trio-shadow's pack guard still blocks
tampering). Use `trioctl omnigent loop --acceptance` for a gated run.

- `MODELS.acceptance = MODELS.evaluator`, with a tier-equality refusal at
  `begin`.
- A `trio-acceptance` agent generated from `prompts/canonical/acceptance.md`
  via a `.claude` overlay target.
- Helper ops in `trio_native_step.py` reuse this build's functions:
  - `acceptance-export` -> `trio-acceptance.build_export`;
  - `acceptance-freeze` -> `AcceptanceController.freeze_from_export` plus
    `audit_transcript`;
  - `coverage` -> `trio-check.coverage_refusals`;
  - `acceptance-run` -> `run_pack`;
  - the `op_gate` pin check -> `AcceptanceController.check_pin`;
  - the `op_apply` SHIP gate and amendments ->
    `AcceptanceController.review_verdict`.
- `PLAN_SCHEMA` gains `covers`, `lead_integration` and
  `acceptance_bindings`.
- The Lead/Evaluator fragments `prompts/canonical/acceptance-lead.md` and
  `acceptance-evaluator.md` are the native `leadPlanPrompt`/`evaluatorPrompt`
  additions.

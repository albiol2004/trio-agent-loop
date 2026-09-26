# Isolated Concurrent Workers — r9 Live Qualification

## 1. Purpose

Worker isolation (`--isolate-workers`; default ON since r11, see
[Defaults](#6-defaults)) gives each concurrent role its own git worktree
instead of sharing the driver's checkout:

- **Per-builder worktrees**: each Lead-dispatched builder runs in its own
  worktree/branch (`trio-worker/...`), so concurrent builders never race.
- **Per-slice evaluator worktrees**: an open-loop slice evaluator gets its own
  worktree, separate from builders/integration evaluator.
- **Acceptance-bound retirement**: a worktree is removed only once accepted
  by the SHIP that binds it (`accepted_by.attempt`/`evaluated` must match).
- **Root `.cursor` restore**: the driver root's `.cursor/mcp.json` and
  `.cursor/hooks.json` are swapped out for the run and restored byte-for-byte
  (hash + mode) afterward, with no leftover empty `.cursor` or store entries.

"Qualified" means a live run passes all 12 independent, read-only gates in
`<lab>/live-qual/r7-live/verify_r7.py`: G1 candidate pin unmodified, G2
vendored metrics pinned to the r7 fixture base, G3 loop exit 0, G4 real bound
evaluator acceptance + clean retirement, G5 builder/eval worktrees
auto-removed (no leftover branches/dirs), G6 observed concurrent builder
overlap (correct model), G7 live D1 (delayed eval-worktree teardown after
owner exit — may be NOT_COVERED), G8 no runners/sessions left at the broker,
G9 root `.cursor` restored exactly, G10 monitor bounds/no violations, G11 no
secrets in evidence, G12 one mailbox per root. Any FAIL fails the run;
G6/G7 may be NOT_COVERED without failing it.

## 2. Three defects fixed on top of a241b8b

### a01cddb — mojibake held the first-prompt receipt

Symptom (run `20260926T114109Z`, session `3f5a37b6`): the Evaluator prompt
was delivered and ran to completion, but the mirrored user row held one EM
DASH as two U+FFFD, so the exact `want in rows` check raised
`PromptDeliveryUncertain`. Root cause: `ensure_first_prompt` required
byte-exact row matches, but broker-side mirroring occasionally reencodes one
non-ASCII character as a short run of U+FFFD. Fix:
`omnigent/broker_http.py::_row_matches_prompt` now accepts a bounded rule —
a maximal run of 1–3 U+FFFD may stand for exactly one non-ASCII (>= 0x80)
prompt character at that position; no truncation, no DEL prefix, no U+FFFD
for ASCII, and a prompt itself containing U+FFFD must match exactly.
**Commit `a01cddb`** — `omnigent/broker_http.py`,
`.../fixtures/live_3f5a37b6_held_user_row.txt`,
`omnigent/tests/test_prompt_mojibake.py` (314 lines added).

### 288a711 — integration verdict rejected by slice-section scan

Symptom (run `20260926T131520Z`): a correct bound SHIP (iteration fields
`['1','0','1']`) never became "ready"; the loop timed out and held the
session as `role_completion_uncertain`. Root cause: open-loop slice-evals
append `## slice <id> @<sha> — SHIP|ITERATE` sections (own
`iteration:`/`evaluated:` lines) to the same `VERDICT.md` as the integration
verdict, and `_role_artifact_ready` scanned those fields too. Fix: new
`_integration_verdict_text` (in `omnigent/trioctl` and
`metrics/trio_loop.py`) drops each slice section up to the next non-slice
`# `/`## ` heading before field checks; applied at `_role_artifact_ready`
(freshness still compares full text), `_fresh_evaluator_artifact`,
`_verdict_binds_lockstep`, and `_ship_retirement_problem_once`. Slice-eval
acceptance itself is unchanged. **Commit `288a711`** — `metrics/trio_loop.py`,
`omnigent/trioctl`, `omnigent/tests/test_verdict_scope.py`,
`.../fixtures/live_20260926T131520Z_VERDICT.md` (328 lines added).

### 33c9836 — builders dispatched with the wrong config profile

Symptom (run `20260926T131520Z`): the Lead dispatched builders with a
different `TRIOCTL_CONFIG` (low effort) than the driver's, so builders ran
low effort while the driver's profile said medium. Root cause: nothing
propagated the driver's resolved config path to worker dispatch; each side
resolved its own config independently. Fix: `OmnigentRunner` resolves its
profile path once and exposes `driver_meta = {"config_path": <abs path>}`;
the loop core merges it into `.driver.json` (core keys never overridden).
`trioctl omnigent run --mailbox M` without `--config` now prefers
`M/.driver.json`'s `config_path`, printing `trioctl: using driver config
<path>` when it differs from env/default; a named-but-missing file warns
and falls back; explicit `--config` always wins. The isolated builder path
inherits the pin via `command_run`. **Commit `33c9836`** —
`metrics/trio_loop.py`, `omnigent/trioctl`,
`omnigent/tests/test_driver_config_pin.py`,
`omnigent/tests/test_observe_workers.py` (312 lines added).

## 3. Live evidence table

| Run (UTC) | Round | Result | Notable gate failure |
|---|---|---|---|
| `20260926T114109Z` | r7 | NOT QUALIFIED | mojibake held first-prompt receipt (G1/G3/G4/G5/G8/G9 FAIL — pre-a01cddb) |
| `20260926T131520Z` | r8 | NOT QUALIFIED | verdict-scope scan (G3/G4/G5) + low-effort builders (G6) |
| `20260926T142519Z` | r9 | QUALIFIED | none (G7 NOT_COVERED) |

Each row's `evidence/VERIFY.txt` is authoritative. **r9 detail**
(`<lab>/live-qual/20260926T142519Z/evidence/`): 2 builders overlapped
concurrently, both `cursor-grok-4.6-medium` (G6: 26 overlap polls, peak 2).
Bound SHIP: attempt `99d09445271042afadc366f9a8539fca`, evaluated
`37c03f0102b3122e930a120b69496667efa6ff30`, retirement commit
`d8d69f95ab5dbaf25aafa2b472005b464ee8cbc9` (`loop: iteration 1 — SHIP`,
touching only `loop-qual/`). 4 worktrees auto-removed: builders
`num-utils-d5b8a2a0`, `text-utils-c4ab9c36`; evaluators
`eval-num-utils-12092ac4`, `eval-text-utils-3aabe2ed` (G5 PASS, no leftover
branches/dirs). Root `.cursor` restored exactly (G9 PASS). Wall clock
`started.txt`→`finished.txt`: 14:33:38Z–14:43:23Z (~9m45s). An earlier
`VERIFY.first-pass.txt` failed G8 only because the verifier's process scan
counted the launcher's own shell; `verify_r7.py` now excludes ancestor pids
(`_ancestor_pids`), and the corrected pass is QUALIFIED.

## 4. Known limits / follow-ups (from the r9 review, non-blocking)

- Config pin (33c9836) applies only to `trioctl omnigent run --mailbox` on
  the CLI, not via an environment variable; a relative `--mailbox` from a
  subdirectory silently skips the pin lookup, and a stale `.driver.json`
  from a previous run can pin a later, unrelated manual run.
- G7 (delayed evaluator-worktree retirement after owner exit) remains
  unproven live — every run so far reports NOT_COVERED.
- Multiple mailboxes on one root still need manual retirement; prompt
  templates carry many non-ASCII chars (em dashes/arrows) that can hit the
  bounded mojibake path in `_row_matches_prompt`.

## 5. Status

Candidate `33c9836` is lab-qualified (r9, `20260926T142519Z`) and **not
installed** — the installed `CURRENT` remains `8a19a8cc`.

## 6. Defaults

Since r11 (branch `r11-defaults-on`) isolation is no longer opt-in: a plain
`trioctl omnigent loop --mailbox <m>` isolates workers and runs up to 4
slice-evals concurrently (`docs/CONCURRENT-SLICE-EVAL.md`).

| setting | default | disable / override |
|---|---|---|
| worker isolation | ON | `--no-isolate-workers` (`--isolate-workers` is an accepted no-op) |
| worktree root | `$TRIO_WORKTREE_ROOT`, else `$XDG_STATE_HOME` or `~/.local/state` + `/trio-agent-loop/worktrees/<repo>-<sha256(git common dir)[:12]>` | `--worktree-root DIR` |
| slice-eval concurrency | 4 | `--slice-eval-concurrency 1` |

- The root is resolved once at loop start, printed
  (`trioctl: worktree root <path>`), pinned into the builder dispatch
  command, and created with the first worktree. It is always outside the
  repository (`worker_worktrees.create` refuses a root inside it). The
  ledger stays in `<git common dir>/trio-worktrees/`, and the per-worktree
  `.cursor` handling is relative to each worktree, so neither depends on
  where the root is.
- Isolation is implemented in trioctl only; it works with any loop core
  that trioctl accepts (qualified above with 3b5b93b's).
- Default fallbacks (one stderr line, pre-r11 non-isolated run, serial
  slice-evals): `worker_worktrees.py` missing, not a git checkout on a
  branch, `--observe-workers` (both prescribe the worker command), or
  session-bound Omnigent bindings in the user's/system Cursor config. With
  an explicit `--isolate-workers` each of these is a refusal, as before.
- An explicit `--slice-eval-concurrency N>1` without isolation is refused.

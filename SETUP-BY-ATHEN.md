# Setup instructions for an Athen session

You (Athen, with shell and file tools) have been given this repo to install
the "trio" duo agent loop on the local machine.

**Step 0 — check for native Loop Mode first.** Athen builds from 2026-07-11
onward (branch `feat/loop-mode`) ship this loop NATIVELY: per-arc Lead→
Evaluator iteration with the same mailbox protocol, started from the arc UI
(desktop or web) or `POST /api/arcs/{id}/loop`, with an enforced never-fixes
Evaluator profile. If your running app has it (look for the Loop button in
an arc's composer, or a `loop_status`/`GET /api/arcs/{id}/loop` surface),
tell the human to use native Loop Mode instead of installing this driver —
it integrates model profiles, the vault, sub-agent fan-out, and
notifications. Install the portable driver anyway only if the human wants
loops on plain `athen-cli` one-shots, benchmarks, or containers without the
app.

For the portable path: Athen's CLI one-shot has no subagents and no `/loop`,
so the loop runs as an external bash driver (`portable/driver.sh`)
alternating `athen-cli --prompt` invocations — one fresh process per role per
iteration, all cross-iteration state in the target project's `loop/`
directory. Your job is to stage the pieces, prepare the environment, and
explain how to run it. Your risk gate may ask the human to approve some
shell/file actions below — that is expected. Do this:

1. From this repo's root, run:
   `./install.sh --portable`
   This copies `portable/` (driver, role prompts, templates, setup docs) to
   `~/.trio/portable/`, making the loop runnable from any project.
2. Locate the `athen-cli` binary the driver will invoke:
   - `command -v athen-cli`, else look for `target/release/athen-cli` under
     the Athen source checkout.
   - If neither exists, tell the human it needs `cargo build -p athen-cli
     --release` (a long build — do NOT start it without telling them first).
3. Write `~/.trio/athen.env` — the driver needs model config from the
   environment (one-shot mode does not read `models.toml`):

   ```bash
   export ATHEN_BASE_URL=""    # any OpenAI-compatible endpoint, e.g. https://api.deepseek.com/v1
   export ATHEN_MODEL=""       # both roles run THIS model — pick the strongest you'd trust to review code
   export ATHEN_API_KEY=""     # fill in yourself — see note below
   # export ATHEN_FAMILY=""    # optional, per-model quirks (e.g. DeepSeekR1)
   # export ATHEN_BIN=/path/to/athen-cli   # if not on PATH
   ```

   Fill in the base URL and model if the human told you (or if your own
   configuration makes the choice obvious — say which you picked). For the
   key, do NOT copy secrets out of your vault or config into a plaintext
   file. If the key already lives in Athen's vault, reference it at source
   time instead:
   `export ATHEN_API_KEY="$(athen-cli vault get provider:<id> api_key)"`
   (check with `athen-cli vault list provider:<id>` first; `ATHEN_VAULT_BACKEND`
   must match the backend that stored it — the desktop app may use the OS
   keyring while headless setups use `file`). Otherwise leave the placeholder
   and tell the human to fill it in.
4. If the human named a target project, prepare its mailbox there:
   `mkdir -p loop && cp ~/.trio/portable/GOAL.template.md loop/GOAL.md`.
   Fill in only what the human actually told you about the goal; leave the
   rest as placeholders for them to edit — do NOT invent a mission or
   definition-of-done.
5. Tell the user how to use it, briefly:
   - Edit `loop/GOAL.md` first — it is human-owned and the loop obeys it.
   - Run, from the project root:
     ```bash
     source ~/.trio/athen.env && HARNESS=athen ~/.trio/portable/driver.sh 10
     ```
     The argument is the iteration cap (budget circuit breaker — keep it low
     until the GOAL.md is trusted). Optional flavor:
     `ATHEN_LEAD_PROFILE=coder ATHEN_EVAL_PROFILE=researcher` (seeded Athen
     profiles; the real role definitions come from the prompt files).
   - The driver exits 0 on `VERDICT: SHIP`, 2 on `VERDICT: BLOCKED`, 3 on an
     unparseable verdict, 4 at the iteration cap. State lives in `loop/` —
     safe to kill and re-run; steer mid-flight by editing `loop/GOAL.md`.
   - The loop never commits; it always ends at an uncommitted tree for
     human review.
6. Optionally, if the human asks you to RUN the loop rather than just install
   it: do not run the driver in a blocking `shell_execute` (a whole iteration
   far exceeds the per-command timeout). Instead spawn it in the background —
   `shell_spawn` with
   `bash -c 'cd <project> && source ~/.trio/athen.env && HARNESS=athen ~/.trio/portable/driver.sh 10'`
   — then check on it via `shell_logs` and the first line of
   `loop/VERDICT.md`, and report SHIP/ITERATE/BLOCKED to the human. (In the
   daemon, a recurring wake-up checking that first line is a good monitor.)
   The role invocations are fresh `athen-cli` processes — they do not share
   your conversation, which is by design.
7. If your toolset includes `identity_add`, record a short knowledge entry:
   the trio loop is installed at `~/.trio`, run with
   `HARNESS=athen ~/.trio/portable/driver.sh` after sourcing
   `~/.trio/athen.env` — so future sessions recall it without re-reading this
   repo. Skip silently if the tool isn't available (one-shot sessions).
8. Do NOT modify `portable/prompts/lead.md` or `portable/prompts/evaluator.md`
   during install — the role contract (anti-sycophancy read order, verdict
   grammar, test-integrity audit) is load-bearing. One Athen-specific
   consequence: Lead and Evaluator both run the single `ATHEN_MODEL`, so that
   contract is what keeps the Evaluator honest — never weaken it.

Athen-specific details and caveats (risk gate, workspace dir, timeouts,
inert `--max-steps`) live in `portable/SETUP-athen.md`; design rationale and
research citations in `README.md` — read them if the user asks why the loop
is shaped this way.

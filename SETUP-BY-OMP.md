# Setup instructions for an Oh My Pi (Omp) session

This repository ships an Omp-native bundle that installs as user-level agents
and slash commands. It uses the same `loop/` mailbox protocol as the other
native bundles.

## 1. Install Omp and authenticate

Install Omp and sign in using the method in the official Omp documentation.
Do not run an installer or authenticate from this setup guide automatically.

Verify that the `omp` CLI is on PATH and can report its active agent
configuration path:

```bash
omp config path
```

If `omp` is not on PATH, the installer falls back to the directory in
`$PI_CODING_AGENT_DIR` or `~/.omp/agent`.

## 2. Install the native Trio bundle

From a fresh clone of this repo, install the Omp bundle:

```bash
./install.sh --omp
```

This creates only Trio-owned files under the resolved Omp agent directory:
`agents/trio-{lead,evaluator,scout,builder}.md` and
`commands/{trio,trio-init}.md`. Existing files are preserved, and no
unrelated harness directories are created.

The bundle ships with a default model contract set in role frontmatter:

- **Judgment tier** (`trio-lead`, `trio-evaluator`): `kimi-code/kimi-for-coding`
- **Worker tier** (`trio-scout`, `trio-builder`): `deepseek/deepseek-v4-flash`

Keep those defaults unless the user asks for different models. Only then
install with explicit overrides — both flags together, never just one:

```bash
./install.sh --omp \
  --strong-model '<provider/strong-model>' \
  --light-model '<provider/light-model>'
```

An agent performing this setup must not choose, infer, or substitute
provider/models on its own; the pinned defaults above are the contract.

The overrides land in `task.agentModelOverrides` and can also be changed later
without editing the role files:

```bash
omp config set task.agentModelOverrides '{"trio-lead":"provider/strong-model","trio-evaluator":"provider/strong-model","trio-scout":"provider/light-model","trio-builder":"provider/light-model"}'
```

`task.agentModelOverrides` has precedence over frontmatter `model:` fields, so
changing the override record is the supported way to retarget the loop.

Frontmatter `model:` accepts a prioritized list whose later entries are
per-spawn fallbacks (for example
`model: [kimi-code/kimi-for-coding, kimi-code/kimi]`). The bundle ships
single pins by default — adding fallbacks is a supported local edit, unlike
the fixed role-prompt contract.

## 3. Verify the installation

Confirm the files exist under the resolved Omp agent directory:

```bash
ls "$(omp config path)/agents"/trio-{lead,evaluator,scout,builder}.md
ls "$(omp config path)/commands"/{trio,trio-init}.md
```

Check that `trio-lead.md` and `trio-evaluator.md` contain `blocking: true` in
their frontmatter, and that the default model pins match the contract above:

```bash
grep -E '^model:' "$(omp config path)/agents"/trio-{lead,evaluator,scout,builder}.md
```

Optionally run the repo-side smoke test:

```bash
./omp/smoke-test.sh --installed
```

New Omp sessions discover the agents and commands automatically; the current
session may need a restart to see newly installed commands.

## 4. Use the loop

From any project:

```text
/trio-init add rate limiting to the public API, config-driven, no new deps
/trio          # run ONE iteration supervised — sanity-check the loop first
/trio auto     # autonomous; iterates until VERDICT.md first line is SHIP or BLOCKED
```

- `/trio-init <goal>` creates the `loop/` mailbox (`GOAL.md`, `STATE.md`,
  `PLAN.md`, `REPORT.md`, `VERDICT.md`, `LOG.md`). `GOAL.md` carries
  `profile: software | data` — the data profile switches the Evaluator to
  data-ground-truth checks (reconciliation, integrity, reproducibility).
- `/trio` runs one supervised iteration.
- `/trio auto` runs iterations in the same turn until the Evaluator's
  `loop/VERDICT.md` first line reads `SHIP` or `BLOCKED`. Press **Esc** or
  interrupt to pause it yourself.
- Concurrent loops: use a distinct mailbox directory with
  `/trio-init dir=loop-<name> <goal>` then `/trio dir=loop-<name>`.
- `max_iterations` in `loop/STATE.md` (default 10) is the hard budget cap.
- `/pause` parks the main agent AND all role subagents at safe boundaries —
  the clean way to pause a `/trio auto` run mid-iteration (Esc/interrupt also
  works).
- Cost knobs: `task.maxEffort` ceilings subagent effort and
  `thinkingBudgets.*` sizes thinking levels without touching the bundle.
- You can steer role agents live through the Agent Hub (Alt+A).

## 5. Headless / CI runs

For non-interactive or CI runs, start the loop headless:

```text
omp -p "/trio auto"
```

Use an overlay `--config` YAML to make the run repeatable and cheaper:

```yaml
tools:
  approvalMode: yolo
task:
  maxEffort: med
```

`tools.approvalMode: yolo` auto-approves tool calls, so run CI in a
disposable checkout. `max_iterations` in `loop/STATE.md` still caps the
budget.

## 6. Do NOT modify the role files

The role contract is fixed in the agent frontmatter and prompt bodies:
`trio-lead` plans, delegates, and reviews; `trio-builder` performs the primary
implementation pass; `trio-evaluator` verifies adversarially and writes
`loop/VERDICT.md`; `trio-scout` answers recon questions for either lead role.

Do not edit those role files during install or during a loop. The
Lead-plans/Builder-implements/Evaluator-verifies separation of powers is the
core contract. If you need different models, use the `task.agentModelOverrides`
config path described above instead of editing frontmatter.

## 7. Troubleshooting

- **Unknown agent errors** in a running session usually mean the agents were
  installed after the session started. Restart the Omp session.
- **"Subagent not found" or recursion errors**: ensure
  `task.maxRecursionDepth` is at least `2`. The default chain is main
  session → `trio-lead` (depth 1) → `trio-builder`/`trio-scout` (depth 2).
- **Model not used as expected**: check `omp config get task.agentModelOverrides
  --json`. If it is set, it overrides frontmatter. Clear or update it with
  `omp config set`.

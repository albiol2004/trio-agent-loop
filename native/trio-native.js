export const meta = {
  name: 'trio-native',
  description: 'Claude-native lockstep Trio loop: Lead plan, driver-owned isolated builder waves, Lead integrate, commit gate, pinned Evaluator, verdict; decisions by trio_native_step.py',
  whenToUse: 'Run a lockstep Trio loop on an initialized mailbox without Omnigent. args: {mailbox, max_iterations, [max_agents], [token_budget], [helper: absolute path of trio_native_step.py], [run_token: [A-Za-z0-9._-]{1,64}], [models: {lead, evaluator, builder, repair, step, acceptance}], [acceptance (r19 frozen acceptance; true enables it, default off)]}; all are in-schema. Launch with --settings {"worktree":{"baseRef":"head"}}.',
  phases: [
    { title: 'Begin', detail: 'lock the mailbox, exclude .claude/worktrees/, read STATE' },
    { title: 'Iterate', detail: 'next, Lead plan, builder waves (worktrees), Lead integrate, cleanup, gate, pin, Evaluator, apply' },
    { title: 'Finish', detail: 'remove Evaluator pin worktrees, release the lock, report the outcome' },
  ],
}

// ---------------------------------------------------------------- config
// The script only sequences. Every loop decision (stop conditions, gate,
// repair routing, pin/attempt, SHIP retirement, builder verification) is
// made by the helper native/trio_native_step.py over metrics/trio_loop.py,
// run by a Bash-only `trio-step` agent that returns the helper's raw stdout;
// the script parses it and checks the nonce and the op's required keys.
//
// Workflow subagents have no Agent tool (live probe P4), so the driver owns
// the builders: the Lead plans slices through a schema, the script runs one
// `trio-builder` per slice with `isolation: 'worktree'` (concurrently only
// for disjoint `writes:`), and a Lead integrate call merges and reviews.
const A = (args && typeof args === 'object') ? args : {}
if (typeof A.mailbox !== 'string' || !A.mailbox.startsWith('/')) {
  throw new Error('trio-native: args.mailbox must be an absolute mailbox path')
}
// Printable paths only (metrics/native_args.py's rule, as launch.sh checks):
// spaces, non-ASCII letters and punctuation are fine, control / format /
// line-separator characters never reach a prompt.
if (/(?! )[\p{C}\p{Z}]/u.test(A.mailbox)) {
  throw new Error('trio-native: args.mailbox has a control, format or line-separator character')
}
const MAILBOX = A.mailbox.replace(/\/+$/, '')
// The mailbox as it appears in prompts: unchanged when it only uses
// [A-Za-z0-9._/+@-], else shell single-quoted (one inert token in backticks
// and in any command a prompt spells out; eval3 finding 7).
const MAILBOX_Q = promptPath(MAILBOX)
const MAX_ITERATIONS = Number.isInteger(A.max_iterations) && A.max_iterations > 0 ? A.max_iterations : 4
// Opt-in caps only (user decision): no agent cap and no usage budget unless
// passed. max_iterations stays the loop's normal bound.
const MAX_AGENTS = Number.isInteger(A.max_agents) && A.max_agents > 0 ? A.max_agents : null
const TOKEN_BUDGET = typeof A.token_budget === 'number' && A.token_budget > 0 ? A.token_budget : null
const TOKEN = typeof A.run_token === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(A.run_token)
  ? A.run_token
  : 'trio-native-' + MAILBOX.split('/').filter(Boolean).slice(-2).join('-').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 48)
const MODELS = Object.assign({
  lead: 'opus',
  evaluator: 'opus',
  builder: 'sonnet',
  repair: 'sonnet',
  step: 'sonnet',
}, (A.models && typeof A.models === 'object') ? A.models : {})
// r19 N1: frozen acceptance behind args.acceptance (default off; with it off
// every agent() call, prompt and result is what it was before r19). The
// author runs on the Lead/Evaluator tier, never cheaper: MODELS.acceptance
// is the evaluator's model (after args.models overrides), and `begin`
// refuses an explicit args.models.acceptance, or a Lead, on another tier.
const ACCEPTANCE = A.acceptance === true
if (typeof MODELS.acceptance !== 'string' || !MODELS.acceptance) MODELS.acceptance = MODELS.evaluator
// Default helper: the installed release's copy (CURRENT names the release).
const HELPER = typeof A.helper === 'string' && A.helper.startsWith('/')
  ? shq(A.helper)
  : '"$HOME/.local/share/trio-agent-loop/releases/$(cat "$HOME/.local/share/trio-agent-loop/CURRENT")/native/trio_native_step.py"'
const RESERVE = 1  // with max_agents: always keep one agent for the `end` step (lock release)
const BASE_REF_HINT = 'launch Claude Code with --settings \'{"worktree":{"baseRef":"head"}}\' so isolated builders fork from the Lead\'s HEAD'

// The step agent returns the helper's stdout as one string; the script
// parses it (the probe saw structured copies drop keys in 5 of 18 steps).
const STEP_SCHEMA = {
  type: 'object',
  properties: {
    exit_code: { type: 'integer' },
    stdout: { type: 'string' },
    held: { type: 'boolean' },
    denial: { type: 'string' },
  },
  required: ['exit_code', 'stdout'],
}

// Keys an `ok: true` helper result must carry, per op. A result missing one
// is treated like a nonce mismatch: the step is re-run once.
const REQUIRED = {
  begin: ['repo', 'iteration', 'status', 'phase', 'lock_owner', 'exec_id'],
  next: ['action', 'iteration'],
  dispatch: ['head', 'wave'],
  builders: ['accepted', 'refused', 'merge'],
  cleanup: ['removed', 'kept', 'dropped'],
  gate: ['pass', 'failures', 'final', 'status'],
  pin: ['sha', 'evaluator_attempt', 'context_block', 'skip_evaluator'],
  apply: ['verdict', 'stop', 'status', 'code', 'commit_shas', 'bound'],
  end: ['lock', 'dangling_worktrees'],
  'acceptance-export': ['frozen'],
  'acceptance-freeze': ['action'],
  coverage: ['covered_ok', 'refusals', 'briefs'],
  'acceptance-run': ['text', 'passed', 'failed', 'unavailable', 'total'],
}

// A harness permission denial is recognised by the harness's own wording
// ("Permission to use Bash … has been denied", "Permission for this action
// was denied by the Claude Code auto mode classifier", "… requested
// permissions to use X, but you haven't granted it yet"); anything else — a
// step agent declining in its own words, even ones like "not allowed",
// "permission" or "denied" — is an error, not a hold (eval-native-v0b N1).
// The gap between the anchor words and the verdict is bounded (eval-native-v0c
// C6): the previous lazy, unbounded `[\s\S]*?` could match a self-refusal
// that quotes unrelated harness wording across an arbitrarily long stretch of
// its own reasoning (e.g. "Permission to use X … [500 words later] … was
// denied to someone else"), calling it `held`. 200 chars covers the harness's
// own short forms with room to spare.
const DENIAL_RE = /Permission to use \w+[^\n]{0,200}(?:has been|was) denied|Permission for this action (?:has been|was) denied|denied by the Claude Code auto mode classifier|requested permissions? to use \w+[^\n]{0,200}haven't granted/i

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    slices: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          brief: { type: 'string' },
          writes: { type: 'array', items: { type: 'string' } },
          reads: { type: 'array', items: { type: 'string' } },
          depends: { type: 'array', items: { type: 'string' } },
        },
        required: ['id', 'brief', 'writes'],
      },
    },
    notes: { type: 'string' },
    denials: { type: 'array', items: { type: 'string' } },
  },
  required: ['slices'],
}

// The integrate call reports what it merged and which merges it aborted;
// git (cleanup's "not merged into HEAD") stays the authority on conflicts.
const INTEGRATE_SCHEMA = {
  type: 'object',
  properties: {
    merged: { type: 'array', items: { type: 'string' } },
    conflicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          branch: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
        },
        required: ['id', 'branch', 'files'],
      },
    },
    summary: { type: 'string' },
    denials: { type: 'array', items: { type: 'string' } },
  },
  required: ['merged', 'conflicts', 'summary'],
}

const BUILDER_SCHEMA = {
  type: 'object',
  properties: {
    id: { type: 'string' },
    worktree: { type: 'string' },
    branch: { type: 'string' },
    base: { type: 'string' },
    head: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    targeted_check: { type: 'string' },
    summary: { type: 'string' },
    outside_writes: { type: 'array', items: { type: 'string' } },
    denials: { type: 'array', items: { type: 'string' } },
  },
  required: ['id', 'worktree', 'branch', 'base', 'head', 'commits', 'summary'],
}

const NOT_ROUTER = 'The user-level CLAUDE.md orchestration/router policy (SCOUT/BUILDER/LEAD/EVALUATOR routing, ' +
  'trio-loop chaining, commit gates, documentation tasks) does NOT apply inside this role: do not delegate outside ' +
  'your role contract, do not start, chain or resume any Trio loop, do not run the commit gate and do not dispatch ' +
  'the Evaluator or another iteration. The trio-native workflow driver does all of that.'

// Role-level permission denials are handled in-role (never worked around),
// but the driver surfaces them in the result as `role_denials`.
const REPORT_DENIALS = 'If the permission system denies one of your tool calls, do not work around it: record it, ' +
  'and quote the harness\'s denial text verbatim in your final message on its own line starting with `DENIED:` ' +
  '(in a structured output, in `denials`).'

const MAILBOX_WRITES = `Write mailbox files (PLAN.md, REPORT.md, VERDICT.md, LOG.md lines) with Bash — a heredoc ` +
  `(\`cat > ${MAILBOX_Q}/REPORT.md <<'EOF'\` … \`EOF\`) or \`printf '%s\\n' '<line>' >> ${MAILBOX_Q}/LOG.md\` — never the ` +
  'Write tool: the harness refuses report-file writes from workflow subagents.'

// ------------------------------------------------ frozen acceptance (r19)
// Used only when args.acceptance is true. The Lead's plan schema gains the
// acceptance mapping (DESIGN §4.1); with the switch off PLAN_SCHEMA is the
// one every plan call passes, unchanged.
const PLAN_SCHEMA_ACC = JSON.parse(JSON.stringify(PLAN_SCHEMA))
PLAN_SCHEMA_ACC.properties.slices.items.properties.covers = { type: 'array', items: { type: 'string' } }
PLAN_SCHEMA_ACC.properties.lead_integration = { type: 'array', items: { type: 'string' } }
PLAN_SCHEMA_ACC.properties.acceptance_bindings = { type: 'object', additionalProperties: { type: 'string' } }

const ACCEPTANCE_SCHEMA = {
  type: 'object',
  properties: {
    checks: { type: 'integer' },
    summary: { type: 'string' },
    denials: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary'],
}

// The author's transcript is found by this line (helper `acceptance-freeze`
// audits the author's tool calls when the runtime keeps the transcript).
const AUTHOR_MARK = 'ACCEPTANCE-AUTHOR-RUN:'
// A long helper op (pack runs) answers `pending` after ~8 min; polls beyond
// this many stop the run.
const MAX_POLLS = 12

// The Lead / Evaluator additions: generated by prompts/generate.py from
// prompts/canonical/acceptance-native-{lead,evaluator}.md ({mailbox} and
// {tool} are filled in below). Do not edit between the markers.
// trio-native-acceptance:start
const ACC_LEAD_FRAGMENT = "FROZEN ACCEPTANCE (r19; this loop runs with args.acceptance on) -- extends\nyour plan call. The driver already froze `{mailbox}/acceptance/`: independent\nblack-box checks written from GOAL.md alone, before any plan existed.\n0. Read `{mailbox}/acceptance/MANIFEST.json` and `AUTHOR.md`. You may read\n   the check scripts; NEVER edit, add or delete anything under\n   `{mailbox}/acceptance/` (the driver restores it and counts a gate\n   breach; a second breach in the loop stops it). Never write an\n   `acceptance: amend` commit: only the Evaluator (and a human, while the\n   loop is stopped) amends checks.\n1. Map every `behaviour`/`doc` check id, in BOTH places:\n   - PLAN.md: in the `covers:` list of the slice(s) that make it pass\n     (`covers: [ACC-03, ACC-09]` inside the slice entry of the `slices:`\n     block), or in `lead_integration:` under `## Verification standard`\n     (checks you satisfy yourself when you integrate). A check that crosses\n     slices goes into the covers of every slice it needs, or into\n     `lead_integration:`. `acceptance_bindings: {NAME: value}` under\n     `## Verification standard` sets a manifest-declared binding when your\n     plan uses another name.\n   - Your structured output: each slice's `covers`, and the top-level\n     `lead_integration` and `acceptance_bindings`, with the same ids.\n   The driver refuses the plan before any builder runs while any id is\n   unmapped (in either place), unknown, or a binding is undeclared; you\n   then get one re-plan with the refusal, and a second refusal stops the\n   loop.\n2. The frozen checks are the GOAL's floor: PLAN may be stricter, never\n   looser. If a PLAN contract or accept contradicts a check, change the\n   PLAN, not the check. A check you believe is wrong gets a line in\n   REPORT.md -- `ACCEPTANCE-DISPUTE: ACC-07 — <why; quote the GOAL>` --\n   and is still mapped; the Evaluator adjudicates it.\n3. The driver appends `## Acceptance (frozen; do not edit)` to every\n   builder brief, with the slice's covered ids, their `goal_quote` and the\n   command that runs them; you need not copy them. A covered check that\n   FAILs only because a sibling slice has not landed is fine; a FAIL on\n   the builder's own surface means the slice is not done.\n4. The frozen pack replaces `goal_acceptance:` (do not write one);\n   `goal_probe:` is optional.\n5. When you integrate, run\n   `python3 {tool} run --mailbox {mailbox} --tree \"$(git rev-parse --show-toplevel)\"`\n   on the integrated tree and record the result in REPORT.md under\n   `## Frozen acceptance (Lead run)` -- a receipt; the Evaluator's run is\n   the authority, and the driver refuses any SHIP while a frozen check\n   fails.\n"
const ACC_EVALUATOR_FRAGMENT = "FROZEN ACCEPTANCE (r19; whole-goal verdict) -- the driver ran the frozen\npack at the pin (the FROZEN ACCEPTANCE block above). Its checks were\nwritten from GOAL.md alone by an independent author before any code\nexisted. In order:\n1. Re-run every non-PASS frozen check yourself at the pin:\n   `python3 {tool} run --mailbox {mailbox} --tree <your pinned worktree or\n   sha> --ids <ids>`, and quote the output (exclude flakes).\n2. A confirmed FAIL is ITERATE, `scope=local:<writes of the covering\n   slice(s)>` from PLAN.md `covers:` -- unless you amend that check (4).\n3. Adjudicate every `ACCEPTANCE-DISPUTE:` line in REPORT.md: uphold (the\n   check stands) or amend (4). Review PLAN.md `acceptance_bindings:` too:\n   each value the Lead set must be the surface the GOAL names (or an\n   equivalent the GOAL allows); a binding that points a check at a\n   weaker surface is an ITERATE on the PLAN, not a pass.\n4. Amend only a check whose defect you can name (over-specified, wrong\n   surface, flaky, contradicts GOAL): change only that check's own files\n   under `acceptance/checks/` (a file any other check can load -- named or\n   mentioned by it, anything in `fakes/` or `lib/`, an unowned helper --\n   needs every such check amended and counted; add new files only under\n   `acceptance/checks/<its id>/`), or its manifest `run`, `expect`,\n   `timeout_s`, `binds`, `needs` (never `id`, `goal_quote`, `kind`; never\n   remove a check; never point `run` at another check's file). The\n   amended check must still test its `goal_quote` on every tree, not\n   merely FAIL at base; append\n   `## ACC-NN · iter N · evaluator · <utc>` with `goal_quote:`,\n   `defect in check:` and `change:` lines to `acceptance/AMENDMENTS.md`;\n   commit only `{mailbox}/acceptance/` as\n   `acceptance: amend ACC-NN (evaluator, iter N): <reason>` (never label\n   it `(human)`: the running driver judges every amend commit as yours) --\n   a commit of its own, before any `loop: iteration N — ...` commit. Every\n   check that FAILed at base must still FAIL there (the driver re-runs the\n   whole pack at base and reverts the amendment if not). At most 2\n   amendments per loop and 25% of the checks; beyond that answer\n   NEEDS_HUMAN and list the checks under `## Human check`.\n5. Independent probe: aim your `## Independent probe` at the GOAL\n   sentences AUTHOR.md lists as not covered by a frozen check (drops,\n   live-only) and at any running surface (start the server or board and\n   probe it over HTTP).\n6. UNAVAILABLE frozen checks (exit 77 or unmet needs) are never an\n   ITERATE: with every other check passing the verdict is NEEDS_HUMAN,\n   listing them with exact commands under `## Human check`. With FAILs too,\n   ITERATE names the FAILs only.\n7. Keep your whole-tree suite and full-check re-run, and add to VERDICT.md:\n   ```\n   ## Frozen acceptance\n   acceptance: <p>/<t> PASS @<sha12> manifest <sha256[:12]> (driver run) · re-run: <ids and outcomes>\n   disputes: <ACC-NN upheld|amended — reason; or none>\n   amendments: <ACC-NN — reason; or none>\n   ```\nThe driver re-runs the pack at the evaluated sha after your verdict and\nrefuses a SHIP while any frozen check FAILs or is UNAVAILABLE.\n"
// trio-native-acceptance:end

// ------------------------------------------------------------- plumbing
let agentsUsed = 0
let seq = 0
// v01 fix: the number of agent() calls made so far. The harness names an
// isolated agent's worktree `<repo>/.claude/worktrees/<runId>-<n>` (branch
// `worktree-<runId>-<n>`), n = the 1-based number of that agent() call in
// the run (N0 vps-pool r1: builders 5..9 after begin, next, lead plan,
// dispatch; a journal resume replays the same calls, so the same n). The
// script passes each builder's n to `builders` as `agent_index`: the helper
// derives the builder's own worktree from git with it (the ownership
// ledger), never from the builder's report.
let agentCalls = 0

function callAgent(prompt, opts) {
  agentCalls += 1
  return agent(prompt, opts)
}
// The run-execution id `begin` mints (random, per script execution): every
// later step nonce carries it, so the helper's pin retry allowance
// (`native:{exec_id}:{nonce}@{iteration}`) never matches across runs.
let EXEC = ''

function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
}

function promptPath(s) {
  return /^[A-Za-z0-9._\/+@-]*$/.test(String(s)) ? String(s) : shq(s)
}

function repoShown() {
  return B.repo ? promptPath(B.repo) : '(git toplevel of the mailbox)'
}

function budgetStop(what) {
  const e = new Error(what)
  e.trioStatus = 'budget'
  return e
}

function spend(label, reserved) {
  if (MAX_AGENTS !== null) {
    const limit = reserved ? MAX_AGENTS : MAX_AGENTS - RESERVE
    if (agentsUsed >= limit) throw budgetStop(`max_agents=${MAX_AGENTS} reached before ${label}`)
  }
  if (TOKEN_BUDGET !== null && !reserved && typeof budget !== 'undefined' && budget.spent() >= TOKEN_BUDGET) {
    throw budgetStop(`token_budget=${TOKEN_BUDGET} spent before ${label}`)
  }
  agentsUsed += 1
}

// A step whose Bash call was denied by the permission system (auto-mode
// classifier) comes back `held`: the run stops and surfaces it, never loops.
function stepFail(label, r) {
  return r.held
    ? { status: 'held', reason: `${label} held: ${r.error}`, held_step: label }
    : { status: 'error', reason: `${label}: ${r.error}` }
}

function parseStdout(text) {
  const s = String(text === undefined || text === null ? '' : text).trim()
  if (s.startsWith('{')) return JSON.parse(s)
  const lines = s.split('\n').filter(l => l.trim().startsWith('{'))
  if (!lines.length) throw new Error('no JSON object in stdout')
  return JSON.parse(lines[lines.length - 1].trim())
}

// One step agent answer -> {kind: ok|held|error|retry, res|why}.
function readStep(op, nonce, r) {
  if (!r || typeof r !== 'object') return { kind: 'retry', why: 'no result' }
  if (r.held) {
    const denial = typeof r.denial === 'string' ? r.denial : ''
    if (DENIAL_RE.test(denial)) {
      return { kind: 'held', res: { ok: false, held: true, op, nonce, error: `permission denied: ${denial}` } }
    }
    return { kind: 'error', res: { ok: false, op, nonce,
      error: `step agent declined without a harness permission denial${denial ? ': ' + denial : ''}` } }
  }
  let res
  try {
    res = parseStdout(r.stdout)
  } catch (e) {
    return { kind: 'retry', why: `stdout is not the helper JSON (${String(e && e.message ? e.message : e)})` }
  }
  if (!res || typeof res !== 'object' || res.nonce !== nonce || res.op !== op) {
    return { kind: 'retry', why: `nonce mismatch (${res && res.nonce})` }
  }
  // A long acceptance op still running answers `pending` (the script polls).
  const need = res.ok === true ? (res.pending === true ? [] : (REQUIRED[op] || [])) : ['error']
  const missing = need.filter(k => !(k in res))
  if (missing.length) return { kind: 'retry', why: `result lacks ${missing.join(', ')}` }
  return { kind: 'ok', res }
}

async function step(op, extra, reserved) {
  seq += 1
  const nonce = EXEC ? `${TOKEN}/${EXEC}/${seq}/${op}` : `${TOKEN}/${seq}/${op}`
  const flags = [
    op,
    '--mailbox', shq(MAILBOX),
    '--token', shq(TOKEN),
    '--nonce', shq(nonce),
  ]
  for (const [k, v] of Object.entries(extra || {})) {
    flags.push('--' + k.replace(/_/g, '-'), shq(v))
  }
  const cmd = `python3 ${HELPER} ${flags.join(' ')} --json`
  const prompt = `Run this one trio-native step (op=${op}, nonce=${nonce}) and return its stdout verbatim.\n\n` +
    `COMMAND:\n${cmd}\nEND COMMAND\n`
  for (let tries = 1; tries <= 2; tries++) {
    spend(`step ${op}`, reserved)
    const r = await callAgent(prompt, {
      label: `step ${op}#${seq}` + (tries > 1 ? ' (retry)' : ''),
      agentType: 'trio-step',
      model: MODELS.step,
      effort: 'low',
      schema: STEP_SCHEMA,
    })
    const got = readStep(op, nonce, r)
    if (got.kind !== 'retry') return got.res
    // Helper ops are idempotent, so re-running a step is always safe.
    log(`step ${op}: ${got.why} on try ${tries}`)
  }
  return { ok: false, op, nonce, error: `step ${op} returned no matching result twice` }
}

// r19: the acceptance digest the helper returns (status, pin, pin commit,
// freeze commit, author base, counters). The script is the driver's memory
// across helper processes: it hands the digest back (`--acc`) on every
// acceptance op from `begin` on (`status: authoring` until the helper's own
// freeze) and the helper refuses a sealed record, state file or git pin chain
// that disagrees with it. eval-r19n: the digest never carries the checks'
// PATH (the helper re-derives it at every op), and only `begin` or the
// helper's freeze may turn it frozen; nothing turns it back. eval-r19n2: `seq`
// is the sealed record's sequence number; the helper refuses an older record.
let ACC = null
const ACC_KEYS = ['status', 'pin', 'pin_commit', 'freeze_commit', 'base', 'tamper_events', 'amendments', 'seq']
const accLog = { coverage_refusals: [], replanned: false, ship_refused: [], author_attempts: 0 }

function accTake(r, op) {
  const d = r && r.acceptance
  if (!d || typeof d !== 'object' || d.stop || typeof d.status !== 'string') return
  const frozen = d.status === 'frozen' && !!d.pin
  if (!accFrozen() && frozen && op !== 'begin' && op !== 'acceptance-freeze') return
  if (accFrozen() && !frozen) return
  ACC = d
}

function accFrozen() {
  return !!(ACC && ACC.status === 'frozen' && ACC.pin)
}

// Flags every acceptance-aware op gets (never with the switch off).
function accFlags() {
  if (!ACCEPTANCE) return {}
  const f = { acceptance: 1 }
  if (ACC) {
    const held = {}
    for (const k of ACC_KEYS) held[k] = ACC[k] === undefined ? null : ACC[k]
    f.acc = JSON.stringify(held)
  }
  return f
}

// An acceptance phase that cannot continue: the helper already set STATE
// (needs_human with the reason as phase, or error) and wrote the LOG line.
function accStop(r) {
  const s = r && r.acceptance && r.acceptance.stop
  if (!s) return null
  return { status: s.status || 'error', code: s.code === undefined ? null : s.code,
    reason: `acceptance ${s.reason}: ${String(s.detail || '').slice(0, 400)}` }
}

function accTool() {
  return ACC && typeof ACC.tool === 'string' && ACC.tool ? ACC.tool : 'metrics/trio-acceptance.py'
}

function accFragment(text) {
  return text.replace(/\{mailbox\}/g, MAILBOX_Q).replace(/\{tool\}/g, promptPath(accTool()))
}

// A long op (acceptance-freeze, acceptance-run, apply with acceptance)
// runs as a helper job; `pending` is polled with the same op.
async function stepLong(op, extra) {
  let r = null
  for (let poll = 0; poll <= MAX_POLLS; poll++) {
    r = await step(op, poll ? Object.assign({}, extra, { poll }) : extra)
    if (!r.ok || r.pending !== true) return r
    log(`step ${op}: still running (poll ${poll + 1} of ${MAX_POLLS})`)
  }
  return { ok: false, op, error: `${op} still running after ${MAX_POLLS} polls` }
}

// v01 fix: the run's TMPDIR, created and recorded by the helper at `begin`
// (`.claude/worktrees/tmp-<exec id>`); `end` removes only helper-created dirs.
function tmpNote() {
  if (!B || typeof B.tmpdir !== 'string' || !B.tmpdir) return []
  const t = promptPath(B.tmpdir)
  return ['Temporary files: use `' + t + '` (e.g. `export TMPDIR=' + t + '`) instead of /tmp or a new directory; do ' +
    'not create other directories under `.claude/worktrees/` — the driver removes only the directories it created ' +
    'itself, at the end of the run.']
}

function header(role, n) {
  return [
    `MAILBOX OVERRIDE: this run uses \`${MAILBOX_Q}/\` as the loop mailbox — every \`loop/\` path in the instructions below resolves to \`${MAILBOX_Q}/\`.`,
    '',
    `You are the trio-${role} for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX_Q}. Product repo: ${repoShown()}.`,
    NOT_ROUTER,
    REPORT_DENIALS,
  ].concat(tmpNote())
}

// The driver-verified human answer (helper `next` / `pin`, from trio-dash's
// answer ledger): present only when the mailbox has HUMAN.md and its newest
// ledger answer is current; the roles act on nothing else (eval2 finding 3).
function humanBlock(r) {
  for (const note of (r && Array.isArray(r.human_notes)) ? r.human_notes : []) log(`HUMAN.md: ${note}`)
  return (r && typeof r.human_answer === 'string' && r.human_answer) ? ['', r.human_answer.replace(/\n+$/, '')] : []
}

// v01 item 3: `begin` of a fresh run reused or cleaned up the builder
// worktrees an earlier run of this mailbox left behind.
function logReclaimed(r) {
  if (!r || typeof r !== 'object') return
  for (const x of r.merged || []) log(`previous-run builder ${x.branch}@${String(x.tip).slice(0, 12)} (${x.id}) merged into HEAD`)
  for (const x of r.removed || []) log(`previous-run builder ${x.branch} already merged: worktree removed`)
  for (const x of r.discarded || []) log(`previous-run builder ${x.branch}@${x.tip} discarded (${x.reason || 'not reusable'})`)
  for (const x of r.kept || []) log(`previous-run builder ${x.branch} kept: ${x.reason}`)
}

function reclaimedBlock(n) {
  const merged = (B.reclaimed && Array.isArray(B.reclaimed.merged)) ? B.reclaimed.merged : []
  if (!merged.length || n.iteration !== B.iteration) return []
  return ['', 'PREVIOUS RUN: the driver merged these committed builder branches of an earlier (stopped) run of this ' +
    'iteration into your HEAD (`git merge --no-ff`): ' + merged.map(x => `${x.id} (\`${x.branch}\` @ ${String(x.tip).slice(0, 12)})`).join(', ') +
    '. Review that work on HEAD and plan only what is still missing or wrong; do not re-slice work that is already there.']
}

// r19: the plan call's frozen-acceptance block (acceptance on only).
function accPlanLines(n, refusals) {
  if (!ACCEPTANCE) return []
  const lines = ['', accFragment(ACC_LEAD_FRAGMENT).replace(/\n+$/, ''),
    `Frozen pack: \`${MAILBOX_Q}/acceptance/\` (${ACC && ACC.checks !== undefined ? ACC.checks : '?'} checks, pin ` +
    `${ACC && ACC.pin ? String(ACC.pin).slice(0, 12) : '?'} @${ACC && ACC.pin_commit ? String(ACC.pin_commit).slice(0, 12) : '?'}).`]
  const errs = n && n.acceptance && Array.isArray(n.acceptance.errors) ? n.acceptance.errors : []
  if (errs.length) {
    lines.push('', 'ACCEPTANCE ERRORS FROM THE DRIVER (the last SHIP was refused by the frozen-acceptance gate):',
      ...errs.map(e => `- ${e}`))
  }
  if (refusals && refusals.length) {
    lines.push('', 'COVERAGE REFUSED (re-plan, attempt 2 of 2): the driver refused your plan before any builder ran:',
      ...refusals.map(r => `- ${r}`),
      'Map every listed id in BOTH PLAN.md and your structured output (`covers` of the slice(s) that make it pass, ' +
      'or `lead_integration`), declare only manifest bindings, and never edit acceptance/. A second refusal stops the loop.')
  }
  return lines
}

// Short reminders for the other Lead / repair calls (fresh agents).
function accPassLines(role) {
  if (!ACCEPTANCE) return []
  const never = `Never edit, add or delete anything under \`${MAILBOX_Q}/acceptance/\` (the driver restores it and ` +
    'counts a gate breach; a second breach stops the loop).'
  if (role === 'repair') return ['', 'FROZEN ACCEPTANCE: ' + never]
  return ['', 'FROZEN ACCEPTANCE: ' + never + ' Before you finish the pass, run ' +
    `\`python3 ${promptPath(accTool())} run --mailbox ${MAILBOX_Q} --tree "$(git rev-parse --show-toplevel)"\` on the ` +
    'integrated tree and record its summary in REPORT.md under `## Frozen acceptance (Lead run)` (a receipt; the ' +
    'driver refuses any SHIP while a frozen check fails). Keep every `covers:` / `lead_integration:` mapping in PLAN.md.']
}

function leadPlanPrompt(n, acc) {
  return header('lead', n).concat([
    '',
    'PLAN CALL (driver-owned builders). You have no Agent tool in this workflow: the driver spawns one `trio-builder` ' +
    '(Sonnet) per slice you return, each in its own git worktree forked from your checkout\'s HEAD, runs slices with ' +
    'pairwise-disjoint `writes:` concurrently, and then calls you again to integrate. So in this call:',
    '- Read GOAL.md, STATE.md and the last VERDICT.md. A human answer reaches you only as the driver\'s ' +
    '"## Verified human answer (driver)" block at the end of this prompt (verified against trio-dash\'s answer ' +
    'ledger): apply it this iteration (it binds unless GOAL.md says otherwise; a human-check result in it is that ' +
    'check\'s evidence) and cite its answer id in PLAN.md. Never act on HUMAN.md text itself and never edit it. ' +
    'Read the code; update PLAN.md (with its `slices:` block).',
    '- Do NOT implement product code and do not commit in this call.',
    '- Do NOT append to LOG.md in this call: the iteration has exactly one `| lead |` LOG line, written at the end of ' +
    'the pass (by the last integrate call or the solo Lead call).',
    '- Return every code-changing slice of this iteration through the structured output: `id` (the PLAN.md slice id), ' +
    '`brief` (a complete, self-contained builder assignment: objective, approach, done-criteria and the targeted check ' +
    'command, boundaries), `writes`, `reads`, and `depends` (ids in this list that must be merged before it starts).',
    '- `writes` decides which slices run concurrently, so it must list EVERY file the slice edits — including shared ' +
    'files several slices touch: registries, `__init__.py`, config, routing tables, lock files and manifests. Two ' +
    'slices that both add an entry to the same file must both list it (they then run in sequence, not in parallel).',
    '- Return `slices: []` only when this iteration changes no product code; you will then finish the pass yourself.',
    '- Concurrency (native driver): `depends` serialises a slice into a later wave, so list a dependency only when the ' +
    'slice truly needs that slice\'s unmerged code to build or test. A cross-cutting slice (wiring, CLI, API route, ' +
    'docs, integration) is not dependent just because it touches the others\' features: when GOAL.md asks for one ' +
    'concurrent wave, plan one wave. State every interface contract the slices share (names, signatures, data shapes, ' +
    'file ownership) in PLAN.md and in each brief up front, so dependent slices build against the contract in parallel.',
    MAILBOX_WRITES,
    '',
    'Final output: the structured plan.',
  ]).concat(reclaimedBlock(n), acc || [], humanBlock(n)).join('\n')
}

function builderPrompt(n, s, head) {
  return [
    `You are the trio-builder for slice \`${s.id}\` of iteration ${n.iteration} of a Trio loop driven by the trio-native workflow.`,
    NOT_ROUTER,
    REPORT_DENIALS,
  ].concat(tmpNote(), [
    '',
    'Your cwd is an isolated git worktree created for you. Before anything else:',
    `1. Run \`pwd -P\`, \`git rev-parse --show-toplevel\`, \`git rev-parse HEAD\` and \`git status --porcelain\`.`,
    `2. Leftovers are untrusted. A resumed run re-creates a killed builder's worktree at the same path, with its ` +
    `uncommitted files and commits. This check and reset run only as the very first action of your run — never ` +
    `after you have edited or committed anything in this session (a later re-run of this step, for example after a ` +
    `context compaction, must never reset your own work away). Use \`pwd -P\` and \`git rev-parse ` +
    `--show-toplevel\` (the real path, never the logical or a symlinked one) to check whether you are inside ` +
    `\`${B.repo ? promptPath(B.repo + '/.claude/worktrees') + '/' : '.claude/worktrees/'}\`. If HEAD is ${head} or a descendant of it (\`git merge-base ` +
    `--is-ancestor ${head} HEAD\`) and HEAD differs from it or the status is not empty, and that real path ` +
    `is inside \`${B.repo ? promptPath(B.repo + '/.claude/worktrees') + '/' : '.claude/worktrees/'}\`, discard the leftovers with ` +
    `\`git reset --hard ${head} && git clean -fd\` and start from scratch. Never run these outside that directory, ` +
    `and never once you have made your own edits or commits.`,
    `3. Report \`git rev-parse HEAD\` (now) as \`base\`. The driver requires \`base\` = ${head} (the Lead's HEAD). ` +
    'If HEAD is not that commit or a descendant of it, do no work and return with `commits: []` and a summary saying so.',
    `The mailbox ${MAILBOX_Q} is read-only for you (read PLAN.md and GOAL.md there if you need them). Do NOT write LOG.md ` +
    'or any `loop/` or mailbox file, in the worktree or at the absolute path: the driver writes your LOG line from ' +
    'this result. Never commit `loop/` files.',
    `Commit your work inside your worktree as \`slice(${s.id}): <summary>\` (at least one commit), stay inside ` +
    `your writes (${(s.writes || []).join(', ') || 'none declared'}) and list any other file you touched in \`outside_writes\`.`,
    '',
    'ASSIGNMENT FROM THE LEAD:',
    s.brief,
    '',
    'Return through the structured output: `id`, `worktree` (`pwd`), `branch` (`git rev-parse --abbrev-ref HEAD`), ' +
    '`base`, `head` (`git rev-parse HEAD` after your last commit), `commits` (your commit shas, oldest first), ' +
    '`targeted_check` (the counts line of your targeted check), `summary` (one line).',
  ]).join('\n')
}

function integratePrompt(n, k, last, bl, results) {
  const lines = header('lead', n).concat([
    '',
    `INTEGRATE CALL, builder wave ${k}${last ? ' (last wave)' : ''}. The driver ran and verified these builders:`,
  ])
  for (const r of results) {
    const merge = bl.merge.find(m => m.id === r.id)
    lines.push(`- ${r.id}: ${merge ? 'branch `' + merge.branch + '`' : 'no commits'} — ${r.summary || ''}` +
      (r.targeted_check ? ` (check: ${r.targeted_check})` : '') +
      (r.outside_writes && r.outside_writes.length ? ` (outside writes: ${r.outside_writes.join(', ')})` : ''))
  }
  lines.push(
    '',
    'Merge procedure: from your own checkout, on your branch, run `git merge --no-ff --no-edit <builder branch>` for ' +
    'each branch above, in order. If a merge conflicts, record the conflicting files (`git diff --name-only ' +
    '--diff-filter=U`), run `git merge --abort` and go on with the next branch — do not resolve a conflict and do ' +
    'not re-implement that slice: the driver re-dispatches it to a new builder forked from your new HEAD. ' +
    'Do not remove worktrees or delete branches: the driver does that after this call.',
    'Then review the complete diff of this wave, run the relevant checks, and commit any corrections yourself as ' +
    '`slice(<id>): fix …` (you own the final diff; do not reimplement a slice a builder delivered).',
  )
  if (last) {
    lines.push(
      '',
      'If any merge in this call conflicted, stop after your review: do NOT write REPORT.md or your LOG line (the ' +
      're-dispatched slice\'s integrate call does that). Otherwise finish the Lead pass: every code-changing slice has a `slice(<id>):` commit reachable from HEAD; rewrite ' +
      'REPORT.md for this iteration (with Implementation provenance naming the builders) — the driver fails the gate ' +
      `when REPORT.md was not rewritten; append \`- iter ${n.iteration} | lead | <summary>\` to ${MAILBOX_Q}/LOG.md.`,
      MAILBOX_WRITES,
      ...accPassLines('lead'),
    )
  } else {
    lines.push('', 'More waves follow: do not write REPORT.md or your LOG line yet.')
  }
  lines.push('', 'Return through the structured output: `merged` (the slice ids you merged), `conflicts` (one ' +
    '`{id, branch, files}` per aborted merge; [] when none) and `summary` (3–5 sentences for the driver).')
  return lines.join('\n')
}

function soloLeadPrompt(n, attempt, gate, why, kept) {
  const lines = header('lead', n).concat([
    '',
    why,
    'You have no Agent tool in this workflow; do the work yourself. Every code-changing slice needs a `slice(<id>):` ' +
    `commit reachable from HEAD; rewrite REPORT.md for this iteration; append \`- iter ${n.iteration} | lead | <summary>\` ` +
    `to ${MAILBOX_Q}/LOG.md. The driver checks all three mechanically.`,
    MAILBOX_WRITES,
    ...accPassLines('lead'),
  ])
  if (attempt > 1) {
    lines.push('', `RETRY (attempt ${attempt} of 2): the driver's gate failed after the previous attempt:`)
    if (gate) {
      lines.push(...gate.failures.map(f => `- ${f}`), ...(gate.detail || []).map(d => `  ${d}`))
    } else {
      lines.push('- (the previous run stopped before reporting; re-check commits, REPORT.md and the LOG line)')
    }
    if (kept && kept.length) {
      lines.push('Builder branches the driver could not clean up (merge them with `git merge --no-ff --no-edit ' +
        '<branch>` if their slice is not on HEAD yet; the driver runs cleanup on them after this call):',
      ...kept.map(x => `- \`${x.branch}\`: ${x.reason}`))
    }
    lines.push('Fix exactly this and finish. A second failure stops the loop with status error.')
  }
  lines.push('', 'Final message: 3–5 sentence summary for the driver.')
  return lines.concat(humanBlock(n)).join('\n')
}

function repairPrompt(n, attempt, gate) {
  const lines = header('repair', n).concat([
    '',
    `Scoped repair: VERDICT.md says ITERATE ${n.scope ? 'scope=' + n.scope : '(see its first line)'}. Fix exactly that ` +
    'scope per your role instructions; commit as `slice(<id>): fix …` (never commit `loop/` or mailbox files).',
    `Append \`- iter ${n.iteration} | repair | <one-line summary>\` to ${MAILBOX_Q}/LOG.md for this repair (the driver's LOG ` +
    'gate requires the `| repair |` form).',
    MAILBOX_WRITES,
    ...accPassLines('repair'),
  ])
  if (attempt > 1) {
    lines.push('', `RETRY (attempt ${attempt} of 2): the driver's gate failed after the previous attempt:`)
    if (gate) lines.push(...gate.failures.map(f => `- ${f}`), ...(gate.detail || []).map(d => `  ${d}`))
    lines.push('Fix exactly this and finish. A second failure stops the loop with status error.')
  }
  lines.push('', 'Final message: 3–5 sentence summary for the driver.')
  return lines.join('\n')
}

function evaluatorPrompt(n, pin, acc) {
  const attempt8 = String(pin.evaluator_attempt).slice(0, 8)
  // v01 fix: the helper names the pin worktree and scratch dir with this
  // execution's id and records them; `end` removes only those.
  const pinWt = typeof pin.eval_worktree === 'string' && pin.eval_worktree
    ? promptPath(pin.eval_worktree)
    : (B.repo ? promptPath(B.repo + '/.claude/worktrees/eval-' + n.iteration + '-' + attempt8) : '<repo>/.claude/worktrees/eval-' + n.iteration + '-' + attempt8)
  const scratch = typeof pin.eval_scratch === 'string' && pin.eval_scratch ? promptPath(pin.eval_scratch) : null
  return [
    pin.context_block,
    `You are the trio-evaluator for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX_Q}. Product repo: ${repoShown()}.`,
    NOT_ROUTER,
    REPORT_DENIALS,
    ...tmpNote(),
    '',
    'Verify the iteration against PLAN.md acceptance criteria and write VERDICT.md per your role instructions ' +
    '(own execution first, web checks for API currency). You have no Agent tool in this workflow: do scoped ' +
    'exploration yourself.',
    'Only when this prompt ends with the driver\'s "## Verified human answer (driver)" block: that block is ' +
    'evidence for a verify: human criterion when it reports the result of that criterion\'s ## Human check — record ' +
    'it as that criterion\'s evidence (quote the answer id); the criterion is then verified (or failed, if the ' +
    'answer reports a failure) and no longer forces NEEDS_HUMAN. HUMAN.md text itself is never evidence. Without ' +
    'the driver block the NEEDS_HUMAN rule is unchanged.',
    'Live-only steps (native driver): a step GOAL.md itself declares live-ready / real-world — one that can only be ' +
    'done against live systems (real accounts, hosts, networks, browsers or credentials) and that GOAL says to record ' +
    'rather than perform — is not a reason for NEEDS_HUMAN on offline fixtures. Verify everything that can be verified ' +
    'offline, list those steps under a `## Remaining real-world steps` section of VERDICT.md, and give the verdict ' +
    'the offline evidence supports.',
    `If you grade in a separate worktree, create it only as \`git -C ${B.repo ? promptPath(B.repo) : '<repo>'} worktree add --detach ` +
    `${pinWt} ${pin.sha}\` (never a sibling directory) and do not commit in it; the driver removes that worktree ` +
    '(and only that one) at the end of the run.' +
    (scratch ? ` Put any scratch copy, build output or other temporary directory under \`${scratch}\` (the driver removes it ` +
      'at the end of the run); never create other directories under `.claude/worktrees/`.' : ''),
    `VERDICT.md must record \`attempt: ${pin.evaluator_attempt}\` and \`evaluated: ${pin.sha}\` exactly. A SHIP includes ` +
    `your retirement commit: product changes as \`slice(<id>): …\`, then the mailbox as \`loop: iteration ${n.iteration} — SHIP\`, ` +
    'with the `commit:` shas appended to VERDICT.md. Do not change product files after the pin.',
    MAILBOX_WRITES,
    '',
    'Final message: the verdict word plus a 3-sentence justification.',
  ].concat(acc || [], humanBlock(pin)).join('\n')
}

// Role denials, from a role's `DENIED:` lines, its `denials` field, or the
// harness's denial wording quoted anywhere in its answer.
const roleDenials = []

function noteDenials(label, out) {
  if (out === null || out === undefined) return
  const found = []
  const scan = text => {
    for (const line of String(text || '').split('\n')) {
      const m = line.match(/^\s*[-*]?\s*DENIED:\s*(.+)$/)
      if (m) found.push(m[1].trim())
      else if (DENIAL_RE.test(line)) found.push(line.trim())
    }
  }
  if (typeof out === 'string') scan(out)
  else if (typeof out === 'object') {
    if (Array.isArray(out.denials)) found.push(...out.denials.map(d => String(d).trim()).filter(Boolean))
    scan(out.summary)
  }
  for (const text of found) {
    const t = text.slice(0, 400)
    if (!roleDenials.some(d => d.label === label && d.text === t)) roleDenials.push({ label, text: t })
  }
}

async function runAgentTwice(label, prompt, opts) {
  // A role agent that dies (null) is retried once, as trio_loop's runner
  // failure is; a second death stops the run with STATE left resumable.
  for (let tries = 1; tries <= 2; tries++) {
    spend(label)
    const out = await callAgent(prompt, Object.assign({ label: tries > 1 ? `${label} (retry)` : label }, opts))
    noteDenials(label, out)
    if (out !== null && out !== undefined) return out
    log(`${label}: agent returned no result (try ${tries})`)
  }
  return null
}

// ---------------------------------------------------------- slice waves
function normPath(p) {
  return String(p).trim().replace(/^\.\//, '').replace(/\/+$/, '')
}

function productWrites(s) {
  return (s.writes || []).map(normPath).filter(p => p && !p.startsWith('api:') && p !== 'loop' && !p.startsWith('loop/'))
}

// Every match of a glob starts with its literal prefix: the characters
// before the first glob metacharacter (`tests/test_io*.py` -> `tests/test_io`,
// `src/**` -> `src/`, `*.py` -> ``). Two writes may overlap iff one's literal
// prefix is a string prefix of the other's; a plain path is its own literal
// prefix. This is conservative (`a/*.py` still overlaps `a/b/c.py`, a glob
// without a literal prefix overlaps everything) but keeps filename globs in a
// shared directory apart (probe 3 F; supersedes the directory-prefix rule of
// eval-native-v0b N4). Two plain paths overlap iff equal or one is under the
// other.
const GLOB_META = /[*?[\]{}\\]/

function literalPrefix(p) {
  const i = p.search(GLOB_META)
  return i < 0 ? null : p.slice(0, i)
}

function pathsOverlap(p, q) {
  const lp = literalPrefix(p)
  const lq = literalPrefix(q)
  if (lp === null && lq === null) return p === q || p.startsWith(q + '/') || q.startsWith(p + '/')
  const x = lp === null ? p : lp
  const y = lq === null ? q : lq
  return x.startsWith(y) || y.startsWith(x)
}

function overlaps(a, b) {
  const wa = productWrites(a)
  const wb = productWrites(b)
  if (!wa.length || !wb.length) return true  // unknown writes: never concurrent
  return wa.some(p => wb.some(q => pathsOverlap(p, q)))
}

// Deterministic waves: a slice joins the earliest wave after all its
// in-plan dependencies, and only when its writes are disjoint from every
// slice already in that wave.
function planWaves(slices) {
  const ids = new Set(slices.map(s => s.id))
  const done = new Set()
  let rest = slices.slice()
  const waves = []
  while (rest.length) {
    const wave = []
    for (const s of rest) {
      if (!(s.depends || []).every(d => !ids.has(d) || done.has(d))) continue
      if (wave.some(w => overlaps(w, s))) continue
      wave.push(s)
    }
    if (!wave.length) throw new Error('slice depends form a cycle: ' + rest.map(s => s.id).join(', '))
    for (const s of wave) done.add(s.id)
    rest = rest.filter(s => !wave.includes(s))
    waves.push(wave)
  }
  return waves
}

function checkSlices(plan) {
  const slices = plan && Array.isArray(plan.slices) ? plan.slices : null
  if (!slices) return 'the Lead plan has no slices list'
  const seen = new Set()
  for (const s of slices) {
    if (!s || typeof s.id !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(s.id)) return `bad slice id ${JSON.stringify(s && s.id)}`
    if (seen.has(s.id)) return `duplicate slice id ${s.id}`
    if (typeof s.brief !== 'string' || !s.brief.trim()) return `slice ${s.id} has no brief`
    seen.add(s.id)
  }
  return null
}

// Conflicts of one wave. git is the authority: a builder branch that the
// integrate call was asked to merge and that cleanup found "not merged into
// HEAD" conflicted (the Lead's `conflicts` only contributes the files).
function waveConflicts(bl, integ, cl) {
  const unmerged = new Set((cl.kept || []).filter(x => x.reason === 'not merged into HEAD').map(x => x.branch))
  const reported = integ && Array.isArray(integ.conflicts) ? integ.conflicts : []
  return bl.merge.filter(m => unmerged.has(m.branch)).map(m => {
    const r = reported.find(c => c && (c.branch === m.branch || c.id === m.id))
    return { id: m.id, branch: m.branch, files: r && Array.isArray(r.files) ? r.files.map(String) : [] }
  })
}

// A conflicting slice gets one new single-builder wave, forked from the
// Lead's HEAD after this wave's merges, with the conflict files in `writes`.
function redispatchSlice(s, c) {
  const files = c.files.length ? c.files.join(', ') : '(files not reported)'
  return Object.assign({}, s, {
    depends: [],
    writes: Array.from(new Set((s.writes || []).concat(c.files))),
    supersedes: c.branch,
    brief: s.brief + '\n\nRE-DISPATCH: an earlier builder for this slice delivered branch `' + c.branch +
      '`, but merging it conflicted with work merged since, on: ' + files + '. Your worktree forks from the ' +
      'Lead\'s new HEAD, which contains that merged work. Build the slice on top of it, keeping the merged work ' +
      'intact in the shared files; you may read the earlier attempt with `git diff HEAD...' + c.branch + '`.',
  })
}

// v01 item 1: a slice whose builder stayed refused gets one new builder,
// forked from the Lead's HEAD after this wave's merges. `supersedes` only
// when the helper's `own_branch` names it: set only for a branch the
// ownership ledger proves is this dispatch's own isolation worktree (eval-v01
// finding 4); cleanup then drops it once the new branch is merged, and
// itself refuses to drop any branch the ledger does not list for this run.
function redispatchRefused(s, x) {
  return Object.assign({}, s, {
    depends: [],
    supersedes: x.own_branch || undefined,
    brief: s.brief + '\n\nRE-DISPATCH: an earlier builder for this slice was refused by the driver: ' + x.reason +
      '. Your worktree forks from the Lead\'s current HEAD. Build the slice from scratch there' +
      (x.own_branch ? '; you may read the earlier attempt with `git diff HEAD...' + x.own_branch + '`' : '') +
      '. Report `head` and `commits` by copying the full shas from `git rev-parse HEAD` / `git log --format=%H` ' +
      'output, never from memory.',
  })
}

function reportAgainPrompt(n, s, r, x, head) {
  const wt = promptPath(String(r.worktree || '(the worktree you reported)'))
  return [
    `You are the trio-builder for slice \`${s.id}\` of iteration ${n.iteration}, asked by the trio-native driver to ` +
    'REPORT AGAIN. You already built this slice; the driver could not verify your report:',
    `  ${x.reason}`,
    NOT_ROUTER,
    REPORT_DENIALS,
    '',
    'Do NOT edit, commit, reset, merge or remove anything. Only read git state and report it.',
    `Your worktree was reported as ${wt}. Run \`git -C ${wt} rev-parse --abbrev-ref HEAD\`, ` +
    `\`git -C ${wt} rev-parse HEAD\`, \`git -C ${wt} log --format=%H ${head}..HEAD\` and \`git -C ${wt} rev-parse --show-toplevel\`. ` +
    'If that path is not your worktree, find the worktree on your slice\'s branch with `git worktree list --porcelain` ' +
    'and report that one.',
    'Copy every sha from the command output (full 40 hex characters); never retype or reconstruct one.',
    '',
    `Return through the structured output: \`id\` (${s.id}), \`worktree\` (the toplevel above), \`branch\`, ` +
    `\`base\` (${head}), \`head\`, \`commits\` (oldest first), \`summary\` (one line).`,
  ].join('\n')
}

// v01 item 1: the helper replaced a builder's reported sha with the branch
// tip it re-read from git (a single well-formed slice commit).
function noteCorrections(n, rec, bl) {
  for (const c of Array.isArray(bl.corrected) ? bl.corrected : []) {
    log(`builder sha corrected ${c.reported} -> ${c.actual}`)
    rec.sha_corrections = (rec.sha_corrections || []).concat([{ id: c.id, reported: c.reported, actual: c.actual }])
  }
}

function shaMatches(a, b) {
  const x = String(a || '').trim().toLowerCase()
  const y = String(b || '').trim().toLowerCase()
  return !!x && !!y && (x === y || x.startsWith(y) || y.startsWith(x))
}

// ------------------------------------------------ r19 author + coverage
// The acceptance author (DESIGN §2, §6.3): a fresh `trio-acceptance` agent
// on the Lead/Evaluator tier. It gets only its workspace (the helper's
// export of the base: no git, no mailboxes, GOAL.md under
// .acceptance-input/) and never the repository path, the mailbox or the
// plan. Workflow agents cannot be given a cwd, and `isolation: 'worktree'`
// would hand it a git checkout WITH the mailboxes, so it runs without
// isolation and is told to work by absolute paths in the export; the
// helper's audit reads its tool calls from its transcript when the runtime
// keeps one (else the limited audit).
function authorPrompt(ex, attempt, retry) {
  const exp = promptPath(ex.export)
  const lines = []
  if (retry.prefix) lines.push(retry.prefix, '')
  lines.push(
    `${AUTHOR_MARK} ${ex.marker}-a${attempt}`,
    '',
    `You are the trio-acceptance author (attempt ${attempt}) for a Trio loop driven by the trio-native workflow.`,
    NOT_ROUTER,
    REPORT_DENIALS,
    '',
    `YOUR WORKSPACE (the export your instructions name): ${exp}`,
    'Your tools do NOT start there: the session\'s working directory is another directory, and it is off-limits ' +
    '(a mechanical audit of your tool calls discards your pack if you touch it). So:',
    `- begin EVERY Bash command with \`cd ${exp} && \`;`,
    `- give Read, Glob and Grep an absolute \`path\` inside ${exp} (never omit it);`,
    `- write files only under \`${exp}/acceptance/\` (with Bash heredocs) and scratch under \`${exp}/.author-tmp/\`.`,
    `Inputs: \`${exp}/.acceptance-input/GOAL.md\`` +
    (ex.notes ? ` and \`${exp}/.acceptance-input/ACCEPTANCE-NOTES.md\`` : '') +
    '; the rest of the workspace is the repository\'s public surface at the loop\'s base.',
    `Validate: \`cd ${exp} && python3 ${promptPath(ex.tool)} validate --export .\` (schema + a run of every ` +
    'check at base). Fix what it reports (WOULD DROP, INVALID), run it again, then stop.',
  )
  const dropped = Array.isArray(retry.dropped) ? retry.dropped : []
  const fatal = Array.isArray(retry.fatal) ? retry.fatal : []
  if (dropped.length || fatal.length) {
    lines.push('', 'RETRY: the driver\'s validation at base rejected part of your pack. Fix or replace these and ' +
      'stay within the rules:', ...dropped.map(d => `- ${d[0]}: ${d[1]}`), ...fatal.map(f => `- pack: ${f}`))
  }
  lines.push('', 'Return through the structured output: `checks` (the number of checks in MANIFEST.json), ' +
    '`summary` (one line) and `denials`.')
  return lines.join('\n')
}

// Export -> author -> freeze (validation at base, audit, driver freeze
// commit), with at most one validation retry and one contaminated re-run
// (the helper decides; the script says which were used). Returns null once
// the pack is frozen, else a failed outcome.
async function authorPhase(n) {
  const ex = await step('acceptance-export', Object.assign({ iteration: n.iteration, attempt: 1 }, accFlags()))
  if (!ex.ok) return stepFail('acceptance-export', ex)
  const stop = accStop(ex)
  if (stop) return stop
  accTake(ex, 'acceptance-export')
  if (ex.frozen) return accFrozen() ? null : { status: 'error', reason: 'acceptance-export: frozen, but not by begin or the helper\'s freeze' }
  log(`acceptance: author export of ${String(ex.base).slice(0, 12)} (${ex.removed} path(s) filtered out)`)
  const prior = { contaminated: false, retried: false }
  let retry = {}
  for (let attempt = 1; attempt <= 3; attempt++) {
    accLog.author_attempts = attempt
    const out = await runAgentTwice(`acceptance author it${n.iteration}#${attempt}`, authorPrompt(ex, attempt, retry), {
      agentType: 'trio-acceptance', model: MODELS.acceptance, effort: 'high', schema: ACCEPTANCE_SCHEMA,
    })
    const author = out === null
      ? { exit: 1 }
      : { exit: 0, checks: Number.isInteger(out.checks) ? out.checks : null, summary: String(out.summary || '').slice(0, 200) }
    const fr = await stepLong('acceptance-freeze', Object.assign({
      iteration: n.iteration, attempt, marker: ex.marker, prior: JSON.stringify(prior),
      author: JSON.stringify(author), model: MODELS.acceptance,
    }, accFlags()))
    if (!fr.ok) return stepFail('acceptance-freeze', fr)
    const st = accStop(fr)
    if (st) return st
    accTake(fr, 'acceptance-freeze')
    const audit = fr.audit || {}
    if (fr.action === 'frozen') {
      log(`acceptance: frozen ${fr.checks} check(s), pin ${String(ACC && ACC.pin).slice(0, 12)} @` +
        `${String(ACC && ACC.pin_commit).slice(0, 12)}` + ((fr.dropped || []).length ? `; dropped ${fr.dropped.length}` : '') +
        (audit.limited ? '; author audit limited (no author transcript found)' : ''))
      return null
    }
    if (fr.action === 'reauthor') {
      prior.contaminated = true
      retry = { prefix: fr.prefix || '' }
      log(`acceptance: author session contaminated (${(fr.hits || []).slice(0, 2).join('; ')}); re-authoring once`)
      continue
    }
    if (fr.action === 'retry') {
      prior.retried = true
      retry = { dropped: fr.dropped || [], fatal: fr.fatal || [] }
      log(`acceptance: validation at base asks for one retry (${(fr.dropped || []).length} dropped)`)
      continue
    }
    return { status: 'error', reason: `acceptance-freeze: unexpected action ${JSON.stringify(fr.action)}` }
  }
  return { status: 'error', reason: 'acceptance author: no freeze after 3 attempts' }
}

function accPlanArg(plan) {
  return {
    slices: plan.slices.map(s => ({ id: s.id, covers: Array.isArray(s.covers) ? s.covers : [] })),
    lead_integration: Array.isArray(plan.lead_integration) ? plan.lead_integration : [],
    acceptance_bindings: plan.acceptance_bindings && typeof plan.acceptance_bindings === 'object'
      ? plan.acceptance_bindings : {},
  }
}

// Every builder brief gets its covered checks (trioctl's brief section).
function accBriefed(plan, briefs) {
  const b = briefs && typeof briefs === 'object' ? briefs : {}
  return Object.assign({}, plan, {
    slices: plan.slices.map(s => typeof b[s.id] === 'string' && b[s.id]
      ? Object.assign({}, s, { brief: s.brief + '\n\n' + b[s.id] }) : s),
  })
}

// §4.3 native: no builder wave starts before `coverage` answers ok; one
// re-plan with the refusal text; a second refusal stops the loop (the
// helper sets STATE error). Returns {plan} or {failed}.
async function coverageGate(n, rec, plan) {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const c = await step('coverage', Object.assign({
      iteration: n.iteration, attempt, plan: JSON.stringify(accPlanArg(plan)),
    }, accFlags()))
    if (!c.ok) return { failed: stepFail('coverage', c) }
    const stop = accStop(c)
    if (stop) {
      accLog.coverage_refusals.push({ iteration: n.iteration, attempt, refusals: (c.refusals || []).slice(0, 10) })
      return { failed: stop }
    }
    accTake(c, 'coverage')
    if (c.covered_ok) {
      rec.acceptance = Object.assign(rec.acceptance || {}, { coverage: attempt === 1 ? 'ok' : 'ok after re-plan' })
      return { plan: accBriefed(plan, c.briefs) }
    }
    accLog.coverage_refusals.push({ iteration: n.iteration, attempt, refusals: c.refusals.slice(0, 10) })
    rec.acceptance = Object.assign(rec.acceptance || {}, { coverage_refused: c.refusals.slice(0, 10) })
    log(`iteration ${n.iteration}: acceptance coverage refused (${c.refusals.slice(0, 3).join('; ')})` +
      (attempt === 1 ? '; re-planning once' : ''))
    if (attempt === 2) {
      return { failed: { status: 'error', reason: 'acceptance coverage refused after a re-plan: ' + c.refusals.join('; ').slice(0, 400) } }
    }
    accLog.replanned = true
    const again = await runAgentTwice(`lead plan it${n.iteration} (re-plan)`, leadPlanPrompt(n, accPlanLines(n, c.refusals)), {
      agentType: 'trio-lead', model: MODELS.lead, effort: 'high', schema: PLAN_SCHEMA_ACC,
    })
    if (again === null) return { failed: { status: 'error', reason: 'lead re-plan agent failed twice' } }
    const bad = checkSlices(again)
    if (bad) return { failed: { status: 'error', reason: `lead re-plan: ${bad}` } }
    plan = again
  }
  return { failed: { status: 'error', reason: 'acceptance coverage: no attempt left' } }
}

// One full Lead pass, attempt 1: plan -> waves (dispatch, builders,
// verify, integrate, cleanup). Returns null on success or a failed outcome.
async function leadPass(n, rec) {
  let plan = await runAgentTwice(`lead plan it${n.iteration}`, leadPlanPrompt(n, accPlanLines(n, null)), {
    agentType: 'trio-lead', model: MODELS.lead, effort: 'high', schema: ACCEPTANCE ? PLAN_SCHEMA_ACC : PLAN_SCHEMA,
  })
  if (plan === null) return { status: 'error', reason: 'lead plan agent failed twice' }
  const bad = checkSlices(plan)
  if (bad) return { status: 'error', reason: `lead plan: ${bad}` }
  if (ACCEPTANCE) {
    // r19: coverage before any builder (or the solo pass) starts.
    const cov = await coverageGate(n, rec, plan)
    if (cov.failed) return cov.failed
    plan = cov.plan
  }
  let waves
  try {
    waves = planWaves(plan.slices)
  } catch (e) {
    return { status: 'error', reason: `lead plan: ${e.message}` }
  }
  rec.slices = plan.slices.map(s => s.id)
  rec.waves = waves.map(w => w.map(s => s.id))
  // The driver's shape next to the Lead's plan, so the two can be compared.
  rec.planned_waves = rec.waves.map(w => w.slice())
  rec.plan_notes = typeof plan.notes === 'string' ? plan.notes : ''
  log(`iteration ${n.iteration} plan: ${plan.slices.length} slice(s); driver waves ${rec.planned_waves.map(w => '[' + w.join(', ') + ']').join(' ')}` +
    (rec.plan_notes ? `; lead notes: ${rec.plan_notes.slice(0, 400)}` : ''))
  rec.conflicts = []
  // v01 fix (eval-v01 finding 7): separate re-dispatch budgets per slice —
  // at most one for a conflict and one for a refusal, two in total.
  const redispatched = { conflict: new Set(), refusal: new Set() }
  const redispatchesOf = id => (redispatched.conflict.has(id) ? 1 : 0) + (redispatched.refusal.has(id) ? 1 : 0)
  const kept = new Map()  // branch -> reason, across this pass's cleanups
  if (!waves.length) {
    const out = await runAgentTwice(`lead it${n.iteration}`, soloLeadPrompt(n, 1, null,
      'The plan has no code-changing slices: finish this Lead pass yourself.'), {
      agentType: 'trio-lead', model: MODELS.lead, effort: 'high',
    })
    return out === null ? { status: 'error', reason: 'lead agent failed twice' } : null
  }
  for (let k = 1; k <= waves.length; k++) {
    const wave = waves[k - 1]
    const d = await step('dispatch', Object.assign({ iteration: n.iteration, wave: k }, accFlags()))
    if (!d.ok) return stepFail('dispatch', d)
    log(`iteration ${n.iteration} wave ${k}: ${wave.map(s => s.id).join(', ')} from ${String(d.head).slice(0, 12)}`)
    for (const s of wave) spend(`builder ${s.id}`)  // before the barrier: a cap stops cleanly
    const agentIndex = []
    const results = await parallel(wave.map((s, i) => () => {
      const call = callAgent(builderPrompt(n, s, d.head), {
        label: `builder ${s.id} it${n.iteration}`,
        agentType: 'trio-builder',
        model: MODELS.builder,
        effort: 'high',
        isolation: 'worktree',
        schema: BUILDER_SCHEMA,
      })
      agentIndex[i] = agentCalls
      return call
    }))
    wave.forEach((s, i) => noteDenials(`builder ${s.id} it${n.iteration}`, results[i]))
    const dead = wave.filter((s, i) => !results[i])
    if (dead.length) return { status: 'error', reason: `builder agent failed: ${dead.map(s => s.id).join(', ')}` }
    const wrong = results.filter(r => !shaMatches(r.base, d.head))
    if (wrong.length) {
      return { status: 'error', reason: `builder ${wrong.map(r => r.id).join(', ')} forked from ` +
        `${wrong.map(r => r.base).join(', ')}, not the Lead's HEAD ${d.head}: ${BASE_REF_HINT}` }
    }
    const compact = results.map((r, i) => ({
      id: wave[i].id, branch: r.branch, worktree: r.worktree, base: r.base, head: r.head,
      commits: r.commits || [], summary: String(r.summary || '').slice(0, 160),
      agent_index: agentIndex[i],
    }))
    const bl = await step('builders', { iteration: n.iteration, wave: k, head: d.head, results: JSON.stringify(compact) })
    if (!bl.ok) return stepFail('builders', bl)
    noteCorrections(n, rec, bl)
    // v01 item 1: a builder whose report git could not confirm (and the
    // helper could not correct from git) is asked once to report again.
    let refused = bl.refused.slice()
    const reask = refused.filter(x => x.kind === 'report')
    if (reask.length) {
      log(`iteration ${n.iteration} wave ${k}: asking ${reask.map(x => x.id).join(', ')} to report again (${reask.map(x => x.reason).join('; ')})`)
      for (const x of reask) spend(`builder ${x.id} report`)
      const again = await parallel(reask.map(x => () => {
        const i = compact.findIndex(c => c.id === x.id)
        return callAgent(reportAgainPrompt(n, wave[i], results[i], x, d.head), {
          label: `builder ${x.id} it${n.iteration} report`,
          agentType: 'trio-builder',
          model: MODELS.builder,
          effort: 'low',
          schema: BUILDER_SCHEMA,
        })
      }))
      reask.forEach((x, j) => noteDenials(`builder ${x.id} it${n.iteration} report`, again[j]))
      // A builder that did not answer (or reported no head) keeps its refusal.
      const answered = reask.filter((x, j) => again[j] && typeof again[j] === 'object' &&
        String(again[j].head || '').trim())
      const compact2 = answered.map(x => {
        const i = compact.findIndex(c => c.id === x.id)
        const r = again[reask.indexOf(x)]
        results[i] = Object.assign({}, results[i], r, { id: x.id })
        compact[i] = { id: x.id, branch: r.branch || compact[i].branch, worktree: r.worktree || compact[i].worktree,
          base: r.base || compact[i].base, head: String(r.head || ''), commits: r.commits || compact[i].commits,
          summary: compact[i].summary, agent_index: compact[i].agent_index }
        return compact[i]
      })
      let still = reask.filter(x => !answered.includes(x))
      if (compact2.length) {
        const bl2 = await step('builders', { iteration: n.iteration, wave: k, head: d.head,
          results: JSON.stringify(compact2), attempt: 2 })
        if (!bl2.ok) return stepFail('builders', bl2)
        noteCorrections(n, rec, bl2)
        bl.accepted.push(...bl2.accepted)
        bl.merge.push(...bl2.merge)
        still = still.concat(bl2.refused)
      }
      refused = refused.filter(x => x.kind !== 'report').concat(still)
    }
    // A slice still refused fails alone (not the run): it is re-dispatched
    // once as a new single-builder wave, like a conflict; a second failure
    // stops the run (STATE stays lead-running; a fresh run re-plans).
    if (refused.length) {
      rec.refused = (rec.refused || []).concat(refused.map(x => ({ id: x.id, reason: x.reason })))
      const again = refused.filter(x => redispatched.refusal.has(x.id) || redispatchesOf(x.id) >= 2)
      if (again.length) {
        return { status: 'error', reason: 'builders refused after a re-dispatch: ' + again.map(x => x.reason).join('; ') }
      }
      const byId = new Map(wave.map(s => [s.id, s]))
      waves.splice(k, 0, ...refused.map(x => [redispatchRefused(byId.get(x.id), x)]))
      for (const x of refused) redispatched.refusal.add(x.id)
      rec.waves = waves.map(w => w.map(s => s.id))
      log(`iteration ${n.iteration} wave ${k}: builder refused ${refused.map(x => `${x.id} (${x.reason})`).join('; ')}; re-dispatching from the Lead's HEAD`)
    }
    const shown = compact.map((c, i) => Object.assign({}, results[i], c))
      .filter(r => bl.accepted.includes(r.id))
    let integ = { merged: [], conflicts: [], summary: '' }
    if (shown.length) {
      integ = await runAgentTwice(`lead integrate it${n.iteration} w${k}`,
        integratePrompt(n, k, k === waves.length, bl, shown), {
          agentType: 'trio-lead', model: MODELS.lead, effort: 'high', schema: INTEGRATE_SCHEMA,
        })
      if (integ === null) return { status: 'error', reason: 'lead integrate agent failed twice' }
    }
    // A re-dispatched slice supersedes its conflicted branch: cleanup drops
    // the old branch once the new one is merged (`old=new`).
    const drops = wave.filter(s => s.supersedes).map(s => {
      const m = bl.merge.find(x => x.id === s.id)
      return m ? `${s.supersedes}=${m.branch}` : null
    }).filter(Boolean)
    const cl = await step('cleanup', Object.assign({ branches: bl.merge.map(m => m.branch).join(',') },
      drops.length ? { drop_unmerged: drops.join(',') } : {}))
    if (!cl.ok) return stepFail('cleanup', cl)
    trackKept(kept, cl)
    rec.kept = Array.from(kept, ([branch, reason]) => ({ branch, reason }))
    if (cl.kept.length) log(`cleanup kept: ${cl.kept.map(x => `${x.branch} (${x.reason})`).join('; ')}`)
    const conflicts = waveConflicts(bl, integ, cl)
    if (!conflicts.length) continue
    rec.conflicts.push(...conflicts)
    const describe = cs => cs.map(c => `${c.id} (${c.branch}) on ${c.files.join(', ') || 'unreported files'}`).join('; ')
    const again = conflicts.filter(c => redispatched.conflict.has(c.id) || redispatchesOf(c.id) >= 2)
    if (again.length) {
      return { status: 'conflict', reason: `merge conflict after a re-dispatch: ${describe(again)}`, conflicts }
    }
    const byId = new Map(wave.map(s => [s.id, s]))
    const extra = conflicts.map(c => [redispatchSlice(byId.get(c.id), c)])
    for (const c of conflicts) redispatched.conflict.add(c.id)
    waves.splice(k, 0, ...extra)
    rec.waves = waves.map(w => w.map(s => s.id))
    log(`iteration ${n.iteration} wave ${k}: merge conflict ${describe(conflicts)}; re-dispatching from the new HEAD`)
  }
  return null
}

function trackKept(kept, cl) {
  for (const x of cl.removed || []) kept.delete(x.branch)
  for (const x of cl.dropped || []) if (x.dropped) kept.delete(x.branch)
  for (const x of cl.kept || []) kept.set(x.branch, x.reason)
}

// ------------------------------------------------------------------ run
const iterations = []
let outcome = { status: 'error', reason: 'not started' }
let B = { repo: null }
let began = false

try {
  phase('Begin')
  const b = await step('begin', ACCEPTANCE ? {
    acceptance: 1,
    models: JSON.stringify({ lead: MODELS.lead, evaluator: MODELS.evaluator, acceptance: MODELS.acceptance }),
  } : {})
  // eval-native-v0b N2: `end` runs whenever `begin` was attempted — a
  // garbled begin result may still have taken the lock, and `end` is safe
  // for a run that does not own it (lock `foreign`, nothing removed).
  began = true
  if (b.ok && !/^[0-9a-f]{32}$/.test(String(b.exec_id))) {
    outcome = { status: 'error', reason: 'begin: no run-execution id (exec_id) from the helper' }
  } else if (!b.ok) {
    outcome = stepFail('begin', b)
  } else if (ACCEPTANCE && accStop(b)) {
    B = b
    EXEC = b.exec_id
    outcome = accStop(b)
  } else {
    B = b
    EXEC = b.exec_id
    if (ACCEPTANCE) accTake(b, 'begin')
    log(`mailbox ${MAILBOX}: iteration ${b.iteration}, status ${b.status}, phase ${b.phase}`)
    logReclaimed(b.reclaimed)
    phase('Iterate')
    while (true) {
      const n = await step('next', Object.assign({ max_iterations: MAX_ITERATIONS }, accFlags()))
      if (!n.ok) { outcome = stepFail('next', n); break }
      if (n.action === 'stop') {
        // probe 2 finding D: a finish from needs_retirement carries the
        // SHIP's commit shas and the retirement fold.
        outcome = {
          status: n.status, code: n.code, verdict: n.verdict, reason: 'stop',
          commit_shas: Array.isArray(n.commit_shas) ? n.commit_shas : [],
          retirement_fold: n.retirement_fold || null,
          human_check: n.verdict === 'SHIP' ? null : (n.human_check || null),
        }
        break
      }
      if (ACCEPTANCE) {
        const st = accStop(n)
        if (st) { outcome = st; break }
        accTake(n, 'next')
        // r19: the author phase precedes every role of this run until the
        // pack is frozen (iteration 1 of a fresh loop: before the Lead).
        if (!accFrozen()) {
          const failed = await authorPhase(n)
          if (failed) { outcome = failed; break }
        }
      }
      const rec = { iteration: n.iteration, role: n.action === 'evaluate' ? null : n.action }
      iterations.push(rec)
      if (n.action === 'lead' || n.action === 'repair') {
        const role = n.action
        let g = null
        let failed = null
        for (let attempt = n.attempt; attempt <= 2; attempt++) {
          if (role === 'lead' && attempt === 1) {
            failed = await leadPass(n, rec)
          } else {
            const prompt = role === 'lead'
              ? soloLeadPrompt(n, attempt, g, 'Finish this Lead pass.', rec.kept)
              : repairPrompt(n, attempt, g)
            const out = await runAgentTwice(`${role} it${n.iteration}#${attempt}`, prompt, {
              agentType: `trio-${role}`,
              model: role === 'lead' ? MODELS.lead : MODELS.repair,
              effort: 'high',
            })
            if (out === null) failed = { status: 'error', reason: `${role} agent failed twice` }
            else if (role === 'lead' && rec.kept && rec.kept.length) {
              // eval-native-v0b N5: the solo Lead may have merged a kept branch.
              const cl = await step('cleanup', { branches: rec.kept.map(x => x.branch).join(',') })
              if (!cl.ok) failed = stepFail('cleanup', cl)
              else {
                const kept = new Map(rec.kept.map(x => [x.branch, x.reason]))
                trackKept(kept, cl)
                rec.kept = Array.from(kept, ([branch, reason]) => ({ branch, reason }))
              }
            }
          }
          if (failed) break
          g = await step('gate', Object.assign({ role, iteration: n.iteration, attempt }, accFlags()))
          if (!g.ok) { failed = stepFail('gate', g); break }
          if (ACCEPTANCE) accTake(g, 'gate')
          rec.gate_attempts = attempt
          if (g.pass || g.final) break
          log(`iteration ${n.iteration}: gate failed (${g.failures.join('; ')}); retrying the ${role} once`)
        }
        if (failed) { outcome = failed; break }
        if (!g) { outcome = { status: 'error', reason: `${role}: no gate attempt left (attempt ${n.attempt})` }; break }
        if (!g.pass) { outcome = { status: 'error', reason: `gate breach after ${role}: ${g.failures.join('; ')}` }; break }
      }
      const p = await step('pin', { iteration: n.iteration })
      if (!p.ok) { outcome = stepFail('pin', p); break }
      rec.evaluated_sha = p.sha
      let accEval = []
      if (ACCEPTANCE && !p.skip_evaluator) {
        // r19 §8.1: the driver pre-runs the pinned pack at the pin.
        const ar = await stepLong('acceptance-run', Object.assign({ iteration: n.iteration, sha: p.sha }, accFlags()))
        if (!ar.ok) { outcome = stepFail('acceptance-run', ar); break }
        const st = accStop(ar)
        if (st) { outcome = st; break }
        accTake(ar, 'acceptance-run')
        rec.acceptance = Object.assign(rec.acceptance || {}, {
          prerun: { passed: ar.passed, failed: ar.failed, unavailable: ar.unavailable, total: ar.total } })
        log(`iteration ${n.iteration}: frozen acceptance pre-run ${ar.passed}/${ar.total} PASS` +
          (ar.unavailable ? `, ${ar.unavailable} UNAVAILABLE` : ''))
        accEval = ['', ar.text, '', accFragment(ACC_EVALUATOR_FRAGMENT).replace(/\n+$/, '')]
      }
      if (!p.skip_evaluator) {
        const ev = await runAgentTwice(`evaluator it${n.iteration}`, evaluatorPrompt(n, p, accEval), {
          agentType: 'trio-evaluator',
          model: MODELS.evaluator,
          effort: 'high',
        })
        if (ev === null) { outcome = { status: 'error', reason: 'evaluator agent failed twice' }; break }
      }
      const ap = ACCEPTANCE
        ? await stepLong('apply', Object.assign({ iteration: n.iteration, attempt: p.evaluator_attempt }, accFlags()))
        : await step('apply', { iteration: n.iteration, attempt: p.evaluator_attempt })
      if (!ap.ok) { outcome = stepFail('apply', ap); break }
      if (ACCEPTANCE && ap.acceptance) {
        accTake(ap, 'apply')
        const r = ap.acceptance
        rec.acceptance = Object.assign(rec.acceptance || {}, {
          verdict_in: r.verdict_in || null, ship_refused: !!r.ship_refused, ship_gate: r.ship_gate || null })
        if (r.ship_refused) {
          accLog.ship_refused.push({ iteration: n.iteration, became: ap.verdict })
          log(`iteration ${n.iteration}: the frozen-acceptance gate refused the SHIP (verdict becomes ${ap.verdict})`)
        }
      }
      Object.assign(rec, { verdict: ap.verdict, scope: ap.scope, bound: ap.bound })
      log(`iteration ${n.iteration}: VERDICT ${ap.verdict}${ap.scope ? ' scope=' + ap.scope : ''} -> ${ap.status}`)
      if (ap.stop) {
        outcome = {
          status: ap.status, code: ap.code, verdict: ap.verdict,
          commit_shas: ap.commit_shas,
          human_check: ap.verdict === 'SHIP' ? null : ap.human_check,
          retirement_fold: ap.retirement_fold || null,
          reason: 'verdict',
        }
        break
      }
    }
  }
} catch (e) {
  outcome = { status: e && e.trioStatus ? e.trioStatus : 'error', reason: String(e && e.message ? e.message : e) }
}

phase('Finish')
let end = null
if (began) {
  try {
    end = await step('end', accFlags(), true)
  } catch (e) {
    end = { ok: false, error: String(e && e.message ? e.message : e) }
  }
}
if (end && end.ok) {
  // v01 item 2 (fix): `end` removes only the scratch dirs and Evaluator pin
  // worktrees the helper's ownership ledger lists for this execution;
  // anything else under .claude/worktrees/ is reported, never removed.
  if ((end.scratch_removed || []).length) log(`end removed run scratch: ${end.scratch_removed.join(', ')}`)
  for (const k of end.scratch_kept || []) log(`end could not remove scratch ${k.path}: ${k.reason}`)
  for (const k of end.eval_worktrees_kept || []) log(`end kept eval worktree ${k.worktree}: ${k.reason}`)
  if ((end.scratch_left || []).length) log(`scratch not created by this run, left in place: ${end.scratch_left.join(', ')}`)
  if ((end.eval_worktrees_left || []).length) log(`eval worktrees not owned by this run, left in place: ${end.eval_worktrees_left.join(', ')}`)
}

const RESULT = {
  status: outcome.status,
  verdict: outcome.verdict || null,
  code: outcome.code === undefined ? null : outcome.code,
  reason: outcome.reason,
  iteration: end && end.ok ? end.iteration : (iterations.length ? iterations[iterations.length - 1].iteration : null),
  commit_shas: outcome.commit_shas || [],
  conflicts: outcome.conflicts || [],
  role_denials: roleDenials,
  human_check: outcome.human_check || null,
  retirement_fold: outcome.retirement_fold || null,
  // A held (or failed) `end` is surfaced: the loop outcome stands, but the
  // lock was not released (eval-native-v0 F10).
  held_step: outcome.held_step || (end && !end.ok && end.held ? 'end' : null),
  end_error: end && !end.ok ? (end.error || 'end failed') : null,
  iterations,
  agents_used: agentsUsed,
  max_agents: MAX_AGENTS,
  token_budget: TOKEN_BUDGET,
  run_token: TOKEN,
  mailbox: MAILBOX,
  lock: end && end.ok ? end.lock : 'not_released',
  dangling_worktrees: end && end.ok ? end.dangling_worktrees : [],
  eval_worktrees_removed: end && end.ok ? (end.eval_worktrees_removed || []) : [],
  scratch_removed: end && end.ok ? (end.scratch_removed || []) : [],
  scratch_left: end && end.ok ? (end.scratch_left || []) : [],
  reclaimed_builders: B.reclaimed || null,
}
if (ACCEPTANCE) {
  // r19: the run's frozen-acceptance facts (additive; the dashboard reads
  // them from .native-result.json).
  RESULT.acceptance = Object.assign({ enabled: true },
    end && end.ok && end.acceptance ? end.acceptance : (ACC || {}), {
      coverage_refusals: accLog.coverage_refusals, replanned: accLog.replanned,
      ship_refused: accLog.ship_refused, author_attempts: accLog.author_attempts,
    })
}
return RESULT

export const meta = {
  name: 'trio-native',
  description: 'Claude-native lockstep Trio loop: Lead plan, driver-owned isolated builder waves, Lead integrate, commit gate, pinned Evaluator, verdict; decisions by trio_native_step.py',
  whenToUse: 'Run a lockstep Trio loop on an initialized mailbox without Omnigent. args: {mailbox, max_iterations, [max_agents], [token_budget]}. Launch with --settings {"worktree":{"baseRef":"head"}}.',
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
const MAILBOX = A.mailbox.replace(/\/+$/, '')
const MAX_ITERATIONS = Number.isInteger(A.max_iterations) && A.max_iterations > 0 ? A.max_iterations : 4
// Opt-in caps only (user decision): no agent cap and no usage budget unless
// passed. max_iterations stays the loop's normal bound.
const MAX_AGENTS = Number.isInteger(A.max_agents) && A.max_agents > 0 ? A.max_agents : null
const TOKEN_BUDGET = typeof A.token_budget === 'number' && A.token_budget > 0 ? A.token_budget : null
const TOKEN = typeof A.run_token === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(A.run_token)
  ? A.run_token
  : 'trio-native-' + MAILBOX.split('/').filter(Boolean).slice(-2).join('-').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 48)
const MODELS = Object.assign({
  lead: 'claude-opus-5-5',
  evaluator: 'claude-opus-5-5',
  builder: 'claude-sonnet-5',
  repair: 'claude-sonnet-5',
  step: 'claude-sonnet-5',
}, (A.models && typeof A.models === 'object') ? A.models : {})
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
  begin: ['repo', 'iteration', 'status', 'phase', 'lock_owner'],
  next: ['action', 'iteration'],
  dispatch: ['head', 'wave'],
  builders: ['accepted', 'refused', 'merge'],
  cleanup: ['removed', 'kept', 'dropped'],
  gate: ['pass', 'failures', 'final', 'status'],
  pin: ['sha', 'evaluator_attempt', 'context_block', 'skip_evaluator'],
  apply: ['verdict', 'stop', 'status', 'code', 'commit_shas', 'bound'],
  end: ['lock', 'dangling_worktrees'],
}

// A harness permission denial is recognised by the harness's own wording
// ("Permission to use Bash … has been denied", "Permission for this action
// was denied by the Claude Code auto mode classifier", "… requested
// permissions to use X, but you haven't granted it yet"); anything else — a
// step agent declining in its own words, even ones like "not allowed",
// "permission" or "denied" — is an error, not a hold (eval-native-v0b N1).
const DENIAL_RE = /Permission to use \w+[\s\S]*?(?:has been|was) denied|Permission for this action (?:has been|was) denied|denied by the Claude Code auto mode classifier|requested permissions? to use \w+[\s\S]*?haven't granted/i

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
  `(\`cat > ${MAILBOX}/REPORT.md <<'EOF'\` … \`EOF\`) or \`printf '%s\\n' '<line>' >> ${MAILBOX}/LOG.md\` — never the ` +
  'Write tool: the harness refuses report-file writes from workflow subagents.'

// ------------------------------------------------------------- plumbing
let agentsUsed = 0
let seq = 0

function shq(s) {
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
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
  const need = res.ok === true ? (REQUIRED[op] || []) : ['error']
  const missing = need.filter(k => !(k in res))
  if (missing.length) return { kind: 'retry', why: `result lacks ${missing.join(', ')}` }
  return { kind: 'ok', res }
}

async function step(op, extra, reserved) {
  seq += 1
  const nonce = `${TOKEN}/${seq}/${op}`
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
    const r = await agent(prompt, {
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

function header(role, n) {
  return [
    `MAILBOX OVERRIDE: this run uses \`${MAILBOX}/\` as the loop mailbox — every \`loop/\` path in the instructions below resolves to \`${MAILBOX}/\`.`,
    '',
    `You are the trio-${role} for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX}. Product repo: ${B.repo || '(git toplevel of the mailbox)'}.`,
    NOT_ROUTER,
    REPORT_DENIALS,
  ]
}

function leadPlanPrompt(n) {
  return header('lead', n).concat([
    '',
    'PLAN CALL (driver-owned builders). You have no Agent tool in this workflow: the driver spawns one `trio-builder` ' +
    '(Sonnet) per slice you return, each in its own git worktree forked from your checkout\'s HEAD, runs slices with ' +
    'pairwise-disjoint `writes:` concurrently, and then calls you again to integrate. So in this call:',
    '- Read GOAL.md, STATE.md, the last VERDICT.md and the code; update PLAN.md (with its `slices:` block).',
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
    MAILBOX_WRITES,
    '',
    'Final output: the structured plan.',
  ]).join('\n')
}

function builderPrompt(n, s, head) {
  return [
    `You are the trio-builder for slice \`${s.id}\` of iteration ${n.iteration} of a Trio loop driven by the trio-native workflow.`,
    NOT_ROUTER,
    REPORT_DENIALS,
    '',
    'Your cwd is an isolated git worktree created for you. Before anything else:',
    `1. Run \`pwd\`, \`git rev-parse HEAD\` and \`git status --porcelain\`.`,
    `2. Leftovers are untrusted. A resumed run re-creates a killed builder's worktree at the same path, with its ` +
    `uncommitted files and commits. If HEAD is ${head} or a descendant of it (\`git merge-base --is-ancestor ${head} ` +
    `HEAD\`) and HEAD differs from it or the status is not empty, and \`pwd\` is inside ` +
    `\`${B.repo ? B.repo + '/' : ''}.claude/worktrees/\`, discard the leftovers with \`git reset --hard ${head} && ` +
    `git clean -fd\` and start from scratch. Never run these outside that directory, and never reuse leftover work ` +
    `without redoing it.`,
    `3. Report \`git rev-parse HEAD\` (now) as \`base\`. The driver requires \`base\` = ${head} (the Lead's HEAD). ` +
    'If HEAD is not that commit or a descendant of it, do no work and return with `commits: []` and a summary saying so.',
    `The mailbox ${MAILBOX} is read-only for you (read PLAN.md and GOAL.md there if you need them). Do NOT write LOG.md ` +
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
  ].join('\n')
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
      `when REPORT.md was not rewritten; append \`- iter ${n.iteration} | lead | <summary>\` to ${MAILBOX}/LOG.md.`,
      MAILBOX_WRITES,
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
    `to ${MAILBOX}/LOG.md. The driver checks all three mechanically.`,
    MAILBOX_WRITES,
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
  return lines.join('\n')
}

function repairPrompt(n, attempt, gate) {
  const lines = header('repair', n).concat([
    '',
    `Scoped repair: VERDICT.md says ITERATE ${n.scope ? 'scope=' + n.scope : '(see its first line)'}. Fix exactly that ` +
    'scope per your role instructions; commit as `slice(<id>): fix …` (never commit `loop/` or mailbox files).',
    `Append \`- iter ${n.iteration} | repair | <one-line summary>\` to ${MAILBOX}/LOG.md for this repair (the driver's LOG ` +
    'gate requires the `| repair |` form).',
    MAILBOX_WRITES,
  ])
  if (attempt > 1) {
    lines.push('', `RETRY (attempt ${attempt} of 2): the driver's gate failed after the previous attempt:`)
    if (gate) lines.push(...gate.failures.map(f => `- ${f}`), ...(gate.detail || []).map(d => `  ${d}`))
    lines.push('Fix exactly this and finish. A second failure stops the loop with status error.')
  }
  lines.push('', 'Final message: 3–5 sentence summary for the driver.')
  return lines.join('\n')
}

function evaluatorPrompt(n, pin) {
  const attempt8 = String(pin.evaluator_attempt).slice(0, 8)
  return [
    pin.context_block,
    `You are the trio-evaluator for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX}. Product repo: ${B.repo || '(git toplevel of the mailbox)'}.`,
    NOT_ROUTER,
    REPORT_DENIALS,
    '',
    'Verify the iteration against PLAN.md acceptance criteria and write VERDICT.md per your role instructions ' +
    '(own execution first, web checks for API currency). You have no Agent tool in this workflow: do scoped ' +
    'exploration yourself.',
    `If you grade in a separate worktree, create it only as \`git -C ${B.repo || '<repo>'} worktree add --detach ` +
    `${B.repo || '<repo>'}/.claude/worktrees/eval-${n.iteration}-${attempt8} ${pin.sha}\` (never a sibling directory); ` +
    'the driver removes `.claude/worktrees/eval-*` at the end of the run.',
    `VERDICT.md must record \`attempt: ${pin.evaluator_attempt}\` and \`evaluated: ${pin.sha}\` exactly. A SHIP includes ` +
    `your retirement commit: product changes as \`slice(<id>): …\`, then the mailbox as \`loop: iteration ${n.iteration} — SHIP\`, ` +
    'with the `commit:` shas appended to VERDICT.md. Do not change product files after the pin.',
    MAILBOX_WRITES,
    '',
    'Final message: the verdict word plus a 3-sentence justification.',
  ].join('\n')
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
    const out = await agent(prompt, Object.assign({ label: tries > 1 ? `${label} (retry)` : label }, opts))
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

function overlaps(a, b) {
  const wa = productWrites(a)
  const wb = productWrites(b)
  if (!wa.length || !wb.length) return true  // unknown writes: never concurrent
  return wa.some(p => wb.some(q => p === q || p.startsWith(q + '/') || q.startsWith(p + '/')))
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

function shaMatches(a, b) {
  const x = String(a || '').trim().toLowerCase()
  const y = String(b || '').trim().toLowerCase()
  return !!x && !!y && (x === y || x.startsWith(y) || y.startsWith(x))
}

// One full Lead pass, attempt 1: plan -> waves (dispatch, builders,
// verify, integrate, cleanup). Returns null on success or a failed outcome.
async function leadPass(n, rec) {
  const plan = await runAgentTwice(`lead plan it${n.iteration}`, leadPlanPrompt(n), {
    agentType: 'trio-lead', model: MODELS.lead, effort: 'high', schema: PLAN_SCHEMA,
  })
  if (plan === null) return { status: 'error', reason: 'lead plan agent failed twice' }
  const bad = checkSlices(plan)
  if (bad) return { status: 'error', reason: `lead plan: ${bad}` }
  let waves
  try {
    waves = planWaves(plan.slices)
  } catch (e) {
    return { status: 'error', reason: `lead plan: ${e.message}` }
  }
  rec.slices = plan.slices.map(s => s.id)
  rec.waves = waves.map(w => w.map(s => s.id))
  rec.conflicts = []
  const redispatched = new Set()
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
    const d = await step('dispatch', { iteration: n.iteration, wave: k })
    if (!d.ok) return stepFail('dispatch', d)
    log(`iteration ${n.iteration} wave ${k}: ${wave.map(s => s.id).join(', ')} from ${String(d.head).slice(0, 12)}`)
    for (const s of wave) spend(`builder ${s.id}`)  // before the barrier: a cap stops cleanly
    const results = await parallel(wave.map(s => () => {
      return agent(builderPrompt(n, s, d.head), {
        label: `builder ${s.id} it${n.iteration}`,
        agentType: 'trio-builder',
        model: MODELS.builder,
        effort: 'high',
        isolation: 'worktree',
        schema: BUILDER_SCHEMA,
      })
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
    }))
    const bl = await step('builders', { iteration: n.iteration, wave: k, head: d.head, results: JSON.stringify(compact) })
    if (!bl.ok) return stepFail('builders', bl)
    if (bl.refused.length) {
      return { status: 'error', reason: 'builders refused: ' + bl.refused.map(x => x.reason).join('; ') }
    }
    const integ = await runAgentTwice(`lead integrate it${n.iteration} w${k}`,
      integratePrompt(n, k, k === waves.length, bl, compact.map((c, i) => Object.assign({}, results[i], c))), {
        agentType: 'trio-lead', model: MODELS.lead, effort: 'high', schema: INTEGRATE_SCHEMA,
      })
    if (integ === null) return { status: 'error', reason: 'lead integrate agent failed twice' }
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
    const again = conflicts.filter(c => redispatched.has(c.id))
    if (again.length) {
      return { status: 'conflict', reason: `merge conflict after a re-dispatch: ${describe(again)}`, conflicts }
    }
    const byId = new Map(wave.map(s => [s.id, s]))
    const extra = conflicts.map(c => [redispatchSlice(byId.get(c.id), c)])
    for (const c of conflicts) redispatched.add(c.id)
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
  const b = await step('begin', {})
  // eval-native-v0b N2: `end` runs whenever `begin` was attempted — a
  // garbled begin result may still have taken the lock, and `end` is safe
  // for a run that does not own it (lock `foreign`, nothing removed).
  began = true
  if (!b.ok) {
    outcome = stepFail('begin', b)
  } else {
    B = b
    log(`mailbox ${MAILBOX}: iteration ${b.iteration}, status ${b.status}, phase ${b.phase}`)
    phase('Iterate')
    while (true) {
      const n = await step('next', { max_iterations: MAX_ITERATIONS })
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
          g = await step('gate', { role, iteration: n.iteration, attempt })
          if (!g.ok) { failed = stepFail('gate', g); break }
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
      if (!p.skip_evaluator) {
        const ev = await runAgentTwice(`evaluator it${n.iteration}`, evaluatorPrompt(n, p), {
          agentType: 'trio-evaluator',
          model: MODELS.evaluator,
          effort: 'high',
        })
        if (ev === null) { outcome = { status: 'error', reason: 'evaluator agent failed twice' }; break }
      }
      const ap = await step('apply', { iteration: n.iteration, attempt: p.evaluator_attempt })
      if (!ap.ok) { outcome = stepFail('apply', ap); break }
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
    end = await step('end', {}, true)
  } catch (e) {
    end = { ok: false, error: String(e && e.message ? e.message : e) }
  }
}

return {
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
}

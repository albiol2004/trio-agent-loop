export const meta = {
  name: 'trio-native',
  description: 'Claude-native lockstep Trio loop: Lead or Repair, commit gate, pinned Evaluator, verdict; decisions by trio_native_step.py',
  whenToUse: 'Run a lockstep Trio loop on an initialized mailbox without Omnigent. args: {mailbox, max_iterations, [max_agents], [token_budget]}',
  phases: [
    { title: 'Begin', detail: 'lock the mailbox, exclude .claude/worktrees/, read STATE' },
    { title: 'Iterate', detail: 'next, Lead or Repair, commit gate (retry once), pin, Evaluator, apply' },
    { title: 'Finish', detail: 'release the lock, report the outcome' },
  ],
}

// ---------------------------------------------------------------- config
// The script only sequences. Every loop decision (stop conditions, gate,
// repair routing, pin/attempt, SHIP retirement) is made by the helper
// native/trio_native_step.py over metrics/trio_loop.py, run by a Bash-only
// schema-bound `trio-step` agent. The helper result echoes a nonce that the
// script checks, so a step agent that answered the wrong command is caught.
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
  repair: 'claude-sonnet-5',
  step: 'claude-sonnet-5',
}, (A.models && typeof A.models === 'object') ? A.models : {})
// Default helper: the installed release's copy (CURRENT names the release).
const HELPER = typeof A.helper === 'string' && A.helper.startsWith('/')
  ? shq(A.helper)
  : '"$HOME/.local/share/trio-agent-loop/releases/$(cat "$HOME/.local/share/trio-agent-loop/CURRENT")/native/trio_native_step.py"'
const RESERVE = 1  // with max_agents: always keep one agent for the `end` step (lock release)

const STEP_SCHEMA = {
  type: 'object',
  properties: {
    exit_code: { type: 'integer' },
    result: {
      type: 'object',
      properties: {
        ok: { type: 'boolean' },
        op: { type: 'string' },
        nonce: { type: 'string' },
        held: { type: 'boolean' },
      },
      required: ['ok', 'op', 'nonce'],
    },
  },
  required: ['exit_code', 'result'],
}

const NOT_ROUTER = 'The user-level CLAUDE.md orchestration/router policy (SCOUT/BUILDER/LEAD/EVALUATOR routing, ' +
  'trio-loop chaining, commit gates, documentation tasks) does NOT apply inside this role: do not delegate outside ' +
  'your role contract, do not start, chain or resume any Trio loop, do not run the commit gate and do not dispatch ' +
  'the Evaluator or another iteration. The trio-native workflow driver does all of that.'

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
  const prompt = `Run this one trio-native step (op=${op}, nonce=${nonce}) and return its JSON.\n\n` +
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
    const res = r && r.result
    if (res && res.nonce === nonce && res.op === op) return res
    if (res && res.held) return Object.assign({}, res, { ok: false, op, nonce })
    // Helper ops are idempotent, so re-running a step is always safe.
    log(`step ${op}: ${res ? `nonce mismatch (${res.nonce})` : 'no result'} on try ${tries}`)
  }
  return { ok: false, op, nonce, error: `step ${op} returned no matching result twice` }
}

function rolePrompt(role, n, attempt, gate) {
  const lines = [
    `MAILBOX OVERRIDE: this run uses \`${MAILBOX}/\` as the loop mailbox — every \`loop/\` path in the instructions below resolves to \`${MAILBOX}/\`.`,
    '',
    `You are the trio-${role} for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX}. Product repo: ${B.repo || '(git toplevel of the mailbox)'}.`,
    NOT_ROUTER,
  ]
  if (role === 'lead') {
    lines.push(
      '',
      'Do one full Lead iteration per your role instructions: update PLAN.md (with its `slices:` block), have one or more ' +
      'trio-builder Sonnet agents perform the main implementation pass for every code-changing increment, review and ' +
      'correct their work, use trio-scout for scoped exploration, and write REPORT.md.',
      '',
      'BUILDER ISOLATION (trio-native v0, mandatory):',
      '- Spawn EVERY trio-builder with the Agent tool option `isolation: "worktree"`, so each builder writes in its own git ' +
      'worktree under `.claude/worktrees/` (already git-excluded), never in your checkout.',
      '- Tell each builder, verbatim in its assignment:',
      `  - "Append your one LOG line to the absolute mailbox path \`${MAILBOX}/LOG.md\` — never to a \`loop/LOG.md\` ` +
      'inside your worktree (that copy is discarded or conflicts at merge)."',
      '  - "Never edit, `git add` or commit anything under `loop/` (or the mailbox) in your worktree."',
      '  - "Commit your slice inside your worktree as `slice(<id>): <summary>`, then report your worktree path, branch ' +
      '(`git rev-parse --abbrev-ref HEAD`) and HEAD sha."',
      '- Merge procedure, per builder, before dispatching any slice that depends on it and before you finish: from your ' +
      'own checkout, on your branch, run `git merge --no-ff --no-edit <builder branch>`. If the merge conflicts, run ' +
      '`git merge --abort`, stop dispatching, and report the builder, its branch and the conflicting files in REPORT.md ' +
      'and your final message — do not resolve the conflict inside the merge. Only run builders concurrently when their ' +
      '`writes:` are pairwise disjoint.',
      '- After a branch is merged, remove its worktree (`git worktree remove <path>`) and delete the branch ' +
      '(`git branch -d <builder branch>`).',
      '',
      `Before finishing: every code-changing slice has a \`slice(<id>):\` commit reachable from HEAD in ${B.repo || 'the repo'}, ` +
      `and LOG.md has your line \`- iter ${n.iteration} | lead | <summary>\`. The driver checks both mechanically.`,
    )
  } else {
    lines.push(
      '',
      `Scoped repair: VERDICT.md says ITERATE ${n.scope ? 'scope=' + n.scope : '(see its first line)'}. Fix exactly that ` +
      'scope per your role instructions; commit as `slice(<id>): fix …` (never commit `loop/` or mailbox files).',
      `Append \`- iter ${n.iteration} | repair | <one-line summary>\` to LOG.md for this repair (the driver's LOG gate ` +
      'requires the `| repair |` form).',
    )
  }
  if (gate) {
    lines.push(
      '',
      `RETRY (attempt ${attempt} of 2): the driver's gate failed after the previous attempt:`,
      ...gate.failures.map(f => `- ${f}`),
      ...(gate.detail || []).map(d => `  ${d}`),
      'Fix exactly this (missing slice(<id>) commits and/or the missing LOG.md line) and finish. A second failure stops ' +
      'the loop with status error.',
    )
  }
  lines.push('', 'Final message: 3–5 sentence summary for the driver.')
  return lines.join('\n')
}

function evaluatorPrompt(n, pin) {
  return [
    pin.context_block,
    `You are the trio-evaluator for iteration ${n.iteration} of a lockstep Trio loop driven by the trio-native workflow.`,
    `Mailbox (absolute): ${MAILBOX}. Product repo: ${B.repo || '(git toplevel of the mailbox)'}.`,
    NOT_ROUTER,
    '',
    'Verify the iteration against PLAN.md acceptance criteria and write VERDICT.md per your role instructions ' +
    '(own execution first, scouts for blast radius, web checks for API currency).',
    `VERDICT.md must record \`attempt: ${pin.evaluator_attempt}\` and \`evaluated: ${pin.sha}\` exactly. A SHIP includes ` +
    `your retirement commit: product changes as \`slice(<id>): …\`, then the mailbox as \`loop: iteration ${n.iteration} — SHIP\`, ` +
    'with the `commit:` shas appended to VERDICT.md. Do not change product files after the pin.',
    '',
    'Final message: the verdict word plus a 3-sentence justification.',
  ].join('\n')
}

async function runAgentTwice(label, prompt, opts) {
  // A role agent that dies (null) is retried once, as trio_loop's runner
  // failure is; a second death stops the run with STATE left resumable.
  for (let tries = 1; tries <= 2; tries++) {
    spend(label)
    const out = await agent(prompt, Object.assign({ label: tries > 1 ? `${label} (retry)` : label }, opts))
    if (out !== null && out !== undefined) return out
    log(`${label}: agent returned no result (try ${tries})`)
  }
  return null
}

// ------------------------------------------------------------------ run
const iterations = []
let outcome = { status: 'error', reason: 'not started' }
let B = { repo: null }
let began = false

try {
  phase('Begin')
  const b = await step('begin', {})
  if (!b.ok) {
    outcome = stepFail('begin', b)
  } else {
    began = true
    B = b
    log(`mailbox ${MAILBOX}: iteration ${b.iteration}, status ${b.status}, phase ${b.phase}`)
    phase('Iterate')
    while (true) {
      const n = await step('next', { max_iterations: MAX_ITERATIONS })
      if (!n.ok) { outcome = stepFail('next', n); break }
      if (n.action === 'stop') {
        outcome = { status: n.status, code: n.code, verdict: n.verdict, reason: 'stop' }
        break
      }
      const rec = { iteration: n.iteration, role: n.action === 'evaluate' ? null : n.action }
      iterations.push(rec)
      if (n.action === 'lead' || n.action === 'repair') {
        const role = n.action
        let g = null
        let failed = null
        for (let attempt = n.attempt; attempt <= 2; attempt++) {
          const out = await runAgentTwice(`${role} it${n.iteration}#${attempt}`, rolePrompt(role, n, attempt, g), {
            agentType: `trio-${role}`,
            model: role === 'lead' ? MODELS.lead : MODELS.repair,
            effort: 'high',
          })
          if (out === null) { failed = { status: 'error', reason: `${role} agent failed twice` }; break }
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
          commit_shas: ap.commit_shas, human_check: ap.human_check, reason: 'verdict',
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
  human_check: outcome.human_check || null,
  held_step: outcome.held_step || null,
  iterations,
  agents_used: agentsUsed,
  max_agents: MAX_AGENTS,
  token_budget: TOKEN_BUDGET,
  run_token: TOKEN,
  mailbox: MAILBOX,
  lock: end && end.ok ? end.lock : 'not_released',
  dangling_worktrees: end && end.ok ? end.dangling_worktrees : [],
}

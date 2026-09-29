// Minimal Workflow runtime stub for native/trio-native.js (unit tests only).
// Usage: node wf_harness.mjs <script> '<scenario json>'
// Prints {result, calls, meta} as JSON. agent() is stubbed: trio-step calls
// are answered by a tiny fake helper state machine driven by the scenario;
// role agents return a text or null.
import { readFileSync } from 'node:fs'

const [scriptPath, scenarioJson] = process.argv.slice(2)
const sc = JSON.parse(scenarioJson || '{}')
const src = readFileSync(scriptPath, 'utf8')
const body = src.replace(/^export const meta =/m, 'const meta =')
// Workflow scripts forbid Date.now()/Math.random()/argless new Date().
const guards = 'const Date = { now() { throw new Error("Date.now forbidden") } };\n' +
  'const Math = Object.assign(Object.create(globalThis.Math), { random() { throw new Error("Math.random forbidden") } });\n'
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', 'budget', 'workflow',
  guards + body + '\n')

const calls = []
const logs = []
let iteration = 0
let verdictIdx = 0
let gateIdx = 0
let wrongNonceLeft = sc.wrong_nonce_once ? 1 : 0
let lastVerdict = null
let lastHead = null
const answers = {}
const lossyLeft = Object.assign({}, sc.lossy || {})   // op -> times to drop keys
// Conflicts: sc.conflicts = {<slice id>: [files]} conflicts on the slice's
// first builder branch; sc.conflict_again = [ids] also on the re-dispatch.
let integrateIdx = 0
let lastConflictBranches = []
const builderRuns = {}

function stepResult(op, nonce, prompt) {
  const flag = name => {
    const m = prompt.match(new RegExp(`--${name} '([^']*)'`))
    return m ? m[1] : null
  }
  const base = { ok: true, op, nonce }
  switch (op) {
    case 'begin': return { ...base, mode: 'lockstep', repo: '/repo', iteration, status: 'ready', phase: 'idle',
      lock_owner: 'workflow:x', ...(sc.no_exec_id ? {} : { exec_id: sc.exec_id || '0123456789abcdef0123456789abcdef' }) }
    case 'dispatch': {
      lastHead = `H${iteration}w${flag('wave')}`
      return { ...base, iteration, wave: Number(flag('wave')), head: lastHead }
    }
    case 'builders': {
      const results = JSON.parse(flag('results'))
      const refused = results.filter(r => r.base !== flag('head') || (sc.refuse_ids || []).includes(r.id))
        .map(r => ({ id: r.id, reason: `builder ${r.id} forked from ${r.base}, not the Lead's HEAD` }))
      const accepted = results.filter(r => !refused.some(x => x.id === r.id)).map(r => r.id)
      return { ...base, accepted, refused, merge: results.filter(r => accepted.includes(r.id) && r.commits.length)
        .map(r => ({ id: r.id, branch: r.branch })) }
    }
    case 'cleanup': {
      const branches = (flag('branches') || '').split(',').filter(Boolean)
      const keptBranches = branches.filter(b => lastConflictBranches.includes(b) || (sc.kept_branches || []).includes(b))
      return { ...base,
        removed: branches.filter(b => !keptBranches.includes(b))
          .map(b => ({ branch: b, worktree: `/repo/.claude/worktrees/${b}` })),
        kept: keptBranches.map(b => ({ branch: b, reason: lastConflictBranches.includes(b)
          ? 'not merged into HEAD' : 'uncommitted changes outside the mailbox: notes.txt' })),
        dropped: (flag('drop-unmerged') || '').split(',').filter(Boolean).map(d => {
          const [old, neu] = d.split('=')
          return { branch: old, superseded_by: neu || null, dropped: true }
        }) }
    }
    case 'next': {
      if (sc.first_next_stop && iteration === 0) return { ...base, action: 'stop', iteration: 1, ...sc.first_next_stop }
      if (lastVerdict && ['SHIP', 'BLOCKED', 'NEEDS_HUMAN'].includes(lastVerdict)) {
        return { ...base, action: 'stop', status: lastVerdict.toLowerCase(), code: 0, iteration }
      }
      if (iteration >= Number(flag('max-iterations'))) {
        return { ...base, action: 'stop', status: 'max_iterations', code: 4, iteration }
      }
      iteration += 1
      const scoped = lastVerdict === 'ITERATE scope=local:app.py'
      // sc.human = {answer, notes}: the helper's verified-answer keys (only
      // present when the mailbox has HUMAN.md).
      const human = (sc.human && !scoped) ? { human_answer: sc.human.answer, human_notes: sc.human.notes || [] } : {}
      return { ...base, action: scoped ? 'repair' : 'lead', iteration, attempt: 1, scope: scoped ? 'local:app.py' : null,
        ...human }
    }
    case 'gate': {
      const pass = (sc.gates || [])[gateIdx++] ?? true
      const attempt = Number(flag('attempt'))
      const final = !pass && attempt >= 2
      return { ...base, pass, final, failures: pass ? [] : ['commit gate failed with exit 1'],
        detail: pass ? [] : ["commit gate: slice 'app' ... no slice(app): commit"], status: final ? 'error' : 'running' }
    }
    case 'pin': return { ...base, iteration, evaluator_attempt: `att${iteration}`, sha: `sha${iteration}`,
      skip_evaluator: false, context_block: `LOCKSTEP CONTEXT: attempt=att${iteration} sha=sha${iteration}\n`,
      ...(sc.human ? { human_answer: sc.human.answer, human_notes: sc.human.notes || [] } : {}) }
    case 'apply': {
      const v = sc.verdicts[verdictIdx++]
      lastVerdict = v
      const word = v.split(' ')[0]
      const stop = word !== 'ITERATE'
      const status = { SHIP: 'shipped', BLOCKED: 'blocked', NEEDS_HUMAN: 'needs_human', ITERATE: 'running' }[word]
      return { ...base, verdict: word, scope: v.includes('scope=') ? v.split('scope=')[1] : null, stop, status,
        code: stop ? { SHIP: 0, BLOCKED: 2, NEEDS_HUMAN: 5 }[word] : null, commit_shas: stop ? ['c0ffee'] : [],
        human_check: word === 'NEEDS_HUMAN' ? '1. look' : null, bound: true }
    }
    case 'end': return { ...base, lock: 'released', dangling_worktrees: [], eval_worktrees_removed: [], iteration }
  }
  return { ok: false, op, nonce, error: 'unknown op' }
}

async function agent(prompt, opts = {}) {
  calls.push({ agentType: opts.agentType, model: opts.model, effort: opts.effort, schema: !!opts.schema,
    label: opts.label, isolation: opts.isolation || null, schemaKeys: opts.schema ? Object.keys(opts.schema.properties) : null,
    prompt })
  if (opts.agentType === 'trio-step') {
    const m = prompt.match(/op=(\w+), nonce=([^)]+)\)/)
    if (sc.held_op === m[1]) {
      return { exit_code: -1, stdout: '', held: true,
        denial: 'Permission to use Bash has been denied by the auto mode classifier' }
    }
    if (sc.self_refuse_op === m[1]) {
      return { exit_code: -1, stdout: '', held: true, denial: sc.self_refuse_text || '' }
    }
    // the helper is idempotent: a re-run of the same step gets the same answer
    const res = answers[m[2]] || (answers[m[2]] = stepResult(m[1], m[2], prompt))
    if (lossyLeft[m[1]] > 0) {
      lossyLeft[m[1]] -= 1
      return { exit_code: 0, stdout: JSON.stringify({ ok: true, op: res.op, nonce: res.nonce }) }
    }
    if (wrongNonceLeft > 0) { wrongNonceLeft -= 1; return { exit_code: 0, stdout: JSON.stringify({ ...res, nonce: 'stale' }) } }
    return { exit_code: 0, stdout: JSON.stringify(res) }
  }
  const role = opts.agentType.replace('trio-', '')
  if ((sc.die || []).includes(role)) return null
  if (role === 'lead' && opts.schema && opts.schema.properties.slices) {
    return { slices: sc.plan || [{ id: 'app', brief: 'build app.py', writes: ['app.py'], reads: [], depends: [] }],
      ...(sc.plan_denials ? { denials: sc.plan_denials } : {}) }
  }
  if (role === 'lead' && opts.schema && opts.schema.properties.conflicts) {
    integrateIdx += 1
    const branches = [...prompt.matchAll(/^- ([A-Za-z0-9._-]+): branch `([^`]+)`/gm)].map(m => ({ id: m[1], branch: m[2] }))
    const conflicts = branches.filter(b => (sc.conflicts || {})[b.id] &&
      (!b.branch.includes('-r') || (sc.conflict_again || []).includes(b.id)))
      .map(b => ({ id: b.id, branch: b.branch, files: sc.conflicts[b.id] }))
    lastConflictBranches = conflicts.map(c => c.branch)
    return { merged: branches.filter(b => !lastConflictBranches.includes(b.branch)).map(b => b.id),
      conflicts: sc.integrate_hides_conflicts ? [] : conflicts,
      summary: `integrated wave ${integrateIdx}`, ...(sc.integrate_text ? { summary: sc.integrate_text } : {}) }
  }
  if (role === 'builder') {
    const id = prompt.match(/slice `([^`]+)`/)[1]
    builderRuns[id] = (builderRuns[id] || 0) + 1
    const branch = builderRuns[id] > 1 ? `worktree-${id}-r${builderRuns[id]}` : `worktree-${id}`
    const base = (sc.bad_base_ids || []).includes(id) ? 'origin-head' : lastHead
    return { id, worktree: `/repo/.claude/worktrees/${branch}`, branch, base, head: `T-${id}`,
      commits: [`T-${id}`], summary: `built ${id}` }
  }
  return (sc.role_text || {})[role] || `${role} done`
}

// spent(): 1000 output tokens per agent call so far (deterministic).
const budget = { total: null, spent: () => calls.length * 1000, remaining: () => Infinity }
const phase = t => logs.push(`phase:${t}`)
const log = m => logs.push(m)
const parallel = async thunks => Promise.all(thunks.map(t => t().catch(() => null)))
const pipeline = async () => { throw new Error('pipeline not used in v0') }
const workflow = async () => { throw new Error('workflow() not used in v0') }

const result = await run(agent, parallel, pipeline, phase, log, sc.args, budget, workflow)
const metaMatch = src.match(/^export const meta = (\{[\s\S]*?\n\})/m)
const meta = metaMatch ? new Function(`return (${metaMatch[1]})`)() : null
process.stdout.write(JSON.stringify({ result, calls, logs, meta }))

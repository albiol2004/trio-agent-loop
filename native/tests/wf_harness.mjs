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

function stepResult(op, nonce, prompt) {
  const flag = name => {
    const m = prompt.match(new RegExp(`--${name} '([^']*)'`))
    return m ? m[1] : null
  }
  const base = { ok: true, op, nonce }
  switch (op) {
    case 'begin': return { ...base, mode: 'lockstep', repo: '/repo', iteration, status: 'ready', phase: 'idle' }
    case 'next': {
      if (lastVerdict && ['SHIP', 'BLOCKED', 'NEEDS_HUMAN'].includes(lastVerdict)) {
        return { ...base, action: 'stop', status: lastVerdict.toLowerCase(), code: 0, iteration }
      }
      if (iteration >= Number(flag('max-iterations'))) {
        return { ...base, action: 'stop', status: 'max_iterations', code: 4, iteration }
      }
      iteration += 1
      const scoped = lastVerdict === 'ITERATE scope=local:app.py'
      return { ...base, action: scoped ? 'repair' : 'lead', iteration, attempt: 1, scope: scoped ? 'local:app.py' : null }
    }
    case 'gate': {
      const pass = (sc.gates || [])[gateIdx++] ?? true
      const attempt = Number(flag('attempt'))
      const final = !pass && attempt >= 2
      return { ...base, pass, final, failures: pass ? [] : ['commit gate failed with exit 1'],
        detail: pass ? [] : ["commit gate: slice 'app' ... no slice(app): commit"], status: final ? 'error' : 'running' }
    }
    case 'pin': return { ...base, iteration, evaluator_attempt: `att${iteration}`, sha: `sha${iteration}`,
      skip_evaluator: false, context_block: `LOCKSTEP CONTEXT: attempt=att${iteration} sha=sha${iteration}\n` }
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
    case 'end': return { ...base, lock: 'released', dangling_worktrees: [], iteration }
  }
  return { ok: false, op, nonce, error: 'unknown op' }
}

async function agent(prompt, opts = {}) {
  calls.push({ agentType: opts.agentType, model: opts.model, effort: opts.effort, schema: !!opts.schema,
    label: opts.label, isolation: opts.isolation || null, prompt })
  if (opts.agentType === 'trio-step') {
    const m = prompt.match(/op=(\w+), nonce=([^)]+)\)/)
    const res = stepResult(m[1], m[2], prompt)
    if (wrongNonceLeft > 0) { wrongNonceLeft -= 1; return { exit_code: 0, result: { ...res, nonce: 'stale' } } }
    return { exit_code: 0, result: res }
  }
  const role = opts.agentType.replace('trio-', '')
  if ((sc.die || []).includes(role)) return null
  return `${role} done`
}

const budget = { total: null, spent: () => 0, remaining: () => Infinity }
const phase = t => logs.push(`phase:${t}`)
const log = m => logs.push(m)
const parallel = async thunks => Promise.all(thunks.map(t => t().catch(() => null)))
const pipeline = async () => { throw new Error('pipeline not used in v0') }
const workflow = async () => { throw new Error('workflow() not used in v0') }

const result = await run(agent, parallel, pipeline, phase, log, sc.args, budget, workflow)
const metaMatch = src.match(/^export const meta = (\{[\s\S]*?\n\})/m)
const meta = metaMatch ? new Function(`return (${metaMatch[1]})`)() : null
process.stdout.write(JSON.stringify({ result, calls, logs, meta }))

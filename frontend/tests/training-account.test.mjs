import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const ts = createRequire(import.meta.url)('typescript')
const source = await readFile(new URL('../src/lib/training-account.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } })
const { mergeTrainingAccount, accountMaxDrawdown, initialTrainingSession, trainingSessionConflict, finishTrainingQuestion } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const account = { id: 'one', revision: 1, initial: 100000, equity: 100000 }
const initial = () => ({ account, points: [{ id: 1, question: 0, day: 0, value: 100000 }], accounts: [account] })

test('same-day fees replace one point and retries never duplicate the curve', () => {
  const event = { ...account, revision: 2, equity: 99900, point: { id: 2, question: 1, day: 0, value: 99900 } }
  const next = mergeTrainingAccount(initial(), event)
  const retry = mergeTrainingAccount(next, event)
  assert.equal(retry.points.length, 2)
  assert.equal(retry.points[1].value, 99900)
  assert.equal(retry.points[0].value, 100000)
})

test('different questions retain independent day zero without a capital reset', () => {
  const previous = mergeTrainingAccount(initial(), { ...account, revision: 2, point: { id: 2, question: 1, day: 0, value: 105000 } })
  const next = mergeTrainingAccount(previous, { ...account, revision: 3, equity: 105000, point: { id: 3, question: 2, day: 0, value: 105000 } })
  assert.equal(next.points.length, 3)
  assert.deepEqual(next.points.slice(1).map(p => p.question), [1, 2])
  assert.equal(next.account.equity, 105000)
})

test('another account triggers a fresh load, stale results cannot roll back funds', () => {
  assert.equal(mergeTrainingAccount(initial(), { ...account, id: 'two', point: null }), undefined)
  const previous = { ...initial(), account: { ...account, revision: 5 } }
  assert.equal(mergeTrainingAccount(previous, { ...account, revision: 2, point: null }), previous)
})

test('drawdown uses actual continuous equity instead of average question returns', () => {
  const points = [100000, 120000, 90000, 110000].map((value, id) => ({ id, question: id, day: 0, value }))
  assert.equal(accountMaxDrawdown(points, 100000), -.25)
  assert.equal(accountMaxDrawdown([], 100000), 0)
})


test('startup prefers the active account over a stale independent question, explicit history stays readable', () => {
  const active = { id: 'account', status: 'active', current_session_id: 'current' }
  assert.equal(initialTrainingSession('legacy', '', active), 'current')
  assert.equal(initialTrainingSession('legacy', 'history', active), 'history')
  assert.equal(initialTrainingSession('legacy', '', { ...active, status: 'closed' }), 'legacy')
  assert.equal(initialTrainingSession('', '', null), '')
})

test('legacy and historical questions cannot write into another current account question', async () => {
  const active = { id: 'account', status: 'active', current_session_id: 'current' }
  let calls = 0
  const finish = async v => { calls++; return { ...v, finished: true } }
  for (const view of [{ id: 'legacy', finished: false }, { id: 'old', finished: true, account: active }, undefined]) {
    await assert.rejects(() => finishTrainingQuestion(view, active, finish), /当前题/)
  }
  assert.equal(calls, 0)
  const current = { id: 'current', finished: false, account: active }
  const ended = await finishTrainingQuestion(current, active, finish)
  assert.equal(ended.finished, true)
  assert.equal(await finishTrainingQuestion(ended, active, finish), ended)
  assert.equal(calls, 1)
  assert.equal(trainingSessionConflict(current, active), false)
})

test('legacy adoption remains available without an active account, closed records stay read only', async () => {
  const legacy = { id: 'legacy', finished: false }
  assert.equal(trainingSessionConflict(legacy, null), false)
  assert.equal(trainingSessionConflict({ ...legacy, account: { status: 'closed' } }, null), true)
  assert.equal(await finishTrainingQuestion(undefined, null, async () => assert.fail()), undefined)
})

import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const { replaceEqualDeep } = require('@tanstack/react-query')
async function load(relative) {
  const source = await readFile(new URL(relative, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } })
  const module = { exports: {} }
  new Function('require', 'module', 'exports', outputText)(require, module, module.exports)
  return module.exports
}
const { mergeTrainingStep, shareTrainingView } = await load('../src/lib/training-step.ts')
const { mergeTrainingAccount } = await load('../src/lib/training-account.ts')
const bar = day => ({ date: `T+${day}`, open: 10 + day, close: 11 + day, high: 12 + day, low: 9 + day })
const initial = (window = 3) => ({ id: 'one', revision: 1, cursor: 0, price_unit: 'cny_qfq',
  candidates: [{ alias: 'A', history_window: window, bars: [bar(-2), bar(-1), bar(0)] }], indicators: ['vol'] })
const delta = previous => ({ ...previous, revision: previous.revision + 1, cursor: previous.cursor + 1,
  step_delta: true, base_revision: previous.revision,
  candidates: [{ ...previous.candidates[0], bars: [bar(previous.cursor + 1)] }] })

test('sliding and full-history advances reuse known row objects and preserve exact values', () => {
  for (const window of [3, 'all']) {
    const previous = initial(window)
    const merged = mergeTrainingStep(previous, delta(previous))
    assert.equal(merged.candidates[0].bars.length, window === 'all' ? 4 : 3)
    assert.equal(merged.candidates[0].bars[0], previous.candidates[0].bars[window === 'all' ? 0 : 1])
    assert.equal(shareTrainingView(previous, merged), merged)
    assert.deepEqual(shareTrainingView(previous, merged), replaceEqualDeep(previous, merged))
    assert.equal(previous.cursor, 0)
    assert.equal(previous.candidates[0].bars.length, 3)
  }
})

test('suspended stock replaces the last known bar without inventing a trading candle', () => {
  const previous = initial()
  const next = delta(previous)
  next.candidates[0].bars = [{ ...previous.candidates[0].bars.at(-1) }]
  next.candidates[0].tradable = false
  const merged = mergeTrainingStep(previous, next)
  assert.equal(merged.candidates[0].bars.length, 3)
  assert.equal(merged.candidates[0].bars.at(-1).date, 'T+0')
  assert.equal(merged.candidates[0].tradable, false)
  assert.equal(shareTrainingView(previous, merged), merged)
})

test('full responses keep regular structural sharing and never apply a one-bar shortcut', () => {
  const previous = initial()
  assert.equal(shareTrainingView(previous, structuredClone(previous)), previous)
  const next = structuredClone(delta(previous))
  delete next.step_delta
  assert.deepEqual(shareTrainingView(previous, next), next)
  assert.equal(mergeTrainingStep(previous, next), next)
})

test('revision, session, price-unit, pool and window changes require full recovery', () => {
  const previous = initial()
  for (const patch of [{ id: 'other' }, { base_revision: 0 }, { cursor: 3 }, { price_unit: undefined },
    { candidates: [] }, { candidates: [{ alias: 'B', history_window: 3, bars: [bar(1)] }] },
    { candidates: [{ alias: 'A', history_window: 500, bars: [bar(1)] }] }]) {
    assert.throws(() => mergeTrainingStep(previous, { ...delta(previous), ...patch }))
  }
})

test('account retries reuse the entire result and an unchanged point retains the curve', () => {
  const account = { id: 'one', revision: 1, initial: 100000, equity: 100000 }
  const point = { id: 2, question: 1, day: 0, value: 100000 }
  const previous = { account, points: [{ ...point, id: 1 }, point], accounts: [account] }
  assert.equal(mergeTrainingAccount(previous, { ...account, point }), previous)
  const result = mergeTrainingAccount(previous, { ...account, revision: 2, point: { ...point } })
  assert.equal(result.points, previous.points)
  assert.equal(result.account.revision, 2)
})

test('account correction, ordered insertion and other-account recovery remain immutable', () => {
  const account = { id: 'one', revision: 1, initial: 100000, equity: 100000 }
  const previous = { account, points: [1, 3].map(id => ({ id, question: 1, day: id, value: 100000 })), accounts: [account] }
  const result = mergeTrainingAccount(previous, { ...account, revision: 2, point: { id: 2, question: 1, day: 2, value: 99000 } })
  assert.deepEqual(result.points.map(point => point.id), [1, 2, 3])
  const corrected = mergeTrainingAccount(result, { ...account, revision: 3, point: { id: 2, question: 1, day: 2, value: 98000 } })
  assert.equal(corrected.points[1].value, 98000)
  assert.equal(result.points[1].value, 99000)
  assert.deepEqual(previous.points.map(point => point.id), [1, 3])
  assert.equal(mergeTrainingAccount(previous, { ...account, id: 'other', revision: 2, point: null }), undefined)
})

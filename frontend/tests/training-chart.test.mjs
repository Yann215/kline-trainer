import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import test from 'node:test'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const source = await readFile(new URL('../src/lib/training-chart.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } })
const module = { exports: {} }
new Function('require', 'module', 'exports', outputText)(require, module, module.exports)
const { trainingPriceLines } = module.exports
const candidate = { alias: 'A', bars: [{ date: 'T+0', close: 10 }, { date: 'T+1', close: 10.2 }] }
const position = { alias: 'A', entry_day: 0, entry_price: 10, cost_price: 10.007, pnl: 10.2/10.007-1 }

test('held stock shows the server cost and latest quote, with separated labels', () => {
  const lines = trainingPriceLines(candidate, position)
  assert.equal(lines.length, 2)
  assert.equal(lines[0].value, 10.2)
  assert.equal(lines[1].value, 10.007)
  assert.equal(lines[1].label, '成本 10.01')
  assert.equal(lines[1].labelPosition, 'insideEndBottom')
  assert.equal(lines[1].start, undefined) // A buy on the latest bar still gets a full-width line.
})

test('switching stocks, no holding, and account settlement hide the cost line', () => {
  for (const lines of [trainingPriceLines({ ...candidate, alias: 'B' }, position),
    trainingPriceLines(candidate, null), trainingPriceLines(candidate, position, true)]) {
    assert.equal(lines.length, 1)
    assert.equal(lines[0].label, '最新 10.20')
  }
})

test('unavailable or invalid cost never falls back to an incorrect entry price', () => {
  for (const cost_price of [0, -10, NaN, Infinity]) {
    assert.equal(trainingPriceLines(candidate, { ...position, cost_price }).length, 1)
  }
  assert.deepEqual(trainingPriceLines(undefined, position), [])
  assert.deepEqual(trainingPriceLines({ ...candidate, bars: [] }, position), [])
})

test('an older server derives the exact fee-inclusive cost from its PnL at each step', () => {
  const legacy = { ...position, cost_price: undefined }
  const first = trainingPriceLines(candidate, legacy)[1].value
  assert.ok(Math.abs(first-position.cost_price) < 1e-10)
  const moved = { ...candidate, bars: [{ date: 'T+2', close: 11 }] }
  const later = trainingPriceLines(moved, { ...legacy, pnl: 11/position.cost_price-1 })[1].value
  assert.ok(Math.abs(later-first) < 1e-10)
  for (const pnl of [undefined, -1, -2, Infinity, NaN]) {
    assert.equal(trainingPriceLines(candidate, { ...legacy, pnl }).length, 1)
  }
})

test('advancing quotes keeps the same holding cost without changing the input', () => {
  const before = structuredClone(candidate)
  const moved = { ...candidate, bars: [...candidate.bars, { date: 'T+2', close: 11 }] }
  const lines = trainingPriceLines(moved, position)
  assert.equal(lines[0].value, 11)
  assert.equal(lines[1].value, position.cost_price)
  assert.deepEqual(candidate, before)
})

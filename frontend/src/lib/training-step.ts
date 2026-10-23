import { replaceEqualDeep } from '@tanstack/react-query'
import type { TrainingView } from './training-types'

export function mergeTrainingStep(previous: TrainingView, incoming: TrainingView): TrainingView {
  if (!incoming.step_delta) return incoming
  if (previous.price_unit !== incoming.price_unit) throw new Error('价格口径已变化，正在恢复完整图表')
  const { step_delta: _delta, base_revision: baseRevision, ...view } = incoming
  if (view.id !== previous.id || baseRevision !== previous.revision || view.cursor !== previous.cursor + 1 ||
      view.candidates.length !== previous.candidates.length) throw new Error('训练状态已变化，正在恢复完整图表')
  return { ...view, candidates: view.candidates.map(candidate => {
    const prior = previous.candidates.find(c => c.alias === candidate.alias)
    if (!prior || prior.history_window !== candidate.history_window) throw new Error('图表范围已变化，正在恢复完整图表')
    const bars = [...prior.bars]
    for (const row of candidate.bars) {
      if (bars.at(-1)?.date === row.date) bars[bars.length - 1] = row
      else bars.push(row)
    }
    const window = candidate.history_window ?? (candidate.history_loaded ? 'all' : 120)
    return { ...candidate, bars: window === 'all' ? bars : bars.slice(-window) }
  }) }
}

export function shareTrainingView(previous: unknown, incoming: unknown): unknown {
  const prior = previous as TrainingView | undefined
  const next = incoming as TrainingView | undefined
  // A locally merged day already shares the unchanged bar objects. Default
  // positional comparison rechecks every field when a fixed window shifts.
  const mergedDay = prior && next && prior.id === next.id && prior.price_unit === next.price_unit &&
    next.revision === prior.revision + 1 && next.cursor === prior.cursor + 1 &&
    next.candidates.length === prior.candidates.length && next.candidates.every(candidate => {
      const before = prior.candidates.find(value => value.alias === candidate.alias)
      if (!before || before.history_window !== candidate.history_window || candidate.bars.length < 2) return false
      const sharedRows = new Set(before.bars)
      return candidate.bars.slice(0, -1).every(row => sharedRows.has(row))
    })
  return mergedDay ? incoming : replaceEqualDeep(previous, incoming)
}

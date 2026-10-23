import type { ChartPriceLine } from '@/components/EChartsCandlestick'
import type { TrainingView } from './training-types'

export function trainingPriceLines(
  selected: Pick<TrainingView['candidates'][number], 'alias' | 'bars'> | undefined,
  position: TrainingView['position'] | undefined,
  settled = false,
): ChartPriceLine[] {
  const latest = selected?.bars.at(-1)
  if (!latest || !Number.isFinite(latest.close) || latest.close <= 0) return []
  const lines: ChartPriceLine[] = [
    { value: latest.close, label: `最新 ${latest.close.toFixed(2)}`, color: '#3B82F6' },
  ]
  // Older running servers expose the same cost through close / cost - 1.
  // This keeps an ongoing training session usable without a service restart.
  const pnl = position?.pnl
  const cost = position?.cost_price ?? (pnl != null && Number.isFinite(pnl) && pnl > -1
    ? latest.close / (1 + pnl) : undefined)
  if (!settled && position?.alias === selected?.alias && cost != null && Number.isFinite(cost) && cost > 0) {
    lines.push({ value: cost, label: `成本 ${cost.toFixed(2)}`, color: '#F59E0B', labelPosition: 'insideEndBottom' })
  }
  return lines
}

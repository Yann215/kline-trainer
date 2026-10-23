import type { TrainingAccount, TrainingAccountData, TrainingAccountPoint, TrainingAccountSnapshot, TrainingView } from './training-types'

export function mergeTrainingAccount(previous: TrainingAccountData | undefined, incoming: TrainingAccountSnapshot): TrainingAccountData | undefined {
  if (!previous?.account || previous.account.id !== incoming.id) return undefined
  if (previous.account.revision >= incoming.revision) return previous
  const { point, ...account } = incoming
  let points = previous.points
  if (point) {
    let low = 0, high = points.length
    while (low < high) {
      const middle = (low + high) >>> 1
      if (points[middle].id < point.id) low = middle + 1
      else high = middle
    }
    const existing = points[low]
    if (!existing || existing.id !== point.id) {
      points = [...points.slice(0, low), point, ...points.slice(low)]
    } else if (existing.question !== point.question || existing.day !== point.day || existing.value !== point.value) {
      points = [...points]
      points[low] = point
    }
  }
  return { account, points,
    accounts: previous.accounts.map(value => value.id === account.id ? account : value) }
}

export function accountMaxDrawdown(points: TrainingAccountPoint[], initial: number): number {
  let peak = initial
  let drawdown = 0
  for (const point of points) {
    if (!Number.isFinite(point.value) || point.value < 0) continue
    peak = Math.max(peak, point.value)
    if (peak > 0) drawdown = Math.min(drawdown, point.value / peak - 1)
  }
  return drawdown
}


export function initialTrainingSession(stored: string, requested: string, account?: TrainingAccount | null): string {
  return requested || (account?.status === 'active' ? account.current_session_id : stored)
}

export function trainingSessionConflict(view: TrainingView | undefined, account?: TrainingAccount | null): boolean {
  if (account?.status === 'active' && account.current_session_id !== view?.id) return true
  return !!view?.account && (view.account.status === 'closed' || view.account.current_session_id !== view.id)
}

export async function finishTrainingQuestion(view: TrainingView | undefined, account: TrainingAccount | null | undefined,
  finish: (view: TrainingView) => Promise<TrainingView>): Promise<TrainingView | undefined> {
  // Never settle a different question just because an old tab was restored.
  if (trainingSessionConflict(view, account)) throw new Error('请返回账户当前题后继续；历史记录已保留')
  return view && !view.finished ? finish(view) : view
}

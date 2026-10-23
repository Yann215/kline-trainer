import type { TrainingStrategy, TrainingListItem, TrainingSummary, TrainingView, TrainingCreate, TrainingCommand } from './training-types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body.detail
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(e => e.msg).join('; ') : `请求失败 (${res.status})`)
  }
  return res.json() as Promise<T>
}

export const api = {
  trainingAccount: (verification = false, id = '') => request<import('./training-types').TrainingAccountData>(`/api/custom/training/account?verification=${verification}${id ? `&account_id=${encodeURIComponent(id)}` : ''}`),
  trainingAccountRecords: (id: string) => request<import('./training-types').TrainingAccountRecords>(`/api/custom/training/account/${encodeURIComponent(id)}/records`),
  trainingAccountStop: (id: string, payload: { revision: number; request_id: string }) => request<import('./training-types').TrainingAccountData>(`/api/custom/training/account/${encodeURIComponent(id)}/stop`, { method: 'POST', body: JSON.stringify(payload) }),
  trainingAccountExport: (id: string) => request<Record<string, unknown>>(`/api/custom/training/account/${encodeURIComponent(id)}/export`),
  trainingPreload: (payload: TrainingCreate) => request<import('./training-types').TrainingPreparation>('/api/custom/training/preloads', { method: 'POST', body: JSON.stringify(payload) }),
  trainingPrepare: (payload: TrainingCreate) => request<import('./training-types').TrainingPreparation>('/api/custom/training/preparations', { method: 'POST', body: JSON.stringify(payload) }),
  trainingPreparation: (id: string) => request<import('./training-types').TrainingPreparation>(`/api/custom/training/preparations/${encodeURIComponent(id)}`),
  trainingCancelPreparation: (id: string) => request<import('./training-types').TrainingPreparation>(`/api/custom/training/preparations/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  trainingSelectionTypes: () => request<import('./training-types').TrainingSelectionType[]>('/api/custom/training/available-selection-types'),
  trainingStrategies: () => request<TrainingStrategy[]>('/api/custom/training/strategies'),
  trainingSessions: () => request<TrainingListItem[]>('/api/custom/training/sessions'),
  trainingSummary: () => request<TrainingSummary>('/api/custom/training/summary'),
  trainingSession: (id: string) => request<TrainingView>(`/api/custom/training/sessions/${encodeURIComponent(id)}`),
  trainingReplay: (id: string, payload: { alias: string; revision: number; request_id: string; continuous_account?: boolean; account_id?: string }) => request<TrainingView>(`/api/custom/training/sessions/${encodeURIComponent(id)}/replay`, { method: 'POST', body: JSON.stringify(payload) }),
  trainingHistory: (id: string, payload: { alias: string; revision: number; request_id: string; bars?: number | null }) => request<TrainingView>(`/api/custom/training/sessions/${encodeURIComponent(id)}/history`, { method: 'POST', body: JSON.stringify(payload) }),
  trainingCreate: (payload: TrainingCreate) => request<TrainingView>('/api/custom/training/sessions', { method: 'POST', body: JSON.stringify(payload) }),
  trainingCommand: (id: string, payload: TrainingCommand) => request<TrainingView>(`/api/custom/training/sessions/${encodeURIComponent(id)}/commands`, { method: 'POST', body: JSON.stringify(payload) }),
  trainingExport: (id: string) => request<Record<string, unknown>>(`/api/custom/training/sessions/${encodeURIComponent(id)}/export`),
  trainingAnalyze: (id: string) => request<{ content: string }>(`/api/custom/training/sessions/${encodeURIComponent(id)}/analyze`, { method: 'POST' }),
}

/**
 * 集中管理所有 localStorage 持久化。
 *
 * - key 在此注册，各页面只通过 storage.xxx.get/set 调用。
 * - 类型安全，不再散落 try/catch。
 */

function kv<T>(key: string) {
  return {
    get(fallback: T): T {
      try {
        const raw = localStorage.getItem(key)
        if (raw !== null) return JSON.parse(raw) as T
      } catch { /* ignore */ }
      return fallback
    },
    set(val: T) {
      try { localStorage.setItem(key, JSON.stringify(val)) } catch { /* ignore */ }
    },
    remove() {
      try { localStorage.removeItem(key) } catch { /* ignore */ }
    },
  }
}

export const storage = {
  trainingVerificationSession: kv<string>('kline-trainer-verification-session'),
  trainingSettings: kv<Partial<import('./training-types').TrainingCreate>>('kline-trainer-settings'),
  trainingPreparation: kv<string>('kline-trainer-preparation'),
  trainingVerificationPreparation: kv<string>('kline-trainer-verification-preparation'),
  trainingSession: kv<string>('kline-trainer-active-session'),
  trainingDrafts: kv<Record<string, import('./training-types').TrainingDraft>>('kline-trainer-stock-drafts'),
  trainingVerificationDrafts: kv<Record<string, import('./training-types').TrainingDraft>>('kline-trainer-verification-stock-drafts'),
  trainingIndicators: kv<string[]>('kline-trainer-indicators'),
}

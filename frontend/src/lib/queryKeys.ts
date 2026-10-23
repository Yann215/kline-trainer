export const QK = {
  trainingPreparation: (id: string) => ['training-preparation', id] as const,
  trainingSelectionTypes: ['training-selection-types'] as const,
  trainingStrategies: ['training-strategies'] as const,
  trainingSessions: ['training-sessions'] as const,
  trainingSummary: ['training-summary'] as const,
  trainingAccount: (verification = false, id = '') => ['training-account', verification, id] as const,
  trainingAccountRecords: (id: string) => ['training-account-records', id] as const,
  trainingSession: (id: string) => ['training-session', id] as const,
}

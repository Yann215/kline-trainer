import type { OHLC } from '@/components/EChartsCandlestick'

export type TrainingMode = 'close_t' | 'open_t+1'
export type TrainingPractice = 'single' | 'screening'
export interface TrainingSelectionType { id: string; name: string; group: string; description: string }
export interface TrainingSelection { type: string; name?: string; boards: string[]; exclude_st: boolean; history_start?: string | null; history_end?: string | null }
export type TrainingAction = 'buy' | 'sell' | 'hold' | 'reject' | 'note' | 'advance' | 'scan' | 'finish' | 'cancel' | 'indicators'
export interface TrainingStrategy { id: string; name: string; available: boolean; reason: string }
export interface TrainingEvent {
  id: string; day: number; action: TrainingAction; alias: string; reason: string; tags: string[]; outcome: string
  invalidation?: string; confidence?: number; size?: number; pattern?: string
}
export interface TrainingDraft {
  reason: string; invalidation: string; tags: string[]; confidence: number; size: number; pattern?: string
}
export interface TrainingTrade {
  alias: string; entry_day: number; exit_day: number; entry_event: string; exit_event: string
  pnl: number; profit: number; fees: number; duration: number
}
export type TrainingBar = OHLC & { raw_close?: number | null }
export interface TrainingRecordGroup { id: string; name: string }
export interface TrainingFutureOutcome {
  alias: string; base_day: number; base_date: string; base_close: number | null
  final_day: number; final_date: string; final_close: number | null; final_return: number | null
  max_rise: number | null; max_fall: number | null; max_rise_day: number | null; max_fall_day: number | null
  max_rise_price: number | null; max_fall_price: number | null
  last_available_day: number | null; last_available_date: string | null
  data_status: 'complete' | 'last_available' | 'missing_start' | 'partial_prices'
}
export interface TrainingSummary {
  groups: { record_group?: TrainingRecordGroup; mode: TrainingMode; selection_type: string; selection_name: string; practice_kind: TrainingPractice
    is_replay?: boolean; sessions: number; trades: number; win_rate: number | null
    mean_trade_return: number | null; open_positions: number; settled_positions?: number; buy_tags: Record<string, number> }[]
  note: string
}
export interface TrainingListItem {
  record_group?: TrainingRecordGroup
  replay_of?: string | null; replay_number?: number
  id: string; mode: TrainingMode; cursor: number; finished: boolean; strategies: string[]; total_return: number
  verification: boolean
  selection_type: string; selection_name: string; practice_kind: TrainingPractice; question_number: number
}
export interface TrainingView {
  account?: TrainingAccountSnapshot
  account_question?: number
  account_settlement?: { kind: 'mark_to_market'; alias: string; equity: number; day: number } | null
  price_unit?: 'cny_qfq'
  record_group?: TrainingRecordGroup
  replay_of?: string | null; replay_number?: number
  step_delta?: boolean; base_revision?: number
  verification: boolean
  practice_kind: TrainingPractice; question_number: number; selection: TrainingSelection; candidate_count: number
  strategy_source: 'cached' | 'live'
  scan_interval_min: number; scan_interval_max: number
  scan_info: { day: number; pool_size: number; eligible_size?: number; selected: string[] } | null
  id: string; revision: number; mode: TrainingMode; cursor: number; horizon: number; finished: boolean
  strategies: { id: string; name: string }[]; indicators: string[]; cash: number; equity: number; total_return: number
  candidates: { alias: string; bars: TrainingBar[]; tradable: boolean; rejected: boolean; held: boolean; history_loaded?: boolean; history_window?: number | 'all' }[]
  position: { alias: string; entry_day: number; pnl: number; entry_price: number; cost_price?: number } | null
  pending: { action: 'buy' | 'sell'; alias: string; day: number } | null
  events: TrainingEvent[]; trades: TrainingTrade[]; notices: string[]; auto_scan: boolean
  costs: { commission_pct: number; stamp_tax_pct: number; slippage_bps: number }
  review?: {
    identities: { alias: string; symbol: string; name: string }[]; start: string; end: string
    stats: { trades: number; win_rate: number | null; total_return: number; max_drawdown: number; open_positions: number }
    planned_end?: string; future_outcomes?: TrainingFutureOutcome[]
    events: TrainingEvent[]
    analyses?: { content: string; prompt_version: string }[]
  }
}
export interface TrainingCreate {
  account_id?: string | null
  continuous_account?: boolean
  preload_id?: string | null
  history_bars?: 120 | 250 | 500
  verification?: boolean
  practice_kind?: TrainingPractice; selection_type?: string; boards?: string[]; exclude_st?: boolean
  strategy_source?: 'cached' | 'live'
  history_start?: string | null; history_end?: string | null; scan_interval_min?: number; scan_interval_max?: number
  previous_session_id?: string
  strategy_ids: string[]; mode: TrainingMode; horizon: number; candidate_count: number; auto_scan: boolean
  request_id: string; indicators?: string[]
}
export interface TrainingPreparation {
  preload?: boolean; preload_ready?: boolean
  id: string; status: 'queued' | 'running' | 'cancelling' | 'cancelled' | 'complete' | 'failed'
  phase: string; elapsed: number; completed: number; total: number; result?: TrainingView | null; error?: string | null
}
export interface TrainingCommand {
  continuous_account?: boolean
  pattern?: string
  price_unit?: 'cny_qfq'
  step_delta?: boolean
  action: TrainingAction; revision: number; request_id: string; alias?: string; reason?: string
  tags?: string[]; invalidation?: string; confidence?: number; size?: number; indicators?: string[]
}

export interface TrainingAccountPoint { id: number; question: number; day: number; value: number }
export interface TrainingAccount {
  id: string; verification: boolean; status: 'active' | 'closed'; revision: number
  initial: number; equity: number; total_return: number; current_session_id: string; questions: number
  created_at: string; ended_at: string | null
}
export interface TrainingAccountSnapshot extends TrainingAccount { point: TrainingAccountPoint | null }
export interface TrainingAccountData {
  account: TrainingAccount | null; points: TrainingAccountPoint[]; accounts: TrainingAccount[]
}
export interface TrainingAccountRecords {
  questions?: { id: string; question: number; cursor: number; finished: boolean; initial: number; equity: number;
    total_return: number; strategy: string; mode: TrainingMode; is_replay: boolean }[]
  events: (TrainingEvent & { session_id: string; question: number; is_replay: boolean })[]
  total_events: number; omitted_events: number
  groups: { pattern: string; is_replay: boolean; trades: number; win_rate: number; profit: number }[]
  stats: { trades: number; win_rate: number | null; replay_trades: number; settlements: number }
}

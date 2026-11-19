import request from '@/utils/request'

export interface G3HoldingEntry {
  account_id: string
  account_label: string
  code: string
  name: string
  shares: number
  available_shares: number
  cost_price?: number | null
  current_price?: number | null
  market_value?: number | null
  profit_loss?: number | null
  profit_pct?: number | null
  daily_profit?: number | null
  daily_profit_pct?: number | null
  source: string
}

export interface G3DeskCard {
  code: string
  name: string
  in_holdings: boolean
  in_watchlist: boolean
  total_shares: number
  t_lot_cap: number
  holdings: G3HoldingEntry[]
  regime: 'up' | 'sideways' | 'down' | 'unknown' | string
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  pos20?: number | null
  hi20?: number | null
  lo20?: number | null
  prev_close?: number | null
  chg5d_pct?: number | null
  params: {
    buy_band?: [number, number] | null
    sell_band?: [number, number] | null
    buy_rsi5_max?: number | null
    sell_rsi5_min?: number | null
    exit_rsi5_min?: number | null
    stop?: number | null
    source?: string
  }
  armed_sides: Record<string, { armed?: boolean; last_fire?: string | null }>
  hard_stop?: Record<string, unknown> | null
}

export interface G3DeskResponse {
  as_of: string
  trade_date: string
  accounts_summary: {
    total_assets?: number | null
    total_market_value?: number | null
    total_cash?: number | null
    total_floating_profit?: number | null
    account_count: number
    accounts_ready?: number | null
    bridge_online?: boolean | null
    updated_at?: string | null
  }
  regime_meta: {
    file?: string | null
    generated_at?: string | null
    version?: string | null
  }
  state_meta: {
    file?: string | null
  }
  cards: G3DeskCard[]
  drafts: Array<Record<string, unknown>>
  watchlist_count: number
  holding_count: number
}

export interface G3DailyReviewResponse {
  daily_reviews: Array<{ file: string; date: string; content: string }>
  eod_summaries: Array<Record<string, unknown>>
  ledger_entries: Array<Record<string, unknown>>
}

export interface G3ShadowReviewResponse {
  rolling_summary: Record<string, unknown> | null
  daily_records: Array<Record<string, unknown>>
}

export interface G3SignalQualityResponse {
  reports: Array<{
    date: string
    file: string
    markdown: string
    summary: Record<string, unknown> | null
  }>
}

export interface G3StrategyContract {
  file: string
  strategy_id: string
  name: string
  status: string
  version?: string | null
  description?: string
  entry: Record<string, unknown>
  exit: Record<string, unknown>
  risk: Record<string, unknown>
}

export interface G3SettingsResponse {
  t_trade_rules: {
    regime_engine: string
    direction_gate: string
    uptrend_params: {
      source: string
      sell_trim_rsi5m: number
      sell_exit_rsi5m: number
      buy_normal_rsi5m: number
      buy_conviction_rsi5m: number
    }
    position_discipline: {
      t_lot_formula: string
      max_daily_triggers_per_stock: number
      cooldown_minutes: number
      lunch_gate: string
    }
  }
  l2_risk_config: Record<string, unknown>
  contract_stops: Record<string, unknown>
  strategy_contracts: G3StrategyContract[]
}

export const getG3Desk = () => request.get<unknown, G3DeskResponse>('/g3/desk')
export const syncG3WatchlistFromThs = () => request.post<unknown, Record<string, unknown>>('/g3/watchlist/sync-ths')
export const upsertG3Watchlist = (payload: { add?: Array<string | { code: string; qty?: number }>; remove?: string[] }) =>
  request.post<unknown, Record<string, unknown>>('/g3/watchlist/upsert', payload)

export const getG3DailyReview = (limit = 10) =>
  request.get<unknown, G3DailyReviewResponse>('/g3/review/daily', { params: { limit } })
export const getG3ShadowReview = (limit = 20) =>
  request.get<unknown, G3ShadowReviewResponse>('/g3/review/shadow', { params: { limit } })
export const getG3SignalQuality = (limit = 10) =>
  request.get<unknown, G3SignalQualityResponse>('/g3/review/signal-quality', { params: { limit } })

export const getG3Settings = () => request.get<unknown, G3SettingsResponse>('/g3/settings')

export const getG3MainwaveDaily = (refresh = false) =>
  request.get<unknown, Record<string, unknown>>('/g3/mainwave', {
    params: refresh ? { refresh: true } : undefined,
    timeout: 60000,
  })

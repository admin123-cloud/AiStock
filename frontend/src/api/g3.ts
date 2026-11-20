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

export interface G3LiveQuote {
  price?: number | null
  prev_close?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  change_pct?: number | null
  vwap?: number | null
  vwap_dev_pct?: number | null
  rsi5m?: number | null
  rsi15m?: number | null
  quote_ts?: string | null
  source?: string | null
}

export interface G3PriceLadderLevel {
  tier: string
  price: number
  lot: number
  note: string
}

export interface G3PriceGauge {
  min_px: number
  max_px: number
  current_px: number
  current_pct: number | null
  stop_px?: number | null
  stop_pct?: number | null
  vwap_px?: number | null
  vwap_pct?: number | null
  buy_start_pct?: number | null
  buy_end_pct?: number | null
  sell_start_pct?: number | null
  sell_end_pct?: number | null
  zone: string
  zone_label: string
  dist_to_stop_pct?: number | null
  dist_to_buy_pct?: number | null
  dist_to_sell_pct?: number | null
}

export interface G3StockDailyPlan {
  eligible: boolean
  ineligible_reason?: string | null
  action_mode: string
  action_label: string
  action_tone: 'emerald' | 'amber' | 'red' | 'blue' | 'zinc' | string
  recommended_lot: number
  can_forward_t: boolean
  can_reverse_t: boolean
  cash_note?: string | null
  price_ladder: {
    buy_levels: G3PriceLadderLevel[]
    sell_levels: G3PriceLadderLevel[]
    stop_loss?: number | null
  }
  price_gauge?: G3PriceGauge | null
  scenarios: Array<{ tag: string; tone: string; text: string }>
  alerts: Array<{ level: 'danger' | 'warn' | 'opportunity' | 'info' | string; text: string }>
  priority: number
}

export interface G3OpenTExposure {
  buy_shares: number
  buy_amount: number
  sell_shares: number
  sell_amount: number
  net_shares: number
  matched_shares: number
  realized_pnl: number
  status: 'flat' | 'long_open' | 'short_open' | string
  status_label: string
}

export interface G3TExecution {
  id: string
  trade_date: string
  ts: string
  code: string
  name: string
  side: 'BUY' | 'SELL' | string
  price: number
  shares: number
  amount: number
  account_label?: string
  note?: string
}

export interface G3MarketDirective {
  mode: string
  mode_label: string
  tone: 'emerald' | 'amber' | 'red' | string
  level: string
  breadth_date?: string | null
  above_ma5_pct?: number | null
  above_ma20_pct?: number | null
  above_ma60_pct?: number | null
  new_high_60d?: number | null
  new_low_60d?: number | null
  hl_ratio?: number | null
  summary: string
  rules: string[]
}

export interface G3DeskCard {
  code: string
  name: string
  in_holdings: boolean
  in_watchlist: boolean
  total_shares: number
  available_shares?: number
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
  effective_params?: {
    buy_band?: [number, number] | null
    sell_band?: [number, number] | null
    buy_rsi5_max?: number | null
    sell_rsi5_min?: number | null
    exit_rsi5_min?: number | null
    stop?: number | null
    source?: string
  }
  params_stale?: boolean
  params_stale_reason?: string | null
  live_quote?: G3LiveQuote
  daily_plan?: G3StockDailyPlan
  open_t_exposure?: G3OpenTExposure | null
  armed_sides: Record<string, { armed?: boolean; last_fire?: string | null }>
  hard_stop?: Record<string, unknown> | null
}

export interface G3DeskResponse {
  as_of: string
  today?: string
  trade_date: string
  is_stale_regime?: boolean
  plan_meta?: {
    source?: string
    date?: string | null
    generated_at?: string | null
  }
  market_directive?: G3MarketDirective
  intraday_summary?: {
    execution_count: number
    open_exposure_codes: number
    open_exposure_shares: number
    realized_t_pnl: number
    eligible_count: number
    ineligible_count: number
    stale_param_count: number
    stop_alert_count: number
  }
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
  executions?: G3TExecution[]
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

export const getG3Desk = (refreshQuotes = false) =>
  request.get<unknown, G3DeskResponse>('/g3/desk', {
    params: refreshQuotes ? { refresh_quotes: true } : undefined,
  })
export const generateG3DailyPlan = () =>
  request.post<unknown, { status: string; date: string; generated_at: string; codes_count: number; errors: Record<string, string> }>('/g3/desk/generate-plan', {}, { timeout: 30000 })
export const recordG3TExecution = (payload: {
  code: string
  name?: string
  side: 'BUY' | 'SELL'
  price: number
  shares: number
  account_label?: string
  note?: string
}) => request.post<unknown, { status: string; entry: G3TExecution }>('/g3/desk/execution', payload)
export const deleteG3TExecution = (execId: string) =>
  request.post<unknown, { status: string; deleted_id: string }>('/g3/desk/execution/delete', { exec_id: execId })
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

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
  order_type?: 't_trade' | 'base_rebalance' | string
  price: number
  shares: number
  amount: number
  account_label?: string
  note?: string
}

export interface G3PrefillCheckResponse {
  schema: string
  draft_id: string
  created_at: string
  approved: boolean
  execution_mode: string
  in_trading_session: boolean
  order: {
    account_id: string
    code: string
    name: string
    side: string
    order_type: string
    order_type_label: string
    price: number
    shares: number
    amount: number
  }
  checks: Array<{ check: string; ok: boolean; hard: boolean; detail: string }>
  blockers: string[]
  warnings: string[]
  summary: string
}

export interface G3TradingCoachResponse {
  schema: string
  as_of: string
  window_days: number
  from_date: string
  to_date: string
  grade: string
  metrics: {
    avg_discipline_score?: number | null
    violations_total: number
    stop_ignored_codes: number
    knife_catch_total: number
    off_plan_total: number
    unmatched_drafts_total: number
    t_trade_pnl: number
    estimated_stop_drag_pnl: number
  }
  bleeding_points: Array<{
    tag: string
    severity: 'critical' | 'high' | 'medium' | string
    title: string
    detail: string
    impact_estimate?: number | null
    dates: string[]
  }>
  next_week_red_lines: string[]
  agent_notes?: {
    date: string
    generated_at: string
    agent_name: string
    headline: string
    weekly_diagnosis: string
    red_lines: string[]
  } | null
  recent_daily_discipline: Array<{
    date: string
    discipline_score?: number | null
    violations_total: number
    unmatched_drafts: number
    t_trade_pnl: number
    estimated_stop_drag: number
  }>
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

export interface G3AiSentinel {
  code6: string
  risk_light: 'green' | 'yellow' | 'red' | string
  forbid_forward_t: boolean
  catalyst_score: number
  catalyst_type: string
  risk_flags: string[]
  one_line_thesis: string
  source: string
  decision_date?: string
  updated_at?: string
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
  ai_sentinel?: G3AiSentinel | null
}

export interface G3NotifyConfig {
  enabled: boolean
  channel: 'ilink_bridge' | 'pushplus' | 'wecom_webhook' | 'serverchan' | 'qq_email' | string
  ilink_bridge_url?: string
  ilink_bridge_online?: boolean
  ilink_account_id?: string
  ilink_sdk_available?: boolean
  has_pushplus_token: boolean
  pushplus_token_masked: string
  has_wecom_webhook: boolean
  wecom_webhook_masked: string
  has_serverchan_sendkey: boolean
  serverchan_sendkey_masked: string
  qq_email_to: string
  smtp_ready: boolean
  only_trading_hours: boolean
  notify_on_draft: boolean
  notify_on_stop_alert: boolean
  updated_at?: string | null
}

export interface G3MarketRegimeGate {
  schema: string
  as_of: string
  ref_date: string
  regime: 'offensive' | 'balanced' | 'defensive' | string
  regime_label: string
  tone: 'emerald' | 'amber' | 'red' | string
  degraded: boolean
  target_position_cap: number
  target_position_cap_pct: number
  allow_new_mainwave: boolean
  action_summary: string
  reasons: string[]
  warnings: string[]
  r51_waterline: {
    actual_ratio?: number | null
    actual_pct?: number | null
    target_cap_ratio: number
    target_cap_pct: number
    gap_ratio?: number | null
    gap_pct?: number | null
    max_step_pct: number
    status: 'overweight' | 'underweight' | 'aligned' | 'unknown' | string
    advice: string
  }
  sources: {
    breadth: {
      status: 'fresh' | 'stale' | 'missing' | string
      date?: string | null
      level?: string
      above_ma20_pct?: number | null
    }
    market_mood: {
      status: 'fresh' | 'stale' | 'missing' | string
      date?: string | null
      mode?: string
      limit_up_count?: number
      limit_down_count?: number
      broke_count?: number
      seal_rate?: number | null
      max_streak?: number
      mood_state?: string
      mood_label?: string
    }
    index_r51: {
      status: 'fresh' | 'stale' | 'missing' | string
      date?: string | null
      code?: string
      raw_regime?: string
      confirmed_regime?: string
      streak_days?: number
      close?: number | null
      index_mom60?: number | null
    }
    turnover_forecast: {
      status: 'fresh' | 'stale' | 'missing' | string
      mode?: 'intraday_forecast' | 'daily_actual' | 'intraday_forecast_stale' | 'missing' | string
      trade_date?: string | null
      forecast_trillion?: number | null
      ma5_trillion?: number | null
      vol_ratio_5d?: number | null
      note?: string | null
    }
  }
}

export interface G3AttributionDailyRow {
  date: string
  total_assets?: number | null
  market_value?: number | null
  position_pct?: number | null
  benchmark_ret_pct?: number | null
  total_pnl?: number | null
  beta_pnl?: number | null
  alpha_pnl?: number | null
  t_trade_pnl: number
  t_round_trips: number
  discipline_score?: number | null
  violations_total: number
  unmatched_drafts: number
  estimated_stop_drag: number
  has_account_snapshot: boolean
  has_ledger: boolean
}

export interface G3AttributionResponse {
  schema: string
  as_of: string
  window_days: number
  from_date: string
  to_date: string
  benchmark: {
    code: string
    name: string
    matched_days: number
  }
  summary: {
    coverage_days: number
    account_snapshot_days: number
    ledger_days: number
    total_pnl?: number | null
    beta_pnl?: number | null
    alpha_pnl?: number | null
    t_trade_pnl: number
    t_round_trips: number
    avg_discipline_score?: number | null
    violations_total: number
    unmatched_drafts_total: number
    estimated_stop_drag_pnl: number
  }
  friction_events: Array<{
    date: string
    type: string
    code: string
    label: string
    held_shares?: number | null
    estimated_drag_pnl?: number | null
  }>
  daily_rows: G3AttributionDailyRow[]
  data_notes: string[]
}

export interface G3AiScreeningItem {
  code: string
  code6?: string
  name?: string
  industry?: string
  risk_light: 'green' | 'yellow' | 'red' | string
  risk_flags: string[]
  catalyst_score: number
  catalyst_type: string
  one_line_thesis: string
  source: 'agent_enriched' | 'rule_baseline' | string
  updated_at?: string
}

export interface G3AiScreeningSummary {
  schema: string
  decision_date?: string
  mode: 'agent_enriched' | 'rule_baseline' | string
  agent_enriched_count: number
  total_candidates: number
  counts: { green: number; yellow: number; red: number }
  allow_new_mainwave: boolean
  market_gate_regime?: string | null
  gate_notice: string
  agent_summary_note?: string | null
  generated_at?: string
  top_picks: Array<{
    code?: string
    name?: string
    industry?: string
    score?: number | string | null
    resonance?: number | string | null
    stage?: string
    risk_light: string
    catalyst_score: number
    catalyst_type: string
    one_line_thesis: string
    source: string
  }>
}

export interface G3PremarketBrief {
  schema: string
  date: string
  generated_at: string
  source: string
  market_stance: {
    regime: string
    regime_label: string
    tone: string
    allow_new_mainwave: boolean
    target_cap_pct: number
    actual_pct?: number | null
    waterline_status?: string | null
    waterline_advice?: string | null
  }
  core_themes: Array<{
    industry: string
    candidate_count: number
    green_count: number
    avg_score: number
    representatives: string[]
    strength_score: number
  }>
  top_picks: G3AiScreeningSummary['top_picks']
  overnight_notes: string[]
  agent_thesis?: string | null
  action_checklist: string[]
  markdown_text: string
}

export interface G3StrategyLeg {
  leg_id: 'offense' | 'defense_etf' | 't_enhance' | string
  name: string
  role: string
  target_pct: number
  base_pct: number
  status: 'active' | 'throttled' | 'halted' | 'cash_defense' | 'observe_only' | 'review_needed' | string
  status_label: string
  contracts_count: number
  contracts: Array<{
    file: string
    strategy_id: string
    name: string
    leg: string
    status: string
    version?: string | null
    description?: string
  }>
  action_route: string
  action_label: string
  feedback_note: string
}

export interface G3DefenseEtfSignal {
  index_code: string
  index_name: string
  etf_code: string
  etf_name: string
  style: string
  as_of_date?: string | null
  signal: 'long_trend' | 'neutral' | 'cash_defense' | 'missing' | string
  signal_label: string
  close?: number | null
  ma20?: number | null
  ma60?: number | null
  ret_20d_pct?: number | null
  ann_vol_20d_pct?: number | null
  vol_target_weight_pct: number
}

export interface G3StrategyMatrix {
  schema: string
  as_of: string
  regime: string
  regime_label: string
  allow_new_mainwave: boolean
  defense_auto_cash: boolean
  allocation: {
    offense_pct: number
    defense_etf_pct: number
    t_enhance_pct: number
    cash_pct: number
    total_invest_cap_pct: number
    actual_position_pct?: number | null
    waterline_status?: string | null
  }
  legs: G3StrategyLeg[]
  defense_etf_signals: G3DefenseEtfSignal[]
  r40_shadow_progress: Record<string, unknown>
  governance_pipeline: Array<{ stage: string; rule: string; status: string }>
  contracts_total: number
}

export interface G3PrefillBridgeStatus {
  online: boolean
  state: string
  heartbeat_at?: string | null
  pid?: number | null
}

export interface G3PrefillDispatchResponse {
  status: 'queued' | 'already_queued' | 'already_processed' | string
  draft_id: string
  mode: string
  allow_submit: boolean
  bridge?: G3PrefillBridgeStatus
  message?: string
  result?: Record<string, unknown> | null
}

export interface G3PrefillStatusResponse {
  draft_id: string
  state: 'queued' | 'done' | 'not_found' | string
  bridge?: G3PrefillBridgeStatus
  result?: {
    ok?: boolean
    status?: string
    reason?: string
    message?: string
    draft_id?: string
    finished_at?: string
    [key: string]: unknown
  } | null
}

export interface G3SyncFillsResponse {
  status: string
  trade_date: string
  broker_fills_found: number
  imported: number
  idempotent_skipped: number
  entries: G3TExecution[]
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
  market_regime_gate?: G3MarketRegimeGate
  strategy_allocation?: {
    allocation?: G3StrategyMatrix['allocation']
    defense_auto_cash?: boolean
    legs?: Array<{
      leg_id: string
      name: string
      target_pct: number
      status: string
      status_label: string
      action_route: string
    }>
  } | null
  premarket_brief?: G3PremarketBrief | null
  prefill_bridge?: G3PrefillBridgeStatus
  fill_sync_meta?: {
    imported: number
    broker_fills_found: number
  }
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
  notify_config?: G3NotifyConfig
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
  strategy_matrix?: G3StrategyMatrix
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
  order_type?: 't_trade' | 'base_rebalance'
  account_label?: string
  note?: string
}) => request.post<unknown, { status: string; entry: G3TExecution }>('/g3/desk/execution', payload)
export const prefillCheckG3Order = (payload: {
  code: string
  name?: string
  side: 'BUY' | 'SELL'
  price: number
  shares: number
  order_type?: 't_trade' | 'base_rebalance'
  account_id?: string
}) => request.post<unknown, G3PrefillCheckResponse>('/g3/desk/prefill-check', payload)
export const dispatchG3PrefillOrder = (draftId: string) =>
  request.post<unknown, G3PrefillDispatchResponse>('/g3/desk/prefill-dispatch', { draft_id: draftId })
export const getG3PrefillStatus = (draftId: string) =>
  request.get<unknown, G3PrefillStatusResponse>(`/g3/desk/prefill-status/${encodeURIComponent(draftId)}`)
export const syncG3DeskFills = () =>
  request.post<unknown, G3SyncFillsResponse>('/g3/desk/sync-fills', {})
export const deleteG3TExecution = (execId: string) =>
  request.post<unknown, { status: string; deleted_id: string }>('/g3/desk/execution/delete', { exec_id: execId })
export const getG3NotifyConfig = () =>
  request.get<unknown, G3NotifyConfig>('/g3/desk/notify/config')
export const saveG3NotifyConfig = (payload: {
  enabled?: boolean
  channel?: string
  pushplus_token?: string
  wecom_webhook?: string
  serverchan_sendkey?: string
  qq_email_to?: string
  only_trading_hours?: boolean
  notify_on_draft?: boolean
  notify_on_stop_alert?: boolean
}) => request.post<unknown, { status: string; config: G3NotifyConfig }>('/g3/desk/notify/config', payload)
export const testG3WechatNotify = () =>
  request.post<unknown, { status: string; ok: boolean; channel: string; detail: string }>('/g3/desk/notify/test', {}, { timeout: 20000 })
export const syncG3WatchlistFromThs = () => request.post<unknown, Record<string, unknown>>('/g3/watchlist/sync-ths')
export const upsertG3Watchlist = (payload: { add?: Array<string | { code: string; qty?: number }>; remove?: string[] }) =>
  request.post<unknown, Record<string, unknown>>('/g3/watchlist/upsert', payload)

export const getG3MarketGate = () =>
  request.get<unknown, G3MarketRegimeGate>('/g3/market-gate')
export const generateG3PremarketBrief = (payload?: {
  overnight_notes?: string[]
  agent_thesis?: string
  source?: string
}) => request.post<unknown, G3PremarketBrief>('/g3/premarket-brief/generate', payload || {}, { timeout: 60000 })
export const pushG3PremarketBriefWechat = () =>
  request.post<unknown, { status: string; ok: boolean; date?: string; channel: string; detail: string }>('/g3/premarket-brief/push-wechat', {}, { timeout: 30000 })
export const getG3ReviewAttribution = (days = 30, benchmark = '000300.SH') =>
  request.get<unknown, G3AttributionResponse>('/g3/review/attribution', { params: { days, benchmark } })
export interface G3ForwardGroupAgg {
  n: number
  avg_pct?: number | null
  win_rate_pct?: number | null
}

export interface G3ForwardEvalResponse {
  ai_screening_forward: {
    schema: string
    as_of: string
    batches_count: number
    samples_total: number
    alpha_green_vs_red_5d_pct?: number | null
    by_light: Record<'green' | 'yellow' | 'red', {
      total_samples: number
      t5: G3ForwardGroupAgg
      t10: G3ForwardGroupAgg
      latest: G3ForwardGroupAgg
    }>
    records: Array<{
      decision_date: string
      code: string
      name: string
      industry: string
      risk_light: string
      catalyst_score: number
      base_close?: number | null
      days_elapsed: number
      ret_latest_pct?: number | null
      ret_5d_pct?: number | null
      ret_10d_pct?: number | null
      status: string
    }>
    note: string
  }
  defense_etf_shadow: {
    schema: string
    as_of: string
    available: boolean
    reason?: string
    window_days?: number
    from_date?: string | null
    to_date?: string | null
    summary?: {
      cum_ret_pct: number
      bench_cum_ret_pct: number
      excess_ret_pct: number
      max_drawdown_pct: number
      bench_max_drawdown_pct: number
      drawdown_saved_pct: number
      ret_dd_ratio?: number | null
      switch_events: number
      latest_invested_pct: number
      latest_active_etfs: string[]
    }
    daily_curve: Array<{
      date: string
      invested_pct: number
      cash_pct: number
      active_etfs: string[]
      day_ret_pct: number
      bench_day_ret_pct: number
      nav: number
      bench_nav: number
    }>
  }
}

export const getG3ReviewCoach = (days = 14) =>
  request.get<unknown, G3TradingCoachResponse>('/g3/review/coach', { params: { days } })
export const getG3ReviewForwardEval = (etfDays = 60) =>
  request.get<unknown, G3ForwardEvalResponse>('/g3/review/forward-eval', { params: { etf_days: etfDays }, timeout: 60000 })
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

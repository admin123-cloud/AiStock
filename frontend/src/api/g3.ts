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

export interface G3TradingCommandHoldingAction {
  code: string
  name: string
  total_shares: number
  available_shares: number
  cost_price?: number | null
  current_price?: number | null
  market_value: number
  profit_pct?: number | null
  regime: string
  stop_price?: number | null
  broken_stop: boolean
  ai_risk_light: string
  ai_catalyst_score?: number | null
  ai_thesis: string
  verdict: string
  verdict_label: string
  verdict_tone: string
  suggested_side: 'SELL' | 'HOLD' | string
  suggested_order_type: 'base_rebalance' | 't_trade' | string
  suggested_shares: number
  reasons: string[]
}

export interface G3PanicReboundEtfPlan {
  code: string
  name: string
  index_code: string
  index_name: string
  index_date?: string | null
  index_change_pct?: number | null
  weight_pct: number
  ref_price?: number | null
  stop_loss_price?: number | null
  take_profit_price?: number | null
  hold_days: number
}

export interface G3PanicReboundSignal {
  schema: string
  strategy_id: string
  as_of: string
  status: 'STANDBY_OFF' | 'ARMED_WAITING_REVERSAL' | 'TRIGGERED_BUY_ETF' | string
  status_label: string
  tone: string
  triggered: boolean
  armed: boolean
  headline: string
  metrics: {
    breadth_pct?: number | null
    breadth_threshold: number
    pair_avg_change_pct?: number | null
    reversal_threshold_pct: number
    cyb_change_pct?: number | null
    zz1000_change_pct?: number | null
    index_date?: string | null
  }
  exit_rules: {
    hold_days: number
    stop_loss_pct: number
    take_profit_pct: number
    exit_breadth_pct: number
  }
  evidence: {
    trades_4y: number
    win_rate_pct: number
    avg_ret_pct: number
    cum_ret_pct: number
    max_dd_pct: number
  }
  etf_plans: G3PanicReboundEtfPlan[]
}

export interface G3DailyTradingCommand {
  schema: string
  environment: {
    regime: string
    regime_label: string
    is_defensive: boolean
    actual_position_pct?: number | null
    target_cap_pct: number
    overweight_pct: number
    today_reduce_step_pct: number
    today_reduce_target_yuan: number
  }
  strategy: {
    mode: string
    active_strategy_id: string
    active_strategy_name: string
    allowed_modes: string[]
    forbidden_modes: string[]
    summary: string
  }
  holding_actions: G3TradingCommandHoldingAction[]
  panic_rebound_signal?: G3PanicReboundSignal | null
  new_entries: {
    can_open_new: boolean
    active_picks: G3StrategyRouterPick[]
    reserve_counts: { green?: number; yellow?: number; red?: number }
    reserve_top_picks: G3AiScreeningSummary['top_picks']
    red_blocked: Array<{
      code: string
      name: string
      industry: string
      score?: number | string | null
      risk_flags: string[]
      one_line_thesis: string
    }>
  }
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
  strategy_router?: G3LiveStrategyRouter | null
  trading_command?: G3DailyTradingCommand | null
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

// ---------------------------------------------------------------------------
// AI 策略挖掘工坊（Alpha Mining Studio）
// ---------------------------------------------------------------------------

export interface G3MiningTemplate {
  template_id: string
  name: string
  description: string
  target_regimes: string[]
  default_hold_days: number
  default_stop_loss: number
  default_take_profit: number
  default_rank_by: string
  max_drawdown_limit: number
}

export interface G3MiningFeatureMeta {
  key: string
  label: string
  group: string
}

export interface G3MiningSegmentMetrics {
  from_date: string
  to_date: string
  trades_count: number
  active_days: number
  win_rate_pct: number
  avg_ret_pct: number
  profit_loss_ratio: number
  cum_ret_pct: number
  ann_ret_pct: number
  bench_cum_ret_pct: number
  excess_ret_pct: number
  max_drawdown_pct: number
  sharpe: number
  calmar: number
  ex_best_month_cum_ret_pct: number
  monthly_returns: Array<{ ym: string; ret_pct: number }>
}

export interface G3MiningGateResult {
  all_passed: boolean
  passed_count: number
  total_gates: number
  mining_score: number
  decay_ratio: number
  gates: Array<{
    gate: string
    label: string
    passed: boolean
    detail: string
  }>
  failure_reasons: string[]
}

export interface G3MiningCandidateResult {
  rule: {
    rule_id: string
    name: string
    template_id: string
    target_regimes: string[]
    entry_conditions: Array<{ feature: string; op: string; value: number }>
    exit_policy: { hold_days: number; stop_loss_pct: number; take_profit_pct: number }
    top_k_per_day: number
    rank_by: string
    rank_ascending: boolean
    source: string
    thesis: string
  }
  segments: {
    is: G3MiningSegmentMetrics
    val: G3MiningSegmentMetrics
    oos: G3MiningSegmentMetrics
  }
  oos_sample_trades: Array<{
    trade_date: string
    code: string
    name: string
    industry: string
    entry_open: number
    net_ret_pct: number
    exit_day: number
  }>
  quality_gates: G3MiningGateResult
}

export interface G3MiningRunDetail {
  schema: string
  run_id: string
  created_at: string
  template_id: string
  template_name: string
  universe: {
    start_date: string
    end_date?: string | null
    min_amt_20d_wan: number
    panel_rows: number
    unique_stocks: number
    trade_days: number
  }
  splits: Record<'is' | 'val' | 'oos', { from: string; to: string }>
  summary: {
    candidates_evaluated: number
    passed_all_gates: number
    best_rule_id?: string | null
    best_mining_score: number
  }
  results: G3MiningCandidateResult[]
}

export interface G3MiningExportedContract {
  file: string
  strategy_id: string
  display_name: string
  status: string
  template_id?: string | null
  run_id?: string | null
  exported_at?: string | null
  all_gates_passed: boolean
  mining_score?: number | null
  thesis: string
  readable_conditions: string[]
  target_regimes: string[]
  exit: { hold_days?: number; hard_stop_loss_pct?: number; take_profit_pct?: number }
  oos_summary: {
    trades_count?: number | null
    win_rate_pct?: number | null
    avg_ret_pct?: number | null
    cum_ret_pct?: number | null
    excess_ret_pct?: number | null
    max_drawdown_pct?: number | null
    sharpe?: number | null
  }
}

export interface G3MiningOverviewResponse {
  templates: G3MiningTemplate[]
  features: G3MiningFeatureMeta[]
  recent_runs: Array<{
    run_id: string
    created_at: string
    template_id: string
    template_name: string
    universe: G3MiningRunDetail['universe']
    splits: G3MiningRunDetail['splits']
    summary: G3MiningRunDetail['summary']
  }>
  latest_run?: G3MiningRunDetail | null
  exported_contracts: G3MiningExportedContract[]
}

export const getG3MiningOverview = () =>
  request.get<unknown, G3MiningOverviewResponse>('/g3/mining/overview', { timeout: 30000 })

export const getG3MiningRunDetail = (runId: string) =>
  request.get<unknown, G3MiningRunDetail>(`/g3/mining/runs/${encodeURIComponent(runId)}`, { timeout: 30000 })

export const runG3MiningExperiment = (payload: {
  template_id: string
  start_date?: string
  min_amt_wan?: number
  enable_tree_miner?: boolean
  tune_exit_on_val?: boolean
  custom_name?: string
  custom_thesis?: string
  custom_regimes?: string[]
  custom_conditions?: Array<{ feature: string; op: string; value: number }>
  custom_hold_days?: number
  custom_stop_loss_pct?: number
  custom_take_profit_pct?: number
}) => request.post<unknown, G3MiningRunDetail>('/g3/mining/run', payload, { timeout: 180000 })

export const exportG3MiningContract = (payload: { run_id: string; rule_id: string }) =>
  request.post<unknown, { status: string; strategy_id: string; path: string }>('/g3/mining/export-contract', payload)

export interface G3StrategyRouterPick {
  code: string
  code_full: string
  name: string
  industry: string
  ref_price: number
  stop_loss_price: number
  take_profit_price: number
  hold_days: number
  suggested_slot_pct: number
  ret_1d_pct: number
  ret_5d_pct: number
  rs_20d_vs_hs300: number
  industry_above_ma20_pct: number
  rank_metric_name: string
  rank_metric_val?: number | null
  ai_risk_light: 'green' | 'yellow' | 'red' | 'unknown' | string
  ai_blocked: boolean
  ai_thesis: string
}

export interface G3LiveStrategyRouter {
  schema: string
  as_of: string
  current_regime: string
  regime_label: string
  allow_new_mainwave: boolean
  target_position_cap_pct: number
  active_strategy_id: string
  active_strategy_name: string
  mode: 'cash_defense' | 'active_strategy' | string
  strategy_thesis: string
  all_gates_passed?: boolean
  mining_score?: number | null
  readable_conditions: string[]
  exit_plan?: {
    hold_days: number
    stop_loss_pct: number
    take_profit_pct: number
  } | null
  data_date?: string | null
  matched_total: number
  picks: G3StrategyRouterPick[]
  action_hint: string
  routes: Record<string, string>
  available_contracts: Array<{
    strategy_id: string
    display_name: string
    all_gates_passed: boolean
    mining_score?: number | null
    target_regimes: string[]
  }>
}

export interface G3RegimeRouterResponse {
  router_config: {
    schema: string
    updated_at?: string | null
    routes: Record<string, string>
    regimes: Array<{ regime: string; label: string }>
    available_contracts: G3LiveStrategyRouter['available_contracts']
  }
  live_signal: G3LiveStrategyRouter
}

export const getG3MiningRouter = () =>
  request.get<unknown, G3RegimeRouterResponse>('/g3/mining/router', { timeout: 30000 })

export const bindG3MiningRouter = (payload: { regime: string; strategy_id: string }) =>
  request.post<unknown, { status: string; router_config: G3RegimeRouterResponse['router_config'] }>(
    '/g3/mining/router/bind',
    payload
  )

export interface G3BacktestPerfMetrics {
  cum_ret_pct: number
  ann_ret_pct: number
  max_dd_pct: number
  sharpe: number
  calmar: number
  excess_vs_hs300_pct?: number
}

export interface G3BacktestComparisonRow {
  id: string
  name: string
  role: string
  category: 'active_combo' | 'stage_strategy' | 'benchmark' | 'retired' | string
  full: G3BacktestPerfMetrics
  segments: Record<'is' | 'val' | 'oos', G3BacktestPerfMetrics>
  meta: Record<string, unknown>
}

export interface G3BacktestFiveRegimeRule {
  phase: string
  label: string
  tone: string
  enter_rule: string
  exit_switch: string
  confirm_days: string
  position_cap_pct: number
  active_strategy: string
  hist_days: number
  hist_pct: number
  fwd_5d_avg_pct: number
  fwd_5d_win_pct: number
  fwd_10d_avg_pct: number
}

export interface G3BacktestCurvePoint {
  date: string
  regime: string
  hs300: number
  all_eq: number
  pure_etf_aw: number
  core_sat_aw: number
  lowvol: number
  panic_etf: number
  retired_mw04: number
  ar45_l3b?: number
  ar45_l4?: number
  ar3_dual_ab?: number
  ar3_etf01?: number
}

export interface G3HistoricalBacktestReport {
  schema: string
  generated_at: string
  period: {
    start: string
    end: string
    trade_days: number
    regime_switches: number
  }
  splits: Record<'is' | 'val' | 'oos', { from: string; to: string }>
  five_regime_rules: G3BacktestFiveRegimeRule[]
  comparisons: G3BacktestComparisonRow[]
  curve_points: G3BacktestCurvePoint[]
  anti_hallucination_audit: Array<{
    id: string
    title: string
    status: 'PASS' | 'DISCLOSED_LIMITATION' | string
    detail: string
  }>
}

export const getG3BacktestReport = () =>
  request.get<unknown, G3HistoricalBacktestReport>('/g3/mining/backtest-report', { timeout: 60000 })

export const rebuildG3BacktestReport = () =>
  request.post<unknown, G3HistoricalBacktestReport>('/g3/mining/backtest-report/rebuild', {}, { timeout: 180000 })

// ---------------------------------------------------------------------------
// AR3 v3.0 终极双核实时雷达（50% AB混合 F3A+F2A + #01/#02 跨资产 ETF）
// ---------------------------------------------------------------------------

export interface G3AR3Config {
  active_stock_mode: 'ab_blend' | 'auto_lag_me20' | 'f2a_trend' | 'f3a_pullback'
  etf_core_mode: 'regime_focus' | 'risk_adj_mom'
  stock_budget_cap_pct: number
  enable_macro_dynamic_cap?: boolean
  effective_stock_budget_cap_pct?: number
  dynamic_cap_reason?: string
  max_slots: number
  updated_at?: string | null
  note?: string
}

export interface G3AR4MacroDecision {
  phase: 'erp_overheated' | 'heavy_support_pulse' | 'heavy_support' | 'distribution_low' | 'neutral' | string
  phase_label: string
  tone: 'red' | 'emerald' | 'amber' | 'blue' | string
  advice: string
  erp_zone: 'overheated' | 'cheap' | 'neutral' | string
  erp_label: string
  enable_dynamic: boolean
  base_stock_cap_pct: number
  effective_stock_cap_pct: number
  cap_delta_pct: number
  cap_reason: string
}

export interface G3AR4MacroSnapshot {
  available: boolean
  error?: string
  trade_date?: string
  hs300_close?: number
  hs300_pe_ttm?: number
  cn_bond_10y?: number
  us_bond_10y?: number
  erp_pct?: number
  margin_balance_yi?: number
  hs300_top3_shares_yi?: number
  hs300_top3_scale_yi?: number
  broad6_scale_yi?: number
  nt_ratio_pctile_250d?: number
  nt_share_chg_5d_pct?: number
  nt_share_chg_20d_pct?: number
  broad6_net_inflow_5d_yi?: number
  top5pct_amt_share_pct?: number
  sb_net_1d_yi?: number
  sb_net_5d_yi?: number
  sb_net_20d_yi?: number
  sb_net_60d_yi?: number
  sb_hk_status?: 'blocked' | 'boosted' | 'neutral' | string
  sb_hk_label?: string
  decision: G3AR4MacroDecision
  evidence_note?: string
}

export interface G3AR4CycleZone {
  kind: 'heavy' | 'low' | 'overheat' | string
  start: string
  end: string
  days: number
}

export interface G3AR4PulseEvent {
  date: string
  inflow_5d_yi: number
  hs300_close: number
  nt_pctile_250d: number
  erp_pct: number
  label: string
}

export interface G3AR4EtfBreakdownItem {
  code: string
  name: string
  bucket: string
  close: number
  shares_yi: number
  scale_yi: number
  shares_chg_5d_yi: number
  shares_chg_5d_pct: number
  est_inflow_5d_yi: number
}

export interface G3AR4NationalTeamCycleResponse {
  available: boolean
  error?: string
  years?: number
  date_range?: { start: string; end: string; days: number }
  latest?: G3AR4MacroSnapshot
  thresholds?: {
    heavy_pctile: number
    low_pctile: number
    pulse_5d_yi: number
    erp_overheat_pct: number
    erp_cheap_pct: number
  }
  series?: {
    dates: string[]
    hs300_close: number[]
    nt_pctile_250d: number[]
    hs300_top3_scale_yi: number[]
    broad6_inflow_5d_yi: number[]
    erp_pct: number[]
  }
  cycle_zones?: G3AR4CycleZone[]
  pulse_events?: G3AR4PulseEvent[]
  etf_breakdown?: G3AR4EtfBreakdownItem[]
  methodology?: {
    formula: string
    bottom_rule: string
    top_rule: string
    backtest_proof: string
  }
}

export interface G3AR3EtfItem {
  code: string
  ts_code: string
  name: string
  asset_class: string
  asset_class_label: string
  trade_date: string
  close: number
  ret_1d_pct: number
  ret_10d_pct: number
  ret_20d_pct: number
  ma20: number
  above_ma20: boolean
  vol_20d_pct: number
  risk_adj_mom: number
  is_qdii: boolean
  eligible: boolean
  basket_weight_pct?: number
  account_weight_pct?: number
  reason?: string
  qdii_notice?: string | null
}

export interface G3AR3StockCandidate {
  code: string
  ts_code: string
  name: string
  industry: string
  source_engine: 'F2A' | 'F3A' | 'BOTH' | string
  source_label: string
  close: number
  stop_loss_price: number
  ret_1d_pct: number
  ret_20d_pct: number
  rs_20d_pct: number
  amt_20d_yi: number
  ma10_bias_pct: number
  vol_ratio_20d: number
  notice_date: string
  report_date: string
  net_profit_yoy_pct?: number | null
  revenue_yoy_pct?: number | null
  roe_pct?: number | null
  score: number
  industry_duplicate: boolean
  slot_rank?: number | null
  suggested_weight_pct: number
}

export interface G3AR3MoneyEffectStats {
  samples: number
  active_days: number
  win_rate_pct?: number | null
  avg_5d_ret_pct?: number | null
  median_5d_ret_pct?: number | null
}

export interface G3AR3DualCoreRadarResponse {
  version: string
  generated_at: string
  regime: string
  is_offensive: boolean
  config: G3AR3Config
  stock_mode_options: Array<{ id: G3AR3Config['active_stock_mode']; label: string; short_label: string; desc: string }>
  etf_mode_options: Array<{ id: G3AR3Config['etf_core_mode']; label: string; short_label: string; desc: string }>
  evidence: Record<string, { cum_ret_pct: number; ann_ret_pct: number; max_dd_pct: number; sharpe: number; calmar: number; oos_pct: number }>
  etf_radar: {
    available: boolean
    error?: string
    latest_date?: string | null
    etf_core_mode?: string
    etf_core_label?: string
    etf_total_budget_pct?: number
    stock_budget_active_pct?: number
    panic_triggered?: boolean
    headline?: string
    targets: G3AR3EtfItem[]
    universe: G3AR3EtfItem[]
  }
  stock_radar: {
    available: boolean
    error?: string
    latest_date?: string
    is_offensive?: boolean
    stock_budget_active_pct?: number
    slot_weight_pct?: number
    max_slots?: number
    f2a_total_hits?: number
    f3a_total_hits?: number
    exec_queue?: G3AR3StockCandidate[]
    f2a_candidates?: G3AR3StockCandidate[]
    f3a_candidates?: G3AR3StockCandidate[]
    money_effect_20d?: {
      window: string
      f2a: G3AR3MoneyEffectStats
      f3a: G3AR3MoneyEffectStats
      suggested_mode: G3AR3Config['active_stock_mode']
      headline: string
    }
  }
  macro_dashboard?: G3AR4MacroSnapshot
}

export const getG3AR4NationalTeamCycle = (years = 3) =>
  request.get<unknown, G3AR4NationalTeamCycleResponse>('/g3/mining/ar4/national-team', {
    params: { years },
    timeout: 30000,
  })

export const syncG3AR4MacroData = () =>
  request.post<unknown, { status: string; sync: Record<string, unknown>; snapshot: G3AR4MacroSnapshot }>(
    '/g3/mining/ar4/sync',
    null,
    { timeout: 120000 }
  )

export const getG3AR3Radar = (forceRefresh = false) =>
  request.get<unknown, G3AR3DualCoreRadarResponse>('/g3/mining/ar3/radar', {
    params: forceRefresh ? { force_refresh: true } : undefined,
    timeout: 60000,
  })

export const updateG3AR3Config = (payload: Partial<G3AR3Config>) =>
  request.post<unknown, { status: string; config: G3AR3Config; radar: G3AR3DualCoreRadarResponse }>(
    '/g3/mining/ar3/config',
    payload,
    { timeout: 60000 }
  )

export const syncG3AR3Data = (payload?: { sync_etf?: boolean; sync_financials?: boolean }) =>
  request.post<unknown, { status: string; sync: Record<string, unknown>; radar: G3AR3DualCoreRadarResponse }>(
    '/g3/mining/ar3/sync',
    null,
    { params: payload, timeout: 180000 }
  )

export interface G3AR3SellOrder {
  code: string
  name: string
  is_etf: boolean
  side: 'SELL'
  order_type: string
  current_shares: number
  available_shares: number
  full_clear_shares: number
  suggested_shares: number
  limit_price: number
  ref_price: number
  est_amount: number
  profit_pct?: number | null
  reason: string
  blocked_t1?: boolean
}

export interface G3AR3BuyOrder {
  code: string
  name: string
  asset_kind: 'stock' | 'etf' | string
  is_etf: boolean
  side: 'BUY'
  order_type: string
  target_weight_pct: number
  source_label: string
  ref_price: number
  limit_price: number
  stop_loss_price: number
  immediate_shares: number
  est_immediate_amount: number
  post_sell_shares: number
  est_post_sell_amount: number
  suggested_shares: number
  reason: string
  qdii_notice?: string | null
}

export interface G3AR3RebalancePlanResponse {
  schema: string
  date: string
  generated_at: string
  regime: string
  regime_label: string
  is_offensive: boolean
  smooth_step: boolean
  config: G3AR3Config
  account: {
    account_id: string
    label: string
    available: boolean
    total_assets: number
    available_cash: number
    market_value: number
    position_pct: number
    snapshot_at?: string | null
    available_accounts: Array<{
      account_id: string
      label: string
      total_assets?: number | null
      available_cash?: number | null
      market_value?: number | null
      holdings_count: number
    }>
  }
  cash_flow: {
    current_available_cash: number
    est_sell_proceeds: number
    est_post_sell_cash: number
    immediate_buy_budget: number
    post_sell_buy_budget: number
  }
  sell_orders: G3AR3SellOrder[]
  buy_orders: G3AR3BuyOrder[]
  hold_keep: Array<{ code: string; name: string; shares: number; market_value: number; target_weight_pct: number; action: string; reason: string }>
  order_intents: Array<Record<string, unknown>>
  money_effect_20d?: G3AR3DualCoreRadarResponse['stock_radar']['money_effect_20d']
  execution_adapter: {
    active: string
    mode: string
    allow_submit: boolean
    qmt_ready_protocol: string
  }
}

export interface G3AR3BatchPrefillResponse {
  status: string
  account_id: string
  account_label: string
  mode: string
  allow_submit: boolean
  total_orders: number
  dispatched_count: number
  blocked_count: number
  results: Array<{
    code: string
    name: string
    side: string
    price: number
    shares: number
    draft_id?: string | null
    approved: boolean
    dispatched: boolean
    blockers: string[]
    warnings: string[]
  }>
}

export interface G3AR3ShadowReconcileResponse {
  schema: string
  account_id: string
  account_label: string
  days_tracked: number
  total_planned_orders: number
  total_executed_orders: number
  overall_completion_pct?: number | null
  avg_slippage_bps?: number | null
  daily_records: Array<{
    date: string
    regime: string
    planned_orders: number
    executed_orders: number
    completion_pct: number
    items: Array<{
      code: string
      name: string
      side: string
      plan_shares: number
      plan_price: number
      actual_shares: number
      actual_price?: number | null
      slippage_bps?: number | null
      status: string
    }>
  }>
}

export const getG3AR3RebalancePlan = (accountId = 'acct1', smoothStep = true) =>
  request.get<unknown, G3AR3RebalancePlanResponse>('/g3/mining/ar3/rebalance-plan', {
    params: { account_id: accountId, smooth_step: smoothStep, persist: true },
    timeout: 60000,
  })

export const batchPrefillG3AR3Orders = (payload: {
  account_id: string
  auto_split_l2?: boolean
  orders: Array<{ code: string; name: string; side: 'BUY' | 'SELL'; price: number; shares: number; order_type?: string }>
}) => request.post<unknown, G3AR3BatchPrefillResponse>('/g3/mining/ar3/batch-prefill', payload, { timeout: 60000 })

export const getG3AR3ShadowReconcile = (accountId = 'acct1', days = 20) =>
  request.get<unknown, G3AR3ShadowReconcileResponse>('/g3/mining/ar3/shadow-reconcile', {
    params: { account_id: accountId, days },
    timeout: 30000,
  })

export const pushG3AR3PlanWechat = (accountId = 'acct1', smoothStep = true) =>
  request.post<unknown, { status: string; wechat: Record<string, unknown>; plan_date?: string }>(
    '/g3/mining/ar3/push-plan-wechat',
    null,
    { params: { account_id: accountId, smooth_step: smoothStep }, timeout: 60000 }
  )

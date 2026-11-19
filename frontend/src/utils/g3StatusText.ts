const STATUS_LABELS = {
  NO_STATE_ROUTER_CANDIDATE: '无状态路由候选',
  NO_QUALIFIED_SHADOW_BUY: '无合格影子买入',
  HAS_QUALIFIED_SHADOW_BUY: '有合格影子买入',
  STATE_ROUTER_READY: '状态路由已就绪',
  WORKFLOW_READY: '工作流可运行',
  DATA_MISSING: '数据缺失',
  M30_NOT_CONFIRMED: '30m 未确认',
  PAPER_NOT_REPLAYED: '纸面台账未复现',
  FORMAL_GATE_LOCKED: '正式下单锁定',

  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  success: '成功',
  ok: '通过',
  pass: '通过',
  blocked: '阻断',
  idle: '空闲',
  pending: '等待中',
  pending_confirm: '等待确认',
  waiting: '等待中',
  observable: '可观察',
  executed: '已执行',
  open_shadow: '影子持有',
  shadow_current: '当前影子',
  intraday_confirmed: '盘中确认',
  confirmed_shadow_current: '已确认当前影子',
  confirmed_shadow_candidate: '已确认候选',
  qualified_shadow_buy: '合格影子买入',
  blocked_shadow_observe: '阻断观察',
  current_rebuilt_route_candidate: '当日重建候选',
  daily_candidate_no_intraday_data: '日线候选，缺少盘中确认',
  panic_wait_intraday_confirm_or_no_intraday_data: '等待盘中确认或暂无盘中数据',
  high_heat_reduce_position: '超过主升热度门槛，仅观察',
  high_heat_observe: '超过主升热度门槛，仅观察',
  institutional_mom60_gt_5_block: '主升mom60超过5%，不出票',
  institutional_index_mom60_gt_5pct_strategy_gate: '主升mom60超过5%，不出票',
  institutional_mainwave_dynamic_cooldown_pause_new_buy: '机构主升连亏动态冷却，暂停新买',
  shadow_only_current_rebuilt_requires_live_observation: '仅影子重建，需实盘观察',
}

export function statusWithZh(value) {
  if (value === null || value === undefined || value === '') return '--'
  const raw = String(value)
  const label = STATUS_LABELS[raw] || STATUS_LABELS[raw.toLowerCase()]
  if (!label) return raw
  if (raw.includes(`（${label}）`)) return raw
  return `${raw}（${label}）`
}

export function compactStatusWithZh(value) {
  const text = statusWithZh(value)
  if (text === '--') return text
  return text.replace('shadow_only_current_rebuilt_requires_live_observation', 'shadow_only_rebuilt')
}

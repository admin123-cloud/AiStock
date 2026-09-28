// 静态标签 / 配色 / 自动文案辅助（迁移自 Gen3StateAlphaRisk.vue script 区纯函数）
export type Row = Record<string, any>
export type Tone = 'success' | 'danger' | 'warning' | 'info' | 'primary'

export function truthy(value: any): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok', 'pass'].includes(value.trim().toLowerCase())
  return false
}

export function pct(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '--'
}

export function price(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

export function listText(value: any): string {
  return Array.isArray(value) ? value.join('、') : (value || '--')
}

export function gateType(status: any): Tone {
  if (status === 'pass' || status === 'ready') return 'success'
  if (status === 'warn' || status === 'watch' || status === 'manual_shadow_ready_with_warnings') return 'warning'
  if (status === 'block' || status === 'blocked') return 'danger'
  return 'info'
}

export function pretradeReviewType(action: any): Tone {
  if (action === 'manual_approved') return 'success'
  if (action === 'paper_watch') return 'primary'
  if (action === 'wait_refresh') return 'warning'
  if (action === 'skip' || action === 'reject') return 'danger'
  return 'info'
}

export function paperWatchResultLabel(result: any): string {
  const labels: Record<string, string> = {
    as_expected: '符合预期',
    buy_point_too_early: '买点偏早',
    buy_point_chasing: '买点追高',
    selection_issue: '选股隐患',
    model_switch_issue: '策略切换隐患',
    risk_exit_issue: '风控/卖点隐患',
    missed_opportunity: '可能误杀',
    invalid_signal: '信号失效',
    continue_watch: '继续观察'
  }
  return labels[result] || result || ''
}

export function paperWatchStatusType(status: any): Tone {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

export function day1PostureType(posture: any): Tone {
  if (posture === 'formal_candidate_after_manual_review') return 'success'
  if (posture === 'contract_block_observation') return 'danger'
  if (posture === 'paper_watch' || posture === 'unreviewed_to_paper_watch') return 'primary'
  if (posture === 'wait_refresh') return 'warning'
  if (posture === 'skip') return 'info'
  return 'info'
}

export function day1PostureLabel(posture: any): string {
  const labels: Record<string, string> = {
    formal_candidate_after_manual_review: '人工候选',
    contract_block_observation: '合同阻断观察',
    paper_watch: '纸面观察',
    unreviewed_to_paper_watch: '先纸面',
    wait_refresh: '待刷新',
    skip: '跳过'
  }
  return labels[posture] || posture || '--'
}

export function afterCloseStatusType(status: any): Tone {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'contract_block_observation') return 'danger'
  if (status === 'pending_after_close_review' || status === 'continue_watch') return 'warning'
  if (status === 'pending_paper_execution') return 'info'
  return 'info'
}

export function afterCloseStatusLabel(status: any): string {
  const labels: Record<string, string> = {
    pending_paper_execution: '待写执行',
    contract_block_observation: '合同阻断观察',
    pending_after_close_review: '待盘后复盘',
    validated: '已验证',
    continue_watch: '继续观察',
    issue_found: '发现隐患'
  }
  return labels[status] || status || '--'
}

export function noTradeReviewResultLabel(result: any): string {
  const labels: Record<string, string> = {
    natural_no_trade: '空仓合理',
    missed_opportunity: '可能误杀',
    data_gap: '数据缺口',
    process_gap: '流程缺口',
    continue_watch: '继续观察'
  }
  return labels[result] || result || ''
}

export function noTradeReviewStatusType(status: any): Tone {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

export function reviewBoardStatusType(status: any): Tone {
  if (status === 'validated' || status === 'pass' || status === 'ready' || status === 'covered') return 'success'
  if (status === 'ready_for_manual_live_review') return 'success'
  if (status === 'issue_found' || status === 'block' || status === 'issue_found_hold_live') return 'danger'
  if (String(status || '').startsWith('pending') || status === 'warn' || status === 'watch' || status === 'continue_watch' || status === 'waiting_previous_step') return 'warning'
  if (status === 'next_action_required') return 'warning'
  return 'info'
}

export function reviewBoardStatusLabel(status: any): string {
  const labels: Record<string, string> = {
    no_buy_observe_pending: '无票观察待处理',
    no_buy_observe_ready: '无票观察',
    manual_live_ready: '人工实盘待确认',
    ticket_review_or_formal_action_pending: '有票未放行',
    blocked_review_required: '阻断复盘',
    paper_or_manual_review_pending: '复盘确认中',
    pending: '待处理',
    pending_review: '待复盘',
    pending_observation: '待观察',
    pending_after_close_review: '待盘后复盘',
    pending_paper_execution: '待纸面执行',
    pending_evidence: '待证据',
    next_action_required: '继续下一步',
    ready_for_manual_live_review: '人工实盘复核',
    issue_found_hold_live: '隐患禁止实盘',
    observe_no_blocker: '无阻断观察',
    needs_review: '需调优复盘',
    watch_more: '继续观察',
    issue_found: '发现隐患',
    block: '阻断',
    warn: '警告',
    watch: '观察',
    continue_watch: '继续观察',
    covered: '已覆盖',
    validated: '已验证',
    pass: '通过',
    ready: '就绪',
    waiting_previous_step: '等待前置步骤'
  }
  return labels[status] || status || '--'
}

export function launchDayPlaybookStatusType(status: any): Tone {
  if (status === 'validated') return 'success'
  if (status === 'issue_found') return 'danger'
  if (status === 'continue_watch') return 'warning'
  return 'info'
}

export function launchDayPlaybookStatusLabel(status: any, label?: string): string {
  if (label) return label
  const labels: Record<string, string> = {
    validated: '符合预期',
    issue_found: '发现隐患',
    continue_watch: '继续观察',
    pending_review: '待复盘'
  }
  return labels[status] || status || '待复盘'
}

export function learningStatusType(status: any): Tone {
  if (status === 'needs_review') return 'danger'
  if (status === 'watch_more') return 'warning'
  if (status === 'validated_rule') return 'success'
  return 'info'
}

export function learningStatusLabel(status: any): string {
  const labels: Record<string, string> = {
    needs_review: '需调优',
    watch_more: '继续观察',
    validated_rule: '规则验证'
  }
  return labels[status] || status || '--'
}

export function hiddenRiskLevelType(level: any): Tone {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  if (level === 'low') return 'info'
  return 'info'
}

export function hiddenRiskLevelLabel(level: any): string {
  const labels: Record<string, string> = { high: '高', medium: '中', low: '低' }
  return labels[level] || level || '--'
}

export function reviewAxisLabel(axis: any): string {
  const labels: Record<string, string> = {
    execution_evidence: '执行证据',
    selection: '选股模式',
    model_switch: '策略切换',
    sell_point: '卖点/退出',
    buy_point: '买点',
    natural_trade_consistency: '自然一致性'
  }
  return labels[axis] || axis || '--'
}

export function executionModeType(status: any, allowed: any): Tone {
  if (truthy(allowed)) return 'success'
  if (status === 'locked') return 'info'
  if (status === 'blocked' || status === 'requires_reaudit') return 'danger'
  if (status === 'not_ready') return 'warning'
  return 'info'
}

export function holdingRefreshType(status: any): Tone {
  if (status === 'fresh_enough') return 'success'
  if (status === 'formal_refresh_required') return 'warning'
  if (status === 'observation_refresh') return 'info'
  return 'info'
}

export function naturalConsistencyType(grade: any): Tone {
  if (grade === 'smooth') return 'success'
  if (grade === 'watch') return 'warning'
  if (grade === 'strained') return 'danger'
  if (grade === 'incoherent') return 'danger'
  return 'info'
}

export function naturalConsistencyLabel(grade: any): string {
  const labels: Record<string, string> = {
    smooth: '顺畅',
    watch: '观察',
    strained: '偏拧',
    incoherent: '不顺'
  }
  return labels[grade] || grade || '--'
}

export function formalActionReviewLabel(result: any): string {
  const labels: Record<string, string> = {
    manual_done: '人工已处理',
    system_triggered: '系统动作已触发',
    blocked: '处理卡住',
    continue_watch: '继续观察'
  }
  return labels[result] || result || '--'
}

export function formalActionButtonLabel(row: Row = {}): string {
  const source = String(row?.source || '')
  const object = String(row?.object || '')
  if (source.includes('holding') || object.includes('holding') || object.includes('持仓') || /\d{6}/.test(object)) {
    return '同步持仓并复审'
  }
  if (source.includes('formal_launch') || object.includes('审计')) return '重跑审计'
  if (source.includes('readiness_gates')) return '重跑审计'
  return '查看来源'
}

export function strategyTuningReviewResultLabel(result: any): string {
  const labels: Record<string, string> = {
    validated: 'validated',
    issue_found: 'issue found',
    continue_watch: 'continue watch',
    data_gap: 'data gap',
    evidence_pending: 'evidence pending',
    defer_contract_review: 'defer contract review'
  }
  return labels[result] || result || '--'
}

export function dailyReviewChecklistResultLabel(result: any): string {
  const labels: Record<string, string> = {
    validated: '符合预期',
    issue_found: '发现隐患',
    continue_watch: '继续观察',
    blocked: '卡住/阻断',
    data_gap: '数据缺口',
    process_gap: '流程缺口'
  }
  return labels[result] || result || '--'
}

export function noTradeIssueArea(result: any): string {
  if (result === 'missed_opportunity') return 'selection_or_switch'
  if (result === 'data_gap') return 'data_confirmation'
  if (result === 'process_gap') return 'execution_process'
  return 'observation'
}

export function paperWatchIssueArea(result: any): string {
  if (result === 'buy_point_too_early' || result === 'buy_point_chasing') return 'buy_point'
  if (result === 'selection_issue') return 'selection'
  if (result === 'model_switch_issue') return 'model_switch'
  if (result === 'risk_exit_issue') return 'sell_exit'
  if (result === 'invalid_signal' || result === 'missed_opportunity') return 'signal_validation'
  return 'observation'
}

export function isCandidateOmissionReview(row: Row = {}): boolean {
  return Boolean(
    row?.review_scope === 'candidate_omission' ||
    row?.source === 'candidate_omission_checklist' ||
    row?.selection_state ||
    row?.strategy_switch_assessment ||
    row?.omission_watch_status
  )
}

export function taskToReviewRow(row: Row = {}): Row {
  return {
    ...row,
    review_key: row.review_key || row.blocking_key || row.primary_blocking_key || row.ledger_key,
    review_status: row.task_status || row.review_status,
    stage: row.window,
    required_action: row.required_action || row.action,
    review_type: row.review_type || row.object,
    posture: row.posture || row.task_status,
    evidence: row.evidence || row.acceptance,
    natural_decision: row.natural_decision || row.review_method,
    optimization_focus: row.optimization_focus || row.fallback,
    source: row.source || row.source_table,
    action_recommendation: row.action_recommendation || row.action,
    hidden_risks: row.hidden_risks || row.fallback
  }
}

export function isDay1JournalNoTradeRow(row: Row = {}): boolean {
  const key = String(row.review_key || '')
  const type = String(row.journal_type || '')
  const action = String(row.action || row.next_action || '')
  return /^\d{4}-\d{2}-\d{2}\|/.test(key) || type.includes('无票') || action.includes('无票') || action.includes('归因')
}

// ---------- 行 key 与 loading 判断（依赖 flags: Record<string, any>） ----------

export function formalActionRowKey(row: Row = {}): string {
  return row?.review_key || [row?.source, row?.object, row?.required_action].filter(Boolean).join('|')
}

export function dailyReviewChecklistRowKey(row: Row = {}): string {
  return String(row.review_key || row.ticket_key || [
    row.review_axis || '',
    row.review_scope || '',
    row.object || '',
    row.code || '',
    row.entry_date || ''
  ].join('|')).trim()
}

export function launchDayPlaybookRowKey(row: Row = {}): string {
  return String(row.review_key || [
    row.window || '',
    row.checkpoint_time || '',
    row.review_axis || '',
    row.object || '',
    row.action_type || ''
  ].join('|')).trim()
}

export function strategyTuningTaskRowKey(row: Row = {}): string {
  return String(row.task_key || [
    row.axis || '',
    row.origin || '',
    row.object || '',
    String(row.problem_signal || '').slice(0, 80)
  ].join('|')).trim()
}

export function reviewRowKey(row: Row = {}): string {
  return String(row?.ticket_key || row?.candidate_key || row?.trade_key || row?.code || '')
}

export function isReviewSaving(row: Row = {}, flags: any): boolean {
  return Boolean(flags.reviewSavingKey) && flags.reviewSavingKey === reviewRowKey(row)
}

export function formalActionRowLoading(row: Row = {}, flags: any): boolean {
  return flags.formalActionSavingKey === formalActionRowKey(row) || Boolean(flags.brokerSyncLoading) || Boolean(flags.reviewLoading)
}

export function dailyReviewChecklistRowLoading(row: Row = {}, flags: any): boolean {
  return flags.dailyReviewChecklistSavingKey === dailyReviewChecklistRowKey(row) || Boolean(flags.reviewLoading)
}

export function launchDayPlaybookRowLoading(row: Row = {}, flags: any): boolean {
  return flags.launchDayPlaybookSavingKey === launchDayPlaybookRowKey(row) || Boolean(flags.liveLaunchPacketLoading)
}

export function isStrategyTuningTaskSaving(row: Row = {}, flags: any): boolean {
  return flags.strategyTuningTaskSavingKey === strategyTuningTaskRowKey(row)
}

export function day1JournalRowLoading(row: Row = {}, flags: any): boolean {
  const source = String(row.source_table || '')
  if (source === 'live_daily_review_execution_checklist') return dailyReviewChecklistRowLoading(row, flags)
  if (source === 'live_blocker_evidence_ledger' && isDay1JournalNoTradeRow(row)) return Boolean(flags.reviewLoading)
  return formalActionRowLoading(taskToReviewRow(row), flags) || Boolean(flags.reviewLoading)
}

// ---------- 自动文案 ----------

export function formalActionAutoText(result: string, row: Row = {}) {
  if (result === 'blocked') {
    return {
      issueArea: 'execution_process',
      naturalDecision: '正式动作卡住，不能把流程缺口当成策略买卖点失败',
      suggestion: '先修复持仓刷新、价格确认、审计重跑或人工复盘入口，再讨论交易模型是否需要调参'
    }
  }
  if (result === 'continue_watch') {
    return {
      issueArea: 'operation_evidence',
      naturalDecision: '证据不足，暂不放大为策略问题',
      suggestion: '继续补齐动作证据，等底层审计事实变化后再判断是否清理完成'
    }
  }
  return {
    issueArea: 'operation_evidence',
    naturalDecision: row?.natural_decision || '正式动作已有处理证据，但是否放行仍以重跑审计结果为准',
    suggestion: '保存动作证据后重跑实战前审计，确认 gate/action 是否真实清理'
  }
}

export function dailyReviewChecklistAutoText(result: string, row: Row = {}) {
  if (result === 'validated') {
    return {
      naturalDecision: '复盘证据符合当前合同，不因单日收益倒推改规则。',
      suggestion: row.pass_condition || '保留当前规则，继续积累同类样本。'
    }
  }
  if (result === 'continue_watch') {
    return {
      naturalDecision: '证据不足，继续观察，不放宽规则。',
      suggestion: row.target_action || '继续补齐走势、成交、阻断和切换证据后再判断。'
    }
  }
  if (result === 'data_gap') {
    return {
      naturalDecision: '先补数据证据，不把数据缺口当成交易模型结论。',
      suggestion: row.evidence_to_collect || row.target_action || '补齐行情、持仓、价格或审计证据。'
    }
  }
  return {
    naturalDecision: '发现隐患，进入对应买点、卖点、选股、策略切换或执行证据复盘。',
    suggestion: row.fail_condition || row.target_action || '按复盘轴定位规则缺口，不以单笔收益作为唯一优化目标。'
  }
}

export function launchDayPlaybookAutoText(result: string, row: Row = {}) {
  if (result === 'validated') {
    return {
      naturalDecision: '启动日动作符合当前合同与自然交易边界，保留规则并继续积累同类样本。',
      suggestion: row.pass_condition || '保留当前启动剧本，不因单日收益倒推放宽规则。'
    }
  }
  if (result === 'continue_watch') {
    return {
      naturalDecision: '证据仍不足，继续观察，不提前切换策略或放宽买点。',
      suggestion: row.required_action || row.evidence_to_collect || '继续补齐走势、执行、候选与切换证据。'
    }
  }
  if (result === 'data_gap') {
    return {
      naturalDecision: '先补齐数据与执行证据，不把数据缺口误判为策略结论。',
      suggestion: row.evidence_to_collect || '补齐真实持仓、价格、候选、买卖点或审计证据。'
    }
  }
  return {
    naturalDecision: '发现启动日隐患，进入买点、卖点、选股、模型切换或执行证据复盘，不直接修改合同。',
    suggestion: row.fail_condition || row.required_action || '定位隐患归因，形成可复验样本后再决定是否优化。'
  }
}

export function paperWatchAutoReviewText(result: string, row: Row = {}) {
  const label = paperWatchResultLabel(result)
  const isOmission = isCandidateOmissionReview(row)
  if (isOmission && result === 'as_expected') {
    return {
      hiddenRisk: '候选被挡后没有暴露新增误杀证据',
      suggestion: '保持当前阻断/观察纪律，继续用同类候选验证排序、槽位和盘中确认规则'
    }
  }
  if (isOmission && result === 'missed_opportunity') {
    return {
      hiddenRisk: '可能误杀：候选被挡后仍按预期走强，需要复盘盘中确认、排序或补位规则是否过严',
      suggestion: '先把样本归入候选遗漏复盘，不直接放宽买点；重点检查确认窗口、槽位排序和策略切换时点'
    }
  }
  if (isOmission && result === 'invalid_signal') {
    return {
      hiddenRisk: '信号失效：候选被挡后未走强，当前阻断逻辑得到验证',
      suggestion: '保留当前阻断逻辑，并继续观察同类候选是否稳定失效'
    }
  }
  if (result === 'as_expected') {
    return {
      hiddenRisk: '暂未发现新增隐患',
      suggestion: '保留当前买点、选股和降仓解释，继续观察同类信号复现'
    }
  }
  if (result === 'continue_watch') {
    return {
      hiddenRisk: '证据不足，暂不归因为策略错误',
      suggestion: '延长观察窗口，等待价格、量能和退出信号进一步确认'
    }
  }
  return {
    hiddenRisk: `${label}：需要确认是否属于自然交易逻辑不顺，而不是单日收益噪声`,
    suggestion: `把本次问题纳入 ${paperWatchIssueArea(result)} 调优队列，先复盘行为解释，再决定是否改规则`
  }
}

export function noTradeAutoReviewText(result: string, row: Row = {}) {
  if (result === 'natural_no_trade') {
    return {
      hiddenRisk: '无票日未发现新增误杀证据',
      suggestion: '保持空仓纪律，不为了补满二槽降低买点、盘中确认或持仓刷新要求'
    }
  }
  if (result === 'missed_opportunity') {
    return {
      hiddenRisk: '可能误杀：无票日候选或路由被挡后仍出现可交易走势',
      suggestion: '先复盘候选排序、盘中确认、槽位约束和策略切换时点，不直接放宽仓位'
    }
  }
  if (result === 'data_gap') {
    return {
      hiddenRisk: '数据/确认缺口：无票可能来自分钟线、持仓刷新或审计输入不完整',
      suggestion: '优先修复数据链路和确认证据，再判断是否属于策略规则问题'
    }
  }
  if (result === 'process_gap') {
    return {
      hiddenRisk: '流程缺口：实战前动作未闭环，可能导致本可交易信号无法进入执行',
      suggestion: '把流程问题纳入盘前清单，先修复执行闭环，再评估交易模型'
    }
  }
  return {
    hiddenRisk: row?.hidden_risk || '证据不足，暂不归因为策略错误',
    suggestion: '继续观察无票日候选、持仓刷新和盘中确认结果，等待更完整证据'
  }
}

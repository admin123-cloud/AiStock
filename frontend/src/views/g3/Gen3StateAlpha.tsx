import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import { Card, EmptyState, PageHeader, SectionTitle, StatCard } from '@/components/ui'
import {
  getGen3StateAlphaBrokerHoldings,
  getGen3StateAlphaBrokerTrades,
  getGen3StateAlphaCurrent,
  getGen3StateAlphaObservationSnapshots,
  getGen3StateAlphaPaperExecutions,
  getGen3StateAlphaRefreshTask,
  getGen3StateAlphaWorkflowStatus,
  runGen3StateAlphaObservationSchedulerOnce,
  runGen3StateAlphaRefreshOnce,
  runGen3StateAlphaShadowMonitorOnce,
  setGen3StateAlphaShadowMonitorConfig,
  submitGen3StateAlphaPaperOrder,
  syncGen3StateAlphaBrokerHoldingsFromThs,
} from '@/api/trading'
import { compactStatusWithZh, statusWithZh } from '@/utils/g3StatusText'

type Row = Record<string, any>

function truthy(value: any): boolean {
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return value !== 0
  if (typeof value === 'string') return ['1', 'true', 'yes', 'ok'].includes(value.trim().toLowerCase())
  return false
}

function pct(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return `${(n * 100).toFixed(1)}%`
}

function signedPct(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(2)}%`
}

function price(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(2)
}

function fmt(value: any, digits = 2): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toFixed(digits)
}

function money(value: any): string {
  if (value === null || value === undefined || value === '') return '--'
  const n = Number(value)
  if (!Number.isFinite(n)) return '--'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function pnlColor(value: any): 'red' | 'emerald' | 'zinc' {
  const n = Number(value)
  if (!Number.isFinite(n) || Math.abs(n) < 0.0001) return 'zinc'
  // A 股行情惯例：上涨红色，下跌绿色
  return n > 0 ? 'red' : 'emerald'
}

const ROUTE_LABELS: Record<string, string> = {
  institutional_mainwave: '机构主升浪',
  institutional_mainwave_score88: '机构主升Score88',
  institutional_score120_mainwave: '机构主升Score120（历史）',
  panic_repair: '恐慌修复',
  panic_capitulation_repair: '恐慌出清修复',
  range_weak_repair: '震荡弱势修复',
  old_g3_strong_breakout: '强势突破',
  g2_gap_supplement: 'G2空档补位',
  old_g3_route_v3: '旧G3原生来源',
  old_g3_route: '旧G3原生来源',
}

function routeLabel(route: any): string {
  return ROUTE_LABELS[route] || route || '--'
}

function entryPolicyText(row: Row): string {
  const value = String(row?.entry_price_policy || '')
  if (!value) return '下一交易日开盘参考，人工确认后执行'
  const labels: Record<string, string> = {
    next_trade_day_open_or_manual_shadow_fill: '下一交易日开盘参考，人工确认后执行',
  }
  return labels[value] || statusWithZh(value)
}

function exitContractText(row: Row): string {
  const text = String(row?.exit_contract || '')
  if (!text) return '暂无退出合同'
  if (text.includes('institutional mainwave dynamic contract')) return '机构主升动态合同：2槽、单槽50%；index_mom60<=5%；连续2笔已平仓机构主升亏损后暂停新买至少3个交易日，行情恢复后重启，最多15日复评；30m硬止损-12%，上涨+12%先卖一半，剩余仓按前低保护。'
  if (text.includes('2-slot compound default')) return '二槽复利合同：30m硬止损-12%；上涨+12%先卖一半；剩余仓按前一日低点保护；机构主升新买冷却按连续2笔已平仓亏损的动态规则执行。'
  if (text.includes('G2 gap supplement')) return 'G2补位合同：使用G2成熟买点和止损线；30m 硬止损约 -5%；上涨约 +10% 先减半；后续按结构转弱或前低跌破退出。'
  if (text.includes('panic capitulation repair')) return '恐慌出清修复合同：恐慌释放后介入；结构止损约 -5%，硬止损约 -8%；上涨约 +8% 先减半；剩余仓位按前低跌破或30m转弱退出。'
  if (text.includes('range weak repair')) return '震荡弱势修复合同：弱势/震荡修复介入；结构止损约 -6%，硬止损约 -10%；上涨约 +8% 先减半；剩余仓位按前低跌破或30m转弱退出。'
  if (text.includes('old G3 route')) return '强势突破/旧G3合同：按旧G3原生信号介入；用30m转弱和前一日低点跌破作为退出保护。'
  return text
}

function m30Text(row: Row): string {
  if (truthy(row?.m30_confirmed)) return '通过'
  const status = String(row?.m30_status || '')
  if (!status || status === 'nan') return '通过'
  return compactStatusWithZh(status)
}

function managementAction(row: Row, paper: Row | null): string {
  if (row?.exit_action_label) return row.exit_action_label
  if (row?.source === 'ths_capital_holdings' || row?.route === 'broker_real_position') return '实仓退出检查'
  const status = String(row?.shadow_status || row?.trade_status || row?.status || '').toLowerCase()
  if (status.includes('stop') || status.includes('止损')) return '止损检查'
  if (status.includes('exit') || status.includes('退出')) return '退出检查'
  if (truthy(row?.take_profit_1_hit) || truthy(row?.half_take_profit_done)) return '减半后保护'
  if (!paper) return '补纸面'
  return '持有观察'
}

type TagColor = 'red' | 'emerald' | 'amber' | 'zinc'

function managementActionType(row: Row): TagColor {
  const priority = String(row?.exit_priority || '')
  const actionKey = String(row?.exit_action || '')
  if (priority === 'critical' || actionKey === 'sell_all') return 'red'
  if (priority === 'take_profit' || actionKey === 'sell_half') return 'emerald'
  if (priority === 'warning' || actionKey === 'refresh_before_decision' || actionKey === 'data_missing') return 'amber'
  if (priority === 'protect' || actionKey === 'protect_remaining') return 'amber'
  const action = String(row?.management_action || '')
  if (action.includes('止损') || action.includes('退出')) return 'red'
  if (action.includes('实仓')) return 'amber'
  if (action.includes('补纸面')) return 'amber'
  if (action.includes('减半')) return 'emerald'
  return 'zinc'
}

function exitReasonText(row: Row): string {
  const parts: string[] = []
  const reason = String(row?.exit_reason_label || row?.exit_reason || '').trim()
  if (reason) parts.push(reason)
  const trigger = price(row?.trigger_price)
  if (trigger !== '--') parts.push(`触发价 ${trigger}`)
  const stale = Number(row?.price_stale_minutes)
  if (Number.isFinite(stale) && stale > 30) parts.push(`数据${stale.toFixed(0)}分钟前`)
  return parts.join(' / ') || '--'
}

function naturalActionTagType(row: Row): TagColor {
  const action = String(row?.natural_action || '')
  if (action === 'skip') return 'red'
  if (action === 'allow_reduced') return 'amber'
  if (action === 'allow') return 'emerald'
  return 'zinc'
}

function naturalDisciplineText(row: Row): string {
  if (!row?.code && !row?.code_raw) return '自然纪律：暂无候选。'
  const label = row.natural_action_label || '观察'
  const naturalPct = Number(row.natural_position_pct)
  const pctText = Number.isFinite(naturalPct) ? `，自然仓位 ${pct(naturalPct)}` : ''
  const reason = row.natural_reason || '符合当前自然交易观察合同'
  return `自然纪律：${label}${pctText}；${reason}`
}

function opportunityText(row: Row | null | undefined): string {
  if (!row) return '--'
  if (row.opportunity_explain) return row.opportunity_explain
  const route = String(row.route || row.trade_strategy || '')
  const sector = row.sector_name || row.l2_sector_name || row.industry || row.template_label || '未识别板块'
  const diffusion = Number(row.sector_diffusion_score)
  const label = row.sector_diffusion_label || (Number.isFinite(diffusion)
    ? (diffusion >= 80 ? '强扩散' : diffusion >= 65 ? '有效扩散' : diffusion >= 50 ? '观察扩散' : '扩散不足')
    : '扩散未知')
  if (route === 'institutional_mainwave' || route === 'institutional_mainwave_score88' || route === 'institutional_score120_mainwave') {
    const details = [`机构主升机会落在${sector}`, label]
    if (Number.isFinite(diffusion)) details.push(`扩散分${diffusion.toFixed(1)}`)
    if (row.sector_candidate_count !== undefined && row.sector_candidate_count !== null) details.push(`同板块候选${Number(row.sector_candidate_count).toFixed(0)}只`)
    if (row.same_sector_selected_count >= 2) details.push('同板块共振允许')
    return `${details.join('；')}。`
  }
  return `非机构主升路线；板块${sector}只用于重复暴露检查。`
}

function isShadowLedgerSessionCreatedAt(value: string): boolean {
  const text = String(value || '').trim()
  const match = text.match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/)
  if (!match) return false
  const date = new Date(`${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:00`)
  const day = date.getDay()
  if (day === 0 || day === 6) return false
  const minutes = Number(match[4]) * 60 + Number(match[5])
  return minutes >= 9 * 60 && minutes <= 15 * 60 + 30
}

function observationBlockerText(row: Row): string {
  const blockers = Array.isArray(row?.blockers) ? row.blockers : []
  if (!blockers.length) return '无'
  return blockers.map((item: Row) => `${item.gate || 'gate'}: ${item.message || '--'}`).join('；')
}

// 状态卡 tone 映射：ok=绿(down)、阻断=红(up)、锁定=琥珀(warning)，仅影响展示色
const toneOk = 'down' as const
const toneBlocked = 'up' as const
const toneLocked = 'warning' as const
const toneDefault = 'default' as const

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} className="text-center text-zinc-500 dark:text-zinc-400">
        {text}
      </TableCell>
    </TableRow>
  )
}

export default function Gen3StateAlpha() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [opsLoading, setOpsLoading] = useState(false)
  const [opsRunLoading, setOpsRunLoading] = useState(false)
  const [opsNotifyLoading, setOpsNotifyLoading] = useState(false)
  const [opsConfigLoading, setOpsConfigLoading] = useState(false)
  const [paperSubmitting, setPaperSubmitting] = useState(false)
  const [observationLoading, setObservationLoading] = useState(false)
  const [observationRunLoading, setObservationRunLoading] = useState(false)
  const [brokerLoading, setBrokerLoading] = useState(false)
  const [brokerHoldingSyncLoading, setBrokerHoldingSyncLoading] = useState(false)
  const [payload, setPayload] = useState<Row | null>(null)
  const [workflowPayload, setWorkflowPayload] = useState<Row | null>(null)
  const [refreshTask, setRefreshTask] = useState<Row>({})
  const [paperExecutions, setPaperExecutions] = useState<Row[]>([])
  const [brokerPayload, setBrokerPayload] = useState<Row | null>(null)
  const [observationPayload, setObservationPayload] = useState<Row | null>(null)

  const summary = useMemo(() => payload?.summary || {}, [payload])
  const shadowTickets = useMemo(() => (Array.isArray(payload?.shadow_tickets) ? payload.shadow_tickets : []), [payload])
  const afterhoursSummary = useMemo(() => payload?.afterhours_summary || {}, [payload])
  const afterhoursShadowTickets = useMemo(() => (Array.isArray(payload?.afterhours_shadow_tickets) ? payload.afterhours_shadow_tickets : []), [payload])
  const dateDisplayAnalysis = useMemo(() => payload?.date_display_analysis || {}, [payload])
  const nextTradeBuySummary = useMemo(() => payload?.next_trade_buy_summary || afterhoursSummary || {}, [payload, afterhoursSummary])
  const nextTradeBuyTickets = useMemo(() => (
    Array.isArray(payload?.next_trade_buy_tickets) ? payload.next_trade_buy_tickets : afterhoursShadowTickets
  ), [payload, afterhoursShadowTickets])
  const selectedTicket = useMemo(() => nextTradeBuyTickets[0] || {}, [nextTradeBuyTickets])
  const displayShadowTickets = nextTradeBuyTickets
  const shadowLedger = useMemo(() => (Array.isArray(payload?.shadow_ledger) ? payload.shadow_ledger : []), [payload])
  const candidates = useMemo(() => (Array.isArray(payload?.all_source_candidates) ? payload.all_source_candidates : []), [payload])
  const eligibleCandidateCount = useMemo(() => candidates.filter((item: Row) => truthy(item.router_eligible || item.qualified_shadow_buy)).length, [candidates])
  const blockedCandidateCount = useMemo(() => candidates.filter((item: Row) => !truthy(item.router_eligible || item.qualified_shadow_buy) || item.block_reason).length, [candidates])
  const backtestProfile = useMemo(() => payload?.backtest_snapshot?.profile || {}, [payload])
  const workflowStatus = workflowPayload || {}
  const monitorState = workflowPayload?.monitor || {}
  const workflowChecks = useMemo(() => (Array.isArray(workflowPayload?.pipeline_checks) ? workflowPayload.pipeline_checks : []), [workflowPayload])
  const workflowBlockers = useMemo(() => (Array.isArray(workflowPayload?.blockers) ? workflowPayload.blockers : []), [workflowPayload])
  const recentEvents = useMemo(() => (Array.isArray(workflowPayload?.recent_events) ? workflowPayload.recent_events : []), [workflowPayload])
  const observationSnapshots = useMemo(() => (Array.isArray(observationPayload?.snapshots) ? observationPayload.snapshots : []), [observationPayload])
  const brokerHoldings = useMemo(() => (Array.isArray(brokerPayload?.holdings) ? brokerPayload.holdings : []), [brokerPayload])
  const brokerCapital = brokerPayload?.capital || {}
  const exitManagement = payload?.exit_management || {}
  const exitSummary = exitManagement?.summary || brokerPayload?.exit_advice_summary || {}
  const observationScheduler = observationPayload?.scheduler || workflowPayload?.observation_scheduler || {}
  const acceptedObservationDays = Number(observationPayload?.accepted_observation_days || 0)
  const legacyObservationCount = Number(observationPayload?.legacy_snapshot_count || 0)
  const latestObservation = observationSnapshots[0] || null
  const parity = workflowPayload?.g2_parity || {}
  const parityCount = Object.values(parity).filter(Boolean).length
  const parityTotal = Object.values(parity).length || 0

  const brokerHoldingMarketValue = useMemo(() => {
    const explicitValue = Number(brokerCapital.holding_market_value ?? brokerCapital.market_value)
    if (Number.isFinite(explicitValue)) return explicitValue
    const total = brokerHoldings.reduce((sum: number, item: Row) => {
      const marketValue = Number(item?.market_value)
      return Number.isFinite(marketValue) ? sum + marketValue : sum
    }, 0)
    return total > 0 ? total : null
  }, [brokerCapital, brokerHoldings])

  const brokerAvailableCash = useMemo(() => {
    const availableCash = Number(brokerCapital.available_cash)
    if (brokerCapital.available_cash !== null && brokerCapital.available_cash !== undefined && Number.isFinite(availableCash)) {
      return availableCash
    }
    const totalCapital = Number(brokerCapital.total_capital)
    const holdingMarketValue = Number(brokerHoldingMarketValue)
    if (Number.isFinite(totalCapital) && Number.isFinite(holdingMarketValue)) {
      return Math.max(totalCapital - holdingMarketValue, 0)
    }
    return null
  }, [brokerCapital, brokerHoldingMarketValue])

  const latestCandidateByCode = useMemo(() => {
    const rows = new Map<string, Row>()
    candidates.forEach((item: Row) => {
      const code = String(item?.code || item?.code_raw || '').toUpperCase()
      if (!code) return
      const prev = rows.get(code)
      const itemTime = String(item?.trade_date || item?.date || item?.confirm_datetime || item?.created_at || '')
      const prevTime = String(prev?.trade_date || prev?.date || prev?.confirm_datetime || prev?.created_at || '')
      if (!prev || itemTime >= prevTime) rows.set(code, item)
    })
    return rows
  }, [candidates])

  const currentShadowPrice = useCallback((row: Row): number | null => {
    const direct = firstPositiveNumber(row?.current_price, row?.latest_price, row?.last_price, row?.close)
    if (direct !== null) return direct
    const code = String(row?.code || row?.code_raw || '').toUpperCase()
    const latest = latestCandidateByCode.get(code) || {}
    return firstPositiveNumber(
      latest.current_price,
      latest.latest_price,
      latest.last_price,
      latest.close,
      latest.reference_close,
      latest.execution_price,
    )
  }, [latestCandidateByCode])

  const realHoldingRows = useMemo(() => (
    brokerHoldings
      .filter((item: Row) => item?.code || item?.code_raw)
      .map((item: Row) => normalizeManagementRow({ ...item, paper_recorded: true }, 'broker', paperExecutions, currentShadowPrice))
  ), [brokerHoldings, paperExecutions, currentShadowPrice])

  const realAccountPositionPct = useMemo(() => {
    const totalCapital = Number(brokerCapital.total_capital)
    const holdingMarketValue = Number(brokerHoldingMarketValue)
    if (Number.isFinite(totalCapital) && totalCapital > 0 && Number.isFinite(holdingMarketValue)) {
      return Math.min(Math.max(holdingMarketValue / totalCapital, 0), 1)
    }
    const totalPct = realHoldingRows.reduce((sum: number, item: Row) => {
      const pctValue = Number(item?.position_pct || item?.slot_pct)
      return Number.isFinite(pctValue) ? sum + pctValue : sum
    }, 0)
    return Math.min(Math.max(totalPct, 0), 1)
  }, [brokerCapital, brokerHoldingMarketValue, realHoldingRows])

  const availableCashPositionPct = useMemo(() => {
    const totalCapital = Number(brokerCapital.total_capital)
    const availableCash = Number(brokerAvailableCash)
    if (!Number.isFinite(totalCapital) || totalCapital <= 0 || !Number.isFinite(availableCash)) return 0
    return Math.min(Math.max(availableCash / totalCapital, 0), 1)
  }, [brokerCapital, brokerAvailableCash])

  const remainingBuyPositionPct = useMemo(() => {
    const byPosition = Math.max(1 - realAccountPositionPct, 0)
    return Math.min(byPosition, availableCashPositionPct)
  }, [realAccountPositionPct, availableCashPositionPct])

  const executablePositionPct = useCallback((row: Row): number => {
    if (!row?.code && !row?.code_raw) return 0
    const contractPct = Number(row?.position_pct || row?.slot_pct || 0)
    if (!Number.isFinite(contractPct) || contractPct <= 0) return 0
    return Math.min(contractPct, remainingBuyPositionPct)
  }, [remainingBuyPositionPct])

  const selectedTicketExecutablePositionPct = executablePositionPct(selectedTicket)

  const latestShadowLedgerCreatedAt = useMemo(() => {
    const times = shadowLedger
      .map((item: Row) => String(item?.created_at || '').trim())
      .filter((value: string) => value && isShadowLedgerSessionCreatedAt(value))
      .sort()
    if (times.length) return times[times.length - 1]
    const fallbackTimes = shadowLedger
      .map((item: Row) => String(item?.created_at || '').trim())
      .filter(Boolean)
      .sort()
    return fallbackTimes[fallbackTimes.length - 1] || ''
  }, [shadowLedger])

  const currentShadowHoldingRows = useMemo(() => (
    shadowLedger
      .filter((item: Row) => isCurrentShadowHolding(item, latestShadowLedgerCreatedAt))
      .map((item: Row) => normalizeManagementRow(item, 'shadow', paperExecutions, currentShadowPrice))
  ), [shadowLedger, latestShadowLedgerCreatedAt, paperExecutions, currentShadowPrice])

  const currentTicketDate = shadowTickets[0]?.entry_date || summary.entry_date || '--'
  const afterhoursTicketDate = afterhoursShadowTickets[0]?.entry_date || afterhoursSummary.entry_date || ''
  const nextTradeTicketDate = nextTradeBuyTickets[0]?.entry_date
    || dateDisplayAnalysis.display_entry_date
    || dateDisplayAnalysis.expected_next_trade_date
    || nextTradeBuySummary.entry_date
    || afterhoursTicketDate
    || ''
  const shadowTicketTableTitle = nextTradeTicketDate ? `${nextTradeTicketDate} 买入建议票据` : '买入建议票据'
  const shadowTicketTableExplain = nextTradeBuyTickets.length
    ? `这里展示 ${nextTradeTicketDate || '--'} 当前应重点关注的买入建议；候选日期用于标记批次，非交易时段不会自动改写当前持仓。当前影子台账日期 ${currentTicketDate}。`
    : dateDisplayAnalysis.message || '当前没有独立的买入建议；这里不会用正在持有的影子仓位兜底。'
  const dateDisplayStatusText = (() => {
    const status = dateDisplayAnalysis.status || ''
    const date = dateDisplayAnalysis.display_entry_date || dateDisplayAnalysis.expected_next_trade_date || nextTradeTicketDate || '--'
    if (status === 'ready') return `${date} 候选有效`
    if (status === 'filtered_non_trading') return '已过滤非交易日'
    if (status === 'date_note' || status === 'date_mismatch') return '日期提示'
    return `${date} 暂无候选`
  })()
  const dateDisplayTagType: TagColor = (() => {
    const status = dateDisplayAnalysis.status || ''
    if (status === 'ready') return 'emerald'
    if (status === 'filtered_non_trading' || status === 'date_note' || status === 'date_mismatch') return 'amber'
    return 'zinc'
  })()
  const shadowHoldingExplainText = (() => {
    const holdingNames = currentShadowHoldingRows.map((item: Row) => item.name || item.code).filter(Boolean).join('、') || '暂无'
    const latestNames = nextTradeBuyTickets.map((item: Row) => item.name || item.code).filter(Boolean).join('、')
    if (nextTradeBuyTickets.length) {
      return `当前影子持仓来自 ${currentTicketDate} 台账：${holdingNames}；盘后最新候选是 ${latestNames}，候选变化不等于已换仓。`
    }
    return '记录当前仍在持有的影子交易，仅用于跟踪信号合同，不参与真实账户仓位和可用资金计算。'
  })()

  const realAccountBuyCapacityText = displayShadowTickets.length
    ? remainingBuyPositionPct <= 0.001
      ? '真实账户仓位已满'
      : selectedTicketExecutablePositionPct <= 0.001
        ? '现金不足'
        : `可买 ${pct(selectedTicketExecutablePositionPct)}`
    : '暂无买入候选'
  const realAccountBuyCapacityReason = `真实仓位 ${pct(realAccountPositionPct)}，可用资金 ${money(brokerAvailableCash)}，总资产 ${money(brokerCapital.total_capital)}`
  const realAccountBuyCapacityType: 'amber' | 'zinc' = selectedTicketExecutablePositionPct > 0.001 ? 'amber' : 'zinc'

  const selectedTicketKey = selectedTicket?.ticket_key || selectedTicket?.candidate_key || selectedTicket?.trade_key || ''
  const selectedPaperExecution = useMemo(() => {
    const key = String(selectedTicketKey || '')
    const code = String(selectedTicket?.code || selectedTicket?.code_raw || '').toUpperCase()
    const entryDate = String(selectedTicket?.entry_date || '')
    return paperExecutions.find((item: Row) => {
      if (key && item.ticket_key === key) return true
      return !!(code && entryDate && String(item.code || '').toUpperCase() === code && item.entry_date === entryDate)
    }) || null
  }, [paperExecutions, selectedTicketKey, selectedTicket])

  const canSubmitPaperOrder = !!selectedTicket?.code
    && truthy(selectedTicket?.qualified_shadow_buy)
    && truthy(selectedTicket?.m30_confirmed)
    && selectedTicketExecutablePositionPct > 0.001
    && !selectedPaperExecution

  const selectedTicketExplainText = (() => {
    const row = selectedTicket
    if (!row?.code && !row?.code_raw) return '当前没有可展示的买入票据。'
    const finalPct = Number(row?.position_pct || 0)
    const heatState = String(row?.index_mom60_heat_state || '')
    const parts: string[] = []
    if (afterhoursShadowTickets.length) {
      parts.push(`这是 ${afterhoursTicketDate || '--'} 盘后最新候选，不代表已经替换当前影子持仓。`)
    }
    if (heatState === 'high_heat_reduce_position' || heatState === 'high_heat_observe' || heatState === 'institutional_mom60_gt_5_block') {
      parts.push(`机构主升 mom60 超过 5% 时只观察，不进入影子盘或买入候选；当前仓位为 ${pct(finalPct)}。`)
    } else {
      parts.push(`仓位按当前策略合同计算为 ${pct(finalPct)}。`)
    }
    parts.push(`策略为 ${row.trade_strategy_label || routeLabel(row.trade_strategy || row.route)}。`)
    return parts.join(' ')
  })()

  const backtestProfileText = (() => {
    const model = backtestProfile.model || backtestProfile.profile || 'g3_final_with_g2_gap_supplement'
    return `最新正式G3合同 ${model}，最大回撤 ${pct(backtestProfile.max_drawdown)}`
  })()

  const g2SupplementStatus = summary.g2_gap_supplement_status || {}
  const g2SupplementStatusText = g2SupplementStatus.message
    || `请求 ${g2SupplementStatus.requested_entry_date || summary.entry_date || '--'} / 最新 ${g2SupplementStatus.latest_source_date || g2SupplementStatus.latest_live_update_date || g2SupplementStatus.latest_ledger_entry_date || '--'}`

  const plannedEntryDisplayText = useCallback((row: Row): string => {
    const entryDate = String(row?.entry_date || '').slice(0, 10)
    const planned = String(row?.planned_entry_ts || '').trim()
    const displayDate = entryDate || dateDisplayAnalysis.display_entry_date || dateDisplayAnalysis.expected_next_trade_date || ''
    if (planned && (!displayDate || planned.slice(0, 10) === displayDate)) return planned
    if (displayDate) return `${displayDate} 09:30:00`
    return planned || '--'
  }, [dateDisplayAnalysis])

  const artifactRows = useMemo<Row[]>(() => {
    const artifacts = payload?.artifacts || {}
    const backtestArtifacts = payload?.backtest_snapshot?.artifacts || {}
    return [
      ...Object.entries(artifacts).map(([name, item]) => ({ name, ...(item as Row) })),
      ...Object.entries(backtestArtifacts).map(([name, item]) => ({ name: `backtest_${name}`, ...(item as Row) })),
    ]
  }, [payload])

  const alertColor = (() => {
    if (payload?.data_freshness_blocked || Number(summary.minute_data_failure_rows || 0) > 0) return 'red' as const
    if ((summary.qualified_shadow_buy_rows || 0) > 0) return 'amber' as const
    return 'zinc' as const
  })()

  const statusText = payload?.data_freshness_blocked || Number(summary.minute_data_failure_rows || 0) > 0
    ? `今日数据不可判断：${summary.minute_data_failure_rows || 0}个候选分钟来源异常。没有票据不等于没有机会；请查看数据健康与主升每日跟踪。`
    : `状态 ${statusWithZh(summary.diagnosis_code || '--')}，合格买入候选 ${summary.qualified_shadow_buy_rows ?? 0}。正式买点、自动下单、订单路径均关闭。`

  const loadPaperExecutions = useCallback(async () => {
    try {
      const data: any = await getGen3StateAlphaPaperExecutions({ limit: 100 })
      setPaperExecutions(Array.isArray(data?.paper_executions) ? data.paper_executions : [])
    } catch {
      setPaperExecutions([])
    }
  }, [])

  const loadBrokerState = useCallback(async () => {
    setBrokerLoading(true)
    try {
      const [holdings, trades]: any[] = await Promise.all([
        getGen3StateAlphaBrokerHoldings({}),
        getGen3StateAlphaBrokerTrades({ limit: 200 }),
      ])
      setBrokerPayload(holdings || {})
      void trades
    } catch {
      setBrokerPayload({ holdings: [], capital: {} })
    } finally {
      setBrokerLoading(false)
    }
  }, [])

  const loadObservations = useCallback(async () => {
    setObservationLoading(true)
    try {
      setObservationPayload((await getGen3StateAlphaObservationSnapshots({ limit: 60 })) as any)
    } catch {
      setObservationPayload({ snapshots: [], accepted_observation_days: 0 })
    } finally {
      setObservationLoading(false)
    }
  }, [])

  const loadOps = useCallback(async () => {
    setOpsLoading(true)
    try {
      setWorkflowPayload((await getGen3StateAlphaWorkflowStatus({})) as any)
    } catch (error: any) {
      setWorkflowPayload({
        ok: false,
        message: error?.message || 'G3 运行状态不可用',
        pipeline_checks: [],
        blockers: [],
      })
    } finally {
      setOpsLoading(false)
    }
  }, [])

  const load = useCallback(async (refresh = false) => {
    if (refresh) setRefreshing(true)
    else setLoading(true)
    try {
      setPayload((await getGen3StateAlphaCurrent({ refresh, limit: 120 })) as any)
      await Promise.all([loadOps(), loadPaperExecutions(), loadBrokerState(), loadObservations()])
      if (refresh) toast.success('G3正式五策略已刷新')
    } catch (error: any) {
      toast.error(error?.message || '读取G3正式五策略失败')
    } finally {
      setRefreshing(false)
      setLoading(false)
    }
  }, [loadOps, loadPaperExecutions, loadBrokerState, loadObservations, toast])

  const waitRefreshTask = useCallback(async (taskId: string) => {
    for (let i = 0; i < 90; i += 1) {
      const task: any = await getGen3StateAlphaRefreshTask(taskId)
      setRefreshTask(task || {})
      if (!['queued', 'running'].includes(task?.status)) return task
      await new Promise((resolve) => setTimeout(resolve, 3000))
    }
  }, [])

  const syncBrokerHoldingsFromThs = useCallback(async () => {
    setBrokerHoldingSyncLoading(true)
    try {
      const result: any = await syncGen3StateAlphaBrokerHoldingsFromThs({})
      setBrokerPayload(result || {})
      if (result?.ok) toast.success(`已同步QMT真实持仓：${(result?.holdings || []).length} 只`)
      else toast.error(result?.message || '同步QMT真实持仓失败')
    } catch (error: any) {
      toast.error(error?.message || '同步QMT真实持仓失败')
    } finally {
      setBrokerHoldingSyncLoading(false)
    }
  }, [toast])

  const runObservationOnce = useCallback(async () => {
    setObservationRunLoading(true)
    try {
      const result: any = await runGen3StateAlphaObservationSchedulerOnce({ run_smoke: true, refresh_smoke: true, auto_repair_minute30: true })
      await Promise.all([loadObservations(), loadOps()])
      if (result?.ok) toast.success('G3 今日观察已验收通过')
      else toast.error(result?.blockers?.[0]?.message || 'G3 今日观察存在阻断')
    } catch (error: any) {
      toast.error(error?.message || 'G3 今日观察记录失败')
    } finally {
      setObservationRunLoading(false)
    }
  }, [loadObservations, loadOps, toast])

  const submitSelectedPaperOrder = useCallback(async () => {
    if (!canSubmitPaperOrder) return
    setPaperSubmitting(true)
    try {
      const ticket = selectedTicket || {}
      const result: any = await submitGen3StateAlphaPaperOrder({
        ticket_key: selectedTicketKey,
        code: ticket.code || ticket.code_raw,
        execution_price: ticket.reference_close,
        position_pct: selectedTicketExecutablePositionPct,
        base_capital: brokerCapital.total_capital,
        notes: 'submitted from G3 formal five strategy page',
      })
      if (result?.ok) {
        toast.success('G3 纸面执行已记录')
        await Promise.all([loadPaperExecutions(), loadOps(), loadObservations()])
      } else {
        toast.error(result?.message || 'G3 纸面执行记录失败')
      }
    } catch (error: any) {
      toast.error(error?.message || 'G3 纸面执行记录失败')
    } finally {
      setPaperSubmitting(false)
    }
  }, [canSubmitPaperOrder, selectedTicket, selectedTicketKey, selectedTicketExecutablePositionPct, brokerCapital.total_capital, loadPaperExecutions, loadOps, loadObservations, toast])

  const runOpsOnce = useCallback(async () => {
    setOpsRunLoading(true)
    try {
      const task: any = await runGen3StateAlphaRefreshOnce({})
      setRefreshTask(task || {})
      const finalTask = task?.task_id ? await waitRefreshTask(task.task_id) : task
      await load(false)
      if (finalTask?.status === 'success') toast.success('G3 影子刷新完成')
      else toast.error(finalTask?.message || 'G3 影子刷新结束但存在阻断')
    } catch (error: any) {
      toast.error(error?.message || 'G3 影子刷新失败')
    } finally {
      setOpsRunLoading(false)
    }
  }, [waitRefreshTask, load, toast])

  const runNotifyProbe = useCallback(async () => {
    setOpsNotifyLoading(true)
    try {
      const task: any = await runGen3StateAlphaShadowMonitorOnce({ force_send: true })
      setRefreshTask(task || {})
      const finalTask = task?.task_id ? await waitRefreshTask(task.task_id) : task
      await load(false)
      if (finalTask?.alert?.email_sent) toast.success('G3 影子提醒邮件已发送')
      else if (finalTask?.alert?.error) toast.error(finalTask.alert.error)
      else toast.info('本次没有新的 G3 影子提醒邮件')
    } catch (error: any) {
      toast.error(error?.message || 'G3 影子提醒探测失败')
    } finally {
      setOpsNotifyLoading(false)
    }
  }, [waitRefreshTask, load, toast])

  const toggleMonitor = useCallback(async () => {
    setOpsConfigLoading(true)
    try {
      const nextEnabled = !monitorState.enabled
      await setGen3StateAlphaShadowMonitorConfig({ enabled: nextEnabled })
      await loadOps()
      toast.success(nextEnabled ? 'G3 影子监控已开启' : 'G3 影子监控已暂停')
    } catch (error: any) {
      toast.error(error?.message || 'G3 影子监控配置失败')
    } finally {
      setOpsConfigLoading(false)
    }
  }, [monitorState.enabled, loadOps, toast])

  useEffect(() => {
    load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const pnlCell = (value: any) => (
    <Badge color={pnlColor(value)}>{signedPct(value)}</Badge>
  )

  return (
    <div className="flex flex-col gap-3.5">
      <PageHeader
        title="G3 工作台"
        subtitle={(
          <>
            <span className="font-bold text-blue-600 dark:text-blue-400">第三代定型策略</span>
            ：正式实盘路由聚焦机构主升、强势突破、震荡弱势修复和恐慌出清修复；量能续强补位已退役为观察源。
          </>
        )}
        actions={(
          <>
            <Button outline disabled={loading} onClick={() => load(false)}>{loading ? '刷新中…' : '刷新页面'}</Button>
            <Button color="blue" disabled={refreshing} onClick={() => load(true)}>{refreshing ? '刷新中…' : '运行今日影子刷新'}</Button>
          </>
        )}
      />

      <Alert color={alertColor} className="text-sm">{statusText}</Alert>

      {/* 当前持仓 / 退出管理 */}
      <Card>
        <SectionTitle
          className="mb-3 items-start"
          title={(
            <>
              当前持仓 / 退出管理
              <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">以同花顺真实账户的持仓金额、仓位和可用资金作为买卖决策依据；先处理真实持仓退出，再判断今日是否还有买入容量。</span>
            </>
          )}
          actions={(
            <>
              <Badge color={realHoldingRows.length ? 'amber' : 'zinc'}>
                真实持仓 {realHoldingRows.length} / 仓位 {pct(realAccountPositionPct)}
              </Badge>
              <Button outline disabled={brokerLoading} onClick={loadBrokerState}>刷新真实账户</Button>
              <Button outline disabled={brokerHoldingSyncLoading} onClick={syncBrokerHoldingsFromThs}>同步QMT真实持仓</Button>
            </>
          )}
        />

        <div className="mb-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
          {[
            { label: '同花顺真实持仓', value: String(brokerHoldings.length), note: `更新时间 ${brokerPayload?.updated_at || '--'}${brokerCapital.holdings_stale ? ' / 明细待刷新' : ''}` },
            { label: '可用资金', value: money(brokerAvailableCash), note: `持仓市值 ${money(brokerHoldingMarketValue)} / 总资产 ${money(brokerCapital.total_capital)}` },
            { label: '剩余买入容量', value: pct(remainingBuyPositionPct), note: `按真实账户仓位测算，现金可买 ${pct(availableCashPositionPct)}` },
            { label: '实时卖出建议', value: String(exitSummary.urgent_exit_rows || 0), note: `清仓/减半持仓数；${exitSummary.checked_at || '--'}` },
          ].map((item) => (
            <StatCard key={item.label} label={item.label} value={item.value} hint={item.note} />
          ))}
        </div>

        <SectionTitle
          className="mb-2 mt-3 items-baseline"
          title={(
            <>
              同花顺真实持仓
              <small className="ml-2.5 text-xs font-normal text-zinc-500 dark:text-zinc-400">
                {brokerCapital.holdings_stale ? '资金摘要已按同花顺当前窗口校正，持仓明细仍待实时同步刷新' : '只显示同花顺账户当前真实持有的股票'}
              </small>
            </>
          )}
        />
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['代码', '名称', '路由', '入场日', '仓位', '入场/参考', '最新价', '实时盈亏', '硬止损', '结构保护', '止盈一', '纸面', '动作', '触发原因', '退出合同'].map((h) => (
                <TableHeader key={h}>{h}</TableHeader>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {realHoldingRows.length === 0 && <EmptyRow colSpan={15} text="暂无同花顺真实持仓" />}
            {realHoldingRows.map((row: Row, idx: number) => (
              <TableRow key={`${row.code}-${row.entry_date}-${idx}`}>
                <TableCell className="tabular-nums">{row.code}</TableCell>
                <TableCell>{row.name}</TableCell>
                <TableCell>{row.route_label || routeLabel(row.route)}</TableCell>
                <TableCell>{row.entry_date}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(row.position_pct || row.slot_pct)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.entry_price || row.reference_close || row.execution_price)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.current_price_for_exit || row.current_price || row.reference_close)}</TableCell>
                <TableCell className="text-right">{pnlCell(row.pnl_ratio_for_exit)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.hard_stop)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.structure_stop || row.previous_low_stop)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.take_profit_1)}</TableCell>
                <TableCell><Badge color={row.paper_recorded ? 'emerald' : 'amber'}>{row.paper_recorded ? '已复现' : '待复现'}</Badge></TableCell>
                <TableCell><Badge color={managementActionType(row)}>{row.management_action}</Badge></TableCell>
                <TableCell className="max-w-[220px] truncate" >{exitReasonText(row)}</TableCell>
                <TableCell className="max-w-[300px] truncate">{exitContractText(row)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        <SectionTitle
          className="mb-2 mt-4 items-baseline"
          title={(
            <>
              影子盘持仓
              <small className="ml-2.5 text-xs font-normal text-zinc-500 dark:text-zinc-400">{shadowHoldingExplainText}</small>
            </>
          )}
        />
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['代码', '名称', '路由', '板块机会', '入场时间', '影子仓位', '入场/参考', '最新价', '实时盈亏', '硬止损', '结构保护', '止盈一', '退出建议', '触发原因', '纸面', '状态', '退出合同'].map((h) => (
                <TableHeader key={h}>{h}</TableHeader>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {currentShadowHoldingRows.length === 0 && <EmptyRow colSpan={17} text="暂无影子盘持仓" />}
            {currentShadowHoldingRows.map((row: Row, idx: number) => (
              <TableRow key={`${row.code}-${row.entry_date}-${idx}`}>
                <TableCell className="tabular-nums">{row.code}</TableCell>
                <TableCell>{row.name}</TableCell>
                <TableCell>{row.route_label || routeLabel(row.route)}</TableCell>
                <TableCell className="max-w-[300px] truncate">{opportunityText(row)}</TableCell>
                <TableCell className="max-w-[180px] truncate">{row.planned_entry_ts || row.confirm_datetime || row.entry_date || '--'}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(row.position_pct || row.slot_pct)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.entry_price || row.reference_close || row.execution_price)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(currentShadowPrice(row))}</TableCell>
                <TableCell className="text-right">{pnlCell(row.pnl_ratio_for_exit)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.hard_stop)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.structure_stop || row.previous_low_stop)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.take_profit_1)}</TableCell>
                <TableCell><Badge color={managementActionType(row)}>{row.management_action}</Badge></TableCell>
                <TableCell className="max-w-[220px] truncate">{exitReasonText(row)}</TableCell>
                <TableCell><Badge color={row.paper_recorded ? 'emerald' : 'amber'}>{row.paper_recorded ? '已复现' : '待复现'}</Badge></TableCell>
                <TableCell className="max-w-[200px] truncate">{compactStatusWithZh(row.shadow_status || row.last_state || row.status)}</TableCell>
                <TableCell className="max-w-[300px] truncate">{exitContractText(row)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      {/* 买入建议 */}
      <Card className="flex flex-col gap-3">
        <SectionTitle
          className="items-start"
          title={(
            <>
              买入建议
              <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">买入建议按当前策略候选批次展示；先用真实账户回答还能买多少，最终买入仓位受真实持仓、现金和交易时点约束。</span>
            </>
          )}
          actions={<Badge color={realAccountBuyCapacityType}>{realAccountBuyCapacityText}</Badge>}
        />

        <div className="flex min-h-[34px] flex-wrap items-center gap-2.5 rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-sm text-zinc-700 dark:border-white/10 dark:bg-white/5 dark:text-zinc-300">
          <Badge color={dateDisplayTagType}>{dateDisplayStatusText}</Badge>
          <span>{dateDisplayAnalysis.message || shadowTicketTableExplain}</span>
        </div>

        {selectedTicket.code ? (
          <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3 dark:border-white/10 dark:bg-white/5">
            <div className="mb-3 flex flex-col gap-2">
              <span className="w-fit rounded-md bg-blue-500 px-2 py-1 text-xs font-bold text-white">{selectedTicket.route_label || routeLabel(selectedTicket.route)}</span>
              <h3 className="text-xl font-bold text-zinc-900 dark:text-white">
                {selectedTicket.name} <small className="text-sm font-medium text-zinc-500 dark:text-zinc-400">{selectedTicket.code}</small>
              </h3>
              <div className="flex flex-wrap gap-3.5 text-sm text-zinc-600 dark:text-zinc-300">
                <span>计划时间 {plannedEntryDisplayText(selectedTicket)}</span>
                <span>确认时间 {selectedTicket.confirm_datetime || '--'}</span>
                <span>入场方式 {entryPolicyText(selectedTicket)}</span>
              </div>
              <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-2.5 py-2 text-sm leading-relaxed text-amber-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-300">{opportunityText(selectedTicket)}</div>
              <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-2.5 py-2 text-sm leading-relaxed text-amber-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-300">{selectedTicketExplainText}</div>
              <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-2.5 py-2 text-sm leading-relaxed text-amber-800 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-300">{naturalDisciplineText(selectedTicket)}</div>
            </div>
            <div className="mb-2.5 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
              {[
                { label: '合同仓位', value: pct(selectedTicket.position_pct) },
                { label: '可买仓位', value: pct(selectedTicketExecutablePositionPct) },
                { label: '参考价', value: price(selectedTicket.reference_close) },
                { label: '结构止损', value: price(selectedTicket.structure_stop) },
                { label: '硬止损', value: price(selectedTicket.hard_stop) },
                { label: '止盈一', value: price(selectedTicket.take_profit_1) },
              ].map((item) => (
                <StatCard key={item.label} label={item.label} value={item.value} />
              ))}
            </div>
            <div className="rounded-lg border border-zinc-200 bg-white p-2.5 text-sm text-zinc-600 dark:border-white/10 dark:bg-zinc-900 dark:text-zinc-300">{exitContractText(selectedTicket)}</div>
            <div className="mt-2.5 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white p-2.5 dark:border-white/10 dark:bg-zinc-900">
              <div className="flex min-w-0 flex-col gap-1">
                <span className="text-xs text-zinc-500 dark:text-zinc-400">真实账户执行依据</span>
                <strong className="text-base text-zinc-900 dark:text-white">{realAccountBuyCapacityText}</strong>
                <small className="truncate text-zinc-500 dark:text-zinc-400">{realAccountBuyCapacityReason}</small>
              </div>
              <Button color="blue" disabled={!canSubmitPaperOrder || paperSubmitting} onClick={submitSelectedPaperOrder}>
                {selectedPaperExecution ? '已记录纸面执行' : paperSubmitting ? '提交中…' : '记录纸面执行'}
              </Button>
            </div>
          </div>
        ) : (
          <EmptyState text="暂无合格买入建议票据" className="py-10" />
        )}

        <SectionTitle
          className="mt-3 mb-2 items-baseline"
          title={(
            <>
              {shadowTicketTableTitle}
              <small className="ml-2.5 text-xs font-normal text-zinc-500 dark:text-zinc-400">{shadowTicketTableExplain}</small>
            </>
          )}
        />
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['代码', '名称', '路由', '板块机会', '计划时间', '合同仓位', '自然纪律', '可买仓位', '参考价', '结构止损', '硬止损', '30m', '状态', '自然说明'].map((h) => (
                <TableHeader key={h}>{h}</TableHeader>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {displayShadowTickets.length === 0 && <EmptyRow colSpan={14} text="暂无买入建议票据" />}
            {displayShadowTickets.map((row: Row, idx: number) => (
              <TableRow key={`${row.code}-${idx}`}>
                <TableCell className="tabular-nums">{row.code}</TableCell>
                <TableCell>{row.name}</TableCell>
                <TableCell>{row.route_label}</TableCell>
                <TableCell className="max-w-[300px] truncate">{opportunityText(row)}</TableCell>
                <TableCell className="max-w-[180px] truncate">{plannedEntryDisplayText(row)}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(row.position_pct)}</TableCell>
                <TableCell>
                  <Badge color={naturalActionTagType(row)}>{row.natural_action_label || '观察'}</Badge>
                  <small className="mt-1 block text-xs text-zinc-500 dark:text-zinc-400">{pct(row.natural_position_pct)}</small>
                </TableCell>
                <TableCell className="text-right tabular-nums">{pct(executablePositionPct(row))}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.reference_close)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.structure_stop)}</TableCell>
                <TableCell className="text-right tabular-nums">{price(row.hard_stop)}</TableCell>
                <TableCell><Badge color={truthy(row.m30_confirmed) ? 'emerald' : 'red'}>{m30Text(row)}</Badge></TableCell>
                <TableCell className="max-w-[200px] truncate">{compactStatusWithZh(row.shadow_status)}</TableCell>
                <TableCell className="max-w-[240px] truncate">{naturalDisciplineText(row)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      {/* 运行闭环 */}
      <Card className="flex flex-col gap-3">
        <SectionTitle
          className="items-start"
          title={(
            <>
              运行闭环
              <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">迁移 G2 已成熟的运行能力：状态检查、阻断诊断、影子监控、手动刷新任务和运行记录。</span>
            </>
          )}
          actions={(
            <>
              <Button outline disabled={opsLoading} onClick={loadOps}>刷新运行状态</Button>
              <Button color="blue" disabled={opsRunLoading} onClick={runOpsOnce}>立即运行 G3 影子刷新</Button>
              <Button outline disabled={opsNotifyLoading} onClick={runNotifyProbe}>立即探测提醒</Button>
              <Button outline disabled={opsConfigLoading} onClick={toggleMonitor}>
                {monitorState.enabled ? '暂停监控' : '开启监控'}
              </Button>
            </>
          )}
        />
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3 xl:grid-cols-6">
          {[
            { tone: workflowStatus.ok ? toneOk : toneBlocked, label: '工作流状态', value: workflowStatus.ok ? '可运行' : '有阻断', note: workflowStatus.message || '--' },
            { tone: toneDefault, label: '影子监控', value: monitorState.enabled ? '开启' : '暂停', note: `${monitorState.interval_seconds || 120} 秒 / ${monitorState.trading_hours_only ? '交易时段' : '全天'}` },
            { tone: toneDefault, label: '最后成功', value: monitorState.last_success_at || '--', note: `最后运行 ${monitorState.last_run_at || '--'}` },
            { tone: workflowBlockers.length ? toneBlocked : toneOk, label: '阻断项', value: String(workflowBlockers.length), note: workflowBlockers[0]?.message || '暂无阻断' },
            { tone: toneDefault, label: '刷新任务', value: statusWithZh(refreshTask.status), note: refreshTask.message || refreshTask.task_id || '--' },
            { tone: monitorState.last_email_error ? toneBlocked : toneOk, label: '影子提醒', value: monitorState.email_enabled === false ? '关闭' : '开启', note: `最近邮件 ${monitorState.last_email_sent_at || '--'}` },
            { tone: monitorState.last_email_error ? toneBlocked : toneOk, label: '邮件异常', value: monitorState.last_email_error ? '有异常' : '正常', note: monitorState.last_email_error || '暂无异常' },
            { tone: toneLocked, label: 'G2 补位状态', value: `${parityCount}/${parityTotal}`, note: '已退出实盘交易，仅保留观察' },
          ].map((item) => (
            <StatCard key={item.label} label={item.label} value={item.value} tone={item.tone} hint={item.note} />
          ))}
        </div>
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['检查项', '状态', '说明', '更新时间'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {workflowChecks.length === 0 && <EmptyRow colSpan={4} text="暂无运行检查" />}
            {workflowChecks.map((row: Row, idx: number) => (
              <TableRow key={`${row.name}-${idx}`}>
                <TableCell>{row.name}</TableCell>
                <TableCell><Badge color={row.ok ? 'emerald' : 'red'}>{row.ok ? '通过' : '阻断'}</Badge></TableCell>
                <TableCell className="max-w-[300px] truncate">{row.message}</TableCell>
                <TableCell>{row.modified_at}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <Table dense striped className="mt-3">
          <TableHead>
            <TableRow>
              {['时间', '类型', '状态', '邮件', '说明', '异常'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {recentEvents.length === 0 && <EmptyRow colSpan={6} text="暂无运行事件" />}
            {recentEvents.map((row: Row, idx: number) => (
              <TableRow key={`${row.created_at}-${idx}`}>
                <TableCell>{row.created_at}</TableCell>
                <TableCell>{row.type}</TableCell>
                <TableCell>{row.status}</TableCell>
                <TableCell><Badge color={row.email_sent ? 'emerald' : 'zinc'}>{row.email_sent ? '已发' : '未发'}</Badge></TableCell>
                <TableCell className="max-w-[260px] truncate">{row.message}</TableCell>
                <TableCell className="max-w-[260px] truncate">{row.error}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      {/* G3 融合观察 */}
      <Card>
        <SectionTitle
          className="mb-3 items-start"
          title={(
            <>
              G3 融合观察
              <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">每天用 workflow、盘前烟测、30m 确认和纸面执行台账做一次验收；G2 补位只保留源和历史归因观察，不再作为实盘买入路线。</span>
            </>
          )}
          actions={(
            <>
              <Button outline disabled={observationLoading} onClick={loadObservations}>刷新观察</Button>
              <Button color="blue" disabled={observationRunLoading} onClick={runObservationOnce}>记录今日观察</Button>
            </>
          )}
        />
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3 xl:grid-cols-6">
          {[
            { tone: acceptedObservationDays >= 30 ? toneOk : toneBlocked, label: 'Score88 独立验证日', value: `${acceptedObservationDays}/30`, note: `从冻结合同后重新累计，不是最近30日交易数；旧合同快照 ${legacyObservationCount} 条不计入` },
            { tone: latestObservation?.accepted ? toneOk : toneBlocked, label: '最近快照', value: latestObservation?.status || '--', note: latestObservation?.observation_date || '--' },
            { tone: latestObservation?.smoke_ok ? toneOk : toneBlocked, label: '盘前烟测', value: latestObservation?.smoke_verdict || '--', note: latestObservation?.smoke_trade_action || '--' },
            { tone: latestObservation?.paper_ok ? toneOk : toneBlocked, label: '纸面复现', value: latestObservation?.paper_ok ? '通过' : '待补', note: `缺失 ${latestObservation?.missing_paper_count ?? 0} 笔` },
            { tone: observationScheduler.scheduler_enabled ? toneOk : toneBlocked, label: '自动观察', value: observationScheduler.enabled ? '开启' : '关闭', note: `${observationScheduler.hour ?? 15}:${String(observationScheduler.minute ?? 40).padStart(2, '0')}` },
            { tone: toneDefault, label: '下次验收', value: observationScheduler.next_run_time || '--', note: observationScheduler.last_error || '自动积累观察日' },
          ].map((item) => (
            <StatCard key={item.label} label={item.label} value={item.value} tone={item.tone} hint={item.note} />
          ))}
        </div>
        <Table dense striped className="mt-3">
          <TableHead>
            <TableRow>
              {['观察日', '状态', '分类', '动作', '烟测', '票据', '30m', '修复', '纸面', '记录时间', '阻断原因'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {observationSnapshots.length === 0 && <EmptyRow colSpan={11} text="暂无 G3 观察快照" />}
            {observationSnapshots.map((row: Row) => (
              <TableRow key={row.observation_date || row.created_at}>
                <TableCell>{row.observation_date}</TableCell>
                <TableCell><Badge color={row.accepted ? 'emerald' : 'red'}>{row.accepted ? '通过' : '阻断'}</Badge></TableCell>
                <TableCell className="max-w-[170px] truncate">{row.failure_category}</TableCell>
                <TableCell className="max-w-[190px] truncate">{row.action_required}</TableCell>
                <TableCell>{row.smoke_verdict}</TableCell>
                <TableCell className="text-right tabular-nums">{row.qualified_ticket_count}</TableCell>
                <TableCell><Badge color={row.m30_ok ? 'emerald' : 'red'}>{row.m30_ok ? '通过' : '阻断'}</Badge></TableCell>
                <TableCell><Badge color={row.minute30_repair_triggered ? 'amber' : 'zinc'}>{row.minute30_repair_triggered ? '已触发' : '未触发'}</Badge></TableCell>
                <TableCell><Badge color={row.paper_ok ? 'emerald' : 'red'}>{row.paper_ok ? '通过' : '阻断'}</Badge></TableCell>
                <TableCell>{row.created_at}</TableCell>
                <TableCell className="max-w-[300px] truncate">{observationBlockerText(row)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      {/* 指标 */}
      <section className="grid grid-cols-1 gap-2.5 sm:grid-cols-3 xl:grid-cols-6">
        {[
          { tone: toneDefault, label: '入场交易日', value: summary.entry_date || '--', note: `决策日 ${summary.decision_date || '--'}` },
          { tone: toneBlocked, label: '合格买入候选', value: String(summary.qualified_shadow_buy_rows ?? 0), note: `票据 ${shadowTickets.length} / 来源 ${summary.source_rows ?? 0}` },
          { tone: toneDefault, label: '今日选中路由', value: routeLabel(summary.selected_route), note: summary.route_reason || '--' },
          { tone: toneDefault, label: '全周期回测', value: pct(backtestProfile.total_return), note: backtestProfileText },
          { tone: toneDefault, label: '交易样本', value: fmt(backtestProfile.trade_count, 0), note: `胜率 ${pct(backtestProfile.win_rate)}` },
          { tone: g2SupplementStatus.live_enabled || g2SupplementStatus.ok ? toneOk : toneBlocked, label: 'G2补位源', value: g2SupplementStatus.live_enabled ? '启用' : '已退役', note: g2SupplementStatusText },
          { tone: toneBlocked, label: '默认合同', value: '2槽 / 50%', note: '四条实盘路由；G2补位仅观察' },
          { tone: toneDefault, label: '账户风控', value: summary.account_risk?.action || 'normal', note: summary.account_risk?.reason || '按影子台账判断' },
          { tone: toneLocked, label: '正式交易', value: '锁定', note: '正式买点与自动下单均关闭' },
        ].map((item) => (
          <StatCard key={item.label} label={item.label} value={item.value} tone={item.tone} hint={item.note} className="min-h-[96px]" />
        ))}
      </section>

      {/* 候选池入口 */}
      <Card>
        <SectionTitle
          className="mb-3"
          title={(
            <>
              候选池入口
              <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">工作台只展示候选概览；全量候选、逐日复盘和阻断明细进入候选池页。</span>
            </>
          )}
        />
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
          {[
            { label: '全量候选', value: String(candidates.length), note: '来自全部路由' },
            { label: '合格候选', value: String(eligibleCandidateCount), note: '可进入影子选择' },
            { label: '阻断候选', value: String(blockedCandidateCount), note: '进入候选池页看原因' },
          ].map((item) => (
            <StatCard key={item.label} label={item.label} value={item.value} hint={item.note} />
          ))}
        </div>
      </Card>

      {/* 影子台账 + 数据产物 */}
      <section className="grid grid-cols-1 gap-3.5 xl:grid-cols-2">
        <Card>
          <SectionTitle
            className="mb-3"
            title={(
              <>
                影子台账
                <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">同一票据只保留最新状态，用于后续补成交、退出和复盘。</span>
              </>
            )}
          />
          <Table dense striped>
            <TableHead>
              <TableRow>
                {['入场日', '代码', '名称', '状态', '阶段'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
              </TableRow>
            </TableHead>
            <TableBody>
              {shadowLedger.length === 0 && <EmptyRow colSpan={5} text="暂无影子台账" />}
              {shadowLedger.map((row: Row, idx: number) => (
                <TableRow key={`${row.code}-${row.entry_date}-${idx}`}>
                  <TableCell>{row.entry_date}</TableCell>
                  <TableCell className="tabular-nums">{row.code}</TableCell>
                  <TableCell>{row.name}</TableCell>
                  <TableCell className="max-w-[200px] truncate">{compactStatusWithZh(row.shadow_status)}</TableCell>
                  <TableCell className="max-w-[150px] truncate">{compactStatusWithZh(row.last_state)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>

        <Card>
          <SectionTitle
            className="mb-3"
            title={(
              <>
                数据产物
                <span className="mt-1 block text-sm font-normal leading-relaxed text-zinc-500 dark:text-zinc-400">页面所有结论都要能追到运行产物，方便审计一致性。</span>
              </>
            )}
          />
          <Table dense striped>
            <TableHead>
              <TableRow>
                {['产物', '状态', '更新时间', '路径'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
              </TableRow>
            </TableHead>
            <TableBody>
              {artifactRows.length === 0 && <EmptyRow colSpan={4} text="暂无产物信息" />}
              {artifactRows.map((row) => (
                <TableRow key={row.name}>
                  <TableCell>{row.name}</TableCell>
                  <TableCell><Badge color={row.exists ? 'emerald' : 'red'}>{row.exists ? '存在' : '缺失'}</Badge></TableCell>
                  <TableCell>{row.modified_at}</TableCell>
                  <TableCell className="max-w-[340px] truncate">{row.path}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </section>
    </div>
  )
}

function firstPositiveNumber(...values: any[]): number | null {
  for (const value of values) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

function shadowPnlRatio(row: Row, currentShadowPriceFn: (row: Row) => number | null): number | null {
  const entry = Number(row?.entry_price ?? row?.reference_close ?? row?.execution_price)
  const current = Number(currentShadowPriceFn(row))
  if (!Number.isFinite(entry) || entry <= 0 || !Number.isFinite(current) || current <= 0) return null
  return current / entry - 1
}

function normalizeManagementRow(raw: Row, source: string, paperExecutions: Row[], currentShadowPriceFn: (row: Row) => number | null): Row {
  const key = String(raw?.ticket_key || raw?.candidate_key || raw?.trade_key || '')
  const code = String(raw?.code || raw?.code_raw || '').toUpperCase()
  const entryDate = String(raw?.entry_date || raw?.signal_date || '')
  const paper = paperExecutions.find((item: Row) => {
    if (key && item.ticket_key === key) return true
    return !!(code && entryDate && String(item.code || '').toUpperCase() === code && item.entry_date === entryDate)
  }) || null
  return {
    ...raw,
    code,
    entry_date: String(raw?.entry_date || raw?.signal_date || ''),
    paper_recorded: !!paper || source === 'broker' || truthy(raw?.paper_recorded),
    management_action: raw?.exit_action_label || raw?.management_action || managementAction(raw, paper),
    pnl_ratio_for_exit: raw?.pnl_ratio_for_exit ?? shadowPnlRatio(raw, currentShadowPriceFn),
  }
}

function isCurrentShadowHolding(row: Row, latestCreatedAt: string): boolean {
  if (!row?.code && !row?.code_raw) return false
  const createdAt = String(row?.created_at || '').trim()
  if (latestCreatedAt && createdAt !== latestCreatedAt) return false
  const status = String(row?.shadow_status || row?.trade_status || row?.status || row?.last_state || '').toLowerCase()
  if (['exit', 'sell', 'sold', 'closed', 'cancel', 'invalid', 'expired'].some((token) => status.includes(token))) return false
  if (['退出', '卖出', '已清仓', '已关闭', '取消', '失效', '过期'].some((token) => status.includes(token))) return false
  return truthy(row?.qualified_shadow_buy) || status.includes('qualified') || status.includes('open') || status.includes('hold') || status.includes('planned')
}

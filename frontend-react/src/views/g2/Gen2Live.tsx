import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import request from '@/utils/request'
import {
  getGen2Live,
  getV4MarketGate,
  getGen2RiskCoolShadow,
  getGen2WorkflowStatus,
  getGen2DailyTradeTicket,
  getGen2DailyTradeExecution,
  saveGen2DailyTradeExecution,
  runGen2RiskCoolShadowUpdate,
  getGen2RiskCoolShadowUpdateTask,
  saveGen2ShadowVerification,
  getGen2ShadowMonitorStatus,
  setGen2ShadowMonitorConfig,
  runGen2ShadowMonitorNow,
  getGen2StrategyRefreshStatus,
  runGen2StrategyRefreshNow,
  getV4ManualHoldingSignals,
  getV4ManualHoldingQuotes,
  refreshV4ManualHoldingAll,
  saveV4ManualHoldingState,
  getV4ManualHoldingMonitorStatus,
  setV4ManualHoldingMonitorConfig,
  runV4ManualHoldingMonitorNow,
  getV4ManualHoldingEntryBacktest,
  readV4ManualHoldingThsCurrentTable,
  readV4ManualHoldingThsDeliveryFile,
  readV4ManualHoldingThsCapitalHoldings,
} from '@/api/trading'
import { getStockDetail, getStockHistory, searchStocks } from '@/api/stock'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Checkbox, CheckboxField } from '@/components/catalyst/checkbox'
import { Dialog, DialogActions, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Field, Label } from '@/components/catalyst/fieldset'
import { Input } from '@/components/catalyst/input'
import { Select } from '@/components/catalyst/select'
import { Textarea } from '@/components/catalyst/textarea'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import { useToast } from '@/components/toast'
import {
  Card,
  ChangeValue,
  EmptyState,
  PageHeader,
  PageLoading,
  SectionTitle,
  StatCard,
} from '@/components/ui'

const MANUAL_HOLDINGS_KEY = 'v4_manual_holdings'
const MANUAL_TRADES_KEY = 'v4_manual_trade_logs'
const V4_CAPITAL_PROFILE_KEY = 'v4_capital_profile'
const STOCK_DETAIL_TRADE_MARKS_PREFIX = 'stock_detail_trade_marks_'
const FIXED_BASE_CAPITAL = 150000
const GEN2_MAIN_ALPHA191_GATE = 'g2_v2_complete'

const STRONG_CHECK_MIN_WIN_RATE_5D = 55
const STRONG_CHECK_MIN_AVG_RETURN_5D = 0.8
const STRONG_CHECK_MIN_WIN_RATE_10D = 50
const STRONG_CHECK_MIN_AVG_RETURN_10D = 1.2
const TRAILING_TP_ACTIVATE_PNL = 5
const TRAILING_TP_DRAWDOWN = 4
const SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL = 2

const AUTO_REFRESH_INTERVAL_MS = 15 * 1000
const AUTO_REFRESH_HOLDINGS_MIN_MS = 30 * 1000
const AUTO_REFRESH_MARKET_GATE_MIN_MS = 60 * 1000
const AUTO_REFRESH_GEN2_SHADOW_MIN_MS = 180 * 1000
const AUTO_REFRESH_WORKFLOW_MIN_MS = 180 * 1000

const THS_FILTER_KEYWORDS = ['打新', '新股', '配号', '超配', '红利', '股息', '清算', '利税', '利息', '配售']

type BadgeColor = Parameters<typeof Badge>[0]['color']

// ---------- 纯工具函数 ----------

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function nowMinuteText(): string {
  const d = new Date()
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function nowSecondText(): string {
  const d = new Date()
  return `${nowMinuteText()}:${pad2(d.getSeconds())}`
}

function normalizeCode(value: unknown): string {
  return String(value || '')
    .replace(/\D/g, '')
    .slice(0, 6)
}

function toExchangeCode(value: unknown): string {
  const code = normalizeCode(value)
  if (!code) return ''
  if (code.startsWith('6')) return `${code}.SH`
  if (code.startsWith('4') || code.startsWith('8')) return `${code}.BJ`
  return `${code}.SZ`
}

function displayCode(value: unknown): string {
  const raw = String(value || '').trim().toUpperCase()
  if (!raw) return '--'
  if (raw.includes('.')) return raw
  const code = normalizeCode(raw)
  if (!code) return raw
  if (code.startsWith('6')) return `${code}.SH`
  if (code.startsWith('4') || code.startsWith('8')) return `${code}.BJ`
  return `${code}.SZ`
}

function parseTimeMs(value: unknown): number {
  const txt = String(value || '').trim()
  if (!txt) return 0
  const ts = new Date(txt.replace(' ', 'T')).getTime()
  return Number.isFinite(ts) ? ts : 0
}

function weekKeyFromTime(buyTime?: string): string {
  const base = buyTime ? new Date(String(buyTime).replace(' ', 'T')) : new Date()
  const year = base.getFullYear()
  const daySeed = base.getMonth() * 31 + base.getDate()
  return `${year}-${Math.ceil(daySeed / 7)}`
}

function tradeSignature(item: any): string {
  const time = String(item?.time || '').trim()
  const side = String(item?.side || '').trim()
  const code = normalizeCode(item?.code)
  const shares = Math.abs(Math.trunc(Number(item?.shares || 0)))
  const price = Number(item?.price)
  return [time, side, code, shares, Number.isFinite(price) ? price.toFixed(3) : ''].join('|')
}

function isAshareTradingTime(base = new Date()): boolean {
  const day = base.getDay()
  if (day === 0 || day === 6) return false
  const minutes = base.getHours() * 60 + base.getMinutes()
  const morningStart = 9 * 60 + 25
  const morningEnd = 11 * 60 + 35
  const afternoonStart = 12 * 60 + 55
  const afternoonEnd = 15 * 60 + 5
  return (
    (minutes >= morningStart && minutes <= morningEnd) ||
    (minutes >= afternoonStart && minutes <= afternoonEnd)
  )
}

function formatPct(value: unknown): string {
  if (value === null || value === undefined || value === '') return '--'
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : '--'
}

function formatScore(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(4) : '--'
}

function formatRatio(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(2)}倍` : '--'
}

function formatMoney(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(2) : '--'
}

function formatPrice(value: unknown): string {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n.toFixed(3) : '--'
}

/** A股红涨绿跌 */
function pnlClass(value: unknown, emphasize = false): string {
  const n = Number(value)
  const base = emphasize ? 'font-bold ' : ''
  if (Number.isFinite(n) && n > 0) return `${base}text-red-600 dark:text-red-400 tabular-nums`
  if (Number.isFinite(n) && n < 0) return `${base}text-green-600 dark:text-green-500 tabular-nums`
  return `${base}text-zinc-700 dark:text-zinc-300 tabular-nums`
}

function normalizeTradeTime(dateText: unknown, timeText: unknown): string {
  const ds = String(dateText || '').replace(/\D/g, '')
  if (ds.length !== 8) return ''
  const day = `${ds.slice(0, 4)}-${ds.slice(4, 6)}-${ds.slice(6, 8)}`
  const rawTime = String(timeText || '').trim()
  if (!rawTime) return `${day} 00:00:00`
  const parts = rawTime.split(':').map((x) => x.padStart(2, '0'))
  if (parts.length === 2) return `${day} ${parts[0]}:${parts[1]}:00`
  if (parts.length >= 3) return `${day} ${parts[0]}:${parts[1]}:${parts[2]}`
  return `${day} 00:00:00`
}

function parseThsNumber(value: unknown): number | null {
  const text = String(value ?? '')
    .replace(/,/g, '')
    .trim()
  if (!text) return null
  const num = Number(text)
  return Number.isFinite(num) ? num : null
}

function parseThsTradeSide(action: unknown): string {
  const text = String(action || '').trim()
  if (text === '买入' || text.includes('买入')) return '买入'
  if (text === '卖出' || text.includes('卖出')) return '卖出'
  return ''
}

function extractLatestPriceFromDetail(detail: any): number | null {
  const candidates = [
    detail?.price,
    detail?.close,
    detail?.latest_close,
    detail?.current_price,
    detail?.last_price,
    detail?.kline?.close,
  ]
  for (const value of candidates) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

function extractLatestPriceFromHistory(historyResp: any): number | null {
  const rows: any[] = Array.isArray(historyResp)
    ? historyResp
    : Array.isArray(historyResp?.items)
      ? historyResp.items
      : Array.isArray(historyResp?.data)
        ? historyResp.data
        : []
  if (!rows.length) return null
  const last = rows[rows.length - 1] || {}
  for (const value of [last?.close, last?.price, last?.last_price, last?.current_price]) {
    const n = Number(value)
    if (Number.isFinite(n) && n > 0) return n
  }
  return null
}

function epTagType(type: unknown): BadgeColor {
  const text = String(type || '')
  if (['success', 'emerald', 'green'].includes(text)) return 'emerald'
  if (['danger', 'error', 'red'].includes(text)) return 'red'
  if (['warning', 'warn', 'amber', 'yellow'].includes(text)) return 'amber'
  if (['primary', 'blue'].includes(text)) return 'blue'
  return 'zinc'
}

const WORKFLOW_STAGE_LABELS: Record<string, string> = {
  data_source: '数据源',
  selection: '选股生成',
  selection_context: '板块上下文',
  scoring: '评分因子',
  strategy: '策略产物',
  intraday_data: '盘中数据',
  notification: '通知链路',
}

function workflowStageLabel(stage: unknown): string {
  return WORKFLOW_STAGE_LABELS[String(stage || '')] || String(stage || '--')
}

function isBuyTradeSide(value: unknown): boolean {
  const text = String(value || '').trim().toLowerCase()
  return text === 'buy' || text.includes('买') || text.includes('涔')
}

function isSellTradeSide(value: unknown): boolean {
  const text = String(value || '').trim().toLowerCase()
  return text === 'sell' || text.includes('卖') || text.includes('鍗')
}

// ---------- 组件 ----------

export default function Gen2Live() {
  const toast = useToast()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  // 基础快照
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [available, setAvailable] = useState(false)
  const [message, setMessage] = useState('')
  const [targetDate, setTargetDate] = useState('')
  const [payload, setPayload] = useState<any>({})

  // 持仓/资金（localStorage 持久化；ref 镜像保证异步链读到最新值）
  const [manualHoldings, setManualHoldings] = useState<any[]>([])
  const holdingsRef = useRef<any[]>([])
  const [manualTradeLogs, setManualTradeLogs] = useState<any[]>([])
  const tradeLogsRef = useRef<any[]>([])
  const [capitalForm, setCapitalForm] = useState<any>({
    base_capital: FIXED_BASE_CAPITAL,
    realized_pnl: 0,
    synced_total_capital: null,
    synced_available_cash: null,
    synced_market_value: null,
    synced_cost_value: null,
    synced_at: '',
  })
  const capitalRef = useRef<any>(capitalForm)

  const setHoldings = (next: any[]) => {
    holdingsRef.current = next
    setManualHoldings(next)
  }
  const setTradeLogs = (next: any[]) => {
    tradeLogsRef.current = next
    setManualTradeLogs(next)
  }
  const setCapital = (next: any) => {
    capitalRef.current = next
    setCapitalForm(next)
  }

  // 刷新状态
  const [signalLoading, setSignalLoading] = useState(false)
  const [allRefreshLoading, setAllRefreshLoading] = useState(false)
  const [holdingRefreshLoading, setHoldingRefreshLoading] = useState(false)
  const [thsCapitalSyncLoading, setThsCapitalSyncLoading] = useState(false)
  const [marketGateLoading, setMarketGateLoading] = useState(false)
  const [marketGate, setMarketGate] = useState<any>(null)
  const marketGateRef = useRef<any>(null)

  // G2 影子 / 工作流 / 交易单
  const [gen2ShadowLoading, setGen2ShadowLoading] = useState(false)
  const [gen2Shadow, setGen2Shadow] = useState<any>(null)
  const gen2ShadowRef = useRef<any>(null)
  const [gen2ShadowUpdateLoading, setGen2ShadowUpdateLoading] = useState(false)
  const [gen2ShadowUpdateTask, setGen2ShadowUpdateTask] = useState<any>(null)
  const [gen2ShadowMonitorLoading, setGen2ShadowMonitorLoading] = useState(false)
  const [gen2ShadowMonitorStatus, setGen2ShadowMonitorStatus] = useState<any>(null)
  const [gen2StrategyRefreshLoading, setGen2StrategyRefreshLoading] = useState(false)
  const [gen2StrategyRefreshRunning, setGen2StrategyRefreshRunning] = useState(false)
  const [gen2StrategyRefreshStatus, setGen2StrategyRefreshStatus] = useState<any>(null)
  const [workflowLoading, setWorkflowLoading] = useState(false)
  const [workflowStatus, setWorkflowStatus] = useState<any>(null)
  const [dailyTradeTicketLoading, setDailyTradeTicketLoading] = useState(false)
  const [dailyTradeTicket, setDailyTradeTicket] = useState<any>(null)
  const dailyTicketRef = useRef<any>(null)
  const [dailyExecutionLoading, setDailyExecutionLoading] = useState(false)
  const [dailyExecutionSaving, setDailyExecutionSaving] = useState(false)
  const [dailyExecution, setDailyExecution] = useState<any>(null)
  const [dailyExecutionForm, setDailyExecutionForm] = useState<any>({
    execution_status: 'pending',
    executed: false,
    discipline_ok: true,
    violation_tags: [] as string[],
    execution_note: '',
    t1_review: '',
    t3_review: '',
    t5_review: '',
  })
  const dailyExecFormRef = useRef<any>(dailyExecutionForm)
  dailyExecFormRef.current = dailyExecutionForm

  // 监控
  const [monitorEnabled, setMonitorEnabled] = useState(false)
  const [monitorLoading, setMonitorLoading] = useState(false)
  const [monitorStatusText, setMonitorStatusText] = useState('未开启')
  const [monitorTradingHoursOnly, setMonitorTradingHoursOnly] = useState(true)
  const [monitorQuietMinutes, setMonitorQuietMinutes] = useState(15)
  const [autoRefreshRunning, setAutoRefreshRunning] = useState(false)
  const [lastAutoRefreshAt, setLastAutoRefreshAt] = useState('')

  // 弹窗
  const [gen2VerificationDialogVisible, setGen2VerificationDialogVisible] = useState(false)
  const [gen2VerificationSaving, setGen2VerificationSaving] = useState(false)
  const [gen2VerificationForm, setGen2VerificationForm] = useState<any>({
    row: null,
    status: '',
    note: '',
    position_pct: null,
    fill_price: null,
  })
  const [addDialogVisible, setAddDialogVisible] = useState(false)
  const [addForm, setAddForm] = useState<any>({
    code: '',
    name: '',
    shares: 0,
    cost_price: null as number | null,
    buy_time: '',
    source: '',
    signal_date: '',
  })
  const addFormRef = useRef<any>(addForm)
  addFormRef.current = addForm
  const [codeLookupText, setCodeLookupText] = useState('')
  const [codeSuggestions, setCodeSuggestions] = useState<any[]>([])
  const [liveQuotePrice, setLiveQuotePrice] = useState<number | null>(null)
  const [entryBacktestLoading, setEntryBacktestLoading] = useState(false)
  const [entryBacktest, setEntryBacktest] = useState<any>(null)
  const entryBacktestRef = useRef<any>(null)
  const [ruleDialogVisible, setRuleDialogVisible] = useState(false)
  const [ruleChecklist, setRuleChecklist] = useState<any[]>([])
  const [ruleCheckPassed, setRuleCheckPassed] = useState(false)
  const pendingHoldingPayload = useRef<any>(null)
  const [sellDialogVisible, setSellDialogVisible] = useState(false)
  const [sellForm, setSellForm] = useState<any>({
    index: -1,
    code: '',
    name: '',
    max_shares: 0,
    shares: 0,
    price: null as number | null,
    time: '',
    reason: '',
  })
  const [tradeLogVisible, setTradeLogVisible] = useState(false)
  const [thsImportVisible, setThsImportVisible] = useState(false)
  const [thsImportLoading, setThsImportLoading] = useState(false)
  const [thsImportRawText, setThsImportRawText] = useState('')
  const [thsImportRows, setThsImportRows] = useState<any[]>([])
  const [historyPage, setHistoryPage] = useState(1)
  const [historyPageSize, setHistoryPageSize] = useState(20)
  const [expandedHistoryCodes, setExpandedHistoryCodes] = useState<Set<string>>(new Set())

  const codeLookupTimer = useRef<number | null>(null)
  const autoRefreshTimer = useRef<number | null>(null)
  const lastHoldingAutoRefreshAt = useRef(0)
  const lastMarketGateAutoRefreshAt = useRef(0)
  const lastGen2ShadowAutoRefreshAt = useRef(0)
  const lastWorkflowAutoRefreshAt = useRef(0)

  const focusCode = String(searchParams.get('focus_code') || '').trim()
  const focusName = String(searchParams.get('focus_name') || '').trim()
  const focusSource = String(searchParams.get('source') || '').trim()
  const focusSignalDate = String(searchParams.get('signal_date') || '').trim()
  const focusDescription =
    `${focusSource ? `来源：${focusSource}` : '来源：手工入口'}${focusSignalDate ? `；信号日：${focusSignalDate}` : ''}。该入口用于把交易选股中的目标股直接加入实盘交易持仓。`

  // ---------- 派生数据 ----------

  const selection = payload?.selection || {}
  const positions = payload?.positions || {}
  const fallback = payload?.fallback || null
  const selectedDate = payload?.selected_date || '--'
  const requestedDate = payload?.requested_date || ''
  const latestTradeDate = payload?.latest_trade_date || '--'

  const selectionBranchFreshness = selection?.branch_freshness || {}
  const selectionBranchFreshnessWarning =
    selectionBranchFreshness?.status === 'stale'
      ? `${selectionBranchFreshness?.text || 'g2_v2_complete 正式展示分支滞后'}；当前实盘执行继续按 latest-driven 主链判断，不会因为旧展示分支停在历史而停摆。`
      : ''

  const liveDateResolution = payload?.date_resolution || null
  const dateResolutionNotice = useMemo(() => {
    const requested = String(requestedDate || '').trim()
    const sel = String(selectedDate || '').trim()
    if (!requested || !sel || requested === sel) return ''
    if (requested === '--') return ''
    const reason = String(liveDateResolution?.reason || '').trim()
    const extra = reason ? ` (${reason})` : ''
    if (requested > sel) return `Request date ${requested} not ready; fallback to ${sel}${extra}`
    return `Request date ${requested} differs from displayed date ${sel}`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestedDate, selectedDate, liveDateResolution])

  const liveFallbackNotice = useMemo(() => {
    const title = String(fallback?.title || payload?.fallback_notice || '').trim()
    if (title) return title
    if (payload?.requested_date && payload?.selected_date && payload.requested_date !== payload.selected_date) {
      return `Request date ${payload.requested_date} is incomplete; displaying ${payload.selected_date}`
    }
    return ''
  }, [fallback, payload])

  const officialRebuild = payload?.official_rebuild || workflowStatus?.official_rebuild || null
  const officialRebuildNotice = useMemo(() => {
    const rebuild: any = officialRebuild
    if (!rebuild || typeof rebuild !== 'object') return ''
    const status = String(rebuild.status || '').trim().toLowerCase()
    if (status === 'queued' || status === 'running') {
      return `Official rebuild ${status === 'queued' ? 'queued' : 'running'} (signal_date=${String(rebuild.signal_date || selectedDate || '--')})`
    }
    if (status === 'completed' || status === 'failed') {
      if (rebuild.ok) return ''
      const err = String(rebuild.error || '').trim()
      return `Official rebuild failed (signal_date=${String(rebuild.signal_date || selectedDate || '--')})${err ? `: ${err}` : ''}`
    }
    return ''
  }, [officialRebuild, selectedDate])

  const gen2StrategyRefreshLastResultText = useMemo(() => {
    const result = gen2StrategyRefreshStatus?.last_result || {}
    if (!result || !Object.keys(result).length) return '--'
    if (result.skipped) return result.reason || '已跳过'
    if (result.ok === false) return result.error || '刷新失败'
    const signalDate = result.signal_date ? ` ${result.signal_date}` : ''
    return `成功${signalDate}`
  }, [gen2StrategyRefreshStatus])

  const gen2StrategyRefreshSummary = useMemo(() => {
    const status = gen2StrategyRefreshStatus || {}
    if (gen2StrategyRefreshLoading && !status.next_run_time) return '正在读取 G2 半小时策略刷新状态...'
    if (status.last_error) return `最近一次刷新异常：${status.last_error}`
    const delay = status.data_delay_minutes ?? 2
    const next = status.next_run_time || '--'
    const last = status.last_success_at || status.last_run_at || '--'
    return `按 30m 数据落盘后 ${delay} 分钟触发；下次 ${next}；上次 ${last}；结果 ${gen2StrategyRefreshLastResultText}`
  }, [gen2StrategyRefreshStatus, gen2StrategyRefreshLoading, gen2StrategyRefreshLastResultText])

  const workflowStages = useMemo(() => {
    const stages: any[] = Array.isArray(workflowStatus?.pipeline_stages) ? workflowStatus.pipeline_stages : []
    return stages.map((stage) => ({
      ...stage,
      failed_count: Array.isArray(stage.failed) ? stage.failed.length : 0,
      check_count: Array.isArray(stage.checks) ? stage.checks.length : 0,
    }))
  }, [workflowStatus])

  const workflowFailedChecks = useMemo(() => {
    const blockers: any[] = Array.isArray(workflowStatus?.blockers) ? workflowStatus.blockers : []
    if (blockers.length) return blockers
    return workflowStages.flatMap((stage) => (Array.isArray(stage.failed) ? stage.failed : []))
  }, [workflowStatus, workflowStages])

  const notificationStage = workflowStages.find((stage) => stage.stage === 'notification')
  const notificationStageExists = !!notificationStage
  const notificationStageOk = notificationStage?.ok === true
  const notificationStageLabel = notificationStageOk ? '通知链路正常' : '通知链路异常'
  const workflowLineageRows: any[] = Array.isArray(workflowStatus?.lineage_items)
    ? workflowStatus.lineage_items
    : []
  const workflowOk =
    workflowStatus?.available !== false && workflowStages.length > 0 && workflowStages.every((stage) => !!stage.ok)
  const workflowSummaryText = useMemo(() => {
    const data = workflowStatus || {}
    if (workflowLoading && !data.checked_at) return '正在检查策略流水线...'
    if (!data.available && data.message) return data.message
    const failed = workflowFailedChecks.length
    const checkedAt = data.checked_at ? `; 检查 ${data.checked_at}` : ''
    const prev = data.prev_trade_date ? `; D-1 ${data.prev_trade_date}` : ''
    const gate = data.alpha191_gate ? `; Gate ${data.alpha191_gate}` : ''
    const notificationText = notificationStageExists
      ? notificationStageOk
        ? '; 通知链路通过'
        : '; 通知链路异常'
      : '; 未检测到通知链路'
    return failed
      ? `${String(data.signal_date || selectedDate)} 有 ${failed} 个阻塞点${prev}${gate}${notificationText}${checkedAt}`
      : `${String(data.signal_date || selectedDate)} 数据源、选股、评分、策略和通知链路通过${prev}${gate}${notificationText}${checkedAt}`
  }, [workflowStatus, workflowLoading, workflowFailedChecks, notificationStageExists, notificationStageOk, selectedDate])

  const workflowStageMeta = (stage: any) => {
    const total = Number(stage?.check_count || stage?.checks?.length || 0)
    const failed = Number(stage?.failed_count || stage?.failed?.length || 0)
    return stage?.ok ? `${total}项检查通过` : `${failed}/${total}项未通过`
  }

  const workflowCheckResult = (row: any) => {
    if (row?.row_count !== undefined) return `rows ${String(row.row_count)}`
    if (row?.code_count !== undefined) return `codes ${String(row.code_count)}`
    if (row?.size !== undefined) return `${String(row.size)} bytes`
    if (row?.max_datetime) return row.max_datetime
    return row?.ok ? 'ok' : 'blocked'
  }

  const workflowLineageResult = (row: any) => {
    const status = row?.status || {}
    return status.target_rows !== undefined && status.target_rows !== null
      ? `rows ${String(status.target_rows)}`
      : row?.ok
        ? 'ok'
        : 'pending'
  }

  const dailyTradeTicketFormalRows: any[] = Array.isArray(dailyTradeTicket?.formal_candidates)
    ? dailyTradeTicket.formal_candidates
    : []
  const dailyTradeTicketForbiddenActions: string[] = Array.isArray(dailyTradeTicket?.forbidden_actions)
    ? dailyTradeTicket.forbidden_actions
    : []
  const dailyTradeTicketSummary = useMemo(() => {
    const ticket = dailyTradeTicket || {}
    if (dailyTradeTicketLoading && !ticket.generated_at) return '正在生成 G2 V4 今日交易单...'
    if (!ticket.available && ticket.message) return ticket.message
    const count = dailyTradeTicketFormalRows.length
    const generated = ticket.generated_at ? `；生成 ${ticket.generated_at}` : ''
    return `状态 ${ticket.market_state || '--'}；仓位 ${ticket.target_exposure || '--'}；正式候选 ${count} 只${generated}`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dailyTradeTicket, dailyTradeTicketLoading, dailyTradeTicketFormalRows.length])
  const dailyTradeTicketOutputs = useMemo(() => {
    const outputs = dailyTradeTicket?.outputs || {}
    const jsonPath = outputs.json_path || outputs.latest_path || ''
    const mdPath = outputs.markdown_path || ''
    if (jsonPath && mdPath) return `${jsonPath}；${mdPath}`
    return jsonPath || mdPath || ''
  }, [dailyTradeTicket])

  const dailyExecutionRecord = dailyExecution?.record || dailyTradeTicket?.execution_record || null
  const dailyExecutionStatusText = useMemo(() => {
    const status = String(dailyExecutionForm.execution_status || dailyExecutionRecord?.execution_status || 'pending')
    const labels: Record<string, string> = {
      pending: '待记录',
      followed: '照单执行',
      no_trade: '无交易',
      partial: '部分执行',
      violated: '纪律违规',
      reviewed: '已复盘',
    }
    return labels[status] || status
  }, [dailyExecutionForm.execution_status, dailyExecutionRecord])
  const dailyExecutionSummary = useMemo(() => {
    const record = dailyExecutionRecord
    if (dailyExecutionLoading && !record) return '正在读取今日执行台账...'
    if (!record) return '今日交易单尚未回填执行记录'
    const updated = record.updated_at ? `；更新 ${record.updated_at}` : ''
    const discipline = record.discipline_ok === false ? '；存在纪律问题' : '；纪律正常'
    return `${dailyExecutionStatusText}${discipline}${updated}`
  }, [dailyExecutionRecord, dailyExecutionLoading, dailyExecutionStatusText])

  const gen2ShadowRows: any[] = Array.isArray(gen2Shadow?.rows) ? gen2Shadow.rows : []
  const gen2VerificationQueueRows: any[] = Array.isArray(gen2Shadow?.verification_queue)
    ? gen2Shadow.verification_queue
    : []
  const gen2VerificationOptions = useMemo(() => {
    const options = gen2Shadow?.verification_options
    if (Array.isArray(options) && options.length) return options
    return [
      { value: '', label: '未标记', type: 'info' },
      { value: 'watch', label: '观察', type: 'warning' },
      { value: 'paper', label: '模拟跟踪', type: 'primary' },
      { value: 'small_buy', label: '小仓买入', type: 'success' },
      { value: 'skip', label: '放弃', type: 'info' },
      { value: 'reject', label: '应过滤', type: 'danger' },
    ]
  }, [gen2Shadow])

  const gen2ShadowCanAddHolding = (row: any) => {
    if (!row || row.is_suspended) return false
    return row.can_buy === true || row.buyable === true || row.stage_label === '可买'
  }

  const shadowReviewRows = useMemo(
    () => gen2ShadowRows.filter((row) => !gen2ShadowCanAddHolding(row)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [gen2ShadowRows],
  )

  const gen2ShadowSummaryDisplay = useMemo(() => {
    const data = gen2Shadow || {}
    if (!data.available) return data.message || 'G2 shadow review pending'
    const counts = data.counts || {}
    const observable = counts.observable || 0
    const suspended = (counts.suspended_by_two_stop_cd3 || 0) + (counts.suspended_by_stop_cd5 || 0)
    const executed = counts.executed || 0
    const displayDate = String(data.display_signal_date || data.selection_signal_date || data.signal_date || '--')
    const ledgerDate = String(data.ledger_signal_date || data.signal_date || '--')
    const summaryCore = `${displayDate} shadow rows ${String(data.row_count || gen2ShadowRows.length)}, observable ${String(observable)}, suspended ${String(suspended)}, executed ${String(executed)}`
    if (data.is_ledger_fallback && ledgerDate && ledgerDate !== displayDate) {
      return `${summaryCore}; ledger=${ledgerDate}`
    }
    return summaryCore
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen2Shadow, gen2ShadowRows.length])

  const gen2VerificationSummaryText = useMemo(() => {
    const summary = gen2Shadow?.verification_global_summary || gen2Shadow?.verification_summary || {}
    const marked = Number(summary.marked || 0)
    const total = Number(summary.total || gen2ShadowRows.length || 0)
    const actionCount = Number(summary.action_count || 0)
    const rejectedCount = Number(summary.rejected_count || 0)
    const stopCount = Number(summary.stop5_touch_count || 0)
    const avg5 = Number(summary.avg_marked_fwd5_pct)
    const avg10 = Number(summary.avg_marked_fwd10_pct)
    const avg20 = Number(summary.avg_marked_fwd20_pct)
    const avgText = Number.isFinite(avg5)
      ? `已标记均值 5/10/20日 ${avg5.toFixed(2)}% / ${Number.isFinite(avg10) ? avg10.toFixed(2) : '--'}% / ${Number.isFinite(avg20) ? avg20.toFixed(2) : '--'}%`
      : '已标记样本暂无完整跟踪收益'
    return `全局验证台账：已标记 ${String(marked)}/${String(total)}，观察/模拟/小仓 ${String(actionCount)}，放弃/应过滤 ${String(rejectedCount)}，30m止损 ${String(stopCount)}，${avgText}`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen2Shadow, gen2ShadowRows.length])

  const gen2VerificationPromotionSource =
    gen2Shadow?.verification_global_summary || gen2Shadow?.verification_summary || {}
  const gen2VerificationPromotionLabel = gen2VerificationPromotionSource?.promotion_label || '样本收集中'
  const gen2VerificationPromotionMessage =
    gen2VerificationPromotionSource?.promotion_message || '至少先累计 20 条已标记样本，再判断是否进入小仓实盘验证。'

  const gen2ShadowFreshnessNote = useMemo(() => {
    const d = gen2Shadow?.data_freshness || {}
    const dateNotice = String(gen2Shadow?.date_notice || '').trim()
    if (dateNotice) return dateNotice
    if (!d.latest_daily_date || !d.latest_shadow_date || d.latest_daily_date <= d.latest_shadow_date) return ''
    return `数据提示：V4日线已到 ${String(d.latest_daily_date)}，但G2有效影子台账最新为 ${String(d.latest_shadow_date)}；若页面仍显示旧信号日，说明当前规则尚未产生新的有效G2信号。`
  }, [gen2Shadow])

  const gen2ShadowUpdateText = useMemo(() => {
    const task = gen2ShadowUpdateTask || {}
    if (!task.status) return ''
    if (task.status === 'completed') {
      const result = task.result || {}
      return `影子交易已更新：原始候选 ${String(result.raw_candidates ?? '--')}，过滤后 ${String(result.filtered_signals ?? '--')}，当日影子 ${String(result.shadow_rows_for_date ?? '--')}。`
    }
    if (task.status === 'failed') return `影子交易更新失败：${String(task.error || '未知错误')}`
    return `影子交易更新中：${String(task.status)} ${String(task.progress || 0)}%`
  }, [gen2ShadowUpdateTask])

  const gen2ShadowMonitorText = useMemo(() => {
    const status = gen2ShadowMonitorStatus || {}
    const interval = Math.max(1, Math.round(Number(status.interval_seconds || 60) / 60))
    const state = status.enabled ? `自动探测中，每 ${String(interval)} 分钟` : '自动探测未开启'
    const gate = status.alpha191_gate ? `；Alpha191 ${String(status.alpha191_gate)}` : ''
    const nextRun = status.next_run_time ? `；下次 ${String(status.next_run_time)}` : ''
    const lastRun = status.last_run_at ? `；最近 ${String(status.last_run_at)}` : ''
    const lastMail = status.last_email_sent_at ? `；最近邮件 ${String(status.last_email_sent_at)}` : ''
    const lastHeartbeat = status.last_heartbeat_sent_at ? `；最近心跳 ${String(status.last_heartbeat_sent_at)}` : ''
    const err = status.last_error ? `；错误 ${String(status.last_error)}` : ''
    return state + gate + nextRun + lastRun + lastMail + lastHeartbeat + err
  }, [gen2ShadowMonitorStatus])

  const gen2ShadowMonitorResultText = useMemo(() => {
    const result = gen2ShadowMonitorStatus?.last_result || {}
    if (!Object.keys(result).length) return ''
    if (result.skipped) return `最近探测跳过：${String(result.reason || '--')}`
    if (result.blocked)
      return `最近探测阻塞：${String(result.blocker_count ?? '--')} 个阻塞点${result.blocker_email_sent ? '，已发送阻塞提醒' : ''}`
    const parts = [
      `信号日 ${String(result.signal_date || '--')}`,
      `原始 ${String(result.raw_candidates ?? '--')}`,
      `过滤 ${String(result.filtered_signals ?? '--')}`,
      `影子 ${String(result.shadow_rows_for_date ?? '--')}`,
      `可提醒 ${String(result.candidate_count ?? '--')}`,
      `新增 ${String(result.new_count ?? '--')}`,
    ]
    if (result.email_sent) parts.push('已发送买点邮件')
    else if (result.heartbeat_email_sent) parts.push('已发无买点心跳回执')
    else parts.push('本轮无新邮件')
    return `最近探测：${parts.join('；')}`
  }, [gen2ShadowMonitorStatus])

  const marketGateSummary = useMemo(() => {
    const gate = marketGate || {}
    if (!gate.available) return gate.message || '上证指数数据暂不可用'
    const close = Number(gate.close)
    const ma20 = Number(gate.ma20)
    return `${gate.trade_date || '--'} 收盘 ${Number.isFinite(close) ? close.toFixed(2) : '--'} / MA20 ${Number.isFinite(ma20) ? ma20.toFixed(2) : '--'}`
  }, [marketGate])

  const autoRefreshStatusText = useMemo(() => {
    const running = autoRefreshRunning ? '刷新中' : '待命'
    const last = lastAutoRefreshAt ? `；最近刷新 ${lastAutoRefreshAt}` : ''
    return `交易时段分层自动刷新：桥接/持仓30秒，大盘门槛60秒，G2影子/工作流180秒；当前${running}${last}`
  }, [autoRefreshRunning, lastAutoRefreshAt])

  const currentHoldingRows = useMemo(
    () => [...holdingsRef.current].sort((a, b) => parseTimeMs(b?.buy_time) - parseTimeMs(a?.buy_time)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [manualHoldings],
  )

  const realizedPnlTotal = useMemo(
    () =>
      manualTradeLogs.reduce((sum, item) => {
        const value = Number(item?.realized_pnl)
        return sum + (Number.isFinite(value) ? value : 0)
      }, 0),
    [manualTradeLogs],
  )

  // 资金快照
  const capitalSnapshot = useMemo(() => {
    let marketValue = 0
    let costValue = 0
    let dayPnl = 0
    for (const row of manualHoldings) {
      const shares = Number(row?.shares)
      if (!(Number.isFinite(shares) && shares > 0)) continue
      const cost = Number(row?.cost_price)
      if (Number.isFinite(cost) && cost > 0) costValue += cost * shares
      const price = Number(row?.current_price)
      if (Number.isFinite(price) && price > 0) marketValue += price * shares
      else if (Number.isFinite(cost) && cost > 0) marketValue += cost * shares
      const itemDayPnl = dayPnlAmount(row)
      if (itemDayPnl !== null && Number.isFinite(itemDayPnl)) dayPnl += itemDayPnl
    }
    const baseCapital = FIXED_BASE_CAPITAL
    const tradeLogRealizedPnl = Number(realizedPnlTotal) || 0
    const syncedAvailableCash = Number(capitalForm.synced_available_cash)
    const syncedTotalCapital = Number(capitalForm.synced_total_capital)
    const pnlAmount = marketValue - costValue
    const pnlRatio = costValue > 0 ? (pnlAmount / costValue) * 100 : 0
    const hasSyncedTotal = Number.isFinite(syncedTotalCapital) && syncedTotalCapital > 0
    const hasSyncedCash = Number.isFinite(syncedAvailableCash) && syncedAvailableCash >= 0
    const computedTotalCapital = hasSyncedCash
      ? marketValue + syncedAvailableCash
      : baseCapital + tradeLogRealizedPnl + pnlAmount
    const totalCapital = hasSyncedCash ? computedTotalCapital : hasSyncedTotal ? syncedTotalCapital : computedTotalCapital
    const availableCash = hasSyncedCash ? syncedAvailableCash : totalCapital - marketValue
    const realizedPnl =
      hasSyncedTotal || hasSyncedCash ? totalCapital - baseCapital + pnlAmount : tradeLogRealizedPnl
    const positionRatio = totalCapital > 0 ? (marketValue / totalCapital) * 100 : 0
    return {
      total_capital: Number.isFinite(totalCapital) ? totalCapital : 0,
      available_cash: Number.isFinite(availableCash) ? availableCash : 0,
      market_value: marketValue,
      cost_value: costValue,
      realized_pnl: Number.isFinite(realizedPnl) ? realizedPnl : 0,
      pnl_amount: pnlAmount,
      pnl_ratio: pnlRatio,
      day_pnl: dayPnl,
      position_ratio: positionRatio,
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manualHoldings, capitalForm, realizedPnlTotal])

  const capitalSnapshotRef = useRef(capitalSnapshot)
  capitalSnapshotRef.current = capitalSnapshot

  // 持仓计算辅助
  function holdingMarketValue(row: any): number | null {
    const shares = Number(row?.shares)
    const price = Number(row?.current_price)
    if (!(Number.isFinite(shares) && shares > 0 && Number.isFinite(price) && price > 0)) return null
    return shares * price
  }

  function floatingPnlAmount(row: any): number | null {
    const shares = Number(row?.shares)
    const cost = Number(row?.cost_price)
    const price = Number(row?.current_price)
    if (
      !(Number.isFinite(shares) && shares > 0 && Number.isFinite(cost) && cost > 0 && Number.isFinite(price) && price > 0)
    )
      return null
    return (price - cost) * shares
  }

  function stopLossPrice(row: any): number | null {
    const cost = Number(row?.cost_price)
    if (!(Number.isFinite(cost) && cost > 0)) return null
    return cost * 0.95
  }

  function dayPnlAmount(row: any): number | null {
    const explicit = Number(row?.day_pnl)
    if (Number.isFinite(explicit)) return explicit
    const shares = Number(row?.shares)
    const price = Number(row?.current_price)
    const prevClose = Number(row?.prev_close)
    if (
      !(Number.isFinite(shares) && shares > 0 && Number.isFinite(price) && price > 0 && Number.isFinite(prevClose) && prevClose > 0)
    )
      return null
    return (price - prevClose) * shares
  }

  function dayChangePct(row: any): number | null {
    const explicit = Number(row?.day_change_pct ?? row?.change_pct ?? row?.pct_chg)
    if (Number.isFinite(explicit)) return explicit
    const price = Number(row?.current_price)
    const prevClose = Number(row?.prev_close)
    if (!(Number.isFinite(price) && price > 0 && Number.isFinite(prevClose) && prevClose > 0)) return null
    return ((price - prevClose) / prevClose) * 100
  }

  function lossPctOfTotalCapital(row: any): number | null {
    const loss = floatingPnlAmount(row)
    const total = Number(capitalSnapshotRef.current.total_capital)
    if (!(loss !== null && Number.isFinite(loss) && Number.isFinite(total) && total > 0)) return null
    return (loss / total) * 100
  }

  function isSingleTradeLossCapBreached(row: any): boolean {
    const pctVal = lossPctOfTotalCapital(row)
    if (pctVal === null || !Number.isFinite(pctVal)) return false
    return pctVal <= -Math.abs(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL)
  }

  function trailingDrawdownPct(row: any): number | null {
    const pnl = Number(row?.pnl_ratio)
    const peak = Number(row?.peak_pnl_ratio)
    if (!Number.isFinite(pnl) || !Number.isFinite(peak)) return null
    return peak - pnl
  }

  function isTrailingTakeProfitTriggered(row: any): boolean {
    const peak = Number(row?.peak_pnl_ratio)
    const dd = trailingDrawdownPct(row)
    if (!Number.isFinite(peak) || dd === null || !Number.isFinite(dd)) return false
    return peak >= TRAILING_TP_ACTIVATE_PNL && dd >= TRAILING_TP_DRAWDOWN
  }

  const sellPriorityRows = useMemo(() => {
    return currentHoldingRows
      .map((row: any) => {
        const flags: string[] = []
        let priority = 9
        const pnl = Number(row?.pnl_ratio)
        if (Number.isFinite(pnl) && pnl <= -6) {
          flags.push('硬止损达到 -6%')
          priority = Math.min(priority, 1)
        } else if (Number.isFinite(pnl) && pnl <= -4) {
          flags.push('亏损达到 -4%，禁止加仓并优先观察')
          priority = Math.min(priority, 2)
        }
        if (isSingleTradeLossCapBreached(row)) {
          flags.push(`组合拖累超过 ${String(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL)}%`)
          priority = Math.min(priority, 1)
        }
        if (isTrailingTakeProfitTriggered(row)) {
          flags.push('触发移动止盈回撤')
          priority = Math.min(priority, 2)
        }
        if (row?.score_pool_status === 'out_of_pool' || row?.in_score_pool === false) {
          flags.push('离场提示')
          priority = Math.min(priority, 3)
        }
        if (row?.signal?.rsi15_signal?.detected || row?.signal?.rsi30_signal?.detected) {
          flags.push('15m/30m 移动止盈触发')
          priority = Math.min(priority, 2)
        }
        const boxAction = String(row?.signal?.rsi_box_t?.action || '')
        if (boxAction === 'sell_half' || boxAction === 'sell_part') {
          flags.push(row?.signal?.rsi_box_t?.recommendation || '箱体内 RSI 做T减仓触发')
          priority = Math.min(priority, boxAction === 'sell_half' ? 2 : 3)
        } else if (boxAction === 'risk_control_no_t') {
          flags.push(row?.signal?.rsi_box_t?.recommendation || '跌破箱体，停止做T并风控')
          priority = Math.min(priority, 2)
        } else if (boxAction === 'hold_trend_no_t') {
          flags.push(row?.signal?.rsi_box_t?.recommendation || '突破箱体，停止做T')
          priority = Math.min(priority, 5)
        }
        if (row?.signal?.suggestion) {
          flags.push(row.signal.suggestion)
          priority = Math.min(priority, 4)
        }
        const holdingIndex = holdingsRef.current.findIndex(
          (item) => normalizeCode(item?.code) === normalizeCode(row?.code),
        )
        return {
          ...row,
          holding_index: holdingIndex >= 0 ? holdingIndex : 0,
          risk_priority: priority,
          risk_text: flags.join('；'),
        }
      })
      .filter((row: any) => row.risk_text)
      .sort((a: any, b: any) => a.risk_priority - b.risk_priority || Number(a.pnl_ratio || 0) - Number(b.pnl_ratio || 0))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentHoldingRows, capitalSnapshot])

  const disciplineText = useMemo(() => {
    const lines: string[] = []
    if (manualHoldings.length >= 3) lines.push('纪律1：持仓已达上限 3 只，禁止新增')
    const hardRisk = manualHoldings.filter((x) => Number(x.pnl_ratio) <= -6).map((x) => x.code)
    if (hardRisk.length) lines.push(`纪律4：${hardRisk.join('、')} 已触发 -6%，应优先处理`)
    const warnRisk = manualHoldings
      .filter((x) => Number(x.pnl_ratio) <= -4 && Number(x.pnl_ratio) > -6)
      .map((x) => x.code)
    if (warnRisk.length) lines.push(`纪律4：${warnRisk.join('、')} 已到 -4%，禁止加仓`)
    const trailingRisk = manualHoldings.filter((x) => isTrailingTakeProfitTriggered(x)).map((x) => x.code)
    if (trailingRisk.length) lines.push(`纪律5：${trailingRisk.join('、')} 触发移动止盈回撤，建议分批止盈`)
    const lossCapRisk = manualHoldings.filter((x) => isSingleTradeLossCapBreached(x)).map((x) => x.code)
    if (lossCapRisk.length)
      lines.push(`纪律6：${lossCapRisk.join('、')} 组合拖累超过 ${String(SINGLE_TRADE_LOSS_CAP_PCT_OF_TOTAL)}%`)
    const outPoolRisk = manualHoldings
      .filter((x) => x?.score_pool_status === 'out_of_pool' || x?.in_score_pool === false)
      .map((x) => x.code)
    if (outPoolRisk.length) lines.push(`纪律7：${outPoolRisk.join('、')} 已掉出评分池，按卖出规则优先处理`)
    return lines.join('；')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manualHoldings])

  const historyTradeGroups = useMemo(() => {
    const groups = new Map<string, any>()
    const currentHoldingMap = new Map(manualHoldings.map((item) => [normalizeCode(item?.code), item]))
    for (const trade of manualTradeLogs) {
      const code = normalizeCode(trade?.code)
      if (!code) continue
      if (!groups.has(code)) {
        groups.set(code, {
          code,
          name: String(trade?.name || currentHoldingMap.get(code)?.name || ''),
          trades: [],
        })
      }
      const entry = groups.get(code)
      entry.trades.push({
        ...trade,
        code,
        name: String(trade?.name || entry.name || currentHoldingMap.get(code)?.name || ''),
      })
      if (!entry.name && trade?.name) entry.name = String(trade.name)
    }
    return Array.from(groups.values())
      .map((group) => {
        const trades = [...group.trades].sort((a, b) => parseTimeMs(a?.time) - parseTimeMs(b?.time))
        const buyTrades = trades.filter((item) => String(item?.side || '') === '买入')
        const sellTrades = trades.filter((item) => String(item?.side || '') === '卖出')
        const totalBought = buyTrades.reduce((sum, item) => sum + Math.max(0, Math.trunc(Number(item?.shares || 0))), 0)
        const totalSold = sellTrades.reduce((sum, item) => sum + Math.max(0, Math.trunc(Number(item?.shares || 0))), 0)
        const currentHolding = currentHoldingMap.get(group.code)
        const hasCurrentHolding = currentHoldingMap.has(group.code)
        const currentShares = Math.max(0, Math.trunc(Number(currentHolding?.shares ?? 0)))
        const realizedPnl = trades.reduce((sum, item) => {
          const value = Number(item?.realized_pnl)
          return sum + (Number.isFinite(value) ? value : 0)
        }, 0)
        const latestBuyTrade = [...buyTrades].sort((a, b) => parseTimeMs(b?.time) - parseTimeMs(a?.time))[0]
        const firstBuyTrade = buyTrades[0]
        const latestTrade = [...trades].sort((a, b) => parseTimeMs(b?.time) - parseTimeMs(a?.time))[0]
        return {
          code: group.code,
          name: group.name || currentHolding?.name || '--',
          trades: trades.map((item) => ({ ...item, time: String(item?.time || '') })),
          latest_buy_time: String(latestBuyTrade?.time || latestTrade?.time || '--'),
          first_buy_time: String(firstBuyTrade?.time || latestTrade?.time || '--'),
          last_trade_time: String(latestTrade?.time || '--'),
          buy_count: buyTrades.length,
          sell_count: sellTrades.length,
          net_shares: totalBought - totalSold,
          current_shares: currentShares,
          is_active: hasCurrentHolding && currentShares > 0,
          realized_pnl: realizedPnl,
          summary: `买入 ${String(buyTrades.length)} 次，卖出 ${String(sellTrades.length)} 次；累计买入 ${String(totalBought)} 股，累计卖出 ${String(totalSold)} 股`,
        }
      })
      .sort((a, b) => {
        const buyDiff = parseTimeMs(b.latest_buy_time) - parseTimeMs(a.latest_buy_time)
        if (buyDiff !== 0) return buyDiff
        return parseTimeMs(b.last_trade_time) - parseTimeMs(a.last_trade_time)
      })
  }, [manualTradeLogs, manualHoldings])

  const pagedHistoryTradeGroups = useMemo(() => {
    const pageSize = Math.max(1, Number(historyPageSize) || 20)
    const total = historyTradeGroups.length
    const maxPage = Math.max(1, Math.ceil(total / pageSize))
    const page = Math.min(Math.max(1, Number(historyPage) || 1), maxPage)
    const start = (page - 1) * pageSize
    return historyTradeGroups.slice(start, start + pageSize)
  }, [historyTradeGroups, historyPage, historyPageSize])

  useEffect(() => {
    const pageSize = Math.max(1, Number(historyPageSize) || 20)
    const maxPage = Math.max(1, Math.ceil(historyTradeGroups.length / pageSize))
    setHistoryPage((p) => Math.min(Math.max(1, p), maxPage))
  }, [historyTradeGroups.length, historyPageSize])

  const focusRows = useMemo(() => {
    if (!focusCode) return []
    const pools: any[] = [
      ...((selection as any)?.eligible_rows || []),
      ...((selection as any)?.v4_reference?.top30 || []),
      ...((selection as any)?.top30 || []),
    ]
    const hit = pools.find((item) => normalizeCode(item?.code) === normalizeCode(focusCode))
    if (hit) return [hit]
    return [
      {
        code: focusCode,
        name: focusName || '--',
        rank: '--',
        score_total: '--',
        reason_text: '当前快照未命中该股，可直接加入持仓。',
      },
    ]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusCode, focusName, payload])

  const v4SourceMap = useMemo(() => {
    const map = new Map<string, any>()
    const pools: any[] = [
      ...((selection as any)?.eligible_rows || []),
      ...((selection as any)?.v4_reference?.top30 || []),
      ...((selection as any)?.top30 || []),
    ]
    for (const item of pools) {
      const code = normalizeCode(item?.code)
      if (!code) continue
      if (!map.has(code)) map.set(code, item)
    }
    return map
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  const v4SourceFor = (row: any) => {
    const directRank = Number(row?.v4_rank)
    const directScore = Number(row?.v4_score)
    const directInPool =
      row?.score_pool_status === 'in_pool'
        ? true
        : row?.score_pool_status === 'out_of_pool'
          ? false
          : typeof row?.in_score_pool === 'boolean'
            ? row.in_score_pool
            : null
    if ((Number.isFinite(directRank) && directRank > 0) || Number.isFinite(directScore)) {
      return {
        rank_text: Number.isFinite(directRank) && directRank > 0 ? String(directRank) : '--',
        score_text: Number.isFinite(directScore) ? directScore.toFixed(4) : '--',
        in_pool: directInPool !== null ? directInPool : Number.isFinite(directRank) && directRank > 0,
      }
    }
    const code = normalizeCode(row?.code)
    const source = v4SourceMap.get(code)
    if (!source) return { rank_text: '--', score_text: '--', in_pool: directInPool === true }
    const rankVal = source?.score_rank ?? source?.rank ?? source?.rank_change
    const scoreVal = source?.score_total ?? source?.score
    const rankText = rankVal === null || rankVal === undefined || rankVal === '' ? '--' : String(rankVal)
    const scoreNum = Number(scoreVal)
    const scoreText = Number.isFinite(scoreNum) ? scoreNum.toFixed(4) : '--'
    const inPoolBySource = rankVal !== null && rankVal !== undefined && rankVal !== ''
    return { rank_text: rankText, score_text: scoreText, in_pool: directInPool !== null ? directInPool : inPoolBySource }
  }

  const disciplineTagText = (row: any) => {
    if (isSingleTradeLossCapBreached(row)) return '超阈值'
    if (isTrailingTakeProfitTriggered(row)) return '移动止盈'
    const pnl = Number(row?.pnl_ratio)
    if (Number.isFinite(pnl) && pnl <= -6) return '重度亏损'
    if (Number.isFinite(pnl) && pnl <= -4) return '中度亏损'
    return '轻度波动'
  }

  const disciplineTagColor = (row: any): BadgeColor => {
    const text = disciplineTagText(row)
    if (text === '重度亏损') return 'red'
    if (text === '超阈值') return 'amber'
    if (text === '轻度波动') return 'zinc'
    if (text === '中度亏损') return 'amber'
    return 'emerald'
  }

  const rsiBoxTSignal = (row: any) => row?.signal?.rsi_box_t || null

  const rsiBoxTTagText = (row: any) => {
    const signal = rsiBoxTSignal(row)
    const action = String(signal?.action || '')
    if (!signal) return '未检测'
    if (action === 'sell_half') return '背离减半'
    if (action === 'sell_part') return '跌80减仓'
    if (action === 'buyback_t') return '回20买回'
    if (action === 'wait_buyback') return '超卖等待'
    if (action === 'hold_trend_no_t') return '突破停T'
    if (action === 'risk_control_no_t') return '跌破停T'
    if (signal?.enabled) return '箱内等待'
    return '不做T'
  }

  const rsiBoxTTagColor = (row: any): BadgeColor => {
    const signal = rsiBoxTSignal(row)
    const action = String(signal?.action || '')
    if (action === 'sell_half' || action === 'sell_part') return 'amber'
    if (action === 'buyback_t') return 'emerald'
    if (action === 'hold_trend_no_t') return 'blue'
    if (action === 'risk_control_no_t') return 'red'
    if (action === 'wait_buyback') return 'zinc'
    if (signal?.enabled) return 'emerald'
    return 'zinc'
  }

  const gen2ShadowTagColor = (row: any): BadgeColor => {
    if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5')
      return 'red'
    if (row?.shadow_status === 'executed') return 'emerald'
    if (row?.shadow_status === 'observable') return 'amber'
    return 'zinc'
  }

  const gen2ShadowStatusText = (row: any) => {
    if (row?.status_label) return row.status_label
    if (row?.shadow_status === 'suspended_by_two_stop_cd3' || row?.shadow_status === 'suspended_by_stop_cd5')
      return '风控暂停'
    if (row?.shadow_status === 'executed') return '已执行'
    if (row?.shadow_status === 'observable') return '观察中'
    return '未知'
  }

  const gen2VerificationText = (row: any) => row?.verification_label || '未标记'

  const gen2VerificationNoteText = (row: any) => {
    const parts: string[] = []
    const position = Number(row?.verification_position_pct)
    const fill = Number(row?.verification_fill_price)
    if (Number.isFinite(position) && position > 0) parts.push(`${position.toFixed(0)}%仓位`)
    if (Number.isFinite(fill) && fill > 0) parts.push(`成交 ${fill.toFixed(3)}`)
    if (row?.verification_note) parts.push(row.verification_note)
    return parts.join(' / ') || '--'
  }

  // ---------- 持仓存取 ----------

  const loadManualHoldings = () => {
    try {
      const raw = localStorage.getItem(MANUAL_HOLDINGS_KEY)
      const parsed = raw ? JSON.parse(raw) : []
      setHoldings(Array.isArray(parsed) ? parsed : [])
    } catch {
      setHoldings([])
    }
  }

  const loadManualTradeLogs = () => {
    try {
      const raw = localStorage.getItem(MANUAL_TRADES_KEY)
      const parsed = raw ? JSON.parse(raw) : []
      setTradeLogs(Array.isArray(parsed) ? parsed : [])
    } catch {
      setTradeLogs([])
    }
  }

  const saveManualTradeLogs = () => {
    localStorage.setItem(MANUAL_TRADES_KEY, JSON.stringify(tradeLogsRef.current))
  }

  const clearTradeLogs = () => {
    setTradeLogs([])
    localStorage.setItem(MANUAL_TRADES_KEY, '[]')
    toast.success('交易记录已清空')
  }

  const syncManualHoldingsStateToServer = async () => {
    try {
      await saveV4ManualHoldingState({ holdings: holdingsRef.current })
    } catch {
      // 静默
    }
  }

  const saveManualHoldings = () => {
    localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(holdingsRef.current))
    syncManualHoldingsStateToServer()
  }

  const applyTrailingTakeProfitState = () => {
    let changed = false
    const next = holdingsRef.current.map((item) => {
      const itemNext = { ...item }
      const pnl = Number(itemNext?.pnl_ratio)
      const peak = Number(itemNext?.peak_pnl_ratio)
      if (Number.isFinite(pnl)) {
        if (!Number.isFinite(peak) || pnl > peak) {
          itemNext.peak_pnl_ratio = pnl
          changed = true
        }
      }
      return itemNext
    })
    setHoldings(next)
    if (changed) {
      localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(next))
    }
  }

  const loadCapitalProfile = () => {
    try {
      const raw = localStorage.getItem(V4_CAPITAL_PROFILE_KEY)
      const parsed = raw ? JSON.parse(raw) : {}
      setCapital({
        base_capital: FIXED_BASE_CAPITAL,
        realized_pnl: Number(parsed?.realized_pnl) || 0,
        synced_total_capital: parsed?.synced_total_capital ?? null,
        synced_available_cash: parsed?.synced_available_cash ?? null,
        synced_market_value: parsed?.synced_market_value ?? null,
        synced_cost_value: parsed?.synced_cost_value ?? null,
        synced_at: String(parsed?.synced_at || ''),
      })
    } catch {
      setCapital({
        base_capital: FIXED_BASE_CAPITAL,
        realized_pnl: 0,
        synced_total_capital: null,
        synced_available_cash: null,
        synced_market_value: null,
        synced_cost_value: null,
        synced_at: '',
      })
    }
  }

  const saveCapitalProfile = (next?: any) => {
    const value = next || capitalRef.current
    localStorage.setItem(V4_CAPITAL_PROFILE_KEY, JSON.stringify(value))
  }

  const currentPriceMap = useMemo(() => {
    const m = new Map<string, any>()
    for (const row of positions?.rows || []) {
      m.set(normalizeCode(row?.code), row)
    }
    return m
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  const mergeMarketStats = () => {
    setHoldings(
      holdingsRef.current.map((item) => {
        const hit = currentPriceMap.get(normalizeCode(item.code))
        if (!hit) return item
        const pnl = Number(hit?.pnl_ratio)
        return {
          ...item,
          pnl_ratio: Number.isFinite(pnl) ? pnl : item.pnl_ratio,
          current_price: hit?.current_price ?? item.current_price,
        }
      }),
    )
  }

  // ---------- 刷新动作 ----------

  const fetchMarketGate = async (tradeDate?: string) => {
    setMarketGateLoading(true)
    try {
      const params: any = {}
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.trade_date = tradeDate
      }
      const gate = (await getV4MarketGate(params)) as any
      marketGateRef.current = gate
      setMarketGate(gate)
    } catch {
      marketGateRef.current = { available: false, can_open: false, message: '行情服务异常，暂不建议下单' }
      setMarketGate(marketGateRef.current)
    } finally {
      setMarketGateLoading(false)
    }
  }

  const fetchGen2RiskCoolShadow = async (tradeDate?: string) => {
    setGen2ShadowLoading(true)
    try {
      const params: any = { limit: 30 }
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.signal_date = tradeDate
      }
      const data = (await getGen2RiskCoolShadow(params)) as any
      gen2ShadowRef.current = data
      setGen2Shadow(data)
    } catch {
      const previousRows: any[] = Array.isArray(gen2ShadowRef.current?.rows) ? gen2ShadowRef.current.rows : []
      const next = {
        ...(gen2ShadowRef.current || {}),
        available: previousRows.length > 0,
        rows: previousRows,
        message:
          previousRows.length > 0 ? 'G2主图买点池刷新失败，先用旧缓存展示' : 'G2主图买点池读取失败，请稍后再试',
      }
      gen2ShadowRef.current = next
      setGen2Shadow(next)
    } finally {
      setGen2ShadowLoading(false)
    }
  }

  const fetchWorkflowStatus = async (tradeDate?: string) => {
    setWorkflowLoading(true)
    try {
      const params: any = {}
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.signal_date = tradeDate
      }
      setWorkflowStatus((await getGen2WorkflowStatus(params)) as any)
    } catch (err: any) {
      setWorkflowStatus({
        available: false,
        ok: false,
        message: err?.message || '策略工作流抓取失败，请稍后重试',
        pipeline_stages: [],
        blockers: [],
      })
    } finally {
      setWorkflowLoading(false)
    }
  }

  const fetchDailyTradeTicket = async (tradeDate?: string) => {
    setDailyTradeTicketLoading(true)
    try {
      const params: any = { limit: 30, formal_limit: 3, persist: true }
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.signal_date = tradeDate
      }
      const ticket = (await getGen2DailyTradeTicket(params)) as any
      dailyTicketRef.current = ticket
      setDailyTradeTicket(ticket)
    } catch (err: any) {
      const ticket = {
        available: false,
        can_open: false,
        formal_candidates: [],
        forbidden_actions: [],
        message: err?.message || 'G2 V4 今日交易单生成失败',
      }
      dailyTicketRef.current = ticket
      setDailyTradeTicket(ticket)
    } finally {
      setDailyTradeTicketLoading(false)
    }
  }

  const applyDailyExecutionRecord = (record: any) => {
    setDailyExecutionForm({
      execution_status: record?.execution_status || 'pending',
      executed: !!record?.executed,
      discipline_ok: record?.discipline_ok !== false,
      violation_tags: Array.isArray(record?.violation_tags) ? record.violation_tags : [],
      execution_note: record?.execution_note || '',
      t1_review: record?.t1_review || '',
      t3_review: record?.t3_review || '',
      t5_review: record?.t5_review || '',
    })
  }

  const fetchDailyExecution = async (tradeDate?: string) => {
    setDailyExecutionLoading(true)
    try {
      const params: any = {}
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.signal_date = tradeDate
      }
      const resp = (await getGen2DailyTradeExecution(params)) as any
      setDailyExecution(resp)
      applyDailyExecutionRecord(resp?.record || dailyTicketRef.current?.execution_record || null)
    } catch (err: any) {
      const next = {
        available: false,
        record: dailyTicketRef.current?.execution_record || null,
        message: err?.message || '今日执行台账读取失败',
      }
      setDailyExecution(next)
      applyDailyExecutionRecord(next.record)
    } finally {
      setDailyExecutionLoading(false)
    }
  }

  const saveDailyExecution = async (status?: string) => {
    const signalDateVal = dailyTicketRef.current?.signal_date || (payload?.selected_date || '--')
    if (!signalDateVal || signalDateVal === '--') {
      toast.info('缺少交易单信号日，无法保存执行记录')
      return
    }
    setDailyExecutionSaving(true)
    try {
      const body: any = {
        signal_date: signalDateVal,
        strategy_code: dailyTicketRef.current?.strategy_code || 'g2_v2_complete',
        ...dailyExecFormRef.current,
      }
      if (status) {
        body.execution_status = status
        setDailyExecutionForm((prev: any) => ({ ...prev, execution_status: status }))
      }
      const resp = (await saveGen2DailyTradeExecution(body)) as any
      if (!resp?.ok) {
        toast.error(resp?.error || '今日执行记录保存失败')
        return
      }
      setDailyExecution({ available: true, record: resp.record, ledger_path: resp.ledger_path })
      applyDailyExecutionRecord(resp.record)
      await fetchDailyTradeTicket(signalDateVal)
      toast.success('今日执行记录已保存')
    } catch (err: any) {
      toast.error(err?.message || '今日执行记录保存失败')
    } finally {
      setDailyExecutionSaving(false)
    }
  }

  const inferDailyExecutionFromLocalTrades = async () => {
    const signalDateVal = dailyTicketRef.current?.signal_date || payload?.selected_date
    if (!signalDateVal || signalDateVal === '--') {
      toast.info('缺少交易单信号日，无法自动归因')
      return
    }
    const tradeRows = (tradeLogsRef.current || [])
      .filter((item) => String(item?.time || '').slice(0, 10) === signalDateVal)
      .map((item) => ({ ...item, code6: normalizeCode(item?.code), side_text: String(item?.side || '') }))
    const buyRows = tradeRows.filter((item) => isBuyTradeSide(item.side_text))
    const sellRows = tradeRows.filter((item) => isSellTradeSide(item.side_text))
    const formalCodes = new Set(
      (dailyTicketRef.current?.formal_candidates || [])
        .map((item: any) => normalizeCode(item?.code || item?.code6))
        .filter(Boolean),
    )
    const illegalBuys = buyRows.filter((item) => !dailyTicketRef.current?.can_open || !formalCodes.has(item.code6))
    const matchedBuys = buyRows.filter((item) => formalCodes.has(item.code6))
    let status = 'no_trade'
    let disciplineOk = true
    if (illegalBuys.length) {
      status = 'violated'
      disciplineOk = false
    } else if (matchedBuys.length && matchedBuys.length === buyRows.length) {
      status = buyRows.length >= Math.max(1, formalCodes.size || 1) ? 'followed' : 'partial'
    } else if (buyRows.length || sellRows.length) {
      status = 'partial'
    }
    const parts = [
      `auto attribution from local trade logs: signal_date=${signalDateVal}`,
      `trades=${tradeRows.length}`,
      `buys=${buyRows.length}`,
      `sells=${sellRows.length}`,
      `formal_candidates=${formalCodes.size}`,
      `illegal_buys=${illegalBuys.map((item) => displayCode(item.code6)).join(',') || '-'}`,
    ]
    setDailyExecutionForm((prev: any) => ({
      ...prev,
      execution_status: status,
      executed: tradeRows.length > 0,
      discipline_ok: disciplineOk,
      violation_tags: illegalBuys.length ? ['outside_daily_trade_ticket'] : [],
      execution_note: parts.join('; '),
    }))
    await saveDailyExecution(status)
  }

  const waitForGen2ShadowUpdateTask = async (taskId: string) => {
    for (let i = 0; i < 90; i += 1) {
      const task = (await getGen2RiskCoolShadowUpdateTask(taskId)) as any
      setGen2ShadowUpdateTask(task || {})
      if (task?.status === 'completed' || task?.status === 'failed' || task?.status === 'missing') {
        return task
      }
      await new Promise((resolve) => setTimeout(resolve, 2000))
    }
    return null
  }

  const updateGen2RiskCoolShadow = async (tradeDate?: string) => {
    setGen2ShadowUpdateLoading(true)
    try {
      const params: any = { pool_rank: 200, alpha191_gate: GEN2_MAIN_ALPHA191_GATE }
      if (typeof tradeDate === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(tradeDate)) {
        params.signal_date = tradeDate
      }
      const task = (await runGen2RiskCoolShadowUpdate(params)) as any
      setGen2ShadowUpdateTask(task || {})
      const finalTask = task?.task_id ? await waitForGen2ShadowUpdateTask(task.task_id) : task
      if (finalTask?.status === 'completed') {
        toast.success('G2买点数据刷新已提交')
        await fetchGen2RiskCoolShadow(tradeDate)
      } else {
        toast.error(finalTask?.error || 'G2买点刷新未完成')
      }
    } catch (err: any) {
      toast.error(err?.message || 'G2 影子交易更新失败')
    } finally {
      setGen2ShadowUpdateLoading(false)
    }
  }

  const pullGen2ShadowMonitorStatus = async () => {
    try {
      setGen2ShadowMonitorStatus((await getGen2ShadowMonitorStatus()) as any)
    } catch {
      setGen2ShadowMonitorStatus({ enabled: false, last_error: '运行状态读取失败' })
    }
  }

  const fetchGen2StrategyRefreshStatus = async () => {
    setGen2StrategyRefreshLoading(true)
    try {
      setGen2StrategyRefreshStatus((await getGen2StrategyRefreshStatus()) as any)
    } catch (err: any) {
      setGen2StrategyRefreshStatus({
        enabled: false,
        last_error: err?.message || 'G2 30m策略刷新接口未加载，请重启PyCharm后端服务',
        last_result: { ok: false, error: err?.message || 'strategy-refresh status unavailable' },
      })
    } finally {
      setGen2StrategyRefreshLoading(false)
    }
  }

  const runGen2StrategyRefreshOnce = async () => {
    setGen2StrategyRefreshRunning(true)
    try {
      const result = (await runGen2StrategyRefreshNow({ force: true })) as any
      setGen2StrategyRefreshStatus((prev: any) => ({
        ...(prev || {}),
        last_result: result,
        last_run_at: result?.finished_at || result?.started_at || prev?.last_run_at,
        last_success_at: result?.ok ? result?.finished_at || prev?.last_success_at : prev?.last_success_at,
        last_error: result?.ok === false ? result?.error || '刷新失败' : null,
      }))
      await fetchGen2StrategyRefreshStatus()
      if (result?.ok === false) {
        toast.error(result?.error || 'G2 30m策略刷新失败')
      } else if (result?.skipped) {
        toast.info(result?.reason || 'G2 30m策略刷新已跳过')
      } else {
        toast.success('G2 30m策略刷新已执行')
      }
    } catch (err: any) {
      toast.error(err?.message || 'G2 30m策略刷新接口未加载，请重启PyCharm后端服务')
      await fetchGen2StrategyRefreshStatus()
    } finally {
      setGen2StrategyRefreshRunning(false)
    }
  }

  const toggleGen2ShadowMonitor = async () => {
    setGen2ShadowMonitorLoading(true)
    try {
      const enabled = !gen2ShadowMonitorStatus?.enabled
      setGen2ShadowMonitorStatus(
        (await setGen2ShadowMonitorConfig({
          enabled,
          interval_seconds: 60,
          pool_rank: 200,
          alpha191_gate: GEN2_MAIN_ALPHA191_GATE,
          trading_hours_only: true,
          recipient_email: '',
        })) as any,
      )
      toast.success(enabled ? '已开启 G2 15 分钟买点探测' : '已停止 G2 买点探测')
    } catch (err: any) {
      toast.error(err?.message || '设置 G2 买点探测失败')
    } finally {
      setGen2ShadowMonitorLoading(false)
    }
  }

  const runGen2ShadowMonitorOnce = async () => {
    setGen2ShadowMonitorLoading(true)
    try {
      const resp = (await runGen2ShadowMonitorNow({ force_send: true })) as any
      await pullGen2ShadowMonitorStatus()
      await fetchGen2RiskCoolShadow(resp?.signal_date || payload?.selected_date || undefined)
      await fetchWorkflowStatus(resp?.signal_date || payload?.selected_date || undefined)
      if (resp?.email_sent) toast.success('G2 买点提醒邮件已发送')
      else toast.info(resp?.message || '本次没有新增 G2 买点，未发送邮件')
    } catch (err: any) {
      toast.error(err?.message || 'G2 买点探测失败')
    } finally {
      setGen2ShadowMonitorLoading(false)
    }
  }

  // ---------- 持仓行情/信号 ----------

  const refreshManualSignals = async (options: { silent?: boolean } = {}) => {
    if (!holdingsRef.current.length) return
    setSignalLoading(true)
    try {
      const resp = (await getV4ManualHoldingSignals({ holdings: holdingsRef.current })) as any
      const signalMap = new Map<string, any>((resp?.rows || []).map((r: any) => [String(r.code || ''), r]))
      setHoldings(
        holdingsRef.current.map((item) => {
          const hit = signalMap.get(String(item.code || ''))
          if (!hit) return { ...item, signal: null }
          return {
            ...item,
            signal: {
              rsi15_signal: hit.rsi15_signal || null,
              rsi30_signal: hit.rsi30_signal || null,
              rsi_box_t: hit.rsi_box_t || null,
              risk_level: hit.risk_level || '',
              suggestion: hit.suggestion || '',
            },
          }
        }),
      )
      applyTrailingTakeProfitState()
      saveManualHoldings()
    } catch (error: any) {
      if (!options.silent) toast.error(error?.message || '刷新 RSI 点位失败')
    } finally {
      setSignalLoading(false)
    }
  }

  const refreshManualPnlByDetail = async () => {
    if (!holdingsRef.current.length) return
    try {
      const resp = (await getV4ManualHoldingQuotes({ holdings: holdingsRef.current })) as any
      const quoteMap = new Map<string, any>((resp?.rows || []).map((x: any) => [normalizeCode(x?.code), x]))
      let changed = false
      const nextRows = holdingsRef.current.map((item) => {
        const code = normalizeCode(item?.code)
        const quote = quoteMap.get(code)
        if (!quote) return { ...item }
        const next = { ...item }
        const latest = Number(quote?.latest_price)
        const pnl = Number(quote?.pnl_ratio)
        if (Number.isFinite(latest) && latest > 0) next.current_price = latest
        const prevClose = Number(quote?.prev_close)
        if (Number.isFinite(prevClose) && prevClose > 0) next.prev_close = prevClose
        const dayPnl = Number(quote?.day_pnl)
        if (Number.isFinite(dayPnl)) next.day_pnl = dayPnl
        if (Number.isFinite(pnl)) next.pnl_ratio = pnl
        if (
          next.current_price !== item.current_price ||
          next.prev_close !== item.prev_close ||
          next.day_pnl !== item.day_pnl ||
          next.pnl_ratio !== item.pnl_ratio
        ) {
          changed = true
        }
        return next
      })
      for (const item of nextRows) {
        const code = normalizeCode(item?.code)
        const latest = Number(item?.current_price)
        if (code.length !== 6 || (Number.isFinite(latest) && latest > 0)) continue
        try {
          const detail = (await getStockDetail(toExchangeCode(code), { skipErrorHandler: true })) as any
          let detailPrice = extractLatestPriceFromDetail(detail)
          if (!(detailPrice !== null && Number.isFinite(detailPrice) && detailPrice > 0)) {
            try {
              const history = (await getStockHistory(
                toExchangeCode(code),
                { period: '1d', limit: 2 },
                { skipErrorHandler: true },
              )) as any
              detailPrice = extractLatestPriceFromHistory(history)
            } catch {
              // ignore
            }
          }
          if (detailPrice !== null && Number.isFinite(detailPrice) && detailPrice > 0) {
            item.current_price = detailPrice
            const cost = Number(item?.cost_price)
            if (Number.isFinite(cost) && cost > 0) {
              item.pnl_ratio = ((detailPrice / cost) - 1) * 100
            }
            changed = true
          }
        } catch {
          // ignore
        }
      }
      setHoldings(nextRows)
      applyTrailingTakeProfitState()
      if (changed) {
        localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(nextRows))
      }
    } catch {
      // 静默
    }
  }

  const repairExistingManualHoldings = async () => {
    if (!holdingsRef.current.length) return
    let changed = false
    const repaired: any[] = []
    for (const item of holdingsRef.current) {
      const code = normalizeCode(item?.code)
      const next = { ...item, code }
      if (!code) {
        repaired.push(next)
        continue
      }
      if (!String(next.name || '').trim()) {
        try {
          const detail = (await getStockDetail(toExchangeCode(code), { skipErrorHandler: true })) as any
          const fixedName = String(detail?.name || detail?.stock_name || '').trim()
          if (fixedName) {
            next.name = fixedName
            changed = true
          }
        } catch {
          // ignore
        }
      }
      if ((next.pnl_ratio === null || next.pnl_ratio === undefined || next.pnl_ratio === '') && Number(next.cost_price) > 0) {
        const marketHit = currentPriceMap.get(code)
        const latest = Number(marketHit?.current_price)
        const cost = Number(next.cost_price)
        if (Number.isFinite(latest) && latest > 0 && Number.isFinite(cost) && cost > 0) {
          next.current_price = latest
          next.pnl_ratio = ((latest / cost) - 1) * 100
          changed = true
        }
      }
      repaired.push(next)
    }
    if (changed) {
      setHoldings(repaired)
      saveManualHoldings()
      toast.success('手工持仓修复：名称与金额字段已更新')
    }
  }

  const refreshAllHoldingData = async (options: { silent?: boolean; includePool?: boolean; successMessage?: string } = {}) => {
    setAllRefreshLoading(true)
    try {
      if (!holdingsRef.current.length) {
        if (options.includePool !== false) {
          await Promise.all([fetchMarketGate(), fetchGen2RiskCoolShadow(payload?.selected_date)])
          mergeMarketStats()
        }
        if (!options.silent) {
          if (options.includePool === false) toast.info('当前没有持仓可更新')
          else toast.success('已成功刷新买点与买入观察池')
        }
        return
      }
      const resp = (await refreshV4ManualHoldingAll({
        holdings: holdingsRef.current,
        signal_date: payload?.selected_date,
      })) as any
      const map = new Map<string, any>((resp?.rows || []).map((x: any) => [normalizeCode(x?.code), x]))
      setHoldings(
        holdingsRef.current.map((item) => {
          const hit = map.get(normalizeCode(item?.code))
          if (!hit) return item
          return {
            ...item,
            current_price: hit.latest_price ?? item.current_price,
            prev_close: hit.prev_close ?? item.prev_close,
            day_pnl: hit.day_pnl ?? item.day_pnl,
            pnl_ratio: hit.pnl_ratio ?? item.pnl_ratio,
            signal: {
              rsi15_signal: hit.rsi15_signal || item?.signal?.rsi15_signal || null,
              rsi30_signal: hit.rsi30_signal || item?.signal?.rsi30_signal || null,
              rsi_box_t: hit.rsi_box_t || item?.signal?.rsi_box_t || null,
              risk_level: hit.risk_level || item?.signal?.risk_level || '',
              suggestion: hit.suggestion || item?.signal?.suggestion || '',
            },
            v4_rank: hit.v4_rank ?? item.v4_rank,
            v4_score: hit.v4_score ?? item.v4_score,
            v4_signal_date: hit.v4_signal_date ?? item.v4_signal_date,
            in_score_pool: typeof hit.in_score_pool === 'boolean' ? hit.in_score_pool : item.in_score_pool,
            score_pool_status: hit.score_pool_status || item.score_pool_status || '',
          }
        }),
      )
      applyTrailingTakeProfitState()
      mergeMarketStats()
      saveManualHoldings()
      if (options.includePool !== false) {
        await Promise.all([fetchMarketGate(), fetchGen2RiskCoolShadow(payload?.selected_date)])
        mergeMarketStats()
      }
      setLastAutoRefreshAt(nowSecondText())
      if (!options.silent) {
        toast.success(options.successMessage || '本次自动刷新：行情/规则/信号/动作/买卖/交投/买入池已更新')
      }
    } catch (error: any) {
      if (!options.silent) toast.error(error?.message || '一次自动刷新失败')
    } finally {
      setAllRefreshLoading(false)
    }
  }

  const refreshCurrentHoldings = async () => {
    setHoldingRefreshLoading(true)
    try {
      await refreshAllHoldingData({
        includePool: false,
        successMessage: '刷新手动持仓与流水与行情：买入和卖出已更新',
      })
    } finally {
      setHoldingRefreshLoading(false)
    }
  }

  // 分层自动刷新
  const refreshLiveTradingState = async (options: { silent?: boolean } = {}) => {
    setAutoRefreshRunning(true)
    try {
      const nowMs = Date.now()
      const tasks: Promise<unknown>[] = []
      if (holdingsRef.current.length && nowMs - lastHoldingAutoRefreshAt.current >= AUTO_REFRESH_HOLDINGS_MIN_MS) {
        lastHoldingAutoRefreshAt.current = nowMs
        tasks.push(refreshAllHoldingData({ silent: true, includePool: false }))
      }
      if (nowMs - lastMarketGateAutoRefreshAt.current >= AUTO_REFRESH_MARKET_GATE_MIN_MS) {
        lastMarketGateAutoRefreshAt.current = nowMs
        tasks.push(fetchMarketGate(payload?.selected_date))
      }
      if (nowMs - lastGen2ShadowAutoRefreshAt.current >= AUTO_REFRESH_GEN2_SHADOW_MIN_MS) {
        lastGen2ShadowAutoRefreshAt.current = nowMs
        tasks.push(fetchGen2RiskCoolShadow(payload?.selected_date))
      }
      if (nowMs - lastWorkflowAutoRefreshAt.current >= AUTO_REFRESH_WORKFLOW_MIN_MS) {
        lastWorkflowAutoRefreshAt.current = nowMs
        tasks.push(fetchWorkflowStatus(payload?.selected_date))
      }
      if (tasks.length) await Promise.allSettled(tasks)
      setLastAutoRefreshAt(nowSecondText())
    } catch (error: any) {
      if (!options.silent) toast.error(error?.message || '自动刷新执行失败')
    } finally {
      setAutoRefreshRunning(false)
    }
  }
  const refreshLiveTradingStateRef = useRef(refreshLiveTradingState)
  refreshLiveTradingStateRef.current = refreshLiveTradingState

  const startAutoRefreshTimer = () => {
    if (autoRefreshTimer.current) window.clearInterval(autoRefreshTimer.current)
    autoRefreshTimer.current = window.setInterval(() => {
      if (!isAshareTradingTime()) return
      refreshLiveTradingStateRef.current({ silent: true })
    }, AUTO_REFRESH_INTERVAL_MS)
  }

  // ---------- 同花顺 ----------

  const syncWatchlistHoldingByCode = async (code: string) => {
    const normalizedCode = normalizeCode(code)
    if (!normalizedCode) return
    const row = holdingsRef.current.find((item) => normalizeCode(item?.code) === normalizedCode)
    try {
      await request({
        url: `/watchlist/${normalizedCode}`,
        method: 'post',
        params: { group: 'v4_live' },
        skipErrorHandler: true,
      })
      await request({
        url: `/watchlist/${normalizedCode}`,
        method: 'put',
        params: {
          is_holding: !!row,
          position_shares: Number(row?.shares || 0) || 0,
          position_cost: Number(row?.cost_price || 0) || 0,
        },
        skipErrorHandler: true,
      })
    } catch {
      // 静默
    }
  }

  const syncCapitalAndHoldingsFromThs = async () => {
    setThsCapitalSyncLoading(true)
    try {
      const resp = (await readV4ManualHoldingThsCapitalHoldings()) as any
      if (!resp?.ok) {
        toast.error(resp?.message || '读取同花顺资金持股失败')
        return
      }
      const syncedHoldings: any[] = Array.isArray(resp?.holdings) ? resp.holdings : []
      const oldMap = new Map((holdingsRef.current || []).map((item) => [normalizeCode(item?.code), item]))
      const nextHoldings = syncedHoldings
        .map((item) => {
          const code = normalizeCode(item?.code)
          if (!code) return null
          const old = oldMap.get(code) || {}
          const shares = Math.max(0, Math.trunc(Number(item?.shares || 0)))
          const costPrice = Number(item?.cost_price)
          const currentPrice = Number(item?.current_price)
          const nextPnl = Number(item?.pnl_ratio)
          return {
            ...old,
            code: displayCode(code),
            name: String(item?.name || old?.name || ''),
            shares,
            cost_price:
              Number.isFinite(costPrice) && costPrice > 0 ? Number(costPrice.toFixed(3)) : old?.cost_price ?? null,
            current_price:
              Number.isFinite(currentPrice) && currentPrice > 0
                ? Number(currentPrice.toFixed(3))
                : old?.current_price ?? null,
            prev_close: old?.prev_close ?? null,
            day_pnl: old?.day_pnl ?? null,
            pnl_ratio: Number.isFinite(nextPnl) ? nextPnl : old?.pnl_ratio ?? null,
            market_value: item?.market_value ?? old?.market_value ?? null,
            source: old?.source || 'ths_sync',
            buy_time: old?.buy_time || '',
            buy_count_week: old?.buy_count_week || 0,
            buy_week_key: old?.buy_week_key || '',
            signal_date: old?.signal_date || '',
            signal: old?.signal || null,
            v4_rank: old?.v4_rank ?? null,
            v4_score: old?.v4_score ?? null,
            v4_signal_date: old?.v4_signal_date ?? '',
            in_score_pool: typeof old?.in_score_pool === 'boolean' ? old.in_score_pool : null,
            score_pool_status: old?.score_pool_status || '',
          }
        })
        .filter(Boolean)
      const syncedCapital = resp?.capital && typeof resp.capital === 'object' ? resp.capital : {}
      const syncedCapitalValue = (key: string, fallbackValue: any) =>
        Object.prototype.hasOwnProperty.call(syncedCapital, key) ? syncedCapital[key] : fallbackValue
      const nextCapital = {
        ...capitalRef.current,
        synced_total_capital: syncedCapitalValue('total_capital', capitalRef.current.synced_total_capital),
        synced_available_cash: syncedCapitalValue('available_cash', capitalRef.current.synced_available_cash),
        synced_market_value: syncedCapitalValue('market_value', capitalRef.current.synced_market_value),
        synced_cost_value: syncedCapitalValue('cost_value', capitalRef.current.synced_cost_value),
        synced_at: String(resp?.synced_at || nowMinuteText()),
      }
      setCapital(nextCapital)
      saveCapitalProfile(nextCapital)
      setHoldings(nextHoldings as any[])
      saveManualHoldings()
      applyTrailingTakeProfitState()
      await refreshManualSignals()
      const capitalText = nextCapital.synced_total_capital
        ? `；总资产 ${formatMoney(nextCapital.synced_total_capital)}`
        : ''
      if (resp?.fallback_cache) {
        toast.error(`实时窗口读取失败，已回退到最近一次成功同步快照：${String(nextHoldings.length)} 只${capitalText}`)
      } else {
        toast.success(`已同步同花顺资金持股：${String(nextHoldings.length)} 只${capitalText}`)
      }
    } catch (error: any) {
      toast.error(error?.message || '同步同花顺资金持股失败')
    } finally {
      setThsCapitalSyncLoading(false)
    }
  }

  const parseThsTradeText = (rawText: string) => {
    const text = String(rawText || '')
    const lines = text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
    if (!lines.length) {
      setThsImportRows([])
      toast.info('未读取到同花顺成交文本')
      return
    }
    const rows = lines.map((line) => line.split('\t'))
    const headerIndex = rows.findIndex(
      (cells) => cells.includes('成交日期') && cells.includes('证券代码') && cells.includes('操作'),
    )
    if (headerIndex < 0) {
      setThsImportRows([])
      toast.info('未识别到同花顺成交表头，请确认复制的是“历史成交”表格')
      return
    }
    const header = rows[headerIndex]
    const idx = (name: string) => header.indexOf(name)
    const dateIdx = idx('成交日期')
    const timeIdx = idx('成交时间')
    const codeIdx = idx('证券代码')
    const nameIdx = idx('证券名称')
    const actionIdx = idx('操作')
    const sharesIdx = idx('成交数量')
    const priceIdx = idx('成交均价')
    const amountIdx = idx('成交金额')
    const remarkIdx = idx('备注')
    const parsed: any[] = []
    for (const cells of rows.slice(headerIndex + 1)) {
      const dateText = cells[dateIdx]
      const action = String(cells[actionIdx] || '').trim()
      const code = normalizeCode(cells[codeIdx])
      const name = String(cells[nameIdx] || '').trim()
      const remark = String(remarkIdx >= 0 ? cells[remarkIdx] || '' : '').trim()
      const sharesRaw = parseThsNumber(cells[sharesIdx])
      const priceRaw = parseThsNumber(cells[priceIdx])
      const amountRaw = parseThsNumber(cells[amountIdx])
      const timeText = timeIdx >= 0 ? cells[timeIdx] : ''
      const time = normalizeTradeTime(dateText, timeText)
      const side = parseThsTradeSide(action)
      const shares = Math.abs(Math.trunc(Number(sharesRaw || 0)))
      const price = Number(priceRaw)
      const textBlob = [action, name, remark].join(' ')
      const keywordHit = THS_FILTER_KEYWORDS.find((word) => textBlob.includes(word))
      if (!time || code.length !== 6 || !side) continue
      if (keywordHit) continue
      if (!(shares > 0 && Number.isFinite(price) && price > 0)) continue
      if (!/^(证券买入|证券卖出)$/.test(action)) continue
      parsed.push({
        checked: true,
        time,
        side,
        code,
        name,
        shares,
        price: Number(price.toFixed(3)),
        amount: Number.isFinite(amountRaw as number) ? Number((amountRaw as number).toFixed(3)) : null,
        remark,
        filter_reason: '真实证券成交',
      })
    }
    const dedup: any[] = []
    const seen = new Set<string>()
    for (const item of parsed) {
      const sig = tradeSignature(item)
      if (seen.has(sig)) continue
      seen.add(sig)
      dedup.push(item)
    }
    dedup.sort(
      (a, b) => new Date(b.time.replace(' ', 'T')).getTime() - new Date(a.time.replace(' ', 'T')).getTime(),
    )
    setThsImportRows(dedup)
    if (!dedup.length) {
      toast.info('已读取内容，但过滤后没有可导入的真实证券成交')
      return
    }
    toast.success(`已识别 ${String(dedup.length)} 条真实证券成交，请勾选确认`)
  }

  const readThsTradesFromClipboard = async () => {
    setThsImportLoading(true)
    try {
      const text = await navigator.clipboard.readText()
      setThsImportRawText(text)
      parseThsTradeText(text)
    } catch (error: any) {
      toast.info(error?.message || '读取剪贴板失败，请手动粘贴')
    } finally {
      setThsImportLoading(false)
    }
  }

  const readThsTradesFromWindow = async () => {
    setThsImportLoading(true)
    try {
      const resp = (await readV4ManualHoldingThsCurrentTable()) as any
      const text = String(resp?.raw_text || '')
      setThsImportRawText(text)
      if (!resp?.ok) {
        toast.info(resp?.message || '读取同花顺窗口失败')
        return
      }
      if (!resp?.recognized_recent_trade) {
        const viewDesc = String(resp?.view_type_desc || '未知表格')
        toast.info(`当前读取到的是 ${viewDesc}，不是“历史成交”`)
        return
      }
      parseThsTradeText(text)
    } catch (error: any) {
      toast.info(error?.message || '读取同花顺窗口失败')
    } finally {
      setThsImportLoading(false)
    }
  }

  const readThsTradesFromDeliveryFile = async () => {
    setThsImportLoading(true)
    try {
      const resp = (await readV4ManualHoldingThsDeliveryFile()) as any
      const text = String(resp?.raw_text || '')
      setThsImportRawText(text)
      if (!resp?.ok) {
        toast.info(resp?.message || '读取交割单文件失败')
        return
      }
      parseThsTradeText(text)
      if (resp?.file_path) {
        const fileTimeText = resp?.file_mtime ? `（更新于 ${resp.file_mtime}）` : ''
        toast.success(`已读取交割单文件：${resp.file_path}${fileTimeText}`)
      }
    } catch (error: any) {
      toast.info(error?.message || '读取交割单文件失败')
    } finally {
      setThsImportLoading(false)
    }
  }

  const confirmImportThsTrades = async () => {
    const selected = thsImportRows.filter((item) => !!item.checked)
    if (!selected.length) {
      toast.info('请至少勾选一条成交记录')
      return
    }
    const existingSignatures = new Set((tradeLogsRef.current || []).map((item) => tradeSignature(item)))
    const ordered = [...selected].sort(
      (a, b) => new Date(a.time.replace(' ', 'T')).getTime() - new Date(b.time.replace(' ', 'T')).getTime(),
    )
    let imported = 0
    let duplicated = 0
    let orphanSell = 0
    const touchedCodes = new Set<string>()
    const nextLogs: any[] = []
    const nextHoldings = [...holdingsRef.current]
    for (const tx of ordered) {
      const sig = tradeSignature(tx)
      if (existingSignatures.has(sig)) {
        duplicated += 1
        continue
      }
      const code = normalizeCode(tx.code)
      const shares = Math.abs(Math.trunc(Number(tx.shares || 0)))
      const price = Number(tx.price)
      const time = String(tx.time || nowMinuteText())
      const existing = nextHoldings.find((item) => normalizeCode(item?.code) === code)
      if (tx.side === '' || tx.side === '买入') {
        if (!existing) {
          nextHoldings.unshift({
            code,
            name: String(tx.name || ''),
            shares,
            cost_price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
            buy_count_week: 1,
            buy_week_key: weekKeyFromTime(time),
            buy_time: time.slice(0, 16),
            source: 'ths_import',
            signal_date: '',
          })
        } else {
          const oldShares = Math.max(0, Math.trunc(Number(existing?.shares || 0)))
          const oldCost = Number(existing?.cost_price)
          const nextShares = oldShares + shares
          existing.shares = nextShares
          existing.buy_time = time.slice(0, 16)
          existing.buy_week_key = weekKeyFromTime(time)
          existing.buy_count_week = Number(existing.buy_count_week || 0) + 1
          if (Number.isFinite(oldCost) && oldCost > 0 && Number.isFinite(price) && price > 0 && nextShares > 0) {
            existing.cost_price = Number((((oldCost * oldShares) + (price * shares)) / nextShares).toFixed(3))
          } else if (Number.isFinite(price) && price > 0) {
            existing.cost_price = Number(price.toFixed(3))
          }
          if (!existing.name && tx.name) existing.name = String(tx.name)
        }
        nextLogs.unshift({
          time,
          side: '买入',
          code,
          name: String(tx.name || existing?.name || ''),
          shares,
          price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
          before_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) - shares : 0,
          after_shares: existing ? Math.max(0, Math.trunc(Number(existing.shares || 0))) : shares,
          realized_pnl: null,
          reason: '同花顺导入',
        })
        imported += 1
        touchedCodes.add(code)
        existingSignatures.add(sig)
        continue
      }
      if (!existing) {
        nextLogs.unshift({
          time,
          side: '卖出',
          code,
          name: String(tx.name || ''),
          shares,
          price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
          before_shares: null,
          after_shares: null,
          realized_pnl: null,
          reason: '同花顺导入（未匹配到现持仓）',
        })
        imported += 1
        orphanSell += 1
        existingSignatures.add(sig)
        continue
      }
      const beforeShares = Math.max(0, Math.trunc(Number(existing?.shares || 0)))
      const nextShares = Math.max(0, beforeShares - shares)
      const costPrice = Number(existing?.cost_price)
      let realizedPnl: number | null = null
      if (Number.isFinite(price) && Number.isFinite(costPrice)) {
        realizedPnl = (price - costPrice) * Math.min(beforeShares, shares)
      }
      if (nextShares <= 0) {
        for (let i = nextHoldings.length - 1; i >= 0; i -= 1) {
          if (normalizeCode(nextHoldings[i]?.code) === code) nextHoldings.splice(i, 1)
        }
      } else {
        existing.shares = nextShares
      }
      nextLogs.unshift({
        time,
        side: '卖出',
        code,
        name: String(tx.name || existing?.name || ''),
        shares,
        price: Number.isFinite(price) ? Number(price.toFixed(3)) : null,
        before_shares: beforeShares,
        after_shares: nextShares,
        realized_pnl: realizedPnl,
        reason: '同花顺导入',
      })
      imported += 1
      touchedCodes.add(code)
      existingSignatures.add(sig)
    }
    setHoldings(nextHoldings)
    setTradeLogs([...nextLogs, ...tradeLogsRef.current])
    // 先更新 state 再落盘
    localStorage.setItem(MANUAL_TRADES_KEY, JSON.stringify([...nextLogs, ...tradeLogsRef.current]))
    holdingsRef.current = nextHoldings
    localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(nextHoldings))
    for (const code of touchedCodes) {
      await syncWatchlistHoldingByCode(code)
    }
    setThsImportVisible(false)
    await refreshManualPnlByDetail()
    await refreshAllHoldingData({ silent: true })
    await refreshManualSignals()
    const duplicateText = duplicated ? `，跳过重复 ${String(duplicated)} 条` : ''
    const orphanSellText = orphanSell ? `，其中 ${String(orphanSell)} 条卖出仅记入流水` : ''
    toast.success(`已导入 ${String(imported)} 条成交${duplicateText}${orphanSellText}`)
  }

  // ---------- 监控 ----------

  const pullMonitorStatus = async () => {
    try {
      const status = (await getV4ManualHoldingMonitorStatus()) as any
      setMonitorEnabled(!!status?.enabled)
      setMonitorTradingHoursOnly(status?.trading_hours_only !== false)
      setMonitorQuietMinutes(Number(status?.quiet_minutes ?? 15))
      const nextRun = status?.next_run_time ? `；下次执行 ${String(status.next_run_time)}` : ''
      const lastRun = status?.last_run_at ? `；最近执行 ${String(status.last_run_at)}` : ''
      const lastMail = status?.last_email_sent_at ? `；最近邮件 ${String(status.last_email_sent_at)}` : ''
      const err = status?.last_error ? `；最近错误 ${String(status.last_error)}` : ''
      setMonitorStatusText(`${monitorEnabledText(!!status?.enabled)}${nextRun}${lastRun}${lastMail}${err}`)
    } catch {
      setMonitorStatusText('状态读取失败')
    }
  }

  function monitorEnabledText(enabled: boolean): string {
    return enabled ? '运行中' : '未开启'
  }

  const toggleMonitor = async () => {
    setMonitorLoading(true)
    try {
      await syncManualHoldingsStateToServer()
      await setV4ManualHoldingMonitorConfig({
        enabled: !monitorEnabled,
        interval_seconds: 60,
        trading_hours_only: !!monitorTradingHoursOnly,
        quiet_minutes: Number(monitorQuietMinutes || 0),
        recipient_email: '',
      })
      await pullMonitorStatus()
      toast.success(monitorEnabled ? '已开启每分钟跟踪' : '已停止每分钟跟踪')
    } catch (error: any) {
      toast.error(error?.message || '设置每分钟跟踪失败')
    } finally {
      setMonitorLoading(false)
    }
  }

  const runMonitorNow = async () => {
    setMonitorLoading(true)
    try {
      await syncManualHoldingsStateToServer()
      const resp = (await runV4ManualHoldingMonitorNow({ force_send: true })) as any
      await pullMonitorStatus()
      if (resp?.email_sent) toast.success('测试邮件已发送')
      else toast.info(resp?.message || '本次暂未触发发送')
    } catch (error: any) {
      toast.error(error?.message || '测试提醒发送失败')
    } finally {
      setMonitorLoading(false)
    }
  }

  // ---------- 新增/卖出持仓 ----------

  const quickAddHolding = async (row: any) => {
    const code = normalizeCode(row?.code)
    if (!code) return
    if (holdingsRef.current.length >= 3 && !holdingsRef.current.find((x) => normalizeCode(x.code) === code)) {
      toast.info('持仓上限 3 只，请先移除后再新增')
      return
    }
    const weekKey = weekKeyFromTime(row?.buy_time)
    const exists = holdingsRef.current.find((x) => normalizeCode(x.code) === code)
    const addShares = Math.max(0, Math.trunc(Number(row?.shares || 0)))
    const addCost = Number(row?.cost_price)
    let beforeShares = 0
    let afterShares = 0
    let tradeCost: number | null = Number.isFinite(addCost) ? addCost : null
    const nextHoldings = [...holdingsRef.current]
    if (!exists) {
      beforeShares = 0
      afterShares = addShares
      nextHoldings.unshift({
        code,
        name: row?.name || focusName || '',
        shares: addShares,
        cost_price: row?.cost_price === null || row?.cost_price === undefined ? null : Number(row?.cost_price),
        buy_count_week: 1,
        buy_week_key: weekKey,
        buy_time: row?.buy_time || '',
        source: focusSource || 'v4_top30',
        signal_date: focusSignalDate || payload?.selected_date || '',
      })
      setHoldings(nextHoldings)
      holdingsRef.current = nextHoldings
      localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(nextHoldings))
    } else {
      const costPnl = Number(exists?.pnl_ratio)
      if (Number.isFinite(costPnl) && costPnl < 0) {
        toast.info('已建仓后盈利未正向，请先查看风险')
        return
      }
      const sameWeekCount = exists.buy_week_key === weekKey ? Number(exists.buy_count_week || 1) : 0
      if (sameWeekCount >= 2) {
        toast.info('同一自然周最多允许再买 2 次')
        return
      }
      exists.buy_week_key = weekKey
      exists.buy_count_week = sameWeekCount + 1
      beforeShares = Math.max(0, Math.trunc(Number(exists?.shares || 0)))
      afterShares = beforeShares + addShares
      const oldCost = Number(exists?.cost_price)
      const hasOld = Number.isFinite(oldCost) && oldCost > 0 && beforeShares > 0
      const hasNew = Number.isFinite(addCost) && addCost > 0 && addShares > 0
      if (addShares > 0) {
        exists.shares = afterShares
        if (hasOld && hasNew) {
          exists.cost_price = Number((((oldCost * beforeShares) + (addCost * addShares)) / afterShares).toFixed(3))
          tradeCost = addCost
        } else if (!hasOld && hasNew) {
          exists.cost_price = Number(addCost.toFixed(3))
          tradeCost = addCost
        } else if (hasOld) {
          tradeCost = oldCost
        }
      }
      if (row?.name && !exists.name) exists.name = String(row.name)
      if (row?.buy_time) exists.buy_time = String(row.buy_time)
      setHoldings([...nextHoldings])
      holdingsRef.current = nextHoldings
      localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(nextHoldings))
    }
    const nextLogs = [
      {
        time: String(row?.buy_time || nowMinuteText()),
        side: '买入',
        code,
        name: String(row?.name || exists?.name || focusName || ''),
        shares: addShares,
        price: Number.isFinite(tradeCost as number) ? Number((tradeCost as number).toFixed(3)) : null,
        before_shares: beforeShares,
        after_shares: afterShares,
        realized_pnl: null,
        reason: '新增持仓',
      },
      ...tradeLogsRef.current,
    ]
    setTradeLogs(nextLogs)
    localStorage.setItem(MANUAL_TRADES_KEY, JSON.stringify(nextLogs))
    await syncWatchlistHoldingByCode(code)
    toast.success(`${code} 已加入实盘持仓池`)
    await refreshManualSignals()
    await refreshManualPnlByDetail()
  }

  const resolveStockByCode = async (code: string) => {
    const normalized = normalizeCode(code)
    if (normalized.length !== 6) {
      setCodeLookupText('')
      setLiveQuotePrice(null)
      return
    }
    const localPools: any[] = [
      ...((selection as any)?.eligible_rows || []),
      ...((selection as any)?.v4_reference?.top30 || []),
      ...((selection as any)?.top30 || []),
      ...(positions?.rows || []),
      ...(tradeLogsRef.current || []),
      ...(holdingsRef.current || []),
    ]
    const localHit = localPools.find(
      (item) => normalizeCode(item?.code) === normalized && String(item?.name || '').trim(),
    )
    if (localHit?.name) {
      setAddForm((prev: any) => ({ ...prev, name: String(localHit.name) }))
      setCodeLookupText(`已匹配：${normalized} ${String(localHit.name)}`)
    } else {
      setCodeLookupText('正在匹配代码...')
    }
    try {
      const detail = (await getStockDetail(toExchangeCode(normalized), { skipErrorHandler: true })) as any
      const name = String(detail?.name || detail?.stock_name || '').trim()
      if (name) {
        setAddForm((prev: any) => ({ ...prev, name }))
        setCodeLookupText(`已匹配：${normalized} ${name}`)
        let detailPrice = Number(detail?.price ?? detail?.close ?? detail?.current_price ?? detail?.last_price)
        if (!(Number.isFinite(detailPrice) && detailPrice > 0)) {
          try {
            const history = (await getStockHistory(
              toExchangeCode(normalized),
              { period: '1d', limit: 2 },
              { skipErrorHandler: true },
            )) as any
            detailPrice = Number(extractLatestPriceFromHistory(history))
          } catch {
            // ignore
          }
        }
        if (Number.isFinite(detailPrice) && detailPrice > 0) setLiveQuotePrice(detailPrice)
      } else if (!localHit?.name) {
        setCodeLookupText('代码未匹配到该股票，请手动补充名称')
      }
    } catch {
      if (!localHit?.name) setCodeLookupText('代码未匹配到该股票，请手动补充名称')
    }
    await loadEntryBacktest(normalized)
  }

  const loadEntryBacktest = async (code: string) => {
    const normalized = normalizeCode(code)
    if (normalized.length !== 6) {
      setEntryBacktest(null)
      entryBacktestRef.current = null
      return
    }
    setEntryBacktestLoading(true)
    try {
      const resp = (await getV4ManualHoldingEntryBacktest({ code: normalized })) as any
      setEntryBacktest(resp)
      entryBacktestRef.current = resp
    } catch {
      const fail = { ok: false }
      setEntryBacktest(fail)
      entryBacktestRef.current = fail
    } finally {
      setEntryBacktestLoading(false)
    }
  }

  const entryBacktestText = useMemo(() => {
    const data = entryBacktest
    if (!data || !data.ok) return ''
    const count = Number(data.sample_count || 0)
    if (!count) return '该股暂无历史入场样本'
    const d3 = data?.horizons?.d3 || {}
    const d5 = data?.horizons?.d5 || {}
    const d10 = data?.horizons?.d10 || {}
    const line = (label: string, item: any) => {
      const win = item?.win_rate
      const avg = item?.avg_return
      const n = Number(item?.sample_count || 0)
      const winText = Number.isFinite(Number(win)) ? Number(win).toFixed(2) + '%' : '--'
      const avgText = Number.isFinite(Number(avg)) ? Number(avg).toFixed(2) + '%' : '--'
      return `${label} 胜率 ${winText}，平均收益 ${avgText}（样本 ${String(n)}）`
    }
    return `样本总数 ${String(count)}；${line('3日', d3)}；${line('5日', d5)}；${line('10日', d10)}`
  }, [entryBacktest])

  const openAddHoldingDialog = (row: any, mode = 'new') => {
    const code = normalizeCode(row?.code)
    setAddForm({
      code,
      name: row?.name || focusName || '',
      shares: 0,
      cost_price: null,
      buy_time: '',
      source:
        focusSource ||
        (row?.shadow_status ? 'gen2_risk_cool_shadow' : row?.pool_rank ? 'daily_buy_pool' : 'v4_top30'),
      signal_date: focusSignalDate || payload?.selected_date || '',
    })
    setAddDialogVisible(true)
    setCodeLookupText('')
    setCodeSuggestions([])
    setLiveQuotePrice(null)
    setEntryBacktest(null)
    entryBacktestRef.current = null
    if (code.length === 6) {
      resolveStockByCode(code)
    }
  }

  const livePnlPct = useMemo(() => {
    const cost = Number(addForm.cost_price)
    const quote = Number(liveQuotePrice)
    if (!Number.isFinite(cost) || cost <= 0 || !Number.isFinite(quote) || quote <= 0) return null
    return ((quote / cost) - 1) * 100
  }, [addForm.cost_price, liveQuotePrice])

  const livePnlText = useMemo(() => {
    const quote = Number(liveQuotePrice)
    if (!Number.isFinite(quote) || quote <= 0) return '现价待获取'
    if (livePnlPct === null) return `现价 ${quote.toFixed(3)}，请填写成本价`
    return `现价 ${quote.toFixed(3)}，盈亏 ${livePnlPct >= 0 ? '+' : ''}${livePnlPct.toFixed(2)}%`
  }, [liveQuotePrice, livePnlPct])

  const evaluateNewHoldingRules = (holdingPayload: any) => {
    const code = normalizeCode(holdingPayload?.code)
    const gen2MainHit = gen2ShadowRef.current?.rows?.find?.(
      (item: any) => normalizeCode(item?.code) === code,
    )
    const existing = holdingsRef.current.find((x) => normalizeCode(x.code) === code)
    const sameWeekKey = weekKeyFromTime(holdingPayload?.buy_time)
    const sameWeekCount =
      existing && existing.buy_week_key === sameWeekKey ? Number(existing.buy_count_week || 1) : 0
    const existingPnl = Number(existing?.pnl_ratio)
    const gate = marketGateRef.current || {}
    const marketGatePass = !!gate.available && !!gate.can_open
    const backtestOk = !!entryBacktestRef.current?.ok
    const backtestCount = Number(entryBacktestRef.current?.sample_count || 0)
    const h5 = entryBacktestRef.current?.horizons?.d5 || {}
    const h10 = entryBacktestRef.current?.horizons?.d10 || {}
    const h5Win = Number(h5?.win_rate)
    const h5Avg = Number(h5?.avg_return)
    const h10Win = Number(h10?.win_rate)
    const h10Avg = Number(h10?.avg_return)
    const backtest5Pass =
      Number.isFinite(h5Win) && Number.isFinite(h5Avg) && h5Win >= STRONG_CHECK_MIN_WIN_RATE_5D && h5Avg >= STRONG_CHECK_MIN_AVG_RETURN_5D
    const backtest10Pass =
      Number.isFinite(h10Win) && Number.isFinite(h10Avg) && h10Win >= STRONG_CHECK_MIN_WIN_RATE_10D && h10Avg >= STRONG_CHECK_MIN_AVG_RETURN_10D
    const alphaScore = Number(gen2MainHit?.alpha191_volume5_score ?? gen2MainHit?.alpha191_gate_score)
    const alphaRank = Number(gen2MainHit?.alpha191_volume5_rank_in_day || gen2MainHit?.v4_rank)
    const runup = Number(gen2MainHit?.runup_from_60d_low)
    const statusOk = !!gen2MainHit && !gen2MainHit.is_suspended
    const alphaOk = !!gen2MainHit && Number.isFinite(alphaScore)
    const runupOk = !!gen2MainHit && (!Number.isFinite(runup) || runup <= 100)
    return [
      {
        label: '主策略校验：Alpha191 volume5 主候选命中',
        pass: !!gen2MainHit,
        reason: gen2MainHit
          ? `Alpha191排名 ${Number.isFinite(alphaRank) ? String(alphaRank) : '--'}；${String(gen2MainHit.reason_text || '')}`
          : '当前标的不在 Alpha191 volume5 主执行候选池',
      },
      {
        label: '主策略校验：候选未被熔断/暂停',
        pass: statusOk,
        reason: gen2MainHit ? String(gen2MainHit.status_label || gen2MainHit.shadow_status || '候选状态可用') : '候选不存在',
      },
      {
        label: '主策略校验：Alpha191 volume5 分数有效',
        pass: alphaOk,
        reason: Number.isFinite(alphaScore) ? `Alpha191 volume5=${alphaScore.toFixed(4)}` : '缺少 Alpha191 volume5 分数',
      },
      {
        label: '主策略校验：runup_from_60d_low <= 100%',
        pass: runupOk,
        reason: Number.isFinite(runup)
          ? `60日低点以来涨幅 ${runup.toFixed(2)}%`
          : '候选未提供涨幅字段，按主候选结果放行',
      },
      { label: '大盘门槛：上证指数收盘价 >= MA20', pass: marketGatePass, reason: gate.message || '上证指数开仓状态不可用，暂不允许新增持仓' },
      {
        label: '强校验：5日预期达标',
        pass: backtest5Pass,
        reason:
          backtestOk && backtestCount > 0
            ? `5日成功率 ${Number.isFinite(h5Win) ? h5Win.toFixed(2) : '--'}%；日均收益 ${Number.isFinite(h5Avg) ? h5Avg.toFixed(2) : '--'}%（阈值 ${String(STRONG_CHECK_MIN_WIN_RATE_5D)}% / ${String(STRONG_CHECK_MIN_AVG_RETURN_5D)}%）`
            : '历史样本不足或回测失败',
      },
      {
        label: '强校验：10日预期达标',
        pass: backtest10Pass,
        reason:
          backtestOk && backtestCount > 0
            ? `10日成功率 ${Number.isFinite(h10Win) ? h10Win.toFixed(2) : '--'}%；10日均收益 ${Number.isFinite(h10Avg) ? h10Avg.toFixed(2) : '--'}%（阈值 ${String(STRONG_CHECK_MIN_WIN_RATE_10D)}% / ${String(STRONG_CHECK_MIN_AVG_RETURN_10D)}%）`
            : '历史样本不足或回测失败',
      },
      {
        label: '纪律1：持仓数量上限（最多3只）',
        pass: holdingsRef.current.length < 3 || !!existing,
        reason: `当前持仓 ${String(holdingsRef.current.length)}/3`,
      },
      { label: '纪律2：同一股票当周最多买入2次', pass: sameWeekCount < 2, reason: `本周已买入 ${String(sameWeekCount)} 次` },
      {
        label: '纪律3：亏损状态不允许补仓',
        pass: !(Number.isFinite(existingPnl) && existingPnl < 0),
        reason:
          Number.isFinite(existingPnl) && existingPnl < 0
            ? `当前盈亏 ${existingPnl.toFixed(2)}%`
            : '当前无亏损补仓风险',
      },
    ]
  }

  const confirmAddHolding = async () => {
    const form = addFormRef.current
    const normalizedCode = normalizeCode(form.code)
    if (!normalizedCode) {
      toast.info('新增持仓时请先输入有效股票代码')
      return
    }
    if (!String(form.buy_time || '').trim()) {
      toast.info('请填写买入时间')
      return
    }
    const body = {
      code: normalizedCode,
      name: form.name,
      shares: Number(form.shares || 0),
      cost_price: form.cost_price === null ? null : Number(form.cost_price),
      buy_time: String(form.buy_time || '').trim(),
      source: form.source,
      signal_date: form.signal_date,
    }
    await fetchMarketGate()
    await loadEntryBacktest(normalizedCode)
    const checks = evaluateNewHoldingRules(body)
    setRuleChecklist(checks)
    setRuleCheckPassed(checks.every((item: any) => !!item.pass))
    pendingHoldingPayload.current = body
    setAddDialogVisible(false)
    setRuleDialogVisible(true)
  }

  const confirmAddHoldingAfterCheck = async () => {
    if (!ruleCheckPassed || !pendingHoldingPayload.current) return
    setRuleDialogVisible(false)
    await quickAddHolding(pendingHoldingPayload.current)
    pendingHoldingPayload.current = null
  }

  const removeManualHolding = (index: number) => {
    const next = [...holdingsRef.current]
    next.splice(index, 1)
    setHoldings(next)
    holdingsRef.current = next
    localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(next))
  }

  const openSellDialog = (row: any, index: number) => {
    const maxShares = Math.max(0, Math.trunc(Number(row?.shares || 0)))
    setSellForm({
      index,
      code: normalizeCode(row?.code),
      name: String(row?.name || ''),
      max_shares: maxShares,
      shares: maxShares > 0 ? maxShares : 1,
      price: Number(row?.current_price || row?.cost_price || 0) || null,
      time: nowMinuteText(),
      reason: '策略卖出',
    })
    setSellDialogVisible(true)
  }

  const confirmSellHolding = async () => {
    const idx = Number(sellForm.index)
    if (idx < 0 || idx >= holdingsRef.current.length) {
      toast.info('持仓索引无效')
      return
    }
    const shares = Math.max(0, Math.trunc(Number(sellForm.shares || 0)))
    const maxShares = Math.max(0, Math.trunc(Number(sellForm.max_shares || 0)))
    if (!Number.isFinite(shares) || shares <= 0 || shares > maxShares) {
      toast.info('卖出股数必须介于 1 到可卖股数之间')
      return
    }
    const nextHoldings = [...holdingsRef.current]
    const row = { ...nextHoldings[idx] }
    const remain = Math.max(0, maxShares - shares)
    if (remain <= 0) {
      nextHoldings.splice(idx, 1)
    } else {
      row.shares = remain
      nextHoldings[idx] = row
    }
    const sellPrice = Number(sellForm.price)
    const costPrice = Number(row.cost_price)
    let realizedPnl: number | null = null
    if (Number.isFinite(sellPrice) && Number.isFinite(costPrice)) {
      realizedPnl = (sellPrice - costPrice) * shares
    }
    const nextLogs = [
      {
        time: String(sellForm.time || nowMinuteText()),
        side: '卖出',
        code: normalizeCode(sellForm.code),
        name: String(sellForm.name || row.name || ''),
        shares,
        price: Number.isFinite(sellPrice) ? Number(sellPrice.toFixed(3)) : null,
        before_shares: maxShares,
        after_shares: remain,
        realized_pnl: realizedPnl,
        reason: String(sellForm.reason || ''),
      },
      ...tradeLogsRef.current,
    ]
    setHoldings(nextHoldings)
    holdingsRef.current = nextHoldings
    localStorage.setItem(MANUAL_HOLDINGS_KEY, JSON.stringify(nextHoldings))
    setTradeLogs(nextLogs)
    localStorage.setItem(MANUAL_TRADES_KEY, JSON.stringify(nextLogs))
    await syncWatchlistHoldingByCode(sellForm.code)
    setSellDialogVisible(false)
    toast.success(remain <= 0 ? '已清仓并同步' : `已卖出 ${String(shares)} 股，剩余 ${String(remain)} 股`)
  }

  // ---------- 验证 ----------

  const openGen2VerificationDialog = (row: any) => {
    setGen2VerificationForm({
      row,
      status: row?.verification_status || '',
      note: row?.verification_note || '',
      position_pct: row?.verification_position_pct ?? null,
      fill_price: row?.verification_fill_price ?? null,
    })
    setGen2VerificationDialogVisible(true)
  }

  const saveGen2Verification = async () => {
    const row = gen2VerificationForm.row
    if (!row) return
    setGen2VerificationSaving(true)
    try {
      const saved = (await saveGen2ShadowVerification({
        verification_key: row.verification_key,
        code: row.code,
        code6: row.code6,
        entry_date: row.entry_date,
        confirm_datetime: row.confirm_datetime,
        alpha191_gate_variant: row.alpha191_gate_variant,
        status: gen2VerificationForm.status || '',
        note: gen2VerificationForm.note || '',
        position_pct: gen2VerificationForm.position_pct,
        fill_price: gen2VerificationForm.fill_price,
      })) as any
      if (!saved?.ok) {
        toast.error(saved?.error || 'G2 验证记录保存失败')
        return
      }
      setGen2VerificationDialogVisible(false)
      await fetchGen2RiskCoolShadow(payload?.selected_date)
      toast.success('G2 验证记录已保存')
    } catch (err: any) {
      toast.error(err?.message || 'G2 验证记录保存失败')
    } finally {
      setGen2VerificationSaving(false)
    }
  }

  // ---------- 详情跳转 ----------

  const buildTradeMarkerKey = (row: any) => {
    const normalizedCode = normalizeCode(row?.code)
    return `${STOCK_DETAIL_TRADE_MARKS_PREFIX}${normalizedCode || 'unknown'}_${String(Date.now())}`
  }

  const saveTradeMarkersForDetail = (row: any): string => {
    const trades: any[] = Array.isArray(row?.trades) ? row.trades : []
    if (!trades.length) return ''
    const marks = trades
      .map((item) => ({
        code: normalizeCode(item?.code || row?.code),
        name: String(item?.name || row?.name || ''),
        side: String(item?.side || ''),
        time: String(item?.time || ''),
        price: Number(item?.price),
        shares: Math.max(0, Math.trunc(Number(item?.shares || 0))),
        reason: String(item?.reason || ''),
      }))
      .filter((item) => item.code && item.time)
    if (!marks.length) return ''
    const key = buildTradeMarkerKey(row)
    sessionStorage.setItem(key, JSON.stringify(marks))
    return key
  }

  const openStockDetail = (row: any) => {
    const code = toExchangeCode(row?.code)
    if (!code) {
      toast.info('股票代码无效，无法打开详情')
      return
    }
    const tradeMarksKey = saveTradeMarkersForDetail(row)
    navigate(`/stock/${code}${tradeMarksKey ? `?trade_marks_key=${tradeMarksKey}` : ''}`)
  }

  const copyPlainCode = async (value: unknown) => {
    const code = normalizeCode(value)
    if (!code) {
      toast.info('股票代码无效，无法复制')
      return
    }
    try {
      await navigator.clipboard.writeText(code)
      toast.success(`已复制代码：${code}`)
    } catch {
      toast.info('复制失败，请手动复制代码')
    }
  }

  // ---------- 代码联想 ----------

  const querySearchStock = async (keyword: string) => {
    const localPools: any[] = [
      ...((selection as any)?.eligible_rows || []),
      ...((selection as any)?.v4_reference?.top30 || []),
      ...((selection as any)?.top30 || []),
      ...(positions?.rows || []),
      ...(tradeLogsRef.current || []),
      ...(holdingsRef.current || []),
    ]
    const localMap = new Map<string, any>()
    for (const item of localPools) {
      const code = normalizeCode(item?.code)
      const name = String(item?.name || '').trim()
      if (!code) continue
      localMap.set(code, { value: code + (name ? ` ${name}` : ''), code, name })
    }
    let localRows = Array.from(localMap.values())
    if (keyword) {
      localRows = localRows.filter((x) => x.code.includes(keyword) || x.name.includes(keyword))
    }
    localRows = localRows.slice(0, 20)
    if (keyword.length < 2) {
      setCodeSuggestions(localRows)
      return
    }
    try {
      const remote = (await searchStocks(keyword)) as any
      const remoteRows: any[] = Array.isArray(remote) ? remote : Array.isArray(remote?.items) ? remote.items : []
      for (const item of remoteRows) {
        const code = normalizeCode(item?.code)
        const name = String(item?.name || item?.stock_name || '').trim()
        if (!code) continue
        if (!localMap.has(code)) {
          localMap.set(code, { value: code + (name ? ` ${name}` : ''), code, name })
        }
      }
      setCodeSuggestions(
        Array.from(localMap.values())
          .filter((x) => x.code.includes(keyword) || x.name.includes(keyword))
          .slice(0, 20),
      )
    } catch {
      setCodeSuggestions(localRows)
    }
  }

  // 代码输入防抖
  const onAddFormCodeChange = (value: string) => {
    const normalized = normalizeCode(value)
    setAddForm((prev: any) => ({ ...prev, code: normalized }))
    if (codeLookupTimer.current) window.clearTimeout(codeLookupTimer.current)
    codeLookupTimer.current = window.setTimeout(() => {
      querySearchStock(normalized)
      resolveStockByCode(normalized)
    }, 220)
  }

  const handleCodeSelect = async (item: any) => {
    const code = normalizeCode(item?.code || item?.value || '')
    if (!code) return
    setAddForm((prev: any) => ({ ...prev, code, name: item?.name ? String(item.name) : prev.name }))
    setCodeSuggestions([])
    await resolveStockByCode(code)
  }

  // ---------- 主数据 ----------

  const fetchData = async () => {
    setLoading(true)
    try {
      const resp = (await getGen2Live({ target_date: targetDate || undefined })) as any
      setAvailable(!!resp?.available)
      setMessage(resp?.message || '')
      setPayload(resp || {})
      await fetchMarketGate()
      await fetchGen2RiskCoolShadow(resp?.selected_date || targetDate || undefined)
      await fetchDailyTradeTicket(resp?.selected_date || targetDate || undefined)
      await fetchDailyExecution(dailyTicketRef.current?.signal_date || resp?.selected_date || targetDate || undefined)
      await fetchWorkflowStatus(resp?.selected_date || targetDate || undefined)
      await fetchGen2StrategyRefreshStatus()
      mergeMarketStats()
      saveManualHoldings()
      if (holdingsRef.current.length) {
        await refreshAllHoldingData({ silent: true, includePool: false })
      }
    } finally {
      setLoading(false)
    }
  }

  const refreshSnapshot = async () => {
    setRefreshing(true)
    try {
      const candidateDate = [targetDate, payload?.selected_date, latestTradeDate].find(
        (item: unknown) => typeof item === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(String(item)),
      ) as string | undefined
      await updateGen2RiskCoolShadow(candidateDate)
      await fetchData()
      await fetchGen2StrategyRefreshStatus()
      toast.success('已更新快照')
    } finally {
      setRefreshing(false)
    }
  }

  // query 变化时 add_hold=1 自动开弹窗
  useEffect(() => {
    if (String(searchParams.get('add_hold') || '') === '1' && focusCode) {
      openAddHoldingDialog({ code: focusCode, name: focusName })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  // capitalForm 同步值变化时持久化
  useEffect(() => {
    saveCapitalProfile()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    capitalForm.synced_total_capital,
    capitalForm.synced_available_cash,
    capitalForm.synced_market_value,
    capitalForm.synced_cost_value,
    capitalForm.synced_at,
  ])

  // 挂载
  useEffect(() => {
    let cancelled = false
    const boot = async () => {
      loadCapitalProfile()
      loadManualHoldings()
      loadManualTradeLogs()
      applyTrailingTakeProfitState()
      await syncManualHoldingsStateToServer()
      await repairExistingManualHoldings()
      await fetchData()
      if (cancelled) return
      mergeMarketStats()
      await refreshAllHoldingData({ silent: true, includePool: true })
      await refreshManualPnlByDetail()
      await pullMonitorStatus()
      await pullGen2ShadowMonitorStatus()
      startAutoRefreshTimer()
      if (String(searchParams.get('add_hold') || '') === '1' && focusCode) {
        openAddHoldingDialog({ code: focusCode, name: focusName })
      } else {
        await refreshManualSignals()
      }
    }
    boot()
    return () => {
      cancelled = true
      if (autoRefreshTimer.current) window.clearInterval(autoRefreshTimer.current)
      if (codeLookupTimer.current) window.clearTimeout(codeLookupTimer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---------- 渲染 ----------

  const CodeLink = ({ code, onClickDetail }: { code: unknown; onClickDetail?: (row: any) => void }) => (
    <button
      type="button"
      className="text-blue-600 hover:underline dark:text-blue-400 tabular-nums"
      onClick={() => (onClickDetail ? onClickDetail({ code }) : copyPlainCode(code))}
      title="点击复制代码"
    >
      {displayCode(code)}
    </button>
  )

  const NameLink = ({ row }: { row: any }) => (
    <button
      type="button"
      className="text-blue-600 hover:underline dark:text-blue-400"
      onClick={() => openStockDetail(row)}
    >
      {row?.name || '--'}
    </button>
  )

  const LinkBtn = ({ onClick, children, disabled }: { onClick: () => void; children: React.ReactNode; disabled?: boolean }) => (
    <button
      type="button"
      disabled={disabled}
      className="text-blue-600 hover:underline disabled:cursor-not-allowed disabled:opacity-40 dark:text-blue-400"
      onClick={onClick}
    >
      {children}
    </button>
  )

  const buyPoolStatusText = (row: any) => {
    if (row?.can_buy) return '可买'
    if (row?.candidate_status === 'primary') return '主池'
    return '观察'
  }

  const pnlSpan = (value: unknown) => (
    <ChangeValue value={value === null || value === undefined || value === '' ? null : Number(value)} />
  )

  const ticketMetrics = [
    { label: '信号日', value: dailyTradeTicket?.signal_date || '--' },
    {
      label: '市场状态',
      value: dailyTradeTicket?.market_state || '--',
      tone: dailyTradeTicket?.can_open ? 'ok' : 'warn',
    },
    { label: '允许仓位', value: dailyTradeTicket?.target_exposure || '--' },
    {
      label: '正式候选',
      value: `${dailyTradeTicketFormalRows.length} / ${dailyTradeTicket?.max_new_positions ?? '--'}`,
    },
    { label: '持仓风控', value: `${sellPriorityRows.length} 项`, tone: sellPriorityRows.length ? 'warn' : 'ok' },
    {
      label: '自动探测',
      value: gen2ShadowMonitorStatus?.enabled ? '运行中' : '未开启',
    },
  ]

  const refreshGridItems = [
    { label: '当前30m窗口', value: gen2StrategyRefreshStatus?.current_bar_slot || '--' },
    { label: '下次自动刷新', value: gen2StrategyRefreshStatus?.next_run_time || '--' },
    { label: '上次刷新', value: gen2StrategyRefreshStatus?.last_success_at || gen2StrategyRefreshStatus?.last_run_at || '--' },
    { label: '上次结果', value: gen2StrategyRefreshLastResultText },
  ]

  const capitalItems = [
    { label: '初始总资金', value: formatMoney(FIXED_BASE_CAPITAL) },
    { label: '总资金（系统维护）', value: formatMoney(capitalSnapshot.total_capital) },
    { label: '可用资金/可用冻结', value: formatMoney(capitalSnapshot.available_cash) },
    { label: '持仓市值', value: formatMoney(capitalSnapshot.market_value) },
    {
      label: '持仓占比',
      value: formatPct(capitalSnapshot.position_ratio),
      tone: capitalSnapshot.position_ratio > 80 ? 'down-warn' : undefined,
    },
    { label: '持仓成本', value: formatMoney(capitalSnapshot.cost_value) },
    { label: '已实现盈亏', value: formatMoney(capitalSnapshot.realized_pnl), pnl: capitalSnapshot.realized_pnl },
    {
      label: '当日盈亏',
      value: `${formatMoney(capitalSnapshot.pnl_amount)}（${formatPct(capitalSnapshot.pnl_ratio)}）`,
      pnl: capitalSnapshot.pnl_amount,
    },
    { label: '当日盈亏', value: formatMoney(capitalSnapshot.day_pnl), pnl: capitalSnapshot.day_pnl },
    {
      label: '大盘开仓阈值',
      value: marketGate?.can_open ? '允许新增' : '禁止新增',
      tone: marketGate?.can_open ? 'up' : 'down',
      note: marketGateSummary,
    },
  ]

  return (
    <div className="flex flex-col gap-4 p-4">
      {/* 头部 */}
      <PageHeader
        title="G2 实盘交易驾驶舱"
        subtitle="第一页只展示正式买点、开仓门槛、持仓风控和交易执行状态；影子样本仅用于复盘验证。"
        actions={
          <>
            <Input
              type="date"
              value={targetDate}
              onChange={(e) => setTargetDate(e.target.value)}
              className="w-40"
              placeholder="Select trade date"
            />
            <Button color="blue" disabled={loading} onClick={fetchData}>
              {loading ? '查询中…' : '查询'}
            </Button>
            <Button disabled={refreshing} onClick={refreshSnapshot}>
              {refreshing ? '刷新中…' : '刷新快照'}
            </Button>
          </>
        }
      />

      {loading && !available && <PageLoading text="正在加载 G2 实盘数据…" />}

      {!available && !loading && (
        <Alert color="amber">
          <div>{message || '第二代策略实盘交易数据不可用'}</div>
        </Alert>
      )}

      {available && (
        <>
          {selectionBranchFreshnessWarning && (
            <Alert color="amber">
              <div>{selectionBranchFreshnessWarning}</div>
            </Alert>
          )}
          {dateResolutionNotice && (
            <Alert color="amber">
              <div>{dateResolutionNotice}</div>
            </Alert>
          )}
          {liveFallbackNotice && (
            <Alert color="amber">
              <div>{liveFallbackNotice}</div>
            </Alert>
          )}
          {officialRebuildNotice && (
            <Alert color="amber">
              <div>{officialRebuildNotice}</div>
            </Alert>
          )}

          {/* 今日行动工作台 */}
          <Card>
            <SectionTitle
              title="今日行动工作台"
              actions={
                <>
                  <Badge color={dailyTradeTicket?.can_open ? 'emerald' : 'amber'}>
                    {dailyTradeTicket?.can_open ? '允许开仓' : '禁止新仓'}
                  </Badge>
                  <Badge color={sellPriorityRows.length ? 'red' : 'emerald'}>
                    {sellPriorityRows.length ? '先处理持仓风险' : '持仓风控正常'}
                  </Badge>
                  <Badge color="zinc">{dailyTradeTicket?.mode || 'shadow_only'}</Badge>
                  <Button plain disabled={gen2ShadowMonitorLoading} onClick={runGen2ShadowMonitorOnce}>
                    探测买点
                  </Button>
                  <Button
                    disabled={
                      allRefreshLoading || marketGateLoading || gen2ShadowLoading || holdingRefreshLoading
                    }
                    onClick={() => refreshAllHoldingData()}
                  >
                    刷新交易状态
                  </Button>
                  <Button disabled={dailyTradeTicketLoading} onClick={() => fetchDailyTradeTicket(payload?.selected_date)}>
                    刷新交易单
                  </Button>
                </>
              }
            />
            <div className="mb-3 space-y-1 text-[13px] text-zinc-500 dark:text-zinc-400">
              <div>G2 V4 交易单、开仓门槛、持仓风控、执行回填合并在这里处理。</div>
              {dailyTradeTicketOutputs && (
                <div className="break-all">审计文件：{dailyTradeTicketOutputs}</div>
              )}
            </div>

            <div className="mt-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
              {ticketMetrics.map((item) => (
                <StatCard
                  key={item.label}
                  label={item.label}
                  value={item.value}
                  tone={item.tone === 'warn' ? 'warning' : item.tone === 'ok' ? 'down' : 'default'}
                />
              ))}
            </div>

            {dailyTradeTicket?.message && (
              <Alert color={dailyTradeTicket?.can_open ? 'green' : 'amber'} className="mt-3">
                <div>{dailyTradeTicket.message}</div>
              </Alert>
            )}

            <div className="mt-3">
              <Table dense striped>
                <TableHead>
                  <TableRow>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>来源</TableHeader>
                    <TableHeader>买入区间</TableHeader>
                    <TableHeader>建议仓位</TableHeader>
                    <TableHeader>确认时间</TableHeader>
                    <TableHeader>V4证据</TableHeader>
                    <TableHeader>风控</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {dailyTradeTicketFormalRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={8}>
                        <EmptyState text="今日交易单没有正式买入候选" />
                      </TableCell>
                    </TableRow>
                  )}
                  {dailyTradeTicketFormalRows.map((row, i) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <CodeLink code={row.code} />
                      </TableCell>
                      <TableCell>{row.name || '--'}</TableCell>
                      <TableCell className="max-w-[150px] truncate" title={row.strategy_source}>
                        {row.strategy_source || '--'}
                      </TableCell>
                      <TableCell className="max-w-[220px] truncate" title={row.buy_area}>
                        {row.buy_area || '--'}
                      </TableCell>
                      <TableCell className="tabular-nums">{row.suggested_position || '--'}</TableCell>
                      <TableCell className="max-w-[150px] truncate" title={row.confirm_datetime}>
                        {row.confirm_datetime || '--'}
                      </TableCell>
                      <TableCell>
                        rank {row.evidence?.v4_rank || '--'} / {formatScore(row.evidence?.v4_score)}
                      </TableCell>
                      <TableCell className="max-w-[240px] truncate" title={(row.risk_rules || []).join('；')}>
                        {(row.risk_rules || []).join('；') || '--'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            {dailyTradeTicketForbiddenActions.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
                {dailyTradeTicketForbiddenActions.map((item) => (
                  <Badge key={item} color="red">
                    {item}
                  </Badge>
                ))}
              </div>
            )}

            {/* 执行与复盘记录 */}
            <div className="mt-4 border-t border-zinc-200 pt-3 dark:border-white/10">
              <SectionTitle
                title="执行与复盘记录"
                actions={
                  <>
                    <Badge color={dailyExecutionForm.discipline_ok ? 'emerald' : 'red'}>
                      {dailyExecutionForm.discipline_ok ? '纪律正常' : '纪律问题'}
                    </Badge>
                    <Button disabled={dailyExecutionSaving} onClick={() => saveDailyExecution('no_trade')}>
                      今日无交易
                    </Button>
                    <Button disabled={dailyExecutionSaving} onClick={inferDailyExecutionFromLocalTrades}>
                      从成交归因
                    </Button>
                    <Button plain disabled={dailyExecutionSaving} onClick={() => saveDailyExecution('followed')}>
                      已照单执行
                    </Button>
                    <Button plain disabled={dailyExecutionSaving} onClick={() => saveDailyExecution('violated')}>
                      标记违规
                    </Button>
                  </>
                }
              />
              <div className="mb-3 text-[13px] text-zinc-500 dark:text-zinc-400">{dailyExecutionSummary}</div>
            </div>
              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field>
                  <Label>执行状态</Label>
                  <Select
                    value={dailyExecutionForm.execution_status}
                    onChange={(e) =>
                      setDailyExecutionForm((prev: any) => ({ ...prev, execution_status: e.target.value }))
                    }
                  >
                    <option value="pending">待记录</option>
                    <option value="followed">照单执行</option>
                    <option value="no_trade">无交易</option>
                    <option value="partial">部分执行</option>
                    <option value="violated">纪律违规</option>
                    <option value="reviewed">已复盘</option>
                  </Select>
                </Field>
                <Field>
                  <Label>是否有实际执行</Label>
                  <CheckboxField>
                    <Checkbox
                      checked={dailyExecutionForm.executed}
                      onChange={(checked) =>
                        setDailyExecutionForm((prev: any) => ({ ...prev, executed: checked }))
                      }
                    />
                    {dailyExecutionForm.executed ? '有执行' : '无执行'}
                  </CheckboxField>
                </Field>
                <Field>
                  <Label>是否遵守交易单</Label>
                  <CheckboxField>
                    <Checkbox
                      checked={dailyExecutionForm.discipline_ok}
                      onChange={(checked) =>
                        setDailyExecutionForm((prev: any) => ({ ...prev, discipline_ok: checked }))
                      }
                    />
                    {dailyExecutionForm.discipline_ok ? '遵守' : '违规'}
                  </CheckboxField>
                </Field>
              </div>
              <Field className="mt-3">
                <Label>执行备注</Label>
                <Textarea
                  rows={2}
                  value={dailyExecutionForm.execution_note}
                  placeholder="记录是否按交易单执行、是否有临时冲动、是否错过买点"
                  onChange={(e) =>
                    setDailyExecutionForm((prev: any) => ({ ...prev, execution_note: e.target.value }))
                  }
                />
              </Field>
              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field>
                  <Label>T+1 复盘</Label>
                  <Textarea
                    rows={2}
                    value={dailyExecutionForm.t1_review}
                    placeholder="次日表现、是否符合预期"
                    onChange={(e) => setDailyExecutionForm((prev: any) => ({ ...prev, t1_review: e.target.value }))}
                  />
                </Field>
                <Field>
                  <Label>T+3 复盘</Label>
                  <Textarea
                    rows={2}
                    value={dailyExecutionForm.t3_review}
                    placeholder="三日是否走强，是否需要降级"
                    onChange={(e) => setDailyExecutionForm((prev: any) => ({ ...prev, t3_review: e.target.value }))}
                  />
                </Field>
                <Field>
                  <Label>T+5 复盘</Label>
                  <Textarea
                    rows={2}
                    value={dailyExecutionForm.t5_review}
                    placeholder="盈亏、买点质量和纪律归因"
                    onChange={(e) => setDailyExecutionForm((prev: any) => ({ ...prev, t5_review: e.target.value }))}
                  />
                </Field>
              </div>
              <div className="mt-3">
                <Button color="blue" disabled={dailyExecutionSaving} onClick={() => saveDailyExecution()}>
                  保存执行与复盘记录
                </Button>
              </div>
          </Card>

          {/* 30m 策略自动刷新 */}
          <Card>
            <SectionTitle
              title="G2 30m 策略自动刷新"
              actions={
                <>
                  <Badge color={gen2StrategyRefreshStatus?.enabled ? 'emerald' : 'zinc'}>
                    {gen2StrategyRefreshStatus?.enabled ? '半小时自动刷新已开启' : '半小时自动刷新未开启'}
                  </Badge>
                  <Button disabled={gen2StrategyRefreshLoading} onClick={fetchGen2StrategyRefreshStatus}>
                    刷新状态
                  </Button>
                  <Button plain disabled={gen2StrategyRefreshRunning} onClick={runGen2StrategyRefreshOnce}>
                    立即执行一次
                  </Button>
                </>
              }
            />
            <div className="mb-3 text-[13px] text-zinc-500 dark:text-zinc-400">{gen2StrategyRefreshSummary}</div>
            <div className="mt-3 grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
              {refreshGridItems.map((item) => (
                <StatCard key={item.label} label={item.label} value={item.value} />
              ))}
            </div>
          </Card>

          {/* 工作流监控 */}
          <Card>
            <SectionTitle
              title="策略工作流监控"
              actions={
                <>
                  <Badge color={workflowOk ? 'emerald' : 'red'}>{workflowOk ? '全链路正常' : '存在阻塞'}</Badge>
                  {notificationStageExists && (
                    <Badge color={notificationStageOk ? 'emerald' : 'amber'}>{notificationStageLabel}</Badge>
                  )}
                  <Button disabled={workflowLoading} onClick={() => fetchWorkflowStatus(payload?.selected_date)}>
                    刷新工作流
                  </Button>
                </>
              }
            />
            <div className="mb-3 text-[13px] text-zinc-500 dark:text-zinc-400">{workflowSummaryText}</div>
            <div className="mt-2 grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
              {workflowStages.map((stage: any) => (
                <Card
                  key={stage.stage}
                  className={stage.ok ? 'min-h-[72px] p-2.5' : 'min-h-[72px] bg-red-50 p-2.5 dark:bg-red-500/5'}
                >
                  <div className="flex items-center justify-between gap-2 font-bold text-zinc-900 dark:text-white">
                    <span>{workflowStageLabel(stage.stage)}</span>
                    <Badge color={stage.ok ? 'emerald' : 'red'}>
                      {stage.ok ? '通过' : `${stage.failed_count || stage.failed?.length || 0}项阻塞`}
                    </Badge>
                  </div>
                  <div className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">{workflowStageMeta(stage)}</div>
                </Card>
              ))}
            </div>

            {workflowFailedChecks.length > 0 && (
              <div className="mt-3">
                <Table dense striped>
                  <TableHead>
                    <TableRow>
                      <TableHeader>层级</TableHeader>
                      <TableHeader>检查项</TableHeader>
                      <TableHeader>阻塞说明</TableHeader>
                      <TableHeader>日期</TableHeader>
                      <TableHeader>结果</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {workflowFailedChecks.map((row: any, i: number) => (
                      <TableRow key={i}>
                        <TableCell>{workflowStageLabel(row.stage || row.type)}</TableCell>
                        <TableCell>{row.name || '--'}</TableCell>
                        <TableCell className="max-w-[360px] truncate" title={row.message}>
                          {row.message || '--'}
                        </TableCell>
                        <TableCell className="tabular-nums">{row.trade_date || '--'}</TableCell>
                        <TableCell>{workflowCheckResult(row)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            {workflowLineageRows.length > 0 && (
              <div className="mt-3">
                <Table dense striped>
                  <TableHead>
                    <TableRow>
                      <TableHeader>链路层</TableHeader>
                      <TableHeader>真源/产物</TableHeader>
                      <TableHeader>类型</TableHeader>
                      <TableHeader>存储位置</TableHeader>
                      <TableHeader>官方读取路径</TableHeader>
                      <TableHeader>读取结果</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {workflowLineageRows.map((row: any, i: number) => (
                      <TableRow key={i}>
                        <TableCell>{workflowStageLabel(row.stage)}</TableCell>
                        <TableCell>{row.label || '--'}</TableCell>
                        <TableCell>{row.source_type || '--'}</TableCell>
                        <TableCell className="max-w-[260px] truncate" title={row.storage}>
                          {row.storage || '--'}
                        </TableCell>
                        <TableCell className="max-w-[220px] truncate" title={row.official_reader}>
                          {row.official_reader || '--'}
                        </TableCell>
                        <TableCell>{workflowLineageResult(row)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </Card>

          {/* 持仓风控 */}
          <Card>
            <SectionTitle
              title="持仓风控与卖出优先级"
              actions={
                <Button disabled={holdingRefreshLoading || signalLoading} onClick={refreshCurrentHoldings}>
                  刷新持仓风控
                </Button>
              }
            />
            <div className="mb-3 text-[13px] text-zinc-500 dark:text-zinc-400">
              实盘先处理已有仓位风险，再考虑新增开仓；这里汇总硬止损、移动止盈、评分池和分钟卖点。
            </div>
            <div className="mt-3">
              <Table dense striped>
                <TableHead>
                  <TableRow>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader className="text-right">收益率</TableHeader>
                    <TableHeader>风险原因</TableHeader>
                    <TableHeader>交易建议</TableHeader>
                    <TableHeader>操作</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {sellPriorityRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={6}>
                        <EmptyState text="暂无相关持仓标记" />
                      </TableCell>
                    </TableRow>
                  )}
                  {sellPriorityRows.map((row: any, i: number) => (
                    <TableRow key={`${row.code}-${i}`}>
                      <TableCell>
                        <CodeLink code={row.code} onClickDetail={openStockDetail} />
                      </TableCell>
                      <TableCell>
                        <NameLink row={row} />
                      </TableCell>
                      <TableCell className="text-right">{pnlSpan(row.pnl_ratio)}</TableCell>
                      <TableCell className="max-w-[260px] truncate" title={row.risk_text}>
                        {row.risk_text || '--'}
                      </TableCell>
                      <TableCell className="max-w-[140px] truncate" title={row.signal?.suggestion}>
                        {row.signal?.suggestion || '--'}
                      </TableCell>
                      <TableCell>
                        <LinkBtn onClick={() => openSellDialog(row, row.holding_index)}>卖出</LinkBtn>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Card>

          {/* 资金股票总览 */}
          <Card>
            <SectionTitle title="资金股票总览" />
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
              {capitalItems.map((item, idx) => (
                <StatCard
                  key={`${item.label}-${idx}`}
                  label={item.label}
                  value={item.value}
                  tone={
                    item.pnl !== undefined
                      ? Number(item.pnl) > 0
                        ? 'up'
                        : Number(item.pnl) < 0
                          ? 'down'
                          : 'default'
                      : item.tone === 'up'
                        ? 'up'
                        : item.tone === 'down-warn'
                          ? 'warning'
                          : item.tone === 'down'
                            ? 'down'
                            : 'default'
                  }
                  hint={item.note}
                />
              ))}
            </div>

            {fallback?.active && (
              <Alert color="zinc" className="mt-3">
                <div className="font-semibold">{fallback.title}</div>
                <div className="mt-1">{fallback.message}</div>
              </Alert>
            )}
            {capitalForm.synced_at && (
              <div className="mt-2 text-[13px] text-zinc-500 dark:text-zinc-400">
                同花顺资金持股最近同步：{capitalForm.synced_at}
              </div>
            )}
            {disciplineText && (
              <Alert color="amber" className="mt-3">
                <div>{disciplineText}</div>
              </Alert>
            )}

            <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <Button disabled={thsCapitalSyncLoading} onClick={syncCapitalAndHoldingsFromThs}>
                  同步同花顺资金持股
                </Button>
                <Button color="blue" onClick={() => openAddHoldingDialog(null, 'new')}>
                  新增持仓
                </Button>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <CheckboxField>
                  <Checkbox
                    checked={monitorTradingHoursOnly}
                    onChange={(checked) => setMonitorTradingHoursOnly(checked)}
                  />
                  仅交易时段
                </CheckboxField>
                <Input
                  type="number"
                  min={0}
                  step={5}
                  value={monitorQuietMinutes}
                  onChange={(e) => setMonitorQuietMinutes(Number(e.target.value) || 0)}
                  className="w-28"
                />
                {monitorEnabled ? (
                  <Button color="green" disabled={monitorLoading} onClick={toggleMonitor}>
                    停止1分钟跟踪
                  </Button>
                ) : (
                  <Button disabled={monitorLoading} onClick={toggleMonitor}>
                    开启1分钟跟踪
                  </Button>
                )}
                <Button disabled={monitorLoading} onClick={runMonitorNow}>
                  测试通知
                </Button>
                <Button
                  disabled={
                    allRefreshLoading || marketGateLoading || gen2ShadowLoading || signalLoading
                  }
                  onClick={() => refreshAllHoldingData()}
                >
                  一键刷新全部
                </Button>
              </div>
            </div>
            <div className="mt-2 space-y-1 text-[13px] text-zinc-500 dark:text-zinc-400">
              <div>分钟跟踪状态：{monitorStatusText}</div>
              <div>盘中自动刷新：{autoRefreshStatusText}</div>
              <div>通知邮箱统一取自系统配置，不再在实盘交易页单独维护。</div>
              <div>新增持仓用于按策略计划新建跟踪仓位；已实际发生的持仓请优先通过同花顺资金持股同步。</div>
              <div>当前合并了总资金、当前实盘持仓与持仓纪律，便于在一个面板里看清资金和股票。</div>
            </div>

            {/* Alpha191 影子池 */}
            <Card className="mt-4">
              <SectionTitle
                title="Alpha191 volume5 影子跟踪/复盘池"
                actions={
                  <>
                    <Button disabled={gen2ShadowLoading} onClick={() => fetchGen2RiskCoolShadow(payload?.selected_date)}>
                      刷新候选
                    </Button>
                    <Button
                      plain
                      disabled={gen2ShadowUpdateLoading}
                      onClick={() => updateGen2RiskCoolShadow(payload?.selected_date)}
                    >
                      更新候选快照
                    </Button>
                    <Button disabled={gen2ShadowMonitorLoading} onClick={toggleGen2ShadowMonitor}>
                      {gen2ShadowMonitorStatus?.enabled ? '停止15分钟探测' : '开启15分钟探测'}
                    </Button>
                    <Button plain disabled={gen2ShadowMonitorLoading} onClick={runGen2ShadowMonitorOnce}>
                      立即探测邮件
                    </Button>
                  </>
                }
              />
              <div className="mb-3 min-w-0 space-y-1 text-[13px] text-zinc-500 dark:text-zinc-400">
                <div>{gen2ShadowSummaryDisplay}</div>
                <div className="text-amber-600 dark:text-amber-400">
                  此区是影子跟踪/复盘池，不是待买入清单；只有状态明确为“可买入”的行才允许新增持仓。
                </div>
                <div>{gen2VerificationSummaryText}</div>
                <div>
                  升级提示：
                  <Badge color={epTagType(gen2VerificationPromotionSource?.promotion_type)}>
                    {gen2VerificationPromotionLabel}
                  </Badge>{' '}
                  {gen2VerificationPromotionMessage}
                </div>
                {gen2ShadowFreshnessNote && (
                  <div className="text-amber-600 dark:text-amber-400">{gen2ShadowFreshnessNote}</div>
                )}
                {gen2ShadowUpdateText && <div>{gen2ShadowUpdateText}</div>}
                <div>{gen2ShadowMonitorText}</div>
                {gen2ShadowMonitorResultText && <div>{gen2ShadowMonitorResultText}</div>}
              </div>
              <div className="mt-3">
                <Table dense striped>
                  <TableHead>
                    <TableRow>
                      <TableHeader>状态</TableHeader>
                      <TableHeader>实盘验证</TableHeader>
                      <TableHeader>代码</TableHeader>
                      <TableHeader>名称</TableHeader>
                      <TableHeader className="text-right">影子排名</TableHeader>
                      <TableHeader>确认时间</TableHeader>
                      <TableHeader className="text-right">入场价</TableHeader>
                      <TableHeader className="text-right">V4排名</TableHeader>
                      <TableHeader>Alpha191</TableHeader>
                      <TableHeader>涨幅/压力</TableHeader>
                      <TableHeader>涨幅/量比</TableHeader>
                      <TableHeader>5/10/20日跟踪</TableHeader>
                      <TableHeader>止损</TableHeader>
                      <TableHeader>理由</TableHeader>
                      <TableHeader>验证说明</TableHeader>
                      <TableHeader>操作</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {shadowReviewRows.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={16}>
                          <EmptyState text="暂无 G2 影子观察候选" />
                        </TableCell>
                      </TableRow>
                    )}
                    {shadowReviewRows.map((row: any, i: number) => (
                      <TableRow key={`${row.code}-${i}`}>
                        <TableCell>
                          <Badge color={gen2ShadowTagColor(row)}>{gen2ShadowStatusText(row)}</Badge>
                        </TableCell>
                        <TableCell>
                          <Badge color={epTagType(row?.verification_type)}>{gen2VerificationText(row)}</Badge>
                        </TableCell>
                        <TableCell>
                          <CodeLink code={row.code} />
                        </TableCell>
                        <TableCell>
                          <NameLink row={row} />
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{row.day_signal_rank || '--'}</TableCell>
                        <TableCell className="max-w-[150px] truncate" title={row.confirm_datetime}>
                          {row.confirm_datetime || '--'}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{formatPrice(row.entry_price)}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.v4_rank || '--'}</TableCell>
                        <TableCell className="tabular-nums">
                          {formatScore(row.alpha191_volume5_score ?? row.alpha191_gate_score)} /{' '}
                          {row.alpha191_volume5_rank_in_day || row.alpha191_original_v4_rank || '--'}
                        </TableCell>
                        <TableCell className="tabular-nums">
                          {formatPct(row.runup_from_60d_low)} / {formatPct(row.overhead_pressure_amount_share)}
                        </TableCell>
                        <TableCell className="tabular-nums">
                          {formatPct(row.rt_return_pct)} / {formatRatio(row.amount_ratio)}
                        </TableCell>
                        <TableCell className="tabular-nums">
                          {formatPct(row.fwd5_pct)} / {formatPct(row.fwd10_pct)} / {formatPct(row.fwd20_pct)}
                        </TableCell>
                        <TableCell>
                          <Badge color={row.stop5_touch_30m ? 'red' : 'emerald'}>
                            {row.stop5_touch_30m ? '触发' : '未触发'}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-[240px] truncate" title={row.reason_text}>
                          {row.reason_text || '--'}
                        </TableCell>
                        <TableCell className="max-w-[180px] truncate" title={gen2VerificationNoteText(row)}>
                          {gen2VerificationNoteText(row)}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <LinkBtn onClick={() => openGen2VerificationDialog(row)}>验证</LinkBtn>
                            <LinkBtn
                              disabled={!gen2ShadowCanAddHolding(row)}
                              onClick={() => openAddHoldingDialog(row, 'new')}
                            >
                              {gen2ShadowCanAddHolding(row) ? '新增持仓' : '仅复盘'}
                            </LinkBtn>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              {gen2VerificationQueueRows.length > 0 && (
                <div className="mt-3 border-t border-zinc-200 pt-3 dark:border-white/10">
                  <SectionTitle title="Alpha191 验证待办" />
                  <div className="mb-2 text-[13px] text-zinc-500 dark:text-zinc-400">
                    最近已有跟踪结果、尚未打标的全局影子样本，优先从这里开始复盘。
                  </div>
                  <Table dense striped>
                    <TableHead>
                      <TableRow>
                        <TableHeader>信号日</TableHeader>
                        <TableHeader>代码</TableHeader>
                        <TableHeader>名称</TableHeader>
                        <TableHeader>确认时间</TableHeader>
                        <TableHeader className="text-right">Alpha分</TableHeader>
                        <TableHeader>5/10/20日</TableHeader>
                        <TableHeader>止损</TableHeader>
                        <TableHeader>操作</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {gen2VerificationQueueRows.map((row: any, i: number) => (
                        <TableRow key={`${row.code}-${i}`}>
                          <TableCell className="tabular-nums">{row.entry_date || '--'}</TableCell>
                          <TableCell>
                            <CodeLink code={row.code} />
                          </TableCell>
                          <TableCell>
                            <NameLink row={row} />
                          </TableCell>
                          <TableCell className="max-w-[150px] truncate" title={row.confirm_datetime}>
                            {row.confirm_datetime || '--'}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatScore(row.alpha191_gate_score)}
                          </TableCell>
                          <TableCell className="tabular-nums">
                            {formatPct(row.fwd5_pct)} / {formatPct(row.fwd10_pct)} / {formatPct(row.fwd20_pct)}
                          </TableCell>
                          <TableCell>
                            <Badge color={row.stop5_touch_30m ? 'red' : 'emerald'}>
                              {row.stop5_touch_30m ? '触发' : '未触发'}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <LinkBtn onClick={() => openGen2VerificationDialog(row)}>验证</LinkBtn>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </Card>

            {/* 当前实盘持仓 */}
            <SectionTitle
              title="当前实盘持仓"
              actions={
                <Button color="blue" disabled={holdingRefreshLoading || allRefreshLoading} onClick={refreshCurrentHoldings}>
                  一键更新持仓
                </Button>
              }
            />
            <div className="mb-3 text-[13px] text-zinc-500 dark:text-zinc-400">
              一键更新股价、盈亏金额、V4 排名、评分池状态和卖点信号。
            </div>
            <div className="mt-2">
              <Table dense striped>
                <TableHead>
                  <TableRow>
                    <TableHeader>代码</TableHeader>
                    <TableHeader>名称</TableHeader>
                    <TableHeader>V4评分/排名</TableHeader>
                    <TableHeader>是否在池</TableHeader>
                    <TableHeader className="text-right">数量</TableHeader>
                    <TableHeader className="text-right">成本</TableHeader>
                    <TableHeader className="text-right">当前价</TableHeader>
                    <TableHeader className="text-right">市值</TableHeader>
                    <TableHeader className="text-right">浮动盈亏</TableHeader>
                    <TableHeader className="text-right">当日涨跌</TableHeader>
                    <TableHeader className="text-right">盈亏%</TableHeader>
                    <TableHeader className="text-right">止损价</TableHeader>
                    <TableHeader>纪律状态</TableHeader>
                    <TableHeader>RSI15/30</TableHeader>
                    <TableHeader>箱体T</TableHeader>
                    <TableHeader>交易建议</TableHeader>
                    <TableHeader>入场时间</TableHeader>
                    <TableHeader>操作</TableHeader>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {currentHoldingRows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={18}>
                        <EmptyState text="暂无当前实盘持仓" />
                      </TableCell>
                    </TableRow>
                  )}
                  {currentHoldingRows.map((row: any, index: number) => {
                    const v4 = v4SourceFor(row)
                    return (
                      <TableRow key={`${row.code}-${index}`}>
                        <TableCell>
                          <CodeLink code={row.code} onClickDetail={openStockDetail} />
                        </TableCell>
                        <TableCell>
                          <NameLink row={row} />
                        </TableCell>
                        <TableCell className="tabular-nums">
                          {v4.rank_text} / {v4.score_text}
                        </TableCell>
                        <TableCell>
                          <Badge color={v4.in_pool ? 'emerald' : 'red'}>{v4.in_pool ? '在池' : '不在'}</Badge>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{row.shares ?? '--'}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.cost_price ?? '--'}</TableCell>
                        <TableCell className="text-right tabular-nums">{formatPrice(row.current_price)}</TableCell>
                        <TableCell className="text-right tabular-nums">{formatMoney(holdingMarketValue(row))}</TableCell>
                        <TableCell className="text-right">
                          <ChangeValue value={floatingPnlAmount(row)} digits={2} suffix="" />
                        </TableCell>
                        <TableCell className="text-right">
                          <ChangeValue value={dayChangePct(row)} />
                        </TableCell>
                        <TableCell className="text-right">{pnlSpan(row.pnl_ratio)}</TableCell>
                        <TableCell className="text-right font-semibold text-red-600 dark:text-red-400 tabular-nums">
                          {formatPrice(stopLossPrice(row))}
                        </TableCell>
                        <TableCell>
                          <Badge color={disciplineTagColor(row)}>{disciplineTagText(row)}</Badge>
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            <Badge color={row.signal?.rsi15_signal?.detected ? 'red' : 'emerald'}>
                              15{row.signal?.rsi15_signal?.detected ? '触' : '-'}
                            </Badge>
                            <Badge color={row.signal?.rsi30_signal?.detected ? 'red' : 'emerald'}>
                              30{row.signal?.rsi30_signal?.detected ? '触' : '-'}
                            </Badge>
                          </div>
                        </TableCell>
                        <TableCell title={row.signal?.rsi_box_t?.recommendation || ''}>
                          <Badge color={rsiBoxTTagColor(row)}>{rsiBoxTTagText(row)}</Badge>
                        </TableCell>
                        <TableCell className="max-w-[144px] truncate" title={row.signal?.suggestion}>
                          {row.signal?.suggestion || '--'}
                        </TableCell>
                        <TableCell className="tabular-nums">{row.buy_time || '--'}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <LinkBtn onClick={() => openSellDialog(row, index)}>卖出</LinkBtn>
                            <LinkBtn onClick={() => removeManualHolding(index)} disabled={false}>
                              <span className="text-red-600 dark:text-red-400">删除</span>
                            </LinkBtn>
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>

            {/* focus 标的 */}
            {focusCode && (
              <div className="mt-4">
                <Alert color="zinc">
                  <div>
                    当前标的：{focusCode}
                    {focusName ? ` ${focusName}` : ''}
                  </div>
                  <div className="mt-1 text-sm opacity-80">{focusDescription}</div>
                </Alert>
                <div className="mt-2">
                  <Table dense striped>
                    <TableHead>
                      <TableRow>
                        <TableHeader>代码</TableHeader>
                        <TableHeader>名称</TableHeader>
                        <TableHeader className="text-right">排名</TableHeader>
                        <TableHeader className="text-right">总分</TableHeader>
                        <TableHeader>说明</TableHeader>
                        <TableHeader>操作</TableHeader>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {focusRows.map((row, i) => (
                        <TableRow key={`${row.code}-${i}`}>
                          <TableCell>
                            <CodeLink code={row.code} onClickDetail={openStockDetail} />
                          </TableCell>
                          <TableCell>
                            <NameLink row={row} />
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{row.rank ?? '--'}</TableCell>
                          <TableCell className="text-right tabular-nums">{row.score_total ?? '--'}</TableCell>
                          <TableCell className="max-w-[220px] truncate" title={row.reason_text}>
                            {row.reason_text || '--'}
                          </TableCell>
                          <TableCell>
                            <LinkBtn onClick={() => openAddHoldingDialog(row)}>加入持仓</LinkBtn>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </div>
            )}
          </Card>

          {/* 历史成交与持仓轨迹 */}
          <Card>
            <SectionTitle
              title="历史成交与持仓轨迹"
              actions={
                <>
                  <Button onClick={() => setTradeLogVisible(true)}>交易记录</Button>
                  <Button onClick={() => setThsImportVisible(true)}>读取同花顺成交</Button>
                </>
              }
            />
            <div className="mb-2 text-[13px] text-zinc-500 dark:text-zinc-400">
              按股票分组展示同花顺历史成交，默认按最近买入时间倒序；展开后可查看该股完整买卖明细。
            </div>
            <Table dense striped>
              <TableHead>
                <TableRow>
                  <TableHeader>展开</TableHeader>
                  <TableHeader>代码</TableHeader>
                  <TableHeader>名称</TableHeader>
                  <TableHeader>最近买入时间</TableHeader>
                  <TableHeader>首次买入时间</TableHeader>
                  <TableHeader className="text-right">买入次数</TableHeader>
                  <TableHeader className="text-right">卖出次数</TableHeader>
                  <TableHeader className="text-right">净持仓股数</TableHeader>
                  <TableHeader>当前状态</TableHeader>
                  <TableHeader className="text-right">当前持仓</TableHeader>
                  <TableHeader className="text-right">累计已实现盈亏</TableHeader>
                  <TableHeader>最近成交</TableHeader>
                  <TableHeader>摘要</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {pagedHistoryTradeGroups.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={13}>
                      <EmptyState text="暂无历史成交" />
                    </TableCell>
                  </TableRow>
                )}
                {pagedHistoryTradeGroups.map((group) => {
                  const expanded = expandedHistoryCodes.has(group.code)
                  return (
                    <>
                      <TableRow key={group.code}>
                        <TableCell>
                          <button
                            type="button"
                            className="rounded border border-zinc-300 px-1.5 text-xs dark:border-white/20"
                            onClick={() => {
                              const next = new Set(expandedHistoryCodes)
                              if (expanded) next.delete(group.code)
                              else next.add(group.code)
                              setExpandedHistoryCodes(next)
                            }}
                          >
                            {expanded ? '−' : '+'}
                          </button>
                        </TableCell>
                        <TableCell>
                          <CodeLink code={group.code} onClickDetail={openStockDetail} />
                        </TableCell>
                        <TableCell>
                          <NameLink row={group} />
                        </TableCell>
                        <TableCell className="tabular-nums">{group.latest_buy_time}</TableCell>
                        <TableCell className="tabular-nums">{group.first_buy_time}</TableCell>
                        <TableCell className="text-right tabular-nums">{group.buy_count}</TableCell>
                        <TableCell className="text-right tabular-nums">{group.sell_count}</TableCell>
                        <TableCell className="text-right tabular-nums">{group.net_shares}</TableCell>
                        <TableCell>
                          <Badge color={group.is_active ? 'emerald' : 'zinc'}>
                            {group.is_active ? '持有中' : '已卖出'}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {group.current_shares > 0 ? group.current_shares : '--'}
                        </TableCell>
                        <TableCell className="text-right">
                          <ChangeValue value={group.realized_pnl} digits={2} suffix="" />
                        </TableCell>
                        <TableCell className="tabular-nums">{group.last_trade_time}</TableCell>
                        <TableCell className="max-w-[240px] truncate" title={group.summary}>
                          {group.summary}
                        </TableCell>
                      </TableRow>
                      {expanded && (
                        <TableRow key={`${group.code}-detail`}>
                          <TableCell colSpan={13}>
                            <Table dense>
                              <TableHead>
                                <TableRow>
                                  <TableHeader>成交时间</TableHeader>
                                  <TableHeader>方向</TableHeader>
                                  <TableHeader className="text-right">数量</TableHeader>
                                  <TableHeader className="text-right">成交价</TableHeader>
                                  <TableHeader className="text-right">变动前</TableHeader>
                                  <TableHeader className="text-right">变动后</TableHeader>
                                  <TableHeader className="text-right">已实现盈亏</TableHeader>
                                  <TableHeader>原因/备注</TableHeader>
                                </TableRow>
                              </TableHead>
                              <TableBody>
                                {group.trades.length === 0 && (
                                  <TableRow>
                                    <TableCell colSpan={8}>
                                      <EmptyState text="该股暂无明细" />
                                    </TableCell>
                                  </TableRow>
                                )}
                                {group.trades.map((trade: any, ti: number) => (
                                  <TableRow key={ti}>
                                    <TableCell className="tabular-nums">{trade.time}</TableCell>
                                    <TableCell>{trade.side}</TableCell>
                                    <TableCell className="text-right tabular-nums">{trade.shares ?? '--'}</TableCell>
                                    <TableCell className="text-right tabular-nums">{formatPrice(trade.price)}</TableCell>
                                    <TableCell className="text-right tabular-nums">{trade.before_shares ?? '--'}</TableCell>
                                    <TableCell className="text-right tabular-nums">{trade.after_shares ?? '--'}</TableCell>
                                    <TableCell className="text-right">
                                      <ChangeValue value={trade.realized_pnl} digits={2} suffix="" />
                                    </TableCell>
                                    <TableCell>{trade.reason || '--'}</TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </TableCell>
                        </TableRow>
                      )}
                    </>
                  )
                })}
              </TableBody>
            </Table>
            {historyTradeGroups.length > historyPageSize && (
              <div className="mt-3 flex flex-wrap items-center justify-end gap-3 text-sm">
                <span className="text-zinc-500 dark:text-zinc-400">
                  共 {historyTradeGroups.length} 组 第 {historyPage}/{Math.max(1, Math.ceil(historyTradeGroups.length / historyPageSize))} 页
                </span>
                <Select
                  value={String(historyPageSize)}
                  onChange={(e) => {
                    setHistoryPageSize(Number(e.target.value))
                    setHistoryPage(1)
                  }}
                  className="w-20"
                >
                  {[10, 20, 50, 100].map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </Select>
                <Button
                  disabled={historyPage <= 1}
                  onClick={() => setHistoryPage((p) => Math.max(1, p - 1))}
                >
                  上一页
                </Button>
                <Button
                  disabled={historyPage >= Math.ceil(historyTradeGroups.length / historyPageSize)}
                  onClick={() => setHistoryPage((p) => p + 1)}
                >
                  下一页
                </Button>
              </div>
            )}
          </Card>

          {/* 交易历史持仓明细 */}
          <Card>
            <SectionTitle title="交易历史持仓明细" />
            <Table dense striped>
              <TableHead>
                <TableRow>
                  <TableHeader>代码</TableHeader>
                  <TableHeader>名称</TableHeader>
                  <TableHeader className="text-right">数量</TableHeader>
                  <TableHeader className="text-right">成本</TableHeader>
                  <TableHeader className="text-right">当前价</TableHeader>
                  <TableHeader className="text-right">盈亏%</TableHeader>
                  <TableHeader className="text-right">持有天数</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {(positions.rows || []).length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7}>
                      <EmptyState text="暂无当前交易持仓" />
                    </TableCell>
                  </TableRow>
                )}
                {(positions.rows || []).map((row: any, i: number) => (
                  <TableRow key={`${row.code}-${i}`}>
                    <TableCell>
                      <CodeLink code={row.code} onClickDetail={openStockDetail} />
                    </TableCell>
                    <TableCell>
                      <NameLink row={row} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{row.shares ?? '--'}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.avg_cost ?? '--'}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatPrice(row.current_price)}</TableCell>
                    <TableCell className="text-right">{pnlSpan(row.pnl_ratio)}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.hold_days ?? '--'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>
        </>
      )}

      {/* ---------- 弹窗：验证 ---------- */}
      <Dialog open={gen2VerificationDialogVisible} onClose={() => setGen2VerificationDialogVisible(false)}>
        <DialogTitle>G2候选确认验证</DialogTitle>
        <DialogBody>
          <div className="mb-3 text-sm text-zinc-700 dark:text-zinc-300">
            代码：{normalizeCode(gen2VerificationForm.row?.code) || '--'} {gen2VerificationForm.row?.name || ''}
          </div>
          <Field>
            <Label>验证状态</Label>
            <Select
              value={gen2VerificationForm.status}
              onChange={(e) => setGen2VerificationForm((prev: any) => ({ ...prev, status: e.target.value }))}
            >
              {gen2VerificationOptions.map((item: any) => (
                <option key={item.value || 'blank'} value={item.value}>
                  {item.label}
                </option>
              ))}
            </Select>
          </Field>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field>
              <Label>仓位%</Label>
              <Input
                type="number"
                min={0}
                max={100}
                step={5}
                value={gen2VerificationForm.position_pct ?? ''}
                onChange={(e) =>
                  setGen2VerificationForm((prev: any) => ({
                    ...prev,
                    position_pct: e.target.value === '' ? null : Number(e.target.value),
                  }))
                }
              />
            </Field>
            <Field>
              <Label>成交价</Label>
              <Input
                type="number"
                min={0}
                step={0.01}
                value={gen2VerificationForm.fill_price ?? ''}
                onChange={(e) =>
                  setGen2VerificationForm((prev: any) => ({
                    ...prev,
                    fill_price: e.target.value === '' ? null : Number(e.target.value),
                  }))
                }
              />
            </Field>
          </div>
          <Field className="mt-3">
            <Label>备注</Label>
            <Textarea
              rows={3}
              value={gen2VerificationForm.note}
              placeholder="记录为什么跟、为什么放弃、盘中执行偏差等"
              onChange={(e) => setGen2VerificationForm((prev: any) => ({ ...prev, note: e.target.value }))}
            />
          </Field>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setGen2VerificationDialogVisible(false)}>取消</Button>
          <Button color="blue" disabled={gen2VerificationSaving} onClick={saveGen2Verification}>
            {gen2VerificationSaving ? '保存中…' : '保存验证'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* ---------- 弹窗：新增持仓 ---------- */}
      <Dialog open={addDialogVisible} onClose={() => setAddDialogVisible(false)}>
        <DialogTitle>新增实盘持仓</DialogTitle>
        <DialogBody>
          <Field>
            <Label>代码</Label>
            <div className="relative">
              <Input
                value={addForm.code}
                placeholder="输入代码或名称模糊匹配"
                onChange={(e) => onAddFormCodeChange(e.target.value)}
              />
              {codeSuggestions.length > 0 && (
                <ul className="absolute z-10 mt-1 max-h-48 w-full overflow-auto rounded-lg border border-zinc-200 bg-white shadow-lg dark:border-white/10 dark:bg-zinc-800">
                  {codeSuggestions.map((item) => (
                    <li key={item.code}>
                      <button
                        type="button"
                        className="block w-full px-3 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-white/10"
                        onClick={() => handleCodeSelect(item)}
                      >
                        {item.value}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Field>
          <Field className="mt-3">
            <Label>名称</Label>
            <Input
              value={addForm.name}
              placeholder="可选"
              onChange={(e) => setAddForm((prev: any) => ({ ...prev, name: e.target.value }))}
            />
          </Field>
          {codeLookupText && (
            <div className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">匹配结果：{codeLookupText}</div>
          )}
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field>
              <Label>股数</Label>
              <Input
                type="number"
                min={0}
                step={100}
                value={addForm.shares}
                onChange={(e) => setAddForm((prev: any) => ({ ...prev, shares: Number(e.target.value) || 0 }))}
              />
            </Field>
            <Field>
              <Label>成本价</Label>
              <Input
                type="number"
                min={0}
                step={0.01}
                value={addForm.cost_price ?? ''}
                onChange={(e) =>
                  setAddForm((prev: any) => ({
                    ...prev,
                    cost_price: e.target.value === '' ? null : Number(e.target.value),
                  }))
                }
              />
            </Field>
          </div>
          <div className="mt-3">
            <span className="text-sm text-zinc-500 dark:text-zinc-400">实时盈亏：</span>
            <span className={pnlClass(livePnlPct)}>{livePnlText}</span>
          </div>
          <Field className="mt-3">
            <Label>入场时间（精确到分钟）</Label>
            <Input
              type="datetime-local"
              value={addForm.buy_time ? addForm.buy_time.replace(' ', 'T') : ''}
              onChange={(e) =>
                setAddForm((prev: any) => ({
                  ...prev,
                  buy_time: e.target.value ? e.target.value.replace('T', ' ') : '',
                }))
              }
            />
          </Field>
          <div className="mt-3 text-sm text-zinc-500 dark:text-zinc-400">
            历史预期：
            {entryBacktestLoading ? (
              '正在评估历史样本...'
            ) : entryBacktestText ? (
              entryBacktestText
            ) : (
              '输入代码后自动评估'
            )}
          </div>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setAddDialogVisible(false)}>取消</Button>
          <Button color="blue" onClick={confirmAddHolding}>
            检查交易规则
          </Button>
        </DialogActions>
      </Dialog>

      {/* ---------- 弹窗：规则检查 ---------- */}
      <Dialog open={ruleDialogVisible} onClose={() => setRuleDialogVisible(false)} size="3xl">
        <DialogTitle>交易规则检查</DialogTitle>
        <DialogBody>
          <Alert color={ruleCheckPassed ? 'green' : 'amber'}>
            <div>
              {ruleCheckPassed ? '规则检查通过，可以加入持仓' : '存在不符合策略的规则，请先调整'}
            </div>
          </Alert>
          <div className="mt-3">
            <Table dense striped>
              <TableHead>
                <TableRow>
                  <TableHeader>规则</TableHeader>
                  <TableHeader>结果</TableHeader>
                  <TableHeader>说明</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {ruleChecklist.map((row, i) => (
                  <TableRow key={i}>
                    <TableCell>{row.label}</TableCell>
                    <TableCell>
                      <Badge color={row.pass ? 'emerald' : 'red'}>{row.pass ? '通过' : '不通过'}</Badge>
                    </TableCell>
                    <TableCell>{row.reason || '--'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setRuleDialogVisible(false)}>关闭</Button>
          <Button color="blue" disabled={!ruleCheckPassed} onClick={confirmAddHoldingAfterCheck}>
            通过并加入
          </Button>
        </DialogActions>
      </Dialog>

      {/* ---------- 弹窗：卖出 ---------- */}
      <Dialog open={sellDialogVisible} onClose={() => setSellDialogVisible(false)}>
        <DialogTitle>卖出持仓</DialogTitle>
        <DialogBody>
          <div className="mb-2 text-sm text-zinc-700 dark:text-zinc-300">
            代码：{displayCode(sellForm.code)} ｜ 名称：{sellForm.name || '--'} ｜ 可卖数量：{sellForm.max_shares}
          </div>
          <Field>
            <Label>卖出数量（可卖 {sellForm.max_shares}）</Label>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min={1}
                max={Math.max(1, Number(sellForm.max_shares || 1))}
                step={1}
                value={sellForm.shares}
                onChange={(e) => setSellForm((prev: any) => ({ ...prev, shares: Number(e.target.value) || 0 }))}
              />
              <Button plain onClick={() => setSellForm((prev: any) => ({ ...prev, shares: Math.max(1, Math.floor(Math.max(1, Number(prev.max_shares || 1)) / 2)) }))}>
                卖出一半
              </Button>
              <Button plain onClick={() => setSellForm((prev: any) => ({ ...prev, shares: Math.max(1, Math.trunc(Number(prev.max_shares || 1))) }))}>
                全部卖出
              </Button>
            </div>
          </Field>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field>
              <Label>卖出价格</Label>
              <Input
                type="number"
                min={0}
                step={0.01}
                value={sellForm.price ?? ''}
                onChange={(e) =>
                  setSellForm((prev: any) => ({
                    ...prev,
                    price: e.target.value === '' ? null : Number(e.target.value),
                  }))
                }
              />
            </Field>
            <Field>
              <Label>卖出时间（精确到分钟）</Label>
              <Input
                type="datetime-local"
                value={sellForm.time ? sellForm.time.replace(' ', 'T') : ''}
                onChange={(e) =>
                  setSellForm((prev: any) => ({
                    ...prev,
                    time: e.target.value ? e.target.value.replace('T', ' ') : '',
                  }))
                }
              />
            </Field>
          </div>
          <Field className="mt-3">
            <Label>原因</Label>
            <Input
              value={sellForm.reason}
              placeholder="止盈/止损/策略卖出"
              onChange={(e) => setSellForm((prev: any) => ({ ...prev, reason: e.target.value }))}
            />
          </Field>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setSellDialogVisible(false)}>取消</Button>
          <Button color="blue" onClick={confirmSellHolding}>
            确认卖出
          </Button>
        </DialogActions>
      </Dialog>

      {/* ---------- 弹窗：交易记录 ---------- */}
      <Dialog open={tradeLogVisible} onClose={() => setTradeLogVisible(false)} size="5xl">
        <DialogTitle>实盘持仓交易记录</DialogTitle>
        <DialogBody>
          <div className="max-h-[60vh] overflow-auto">
            <Table dense striped>
              <TableHead>
                <TableRow>
                  <TableHeader>时间</TableHeader>
                  <TableHeader>方向</TableHeader>
                  <TableHeader>代码</TableHeader>
                  <TableHeader>名称</TableHeader>
                  <TableHeader className="text-right">数量</TableHeader>
                  <TableHeader className="text-right">价格</TableHeader>
                  <TableHeader className="text-right">卖前</TableHeader>
                  <TableHeader className="text-right">卖后</TableHeader>
                  <TableHeader className="text-right">已实现盈亏</TableHeader>
                  <TableHeader>原因</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {manualTradeLogs.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={10}>
                      <EmptyState text="暂无交易记录" />
                    </TableCell>
                  </TableRow>
                )}
                {manualTradeLogs.map((row, i) => (
                  <TableRow key={i}>
                    <TableCell className="tabular-nums">{row.time || '--'}</TableCell>
                    <TableCell>{row.side || '--'}</TableCell>
                    <TableCell className="tabular-nums">{displayCode(row.code)}</TableCell>
                    <TableCell>{row.name || '--'}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.shares ?? '--'}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatPrice(row.price)}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.before_shares ?? '--'}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.after_shares ?? '--'}</TableCell>
                    <TableCell className="text-right">
                      <ChangeValue value={row.realized_pnl} digits={2} suffix="" />
                    </TableCell>
                    <TableCell>{row.reason || '--'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setTradeLogVisible(false)}>关闭</Button>
          <Button color="red" onClick={clearTradeLogs}>
            清空记录
          </Button>
        </DialogActions>
      </Dialog>

      {/* ---------- 弹窗：同花顺导入 ---------- */}
      <Dialog open={thsImportVisible} onClose={() => setThsImportVisible(false)} size="5xl">
        <DialogTitle>读取同花顺历史成交</DialogTitle>
        <DialogBody>
          <Alert color="zinc">
            <div>
              优先读取交割单文件；也可以从同花顺历史成交页复制后读取剪贴板。系统会自动过滤无效成交。
            </div>
          </Alert>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button color="green" disabled={thsImportLoading} onClick={readThsTradesFromDeliveryFile}>
                读取交割单文件
              </Button>
              <Button color="blue" disabled={thsImportLoading} onClick={readThsTradesFromWindow}>
                直接读取交易窗口
              </Button>
              <Button disabled={thsImportLoading} onClick={readThsTradesFromClipboard}>
                读取剪贴板
              </Button>
              <Button disabled={thsImportLoading} onClick={() => parseThsTradeText(thsImportRawText)}>
                解析下方文本
              </Button>
            </div>
            <div className="text-sm text-zinc-500 dark:text-zinc-400">
              已识别 {thsImportRows.length} 条可导入成交，待确认 {thsImportRows.filter((item) => item.checked).length} 条
            </div>
          </div>
          <Textarea
            rows={6}
            className="mt-3"
            value={thsImportRawText}
            placeholder="如果浏览器无法直接读取剪贴板，请把同花顺复制内容粘贴到这里，再点击解析下方文本"
            onChange={(e) => setThsImportRawText(e.target.value)}
          />
          <div className="mt-3 max-h-[40vh] overflow-auto">
            <Table dense striped>
              <TableHead>
                <TableRow>
                  <TableHeader>确认</TableHeader>
                  <TableHeader>时间</TableHeader>
                  <TableHeader>方向</TableHeader>
                  <TableHeader>代码</TableHeader>
                  <TableHeader>名称</TableHeader>
                  <TableHeader className="text-right">数量</TableHeader>
                  <TableHeader className="text-right">成交价</TableHeader>
                  <TableHeader className="text-right">成交额</TableHeader>
                  <TableHeader>备注</TableHeader>
                  <TableHeader>识别说明</TableHeader>
                </TableRow>
              </TableHead>
              <TableBody>
                {thsImportRows.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={10}>
                      <EmptyState text="暂无可导入成交" />
                    </TableCell>
                  </TableRow>
                )}
                {thsImportRows.map((row, i) => (
                  <TableRow key={i}>
                    <TableCell>
                      <Checkbox
                        checked={!!row.checked}
                        onChange={(checked) => {
                          const next = [...thsImportRows]
                          next[i] = { ...row, checked }
                          setThsImportRows(next)
                        }}
                      />
                    </TableCell>
                    <TableCell className="tabular-nums">{row.time}</TableCell>
                    <TableCell>{row.side}</TableCell>
                    <TableCell className="tabular-nums">{row.code}</TableCell>
                    <TableCell>{row.name}</TableCell>
                    <TableCell className="text-right tabular-nums">{row.shares}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatPrice(row.price)}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatMoney(row.amount)}</TableCell>
                    <TableCell>{row.remark || '--'}</TableCell>
                    <TableCell>{row.filter_reason || '--'}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </DialogBody>
        <DialogActions>
          <Button onClick={() => setThsImportVisible(false)}>取消</Button>
          <Button
            color="blue"
            disabled={thsImportRows.filter((item) => item.checked).length <= 0}
            onClick={confirmImportThsTrades}
          >
            勾选确认导入
          </Button>
        </DialogActions>
      </Dialog>
    </div>
  )
}

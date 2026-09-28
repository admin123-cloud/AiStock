import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { EChartsCoreOption } from 'echarts'
import { Alert } from '@/components/catalyst/alert'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Dialog, DialogActions, DialogBody, DialogTitle } from '@/components/catalyst/dialog'
import { Input } from '@/components/catalyst/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/catalyst/table'
import EChart from '@/components/EChart'
import { useToast } from '@/components/toast'
import { Card, ChangeValue, PageHeader, PageLoading, SectionTitle, StatCard } from '@/components/ui'
import { getStockDetail } from '@/api/stock'
import {
  confirmGen3HoldingTPortfolio,
  getGen3HoldingTPortfolioState,
  getGen3StateAlphaHoldingTickAnalysis,
  getGen3StateAlphaHoldingTickChart,
  getGen3StateAlphaHoldingTickWatchlist,
  saveGen3HoldingTPortfolioState,
  saveGen3StateAlphaHoldingTickWatchlist,
} from '@/api/trading'

type Row = Record<string, any>

const TRACKED_CODES_STORAGE_KEY = 'aistock:g3-holding-tick-tracked-codes'
const ACTION_STATES_STORAGE_KEY = 'aistock:g3-holding-tick-action-states'
const ACTION_CONFIRMATIONS_REQUIRED = 3
const CHART_PERIODS = ['1d', '60m', '15m', '5m'] as const

const DEFAULT_STABLE = { key: 'hold', type: 'info' as const, label: '持有观察', detail: '尚未形成连续确认的方向。' }

function normalizeCode(value: any): string {
  const code = String(value || '').trim().toUpperCase()
  if (!code) return ''
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(code)) return code
  if (!/^\d{6}$/.test(code)) return code
  if (/^(4|8)/.test(code)) return `${code}.BJ`
  return `${code}.${/^(5|6|9)/.test(code) ? 'SH' : 'SZ'}`
}

function parseCodes(value: any): string[] {
  return [...new Set(String(value || '').split(/[,，\s]+/).map(normalizeCode).filter(Boolean))]
}

function isCodeLikeName(name: any, code: any): boolean {
  const value = String(name || '').trim().toUpperCase()
  if (!value) return true
  return value === normalizeCode(code) || value === String(normalizeCode(code)).split('.')[0]
}

function price(value: any): string {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '--'
}

function changePct(value: any): string {
  return Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)}%` : '--'
}

function ratioPct(value: any): string {
  return Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(0)}%` : '--'
}

function rsi(value: any): string {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(1) : '--'
}

function rsiState(value: any): string {
  return Number(value) >= 70 ? '超买' : Number(value) <= 30 ? '超卖' : '中性'
}

function rsiClass(value: any): string {
  return Number(value) >= 70
    ? 'font-bold text-red-600 dark:text-red-400'
    : Number(value) <= 30
      ? 'font-bold text-green-600 dark:text-green-500'
      : ''
}

function tickAge(row: Row): string {
  return Number.isFinite(Number(row.tick_age_seconds)) ? `${Math.round(Number(row.tick_age_seconds))}秒前` : '新鲜度未知'
}

function sectorBreadth(row: Row): string {
  const sampleSize = Number(row.peer_sample_size)
  const positiveRatio = Number(row.peer_positive_ratio)
  if (!Number.isFinite(sampleSize) || sampleSize <= 0 || !Number.isFinite(positiveRatio)) return '--'
  const upCount = Math.round(sampleSize * positiveRatio)
  return `${ratioPct(positiveRatio)}（${upCount}/${sampleSize}上涨）`
}

function sectorTagColor(value: any): 'emerald' | 'red' | 'amber' | 'zinc' {
  return ({ 同步增强: 'emerald', 同步走弱: 'red', 分化: 'amber', 样本不足: 'zinc' } as Record<string, 'emerald' | 'red' | 'amber' | 'zinc'>)[value] || 'zinc'
}

function peerLeaders(row: Row): string {
  return (row.peer_changes || []).map((item: Row) => `${item.name || item.code} ${changePct(item.change_pct)}`).join('；') || '--'
}

function tagColorOf(type: string): 'red' | 'emerald' | 'amber' | 'zinc' {
  if (type === 'danger') return 'red'
  if (type === 'success') return 'emerald'
  if (type === 'warning') return 'amber'
  return 'zinc'
}

function evaluateActionSuggestion(row: Row) {
  const last = Number(row.last_price)
  const open = Number(row.open)
  const low = Number(row.low)
  const vwap = Number(row.vwap_5m)
  const change = Number(row.change_pct)
  const imbalance = Number(row.order_book_imbalance)
  const rsi5 = Number(row.rsi_5m_14)
  const rsi15 = Number(row.rsi_15m_14)
  const belowVwap = Number.isFinite(last) && Number.isFinite(vwap) && last < vwap
  const nearLow = Number.isFinite(last) && Number.isFinite(low) && last <= low * 1.005
  const sellerDominant = Number.isFinite(imbalance) && imbalance <= -0.35
  const buyerDominant = Number.isFinite(imbalance) && imbalance >= 0.35
  const sectorStrong = row.sector_follow_state === '同步增强'
  const sectorWeak = row.sector_follow_state === '同步走弱'
  const relativeWeak = Number(row.relative_strength_pct) <= -0.8
  const overbought = rsi5 >= 70 || rsi15 >= 70
  const oversold = rsi5 <= 30 || rsi15 <= 30

  if (!row.available || !row.tick_fresh) {
    return { key: 'observe', type: 'info', label: '仅观察', detail: '行情不可用或已过期，不生成交易候选。' }
  }
  if (belowVwap && sellerDominant && (nearLow || change <= -5 || sectorWeak)) {
    return { key: 'sell', type: 'danger', label: '建议卖出候选', detail: '弱于当日VWAP且卖盘占优；需5分钟、30分钟确认后再核实。' }
  }
  if (change >= 3 && overbought && belowVwap && (sellerDominant || sectorWeak || last < open)) {
    return { key: 't_sell', type: 'warning', label: '建议做T卖出候选', detail: '日内较强且RSI进入超买后转弱；仅针对可卖老仓，需5分钟确认。' }
  }
  if (!belowVwap && buyerDominant && sectorStrong && !relativeWeak) {
    return { key: 'buy', type: 'success', label: '建议买入候选', detail: '个股站上当日VWAP、买盘占优且板块同步增强；仍需30分钟结构确认。' }
  }
  if (sectorStrong && relativeWeak) {
    return { key: 'no_add', type: 'warning', label: '暂不建议补仓', detail: '板块偏强但个股明显落后，等待个股重新跟随。' }
  }
  if (oversold && belowVwap) {
    return { key: 'oversold_wait', type: 'warning', label: '超卖观察', detail: 'RSI进入超卖但尚未出现承接确认，不建议直接抄底。' }
  }
  return { key: 'hold', type: 'info', label: '持有观察', detail: '当前证据不足以形成买卖或做T候选。' }
}

function trendText(row: Row | null): string {
  const trend = row?.daily_trend || {}
  const line = Number(trend.intraday_trend_line_price)
  if (!Number.isFinite(line)) return '日线趋势线数据不足，仍可查看多周期K线。'
  return trend.intraday_trend_broken
    ? `盘中价格已跌破日线主升趋势线 ${line.toFixed(2)}：卖出/换股复核。`
    : `盘中价格仍在日线主升趋势线 ${line.toFixed(2)} 之上。`
}

/** 由图表行构造 ECharts K线 option（红涨绿跌），可选趋势线 */
function buildKLineOption(title: string, rows: Row[], trendPoints: { time: string; value: number }[] = []): EChartsCoreOption {
  const times = rows.map((row) => String(row.datetime || row.time || row.date || ''))
  const candles = rows.map((row) => [Number(row.open), Number(row.close), Number(row.low), Number(row.high)])
  const trendByTime = new Map(trendPoints.map((p) => [String(p.time), p.value]))
  const trendData = times.map((t) => (trendByTime.has(t) ? trendByTime.get(t) : null))
  return {
    title: { text: title, left: 8, top: 6, textStyle: { fontSize: 13, fontWeight: 600 } },
    animation: false,
    grid: { left: 56, right: 16, top: 40, bottom: 44 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    xAxis: { type: 'category', data: times, boundaryGap: true, axisLine: { lineStyle: { color: '#98a2b3' } } },
    yAxis: { scale: true, axisLine: { lineStyle: { color: '#98a2b3' } }, splitLine: { lineStyle: { color: 'rgba(128,128,128,0.15)' } } },
    dataZoom: [
      { type: 'inside', start: 60, end: 100 },
      { type: 'slider', height: 18, bottom: 8 },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: candles,
        // A 股语义色：上涨红色，下跌绿色
        itemStyle: { color: '#ef232a', color0: '#14b143', borderColor: '#ef232a', borderColor0: '#14b143' },
      },
      ...(trendPoints.length
        ? [{
            name: '趋势线',
            type: 'line',
            data: trendData,
            showSymbol: false,
            lineStyle: { color: '#f59e0b', width: 1.5 },
            connectNulls: false,
          }]
        : []),
    ],
  }
}

export default function Gen3StateAlphaHoldingTicks() {
  const toast = useToast()
  const [loading, setLoading] = useState(false)
  const [codes, setCodes] = useState('')
  const [items, setItems] = useState<Row[]>([])
  const [warnings, setWarnings] = useState<string[]>([])
  const [source, setSource] = useState<Row>({})
  const [brokerUpdatedAt, setBrokerUpdatedAt] = useState('')
  const [savedCodes, setSavedCodes] = useState<string[]>([])
  const [trackedCodes, setTrackedCodes] = useState<string[]>([])
  const [actionStates, setActionStates] = useState<Record<string, Row>>({})
  const [portfolioState, setPortfolioState] = useState<Row>({ positions: {} })
  const [positionConfirmationOpen, setPositionConfirmationOpen] = useState(false)
  const [positionConfirmationSubmitting, setPositionConfirmationSubmitting] = useState(false)
  const [positionConfirmation, setPositionConfirmation] = useState<Row>({ code: '', side: 'buy', price: null, shares: null })
  const [chartOpen, setChartOpen] = useState(false)
  const [chartLoading, setChartLoading] = useState(false)
  const [chartReady, setChartReady] = useState(false)
  const [selectedChartRow, setSelectedChartRow] = useState<Row | null>(null)
  const [chartPayload, setChartPayload] = useState<Row>({ charts: {} })
  // 模拟做T记录本地编辑态（key 为行索引）
  const [simEdits, setSimEdits] = useState<Record<number, { editing: boolean; price: any; shares: any }>>({})

  const stockNameCache = useRef(new Map<string, string>())
  const loadingRef = useRef(false)
  const trackedCodesRef = useRef<string[]>([])
  trackedCodesRef.current = trackedCodes

  const availableCount = items.filter((item) => item.available).length
  const batchCodes = parseCodes(codes)
  const positionConfirmationLabel = positionConfirmation.side === 'buy' ? '买入' : '卖出'

  const isTracked = (code: any) => trackedCodes.includes(normalizeCode(code))

  const enrichStockNames = useCallback(async (rows: Row[]) => {
    const missingCodes = [...new Set(rows
      .filter((row) => isCodeLikeName(row.name, row.code))
      .map((row) => normalizeCode(row.code))
      .filter(Boolean))]
    await Promise.all(missingCodes.map(async (code) => {
      if (stockNameCache.current.has(code)) return
      try {
        const payload: any = await getStockDetail(code)
        const name = String(payload?.name || '').trim()
        if (name && !isCodeLikeName(name, code)) stockNameCache.current.set(code, name)
      } catch {
        // 名称缺失不能阻塞只读 Tick 刷新
      }
    }))
    rows.forEach((row) => {
      const name = stockNameCache.current.get(normalizeCode(row.code))
      if (name && isCodeLikeName(row.name, row.code)) row.name = name
    })
  }, [])

  const updateActionStates = useCallback((rows: Row[]) => {
    setActionStates((prevStates) => {
      const next = { ...prevStates }
      for (const row of rows) {
        const raw = evaluateActionSuggestion(row)
        const previous = next[row.code] || { stable: DEFAULT_STABLE, pendingKey: '', pendingCount: 0 }
        if (raw.key === 'observe') {
          next[row.code] = { ...previous, raw, pendingKey: '', pendingCount: 0 }
          continue
        }
        const pendingCount = raw.key === previous.pendingKey
          ? Math.min(previous.pendingCount + 1, ACTION_CONFIRMATIONS_REQUIRED)
          : 1
        next[row.code] = {
          stable: pendingCount >= ACTION_CONFIRMATIONS_REQUIRED ? raw : previous.stable,
          raw,
          pendingKey: raw.key,
          pendingCount,
        }
      }
      window.localStorage.setItem(ACTION_STATES_STORAGE_KEY, JSON.stringify(next))
      return next
    })
  }, [])

  function actionSuggestion(row: Row) {
    const raw = evaluateActionSuggestion(row)
    if (raw.key === 'observe') return raw
    const state = actionStates[row.code]
    const stable = state?.stable || DEFAULT_STABLE
    const confirmations = state?.pendingKey === raw.key ? state.pendingCount : 1
    const confirmed = stable.key === raw.key && confirmations >= ACTION_CONFIRMATIONS_REQUIRED
    const realtimeDetail = confirmed
      ? `同向信号已连续核实 ${ACTION_CONFIRMATIONS_REQUIRED}/${ACTION_CONFIRMATIONS_REQUIRED} 次。`
      : `实时提示为“${raw.label}”，连续核实 ${confirmations}/${ACTION_CONFIRMATIONS_REQUIRED} 次。`
    return { ...stable, detail: `${stable.detail} ${realtimeDetail}` }
  }

  const load = useCallback(async (overrideCodes: string = '', options: { persistInput?: boolean } = {}) => {
    const { persistInput = true } = options
    loadingRef.current = true
    setLoading(true)
    try {
      const explicitCodes = typeof overrideCodes === 'string' ? overrideCodes.trim() : ''
      const inputCodes = parseCodes(explicitCodes || codes)
      if (persistInput && inputCodes.length) {
        try {
          const payload: any = await saveGen3StateAlphaHoldingTickWatchlist(inputCodes)
          if (!payload?.ok) throw new Error(payload?.message || 'Failed to save holding Tick watchlist')
          setSavedCodes(parseCodes((payload.codes || []).join(',')))
        } catch (error: any) {
          setWarnings([error?.message || 'Holding Tick watchlist save failed; analysis will continue'])
        }
      }
      const requestCodes = [...new Set([...trackedCodesRef.current, ...inputCodes])].join(',')
      const response: any = await getGen3StateAlphaHoldingTickAnalysis(requestCodes ? { codes: requestCodes } : {})
      const responseItems = response.items || []
      await enrichStockNames(responseItems)
      setItems(responseItems)
      updateActionStates(responseItems)
      setWarnings(response.warnings || [])
      setSource(response.source || {})
      setBrokerUpdatedAt(response.broker_updated_at || '')
    } catch (error: any) {
      setWarnings([error?.message || 'Tick 分析请求失败'])
      setItems([])
    } finally {
      loadingRef.current = false
      setLoading(false)
    }
  }, [codes, enrichStockNames, updateActionStates])

  // 跟踪定时器：5 秒轮询，卸载时清理
  const loadRef = useRef(load)
  loadRef.current = load
  const resetTrackingTimer = useCallback(() => {
    window.clearInterval((window as any).__g3TickTimer)
    ;(window as any).__g3TickTimer = null
    if (!trackedCodesRef.current.length) return
    ;(window as any).__g3TickTimer = window.setInterval(() => {
      if (!loadingRef.current) loadRef.current(trackedCodesRef.current.join(','), { persistInput: false })
    }, 5000)
  }, [])

  useEffect(() => () => {
    window.clearInterval((window as any).__g3TickTimer)
  }, [])

  const persistTrackedCodes = (next: string[]) => {
    window.localStorage.setItem(TRACKED_CODES_STORAGE_KEY, JSON.stringify(next))
  }

  async function startTracking(code: any) {
    const normalizedCode = normalizeCode(code)
    if (!normalizedCode || trackedCodes.includes(normalizedCode)) return
    const next = [...trackedCodes, normalizedCode]
    setTrackedCodes(next)
    trackedCodesRef.current = next
    persistTrackedCodes(next)
    resetTrackingTimer()
    await load(next.join(','), { persistInput: false })
  }

  function stopTracking(code: any) {
    const normalizedCode = normalizeCode(code)
    const next = trackedCodes.filter((item) => item !== normalizedCode)
    setTrackedCodes(next)
    trackedCodesRef.current = next
    persistTrackedCodes(next)
    resetTrackingTimer()
  }

  async function trackBatch() {
    const codesToTrack = batchCodes.length
      ? batchCodes
      : items.filter((item) => item.available).map((item) => item.code)
    if (!codesToTrack.length) {
      setWarnings(['请先输入至少一只股票代码，或先分析并加载持仓。'])
      return
    }
    const next = [...new Set([...trackedCodes, ...codesToTrack])]
    setTrackedCodes(next)
    trackedCodesRef.current = next
    persistTrackedCodes(next)
    resetTrackingTimer()
    await load(next.join(','), { persistInput: false })
  }

  function stopAllTracking() {
    setTrackedCodes([])
    trackedCodesRef.current = []
    persistTrackedCodes([])
    resetTrackingTimer()
  }

  async function openChart(row: Row) {
    if (!row?.code) return
    setSelectedChartRow(row)
    setChartOpen(true)
    setChartReady(false)
    setChartLoading(true)
    try {
      const payload: any = await getGen3StateAlphaHoldingTickChart(row.code)
      setChartPayload(payload)
      setChartReady(true)
    } catch (error: any) {
      setWarnings([error?.message || '多周期K线加载失败'])
      setChartPayload({ charts: {} })
    } finally {
      setChartLoading(false)
    }
  }

  const chartRows = useCallback((period: string): Row[] => {
    const rows = chartPayload?.charts?.[period] || []
    return rows.map((row: Row) => {
      const sourceTime = String(row?.datetime || row?.time || row?.date || '')
      if (period === '1d') {
        return { ...row, date: sourceTime.slice(0, 10) }
      }
      return { ...row, datetime: sourceTime.replace('T', ' ') }
    })
  }, [chartPayload])

  const trendLinePoints = useCallback((period: string): { time: string; value: number }[] => {
    if (period !== '1d') return []
    const trend = selectedChartRow?.daily_trend || {}
    const anchor = trend.anchor_low_2
    const slope = Number(trend.trend_slope_per_day)
    const rows = chartRows(period)
    if (!anchor || !Number.isFinite(slope)) return []
    const anchorDate = String(anchor.trade_date || anchor.date || '').slice(0, 10)
    const start = rows.findIndex((row) => String(row.date || row.datetime || row.time || '').slice(0, 10) === anchorDate)
    const base = Number(anchor.price)
    if (!anchorDate || start < 0 || !Number.isFinite(base)) return []
    return rows.slice(start).map((row, offset) => ({
      time: row.date || row.datetime || row.time,
      value: base + slope * offset,
    }))
  }, [selectedChartRow, chartRows])

  async function loadPortfolioState() {
    try {
      const payload: any = await getGen3HoldingTPortfolioState()
      setPortfolioState(payload?.state || { positions: {} })
    } catch {
      setPortfolioState({ positions: {} })
    }
  }

  function openPositionConfirmation(code: any, side: string) {
    const latest = items.find((item) => normalizeCode(item.code) === normalizeCode(code))
    const latestPrice = Number(latest?.last_price)
    setPositionConfirmation({
      code: normalizeCode(code),
      side,
      price: Number.isFinite(latestPrice) && latestPrice > 0 ? latestPrice : null,
      shares: null,
    })
    setSimEdits({})
    setPositionConfirmationOpen(true)
  }

  async function submitPositionConfirmation() {
    const { code, side } = positionConfirmation
    const p = Number(positionConfirmation.price)
    const shares = Number(positionConfirmation.shares)
    const label = side === 'buy' ? '买入' : '卖出'
    if (!Number.isFinite(p) || p <= 0 || !Number.isInteger(shares) || shares <= 0) {
      toast.error('请填写正的成交价格和整数成交数量')
      return
    }
    setPositionConfirmationSubmitting(true)
    try {
      const payload: any = await confirmGen3HoldingTPortfolio({ code, side, weight_pct: 25, price: p, shares })
      if (!payload?.ok) throw new Error(payload?.message || '保存失败')
      setPortfolioState(payload.state)
      setPositionConfirmationOpen(false)
      toast.success(`已持久化确认${label}：${shares} 股，${p.toFixed(3)} 元`)
    } catch (error: any) {
      toast.error(error?.message || '确认失败')
    } finally {
      setPositionConfirmationSubmitting(false)
    }
  }

  async function saveSimRecords(editedEvents?: Row[]) {
    try {
      const next = editedEvents ? { ...portfolioState, events: editedEvents } : portfolioState
      const payload: any = await saveGen3HoldingTPortfolioState(next)
      if (!payload?.ok) throw new Error(payload?.message || '保存失败')
      setPortfolioState(payload.state)
      toast.success('模拟做T记录已保存')
    } catch (error: any) {
      toast.error(error?.message || '保存失败')
    }
  }

  async function saveSimRecord(row: Row, index: number) {
    const edit = simEdits[index] || {}
    const events = (portfolioState.events || []).map((event: Row, i: number) => (
      i === index ? { ...event, price: edit.price ?? event.price, shares: edit.shares ?? event.shares } : event
    ))
    setSimEdits((prev) => ({ ...prev, [index]: { editing: false, price: undefined, shares: undefined } }))
    await saveSimRecords(events)
  }

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      await loadPortfolioState()
      try {
        const saved = JSON.parse(window.localStorage.getItem(TRACKED_CODES_STORAGE_KEY) || '[]')
        if (!cancelled && Array.isArray(saved)) {
          const next = parseCodes(saved.join(','))
          setTrackedCodes(next)
          trackedCodesRef.current = next
        }
      } catch {
        window.localStorage.removeItem(TRACKED_CODES_STORAGE_KEY)
      }
      try {
        const saved = JSON.parse(window.localStorage.getItem(ACTION_STATES_STORAGE_KEY) || '{}')
        if (!cancelled && saved && typeof saved === 'object' && !Array.isArray(saved)) setActionStates(saved)
      } catch {
        window.localStorage.removeItem(ACTION_STATES_STORAGE_KEY)
      }
      try {
        const payload: any = await getGen3StateAlphaHoldingTickWatchlist()
        if (!cancelled && payload?.ok) {
          const saved = parseCodes((payload.codes || []).join(','))
          setSavedCodes(saved)
          const merged = [...new Set([...trackedCodesRef.current, ...saved])]
          setTrackedCodes(merged)
          trackedCodesRef.current = merged
          persistTrackedCodes(merged)
          setCodes((prev) => prev || (saved.length ? saved.join(',') : ''))
        }
      } catch (error: any) {
        if (!cancelled) setWarnings([error?.message || 'Saved holding Tick watchlist load failed'])
      }
      if (cancelled) return
      resetTrackingTimer()
      loadRef.current(codes || trackedCodesRef.current.join(','), { persistInput: false })
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const chartOptions = useMemo(() => {
    if (!chartReady || !selectedChartRow) return {}
    const map: Record<string, EChartsCoreOption> = {}
    for (const period of CHART_PERIODS) {
      const title = `${selectedChartRow.name || selectedChartRow.code || ''} ${period}`
      map[period] = buildKLineOption(title, chartRows(period), trendLinePoints(period))
    }
    return map
  }, [chartReady, selectedChartRow, chartRows, trendLinePoints])

  const positions: Row = portfolioState.positions || {}
  const events: Row[] = portfolioState.events || []

  return (
    <div className="flex flex-col">
      <PageHeader
        title="做T交易"
        subtitle={
          <>
            <span className="font-bold text-blue-600 dark:text-blue-400">G3第三代策略 / 只读行情</span>
            {' · 结合 QMT 五档盘口与本地 5 分钟确认，识别做T的卖出与回补时机、盘口承接和行情异常；本页不调用下单接口。'}
          </>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {trackedCodes.length > 0 && <Badge color="emerald">持续跟踪 {trackedCodes.length} 只 / 5秒</Badge>}
            {loading && <Badge color="zinc">异步刷新中</Badge>}
            <Button color="blue" disabled={loading} onClick={() => load()}>刷新 Tick</Button>
          </div>
        }
      />

      <Alert color="zinc" className="mb-4 text-sm">
        常态建议需同一方向连续 3 次 Tick（约15秒）才切换；实时信号可变化，但不能单独推翻常态判断。仍须核实5分钟、30分钟结构、板块状态及可用仓位；页面不自动下单。
      </Alert>

      <Card className="mb-4">
        <SectionTitle title="手动仓位确认" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">目标：两只各 50%，单次调整总资金 25%。仅记录你的确认，不会下单。</p>
        <div className="flex flex-wrap gap-3">
          {Object.entries(positions).map(([code, position]: [string, Row]) => (
            <div key={code} className="flex flex-wrap items-center gap-2.5 rounded-lg border border-zinc-200 px-2.5 py-2.5 dark:border-white/10">
              <strong className="text-zinc-900 dark:text-white">{code}</strong>
              <span className="text-sm text-zinc-500 dark:text-zinc-400">当前 {position.current_weight_pct || 0}% / 目标 {position.target_weight_pct || 50}%</span>
              <Button outline onClick={() => openPositionConfirmation(code, 'buy')}>确认买入 25%</Button>
              <Button outline onClick={() => openPositionConfirmation(code, 'sell')}>确认卖出 25%</Button>
            </div>
          ))}
        </div>
      </Card>

      <Dialog open={positionConfirmationOpen} onClose={() => setPositionConfirmationOpen(false)} size="sm">
        <DialogTitle>确认{positionConfirmationLabel} 25%</DialogTitle>
        <DialogBody>
          <Alert color="zinc" className="text-sm">只记录手动确认，不会向券商提交委托。请按实际成交填写价格和数量。</Alert>
          <div className="mt-4 flex flex-col gap-3">
            <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
              标的
              <Input value={positionConfirmation.code || ''} disabled />
            </label>
            <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
              成交价格（元）
              <Input
                type="number"
                min={0.001}
                step={0.001}
                value={positionConfirmation.price ?? ''}
                onChange={(e) => setPositionConfirmation({ ...positionConfirmation, price: e.target.value === '' ? null : Number(e.target.value) })}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm text-zinc-600 dark:text-zinc-300">
              成交数量（股）
              <Input
                type="number"
                min={1}
                step={100}
                value={positionConfirmation.shares ?? ''}
                onChange={(e) => setPositionConfirmation({ ...positionConfirmation, shares: e.target.value === '' ? null : Number(e.target.value) })}
              />
            </label>
          </div>
        </DialogBody>
        <DialogActions>
          <Button plain onClick={() => setPositionConfirmationOpen(false)}>取消</Button>
          <Button
            color={positionConfirmation.side === 'buy' ? 'blue' : 'red'}
            disabled={positionConfirmationSubmitting}
            onClick={submitPositionConfirmation}
          >
            确认{positionConfirmationLabel}
          </Button>
        </DialogActions>
      </Dialog>

      <Card className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <div>
          <SectionTitle title="观察标的" className="mb-0" />
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">留空则读取账户持仓快照；可一次输入多只自选或持仓代码，以逗号、空格或换行分隔。</p>
        </div>
        <div className="flex w-full max-w-3xl flex-wrap gap-2">
          <div className="min-w-[220px] flex-1">
            <Input
              value={codes}
              onChange={(e) => setCodes(e.target.value)}
              placeholder="例如：002245,300037,300054"
              onKeyDown={(e) => { if (e.key === 'Enter') load() }}
            />
          </div>
          <Button outline disabled={loading} onClick={() => load()}>分析</Button>
          <Button outline disabled={!batchCodes.length && !items.some((item) => item.available)} onClick={trackBatch}>批量持续跟踪</Button>
          {trackedCodes.length > 0 && <Button color="red" onClick={stopAllTracking}>停止全部跟踪</Button>}
        </div>
      </Card>

      {warnings.length > 0 && (
        <Alert color="amber" className="mb-4 text-sm">{warnings.join('；')}</Alert>
      )}

      <section className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="行情来源" value={`QMT ${source.port || '--'}`} tone="info" hint={source.host || '--'} />
        <StatCard label="可用 Tick" value={`${availableCount}/${items.length}`} tone={items.length ? 'down' : 'warning'} hint="只读快照" />
        <StatCard label="账户快照时间" value={brokerUpdatedAt || '--'} hint="不在盘中自动同步账户" />
        <StatCard label="下单路径" value="已锁定" hint="Tick 页面不产生委托" />
      </section>

      <Card className="mb-4">
        <SectionTitle title="持仓盘口与异常诊断" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">“五档失衡”=(买五总量−卖五总量)/(买五总量+卖五总量)。仅持续异常且经5分钟确认后才应进入风控复核。</p>
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['标的 / Tick', '价格趋势', 'RSI 情绪', '盘口', '诊断 / 常态建议'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {items.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-zinc-500 dark:text-zinc-400">暂无可分析标的，请输入代码或先刷新账户持仓。</TableCell>
              </TableRow>
            )}
            {items.map((row) => {
              const suggestion = actionSuggestion(row)
              return (
                <TableRow key={row.code} className="cursor-pointer" onClick={() => openChart(row)}>
                  <TableCell>
                    <strong className="block text-zinc-900 dark:text-white">{row.name || row.code}</strong>
                    <small className="block text-zinc-500 dark:text-zinc-400">{row.code}</small>
                    <small className={`block ${row.tick_fresh ? 'text-green-600 dark:text-green-500' : 'text-amber-600 dark:text-amber-400'}`}>
                      {tickAge(row)} · {row.tick_time || '--'}
                    </small>
                  </TableCell>
                  <TableCell className="text-right">
                    <strong className="block text-zinc-900 tabular-nums dark:text-white">{price(row.last_price)}</strong>
                    <span className="block text-sm"><ChangeValue value={row.change_pct == null ? null : Number(row.change_pct)} /></span>
                    <small className="block text-zinc-500 tabular-nums dark:text-zinc-400">区间 {price(row.low)} - {price(row.high)}</small>
                    <small className="block text-zinc-500 tabular-nums dark:text-zinc-400">当日 VWAP {price(row.vwap_5m)}</small>
                  </TableCell>
                  <TableCell className="text-right">
                    <span className={`block tabular-nums ${rsiClass(row.rsi_5m_14)}`}>5m {rsi(row.rsi_5m_14)}</span>
                    <small className="block text-zinc-500 dark:text-zinc-400">{rsiState(row.rsi_5m_14)}</small>
                    <span className={`block tabular-nums ${rsiClass(row.rsi_15m_14)}`}>15m {rsi(row.rsi_15m_14)}</span>
                    <small className="block text-zinc-500 dark:text-zinc-400">{rsiState(row.rsi_15m_14)}</small>
                  </TableCell>
                  <TableCell className="text-right">
                    <span className="block tabular-nums">{price(row.bid1)} / {price(row.ask1)}</span>
                    <small className="block">五档 <ChangeValue value={row.order_book_imbalance == null ? null : Number(row.order_book_imbalance)} digits={0} /></small>
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge color={tagColorOf(row.level || 'info')}>{row.diagnosis || '行情缺失'}</Badge>
                      <Badge color={tagColorOf(suggestion.type)}>{suggestion.label}</Badge>
                      {isTracked(row.code) ? (
                        <Button outline onClick={(e: any) => { e?.stopPropagation?.(); stopTracking(row.code) }}>停止跟踪</Button>
                      ) : (
                        <Button outline disabled={!row.available} onClick={(e: any) => { e?.stopPropagation?.(); startTracking(row.code) }}>持续跟踪</Button>
                      )}
                      <Button plain onClick={(e: any) => { e?.stopPropagation?.(); openChart(row) }}>查看K线</Button>
                    </div>
                    <small className="mt-1 block max-w-[380px] leading-snug text-zinc-500 dark:text-zinc-400">{suggestion.detail}</small>
                    {row.anomalies?.length > 0 && (
                      <small className="mt-1 block max-w-[380px] leading-snug text-amber-600 dark:text-amber-400">{row.anomalies.join('；')}</small>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Card>

      <Card className="mb-4">
        <SectionTitle
          title="模拟做T记录"
          actions={<Button color="blue" onClick={() => saveSimRecords()}>保存全部</Button>}
        />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">点击“编辑”后填写模拟价格和数量，再点击该行“保存”。</p>
        <Table dense>
          <TableHead>
            <TableRow>
              {['确认时间', '标的', '方向', '仓位', '模拟买卖价', '数量', '确认后仓位', '操作'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {events.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-zinc-500 dark:text-zinc-400">暂无模拟做T记录</TableCell>
              </TableRow>
            )}
            {events.map((row, index) => {
              const edit = simEdits[index] || {}
              return (
                <TableRow key={`${row.at}-${index}`}>
                  <TableCell>{row.at}</TableCell>
                  <TableCell>{row.code}</TableCell>
                  <TableCell>{row.side}</TableCell>
                  <TableCell className="tabular-nums">{row.weight_pct}%</TableCell>
                  <TableCell>
                    {edit.editing ? (
                      <div className="w-32">
                        <Input type="number" min={0} step={0.001} value={edit.price ?? row.price ?? ''} onChange={(e) => setSimEdits({ ...simEdits, [index]: { ...edit, price: e.target.value === '' ? null : Number(e.target.value) } })} />
                      </div>
                    ) : (row.price ?? '--')}
                  </TableCell>
                  <TableCell>
                    {edit.editing ? (
                      <div className="w-28">
                        <Input type="number" min={0} step={100} value={edit.shares ?? row.shares ?? ''} onChange={(e) => setSimEdits({ ...simEdits, [index]: { ...edit, shares: e.target.value === '' ? null : Number(e.target.value) } })} />
                      </div>
                    ) : (row.shares ?? '--')}
                  </TableCell>
                  <TableCell className="tabular-nums">{row.after_weight_pct}%</TableCell>
                  <TableCell>
                    {edit.editing ? (
                      <Button color="blue" onClick={() => saveSimRecord(row, index)}>保存</Button>
                    ) : (
                      <Button plain onClick={() => setSimEdits({ ...simEdits, [index]: { editing: true, price: row.price ?? null, shares: row.shares ?? null } })}>编辑</Button>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Card>

      <Card className="mb-4">
        <SectionTitle title="板块跟随确认" />
        <p className="mb-3 text-sm text-zinc-500 dark:text-zinc-400">从 QMT 同步的行业成分中，按最近成交额抽样至多 10 只同板块股票。至少 4 只可用行情才给出结论；它只用于确认个股走势，不产生买卖委托。</p>
        <Table dense striped>
          <TableHead>
            <TableRow>
              {['持仓', '行业 / 样本', '板块扩散', '样本中位涨跌', '个股相对强弱', '跟随结论', '强势样本'].map((h) => <TableHeader key={h}>{h}</TableHeader>)}
            </TableRow>
          </TableHead>
          <TableBody>
            {items.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-zinc-500 dark:text-zinc-400">暂无板块跟随数据</TableCell>
              </TableRow>
            )}
            {items.map((row) => (
              <TableRow key={row.code}>
                <TableCell>
                  <strong className="block text-zinc-900 dark:text-white">{row.name || row.code}</strong>
                  <small className="block text-zinc-500 dark:text-zinc-400">{row.code}</small>
                </TableCell>
                <TableCell>
                  <span>{row.sector_name || '--'}</span>
                  <small className="block text-zinc-500 dark:text-zinc-400">抽样 {row.peer_sample_size || 0} / 全板块 {row.sector_member_count || '--'} 只</small>
                </TableCell>
                <TableCell className="text-right tabular-nums">{sectorBreadth(row)}</TableCell>
                <TableCell className="text-right tabular-nums"><ChangeValue value={row.peer_median_change_pct == null ? null : Number(row.peer_median_change_pct)} /></TableCell>
                <TableCell className="text-right"><ChangeValue value={row.relative_strength_pct == null ? null : Number(row.relative_strength_pct)} /></TableCell>
                <TableCell>
                  <Badge color={sectorTagColor(row.sector_follow_state)}>{row.sector_follow_state || '未映射'}</Badge>
                  <small className="block text-zinc-500 dark:text-zinc-400">{row.sector_follow_label}</small>
                </TableCell>
                <TableCell className="max-w-[240px] truncate">{peerLeaders(row)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Card>

      <Dialog open={chartOpen} onClose={() => setChartOpen(false)} size="5xl">
        <DialogTitle>{selectedChartRow?.name || selectedChartRow?.code || ''} 多周期K线</DialogTitle>
        <DialogBody>
          {selectedChartRow && (
            <Alert color={selectedChartRow.daily_trend?.intraday_trend_broken ? 'red' : 'zinc'} className="text-sm">
              {trendText(selectedChartRow)}
            </Alert>
          )}
          {chartLoading && <PageLoading text="多周期K线加载中…" />}
          {!chartLoading && chartReady && (
            <div className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-2">
              {CHART_PERIODS.map((period) => (
                <EChart key={period} option={chartOptions[period]} height={390} />
              ))}
            </div>
          )}
        </DialogBody>
        <DialogActions>
          <Button plain onClick={() => setChartOpen(false)}>关闭</Button>
        </DialogActions>
      </Dialog>
    </div>
  )
}

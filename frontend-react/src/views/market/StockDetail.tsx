import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, EmptyState, PageHeader, PageLoading, SectionTitle, StatCard } from '@/components/ui'
import LocalKLineChart, {
  type KLineDataItem,
  type RangeEdgePayload,
  type TradeMarkerItem,
} from '@/components/LocalKLineChart'
import request from '@/utils/request'

interface StockInfo {
  code: string
  name: string
  market?: string
  type?: string
  industry?: string
  region?: string
  list_date?: string
  status?: string
}

interface KlineStats {
  total_count?: number
  periods_with_data?: number
  periods?: Record<string, { count: number; table?: string }>
}

interface KlineResponse {
  data?: KLineDataItem[]
}

interface StockBoard {
  code: string
  name: string
  type?: string
}

interface SyncResponse {
  success?: boolean
  message?: string
}

interface NavigationResponse {
  previous?: { code?: string } | null
  next?: { code?: string } | null
}

const PERIODS = [
  { label: '1分钟', value: '1m' },
  { label: '15分钟', value: '15m' },
  { label: '30分钟', value: '30m' },
  { label: '60分钟', value: '60m' },
  { label: '日K', value: '1d' },
  { label: '周K', value: '1w' },
  { label: '月K', value: '1mon' },
  { label: '季K', value: '1q' },
]

const PERIOD_LABEL_MAP: Record<string, string> = Object.fromEntries(
  PERIODS.map((item) => [item.value, item.label]),
)

const MINUTE_PERIODS = ['1m', '15m', '30m', '60m']
const TRADE_MARKS_STORAGE_PREFIX = 'stock_detail_trade_marks_'

function defaultLimitFor(period: string): number {
  return MINUTE_PERIODS.includes(period) ? 240 : 100
}

function toFiniteNumber(value: unknown): number | null {
  const num = Number(value)
  return Number.isFinite(num) ? num : null
}

function formatPrice(value: unknown): string {
  const num = toFiniteNumber(value)
  return num === null ? '-' : num.toFixed(2)
}

function formatPercent(value: unknown): string {
  const num = toFiniteNumber(value)
  return num === null ? '-' : `${num.toFixed(2)}%`
}

function formatNumber(value: unknown): string {
  const num = toFiniteNumber(value)
  if (num === null) return '-'
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(2)}万`
  return num.toFixed(2)
}

function formatDate(value: unknown): string {
  if (!value) return '-'
  if (typeof value === 'string') return value.substring(0, 10)
  return new Date(value as string).toISOString().substring(0, 10)
}

function formatTradeDate(value: unknown): string {
  if (!value) return '-'
  const text = String(value).trim()
  if (text.includes('T')) return text.split('T')[0]
  if (text.includes(' ')) return text.split(' ')[0]
  return text.slice(0, 10)
}

function getMarketLabel(market?: string): string {
  const labels: Record<string, string> = { sh: '上海', sz: '深圳', bj: '北京' }
  return labels[market || ''] || market || '-'
}

function getTypeLabel(type?: string): string {
  const labels: Record<string, string> = { stock: '股票', index: '指数', industry: '行业', sector: '板块' }
  return labels[type || ''] || type || '-'
}

function getStatusLabel(status?: string): string {
  const labels: Record<string, string> = { active: '正常', delisted: '退市', unknown: '未知' }
  return labels[status || ''] || status || '未知'
}

function normalizeTradeMarkerCode(value: unknown): string {
  const text = String(value || '').trim().toUpperCase()
  if (!text) return ''
  return text.includes('.') ? text.split('.')[0] : text
}

const UP_CLASS = 'text-red-600 dark:text-red-400'
const DOWN_CLASS = 'text-green-600 dark:text-green-500'

export default function StockDetail() {
  const { code: routeCode } = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const code = routeCode || ''

  // —— 状态（与旧版一一对应）——
  const [loading, setLoading] = useState(false)
  const [stockInfo, setStockInfo] = useState<StockInfo | null>(null)
  const [loadingNav, setLoadingNav] = useState(false)
  const [klineStatsLoading, setKlineStatsLoading] = useState(false)
  const [klineStats, setKlineStats] = useState<KlineStats | null>(null)
  const [chartLoading, setChartLoading] = useState(false)
  const [klineData, setKlineData] = useState<KlineResponse | null>(null)
  const [selectedPeriod, setSelectedPeriod] = useState('1d')
  const [klineLimit, setKlineLimit] = useState(100)
  const [loadingMoreKline, setLoadingMoreKline] = useState(false)
  const [hasMoreHistory, setHasMoreHistory] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [syncMessage, setSyncMessage] = useState('')
  const [tradeMarkers, setTradeMarkers] = useState<TradeMarkerItem[]>([])
  const [stockBoards, setStockBoards] = useState<StockBoard[]>([])
  const [boardsLoading, setBoardsLoading] = useState(false)

  // 用 ref 保存最新值，避免异步竞态与闭包过期
  const codeRef = useRef(code)
  codeRef.current = code
  const periodRef = useRef(selectedPeriod)
  periodRef.current = selectedPeriod
  const klineLimitRef = useRef(klineLimit)
  klineLimitRef.current = klineLimit
  const hasMoreHistoryRef = useRef(hasMoreHistory)
  hasMoreHistoryRef.current = hasMoreHistory
  const loadingMoreKlineRef = useRef(loadingMoreKline)
  loadingMoreKlineRef.current = loadingMoreKline
  const chartLoadingRef = useRef(chartLoading)
  chartLoadingRef.current = chartLoading
  const klineDataRef = useRef<KlineResponse | null>(klineData)
  klineDataRef.current = klineData

  // —— 成交标记（sessionStorage，按 trade_marks_key 查询参数读取）——
  const tradeMarksKey = searchParams.get('trade_marks_key') || ''
  useEffect(() => {
    const key = String(tradeMarksKey || '').trim()
    if (!key || !key.startsWith(TRADE_MARKS_STORAGE_PREFIX)) {
      setTradeMarkers([])
      return
    }
    try {
      const parsed = JSON.parse(sessionStorage.getItem(key) || '[]')
      const currentCode = normalizeTradeMarkerCode(codeRef.current)
      const markers = (Array.isArray(parsed) ? parsed : []) as TradeMarkerItem[]
      setTradeMarkers(
        markers.filter((item) => {
          const markerCode = normalizeTradeMarkerCode((item as { code?: unknown })?.code)
          return markerCode && markerCode === currentCode
        }),
      )
    } catch (error) {
      console.error('load trade markers failed:', error)
      setTradeMarkers([])
    }
  }, [tradeMarksKey, code])

  // —— 数据加载 ——
  const loadStockInfo = useCallback(async () => {
    const targetCode = codeRef.current
    setLoading(true)
    try {
      const data = (await request.get(`/stocks/${targetCode}`)) as unknown as StockInfo
      if (targetCode !== codeRef.current) return
      setStockInfo(data || null)
    } catch (error) {
      if (targetCode !== codeRef.current) return
      console.error('加载股票信息失败:', error)
      setStockInfo(null)
    } finally {
      if (targetCode === codeRef.current) setLoading(false)
    }
  }, [])

  const loadKlineStats = useCallback(async () => {
    setKlineStatsLoading(true)
    try {
      const data = (await request.get(`/stocks/${codeRef.current}/kline-stats`)) as unknown as KlineStats
      setKlineStats(data || null)
    } catch (error) {
      console.error('加载K线统计失败:', error)
      setKlineStats(null)
    } finally {
      setKlineStatsLoading(false)
    }
  }, [])

  const loadKlineData = useCallback(async (limit?: number) => {
    const requestedLimit = limit ?? klineLimitRef.current
    chartLoadingRef.current = true
    setChartLoading(true)
    try {
      const previousCount = Array.isArray(klineDataRef.current?.data)
        ? klineDataRef.current.data.length
        : 0
      setKlineLimit(requestedLimit)
      const data = (await request.get(`/stocks/${codeRef.current}/kline/${periodRef.current}`, {
        params: { limit: requestedLimit },
      })) as unknown as KlineResponse
      setKlineData(data)
      const nextCount = Array.isArray(data?.data) ? data.data.length : 0
      setHasMoreHistory(
        nextCount >= requestedLimit || (requestedLimit > previousCount && nextCount > previousCount),
      )
    } catch (error) {
      console.error('加载K线数据失败:', error)
      setKlineData(null)
      setHasMoreHistory(false)
    } finally {
      setChartLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadStockBoards = useCallback(async () => {
    setBoardsLoading(true)
    try {
      const data = (await request.get(`/stocks/${codeRef.current}/boards`)) as unknown as { boards?: StockBoard[] }
      setStockBoards(data?.boards || [])
    } catch (error) {
      console.error('获取板块数据失败:', error)
      setStockBoards([])
    } finally {
      setBoardsLoading(false)
    }
  }, [])

  const loadMoreKlineData = useCallback(async () => {
    if (loadingMoreKlineRef.current || chartLoadingRef.current || !hasMoreHistoryRef.current) return
    setLoadingMoreKline(true)
    try {
      const step = MINUTE_PERIODS.includes(periodRef.current) ? 240 : 100
      await loadKlineData(klineLimitRef.current + step)
    } finally {
      setLoadingMoreKline(false)
    }
  }, [loadKlineData])

  const resetKlineToLatest = useCallback(() => {
    setKlineLimit(defaultLimitFor(periodRef.current))
    setHasMoreHistory(true)
    loadKlineData(defaultLimitFor(periodRef.current))
  }, [loadKlineData])

  const selectPeriod = useCallback(
    (period: string) => {
      setSelectedPeriod(period)
      periodRef.current = period
      const limit = defaultLimitFor(period)
      setKlineLimit(limit)
      klineLimitRef.current = limit
      setHasMoreHistory(true)
      hasMoreHistoryRef.current = true
      loadKlineData(limit)
    },
    [loadKlineData],
  )

  const handleChartRangeEdge = useCallback(
    (payload: RangeEdgePayload) => {
      if (payload.side === 'left') {
        loadMoreKlineData()
      }
    },
    [loadMoreKlineData],
  )

  // —— 键盘：← 加载更多历史，→ 回到最新 ——
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const tagName = String((event.target as HTMLElement | null)?.tagName || '').toLowerCase()
      if (['input', 'textarea', 'select'].includes(tagName)) return
      if (event.key === 'ArrowLeft') {
        loadMoreKlineData()
      } else if (event.key === 'ArrowRight') {
        resetKlineToLatest()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [loadMoreKlineData, resetKlineToLatest])

  // —— 挂载 / 路由 code 变化时加载全部数据 ——
  useEffect(() => {
    if (!code) return
    setKlineData(null)
    setHasMoreHistory(true)
    loadStockInfo()
    loadKlineStats()
    loadKlineData()
    loadStockBoards()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code])

  // —— 同步数据 ——
  const syncData = useCallback(async () => {
    if (!code) return
    setSyncing(true)
    setSyncMessage('正在同步数据...')
    try {
      const data = (await request.post(`/stocks/${code}/sync`)) as unknown as SyncResponse
      if (data?.success) {
        setSyncMessage(`同步完成！${data.message || ''}`)
        await loadKlineStats()
        await loadKlineData()
        setTimeout(() => setSyncMessage(''), 3000)
      } else {
        setSyncMessage(`同步失败：${data?.message || '未知错误'}`)
      }
    } catch (error) {
      console.error('同步数据失败:', error)
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (error as Error)?.message ||
        '网络错误'
      setSyncMessage(`同步失败：${detail}`)
      setTimeout(() => setSyncMessage(''), 5000)
    } finally {
      setSyncing(false)
    }
  }, [code, loadKlineStats, loadKlineData])

  // —— 上一个 / 下一个股票 ——
  const navigateToAdjacent = useCallback(
    async (direction: 'previous' | 'next') => {
      if (loadingNav || !code) return
      setLoadingNav(true)
      try {
        const data = (await request.get(`/stocks/${code}/navigation`)) as unknown as NavigationResponse
        const target = data?.[direction]
        if (target?.code) navigate(`/stock/${target.code}`)
      } catch (error) {
        console.error('导航失败:', error)
      } finally {
        setLoadingNav(false)
      }
    },
    [code, loadingNav, navigate],
  )

  const goBack = useCallback(() => navigate(-1), [navigate])

  const bars = klineData?.data || []
  const recentBars = useMemo(() => [...bars].slice(-20).reverse(), [bars])
  const periodEntries = useMemo(
    () => Object.entries(klineStats?.periods || {}) as [string, { count: number; table?: string }][],
    [klineStats],
  )

  const syncMessageTone = syncMessage.includes('完成')
    ? 'success'
    : syncMessage.includes('失败')
      ? 'error'
      : 'info'

  return (
    <div className="min-h-[calc(100vh-60px)] bg-zinc-50 p-5 dark:bg-zinc-950">
      {/* 侧边导航按钮 */}
      <div className="fixed top-1/2 right-8 z-[100] hidden -tranzinc-y-1/2 flex-col gap-4 xl:flex">
        <button
          type="button"
          onClick={() => navigateToAdjacent('previous')}
          disabled={loadingNav}
          aria-label="上一只股票"
          className="flex size-14 cursor-pointer items-center justify-center rounded-full border border-indigo-400/40 bg-white text-2xl font-bold text-indigo-500 shadow-lg transition-transform hover:-tranzinc-y-1 hover:bg-indigo-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-40 dark:bg-zinc-800 dark:text-indigo-300"
        >
          ↑
        </button>
        <button
          type="button"
          onClick={() => navigateToAdjacent('next')}
          disabled={loadingNav}
          aria-label="下一只股票"
          className="flex size-14 cursor-pointer items-center justify-center rounded-full border border-indigo-400/40 bg-white text-2xl font-bold text-indigo-500 shadow-lg transition-transform hover:tranzinc-y-1 hover:bg-indigo-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-40 dark:bg-zinc-800 dark:text-indigo-300"
        >
          ↓
        </button>
      </div>

      <div className="mx-auto max-w-[1400px]">
        {/* 页头 */}
        <PageHeader
          title={stockInfo ? `${stockInfo.name} (${stockInfo.code})` : `股票 ${code}`}
          subtitle={
            loading
              ? '加载中…'
              : stockInfo
                ? `${getMarketLabel(stockInfo.market)} · ${getTypeLabel(stockInfo.type)}`
                : '股票详情'
          }
          actions={
            <Button outline onClick={goBack}>
              ← 返回
            </Button>
          }
        />

        {/* 股票基本信息 */}
        <Card className="mb-5">
          {loading ? (
            <PageLoading />
          ) : stockInfo ? (
            <div>
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-3">
                  <Badge color={stockInfo.market === 'sh' ? 'blue' : stockInfo.market === 'sz' ? 'purple' : 'orange'}>
                    {getMarketLabel(stockInfo.market)}
                  </Badge>
                  <Badge color="emerald">{getTypeLabel(stockInfo.type)}</Badge>
                </div>
                <Button color="green" disabled={syncing} onClick={syncData}>
                  {syncing ? '同步中...' : '同步数据'}
                </Button>
              </div>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-4">
                <div className="flex items-center gap-2 text-[15px] text-zinc-700 dark:text-zinc-200">
                  <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">行业:</span>
                  {stockInfo.industry || '-'}
                </div>
                <div className="flex items-center gap-2 text-[15px] text-zinc-700 dark:text-zinc-200">
                  <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">地区:</span>
                  {stockInfo.region || '-'}
                </div>
                <div className="flex items-center gap-2 text-[15px] text-zinc-700 dark:text-zinc-200">
                  <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">上市日期:</span>
                  {formatDate(stockInfo.list_date)}
                </div>
                <div className="flex items-center gap-2 text-[15px] text-zinc-700 dark:text-zinc-200">
                  <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">状态:</span>
                  <span
                    className={`rounded px-2 py-0.5 font-medium ${
                      stockInfo.status === 'active'
                        ? 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-400'
                        : stockInfo.status === 'delisted'
                          ? 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-400'
                          : 'bg-zinc-100 text-zinc-500 dark:bg-white/5 dark:text-zinc-400'
                    }`}
                  >
                    {getStatusLabel(stockInfo.status)}
                  </span>
                </div>
              </div>

              {/* 所属板块 */}
              {stockBoards.length > 0 && (
                <div className="mt-4 border-t border-zinc-200 pt-4 dark:border-zinc-700">
                  <div className="mb-3 flex items-center justify-between">
                    <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">所属板块:</span>
                    <Button plain disabled={boardsLoading} onClick={loadStockBoards} className="text-xs">
                      {boardsLoading ? '加载中...' : '刷新'}
                    </Button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {stockBoards.map((board) => (
                      <Badge
                        key={board.code}
                        color={board.type === 'concept' ? 'indigo' : board.type === 'industry' ? 'rose' : 'zinc'}
                      >
                        {board.name}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <EmptyState text="股票信息不存在" />
          )}
        </Card>

        {/* K线数据统计 */}
        <Card className="mb-5">
          <SectionTitle
            title="K线数据统计"
            actions={
              <Button color="indigo" onClick={loadKlineStats}>
                刷新
              </Button>
            }
          />
          {klineStatsLoading ? (
            <PageLoading />
          ) : syncMessage ? (
            <div
              className={`mb-5 rounded-lg px-4 py-4 text-center text-sm font-medium ${
                syncMessageTone === 'success'
                  ? 'border border-green-500 bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-400'
                  : syncMessageTone === 'error'
                    ? 'border border-red-500 bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400'
                    : 'border border-blue-400 bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
              }`}
            >
              {syncMessage}
            </div>
          ) : klineStats ? (
            <div>
              <div className="mb-6 flex flex-col gap-4 sm:flex-row">
                <StatCard
                  label="总数据条数"
                  value={klineStats.total_count || 0}
                  tone="info"
                  className="flex-1"
                />
                <StatCard
                  label="有数据的周期"
                  value={`${klineStats.periods_with_data || 0} / ${periodEntries.length}`}
                  tone="info"
                  className="flex-1"
                />
              </div>
              <SectionTitle title="各周期数据量" />
              <div className="grid grid-cols-[repeat(auto-fit,minmax(100px,1fr))] gap-3">
                {periodEntries.map(([period, stat]) => (
                  <div
                    key={period}
                    className={`rounded-xl border-2 p-4 text-center transition-colors ${
                      stat.count > 0
                        ? 'border-green-500 bg-green-50 dark:bg-green-500/10'
                        : 'border-red-500 bg-red-50 opacity-60 dark:bg-red-500/10'
                    }`}
                  >
                    <div className="mb-2 text-lg font-bold text-zinc-800 dark:text-zinc-100">{period}</div>
                    <div
                      className={`mb-1 text-2xl font-bold tabular-nums ${
                        stat.count > 0 ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                      }`}
                    >
                      {stat.count}
                    </div>
                    <div className="text-xs text-zinc-500 dark:text-zinc-400">{stat.table}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </Card>

        {/* K线图表 */}
        <Card className="mb-5">
          <SectionTitle
            title="K线图表"
            actions={
              <div className="flex flex-wrap justify-end gap-2">
                {PERIODS.map((period) => (
                  <button
                    key={period.value}
                    type="button"
                    onClick={() => selectPeriod(period.value)}
                    className={`cursor-pointer rounded-md border px-4 py-2 text-[13px] font-medium transition-colors ${
                      selectedPeriod === period.value
                        ? 'border-indigo-500 bg-indigo-600 text-white'
                        : 'border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700'
                    }`}
                  >
                    {period.label}
                  </button>
                ))}
              </div>
            }
          />
          {chartLoading ? (
            <PageLoading />
          ) : (
            <LocalKLineChart
              data={bars}
              tradeMarkers={tradeMarkers}
              interval={selectedPeriod}
              title={`${stockInfo?.name || code} ${PERIOD_LABEL_MAP[selectedPeriod] || selectedPeriod}K线`}
              height={820}
              onRangeEdge={handleChartRangeEdge}
            />
          )}
          {!chartLoading && bars.length > 0 && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-zinc-200 pt-4 text-sm text-zinc-500 dark:border-zinc-700 dark:text-zinc-400">
              <span>
                {hasMoreHistory
                  ? `显示 ${bars.length} 条数据，按 ← 加载更多历史，按 → 回到最新`
                  : `当前周期共 ${bars.length} 条数据，已经到最早历史`}
              </span>
              <Button outline onClick={() => loadKlineData()} className="text-xs">
                刷新
              </Button>
            </div>
          )}
          {!chartLoading && bars.length === 0 && (
            <EmptyState text="暂无K线数据，点击“同步数据”按钮获取数据" />
          )}
        </Card>

        {/* 历史数据表格 */}
        <Card className="mb-5">
          <SectionTitle title="历史数据 (最近20条)" />
          {chartLoading ? (
            <PageLoading />
          ) : recentBars.length === 0 ? (
            <EmptyState text="暂无数据" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr className="bg-zinc-50 dark:bg-zinc-800/60">
                    {['日期', '开盘', '最高', '最低', '收盘', '成交量', '成交额', '涨跌幅'].map((header) => (
                      <th
                        key={header}
                        className="px-3 py-3 text-left text-sm font-semibold text-zinc-800 dark:text-zinc-100"
                      >
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {recentBars.map((item, index) => {
                    const changePct = Number(item.change_pct)
                    const openNum = toFiniteNumber(item.open)
                    const closeNum = toFiniteNumber(item.close)
                    const inferredPct =
                      openNum && closeNum ? ((closeNum - openNum) / openNum) * 100 : null
                    const closeClass = changePct > 0 ? UP_CLASS : changePct < 0 ? DOWN_CLASS : ''
                    const pctUp =
                      (Number.isFinite(changePct) && changePct > 0) ||
                      (inferredPct !== null && inferredPct > 0)
                    const pctDown =
                      (Number.isFinite(changePct) && changePct < 0) ||
                      (inferredPct !== null && inferredPct < 0)
                    return (
                      <tr
                        key={`${item.date || item.datetime}-${index}`}
                        className="border-b border-zinc-200 hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-800/40"
                      >
                        <td className="px-3 py-3 text-sm text-zinc-600 dark:text-zinc-300">
                          {formatTradeDate(item.date || item.datetime)}
                        </td>
                        <td className="px-3 py-3 text-sm tabular-nums text-zinc-600 dark:text-zinc-300">
                          {formatPrice(item.open)}
                        </td>
                        <td className="px-3 py-3 text-sm tabular-nums text-zinc-600 dark:text-zinc-300">
                          {formatPrice(item.high)}
                        </td>
                        <td className="px-3 py-3 text-sm tabular-nums text-zinc-600 dark:text-zinc-300">
                          {formatPrice(item.low)}
                        </td>
                        <td
                          className={`px-3 py-3 text-sm tabular-nums ${closeClass || 'text-zinc-600 dark:text-zinc-300'}`}
                        >
                          {formatPrice(item.close)}
                        </td>
                        <td className="px-3 py-3 text-sm tabular-nums text-zinc-600 dark:text-zinc-300">
                          {formatNumber(item.volume)}
                        </td>
                        <td className="px-3 py-3 text-sm tabular-nums text-zinc-600 dark:text-zinc-300">
                          {formatNumber(item.amount)}
                        </td>
                        <td
                          className={`px-3 py-3 text-sm tabular-nums ${
                            pctUp ? UP_CLASS : pctDown ? DOWN_CLASS : 'text-zinc-600 dark:text-zinc-300'
                          }`}
                        >
                          {item.change_pct !== undefined && item.change_pct !== null && changePct !== 0 ? (
                            formatPercent(item.change_pct)
                          ) : openNum && closeNum ? (
                            `${inferredPct!.toFixed(2)}%`
                          ) : (
                            '-'
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}

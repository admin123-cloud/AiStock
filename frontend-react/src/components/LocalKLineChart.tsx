import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type BusinessDay,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  type MouseEventParams,
} from 'lightweight-charts'

/** 日线及以上周期使用 BusinessDay，分钟线使用 Unix 时间戳（秒） */
type BarTime = number | BusinessDay

export interface KLineDataItem {
  date?: string | number
  datetime?: string | number
  time?: string | number
  chartTime?: number
  unix_time?: number
  open: number | string
  high: number | string
  low: number | string
  close: number | string
  volume?: number | string
  amount?: number | string
  is_provisional?: boolean
  as_of?: string
  snapshot_at?: string
  [key: string]: unknown
}

export interface TradeMarkerItem {
  time?: string | number
  side?: string
  shares?: number | string
  price?: number | string
  reason?: string
  [key: string]: unknown
}

export interface TrendLinePoint {
  time?: string | number
  value: number | string
  [key: string]: unknown
}

export interface RangeEdgePayload {
  side: string
  reason: string
}

export interface LocalKLineChartProps {
  data?: KLineDataItem[]
  tradeMarkers?: TradeMarkerItem[]
  trendLine?: TrendLinePoint[]
  title?: string
  interval?: string
  height?: number
  compact?: boolean
  /** 对应旧组件的 range-edge 事件 */
  onRangeEdge?: (payload: RangeEdgePayload) => void
}

interface NormalizedBar {
  time: BarTime
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
  isProvisional: boolean
  asOf: string
  color: string
  label: string
}

interface MarkerTrade {
  side: string
  time: string
  timeText: string
  shares: number
  sharesText: string
  price: number | null
  priceText: string
  reasonText: string
  tone: 'buy' | 'sell'
}

interface MarkerGroup {
  key: string
  time: BarTime
  label: string
  buyCount: number
  sellCount: number
  buyShares: number
  sellShares: number
  trades: MarkerTrade[]
  summary: string
}

const DAILY_LIKE = ['1d', '1w', '1mon', '1q']

const INTERVAL_LABELS: Record<string, string> = {
  '1m': '1分钟',
  '15m': '15分钟',
  '30m': '30分钟',
  '1w': '周线',
  '60m': '60分钟',
  '1d': '日线',
  '1mon': '月线',
  '1q': '季线',
}

function parseChartTime(value: unknown, interval: string): BarTime | null {
  if (!value) return null
  const text = String(value).trim()
  if (!text) return null

  // 日线及以上周期可能是日期或零点 ISO 文本，统一用 BusinessDay：
  // lightweight-charts 不允许同一条系列里混用 BusinessDay 与 Unix 时间戳
  const dateMatch = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$/)
  if (DAILY_LIKE.includes(interval) && dateMatch) {
    const [, year, month, day] = dateMatch
    return { year: Number(year), month: Number(month), day: Number(day) }
  }

  // 不依赖浏览器对 "2026-07-08 13:25:00" 这类非标准格式的解析
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/)
  if (match) {
    const [, year, month, day, hour, minute, second = '0'] = match
    return Math.floor(
      Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)) / 1000,
    )
  }

  return null
}

function formatLabel(value: unknown, interval: string): string {
  const text = String(value || '').replace('T', ' ')
  if (!text) return '-'
  if (DAILY_LIKE.includes(interval)) {
    return text.slice(0, 10)
  }
  return text.length >= 16 ? text.slice(0, 16) : text
}

function formatPrice(value: unknown): string {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toFixed(2)
}

function formatLargeNumber(value: unknown): string {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  if (Math.abs(num) >= 100000000) return `${(num / 100000000).toFixed(2)}亿`
  if (Math.abs(num) >= 10000) return `${(num / 10000).toFixed(2)}万`
  return num.toFixed(2)
}

function serializeBarTime(time: BarTime, interval: string): string {
  if (time === null || time === undefined) return ''
  if (DAILY_LIKE.includes(interval) && typeof time === 'object') {
    const year = String(time.year || '').padStart(4, '0')
    const month = String(time.month || '').padStart(2, '0')
    const day = String(time.day || '').padStart(2, '0')
    return `${year}-${month}-${day}`
  }
  return String(time)
}

function normalizeMarkerTime(value: unknown, interval: string): BarTime | null {
  if (!value) return null
  const text = String(value).trim().replace(' ', 'T')
  if (!text) return null

  if (DAILY_LIKE.includes(interval)) {
    const dateText = text.slice(0, 10)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateText)) return null
    const [year, month, day] = dateText.split('-').map(Number)
    return { year, month, day }
  }

  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return null
  date.setSeconds(0, 0)
  return Math.floor(date.getTime() / 1000)
}

function normalizeMarkerLabel(value: unknown, interval: string): string {
  const text = String(value || '').trim().replace('T', ' ')
  if (!text) return ''
  if (DAILY_LIKE.includes(interval)) {
    return text.slice(0, 10)
  }
  return text.length >= 16 ? text.slice(0, 16) : text
}

function findMarkerBarTime(marker: TradeMarkerItem | undefined, interval: string, bars: NormalizedBar[]): BarTime | null {
  const normalized = normalizeMarkerTime(marker?.time, interval)
  if (normalized === null) return null
  const label = normalizeMarkerLabel(marker?.time, interval)

  if (DAILY_LIKE.includes(interval)) {
    const hit = bars.find((bar) => String(bar.label || '').slice(0, 10) === label)
    return hit?.time ?? normalized
  }

  const exact = bars.find((bar) => bar.time === normalized)
  if (exact) return exact.time
  const sameMinute = bars.find((bar) => String(bar.label || '') === label)
  return sameMinute?.time ?? normalized
}

function compareBarTime(left: BarTime, right: BarTime, interval: string): number {
  if (typeof left === 'number' && typeof right === 'number') return left - right
  return serializeBarTime(left, interval).localeCompare(serializeBarTime(right, interval))
}

function groupTradeMarkers(markers: TradeMarkerItem[], interval: string, bars: NormalizedBar[]): MarkerGroup[] {
  const source = Array.isArray(markers) ? markers : []
  const grouped = new Map<string, Omit<MarkerGroup, 'summary'>>()
  source.forEach((item) => {
    const time = findMarkerBarTime(item, interval, bars)
    if (!time) return
    const key = serializeBarTime(time, interval)
    const side = String(item?.side || '').trim()
    const isSell = side.includes('卖')
    const shares = Number(item?.shares || 0)
    const price = Number(item?.price)
    if (!grouped.has(key)) {
      grouped.set(key, {
        key,
        time,
        label: normalizeMarkerLabel(item?.time, interval),
        buyCount: 0,
        sellCount: 0,
        buyShares: 0,
        sellShares: 0,
        trades: [],
      })
    }
    const group = grouped.get(key)!
    if (isSell) {
      group.sellCount += 1
      group.sellShares += Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0
    } else {
      group.buyCount += 1
      group.buyShares += Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0
    }
    group.trades.push({
      side: side || (isSell ? '卖出' : '买入'),
      time: String(item?.time || ''),
      timeText: normalizeMarkerLabel(item?.time, interval),
      shares: Number.isFinite(shares) ? Math.max(0, Math.trunc(shares)) : 0,
      sharesText: Number.isFinite(shares) && shares > 0 ? `${Math.trunc(shares)}股` : '--',
      price: Number.isFinite(price) ? price : null,
      priceText: Number.isFinite(price) && price > 0 ? `@${price.toFixed(2)}` : '@--',
      reasonText: String(item?.reason || '').trim(),
      tone: isSell ? 'sell' : 'buy',
    })
  })
  return [...grouped.values()]
    .map((group) => {
      const parts: string[] = []
      if (group.buyCount > 0) parts.push(`B${group.buyCount}/${group.buyShares}`)
      if (group.sellCount > 0) parts.push(`S${group.sellCount}/${group.sellShares}`)
      return { ...group, summary: parts.join('  ') }
    })
    .sort((a, b) => {
      const aTime = typeof a.time === 'number' ? a.time : Date.parse(`${serializeBarTime(a.time, interval)}T00:00:00`)
      const bTime = typeof b.time === 'number' ? b.time : Date.parse(`${serializeBarTime(b.time, interval)}T00:00:00`)
      return aTime - bTime
    })
}

function buildTradeMarkers(groups: MarkerGroup[]): SeriesMarker<Time>[] {
  return (Array.isArray(groups) ? groups : []).map((group) => {
    const hasSell = group.sellCount > 0
    const hasBuy = group.buyCount > 0
    let markerText = ''
    if (hasBuy) markerText += `B${group.buyCount}`
    if (hasSell) markerText += `${markerText ? '/' : ''}S${group.sellCount}`
    return {
      time: group.time as Time,
      position: hasSell ? 'aboveBar' : 'belowBar',
      color: hasSell ? '#16a34a' : '#d4380d',
      shape: hasSell ? 'arrowDown' : 'arrowUp',
      text: markerText || 'T',
    }
  })
}

function buildBars(data: KLineDataItem[], interval: string): NormalizedBar[] {
  const normalized = (Array.isArray(data) ? data : [])
    .map((item): NormalizedBar | null => {
      const label = (item.date || item.datetime || item.time) as string | number | undefined
      const explicitTime = Number(item.chartTime ?? item.unix_time)
      const time =
        !DAILY_LIKE.includes(interval) && Number.isFinite(explicitTime) && explicitTime > 0
          ? explicitTime
          : parseChartTime(label, interval)
      const open = Number(item.open)
      const high = Number(item.high)
      const low = Number(item.low)
      const close = Number(item.close)
      const volume = Number(item.volume || 0)
      const amount = Number(item.amount || 0)

      if (!time || ![open, high, low, close].every(Number.isFinite)) {
        return null
      }

      return {
        time,
        open,
        high,
        low,
        close,
        volume,
        amount,
        isProvisional: Boolean(item.is_provisional),
        asOf: (item.as_of as string) || (item.snapshot_at as string) || '',
        color: close >= open ? '#ff4d4f' : '#22c55e',
        label: formatLabel(label, interval),
      }
    })
    .filter((bar): bar is NormalizedBar => bar !== null)

  // lightweight-charts 要求严格递增且唯一的时间戳；增量刷新可能带来重复的
  // 最新一根 bar，保留后到的值并保持图表可渲染。
  const byTime = new Map<string, NormalizedBar>()
  normalized
    .sort((left, right) => compareBarTime(left.time, right.time, interval))
    .forEach((bar) => byTime.set(serializeBarTime(bar.time, interval), bar))

  return [...byTime.values()].sort((left, right) => compareBarTime(left.time, right.time, interval))
}

interface MovingAveragePoint {
  time: BarTime
  value: number
}

function createMovingAverage(data: NormalizedBar[], period: number, valueKey: 'close' | 'volume' | 'amount' = 'close'): MovingAveragePoint[] {
  const result: MovingAveragePoint[] = []
  let sum = 0

  data.forEach((item, index) => {
    sum += Number(item[valueKey] || 0)
    if (index >= period) {
      sum -= Number(data[index - period][valueKey] || 0)
    }
    if (index >= period - 1) {
      result.push({
        time: item.time,
        value: Number((sum / period).toFixed(2)),
      })
    }
  })

  return result
}

function buildTrendLineData(points: TrendLinePoint[], interval: string): MovingAveragePoint[] {
  const byTime = new Map<string, MovingAveragePoint>()
  ;(Array.isArray(points) ? points : []).forEach((item) => {
    const time = parseChartTime(item?.time, interval)
    const value = Number(item?.value)
    if (!time || !Number.isFinite(value)) return
    // 日线源在增量刷新后可能包含重复的最新交易日，线系列与 K 线一样
    // 要求时间严格递增。
    byTime.set(serializeBarTime(time, interval), { time, value })
  })
  return [...byTime.values()].sort((left, right) => compareBarTime(left.time, right.time, interval))
}

function getDefaultBarSpacing(interval: string): number {
  return ['1m', '15m', '30m', '60m'].includes(interval) ? 10 : 14
}

export default function LocalKLineChart({
  data,
  tradeMarkers,
  trendLine,
  title = 'K线图',
  interval = '1d',
  height = 760,
  compact = false,
  onRangeEdge,
}: LocalKLineChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const volumeMa5SeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const volumeMa10SeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const ma5SeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const ma10SeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const trendLineSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  const markersApiRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const lastRangeEdgeAtRef = useRef(0)
  const currentBarSpacingRef = useRef(getDefaultBarSpacing(interval))
  const onRangeEdgeRef = useRef(onRangeEdge)
  onRangeEdgeRef.current = onRangeEdge

  const [bars, setBars] = useState<NormalizedBar[]>([])
  const [legendIndex, setLegendIndex] = useState(-1)
  const [histogramMode, setHistogramMode] = useState<'amount' | 'volume'>('amount')

  // bars 的最新值，供 crosshair 回调等读取（避免闭包过期）
  const barsRef = useRef(bars)
  barsRef.current = bars

  const chartHeight = Math.max(Number(height) || 760, compact ? 300 : 520)
  const latestBar = bars.length ? bars[bars.length - 1] : null
  const activeBar = legendIndex >= 0 && bars[legendIndex] ? bars[legendIndex] : latestBar
  const intervalLabel = INTERVAL_LABELS[interval] || interval

  const markerGroups = useMemo(
    () => groupTradeMarkers(tradeMarkers ?? [], interval, bars),
    [tradeMarkers, interval, bars],
  )

  const activeMarkerGroup = useMemo(() => {
    if (!activeBar) return null
    const key = serializeBarTime(activeBar.time, interval)
    return markerGroups.find((item) => item.key === key) || null
  }, [activeBar, markerGroups, interval])

  const legendBar = useMemo(() => {
    if (!activeBar) return null
    const change = activeBar.close - activeBar.open
    const changePct = activeBar.open ? (change / activeBar.open) * 100 : 0
    return {
      ...activeBar,
      changeClass: change > 0 ? 'rise' : change < 0 ? 'fall' : 'flat',
      changeText: `${change >= 0 ? '+' : ''}${formatPrice(change)} (${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%)`,
    }
  }, [activeBar])

  const summaryStats = useMemo(() => {
    if (!latestBar) return []
    const previous = bars.length > 1 ? bars[bars.length - 2] : null
    const prevClose = previous?.close ?? latestBar.open
    const diff = latestBar.close - prevClose
    const diffPct = prevClose ? (diff / prevClose) * 100 : 0
    const amplitudeBase = latestBar.low !== 0 ? ((latestBar.high - latestBar.low) / latestBar.low) * 100 : 0

    return [
      {
        label: '最新价',
        value: formatPrice(latestBar.close),
        tone: diff > 0 ? 'rise' : diff < 0 ? 'fall' : 'flat',
      },
      {
        label: '较前收',
        value: `${diff >= 0 ? '+' : ''}${formatPrice(diff)} / ${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(2)}%`,
        tone: diff > 0 ? 'rise' : diff < 0 ? 'fall' : 'flat',
      },
      { label: '振幅', value: `${amplitudeBase.toFixed(2)}%`, tone: 'neutral' as const },
      { label: '成交量', value: formatLargeNumber(latestBar.volume), tone: 'neutral' as const },
      { label: '成交额', value: formatLargeNumber(latestBar.amount), tone: 'neutral' as const },
    ]
  }, [bars, latestBar])

  const applyBarSpacing = useCallback(
    (spacing: number) => {
      const chart = chartRef.current
      if (!chart) return
      const next = Math.max(2, Math.min(30, Number(spacing) || getDefaultBarSpacing(interval)))
      currentBarSpacingRef.current = next
      chart.applyOptions({ timeScale: { barSpacing: next } })
    },
    [interval],
  )

  const requestMoreHistory = useCallback((side = 'left', reason = 'range-change') => {
    if (!bars.length) return
    const now = Date.now()
    if (now - lastRangeEdgeAtRef.current < 800) return
    lastRangeEdgeAtRef.current = now
    onRangeEdgeRef.current?.({ side, reason })
  }, [bars.length])

  const maybeRequestMoreHistory = useCallback(
    (reason = 'range-change') => {
      const chart = chartRef.current
      if (!chart) return
      const range = chart.timeScale().getVisibleLogicalRange()
      if (!range) return
      if (typeof range.from === 'number' && range.from <= 3) {
        requestMoreHistory('left', reason)
      }
    },
    [requestMoreHistory],
  )

  const zoomIn = useCallback(() => {
    applyBarSpacing(currentBarSpacingRef.current + 2)
  }, [applyBarSpacing])

  const zoomOut = useCallback(() => {
    applyBarSpacing(currentBarSpacingRef.current - 2)
    requestAnimationFrame(() => {
      maybeRequestMoreHistory('zoom-out')
    })
  }, [applyBarSpacing, maybeRequestMoreHistory])

  const resetZoom = useCallback(() => {
    applyBarSpacing(getDefaultBarSpacing(interval))
    chartRef.current?.timeScale().fitContent()
  }, [applyBarSpacing, interval])

  const handleMouseWheel = useCallback(
    (event: React.WheelEvent<HTMLDivElement>) => {
      if (!event || event.deltaY <= 0) return
      requestAnimationFrame(() => {
        maybeRequestMoreHistory('wheel-zoom-out')
      })
    },
    [maybeRequestMoreHistory],
  )

  const handleHistogramMode = useCallback((mode: 'amount' | 'volume') => {
    setHistogramMode((prev) => (prev === mode ? prev : mode))
  }, [])

  // 图表初始化 / 销毁（interval、height 变化时重建，与旧 watch 行为一致）
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const destroyChart = () => {
      if (resizeObserverRef.current) {
        resizeObserverRef.current.disconnect()
        resizeObserverRef.current = null
      }
      if (chartRef.current) {
        chartRef.current.remove()
        chartRef.current = null
      }
      candleSeriesRef.current = null
      volumeSeriesRef.current = null
      volumeMa5SeriesRef.current = null
      volumeMa10SeriesRef.current = null
      ma5SeriesRef.current = null
      ma10SeriesRef.current = null
      trendLineSeriesRef.current = null
      markersApiRef.current = null
    }

    destroyChart()
    currentBarSpacingRef.current = getDefaultBarSpacing(interval)

    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: '#ffffff' },
        textColor: '#475569',
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: '#eef2f7' },
        horzLines: { color: '#eef2f7' },
      },
      crosshair: {
        mode: 0,
        vertLine: {
          color: '#94a3b8',
          style: 2,
          labelBackgroundColor: '#475569',
        },
        horzLine: {
          color: '#94a3b8',
          style: 2,
          labelBackgroundColor: '#475569',
        },
      },
      rightPriceScale: {
        borderColor: '#e2e8f0',
        scaleMargins: {
          top: 0.08,
          bottom: 0.22,
        },
      },
      timeScale: {
        borderColor: '#e2e8f0',
        timeVisible: !DAILY_LIKE.includes(interval),
        secondsVisible: false,
        rightOffset: 6,
        barSpacing: currentBarSpacingRef.current,
        minBarSpacing: 4,
      },
      localization: {
        locale: 'zh-CN',
      },
    })
    chartRef.current = chart

    candleSeriesRef.current = chart.addSeries(CandlestickSeries, {
      upColor: '#ff4d4f',
      downColor: '#22c55e',
      borderVisible: false,
      wickUpColor: '#ff4d4f',
      wickDownColor: '#22c55e',
      priceLineVisible: true,
      lastValueVisible: true,
    })

    volumeSeriesRef.current = chart.addSeries(
      HistogramSeries,
      {
        priceFormat: {
          type: 'volume',
        },
        lastValueVisible: false,
        priceLineVisible: false,
      },
      1,
    )
    volumeSeriesRef.current.priceScale().applyOptions({
      // 量能面板使用独立的线性自动缩放，保留普通量与放量的真实比例关系
      autoScale: true,
      mode: 0,
      scaleMargins: {
        top: 0.08,
        bottom: 0.08,
      },
    })

    volumeMa5SeriesRef.current = chart.addSeries(
      LineSeries,
      {
        color: '#facc15',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      },
      1,
    )

    volumeMa10SeriesRef.current = chart.addSeries(
      LineSeries,
      {
        color: '#e2e8f0',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      },
      1,
    )

    // 价格与成交量保持为独立面板（对齐主流终端图表），量能面板足够容纳日常换手
    chart.panes()[0]?.setStretchFactor(0.66)
    chart.panes()[1]?.setStretchFactor(0.34)

    ma5SeriesRef.current = chart.addSeries(LineSeries, {
      color: '#2563eb',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })

    ma10SeriesRef.current = chart.addSeries(LineSeries, {
      color: '#f59e0b',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    })

    trendLineSeriesRef.current = chart.addSeries(LineSeries, {
      color: '#ef4444',
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
    })

    // lightweight-charts v5 移除了 series.setMarkers，改用 createSeriesMarkers 插件
    markersApiRef.current = createSeriesMarkers(candleSeriesRef.current, [])

    chart.subscribeCrosshairMove((param: MouseEventParams<Time>) => {
      if (!param?.time || !param.point) {
        setLegendIndex(-1)
        return
      }
      if (typeof param.logical === 'number') {
        const logicalIndex = Math.round(param.logical)
        const barsNow = barsRef.current
        setLegendIndex(logicalIndex >= 0 && logicalIndex < barsNow.length ? logicalIndex : -1)
      }
    })

    const resizeObserver = new ResizeObserver(() => {
      if (!chartRef.current || !containerRef.current) return
      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
        timeScale: {
          timeVisible: !DAILY_LIKE.includes(interval),
          barSpacing: currentBarSpacingRef.current,
        },
      })
      chartRef.current.timeScale().fitContent()
    })
    resizeObserver.observe(container)
    resizeObserverRef.current = resizeObserver

    return destroyChart
  }, [interval, height])

  // 数据同步：data / trendLine / histogramMode / tradeMarkers 变化时刷新
  useEffect(() => {
    const candleSeries = candleSeriesRef.current
    const volumeSeries = volumeSeriesRef.current
    const chart = chartRef.current
    if (!chart || !candleSeries || !volumeSeries) return

    const nextBars = buildBars(data ?? [], interval)
    setBars(nextBars)
    setLegendIndex(-1)
    const histogramKey = histogramMode

    candleSeries.setData(
      nextBars.map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        open: item.open,
        high: item.high,
        low: item.low,
        close: item.close,
      })),
    )

    volumeSeries.setData(
      nextBars.map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item[histogramKey],
        color: item.color,
      })),
    )

    volumeMa5SeriesRef.current?.setData(
      createMovingAverage(nextBars, 5, histogramKey).map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item.value,
      })),
    )
    volumeMa10SeriesRef.current?.setData(
      createMovingAverage(nextBars, 10, histogramKey).map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item.value,
      })),
    )
    ma5SeriesRef.current?.setData(
      createMovingAverage(nextBars, 5).map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item.value,
      })),
    )
    ma10SeriesRef.current?.setData(
      createMovingAverage(nextBars, 10).map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item.value,
      })),
    )
    trendLineSeriesRef.current?.setData(
      buildTrendLineData(trendLine ?? [], interval).map((item) => ({
        time: item.time as UTCTimestamp | BusinessDay,
        value: item.value,
      })),
    )

    // 成交标记：lightweight-charts v5 通过 markers 插件更新（旧版 setMarkers 已移除）
    markersApiRef.current?.setMarkers(
      buildTradeMarkers(groupTradeMarkers(tradeMarkers ?? [], interval, nextBars)),
    )

    chart.timeScale().fitContent()
  }, [data, trendLine, interval, histogramMode, tradeMarkers])

  const toneTextClass = (tone: string) =>
    tone === 'rise'
      ? 'text-red-600 dark:text-red-400'
      : tone === 'fall'
        ? 'text-green-600 dark:text-green-400'
        : 'text-slate-600 dark:text-slate-300'

  const toneCardClass = (tone: string) =>
    tone === 'rise'
      ? 'border-red-400/30 bg-gradient-to-b from-red-50/95 to-white/95 dark:from-red-500/10 dark:to-transparent'
      : tone === 'fall'
        ? 'border-green-500/30 bg-gradient-to-b from-green-50/95 to-white/95 dark:from-green-500/10 dark:to-transparent'
        : 'border-blue-400/20 dark:border-blue-400/20'

  return (
    <div
      className={`kline-chart-shell relative w-full overflow-hidden rounded-2xl border border-slate-200 bg-gradient-to-b from-white to-[#f8fbff] px-[18px] pb-2 pt-[18px] dark:border-slate-700 dark:from-slate-900 dark:to-slate-900 ${
        compact ? 'rounded-xl px-3.5 pb-1.5 pt-3' : ''
      }`}
      style={{ height: `${chartHeight}px` }}
    >
      <div
        className="pointer-events-none absolute inset-0 z-0 bg-[linear-gradient(rgba(148,163,184,0.05)_1px,transparent_1px),linear-gradient(90deg,rgba(148,163,184,0.05)_1px,transparent_1px)] bg-[size:28px_28px] [mask-image:linear-gradient(180deg,rgba(0,0,0,0.35),transparent_72%)]"
        aria-hidden
      />
      <div
        className={`pointer-events-none absolute inset-0 z-0 bg-[radial-gradient(circle_at_top_right,rgba(37,99,235,0.1),transparent_28%),radial-gradient(circle_at_top_left,rgba(245,158,11,0.08),transparent_24%)]`}
        aria-hidden
      />
      <div
        className={`relative z-[1] flex items-start justify-between gap-4 ${compact ? 'gap-2 pb-1.5' : 'mb-3'}`}
      >
        <div className="min-w-0 flex-1">
          <h3
            className={`m-0 text-slate-800 dark:text-slate-100 ${compact ? 'mb-1 text-lg' : 'mb-2.5 text-[22px]'} leading-tight`}
          >
            {title}
          </h3>
          <div className={`mb-2.5 flex flex-wrap gap-2.5 ${compact ? 'gap-1.5 pb-1.5' : ''}`}>
            {summaryStats.map((item) => (
              <div
                key={item.label}
                className={`min-w-[130px] rounded-xl border bg-white/90 px-3 py-2.5 shadow-sm dark:bg-slate-800/90 ${toneCardClass(
                  item.tone,
                )} ${compact ? 'min-w-20 rounded-lg px-2 py-1.5' : ''}`}
              >
                <span className={`mb-1.5 block text-xs text-slate-500 dark:text-slate-400 ${compact ? 'mb-0.5 text-[11px]' : ''}`}>
                  {item.label}
                </span>
                <strong className={`text-base text-slate-900 tabular-nums dark:text-slate-50 ${compact ? 'text-[13px]' : ''}`}>
                  {item.value}
                </strong>
              </div>
            ))}
          </div>
          <div className={`flex flex-wrap gap-2.5 text-[13px] text-slate-500 dark:text-slate-400 ${compact ? 'gap-x-2 gap-y-0.5 text-[11px]' : 'gap-x-3.5'}`}>
            {legendBar ? (
              <>
                <span className="whitespace-nowrap font-bold text-slate-600 dark:text-slate-300">{legendBar.label}</span>
                <span className="whitespace-nowrap">开 {formatPrice(legendBar.open)}</span>
                <span className="whitespace-nowrap">高 {formatPrice(legendBar.high)}</span>
                <span className="whitespace-nowrap">低 {formatPrice(legendBar.low)}</span>
                <span className="whitespace-nowrap">收 {formatPrice(legendBar.close)}</span>
                <span className={`whitespace-nowrap ${toneTextClass(legendBar.changeClass)}`}>{legendBar.changeText}</span>
                <span className="whitespace-nowrap">量 {formatLargeNumber(legendBar.volume)}</span>
                <span className="whitespace-nowrap">额 {formatLargeNumber(legendBar.amount)}</span>
              </>
            ) : (
              <span className="text-slate-400 dark:text-slate-500">暂无可展示的K线数据</span>
            )}
          </div>
          {activeMarkerGroup && (
            <div className="mt-2.5 rounded-xl border border-slate-400/20 bg-slate-50/90 px-3 py-2.5 dark:bg-slate-800/70">
              <div className="mb-2 flex flex-wrap items-center gap-2.5 text-xs text-slate-600 dark:text-slate-300">
                <span className="font-bold text-slate-900 dark:text-slate-100">成交标记</span>
                <span className="font-semibold">{activeMarkerGroup.label}</span>
                <span className="text-slate-500 dark:text-slate-400">{activeMarkerGroup.summary}</span>
              </div>
              <div className="flex flex-col gap-1.5">
                {activeMarkerGroup.trades.map((trade, index) => (
                  <div
                    key={`${trade.side}-${trade.time}-${trade.price}-${trade.shares}-${index}`}
                    className={`flex flex-wrap gap-2.5 text-xs leading-relaxed ${
                      trade.tone === 'sell'
                        ? 'text-green-700 dark:text-green-400'
                        : 'text-[#d4380d] dark:text-red-400'
                    }`}
                  >
                    <span className="font-bold">{trade.side}</span>
                    <span>{trade.timeText}</span>
                    <span>{trade.sharesText}</span>
                    <span>{trade.priceText}</span>
                    {trade.reasonText && <span>{trade.reasonText}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className={`flex gap-2 ${compact ? 'gap-1.5' : ''}`}>
            <button
              type="button"
              className={`rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-600 transition-colors hover:bg-slate-50 hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 ${
                compact ? 'px-1.5 py-1 text-[11px]' : ''
              }`}
              onClick={zoomOut}
            >
              缩小
            </button>
            <button
              type="button"
              className={`rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-600 transition-colors hover:bg-slate-50 hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 ${
                compact ? 'px-1.5 py-1 text-[11px]' : ''
              }`}
              onClick={zoomIn}
            >
              放大
            </button>
            <button
              type="button"
              className={`rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-600 transition-colors hover:bg-slate-50 hover:border-slate-400 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 ${
                compact ? 'px-1.5 py-1 text-[11px]' : ''
              }`}
              onClick={resetZoom}
            >
              重置
            </button>
          </div>
        </div>
        <div className={`flex flex-wrap justify-end gap-2 ${compact ? 'gap-1.5' : ''}`}>
          <div
            className="inline-flex overflow-hidden rounded-lg border border-slate-300 bg-white dark:border-slate-600 dark:bg-slate-800"
            role="group"
            aria-label="柱状图指标"
          >
            <button
              type="button"
              className={`min-w-7 border-0 px-2 py-1.5 text-xs ${histogramMode === 'amount' ? 'bg-blue-600 text-white' : 'bg-transparent text-slate-500 dark:text-slate-300'}`}
              aria-pressed={histogramMode === 'amount'}
              onClick={() => handleHistogramMode('amount')}
            >
              额
            </button>
            <button
              type="button"
              className={`min-w-7 border-0 border-l border-slate-200 px-2 py-1.5 text-xs dark:border-slate-600 ${
                histogramMode === 'volume' ? 'bg-blue-600 text-white' : 'bg-transparent text-slate-500 dark:text-slate-300'
              }`}
              aria-pressed={histogramMode === 'volume'}
              onClick={() => handleHistogramMode('volume')}
            >
              量
            </button>
          </div>
          <span className={`rounded-full bg-blue-600/10 px-2.5 py-1.5 text-xs font-bold text-blue-700 dark:text-blue-300 ${compact ? 'px-1.5 py-1 text-[11px]' : ''}`}>
            本地数据
          </span>
          <span className={`rounded-full bg-amber-500/15 px-2.5 py-1.5 text-xs font-bold text-amber-700 dark:text-amber-300 ${compact ? 'px-1.5 py-1 text-[11px]' : ''}`}>
            {intervalLabel}
          </span>
          {latestBar?.isProvisional && (
            <span className={`rounded-full border border-orange-300 bg-orange-50 px-2.5 py-1.5 text-xs font-bold text-amber-700 dark:border-orange-500/40 dark:bg-orange-500/10 dark:text-amber-300 ${compact ? 'px-1.5 py-1 text-[11px]' : ''}`}>
              盘中临时K · {latestBar.asOf || '更新中'}
            </span>
          )}
          <span className={`rounded-full bg-blue-600/10 px-2.5 py-1.5 text-xs font-bold text-blue-700 dark:text-blue-300 ${compact ? 'px-1.5 py-1 text-[11px]' : ''}`}>
            MA5
          </span>
          <span className={`rounded-full bg-amber-500/15 px-2.5 py-1.5 text-xs font-bold text-amber-700 dark:text-amber-300 ${compact ? 'px-1.5 py-1 text-[11px]' : ''}`}>
            MA10
          </span>
        </div>
      </div>
      <div
        ref={containerRef}
        className={`relative z-[1] w-full ${compact ? 'h-[220px] min-h-[220px]' : 'h-[calc(100%-70px)] min-h-[420px]'}`}
        onWheel={handleMouseWheel}
      />
    </div>
  )
}

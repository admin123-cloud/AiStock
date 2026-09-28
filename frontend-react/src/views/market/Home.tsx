import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import request from '@/utils/request'
import { EChart } from '@/components/EChart'
import { Badge } from '@/components/catalyst/badge'
import { Button } from '@/components/catalyst/button'
import { Card, ChangeValue, EmptyState, PageHeader, SectionTitle } from '@/components/ui'

/* ------------------------------------------------------------------ */
/* 常量与元数据（与旧版 Home.vue 保持一致）                              */
/* ------------------------------------------------------------------ */

const MAIN_INDICES = ['000001.SH', '399001.SZ', '399006.SZ', '000680.SH']

const EMOTION_SCORE_BANDS = [
  { key: 'ice', label: '冰点', min: 0, max: 20, color: '#39d353', bg: 'rgba(57, 211, 83, 0.10)' },
  { key: 'repair', label: '修复', min: 20, max: 40, color: '#78d64b', bg: 'rgba(120, 214, 75, 0.10)' },
  { key: 'start', label: '启动', min: 40, max: 60, color: '#f3d12f', bg: 'rgba(243, 209, 47, 0.12)' },
  { key: 'ferment', label: '发酵', min: 60, max: 80, color: '#ff7a2f', bg: 'rgba(255, 122, 47, 0.12)' },
  { key: 'hot', label: '高潮', min: 80, max: 100, color: '#ff3b30', bg: 'rgba(255, 59, 48, 0.12)' }
] as const

interface EmotionSeriesMeta {
  key: string
  name: string
  shortName: string
  color: string
  width: number
  lineType: 'solid' | 'dashed'
}

const EMOTION_CYCLE_SERIES_META: EmotionSeriesMeta[] = [
  { key: 'close_up_rate', name: '收盘上涨比例', shortName: '收盘', color: '#d8dde8', width: 2.2, lineType: 'solid' },
  { key: 'intraday_up_rate', name: '盘中上涨比例', shortName: '盘中', color: '#b8c4d9', width: 2.2, lineType: 'solid' },
  { key: 'sh_up_rate', name: '上证上涨比例', shortName: '上证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'sz_up_rate', name: '深证上涨比例', shortName: '深证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'cyb_up_rate', name: '创业板上涨比例', shortName: '创业板', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_monster_up_rate', name: '上日妖股上涨比例', shortName: '妖股', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_strong_up_rate', name: '上日强势票上涨比例', shortName: '强势', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_weak_up_rate', name: '上日弱势票上涨比例', shortName: '弱势', color: '#e2e8f0', width: 2.0, lineType: 'dashed' }
]

const PHASE_LABEL_MAP: Record<string, string> = {
  ice: '冰点',
  start: '启动',
  ferment: '发酵',
  euphoria: '高潮',
  decline: '退潮'
}

const PHASE_CARD_META: Record<string, { phase_name: string; action_hint: string; risk_hint: string }> = {
  ice: {
    phase_name: '冰点期(股灾)',
    action_hint: '按计划分批低吸，等待情绪修复的第一波确认。',
    risk_hint: '优先控制抄底节奏，避免恐慌未收敛前过早重仓。'
  },
  start: {
    phase_name: '启动期',
    action_hint: '轻仓试错，优先跟踪开始转强的核心方向。',
    risk_hint: '信号仍在构建，避免一次性重仓。'
  },
  ferment: {
    phase_name: '发酵期',
    action_hint: '持股为主，围绕主线做结构优化。',
    risk_hint: '注意分化，不要在同一方向过度堆叠。'
  },
  euphoria: {
    phase_name: '高潮期',
    action_hint: '分批止盈，降低追高频率，保留核心强势仓位。',
    risk_hint: '高波动区间，提防情绪反转和高位回撤。'
  },
  decline: {
    phase_name: '退潮期',
    action_hint: '控制回撤，减仓弱势品种，等待下一轮信号。',
    risk_hint: '退潮时容易连续回撤，避免频繁交易。'
  }
}

const PHASE_CELL_STYLE: Record<string, React.CSSProperties> = {
  ice: { borderColor: 'rgba(56, 189, 248, 0.42)', background: 'linear-gradient(135deg, rgba(12, 74, 110, 0.72), rgba(30, 41, 59, 0.82))' },
  start: { borderColor: 'rgba(250, 204, 21, 0.42)', background: 'linear-gradient(135deg, rgba(92, 53, 15, 0.70), rgba(30, 41, 59, 0.82))' },
  ferment: { borderColor: 'rgba(74, 222, 128, 0.42)', background: 'linear-gradient(135deg, rgba(6, 78, 59, 0.72), rgba(30, 41, 59, 0.82))' },
  euphoria: { borderColor: 'rgba(248, 113, 113, 0.42)', background: 'linear-gradient(135deg, rgba(127, 29, 29, 0.72), rgba(30, 41, 59, 0.82))' },
  decline: { borderColor: 'rgba(244, 114, 182, 0.42)', background: 'linear-gradient(135deg, rgba(112, 26, 117, 0.72), rgba(30, 41, 59, 0.82))' }
}

/* 亮色主题下的阶段卡片配色（浅底） */
const PHASE_CELL_STYLE_LIGHT: Record<string, React.CSSProperties> = {
  ice: { borderColor: 'rgba(2, 132, 199, 0.45)', background: 'linear-gradient(135deg, rgba(224, 242, 254, 0.9), rgba(241, 245, 249, 0.92))' },
  start: { borderColor: 'rgba(202, 138, 4, 0.45)', background: 'linear-gradient(135deg, rgba(254, 249, 195, 0.9), rgba(254, 252, 232, 0.92))' },
  ferment: { borderColor: 'rgba(22, 163, 74, 0.45)', background: 'linear-gradient(135deg, rgba(220, 252, 231, 0.9), rgba(240, 253, 244, 0.92))' },
  euphoria: { borderColor: 'rgba(220, 38, 38, 0.45)', background: 'linear-gradient(135deg, rgba(254, 226, 226, 0.9), rgba(255, 241, 242, 0.92))' },
  decline: { borderColor: 'rgba(192, 38, 211, 0.40)', background: 'linear-gradient(135deg, rgba(250, 232, 255, 0.9), rgba(253, 244, 255, 0.92))' }
}

/* 亮色主题下情绪分段的加深配色（原色在白底上偏浅、可读性差） */
const BAND_LIGHT_COLORS: Record<string, string> = {
  ice: '#16a34a',
  repair: '#65a30d',
  start: '#ca8a04',
  ferment: '#ea580c',
  hot: '#dc2626'
}

/* ------------------------------------------------------------------ */
/* 工具函数                                                             */
/* ------------------------------------------------------------------ */

const formatPrice = (price?: number | null) => {
  if (!price) return '--'
  return price.toFixed(2)
}

const formatAmount = (amount?: number | null) => {
  if (!amount) return '--'
  if (amount >= 100000000) return (amount / 100000000).toFixed(2) + '亿'
  if (amount >= 10000) return (amount / 10000).toFixed(2) + '万'
  return amount.toFixed(2)
}

const formatTrendDate = (dateStr?: string | null) => {
  if (!dateStr || typeof dateStr !== 'string') return '--'
  if (dateStr.includes(' ')) {
    const [datePart, timePart = ''] = dateStr.split(' ')
    const parts = datePart.split('-')
    const hour = timePart.slice(0, 5)
    if (parts.length === 3 && hour) return `${parts[1]}-${parts[2]} ${hour}`
  }
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  return `${parts[1]}-${parts[2]}`
}

const formatTrendDateCompact = (dateStr?: string | null) => {
  if (!dateStr || typeof dateStr !== 'string') return '--'
  if (dateStr.includes(' ')) {
    const [datePart, timePart = ''] = dateStr.split(' ')
    const parts = datePart.split('-')
    const hour = timePart.slice(0, 5)
    if (parts.length === 3 && hour) return `${parts[0]}${parts[1]}${parts[2]} ${hour}`
  }
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  return `${parts[0]}${parts[1]}${parts[2]}`
}

const formatMarginTrillion = (amount?: number | null) => `${(Number(amount || 0) / 1_000_000_000_000).toFixed(2)} 万亿`

const formatMarginBillions = (amount?: number | null) => {
  const value = Number(amount || 0) / 100_000_000
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)} 亿`
}

const formatTurnoverTrillion = (amount?: number | null) => `${(Number(amount || 0) / 1_000_000_000_000).toFixed(2)} 万亿`

const getEmotionScoreBand = (value: number | null | undefined) => {
  const score = Math.max(0, Math.min(100, Number(value) || 0))
  return EMOTION_SCORE_BANDS.find((item) => score >= item.min && score <= item.max) || EMOTION_SCORE_BANDS[0]
}

const buildSmoothPath = (points: { x: number; y: number }[]) => {
  if (!points.length) return ''
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`
  let d = `M ${points[0].x} ${points[0].y}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const current = points[i]
    const next = points[i + 1]
    const previous = points[i - 1] || current
    const afterNext = points[i + 2] || next
    const cp1x = current.x + (next.x - previous.x) / 6
    const cp1y = current.y + (next.y - previous.y) / 6
    const cp2x = next.x - (afterNext.x - current.x) / 6
    const cp2y = next.y - (afterNext.y - current.y) / 6
    d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${next.x} ${next.y}`
  }
  return d
}

interface EmotionPoint {
  closeUp: number
  strongUp: number
  followRate: number
}

const resolveEmotionPhaseCode = (point: EmotionPoint, prevPoint: EmotionPoint): string => {
  const closeUp = Number(point.closeUp || 0)
  const strongUp = Number(point.strongUp || 0)
  const followRate = Number(point.followRate || 0)
  const prevCloseUp = Number(prevPoint?.closeUp ?? closeUp)
  const prevStrongUp = Number(prevPoint?.strongUp ?? strongUp)
  const deltaCloseUp = closeUp - prevCloseUp
  const deltaStrongUp = strongUp - prevStrongUp

  if (closeUp <= 38 || (closeUp <= 45 && strongUp <= 15)) return 'ice'
  if (closeUp >= 72 && strongUp >= 35 && followRate >= 58) return 'euphoria'
  if ((deltaCloseUp <= -5 && closeUp < 62) || (strongUp < 20 && deltaStrongUp <= -2)) return 'decline'
  if (closeUp >= 56 && strongUp >= 24 && deltaCloseUp >= -1) return 'ferment'
  return 'start'
}

interface EmotionPhaseDetail {
  phase_code: string
  phase_name: string
  confidence: number
  action_hint: string
  risk_hint: string
  reason_summary: string
  data_status?: Record<string, unknown> | null
}

const resolveEmotionPhaseDetail = (
  point: EmotionPoint,
  prevPoint: EmotionPoint,
  granularity: string,
  dataStatus: Record<string, unknown> | null | undefined = null
): EmotionPhaseDetail => {
  const closeUp = Number(point.closeUp || 0)
  const strongUp = Number(point.strongUp || 0)
  const followRate = Number(point.followRate || 0)
  const prevCloseUp = Number(prevPoint?.closeUp ?? closeUp)
  const prevStrongUp = Number(prevPoint?.strongUp ?? strongUp)
  const deltaCloseUp = closeUp - prevCloseUp
  const deltaStrongUp = strongUp - prevStrongUp
  const code = resolveEmotionPhaseCode(point, prevPoint)
  const meta = PHASE_CARD_META[code] || PHASE_CARD_META.start
  const reasons: string[] = []
  let confidenceHits = 0

  if (code === 'ice') {
    if (closeUp <= 38) { reasons.push(`收盘上涨比例偏低(${closeUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (strongUp <= 15) { reasons.push(`强势上涨比例偏低(${strongUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (deltaCloseUp <= 0) { reasons.push(`短期动量仍未转强(${deltaCloseUp.toFixed(1)})`); confidenceHits += 1 }
  } else if (code === 'euphoria') {
    if (closeUp >= 72) { reasons.push(`收盘上涨比例高位(${closeUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (strongUp >= 35) { reasons.push(`强势上涨比例较高(${strongUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (followRate >= 58) { reasons.push(`涨停溢价较好(${followRate.toFixed(1)}%)`); confidenceHits += 1 }
  } else if (code === 'decline') {
    if (deltaCloseUp <= -5) { reasons.push(`收盘上涨比例回落(${deltaCloseUp.toFixed(1)})`); confidenceHits += 1 }
    if (strongUp < 20) { reasons.push(`强势上涨比例偏弱(${strongUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (deltaStrongUp <= -2) { reasons.push(`强势动量走弱(${deltaStrongUp.toFixed(1)})`); confidenceHits += 1 }
  } else if (code === 'ferment') {
    if (closeUp >= 56) { reasons.push(`收盘上涨比例维持强势(${closeUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (strongUp >= 24) { reasons.push(`强势上涨比例抬升(${strongUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (deltaCloseUp >= -1) { reasons.push(`短期动量保持稳定(${deltaCloseUp.toFixed(1)})`); confidenceHits += 1 }
  } else {
    if (closeUp >= 42 && closeUp <= 58) { reasons.push(`收盘上涨比例进入修复区间(${closeUp.toFixed(1)}%)`); confidenceHits += 1 }
    if (deltaCloseUp >= 2) { reasons.push(`短期动量明显改善(${deltaCloseUp.toFixed(1)})`); confidenceHits += 1 }
    if (strongUp >= 18) { reasons.push(`强势表现开始回升(${strongUp.toFixed(1)}%)`); confidenceHits += 1 }
  }

  if (!reasons.length) {
    reasons.push(`${granularity === 'hourly' ? '小时' : '日线'}情绪曲线当前处于中间区间`)
  }

  return {
    phase_code: code,
    phase_name: meta.phase_name,
    confidence: Math.min(0.95, 0.48 + confidenceHits * 0.12),
    action_hint: meta.action_hint,
    risk_hint: meta.risk_hint,
    reason_summary: reasons.join('；'),
    data_status: dataStatus || undefined
  }
}

const marginBalancePoints = (values: number[]) => {
  if (!values.length) return ''
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = Math.max(maximum - minimum, 1)
  const width = 746
  return values
    .map((value, index) => {
      const x = 36 + (values.length === 1 ? width / 2 : (index * width) / (values.length - 1))
      const y = 118 - ((Number(value || 0) - minimum) / range) * 94
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

interface MarginBar {
  index: number
  value: number
  x: number
  y: number
  width: number
  height: number
}

const marginChangeBars = (values: number[], offset: number): MarginBar[] => {
  if (!values.length) return []
  const maxAbs = Math.max(...values.map((value) => Math.abs(Number(value || 0))), 1)
  const step = 746 / values.length
  const width = Math.max(2, Math.min(8, step * 0.32))
  return values.map((value, index) => {
    const amount = Number(value || 0)
    const height = (Math.abs(amount) / maxAbs) * 57
    return { index, value: amount, x: 36 + index * step + offset * width, y: amount >= 0 ? 75 - height : 75, width, height }
  })
}

/* ------------------------------------------------------------------ */
/* 组件                                                                 */
/* ------------------------------------------------------------------ */

export default function Home() {
  const navigate = useNavigate()

  /* ---------------- 状态 ---------------- */
  const [allIndices, setAllIndices] = useState<any[]>([])
  const [tradingDate, setTradingDate] = useState<string | null>(null)
  const [sentiment, setSentiment] = useState<any>(null)
  const [marginSentiment, setMarginSentiment] = useState<any>({ available: false, dates: [], latest: null })
  const [emotionCycleData, setEmotionCycleData] = useState<any>(null)
  const [emotionGranularity, setEmotionGranularity] = useState<'daily' | 'hourly'>('daily')
  const [showAllIndices, setShowAllIndices] = useState(false)
  const [emotionLegendSelected, setEmotionLegendSelected] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(EMOTION_CYCLE_SERIES_META.map((item) => [item.name, true]))
  )
  const [hoveredEmotionIndex, setHoveredEmotionIndex] = useState<number | null>(null)
  const [selectedSentimentIndex, setSelectedSentimentIndex] = useState<number | null>(null)
  const [hoveredMarginIndex, setHoveredMarginIndex] = useState<number | null>(null)
  const [marginHoverPosition, setMarginHoverPosition] = useState({ x: 12, y: 12 })
  const [dark, setDark] = useState<boolean>(() => document.documentElement.classList.contains('dark'))

  const granularityRef = useRef(emotionGranularity)
  granularityRef.current = emotionGranularity
  const retryTimerRef = useRef<number | null>(null)
  const marginCardRef = useRef<HTMLDivElement | null>(null)

  /* ---------------- 暗色监听 ---------------- */
  useEffect(() => {
    const observer = new MutationObserver(() => {
      setDark(document.documentElement.classList.contains('dark'))
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])

  /* ---------------- 数据加载 ---------------- */
  const loadEmotionCycle = useCallback(async () => {
    try {
      const data = (await request.get('/market/emotion-trend', {
        params: { days: 30, granularity: granularityRef.current },
        skipErrorHandler: true
      })) as any
      setEmotionCycleData(data)
      if (data?.error) console.error('情绪周期数据加载失败:', data.error)
    } catch (error) {
      console.error('加载情绪周期失败:', error)
    }
  }, [])

  const loadMarketData = useCallback(
    async (retryCount = 0) => {
      try {
        const refreshParams = { _ts: Date.now() }
        const [indicesRes, sentimentRes, marginRes] = (await Promise.all([
          request.get('/market/indices', { params: { all: true, ...refreshParams }, skipErrorHandler: true }),
          request.get('/market/sentiment', { params: refreshParams, skipErrorHandler: true }),
          request.get('/market/margin-sentiment', { params: { days: 30, ...refreshParams }, skipErrorHandler: true })
        ])) as any[]

        setMarginSentiment(marginRes || { available: false, dates: [], latest: null })
        setAllIndices(indicesRes?.indices || [])
        setTradingDate(indicesRes?.trading_date || null)
        setSentiment(sentimentRes?.error ? null : sentimentRes)

        await loadEmotionCycle()
      } catch (error) {
        console.error('加载大盘数据失败:', error)
        // 前后端容器同时重启时，前端可能比后端更早就绪；短暂重试避免首页图表永久空白。
        if (retryCount < 3) {
          if (retryTimerRef.current) window.clearTimeout(retryTimerRef.current)
          retryTimerRef.current = window.setTimeout(() => {
            retryTimerRef.current = null
            loadMarketData(retryCount + 1)
          }, 2_000)
        }
      }
    },
    [loadEmotionCycle]
  )

  useEffect(() => {
    loadMarketData()
    const marketRefreshTimer = window.setInterval(() => loadMarketData(), 60 * 1000)
    const emotionRefreshTimer = window.setInterval(() => loadEmotionCycle(), 60 * 1000)
    return () => {
      window.clearInterval(marketRefreshTimer)
      window.clearInterval(emotionRefreshTimer)
      if (retryTimerRef.current) {
        window.clearTimeout(retryTimerRef.current)
        retryTimerRef.current = null
      }
    }
  }, [loadMarketData, loadEmotionCycle])

  /* ---------------- 按当前明暗取情绪分段色（暗色用原亮色系，亮色用加深版） ---------------- */
  const bandTone = useCallback(
    (band: { key: string; color: string }) => (dark ? band.color : BAND_LIGHT_COLORS[band.key] || band.color),
    [dark]
  )

  /* ---------------- 派生数据 ---------------- */
  const displayIndices = useMemo(() => {
    if (showAllIndices) return allIndices
    return allIndices.filter((index) => MAIN_INDICES.includes(index.code))
  }, [allIndices, showAllIndices])

  const sentimentDistribution = useMemo(() => {
    const raw = sentiment?.change_distribution
    if (!Array.isArray(raw)) return []
    return raw.map((item: any) => ({
      label: item.label as string,
      count: Number(item.count || 0),
      side: (item.side || 'flat') as 'up' | 'down' | 'flat'
    }))
  }, [sentiment])

  const emotionCurve30d = useMemo(() => {
    const curve = sentiment?.emotion_curve_30d
    if (!curve || !Array.isArray(curve.trade_dates)) {
      return { days: [] as string[], raw: {} as Record<string, number[]>, normalized: {} as Record<string, number[]>, latest: {} as Record<string, number> }
    }
    return {
      days: curve.trade_dates,
      raw: curve.raw_series || {},
      normalized: curve.normalized_series || {},
      latest: curve.latest_snapshot || {}
    }
  }, [sentiment])

  const selectedSentimentSnapshot = useMemo(() => {
    const index = selectedSentimentIndex
    const { days, raw } = emotionCurve30d
    if (!Number.isInteger(index) || index === null || index < 0 || index >= days.length) return null
    const value = (key: string) => Number(raw[key]?.[index] || 0)
    return {
      date: days[index],
      up: value('up_count_series'),
      down: value('down_count_series'),
      limitUp: value('limit_up_count_series'),
      limitDown: value('limit_down_count_series'),
      up5: value('up_5_count_series'),
      down5: value('down_5_count_series')
    }
  }, [selectedSentimentIndex, emotionCurve30d])

  const turnoverTrend = useMemo(() => {
    const raw = sentiment?.turnover_trend_30d || {}
    return {
      days: Array.isArray(raw.trade_dates) ? raw.trade_dates : [],
      amounts: Array.isArray(raw.amounts) ? raw.amounts.map((value: any) => Number(value || 0)) : [],
      shAmounts: Array.isArray(raw.sh_amounts) ? raw.sh_amounts.map((value: any) => Number(value || 0)) : [],
      szAmounts: Array.isArray(raw.sz_amounts) ? raw.sz_amounts.map((value: any) => Number(value || 0)) : [],
      bjAmounts: Array.isArray(raw.bj_amounts) ? raw.bj_amounts.map((value: any) => Number(value || 0)) : [],
      threshold: Number(raw.threshold_amount || 2_000_000_000_000),
      latestIsProvisional: Boolean(raw.latest_is_provisional),
      latestAsOf: (raw.latest_as_of || '') as string
    }
  }, [sentiment])

  const intradayTurnoverForecast = useMemo(
    () => sentiment?.turnover_forecast_intraday || { available: false, reason: '' },
    [sentiment]
  )

  const latestTurnoverAmount = useMemo(() => {
    const amounts = turnoverTrend.amounts
    return amounts.length ? amounts[amounts.length - 1] : 0
  }, [turnoverTrend])

  const turnoverIsActive = latestTurnoverAmount >= turnoverTrend.threshold

  const latestTurnoverText = useMemo(() => {
    if (!latestTurnoverAmount) return '成交额数据待更新'
    const amountInTrillion = latestTurnoverAmount / 1_000_000_000_000
    if (turnoverTrend.latestIsProvisional) {
      const asOf = turnoverTrend.latestAsOf.slice(11, 16)
      return `${amountInTrillion.toFixed(2)} 万亿 · 盘中累计${asOf ? `（截至 ${asOf}）` : ''}`
    }
    return `${amountInTrillion.toFixed(2)} 万亿 · ${turnoverIsActive ? '交易活跃' : '交易冷清'}`
  }, [latestTurnoverAmount, turnoverTrend, turnoverIsActive])

  const intradayEmotionHint = useMemo(() => {
    const snapshot = emotionCycleData?.intraday_snapshot
    if (!snapshot?.is_temporary) return ''
    const asOf = String(snapshot.as_of || '').slice(0, 16)
    const total = Number(snapshot.total_stocks || 0).toLocaleString()
    return `盘中临时情绪：${asOf}，${total}只股票，基于QMT 5分钟行情；收盘后以正式日线结果为准。`
  }, [emotionCycleData])

  const activeEmotionSeriesMeta = useMemo(
    () => EMOTION_CYCLE_SERIES_META.filter((item) => emotionLegendSelected[item.name] !== false),
    [emotionLegendSelected]
  )

  const latestEmotionMetrics = useMemo(() => {
    const ec = emotionCycleData
    const dates = ec?.dates || []
    if (!dates.length) return []
    const latestIndex = dates.length - 1
    return EMOTION_CYCLE_SERIES_META.map((meta) => {
      const value = Number(ec?.[meta.key]?.[latestIndex] ?? 0)
      return { ...meta, value, valueText: value.toFixed(2) + '%', band: getEmotionScoreBand(value) }
    })
  }, [emotionCycleData])

  const currentEmotionPhase = useMemo<EmotionPhaseDetail | null>(() => {
    const ec = emotionCycleData
    const dates = ec?.dates || []
    if (!dates.length) {
      return sentiment?.emotion_phase || null
    }
    const lastIndex = dates.length - 1
    const point = {
      closeUp: ec?.close_up_rate?.[lastIndex] ?? 0,
      strongUp: ec?.strong_up_rate?.[lastIndex] ?? 0,
      followRate: ec?.limit_up_follow_rate?.[lastIndex] ?? 0
    }
    const prevPoint = lastIndex > 0
      ? {
          closeUp: ec?.close_up_rate?.[lastIndex - 1] ?? point.closeUp,
          strongUp: ec?.strong_up_rate?.[lastIndex - 1] ?? point.strongUp,
          followRate: ec?.limit_up_follow_rate?.[lastIndex - 1] ?? point.followRate
        }
      : point

    const fallbackStatus =
      emotionGranularity === 'daily'
        ? ec?.intraday_snapshot
          ? {
              emotion_cycle_date: ec.intraday_snapshot.date,
              trade_date: ec.intraday_snapshot.date,
              is_stale: false,
              is_temporary: true
            }
          : sentiment?.emotion_phase?.data_status
        : null

    return resolveEmotionPhaseDetail(point, prevPoint, emotionGranularity, fallbackStatus)
  }, [emotionCycleData, sentiment, emotionGranularity])

  /* ---------------- 情绪周期 SVG 模型 ---------------- */
  const emotionChartModel = useMemo(() => {
    const ec = emotionCycleData
    const dates = ec?.dates || []
    const seriesMeta = activeEmotionSeriesMeta
    if (!dates.length || !seriesMeta.length) return null

    const width = 1100
    const height = 470
    const padding = { top: 30, right: 14, bottom: 40, left: 50 }
    const plotWidth = width - padding.left - padding.right
    const plotHeight = height - padding.top - padding.bottom
    const maxIndex = Math.max(dates.length - 1, 1)
    const xScale = (index: number) => padding.left + (plotWidth * index) / maxIndex
    const yScale = (value: number) => padding.top + ((100 - Math.max(0, Math.min(100, Number(value) || 0))) / 100) * plotHeight
    const labelStep = Math.max(1, Math.ceil(dates.length / 7))

    const bands = EMOTION_SCORE_BANDS.map((band) => {
      const upperY = yScale(band.max)
      const lowerY = yScale(band.min)
      return { ...band, y: upperY, height: Math.max(1, lowerY - upperY), tone: bandTone(band) }
    })

    const series = seriesMeta.map((meta) => {
      const values = dates.map((_: unknown, index: number) => Number(ec[meta.key]?.[index] ?? 0))
      const points = values.map((value: number, index: number) => ({ x: xScale(index), y: yScale(value), value, band: getEmotionScoreBand(value) }))
      return {
        key: meta.key,
        name: meta.name,
        color: meta.color,
        values,
        strokeWidth: meta.width,
        dasharray: meta.lineType === 'dashed' ? '8 6' : undefined,
        points,
        path: buildSmoothPath(points)
      }
    })

    const columns = dates.map((_: unknown, index: number) => {
      const x = xScale(index)
      const prevX = index === 0 ? padding.left : xScale(index - 1)
      const nextX = index === dates.length - 1 ? width - padding.right : xScale(index + 1)
      return {
        index,
        x,
        left: index === 0 ? padding.left : (prevX + x) / 2,
        width:
          index === dates.length - 1
            ? Math.max(16, width - padding.right - (prevX + x) / 2)
            : Math.max(16, (nextX - prevX) / 2)
      }
    })

    const xLabels = dates
      .map((date: string, index: number) => ({ date, index, x: xScale(index) }))
      .filter((item, index) => index === 0 || index === dates.length - 1 || index % labelStep === 0)
      .map((item) => ({ ...item, text: formatTrendDate(item.date) }))

    const hoverPoints = (index: number) =>
      series.map((item) => {
        const value = item.values[index] ?? 0
        return { key: item.key, color: bandTone(getEmotionScoreBand(value)), x: xScale(index), y: yScale(value) }
      })

    return { width, height, padding, plotWidth, plotHeight, bands, yTicks: [0, 20, 40, 60, 80, 100], dates, series, columns, xLabels, xScale, yScale, hoverPoints }
  }, [emotionCycleData, activeEmotionSeriesMeta, bandTone])

  const emotionTooltipData = useMemo(() => {
    if (hoveredEmotionIndex === null || !emotionChartModel) return null
    const index = hoveredEmotionIndex
    const date = emotionChartModel.dates[index]
    if (!date) return null
    return {
      date: formatTrendDateCompact(date),
      items: emotionChartModel.series.map((seriesItem) => {
        const value = seriesItem.values[index] ?? 0
        return { key: seriesItem.key, name: seriesItem.name, color: bandTone(getEmotionScoreBand(value)), value: value.toFixed(2) + '%' }
      })
    }
  }, [hoveredEmotionIndex, emotionChartModel, bandTone])

  const emotionTooltipStyle = useMemo<React.CSSProperties>(() => {
    if (hoveredEmotionIndex === null || !emotionChartModel) return {}
    const column = emotionChartModel.columns[hoveredEmotionIndex]
    if (!column) return {}
    const ratio = emotionChartModel.width <= 0 ? 0 : column.x / emotionChartModel.width
    return {
      left:
        ratio > 0.62
          ? `clamp(16px, calc(${(ratio * 100).toFixed(2)}% - 210px), calc(100% - 186px))`
          : `clamp(16px, calc(${(ratio * 100).toFixed(2)}% + 12px), calc(100% - 186px))`,
      top: '22px'
    }
  }, [hoveredEmotionIndex, emotionChartModel])

  const emotionPhaseTimeline = useMemo(() => {
    const ec = emotionCycleData
    const dates = ec?.dates || []
    if (!dates.length) return []
    const closeSeries = ec?.close_up_rate || []
    const strongSeries = ec?.strong_up_rate || []
    const followSeries = ec?.limit_up_follow_rate || []
    const size = dates.length
    const timeline: { date: string; dateLabel: string; code: string; label: string; showLabel: boolean; closeUp: string; strongUp: string; followRate: string }[] = []
    const labelStep = Math.max(1, Math.floor(size / 6))

    for (let i = 0; i < size; i += 1) {
      const point = { closeUp: closeSeries[i] ?? 0, strongUp: strongSeries[i] ?? 0, followRate: followSeries[i] ?? 0 }
      const prevPoint = i > 0
        ? { closeUp: closeSeries[i - 1] ?? point.closeUp, strongUp: strongSeries[i - 1] ?? point.strongUp, followRate: followSeries[i - 1] ?? point.followRate }
        : point
      const detail = resolveEmotionPhaseDetail(point, prevPoint, emotionGranularity)
      timeline.push({
        date: dates[i],
        dateLabel: formatTrendDate(dates[i]),
        code: detail.phase_code,
        label: PHASE_LABEL_MAP[detail.phase_code] || '启动',
        showLabel: i === 0 || i === size - 1 || i % labelStep === 0,
        closeUp: Number(point.closeUp || 0).toFixed(1),
        strongUp: Number(point.strongUp || 0).toFixed(1),
        followRate: Number(point.followRate || 0).toFixed(1)
      })
    }
    return timeline
  }, [emotionCycleData, emotionGranularity])

  const emotionPhaseTimelineRange = useMemo(() => {
    const list = emotionPhaseTimeline
    if (!list.length) return { start: '--', end: '--' }
    return { start: list[0].dateLabel, end: list[list.length - 1].dateLabel }
  }, [emotionPhaseTimeline])

  /* ---------------- 两融派生 ---------------- */
  const financingBalancePoints = useMemo(
    () => marginBalancePoints((marginSentiment?.financing_balance || []).map(Number)),
    [marginSentiment]
  )
  const securitiesLendingBalancePoints = useMemo(
    () => marginBalancePoints((marginSentiment?.securities_lending_balance || []).map(Number)),
    [marginSentiment]
  )
  const financingChangeBars = useMemo(() => marginChangeBars(marginSentiment?.financing_net_buy || [], 0), [marginSentiment])
  const securitiesLendingChangeBars = useMemo(
    () => marginChangeBars(marginSentiment?.securities_lending_balance_change || [], 1.15),
    [marginSentiment]
  )
  const marginHoverX = useMemo(() => {
    const count = marginSentiment?.dates?.length || 0
    if (hoveredMarginIndex === null || !count) return 36
    return 36 + (count === 1 ? 373 : (hoveredMarginIndex * 746) / (count - 1))
  }, [hoveredMarginIndex, marginSentiment])

  const marginHoverData = useMemo(() => {
    const index = hoveredMarginIndex
    const dates = marginSentiment?.dates || []
    if (!Number.isInteger(index) || index === null || !dates[index]) return null
    const value = (key: string) => Number(marginSentiment?.[key]?.[index] || 0)
    return {
      date: dates[index],
      financingBalance: value('financing_balance'),
      securitiesLendingBalance: value('securities_lending_balance'),
      financingChange: value('financing_net_buy'),
      securitiesLendingChange: value('securities_lending_balance_change'),
      marginBalance: value('margin_balance'),
      financingBuy: value('financing_buy')
    }
  }, [hoveredMarginIndex, marginSentiment])

  const updateMarginHover = (event: React.MouseEvent<SVGSVGElement>) => {
    const dates = marginSentiment?.dates || []
    if (!dates.length) return
    const svgRect = event.currentTarget.getBoundingClientRect()
    const chartRatio = Math.max(0, Math.min(1, (event.clientX - svgRect.left - svgRect.width * 0.045) / (svgRect.width * 0.9325)))
    setHoveredMarginIndex(Math.round(chartRatio * (dates.length - 1)))
    const cardRect = marginCardRef.current?.getBoundingClientRect()
    if (!cardRect) return
    setMarginHoverPosition({
      x: Math.max(8, Math.min(cardRect.width - 262, event.clientX - cardRect.left + 14)),
      y: Math.max(8, Math.min(cardRect.height - 142, event.clientY - cardRect.top + 14))
    })
  }

  /* ---------------- 涨跌分布 ---------------- */
  const getUpWidth = (s: any) => {
    const total = (s?.up_count || 0) + (s?.down_count || 0) + (s?.unchanged_count || 0)
    if (total === 0) return '50%'
    return (s?.up_count || 0) / total * 100 + '%'
  }

  const getDistributionHeight = (count: number) => {
    const maxCount = Math.max(...sentimentDistribution.map((item) => item.count), 1)
    const ratio = Number(count || 0) / maxCount
    return `${Math.max(6, Math.round(ratio * 120))}px`
  }

  /* ---------------- 交互 ---------------- */
  const changeEmotionGranularity = async (granularity: 'daily' | 'hourly') => {
    if (emotionGranularity === granularity) return
    setEmotionGranularity(granularity)
    setHoveredEmotionIndex(null)
    await loadEmotionCycle()
  }

  const toggleEmotionLegend = (name: string) => {
    setEmotionLegendSelected((prev) => ({ ...prev, [name]: !prev[name] }))
    setHoveredEmotionIndex(null)
  }

  const handleSentimentCurveClick = (event: React.MouseEvent<HTMLDivElement>) => {
    const days = emotionCurve30d.days
    if (!days.length) return
    const rect = event.currentTarget.getBoundingClientRect()
    const plotLeft = 44
    const plotRight = 18
    const usableWidth = Math.max(1, rect.width - plotLeft - plotRight)
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left - plotLeft) / usableWidth))
    setSelectedSentimentIndex(Math.round(ratio * (days.length - 1)))
  }

  /* ---------------- ECharts 配置 ---------------- */
  const palette = useMemo(
    () =>
      dark
        ? { axisLine: '#3b4463', axisLabel: '#8a97b8', legend: '#627191', splitLine: 'rgba(140, 155, 187, 0.18)', markLineLabel: '#fef3c7' }
        : { axisLine: '#d7def2', axisLabel: '#7a89ab', legend: '#627191', splitLine: 'rgba(140, 155, 187, 0.18)', markLineLabel: '#92400e' },
    [dark]
  )

  const sentimentCurveOption = useMemo(() => {
    if (!emotionCurve30d.days.length) return null
    const days = emotionCurve30d.days
    const raw = emotionCurve30d.raw || {}
    const normalized = emotionCurve30d.normalized || {}
    const seriesMeta = [
      { key: 'up_count_series', name: '上涨家数', color: '#ff6b6b' },
      { key: 'down_count_series', name: '下跌家数', color: '#28c76f' },
      { key: 'limit_up_count_series', name: '涨停家数', color: '#ff9f43' },
      { key: 'limit_down_count_series', name: '跌停家数', color: '#00cfe8' },
      { key: 'up_5_count_series', name: '涨幅>=5%', color: '#ea5455' },
      { key: 'down_5_count_series', name: '跌幅<=-5%', color: '#20c997' }
    ]
    return {
      animation: false,
      tooltip: {
        trigger: 'axis' as const,
        triggerOn: 'mousemove|click',
        alwaysShowContent: true,
        backgroundColor: 'rgba(13, 18, 29, 0.94)',
        borderColor: '#26324d',
        textStyle: { color: '#eef3ff' },
        formatter: (params: any) => {
          const idx = params?.[0]?.dataIndex ?? 0
          const lines = [`<div style="margin-bottom:6px;font-weight:700;">${days[idx]}</div>`]
          params.forEach((item: any) => {
            const rawSeries = raw[item.seriesId] || []
            const rawVal = rawSeries[idx] ?? 0
            const delta = idx > 0 ? rawVal - (rawSeries[idx - 1] ?? 0) : 0
            const deltaText = idx > 0 ? ` (${delta >= 0 ? '+' : ''}${delta})` : ''
            lines.push(
              `<div style="display:flex;justify-content:space-between;gap:16px;"><span>${item.marker}${item.seriesName}</span><span>${rawVal}${deltaText}</span></div>`
            )
          })
          return lines.join('')
        }
      },
      legend: {
        type: 'scroll',
        top: 0,
        left: 0,
        right: 8,
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: palette.legend, fontSize: 11 }
      },
      grid: { left: 30, right: 14, top: 34, bottom: 26, containLabel: true },
      xAxis: {
        type: 'category' as const,
        boundaryGap: false,
        data: days,
        axisLine: { lineStyle: { color: palette.axisLine } },
        axisLabel: {
          color: palette.axisLabel,
          fontSize: 10,
          interval: Math.max(0, Math.floor(days.length / 6) - 1),
          formatter: (value: string) => formatTrendDate(value)
        }
      },
      yAxis: {
        type: 'value' as const,
        min: 0,
        max: 100,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: palette.axisLabel, fontSize: 10, formatter: '{value}' },
        splitLine: { lineStyle: { color: palette.splitLine, type: 'dashed' as const } }
      },
      series: seriesMeta.map((meta) => ({
        id: meta.key,
        name: meta.name,
        type: 'line' as const,
        smooth: true,
        showSymbol: false,
        symbolSize: 5,
        data: normalized[meta.key] || [],
        lineStyle: { width: meta.key.includes('limit_') ? 1.8 : 2.3, color: meta.color },
        itemStyle: { color: meta.color },
        emphasis: { focus: 'series' as const }
      }))
    }
  }, [emotionCurve30d, palette])

  const turnoverTrendOption = useMemo(() => {
    const { days, amounts, shAmounts, szAmounts, bjAmounts, threshold } = turnoverTrend
    if (!days.length) return null
    const shAmountsInBillion = shAmounts.map((value) => value / 100_000_000)
    const szAmountsInBillion = szAmounts.map((value) => value / 100_000_000)
    const bjAmountsInBillion = bjAmounts.map((value) => value / 100_000_000)
    const thresholdInBillion = threshold / 100_000_000
    const forecast = intradayTurnoverForecast
    const forecastAmountInBillion = forecast.available ? Number(forecast.forecast_amount || 0) / 100_000_000 : 0
    const forecastRemainingInBillion = amounts.map((amount, index) =>
      index === amounts.length - 1 && forecastAmountInBillion > amount / 100_000_000
        ? forecastAmountInBillion - amount / 100_000_000
        : 0
    )
    const forecastMarkPoint = forecastRemainingInBillion.some((value) => value > 0)
      ? [{ name: '预计全天', coord: [days[days.length - 1], forecastAmountInBillion], value: forecastAmountInBillion, itemStyle: { color: '#facc15' } }]
      : []
    const turnoverExtrema = amounts
      .map((amount, index) => ({ index, value: Number(amount) / 100_000_000 }))
      .filter((item) => Number.isFinite(item.value))
    if (!turnoverExtrema.length) return null
    const minTurnover = turnoverExtrema.reduce((min, item) => (item.value < min.value ? item : min), turnoverExtrema[0])
    const maxTurnover = turnoverExtrema.reduce((max, item) => (item.value > max.value ? item : max), turnoverExtrema[0])
    const turnoverExtremaMarkPoints = [
      { name: '最低值', coord: [days[minTurnover.index], minTurnover.value], value: minTurnover.value, itemStyle: { color: '#60a5fa' } },
      { name: '最高值', coord: [days[maxTurnover.index], maxTurnover.value], value: maxTurnover.value, itemStyle: { color: '#fbbf24' } }
    ]
    const changeDirections = amounts.map((amount, index) => {
      if (index === 0) return 'flat'
      return amount > amounts[index - 1] ? 'up' : amount < amounts[index - 1] ? 'down' : 'flat'
    })
    const segmentColors: Record<string, string[]> = {
      up: ['#ef4444', '#f97316', '#fca5a5'],
      down: ['#16a34a', '#22c55e', '#86efac'],
      flat: ['#64748b', '#94a3b8', '#cbd5e1']
    }
    const segmentColor = (index: number, segmentIndex: number) => segmentColors[changeDirections[index]][segmentIndex]
    return {
      animation: false,
      tooltip: {
        trigger: 'axis' as const,
        backgroundColor: 'rgba(13, 18, 29, 0.94)',
        borderColor: '#26324d',
        textStyle: { color: '#eef3ff' },
        formatter: (params: any) => {
          const item = params?.[0]
          if (!item) return ''
          const index = item.dataIndex
          const value = Number(amounts[index] || 0) / 100_000_000
          const active = value >= thresholdInBillion
          const previous = index > 0 ? Number(amounts[index - 1] || 0) : null
          const change = previous === null ? 0 : Number(amounts[index] || 0) - previous
          const changePercent = previous ? (change / previous) * 100 : 0
          const changeText =
            previous === null
              ? '首个统计日，无前日对比'
              : `较前日${change > 0 ? '放量' : change < 0 ? '缩量' : '持平'} ${(Math.abs(change) / 1000000000000).toFixed(2)} 万亿（${change > 0 ? '+' : ''}${changePercent.toFixed(2)}%）`
          const changeColor = change > 0 ? '#f87171' : change < 0 ? '#4ade80' : '#cbd5e1'
          const markets = [
            { label: '沪市', value: shAmounts[index], color: segmentColor(index, 0) },
            { label: '深市', value: szAmounts[index], color: segmentColor(index, 1) },
            { label: '北交所', value: bjAmounts[index], color: segmentColor(index, 2) }
          ]
          const marketLines = markets
            .map(
              (market) =>
                `<div style="display:flex;justify-content:space-between;gap:26px;margin-top:4px;"><span><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${market.color};margin-right:6px;"></i>${market.label}</span><b>${(Number(market.value || 0) / 10000).toFixed(2)} 万亿</b></div>`
            )
            .join('')
          const forecastLine =
            index === days.length - 1 && forecast.available
              ? `<div style="display:flex;justify-content:space-between;gap:26px;margin-top:4px;color:#fef08a;"><span>预计全天</span><b>${(forecastAmountInBillion / 10000).toFixed(2)} 万亿</b></div>`
              : ''
          return `<div style="font-weight:700;margin-bottom:6px;">${formatTrendDate(days[index])}</div><div style="display:flex;justify-content:space-between;gap:26px;border-bottom:1px solid #334155;padding-bottom:6px;"><span>三市合计</span><b>${(value / 10000).toFixed(2)} 万亿</b></div>${marketLines}${forecastLine}<div style="margin-top:7px;color:${changeColor};">${changeText}</div><div style="margin-top:4px;color:${active ? '#86efac' : '#fda4af'};">${active ? '交易活跃，可交易环境' : '低于 2 万亿：交易冷清，赚钱难度极大'}</div>`
        }
      },
      grid: { left: 34, right: 18, top: 48, bottom: 30, containLabel: true },
      xAxis: {
        type: 'category' as const,
        data: days,
        axisLine: { lineStyle: { color: palette.axisLine } },
        axisLabel: { color: palette.axisLabel, fontSize: 10, interval: Math.max(0, Math.floor(days.length / 6) - 1), formatter: (value: string) => formatTrendDate(value) }
      },
      yAxis: {
        type: 'value' as const,
        axisLabel: { color: palette.axisLabel, fontSize: 10, formatter: (value: number) => `${(value / 10000).toFixed(1)}万亿` },
        splitLine: { lineStyle: { color: palette.splitLine, type: 'dashed' as const } }
      },
      series: [
        {
          name: '沪市',
          type: 'bar' as const,
          stack: '三市成交额',
          barMaxWidth: 24,
          data: shAmountsInBillion,
          itemStyle: { color: (item: any) => segmentColor(item.dataIndex, 0), borderRadius: [0, 0, 0, 0] }
        },
        {
          name: '深市',
          type: 'bar' as const,
          stack: '三市成交额',
          barMaxWidth: 24,
          data: szAmountsInBillion,
          itemStyle: { color: (item: any) => segmentColor(item.dataIndex, 1), borderRadius: [0, 0, 0, 0] }
        },
        {
          name: '北交所',
          type: 'bar' as const,
          stack: '三市成交额',
          barMaxWidth: 24,
          data: bjAmountsInBillion,
          itemStyle: { color: (item: any) => segmentColor(item.dataIndex, 2), borderRadius: [4, 4, 0, 0] },
          markLine: {
            symbol: 'none',
            lineStyle: { color: '#facc15', type: 'dashed' as const, width: 2 },
            label: { color: palette.markLineLabel, formatter: '2万亿交易活跃底线' },
            data: [{ yAxis: thresholdInBillion }]
          },
          markPoint: {
            symbol: 'circle',
            symbolSize: 7,
            label: {
              show: true,
              position: 'top' as const,
              distance: 7,
              color: '#f8fafc',
              fontSize: 11,
              fontWeight: 700,
              backgroundColor: 'rgba(15, 23, 42, 0.88)',
              borderRadius: 3,
              padding: [3, 5],
              formatter: ({ name, value }: any) => `${name} ${(Number(value) / 10000).toFixed(2)}万亿`
            },
            data: turnoverExtremaMarkPoints
          }
        },
        {
          name: '预计剩余成交额（虚拟）',
          type: 'bar' as const,
          stack: '三市成交额',
          barMaxWidth: 24,
          data: forecastRemainingInBillion,
          itemStyle: { color: 'rgba(250, 204, 21, 0.26)', borderColor: '#facc15', borderType: 'dashed' as const, borderWidth: 1, borderRadius: [4, 4, 0, 0] },
          markPoint: {
            symbol: 'diamond',
            symbolSize: 9,
            label: {
              show: true,
              position: 'top' as const,
              distance: 7,
              color: '#fef08a',
              fontSize: 11,
              fontWeight: 700,
              backgroundColor: 'rgba(15, 23, 42, 0.9)',
              borderRadius: 3,
              padding: [3, 5],
              formatter: ({ value }: any) => `预计全天 ${(Number(value) / 10000).toFixed(2)}万亿`
            },
            data: forecastMarkPoint
          }
        }
      ]
    }
  }, [turnoverTrend, intradayTurnoverForecast, palette])

  /* ---------------- 渲染 ---------------- */
  const UP = 'text-red-600 dark:text-red-400'
  const DOWN = 'text-green-600 dark:text-green-500'

  return (
    <div className="min-h-full bg-zinc-100 px-4 py-6 dark:bg-zinc-950 sm:px-6">
      <div className="mx-auto max-w-[1400px]">
        {/* 头部 */}
        <PageHeader
          title="大盘情绪概览"
          subtitle={<span className="tabular-nums">{sentiment?.date || tradingDate || '加载中...'}</span>}
        />

        {/* 主要指数 */}
        <section className="mb-8">
          <SectionTitle
            title="主要指数"
            actions={
              <Button plain onClick={() => setShowAllIndices((v) => !v)}>
                {showAllIndices ? '收起' : '更多'}
              </Button>
            }
          />
          {displayIndices.length > 0 ? (
            <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-4">
              {displayIndices.map((index) => {
                const isUp = index.change > 0
                const isDown = index.change < 0
                return (
                  <Card
                    key={index.code}
                    onClick={() => navigate(`/index/${index.code}`)}
                    className={`cursor-pointer p-5 transition-shadow hover:shadow-md ${
                      isUp ? 'border-l-4 border-l-red-500' : isDown ? 'border-l-4 border-l-green-500' : ''
                    }`}
                  >
                    <div className="text-sm text-zinc-500 dark:text-zinc-400">{index.name}</div>
                    <div className="mb-2 text-xs text-zinc-400 dark:text-zinc-500">{index.code}</div>
                    <div
                      className={`mb-2 text-2xl font-bold tabular-nums ${
                        isUp ? UP : isDown ? DOWN : 'text-zinc-800 dark:text-zinc-100'
                      }`}
                    >
                      {formatPrice(index.price)}
                    </div>
                    <div className="mb-2">
                      <ChangeValue value={index.change_pct} className="mr-3 text-base" />
                      <ChangeValue value={index.change} digits={2} suffix="" className="text-sm" />
                    </div>
                    <div className="text-xs text-zinc-400 tabular-nums dark:text-zinc-500">成交额: {formatAmount(index.amount)}</div>
                  </Card>
                )
              })}
            </div>
          ) : (
            <EmptyState className="py-10" text="暂无指数数据 · 请在系统配置中获取指数数据" />
          )}
        </section>

        {/* 涨跌统计 */}
        {sentiment && (
          <section className="mb-8">
            <SectionTitle title="涨跌统计" />
            <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-4">
              {/* 涨跌家数 */}
              <Card>
                <div className="mb-4 text-sm font-medium text-zinc-500 dark:text-zinc-400">涨跌家数</div>
                <div className="flex justify-around text-center">
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums ${UP}`}>{sentiment.up_count || 0}</span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">上涨</span>
                  </div>
                  <div>
                    <span className="block text-2xl font-bold tabular-nums text-zinc-400 dark:text-zinc-500">{sentiment.unchanged_count || 0}</span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">平盘</span>
                  </div>
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums ${DOWN}`}>{sentiment.down_count || 0}</span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">下跌</span>
                  </div>
                </div>
                <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-green-500">
                  <div className="h-full rounded-full bg-red-500 transition-[width] duration-300" style={{ width: getUpWidth(sentiment) }} />
                </div>
              </Card>

              {/* 涨跌超5% */}
              <Card>
                <div className="mb-4 text-sm font-medium text-zinc-500 dark:text-zinc-400">涨跌超5%</div>
                <div className="flex justify-around text-center">
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums ${UP}`}>{sentiment.up_5_percent_count || 0}</span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">涨幅&gt;5%</span>
                  </div>
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums ${DOWN}`}>{sentiment.down_5_percent_count || 0}</span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">跌幅&gt;5%</span>
                  </div>
                </div>
              </Card>

              {/* 涨跌停 */}
              <Card>
                <div className="mb-4 text-sm font-medium text-zinc-500 dark:text-zinc-400">涨跌停</div>
                <div className="flex justify-around text-center">
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums text-red-700 drop-shadow-[0_0_8px_rgba(211,47,47,0.3)] dark:text-red-400`}>
                      {sentiment.limit_up_count || 0}
                    </span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">涨停</span>
                  </div>
                  <div>
                    <span className={`block text-2xl font-bold tabular-nums text-green-700 drop-shadow-[0_0_8px_rgba(56,142,60,0.3)] dark:text-green-400`}>
                      {sentiment.limit_down_count || 0}
                    </span>
                    <span className="text-xs text-zinc-400 dark:text-zinc-500">跌停</span>
                  </div>
                </div>
              </Card>

              {/* 两市成交 */}
              <Card className="flex flex-col gap-3">
                <div className="text-sm font-medium text-zinc-500 dark:text-zinc-400">两市成交</div>
                <div className="flex items-center justify-between border-b border-zinc-100 pb-2 dark:border-white/5">
                  <span className="text-sm text-zinc-500 dark:text-zinc-400">上证指数</span>
                  <span className="text-base font-semibold tabular-nums text-zinc-800 dark:text-zinc-100">{formatAmount(sentiment.sh_amount || 0)}</span>
                </div>
                <div className="flex items-center justify-between border-b border-zinc-100 pb-2 dark:border-white/5">
                  <span className="text-sm text-zinc-500 dark:text-zinc-400">深证成指</span>
                  <span className="text-base font-semibold tabular-nums text-zinc-800 dark:text-zinc-100">{formatAmount(sentiment.sz_amount || 0)}</span>
                </div>
                <div className="mt-1 flex items-center justify-between border-t-2 border-zinc-200 pt-3 dark:border-white/10">
                  <span className="text-sm text-zinc-500 dark:text-zinc-400">合计</span>
                  <span className="text-lg font-bold tabular-nums text-indigo-500 dark:text-indigo-400">
                    {formatAmount((sentiment.sh_amount || 0) + (sentiment.sz_amount || 0))}
                  </span>
                </div>
              </Card>
            </div>

            {/* 30天情绪曲线 + 涨跌分布 */}
            <div className="mt-4 grid grid-cols-1 items-stretch gap-4 lg:grid-cols-2">
              {emotionCurve30d.days.length > 0 && (
                <Card className="min-h-[286px] px-4 pb-3 pt-4">
                  <div className="mb-1.5 flex items-center justify-between gap-3">
                    <h3 className="text-[15px] text-zinc-700 dark:text-zinc-200">30天情绪曲线</h3>
                    <div className="flex flex-wrap justify-end gap-1.5">
                      <Badge color="red">{sentiment.up_5_percent_count || 0}</Badge>
                      <Badge color="green">{sentiment.down_5_percent_count || 0}</Badge>
                      <Badge color="orange">{sentiment.limit_up_count || 0}</Badge>
                      <Badge color="cyan">{sentiment.limit_down_count || 0}</Badge>
                    </div>
                  </div>
                  <div className="mb-2 text-[11px] text-zinc-400 dark:text-zinc-500">涨跌家数 / 涨跌停 / 5%家数按各自30日区间归一化</div>
                  <div className="w-full cursor-crosshair" onClick={handleSentimentCurveClick}>
                    {sentimentCurveOption && <EChart option={sentimentCurveOption} height={220} />}
                  </div>
                  {selectedSentimentSnapshot && (
                    <div className="mt-1 rounded-lg border border-zinc-200 bg-zinc-50 p-2.5 text-xs text-zinc-600 dark:border-white/10 dark:bg-white/5 dark:text-zinc-300">
                      <div className="mb-1.5 flex items-center gap-2">
                        <strong className="text-blue-800 dark:text-blue-300">{selectedSentimentSnapshot.date}</strong>
                        <span className="text-zinc-500 dark:text-zinc-400">当天情绪数据</span>
                        <button
                          type="button"
                          className="ml-auto cursor-pointer rounded border border-zinc-300 bg-white px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100 dark:border-white/15 dark:bg-transparent dark:text-zinc-300 dark:hover:bg-white/10"
                          onClick={() => setSelectedSentimentIndex(null)}
                        >
                          关闭
                        </button>
                      </div>
                      <div className="grid grid-cols-3 gap-x-2.5 gap-y-1">
                        <span>上涨 {selectedSentimentSnapshot.up}</span>
                        <span>下跌 {selectedSentimentSnapshot.down}</span>
                        <span>涨停 {selectedSentimentSnapshot.limitUp}</span>
                        <span>跌停 {selectedSentimentSnapshot.limitDown}</span>
                        <span>涨幅≥5% {selectedSentimentSnapshot.up5}</span>
                        <span>跌幅≤-5% {selectedSentimentSnapshot.down5}</span>
                      </div>
                    </div>
                  )}
                </Card>
              )}

              {sentimentDistribution.length > 0 && (
                <div className="rounded-xl bg-white p-4 shadow-md ring-1 ring-zinc-950/10 dark:bg-zinc-900/80 dark:ring-white/10">
                  <div className="mb-3 flex items-center justify-between">
                    <h3 className="text-base text-zinc-900 dark:text-zinc-100">涨跌分布</h3>
                    <div className="flex items-center gap-1.5 text-base font-bold tabular-nums">
                      <span className="text-red-600 dark:text-red-400">{sentiment.up_count || 0}</span>
                      <span className="text-zinc-400 dark:text-zinc-500">:</span>
                      <span className="text-yellow-600 dark:text-yellow-300">{sentiment.unchanged_count || 0}</span>
                      <span className="text-zinc-400 dark:text-zinc-500">:</span>
                      <span className="text-emerald-600 dark:text-emerald-400">{sentiment.down_count || 0}</span>
                    </div>
                  </div>
                  <div className="grid grid-cols-[repeat(9,minmax(40px,1fr))] items-end gap-2.5 min-h-[160px] max-[900px]:grid-cols-3">
                    {sentimentDistribution.map((item) => (
                      <div key={item.label} className="flex flex-col items-center justify-end gap-1">
                        <div
                          className={`w-full max-w-8 rounded-t-md ${
                            item.side === 'up'
                              ? 'bg-gradient-to-b from-red-400 to-red-600'
                              : item.side === 'down'
                                ? 'bg-gradient-to-b from-emerald-400 to-emerald-600'
                                : 'bg-gradient-to-b from-yellow-300 to-yellow-600'
                          }`}
                          style={{ height: getDistributionHeight(item.count) }}
                        />
                        <div className="text-xs text-zinc-500 dark:text-zinc-400">{item.label}</div>
                        <div className="text-xs font-semibold tabular-nums text-zinc-800 dark:text-zinc-100">{item.count}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </section>
        )}

        {/* 融资客情绪 */}
        <section className="mb-6">
          <div className="mb-4">
            <SectionTitle
              title="融资客情绪"
              actions={
                marginSentiment?.available ? (
                  <span className="whitespace-nowrap text-[13px] text-zinc-400 tabular-nums dark:text-zinc-300">{marginSentiment.latest?.date}</span>
                ) : undefined
              }
            />
            <p className="-mt-1 text-xs text-zinc-500 dark:text-zinc-400">交易所盘后确认数据，统计沪深两市融资余额与融资净买入</p>
          </div>
          {marginSentiment?.available ? (
            <div ref={marginCardRef} className="relative rounded-lg border border-zinc-200 bg-zinc-50 px-4 pb-2.5 pt-3.5 dark:border-slate-600/60 dark:bg-slate-900">
              <div className="grid grid-cols-2 gap-3.5 md:grid-cols-4 max-[900px]:grid-cols-2">
                <div className="grid gap-1">
                  <span className="text-xs text-zinc-500 dark:text-slate-400">融资余额</span>
                  <strong className="text-xl tabular-nums text-zinc-900 dark:text-white">{formatMarginTrillion(marginSentiment.latest?.financing_balance)}</strong>
                </div>
                <div className="grid gap-1">
                  <span className="text-xs text-zinc-500 dark:text-slate-400">融券余额</span>
                  <strong className="text-xl tabular-nums text-zinc-900 dark:text-white">{formatMarginBillions(marginSentiment.latest?.securities_lending_balance)}</strong>
                </div>
                <div className="grid gap-1">
                  <span className="text-xs text-zinc-500 dark:text-slate-400">融资余额日变动</span>
                  <strong className={`text-xl tabular-nums ${Number(marginSentiment.latest?.financing_net_buy || 0) >= 0 ? 'text-rose-600 dark:text-rose-400' : 'text-green-600 dark:text-green-400'}`}>
                    {formatMarginBillions(marginSentiment.latest?.financing_net_buy)}
                  </strong>
                </div>
                <div className="grid gap-1">
                  <span className="text-xs text-zinc-500 dark:text-slate-400">融券余额日变动</span>
                  <strong className={`text-xl tabular-nums ${Number(marginSentiment.latest?.securities_lending_balance_change || 0) >= 0 ? 'text-rose-600 dark:text-rose-400' : 'text-green-600 dark:text-green-400'}`}>
                    {formatMarginBillions(marginSentiment.latest?.securities_lending_balance_change)}
                  </strong>
                </div>
              </div>

              <div className="mt-3.5 flex justify-between gap-2.5 text-xs text-zinc-600 dark:text-slate-300">
                <span>余额趋势</span>
                <span className="text-zinc-400 dark:text-slate-400">融资余额 / 融券余额</span>
              </div>
              <svg
                className="mt-1 block h-[150px] w-full"
                viewBox="0 0 800 150"
                preserveAspectRatio="none"
                role="img"
                aria-label="近30日融资余额和融券余额趋势"
                onMouseMove={updateMarginHover}
                onMouseLeave={() => setHoveredMarginIndex(null)}
              >
                <line x1={36} x2={782} y1={126} y2={126} className="stroke-zinc-300 dark:stroke-slate-500" />
                <polyline points={financingBalancePoints} fill="none" className="stroke-rose-500 dark:stroke-rose-400" strokeWidth={2.4} style={{ vectorEffect: 'non-scaling-stroke' }} />
                <polyline points={securitiesLendingBalancePoints} fill="none" className="stroke-sky-500 dark:stroke-sky-400" strokeWidth={2.2} style={{ vectorEffect: 'non-scaling-stroke' }} />
                {hoveredMarginIndex !== null && <line x1={marginHoverX} x2={marginHoverX} y1={12} y2={126} className="stroke-zinc-900/40 dark:stroke-white/70" strokeWidth={1} strokeDasharray="3 3" pointerEvents="none" />}
                <rect x={36} y={12} width={746} height={114} fill="transparent" className="cursor-crosshair" />
                <text x={4} y={22} className="fill-zinc-500 dark:fill-slate-400 text-[11px]">融资</text>
                <text x={764} y={22} className="fill-zinc-500 dark:fill-slate-400 text-[11px]">融券</text>
              </svg>
              <div className="flex gap-4 text-[11px] text-zinc-500 dark:text-slate-400">
                <span className="before:mr-1.5 before:inline-block before:h-2 before:w-2 before:bg-rose-500 dark:before:bg-rose-400">融资余额</span>
                <span className="before:mr-1.5 before:inline-block before:h-2 before:w-2 before:bg-sky-500 dark:before:bg-sky-400">融券余额</span>
              </div>

              <div className="mt-3.5 flex justify-between gap-2.5 text-xs text-zinc-600 dark:text-slate-300">
                <span>每日余额变动</span>
                <span className="text-zinc-400 dark:text-slate-400">柱体向上为增加，向下为减少</span>
              </div>
              <svg
                className="mt-1 block h-[150px] w-full"
                viewBox="0 0 800 150"
                preserveAspectRatio="none"
                role="img"
                aria-label="近30日融资和融券余额每日变动"
                onMouseMove={updateMarginHover}
                onMouseLeave={() => setHoveredMarginIndex(null)}
              >
                <line x1={36} x2={782} y1={75} y2={75} className="stroke-zinc-300 dark:stroke-slate-500" strokeDasharray="4 4" />
                {financingChangeBars.map((bar) => (
                  <rect key={`financing-${bar.index}`} x={bar.x} y={bar.y} width={bar.width} height={bar.height} className={bar.value >= 0 ? 'fill-rose-500 dark:fill-rose-400' : 'fill-green-500 dark:fill-green-400'} />
                ))}
                {securitiesLendingChangeBars.map((bar) => (
                  <rect key={`lending-${bar.index}`} x={bar.x} y={bar.y} width={bar.width} height={bar.height} className={bar.value >= 0 ? 'fill-sky-500 dark:fill-sky-400' : 'fill-violet-500 dark:fill-violet-400'} />
                ))}
                {hoveredMarginIndex !== null && <line x1={marginHoverX} x2={marginHoverX} y1={12} y2={138} className="stroke-zinc-900/40 dark:stroke-white/70" strokeWidth={1} strokeDasharray="3 3" pointerEvents="none" />}
                <rect x={36} y={12} width={746} height={126} fill="transparent" className="cursor-crosshair" />
                <text x={4} y={22} className="fill-zinc-500 dark:fill-slate-400 text-[11px]">增加</text>
                <text x={4} y={142} className="fill-zinc-500 dark:fill-slate-400 text-[11px]">减少</text>
              </svg>
              <div className="flex gap-4 text-[11px] text-zinc-500 dark:text-slate-400">
                <span className="before:mr-1.5 before:inline-block before:h-2 before:w-2 before:bg-rose-500 dark:before:bg-rose-400">融资变动</span>
                <span className="before:mr-1.5 before:inline-block before:h-2 before:w-2 before:bg-sky-500 dark:before:bg-sky-400">融券变动</span>
              </div>
              <div className="flex justify-between text-[11px] text-zinc-500 dark:text-slate-400 tabular-nums">
                <span>{marginSentiment.dates?.[0]}</span>
                <span>{marginSentiment.dates?.[marginSentiment.dates.length - 1]}</span>
              </div>

              {marginHoverData && (
                <div
                  className="pointer-events-none absolute z-10 w-[250px] rounded-md border border-zinc-200 bg-white/95 p-2.5 text-xs leading-relaxed text-zinc-700 shadow-lg dark:border-slate-400/50 dark:bg-slate-900/95 dark:text-blue-100"
                  style={{ left: marginHoverPosition.x, top: marginHoverPosition.y }}
                >
                  <div className="mb-1.5 font-bold text-zinc-900 dark:text-slate-50">{marginHoverData.date} 当日两融明细</div>
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                    <span className="flex flex-col text-zinc-500 dark:text-slate-400">融资余额 <b className="text-[13px] text-zinc-900 dark:text-slate-50">{formatMarginTrillion(marginHoverData.financingBalance)}</b></span>
                    <span className="flex flex-col text-zinc-500 dark:text-slate-400">融券余额 <b className="text-[13px] text-zinc-900 dark:text-slate-50">{formatMarginBillions(marginHoverData.securitiesLendingBalance)}</b></span>
                    <span className={`flex flex-col ${marginHoverData.financingChange >= 0 ? 'text-rose-600 dark:text-rose-300' : 'text-green-600 dark:text-green-300'}`}>
                      融资日变动 <b className="text-[13px]">{formatMarginBillions(marginHoverData.financingChange)}</b>
                    </span>
                    <span className={`flex flex-col ${marginHoverData.securitiesLendingChange >= 0 ? 'text-rose-600 dark:text-rose-300' : 'text-green-600 dark:text-green-300'}`}>
                      融券日变动 <b className="text-[13px]">{formatMarginBillions(marginHoverData.securitiesLendingChange)}</b>
                    </span>
                    <span className="flex flex-col text-zinc-500 dark:text-slate-400">两融余额 <b className="text-[13px] text-zinc-900 dark:text-slate-50">{formatMarginTrillion(marginHoverData.marginBalance)}</b></span>
                    <span className="flex flex-col text-zinc-500 dark:text-slate-400">融资买入额 <b className="text-[13px] text-zinc-900 dark:text-slate-50">{formatMarginBillions(marginHoverData.financingBuy)}</b></span>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <EmptyState text="两融汇总数据等待收盘后同步" />
          )}
        </section>

        {/* 情绪周期 */}
        <section className="mb-8">
          <SectionTitle
            title="情绪周期"
            actions={
              <Button outline onClick={() => navigate('/system-config')}>
                去系统配置修复
              </Button>
            }
          />

          {currentEmotionPhase && (
            <div className="mb-3 rounded-2xl border border-slate-300/50 bg-slate-900/70 px-4 py-3 text-zinc-900 shadow-lg backdrop-blur dark:text-slate-50" style={(dark ? PHASE_CELL_STYLE : PHASE_CELL_STYLE_LIGHT)[currentEmotionPhase.phase_code] || PHASE_CELL_STYLE.start}>
              <div className="flex items-center justify-between gap-2">
                <div className="text-lg font-bold tracking-wide">{currentEmotionPhase.phase_name}</div>
                <div className="text-xs text-blue-700 dark:text-blue-200">置信度 {Math.round((currentEmotionPhase.confidence || 0) * 100)}%</div>
              </div>
              <div className="mt-2 text-sm font-semibold">{currentEmotionPhase.action_hint}</div>
              {currentEmotionPhase.reason_summary && <div className="mt-1.5 text-xs leading-relaxed text-zinc-600 dark:text-slate-200">{currentEmotionPhase.reason_summary}</div>}
              {(currentEmotionPhase.data_status as any)?.is_stale && (
                <div className="mt-1.5 text-xs text-amber-700 dark:text-yellow-200">情绪周期日期与行情日期不一致，请先执行“更新最新情绪”。</div>
              )}
            </div>
          )}
          {intradayEmotionHint && (
            <div className="mb-2.5 rounded-lg border border-sky-500/60 bg-blue-50 px-3 py-2 text-xs text-blue-700 dark:bg-blue-900/25 dark:text-blue-200">{intradayEmotionHint}</div>
          )}

          <div
            className="min-h-[548px] rounded-2xl border border-zinc-200 bg-gradient-to-b from-white to-zinc-50 p-2.5 pb-2 shadow-xl dark:border-white/5 dark:from-[#20252f] dark:to-[#171c26] max-[900px]:min-h-[520px]"
            onMouseLeave={() => setHoveredEmotionIndex(null)}
          >
            <div className="mb-2 flex flex-wrap items-start justify-between gap-3.5 max-[900px]:flex-col">
              <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1.5">
                {EMOTION_CYCLE_SERIES_META.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    className={`inline-flex cursor-pointer items-center gap-1.5 whitespace-nowrap text-[11px] font-semibold text-zinc-600 hover:text-zinc-900 dark:text-slate-200 dark:hover:text-white ${
                      !emotionLegendSelected[item.name] ? 'opacity-35' : ''
                    }`}
                    onClick={() => toggleEmotionLegend(item.name)}
                  >
                    <span className="h-2 w-2 rounded-full border border-zinc-400 bg-zinc-300 dark:border-white/70 dark:bg-[#d6dce8]" />
                    <span>{item.name}</span>
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2.5 max-[900px]:w-full max-[900px]:justify-between">
                <div className="inline-flex shrink-0 overflow-hidden rounded-lg border border-zinc-300 bg-zinc-100 dark:border-slate-400/40 dark:bg-slate-900/50">
                  <button
                    type="button"
                    className={`cursor-pointer border-none px-2.5 py-1 text-xs font-bold ${emotionGranularity === 'daily' ? 'bg-blue-500/15 text-blue-700 dark:bg-blue-500/25 dark:text-white' : 'bg-transparent text-zinc-500 dark:text-slate-300'}`}
                    onClick={() => changeEmotionGranularity('daily')}
                  >
                    日线
                  </button>
                  <button
                    type="button"
                    className={`cursor-pointer border-none px-2.5 py-1 text-xs font-bold ${emotionGranularity === 'hourly' ? 'bg-blue-500/15 text-blue-700 dark:bg-blue-500/25 dark:text-white' : 'bg-transparent text-zinc-500 dark:text-slate-300'}`}
                    onClick={() => changeEmotionGranularity('hourly')}
                  >
                    小时
                  </button>
                </div>
                <div className="flex max-w-[330px] flex-wrap items-center justify-end gap-x-2 gap-y-1.5">
                  {EMOTION_SCORE_BANDS.map((band) => (
                    <span key={band.key} className="inline-flex items-center gap-1 whitespace-nowrap text-[11px] font-semibold text-zinc-600 dark:text-slate-300">
                      <i className="block h-2 w-2 rounded-full" style={{ backgroundColor: band.color, boxShadow: `0 0 10px ${band.color}` }} />
                      {band.label}
                    </span>
                  ))}
                </div>
              </div>
            </div>

            {latestEmotionMetrics.length > 0 && (
              <div className="mb-2 mt-1.5 grid grid-cols-8 gap-1.5 max-[900px]:grid-cols-2">
                {latestEmotionMetrics.map((item) => (
                  <div
                    key={item.key}
                    className="flex h-[26px] items-center justify-between gap-1.5 rounded-md border px-2"
                    style={{ borderColor: `${item.band.color}59`, background: item.band.bg }}
                  >
                    <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-[11px] font-semibold text-zinc-700 dark:text-slate-200">{item.shortName}</span>
                    <span className="whitespace-nowrap text-[13px] font-extrabold tabular-nums" style={{ color: bandTone(item.band) }}>
                      {item.valueText}
                    </span>
                  </div>
                ))}
              </div>
            )}

            <div className="relative min-h-[452px] overflow-hidden rounded-2xl bg-gradient-to-b from-zinc-900/[0.04] to-zinc-900/[0.02] dark:from-slate-900/20 dark:to-slate-900/5">
              {emotionChartModel && (
                <svg className="block h-[452px] w-full" viewBox="0 0 1100 470" preserveAspectRatio="none">
                  <defs>
                    {emotionChartModel.bands.map((band) => (
                      <clipPath key={`clip-${band.key}`} id={`emotion-band-clip-${band.key}`} clipPathUnits="userSpaceOnUse">
                        <rect x={emotionChartModel.padding.left} y={band.y} width={emotionChartModel.plotWidth} height={band.height} />
                      </clipPath>
                    ))}
                  </defs>

                  {emotionChartModel.bands.map((band) => (
                    <rect key={`band-${band.key}`} x={emotionChartModel.padding.left} y={band.y} width={emotionChartModel.plotWidth} height={band.height} fill={band.bg} />
                  ))}

                  {emotionChartModel.yTicks.map((tick) => (
                    <g key={`y-${tick}`}>
                      <line
                        x1={emotionChartModel.padding.left}
                        x2={emotionChartModel.width - emotionChartModel.padding.right}
                        y1={emotionChartModel.yScale(tick)}
                        y2={emotionChartModel.yScale(tick)}
                        className="stroke-zinc-900/10 dark:stroke-white/10"
                        strokeWidth={1}
                      />
                      <text x={emotionChartModel.padding.left - 10} y={emotionChartModel.yScale(tick) + 4} className="fill-zinc-400 dark:fill-[#aeb8d2] text-[10px]" textAnchor="end">
                        {tick}%
                      </text>
                    </g>
                  ))}

                  <text x={emotionChartModel.padding.left} y={emotionChartModel.padding.top - 12} className="fill-zinc-700 dark:fill-[#ecf1ff] text-xs font-semibold">
                    上涨比例(%)
                  </text>

                  {emotionChartModel.bands.map((band) => (
                    <text
                      key={`label-${band.key}`}
                      x={emotionChartModel.width - emotionChartModel.padding.right - 8}
                      y={band.y + band.height / 2 + 4}
                      className="fill-zinc-400/80 dark:fill-slate-300/50 text-[10px] font-bold"
                      textAnchor="end"
                    >
                      {band.label}
                    </text>
                  ))}

                  {emotionChartModel.xLabels.map((label) => (
                    <text key={`x-${label.index}`} x={label.x} y={emotionChartModel.height - 10} className="fill-zinc-400 dark:fill-[#aeb8d2] text-[10px]" textAnchor="middle">
                      {label.text}
                    </text>
                  ))}

                  {emotionChartModel.series.map((seriesItem) => (
                    <g key={seriesItem.key}>
                      {emotionChartModel.bands.map((band) => (
                        <path
                          key={`${seriesItem.key}-${band.key}`}
                          d={seriesItem.path}
                          fill="none"
                          stroke={band.tone}
                          strokeWidth={seriesItem.strokeWidth}
                          strokeDasharray={seriesItem.dasharray}
                          clipPath={`url(#emotion-band-clip-${band.key})`}
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          className="opacity-95"
                        />
                      ))}
                    </g>
                  ))}

                  {hoveredEmotionIndex !== null && emotionChartModel.columns[hoveredEmotionIndex] && (
                    <g>
                      <line
                        x1={emotionChartModel.columns[hoveredEmotionIndex].x}
                        x2={emotionChartModel.columns[hoveredEmotionIndex].x}
                        y1={emotionChartModel.padding.top}
                        y2={emotionChartModel.height - emotionChartModel.padding.bottom}
                        className="stroke-zinc-900/25 dark:stroke-white/30"
                        strokeWidth={1}
                        strokeDasharray="5 5"
                      />
                      {emotionChartModel.hoverPoints(hoveredEmotionIndex).map((point) => (
                        <circle key={`${point.key}-${hoveredEmotionIndex}`} cx={point.x} cy={point.y} r={4} fill={point.color} stroke={dark ? '#f8fafc' : '#ffffff'} strokeWidth={1.4} />
                      ))}
                    </g>
                  )}

                  {emotionChartModel.columns.map((column) => (
                    <rect
                      key={`col-${column.index}`}
                      x={column.left}
                      y={emotionChartModel.padding.top}
                      width={column.width}
                      height={emotionChartModel.plotHeight}
                      fill="transparent"
                      onMouseEnter={() => setHoveredEmotionIndex(column.index)}
                    />
                  ))}
                </svg>
              )}

              {emotionTooltipData && (
                <div
                  className="pointer-events-none absolute z-10 min-w-[168px] max-w-[216px] rounded-xl border border-slate-900/10 bg-white/95 p-2.5 shadow-xl"
                  style={emotionTooltipStyle}
                >
                  <div className="mb-2 text-[13px] font-bold text-zinc-900">{emotionTooltipData.date}</div>
                  {emotionTooltipData.items.map((item) => (
                    <div key={item.key} className="flex items-center gap-1.5 text-[11px] leading-relaxed text-slate-700">
                      <span className="h-2 w-2 shrink-0 rounded-full border border-white/85" style={{ backgroundColor: item.color }} />
                      <span className="flex-1">{item.name}</span>
                      <span className="min-w-[56px] text-right font-bold tabular-nums text-slate-900">{item.value}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {emotionPhaseTimeline.length > 0 && (
              <div className="mt-2.5 border-t border-slate-400/25 pt-2.5">
                <div className="mb-2 flex items-center justify-between text-xs text-slate-300">
                  <span>阶段演化</span>
                  <span>
                    {emotionPhaseTimelineRange.start} - {emotionPhaseTimelineRange.end}
                  </span>
                </div>
                <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${emotionPhaseTimeline.length}, minmax(8px, 1fr))` }}>
                  {emotionPhaseTimeline.map((item) => (
                    <div
                      key={item.date}
                      className="flex min-h-[30px] items-center justify-center rounded-md border border-white/10 opacity-90 transition hover:opacity-100"
                      style={PHASE_CELL_STYLE[item.code] || PHASE_CELL_STYLE.start}
                      title={`${item.date} ${item.label} | 收盘上涨比例 ${item.closeUp}% | 强势上涨比例 ${item.strongUp}% | 涨停溢价 ${item.followRate}%`}
                    >
                      {item.showLabel && <span className="text-[10px] font-semibold whitespace-nowrap text-white">{item.label}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </section>

        {/* 市场成交额趋势 */}
        <section className="mb-8 rounded-2xl border border-zinc-200 bg-gradient-to-b from-white to-zinc-50 p-4 shadow-sm dark:border-white/10 dark:bg-gradient-to-b dark:from-[#20252f] dark:to-[#171c26] dark:shadow-xl">
          <div className="mb-2.5 flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-zinc-900 dark:text-slate-100">市场成交额趋势</h2>
              <p className="mt-1 text-[13px] text-zinc-500 dark:text-slate-300">两市成交额低于 2 万亿元，说明交易冷清，赚钱难度极大。</p>
            </div>
            <div
              className={`whitespace-nowrap rounded-lg px-2.5 py-1.5 text-[13px] font-bold ${
                turnoverIsActive ? 'bg-green-600/15 text-green-700 dark:bg-green-600/20 dark:text-green-300' : 'bg-red-600/15 text-red-700 dark:bg-red-600/20 dark:text-red-300'
              }`}
            >
              {latestTurnoverText}
            </div>
          </div>
          {intradayTurnoverForecast.available ? (
            <div className="mb-2.5 flex flex-wrap gap-x-4 gap-y-2 rounded-lg border border-sky-500/40 bg-sky-50 px-2.5 py-2 text-xs text-sky-800 dark:border-sky-500/30 dark:bg-blue-900/15 dark:text-blue-200">
              <span>盘中累计 {formatTurnoverTrillion(intradayTurnoverForecast.current_amounts?.total)}（截至 {intradayTurnoverForecast.time_bucket}）</span>
              <strong className="text-amber-700 dark:text-yellow-200">预计全天 {formatTurnoverTrillion(intradayTurnoverForecast.forecast_amount)}</strong>
              <span>
                区间 {formatTurnoverTrillion(intradayTurnoverForecast.forecast_interval?.lower)} - {formatTurnoverTrillion(intradayTurnoverForecast.forecast_interval?.upper)}
              </span>
              <span>置信度 {Math.round((intradayTurnoverForecast.confidence || 0) * 100)}%</span>
              {intradayTurnoverForecast.fallback && <span>临时估算，5分钟历史样本就绪后自动切换为数据驱动预测</span>}
              <span>浅黄虚线段＝预计剩余成交额</span>
            </div>
          ) : (
            intradayTurnoverForecast.reason && (
              <div className="mb-2.5 flex flex-wrap gap-x-4 gap-y-2 rounded-lg border border-zinc-300 bg-zinc-100 px-2.5 py-2 text-xs text-zinc-600 dark:border-slate-400/25 dark:bg-slate-500/10 dark:text-slate-300">
                盘中全天成交额预测：{intradayTurnoverForecast.reason}
              </div>
            )
          )}
          {turnoverTrendOption && <EChart option={turnoverTrendOption} height={285} />}
        </section>
      </div>
    </div>
  )
}

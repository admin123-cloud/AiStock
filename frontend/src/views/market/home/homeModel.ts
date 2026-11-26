/* 首页大盘与情绪周期纯领域常量、类型与计算辅助函数（从 Home.tsx 抽离） */

export const MAIN_INDICES = ['000001.SH', '399001.SZ', '399006.SZ', '000680.SH']

export const EMOTION_SCORE_BANDS = [
  { key: 'ice', label: '冰点', min: 0, max: 20, color: '#39d353', bg: 'rgba(57, 211, 83, 0.10)' },
  { key: 'repair', label: '修复', min: 20, max: 40, color: '#78d64b', bg: 'rgba(120, 214, 75, 0.10)' },
  { key: 'start', label: '启动', min: 40, max: 60, color: '#f3d12f', bg: 'rgba(243, 209, 47, 0.12)' },
  { key: 'ferment', label: '发酵', min: 60, max: 80, color: '#ff7a2f', bg: 'rgba(255, 122, 47, 0.12)' },
  { key: 'hot', label: '高潮', min: 80, max: 100, color: '#ff3b30', bg: 'rgba(255, 59, 48, 0.12)' }
] as const

export interface EmotionSeriesMeta {
  key: string
  name: string
  shortName: string
  color: string
  width: number
  lineType: 'solid' | 'dashed'
}

export const EMOTION_CYCLE_SERIES_META: EmotionSeriesMeta[] = [
  { key: 'close_up_rate', name: '收盘上涨比例', shortName: '收盘', color: '#d8dde8', width: 2.2, lineType: 'solid' },
  { key: 'intraday_up_rate', name: '盘中上涨比例', shortName: '盘中', color: '#b8c4d9', width: 2.2, lineType: 'solid' },
  { key: 'sh_up_rate', name: '上证上涨比例', shortName: '上证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'sz_up_rate', name: '深证上涨比例', shortName: '深证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'cyb_up_rate', name: '创业板上涨比例', shortName: '创业板', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_monster_up_rate', name: '上日妖股上涨比例', shortName: '妖股', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_strong_up_rate', name: '上日强势票上涨比例', shortName: '强势', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_weak_up_rate', name: '上日弱势票上涨比例', shortName: '弱势', color: '#e2e8f0', width: 2.0, lineType: 'dashed' }
]

export const PHASE_LABEL_MAP: Record<string, string> = {
  ice: '冰点',
  start: '启动',
  ferment: '发酵',
  euphoria: '高潮',
  decline: '退潮'
}

export const PHASE_CARD_META: Record<string, { phase_name: string; action_hint: string; risk_hint: string }> = {
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

export const PHASE_CELL_STYLE: Record<string, React.CSSProperties> = {
  ice: { borderColor: 'rgba(56, 189, 248, 0.42)', background: 'linear-gradient(135deg, rgba(12, 74, 110, 0.72), rgba(30, 41, 59, 0.82))' },
  start: { borderColor: 'rgba(250, 204, 21, 0.42)', background: 'linear-gradient(135deg, rgba(92, 53, 15, 0.70), rgba(30, 41, 59, 0.82))' },
  ferment: { borderColor: 'rgba(74, 222, 128, 0.42)', background: 'linear-gradient(135deg, rgba(6, 78, 59, 0.72), rgba(30, 41, 59, 0.82))' },
  euphoria: { borderColor: 'rgba(248, 113, 113, 0.42)', background: 'linear-gradient(135deg, rgba(127, 29, 29, 0.72), rgba(30, 41, 59, 0.82))' },
  decline: { borderColor: 'rgba(244, 114, 182, 0.42)', background: 'linear-gradient(135deg, rgba(112, 26, 117, 0.72), rgba(30, 41, 59, 0.82))' }
}

/* 亮色主题下的阶段卡片配色（浅底） */
export const PHASE_CELL_STYLE_LIGHT: Record<string, React.CSSProperties> = {
  ice: { borderColor: 'rgba(2, 132, 199, 0.45)', background: 'linear-gradient(135deg, rgba(224, 242, 254, 0.9), rgba(241, 245, 249, 0.92))' },
  start: { borderColor: 'rgba(202, 138, 4, 0.45)', background: 'linear-gradient(135deg, rgba(254, 249, 195, 0.9), rgba(254, 252, 232, 0.92))' },
  ferment: { borderColor: 'rgba(22, 163, 74, 0.45)', background: 'linear-gradient(135deg, rgba(220, 252, 231, 0.9), rgba(240, 253, 244, 0.92))' },
  euphoria: { borderColor: 'rgba(220, 38, 38, 0.45)', background: 'linear-gradient(135deg, rgba(254, 226, 226, 0.9), rgba(255, 241, 242, 0.92))' },
  decline: { borderColor: 'rgba(192, 38, 211, 0.40)', background: 'linear-gradient(135deg, rgba(250, 232, 255, 0.9), rgba(253, 244, 255, 0.92))' }
}

/* 亮色主题下情绪分段的加深配色（原色在白底上偏浅、可读性差） */
export const BAND_LIGHT_COLORS: Record<string, string> = {
  ice: '#16a34a',
  repair: '#65a30d',
  start: '#ca8a04',
  ferment: '#ea580c',
  hot: '#dc2626'
}

/* ------------------------------------------------------------------ */
/* 工具函数                                                             */
/* ------------------------------------------------------------------ */

export const formatPrice = (price?: number | null) => {
  if (!price) return '--'
  return price.toFixed(2)
}

export const formatAmount = (amount?: number | null) => {
  if (!amount) return '--'
  if (amount >= 100000000) return (amount / 100000000).toFixed(2) + '亿'
  if (amount >= 10000) return (amount / 10000).toFixed(2) + '万'
  return amount.toFixed(2)
}

export const formatTrendDate = (dateStr?: string | null) => {
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

export const formatTrendDateCompact = (dateStr?: string | null) => {
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

export const formatTurnoverTrillion = (amount?: number | null) => `${(Number(amount || 0) / 1_000_000_000_000).toFixed(2)} 万亿`

export const getEmotionScoreBand = (value: number | null | undefined) => {
  const score = Math.max(0, Math.min(100, Number(value) || 0))
  return EMOTION_SCORE_BANDS.find((item) => score >= item.min && score <= item.max) || EMOTION_SCORE_BANDS[0]
}

export const buildSmoothPath = (points: { x: number; y: number }[]) => {
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

export interface EmotionPoint {
  closeUp: number
  strongUp: number
  followRate: number
}

export const resolveEmotionPhaseCode = (point: EmotionPoint, prevPoint: EmotionPoint): string => {
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

export interface EmotionPhaseDetail {
  phase_code: string
  phase_name: string
  confidence: number
  action_hint: string
  risk_hint: string
  reason_summary: string
  data_status?: Record<string, unknown> | null
}

export const resolveEmotionPhaseDetail = (
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


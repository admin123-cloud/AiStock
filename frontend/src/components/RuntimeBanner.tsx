import React, { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import request from '@/utils/request'

interface HealthComponent {
  name: string
  status: string
}

interface RuntimeHealth {
  preview?: boolean
  strategy_actionable?: boolean
  publisher_status?: string
  components?: HealthComponent[]
}

interface ClsNewsStatus {
  ok?: boolean
  state?: {
    last_sync_at?: string
  }
}

const COMPONENT_LABELS: Record<string, string> = {
  daily_kline_coverage: '日线覆盖',
  qmt_after_close_validation: '分钟盘后验收',
  g3_strategy_summary: 'G3摘要',
  broker_snapshot: '账户快照'
}

export function RuntimeBanner() {
  const location = useLocation()
  const [health, setHealth] = useState<RuntimeHealth | null>(null)
  const [clsStatus, setClsStatus] = useState<ClsNewsStatus | null>(null)
  const [clsStatusError, setClsStatusError] = useState(false)
  const isClsNewsRadar = location.pathname === '/news/cls-radar'

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      if (location.pathname === '/news/cls-radar') {
        try {
          const data = (await request.get('/news/cls/status', { skipErrorHandler: true })) as unknown as ClsNewsStatus
          if (!cancelled) {
            setClsStatus(data)
            setClsStatusError(data?.ok === false && !data?.state?.last_sync_at)
          }
        } catch {
          if (!cancelled) {
            setClsStatus(null)
            setClsStatusError(true)
          }
        }
        return
      }
      try {
        const data = (await request.get('/health/runtime', { skipErrorHandler: true })) as unknown as RuntimeHealth
        if (!cancelled) setHealth(data)
      } catch {
        if (!cancelled) setHealth(null)
      }
    }
    load()
    const timer = setInterval(load, 60000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [location.pathname])

  const blocked = !health?.strategy_actionable
  const title = isClsNewsRadar
    ? '财联社消息同步'
    : health == null
      ? '数据状态待确认'
      : blocked
        ? '数据交付未通过'
        : '数据交付检查通过'
  const detail = isClsNewsRadar
    ? clsStatus?.state?.last_sync_at
      ? `上次消息同步：${formatSyncTime(clsStatus.state.last_sync_at)}`
      : clsStatusError
        ? '消息同步状态暂不可用'
        : '尚未完成消息同步'
    : health?.publisher_status === 'stale'
      ? '健康发布已过期，不能沿用上次结论'
      : (health?.components || [])
          .filter((x) => !['healthy', 'deferred'].includes(x.status))
          .map((x) => COMPONENT_LABELS[x.name] || x.name)
          .join('、') || '详细验收见左侧数据健康；策略准入另行检查'

  const bannerClass = isClsNewsRadar
    ? clsStatusError
      ? 'bg-orange-50 text-orange-800 dark:bg-orange-950/50 dark:text-orange-300'
      : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'
    : blocked
      ? 'bg-orange-50 text-orange-800 dark:bg-orange-950/50 dark:text-orange-300'
      : 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300'

  return (
    <div
      role="status"
      className={`mb-4 flex flex-wrap gap-x-3 rounded-lg px-4 py-2.5 text-sm leading-relaxed ${bannerClass}`}
    >
      <strong className="shrink-0">{!isClsNewsRadar && health?.preview ? '只读快照预览 · ' : ''}{title}</strong>
      <span>{detail}</span>
    </div>
  )
}

function formatSyncTime(value: string) {
  return value
    .replace('T', ' ')
    .replace(/\.\d+(?=[+-]\d{2}:\d{2}|Z$)/, '')
    .replace(/([+-]\d{2}:\d{2}|Z)$/, '')
}
